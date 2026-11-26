"""Versioned requirements and human decisions over actual tool evidence."""

# 主要引用方包括 `workbench.py` 和 `tools.py`。

import json
from pathlib import Path
import time
import uuid
from . import workbench as W, projects, artifacts, evidence
from .filelock import FileLock, atomic_json

MAX_BYTES = 1024 * 1024


def record_schema():
    """Describe conditional fields at the same seam that validates the record."""
    string = dict(type="string", minLength=1)
    target = dict(anyOf=[
        dict(type="object", properties=dict(kind=dict(enum=["code"]), id=string, revision=string),
             required=["kind", "id", "revision"]),
        dict(type="object", properties=dict(kind=dict(enum=["artifact"]), id=string, versionId=string, hash=string),
             required=["kind", "id", "versionId", "hash"])])
    props = dict(operation=dict(enum=["requirement", "coverage", "finding"]),
                 expectedRevision=dict(type="integer", description="inspect_delivery 的整数 revision，非代码哈希"),
                 text=string, required=dict(type="boolean"), requirementId=string,
                 kind=dict(enum=["automatic", "model"]), target=dict(type="object"), actionId=string, note=string)
    return dict(type="object", properties=dict(payload=dict(type="object", properties=props,
        required=["operation", "expectedRevision"], anyOf=[
            dict(properties=dict(operation=dict(enum=["requirement"])), required=["text"]),
            dict(properties=dict(operation=dict(enum=["coverage"]), target=target), required=["requirementId", "target", "kind", "note"],
                 anyOf=[dict(properties=dict(kind=dict(enum=["automatic"])), required=["actionId"]),
                        dict(properties=dict(kind=dict(enum=["model"]))) ]),
            dict(properties=dict(operation=dict(enum=["finding"]), target=target), required=["text", "target"])])), required=["payload"], additionalProperties=False)


def signature(value):
    return W.digest(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())


def current_target(ident, requested):
    if not isinstance(requested, dict):
        raise W.Problem("target_required", "请选择要核对的成果或代码版本")
    kind, item_id = requested.get("kind"), requested.get("id")
    if kind == "artifact":
        item = artifacts.artifact(ident, item_id)
        version = artifacts.read_version(ident, item_id, item["versions"][-1]["id"])[
            "version"
        ]
        return dict(
            kind=kind, id=item_id, versionId=version["id"], hash=version["hash"]
        )
    if kind == "code":
        resource = next(
            (
                r
                for r in projects.scope(ident)["resources"]
                if r["id"] == item_id
                and r["kind"] == "code"
                and r["permission"] == "read"
            ),
            None,
        )
        if not resource:
            raise W.Problem(
                "resource_denied", "此代码资源不在当前明确选择的读取范围", 403
            )
        root = Path(resource["path"])
        if not root.is_dir() or str(root.resolve()) != resource["path"]:
            raise W.Problem("resource_moved", "代码资源路径已变化", 409)
        status = W.git_status({"path": str(root)})
        if not status["available"] or not status["revision"]:
            raise W.Problem(
                "revision_unavailable", "代码验收需要可核对的 Git 工作树版本", 409
            )
        return dict(kind=kind, id=item_id, revision=status["revision"])
    raise W.Problem("invalid_target", "不支持的验收目标")


# 验收绑定完整且当前的目标版本，不能只靠资源 ID 把旧证据或旧决定套到新成果。
def bind_target(ident, requested):
    if isinstance(requested, dict):
        fields = {"code": ("revision",), "artifact": ("versionId", "hash")}.get(requested.get("kind"), ())
        missing = [key for key in fields if not isinstance(requested.get(key), str) or not requested[key]]
        if missing:
            raise W.Problem("target_revision_required", "缺少 " + "、".join("target." + key for key in missing)
                            + "；请完整使用 inspect_delivery 返回的 target", 400)
    current = current_target(ident, requested)
    if current != requested:
        raise W.Problem("target_changed", "目标版本已变化，请核对新版本后重新决定", 409)
    return current


def storage(ident):
    if not isinstance(ident, str) or not evidence.ACTION_ID.fullmatch(ident):
        raise W.Problem("invalid_source", "验收会话身份无效", 403)
    projects.conversation(ident)
    root = W.home() / "delivery-reviews"
    return root / (ident + ".json"), FileLock(root / (ident + ".lock"))


