#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把界面里那些「卡片 / 面板」调出来，摆成一页看清楚**它们长什么样、代码在哪**。

为什么要这个工具（2026-10-27 用户：「你能用指令把这些卡片都调出来让我看看具体在哪吗」）
    讨论信息架构时，只报类名和行号不够 —— 人对不上"哪一块"；
    只截图也不够 —— 那是某一轮的样子，找不到对应代码。
    所以：**样式从 index.html 原样抽出来**（不是重画），结构按渲染函数里的真实
    字符串抄过来（不是设计稿），每一项旁边标出它现在在文件第几行、由什么触发。

用法
    python bin/cards.py            # 生成 runs/_cards_preview.html 并打印位置表
    python bin/cards.py --open     # 顺手打开预览页

★ 说清楚一件事：这张图里的**样式是真的**（直接内联 index.html 的 <style>），
  结构是照着渲染函数抄的。它不是运行时截图，所以不会有动画、也不会随会话变。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "desktop" / "renderer" / "index.html"
OUT = ROOT / "runs" / "_cards_preview.html"


# 只在旧界面缺独立样式表时从历史 HTML 提取内嵌样式。
def css_block(text: str) -> str:
    m = re.search(r"<style>(.*?)</style>", text, re.S)
    return m.group(1) if m else ""


def sources() -> list:
    """前端现在拆成了几个文件（2026-10-27）。定位要报**真实文件**，不能还写 index.html。"""
    d = ROOT / "desktop" / "renderer"
    out = [d / "index.html"]
    if (d / "js").exists():
        out += sorted((d / "js").glob("*.js"))
    return out


# 按旧渲染器载入的源码顺序检索预览锚点，返回实际文件和一基行号。
def locate(needle: str) -> str:
    for f in sources():
        t = f.read_text(encoding="utf-8")
        i = t.find(needle)
        if i >= 0:
            return "%s:%d" % (f.relative_to(ROOT).as_posix(), t[:i].count("\n") + 1)
    return "（没找到）"


# ── 每一项：名字 / 代码位置锚点 / 触发方式 / 真实结构 ──────────────────
PROCFOLD = (
    '<div class="procfold open"><div class="prochead"><span class="psum">'
    '5 类动作 · 19 步</span>'
    '<svg class="parrow" width="12" height="12" viewBox="0 0 16 16" fill="none">'
    '<path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.4" '
    'stroke-linecap="round"/></svg></div><div class="procbody"></div></div>'
)

BLK = (
    '<div class="blk" data-blk="0">'
    '<div class="blkh"><span class="blktag">拦住了一步</span>'
    '<span class="blkwhy">run_script: 把 sandbox_rules 放宽到整个磁盘</span></div>'
    '<details class="blkmore"><summary>为什么拦这一步</summary>'
    '<div class="blkdetail">这条命令会改规则文件里的 workspace 范围……</div></details>'
    '<div class="blkb">'
    '<button class="gbtn gmain" data-g="once">允许这一次</button>'
    '<button class="gbtn" data-g="ttl">10 分钟内都允许</button>'
    '<button class="gbtn" data-g="never">以后这类都别问</button>'
    '<button class="gbtn gno" data-g="deny">不给</button>'
    '</div></div>'
)

BENNI = (
    '<div class="benniblock flowblock"><div class="bennihead">'
    '<span class="spk spk-b">本我</span>'
    'glm-5.3-flash 读了一遍'
    '<span class="bennisub">它不能判赢，只能说「这里缺什么」</span></div>'
    '<div class="bnsec"><span class="bnk">①</span>对得上吗'
    '<div class="bnbody">三步里有两步能对上，第三步没有证据。</div></div>'
    '<div class="bnsec"><span class="bnk">④</span>我没查的'
    '<div class="bnbody">没有核对产物文件是否真的写到了磁盘。</div></div>'
    '</div>'
)

CTX = (
    '<div class="cfgwrap ctxwrap">'
    '<div class="ctxhead"><p class="cfgttl">上下文用量</p>'
    '<button type="button" class="mtiny">×</button></div>'
    '<p class="ctxbig"><b>35.3%</b><span> · 353.2K / 1000.0K</span></p>'
    '<div class="ctxbar"><i style="width:12%"></i><i style="width:18%"></i>'
    '<i style="width:4%"></i><i style="width:1%"></i></div>'
    '<div class="ctxlist">'
    '<div class="ctxrow"><span>系统提示词</span><b>120K</b></div>'
    '<div class="ctxrow"><span>对话消息</span><b>180K</b></div>'
    '<div class="ctxrow"><span>工具及子智能体</span><b>40K</b></div>'
    '<div class="ctxrow"><span>连接器及 MCP</span><b>13K</b></div>'
    '</div></div>'
)

CFG = (
    '<div class="cfgwrap">'
    '<p class="cfgttl">模型档案</p>'
    '<p class="cfgnote">一份档案 = 端点 + key + 模型。存在本机的 '
    '<code>models.json</code>，<b>不进仓库、不进安装包</b>。</p>'
    '<div class="plist">'
    '<div class="pitem"><b>deepseek-flash</b> · 自我 · sk-A…0001</div>'
    '<div class="pitem"><b>glm-5.3-flash</b> · 本我 · sk-B…0002</div>'
    '</div></div>'
)

