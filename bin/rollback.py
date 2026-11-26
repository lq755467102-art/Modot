#!/usr/bin/env python3
"""rollback.py —— 把这一轮改过的文件退回去。

★ 它解决的是哪件事
    我们能改文件，但改坏了只有一个退路：**指望有 git**。
    没 git 的目录、没提交的改动、别人拉过的分支 —— 那一条都不成立。
    这个命令用的是我们自己留的快照（`runs/<会话>/snap/`），不依赖版本库。

★ 和 `swap.py rollback` 的区别（两个都能撤，别混）
    `swap.py rollback a.py`  —— 只认 `.bak`，**一次提交内**的事，粒度是被 stage 的那组文件
    `rollback.py --to N`     —— 走会话快照，**跨多次修改**，能退到"第 N 次之前"
    两者不冲突：swap 管"刚才那次提交要不要撤"，这里管"这一路改下来要退到哪一步"。

★ 为什么默认要显式给 --session
    不给就"猜最近一个会话"很方便，但猜错了会撤掉**另一次任务**的改动 ——
    而撤回本身是改磁盘，猜错没有第二次机会。所以不给就列出来让人选。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import snapshot as SNAP          # noqa: E402


# 从约定的 `runs` 目录列出已有快照的会话，仅供操作者选择。
def _recent(limit: int = 5) -> list[Path]:
    runs = ROOT / "runs"
    if not runs.is_dir():
        return []
    ds = [d for d in runs.iterdir() if d.is_dir() and (d / "snap").is_dir()]
    ds.sort(key=lambda d: (d / "events.jsonl").stat().st_mtime
            if (d / "events.jsonl").exists() else 0, reverse=True)
    return ds[:limit]


# 只接受操作者指定的会话。相对路径先按项目根解析，再回退到当前目录。
def _pick_session(s: str) -> Path | None:
    if not s:
        print("没给 --session。要撤的是**哪一次任务**不能猜 —— 猜错了撤掉的是另一次任务的改动。")
        cand = _recent()
        if cand:
            print("\n最近改过文件的会话：")
            for d in cand:
                n = len(SNAP.index(d))
                print("  %s  （%d 次改动）" % (d.relative_to(ROOT), n))
            print("\n用法：python bin/rollback.py --session %s --last"
                  % cand[0].relative_to(ROOT))
        else:
            print("runs/ 下没有带快照的会话。")
        return None
    p = Path(s)
    if not p.is_absolute():
        p = (ROOT / s).resolve()
        if not p.exists():
            p = (Path.cwd() / s).resolve()
    if not p.exists():
        print("没有这个目录：%s" % s)
        return None
    return p


# 展示 `loop/snapshot.py` 的快照索引，标明当时是新建文件还是记录有错。
def _show(recs: list[dict]) -> None:
    if not recs:
        print("这个会话没有改动记录（可能这一轮一个文件都没改）。")
        return
    print("这个会话改过 %d 次，涉及 %d 个文件："
          % (len(recs), len({r.get("path") for r in recs})))
    for r in recs:
        mark = "" if r.get("existed") else "   ← 新建"
        bad = "   ⚠ " + r["error"] if r.get("error") else ""
        print("  #%-3s %s  %-11s %s%s%s"
              % (r.get("n"), r.get("t", ""), r.get("tool", ""),
                 r.get("path", ""), mark, bad))


# 决定快照回退的起始编号。
# 列出、预演和实际回退统一沿 `snapshot.py` 的会话目录与原编号执行。
def main() -> int:
    ap = argparse.ArgumentParser(
        description="把这一轮改过的文件退回去（用会话快照，不依赖 git）")
    ap.add_argument("--session", default="",
                    help="会话目录，例如 runs/app-20261026-101500")
    ap.add_argument("--list", action="store_true", help="只列出改过什么，不动文件")
    ap.add_argument("--last", action="store_true", help="撤销最后一次改动")
    ap.add_argument("--to", type=int, default=0, metavar="N",
                    help="撤销第 N 次及之后的全部改动")
    ap.add_argument("--all", action="store_true", help="撤销这个会话的全部改动")
    ap.add_argument("--dry", action="store_true", help="只显示会做什么，不动文件")
    ap.add_argument("--force", action="store_true", help="显式覆盖版本冲突（仍先备份）")
    a = ap.parse_args()

    sess = _pick_session(a.session)
    if sess is None:
        return 2
    recs = SNAP.index(sess)
    if a.list or (not a.last and not a.to and not a.all):
        _show(recs)
        if not a.list:
            print("\n要真撤：--last（撤最后一次）/ --to N（撤第 N 次及之后）"
                  "/ --all（全撤）。先看会做什么加 --dry。")
        return 0

    n = None
    if a.all:
        n = 1
    elif a.to:
        n = a.to
    elif not a.last:
        _show(recs)
        return 0

    out = SNAP.restore(n=n, sess_dir=sess, dry=a.dry, force=a.force)
    title = ("撤销第 %d 次及之后" % n) if n else "撤销最后一次"
    print("%s%s：" % (title, "（演练，没动文件）" if a.dry else ""))
    bad = 0
    for r in out:
        if r.get("did") == "backup":
            print("  · 撤回前的状态已存到 %s" % Path(r["path"]).name)
            continue
        if not r.get("ok"):
            bad += 1
            print("  ✗ %s —— %s" % (r.get("path"), r.get("why") or "没成"))
            continue
        did = {"restore": "恢复", "delete": "删除（那次新建的）",
               "would-restore": "会恢复", "would-delete": "会删除",
               "skip": "跳过"}.get(r.get("did"), r.get("did"))
        print("  ✓ %s %s%s" % (did, r.get("path"),
                               ("（用第 %s 次改动之前的快照）" % r["n"]) if r.get("n") else ""))
    print()
    print("撤回靠的是改之前留的原件，不是 git —— 所以它在没版本库的目录里也管用。"
          if not bad else "有 %d 处没撤成，上面写了原因。" % bad)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())

