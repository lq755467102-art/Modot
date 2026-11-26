#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tree-dup.py —— 查重复。两个来源一起看：项目规则（树）+ 跨项目习惯（习惯区）。

为什么要把两个来源放在一起看
    注入的时候，习惯区排在树前面，两块都会进上下文。所以「同一条在两边各写一份」
    的代价是**双份带宽**，而且两份会各自漂 —— 每份单看都自洽，没人发现。
    之前只看树，等于只查了一半。

三档判据（前两档是判的，第三档只是提示）
    · 跨区同名 / 同名互相包含 ⇒ **判红**（搬过去没删源，最典型的重复）
    · 同一句话出现两次（跨区或同区）⇒ **判红**
    · 正文相似度（4-gram Jaccard）≥ 阈值 ⇒ **只提示**，因为不同规则措辞相近是正常的

伪重复为什么不判
    「别用数字代替判断」和「判据要判做对没有」是同一件事的两种说法，字面相似度很低；
    而「报告失败说清哪一层」和「说自己做过什么给证据」字面很像，却是两件事。
    任何阈值都必须在漏报和误报之间二选一 ⇒ 所以只提示，判断留给人。
    结构性解法是**主题键**：同 key 只能在一个区里存在，机器判"撞没撞"即可。

用法
    python bin/tree-dup.py                       # 树 + 习惯区一起查
    python bin/tree-dup.py --no-habits           # 只看树
    python bin/tree-dup.py --habits <路径>       # 指定习惯区文件
    python bin/tree-dup.py --min 12 --no-similar
    python bin/tree-dup.py --json

退出码：0 = 无红；3 = 有跨区同名 / 真重复 / 键冲突
★ 只读：不写文件、不改记忆。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TREE = ROOT / "memory" / "tree"
KEY_RE = re.compile(r"<!--\s*key:\s*([a-z0-9][a-z0-9.\-]*)")   # 后面可能跟 " | state: xxx"
SPLIT_RE = re.compile(r"[。！？；\n]+")
STRIP = " \t*`|>—，,、；;：:！!？?（）()【】[]「」『』\"'“”‘’…"
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

GRAM = 4
SIMILAR_AT = 0.45        # 跨区比同区更松：两边内容本来就同源


def norm(s: str) -> str:
    return s.translate({ord(c): None for c in STRIP}).strip()


def sentences(text: str):
    text = COMMENT_RE.sub("", text)
    for raw in SPLIT_RE.split(text):
        n = norm(raw)
        if n:
            yield raw.strip(), n


def grams(s: str) -> set:
    s = norm(s)
    return {s[i:i + GRAM] for i in range(max(0, len(s) - GRAM + 1))} if len(s) >= GRAM else set()


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _finish(node: dict, path: Path, zone: str) -> dict:
    blob = "\n".join(node["body"])
    km = KEY_RE.search(blob)
    node["key"] = km.group(1) if km else None
    node["file"] = path.name
    node["zone"] = zone
    return node


def read_tree(directory: Path) -> list:
    """项目规则：每个 `## ` 是一个节点。"""
    out = []
    for f in sorted(directory.glob("*.md")):
        cur = None
        for i, line in enumerate(f.read_text(encoding="utf-8").split("\n"), 1):
            if line.startswith("## "):
                if cur:
                    out.append(_finish(cur, f, "树"))
                cur = {"title": line[3:].strip(), "line": i, "body": []}
            elif cur is not None:
                cur["body"].append(line)
        if cur:
            out.append(_finish(cur, f, "树"))
    return out


def read_habits(path: Path) -> list:
    """跨项目习惯：`## ` 是组，`### ` 是节点（标题前带上组名，便于对照）。"""
    if not path.is_file():
        return []
    out, group, cur = [], "", None
    for i, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if line.startswith("### "):
            if cur:
                out.append(_finish(cur, path, "习惯"))
            cur = {"title": line[4:].strip(), "line": i, "body": [], "group": group}
        elif line.startswith("## "):
            if cur:
                out.append(_finish(cur, path, "习惯"))
                cur = None
            group = line[3:].strip()
        elif cur is not None:
            cur["body"].append(line)
    if cur:
        out.append(_finish(cur, path, "习惯"))
    return out


