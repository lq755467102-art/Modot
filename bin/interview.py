#!/usr/bin/env python3
"""interview.py —— 提案进来之后、写契约之前的那一轮采访

为什么要有这个
    「我建议 → 你拍板」这个循环本身会漂：否决需要替代方案，替代方案需要信息，
    而信息在建议方手里，所以"拍板"退化成"批准"。
    采访制度把议程权推回提案方：**你给方向 → 我问 → 我收敛成方案 → 你拍板方案**。

    但采访有个后门：**问什么是我定的**。我可以只问那些我已有答案的问题，
    把既定方案包装成"采访结论"——问卷设计里这叫引导性提问，比不问更糟，
    因为它披了一层"已经充分了解"的皮。所以这里用三条可核的约束堵它：
      ① 问题单必须落盘（只留答案等于没留）
      ② 必须有一节「反向问」（我理解成 A，如果其实是 B 呢）
      ③ 不可逆的任务**无条件**要采访，不许用"我心里清楚"跳过

它管三件事
    new     产一份模板，省得每次忘字段
    check   验格式：字段齐不齐、该采访的采了没有、反向问有没有
    anchors 列出可被契约引用的锚点（contract.py 用它做出处校验）

★ 天花板（写出来是为了别自欺）
    「反向问」这里只能验**存在**，验不了**真诚**。
    我完全可以写一句敷衍的反向问。这条约束挡的是"忘写"，挡不了"假装写"。
    真正挡得住的是：你看到那句反向问觉得"这不是我的意思"。

用法
    python bin/interview.py new --out itv/2026-10-24.md --irreversible yes
    python bin/interview.py check itv/2026-10-24.md
    python bin/interview.py anchors itv/2026-10-24.md
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

IRR = ("yes", "no", "unknown")

TEMPLATE = """---
irreversible: {irr}
{extra}---

## Q1 （要问清的第一件事）
A1 

## 反向问
（我把它理解成 A。如果其实是 B，那么上面哪一条会不成立？）

