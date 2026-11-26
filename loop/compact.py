#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""上下文压缩 —— 让会话跑得下去，而且**丢了什么要看得出来**

为什么先做这一层
    不做就会硬死：上下文满了接口直接 400，任务当场结束，前面花的钱全废。
    这是唯一一个"不补会死"的能力（其余都是"缺了不好用"）。

先量再设计（这一步很关键，量出来跟直觉相反）
    拆一个真实会话（`bench/t2-answer`，13 条消息）：
        assistant 的 reasoning   6201 字符  **74.6%**
        工具结果                  1260 字符    15.2%
        其余（正文/任务）           846 字符    10.2%
    ⇒ **撑大上下文的不是工具输出，是模型的思考过程。**
      所以第一刀砍 reasoning，不是砍工具结果。

一个必须先验证的前提（已实测，见 tools/reasoning-echo-probe.py）
    DeepSeek 思考模式有过「reasoning_content must be passed back」的 400。
    那到底管不管**历史**消息？四种形态各跑一次真接口，**全部成功**：
        带 tool_calls + 有 reasoning ✅   带 tool_calls + 空 reasoning ✅
        纯文本     + 有 reasoning ✅   纯文本     + 无 reasoning ✅
    ⇒ 历史 reasoning 可以清。这条不实测就做压缩，很可能白做一套。

三条设计原则
    1. **惰性降级**：先做破坏性最小的。清 reasoning（信息价值最低）→ 再压工具结果 → 最后才摘要。
       现在只做前两层，因为它们**不调模型、不花钱、能砍掉九成**。
    2. **不破坏配对**：只换内容，不删消息。
       tool 消息一旦被删，前面的 assistant.tool_calls 就找不到对应结果 ⇒ 400，
       而那个 400 看起来跟"压缩"毫无关系。
    3. **丢了什么必须显形**：占位符里写明"原本多少字符、需要就重新调、别猜"，
       并且逐层记一条事件。**悄悄变短 = 我们反对的那种"无意识地省 token"。**
