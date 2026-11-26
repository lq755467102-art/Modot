#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""distill.py —— 会话收尾时，把"这一轮有什么值得进树"提出来，等人点头。

为什么要有这一步
    记忆树是**每轮开工前注入**的长期规则，它现在只有一个来源：人手动写。
    而机器每天跑几十轮，**没有一步是往回递的** ——
    于是"在主会话里做很多事"和"这些事留下的结论"是两件事，
    人一换会话，没写进树的观点就散了。
    ★ 但机器**不直接写树**：树是人拍的板，机器代写就是污染。
      所以这里只有"提候选"，落到 memory/inbox/，等 `python bin/distill.py accept`。
      这和「机器不写教训」是同一条：教训是判断，机器能给的只是复述。

它凭什么判断"值得进树"
    只认定三件事：跨轮仍然成立的规矩 / 判断标准 / 操作纪律。
    过程复述（"我改了 X 文件"）不算 —— 那是这一轮的脚印，不是经验。
    拿不准就给空数组：**0 条是合法答案**，凑数会把树写脏，而树每轮都注入，
    脏一条的代价是每一轮都被它带偏。

层怎么认
    按**关键字**匹配文件名（对话姿态 / 收束 / 守卫 / 记忆），不按完整文件名 ——
    现在树里三层都叫 L1.xxx（历史遗留），按名匹配的话改一次名就断。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import paths as P            # 程序根 / 数据根，只此一处
ROOT = P.data_root()               # 数据根（打包后是用户目录；开发时就是仓库根）

# layer 关键字 → 认文件名用的关键字。模型只在这四个里选。
LAYERS = ("姿态", "收束", "守卫", "记忆")

# 待定目录提成常量：测试要能把它指到临时目录，
# 否则每跑一次验收就往真 inbox 里塞一份假候选 —— 那会污染人的待办队列。
INBOX = ROOT / "memory" / "inbox"

SYS = """你在给一个 agent 的「记忆树」提候选条目。记忆树是每轮开工前注入的长期规则，跨会话生效。

你的活：从这一轮里挑出**跨轮仍然成立**的东西，最多 2 条。挑不出就交空数组。

什么算（三条之一）：
- 一条以后每轮都该遵守的规矩
- 一个判断标准（怎么算对、怎么算完）
- 一条操作纪律（什么顺序做、什么情况下必须先停）
必须能脱离这一轮独立读懂：不许出现「这一轮」「刚才」「上面」。

什么不算（写进去就是污染，每一轮都会被它带偏）：
- 过程复述：「改了 accept.py」「跑了三次验收」
- 只在这一次成立的细节：具体文件路径、具体数字、这一次的结论
- 树里已经有的（下面给了现有条目的摘录，重复的不要）
- 拿数字代替判断的东西：「至少读 3 个文件」「输出不少于 800 字」

layer 只能取这四个之一：
  姿态 = 怎么和人对话、怎么表达
  收束 = 什么算做完、怎么验、结论怎么给
  守卫 = 什么不许做、谁说了算、权限与不可逆动作
  记忆 = 记忆本身怎么维护
拿不准就选「收束」。

只输出一个 JSON 数组，不要解释、不要代码块围栏：
[{"layer":"守卫","rule":"一句话，命令式","why":"依据这一轮的哪一步（一句话）"}]
没有值得留的就输出 []
"""


def _parse_json_array(text: str) -> list | None:
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


def layer_file(tree_dir: Path, key: str) -> Path | None:
    """按关键字找层文件。找不到返回 None（不猜、不新建）。"""
    k = (key or "").strip()
    if k not in LAYERS:
        return None
    for f in sorted(Path(tree_dir).glob("*.md")):
        if k in f.stem:
            return f
    return None


def _tree_digest(tree_dir: Path, limit: int = 1400) -> str:
    """现有树的摘要：只给每层的小标题和条目开头，用来去重。"""
    out = []
    used = 0
    for f in sorted(Path(tree_dir).glob("*.md")):
        lines = [ln.strip() for ln in f.read_text(encoding="utf-8", errors="replace").splitlines()]
        keep = []
        for ln in lines:
            if not ln or ln.startswith("#"):
                if ln.startswith("# "):
                    keep.append(ln)
                continue
            if ln.startswith(("- ", "* ")):
                keep.append("  " + ln[:70])
        if not keep:
            continue
        blob = "\n".join(keep)
        if used + len(blob) > limit:
            blob = blob[:max(0, limit - used)]
        out.append("%s\n%s" % (f.stem, blob))
        used += len(blob)
        if used >= limit:
            break
    return "\n".join(out)


