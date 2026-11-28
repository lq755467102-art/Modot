"""Versioned engineering control plane. All paths are relative to registered workspaces.

This is an authenticated local-user API, never a tool exposed directly to a model.
Command/terminal/DAP input represents explicit user actions, not agent approvals.
"""

# 主要引用方包括 `tools.py` 和 `projects.py`。

from __future__ import annotations
import codecs
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import threading
import time
import uuid
import re
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from . import paths as P, runtime as RT, netguard as NG, event_index as EI
from . import core_runtime
from . import git_observation
from . import workroom_preview
from .filelock import FileLock, atomic_json, read_text

LIMIT = 2 * 1024 * 1024
TEXT_PAGE_BYTES = 64 * 1024
GIT_CHANGE_PAGE_SIZE = 200
GIT_CHANGE_PAGE_BYTES = 512 * 1024
GIT_CHANGE_REVISION_MAX_BYTES = 6 * 1024 * 1024 - 512
GIT_CHANGE_REVISION_SECONDS = 17
MAX_PROCESS_OUTPUT = 32 * 1024 * 1024
_output_lock = threading.Lock()
IGNORE = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "runs",
}
_lock = threading.RLock()
_LAUNCH_LOCK_TIMEOUT = 10
_processes = {}
_debuggers = {}


