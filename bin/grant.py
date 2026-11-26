#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""grant.py —— 一次性许可的**命令行**入口（人用的，不是 agent 用的）。

什么时候用
    agent 被闸拦下来，界面上那条「允许这一次」按不了的时候：
      · 跑在服务器/终端里（`bin/run-task.py` 没有界面）；
      · 界面没开、或者你只想在终端里操作。

五个动作
    list      看现在有哪些活着的许可、最近批过/用过/撤过什么
    allow     批一张（默认"只这一次"；`--ttl 600` 换成 10 分钟）
    never     批这一张 **+ 记一条「以后这类都别问」的提案**（等于界面第三个按钮）
    deny      不给（同时撤掉之前给过的同一步）
    revoke    撤销（和 deny 的区别：deny 是"这次不该做"，revoke 是"之前批错了"）

为什么它必须有
    "停下来问"这个设计只有在**人真能回答**的时候才成立。
    如果回答方式只有"改 JSON 规则文件"，那它实际等于"没这条路" ——
    而 agent 那边看到的永远是"这事我做不到"。

★ 它不提供"永久允许"。要长期允许有两条路，都不在这一层留残留：
  ① `never`：批这一步 + 记一条待采纳的「以后这类都别问」（**采纳才生效**，
     范围由命中的规则算出来，随时 `bin/rules.py revoke` 撤掉）；
  ② 直接改 `harness/sandbox_rules.json`（现在它已经在版本库里了）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loop import grants as G          # noqa: E402


def _sig_from_args(a) -> tuple[str, str, str]:
    """从参数算出 (签名, 类型, 人看的标签)。三种给法互斥。"""
    if a.sig:
        return a.sig.strip(), "", ""
    if a.cmd:
        # ★ 收的是**数组**（`--cmd cat api-token`），不是一整串再自己切：
        #   按空格切会把带空格的参数切坏，而签名必须和应用里跑的那条**一字不差**。
        parts = [str(x) for x in a.cmd]
        return G.sig_cmd(parts), "cmd", G.label_for("cmd", parts)
    if a.script:
        p = Path(a.script)
        if not p.is_absolute():
            p = Path.cwd() / p
        return G.sig_script(p), "script", G.label_for("script", p)
    if a.file:
        return G.sig_file(a.action, a.file), "file", \
            G.label_for("file", a.file, a.action)
    return "", "", ""