def bind_pending_requirements(ident, task_id):
    """Assign requirements recorded before the first task to that task."""
    path, lock = storage(ident)
    if not path.is_file():
        return
    with lock:
        data = load(path)
        pending = [item for item in data["requirements"]
                   if item.get("required") and item.get("logicalTaskId") is None]
        if pending:
            for item in pending:
                item["logicalTaskId"] = task_id
            data["revision"] += 1
            save(path, data)


def executor_gate(directory, task, *, only_requirements=None, targets=None):
    """Check declared must-pass requirements for this task and demand version."""
    from .corrections import view

    directory = Path(directory)
    ident = directory.name
    sv = view(directory)
    path, lock = storage(ident)
    with lock:
        data = load(path)
    rows = required_status(ident, data, task, sv, only_requirements=only_requirements, targets=targets)
    return dict(ok=all(row["status"] in {"passed", "confirmed"} for row in rows),
                revision=data["revision"], requirements=rows)


def required_status(ident, data, task, sv, *, only_requirements=None, target=None, targets=None):
    rows = []
    targets = dict(targets or {})
    for item in data["requirements"]:
        if only_requirements is not None and item["id"] not in only_requirements:
            continue
        if not item.get("required") or item.get("logicalTaskId") not in {None, task["id"]}:
            continue
        ref = item["targetRef"]
        if target and not all(target.get(key) == value for key, value in ref.items()):
            continue
        key = ref["kind"], ref["id"]
        if key not in targets:
            try:
                targets[key] = target or current_target(ident, ref)
            except (W.Problem, OSError):
                targets[key] = None
        selected = targets[key]
        status = "target_unavailable" if selected is None else "unverified"
        previous = False
        if selected:
            # 必验覆盖同时匹配逻辑任务、需求修订、要求修订和成果版本；模型自评不能替代人工/自动证据。
            latest = {}
            for proof in reversed(data["coverage"]):
                if (proof["requirementId"] != item["id"] or proof["kind"] not in {"human", "automatic"}
                        or proof.get("logicalTaskId") != task["id"]):
                    continue
                if (proof["requirementRevision"] == item["revision"] and proof["target"] == selected
                        and proof.get("demandRevision") == task["revision"]):
                    latest.setdefault(proof["kind"], proof)
                else:
                    previous = True
            if "human" in latest:
                status = "confirmed"
            elif "automatic" in latest:
                try:
                    status = automatic_result(ident, latest["automatic"], selected, sv)
                except (W.Problem, OSError, ValueError):
                    status = "unverified"
        rows.append(dict(id=item["id"], text=item["text"], status=status, target=selected,
                         needsRebind=previous and status not in {"passed", "confirmed"}))
    return rows


def code_review_requirements(ident, task_id):
    path, lock = storage(ident)
    with lock:
        return [item for item in load(path)["requirements"] if item.get("required")
                and item.get("logicalTaskId") == task_id and item["targetRef"]["kind"] == "code"]


# 缺失文件按尚未建账本处理；已存在但损坏/超限的记录必须报错，不能静默清空验收义务。
def load(path):
    if not path.exists():
        return dict(
            revision=0,
            requirements=[],
            coverage=[],
            decisions=[],
            findings=[],
            receipts={},
        )
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not {
            "revision",
            "requirements",
            "coverage",
            "decisions",
            "findings",
            "receipts",
        }.issubset(value):
            raise ValueError()
        required = {
            "requirements": ({"id", "text", "revision", "position", "history"}, 50),
            "coverage": (
                {
                    "id",
                    "kind",
                    "requirementId",
                    "requirementRevision",
                    "target",
                    "note",
                },
                250,
            ),
            "decisions": (
                {
                    "id",
                    "decision",
                    "target",
                    "reason",
                    "requirementsRevision",
                    "coverageRevision",
                },
                100,
            ),
            "findings": ({"id", "text", "target", "state", "history"}, 100),
        }
        if (
            type(value["revision"]) is not int
            or value["revision"] < 0
            or not isinstance(value["receipts"], dict)
            or len(value["receipts"]) > 500
        ):
            raise ValueError()
        for name, (keys, limit) in required.items():
            if (
                not isinstance(value[name], list)
                or len(value[name]) > limit
                or any(
                    not isinstance(row, dict) or not keys.issubset(row)
                    for row in value[name]
                )
            ):
                raise ValueError()
        return value
    except (ValueError, OSError, RecursionError):
        raise W.Problem("review_unreadable", "验收记录不可读，未重置", 503)


