#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正文体检：说出来的那几段，是在讲结论，还是在讲「我干了什么」。

为什么要它（2026-10-27）
    用户的原话是「没必要所有东西都挤在 say 里」。实测一条回答 2059 字，
    开头是「我读了这两个文件头部完整的 docstring（前 4000/17315 字）」——
    过程占了正文，判断被挤到下面。改提示词之后掉到 1553 字、第一段是结论。
    但**改提示词是一次性的，退回去是持续的**：不加判据，过两周又会长回来。

判据怎么选（都不用数字代替判断）
    ① 开头就在讲过程：前 80 字里出现「我读了 / 我跑了 / 我先看 / 让我先 / 接下来我」；
    ② 分步清单：「第一步 / 步骤 1 / 第 1 步」这种叙述；
    ③ 贴整段输出：正文里连续 12 行以 `#` / `{` / `|` / `$` 开头的块（该给位置，不该抄全文）。
    ★ 长度**只报不判**：一句话说清一件事可以很长，说得再多也不等于跑题。

用法
    python bin/probe-say.py                  # 扫最近 20 个会话，列出可疑处
    python bin/probe-say.py --self-test      # 用合成样本证明判据能红也能绿
    python bin/probe-say.py --session runs/xxx
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ① 开头讲过程
HEAD_PROC = re.compile(r"(我(先)?读[了过]|我跑了|我先看|让我先|接下来我|我再去|我接下来)")
# ② 分步清单
STEPPING = re.compile(r"(第[一二三四五六七八九十1-9][步段]|步骤\s*[1-9]|Step\s*[1-9])", re.I)


def block_lines(text: str) -> int:
    """连续「像输出」的行数最大值：从 ``` 块里数，或者纯文本里以符号开头的连续行。"""
    worst = 0
    cur = 0
    for ln in text.splitlines():
        s = ln.strip()
        looks = bool(s) and (s[0] in "#{|$<>" or s.startswith("PS ") or s.startswith(">>>"))
        if looks:
            cur += 1
            worst = max(worst, cur)
        elif not s:
            continue
        else:
            cur = 0
    return worst


def check_text(t: str) -> list:
    """返回问题清单（空 = 这一段是结论）。"""
    bad = []
    head = t.strip()[:80]
    m = HEAD_PROC.search(head)
    if m:
        bad.append("开头在讲过程（「%s」）" % m.group(0))
    m2 = STEPPING.search(t)
    if m2:
        bad.append("正文里有分步叙述（「%s」）" % m2.group(0))
    n = block_lines(t)
    if n >= 12:
        bad.append("贴了 %d 行输出（该给位置）" % n)
    return bad


# 直接访问会话事件文件，只把 `say` 正文交给规则检查，报告中的行号沿用原日志。
def scan(session: Path) -> list:
    out = []
    try:
        lines = (session / "events.jsonl").read_text(encoding="utf-8",
                                                     errors="replace").splitlines()
    except OSError:
        return out
    for i, line in enumerate(lines):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("act") != "say":
            continue
        t = e.get("text") or ""
        if not t.strip():
            continue
        bad = check_text(t)
        if bad:
            out.append({"session": session.name, "line": i + 1, "chars": len(t),
                        "why": bad, "head": t.strip()[:60]})
    return out


FIXTURE_BAD = ("我读了这两个文件头部完整的 docstring（分别是 isolate.py 的前 4000/17315 字，"
               "和 contain.py 的前 4000/27415 字，两段都被我读到了结尾，没有截断）。\n\n"
               "第一步先看 isolate，第二步看 contain，第三步对比。\n" + "\n".join(
                   '{"ok": true, "line": %d}' % i for i in range(14)))
FIXTURE_GOOD = ("两层是叠加关系：isolate 管窗口与资源，contain 补文件（默认关）。\n"
                "证据：loop/isolate.py:3-45、loop/contain.py:3-39。")


def self_test() -> int:
    """判据自己先过关：坏样本必须红，好样本必须绿。"""
    ok = 0
    tot = 0
    for text, want_bad, name in ((FIXTURE_BAD, True, "坏样本（过程复述）"),
                                 (FIXTURE_GOOD, False, "好样本（结论 + 位置）")):
        tot += 1
        bad = check_text(text)
        got = bool(bad)
        good = got == want_bad
        ok += 1 if good else 0
        print(("  PASS  " if good else "  FAIL  ") + name
              + ("" if good else "  → 实际判定 %r" % (bad,)))
    print("%d/%d" % (ok, tot))
    return 0 if ok == tot else 1


# 显式会话和最近会话使用同一检查器，返回码区分存在可疑正文与未发现命中。
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--session", default="")
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    if a.session:
        cands = [Path(a.session)]
    else:
        base = ROOT / "runs"
        cands = sorted([p for p in base.glob("*") if p.is_dir()]
                       + [p for p in (base / "sessions").glob("*") if p.is_dir()],
                       key=lambda p: (p / "events.jsonl").stat().st_mtime
                       if (p / "events.jsonl").exists() else 0)[-a.limit:]

    hits, scanned, total_chars, says = [], 0, 0, 0
    for c in cands:
        if not (c / "events.jsonl").exists():
            continue
        scanned += 1
        hits += scan(c)
        for line in (c / "events.jsonl").read_text(encoding="utf-8",
                                                   errors="replace").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("act") == "say" and (e.get("text") or "").strip():
                says += 1
                total_chars += len(e["text"])

    print("扫了 %d 个会话，正文 %d 段，合计 %d 字（中位 %d 字）"
          % (scanned, says, total_chars, (total_chars // says) if says else 0))
    if not hits:
        print("没有发现「正文在讲过程」的段落。")
        return 0
    print("发现 %d 段可疑：" % len(hits))
    for h in hits:
        print("  · %s 第 %d 行（%d 字）：%s" % (h["session"], h["line"], h["chars"],
                                              "；".join(h["why"])))
        print("    %s…" % h["head"].replace("\n", " "))
    return 1


if __name__ == "__main__":
    sys.exit(main())
