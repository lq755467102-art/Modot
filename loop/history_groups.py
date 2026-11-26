"""Fallback for complete, scope-verified tool batches after compaction is exhausted."""
# 主要引用方包括 `context_view.py` 和 `context_manager.py`。
from collections import Counter
from copy import copy
import json
import re

from . import history_compaction, projects, workbench
from .context_manager import ContextBlocked, fingerprint, native_receipts, runtime_update_name, validate_pairs
from .filelock import read_text


# These project reads finish synchronously and do not create operations that
# still need a result, an approval, acceptance, or recovery.
_READS = {"inspect_code", "search_project_code", "read_project_resource"}
_IDENTITY = re.compile("[0-9a-f]{32}")


def group_key(calls):
    return fingerprint(calls)


def index(session):
    """Load omissions independently of summary reuse and current permission."""
    from .context_view import committed_record, summary_path

    try:
        row = json.loads(read_text(summary_path(session)))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        # Legacy summary files had no separately retired tool groups.
        if getattr(session, "_retired_group_commit", None):
            raise ContextBlocked("context_blocked: 历史退出索引无法核对，原记录保留") from None
        return []
    if not isinstance(row, dict) or not row.get("retiredGroups"):
        return []
    identity = row.get("checkpointId")
    try:
        if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity):
            raise ValueError("missing committed history identity")
        stat = (session.dir / "context-checkpoints" / ("cp-" + identity + ".json")).stat()
        cache = identity, fingerprint(row), stat.st_mtime_ns, stat.st_size
        if getattr(session, "_retired_group_commit", None) != cache:
            committed = committed_record(session, identity)
            if committed.get("role") != "executor" or committed.get("workSummary") != row:
                raise ValueError("uncommitted history index")
            session._retired_group_commit = cache
    except (OSError, ValueError, TypeError, AttributeError):
        raise ContextBlocked("context_blocked: 历史退出索引无法核对，原记录保留") from None
    return row["retiredGroups"]


def reference(session, groups):
    """Give the model a small locator without disclosing resource paths or text."""
    if not groups:
        return ""
    try:
        current = projects.read_bindings(projects.history_read_resources(session.dir.name).values())
    except (OSError, workbench.Problem):
        current = {}
    first = next((item for item in groups if projects.covers_reads(current, item["reads"])), None)
    if first is None:
        return "旧工具批次已留档，其中的资料当前不可回读。"
    return (
        "旧工具批次已留档。精确核对时先 use_tool_group(history)，"
        "用 search_conversation_history(kind=checkpoint) 查找，"
        "再用 read_context_history 读取公开消息或工具回执。"
        f"首组 checkpoint_id={first['checkpointId']}，"
        f"message_index={first['firstIndex']} 至 {first['endExclusive'] - 1}。"
        "回读为历史证据，不能重放调用。"
    )


def _batches(messages):
    for i, message in enumerate(messages):
        calls = message.get("tool_calls") or []
        if calls:
            yield i, i + 1 + len(calls), group_key(calls)


def _read_completed(messages, session, by_id, current, requirements, previous):
    """Reject unknown provenance, unfinished reads, and explicit live references."""
    calls = messages[0]["tool_calls"]
    receipts = {m["tool_call_id"]: m for m in messages[1:]}
    for call in calls:
        function = call["function"]
        if function["name"] not in _READS:
            return False
        try:
            args = json.loads(function["arguments"])
            receipt = json.loads(receipts[call["id"]]["content"])
        except (KeyError, TypeError, ValueError):
            return False
        if not isinstance(args, dict) or not isinstance(receipt, dict):
            return False
        if (receipt.get("ok") is not True
                or receipt.get("state") not in (None, "completed", "success")
                or any(receipt.get(key) for key in ("error", "errors", "process", "operationId",
                    "needs_human", "pending", "hasMore", "truncated", "spill", "historicalEvidence"))):
            return False
        resource_id = args.get("resource_id")
        if not isinstance(resource_id, str) or resource_id not in by_id or args.get("resource_ids"):
            return False
        ref = receipt.get("_harnessEvidence")
        if not isinstance(ref, dict):
            return False
        action, run = ref.get("actionId"), ref.get("runId")
        if not all(isinstance(ident, str) and _IDENTITY.fullmatch(ident) for ident in (action, run)):
            return False
        if run != session.run_id:
            if run not in previous:
                previous[run] = projects.retained_run_reads(session.dir, run, "executor")
            bindings = previous[run]
            if (bindings is None or resource_id not in bindings
                    or not projects.covers_reads(current, {resource_id: bindings[resource_id]})):
                return False
        path = args.get("path") or receipt.get("path")
        if (call["id"] in requirements or action in requirements
                or isinstance(path, str) and path not in {"", ".", "/"} and path in requirements):
            return False
    return True


