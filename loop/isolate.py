#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""isolate.py —— 执行层的**降权**：给子进程套一个受限的 Job Object。

它是什么
    Windows 有个内核对象叫 Job Object，能把一个（或一棵）进程装进去，然后限制它们：
    **不许访问默认桌面、不许读写剪贴板、不许拿别的进程的句柄、不许改显示设置**，
    再加两个保命的：进程数上限、内存上限，以及"这个 job 句柄一关，里面全杀"。

为什么这一层比"跑之前停住"更硬
    决策层（`sandbox_rules`）拦的是"agent 主动去做什么"——它靠**读代码**判断，
    换个写法就能绕（清单里那些写法都是我在这一行里能想到的，想不到的就漏）。
    Job Object 拦的是"进程能碰到什么"——这不看意图，看**内核给不给**。
    最典型的一条：**模拟点击付款界面**。那个动作的本质是"拿到桌面上的窗口、往里发消息"，
    而 `JOB_OBJECT_UILIMIT_DESKTOP` 一开，这个子进程连默认桌面都访问不到，
    它想点也没有那只手 —— 不用猜它想干什么。

★★ 一条实测出来的硬要求：**必须"先套后跑"**
    第一版是"Popen 起来之后立刻 Assign"——看着没问题，实测**一点作用都没有**：
    子进程照样 `EnumWindows` 拿到 16 个别人的窗口、照样开剪贴板。
    原因是窗口站（window station）在进程**创建那一刻**就继承了父进程的，
    之后再把它扔进 job，已经不re-parent 了。
    ⇒ 现在改成 `CREATE_SUSPENDED` 建进程 → 套 job → 恢复线程。同一份探针脚本：
      不隔离 `foreign windows: 16`，隔离后 **`foreign windows: 0`**。
    这条如果只读文档不实测，会写成"已经挡住了模拟点击"—— 而它什么都没挡。

★ 能挡什么、挡不住什么（实测口径）
    能挡：**枚举/操作别的进程的窗口**（= 模拟点击的原料，实测 16 → 0）；
          进程树无限膨胀（`ActiveProcessLimit`）；单进程吃光内存（`ProcessMemoryLimit`）；
          父进程一死留一堆孤儿（`KILL_ON_JOB_CLOSE`）。
    挡不住：**联网**（Job Object 没有网络维度；Windows 上要 WFP 过滤，那需要管理员和驱动）；
            读文件（读几乎不受限；"读凭据"靠路径规则挡）；
            **已经起来的进程**（套晚了就是没套）。
    所以这一层的准确说法是「**窗口与资源隔离**」，不是"沙箱"。写成沙箱就是吹。

★ 为什么默认开、失败就降级
    它不改命令的语义（stdout、退出码、cwd 全照旧），所以开着对正常活没有影响。
    但万一某台机器上 Assign 失败（权限、嵌套 job），**不能因此让命令跑不了** ——
    那正是"把新 agent 锁死"的典型形态。⇒ 失败记一条、继续跑，并把原因写进日志。
    真遇到"某条命令因为 UI 限制跑不起来"，用 `HARNESS_NO_ISOLATE=1` 关掉整层。

★ 为什么没做低完整性令牌（想清楚的，不是漏的）
    低完整性（Low IL）只限制**写**：写不了用户目录/注册表。而用户最担心的两件事是
    "把信息发出去"（读 + 联网）和"用支付软件付款"（UI + 联网）——
    低完整性对这两件**一件都挡不住**，却会让所有要写文件的命令（pytest、accept、git）直接失败。
    代价和收益完全不成比例。真沙箱要等打包后走 AppContainer 或独立用户账户。
