#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""habits-migrate.py —— 把记忆树里那些"其实是习惯"的层搬到跨项目的习惯区。

为什么要有这个工具
    2026-10-26 定了三层记忆：习惯区（跨项目）/ 规则区（本项目）/ 事实区（agent 自己）。
    而现在的记忆树里混着两样东西 —— 「对话姿态」「收束与验收」这类其实是习惯，
    「守卫与判据」「记忆维护」才是这个项目的规则。
    搬完之后：树只剩项目规则（更小、更准），习惯在所有项目都能用。

★ 默认只打印，不动任何文件（`--dry` 就是默认）。
  真要动手才加 `--apply`，而且**两边都先备份**：
  树的原文件挪进 `memory/_migrated-<时间>/`，习惯区旧内容备份成 `HABITS.md.bak-<时间>`。
  理由：删记忆是不可逆的，而"事后发现搬错了"是常态。

用法
    python bin/habits-migrate.py                      # 看看会搬什么（默认）
    python bin/habits-migrate.py --layers 对话姿态,收束与验收 --apply
    python bin/habits-migrate.py --layers 对话姿态 --apply --group 说话
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import habits as HAB            # noqa: E402
from loop import tree as T                # noqa: E402


def _find_layer(mem: Path, name: str) -> Path | None:
    """层名 → 文件。`L1.对话姿态` / `对话姿态` 都认。"""
    tree = mem / "tree"
    if not tree.is_dir():
        return None
    for p in sorted(tree.glob("*.md")):
        stem = p.stem
        if stem == name or stem.endswith("." + name):
            return p
    return None


def _nodes(path: Path) -> list[dict]:
    """按 `##` 切小节。

    ★ 第一个**无标题**的块当作「这一层管什么」的引言，不搬成习惯 ——
      搬过去会变成一条没有标题的叶子，读起来莫名其妙。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    cur = {"title": "", "body": []}
    for ln in raw.replace("\r\n", "\n").splitlines():
        if ln.startswith("# "):
            continue                       # 文件一级标题不要（层名已经带了）
        if ln.startswith("## "):
            if cur["title"] or cur["body"]:
                out.append({"title": cur["title"], "body": "\n".join(cur["body"]).strip()})
            cur = {"title": ln[3:].strip(), "body": []}
        else:
            cur["body"].append(ln)
    if cur["title"] or cur["body"]:
        out.append({"title": cur["title"], "body": "\n".join(cur["body"]).strip()})
    keep = []
    for n in out:
        if not n["title"] and not keep:
            continue                       # 引言
        if n["body"] or n["title"]:
            keep.append(n)
    return keep


def _rewrite_without(path: Path, gone: set[str]) -> int:
    """把文件里**已搬走**的 `##` 小节去掉，其余原样留下。返回剩下的节点数。

    noqa: R4 —— 这里读写的是 **markdown**（记忆树的层文件），不是 Python 源码，
    所以"要不要 ast.parse"这条对它没有意义。真正的风险在别处：删错节点，
    所以上面用了 `_rewrite_without` 精确匹配标题，而不是整段替换。

    ★ 为什么不能整个删：一层里可能只有一部分是习惯，另一部分是项目规则。
      整层删掉 = 把不该搬的也一起删了。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    lines = raw.replace("\r\n", "\n").splitlines()
    head, blocks, cur = [], [], None
    for ln in lines:
        if ln.startswith("## "):
            if cur is not None:
                blocks.append(cur)
            cur = [ln]
        elif cur is None:
            head.append(ln)
        else:
            cur.append(ln)
    if cur is not None:
        blocks.append(cur)
    keep = []
    for b in blocks:
        title = b[0][3:].strip()
        if title not in gone:
            keep.append(b)
    out = head + [ln for b in keep for ln in b]
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return len(keep)


