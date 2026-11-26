"""Native L0 project shell with per-conversation access and command approvals."""
# 主要引用方包括 `executor_core.py`。

from contextlib import ExitStack, contextmanager
import hashlib
import os
import re
from pathlib import Path
import stat
import tempfile
import time
from . import change_history, code_service as C, exclusive_file, permissions, repository_staging, safe_executor, workbench as W
from .filelock import FileLock
from .tools import current_stop


def inspect_git(root, resource_id, operation="status", directory=".", file=None, commit=None):
    """Inspect the selected checkout using the application's Git in every sandbox mode."""
    from . import baseline
    from .evidence_redaction import redact
    if not isinstance(directory, str) or not directory or (file is not None and not isinstance(file, str)):
        raise W.Problem("invalid_path", "请选择项目内的仓库或文件")
    _, resource, ws, _ = C.context_resource(root, resource_id, register_workspace=False)
    path = W.file_path(ws, directory)
    marker = path / ".git"
    if not path.is_dir() or path != Path(ws["path"]) and path != Path(ws["path"]) / directory:
        raise W.Problem("invalid_path", "请提供项目内的仓库目录")
    if baseline.hit("read", path) or marker.is_symlink() or marker.is_junction():
        raise W.Problem("resource_denied", "当前授权不能读取该仓库", 403)
    if not marker.is_dir() and not (marker.is_file() and resource.get("worktreeOf") and path == Path(ws["path"])):
        raise W.Problem("not_git_repository", "这个目录没有 Git 仓库；仓库在子目录时请选择该子目录")
    commands = {
        "status": ["status", "--short", "--branch", "--untracked-files=normal", "--ignore-submodules=all"],
        "branches": ["for-each-ref", "--count=100", "--format=%(HEAD) %(refname:short) %(objectname:short)", "refs/heads/"],
        "log": ["log", "-20", "--format=%h %s", "--no-notes", "--no-decorate", "--no-show-signature"],
        "diff": ["diff"], "staged": ["diff", "--cached"],
    }
    if operation == "show":
        ref = commit or "HEAD"
        if not isinstance(ref, str) or (ref != "HEAD" and not re.fullmatch(r"[a-fA-F0-9]{4,64}", ref)):
            raise W.Problem("invalid_commit", "请选择提交哈希或 HEAD")
        commands["show"] = ["show", "--no-notes", "--no-show-signature", "--no-ext-diff", "--no-textconv",
                            "--no-renames", "--format=%h %s", "--name-status", ref, "--"]
    if commit is not None and operation != "show":
        raise W.Problem("invalid_arguments", "查看单次提交时才需指定提交哈希")
    if operation not in commands or (file is not None and operation not in {"diff", "staged"}):
        raise W.Problem("invalid_arguments", "请选择状态、分支、提交或差异")
    args = commands[operation]
    if operation in {"diff", "staged"}:
        args += ["--no-color", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "--no-renames"]
        if file is None:
            args += ["--stat"]
        else:
            target = W.file_path({"path": str(path)}, file)
            if target == path or target.is_dir() or baseline.hit("read", target):
                raise W.Problem("resource_denied", "请选择当前仓库内可读的具体文件", 403)
            args += ["--", target.relative_to(path).as_posix()]
    flags = ["--no-pager", "--no-optional-locks", "--no-lazy-fetch", "--no-replace-objects",
             "--literal-pathspecs", "--git-dir=" + str(marker), "--work-tree=" + str(path),
             "-c", "core.quotePath=false", "-c", "log.showSignature=false"]
    out = W.safe_git({"path": str(path)}, flags + args, check=False)
    C.context_resource(root, resource_id, register_workspace=False)
    raw = out.stdout if out.returncode == 0 else out.stderr
    return dict(ok=out.returncode == 0, exitCode=out.returncode, directory=directory, operation=operation,
                output=redact(raw[:32768].decode("utf8", "replace")), truncated=len(raw)>32768)


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _scan(root, hidden):
    files, dirs = {}, set()
    total = 0
    pending = [root]
    while pending:
        folder = pending.pop()
        for path in folder.iterdir():
            rel = path.relative_to(root).as_posix()
            name = path.name.casefold()
            if name in {".harness-check", ".venv", "venv", "__pycache__"}:
                continue
            if (rel.casefold() in hidden or name in safe_executor.EXCLUDED or name in safe_executor.SECRET_NAMES
                    or name.startswith((".env", ".modot-record-")) or name.endswith((".pem", ".key", ".pfx", ".p12"))):
                raise W.Problem("shell_protected_output", "命令改动包含受保护的路径，未写回项目", 403)
            info = path.lstat()
            if (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                raise W.Problem("shell_link", "命令产物包含链接，无法安全同步", 409)
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
                dirs.add(rel)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > safe_executor.MAX_BYTES:
                    raise W.Problem("sandbox_capacity", "命令产物超过执行副本容量，未写回项目", 413)
                files[rel] = _hash(path)
            else:
                raise W.Problem("shell_file", "命令产物包含特殊文件，无法同步", 409)
            if len(files) + len(dirs) > safe_executor.MAX_FILES:
                raise W.Problem("sandbox_capacity", "命令产物条目过多，未写回项目", 413)
    return files, dirs


class WorkspaceChanges:
    def __init__(self, ws, staging, hidden):
        self.ws, self.staging, self.hidden = ws, staging, hidden
        self.changes, self.folders, self.removed_folders = {}, set(), set()
        self.root = Path(ws["path"])
        self.root_key = repository_staging._identity(self.root.lstat())
        self.checkout = W.file_workspace_state(ws)
        self.ready = False
        self.failure = None

    def capture_base(self, root):
        self.base, self.dirs = _scan(root, self.hidden)

    def collect(self, root):
        # Runs after the OS job has stopped every descendant. Never read a live writer.
        import shutil
        try:
            final, dirs = _scan(root, self.hidden)
        except W.Problem as error:
            self.failure = dict(ok=False, code=error.code, error=error.message)
            return self.failure
        self.final = final
        self.folders, self.removed_folders = dirs - self.dirs, self.dirs - dirs
        for rel in self.base.keys() | final.keys():
            if self.base.get(rel) == final.get(rel):
                continue
            target = None
            if rel in final:
                target = self.staging / str(len(self.changes))
                shutil.copyfile(root / rel, target)
            self.changes[rel] = target
        self.ready = True
        return {"changedFiles": len(self.changes)}

    def apply(self, live, thread, resource, owner, process_id):
        if not (self.folders or self.removed_folders or self.changes):
            return dict(ok=True, applied=[])
        root = self.root
        def check():
            live()
            state = W.file_workspace_state(self.ws)
            if state.get("protected") or state.get("branch") == "(detached)":
                raise W.Problem("protected_branch", "请先切换到可写功能分支，项目终端的结果尚未写回", 403)
            if state != self.checkout:
                raise W.Problem("revision_conflict", "终端运行期间工作树或分支已切换，未使用旧版本", 409)

        def dest(rel):
            path = W.agent_file_path(self.ws, rel, write=True)
            if path != root / rel or any(os.path.isreserved(p) for p in Path(rel).parts):
                raise W.Problem("path_escape", "目标文件夹已重定向或文件名无效", 409)
            return path

        kernel = repository_staging._kernel()
        @contextmanager
        def pinned_parent(rel):
            with ExitStack() as pins:
                directory = root
                for part in Path(rel).parts[:-1]:
                    directory /= part
                    pins.enter_context(repository_staging._pin(kernel, directory))
                yield dest(rel)

        applied = []
        change_id = None
        receipts = []
        with FileLock(W.writer_lock_path(root), check=live), repository_staging._pin(kernel, root) as (_, info):
            if repository_staging._identity(info) != self.root_key or root.resolve(strict=True) != root:
                raise W.Problem("revision_conflict", "终端运行期间项目目录被替换", 409)
            check()
            W.guard_workspace_writer(root, C.RuntimeView(), exclude_thread=thread["id"])
            for rel in self.folders | self.removed_folders:
                p = dest(rel)
                if (rel in self.folders and p.exists()) or (rel in self.removed_folders and not p.is_dir()):
                    raise W.Problem("revision_conflict", "目录已被其他编辑者更改：" + rel, 409)
            for rel in self.changes:
                p = dest(rel)
                if (p.exists() and (not p.is_file() or p.stat().st_nlink != 1)
                        or (_hash(p) if p.is_file() else None) != self.base.get(rel)):
                    raise W.Problem("revision_conflict", "项目文件已被其他编辑者更改：" + rel, 409)
            if self.changes:
                operation = dict(id=process_id, operation="write", source=owner, branch=self.checkout["branch"],
                                 target=dict(relative_paths=sorted(self.changes)))
                change_id = change_history.prepare(thread["id"], resource, self.ws, operation,
                                                   check=live, targets_only=True)
            try:
                for rel in sorted(self.folders, key=lambda v: (v.count("/"), v)):
                    check()
                    with pinned_parent(rel) as target:
                        target.mkdir()
                    applied.append(rel + "/")
                for rel, source in sorted(self.changes.items()):
                    check()
                    with pinned_parent(rel) as target, FileLock(W.file_write_lock_path(self.ws, rel), check=live):
                        exclusive_file.write(target, root, source.read_bytes() if source else None,
                                             self.base.get(rel), safe_executor.MAX_BYTES)
                    receipts.append(dict(path=rel, before=self.base.get(rel), after=self.final.get(rel)))
                    applied.append(rel)
                for rel in sorted(self.removed_folders, key=lambda v: (-v.count("/"), v)):
                    check()
                    with pinned_parent(rel) as target:
                        target.rmdir()  # Never remove newly added user files.
                    applied.append(rel + "/")
            except (exclusive_file.Conflict, W.Problem, OSError) as error:
                if change_id:
                    change_history.mark_unknown(thread["id"], change_id)
                return dict(ok=False, code=getattr(error, "code", "shell_sync_conflict"),
                            error=getattr(error, "message", str(error)), applied=applied, changeId=change_id)
            if change_id:
                try:
                    change_history.finish(thread["id"], change_id, self.ws, verified=receipts)
                except (OSError, W.Problem):
                    change_history.mark_unknown(thread["id"], change_id)
                    return dict(ok=False, code="history_unconfirmed", applied=applied, changeId=change_id)
        return dict(ok=True, applied=applied, changeId=change_id)


def run(root, resource_id, argv, timeout=60, outside_reason=None):
    if (not isinstance(argv, list) or not argv or any(not isinstance(a,str) or "\0" in a for a in argv)
            or type(timeout) is not int or not 1 <= timeout <= 120):
        raise W.Problem("invalid_command", "请提供 argv 和 1–120 秒的超时")
    thread, resource, ws, owner = C.context_resource(root, resource_id, "execute")
    if (not owner.get("nativeExecutor") or owner.get("role") != "executor" or
            any(thread.get(k) for k in ("subtaskOf", "readonlyEvidence", "scheduledRun", "archived"))):
        raise W.Problem("shell_scope", "项目终端限当前用户会话的原生执行者", 403)
    mode = permissions.effective_sandbox()
    captured = next((r for r in thread.get("resourceSnapshot", {}).get("resources", []) if r["id"] == resource_id), {})
    if not resource.get("write") or not captured.get("write"):
        mode = "read-only"
    revision = permissions.profile(thread["id"])["revision"]
    stop = current_stop()
    def live():
        if (stop and stop()) or permissions.profile(thread["id"])["revision"] != revision:
            raise W.Problem("approval_revoked", "任务已停止或终端权限已变化", 403)
        C.context_resource(root, resource_id, "execute", register_workspace=False)
        if mode != "read-only":
            C.context_resource(root, resource_id, "write", register_workspace=False)

    from .policy import Policy
    policy = Policy("trusted", root=Path(ws["path"]))
    allowed, why = policy.check("run_check", {"cmd": argv}, kind="exec")
    if not allowed:
        raise W.Problem("policy_denied", why, 403)
    grant = None
    if (outside_reason is not None or mode == "danger-full-access") and argv[0].casefold() in {"git", "git.exe"}:
        executable = W.git_exe()
        if executable:
            argv = [executable, *argv[1:]]
    if outside_reason is not None:
        if mode == "read-only" or not isinstance(outside_reason, str) or not 1 <= len(outside_reason.strip()) <= 1000:
            raise W.Problem("shell_scope", "只读模式不能启动沙箱外命令，跨界请求需要说明原因", 403)
        if mode == "workspace-write":
            grant = permissions.authorize(dict(kind="host-shell", cwd=ws["path"], argv=argv, reason=outside_reason),
                                          validate_live=live)
    live()
    isolated = mode != "danger-full-access" and not grant
    source = W.check_source_state(ws)
    source_revision = W.selected_check_revision(source, False)
    record = source.get("recordPath")
    if ws.get("recordBinding") and "recordPath" not in source:
        from .conversation_record import check_view
        with check_view(ws) as (record, _, _):
            pass
    hidden = {str(record).casefold()} if record else set()
    with tempfile.TemporaryDirectory(prefix="modot-shell-writes-") as folder:
        sync = WorkspaceChanges(ws, Path(folder), hidden) if isolated and mode == "workspace-write" else None
        process = W.launch(ws, argv, source=owner, operation_id=owner.get("actionId"), check=live,
                           force_isolated=isolated, host_grant=grant, expected_revision=source_revision,
                           readonly_source=mode == "read-only", stage_material=sync.capture_base if sync else None,
                           retain_output=sync.collect if sync else None)
        deadline = time.monotonic() + timeout
        ident = process["id"]
        timed_out = False
        try:
            while W.process_record(ident)["state"] in {"starting","running","stopping"}:
                live()
                if time.monotonic() >= deadline:
                    W.stop_process(ident)
                    timed_out = True
                    break
                time.sleep(.1)
        except BaseException:
            W.stop_process(ident)
            raise
        page = W.process_output(ident)
        row = page["process"]
        result = dict(ok=row["state"] == "completed" and row.get("exitCode") == 0,
                      exitCode=row.get("exitCode"), processId=ident, sandboxMode=mode,
                      isolation=row.get("isolation"), output=page["text"], hasMore=page["hasMore"])
        if timed_out:
            live()
            result.update(ok=False, code="shell_timeout", error=f"命令超过 {timeout} 秒，已请求停止。", timeout=timeout,
                          processState=row["state"])
            return result
        if sync and result["ok"]:
            if not sync.ready:
                if sync.failure:
                    result.update(sync.failure)
                    return result
                raise W.Problem("shell_sync_failed", "命令已退出，但未取得可核对的项目改动", 409)
            live()
            result.update(sync.apply(live, thread, resource, owner, ident))
        return result
