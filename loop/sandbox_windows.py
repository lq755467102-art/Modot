"""Native process setup, suspended until private pipes and job ownership exist."""

# 主要引用方包括 `safe_executor.py`。

import ctypes
from ctypes import wintypes as wt
import io
import msvcrt
import os
import subprocess
from . import contain as C
from .process_family import Family


# `safe_executor.py` 提供已准备好的执行目录与隔离配置。
# 原生进程和管道先建立，进程族限制成功后才开始运行。
def launch(argv, cwd, env, profile, merge=False, memory=1024*1024*1024, processes=32, duplex=False, *,
           internet_client=False):
    k = C._k()
    k.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    k.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    k.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(C._STARTUPINFOEXW), ctypes.POINTER(C._PROCESS_INFORMATION)]
    handles = []
    proc = family = None
    attrs = None
    initialized = False
    internet_sid = ctypes.c_void_p()
    try:
        out_r, out_w = C.make_pipe()
        handles.extend([out_r,out_w])
        err_r, err_w = C.make_pipe()
        handles.extend([err_r,err_w])
        in_r, in_w = C.make_pipe(keep_read=True)
        handles.extend([in_r,in_w])
        size = ctypes.c_size_t()
        k.InitializeProcThreadAttributeList(None, 3, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        if not k.InitializeProcThreadAttributeList(attrs, 3, 0, ctypes.byref(size)):
            raise OSError("process attribute initialization failed")
        initialized = True
        caps = C._SECURITY_CAPABILITIES(profile["sid"], None, 0, 0)
        if internet_client:
            class SidAndAttributes(ctypes.Structure):
                _fields_ = [("sid", ctypes.c_void_p), ("attributes", wt.DWORD)]

            # Only repository import uses internetClient. Normal task copies
            # have no capabilities on the process token.
            convert = ctypes.WinDLL("advapi32", use_last_error=True).ConvertStringSidToSidW
            convert.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
            convert.restype = wt.BOOL
            if not convert("S-1-15-3-1", ctypes.byref(internet_sid)):
                raise ctypes.WinError(ctypes.get_last_error())
            capability = SidAndAttributes(internet_sid, 4)  # SE_GROUP_ENABLED
            caps.Capabilities = ctypes.addressof(capability)
            caps.CapabilityCount = 1
        inherited = (wt.HANDLE*3)(in_r,out_w,err_w)
        if not k.UpdateProcThreadAttribute(attrs,0,ctypes.c_void_p(0x20009),ctypes.byref(caps),ctypes.sizeof(caps),None,None):
            raise OSError("AppContainer capability setup failed")
        if not k.UpdateProcThreadAttribute(attrs,0,ctypes.c_void_p(0x20002),ctypes.byref(inherited),ctypes.sizeof(inherited),None,None):
            raise OSError("private handle list setup failed")
        # Install kill-on-close before Windows can create a suspended child.
        family = Family(memory=memory, processes=processes, name=profile.get("jobName"))
        jobs = (wt.HANDLE * 1)(family.handle)
        if not k.UpdateProcThreadAttribute(attrs, 0, ctypes.c_void_p(0x2000D), jobs, ctypes.sizeof(jobs), None, None):
            raise OSError("AppContainer job setup failed")
        si = C._STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(si)
        si.StartupInfo.dwFlags = C.STARTF_USESTDHANDLES
        si.StartupInfo.hStdInput,si.StartupInfo.hStdOutput,si.StartupInfo.hStdError = in_r,out_w,out_w if merge else err_w
        si.lpAttributeList = ctypes.cast(attrs,ctypes.c_void_p)
        pi = C._PROCESS_INFORMATION()
        block = C._env_block(env)
        from .safe_executor import command_line
        command = ctypes.create_unicode_buffer(command_line(argv))
        if not k.CreateProcessW(None,command,None,None,True,0x08000000|0x00080000|0x00000400|0x00000004,block,str(cwd),ctypes.byref(si),ctypes.byref(pi)):
            raise OSError(ctypes.get_last_error(),"isolated CreateProcess failed")
        proc = C.Proc(pi.hProcess,pi.hThread,int(pi.dwProcessId),None,None,[],[],{},[])
        proc._argv = argv
        proc._handle = pi.hProcess
        out = io.open(msvcrt.open_osfhandle(out_r.value,os.O_RDONLY|os.O_BINARY),"rb")
        handles.remove(out_r)
        err = io.open(msvcrt.open_osfhandle(err_r.value,os.O_RDONLY|os.O_BINARY),"rb")
        handles.remove(err_r)
        stdin = None
        if duplex:
            stdin = io.open(msvcrt.open_osfhandle(in_w.value,os.O_WRONLY|os.O_BINARY),'wb',buffering=0)
            handles.remove(in_w)
        return (proc,family,out,err,stdin) if duplex else (proc,family,out,err)
    except BaseException:
        if family and family.handle:
            try:
                family.close()
            except OSError:
                # Closing our last handle still requests kill-on-close.
                k.CloseHandle(family.handle)
                family.handle = None
        if proc:
            try:
                proc.kill()
            finally:
                proc._k_close()
        raise
    finally:
        for handle in handles:
            k.CloseHandle(handle)
        if initialized:
            k.DeleteProcThreadAttributeList(attrs)
        if internet_sid:
            k.LocalFree.argtypes = [ctypes.c_void_p]
            k.LocalFree(internet_sid)
