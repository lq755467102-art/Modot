"""Resolve saved review and check identities without replaying their actions."""
# 主要引用方包括 `evidence.py`、`action_review.py` 和 `answer_citations.py`。
import json

from . import action_review, artifacts, code_service, delivery_review, evidence, projects, workbench as W


FINDING_TOOL = "record_delivery_evidence"


def review_target_allowed(ident, target):
    """Check the live grant for a version bound in the review ledger."""
    if not isinstance(target, dict) or not isinstance(target.get("id"), str):
        return False
    if target.get("kind") == "code":
        return (isinstance(target.get("revision"), str) and bool(target["revision"])
                and target["id"] in action_review.allowed_resources(ident))
    if target.get("kind") == "artifact":
        try:
            item = artifacts.artifact(ident, target["id"])
        except W.Problem as error:
            if error.status not in {403, 404}:
                raise
            return False
        return any(version["id"] == target.get("versionId") and version["hash"] == target.get("hash")
                   for version in item["versions"])
    return False


def check_action(ident, operation, sv, *, verify_owner=False):
    source = operation.get("source") or {}
    action_id, run_id = source.get("actionId"), source.get("runId")
    if (operation["operation"] != "check" or source.get("threadId") != ident or source.get("sessionId") != ident
            or not action_id or not run_id or not operation.get("beforeRevision")):
        raise W.Problem("evidence_unbound", "检查缺少可核对的原动作或源码版本", 404)
    action = evidence.detail(ident, action_id, sv, verify_owner=verify_owner)
    impact = action.get("impact") or {}
    if (action.get("identityKind") != "action" or action.get("runId") != run_id
            or action.get("tool") != "run_project_check"
            or action_review.input_resource(action) != operation["resourceId"]
            or impact.get("operationId") != operation["id"]):
        raise W.Problem("wrong_source", "检查与原动作的身份无法核对", 409)
    evidence.check_output_access(ident, action)
    return action


def check_process(ident, operation, action):
    process_id = ((action.get("impact") or {}).get("process") or {}).get("id")
    stored_ids = set(filter(None, ((operation.get("result", {}).get("process") or {}).get("id"),
                                   operation.get("processId"))))
    if not process_id:
        raise W.Problem("evidence_missing", "原检查未保留可核对的进程", 404)
    if stored_ids != {process_id}:
        raise W.Problem("wrong_source", "原动作与检查记录的进程身份不同", 409)
    process = W.process_record(process_id)
    source = process.get("source") or {}
    if (process.get("kind") != "check" or process.get("workspaceId") != operation["workspaceId"]
            or process.get("conversationId") != ident or process.get("operationId") != operation["id"]
            or source.get("sessionId") != ident or source.get("runId") != action["runId"]
            or source.get("actionId") != action["id"] or process.get("revision") != operation["beforeRevision"]):
        raise W.Problem("wrong_source", "进程不属于原检查的轮次或源码版本", 409)
    return process_id


def check_output(ident, operation_id, after, expected, sv, *, stream=None):
    projects.conversation(ident)
    operation = code_service.operation(ident, operation_id)
    action = check_action(ident, operation, sv)
    if expected is not None and action["evidenceRevision"] != expected:
        raise W.Problem("evidence_changed", "原检查回执已变化，请重新选择证据", 409)
    page = code_service.output_page(ident, check_process(ident, operation, action), after, stream=stream)
    if expected is not None and evidence.detail(ident, action["id"], sv)["evidenceRevision"] != expected:
        raise W.Problem("evidence_changed", "检查日志读取期间原回执已更新，请重新选择证据", 409)
    evidence.check_output_access(ident, action)
    return page


def retained_action(ident, action, sv):
    output = action.get("output") or {}
    if output.get("state") != "retained" or output.get("id") != action["id"]:
        raise W.Problem("evidence_missing", "原动作的输出未保留", 404)
    page = evidence.output(ident, action["id"], 0, sv)
    if page["evidenceRevision"] != action["evidenceRevision"]:
        raise W.Problem("evidence_changed", "原输出已变化，请重新选择证据", 409)


