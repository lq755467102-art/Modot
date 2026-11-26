#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""contain.py —— AppContainer 那一档：建得起来、起得来、**自己会体检**

它现在的定位（必须说清，别让人以为已经有 OS 级隔离了）
    这一版做**三件**事：
      ① 建一个 AppContainer profile（普通权限就行，不用管理员）；
      ② 用它把进程**放进容器**里起一次（`CreateProcessW` + SECURITY_CAPABILITIES）；
      ③ 体检：真起一个进程，报告"起没起来、退出码是多少"。
    **已经做的（2026-10-27）**：接进了 `isolate.popen` —— 一次性命令（stdin 是空的那种）
    走容器：`CreateProcessW` + 三根管道、先挂起套 Job 再放行、ACL 按目录授权并可收回。
    **没做的**：python 类命令不进容器（理由见下），MCP 那种要交互的长进程也不进。

为什么值得有这一层
    现在的隔离是 Job Object：管进程树、内存、窗口站。它**不管文件**。
    一个进程照样能读 `~/.ssh`、能改 `C:\\Windows`。AppContainer 补的正是这块：
    容器里的进程默认**读不到容器外的东西**，写入只能进它自己的存储。
    差的那一个级别就在这里。

接入时踩到的四件事（都是实测，别再踩）
    ① `subprocess.Popen` 没法带 `PROC_THREAD_ATTRIBUTES` ⇒ 管道得自己用
       `CreatePipe` + `STARTUPINFOEX` 搭，读端用 `PeekNamedPipe` 轮询（裸 `ReadFile`
       会被孙子进程挂住，实测 180 秒）。
    ② 管道要带一份允许 AppContainer 的安全描述符，否则子进程初始化 std 句柄时
       直接 0xC0000142 —— 看着像 DLL 坏了，其实是「这根管子不归它用」。
    ③ stdin 那根管子的继承标志要和输出管**反过来**，关掉就是无效句柄。
    ④ python.exe 起不来（0xC0000135）不是缺 DLL：解释器在很深的用户目录，
       容器要走过去，路径上每级父目录都要遍历权限；而父目录上加 ACL 会让系统
       重算整棵子树（`.workbuddy/binaries` 43 秒，再上一级跑不完）。
       ⇒ python 类命令**不进容器**，退回 job-only 并记账（`py_skipped`）。
       等解释器随包落到浅路径，用 `HARNESS_CONTAIN_PY=1` 再验。

所以这一版的价值是：**把"可行/不可行"变成一条可重跑的命令**（`bin/sandbox.py doctor`），
而不是一句"我们验证过可行"。等打包和内嵌解释器到位，接进执行就有地基了。
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes as w

PROFILE = "harness.agent"          # 固定名字：每次建同一个，不越建越多
_DESC = "harness 的子进程隔离档（AppContainer）"

# PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES
_ATTR_SECCAP = 0x00020009
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_NO_WINDOW = 0x08000000
_ERROR_ALREADY_EXISTS = 0x800700B7


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("AppContainerSid", ctypes.c_void_p),
                ("Capabilities", ctypes.c_void_p),
                ("CapabilityCount", w.DWORD),
                ("Reserved", w.DWORD)]


# ★ `ctypes.wintypes` 里**没有** STARTUPINFO（2026-10-26 实测：
#   它只有 GUID / HANDLE / RECT 这一批基础类型）。手写一份 —— 而且要
#   字段顺序、对齐、句柄类型都对，错一个字段 CreateProcess 就返回 87（参数错误），
#   而那个错误码不会告诉你是哪个字段。
class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR),
                ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                ("dwX", w.DWORD), ("dwY", w.DWORD),
                ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD),
                ("dwFillAttribute", w.DWORD), ("dwFlags", w.DWORD),
                ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE),
                ("hStdError", w.HANDLE)]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW),
                ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
                ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


def supported() -> bool:
    """这个平台有没有这个能力（只有 Windows 有）。"""
    return os.name == "nt"


def enabled() -> bool:
    """配置里有没有打开这一档（`sandbox.container`）。**默认关**：它是候选档，不是默认档。"""
    try:
        from . import settings as SET
        return bool(((SET.load().get("sandbox") or {}).get("container")))
    except Exception:
        return False


