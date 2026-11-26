#!/usr/bin/env python3
"""health.py —— 检查器体检：这 300 条验收到底覆盖了什么

为什么需要这个
    业务方的原话：「这个设计检查器的过程对我来说其实是相当不可见的。
    我并不清楚我们的系统是否健康，跟其他厂商的差距又在哪里。」

    这句话是对的，而且是我造成的。我一直在加验收（127 → 153 → 183 → … → 309），
    但**没有一处能让人一眼看出"这些验收到底盖住了什么、漏了什么"**。
    数字在涨，可见性没涨。

它回答三个问题
    ① **都验了什么** —— 94 组、300 条，按模块分。
    ② **哪些是弱断言** —— grep 源码字符串的那种。改实现不改行为它会红；
       行为坏了但字符串还在它会绿。这类必须单独标出来。
    ③ **漏了什么** —— 有没有代码存在、却一条验收都没碰过。

★ 一条纪律（和 board.py 一样）
    每个数字都要能追到 `bin/accept.py` 里的具体组号。页面上不出现"只有我知道"的数。

用法
    python bin/health.py            # 写 runs/health.html
    python bin/health.py --open
"""
from __future__ import annotations

import argparse
import ast
import html
import re
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACC = ROOT / "bin" / "accept.py"

# ★ 和同行的差距：这张表是**静态**的，来源是 docs/research.md 已经查证过的
#   （每一行在 research.md 里都有出处）。不扫代码，因为它描述的是"别人有什么"。
PEERS = [
    ("拦截式钩子", "PreToolUse 可以 deny 工具调用",
     "只有 PostToolUse，而且 fail open —— 只记录，不拦", "❌ 硬差距"),
    ("权限模式", "Manual / acceptEdits / plan 等几档，按工具翻",
     "zone 三档 + 人点头，没有「按会话切档」", "⚠ 半"),
    ("操作系统级隔离", "容器 / 受限 shell / 独立 worktree",
     "无。agent 和人在同一个目录里跑", "❌ 硬差距"),
    ("子 agent", "内置 Explore / Plan / general-purpose；md 定义、"
                 "工具白名单是执行边界、可配 worktree 隔离、maxTurns 防跑飞",
     "**零**。框架层已经隔离好（读账本/停止回调/事件序号实测不串），"
     "但没有 spawn、没有只读工具集", "❌ 硬差距"),
    ("显示层注入", "MessageDisplay：改显示不改转写、非阻塞",
     "离线渲染 HTML，事后才看得到", "⚠ 半"),
    ("验收可重跑", "内部有 eval，但不开源、不可复核",
     "**全部断言离线可重跑**，accept.py 一条命令", "✅ 我们更好"),
    ("子 agent 的交付验收", "父读子返回的报告；没有机械判据",
     "`done.json` 哈希锁 + 逐条 check + blocked 出口 —— **做出来了，但还没接到子 agent 上**",
     "⚠ 机制更好但没用上"),
    ("人能看到逐步过程", "转写里有，但要翻",
     "render.py 一页、board.py 一览、本我批注贴到步骤上", "✅ 我们更好"),
]


# ★ 纯文本匹配不一定是坏事。下面这几组验的**本来就是文本**（文档一致性、
#   提示词不漂移、夹具内容），用字符串匹配是合理的。
#   其余「纯文本组」才是**疑似弱断言**：它们想验行为，却只在验代码里有没有那句话。
#   ★ 这份清单要人维护 —— 机器判不出"这组到底想验什么"。加组时必须想一遍。
TEXTY_IS_FINE = {
    "d38": "验的就是两份 prompt 文本不漂移",
    "d81": "验的就是文档里没有自相矛盾的说法",
    "d90": "验的是 T2 夹具里那个坑还在（夹具内容）",
    "d91": "验的是 T4 夹具三处矛盾都在（夹具内容）",
}

# ★ 还有一档：**离线根本验不了行为**，只能真跑一次模型。
#   不能把它们算作"弱"（那是冤枉），也不能算作"没问题"（那是自欺）。
#   单列出来，标明"这条要靠联调兜"。
NEEDS_LIVE = {
    "d84": "要真跑一次 run-task 才知道自动渲染有没有触发",
    "d94": "要真跑一次 bench 才知道本我有没有被叫起来",
}


