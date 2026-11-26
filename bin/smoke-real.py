#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""真实联调 —— 用真模型跑一轮完整循环（会联网、会花钱，所以不在 accept.py 里）

为什么单独一个文件
    `bin/accept.py` 必须是**离线可复现**的：同样的输入永远同样的结果。
    真模型有网络、有延迟、有概率差异 ⇒ 它属于"联调"，不属于"验收"。

它验什么
    1. 凭据能读（仓库外的 credentials.env）
    2. 模型能吃带 tools 的请求，并真的发起工具调用
    3. 工具结果回灌后，模型能继续（这条最容易 400，见下方"踩过的坑"）
    4. 验证器能看到执行痕迹，据此决定放不放行

用法
    python bin/smoke-real.py            # 跑一轮，退出码 0=成功
    python bin/smoke-real.py --verbose  # 连每轮消息形状一起打

★ 踩过的两个坑（都真实发生过，别重犯）
    坑 1：assistant 消息带 tool_calls 时，`content` 必须是 null，不能是 ""。
    坑 2：`reasoning_content` **空了也要发**（空字符串 ≠ 字段不存在）。
          而且**一条回复只能对应一个 assistant 消息** —— 拆成"先文字、再工具调用"两条，
          会报一个看起来毫不相干的错：
          「The reasoning_content in the thinking mode must be passed back」。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop.provider import OpenAICompatProvider          # noqa: E402
from loop.tools import default_registry                 # noqa: E402
from loop import loop as LOOP                           # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--max-turns", type=int, default=5)
    a = ap.parse_args(argv)

    try:
        prov = OpenAICompatProvider()
    except RuntimeError as e:
        print("✗ %s" % e, file=sys.stderr)
        return 2

    reg = default_registry(ROOT)

    def verify(actions):
        ran = any(x.get("act") == "tool" and x.get("tool") == "run_acceptance" for x in actions)
        return ({"ok": True, "reason": ""} if ran
                else {"ok": False, "reason": "你还没真的跑过一次验收，不能算完成"})

    task = ("查清楚本仓库现在验收过不过。必须真的跑一次验收工具，"
            "然后只根据工具返回的结果回答：通过多少条、失败多少条。")

    t0 = time.time()
    r = LOOP.run(task, prov, reg, verify, max_turns=a.max_turns,
                 system="你是一个谨慎的执行者。不确定就去查，不要凭印象回答。")
    dt = time.time() - t0

    print("模型：%s ｜ 耗时 %.1fs" % (prov.model, dt))
    print("status = %s ｜ turns = %s ｜ why = %s" % (r["status"], r["turns"], r["why"] or "-"))
    for x in r["actions"]:
        print("  %-10s %-16s ok=%s %s"
              % (x["act"], x.get("tool") or "", x.get("ok"), (x.get("reason") or "")[:70]))
    print("--- 回答 ---")
    print(r["final"] or "（空）")

    ok = (r["status"] == "success"
          and any(x.get("act") == "tool" and x.get("tool") == "run_acceptance" for x in r["actions"]))
    print("\n%s" % ("✓ 真模型联调通过" if ok else "✗ 真模型联调未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
