"""Process identity and recovery shared by API and command-line entry points."""

# 主要引用方包括 `workbench.py` 和 `artifacts.py`。

from __future__ import annotations
import json
import os
import time
import uuid
from pathlib import Path
from .filelock import atomic_json, FileLock, read_text
from .lease import Lease


def identity(pid):
    """Return a creation identity, None for dead, 'unknown' if unverifiable."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes as W

            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
            k.OpenProcess.restype = W.HANDLE
            k.CloseHandle.argtypes = [W.HANDLE]
            k.GetProcessTimes.argtypes = [W.HANDLE] + [
                ctypes.POINTER(ctypes.c_ulonglong)
            ] * 4
            h = k.OpenProcess(0x1000, False, int(pid))
            if not h:
                return None if ctypes.get_last_error() == 87 else "unknown"
            try:
                vals = [ctypes.c_ulonglong() for _ in range(4)]
                if not k.GetProcessTimes(h, *(ctypes.byref(v) for v in vals)):
                    return "unknown"
                # Exit time is nonzero for a terminated process retained by a handle.
                return None if vals[1].value else str(vals[0].value)
            finally:
                k.CloseHandle(h)
        p = Path("/proc") / str(int(pid)) / "stat"
        if p.exists():
            tail = p.read_text().rsplit(")", 1)[1].split()
            return None if tail[0] == "Z" else tail[19]
        os.kill(int(pid), 0)
        return "unknown"
    except ProcessLookupError:
        return None
    except (OSError, ValueError):
        return "unknown"


def read(directory):
    try:
        return json.loads(
            read_text(Path(directory) / "runtime.json")
        )
    except (OSError, ValueError):
        return {}


def register(directory, pid, phase="running", policy=None):
    with FileLock(Path(directory) / "runtime.lock"):
        creation = identity(pid)
        old = read(directory)
        same = (
            old.get("pid") == int(pid)
            and old.get("identity") == creation
            and creation not in (None, "unknown")
        )
        # The child may already have advanced or finished before its launcher
        # records Popen. A late parent acknowledgement must not reset that state.
        if int(pid) != os.getpid() and old.get("pid") == int(pid):
            if same or (
                creation is None
                and old.get("state")
                in ("completed", "failed", "interrupted", "waiting_approval")
            ):
                return old
        rec = {
            "run_id": old["run_id"] if same else uuid.uuid4().hex,
            "pid": int(pid),
            "identity": creation,
            "phase": phase,
            "policy": policy,
            "started_at": old["started_at"] if same else time.time(),
            "state": "running",
        }
        atomic_json(Path(directory) / "runtime.json", rec)
    return rec


# PID 会复用；只有当前进程及其创建身份都匹配，才允许修改这份运行状态。
def update_owned(directory, state=None, phase=None, outcome=None):
    with FileLock(Path(directory) / "runtime.lock"):
        rec = read(directory)
        if rec.get("pid") != os.getpid() or rec.get("identity") != identity(
            os.getpid()
        ):
            return
        if state:
            rec["state"] = state
        if phase:
            rec["phase"] = phase
            if phase == "finished":
                rec["finished_at"] = time.time()
        if outcome:
            rec["outcome"] = outcome
        atomic_json(Path(directory) / "runtime.json", rec)


def status(directory):
    rec = read(directory)
    if rec.get("pid"):
        actual = identity(rec["pid"])
        if actual is not None and actual == rec.get("identity") and actual != "unknown":
            return dict(rec, active=True)
        # 无法核实身份时保守视为仍活跃且状态未知，不能据此释放写入归属或宣告执行结束。
        if actual == "unknown":
            return dict(rec, active=True, state="unknown")
    # 主运行记录未确认活跃时再查租约；记录中的终止状态与实际进程/租约存活分别判断。
    holder = Lease(directory, Path(directory).name).alive()
    if holder:
        return dict(rec, active=True, state="running", lease=holder)
    if rec.get("run_id") and rec.get("state") not in (
        "completed",
        "failed",
        "interrupted",
        "waiting_approval",
    ):
        return dict(rec, active=False, state="unknown")
    return dict(rec, active=False, state=rec.get("state", "idle"))


class RecoveredProcess:
    """A read/wait adapter. Never send signals to a bare recovered PID."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.pid = read(directory).get("pid")

    # 兼容返回值只表达已不活跃，0 不是恢复出的真实命令退出码；真实结果需查持久回执。
    def poll(self):
        return None if status(self.directory)["active"] else 0

    def wait(self, timeout=None):
        end = time.monotonic() + timeout if timeout is not None else None
        while self.poll() is None:
            if end is not None and time.monotonic() >= end:
                raise TimeoutError("process still active")
            time.sleep(0.1)
        return 0


def confirm_owned_exit(directory, run_id, process, cancelled=False):
    """Only an original Popen handle can attest an early pre-checkpoint exit."""
    import subprocess

    if not isinstance(process, subprocess.Popen) or process.poll() is None:
        return False
    with FileLock(Path(directory) / "runtime.lock"):
        row = read(directory)
        if row.get("run_id") != run_id or row.get("pid") != process.pid:
            return False
        row.update(
            state="interrupted" if cancelled else "failed",
            outcome="startup_cancelled" if cancelled else "startup_failed",
            exitCode=process.returncode,
            exitEvidence="owned_popen_handle",
            finished_at=time.time(),
        )
        atomic_json(Path(directory) / "runtime.json", row)
        return True
