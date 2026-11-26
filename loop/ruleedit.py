#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ruleedit.py —— 改 `sandbox_rules.json` 的**唯一入口**（人这一侧）。

它解决什么
    拦截卡上一直少一个按钮：「以后这类都别问」。
    少了它，人会做两件更糟的事 —— 要么每次点「允许这一次」（点十次之后就开始无脑点），
    要么干脆把闸关掉。而**把闸关掉是不可逆的**：那一刻之后再没有"停下来问"这件事。

为什么落点不是"改掉原来那条 ask 规则"
    原来那条规则是**描述世界**的（`~/.config/**` 的读要问人），它没说错。
    人点这个按钮说的是"**这一次的这类**我认了"，是一件**例外**，不是把事实改掉。
    ⇒ 所以例外单独一段 `allow_rules`：原规则不动、例外可单条撤、撤掉就回到问人。
    还有一个硬理由：文件规则是"先匹配先赢"，往末尾追加一条 allow 根本盖不住前面那条 ask。

★ 优先级（写死在 `sandbox_rules.decide_file` 里，这里只声明）
    例外只能把 **ask 变成 allow**，永远不能把 **deny 变成 allow**。
    凭据、许可表、规则文件本身都是 deny —— 那一类**没有例外机制**，
    这也是为什么本模块的校验会先问一遍底线清单。

★ 为什么 agent 不能调这个模块
    能给自己发免问条子 = 这一层等于不存在。所以：
      · 规则文件在 `grants.is_protected` / `baseline` / `runcheck.DENY_PATH` 三处都被挡；
      · 本模块只由服务端（人点界面）和 `bin/rules.py`（人跑命令）调用；
      · agent 那边只有**提议权**：它写进提案表，人不采纳就不存在（见 `proposals`）。

★ 为什么每次改都留备份
    例外是"看不见的权力"，而看不见的权力最需要的性质是**能退回去**。
    备份放在数据根的 `.harness/rule-backups/`，文件名带时间戳，不覆盖。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import paths as P

MAX_FILE = 40
MAX_MCP = 30
ACTIONS = ("read", "write")


def _rules_path() -> Path:
    from . import sandbox_rules as SBR
    return SBR.rules_path()


def _backup_dir() -> Path:
    return P.data_root() / ".harness" / "rule-backups"