class Problem(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


def home():
    d = P.data_root() / "runs" / "workbench"
    d.mkdir(parents=True, exist_ok=True)
    return d


# 读取宿主托管状态文件；调用方可显式提供缺失或不可读时的缺省。
def read(path, default=None):
    try:
        return json.loads(read_text(path))
    except (OSError, ValueError):
        return {} if default is None else default


def digest(value):
    return hashlib.sha256(value).hexdigest()


def git_exe():
    return os.environ.get("HARNESS_GIT") or shutil.which("git")


# 在登记的工作区运行 Git，记录实际读写代价，按调用方的检查要求解释非零结果。
def git(ws, args, input=None, check=True):
    exe = git_exe()
    if not exe:
        raise Problem("unavailable", "未找到 Git，请配置 HARNESS_GIT", 503)
    started = time.monotonic()
    r = subprocess.run(
        [exe, "-c", "core.longpaths=true", "-C", ws["path"], *args],
        input=input,
        capture_output=True,
        timeout=40,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    cost = git_observation.current()
    if cost is not None:
        cost["gitProcesses"] += 1
        cost["gitSeconds"] += time.monotonic() - started
        if os.environ.get('HARNESS_GIT_DIAGNOSTICS') == '1':
            command = next((a for a in args if a in {'config','status','diff','rev-parse'}), 'other')
            label = (cost.get('phase') or 'other') + ':' + command
            row = cost.setdefault('gitByPhase', {}).setdefault(label, dict(count=0, seconds=0))
            row['count'] += 1
            row['seconds'] += time.monotonic() - started
    if check and r.returncode:
        raise Problem("git_error", r.stderr.decode("utf-8", "replace")[-2000:], 409)
    return r


def safe_git(ws, args, input=None, check=True):
    # Read/branch capabilities do not include executing repository hooks.
    hooks = home() / "disabled-git-hooks"
    hooks.mkdir(exist_ok=True)
    config = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + str(hooks)]
    observation = git_observation.current()
    try:
        identity = git_observation.identity(ws) if observation is not None else None
        cached = observation["filters"].get(identity) if identity else None
        if cached and cached[0] == git_observation.config_identity(cached[1]):
            keys = cached[2]
        else:
            cached = None
    except (OSError, ValueError):
        identity = cached = None
    if not cached:
        filters = git(ws, ["config", "--null", "--show-origin", "--includes", "--list"], check=False)
        if filters.returncode:
            raise Problem("git_config", "无法核对 Git 外部过滤器，未执行受限操作", 409)
        dependencies = before_config = None
        if identity:
            try:
                dependencies = git_observation.config_paths(filters.stdout, git_exe(), ws)
                before_config = git_observation.config_identity(dependencies)
                # Establish dependencies first, then observe values between two matching fingerprints.
                # Otherwise a change between the query and its first stamp could cache stale filters.
                filters = git(ws, ["config", "--null", "--show-origin", "--includes", "--list"], check=False)
                if filters.returncode:
                    raise Problem("git_config", "无法核对 Git 过滤器配置", 409)
            except (OSError, ValueError):
                dependencies = None
        tokens = filters.stdout.decode("utf8").split("\0")
        keys = sorted({entry.partition("\n")[0] for entry in tokens[1::2]
                       if re.match(r"^filter\..*\.(clean|smudge|process|required)$", entry.partition("\n")[0], re.I)})
        if identity and dependencies:
            try:
                confirmed = git_observation.config_paths(filters.stdout, git_exe(), ws)
                after_config = git_observation.config_identity(confirmed)
                if confirmed == dependencies and before_config == after_config:
                    observation["filters"][identity] = (after_config, confirmed, keys)
            except (OSError, ValueError):
                pass  # Unresolved config dependency: query again, never assume a TTL.
    if not isinstance(keys, list):
        raise Problem("git_config", "无法核对 Git 外部过滤器，未执行受限操作", 409)
    for key in keys:
        if not re.fullmatch(
            r"filter\.[A-Za-z0-9_.-]+\.(clean|smudge|process|required)", key, re.I
        ):
            raise Problem(
                "git_filter", "Git 过滤器名称无法安全覆盖，请用人工工程入口核对", 409
            )
        config += ["-c", key + ("=false" if key.lower().endswith(".required") else "=")]
    reusable = tuple(args) in {("rev-parse", "HEAD"), ("rev-parse", "--show-toplevel"), ("rev-parse", "--is-inside-work-tree")}
    key = (identity, tuple(args), tuple(config))
    if identity and reusable and input is None and key in observation["reads"]:
        observation["observationHits"] += 1
        return observation["reads"][key]
    result = git(ws, [*config, *args], input=input, check=check)
    if identity and reusable and input is None and result.returncode == 0:
        observation["reads"][key] = result
    return result


# 读取当前工作间持久登记，补齐旧登记需要的结构。
def registry():
    try:
        value = json.loads(read_text(home() / "registry.json"))
        if not isinstance(value, dict) or any(
            key not in value for key in ("workspaces", "threads", "presets", "reviews")
        ):
            raise ValueError("invalid registry")
        if (
            type(value.get("schemaVersion", 1)) is not int
            or not 1 <= value.get("schemaVersion", 1) <= 2
        ):
            raise Problem(
                "data_version", "工作记录版本不兼容，原数据保留；禁止破坏性写入", 409
            )
        return value
    except FileNotFoundError:
        return {"workspaces": {}, "threads": {}, "presets": {}, "reviews": {}}
    except (OSError, ValueError) as error:
        raise Problem(
            "registry_unavailable",
            "工作记录暂时不可读取；原数据已保留，请稍后重试",
            503,
        ) from error


# 用同一全局登记锁读取、应用并保存变更。
def change_registry(fn):
    with FileLock(home() / "registry.lock"):
        data = registry()
        value = fn(data)
        atomic_json(home() / "registry.json", data)
        return value


def workspace(ident):
    w = registry()["workspaces"].get(ident)
    if not w or not Path(w["path"]).is_dir():
        raise Problem("not_found", "工作区不存在或已移动", 404)
    return w


# 将真实检出目录解析为工作区身份，同一目录可沿用已有记录。
def register(path, kind="local", parent=None):
    p = Path(path).resolve(strict=True)
    if not p.is_dir():
        raise Problem("invalid_path", "请选择工程目录")
    ident = digest(os.path.normcase(str(p)).encode())[:20]

    def add(data):
        w = data["workspaces"].setdefault(
            ident,
            {
                "id": ident,
                "path": str(p),
                "name": p.name,
                "kind": kind,
                "parent": parent,
            },
        )
        return w

    return change_registry(add)


# 在事件日志尾部倒查，只返回仍有效的待回应权限提示。
def pending_approval(directory):
    p = Path(directory) / "events.jsonl"
    if not p.exists():
        return None
    with p.open("rb") as f:
        f.seek(max(0, p.stat().st_size - 262144))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("act") == "end" and row.get("status") != "needs_human":
            return None
        if row.get("act") in {"user", "tool", "say"}:
            return None
        if row.get("act") == "needs_human" and row.get("sig"):
            return row
    return None


def file_path(w, relative):
    return file_path_from_root(Path(w["path"]).resolve(), relative)


def file_path_from_root(root, relative):
    """Resolve a file inside a workspace root already canonicalized by the caller."""
    rel = Path(relative)
    if rel.anchor or ".." in rel.parts or (os.name == "nt" and ":" in str(rel)):
        raise Problem("invalid_path", "请使用工作区内的相对路径，不含上级目录", 403)
    if any(x.lower() == ".git" for x in rel.parts):
        raise Problem("invalid_path", "工作区文件接口不提供 Git 元数据", 403)
    p = (root / rel).resolve()
    if not p.is_relative_to(root) or ".git" in {
        x.lower() for x in p.relative_to(root).parts
    }:
        raise Problem("path_escape", "链接指向工作区以外或 Git 元数据", 403)
    return p


# 返回实际文件正文和版本，便于下一次写入或审查核对。
def file_read(w, path, *, editor=False):
    raw = editable_bytes(w, path)
    if editor and workroom_preview.binary_block(raw, first=True):
        raise Problem("binary", "此文件可在工作间只读预览", 415)
    return _file_text(path, raw)


def editable_bytes(w, path):
    p = file_path(w, path)
    if not p.is_file():
        raise Problem("not_found", "不是可读取文件", 404)
    with p.open("rb") as stream:
        data = stream.read(LIMIT + 1) if os.fstat(stream.fileno()).st_size <= LIMIT else None
    if data is None or len(data) > LIMIT:
        raise Problem("large_file", "文件超过 2 MiB 编辑上限，可分页只读打开", 413)
    return data


def decode_utf8_page(raw, offset, size):
    decoder = codecs.getincrementaldecoder("utf-8")()
    text = decoder.decode(raw, final=offset + len(raw) >= size)
    return text, offset + len(raw) - len(decoder.getstate()[0])


def _page_file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [('creation_time', ctypes.c_int64), ('access_time', ctypes.c_int64),
                    ('write_time', ctypes.c_int64), ('change_time', ctypes.c_int64),
                    ('attributes', wintypes.DWORD)]

    _get_file_info = ctypes.WinDLL('kernel32', use_last_error=True).GetFileInformationByHandleEx
    _get_file_info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _get_file_info.restype = wintypes.BOOL


def file_text_stamp(stream):
    info = os.fstat(stream.fileno())
    changed = info.st_ctime_ns
    if os.name == 'nt':
        # Windows stat's ctime is not a metadata change time. Restoring mtime
        # after a write still changes FILE_BASIC_INFO.ChangeTime on NTFS.
        basic = _FileBasicInfo()
        if not _get_file_info(msvcrt.get_osfhandle(stream.fileno()), 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not basic.change_time:
            raise OSError('当前文件系统没有提供可核对的文件变更时间')
        changed = basic.change_time
    return (*_page_file_identity(info), changed)


def file_text_page(w, path, revision, after):
    return workspace_file_page(w, path, revision, after, "page")


def _page_contents(stream, offset, size, view):
    stream.seek(offset)
    if view == "bytes":
        return workroom_preview.bytes_page(stream.read(workroom_preview.BYTE_PAGE), offset, size)
    raw = stream.read(TEXT_PAGE_BYTES)
    if view == "preview":
        fmt = workroom_preview.raster_format(raw) if offset == 0 else None
        if fmt:
            return workroom_preview.image_page(stream, raw, size, fmt)
        # Small files might have failed the editor's check on a later byte.
        probe = raw
        if offset == 0 and len(raw) < size <= LIMIT:
            probe = raw + stream.read(size - len(raw))
            try:
                probe.decode("utf-8")
            except UnicodeDecodeError:
                return workroom_preview.bytes_page(raw, offset, size)
        if workroom_preview.binary_block(probe, first=offset == 0):
            return workroom_preview.bytes_page(raw, offset, size)
    try:
        text, next_offset = decode_utf8_page(raw, offset, size)
    except UnicodeDecodeError as error:
        if view == "preview":
            return workroom_preview.bytes_page(raw, offset, size)
        if offset and error.start == 0 and 0x80 <= error.object[0] < 0xc0:
            raise Problem("invalid_cursor", "文本分页游标须对齐 UTF-8 字符") from error
        raise Problem("encoding", "分页只读支持 UTF-8 文本", 415) from error
    if "\0" in text:
        raise Problem("binary", "此文件不是可分页读取的 UTF-8 文本", 415)
    if offset == 0:
        text = text.removeprefix("\ufeff")
    return dict(text=text, nextOffset=next_offset, hasMore=next_offset < size)


def workspace_file_page(w, path, revision, after, view):
    if (not re.fullmatch(r"[0-9]{1,16}", after)
            or revision is not None and not re.fullmatch(r"[a-f0-9]{64}", revision)):
        raise Problem("invalid_cursor", "文件分页游标无效")
    offset = int(after)
    if offset and revision is None:
        raise Problem("invalid_cursor", "继续读取时需要文件版本")
    with text_file_snapshot(w, path) as (stream, current, size):
        if revision is not None and revision != current:
            raise Problem("revision_conflict", "文件已更新，请从头重新读取", 409)
        if offset > size:
            raise Problem("invalid_cursor", "分页游标超出文件范围")
        contents = _page_contents(stream, offset, size, view)
    return dict(path=path, revision=current, sizeBytes=size, **contents)


@contextmanager
def text_file_snapshot(w, path, *, expected_path=None):
    """Pin the open file and verify that the registered path still names it after a bounded read."""
    p = file_path(w, path)
    if expected_path is not None and p != expected_path:
        raise Problem("resource_denied", "文件路径在读取前已改变，请重新选择", 403)
    staged = w.get('projectView')
    if staged is not None:
        with staged.lock, staged.existing(staged.normalize(str(path))) as (backing, info), backing.open('rb') as stream:
            stamp = file_text_stamp(stream)
            revision = digest(repr((str(p), stamp)).encode('utf8'))
            yield stream, revision, info.st_size
            if file_text_stamp(stream) != stamp:
                raise Problem("revision_conflict", "文件已更新，请从头重新读取", 409)
            staged.guard()
        return
    try:
        info = p.stat()
    except FileNotFoundError as error:
        raise Problem("not_found", "不是可读取文件", 404) from error
    if not stat.S_ISREG(info.st_mode):
        raise Problem("not_found", "不是可读取文件", 404)
    identity = _page_file_identity(info)
    size = info.st_size
    changed = Problem("revision_conflict", "文件已更新，请从头重新读取", 409)
    try:
        with p.open("rb") as stream:
            stamp = file_text_stamp(stream)
            if stamp[:4] != identity:
                raise changed
            current = digest(repr((str(p), stamp)).encode("utf-8"))
            yield stream, current, size
            if (file_text_stamp(stream) != stamp or file_path(w, path) != p
                    or _page_file_identity(p.stat()) != identity):
                raise changed
    except FileNotFoundError as error:
        raise changed from error


# 将保留的文件字节解码为界面文本，编码或大小异常用受控错误记录。
def _file_text(path, b):
    if b"\0" in b:
        raise Problem("binary", "二进制/UTF-16 文件暂不支持文本编辑", 415)
    try:
        text = b.decode("utf-8-sig")
    except UnicodeError:
        raise Problem("encoding", "当前编辑器只写入 UTF-8，未转换原文件", 415)
    return {
        "path": path,
        "text": text,
        "revision": digest(b),
        "bom": b.startswith(codecs.BOM_UTF8),
        "eol": "CRLF" if b"\r\n" in b else "LF",
    }


# 按工作区实际路径列目录，只在该登记目录范围内导航。
def files(w, path=""):
    p = file_path(w, path)
    if not p.is_dir():
        raise Problem("not_found", "目录不存在", 404)
    result = []
    for child in sorted(p.iterdir(), key=lambda f: (not f.is_dir(), f.name.lower())):
        if child.name in IGNORE:
            continue
        try:
            safe = file_path(w, str(child.relative_to(w["path"])))
            result.append(
                {
                    "path": child.relative_to(w["path"]).as_posix(),
                    "name": child.name,
                    "directory": safe.is_dir(),
                }
            )
        except (Problem, OSError):
            continue
        if len(result) >= 1000:
            break
    return result


# `authority_paths.py` 再校验普通文件定位，Agent 的读写不能越过宿主保护范围。
def agent_file_path(w, relative, write=False):
    from . import authority_paths

    candidate = file_path(w, relative)
    reason = authority_paths.protected(
        candidate, "write" if write else "read", workspace_root=w["path"]
    )
    if reason:
        raise Problem(
            reason,
            "应用内部记录、宿主与加载依赖不能通过代码文件工具改写；开发宿主请使用独立工作树",
            403,
        )
    return candidate


# 文件保存分别处理 Windows 独占写和同目录替换；两条路径都在实际写入边界重新核对预期版本。
def _replace_file_bytes(w, relative, raw, expected_revision):
    import tempfile

    p = file_path(w, relative)
    if os.name == "nt":
        return _write_exclusive(w, relative, p, raw, expected_revision)
    info = _file_text(relative, raw)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix="." + p.name + ".")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(p, tmp)
        if digest(p.read_bytes()) != expected_revision:
            raise Problem(
                "revision_conflict", "保存准备期间文件已被外部修改，未替换", 409
            )
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return info


# 用于已有原内容的字节级恢复，根据平台选择实际写入路径和版本检查。
def save_bytes(w, relative, raw, expected_revision):
    if not isinstance(raw, bytes) or len(raw) > LIMIT:
        raise Problem("large_file", "恢复字节内容无效或超过2MiB", 413)
    with FileLock(file_write_lock_path(w, relative)):
        file_path(w, relative)
        if os.name == "nt":
            # Keep the lexical path so the exclusive writer can reject every
            # junction component before opening the final file.
            return _write_exclusive(w, relative, Path(w["path"]) / relative, raw, expected_revision)
        return _replace_file_bytes(w, relative, raw, expected_revision)


# 调用 `exclusive_file.py` 在独占边界提交原版本变更。
def _write_exclusive(w, relative, path, raw, expected_revision):
    from . import exclusive_file

    result = _file_text(relative, raw)
    try:
        exclusive_file.write(
            path, Path(w["path"]), raw, expected_revision, LIMIT
        )
    except exclusive_file.Conflict as error:
        raise Problem(error.code, str(error), 409) from error
    return result


def file_text_bytes(text, old=None):
    raw = text.encode("utf-8")
    if len(raw) > LIMIT:
        raise Problem("large_file", "内容超过 2 MiB", 413)
    if old and old["eol"] == "CRLF":
        raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    if old and old["bom"]:
        raw = codecs.BOM_UTF8 + raw
    if len(raw) > LIMIT:
        raise Problem("large_file", "转换行尾后内容超过 2 MiB", 413)
    return raw


def create_file_bytes(w, relative, raw):
    with FileLock(file_write_lock_path(w, relative)):
        p = file_path(w, relative)
        info = _file_text(relative, raw)
        if len(raw) > LIMIT:
            raise Problem("large_file", "内容超过 2 MiB", 413)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            with p.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise Problem("revision_conflict", "文件已存在，未覆盖", 409)
        return info


def save_file(w, b):
    # 新建用排他创建，不能用 create 绕过已有文件的版本冲突保护。
    path = b["path"]
    if b.get("create") is True:
        return create_file_bytes(w, path, file_text_bytes(str(b.get("text", ""))))
    with FileLock(file_write_lock_path(w, path)):
        old = file_read(w, path, editor=True)
        if old["revision"] != b.get("expectedRevision"):
            raise Problem("revision_conflict", "磁盘文件已改变，请比较后重试", 409)
        raw = file_text_bytes(str(b["text"]), old)
        return _replace_file_bytes(w, path, raw, old["revision"])


# 在当前工作区检索文件名或受限内容，返回可定位的结果。
def search(w, query, content=False):
    if not query:
        return []
    needle = (query if content else query.replace("\\", "/")).casefold()
    result = []
    for base, dirs, names in os.walk(w["path"], followlinks=False):
        allowed = []
        for directory in dirs:
            if directory.casefold() in IGNORE:
                continue
            try:
                info = os.lstat(os.path.join(base, directory))
            except OSError:
                continue
            if not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                allowed.append(directory)
        dirs[:] = allowed
        for name in names:
            rel = (Path(base) / name).relative_to(w["path"]).as_posix()
            if not content:
                if needle in rel.casefold():
                    result.append({"path": rel, "line": 1, "text": rel})
            else:
                try:
                    f = file_read(w, rel)
                    for i, line in enumerate(f["text"].splitlines(), 1):
                        if needle in line.casefold():
                            result.append({"path": rel, "line": i, "text": line[:300]})
                        if len(result) >= 200:
                            return result
                except (Problem, OSError):
                    pass
            if len(result) >= 200:
                return result
    return result


def protected_code_branch(branch):
    return branch in {"master", "main", "develop", "release"}


# 提供当前检出版本。仅在同一观察上下文和工作区仍有效时复用读结果。
def git_status(w, *, reuse=False):
    observation = git_observation.current()
    before = git_observation.workspace_token(w) if observation is not None else None
    key = str(Path(w['path']).resolve())
    cached = observation['states'].get(key) if observation is not None else None
    if reuse and before is not None and cached and cached[0] == before:
        observation['observationHits'] += 1
        return cached[1]
    result = _git_status(w)
    if before is not None and before == git_observation.workspace_token(w):
        observation['states'][key] = (before, result)
    return result


def git_head(w):
    if not git_exe():
        return {"available": False, "reason": "Git 未安装"}
    branch = git(w, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    head = git(w, ["rev-parse", "--verify", "HEAD"], check=False)
    if branch.returncode and head.returncode:
        return {"available": False, "reason": "当前目录不是 Git 仓库"}
    name = branch.stdout.decode("utf-8", "replace").strip() if not branch.returncode else "(detached)"
    return dict(available=True, branch=name, protected=protected_code_branch(name),
                head=head.stdout.decode("ascii").strip() if not head.returncode else None)


def _git_listing(w):
    if not git_exe():
        return {
            "available": False,
            "reason": "Git 未安装",
            "files": [],
            "revision": None,
        }, b""
    observed = safe_git(w, ["--no-optional-locks", "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"], check=False)
    if observed.returncode:
        return {
            "available": False,
            "reason": "当前目录不是 Git 仓库",
            "files": [],
            "revision": None,
        }, b""
    head, branch, entries = None, "(detached)", []
    source = iter(observed.stdout.split(b"\0"))
    for entry in source:
        if entry.startswith(b"# branch.oid "):
            value = entry[13:].decode()
            head = None if value == "(initial)" else value
        elif entry.startswith(b"# branch.head "):
            branch = entry[14:].decode()
        elif entry.startswith((b"1 ", b"2 ")):
            fields = entry.split(b" ", 8 if entry[:1] == b"1" else 9)
            entries.append(fields[1].replace(b".", b" ") + b" " + fields[-1] + b"\0")
            if entry[:1] == b"2":
                entries.append(next(source) + b"\0")
        elif entry.startswith(b"u "):
            fields = entry.split(b" ", 10)
            entries.append(fields[1] + b" " + fields[-1] + b"\0")
        elif entry.startswith(b"? "):
            entries.append(b"?? " + entry[2:] + b"\0")
    raw = b"".join(entries)
    parts = raw.split(b"\0")
    rows = []
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        status = entry[:2].decode()
        path = entry[3:].decode("utf-8", "replace")
        old = None
        if "R" in status or "C" in status:
            old = parts[i].decode("utf-8", "replace")
            i += 1
        rows.append({"path": path, "status": status, "oldPath": old})
    return dict(available=True, branch=branch, head=head, revision=None,
                files=rows, protected=protected_code_branch(branch)), raw


def _git_listing_path(root, name):
    try:
        return file_path_from_root(root, name)
    except Problem as error:
        if error.code not in {"path_escape", "invalid_path"}:
            raise
        return None


# The full revision includes file contents and index changes for guarded writes.
def _git_revision(w, listing, raw, *, deadline=None):
    if not listing["available"]:
        return listing
    root = Path(w["path"]).resolve()
    files, fingerprints = [], []
    for row in listing["files"]:
        if deadline is not None and time.monotonic() >= deadline:
            return listing
        name = row["path"]
        p = _git_listing_path(root, name)
        if p is None:
            # Keep junction descendants in the revision without reading the targets.
            fingerprints.append((name, "outside-workspace"))
            continue
        if p.is_file() and p.stat().st_size <= LIMIT:
            fp = digest(p.read_bytes())
        else:
            fp = str((p.exists(), p.stat().st_size, p.stat().st_mtime_ns)) if p.exists() else "deleted"
        fingerprints.append((name, fp))
        files.append(row)
    staged_changed = any(row["status"][0] not in {" ", "?"} for row in listing["files"])
    staged = (safe_git(w, ["diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv"]).stdout
              if staged_changed else b"")
    revision = digest((listing["head"] or "").encode() + raw + staged + json.dumps(fingerprints).encode())
    return dict(listing, revision=revision, files=files)


def _git_status(w):
    return _git_revision(w, *_git_listing(w))


# A large listing validates only the visible paths and carries no write revision.
def git_changes_page(w, after, snapshot):
    deadline = time.monotonic() + GIT_CHANGE_REVISION_SECONDS
    listing, raw = _git_listing(w)
    rows = listing["files"]
    identity = digest(json.dumps([listing.get("branch"), listing.get("head")]).encode() + raw)
    if after > len(rows) or ((after or snapshot) and snapshot != identity):
        raise Problem("git_changes_changed", "变更列表已变化，请从开头刷新", 409)
    # Preserve exact edit revisions when the complete result fits the existing transport.
    status = listing
    if (len(raw) <= GIT_CHANGE_REVISION_MAX_BYTES
            and len(json.dumps(listing, ensure_ascii=False).encode("utf-8")) <= GIT_CHANGE_REVISION_MAX_BYTES):
        status = _git_revision(w, listing, raw, deadline=deadline)
    if status["revision"] and len(rows) <= GIT_CHANGE_PAGE_SIZE and len(raw) <= GIT_CHANGE_PAGE_BYTES:
        return dict(git=status, changeCount=len(status["files"]), nextOffset=None, snapshot=identity)
    root = Path(w["path"]).resolve()
    files = []
    size = 0
    offset = after
    for row in rows[after:after + GIT_CHANGE_PAGE_SIZE]:
        if _git_listing_path(root, row["path"]) is not None:
            size += len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
            if files and size > GIT_CHANGE_PAGE_BYTES:
                break
            files.append(row)
        offset += 1
    return dict(git=dict(status, files=files), changeCount=len(rows),
                nextOffset=offset if offset < len(rows) else None, snapshot=identity)


def current_revision(w):
    s = git_status(w)
    return s["revision"] or "non-git"


@lru_cache(maxsize=32)
def _folder_fingerprint(root, observed_generation, projection=None):
    from .verification_workspace import fingerprint
    return "folder:" + fingerprint(root, source=True, projection=projection)


def record_revision(value):
    return isinstance(value, str) and value.startswith('folder:') and ':record:' in value


def selected_check_revision(state, include_record):
    return state.get('recordRevision', state['revision']) if include_record else state['revision']


def check_source_state(w, *, reuse=False, fingerprint_folder=True):
    """Identify the source seen by an isolated check, including ordinary local folders."""
    root = Path(w["path"]).resolve(strict=True)
    if local_writer_root(root) != root:
        raise Problem("repository_root_required", "请核对所选工作目录", 403)
    if os.path.lexists(root / ".git"):
        return git_status(w, reuse=reuse)
    # Notifications only authorize reuse within this process. The fingerprint itself
    # must compare equal in the conversation runner and in the command-owning backend.
    revision = "non-git"
    extra = {}
    if fingerprint_folder:
        from .directory_generation import generation
        if w.get('recordBinding'):
            from .conversation_record import check_view
            with check_view(w) as (record, digest, stage):
                observed = generation(root)
                excluded = frozenset(name for name in (record, stage) if name)
                revision = None if observed is None else _folder_fingerprint(root, observed, excluded)
                extra['recordPath'] = record
                extra['recordRevision'] = f"{revision}:record:{digest or 'absent'}" if revision else None
        else:
            observed = generation(root)
            revision = None if observed is None else _folder_fingerprint(root, observed)
    return dict(available=False, files=[], head=None, branch=None, protected=False,
                folderObservation=True, revision=revision, **extra)


# 关键写入前使用实际工作区版本核对预期修订。
def check_revision(w, expected):
    s = git_status(w, reuse=True)
    actual = s['revision'] or 'non-git'
    if not expected or expected != actual:
        raise Problem("revision_conflict", "代码版本已变化，请刷新并重新审阅", 409)


# 根据文件和暂存侧选择 Git 差异，保留观察到的截断边界。
def git_diff(w, path=None, staged=False, *, include_revision=True):
    args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--unified=4"]
    if staged:
        args += ["--cached"]
    if path:
        file_path(w, path)
        args += ["--", path]
    raw = safe_git(w, args).stdout
    if (
        path
        and not raw
        and not staged
        and safe_git(
            w, ["ls-files", "--error-unmatch", "--", path], check=False
        ).returncode
    ):
        f = file_read(w, path)
        lines = f["text"].splitlines()
        raw = (
            "--- /dev/null\n+++ b/"
            + path
            + "\n@@ -0,0 +1,%d @@\n" % len(lines)
            + "\n".join("+" + line for line in lines)
            + "\n"
        ).encode()
    return {
        "text": raw[:LIMIT].decode("utf-8", "replace"),
        "truncated": len(raw) > LIMIT,
        "revision": current_revision(w) if include_revision else None,
    }


# 补充工作间实际可用工具与目录状态，供操作视图选择可用入口。
def capability(w, *, include_git=True):
    result = dict(
        w,
        capabilities={
            "files": True,
            "checks": True,
            "pty": importlib.util.find_spec("winpty") is not None
            if os.name == "nt"
            else False,
            "debug": importlib.util.find_spec("debugpy") is not None,
            "git": bool(git_exe()),
        },
    )
    if include_git:
        result["git"] = git_status(w)
    return result


def record_path(ident):
    if not ident or not all(c in "0123456789abcdef" for c in ident):
        raise Problem("not_found", "无效运行标识", 404)
    return home() / "processes" / (ident + ".json")


# 合并原进程记录与宿主仍持有的状态，没有归属证据时保留不确定。
def shell_process_for_action(thread_id, run_id, action_id, *, process_id=None, historical=False):
    """Match all owner fields. Old timeout receipts predate the process ID field."""
    def matches(row):
        source = row.get("source") or {}
        return (row.get("conversationId") == thread_id == source.get("sessionId")
                and row.get("operationId") == action_id == source.get("actionId")
                and source.get("runId") == run_id and source.get("tool") == "run_project_shell")
    if process_id:
        if not isinstance(process_id,str) or not re.fullmatch("[0-9a-f]{32}",process_id):
            return None
        row = read(record_path(process_id))
        return row if matches(row) else None
    candidates = (home() / "processes").glob("[0-9a-f]" * 32 + ".json") if historical else (
        record_path(key) for key in tuple(_processes))
    found = None
    for file in candidates:
        row = read(file)
        if row.get("id") == file.stem and matches(row):
            if found is not None:
                return None
            found = row
    return found


def process_record(ident):
    r = read(record_path(ident))
    if not r:
        raise Problem("not_found", "运行记录不存在", 404)
    if ident not in _processes and r.get("state") in {
        "running",
        "starting",
        "stopping",
    }:
        actual = RT.identity(r["pid"])
        r["state"] = "unknown" if actual is not None else "interrupted"
    return r


# 将实际进程输出有界写入保留区，分流时同时保留对应流的定位。
def append_output(ident, text, *, stream=None):
    data = text.encode("utf-8", errors="replace")
    if not data:
        return
    directory = home() / "processes"
    path = directory / (ident + ".log")
    with _output_lock:
        size = path.stat().st_size if path.exists() else 0
        room = max(0, MAX_PROCESS_OUTPUT - size)
        marker = directory / (ident + ".output.json")
        if len(data) > room and not marker.exists():
            # Persist the missing-evidence warning before discarding any bytes.
            atomic_json(
                marker,
                dict(
                    truncated=True,
                    outputLimitBytes=MAX_PROCESS_OUTPUT,
                    outputNotice="输出达到32MiB保留上限，后续内容未留存；退出码仍为实际命令结果，日志不能证明完整输出。",
                ),
            )
        if room:
            # Retain a valid UTF-8 prefix. Continue draining the real process even
            # after capacity is reached, so noisy commands can finish normally.
            prefix = data[:room].decode("utf-8", errors="ignore").encode("utf-8")
            with path.open("ab") as f:
                f.write(prefix)
            if stream in {'stdout', 'stderr'}:
                with (directory/(ident+'.'+stream+'.log')).open('ab') as f:
                    f.write(prefix)


# 终端、调试和预览依赖主机交互能力，在当前隔离模式下返回对应的受限原因。
def host_transport_block(kind, mode):
    if mode == "danger-full-access":
        return None
    label = {"terminal": "交互终端", "debug": "Python 调试", "preview": "本地应用预览"}.get(kind)
    return f"当前隔离模式不支持{label}。" if label else None


def require_host_transport(kind, mode):
    reason = host_transport_block(kind, mode)
    if reason:
        raise Problem("sandbox_transport", reason, 503)


# 在已登记的检出目录启动进程并关联源动作。需要核对项目版本的检查同时记录该版本。
def launch(w, argv, kind="check", rows=24, cols=100, on_finished=None, *,
           source=None, operation_id=None, check=None, start_deadline=None, force_isolated=False,
           expected_revision=None, pinned_revision=None, stage_material=None, retain_output=None,
           populate_workspace=None, workspace_scope=None, environment_id=None, readonly_source=False,
           separate_output=False, host_grant=None, working_directory='.', capture_host_revision=True, project_view=None,
           include_project_record=False):
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(x, str) or "\0" in x for x in argv)
    ):
        raise Problem("invalid_command", "命令必须是非空 argv 字符串数组")
    if len(argv) > 100 or sum(map(len, argv)) > 16000:
        raise Problem("invalid_command", "命令过长")
    from .private_keys import clean
    if clean(argv) != argv:
        raise Problem("private_key_material", "命令含私钥正文，请选择本机 SSH 身份", 403)
    ident = uuid.uuid4().hex
    if pinned_revision is not None and (not force_isolated or kind != "check" or check is None):
        raise Problem("invalid_scope", "固定候选只能用于受控隔离检查", 403)
    directory = home() / "processes"
    directory.mkdir(exist_ok=True)
    env, _ = NG.for_child()
    if w.get("isolatedTask"):
        from .subtasks import command_environment

        env = command_environment(w["isolatedTask"])
    creation = 0x08000000 if os.name == "nt" else 0
    from . import permissions, safe_executor
    permission_revision = permissions.profile()["revision"]
    mode = permissions.effective_sandbox()
    if host_grant is not None and (kind != "check" or not source or check is None or force_isolated):
        raise Problem("invalid_scope", "单次终端许可只适用于原动作", 403)
    require_host_transport(kind, mode)
    # 强制隔离优先于全访问偏好；只有显式宿主路径才可直接执行，准备环境也必须属于隔离检查。
    managed = force_isolated or (mode != "danger-full-access" and host_grant is None)
    # A live terminal has a process receipt, but no frozen project version.
    revision = pinned_revision or expected_revision
    if project_view is not None:
        if not managed or project_view.project != Path(w['path']).resolve():
            raise Problem('invalid_scope', '项目暂存必须用对应的隔离执行环境', 403)
        revision = project_view.revision()
    if not revision and (managed or capture_host_revision):
        revision = current_revision(w)
    if not managed and source and source.get("tool") == "run_project_shell" and git_exe():
        env["PATH"] = str(Path(git_exe()).resolve().parent) + os.pathsep + env.get("PATH", "")
    if separate_output and (not managed or kind != 'check'):
        raise Problem('invalid_scope','分流输出仅用于受控隔离命令')
    if environment_id is not None and (not managed or kind != "check"):
        raise Problem("invalid_scope", "准备环境只用于隔离检查")
    owned = os.name == "nt" and not managed
    control = directory / (ident + ".owner.json")
    if owned:
        atomic_json(
            control,
            dict(
                mode="command",
                permissionRevision=permission_revision,
                hostGrant=host_grant, sessionId=permissions.call_session(),
                runId=(source or {}).get("runId"), actionId=(source or {}).get("actionId"),
                argv=argv,
                cwd=w["path"],
                tty=kind == "terminal",
                nonce=uuid.uuid4().hex,
                ownerPid=os.getpid(),
                ownerIdentity=RT.identity(os.getpid()),
                stopFile=str(directory / (ident + ".stop.json")),
                readyFile=str(directory / (ident + ".ready.json")),
                exitFile=str(directory / (ident + ".exit.json")),
                startDeadline=start_deadline,
            ),
        )
        command = core_runtime.command("owned-process.py", "--config", str(control), isolated=True)
    else:
        command = argv
    if check is not None:
        check()
    if kind == "terminal":
        try:
            from winpty import PtyProcess
        except ImportError:
            raise Problem("unavailable", "交互终端需要 pywinpty，尚未创建终端", 503)
        p = PtyProcess.spawn(command, cwd=w["path"], env=env, dimensions=(rows, cols))
    elif managed:
        # 在启动前的实际边界调用上层活性检查，取消请求不应留下迟到进程。
        def validate():
            if pinned_revision is None and project_view is None:
                if force_isolated and str(revision).startswith("folder:"):
                    if selected_check_revision(check_source_state(w), record_revision(revision)) != revision:
                        raise Problem("revision_conflict", "检查准备期间目录已变化，请重新核对", 409)
                else:
                    check_revision(w, revision)
            if check is not None:
                check()

        material_hooks = {"verify_copy": stage_material} if stage_material else {}
        if force_isolated and (project_view is not None or str(revision).startswith('folder:')) and w.get('recordBinding'):
            from .conversation_record import check_view
            material_hooks.update(record_view=lambda: check_view(w), include_record=include_project_record if project_view is not None else record_revision(revision))
        if workspace_scope is None and kind == "check" and source and source.get("sessionId") and source.get("runId"):
            workspace_scope = (source["sessionId"], source["runId"], str(Path(w["path"]).resolve()))
        # Named environments retain one explicit task identity across source
        # modes. Their lease is exclusive and the sandbox resets source ACLs.
        if readonly_source and workspace_scope is not None and environment_id is None:
            workspace_scope = (*workspace_scope, "readonly-source")
        p = safe_executor.start(argv, cwd=w["path"], env=env, merge=not separate_output, validate=validate,
                                defer_cleanup=kind == "check", trusted_source=pinned_revision is not None,
                                retain_output=retain_output, populate_workspace=populate_workspace,
                                workspace_scope=workspace_scope, environment_id=environment_id, readonly_source=readonly_source,
                                source_revision=pinned_revision or (revision if readonly_source else None),
                                source_guard=(lambda: FileLock(writer_lock_path(w["path"]), check=check))
                                    if pinned_revision is None and force_isolated else None,
                                 working_directory=working_directory,
                                 **({'project_view': project_view} if project_view is not None else {}),
                                 **(safe_executor.check_options() if kind == 'check' else {}),
                                **material_hooks)
    else:
        p = subprocess.Popen(
            command,
            cwd=w["path"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creation,
        )
    r = {
        "id": ident,
        "workspaceId": w["id"],
        "argv": argv,
        "kind": kind,
        "ownedFamily": owned,
        "isolation": getattr(p, "isolation", "host-full-access"),
        "pidRole": "controller" if owned else "command",
        "pid": p.pid,
        "identity": RT.identity(p.pid),
        "ownerPid": os.getpid(),
        "ownerIdentity": RT.identity(os.getpid()),
        "state": "running",
        "started": time.time(),
        "revision": revision,
        "exitCode": None,
        "outputFormat": 'separate' if separate_output else 'combined',
    }
    if environment_id is not None and getattr(p, "workspace_lease", None):
        p.workspace_lease["lastRevision"] = revision
        r["environmentId"] = p.workspace_lease["environmentId"]
    if getattr(p, 'execution_environment', None):
        r['executionEnvironment'] = p.execution_environment
    if source is not None:
        r.update(source=source, conversationId=source["sessionId"], operationId=operation_id)
    try:
        atomic_json(record_path(ident), r)
    except Exception:
        # Startup may already have passed its gate. Close the original controller
        # handle, whose job kills all descendants; never recover/signal a PID.
        if kind == "terminal":
            p.terminate(force=True)
            p.close(force=True)
        else:
            try:
                p.kill()
                p.wait(timeout=10)
            finally:
                for stream in (p.stdout,p.stderr):
                    if stream:stream.close()
        raise
    _processes[ident] = p
    if not managed:
        permissions.watch_host(lambda:p.isalive() if kind=="terminal" else p.poll() is None,
                               lambda:stop_process(ident),permission_revision)

    # 消费原进程管道并记录退出和回收事实，完成后调用已登记的上层回执回调。
    def collect():
        from .private_keys import PrivateKeyFilter
        isolated_check = managed and kind == "check"
        try:
            if kind == "terminal":
                guard = PrivateKeyFilter()
                while p.isalive():
                    try:
                        append_output(ident, guard.feed(p.read(4096)))
                    except EOFError:
                        break
                append_output(ident, guard.finish())
                code = p.exitstatus
            else:
                def drain(stream, label=None):
                    guard = PrivateKeyFilter()
                    encoding = getattr(p, 'execution_environment', {}).get('outputEncoding', 'utf-8')
                    decoder = codecs.getincrementaldecoder(encoding)(errors='replace')
                    while True:
                        b = stream.read1(16384)
                        if not b:break
                        append_output(ident, guard.feed(decoder.decode(b)), stream=label)
                    append_output(ident, guard.feed(decoder.decode(b'', final=True)) + guard.finish(), stream=label)
                if separate_output:
                    errors=[]
                    def read_stream(stream,label):
                        try:drain(stream,label)
                        except Exception as error:errors.append(error)
                    readers=[threading.Thread(target=read_stream,args=(stream,label),daemon=True)
                        for stream,label in ((p.stdout,'stdout'),(p.stderr,'stderr')) if stream is not None]
                    for reader in readers:reader.start()
                    for reader in readers:reader.join()
                    if errors:raise errors[0]
                else:
                    drain(p.stdout)
                code = p.wait_for_exit() if isolated_check else p.wait()
            # 控制器退出不等于命令族已确认结束；必须收到匹配 nonce 和进程族收尾证明的真实退出码。
            if owned:
                control_data = read(control)
                receipt = read(control_data["exitFile"])
                if (
                    receipt.get("nonce") != control_data["nonce"]
                    or receipt.get("familyTerminationConfirmed") is not True
                    or type(receipt.get("exitCode")) is not int
                ):
                    raise RuntimeError(
                        "进程控制器退出但缺少匹配的命令终态回执；实际影响需核对"
                    )
                code = receipt["exitCode"]
            if on_finished is not None:
                on_finished()
            with _lock:
                r.update(
                    state="completed" if code == 0 else "failed",
                    exitCode=code,
                    finished=time.time(),
                )
                if read(record_path(ident)).get("stopRequested"):
                    r["state"] = "interrupted"
                if isolated_check:
                    if retain_output is not None and p.retained_output is not None:
                        r["retainedOutput"] = p.retained_output
                    r["timingsMs"] = dict(p.phase_timings(),
                                          receiptAfterExit=round((time.monotonic()-p.exited)*1000))
                atomic_json(record_path(ident), r)
        except Exception as e:
            r.update(state="unknown", exitCode=None, error=str(e))
            atomic_json(record_path(ident), r)
        finally:
            _processes.pop(ident, None)
            if kind == "terminal":
                try:
                    p.close(force=True)
                finally:
                    # pywinpty 3.1.2 isalive() marks closed before close() can
                    # release reader sockets. Explicitly release these owned
                    # sockets even after the terminal process has exited.
                    for name in ("fileobj", "_server"):
                        sock = getattr(p, name, None)
                        if sock:
                            try:
                                sock.close()
                            except OSError:
                                pass
                    reader = getattr(p, "_thread", None)
                    if reader:
                        reader.join(timeout=1)
            elif p.stdout:
                p.stdout.close()
                if p.stderr:
                    p.stderr.close()
            if isolated_check and p.exited is not None:
                p.wait_cleanup()
                with _lock:
                    saved = read(record_path(ident))
                    if saved.get("id") == ident:
                        saved.setdefault("timingsMs", {}).update(p.phase_timings())
                        if p.cleanup_error:
                            saved["cleanupError"] = p.cleanup_error
                        atomic_json(record_path(ident), saved)

    threading.Thread(target=collect, daemon=True, name="workbench-process").start()
    return r


# 按原进程和流读取已保留的输出页，游标以原输出的偏移计算。
def process_output(ident, after=0, *, stream=None):
    r = process_record(ident)
    r.setdefault('outputFormat','combined')
    if stream is not None and (not isinstance(stream,str) or stream not in {'stdout','stderr'}):
        raise Problem('invalid_stream','stream为stdout或stderr')
    if stream and r['outputFormat'] != 'separate':
        raise Problem('combined_output','历史记录只有合并日志，不能恢复独立输出流',409)
    p = home() / "processes" / (ident + ('.'+stream if stream else '') + ".log")
    data = b""
    if p.exists():
        with p.open("rb") as f:
            f.seek(max(0, after))
            data = f.read(65536)
            # Logs are always UTF-8. Do not split a code point across cursor pages.
            if len(data) == 65536:
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError as e:
                    if e.reason == "unexpected end of data":
                        f.seek(e.start - len(data), 1)
                        data = data[: e.start]
            cursor = f.tell()
    else:
        cursor = 0
    return {
        "process": r,
        "text": data.decode("utf-8", "replace"),
        "next": cursor,
        "hasMore": p.exists() and p.stat().st_size > cursor,
        "stream": stream or 'combined',
        **read(home() / "processes" / (ident + ".output.json")),
    }


# 停止先记录 stopping，再通知原持有者；返回接收停止不等于已退出，恢复的裸 PID 不能作为控制句柄。
def stop_process(ident):
    with _lock:
        r = read(record_path(ident))
        if not r:
            raise Problem("not_found", "运行记录不存在", 404)
        if r["state"] in {"completed", "failed", "interrupted"}:
            return {"ok": True, "state": r["state"]}
        p = _processes.get(ident)
        if not p:
            raise Problem("unknown", "该运行不在本服务内，禁止对恢复的裸 PID 发送信号", 409)
        r["stopRequested"] = True
        r["state"] = "stopping"
        atomic_json(record_path(ident), r)
    if r.get("ownedFamily"):
        control = read(home() / "processes" / (ident + ".owner.json"))
        atomic_json(control["stopFile"], dict(nonce=control["nonce"], stop=True))
    elif r["kind"] == "terminal":
        p.terminate(force=True)
    else:
        p.terminate()
    return {"ok": True, "state": "stopping"}


# 报告宿主目前拥有的运行进程和调试器，供桌面退出决定。
def lifecycle(sv):
    processes = [process_record(i) for i in list(_processes)]
    debuggers = [d.id for d in list(_debuggers.values()) if d.p.poll() is None]
    threads = []
    for t in registry()["threads"].values():
        d = sv._resolve_sid(t["id"])
        if d and RT.status(d)["active"]:
            threads.append(t["id"])
    return {"processes": processes, "debuggers": debuggers, "threads": threads}


class Debugger:
    """DAP transport owned by the backend, independent of renderer reloads."""

    # 创建并持有调试适配器的进程与通信状态，所有请求沿同一会话发送。
    def __init__(self, w, on_closed=None):
        from . import permissions
        permission_revision = permissions.profile()["revision"]
        require_host_transport("debug", permissions.effective_sandbox())
        if importlib.util.find_spec("debugpy") is None:
            raise Problem("unavailable", "请安装 debugpy", 503)
        self.id = uuid.uuid4().hex
        self.on_closed = on_closed
        self.workspace = w
        self.messages = []
        self.first = 0
        self.messageBytes = 0
        self.phase = "starting"
        self.stoppedThread = None
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.close_lock = threading.RLock()
        self.seq = 0
        self.secret_evaluations = set()
        env, _ = NG.for_child()
        self.p = subprocess.Popen(
            [core_runtime.tool_python(), "-m", "debugpy.adapter"],
            cwd=w["path"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=(0x08000000 | 0x00000004) if os.name == "nt" else 0,
        )
        from .process_family import Family

        try:
            self.family = Family(self.p)
            if permissions.profile()["revision"] != permission_revision or permissions.effective_sandbox() != "danger-full-access":
                self.family.close()
                raise Problem("permission_changed", "启动期间权限变化，未执行", 409)
            if os.name == "nt":
                from . import isolate

                if not isolate.resume(self.p.pid):
                    self.family.close()
                    raise OSError("调试进程未能恢复执行")
        except Exception:
            self.p.kill()
            self.p.wait(timeout=5)
            self.p.stdin.close()
            self.p.stdout.close()
            raise
        self.closed = False
        permissions.watch_host(lambda:not self.closed and self.p.poll() is None,self.close,permission_revision)
        threading.Thread(target=self.collect, daemon=True).start()

    # 解析适配器消息并按序写入调试记录，更新可确认的进程和线程状态。
    def collect(self):
        try:
            while True:
                length = 0
                header_bytes = 0
                while True:
                    line = self.p.stdout.readline(8193)
                    header_bytes += len(line)
                    if header_bytes > 8192 or len(line) > 8192:
                        raise ValueError("DAP header too large")
                    if not line:
                        raise ValueError("DAP stream closed")
                    if line == b"\r\n":
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":")[1])
                if length <= 0 or length > LIMIT:
                    raise ValueError("DAP content length invalid")
                message = json.loads(self.p.stdout.read(length))
                with self.lock:
                    from .evidence_redaction import redact, MASK

                    if message.get("request_seq") in self.secret_evaluations:
                        self.secret_evaluations.discard(message["request_seq"])
                        message["body"] = dict(
                            message.get("body", {}), result=MASK, variablesReference=0
                        )
                    message = redact(message)
                    self.messages.append(message)
                    self.messageBytes += len(json.dumps(message).encode())
                    if message.get("event") == "stopped":
                        self.phase = "stopped"
                        self.stoppedThread = (message.get("body") or {}).get("threadId")
                    if message.get("event") == "continued":
                        self.phase = "running"
                        self.stoppedThread = None
                    if message.get("event") in {"exited", "terminated"}:
                        self.phase = "exited"
                        self.stoppedThread = None
                    while (
                        len(self.messages) > 2000
                        or self.messageBytes > 16 * 1024 * 1024
                    ):
                        old = self.messages.pop(0)
                        self.messageBytes -= len(json.dumps(old).encode())
                        self.first += 1
        except (OSError, ValueError, RecursionError):
            self.phase = "unknown"
            if self.p.poll() is None:
                self.p.kill()

    # 按受控协议命令生成请求并保留序号，响应沿原序号匹配。
    def send(self, command, args):
        if command not in {
            "initialize",
            "launch",
            "attach",
            "setBreakpoints",
            "configurationDone",
            "threads",
            "stackTrace",
            "scopes",
            "variables",
            "continue",
            "next",
            "stepIn",
            "stepOut",
            "pause",
            "disconnect",
            "evaluate",
        }:
            raise Problem("invalid_debug_command", "不支持的调试命令")
        if command == "launch":
            program = file_path(self.workspace, args.get("program", ""))
            if not program.is_file():
                raise Problem("not_found", "请选择 Python 程序", 404)
            args = dict(
                args,
                program=str(program),
                cwd=self.workspace["path"],
                console="internalConsole",
            )
        if command == "variables":
            args = dict(
                args,
                start=max(0, int(args.get("start", 0))),
                count=min(200, max(1, int(args.get("count", 200)))),
            )
        if command == "stackTrace":
            args = dict(args, levels=min(100, max(1, int(args.get("levels", 50)))))
        if command == "setBreakpoints":
            args = dict(
                args,
                source=dict(
                    args.get("source") or {},
                    path=str(
                        file_path(
                            self.workspace, (args.get("source") or {}).get("path", "")
                        )
                    ),
                ),
            )
        # DAP 请求序号用于关联回复；敏感求值先登记身份，后续日志处理可据此避免泄漏求值结果。
        with self.lock:
            self.seq += 1
            seq = self.seq
            if command == "evaluate":
                from .evidence_redaction import SENSITIVE

                if SENSITIVE.search(str(args.get("expression", ""))):
                    if len(self.secret_evaluations) >= 1000:
                        raise Problem("debug_pending_limit", "未确认求值过多", 429)
                    self.secret_evaluations.add(seq)
        message = {"seq": seq, "type": "request", "command": command, "arguments": args}
        b = json.dumps(message).encode()
        if len(b) > LIMIT:
            raise Problem("debug_payload_too_large", "调试命令超出大小限制", 413)
        finished = threading.Event()
        failure = []

        def write():
            try:
                if not self.write_lock.acquire(timeout=5):
                    raise OSError("DAP write busy")
                try:
                    self.p.stdin.write(
                        ("Content-Length: %d\r\n\r\n" % len(b)).encode() + b
                    )
                    self.p.stdin.flush()
                finally:
                    self.write_lock.release()
            except (OSError, ValueError) as error:
                failure.append(error)
            finally:
                finished.set()

        threading.Thread(target=write, daemon=True).start()
        # 写入超时意味着发送状态未知，不能重发有副作用的调试命令；终止原持有连接后交给调用方核对。
        if not finished.wait(5):
            self.phase = "unknown"
            if self.p.poll() is None:
                self.p.kill()
            raise Problem("debug_send_timeout", "调试发送未确认", 504)
        if failure:
            raise Problem("debug_transport_failed", "调试连接已失效", 409)
        return {"seq": seq}

    # 用同一关闭锁保证调试适配器按实际进程归属回收。
    def close(self):
        with self.close_lock:
            if self.closed:
                return
            try:
                if self.p.poll() is None:
                    try:
                        self.send("disconnect", {"terminateDebuggee": True})
                    except (OSError, Problem):
                        pass
            finally:
                self.family.close()
                if self.p.poll() is None:
                    self.p.kill()
                self.p.wait(timeout=5)
                self.closed = True
                record = read(record_path(self.id))
                if record.get("actor") == "human":
                    record.update(
                        state="interrupted",
                        finished=time.time(),
                        familyTerminationConfirmed=True,
                    )
                    atomic_json(record_path(self.id), record)
                for stream in (self.p.stdin, self.p.stdout):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
                if self.on_closed is not None:
                    try:
                        self.on_closed()
                    except Exception:
                        pass  # A history failure must not prevent process cleanup.


