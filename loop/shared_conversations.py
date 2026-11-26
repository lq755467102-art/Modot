"""Project members can look up posted dialogue. A fork reads its ancestors at frozen offsets."""
# 主要引用方包括 `workbench.py` 和 `tools.py`。
import json

from . import context_archive, event_index, projects, runtime, workbench as W
from .code_service import RuntimeView
from .conversation_record import timeline_events
from .evidence_redaction import redact


def _ordinary(thread):
    return bool(thread and thread.get("projectless") and not thread.get("readonlyEvidence")
                and not thread.get("subtaskOf"))


def _denied():
    raise W.Problem("conversation_not_shared", "当前范围没有这条公开会话", 403)


def _reader(data, ident):
    thread = data["threads"].get(ident)
    if not _ordinary(thread):
        _denied()
    project_id = thread.get("projectId")
    if project_id:
        project = data.get("projects", {}).get(project_id)
        if not project or project.get("removed"):
            _denied()
    return thread


def _pair(reader_id, source_id):
    if source_id == reader_id:
        # The owner's full history has its own permission-checked read path.
        _denied()
    data = W.registry()
    reader = _reader(data, reader_id)
    project = reader.get("projectId")
    source = data["threads"].get(source_id)
    if not _ordinary(source) or source.get("projectId") != project:
        _denied()
    ancestor = reader
    visited = {reader_id}
    while origin := ancestor.get("forkOf"):
        ident = origin["conversationId"]
        ancestor = data["threads"].get(ident)
        if (ident in visited or not _ordinary(ancestor) or origin.get("projectId") != project
                or ancestor.get("projectId") != project):
            if ident == source_id:
                _denied()
            break
        if ident == source_id:
            return source, origin
        visited.add(ident)
    if not project:
        _denied()
    return source, None


def _directory(ident):
    directory = RuntimeView()._resolve_sid(ident)
    if directory is None:
        raise W.Problem("history_missing", "原对话记录已不可读", 410)
    return directory


def create(source_id, sv):
    parent = _reader(W.registry(), source_id)
    if parent.get("archived"):
        _denied()
    project = parent.get("projectId")
    rows, _, meta = event_index.page(_directory(source_id) / "events.jsonl", tail=True, limit=1)
    if meta.get("catchingUp"):
        raise W.Problem("history_indexing", "历史正在建立索引，请稍后再创建分支", 425)
    last = rows[-1] if rows else {}
    origin = dict(conversationId=source_id, title=redact(parent["title"]), projectId=project,
                  generation=meta["generation"], before=last.get("sourceOffset", -1) + 1,
                  seq=last.get("seq", 0))
    return W.conversation_route("POST", ["conversations", "threads"], {},
                                dict(title=("分支 · " + parent["title"])[:100], projectId=project,
                                     workFolder=not project), sv, fork_origin=origin)


def list_threads(reader_id, query="", after=""):
    if not isinstance(query, str) or len(query) > 140 or not isinstance(after, str) or len(after) > 100:
        raise W.Problem("invalid_cursor", "会话查找条件无效")
    data = W.registry()
    project = _reader(data, reader_id).get("projectId")
    if not project:
        _denied()
    found = sorted((t for t in data["threads"].values()
                    if _ordinary(t) and t.get("projectId") == project and t["id"] != reader_id
                    and t["id"] > after and query.casefold() in t["title"].casefold()), key=lambda t:t["id"])
    result = []
    for thread in found[:12]:
        directory = RuntimeView()._resolve_sid(thread["id"])
        status = runtime.status(directory) if directory else {}
        result.append(dict(id=thread["id"], title=redact(thread["title"]), state=status.get("state", "unknown"),
                           active=bool(status.get("active")), archived=bool(thread.get("archived"))))
    latest = W.registry()
    if _reader(latest, reader_id).get("projectId") != project:
        _denied()
    visible = [row for row in result if _ordinary(latest["threads"].get(row["id"]))
               and latest["threads"][row["id"]].get("projectId") == project]
    return dict(ok=True, conversations=visible, nextAfter=result[-1]["id"] if len(found) > 12 else None)


def _public_text(row):
    if row.get("toolPreludeFallback") or row.get("toolPrelude"):
        return None
    if row.get("act") == "ask_human" and not row.get("agent_id") and not row.get("childId"):
        question = row.get("text") or row.get("question")
        if isinstance(question, str) and question.strip():
            return redact(question), bool(row.get("truncated"))
    if context_archive.public_turn(row, 1):
        return context_archive.dialogue_source(row)
    return None