def parse_groups() -> list[dict]:
    """把 accept.py 按 `print("=== dNN 说明 ===")` 切开，数每组的 chk。

    ★ 用 ast 而不是逐行正则。第一版是逐行扫的，数出 303 条，
      而 accept.py 实际跑出 310 条 —— **报告的数字和实测对不上，这报告就没用了**。
      差的 7 条是参数换行的 chk（正则按行匹配会漏）。
      ⇒ 数字要能被追，就别用近似方法去数它。
    """
    src = ACC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    marks, chks = [], []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id == "print" and node.args:
            a0 = node.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                m = re.match(r"=== (d\d+) ?(.*?) ?===", a0.value)
                if m:
                    marks.append((node.lineno, m.group(1), m.group(2)))
        elif node.func.id == "chk":
            seg = ast.get_source_segment(src, node) or ""
            # 弱断言：拿源码字符串做成员判断（`"xxx" in _src` / `in txt(...)`）
            weak = bool(re.search(r"\bin\s+(_\w+|txt\()", seg))
            chks.append((node.lineno, weak))

    marks.sort()
    groups = []
    for i, (ln, gid, desc) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else 10 ** 9
        mine = [w for (l, w) in chks if ln <= l < end]
        groups.append({"id": gid, "desc": desc,
                       "total": len(mine), "texty": sum(1 for x in mine if x)})
    return groups


def file_coverage() -> list[dict]:
    src = ACC.read_text(encoding="utf-8")
    rows = []
    for d in ("bin", "loop"):
        for f in sorted((ROOT / d).glob("*.py")):
            if f.name == "accept.py":
                continue
            rows.append({
                "name": "%s/%s" % (d, f.name),
                "lines": len(f.read_text(encoding="utf-8").splitlines()),
                "hit": (f.name in src) or (f.stem in src),
            })
    return rows


CSS = """
*{box-sizing:border-box}
body{margin:0;padding:32px 24px 64px;background:#FAF9F6;color:#2C2C2A;
     font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:20px;font-weight:500;margin:0 0 4px}
h2{font-size:15px;font-weight:500;margin:32px 0 10px}
.sub{color:#5F5E5A;margin:0 0 24px;font-size:13px}
.k{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 8px}
.k div{background:#fff;border:1px solid #E5E3DD;border-radius:10px;padding:12px 16px;min-width:130px}
.k b{display:block;font-size:22px;font-weight:500;line-height:1.2}
.k span{color:#5F5E5A;font-size:12px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #E5E3DD;
      border-radius:10px;overflow:hidden;font-size:13px}
th,td{text-align:left;padding:7px 12px;border-bottom:1px solid #F1EFE8}
th{background:#F1EFE8;font-weight:500;font-size:12px;color:#444441}
tr:last-child td{border-bottom:none}
td.n{text-align:right;font-family:ui-monospace,monospace}
.weak{color:#993C1D}
.ok{color:#0F6E56}
.bad{color:#A32D2D}
.mono{font-family:ui-monospace,monospace;font-size:12px}
.note{background:#F1EFE8;border-radius:10px;padding:14px 16px;margin:16px 0;color:#444441;font-size:13px}
"""


def build(groups: list[dict], files: list[dict]) -> str:
    tot = sum(g["total"] for g in groups)
    weak_groups = [g for g in groups if g["total"] and g["texty"] == g["total"]]
    fine = [g for g in weak_groups if g["id"] in TEXTY_IS_FINE]
    live = [g for g in weak_groups if g["id"] in NEEDS_LIVE]
    real_weak = [g for g in weak_groups if g not in fine and g not in live]
    missing = [f for f in files if not f["hit"]]

    rows = "".join(
        "<tr><td class='mono'>%s</td><td>%s</td><td class='n'>%d</td><td class='n %s'>%d</td></tr>"
        % (html.escape(g["id"]), html.escape(g["desc"]),
           g["total"],
           "weak" if g["id"] in [x["id"] for x in real_weak] else "",
           g["texty"])
        for g in groups)

    frows = "".join(
        "<tr><td class='mono'>%s</td><td class='n'>%d</td><td>%s</td></tr>"
        % (html.escape(f["name"]), f["lines"],
           "<span class='ok'>✓ 有验收提到</span>" if f["hit"]
           else "<span class='bad'>✗ 一条都没有</span>")
        for f in files)

    prows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (html.escape(a), html.escape(b), html.escape(c), html.escape(d))
        for a, b, c, d in PEERS)

    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>检查器体检</title><style>%s</style></head><body><div class="wrap">
