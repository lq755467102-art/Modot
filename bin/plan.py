#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""plan.py —— 规划者：出题、说方向

三个角色里的第一个。它有三种活，按「人在不在场」分：

    起草契约（--job）      人不在场时用：把任务要求变成**能机器判定的判据**，人锁一道。
    说一句话方向（--plan） 人在场时用（L2）：这一轮干什么。**不写判据** ——
                           人在场，他会当场说"不对，先写测试"，不需要事先写死。
    挑先做哪条（--focus）  有锁死的契约时用（L4）：从"还没过的判据"里挑顺序。

为什么契约定位于"人不在场才用"
    它是**「打断」的替代品**。人在场能在第三轮说"这样不行"；人不在场，
    这句话没人说，只能提前写下来。所以 L2 用计划（人随时改），L4 用契约（没人改）。

它**不锁**契约
    起草 → 人看一遍 → 人 lock。这不是流程繁琐，是"出题的人不能同时宣布题目没问题"。
    所以这个脚本永远不会写 done.lock；锁只能由 `job.py contract lock`（人的动作）产生。

它也不写 scope
    范围是启动参数，人定。规划者连提建议都不主动写进去 —— 免得"顺手放宽一点"。

用法
    python bin/plan.py --job runs/jobs/job-xxxx          # 起草契约（不锁）
    python bin/plan.py --job runs/jobs/job-xxxx --dry     # 只看提示词，不调模型
    python bin/plan.py --job runs/jobs/job-xxxx --force   # 覆盖已起草但未锁的

    python bin/plan.py --plan  --memo <状态.json>         # 这一轮干什么（一句话）
    python bin/plan.py --focus --memo <状态.json> --contract <done.json>
                                                          # 这轮先做哪几条判据（有契约时）
退出码
    0 有产出 / 2 用法或环境错 / 3 凭据没配 / 4 模型这次没调通 /
    5 模型给的东西不合规 / 6 没给出能用的东西（要兜底）
"""
# 主要引用或加载入口包括 `harness_core.py`。
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "bin"))

from loop import paths as P                                    # noqa: E402

ROOT = P.data_root()
JOBS = ROOT / "runs" / "jobs"
# ★ 索引每次都现算，不存成常量（理由见 bin/job.py 的 `_index_path`）：
#   常量在 import 时就定了，而测试会改 JOBS。
def _index_path() -> Path:
    return JOBS.parent / "jobs-index.jsonl"


# ── 规划者的手：只读 + 试跑（2026-10-29 用户批的） ──
#
# 它原来一个工具都没有。于是它写出来的 `check` 命令**从没执行过一次就被锁了**，
# 而"这条判据能不能跑"只有真跑一次才知道。这是判据跑不起来的主要来源。
#
# ★ 但**没有写**。为什么呢：试跑发现跑不通，它唯一的处置是把这件事**说出来、
#   让执行者接手**。给了写权限，它就会顺手把代码改了 —— 那它就成了第二个执行者，
#   而且没有范围门禁、没人在看它改了什么，交接物里也就多了一个来源。
#   用户的原话：「只能试跑，哪怕跑出问题也只能让执行者接手」。
_PLAN_TOOLS = ("read_file", "list_dir", "code_search", "code_outline", "run_check")


def _plan_registry(root: Path):
    """规划者能用的那一副手。**白名单，不是黑名单** —— 新加的工具默认它没有。"""
    from loop.tools import default_registry, Registry
    real = default_registry(root)
    reg = Registry()
    for n in _PLAN_TOOLS:
        t = real.get(n)
        if t is not None:
            reg.add(t)
    return reg


def _run_with_tools(prov, sys_p: str, user_p: str, reg, max_rounds: int = 6,
                    on_step=None) -> str:
    """让规划者**边想边试**，然后交回它最后那段文字。

    ★ 为什么要这个循环：判据里那条命令是它自己写的，"能不能跑"只有跑一次知道。
      单次调用时它只能凭空想 —— 而凭空想的命令锁进契约之后，
      执行者要按一个跑不起来的标准干活。
    ★ 每个工具调用都记下来（`on_step`），最后落成 `plan-precheck.json`：
      人锁之前能看见"这条判据，它到底试过没有、跑出来什么"。
    """
    from loop.provider import Msg, assistant_tool_calls
    msgs = [Msg("system", sys_p), Msg("user", user_p)]
    tools = [reg.get(n).schema() for n in reg.names()]
    text = ""
    for _i in range(max(1, max_rounds)):
        # ★ 倒数第二轮插一句"该收口了"（2026-10-29 实测撞到）：
        #   6 轮全花在试跑上，一个字没交 —— 它试得很起劲（11 次调用），
        #   而这一层要的**产出是那份 JSON**，不是"它试了多少次"。
        #   靠 prompt 里写"试够了就交"不管用：模型不会自己数还有几轮。
        if _i == max(1, max_rounds) - 2:
            msgs.append(Msg("user",
                            "你只剩最后一轮了。**现在直接给出那个 JSON 契约，别再调工具。**\n"
                            "试跑看到的问题（哪条跑不起来、哪条现在就是红的）写进 note。\n"
                            "试了多少次不重要，交不出来等于没干。"))
        r = prov.chat(msgs, tools)
        text = (getattr(r, "text", "") or "").strip()
        calls = getattr(r, "tool_calls", None) or []
        if not calls:
            return text
        # ★ 回传要用**OpenAI 那套结构**（`type: function` + `function` 包裹），
        #   而模型回来的是扁平的 `{id,name,args}`。这个转换只能有一处 ——
        #   自己拼漏一个字段，报错是「消息缺 type」，完全指不到真正的原因。
        msgs.append(Msg("assistant", getattr(r, "text", "") or "",
                        tool_calls=assistant_tool_calls(calls, _i),
                        reasoning=getattr(r, "reasoning", "") or ""))
        for tc in calls:
            name = str(tc.get("name") or "").strip()
            args = tc.get("args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            t = reg.get(name)
            if t is None:
                res = {"ok": False, "error": "没有这个工具：%s" % name}
            else:
                try:
                    res = t.fn(**args) if isinstance(args, dict) else t.fn()
                except Exception as e:
                    res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            if on_step is not None:
                try:
                    on_step(name, args, res)
                except Exception:
                    pass
            _out = json.dumps(res, ensure_ascii=False)
            msgs.append(Msg("tool", _out[:4000], tool_call_id=str(tc.get("id") or "")))
    return text


SYS = (
    "你是「规划者」。你的活只有一件：把一段任务要求，变成**能机器判定的判据**。\n"
    "你不写代码，也不决定怎么做 —— 你决定的是「凭什么说这件事做完了」。\n"
    "★ 判据必须能真跑：每条都要有一个命令，在这个工作目录里执行，"
    "退出码或输出能直接说明它成没成。\n"
    "★ 判据要判「做对没有」，不是「做了多少」。"
    "「文件存在」很弱；「跑起来结果等于 5」才算数。\n"
    "★ 判不出来的需求（好看、合理、优雅）**不要写成判据** —— "
    "写进去只会让人以为验过了。\n"
    "\n"
    "★★ 你有手，但**只有两只**：能读（看文件、找代码），能**试跑**（跑一条命令看结果）。\n"
    "   你**不能改任何文件** —— 没有写工具，也不该想办法绕。\n"
    "   试跑看到问题（判据跑不起来、本来就是红的、跑出来不是你要的），"
    "你唯一的处置是**把它写下来**：写进 `note`，说清哪一条、什么问题、"
    "执行者接手时要注意什么。**不要自己修**，也不要因为「跑不通」就把那条判据删掉 —— "
    "删掉等于把问题藏起来，那比留着更坏。\n"
    "   写判据之前先试着跑一遍，是你该做的；跑不通就如实记下来，是你能做的全部。\n"
)

TPL = """任务书如下：

