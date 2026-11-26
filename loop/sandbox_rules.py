#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sandbox_rules.py —— 执行前的**决策层**：这条命令/这个脚本，允许、问人、还是拒。

它是什么
    一个规则引擎：读 `harness/sandbox_rules.json`，按"路径 / 进程 / 脚本文本"三类
    给出 allow / ask / deny，然后 `tools` 层据此决定跑、停下来问人、还是直接拒。

为什么长这个样子（2026-10-26 用户：「直接照搬厂商的安全设计」）
    WorkBuddy 在 Windows 上用一套自研的用户态沙箱（`tsbx`）：常驻决策服务 +
    执行代理 + 命名管道审批 + 一份 `tsbx_rules.json`。我们**照搬它的结构**：
      · 规则表按 `path glob + read/write/delete 三个动作`写；
      · `default_action = ask` —— 不认识的不是放行，是**问人**；
      · 进程白名单 + 支付/社交软件单列（这是用户最担心的那一类）；
      · 决策与执行分离：策略在 JSON 里，代码只负责匹配。
    **不搬**它的引擎（闭源二进制，也不该反编译）。引擎我们自己写，
    所以下面每一段都能被读、被改、被测。

★ 这一层能挡什么、挡不住什么（必须一直说清）
    能挡：agent **主动**去读凭据、去起支付软件、去联网外发、去模拟点击。
    挡不住：已经跑起来的一段代码里自己干这些事（那要靠解释器层的降权，
    下一轮做；Windows 上是低完整性令牌 + Job Object 的 UI 限制）。
    所以这里叫「决策层」，不叫沙箱 —— 别把两件事混成一个词。

★ 为什么默认 ask 而不是 deny
    deny 在无人值守时等于把活停死；ask 至少留一条路，而且人能看见"它想干什么"。
    这是用户拍板的（2026-10-26）：「我倾向于停下来问」。