# 从会话持久元数据定位原检出目录，兼容旧格式来源。
def recorded_directory(directory, sv):
    value = read(Path(directory) / "session.json").get("cwd")
    if not value and (Path(directory) / "cwd.txt").exists():
        value = (Path(directory) / "cwd.txt").read_text(encoding="utf-8").strip()
    return Path(value or sv.APP).resolve()


# 将检出目录归一到实际 Git 根，用于同一代码树共享写保护。
def writer_root(path):
    resolved = Path(path).resolve()
    if git_exe() and resolved.is_dir():
        result = safe_git(
            {"path": str(resolved)}, ["rev-parse", "--show-toplevel"], check=False
        )
        if result.returncode == 0:
            return Path(result.stdout.decode("utf-8").strip()).resolve()
    return resolved


def local_writer_root(path):
    """Coordinate file access by the nearest worktree marker, without running Git."""
    resolved = Path(path).resolve()
    return next((parent for parent in (resolved, *resolved.parents)
                 if (parent / ".git").exists()), resolved)


def file_workspace_state(ws):
    """Read only checkout identity/branch for file policy; never scan status/index."""
    root = Path(ws["path"]).resolve()
    marker = root / ".git"
    if not marker.exists():
        return dict(available=False, branch=None, head=None, revision=None, protected=False, gitContext=None)
    identity = git_observation.identity(ws)
    directory = marker
    if marker.is_file():
        line = marker.read_text(encoding="utf8").strip()
        if not line.startswith("gitdir: "):
            raise Problem("git_metadata", "工作树标记无法读取，请核对目录", 409)
        directory = (root / line[8:]).resolve()
    raw = (directory / "HEAD").read_text(encoding="ascii").strip()
    branch = raw[len("ref: refs/heads/"):] if raw.startswith("ref: refs/heads/") else "(detached)"
    return dict(available=True, branch=branch, head=None, revision=None,
                protected=protected_code_branch(branch), gitContext=digest(repr(identity).encode()))


