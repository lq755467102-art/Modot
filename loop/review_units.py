"""Bind behavioral acceptance to observed writes and immutable source versions."""

# 主要引用方包括 `subtasks.py` 和 `role_coordination.py`。

from pathlib import Path, PurePosixPath, PureWindowsPath
import time

from . import change_history as H, leaks, workbench as W


def relative_path(value):
    if not isinstance(value, str):
        raise W.Problem("invalid_review_path", "验收路径必须是相对文件路径", 400)
    path = PurePosixPath(value.replace("\\", "/"))
    if not path.parts or path.is_absolute() or PureWindowsPath(value).drive or ".." in path.parts or "\0" in value:
        raise W.Problem("invalid_review_path", "验收路径必须在候选范围内", 400)
    return path.as_posix()


def fingerprints(manifest, paths):
    return {path: manifest["files"].get(path, {"kind": "absent", "hash": None}) for path in paths}


def bind(records, manifest, *, work_item, criterion, watches):
    files = {}
    records = sorted(records, key=lambda row: row["observationOrder"])
    for record in records:
        for change in record["files"]:
            if change.get("attribution") != "verified_file_write":
                continue
            path = change["path"]
            if path in files and files[path] != change["before"]:
                raise W.Problem("review_change_gap", "所选变更缺少中间写入记录", 409)
            files[path] = change["after"]
    if not files or any(meta.get("hash") != manifest["files"].get(path, {}).get("hash")
                        or meta["kind"] != manifest["files"].get(path, {"kind": "absent"})["kind"]
                        for path, meta in files.items()):
        raise W.Problem("review_change_mismatch", "所选写入已改变或没有包含在当前候选", 409)
    paths = sorted(set(files) | set(watches))
    return dict(itemId=work_item, criterion=criterion, changeIds=[row["id"] for row in records],
                changedPaths=sorted(files), fingerprints=fingerprints(manifest, paths),
                throughObservation=records[-1]["observationOrder"])


def refresh(unit, manifest, records):
    current = fingerprints(manifest, unit["fingerprints"])
    changed = {path for path, meta in current.items() if meta != unit["fingerprints"][path]}
    if not changed:
        return None
    matched = {}
    for record in records:
        if record["observationOrder"] <= unit.get("throughObservation", 0):
            continue
        for entry in record["files"]:
            path = entry["path"]
            if path in changed and entry.get("attribution") == "verified_file_write":
                matched[path] = (record["id"], entry["after"])
    attributed = {path: key for path, (key, after) in matched.items()
                  if after["kind"] == current[path]["kind"] and after.get("hash") == current[path].get("hash")}
    return dict(unit, fingerprints=current, unattributedPaths=sorted(changed - attributed.keys()),
                changeIds=list(dict.fromkeys([*unit["changeIds"], *attributed.values()]))[-128:],
                throughObservation=max(unit.get("throughObservation", 0),
                                       max((r["observationOrder"] for r in records), default=0)))


def source_file(meta, path):
    raw = H.raw_content(meta)
    if raw is None:
        raise W.Problem("not_found", "所选文件不在封存候选中", 404)
    text = raw.decode("utf-8-sig")
    if leaks.has_secret(text):
        safe = leaks.candidate_python(text) if Path(path).suffix.lower() == ".py" else None
        if safe is None:
            raise W.Problem("resource_denied", "此封存文件含受保护内容", 403)
        if safe != text:
            raw = safe.encode("utf8")
    return W._file_text(path, raw)


def search(files, path, query, limit, offset, stopped):
    if (not isinstance(query, str) or not query.strip() or len(query) > 200 or "\n" in query or "\r" in query
            or type(limit) is not int or not 1 <= limit <= 30 or type(offset) is not int or not 0 <= offset <= 100000):
        raise W.Problem("invalid_arguments", "请提供单行搜索词、结果上限和续查游标", 400)
    matches, scanned = [], 0
    deadline = time.monotonic() + 5
    for index, (name, meta) in enumerate(files):
        if stopped():
            raise W.Problem("cancelled", "代码搜索已取消", 409)
        if index < offset or (path and name != path and not name.startswith(path + "/")):
            continue
        if time.monotonic() >= deadline:
            return dict(ok=True, matches=matches, filesScanned=scanned, truncated=True, nextOffset=index)
        if meta["kind"] != "text":
            continue
        try:
            text = source_file(meta, name)["text"]
        except W.Problem:
            continue
        scanned += 1
        for line, content in enumerate(text.splitlines(), 1):
            if (at := content.find(query)) >= 0:
                matches.append(dict(path=name, line=line, text=content[max(0, at - 100):at + 260]))
                if len(matches) == limit:
                    return dict(ok=True, matches=matches, filesScanned=scanned, truncated=True)
    return dict(ok=True, matches=matches, filesScanned=scanned, truncated=False)
