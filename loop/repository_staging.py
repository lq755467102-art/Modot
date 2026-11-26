"""Track and reclaim named staging directories created by repository imports."""
# 主要引用方包括 `repository_import.py` 和 `server.py`。

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import uuid

from . import authority_paths, workbench as W


PREFIX = ".modot-import-"
_REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT


def _journals():
    return W.home() / "repository-import-staging"


def _identity(info):
    return info.st_dev, info.st_ino, getattr(info, "st_birthtime_ns", info.st_ctime_ns)


def _key(value):
    return (isinstance(value, (list, tuple)) and len(value) == 3
            and all(type(part) is int and part >= 0 for part in value)
            and value[1] > 0 and value[2] > 0)


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    return kernel


def _native(path):
    return "\\\\?\\" + str(path)


@contextmanager
def _pin(kernel, path, *, delete=False):
    access = 0x80 | (0x10000 if delete else 0)  # FILE_READ_ATTRIBUTES, DELETE
    # Open the entry itself, including for directories. Withhold FILE_SHARE_DELETE
    # to prevent replacement while examining it and while removing its children.
    handle = kernel.CreateFileW(_native(path), access, 3, None, 3, 0x02200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = os.lstat(_native(path))
        if info.st_file_attributes & _REPARSE:
            raise OSError("staging entry is a reparse point")
        yield handle, info
    finally:
        kernel.CloseHandle(handle)


def _remove_entry(kernel, path, expected=None):
    with _pin(kernel, path, delete=True) as (handle, info):
        directory = stat.S_ISDIR(info.st_mode)
        if expected is not None and (not directory or _identity(info) != expected):
            raise OSError("staging directory identity changed")
        if directory:
            with os.scandir(_native(path)) as entries:
                for entry in entries:
                    try:
                        _remove_entry(kernel, path / entry.name)
                    except FileNotFoundError:
                        pass
        # Remove by the verified handle. Read-only Git packs can be removed
        # without changing attributes on a potentially hard-linked file.
        if info.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
            kind, disposition = 21, wintypes.DWORD(0x11)  # FileDispositionInfoEx, DELETE | IGNORE_READONLY
        else:
            kind, disposition = 4, ctypes.c_ubyte(1)  # FileDispositionInfo
        if not kernel.SetFileInformationByHandle(handle, kind, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise ctypes.WinError(ctypes.get_last_error())


@dataclass(frozen=True)
class Staging:
    base: Path
    base_key: tuple
    stage_key: tuple
    journal: Path
    project_id: str | None = None

    @property
    def path(self):
        return self.base / (PREFIX + self.journal.stem)

    def cleanup(self):
        try:
            if (not self.base.is_absolute() or self.base.drive.startswith("\\\\")
                    or (self.project_id is None and self.base.name != "projects")
                    or (self.project_id is not None and not re.fullmatch(r"[0-9a-f]{32}", self.project_id))
                    or not _key(self.base_key) or not _key(self.stage_key)):
                return False
            kernel = _kernel()
            with _pin(kernel, self.base) as (_, info):
                if (not stat.S_ISDIR(info.st_mode) or _identity(info) != self.base_key
                        or self.base.resolve(strict=True) != self.base):
                    return False
                try:
                    _remove_entry(kernel, self.path, self.stage_key)
                except FileNotFoundError:
                    pass
            self.journal.unlink(missing_ok=True)
            return True
        except (OSError, RuntimeError, ValueError):
            # The journal remains available if a lock, link or identity change
            # prevented safe cleanup. Never fall back to deleting by name.
            return False


def create(base, *, project_id=None):
    pid = os.getpid()
    owner = W.RT.identity(pid)
    base_key = _identity(base.lstat())
    journal = _journals() / (uuid.uuid4().hex[:12] + ".json")
    path = base / (PREFIX + journal.stem)
    path.mkdir()
    stage = Staging(base, base_key, _identity(path.lstat()), journal, project_id)
    try:
        W.atomic_json(journal, dict(version=1, base=str(base), baseKey=stage.base_key, projectId=project_id,
                                   stageKey=stage.stage_key, pid=pid, owner=owner))
    except OSError:
        stage.cleanup()
        raise
    return stage


def _registered(path, registry):
    resources = (resource for project in registry.get("projects", {}).values()
                 for resource in project.get("resources", {}).values())
    return any(isinstance(row.get("path"), str) and Path(row["path"]).is_relative_to(path)
               for rows in (resources, registry["workspaces"].values()) for row in rows)


def _project_base(registry, project_id, base):
    if not isinstance(project_id, str) or not re.fullmatch(r"[0-9a-f]{32}", project_id):
        return False
    owner = registry.get("projects", {}).get(project_id, {})
    primary = owner.get("folderResourceId")
    root = owner.get("resources", {}).get(primary, {}).get("path")
    if root and Path(root) == base:
        return True
    return any(binding.get("projectId") == project_id and binding.get("sourcePath") == root
               and binding.get("path") and Path(binding["path"]) == base
               for thread in registry["threads"].values()
               if (binding := thread.get("codeWorktrees", {}).get(primary)))


def recover():
    """Only use the application's journal. Prefix-only and legacy directories are unknown."""
    if os.name != "nt":
        return
    try:
        for journal in _journals().glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{12}", journal.stem):
                continue
            try:
                info = journal.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_file_attributes & _REPARSE
                        or info.st_size > 8192):
                    continue
                row = W.read(journal)
                if not isinstance(row, dict) or row.get("version") != 1:
                    continue
                base, base_key, key = row.get("base"), row.get("baseKey"), row.get("stageKey")
                pid, owner = row.get("pid"), row.get("owner")
                if (not isinstance(base, str) or not _key(base_key) or not _key(key)
                        or type(pid) is not int or not 0 < pid < 2**32
                        or not isinstance(owner, str) or not re.fullmatch(r"[0-9]{1,20}", owner)):
                    continue
                if W.RT.identity(pid) in (owner, "unknown"):
                    continue
                registry = W.registry()
                stage = Staging(Path(base), tuple(base_key), tuple(key), journal, row.get("projectId"))
                if stage.project_id is not None and not _project_base(registry, stage.project_id, stage.base):
                    continue
                if not authority_paths.protected(stage.path, "delete") and not _registered(stage.path, registry):
                    stage.cleanup()
            except (W.Problem, OSError, RuntimeError, ValueError):
                continue
    except OSError:
        pass
