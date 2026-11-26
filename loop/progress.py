"""Structured stage facts share the append-only evidence projection."""

# 从原始事件归集可回查的进展事实；这里不调度角色、不重跑工具，也不根据模型正文判定验收。
# 主要引用方包括 `evidence.py` 和 `workbench.py`。
import json
import re
from collections import Counter
from . import workbench as W

ROLES = ("planner", "executor", "reviewer")
ROLE_NAMES = {"planner": "规划者", "executor": "执行者", "reviewer": "检查者"}
ROLE_ALIASES = dict(
    zip(ROLES, ROLES),
    **{
        "规划者": "planner",
        "执行者": "executor",
        "检查者": "reviewer",
        "测试者": "reviewer",
    },
)
STATES = {
    "not_run",
    "running",
    "completed",
    "failed",
    "skipped",
    "paused",
    "timed_out",
    "waiting_human",
    "unknown",
}


def create(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS progress_runs (id TEXT PRIMARY KEY,position INTEGER,data TEXT)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS progress_position ON progress_runs(position)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS progress_summaries (run TEXT PRIMARY KEY,coverage INTEGER)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS progress_attempts (run TEXT,role TEXT,id TEXT,source TEXT,PRIMARY KEY(run,role,id))"
    )


# `evidence.projection` 传入脱敏后的 JSONL 事件。
# `position` 是原日志中的记录序号，`source_ref` 保留原日志字节位置、长度和哈希。
def observe(db, row, position, source_ref):
    ident = row.get("run_id")
    if not isinstance(ident, str) or not 1 <= len(ident) <= 128:
        return
    found = db.execute("SELECT data FROM progress_runs WHERE id=?", (ident,)).fetchone()
    run = (
        json.loads(found[0])
        if found
        else dict(
            id=ident,
            firstPosition=position,
            goal="",
            crew=None,
            # 缺少角色事实时从 unknown 开始；未运行需有明确配置或事件，不能按默认档位猜历史行为。
            roles={
                r: dict(
                    state="unknown",
                    reason="未记录本角色的运行状态",
                    lastSeq=0,
                    attempts=0,
                    source=None,
                )
                for r in ROLES
            },
            judgments=[],
            blocker=None,
            outcome=None,
            modelCalls=0,
        )
    )
    # 有序更新只认正整数序号（排除 bool）；无效序号归零，不能覆盖已有的角色/字段事实。
    seq = row.get("seq")
    seq = seq if type(seq) is int and 0 < seq < 2**63 else 0
    source = dict(
        seq=seq,
        position=position,
        kind=str(row.get("act", ""))[:80],
        runId=ident,
        record=source_ref,
    )

    # 普通字段取最新序号，轮次目标取最早有效用户消息；连同来源保存，供后续回查。
    def assign(field, value, earliest=False):
        prior = run.get(field + "Source", {}).get("seq", 0)
        if not seq or (prior and (seq >= prior if earliest else seq <= prior)):
            return False
        run[field] = value
        run[field + "Source"] = source
        return True

    # 同角色只接受更大的序号；执行者重新 running 时清掉旧 outcome，避免恢复后还显示上次终态。
    def phase(role, state, reason=None):
        if role not in ROLES or seq <= run["roles"][role]["lastSeq"]:
            return False
        prior = run["roles"][role]
        if state not in STATES:
            state = "unknown"
        run["roles"][role] = dict(
            state=state,
            reason=str(reason or "")[:1000],
            lastSeq=seq,
            attempts=prior["attempts"],
            source=dict(source, role=role, state=state),
        )
        if state == "skipped" and row.get("skipKind") == "no_checkable_work":
            run["roles"][role]["skipKind"] = "no_checkable_work"
        if role == "executor" and state == "running":
            assign("outcome", None)
        return True

    kind = row.get("act")
    # 候选复核单元的局部事件不代表整轮角色状态，避免局部复核覆盖主流程。
    if row.get("reviewUnitId") and kind in {"role_phase", "benni", "review_candidate"}:
        return
    role = ROLE_ALIASES.get(str(row.get("role")))
    if (kind == "role_phase" and row.get("state") == "running" and role) or (
        kind == "benni" and row.get("running")
    ):
        # 开始事实按 attempt_id 去重；旧记录缺少身份时用兼容键，最终次数从表内计数。
        start_role = role if kind == "role_phase" else "reviewer"
        attempt = row.get("attempt_id")
        if not isinstance(attempt, str) or not 1 <= len(attempt) <= 128:
            attempt = "legacy-review" if kind == "benni" else "start-" + str(seq)
        db.execute(
            "INSERT OR IGNORE INTO progress_attempts VALUES(?,?,?,?)",
            (ident, start_role, attempt, json.dumps(source)),
        )

    if kind == "run_config":
        crew = row.get("crew")
        requested = row.get("requestedRoles")
        if assign(
            "crew", crew if crew in {"l0", "l1", "l2", "l3", "l4"} else None
        ) and isinstance(requested, dict):
            run["requestedRoles"] = {
                r: requested.get(r) if type(requested.get(r)) is bool else None
                for r in ROLES
            }
            # 只有显式 False 且角色仍 unknown 时补未运行；已有实际角色事实优先于配置推断。
            for r in ROLES:
                if requested.get(r) is False and run["roles"][r]["state"] == "unknown":
                    phase(r, "not_run", "本轮配置未请求此角色")
    if kind == "user":
        text = str(row.get("text") or "")
        if text and assign("goal", text.splitlines()[0][:512], earliest=True):
            run["goalExcerpt"] = len(run["goal"]) < len(text)
            run["started"] = row.get("at") if isinstance(row.get("at"), (int, float)) else None
    if kind == "role_coordination" and row.get("parallelRoles") is True:
        assign("parallelRoles", True)
    if kind == "review_candidate" and isinstance(row.get("candidateId"), str):
        latest = run.get("latestCandidate") or {}
        if row.get("state") == "queued" or latest.get("id") == row["candidateId"]:
            assign("latestCandidate", dict(id=row["candidateId"], state=row.get("state"), verdict=row.get("verdict")))
    # 重连反映请求可用性：耗尽记 paused，其余阶段记 running；结束结论仍等待独立事实。
    if kind == 'provider_recovery' and role:
        labels = dict(waiting='正在重连上游', recovered='连接已恢复，继续任务',
                      exhausted='上游暂不可用，进度已保存', cancelled='已取消重连', failed='模型请求失败')
        phase(role, 'paused' if row.get('state') == 'exhausted' else 'running', labels.get(row.get('state'), '连接中'))
    if kind == "role_phase":
        state = row.get("state")
        if (state == "running" and role and seq > run["roles"][role]["lastSeq"]):
            run.setdefault("reviewNotes", {}).pop(role, None)
            run.setdefault("reviewAttempts", {})[role] = row.get("attempt_id")
        phase(
            role,
            state if isinstance(state, str) else "unknown",
            row.get("reason") or row.get("error"),
        )
    elif kind == "benni":
        if row.get("warn"):
            phase("reviewer", "failed", row["warn"])
        elif row.get("timedOut"):
            phase("reviewer", "timed_out", row.get("reason"))
        elif row.get("paused"):
            phase("reviewer", "paused", row.get("reason"))
        elif row.get("running"):
            phase("reviewer", "running", row.get("reason"))
        elif row.get("skipped") and row.get("skipKind") != "role_not_requested":
            if phase("reviewer", "skipped", row.get("reason")):
                attempt = row.get("attempt_id")
                run["roles"]["reviewer"]["skippedAttempt"] = (
                    attempt
                    if isinstance(attempt, str) and 1 <= len(attempt) <= 128
                    else "legacy-review"
                )
        # completed 仅说明复核意见已生成；发现缺陷或证据不足也可完成复核，不等于验收通过。
        elif row.get("ok") is True:
            reason = {"issues": "已提交复核意见；发现待修缺陷",
                      "insufficient": "已提交复核意见；证据不足",
                      "pass": "已提交复核意见；候选检查通过"}.get(row.get("reviewVerdict"))
            phase("reviewer", "completed", reason or "复核意见已生成；不是自动检查通过")
        elif row.get("ok") is False and not row.get("skipped") and row.get("reason"):
            if run["roles"]["reviewer"]["state"] in {"unknown", "running"}:
                phase("reviewer", "failed", row["reason"])
    elif kind in {"action", "llm"}:
        action_role = role if role in ROLES else "executor"
        if run["roles"][action_role]["state"] in {"not_run", "unknown"}:
            phase(action_role, "running", "已记录到执行行为，启动事实可能缺失")
        if kind == "llm":
            run["modelCalls"] += 1
    elif kind == "run_outcome":
        assign("outcome", str(row.get("status") or "unknown")[:128])
    # end 记录整轮 outcome；已有角色结算或并行模式时保留独立状态，复核超时不被 paused 覆写。
    elif kind == "end":
        status = row.get("status")
        if assign("outcome", str(status or "unknown")[:128]):
            if not row.get("settledRoles") and not run.get("parallelRoles") and not (
                    role == "reviewer" and status == "paused" and run["roles"][role]["state"] == "timed_out"):
                phase(
                    role if role in ROLES else "executor",
                    {
                        "success": "completed",
                        "paused": "paused",
                        "needs_human": "waiting_human",
                    }.get(status, "failed"),
                    "执行循环结束；完成要求仍需检查和用户验收",
                )
            if status == "success":
                assign("blocker", None)
    # 阻塞必须晚于当前 outcome，迟到的旧问句不能把已结束轮次重新挂起。
    elif kind in {"needs_human", "ask_human", "stuck", "tool_cancelling"}:
        if seq > run.get("outcomeSource", {}).get("seq", 0):
            assign(
                "blocker",
                str(row.get("why") or row.get("question") or row.get("error") or kind)[
                    :1000
                ],
            )
            if kind in {"needs_human", "ask_human"}:
                phase(role if role in ROLES else "executor", "waiting_human", run["blocker"])
    if kind == "say":
        if (role in {None, "executor"} and not row.get("toolPrelude") and row.get("roleSummary") is not False and row.get("text")):
            assign("navigationResult", str(row["text"])[:320])
        # 笔记需匹配当前 attempt 且不早于角色最后事实，防止旧尝试的迟到总结覆盖新尝试。
        if role in ROLES and row.get("roleSummary") and row.get("reviewNotes"):
            from .review_notes import normalize

            notes = run.setdefault("reviewNotes", {})
            attempt = run.get("reviewAttempts", {}).get(role)
            if (seq > notes.get(role, {}).get("source", {}).get("seq", 0)
                    and seq >= run["roles"][role]["lastSeq"]
                    and (not attempt or attempt == row.get("attempt_id"))):
                notes[role] = dict(items=normalize(row["reviewNotes"]), source=source,
                                   attempt=row.get("attempt_id"))
        judgment_role = role or "unknown"
        run["judgments"] = sorted(
            run["judgments"]
            + [
                dict(
                    role=judgment_role,
                    text=str(row.get("text") or "")[:1000],
                    source=source,
                )
            ],
            key=lambda j: (j["source"]["seq"], j["source"]["position"]),
        )[-3:]
    # 单独记录循环终止判据的机械结果和来源，保留它与角色自述、任务验收的区别。
    if kind == "verify":
        assign(
            "terminationCheck",
            dict(
                ok=row.get("ok") if type(row.get("ok")) is bool else None,
                reason=str(row.get("reason") or "")[:1000],
                source=source,
            ),
        )
    for r in ROLES:
        run["roles"][r]["attempts"] = db.execute(
            "SELECT COUNT(*) FROM progress_attempts WHERE run=? AND role=?", (ident, r)
        ).fetchone()[0]
    reviewer = run["roles"]["reviewer"]
    # 已记录该 attempt 启动时，后来的 skipped 按 failed 呈现；未启动的跳过才保留 skipped。
    if reviewer.get("skippedAttempt"):
        attempted = db.execute(
            "SELECT 1 FROM progress_attempts WHERE run=? AND role='reviewer' AND id=?",
            (ident, reviewer["skippedAttempt"]),
        ).fetchone()
        reviewer["state"] = "failed" if attempted else "skipped"
        reviewer["source"]["state"] = reviewer["state"]
    run["throughPosition"] = max(position, run.get("throughPosition", 0))
    db.execute(
        "INSERT OR REPLACE INTO progress_runs VALUES(?,?,?)",
        (ident, run["firstPosition"], json.dumps(run, ensure_ascii=False)),
    )


