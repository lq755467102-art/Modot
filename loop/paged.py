#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读协议 —— 分页、截断、以及"读不到的 id 不出现"

它解决什么
    各处读取方式不一样：有的整文件塞进来，有的截断但不告诉截了多少，
    有的把"你没权读的东西"列成一行错误。三种都在浪费上下文，
    最后一种还会让模型去追一个它根本打不开的 id。

约定（就这三条，别扩散）
    ① 列表一律 `{items, next_offset}`：`next_offset` 为 null 表示没有了。
    ② 文本截断必须带 `chars`（原长）和 `read_pointer`（去哪接着读）。
    ③ **读不到的 id 不出现**：越权或读失败的条目直接从 items 里剔除，
      数量记进 `hidden`，而不是摆一行"无权访问"让它去点。

为什么不全仓替换
    这是一个约定 + 两个示范（`bin/server.py` 的轮次事件、`loop/tools.py`
    的 `_recent_runs`）。别的地方要用就 import，不强迫一次改完。
"""
from __future__ import annotations


def page(items, offset: int = 0, limit: int = 20, max_limit: int = 50) -> dict:
    """把一段列表切成统一形状。offset 越界返回空页，不报错。"""
    seq = list(items or [])
    try:
        off = max(0, int(offset or 0))
    except (TypeError, ValueError):
        off = 0
    try:
        lim = int(limit or 20)
    except (TypeError, ValueError):
        lim = 20
    lim = max(1, min(lim, int(max_limit or 50)))
    part = seq[off:off + lim]
    nxt = off + lim if off + lim < len(seq) else None
    return {"items": part, "offset": off, "limit": lim,
            "total": len(seq), "next_offset": nxt}


def clip(text, limit: int = 4000, hard: int = 8000, read_pointer: str = "") -> dict:
    """截断一段文本，**必须**带原长和续读入口。

    `limit` 是软上限（优先收到这里），`hard` 是硬上限（绝不超过）。
    """
    s = str(text or "")
    total = len(s)
    try:
        soft = max(1, int(limit or 4000))
    except (TypeError, ValueError):
        soft = 4000
    try:
        hard_n = max(soft, int(hard or 8000))
    except (TypeError, ValueError):
        hard_n = max(soft, 8000)
    cut = s[:hard_n]
    return {"text": cut, "chars": total,
            "truncated": total > len(cut),
            "read_pointer": (read_pointer or None) if total > len(cut) else None}


def visible(items, can_read) -> tuple[list, int]:
    """过滤掉读不到的条目。返回 (能读的, 被藏掉的条数)。

    ★ 为什么是"藏掉"而不是"标一行错误"：模型看到 id 就会去追。
      追一个它打不开的 id，既浪费一轮工具调用，也让它以为"只是暂时没读到"。
    """
    out, hidden = [], 0
    for x in items or []:
        try:
            ok = bool(can_read(x))
        except Exception:
            ok = False
        if ok:
            out.append(x)
        else:
            hidden += 1
    return out, hidden
