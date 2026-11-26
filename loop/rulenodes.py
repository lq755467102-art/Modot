#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rulenodes.py —— 规则区的**节点勾选表**：这个项目这一轮带哪几条规则。

为什么要有它（2026-10-26 用户拍板「粒度从层降到节点」）
    层只有 3 个，节点有 15 个。"这轮用不用得上"的真实单位是节点 ——
    lean 原来带一整层（含 5 个用不上的），等于用层当了一个很粗的筛子。

和习惯开关**故意不同**的两处
    ① 落点：这份表放**项目里** `<repo>/.harness/rules.json`，进版本控制。
       规则是项目级的，换机器、给同事，勾选得跟着走；习惯是跨项目的，所以放 `~/.harness/`。
    ② 默认集不同：习惯默认全开；规则按档位默认（off=空 / lean=预勾 / full=全部），
       例外按**档位分别存**——「下次打开这个档位保留上次设置」是用户的原话。

★ 例外是**相对档位**的，不是全局一份
    一份全局例外会撞上三种不同的默认集：你在 full 里关掉的，到了 lean 里可能本来就默认关着，
    于是"关两次"和"关一次"会得到不同结果，而没人能解释为什么。所以按档位分。

★ 「预勾」不写进文件，只在读的时候算
    第一次切到 lean 时给一份默认（= 原来 lean 带的那一层的节点），但**不落盘** ——
    落盘就是"读的时候替人做决定"。人一动勾选才写，从此以后以文件为准。
"""
# 主要引用方包括 `charter.py` 和 `memory_tree.py`。
from __future__ import annotations

import json
import os
from pathlib import Path

MODES = ("off", "lean", "full")

def path(root) -> Path:
    env = os.environ.get("HARNESS_RULES")
    if env:
        return Path(env)
    base = Path(root) if root else Path.cwd()
    return base / ".harness" / "rules.json"


def _empty() -> dict:
    # ★ 刻意**不存档位**（2026-10-26）：档位只有一个真源（`harness/charter.json`）。
    #   这里再存一个 `active`，就会出现"表说 lean、配置说 full、到底听谁的"——
    #   而那种错的表现是"切了没反应"，最难查。
    return {"modes": {}}


def load(root) -> dict:
    """读表。坏了就按空表走 —— 不因为一张表让整个任务跑不起来。"""
    idx = _empty()
    p = path(root)
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if isinstance(raw.get("modes"), dict):
                    idx["modes"] = {k: {"on": list(v.get("on") or []),
                                        "off": list(v.get("off") or [])}
                                    for k, v in raw["modes"].items()
                                    if isinstance(v, dict)}
    except Exception:
        pass
    return idx


def save(root, idx: dict) -> dict:
    p = path(root)
    clean = {"modes": {k: {"on": sorted(set(v.get("on") or [])),
                           "off": sorted(set(v.get("off") or []))}
                       for k, v in (idx.get("modes") or {}).items()}}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": "写不进勾选表：%s" % e}
    return {"ok": True, "path": str(p), "index": clean}


def effective(mode: str, root, all_ids: list[str],
              base_ids: list[str]) -> list[str]:
    """这个档位最终带哪些节点 = **默认集 + on − off**。顺序按 `all_ids`（稳定）。

    ★ 默认集（`base_ids`）由**调用方**算（`charter.mode_base_ids`），本模块不猜。
      第一版按档位**名字**分支（off/full/其它=lean），结果自定义档位名（x/y/z）
      全掉进 lean 那一支，带出来是空的 —— 名字不是语义，声明才是。
    """
    idx = load(root)
    exc = (idx.get("modes") or {}).get(mode) or {}
    return select_ids(all_ids, base_ids, exc)


def select_ids(all_ids, base_ids, exc):
    """Same stable default + on - off algebra for both tree adapters."""
    keep = set(base_ids)
    keep |= {x for x in (exc.get("on") or []) if x in set(all_ids)}
    keep -= {x for x in (exc.get("off") or [])}
    return [i for i in all_ids if i in keep]


def toggle(root, node_id: str, mode: str, all_ids: list[str],
           base_ids: list[str]) -> dict:
    """翻一个节点。**相对当前档位的默认集**翻，并写进这个档位自己的例外里。

    ★ 为什么不写成绝对集合：绝对集合会让"换档位"变成"猜上一份名单还成不成立"。
    """
    if node_id not in all_ids:
        return {"ok": False, "error": "没有这个节点：%s" % node_id}
    idx = load(root)
    exc = idx["modes"].setdefault(mode, {"on": [], "off": []})
    on = set(exc.get("on") or [])
    off = set(exc.get("off") or [])
    cur = node_id in effective(mode, root, all_ids, base_ids)
    want = not cur
    in_base = node_id in set(base_ids)
    # ★ 翻回到"默认状态"时**两边都不留记录**（2026-10-26）。
    #   只记例外是这个项目的老规矩：留一条没用的 `on`，人就分不清
    #   这条到底是"我特意开的"还是"它本来就在"。
    if want == in_base:
        on.discard(node_id)
        off.discard(node_id)
    elif want:
        on.add(node_id)
        off.discard(node_id)
    else:
        off.add(node_id)
        on.discard(node_id)
    exc["on"] = sorted(on)
    exc["off"] = sorted(off)
    r = save(root, idx)
    r["node"] = node_id
    r["on"] = not cur
    r["mode"] = mode
    return r


def overview(mode: str, root, all_ids: list[str],
             base_ids: list[str]) -> dict:
    """给界面用：每个节点的开关状态 + 本档带多少。"""
    keep = set(effective(mode, root, all_ids, base_ids))
    return {"mode": mode,
            "nodes": {i: (i in keep) for i in all_ids},
            "count": len(keep),
            "total": len(all_ids),
            "base": len(set(base_ids) & set(all_ids)),
            "index": load(root)}