# 核对进展引用仍对应原始日志；超过本次核对上限或来源未确认时，将统计标为暂定。
def verify_run(root, run, db, evidence):
    sources = [
        run.get(key)
        for key in (
            "crewSource",
            "goalSource",
            "outcomeSource",
            "blockerSource",
            "terminationCheckSource",
            "parallelRolesSource",
            "latestCandidateSource",
        )
    ]
    sources += [r.get("source") for r in run["roles"].values()]
    sources += [j.get("source") for j in run["judgments"]]
    attempt_rows = db.execute(
        "SELECT source FROM progress_attempts WHERE run=? LIMIT 65", (run["id"],)
    ).fetchall()
    sources += [json.loads(row[0]) for row in attempt_rows[:64]]
    refs = {
        s["record"]["offset"]: s["record"] for s in sources if s and s.get("record")
    }
    evidence._verify_source(root, {"sourceRefs": list(refs.values())}, db)
    action_rows = db.execute(
        "SELECT data FROM actions WHERE run=? LIMIT 51", (run["id"],)
    ).fetchall()
    provisional = len(action_rows) > 50 or len(attempt_rows) > 64
    for row in action_rows[:50]:
        value = json.loads(row[0])
        if value.get("sourceUnverified"):
            provisional = True
        else:
            evidence._verify_source(root, value, db)
    return provisional


