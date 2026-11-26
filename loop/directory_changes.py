"""Bounded, process-local Windows path journal for isolated copy updates.

Missing/overflowed notifications return None, never an empty delta. Watches live
on the same persistent owner thread as the directory-generation certificates.
"""
# 主要引用方包括 `safe_executor.py` 和 `verification_workspace.py`。
import atexit
import ctypes as C
from ctypes import wintypes as T
import os
from pathlib import Path
import struct

from .directory_generation import _owner

_watches = {}
_serial = 0


class Overlapped(C.Structure):
    _fields_ = [('Internal', C.c_size_t), ('InternalHigh', C.c_size_t),
                ('Offset', T.DWORD), ('OffsetHigh', T.DWORD), ('hEvent', T.HANDLE)]


def _kernel():
    k = C.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.argtypes = [T.LPCWSTR, T.DWORD, T.DWORD, C.c_void_p, T.DWORD, T.DWORD, T.HANDLE]
    k.CreateFileW.restype = T.HANDLE
    k.ReadDirectoryChangesW.argtypes = [T.HANDLE, C.c_void_p, T.DWORD, T.BOOL, T.DWORD,
                                      C.c_void_p, C.POINTER(Overlapped), C.c_void_p]
    k.GetOverlappedResult.argtypes = [T.HANDLE, C.POINTER(Overlapped), C.POINTER(T.DWORD), T.BOOL]
    k.CancelIoEx.argtypes = [T.HANDLE, C.POINTER(Overlapped)]
    k.CloseHandle.argtypes = [T.HANDLE]
    return k


def _arm(k, w):
    w['overlapped'] = Overlapped()
    return k.ReadDirectoryChangesW(w['handle'], w['buffer'], len(w['buffer']), True,
                                  0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x100,
                                  None, C.byref(w['overlapped']), None)


def _close(k, w):
    k.CancelIoEx(w['handle'], C.byref(w['overlapped']))
    size = T.DWORD()
    k.GetOverlappedResult(w['handle'], C.byref(w['overlapped']), C.byref(size), True)
    k.CloseHandle(w['handle'])


def changes(path, cursor=None):
    return _observe(path, cursor, source_only=False)


def source_generation(path, *, projection=None):
    """Observe source changes, optionally within a verified ordinary-folder view."""
    return _observe(path, None, source_only=True, projection=projection)[0]


def _observe(path, cursor, *, source_only, projection=None):
    if os.name != 'nt' or Path(path).drive.startswith('\\\\'):
        return None, None
    return _owner.submit(_changes, path, cursor, source_only, projection).result()


def _visible_change(entries, since, previous, excluded):
    # Both endpoints under the publisher's shared lock are known. The journal
    # also sees its short-lived root-level staging file between these endpoints.
    edges = {}
    for seq, name, action in reversed(entries):
        if seq <= since:
            break
        if name in previous and name in excluded:
            continue
        possible_stage = '/' not in name and name.startswith('.modot-record-') and name.endswith('.tmp')
        if name not in previous and name not in excluded and not possible_stage:
            return True
        latest = edges.get(name, (action, action))[1]
        edges[name] = (action, latest)
    # Ownership alone may have changed even if the watch has no file event.
    if (previous ^ excluded) - edges.keys():
        return True
    for name, (first, last) in edges.items():
        # FILE_ACTION_ADDED (1), REMOVED (2), RENAMED_OLD/NEW_NAME (4/5).
        hidden_before = name in previous or first == 1 or (name in excluded and first == 5)
        hidden_after = name in excluded or last in (2, 4)
        if not (hidden_before and hidden_after):
            return True
    return False


def _projected_generation(w, scope, excluded):
    saved = w.get('projection')
    version = saved[3] if saved else 0
    if (not saved or saved[0] != scope or saved[1] < w['floor']
            or _visible_change(w['entries'], saved[1], saved[2], excluded)):
        version += 1
    w['projection'] = (scope, w['sequence'], excluded, version)
    return w['serial'], version


def _changes(path, cursor, source_only, projection):
    global _serial
    k = _kernel()
    root = Path(path).resolve(strict=True)
    key = str(root)
    info = root.stat()
    identity = (info.st_dev, info.st_ino)
    w = _watches.get(key)
    if w and w['identity'] != identity:
        _close(k, _watches.pop(key)); w = None
    if w is None:
        handle = k.CreateFileW(key, 1, 7, None, 3, 0x02000000 | 0x40000000, None)
        if not handle or handle == C.c_void_p(-1).value:
            return None, None
        _serial += 1
        w = dict(handle=handle, buffer=C.create_string_buffer(65536), identity=identity,
                 serial=_serial, sequence=0, source_sequence=0, floor=0, entries=[])
        if not _arm(k, w):
            k.CloseHandle(handle)
            return None, None
        while len(_watches) >= 32:
            _close(k, _watches.pop(next(iter(_watches))))
        _watches[key] = w
    for _ in range(128):
        size = T.DWORD()
        if not k.GetOverlappedResult(w['handle'], C.byref(w['overlapped']), C.byref(size), False):
            if C.get_last_error() == 996:  # ERROR_IO_INCOMPLETE
                break
            _close(k, _watches.pop(key))
            return None, None
        raw = w['buffer'].raw[:size.value]
        # Re-arm before parsing; Windows retains notifications between reads.
        if not _arm(k, w):
            _close(k, _watches.pop(key))
            return None, None
        w['sequence'] += 1
        if not raw:
            w['source_sequence'] = w['sequence']
            w['entries'].clear(); w['floor'] = w['sequence']
            continue
        offset = 0
        while True:
            following, action, length = struct.unpack_from('<III', raw, offset)
            name = raw[offset+12:offset+12+length].decode('utf-16-le', 'surrogatepass').replace('\\', '/')
            w['entries'].append((w['sequence'], name, action))
            if name.split('/', 1)[0].casefold() != '.git':
                w['source_sequence'] = w['sequence']
            if not following:
                break
            offset += following
        if len(w['entries']) > 8192:
            w['entries'].clear(); w['floor'] = w['sequence']
    else:  # Busy writer: no stable delta certificate.
        return None, None
    if source_only:
        if projection is not None:
            return _projected_generation(w, *projection), None
        return (w['serial'], w['source_sequence']), None
    current = (w['serial'], w['sequence'])
    valid = cursor and cursor[0] == w['serial'] and w['floor'] <= cursor[1] <= w['sequence']
    return current, ({name for sequence, name, _ in w['entries'] if sequence > cursor[1]} if valid else None)


def close():
    if os.name == 'nt':
        k = _kernel()
        for w in _watches.values():
            _close(k, w)
        _watches.clear()


atexit.register(close)
