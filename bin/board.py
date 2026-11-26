#!/usr/bin/env python3
"""board.py —— 一页看全部轮次

这东西是干嘛的
    跑一轮下来，东西散在 `runs/<名字>/` 里：契约、轨迹、本我五行、文章、渲染页。
    要看"最近几轮都干了什么"，得一个个目录点进去比。
    这一页把它们排成一列，每轮一张卡片。

★ 和界面里的「轮次/状态」是**同一份数据、两处渲染**（2026-10-26 盘点）
    改 `runs/<名字>/` 里的字段时两边都要看；改一处忘了另一处，
    表现是「命令行对、界面错」（或反过来），而这种差最难发现。
    同样的情况还有 `console.py`（记忆树/总纲）、`health.py`（验收覆盖）、
    `render.py`（单次轨迹页）—— 四个命令行页面 + 界面，各处读的目录不同，
    但**「这个数字怎么算」在三处各写过一遍**。

★ 三条克制（和 render.py 一路，但理由不同）

1. **不启服务，每次跑完重新生成一个静态页。**
   加服务就要管端口、进程、鉴权，而这一层不带任何新信息。
   更要紧的是：**静态页意味着"我读到的"和"你看到的"是同一份文件。**
   服务一旦开始自己存状态，两边就会分叉 —— 那是最难查的一类问题。

2. **页面上每个数字都标来源。**
   卡片底下那行写着它从哪个文件读的。**界面上不许出现"只有界面知道"的数。**

3. **不缓存。** 每次生成都重新扫磁盘。把浏览器关了重开，看到的一模一样。
   判据：杀掉所有进程，重新跑一次 board.py，输出应当逐字节相同。

用法
    python bin/board.py            # 扫 runs/，写 runs/index.html
    python bin/board.py --open     # 生成后直接打开
"""
# 主要引用或加载入口包括 `harness_core.py`。
from __future__ import annotations

import argparse
import html
import json
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from loop.paths import data_root

RUNS = data_root() / "runs"

PRODUCE = [
    ("article.md", "文章"),
    ("after.md", "后记"),
    ("benni.md", "本我（人写）"),
    ("benni-model.md", "本我（模型）"),
    ("report.html", "渲染页"),
]

STATUS_ZH = {"success": "跑完了", "budget": "轮数用完了", "stuck": "卡住了",
             "needs_human": "等人点头", "error": "出错了"}


def read_trace(p: Path):
    """轨迹可能是"一整个 JSON 对象"，也可能是逐行 jsonl —— 两种都认。"""
    txt = p.read_text(encoding="utf-8", errors="replace").strip()
    if not txt:
        return None
    if txt.startswith("{"):
        try:
            return json.loads(txt)
        except Exception:
            return None
    last = None
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line)
        except Exception:
            pass
    return last


