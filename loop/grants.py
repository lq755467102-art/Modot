#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""grants.py —— 一次性许可：被拦下来的那一步，人点头之后**只放行这一步**。

它是什么
    闸门（`sandbox_rules` / 只读白名单）拦下动作时，会顺手算一个**签名**：
    这个脚本的路径 + 内容哈希 / 这条命令的完整参数 / 这个路径加动作。
    人看过之后按一下「允许这一次」，就往这台机器的许可表里写一行。
    同样的签名再过闸时，闸门放行，并**当场把它用掉**（`once`）或让它自己过期（`ttl`）。

为什么按签名给，而不是按工具给
    如果许可是「允许 run_script」，那"允许一次"就等于"以后这个工具随便用" ——
    而拦住动作的全部意义恰恰在"这一次是什么"。
    签名把许可钉在**具体内容**上：脚本改一个字哈希就变，许可自动失效。
    这条性质比任何提示词都硬，也是这一层唯一值得信的地方。
    ⇒ 所以界面上必须让人看见"它想干什么"（那几行命中原文），再给按钮。

★ 三条必须一直在的性质
    ① 许可表**不在工作区**（`~/.harness/grants.jsonl`）—— 文件工具写不到它。
       再加一道：`is_protected()` 也挡（万一以后挪位置、或者工作区正好是家目录）。
    ② **用完即焚**（`once`）或**限时**（`ttl`，默认 10 分钟）。
       没有"永久允许"这一档 —— 要长期允许请改 `harness/sandbox_rules.json`，
       那是**写下来的规则**（可审、可进版本库），而不是一次点击的残留。
    ③ 每一次授予、拒绝、使用、撤销都写进同一张表。
       否则"它到底自己批过几次"没人答得出 —— 而"看不见的权力"和"没有这道权力"
       在出事那天长得一模一样。

★ 它挡不住什么（说清）
    许可是**人的一次决定**，不是沙箱。给了之后，这一段代码就跑在本机权限里 ——
    它能在这一刻干的事，仍然由 OS 决定，不由这张表决定。