def save(path, data):
    if len(json.dumps(data, ensure_ascii=False, indent=1).encode()) > MAX_BYTES:
        raise W.Problem("review_full", "验收记录达到保留上限，本次尚未接收", 413)
    atomic_json(path, data)


def automatic_result(ident, proof, target, sv):
    fact = evidence.detail(ident, proof["actionId"], sv)
    if fact["evidenceRevision"] != proof["evidenceRevision"]:
        raise W.Problem("evidence_changed", "检查动作的原始记录已变化", 409)
    meta = fact.get("output") or {}
    if meta.get("state") != "retained" or meta.get("bytes", 0) > 65536:
        return "unknown"
    page = evidence.output(ident, fact["id"], 0, sv)
    if page["evidenceRevision"] != proof["evidenceRevision"]:
        raise W.Problem("evidence_changed", "检查输出的来源版本已变化", 409)
    impact = fact.get("impact") if isinstance(fact.get("impact"), dict) else {}
    process = impact.get("process") if isinstance(impact.get("process"), dict) else {}
    arguments = fact.get("input") if isinstance(fact.get("input"), dict) else {}
    if (
        target["kind"] != "code"
        or fact.get("tool") != "run_project_check"
        or arguments.get("resource_id") != target["id"]
    ):
        return "unknown"
    # A check that modifies the worktree cannot certify the resulting new version.
    if (
        process.get("revision") != target["revision"]
        or impact.get("projectRevisionAfter") != target["revision"]
    ):
        return "stale"
    if proof.get("sourceRunId") and not historical_check(ident, fact, target,
            dict(id=proof.get("logicalTaskId"), revision=proof.get("demandRevision"))):
        return "stale"
    if (
        process.get("state") not in {"completed", "failed"}
        or type(process.get("exitCode")) is not int
    ):
        return "unknown"
    verification = impact.get('verification') or {}
    if (verification.get('reason')=='assertion_failed' and verification.get('status')=='failed'
            and verification.get('revision')==target['revision']):
        return 'failed'
    if (
        process["state"] == "completed"
        and process["exitCode"] == 0
        and fact["state"] == "completed"
    ):
        return "passed"
    if (
        process["state"] == "failed"
        and process["exitCode"] != 0
        and fact["state"] == "failed"
    ):
        return "failed"
    return "unknown"


def historical_check(ident, fact, target, task):
    """Allow only a persisted same-demand check in the still-selected checkout."""
    from . import code_service as C

    if target.get("kind") != "code":
        return False
    with C.database() as db:
        rows = db.execute("SELECT data FROM operations WHERE thread=? AND json_extract(data,'$.source.actionId')=?",
                          (ident, fact["id"])).fetchall()
    for raw, in rows:
        op = json.loads(raw)
        source = op.get("source", {})
        ws = W.registry()["workspaces"].get(op.get("workspaceId"), {})
        resource = next((r for r in projects.scope(ident)["resources"] if r["id"] == target["id"]), {})
        if (op.get("operation") == "check" and op.get("state") == "completed"
                and op.get("resourceId") == target["id"] and op.get("beforeRevision") == target["revision"]
                and source.get("runId") == fact.get("runId")
                and source.get("logicalTaskId") == task.get("id") and source.get("demandRevision") == task.get("revision")
                and resource.get("permission") == "read" and resource.get("execute")
                and ws.get("path") == resource.get("path")
                and op.get("target", {}).get("environment") == C.check_environment()
                and op.get("result", {}).get("verification", {}).get("status") in {"passed", "failed"}
                and (not op.get("target", {}).get("retain_output")
                     or op.get("result", {}).get("process", {}).get("retainedOutput", {}).get("versionId"))):
            if op.get("target", {}).get("retain_output"):
                retained = op["result"]["process"]["retainedOutput"]
                try:
                    artifacts.read_version(ident, retained["artifactId"], retained["versionId"])
                except (W.Problem, OSError, KeyError):
                    return False
            return True
    return False


def requirement_signature(data):
    return signature([(r["id"], r["revision"]) for r in data["requirements"]])