def check_target(ident, run_id, item_id, sv):
    # A check gap can hold an operation ID or an action ID. A resource/issue
    # ID with the same shape does not establish either relationship.
    with code_service.database() as db:
        rows = db.execute("""SELECT data FROM operations WHERE thread=?
            AND json_extract(data,'$.operation')='check'
            AND json_extract(data,'$.source.runId')=?
            AND (id=? OR json_extract(data,'$.source.actionId')=?) LIMIT 2""",
            (ident, run_id, item_id, item_id)).fetchall()
    if len(rows) != 1:
        raise W.Problem("evidence_ambiguous" if rows else "evidence_unbound", "没有唯一对应的原检查", 404)
    operation = json.loads(rows[0][0])
    action = check_action(ident, operation, sv)
    kind = "check-log"
    try:
        process_id = check_process(ident, operation, action)
        page = code_service.output_page(ident, process_id)
        if not page["text"]:
            raise W.Problem("evidence_missing", "原检查日志未保留或尚未输出", 404)
        evidence.check_output_access(ident, action)
    except W.Problem as error:
        if error.code not in {"evidence_missing", "not_found"}:
            raise
        retained_action(ident, action, sv)
        kind = "action"
    return action, dict(kind=kind, operationId=operation["id"], workspaceId=operation["workspaceId"],
                        version=dict(kind="code", id=operation["resourceId"], revision=operation["beforeRevision"]))


def review_target(ident, run_id, kind, item_id, sv):
    path, lock = delivery_review.storage(ident)
    with lock:
        data = delivery_review.load(path)
    if kind in {"finding", "defect"}:
        record = next((row for row in data["findings"] if row["id"] == item_id), None)
        source = (record or {}).get("source") or {}
        if source.get("sessionId") != ident or source.get("runId") != run_id or not source.get("actionId"):
            raise W.Problem("evidence_unbound", "发现未绑定该轮的原记录动作", 404)
        action = evidence.detail(ident, source["actionId"], sv)
        args = action.get("input")
        payload = args.get("payload") if isinstance(args, dict) else None
        if (action.get("tool") != FINDING_TOOL or action.get("state") != "completed"
                or not isinstance(payload, dict)
                or payload.get("operation") != "finding" or payload.get("target") != record["target"]):
            raise W.Problem("wrong_source", "发现与原登记动作无法核对", 409)
    else:
        field = "requirementId" if kind == "requirement" else "id"
        proofs = [row for row in data["coverage"] if row[field] == item_id and row["kind"] == "automatic"
                  and row.get("actionId") and row.get("evidenceRevision")]
        if not proofs:
            raise W.Problem("evidence_unbound", "未登记自动证据", 404)
        ids = list({row["actionId"] for row in proofs})
        with evidence.projection(evidence.directory(ident, sv)) as (db, state):
            slots = ",".join("?" for _ in ids)
            chosen = {row[0] for row in db.execute(
                f"SELECT id FROM actions WHERE run=? AND id IN ({slots})", (run_id, *ids))}
            if not chosen and state["catchingUp"]:
                raise W.Problem("evidence_indexing", "原始证据仍在补齐索引", 425)
        matches = [row for row in proofs if row["actionId"] in chosen]
        if not matches:
            raise W.Problem("evidence_unbound", "该项没有登记本轮的原检查", 404)
        record = matches[-1]
        if any((row["actionId"], row["evidenceRevision"], row["target"]) !=
               (record["actionId"], record["evidenceRevision"], record["target"]) for row in matches):
            raise W.Problem("evidence_ambiguous", "该轮有多条不同关联，请从本轮证据核对", 404)
        action = evidence.detail(ident, record["actionId"], sv)
        if action["evidenceRevision"] != record["evidenceRevision"]:
            raise W.Problem("evidence_changed", "验收所用原动作的回执已变化", 409)
        if (action.get("tool") != "run_project_check" or record["target"].get("kind") != "code"
                or action_review.input_resource(action) != record["target"]["id"]
                or ((action.get("impact") or {}).get("process") or {}).get("revision") != record["target"]["revision"]):
            raise W.Problem("wrong_source", "检查与验收记录绑定的版本无法核对", 409)
    if action.get("identityKind") != "action" or action.get("runId") != run_id:
        raise W.Problem("wrong_source", "原动作不属于所选轮次", 409)
    if not review_target_allowed(ident, record["target"]):
        raise W.Problem("resource_denied", "证据的源资源已不在当前读取范围", 403)
    retained_action(ident, action, sv)
    return action, dict(kind="action", reviewRecordId=record["id"], version=record["target"])