{task}
{lessons}

请产出这份契约，只输出一个 JSON 对象，不要别的文字：

{{
  "intent": "一句话：这件事要改变什么行为（不是'改哪个文件'）",
  "done": [
    {{"claim": "这条判据说的是什么（一句人话，能看懂）",
      "check": "在这个工作目录里直接能跑的命令",
      "pass": "什么算通过（退出码 0 / 输出包含什么）"}}
  ],
  "note": "为什么这么定，一两句（可省）"
}}

要求：
- done **3 到 6 条**。少于 3 条多半是没想清；多于 6 条是拿清单充数。
- check 要能直接跑，别写伪代码。工作目录里有什么就用什么。
- 每条 claim 要能看懂"它在验什么"，不要写成命令的复述。
- 如果任务里有一部分**没法用命令验**，在 note 里明说，别硬编一条判据。

两条硬约束（实测踩过）：
- **判行为，不判源码写法。** `assert add(2,3)==5` 和 `assert add(2, 3) == 5`
  是同一件事，判据不该因为空格不同就红。除非任务明确要求某种写法，
  否则别去检查源码长什么样 —— 那会把"怎么写"钉死，而任务要求的是"做成什么"。
- **判据要能在干净状态下跑。** 不要依赖上一轮留下的文件、日志或状态。
  一条判据如果"上一轮跑过才过得了"，它就不是判据，是巧合。
"""


def _pick(arg: str) -> Path:
    if arg:
        d = Path(arg)
        if not d.is_absolute():
            d = Path.cwd() / d
        if not (d / "job.json").exists():
            raise SystemExit("不是 job 目录：%s" % d)
        return d.resolve()
    if not JOBS.exists():
        raise SystemExit("还没有任何 job（%s 不存在）" % JOBS)
    cands = [p for p in JOBS.iterdir() if (p / "job.json").exists()]
    if not cands:
        raise SystemExit("还没有任何 job")
    d = max(cands, key=lambda p: p.name)
    print("（没给 --job，用最新的：%s）" % d.name)
    return d.resolve()


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出 JSON。**先试整体，再从代码块里提，最后找第一个大括号**。

    ★ 三级回退不是"宽容"，是因为模型经常在 JSON 前后加一句"好的，这是契约："。
      只认"整体是 JSON"会让一份合格的产出被当成失败。
    """
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