"""
# 主要引用方包括 `tools.py` 和 `baseline.py`。
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import paths as P

RULES_FILE = "sandbox_rules.json"

_cache: dict = {"mtime": None, "rules": None}


def rules_path() -> Path:
    """规则文件的位置：数据根优先，退回程序根（打包后改的是数据根那份）。"""
    cand = P.data_root() / "harness" / RULES_FILE
    if cand.exists():
        return cand
    return P.app_root() / "harness" / RULES_FILE


def rules() -> dict:
    """读规则（按 mtime + 文件大小缓存 —— 改完不用重启，这一点和 tsbx 一样）。

    ★ 缓存键从 `mtime` 改成 `(mtime, size)`（2026-10-26 端到端走查逮到的）：
      Windows 上 mtime 的精度可能粗到秒级，**同一秒内改一次规则**时 mtime 不变 ⇒
      正在跑的进程继续按旧规则判定 —— 人会看到「我采纳了它还在问我」。
      加 size 之后，同一秒内的改动几乎必被发现（例外表多一条，字节数必然变）。
      这是"跨进程缓存"能做到的极限：别人的缓存清不掉，只能让判据更敏感。
    """
    p = rules_path()
    try:
        st = p.stat()
        m = (st.st_mtime, st.st_size)
    except OSError:
        return {"default_action": "ask", "file_rules": [], "process_rules": []}
    if _cache["mtime"] == m and _cache["rules"] is not None:
        return _cache["rules"]
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as error:
        raise ValueError("安全规则无法读取，未按空规则继续执行") from error
    if not isinstance(d, dict) or d.get("default_action", "ask") not in {"ask", "allow", "deny"}:
        raise ValueError("安全规则格式无效，未执行")
    d.setdefault("default_action", "ask")
    d.setdefault("file_rules", [])
    d.setdefault("process_rules", [])
    d.setdefault("script_scan", {"rules": []})
    _cache.update(mtime=m, rules=d)
    return d


# ───────────────────── 路径：先归一，再匹配 ─────────────────────
def norm_path(p, root=None, *, resolved=False) -> str:
    """归一成"小写、正斜杠、无尾斜杠"的绝对路径。

    ★ 五个必须在归一里处理掉的绕过（都是我在这一行里能想到的，写完再补）：
      ① `..` —— 用 resolve() 吃掉；
      ② 符号链接 / 目录联接 —— resolve() 会跟过去（Windows 上也会）；
      ③ 大小写 —— NTFS 不敏感，规则里也得按不敏感匹配；
      ④ 短名（`C:/PROGRA~1`）—— resolve() 之后短名会被展开成长名；
      ⑤ NTFS 备用数据流（`a.txt:secret`）—— 匹配前砍掉冒号后半段。
    ★ UNC（`//server/share`）原样保留：它也是"外面"，规则里 `~/**` 匹配不到它，
      于是会落到 default_action（ask）—— 行为是对的，不用特判。
    """
    s = str(p or "").strip().strip('"').strip("'")
    if not s:
        return ""
    # ⑤ 砍掉数据流后缀（但别把盘符 `C:` 也砍了）
    m = re.match(r"^(.*?)(?::([^:]*))$", s)
    if m and len(m.group(1)) > 1 and not re.match(r"^[A-Za-z]$", m.group(1)):
        s = m.group(1)
    s = s.replace("\\", "/")
    if s.startswith("~"):
        s = str(Path.home()).replace("\\", "/") + s[1:]
    if not resolved:
        base = Path(root or P.data_root())
        try:
            pth = Path(s)
            if not pth.is_absolute():
                pth = base / s
            s = str(pth.resolve())
        except (OSError, RuntimeError):
            pass
    s = s.replace("\\", "/")
    if len(s) > 1:
        s = s.rstrip("/")
    return s.lower()


def _glob_re(pat: str) -> re.Pattern:
    """把路径 glob 翻成正则：`**` 跨目录，`*` 不跨，`?` 单字符。"""
    p = pat.replace("\\", "/")
    out = []
    i = 0
    while i < len(p):
        c = p[i]
        if c == "*":
            if p[i:i + 2] == "**":
                out.append(".*")
                i += 2
                # `**/` 要能匹配"零层目录"，否则 `<work>/**` 匹配不到 <work> 本身
                if p[i:i + 1] == "/":
                    i += 1
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.I)


def _as_argv(cmd) -> list:
    """把传进来的 `cmd` 归一成一个词表。

    ★ 为什么要有这一步（2026-10-27 实测）：模型有时把命令写成**一个 JSON 字符串**
      `'["ls", "loop"]'`，而不是数组。原来这里只判 list，字符串就整条进了 `parts[0]`，
      于是白名单按一条叫 `["ls", "loop"]` 的"命令"去比对 —— 必然不匹配，
      一条最普通的 `ls` 被要求人工批准。
      这不是模型笨，是**入口容错没做**：同一件事它可能以三种形状递进来。
    """
    if isinstance(cmd, (list, tuple)):
        return [str(x) for x in cmd]
    sq = str(cmd or "").strip()
    if not sq:
        return [""]
    if sq[0] in "[(":                       # 形如 ["ls", "loop"] / ("ls","loop")
        import ast as _ast
        import json as _json
        for _load in (_json.loads, _ast.literal_eval):
            try:
                v = _load(sq)
            except Exception:
                continue
            if isinstance(v, (list, tuple)):
                return [str(x) for x in v]
    # 最后按 shell 词法拆：`ls -la loop` 这种
    import shlex
    try:
        return shlex.split(sq, posix=False) or [sq]
    except ValueError:
        return [sq]


def _expand(pat: str, root=None) -> str:
    s = str(pat or "")
    s = s.replace("<work>", norm_path(root or P.data_root()))
    s = s.replace("~", str(Path.home()).replace("\\", "/"))
    return s.replace("\\", "/")


# ───────────────────── 决策 ─────────────────────
def _parse_action(a: str) -> dict:
    """`read=allow|write=ask|delete=deny` → {read: allow, ...}。"""
    out = {}
    for part in str(a or "").split("|"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip().lower()] = v.strip().lower()
    return out


def decide_file(action: str, path, root=None) -> dict:
    """这个路径、这个动作（read/write/delete）该 allow / ask / deny。

    返回 {"action": "allow|ask|deny", "rule": 命中的那条, "path": 归一后的路径}

    ★★ 「以后这类都别问」的例外在这里生效（2026-10-26），顺序是**故意**的：
        ① 先按原规则算；
        ② 结果是 `deny` ⇒ 直接返回，**例外根本没机会** ——
           凭据、许可表、规则文件这些"没有允许选项"的东西，
           不能靠人点一下按钮就变成"可以"；
        ③ 结果是 `ask` ⇒ 再查 `allow_rules.file`，命中就降级成 allow。
      ⇒ 一句话：例外只能把「要问」变成「不问」，永远不能把「不许」变成「可以」。
    """
    d = rules()
    tgt = norm_path(path, root)
    res = None
    for r in d.get("file_rules") or []:
        pat = _expand(r.get("path") or "", root)
        if not pat:
            continue
        if _glob_re(pat).match(tgt):
            acts = _parse_action(r.get("action"))
            got = acts.get(action)
            if got:
                res = {"action": got, "rule": r.get("path"), "path": tgt}
                break
    if res is None:
        res = {"action": d.get("default_action") or "ask", "rule": "(默认)", "path": tgt}
    if res["action"] == "ask":
        ex = file_allow_exc(action, tgt, root)
        if ex is not None:
            return {"action": "allow", "rule": ex.get("path"), "path": tgt,
                    "allow_id": ex.get("id"), "by_exception": True}
    return res


def file_allow_exc(action: str, path, root=None):
    """这个动作+路径在「以后这类都别问」的例外表里吗？在就返回那条。

    ★ 只跟 `ask` 比，不跟 `deny` 比 —— 调用点（`decide_file`）也只在这个顺序上调。
    """
    d = rules()
    tgt = norm_path(path, root)
    for it in (d.get("allow_rules") or {}).get("file") or []:
        if str(it.get("action") or "").lower() != str(action or "").lower():
            continue
        pat = _expand(str(it.get("path") or ""), root)
        if pat and _glob_re(pat).match(tgt):
            return it
    return None


def mcp_allow_exc(server: str, tool: str, action: str):
    """这个 MCP 工具的这类调用在例外表里吗？"""
    d = rules()
    for it in (d.get("allow_rules") or {}).get("mcp") or []:
        if (str(it.get("server") or "") == str(server or "")
                and str(it.get("tool") or "") == str(tool or "")
                and str(it.get("action") or "") == str(action or "")):
            return it
    return None


def decide_process(name: str) -> dict:
    """要起的外部程序叫什么（`alipay.exe` 这种）该不该问人。"""
    d = rules()
    n = str(name or "").replace("\\", "/").split("/")[-1].lower()
    for r in d.get("process_rules") or []:
        pat = str(r.get("name") or "").lower()
        if not pat:
            continue
        if _glob_re(pat).match(n):
            return {"action": (r.get("action") or "ask").lower(),
                    "rule": r.get("name"), "name": n}
    return {"action": "allow", "rule": "", "name": n}


def scan_script(text: str, limit: int = 6) -> list[dict]:
    """扫脚本源码：命中"对外产生后果"的具体调用。返回 [{kind, line, code}]。

    ★ 扫的是**写法**不是意图：`import requests` 和 `requests.post(...)` 一眼能看出来，
      而"它到底想不想付款"看不出来 —— 所以这里只报事实，判断交给人。
    """
    hits: list[dict] = []
    if not text:
        return hits
    lines = str(text).splitlines()
    for r in (rules().get("script_scan") or {}).get("rules") or []:
        pat = str(r.get("pattern") or "")
        if not pat:
            continue
        try:
            rx = re.compile(pat)
        except re.error:
            continue
        for i, ln in enumerate(lines, 1):
            if ln.lstrip().startswith("#") or ln.lstrip().startswith("//"):
                continue
            if rx.search(ln):
                hits.append({"kind": str(r.get("kind") or "?"), "line": i,
                             "code": ln.strip()[:160]})
                break
        if len(hits) >= limit:
            break
    return hits


KIND_CN = {"net": "联网", "exec": "起外部程序", "ui": "模拟点击/剪贴板",
           "secret": "读凭据/浏览器数据", "money": "支付相关", "upload": "上传/外发"}


def allow_entry(script_path, root=None):
    """这个脚本在免扫名单里吗？返回那条记录（含 sha256），不在就 None。"""
    d = rules()
    tgt = norm_path(script_path, root)
    sa = d.get("script_allow") or {}
    # ★ 老格式（`paths` 纯列表）也认：配置是人手写的，读到老格式不该崩。
    items = sa.get("items")
    if not items:
        items = [{"path": x, "sha256": ""} for x in (sa.get("paths") or [])]
    for it in items:
        pat = _expand(str(it.get("path") or ""), root)
        if pat and _glob_re(pat).match(tgt):
            return it
    return None


def _sha_of_file(p) -> str:
    import hashlib
    try:
        return hashlib.sha256(Path(str(p)).read_bytes()).hexdigest()
    except OSError:
        return ""


def scan_policy(script_path, root=None) -> dict:
    """这个脚本要不要扫、为什么。返回 {"exempt", "why", "signed"}。

    ★ 免扫 = 「**这份内容**我核过」，不是「这个文件名我放心」（2026-10-26 补哈希）。
      名单里但哈希对不上 ⇒ 免扫失效、退回扫描，并把这件事写进拦截理由：
      "内容和签过的不一样" 是人判断时最需要知道的一句话。
    ★ 没签（sha256 为空）的老条目 ⇒ 也按"没核过"处理，同样退回扫描。
      宁可多扫一次，也不要让一条空哈希变成永久免死金牌。
    """
    it = allow_entry(script_path, root)
    if it is None:
        return {"exempt": False, "why": "", "signed": False}
    want = str(it.get("sha256") or "").strip().lower()
    if not want:
        return {"exempt": False, "signed": False,
                "why": "它在免扫名单 `%s` 里，但那一条**没签哈希** —— 按没核过处理"
                       % it.get("path")}
    got = _sha_of_file(script_path)
    if not got:
        return {"exempt": False, "signed": True,
                "why": "它在免扫名单 `%s` 里，但这个文件读不出来" % it.get("path")}
    if got != want:
        return {"exempt": False, "signed": True,
                "why": "它在免扫名单 `%s` 里，但**内容和签过的不一样**"
                       "（签的是 %s…，现在是 %s…）—— 要么看一眼，要么重签："
                       "`python bin/rules.py sign`" % (it.get("path"), want[:8], got[:8])}
    return {"exempt": True, "why": "", "signed": True}


def is_scan_exempt(script_path, root=None) -> bool:
    """这个脚本是不是"免扫"的（我们自己那批检查器，且内容没变过）。

    ★ 为什么要留口子：`bin/accept.py` 本来就大量用 subprocess —— 不豁免的话，
      验收自己都跑不了，而「子 agent 说这事我做不到」正是权限收窄最典型的翻车形态。
    ★ 现在带哈希：改了就不是同一份了，免扫跟着失效（见 `scan_policy`）。
    """
    return bool(scan_policy(script_path, root).get("exempt"))


def gate_script(script_path, root=None, relaxed: bool = False) -> dict | None:
    """跑脚本之前的闸。返回 None = 放行；否则返回要交给主循环的结果。

    ★ 一次拿三样东西判断：**脚本内容**（最要紧）、**脚本路径**（写类）、
      **脚本所在目录**。为什么不是只看内容：内容能加密/编码，路径不能。

    `relaxed=True`（trusted）：脚本里的“联网 / 起子进程”不再弹窗；
    脚本路径命中 `deny` 仍然硬拒，且不给一次性许可。
    """
    if _owned_call():
        from . import permissions
        relaxed = relaxed and permissions.effective_sandbox() == "danger-full-access"
    sp = Path(str(script_path or ""))
    verdicts = []
    _hard = False
    fv = decide_file("read", sp, root)
    if fv["action"] == "deny":
        # ★ 同理：deny 级不给"允许这一次"（2026-10-26）
        _hard = True
        verdicts.append({"why": "脚本路径命中规则 `%s`（deny）—— 这一类没有允许选项"
                                % fv["rule"], "path": fv["path"]})
    elif fv["action"] != "allow":
        verdicts.append({"why": "脚本路径命中规则 `%s`（%s）" % (fv["rule"], fv["action"]),
                         "path": fv["path"]})
    if relaxed:
        if not _hard:
            return None
        return {"ok": False, "needs_human": False, "blocked_by": "sandbox",
                "error": "脚本路径命中沙箱硬拒绝：" + "；".join(
                    v["why"] for v in verdicts),
                "why": "trusted 不再为脚本里的联网/子进程停下来问人，但凭据和底线仍然不放。",
                "hint": "这条没有一次性许可；真要动它，请人自己来。",
                "script": str(sp)}
    _pol, hits = _script_scan(sp, root)
    if hits:
        why = ("脚本里出现这些调用：" + "、".join(
            sorted({KIND_CN.get(h["kind"], h["kind"]) for h in hits})))
        if _pol.get("why"):
            # ★ 免扫失效的原因要和命中一起给：人看到"内容和签过的不一样"，
            #   判断会完全不同（这是"我改了它"还是"别人动了它"）。
            why = _pol["why"] + "；\n并且" + why
        verdicts.append({"why": why, "hits": hits})
    if not verdicts:
        return None
    # ★★ 一次性许可（2026-10-26）：**先判"要不要拦"，再问"人批过没有"**。
    #   顺序不能反：反了的话，一次本来就不该拦的执行会把许可悄悄用掉，
    #   等真被拦时那张许可已经没了 —— 人会看到"我明明批过，它还在问我"。
    from . import grants as G
    sig = "" if _hard else G.sig_script(sp, root)
    if sig and not _owned_call():
        g = G.take(sig)
        if g is not None:
            return None
    r = _stop(sp, verdicts, sig=sig, kind="script", label=G.label_for("script", sp),
              scope=None if _hard else _script_scope(sp, root, fv, bool(hits)))
    if _hard:
        r["hint"] = ("脚本路径本身在 `deny` 级规则里 —— **没有「允许这一次」**。"
                     "请人自己来。")
    return r


def _script_scope(sp, root, fv, has_hits: bool):
    """「以后这类都别问」对这个脚本是什么范围。

    ★ 内容命中 ⇒ 免扫这一份（带哈希）；路径也正好是 ask 级 ⇒ 顺手把那个目录也放进来
      （不然免扫了还是会被路径规则拦，人点了按钮却没效果 —— 那种"点了没用"最伤人）。
    ★ 只有路径 ask ⇒ 就只放那个目录。
    """
    from . import ruleedit as RE
    sc = None
    if has_hits:
        sc = {"domain": "script", "file": norm_path(sp, root)}
    if fv and fv.get("action") == "ask" and not str(fv.get("by_exception")):
        fs = RE.scope_from_rule(str(fv.get("rule") or ""), fv.get("path") or "", "read")
        if fs:
            if sc:
                sc = {"domain": "script", "file": norm_path(sp, root),
                      "also_file": fs["path"], "also_action": fs["action"]}
            else:
                sc = fs
    return sc


def _cmd_bad(cmd, root=None) -> list[dict]:
    """这条命令的**参数**里，哪些会撞规则。返回 [{why, path, action, rule}]。

    ★ 抽出来是为了让闸和命令行**共用一份判断**（2026-10-26）：
      命令行要回答"这条命令如果被拦，「以后这类都别问」会放开什么"，
      而它不能去调 `gate_cmd` —— 那个会**消耗一次性许可**。两处各写一份的话，
      迟早出现"界面说的范围"和"命令行算的范围"不一样。
    """
    parts = _as_argv(cmd)
    bad = []
    # ★ `-c` / `-e` 后面那一格是**代码**，不是路径（2026-10-30 千行工程实测）。
    #   不跳过它的后果：`python -c "import pathlib\n..."` 整段脚本被当成一个"文件"，
    #   路径规则当然认不出来 ⇒ 落到默认动作「问人」⇒ 测试者想数一下行数也得停下来，
    #   人不在场时这一步就白跑。实测测试者在一次复核里被这条拦了一次。
    #   ★ 跳过的只是**路径规则**：凭据底线在 `baseline` 那一层照查（那一层看的是文本里
    #     有没有那条路径，不要求它长得像个参数），危险动作在 `runcheck` 那层照扫。
    _INLINE_FLAGS = {"-c", "-e", "--eval", "-command", "-encodedcommand", "-p"}
    _prev = ""
    for a in parts[1:]:
        if _prev.lower() in _INLINE_FLAGS:
            _prev = a
            continue
        _prev = a
        if a.startswith("-") or not a:
            continue
        # ★ `.` / `..` 跳过（2026-10-26 真跑逮到的误伤）：
        #   `ls -la .` 里的 `.` 归一化之后就是**工作区根**，而规则写的是 `<work>/**` ——
        #   `**` 不匹配根本身，于是落到 default_action（ask）⇒ 一条最普通的 `ls .`
        #   让整轮停下来问人。这不是"安全"，这是把人训练成无脑点允许。
        if a in (".", ".."):
            continue
        looks = ("/" in a or "\\" in a or a.startswith("~") or a in (".", "..")
                 or re.search(r"\.[A-Za-z0-9]{1,6}$", a))
        if not looks:
            continue
        v = decide_file("read", a, root)
        if v["action"] != "allow":
            # ★ 两个键名字必须分开（2026-10-26 命令行探针逮到的真 bug）：
            #   `action` 是**判定结果**（allow/ask/deny），`act` 是**被查的动作**
            #   （read/write/delete）。第一版把 `action` 当成动作传给 `scope_from_rule`，
            #   于是例外表里写进了 `"action": "ask"` —— 而 `ruleedit` 只认 read/write，
            #   提案永远被拒，**界面那条路也是同一份代码**，一起坏。
            #   同名不同义这个坑这个仓库已经踩过四次，这里再记一次。
            bad.append({"why": "参数 `%s` 命中规则 `%s`（%s）"
                               % (a, v["rule"], v["action"]),
                        "path": v["path"], "action": v["action"],
                        "act": "read", "rule": v["rule"]})
    return bad


def gate_cmd(cmd, root=None, relaxed: bool = False) -> dict | None:
    """跑一条命令之前的闸（`run_check` 用）：只看它要读的文件在不在禁读/要问的规则里。

    `relaxed=True`（trusted）：规则里的 `ask` 不再弹窗，直接放行；
    `deny` 仍然是**硬拒绝**，而且不给一次性许可。trusted 降低的是打扰，不是底线。

    ★ 为什么要它：`run_check` 已经有命令白名单了，但白名单只管"这个程序能不能跑"，
      不管"它要读哪个文件"。`cat ~/.ssh/id_rsa` 的命令名是 `cat`，在白名单里 ——
      参数里的路径才是问题。这一层补的就是参数里的路径。
    """
    if _owned_call():
        from . import permissions
        relaxed = relaxed and permissions.effective_sandbox() == "danger-full-access"
    bad = _cmd_bad(cmd, root)
    if relaxed:
        # trusted：规则里的「问人」降级为放行；但 deny 是底线，照旧硬拒。
        bad = [b for b in bad if b.get("action") == "deny"]
        if not bad:
            return None
        detail = "\n".join("- " + b["why"] for b in bad)
        return {"ok": False, "needs_human": False, "blocked_by": "sandbox",
                "error": "这一步命中了沙箱的硬拒绝：\n" + detail,
                "why": "trusted 不再为「认不出来」停下来问人，但凭据和底线仍然不放。",
                "hint": "这条没有一次性许可；真要动它，请人自己来。",
                "target": str(cmd)[:300]}
    if not bad:
        return None
    # ★ `deny` 不给签名（2026-10-26）：deny 的意思是"这一类没有允许选项" ——
    #   给了签名就等于给了一个「允许这一次」的按钮，语义自相矛盾。
    #   只有 `ask`（认不出来、要人决定）才可批一次。
    _hard = any(b.get("action") == "deny" for b in bad)
    from . import grants as G
    sig = "" if _hard else G.sig_cmd(cmd)
    if sig and not _owned_call():
        g = G.take(sig)
        if g is not None:
            return None
    # ★「以后这类都别问」的范围：拿**第一个 ask 参数**自己那条规则的形状去推
    #   （见 `ruleedit.scope_from_rule`）。deny 级一律不给 —— 那一类没有例外。
    scope = None
    if not _hard:
        from . import ruleedit as RE
        for b in bad:
            if b.get("action") != "ask":
                continue
            scope = RE.scope_from_rule(str(b.get("rule") or ""), b.get("path") or "",
                                       b.get("act") or "read")
            if scope:
                break
    r = _stop(cmd, bad, sig=sig, kind="cmd", label=G.label_for("cmd", cmd), scope=scope)
    if _hard:
        r["hint"] = ("这里命中的是 `deny` 级规则（凭据这类）—— **没有「允许这一次」**。"
                     "真要动它，请人自己来；改规则也不行，那条路不经过 agent。")
    return r


def _leaves(obj, depth: int = 0):
    """把参数里所有字符串叶子挖出来（嵌套 dict/list 都挖）。"""
    if depth > 6:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _leaves(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _leaves(v, depth + 1)


_MCP_WRITEISH = ("write", "create", "update", "delete", "remove", "move", "upload",
                 "send", "post", "put", "patch", "exec", "run", "shell", "bash")


def gate_mcp(server: str, tool: str, args=None, root=None) -> dict | None:
    """调一个 MCP 工具之前的闸：**参数里的路径**过文件规则。

    ★ 为什么 MCP 也要过：它是另一条能付款、能发消息、能读写文件的通道 ——
      我们自己的工具被管住了，第三方工具却什么都没查，那道闸就是半扇门。
    ★ 只查路径这一层（不做内容扫描）：工具参数里没有"代码"可扫，
      而**路径**是唯一一件我们真能判的事。剩下的靠"默认每次要人点头"（danger）。
    ★ 写还是读？工具名里带 write/create/send/delete… 就当写，否则当读。
      判错的方向是"多问一次"，不会"少问一次"—— 因为读和写两边都查。
    """
    from . import grants as G
    tgt = "mcp__%s__%s" % (server, tool)
    low = ("%s %s" % (tool, server)).lower()
    act = "write" if any(w in low for w in _MCP_WRITEISH) else "read"
    bad = []
    for v in _leaves(args or {}):
        a = str(v).strip()
        if not a or len(a) > 800:
            continue
        looks = ("/" in a or "\\" in a or a.startswith("~")
                 or re.search(r"\.[A-Za-z0-9]{1,6}$", a))
        if not looks:
            continue
        d = decide_file(act, a, root)
        if d["action"] in ("deny", "ask"):
            bad.append({"why": "参数 `%s` 命中规则 `%s`（%s）" % (a[:120], d["rule"], d["action"]),
                        "path": d["path"], "action": d["action"]})
    if not bad:
        return None
    # ★ 例外（2026-10-26）：这个工具的这类调用如果已经被允许过，直接放行。
    #   ★ 只认 `ask` 来的 bad —— `deny` 的没有例外机制，顺序不能反。
    if all(str(b.get("action") or "") != "deny" for b in bad):
        if mcp_allow_exc(server, tool, act) is not None:
            return None
    sig = G.sig_mcp(server, tool, args)
    g = None if _owned_call() else G.take(sig)
    if g is not None:
        return None
    _sc = None
    if all(str(b.get("action") or "") != "deny" for b in bad):
        _sc = {"domain": "mcp", "server": server, "tool": tool, "action": act}
    return _stop(tgt, bad, sig=sig, kind="mcp", label=G.label_for("mcp", tgt), scope=_sc)


def _owned_call():
    from .tools import current_call
    return current_call()


def _stop(target, verdicts, sig: str = "", kind: str = "", label: str = "",
          scope=None) -> dict:
    """把"要停下来"拼成工具结果。**这是"停下来问"的落地形态**。

    ★ 用 `needs_human: True`：主循环看到它就整轮停、等人说话。
      不带这个字段的话，模型会拿到一句"被拒了"然后换个写法继续试 ——
      而"换个写法继续试"正是我们要防的。
    ★ 带 `grant_sig`：界面拿到它才能给出「允许这一次」那个按钮。
      没有签名就只能让人去改规则文件 —— 那正是这一步要消灭的摩擦。
    ★ 带 `grant_scope`：界面拿到它才能给出「以后这类都别问」。
      范围由命中规则推出来（`ruleedit.scope_from_rule`），**不是**由模型或人临时输入 ——
      让人手填一个 glob，等于给他一个"把闸关掉"的输入框。
      `deny` 级 / 底线那两类没有 scope（`None`），界面就不给这个按钮。
    """
    if sig and _owned_call() and not any(v.get("action")=="deny" for v in verdicts):
        from . import permissions
        original = _sha_of_file(target) if kind == "script" else ""
        permissions.authorize(dict(kind="policy-boundary",sourceKind=kind,target=str(target),
            matchedRules=verdicts,scriptDigest=original,source=_owned_call()))
        if kind == "script" and (not original or _sha_of_file(target)!=original):
            raise ValueError("批准后脚本内容变化，未执行")
        return None
    detail = "\n".join("- " + v["why"] + (("\n  " + "\n  ".join(
        "第 %d 行：%s" % (h["line"], h["code"]) for h in v.get("hits") or []))
        if v.get("hits") else "") for v in verdicts)
    hint = ("人看完这些再决定：界面上的「允许这一次」只放行**这一份**"
            "（脚本改一个字就失效）；要长期允许，改 `harness/sandbox_rules.json`"
            "（改完立刻生效，不用重启）。命令行：`python bin/grant.py`。")
    if not sig:
        hint = ("这一步给不了一次性许可（脚本读不出来 / 拿不到签名）—— "
                "先把文件修好让人能看清，再谈放行。")
    return {
        "ok": False, "needs_human": True, "blocked_by": "sandbox",
        "error": "这一步要对机器外面的世界产生后果了，停下来问人。\n" + detail,
        "why": "沙箱的默认动作是「问人」而不是「放行」：认不出来的东西先停。",
        # 界面靠这两个字段给按钮；`sig` 空 ⇒ 不给按钮（只给"为什么不能批"）
        "grant_sig": sig, "grant_kind": kind, "label": label,
        # 「以后这类都别问」的范围（None ⇒ 界面不给这个按钮）
        "grant_scope": scope or None,
        "hint": hint,
        "target": str(target)[:300],
    }


def _script_scan(sp, root=None):
    """这个脚本要不要扫、扫出什么。返回 (pol, hits)。

    ★ 抽出来是为了让 `gate_script` 和 `scope_for_script` 用**同一份**扫描：
      命令行要回答"以后这类都别问会放开什么"，而它不能调 `gate_script`（会消耗许可）。
    """
    pol = scan_policy(sp, root)
    hits: list[dict] = []
    if not pol.get("exempt"):
        try:
            txt = Path(str(sp)).read_text(encoding="utf-8", errors="replace")[:200000]
            hits = scan_script(txt)
        except OSError:
            hits = []
    return pol, hits


def scope_for_file(action, path, root=None):
    """这个「动作 + 路径」如果会被拦，例外范围是什么。**不消耗任何许可**。

    ★ 只对 `ask` 给范围：`deny` 那类没有例外（凭据/许可表/规则文件），
      `allow` 本来就不用问。三种情况都用一句话说得清，所以这里不用额外参数。
    """
    v = decide_file(action, path, root)
    if v.get("action") != "ask":
        return None
    from . import ruleedit as RE
    return RE.scope_from_rule(str(v.get("rule") or ""), v.get("path") or "", v["action"])


def scope_for_cmd(cmd, root=None):
    """这条命令如果会被拦，例外范围是什么。**不消耗任何许可**。

    取**第一个 ask 级参数**那条规则的形状 —— 和闸里给界面的范围是同一套
    （`gate_cmd` 也走 `ruleedit.scope_from_rule`）。
    """
    from . import ruleedit as RE
    for b in _cmd_bad(cmd, root):
        if b.get("action") != "ask":
            continue
        s = RE.scope_from_rule(str(b.get("rule") or ""), b.get("path") or "",
                               b.get("act") or "read")
        if s:
            return s
    return None


def scope_for_script(sp, root=None):
    """这个脚本如果会被拦，例外范围是什么。**不消耗任何许可**。"""
    sp = Path(str(sp or ""))
    fv = decide_file("read", sp, root)
    _pol, hits = _script_scan(sp, root)
    if fv.get("action") == "allow" and not hits:
        return None
    return _script_scope(sp, root, fv, bool(hits))


def explain() -> str:
    d = rules()
    _ar = d.get("allow_rules") or {}
    _n = len(_ar.get("file") or []) + len(_ar.get("mcp") or [])
    return ("沙箱规则：%d 条文件规则 / %d 条进程规则 / %d 条内容规则，"
            "免问例外 %d 条（只能盖 ask，盖不住 deny），默认=%s（%s）"
            % (len(d.get("file_rules") or []), len(d.get("process_rules") or []),
               len((d.get("script_scan") or {}).get("rules") or []),
               _n, d.get("default_action"), str(rules_path())))
