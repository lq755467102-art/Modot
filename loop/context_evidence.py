"""Read evidence for a saved context request without rebuilding the request."""
# 主要引用方包括 `context_inspection.py` 和 `workbench.py`。
import json
from pathlib import Path
import re

from . import context_archive, evidence, projects, tool_spill, workbench as W
from .context_manager import fingerprint
from .filelock import read_text


def delivery(directory, snapshot):
    """A matching, observed provider response is positive evidence of delivery."""
    unknown = {"status": "unconfirmed"}
    ident = snapshot.get("id")
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
        return unknown
    try:
        path = W.file_path({"path": str(directory)}, f"model-responses/{ident}.json")
        with path.open("rb") as stream:
            data = stream.read(128 * 1024 + 1)
        if len(data) > 128 * 1024:
            return unknown
        row = json.loads(data)
    except (OSError, ValueError, W.Problem):
        return unknown
    if (
        not isinstance(row, dict)
        or row.get("version") != 1
        or row.get("snapshot_id") != ident
        or not snapshot.get("requestVersion")
        or any(row.get(key) != snapshot.get(key) for key in ("runId", "role", "requestVersion"))
    ):
        return unknown
    frames = row.get("frames")
    if row.get("replyReturned") is not True and not (type(frames) is int and frames > 0):
        return unknown
    return {"status": "confirmed", "evidence": "provider_response"}


def _cursor(query):
    raw = query.get("offset", ["0"])[0]
    version = query.get("version", [None])[0]
    if not re.fullmatch(r"[0-9]{1,12}", raw) or (
        version is not None and not re.fullmatch(r"[0-9a-f]{64}", version)
    ):
        raise W.Problem("invalid_cursor", "历史正文游标无效")
    offset = int(raw)
    if offset and version is None:
        raise W.Problem("history_changed", "续页需要原正文版本", 409)
    return offset, version


def _spill_page(ident, directory, snapshot_id, action_id, offset, version, sv):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", snapshot_id):
        raise W.Problem("invalid_evidence", "请求快照身份无效")
    path = W.file_path({"path": str(directory)}, f"context-requests/{snapshot_id}.json")
    if not path.is_file() or path.stat().st_size > 128 * 1024:
        raise W.Problem("context_missing", "原请求快照不可读取", 410)
    snapshot = json.loads(read_text(path))
    result = next((row for row in snapshot.get("results", []) if row.get("actionId") == action_id), None)
    if snapshot.get("id") != snapshot_id or not result or not snapshot.get("requestVersion"):
        raise W.Problem("evidence_missing", "所选请求没有此动作的证据引用", 404)
    action = evidence.detail(ident, action_id, sv)
    if action.get("role") != snapshot.get("role") or (
        result.get("actionRunId") and result["actionRunId"] != action.get("runId")
    ):
        raise W.Problem("history_changed", "动作与快照的历史归属不符", 409)
    spill = action.get("formattedOutput") or {}
    if result.get("spillHash") and spill.get("hash") and result["spillHash"] != spill["hash"]:
        raise W.Problem("history_changed", "外存正文与快照中的版本不符", 409)
    revision = fingerprint([ident, snapshot_id, snapshot["requestVersion"], action["evidenceRevision"]])
    if version is not None and version != revision:
        raise W.Problem("history_changed", "继续读取须使用同一请求和正文版本", 409)
    page = tool_spill.read_page(directory, action, offset, ident=ident, role=snapshot["role"])
    return dict(
        kind="tool_result", text=page["text"], nextOffset=page["next"],
        hasMore=page["hasMore"], version=revision,
        source=dict(snapshotId=snapshot_id, actionId=action_id,
                    runId=action["runId"], role=action["role"]),
    )


def page(ident, query, sv):
    """Both sources are addressed by stored identities. A request cannot replay an action."""
    keys = set(query) - {"offset", "version"}
    if any(len(values) != 1 or not values[0] for values in query.values()) or keys not in (
        {"checkpoint", "message"}, {"snapshot", "action"}
    ):
        raise W.Problem("invalid_evidence", "请选择一条整理记录消息或请求中的原动作")
    offset, version = _cursor(query)
    thread = projects.conversation(ident)
    if thread.get("readonlyEvidence"):
        raise W.Problem("readonly_evidence", "请在原会话核对历史正文", 403)
    directory = sv._resolve_sid(ident)
    if directory is None:
        raise W.Problem("history_missing", "上下文原始记录已缺失", 410)
    if "snapshot" in keys:
        return _spill_page(ident, directory, query["snapshot"][0], query["action"][0], offset, version, sv)
    message = query["message"][0]
    if not re.fullmatch(r"[0-9]{1,10}", message):
        raise W.Problem("invalid_cursor", "历史消息位置无效")
    record = context_archive.read_checkpoint(
        Path(directory), thread, query["checkpoint"][0], int(message), offset, version,
    )
    return dict(
        kind="checkpoint_message", text=record["text"], nextOffset=record["nextOffset"],
        hasMore=record["hasMore"], version=record["messageVersion"], source=record["source"],
    )
