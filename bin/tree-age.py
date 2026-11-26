#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tree-age.py —— 看记忆树里哪些节点「太久没人动」，该复核了。

为什么要有它
    树每轮都注入。节点只进不出 ⇒ 注入成本线性涨，真正该被看到的那几条被淹在里面。
    现状是：**进树有人把门**（`bin/distill.py` 机器提候选、人点头），
    **但没有任何机制让它退** —— 这一半是空的。这个工具只补这一半。

判据，以及为什么是这个判据
    「最后证据时间」目前拿不到：事件流不记「哪个节点被引用」。
    于是退一步，用**该节点最后一次被改动的时间**（git blame 到标题行）当代理。
    它不等于「被用过」，但一个 90 天没人碰过、和一个 3 天前刚改过的节点，
    可信度显然不同 —— 当「该看一眼」的信号已经够用，而且**可解释**（能指到具体提交）。

★ 只读：不写文件、不改注入、不碰 git 索引、不提交。
  真要把某个节点降级，是另一个动作（要动注入逻辑），不在本工具的职责里。

用法
    python bin/tree-age.py                  # 默认 60 天
    python bin/tree-age.py --days 90
    python bin/tree-age.py --json           # 给脚本/看板用
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TREE = ROOT / "memory" / "tree"
HEAD_RE = re.compile(r"^##\s+(.+?)\s*$")


def git(args: list) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), "-c", "core.quotepath=false", *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[:200])
    return r.stdout


def blame_times(path: Path) -> dict:
    """返回 {行号: 该行最后一次提交的 unix 时间}；未提交行记为 None。"""
    out = git(["blame", "--line-porcelain", str(path.relative_to(ROOT))])
    times, cur_time, final_no = {}, None, 0
    for line in out.split("\n"):
        if line.startswith("\t"):
            final_no += 1
            times[final_no] = cur_time
            continue
        m = re.match(r"^([0-9a-f]{40})\s+\d+\s+(\d+)(?:\s+\d+)?$", line)
        if m:
            cur_time = None if set(m.group(1)) == {"0"} else None
            final_no = int(m.group(2)) - 1
            continue
        if line.startswith("author-time "):
            try:
                cur_time = int(line.split()[1])
            except Exception:
                cur_time = None
    return times


def nodes(path: Path) -> list:
    """提取 `## 标题` 节点：[(行号, 标题)]。"""
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        m = HEAD_RE.match(line)
        if m:
            out.append((i, m.group(1)))
    return out


def collect(threshold_days: int) -> list:
    now = dt.datetime.now().timestamp()
    rows = []
    for f in sorted(TREE.glob("*.md")):
        times = blame_times(f)
        mtime = f.stat().st_mtime
        for line_no, title in nodes(f):
            t = times.get(line_no) or mtime      # 未提交的节点退回文件时间
            days = (now - t) / 86400.0
            rows.append({
                "file": f.name,
                "node": title,
                "line": line_no,
                "last": dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d"),
                "days": round(days, 1),
                "stale": days > threshold_days,
            })
    rows.sort(key=lambda r: -r["days"])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆树节点年龄审计（只读）")
    ap.add_argument("--days", type=int, default=60, help="超过多少天算「该复核」，默认 60")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args()

    if not TREE.is_dir():
        print("✗ 找不到 %s" % TREE)
        return 2
    try:
        rows = collect(a.days)
    except Exception as e:
        print("✗ 读不到 git 历史（%s）⇒ 判不了，不猜" % e)
        return 2

    if a.json:
        print(json.dumps({"threshold_days": a.days, "total": len(rows),
                          "stale": sum(1 for r in rows if r["stale"]), "nodes": rows},
                         ensure_ascii=False, indent=2))
        return 0

    stale = [r for r in rows if r["stale"]]
    print("记忆树节点年龄（阈值 %d 天）｜共 %d 个节点，超期 %d 个" % (a.days, len(rows), len(stale)))
    print()
    print("| 层 | 节点 | 最后改动 | 天数 | 判断 |")
    print("|---|---|---|---|---|")
    for r in rows:
        flag = "⚠ 该复核" if r["stale"] else "· 新鲜"
        print("| %s | %s | %s | %.0f | %s |" % (r["file"].replace(".md", ""), r["node"][:24], r["last"], r["days"], flag))

    print()
    if stale:
        print("⇒ 优先看这 %d 个：它们已经 %d 天以上没有被任何改动碰到。" % (len(stale), a.days))
        print("  注意：不动 ≠ 没用。复核的结论可能是「仍然成立，只是没变」—— 那就原样留着，")
        print("  并在下次真用到时顺手补一行证据，让它的时间往前挪。")
    else:
        print("⇒ 当前没有超期节点。这不代表机制有效，只说明**时间还没拉开**：")
        print("  树是最近建的，所有节点的改动时间都很新。等它跑够两三个月，这张表才开始有信号。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
