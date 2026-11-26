"""Read-only presentation of the exact receipt cited in an assistant reply."""
# 主要引用方包括 `evidence.py`。

from . import action_review, code_service, evidence, evidence_targets, projects, workbench as W
from .evidence_redaction import redact, terminal_command


def _operation(ident, oid):
    try:
        return code_service.operation(ident, oid)
    except W.Problem as error:
        if error.code != "not_found":
            raise


def _source(ident, ref, sv):
    # Older replies sometimes put the operation ID in the action link.
    # Accept that exact ID only if the operation leads back to its original action.
    op = _operation(ident, ref)
    try:
        cited = evidence.detail(ident, ref, sv, verify_owner=True)
    except W.Problem as error:
        if error.code != "evidence_missing" or not op or op["operation"] != "check":
            raise
        cited = evidence_targets.check_action(ident, op, sv, verify_owner=True)
    if op and (op.get("source") or {}).get("actionId") != cited["id"]:
        raise W.Problem("wrong_source", "引用同时指向不同操作", 409)
    review = action_review.graph(ident, cited, 0)
    if review["restricted"]:
        raise W.Problem("resource_denied", review.get("reason") or "这次调用已不在读取范围内", 403)
    if cited["tool"] not in {"read_check_output", "run_project_check"}:
        return cited, None, cited, None

    impact = cited.get("impact") or {}
    if not op and impact.get("operationId"):
        op = code_service.operation(ident, impact["operationId"])
    if not op:
        if cited["tool"] == "read_check_output":
            raise W.Problem("evidence_unbound", "这段输出没有可核对的原检查", 404)
        return cited, None, cited, None
    launcher = evidence_targets.check_action(ident, op, sv, verify_owner=True)
    if cited["tool"] == "run_project_check" and cited["id"] != launcher["id"]:
        raise W.Problem("wrong_source", "启动动作与引用不符", 409)
    process = impact.get("process") or {}
    process_id = None
    if op.get("processId") or (op.get("result") or {}).get("process") or process.get("id"):
        process_id = evidence_targets.check_process(ident, op, launcher)
    if cited["tool"] == "read_check_output":
        args = cited.get("input") or {}
        # Preparation can be polled by operation ID before a process exists.
        by_operation = args.get("operation_id") == op["id"]
        by_process = bool(process_id) and args.get("process_id") == process_id
        if (not (by_operation or by_process) or ("process_id" in args and not by_process)
                or ("operation_id" in args and not by_operation)
                or (process and (process.get("id") != process_id or process.get("revision") != op["beforeRevision"]))
                or action_review.input_resource(cited) != op["resourceId"]):
            raise W.Problem("wrong_source", "这段输出与原检查的进程或版本不符", 409)
    return cited, op, launcher, process_id


def _status(action, result):
    execution = result.get("execution") or {}
    code = result.get("code") or (action.get("impact") or {}).get("code")
    state = execution.get("state") or action["state"]
    if execution.get("timedOut") is True or code == "shell_timeout":
        return "timeout"
    if code == "approval_expired":
        return "approval_expired"
    if state == "cancelled_before_start" or (execution.get("executed") is False and state == "cancelled"):
        return "cancelled_before_start"
    if execution.get("executed") is False or code in {"command_not_found", "unsupported_runtime"}:
        return "not_started"
    if execution.get("cancelled") is True or state in {"cancelled", "interrupted"}:
        return "interrupted"
    if state == "failed" or (type(execution.get("exitCode")) is int and execution["exitCode"] != 0):
        # Distinguish a script's own failure report from a failure to launch that script.
        failure = (result.get("diagnostics") or {}).get("firstFailure") or {}
        trace = (result.get("text") or "")[:8000].rpartition("Traceback (most recent call last):")[2]
        message = failure.get("text") or ""
        if (message.startswith("FileNotFoundError:") and message in trace
                and " in _execute_child" in trace.partition(message)[0]):
            return "child_not_started"
        return "failed"
    if state in {"running", "starting", "stopping", "waiting_approval", "completed"}:
        return state
    return "unknown"


def citation(ident, ref, query, sv):
    if not evidence.ACTION_ID.fullmatch(ref):
        raise W.Problem("invalid_evidence", "证据引用无效")
    question = projects.conversation(ident).get("questionOf") or {}
    parent_reference = question.get("actionId") == ref
    if parent_reference:
        ident = question["conversationId"]
    cited, op, action, pid = _source(ident, ref, sv)
    if parent_reference and (cited["runId"] != question.get("runId")
                             or any(item not in cited["sourceRefs"] for item in question.get("sourceRefs", []))):
        raise W.Problem("wrong_source", "引用与追问所选的原始记录不符", 409)
    result = (op or {}).get("result") or {}
    assessment = result.get("assessment") or {}
    revision = W.digest("|".join([cited["evidenceRevision"], action["evidenceRevision"], (op or {}).get("id", ""), pid or ""]).encode())
    status = _status(action, result)
    if not result and action["tool"] == "run_project_check":
        if op and op.get("state") == "running":
            status = "running" if pid else "starting"
        elif status == "completed":
            status = "unknown"
    header = dict(reference=ref, actionId=action["id"], runId=action["runId"], revision=revision,
                  shell=action["tool"] == "run_project_shell",
                  status=status, isTest=assessment.get("kind") == "test_report",
                  partial=status == "timeout" and bool(assessment.get("testCounts") or result.get("text")))
    if "after" not in query:
        return header
    if query.get("revision", [None])[0] != revision:
        raise W.Problem("evidence_changed", "引用的运行记录已更新，请重新打开", 409)
    cursor = query["after"][0]
    anchor = (cited.get("input") or {}).get("after", 0) if cited["tool"] == "read_check_output" else 0
    if cursor == "cited":
        after = anchor
    elif cursor.isascii() and cursor.isdigit() and len(cursor) < 15:
        after = int(cursor)
    else:
        raise W.Problem("invalid_cursor", "日志位置无效")
    if type(after) is not int or after < 0:
        raise W.Problem("invalid_cursor", "日志位置无效")

    args = (op or {}).get("target") or action.get("input") or {}
    command = terminal_command(args.get("argv"))
    counts = assessment.get("testCounts") or {}
    detail = dict(command=command, after=after,
                  counts={k: counts[k] for k in ("passed", "failed", "errors", "skipped") if type(counts.get(k)) is int},
                  exitCode=(result.get("execution") or {}).get("exitCode"),
                  reason=redact(result.get("error") or action.get("error") or ""))
    if pid:
        stream = (cited.get("input") or {}).get("stream") if cited["tool"] == "read_check_output" else None
        page = evidence_targets.check_output(ident, op["id"], after, action["evidenceRevision"], sv, stream=stream)
        detail.update(text=redact(page["text"]), next=page["next"], hasMore=page["hasMore"], notice=page.get("outputNotice"))
    elif status in {"not_started", "cancelled_before_start", "approval_expired"} or (op and not result):
        detail.update(text="", next=0, hasMore=False)
    else:
        page = evidence.output(ident, action["id"], after, sv, readable=True, live=True)
        shell = page.get("terminal") or {}
        detail.update(text=page["text"] if page.get("hash") or page.get("complete") or shell else "",
                      next=page["next"], hasMore=page["hasMore"], notice=page.get("reason"))
        if shell:
            detail.update(command=shell["command"], exitCode=shell.get("exitCode"))
    evidence.check_output_access(ident, action)
    return dict(header, **detail)
