"""Windows evidence used by the sandbox resource reclaimer."""

import ctypes
from contextlib import contextmanager
from ctypes import wintypes as W

_MAPPINGS = (r"Software\Classes\Local Settings\Software\Microsoft\Windows"
             r"\CurrentVersion\AppContainer\Mappings")


def _local_free(value):
    fn = ctypes.WinDLL("kernel32").LocalFree
    fn.argtypes = [ctypes.c_void_p]
    fn.restype = ctypes.c_void_p
    fn(value)


def container_sid(name):
    api = ctypes.WinDLL("userenv", use_last_error=True)
    derive = api.DeriveAppContainerSidFromAppContainerName
    derive.argtypes = [W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    derive.restype = ctypes.c_long
    sid = ctypes.c_void_p()
    if derive(name, ctypes.byref(sid)) or not sid:
        raise OSError("Could not derive the AppContainer SID")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    text = W.LPWSTR()
    try:
        convert = advapi.ConvertSidToStringSidW
        convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(W.LPWSTR)]
        convert.restype = W.BOOL
        if not convert(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        return text.value
    finally:
        free = advapi.FreeSid
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(sid)
        if text:
            _local_free(text)


def container_present(name, sid):
    """Confirm the registry maps the exact derived SID to this moniker."""
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MAPPINGS + "\\" + sid)
    except FileNotFoundError:
        return False
    with key:
        moniker, kind = winreg.QueryValueEx(key, "Moniker")
        if kind != winreg.REG_SZ or moniker.casefold() != name.casefold():
            raise OSError("AppContainer registration does not match the recorded moniker")
        return True


def acl_has_container(path, sid):
    """Legacy journals are bound to their directories by an explicit allow ACE."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    expected = ctypes.c_void_p()
    convert = advapi.ConvertStringSidToSidW
    convert.argtypes = [W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    convert.restype = W.BOOL
    if not convert(sid, ctypes.byref(expected)):
        raise ctypes.WinError(ctypes.get_last_error())
    desc, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        get = advapi.GetNamedSecurityInfoW
        pointer = ctypes.POINTER(ctypes.c_void_p)
        get.argtypes = [W.LPWSTR, W.DWORD, W.DWORD, pointer, pointer, pointer, pointer, pointer]
        get.restype = W.DWORD
        status = get(str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(desc))
        if status:
            raise ctypes.WinError(status)
        if not dacl:
            return False
        # ACL and ACCESS_ALLOWED_ACE headers are fixed-width Win32 structures.
        count = ctypes.c_ushort.from_address(dacl.value + 4).value
        get_ace = advapi.GetAce
        get_ace.argtypes = [ctypes.c_void_p, W.DWORD, pointer]
        get_ace.restype = W.BOOL
        equal = advapi.EqualSid
        equal.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        equal.restype = W.BOOL
        ace = ctypes.c_void_p()
        for index in range(count):
            if not get_ace(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.c_ubyte.from_address(ace.value).value == 0 and equal(ace.value + 8, expected):
                return True
        return False
    finally:
        if desc:
            _local_free(desc)
        _local_free(expected)


def task_registrations(limit=4096):
    """A bounded, read-only look at registrations for legacy diagnostics."""
    import winreg
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MAPPINGS)
    except FileNotFoundError:
        return [], False
    found = []
    with root:
        count, _, _ = winreg.QueryInfoKey(root)
        for index in range(min(count, limit)):
            try:
                sid = winreg.EnumKey(root, index)
                with winreg.OpenKey(root, sid) as item:
                    name, kind = winreg.QueryValueEx(item, "Moniker")
                if kind == winreg.REG_SZ and name.casefold().startswith("harness.task."):
                    found.append((name, sid))
            except OSError:
                # The registration may disappear during a sweep.
                continue
    return found, count > limit


@contextmanager
def mutex(key, timeout):
    """Sleep-free on contention by default; a killed owner leaves no lock file."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = [ctypes.c_void_p, W.BOOL, W.LPCWSTR]
    create.restype = W.HANDLE
    wait = kernel.WaitForSingleObject
    wait.argtypes = [W.HANDLE, W.DWORD]
    wait.restype = W.DWORD
    release = kernel.ReleaseMutex
    release.argtypes = [W.HANDLE]
    release.restype = W.BOOL
    close = kernel.CloseHandle
    close.argtypes = [W.HANDLE]
    close.restype = W.BOOL
    handle = create(None, False, "Global\\Modot.SandboxLifecycle." + key)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        status = wait(handle, int(timeout * 1000))
        if status == 0x102:  # WAIT_TIMEOUT
            raise TimeoutError("Sandbox ownership is busy")
        if status not in (0, 0x80):  # WAIT_OBJECT_0, WAIT_ABANDONED
            raise ctypes.WinError(ctypes.get_last_error())
        # Callers always reread and validate the atomic record after acquiring.
        try:
            yield
        finally:
            release(handle)
    finally:
        close(handle)
