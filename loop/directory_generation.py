"""Process-local Windows change notifications invalidate caches without tree polling.

Notifications are conservative (including ignored files). Missing handles, root
replacement, and unsupported platforms never certify an unchanged directory.
"""
# 主要引用方包括 `safe_executor.py` 和 `verification_workspace.py`。
import atexit
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

_lock = threading.RLock()
_watches = {}
_counter = 0
_owner = ThreadPoolExecutor(max_workers=1, thread_name_prefix='directory-watch')


def generation(path):
    # Remote filesystems may omit change notifications. They must retain full
    # validation rather than gaining an unchanged-directory certificate.
    if os.name != 'nt' or Path(path).drive.startswith('\\\\'):
        return None
    # Windows cancels directory notifications when their creating thread exits.
    # Tool calls use short-lived worker threads, so own watches on one stable
    # thread. No polling thread or per-directory process is needed.
    return _owner.submit(_generation, path).result()


def _generation(path):
    global _counter
    path = Path(path).resolve(strict=True)
    if os.name != 'nt':
        return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.FindFirstChangeNotificationW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
    kernel.FindFirstChangeNotificationW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.FindNextChangeNotification.argtypes = [wintypes.HANDLE]
    kernel.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
    key = str(path)
    info = path.stat()
    identity = (info.st_dev, info.st_ino)
    with _lock:
        old = _watches.get(key)
        if old and old['identity'] != identity:
            kernel.FindCloseChangeNotification(old['handle']); del _watches[key]; old = None
        if old:
            status = kernel.WaitForSingleObject(old['handle'], 0)
            if status == 258:  # WAIT_TIMEOUT: no changes since last acknowledged signal.
                return old['generation']
            if status != 0 or not kernel.FindNextChangeNotification(old['handle']):
                kernel.FindCloseChangeNotification(old['handle']); del _watches[key]
                return None
            _counter += 1
            old['generation'] = _counter
            return _counter
        # Subtree names, writes, sizes, attributes and security changes invalidate.
        handle = kernel.FindFirstChangeNotificationW(key, True, 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x100)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        while len(_watches) >= 32:
            oldest = next(iter(_watches)); kernel.FindCloseChangeNotification(_watches.pop(oldest)['handle'])
        _counter += 1
        _watches[key] = dict(handle=handle, generation=_counter, identity=identity)
        return _counter


def close():
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
        with _lock:
            for row in _watches.values(): kernel.FindCloseChangeNotification(row['handle'])
            _watches.clear()


atexit.register(close)
