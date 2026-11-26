#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""crosscheck.py —— 三问：三个角色对着同一份契约各提一句最尖的质疑

为什么需要它
    L4 没有人在场。契约是唯一的靶子，而靶子画歪了没人知道 ——
    执行者照着自己理解做，测试者照着判据验，两边都很努力，方向是错的。
    人锁那一眼能兜一部分，但**人的能力差距比人和 AI 的差距都大**：
    有人能看出判据写歪了，有人看不出来。所以不能把全部重量压在那一眼上。

它凭什么有用
    三个角色坐在三个不同的位置上，怕的东西不一样：
      路线   —— 这份契约假设了什么？那个假设不成立的话，跑完也白跑
      做歪   —— 哪条判据最容易被"做了但不对"的产出满足？那个产出长什么样
      证明   —— 如果它作弊（硬编码 / 只对样例 / 把判据挪走），哪条拦不住
    这三种质疑**不是同一个问题换个说法**，它们来自三种不同的失败经验。

★ 为什么不是"一次调用演三个角色"
      一个模型连着演三个人，会把它自己第一个想法沿用到第二、第三个上 ——
      得到的是"一个人的三种口气"，不是三个人的判断。
      所以这里是**三次独立调用**，每次只给一个视角，互相看不见。
      代价是三次串行，所以**只在 L4 启用**（前三档人在场，三问是替他多想一遍）。

★ 它产出的是「给人看的材料」，不是「给机器判的依据」
      这三个质疑不进 done.json，不改判据，不参与任何自动判定。
      人锁之前看一遍，觉得哪条扎心就去 amend 契约。
      它一旦能改契约，就等于"出题的人同时宣布题目没问题" —— 那正是要防的。
"""
# 主要引用或加载入口包括 `harness_core.py`。
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "bin"))

# 三个视角。**每个都只问一个问题，而且只问契约**（不问"这个任务能不能做"）。
VIEWS = (
    {
        "who": "路线",
        "sys": (
            "你是「规划者」，现在做的是最后一道自检：**别人来质疑你画的靶子**。\n"
            "只问一件事：这份契约**假设了什么**？那个假设如果不成立，跑完也白跑。\n"
            "\n"
            "★ 找最脆的那个假设，比如：\n"
            "  · 它默认某个文件 / 接口 / 数据已经存在，而其实没有\n"
            "  · 它默认「做完 A 就等于做完 B」，而两者不是一回事\n"
            "  · 它把某个技术判断写成了既定事实（「用 X 实现」），而 X 可能根本跑不通\n"
            "\n"
            "★ 不要说「整体方向不对，重写吧」。那种话没有任何用 —— "
            "指不到具体哪一条，人就改不了。\n"
        ),
    },
    {
        "who": "做歪",
        "sys": (
            "你是「执行者」，你天天在这种契约下干活，最清楚哪里能糊过去。\n"
            "只问一件事：这几条判据里，**哪一条最容易被「做了但不对」的产出满足**？\n"
            "\n"
            "★ 说出那个「假对的产出」长什么样，具体到能被认出来，比如：\n"
            "  · 只对着样例硬编码，换一个输入就崩\n"
            "  · 把函数改名 / 加个 try-except 让报错消失\n"
            "  · 写一个看起来像但根本没被调用的实现\n"
            "\n"
            "★ 不要提「应该怎么实现」。你是在指出漏洞，不是在领活。\n"
        ),
    },
    {
        "who": "证明",
        "sys": (
            "你是「测试者」，你的活是让「看起来做完了」露馅。\n"
            "只问一件事：如果它作弊，**哪一条判据拦不住**？给一个具体的反例。\n"
            "\n"
            "★ 反例要能被真跑，比如：\n"
            "  · 判据只验 add(2,3)==5，那就给 add(-1, 0.5) 试试\n"
            "  · 判据只查文件里有没有某个词，那就把那个词写在注释里\n"
            "  · 判据只跑一遍就绿，那就让它依赖上一次留下的状态\n"
            "\n"
            "★ 不要给「应该加一条什么判据」的完整写法。指出缺口就够 —— "
            "补判据是规划者的活，你替他写就等于既出题又判卷。\n"
        ),
    },
)

TPL = """任务原话（这个人当初要的是什么）：
{task}

契约（{total} 条判据，已经定下来了）：
{items}

{extra}

只输出一个 JSON 对象，不要别的文字：
{{"ask": "一句话：你最想拿哪一条开刀，问什么",
  "risk": "一两句：如果这个担心成立，最后会怎样（具体到能认出来）",
  "where": "针对第几条判据（写编号，没有就写 0）"}}
