#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cost.py —— 把 token 换成钱

现在有什么、缺什么
    已经有：每一轮 `llm` 事件里的 `model` + `prompt_tokens` / `completion_tokens`
    （`loop/loop.py` 那条 `rec("llm", ...)`）。所以"按轮拆"这件事**不用新做**。
    缺的是**单价** —— 没有价格，token 就只是一个数，人没法判断"这一轮贵不贵"。

★★ 为什么不内置一张价格表
    单价会变，而且各家按地区/时段/批量折扣差别很大。**编一个数字比没有更糟**：
    人看到"这一轮 $0.03"就会拿它做决定，而那个数字是我猜的。
    所以这里只做**机制**：`prices.json` 由人填（`bin/cost.py set`），
    没填的模型明确显示「未配单价」，不猜、不按相似模型估。

三条口径（这三条决定了数字对不对）
    ① **按 `llm` 事件逐条算**，不按总量乘一次 —— 同一会话里模型可能换过
       （降级链、自我/本我），总量乘一个单价必然是错的。
    ② **单价单位是「每 100 万 token」**（各家官网都这么标），
       所以算式是 `tokens / 1e6 * price`。混用"每 1K"会让结果差三个数量级，
       而且看起来只是个"小数位数不对"。
    ③ **缺字段不算零**：没有 `prompt_tokens` 的那条，记为「未计价」并计数，
       不当作 0 混进总和。少算和不算要分得开 —— 否则"这个月很便宜"可能是假的。

它不做什么
    不做汇率换算（填什么币种就是什么币种）、不做额度/预算拦截（那是策略，不是记账）、
    不把成本写回事件（事件是事实，价格是外部配置；混在一起会让"事实"跟着价格变）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import paths as P

UNIT = 1_000_000            # 单价按"每 100 万 token"


def path() -> Path:
    """价格文件。放数据根下 —— 它是本机配置，和项目无关。"""
    env = os.environ.get("HARNESS_PRICES")
    return Path(env) if env else (P.data_root() / ".harness" / "prices.json")


def _empty() -> dict:
    return {"version": 1, "currency": "USD", "unit": UNIT, "models": {}}


def load() -> dict:
    p = path()
    if not p.exists():
        return _empty()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        out = _empty()
        out["error"] = "prices.json 读不出来（%s）—— 现在按「没有单价」处理" % e
        return out
    if not isinstance(d, dict):
        out = _empty()
        out["error"] = "prices.json 不是一个对象，按「没有单价」处理"
        return out
    out = _empty()
    out["currency"] = str(d.get("currency") or "USD")
    out["models"] = {str(k): v for k, v in (d.get("models") or {}).items()
                     if isinstance(v, dict)}
    return out


def save(d: dict) -> dict:
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
        return {"ok": True, "path": str(p)}
    except OSError as e:
        return {"ok": False, "error": "写不进去（%s）：%s" % (p, e)}


def rate(model: str) -> dict | None:
    """这个模型的单价（每 100 万 token）。没配就是 None —— **不猜**。"""
    d = load()
    m = d["models"].get(str(model or ""))
    if not isinstance(m, dict):
        return None
    try:
        return {"in": float(m.get("in") or 0), "out": float(m.get("out") or 0),
                "currency": str(m.get("currency") or d["currency"])}
    except (TypeError, ValueError):
        return None


def set_price(model: str, price_in: float, price_out: float,
              currency: str = "") -> dict:
    d = load()
    m = str(model or "").strip()
    if not m:
        return {"ok": False, "error": "要说清是哪个模型"}
    try:
        a, b = float(price_in), float(price_out)
    except (TypeError, ValueError):
        return {"ok": False, "error": "单价要是数字（每 100 万 token）"}
    if a < 0 or b < 0:
        return {"ok": False, "error": "单价不能是负数"}
    if currency:
        d["currency"] = str(currency)
    d["models"][m] = {"in": a, "out": b}
    r = save(d)
    if not r.get("ok"):
        return r
    return {"ok": True, "note": "记下了：%s 输入 %.4g / 输出 %.4g（每 100 万 token，%s）"
                                % (m, a, b, d["currency"])}


