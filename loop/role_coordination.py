"""Coordinate one opt-in role run. Only the owning runner settles the conversation."""

# 主要引用方包括 `workbench.py` 和 `assistant_patches.py`。

from concurrent.futures import ThreadPoolExecutor
from copy import copy
import hashlib
import json
from pathlib import Path
import re
import stat
import tempfile
import threading
import time

from . import change_history as H, code_service as C, control, evidence
from . import leaks, projects, review_units, scoped_roles, workbench as W
from .filelock import FileLock, atomic_json
from .policy import Policy
from .provider import Interrupted
from .tools import Registry, Tool, current_call, current_stop, reset_call, set_call, set_stop
from .work_queue import QueueConflict, WorkQueue


WRITES = {"edit_project_file", "write_project_file", "restore_project_file", "undo_own_project_write"}
TERMINAL_PROCESS = {"completed", "failed", "interrupted", "unknown"}
ABSENT = dict(kind="absent", hash=None)
HANDOFF_PREPARATION_SECONDS = 90
HANDOFF_REPAIR_SECONDS = 40
REVIEW_CONCLUSION_SECONDS = 25
CHECKPOINT_REVIEW_SECONDS = 90


def _excluded(relative):
    from .safe_executor import EXCLUDED, SECRET_NAMES

    return any(part in EXCLUDED or part in SECRET_NAMES or part.startswith(".env")
               or part.endswith((".pem", ".key", ".p12", ".pfx"))
               for part in (p.casefold() for p in Path(relative).parts))


def _review_scope(row):
    return row["resourceId"], row.get("sourcePath"), row.get("unitId")


# 按声明的验收项 ID 和修订核对复核覆盖，避免把旧要求的检查当成当前证据。
def _coverage_matches(row, coverage):
    criteria = row.get("criteria", [])
    if not criteria:
        return True
    if any(not isinstance(item, dict) for item in criteria):
        return False
    return {(item["id"], item["revision"]) for item in criteria} == {
        (item.get("requirementId"), item.get("requirementRevision"))
        for item in (coverage or []) if item.get("status") == "covered" and item.get("actionIds")
    }


# 恢复只接受同一执行者主导模式及原后台角色开关。
def compatible_role_mode(saved, *, planner, reviewer):
    return bool(saved and saved.get("executorLed") and (planner or reviewer)
                and bool(saved.get("backgroundPlanner")) == planner
                and bool(saved.get("backgroundReviewer", True)) == reviewer)


