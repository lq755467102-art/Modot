#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tree-state.py —— 改一条记忆的「去留状态」。这是升降级的**执行端**。

三态，只此三种
    active     进每轮注入（默认；不写 state 就是它）
    reference  不进注入，内容仍在文件里，可查（降级）
    retired    不进注入，视为退役（留档，理由应写进 docs/）

★ 为什么降级不是「搬到更深的层」
    树形（层 / 子层 / 叶子）回答的是「这条属于哪」；
    state 回答的是「带不带」。用「搬到更深层」表达降级，会让**位置**同时承担
    两种含义 —— 以后没人解释得清「它为什么在 L2 而不是 L1」是内容更细，还是更不重要。

★ 为什么不做「自动升降」
    自动改 state 等于让机器替人判断「这条还重不重要」。本仓库的既有结论是：
    机器只能提候选（见 `bin/distill.py` 的开头），落笔要人。
    这个工具只负责**执行人的决定**，不负责做决定。

用法
    python bin/tree-state.py list                     看全部节点的状态
    python bin/tree-state.py list --inactive          只看非 active 的
    python bin/tree-state.py set --key tone.name-first --state reference
    python bin/tree-state.py set --key tone.name-first --state active

改完想看效果：
    python bin/charter.py budget      # 注入字符数应变化
    python bin/charter.py show        # 被降级的条目应消失
★ 只改那一行注释，不动正文、不动位置。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TREE = ROOT / "memory" / "tree"
HABITS = Path.home() / ".harness" / "HABITS.md"
STATES = ("active", "reference", "retired")
HEAD_RE = re.compile(r"^#{2,3} (.+?)\s*$")


def parse(path: Path, level: int) -> list:
    """返回 [{title, line, key, state}]。level=2 读 ## ，level=3 读 ### 。"""
    want = "#" * level + " "
    out, cur = [], None
    for i, ln in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if ln.startswith(want) and not (level == 2 and ln.startswith("### ")):
            cur = {"title": ln[len(want):].strip(), "line": i,
                   "key": None, "state": "active", "file": path}
            out.append(cur)
        elif cur and "<!--" in ln and "key:" in ln:
            cur["key_line"] = i          # ★ 要改的是**这一行**（key 注释），不是标题行
            seg = ln.split("key:", 1)[1]
            key_part, _, rest = seg.partition("|")
            cur["key"] = key_part.replace("-->", "").strip()
            cur["by"] = None
            if "state:" in rest:
                cur["state"] = rest.split("state:", 1)[1].replace("-->", "").strip()
            if "by:" in rest:
                # `by:` = 谁在保证这条规则执行：code / hook / human / none
                cur["by"] = rest.split("by:", 1)[1].replace("-->", "").strip().split()[0]
    return out


def all_nodes(include_habits: bool = True) -> list:
    nodes = []
    for f in sorted(TREE.glob("*.md")):
        nodes.extend(parse(f, 2))
    if include_habits and HABITS.is_file():
        nodes.extend(parse(HABITS, 3))
    return nodes


def do_list(a) -> int:
    nodes = all_nodes(not a.no_habits)
    if a.inactive:
        nodes = [n for n in nodes if n["state"] != "active"]
    if not nodes:
        print("（没有符合条件的节点）")
        return 0
    print("| 状态 | 执行者 | 主题键 | 标题 | 位置 |")
    print("|---|---|---|---|---|")
    for n in nodes:
        print("| %s | %s | %s | %s | %s:%d |" % (
            n["state"], n.get("by") or "—", n["key"] or "—",
            n["title"][:18], n["file"].name, n["line"]))
    bad = [n for n in nodes if n["key"] and n["state"] not in STATES]
    if bad:
        print()
        print("⚠ 有 %d 条 state 值不认识（只认 %s）：" % (len(bad), " / ".join(STATES)))
        for n in bad:
            print("   · %s → %s" % (n["title"][:20], n["state"]))
    return 0


def do_set(a) -> int:
    # noqa: R4 —— 这里改的是 **markdown 记忆文件**（HABITS.md / memory/tree/*.md），
    #   不是 Python 源码，ast.parse 不适用。等价的校验用「读回三样」代替（见下方 back/again）：
    #   行数没变、目标行确实是新内容、重新解析能读到新 state。
    #   命中数也验了：只改 key 那一行，且 raw != new_line 才算改过；没命中就原地不动。
    if a.state not in STATES:
        print("✗ state 只能是 %s" % " / ".join(STATES))
        return 2
    hits = [n for n in all_nodes(True) if n["key"] == a.key]
    if not hits:
        print("✗ 没找到主题键为 %s 的条目（先跑 list 看有哪些）" % a.key)
        return 2
    if len(hits) > 1:
        print("✗ 这个键在 %d 处出现（跨区重复），先修重复再改状态：" % len(hits))
        for n in hits:
            print("   · %s:%d" % (n["file"], n["line"]))
        return 2

    n = hits[0]
    path = n["file"]
    lines = path.read_text(encoding="utf-8").split("\n")
    at = (n.get("key_line") or n["line"]) - 1
    raw = lines[at]
    if "state:" in raw:
        head, _, tail = raw.partition("state:")
        tail = tail.replace("-->", "").strip()
        # 把 state: xxx 整段换掉，保留前后的注释符号
        new_line = head + "state: " + a.state + " -->"
        new_line = new_line.replace("  -->", " -->")
    else:
        new_line = raw.replace("-->", "| state: %s -->" % a.state).replace("|  ", "| ")
    if new_line == raw:
        print("· 已经是 %s，未改动" % a.state)
        return 0
    lines[at] = new_line
    path.write_text("\n".join(lines), encoding="utf-8")

    # ★ 写回后必须验三样：行数没变、目标行确实是新内容、重新解析能读到新状态。
    #   不验就等于「改了一半」—— 文件真坏了也会被当成成功（验收第 112 项专门查这个）。
    back = path.read_text(encoding="utf-8").split("\n")
    ok = len(back) == len(lines) and back[at] == new_line
    if ok:
        lvl = 3 if path == HABITS else 2
        again = next((x for x in parse(path, lvl) if x["key"] == a.key), None)
        ok = bool(again and again["state"] == a.state)
    if not ok:
        print("✗ 写回校验失败，文件可能已损坏。原始那一行是：")
        print("   %s" % raw)
        print("   手工恢复：把 %s 第 %d 行改回上面那一行" % (path.name, at + 1))
        return 4
    print("✅ %s → %s" % (n["key"], a.state))
    print("   %s:%d" % (path.name, at + 1))
    print("   新内容：%s" % new_line.strip())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="改记忆条目的去留状态（只动那一行注释）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="列出节点与状态")
    ls.add_argument("--inactive", action="store_true", help="只看非 active")
    ls.add_argument("--no-habits", action="store_true", help="不看习惯区")

    st = sub.add_parser("set", help="改一条的状态")
    st.add_argument("--key", required=True, help="主题键，如 tone.name-first")
    st.add_argument("--state", required=True, help="active / reference / retired")

    a = ap.parse_args()
    return {"list": do_list, "set": do_set}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
