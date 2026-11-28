"""Detect the system Dokan 2 driver and launch only the bundled, digest-pinned installer."""
import ctypes
from ctypes import wintypes
from enum import IntEnum
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


class Status(IntEnum):
    READY = 0
    MISSING = 1
    CANCELLED = 2
    RESTART = 3
    FAILED = 4
    BUSY = 5
    INVALID = 6
    NEWER = 7


def status():
    try:
        lib = ctypes.WinDLL("dokan2.dll", winmode=0x800)
        lib.DokanInit.restype = lib.DokanShutdown.restype = None
        lib.DokanVersion.restype = lib.DokanDriverVersion.restype = wintypes.ULONG
        lib.DokanInit()
        try:
            version, kernel = lib.DokanVersion(), lib.DokanDriverVersion()
            # Dokan 2.3.1 requires an exact kernel protocol match (sys/public.h).
            if (version == 231 and kernel == 0x190) or (version > 231 and kernel >= 0x190):
                return Status.READY
            return Status.NEWER if version > 231 else Status.MISSING
        finally:
            lib.DokanShutdown()
    except (OSError, AttributeError):
        return Status.MISSING


def system_installer():
    directory = ctypes.create_unicode_buffer(32768)
    length = ctypes.WinDLL("kernel32", winmode=0x800).GetSystemDirectoryW(directory, len(directory))
    if not 0 < length < len(directory):
        raise OSError("Cannot locate Windows Installer")
    return str(Path(directory.value) / "msiexec.exe")


def locked_read(path):
    # Keep the verified file open without write or delete sharing until msiexec exits.
    import msvcrt
    handle_api = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x800)
    create = handle_api.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb")


def install():
    initial = status()
    if initial != Status.MISSING:
        return initial
    directory = Path(__file__).resolve().parent
    pin = json.loads((directory / "windows-components.json").read_text(encoding="utf-8"))["dokan"]
    msi = directory / "Dokan_x64.msi"
    with locked_read(msi) as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != pin["sha256"]:
            return Status.INVALID
        try:
            # Windows Installer owns elevation and consent. Never run the application elevated.
            result = subprocess.run([system_installer(), "/i", str(msi), "/norestart"],
                                    cwd=directory, creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as error:
            return Status.CANCELLED if getattr(error, "winerror", None) == 1223 else Status.FAILED
    if result.returncode == 0:
        return Status.READY if status() == Status.READY else Status.FAILED
    return {1602: Status.CANCELLED, 1223: Status.CANCELLED, 1618: Status.BUSY,
            1641: Status.RESTART, 3010: Status.RESTART}.get(result.returncode, Status.FAILED)


if __name__ == "__main__":
    try:
        operation = {"status": status, "install": install}[sys.argv[1]]
        sys.exit(operation() if len(sys.argv) == 2 else Status.FAILED)
    except (OSError, ValueError, KeyError, IndexError):
        sys.exit(Status.FAILED)
