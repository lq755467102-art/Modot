#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""benni.py —— 本我（逆向 harness）的三个工具

它不是"第二个 agent"，它只有三件事：

  build-prompt   把 intent + done + 动作流水快照 + 待审回复，拼成本我的一次性 prompt
  check          机械校验一份五行输出是否**符合契约**（不合规就打回，不靠人眼看）
  stats          从动作流水出四个查询（做了什么 / 哪条非零 / 动了哪些文件 / 什么时候忙）

为什么 check 要机械做
    "格式合规但内容空"是我们列过的、原理上修不了的一条。
    但其中一半能靠**可机械判的规则**压住：**指不出具体位置的那条，作废。**
    （这条来自记忆树 `L1.对话姿态.md` §二 反喂养："反驳必须具体，空泛的不算"）

设计约束
    · check 的结论必须自带 verdict —— 因为 0 有两种含义：**"比了、都合规"** 和
      **"一条都没产出"**。两者都显示 0。（同 check-doc-index 那次教训）
    · stats 的每个数都要带**分母**，否则"0"不知道是没比还是比了都对。
    · 全程只读，不写任何状态。

五行契约（见 FRAMEWORK.md §5）
    ① 对得上吗   逐条对 done.json
    ② 我看到的   每条带命令或文件名
    ③ 我怀疑的   逐条：哪一句 / 缺什么（**指不出位置的这条作废**）
    ④ 我没查的   **不许写"无"**
    ⑤ 我提醒的   必须带上下文使用率 %
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

MARKS = ("①", "②", "③", "④", "⑤")
LABELS = {
    "①": "对得上吗",
    "②": "我看到的",
    "③": "我怀疑的",
    "④": "我没查的",
    "⑤": "我提醒的",
}


def _strip_label(m: str, seg: str) -> str:
    """去掉标记后面那截标签（如「④ 我没查的   无」里的「我没查的」）。

    不删的话，"无" 会被判成 "我没查的无"，空值检测就漏了。
    """
    s = seg.lstrip()
    lab = LABELS.get(m, "")
    if lab and s.startswith(lab):
        s = s[len(lab):]
    # ★ 标签后面常跟一个冒号（`③ 我怀疑的：`），不剥掉的话
    #   它会被当成 ③ 的第一行内容 ⇒ 多切出一条空批注（实测：4 条被切出 5 条）。
    # ★ 标签后面还可能跟一个计数（`③ 我怀疑的（4 条）`），
    #   不剥掉的话它会变成第一条批注（"（4 条）"本身）。
    s = re.sub(r"^[（(]\s*\d+\s*条\s*[)）]", "", s.lstrip("：: \t　"))
    s = s.lstrip("：: \t　")
    return s.strip()

# ④ 写了这些词 = 假装全面
EMPTY_ANSWERS = ("无", "没有", "暂无", "n/a", "none", "无。", "暂无。")
# ③ 每条必须带"缺口词" —— 说不出缺什么，就只是"我觉得不对"
# ★ 缺口词表。它是"这条有没有指出缺什么"的代理判据，**一定会被自然语言变体打穿**：
#   实测「会被漏掉，它没查」不命中（表里只有"没有"，没有"没"），
#   正确批注被误杀。所以宁可放宽，真正的守门交给 bin/audit.py（命令能重跑才算数）。
GAP_WORDS = ("缺", "不足", "没有", "没", "未", "找不到", "支撑不起", "只覆盖",
             "不成立", "反例", "漏", "无法", "不能", "覆盖不到", "未覆盖")
# ③ 每条必须带"位置标记" —— 指不出位置的作废
POS_PATTERNS = (
    re.compile(r"`[^`]+`"),                 # 反引号里的文件名/命令/字段
    re.compile(r"「[^」]+」"),                # 「引号里的一句话」
    re.compile(r"\"[^\"]{4,}\""),           # 英文引号
    re.compile(r"[\w./\\-]+\.(py|mjs|md|json|jsonl|ps1|sh)\b"),  # 文件路径
    re.compile(r"(第\s*\d+\s*行|line\s*\d+)", re.I),
)
# 本我不能自判赢了
SELF_JUDGE = ("我发现", "我抓到", "确实有问题", "确认是错", "确实是错", "我抓出了")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


# 意图或完成判据文件缺失、不可解析时返回空值，让提示词明确指出本轮复核缺哪种材料。
def _load_json(p: Path):
    try:
        return json.loads(_read(p)) if p.exists() else None
    except Exception:
        return None


