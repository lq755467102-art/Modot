"""Filtered dirty-tree baselines without touching the user's HEAD or index.

Git's inline fast-import format avoids invoking attributes, hooks or filters.
https://git-scm.com/docs/git-fast-import ; https://git-scm.com/docs/git-worktree
No environment provisioning and no claim of an OS execution sandbox.
"""

# 主要引用方包括 `subtasks.py`。

import io
import json
from pathlib import Path
import re
import stat
import time
from . import workbench as W, authority_paths, baseline, change_history as H, leaks
from .filelock import atomic_json

EXCLUDED = W.IGNORE | {
    ".harness",
    ".ssh",
    ".aws",
    ".codex",
    ".agents",
    ".idea",
    ".vscode",
    ".npm",
    ".cache",
}


# 将 Git 导入的路径编码到字节流中，保证文件名不会改变导入记录边界。
def _quote(path):
    return (
        b'"'
        + b"".join(
            bytes([c])
            if 32 <= c < 127 and c not in (34, 92)
            else ("\\%03o" % c).encode()
            for c in path.encode("utf8")
        )
        + b'"'
    )


# `ws` 是 `subtasks.py` 确认的源检出目录。
# `sealed` 是 `change_history.py` 的旧版本清单。
# 封存路径按对象哈希验证，实时路径另外检测读取期间的变化。
def _filtered(ws, sealed, check):
    if sealed is None:
        names = set(W.safe_git(ws, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"]).stdout.split(b"\0")) - {b""}
        modes = {}
        for row in W.safe_git(ws, ["ls-files", "--stage", "-z"]).stdout.split(b"\0"):
            if row:
                meta, name = row.split(b"\t", 1)
                modes[name.decode("utf8")] = meta.split()[0].decode("ascii")
        entries = {name.decode("utf8"): None for name in names}
    else:
        entries = sealed["files"]
        modes = {}
    if len(entries) > H.MAX_FILES:
        raise W.Problem("snapshot_large", "封存源文件超过历史存储容量", 413)
    source = Path(ws["path"]).resolve()
    guard = baseline.read_snapshot_guard()
    files, excluded, total = {}, 0, 0
    for name, meta in sorted(entries.items()):
        check()
        relative = Path(name)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            raise W.Problem("invalid_path", "Git路径不能越界", 403)
        if any(p in EXCLUDED or p.startswith(".env")
               or p in {"credentials", "id_rsa", "id_ed25519", ".npmrc", ".pypirc"}
               or p.endswith((".pem", ".key", ".p12", ".pfx"))
               for p in (part.casefold() for part in relative.parts)):
            excluded += 1
            continue
        if meta is not None:
            if meta["kind"] == "absent":
                continue
            if meta["kind"] not in {"text", "binary"} or guard(source / relative):
                excluded += 1
                continue
            blob = H.object_path(meta["hash"])
            if blob.is_symlink() or not blob.is_file() or blob.stat().st_size > W.LIMIT:
                raise W.Problem("history_missing", "封存源文件无法核对", 409)
            content = blob.read_bytes()
            if W.digest(content) != meta["hash"]:
                raise W.Problem("history_changed", "封存源文件已经变化", 409)
            mode = meta.get("mode", "100644")
        else:
            lexical = source / relative
            try:
                target = W.file_path(ws, name)
                info = lexical.lstat()
            except FileNotFoundError:
                continue
            except W.Problem as exc:
                if exc.code not in {"path_escape", "protected_app_state", "protected_runtime"}:
                    raise
                excluded += 1
                continue
            mode = modes.get(name, "100644")
            if (target != lexical or not stat.S_ISREG(info.st_mode) or mode in {"120000", "160000"}
                    or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                    or guard(target)):
                excluded += 1
                continue
            if info.st_size > W.LIMIT:
                raise W.Problem("snapshot_large", "单个快照文件超过2MiB，未静默省略", 413)
            content = target.read_bytes()
            current = lexical.lstat()
            if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                    or len(content) != info.st_size or lexical.resolve() != target):
                raise W.Problem("history_race", "封存期间源文件发生变化", 409)
        if leaks.has_secret(content.decode("utf8", "replace")):
            excluded += 1
            continue
        total += len(content)
        if total > H.MAX_SOURCE_BYTES:
            raise W.Problem("snapshot_large", "封存源文件总量超过历史存储容量", 413)
        files[name] = (content, mode if mode in {"100644", "100755"} else "100644")
    return files, excluded, total


# `files` 是过滤后的内容和文件模式。
# 仅通过 `workbench.safe_git` 在子任务工作区生成隔离基线，父检出目录由调用者保持原样。
def _import(ws, branch, head, files):
    message = b"Isolated filtered task baseline"
    stream = io.BytesIO()
    stream.write(b"feature done\ncommit refs/heads/" + branch.encode()
                 + b"\ncommitter Harness <harness@local.invalid> " + str(int(time.time())).encode()
                 + b" +0000\ndata " + str(len(message)).encode() + b"\n" + message
                 + b"\nfrom " + head.encode() + b"\ndeleteall\n")
    for name, (content, mode) in files.items():
        stream.write(b"M " + mode.encode() + b" inline " + _quote(name)
                     + b"\ndata " + str(len(content)).encode() + b"\n")
        stream.write(content)
        stream.write(b"\n")
    stream.write(b"done\n")
    W.safe_git(ws, ["fast-import", "--quiet"], input=stream.getvalue())
    return W.safe_git(ws, ["rev-parse", branch]).stdout.decode().strip()


# 携带预检时重新观察到的整项目 `current_revision`，由子任务派发入口返回给父任务重新核对版本。
class RevisionConflict(W.Problem):
    # 除稳定冲突原因外带回实际版本，供父派发方重新核对。
    def __init__(self, current_revision):
        super().__init__("revision_conflict", "代码版本已变化，且没有可复用的封存版本", 409)
        self.current_revision = current_revision


# 从工作区读源 Git 状态，并在读取期间响应父运行的停止校验。
def _workspace_state(ws, check):
    source = Path(ws["path"]).resolve()
    if authority_paths.protected(source, "write"):
        raise W.Problem(
            "protected_runtime",
            "运行中的宿主仓库不能派生写入工作树，请选择独立开发副本",
            403,
        )
    check()
    state = W.git_status(ws)
    if not state.get("head"):
        raise W.Problem("git_required", "代码子任务需要已有Git基线", 409)
    return state


# 按目录、授权范围、提交和要求的修订寻找可复用基线，缺少匹配才读取封存内容或当前源。
def _revision_source(ws, state, expected_revision, scope, sealed):
    source = Path(ws["path"]).resolve()
    key = W.digest(json.dumps([1, str(source), scope, state["head"], expected_revision], sort_keys=True).encode())
    index = W.home() / "subtask-baselines" / (key + ".json")
    record = W.read(index) if scope else {}
    commit = record.get("baseline", "")
    reused = bool(isinstance(commit, str) and re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit)
                  and W.safe_git(ws, ["cat-file", "-t", commit], check=False).stdout.strip() == b"commit")
    if reused:
        return index, record, None, None
    snapshot_id, manifest = (sealed() if sealed else None) or (None, None)
    if manifest is not None and (manifest["head"] != state["head"]
                                or manifest["coverage"] != "git-visible-source-and-explicit-targets"):
        snapshot_id, manifest = None, None
    if manifest is None and state["revision"] != expected_revision:
        raise RevisionConflict(state["revision"])
    return index, None, snapshot_id, manifest


