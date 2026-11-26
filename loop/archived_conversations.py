"""Confirmed removal of an archived conversation's app-owned files."""

# 人工确认入口由 workbench 调用；磁盘清理失败时在归档列表保留重试入口。
import re
import shutil
import stat
from pathlib import Path

from . import workbench as W
from .filelock import FileLock
from .local_storage import is_link


def _owned_directory(path):
    """Validate the resolved target before removing a directory on disk."""
    if path.resolve() != path:
        raise W.Problem("invalid_path", "会话数据目录包含链接，无法删除", 409)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise W.Problem("invalid_path", "会话数据目录无效，无法删除", 409)
    return True


def _paths(data, thread):
    runs = W.P.data_root().resolve() / "runs"
    home = runs / "workbench"
    workspace = data["workspaces"].get(thread["workspaceId"], {})
    storage = Path(workspace.get("path", ""))
    if (workspace.get("kind") != "conversation"
            or storage.parent != home / "conversations"
            or not re.fullmatch(r"[0-9a-f]{32}", storage.name)
            or any(other["workspaceId"] == thread["workspaceId"]
                   for other in data["threads"].values() if other["id"] != thread["id"])):
        raise W.Problem("invalid_path", "无法确认会话的内部目录，未删除", 409)
    ident = thread["id"]
    return runs / ident, storage, home / "attachments" / ident


def _mark_deletion(ident):
    from . import check_sessions, documents, subtasks

    with W._lock:
        data = W.registry()
        thread = data["threads"].get(ident)
        if not thread or not thread.get("projectless") or thread.get("subtaskOf"):
            raise W.Problem("not_found", "对话不存在", 404)
        if not thread.get("archived"):
            raise W.Problem("archive_required", "请先归档要删除的会话", 409)
        if thread.get("scheduledPlanId"):
            raise W.Problem("schedule_history_managed", "这条会话是定时计划的运行记录", 409)
        run, storage, imports = _paths(data, thread)
        has_run = _owned_directory(run)
        _owned_directory(storage)
        _owned_directory(imports)
        if not thread.get("deletionPending"):
            def mark(data):
                data["threads"][ident]["deletionPending"] = True

            def guard_and_mark():
                if ((has_run and W.RT.status(run)["active"]) or thread.get("activeDelegation")
                        or any(row["state"] in subtasks.ACTIVE for row in subtasks.listing(ident))
                        or any(job.context["sessionId"] == ident for job in check_sessions.owned_jobs())):
                    raise W.Problem("active_run", "这条会话仍有运行或待处理任务，无法删除", 409)
                W.change_registry(mark)

            if has_run:
                with W.launch_admission(run, wait_for_launch=False) as admitted:
                    if not admitted:
                        raise W.Problem("active_run", "这条会话正在启动，请稍后重试", 409)
                    guard_and_mark()
            else:
                guard_and_mark()
        for rid in thread.get("attachments", {}):
            documents.cancel(ident, rid)
    return thread["workspaceId"], (run, storage, imports)


def delete(ident, body):
    if body.get("confirm") is not True:
        raise W.Problem("confirmation", "请确认删除归档会话")
    if not re.fullmatch(r"[A-Za-z0-9-][A-Za-z0-9_-]{0,99}", ident) or ident.lower() == "workbench":
        raise W.Problem("not_found", "对话不存在", 404)
    gate = FileLock(W.home() / "archive-deletion" / (ident + ".lock"), timeout=0)
    if not gate.acquire():
        raise W.Problem("archive_cleanup_pending", "正在删除此会话，请稍后刷新", 409)
    try:
        workspace_id, paths = _mark_deletion(ident)
        # The persisted marker makes interrupted cleanup retryable. Do not
        # hold the dispatch lock during recursive disk operations.
        try:
            for path in paths:
                if _owned_directory(path):
                    shutil.rmtree(path)
        except OSError as error:
            raise W.Problem("archive_cleanup_pending", "仍有会话文件未能删除，请稍后重试", 409) from error

        def remove(data):
            del data["threads"][ident]
            del data["workspaces"][workspace_id]

        W.change_registry(remove)
    finally:
        gate.release()
    return 200, {"ok": True, "deleted": ident}
