#!/usr/bin/env python3
"""console.py —— 让状态不经过模型转述，直接摆在人眼前

为什么要有这个
    用户的原话：「最理想的场景就是，记忆树和总纲是人直接可见的，也可以知道
    当前对话的总纲发展到什么程度了，而不是需要专门问 ai 现在工程做得怎么样了。
    这既浪费 token，又容易造成漂移和过拟合。」

    这句话是对的，而且比"界面好不好看"严重得多：
    **只要状态要靠问才拿得到，人就只能拿模型的叙述做判断。**
    叙述是二手的、有取舍的，而且"我问你答"这个循环本身在塑造人看到的世界。
    从这里以前，这个问题一直存在 —— 看板列的是轮次目录，健康页讲的是验收覆盖，
    没有一个地方回答得了「总纲现在带了哪几层」。

它回答四个问题（都不用问模型）
    ① 现在带了哪几层、多大、占上下文多少 —— 总纲实况
    ② 换一档要多花多少 —— 四档成本（数字是真去树里拼一遍算的）
    ③ 树里有哪些层、各自多大、哪些没被带 —— 记忆树全貌
    ④ 最近几轮做了什么、结果如何 —— 轮次索引

它能写什么（以及为什么只写这些）
    **切档**。档位是人的选择，本来就该人直接定；模型只能在预算内申请加层。
    其余一律只读 —— 写契约、改树的正文这些，错一次就改世界，不该在第一版里做。

    ★ 切档的判断在 `loop/charter.py:switch_mode`，这里不重写一遍。
      升档仍然要二次确认：花出去的钱收不回来。

用法
    python bin/console.py --out runs/console.html     # 出静态快照（只读）
    python bin/console.py serve --port 8787           # 本地服务，能切档
    python bin/console.py --config 某份.json serve    # ★ --config 要放在 serve **之前**
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import charter as CH                       # noqa: E402
from loop.tools import _recent_runs                  # noqa: E402

PAGE_TITLE = "harness 控制台"


# ───────────────────────── 取数 ─────────────────────────

def collect(config: Path | None = None) -> dict:
    cfg = CH.load_config(config)
    res = CH.build(cfg)
    b = CH.budget(cfg, len(res["text"]))
    root = Path(res["root"]) if res["root"] else None
    active = set(CH.active_layers(cfg))

    layers = []
    if root and (root / "tree").is_dir():
        tdir = root / "tree"
        names = sorted({p.stem for p in tdir.glob("*.md")} |
                       {d.name for d in tdir.iterdir() if d.is_dir()})
        for n in names:
            files = CH.layer_files(root, n)
            chars = sum(len(CH.strip_meta(f.read_text(encoding="utf-8", errors="replace")))
                        for f in files)
            meta = CH.layer_meta(root, n)
            layers.append({"name": n, "chars": chars, "files": len(files),
                           "in_mode": n in active,
                           # ★ 把层自己写的两件事摆出来：优先级、什么时候该读。
                           #   选层逻辑现在是按 priority 自动装的，
                           #   所以人必须能看见「为什么是这几层」——
                           #   否则又是"系统替我决定了但我不知道为什么"。
                           "priority": meta.get("priority", ""),
                           "reads_when": meta.get("reads_when", ""),
                           "kind": meta.get("kind", "")})
    layers.sort(key=lambda x: -x["chars"])

    log = ""
    lp = ROOT / "harness" / "mode.log"
    if lp.exists():
        lines = [x for x in lp.read_text(encoding="utf-8",
                                          errors="replace").splitlines() if x.strip()]
        log = lines[-1] if lines else ""

    return {
        "mode": CH.active_mode(cfg),
        "modes": CH.mode_table(cfg),
        "root": str(root) if root else "",
        "missing": res.get("missing") or [],
        "charter_chars": len(res["text"]),
        "charter_parts": res.get("parts") or [],
        "budget": b,
        "layers": layers,
        "runs": (_recent_runs(limit=12, repo_root=ROOT).get("shown") or []),
        "log": log,
    }


# ───────────────────────── 渲染 ─────────────────────────

CSS = """
body{margin:0;background:#f7f7f5;color:#22252a;
 font:14px/1.65 system-ui,-apple-system,'Microsoft YaHei',sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:19px;font-weight:500;margin:0 0 4px}