def file_write_lock_path(ws, relative):
    path = file_path(ws, relative)
    return home() / ("file-" + digest(os.path.normcase(str(path)).encode()) + ".lock")


def writer_lock_path(path, canonical=False):
    root = Path(path).resolve() if canonical else local_writer_root(path)
    return home() / ("writer-" + digest(os.path.normcase(str(root)).encode()) + ".lock")


def guard_workspace_writer(
    path, sv, exclude_thread=None, exclude_job=None, target_root=None
):
    """One automatic writer across V2, legacy sessions, L4 and recovered leases."""
    roots = {}

    def root_of(value):
        key = os.path.normcase(str(Path(value).resolve()))
        if key not in roots:
            roots[key] = local_writer_root(value)
        return roots[key]

    target = Path(target_root).resolve() if target_root is not None else root_of(path)
    roots[os.path.normcase(str(Path(path).resolve()))] = target
    roots[os.path.normcase(str(target))] = target
    # A stopped conversation does not release an owned command that may still
    # be running. Recovered/unknown identities fail closed until reconciled.
    for file in (home() / "processes").glob("*.json"):
        process = read(file)
        if process.get("kind") == "check" and process.get("isolation") in {"appcontainer-task-copy", "appcontainer-project-overlay"}:
            continue  # The supervised process cannot write back to the source checkout.
        if process.get("kind") == "debug":
            debugger = _debuggers.get(process.get("id"))
            if debugger is not None and debugger.p.poll() is not None:
                try:
                    debugger.close()
                except OSError:
                    raise Problem("writer_busy", "调试目标进程树尚未确认退出", 409)
        if (
            process.get("kind") in {"preview", "debug"}
            and process.get("conversationId") == exclude_thread
            and process.get("state") == "running"
            and process.get("ownerPid")
            == int(os.environ.get("HARNESS_AGENT_HOST_PID", os.getpid()))
            and process.get("ownerIdentity")
            == os.environ.get("HARNESS_AGENT_HOST_ID", RT.identity(os.getpid()))
            and RT.identity(process.get("ownerPid")) == process.get("ownerIdentity")
        ):
            continue
        if process.get("state") not in {
            "running",
            "starting",
            "stopping",
            "unknown",
        }:
            continue
        ws = registry()["workspaces"].get(process.get("workspaceId"))
        if not ws or root_of(ws["path"]) != target:
            continue
        actual = RT.identity(process.get("pid"))
        if actual is not None and (
            actual == "unknown" or actual == process.get("identity")
        ):
            raise Problem(
                "writer_busy", "同一代码目录的检查进程尚未确认退出，请先核对", 409
            )
    for other in registry()["threads"].values():
        if other["id"] == exclude_thread:
            continue
        if other.get("projectless"):
            directory = sv._resolve_sid(other["id"])
            if not directory or not RT.status(directory)["active"]:
                continue
            from .projects import resource_snapshot

            resources = (resource_snapshot(other) or {}).get("resources", [])
            if any(
                r.get("kind") == "code"
                and r.get("write")
                and r.get("path")
                and root_of(r["path"]) == target
                for r in resources
            ):
                directory = sv._resolve_sid(other["id"])
                if directory and RT.status(directory)["active"]:
                    raise Problem(
                        "writer_busy",
                        "另一个对话仍在处理同一代码目录，请等待或使用隔离子任务",
                        409,
                    )
        if other["kind"] != "implementation":
            continue
        w = registry()["workspaces"].get(other["workspaceId"])
        if not w or root_of(w["path"]) != target:
            continue
        d = sv._resolve_sid(other["id"])
        if d and RT.status(d)["active"]:
            raise Problem(
                "writer_busy", "同一工作区已有自动写入任务，请使用旁问或独立工作树", 409
            )
    runs = getattr(sv, "RUNS", None)
    if runs and Path(runs).is_dir():
        for d in Path(runs).iterdir():
            if not d.is_dir() or d.name == exclude_thread or not sv._is_visible(d):
                continue
            state = RT.status(d)
            if (
                state["active"]
                and state.get("policy") != "readonly"
                and root_of(recorded_directory(d, sv)) == target
            ):
                raise Problem(
                    "writer_busy", "旧会话仍在同一目录执行，请先暂停或创建工作树", 409
                )
    jobs = getattr(sv, "JOBS", None)
    if jobs and Path(jobs).is_dir():
        for d in Path(jobs).iterdir():
            if not d.is_dir() or d.name == exclude_job:
                continue
            if (
                RT.status(d)["active"]
                and root_of(read(d / "job.json").get("cwd") or sv.APP) == target
            ):
                raise Problem(
                    "writer_busy", "持续任务仍在同一目录执行，请使用独立工作树", 409
                )


