#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用量账本 —— 把「这次到底花了多少」算出来，而不是估出来

为什么要有它
    业务方定过两条，这一层就是兑现第二条：
      ① 开发阶段**不省** token（省了会掩盖问题）；
      ② 但现在必须**数清楚花在哪**，否则收尾时不知道该砍哪个。
    在这之前，"花了多少"只能靠翻 events.jsonl 一行行看。

一条硬规矩：**没配单价就不报钱。**
    我们踩过"编造 97%"那个坑。宁可以下三种情况分开说，也不给一个看着像真的数：
      有 usage + 有单价  → 报 token 和钱
      有 usage + 没单价  → **只报 token**，明说"没配单价，所以不算钱"
      没有 usage         → 明说"这次没拿到 usage"，不是 0

单价从哪来（和 provider 的凭据同一套命名）
    <PROFILE>_PRICE_IN / <PROFILE>_PRICE_OUT，单位是「每百万 token 多少钱」。
    没配就是没配，不去猜。
"""
# 主要引用方包括 `bin/accept.py` 和 `bin/sessions.py`。
from __future__ import annotations

import json
from pathlib import Path


def price_of(profile: str, cred: dict | None = None) -> dict | None:
    """读单价。没配全就返回 None —— 不半价、不猜。"""
    if not profile:
        return None
    env = cred or {}
    sin = env.get("%s_PRICE_IN" % profile)
    sout = env.get("%s_PRICE_OUT" % profile)
    if not sin or not sout:
        return None
    try:
        return {"in": float(sin), "out": float(sout)}
    except (TypeError, ValueError):
        return None


def summarize(session) -> dict:
    """一次会话的账。**只读事件，不改任何东西。**"""
    rows = session.events() if session is not None else []
    st = (session.load() if session is not None else {}) or {}

    turns: dict = {}
    tool_calls: dict = {}
    ctx_chars = 0
    charter_chars = 0
    charter_injections = 0
    llm_calls = 0
    no_usage = 0

    for r in rows:
        act = r.get("act")
        if act == "llm":
            llm_calls += 1
            t = r.get("turn")
            pin = r.get("prompt_tokens")
            pout = r.get("completion_tokens")
            if pin is None and pout is None:
                no_usage += 1
                continue
            slot = turns.setdefault(t, {"in": 0, "out": 0})
            slot["in"] += int(pin or 0)
            slot["out"] += int(pout or 0)
        elif act == "tool":
            n = r.get("tool") or "?"
            tool_calls[n] = tool_calls.get(n, 0) + 1
            ctx_chars += int(r.get("ctx_chars") or 0)
        elif act == "charter":
            charter_injections += 1
            charter_chars += int(r.get("chars") or 0)
        elif act == "tools_batch":
            ctx_chars += 0          # 批次事件本身不进上下文，只记事件

    pin = sum(v["in"] for v in turns.values())
    pout = sum(v["out"] for v in turns.values())
    total = pin + pout

    prof = st.get("profile") or ""
    p = price_of(prof, _load_cred(prof))
    cost = None
    if p is not None and total:
        cost = round(pin / 1e6 * p["in"] + pout / 1e6 * p["out"], 6)

    # ★ 三种情况分开说。把"没数据"写成 0，是最容易骗过自己的写法。
    if no_usage and not llm_calls:
        usage_note = "这次一次都没拿到 usage"
    elif no_usage:
        usage_note = "%d 次调用里有 %d 次没拿到 usage（那几次的消耗未计入）" % (
            llm_calls, no_usage)
    elif not llm_calls:
        usage_note = "这次没有模型调用"
    else:
        usage_note = ""

    if cost is None:
        cost_note = ("没配单价（%s_PRICE_IN / %s_PRICE_OUT），**所以不报钱**" % (prof, prof)
                     if prof else "存档里没有 profile 名，不知道按哪家的单价算")
    else:
        cost_note = "按 %s 的单价算" % prof

    return {
        "turns": len(turns),
        "llm_calls": llm_calls,
        "prompt_tokens": pin,
        "completion_tokens": pout,
        "total_tokens": total,
        "per_turn": [{"turn": t, "in": v["in"], "out": v["out"]}
                     for t, v in sorted(turns.items(), key=lambda x: (x[0] is None, x[0]))],
        "tool_calls": tool_calls,
        "ctx_chars": ctx_chars,
        "charter_chars_per_turn": (charter_chars // charter_injections
                                   if charter_injections else 0),
        "charter_injections": charter_injections,
        "cost": cost,
        "cost_note": cost_note,
        "usage_note": usage_note,
        "status": st.get("status") or "",
    }


def _load_cred(profile: str) -> dict:
    """从环境变量读（凭据文件由 run-task 在启动时载入环境）。读不到就是空。"""
    import os
    if not profile:
        return {}
    return {k: v for k, v in os.environ.items() if k.startswith(profile + "_")}


def fmt(bag: dict) -> str:
    """一段人能直接读的摘要。命令行和报告共用这一份，避免两边说法不一致。"""
    out = []
    out.append("轮次 %d ／ 模型调用 %d" % (bag["turns"], bag["llm_calls"]))
    if bag["total_tokens"]:
        out.append("token：入 %d ／ 出 %d ／ 合计 **%d**"
                   % (bag["prompt_tokens"], bag["completion_tokens"], bag["total_tokens"]))
        if bag["turns"]:
            out.append("平均每轮 %d" % (bag["total_tokens"] // max(1, bag["turns"])))
    if bag["cost"] is not None:
        out.append("花费：约 $%s（%s）" % (bag["cost"], bag["cost_note"]))
    else:
        out.append("花费：%s" % bag["cost_note"])
    if bag["usage_note"]:
        out.append("⚠ " + bag["usage_note"])
    if bag["tool_calls"]:
        out.append("工具：" + "、".join("%s×%d" % (k, v)
                                       for k, v in sorted(bag["tool_calls"].items())))
    if bag["ctx_chars"]:
        out.append("工具结果塞进上下文共 %d 字符" % bag["ctx_chars"])
    if bag["charter_injections"]:
        out.append("总纲每轮 %d 字符 × %d 轮 = %d 字符"
                   % (bag["charter_chars_per_turn"], bag["charter_injections"],
                      bag["charter_chars_per_turn"] * bag["charter_injections"]))
    return "\n".join("  " + x for x in out)


def sum_sessions(base: Path | None = None) -> dict:
    """把一个目录下所有会话加起来。用于"这一整段时间花了多少"。"""
    from .session import SESSIONS, Session

    root = Path(base) if base else SESSIONS
    if not root.exists():
        return {"sessions": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "cost": None, "with_usage": 0, "no_price": 0}
    tot = {"sessions": 0, "prompt_tokens": 0, "completion_tokens": 0,
           "total_tokens": 0, "cost": 0.0, "with_usage": 0, "no_price": 0}
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not (d / "events.jsonl").exists():
            continue
        bag = summarize(Session(d))
        tot["sessions"] += 1
        tot["prompt_tokens"] += bag["prompt_tokens"]
        tot["completion_tokens"] += bag["completion_tokens"]
        tot["total_tokens"] += bag["total_tokens"]
        if bag["total_tokens"]:
            tot["with_usage"] += 1
        if bag["cost"] is None:
            tot["no_price"] += 1
        else:
            tot["cost"] += bag["cost"]
    if tot["no_price"]:
        tot["cost"] = None          # 有一份算不出钱，总账就不能报钱
    return tot


if __name__ == "__main__":
    import sys
    from .session import SESSIONS, Session
    if len(sys.argv) > 1:
        print(fmt(summarize(Session(Path(sys.argv[1])))))
    else:
        t = sum_sessions(SESSIONS)
        print(json.dumps(t, ensure_ascii=False, indent=2))
