"""Verify local listener ownership, including the launched process's descendants."""

# 主要引用方包括 `previews.py`。

import ctypes
import os
import socket
import struct
from pathlib import Path
from . import runtime as RT


# `previews.py` 传入正在检查的进程与端口。
# 先核对进程创建身份，再查监听或具体连接的归属，避免进程号重用时认错预览。
def owned_listener(port, pid, identity, client_port=None):
    if RT.identity(pid) != identity or identity in (None, "unknown"):
        return False
    if os.name == "nt":
        from ctypes import wintypes as T

        class Row(ctypes.Structure):
            _fields_ = [
                (name, T.DWORD)
                for name in ("state", "local", "port", "remote", "remotePort", "pid")
            ]

        size = T.DWORD(0)
        api = ctypes.WinDLL("iphlpapi")
        api.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)
        buffer = ctypes.create_string_buffer(size.value)
        if api.GetExtendedTcpTable(buffer, ctypes.byref(size), False, 2, 5, 0):
            return False
        count = T.DWORD.from_buffer(buffer).value
        rows = (Row * count).from_buffer(buffer, ctypes.sizeof(T.DWORD))
        owners = {
            int(row.pid)
            for row in rows
            if row.state == (5 if client_port else 2)
            and socket.ntohs(row.port & 0xFFFF) == port
            and (
                client_port is None
                or socket.ntohs(row.remotePort & 0xFFFF) == client_port
            )
            and socket.inet_ntoa(struct.pack("<I", row.local))
            in {"0.0.0.0", "127.0.0.1"}
        }

        class Entry(ctypes.Structure):
            _fields_ = [
                ("size", T.DWORD),
                ("usage", T.DWORD),
                ("pid", T.DWORD),
                ("heap", ctypes.c_size_t),
                ("module", T.DWORD),
                ("threads", T.DWORD),
                ("parent", T.DWORD),
                ("priority", T.LONG),
                ("flags", T.DWORD),
                ("exe", T.WCHAR * 260),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.restype = T.HANDLE
        kernel.CloseHandle.argtypes = [T.HANDLE]
        kernel.Process32FirstW.argtypes = [T.HANDLE, ctypes.POINTER(Entry)]
        kernel.Process32NextW.argtypes = [T.HANDLE, ctypes.POINTER(Entry)]
        handle = kernel.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            return False
        parents = {}
        try:
            entry = Entry()
            entry.size = ctypes.sizeof(Entry)
            more = kernel.Process32FirstW(handle, ctypes.byref(entry))
            while more:
                parents[int(entry.pid)] = int(entry.parent)
                more = kernel.Process32NextW(handle, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(handle)
    else:
        owners, parents = set(), {}
        inodes = set()
        try:
            for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
                row = line.split()
                if (
                    int(row[1].split(":")[1], 16) == port
                    and row[3] == ("01" if client_port else "0A")
                    and (
                        client_port is None
                        or int(row[2].split(":")[1], 16) == client_port
                    )
                    and row[1].split(":")[0] in {"00000000", "0100007F"}
                ):
                    inodes.add(row[9])
            for directory in Path("/proc").iterdir():
                if not directory.name.isdigit():
                    continue
                try:
                    parents[int(directory.name)] = int(
                        (directory / "stat").read_text().rsplit(")", 1)[1].split()[1]
                    )
                    if any(
                        os.readlink(fd)
                        in {"socket:[" + inode + "]" for inode in inodes}
                        for fd in (directory / "fd").iterdir()
                    ):
                        owners.add(int(directory.name))
                except OSError:
                    continue
        except OSError:
            return False
    return ancestry_owned(owners, parents, pid, identity)


def ancestry_owned(owners, parents, pid, identity):
    if identity in (None, "unknown"):
        return False
    for owner in owners:
        seen = set()
        current = owner
        child_birth = None
        while current not in seen and current:
            birth = RT.identity(current)
            if (
                birth in (None, "unknown")
                or int(birth) < int(identity)
                or (child_birth is not None and int(birth) > child_birth)
            ):
                break
            if current == pid:
                return birth == identity
            seen.add(current)
            child_birth = int(birth)
            current = parents.get(current, 0)
    return False