def load() -> dict:
    p = _rules_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> dict:
    """先留底再写。**备份不成就不改** —— 否则一次误点之后没有退路。"""
    p = _rules_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    if raw:
        bd = _backup_dir()
        try:
            bd.mkdir(parents=True, exist_ok=True)
            (bd / ("sandbox_rules.%s.json" % time.strftime("%Y%m%d-%H%M%S"))).write_text(
                raw, encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": "没能给规则文件留底（%s）—— 为了能退回去，这次不改" % e}
    try:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        return {"ok": False, "error": "规则文件写不进去：%s" % e}
    # ★ 让 `sandbox_rules` 的 mtime 缓存立刻失效：不然界面点完"采纳"，
    #   当前进程还按老规则判，人会看到"我采纳了它还在问我"。
    try:
        from . import sandbox_rules as SBR
        SBR._cache.update(mtime=None, rules=None)
    except Exception:
        pass
    return {"ok": True, "path": str(p)}


def write(d: dict) -> dict:
    """公开的写入口（`bin/rules.py` 签哈希时也走这里）。

    ★ 为什么不让调用方自己 `write_text`：留底、原子替换、清 mtime 缓存这三件事
      漏一件都会出问题（漏清缓存 = 改完不生效，而人只会觉得"这按钮是假的"）。
      规则文件只留**一个**写入口，这三件事就只会漏一次、也只在一个地方漏。
    """
    return _save(d)


def _wing(d: dict) -> dict:
    w = d.setdefault("allow_rules", {})
    w.setdefault("_说明", [
        "「以后这类都别问」的落点（2026-10-26）。这些是**例外**，不是规则本身：",
        "  · 只能把 ask 变成 allow，**永远盖不住 deny**（凭据/许可表/规则文件没有例外）；",
        "  · 底线清单仍然先判，命中就拒，例外到不了那儿；",
        "  · 每条可单独撤（bin/rules.py revoke / 界面），撤掉就回到问人；",
        "  · 改这个文件走 `loop/ruleedit.py`（它先留底再写），agent 的文件工具碰不到它。",
    ])
    w.setdefault("file", [])
    w.setdefault("mcp", [])
    return w


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _id(parts) -> str:
    import hashlib
    return hashlib.sha1("\x00".join(str(x) for x in parts).encode("utf-8")).hexdigest()[:8]


# ───────────────────── 校验：例外能被发出去之前先过这一关 ─────────────────────
def _bad_roots() -> set:
    """不能当例外范围的"太宽的根"。"""
    out = set()
    for x in (Path.home(), P.data_root(), P.app_root()):
        try:
            out.add(str(Path(x)).replace("\\", "/").rstrip("/").lower())
        except (OSError, RuntimeError):
            continue
    for drive in ("c:", "d:", "e:"):
        out.add(drive)
    return out


def check_file_scope(action: str, path_glob: str, root=None) -> dict:
    """一条文件例外能不能成立。返回 {ok, path（规范化后）, why}。

    ★ 为什么查得这么细：这是整条链上唯一一处**扩大权力**的写入口。
      别的写入口（记忆、习惯、提案）最坏是"多一句废话"，这里最坏是"以后不问"。
    """
    a = str(action or "").strip().lower()
    if a not in ACTIONS:
        return {"ok": False, "error": "动作只能是 read / write（delete 不给例外：它不可逆）"}
    s = str(path_glob or "").strip().replace("\\", "/")
    if not s:
        return {"ok": False, "error": "没有范围"}
    if s.startswith("**"):
        # `**/x` 这种是全局名字模式 —— 一放开就是所有目录
        return {"ok": False, "error": "范围不能从 `**` 开头（那等于全盘）"}
    # 盘根 / 家目录根 / 工作区根：都不许整根放开
    for bad in _bad_roots():
        if s.rstrip("/").lower() in (bad, bad + "/**", bad + "/*"):
            return {"ok": False,
                    "error": "「%s」是整个 %s —— 整根开放等于把闸关了，不给例外"
                             % (s, "盘" if len(bad) <= 2 else "目录")}
    # 只允许 `…/**` 或 `…/<名字模式>`：必须以某段目录开头
    if not s.endswith("/**") and "/" not in s.strip("/"):
        return {"ok": False, "error": "范围要写成某段目录下的通配（例如 `<目录>/**`）"}
    # ★ 底线：目标目录本身就是凭据/系统/自我放权 ⇒ 不给例外
    from . import baseline as BL
    probe = s.rstrip("*").rstrip("/")
    h = BL.hit(a, probe, root)
    if h is not None:
        return {"ok": False,
                "error": "「%s」在底线清单里（%s）—— 这一类没有「以后都别问」"
                         % (h.get("why"), a)}
    return {"ok": True, "path": s}


def add_file(action: str, path_glob: str, why: str = "", src: str = "",
             root=None) -> dict:
    v = check_file_scope(action, path_glob, root)
    if not v.get("ok"):
        return v
    d = load()
    w = _wing(d)
    a = str(action).strip().lower()
    for it in w["file"]:
        if it.get("action") == a and it.get("path") == v["path"]:
            it["times"] = int(it.get("times") or 1) + 1
            it["at"] = _now()
            r0 = _save(d)
            if not r0.get("ok"):
                return r0
            return {"ok": True, "id": it.get("id"), "dup": True,
                    "note": "这条例外已经有了（第 %d 次被提到）" % it["times"]}
    if len(w["file"]) >= MAX_FILE:
        return {"ok": False, "error": "文件例外已经 %d 条了（上限 %d）—— 先撤几条不用的"
                                      % (len(w["file"]), MAX_FILE)}
    it = {"id": _id(["file", a, v["path"]]), "action": a, "path": v["path"],
          "why": str(why or "")[:300], "src": str(src or "")[:80],
          "at": _now(), "times": 1}
    w["file"].append(it)
    r = _save(d)
    if not r.get("ok"):
        return r
    return {"ok": True, "id": it["id"], "action": a, "path": v["path"],
            "note": "记下了：以后「%s %s」不问，直接放行（原规则没动，随时能撤）" % (a, v["path"])}


def add_script(path, why: str = "", src: str = "", root=None) -> dict:
    """脚本免扫：把这份脚本（**按内容**）加进 `script_allow` 并签哈希。

    ★ 为什么脚本的"以后不问"只能是"这一份"：脚本一改，行为就变了。
      人批的是这份代码，不是这个文件名 —— 哈希一变自动退回扫描，
      和一次性许可同一个道理。所以这里不生成目录范围的例外。
    """
    import hashlib
    from . import sandbox_rules as SBR
    p = Path(str(path or ""))
    try:
        body = p.read_bytes()
    except OSError as e:
        return {"ok": False, "error": "脚本读不出来（%s）—— 没看清的东西不给免问" % e}
    rel = SBR.norm_path(p, root)
    # 写进规则文件时用可读形态：工作区内的写 `<work>/…`
    work = SBR.norm_path(P.data_root())
    if rel.startswith(work + "/"):
        shown = "<work>/" + rel[len(work) + 1:]
    else:
        shown = rel
    d = load()
    sa = d.setdefault("script_allow", {})
    items = sa.get("items")
    if not items:
        items = [{"path": str(x), "sha256": ""} for x in (sa.get("paths") or [])]
        sa["items"] = items
        sa.pop("paths", None)
    h = hashlib.sha256(body).hexdigest()
    for it in items:
        if str(it.get("path")) == shown:
            it["sha256"] = h
            it["why"] = str(why or "")[:300]
            it["src"] = str(src or "")[:80]
            it["at"] = _now()
            r = _save(d)
            return (r | {"ok": True, "path": shown, "sha256": h[:12], "dup": True,
                         "note": "这份内容已经签过（哈希更新了）"}) if r.get("ok") else r
    items.append({"path": shown, "sha256": h, "why": str(why or "")[:300],
                  "src": str(src or "")[:80], "at": _now()})
    r = _save(d)
    if not r.get("ok"):
        return r
    return {"ok": True, "path": shown, "sha256": h[:12],
            "note": "记下了：这份脚本以后跳过内容扫描（内容一变就自动失效，重新问）"
                    + _also_path(shown, p, why, src, root)}


def _also_path(shown: str, p, why: str, src, root) -> str:
    """脚本免扫之后，如果**脚本所在的路径**本身还要问人，就顺手把那个目录也放开。

    ★ 为什么必须顺手做（2026-10-26 真跑逮到的）：`gate_script` 是"内容"和"路径"
      两条判据**任一命中就拦**。只免扫内容的话，脚本放在 `~/.config` 这类 ask 目录里，
      人点了「以后这类都别问」、看着它变绿了，下一次**照样被拦** ——
      而"点了按钮没反应"比"没有这个按钮"更伤信任。
    ★ 只处理 `ask`：`deny` 那条路本来就没有例外（底线也不许）。
    """
    try:
        from . import sandbox_rules as SBR
        fv = SBR.decide_file("read", p, root)
    except Exception:
        return ""
    if not fv or fv.get("action") != "ask" or fv.get("by_exception"):
        return ""
    fs = scope_from_rule(str(fv.get("rule") or ""), fv.get("path") or "", "read")
    if not fs:
        return ""
    r2 = add_file(fs["action"], fs["path"], why=why, src=src, root=root)
    if not r2.get("ok"):
        return "（脚本所在目录还要问人，但那条例外没加进去：%s）" % (r2.get("error") or "")
    return "；并且放开了它所在位置的读：%s" % fs["path"]


def add_mcp(server: str, tool: str, action: str = "read", why: str = "",
            src: str = "") -> dict:
    sv = str(server or "").strip()
    tl = str(tool or "").strip()
    a = str(action or "read").strip().lower()
    if not sv or not tl:
        return {"ok": False, "error": "要说清是哪个 server 的哪个工具"}
    if a not in ACTIONS:
        a = "read"
    d = load()
    w = _wing(d)
    for it in w["mcp"]:
        if it.get("server") == sv and it.get("tool") == tl and it.get("action") == a:
            it["times"] = int(it.get("times") or 1) + 1
            it["at"] = _now()
            r = _save(d)
            return (r | {"ok": True, "id": it.get("id"), "dup": True,
                         "note": "这条例外已经有了"}) if r.get("ok") else r
    if len(w["mcp"]) >= MAX_MCP:
        return {"ok": False, "error": "工具例外已经 %d 条了（上限 %d）"
                                      % (len(w["mcp"]), MAX_MCP)}
    it = {"id": _id(["mcp", sv, tl, a]), "server": sv, "tool": tl, "action": a,
          "why": str(why or "")[:300], "src": str(src or "")[:80],
          "at": _now(), "times": 1}
    w["mcp"].append(it)
    r = _save(d)
    if not r.get("ok"):
        return r
    return {"ok": True, "id": it["id"],
            "note": "记下了：`%s` 的 `%s` 以后不问（只对这一个工具生效）" % (sv, tl)}


def resign_after_write(path, root=None) -> dict | None:
    """原件刚被我们换掉 ⇒ 如果它本来就在免扫名单里，把签名跟上。

    ★ 为什么放在 ruleedit（2026-10-27）：现在有**两个**写入口会改原件 ——
      `tools.write_file/edit_file`，以及 `swap.py commit`（镜像落回原位）。
      只在 tools 里钩的话，走镜像那条路的改动**不会续签**，
      于是 agent 改完代码、想自己跑验收时又被拦（第五轮实测就是这个）。
      实现只此一份，两个入口都调它。
    ★ 边界：只对**已在名单里**的脚本续签，绝不为新脚本造条目；
      每次都留痕 `src=auto:write`，`bin/rules.py list` 看得见、`revoke` 能撤。
    """
    try:
        from . import sandbox_rules as SBR
        if not SBR.allow_entry(path, root):
            return None
        r = add_script(path, why="自动重签：这份内容是刚通过闸门的那次写入",
                       src="auto:write", root=root)
        return r if r.get("ok") else {"ok": False,
                                      "error": r.get("error") or "重签失败"}
    except Exception as e:                     # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def revoke_script(path: str, root=None) -> dict:
    """把一份脚本从 `script_allow` 里撤掉。

    ★ 为什么必须有（2026-10-27）：`revoke()` 只处理 `allow_rules`（文件/MCP 例外），
      **撤不掉 `script_allow`** —— 而界面上承诺的是"随时能撤"。
      自测时加进去的一条临时条目就撤不掉，只能手改规则文件。
      现在写入即自动重签，撤不掉的后果更重：它改一次就自动续一次期。
    """
    from . import sandbox_rules as SBR
    if not path:
        return {"ok": False, "error": "要撤哪一份？给个路径"}
    rel = SBR.norm_path(path, root)
    work = SBR.norm_path(P.data_root())
    shown = ("<work>/" + rel[len(work) + 1:]) if rel.startswith(work + "/") else rel
    d = load()
    sa = d.get("script_allow") or {}
    items = sa.get("items")
    if not items:
        items = [{"path": str(x), "sha256": ""} for x in (sa.get("paths") or [])]
    keep = [it for it in items if str(it.get("path")) != shown]
    if len(keep) == len(items):
        return {"ok": False, "error": "名单里没有这条：%s" % shown}
    sa["items"] = keep
    sa.pop("paths", None)
    d["script_allow"] = sa
    r = _save(d)
    if not r.get("ok"):
        return r
    return {"ok": True, "path": shown,
            "note": "撤了：这份脚本以后又会走内容扫描（要再免扫就重新 add）"}


def revoke(rid: str = "", domain: str = "", key: str = "") -> dict:
    """撤一条例外。给 id 或给 (domain, key) 都行 —— 命令行按人看得懂的东西撤。"""
    d = load()
    w = d.get("allow_rules") or {}
    rid = str(rid or "").strip()
    dom = str(domain or "").strip().lower()
    key = str(key or "").strip()
    for name in ("file", "mcp"):
        rows = w.get(name) or []
        keep = []
        for it in rows:
            hit = (rid and it.get("id") == rid) or (
                not rid and name == dom and str(it.get("path") or it.get("tool") or "") == key)
            if hit:
                continue
            keep.append(it)
        if len(keep) != len(rows):
            w[name] = keep
            d["allow_rules"] = w
            r = _save(d)
            return (r | {"ok": True, "revoked": rid or key, "domain": name,
                         "note": "撤了。这一类下次还会问人"}) if r.get("ok") else r
    if dom in ("script", "script_allow") and key:
        sa = d.get("script_allow") or {}
        items = sa.get("items") or []
        keep = [it for it in items if str(it.get("path")) != key]
        if len(keep) != len(items):
            sa["items"] = keep
            d["script_allow"] = sa
            r = _save(d)
            return (r | {"ok": True, "revoked": key, "domain": "script",
                         "note": "撤了。这份脚本以后照扫，内容有对外调用就会问人"}) if r.get("ok") else r
    return {"ok": False, "error": "没找到这条例外（可能已经撤过了）"}


def listing() -> dict:
    import hashlib
    d = load()
    w = d.get("allow_rules") or {}
    out = []
    for it in w.get("file") or []:
        out.append({"domain": "file", "id": it.get("id"), "action": it.get("action"),
                    "key": it.get("path"), "label": "%s · %s"
                    % ({"read": "读", "write": "写"}.get(it.get("action"), it.get("action")),
                       it.get("path")),
                    "why": it.get("why"), "src": it.get("src"),
                    "at": it.get("at"), "times": it.get("times")})
    for it in w.get("mcp") or []:
        out.append({"domain": "mcp", "id": it.get("id"), "action": it.get("action"),
                    "key": it.get("tool"),
                    "label": "工具 %s · %s" % (it.get("server"), it.get("tool")),
                    "why": it.get("why"), "src": it.get("src"),
                    "at": it.get("at"), "times": it.get("times")})
    # 脚本免扫：只把**人加的**（带 src/why）算进"例外"，我们自己那批清单不算 ——
    # 否则界面上会冒出九个 bin/*.py，把真正需要看的两三条淹掉。
    for it in (d.get("script_allow") or {}).get("items") or []:
        if not (it.get("src") or it.get("why")):
            continue
        out.append({"domain": "script", "id": "", "action": "skip-scan",
                    "key": it.get("path"), "label": "脚本 %s" % it.get("path"),
                    "why": it.get("why"), "src": it.get("src"),
                    "at": it.get("at"), "times": it.get("times") or 1})
    return {"path": str(_rules_path()), "count": len(out), "items": out}


def describe() -> str:
    d = listing()
    if not d["count"]:
        return "免问例外：没有（规则文件：%s）" % d["path"]
    return "免问例外：%d 条（%s）" % (d["count"], d["path"])


# ───────────────────── "这类"是多大：由命中规则决定，不由人猜 ─────────────────────
def scope_from_rule(rule_pat: str, target_norm: str, action: str) -> dict:
    """把「命中了哪条规则」翻成一个**最小可解释**的例外范围。

    三种形状，各对应一句人能看懂的话：
      · 目录型规则（`~/.config/**`）  ⇒ `<父目录>/**`      「这个目录下的读/写」
      · 名字型规则（`**/*.env`）     ⇒ `<父目录>/*.env`   「这个目录下的 .env」
      · 默认（认不出来的）           ⇒ `<这个文件>`       「就这个文件」
    ★ 为什么不用原规则 pattern 当范围：那等于把人点的"这一次"升级成
      "所有地方都别问"，而人看到的是具体一个路径 —— 界面说的和实际生效的必须一样。
    """
    t = str(target_norm or "").replace("\\", "/").rstrip("/")
    pat = str(rule_pat or "").replace("\\", "/")
    if not t:
        return {}
    d, _, name = t.rpartition("/")
    if not d:
        return {}
    if pat.endswith("/**") or pat in ("(默认)", ""):
        # 目录型 / 默认：默认给精确文件（最保守），目录型给目录
        scope = (d + "/**") if pat.endswith("/**") else t
    else:
        tail = pat.split("/")[-1]          # `*.env` / `credentials*` / `id_rsa`
        scope = (d + "/" + tail) if tail else t
    return {"domain": "file", "action": str(action or "read").lower(), "path": scope,
            "dir": d, "target": t}


def validate_scope(sc) -> dict:
    """一份范围**能不能成立**（提案阶段就验一遍，别等到人点了采纳才报错）。

    ★ 为什么要在提案阶段验（2026-10-26 端到端走查逮到的）：只在校验"采纳"那一侧验，
      界面上会先出现一条「以后读 ~/.ssh 不问」的待采纳提案 —— 人看到这种东西，
      第一反应不是"这条采纳不了"，而是"这系统居然能提这种提案"，信任从这里开始掉。
      fail fast：提的时候就拒，并把理由给人看。
    ★ 采纳那一侧**照样**验（`add_file` / `add_script` / `add_mcp` 里各有一遍）——
      上游的校验是体验，下游的校验才是边界。
    """
    a = sc or {}
    dom = str(a.get("domain") or "")
    if dom == "file":
        v = check_file_scope(str(a.get("action") or "read"), str(a.get("path") or ""))
        return {"ok": bool(v.get("ok")), "error": v.get("error") or ""}
    if dom == "script":
        from pathlib import Path as _P
        f = _P(str(a.get("file") or ""))
        try:
            f.read_bytes()
        except OSError as e:
            return {"ok": False, "error": "脚本读不出来（%s）" % e}
        from . import baseline as BL
        h = BL.hit("read", f)
        if h is not None:
            return {"ok": False, "error": "「%s」在底线清单里 —— 这一类没有免问"
                                         % h.get("why")}
        return {"ok": True, "error": ""}
    if dom == "mcp":
        if not str(a.get("server") or "").strip() or not str(a.get("tool") or "").strip():
            return {"ok": False, "error": "没说是哪个 server 的哪个工具"}
        return {"ok": True, "error": ""}
    return {"ok": False, "error": "范围认不出来（domain=%s）" % (dom or "空")}


def describe_scope(sc) -> dict:
    """把一份范围翻成**人能审的一句话**（提案标题 + 正文）。

    ★ 这两句是人在做决定时唯一会读的东西，所以它们必须说清
      「哪些动作」「在哪」「以后会怎么样」—— 少一样，这个按钮就又变成手滑。
    """
    a = sc or {}
    dom = str(a.get("domain") or "")
    act_cn = {"read": "读", "write": "写"}.get(str(a.get("action") or "read"), "碰")
    if dom == "file":
        path = str(a.get("path") or "")
        return {"title": "以后「%s %s」不问" % (act_cn, path),
                # ★ 这里不要用 markdown 星号（2026-10-26）：这段正文是给界面 `esc()`
                #   之后原样显示的，`**例外**` 会连星号一起显示出来 —— 看着像出 bug。
                "body": ("这个目录下的%s动作以后不再停下来问人：%s\n"
                         "它是「例外」，不是改规则：原来那条「要问」还在，"
                         "撤掉这条例外就回到问人。凭据/许可表那类仍然永远拒。"
                         % (act_cn, path))}
    if dom == "script":
        f = str(a.get("file") or "")
        extra = ("另外把这个目录的读也放进来：%s" % a.get("also_file")
                 if a.get("also_file") else "")
        return {"title": "以后这个脚本不问：%s" % (f.rsplit("/", 1)[-1] or f),
                "body": ("这份脚本（按内容签哈希）以后跳过内容扫描：%s\n"
                         "内容改一个字就自动失效、重新问人。%s" % (f, extra)).strip()}
    if dom == "mcp":
        return {"title": "以后这个工具不问：%s" % (a.get("tool") or ""),
                "body": ("`%s` 的 `%s` 以后不再停下来问人（参数里的路径也不再逐一问）。\n"
                         "只对这一个工具生效；别的工具照旧。"
                         % (a.get("server") or "", a.get("tool") or ""))}
    return {"title": "（范围认不出来）", "body": "这条提案没有可用的范围，采纳不了。"}
