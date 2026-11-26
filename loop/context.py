#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""context.py —— 「这一轮到底把什么发出去了」，按来源拆开。

为什么要在**发送那一刻**量，不能事后再算
    事后从磁盘重算一遍，就是第二套口径：charter 注入了没有、工具死了几个、
    MCP 连上没有、压过几次 —— 每一样都要重新判断一次，而只要有一处判断不同，
    面板显示的就不是真正发出去的那一份。**人在用它做决定**，那它就得是真的。
    所以：`loop` 在调模型之前把这份构成写下来，面板只读它，不算。

为什么分成这六类
    它们对应人真正会问的问题：
      系统提示词 / 记忆注入 —— "它脑子里常驻的东西占了多少"（这是最想看的那一条）
      工具定义 / 连接器及 MCP —— "挂了这么多工具，光说明书就吃掉多少"
      技能 —— "技能是正文进上下文，还是只进名字"
      对话消息 —— "我这轮说的话、它跑出来的东西占多少"

★ 记忆为什么单列
    它物理上是塞在系统提示词里的（`with_charter` 把那一段拼进去），
    但"我的记忆把上下文吃掉了三分之一"和"系统提示词太长"是两件要分别处理的事。
    所以这里把它**从系统提示词里扣出来**单独算，两个加起来正好是系统那一段的总长。

★ MCP 为什么不用另算
    `mcp__<server>__<tool>` 本身就是注册进工具表的 schema，所以按名字前缀
    从工具定义里切出来即可 —— 绝不"再估一次"。
