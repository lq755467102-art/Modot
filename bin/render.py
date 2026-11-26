#!/usr/bin/env python3
"""render.py —— 把一次运行的轨迹，渲染成一页能直接看的 HTML。

解决什么：现在的输出全在终端里（JSON 和 markdown），
**"它到底做了什么、本我又说了什么" 要在三个文件之间来回跳着读。**
这一页把它们按时间顺序排在一条线上，并且把"执行者"和"本我"分成两块。

用法：
    python bin/render.py                       # 读 runs/loop-real.jsonl，取最后一次运行
    python bin/render.py --all                 # 把所有运行都渲染出来（含失败的那些）
    python bin/render.py --benni runs/round-01/benni-r2.md

设计上的三条克制：
1. **不引 CDN、不启服务** —— 单文件，双击就能开，发给人也能开。
2. **人话在前，原始数据折叠** —— 每步先一句能读的话；想看细节再展开。
3. **失败也照实渲染** —— 报错的运行和成功的一视同仁，只是颜色不同。
"""

# 主要引用或加载入口包括 `harness_core.py`。

from __future__ import annotations

import argparse
import html
import io
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 工具名 → 人话（查不到就用原名，别硬编）
TOOL_ZH = {
    "run_acceptance": "跑一遍验收脚本",
    "run_check": "跑一条只读命令",
    "read_file": "读一个文件",
    "list_dir": "看一眼目录里有什么",
    "grep": "在文件里搜一段字",
}

STATUS_ZH = {
    "success": ("跑完了", "ok"),
    "budget": ("轮数用完了，还没做完", "warn"),
    "stuck": ("卡住了（原地打转）", "warn"),
    "needs_human": ("停下等人点头", "warn"),
    "error": ("出错了", "bad"),
}

MARKS = ("①", "②", "③", "④", "⑤")
MARKS_ZH = {
    "①": "对得上吗",
    "②": "我看到的",
    "③": "我怀疑的",
    "④": "我没查的",
    "⑤": "我提醒的",
}


# ────────────────────────── 读入 ──────────────────────────

def load_runs(path: Path) -> list[dict]:
    """trace 是只追加的，一行一次运行。空行和坏行跳过，但要说出来。"""
    if not path.exists():
        return []
    runs, bad = [], 0
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    if bad:
        print("⚠ %s 里有 %d 行不是合法 JSON，已跳过" % (path.name, bad))
    return runs


def parse_benni(md: str) -> dict[str, str]:
    """把本我的五行拆成 {标记: 正文}。**只认行首标记**，正文里出现字面①不算。"""
    out: dict[str, str] = {}
    lines = md.split("\n")
    starts: dict[str, int] = {}
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        for m in MARKS:
            if s.startswith(m) and m not in starts:
                starts[m] = i
    order = [m for m in MARKS if m in starts]
    for i, m in enumerate(order):
        a = starts[m]
        b = starts[order[i + 1]] if i + 1 < len(order) else len(lines)
        body = "\n".join(lines[a:b])
        body = body.split("\n", 1)[1] if "\n" in body else ""
        out[m] = body.strip()
    return out


# ────────────────────────── 渲染 ──────────────────────────

def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def step_sentence(a: dict) -> tuple[str, str, str]:
    """一条动作 → (图标, 一句话, 细节原文)。**这句话是给人看的，不是给 grep 用的。**"""
    act = a.get("act") or "?"
    t = a.get("t") or ""

    if act == "tool":
        name = a.get("tool") or "?"
        zh = TOOL_ZH.get(name, name)
        ok = a.get("ok")
        detail = a.get("out") or ""
        # ★ 工具返回 ok=false 有两种意思：「命令出错」和「命令给出了否定答案」
        #   （grep 没搜到就是退出码 1）。**混成一个红色 ✗ 就是在骗看页面的人。**
        if ok is False:
            mark, head = "·", "调用了「%s」，命令返回非 0" % zh
        elif ok is True:
            mark, head = "✓", "调用了「%s」" % zh
        else:
            mark, head = "·", "调用了「%s」" % zh
        if detail:
            head += "，拿回来：%s" % detail[:180]
        return mark, head, json.dumps(a, ensure_ascii=False, indent=1)

    if act == "llm":
        # ★ 把"真的调了模型、花了多少"画出来。
        #   没有这一行，"执行者有没有真在跑"就只能靠嘴说。
        return ("·", "第 %s 轮调模型（%s）：prompt %s token，输出 %s token，合计 %s"
                % (a.get("turn"), a.get("model") or "?",
                   a.get("prompt_tokens"), a.get("completion_tokens"),
                   a.get("total_tokens")),
                json.dumps(a, ensure_ascii=False, indent=1))

    if act == "say":
        return "💬", "它说：%s" % (a.get("text") or "")[:400], json.dumps(a, ensure_ascii=False, indent=1)

    if act == "verify":
        ok = a.get("ok")
        reason = (a.get("reason") or "").strip()
        if ok:
            return "✓", "验证器判：**通过**，允许结束", json.dumps(a, ensure_ascii=False, indent=1)
        return "✗", "验证器判：**没过** —— %s" % (reason or "没有给原因"), json.dumps(a, ensure_ascii=False, indent=1)

    if act == "stuck":
        return "⚠", "卡住了：同一个工具同一个参数连着调了 %s 次" % a.get("repeat"), json.dumps(a, ensure_ascii=False, indent=1)

    if act == "needs_human":
        return "✋", "停下等人：它要动不可逆的东西（%s）" % (a.get("tool") or "?"), json.dumps(a, ensure_ascii=False, indent=1)

    if act == "tool_missing":
        return "✗", "它要用的工具不存在：%s" % (a.get("tool") or "?"), json.dumps(a, ensure_ascii=False, indent=1)

    if act == "error":
        return "✗", "出错了：%s" % (a.get("error") or "")[:300], json.dumps(a, ensure_ascii=False, indent=1)

    return "·", act, json.dumps(a, ensure_ascii=False, indent=1)


