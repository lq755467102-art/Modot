"""File targets derived from scoped evidence and retained change records."""

# 主要引用方包括 `workbench.py` 和 `evidence.py`。

import json
from pathlib import Path, PureWindowsPath

from . import change_history, code_feedback, projects, workbench as W
from .subtasks import audit_child


CODE_TOOLS = {
    "inspect_code", "write_project_file", "run_project_check", "inspect_git_repository", "run_project_shell",
    "restore_project_file", "prepare_code_branch", "initialize_code_project",
    "edit_project_file", "undo_own_project_write",
}
REPLAY_TOOLS = {
    "read_action_evidence", "read_correction_evidence",
    "read_selected_evidence", "read_context_history",
}
SCOPED_OUTPUT_TOOLS = CODE_TOOLS | REPLAY_TOOLS | {
    "read_code_history", "read_project_resource", "read_artifact", "record_delivery_evidence",
}
CONFLICT_ERRORS = {
    "磁盘文件已改变，请比较后重试", "文件已存在，未覆盖",
    "保存准备期间文件已被外部修改，未替换",
}


def timeline_event(row):
    """Leave navigation metadata in the feed, with excerpts behind evidence access."""
    if 'recordText' in row:
        row = {key: value for key, value in row.items() if key != 'recordText'}
    impact = row.get("impact") or {}
    from .image_evidence import TOOLS

    tool = row.get("tool")
    special = ("imageView" if tool in TOOLS and impact.get("imageView") else
               "imageArtifact" if tool in {"publish_artifact", "transform_image"} and impact.get("imageArtifact") else
               "search" if tool == "call_plugin_tool" and impact.get("search") else None)
    if row.get("tool") not in SCOPED_OUTPUT_TOOLS and not special:
        return row
    from .tools import describe

    args = row.get("args")
    label = describe(row["tool"], args if isinstance(args, dict) else {})
    safe = {key: value for key, value in row.items()
            if key not in {"args", "out", "impact", "path", "error"}}
    if special:
        safe["eventCard"] = special
        if special == "imageView":
            label = "查看图片"
    return dict(safe, summary=label)


def relative_path(value):
    if not isinstance(value, str) or ":" in value or "\0" in value:
        return None
    path = Path(value)
    if not PureWindowsPath(value).anchor and path.parts and not any(p.lower() in {"..", ".git"} for p in path.parts):
        return path.as_posix()
    return None


def allowed_resources(ident):
    scope = projects.scope(ident)
    thread = projects.conversation(ident)
    captured = {r["id"]: r["path"] for r in (scope.get("lastRun") or {}).get("resources", [])}
    allowed = {}
    for resource in scope["resources"]:
        rid = resource["id"]
        if (resource.get("kind") != "code" or resource["permission"] != "read"
                or captured.get(rid) != resource["path"]):
            continue
        try:
            audit_child(ident, rid)
        except W.Problem as error:
            if error.code != "resource_denied":
                raise
        else:
            allowed[rid] = resource
    for origin, binding in thread.get("codeWorktrees", {}).items():
        if projects.switched_code(thread, origin):
            for rid in (origin, binding["id"]):
                resource = projects.retained_code(ident, rid)
                if resource:
                    allowed[rid] = resource
    return allowed


def input_resource(action):
    args = action.get("input")
    rid = args.get("resource_id") if isinstance(args, dict) else None
    return rid if isinstance(rid, str) else None


def target_hint(action, allowed):
    if input_resource(action) not in allowed:
        return None
    args = action["input"]
    return relative_path(args.get("relative_path"))


def scoped_read_restricted(ident, action):
    """Check a retained read against its original target without reading live bytes."""
    tool = action.get("tool")
    try:
        image = (action.get("impact") or {}).get("imageView")
        if image and (image.get("source") or {}).get("resourceId"):
            from .image_evidence import authorize

            authorize(ident, image)
            return False
        if tool == "read_artifact":
            from .artifacts import artifact

            args = action.get("input") or {}
            if not isinstance(args.get("artifact_id"), str):
                return True
            audit_child(ident)
            artifact(ident, args["artifact_id"])
            return False
        if tool == "read_code_history":
            args = action.get("input")
            target = args.get("target") if isinstance(args, dict) else None
            if not isinstance(target, dict) or not isinstance(target.get("id"), str):
                return True
            row = code_feedback.record(ident, target)
            return bool(target.get("revision") and target["revision"] != row.get("revision"))
        rid = input_resource(action)
        if rid is None:
            return True
        audit_child(ident, rid)
        if projects.switched_code(projects.conversation(ident), rid):
            return projects.retained_code(ident, rid) is None
        scope = projects.scope(ident)
        current = next((r for r in scope["resources"] if r["id"] == rid), None)
        if not current or current["permission"] != "read":
            return True
        snapshot = scope.get("lastRun")
        return snapshot is not None and not any(
            r["id"] == rid and r.get("path") == current["path"] for r in snapshot["resources"]
        )
    except W.Problem as error:
        if error.status not in {400, 403, 404, 409, 410}:
            raise
        return True