"""
# 主要引用方包括 `workbench.py` 和 `tools.py`。
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

ONCE = "once"
TTL = "ttl"
DEFAULT_TTL = 600          # 10 分钟：够它跑完、重跑一次，不够它过夜
MODES = (ONCE, TTL)

MODE_CN = {ONCE: "只这一次", TTL: "限时"}


def path() -> Path:
    """许可表的位置。`HARNESS_GRANTS` 可覆盖（验收/探针用，不碰真表）。

    ★ 为什么在 `~/.harness` 而不是数据根：这里是**这台机器的安全状态**，
      和习惯区同一个性质（跨项目）。而工作区边界天然挡住它 ——
      更重要的是它在工作区外面，agent 的文件工具根本够不着。
    """
    env = os.environ.get("HARNESS_GRANTS")
    return Path(env) if env else (Path.home() / ".harness" / "grants.jsonl")


def is_protected(p) -> bool:
    """这个路径是安全文件吗（许可表 / 接口钥匙 / 凭据）。文件工具一律不许碰。

    ★ 为什么要有：许可表如果 agent 自己能写，那"停下来问"就变成
      "它自己给自己批一张条子" —— 这一层等于不存在，而且看不出来。
    """
    try:
        from . import paths as P
        # ★ 规则文件也算安全状态（2026-10-26）：能改规则就等于能给自己发免死金牌 ——
        #   往免扫名单里塞一个脚本、或者把某条 deny 改成 allow，都不用再问任何人。
        #   人要改它用 `bin/rules.py`（或直接编辑），那条路不经过 agent 的工具。
        cands = [path(), P.token_file(),
                 P.data_root() / "harness" / "sandbox_rules.json",
                 P.app_root() / "harness" / "sandbox_rules.json"]
    except Exception:
        cands = [path()]
    try:
        cands.append(Path(os.path.expanduser("~/.workbuddy/harness/credentials.env")))
    except Exception:
        pass
    try:
        tgt = Path(str(p or "")).resolve()
    except (OSError, RuntimeError):
        return False
    for c in cands:
        try:
            if tgt == Path(c).resolve():
                return True
        except (OSError, RuntimeError):
            continue
    return False


# ───────────────────── 签名：把"这一次"变成一个字符串 ─────────────────────
def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:16]


# 命令名归一时要砍掉的后缀（Windows 上 `cat` 和 `CAT.EXE` 是同一条命令）
_EXE_SUFF = (".exe", ".cmd", ".bat", ".com", ".ps1")


def _bin_name(s: str) -> str:
    """`C:/x/CAT.EXE` → `cat`。只留文件名、小写、去可执行后缀。

    ★ 为什么要去后缀：人批的是"跑 cat 这条命令"，而他看到的那条可能带
      `.exe`、agent 重发时可能不带 —— 两个签名对不上，就会"我明明批过它还在问我"。
    """
    b = str(s or "").replace("\\", "/").split("/")[-1].strip().lower()
    for suf in _EXE_SUFF:
        if b.endswith(suf) and len(b) > len(suf):
            return b[:-len(suf)]
    return b


def sig_cmd(cmd) -> str:
    """一条命令的签名：程序名（归一）+ 参数**原样**。

    ★ 参数不做任何归一：`rm -rf a` 和 `rm -rf A` 在 Windows 上可能是同一个路径，
      但"按原样算"只会**多拦一次**，不会少拦。这类地方宁可保守。
    """
    parts = ([str(x) for x in cmd] if isinstance(cmd, (list, tuple))
             else [str(cmd or "")])
    if not parts or not parts[0].strip():
        return ""
    return _h("cmd\x00" + "\x00".join([_bin_name(parts[0])] + parts[1:]))


def sig_script(sp, root=None) -> str:
    """一个脚本的签名：归一化路径 + **内容哈希**。

    ★ 内容进哈希是关键：人批准的是"这份代码"，不是"这个文件名"。
      脚本被改过 ⇒ 签名变 ⇒ 许可自动失效 ⇒ 得再看一次。这比提示词可靠。
    ★ 读不出内容就返回空串（= 不给许可）。宁可让人去修文件，
      也不要给一个"没被看清过"的东西发通行证。
    """
    from . import sandbox_rules as SBR          # 迟导入：两边互相引用
    p = Path(str(sp or ""))
    try:
        body = p.read_bytes()
    except OSError:
        return ""
    return _h("script\x00" + SBR.norm_path(p, root) + "\x00"
              + hashlib.sha256(body).hexdigest()[:16])


def sig_mcp(server: str, tool: str, args=None) -> str:
    """一次 MCP 工具调用的签名：server + tool + **规范化之后的参数**。

    ★ 参数要排序后序列化：同一个调用换个键序还是同一次调用，
      不该因为 JSON 键顺序不同就变成"没批过"。
    """
    import json as _j
    try:
        raw = _j.dumps(args or {}, ensure_ascii=False, sort_keys=True,
                       default=str)[:8000]
    except Exception:
        raw = repr(args)[:8000]
    return _h("mcp\x00%s\x00%s\x00%s" % (server, tool, raw))


def sig_file(action: str, p, root=None) -> str:
    from . import sandbox_rules as SBR
    a = str(action or "").strip().lower()
    if a not in ("read", "write", "delete"):
        return ""
    return _h("file\x00" + a + "\x00" + SBR.norm_path(p, root))


# ───────────────────── 表：只追加，读的时候现算 ─────────────────────
def _append(rec: dict) -> bool:
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def rows() -> list[dict]:
    """整张表（旧在前）。坏行跳过 —— 一行写坏了不该让整层失效。"""
    p = path()
    out: list[dict] = []
    try:
        if not p.exists():
            return out
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if isinstance(r, dict):
                out.append(r)
    except OSError:
        return out
    return out


class _Lock:
    """跨进程的短锁（许可表会被界面和若干 agent 进程同时写）。

    ★ 自己实现而不用 `msvcrt`：这段代码在 Windows 和 Linux 都要跑，
      而消费许可**不是**高频操作 —— 独占创建文件足够，且失败方式看得见
      （超时就报错，不静默继续）。
    """

    def __init__(self, p: Path, timeout: float = 3.0):
        self.p = p
        self.timeout = timeout

    def __enter__(self):
        self.p.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        while True:
            try:
                fd = os.open(str(self.p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except FileExistsError:
                # 陈旧锁（进程死了没删）超过 10 秒就当没有
                try:
                    if time.time() - self.p.stat().st_mtime > 10:
                        self.p.unlink()
                        continue
                except OSError:
                    pass
                if time.time() - t0 > self.timeout:
                    raise TimeoutError("许可表被占用（%s）" % self.p)
                time.sleep(0.02)
            except OSError:
                return self            # 建不了锁就别卡住：这只是防并发，不是权限

    def __exit__(self, *a):
        try:
            self.p.unlink()
        except OSError:
            pass
        return False


def _state(all_rows) -> dict[str, list[dict]]:
    """按签名折叠出"现在有哪些许可"。只有这一处定义"什么算活着"。

    ★ 顺序有意义：`use` 只能核销**它之前**的 grant；`revoke`/`deny`
      只作用于它之前的 grant（之后人再点一次允许，就是新许可）。
    """
    per: dict[str, list[dict]] = {}
    for r in all_rows:
        s = str(r.get("sig") or "")
        if not s:
            continue
        a = str(r.get("act") or "")
        if a == "grant":
            t = float(r.get("t") or 0)
            mode = TTL if str(r.get("mode") or "") == TTL else ONCE
            ttl = float(r.get("ttl") or 0) if mode == TTL else 0.0
            per.setdefault(s, []).append({
                "gid": str(r.get("gid") or ""), "mode": mode, "t": t,
                "exp": (t + ttl) if mode == TTL else 0.0,
                "used": False, "rev": False, "used_at": 0.0,
                "kind": str(r.get("kind") or ""), "label": str(r.get("label") or ""),
                "why": str(r.get("why") or ""), "src": str(r.get("src") or ""),
            })
        elif a == "use":
            gid = str(r.get("gid") or "")
            for g in per.get(s, []):
                if g["rev"] or (g["mode"] == ONCE and g["used"]):
                    continue
                if gid and g["gid"] != gid:
                    continue
                g["used"] = True
                g["used_at"] = float(r.get("t") or 0)
                break
        elif a in ("revoke", "deny"):
            for g in per.get(s, []):
                g["rev"] = True
    return per


def _live_one(gs, now: float):
    for g in gs:
        if g["rev"]:
            continue
        if g["mode"] == TTL:
            if g["exp"] > now:
                return g
        elif not g["used"]:
            return g
    return None


def live(sig: str):
    """这个签名现在有活的许可吗（不消耗）。"""
    if not sig:
        return None
    return _live_one(_state(rows()).get(sig, []), time.time())


def take(sig: str):
    """有许可就**用掉**并返回它；没有返回 None。

    ★ 用完即焚：`once` 写完 `use` 记录之后就不再算活的了。
      所以"允许一次"真的只能过一关，它想再来一次得再问一遍 ——
      这一条是"停下来问"能不能长期用下去的关键：批一次的成本必须是一次点击，
      而不是"批了之后再也收不回来"。
    """
    if not sig:
        return None
    with _Lock(path().with_suffix(".lock")):
        g = _live_one(_state(rows()).get(sig, []), time.time())
        if g is None:
            return None
        _append({"t": time.time(), "act": "use", "gid": g["gid"], "sig": sig})
        return g


def grant(sig: str, kind: str = "", mode: str = ONCE, ttl: int = DEFAULT_TTL,
          why: str = "", src: str = "", label: str = "") -> dict:
    if not sig:
        return {"ok": False,
                "error": "没有签名 —— 这一步给不了一次性许可（它还没被看清，先让它可读）"}
    m = TTL if str(mode or "").lower() in (TTL, "temp", "限时") else ONCE
    t = time.time()
    # ★ `ttl or DEFAULT_TTL` 会把 0 换成 600（0 是假值）—— 而 0 的意思是
    #   "立刻过期"。这一行原来就写错了，是探针 D3 逮出来的。
    _ttl = int(DEFAULT_TTL if ttl is None else ttl)
    if _ttl < 0:
        _ttl = 0
    gid = _h("%s|%s|%.6f|%s" % (sig, m, t, os.getpid()))[:8]
    rec = {"t": t, "act": "grant", "gid": gid, "sig": sig, "kind": kind, "mode": m,
           "ttl": _ttl if m == TTL else 0,
           "why": why, "src": src, "label": label}
    if not _append(rec):
        return {"ok": False, "error": "许可表写不进去：%s" % path()}
    return {"ok": True, "gid": gid, "sig": sig, "mode": m,
            "mode_cn": MODE_CN[m], "ttl": rec["ttl"], "label": label,
            # ★ ttl 存的是**秒**，对人说的是**分钟**（2026-10-26 走查时发现：
            #   600 秒被说成"600 分钟"，差 60 倍）。换算只在这一处做，别的地方都读 `ttl`。
            "note": ("只放行这一次；用掉就没了" if m == ONCE
                     else "接下来 %d 分钟内同一步不再问" % max(1, round(rec["ttl"] / 60)))}


def deny(sig: str, why: str = "", src: str = "", label: str = "") -> dict:
    """人说不给。**同时把之前给过的那张也撤掉** —— 人刚说"不"，
    还留着一张活许可就是自相矛盾。"""
    if not sig:
        return {"ok": False, "error": "没有签名"}
    ok = _append({"t": time.time(), "act": "deny", "sig": sig,
                  "why": why, "src": src, "label": label})
    if not ok:
        return {"ok": False, "error": "许可表写不进去：%s" % path()}
    return {"ok": True, "denied": True, "sig": sig, "label": label,
            "note": "记下了：这一步不给。之前给过的同一步也一起撤了。"}


def revoke(sig: str, why: str = "", src: str = "") -> dict:
    if not sig:
        return {"ok": False, "error": "没有签名"}
    if not _append({"t": time.time(), "act": "revoke", "sig": sig,
                    "why": why, "src": src}):
        return {"ok": False, "error": "许可表写不进去：%s" % path()}
    return {"ok": True, "revoked": sig, "note": "撤了。同一步的下次会重新问人。"}


def listing(limit: int = 30) -> dict:
    """给界面/命令行看：现在活着的许可 + 最近发生过什么。"""
    rws = rows()
    per = _state(rws)
    now = time.time()
    items = []
    for sig, gs in per.items():
        g = _live_one(gs, now)
        if not g:
            continue
        items.append({"sig": sig, "gid": g["gid"], "kind": g["kind"],
                      "mode": g["mode"], "mode_cn": MODE_CN.get(g["mode"], g["mode"]),
                      "label": g["label"], "why": g["why"], "src": g["src"],
                      "at": int(g["t"]),
                      "left": (int(g["exp"] - now) if g["mode"] == TTL else 0)})
    items.sort(key=lambda x: -x["at"])
    recent = [r for r in rws if str(r.get("act") or "") in
              ("use", "deny", "revoke")][-12:]
    return {"path": str(path()), "count": len(items), "items": items[:limit],
            "recent": list(reversed(recent))}


def describe() -> str:
    """一句话说清现在是什么状态（进启动日志，也给人看）。"""
    d = listing()
    return ("一次性许可：%d 张还在有效期（表在 %s）" % (d["count"], d["path"])
            if d["count"] else "一次性许可：现在没有活着的（表在 %s）" % d["path"])


def label_for(kind: str, target, extra: str = "") -> str:
    """人看的短标签：`脚本 runs/x.py（内容 a1b2c3d4）`。"""
    if kind == "cmd":
        parts = ([str(x) for x in target] if isinstance(target, (list, tuple))
                 else [str(target or "")])
        s = " ".join(parts)
        return "命令 " + (s[:90] + "…" if len(s) > 90 else s)
    if kind == "script":
        return "脚本 %s%s" % (str(target)[:80], extra and ("（%s）" % extra))
    if kind == "mcp":
        return "工具 %s" % str(target)[:90]
    if kind == "file":
        return "路径 %s%s" % (str(target)[:80], extra and ("（%s）" % extra))
    return str(target)[:100]
