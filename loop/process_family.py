"""Owned Windows process family lifetime, independent of adapter PID lifetime."""

# 主要引用方包括 `workbench.py` 和 `safe_executor.py`。

import ctypes
import os
import time


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("times", ctypes.c_longlong * 4),
        ("faults", ctypes.c_uint32),
        ("total", ctypes.c_uint32),
        ("active", ctypes.c_uint32),
        ("terminated", ctypes.c_uint32),
    ]


def _active(kernel, handle):
    from ctypes import wintypes
    query = kernel.QueryInformationJobObject
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                      wintypes.DWORD, ctypes.c_void_p]
    query.restype = wintypes.BOOL
    info = _Accounting()
    if not query(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
        raise OSError("进程族状态未确认")
    return info.active


def job_quiet(name):
    if os.name != "nt":
        return True
    from ctypes import wintypes
    from . import isolate

    kernel = isolate._lib()["k32"]
    open_job = kernel.OpenJobObjectW
    open_job.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    open_job.restype = wintypes.HANDLE
    handle = open_job(4, False, name)  # JOB_OBJECT_QUERY
    if not handle:
        return ctypes.get_last_error() == 2  # ERROR_FILE_NOT_FOUND
    try:
        return _active(kernel, handle) == 0
    finally:
        kernel.CloseHandle(handle)


class Family:
    def __init__(self, process=None, memory=None, processes=None, *, name=None):
        self.handle = None
        if os.name != "nt":
            return
        from . import isolate
        from ctypes import wintypes

        lib = isolate._lib()
        self.kernel = lib["k32"]
        ctypes.set_last_error(0)
        self.handle = self.kernel.CreateJobObjectW(None, name)
        already_exists = name and ctypes.get_last_error() == 183
        limits = lib["EXT"]()
        limits.BasicLimitInformation.LimitFlags = (
            isolate.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if memory is not None:
            limits.BasicLimitInformation.LimitFlags |= 0x00000200  # JOB_MEMORY
            limits.JobMemoryLimit = memory
        if processes is not None:
            limits.BasicLimitInformation.LimitFlags |= 0x00000008  # ACTIVE_PROCESS
            limits.BasicLimitInformation.ActiveProcessLimit = processes
        try:
            if (
                not self.handle or already_exists
                or not self.kernel.SetInformationJobObject(
                    self.handle,
                    isolate.JobObjectExtendedLimitInformation,
                    ctypes.byref(limits),
                    ctypes.sizeof(limits),
                )
                or (process is not None and not self.kernel.AssignProcessToJobObject(
                    self.handle, int(process._handle)
                ))
            ):
                raise OSError("无法建立调试进程树所有权")
            self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self.kernel.TerminateJobObject.restype = wintypes.BOOL
            self.kernel.QueryInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.c_void_p,
            ]
            self.kernel.QueryInformationJobObject.restype = wintypes.BOOL
        except Exception:
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
            raise

    def memory_limit(self):
        """Read the applied Job limit, rather than echoing a requested value."""
        if not self.handle:
            raise OSError('隔离进程族已关闭，无法核对内存额度')
        from . import isolate
        info = isolate._lib()['EXT']()
        if not self.kernel.QueryInformationJobObject(
            self.handle, isolate.JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise OSError('无法读取隔离进程的实际内存额度')
        return int(info.JobMemoryLimit) if info.BasicLimitInformation.LimitFlags & 0x200 else None

    def close(self):
        if not self.handle:
            return

        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise OSError("调试进程树终止未确认")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if _active(self.kernel, self.handle) == 0:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
                return
            time.sleep(0.02)
        raise OSError("调试子进程退出尚未确认")