"""
# 主要引用方包括 `loop.py` 和 `bin/server.py`。
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import compact as CP

# (键, 人话) —— 顺序就是面板里的顺序，也是人从"常驻"到"这一轮"的阅读顺序
SEGS = (
    ("system", "系统提示词"),
    ("memory", "记忆注入"),
    ("tools", "工具定义"),
    ("mcp", "连接器及 MCP"),
    ("skills", "技能"),
    ("messages", "对话消息"),
)

FILE = "context.json"


def _cpt() -> float:
    """每 token 多少字符。**跟压缩用的是同一个系数** —— 两处各写一个，
    面板和"什么时候该压"就会给出互相矛盾的说法。"""
    try:
        return float(CP.DEFAULTS.get("chars_per_token") or 2.5)
    except (TypeError, ValueError):
        return 2.5


def _tok(chars) -> int:
    return int(int(chars or 0) / max(0.1, _cpt()))


def _schema_chars(s: dict) -> int:
    return len(json.dumps(s, ensure_ascii=False))


def _skill_call_ids(msgs) -> set:
    """哪些 tool 调用是 `load_skill`（它的返回算「技能」而不是「对话」）。

    ★ 按调用 id 认，不按内容猜：技能的正文可能长得像任何东西，
      而 `call_id` 是当时那一问一答确实对得上的那根线。
    """
    ids = set()
    for m in msgs or []:
        if getattr(m, "role", "") != "assistant":
            continue
        for c in getattr(m, "tool_calls", None) or []:
            name = (c.get("function") or {}).get("name") or c.get("name") or ""
            if name in ("load_skill", "read_skill"):
                cid = c.get("id") or ""
                if cid:
                    ids.add(cid)
    return ids


def measure(msgs, tools=None, charter_text: str = "",
            cap_tokens: int = 0) -> dict:
    """把"要发出去的那一份"拆成六类。返回的分项**加起来等于总数**。

    ★ `msgs` 要传**注入之前**的那份，`charter_text` 单独给 —— 注入是
      "多插一条消息"，不是"改系统提示词"。两件事分开，账才对得上。
    """
    msgs = list(msgs or [])
    tools = list(tools or [])

    sys_msgs = [m for m in msgs if getattr(m, "role", "") == "system"]
    other = [m for m in msgs if getattr(m, "role", "") != "system"]

    sys_chars = sum(len(getattr(m, "content", "") or "") for m in sys_msgs)
    mem_chars = len(charter_text or "")

    # 内置工具 vs MCP：按 schema 名字切，不另估
    tool_chars = sum(_schema_chars(s) for s in tools
                     if not str(s.get("function", {}).get("name", "")
                                ).startswith("mcp__"))
    mcp_chars = sum(_schema_chars(s) for s in tools
                    if str(s.get("function", {}).get("name", "")
                           ).startswith("mcp__"))

    ids = _skill_call_ids(msgs)
    skill_msgs = [m for m in other
                  if getattr(m, "role", "") == "tool"
                  and getattr(m, "tool_call_id", "") in ids]
    rest = [m for m in other if m not in skill_msgs]

    # ★ 传进来的 `msgs` 必须是**还没注入 charter 的那一份**（`loop` 保证这一点）。
    #   一开始我按"charter 拼进系统提示词"去算，从系统里减 —— 结果两处都错：
    #   `with_charter` 其实是把总纲**插成一条 user 消息**，于是记忆既被单列了一次，
    #   又被算进了「对话消息」（重复计数），而系统提示词那格永远是 0。
    #   ⇒ 只认一件事：给了 charter_text 就是注入了一条，长度按它算，其余按原消息算。
    chars = {
        "system": sys_chars,
        "memory": mem_chars,
        "tools": tool_chars,
        "mcp": mcp_chars,
    }
    # 消息类不按字符算，直接走压缩模块自己的估算（它按 `to_dict()` 的规则算"会发出去的"）
    toks = {
        "system": _tok(chars["system"]),
        "memory": _tok(chars["memory"]),
        "tools": _tok(chars["tools"]),
        "mcp": _tok(chars["mcp"]),
        "skills": int(CP.estimate_tokens(skill_msgs)),
        "messages": int(CP.estimate_tokens(rest)),
    }
    segs = [{"k": k, "cn": cn, "chars": int(chars.get(k, 0)),
             "tokens": int(toks[k])} for k, cn in SEGS]
    used = sum(s["tokens"] for s in segs)
    cap = int(cap_tokens or 0)
    for s in segs:
        s["pct"] = round(s["tokens"] * 100.0 / max(1, used), 1)
        s["of_cap"] = round(s["tokens"] * 100.0 / cap, 1) if cap else 0.0
    return {
        "segs": segs,
        "used_tokens": used,
        "budget_tokens": cap,
        "pct": round(used * 100.0 / cap, 1) if cap else 0.0,
        "chars_per_token": _cpt(),
        "est": True,     # ★ 字符估的，不是接口回来的 usage。界面必须这么写。
    }


def remaining(d: dict):
    """还剩多少百分比 = 100 - 已用（保留一位小数）。`budget_tokens` 为 0 时返回 None。

    ★ 为什么从 `used_tokens`/`budget_tokens` 现算，不读 `d["pct"]`
        `pct` 那一格是**四舍五入过**的；拿 100 去减一个已经取过整的数，误差会翻倍。
        而且调用方给的 dict 不一定带 `pct`（判据里就只给了两个 token 数），
        读不到就变成"没有剩余"——两件事都不该发生。
    ★ 为什么 0 就是 None，不是 100
        分母为 0 时"还剩多少"没有定义。硬答一个 100 会在界面上显示成"空得很"，
        而真相是"压根没设上限"——把不知道写成 100% 和写成 0% 一样是在编。
    """
    d = d or {}
    try:
        used = float(d.get("used_tokens") or 0)
        cap = float(d.get("budget_tokens") or 0)
    except (TypeError, ValueError):
        return None
    if cap <= 0:
        return None
    return round(100.0 - used * 100.0 / cap, 1)


def write(sess_dir, msgs, tools=None, charter_text: str = "",
          cap_tokens: int = 0, turn: int = 0) -> dict:
    """把这一轮的构成落到 `<会话>/context.json`（原子写）。"""
    d = measure(msgs, tools, charter_text, cap_tokens)
    d["turn"] = int(turn or 0)
    d["at"] = time.strftime("%H:%M:%S")
    d["model"] = ""
    p = Path(sess_dir) / FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        # 写不下来只是"面板没数据"，不能因此让这一轮任务失败
        pass
    return d


def read(sess_dir) -> dict:
    """读回最近一次发送时的构成。没有就直说没有，不临时编一份。"""
    p = Path(sess_dir) / FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "why": "这一轮还没发过请求（或这份记录写不下来），"
                                    "所以没有可看的构成。"}
    d["ok"] = True
    return d


def describe(d: dict) -> str:
    """一行话，给日志/命令行用。"""
    if not d or not d.get("ok", True):
        return "上下文：没有记录"
    parts = ["%s %.1f%%" % (s["cn"], s["pct"]) for s in d.get("segs") or []
             if s["pct"] >= 0.1]
    return "上下文：%.1f%%（%d/%d token，字符估）｜ %s" % (
        d.get("pct") or 0, d.get("used_tokens") or 0,
        d.get("budget_tokens") or 0, " · ".join(parts))