def graph(ident, action, offset):
    rid = input_resource(action)
    tool = action.get("tool")
    state = action["state"]
    if tool == "record_delivery_evidence":
        from .evidence_targets import review_target_allowed

        args = action.get("input") or {}
        payload = args.get("payload") if isinstance(args, dict) else None
        restricted = not isinstance(payload, dict)
        if not restricted and payload.get("operation") in ("coverage", "finding"):
            restricted = not review_target_allowed(ident, payload.get("target"))
        return dict(files=[], nextOffset=None, restricted=restricted, conflict=False)
    if tool in REPLAY_TOOLS:
        # A replay can contain copied bytes from a separate grant. The
        # original evidence entry is the authoritative place to inspect them.
        return dict(files=[], nextOffset=None, restricted=True, conflict=False,
                    reason="回查结果请从原始证据入口查看。")
    if tool in {"read_code_history", "read_project_resource", "read_artifact"} or ((action.get("impact") or {}).get("imageView", {}).get("source") or {}).get("resourceId"):
        return dict(files=[], nextOffset=None, restricted=scoped_read_restricted(ident, action), conflict=False)
    if tool not in CODE_TOOLS and not rid:
        return dict(files=[], nextOffset=None, restricted=False, conflict=False)
    allowed = allowed_resources(ident)
    with change_history.database() as db:
        rows = db.execute(
            "SELECT data FROM changes WHERE thread=? AND json_extract(data,'$.source.actionId')=? ORDER BY seq",
            (ident, action["id"]),
        ).fetchall()
    records = []
    hidden = bool(tool in CODE_TOOLS and rid and rid not in allowed)
    for (raw,) in rows:
        row = json.loads(raw)
        if row["source"].get("runId") != action.get("runId"):
            continue
        try:
            change_history.authorize(ident, row)
        except W.Problem as error:
            if error.status not in {403, 404, 409, 410}:
                raise
            hidden = True
            continue
        records.append(row)
    args = action.get("input")
    if hidden or (tool in CODE_TOOLS and isinstance(args, dict) and args.get("truncated") and not records):
        return dict(files=[], nextOffset=None, restricted=True, conflict=False)
    conflict = tool in {"write_project_file", "edit_project_file"} and state == "failed" and (
        (action.get("impact") or {}).get("code") == "revision_conflict"
        or action.get("error") in CONFLICT_ERRORS
    )
    files = []
    for row in records:
        if not row.get("revision"):
            continue
        for index, file in enumerate(row["files"]):
            if row["state"] == "unknown" or file["status"] == "unknown":
                kind = "unknown"
            elif file["attribution"] == "verified_file_write":
                kind = file["status"]
            else:
                kind = "observed"
            target = dict(path=file["path"], kind=kind, resourceId=row["resourceId"])
            if kind == "observed":
                target["observedChange"] = file["status"]
            if kind != "unknown":
                target.update(changeId=row["id"], revision=row["revision"], fileIndex=index)
            files.append(target)
    path = target_hint(action, allowed)
    if tool == "inspect_code" and state == "completed" and rid:
        read_path = relative_path((action.get("impact") or {}).get("path"))
        if read_path and read_path == path:
            files.append(dict(path=read_path, kind="read", resourceId=rid))
    if tool in {"write_project_file", "edit_project_file"} and path and not any(f["path"] == path for f in files):
        kind = "conflict" if conflict else "attempt" if state == "failed" else "unknown"
        files.append(dict(path=path, kind=kind, resourceId=rid))
    multiple = len({f["resourceId"] for f in files}) > 1
    if multiple:
        for target in files:
            target["resourceName"] = allowed.get(target["resourceId"], {}).get("name")
    return dict(files=files[offset:offset+100], nextOffset=offset+100 if len(files)>offset+100 else None,
                restricted=False, conflict=conflict)