# 仅 success/completed 轮次提供结果摘录；活跃状态须由同 run 的宿主记录确认，锚点保留原始日志位置。
def navigation(run, runtime):
    outcome = run.get("outcome")
    result = run.get("navigationResult") if outcome in {"success", "completed"} else None
    source = (run.get("navigationResultSource") if result else run.get("blockerSource") if run.get("blocker") else None) or run.get("goalSource")
    active = runtime.get("run_id") == run["id"] and runtime.get("active")
    record = (source or {}).get("record") or {}
    return dict(id=run["id"], position=run["firstPosition"], started=run.get("started"), goal=run["goal"],
                result=result, blocker=run.get("blocker"), outcome=runtime.get("state") if active else outcome,
                active=bool(active), offset=record.get("offset"), before=record["offset"] + record["bytes"] if record else None)


# 历史分页按轮次首次出现的物理位置定位，游标不依赖可能重复的事件序号。
def navigation_page(root, db, state, runtime, q, evidence):
    requested = (q.get("run") or [None])[0]
    try:
        before = int((q.get("before") or [str(2**63 - 1)])[0])
        if not 0 < before < 2**63:
            raise ValueError()
    except ValueError as error:
        raise W.Problem("invalid_cursor", "历史轮次位置无效") from error
    if requested:
        rows = db.execute("SELECT data FROM progress_runs WHERE id=?", (requested,)).fetchall()
        if not rows:
            raise W.Problem("run_missing", "此轮原记录未保留或仍在索引中", 425 if state["catchingUp"] else 404)
    else:
        rows = db.execute("SELECT data FROM progress_runs WHERE position<? AND json_extract(data,'$.goal')<>'' ORDER BY position DESC LIMIT 31", (before,)).fetchall()
    runs = [json.loads(row[0]) for row in rows[:30]]
    references = [run[key]["record"] for run in runs for key in ("goalSource", "navigationResultSource", "blockerSource", "outcomeSource")
                  if (run.get(key) or {}).get("record")]
    if references:
        evidence._verify_source(root, {"sourceRefs": references}, db)
    return 200, dict(rounds=[navigation(run, runtime) for run in runs],
                     nextBefore=runs[-1]["firstPosition"] if len(rows)>30 else None,
                     catchingUp=state["catchingUp"])


