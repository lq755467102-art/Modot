"""Hosted observation of ordinary native children and their model status receipts."""
# 主要引用方包括 `subtasks.py` 和 `context_manager.py`。
import json
import time
from copy import copy
from pathlib import Path

from . import control, projects, subtasks, workbench as W
from .check_sessions import _milliseconds, _waiting_for_results
from .context_manager import fingerprint


def _facts(row):
    result = row.get("result")
    snapshot = row.get("snapshot") or {}
    facts = {key: row[key] for key in ("id", "state", "reason", "goal", "nickname", "kind", "workItemId", "failure")
             if key in row}
    facts.update(parentRunId=row["parentRunId"], childId=row["childId"], childRunId=row.get("childRunId"),
                 sourceRevision=row.get("expectedRevision"), sourceSnapshot=snapshot.get("sourceSnapshot"),
                 worktree=snapshot.get("path"))
    if row["state"] not in subtasks.ACTIVE:
        facts["deliveryId"] = subtasks.delivery_id(row)
    if result:
        facts["resultVersion"] = fingerprint({key: result[key]
            for key in ("runId", "status", "final", "summary", "exitCode") if key in result})
        facts["resultStatus"] = result.get("status")
        if "exitCode" in result:
            facts["exitCode"] = result["exitCode"]
    return facts


def inspect(parent, source, args, directory, validate_live):
    """Read once, or wait for an actionable state in this parent run."""
    wait_ms = _milliseconds(args.get("wait_ms"), "wait_ms")
    task_id = args.get("task_id") or None
    run_id = source["runId"]
    if not directory:
        raise W.Problem("host_required", "子任务查询需要宿主持有父会话目录", 503)
    directory = Path(directory)

    def snapshot():
        if W.RT.read(directory).get("run_id") != run_id:
            raise W.Problem("wrong_run", "父轮次已变化，无法核对子任务", 409)
        scope = projects.scope(parent)
        rows = [subtasks.load(parent, task_id)] if task_id else subtasks.listing(parent)
        ordinary = [row for row in rows if not row.get("worker")]
        if not task_id:
            ordinary = [row for row in ordinary if row["parentRunId"] == run_id]
        visible = subtasks._readable_results(parent, ordinary, scope=scope)
        workers = [row for row in rows if row.get("worker")]
        return scope, ordinary, visible, workers

    def receipt(current, reason=None):
        scope, ordinary, visible, workers = current
        if task_id and ordinary and not visible and reason != "scope_changed":
            raise W.Problem("scope_changed", "子任务资料已超出当前读取范围", 403)
        rows = [_facts(row) for row in sorted(visible, key=lambda row: row["id"])] + workers
        value = dict(ok=True, task=rows[0] if rows else None) if task_id else dict(ok=True, tasks=rows)
        if ordinary or not workers:
            value["statusScope"] = dict(revision=scope["revision"], taskId=task_id)
        if not workers:
            value["statusVersion"] = fingerprint([parent, run_id, value["statusScope"], rows])
        if reason:
            value["waitReason"] = reason
        return value

    if not wait_ms:
        return receipt(snapshot())

    cutoff = time.monotonic() + wait_ms / 1000
    deadline = control.benchmark_deadline(directory, "executor")
    if deadline is not None:
        cutoff = min(cutoff, deadline)
    watch = control.StopWatch(control.path(directory), deadline=deadline)

    def halt():
        if watch.expired():
            return "run_deadline"
        if watch():
            return "stopped"
        return None

    scope_start = None
    with _waiting_for_results(parent, run_id) as wake:
        while True:
            # Register before the durable read. The hint may be duplicate,
            # missing, or for another child in the same parent run.
            wake.clear()
            current = snapshot()
            scope, ordinary, visible, workers = current
            if reason := halt():
                return receipt(current, reason)
            try:
                validate_live()
            except W.Problem:
                if reason := halt():
                    return dict(ok=False, waitReason=reason)
                raise
            if reason := halt():
                return receipt(current, reason)
            if scope_start is None:
                scope_start = scope["revision"]
            elif scope_start != scope["revision"]:
                return receipt(current, "scope_changed")
            if workers and not ordinary:
                return receipt(current)
            candidates = [row for row in visible if row["parentRunId"] == run_id]
            settled = [row for row in candidates if row["state"] not in subtasks.ACTIVE - {"unknown"}]
            pending = len(settled) < len(candidates)
            if settled:
                accepted = subtasks._accepted_deliveries(W.read(directory / "session.json"), settled)
                if not pending or any(subtasks.delivery_id(row) not in accepted for row in settled):
                    return receipt(current, "subtask_ready")
            if not pending:
                return receipt(current, "no_pending_subtasks")
            remaining = cutoff - time.monotonic()
            if remaining <= 0:
                return receipt(current, halt() or "wait_timeout")
            wake.wait(min(.25, remaining))


def for_request(session, body, *, reuse=True):
    """Keep status in live scope and reference only complete visible receipts."""
    if not hasattr(body, "parts"):
        return body
    parent = Path(session.dir).name
    run_id = session.run_id
    calls = set()
    scope_revision = None
    anchors = {}
    replacements = {}

    for index, message in enumerate(body["messages"]):
        for call in message.get("tool_calls") or ():
            if call.get("function", {}).get("name") == "inspect_subtasks":
                calls.add(call["id"])
        text = message.get("content")
        if (message.get("role") != "tool" or message.get("tool_call_id") not in calls
                or not isinstance(text, str) or '"statusScope"' not in text):
            continue
        try:
            receipt = json.loads(text)
        except ValueError:
            continue
        if not isinstance(receipt, dict):
            continue
        scope = receipt.get("statusScope")
        ref = receipt.get("_harnessEvidence")
        if not isinstance(scope, dict) or not isinstance(ref, dict) or not ref.get("actionId"):
            continue
        if scope_revision is None:
            try:
                scope_revision = projects.scope(parent)["revision"]
            except W.Problem:
                scope_revision = ""
        if scope.get("revision") != scope_revision:
            replacements[index] = dict(statusView="scope_changed", _harnessEvidence=ref,
                notice="当前读取范围已变化，可重新查询有效状态。")
            continue
        if not reuse or ref.get("runId") != run_id:
            continue
        selector = scope.get("taskId")
        if selector is not None and not isinstance(selector, str):
            continue
        task = receipt.get("task")
        rows = ([task] if isinstance(task, dict) else []) if selector else receipt.get("tasks")
        version = receipt.get("statusVersion")
        if (not isinstance(rows, list) or not all(isinstance(row, dict) and "state" in row and "id" in row
                                                  for row in rows)
                or version != fingerprint([parent, run_id, scope, rows])
                or receipt.get("truncated") or receipt.get("spill")):
            # A pressure excerpt cannot serve as the reference for a short receipt.
            anchors.pop(selector, None)
            continue
        previous = anchors.get(selector)
        if previous is None or version != previous[0]:
            anchors[selector] = version, ref
            continue
        states = [dict(id=row["id"], state=row["state"]) for row in rows]
        short = dict(ok=True, statusVersion=version, sameAs=previous[1])
        if selector:
            short["task"] = states[0] if states else None
        else:
            short["tasks"] = states
        if "waitReason" in receipt:
            short["waitReason"] = receipt["waitReason"]
        replacements[index] = short

    if not replacements:
        return body
    result = copy(body)
    result["messages"] = list(body["messages"])
    result.parts = dict(body.parts)
    for index, value in replacements.items():
        result["messages"][index] = dict(result["messages"][index], content=json.dumps(value, ensure_ascii=False))
        result.parts.pop(index, None)
    return result