def bold(s: str) -> str:
    """只处理 **加粗** 和 `代码`，其余原样转义。够用就行，不引 markdown 库。"""
    out = esc(s)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return out


ANCHOR_RE = re.compile(r"锚点[：:]\s*([^\n]+)")


def split_items(body: str) -> list[dict]:
    """把 ③ 拆成一条条，并取出每条的锚点。

    ★ 为什么要有"锚点"：完全分开的两块读起来是割裂的 ——
      **看不出这条批评到底在说哪一步。** 锚点就是"它指的是这一步"。
      ★ 但也不是合并：批注贴在旁边，用不同颜色和"本我"标签标出来，
        读者仍然一眼分得清哪句是谁说的。
    """
    groups: list[list[str]] = []
    for line in body.split("\n"):
        if not line.strip():
            continue
        if re.match(r"^\s*(?:[-·•*]|\d+[.)])\s+", line) or not groups:
            groups.append([line])
        else:
            groups[-1].append(line)
    out = []
    for g in groups:
        text = "\n".join(g).strip()
        m = ANCHOR_RE.search(text)
        out.append({"text": ANCHOR_RE.sub("", text).strip(),
                    "anchor": m.group(1).strip() if m else ""})
    return out


def resolve_anchor(anchor: str, actions: list[dict]) -> int | None:
    """锚点 → 第几步（0 基）。认「第 N 步」，也认时间戳。认不出就是认不出，不猜。"""
    if not anchor:
        return None
    m = re.search(r"第\s*(\d+)\s*步", anchor)
    if m:
        i = int(m.group(1)) - 1
        return i if 0 <= i < len(actions) else None
    m = re.search(r"(\d{2}:\d{2}:\d{2})", anchor)
    if m:
        for i, a in enumerate(actions):
            if a.get("t") == m.group(1):
                return i
    return None


def note_html(item: dict, why: str = "") -> str:
    """一条本我批注。**必须看起来就不像执行者的话** —— 紫边 + 标签。

    ★ 撤回的那条单独一种样式：它承认之前判错了。
      不能和正常批注长得一样 —— 否则读者会把"我说错了"读成"我又发现一条"。
    """
    tail = ('<span class="why">（%s）</span>' % esc(why)) if why else ""
    if "撤回" in item["text"]:
        return ('<li class="note retract"><span class="tk tk-r">本我 · 撤回</span>'
                '<div class="nb">%s%s</div></li>' % (bold(item["text"]), tail))
    return ('<li class="note"><span class="tk">本我</span>'
            '<div class="nb">%s%s</div></li>' % (bold(item["text"]), tail))