def retire(provider, session, role, body, original, policy, capacity, checkpoint_id, should_stop, exhausted):
    """Select the oldest safe batches, pricing the authorized request after each removal."""
    from .context_view import _projection_state, prefix_version, project, prune_long_results
    from .provider import Interrupted
    from .subtasks import validate_child

    if should_stop and should_stop():
        raise Interrupted("用户请求停止，未退出旧工具批次")
    source = getattr(session, "_context_view_source", None)
    if source is None or not policy["threshold"]:
        raise exhausted
    captured = getattr(session, "context_scope", {}).get("resources")
    if not isinstance(captured, list) or not captured:
        raise exhausted
    try:
        allowed = projects.history_read_resources(session.dir.name)
        for resource in captured:
            validate_child(session.dir.name, resource["id"])
    except workbench.Problem:
        raise exhausted from None
    reads = projects.read_bindings(captured)
    current = projects.read_bindings(allowed.values())
    if not projects.covers_reads(current, reads):
        raise exhausted
    by_id = {ref["id"]: ref for ref in captured
             if ref.get("kind") == "code" and ref.get("permission") == "read"}
    if not by_id:
        raise exhausted

    # Pin exact sources cited by user requirements, active task state, or the
    # newest call. All user messages, public replies and non-read actions remain.
    active = getattr(getattr(session, "executor_core", None), "state", {})
    pinned = [getattr(session, "effective_requirements", {}), active]
    pinned.extend(message for i, message in enumerate(body["messages"])
                  if message.get("role") == "user" and runtime_update_name(message, body.parts.get(i, ())) is None)
    batches = list(_batches(body["messages"]))
    if not batches:
        raise exhausted
    last, end, _ = batches[-1]
    pinned.extend(body["messages"][last:end])
    for first, end, _ in batches[:-1]:
        if any(call["function"]["name"] not in _READS for call in body["messages"][first]["tool_calls"]):
            pinned.extend(body["messages"][first:end])
    requirements = json.dumps(pinned, ensure_ascii=False)
    previous = {}
    positions = body.source_positions
    restored = list(original["messages"])
    source_rows = source["messages"]
    keys = Counter(key for _, _, key in _batches(source_rows))
    row = _projection_state(session, body, prune_long_results(body,
        registry=getattr(getattr(session, "executor_core", None), "registry", None)))
    row.setdefault("sourceCount", 0)
    row.setdefault("sourceVersion", prefix_version(source, 0))
    row.setdefault("text", "")
    retired = row.setdefault("retiredGroups", [])
    removed = []
    original_bytes = len(json.dumps(body, ensure_ascii=False).encode("utf8"))
    for first, end, key in batches[:-1]:
        if should_stop and should_stop():
            raise Interrupted("用户请求停止，未退出旧工具批次")
        start = positions.get(first)
        if (start is None or keys[key] != 1 or end > len(restored)
                or any(positions.get(i) != start + i - first for i in range(first, end))):
            continue
        batch = source_rows[start:start + end - first]
        if (len(batch) != end - first or group_key(batch[0].get("tool_calls")) != key
                or not _read_completed(batch, session, by_id, current, requirements, previous)):
            continue
        # The checkpoint reader uses these exact indices in its original.
        restored[first:end] = batch
        entry = dict(key=key, checkpointId=checkpoint_id, firstIndex=first, endExclusive=end, reads=reads)
        retired.append(entry)
        removed.append(entry)
        candidate = native_receipts(session, project(session, role, source, work_summary=row))
        measurement = history_compaction.measured(provider, candidate, capacity)
        if (measurement["tokens"] >= policy["threshold"]
                or len(json.dumps(candidate, ensure_ascii=False).encode("utf8")) >= original_bytes):
            continue
        validate_pairs(candidate["messages"])
        candidate.work_summary = row
        archived = copy(original)
        archived["messages"] = restored
        handoff = dict(version=5, strategy="retire-completed-tool-groups", policy=policy,
            sourceIndices=[], retiredUserIndices=[], retiredGroups=removed, historicalActions=[],
            exhaustedReason=str(exhausted),
            userMessages=[dict(index=i, hash=fingerprint(m)) for i, m in enumerate(restored) if m["role"] == "user"],
            instructionHashes=[fingerprint(m) for m in restored if m["role"] in {"system", "developer"}],
            runtimeStateIndices=[i for i, m in enumerate(restored) if runtime_update_name(m, original.parts.get(i, ()))])
        return candidate, handoff, archived
    raise exhausted