def _check_shape(obj: dict) -> tuple[list, list]:
    """返回（合格的条目, 问题）。**不合格的条目丢掉，不硬塞进去**。"""
    good, bad = [], []
    items = obj.get("done")
    if not isinstance(items, list):
        return [], ["没有 done 数组"]
    for n, it in enumerate(items, 1):
        if not isinstance(it, dict):
            bad.append("第 %d 条不是对象" % n)
            continue
        claim = str(it.get("claim") or "").strip()
        check = str(it.get("check") or "").strip()
        ok = str(it.get("pass") or "").strip()
        if not claim:
            bad.append("第 %d 条没有 claim（说不清它在验什么）" % n)
        elif not check:
            bad.append("第 %d 条没有 check 命令（判不了）" % n)
        elif not ok:
            bad.append("第 %d 条没有 pass（不知道什么算过）" % n)
        else:
            good.append({"claim": claim, "check": check, "pass": ok})
    return good, bad


# ─────────────────── 第二种活：只说「这一轮先做哪几条」 ───────────────────
#
# 为什么这件事该规划者说，而不是规则推导（2026-10-28）
#     上一节里"这一轮该谁干"是规则推的，因为那是纪律问题，模型会心软。
#     但"先做哪一条"不是纪律问题，是**依赖与卡点的判断**：
#     契约 5 条没过，先修哪条，取决于谁是谁的前提、谁的报错最具体。
#     规则推不出来，程序也不该假装推得出来。
#
# 但它的话**必须被钉住**，否则这一档就退化成"第二个执行者"
#     它能挑的编号，程序限定在"上一轮没过的那几条"里；挑到别处一律丢掉并记 rejected。
#     它**不能**改契约（已锁，改不了）、不能改范围、不能新增目标。
#     所以它的话再飘，也只飘在"顺序"这一个维度上 —— 这就是防多揽活的机制，
#     不是靠提示词求它别多嘴。
#
# 它只说 why，不说 how
#     "先说第 3 条，因为第 4 条依赖它" 是判断；
#     "第 3 条应该改成先调 X 再调 Y" 是代劳 —— 一旦出现，它就是执行者了。

SYS_FOCUS = (
    "你是「规划者」。执行者马上要动手了，你的活只有一件：\n"
    "从**已锁定的契约里、上一轮还没过的那几条**中，挑出这一轮先做哪几条。\n"
    "\n"
    "你**不能**做这些（做了就是越界，不是「帮忙」）：\n"
    "- 不能新增目标，不能提议改判据，不能说「顺便把某某也做了」。\n"
    "- 不能说怎么做 —— 那是执行者的事。你只说「先做哪条、为什么是它」。\n"
    "- 不能因为挑不出来就说「推倒重来」。挑不出来就如实说「一起吃」。\n"
    "\n"
    "按什么挑：\n"
    "- 依赖：某条是另一条的前提 ⇒ 前提先做。\n"
    "- 卡点：上一轮报错最具体、差得最远的那条，先打它。\n"
    "- 独立：互不相干就别硬凑顺序 —— 「这几条一起做」也是诚实的回答。\n"
    "\n"
    "除了挑先做什么，你还要说**这一轮明确不做什么**（declined）。\n"
    "它不是「没挑中的」，而是「我判断这一轮不该碰它」：\n"
    "写下它，下一轮才不会把你的暂缓当成「这件事已经定了」。\n"
)

TPL_FOCUS = """这件事的意图：{intent}

契约（{total} 条，已锁，谁都不能改）：

{items}

{state}
{lessons}

**还能动的**（你只能从这些编号里挑）：{open}

上一轮你明确说过「不做」的：{prev_declined}
（只是当时暂缓的，这一轮可以重新挑；仍然不该做的，请在 declined 里再说一次。）

只输出一个 JSON 对象，不要别的文字：
{{"focus": [{first}], "why": "一句话：为什么先这几条",
  "declined": [{{"what": "这一轮不做什么", "why": "为什么不做"}}]}}

focus 里只能出现上面「还能动的」里的编号。写别的编号会被程序直接丢掉。
declined 是**这一轮明确不做**的事；没有就给 []。它不等于「没挑中的」。
"""