# ── ① 建 profile ────────────────────────────────────────────────────
def ensure_profile(name=None) -> dict:
    """建一个容器 profile（已经有了就取它的 SID）。返回 {ok, sid, created, why}。

    ★ 为什么固定名字：profile 会注册到系统里，用随机名会越建越多，
      而且删不干净 —— 那种"看不见的垃圾"正是这个仓库一直在防的东西。
    """
    if not supported():
        return {"ok": False, "why": "只有 Windows 支持 AppContainer"}
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    fn = userenv.CreateAppContainerProfile
    fn.restype = ctypes.c_long
    fn.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD,
                   ctypes.POINTER(ctypes.c_void_p)]
    sid = ctypes.c_void_p()
    profile_name = name or PROFILE
    hr = fn(profile_name, profile_name, _DESC, None, 0, ctypes.byref(sid)) & 0xFFFFFFFF
    created = False
    if hr == _ERROR_ALREADY_EXISTS:
        d = userenv.DeriveAppContainerSidFromAppContainerName
        d.restype = ctypes.c_long
        d.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        hr2 = d(profile_name, ctypes.byref(sid)) & 0xFFFFFFFF
        if hr2 != 0:
            return {"ok": False, "why": "取已有 profile 的 SID 失败（0x%08x）" % hr2}
    elif hr != 0:
        return {"ok": False, "why": "建 profile 失败（0x%08x）" % hr}
    else:
        created = True
    if not sid:
        return {"ok": False, "why": "拿到空 SID"}
    return {"ok": True, "sid": sid, "created": created, "name": profile_name}


def sid_str(sid) -> str:
    try:
        advapi = ctypes.WinDLL("advapi32")
        advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                  ctypes.POINTER(w.LPWSTR)]
        out = w.LPWSTR()
        if advapi.ConvertSidToStringSidW(sid, ctypes.byref(out)):
            return str(out.value)
    except Exception:
        pass
    return "?"


# ── ② 在容器里起一个进程 ──────────────────────────────────────────
def launch(exe: str, args: list[str] | None = None, cwd: str | None = None,
           timeout_ms: int = 20000) -> dict:
    """把 `exe` 放进容器里跑一次。返回 {ok, rc, why}。

    ★ 只用来**体检**：输出不接管（容器里的进程写不到我们的管道以外的东西），
      判断只看退出码。真要接进执行，得把三根管道重搭一遍（见模块头）。
    """
    if not supported():
        return {"ok": False, "rc": None, "why": "只有 Windows 支持"}
    p = ensure_profile()
    if not p.get("ok"):
        return {"ok": False, "rc": None, "why": p.get("why")}
    sid = p["sid"]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    kernel.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, w.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    kernel.CreateProcessW.argtypes = [
        w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD,
        ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(_STARTUPINFOEXW),
        ctypes.POINTER(_PROCESS_INFORMATION)]

    size = ctypes.c_size_t(0)
    kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    if size.value <= 0:
        return {"ok": False, "rc": None, "why": "拿不到属性表大小"}
    buf = ctypes.create_string_buffer(size.value)
    if not kernel.InitializeProcThreadAttributeList(buf, 1, 0, ctypes.byref(size)):
        return {"ok": False, "rc": None,
                "why": "初始化属性表失败（%d）" % ctypes.get_last_error()}
    caps = _SECURITY_CAPABILITIES(sid, None, 0, 0)
    if not kernel.UpdateProcThreadAttribute(
            buf, 0, ctypes.c_void_p(_ATTR_SECCAP), ctypes.byref(caps),
            ctypes.sizeof(caps), None, None):
        return {"ok": False, "rc": None,
                "why": "挂 SECURITY_CAPABILITIES 失败（%d）" % ctypes.get_last_error()}

    si = _STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
    si.lpAttributeList = ctypes.cast(buf, ctypes.c_void_p)
    pi = _PROCESS_INFORMATION()
    line = " ".join([exe] + ['"%s"' % a if " " in a else a for a in (args or [])])
    cmd = ctypes.create_unicode_buffer(line)
    ok = kernel.CreateProcessW(
        None, cmd, None, None, False,
        _EXTENDED_STARTUPINFO_PRESENT | _CREATE_NO_WINDOW,
        None, cwd, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        return {"ok": False, "rc": None,
                "why": "CreateProcess 失败（%d）" % ctypes.get_last_error()}
    try:
        kernel.WaitForSingleObject(pi.hProcess, timeout_ms)
        code = w.DWORD(0xFFFFFFFF)
        kernel.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        rc = int(code.value)
        # 转成有符号：0xC0000135 这种是大数，人看着以为是"正常退出码"
        if rc >= 0x80000000:
            rc -= 0x100000000
        return {"ok": rc == 0, "rc": rc, "why": "" if rc == 0 else _rc_why(rc)}
    finally:
        try:
            kernel.CloseHandle(pi.hProcess)
            kernel.CloseHandle(pi.hThread)
        except Exception:
            pass


def _sub(cmd: list) -> int:
    """在**容器外**跑同一条命令，用它的退出码做对照。"""
    import subprocess
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30).returncode
    except Exception:
        return -1