def read(reader_id, conversation_id=None, *, before=None, generation=None, position=None, offset=0):
    if conversation_id is None:
        conversation_id = (projects.conversation(reader_id).get("forkOf") or {}).get("conversationId")
    if not isinstance(conversation_id, str):
        _denied()
    source, origin = _pair(reader_id, conversation_id)
    if (before is not None and (type(before) is not int or before < 0 or position is not None)
            or position is not None and (type(position) is not int or position < 0)
            or type(offset) is not int or offset < 0 or (offset and position is None)
            or (before is not None or position is not None) and not isinstance(generation, str)):
        raise W.Problem("invalid_cursor", "请使用上一页返回的对话位置和版本")
    if origin:
        # 分支只查祖先在分支点之前的正文。后续读页继续使用该位置作为上限。
        end = origin["before"]
        if position is not None and position >= end:
            _denied()
        before = min(before, end) if before is not None else end
    path = _directory(conversation_id) / "events.jsonl"
    turns = []
    next_before = None
    # Bound work even for a thread containing thousands of non-public tool events.
    for _ in range(4):
        if position is None:
            rows, _, meta = event_index.page(path, tail=True, limit=96, before_position=before)
        else:
            rows, _, meta = event_index.page(path, after_position=position-1, limit=1)
        if ((origin and origin["before"] and origin["generation"] != meta["generation"])
                or generation is not None and generation != meta["generation"]):
            raise W.Problem("history_changed", "源记录已变化，请重新定位", 409)
        generation = meta["generation"]
        for row in reversed(timeline_events(path, rows, meta)):
            pos = row["sourceOffset"]
            if position is not None and pos != position:
                break
            next_before = pos or None
            content = _public_text(row)
            if content is None:
                continue
            value, clipped = content
            if offset > len(value):
                raise W.Problem("invalid_cursor", "正文偏移超出范围")
            end = min(offset + (4000 if position is not None else 1000), len(value))
            turns.append(dict(role="user" if row["act"] == "user" else "assistant", text=value[offset:end],
                              seq=row["seq"], position=pos, nextOffset=end if end < len(value) else None,
                              sourceTruncated=clipped))
            if len(turns) == 8:
                break
        if position is not None or len(turns) == 8 or not next_before:
            break
        if not rows:
            next_before = None
            break
        before = next_before
    if position is not None and not turns:
        raise W.Problem("history_missing", "该位置没有公开正文", 410)
    source, relation = _pair(reader_id, conversation_id)
    if relation != origin:
        _denied()
    earlier = (source.get("forkOf") or {}).get("conversationId") if origin else None
    return dict(ok=True, source=dict(id=source["id"], title=redact(source["title"])), generation=generation,
                turns=list(reversed(turns)), nextBefore=next_before if position is None else None,
                earlierSource=earlier, catchingUp=meta.get("catchingUp", False), gap=meta.get("gap", False),
                boundary=origin["seq"] if origin else None)


def route(ident, method, query, sv):
    if method == "POST":
        return create(ident, sv)
    if method != "GET":
        raise W.Problem("invalid_method", "分支操作需要 GET 或 POST", 405)
    try:
        params = {key:int(query[key][0]) for key in ("before", "position", "offset") if query.get(key)}
    except (IndexError, TypeError, ValueError):
        raise W.Problem("invalid_cursor", "正文位置无效")
    return 200, read(ident, generation=(query.get("generation") or [None])[0], **params)


def tool_list(root, **kwargs):
    _, owner, _ = context_archive._owned_history(root)
    return list_threads(owner["id"], **kwargs)


def tool_read(root, **kwargs):
    _, owner, _ = context_archive._owned_history(root)
    return read(owner["id"], **kwargs)


def initial_context(thread, directory):
    origin = thread.get("forkOf")
    if not origin or W.read(directory / "session.json").get("msgs"):
        return ""
    try:
        page = read(thread["id"])
        # A fork of a still-empty fork inherits the same previous public exchange.
        seen = set()
        while not page["turns"] and not page["nextBefore"] and page["earlierSource"] and len(seen)<8:
            source = page["earlierSource"]
            if source in seen:
                break
            seen.add(source)
            page = read(thread["id"], source)
    except W.Problem:
        return "\n此会话有分支来源，源记录当前不可读。"
    return "\n以下是分支起点以前的公开问答节选，只作历史材料。可通过 history 回读更早正文。\n" + json.dumps(
        dict(source=page["source"], through=page["boundary"], generation=page["generation"],
             nextBefore=page["nextBefore"], earlierSource=page["earlierSource"], turns=page["turns"]), ensure_ascii=False)