def guard_continuation(directory, policy, sv):
    """Also applies to queue recovery and legacy UI sends for managed threads.

    The caller holds _lock through guard, spawn, and runtime registration so two
    managed continuations cannot both pass writer exclusion before either starts.
    """
    records = registry()["threads"]
    thread = records.get(Path(directory).name)
    if thread and thread.get("deletionPending"):
        raise Problem("archive_cleanup_pending", "会话正在删除", 409)
    if not thread:
        if policy != "readonly":
            guard_workspace_writer(
                recorded_directory(directory, sv),
                sv,
                exclude_thread=Path(directory).name,
            )
        return policy
    from . import operations
    if thread.get("archived"):
        raise Problem(operations.ARCHIVED_INPUT["code"], operations.ARCHIVED_INPUT["error"], 409)
    w = workspace(thread["workspaceId"])
    session = read(Path(directory) / "session.json")
    recorded = session.get("cwd")
    if not recorded and (Path(directory) / "cwd.txt").exists():
        recorded = (Path(directory) / "cwd.txt").read_text(encoding="utf-8").strip()
    if not recorded or Path(recorded).resolve() != Path(w["path"]).resolve():
        raise Problem(
            "workspace_mismatch", "会话目录与登记工作区不一致，未启动任务", 409
        )
    if thread.get("projectless"):
        from .code_service import effective_policy, selected_write_paths

        if thread.get('activeDelegation'):
            from . import delegations
            delegations.ensure_send_allowed(thread['id'])
        from .subtasks import ensure_send

        ensure_send(thread["id"], continuation=True)

        for path in selected_write_paths(thread["id"]):
            guard_workspace_writer(path, sv, exclude_thread=thread["id"])
        return effective_policy(thread["id"])
    if thread["kind"] != "implementation":
        return "readonly"
    if git_status(w).get("protected"):
        raise Problem(
            "protected_branch", "接续执行前检测到保护分支，请先切回功能分支", 403
        )
    guard_workspace_writer(w["path"], sv, exclude_thread=thread["id"])
    for other in records.values():
        if (
            other["id"] == thread["id"]
            or other["workspaceId"] != w["id"]
            or other["kind"] != "implementation"
        ):
            continue
        d = sv._resolve_sid(other["id"])
        if d and RT.status(d)["active"]:
            raise Problem("writer_busy", "同一工作区已有自动写入任务，待办未启动", 409)
    return policy


def handle(method, path, query, body, sv):
    """Return (HTTP status, JSON). Called after existing auth/body/receipt middleware."""
    try:
        if method == "POST":
            from . import human_actions

            return human_actions.perform(
                path, body, sv, lambda: _dispatch_route(method, path, query, body, sv)
            )
        return route(method, path, query, body, sv)
    except Problem as e:
        return e.status, {"error": e.message, "code": e.code}
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        subprocess.SubprocessError,
    ) as e:
        return 400, {"error": str(e), "code": "operation_error"}


def launch_busy():
    return 409, {"code": "launch_busy", "error": "会话正在启动或核对待办，请稍后继续"}