# `code_checks` 来自 `code_service.executor_verification`。
# 分别统计交付与探索检查，再单独显示退出码为零但目标断言失败或未报告的情形。
def _completion_check_text(code_checks):
    labels = {
        "passed": "通过", "failed": "失败", "blocked": "受阻", "running": "运行中",
        "stale": "待复验", "unknown": "待核对",
    }
    delivery, exploration = Counter(), Counter()
    completed_with_assertion_failure = unreported = repaired = 0
    uncertain = code_checks.get("status") == "unknown"
    for resource in code_checks.get("resources", []):
        uncertain |= resource.get("status") == "unknown"
        repaired += len(resource.get('assertionRepairs', []))
        seen = set()
        for row in resource.get("checks", []):
            seen.add(row.get("operationId"))
            if row.get("replaced"):
                continue
            state = row.get("status")
            if state == "failed" and row.get("environmentBlocker"):
                state = "blocked"
            counts = exploration if row.get("purpose") == "exploration" else delivery
            counts[state if state in labels else "unknown"] += 1
            if counts is delivery and row.get('exitCode')==0:
                if state=='failed' and row.get('assertionFailures'):
                    completed_with_assertion_failure += 1
                elif state=='passed' and row.get('testReportState') in {'not_reported','no_tests','all_skipped'}:
                    unreported += 1
        for gap in resource.get("pending", []):
            if gap.get("operationId") not in seen:
                state = gap.get("status")
                delivery[state if state in labels else "unknown"] += 1
    parts = [
        title + " · " + "、".join(f"{counts[state]} 项{label}" for state, label in labels.items() if counts[state])
        for title, counts in (("交付检查", delivery), ("探索检查", exploration)) if counts
    ]
    if completed_with_assertion_failure:
        parts.append(f'{completed_with_assertion_failure} 项命令退出 0，断言失败')
    if unreported:
        parts.append(f'{unreported} 项命令退出 0，缺少目标断言结果')
    if repaired:
        parts.append(f'{repaired} 项断言已验证修复前后结果')
    if uncertain:
        parts.append("检查回执待核对")
    if delivery["running"] or exploration["running"]:
        parts.insert(0, "收尾时的记录")
    return "。".join(parts) or "尚无本轮检查回执"