def scan() -> list[dict]:
    rows = []
    for d in sorted(RUNS.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        r = {"name": d.name, "dir": d, "task": "", "claims": 0,
             "locked": False, "amends": 0, "status": "", "turns": 0,
             "actions": 0, "tokens": 0, "when": "", "made": []}

        dj = d / "done.json"
        if dj.exists():
            try:
                o = json.loads(dj.read_text(encoding="utf-8"))
                r["task"] = o.get("task") or ""
                r["claims"] = len(o.get("done") or [])
            except Exception:
                r["task"] = "（done.json 读不出来）"
        r["locked"] = (d / "done.lock").exists()
        lg = d / "done.log"
        if lg.exists():
            r["amends"] = len([x for x in lg.read_text(encoding="utf-8").splitlines() if x.strip()])

        tj = d / "trace.jsonl"
        if tj.exists():
            t = read_trace(tj)
            if isinstance(t, dict):
                r["status"] = t.get("status") or ""
                r["turns"] = t.get("turns") or 0
                acts = t.get("actions") or []
                r["actions"] = len(acts)
                r["tokens"] = sum(int(a.get("total_tokens") or 0) for a in acts)
                for a in acts:
                    if a.get("t"):
                        r["when"] = a["t"]
                        break

        for fn, label in PRODUCE:
            if (d / fn).exists():
                r["made"].append((fn, label))
        rows.append(r)

    rows.sort(key=lambda x: (x["dir"].stat().st_mtime), reverse=True)
    return rows


def card(r: dict) -> str:
    e = html.escape
    bits = []
    if r["claims"]:
        bits.append("契约 %d 条%s" % (r["claims"], "（已锁）" if r["locked"] else "（**没锁**）"))
    if r["amends"]:
        bits.append("改过 %d 次（留了痕）" % r["amends"])
    if r["status"]:
        bits.append(STATUS_ZH.get(r["status"], r["status"]))
    if r["turns"]:
        bits.append("%d 轮 / %d 个动作" % (r["turns"], r["actions"]))
    if r["tokens"]:
        bits.append("%s token" % format(r["tokens"], ","))

    links = "".join(
        '<a href="%s/%s">%s</a>' % (e(r["name"]), e(fn), e(label))
        for fn, label in r["made"]
    ) or '<span class="dim">这一轮没有产出文件</span>'

    src = []
    if (r["dir"] / "done.json").exists():
        src.append("done.json")
    if (r["dir"] / "trace.jsonl").exists():
        src.append("trace.jsonl")
    if r["amends"]:
        src.append("done.log")

    return """<section class="c">
  <h2>%s</h2>
  <p class="task">%s</p>
  <p class="meta">%s</p>
  <p class="made">%s</p>
  <p class="src">数字来自 %s%s</p>
</section>""" % (
        e(r["name"]),
        e(r["task"]) or '<span class="dim">（这一轮没有写契约）</span>',
        " · ".join(bits) or '<span class="dim">没有轨迹</span>',
        links,
        " / ".join("runs/%s/%s" % (r["name"], s) for s in src) or "（无）",
        (" · 时间 %s" % r["when"]) if r["when"] else "",
    )


CSS = """
*{box-sizing:border-box}
body{margin:0;padding:32px 24px 64px;background:#FAF9F6;color:#2C2C2A;
     font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:880px;margin:0 auto}
h1{font-size:20px;font-weight:500;margin:0 0 4px}
.sub{color:#5F5E5A;margin:0 0 28px;font-size:13px}
.c{background:#fff;border:1px solid #E5E3DD;border-radius:10px;padding:16px 18px;margin:0 0 12px}
.c h2{font-size:14px;font-weight:500;margin:0 0 6px;font-family:ui-monospace,monospace}
.task{margin:0 0 10px;color:#2C2C2A}
.meta{margin:0 0 10px;color:#5F5E5A;font-size:13px}
.made{margin:0 0 10px;font-size:13px}
.made a{color:#185FA5;text-decoration:none;margin-right:14px}
.made a:hover{text-decoration:underline}
.src{margin:0;color:#888780;font-size:11px;font-family:ui-monospace,monospace}
.dim{color:#888780}
.note{background:#F1EFE8;border-radius:10px;padding:14px 16px;margin:0 0 24px;
      color:#444441;font-size:13px}
"""


def build(rows: list[dict]) -> str:
    with_c = sum(1 for r in rows if r["claims"])
    with_b = sum(1 for r in rows if any(fn.startswith("benni") for fn, _ in r["made"]))
    body = "\n".join(card(r) for r in rows) or '<p class="dim">runs/ 下还没有轮次。</p>'
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>agent-harness · 轮次看板</title><style>%s</style></head><body><div class="wrap">
<h1>轮次看板</h1>
<p class="sub">%d 轮 ｜ %d 轮写了契约 ｜ %d 轮有本我复核</p>
<div class="note">这一页是<b>每次重新扫磁盘生成的静态页</b>，不启服务、不缓存状态。
所以它显示的东西和我直接读文件看到的是同一份 —— 页面上每个数字都可以顺着底下那行追回文件。</div>
%s
</div></body></html>""" % (CSS, len(rows), with_c, with_b, body)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="扫 runs/ 生成一页轮次看板")
    ap.add_argument("--open", action="store_true", help="生成后打开")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)

    if not RUNS.exists():
        print("× 没有 runs/ 目录")
        return 2
    rows = scan()
    out = Path(a.out) if a.out else RUNS / "index.html"
    out.write_text(build(rows), encoding="utf-8")
    print("已生成：%s（%d 轮）" % (out, len(rows)))
    for r in rows:
        print("  · %-16s %s" % (r["name"], (r["task"] or "（无契约）")[:46]))
    if a.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
