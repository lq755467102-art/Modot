"""Read-only inventory of Harness-owned files, measured by logical file size."""

# 主要引用方包括 `workbench.py`。

import os
from pathlib import Path
import re
import stat
import threading
import time

from . import workbench as W


CATEGORIES = ("conversations", "agents", "archives", "evidence", "worktrees", "other")
EVIDENCE = ("evidence-output", "evidence-images", "evidence.sqlite3",
            "evidence.sqlite3-wal", "evidence.sqlite3-shm")
_scan_lock = threading.Lock()


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def inventory(data, registry, *, seconds=7, max_entries=250_000):
    runs = data / "runs"
    home = runs / "workbench"
    checkout_root = data.with_name(data.name + "-conversation-worktrees")
    task_root = data / "task-worktrees"
    skipped = 0
    try:
        info = task_root.lstat()
        task_root_available = stat.S_ISDIR(info.st_mode) and not is_link(info)
        if not task_root_available:
            skipped += 1
    except FileNotFoundError:
        task_root_available = False
    except OSError:
        task_root_available = False
        skipped += 1
    threads, workspaces = registry["threads"], registry["workspaces"]
    excluded = {
        Path(os.path.abspath(resource["path"]))
        for project in registry.get("projects", {}).values()
        for resource in project.get("resources", {}).values()
        if resource.get("path")
    }
    excluded.update(Path(workspace["path"]) for workspace in workspaces.values()
                    if workspace.get("kind") == "local")
    scopes = {home / name: "evidence" for name in (
        "attachments", "artifacts", "change-objects", "revert-backups",
        *(name + suffix for name in ("change-history.sqlite3", "code-operations.sqlite3")
          for suffix in ("", "-wal", "-shm")),
    )}
    scopes[home / "subtasks"] = "agents"
    worktrees = {}

    def own_tree(path, thread=None):
        if path.parent not in (checkout_root, home / "worktrees", task_root):
            return
        scopes[path] = "worktrees"
        item = worktrees.setdefault(path, {"name": path.name, "path": str(path), "bytes": 0,
                                          "available": False, "conversations": []})
        if thread:
            label = thread.get("title") or "未命名会话"
            if label not in item["conversations"]:
                item["conversations"].append(label)

    for workspace in workspaces.values():
        if workspace.get("kind") == "worktree":
            own_tree(Path(workspace["path"]))
    for ident, thread in threads.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", ident):
            continue
        bucket = "agents" if thread.get("subtaskOf") else "archives" if thread.get("archived") else "conversations"
        directory = runs / ident
        scopes[directory] = bucket
        scopes.update({directory / name: "evidence" for name in EVIDENCE})
        workspace = workspaces.get(thread.get("workspaceId"), {})
        if workspace.get("kind") == "conversation":
            private = Path(workspace["path"])
            if private.parent == home / "conversations":
                scopes[private] = bucket
        for binding in thread.get("codeWorktrees", {}).values():
            if binding.get("path"):
                own_tree(Path(binding["path"]), thread)
        task = (thread.get("subtaskOf") or {}).get("taskId", "")
        if task_root_available and re.fullmatch(r"[a-f0-9]{32}", task):
            target = task_root / task
            # Report-only children have a task ID but never create a worktree.
            try:
                info = target.lstat()
                if stat.S_ISDIR(info.st_mode) or is_link(info):
                    own_tree(target, thread)
            except FileNotFoundError:
                pass
            except OSError:
                skipped += 1

    categories = {key: dict(id=key, bytes=0, files=0) for key in CATEGORIES}
    # The development data root can also be the source checkout. Account for
    # application-owned top-level files explicitly instead of scanning it.
    roots = [runs, *(data / name for name in (
        "memory", ".harness", "task-environments", "recycled-worktrees", "browser-history.jsonl",
        "harness/charter.json", "harness/sandbox_rules.json", "harness/contain_grants.json", "harness/mode.log",
    )), *worktrees]
    # Prepared check sources use a short physical root; include only this data root's conversations.
    from .prepared_sources import cache_path
    roots.extend(cache_path(runs / ident)[0] for ident in threads
                 if re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", ident))
    from .verification_workspace import retained_roots
    roots.extend(retained_roots(data))
    explicit_roots = set(roots)
    # A registered worktree is scanned only through its explicit root.
    ignored = {home / "worktrees", home / "conversations"}
    pending = [(path, "other", worktrees.get(path)) for path in reversed(roots)]
    pending.extend((path, category, None) for path, category in scopes.items()
                   if path.parent == home / "conversations")
    deadline = time.monotonic() + seconds
    visited = 0
    reason = None
    while pending:
        if visited >= max_entries or time.monotonic() >= deadline:
            reason = "scan_limit"
            break
        path, category, tree = pending.pop()
        if path in excluded or path in ignored:
            continue
        visited += 1
        try:
            info = path.lstat()
            if is_link(info):
                skipped += 1
                continue
            category = scopes.get(path, category)
            if stat.S_ISDIR(info.st_mode):
                # A declared root may itself be nested inside a redirected parent.
                if path.resolve() != path:
                    skipped += 1
                    continue
                if path in worktrees:
                    tree["available"] = True
                with os.scandir(path) as entries:
                    for entry in entries:
                        pending.append((Path(entry.path), category, tree))
                        if len(pending) + visited >= max_entries or time.monotonic() >= deadline:
                            reason = "scan_limit"
                            break
            elif stat.S_ISREG(info.st_mode):
                if path in explicit_roots and path.parent.resolve() != path.parent:
                    skipped += 1
                    continue
                categories[category]["bytes"] += info.st_size
                categories[category]["files"] += 1
                if tree is not None:
                    tree["bytes"] += info.st_size
        except FileNotFoundError:
            if path not in roots and path not in scopes:
                skipped += 1
        except (OSError, RuntimeError, ValueError):
            skipped += 1
    return dict(categories=list(categories.values()),
                totalBytes=sum(item["bytes"] for item in categories.values()),
                scannedAt=time.time(), dataRoot=str(data), skipped=skipped,
                partial=bool(reason or skipped), reason=reason,
                worktrees=sorted(worktrees.values(), key=lambda item: (-item["bytes"], item["name"])))


def route():
    if not _scan_lock.acquire(blocking=False):
        raise W.Problem("storage_busy", "另一个存储扫描正在进行，请稍后刷新。", 409)
    try:
        return 200, inventory(W.P.data_root().resolve(), W.registry())
    finally:
        _scan_lock.release()