def _rc_why(rc: int) -> str:
    if rc == -1073741515:            # 0xC0000135
        return ("进程起不来：缺依赖 DLL（0xC0000135）。实测 python.exe 在容器里就是这样 —— "
                "解释器的依赖链加载不到，得等内嵌解释器那一轮")
    if rc == -1073741502:            # 0xC0000142
        return "DLL 初始化失败（0xC0000142）"
    if rc == -1073741790:            # 0xC0000022
        return "拒绝访问（0xC0000022）—— 容器里没有这个权限"
    return "退出码 %d" % rc


# ── ③ 体检 ─────────────────────────────────────────────────────────
def self_check() -> dict:
    """真的起一个进程，报告事实。**这是这一档唯一能自证的东西。**

    ★ 为什么用 `whoami.exe`：它是系统自带的、不依赖我们的工程、
      也不需要 cmd 解释器，能干净地回答"容器里到底能不能起进程"。
    """
    if not supported():
        return {"ok": False, "why": "只有 Windows 支持 AppContainer", "steps": []}
    steps = []
    p = ensure_profile()
    steps.append({"step": "建/取 profile", "ok": bool(p.get("ok")),
                  "why": p.get("why") or ("新建" if p.get("created") else "已有")})
    if not p.get("ok"):
        return {"ok": False, "why": p.get("why") or "profile 建不起来", "steps": steps}
    steps.append({"step": "SID", "ok": True, "why": sid_str(p["sid"])})
    r = launch("whoami.exe")
    steps.append({"step": "容器里起 whoami.exe", "ok": bool(r.get("ok")),
                  "why": r.get("why") or ("rc=%s" % r.get("rc"))})
    # ★ 真正证明"隔离在起作用"的一步：同一份 certutil，读**容器外**的文件。
    #   用 certutil 而不是 findstr：findstr 的"找不到"和"没权限"都是 rc=1，
    #   分不开；certutil 读不到文件会返回 0x80070002 这类明确的失败码。
    _tmp = None
    try:
        import tempfile
        fd, _tmp = tempfile.mkstemp(prefix="ac_read_", suffix=".bin")
        os.write(fd, b"hello")
        os.close(fd)
        _out = _sub(["certutil.exe", "-hashfile", _tmp, "MD5"])
        _in = launch("certutil.exe", ["-hashfile", _tmp, "MD5"])
        # 外面读得到、里面读不到 —— 这才叫隔离
        _rc_in = _in.get("rc")
        steps.append({"step": "容器内读**容器外**的文件（应当读不到）",
                      "ok": _out == 0 and _rc_in is not None and int(_rc_in) != 0,
                      "why": "外面 rc=%s ／ 里面 rc=%s" % (_out, _rc_in)})
    except Exception as e:
        steps.append({"step": "容器内读容器外的文件", "ok": False, "why": str(e)})
    finally:
        if _tmp:
            try:
                os.unlink(_tmp)
            except OSError:
                pass
    # 系统文件两边都该读得到（否则说明这一档"能用"是假的：连 DLL 都读不到，
    # 那谁进来都起不来 —— 这一步是上面那一步的对照）
    try:
        _out2 = _sub(["certutil.exe", "-hashfile", r"C://Windows//win.ini", "MD5"])
        _in2 = launch("certutil.exe", ["-hashfile", r"C://Windows//win.ini", "MD5"])
        # ★ 别写 `int(rc or 1)`：rc=0 会被 `or` 吃掉变成 1，
        #   于是"里面读到了"这种**正确**的情况被判成失败（2026-10-26 自己踩的）。
        _rc2 = _in2.get("rc")
        steps.append({"step": "容器内读系统文件（应当读得到，这是对照）",
                      "ok": _out2 == 0 and _rc2 is not None and int(_rc2) == 0,
                      "why": "外面 rc=%s ／ 里面 rc=%s" % (_out2, _rc2)})
    except Exception as e:
        steps.append({"step": "容器内读系统文件", "ok": False, "why": str(e)})

    # python 那一项单独看：它是"能不能把执行器放进去"的判据
    rp = launch(sys.executable, ["-c", "pass"])
    # ★ 这一步标 caveat：它回答的是"执行器能不能直接放进去"，
    #   **不是**"这一档能不能用"。混进总分的话，一个已知的环境限制
    #   会把整档判死，而人只会把开关关掉 —— 那时候连"能不能用"都没人知道了。
    steps.append({"step": "容器里起 python（caveat：关乎执行器，不关乎这一档可用）",
                  "ok": bool(rp.get("ok")), "caveat": True,
                  "why": rp.get("why") or ("rc=%s" % rp.get("rc"))})
    _ok = bool(r.get("ok")) and all(x.get("ok") for x in steps
                                     if not x.get("caveat"))
    return {"ok": _ok,
            "why": "" if _ok else next((x.get("why") for x in steps if not x.get("ok")),
                                       "原因不明"),
            "steps": steps,
            # ★ python 起不来 ≠ 这一档没价值：它只说明"执行器不能直接放进去"。
            #   把两件事分开报，人才不会因为一个红点把整档关掉。
            "python_ok": bool(rp.get("ok"))}