def turn_cost(model: str, prompt_tokens, completion_tokens) -> dict:
    """一条 llm 事件值多少钱。没有单价 ⇒ `cost=None` 且 `why` 说清。"""
    r = rate(model)
    try:
        pi = int(prompt_tokens or 0)
        co = int(completion_tokens or 0)
    except (TypeError, ValueError):
        pi = co = 0
    if r is None:
        return {"cost": None, "why": "这个模型没配单价", "model": str(model or ""),
                "in": pi, "out": co}
    if pi == 0 and co == 0:
        # ★ 两个都是 0 = **这一条没有 token 数**（事件里字段是 None），
        #   不是"免费"。按 0 算进去会让合计偏低，而偏低没人会怀疑。
        return {"cost": None, "why": "这一条没有 token 数", "model": str(model or ""),
                "in": 0, "out": 0}
    cost = (pi * r["in"] + co * r["out"]) / UNIT
    return {"cost": cost, "currency": r["currency"], "model": str(model or ""),
            "in": pi, "out": co}


def _events(session_dir: Path) -> list[dict]:
    f = Path(session_dir) / "events.jsonl"
    out = []
    try:
        if not f.exists():
            return out
        for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            if isinstance(ev, dict):
                out.append(ev)
    except OSError:
        pass
    return out


def session(session_dir) -> dict:
    """一个会话花了多少：逐轮 + 合计 + 未计价的条数。"""
    d = Path(session_dir)
    rows = []
    total = 0.0
    unpriced: dict[str, int] = {}
    cur = ""
    for ev in _events(d):
        if ev.get("type") != "llm" and ev.get("ev") != "llm":
            continue
        model = str(ev.get("model") or "")
        c = turn_cost(model, ev.get("prompt_tokens"), ev.get("completion_tokens"))
        row = {"turn": ev.get("turn"), "model": model, "in": c["in"], "out": c["out"],
               "cost": c["cost"], "why": c.get("why") or ""}
        rows.append(row)
        if c["cost"] is None:
            unpriced[model or "(没写模型)"] = unpriced.get(model or "(没写模型)", 0) + 1
        else:
            total += c["cost"]
            cur = c.get("currency") or cur
    return {"ok": True, "session": d.name, "turns": len(rows), "rows": rows[-40:],
            "total": total, "currency": cur or load()["currency"],
            "unpriced": unpriced,
            "unpriced_n": sum(unpriced.values()),
            "note": ("有 %d 轮没配单价，合计里**不含**它们 —— 少算和不算要分开"
                     % sum(unpriced.values())) if unpriced else ""}


def all_sessions() -> dict:
    """所有会话的合计（"这个月花了多少"就靠这个）。"""
    runs = P.data_root() / "runs"
    rows = []
    total = 0.0
    unp = 0
    cur = load()["currency"]
    try:
        dirs = [x for x in runs.iterdir() if x.is_dir() and not x.name.startswith("_")
                and x.name not in ("locks", "sessions", "diagnose", "_trash")]
    except OSError:
        dirs = []
    for d in sorted(dirs, key=lambda x: x.name):
        s = session(d)
        if not s["turns"]:
            continue
        rows.append({"session": s["session"], "turns": s["turns"],
                     "total": s["total"], "unpriced": s["unpriced_n"]})
        total += s["total"]
        unp += s["unpriced_n"]
        cur = s["currency"] or cur
    return {"ok": True, "sessions": len(rows), "rows": rows, "total": total,
            "currency": cur, "unpriced": unp}


def describe() -> str:
    """一行（启动日志/诊断包）。"""
    d = load()
    n = len(d["models"])
    if not n:
        return "成本：还没有单价（token 在记，钱算不出来；bin/cost.py set 填一条）"
    return "成本：已配 %d 个模型的单价（%s，每 100 万 token）" % (n, d["currency"])