def _existing_titles(text: str) -> set[str]:
    return {lf["title"] for g in HAB.parse(text) for lf in g["leaves"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--layers", default="",
                    help="要搬的层，逗号分隔（不填就用内置建议）")
    ap.add_argument("--skip", default="",
                    help="这些标题不搬（逗号分隔）。整层搬会把项目规则混进来，看清单再挑")
    ap.add_argument("--group", default="",
                    help="全部放进这一个组（不填就按层名分组）")
    ap.add_argument("--apply", action="store_true", help="真的动手（默认只打印）")
    ap.add_argument("--repo", default=str(ROOT))
    a = ap.parse_args(argv)

    mem = Path(a.repo) / "memory"
    if not mem.is_dir():
        print("✗ 没有 memory/ 目录：%s" % mem, file=sys.stderr)
        return 2

    layers = [x.strip() for x in (a.layers or "对话姿态,收束与验收").split(",") if x.strip()]
    plan = []
    for name in layers:
        f = _find_layer(mem, name)
        if f is None:
            print("⚠ 找不到这一层：%s（跳过）" % name)
            continue
        nodes = _nodes(f)
        skip = [x.strip() for x in (a.skip or "").split(",") if x.strip()]
        if skip:
            nodes = [n for n in nodes if not any(k in n["title"] for k in skip)]
        if not nodes:
            print("⚠ %s 里没有 `##` 小节（跳过）" % f.name)
            continue
        plan.append({"layer": name, "file": f, "nodes": nodes,
                     "group": a.group or name})

    if not plan:
        print("没有可搬的内容。")
        return 1

    total = sum(len(p["nodes"]) for p in plan)
    print("=" * 62)
    print("会搬 %d 层 / %d 个习惯 → %s" % (len(plan), total, HAB.path()))
    print("=" * 62)
    for p in plan:
        print("\n[%s] → 组「%s」（原文件：%s）" % (p["layer"], p["group"], p["file"].name))
        for n in p["nodes"]:
            first = (n["body"].splitlines() or [""])[0][:52]
            print("   ### %s" % (n["title"] or "（无标题）"))
            print("       %s" % first)

    old = ""
    if HAB.path().exists():
        old = HAB.path().read_text(encoding="utf-8")
    have = _existing_titles(old)
    dup = [n["title"] for p in plan for n in p["nodes"] if n["title"] in have]
    if dup:
        print("\n⚠ 习惯区里已经有同名的 %d 条，会跳过（不重复加）：%s"
              % (len(dup), "、".join(dup[:5])))

    if not a.apply:
        print("\n（这是预览。真要动手加 --apply；动手前两边都会备份。）")
        return 0

    # ── 真动手 ──
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = mem / ("_migrated-" + ts)
    bak.mkdir(parents=True, exist_ok=True)
    for p in plan:
        shutil.copy2(p["file"], bak / p["file"].name)

    hp = HAB.path()
    if hp.exists():
        shutil.copy2(hp, hp.with_name(hp.name + ".bak-" + ts))

    # 追加到习惯区（保留原有内容，按组插入）
    groups = HAB.parse(old) if old else []
    by_name = {g["name"]: g for g in groups}
    for p in plan:
        g = by_name.get(p["group"])
        if g is None:
            g = {"name": p["group"], "intro": "", "leaves": [], "chars": 0}
            groups.append(g)
            by_name[p["group"]] = g
        for n in p["nodes"]:
            if n["title"] in have:
                continue
            g["leaves"].append({"title": n["title"], "body": n["body"],
                                "chars": len(n["title"]) + len(n["body"]) + 6})
            have.add(n["title"])
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text("# 我的习惯\n\n> 跨项目：不管在哪个仓库、哪个任务，都该这么做。\n\n"
                  + HAB._render(groups) + "\n", encoding="utf-8")

    # ★ 只删**搬走的那些节点**，不整个删文件（2026-10-26 踩过）。
    #   第一版直接 unlink，而 `--skip` 只是"不搬"，于是被跳过的节点跟着文件一起没了 ——
    #   "不该搬的那两条"当场消失。备份里还在，但这种错一旦发生在没备份的地方就找不回来。
    moved = []
    for p in plan:
        try:
            rest = _rewrite_without(p["file"], {n["title"] for n in p["nodes"]})
            if rest:
                print("↺ %s 里留下 %d 条没搬的节点（%s）"
                      % (p["file"].name, rest, "、".join(
                          n["title"] for n in _nodes(p["file"])) or "无"))
            else:
                p["file"].unlink()
                moved.append(p["file"].name)
        except OSError as e:
            print("⚠ 处理 %s 失败：%s（内容已经写进习惯区了，手动删一下）"
                  % (p["file"].name, e))
    print("\n✓ 已写进 %s" % hp)
    print("✓ 备份：%s" % bak)
    print("✓ 从树里移走：%s" % "、".join(moved))
    print("\n下一步：跑一遍 python bin/accept.py —— 树少了这些层，判据可能要跟着改。")
    print(json.dumps({"moved": moved, "habits": str(hp), "backup": str(bak)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