def coverage_signature(data, target=None):
    return signature(
        dict(
            coverage=[
                c for c in data["coverage"] if target is None or c["target"] == target
            ],
            findings=[f for f in data["findings"] if same_item(f["target"], target)],
        )
    )


def same_item(bound, target):
    return (
        target is None
        or bound["kind"] == target["kind"]
        and bound["id"] == target["id"]
    )


def history_page(values, before, target):
    rows = [
        dict(row, position=i + 1)
        for i, row in reversed(list(enumerate(values)))
        if i + 1 < before and same_item(row["target"], target)
    ]
    return rows[:10], len(rows) > 10, rows[9]["position"] if len(rows) > 10 else None


# `workbench.py` 请求会话交付审查。
# `before`、`finding_before` 和 `decision_before` 分别约束要求、发现和决定的页；必需项还按当前逻辑任务及目标筛选。
def overview(
    ident, data, target, sv, before=51, finding_before=101, decision_before=101, *, requirement_scope=None
):
    root = sv._resolve_sid(ident)
    task = (W.read(Path(root) / "session.json").get("taskInput") or {}) if root else {}
    rows = []
    selected = [r for r in reversed(data["requirements"]) if r["position"] < before]
    if requirement_scope == "required":
        selected = [r for r in selected if r.get("required")
                    and r.get("logicalTaskId") in {None, task.get("id")}
                    and (not target or same_item(r["targetRef"], target))]
    elif requirement_scope == "optional":
        selected = [r for r in selected if not r.get("required")]
    for requirement in selected[:10]:
        facts = []
        for kind in ("automatic", "model", "human"):
            proof = next(
                (
                    c
                    for c in reversed(data["coverage"])
                    if c["requirementId"] == requirement["id"]
                    and c["kind"] == kind
                    and (
                        not target
                        or c["target"]["kind"] == target["kind"]
                        and c["target"]["id"] == target["id"]
                    )
                ),
                None,
            )
            if not proof:
                continue
            result = dict(
                proof,
                stale=bool(target and proof["target"] != target)
                or proof["requirementRevision"] != requirement["revision"]
                or bool(requirement.get("required") and (
                    proof.get("logicalTaskId") != task.get("id")
                    or proof.get("demandRevision") != task.get("revision")
                )),
            )
            result["status"] = {"model": "opinion", "human": "confirmed"}.get(
                kind, "unknown"
            )
            if target is None:
                result["status"] = "target_unselected"
            if kind == "automatic" and target is not None and not result["stale"]:
                try:
                    result["status"] = automatic_result(ident, proof, target, sv)
                except (W.Problem, OSError, ValueError):
                    result["status"] = "unknown"
            if result["stale"] or result["status"] == "stale":
                result.update(stale=True, status="stale")
            facts.append(result)
        rows.append(
            dict(
                requirement,
                coverage=facts,
                uncovered=target is None or not any(not f["stale"] for f in facts),
            )
        )
    decision_rows, decision_more, decision_next = history_page(
        data["decisions"], decision_before, target
    )
    finding_rows, finding_more, finding_next = history_page(
        data["findings"], finding_before, target
    )
    open_findings = sum(
        f["state"] == "open" and same_item(f["target"], target)
        for f in data["findings"]
    )
    decisions = [
        dict(
            d,
            scopeUnknown=target is None,
            stale=bool(target and d["target"] != target)
            or d["requirementsRevision"] != requirement_signature(data)
            or d["coverageRevision"] != coverage_signature(data, d["target"]),
        )
        for d in decision_rows
    ]
    return dict(
        revision=data["revision"],
        target=target,
        requirements=rows,
        decisions=decisions,
        findings=[
            dict(
                f,
                scopeUnknown=target is None,
                stale=bool(target and f["target"] != target),
            )
            for f in finding_rows
        ],
        findingsHasMore=finding_more,
        findingsNextBefore=finding_next,
        decisionsHasMore=decision_more,
        decisionsNextBefore=decision_next,
        totalRequirements=len(data["requirements"]),
        openFindings=open_findings,
        requirementsRevision=requirement_signature(data),
        coverageRevision=coverage_signature(data, target),
        execution=W.RT.status(root) if root else dict(state="unknown", active=False),
        hasMore=len(selected) > 10,
        nextBefore=selected[9]["position"] if len(selected) > 10 else None,
        gaps=bool(open_findings)
        or not data["requirements"]
        or any(
            r["uncovered"]
            or not any(c["status"] in {"passed", "confirmed"} for c in r["coverage"])
            or any(c["status"] in {"failed", "unknown", "stale"} for c in r["coverage"])
            for r in rows
        ),
        gapsProvisional=len(data["requirements"]) > len(rows),
    )


