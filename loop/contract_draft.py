#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""contract_draft.py —— 开工前把"什么算完成"写成 2-4 条可核的判据。

为什么必须自动起草
    `bin/contract.py` 已经会校验、加锁、逐条跑 check、并在哈希对不上时红。
    但它**不会凭空写出那几条** —— 它一直等着人递 `--done "claim::check"`。
    结果这套机器空转了整整一个星期：实测几十轮任务，没有一轮生成过 done.json，
    于是所有任务都退回代理指标（数读了几个文件、写了多少字）。
    ★ 判据空着的时候，"用代理指标"看起来是合理的降级；实际是**降级到判错**。

起草的人是谁，以及这个选择的代价
    干活的那个模型（不是本我）。理由：它最清楚这活打算怎么做。
    代价是它有动机把判据写宽 —— 宽判据它容易过。
    ⇒ 这正是本我下一步要在**开工前**反驳的对象（现在的本我只看收尾）。
    ⇒ 起草和执行是**两次独立调用、不共享上下文**：起草时它还"没开始做"，
      说得会诚实一些；等它做完再让它总结，那就是自我评价了。

两条硬规矩
    ① 判据不能是"永远为真"：`true` / `exit 0` / `echo ok` 退出码恒为 0，
       跑出来的"通过"什么也不说明。当场拒。
    ② 起草失败必须**说出来**，不许静默退回代理指标 ——
       那等于把"没判据"包装成"有判据"，比没契约更糟。
"""
# 主要引用方包括 `bin/run-task.py` 和 `bin/accept.py`。
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from . import paths as P
APPDIR = P.app_root()              # ★ 程序根：bin/*.py 在这儿，打包后是只读的

SYS = """你在**开工前**做两件事，顺序不能反。

第一步 —— 先看这句话说清楚没有。不清楚就**问人**，只输出：
{"need": ["问题1", "问题2"]}

什么叫不清楚（满足任何一条就算）：
  · 要改哪个文件 / 哪个模块不明，而且猜错要推翻重做；
  · 做到什么程度不明（改一处还是全改、要不要动别的模块）；
  · 拿什么算「做对了」不明，而且你找不到现成的验证办法。
什么时候**不许问**：
  · 一句话能说明白的小任务（改个文案、答个问题）—— 直接给判据；
  · 你能靠下面的「项目里现成的东西」自己判断出来 —— 自己判断，别拿这个当挡箭牌；
  · 最多 3 个问题。**不要为了显得严谨而问。**
  · 如果来文里已经带着「对上一轮问题的回答」，说明已经问过了 —— **必须直接起草，不许再问。**

第二步 —— 信息够了就起草契约，把任务翻译成 2-4 条能用命令判定的判据。

规矩（违反任何一条，这份契约就是废纸）：
0. **先用项目里现成的验证方式，不许自己发明。**
   有 package.json / Makefile / pytest / tests/ / bin/accept.py 之类，就用它们原来的入口；
   自己编一条"看起来能验证"的命令，等于给自己出考题 —— 出得松就白判，出得紧就永远不过。
   一条都找不到，才自己写，而且要在 claim 里写明"这是新写的、不是项目原有的"。
1. 每条必须有 check：一条命令，**退出码 0 = 通过**。
1.5 **claim 必须是一句人话，不超过 24 个字。** 它会被当成这条判据的名字显示给人看。
   反例（太长、全是套话）：
     仓库里确实存在「新会话」的实现入口，回复对该功能的定位有实物依据、不是编造
   正例：
     「新会话」有没有真的做出来
     本我批注有没有留下产物
    验收会不会因为改坏一处而变红
   ★ 不要写「r1 / 判据一 / 验证项」这种编号 —— 编号是锁定时自动生成的，不用你写。
2. check 在项目根目录执行，用相对路径（例如 python bin/accept.py）。
3. 判据要判「做对没有」，不是「做了多少」。
   禁止写「写出 ≥N 字」「读了 ≥N 个文件」「输出非空」这类代理指标。
4. 禁止写永远为真的命令（true / exit 0 / echo ok）。
5. 任务很小（比如只是问一句话）就写 1 条，别凑数。
6. 每条都要能被一条命令验证；验不了的，不要写进契约。

