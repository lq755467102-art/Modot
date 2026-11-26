#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让路 —— 告诉另一个委托"这条路径我不拦你了"

它是干嘛的
    拦是为了**保护正在做的工作**，不是为了抢文件。你这段做完了、或者根本不在乎，
    就该能明确说一句"你改吧"，否则对方只能等你 15 分钟心跳过期 ——
    那不是保护，是拖延。

用法
    python bin/yield.py <路径> <对方委托号> [自己的委托号]
    不给自己的委托号时取环境里的 HARNESS_JOB（agent 进程里本来就有）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loop import claims as CL          # noqa: E402


def main(argv=None) -> int:
    a = list(argv if argv is not None else sys.argv)
    if len(a) < 3:
        print("用法：python bin/yield.py <路径> <对方委托号> [自己的委托号]")
        return 2
    target, to_job = a[1], a[2]
    job = a[3] if len(a) > 3 else None
    r = CL.yield_path(target, to_job, job=job)
    if not r.get("ok"):
        print("✗ %s" % (r.get("error") or "让路失败"))
        return 1
    print("✓ %s" % r.get("note"))
    print("  路径：%s" % r.get("path"))
    print("  现在让给了：%s" % "、".join(r.get("yield_to") or []))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
