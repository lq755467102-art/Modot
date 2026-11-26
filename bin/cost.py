#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cost.py（命令行）—— 配单价、看花了多少

token 早就在记了（每轮 `llm` 事件里都有 model + prompt/completion）。
缺的一直是**单价**：没有它，token 只是一个数，人判断不了"这一轮贵不贵"。

为什么这里没有内置价格表
    单价会变，各家按地区/时段/批量折扣差别很大。**编一个数字比没有更糟** ——
    人看到「这一轮 $0.03」就会拿它做决定，而那个数字是我猜的。
    所以价格由人填，没填的模型明确显示「未配单价」。

四个动作
    list                        看价格文件在哪、配了哪些模型
    set <模型> <输入价> <输出价>   填一条（单位：每 100 万 token，和官网一致）
    show [--session X] [--all]  看某个会话 / 全部会话花了多少（逐轮 + 合计）
    where                       价格文件在哪

例：
    python bin/cost.py set deepseek-chat 0.28 0.42 --currency CNY
    python bin/cost.py show --all
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import cost as C     # noqa: E402
from loop import paths as P    # noqa: E402


def _fmt(amount, currency: str) -> str:
    if amount is None:
        return "—"
    cur = {"USD": "$", "CNY": "¥", "RMB": "¥"}.get(str(currency).upper(), "")
    if cur:
        return "%s%.4f" % (cur, amount)
    return "%.4f %s" % (amount, currency)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cost.py", description="配单价 / 看花了多少")
    ap.add_argument("act", nargs="?", default="list",
                    choices=("list", "set", "show", "where"))
    ap.add_argument("model", nargs="?", default="", help="set 用：模型名")
    ap.add_argument("price_in", nargs="?", default="", help="set 用：输入单价")
    ap.add_argument("price_out", nargs="?", default="", help="set 用：输出单价")
    ap.add_argument("--currency", default="", help="币种（默认沿用文件里的）")
    ap.add_argument("--session", default="", help="show 用：会话目录或名字")
    ap.add_argument("--all", action="store_true", help="show 用：全部会话合计")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.act == "where":
        print("价格文件：%s" % C.path())
        print("（不进仓库；换机器要自己填。没填的模型显示「未配单价」，不会拿别的模型估。）")
        return 0

    if a.act == "set":
        if not (a.model and a.price_in and a.price_out):
            print("用法：python bin/cost.py set <模型> <输入价> <输出价> "
                  "[--currency CNY]\n单位是每 100 万 token（和各家官网一致）。",
                  file=sys.stderr)
            return 2
        r = C.set_price(a.model, a.price_in, a.price_out, a.currency)
        if not r.get("ok"):
            print("✗ %s" % r.get("error"), file=sys.stderr)
            return 2
        print("✓ %s" % r["note"])
        return 0

    if a.act == "show":
        if a.all:
            r = C.all_sessions()
        else:
            sid = a.session
            base = P.data_root() / "runs"
            if not sid:
                dirs = [d for d in base.iterdir() if d.is_dir()
                        and not d.name.startswith("_")] if base.exists() else []
                if not dirs:
                    print("还没有会话。", file=sys.stderr)
                    return 2
                sid = sorted(dirs, key=lambda d: d.name)[-1].name
            d = Path(sid)
            if not d.is_absolute():
                d = base / sid
            r = C.session(d)
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=1))
            return 0 if r.get("ok") else 2
        cur = r.get("currency") or "USD"
        if a.all:
            print("全部会话：%d 个，合计 %s" % (r.get("sessions", 0),
                                            _fmt(r.get("total"), cur)))
            for row in r.get("rows") or []:
                print("  %-24s %3d 轮  %s%s"
                      % (row["session"], row["turns"], _fmt(row["total"], cur),
                         ("  （%d 轮未计价）" % row["unpriced"]) if row["unpriced"] else ""))
            if r.get("unpriced"):
                print("\n★ 有 %d 轮没配单价，**不在合计里** —— 所以真实花销只会更高。"
                      % r["unpriced"])
            return 0
        print("会话 %s：%d 轮，合计 %s" % (r.get("session"), r.get("turns", 0),
                                         _fmt(r.get("total"), cur)))
        for row in (r.get("rows") or [])[-10:]:
            print("  第 %-3s 轮  %-24s 入 %6d / 出 %5d  %s%s"
                  % (row.get("turn") or "?", (row.get("model") or "")[:24],
                     row.get("in") or 0, row.get("out") or 0,
                     _fmt(row.get("cost"), cur),
                     ("  （%s）" % row["why"]) if row.get("why") else ""))
        if r.get("note"):
            print("\n★ %s" % r["note"])
        return 0

    # list
    d = C.load()
    print("价格文件：%s" % C.path())
    print("币种：%s ｜ 单位：每 %d token" % (d.get("currency"), C.UNIT))
    if d.get("error"):
        print("⚠ %s" % d["error"])
    ms = d.get("models") or {}
    if not ms:
        print("\n（还没配任何单价。token 一直在记，但没有单价就算不出钱。）")
        print("填一条：python bin/cost.py set <模型> <输入价> <输出价>")
        return 0
    print()
    for name, v in sorted(ms.items()):
        print("  %-28s 入 %-8s 出 %-8s"
              % (name, v.get("in"), v.get("out")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
