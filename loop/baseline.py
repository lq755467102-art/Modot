#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""baseline.py —— **底线清单**：几类事情，任何权限档都不许做。

它是什么
    一张短表：凭据与密钥、系统关键位置的写、**给自己放权的配置**、本机安全状态。
    命中就拒，**不看当前是哪一档**，也不给人"允许"这个选项。

为什么需要它（2026-10-26 用户的实际观察）
    用户最后都会选 `trusted` —— 这不该被对抗，而应该被承认。
    那么 `trusted` 的定义就得从"什么都行"改成"**不问人，但底线还在**"。
    实测发现我们原来的洞正是这个：`trusted` 档下 `read_file` 能读 `~/.ssh/id_ed25519`、
    能读凭据文件、能写 `~/.ssh/authorized_keys` —— 命令层（`runcheck` 的禁读名单）有底线，
    文件工具层没有。用户最放权的那一档，恰好是最没保护的那条路。

和"档位"的分工（不要混）
    档位回答「**要不要问人**」；底线回答「**能不能做**」。
    两件事正交 —— 这也是厂商的做法：`bypassPermissions` 只跳过审批，不关沙箱。

★ 为什么是"直接拒"而不是"问人"
    这三类事情没有"允许"的意义：
      · 人不会批准 agent 读自己的私钥；
      · 人不会批准 agent 去改"允许哪些动作"的那份配置（那是把锁交给被锁的人）。
    问了反而制造一个"手滑点允许"的机会。厂商的 `.ssh` 就是 `deny` 不是 `ask`。

★ 为什么清单必须短
    超过十条就没人读得完，等于没有。所以这里只放**能被一句话说清**的类别；
    别的东西靠"停下问人"和"沙箱规则"去管，不要往这里堆。
