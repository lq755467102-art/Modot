"""Reuse observed native code pages only inside an authorized model request."""
# 主要引用方包括 `projects.py` 和 `context_manager.py`。
from copy import copy
import json
from pathlib import Path

from . import projects, workbench as W
from .context_manager import fingerprint


_SOURCE = "_harnessCodePage"
_READ_TOOLS = {"inspect_code", "read_project_resource"}


def _binding(project_id, resource):
    keys = ("id", "kind", "path", "permission", "permissionAt", "worktreeOf")
    return fingerprint([project_id, *[resource.get(key) for key in keys]])


def record_source(result, parent, project_id, resource, relative):
    """Record the actual authorized file target alongside the captured revision."""
    from .tools import current_call

    call = current_call()
    if (call and call.get("nativeExecutor") and call.get("role") == "executor"
            and call.get("sessionId") == parent and call.get("tool") in _READ_TOOLS):
        result[_SOURCE] = dict(sessionId=parent, runId=call["runId"], resourceId=resource["id"],
                               path=relative, binding=_binding(project_id, resource),
                               revision=result["revision"])
        # Give a direct reader a pin for the bytes it actually saw. Ordinary file reads
        # stay cheap, and an unavailable check observation must never prevent reading.
        from . import conversation_record
        if project_id is None and conversation_record.NAME.fullmatch(relative):
            ws = conversation_record.check_workspace({'path': resource['path']}, projects.conversation(parent), resource['id'])
            if 'recordBinding' in ws and not (Path(ws['path']) / '.git').exists():
                try:
                    state = W.check_source_state(ws)
                    if state.get('recordPath') == relative and state.get('recordRevision'):
                        result['recordCheckRevision'] = state['revision'] + ':record:' + result['revision']
                except (OSError, W.Problem):
                    pass


def _page_key(receipt, source):
    text = receipt.get("text")
    total = receipt.get("totalChars")
    more = receipt.get("hasMore")
    end = receipt.get("nextOffset") if more is True else total
    if (receipt.get("ok") is not True or not isinstance(text, str) or not text
            or type(total) is not int or type(end) is not int or not len(text) <= end <= total
            or type(more) is not bool or more != (end < total)
            or any(type(receipt.get(key)) is not int for key in ("startLine", "endLine"))
            or receipt.get("revision") != source["revision"]
            or receipt.get("modelExcerpt") or receipt.get("spill")):
        return None
    # The code reader returns a contiguous character slice. The next offset
    # marks its end, including when it stops partway through a long line.
    start = end - len(text)
    return (source["binding"], source["path"], source["revision"], start, end,
            W.digest(text.encode("utf-8")))


def for_request(session, body, *, reuse=True):
    """Filter live scopes early, then shorten only against prior complete pages."""
    from .subtasks import validate_child

    parent = Path(session.dir).name
    calls = set()
    scope = None
    live_bindings = {}
    anchors = {}
    changes = {}
    for index, message in enumerate(body["messages"]):
        for call in message.get("tool_calls") or ():
            if call.get("function", {}).get("name") in _READ_TOOLS:
                calls.add(call["id"])
        text = message.get("content")
        if (message.get("role") != "tool" or message.get("tool_call_id") not in calls
                or not isinstance(text, str) or '"_harnessCodePage"' not in text):
            continue
        try:
            receipt = json.loads(text)
        except ValueError:
            continue
        if not isinstance(receipt, dict) or not isinstance(receipt.get(_SOURCE), dict):
            continue
        source = receipt[_SOURCE]
        ref = receipt.get("_harnessEvidence")
        resource_id = source.get("resourceId")
        if scope is None:
            try:
                scope = projects.scope(parent)
            except W.Problem:
                scope = {}
        if isinstance(resource_id, str) and resource_id not in live_bindings:
            binding = None
            try:
                validate_child(parent, resource_id)
                resource = next((row for row in scope.get("resources", ()) if row["id"] == resource_id), None)
                snapshot = scope.get("lastRun")
                selected = not snapshot or any(row["id"] == resource_id for row in snapshot["resources"])
                if selected and resource and resource["kind"] == "code" and resource["permission"] == "read":
                    root = Path(resource["path"])
                    if root.is_dir() and str(root.resolve()) == resource["path"]:
                        binding = _binding(scope["projectId"], resource)
            except (W.Problem, OSError):
                pass
            live_bindings[resource_id] = binding
        binding = live_bindings.get(resource_id) if isinstance(resource_id, str) else None
        if not binding or source.get("sessionId") != parent or source.get("binding") != binding:
            changes[index] = json.dumps(dict(codePageView="scope_changed", _harnessEvidence=ref,
                notice="这份历史代码页已超出当前读取范围，可重新核对资料授权。"), ensure_ascii=False)
            continue
        if not reuse:
            continue
        del receipt[_SOURCE]
        full = json.dumps(receipt, ensure_ascii=False)
        changes[index] = full
        if (not isinstance(ref, dict) or not ref.get("actionId")
                or ref.get("runId") != session.run_id or source.get("runId") != session.run_id
                or not isinstance(source.get("path"), str) or not isinstance(source.get("revision"), str)):
            continue
        key = _page_key(receipt, source)
        if key is None:
            continue
        if key not in anchors:
            anchors[key] = ref
            continue
        del receipt["text"]
        receipt.update(startOffset=key[3], endOffset=key[4], sameAs=anchors[key])
        short = json.dumps(receipt, ensure_ascii=False)
        # Provider request bodies escape Unicode and embed the receipt as a
        # string. Compare the serialized cost at that boundary.
        if len(json.dumps(short)) < len(json.dumps(full)):
            changes[index] = short

    if not changes:
        return body
    projected = copy(body)
    projected["messages"] = list(body["messages"])
    if hasattr(body, "parts"):
        projected.parts = dict(body.parts)
    for index, content in changes.items():
        projected["messages"][index] = dict(body["messages"][index], content=content)
        if hasattr(projected, "parts"):
            projected.parts.pop(index, None)
    return projected
