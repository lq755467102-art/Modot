"""Windows exclusive exact-byte writes. Callers retain a durable pre-write backup.

This is deliberately not rename-based: a share-zero handle prevents another
editor from opening/replacing the file between the version check and the write.
A crash can leave a partial file; callers must retain unknown and the backup.
CreateFile sharing: https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
"""

# 主要引用方包括 `workbench.py` 和 `integrations.py`。

import ctypes
from ctypes import wintypes as T
import hashlib
import os
from pathlib import Path
import time


class Conflict(ValueError):
    # 把稳定的错误码和细节交给上层转换成可核对的写入冲突。
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


# 打开已取得的 Windows 文件句柄，不再次按文件名解析路径。
def _open_stream(handle, kernel):
    import msvcrt

    try:
        fd = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDWR)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, "r+b", buffering=0)
    except BaseException:
        os.close(fd)
        raise


# 调用方提供原版本与受控根目录。
# `raw is None` 表示删除且必须有明确原版本。
# 写入和删除都在独占句柄上核对身份。
def write(path, root, raw, expected, limit):
    if raw is not None and (not isinstance(raw, bytes) or len(raw) > limit):
        raise Conflict("large_file", "独占写入内容无效或超出上限")
    if raw is None and expected is None:
        raise Conflict("revision_conflict", "删除需要已有文件的明确版本")
    if os.name != "nt":
        raise Conflict("unsupported_platform", "首批受控恢复仅支持Windows独占文件句柄")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        T.LPCWSTR,
        T.DWORD,
        T.DWORD,
        T.LPVOID,
        T.DWORD,
        T.DWORD,
        T.HANDLE,
    ]
    kernel.CreateFileW.restype = T.HANDLE
    kernel.CloseHandle.argtypes = [T.HANDLE]
    kernel.CloseHandle.restype = T.BOOL

    class Info(ctypes.Structure):
        _fields_ = [
            ("attributes", T.DWORD),
            ("created", T.FILETIME),
            ("accessed", T.FILETIME),
            ("written", T.FILETIME),
            ("volume", T.DWORD),
            ("sizeHigh", T.DWORD),
            ("sizeLow", T.DWORD),
            ("links", T.DWORD),
            ("indexHigh", T.DWORD),
            ("indexLow", T.DWORD),
        ]

    kernel.GetFileInformationByHandle.argtypes = [T.HANDLE, ctypes.POINTER(Info)]
    kernel.GetFileInformationByHandle.restype = T.BOOL
    kernel.GetFinalPathNameByHandleW.argtypes = [T.HANDLE, T.LPWSTR, T.DWORD, T.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = T.DWORD
    path = Path(path).absolute()
    root = Path(root).resolve()
    name = str(path)
    native = (
        ("\\\\?\\UNC\\" + name[2:]) if name.startswith("\\\\") else "\\\\?\\" + name
    )
    deadline = time.monotonic() + 1
    while True:
        handle = kernel.CreateFileW(
            native,
            0xC0000000 | (0x10000 if raw is None else 0),
            0,
            None,
            1 if expected is None else 3,
            0x00200000,
            None,
        )
        if handle not in (None, ctypes.c_void_p(-1).value):
            break
        error = ctypes.get_last_error()
        if error not in (5, 32, 33) or time.monotonic() >= deadline:
            raise Conflict(
                "file_busy", "无法独占打开当前文件，未写入；请等待外部编辑器释放文件"
            )
        time.sleep(0.02)
    try:
        info = Info()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & 0x400 or info.links != 1:
            raise Conflict(
                "linked_file", "符号链接、重解析点或硬链接文件不支持原位恢复"
            )
        if (info.sizeHigh << 32 | info.sizeLow) > limit:
            raise Conflict("large_file", "当前文件超过受控恢复上限")
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise Conflict("path_escape", "无法核对已打开文件的实际路径")
        final = buffer.value
        final = (
            "\\\\" + final[8:]
            if final.startswith("\\\\?\\UNC\\")
            else final[4:]
            if final.startswith("\\\\?\\")
            else final
        )
        actual = Path(final)
        if os.path.normcase(str(actual)) != os.path.normcase(
            str(path)
        ) or not actual.is_relative_to(root):
            raise Conflict("path_escape", "已打开文件的目标路径已经变化")
        owned = handle
        handle = None
        stream = _open_stream(owned, kernel)
        with stream:
            before = stream.read(limit + 1)
            if (
                before != b""
                if expected is None
                else hashlib.sha256(before).hexdigest() != expected
            ):
                raise Conflict("revision_conflict", "独占打开后发现外部编辑，未写入")
            if raw is None:
                import msvcrt

                kernel.SetFileInformationByHandle.argtypes = [
                    T.HANDLE,
                    ctypes.c_int,
                    T.LPVOID,
                    T.DWORD,
                ]
                kernel.SetFileInformationByHandle.restype = T.BOOL
                remove = ctypes.c_ubyte(1)
                if not kernel.SetFileInformationByHandle(
                    msvcrt.get_osfhandle(stream.fileno()),
                    4,
                    ctypes.byref(remove),
                    ctypes.sizeof(remove),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                return None
            stream.seek(0)
            remaining = memoryview(raw)
            while remaining:
                count = stream.write(remaining)
                if not count:
                    raise OSError("文件写入中断，需核对保留备份")
                remaining = remaining[count:]
            stream.truncate(len(raw))
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            observed = stream.read(limit + 1)
            if observed != raw:
                raise OSError("独占写入后字节核验失败，需核对保留备份")
            return observed
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