# 动作流水可能以不完整记录收尾。逐行保留可解析记录，复核所用序号以有效记录为准。
def _load_actions(p: Path, limit: int | None = None) -> list[dict]:
    rows = []
    if not p.exists():
        return rows
    for line in _read(p).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows[:limit] if limit else rows


# ─────────────────────────── build-prompt ───────────────────────────

# 从 CLI 指定的工作目录读取动作、意图和完成判据。
# 动作流水缺失时结束复核入口，避免把缺证据当成无问题。
def cmd_build_prompt(a) -> int:
    root = Path(a.dir)
    intent = _load_json(root / a.intent)
    done = _load_json(root / a.done)
    acts_name = a.actions or ("actions-%s.jsonl" % __import__("datetime").date.today().isoformat())
    acts = _load_actions(root / acts_name, a.lines or None)
    reply = _read(Path(a.reply)) if a.reply else ""

    if not acts:
        print("✗ 动作流水是空的（读了 0 条）—— 没有事实可审，本我不该启动。", file=sys.stderr)
        print("  ⇒ 这不是「没问题」，是「没得审」。先修记录器。", file=sys.stderr)
        return 2

    L = []
    L.append("你是「本我」：只看、只报，不动手。")
    L.append("")
    L.append("## 你的边界（越界就等于没做这件事）")
    L.append("- 只能读，不能改任何东西；不能写文件、不能推分支、不能动平台")
    L.append("- 不能给建议（不要说\"应该改成…\"）；不能评价结论对不对，只能问证据够不够")
    L.append("- 不能说\"我发现了一个问题\"。你只能说\"这一条的 evidence 缺 X，你看算不算\"")
    L.append("- 只跑这一轮，不要追问、不要等附和")
    L.append("")

    L.append("## ① 人要做的是什么")
    if isinstance(intent, dict):
        L.append(intent.get("task") or json.dumps(intent, ensure_ascii=False))
        for c in (intent.get("constraints") or []):
            L.append("- 约束：%s" % c)
        if intent.get("said"):
            L.append("- 原话：%s" % intent["said"])
    else:
        L.append("（没提供 intent.json —— 缺这个，你只能查自相矛盾，查不出\"做的不是人要的\"）")
    L.append("")

    L.append("## ② 什么算完成（开工前定的，不许现在改）")
    if isinstance(done, dict) and done.get("done"):
        for d in done["done"]:
            L.append("- [%s] %s ｜验收命令：`%s` ｜通过标准：%s"
                     % (d.get("id", "?"), d.get("claim", ""),
                        d.get("check", "(缺命令)"), d.get("pass", "")))
    else:
        L.append("（没提供 done.json —— 缺这个，\"完成\"只能靠感觉）")
    L.append("")

    L.append("## ③ 发生了什么（动作流水 1..%d，共 %d 条）" % (len(acts), len(acts)))
    L.append("")
    L.append("| # | 时间 | 动作 | 命令/文件 | rc | 输出摘要 |")
    L.append("|---|---|---|---|---|---|")
    for i, r in enumerate(acts, 1):
        L.append("| %d | %s | %s | %s | %s | %s |" % (
            i, str(r.get("t", ""))[11:19], r.get("act", "?"),
            (r.get("cmd") or "").replace("|", "\\|")[:80],
            r.get("rc") or "-",
            (r.get("out") or "").replace("|", "\\|")[:80]))
    L.append("")

    if reply:
        L.append("## ④ 待审的那段话（自我最后的回复）")
        L.append("")
        L.append("```")
        L.append(reply[:12000])
        L.append("```")
        L.append("")

    L.append("## ⑤ 输出格式（五行，按顺序，不许扩写）")
    L.append("")
    L.append("① 对得上吗   逐条对上面的完成判据，说清哪条对上了、哪条没对")
    L.append("② 我看到的   N 条事实，每条必须带命令或文件名")
    L.append("③ 我怀疑的   对每条可疑的说法，写成一行（**每条以 `- ` 开头**）：")
    L.append("             「<原话或字段> —— evidence 缺 <具体缺什么>」")
    L.append("             做法是**构造反例**：什么情况下这条推理会给出错答案？")
    L.append("             构造不出 ⇒ 只能写\"我构造不出反例\"，**不能说\"它是对的\"**")
    L.append("             ★ 指不出具体位置、说不出缺什么的，**这条作废**，不许写进来")
    L.append("             再另起一行写：`复核：<一条能重跑的命令>` —— 别人跑这条命令，"
             "应当能看出你说的是真是假")
    L.append("             每条末尾**必须**另起一行写：`锚点：第 N 步`（N 见上面流水表的 # 列）")
    L.append("             —— 读者要能顺着这个锚点，**当场看到你批的是哪一步**")
    L.append("④ 我没查的   这次没覆盖什么；**不许写\"无\"**")
    L.append("⑤ 我提醒的   上下文已用 N% —— 这种状态下\"完成\"的判定容易放宽")
    L.append("")
    L.append("★ ③ 是零条也可以，但那样本轮就是**零产出** —— 不会因为\"没找到\"而得分。")

    out = "\n".join(L)
    print(out)
    if a.save:
        Path(a.save).parent.mkdir(parents=True, exist_ok=True)
        Path(a.save).write_text(out, encoding="utf-8")
        print("\n（已存 %s）" % a.save, file=sys.stderr)
    return 0