def mutate(ident, body, sv, actor="user", source=None):
    if projects.conversation(ident).get("readonlyEvidence"):
        raise W.Problem("readonly_question", "只读追问不能更改验收记录", 403)
    op = body.get("operation")
    # 模型可登记要求、覆盖和发现；验收决定及发现状态由用户维护，工具调用不构成人工批准。
    if actor != "user" and op not in {"requirement", "coverage", "finding"}:
        raise W.Problem("human_only", "验收和发现状态只能由用户决定", 403)
    key = (source or {}).get("actionId") if actor != "user" else body.get("id")
    if not isinstance(key, str) or not 16 <= len(key) <= 80:
        raise W.Problem("invalid_request", "验收请求需要稳定编号")
    fingerprint = signature(body)
    path, lock = storage(ident)
    with lock:
        data = load(path)
        # 稳定请求编号先幂等回取；同编号不同正文拒绝，新的变更再按账本整数 revision 做并发校验。
        receipt = data["receipts"].get(key)
        if receipt:
            if receipt["hash"] != fingerprint:
                raise W.Problem("id_conflict", "同一请求编号不能更改内容", 409)
            return receipt["result"]
        if body.get("expectedRevision") != data["revision"]:
            raise W.Problem("review_changed", "需求或验收资料已更新，请核对后重试", 409)
        now = time.time()
        record = dict(id=uuid.uuid4().hex, actor=actor, source=source, at=now)
        if op == "requirement":
            text = body.get("text")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                raise W.Problem("invalid_requirement", "要求正文需1—2000字")
            current = next(
                (
                    r
                    for r in data["requirements"]
                    if r["id"] == body.get("requirementId")
                ),
                None,
            )
            required = body.get("required", bool(current and current.get("required")))
            if type(required) is not bool:
                raise W.Problem("invalid_requirement", "required 必须为布尔值")
            directory = sv._resolve_sid(ident)
            task = W.read(Path(directory) / "session.json").get("taskInput") if directory else None
            task = task or {}
            if current and actor != "user" and (current.get("actor") == "user" or current.get("required")):
                raise W.Problem("human_only", "用户要求和必验范围只能由用户更改", 403)
            fields = dict(required=required)
            if required:
                target = body.get("target") or (current or {}).get("targetRef")
                selected = current_target(ident, target)
                fields["targetRef"] = {key: selected[key] for key in ("kind", "id")}
                fields["logicalTaskId"] = (current or {}).get("logicalTaskId", task.get("id"))
            if current:
                history = current.setdefault("history", [])
                if len(history) >= 20:
                    raise W.Problem(
                        "requirement_limit",
                        "此要求已保留20次历史修订，请建立新事项",
                        413,
                    )
                history.append({k: v for k, v in current.items() if k != "history"})
                current.update(
                    text=text.strip(),
                    revision=signature([text.strip(), data["revision"] + 1]),
                    actor=actor,
                    source=source,
                    at=now,
                    **fields,
                )
                record = current
            else:
                if len(data["requirements"]) >= 50:
                    raise W.Problem("requirement_limit", "本事项最多50条要求", 413)
                record.update(
                    text=text.strip(),
                    revision=signature([text.strip(), data["revision"] + 1]),
                    position=len(data["requirements"]) + 1,
                    history=[],
                    **fields,
                )
                data["requirements"].append(record)
        elif op == "coverage":
            if not isinstance(body.get("requirementId"), str) or not body["requirementId"]:
                raise W.Problem("requirement_required", "coverage 缺少 requirementId；使用 inspect_delivery 中已有要求的原 id。空清单无需登记覆盖。")
            if len(data["coverage"]) >= 250:
                raise W.Problem("coverage_limit", "证据关联已达保留上限", 413)
            requirement = next(
                (
                    r
                    for r in data["requirements"]
                    if r["id"] == body.get("requirementId")
                ),
                None,
            )
            kind = body.get("kind")
            if (
                not requirement
                or kind not in {"automatic", "model", "human"}
                or kind == "human"
                and actor != "user"
            ):
                raise W.Problem(
                    "invalid_coverage", "要求不存在或不允许这种判断来源", 403
                )
            selected = bind_target(ident, body.get("target"))
            note = str(body.get("note") or "").strip()
            if not 1 <= len(note) <= 2000:
                raise W.Problem("reason_required", "请说明此证据覆盖什么及其限制")
            record.update(
                requirementId=requirement["id"],
                requirementRevision=requirement["revision"],
                kind=kind,
                target=selected,
                note=note,
            )
            if body.get("actionId"):
                fact = evidence.detail(ident, body["actionId"], sv)
                record.update(
                    actionId=fact["id"], evidenceRevision=fact["evidenceRevision"]
                )
            if kind == "automatic":
                if not record.get("actionId"):
                    raise W.Problem(
                        "evidence_required", "自动检查必须关联实际动作", 409
                    )
                result = automatic_result(ident, record, selected, sv)
                if result in {"unknown", "stale"}:
                    raise W.Problem(
                        "check_unverified",
                        "此证据不能证明当前目标版本的自动检查结果",
                        409,
                    )
            if requirement.get("required"):
                if any(selected[key] != value for key, value in requirement["targetRef"].items()):
                    raise W.Problem("wrong_target", "证据不属于必验项指定的交付目标", 409)
                directory = sv._resolve_sid(ident)
                state = W.read(Path(directory) / "session.json") if directory else {}
                task = state.get("taskInput") or {}
                if not task.get("id") or requirement.get("logicalTaskId") not in {None, task["id"]}:
                    raise W.Problem("wrong_task", "必验项不属于当前任务", 409)
                if kind == "automatic":
                    if ((fact.get("startedAt") or 0) < requirement.get("at", 0)
                            or fact.get("runId") != state.get("run_id") and not historical_check(ident, fact, selected, task)):
                        raise W.Problem("stale_check", "必验项需要要求登记后的当前检查，或同任务同要求同版本的可核对历史回执", 409)
                    if fact.get("runId") != state.get("run_id"):
                        record["sourceRunId"] = fact["runId"]
                record.update(logicalTaskId=task["id"], demandRevision=task["revision"])
            data["coverage"].append(record)
        elif op in {"accept", "reject"}:
            if len(data["decisions"]) >= 100:
                raise W.Problem("decision_limit", "用户决定已达保留上限", 413)
            selected = bind_target(ident, body.get("target"))
            state = overview(ident, data, selected, sv)
            if (
                body.get("confirm") is not True
                or not str(body.get("reason") or "").strip()
            ):
                raise W.Problem("confirmation", "用户决定需要确认和具体依据")
            if state["execution"]["active"] or state["execution"]["state"] == "unknown":
                raise W.Problem(
                    "active_or_unknown",
                    "执行仍在进行或无法确认，请先核对实际生命周期",
                    409,
                )
            if (
                op == "accept"
                and (state["gaps"] or state["gapsProvisional"])
                and body.get("acknowledgeGaps") is not True
            ):
                raise W.Problem(
                    "uncovered_requirements",
                    "仍有缺口或未展开要求，请明确核对后决定",
                    409,
                )
            record.update(
                decision=op,
                target=selected,
                reason=str(body["reason"]).strip()[:2000],
                requirementsRevision=requirement_signature(data),
                coverageRevision=coverage_signature(data, selected),
                acknowledgedGaps=bool(body.get("acknowledgeGaps")),
            )
            data["decisions"].append(record)
        elif op == "finding":
            text = str(body.get("text") or "").strip()
            if not 1 <= len(text) <= 2000 or len(data["findings"]) >= 100:
                raise W.Problem("invalid_finding", "发现正文无效或达到保留上限")
            record.update(
                text=text,
                target=bind_target(ident, body.get("target")),
                state="open",
                history=[],
            )
            data["findings"].append(record)
        elif op == "finding_state":
            finding = next(
                (f for f in data["findings"] if f["id"] == body.get("findingId")), None
            )
            reason = str(body.get("reason") or "").strip()
            state = body.get("state")
            if (
                not finding
                or state not in {"open", "closed"}
                or not 1 <= len(reason) <= 2000
                or len(finding["history"]) >= 30
            ):
                raise W.Problem(
                    "invalid_finding", "发现状态变更需要具体理由，且最多保留30次变更"
                )
            finding["history"].append(
                dict(state=state, reason=reason, at=now, actor="user")
            )
            finding["state"] = state
            record = finding
        else:
            raise W.Problem("invalid_operation", "未知验收操作")
        data["revision"] += 1
        result = dict(record=record, revision=data["revision"])
        if len(data["receipts"]) >= 500:
            raise W.Problem("review_full", "验收操作身份保留已达上限", 413)
        data["receipts"][key] = dict(hash=fingerprint, result=result)
        save(path, data)
        return result