def render_run(run: dict, title: str, benni: dict[str, str] | None) -> str:
    status = run.get("status") or "?"
    label, tone = STATUS_ZH.get(status, (status, "warn"))
    turns = run.get("turns")
    why = (run.get("why") or "").strip()
    final = (run.get("final") or "").strip()
    actions = run.get("actions") or []

    # ── 先把批注按锚点分好 ──────────────────────────────
    items = split_items(benni.get("③", "")) if benni else []
    notes: dict[int, list[dict]] = {}
    loose: list[dict] = []
    for it in items:
        i = resolve_anchor(it.get("anchor") or "", actions)
        if i is None:
            loose.append(it)
        else:
            notes.setdefault(i, []).append(it)

    steps = []
    for i, a in enumerate(actions):
        mark, sentence, raw = step_sentence(a)
        steps.append(
            '<li class="step">'
            '<span class="mk %s">%s</span>'
            '<div class="body"><div class="t">第 %d 步 · %s</div>'
            '<div class="s">%s</div>'
            '<details><summary>看原始记录</summary><pre>%s</pre></details>'
            "</div></li>"
            % (tone if mark in ("✗", "⚠", "✋") else "ok", esc(mark),
               i + 1, esc(a.get("t") or ""), bold(sentence), esc(raw))
        )
        for it in notes.get(i, []):
            steps.append(note_html(it))

    stat_line = '状态：<b class="%s">%s</b>' % (tone, esc(label))
    if turns not in (None, ""):
        stat_line += ' ｜ 走了 <b>%s</b> 轮' % esc(turns)
    if why:
        stat_line += ' ｜ 原因：%s' % esc(why[:300])

    final_html = ""
    if final:
        final_html = ('<div class="final"><div class="lbl">它最后交出来的话</div>'
                      '<div class="txt">%s</div></div>' % bold(final))

    # ── 本我：逐条批注已经贴上去了，这里只留总评 + 锚不上的 ──
    benni_html = ""
    if benni:
        parts = []
        for m in MARKS:
            if m == "③" or m not in benni:
                continue
            parts.append(
                '<div class="row"><div class="m">%s</div>'
                '<div class="k">%s</div><div class="v">%s</div></div>'
                % (esc(m), esc(MARKS_ZH.get(m, "")), bold(benni[m]))
            )
        anchored = sum(len(v) for v in notes.values())
        head = ('<h2><span class="tagdenni">本我</span> 总评</h2>'
                '<p class="hint">这一段<b>不是执行者</b>写的 —— 另一个人格，只读记录、不动手，'
                '<b>没有经过我改写</b>。逐条批注（<b>%d 条</b>）已经贴在'
                '<b>上面对应那一步的下面</b>，紫色边线 + “本我”标签，不会和我说的混在一起。'
                '这里只剩总评和<b>指不出位置</b>的那些。</p>' % anchored)
        if loose:
            lis = "".join(
                '<div class="li">%s<span class="why">（锚点：%s）</span></div>'
                % (bold(it["text"]), esc(it["anchor"] or "没写"))
                for it in loose)
            head += ('<div class="loose"><div class="loose-t">'
                     '这 %d 条<b>没锚上</b> —— 它指不出是哪一步，参考价值打折</div>%s</div>'
                     % (len(loose), lis))
        benni_html = (
            '<section class="benni">' + head + "".join(parts) + "</section>"
        )

    return (
        '<article class="run">'
        '<header><h1>%s</h1><div class="stat">%s</div></header>'
        '<section class="self">'
        '<h2><span class="tagself">执行者</span> 做了什么</h2>'
        '<ol class="steps">%s</ol>'
        "%s"
        "</section>"
        "%s"
        "</article>"
        % (esc(title), stat_line, "".join(steps) or '<li class="step">（没有记录到任何动作）</li>',
           final_html, benni_html)
    )