# ─────────────────────────── check ───────────────────────────

# 判决词：本我只能判断，不能说"通过/合格/准予"（判定权在人）。
# ★ 只在 --free 模式里用，且只产生 warning，不红。
FREE_VERDICT_WORDS = ("验收通过", "判定通过", "判定为通过", "准予上线", "准予发布",
                      "批准上线", "可以上线", "可以通过验收", "合格")


def cmd_check_free(a) -> int:
    """自由形态的检查：**不要求五行**，只守两条底线。

    ★ 为什么放开形状但保留检查（2026-10-28 用户：「本我不用再严格输出 5 行格式了，
      现在本我的测试者身份已经可以支持他畅所欲言了」）：
      形状该放开 —— 五行是它**没有工具**时的笼子，那时只能靠模板逼它给出可核对的位置；
      现在它能写测试、能跑，报告的形状事先规定不了（查 API 和查 UI 差得很远）。

      但有两种病跟形状无关，自由之后反而更容易犯：
      ① **交空话**：五行至少逼它写满五格，自由了可以只回一句"看着还行"；
      ② **替人拍板**：没有格子约束，更容易写下"我认为可以通过"。
      这两条是"判断 vs 判定"那条边界的地面，不能跟着格式一起放开。

    ★ 判红只有一种情况：**几乎没写东西**。写得短、没按建议顺序、没有小标题 ——
      一律不红。检查器在这里的职责是提醒，不是打分。
    """
    text = _read(Path(a.file)) if Path(a.file).exists() else ""
    if not text.strip():
        print(json.dumps({"ok": False, "issues": ["文件为空或不存在"],
                          "verdict": "没有可校验的内容"}, ensure_ascii=False))
        return 2

    # 去掉来源注释行再数长度（`<!-- 本我动手核了 3 次：... -->` 不算它的产出）
    body = re.sub(r"(?m)^\s*<!--.*?-->\s*$", "", text)
    n = len(re.sub(r"\s", "", body))
    warns: list[str] = []
    issues: list[str] = []

    # 判据：有没有一处能被别人核对的痕迹（数字 / 步号 / 括号里的证据 / 文件名）
    # ★ 用这组痕迹而不是字数（2026-10-28 写第一版时踩到）：
    #   按"少于 60 字就算空"判，把「改了 3 个文件，验收 1617 条全过」这种
    #   又短又实的句子判成"没产出"，也把一条只写着「验收通过」的 44 字报告
    #   判成空 —— **短不等于空**。空的本质是"没有任何具体信息"。
    _has_detail = bool(re.search(
        r"\d|第\s*\d+\s*步|（[^）]{2,}）|\([^)]{2,}\)|[\w\-]+\.(py|js|md|json|css|html)",
        body))

    if not _has_detail and n < 60:
        issues.append("既短（%d 字）又没有任何具体内容（没数字、没文件名、"
                      "没写跑过什么）—— 这一轮没产出" % n)
    elif not _has_detail:
        warns.append("通篇没有一处能核对的东西（没有数字、步号、文件名、括号里的证据）"
                     "—— 这更像感想，不像复核")
    elif n < 40:
        warns.append("只有 %d 字 —— 如果确实只查到这么多就没问题，"
                     "否则看看还有哪块没说" % n)

    for w in FREE_VERDICT_WORDS:
        if w in body and ("不" + w) not in body:
            warns.append("出现「%s」—— 判断可以，判定不行；"
                         "换成「我看这样算过 / 我看没过 / 我判不了」" % w)
            break

    ok = not issues
    if ok:
        verdict = ("自由形态，正文 %d 字%s" % (n, "；有 %d 处提醒" % len(warns) if warns else ""))
    else:
        verdict = "没产出：%d 处（只是提醒，不拦流程）" % len(issues)
    print(json.dumps({"ok": ok, "issues": issues, "warnings": warns, "chars": n,
                      "verdict": verdict}, ensure_ascii=False, indent=1))
    # ★★ 退出码**恒 0**（2026-10-28 用户：「完全放开，只把本我规划成测试者」）。
    #   理由不是"懒得管"，是**否决权只该在人手里**：
    #   一个检查器若能凭"你怎么写"让整条流水线失败，它事实上就在判定。
    #   它没写够是事实 —— 写进 JSON 给程序读、给人看，但不许因此把这一轮
    #   判成"没跑成"（上游看到 rc≠0 会当成故障，人看到红字会当成出错）。
    #   文件为空仍返回 2：那不是"写得不好"，是**真的没产出**，两者不能混。
    return 0


