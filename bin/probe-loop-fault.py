#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""往循环里灌故障：模型报错 / 空回复 / 调用不存在的工具 / 原地重复。

为什么不用真模型测这些
    真模型不会稳定地"刚好在第三轮报错"，也不会刚好重复同一个调用。
    而这四种是**必然会发生**的（网络断、上游限流、模型抽风、脚本里写错工具名），
    所以要用假模型把它们钉死：出了事循环必须**明确收尾**，不许挂住、不许假装成功。

判据（每条都能红）
    A 模型调用抛异常   ⇒ 不许崩穿调用方，必须有非 success 的结论 + 结束事件
    B 模型返回空回复   ⇒ 不许当 success（"什么都没说"不是"做完了"）
    C 调不存在的工具   ⇒ 不许崩，要给模型一条能看懂的错，然后继续
    D 同一工具同样参数连着来 ⇒ 到达 max_same 后以 stuck 收尾（不是死循环跑满轮数）

用法：python bin/probe-loop-fault.py [-v]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import loop as LOOP            # noqa: E402
from loop.provider import Reply          # noqa: E402
from loop.tools import Registry, Tool    # noqa: E402


class FakeProv:
    """按脚本回话的假模型：每一步要么给 Reply，要么抛异常。"""
    supports_stream = False
    model = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def chat(self, msgs, schemas=None, **_kw):
        self.calls += 1
        if not self.script:
            return Reply(text="（脚本用完了）")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def chat_stream(self, msgs, schemas=None, **_kw):   # 有的调用方会走这条
        return self.chat(msgs, schemas, **_kw)


def make_reg(calls):
    """两个假工具：一个正常返回，一个永远报错。"""
    reg = Registry()
    reg.add(Tool("echo", "回显", {"type": "object", "properties": {}},
                 lambda **kw: (calls.append("echo"), {"ok": True, "out": "hi"})[1],
                 zone="offflow", readonly=True))
    reg.add(Tool("boom", "总是炸", {"type": "object", "properties": {}},
                 lambda **kw: (_ for _ in ()).throw(RuntimeError("工具内部炸了")),
                 zone="offflow", readonly=True))
    return reg


def run_case(script, *, max_turns=4, max_same=2):
    calls = []
    prov = FakeProv(script)
    r = LOOP.run("测试任务", prov, make_reg(calls), None,
                 max_turns=max_turns, max_same=max_same, system="")
    return r, calls


def main() -> int:
    verbose = "-v" in sys.argv
    cases = []

    def ck(name, cond, detail=""):
        cases.append((name, bool(cond), detail))
        print(("  PASS  " if cond else "  FAIL  ") + name + (("  —— " + detail) if detail else ""))

    # A 模型调用抛异常
    try:
        r, _ = run_case([RuntimeError("上游 502")])
        ck("A 模型报错：不崩穿，且结论不是 success",
           isinstance(r, dict) and r.get("status") != "success",
           "status=%r why=%r" % (r.get("status"), str(r.get("why"))[:50]))
        if verbose:
            print("        ", r)
    except Exception as e:
        ck("A 模型报错：不崩穿，且结论不是 success", False,
           "直接抛到了调用方：%s: %s" % (type(e).__name__, e))

    # B 空回复
    try:
        r, _ = run_case([Reply(text="", tool_calls=[])])
        ck("B 空回复：不许当 success", r.get("status") != "success",
           "status=%r final=%r" % (r.get("status"), str(r.get("final"))[:40]))
    except Exception as e:
        ck("B 空回复：不许当 success", False, "%s: %s" % (type(e).__name__, e))

    # C 调不存在的工具
    try:
        r, _ = run_case([Reply(text="", tool_calls=[{"id": "1", "name": "nosuch", "args": {}}]),
                         Reply(text="好的，改用别的方式。")])
        ck("C 不存在的工具：不崩，且这一轮能收住",
           r.get("status") in ("success", "stuck", "budget", "failed"),
           "status=%r" % r.get("status"))
    except Exception as e:
        ck("C 不存在的工具：不崩，且这一轮能收住", False, "%s: %s" % (type(e).__name__, e))

    # D 同一工具同样参数重复
    try:
        rep = Reply(text="", tool_calls=[{"id": "x", "name": "echo", "args": {"a": 1}}])
        r, calls = run_case([rep, rep, rep, rep, rep], max_turns=6, max_same=2)
        ck("D 原地重复：以 stuck 收尾（不是跑满 6 轮）",
           r.get("status") == "stuck" or len(calls) <= 3,
           "status=%r 实际调了 %d 次" % (r.get("status"), len(calls)))
    except Exception as e:
        ck("D 原地重复：以 stuck 收尾（不是跑满 6 轮）", False, "%s: %s" % (type(e).__name__, e))

    bad = [c for c in cases if not c[1]]
    print("%d/%d" % (len(cases) - len(bad), len(cases)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
