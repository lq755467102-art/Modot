"""Cross-process locks and durable JSON replacement (standard library only).

Lock files are stable: never unlink a lock file while another process may open it.
The kernel releases ownership when a process exits, including abnormal exits.
"""

# 主要引用方包括 `workbench.py` 和 `code_service.py`。

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


# 跨进程锁实例保存固定 `path` 和当前占用的 `file` 句柄。
# `timeout` 限制抢锁等待，`check` 让等待响应取消，`shared` 用于可并行的共享持有者。
class FileLock:
    def __init__(self, path, timeout=10, *, check=None, shared=False, create_parent=True):
        self.path = Path(path)
        self.timeout = timeout
        self.file = None
        self.check = check
        self.shared = shared
        self.create_parent = create_parent

    def acquire(self):
        if self.create_parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        # Windows byte-range locks can extend past EOF, so an empty lock
        # file is valid even when its creator exited before acquiring it.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
        f = os.fdopen(fd, "r+b", buffering=0)
        while True:
            if self.check is not None:
                try:
                    self.check()
                except BaseException:
                    f.close()
                    raise
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt

                    if self.shared:
                        _windows_shared_lock(f)
                    else:
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), (fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                self.file = f
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    f.close()
                    return False
                time.sleep(0.02)

    def release(self):
        f, self.file = self.file, None
        if f is None:
            return
        try:
            f.seek(0)
            if os.name == "nt":
                import msvcrt

                if self.shared:
                    _windows_shared_lock(f, unlock=True)
                else:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError("lock busy: %s" % self.path)
        return self

    def __exit__(self, *_):
        self.release()


def _windows_shared_lock(stream, *, unlock=False):
    """Lock the same byte as CRT exclusive locks, shared across processes."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
    overlap = Overlapped()
    if unlock:
        fn = kernel.UnlockFileEx
        fn.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        fn.restype = wintypes.BOOL
        ok = fn(handle, 0, 1, 0, ctypes.byref(overlap))
    else:
        fn = kernel.LockFileEx
        fn.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        fn.restype = wintypes.BOOL
        ok = fn(handle, 1, 0, 1, 0, ctypes.byref(overlap))  # FAIL_IMMEDIATELY; no EXCLUSIVE flag.
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def read_text(path):
    """Read UTF-8 metadata through short Windows sharing violations.

    CPython's CRT file open reports EACCES (13) without a winerror field.
    Permanent denial is still raised after a one-second bounded retry.
    """
    deadline = time.monotonic() + 1
    while True:
        try:
            return Path(path).read_text(encoding="utf-8")
        except PermissionError as error:
            transient = (
                getattr(error, "winerror", None) in (5, 32, 33) or error.errno == 13
            )
            if os.name != "nt" or not transient or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Repeating a long metadata basename can make the temporary path exceed
    # Windows limits even when the final file itself is valid. Same-directory
    # randomness preserves atomic replacement without duplicating that name.
    fd, name = tempfile.mkstemp(prefix=".h-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        # Windows readers may briefly hold a handle without FILE_SHARE_DELETE.
        # Retain the complete old file and retry only these transient cases.
        deadline = time.monotonic() + 1
        while True:
            try:
                os.replace(name, path)
                break
            except PermissionError as error:
                if (
                    os.name != "nt"
                    or getattr(error, "winerror", None) not in (5, 32, 33)
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(0.01)
    finally:
        if os.path.exists(name):
            os.unlink(name)
