#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""会话的查看入口 —— 在 UI 之前，先用命令行把"看得见"这件事做出来

三个动作
    list        有哪些会话、各自什么状态（扫磁盘，不维护索引）
    tail <id>   实时看一个会话的事件（像 tail -f，但每一行是人话）
    show <id>   看它的状态摘要（任务、轮数、总纲多大、最后说了什么）

为什么现在就用命令行做
    因为 UI 要的东西和这里要的东西**是同一份**：`events.jsonl`。
    先在这里把事件的长相定下来、用起来，将来 UI 只是换一个渲染器。
    反过来说，如果先做 UI，事件格式就会被界面的需要牵着走，
    最后变成一个只有那个界面能读的格式。

为什么不用服务、不用数据库
    和 `bin/board.py` 同一条理由：**扫真实文件，不存状态**。
    这样"我看到"和"界面上显示"永远是同一份东西。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop.session import (SESSIONS, Session, fmt_event, list_sessions,   # noqa: E402
                          timeline)
from loop import usage as US                            # noqa: E402


def _find(key: str) -> Path | None:
    """按目录名、或目录名的一部分找会话。找不到就明说，不猜。

    先试几种常见位置（相对当前目录、runs/ 下），再在 runs/sessions/ 里按名字匹配 ——
    因为既有人会传完整路径，也有人只传一个 id。
    """
    p = Path(key)
    if p.is_dir():
        return p
    for base in (ROOT / "runs", ROOT / "runs" / "sessions"):
        cand = base / key
        if cand.is_dir():
            return cand
    if not SESSIONS.exists():
        return None
    hits = [d for d in SESSIONS.iterdir() if d.is_dir() and key in d.name]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        print("× 「%s」对上 %d 个会话，写全一点：%s"
              % (key, len(hits), "、".join(sorted(d.name for d in hits)[:5])))
        return None
    return None


def cmd_list(a) -> int:
    rows = list_sessions()
    if not rows:
        print("还没有会话。跑一次带 --task 的任务就会自动建一个。")
        return 0
    print("%-16s %-10s %5s %6s  %s" % ("会话", "状态", "轮", "事件", "任务"))
    for s in rows[: a.limit]:
        task = (s.get("task") or "").replace("\n", " ")[:40]
        print("%-16s %-10s %5s %6s  %s"
              % (s.get("id") or "?", s.get("status") or "?", s.get("turns") or "-",
                 s.get("events") or 0, task or "（没记任务）"))
    if len(rows) > a.limit:
        print("…还有 %d 个（--limit 调大）" % (len(rows) - a.limit))
    print("\n★ 这些是扫 runs/sessions/ 数出来的，不是索引 —— 所以不会和实际不一致。")
    return 0


def cmd_tail(a) -> int:
    d = _find(a.key)
    if d is None:
        print("× 找不到这个会话：%s" % a.key)
        return 3
    sess = Session(d)
    seen = 0
    if not a.follow:
        for r in sess.events():
            print(fmt_event(r))
        return 0
    print("（跟着看；Ctrl+C 退出。看的是 %s）" % sess.events_path)
    try:
        while True:
            rows = sess.events()
            for r in rows[seen:]:
                print(fmt_event(r))
            seen = len(rows)
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\n（停了。会话本身没受影响）")
    return 0


def cmd_show(a) -> int:
    d = _find(a.key)
    if d is None:
        print("× 找不到这个会话：%s" % a.key)
        return 3
    sess = Session(d)
    st = sess.load()
    if not st:
        print("× 这个会话没有状态文件")
        return 3
    print("会话：%s" % (st.get("id") or d.name))
    print("状态：%s ｜ 轮数：%s" % (st.get("status"), st.get("turns")))
    print("任务：%s" % (st.get("task") or "（没记）").replace("\n", " ")[:110])
    print("消息：%d 条 ｜ 总纲：%s 字符" % (len(st.get("msgs") or []),
                                          st.get("charter_chars") or 0))
    g = st.get("charter_granted") or []
    if g:
        print("中途批准的层：%s" % "、".join(x.get("layer") or "?" for x in g))
    if st.get("why"):
        print("为什么停：%s" % st["why"])
    # ★ 账本：花了多少。没配单价就只报 token、不报钱 —— 不编一个看着像真的数。
    bag = US.summarize(sess)
    print("\n--- 用量 ---")
    print(US.fmt(bag))
    if st.get("final"):
        print("\n--- 它最后说的 ---")
        print(st["final"][:600])
    print("\n事件文件：%s（%d 条）" % (sess.events_path, len(sess.events())))
    return 0


def cmd_timeline(a) -> int:
    """把一次会话的事件和当天的平台流水，按时间排成一条线。

    ★ 为什么并读而不是合并成一个文件：两者记的不是同一个主体 ——
      钩子记"平台里的 agent 做了什么"，会话记"harness 里的模型做了什么"。
      缝成一个文件就分不清谁做的；并读 + 标来源才既看得全、又分得清。
    """
    d = _find(a.key)
    if d is None:
        print("× 找不到这个会话：%s" % a.key)
        return 3
    sess = Session(d)
    rows = timeline(sess, with_platform=not a.no_platform)
    if not rows:
        print("（这个会话还没有事件）")
        return 0
    print("%-9s %-9s %-6s %-14s %-4s %s" % ("时间", "来源", "类型", "名字", "结果", "说明"))
    for r in rows[-a.limit:] if a.limit else rows:
        ok = r.get("ok")
        mark = "—" if ok is None else ("成" if ok else "败")
        print("%-9s %-9s %-6s %-14s %-4s %s"
              % (r.get("t") or "", r.get("src") or "", r.get("kind") or "",
                 (r.get("name") or "")[:14], mark, r.get("detail") or ""))
    srcs = {}
    for r in rows:
        srcs[r.get("src") or "?"] = srcs.get(r.get("src") or "?", 0) + 1
    print("\n合计 %d 条：%s" % (len(rows),
                             "、".join("%s %d" % (k, v) for k, v in srcs.items())))
    print("★ harness = 循环里模型的动作；platform = 平台钩子记的 agent 动作。")
    print("  两者不是一个主体，**不要按时间混为一谈**，要看清是谁做的。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="看会话：列表 / 跟事件 / 看摘要")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list", help="有哪些会话")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("tail", help="跟事件（-f 持续）")
    p.add_argument("key")
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(fn=cmd_tail)

    p = sub.add_parser("show", help="状态摘要")
    p.add_argument("key")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("timeline", help="事件 + 平台流水，按时间并排（标来源）")
    p.add_argument("key")
    p.add_argument("--no-platform", action="store_true", help="只看 harness 这一侧")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(fn=cmd_timeline)

    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 4
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