def cmd_check(a) -> int:
    # ★ `--free`：正文自由形态（2026-10-28），不要求五行齐全。
    #   走另一套判据 —— 见 cmd_check_free 的说明。
    if getattr(a, "free", False):
        return cmd_check_free(a)

    text = _read(Path(a.file)) if Path(a.file).exists() else ""
    if not text.strip():
        print(json.dumps({"ok": False, "issues": ["文件为空或不存在"],
                          "verdict": "没有可校验的内容"}, ensure_ascii=False))
        return 2

    # ★ --display 模式：校验的是「整份回复」而不是「纯五行文件」
    #   规则来自 2026-10-23 第一次实跑的失败：自我把本我的结论复述了一遍，
    #   读者分不清哪句是谁说的 ⇒ 要求本我独立成块，**且只在块内解析五行**。
    #   附带效果：自我区里引用「③」「⑤」这类编号，不会再把解析带歪。
    if getattr(a, "display", False):
        mk = "━━━ 本我 · 不是自我 ━━━"
        idx = text.find(mk)
        if idx < 0:
            print(json.dumps({
                "ok": False,
                "issues": ["整份回复里找不到本我区块标记「%s」——"
                           "没有它，读者分不清哪句是自我、哪句是本我" % mk],
                "verdict": "显示层分工不合规"}, ensure_ascii=False, indent=1))
            return 1
        text = text[idx + len(mk):]

    # 切五行 —— **只认行首的标记**
    # ★ 曾经的 bug：用 text.find("③") 会在正文里"不校验 ③ 的内容"这种句子处切断，
    #   条数看起来对（有效的还是那几条），但分段是错的 —— 典型的假绿。
    pos: dict[str, tuple[int, int]] = {}
    for m in MARKS:
        mt = re.search(r"(?m)^[ \t]*" + re.escape(m), text)
        if mt:
            pos[m] = (mt.start(), mt.end())
    issues: list[str] = []
    for m in MARKS:
        if m not in pos:
            issues.append("缺少第 %s 行（行首找不到该标记）" % m)
    if issues:
        print(json.dumps({"ok": False, "issues": issues,
                          "verdict": "契约不完整"}, ensure_ascii=False, indent=1))
        return 1

    # 各段正文 = 本标记行首 → 下一个标记行首
    body: dict[str, str] = {}
    for i, m in enumerate(MARKS):
        start = pos[m][1]
        end = pos[MARKS[i + 1]][0] if i + 1 < len(MARKS) else len(text)
        body[m] = _strip_label(m, text[start:end])

    warns: list[str] = []

    # ④ 不许写"无"
    b4 = re.sub(r"[\s。；;,.，]", "", body["④"]).lower()
    if not b4 or b4 in EMPTY_ANSWERS:
        issues.append("④ 写了空/「无」—— 没有它，本我会假装全面")

    # ⑤ 上下文用量：**必须说清这个数是怎么来的**
    #   ★ 起因：出现过"约 97%（不是程序给的数，是我自己估的）"这种写法，
    #     而且**检查器放行了** —— 披露了但没被处理，等于没披露。
    #     ⇒ 只给一个光秃秃的百分比 = 拒收。它看起来像测量结果，实际可能是编的。
    b5 = body["⑤"]
    lead = re.sub(r"^[\s：:、，,。*\-]+", "", b5).strip()
    has_pct = bool(re.search(r"\d+\s*%", b5))
    # ★ 判据只看**开头**，不全文搜词 —— 否则"我当场估的，已删"这种**诚实交代**会被误杀，
    #   结果是逼人把历史藏起来。规则不该惩罚坦白。
    if lead.startswith("实测"):
        if "token" not in b5:
            issues.append("⑤ 自称「实测」但没给 token 数 —— 不可复核")
    elif lead.startswith("测不到"):
        warns.append("⑤ 写的是「测不到」—— 诚实的空值，不是编出来的数")
    elif has_pct:
        issues.append("⑤ 只给了百分比，**没说这数怎么来的**"
                      "（开头必须写「实测」或「测不到」，二选一）—— 这种数无法复核")
    else:
        issues.append("⑤ 没有上下文用量（要写「实测：N token」或「测不到：原因」）")

    # ③ 逐条检（v1.5：**人话优先** —— 每条 = 一句人话 + 一行可跳过的证据）
    #   为什么改：初版要求"指得出位置"，结果输出变成命令/路径/行号堆砌，
    #   能指认但读着费劲 —— 偏离了"把自我做了什么翻译成人话"的初衷。
    #   ⇒ 人话行不许出现反引号/文件路径/命令；证据放第二行，读者可以整段跳过。
    raw = [x for x in body["③"].splitlines() if x.strip()]
    groups: list[list[str]] = []
    prev_was_anchor = False
    for line in raw:
        # ★ 切条两种信号，缺一不可：
        #   ① 以 - / 1. 开头（人写的习惯）
        #   ② **上一行是「锚点：」** —— 每条都以锚点结尾，所以它是天然的分隔符
        #   只有 ① 的话：模型不写 "-" 就会把两条并成一条，
        #   内容明明对，却因为少一个符号被整条吞掉（实测踩过）。
        stripped = re.sub(r"^\s*(?:[-·•*]|\d+[.)])\s*", "", line)
        # 该行是"子字段"吗？（证据/复核/锚点 —— 它们属于上一条，不是新一条）
        is_subfield = bool(re.match(r"^(证据|复核|锚点)[：:]", stripped))
        has_bullet = bool(re.match(r"^\s*(?:[-·•*]|\d+[.)])\s*", line))
        # ★ 三种信号要一起看：
        #   ① 空组
        #   ② 上一行是「锚点：」⇒ 上一条已结束
        #   ③ 有项目符号 **且不是子字段** ⇒ 新一条
        #   只认 ③ 会错：模型常把「证据/复核/锚点」也加 "-"，那样每行都成了新条目
        #   （实测：3 条批注被切成了 17 条，全部作废）。
        if not groups or prev_was_anchor or (has_bullet and not is_subfield):
            groups.append([line.strip()])
        else:
            groups[-1].append(line.strip())
        prev_was_anchor = bool(re.search(r"锚点[：:]", line))

    items, valid, human_issues = [], [], []
    anchored: list[str] = []
    no_anchor: list[str] = []
    rechecked: list[str] = []
    no_recheck: list[str] = []
    retracted: list[str] = []
    for g in groups:
        head = re.sub(r"^\s*(?:[-·•*]|\d+[.)])\s*", "", g[0]).strip()
        ev = " ".join(g[1:]).strip()
        whole = head + " " + ev
        items.append(head or ev)

        # 人话行：必须有不含反引号/路径/命令的一句话
        plain_head = re.sub(r"`[^`]*`", "", head)
        human_ok = bool(plain_head.strip(" :：-—")) and \
            not re.search(r"[\w./\\-]+\.(py|mjs|md|json|jsonl|sh|ps1)\b", plain_head)
        # 证据行：**要求有结构标签，不要求有反引号**。
        #   ★ 原来要求"必须出现 `" —— 那是拿标点当结构的替身：
        #     模型写成 `证据：grep ... → 0`（不带反引号）内容完全对，却被判"缺证据行"。
        #   ⇒ 改成认标签：有「证据：」或「复核：」就算有结构；
        #     至于那条命令能不能真跑，交给 bin/audit.py 管 —— 两件事分开。
        ev_ok = ("`" in ev) or bool(re.search(r"证据[：:]", ev)) or bool(re.search(r"复核[：:]", ev))
        has_pos = any(p.search(whole) for p in POS_PATTERNS)
        has_gap = any(w in whole for w in GAP_WORDS)
        # ★ 锚点：这条批注指的是**哪一步**。没有它，读者看不出它在批哪一段
        #   —— 这正是"两块完全分开会割裂"的根因。
        has_anchor = any(re.search(r"锚点[：:]", ln) for ln in g)
        # ★ 复核：这条判断**能不能被第三方重跑验证**。
        #   起因：出现过一条"两条命令搜的是同一批内容"——听上去对，实际我判断错了。
        #   没有可重跑的复核命令，这种错就永远发现不了。
        has_recheck = any(re.search(r"复核[：:]", ln) for ln in g)
        # 撤回：承认之前那条判断错了。**它不算产出**，只算认错记录。
        is_retract = bool(re.search(r"撤回", head)) or any("撤回" in ln for ln in g[:1])

        if is_retract:
            # 撤回只要"说得出位置 + 有证据"，不要求"缺口词"（它不是在提新缺口）
            if ev_ok and has_pos:
                retracted.append(head)
            else:
                human_issues.append((head[:26] or "(空)", "撤回但缺证据/位置"))
            continue
        # ★ 缺口词从"阻塞条件"降级为"提示条件"。
        #   理由：人话 / 证据 / 复核 / 锚点 **都能机械判**；
        #   而"有没有说清缺什么"只能靠一张词表猜 —— 自然语言一换说法就漏。
        #   实测连续误杀：「会被漏掉，它没查」「谁也无从看见」「那把尺子从没被使用」。
        #   能机械判的才拦，判不准的只提示，别让检查器自己变成噪音源。
        if human_ok and ev_ok and has_pos:
            valid.append(head)
            (anchored if has_anchor else no_anchor).append(head)
            (rechecked if has_recheck else no_recheck).append(head)
            if not has_gap:
                warns.append("③ 这条没明说「缺什么」—— 建议但不算违规：%s" % head[:30])
        else:
            human_issues.append((head[:26] or "(空)",
                                 "缺人话" if not human_ok else
                                 "缺证据行" if not ev_ok else
                                 "缺位置" if not has_pos else "缺缺口词"))

    # 撤回的条目不算"被丢弃" —— 它是认错记录，本来就不该进 valid（不产生产出）
    weak = [it for it in items if it not in valid and it not in retracted]

    if body["③"] and not items:
        issues.append("③ 是空的")
    if items and not valid:
        issues.append("③ 有 %d 条，但**没有一条同时做到「有句人话 + 有证据行 + 指得出位置 + 说得出缺什么」** ⇒ 全部作废"
                      % len(items))
    if no_recheck:
        issues.append("③ 有 %d 条没写「复核：<命令>」—— 不可重跑的判断等于不可验证，"
                      "而「看起来合理但实际判错」的批注正是这么混进来的" % len(no_recheck))
    if no_anchor and anchored:
        warns.append("%d 条 ③ 没写「锚点：第 N 步」—— 读者看不出它在批哪一段（"
                     "这条不影响合规，但会削弱可用性）" % len(no_anchor))
    elif no_anchor and not anchored and getattr(a, "require_anchor", False):
        issues.append("③ 的 %d 条全都没写锚点 —— 要求带锚点，一条都没锚上"
                      % len(no_anchor))

    for name, why in human_issues[:6]:
        warns.append("③ 这条不达标（%s）：%s" % (why, name))

    # 自判语
    for w in SELF_JUDGE:
        if w in text:
            warns.append("出现「%s」—— 本我不能自己判赢了，只能报\"缺什么，你看算不算\"" % w)

    # 空泛回答
    for m in ("③",):
        t = re.sub(r"[\s。；;]", "", body[m])
        if t in ("无明显问题", "无异常", "没问题", "暂未发现", "无"):
            warns.append("③ 写成了「%s」—— 合规但无内容" % body[m][:12])

    ok = not issues
    yield_n = len(valid)
    if yield_n == 0 and ok:
        verdict = "契约合规，但 ③ 0 条 ⇒ **本轮本我零产出**（这不代表自我没问题，只代表本我没说话）"
    elif not ok:
        verdict = "不合规：%d 处" % len(issues)
    else:
        verdict = "契约合规，③ 有效 %d 条（锚定 %d / 可复核 %d / 撤回 %d）" % (
            yield_n, len(anchored), len(rechecked), len(retracted))

    print(json.dumps({"ok": ok, "issues": issues, "warnings": warns,
                      "items_total": len(items), "items_valid": yield_n,
                      "items_dropped": len(weak),
                      "anchored": len(anchored), "not_anchored": len(no_anchor),
                      "rechecked": len(rechecked), "not_rechecked": len(no_recheck),
                      "retracted": len(retracted),
                      "verdict": verdict},
                     ensure_ascii=False, indent=1))
    return 0 if ok else 1