"""


def _extract_json(text: str) -> dict | None:
    t = (text or "").strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            pass
    return None


def _items_text(done: dict) -> str:
    L = []
    for n, it in enumerate(done.get("done") or [], 1):
        L.append("%d. %s\n   怎么验：%s\n   算过的标准：%s"
                 % (n, str(it.get("claim") or "")[:200],
                    str(it.get("check") or "")[:160],
                    str(it.get("pass") or "")[:160]))
    return "\n\n".join(L) or "（这份契约里没有判据 —— 那本身就是最大的问题）"


def ask_one(view: dict, prompt: str, provider: str) -> dict:
    from loop.provider import Msg, OpenAICompatProvider
    prov = OpenAICompatProvider(profile=provider)
    r = prov.chat([Msg("system", view["sys"]), Msg("user", prompt)], [])
    obj = _extract_json(r.text or "")
    if not isinstance(obj, dict):
        return {"who": view["who"], "ok": False,
                "reason": "没给出可解析的 JSON（%d 字）" % len(r.text or "")}
    try:
        where = int(str(obj.get("where") or 0).strip() or 0)
    except (TypeError, ValueError):
        where = 0
    return {"who": view["who"], "ok": True,
            "ask": str(obj.get("ask") or "").strip()[:300],
            "risk": str(obj.get("risk") or "").strip()[:400],
            "where": where,
            "model": getattr(prov, "model", "")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="三问：三个角色对着契约各提一句最尖的质疑")
    ap.add_argument("--contract", required=True, help="契约目录（含 done.json）")
    ap.add_argument("--task", default="", help="任务书（不给就只用契约）")
    ap.add_argument("--provider", default="review", help="用哪份模型档案")
    ap.add_argument("--dry", action="store_true", help="只打印提示词，不调模型")
    a = ap.parse_args(argv)

    cd = Path(a.contract)
    done_p = cd / "done.json" if cd.is_dir() else cd
    cd = done_p.parent
    try:
        done = json.loads(done_p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print("✗ 契约读不出来：%s（%s）" % (done_p, e), file=sys.stderr, flush=True)
        return 2

    task = ""
    if a.task:
        try:
            task = Path(a.task).read_text(encoding="utf-8", errors="replace")
        except OSError:
            task = ""
    total = len(done.get("done") or [])
    items = _items_text(done)
    intent = str(done.get("intent") or "").strip()
    extra = ("这份契约自己说的意图：%s" % intent) if intent else "（契约里没写意图）"
    # ★ 判据体检的结果一并给它（2026-10-28）：如果已经有几条被判"恒真"，
    #   三问再花力气去质疑那几条是重复；让它把火力放到体检没查出来的地方。
    probe = cd / "probe.json"
    if probe.exists():
        try:
            rows = (json.loads(probe.read_text(encoding="utf-8")) or {}).get("rows") or []
            weak = [r for r in rows
                    if str(r.get("verdict") or "") in ("恒真", "自指", "自指·内容", "自指·存在")]
            if weak:
                extra += ("\n\n（注意：下面这几条已经被**判据体检**查出是恒真的，"
                          "不用再拿它们开刀 —— 挑别的）\n" +
                          "\n".join("  · %s" % str(r.get("label") or "?")[:40] for r in weak))
        except Exception:
            pass

    prompt = TPL.format(task=task.strip()[:5000] or "（没给任务书）",
                        total=total, items=items, extra=extra)
    if a.dry:
        for v in VIEWS:
            print("=" * 60)
            print("[%s]" % v["who"])
            print(v["sys"])
        print("=" * 60)
        print(prompt)
        return 0

    out = {"by": "三问（三个角色各问一句，产出给人看的材料）",
           "contract": str(done_p), "total": total, "items": []}
    bad = 0
    for v in VIEWS:
        print("· %s 在想……" % v["who"], flush=True)
        try:
            one = ask_one(v, prompt, a.provider)
        except Exception as e:
            one = {"who": v["who"], "ok": False,
                   "reason": "%s: %s" % (type(e).__name__, str(e)[:160])}
        if not one.get("ok"):
            bad += 1
            print("  ✗ 这次没问到：%s" % one.get("reason"), flush=True)
        else:
            print("  %s" % one.get("ask", "")[:120], flush=True)
        out["items"].append(one)

    out["ok"] = (bad == 0)
    try:
        (cd / "crosscheck.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except OSError as e:
        print("（结果没写成：%s）" % e, file=sys.stderr, flush=True)

    print("\n三问 → %s" % (cd / "crosscheck.json"))
    if bad:
        print("  ⚠ 有 %d 个视角没问到 —— 那一路的质疑这次是空的，别当成「没问题」。" % bad)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
