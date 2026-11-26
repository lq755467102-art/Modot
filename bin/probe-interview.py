#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采访流水的取证：这一轮「你说了什么」，进流的到底是哪句话。

为什么要它（2026-10-27 用户：「这个对话发了两次」）
    采访之后的第二次启动走的是新起一轮（会话还没有可续的消息），只传 `--task`，
    没有 `--say`。于是一句话的兜底退到了任务书 —— 界面上同一句话出现两次，
    而**人真正打的回答一个字都没进流**。回头看记录，看不出自己答了什么。

判据（能被打红的那三条）
    ① 这一轮明说了（续跑带 --say）      ⇒ 必须用这句话
    ② 采访问过、也有回答，但这一轮没带 --say ⇒ 必须用**回答**，不许再用任务书
    ③ 问过但还没答（第一次启动）        ⇒ 用任务书（那时它确实只是在读任务）

用法
    python bin/probe-interview.py          # 三条判据 + 自检
    python bin/probe-interview.py -v       # 把每个用例的输入输出都打出来
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "bin" / "run-task.py"

TASK = "自检一下系统里面有没有bug，然后看看也没有什么可以优化的地方"
SAY = "顺便把结论写进 report.md"
ANSWER = "1.不用顺手改动\n2.不用写结论\n3.你自己看着办"


def load():
    spec = importlib.util.spec_from_file_location("rt_probe", str(SRC))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)       # 只定义函数，main() 在 __main__ 保护里
    return mod


def main() -> int:
    mod = load()
    fn = getattr(mod, "bubble_text", None)
    if not callable(fn):
        print("  FAIL  没找到 bubble_text —— 判据悬空，等于没验")
        return 1

    cases = [
        ("续跑带 --say：用这句话", dict(task=TASK, say=SAY, asked=True, answer=ANSWER), SAY),
        ("采访有回答、这一轮没带 --say：用**回答**（不许再用任务书）",
         dict(task=TASK, say="", asked=True, answer=ANSWER), ANSWER),
        ("问过还没答：用任务书", dict(task=TASK, say="", asked=True, answer=""), TASK),
        ("什么都没有：空（不编）", dict(task="", say="", asked=False, answer=""), ""),
    ]
    ok = 0
    for name, kw, want in cases:
        got = fn(**kw)
        good = got == want
        ok += 1 if good else 0
        print(("  PASS  " if good else "  FAIL  ") + name
              + ("" if good else "  → 实际 %r，期望 %r" % (got[:40], want[:40])))
        if "-v" in sys.argv:
            print("        %r" % (got[:80],))
    # ★ 反向证一遍：如果谁把它改回「--say 或任务书」，第 2 条必须红。
    #   这里直接算一遍那个写法，确认它和正确答案不同 —— 否则第 2 条就是在白测。
    old_style = ("" or TASK)[:2000]
    same = old_style == ANSWER
    print(("  PASS  " if not same else "  FAIL  ")
          + "旧写法（--say 或任务书）确实给出不同结果 —— 第 2 条不是白测")
    ok += 1 if not same else 0
    print("%d/%d" % (ok, len(cases) + 1))
    return 0 if ok == len(cases) + 1 else 1


if __name__ == "__main__":
    sys.exit(main())