"""
# 主要引用方包括 `loop.py` 和 `context.py`。
from __future__ import annotations

import copy
import json

DEFAULTS = {
    "enabled": True,
    # ★ 0.60 → 0.90（2026-10-28）。0.60 是在"窗口只当 15 万"的假设下拍的，
    #   实际窗口是 1M ⇒ 40 万 token 时就开始压，压掉的全是还热着的上下文。
    #   现在只在真贴近窗口时才压 —— 它是**保命**机制，不是预算控制。
    "at": 0.90,              # 上下文用到这个比例才开始压（保命，不是预算）
    "keep_reasoning": 2,     # 最近 N 条 assistant 的 reasoning 保留
    "keep_tool": 6,          # 最近 N 条工具结果保留
    "chars_per_token": 2.5,  # 粗估系数（中英混排实测约 2.5）
    "min_tool_chars": 200,   # 工具结果短于这个不压（压了省不了多少，还白丢信息）
}

TOOL_NOTE = ("这条工具输出已被压缩以节省上下文。"
             "需要细节请按动作引用只读获取历史证据；不可为恢复记忆重放写入或执行。"
             "历史不可读时说明缺口，不要凭记忆猜。")


def cfg_with(base: dict | None = None, **over) -> dict:
    c = dict(DEFAULTS)
    if base:
        c.update({k: v for k, v in base.items() if k in DEFAULTS})
    c.update({k: v for k, v in over.items() if k in DEFAULTS})
    return c


def estimate_tokens(msgs: list) -> int:
    """粗估"下一次请求会把多少内容发出去"。

    ★ 为什么要按"会发出去的"算，而不是按"存着的"算：
      `to_dict()` 会按规则裁字段（比如带 tool_calls 的 assistant 一定发 reasoning_content，
      纯文本的只在有 reasoning 时才发）。按存着的算会高估，高了就会过早压缩。

    ★ 为什么不精确算 token：精确算要等接口返回 usage，那时候钱已经花了。
      字符是**花之前**就能算的。误差靠比例留余量吃掉（默认 0.6 就压，留 40%）。
    """
    ch = 0
    for m in msgs:
        _c = getattr(m, "content", "") or ""
        if isinstance(_c, list):
            # ★ 多模态：文本按字算，图片按**一张固定值**算（一张图大约几百到一千多 token，
            #   按 1200 估，宁可高估 —— 高了只会早点压缩，低了会撑爆接口）。
            for part in _c:
                if not isinstance(part, dict):
                    ch += len(str(part))
                elif part.get("type") == "text":
                    ch += len(part.get("text") or "")
                elif part.get("type") == "image_url":
                    ch += 1200 * 4      # 1200 token ≈ 4800 个英文字符的量级
        else:
            ch += len(_c)
        if getattr(m, "tool_calls", None):
            ch += len(json.dumps(m.tool_calls, ensure_ascii=False))
            ch += len(getattr(m, "reasoning", "") or "")   # 带 tool_calls 的一定发
        elif getattr(m, "reasoning", ""):
            ch += len(m.reasoning)                          # 纯文本的也发（有就发）
    return int(ch / max(0.1, DEFAULTS["chars_per_token"]))


def compact(msgs: list, cfg: dict | None = None) -> tuple[list, dict]:
    """按惰性降级压一遍。**返回新列表，不动传进来的那份。**

    为什么要返回新的：调用方（loop）拿到的 msgs 还要给存档用。
    原地改会让"压缩前"和"压缩后"混在一起，事后分不清哪条是原始的。
    """
    c = cfg_with(cfg)
    out = [copy.copy(m) for m in msgs]
    before = estimate_tokens(out)
    layers = []
    spill = []          # ★ 被丢掉的原文。**不扔垃圾桶，交给调用方落盘** ——
    #                      否则"压缩过什么"事后查不到，而不可查的裁剪 = 无意识地省 token。

    # ── 第 1 层：清历史 reasoning（占 74.6%，信息价值最低）──
    r_idx = [i for i, m in enumerate(out)
             if getattr(m, "role", "") == "assistant" and getattr(m, "reasoning", "")]
    keep = max(0, int(c["keep_reasoning"]))
    drop_r = r_idx[:-keep] if keep else r_idx
    saved_r = 0
    for i in drop_r:
        saved_r += len(out[i].reasoning)
        spill.append({"idx": i, "role": "assistant", "kind": "reasoning",
                      "chars": len(out[i].reasoning), "text": out[i].reasoning})
        # ★ 清成空串而不是留占位文字：带 tool_calls 的消息本来就"必须发这个字段"，
        #   留占位反而白占 token。而"这里被压过"这件事，由事件流负责记，不靠正文。
        out[i].reasoning = ""
    if drop_r:
        layers.append({"layer": "reasoning", "dropped": len(drop_r), "chars": saved_r})

    # ── 第 2 层：压历史工具结果（占 15.2%）──
    t_idx = [i for i, m in enumerate(out) if getattr(m, "role", "") == "tool"]
    keep_t = max(0, int(c["keep_tool"]))
    drop_t = [i for i in (t_idx[:-keep_t] if keep_t else t_idx)
              if len(out[i].content or "") >= c["min_tool_chars"]]
    saved_t = 0
    for i in drop_t:
        orig = out[i].content or ""
        ok = None
        evidence = {}
        try:
            j = json.loads(orig)
            if isinstance(j, dict):
                if j.get("compacted") is True:
                    continue
                ok = j.get("ok")
                original_evidence = j.get("_harnessEvidence")
                if isinstance(original_evidence,dict):
                    evidence={k:str(original_evidence[k])[:128] for k in ("actionId","runId","role") if k in original_evidence}
        except Exception:
            pass
        ph = json.dumps({"ok": ok, "compacted": True, "orig_chars": len(orig), "_harnessEvidence":evidence,
                         "note": TOOL_NOTE}, ensure_ascii=False)
        saved_t += len(orig) - len(ph)
        spill.append({"idx": i, "role": "tool", "kind": "tool_result",
                      "chars": len(orig), "text": orig})
        out[i].content = ph
    if drop_t:
        layers.append({"layer": "tool", "dropped": len(drop_t), "chars": saved_t})

    after = estimate_tokens(out)
    return out, {
        "before_tokens": before,
        "after_tokens": after,
        "saved_tokens": max(0, before - after),
        "layers": layers,
        "kept": {"reasoning": keep, "tool": keep_t},
        "n_msgs": len(out),
        "spill": spill,
    }


def should_compact(msgs: list, cap_tokens: int, cfg: dict | None = None) -> bool:
    c = cfg_with(cfg)
    if not c["enabled"] or cap_tokens <= 0:
        return False
    return estimate_tokens(msgs) > cap_tokens


def audit(msgs: list) -> dict:
    """体检用：现在上下文里各占多少。**压缩前该砍谁，靠这个说，不靠感觉。**"""
    rows = {"system": 0, "user": 0, "assistant_text": 0,
            "assistant_reasoning": 0, "tool_calls": 0, "tool_result": 0}
    for m in msgs:
        role = getattr(m, "role", "")
        if role == "system":
            rows["system"] += len(m.content or "")
        elif role == "user":
            rows["user"] += len(m.content or "")
        elif role == "tool":
            rows["tool_result"] += len(m.content or "")
        elif role == "assistant":
            rows["assistant_text"] += len(m.content or "")
            rows["assistant_reasoning"] += len(m.reasoning or "")
            rows["tool_calls"] += len(json.dumps(m.tool_calls or [], ensure_ascii=False))
    total = sum(rows.values())
    return {"chars": rows, "total_chars": total,
            "pct": {k: round(v * 100.0 / max(1, total), 1) for k, v in rows.items()}}

