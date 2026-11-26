#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tree-suggest.py —— 给出「哪些条目该降级 / 该复核」的**候选清单**，不改任何东西。

它做什么
    给每条记忆打一个粗糙的分，然后按分给出建议：保持 / 复核 / 建议降级。
    每条建议都带理由和证据（字数、年龄、缺哪一槽），你看着决定。
    **本工具不写文件**——落笔由 `bin/tree-state.py set` 去做，且必须由人触发。

为什么不做「自动降级」
    自动改 state 等于让机器替人判断「这条还重不重要」。这个仓库已有的结论是：
    机器只能提候选（见 `bin/distill.py` 开头），落笔要人。
    另一个更硬的理由：降级会让一条规则从下一轮开始消失，而**消失是无声的**
    —— 你只会在某次它本该拦住你的时候发现它不在了。

三个信号（都是弱的，所以只用来排序、不用来定罪）
    ① 结构不全：填不出「触发」或「违反」⇒ 按本仓库的判据，它是解释而不是约束
    ② 年龄：多久没被改动（只对树有效；习惯区在仓库外，没有版本史）
    ③ 体积：占多少注入字符 —— 大的更值得审视，但大不等于该删

用法
    python bin/tree-suggest.py              # 打清单
    python bin/tree-suggest.py --json       # 给脚本
    python bin/tree-suggest.py --min-chars 100    # 低于这个字数不查三槽（指针类）
★ 只读：不写文件、不改 state。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent
ROOT = BIN.parent
AGE_DAYS = 60          # 多少天没动算「老」
BIG_CHARS = 260        # 多少字算「体积大」
POINTER_CHARS = 100    # 低于这个字数当作指针/短规则，不查三槽


def _load(name: str):
    """加载同目录的脚本（复用它的解析/评分逻辑，避免两套口径）。"""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def body_of(node: dict) -> str:
    """取节点正文：从标题行到下一个同级标题之前。"""
    path = node["file"]
    lines = path.read_text(encoding="utf-8").split("\n")
    start = node["line"] - 1
    level = "#" * (3 if path.name == "HABITS.md" else 2) + " "
    out = []
    for ln in lines[start + 1:]:
        if ln.startswith(level) or ln.startswith("# "):
            break
        out.append(ln)
    return "\n".join(out)


def slots(body: str) -> dict:
    """三槽有没有填。只认 `触发：` / `违反：` 这两个标记。"""
    return {"触发": "触发" in body, "违反": "违反" in body}


def ages() -> dict:
    """树节点的年龄（天）。取 git blame 到**标题行**——复用 tree-age 的实现。"""
    try:
        ta = _load("tree-age")
        rows = ta.collect(0)          # 阈值 0 ⇒ 全部返回，含 days
        return {(r["file"], r["node"]): r["days"] for r in rows}
    except Exception:
        return {}


STYLE_PREFIXES = ("tone.",)          # 风格类：怎么写。违反能感知，但列不全
STYLE_MARKERS = ("反例", "正例", "不要", "别用", "不是", "不算")
POINTER_MARKERS = ("HABITS.md", "见习惯区")   # 指针条目：正文就是"去别处看"，不评分

def judge(n: dict, chars: int, age, sl: dict, min_chars: int, body: str = "") -> tuple:
    """返回 (建议, 分数, 理由列表)。

    ★ 两类规则用两套判据（2026-10-29 补）：硬套三槽会把「写作姿态」全判死。
      风格类（`tone.*`）的特征是**违反能被感知、但没法枚举**（"语气像机器"），
      要求它写「触发/违反」只会逼出一句假判据。对它改问：
      「有没有给出正反例或明确禁止」—— 这才是风格类能落地的形态。
      约束类（guard./accept./memory./flow.）仍然要求三槽：它们能判。
    """
    score, why = 0, []
    is_style = (n.get("key") or "").startswith(STYLE_PREFIXES)
    is_pointer = any(w in body for w in POINTER_MARKERS)

    if is_pointer:
        pass                     # 指针条目：正文本身就是「去别处看」，不按规则评
    elif is_style:
        if not any(w in body for w in STYLE_MARKERS):
            score += 1
            why.append("风格类但没给正反例：读者不知道「不对」长什么样")
    elif chars >= min_chars:
        if not sl["触发"]:
            score += 2
            why.append("缺「触发」：说不清什么时候该想起它")
        if not sl["违反"]:
            score += 2
            why.append("缺「违反」：判不出有没有被违反 ⇒ 是解释不是约束")

    if age is not None and age > AGE_DAYS:
        score += 1
        why.append("已经 %.0f 天没被改动" % age)
    if chars > BIG_CHARS:
        score += 1
        why.append("占 %d 字符，体积偏大" % chars)

    if score >= 2:
        return "建议降级", score, why
    if score == 1:
        return "复核", score, why
    return "保持", score, why