class RoleCoordinator:
    # 由运行入口传入角色配置、授权与会话，持有正式队列、已发布候选和复核预算。
    def __init__(self, session, root, task, *, stop, previous_run=None, executor_led=False,
                 background_planner=False, background_reviewer=True):
        self.session, self.root, self.task, self.stop = session, Path(root), task, stop
        self.serial_review = False
        self.runtime_completion = executor_led
        self.executor_led = executor_led
        self.background_reviewer = background_reviewer
        self.investigation = None
        if background_planner:
            from .background_planning import BackgroundPlanning
            self.investigation = BackgroundPlanning(self, _excluded)
        self.task_input = getattr(session, "task_input", {})
        self.review_closed = threading.Event()
        self.review_deadline = control.benchmark_deadline(session.dir, "reviewer")
        self.work_deadline = control.benchmark_deadline(session.dir, "executor")
        self.handoff_deadline = None
        self.handoff_check_lead = 0
        if self.review_deadline is not None and self.work_deadline is not None:
            work_left = max(0, self.work_deadline - time.monotonic())
            preparation = min(HANDOFF_PREPARATION_SECONDS, work_left * .2)
            self.handoff_check_lead = min(35, work_left * .15)
            repair_window = min(HANDOFF_PREPARATION_SECONDS + HANDOFF_REPAIR_SECONDS, work_left * .3)
            self.handoff_deadline = min(self.work_deadline - repair_window, max(
                self.work_deadline - work_left * .3,
                self.review_deadline - scoped_roles.review_seconds() - preparation,
            ))
        self.path = Path(session.dir) / "role-runs" / (session.run_id + ".json")
        self.lock = threading.RLock()
        self.queue = ThreadPoolExecutor(max_workers=1, thread_name_prefix="candidate-review")
        self.worker = None
        self.unit_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="acceptance-review")
        self.unit_futures = {}
        self.source_cache = {}
        self.source_leases = {}
        self.source_cache_lock = threading.Lock()
        self.materialization_cache = {}
        self.check_slot = threading.Semaphore(1)
        self.planning = ThreadPoolExecutor(max_workers=1, thread_name_prefix="work-order")
        self.planning_active = False
        self.planning_closed = threading.Event()
        self.state = dict(runId=session.run_id, baselineRun=session.run_id,
                          state="running", candidates={}, plans=[], dirty={}, reviewUnits={})
        self.state["effectiveRequirements"] = getattr(session, "effective_requirements", None)
        if executor_led:
            self.state['completionDeclarationRequired'] = False
            self.state['serialReadonlyReview'] = self.serial_review
            self.state['runtimeCompletion'] = self.runtime_completion
            self.state.update(executorLed=True, backgroundPlanner=background_planner,
                              backgroundReviewer=background_reviewer, **self._task_binding())
        restored_reports = []
        if previous_run and re.fullmatch(r"[a-f0-9]{32}", previous_run):
            old_path = self.path.with_name(previous_run + ".json")
            if old_path.is_file():
                old = json.loads(old_path.read_text(encoding="utf8"))
                if executor_led and (not compatible_role_mode(old, planner=background_planner, reviewer=background_reviewer)
                        or old.get("logicalTaskId") != self.task_input["id"]
                        or old.get("demandRevision") != self.task_input["revision"]):
                    if (compatible_role_mode(old, planner=background_planner, reviewer=background_reviewer)
                            and old.get("logicalTaskId") == self.task_input["id"]):
                        self.state["baselineRun"] = old.get("baselineRun", previous_run)
                        self.state["reviewMaterials"] = old.get("reviewMaterials", {})
                        self.state["historicalPreparation"] = old.get("reviewPreparation", {}) or old.get("historicalPreparation", {})
                    old = {}
            else:
                old = {}
            if old:
                self.state.update(candidates=old["candidates"], plans=old["plans"], dirty=old["dirty"], resumedFrom=previous_run)
                self.state["baselineRun"] = old.get("baselineRun", previous_run)
                self.state["reviewUnits"] = {
                    json.dumps(_review_scope(self.state["candidates"][key])): key
                    for key in old.get("reviewUnits", {}).values()
                }
                if "workQueue" in old:
                    self.state["workQueue"] = old["workQueue"]
                for field in ("blueprint", "troubleshooting", "changeLedger", "environmentChecks", "reviewMaterials", "preparedSources", "preparationBlocks", "reviewPreparation", "historicalPreparation", "executionDeclaration", "executorChecks", "executorAcceptance", "assistantRepairs"):
                    if field in old:
                        self.state[field] = old[field]
                if old.get("deliveryReady"):
                    self.state["deliveryReady"] = old["deliveryReady"]
                if old.get("executorOutcome", {}).get("status") == "success":
                    self.state["executorOutcome"] = old["executorOutcome"]
                latest = {_review_scope(row): row["id"] for row in self.state["candidates"].values()
                          if not executor_led or row["state"] != "superseded"}
                for row in self.state["candidates"].values():
                    for receipt in row.get("checks", {}).values():
                        if receipt["state"] == "started":
                            receipt["state"] = "unknown"
                    if row["state"] == "running" and self._restore_report(row):
                        restored_reports.append(row)
                        continue
                    unfinished = row["state"] in {"queued", "running", "paused", "failed", "timed_out"}
                    insufficient = row["state"] == "completed" and row.get("verdict") == "insufficient"
                    if (unfinished or insufficient) and latest.get(_review_scope(row)) == row["id"] and row["attempts"] < 3:
                        row.update(state="queued")
                        for field in ("verdict", "blocking", "error", "delivered", "delivery"):
                            row.pop(field, None)
                    elif row["state"] in {"queued", "running", "paused"}:
                        row.update(state="superseded")
                for plan in self.state["plans"]:
                    if plan["state"] in {"running", "queued"}:
                        plan["state"] = "interrupted"
                for grant in self.state.get("assistantRepairs", {}).get("grants", {}).values():
                    if grant.get('job', {}).get('state') in {'running', 'paused'}:
                        grant['job']['state'] = 'queued'
                    for draft in grant.get("drafts", {}).values():
                        for review in draft.get("checks", {}).values():
                            for receipt in review.get("checks", {}).values():
                                if receipt.get("state") == "started":
                                    receipt["state"] = "unknown"
        if self.runtime_completion:
            self.state.pop("deliveryReady", None)
        self.work = WorkQueue(self.state, task)
        if self.executor_led:
            from .executor_workflow import migrate
            migrate(self)
        for row in restored_reports:
            self.work.retest(row, accept_checks=row["verdict"] == "pass")
        self.save()
        self.session.emit("role_coordination", state="running", parallelRoles=True)
        for row in self.state["candidates"].values():
            if row["state"] == "queued":
                self.session.emit("review_candidate", candidateId=row["id"], revision=row["revision"], state="queued",
                                  **({"reviewUnitId": row["unitId"]} if row.get("unitId") else {}))
        for row in restored_reports:
            fields = dict(candidateId=row["id"], candidateRevision=row["revision"], recovered=True)
            self.session.emit("review_candidate", revision=row["revision"], state="completed", verdict=row["verdict"], **fields)
            self.session.emit("benni", ok=True, body=row["text"], reviewVerdict=row["verdict"], **fields)

    # 统一落盘本轮角色事实，使会话结束后仍能核对原版本。
    def save(self):
        if self.executor_led:
            from .execution_completion import overview
            self.state["completion"] = overview(self.state)
        atomic_json(self.path, self.state)

    def _report_path(self, run_id, candidate_id, attempt=1):
        suffix = f"-{attempt}" if attempt > 1 else ""
        return self.path.parent / ("r-" + run_id[:12] + "-" + candidate_id[:16] + suffix + ".json")

    # 恢复前轮报告中仍可对应原候选和材料的内容。
    def _restore_report(self, row):
        run_id = row.get("attemptRunId")
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            return False
        path = self._report_path(run_id, row["id"], row.get("attemptInRun", 1))
        report = W.read(path)
        if (report.get("runId") != run_id or report.get("candidateId") != row["id"]
                or report.get("candidateRevision") != row["revision"]
                or report.get("verdict") not in {"pass", "issues", "insufficient"}
                or not isinstance(report.get("text"), str)):
            return False
        if self.executor_led and report["verdict"] == "pass" and not _coverage_matches(row, report.get("coverage")):
            return False
        token = set_call(self._identity("reviewer", "restore_candidate_report"))
        try:
            self._candidate_resource(row)
        except (W.Problem, OSError):
            return False
        finally:
            reset_call(token)
        row.update(state="completed", verdict=report["verdict"], text=report["text"],
                   blocking=report.get("blocking") is True, report=path.name, coverage=report.get("coverage"))
        return True

    # 用主执行者终态推进角色记录，复核应保留其实际判断和候选身份。
    def executor_done(self, status, turns, why, final):
        with self.lock:
            if status == "success":
                self.state.pop("deliveryReady", None)
            self.state["executorOutcome"] = dict(status=status, turns=turns, why=why, final=final,
                                                 runId=self.session.run_id)
            if status in {"paused", "needs_human", "interrupted"}:
                from .executor_workflow import pause_reason
                self.state["pause"] = pause_reason(status, why or "")
            self.save()

    # 为同一运行创建角色视图，使证据和历史显式区分角色。
    def _role_session(self):
        session = copy(self.session)
        session.role_recovery = None
        session.context_sources = list(getattr(session, "context_sources", []))
        return session

    # 为真实角色工具动作附上会话、运行和调用标识。
    def _identity(self, role, tool):
        source = dict(runId=self.session.run_id, sessionId=Path(self.session.dir).name, role=role, tool=tool)
        if self.executor_led:
            source.update(self._task_binding())
        return source

    def _task_binding(self):
        return dict(logicalTaskId=self.task_input["id"], demandRevision=self.task_input["revision"])

    def task_brief(self):
        with self.lock:
            return evidence.redact(dict(goal=self.task[:10000], blueprint=self.state.get("blueprint"),
                                        **self._task_binding()))

    def verification_scope(self, resource_id):
        return (Path(self.session.dir).name, self.session.run_id, resource_id,
                json.dumps(self._task_binding(), sort_keys=True))

    def _resource(self, resource_id, capability=None):
        return C.context_resource(self.root, resource_id, capability)

    def _candidate_resource(self, row, capability=None):
        result = self._resource(row["resourceId"], capability)
        if row.get("sourcePath") != result[2]["path"]:
            raise W.Problem("candidate_checkout_changed", "候选所属检出已变化，不能在当前授权下恢复旧候选", 409)
        return result

    def _ensure_running(self):
        if self.stop() or (current_stop() or (lambda: False))():
            raise W.Problem("cancelled", "角色已停止", 409)

    def _review_stop(self):
        return self.review_closed.is_set() or self.stop() or self.review_deadline is not None and time.monotonic() >= self.review_deadline

    # 将实际停止来源转换成角色可记录的原因。
    def stop_reason(self, role='reviewer'):
        deadline = self.review_deadline if role == 'reviewer' else self.work_deadline
        closed = self.review_closed if role == 'reviewer' else self.planning_closed
        if deadline is not None and time.monotonic() >= deadline:
            return dict(kind='deadline', text='已到本轮截止时间，保留未完成进度')
        if self.stop():
            return dict(kind='user_stop', text='收到用户停止或插话请求，保留进度')
        if closed.is_set():
            return dict(kind='task_finishing', text='任务收尾，取消尚未完成的助手工作并保存进度')
        return dict(kind='internal_stop', text='角色执行停止，保留未完成进度')

    # 以角色状态和剩余预算判断何时留出成果交接空间。
    def handoff_due(self, *, repairing=False, lead=0):
        if getattr(self, "serial_review", False): return False
        if self.executor_led:
            return False
        with self.lock:
            checkpoint_sent = self.state.get("budgetCheckpoint", False)
            has_changes = bool(self.state["dirty"])
        if checkpoint_sent and not lead:
            return False
        deadline = self.work_deadline if repairing or checkpoint_sent else self.handoff_deadline
        if deadline is None or time.monotonic() + min(lead, self.handoff_check_lead) < deadline or self._review_stop():
            return False
        return has_changes

    def work_expired(self):
        return self.work_deadline is not None and time.monotonic() >= self.work_deadline and not self._review_stop()

    # 向执行者工具集登记候选、队列、复核和调查入口，受当前角色模式约束。
    def install(self, registry):
        def add(name, description, properties, function):
            registry.add(Tool(name, description, dict(type="object", properties=properties, required=list(properties)),
                              function, zone="offflow", kind="meta"))

        string = dict(type="string")
        if self.executor_led:
            from .executor_workflow import install
            install(self, registry)
            from .working_context import install as install_working_summary
            install_working_summary(self.session, 'executor', registry)
            if getattr(self, "serial_review", False):
                from .serial_review import install_executor
                install_executor(self, registry)
                if self.investigation:
                    self.investigation.install(registry)
                return
            if self.investigation:
                self.investigation.install(registry)
            if not self.background_reviewer:
                return
            from .assistant_patches import install_executor
            install_executor(self, registry)
            registry.add(Tool("publish_review_candidate", "将可独立验收的稳定阶段交给后台测试者。reason 描述行为、意图和后续独立工作。"
                              "仅完成部分必验项时用 requirement_ids 指定已就绪的项。",
                              dict(type="object", properties=dict(resource_id=string, expected_revision=string,
                                   reason=string, requirement_ids=dict(type="array", items=string)),
                                   required=["resource_id", "expected_revision", "reason"]),
                              lambda resource_id, expected_revision, reason, requirement_ids=None:
                              self.publish(resource_id, expected_revision, reason, intent=reason, provisional=True,
                                           requirement_ids=requirement_ids), zone="offflow", kind="meta"))
            add("record_review_feedback", "登记如何处理测试者的缺陷反馈。decision 用 fix（修正）、recheck（在新候选上复查）或 defer（暂缓）。"
                "记录处理不代表缺陷已关闭。",
                dict(item_id=string, decision=dict(type="string", enum=["fix", "recheck", "defer"]), reason=string),
                self.record_feedback)
            self._schedule()
            return
        add("request_role_plan", "有明确规划或重新规划需求时提交问题；等规划者给出方向，再继续相关执行。",
            dict(question=string), self.request_plan)
        add("publish_review_candidate", "仅在长任务完成一个可独立验收的阶段时提前送审。先用不带 relative_path 的 inspect_code 获取项目 projectRevision；单文件读写只返回文件版本。在 reason 说明已实现什么、为什么这样改和希望验证的行为。",
            dict(resource_id=string, expected_revision=string, reason=string), self.publish)
        add("report_work_progress", "将规划者拆出的功能子项标为已实现，使依赖它的工作可以继续。缺陷只能由新版本上的独立复验关闭。",
            dict(item_id=string), self._work_implemented)
        registry.add(Tool("publish_review_unit", "完成一个可独立验证的行为后送审。用队列条目、当前项目版本和本轮实际写入的 changeId，"
                          "说明验收条件。依赖文件可通过 watch_paths 追加。测试子 Agent 在封存版本上并行核对。",
                          dict(type="object", properties=dict(resource_id=string, expected_revision=string,
                               work_item=string, criterion=string, change_ids=dict(type="array", items=string),
                               watch_paths=dict(type="array", items=string)),
                               required=["resource_id", "expected_revision", "work_item", "criterion", "change_ids"]),
                          self.publish_unit, zone="offflow", kind="meta"))
        self._schedule()
        self._schedule_work_plan()

    def _work_implemented(self, item_id):
        self._ensure_running()
        with self.lock:
            try:
                state = self.work.implemented(item_id)
            except QueueConflict as exc:
                raise W.Problem(exc.code, str(exc), 409) from None
            self.save()
        return dict(ok=True, state=state)

    # 把执行者对已投递发现的处理记录到正式队列，后续仍需检验对应版本。
    def record_feedback(self, item_id, decision, reason):
        self._ensure_running()
        if not isinstance(item_id, str) or decision not in ("fix", "recheck", "defer") or (
            not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1200
        ):
            raise W.Problem("invalid_feedback", "请说明缺陷处理决定和理由")
        with self.lock:
            item = self.work.state["items"].get(item_id)
            if not item or item["kind"] != "defect" or item["state"] == "verified":
                raise W.Problem("review_feedback_changed", "缺陷不存在或已经复验")
            if decision == 'fix':
                from .repair_collaboration import claim
                claim(self, item_id, 'executor')
            elif decision == 'recheck' and item.get('repair', {}).get('role') == 'executor':
                from .repair_collaboration import update
                update(self, item_id, 'awaiting_verification', reason='执行者提交定点复验')
            elif decision == 'defer' and item.get('repair', {}).get('role') == 'executor':
                from .repair_collaboration import update
                update(self, item_id, 'interrupted', reason='执行者暂缓处理')
            item["handling"] = dict(decision=decision, reason=evidence.redact(reason.strip()), runId=self.session.run_id)
            self.work._change()
            self.save()
        self.session.emit("review_feedback_handled", itemId=item_id, decision=decision)
        return dict(ok=True, itemId=item_id, state=item["state"])

    def _undelivered_defects(self):
        with self.lock:
            return any(self._finding_needs_delivery(finding, row["revision"]) for row in self.state["candidates"].values()
                       for finding in row.get("findings", {}).values())

    # 发现仍待送达时先让执行者收到复核证据，再继续检查当前改动。
    def pause_check_for_feedback(self):
        if getattr(self, "serial_review", False): return False
        if self.state.get("assistantRepairs", {}).get("grants"):
            # Explicit patch collaboration delivers at model boundaries. Pending
            # feedback is not a global check barrier; final delivery still gates.
            return False
        with self.lock:
            unhandled = any(item["kind"] == "defect" and item["state"] != "verified"
                            and not item.get("handling") for item in self.work.state["items"].values())
        return self.executor_led and (self._undelivered_defects() or unhandled)

    def prepare_project_check(self, resource_id=None):
        if getattr(self, "serial_review", False): return False
        """Deliver untouched findings first; otherwise start repaired-version review before self-check."""
        paused = self.pause_check_for_feedback()
        from .execution_completion import behavior_enabled
        if not behavior_enabled():
            return paused
        if self.executor_led and not paused:
            from .executor_workflow import before_checks
            revisions = None
            if resource_id is not None:
                with self.lock:
                    pending = any(item["kind"] == "defect" and item["resourceId"] == resource_id
                                  and item["state"] != "verified" and item.get("handling")
                                  for item in self.work.state["items"].values())
                if not pending:
                    return False
                current = self._current_revision(resource_id)
                if current is None:
                    return False  # The check tool retains its own authoritative permission/version validation.
                revisions = {resource_id: current}
            before_checks(self, repairs_only=True, revisions=revisions)
        return paused

    def _ready_work_item(self, item_id):
        with self.lock:
            item = self.work.state["items"].get(item_id) if isinstance(item_id, str) else None
            if (not item or item["state"] == "verified"
                    or any(self.work.state["items"][key]["state"] not in {"implemented", "verified"}
                           for key in item["dependsOn"])):
                raise W.Problem("work_dependencies_pending", "验收项不存在或前置工作尚未完成", 409)
            return item

    # 按工作项、验收目标和已有变更回执发布受限复核单元。
    def publish_unit(self, resource_id, expected_revision, work_item, criterion, change_ids, watch_paths=None):
        self._ready_work_item(work_item)
        if (not isinstance(change_ids, list) or not 1 <= len(change_ids) <= 64
                or not all(isinstance(key, str) and re.fullmatch(r"[a-f0-9]{32}", key) for key in change_ids)
                or len(set(change_ids)) != len(change_ids)
                or watch_paths is not None and (not isinstance(watch_paths, list) or len(watch_paths) > 48)):
            raise W.Problem("invalid_review_unit", "请提供有效写入回执和有限的依赖文件列表", 400)
        watches = [review_units.relative_path(path) for path in watch_paths or []]
        if any(_excluded(path) for path in watches):
            raise W.Problem("resource_denied", "依赖路径含受保护文件", 403)
        return dict(self.publish(resource_id, expected_revision, criterion, provisional=True,
                                 unit_request=(work_item, change_ids, watches)), itemId=work_item)

    def _planner_tools(self, *, scheduling=False, stopped=None):
        base = scoped_roles.registry(self.root, role="planner")
        tools = Registry()
        excluded = {"run_project_check", "read_check_output"}
        if scheduling:
            excluded.add("ask_human")
        for name in base.names():
            if name not in excluded:
                tools.add(base.get(name))
        tools.add(Tool("read_work_queue", "读取本轮功能、缺陷、依赖、复验进度与队列版本。",
                       dict(type="object", properties={}), self._read_work_queue, zone="offflow", kind="read", readonly=True))

        def propose(queue_revision, order, reason, features=None, dependencies=None):
            with self.lock:
                if self._planning_stop() or stopped and stopped():
                    raise W.Problem("cancelled", "调序窗口已结束", 409)
                self._check_work_scope()
                try:
                    result = self.work.propose(queue_revision, order, reason,
                                               [] if features is None else features,
                                               {} if dependencies is None else dependencies)
                except QueueConflict as exc:
                    raise W.Problem(exc.code, str(exc), 409) from None
                self.save()
                self.session.emit("work_queue_order", queueRevision=result["queueRevision"], order=order)
                return dict(ok=True, **result)

        schema = dict(type="object", required=["queue_revision", "order", "reason"], properties=dict(
            queue_revision=dict(type="integer"), order=dict(type="array", items=dict(type="string")),
            reason=dict(type="string"), features=dict(type="array", items=dict(type="object",
                properties=dict(id=dict(type="string"), text=dict(type="string")), required=["id", "text"])),
            dependencies=dict(type="object", additionalProperties=dict(type="array", items=dict(type="string")))))
        tools.add(Tool("propose_work_order", "按最新队列版本提交全序、调序原因和可选依赖。可把用户要求拆成 feature- 编号的子项。"
                       "保持原目标及权限。正在执行的工具会先安全收尾。",
                       schema, propose, zone="offflow", kind="meta"))
        return tools

    def _read_work_queue(self):
        with self.lock:
            self._check_work_scope()
            view = self.work.read()
            acceptance = [dict(itemId=row["unitId"], revision=row["revision"], state=row["state"],
                               verdict=row.get("verdict")) for key in self.state["reviewUnits"].values()
                          if (row := self.state["candidates"][key])]
            return dict(ok=True, **view, workingRevisions=dict(self.state["dirty"]), acceptance=acceptance,
                        completion=self.state.get("completion"))

    def _check_work_scope(self):
        defects = [(item["resourceId"], item["sourcePath"]) for item in self.work.state["items"].values()
                   if item["kind"] == "defect"]
        if not defects:
            return
        scope = projects.scope(Path(self.session.dir).name)
        current = {(r["id"], r["path"]) for r in scope["resources"]
                   if r["kind"] == "code" and r["permission"] == "read"}
        captured = {(r["id"], r["path"]) for r in (scope["lastRun"] or {}).get("resources", [])}
        if not set(defects) <= current & captured:
            raise W.Problem("queue_scope_changed", "需求队列的代码范围已变更，无法读取原队列", 403)

    def _planning_stop(self):
        return self.planning_closed.is_set() or self.stop() or (
            self.work_deadline is not None and time.monotonic() >= self.work_deadline)

    def _schedule_work_plan(self):
        if self.executor_led:
            return
        with self.lock:
            if self.planning_active or self._planning_stop() or not self.work.state["pendingPlan"]:
                return
            self.planning_active = True
            self.planning.submit(self._drain_work_plans)

    def _drain_work_plans(self):
        while True:
            with self.lock:
                pending = list(self.work.state["pendingPlan"])
                if not pending or self._planning_stop():
                    self.planning_active = False
                    return
                goal = self.work.state["items"]["goal"]["text"]
                start_revision = self.work.state.get("orderRevision", 0)
            deadline = time.monotonic() + 45

            def stopped():
                return self._planning_stop() or time.monotonic() >= deadline

            failed = False
            try:
                scoped_roles.run(self.root, self._role_session(), "planner",
                                 "用户目标：" + goal + "\n测试者提交了缺陷，请读取统一队列并调序。"
                                 "优先安排有影响的缺陷，让无关工作继续。需要改变顺序或记录依赖时，调用 propose_work_order。"
                                 "不要改变用户需求、扩大授权或等待用户回复。",
                                 should_stop=stopped, tools=self._planner_tools(scheduling=True, stopped=stopped))
            except Exception:
                failed = True
            with self.lock:
                if self._planning_stop():
                    self.planning_active = False
                    return
                applied = self.work.state.get("orderRevision", 0) > start_revision
                self.work.state["pendingPlan"] = [key for key in self.work.state["pendingPlan"] if key not in pending]
                self.save()
                self.session.emit("work_queue_planning", items=pending,
                                  state="applied" if applied else "failed" if failed else "unchanged")

    # 将当前队列及作用域交给后台规划入口，读回建议仍要由执行者确认。
    def request_plan(self, question):
        if not isinstance(question, str) or not question.strip() or len(question) > 3000:
            raise W.Problem("invalid_plan_request", "请提供具体规划问题", 400)
        self._ensure_running()
        question = question.strip()
        stop = current_stop() or self.stop
        with self.lock:
            revisions = dict(self.state["dirty"])
            prior = next((p for p in reversed(self.state["plans"])
                          if p["question"] == question and p.get("revisions") == revisions), None)
            if prior and prior["state"] == "completed":
                return dict(ok=True, plan=prior["text"], reused=True)
            if len(self.state["plans"]) >= 20:
                raise W.Problem("plan_capacity", "规划请求已达到本任务保留上限", 409)
            row = dict(id=len(self.state["plans"]) + 1, question=question, revisions=revisions, state="running")
            self.state["plans"].append(row)
            self.save()
        tools = self._planner_tools(stopped=stop)
        try:
            text = scoped_roles.run(self.root, self._role_session(), "planner",
                                    self.task + "\n执行者请求规划：" + question,
                                    should_stop=lambda: self.stop() or stop(), tools=tools)
        except scoped_roles.RoleWaiting as exc:
            self._plan_state(row, "waiting_human")
            return dict(ok=False, needs_human=True, question=exc.question or str(exc), memoryConflictId=exc.conflict_id)
        except Interrupted:
            self._plan_state(row, "interrupted")
            raise W.Problem("cancelled", "规划请求已停止", 409) from None
        except Exception:
            self._plan_state(row, "failed")
            raise
        self._plan_state(row, "completed", text=text)
        return dict(ok=True, plan=text)

    def _plan_state(self, row, state, **fields):
        with self.lock:
            row.update(state=state, **fields)
            self.save()

    # 由执行者提交期望修订，核对资源范围后将可复核内容和要求绑定到候选快照。
    def publish(self, resource_id, expected_revision, reason, *, intent=None, provisional=False, final=False,
                unit_request=None, requirement_ids=None):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1500:
            raise W.Problem("invalid_candidate", "请说明这个版本需要验证的内容", 400)
        criteria = []
        if self.executor_led:
            from .delivery_review import code_review_requirements

            criteria = [{key: item[key] for key in ("id", "revision", "text")}
                        for item in code_review_requirements(Path(self.session.dir).name, self.task_input["id"])
                        if item["targetRef"]["id"] == resource_id]
            if requirement_ids is not None:
                if (not provisional or not isinstance(requirement_ids, list) or not requirement_ids
                        or any(not isinstance(key, str) for key in requirement_ids)
                        or not set(requirement_ids) <= {item["id"] for item in criteria}):
                    raise W.Problem("invalid_candidate_scope", "只可送审当前资源已登记的必验项")
                criteria = [item for item in criteria if item["id"] in requirement_ids]
            if not criteria:
                criteria = [dict(id="task:" + resource_id,
                                 revision=(self.state.get("effectiveRequirements") or {}).get("contentHash", self.task_input["revision"]),
                                 text=self.task)]
            elif self.state.get("effectiveRequirements") and requirement_ids is None:
                criteria.append(dict(id="task:" + resource_id,
                    revision=self.state["effectiveRequirements"]["contentHash"],
                    text="核对当前资源相关的有效要求，保留仍有效旧目标；后续明确更正覆盖冲突部分：\n" + self.task))
        def check():
            if self._review_stop():
                raise W.Problem("cancelled", "候选封存窗口已结束", 409)
            self._ensure_running()

        check()
        _, _, ws, _ = self._resource(resource_id)
        with FileLock(W.writer_lock_path(ws["path"]), check=check):
            _, _, ws, _ = self._resource(resource_id)
            W.check_revision(ws, expected_revision)
            with self.lock:
                same = [row for row in self.state["candidates"].values()
                        if row["resourceId"] == resource_id and row["sourcePath"] == ws["path"]
                        and row["revision"] == expected_revision]
                prior = next((row for row in same if not row.get("unitId")
                              and row.get("criteria", []) == criteria), None) if not unit_request else None
                if prior:
                    if self.executor_led and prior.get("verdict") == "pass":
                        invalid = self._invalid_history(prior)
                        if any(set(item.get("actionIds", [])) & invalid for item in prior.get("coverage", [])):
                            prior["invalidatedReport"] = dict(verdict=prior["verdict"], coverage=prior.get("coverage"),
                                                               reason="旧检查来源、环境或产物已无法核对")
                            self._review_state(prior, "queued", verdict=None)
                            self._schedule()
                    if self.executor_led and (final or prior["state"] == "superseded"):
                        for queued in self.state["candidates"].values():
                            if queued is not prior and queued["state"] == "queued" and _review_scope(queued) == _review_scope(prior):
                                self._review_state(queued, "superseded")
                    if self.executor_led and final and (prior.get("criteria", []) != criteria
                            or prior.get("verdict") == "pass" and not _coverage_matches(prior, prior.get("coverage"))):
                        prior.update(criteria=criteria, criteriaChanged=True, provisional=True)
                    if (self.executor_led and final and prior["state"] == "completed"
                            and prior.get("verdict") == "pass" and self.work.pending_retests(prior)):
                        self._review_state(prior, "queued", verdict=None)
                        self._schedule()
                    if final and prior.get("provisional"):
                        prior["finalRequested"] = True
                        self._promote_checkpoint(prior)
                        self.save()
                        self._schedule()
                    if prior["state"] == "superseded" and not prior["attempts"]:
                        self._review_state(prior, "queued")
                        self._schedule()
                    return dict(ok=True, candidateId=prior["id"], revision=expected_revision,
                                state=prior["state"], reused=True, snapshotMs=0,
                                projectBaseline="captured" if prior.get("handoff", {}).get("baseline") else "unavailable")
            started = time.monotonic()
            ident = Path(self.session.dir).name
            runs = (self.state["baselineRun"], self.session.run_id)
            records_for_refresh = []
            with FileLock(W.home() / "change-store.lock", check=check), H.database() as db:
                H.metadata_capacity(db, 16 * 1024 * 1024)
                baseline = H.candidate_baseline(db, ident, resource_id, ws["path"], runs)
                if same:
                    snapshot_id = same[0]["snapshot"]
                    manifest = H.load_snapshot(db, snapshot_id)
                else:
                    try:
                        snapshot_id, manifest = H.snapshot(db, ws, check=check)
                    except H.SourceSnapshotCapacity as error:
                        raise W.Problem("candidate_snapshot_unavailable",
                            f"{error.message}。本次未封存整仓候选；可通过单次操作历史及累计差异核对已保留文件。", 409) from error
                unit = None
                if unit_request:
                    item_id, ids, watches = unit_request
                    records = [H.get_record(db, ident, key) for key in ids]
                    if any(r["state"] != "observed" or r["resourceId"] != resource_id
                           or r["resourcePath"] != ws["path"] or r["source"].get("runId") not in (*runs, self.state.get("resumedFrom"))
                           or not any(f.get("attribution") == "verified_file_write" and not _excluded(f["path"])
                                      for f in r["files"]) for r in records):
                        raise W.Problem("review_change_scope", "只可引用本任务当前工作树已确认的写入", 403)
                    unit = review_units.bind(records, manifest, work_item=item_id,
                                             criterion=evidence.redact(reason), watches=watches)
                    if any(_excluded(path) for path in unit["changedPaths"]):
                        raise W.Problem("review_change_scope", "验收项含受保护文件", 403)
                if final and self.state["reviewUnits"]:
                    changes = db.execute("SELECT data FROM changes WHERE thread=? ORDER BY seq DESC LIMIT 128", (ident,))
                    records_for_refresh = sorted((r for (data,) in changes if (r := json.loads(data))["state"] == "observed"
                                                  and r["resourceId"] == resource_id and r["resourcePath"] == ws["path"]
                                                  and r["source"].get("runId") in (*runs, self.state.get("resumedFrom"))),
                                                 key=lambda r: r["observationOrder"])
            snapshot_ms = round((time.monotonic() - started) * 1000)
            _, _, current, _ = self._resource(resource_id)
            if current["path"] != ws["path"]:
                raise W.Problem("candidate_checkout_changed", "封存期间工作区已切换", 409)
            W.check_revision(ws, expected_revision)
            receipts = C.candidate_receipts(ident, resource_id, ws["id"], runs, expected_revision)
            baseline_status = "captured" if baseline else "unavailable"
            handoff = dict(baseline=baseline, projectBaseline=baseline_status, intent=evidence.redact(intent or reason)[:3200],
                           selfChecks=evidence.redact(receipts), currentRequirements=self.state.get("effectiveRequirements") or self.task,
                           reusableMaterials=self.state.get("reviewMaterials", {}).get(resource_id, []))
            if final:
                self._refresh_units(resource_id, ws["path"], expected_revision, snapshot_id, manifest, handoff,
                                    records_for_refresh)
            result = self._enqueue(resource_id, expected_revision, snapshot_id, reason,
                                   source_path=ws["path"], handoff=handoff, provisional=provisional, final=final,
                                   unit=unit, criteria=criteria)
        self.session.emit("review_snapshot", candidateId=result["candidateId"], revision=expected_revision,
                          elapsedMs=snapshot_ms)
        return dict(result, snapshotMs=snapshot_ms, projectBaseline=baseline_status)

    # 以源、修订和快照身份合并重复候选，保留可交给复核的原材料。
    def _enqueue(self, resource_id, revision, snapshot_id, reason, *, source_path=None, handoff=None,
                 provisional=False, final=False, unit=None, criteria=()):
        identity = f"{resource_id}:{source_path}:{revision}:{snapshot_id}"
        if criteria:
            identity += ":" + json.dumps(criteria, sort_keys=True, ensure_ascii=False)
        if unit:
            identity += ":" + json.dumps(unit, sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(identity.encode()).hexdigest()
        with self.lock:
            candidates = self.state["candidates"]
            if key in candidates:
                return dict(ok=True, candidateId=key, revision=revision, state=candidates[key]["state"], reused=True)
            if len(candidates) >= 20:
                raise W.Problem("candidate_capacity", "候选版本已达到本任务保留上限", 409)
            unit_id = unit["itemId"] if unit else None
            item = self.work.state["items"].get(unit_id)
            if item and item["state"] == "pending" and item["kind"] == "feature":
                self._ready_work_item(unit_id)
            for old in candidates.values():
                if _review_scope(old) == (resource_id, source_path, unit_id) and old["state"] == "queued":
                    old["state"] = "superseded"
            candidates[key] = dict(id=key, resourceId=resource_id, revision=revision, snapshot=snapshot_id,
                                   sourcePath=source_path, reason=evidence.redact(reason), state="queued", attempts=0,
                                   provisional=provisional, finalRequested=final)
            if handoff is not None:
                candidates[key]["handoff"] = handoff
            if criteria:
                candidates[key]["criteria"] = list(criteria)
            if unit:
                candidates[key].update(unitId=unit_id, unit=unit)
                self.state["reviewUnits"][json.dumps(_review_scope(candidates[key]))] = key
                self.work.link_changes(unit_id, unit["changeIds"])
                if (item := self.work.state["items"].get(unit_id)) and item["kind"] == "feature" and unit_id != "goal":
                    self.work.implemented(unit_id)
            else:
                self.work.retest(candidates[key], bind=True)
            self.save()
            self.session.emit("review_candidate", candidateId=key, revision=revision, state="queued",
                              **({"reviewUnitId": unit_id} if unit_id else {}))
            self._schedule()
        return dict(ok=True, candidateId=key, revision=revision, state="queued")

    # 刷新队列里复核单元与真实版本的关联。
    def _refresh_units(self, resource_id, source_path, revision, snapshot_id, manifest, handoff, records=()):
        with self.lock:
            active = self._units_for(resource_id, source_path)
            for row in active:
                unit = row["unit"]
                refreshed = review_units.refresh(unit, manifest, records)
                if refreshed is None:
                    continue
                self._enqueue(resource_id, revision, snapshot_id, refreshed["criterion"], source_path=source_path,
                              handoff=handoff, provisional=True, unit=refreshed)
                self.session.emit("review_unit_stale", itemId=row["unitId"], candidateId=row["id"], revision=revision)

    def _units_for(self, resource_id, source_path):
        return [self.state["candidates"][key] for key in self.state["reviewUnits"].values()
                if self.state["candidates"][key]["resourceId"] == resource_id
                and self.state["candidates"][key]["sourcePath"] == source_path]

    # 用实际工具回执跟踪读写和检查，决定哪些工作项或候选已受影响。
    def observe(self, jobs, results):
        stable = []
        for job in jobs:
            output = results.get(job["i"], (None, 0))[0]
            if not isinstance(output, dict):
                continue
            if self.executor_led and self.background_reviewer and job["name"] == "record_delivery_evidence" and output.get("ok"):
                payload = job["args"].get("payload") or {}
                target = payload.get("target") or {}
                if payload.get("operation") == "coverage" and payload.get("kind") == "automatic" and target.get("kind") == "code":
                    stable.append((payload.get("requirementId"), target.get("id"), target.get("revision")))
            rid = job["args"].get("resource_id")
            integrated = job["name"] == "apply_subtask_integration" and output.get("ok")
            if integrated:
                rid = output.get("resourceId")
            if not isinstance(rid, str):
                continue
            changed = (output.get("projectRevision") or output.get("fileChangeRevision")) if integrated or job["name"] in WRITES and output.get("ok") else None
            if job["name"] == "run_project_check" and (output.get("verification") or {}).get("status") == "stale":
                changed = output.get("projectRevisionAfter")
            # Ordinary edits deliberately leave the whole-project version
            # unresolved. Bind it at a real inspection/check boundary, never
            # pass a file hash or event watermark to candidate verification.
            observed = (output.get("projectRevisionAfter") if job["name"] == "run_project_check"
                        else output.get("projectRevision") if job["name"] == "inspect_code" else None)
            if observed:
                with self.lock:
                    if str(self.state["dirty"].get(rid, "")).startswith("pending:"):
                        self.state["dirty"][rid] = observed
                        self.save()
            if changed:
                if self.investigation and job["name"] in WRITES:
                    path = job["args"].get("relative_path")
                    if isinstance(path, str):
                        self.investigation.invalidate(rid, review_units.relative_path(path))
                with self.lock:
                    self.state["dirty"][rid] = changed
                    ledger = self.state.setdefault("changeLedger", {})
                    recent = list(ledger.get(rid, {}).get("recent", []))
                    recent.append(dict(revision=changed, path=job["args"].get("relative_path"),
                                       operationId=output.get("operationId"), actionId=job.get("action_id"),
                                       itemId=job["args"].get("issue_id"), at=time.time(), role="executor"))
                    ledger[rid] = dict(revision=changed, path=job["args"].get("relative_path"),
                                       changeId=output.get("changeId"), actionId=job.get("action_id"), recent=recent[-12:])
                    if self.executor_led:
                        for item in self.work.state["items"].values():
                            if (item["kind"] == "defect" and item["resourceId"] == rid
                                    and item["state"] == "verified" and item["verifiedRevision"] != changed):
                                item["state"] = "awaiting_review"
                    if integrated and output.get("workItemId") and output.get("changeId"):
                        self.work.link_changes(output["workItemId"], [output["changeId"]])
                    self.save()
        if self.executor_led:
            # A reply may contain a write followed by a check. Register the resulting version first.
            from .executor_workflow import observe
            observe(self, jobs, results)
        for requirement, resource, revision in stable:
            self._publish_stable_stage(requirement, resource, revision)

    # 稳定的阶段版本可提前形成复核候选，使用原改动材料。
    def _publish_stable_stage(self, requirement, resource, revision):
        if getattr(self, "serial_review", False): return
        from .delivery_review import code_review_requirements, executor_gate

        if self._review_stop():
            return
        with self.lock:
            if self.state["dirty"].get(resource) != revision or any(
                row["resourceId"] == resource for row in self.state["candidates"].values()
            ):
                return
        token = set_call(self._identity("executor", "publish_review_candidate"))
        try:
            criteria = code_review_requirements(Path(self.session.dir).name, self.task_input["id"])
            scoped = {item["id"] for item in criteria if item["targetRef"]["id"] == resource}
            if requirement not in scoped:
                return
            gate = executor_gate(self.session.dir, self.task_input, only_requirements=scoped)
            if not scoped <= {item["id"] for item in gate["requirements"] if item["status"] in {"passed", "confirmed"}}:
                return
            self.publish(resource, revision, "已登记当前版本的必验检查回执。开始独立复核，执行者可继续其他工作。",
                         provisional=True)
        except (W.Problem, OSError, ValueError) as error:
            self.session.emit("review_candidate", state="failed", reason=str(error))
        finally:
            reset_call(token)

    # 对未送审的实际改动按确认过的资源版本补候选。
    def _publish_dirty(self, intent, reason, *, provisional=False, final=False):
        with self.lock:
            targets = dict(self.state["dirty"])
        targets = {rid: self._current_revision(rid) if str(revision).startswith("pending:") else revision
                   for rid, revision in targets.items()}
        if self.executor_led:
            from .delivery_review import code_review_requirements

            requirements = code_review_requirements(Path(self.session.dir).name, self.task_input["id"])
            with self.lock:
                resources = {item["resourceId"] for item in self.work.state["items"].values()
                             if item["kind"] == "defect"}
            resources.update(targets)
            resources.update(item["targetRef"]["id"] for item in requirements)
            targets = {rid: self._current_revision(rid) for rid in sorted(resources)}
            if any(revision is None for revision in targets.values()):
                raise W.Problem("candidate_scope", "无法核对需要复核的代码版本", 409)
        candidate_ids = []
        token = set_call(self._identity("executor", "publish_review_candidate"))
        try:
            for resource_id, revision in targets.items():
                candidate = self.publish(resource_id, revision, reason, intent=intent,
                                         provisional=provisional, final=final)
                candidate_ids.append(candidate["candidateId"])
        finally:
            reset_call(token)
        return candidate_ids

    # 在执行者自查边界保存可核对检查点，支持后续复核和恢复。
    def publish_checkpoint(self, intent, *, closing=False):
        if getattr(self, "serial_review", False): return
        if self._review_stop():
            return
        if not closing:
            with self.lock:
                self.state["budgetCheckpoint"] = True
                self.save()
        reason = ("执行窗口结束，复核最后确认的版本。执行者尚未声明交付完成。" if closing else
                  "保底候选。执行者尚未声明交付完成，仍在继续实现。发现已复现的问题请立即反馈。")
        try:
            self._publish_dirty(intent, reason, provisional=not closing, final=closing)
        except (W.Problem, OSError, ValueError) as exc:
            self.session.emit("review_candidate", state="failed", reason=str(exc))

    # 在执行者仍能处理反馈时核对当前版本的持久复核和要求。由相应模式选择当前的交付关口。
    def delivery_gate(self, final, turns, *, partial=False):
        if getattr(self, "runtime_completion", False) or getattr(self, "serial_review", False): return ('pass', '')
        """Settle the ready revision while the executor can still apply feedback."""
        if self.executor_led:
            from .assistant_patches import jobs
            while jobs(self):
                if self._review_stop():
                    return 'paused', '修复任务未结束，保留副本和回执'
                from .assistant_patches import pending
                if pending(self):
                    return 'repair', '助手补丁已送达，请核对并合并或明确拒绝'
                time.sleep(.1)
            failed_jobs = jobs(self, ('failed', 'paused'))
            if failed_jobs:
                seen = all(g['job'].get('reported') for g in failed_jobs)
                for grant in failed_jobs:
                    grant['job']['reported'] = True
                self.save()
                return ('paused' if seen else 'repair'), '助手未交付补丁，请核对原因，调整任务或明确撤销授权后自行处理'
            if not partial and not self._review_stop():
                from .execution_completion import preflight
                if gap := preflight(self):
                    return gap
            from .assistant_patches import pending
            if pending(self):
                return "repair", "助手补丁尚待处理；请核对、应用或明确拒绝，暂缓不表示完成"
        if not self.background_reviewer:
            return "pass", ""
        if self._review_stop():
            return "paused", "复核窗口已停止，保留当前改动和回执"
        if self.executor_led:
            pending = self._undelivered_defects()
            if pending:
                return "repair", "独立复核发现待核对的缺陷"
        with self.lock:
            if self.state["dirty"]:
                self.state["deliveryReady"] = dict(final=evidence.redact(final), turns=turns, partial=partial)
                self.save()
        reason = ("复核截止时最后确认的候选。执行者尚未声明交付完成。" if partial else
                  "交付前独立复核，优先验证用户要求和未关闭的问题。")
        try:
            candidate_ids = self._publish_dirty(final, reason, final=True)
        except (W.Problem, OSError, ValueError) as exc:
            return "paused", "未能封存交付候选：" + str(exc)

        wait_started = None
        while True:
            with self.lock:
                rows = [dict(self.state["candidates"][key]) for key in candidate_ids]
                resources = {row["resourceId"] for row in rows}
                with_findings = [dict(row) for row in self.state["candidates"].values()
                                 if not self.executor_led and row["resourceId"] in resources
                                 and (any(not f.get("delivered") for f in row.get("findings", {}).values())
                                      or row["state"] == "completed"
                                      and row.get("verdict") == "issues" and not row.get("delivered"))]
            if any(self._current_revision(row["resourceId"], row["sourcePath"]) is not None for row in with_findings):
                with self.lock:
                    self.state.pop("deliveryReady", None)
                    self.save()
                return "repair", "独立复核已有缺陷证据，返回执行者修正"
            if all(row["state"] not in {"queued", "running"}
                   and not (row.get("provisional") and row.get("finalRequested")) for row in rows):
                if wait_started is not None:
                    self.session.emit("assistant_wait", role="reviewer", phase="delivery",
                                      elapsedMs=round((time.monotonic() - wait_started) * 1000))
                if self.executor_led and self._undelivered_defects():
                    return "repair", "独立复核发现待核对的缺陷"
                break
            if self._review_stop():
                if wait_started is not None:
                    self.session.emit("assistant_wait", role="reviewer", phase="delivery",
                                      elapsedMs=round((time.monotonic() - wait_started) * 1000))
                return "paused", "复核窗口已停止，保留候选和已完成的回执"
            if wait_started is None and self.executor_led:
                wait_started = time.monotonic()
            time.sleep(.1)
        if rows and not all(self._current(row) for row in rows):
            return "paused", "复核期间工作区版本发生变化，需重新确认交付范围"
        if all(row["state"] == "completed" and row.get("verdict") == "pass"
               and (not self.executor_led or _coverage_matches(row, row.get("coverage"))) for row in rows):
            with self.lock:
                pending_units = [unit["unitId"] for row in rows for unit in self._units_for(row["resourceId"], row["sourcePath"])
                                 if unit.get("verdict") != "pass"]
                outstanding = [item["id"] for row in rows for item in self.work.applicable(row)]
                if outstanding and not self.work_expired():
                    self.state.pop("deliveryReady", None)
                    self.save()
            if outstanding:
                reason = "需求队列有缺陷尚未通过新版本定点复验：" + "、".join(outstanding)
                return ("paused" if self.executor_led or self.work_expired() else "repair"), reason
            if pending_units:
                return "paused", "验收项尚无独立通过回执：" + "、".join(pending_units)
            if partial:
                return "paused", "已复核截止时的候选；执行者尚未提交完整交付"
            if self.executor_led:
                with self.lock:
                    if self._undelivered_defects():
                        return "repair", "独立复核发现待核对的缺陷"
                    if any(self.work.applicable(row) for row in rows):
                        return "repair", "交付确认期间有缺陷尚未复验"
                    if any(not self._current(row) for row in rows):
                        return "paused", "交付确认期间版本变化，需重新复核"
                    self.review_closed.set()
            return "pass", ""
        if not self.executor_led and any(row["state"] == "completed" and row.get("verdict") == "issues" and not row.get("delivered")
               for row in rows):
            with self.lock:
                self.state.pop("deliveryReady", None)
                self.save()
            return "repair", "独立复核发现问题，返回执行者修正"
        return "paused", "当前版本尚无完整的独立复核通过结论，已保留候选和回执"

    # 只为尚待处理且仍有效的候选启动独立复核。
    def _schedule(self):
        if getattr(self, "serial_review", False): return
        with self.lock:
            if self._review_stop():
                return
            queued = [r for r in self.state["candidates"].values() if r["state"] == "queued"]
            for row in queued:
                if row.get("unitId") and row["id"] not in self.unit_futures:
                    self.unit_futures[row["id"]] = self.unit_workers.submit(self._review_unit, row)
            from .assistant_patches import jobs
            if self.worker is None and (any(not r.get("unitId") for r in queued) or jobs(self, ('queued',))):
                self.worker = self.queue.submit(self._drain)

    # 记录原候选进入复核的身份与状态。
    def _begin_review(self, row):
        attempt = row.get("attemptInRun", 1) + 1 if row.get("attemptRunId") == self.session.run_id else 1
        row.update(state="running", attempts=row["attempts"] + 1, attemptRunId=self.session.run_id, attemptInRun=attempt)
        if row.get("provisional"):
            row["recoveryFingerprint"] = self._recovery_fingerprint(row)
        self.save()

    def _recovery_fingerprint(self, row):
        # A newly written probe is not evidence that its environment now works.
        preparation = self.state.get("reviewPreparation", {}).get(row.get("resourceId"), {})
        passed = sorted(rec.get("actionId", "") for source in (row, preparation)
                        for rec in source.get("checks", {}).values() if rec.get("state") == "passed")
        return hashlib.sha256(json.dumps([row.get("criteria"),
            passed, C.check_environment()],
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _review_unit(self, row):
        with self.lock:
            if row["state"] != "queued" or self._review_stop():
                return
            self._begin_review(row)
        self._review(row)

    # 条件满足后用已核对的候选推进正式复核记录。
    def _promote_checkpoint(self, row):
        if (not row.get("provisional") or not row.get("finalRequested") or row["state"] == "running"
                or self._review_stop()):
            return
        row["provisional"] = False
        if self.review_deadline is not None and time.monotonic() + REVIEW_CONCLUSION_SECONDS + 12 >= self.review_deadline:
            self.save()
            return
        criteria_changed = row.pop("criteriaChanged", False)
        recovery = self._recovery_fingerprint(row)
        recover_insufficient = (row.get("verdict") == "insufficient"
                                and row.get("recoveryFingerprint") != recovery)
        if ((criteria_changed and row["state"] == "completed"
             and not (row.get("verdict") == "pass" and _coverage_matches(row, row.get("coverage"))))
                or row["state"] in {"paused", "timed_out", "failed"}
                or recover_insufficient):
            row["recoveryFingerprint"] = recovery
            for field in ("verdict", "error", "delivered", "delivery", "blocking"):
                row.pop(field, None)
            self._review_state(row, "queued")

    # 等待在途复核收尾，遵守当前运行的停止和时间范围。
    def _drain(self):
        while True:
            with self.lock:
                from .assistant_patches import jobs, run_job
                queued = [r for r in self.state["candidates"].values() if r["state"] == "queued" and not r.get("unitId")]
                row = next((r for r in queued if r.get("finalRequested")), queued[0] if queued else None)
                repair = next(iter(jobs(self, ('queued',))), None) if row is None else None
                if row is None and repair is None or self._review_stop():
                    self.worker = None
                    return
                if repair:
                    repair['job']['state'] = 'running'
                    self.save()
                else:
                    self._begin_review(row)
            if repair:
                run_job(self, repair)
                continue
            self._review(row)
            with self.lock:
                self._promote_checkpoint(row)

    # 在复核角色的独立工具和资料范围内核对候选，把原工具结果和复核结论关联到同一版本。
    def _review(self, row):
        role = self._role_session()
        report = {}
        fields = dict(candidateId=row["id"], candidateRevision=row["revision"])
        if row.get("unitId"):
            fields["reviewUnitId"] = row["unitId"]
            role.review_unit = True
        token = set_call(self._identity("reviewer", "review_candidate"))
        try:
            self._candidate_resource(row)
            with H.database() as db:
                manifest = H.load_snapshot(db, row["snapshot"])
                baseline_id = row.get("handoff", {}).get("baseline")
                baseline = H.load_snapshot(db, baseline_id) if baseline_id else None
            comparison_base = baseline
            with self.lock:
                prior_rows = list(self.state["candidates"].values())
                prior_rows = prior_rows[:next((i for i, item in enumerate(prior_rows) if item["id"] == row["id"]), len(prior_rows))]
                previous = next((item for item in reversed(prior_rows)
                                 if _review_scope(item) == _review_scope(row)
                                 and item.get("state") == "completed" and item.get("verdict")), None)
            if previous:
                with H.database() as db:
                    comparison_base = H.load_snapshot(db, previous["snapshot"])
                row["comparison"] = dict(kind="previous_candidate", candidateId=previous["id"],
                    revision=previous["revision"], verdict=previous["verdict"],
                    criteria=previous.get("criteria", []),
                    coverage=previous.get("coverage", []),
                    checks=[dict(rec) for rec in previous.get("checks", {}).values()])
            else:
                row["comparison"] = dict(kind="initial_baseline", revision=(baseline or {}).get("workspaceRevision"))
            delta = self._candidate_delta(comparison_base, manifest)
            if delta and (unit := row.get("unit")):
                delta["files"].sort(key=lambda f: (f["path"] not in unit["fingerprints"], f["path"]))
            handoff = self._handoff_text(row, delta)
            if not baseline:
                handoff += "\n未保留完整工程初始基线。本候选可核对封存源码及已有回执，整仓累计差异无基线可比较。"
            handoff += "\n工程背景：" + json.dumps(self.task_brief(), ensure_ascii=False)
            preparation = (self.state.get("reviewPreparation", {}).get(row["resourceId"])
                           or self.state.get("historicalPreparation", {}).get(row["resourceId"]))
            if preparation:
                handoff += "\n提前准备说明（不是本候选通过证据）：" + str(preparation.get("text", ""))[:4000]
            handoff += ("\n可用 review_test_environment 查看本任务环境。自行判断是否复用、补充或新建；"
                        "在 run_project_check 传 environment_id。复用准备不等于复用旧版检查结论。")
            affected = set()
            watched = set(row["unit"]["fingerprints"]) if row.get("unit") else {f["path"] for f in (delta or {}).get("files", [])}

            def prepared(version):
                key, source = (baseline_id, baseline) if version == "baseline" else (row["snapshot"], manifest)
                pinned, omitted, sanitized = self._prepared_source(key, source, row)
                if version == "candidate":
                    affected.update(watched & (omitted | sanitized))
                return pinned

            def populate(version, target):
                selected = baseline if version == "baseline" else manifest
                omitted, sanitized = self._materialize(selected, target, incremental=True)
                if version == "candidate":
                    affected.update(watched & (omitted | sanitized))

            unit_results = self._wait_unit_reviews(row) if row.get("finalRequested") else []
            review_seconds = scoped_roles.review_seconds()
            # Preparation and necessary retests share the real request deadline.
            # Do not silently shrink a stage to a 90-second model+copy window.
            review_until = time.monotonic() + review_seconds
            if self.review_deadline is not None:
                review_until = min(review_until, self.review_deadline)
            conclude_at = review_until - min(REVIEW_CONCLUSION_SECONDS, review_seconds * .15)
            criteria = tuple(row.get("criteria", ()))
            registry, checks = self._review_tools(row, None, delta=delta, check_until=conclude_at,
                                                 manifest=manifest, baseline=baseline, prepared=prepared, populate=populate,
                                                 review_criteria=criteria)
            old_checks = [dict(actionId=rec["actionId"], runId=rec["runId"],
                               verification=rec["state"], processId=rec.get("processId"),
                               usableForCurrentCandidate=rec["actionId"] in checks["passed"] | checks["failed"])
                          for rec in row.get("checks", {}).values() if rec["runId"] != self.session.run_id]
            if old_checks:
                role.role_recovery = dict(role="reviewer", receipts=old_checks[-30:])
            with self.lock:
                earlier = [prior for prior in reversed(self.state["candidates"].values())
                           if prior["id"] != row["id"] and prior["resourceId"] == row["resourceId"]
                           and prior.get("sourcePath") == row.get("sourcePath")]
                previous_issue = next((prior["text"] for prior in earlier if prior.get("verdict") == "issues"), "")
                findings = [finding["text"] for prior in earlier[:3]
                            for finding in prior.get("findings", {}).values()]
                defects = self.work.pending_retests(row) if not row.get("unit") else []
                previous_notes = [dict(version=prior["revision"], verdict=prior["verdict"],
                                       conclusion=prior.get("text", "")[:700])
                                  for prior in earlier if prior.get("verdict")][:4]
            retest = ("\n上一候选的独立发现（仅作证据，请在当前版本复验）：\n" + previous_issue[:4500]
                      if previous_issue else "")
            if findings:
                retest += "\n较早候选已反馈的问题，请在当前版本复验：\n" + "\n".join(findings)[:4500]
            if self.executor_led and previous_notes:
                retest += "\n本任务早期复核的结论与覆盖线索，仅用于缩小本轮检查：\n" + json.dumps(previous_notes, ensure_ascii=False)
            if self.executor_led:
                handoff += ("\n执行者的回执仅供选取范围。优先验证未覆盖的风险和已知缺陷。"
                            "只有为了独立复现必验行为才重复已有检查；已有足够证据时直接给出结论和未覆盖项。"
                            "用上报工具记录有具体证据且影响交付的缺陷，普通建议写在复核结论。")
                handoff += ("\n先看本次增量，再定点补查原缺陷与新增风险；累计差异可用 read_candidate_changes(comparison='baseline')。"
                            "相关测试、配置或共享依赖变化时按需扩大，说明覆盖范围。测试断言变化要检查是否削弱覆盖，"
                            "不能因为生产文件未变就放行；只有说明文档变化时可核对差异，不必自动重跑功能测试。"
                            "旧版本通过仅作线索，不证明本候选通过。阶段说明是工程背景，当前需求才是权威验收要求。")
            if defects:
                role.defect_retest = self.executor_led
                retest = ("\n新候选必须逐条关闭下列缺陷后才能提交 pass。先复验缺陷，再做其他验收。"
                          "源码缺陷需读当前候选的修复行，再用 verify_candidate_code_defect 绑定原 item_id。"
                          "命令缺陷请在当前候选原样复跑记录的命令及材料，由系统绑定原缺陷。"
                          "\n待定点复验：" + json.dumps([self._retest_summary(item) for item in defects[:12]],
                                                            ensure_ascii=False) + retest)
            withheld = (delta or {}).get("withheld", 0)
            if row.get("unit"):
                handoff += "\n你是本验收项的测试子 Agent。独立检查这个行为，其他项目由父测试者汇总。"
                handoff += "\n已关联变更记录：" + json.dumps(row["unit"]["changeIds"], ensure_ascii=False)
                if row["unit"].get("unattributedPaths"):
                    handoff += "\n需单独核对归属的变化：" + json.dumps(row["unit"]["unattributedPaths"], ensure_ascii=False)
            elif unit_results:
                handoff += "\n已核对的验收项与回执，优先做跨项集成验证：" + json.dumps(unit_results, ensure_ascii=False)
            scoped_roles.run(self.root, role, "reviewer", self.task + "\n验证目标：" + row["reason"] + retest +
                             ("\n本候选本次仅验证以下必验项：" + json.dumps(criteria, ensure_ascii=False) if criteria else "") +
                             f"\n候选版本 {row['revision']}。读码直接使用封存源版本，首次运行命令才准备隔离副本。"
                             "\n优先依据交接的版本差异定点验证。当前副本无 Git 元数据，用 read_candidate_changes 读取精确差异。"
                             "先核对最重要的可复现行为。工具提示进入收尾时，根据已有证据立即提交结论和未覆盖项。"
                             "发现明确缺陷要立即用工具上报，别等最后报告。能从源码确定的回归，先用 inspect_code 按行读取，"
                             "再调用 report_candidate_code_defect，引用当前候选版本、具体行和读取回执。即使测试无法运行也可上报。"
                             "实测失败则用 flag_candidate_blocker 引用失败回执并填写 finding。"
                             + handoff,
                             should_stop=self._review_stop, stop_reason=self.stop_reason, tools=registry, report=report,
                             binding_fields=fields, conclude_at=conclude_at, review_limit=review_seconds,
                             structured_review=self.executor_led,
                             check_allowance=2 if self.executor_led else None,
                             qualify_verdict=lambda verdict: "insufficient" if (
                                 verdict == "pass" and (not (checks["passed"] or self.executor_led and checks.get("inspected")) or affected or withheld)
                                 or self.executor_led and verdict == "issues" and not row.get("findings")
                             ) else verdict)
            if self.executor_led and self._review_stop():
                raise Interrupted("复核已停止，保留先前记录")
            self._candidate_resource(row)
            verdict = report.get("verdict") or "insufficient"
            blocking = verdict == "issues" and bool(checks["blocker"])
            report_path = self._report_path(self.session.run_id, row["id"], row["attemptInRun"])
            with self.lock:
                if self.executor_led and self._review_stop():
                    raise Interrupted("复核已停止，保留先前记录")
                atomic_json(report_path, evidence.redact(dict(report, verdict=verdict, blocking=blocking,
                             candidateId=row["id"], candidateRevision=row["revision"], runId=self.session.run_id)))
                verified = self.work.retest(row, accept_checks=verdict == "pass")
                self._review_state(row, "completed", verdict=verdict, text=report["text"], blocking=blocking,
                                   report=report_path.name, **({"coverage": report["coverage"]} if "coverage" in report else {}))
                if verified:
                    self.session.emit("work_queue_verified", items=verified, candidateId=row["id"], revision=row["revision"])
            role.emit("role_phase", role="reviewer", attempt_id=report["attemptId"], state="completed", **fields)
            role.emit("benni", attempt_id=report["attemptId"], ok=True, body=report["text"], reviewVerdict=verdict, **fields)
        except scoped_roles.ReviewTimedOut as exc:
            self._review_state(row, "timed_out", error=str(exc))
        except Interrupted as exc:
            self._review_state(row, "paused", error=str(exc), stopKind=self.stop_reason()['kind'])
        except scoped_roles.RoleWaiting as exc:
            self._review_state(row, "waiting_human", error=str(exc))
        except Exception as exc:
            unavailable = getattr(exc, 'kind', '') == 'recovery_exhausted'
            self._review_state(row, "paused" if unavailable else "failed", error=str(exc)[:800])
            if not getattr(role, "scoped_role_attempts", {}).get("reviewer"):
                role.emit("role_phase", role="reviewer", state="failed", reason=str(exc)[:800], **fields)
        finally:
            self._release_prepared_sources(row)
            reset_call(token)

    @staticmethod
    def _retest_summary(item):
        result = dict(itemId=item["id"], finding=item["text"][:700], originalActionId=item["actionId"])
        if anchor := item.get("codeEvidence"):
            result.update(path=anchor["path"], line=anchor["line"])
        if item.get("argv"):
            result["argv"] = item["argv"]
        if item.get("material"):
            result["material"] = item["material"]
        return result

    # 按剩余时间读取当前单元复核结果，过期或已失效的候选单独处理。
    def _wait_unit_reviews(self, row):
        while not self._review_stop():
            with self.lock:
                units = self._units_for(row["resourceId"], row["sourcePath"])
                if all(unit["state"] not in {"queued", "running"} for unit in units):
                    return [dict(itemId=unit["unitId"], revision=unit["revision"], verdict=unit.get("verdict"),
                                 state=unit["state"], report=unit.get("text", "")[:1200],
                                 checks=[r["actionId"] for r in unit.get("checks", {}).values()
                                         if r["state"] == "passed" and r.get("sourceVersion", "candidate") == "candidate"])
                            for unit in units]
            time.sleep(.05)
        raise Interrupted("等待验收项复核时已停止")

    def _prepared_source(self, key, manifest, row):
        if not key or manifest is None:
            raise W.Problem("baseline_missing", "没有可执行的初始基线", 404)
        with self.source_cache_lock:
            if key not in self.source_cache or not self.source_leases.get(key):
                started = time.monotonic()
                from .prepared_sources import cache_path, load_or_prepare
                binding = W.digest(json.dumps([key, manifest, str(cache_path(self.session.dir)[0]), "short-v1"],
                                              sort_keys=True).encode())
                with self.lock:
                    blocked = self.state.get("preparationBlocks", {}).get(binding)
                if blocked:
                    raise W.Problem("review_source_unavailable", blocked["message"], 409)
                try:
                    self.source_cache[key] = load_or_prepare(self, key, manifest, row)
                except OSError as error:
                    message = "候选源准备失败，检查命令尚未启动：" + str(error)[:600]
                    if getattr(error, "winerror", None) in {123, 206}:
                        with self.lock:
                            blocks = self.state.setdefault("preparationBlocks", {})
                            blocks[binding] = dict(message=message, snapshotId=key, winerror=error.winerror)
                            while len(blocks) > 32:
                                del blocks[next(iter(blocks))]
                            self.save()
                    raise W.Problem("review_source_unavailable", message, 409) from error
                directory, pinned, omitted, sanitized = self.source_cache[key]
                self.session.emit("review_snapshot_ready", candidateId=row["id"], snapshotId=key,
                                  elapsedMs=round((time.monotonic() - started) * 1000),
                                  omitted=len(omitted), sanitized=len(sanitized))
            self.source_leases.setdefault(key, set()).add(row["id"])
            return self.source_cache[key][1:]

    def _release_prepared_sources(self, row):
        with self.source_cache_lock:
            for leases in self.source_leases.values():
                leases.discard(row["id"])

    # 按两份候选清单的文件内容比较变更，保留无法展示的数量。
    @staticmethod
    def _candidate_delta(baseline, candidate):
        if baseline is None:
            return None
        a, b = baseline["files"], candidate["files"]
        files, withheld = [], 0
        for path in sorted(set(a) | set(b)):
            before, after = a.get(path, ABSENT), b.get(path, ABSENT)
            if before == after:
                continue
            if _excluded(path):
                withheld += 1
                continue
            kinds = {before["kind"], after["kind"]}
            status = ("unknown" if not kinds <= {"text", "binary", "absent"} else
                      "added" if before["kind"] == "absent" else
                      "deleted" if after["kind"] == "absent" else "modified")
            files.append(dict(path=path, before=before, after=after, status=status))
        return dict(revision=candidate["workspaceRevision"], files=files, withheld=withheld)

    # 将候选上下文转成复核角色可读的材料，带上原版本与来源。
    @staticmethod
    def _handoff_text(row, delta):
        handoff = row.get("handoff") or {}
        paths = [] if delta is None else [
            dict(index=i, path=f["path"], status=f["status"]) for i, f in enumerate(delta["files"][:30])]
        previews = []
        for entry in paths[:3]:
            f = delta["files"][entry["index"]]
            if sum(f[side].get("bytes", 0) for side in ("before", "after")) > 128 * 1024:
                continue
            try:
                page = H.read_file_page(delta, entry["index"], delta["revision"], redact=leaks.redact_source)
                previews.append(dict(index=entry["index"], diff=page["text"][:1100]))
            except W.Problem:
                continue
        return "\n交接材料（资料和待核对线索，不能代替独立验证）：\n" + json.dumps(evidence.redact(dict(
            currentRequirements=handoff.get("currentRequirements"),
            reusableMaterials=handoff.get("reusableMaterials", []),
            comparison=row.get("comparison"),
            previousInvalidation=row.get("invalidatedReport"),
            baseline=handoff.get("baseline"), changeCount=None if delta is None else len(delta["files"]),
            withheldChanges=(delta or {}).get("withheld", 0),
            changes=paths, diffPreviews=previews, implementationIntent=handoff.get("intent"),
            executorSelfChecks=handoff.get("selfChecks", []),
            notice="差异包含期间观察到的变化，归属需核实。基线为空表示旧记录缺失。"
                   "matchesCandidate=false 的自检仅作历史参考。可通过 read_executor_self_check 核对原回执。",
        )), ensure_ascii=False)

    # 按冻结的清单准备复核副本，增量路径只沿可复用的原副本。
    def _materialize(self, manifest, pinned, *, incremental=False):
        from .protected_copy import current, remove
        state = current.get()
        previous = state.get('manifest', {}) if state is not None else {}
        entries = manifest['files']
        changed = {name for name in entries.keys() | previous.keys() if entries.get(name) != previous.get(name)}
        omitted, sanitized = set(), set()
        if state is not None:
            omitted = set(state.get('omitted', ())) - changed
            sanitized = set(state.get('sanitized', ())) - changed
        retained = set()
        # Protected leases use a manifest delta. Untrusted writable callers
        # still require validation; their output is never a trusted delta base.
        pinned = Path(pinned)
        if pinned.is_symlink() or getattr(pinned, "is_junction", lambda: False)():
            raise W.Problem("cache_changed", "验证根目录包含重定向", 409)
        pinned = pinned.resolve()
        object_root = (W.home() / "change-objects").resolve()
        directories = {pinned}
        cache = dict(getattr(self, "materialization_cache", {}))
        cache_bytes = sum(len(v or b"") for v in cache.values())

        def directory(path):
            if path in directories:
                return
            directory(path.parent)
            if path.is_file():
                path.unlink()
            path.mkdir(exist_ok=True)
            directories.add(path)
        if incremental and state is None:
            # The previous process family has exited. Never follow a test-created link.
            for path in pinned.rglob("*"):
                info = path.lstat()
                if (path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                        or getattr(info,"st_file_attributes",0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                        or stat.S_ISREG(info.st_mode) and info.st_nlink>1):
                    raise W.Problem("cache_changed", "验证区包含重定向，隔离并重新构建", 409)
        stopped = current_stop() or self.stop
        if state is not None:
            for relative in sorted(changed & state['files'].keys(), key=lambda p: len(Path(p).parts), reverse=True):
                remove(pinned, review_units.relative_path(relative), prune_empty=True)
                state['files'].pop(relative, None)
        for relative, info in entries.items():
            if state is not None and relative not in changed:
                continue
            if self._review_stop() or stopped():
                raise W.Problem("sandbox_cancelled", "候选准备已停止", 409)
            if info["kind"] == "absent":
                continue
            if info["kind"] not in {"text", "binary"} or _excluded(relative):
                omitted.add(relative)
                continue
            raw = H.object_path(info["hash"], root=object_root).read_bytes()
            if W.digest(raw) != info["hash"]:
                raise W.Problem("history_changed", "候选文件证据已改变", 409)
            if info["kind"] == "text":
                key = (info["hash"], Path(relative).suffix.lower(), id(leaks.has_secret), id(leaks.candidate_python))
                if key not in cache:
                    text = raw.decode("utf8")
                    safe = text
                    if leaks.has_secret(text):
                        safe = leaks.candidate_python(text) if key[1] == ".py" else None
                    processed = None if safe is None else safe.encode("utf8")
                    # Bounded processing cache, never a substitute for checking
                    # the retained object's bytes against its hash above.
                    if len(cache) >= 10000 or cache_bytes + len(raw) > 64 * 1024 * 1024:
                        cache.clear()
                        cache_bytes = 0
                    cache[key] = processed
                    cache_bytes += len(processed or b"")
                processed = cache[key]
                if processed is None:
                    omitted.add(relative)
                    continue
                if processed != raw:
                    sanitized.add(relative)
                    raw = processed
            relative = review_units.relative_path(relative)
            target = pinned / relative
            if target.is_dir():
                import shutil
                shutil.rmtree(target)
            directory(target.parent)
            if state is not None or not incremental or not target.is_file() or W.digest(target.read_bytes()) != W.digest(raw):
                target.write_bytes(raw)
            retained.add(relative)
            if state is not None:
                state['files'][relative] = len(raw)
        if incremental and state is None:
            for target in sorted(pinned.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if target.is_file() and target.relative_to(pinned).as_posix() not in retained:
                    target.unlink()
                elif target.is_dir() and not any(target.iterdir()):
                    target.rmdir()
        if state is not None:
            state.update(manifest={name: dict(info) for name, info in entries.items()},
                         omitted=omitted, sanitized=sanitized)
        self.materialization_cache = cache
        return omitted, sanitized

    # 把复核发现、原动作和签名登记为可处理条目，原证据仍使用候选修订。
    def _record_finding(self, row, key, action, finding, signature, *, argv=None, code=None, material=None,
                        existing_item_id=None):
        if not isinstance(finding, str) or not finding.strip() or len(finding) > 2000:
            raise W.Problem("invalid_finding", "请简述具体缺陷和受影响的行为", 400)
        text = evidence.redact(finding.strip())
        with self.lock:
            if self.executor_led and self._review_stop():
                raise W.Problem("cancelled", "独立复核已经结束", 409)
            findings = row.setdefault("findings", {})
            if existing := findings.get(key, {}).get("workItemId"):
                from .assistant_patches import task_scope
                task_scope(self, row, existing, code)
                return existing
            try:
                item_id, created = self.work.defect(row, action, text, signature, argv, code=code, material=material,
                                                    existing_item_id=existing_item_id)
            except QueueConflict as exc:
                raise W.Problem(exc.code, str(exc), 409) from None
            if existing_item_id is not None or any(item.get("workItemId") == item_id for item in findings.values()):
                from .assistant_patches import task_scope
                task_scope(self, row, item_id, code)
                self.save()
                return item_id
            if self.executor_led:
                item = self.work.state["items"][item_id]
                if item.get("lastObservedRevision", item["revision"]) != row["revision"]:
                    item.update(state="pending", lastObservedRevision=row["revision"], lastObservedActionId=action)
                    # New, validated failing evidence reopens repair ownership;
                    # merely scheduling a new candidate must not do so.
                    if item.get("repair", {}).get("state") == "awaiting_verification":
                        item["repair"].update(state="interrupted", failedCandidateId=row["id"],
                                              failedActionId=action)
                    if handling := item.pop("handling", None):
                        item["previousHandling"] = handling
            if code:
                text += f"（只读证据 {code['path']}:{code['line']}）"
            findings[key] = dict(text=text, workItemId=item_id, actionId=action)
            from .assistant_patches import task_scope
            task_scope(self, row, item_id, code)
            self.save()
            self.session.emit("review_finding", candidateId=row["id"], revision=row["revision"],
                              actionId=action, workItemId=item_id, evidenceKind="code" if code else "check")
            if created:
                self.session.emit("work_queue_defect", itemId=item_id, candidateId=row["id"], actionId=action)
                self._schedule_work_plan()
            return item_id

    # 挑出环境、材料或版本已经无法继续核对的历史检查回执。
    def _invalid_history(self, row):
        from . import artifacts

        invalid_history = set()
        receipts = [rec for rec in row.get("checks", {}).values()
                    if rec.get("runId") != self.session.run_id and rec.get("state") in {"passed", "failed"}]
        environment = C.check_environment() if receipts else None
        for receipt in receipts:
            try:
                process = W.process_record(receipt["processId"])
                log = W.record_path(receipt["processId"]).with_suffix(".log")
                output_size = log.stat().st_size if log.is_file() else 0
                if (receipt.get("environment") != environment
                        or process.get("environmentId")
                        or not log.is_file() and receipt.get("outputBytes") != 0
                        or receipt.get("outputBytes") is not None and output_size != receipt["outputBytes"]
                        or process.get("revision") != receipt.get("revision")
                        or process.get("state") not in {"completed", "failed"}
                        or (process.get("exitCode") == 0) != (receipt["state"] == "passed")
                        or receipt.get("retainOutput") and not process.get("retainedOutput", {}).get("versionId")):
                    raise ValueError("historical check is no longer verifiable")
                C.output_page(Path(self.session.dir).name, process["id"])
                if receipt.get("retainOutput"):
                    retained = process["retainedOutput"]
                    artifacts.read_version(Path(self.session.dir).name, retained["artifactId"], retained["versionId"])
            except (W.Problem, OSError, KeyError, ValueError):
                invalid_history.add(receipt.get("actionId"))
        return invalid_history

    # 为当前候选提供有限的只读查看、检查和提交结论工具；每次调用核对候选范围。
    def _review_tools(self, row, pinned, *, delta=None, check_until=None, manifest=None, baseline=None, prepared=None,
                      review_criteria=None, populate=None, repair_only=False):
        from . import artifacts, check_material

        registry = Registry()
        base = scoped_roles.registry(self.root, role="reviewer")
        journal = row.setdefault("checks", {})
        invalid_history = self._invalid_history(row)
        candidate_checks = [r for r in journal.values() if r.get("sourceVersion", "candidate") == "candidate"
                            and r.get("actionId") not in invalid_history]
        checks = dict(failed={r["actionId"] for r in candidate_checks if r["state"] == "failed"},
                      passed={r["actionId"] for r in candidate_checks if r["state"] == "passed"}, blocker=None)
        processes = {r["processId"] for r in journal.values() if r.get("processId")
                     and r["state"] in {"passed", "failed", "timed_out", "output_unavailable"}}
        code_reads = {}
        checks["inspected"] = code_reads
        files = {path: meta for path, meta in manifest["files"].items() if not _excluded(path)} if manifest else None

        def resource(resource_id, capability=None):
            self._ensure_running()
            if row.get("repairGrantId"):
                from .assistant_patches import _grant
                _grant(self, row["repairGrantId"], row)
            if self.executor_led and self._review_stop():
                raise W.Problem("cancelled", "独立复核已经结束", 409)
            if resource_id != row["resourceId"]:
                raise W.Problem("candidate_scope", "复核工具只能访问指定候选", 403)
            return self._candidate_resource(row, capability)

        def current_requirements(after=0):
            resource(row["resourceId"])
            from .executor_workflow import requirements
            return requirements(self, after)

        registry.add(Tool("read_task_requirements", "分页取回完整当前需求、补充和用户答复，避免摘要遗漏验收项。",
                          dict(type="object", properties=dict(after=dict(type="integer"))),
                          current_requirements, zone="offflow", kind="read", readonly=True))

        # 以候选快照和授权资源提供只读文件内容，保留原定位。
        def inspect(resource_id, relative_path=".", **args):
            resource(resource_id)
            if files is not None:
                path = "" if relative_path == "." else review_units.relative_path(relative_path)
                if path not in files:
                    prefix = path + "/" if path else ""
                    entries = {}
                    for name in files:
                        if name.startswith(prefix):
                            child, separator, _ = name[len(prefix):].partition("/")
                            entries[child] = dict(path=prefix + child, name=child, directory=bool(separator))
                    if not entries and path:
                        raise W.Problem("not_found", "路径不在封存候选中", 404)
                    return dict(ok=True, entries=list(entries.values())[:1000], projectRevision=row["revision"])
                page = C._read_code_page(review_units.source_file(files[path], path), args)
            else:
                ws = dict(path=str(pinned))
                target = W.file_path_from_root(pinned, relative_path)
                if target.is_dir():
                    return dict(ok=True, entries=W.files(ws, relative_path), projectRevision=row["revision"])
                page = C._read_code_page(W.file_read(ws, relative_path), args)
                path = target.relative_to(pinned).as_posix()
            if not _excluded(path) and (source := current_call()) and source.get("actionId"):
                code_reads[source["actionId"]] = dict(path=path, revision=page["revision"],
                                                     start=page["startLine"], lines=page["text"].splitlines())
            return dict(page, ok=True, projectRevision=row["revision"],
                        readEvidenceActionId=(current_call() or {}).get("actionId"))

        def code_anchor(expected_revision, evidence_action_id, line):
            resource(row["resourceId"])
            if expected_revision != row["revision"]:
                raise W.Problem("candidate_revision", "请使用当前封存候选的版本", 409)
            read = code_reads.get(evidence_action_id) if isinstance(evidence_action_id, str) else None
            if read is None or type(line) is not int or not 0 <= line - read["start"] < len(read["lines"]):
                raise W.Problem("review_evidence_required", "请引用本次复核 inspect_code 已读取的具体文件行和 actionId", 409)
            offset = line - read["start"]
            if not read["lines"][offset].strip():
                raise W.Problem("review_evidence_required", "请引用包含相关代码的行", 409)
            return dict(path=read["path"], line=line, fileRevision=read["revision"], actionId=evidence_action_id,
                        excerpt=leaks.redact_source("\n".join(read["lines"][max(0, offset - 2):offset + 3]))[:850])

        def report_code(expected_revision, evidence_action_id, line, finding, existing_item_id=None):
            anchor = code_anchor(expected_revision, evidence_action_id, line)
            identity = json.dumps([anchor["path"], anchor["line"], anchor["excerpt"]], ensure_ascii=False)
            signature = "code:" + hashlib.sha256(identity.encode()).hexdigest()
            key = f"{evidence_action_id}-{line}"
            item_id = self._record_finding(row, key, evidence_action_id, finding, signature, code=anchor,
                                           existing_item_id=existing_item_id)
            checks["blocker"] = evidence_action_id
            return dict(ok=True, itemId=item_id, candidateRevision=row["revision"])

        def verify_code(item_id, expected_revision, evidence_action_id, line, conclusion):
            anchor = code_anchor(expected_revision, evidence_action_id, line)
            if not isinstance(conclusion, str) or not conclusion.strip() or len(conclusion) > 1200:
                raise W.Problem("invalid_finding", "请说明新代码如何消除缺陷", 400)
            with self.lock:
                item = next((item for item in self.work.applicable(row) if item["id"] == item_id), None)
                original = item.get("codeEvidence") if item else None
                if not original or item.get("lastObservedRevision", item["revision"]) == row["revision"]:
                    raise W.Problem("candidate_scope", "此缺陷没有绑定到当前候选的复验，或仍是原缺陷版本", 409)
                if (anchor["path"] != original["path"] or anchor["fileRevision"] == original["fileRevision"]
                        or anchor["excerpt"] == original["excerpt"]):
                    raise W.Problem("review_evidence_required", "请引用新候选中原证据文件的已修改代码", 409)
                receipts = row.setdefault("codeVerifications", {})
                if item_id not in receipts:
                    receipts[item_id] = dict(anchor, conclusion=evidence.redact(conclusion.strip()))
                    self.save()
            return dict(ok=True, itemId=item_id, candidateRevision=row["revision"],
                        evidenceKind="code", evidenceActionId=receipts[item_id]["actionId"])

        def search(resource_id, **args):
            resource(resource_id)
            if files is not None:
                path = args.get("relative_path", ".")
                path = "" if path == "." else review_units.relative_path(path)
                return review_units.search(files.items(), path, args.get("query"), args.get("limit", 20),
                                           args.get("offset", 0), current_stop() or self.stop)
            return C._search_code(dict(path=str(pinned)), args, current_stop() or self.stop)

        def candidate_changes(resource_id, file_index=None, after=0, comparison="latest"):
            resource(resource_id)
            if comparison not in {"latest", "baseline"}:
                raise W.Problem("invalid_arguments", "comparison 使用 latest 或 baseline")
            selected_delta = self._candidate_delta(baseline, manifest) if comparison == "baseline" and manifest else delta
            if selected_delta is None:
                raise W.Problem("baseline_missing", "该候选没有已保留的初始基线", 404)
            if file_index is not None:
                page = H.read_file_page(selected_delta, file_index, selected_delta["revision"], after=after, redact=leaks.redact_source)
                return dict(ok=True, **page)
            if type(after) is not int or not 0 <= after <= len(selected_delta["files"]):
                raise W.Problem("invalid_cursor", "候选差异分页游标无效")
            end = min(after + 50, len(selected_delta["files"]))
            focus = row.get("unit", {}).get("fingerprints", {})
            return dict(ok=True, files=[dict(index=i, path=f["path"], status=f["status"],
                                            **({"inAcceptanceScope": f["path"] in focus} if focus else {}))
                                       for i, f in enumerate(selected_delta["files"][after:end], after)],
                        comparison="initial_baseline" if comparison == "baseline" else row.get("comparison", {}).get("kind", "initial_baseline"),
                        next=end, hasMore=end < len(selected_delta["files"]), withheld=selected_delta["withheld"])

        def executor_check(resource_id, action_id, after=0, evidence_revision=None):
            resource(resource_id, "execute")
            receipt = next((item for item in row.get("handoff", {}).get("selfChecks", [])
                            if item.get("actionId") == action_id and item.get("runId")), None)
            if not receipt or not isinstance(action_id, str):
                raise W.Problem("candidate_scope", "这条自检回执未交给当前候选", 403)
            return evidence.tool_read(self.root, action_id, receipt["runId"], after, evidence_revision)

        def fixture(resource_id, title, text):
            thread, _, _, source = resource(resource_id, "execute")
            if not isinstance(text, str) or not 0 < len(text.encode("utf-8")) <= check_material.MAX_BYTES:
                raise W.Problem("invalid_arguments", "复现材料须为非空 UTF-8 文本且不超过 256 KiB")
            digest = W.digest(text.encode("utf8"))
            with self.lock:
                materials = self.state.setdefault("reviewMaterials", {}).setdefault(resource_id, [])
                prior = next((m for m in materials if m["hash"] == digest), None)
            if prior:
                # Revalidate current artifact access instead of trusting the cache.
                check_material.prepare(thread["id"], dict(material=prior["material"]))
                return dict(ok=True, material=prior["material"], reused=True)
            saved = artifacts.publish(thread["id"], dict(title=title, format="text", text=text),
                                      dict(source, candidateId=row["id"], codeRevision=row["revision"],
                                           logicalTaskId=self.task_input["id"], demandRevision=self.task_input["revision"]))
            material_ref = dict(artifact_id=saved["id"], version_id=saved["version"]["id"])
            with self.lock:
                materials.append(dict(hash=digest, title=evidence.redact(title), material=material_ref,
                                      sourceRevision=row["revision"], candidateId=row["id"], sourcePath=row.get("sourcePath"),
                                      **self._task_binding()))
                del materials[:-32]
                self.save()
            return dict(ok=True, material=material_ref)

        def check(resource_id, argv, expected_revision=None, timeout=30, capture_changes=False, source_version="candidate",
                  material=None, retain_output=None, environment_id=None, purpose="acceptance", scope_reason=None,
                  writable_copy=False):
            if type(writable_copy) is not bool:
                raise W.Problem("invalid_arguments", "writable_copy 必须为布尔值")
            if purpose not in {"acceptance", "exploration"} or (scope_reason is not None and not isinstance(scope_reason, str)):
                raise W.Problem("invalid_arguments", "请使用有效检查用途和范围说明")
            stopped = current_stop() or self.stop
            while not self.check_slot.acquire(timeout=.05):
                if stopped() or check_until is not None and time.monotonic() + 12 >= check_until:
                    return dict(ok=False, code="review_conclusion_due",
                                error="检查并发槽在结论窗口前不可用",
                                verification=dict(status="not_run", revision=row["revision"]))
            try:
                return run_check(resource_id, argv, expected_revision, timeout, capture_changes, source_version,
                                 material, retain_output, environment_id, writable_copy)
            finally:
                self.check_slot.release()

        def missing_output(verification, name):
            verification["commandStatus"] = "passed"
            return dict(code="output_unavailable", summary=f"命令已通过，但未保存 {name}。如需交付该产物，请先确认脚本会生成文件。")

        # 在冻结复核材料上调度受控检查，实际进程和结果进入本候选的证据。
        def run_check(resource_id, argv, expected_revision, timeout, capture_changes, source_version,
                      material, retain_output, environment_id, writable_copy):
            from . import subtasks

            thread, _, _, source = resource(resource_id, "execute")
            if capture_changes is not False:
                raise W.Problem("policy_denied", "独立复核只可在候选副本执行检查", 403)
            if environment_id is not None and (not self.executor_led or not isinstance(environment_id, str)
                    or environment_id != "new" and not re.fullmatch("[0-9a-f]{32}", environment_id)):
                raise W.Problem("invalid_arguments", "environment_id 使用 new 或列出的环境编号")
            if expected_revision is not None and expected_revision != row["revision"]:
                raise W.Problem("revision_conflict", "请使用候选版本的 projectRevision", 409)
            if source_version not in {"candidate", "baseline"}:
                raise W.Problem("invalid_review_version", "检查版本应为 candidate 或 baseline", 400)
            if source_version == "baseline" and (baseline is None or prepared is None):
                raise W.Problem("baseline_missing", "没有可执行的初始基线", 404)
            on_candidate = source_version == "candidate"
            tested_revision = row["revision"] if on_candidate else baseline["workspaceRevision"]
            if type(timeout) is not int or not 1 <= timeout <= 120:
                raise W.Problem("invalid_timeout", "检查时限需为 1–120 秒")
            if (not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv)
                    or not argv[0].strip() or argv[0].lstrip().startswith("-")):
                raise W.Problem("invalid_command", "argv 第一项必须是可执行程序，其余项才是参数", 400)
            wrap_up = "复核已进入结论窗口。请根据现有证据提交独立意见，明确未覆盖范围。"
            if check_until is not None and time.monotonic() + 12 >= check_until:
                return dict(ok=False, code="review_conclusion_due", error=wrap_up,
                            verification=dict(status="not_run", revision=tested_revision))

            environment = None
            def authorize():
                if check_until is not None and time.monotonic() >= check_until:
                    raise W.Problem("sandbox_cancelled", wrap_up, 409)
                thread, _, original, _ = resource(resource_id, "execute")
                subtasks.command_policy(thread["id"], argv)
                allowed, why = Policy("workspace", root=Path(original["path"])).check("run_check", {"cmd": argv}, kind="exec")
                if not allowed:
                    raise W.Problem("policy_denied", why, 403)
                if environment is not None and C.check_environment() != environment:
                    raise W.Problem("sandbox_runtime", "复核准备期间运行时依赖变化，未启动命令", 409)

            authorize()
            material_args = {k: v for k, v in dict(material=material, retain_output=retain_output).items() if v is not None}
            if material_args and not self.executor_led:
                raise W.Problem("invalid_arguments", "此复核模式不支持隔离验证材料")
            data, retained = check_material.prepare(thread["id"], material_args)
            check_key = [argv, material] if material is not None else argv
            signature = hashlib.sha256(json.dumps(check_key, ensure_ascii=False, sort_keys=True).encode("utf8")).hexdigest()
            # Source permissions are part of execution evidence, not an implicit
            # side effect of attaching a probe or registering a repair.
            signature += ":writable" if writable_copy else ":readonly"
            if environment_id is not None:
                # Reusing preparation never silently reuses a command result.
                signature += ":" + (current_call() or {}).get("actionId", str(time.monotonic_ns()))
            environment = C.check_environment() if self.executor_led else None
            if not on_candidate:
                signature = "baseline:" + signature
            with self.lock:
                previous = journal.get(signature)
                if previous and previous.get("actionId") in invalid_history:
                    previous = None
                if previous and (previous["state"] in {"started", "unknown"} or (
                                 previous.get("environment") == environment
                                 and previous.get("retainOutput") == retained
                                 and (previous["state"] not in {"blocked", "timed_out", "cancelled", "output_unavailable"}
                                 or previous["runId"] == self.session.run_id
                                 and previous.get("attempt", row["attempts"]) == row["attempts"]))):
                    outcome = "unknown" if previous["state"] == "started" else previous["state"]
                    if on_candidate and outcome in {"passed", "failed"}:
                        checks[outcome].add(previous["actionId"])
                    verification = dict(status=outcome, revision=tested_revision, actionId=previous["actionId"])
                    detail = missing_output(verification, previous["retainOutput"]) if outcome == "output_unavailable" else {}
                    return dict(ok=outcome == "passed", reused=True,
                                verification=verification, receipt=previous, **detail)
                receipt = dict(actionId=source["actionId"], runId=self.session.run_id, attempt=row["attempts"],
                               state="started", sourceVersion=source_version, revision=tested_revision,
                               at=time.time(),
                               sourceMode="writable_copy" if writable_copy else "protected")
                if sum(len(arg) for arg in argv) <= 1800 and evidence.redact(argv) == argv:
                    receipt["argv"] = argv
                if self.executor_led:
                    receipt.update(environment=environment, material=material, retainOutput=retained)
                journal[signature] = receipt
                self.save()
            stopped = current_stop() or self.stop
            def stop_check():
                return stopped() or check_until is not None and time.monotonic() >= check_until

            prior_stop = set_stop(stop_check)
            try:
                hooks = {}
                if self.executor_led and manifest is not None:
                    selected = manifest if on_candidate else baseline
                    target = Path(row["sourcePath"])
                    hooks["populate_workspace"] = lambda path: (populate(source_version, path) if populate else
                                                               self._materialize(selected, path, incremental=True))
                    hooks["workspace_scope"] = self.verification_scope(row["resourceId"])
                    if environment_id is not None:
                        hooks["environment_id"] = environment_id
                else:
                    target = prepared(source_version) if prepared else pinned
                if material_args:
                    hooks["stage_material"] = lambda path: check_material.stage(path, data)
                if retained:
                    output_source = dict(source, candidateId=row["id"], codeRevision=tested_revision, material=material,
                                         logicalTaskId=self.task_input["id"], demandRevision=self.task_input["revision"])
                    hooks["retain_output"] = lambda path: check_material.collect(thread["id"], path, retained, output_source)
                process = W.launch(dict(path=str(target), id=row["id"]), argv, source=source,
                                   check=authorize, force_isolated=True, pinned_revision=tested_revision,
                                   readonly_source=not writable_copy, **hooks)
            except W.Problem as exc:
                blocked = {"sandbox_runtime", "sandbox_unavailable", "sandbox_transport", "unsupported_runtime",
                           "review_source_unavailable", "cache_capacity", "cache_disk_full", "cache_changed"}
                if exc.code in blocked | {"sandbox_cancelled", "cancelled"}:
                    outcome = "blocked" if exc.code in blocked else "cancelled"
                    with self.lock:
                        receipt["state"] = outcome
                        self.save()
                    return dict(ok=False, code=exc.code, error=exc.message,
                                verification=dict(status=outcome, revision=tested_revision))
                if self.executor_led:
                    with self.lock:
                        receipt["state"] = "blocked"
                        self.save()
                raise
            finally:
                set_stop(prior_stop)
            processes.add(process["id"])
            with self.lock:
                receipt["processId"] = process["id"]
                self.save()
            deadline = time.monotonic() + timeout
            if check_until is not None:
                deadline = min(deadline, check_until)
            while (current := W.process_record(process["id"]))["state"] not in TERMINAL_PROCESS:
                revoked = False
                if row.get("repairGrantId"):
                    from .assistant_patches import _grant
                    try:
                        _grant(self, row["repairGrantId"], row)
                    except W.Problem:
                        revoked = True
                if stopped() or revoked or time.monotonic() >= deadline:
                    W.stop_process(process["id"])
                    break
                time.sleep(.05)
            settle = time.monotonic() + 3
            while (current := W.process_record(process["id"]))["state"] not in TERMINAL_PROCESS and time.monotonic() < settle:
                time.sleep(.05)
            outcome = ("unknown" if current["state"] not in {"completed", "failed", "interrupted"} else
                       "timed_out" if stopped() or time.monotonic() >= deadline else
                       "passed" if current["state"] == "completed" and current["exitCode"] == 0 else
                       "failed" if current["state"] == "failed" else "unknown")
            if retained and outcome == "passed" and not (current.get("retainedOutput") or {}).get("versionId"):
                outcome = "output_unavailable"
            if on_candidate and outcome == "failed":
                checks["failed"].add(source["actionId"])
            elif on_candidate and outcome == "passed":
                checks["passed"].add(source["actionId"])
            self._candidate_resource(row, "execute")
            output = C.output_page(Path(self.session.dir).name, process["id"])
            verification = dict(status=outcome, revision=tested_revision, sourceVersion=source_version,
                                actionId=receipt["actionId"])
            if outcome == "output_unavailable":
                output.update(missing_output(verification, retained))
            elif outcome != "passed":
                output["diagnostics"] = C.check_diagnostics(output)
                output["reviewGuidance"] = ("失败已保留到项目检查日志，尚未归因为产品缺陷。"
                    "先区分探针/环境问题与产品偏差；确认偏差后下一批优先 flag_candidate_blocker，"
                    "引用 verification.actionId，再定位修复。探针入口反复出错时回到已有成功入口。")
            with self.lock:
                receipt["state"] = outcome
                log = W.record_path(process["id"]).with_suffix(".log")
                receipt["outputBytes"] = log.stat().st_size if log.is_file() else 0
                self.save()
            if check_until is not None and time.monotonic() >= check_until:
                output["summary"] = (output.get("summary", "") + " " + wrap_up).strip()
            return dict(C.check_tool_result(output), ok=outcome == "passed", verification=verification)

        # 按检查身份和版本读取当前候选已有日志，不触发执行。
        def check_output(resource_id, process_id, after=None, query=None):
            resource(resource_id, "execute")
            preparation = self.state.get("reviewPreparation", {}).get(resource_id, {})
            processes.update(rec["processId"] for rec in preparation.get("checks", {}).values() if rec.get("processId"))
            repair_processes = set()
            from .assistant_patches import _grant
            for grant in list(self.state.get("assistantRepairs", {}).get("grants", {}).values()):
                try:
                    _grant(self, grant["id"], row)
                except W.Problem:
                    continue
                for review in grant.get("drafts", {}).get(row["id"], {}).get("checks", {}).values():
                    repair_processes.update(rec["processId"] for rec in review.get("checks", {}).values() if rec.get("processId"))
            if not isinstance(process_id, str) or process_id not in processes | repair_processes:
                raise W.Problem("candidate_scope", "这条进程回执不属于本次候选测试", 403)
            if query is not None:
                if after is not None:
                    raise W.Problem("invalid_arguments", "日志查询与定点偏移需分别使用")
                result = C.search_check_output(W.process_record(process_id), query, current_stop() or self.stop)
            else:
                result = dict(C.output_page(Path(self.session.dir).name, process_id, 0 if after is None else after), ok=True)
            return dict(result, evidenceKind="repair_self_check") if process_id in repair_processes else result

        def blocker(action_id, finding=None, existing_item_id=None):
            resource(row["resourceId"], "execute")
            if action_id not in checks["failed"]:
                raise W.Problem("review_evidence_required", "请引用本候选中已完成且失败的检查", 409)
            if self.executor_led or finding is not None:
                signature = next((key for key, receipt in journal.items()
                                  if receipt["actionId"] == action_id and receipt["state"] == "failed"), None)
                if signature is None:
                    raise W.Problem("review_evidence_required", "未找到此失败检查的回执", 409)
                item = self._record_finding(row, action_id, action_id, finding, signature, argv=journal[signature].get("argv"),
                                            material=journal[signature].get("material"),
                                            existing_item_id=existing_item_id)
            else:
                item = None
            checks["blocker"] = action_id
            return dict(ok=True, actionId=action_id, itemId=item)

        criteria = {item["id"]: item for item in (row.get("criteria", []) if review_criteria is None else review_criteria)
                    if isinstance(item, dict)}

        def coverage_template():
            return [dict(requirement_id=ident, status='uncovered', action_ids=[], note='请填写实际覆盖或未覆盖原因')
                    for ident in criteria]

        def review_requirements():
            resource(row["resourceId"])
            return dict(ok=True, candidateId=row["id"], candidateRevision=row["revision"],
                        coverageTemplate=coverage_template(),
                        requirements=list(criteria.values()), eligiblePassedActionIds=sorted(checks["passed"]),
                        eligibleFailedActionIds=sorted(checks["failed"]),
                        eligibleInspectionActionIds=sorted(code_reads),
                        historicalReferenceOnly=sorted(action for action in invalid_history if action),
                        notice="提交 coverage 仅使用这里的 id；passed/failed 引用对应检查回执，读日志或登记缺陷不是检查证据。任务蓝图不能替换候选验收编号。")

        # 提交本候选的复核意见和覆盖依据，不能替代独立人工决定。
        def submit(verdict, summary, coverage):
            resource(row["resourceId"])
            if isinstance(coverage, list):
                normalized = []
                for item in coverage:
                    if isinstance(item, dict):
                        item = dict(item)
                        for canonical, alias in (('requirement_id', 'requirementId'), ('action_ids', 'actionIds')):
                            if alias in item:
                                if canonical in item and item[canonical] != item[alias]:
                                    return dict(ok=False, code='review_coverage_required', error='同一字段的两种写法不一致',
                                                coverageTemplate=coverage_template())
                                item[canonical] = item[alias]
                    normalized.append(item)
                coverage = normalized
            if (not isinstance(verdict, str) or verdict not in {"pass", "issues", "insufficient"}
                    or not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 4000):
                raise W.Problem("invalid_review_submission", "请提供有效复核结论及简述")
            if verdict == "pass" and not row.get("unit"):
                with self.lock:
                    pending = self.work.pending_retests(row)
                    if pending:
                        return dict(ok=False, code="defect_retest_required", candidateRevision=row["revision"],
                                    error="先逐条复验并绑定原缺陷；缺证据时提交 insufficient。",
                                    pendingCount=len(pending),
                                    pendingDefects=[self._retest_summary(item) for item in pending[:12]])
            submitted = [i.get("requirement_id") for i in coverage if isinstance(i, dict)] if isinstance(coverage, list) else []
            if (not isinstance(coverage, list) or len(coverage) != len(criteria)
                    or any(not isinstance(i, str) for i in submitted) or len(submitted) != len(coverage)
                    or set(submitted) != set(criteria)):
                return dict(ok=False, code="review_coverage_required", candidateRevision=row["revision"],
                            error="coverage 必须逐项对应当前候选契约；不要提交任务清单中的自定义编号。可调用 read_review_requirements。",
                            expectedRequirementIds=list(criteria), expectedCount=len(criteria),
                            coverageTemplate=coverage_template(),
                            requirements=list(criteria.values()), eligiblePassedActionIds=sorted(checks["passed"]),
                            allowedStatuses=["covered", "failed", "uncovered"])
            validated = {}
            for item in coverage:
                if not isinstance(item, dict) or not isinstance(item.get("requirement_id"), str):
                    raise W.Problem("invalid_review_submission", "必验项记录缺少标识")
                ident = item["requirement_id"]
                status, actions, note = item.get("status"), item.get("action_ids"), item.get("note")
                if (ident not in criteria or ident in validated or not isinstance(status, str)
                        or status not in {"covered", "failed", "uncovered"}
                        or not isinstance(actions, list) or len(actions) > 8
                        or any(not isinstance(action, str) for action in actions)
                        or not isinstance(note, str) or not 1 <= len(note.strip()) <= 1000):
                    return dict(ok=False, code='invalid_review_submission', error='核对必验项、证据编号和具体覆盖说明',
                                coverageTemplate=coverage_template())
                findings = {finding.get("actionId", key.split("-", 1)[0]) for key, finding in row.get("findings", {}).items()}
                allowed = checks["passed"] | set(code_reads) if status == "covered" else checks["failed"] | findings if status == "failed" else set()
                if (status != "uncovered" and not actions) or not set(actions) <= allowed:
                    return dict(ok=False, code="review_evidence_required",
                                error="请只使用本条状态对应的真实候选检查或阅读证据编号；静态审查不能冒充测试通过。",
                                requirementId=ident, status=status, eligibleActionIds=sorted(allowed),
                                coverageTemplate=coverage_template(),
                                rejectedActionIds=[action for action in actions if action not in allowed])
                validated[ident] = dict(requirementId=ident, requirementRevision=criteria[ident]["revision"],
                                        status=status, actionIds=actions, note=evidence.redact(note.strip()),
                                        evidenceKind="inspection" if set(actions) & set(code_reads) else "execution")
            if verdict == "pass" and (not (checks["passed"] or code_reads) or any(item["status"] != "covered" for item in validated.values())):
                raise W.Problem("review_coverage_required", "必验项尚无完整通过证据，请补验或提交证据不足")
            if verdict == "issues" and not row.get("findings"):
                raise W.Problem("review_evidence_required", "先使用缺陷工具登记具体发现")
            return dict(ok=True, verdict=verdict, summary=evidence.redact(summary.strip()), coverage=list(validated.values()))

        for name, adapter in {"inspect_code": inspect, "search_project_code": search,
                              "run_project_check": check, "read_check_output": check_output}.items():
            tool = base.get(name)
            schema, description = tool.parameters, tool.description
            if name == "run_project_check":
                schema = dict(schema, properties=dict(schema.get("properties", {}), source_version=dict(
                    type="string", enum=["candidate", "baseline"])))
                description += " 默认检查候选。要做红绿对照可用 source_version=baseline 在初始封存版本执行同一条命令。"
                schema["properties"]["writable_copy"] = dict(type="boolean", description="默认保护候选源码；确需在源码旁写文件或数据库时显式设 true，仅写隔离副本，不回写主线。")
                description += " 普通套件与探针默认复用受保护源码，输出写临时区；需要源码内写入时选择 writable_copy=true。"
                if self.executor_led:
                    description += " 默认只取检查回执。仅当脚本实际生成 .harness-check/ 下的文件且确实需要保留时才传 retain_output。"
                    schema["properties"]["environment_id"] = dict(type="string", description="new 创建并保留准备目录，或指定已有环境编号复用。是否适用由你决定；命令仍实际执行。")
                    description += " 可用 environment_id 显式保留/复用准备环境；将要复用的文件写到 os.environ['MODOT_TEST_ENV']，检查源码区每次复位，准备目录单独保留。"
                else:
                    for field in ("material", "retain_output"):
                        schema["properties"].pop(field, None)
            registry.add(Tool(name, description, schema, adapter, zone=tool.zone, kind=tool.kind, readonly=tool.readonly))
        for name in ("read_conversation_dialogue", "read_memory_tree"):
            registry.add(base.get(name))
        registry.add(Tool("read_candidate_changes", "默认查看本次增量；comparison=baseline 查看初始基线到当前候选的累计差异。先取列表，再用 file_index 取单文件差异，after 分页。",
                          dict(type="object", properties=dict(resource_id=dict(type="string"), file_index=dict(type="integer"),
                                                               comparison=dict(type="string", enum=["latest", "baseline"]),
                                                               after=dict(type="integer")), required=["resource_id"]),
                          candidate_changes, zone="offflow", kind="read", readonly=True))
        registry.add(Tool("read_executor_self_check", "按交接中的 actionId 核对执行者自检的原回执。续页使用 evidence_revision，历史回执不能代替独立验证。",
                          dict(type="object", properties=dict(resource_id=dict(type="string"), action_id=dict(type="string"),
                                                               after=dict(type="integer"), evidence_revision=dict(type="string")),
                               required=["resource_id", "action_id"]),
                          executor_check, zone="offflow", kind="read", readonly=True))
        registry.add(Tool("flag_candidate_blocker", "标记实测失败并即时反馈。如果已用源码上报同一根因，传 existing_item_id 追加证据。",
                          dict(type="object", properties=dict(action_id=dict(type="string"), finding=dict(type="string"),
                                                              existing_item_id=dict(type="string")),
                               required=["action_id", "finding"] if self.executor_led else ["action_id"]),
                          blocker, zone="offflow", kind="meta"))
        if self.executor_led:
            string = dict(type="string")
            def environment(operation="list", environment_id=None, after=0):
                self._candidate_resource(row, "execute")
                if type(after) is not int or after < 0:
                    raise W.Problem("invalid_arguments", "材料目录游标无效")
                if operation not in {"list", "release"} or operation == "release" and environment_id is None:
                    raise W.Problem("invalid_arguments", "使用 list 查看，或 release 加 environment_id 释放")
                from .verification_workspace import environments
                result = environments(self.verification_scope(row["resourceId"]), environment_id, remove=operation == "release")
                if operation == "list":
                    preparation = self.state.get("reviewPreparation", {}).get(row["resourceId"], {})
                    result["preparationChecks"] = [evidence.redact(rec) for rec in list(preparation.get("checks", {}).values())[-20:]]
                    materials = self.state.get("reviewMaterials", {}).get(row["resourceId"], [])
                    result.update(materials=evidence.redact(materials[after:after + 20]),
                                  next=after + 20 if len(materials) > after + 20 else None,
                                  preparationNote=evidence.redact((preparation or self.state.get("historicalPreparation", {}).get(row["resourceId"], {})).get("text", "")),
                                  notice="历史材料仅供复用选择，不是当前候选通过证据；已结束的进程环境不会自动恢复。")
                return result
            registry.add(Tool("review_test_environment", "查看本任务保留的实际环境与命令记录，或显式释放。结合任务自行判断复用/补充/新建；无自动适配判断。重启或容量清理后原环境可能不存在。",
                              dict(type="object", properties=dict(operation=dict(type="string", enum=["list", "release"]), environment_id=string, after=dict(type="integer"))),
                              environment, zone="offflow", kind="meta"))
            registry.add(Tool("read_review_requirements", "读取当前候选的权威验收编号、完整要求和可用通过证据。coverage 必须与这里逐项对应。",
                              dict(type="object", properties={}), review_requirements,
                              zone="offflow", kind="read", readonly=True))
            registry.add(Tool("submit_candidate_review", "提交结构化复核结论。每条必验项列出编号、状态、真实动作编号和验证行为。"
                              "运行验证引用本候选 run_project_check 的 verification.actionId 且 status=passed；"
                              "文档等可静态核对的要求可引用本候选 inspect_code 的读取回执，note 说明核对依据与未运行范围。"
                              "系统分别记录静态审查与运行验证，静态审查不能冒充运行测试或替代执行者必验检查。"
                              "pass 前必须逐条完成待定点复验的原缺陷，整体覆盖不能替代缺陷回执。"
                              "未覆盖时用 uncovered、空 action_ids 并说明缺口。",
                              dict(type="object", properties=dict(verdict=dict(type="string", enum=["pass","issues","insufficient"]),
                                   summary=string, coverage=dict(type="array", items=dict(type="object",
                                       properties=dict(requirement_id=dict(type="string", **({"enum": list(criteria)} if criteria else {})), status=dict(type="string", enum=["covered","failed","uncovered"]),
                                                       action_ids=dict(type="array", items=string), note=string),
                                       required=["requirement_id","status","action_ids","note"]))),
                                   required=["verdict","summary","coverage"]),
                              submit, zone="offflow", kind="meta"))
            registry.add(Tool("publish_review_fixture", "保存独立复现脚本，返回 run_project_check 的 material 引用。"
                              "脚本只会出现在隔离副本的 .harness-check/input.py。",
                              dict(type="object", properties=dict(resource_id=dict(type="string"), title=dict(type="string"),
                                                                   text=dict(type="string")), required=["resource_id", "title", "text"]),
                              fixture, zone="offflow", kind="meta"))
        anchor_fields = dict(expected_revision=dict(type="string"), evidence_action_id=dict(type="string"),
                             line=dict(type="integer"))
        for name, field, detail, handler in (
            ("report_candidate_code_defect", "finding", "用当前候选的 inspect_code 读取回执和具体行立即上报明确的代码缺陷。", report_code),
            ("verify_candidate_code_defect", "conclusion", "在新候选重读并确认原缺陷消除，引用新读码回执和行，说明修复为何有效。", verify_code),
        ):
            fields = dict(anchor_fields, **{field: dict(type="string")})
            if name == "verify_candidate_code_defect":
                fields["item_id"] = dict(type="string")
            required = list(fields)
            if name == "report_candidate_code_defect":
                fields["existing_item_id"] = dict(type="string")
            registry.add(Tool(name, detail, dict(type="object", properties=fields, required=required),
                              handler, zone="offflow", kind="meta"))
        if self.executor_led and not repair_only and row.get("snapshot"):
            from .assistant_patches import install_reviewer
            install_reviewer(self, row, registry)
        return registry, checks

    def _review_state(self, row, state, **fields):
        with self.lock:
            row.update(state=state, **fields)
            self.save()
        self.session.emit("review_candidate", candidateId=row["id"], revision=row["revision"], state=state,
                          **({"reviewUnitId": row["unitId"]} if row.get("unitId") else {}),
                          **{k: v for k, v in fields.items() if k != "text"})

    # 将已形成的调查与复核事实按实际投递边界交还执行者。
    def feedback(self, msgs, *, validation=False, settling=False):
        if getattr(self, "serial_review", False):
            from .serial_review import feedback
            return feedback(self, msgs)
        from .provider import Msg

        if self.executor_led:
            from .execution_completion import feedback as gap_feedback
            gap_feedback(self, msgs)
            from .assistant_patches import feedback as patch_feedback
            patch_feedback(self, msgs)
        delivery = self.investigation.feedback(msgs) if self.investigation else {}
        if self.executor_led:
            from .executor_workflow import feedback, dispatch
            if not settling:
                dispatch(self)
            feedback(self, msgs)
        # Deliver findings at the next model boundary; verification remains batched.
        with self.lock:
            rows = [dict(row) for row in self.state["candidates"].values() if row["state"] == "completed" and not row.get("delivered")]
            findings = [(dict(row), action_id, dict(item)) for row in self.state["candidates"].values()
                        for action_id, item in row.get("findings", {}).items()
                        if self._finding_needs_delivery(item, row["revision"])]
            order_revision = self.work.state.get("orderRevision", 0)
            order = None
            if order_revision > self.work.state.get("deliveredOrder", 0):
                try:
                    self._check_work_scope()
                    order = self.work.read()
                except W.Problem:
                    pass
        if order:
            marker = f"[work-order:{order_revision}]"
            if not any(marker in msg.content for msg in msgs if isinstance(msg.content, str)):
                lines = [f"{item['id']} {item['text']}" + ("（等待 " + "、".join(item["dependsOn"]) + "）"
                         if not item["ready"] else "")
                         for item in order["items"] if item["state"] not in {"implemented", "verified"}]
                msgs.append(Msg("user", marker + " 规划者已调整工作顺序，请在安全步骤切换，等待依赖的项目先搁置。"
                                "此安排沿用用户目标和授权。\n" + order["reason"] + "\n" + "\n".join(lines)[:6000]))
            delivery[f"work-order:{order_revision}"] = "next_round"
        batch = []
        for row, action_id, item in findings:
            key = f"{row['id']}:{action_id}"
            marker = f"[candidate-finding:{key}]"
            if any(marker in msg.content for msg in msgs if isinstance(msg.content, str)):
                delivery[key] = "next_round"
                continue
            current = self._current_revision(row["resourceId"], row["sourcePath"])
            if current is None:
                delivery[key] = "archived"
                continue
            note = "候选独立复核发现（证据，不扩展授权）"
            if self.executor_led:
                note += (f"。缺陷 {item['workItemId']}。测试者可在当前任务范围内直接修隔离副本，不需你再授权。"
                         "先看 assistant-patches 中的修复进度和补丁，避免双方重复修改；你可以先继续无关工作。"
                         "补丁在下一请求送达，由你核对后合并、调整或拒绝；若自行接手，用 record_review_feedback 说明。"
                         "主目录仍只由你修改，修复自检不代表实际合入版本已通过验收")
            if current != row["revision"]:
                note += "。来自较早候选，请先核对当前版本是否仍有此问题"
            entry = marker + " " + note + "：" + item["text"]
            if self.executor_led:
                batch.append(entry)
            else:
                msgs.append(Msg("user", entry))
            delivery[key] = "next_round"
        if batch:
            msgs.append(Msg("user", "安全步骤边界汇总待处理发现。请按当前版本合批核对和修正，随后验证相关问题。\n"
                            + "\n".join(batch)))
        for row in rows:
            if self.executor_led and row.get("verdict") == "issues" and row.get("findings"):
                delivery[row["id"]] = "next_round"
                continue
            marker = f"[candidate-review:{row['id']}]"
            if any(marker in (msg.content or "") for msg in msgs if isinstance(msg.content, str)):
                delivery[row["id"]] = "next_round"
                continue
            current = self._current(row)
            if (not current and row.get("sourcePath") and row.get("verdict") == "issues"
                    and self._current_revision(row["resourceId"], row["sourcePath"]) is not None):
                msgs.append(Msg("user", marker + " 较早候选的独立发现。请先核对当前版本是否仍受影响：" + row["text"][:6000]))
                delivery[row["id"]] = "next_round"
                continue
            if current:
                msgs.append(Msg("user", marker + " 独立测试结果（证据，不扩展授权）：" + row["text"][:6000]))
            delivery[row["id"]] = "next_round" if current else "archived"
        return delivery

    def _finding_needs_delivery(self, finding, revision):
        if not self.executor_led:
            return not finding.get("delivered")
        item = self.work.state["items"].get(finding.get("workItemId"))
        return bool(item and item["state"] != "verified" and not item.get("handling")
                    and item.get("lastObservedRevision", item["revision"]) == revision
                    and finding.get("deliveredRun") != self.session.run_id)

    # 按记录的投递方式确认反馈进入对应的运行上下文。
    def acknowledge(self, delivery):
        with self.lock:
            for key, mode in delivery.items():
                candidate_id, _, action_id = key.partition(":")
                if candidate_id == "background-plan":
                    self.investigation._find(action_id)["deliveredRun"] = self.session.run_id
                    continue
                if candidate_id == "work-order":
                    self.work.state["deliveredOrder"] = max(int(action_id), self.work.state.get("deliveredOrder", 0))
                    continue
                row = self.state["candidates"][candidate_id]
                target = row["findings"][action_id] if action_id else row
                target.update(delivered=True, delivery=mode)
                if self.executor_led:
                    target["deliveredRun"] = self.session.run_id
            self.save()
        for key, mode in delivery.items():
            candidate_id, _, action_id = key.partition(":")
            if candidate_id == "background-plan":
                self.session.emit("background_plan_delivery", planId=action_id, role="executor", delivery=mode)
                continue
            if candidate_id == "work-order":
                self.session.emit("work_queue_dispatched", queueRevision=int(action_id))
                continue
            self.session.emit("role_feedback", candidateId=candidate_id, delivery=mode,
                              **({"findingActionId": action_id} if action_id else {}))

    def _current_revision(self, resource_id, source_path=None):
        token = set_call(self._identity("executor", "review_feedback"))
        try:
            _, _, ws, _ = self._resource(resource_id)
            if source_path and ws["path"] != source_path:
                return None
            return W.current_revision(ws)
        except (W.Problem, OSError):
            return None
        finally:
            reset_call(token)

    def _current(self, row):
        return bool(row.get("sourcePath")) and self._current_revision(row["resourceId"], row["sourcePath"]) == row["revision"]

    def blocks_write(self, resource_id):
        if self.executor_led:
            return False
        with self.lock:
            rows = [dict(row) for row in self.state["candidates"].values() if row["resourceId"] == resource_id
                    and (row.get("blocking") and not row.get("delivered")
                         or any(not f.get("delivered") for f in row.get("findings", {}).values()))]
        return any(self._current(row) for row in rows)

    # 运行结束时收束已启动的角色与候选，保留待核对和未完成状态交回运行拥有者。
    def finish(self, result):
        if getattr(self, "serial_review", False):
            from .serial_review import finish
            return finish(self, result)
        if self.executor_led:
            with self.lock:
                latest_scopes = {_review_scope(row): key for key, row in self.state["candidates"].items()
                                 if row["state"] != "superseded"}
                if not self.runtime_completion and result["status"] == "success" and any(
                    row["state"] in {"queued", "running"} for key, row in self.state["candidates"].items()
                    if latest_scopes.get(_review_scope(row)) == key
                    and (row["resourceId"] not in self.state["dirty"] or row["revision"] == self.state["dirty"][row["resourceId"]])
                    and (not row.get("unitId") or self.state["reviewUnits"].get(json.dumps(_review_scope(row))) == key)
                ):
                    result.update(status="paused", why="必要复核尚未结束，保留候选和进度")
                self.review_closed.set()
                if self.background_reviewer and not self.state["candidates"] and result["status"] == "success":
                    self.session.emit("role_phase", role="reviewer", state="skipped", skipKind="no_review_candidate",
                                      reason="本轮没有阶段送审、代码必验项或待复验缺陷")
                for row in self.state["candidates"].values():
                    if row["state"] == "queued":
                        self._review_state(row, "paused", error="任务收尾时尚未执行此候选")
        self.planning_closed.set()
        plan_wait_started = time.monotonic()
        self.planning.shutdown(wait=True)
        if self.investigation:
            elapsed = round((time.monotonic() - plan_wait_started) * 1000)
            if elapsed:
                self.session.emit("assistant_wait", role="planner", phase="stop", elapsedMs=elapsed)
            if not self.state["plans"]:
                self.session.emit("role_phase", role="planner", state="skipped", skipKind="no_investigation",
                                  reason="本轮没有需要并行调查的问题")
        if not self.executor_led and result["status"] == "success" and not self.stop():
            token = set_call(self._identity("executor", "publish_review_candidate"))
            try:
                for rid, revision in self.state["dirty"].items():
                    if str(revision).startswith("pending:"):
                        revision = self._current_revision(rid)
                    if not any(r["resourceId"] == rid and r["revision"] == revision and not r.get("unitId")
                               for r in self.state["candidates"].values()):
                        try:
                            self.publish(rid, revision, "执行轮收尾，请独立核对尚未复核的改动。",
                                         intent=result.get("final"))
                        except (W.Problem, OSError, ValueError) as exc:
                            self.session.emit("review_candidate", state="failed", reason=str(exc))
            finally:
                reset_call(token)
        with self.lock:
            pending = self.worker
        self.queue.shutdown(wait=True)
        self.unit_workers.shutdown(wait=True)
        if pending:
            pending.result()
        for future in self.unit_futures.values():
            future.result()
        if self.stop() and result["status"] == "success":
            result.update(status="paused", why="任务已暂停，保留候选与各角色进度")
        with self.lock:
            candidates = list(self.state["candidates"].values())
        current = {(rid, path): self._current_revision(rid, path) for rid, path in
                   {(row["resourceId"], row.get("sourcePath")) for row in candidates} if path}
        archived = {row["id"]: "archived" for row in candidates if row["state"] == "completed" and not row.get("delivered")
                    and row.get("verdict") != "issues"
                    and (revision := current.get((row["resourceId"], row.get("sourcePath")))) and revision != row["revision"]}
        if archived:
            self.acknowledge(archived)
        if not self.runtime_completion and self.background_reviewer and result["status"] == "success" and self.state["dirty"]:
            for rid in self.state["dirty"]:
                latest = next((row for row in reversed(candidates) if row["resourceId"] == rid
                               and not row.get("unitId")
                               and row["revision"] == current.get((rid, row.get("sourcePath")))), None)
                if (not latest or latest.get("verdict") != "pass" or latest["state"] != "completed"
                        or self.work.applicable(latest)
                        or any(unit.get("verdict") != "pass" for unit in self._units_for(rid, latest["sourcePath"]))):
                    result.update(status="paused", why="当前改动尚无完整的独立复核通过结论，保留候选与进度")
                    break
        if result["status"] == "success":
            # Ending the tool loop does not verify the goal or unfinished defects.
            if not self.runtime_completion:
                self.work.finish()
            from .prepared_sources import record_source
            for record in self.state.get("preparedSources", {}).values():
                record_source(self, record).cleanup()
            self.state.pop("preparedSources", None)
            for directory, *_ in self.source_cache.values():
                directory.cleanup()
        self.source_cache.clear()
        self.state.update(state=result["status"], finished=time.time())
        from .verification_workspace import finish_scope
        try:
            finish_scope(Path(self.session.dir).name, self.session.run_id)
        except (OSError, ValueError, W.Problem) as error:
            self.session.emit("verification_cleanup_deferred", reason=str(error)[:300])
        if result["status"] in {"paused", "needs_human", "interrupted"}:
            from .executor_workflow import pause_reason
            self.state["pause"] = pause_reason(result["status"], result.get("why", ""))
        self.save()