def describe() -> str:
    """一行（启动日志/诊断包）。"""
    if not supported():
        return "容器档：这个平台没有（只有 Windows 有 AppContainer）"
    if not enabled():
        return "容器档：没开（sandbox.container=false）；开了用 bin/sandbox.py doctor 体检"
    r = self_check()
    if r.get("ok"):
        return ("容器档：可用（容器里能起进程）｜执行器放不进去：%s"
                % ("能" if r.get("python_ok") else "不能（0xC0000135，缺依赖）"))
    return "容器档：开了但**不可用** —— %s" % (r.get("why") or "原因不明")


# ══════════════════════════════════════════════════════════════════
# ④ 带管道跑一次性命令（2026-10-27：这一段就是**接进执行层**的部分）
#
# 为什么放在这个文件：句柄、安全描述符、属性表的常量全在这儿，分开放会长出第二套。
#
# ★ 只接「一次性命令」：stdin 是空的那种（`run_check` / `run_script`）。
#   MCP server 那种要一直说话的进程**不进来** —— 它的 stdin 是活的，而这个 Proc
#   只有一根管子、写完就没有第二句。这条边界必须写清：不然以后会有人以为
#   「MCP 也被关在容器里了」，那是比没隔离更糟的误解。
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
WAIT_TIMEOUT = 0x00000102
CREATE_UNICODE_ENVIRONMENT = 0x00000400

_SD = None
_k32 = None
_dll_lock = None


def _k():
    """kernel32 单例（顺手把要用的原型都声明好 —— 漏一个 argtypes，
    在 64 位下会把指针截成 32 位，症状是随机的“参数错误”）。"""
    global _k32
    if _k32 is not None:
        return _k32
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreatePipe.argtypes = [ctypes.POINTER(w.HANDLE), ctypes.POINTER(w.HANDLE),
                             ctypes.c_void_p, w.DWORD]
    k.SetHandleInformation.argtypes = [w.HANDLE, w.DWORD, w.DWORD]
    k.PeekNamedPipe.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD,
                                ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD),
                                ctypes.POINTER(w.DWORD)]
    k.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD,
                           ctypes.POINTER(w.DWORD), ctypes.c_void_p]
    k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    k.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    k.ResumeThread.argtypes = [w.HANDLE]
    k.CloseHandle.argtypes = [w.HANDLE]
    _k32 = k
    return k