.sub{color:#6b7280;font-size:12.5px;margin:0 0 22px}
h2{font-size:13px;font-weight:500;color:#6b7280;margin:26px 0 10px;
 letter-spacing:.04em}
.k{display:flex;gap:26px;flex-wrap:wrap;margin:0 0 6px}
.k div{min-width:110px}
.k b{display:block;font-size:22px;font-weight:500;font-variant-numeric:tabular-nums}
.k span{color:#6b7280;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:500;color:#6b7280;font-size:12px;
 border-bottom:1px solid #e3e3e0;padding:6px 8px}
td{padding:7px 8px;border-bottom:1px solid #efefec}
td.n{text-align:right;font-variant-numeric:tabular-nums;
 font-family:ui-monospace,Consolas,monospace}
.on{background:#eef4ff}
.dim{color:#9ca3af}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.tag{display:inline-block;padding:0 6px;border-radius:4px;font-size:11.5px}
.ok{background:#e8f5e9;color:#2f6b35}
.warn{background:#fff4e5;color:#8a5a13}
.bad{background:#fdeaea;color:#8f2f2f}
.note{background:#fff;border:1px solid #e3e3e0;border-radius:8px;
 padding:10px 12px;font-size:12.5px;color:#4b5563;margin:10px 0 0}
button{font:inherit;font-size:12.5px;padding:4px 10px;border-radius:6px;
 border:1px solid #d3d3cf;background:#fff;cursor:pointer}
button:hover{border-color:#9aa0a6}
button.now{border-color:#378add;color:#185fa5}
"""


def _esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _pct(x: float) -> str:
    return "%.1f%%" % x


def render(d: dict) -> str:
    b = d["budget"]
    over = b["over"]
    L = []
    L.append("<!doctype html><meta charset='utf-8'>")
    L.append("<title>%s</title><style>%s</style>" % (PAGE_TITLE, CSS))
    L.append("<div class='wrap'>")
    L.append("<h1>%s</h1>" % PAGE_TITLE)
    L.append("<p class='sub'>这一页的数据全部来自文件："
             "<span class='mono'>harness/charter.json</span>、记忆树的 "
             "<span class='mono'>tree/</span>、"
             "<span class='mono'>runs/**/summary.json</span>。"
             "没有任何一处是模型口述的。</p>")

    # ① 总纲实况
    L.append("<h2>总纲实况</h2>")
    L.append("<div class='k'>")
    L.append("<div><b>%s</b><span>当前档位</span></div>" % _esc(d["mode"]))
    L.append("<div><b>%d</b><span>每轮注入字符</span></div>" % d["charter_chars"])
    L.append("<div><b>%s</b><span>占上下文</span></div>" % _pct(b["pct_ctx"]))
    L.append("<div><b>%s</b><span>占软上限</span></div>" % _pct(b["pct_cap"]))
    L.append("<div><b>%s</b><span>软上限</span></div>" % b["cap"])
    L.append("</div>")
    if not d["root"]:
        L.append("<div class='note warn'>记忆树的根没找到 —— 这一轮实际不带约束。"
                 "检查 <span class='mono'>charter.json</span> 里的 "
                 "<span class='mono'>roots</span>。</div>")
    if d["missing"]:
        L.append("<div class='note warn'>选了但树里没有的层：%s</div>"
                 % _esc("、".join(d["missing"])))
    if over:
        L.append("<div class='note bad'>超软上限了（%d &gt; %d）。该丢哪层是判断，"
                 "跑 <span class='mono'>bin/charter.py plan</span> 看取舍。</div>"
                 % (b["used"], b["cap"]))
    parts = d["charter_parts"]
    if parts:
        L.append("<table><tr><th>这一轮带上的层</th><th class='n'>字符</th>"
                 "<th class='n'>文件</th></tr>")
        for p in parts:
            L.append("<tr><td>%s</td><td class='n'>%d</td><td class='n'>%d</td></tr>"
                     % (_esc(p["name"]), p["chars"], len(p.get("files") or [])))
        L.append("</table>")

    # ② 四档成本（可切）
    L.append("<h2>四档成本 · 换一档要多花多少</h2>")
    L.append("<table><tr><th>档位</th><th class='n'>每轮字符</th>"
             "<th class='n'>占上下文</th><th>能申请加层</th><th>说明</th><th></th></tr>")
    for r in d["modes"]:
        cur = r["mode"] == d["mode"]
        L.append("<tr class='%s'><td>%s%s</td><td class='n'>%d</td>"
                 "<td class='n'>%s</td><td>%s</td><td class='dim'>%s</td><td>%s</td></tr>"
                 % ("on" if cur else "", _esc(r["mode"]),
                    " <span class='tag ok'>当前</span>" if cur else "",
                    r["chars"], _pct(100.0 * r["chars"] / b["limit"] if b["limit"] else 0),
                    "可" if r.get("request") else "不可",
                    _esc(r.get("note") or ""),
                    "" if cur else
                    "<form method='post' action='/mode' style='margin:0'>"
                    "<input type='hidden' name='mode' value='%s'>"
                    "<button type='submit'>切到 %s</button></form>"
                    % (_esc(r["mode"]), _esc(r["mode"]))))
    L.append("</table>")
    if d["log"]:
        L.append("<div class='note'>上一次切档：<span class='mono'>%s</span></div>"
                 % _esc(d["log"]))
    L.append("<div class='note'>★ 升到更贵的档要再点一次确认 —— "
             "花出去的 token 收不回来。判断逻辑和命令行共用一处，不会两边不一致。</div>")

    # ③ 记忆树
    L.append("<h2>记忆树 · 树里有什么，哪些没被带</h2>")
    if not d["layers"]:
        L.append("<div class='note dim'>树里读不到层。</div>")
    else:
        L.append("<table><tr><th>层</th><th class='n'>优先级</th><th class='n'>字符</th>"
                 "<th>它自己说什么时候读</th><th>这一轮</th></tr>")
        for x in d["layers"]:
            L.append("<tr class='%s'><td>%s</td><td class='n'>%s</td><td class='n'>%d</td>"
                     "<td class='dim'>%s</td><td>%s</td></tr>"
                     % ("on" if x["in_mode"] else "", _esc(x["name"]),
                        _esc(x.get("priority") or "—"), x["chars"],
                        _esc((x.get("reads_when") or "")[:40]),
                        "<span class='tag ok'>带上</span>" if x["in_mode"]
                        else "<span class='tag dim'>没带</span>"))
        L.append("</table>")
        used = sum(1 for x in d["layers"] if x["in_mode"])
        L.append("<div class='note'>%d 层里有 %d 层被带上。"
                 "<b>带哪几层是按优先级自动算的</b>：priority 小的先装，装到这一档的预算为止。"
                 "「什么时候读」那列是层自己写的，机器读不懂，只给你看 —— "
                 "如果某层的优先级和它自己写的用途不符，改那个层的 priority。</div>"
                 % (len(d["layers"]), used))

    # ④ 最近轮次
    L.append("<h2>最近几轮</h2>")
    if not d["runs"]:
        L.append("<div class='note dim'>还没有轮次索引（跑一次 "
                 "<span class='mono'>run-task.py</span> 就会写）。</div>")
    else:
        L.append("<table><tr><th>时间</th><th>结果</th><th class='n'>轮数</th>"
                 "<th>为什么停</th></tr>")
        for r in d["runs"]:
            if "error" in r:
                L.append("<tr><td colspan='4' class='dim'>坏档：%s（%s）</td></tr>"
                         % (_esc(r.get("path")), _esc(r.get("error"))))
                continue
            st = r.get("status") or ""
            cls = "ok" if st == "success" else ("warn" if st in ("budget", "paused")
                                                else "bad")
            L.append("<tr><td class='mono'>%s</td><td><span class='tag %s'>%s</span></td>"
                     "<td class='n'>%s</td><td class='dim'>%s</td></tr>"
                     % (_esc(r.get("at")), cls, _esc(st), _esc(r.get("turns")),
                        _esc(r.get("why") or "")))
        L.append("</table>")

    L.append("</div>")
    return "".join(L)


# ───────────────────────── 服务 ─────────────────────────

def _confirm_page(msg: str, mode: str) -> str:
    return ("<!doctype html><meta charset='utf-8'><title>确认</title>"
            "<style>%s</style><div class='wrap'><h1>要花更多吗？</h1>"
            "<div class='note warn'>%s</div>"
            "<form method='post' action='/mode' style='margin-top:14px'>"
            "<input type='hidden' name='mode' value='%s'>"
            "<input type='hidden' name='confirm' value='1'>"
            "<button type='submit'>确认切到 %s</button></form>"
            "<p class='sub' style='margin-top:16px'>"
            "<a href='/'>算了，回控制台</a></p></div>"
            % (CSS, _esc(msg), _esc(mode), _esc(mode)))


def serve(port: int, config: Path | None = None) -> int:
    class H(BaseHTTPRequestHandler):
        def _send(self, body: str, code: int = 200):
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if urllib.parse.urlparse(self.path).path not in ("/", "/index.html"):
                self._send("没这页", 404)
                return
            self._send(render(collect(config)))

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/mode":
                self._send("没这页", 404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", "replace")
            q = urllib.parse.parse_qs(raw)
            mode = (q.get("mode") or [""])[0]
            confirm = (q.get("confirm") or ["0"])[0] == "1"
            cfg = CH.load_config(config)
            rc, msg, _ = CH.switch_mode(cfg, mode, force=confirm, why="控制台", path=config)
            if rc == 6:
                self._send(_confirm_page(msg, mode))
                return
            if rc != 0:
                self._send(("<div class='wrap'><p>%s</p><p><a href='/'>回去</a></p></div>"
                            % _esc(msg)), 400)
                return
            # 切完回控制台：人应该立刻看到新状态，而不是停在一个"成功"页上
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def log_message(self, *a):        # 别把每个请求都刷到终端
            return

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print("控制台：http://127.0.0.1:%d  （只监听本机，Ctrl-C 退出）" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停了。")
    finally:
        srv.server_close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="控制台：总纲 / 记忆树 / 轮次，一屏看全")
    sub = ap.add_subparsers(dest="cmd")
    a1 = sub.add_parser("serve", help="起本地服务（可以切档）")
    a1.add_argument("--port", type=int, default=8787)
    ap.add_argument("--out", default="", help="只出一份静态快照，不起服务")
    # ★ 能让它指向临时配置，是为了验收能测"写"而不碰真实档位。
    #   一个测试会改用户真实配置的工具，没人敢跑第二遍。
    ap.add_argument("--config", default="", help="用哪份 charter.json（默认仓库那份）")
    a = ap.parse_args(argv)
    _cfg = Path(a.config) if a.config else None

    if a.cmd == "serve":
        return serve(a.port, _cfg)
    if a.out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render(collect(_cfg)), encoding="utf-8")
        print("写了 %s" % p)
        return 0
    print("用法：python bin/console.py serve --port 8787 ｜ --out runs/console.html")
    return 4


if __name__ == "__main__":
    sys.exit(main())
