#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""总纲的命令行 —— 拼装、算预算、报警、给取舍方案

五个动作
    build    按 harness/charter.json 选的层，从树里现拼一份；可写出到文件
    budget   它占上下文多少（分「占上下文」和「占预算」两个口径）
    check    根可达吗、选的层都在吗、超软上限了吗（超了判 5，不是判 3）
    plan     超了之后：丢哪一层能省多少、丢完剩多少
    show     打印真正会被注入的样子（含包装）

退出码
    0  正常
    5  **没坏，但要人动手**：总纲超软上限了，该丢一些层 —— 这是判断，不替人做
    3  真坏了：根找不到 / 选的层一个都不在
    4  用法错

为什么超上限不判 3
    3 在这套东西里一直表示"这份东西不可信"。而超上限是"东西没错，只是太胖"，
    需要一个人来挑，不是需要一个人来修。混成一个码，看报表的人就分不出该做什么。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import charter as CH          # noqa: E402


def _report_build(res: dict) -> None:
    if res["missing"]:
        for m in res["missing"]:
            print("× 找不到这一层：%s" % m)
    if res["root"]:
        print("根：%s" % res["root"])
    else:
        print("× 一个根都不可达，试过：")
        for t in res["tried"]:
            print("    %s" % t)
        return
    print("%-22s %8s  %s" % ("层", "字符", "文件"))
    for p in res["parts"]:
        print("%-22s %8d  %s" % (p["name"], p["chars"], "、".join(p["files"])[:46]))
    print("合计 %d 字符" % len(res["text"]))


def cmd_build(a) -> int:
    cfg = CH.load_config(Path(a.config) if a.config else None)
    res = CH.build(cfg)
    _report_build(res)
    if not res["parts"]:
        print("× 一层都没拼出来 —— 总纲会是空的，这比没有更坏（你以为有约束）")
        return 3
    b = CH.budget(cfg, len(res["text"]))
    print("占上下文 %.1f%%（上限 %d）｜占预算 %.1f%%（预算 %d）"
          % (b["pct_ctx"], b["limit"], b["pct_cap"], b["cap"]))
    if a.out:
        Path(a.out).write_text(res["text"], encoding="utf-8")
        print("已写出：%s（这是产物，别提交）" % a.out)
    return 0


def cmd_budget(a) -> int:
    cfg = CH.load_config(Path(a.config) if a.config else None)
    res = CH.build(cfg)
    b = CH.budget(cfg, len(res["text"]))
    print("上下文上限 %d ／ 总纲比例 %.0f%% ⇒ 总纲预算 %d 字符"
          % (b["limit"], b["ratio"] * 100, b["cap"]))
    print("现在用了 %d 字符 = 占上下文 %.1f%% ／ 占预算 %.1f%%"
          % (b["used"], b["pct_ctx"], b["pct_cap"]))
    print("还能再加 %d 字符" % b["left"])
    return 0


def cmd_check(a) -> int:
    cfg = CH.load_config(Path(a.config) if a.config else None)
    res = CH.build(cfg)
    bad = 0

    if not res["root"]:
        print("× 一个根都不可达：")
        for t in res["tried"]:
            print("    %s" % t)
        print("  ⇒ 这不是「总纲没配好」，是这台机器找不到记忆库。别静默跑。")
        return 3

    if res["missing"]:
        for m in res["missing"]:
            print("× 找不到这一层：%s（选了但树里没有）" % m)
        bad += len(res["missing"])
    if not res["parts"]:
        print("× 一层都没拼出来")
        return 3

    b = CH.budget(cfg, len(res["text"]))
    print("✓ 根可达 ／ %d 层都在 ／ %d 字符" % (len(res["parts"]), b["used"]))
    print("  占上下文 %.1f%% ／ 占预算 %.1f%%（%d ÷ %d）"
          % (b["pct_ctx"], b["pct_cap"], b["used"], b["cap"]))

    if b["over"]:
        print()
        print("★ 超过软上限了：%d > %d（超 %d 字符）" % (b["used"], b["cap"], b["used"] - b["cap"]))
        print("  这不是坏，是**需要你挑一下**。下面按体量给了取舍方案（plan）。")
        print("  理由：该丢哪层是判断，判断不替你做。")
        return 5

    if bad:
        return 3
    return 0


