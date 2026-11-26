"""Local calendar plans and durable dispatch history for the desktop host."""
# 主要引用方包括 `server.py` 和 `workbench.py`。

from contextlib import contextmanager
from datetime import date, datetime, time as wall_time, timedelta, timezone
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import operations, permissions, runtime as RT, scheduled_execution, workbench as W
from .filelock import FileLock

IN_FLIGHT_SQL = "('queued','dispatching','running','waiting','unknown')"
POLL_SECONDS = 0.5
DISPATCH_GRACE_SECONDS = 8
LOG = logging.getLogger(__name__)


def _zone(name):
    if name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise W.Problem("invalid_timezone", "时区不可用，请选择 IANA 时区并核对本机时区数据") from error


def _timing(raw):
    if not isinstance(raw, dict):
        raise W.Problem("invalid_schedule", "请选择计划日期与时区")
    frequency, clock, zone = (raw.get(key) for key in ("frequency", "time", "timezone"))
    if (frequency not in ("once", "daily", "weekly") or not isinstance(clock, str)
            or not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", clock)):
        raise W.Problem("invalid_schedule", "请选择单次、每日或每周并填写有效时间")
    if not isinstance(zone, str) or not zone or len(zone) > 80:
        raise W.Problem("invalid_timezone", "请选择 IANA 时区")
    _zone(zone)
    result = dict(frequency=frequency, timezone=zone)
    try:
        result["time"] = wall_time.fromisoformat(clock).isoformat()
        if frequency == "once":
            day = raw.get("date")
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise ValueError("date")
            result["date"] = date.fromisoformat(day).isoformat()
        if frequency == "weekly":
            weekday = raw.get("weekday")
            if type(weekday) is not int or not 0 <= weekday <= 6:
                raise ValueError("weekday")
            result["weekday"] = weekday
    except ValueError as error:
        raise W.Problem("invalid_schedule", "日期、时间或星期无效") from error
    return result


def _next_time(timing, after):
    zone = _zone(timing["timezone"])
    clock = wall_time.fromisoformat(timing["time"])
    today = datetime.fromtimestamp(after, zone).date()
    frequency = timing["frequency"]
    day = date.fromisoformat(timing["date"]) if frequency == "once" else today
    step = 1
    if frequency == "weekly":
        day += timedelta(days=(timing["weekday"] - (today.weekday() + 1)) % 7)
        step = 7
    try:
        while True:
            local = datetime.combine(day, clock)
            instant = int(local.replace(tzinfo=zone, fold=0).timestamp())
            # A spring-forward gap has no corresponding wall time. An autumn
            # fold has two, and the first is the single occurrence for the date.
            exists = datetime.fromtimestamp(instant, zone).replace(tzinfo=None) == local
            if exists and instant > after:
                return instant
            if frequency == "once":
                return None
            day += timedelta(days=step)
    except (OverflowError, OSError, ValueError) as error:
        raise W.Problem("invalid_schedule_time", "计划日期超出本地时区可用范围") from error


def _db_path():
    return W.home() / "scheduled-tasks.sqlite3"


@contextmanager
def _database(write=False):
    db = sqlite3.connect(_db_path(), timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY, next_due INTEGER, data TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS due_plans ON plans(next_due)")
        db.execute("""CREATE TABLE IF NOT EXISTS runs (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT UNIQUE NOT NULL, plan_id TEXT NOT NULL, trigger_key TEXT NOT NULL,
            state TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(plan_id, trigger_key)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS run_history ON runs(plan_id, seq)")
        db.execute("CREATE INDEX IF NOT EXISTS run_states ON runs(state, seq)")
        if write:
            db.execute("BEGIN IMMEDIATE")
        with db:
            yield db
    finally:
        db.close()


def _plan(row):
    return dict(json.loads(row["data"]), nextRunAt=row["next_due"])


def _get_plan(db, ident):
    row = db.execute("SELECT * FROM plans WHERE id=?", (ident,)).fetchone()
    if not row:
        raise W.Problem("schedule_not_found", "计划不存在", 404)
    return _plan(row)


def _save_plan(db, value):
    payload = json.dumps({k: v for k, v in value.items() if k != "nextRunAt"}, ensure_ascii=False)
    db.execute(
        "INSERT INTO plans VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET next_due=excluded.next_due,data=excluded.data",
        (value["id"], value["nextRunAt"], payload),
    )
    return value


def _plan_view(plan):
    if "_binding" in plan:
        return dict({k: v for k, v in plan.items() if k != "_binding"},
                    conversationId=None, bindingPending=True, nextRunAt=None)
    return plan


def _complete_binding(plan, sv):
    scheduled_execution.materialize(plan, sv)
    with _database(write=True) as db:
        ready = _get_plan(db, plan["id"])
        reserved = ready.pop("_binding", None)
        if reserved is None:
            return ready
        # Keep the reservation until the create receipt is recoverable as well.
        if receipt := reserved["requestId"]:
            operations.finish(W.P.data_root(), receipt, 201, dict(ready, request_id=receipt))
        return _save_plan(db, ready)


def _recover_bindings(sv):
    with _database() as db:
        plans = [_plan(row) for row in db.execute(
            "SELECT * FROM plans WHERE json_extract(data,'$._binding') IS NOT NULL")]
    for plan in plans:
        try:
            _complete_binding(plan, sv)
        except W.Problem as error:
            LOG.warning("Scheduled conversation binding needs attention (%s)", error.code)


def _expected(value, body):
    expected = body.get("expectedRevision")
    if type(expected) is not int or expected != value["revision"]:
        raise W.Problem("schedule_changed", "计划版本已变化，请重新读取", 409)


def _configure(db, body, previous=None):
    if previous is not None:
        _expected(previous, body)
    title, instructions = (body.get(k) for k in ("title", "instructions"))
    if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 100
            or not isinstance(instructions, str) or not 1 <= len(instructions.strip()) <= 20000
            or "\0" in title or "\0" in instructions):
        raise W.Problem("invalid_schedule", "请填写计划标题与任务说明")
    timing = _timing(body.get("timing"))
    enabled = body.get("enabled", previous["enabled"] if previous else True)
    if type(enabled) is not bool:
        raise W.Problem("invalid_schedule", "启用状态必须是布尔值")
    target, authority = scheduled_execution.confirm_plan(body.get("target"), body.get("authority"), body.get("confirm"))
    if previous and previous["target"]["projectId"] != target["projectId"]:
        raise W.Problem("schedule_project_changed", "计划的项目归属固定，请在原项目内选择目录", 409)
    if previous and target.get("checkoutConversationId") and target["checkoutConversationId"] == previous.get("conversationId"):
        raise W.Problem("invalid_schedule_target", "请选择实际持有工作树的会话")
    now = time.time()
    next_run = _next_time(timing, now)
    same_timing = previous and timing == previous["timing"]
    if next_run is None and not same_timing:
        raise W.Problem("invalid_schedule_time", "请选择未来且在指定时区存在的单次时间")
    if same_timing and previous["enabled"] and enabled:
        next_run = previous["nextRunAt"]
    plan = dict(
        previous or {},
        id=previous["id"] if previous else uuid.uuid4().hex,
        revision=previous["revision"] + 1 if previous else 1,
        createdAt=previous["createdAt"] if previous else now, updatedAt=now,
        title=title.strip(), instructions=instructions.strip(), timing=timing, target=target,
        authority=authority, enabled=enabled, nextRunAt=next_run if enabled else None,
    )
    if previous is None:
        plan = scheduled_execution.reserve(plan, body.get("_request_id"))
    return _save_plan(db, plan)


def _set_enabled(db, plan, body):
    _expected(plan, body)
    enabled = body.get("enabled")
    if type(enabled) is not bool:
        raise W.Problem("invalid_schedule", "启用状态必须是布尔值")
    if enabled == plan["enabled"]:
        return plan
    now = time.time()
    next_run = _next_time(plan["timing"], now) if enabled else None
    if enabled and next_run is None:
        raise W.Problem("invalid_schedule_time", "单次计划已过，修改时间后才能启用")
    return _save_plan(db, dict(plan, enabled=enabled, nextRunAt=next_run,
                               revision=plan["revision"] + 1, updatedAt=now))


def _run(db, plan_id, run_id):
    record = db.execute("SELECT data FROM runs WHERE plan_id=? AND id=?", (plan_id, run_id)).fetchone()
    if not record:
        raise W.Problem("schedule_run_not_found", "运行记录不存在", 404)
    return json.loads(record[0])


def _by_trigger(db, plan_id, key):
    record = db.execute("SELECT data FROM runs WHERE plan_id=? AND trigger_key=?", (plan_id, key)).fetchone()
    return json.loads(record[0]) if record else None


def _new_run(db, plan, key, source, *, scheduled_at=None, missed_id=None, missed=False):
    existing = _by_trigger(db, plan["id"], key)
    if existing is not None:
        return existing
    busy = db.execute(f"SELECT id FROM runs WHERE plan_id=? AND state IN {IN_FLIGHT_SQL} LIMIT 1",
                      (plan["id"],)).fetchone()
    now = time.time()
    record = dict(
        id=uuid.uuid4().hex, planId=plan["id"], planRevision=plan["revision"], plan=plan,
        source=source, scheduledAt=scheduled_at, missedRunId=missed_id, triggeredAt=now,
        startedAt=None, endedAt=None, conversationId=None, runId=None, activeRunId=None,
        dispatchReceiptId=None, dispatchState="queued", state="queued", reasonCode=None, reason=None,
        execution=None,
    )
    if missed:
        record.update(state="missed", dispatchState="not_dispatched", endedAt=now,
                      reasonCode="service_unavailable", reason="到点时本地服务未及时运行，等待手动补跑")
    elif busy:
        record.update(state="skipped", dispatchState="not_dispatched", endedAt=now,
                      reasonCode="overlap", reason="同一计划已有在途或待核对的运行",
                      blockingRunId=busy[0])
    db.execute("INSERT INTO runs(id,plan_id,trigger_key,state,data) VALUES(?,?,?,?,?)",
               (record["id"], plan["id"], key, record["state"], json.dumps(record, ensure_ascii=False)))
    return record


def _history(db, plan_id, query):
    cursor = (query.get("cursor") or [""])[0]
    limit = (query.get("limit") or ["20"])[0]
    if not re.fullmatch(r"[0-9]{1,2}", limit) or not 1 <= int(limit) <= 50:
        raise W.Problem("invalid_limit", "运行历史每页允许 1 至 50 条")
    count = int(limit)
    if cursor:
        match = re.fullmatch(r"([a-f0-9]{32}):([0-9]{1,19})", cursor)
        if not match or match[1] != plan_id or not 1 <= int(match[2]) <= 2**63 - 1:
            raise W.Problem("invalid_cursor", "运行历史游标无效或不属于此计划")
        rows = db.execute("SELECT seq,data FROM runs WHERE plan_id=? AND seq<? ORDER BY seq DESC LIMIT ?",
                          (plan_id, int(match[2]), count + 1)).fetchall()
    else:
        rows = db.execute("SELECT seq,data FROM runs WHERE plan_id=? ORDER BY seq DESC LIMIT ?",
                          (plan_id, count + 1)).fetchall()
    return dict(runs=[json.loads(row["data"]) for row in rows[:count]],
                nextCursor=f"{plan_id}:{rows[count - 1]['seq']}" if len(rows) > count else None)


def _human_trigger(db, plan, action, body):
    if action == "run-now":
        request_id = body.get("requestId")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id):
            raise W.Problem("invalid_request_id", "立即运行需要稳定的请求编号")
        key, source = "manual:" + request_id, "manual"
    else:
        request_id = body.get("missedRunId")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise W.Problem("invalid_missed_run", "请选择要补跑的错过记录")
        key, source = "catch_up:" + request_id, "catch_up"
    previous = _by_trigger(db, plan["id"], key)
    if previous is not None:
        return 200, previous
    _expected(plan, body)
    if source == "manual":
        return 202, _new_run(db, plan, key, source)
    latest = db.execute("""SELECT data FROM runs WHERE plan_id=? AND state='missed'
                        ORDER BY json_extract(data,'$.scheduledAt') DESC, seq DESC LIMIT 1""",
                        (plan["id"],)).fetchone()
    original = json.loads(latest[0]) if latest else None
    if not original or original["id"] != request_id:
        raise W.Problem("missed_changed", "只可补跑此计划最近一次错过，请刷新历史", 409)
    return 202, _new_run(db, plan, key, source, scheduled_at=original["scheduledAt"], missed_id=request_id)


def _put_run(db, record, **changes):
    updated = dict(record, **changes)
    db.execute("UPDATE runs SET state=?,data=? WHERE id=?",
               (updated["state"], json.dumps(updated, ensure_ascii=False), updated["id"]))
    return updated


def _rejected(code, reason):
    return dict(state="failed", dispatchState="rejected", endedAt=time.time(), reasonCode=code, reason=reason)


def _unknown(code, reason):
    return dict(state="unknown", reasonCode=code, reason=reason)


def _execution_state(record, sv, handle):
    directory = sv._resolve_sid(record["conversationId"])
    if not directory:
        return _unknown("schedule_conversation_missing", "原运行会话不可用，保留分派回执待核对")
    runtime = RT.status(directory)
    checkpoint = W.read(directory / "session.json")
    run_id = record["activeRunId"]
    current = runtime.get("run_id")
    if current != run_id:
        # A reply can create another native L0 run for the same logical task.
        if current and checkpoint.get("run_id") != current and runtime["active"]:
            return {}
        task = checkpoint.get("taskInput") or {}
        if current and checkpoint.get("run_id") == current and task.get("id") == record["runId"]:
            run_id = current
        else:
            # The history of the exact original run can still settle an old
            # dispatch after a distinct task starts in its conversation.
            from . import progress
            try:
                _, evidence = progress.route(record["conversationId"], [], "GET", {"run": [run_id]}, sv)
            except W.Problem:
                evidence = {}
            observed = (evidence.get("run") or {}).get("outcome") if not evidence.get("evidenceIncomplete") else None
            if observed in ("success", "completed"):
                return dict(state="completed", reasonCode=None, reason=None)
            if observed in ("error", "stuck", "budget"):
                return dict(state="failed", reasonCode="run_failed", reason="原运行的终态记录为失败")
            return _unknown("schedule_run_changed", "会话已进入其他运行，原轮未确认收尾")
    if runtime["active"]:
        if permissions.waiting_for_run(run_id):
            return dict(activeRunId=run_id, state="waiting", reasonCode="approval_required", reason="请在关联会话审批")
        return dict(activeRunId=run_id, state="running", reasonCode=None, reason=None)
    outcome = checkpoint.get("status") if checkpoint.get("run_id") == run_id else None
    if outcome not in ("success", "needs_human", "paused", "error", "stuck", "budget"):
        outcome = runtime.get("outcome")
        if not outcome and handle is not None and RT.confirm_owned_exit(directory, run_id, handle):
            runtime = RT.read(directory)
            outcome = runtime.get("outcome")
    end = runtime.get("finished_at")
    if outcome == "success":
        return dict(activeRunId=run_id, state="completed", endedAt=end, reasonCode=None, reason=None)
    if outcome in ("needs_human", "paused"):
        stopped = bool(record.get("stopRequestedAt"))
        return dict(activeRunId=run_id, state="stopped" if stopped else "waiting",
                    endedAt=end if stopped else None,
                    reasonCode="user_stopped" if stopped else "response_required",
                    reason="用户已停止本次运行" if stopped else "请在关联会话继续")
    if outcome in ("error", "stuck", "budget", "startup_failed", "startup_cancelled"):
        from .evidence_redaction import redact

        why = checkpoint.get("why") if checkpoint.get("run_id") == run_id else None
        reason = redact(why[:1000]) if isinstance(why, str) and why else "关联会话已记录运行失败，请查看原记录"
        return dict(activeRunId=run_id, state="failed", endedAt=end, reasonCode="run_failed",
                    reason=reason)
    return dict(_unknown("schedule_outcome_unknown", "原进程已退出，尚无可核对的运行终态"),
                activeRunId=run_id)


def _request_stop(db, record, body, sv):
    run_id = record["activeRunId"]
    if not run_id or body.get("expectedRunId") != run_id:
        raise W.Problem("schedule_run_changed", "原运行身份未确认或已变化，请刷新后再停止", 409)
    if record["state"] == "stopped":
        return record
    if record["state"] not in ("running", "waiting"):
        raise W.Problem("schedule_run_inactive", "此运行已经结束或仍待核对", 409)
    ident = record["conversationId"]
    directory = sv._resolve_sid(ident)
    runtime = RT.status(directory) if directory else {}
    if runtime.get("run_id") != run_id:
        raise W.Problem("schedule_run_changed", "会话已进入其他运行，请刷新", 409)
    now = time.time()
    if not runtime["active"] and record["state"] == "waiting":
        return _put_run(db, record, state="stopped", endedAt=now, stopRequestedAt=now,
                        reasonCode="user_stopped", reason="用户已结束本次等待")
    if not runtime["active"]:
        raise W.Problem("schedule_run_inactive", "原进程已停止，正在核对运行结果", 409)
    code, response = W.conversation_route("POST", ["conversations", "threads", ident, "stop"], {}, {}, sv)
    if code != 200:
        raise W.Problem(response.get("code", "schedule_stop_failed"), response.get("error", "未能请求停止"), code)
    return _put_run(db, record, stopRequestedAt=now)


class Scheduler:
    def __init__(self, sv):
        self.sv = sv
        self.started_at = time.time()
        self.stopping = threading.Event()
        self.worker = threading.Thread(target=self._serve, name="local-scheduled-tasks", daemon=True)
        self.handles = {}

    def start(self):
        self.worker.start()

    def stop(self):
        self.stopping.set()

    def close(self):
        self.stop()
        self.worker.join(timeout=2)

    def _save(self, original, **changes):
        if not changes or all(original.get(k) == v for k, v in changes.items()):
            return
        with _database(write=True) as db:
            current = _run(db, original["planId"], original["id"])
            if current == original:
                _put_run(db, current, **changes)

    def _observe(self, recover):
        states = ("dispatching", "running", "waiting", "unknown") if recover else ("dispatching", "running", "waiting")
        with _database() as db:
            rows = db.execute("SELECT data FROM runs WHERE state IN (" + ",".join("?" for _ in states) + ")",
                              states).fetchall()
        for (raw,) in rows:
            record = json.loads(raw)
            if record["dispatchState"] in ("sending", "unknown"):
                receipt = operations.get(W.P.data_root(), record["dispatchReceiptId"])
                response = (receipt or {}).get("response") or {}
                if (response.get("scheduledRunId") != record["id"]
                        or response.get("conversationId") != record["conversationId"]):
                    self._save(record, dispatchState="unknown", **_unknown(
                        "schedule_dispatch_unknown", "原分派结果未确认，保留记录待核对"))
                elif receipt["state"] == "completed" and response.get("runId"):
                    self._save(record, dispatchState="accepted", state="running", runId=response["runId"],
                               activeRunId=response["runId"], startedAt=response.get("startedAt"),
                               reasonCode=None, reason=None)
                elif receipt["state"] == "failed":
                    self._save(record, **_rejected(response["code"], response["error"]))
                continue
            handle = self.handles.get(record["id"])
            result = _execution_state(record, self.sv, handle)
            self._save(record, **result)
        # A run can publish its terminal checkpoint before its process exits.
        self.handles = {ident: handle for ident, handle in self.handles.items() if handle.poll() is None}

    def _claim_due(self, now):
        before = max(self.started_at, now - DISPATCH_GRACE_SECONDS)
        with _database(write=True) as db:
            for _ in range(100):
                row = db.execute("""SELECT * FROM plans WHERE next_due<=?
                                 AND json_extract(data,'$._binding') IS NULL ORDER BY next_due,id LIMIT 1""",
                                 (now,)).fetchone()
                if not row:
                    return
                plan = _plan(row)
                due = plan["nextRunAt"]
                key = f"time:{due}"
                try:
                    next_run = _next_time(plan["timing"], due)
                except W.Problem as error:
                    if not _by_trigger(db, plan["id"], key):
                        record = _new_run(db, plan, key, "scheduled", scheduled_at=due, missed=True)
                        failure = dict(_rejected(error.code, error.message), dispatchState="not_dispatched")
                        _put_run(db, record, **failure)
                    next_run = None
                else:
                    _new_run(db, plan, key, "scheduled", scheduled_at=due, missed=due < before)
                _save_plan(db, dict(plan, nextRunAt=next_run))

    def _take(self):
        with _database(write=True) as db:
            row = db.execute("SELECT data FROM runs WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
            if not row:
                return None
            record = json.loads(row[0])
            if record["source"] == "scheduled" and record["scheduledAt"] < self.started_at:
                _put_run(db, record, state="missed", dispatchState="not_dispatched", endedAt=time.time(),
                         reasonCode="service_unavailable", reason="服务重启前到期，原记录尚未分派")
                return None
            return _put_run(db, record, state="dispatching", dispatchState="sending",
                            dispatchReceiptId="schedule_" + record["id"])

    def _dispatch(self, record):
        with _database() as db:
            current = _get_plan(db, record["planId"])
        if any(record["plan"][key] != current[key] for key in ("target", "authority")):
            self._save(record, **_rejected("schedule_authority_changed", "计划授权或目标已修改，请重新触发"))
            return
        try:
            if not current.get("conversationId"):
                with _database(write=True) as db:
                    current = _save_plan(db, scheduled_execution.reserve(current))
            if "_binding" in current:
                current = _complete_binding(current, self.sv)
            ident, route, body, execution = scheduled_execution.prepare(record, current["conversationId"], self.sv)
        except W.Problem as error:
            self._save(record, **_rejected(error.code, error.message))
            return
        self._save(record, conversationId=ident, execution=execution)
        record = dict(record, conversationId=ident, execution=execution)
        request_id = record["dispatchReceiptId"]
        root = W.P.data_root()
        fresh, _ = operations.claim(root, request_id, route, body)
        if not fresh:
            return
        try:
            status, response = W.conversation_route(
                "POST", ["conversations", "threads", ident, "send"], {},
                dict(body, _request_id=request_id), self.sv, scheduled_start=True)
        except W.Problem as error:
            status, response = error.status, dict(code=error.code, error=error.message)
        bound = dict(scheduledRunId=record["id"], conversationId=ident)
        if status >= 400:
            code = response.get("code", "schedule_launch_rejected")
            reason = response.get("error", "默认 L0 发送被拒绝")
            operations.finish(root, request_id, status, dict(bound, code=code, error=reason))
            self._save(record, **_rejected(code, reason))
            return
        directory = self.sv._resolve_sid(ident)
        runtime = RT.read(directory) if directory else {}
        if not runtime.get("run_id") or not response.get("pid") or runtime.get("pid") != response["pid"]:
            operations.finish(root, request_id, 202, bound, state="unknown")
            self._save(record, dispatchState="unknown", **_unknown(
                "schedule_dispatch_unknown", "分派回执与实际运行身份无法核对"))
            return
        run_id = runtime["run_id"]
        started = runtime.get("started_at")
        operations.finish(root, request_id, 200, dict(bound, runId=run_id, startedAt=started))
        handle = self.sv.PROCS.get(ident)
        if handle is not None:
            self.handles[record["id"]] = handle
        self._save(record, state="running", dispatchState="accepted", runId=run_id, activeRunId=run_id,
                   startedAt=started, reasonCode=None, reason=None)

    def _serve(self):
        owner = FileLock(W.home() / "scheduled-tasks-owner.lock", timeout=0)
        if not owner.acquire():
            return
        try:
            recover = True
            while not self.stopping.is_set():
                if _db_path().is_file():
                    try:
                        if recover:
                            with W._lock:
                                _recover_bindings(self.sv)
                        self._observe(recover)
                        recover = False
                        self._claim_due(time.time())
                        with W._lock:
                            if not self.stopping.is_set():
                                record = self._take()
                                if record:
                                    self._dispatch(record)
                    except (W.Problem, sqlite3.Error, OSError, ValueError, TimeoutError) as error:
                        LOG.warning("Local scheduled tasks deferred (%s)", type(error).__name__)
                self.stopping.wait(POLL_SECONDS)
        finally:
            owner.release()


def route(method, parts, query, body, sv):
    try:
        if method == "POST" and not parts:
            with _database(write=True) as db:
                reservation = _configure(db, body)
            return 201, _complete_binding(reservation, sv)
        with _database(method == "POST") as db:
            if not parts:
                return 200, dict(plans=[_plan_view(_plan(row)) for row in db.execute("SELECT * FROM plans ORDER BY rowid DESC")])
            plan = _get_plan(db, parts[0])
            if method == "POST" and "_binding" in plan:
                raise W.Problem("schedule_binding_pending", "定时会话正在恢复，请刷新后再试", 409)
            tail = parts[1:]
            if not tail:
                return (200, _plan_view(plan)) if method == "GET" else (200, _configure(db, body, plan))
            if method == "POST" and tail == ["enabled"]:
                return 200, _set_enabled(db, plan, body)
            if method == "POST" and tail in (["run-now"], ["catch-up"]):
                return _human_trigger(db, plan, tail[0], body)
            if method == "GET" and tail == ["runs"]:
                return 200, _history(db, plan["id"], query)
            if method == "GET" and len(tail) == 2 and tail[0] == "runs":
                return 200, _run(db, plan["id"], tail[1])
            if method == "POST" and len(tail) == 3 and tail[0] == "runs" and tail[2] == "stop":
                return 200, _request_stop(db, _run(db, plan["id"], tail[1]), body, sv)
    except sqlite3.Error as error:
        raise W.Problem("schedule_storage_unavailable", "定时任务存储暂不可用，原记录已保留", 503) from error
    raise W.Problem("not_found", "没有这个定时任务接口", 404)
