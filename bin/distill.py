#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""distill 的确认端 —— 机器提候选，人点头才进树。

    python bin/distill.py list               看有哪些待定候选
    python bin/distill.py accept <文件>      收下（全部）
    python bin/distill.py accept <文件> --only 2   只收第 2 条
    python bin/distill.py reject <文件>      丢掉

★ 为什么非要人来点这一下
    记忆树每轮都注入。写错一条，不是"多了一行没人看"，是**每一轮都被它带偏**。
    而"哪条值得留"是判断，机器给的只是复述 —— 让它自己写，等于让复述变成规矩。
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "memory" / "inbox"
TREE = ROOT / "memory" / "tree"

sys.path.insert(0, str(ROOT))
from loop.distill import LAYERS, layer_file        # noqa: E402


def _entries(path: Path) -> list[dict]:
    """从待定文件里抠出条目：## N. [层] 规则 / 为什么：…"""
    txt = path.read_text(encoding="utf-8", errors="replace")
    out = []
    cur = None
    for ln in txt.splitlines():
        m = re.match(r"^##\s*(\d+)\.\s*\[([^\]]+)\]\s*(.+)$", ln.strip())
        if m:
            cur = {"n": int(m.group(1)), "layer": m.group(2).strip(),
                   "rule": m.group(3).strip(), "why": ""}
            out.append(cur)
            continue
        if cur is not None and ln.strip().startswith("为什么："):
            cur["why"] = ln.strip()[len("为什么："):]
    return out


def _sid(path: Path) -> str:
    m = re.search(r"^#\s*待定\s*·\s*(.+)$", path.read_text(encoding="utf-8", errors="replace"),
                  re.M)
    return m.group(1).strip() if m else path.stem


def cmd_list() -> int:
    files = sorted(INBOX.glob("*.md")) if INBOX.exists() else []
    if not files:
        print("没有待定候选。当前会话记忆通过所选记忆实现采集，不会写入旧预设准则收件箱。")
        return 0
    for f in files:
        es = _entries(f)
        print("%s  （%d 条）" % (f.name, len(es)))
        for e in es:
            print("   %d. [%s] %s" % (e["n"], e["layer"], e["rule"]))
            if e["why"]:
                print("      为什么：%s" % e["why"])
    print()
    print("收：python bin/distill.py accept <文件名> [--only N]")
    return 0


def cmd_accept(path_str: str, only: list[int]) -> int:
    f = Path(path_str) if Path(path_str).exists() else INBOX / path_str
    if not f.exists():
        print("✗ 找不到待定文件：%s" % f)
        return 1
    es = [e for e in _entries(f) if not only or e["n"] in only]
    if not es:
        print("✗ 这个文件里没有可收的条目（或 --only 没对上编号）")
        return 1

    sid = _sid(f)
    today = dt.date.today().isoformat()
    wrote = 0
    for e in es:
        lf = layer_file(TREE, e["layer"])
        if lf is None:
            print("✗ 认不出层「%s」，树里没有对应文件（可选：%s）—— 跳过"
                  % (e["layer"], " / ".join(LAYERS)))
            continue
        line = "- %s（%s · 来自会话 %s）\n" % (e["rule"], today, sid)
        with open(lf, "a", encoding="utf-8") as fh:
            fh.write(line)
        print("＋ %s ← %s" % (lf.name, e["rule"][:60]))
        wrote += 1

    if wrote:
        done = INBOX / "_done"
        done.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(done / f.name))
        print("\n收下 %d 条。文件已挪到 memory/inbox/_done/" % wrote)
        print("树变了 ⇒ 下一轮任务开工前注入的就是新树，不用重启。")
    return 0 if wrote else 1


def cmd_reject(path_str: str) -> int:
    f = Path(path_str) if Path(path_str).exists() else INBOX / path_str
    if not f.exists():
        print("✗ 找不到待定文件：%s" % f)
        return 1
    rej = INBOX / "_rejected"
    rej.mkdir(parents=True, exist_ok=True)
    shutil.move(str(f), str(rej / f.name))
    print("丢掉了：%s（挪到 memory/inbox/_rejected/，要翻还在）" % f.name)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("list", "ls"):
        return cmd_list()
    if args[0] == "accept":
        if len(args) < 2:
            print("用法：python bin/distill.py accept <文件> [--only N]")
            return 1
        only: list[int] = []
        if "--only" in args:
            k = args.index("--only")
            only = [int(x) for x in re.findall(r"\d+", " ".join(args[k + 1:]))]
        return cmd_accept(args[1], only)
    if args[0] == "reject":
        if len(args) < 2:
            print("用法：python bin/distill.py reject <文件>")
            return 1
        return cmd_reject(args[1])
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