def cmd_plan(a) -> int:
    cfg = CH.load_config(Path(a.config) if a.config else None)
    res = CH.build(cfg)
    b = CH.budget(cfg, len(res["text"]))
    print("现在 %d 字符 ／ 预算 %d（超 %d）"
          % (b["used"], b["cap"], max(0, b["used"] - b["cap"])))
    if not b["over"]:
        print("没超，不需要丢。")
        return 0
    rows = CH.plan(res, cfg)
    if not rows:
        print("★ 单个层都很大，丢一层不够 —— 得考虑子层（见每层的字符数）或换更小的层")
        for p in sorted(res["parts"], key=lambda x: -x["chars"]):
            print("    %-22s %8d" % (p["name"], p["chars"]))
        return 0
    print()
    print("丢这些能回到预算内（按体量从大到小）：")
    for r in rows:
        print("  丢 %-20s 省 %7d ⇒ 剩 %7d（占上下文 %.1f%%）"
              % (r["name"], r["chars"], r["after"], r["pct_after"]))
    print()
    print("★ 这是**方案**，不是决定。丢哪层会丢掉什么，只有人知道。")
    return 0


def cmd_show(a) -> int:
    cfg = CH.load_config(Path(a.config) if a.config else None)
    res = CH.build(cfg)
    if not res["parts"]:
        print("× 拼不出东西来，先跑 check")
        return 3
    print(CH.wrap(res["text"]))
    return 0


def cmd_modes(a) -> int:
    """把每一档的**真实成本**摆出来。数字是真去树里拼一遍算的，不是估的。"""
    cfg = CH.load_config(Path(a.config) if a.config else None)
    rows = CH.mode_table(cfg)
    if not rows:
        print("× 配置里没有 modes，先看 harness/charter.json")
        return 3
    cur = CH.active_mode(cfg)
    limit = int(cfg.get("context_limit") or CH.DEFAULT_LIMIT)
    turns = a.turns

    print("%-11s %9s %9s %7s  %s" % ("模式", "每轮字符", "占上下文", "可申请", "适合"))
    for r in rows:
        print("%s%-10s %9d %8.1f%% %7s  %s"
              % ("★" if r["mode"] == cur else " ", r["mode"], r["chars"],
                 100.0 * r["chars"] / limit if limit else 0,
                 "可" if r["request"] else "不可", r["note"]))

    top = max(rows, key=lambda x: x["chars"])
    low = min(rows, key=lambda x: x["chars"])
    print()
    ratio = ("差 %d 倍" % round(top["chars"] / low["chars"])) if low["chars"] \
        else "最省的那档完全不占"
    print("按 %d 轮算：%s 要 %d 字符，%s 要 %d 字符 —— %s"
          % (turns, top["mode"], top["chars"] * turns,
             low["mode"], low["chars"] * turns, ratio))
    print("★ 每轮都注入 = 成本随轮数线性涨。轮数多的任务，档位比模型选择更影响账单。")
    print("★ 这些数字是去树里真拼一遍算出来的（%d 层文件在），不是估的。"
          % len(CH.build(cfg)["parts"]))
    return 0


def cmd_use(a) -> int:
    """切档。**升到更贵的档要显式确认** —— 这和"不可逆动作先停"是同一条道理：
    花出去的钱收不回来，所以要在花之前让人知道花多少。

    ★ 判断本身在 `loop/charter.py:switch_mode`，这里只负责把话说清楚。
      因为控制台页面也会切档，两处各写一遍迟早会漂。
    """
    p = Path(a.config) if a.config else None
    cfg = CH.load_config(p)
    rc, msg, delta = CH.switch_mode(cfg, a.mode, force=a.yes, why=a.why, path=p)
    if rc == 6:
        print("★ " + msg)
        print("  按 %d 轮估，多约 %d 字符。" % (a.turns, delta * a.turns))
        print("  这些钱花出去收不回来，所以先问一句。确认请加 --yes。")
        return 6
    print(("✓ " if rc == 0 else "× ") + msg)
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="总纲：从记忆树里选几层，每轮注入")
    sub = ap.add_subparsers(dest="cmd")
    for name, fn, hlp in (
        ("build", cmd_build, "拼一份（可 --out 写文件）"),
        ("budget", cmd_budget, "占上下文多少"),
        ("check", cmd_check, "根/层/软上限（超了判 5）"),
        ("plan", cmd_plan, "超了之后丢哪层"),
        ("show", cmd_show, "打印会被注入的样子"),
        ("modes", cmd_modes, "四档各自的真实成本"),
        ("use", cmd_use, "切档（升到更贵的档要 --yes，判 6）"),
    ):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--config", default="", help="默认 harness/charter.json")
        if name == "build":
            p.add_argument("--out", default="", help="写到哪（产物，别提交）")
        if name in ("modes", "use"):
            p.add_argument("--turns", type=int, default=7, help="按几轮估算（默认 7）")
        if name == "use":
            p.add_argument("mode", help="off / lean / full")
            p.add_argument("--yes", action="store_true", help="确认升到更贵的档")
            p.add_argument("--why", default="", help="为什么切（会留痕）")
        p.set_defaults(fn=fn)
    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 4
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