def _focus_material(memo: dict, contract: dict) -> tuple[str, str, list, int, str]:
    """把"给规划者看的材料"拼出来。**不在这里判断该不该问**（那是 job.py 的事）。"""
    intent = str(contract.get("intent") or "").strip() or "（契约里没写意图）"
    items = contract.get("done") or []
    per = {int(x.get("n")): x for x in (memo.get("items") or [])
           if isinstance(x, dict) and str(x.get("n", "")).isdigit()}
    lines = []
    for n, it in enumerate(items, 1):
        p = per.get(n)
        if not p:
            mark = "上一轮**没跑到**这条"
        elif p.get("ok"):
            mark = "上一轮 **rc=%s 过了**" % p.get("rc")
        else:
            mark = "上一轮 **rc=%s 没过**" % p.get("rc")
        lines.append("%d. %s\n   怎么验：%s\n   算过的标准：%s\n   %s"
                     % (n, str(it.get("claim") or "")[:200],
                        str(it.get("check") or "")[:160],
                        str(it.get("pass") or "")[:160], mark))
    state = []
    if memo.get("fresh"):
        if memo.get("verdict"):
            state.append("上一轮测试者的疑点：%s" % str(memo["verdict"])[:300])
        ch = memo.get("changed") or []
        state.append("上一轮真正变了的文件：%s"
                     % ("、".join(str(x) for x in ch[:8]) if ch else "**一个都没有**"))
        if not ch:
            state.append("（一个文件都没变 —— 说明上一轮基本没落地，别把顺序排得太细）")
    else:
        state.append("这是第 1 轮，还没有跑过判据。")
    open_ids = [int(x) for x in (memo.get("open") or [])]
    return intent, "\n\n".join(lines), open_ids, len(items), "\n".join(state)


