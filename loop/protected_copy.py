"""Lease-owned bookkeeping; no state is trusted across process restarts."""
# 主要引用方包括 `safe_executor.py` 和 `role_coordination.py`。
from contextvars import ContextVar
from pathlib import Path, PureWindowsPath
import shutil

current = ContextVar('protected_copy', default=None)


def remove(root, relative, *, prune_empty=False):
    from . import workbench as W
    root = Path(root).resolve()
    name = PureWindowsPath(relative)
    if name.is_absolute() or name.drive or '..' in name.parts or not name.parts:
        raise W.Problem('cache_changed', '验证路径不在副本内', 409)
    path = root / relative
    if not path.parent.resolve().is_relative_to(root):
        raise W.Problem('cache_changed', '验证路径已被重定向', 409)
    if path.is_dir() and not path.is_symlink() and not getattr(path, 'is_junction', lambda: False)():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()
    if prune_empty:
        parent = path.parent
        while parent != root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