def route(ident, parts, method, q, body, sv):
    if parts:
        raise W.Problem("not_found", "验收入口不存在", 404)
    if method == "POST":
        return 200, mutate(ident, body, sv)
    if method != "GET":
        raise W.Problem("invalid_method", "不支持此验收请求", 405)
    requested = dict(
        kind=(q.get("kind") or [None])[0], id=(q.get("target") or [None])[0]
    )
    selected = current_target(ident, requested) if requested["id"] else None
    path, lock = storage(ident)
    with lock:
        return 200, overview(
            ident,
            load(path),
            selected,
            sv,
            int((q.get("before") or [51])[0]),
            int((q.get("finding_before") or [101])[0]),
            int((q.get("decision_before") or [101])[0]),
        )


def tool_context(root, directory=None):
    from .tools import current_call
    from .code_service import RuntimeView
    from .corrections import view

    context = current_call() or {}
    thread = projects.conversation(context.get("sessionId"))
    if (
        thread.get("readonlyEvidence")
        or Path(W.workspace(thread["workspaceId"])["path"]).resolve()
        != Path(root).resolve()
    ):
        raise W.Problem("wrong_source", "验收资料只能关联当前执行会话", 403)
    directory = (
        Path(directory) if directory else RuntimeView()._resolve_sid(thread["id"])
    )
    if directory is None or directory.name != thread["id"]:
        raise W.Problem("not_found", "实际执行会话不存在", 404)
    return (
        thread,
        dict(
            context, actor="agent", taskId=thread.get("taskId"), threadId=thread["id"]
        ),
        view(directory),
    )