def preflight(ws, expected_revision, *, scope, sealed, check):
    """Check that the requested source can be used without creating a snapshot."""
    state = _workspace_state(ws, check)
    _revision_source(ws, state, expected_revision, scope, sealed)
    check()


# `subtasks.py` 交入已核对的源修订和任务 ID。
# 取可复用基线或重新过滤文件，之后才建独立工作树；失败时回收尚未使用的分支。
def create(ws, task_id, expected_revision, *, scope=None, sealed=None, check=lambda: None):
    started = time.monotonic()
    state = _workspace_state(ws, check)
    branch = "agent/task-" + task_id
    parent = W.P.data_root() / "task-worktrees"
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / task_id
    if target.exists():
        raise W.Problem("worktree_exists", "子任务工作树已存在，不能重放创建", 409)
    if W.safe_git(ws, ["show-ref", "--verify", "--quiet", "refs/heads/" + branch], check=False).returncode == 0:
        raise W.Problem("worktree_exists", "子任务分支已存在，不能重放创建", 409)
    index, record, snapshot_id, manifest = _revision_source(ws, state, expected_revision, scope, sealed)
    reused = record is not None
    if reused:
        commit = record["baseline"]
    else:
        files, excluded, total = _filtered(ws, manifest, check)
        if manifest is None:
            W.check_revision(ws, expected_revision)
        check()
        commit = _import(ws, branch, state["head"], files)
        record = dict(baseline=commit, sourceSnapshot=snapshot_id, fileCount=len(files),
                      excludedCount=excluded, sourceBytes=total)
        if scope:
            index.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(index, record)
    prepared = time.monotonic()
    check()
    args = ["worktree", "add", "-b", branch, str(target), commit] if reused else ["worktree", "add", str(target), branch]
    try:
        W.safe_git(ws, args)
    except BaseException:
        if not target.exists():
            W.safe_git(ws, ["branch", "-D", branch], check=False)
        raise
    return dict(
        record,
        path=str(target.resolve()),
        branch=branch,
        reusedBaseline=reused,
        timingsMs=dict(baseline=round((prepared-started)*1000), checkout=round((time.monotonic()-prepared)*1000)),
        sourceHead=state["head"],
        sourceRevision=expected_revision,
        environment="未复制依赖目录、用户环境或凭据；检查缺少依赖时需单独处理。过滤不是凭据扫描或操作系统沙箱。",
    )