## 收敛
- R1 （要做什么，来自 Q1）
- R2 （不做什么，来自 反向问）
"""

TEMPLATE_SKIP = """---
irreversible: no
why_no_interview: （为什么这件事不需要采访 —— 一句话，要经得起反问）
---
"""


def _filled(s: str) -> bool:
    """填了没有。★ 全角括号开头算没填 —— 模板里的占位符就是这种形状，
    不排除它的话，"生成模板 → 直接 check" 会一路绿灯，制度当没建。"""
    s = s.strip()
    return bool(s) and not s.startswith("（")


def _body(lines: list[str], start: int) -> str:
    """从锚点行的下一行取到下一个二级标题为止。"""
    out = []
    for ln in lines[start:]:
        if ln.startswith("## "):
            break
        out.append(ln)
    return "\n".join(out)


def parse(p: Path) -> tuple[dict, dict, str]:
    """(字段, 锚点, 错误)。锚点 = 契约能引用的出处。格式问题全在这里报。"""
    if not p.exists():
        return {}, {}, "文件不存在：%s" % p
    txt = p.read_text(encoding="utf-8", errors="replace")
    if not txt.startswith("---"):
        return {}, {}, "开头没有 --- 字段区（不知道这是不是采访记录）"
    end = txt.find("\n---", 3)
    if end < 0:
        return {}, {}, "字段区没有用 --- 收尾"
    head = txt[3:end]
    meta: dict[str, str] = {}
    for ln in head.splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            meta[k.strip()] = v.strip()

    anchors: dict[str, int] = {}
    lines = txt.splitlines()
    for i, ln in enumerate(lines, 1):
        # ★ 标题后面可以跟问题正文（模板就是 `## Q1 要问清的第一件事`）。
        #   第一版写成 `^##\s*Q(\d+)\s*$`，要求标题后空无一物 —— 于是永远解析不出 Q，
        #   填得再实也判「没采访」。模板和解析器各写各的，没对齐，这是同一类错。
        m = re.match(r"^##\s*Q(\d+)(?![\d])\s*(.*)$", ln)
        if m:
            anchors["q%s" % m.group(1)] = i
        elif re.match(r"^##\s*反向问", ln):
            anchors["counter"] = i
        m2 = re.match(r"^-\s*R(\d+)\s", ln)
        if m2:
            anchors["r%s" % m2.group(1)] = i

    # ① 每个 Q 下面必须有实际回答。
    #    ★ 光判"有非空行"不够：模板里 `A1 ` 去掉空白剩 "A1"，照样算有内容，
    #    于是"生成模板 → 直接 check"全绿。必须把 A 标记剥掉再看剩下什么。
    for name, ln_no in sorted((k, v) for k, v in anchors.items() if k.startswith("q")):
        body = re.sub(r"^A\d+\s*[:：]?", "", _body(lines, ln_no), flags=re.M)
        if not _filled(body.replace("\n", " ")):
            return meta, anchors, "%s（第 %d 行）下面没有实际回答" % (name.upper(), ln_no)

    # ② 反向问不能是空的或占位符
    if "counter" in anchors:
        if not _filled(_body(lines, anchors["counter"]).replace("\n", " ")):
            return meta, anchors, "「## 反向问」下面还是空的或占位符 —— 这一节就是拿来堵引导性提问的"

    # ③ 收敛条目不能是占位符
    for name, ln_no in sorted((k, v) for k, v in anchors.items() if k.startswith("r")):
        seg = re.sub(r"^-\s*R\d+\s*", "", lines[ln_no - 1])
        if not _filled(seg):
            return meta, anchors, "%s（第 %d 行）还是占位符" % (name.upper(), ln_no)
    return meta, anchors, ""


def check(p: Path) -> tuple[int, list[str]]:
    meta, anchors, err = parse(p)
    if err:
        return 2, [err]
    irr = (meta.get("irreversible") or "").lower()
    if irr not in IRR:
        return 2, ["irreversible 必须是 %s 之一，现在是 %r" % ("/".join(IRR), irr)]

    has_q = any(k.startswith("q") for k in anchors)
    if irr == "no" and not has_q:
        why = meta.get("why_no_interview") or ""
        if not _filled(why):
            return 2, ["说不采访，就得给 why_no_interview 一句话理由 —— "
                       "一句话都没有，等于这轮没做判断，只是略过"]
        return 0, ["不采访（理由已留痕：%s）" % why.strip()[:60]]

    if not has_q:
        return 2, ["irreversible=%s 却没采访 —— 不可逆的动作无条件要问清楚" % irr]
    if "counter" not in anchors:
        return 2, ["没有「## 反向问」一节：只问自己已经知道答案的问题，"
                   "采访就变成了给既定方案补一层皮"]
    if not any(k.startswith("r") for k in anchors):
        return 2, ["没有「## 收敛」的 R 条目：问完不收，等于没问"]
    return 0, ["%d 问，含反向问，%d 条收敛" %
               (len([k for k in anchors if k.startswith("q")]),
                len([k for k in anchors if k.startswith("r")]))]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="采访记录：新建 / 校验 / 列出可引用锚点")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a1 = sub.add_parser("new", help="产一份模板")
    a1.add_argument("--out", required=True)
    # ★ 默认不能给 unknown：--skip 的本意就是"不采访"，unknown 会被自己的闸拦住。
    #   第一版这里写死 default="unknown"，于是 --skip 永远失败 —— 新工具第一版就有 bug。
    a1.add_argument("--irreversible", default=None, choices=IRR)
    a1.add_argument("--skip", action="store_true",
                    help="这次不采访，只写不采访的理由")

    a2 = sub.add_parser("check", help="验格式与约束")
    a2.add_argument("file")

    a3 = sub.add_parser("anchors", help="列出契约能引用的出处")
    a3.add_argument("file")

    a = ap.parse_args(argv)
    if a.cmd == "new":
        p = Path(a.out)
        if p.exists():
            print("× %s 已经有了。改它，别覆盖。" % p)
            return 2
        p.parent.mkdir(parents=True, exist_ok=True)
        irr = a.irreversible or ("no" if a.skip else "unknown")
        if a.skip:
            if irr != "no":
                print("× 不可逆的任务不许 --skip —— 无条件要采访")
                return 2
            p.write_text(TEMPLATE_SKIP, encoding="utf-8")
        else:
            extra = "" if irr != "no" else "why_no_interview: \n"
            p.write_text(TEMPLATE.format(irr=irr, extra=extra), encoding="utf-8")
        print("写了 %s" % p)
        print("下一步：把 Q/A 填实 → check → 契约里每条 done 用 ::q1 这样的引用指回出处")
        return 0

    p = Path(a.file)
    if a.cmd == "check":
        rc, msgs = check(p)
        for m in msgs:
            print(("✓ " if rc == 0 else "× ") + m)
        return rc

    meta, anchors, err = parse(p)
    if err:
        print("×", err)
        return 2
    for k in sorted(anchors):
        print("%s\t第 %d 行" % (k, anchors[k]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