"""
# 主要引用方包括 `workbench.py` 和 `safe_executor.py`。
from __future__ import annotations

import os
import sys

_LIB = None
_JOB = None            # 本进程持有的那个 job 句柄（关掉它 = 里面全杀）
_STATUS = {"tried": False, "ok": False, "why": "", "flags": 0, "ui": 0}

# ── 常量（照 winnt.h，值不能猜）──
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_UILIMIT_HANDLES = 0x0001
JOB_OBJECT_UILIMIT_READCLIPBOARD = 0x0002
JOB_OBJECT_UILIMIT_WRITECLIPBOARD = 0x0004
JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS = 0x0008
JOB_OBJECT_UILIMIT_DISPLAYSETTINGS = 0x0010
JOB_OBJECT_UILIMIT_GLOBALATOMS = 0x0020
JOB_OBJECT_UILIMIT_DESKTOP = 0x0040
JOB_OBJECT_UILIMIT_EXITWINDOWS = 0x0080

UI_ALL = (JOB_OBJECT_UILIMIT_HANDLES | JOB_OBJECT_UILIMIT_READCLIPBOARD
          | JOB_OBJECT_UILIMIT_WRITECLIPBOARD
          | JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS
          | JOB_OBJECT_UILIMIT_DISPLAYSETTINGS
          | JOB_OBJECT_UILIMIT_GLOBALATOMS
          | JOB_OBJECT_UILIMIT_DESKTOP
          | JOB_OBJECT_UILIMIT_EXITWINDOWS)

JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9

PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
CREATE_SUSPENDED = 0x00000004
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002


def _cfg() -> dict:
    """读配置（`harness/charter.json` 的 `sandbox.isolate`）。读不到就用默认。"""
    try:
        from . import settings as SET
        return (SET.load().get("sandbox") or {}).get("isolate") or {}
    except Exception:
        return {}


def enabled() -> bool:
    if os.environ.get("HARNESS_NO_ISOLATE"):
        return False
    if os.name != "nt":
        return False
    c = _cfg()
    return bool(c.get("enabled", True))


def _lib():
    global _LIB
    if _LIB is not None:
        return _LIB
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IOC(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong)]

    class EXT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC),
                    ("IoInfo", IOC),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class UI(ctypes.Structure):
        _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD]
    k32.QueryInformationJobObject.restype = wintypes.BOOL
    k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.c_void_p]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _LIB = {"k32": k32, "EXT": EXT, "UI": UI}
    return _LIB


def _job():
    """本进程那个 job（懒建一次）。建不成返回 None。"""
    global _JOB
    if _JOB:
        return _JOB
    if not enabled():
        return None
    try:
        import ctypes
        L = _lib()
        k32 = L["k32"]
        h = k32.CreateJobObjectW(None, None)
        if not h:
            _STATUS.update(tried=True, ok=False,
                           why="CreateJobObject 失败（err=%d）" % ctypes.get_last_error())
            return None
        c = _cfg()
        ui = UI_ALL if c.get("ui_limits", True) else 0
        u = L["UI"](UIRestrictionsClass=ui)
        if ui and not k32.SetInformationJobObject(
                h, JobObjectBasicUIRestrictions, ctypes.byref(u), ctypes.sizeof(u)):
            _STATUS.update(tried=True, ok=False,
                           why="设 UI 限制失败（err=%d）" % ctypes.get_last_error())
            k32.CloseHandle(h)
            return None
        e = L["EXT"]()
        e.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_PROCESS_MEMORY)
        # ★ 环境变量能覆盖（只给探针用）：要验"上限真的生效"，就得设一个很小的值。
        _procs = int(os.environ.get("HARNESS_ISO_PROCS") or c.get("max_procs") or 64)
        _mem = int(os.environ.get("HARNESS_ISO_MEM_MB") or c.get("mem_mb") or 3072)
        e.BasicLimitInformation.ActiveProcessLimit = max(1, _procs)
        e.ProcessMemoryLimit = max(0, _mem) * 1024 * 1024
        if not k32.SetInformationJobObject(h, JobObjectExtendedLimitInformation,
                                           ctypes.byref(e), ctypes.sizeof(e)):
            _STATUS.update(tried=True, ok=False,
                           why="设资源上限失败（err=%d）" % ctypes.get_last_error())
            k32.CloseHandle(h)
            return None
        _JOB = h
        _STATUS.update(tried=True, ok=True, why="", flags=ui)
        return h
    except Exception as ex:                     # 探测失败也不该让命令跑不了
        _STATUS.update(tried=True, ok=False, why="%s: %s" % (type(ex).__name__, ex))
        return None


def apply(proc, tag: str = "") -> dict:
    """把**已经起来**的进程装进 job。返回 {"ok", "why"}；永不抛。

    ★ 为什么是"起来之后"而不是"起来之前"：Python 的 `subprocess` 没法在
      CreateProcess 那一刻把 job 交进去（那要自己调 CreateProcessAsUser / STARTUPINFOEX，
      几百行、错一处所有命令都跑不了）。而进程起来到 Assign 之间只有几毫秒，
      这几毫秒里它还没有"去点桌面"的能力（要加载 UI 库、找窗口）。够用。
    """
    if not enabled():
        return {"ok": False, "why": "没开（HARNESS_NO_ISOLATE 或平台不是 Windows）"}
    h = _job()
    if not h:
        return {"ok": False, "why": _STATUS.get("why") or "job 建不起来"}
    pid = getattr(proc, "pid", None)
    if not pid:
        return {"ok": False, "why": "拿不到 pid"}
    try:
        import ctypes
        k32 = _lib()["k32"]
        hp = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not hp:
            return {"ok": False,
                    "why": "OpenProcess 失败（err=%d）—— 进程可能已经退出"
                           % ctypes.get_last_error()}
        try:
            ok = bool(k32.AssignProcessToJobObject(h, hp))
            err = 0 if ok else ctypes.get_last_error()
        finally:
            k32.CloseHandle(hp)
        if not ok:
            return {"ok": False, "why": "AssignProcessToJobObject 失败（err=%d）" % err}
        return {"ok": True, "why": "", "ui": _STATUS.get("flags", 0), "tag": tag}
    except Exception as ex:
        return {"ok": False, "why": "%s: %s" % (type(ex).__name__, ex)}


def resume(pid: int) -> int:
    """把一个 `CREATE_SUSPENDED` 起来的进程恢复运行。返回恢复了几条线程。

    ★ 为什么要自己枚举线程：`subprocess` 只给 pid，不给主线程句柄，
      而 `ResumeThread` 要的是线程句柄。toolhelp 快照是唯一不装依赖的办法。
    ★ 恢复 0 条 = 这个进程会永远挂着 ⇒ 调用方必须把这种情况当成失败处理，
      否则现象是"命令发出去了，然后什么都不发生"，最难查。
    """
    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        class TE(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ThreadID", wintypes.DWORD),
                        ("th32OwnerProcessID", wintypes.DWORD),
                        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if not snap or int(snap) == -1:
            return 0
        te = TE()
        te.dwSize = ctypes.sizeof(te)
        n = 0
        ok = k32.Thread32First(snap, ctypes.byref(te))
        while ok:
            if te.th32OwnerProcessID == int(pid):
                h = k32.OpenThread(THREAD_SUSPEND_RESUME, False, te.th32ThreadID)
                if h:
                    k32.ResumeThread(h)
                    k32.CloseHandle(h)
                    n += 1
            ok = k32.Thread32Next(snap, ctypes.byref(te))
        k32.CloseHandle(snap)
        return n
    except Exception:
        return 0


def _exe_path(argv) -> str:
    """这条命令真正要跑的可执行文件在哪（绝对路径）。"""
    import shutil
    if not argv:
        return ""
    head = str(argv[0])
    got = shutil.which(head) or head
    try:
        return os.path.abspath(got)
    except Exception:
        return ""


def _container_eligible(cmd, kw: dict) -> bool:
    """这条命令适不适合进 AppContainer。

    ★ 只接**一次性命令**：stdin 是空的、stdout/stderr 走管道。
      MCP server 那种要一直说话的进程不行 —— 容器的 Proc 只有一根输入管子，
      写完就没有第二句，硬塞会把 server 弄成哑巴。
    ★ **python 类命令不进**（2026-10-27 实测）：解释器装在一个很深的用户目录下，
      容器要走到它的 DLL，路径上每一级父目录都得有遍历权限；而在父目录上改 ACL
      会让系统把整棵子树重算一遍（实测 `.workbuddy/binaries` 花了 43 秒，
      再上一级直接跑不完）。这个代价不能挂在每台机器的日常执行上。
      ⇒ 让它们退回 job-only，并**明说**这一条 —— 不说就等于吹。
      要试的话设 `HARNESS_CONTAIN_PY=1`（等解释器随包放到浅路径再默认开）。
    """
    try:
        import subprocess
        import sys
        from . import contain as CN
    except Exception:
        return False
    if not (CN.supported() and CN.enabled()):
        return False
    if kw.get("shell"):
        return False
    if kw.get("stdin") != subprocess.DEVNULL:
        return False
    if kw.get("stdout") != subprocess.PIPE or kw.get("stderr") != subprocess.PIPE:
        return False
    if not os.environ.get("HARNESS_CONTAIN_PY"):
        argv = [cmd] if isinstance(cmd, str) else list(cmd or [])
        exe = _exe_path(argv).lower()
        pre = os.path.abspath(sys.base_prefix).lower()
        if exe and exe.startswith(pre):
            _STATUS["py_skipped"] = int(_STATUS.get("py_skipped") or 0) + 1
            return False
    return True


def popen(cmd, **kw):
    """`subprocess.Popen` 的安全版：**先把进程装进 job，再让它跑第一行**。

    ★ 顺序是这一层的全部：见顶上那段实测（晚套 = 没套）。
    ★ 任何一步失败都**降级重来**（杀掉挂着的那个，按原样再起一个）：
      隔离是加固，不是前提。为了加固而让命令跑不了，是本末倒置 ——
      而且那种失败会表现成"命令发出去了，什么反应都没有"，比报错难查十倍。
    ★ 容器优先（2026-10-27 接进来的）：够条件的命令进 AppContainer，
      这才管到「读哪个文件」；Job Object 只管窗口和资源。
      容器这条路任何一步失败，都退回下面那条 job-only 的老路，并记账。
    """
    import subprocess
    required = bool(_cfg().get("require_container", False))
    if required and (not enabled() or not _container_eligible(cmd, kw)):
        raise PermissionError("此命令不满足必需的容器隔离条件，未执行")
    if not enabled():
        return subprocess.Popen(cmd, **kw)
    if _container_eligible(cmd, kw):
        from . import contain as CN
        argv = [cmd] if isinstance(cmd, str) else list(cmd)
        pr = CN.spawn(argv, cwd=kw.get("cwd"), env=kw.get("env"))
        if pr is not None:
            # ★ 接口不兼容就先退回（2026-10-29）：要检查的是**类上有没有** stdout，
            #   不能 `hasattr(pr, "stdout")` —— 那是 property，会触发 communicate
            #   一直阻塞到进程退出（探针会挂死）。
            if not hasattr(type(pr), "stdout"):
                try:
                    pr.kill()
                    pr.wait(timeout=3)
                except Exception:
                    pass
                _STATUS["container_falls"] = int(_STATUS.get("container_falls") or 0) + 1
                _STATUS["container_why"] = "容器 Proc 不是 Popen 接口（缺 stdout）"
                print("⚠ 容器档没走成（%s），这一条退回 Job Object"
                      % _STATUS["container_why"], flush=True)
                pr = None
        if pr is not None:
            # ★★ 二选一（2026-10-29）：AppContainer 和 Job Object 在 Windows 上**互斥** ——
            #    进了容器的进程再 AssignProcessToJobObject 会回 err=50（实测，
            #    `runs/_probe_contain_exec.out` 3 条 FAIL 就是这个）。
            #    原来"先套 job 再进容器"的顺序，让容器档**一次都没生效过**：
            #    每次 assign 失败 → 杀容器 → 退回 job-only，而外面看不出来。
            #    两者管的事不一样：容器管「能读哪个文件」（真正要紧的那道），
            #    Job 只管窗口和资源上限。所以先保容器：能进容器就只进容器。
            n = 0
            try:
                n = int(pr.resume() or 0)
            except Exception:
                n = 0
            if n:
                _STATUS["container"] = int(_STATUS.get("container") or 0) + 1
                _STATUS["container_why"] = "已进容器（未套 Job：两者互斥）"
                return pr
            try:
                pr.kill()
                pr.wait(timeout=3)
            except Exception:
                pass
            _STATUS["container_falls"] = int(_STATUS.get("container_falls") or 0) + 1
            _STATUS["container_why"] = "容器起来了，但线程放行失败"
            # ★ 这条必须**打出来**：只在 _STATUS 里记一笔没人看，
            #   而"这一轮其实没在容器里"是安全问题，不该静默降级。
            print("⚠ 容器档没走成（%s），这一条退回 Job Object"
                  % _STATUS["container_why"], flush=True)
    if required:
        raise PermissionError("容器隔离启动失败；必需隔离模式不允许降级执行")
    flags = int(kw.pop("creationflags", 0) or 0)
    try:
        p = subprocess.Popen(cmd, creationflags=flags | CREATE_SUSPENDED, **kw)
    except Exception:
        return subprocess.Popen(cmd, creationflags=flags, **kw)
    a = apply(p)
    n = resume(p.pid) if a.get("ok") else 0
    if a.get("ok") and n:
        return p
    # 降级：挂着的那个先杀干净，再按原样起（不带 CREATE_SUSPENDED）
    try:
        p.kill()
        p.wait(timeout=3)
    except Exception:
        pass
    _STATUS.update(tried=True, ok=False,
                   why=(a.get("why") or "恢复线程失败（进程会一直挂着）"))
    # ★ 降级次数要记账（2026-10-26）：不记的话，"这一轮其实没隔离"从外面看不出来 ——
    #   而最典型的降级场景是**嵌套 job**（宿主把我们也放进了某个 job 里，
    #   Windows 下 Assign 会失败）。那一刻起隔离就静默失效了。
    _STATUS["falls"] = int(_STATUS.get("falls") or 0) + 1
    return subprocess.Popen(cmd, creationflags=flags, **kw)


def warm() -> dict:
    """启动时就把 job 建好（这样日志里那句"隔离生效了吗"是**现在**的答案，
    而不是"等第一条命令起来时才知道"）。失败不抛。"""
    if not enabled():
        return {"ok": False, "why": describe()}
    h = _job()
    return query() if h else {"ok": False, "why": _STATUS.get("why") or "建不起来"}


def query() -> dict:
    """读回 job 上真正的限制（探针用：**别信自己设过什么，要问内核**）。"""
    h = _JOB
    if not h:
        return {"ok": False, "why": _STATUS.get("why") or "还没建 job"}
    try:
        import ctypes
        L = _lib()
        k32 = L["k32"]
        u = L["UI"]()
        got = ctypes.c_ulong(0)
        ok1 = k32.QueryInformationJobObject(h, JobObjectBasicUIRestrictions,
                                            ctypes.byref(u), ctypes.sizeof(u),
                                            ctypes.byref(got))
        e = L["EXT"]()
        ok2 = k32.QueryInformationJobObject(h, JobObjectExtendedLimitInformation,
                                            ctypes.byref(e), ctypes.sizeof(e),
                                            ctypes.byref(got))
        if not (ok1 and ok2):
            return {"ok": False,
                    "why": "QueryInformationJobObject 失败（err=%d）"
                           % ctypes.get_last_error()}
        return {"ok": True,
                "ui": int(u.UIRestrictionsClass),
                "flags": int(e.BasicLimitInformation.LimitFlags),
                "max_procs": int(e.BasicLimitInformation.ActiveProcessLimit),
                "mem_mb": int(e.ProcessMemoryLimit // (1024 * 1024))}
    except Exception as ex:
        return {"ok": False, "why": "%s: %s" % (type(ex).__name__, ex)}


def describe() -> str:
    """一句人话（进启动日志）。"""
    if os.name != "nt":
        return "执行层隔离：这个平台没有 Job Object（Linux 上等价物是 cgroup/systemd-run，没接）"
    if not enabled():
        return "执行层隔离：**关掉了**（HARNESS_NO_ISOLATE 或配置 enabled=false）"
    _f = int(_STATUS.get("falls") or 0)
    if _f:
        return ("执行层隔离：⚠ 有 %d 次**没套上**（%s）—— 从那一刻起等于没隔离。"
                "常见原因是宿主进程自己也在一个 job 里（嵌套 job 下 Assign 会失败）。"
                % (_f, _STATUS.get("why") or "原因不明"))
    q = query()
    if not q.get("ok"):
        # 还没建 job 也算正常（第一次跑命令时才建）—— 只报能力探测结果
        h = _job()
        q = query() if h else {"ok": False, "why": "还没起过子进程"}
        if not q.get("ok"):
            return "执行层隔离：等第一条命令起来时才知道（%s）" % q.get("why", "")
    # ★ 措辞只写**实测过**的：UI 限制真正拦住的是"枚举/操作别人的窗口"，
    #   不是"桌面完全不可访问"（后者我在这台 Win10 上没验出来，就别写成那样）。
    return ("执行层隔离：Job Object 已生效 —— UI 0x%02x（实测：子进程枚举不到别人的窗口）、"
            "最多 %d 个进程、单进程内存 %d MB"
            % (q.get("ui", 0), q.get("max_procs", 0), q.get("mem_mb", 0)))