def cmd_focus(a) -> int:
    """回答"这一轮先做哪几条"。**只说不做，不改任何文件。**"""
    try:
        mem = json.loads(Path(a.memo).read_text(encoding="utf-8"))
        cdir = Path(a.contract)
        done_p = cdir / "done.json" if cdir.is_dir() else cdir
        contract = json.loads(done_p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print("✗ 状态文件或契约读不出来：%s" % e, file=sys.stderr, flush=True)
        return 2
    intent, items_txt, open_ids, total, state = _focus_material(mem, contract)
    if len(open_ids) <= 1:
        # 只有一条没过的时候，"挑"这件事不存在。让模型说也只是让它多说一句话。
        print("没得挑（还能动的只有 %d 条）—— 不需要规划者说话" % len(open_ids),
              file=sys.stderr, flush=True)
        return 6

    _pd = mem.get("prev_declined") or []
    _pd_txt = "；".join("%s（%s）" % (str(x.get("what") or "")[:80],
                                    str(x.get("why") or "")[:80])
                       for x in _pd if isinstance(x, dict)) or "（上一轮没有）"

    prompt = TPL_FOCUS.format(intent=intent[:300], total=total, items=items_txt,
                              state=state, open="、".join(str(x) for x in open_ids),
                              first=", ".join(str(x) for x in open_ids[:2]),
                              prev_declined=_pd_txt,
                              lessons=_lessons())
    if a.dry:
        print(SYS_FOCUS)
        print("---")
        print(prompt)
        return 0

    try:
        from loop.provider import Msg, OpenAICompatProvider
        prov = OpenAICompatProvider(profile=a.provider)
    except Exception as e:
        print("✗ 模型没配起来：%s: %s" % (type(e).__name__, e), file=sys.stderr, flush=True)
        return 3
    try:
        r = prov.chat([Msg("system", SYS_FOCUS), Msg("user", prompt)], [])
    except Exception as e:
        print("✗ 调用失败：%s: %s" % (type(e).__name__, str(e)[:200]),
              file=sys.stderr, flush=True)
        return 4

    text = (r.text or "").strip()
    obj = _extract_json(text)
    if not isinstance(obj, dict):
        print("✗ 没给出可解析的 JSON（原文 %d 字）：%s" % (len(text), text[:300]),
              file=sys.stderr, flush=True)
        return 5

    raw = obj.get("focus")
    if not isinstance(raw, list):
        raw = [raw] if raw is not None else []
    # ★ 规则只有一条：**只能挑 open 里的**。这里滤一遍（保证 FOCUS 行本身合法），
    #   job.py 收到之后还会再滤一遍（防这个脚本被换掉、或输出被人手改）。
    #   两处守的是同一条规则，不是两份规则。
    picked, rejected = [], []
    for x in raw:
        try:
            n = int(str(x).strip())
        except (TypeError, ValueError):
            rejected.append(str(x)[:20])
            continue
        if n not in open_ids:
            # ★ 挑到"已经过了的"或"根本不存在的"——一律丢掉。
            #   这不是不信任它，是因为**它没有理由挑那些**：
            #   过了的不用做，不存在的做不了。留下它就是给它开一个加活的口子。
            rejected.append(n)
            continue
        if n not in picked:
            picked.append(n)
    picked.sort()
    if rejected:
        print("⚠ 它挑了不该挑的：%s —— 已丢掉（只能挑还没过的）"
              % "、".join(str(x) for x in rejected), file=sys.stderr, flush=True)
    if not picked:
        print("✗ 一条能用的都没挑出来 —— 这一轮要兜底", file=sys.stderr, flush=True)
        return 6

    out = {"focus": picked,
           "why": str(obj.get("why") or "").strip()[:300],
           "declined": obj.get("declined") or [],
           "rejected": rejected,
           "model": getattr(prov, "model", ""),
           "by": "planner"}
    print("FOCUS " + json.dumps(out, ensure_ascii=False), flush=True)
    return 0


# ─────────────────── 第三种活：说「这一轮干什么」（一句话）───────────────────
#
# 为什么 L2 不给判据（2026-10-28 用户定的）
#     **契约是「打断」的替代品。** 人在场时，你能在第三轮说"不对，先写测试"——
#     这就是即时纠偏，不需要事先写下"什么算完成"。
#     人不在场时，这句话没人说，只能提前写死 —— 那才是契约。
#     所以 L2 用计划（一句话方向，人随时改），L4 用契约（锁死，没人改）。
#
# 计划不能写成判据
#     一旦它开始写"验收命令"，就又是一份契约 —— 只是没锁。
#     而"没锁的契约"比两种都坏：人以为它只是建议，程序却拿它当依据。
#     所以这个模式的输出**只有一个自由文本字段**，没有 done 数组可以填。

SYS_PLAN = (
    "你是「规划者」。这一轮该执行者动手了，**人就在旁边看着**。\n"
    "你的活只有一件：说清**这一轮干什么**，一句话。\n"
    "\n"
    "你**不要**做这些：\n"
    "- 不要写判据、不要写验收命令。这一档不需要契约 —— 人在场，他会当场判断对不对。\n"
    "- 不要写步骤清单（第一步第二步）。清单会变成它自己给自己派的活。\n"
    "- 不要复述任务要求。任务书它自己会读。\n"
    "\n"
    "该说什么：**这一轮把力气放在哪**，以及为什么是这儿（上一轮哪件事说明该先做这个）。\n"
    "★★ 如果任务书要求的是一次**完整交付**，不要擅自把它缩成「先做个骨架 / 最小竖切 / 其余留空壳」。\n"
    "   任务很大时，可以说先打通哪条闭合路径，但方向必须指向任务书要求的完整状态；\n"
    "   只有人明确说「先做一小步」或这是多轮迭代时，才可以把范围收窄。\n"
    "如果上一轮的成果已经够了，就直说「这一轮不用再动，等人看」—— "
    "那也是有用的回答，比编一件事给它做强。\n"
)

TPL_PLAN = """任务要求（原话）：
{task}
{lessons}

{state}

只输出一个 JSON 对象，不要别的文字：
{{"plan": "一句话：这一轮干什么", "why": "一句：为什么是这儿"}}
"""


def cmd_plan(a) -> int:
    """说一句话：这一轮干什么。**不写判据、不落盘、不改任何东西。**"""
    try:
        mem = json.loads(Path(a.memo).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # ★ 不吞成 traceback（2026-10-28）：上游拿这个退出码决定要不要兜底，
        #   一个 traceback 和"模型没说话"在调用方看来长得一样，但处置完全相反 ——
        #   前者是状态文件坏了要修，后者是模型这次不行可以重试。
        print("✗ 状态文件读不出来：%s（%s）" % (a.memo, e), file=sys.stderr, flush=True)
        return 2
    task = str(mem.get("task") or "").strip() or "（任务书是空的）"
    L = []
    if mem.get("fresh"):
        if mem.get("verdict"):
            L.append("上一轮测试者说：%s" % str(mem["verdict"])[:300])
        ch = mem.get("changed") or []
        L.append("上一轮真正变了的文件：%s"
                 % ("、".join(str(x) for x in ch[:8]) if ch else "**一个都没有**"))
        if not ch:
            L.append("（一个文件都没变 —— 上一轮基本没落地）")
    else:
        L.append("这是第 1 轮，还没跑过。")
    prompt = TPL_PLAN.format(task=task[:8000], state="\n".join(L), lessons=_lessons())
    if a.dry:
        print(SYS_PLAN)
        print("---")
        print(prompt)
        return 0

    try:
        from loop.provider import Msg, OpenAICompatProvider
        prov = OpenAICompatProvider(profile=a.provider)
    except Exception as e:
        print("✗ 模型没配起来：%s: %s" % (type(e).__name__, e), file=sys.stderr, flush=True)
        return 3
    try:
        r = prov.chat([Msg("system", SYS_PLAN), Msg("user", prompt)], [])
    except Exception as e:
        print("✗ 调用失败：%s: %s" % (type(e).__name__, str(e)[:200]),
              file=sys.stderr, flush=True)
        return 4

    text = (r.text or "").strip()
    obj = _extract_json(text)
    if not isinstance(obj, dict):
        print("✗ 没给出可解析的 JSON（原文 %d 字）：%s" % (len(text), text[:300]),
              file=sys.stderr, flush=True)
        return 5
    plan = str(obj.get("plan") or "").strip()
    if not plan:
        print("✗ 它没说出这一轮干什么（plan 是空的）", file=sys.stderr, flush=True)
        return 5
    print("PLAN " + json.dumps({"plan": plan[:300],
                                "why": str(obj.get("why") or "").strip()[:300],
                                # ★ 把**思考**也带出来（2026-10-29 联调时用户报：
                                #   「这轮还是没有出现规划者的思考块」）。
                                #   它原来只输出 plan 那一句话，思考过程没接出去，
                                #   于是界面上规划者像"凭空给了一个方向"。
                                "reasoning": (getattr(r, "reasoning", "") or "")[:1200],
                                "model": getattr(prov, "model", ""),
                                "by": "planner"}, ensure_ascii=False), flush=True)
    return 0


def _probe(cd: Path) -> dict:
    """起草完跑一遍**判据体检**：把判据扔进"什么都没做"的空世界，看它还绿不绿。

    ★ 为什么起草之后立刻做（2026-10-28）：一条恒真的判据 = 不管做没做都绿 =
      整份验收永远通过。而这件事**只有在这一刻能被便宜地发现** ——
      跑几轮之后才察觉"怎么老是绿的"，已经烧掉几十轮了。
    ★ 也不阻断起草：体检失败、超时、跑不起来，都只是"这次没体检成"，
      不影响契约本身。真正起作用的地方是 `contract.py lock` —— 那才是批准。
    """
    r = subprocess.run([sys.executable, str(APP / "bin" / "probe_judge.py"),
                        "--dir", str(cd), "--json"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        data = json.loads(r.stdout or "")
    except Exception:
        return {"ok": False, "reason": (r.stderr or r.stdout or "")
                .strip().splitlines()[-1:] or ["体检没跑起来"]}
    data["ok"] = True
    try:
        (cd / "probe.json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                                       encoding="utf-8")
    except OSError:
        pass
    return data


def _lessons(limit: int = 5) -> str:
    """以前这类活，人最后是怎么判的。

    ★ 为什么这可能是整套东西里唯一真正"跨 job"的东西（2026-10-29）：
      每个 job 都是孤岛 —— 纪要只给自己的下一轮看。而"上次这条路走过、不通"
      这件事，只有人的裁定里写着。不读它，下一个 job 会**从头再踩一遍**。
    ★ 理由是人写的，不是模型总结的。这一点不能变：模型的总结会把"人当时不满意"
      润色成"还可以再优化"，而前者能改行为，后者不能。
    ★ 没过的排前面 —— 那才是该看的。过了的那些只能说明"那样能行"。
    """
    p = _index_path()
    if not p.exists():
        return ""
    recs = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return ""
    done = [r for r in recs if r.get("verdict")]
    if not done:
        return ""
    done.sort(key=lambda r: (r.get("verdict") != "没过", r.get("at") or ""))
    L = ["", "## 以前这类活，人最后是怎么判的（理由是人写的，不是模型总结的）"]
    for r in done[-limit:]:
        L.append("- **%s** ｜ %s ｜ 契约 %s/%s ｜ 人说的理由：%s"
                 % (r.get("verdict"), str(r.get("task") or "")[:36],
                    r.get("passed") if r.get("passed") is not None else "?",
                    r.get("total") if r.get("total") is not None else "?",
                    str(r.get("reason") or "")[:140]))
    L.append("★ 没过的那些尤其要看：它不是「那次没做好」，是**这条路走过、不通**。"
             "如果这次的任务和它像，就别再定一样的目标。")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="规划者：起草契约 / 说这一轮聚焦什么")
    ap.add_argument("--focus", action="store_true",
                    help="只回答「这一轮先做哪几条」（读 --memo 与 --contract）")
    ap.add_argument("--plan", action="store_true",
                    help="只回答「这一轮干什么」一句话（读 --memo；不写判据）")
    ap.add_argument("--memo", default="", help="--focus / --plan 用的状态文件（job.py 生成）")
    ap.add_argument("--contract", default="", help="--focus 用的契约目录或 done.json")
    ap.add_argument("--job", default="")
    ap.add_argument("--provider", default="plan",
                    help="用哪份模型档案（默认 plan —— 规划者自己那一份，"
                         "可在界面的「规划者」下拉里选；原来默认 review，"
                         "等于规划者一直在用测试者的模型）")
    ap.add_argument("--dry", action="store_true", help="只打印提示词，不调模型")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已起草但**未锁**的契约（已锁的永远不覆盖）")
    a = ap.parse_args(argv)

    if a.focus:
        if not a.memo or not a.contract:
            print("✗ --focus 要同时给 --memo 和 --contract", file=sys.stderr, flush=True)
            return 2
        return cmd_focus(a)
    if a.plan:
        if not a.memo:
            print("✗ --plan 要给 --memo", file=sys.stderr, flush=True)
            return 2
        return cmd_plan(a)

    print("旧任务起草已退役，请在会话中建立持续委托。", file=sys.stderr)
    return 4


def _draft(a) -> int:
    from loop.lease import Lease
    from loop import runtime as RT
    d = _pick(a.job)
    lease = Lease(d, d.name)
    if not lease.acquire()[0]:
        print("这个委托已有运行，不能同时起草", file=sys.stderr)
        return 7
    try:
        RT.register(d, os.getpid(), phase="drafting")
        code = _draft_owned(a, d)
        RT.update_owned(d, state="completed" if code == 0 else "failed", phase="finished")
        return code
    except BaseException:
        RT.update_owned(d, state="unknown", phase="finished")
        raise
    finally:
        lease.release()


def _draft_owned(a, d) -> int:
    cfg = json.loads((d / "job.json").read_text(encoding="utf-8"))
    task_p = d / (cfg.get("task") or "task.md")
    task = task_p.read_text(encoding="utf-8", errors="replace").strip() if task_p.exists() else ""
    if not task:
        print("✗ 任务书是空的（%s）—— 规划者没有东西可规划" % task_p,
              file=sys.stderr, flush=True)
        return 2

    cd = d / "contract"
    cd.mkdir(parents=True, exist_ok=True)
    done_p, lock_p = cd / "done.json", cd / "done.lock"
    if lock_p.exists():
        print("✗ 这份总契约**已经锁了** —— 起草者不能改已锁的契约。", file=sys.stderr, flush=True)
        print("  要改只能走人的动作：python bin/contract.py amend --dir %s --reason \"...\""
              % cd, file=sys.stderr, flush=True)
        return 2
    if done_p.exists() and not a.force:
        print("已经有一份草案（%s）。要重新起草加 --force。" % done_p, file=sys.stderr, flush=True)
        return 2

    prompt = TPL.format(task=task[:6000], lessons=_lessons())
    if a.dry:
        print(SYS)
        print("---")
        print(prompt)
        return 0

    try:
        from loop.provider import Msg, OpenAICompatProvider
        prov = OpenAICompatProvider(profile=a.provider)
    except Exception as e:
        print("✗ 模型没配起来：%s: %s" % (type(e).__name__, e), file=sys.stderr, flush=True)
        return 3
    print("规划者：%s（%s）" % (getattr(prov, "model", "?"), a.provider), flush=True)
    # ★ 给它手（2026-10-29）：只读 + 试跑。每跑一条都记下来，
    #   最后落成 `contract/plan-precheck.json` —— 人锁之前能看见
    #   "这条判据它到底试过没有、跑出来什么"。
    _steps: list[dict] = []

    def _on_step(name: str, args: dict, res) -> None:
        _cm = ""
        if isinstance(args, dict):
            if args.get("cmd"):
                _cm = json.dumps(args.get("cmd"), ensure_ascii=False)[:200]
            elif args.get("path"):
                _cm = str(args.get("path"))[:120]
        _d = res if isinstance(res, dict) else {}
        _out = str(_d.get("out") or _d.get("error") or "")[:400]
        _steps.append({"tool": name, "what": _cm,
                       "ok": _d.get("ok"), "rc": _d.get("rc"), "out": _out})
        print("  %s %s %s" % ("✓" if _d.get("ok") else "✗", name, _cm[:100]),
              flush=True)

    _root = Path(cfg.get("cwd") or ".") if cfg.get("cwd") else Path(".")
    if not _root.is_dir():
        _root = Path(".")
    try:
        _reg = _plan_registry(_root)
    except Exception as _e:
        _reg = None
        print("⚠ 工具没建起来（%s）—— 这次只能凭空写判据" % _e, flush=True)
    try:
        if _reg is not None and _reg.names():
            r_text = _run_with_tools(prov, SYS, prompt, _reg, max_rounds=8,
                                     on_step=_on_step)
            text = (r_text or "").strip()
        else:
            r = prov.chat([Msg("system", SYS), Msg("user", prompt)], [])
            text = (r.text or "").strip()
    except Exception as e:
        m = "%s: %s" % (type(e).__name__, e)
        low = m.lower()
        if "quota" in low or "limit" in low:
            print("✗ 额度用完了（不是代码问题）：%s" % m[:200], file=sys.stderr, flush=True)
            return 3
        print("✗ 调用失败：%s" % m[:300], file=sys.stderr, flush=True)
        return 4

    # ★ 不再从 `r` 取（2026-10-29）：上面那条路是**带工具的循环**，
    #   没有单个 `r` 可用。循环里已经赋给 text 了。
    text = (text or "").strip()
    obj = _extract_json(text)
    if not obj:
        print("✗ 模型没给出可解析的 JSON（原文 %d 字）。它说了：" % len(text),
              file=sys.stderr, flush=True)
        print(text[:600], file=sys.stderr, flush=True)
        return 5

    good, bad = _check_shape(obj)
    if bad:
        print("⚠ 有 %d 条不合格，已经丢掉（不把判不了的东西写进契约）：" % len(bad),
              flush=True)
        for b in bad[:6]:
            print("   · %s" % b, flush=True)
    if not good:
        print("✗ 一条合格判据都没有 —— 没有写成契约。", file=sys.stderr, flush=True)
        print("  多半是任务书太模糊，或者这条活本来就不该用判据去验。",
              file=sys.stderr, flush=True)
        return 5

    out = {
        "intent": str(obj.get("intent") or "").strip()[:200],
        "done": good,
        "drafted_by": "plan.py（规划者）",
        "drafted_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": getattr(prov, "model", ""),
    }
    if obj.get("note"):
        out["planner_note"] = str(obj["note"])[:600]
    done_p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")

    # ── 试跑记录落盘（2026-10-29）──
    # ★ 为什么单独一个文件：`done.json` 是**给机器跑的判据**，格式要稳；
    #   试跑记录是**给人看的证据**，字段会变。混在一起，早晚有一边要迁就另一边。
    # ★ 它在锁契约之前就存在，所以人锁那一眼能看到"这条判据它试过没有、
    #   跑出来什么" —— 而不是只能看它自称"这些都能跑"。
    _ran = [s for s in _steps if s.get("tool") == "run_check"]
    if _steps:
        _pc = cd / "plan-precheck.json"
        _pc.write_text(json.dumps({
            "at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "by": "规划者（试跑，只读 + 执行，没有写）",
            "tried": len(_steps),
            "ran_commands": len(_ran),
            "failed": [s for s in _steps if s.get("ok") is False][:20],
            "steps": _steps[:60],
        }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print("\n起草了 %d 条判据 → %s" % (len(good), done_p), flush=True)
    for n, it in enumerate(good, 1):
        print("  %d. %s" % (n, it["claim"][:70]), flush=True)
        print("     check: %s" % it["check"][:90], flush=True)
    if out.get("planner_note"):
        print("\n它自己补了一句：%s" % out["planner_note"][:300], flush=True)
    # ★ 试跑这件事要**被看见**（2026-10-29）：没跑过和跑失败了，
    #   在"判据能不能跑"这个问题上是两回事，而 done.json 里看不出来。
    if _steps:
        _bad = [s for s in _steps if s.get("ok") is False]
        print("\n它试了 %d 次（跑命令 %d 条）%s"
              % (len(_steps), len(_ran),
                 "，其中 %d 次没成 —— 那些它应该写进上面那句 note 里" % len(_bad)
                 if _bad else "，都成了"), flush=True)
        for s in _steps[:6]:
            print("  %s %s" % ("✓" if s.get("ok") else "✗", str(s.get("what"))[:90]),
                  flush=True)
        print("  （全部记在 %s）" % (cd / "plan-precheck.json"), flush=True)
    else:
        print("\n★ 它**一次都没试跑** —— 那些判据能跑只是它说的，没人验过。",
              flush=True)

    # ── 判据体检（2026-10-28）──
    # 一条恒真的判据 = 不管做没做都绿 = 整份验收永远通过。
    # 这件事只有在**这一刻**能被便宜地发现；跑几轮之后才察觉"怎么老是绿的"，
    # 已经烧掉几十轮了。
    print("\n判据体检：把判据扔进「什么都没做」的空世界，看它还绿不绿……", flush=True)
    pr = _probe(cd)
    if not pr.get("ok"):
        print("  ⚠ 这次没体检成（%s）—— 不影响契约本身，但锁之前你就看不到这一层"
              % str(pr.get("reason"))[:160], flush=True)
    else:
        rows = pr.get("rows") or []
        bad = [r for r in rows
               if str(r.get("verdict") or "") in ("恒真", "自指", "自指·内容", "自指·存在")]
        for r in rows:
            _v = str(r.get("verdict_say") or r.get("verdict") or "?")
            print("  %s %s ｜ %s" % ("✗" if r in bad else "·",
                                    str(r.get("label") or "?")[:40], _v), flush=True)
        if bad:
            print("\n  ✗ **%d 条绿了不代表做完了**：把整个世界清空，它们照样绿。" % len(bad), flush=True)
            for r in bad[:5]:
                print("     · %s：%s" % (str(r.get("label") or "?")[:30],
                                        str(r.get("why") or "")[:100]), flush=True)
            print("  ⇒ 锁的时候会被拦下（要么改判据，要么加 --allow-weak 知情地接受）",
                  flush=True)
        else:
            print("  ✓ 没有恒真判据（这只说明它们不是恒真，不等于判得准）", flush=True)

    # ── 三问（只在第四档，2026-10-28）──
    # 前三档人在场，三问是替人把同一件事多想一遍 —— 人多花那几分钟不值。
    # 第四档没有人在场，契约是唯一的靶子，而"靶子画歪"这件事只有这一层能查。
    # 三次串行调用，所以只在这里做。
    if (cfg.get("crew") or "l1") == "l4":
        print("\n三问：三个角色对着这份契约各提一句最尖的质疑……", flush=True)
        try:
            # ★ 不抓 stdout（2026-10-28）：抓走了人就看三问的正文了，
            #   而人正是唯一该读它的人。这里只要退出码。
            r3 = subprocess.run(
                [sys.executable, str(APP / "bin" / "crosscheck.py"),
                 "--contract", str(cd), "--task", str(task_p)])
            if r3.returncode != 0:
                print("  ⚠ 三问没跑全 —— 缺的那一路质疑这次是空的，"
                      "别把它当成「没问题」。", flush=True)
        except Exception as e:
            print("  ⚠ 三问没跑成（%s: %s）—— 别把空当成「没问题」。"
                  % (type(e).__name__, str(e)[:120]), flush=True)

    print("\n★ **没锁**。锁是人的动作：", flush=True)
    print("    python bin/job.py contract --job \"%s\" lock" % d, flush=True)
    print("  起草的人不能同时宣布题目没问题 —— 所以这一步必须你来看。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

