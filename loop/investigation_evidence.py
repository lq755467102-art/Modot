"""Version-bound failure handoff; referenced evidence stays in its existing store."""
# 主要引用方包括 `executor_workflow.py` 和 `background_planning.py`。
from copy import deepcopy
import json
from pathlib import Path
from . import code_service as C, evidence, workbench as W, check_material, artifacts


def packet(owner, blocker):
    action = blocker.get("latestActionId") or (blocker.get("observedBinding") or {}).get("actionId")
    receipt = deepcopy(owner.state.get("environmentChecks", {}).get(action, {}))
    data = dict(schemaVersion=1, issueId=blocker["issueId"], taskBinding=owner._task_binding(),
                question=blocker["goal"], resourceId=blocker["resourceId"], failedAction=receipt,
                attempts=deepcopy(blocker.get("attempts", [])[-3:]),
                requirements=(owner.state.get("effectiveRequirements") or {}).get("contentHash"),
                blueprint=deepcopy(owner.state.get("blueprint")), remainingSeconds=90)
    data["needsEvidence"] = not bool(receipt.get("operationId"))
    # Dispatch identity excludes attempt counters, wording, action/run IDs and queue revisions.
    data["generation"] = W.digest(json.dumps([data["taskBinding"], data["issueId"],
        [receipt.get(k) for k in ("revision", "argv", "material", "environment", "diagnostics")]],
        sort_keys=True, ensure_ascii=False).encode())
    # Keep a parseable short handoff; full fields remain readable through the operation reference.
    original = receipt.get("diagnostics") or {}
    receipt["diagnostics"] = {key: original.get(key, [])[:3] for key in ("failedCases", "environmentBlockers")}
    receipt["diagnostics"]["keyErrors"] = original.get("keyErrors", [])[-3:]
    for attempt in data["attempts"]:
        for key,value in list(attempt.items()):
            if isinstance(value,str):attempt[key]=value[:320]
    if len(json.dumps(receipt.get("argv"),ensure_ascii=False))>2000:
        receipt["argvPreview"] = [str(arg)[:160] for arg in receipt.pop("argv")[:6]]
        receipt["fullArgvRef"] = receipt.get("operationId")
    return evidence.redact(data)


def read(owner, row, kind, action_id=None, relative_path=None, after=0, line=1, line_count=60, query=None):
    owner._ensure_running()
    binding = row.get("failurePacket") or {}
    if binding.get("taskBinding") != owner._task_binding():
        raise W.Problem("investigation_stale", "调查不属于当前任务", 409)
    rid = row["resourceId"]
    C.context_resource(owner.root, rid)  # Recheck current grants on every access.
    if kind == "source":
        from .role_coordination import _excluded
        if not isinstance(relative_path, str) or _excluded(relative_path):
            raise W.Problem("resource_denied", "证据路径受保护", 403)
        result = C.invoke(owner.root, "inspect", rid, relative_path=relative_path, line=line, line_count=line_count)
        return dict(result, source="current_authorized_checkout", originalFailureRevision=binding.get("failedAction", {}).get("revision"),
                    needsRecheck=True)
    refs = [binding.get("failedAction", {}), *row.get("probeEvidence", [])]
    receipt = next((r for r in refs if r.get("actionId") == action_id), None)
    if not receipt or not receipt.get("operationId"):
        return dict(ok=False, code="needs_evidence", missing=["operation_reference"])
    op = C.operation(Path(owner.session.dir).name, receipt["operationId"])
    if (op.get("resourceId") != rid or op.get("source", {}).get("actionId") != action_id
            or op.get("source", {}).get("runId") != receipt.get("runId")):
        raise W.Problem("evidence_binding_mismatch", "调查证据绑定不一致", 409)
    if kind == "metadata":
        return dict(ok=True, evidence=evidence.redact(op))
    if kind == "material":
        material = op.get("target", {}).get("material")
        if not material:
            return dict(ok=False, code="needs_evidence", missing=["material_version"])
        data, _ = check_material.prepare(Path(owner.session.dir).name, dict(material=material))
        if type(after) is not int or not 0 <= after <= len(data):
            raise W.Problem("invalid_cursor", "材料偏移无效")
        text = data.decode("utf8")
        return dict(ok=True, text=evidence.redact(text[after:after+4000]), next=after+4000 if after+4000<len(text) else None,
                    hash=W.digest(data), material=material, sandboxPath=".harness-check/input.py")
    if kind == "log":
        process = (op.get("result") or {}).get("process", {}).get("id")
        if not process:
            return dict(ok=False, code="needs_evidence", missing=["process_log"], started=False)
        args = dict(process_id=process, **({"query": query} if query else {"after": after}))
        return C.invoke(owner.root, "output", rid, **args)
    raise W.Problem("invalid_arguments", "证据类型为 metadata/log/material/source")
