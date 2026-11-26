"""Executor boundaries shared by automatic assistance and explicit tools.

All durable state belongs to RoleCoordinator; this module starts no workers.
"""
# 主要引用方包括 `role_coordination.py` 和 `serial_review.py`。
from copy import deepcopy
import hashlib
import json
import time
import re

from . import evidence, workbench as W
from .provider import Msg
from .tools import Tool, current_call

ISSUE_KINDS = {"product_defect", "environment", "fixture", "evidence_gap", "user_decision",
               "context_changed", "infrastructure", "external_limit", "unknown"}


def migrate(owner):
    """Upgrade only the explicitly resumed task; never run jobs as a migration side effect."""
    changed = False
    for key, row in owner.state.get("troubleshooting", {}).items():
        if row.get("schemaVersion") == 1:
            continue
        row.update(schemaVersion=1, issueId=key, itemRevision=row.get("itemRevision", 0),
                   kind=row.get("kind", "unknown"), taskBinding=owner._task_binding(),
                   attemptCount=len(row.get("attempts", [])))
        if row.get("state") in {"resolved", "closed"} and not row.get("closure"):
            row.update(legacyState=row["state"], state="awaiting_verification", legacyVerificationUnknown=True)
        changed = True
    if changed:
        owner.save()


# `ExecutorCore` 传入运行状态，工具调用带回需求哈希和最近一条用户输入位置。
# `task_input.py` 计算有效文本及哈希，过时提案要先重读需求。
def normalize_requirements(owner, expected_content_hash, source_entry, requirements):
    from .task_input import effective_text, requirements_hash
    owner._ensure_running()
    with owner.lock:
        material = deepcopy(owner.state.get("effectiveRequirements"))
        if not material or material["contentHash"] != expected_content_hash:
            raise W.Problem("requirements_changed", "当前需求已更新，请 read_task_requirements 后重试", 409)
        entries = material["entries"]
        if type(source_entry) is not int or source_entry != len(entries) - 1:
            raise W.Problem("invalid_requirement_source", "需引用最近一条用户输入，不能用旧输入撤销新要求")
        if not isinstance(requirements, list) or not 1 <= len(requirements) <= 64:
            raise W.Problem("invalid_requirements", "提交完整的有限验收清单，保留仍有效的要求")
        normalized = []
        seen = set()
        previous_items = {i["id"]: i for i in material.get("requirements", [])}
        for item in requirements:
            if (not isinstance(item, dict) or set(item) != {"id", "text", "status"}
                    or not isinstance(item["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["id"])
                    or item["id"] in seen or not isinstance(item["text"], str) or not 1 <= len(item["text"]) <= 4000
                    or item["status"] not in {"active", "withdrawn"}):
                raise W.Problem("invalid_requirements", "每条需要唯一稳定 id、要求文字和 active/withdrawn 状态")
            previous = previous_items.get(item["id"])
            retained_withdrawal = previous and previous["status"] == "withdrawn" and item["status"] == "withdrawn"
            if item["status"] == "withdrawn" and not retained_withdrawal and not re.search(
                    r"取消|撤销|不再|去掉|删掉|不要|放弃|cancel|withdraw|drop|remove|no longer|don't|do not", entries[source_entry]["text"], re.I):
                raise W.Problem("withdrawal_not_authorized", "用户当前输入未明确撤销要求；保留原义务")
            if previous and previous["status"] == "withdrawn" and item["status"] == "active" and not re.search(
                    r"恢复|重新|又要|restore|re.?enable|bring back", entries[source_entry]["text"], re.I):
                raise W.Problem("reactivation_not_authorized", "已撤销事项不能随新补充静默恢复")
            seen.add(item["id"])
            normalized.append(dict(item, text=evidence.redact(item["text"]), sourceEntry=(previous["sourceEntry"]
                if previous and previous["status"] == item["status"] and previous["text"] == evidence.redact(item["text"])
                else source_entry)))
        old_ids = {i["id"] for i in material.get("requirements", [])}
        if not old_ids <= seen:
            raise W.Problem("requirements_missing", "不能静默省略原必验项；撤销项也需显式保留")
        if (material.get("requirements") == normalized and material.get("normalizedAt") == source_entry
                and not material.get("normalizationPending")):
            return dict(ok=True, contentHash=material["contentHash"], requirements=normalized, reused=True)
        previous_requirements = material.get("requirements", [])
        material.update(requirements=normalized, normalizationPending=False)
        material.setdefault("normalizationHistory", []).append(dict(sourceEntry=source_entry,
            previous=material.get("normalizedAt"), requirements=previous_requirements))
        material["normalizedAt"] = source_entry
        material["contentHash"] = requirements_hash(material)
        if hasattr(owner.session, "save"):
            owner.session.save(effectiveRequirements=material)
        owner.state["effectiveRequirements"] = material
        owner.task = effective_text(material)
        owner.save()
        return dict(ok=True, contentHash=material["contentHash"], requirements=normalized)


# 从持久工作队列恢复需要用户继续处理的阻塞原因。
def pause_reason(status, why):
    reason = "evidence_unavailable"
    if status == "needs_human":
        reason = "waiting_user"
    elif any(word in why for word in ("截止", "窗口已停止", "超时")):
        reason = "deadline_reached"
    elif "未知" in why or "未确认" in why:
        reason = "stop_unconfirmed"
    elif "环境" in why:
        reason = "environment_blocked"
    return dict(reason=reason, origin="system", detail=evidence.redact(why),
                nextAction="核对原候选、需求和缺项；先确认未知进程，恢复后定点补验")


# 调查超出时限后更新阻塞状态，保留已取得的线索。
def expire_blockers(owner):
    changed = False
    for row in owner.state.get("troubleshooting", {}).values():
        if row.get("state") == "investigating" and time.time() >= row.get("deadline", float("inf")):
            row.update(state="needs_scout", reason="diagnostic_deadline", itemRevision=row.get("itemRevision", 0) + 1)
            changed = True
    if changed:
        owner.save()


# 把工作流入口注册为执行者工具，实际任务状态保留在传入的执行者对象。
def install(owner, registry):
    from .execution_completion import install as completion_tools
    completion_tools(owner, registry)
    string = dict(type="string")
    registry.add(Tool("normalize_task_requirements", "整理完整有效要求；续接后明确保留、补充与撤销。"
        "引用最近用户输入的 source_entry，不能静默丢原验收项；历史原话保留。",
        dict(type="object", properties=dict(expected_content_hash=string, source_entry=dict(type="integer"),
             requirements=dict(type="array", items=dict(type="object", properties=dict(id=string, text=string,
                 status=dict(type="string", enum=["active", "withdrawn"])), required=["id", "text", "status"]))),
             required=["expected_content_hash", "source_entry", "requirements"]),
        lambda **args: normalize_requirements(owner, **args), zone="offflow", kind="meta"))
    registry.add(Tool("update_task_blueprint",
        "工程任务开始并准备分支后维护简短蓝图；保留全部验收条件，列下一到两个具体未知。"
        "调查自动派发，不等批准。完成一个可审查阶段时传 stage_complete=true。",
        dict(type="object", properties=dict(resource_id=string, paths=dict(type="array", items=string),
             acceptance=string, stage=string, investigations=dict(type="array", items=string),
             stage_complete=dict(type="boolean")),
             required=["resource_id", "paths", "acceptance", "stage", "investigations"]),
        lambda **args: blueprint(owner, **args), zone="offflow", kind="meta"))
    registry.add(Tool("read_check_queue", "读取检查待办、未关闭缺陷、排障尝试及当前蓝图；阅读不关闭事项。",
        dict(type="object", properties={}), lambda: read(owner), zone="offflow", kind="read", readonly=True))
    registry.add(Tool("read_task_requirements", "分页读取本逻辑任务的原目标、补充与用户答复；新要求须保留仍有效的旧目标。",
        dict(type="object", properties=dict(after=dict(type="integer"))),
        lambda after=0: requirements(owner, after), zone="offflow", kind="read", readonly=True))
    registry.add(Tool("record_troubleshooting", "记录具体阻塞及假设/新证据。无新证据重复尝试时自动转交探路；"
        "更新使用返回的 issue_id 和 expected_item_revision，不要改描述来创建新问题。"
        "resolved 是解决声明，只有有效回执才关闭；blocked 保留未完成义务。",
        dict(type="object", properties=dict(resource_id=string, goal=string, signature=string,
             hypothesis=string, result=string, new_evidence=string,
             state=dict(type="string", enum=["investigating", "resolved", "blocked"]),
             issue_id=string, expected_item_revision=dict(type="integer"),
             kind=dict(type="string", enum=sorted(ISSUE_KINDS)), evidence_action_id=string),
             required=["resource_id", "goal", "signature", "hypothesis", "result", "new_evidence", "state"]),
        lambda **args: troubleshoot(owner, **args), zone="offflow", kind="meta"))


# 读取执行者当前有效需求及版本，供修改验收清单前核对。
def requirements(owner, after=0):
    material = owner.state.get("effectiveRequirements") or {"entries": [dict(kind="goal", text=owner.task)]}
    from .task_input import requirements_page
    return requirements_page(material, after)


# 汇总正式队列、蓝图与故障调查，再用 `delivery_review.executor_gate` 读取实际验收证据。
def read(owner):
    with owner.lock:
        migrate(owner)
        expire_blockers(owner)
        result = deepcopy(dict(queue=owner.work.read(), blueprint=owner.state.get("blueprint"),
                               troubleshooting=owner.state.get("troubleshooting", {})))
    # Executor's own required checks retain their existing evidence store and IDs.
    from .delivery_review import executor_gate
    try:
        result["acceptance"] = executor_gate(owner.session.dir, owner.task_input)
    except (W.Problem, OSError, ValueError) as error:
        result["acceptance"] = dict(ok=False, unavailable=True, reason=str(error)[:200])
    return result


# 先核对代码资源、受限文件列表和阶段，再保存计划与后续调查问题。
def blueprint(owner, resource_id, paths, acceptance, stage, investigations, stage_complete=False):
    from .review_units import relative_path
    owner._ensure_running()
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 8
            or any(not isinstance(p, str) for p in paths)
            or not isinstance(investigations, list) or len(investigations) > 2
            or any(not isinstance(q, str) or not 1 <= len(q.strip()) <= 2000 for q in investigations)
            or not isinstance(acceptance, str) or not 1 <= len(acceptance.strip()) <= 1800
            or not isinstance(stage, str) or not 1 <= len(stage.strip()) <= 400
            or type(stage_complete) is not bool):
        raise W.Problem("invalid_blueprint", "提供有限文件范围、验收条件、当前阶段和最多两条具体调查")
    owner._resource(resource_id)
    paths = sorted({relative_path(p) for p in paths})
    with owner.lock:
        old = owner.state.get("blueprint", {})
        owner.state["blueprint"] = dict(resourceId=resource_id, paths=paths,
            acceptance=evidence.redact(acceptance.strip()), stage=evidence.redact(stage.strip()),
            investigations=[evidence.redact(q.strip()) for q in investigations],
            revision=old.get("revision", 0) + 1)
        for plan in owner.state["plans"]:
            if plan.get("automatic") and not plan.get("blocker") and (
                plan["resourceId"] != resource_id or plan["question"] not in investigations
                or old.get("acceptance") != acceptance.strip()
            ):
                plan["invalidated"] = True
                if plan["state"] in {"completed", "queued"}:
                    plan["state"] = "expired"
        owner.save()
    dispatch(owner)
    if not getattr(owner, "serial_review", False):
        from .review_preparation import start as prepare_review
        prepare_review(owner, resource_id)
    if stage_complete:
        before_checks(owner)
    return dict(ok=True, blueprint=read(owner)["blueprint"])


# 记录假设、实验和新证据；需证明已解决时要求填写证据，并按预期条目修订防止过期覆盖。
def troubleshoot(owner, resource_id, goal, signature, hypothesis, result, new_evidence, state,
                 issue_id=None, expected_item_revision=None, kind="unknown", evidence_action_id=None):
    owner._ensure_running()
    if (state not in {"investigating", "resolved", "blocked"}
            or any(not isinstance(s, str) or not 1 <= len(s.strip()) <= 1200
                   for s in (goal, signature, hypothesis, result))
            or not isinstance(new_evidence, str) or len(new_evidence) > 1200
            or state == "resolved" and not new_evidence.strip()):
        raise W.Problem("invalid_troubleshooting", "说明阻塞、假设、实际结果及新证据；解决必须附证据")
    if kind not in ISSUE_KINDS:
        raise W.Problem("invalid_issue_kind", "请选择已知问题类型；不确定时使用 unknown")
    owner._resource(resource_id)
    key = hashlib.sha256(json.dumps([resource_id, goal, signature], ensure_ascii=False).encode()).hexdigest()[:24]
    with owner.lock:
        rows = owner.state.setdefault("troubleshooting", {})
        if issue_id is not None:
            if not isinstance(issue_id, str) or issue_id not in rows or rows[issue_id]["resourceId"] != resource_id:
                raise W.Problem("unknown_issue", "问题不属于当前任务或资源")
            key = issue_id
        elif state == "resolved" and key not in rows:
            raise W.Problem("unknown_issue", "解决已有问题必须使用原 issue_id；请先 read_check_queue")
        event_id = (current_call() or {}).get("actionId")
        if key in rows and event_id and event_id in rows[key].get("eventIds", []):
            return dict(ok=True, blockerId=key, blocker=deepcopy(rows[key]), reused=True)
        if issue_id is not None and (type(expected_item_revision) is not int
                                    or expected_item_revision != rows[key].get("itemRevision", 0)):
            raise W.Problem("revision_conflict", "问题已有更新，请 read_check_queue 后使用当前 itemRevision", 409)
        if key not in rows and len(rows) >= 32:
            raise W.Problem("troubleshooting_capacity", "请先处理已有阻塞")
        row = deepcopy(rows.get(key, dict(schemaVersion=1, issueId=key, kind=kind,
            taskBinding=owner._task_binding(), itemRevision=0, resourceId=resource_id, goal=evidence.redact(goal),
            signature=evidence.redact(signature), attempts=[], owner="executor", deadline=time.time() + 90)))
        if kind != "unknown":
            if row.get("kind") != kind:
                row["classificationHistory"] = [*row.get("classificationHistory", []),
                    dict(previous=row.get("kind", "unknown"), kind=kind, reason=evidence.redact(result))][-16:]
            row["kind"] = kind
        if evidence_action_id and state != "resolved":
            receipt = owner.state.get("environmentChecks", {}).get(evidence_action_id)
            if not receipt or receipt["resourceId"] != resource_id:
                raise W.Problem("evidence_binding_mismatch", "发现证据需来自当前任务相同资源的实际检查")
            row.setdefault("observedBinding", deepcopy(receipt))
            row.setdefault("blockedArgv", receipt.get("argv"))
            row["latestActionId"] = evidence_action_id
        attempt = evidence.redact(dict(hypothesis=hypothesis, result=result, evidence=new_evidence))
        row["attempts"] = [*row["attempts"], attempt][-8:]
        row["attemptCount"] = row.get("attemptCount", 0) + 1
        row["itemRevision"] = row.get("itemRevision", 0) + 1
        if event_id:
            row["eventIds"] = [*row.get("eventIds", []), event_id][-64:]
        delegated = row.get("state") == "delegated" and state == "investigating"
        row["state"] = "awaiting_verification" if state == "resolved" else state
        if state == "resolved" and evidence_action_id:
            receipt = owner.state.get("environmentChecks", {}).get(evidence_action_id)
            if (not receipt or receipt["resourceId"] != resource_id
                    or receipt["revision"] != owner._current_revision(resource_id)):
                raise W.Problem("evidence_binding_mismatch", "需当前资源版本上的真实检查回执")
            if (row.get("kind") in {"environment", "fixture"} and receipt["status"] == "passed"
                    and not receipt["blockers"] and row.get("blockedArgv") == receipt.get("argv")):
                row.update(state="closed", closure=dict(reason="verified", actionId=evidence_action_id,
                                                       revision=receipt["revision"]))
        if state == "investigating" and row["attemptCount"] >= 2 and not new_evidence.strip():
            row["state"] = "needs_scout"
        if delegated:
            row["state"] = "delegated"
        rows[key] = row
        expire_blockers(owner)
        owner.save()
    dispatch(owner)
    return dict(ok=True, blockerId=key, blocker=deepcopy(row))


# 遇到需要探查的阻塞项时优先分派并附原检查材料，否则沿当前蓝图发调查。
def dispatch(owner):
    with owner.lock:
        expire_blockers(owner)
    planner = owner.investigation
    if not planner or owner._planning_stop():
        return
    with owner.lock:
        for row in owner.state.get("troubleshooting", {}).values():
            if row["state"] == "delegated":
                plan = next((p for p in owner.state["plans"] if p["id"] == row.get("planId")), None)
                if plan and plan["state"] not in {"queued", "running"}:
                    row.update(state="investigated" if plan["state"] == "completed" else "blocked",
                               conclusion=plan.get("text", plan.get("error", plan["state"])))
                    owner.save()
        if any(p["state"] in {"queued", "running"} for p in owner.state["plans"]):
            return
        bp = deepcopy(owner.state.get("blueprint", {}))
        if not bp:
            return
        blocker = next((r for r in owner.state.get("troubleshooting", {}).values()
                        if r["state"] == "needs_scout" and r["resourceId"] == bp["resourceId"]), None)
        from .investigation_evidence import packet
        failure_packet = packet(owner, blocker) if blocker else None
        questions = (["定位检查失败的真实原因；通过 read_investigation_evidence 读取原日志和材料，不猜占位路径。问题：" + blocker["goal"]]
                     if blocker else bp["investigations"])
    for question in questions:
        try:
            result = planner.request(question, bp["resourceId"], bp["paths"], 90,
                                     scout=True, next_write_paths=[], allow_overlap=True, failure_packet=failure_packet)
        except (W.Problem, OSError, ValueError) as error:
            owner.session.emit("assistant_dispatch_deferred", role="planner", reason=str(error)[:200])
            return
        with owner.lock:
            plan = next((p for p in owner.state["plans"] if p["id"] == result["planId"]), None)
            if plan:
                plan.update(automatic=True, blocker=bool(blocker), blueprintRevision=bp.get("revision"))
            if blocker:
                blocker.update(owner="planner", planId=result["planId"], state="delegated")
            owner.save()
        if not result.get("reused"):
            return


# 阶段检查前仅为版本已确认且尚未有有效候选的资源准备送审。
def before_checks(owner, *, repairs_only=False, revisions=None):
    if getattr(owner, "serial_review", False): return
    if not owner.executor_led or not owner.background_reviewer or owner._review_stop():
        return
    with owner.lock:
        dirty = dict(owner.state["dirty"] if revisions is None else revisions)
    for rid, revision in dirty.items():
        if str(revision).startswith("pending:"):
            current = owner._current_revision(rid)
            if current is None:
                continue
            with owner.lock:
                if owner.state["dirty"].get(rid) != revision:
                    continue
                owner.state["dirty"][rid] = current
                owner.save()
            revision = current
        with owner.lock:
            if repairs_only and not any(item["kind"] == "defect" and item["resourceId"] == rid
                and item["state"] != "verified" and (item.get("handling") or {}).get("decision") in {"fix", "recheck"}
                and item.get("lastObservedRevision", item["revision"]) != revision
                for item in owner.work.state["items"].values()):
                continue
            if any(item["kind"] == "defect" and item["resourceId"] == rid
                   and item["state"] != "verified" and not item.get("handling")
                   for item in owner.work.state["items"].values()):
                continue  # Deliver existing findings before reviewing an unrepaired intermediate version.
            if any(r["resourceId"] == rid and r["revision"] == revision
                   and r["state"] != "superseded" for r in owner.state["candidates"].values()):
                continue
        try:
            from .tools import set_call, reset_call
            token = set_call(owner._identity("executor", "publish_review_candidate"))
            try:
                owner.publish(rid, revision, "阶段检查前自动送审：核对差异、需求和未覆盖边界。", provisional=True)
            finally:
                reset_call(token)
        except (W.Problem, OSError, ValueError) as error:
            owner.session.emit("assistant_dispatch_deferred", role="reviewer", reason=str(error)[:200])


def observe_reads(owner, jobs, results):
    """A soft progress signal: a repeated read remains available, including after compaction."""
    with owner.lock:
        progress = owner.state.setdefault("readProgress", dict(streak=0, unchangedRepeats=0, seen={}))
        changed = False
        for job in jobs:
            output = results.get(job["i"], (None, 0))[0]
            name, args = job["name"], job["args"]
            if (name == "run_project_check" or name in {"write_project_file", "edit_project_file", "apply_subtask_integration"}
                    and isinstance(output, dict) and output.get("ok")):
                progress.update(streak=0, unchangedRepeats=0, seen={})
                changed = True
            if not isinstance(output, dict) or output.get("ok") is False:
                continue
            if name not in {"read_project_resource", "search_project_code", "inspect_code"}:
                continue
            key = W.digest(json.dumps([name, args], sort_keys=True, ensure_ascii=False).encode())
            digest = W.digest(json.dumps(output, sort_keys=True, ensure_ascii=False).encode())
            previous = progress["seen"].get(key)
            progress["streak"] += 1
            if previous and previous["digest"] == digest:
                progress["unchangedRepeats"] += 1
                progress["lastRepeat"] = dict(tool=name, path=args.get("path", args.get("relative_path")),
                    previousActionId=previous["actionId"], actionId=job.get("action_id"))
            progress["seen"][key] = dict(digest=digest, actionId=job.get("action_id"))
            changed = True
            while len(progress["seen"]) > 128:
                del progress["seen"][next(iter(progress["seen"]))]
        if changed:
            owner.save()


def observe_failures(owner, job, output):
    """Keep assertion/fixture loops visible even when the command or code revision changes."""
    diagnostics = output.get("diagnostics") or {}
    verification = output.get("verification") or {}
    rid, argv = job["args"].get("resource_id"), job["args"].get("argv")
    if not rid or not verification.get("revision"):
        return
    if verification.get("status") == "passed":
        with owner.lock:
            if owner.state.get("dirty", {}).get(rid, verification["revision"]) != verification["revision"]:
                return  # A later write in this same reply already invalidated the check.
            for row in owner.state.get("troubleshooting", {}).values():
                if (row.get("origin") == "self_check" and row["resourceId"] == rid
                        and row.get("kind") == "unknown"
                        and row.get("blockedArgv") == argv and row.get("state") != "closed"):
                    row.update(state="closed", itemRevision=row["itemRevision"] + 1,
                               closure=dict(reason="original_check_passed", actionId=verification.get("actionId") or job.get("action_id"),
                                            revision=verification["revision"]))
            owner.save()
        return
    if verification.get("status") != "failed" or diagnostics.get("environmentBlockers"):
        return
    failures = diagnostics.get("failedCases") or diagnostics.get("keyErrors") or []
    # Prefer a stable test identity; do not classify an assertion as a confirmed product defect.
    for entry in failures[:3]:
        text = str(entry.get("text", ""))
        signature = re.sub(r"^(?:FAIL|ERROR):\s*", "", text)[:900]
        if not signature:
            continue
        if any(row.get("latestActionId") == job.get("action_id") and row.get("signature") == signature
               for row in owner.state.get("troubleshooting", {}).values()):
            continue
        try:
            result = troubleshoot(owner, rid, "核对失败自检的预期、夹具与实现", signature,
                "依据用户需求和原有行为核对预期；不要仅为通过而削弱断言",
                json.dumps(dict(failure=text, errors=diagnostics.get("keyErrors", [])[:2],
                                actionId=job.get("action_id"), revision=verification["revision"]),
                           ensure_ascii=False)[:1200], "", "investigating", evidence_action_id=job.get("action_id"))
            with owner.lock:
                row = owner.state["troubleshooting"][result["blockerId"]]
                row.update(origin="self_check", latestActionId=job.get("action_id"))
                row.setdefault("blockedArgv", argv)
                owner.save()
        except (W.Problem, OSError) as error:
            owner.session.emit("assistant_dispatch_deferred", role="planner", reason=str(error)[:200])


# 根据实际工具活动更新工作队列和可核对的阶段状态。
def observe(owner, jobs, results):
    from .execution_completion import behavior_enabled
    if behavior_enabled():
        observe_reads(owner, jobs, results)
    for job in jobs:
        output = results.get(job["i"], (None, 0))[0]
        if job["name"] != "run_project_check" or not isinstance(output, dict):
            continue
        diagnostics = output.get("diagnostics") or {}
        rid = job["args"].get("resource_id")
        verification = output.get("verification") or {}
        action = job.get("action_id")
        if rid and action and verification.get("revision"):
            with owner.lock:
                checks = owner.state.setdefault("environmentChecks", {})
                checks[action] = dict(resourceId=rid, revision=verification["revision"],
                    status=verification.get("status"), blockers=bool(diagnostics.get("environmentBlockers")),
                    argv=job["args"].get("argv"), actionId=action, runId=owner.session.run_id,
                    operationId=output.get("operationId"), processId=(output.get("process") or {}).get("id"),
                    environment=output.get("environment"), material=job["args"].get("material"),
                    diagnostics=deepcopy(diagnostics), started=bool(output.get("process")))
                while len(checks) > 128:
                    del checks[next(iter(checks))]
                owner.save()
            if behavior_enabled():
                observe_failures(owner, job, output)
            if verification.get("status") == "passed":
                # A completed self-check is a runnable stage signal, not a promise before launch.
                before_checks(owner)
            elif behavior_enabled() and verification.get("status") == "failed":
                # Batched writes are observed here after their actual resulting version is known.
                before_checks(owner, repairs_only=True)
        for entry in diagnostics.get("environmentBlockers", [])[:1]:
            rid = job["args"].get("resource_id")
            if not rid:
                continue
            try:
                signature = json.dumps([entry.get("kind"), entry.get("text"),
                                        job["args"].get("argv")], ensure_ascii=False)[:1200]
                recorded = troubleshoot(owner, rid, "恢复项目检查环境", signature,
                    json.dumps(job["args"].get("argv", []), ensure_ascii=False)[:1200],
                    str(entry.get("text", entry))[:1200], "", "investigating", kind="environment",
                    evidence_action_id=action if action in owner.state.get("environmentChecks", {}) else None)
                with owner.lock:
                    owner.state["troubleshooting"][recorded["blockerId"]]["blockedArgv"] = job["args"].get("argv")
                    owner.state["troubleshooting"][recorded["blockerId"]]["latestActionId"] = action
                    owner.save()
            except (W.Problem, OSError) as error:
                owner.session.emit("assistant_dispatch_deferred", role="planner", reason=str(error)[:200])


# 把本轮新增、仍有效的工作流事实交回执行者上下文。
def feedback(owner, messages):
    with owner.lock:
        delivered = {f.get("workItemId") for c in owner.state["candidates"].values()
                     for f in c.get("findings", {}).values() if f.get("deliveredRun")}
        pending = [dict(id=r["id"], state=r["state"], text=r["text"], handling=r.get("handling"))
                   for r in owner.work.state["items"].values() if r["kind"] == "defect" and r["state"] != "verified"
                   and (r.get("handling") or r["id"] in delivered)]
        active_blockers = [(key, row) for key, row in owner.state.get("troubleshooting", {}).items()
                           if row.get("state") not in {"closed", "resolved"}]
        blockers = {key: dict(issueId=key, itemRevision=row.get("itemRevision", 0),
                             kind=row.get("kind", "unknown"), resourceId=row["resourceId"], goal=row["goal"], state=row["state"],
                             owner=row["owner"], latestActionId=row.get("latestActionId"),
                             attempts=[{field: str(value)[:320] for field, value in attempt.items()}
                                       for attempt in row["attempts"][-1:]], conclusion=row.get("conclusion", "")[:320])
                    for key, row in active_blockers[:4]}
        material = owner.state.get("effectiveRequirements") or {}
        requirement_update = (dict(contentHash=material["contentHash"], sourceEntry=len(material["entries"]) - 1,
                                   action="先 read_task_requirements，再 normalize_task_requirements 整理保留/补充/撤销项")
                              if material.get("normalizationPending") else None)
        progress = owner.state.get("readProgress", {})
        read_progress = ({k: v for k, v in progress.items() if k != "seen"}
                         if progress.get("unchangedRepeats", 0) >= 2 or progress.get("streak", 0) >= 24 else None)
        note = json.dumps(dict(pending=pending, requirementUpdate=requirement_update, blueprint=owner.state.get("blueprint"),
            blockers=blockers, additionalBlockerCount=max(0, len(active_blockers) - 4),
            readProgress=read_progress), ensure_ascii=False)
    marker = "[executor-check-queue]"
    from .context_manager import fingerprint
    marker += ':' + fingerprint(note)
    previous = next((m for m in reversed(messages)
                     if str(m.content).startswith('[executor-check-queue]:')), None)
    if previous and str(previous.content).startswith(marker):
        return
    if previous or pending or requirement_update or owner.state.get("blueprint") or owner.state.get("troubleshooting") or read_progress:
        messages.append(Msg("user", marker + " 新事项在安全步骤核对，相关修复合批检查；送达不等于关闭。"
                            "更新排障用 issue_id 和当前 itemRevision；完整记录见 read_check_queue。"
                            + ("当前连续读码或重复读未变化内容较多：先明确未解假设，用 search_project_code 定位后 inspect_code 按行读取；"
                               "已有证据优先复用。需要回忆可重读；仍未明的问题写进蓝图交探路者，执行者继续可做的实现。" if read_progress else "")
                            + "\n" + note, context_parts=(("runtimeUpdate", note),)))
