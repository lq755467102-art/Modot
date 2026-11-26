"""Read-only investigations that run alongside the executor."""

# 主要引用方包括 `role_coordination.py`。

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time

from . import code_service as C, evidence, leaks, review_units, scoped_roles, workbench as W
from .filelock import FileLock
from .provider import Interrupted, Msg
from .tools import Registry, Tool, current_call, current_stop, reset_call, set_call
from .work_queue import QueueConflict, WorkQueue


class BackgroundPlanning:
    # 保存运行所有者与受保护路径判定，后续申请、读取和后台调查沿用这两项。
    def __init__(self, owner, excluded):
        self.owner = owner
        self.excluded = excluded

    # 给执行者注册申请、读取和处理调查的工具，规划者使用单独的只读工具集。
    def install(self, registry):
        string = dict(type="string")
        scope = dict(resource_id=string, paths=dict(type="array", items=string),
                     next_write_paths=dict(type="array", items=string,
                         description="本轮接下来准备修改的相对文件。确实没有计划写入时传 []。"),
                     useful_seconds=dict(type="integer", minimum=5, maximum=180))
        required = ["resource_id", "paths", "next_write_paths"]
        self._tool(registry, "request_scout",
                   "把一个具体未知交给规划者只读探查。领取后异步工作，结果会送回，无须确认。",
                   dict(question=string, **scope), ["question", *required], self.request_scout)
        self._tool(registry, "scout_draft_todos",
                   "导入指定 Markdown 草稿的「探查待办」标题下未勾选的清单。给出共用的调查文件范围。",
                   dict(draft_path=string, **scope), ["draft_path", *required], self.scout_draft_todos)
        self._tool(registry, "read_background_plan",
                   "查询调查或取回结论。只在当前决策确实被阻塞时传 wait_seconds（最多 30 秒）。",
                   dict(plan_id=string, wait_seconds=dict(type="integer", minimum=0, maximum=30)),
                   ["plan_id"], self.read)
        if any(not row.get("scout") and row["state"] == "completed" and row.get("proposal")
               and row.get("decision", {}).get("kind") not in {"accept", "reject"}
               for row in self.owner.state["plans"]):
            self._tool(registry, "decide_background_plan",
                       "仅恢复旧轮次里已生成的调序建议。",
                       dict(plan_id=string, decision=dict(type="string", enum=["accept", "defer", "reject"]), reason=string),
                       ["plan_id", "decision", "reason"], self.decide)

    @staticmethod
    def _tool(registry, name, description, fields, required, call, *, readonly=False):
        registry.add(Tool(name, description, dict(type="object", properties=fields, required=required),
                          call, zone="offflow", kind="read" if readonly else "meta", readonly=readonly))

    # 由执行者的资源范围解析实际检出目录和 Git 状态。
    def _checkout(self, resource_id):
        _, _, workspace, _ = self.owner._resource(resource_id)
        git = {}
        for key, args in (("branch", ["symbolic-ref", "--quiet", "HEAD"]),
                          ("head", ["rev-parse", "--verify", "HEAD"])):
            result = W.safe_git(workspace, args, check=False)
            git[key] = result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else None
        return workspace, git

    def _read_file(self, workspace, path):
        target = W.file_path(workspace, path)
        if self.excluded(target.relative_to(Path(workspace["path"])).as_posix()):
            raise W.Problem("resource_denied", "调查不能读取受保护文件", 403)
        return W.file_read(workspace, path)

    # 冻结文件内容及各自修订，连同分支、提交与目录用于后续核对。
    def _snapshot(self, resource_id, paths):
        workspace, git = self._checkout(resource_id)
        files = {path: self._read_file(workspace, path) for path in paths}
        if sum(len(file["text"]) for file in files.values()) > 1024 * 1024:
            raise W.Problem("plan_scope_limit", "调查文件合计不能超过一百万字符，请缩小范围", 413)
        stamp = dict(path=workspace["path"], branch=git["branch"], head=git["head"],
                     files={path: file["revision"] for path, file in files.items()})
        return stamp, files

    # 用 `delivery_review.py` 的必需项、当前需求哈希与蓝图判据共同绑定调查条件。
    def _requirements(self):
        from .delivery_review import code_review_requirements

        owner = self.owner
        items = code_review_requirements(Path(owner.session.dir).name, owner.task_input["id"])
        requirements = {item["id"]: item["revision"] for item in items}
        if material := owner.state.get("effectiveRequirements"):
            requirements["effective-requirements"] = material["contentHash"]
        if blueprint := owner.state.get("blueprint"):
            requirements["blueprint-acceptance"] = hashlib.sha256(blueprint["acceptance"].encode()).hexdigest()
        return requirements

    # 条件一致才允许沿用原结果。普通调查要求文件一致，探查可把之后编辑的文件标成需复核。
    def _current(self, row):
        try:
            if row.get("requirements") != self._requirements():
                return False
            stamp, _ = self._snapshot(row["resourceId"], row["stamp"]["files"])
            if stamp != row["stamp"]:
                if not row.get("scout") or any(stamp[k] != row["stamp"][k] for k in ("path", "branch", "head")):
                    return False
                row["needsRecheck"] = True
            if row.get("draftPath"):
                workspace, _ = self._checkout(row["resourceId"])
                return row["question"] in self._draft_questions(workspace, row["draftPath"])
            return True
        except (OSError, W.Problem):
            return False

    # 归一输入空白后脱敏，以便草稿待办和原问题一致地匹配。
    @staticmethod
    def _question(text):
        return evidence.redact(" ".join(text.split()))

    # 只识别探查标题下未勾选的任务，代码围栏中的示例不参加派发。
    def _draft_questions(self, workspace, path):
        text = self._read_file(workspace, path)["text"]
        level = None
        fence = None
        questions = {}
        for line in text.splitlines():
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if marker:
                run, suffix = marker.groups()
                if fence is None:
                    fence = run
                elif run.startswith(fence) and not suffix.strip():
                    fence = None
                continue
            if fence:
                continue
            heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if heading:
                depth = len(heading[1])
                if level is not None and depth <= level:
                    level = None
                if heading[2] in {"探查待办", "Scout TODOs"}:
                    level = depth
                continue
            if level is not None and (item := re.match(r"^ {0,3}[-*+] \[ \]\s+(.+)", line)):
                question = self._question(item[1])
                if question:
                    questions[question] = None
        return list(questions)

    # 沿用后台调查入口，显式传入后续写入范围用于避开冲突。
    def request_scout(self, question, resource_id, paths, next_write_paths, useful_seconds=90):
        return self.request(question, resource_id, paths, useful_seconds, scout=True,
                            next_write_paths=next_write_paths)

    # 将 Markdown 草稿中的未勾选问题逐项派发，各项错误单独保留。
    def scout_draft_todos(self, draft_path, resource_id, paths, next_write_paths, useful_seconds=90):
        draft_path = review_units.relative_path(draft_path)
        if Path(draft_path).suffix.lower() not in {".md", ".markdown"}:
            raise W.Problem("invalid_scout_draft", "请指定 Markdown 草稿")
        self.owner._ensure_running()
        workspace, _ = self._checkout(resource_id)
        questions = self._draft_questions(workspace, draft_path)
        if len(questions) > 8:
            raise W.Problem("invalid_scout_draft", "一次最多导入 8 条探查待办")
        if any(len(question) > 2000 for question in questions):
            raise W.Problem("invalid_scout_draft", "单条探查待办不能超过 2000 字符")
        investigations = []
        for question in questions:
            try:
                investigations.append(self.request(question, resource_id, paths, useful_seconds,
                                                   scout=True, draft_path=draft_path,
                                                   next_write_paths=next_write_paths))
            except W.Problem as problem:
                investigations.append(dict(ok=False, question=question, code=problem.code, error=str(problem)))
        return dict(ok=all(row["ok"] for row in investigations), investigations=investigations)

    # 执行者工具传入问题、资源和文件列表。
    # 先由 `review_units.py` 规范路径，再在工作区写锁内冻结调查材料；普通只读探查会避开声明即将写入的文件。
    def request(self, question, resource_id, paths, useful_seconds=90, *, scout=False, draft_path=None,
                next_write_paths=None, allow_overlap=False, failure_packet=None):
        owner = self.owner
        owner._ensure_running()
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
            raise W.Problem("invalid_plan_request", "请提供具体调查问题")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 8 or type(useful_seconds) is not int or not 5 <= useful_seconds <= 180:
            raise W.Problem("invalid_plan_request", "请提供 1–8 个文件和 5–180 秒有效期")
        paths = sorted({review_units.relative_path(path) for path in paths})
        if any(self.excluded(path) for path in paths):
            raise W.Problem("resource_denied", "调查不能读取受保护文件", 403)
        workspace, _ = self._checkout(resource_id)
        with FileLock(W.writer_lock_path(workspace["path"]), check=owner._ensure_running):
            omitted = []
            if scout:
                if not isinstance(next_write_paths, list) or len(next_write_paths) > 32:
                    raise W.Problem("invalid_scout_write_scope", "请声明接下来要修改的文件，最多 32 个")
                writes = {W.file_path(workspace, review_units.relative_path(path)) for path in next_write_paths}
                omitted = [path for path in paths if W.file_path(workspace, path) in writes] if not allow_overlap else []
                paths = [path for path in paths if path not in omitted]
                if not paths:
                    return dict(ok=True, state="skipped_overlap", excludedWritePaths=omitted)
            stamp, files = self._snapshot(resource_id, paths)
            if draft_path and (stamp["path"] != workspace["path"]
                               or question not in self._draft_questions(workspace, draft_path)):
                raise W.Problem("scout_draft_changed", "探查待办已变化，请重新读取草稿", 409)
            branch = (stamp["branch"] or "").removeprefix("refs/heads/")
            if not stamp["head"] or not branch or W.protected_code_branch(branch):
                raise W.Problem("planning_branch_not_ready", "先用 prepare_code_branch 准备本轮功能分支，再派发调查", 409)
            requirements = self._requirements()
            with owner.lock:
                if owner._planning_stop():
                    raise W.Problem("cancelled", "调查窗口已关闭", 409)
                owner._check_work_scope()
                queue_revision = owner.work.state["revision"]
                question = self._question(question) if scout else evidence.redact(question.strip())
                identity_stamp = {k: v for k, v in stamp.items() if k != "files"} if scout else stamp
                identity = json.dumps([failure_packet["generation"] if failure_packet else question, resource_id, identity_stamp, requirements,
                                       "scout" if scout else queue_revision],
                                      sort_keys=True, ensure_ascii=False)
                key = hashlib.sha256(identity.encode()).hexdigest()[:24]
                plans = owner.state["plans"]
                for row in plans:
                    if row["id"] == key:
                        self._expire(row)
                        result = dict(self._view(row), reused=True)
                        if scout:
                            result["excludedWritePaths"] = omitted
                        return result
                if len(plans) >= 20:
                    raise W.Problem("plan_capacity", "本任务已达到调查保留上限", 409)
                if not scout and any(row["state"] in {"queued", "running"} for row in plans):
                    raise W.Problem("plan_busy", "已有一个调查在途，请先继续其他工作", 409)
                row = dict(id=key, state="queued", question=question, resourceId=resource_id, stamp=stamp,
                           queueRevision=queue_revision, requirements=requirements, expiresAt=time.time() + useful_seconds,
                           sourceRunId=owner.session.run_id, **owner._task_binding())
                if scout:
                    row.update(scout=True, draftPath=draft_path, usefulSeconds=useful_seconds, expiresAt=None,
                               excludedWritePaths=omitted)
                if failure_packet:
                    row["failurePacket"] = deepcopy(failure_packet)
                plans.append(row)
                self._state(row, "queued")
                owner.planning.submit(self._run, row, files, deepcopy(owner.work.state), owner.work.read())
                return self._view(row)

    def _state(self, row, state, **fields):
        row.update(state=state, **fields)
        self.owner.save()
        self.owner.session.emit("background_plan", planId=row["id"], state=state, role="planner")

    # 依据持久到期时间反映调查是否还能沿用。
    def _expire(self, row):
        if row["state"] in {"completed", "partial"} and ((not row.get("scout") and row["expiresAt"] <= time.time()) or not self._current(row)):
            self._state(row, "expired")

    # 给执行者投影调查状态及有权查看的结果，不返回内部文件材料副本。
    @staticmethod
    def _view(row):
        result = dict(ok=True, planId=row["id"], state=row["state"], expiresAt=row["expiresAt"])
        if row.get("scout"):
            result.update(needsRecheck=bool(row.get("needsRecheck")), sourceVersion=row["stamp"])
        if row["state"] in {"completed", "partial"}:
            result.update(conclusion=row["text"], evidence=row.get("evidence", []))
            if not row.get("scout"):
                result.update(hasOrderProposal=bool(row.get("proposal")), queueRevision=row["queueRevision"])
            if row.get("proposal"):
                result["proposal"] = deepcopy(row["proposal"])
        if row.get("scout"):
            result.update(question=row["question"], source="draft" if row.get("draftPath") else "question",
                          excludedWritePaths=row.get("excludedWritePaths", []))
        if row.get("decision"):
            result["decision"] = row["decision"]
        return result

    # 从当前执行者保存的调查队列查原调查身份。
    def _find(self, plan_id):
        if not isinstance(plan_id, str):
            raise W.Problem("invalid_plan", "请提供调查标识")
        row = next((row for row in self.owner.state["plans"] if row["id"] == plan_id), None)
        if row is None:
            raise W.Problem("plan_not_found", "调查不属于当前任务及需求版本", 404)
        return row

    # 短等已有调查结果，等待期间仍受运行停止和原调查时限约束。
    def read(self, plan_id, wait_seconds=0):
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 30:
            raise W.Problem("invalid_plan_wait", "等待时间须为 0–30 秒")
        start = time.monotonic()
        stop = current_stop()
        waited = False
        try:
            while True:
                self.owner._ensure_running()
                with self.owner.lock:
                    row = self._find(plan_id)
                    self._expire(row)
                    if row["state"] not in {"queued", "running"} or time.monotonic() - start >= wait_seconds:
                        return self._view(row)
                if stop and stop():
                    raise W.Problem("cancelled", "调查等待已取消", 409)
                waited = True
                time.sleep(.05)
        finally:
            if waited:
                self.owner.session.emit("assistant_wait", role="planner", planId=plan_id,
                                        elapsedMs=round((time.monotonic() - start) * 1000))

    # `plan_id` 来自本模块的调查回执。
    # 采纳前复核建议的有效期、源版本和正式队列修订；相同的终态决定可重复查询，改变终态需发起新调查。
    def decide(self, plan_id, decision, reason):
        owner = self.owner
        owner._ensure_running()
        if decision not in ("accept", "defer", "reject") or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
            raise W.Problem("invalid_plan_decision", "请说明采纳、推迟或拒绝的理由")
        with owner.lock:
            row = self._find(plan_id)
            if row.get("scout"):
                raise W.Problem("scout_decision_not_required", "探查结论可直接参考，无须登记决定", 409)
        with FileLock(W.writer_lock_path(row["stamp"]["path"]), check=owner._ensure_running), owner.lock:
            owner._ensure_running()
            if owner._planning_stop():
                raise W.Problem("cancelled", "调查窗口已关闭", 409)
            if row.get("decision", {}).get("kind") in {"accept", "reject"}:
                if row["decision"]["kind"] == decision:
                    return dict(self._view(row), reused=True)
                raise W.Problem("plan_decided", "此建议已有最终处理决定", 409)
            self._expire(row)
            if row["state"] != "completed":
                raise W.Problem("plan_stale", "建议未完成或已过期，不能修改正式队列", 409)
            old_queue = owner.work.state
            queue = old_queue
            if decision == "accept":
                owner._check_work_scope()
                if row["queueRevision"] != owner.work.state["revision"]:
                    self._state(row, "expired")
                    raise W.Problem("plan_stale", "正式队列已经变更，请重新核对", 409)
                if proposal := row.get("proposal"):
                    queue = deepcopy(old_queue)
                    try:
                        WorkQueue(dict(workQueue=queue), owner.task).propose(row["queueRevision"], **proposal)
                    except QueueConflict as exc:
                        raise W.Problem(exc.code, str(exc), 409) from None
            previous = row.get("decision")
            owner.state["workQueue"] = owner.work.state = queue
            row["decision"] = dict(kind=decision, reason=evidence.redact(reason.strip()), runId=owner.session.run_id)
            try:
                owner.save()
            except OSError:
                owner.state["workQueue"] = owner.work.state = old_queue
                if previous is None:
                    row.pop("decision")
                else:
                    row["decision"] = previous
                raise
            owner.session.emit("background_plan_decision", planId=plan_id, decision=decision,
                               queueRevision=owner.work.state["revision"], role="executor")
            return dict(self._view(row), queueRevision=owner.work.state["revision"])

    # 在规划者动作上下文下用冻结文件和队列执行调查。
    # 建议与正式队列分开，直到执行者通过 `decide` 采纳。
    def _run(self, row, files, queue_state, queue):
        owner = self.owner
        token = set_call(owner._identity("planner", "background_plan"))
        started = time.monotonic()
        # 运行停止、版本失效和时限到达均终止后台调查。
        def stopped():
            expiry = row["expiresAt"]
            return owner._planning_stop() or row.get("invalidated") or (expiry is not None and time.time() >= expiry)

        # 区分父运行停止和调查自身失效，给持久回执留下原因。
        def stop_reason():
            if owner._planning_stop():
                return owner.stop_reason('planner')
            return dict(kind='investigation_expired', text='调查已过期或所依据的版本已失效，保留已取得证据')

        report = {}
        receipts = {}
        try:
            if not self._current(row):
                raise Interrupted("调查已失效")
            with owner.lock:
                if stopped():
                    raise Interrupted("调查窗口关闭")
                timing = {"expiresAt": time.time() + row["usefulSeconds"]} if row.get("scout") else {}
                self._state(row, "running", **timing)
                previous = [dict(question=old["question"], conclusion=old["text"][:600],
                                 decision=old.get("decision")) for old in owner.state["plans"]
                            if old["state"] == "completed" and old.get("text") and old["stamp"] == row["stamp"]
                            and old.get("requirements") == row["requirements"] and self._current(old)][-3:]
            tools = Registry()
            # 仅查看申请中允许的材料，检查版本与内容边界。
            def inspect(relative_path, line=1, line_count=60):
                if stopped():
                    raise W.Problem("cancelled", "调查已停止", 409)
                if relative_path not in files:
                    raise W.Problem("plan_scope", "只可读取派发时指定的文件", 403)
                if type(line) is not int or line < 1 or type(line_count) is not int or not 1 <= line_count <= 200:
                    raise W.Problem("invalid_arguments", "行号和行数无效")
                workspace, git = self._checkout(row["resourceId"])
                if (workspace["path"] != row["stamp"]["path"] or git["branch"] != row["stamp"]["branch"]
                        or git["head"] != row["stamp"]["head"]
                        or not row.get("scout") and self._read_file(workspace, relative_path)["revision"] != files[relative_path]["revision"]):
                    raise W.Problem("plan_stale", "调查文件或检出已变化", 409)
                receipt = dict(path=relative_path, line=line, revision=files[relative_path]["revision"],
                               actionId=(current_call() or {}).get("actionId"))
                if len(receipts) < 32:
                    receipts[(relative_path, line)] = receipt
                page = C._read_code_page(files[relative_path], dict(line=line, line_count=line_count))
                return dict(page, text=leaks.redact_source(page["text"]), ok=True)

            # 验证规划建议后暂存，正式队列的更新由执行者确认。
            def propose(order, reason, features=None, dependencies=None):
                if stopped():
                    raise W.Problem("cancelled", "调查已停止", 409)
                proposal = dict(order=order, reason=evidence.redact(reason),
                                features=evidence.redact([] if features is None else features),
                                dependencies={} if dependencies is None else dependencies)
                try:
                    WorkQueue(dict(workQueue=deepcopy(queue_state)), owner.task).propose(row["queueRevision"], **proposal)
                except QueueConflict as exc:
                    raise W.Problem(exc.code, str(exc), 409) from None
                report["proposal"] = proposal
                return dict(ok=True, applied=False, queueRevision=row["queueRevision"])

            string = dict(type="string")
            self._tool(tools, "read_plan_file", "按行读取派发时指定的文件。", dict(relative_path=string,
                       line=dict(type="integer"), line_count=dict(type="integer")), ["relative_path"], inspect, readonly=True)
            # 在冻结的调查文件内查找文本匹配。
            def search(query):
                if not isinstance(query, str) or not 1 <= len(query) <= 200:
                    raise W.Problem("invalid_query", "提供有限文本查询")
                matches = []
                for path, file in files.items():
                    for line, text in enumerate(file["text"].splitlines(), 1):
                        if query.casefold() in text.casefold():
                            matches.append(inspect(path, line, 1))
                            if len(matches) >= 20:
                                return dict(matches=matches, truncated=True)
                return dict(matches=matches, truncated=False)

            probe_count = 0
            # 执行探查中的受限核对并保存可回查结果。
            def probe(argv, timeout=20, material=None):
                nonlocal probe_count
                if stopped() or not self._current(row):
                    raise W.Problem("plan_stale", "调查前提已变化", 409)
                if row.get("needsRecheck"):
                    return dict(ok=False, code="source_changed", error="源码变化：继续分析派发快照，实验需执行者在当前版本核对")
                if type(timeout) is not int or not 1 <= timeout <= 30:
                    raise W.Problem("invalid_timeout", "探测最多 30 秒")
                if probe_count >= 2:
                    return dict(ok=False, code="probe_limit", error="根据已有证据结束调查，返回未解决范围")
                probe_count += 1
                revision = owner._current_revision(row["resourceId"])
                base = scoped_roles.registry(owner.root, role="planner")
                if material is not None and material != (row.get("failurePacket", {}).get("failedAction", {}).get("material")):
                    raise W.Problem("resource_denied", "仅可重用本调查交接的材料版本", 403)
                result = base.get("run_project_check").fn(resource_id=row["resourceId"], argv=argv,
                    timeout=timeout, expected_revision=revision, capture_changes=False,
                    **({"material":material} if material is not None else {}))
                with owner.lock:
                    row.setdefault("probeEvidence", []).append(dict(actionId=(current_call() or {}).get("actionId"),
                        runId=owner.session.run_id, operationId=result.get("operationId")))
                    owner.save()
                if stopped() or not self._current(row):
                    raise W.Problem("plan_stale", "探测期间版本变化，结果须重新核对", 409)
                return result

            if row.get("scout"):
                self._tool(tools, "search_plan_files", "搜索派发时限定的文件。", dict(query=string), ["query"], search, readonly=True)
                self._tool(tools, "run_scout_probe", "隔离最小环境实验，最多两次，每次 30 秒。禁止回写；先说明假设和预期。",
                           dict(argv=dict(type="array", items=string), timeout=dict(type="integer"),
                                material=dict(type="object", properties=dict(artifact_id=string, version_id=string),
                                              required=["artifact_id", "version_id"])), ["argv"], probe)
                if row.get("failurePacket"):
                    from .investigation_evidence import read
                    observed = set()
                    # 从调度方提供的失败材料读取原检查证据。
                    def read_failure(**args):
                        result = read(owner, row, **args)
                        if result.get("ok"):
                            observed.add(args.get("action_id") or (current_call() or {}).get("actionId"))
                        return result
                    self._tool(tools, "read_investigation_evidence", "读取本调查原故障或自己的探针：metadata/log/material；source 可按需读取当前授权项目，明确不同于原失败版本。缺证据返回 needs_evidence。",
                        dict(kind=dict(type="string", enum=["metadata","log","material","source"]), action_id=string,
                             relative_path=string, after=dict(type="integer"), line=dict(type="integer"),
                             line_count=dict(type="integer"), query=string), ["kind"],
                        read_failure, readonly=True)
                    # 保存探查结论与证据，让主任务继续判断是否采用。
                    def submit_investigation(status, summary, action_ids, missing):
                        if (status not in {"confirmed", "hypothesis", "needs_evidence"}
                                or not isinstance(summary,str) or not 1 <= len(summary) <= 4000
                                or not isinstance(action_ids,list) or len(action_ids)>16 or not set(action_ids)<=observed
                                or not isinstance(missing,list) or len(missing)>16
                                or status=="confirmed" and not action_ids):
                            raise W.Problem("investigation_evidence_required", "确认原因需要实际读取的证据；无证据使用 hypothesis/needs_evidence")
                        report["investigationReport"] = dict(status=status,summary=evidence.redact(summary),
                            actionIds=action_ids, missing=evidence.redact(missing))
                        return dict(ok=True, problemClosed=False)
                    self._tool(tools,"submit_investigation_report","区分已证实原因、假设和缺证据。confirmed 必须引用本调查实际读取的证据动作；提交报告不会关闭原问题。",
                        dict(status=dict(type="string",enum=["confirmed","hypothesis","needs_evidence"]), summary=string,
                             action_ids=dict(type="array",items=string),missing=dict(type="array",items=string)),
                        ["status","summary","action_ids","missing"],submit_investigation)
            if not row.get("scout"):
                self._tool(tools, "propose_work_order", "提交建议，正式队列不变。order 包含已有和新增条目，新增编号以 feature- 开头。",
                       dict(order=dict(type="array", items=string), reason=string,
                            features=dict(type="array", items=dict(type="object", properties=dict(id=string, text=string),
                                                                  required=["id", "text"])),
                            dependencies=dict(type="object", additionalProperties=dict(type="array", items=string))),
                       ["order", "reason"], propose)
            context = dict(goal=owner.task, question=row["question"], files=list(files), previous=previous)
            context["engineeringBrief"] = owner.task_brief()
            if row.get("failurePacket"):
                context["failurePacket"] = row["failurePacket"]
            if not row.get("scout"):
                context["queue"] = queue
            elif row.get("excludedWritePaths"):
                context["excluded_files"] = row["excludedWritePaths"]
            instruction = ("只读探路。回答调查问题，给出文件和行号依据、可选做法和未解决的问题。不要向用户提问。"
                           "读取绑定派发快照；源码变化后旧结论仅作线索，标注待核对部分。优先最小实验，勿重复失败尝试。"
                           if row.get("scout") else "只读调查。说明建议方向、依据、影响范围和下一步。不要向用户提问。")
            text = scoped_roles.run(owner.root, owner._role_session(), "planner",
                                    instruction + "\n" + json.dumps(context, ensure_ascii=False),
                                    should_stop=stopped, stop_reason=stop_reason, tools=tools, binding_fields=dict(planId=row["id"]))
            with owner.lock:
                if stopped() or not self._current(row):
                    raise Interrupted("调查已失效")
                self._state(row, "completed", text=evidence.redact(text)[:6000], evidence=list(receipts.values()), **report)
                if row.get("failurePacket") and not row.get("investigationReport"):
                    row["investigationReport"] = dict(status="hypothesis",summary="未提交结构化原因确认；仅作调查线索",actionIds=[],missing=[])
                    owner.save()
        except Interrupted:
            with owner.lock:
                state = "cancelled" if owner._planning_stop() else "expired"
                partial = dict(evidence=list(receipts.values()))
                if receipts and not row.get("invalidated") and not owner._planning_stop():
                    state = "partial"
                    partial["text"] = "调查窗口结束。已读取所列派发版本证据，尚无完整结论；不要当作已解决。"
                self._state(row, state, **partial,
                            **({"reason": "调查前提撤销"} if row.get("invalidated") else {}))
                owner.session.emit("role_phase", role="planner", state=state, planId=row["id"],
                                   reason="调查已停止，未提交可采纳建议")
        except Exception as error:
            with owner.lock:
                detail = evidence.redact(str(error))[:300]
                partial = bool(receipts) and not owner._planning_stop() and not row.get("invalidated")
                self._state(row, "partial" if partial else "failed", error=detail,
                            text="调查未完成：" + detail, evidence=list(receipts.values()))
        finally:
            owner.session.emit("assistant_plan_duration", role="planner", planId=row["id"],
                               elapsedMs=round((time.monotonic() - started) * 1000))
            reset_call(token)

    # 项目或要求变化后使旧调查失效并保留原记录。
    def invalidate(self, resource_id, path):
        with self.owner.lock:
            for row in self.owner.state["plans"]:
                if (row["state"] in {"queued", "running"} and row["resourceId"] == resource_id
                        and path in row["stamp"]["files"]):
                    if row.get("scout"):
                        row["needsRecheck"] = True
                        row["changedPaths"] = sorted(set(row.get("changedPaths", [])) | {path})
                    else:
                        row["invalidated"] = True
                    self.owner.save()

    # 把可用调查按轮次投递到执行者消息；结果是补充证据或建议。
    def feedback(self, messages):
        owner = self.owner
        delivery = {}
        with owner.lock:
            for row in owner.state["plans"]:
                marker = "[background-plan:" + row["id"] + "]"
                if row.get("scout"):
                    self._expire(row)
                    if row["state"] == "expired":
                        if any(msg.role == 'user' and isinstance(msg.content, str)
                               and msg.content.startswith(marker) for msg in messages):
                            from .working_context import append_update
                            append_update(messages, 'expired-plan-' + row['id'],
                                          marker + " 此探查结论已过期，相关判断需重新核对。")
                if row["state"] not in {"completed", "partial"} or row.get("decision") or row.get("deliveredRun") == owner.session.run_id:
                    continue
                self._expire(row)
                if row["state"] not in {"completed", "partial"}:
                    continue
                if not any(isinstance(msg.content, str) and marker in msg.content for msg in messages):
                    note = (" 探查结果（仅供参考，由你自行取舍）："
                            if row.get("scout") else " 规划者建议（不改变用户要求及授权）：")
                    if row.get("needsRecheck"):
                        note += " 基于派发时源码；期间文件变化，相关结论须在当前版本核对，不能作为已验证事实。"
                    suffix = ("" if row.get("scout") else "\n如有帮助，先用 read_background_plan 核对具体调序，"
                              "再用 decide_background_plan 记录处理。")
                    messages.append(Msg("user", marker + note + row["text"] + suffix))
                delivery["background-plan:" + row["id"]] = "next_round"
        return delivery
