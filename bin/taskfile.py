#!/usr/bin/env python3
"""taskfile.py —— 一个文件说清这一轮：做什么、什么算完、**不许做什么**

为什么要把三份合成一份
    之前同一件事存在三处：`--task` 参数说做什么、`done.json` 说算什么完、
    `interview.md` 说为什么这么定。改一处忘另两处，三份互相矛盾，
    而且**没有任何检查器能发现**——因为每份单看都自洽。

    现在 task.md 是源：frontmatter 放判据和边界，正文放任务。
    `done.json` 降级成它的规范化快照，只用来加锁和对比。

为什么不是纯 json
    人要先能读、能改。md 可以先随手写，字段用着用着自己长出来；
    反过来把规范文件渲染成表单容易，把表单变成能读的文件很难。

它管三件事
    new     产模板
    check   验格式（done 非空、每条的 check 是可跑命令、not_build 不空着糊弄）
    show    把解析结果打出来，人一眼对

字段（每行列表项用 :: 分段，跟 contract.py 的 --done 同一套写法）
    done:
      - 说明这一轮要什么::python bin/accept.py::退出码 0::q1
        第 4 段是采访出处，没有采访就省掉
    not_build:
      - 不做交互式确认弹窗
      - 不加第三方依赖::! python -c "import yaml"

★ not_build 的两种写法，区别要说清
    只写一句话 = **只能人核**。它会被列进复核单，但机器验不了。
    写了 ::check = 能自动验，check 命令退出码 0 才算"确实没做"。
    别把"写了一句"当成"验过了"。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

TEMPLATE = """---
done:
  - （这一轮要什么）::python bin/accept.py::退出码 0
not_build:
  - （这一轮明确不做什么 —— 写具体，「不做多余的事」等于没写）
---

# 这一轮要什么

（正文写清楚：给谁、解决什么、什么时候算完。改这段等于改需求，
 所以它的哈希会被记进契约——改了 verify 会红。）
"""


def body_sha(body: str) -> str:
    """正文的指纹。★ 改正文等于改需求，但正文不参与 json 锁，
    所以单独算一个哈希放进契约里，verify 时对比。"""
    norm = "\n".join(x.rstrip() for x in body.strip().splitlines())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def parse(p: Path) -> tuple[dict[str, list[str]], str, str]:
    """(字段, 正文, 错误)。字段的值都是字符串列表，列表项本身不做拆分。"""
    if not p.exists():
        return {}, "", "文件不存在：%s" % p
    txt = p.read_text(encoding="utf-8", errors="replace")
    if not txt.startswith("---"):
        return {}, "", "开头没有 --- 字段区"
    end = txt.find("\n---", 3)
    if end < 0:
        return {}, "", "字段区没有用 --- 收尾"
    head = txt[3:end]
    body = txt[end + 4:]

    meta: dict[str, list[str]] = {}
    cur = None
    for ln in head.splitlines():
        if not ln.strip() or ln.strip().startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z_]+):\s*(.*)$", ln)
        if m and not ln.startswith((" ", "\t")):
            cur = m.group(1)
            meta[cur] = []
            if m.group(2).strip():
                meta[cur].append(m.group(2).strip())
            continue
        m2 = re.match(r"^\s+-\s*(.*)$", ln)
        if m2 and cur:
            meta[cur].append(m2.group(1).strip())
    return meta, body, ""


def check(p: Path) -> tuple[int, list[str], dict, str]:
    meta, body, err = parse(p)
    if err:
        return 2, [err], meta, body
    msgs: list[str] = []
    rc = 0

    done = meta.get("done") or []
    if not done:
        return 2, ["done 是空的 —— 没有判据的契约只能靠感觉验收"], meta, body
    for i, raw in enumerate(done, 1):
        parts = [x.strip() for x in raw.split("::")]
        if len(parts) < 2 or not parts[1]:
            rc = 2
            msgs.append("done 第 %d 条只有说明、没有 check 命令（要能跑的，不是形容词）" % i)
        elif parts[0].startswith("（"):
            rc = 2
            msgs.append("done 第 %d 条还是模板占位符" % i)

    nb = meta.get("not_build") or []
    if not nb:
        rc = 2
        msgs.append("not_build 是空的。★ 这不是可选项：只有正面清单时，"
                    '"顺手多做"没有任何东西挡得住，事后也验不出来')
    for i, raw in enumerate(nb, 1):
        if raw.strip().startswith("（"):
            rc = 2
            msgs.append("not_build 第 %d 条还是模板占位符" % i)
        elif len(raw.strip()) < 4:
            rc = 2
            msgs.append("not_build 第 %d 条太短（%r），写具体点，否则没法核" % (i, raw))

    if not body.strip():
        rc = 2
        msgs.append("正文是空的 —— 判据可以很短，但「为什么做」要写一句")

    if rc == 0:
        msgs.append("done %d 条 / not_build %d 条（可自验 %d 条，只能人核 %d 条）/ 正文 %d 字"
                    % (len(done), len(nb),
                       sum(1 for x in nb if "::" in x),
                       sum(1 for x in nb if "::" not in x),
                       len(body.strip())))
    return rc, msgs, meta, body


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="task.md：做什么 / 什么算完 / 不许做什么")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("new", help="产模板")
    a1.add_argument("--out", required=True)
    a2 = sub.add_parser("check", help="验格式")
    a2.add_argument("file")
    a3 = sub.add_parser("show", help="把解析结果打出来")
    a3.add_argument("file")
    a = ap.parse_args(argv)

    if a.cmd == "new":
        p = Path(a.out)
        if p.exists():
            print("× %s 已经有了。改它，别覆盖。" % p)
            return 2
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TEMPLATE, encoding="utf-8")
        print("写了 %s" % p)
        print("下一步：填 done / not_build / 正文 → check → contract.py new --from-task")
        return 0

    p = Path(a.file)
    if a.cmd == "check":
        rc, msgs, _, _ = check(p)
        for m in msgs:
            print(("✓ " if rc == 0 else "× ") + m)
        return rc

    meta, body, err = parse(p)
    if err:
        print("×", err)
        return 2
    for k, v in meta.items():
        print("%s:" % k)
        for x in v:
            print("  - %s" % x)
    print("\n正文 %d 字，指纹 %s" % (len(body.strip()), body_sha(body)[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