起草判据时只输出一个 JSON 数组，不要解释、不要代码块围栏：
[{"claim": "这条保证什么（一句话）", "check": "命令", "pass": "什么算通过"}]
"""

# 退出码恒为 0 的命令 —— 判据里出现就等于没判据
FAKE = ("true", ":", "exit 0", "exit", "echo ok", "echo done", "cd .", "sleep 0")


def _parse_json_array(text: str) -> list | None:
    """从模型回话里抠出 JSON 数组。剥掉 ```json 围栏，也容忍前后废话。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(s[i:j + 1])
    except Exception:
        return None
    return obj if isinstance(obj, list) else None


def _bad_check(cmd: str) -> str:
    """这条 check 是假判据吗？返回理由，空字符串表示没问题。"""
    c = (cmd or "").strip()
    if not c:
        return "check 是空的"
    low = re.sub(r"\s+", " ", c.lower()).strip()
    if low in FAKE:
        return "check 恒为真（%s），判不出任何东西" % c
    # 纯 echo 也是恒为真（退出码永远是 0）
    if low.startswith("echo ") and not any(x in low for x in ("&&", "||", "|")):
        return "check 只是 echo（%s）—— 退出码恒为 0" % c
    return ""


def _parse_need(text: str) -> list | None:
    """模型是在问人吗？是就返回问题列表，不是返回 None。

    ★ 为什么先剥围栏再找 `{`：模型很爱写 ```json 围栏，而且常在数组外面
      再包一层对象。这里只认 `{"need": [...]}` 这一种形状 ——
      认宽了会把判据里的 `{"claim": ...}` 误判成提问。
    """
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(s[i:j + 1])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    need = obj.get("need")
    if not isinstance(need, list):
        return None
    qs = [str(x).strip() for x in need if str(x).strip()]
    return qs[:3] or None


def _repo_hint(work_root: Path) -> str:
    """把项目里"现成的东西"列给它，让它能自己判断，别拿"不清楚"当挡箭牌。

    ★ 起草阶段没有工具可用（只是一次 chat），所以它没法自己去 grep。
      不给它看，它只能问"这个项目怎么验证" —— 而答案就在根目录摆着。
    """
    try:
        names = sorted(p.name + ("/" if p.is_dir() else "")
                       for p in Path(work_root).iterdir()
                       if not p.name.startswith("."))
    except Exception:
        return ""
    if not names:
        return ""
    return "项目根目录下现成的东西（判断用得上，不用问人）：" + "、".join(names[:40])


def draft(task: str, prov, work_root: Path, target_dir: Path, sess=None,
          on_delta=None, allow_ask: bool = True, history: str = "") -> dict:
    """让模型先判断需不需要问人，再起草契约。

    返回（**不再是 Path | None**，因为「在问人」和「没起草出来」是两件事，
    原来都返回 None，调用方只能一律当成失败继续往下跑 —— 于是采访这件事
    永远不可能发生）：
        {"kind": "contract", "dir": Path}    契约已写并锁上
        {"kind": "need", "questions": [...]}  信息不够，停下来问人
        {"kind": "failed", "reason": "..."}   起草失败，要说出来

    ★ `allow_ask=False`：这一轮已经问过了（会话里有 interview.json）。
      再让它问就成了"永远在问、永远不开工"，所以这一轮强制起草。
    ★ `history`：上一轮的问答，拼进 prompt —— 否则它看不到答案，
      会把同一个问题再问一遍，而人看到的是"我说了它没听"。
    """
    from loop.provider import Msg

    ask = "这次要做的事：\n%s\n" % task
    if history.strip():
        ask += "\n（对上一轮问题的回答，已经问过了 —— 不要再问）\n%s\n" % history.strip()
    _hint = _repo_hint(work_root)
    if _hint:
        ask += "\n%s\n" % _hint
    ask += "\n项目根目录：%s\n" % work_root
    if allow_ask and not history.strip():
        ask += "先判断说清楚没有：不清楚就输出 {\"need\": [...]}，清楚就给判据数组。\n"
    else:
        ask += "信息够用，**直接给判据数组**（不许再问）。\n"
    try:
        # 有 on_delta 且 provider 支持流式 ⇒ 边走边报（思考能实时看见）
        if on_delta is not None and getattr(prov, "supports_stream", False):
            reply = prov.chat_stream([Msg("system", SYS), Msg("user", ask)], [],
                                     on_delta=on_delta)
        else:
            reply = prov.chat([Msg("system", SYS), Msg("user", ask)], [])
    except Exception as e:
        _emit(sess, ok=False, reason="起草契约时模型调不动：%s" % e)
        return {"kind": "failed", "reason": "起草契约时模型调不动：%s" % e}

    _txt = getattr(reply, "text", "") or ""

    # ── 它在问人 ──
    _qs = _parse_need(_txt)
    if _qs:
        if not allow_ask:
            # 说好不问了还问 ⇒ 不能让它靠提问逃避开工，按失败报出来。
            _emit(sess, ok=False, reason="这一轮说好不再问了，它还在问：%s" % _qs[0][:80])
            return {"kind": "failed", "reason": "该起草了但它在提问（已经问过一轮）"}
        _emit(sess, ok=False, interview=True, questions=_qs,
              reason="信息不够定判据，先问人：%s" % " ／ ".join(_qs)[:200])
        return {"kind": "need", "questions": _qs}

    items = _parse_json_array(_txt)
    if not items:
        _emit(sess, ok=False, reason="模型没给出可解析的契约（不是 JSON 数组）")
        return {"kind": "failed", "reason": "模型没给出可解析的契约"}

    rows = []
    for i, it in enumerate(items[:4], 1):
        if not isinstance(it, dict):
            continue
        claim = str(it.get("claim") or "").strip()
        check = str(it.get("check") or "").strip()
        pas = str(it.get("pass") or "").strip() or "退出码 0"
        why = _bad_check(check)
        if why:
            _emit(sess, ok=False, reason="第 %d 条不算判据：%s" % (i, why))
            return {"kind": "failed", "reason": "第 %d 条不算判据：%s" % (i, why)}
        if claim:
            rows.append({"id": "r%d" % i, "claim": claim, "check": check, "pass": pas})

    if not rows:
        _emit(sess, ok=False, reason="契约里一条可用判据都没有")
        return {"kind": "failed", "reason": "契约里一条可用判据都没有"}

    # ── 落盘：走 contract.py new，不自己写文件 ──
    # ★ 为什么不直接 write_text：contract.py 会做语义校验（不可逆动作、出处、
    #   命令能不能起步），自己写就绕过这些，等于造第二套规则。
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, str(APPDIR / "bin" / "contract.py"), "new",
            "--dir", str(target_dir), "--task", task[:400]]
    for d in rows:
        args += ["--done", "%s::%s::%s" % (d["claim"], d["check"], d["pass"])]
    r = subprocess.run(args, cwd=str(work_root), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        _why = "contract.py 拒了这份契约（rc=%d）：%s" % (
            r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()[:300])
        _emit(sess, ok=False, reason=_why)
        return {"kind": "failed", "reason": _why}

    # ★★ **不锁**（2026-10-29 改的，这条原来反了）：
    #   原来起草完就地 `contract.py lock`，理由是"锁在开工前，免得边做边把标准调低"。
    #   那个理由没错，错的是**谁锁**。这个项目一直讲"出题的人不能同时宣布题目没问题"，
    #   而自动锁等于让起草的人替人做了那个宣布 —— 之后哈希对上了，
    #   于是那份契约在记录里看起来像"人批准过"。
    #   现在起草完就停在这里，状态是"等人锁"。要锁是人敲 `contract.py lock`。
    _emit(sess, ok=True, items=rows, locked=False, need_lock=True,
          reason="契约写好了，**还没锁** —— 锁是人的动作（contract.py lock）")
    return {"kind": "contract", "dir": target_dir, "locked": False}


def _emit(sess, ok: bool, reason: str = "", **kw) -> None:
    if sess is None:
        return
    try:
        sess.emit("contract", ok=ok, reason=reason, **kw)
    except Exception:
        pass