def same_title(a: str, b: str) -> bool:
    x, y = norm(a), norm(b)
    if not x or not y:
        return False
    return x == y or x in y or y in x      # 搬动时常顺手改几个字，互相包含也算同名


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆重复检测（树 + 习惯区，只读）")
    ap.add_argument("--min", type=int, default=16, help="判真重复的最短句长，默认 16")
    ap.add_argument("--no-similar", action="store_true", help="跳过相似度提示")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tree", default=str(TREE), help="树的目录（默认 memory/tree）")
    ap.add_argument("--habits", default=str(Path.home() / ".harness" / "HABITS.md"),
                    help="习惯区文件（默认 ~/.harness/HABITS.md）")
    ap.add_argument("--no-habits", action="store_true", help="只看树")
    a = ap.parse_args()

    tree_dir = Path(a.tree)
    if not tree_dir.is_dir():
        print("✗ 找不到 %s" % tree_dir)
        return 2

    tree_nodes = read_tree(tree_dir)
    hb_nodes = [] if a.no_habits else read_habits(Path(a.habits))
    nodes = tree_nodes + hb_nodes

    # ① 同句（跨区与同区一起算）
    sent_map = {}
    for n in nodes:
        for raw, nz in sentences("\n".join(n["body"])):
            if len(nz) < a.min:
                continue
            h = hashlib.sha1(nz.encode("utf-8")).hexdigest()
            sent_map.setdefault(h, []).append((n, raw.strip()))
    dup_sent = [v for v in sent_map.values() if len(v) > 1]

    # ② 跨区同名
    cross_same = []
    for t in tree_nodes:
        for h in hb_nodes:
            if same_title(t["title"], h["title"]):
                cross_same.append({"tree": t, "habits": h})

    # ③ key 冲突（跨区也算）
    key_map = {}
    for n in nodes:
        if n["key"]:
            key_map.setdefault(n["key"], []).append(n)
    dup_keys = {k: v for k, v in key_map.items() if len(v) > 1}

    # ④ 跨区相似（提示）
    cross_sim = []
    if not a.no_similar and hb_nodes:
        for t in tree_nodes:
            gt = grams("\n".join(t["body"]))
            for h in hb_nodes:
                s = jaccard(gt, grams("\n".join(h["body"])))
                if s >= SIMILAR_AT:
                    cross_sim.append({"tree": t["title"], "habits": h["title"], "score": round(s, 2)})
        cross_sim.sort(key=lambda x: -x["score"])

    red = bool(dup_sent or cross_same or dup_keys)

    if a.json:
        print(json.dumps({
            "tree_nodes": len(tree_nodes), "habits_nodes": len(hb_nodes),
            "cross_same_title": [{"tree": x["tree"]["title"], "habits": x["habits"]["title"]} for x in cross_same],
            "dup_sentences": [[{"zone": n["zone"], "title": n["title"], "text": r} for n, r in g] for g in dup_sent],
            "dup_keys": {k: [x["title"] for x in v] for k, v in dup_keys.items()},
            "cross_similar": cross_sim,
        }, ensure_ascii=False, indent=2))
        return 3 if red else 0

    no_key = [n["title"] for n in nodes if not n["key"]]
    print("重复检测（两个来源）｜树 %d 节点 ／ 习惯区 %d 节点 ／ 其中 %d 个没有主题键"
          % (len(tree_nodes), len(hb_nodes), len(no_key)))
    print()

    print("══ 跨区同名（同一主题两处各一份 · 必须处理）══")
    if not cross_same:
        print("  ✓ 无" if hb_nodes else "  ○ 未查（习惯区没读到）")
    else:
        for x in cross_same:
            t, h = x["tree"], x["habits"]
            print("  「%s」" % t["title"][:26])
            print("    · 习惯 %s:%d（组：%s）" % (h["file"], h["line"], h.get("group") or "-"))
            print("    · 树   %s:%d" % (t["file"], t["line"]))

    print()
    print("══ 真重复（同一句出现两次 · 必须处理）══")
    if not dup_sent:
        print("  ✓ 无")
    else:
        for g in dup_sent:
            zones = {n["zone"] for n, _ in g}
            tag = "跨区" if len(zones) > 1 else "同区"
            print("  [%s] 「%s」" % (tag, g[0][1][:34]))
            for n, _ in g:
                print("    · %s ／ %s:%d" % (n["zone"], n["file"], n["line"]))

    print()
    print("══ 主题键冲突（必须处理）══")
    if not dup_keys:
        print("  ✓ 无")
    else:
        for k, v in dup_keys.items():
            print("  key %s 被 %d 处占用：%s" % (k, len(v), "、".join(x["title"][:14] for x in v)))

    if not a.no_similar and hb_nodes:
        print()
        print("══ 跨区相似（提示，不是结论）══")
        if not cross_sim:
            print("  ✓ 无")
        else:
            for s in cross_sim[:8]:
                print("  %.2f：树「%s」 ↔ 习惯「%s」" % (s["score"], s["tree"][:20], s["habits"][:20]))
            print()
            print("  ⚠ 相似不等于重复：改过措辞的同一条会落在这里，措辞相近的两件事也会。")

    print()
    if red:
        n = len(cross_same) + len(dup_sent) + len(dup_keys)
        print("⇒ %d 处能判的重复。按分工留一边：跨项目的进习惯区，本项目约束进树。" % n)
        return 3
    print("⇒ 能判的部分都干净。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