def _container_sd():
    """管道用的安全描述符：允许 AppContainer（SDDL 别名 `AC`）与本机所有用户。

    ★ 为什么管道要单独做：它是**我们的对象**，默认 DACL 只授给当前用户。
      容器里的进程拿受限令牌做访问检查，过不去 —— 表现是初始化 std 句柄时
      直接 0xC0000142，看着像 DLL 坏了，其实是「这根管子不归我用」。
      （2026-10-27 实测：同一份 whoami.exe，不接管道 rc=0，接了就 0xC0000142，
       差别全在这一处。）
    """
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    sd = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            ctypes.c_wchar_p("D:(A;;GA;;;WD)(A;;GA;;;AC)"), 1,
            ctypes.byref(sd), None):
        raise OSError(ctypes.get_last_error(), "建安全描述符失败")
    return sd


class _SA(ctypes.Structure):
    _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", w.BOOL)]


def make_pipe(keep_read: bool = False):
    """建一根**容器可用**的管道，返回 (读端, 写端)。

    ★ 两边的继承标志必须分开处理，这一条踩过：
      · 我们读的那一端（stdout/stderr 的读端）**不许继承** —— 继承了子进程会
        等一个永远关不掉的写端，于是挂死；
      · 给子进程当 stdin 的那一端**必须继承** —— 关掉它，子进程拿到无效句柄，
        CRT 初始化直接失败（0xC0000142）。同一份代码，差一行就是两种坏法。
    """
    global _SD
    if _SD is None:
        _SD = _container_sd()
    k = _k()
    sa = _SA(ctypes.sizeof(_SA), _SD, True)
    r, wr = w.HANDLE(), w.HANDLE()
    if not k.CreatePipe(ctypes.byref(r), ctypes.byref(wr), ctypes.byref(sa), 0):
        raise OSError(ctypes.get_last_error(), "CreatePipe 失败")
    if keep_read:
        k.SetHandleInformation(wr, HANDLE_FLAG_INHERIT, 0)
    else:
        k.SetHandleInformation(r, HANDLE_FLAG_INHERIT, 0)
    return r, wr


def _env_block(env: dict | None):
    """把 dict 编成 CreateProcessW 要的环境块（`K=V\\0…\\0`，按名字排序）。"""
    if env is None:
        return None
    keep = {str(k): str(v) for k, v in env.items() if v is not None}
    import re as _re
    # 大小写不敏感去重（Windows 环境变量名不区分大小写，重复会让子进程拿到两套）
    seen = {}
    for k, v in keep.items():
        seen[k.upper()] = (k, v)
    items = ["%s=%s" % kv for kv in sorted(seen.values(), key=lambda x: x[0].upper())]
    return ctypes.create_unicode_buffer("\0".join(items) + "\0\0")