def _completion_acceptance_text(verify, contract_configured):
    if not verify:
        return "尚无本轮验收记录"
    gate = verify.get("acceptance") or {}
    required = {row.get("status") for row in gate.get("requirements", [])}
    contract = verify.get("contract")
    has_contract_checks = (contract or {}).get("total", 0) > 0
    contract_ok = verify.get("verifierOk", verify.get("ok"))
    if "failed" in required or (has_contract_checks and contract_ok is False):
        return "验收未通过"
    if verify.get("blocked"):
        return "验收受阻"
    if (gate.get("ok") is False or required - {"passed", "confirmed"}
            or (contract is not None and contract_ok is not True)
            or (contract_configured and contract is None)):
        return "验收证据不足"
    if required or has_contract_checks:
        return "已配置的验收条件通过"
    return "未配置运行内验收"


def _completion_facts(completion, session):
    """Render saved facts for one run without changing its outcome or obligations."""
    run = {
        "completed": "本轮已结束", "running": "本轮进行中", "paused": "本轮已暂停",
        "failed": "本轮运行失败", "waiting_user": "本轮等待用户", "waiting": "本轮等待结果",
    }.get(completion["status"], "运行状态待核对")
    if completion["status"] == "paused":
        run = {
            "budget": "本轮额度已用尽", "stuck": "本轮执行停滞", "interrupted": "本轮已中断",
        }.get(session.get("status"), run)
    verify = session.get("verify") or {}
    return dict(
        run=run,
        checks=_completion_check_text(verify.get("codeChecks") or {}),
        acceptance=_completion_acceptance_text(verify, bool(session.get("contract"))),
    )