<h1>检查器体检</h1>
<p class="sub">数据全部解析自 <span class="mono">bin/accept.py</span>，没有任何手工填的数。
★ 这里数的是**代码里 <span class="mono">chk(</span> 的调用点**（%d 个）。循环里的会重复执行，
所以实际跑的次数更多 —— **以 <span class="mono">accept.py</span> 结尾那行输出为准**，别把这里的数当成通过数。</p>

<div class="k">
  <div><b>%d</b><span>组验收</span></div>
  <div><b>%d</b><span>个断言调用点</span></div>
  <div><b>%d</b><span>个代码文件被提到</span></div>
  <div><b class="weak">%d</b><span>组疑似弱断言</span></div>
  <div><b class="bad">%d</b><span>个文件没人验</span></div>
</div>

<div class="note">
<b>怎么读这张表。</b>「文本匹配」指的是断言在用源码字符串做成员判断
（<span class="mono">"xxx" in _src</span>），而不是跑一遍看行为。
它的毛病是双向的：<b>改实现不改行为，它会红；行为坏了但字符串还在，它会绿。</b>
所以「纯文本匹配」的组要单独看 —— 有的合理（验的就是文档一致性），有的就是弱。
</div>

<h2>逐组清点</h2>
<table><tr><th>组</th><th>说明</th><th>断言</th><th>其中文本匹配</th></tr>%s</table>

<h2>纯文本匹配的组：%d 组，分三档</h2>
<p class="sub"><b>合理的</b>（验的就是文本，字符串匹配没错）%d 组：%s</p>
<p class="sub"><b>只能联调验</b>（离线验不了行为，要走真跑）%d 组：%s</p>
<p class="sub"><b class="weak">疑似弱</b>（想验行为，却只在验代码里有没有那句话）%d 组：%s</p>

<h2>代码覆盖：有没有人验它</h2>
<table><tr><th>文件</th><th>行数</th><th>状态</th></tr>%s</table>

<h2>和同行的差距</h2>
<p class="sub">这张表是静态的，来源见 <span class="mono">docs/research.md</span> 各节 —— 不扫代码，因为描述的是别人有什么。</p>
<table><tr><th>能力</th><th>同行</th><th>我们</th><th>结论</th></tr>%s</table>

</div></body></html>""" % (
        CSS, tot, len(groups), tot, len(files) - len(missing),
        len(real_weak), len(missing),
        rows,
        len(weak_groups),
        len(fine), html.escape("、".join("%s（%s）" % (g["id"], TEXTY_IS_FINE[g["id"]])
                                          for g in fine)) or "无",
        len(live), html.escape("、".join("%s（%s）" % (g["id"], NEEDS_LIVE[g["id"]])
                                          for g in live)) or "无",
        len(real_weak), html.escape("、".join(g["id"] for g in real_weak)) or "无",
        frows, prows,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="检查器体检：验收覆盖了什么、漏了什么")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)

    groups = parse_groups()
    files = file_coverage()
    out = Path(a.out) if a.out else ROOT / "runs" / "health.html"
    out.write_text(build(groups, files), encoding="utf-8")

    tot = sum(g["total"] for g in groups)
    weak = [g for g in groups if g["total"] and g["texty"] == g["total"]]
    fine = [g for g in weak if g["id"] in TEXTY_IS_FINE]
    live = [g for g in weak if g["id"] in NEEDS_LIVE]
    real = [g for g in weak if g not in fine and g not in live]
    missing = [f["name"] for f in files if not f["hit"]]
    print("已生成：%s" % out)
    print("  %d 组 ／ %d 个断言调用点（实际执行次数以 accept.py 输出为准）"
          % (len(groups), tot))
    print("  纯文本组 %d：合理 %d ／ 只能联调 %d ／ **疑似弱 %d**%s"
          % (len(weak), len(fine), len(live), len(real),
             ("（%s）" % "、".join(g["id"] for g in real)) if real else ""))
    print("  没人验的文件：%s" % ("、".join(missing) or "无"))
    if a.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