class Proc:
    """最小的 `Popen` 替身：只做 `communicate(timeout)` / `poll` / `kill` / `resume`。

    ★ 为什么自己写 communicate 而不用线程 + `ReadFile` 阻塞读：
      写端被子进程的**孙子**握着时，阻塞读会一直等 EOF —— 实测直接把探针挂死
      （2026-10-27：一次 `python -c` 试跑卡了 180 秒）。所以改成
      `PeekNamedPipe` 轮询 + 进程已退出后再给一小段收尾时间。
    """

    def __init__(self, hproc, hthread, pid, r_out, r_err, out_buf, err_buf,
                 state, threads):
        self._h = hproc
        self._ht = hthread
        self.pid = pid
        self._r_out, self._r_err = r_out, r_err
        self._out, self._err = out_buf, err_buf
        self._state = state
        self._threads = threads
        self.returncode = None

    # ── 让 isolate 能在「套 job」之后把它放行 ──
    def resume(self) -> int:
        k = _k()
        n = int(k.ResumeThread(self._ht) or 0)
        return n

    # ── 兼容 Popen 的 .stdout / .stderr（2026-10-29）──────────────────────
    # 为什么要有：`isolate.popen` 的调用方习惯 `p.stdout.read()`，而 Proc 只有
    # communicate()。于是**容器这条路即使走进去，调用方第一句就 AttributeError** ——
    # 那 3 条「命令确实走了容器」的探针一直红，根因在这里而不是 assign 顺序。
    # 代价：`read()` 会先 communicate（等进程退出），也就是**不做流式**。
    # 容器这一档只接一次性命令（见 `_container_eligible`），这个代价可以接受。
    class _PipeView:
        def __init__(self, owner, idx):
            self._o, self._i, self._pos = owner, idx, 0

        def _data(self):
            d = self._o._pipes()[self._i]
            return d if isinstance(d, str) else (d or b"")

        def read(self, n=-1):
            s = self._data()[self._pos:]
            if n is not None and n >= 0:
                s = s[:n]
            self._pos += len(s)
            return s

        def readlines(self):
            return self.read().splitlines(True)

        def __iter__(self):
            return iter(self.read().splitlines(True))

        def close(self):
            pass

    def _pipes(self):
        if getattr(self, "_cached", None) is None:
            self._cached = self.communicate()
        return self._cached

    @property
    def stdout(self):
        if not hasattr(self, "_pv"):
            self._pv = (Proc._PipeView(self, 0), Proc._PipeView(self, 1))
        return self._pv[0]

    @property
    def stderr(self):
        if not hasattr(self, "_pv"):
            self._pv = (Proc._PipeView(self, 0), Proc._PipeView(self, 1))
        return self._pv[1]

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if _k().WaitForSingleObject(self._h, 0) == WAIT_TIMEOUT:
            return None
        self.returncode = self._code()
        return self.returncode

    def _code(self) -> int:
        code = w.DWORD(0)
        _k().GetExitCodeProcess(self._h, ctypes.byref(code))
        rc = int(code.value)
        if rc >= 0x80000000:
            rc -= 0x100000000
        return rc

    def wait(self, timeout: float | None = None):
        import time as _t
        end = None if timeout is None else _t.time() + timeout
        while True:
            r = self.poll()
            if r is not None:
                return r
            if end is not None and _t.time() >= end:
                raise TimeoutError("进程 %s 还没结束" % self.pid)
            _t.sleep(0.05)

    def communicate(self, timeout: float | None = None):
        import subprocess
        import time as _t
        if self.returncode is None:
            end = None if timeout is None else _t.time() + timeout
            while True:
                left = 0.2 if end is None else min(0.2, max(0.0, end - _t.time()))
                r = _k().WaitForSingleObject(self._h, int(left * 1000))
                if r != WAIT_TIMEOUT:
                    self.returncode = self._code()
                    break
                if end is not None and _t.time() >= end:
                    raise subprocess.TimeoutExpired(" ".join(self._argv), timeout)
        self._state["exited"] = True
        for t in self._threads:
            t.join(timeout=1.5)
        try:
            self._k_close()
        except Exception:
            pass
        return (b"".join(self._out).decode("utf-8", "replace"),
                b"".join(self._err).decode("utf-8", "replace"))

    def kill(self):
        try:
            _k().TerminateProcess(self._h, 1)
        except Exception:
            pass

    def _k_close(self):
        k = _k()
        for h in (self._r_out, self._r_err, self._h, self._ht):
            try:
                k.CloseHandle(h)
            except Exception:
                pass


def _reader(h, buf, state):
    """轮询读一根管子，读到 EOF 就把线程结束。"""
    import time as _t
    k = _k()
    block = ctypes.create_string_buffer(8192)
    got = w.DWORD(0)
    avail = w.DWORD(0)
    while True:
        if not k.PeekNamedPipe(h, None, 0, None, ctypes.byref(avail), None):
            break
        if avail.value:
            if not k.ReadFile(h, block, 8192, ctypes.byref(got), None):
                break
            if got.value:
                buf.append(block.raw[:got.value])
                continue
        if state.get("exited"):
            # 进程已经退了，但孙子可能还握着写端 —— 给 0.3 秒收尾，然后放弃。
            # ★ 不设这个上限就会「命令跑完了，我们还挂着」。
            if state.setdefault("_grace", None) is None:
                state["_grace"] = _t.time()
            elif _t.time() - state["_grace"] > 0.3:
                break
        _t.sleep(0.02)