def _wait_for_launch(directory, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    gate = FileLock(directory / "launch.lock", timeout=remaining, create_parent=False)
    try:
        admitted = gate.acquire()
    except FileNotFoundError:
        return False
    if not admitted:
        return False
    gate.release()
    return True


@contextmanager
def launch_admission(directory, *, wait_for_launch):
    """Hold dispatch and session locks together only after a zero-wait attempt."""
    deadline = None
    while True:
        with _lock:
            gate = FileLock(directory / "launch.lock", timeout=0, create_parent=False)
            try:
                admitted = gate.acquire()
            except FileNotFoundError as error:
                raise Problem("not_found", "对话不存在", 404) from error
            if admitted:
                try:
                    yield True
                finally:
                    gate.release()
                return
        if wait_for_launch:
            if deadline is None:
                deadline = time.monotonic() + _LAUNCH_LOCK_TIMEOUT
            if _wait_for_launch(directory, deadline):
                continue
        yield False
        return


# 选择人工作业或会话路由，动作记录沿真实请求上下文保留。
def _dispatch_route(method, path, query, body, sv):
    from .human_actions import professional

    if method == 'POST' and path == '/api/v2/lifecycle':
        from . import check_sessions, verification_workspace

        status, result = route(method, path, query, body, sv)
        if status != 200:
            return status, result
        # Collectors acquire _lock to publish their terminal receipts.
        deadline = time.monotonic() + 8
        while (_processes or check_sessions.owned_jobs()) and time.monotonic() < deadline:
            time.sleep(.05)
        pending_processes = list(_processes)
        pending_checks = [job.operation_id for job in check_sessions.owned_jobs()]
        verification_workspace.cleanup()
        complete = not (pending_processes or pending_checks)
        return 200, dict(ok=complete, pendingProcessReceipts=pending_processes,
            pendingCheckReceipts=pending_checks,
            notice='自有进程与检查回执已收齐' if complete else '仍有终态回执未确认；保留未知状态，不重放原操作')
    # Network Git/PR operations use their workspace Git lock. PTY/DAP input
    # and reads must remain responsive while a repository action is slow.
    if (
        method == "GET"
        or professional(path)
        or path.startswith(("/api/v2/processes/", "/api/v2/debug/"))
        # A remote catalog read must allow uninstall/revocation while it is in flight.
        or (method == "POST" and re.fullmatch(r"/api/v2/plugins/[0-9a-f]{32}/tools", path))
        # Language downloads/extraction may take minutes. The plugin registry serializes publication.
        or (method == "POST" and re.fullmatch(r"/api/v2/plugins/applications/code-workroom/languages/(?:cpp|rust|typescript|python)", path))
        or path.endswith(("/git", "/pull-request"))
        or (method == "POST" and re.fullmatch(r"/api/v2/conversations/threads/[^/]+/delete", path))
        or (
            method == "POST"
            and re.fullmatch(
                r"/api/v2/conversations/threads/[A-Za-z0-9_-]+/artifacts/[0-9a-f]{32}/compare",
                path,
            )
        )
    ):
        return route(method, path, query, body, sv)
    session_action = re.fullmatch(
        r"/api/v2/(?:conversations|workspaces/[^/]+)/threads/([^/]+)/(?:send|resolve)", path
    )
    deadline = None
    while True:
        with _lock:
            response = route(method, path, query, body, sv)
        if not session_action or response[0] != 409 or response[1].get("code") != "launch_busy":
            return response
        # A busy gate means neither dispatch nor reconciliation has begun.
        # Revalidate the route after waiting, without reclaiming its operation receipt.
        if deadline is None:
            deadline = time.monotonic() + _LAUNCH_LOCK_TIMEOUT
        directory = sv._resolve_sid(session_action[1])
        if directory is None or not _wait_for_launch(directory, deadline):
            return response


# 从当前检出目录的分支和远端查询拉取请求；创建等变更用同一工作区确认。
def pull_request(w, method, b):
    gh = os.environ.get("HARNESS_GH") or shutil.which("gh")
    if method == "GET":
        return 200, {
            "available": bool(gh),
            "reason": ""
            if gh
            else "需要安装并登录 GitHub CLI（gh），也可以在外部 Git 客户端创建 PR",
        }
    if not gh:
        raise Problem("unavailable", "未找到已配置的 GitHub CLI", 503)
    check_revision(w, b.get("expectedRevision"))
    state = git_status(w)
    if state.get("protected") or not state.get("available"):
        raise Problem("protected_branch", "PR 来源必须为功能分支", 403)
    if state["files"]:
        raise Problem("dirty_source", "请先提交并推送工作区改动", 409)
    upstream = git(w, ["rev-parse", "@{upstream}"], check=False)
    if upstream.returncode or upstream.stdout.decode().strip() != state["head"]:
        raise Problem("not_pushed", "请先推送当前分支，确认远端已包含当前提交", 409)
    title = str(b.get("title", "")).strip()
    base = str(b.get("base", "main"))
    if not title or len(title) > 300:
        raise Problem("invalid_command", "请输入不超过 300 字符的 PR 标题")
    if git(w, ["check-ref-format", "--branch", base], check=False).returncode:
        raise Problem("invalid_branch", "目标分支名称无效")
    if b.get("confirm") is not True:
        raise Problem("confirmation", "请确认创建远端草稿 PR")
    try:
        result = subprocess.run(
            [
                gh,
                "pr",
                "create",
                "--draft",
                "--title",
                title,
                "--body",
                str(b.get("body", ""))[:100000],
                "--base",
                base,
                "--head",
                state["branch"],
            ],
            cwd=w["path"],
            capture_output=True,
            timeout=40,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        return 202, {
            "state": "unknown",
            "code": "unknown",
            "error": "创建 PR 的结果未确认，请先在 GitHub 核对；不会自动重试",
        }
    if result.returncode:
        raise Problem(
            "github_error", result.stderr.decode("utf-8", "replace")[-2000:], 409
        )
    return 200, {
        "ok": True,
        "url": result.stdout.decode("utf-8", "replace").strip(),
    }


def _create_thread(w, title, kind, sv, *, binding=None):
    reserved = {"reserved_sid": binding["id"]} if binding else {}
    code, result = sv._start("", cwd=w["path"], blank=True, **reserved)
    if code != 200:
        return code, result
    item = {
        "id": result["session"], "taskId": uuid.uuid4().hex,
        "workspaceId": w["id"], "kind": kind,
        "title": str(title or "新任务")[:100], "created": time.time(),
        **(binding or {}),
    }
    if binding:
        stored = change_registry(lambda data: data["threads"].setdefault(item["id"], item))
        if stored.get("scheduledPlanId") != binding["scheduledPlanId"]:
            raise Problem("schedule_conversation_unavailable", "定时会话身份已被占用", 409)
        return 200, stored
    change_registry(lambda data: data["threads"].__setitem__(item["id"], item))
    return 200, item


def conversation_route(method, parts, q, b, sv, *, scheduled_start=False, managed_child=False, fork_origin=None):
    """Projectless public view; private execution directories never grant resources."""
    if len(parts) < 2 or parts[1] != "threads":
        raise Problem("not_found", "没有这个对话接口", 404)

    # 只展示会话可见的摘要和运行状态，持久结构另由宿主维护。
    def public(item):
        return dict(
            {
                key: item[key]
                for key in (
                    "id",
                    "title",
                    "taskId",
                    "kind",
                    "created",
                    "archived",
                    "deletionPending",
                    "runtime",
                    "readonlyEvidence",
                    "questionOf",
                    "discussionOf",
                    "forkOf",
                    "subtaskOf",
                    "scheduledPlanId",
                    "eventCursor",
                )
                if key in item
            },
            workspaceId=None,
            projectId=item.get("projectId"),
            projectName=item.get('projectName'),
            pinned=item.get('pinned',False),
            organizationRevision=item.get('organizationRevision',1),
        )

    if len(parts) >= 4:
        ident = parts[2]
        if parts[3:] == ["delete"] and method == "POST":
            from . import archived_conversations

            return archived_conversations.delete(ident, b)
        from . import projects

        question = projects.conversation(parts[2])
        if parts[3:] == ["fork"]:
            from . import shared_conversations as shared
            return shared.route(parts[2], method, q, sv)
        if len(parts) == 4 and parts[3] == "discussions":
            from . import discussions

            return discussions.route(parts[2], method, b, q, sv)
        if parts[3] == "integrations":
            from . import integrations

            return integrations.route(parts[2], parts[4:], method, b, q, sv)
        if parts[3] == "subtasks":
            from . import subtasks

            return subtasks.route(parts[2], parts[4:], method, b, sv)
        if question.get("subtaskOf") and method != "GET" and parts[3] != "questions":
            raise Problem("managed_child", "子任务会话只供审查，请从父任务控制", 403)
        if parts[3:] == ["permissions"]:
            from . import permissions
            if method == "GET":
                return 200, dict(permissions.profile(parts[2]), requests=permissions.listing(summary=True, session=parts[2]))
            if method == "POST":
                if any(question.get(key) for key in ("archived", "readonlyEvidence", "scheduledPlanId", "scheduledRun")):
                    raise Problem("permission_managed", "该会话的权限由其来源管理", 403)
                return 200, permissions.change_preset(b, parts[2])
        if len(parts)==4 and parts[3]=='organization' and method=='POST':
            from . import conversation_organization
            return 200,conversation_organization.configure(parts[2],b)
        if len(parts) >= 4 and parts[3] == "memory":
            from . import memory_service
            return memory_service.route(method, parts[4:], q, b, parts[2])
        if parts[3:] == ["context", "evidence"] and method == "GET":
            from . import context_evidence

            return 200, context_evidence.page(parts[2], q, sv)
        if len(parts) == 4 and parts[3] == "context":
            from . import context_inspection

            return context_inspection.route(parts[2], method, b, q, sv)
        if parts[3] == "delegations":
            from . import delegations

            return delegations.route(parts[2], parts[4:], method, b, sv)
        if len(parts) == 4 and parts[3] == "execution":
            from . import execution_policy

            return execution_policy.route(parts[2], method, b, sv)
        if (
            question.get("readonlyEvidence")
            and parts[3]
            in {
                "scope",
                "attachments",
                "artifacts",
                "previews",
                "agent-debuggers",
                "questions",
            }
            and method != "GET"
        ):
            raise Problem(
                "readonly_question",
                "证据追问固定只读；请在普通会话提出新的执行要求",
                403,
            )
        if len(parts) == 4 and parts[3] == "questions" and method == "POST":
            from . import evidence_questions

            return evidence_questions.create(parts[2], b, sv)
        if parts[3] == "corrections":
            from . import corrections

            return corrections.route(parts[2], parts[4:], method, q, b, sv)
        if parts[3] == "delivery":
            from . import delivery_review

            return delivery_review.route(parts[2], parts[4:], method, q, b, sv)
        if parts[3] == "restorations":
            from . import restorations

            return restorations.route(parts[2], parts[4:], method, b, q)
    if len(parts) == 4 and parts[3] == 'test-workroom':
        from . import test_workroom, projects
        thread=projects.conversation(parts[2])
        directory=sv._resolve_sid(thread['id'])
        state=read(Path(directory)/'session.json') if directory else {}
        task=(state.get('taskInput') or {}).get('id')
        if not task:raise Problem('scenario_scope','当前会话还没有任务场景',409)
        if method == 'GET':return 200,test_workroom.manage(thread['id'],task)
        if method == 'POST':
            if thread.get('readonlyEvidence'):raise Problem('readonly_evidence','历史审查不可保存场景',403)
            if set(b)-{'operation','scenario_id','version','definition','expected_version','_request_id'}:raise Problem('scenario_arguments','未声明的场景参数')
            return 200,test_workroom.manage(thread['id'],task,**{k:v for k,v in b.items() if k!='_request_id'})
    if len(parts) >= 4 and parts[3] == "artifacts":
        from . import artifacts, projects

        projects.conversation(parts[2])
        return artifacts.route(parts[2], parts[4:], method, q, b)
    if len(parts) >= 4 and parts[3] == "progress":
        from . import progress

        return progress.route(parts[2], parts[4:], method, q, sv)
    if len(parts) >= 4 and parts[3] == "evidence":
        from . import evidence

        return evidence.route(parts[2], parts[4:], method, q, sv)
    if len(parts) >= 4 and parts[3] == "agent-debuggers":
        from . import debugging

        return debugging.route(parts[2], parts[4:], method)
    if len(parts) >= 4 and parts[3] == "previews":
        from . import previews

        return previews.route(parts[2], parts[4:], method, b,q)
    if len(parts) >= 4 and parts[3] == "changes":
        from . import change_history
        return change_history.route(parts[2], parts[4:], method, q)
    if len(parts) == 4 and parts[3] == "project-activity" and method in {"GET", "POST"}:
        from . import project_activity

        return project_activity.route(parts[2], method, q, b)
    if len(parts) >= 4 and parts[3] == "change-ranges":
        from . import change_ranges
        return change_ranges.route(parts[2], parts[4:], method, q)
    if (
        len(parts) == 6
        and parts[3] == "code-operations"
        and parts[5] == "output"
        and method == "GET"
    ):
        from .evidence_targets import check_output

        return 200, check_output(
            parts[2],
            parts[4],
            int((q.get("after") or ["0"])[0]),
            (q.get("evidenceRevision") or [None])[0],
            sv,
        )

    if len(parts) == 2 and method == "GET":
        rows = []
        data=registry()
        for item in data["threads"].values():
            if not item.get("projectless"):
                continue
            d = None if item.get("deletionPending") else sv._resolve_sid(item["id"])
            from .discussions import event_cursor

            counted = event_cursor(d) if d else None
            rows.append(
                public(
                    dict(
                        item,
                        projectName=data.get('projects',{}).get(item.get('projectId'),{}).get('name'),
                        eventCursor=counted,
                        runtime=RT.status(d)
                        if d
                        else {"state": "unknown", "active": False},
                    )
                )
            )
        return 200, {
            "threads": sorted(rows, key=lambda row: row.get("created", 0), reverse=True)
        }
    if len(parts) == 2 and method == "POST":
        from . import permissions
        initial_permissions = None if managed_child else permissions.new_chat_profile(b)
        pid=b.get("projectId")
        if 'workFolder' in b and type(b['workFolder']) is not bool:
            raise Problem('invalid_path', '工作目录选项无效')
        if pid:
            from .projects import project
            project(pid, active=True)
        own = None
        if not pid and b.get('workFolder') is True:
            from .work_folders import conversation_resource
            own = conversation_resource()
        parent = home() / "conversations"
        parent.mkdir(exist_ok=True)
        directory = parent / uuid.uuid4().hex
        directory.mkdir()
        if not directory.resolve().is_relative_to(home().resolve()):
            raise Problem("invalid_path", "内部对话目录超出应用数据范围", 403)
        w = register(str(directory), kind="conversation")
        code, item = route(
            "POST",
            "/api/v2/workspaces/" + w["id"] + "/threads",
            q,
            {"title": b.get("title") or "新对话", "kind": "discussion"},
            sv,
            True,
        )
        if code == 200:
            item["projectless"] = True
            item["projectId"] = pid or None
            if initial_permissions is not None:
                permissions.save_new_chat_profile(item["id"], initial_permissions)
            if fork_origin is not None:
                item["forkOf"] = fork_origin
            def bind_project(data):
                if fork_origin is not None:
                    data["threads"][item["id"]]["forkOf"] = fork_origin
                owner = data.get('projects', {}).get(pid, {})
                root = owner.get('resources', {}).get(owner.get('folderResourceId'), {})
                ids = [root['id']] if root.get('kind') == 'code' and root.get('permission') == 'read' else []
                if own:
                    data['threads'][item['id']]['workResource'] = own
                    ids.append(own['id'])
                data['threads'][item['id']].update(projectless=True, projectId=pid or None, resourceIds=ids)
            change_registry(bind_project)
            item = public(item)
        return code, item
    if len(parts) >= 4 and parts[3] == 'record':
        from . import conversation_record, projects

        thread = projects.conversation(parts[2])
        return conversation_record.route(thread, sv._resolve_sid(thread['id']), parts[4:], method, q, b)
    if len(parts) != 4 or parts[-1] not in {
        "events",
        "live",
        "send",
        "stop",
        "archive",
        "scope",
        "attachments",
        "code-operations",
        "local-environment",
        "worktree-session",
        "search-sources",
    }:
        raise Problem("not_found", "没有这个对话命令", 404)
    thread = registry()["threads"].get(parts[2])
    if not thread or not thread.get("projectless"):
        raise Problem("not_found", "对话不存在", 404)
    if thread.get("deletionPending"):
        raise Problem("archive_cleanup_pending", "会话正在删除", 409)
    if len(parts) == 4 and parts[3] in {"local-environment", "worktree-session"}:
        from . import conversation_worktrees

        if method == "POST" and thread.get("scheduledPlanId"):
            raise Problem("schedule_scope_managed", "请在计划设置里调整定时任务的检出目录", 409)
        return conversation_worktrees.route(thread["id"], parts[3], method, q, b, sv)
    if parts[3:] == ["search-sources"] and method == "GET":
        from . import search_sources

        return 200, search_sources.listing(thread["id"], sv)
    if parts[-1] == "code-operations" and method == "GET":
        from . import code_service

        return 200, code_service.operations(
            thread["id"], (q.get("before") or [None])[0]
        )
    if parts[-1] == "attachments":
        from . import attachments

        return attachments.route(thread["id"], method, b)
    if parts[-1] == "scope":
        from . import projects

        if method == "GET":
            if q.get("review") == ["1"] and thread.get("subtaskOf"):
                from .subtasks import audit_child

                audit_child(thread["id"])
            return 200, projects.scope(thread["id"])
        return 200, projects.set_scope(thread["id"], b, sv)
    if parts[-1] == "send" and method == "POST":
        if thread.get('archived'):raise Problem('archived_conversation','此对话已归档，请恢复后继续；历史和草稿保留',409)
        from . import projects
        from . import execution_policy

        chosen_execution = execution_policy.for_send(thread["id"], b)
        b = dict(b, crew=chosen_execution["crew"], executionConfig=chosen_execution)
        if thread.get("readonlyEvidence"):
            b = dict(b, crew="l0", policy="readonly")

        if not str(b.get("text", "")).strip():
            raise Problem("invalid_command", "请输入对话内容")
        if (
            b.get("expectedScopeRevision") is not None
            and b["expectedScopeRevision"] != projects.scope(thread["id"])["revision"]
        ):
            raise Problem(
                "scope_changed", "资料范围已变化，请查看当前范围后重新发送", 409
            )
        if "attachmentIds" in b:
            from .attachments import message_attachments

            message_attachments(thread["id"], b["attachmentIds"])
        if "githubPage" in b:
            from . import github_workroom

            b["githubPage"] = github_workroom.capture(thread["id"], b["githubPage"])
    w = workspace(thread["workspaceId"])
    if w.get("kind") != "conversation" or not Path(w["path"]).resolve().is_relative_to(
        (home() / "conversations").resolve()
    ):
        raise Problem("invalid_path", "内部对话目录无效", 403)
    return route(
        method,
        "/api/v2/workspaces/" + w["id"] + "/threads/" + parts[2] + "/" + parts[3],
        q,
        b,
        sv,
        True,
        scheduled_start=scheduled_start,
    )


# 工作间入口按动作分派工作区、文件、Git、进程与证据请求，资源 ID 必须指向实际登记。
def route(method, path, q, b, sv, _conversation=False, *, scheduled_start=False):
    parts = path.removeprefix("/api/v2/").split("/")
    action = parts[0]
    if parts == ["local-storage"] and method == "GET":
        from . import local_storage

        return local_storage.route()
    if action == "permissions":
        from . import permissions
        return permissions.route(method, parts[1:], b)
    if action == "memory":
        from . import memory_service
        return memory_service.route(method, parts[1:], q, b)
    if action == "human-actions" and method == "GET":
        from . import human_actions

        return 200, human_actions.listing(q)
    if action == "migration":
        from . import legacy_migration

        return legacy_migration.route(parts, method, b, q, sv)
    if action == "workroom":
        from . import project_workroom

        return project_workroom.route(method, parts[1:], q, b)
    if action == "github-workroom":
        from . import github_workroom

        return github_workroom.route(method, parts[1:], q, b)
    if action == "projects":
        from . import projects

        return projects.route(method, parts, b, sv)
    if action == "scheduled-tasks":
        from . import scheduled_tasks

        return scheduled_tasks.route(method, parts[1:], q, b, sv)
    get = lambda key, default="": (q.get(key) or [default])[0]
    if action == "conversations":
        return conversation_route(method, parts, q, b, sv)
    if action=='plugins':
        from . import plugin_runtime
        return plugin_runtime.route(parts[1:],method,q,b)
    if action == "bootstrap":
        from . import permissions

        profile = permissions.profile()
        include_git = (q.get("includeGit") or ["1"])[0] != "0"
        return 200, {
            "version": 2,
            "defaultPath": str(sv.APP),
            "workspaces": [
                capability(w, include_git=include_git)
                for w in registry()["workspaces"].values()
                if w.get("kind") != "conversation" and Path(w["path"]).is_dir()
            ],
            "policy": sv.POLICY,
            "permissions": profile,
            "hostTransportBlocks": {
                kind: host_transport_block(kind, profile["sandboxMode"])
                for kind in ("terminal", "debug", "preview")
            },
        }
    if action == "lifecycle":
        if method == "GET":
            return 200, lifecycle(sv)
        if b.get("confirm") is not True:
            raise Problem("confirmation", "需要明确结束工作台进程")
        scheduler = getattr(sv, "SCHEDULED_TASKS", None)
        if scheduler:
            scheduler.stop()
        from . import delegations

        delegations.stop_all()
        from .subtasks import stop_owned

        stop_owned(sv)
        from .repository_import import stop_owned as stop_repository_imports
        from .check_sessions import owned_jobs

        stop_repository_imports()
        for job in owned_jobs():
            job.cancel.set()
        # Only handles owned by this backend are signalled; recovered PIDs are never killed.
        for ident in list(_processes):
            try:
                stop_process(ident)
            except (Problem, OSError):
                pass
        for d in list(_debuggers.values()):
            d.close()
        return 200, {"stopRequested": True}
    if action == "workspaces" and method == "POST" and len(parts) == 1:
        return 200, capability(register(b["path"]))
    if action == "debug":
        d = _debuggers.get(parts[1]) if len(parts) > 1 else None
        if not d:
            raise Problem("not_found", "调试器不存在", 404)
        if method == "GET":
            after = max(0, int(get("after", "0")))
            with d.lock:
                start = max(after, d.first)
                rows = []
                size = 0
                for message in d.messages[start - d.first :]:
                    length = len(json.dumps(message).encode())
                    if rows and (size + length > LIMIT or len(rows) >= 200):
                        break
                    rows.append(message)
                    size += length
                cursor = start + len(rows)
                from .evidence_redaction import redact

                return 200, {
                    "messages": redact(rows),
                    "next": cursor,
                    "hasMore": cursor < d.first + len(d.messages),
                    "gap": after < d.first,
                    "phase": d.phase,
                    "stoppedThread": d.stoppedThread,
                    "alive": d.p.poll() is None,
                }
        if b["command"] == "disconnect":
            d.close()
            return 200, dict(
                ok=True,
                id=d.id,
                seq=d.seq,
                state="interrupted",
                familyTerminationConfirmed=True,
            )
        return 200, d.send(b["command"], b.get("arguments") or {})
    if action == "processes":
        ident = parts[1]
        if method == "GET":
            return 200, process_output(ident, int(get("after", "0")))
        p = _processes.get(ident)
        r = process_record(ident)
        if parts[-1] == "stop":
            return 200, stop_process(ident)
        if not p or r["kind"] != "terminal":
            raise Problem("not_found", "交互终端已结束", 409)
        if parts[-1] == "input":
            p.write(str(b.get("text", ""))[:16384])
            return 200, {"ok": True}
        if parts[-1] == "resize":
            p.setwinsize(max(2, min(200, b["rows"])), max(10, min(400, b["cols"])))
            return 200, {"ok": True}
    if action != "workspaces" or len(parts) < 2:
        raise Problem("not_found", "没有这个工作台接口", 404)
    w = workspace(parts[1])
    if w.get("kind") == "conversation" and not _conversation:
        raise Problem("private_scope", "内部对话目录不提供工程操作", 403)
    op = parts[2] if len(parts) > 2 else ""
    if not op:
        return 200, capability(w)
    if op == "jobs":
        if method != "GET":
            raise Problem("execution_retired", "旧任务仅供查看，请在会话中建立持续委托", 410)
        return 200, {
            "jobs": [
                j for j in sv._jobs_index().get("jobs", [])
                if Path((sv._job_overview(j["id"]).get("job") or {}).get("cwd") or "").resolve()
                == Path(w["path"]).resolve()
            ]
        }
    if op == "pull-request":
        if method == "GET":
            return pull_request(w, method, b)
        with FileLock(home() / (w["id"] + ".git.lock")):
            return pull_request(w, method, b)
    if op == "inbox":
        items = []
        for t in registry()["threads"].values():
            if t["workspaceId"] != w["id"]:
                continue
            d = sv._resolve_sid(t["id"])
            if not d:
                continue
            approval = pending_approval(d)
            decision = t.get("approvalDecision") or {}
            if approval and decision.get("event") != digest(
                json.dumps(approval, sort_keys=True).encode()
            ):
                items.append({"kind": "approval", "thread": t, "approval": approval})
            if (d / sv.PENDING_CLAIMED).exists() and not (
                d / sv.PENDING_RESULT
            ).exists():
                items.append({"kind": "unknown", "thread": t})
        return 200, {"items": items}
    if op == "legacy-sessions":
        registered = registry()["threads"]
        if method == "GET":
            candidates = []
            for row in sv._sessions(200):
                if row["id"] in registered:
                    continue
                d = sv._resolve_sid(row["id"])
                session = read(d / "session.json") if d else {}
                cwd = session.get("cwd")
                if cwd and Path(cwd).resolve() == Path(w["path"]).resolve():
                    candidates.append(
                        {
                            "id": row["id"],
                            "title": row["title"],
                            "active": row["runtime"]["active"],
                        }
                    )
            return 200, {"sessions": candidates}
        d = sv._resolve_sid(str(b.get("sessionId", "")))
        session = read(d / "session.json") if d else {}
        if (
            not session.get("cwd")
            or Path(session["cwd"]).resolve() != Path(w["path"]).resolve()
        ):
            raise Problem("wrong_source", "历史会话没有明确匹配当前工作区的 cwd", 403)
        if RT.status(d)["active"]:
            raise Problem("active_run", "请先等待旧会话结束再关联", 409)
        ident = d.name
        if ident in registered:
            raise Problem("already_imported", "此会话已经关联", 409)
        # Default to read-only review; importing history never grants write permission.
        item = {
            "id": ident,
            "taskId": uuid.uuid4().hex,
            "workspaceId": w["id"],
            "kind": "review",
            "title": str(b.get("title") or "历史会话")[:100],
            "created": time.time(),
            "imported": True,
        }
        change_registry(lambda data: data["threads"].__setitem__(ident, item))
        return 200, item
    if op == "threads":
        items = [
            r for r in registry()["threads"].values() if r["workspaceId"] == w["id"]
        ]
        if method == "GET" and len(parts) == 3:
            for item in items:
                d = sv._resolve_sid(item["id"])
                item["runtime"] = (
                    RT.status(d) if d else {"state": "unknown", "active": False}
                )
            return 200, {"threads": items}
        if len(parts) == 3:
            kind = b.get("kind", "implementation")
            if kind not in {"implementation", "question", "discussion", "review"}:
                raise Problem("invalid_thread", "未知会话类型")
            return _create_thread(w, b.get("title"), kind, sv)
        thread = registry()["threads"].get(parts[3])
        d = sv._resolve_sid(parts[3])
        if not thread or thread["workspaceId"] != w["id"] or d is None:
            raise Problem("not_found", "会话不属于当前工作区", 404)
        if thread.get("deletionPending"):
            raise Problem("archive_cleanup_pending", "会话正在删除", 409)
        if method == "GET":
            if parts[-1] == "events":
                limit = max(1, min(500, int(get("limit", "200"))))
                before = get("before")
                rows, total, history_meta = EI.page(
                    d / "events.jsonl",
                    after=max(0, int(get("after", "0"))),
                    limit=limit + 1,
                    tail=get("tail") == "1",
                    before=int(before) if before else None,
                    after_position=int(get("after_offset"))
                    if get("after_offset")
                    else None,
                    before_position=int(get("before_offset"))
                    if get("before_offset")
                    else None,
                )
                if (
                    get("generation")
                    and get("generation") != history_meta["generation"]
                ):
                    raise Problem(
                        "history_changed",
                        "原始日志已重建，旧历史游标不能继续使用，请返回当前记录",
                        409,
                    )
                more = len(rows) > limit or bool(history_meta.get("pageClipped"))
                if more:
                    rows = rows[-limit:] if get("tail") == "1" else rows[:limit]
                from .conversation_record import timeline_events
                from .evidence_redaction import redact

                rows = timeline_events(d / "events.jsonl", rows, history_meta)
                if thread.get("projectless"):
                    from .action_review import timeline_event

                    rows = [timeline_event(row) for row in rows]
                return 200, {
                    **history_meta,
                    "next_offset": max(
                        (r["sourceOffset"] for r in rows),
                        default=int(get("after_offset", "-1")),
                    ),
                    "events": redact(rows),
                    "total": total,
                    "has_more": more,
                    "next_after": max(
                        (r.get("seq", 0) for r in rows), default=int(get("after", "0"))
                    ),
                    "first": min((r.get("seq", 0) for r in rows), default=0),
                }
            if parts[-1] == "live":
                from .private_keys import live_tail
                text, truncated = live_tail(d / "stream.txt")
                return 200, {"text": text, "truncated": truncated}
            raise Problem("invalid_method", "此会话命令需要 POST", 405)
        if parts[-1] == "stop":
            from . import control

            if thread.get("projectless"):
                from .repository_import import cancel_private_import

                cancel_private_import(d, RT.read(d).get("run_id"))
            control.request_stop(d, why="工作台请求步边界暂停", after_turn=True)
            if thread.get("projectless"):
                from . import debugging

                debugging.stop_conversation(thread["id"])
                from .subtasks import cancel_all

                cancel_all(thread["id"], sv,code='user_stop',reason='用户停止了父任务')

            return 200, {"ok": True, "state": "stopping"}
        if parts[-1] == "accept":
            if not git_status(w)["available"]:
                raise Problem(
                    "revision_unavailable",
                    "版本化验收需要 Git；此目录可编辑和运行，但无法绑定验收版本",
                    409,
                )
            if RT.status(d)["active"]:
                raise Problem("active_run", "任务仍在运行，不能标记验收", 409)
            check_revision(w, b.get("expectedRevision"))
            if not str(b.get("reason", "")).strip():
                raise Problem("reason_required", "请输入验收依据")

            def accept(data):
                t = data["threads"][thread["id"]]
                t.update(
                    acceptedAt=time.time(),
                    acceptedRevision=b["expectedRevision"],
                    acceptanceNote=b["reason"],
                )
                return t

            return 200, change_registry(accept)
        if parts[-1] == "archive":
            from . import operations
            if RT.status(d)["active"]:
                raise Problem("active_run", "运行中的任务不能归档", 409)
            if thread.get("projectless") and b.get("archived", True):
                from . import debugging

                debugging.stop_conversation(thread["id"])
                from .subtasks import cancel_all

                cancel_all(thread["id"], sv,code='archived',reason='父会话已归档')
            archived = bool(b.get("archived", True))
            if thread.get("archived") and not archived:
                operations.cancel_waiting_inputs(P.data_root(), d)
            change_registry(lambda data: data["threads"][thread["id"]].update(archived=archived))
            if archived:
                operations.cancel_waiting_inputs(P.data_root(), d)
            return 200, {"ok": True}
        if parts[-1] == "resolve":
            if b.get("confirm") is not True:
                raise Problem("confirmation", "请先核对日志与实际产出")
            return sv._acknowledge_pending(d, wait_for_launch=False)
        if parts[-1] == "approval":
            from . import grants

            pending = pending_approval(d)
            if not pending or pending["sig"] != b.get("sig"):
                raise Problem("approval_expired", "审批已失效，请重新读取", 409)
            event = digest(json.dumps(pending, sort_keys=True).encode())
            if (thread.get("approvalDecision") or {}).get("event") == event:
                raise Problem("approval_expired", "该审批已经决定，请在会话中继续", 409)
            if b.get("decision") == "allow":
                result = grants.grant(
                    pending["sig"],
                    kind=pending.get("gkind", ""),
                    mode=grants.ONCE,
                    why="工作台用户仅允许这一次",
                    src="ui",
                    label=pending.get("label", ""),
                )
            elif b.get("decision") == "deny":
                result = grants.deny(
                    pending["sig"],
                    why="工作台用户拒绝",
                    src="ui",
                    label=pending.get("label", ""),
                )
            else:
                raise Problem("invalid_command", "请选择允许一次或拒绝")
            if result.get("ok") is False:
                return 409, result
            change_registry(
                lambda data: data["threads"][thread["id"]].update(
                    approvalDecision={
                        "event": event,
                        "decision": b["decision"],
                        "at": time.time(),
                    }
                )
            )
            # Do not silently resume with a broader server policy. The next send
            # still follows this thread's persisted kind and writer ownership.
            return 200, dict(
                result, note="决定已保存。请在该会话发送继续，仍使用原会话权限。"
            )
        from .private_keys import text as protect_text
        text = protect_text(str(b.get("text", ""))).strip()
        b = dict(b, text=text)
        if not text:
            raise Problem("invalid_command", "请输入任务要求")
        readonly = thread["kind"] != "implementation"
        from .protocol import validate_command

        validate_command(b)
        options = {
            name: b[name]
            for name in ("min_reads", "min_chars", "max_turns", "ctx")
            if b.get(name) is not None
        }
        if _conversation:
            options["execution_config"] = b["executionConfig"]
            if b.get("attachmentIds"):
                options["attachment_ids"] = b["attachmentIds"]
            if b.get("githubPage"):
                options["github_context"] = b["githubPage"]
        if scheduled_start:
            options["scheduled_start"] = True
        from . import operations
        proc = sv._active_proc(d) if hasattr(sv, "_active_proc") else None
        if proc is not None or operations.waiting_inputs(d):
            if scheduled_start:
                raise Problem("active_run", "定时会话仍有运行正在执行", 409)
            if proc is not None and _conversation and text in {'继续','继续。','continue','resume'} and not (b.get('images') or b.get('attachmentIds')):
                from .subtasks import listing
                active_id=RT.read(d).get('run_id')
                if active_id and any((r.get('worker',{}).get('wakeClaim') or {}).get('runId')==active_id for r in listing(thread['id'])):
                    return 200,dict(session=thread['id'],pid=proc.pid,alreadyRunning=True)
            mode = b.get("mode") or ("steer" if _conversation else None)
            if (readonly and not _conversation) or mode not in {"steer", "queued"}:
                raise Problem(
                    "active_run", "运行中请使用旁问，或选择步边界调整/排队补充", 409
                )
            kind = "interrupt" if mode == "steer" else "say"
            request = {
                "text": text,
                "crew": b.get("crew", "l0"),
                "_request_id": b.get("_request_id"),
                "policy": "readonly" if readonly else getattr(sv, "POLICY", None),
                **options,
            }
            if not sv._queue_pending(d, proc, kind, request):
                raise Problem("queue_full", "已有待办，当前输入尚未接收", 409)
            if kind == "interrupt" and proc is not None:
                from . import control

                control.request_stop(d, why="工作台用户调整要求", after_turn=True)
            return 200, operations.followup_receipt(request, {
                "queued": True,
                "note": "已持久保存；将在执行步边界调整"
                if kind == "interrupt" and proc is not None
                else "已持久排队，前面的运行结束后继续",
            })
        if not readonly:
            status = git_status(w)
            if status.get("protected"):
                raise Problem(
                    "protected_branch",
                    "请创建独立 worktree 或切到功能分支后执行修改",
                    403,
                )
            for other in items:
                od = sv._resolve_sid(other["id"])
                if (
                    other["id"] != thread["id"]
                    and other["kind"] == "implementation"
                    and od
                    and RT.status(od)["active"]
                ):
                    raise Problem(
                        "writer_busy",
                        "已有自动写入任务，请使用旁问或独立 worktree",
                        409,
                    )
        return sv._continue(
            d,
            text,
            crew=b.get("crew", "l0"),
            policy="readonly" if readonly else sv.POLICY,
            wait_for_launch=False,
            **options,
        )
    if op == "files":
        if method == "GET":
            return 200, {"entries": files(w, get("path"))}
    if op == "file":
        if method == "GET":
            view = get("view")
            if view in {"page", "preview", "bytes"}:
                return 200, workspace_file_page(w, get("path"), get("revision", None), get("after", "0"), view)
            return 200, file_read(w, get("path"), editor=True)
        return 200, save_file(w, b)
    if op == "search":
        return 200, {"matches": search(w, get("q"), get("content") == "1")}
    if op == "git":
        if method == "GET":
            if get("view") == "head":
                return 200, git_head(w)
            if get("view") == "changes":
                try:
                    after = int(get("after", "0"))
                except ValueError:
                    after = -1
                if after < 0:
                    raise Problem("invalid_page", "变更列表位置无效")
                return 200, git_changes_page(w, after, get("snapshot"))
            return 200, git_status(w)
        with FileLock(home() / (w["id"] + ".git.lock")):
            check_revision(w, b.get("expectedRevision"))
            cmd = b["action"]
            paths = b.get("paths") or []
            for p in paths:
                file_path(w, p)
            if cmd in {"stage", "unstage"}:
                if not paths:
                    raise Problem("invalid_path", "请选择文件")
                git(
                    w,
                    (["add", "--"] if cmd == "stage" else ["reset", "HEAD", "--"])
                    + paths,
                )
            elif cmd == "stagePatch":
                patch = str(b["patch"]).encode()
                git(w, ["apply", "--cached", "--check", "-"], patch)
                git(w, ["apply", "--cached", "-"], patch)
            elif cmd == "branch":
                branch = str(b.get("branch", ""))
                if (
                    not branch.startswith("agent/")
                    or git(
                        w, ["check-ref-format", "--branch", branch], check=False
                    ).returncode
                ):
                    raise Problem("invalid_branch", "请输入 agent/ 开头的新分支")
                git(w, ["switch", "-c", branch])
            elif cmd == "revert":
                if b.get("confirm") is not True or not paths:
                    raise Problem("confirmation", "请确认撤回选中文件")
                # Refuse deletion of untracked files. Back up every target before
                # restoring any tracked path, including unstaged human edits.
                archive = home() / "revert-backups" / uuid.uuid4().hex
                archive.mkdir(parents=True)
                for name in paths:
                    if git(
                        w, ["ls-files", "--error-unmatch", "--", name], check=False
                    ).returncode:
                        raise Problem("untracked", "未跟踪文件不能自动撤回", 409)
                    p = file_path(w, name)
                    if p.is_file():
                        target = archive / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, target)
                atomic_json(
                    archive / "manifest.json",
                    {"workspace": w, "paths": paths, "revision": b["expectedRevision"]},
                )
                git(w, ["restore", "--worktree", "--", *paths])
            elif cmd == "applyCommit":
                if git_status(w)["files"]:
                    raise Problem("dirty_target", "目标工作区必须先处理未提交改动", 409)
                if git_status(w)["protected"]:
                    raise Problem("protected_branch", "请先选择功能分支", 403)
                source = workspace(b.get("sourceWorkspaceId"))
                if source.get("parent") != w["id"]:
                    raise Problem(
                        "wrong_source", "来源必须是当前工作区创建的 worktree", 403
                    )
                if git_status(source)["files"]:
                    raise Problem("dirty_source", "请先提交来源工作区的改动", 409)
                commit = git(source, ["rev-parse", "HEAD"]).stdout.decode().strip()
                if commit != b.get("sourceCommit"):
                    raise Problem("revision_conflict", "来源提交已经变化", 409)
                baseline = (
                    source.get("baseCommit")
                    or git(w, ["merge-base", "HEAD", commit]).stdout.decode().strip()
                )
                if git(
                    source, ["rev-list", "--merges", baseline + ".." + commit]
                ).stdout.strip():
                    raise Problem(
                        "merge_history", "含合并提交，请在外部 Git 工具审查引入", 409
                    )
                commits = (
                    git(source, ["rev-list", "--reverse", baseline + ".." + commit])
                    .stdout.decode()
                    .split()
                )
                if not commits:
                    raise Problem("empty_change", "没有可引入的提交", 409)
                change_registry(
                    lambda data: data["workspaces"][w["id"]].update(
                        pendingIntegration={"source": source["id"], "commit": commit}
                    )
                )
                git(w, ["cherry-pick", *commits])

                def delivered(data):
                    data["workspaces"][source["id"]]["deliveredCommit"] = commit
                    data["workspaces"][w["id"]].pop("pendingIntegration", None)

                change_registry(delivered)
            elif cmd in {"continueIntegration", "abortIntegration"}:
                if git_status(w)["protected"]:
                    raise Problem("protected_branch", "请在功能分支处理引入", 403)
                pending = w.get("pendingIntegration")
                if not pending:
                    raise Problem("not_found", "没有工作台引入中的操作", 404)
                git(
                    w,
                    ["-c", "core.editor=true", "cherry-pick", "--continue"]
                    if cmd == "continueIntegration"
                    else ["cherry-pick", "--abort"],
                )

                def finish(data):
                    if cmd == "continueIntegration":
                        data["workspaces"][pending["source"]]["deliveredCommit"] = (
                            pending["commit"]
                        )
                    data["workspaces"][w["id"]].pop("pendingIntegration", None)

                change_registry(finish)
            elif cmd == "commit":
                if git_status(w)["protected"]:
                    raise Problem("protected_branch", "请在功能分支提交", 403)
                if not str(b.get("message", "")).strip():
                    raise Problem("invalid_command", "提交说明不能为空")
                git(w, ["commit", "-m", b["message"]])
            elif cmd == "push":
                if git_status(w)["protected"]:
                    raise Problem("protected_branch", "禁止直接推送保护分支", 403)
                if b.get("confirm") is not True:
                    raise Problem("confirmation", "请确认目标远端", 400)
                git(w, ["push", "-u", "origin", "HEAD"])
            else:
                raise Problem("invalid_command", "不支持的 Git 操作")
            return 200, git_status(w)
    if op == "diff":
        path = get("path") or None
        preview = get("view") == "preview"
        if preview and not path:
            raise Problem("invalid_path", "请选择要查看的文件")
        return 200, git_diff(w, path, get("staged") == "1", include_revision=not preview)
    if op == "worktree":
        if method != "POST":
            raise Problem("invalid_method", "需要 POST")
        check_revision(w, b.get("expectedRevision"))
        branch = str(b.get("branch", ""))
        if (
            not branch.startswith("agent/")
            or git(w, ["check-ref-format", "--branch", branch], check=False).returncode
        ):
            raise Problem("invalid_branch", "请输入 agent/ 开头的合法新分支")
        base = b.get("base") or git_status(w)["head"]
        base = (
            git(w, ["rev-parse", "--verify", "--end-of-options", base + "^{commit}"])
            .stdout.decode()
            .strip()
        )
        target = home() / "worktrees" / uuid.uuid4().hex
        target.parent.mkdir(exist_ok=True)
        git(w, ["worktree", "add", "-b", branch, str(target), base])
        child = register(str(target), "worktree", w["id"])
        change_registry(
            lambda data: data["workspaces"][child["id"]].update(baseCommit=base)
        )
        return 200, capability(workspace(child["id"]))
    if op == "handoff":
        if not w.get("parent"):
            raise Problem("wrong_source", "当前工作区不是并行 worktree")
        parent = workspace(w["parent"])
        head = git_status(w)["head"]
        baseline = (
            w.get("baseCommit")
            or git(w, ["merge-base", "HEAD", git_status(parent)["head"]])
            .stdout.decode()
            .strip()
        )
        raw = git(w, ["diff", "--no-ext-diff", "--no-color", baseline, head]).stdout
        return 200, {
            "sourceCommit": head,
            "baseCommit": baseline,
            "parentId": parent["id"],
            "commits": git(w, ["log", "--format=%h %s", baseline + ".." + head])
            .stdout.decode("utf-8", "replace")
            .splitlines(),
            "text": raw[:LIMIT].decode("utf-8", "replace"),
            "truncated": len(raw) > LIMIT,
        }
    if op == "remove-worktree":
        if method != "POST" or b.get("confirm") is not True:
            raise Problem("confirmation", "请确认回收工作树，分支将保留")
        if w.get("kind") != "worktree" or not w.get("parent"):
            raise Problem("wrong_source", "只可回收工作台创建的 worktree", 403)
        check_revision(w, b.get("expectedRevision"))
        s = git_status(w)
        if s["files"]:
            raise Problem("dirty_source", "工作区有未提交改动，不能回收", 409)
        if s["head"] not in {w.get("baseCommit"), w.get("deliveredCommit")}:
            raise Problem("undelivered", "存在未交付提交，请先引入成果", 409)
        activity = lifecycle(sv)
        if (
            any(r["workspaceId"] == w["id"] for r in activity["processes"])
            or any(
                t["workspaceId"] == w["id"] and t["id"] in activity["threads"]
                for t in registry()["threads"].values()
            )
            or any(
                d.workspace["id"] == w["id"] and d.p.poll() is None
                for d in list(_debuggers.values())
            )
        ):
            raise Problem("active_run", "任务、终端或调试仍在运行，不能回收", 409)
        parent = workspace(w["parent"])
        git(parent, ["worktree", "remove", "--", w["path"]])
        change_registry(lambda data: data["workspaces"].pop(w["id"], None))
        return 200, {"ok": True, "parentId": parent["id"]}
    if op == "checks" or op == "terminals":
        if method == "GET":
            return 200, {
                "processes": [
                    process_record(p.stem)
                    for p in (home() / "processes").glob("*.json")
                    if read(p).get("workspaceId") == w["id"]
                ]
            } if (home() / "processes").exists() else {"processes": []}
        if op == "checks":
            return 200, launch(w, b["argv"])
        if b.get("confirm") is not True:
            raise Problem("confirmation", "交互终端按用户权限执行，请明确创建", 400)
        argv = b.get("argv") or (
            [os.environ.get("COMSPEC", "cmd.exe")] if os.name == "nt" else ["/bin/sh"]
        )
        return 200, launch(w, argv, "terminal")
    if op == "debuggers":
        if method == "GET":
            return 200, {
                "debuggers": [
                    {"id": d.id, "alive": d.p.poll() is None, "workspaceId": w["id"]}
                    for d in list(_debuggers.values())
                    if d.workspace["id"] == w["id"]
                ]
            }
        d = Debugger(w)
        _debuggers[d.id] = d
        try:
            atomic_json(
                record_path(d.id),
                dict(
                    id=d.id,
                    workspaceId=w["id"],
                    kind="debug",
                    actor="human",
                    pid=d.p.pid,
                    identity=RT.identity(d.p.pid),
                    ownerPid=os.getpid(),
                    ownerIdentity=RT.identity(os.getpid()),
                    state="running",
                    started=time.time(),
                    revision=current_revision(w),
                    exitCode=None,
                ),
            )
        except Exception:
            d.close()
            raise
        return 200, {"id": d.id, "workspaceId": w["id"]}
    if op == "presets":
        if method == "GET":
            return 200, {"presets": registry()["presets"].get(w["id"], [])}
        values = b.get("presets")
        if not isinstance(values, list):
            raise Problem("invalid_command", "运行配置应为数组")
        change_registry(lambda data: data["presets"].__setitem__(w["id"], values))
        return 200, {"presets": values}
    if op == "reviews":
        if method == "GET":
            return 200, {"reviews": registry()["reviews"].get(w["id"], [])}
        check_revision(w, b.get("expectedRevision"))
        if not git_status(w)["available"]:
            raise Problem("revision_unavailable", "版本化审查需要 Git", 409)
        if b.get("id"):
            if b.get("state") not in {"open", "resolved", "needs_review"}:
                raise Problem("invalid_review", "未知审查状态")
            if not str(b.get("reason", "")).strip():
                raise Problem("reason_required", "请输入复核依据")

            def resolve(data):
                finding = next(
                    (r for r in data["reviews"].get(w["id"], []) if r["id"] == b["id"]),
                    None,
                )
                if not finding:
                    raise Problem("not_found", "发现不存在", 404)
                finding.update(
                    state=b["state"],
                    resolution=b["reason"],
                    resolutionRevision=current_revision(w),
                    resolvedAt=time.time(),
                )
                return finding

            return 200, change_registry(resolve)
        review = {
            "id": uuid.uuid4().hex,
            "revision": current_revision(w),
            "path": b.get("path"),
            "line": b.get("line"),
            "text": str(b.get("text", ""))[:8000],
            "state": b.get("state", "open"),
            "created": time.time(),
        }
        change_registry(
            lambda data: data["reviews"].setdefault(w["id"], []).append(review)
        )
        return 200, review
    raise Problem("not_found", "没有这个工作台接口", 404)