def completion_review_target(ident, run_id, review_id, sv):
    from .completion_review import record_path, valid_run_id

    if not valid_run_id(review_id):
        raise W.Problem("evidence_unbound", "没有可核对的原生收尾复核记录", 404)
    record = W.read(record_path(evidence.directory(ident, sv), review_id))
    if not isinstance(record, dict) or record.get("ownerRunId") != run_id:
        raise W.Problem("evidence_unbound", "复核记录不属于所选轮次", 404)
    checks = record.get("checkOperations") or {}
    if not isinstance(checks, dict):
        raise W.Problem("evidence_unverifiable", "复核检查引用不可核对", 409)
    candidates = [key for key, ref in checks.items() if isinstance(ref, dict)
                  and ref.get("runId") == run_id and ref.get("operationId") == key]
    if len(checks) != 1 or len(candidates) != 1:
        raise W.Problem("evidence_ambiguous" if checks else "evidence_unbound", "复核没有唯一的同轮检查", 404)
    action, target = check_target(ident, run_id, candidates[0], sv)
    if target["operationId"] != candidates[0] or target["version"]["id"] != checks[candidates[0]].get("resourceId"):
        raise W.Problem("wrong_source", "复核所引用的原检查无法核对", 409)
    return action, dict(target, reviewRecordId=review_id)


def resolve(ident, query, sv):
    keys = ("runId", "kind", "id")
    if set(query) != set(keys) or any(len(query[key]) != 1 for key in keys):
        raise W.Problem("invalid_evidence_target", "证据定位需要 runId、kind 和 id")
    run_id, kind, item_id = (query[key][0] for key in keys)
    if not evidence.ACTION_ID.fullmatch(run_id) or not 1 <= len(kind) <= 32 or not 1 <= len(item_id) <= 128:
        raise W.Problem("invalid_evidence_target", "轮次或缺项身份无效")
    conversation = projects.conversation(ident)
    result = dict(conversationId=ident, runId=run_id, kind=kind, id=item_id,
                  archived=bool(conversation.get("archived")), target=None, reason=None)
    try:
        if kind == "check":
            action, target = check_target(ident, run_id, item_id, sv)
        elif kind in {"requirement", "coverage", "finding", "defect"}:
            action, target = review_target(ident, run_id, kind, item_id, sv)
        elif kind == "review":
            action, target = completion_review_target(ident, run_id, item_id, sv)
        else:
            return dict(result, reason="evidence_unbound")
    except W.Problem as error:
        if error.code not in {"evidence_unbound", "evidence_ambiguous", "evidence_missing", "not_found",
                              "evidence_changed", "evidence_indexing", "evidence_unverifiable", "wrong_source",
                              "resource_denied"}:
            raise
        return dict(result, reason=error.code if error.code != "not_found" else "evidence_missing")
    return dict(result, target=dict(target, conversationId=ident, runId=run_id, actionId=action["id"],
                                    evidenceRevision=action["evidenceRevision"]))