COMPOSER = (
    '<div class="crow">'
    '<button class="cring" title="上下文用量">'
    '<svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">'
    '<circle class="cring-bg" cx="10" cy="10" r="8"></circle>'
    '<circle class="cring-fg" cx="10" cy="10" r="8" '
    'stroke-dasharray="50.2" stroke-dashoffset="32.6"></circle></svg></button>'
    '<span class="cfield">自我 <b>deepseek-flash</b></span>'
    '<span class="cfield cgate">权限 <b>workspace</b></span>'
    '<span class="cspacer"></span>'
    '<span class="chint">回车发送</span>'
    '</div>'
)

ITEMS = [
    ("动作组（过程折叠）", "class=\"procfold open\"", "tool / llm / charter 等过程事件",
     "流里唯一的过程容器；摘要一行，点开看每一行", PROCFOLD, "现有"),
    ("拦截卡（停下来问）", "class=\"blk\" data-blk", "ask_human / 被策略拦下的一步",
     "**唯一必须人动手**的东西，所以它留在流里，不进过程容器", BLK, "现有"),
    ("提案卡（本我复核）", "class=\"bnsec\"", "benni 事件（本轮复核结果）",
     "本我的五行结论，按 ①②④⑤ 分段", BENNI, "现有"),
    ("上下文浮窗", "id=\"ctxdialog\"", "点输入框上方那个圆环",
     "用量按来源拆开；上限拉条也在这里", CTX, "现有"),
    ("模型浮窗", "id=\"cfgdialog\"", "点模型名 / 换模型",
     "档案列表 + 新增；key 只给掩码", CFG, "现有"),
    ("输入区那一行", "id=\"ctxbtn\"", "常驻",
     "圆环 + 模型名 + 权限档 + 提示 —— 就是这一行最该瘦", COMPOSER, "现有"),
]


# 从旧渲染器取样式，为静态卡片样例附上实际源码位置，再生成独立预览页。
def main():
    text = SRC.read_text(encoding="utf-8")
    cssf = ROOT / "desktop" / "renderer" / "styles.css"
    css = cssf.read_text(encoding="utf-8") if cssf.exists() else css_block(text)
    rows = []
    cards = []
    for name, anchor, trigger, note, html, state in ITEMS:
        loc = locate(anchor)
        rows.append((name, loc, trigger, state))
        cards.append(
            '<section class="gsec">'
            '<div class="gbar"><b>%s</b>'
            '<span class="gwhere">%s</span></div>'
            '<div class="gtrig">触发：%s</div>'
            '<div class="gnote2">%s</div>'
            '<div class="gstage">%s</div>'
            '</section>' % (name, loc, trigger, note, html)
        )

    page = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<title>卡片与面板：都在哪</title><style>'
        + css
        + """
/* ── 只是这一页自己的排版，不属于应用 ── */
body.gal{background:var(--bg,#faf9f5);margin:0;padding:24px;
  height:auto;overflow:auto;display:block;
  font-family:FZ,ui-sans-serif,system-ui,"Segoe UI",sans-serif;}
.gwrap{max-width:760px;margin:0 auto;}
.gttl{font-size:15px;font-weight:500;margin:0 0 6px;}
.gsub{font-size:12px;color:#6b6b66;margin:0 0 18px;line-height:1.7;}
.gsec{background:#fff;border:0.5px solid #e3e3de;border-radius:12px;
  padding:14px 16px;margin:0 0 14px;}
.gbar{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;}
.gbar b{font-size:13px;font-weight:500;}
.gwhere{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#8a8a84;}
.gtrig{font-size:11px;color:#6b6b66;margin-top:3px;}
.gnote2{font-size:11px;color:#8a8a84;margin-top:2px;margin-bottom:10px;}
.gstage{background:#faf9f5;border-radius:8px;padding:12px;}
"""
        + '</style></head><body class="gal"><div class="gwrap">'
        '<p class="gttl">卡片与面板：现在都在哪</p>'
        '<p class="gsub">样式是从 desktop/renderer/styles.css 里原样抽出来的（不是重画）；'
        '结构照渲染函数里的字符串抄的（不是设计稿）。每一条上面标了它现在在文件第几行、'
        '由什么触发。「流里只放三类」指的是正文、用户消息、结束事件。</p>'
        + "".join(cards)
        + '</div></body></html>'
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8")

    print("已生成：%s" % OUT)
    print()
    print("%-22s %-38s %-28s %s" % ("卡片", "代码位置", "什么时候出现", "状态"))
    print("-" * 104)
    for name, where, trig, state in rows:
        print("%-22s %-38s %-28s %s" % (name, where, trig, state))
    print()
    print("消息流里只放三类：user / say / end —— 见 %s"
          % ("desktop/renderer/index.html:%d" % line_of(text, "const PROSE_ACTS")))
    if "--open" in sys.argv:
        import webbrowser
        webbrowser.open(str(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