def _scope_from_args(a):
    """从 --cmd / --script / --file 推出「这类」的范围（不消耗任何许可）。

    ★ 为什么 `--sig` 推不出来：签名是哈希，哈希反推不回路径 —— 这不是缺功能，
      是"按内容签名"的必然结果。所以走 `never` 必须用这三者之一指认**它是什么**，
      只给签名就只能 `allow` 这一次。
    """
    from loop import sandbox_rules as SB
    if a.cmd:
        return SB.scope_for_cmd([str(x) for x in a.cmd])
    if a.script:
        p = Path(a.script)
        if not p.is_absolute():
            p = Path.cwd() / p
        return SB.scope_for_script(p)
    if a.file:
        return SB.scope_for_file(a.action, a.file)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="grant.py", description="一次性许可（人用）")
    ap.add_argument("act", nargs="?", default="list",
                    choices=("list", "allow", "never", "deny", "revoke"),
                    help="默认 list")
    ap.add_argument("--sig", default="", help="直接给签名（事件里那个 sig）")
    ap.add_argument("--cmd", nargs="+", default=None,
                    help="按命令算签名，逐段给：`--cmd cat api-token`"
                         "（要和应用里跑的那条一字不差）")
    ap.add_argument("--script", default="", help="按脚本算签名（路径；内容是哈希的一部分）")
    ap.add_argument("--file", default="", help="按路径算签名")
    ap.add_argument("--action", default="read", choices=("read", "write", "delete"),
                    help="配合 --file：哪个动作")
    ap.add_argument("--ttl", type=int, default=0,
                    help="多少秒后失效（给了就是限时许可，默认「只这一次」）")
    ap.add_argument("--why", default="", help="为什么批（会留在审计里）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    a = ap.parse_args(argv)

    if a.act == "list":
        d = G.listing()
        if a.json:
            print(json.dumps(d, ensure_ascii=False, indent=1))
            return 0
        print("许可表：%s" % d["path"])
        if not d["items"]:
            print("现在没有活着的许可。")
        for it in d["items"]:
            left = ("剩 %d 分钟" % (it["left"] // 60 + 1)) if it["mode"] == "ttl" else "用一次就没了"
            print("  · [%s] %s ｜ %s ｜ %s"
                  % (it["mode_cn"], it["label"] or it["sig"], left, it["sig"]))
        if d["recent"]:
            print("\n最近发生：")
            for r in d["recent"][:8]:
                print("  · %s %s %s" % (r.get("act"), r.get("sig", "")[:12],
                                        (r.get("label") or "")[:50]))
        return 0

    sig, kind, label = _sig_from_args(a)
    if not sig:
        print("✗ 要有 --sig，或者用 --cmd / --script / --file 让它算出来。", file=sys.stderr)
        print("  签名就是 needs_human 事件里的 `sig`（界面上也能看到）。", file=sys.stderr)
        return 2
    if not a.why.strip():
        # ★ why 必写：日后回看"谁批的、凭什么"，第一个问题就是这个。
        print("✗ 写一句 --why（为什么批）。这条会留在审计里，"
              "而审计的价值全在这一句上。", file=sys.stderr)
        return 2

    if a.act == "never":
        # ★ 和界面第三个按钮**同一条路径**（2026-10-26）：这一步立刻放行 + 记一条提案。
        #   不这么做，命令行用户只能反复 `allow`，或者去手改 JSON —— 前者是训练人无脑批，
        #   后者是绕过留底和底线校验。
        _sc = _scope_from_args(a)
        if not _sc:
            print("✗ 这一步算不出「这类」的范围。用 --cmd / --script / --file 指认它；"
                  "只给 --sig 的话只能用 allow（签名是哈希，反推不回路径）。", file=sys.stderr)
            return 2
        r = G.grant(sig, kind=kind, mode=G.ONCE, ttl=G.DEFAULT_TTL,
                    why=a.why + "（并且要求：以后这类都别问）", src="cli", label=label)
        if r.get("ok"):
            from loop import charter as CH
            from loop import proposals as PR
            from loop import ruleedit as RE
            _v = RE.validate_scope(_sc)
            if not _v.get("ok"):
                r["proposal"] = {"ok": False, "error": _v.get("error")}
            else:
                _txt = RE.describe_scope(_sc)
                r["proposal"] = PR.add(CH.rules_store(), None, "", _txt["title"],
                                       _txt["body"], why=a.why, src="cli", allow=_sc)
                r["scope"] = _sc
    elif a.act == "allow":
        mode = G.TTL if a.ttl > 0 else G.ONCE
        r = G.grant(sig, kind=kind, mode=mode, ttl=a.ttl or G.DEFAULT_TTL,
                    why=a.why, src="cli", label=label)
    elif a.act == "deny":
        r = G.deny(sig, why=a.why, src="cli", label=label)
    else:
        r = G.revoke(sig, why=a.why, src="cli")

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    elif r.get("ok"):
        print("✓ %s ｜ %s" % (r.get("note") or r.get("act") or "好了",
                             label or sig))
        if a.act in ("allow", "never"):
            print("  接着跑：被拦的那一轮要**重新发一次同样的动作**才会用到这张许可"
                  "（界面上的按钮会自动续跑）。")
        if a.act == "never":
            _p = r.get("proposal") or {}
            if _p.get("ok"):
                print("  另：已记一条待采纳的「以后这类都别问」——「%s」"
                      % (_p.get("note") or "已记下"))
                print("      它**现在还没生效**。采纳：界面「待采纳」里点一下，"
                      "或改 `harness/sandbox_rules.json`。撤：`bin/rules.py revoke <id>`。")
            else:
                print("  ⚠ 免问提案没记上：%s（这一步已经放行，只是以后还会问）"
                      % (_p.get("error") or "原因不明"))
    else:
        print("✗ %s" % (r.get("error") or "没成"), file=sys.stderr)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