CSS = """
:root{
  --bg:#fbfbfa; --panel:#fff; --ink:#1d1d1f; --dim:#6b6b70; --line:#e6e6e3;
  --ok:#0f7b52; --warn:#9a6400; --bad:#b3261e; --acc:#2c5cc5; --ben:#6b3fa0;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#16161a; --panel:#1e1e23; --ink:#ececf1; --dim:#9a9aa5; --line:#2e2e36;
         --ok:#5fcf9c; --warn:#e0ab4a; --bad:#f2777a; --acc:#8ab4ff; --ben:#c39bf5; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;}
main{max-width:900px;margin:0 auto;padding:40px 22px 80px}
.run{margin-bottom:64px}
h1{font-size:20px;margin:0 0 6px}
h2{font-size:15px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
.stat{color:var(--dim);font-size:13.5px}
.tagself,.tagdenni{font-size:11.5px;padding:2px 8px;border-radius:20px;
 font-weight:600;letter-spacing:.5px}
.tagself{background:rgba(44,92,197,.12);color:var(--acc)}
.tagdenni{background:rgba(107,63,160,.14);color:var(--ben)}
section{margin-top:26px;background:var(--panel);border:1px solid var(--line);
 border-radius:12px;padding:20px 22px}
section.benni{border-left:3px solid var(--ben)}
section.self{border-left:3px solid var(--acc)}
.hint{color:var(--dim);font-size:13px;margin:6px 0 16px}
.steps{list-style:none;margin:14px 0 0;padding:0}
.step{display:flex;gap:12px;padding:10px 0;border-top:1px dashed var(--line)}
.step:first-child{border-top:0}
.mk{flex:0 0 22px;text-align:center;font-weight:700}
.mk.ok{color:var(--ok)} .mk.warn{color:var(--warn)} .mk.bad{color:var(--bad)}
.body{flex:1;min-width:0}
.t{color:var(--dim);font-size:12.5px;font-variant-numeric:tabular-nums}
.s{word-break:break-word}
.s b{font-weight:650}
details{margin-top:6px}
summary{cursor:pointer;color:var(--dim);font-size:12.5px}
code{background:rgba(125,125,135,.14);border-radius:4px;padding:1px 5px;
 font-size:.92em;font-family:ui-monospace,Consolas,monospace}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;
 padding:10px 12px;overflow:auto;font-size:12px;margin:6px 0 0}
.final{margin-top:18px;padding:14px 16px;background:var(--bg);
 border:1px solid var(--line);border-radius:10px}
.final .lbl{font-size:12px;color:var(--dim);margin-bottom:6px}
.final .txt{white-space:pre-wrap}
.row{display:grid;grid-template-columns:26px 92px 1fr;gap:10px;
 padding:11px 0;border-top:1px dashed var(--line)}
.row:first-of-type{border-top:0}
.row .m{font-weight:700;color:var(--ben)}
.row .k{color:var(--dim);font-size:13px}
.row .v{white-space:pre-wrap;word-break:break-word}
/* 本我批注：贴在被批的那一步下面，但一眼看得出不是我说的话 */
.step.note{display:block;border-top:0;padding:0 0 12px 34px;margin-top:-4px}
.note{list-style:none;display:block;border-top:0;padding:0 0 14px 34px;margin-top:-6px}
.note .tk{display:inline-block;font-size:11px;font-weight:600;letter-spacing:.5px;
 color:var(--ben);background:rgba(107,63,160,.14);border-radius:20px;
 padding:1px 7px;margin-right:7px;vertical-align:1px}
.note.retract .nb{background:rgba(125,125,135,.07);border-left-color:var(--dim);
 color:var(--dim);text-decoration:line-through;text-decoration-thickness:1px}
.note.retract .tk-r{background:rgba(125,125,135,.16);color:var(--dim)}
.note .nb{display:inline;background:rgba(107,63,160,.06);
 border-left:2px solid var(--ben);border-radius:0 6px 6px 0;
 padding:8px 12px;white-space:pre-wrap;word-break:break-word;font-size:14px}
.loose{margin:16px 0 6px;padding:12px 14px;border-radius:8px;
 background:rgba(154,100,0,.07);border:1px dashed rgba(154,100,0,.35)}
.loose-t{font-size:13px;color:var(--warn);margin-bottom:8px}
.loose .li{white-space:pre-wrap;padding:5px 0}
.why{color:var(--dim);font-size:12.5px}
.foot{color:var(--dim);font-size:12.5px;text-align:center;margin-top:10px}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", default=str(ROOT / "runs" / "loop-real.jsonl"))
    ap.add_argument("--benni", default="")
    ap.add_argument("--out", default=str(ROOT / "runs" / "report.html"))
    ap.add_argument("--all", action="store_true", help="渲染全部运行，不只最后一次")
    a = ap.parse_args()

    runs = load_runs(Path(a.trace))
    if not runs:
        print("没有可渲染的运行记录：%s" % a.trace)
        return 2

    benni = {}
    if a.benni:
        p = Path(a.benni)
        if p.exists():
            benni = parse_benni(io.open(p, encoding="utf-8").read())
        else:
            print("⚠ 找不到本我的那份：%s（这一页就只有执行者）" % a.benni)

    # 默认只渲染最后一次；--all 才全渲染（失败的那些也要能看到）
    picked = list(enumerate(runs, 1))
    if not a.all:
        picked = picked[-1:]

    if a.all:
        blocks = [render_run(r, "第 %d 次运行（共 %d 次）" % (i, len(runs)),
                             benni if i == len(runs) else None) for i, r in picked]
    else:
        blocks = [render_run(r, "最近一次运行", benni) for _, r in picked]

    doc = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>这一轮它做了什么</title><style>%s</style><main>%s"
        '<div class="foot">数据来源：%s ｜ 由 bin/render.py 生成，不是手工写的</div>'
        "</main></html>"
        % (CSS, "".join(blocks), esc(Path(a.trace).name))
    )

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    io.open(out, "w", encoding="utf-8").write(doc)

    print("已生成：%s" % out)
    print("  渲染了 %d 次运行（共 %d 次）｜ 本我五行：%s"
          % (len(picked), len(runs), ("有（%d 行）" % len(benni)) if benni else "没有"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