# ─────────────────────────── stats ───────────────────────────

# 从动作流水统计异常和来源。现场钩子、事后重建和测试夹具各用独立计数。
def cmd_stats(a) -> int:
    rows = _load_actions(Path(a.actions))
    if not rows:
        print("比对 0 条 —— **不能当成\"没问题\"**：这是没读到流水。", file=sys.stderr)
        return 2

    print("流水共 %d 条（分母）\n" % len(rows))

    print("① 动作类型分布")
    for k, v in collections.Counter(str(r.get("act")) for r in rows).most_common():
        print("   %-10s %3d 次  (%.0f%%)" % (k, v, 100.0 * v / len(rows)))

    nz = [r for r in rows if str(r.get("rc") or "") not in ("", "0")]
    print("\n② 非零/异常返回码：%d 条 / 共 %d 条" % (len(nz), len(rows)))
    for r in nz[:12]:
        print("   %s  %-8s rc=%-4s %s" % (str(r.get("t", ""))[11:19], r.get("act"),
                                          r.get("rc"), str(r.get("out"))[:60]))
    if not nz:
        print("   （全 0 —— 注意：全 0 也可能因为\"一个都没真正跑\"）")

    files = collections.Counter(f for r in rows for f in (r.get("files") or []))
    print("\n③ 动过的文件：%d 个不同文件" % len(files))
    for f, n in files.most_common(12):
        print("   %-46s %d 次" % (f, n))

    print("\n④ 时间分布")
    for h, v in sorted(collections.Counter(str(r.get("t", ""))[11:13] for r in rows).items()):
        print("   %s 点  %s" % (h, "█" * v))

    # 三态，不是两态：live（钩子写的）/ recon（事后重建）/ fixture（测试夹具，根本没发生过）
    cnt = collections.Counter(str(r.get("src") or "?") for r in rows)
    live, recon, fix = cnt.get("live", 0), cnt.get("recon", 0), cnt.get("fixture", 0)
    print("\n⑤ 来源可信度：live %d / recon %d / fixture %d（共 %d）"
          % (live, recon, fix, len(rows)))
    if recon:
        print("   ⚠ recon %d 条不能用来说\"当时发生了什么\"—— 时间戳是回忆填的" % recon)
    if fix:
        print("   ⚠ fixture %d 条**根本没发生过**，是测试数据 —— 审的时候必须剔掉" % fix)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("build-prompt", help="拼本我 prompt")
    p1.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent))
    p1.add_argument("--intent", default="intent.json")
    p1.add_argument("--done", default="done.json")
    p1.add_argument("--actions", default="")
    p1.add_argument("--lines", type=int, default=0, help="只读前 N 条（快照边界）")
    p1.add_argument("--reply", default="", help="自我最终的回复全文文件")
    p1.add_argument("--save", default="")
    p1.set_defaults(fn=cmd_build_prompt)

    p2 = sub.add_parser("check", help="机械校验本我输出（默认按五行契约；--free 走自由形态）")
    p2.add_argument("file")
    p2.add_argument("--free", action="store_true",
                    help="自由形态：不要求五行，只查「交空话」和「替人拍板」")
    p2.add_argument("--require-anchor", action="store_true", dest="require_anchor",
                    help="要求 ③ 每条都带「锚点：第 N 步」，否则判不合规")
    p2.add_argument("--display", action="store_true",
                    help="按「整份回复」校验：要求有本我区块标记，且只在块内解析五行")
    p2.set_defaults(fn=cmd_check)

    p3 = sub.add_parser("stats", help="动作流水四个查询")
    p3.add_argument("--actions", required=True)
    p3.set_defaults(fn=cmd_stats)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