def spawn(argv, cwd: str | None = None, env: dict | None = None):
    """在容器里**挂起**着建一个进程，接好三根管道。返回 Proc；建不起来返回 None。

    ★ 挂起（CREATE_SUSPENDED）是刻意的：顺序必须是「先套 Job Object，再放行」，
      理由见 `isolate.py` 顶上那段实测（晚套 = 没套，窗口站已经继承完了）。
    """
    import subprocess
    import threading
    if not supported() or not enabled():
        return None
    p = ensure_profile()
    if not p.get("ok"):
        return None
    try:
        out_r, out_w = make_pipe()
        err_r, err_w = make_pipe()
        in_r, in_w = make_pipe(keep_read=True)
    except OSError:
        return None

    k = _k()
    size = ctypes.c_size_t(0)
    k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    if size.value <= 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if not k.InitializeProcThreadAttributeList(buf, 1, 0, ctypes.byref(size)):
        return None
    caps = _SECURITY_CAPABILITIES(p["sid"], None, 0, 0)
    if not k.UpdateProcThreadAttribute(buf, 0, ctypes.c_void_p(_ATTR_SECCAP),
                                       ctypes.byref(caps), ctypes.sizeof(caps),
                                       None, None):
        return None

    si = _STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    si.StartupInfo.hStdInput = in_r
    si.StartupInfo.hStdOutput = out_w
    si.StartupInfo.hStdError = err_w
    si.lpAttributeList = ctypes.cast(buf, ctypes.c_void_p)
    pi = _PROCESS_INFORMATION()

    line = subprocess.list2cmdline([str(x) for x in argv])
    cmd = ctypes.create_unicode_buffer(line)
    eblk = _env_block(env)
    flags = (_EXTENDED_STARTUPINFO_PRESENT | _CREATE_NO_WINDOW
             | 0x00000004)                      # CREATE_SUSPENDED
    if eblk is not None:
        flags |= CREATE_UNICODE_ENVIRONMENT
    ok = k.CreateProcessW(None, cmd, None, None, True, flags,
                          ctypes.cast(eblk, ctypes.c_void_p) if eblk else None,
                          cwd, ctypes.byref(si), ctypes.byref(pi))
    err = ctypes.get_last_error()
    # 父进程手里这几根必须立刻放开，否则读端永远等不到 EOF
    for h in (out_w, err_w, in_r, in_w):
        try:
            k.CloseHandle(h)
        except Exception:
            pass
    if not ok:
        for h in (out_r, err_r):
            try:
                k.CloseHandle(h)
            except Exception:
                pass
        return None
    out_buf, err_buf = [], []
    state = {"exited": False}
    th = [threading.Thread(target=_reader, args=(out_r, out_buf, state), daemon=True),
          threading.Thread(target=_reader, args=(err_r, err_buf, state), daemon=True)]
    for t in th:
        t.start()
    pr = Proc(pi.hProcess, pi.hThread, int(pi.dwProcessId),
              out_r, err_r, out_buf, err_buf, state, th)
    pr._argv = [str(x) for x in argv]
    return pr


# ── ⑤ 授权：容器能碰什么，写下来、也能收回去 ────────────────────────
def _icacls(args: list) -> tuple:
    import subprocess
    r = subprocess.run(["icacls"] + args, capture_output=True)
    raw = (r.stdout or b"") + (r.stderr or b"")
    for enc in ("utf-8", "gbk"):
        try:
            return r.returncode, raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return r.returncode, raw.decode("utf-8", "replace")


def _grants_file():
    from . import paths as P
    return P.data_root() / "harness" / "contain_grants.json"


def grants() -> list:
    try:
        import json
        d = json.loads(_grants_file().read_text(encoding="utf-8"))
        return [x for x in (d.get("paths") or []) if isinstance(x, list) and len(x) == 2]
    except Exception:
        return []


