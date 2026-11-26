#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sandbox.py（命令行）—— 看隔离有几档、容器档到底能不能用

现在有两档：
    Job Object     默认开。管进程树 / 内存 / 窗口站。**不管文件**。
    AppContainer   默认关（`sandbox.container`）。管文件：容器里的进程
                   读不到容器外的东西。**还没接进日常执行**（原因见 loop/contain.py 顶部）。

两个动作
    status   现在开的是哪一档、容器档能不能用（不跑体检，快）
    doctor   真跑一次体检：建 profile → 容器里起 whoami → 试读容器外文件 →
             试读系统文件 → 试起 python（这一项单独说，不影响这一档的结论）

为什么要有 doctor
    "我们验证过 AppContainer 可行"是一句话，人没法复核。
    `doctor` 把它变成**可重跑的四步**，每一步都有退出码和对照 ——
    换台机器、换个 Windows 版本，跑一遍就知道还成不成立。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import contain as C     # noqa: E402
from loop import isolate as ISO   # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sandbox.py", description="看隔离有几档")
    ap.add_argument("act", nargs="?", default="status", choices=("status", "doctor"))
    a = ap.parse_args(argv)

    if a.act == "status":
        print(ISO.describe() or "执行层隔离：读不到")
        print("容器档：%s" % C.enabled())
        print(C.describe())
        print()
        print("看容器档到底能不能用：python bin/sandbox.py doctor")
        return 0

    if not C.supported():
        print("这个平台没有 AppContainer（只有 Windows 有）。")
        return 2
    r = C.self_check()
    print("体检结果：" + ("通过" if r.get("ok") else "**没通过** —— " + (r.get("why") or "")))
    print()
    for s in r.get("steps") or []:
        mark = "✓" if s.get("ok") else ("·" if s.get("caveat") else "✗")
        print("  %s %-38s %s" % (mark, s.get("step"), s.get("why") or ""))
    print()
    if r.get("python_ok"):
        print("执行器（python）也能放进容器 —— 这一档可以接进日常执行了。")
    else:
        print("★ python 放不进容器：这不影响「这一档可用」，但意味着")
        print("  把隔离接进**日常执行的每一条命令**还差一步（得内嵌解释器）。")
        print("  详见 loop/contain.py 顶部「为什么还没接进执行」。")
    print()
    print("配置里打开它：harness/charter.json 的 sandbox.container = true")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