def route(ident, parts, method, q, sv):
    if method != "GET" or parts not in ([], (), ["rounds"], ("rounds",)):
        raise W.Problem("not_found", "进展只读入口不存在", 404)
    from . import evidence

    root = evidence.directory(ident, sv)
    runtime = W.RT.status(root)
    with evidence.projection(root) as (db, state):
        if parts:
            return navigation_page(root, db, state, runtime, q, evidence)
        requested = (q.get("run") or [None])[0]
        # 未指定轮次时优先查询宿主当前 run；仅无运行身份时回退旧记录，避免用上一轮冒充本轮。
        current = requested or runtime.get("run_id")
        found = (
            db.execute(
                "SELECT data FROM progress_runs WHERE id=?", (current,)
            ).fetchone()
            if current
            else None
        )
        if found is None and not requested and not runtime.get("run_id"):
            found = db.execute(
                "SELECT data FROM progress_runs ORDER BY position DESC LIMIT 1"
            ).fetchone()
        if requested and found is None:
            raise W.Problem(
                "run_missing",
                "此轮进展未记录或仍在索引中",
                425 if state["catchingUp"] else 404,
            )
        run = json.loads(found[0]) if found else None
        current_missing = bool(current and run is None)
        incomplete = bool(
            state["catchingUp"]
            or state["gapCount"]
            or state["malformedCount"]
            or state["sequenceConflicts"]
        )
        stale = False
        summary = None
        missing = True
        if run:
            provisional = verify_run(root, run, db, evidence)
            incomplete = incomplete or provisional
            run["countsProvisional"] = provisional
            # 历史 running 须由同 run、活跃且状态已知的所有者确认；否则响应降为 unknown 并保留旧观察。
            owner_confirms = (
                runtime.get("run_id") == run["id"]
                and runtime.get("active")
                and runtime.get("state") != "unknown"
            )
            for role in ROLES:
                phase = run["roles"][role]
                if phase["state"] == "running" and not owner_confirms:
                    phase.update(
                        state="unknown",
                        lastObservedState="running",
                        reason="最后记录为处理中，但当前没有可确认的运行所有者或结束事实",
                    )
            counts = db.execute(
                "SELECT COUNT(*),SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END),SUM(CASE WHEN state='unknown' THEN 1 ELSE 0 END) FROM actions WHERE run=?",
                (run["id"],),
            ).fetchone()
            run.update(
                actionCount=counts[0],
                failedActions=counts[1] or 0,
                unknownActions=counts[2] or 0,
            )
            old = db.execute(
                "SELECT coverage FROM progress_summaries WHERE run=?", (run["id"],)
            ).fetchone()
            stale = bool(old and old[0] != state["records"])
            native = run.get('crew') == 'l0'
            missing = bool(
                incomplete
                or run["failedActions"]
                or run["unknownActions"]
                or (not native and run["roles"]["reviewer"]["state"] != "completed"
                    and not (run["roles"]["reviewer"]["state"] == "skipped"
                             and run["roles"]["reviewer"].get("skipKind") == "no_checkable_work"))
            )
            labels = {
                "not_run": "未运行",
                "running": "处理中",
                "completed": "已结束",
                "failed": "失败",
                "skipped": "已跳过",
                "paused": "已暂停",
                "timed_out": "复核超时",
                "waiting_human": "等待用户",
                "unknown": "结果未知",
            }
            try:
                text = "；".join(
                    ROLE_NAMES[role] + "：" + labels[run["roles"][role]["state"]]
                    for role in (('executor',) if native else ROLES)
                )
                text += "。记录到 %d 条工具动作，其中 %d 次失败。" % (
                    run["actionCount"],
                    run["failedActions"],
                )
                if missing:
                    text += "仍有未完成或未核实的检查。"
                elif not native:
                    text += ("本轮无需独立检查；是否满足需求仍待验收。"
                             if run["roles"]["reviewer"].get("skipKind") == "no_checkable_work"
                             else "已生成复核意见；是否满足需求仍待验收。")
                summary = dict(kind="structured-facts", text=text)
            except (KeyError, TypeError):
                summary = dict(
                    kind="structured-facts",
                    text="摘要暂不可用，请核对下面的角色事实与工具证据。",
                    fallback=True,
                )
            db.execute(
                "INSERT OR REPLACE INTO progress_summaries VALUES(?,?)",
                (run["id"], state["records"]),
            )
            db.commit()
        # 投影生成期间日志仍可能追加；比较当前大小与已索引字节，保留尚未归集尾部的不完整提示。
        latest_size = (
            (root / "events.jsonl").stat().st_size
            if (root / "events.jsonl").exists()
            else 0
        )
        # 完成视图只接收与展示 run 绑定的已保存记录，再补后台检查状态，避免沿用别轮完成结论。
        completion = None
        native_session = None
        if run:
            session = W.read(root / 'session.json')
            native = session.get('executorCompletion', {})
            if native.get('runId') == run['id']:
                completion = native.get('completion')
                if session.get('run_id') == run['id']:
                    native_session = session
        if run and run.get('crew') in {'l1', 'l2', 'l4'} and re.fullmatch(r"[a-f0-9]{32}", run["id"]):
            saved = W.read(root / "role-runs" / (run["id"] + ".json"))
            if saved.get("runId") == run["id"] and saved.get("executorLed"):
                completion = saved.get("completion")
                native_session = None
        if completion:
            from .check_sessions import completion_view
            completion = completion_view(ident, run['id'], completion)
            # 运行已停止而完成视图仍 running/waiting 时降为 paused/unknown，保留待办，不凭停止判完成。
            if (runtime.get("run_id") == run["id"] and not runtime.get("active")
                    and completion["status"] in {"running", "waiting"}):
                uncertain = runtime.get("state") == "unknown"
                completion = dict(completion, status="unknown" if uncertain else "paused",
                    label="运行状态待核对" if uncertain else "运行已停止 · 尚有事项未完成",
                    pending=[*completion["pending"], dict(kind="execution", id="runtime", role="executor", state="unknown",
                        text="本轮尚无正常收尾确认，请核对已保存的检查与待办")])
            if native_session is not None:
                completion = dict(completion, facts=_completion_facts(completion, native_session))
            missing = any(item.get("kind") in {"check", "requirement", "review", "review_requirement", "defect", "review_unit"}
                          for item in completion["pending"])
            summary = dict(kind="structured-facts", text=completion["label"] + "。本轮记录到 %d 个工具动作，含 %d 次历史失败。" % (
                run["actionCount"], run["failedActions"]))
        return 200, dict(
            run=run,
            runtime=runtime,
            summary=summary,
            checksMissing=missing,
            completion=completion,
            currentRunPending=bool(
                current_missing and (runtime.get("active") or state["catchingUp"])
            ),
            currentRunUnknown=bool(
                current_missing
                and not runtime.get("active")
                and not state["catchingUp"]
            ),
            evidenceIncomplete=incomplete or latest_size > state["offset"],
            previousSummaryStale=stale,
            evidenceRange=dict(
                fromPosition=run["firstPosition"] if run else 0,
                throughPosition=state["records"],
                sourceBytes=state["offset"],
                currentSourceBytes=latest_size,
                maxSeq=state["maxSeq"],
            ),
            isDelegated=bool(
                run
                and (
                    run["actionCount"]
                    or any(
                        run.get("requestedRoles", {}).get(r)
                        for r in ("planner", "reviewer")
                    )
                )
            ),
        )