def ensure_grants(paths: list) -> dict:
    """给容器 SID 授 `修改`（(OI)(CI)M）——**只给列出来的目录**。

    ★ 为什么必须授：AppContainer 的默认策略是「读不到容器外的东西」。
      不授，命令一进容器就报 0x80070002（找不到文件），看着像路径写错了。
    ★ 为什么记下来：改的是用户磁盘上的 ACL，属于**留痕的副作用**。
      记在 `~/.harness/contain_grants.json`，`revoke()` 才能精确收回。
    """
    import json
    p = ensure_profile()
    if not p.get("ok"):
        return {"ok": False, "why": p.get("why")}
    sid = sid_str(p["sid"])
    if sid == "?":
        return {"ok": False, "why": "拿不到 SID 字符串"}
    done, fails = [], []
    for path in paths:
        if not path or not os.path.isdir(path):
            continue
        code, msg = _icacls([path, "/grant", "*%s:(OI)(CI)M" % sid])
        (done if code == 0 else fails).append([path, msg.strip()[:200]])
    old = grants()
    merged = {tuple(x): x for x in old}
    for d in done:
        merged[tuple(d)] = d
    try:
        _grants_file().parent.mkdir(parents=True, exist_ok=True)
        _grants_file().write_text(json.dumps(
            {"sid": sid, "paths": sorted(merged.values())},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return {"ok": not fails, "granted": [x[0] for x in done], "failed": fails, "sid": sid}


def interpreter_paths(deep: bool = False) -> list:
    """解释器那一套要授什么。

    ★ `deep=False`（默认）：只授解释器自己的目录（递归只读）。
      **不碰祖先目录** —— 2026-10-27 实测：在父目录上加一条 ACE，系统会把整棵子树
      重算一遍（`.workbuddy/binaries` 43 秒，`C:\\Users\\Administrator` 跑不完）。
      这个代价不能挂在日常执行上，所以 python 类命令默认不进容器
      （见 `isolate._container_eligible`）。
    ★ `deep=True`：连祖先目录逐级授 (X)（只遍历、不继承）。
      只在「想把解释器也关进容器」时手动用，并接受那几十秒。
    """
    import sys
    base = os.path.abspath(sys.base_prefix)
    out = [[base, "(OI)(CI)RX"]]
    if not deep:
        return out
    chain = []
    p = base
    while True:
        parent = os.path.dirname(p)
        if parent == p or len(parent) <= 3:
            break
        chain.append([parent, "(X)"])
        p = parent
    return list(reversed(chain)) + out


def ensure_interpreter(deep: bool = False) -> dict:
    """把解释器那套授权授好，然后**实测**一次 python 能不能在容器里跑。"""
    import json
    import sys
    p = ensure_profile()
    if not p.get("ok"):
        return {"ok": False, "why": p.get("why")}
    sid = sid_str(p["sid"])
    pairs = interpreter_paths(deep=deep)
    fails = []
    for path, mode in pairs:
        if not os.path.isdir(path):
            continue
        code, msg = _icacls([path, "/grant", "*%s:%s" % (sid, mode)])
        if code != 0:
            fails.append([path, msg.strip()[:160]])
    try:
        old = json.loads(_grants_file().read_text(encoding="utf-8"))
    except Exception:
        old = {"sid": sid, "paths": []}
    have = {tuple(x) for x in (old.get("paths") or [])}
    for path, mode in pairs:
        have.add((path, mode))
    try:
        _grants_file().parent.mkdir(parents=True, exist_ok=True)
        _grants_file().write_text(json.dumps(
            {"sid": sid, "paths": sorted(have)}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass
    r = launch(sys.executable, ["-c", "print(1)"], timeout_ms=20000)
    return {"ok": bool(r.get("ok")), "rc": r.get("rc"), "why": r.get("why") or "",
            "failed": fails, "deep": deep}


def revoke_grants() -> dict:
    """把记下来的授权全部收回（改过 ACL 的东西必须能还原）。"""
    p = ensure_profile()
    if not p.get("ok"):
        return {"ok": False, "why": p.get("why")}
    sid = sid_str(p["sid"])
    gone, fails = [], []
    for path, _mode in grants():
        code, msg = _icacls([path, "/remove", "*%s" % sid])
        (gone if code == 0 else fails).append([path, msg.strip()[:160]])
    try:
        _grants_file().unlink()
    except OSError:
        pass
    return {"ok": not fails, "removed": [x[0] for x in gone], "failed": fails}