"""

# 主要引用方包括 `tools.py` 和 `projects.py`。

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import paths as P

# ── 一、精确路径：本机这几份"安全状态"，文件工具一律不许碰 ──
# ★ 用**绝对路径相等**而不是名字模式（2026-10-26 想清楚的）：
#   `harness/charter.json` 这个名字在别处也有（探针会在临时目录里造一份同名的），
#   按名字挡会把测试一起挡死。按"本仓库的/本数据根的那一份"挡，才挡得准。
W, WD = ("write", "delete"), ("read", "write", "delete")


# ★ 清单构建过程中失败的项（2026-10-26 自查加的）。
#   为什么必须留痕：清单**少一条**和"本来就没这条"在行为上完全一样 ——
#   都是"agent 可以碰"，没有任何一处会报错。而这是整条链上唯一"被绕了也不会响"的地方。
_BUILD_ERRORS: list[str] = []


def selfcheck() -> list[str]:
    """清单里哪些没建起来（空 = 全建好了）。启动日志/状态页要能看见。"""
    exact()  # 触发一次构建，填 _BUILD_ERRORS
    return list(_BUILD_ERRORS)


def _exact() -> dict[str, dict]:
    """精确到文件的几条。**每条带动作**（2026-10-26 想清楚的一刀）。

    ★ 为什么要分读写：
      · 凭据类（私钥、key、许可表）—— 读一次就泄露了 ⇒ 读也挡；
      · 配置类（charter / sandbox_rules / policy）—— 读它无害，而且**排查问题时正需要读**
        （不然模型不知道当前是哪一档、白名单里有什么）。挡的只是"写"，那才是给自己放权。
      一刀切"全挡"会把"我看看现在什么档"这种正常动作也堵死，而这属于用户明确说过
      不该堵的那类（学习成本 / 排查成本）。
    """
    out: dict[str, dict] = {}

    def put(p, group, why, actions):
        try:
            k = str(Path(p).resolve()).replace("\\", "/").lower()
        except (OSError, RuntimeError):
            return
        out[k] = {"group": group, "why": why, "actions": actions}

    for root in (P.app_root(), P.data_root()):
        put(
            root / "harness" / "charter.json",
            "selfpower",
            "这一轮的权限档和命令白名单就写在这儿",
            W,
        )
        put(
            root / "harness" / "sandbox_rules.json",
            "selfpower",
            "沙箱规则（谁能碰什么）",
            W,
        )
        put(root / ".harness" / "policy.json", "selfpower", "项目级禁令与放行清单", W)
        put(
            root / ".harness" / "grants.jsonl",
            "creds",
            "一次性许可表（能写就能自己批条子）",
            WD,
        )
        put(root / ".harness" / "mem-audit.jsonl", "selfpower", "记忆改动的审计表", W)
        put(root / "api-token", "creds", "本机接口的钥匙", WD)
    put(P.token_file(), "creds", "本机接口的钥匙", WD)
    from . import grants as G

    put(G.path(), "creds", "一次性许可表（能写就能自己批条子）", WD)
    try:
        from . import habits as HB

        put(
            HB.path(),
            "selfpower",
            "跨项目习惯（agent 改要走 edit_memory，不走文件工具）",
            W,
        )
    except Exception as _e:
        # ★ 不能静默（2026-10-26 自查）：静默的话，习惯文件就从保护清单里**消失**，
        #   而"消失了"和"本来没有"表现完全一样 —— agent 可以用文件工具直接改它，
        #   没有人会发现少了这一条。
        _BUILD_ERRORS.append(
            "跨项目习惯文件没进清单（%s: %s）" % (type(_e).__name__, _e)
        )
    put(
        Path.home() / ".workbuddy" / "harness" / "credentials.env",
        "creds",
        "模型的接口 key",
        WD,
    )
    return out


# ── 二、路径模式：凭据、系统关键位置 ──
# 三个键：actions（这个模式管哪几个动作）、patterns、why
PATTERNS = [
    {
        "id": "ssh",
        "actions": ("read", "write", "delete"),
        "why": "SSH 私钥/配置",
        "patterns": [r"(^|/)\.ssh(/|$)"],
    },
    {
        "id": "gnupg",
        "actions": ("read", "write", "delete"),
        "why": "GPG 私钥",
        "patterns": [r"(^|/)\.gnupg(/|$)"],
    },
    {
        "id": "keys",
        "actions": ("read", "write", "delete"),
        "why": "私钥文件",
        "patterns": [
            r"(^|/)id_(rsa|ed25519|ecdsa|dsa)(\.pub)?$",
            r"(^|/)\.(netrc|git-credentials)$",
            r"(^|/)\.aws/credentials$",
            r"(^|/)\.docker/config\.json$",
            r"(^|/)\.npmrc$",
            r"(^|/)\.pypirc$",
            r"(^|/)\.kube/config$",
        ],
    },
    {
        "id": "browser",
        "actions": ("read",),
        "why": "浏览器保存的 Cookie/密码库",
        "patterns": [
            r"/appdata/(local|roaming)/.*/(cookies|login data|web data|key4\.db|logins\.json)$"
        ],
    },
    # ★ 系统目录：**只挡写**。读要放行 —— 排查系统问题时读日志和配置是刚需，
    #   而"写系统目录"从来不是 agent 该做的事。
    {
        "id": "syswrite",
        "actions": ("write", "delete"),
        "why": "系统关键位置",
        "patterns": [
            r"^[a-z]:/windows(/|$)",
            r"^[a-z]:/program files( \(x86\))?(/|$)",
            r"/appdata/roaming/microsoft/windows/start menu/programs/startup(/|$)",
            r"(^|/)drivers/etc/hosts$",
        ],
    },
]


def _norm(p, root=None) -> str:
    """归一成"小写、正斜杠、绝对、无尾斜杠"（和沙箱规则用同一套口径）。"""
    value = str(p or "").replace("\\", "/")
    if value.lower().startswith("//?/unc/"):
        p = "//" + value[8:]
    elif value.startswith(("//?/", "//./")):
        p = value[4:]
    try:
        from . import sandbox_rules as SBR

        return SBR.norm_path(p, root)
    except Exception:
        s = str(p or "").replace("\\", "/")
        if s.startswith("~"):
            s = str(Path.home()).replace("\\", "/") + s[1:]
        try:
            s = str(Path(s).resolve())
        except (OSError, RuntimeError):
            pass
        return s.replace("\\", "/").rstrip("/").lower()


_CACHE = {"exact": None, "mtime": None}


def exact() -> dict[str, dict]:
    """精确表（懒建一次；数据根/家目录在进程内不会变）。"""
    if _CACHE["exact"] is None:
        _CACHE["exact"] = _exact()
    return _CACHE["exact"]


def hit(action: str, path, root=None, *, scoped_app_resource=False) -> dict | None:
    """这个动作 + 这个路径，踩到底线了吗。返回 {group, why, rule, path} 或 None。

    action: "read" / "write" / "delete"。认不出的动作按 **write** 处理（保守）。
    """
    a = str(action or "").strip().lower()
    if a not in ("read", "write", "delete"):
        a = "write"
    if not path:
        return None
    tgt = _norm(path, root)
    if not tgt:
        return None
    from . import authority_paths

    reason = authority_paths.protected(tgt, a, scoped_app_resource, root)
    if reason:
        return {
            "group": reason,
            "why": "应用内部记录、宿主及加载依赖需通过专用接口管理；开发宿主请用独立工作树",
            "rule": reason,
            "path": tgt,
            "action": a,
        }
    return _path_rule(a, tgt)


def read_snapshot_guard():
    """Reuse fixed policy roots while checking each freshly resolved source file."""
    from . import authority_paths, paths, sandbox_rules

    state = authority_paths.canonical(paths.data_root() / "runs")

    def protected(path):
        path = authority_paths.normalized(path)
        return path.is_relative_to(state) or bool(
            _path_rule("read", sandbox_rules.norm_path(path, resolved=True))
        )

    return protected


def _path_rule(a, tgt):
    ex = exact().get(tgt)
    if ex and a in ex.get("actions", WD):
        return {
            "group": ex["group"],
            "why": ex["why"],
            "rule": tgt,
            "path": tgt,
            "action": a,
        }
    for g in PATTERNS:
        if a not in g["actions"]:
            continue
        for pat in g["patterns"]:
            if re.search(pat, tgt, re.I):
                return {
                    "group": g["id"],
                    "why": g["why"],
                    "rule": pat,
                    "path": tgt,
                    "action": a,
                }
    return None


def text(h: dict) -> str:
    """给人和模型看的一句话。"""
    cn = {"read": "读", "write": "写", "delete": "删"}.get(h.get("action"), "碰")
    return "「%s」这类东西不能%s（%s）。它不是权限档的问题 —— trusted 也不放。" % (
        h.get("why") or h.get("group"),
        cn,
        h.get("path") or "",
    )


def regexes() -> list[str]:
    """给命令层（`runcheck.DENY_PATH`）用的**名字模式**。

    ★ 命令层只能看到参数里的字符串，没法做"绝对路径相等"，
      所以这里给出的是名字级的保守模式 —— 比文件工具那层宽一点是刻意的：
      宁可多挡一次读，也不要让 `cat ~/.ssh/id_rsa` 过去。
    """
    out: list[str] = []
    for g in PATTERNS:
        for pat in g["patterns"]:
            # ★ 带 `^[a-z]:` 锚的模式只在"完整路径"上有意义（命令层拿到的是一个个参数短串），
            #   塞进联合正则只会变成不匹配的死条目 —— 干脆不收。
            if pat.startswith("^[a-z]:"):
                continue
            out.append(pat.replace("(^|/)", r"(^|[\\/])"))
    for k in exact():
        name = k.rsplit("/", 1)[-1]
        if name:
            out.append(r"(^|[\\/])%s$" % re.escape(name))
    # 去重、保序
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def summary() -> str:
    """一行说清这张表管什么（进启动日志，也在文档里用）。"""
    _err = _BUILD_ERRORS
    return (
        "底线清单：%d 个精确文件 + %d 类路径（凭据/系统写/自我放权），"
        "**任何权限档都不放**，也没有「允许」这个选项%s"
        % (
            len(exact()),
            len(PATTERNS),
            ("　⚠ 有 %d 项没建起来：%s" % (len(_err), "；".join(_err))) if _err else "",
        )
    )


# ───────────────────── 命中计数：让"被挡了多少次"能看见 ─────────────────────
# ★ 为什么需要（2026-10-26）：底线一直是"拒了就走"，拒了几次、都是哪一类，
#   只有当场那个人知道。于是两种真问题看不见：
#     ① 某个任务其实一直在做危险的事（一直撞同一类）；
#     ② 某条规则误伤了正常动作（一直撞同一个路径，而它本来是排查要用的）。
#   两种都不该自动改任何东西 —— 数只是"该看一看了"的提示。
# ★ 为什么不放在 `hit()` 里：它是**纯查询**，探针会大量调它（一次验收几百次）。
#   计数由**真正拒绝的那一方**记（policy / 文件工具 / 命令层各一处）。
def hits_path() -> Path:
    env = os.environ.get("HARNESS_BASELINE_HITS")
    return Path(env) if env else (Path.home() / ".harness" / "baseline-hits.jsonl")


_MAX_BYTES = 512 * 1024  # 超过就压到最近 500 行（不是"清空"，是"留最近的"）


def record(h: dict, src: str = "") -> None:
    """记一次命中。**失败一律静默** —— 计数不是关键路径，不能因为它出错挡住拒绝。"""
    if not h:
        return
    from datetime import datetime

    try:
        p = hits_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "t": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "action": h.get("action"),
            "group": h.get("group"),
            "why": h.get("why"),
            "path": str(h.get("path") or "")[:300],
            "src": str(src or "")[:40],
        }
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # 压缩：太大了就只留最近 500 行（宁可丢老的计数，也不要让它无限涨）
        try:
            if p.stat().st_size > _MAX_BYTES:
                keep = p.read_text(encoding="utf-8", errors="replace").splitlines()[
                    -500:
                ]
                p.write_text("\n".join(keep) + "\n", encoding="utf-8")
        except OSError:
            pass
    except Exception:
        return


def rows() -> list[dict]:
    """整张命中表（坏行跳过）。"""
    p = hits_path()
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


def stats(days: int = 7) -> dict:
    """近 N 天命中了几次、都是哪一类。`hint` 只在"频繁"时给，且不自动改任何东西。"""
    from datetime import datetime, timedelta

    now = datetime.now()
    cut = (now - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M")
    today = now.strftime("%Y-%m-%d")
    rs = rows()
    recent = [r for r in rs if str(r.get("t") or "") >= cut]
    by_group: dict[str, int] = {}
    by_src: dict[str, int] = {}
    for r in recent:
        g = str(r.get("group") or "?")
        by_group[g] = by_group.get(g, 0) + 1
        s = str(r.get("src") or "?")
        by_src[s] = by_src.get(s, 0) + 1
    n_today = sum(1 for r in recent if str(r.get("t") or "").startswith(today))
    hint = ""
    if n_today >= 5:
        hint = (
            "今天已经命中底线 %d 次。两种可能，都值得看一眼："
            "① 这个任务确实在做危险的事；② 最近加的那条规则误伤了正常动作。"
            "（只是提示，不会自动改任何规则）" % n_today
        )
    return {
        "days": int(days),
        "total": len(recent),
        "today": n_today,
        "by_group": by_group,
        "by_src": by_src,
        "hint": hint,
        "path": str(hits_path()),
        "recent": recent[-10:],
    }


def describe_hits() -> str:
    """一行（进启动日志/状态页）。"""
    s = stats(7)
    if not s["total"]:
        return "底线命中：近 7 天 0 次"
    return "底线命中：近 7 天 %d 次（今天 %d 次，主要是「%s」）%s" % (
        s["total"],
        s["today"],
        max(s["by_group"], key=s["by_group"].get) if s["by_group"] else "?",
        ("　⚠ " + s["hint"]) if s["hint"] else "",
    )