def main() -> int:
    ap = argparse.ArgumentParser(description="给出降级/复核的候选清单（只读）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--audit", action="store_true",
                    help="审「谁在保证这条规则执行」（code / hook / human / none）")
    ap.add_argument("--min-chars", type=int, default=POINTER_CHARS,
                    help="低于这个字数不查三槽（指针/短规则），默认 100")
    a = ap.parse_args()

    ts = _load("tree-state")
    nodes = ts.all_nodes(True)
    age_map = ages()

    rows = []
    for n in nodes:
        body = body_of(n)
        chars = len(body.strip())
        age = age_map.get((n["file"].name, n["title"]))
        sl = slots(body)
        verdict, score, why = judge(n, chars, age, sl, a.min_chars, body)
        rows.append({
            "verdict": verdict, "score": score, "key": n["key"], "title": n["title"],
            "file": n["file"].name, "line": n["line"], "state": n["state"],
            "by": n.get("by"),
            "chars": chars, "age": None if age is None else round(age, 1),
            "slots": sl, "why": why,
        })

    order = {"建议降级": 0, "复核": 1, "保持": 2}
    rows.sort(key=lambda r: (order[r["verdict"]], -r["score"], -r["chars"]))

    if a.audit:
        # 「谁在保证这条规则」审一遍：比「有没有被引用」可判得多。
        # code=机器执行 / hook=钩子执行 / human=靠人看 / none=没人管 / 未标=待定
        rank = {None: 0, "none": 1, "human": 2, "hook": 3, "code": 4}
        mark = {None: "？ 待定", "none": "✗ 没人管", "human": "⚠ 靠人看",
                "hook": "✓ 钩子", "code": "✓ 机器"}
        rows.sort(key=lambda r: rank.get(r["by"], 0))
        print("执行者审计｜%d 条" % len(rows))
        print()
        print("| 执行者 | 主题键 | 标题 |")
        print("|---|---|---|")
        for r in rows:
            print("| %s | %s | %s |" % (mark.get(r["by"], r["by"]), r["key"] or "—",
                                        r["title"][:22]))
        cnt = {}
        for r in rows:
            cnt[r["by"]] = cnt.get(r["by"], 0) + 1
        print()
        print("统计：" + " · ".join("%s %d" % (mark.get(k, k), v) for k, v in sorted(
            cnt.items(), key=lambda x: rank.get(x[0], 0))))
        print()
        print("★ 「没人管」和「靠人看」是两类最该动土的：前者靠自觉，后者要等你有空。")
        print("  标法：在 key 注释里加 ` | by: code`（或 hook / human / none）。")
        return 0

    if a.json:
        print(json.dumps({"total": len(rows), "rows": rows}, ensure_ascii=False, indent=2))
        return 0

    down = [r for r in rows if r["verdict"] == "建议降级"]
    check = [r for r in rows if r["verdict"] == "复核"]
    print("候选清单｜%d 条（建议降级 %d · 复核 %d · 保持 %d）"
          % (len(rows), len(down), len(check), len(rows) - len(down) - len(check)))
    print()
    print("| 建议 | 主题键 | 字数 | 年龄 | 理由 |")
    print("|---|---|---|---|---|")
    for r in rows:
        if r["verdict"] == "保持":
            continue
        print("| %s | %s | %d | %s | %s |"
              % (r["verdict"], r["key"] or "—", r["chars"],
                 ("%.0f 天" % r["age"]) if r["age"] is not None else "未知",
                 "；".join(r["why"]) or "—"))

    if not down and not check:
        print()
        print("⇒ 没有候选。注意这不代表「都该保留」，只代表**这三个信号都没响**：")
        print("  结构是全的、没有超龄、体积也不大 —— 但它们有没有用，只有你知道。")

    if down:
        print()
        print("⇒ 要动手的话，逐条执行（一条一条来，改完跑 charter.py budget 看变化）：")
        for r in down:
            print("   python bin/tree-state.py set --key %s --state reference" % r["key"])

    print()
    print("★ 本工具不改任何东西。降级会让一条规则从下一轮**无声消失** ——")
    print("  所以「建议降级」只是建议；你判断它仍然必要时，原样留着。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