def _digest(events_path: Path, benni_path: Path | None, limit: int = 3200) -> str:
    """这一轮发生了什么：任务、它说过的话、调过的工具、判据结果。

    ★ 只给"结论和动作"，不给原始输出 —— 提炼要的是脉络，
      把 20 万字的工具输出塞进去，模型只会开始复述过程。
    """
    rows = []
    if events_path.exists():
        for ln in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except Exception:
                continue
    parts = []
    for r in rows:
        act = r.get("act")
        if act == "user":
            parts.append("任务：" + str(r.get("text") or "")[:400])
        elif act == "say":
            parts.append("它说：" + str(r.get("text") or "")[:200].replace("\n", " "))
        elif act == "tool":
            a = r.get("args")
            brief = ""
            if isinstance(a, dict):
                brief = str(a.get("path") or a.get("cmd") or "")[:70]
            parts.append("动作：%s %s → %s" % (r.get("tool") or "", brief,
                                              "成" if r.get("ok") is not False else "败"))
        elif act == "verify":
            parts.append("判据：%s（%s）" % ("过" if r.get("ok") else "没过",
                                            str(r.get("reason") or "")[:120]))
        elif act == "error":
            parts.append("出错：" + str(r.get("reason") or "")[:120])
        elif act == "end":
            parts.append("收尾：%s · %s 轮 · %s" % (r.get("status"), r.get("turns"),
                                                   str(r.get("why") or "")[:100]))
    blob = "\n".join(parts)
    if benni_path and benni_path.exists():
        b = benni_path.read_text(encoding="utf-8", errors="replace")
        b = re.sub(r"^<!--[\s\S]*?-->", "", b).strip()
        blob += "\n\n本我复核（另一个模型写的，可参考它的怀疑）：\n" + b[:700]
    return blob[:limit]


def propose(task: str, prov, work_root: Path, sess_dir: Path, tree_dir: Path,
            sess=None, on_delta=None) -> tuple[list[dict], str]:
    """提候选。**不写树**，只返回 (候选列表, 为什么是空的)。

    ★ 空列表是合法答案，但要带理由：0 条可能是"这一轮确实没什么"，
      也可能是"模型没吐 JSON"—— 这两种必须分开，否则"没提炼出来"
      会被读成"没有值得记的"。
    """
    from loop.provider import Msg

    digest = _digest(Path(sess_dir) / "events.jsonl",
                     Path(sess_dir) / "benni-model.md")
    ask = ("这一轮的任务：\n%s\n\n"
           "这一轮的实际经过：\n%s\n\n"
           "记忆树现有的条目（别重复）：\n%s\n\n"
           "请按格式给出最多 2 条候选（JSON 数组，没有就 []）。"
           % (task[:1200], digest, _tree_digest(tree_dir)))

    try:
        if on_delta is not None and getattr(prov, "supports_stream", False):
            reply = prov.chat_stream([Msg("system", SYS), Msg("user", ask)], [],
                                     on_delta=on_delta)
        else:
            reply = prov.chat([Msg("system", SYS), Msg("user", ask)], [])
    except Exception as e:
        return [], "提炼时模型调不动：%s" % e

    items = _parse_json_array(getattr(reply, "text", "") or "")
    if items is None:
        return [], "模型没给出可解析的候选（不是 JSON 数组）"

    # 现有树里出现过的句子不再提；**待定队列里已有的也不提** ——
    # 不去重待定队列的话，同一个观点每轮都会被重提一次，inbox 很快变成垃圾场，
    # 而人一旦开始忽略 inbox，这一步就白做了。
    existing = ""
    for f in sorted(Path(tree_dir).glob("*.md")):
        existing += f.read_text(encoding="utf-8", errors="replace")
    inbox = INBOX
    if inbox.exists():
        for f in sorted(inbox.glob("*.md")):
            existing += f.read_text(encoding="utf-8", errors="replace")

    out = []
    dropped = 0
    for it in items[:2]:
        if not isinstance(it, dict):
            continue
        rule = str(it.get("rule") or "").strip()
        key = str(it.get("layer") or "").strip()
        why = str(it.get("why") or "").strip()
        if not rule:
            continue
        if key not in LAYERS:
            key = "收束"                     # 拿不准归到"什么算做完"，不丢条目
        head = re.sub(r"\s+", "", rule)[:14]
        if head and head in re.sub(r"\s+", "", existing):
            dropped += 1                      # 树里已经有了
            continue
        if re.search(r"这一轮|刚才|上面|本次任务", rule):
            dropped += 1                      # 离不开这一轮 = 不是长期规则
            continue
        out.append({"layer": key, "rule": rule, "why": why})

    if out:
        return out, ""
    if dropped:
        return [], "提了 %d 条，但都和树里重复或离不开这一轮，已丢掉" % dropped
    return [], "它认为这一轮没有值得进树的东西（0 条是合法答案）"


def save_inbox(sess_dir: Path, task: str, items: list[dict], model: str = "") -> Path | None:
    """把候选写成待定文件。返回文件路径。"""
    if not items:
        return None
    inbox = INBOX
    inbox.mkdir(parents=True, exist_ok=True)
    import datetime as dt
    stamp = dt.datetime.now()
    sid = Path(sess_dir).name
    f = inbox / ("%s-%s.md" % (sid, stamp.strftime("%m%d-%H%M")))
    lines = ["# 待定 · %s" % sid, "",
             "<!-- 由 distill 提的候选，等你点头才进树 ｜ 模型：%s ｜ %s -->"
             % (model or "?", stamp.strftime("%Y-%m-%d %H:%M:%S")),
             "", "这一轮的任务：%s" % (task or "")[:200].replace("\n", " "), ""]
    for i, it in enumerate(items, 1):
        lines.append("## %d. [%s] %s" % (i, it["layer"], it["rule"]))
        if it.get("why"):
            lines.append("为什么：%s" % it["why"])
        lines.append("")
    f.write_text("\n".join(lines), encoding="utf-8")
    return f