def tool_inspect(root, kind=None, target_id=None, before=51, directory=None, requirement_scope="required"):
    thread, _, sv = tool_context(root, directory)
    if requirement_scope not in {"required", "optional", "all"}:
        raise W.Problem("invalid_requirement_scope", "请选择 required、optional 或 all")
    ident = thread["id"]
    target = current_target(ident, dict(kind=kind, id=target_id)) if target_id else None
    path, lock = storage(ident)
    with lock:
        data = load(path)
    result = overview(ident, data, target, sv, int(before), requirement_scope=requirement_scope)
    resolved = sv._resolve_sid(ident)
    task = (W.read(Path(resolved) / "session.json").get("taskInput") or {}) if resolved else {}
    rows = required_status(ident, data, task, sv, target=target) if task.get("id") else []
    result.update(requirementScope=requirement_scope,
                  requirementConfiguration="configured" if data["requirements"] else "not_configured",
                  requiredRebind=[row for row in rows if row["needsRebind"]],
                  requiredPending=[row for row in rows if not row["needsRebind"]
                                   and row["status"] not in {"passed", "confirmed"}])
    if not data["requirements"]:
        # The human acceptance API keeps its existing acknowledgement rules.
        # The tool must not invent pending requirements for an unconfigured ledger.
        result.update(gaps=bool(result["openFindings"]),
                      notice="未配置结构化验收要求。无需为普通任务创建要求或登记覆盖；引用实际检查回执，未运行的范围如实说明。此状态不表示成果已验收。")
    return dict(ok=True, **result)


def tool_record(root, payload, directory=None):
    thread, source, sv = tool_context(root, directory)
    if not isinstance(payload, dict):
        raise W.Problem("invalid_request", "验收资料必须为对象")
    result = mutate(thread["id"], payload, sv, actor="agent", source=source)
    if payload.get("operation") == "coverage" and not result["record"].get("logicalTaskId"):
        result = dict(result, notice="这条证据关联到非必验项。交付所需的证据请按 inspect_delivery 的必验清单登记。")
    return dict(ok=True, **result)
