#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验一遍 MCP 客户端到底能不能接第三方 server。

为什么要有这个探针
    `loop/mcp.py` 750 行从没接过真实 server，只验过内部造的假 server。
    「支持 MCP」这句话在那种状态下是自评，不是结论。

它验四条，对应四种会真出事的走法：

    1. 连得上 + 列得出     —— 最常见的一种，也是最容易假过的一种
    2. 调得动              —— 列得出不代表调得动（参数映射、结果解析是两段）
    3. 超时退得出来        —— server 卡死时必须自己退，否则任务挂死在无人看见的地方
    4. 断线认得出          —— server 半路退出，要报"它退出了"，不能装成"没有结果"

用法
    python bin/probe_mcp.py                      # 用默认的演示 server
    python bin/probe_mcp.py --server demo/mcp-demo/server.py
    python bin/probe_mcp.py --json

退出码
    0 四条全过 / 1 有不通过的（那就是真的有问题，别当"环境问题"绕过去）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loop import mcp as MC                                        # noqa: E402
from loop.tools import Registry                                   # noqa: E402


def _cfg_for(server: Path, mode: str, root: Path) -> dict:
    """造一份只含这个 server 的配置。**用完就删**，不碰用户真实的 mcp.json。"""
    return {"mcpServers": {"demo": {
        "command": sys.executable,
        "args": [str(server)],
        "env": {"DEMO_MCP_MODE": mode},
        # ★ 故意设成 auto：验的是"接线通不通"，不是"审批卡不卡"。
        #   审批那条路另有验收兜着（deny/ask/allow 三档），混在一起会说不清
        #   失败到底出在哪一段。
        "default_tools_approval_mode": "auto",
        "output_token_limit": 2000,
        # ★ 超时压到 5 秒（2026-10-29）：默认是 60 秒，而"验超时"这件事
        #   没必要真等一分钟 —— 60 秒是给真实 server 的余量，不是给探针的耐心。
        "tool_timeout_sec": 5,
    }}}


def _with_cfg(cfg: dict):
    """临时把 mcp 配置指向我们的演示 server。"""
    tmp = Path(tempfile.mkdtemp(prefix="mcp-probe-"))
    p = tmp / "mcp.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    os.environ["HARNESS_MCP"] = str(p)
    return p


def run(server: Path, root: Path) -> dict:
    out = {"server": str(server), "checks": []}

    def add(name, ok, detail=""):
        out["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})

    # ── 1 & 2：连得上、列得出、调得动 ──
    _with_cfg(_cfg_for(server, "ok", root))
    reg = Registry()
    rep = MC.register(reg, root, sys.executable)
    srv = (rep.get("servers") or {}).get("demo") or {}
    add("连得上 + 列得出", srv.get("ok") and srv.get("tools", 0) >= 1,
        srv.get("why") or ("工具 %s 个：%s" % (srv.get("tools"), srv.get("names"))))

    # 注册出来的名字要能看见（模型是靠这个名调的）
    names = [n for n in reg.names() if n.startswith("mcp__")]
    add("工具进了注册表（模型看得见）", bool(names), names)

    # 真调一次。★ 名字必须从注册表里取，不能自己拼 ——
    #   拼出来的名字就算调通了，也证明不了"模型拿到的那个名字"是对的。
    tool = reg.get(names[0]) if names else None
    got = None
    if tool is not None:
        try:
            got = tool.fn(a=2, b=3)
        except Exception as e:
            got = {"__err__": "%s: %s" % (type(e).__name__, e)}
    txt = json.dumps(got, ensure_ascii=False) if got is not None else ""
    add("调得动（2+3 要得 5）", '"5"' in txt or "'5'" in txt, txt[:300])

    try:
        MC.close_all()
    except Exception:
        pass

    # ── 3：超时退得出来 ──
    _with_cfg(_cfg_for(server, "hang", root))
    reg2 = Registry()
    rep2 = MC.register(reg2, root, sys.executable)
    n2 = [n for n in reg2.names() if n.startswith("mcp__")]
    t0 = __import__("time").time()
    res = None
    if n2 and reg2.get(n2[0]) is not None:
        try:
            res = reg2.get(n2[0]).fn(a=1, b=1)
        except Exception as e:
            res = {"__err__": "%s: %s" % (type(e).__name__, e)}
    spent = __import__("time").time() - t0
    rt = json.dumps(res, ensure_ascii=False) if res is not None else ""
    # ★ 判据是"回来了，而且说清楚了是超时"，不是"没崩"。
    #   卡了 300 秒才回来也算"没崩"，但那种在任务里等于挂死。
    add("超时退得出来", ("超时" in rt or "timeout" in rt.lower()) and spent < 120,
        "%.1fs ｜ %s" % (spent, rt[:240]))
    try:
        MC.close_all()
    except Exception:
        pass

    # ── 4：断线认得出 ──
    _with_cfg(_cfg_for(server, "die", root))
    reg3 = Registry()
    rep3 = MC.register(reg3, root, sys.executable)
    n3 = [n for n in reg3.names() if n.startswith("mcp__")]
    res3 = None
    if n3 and reg3.get(n3[0]) is not None:
        try:
            res3 = reg3.get(n3[0]).fn(a=1, b=1)
        except Exception as e:
            res3 = {"__err__": "%s: %s" % (type(e).__name__, e)}
    rt3 = json.dumps(res3, ensure_ascii=False) if res3 is not None else ""
    # ★ 要求它**说出"退出了"**，而不是只说"没有结果"。两者对处置完全相反：
    #   "没有结果"像是工具没给答案（可能再试一次），"它退出了"是它没了。
    add("断线认得出", ("退出" in rt3 or "eof" in rt3.lower() or "closed" in rt3.lower()),
        rt3[:240])
    try:
        MC.close_all()
    except Exception:
        pass

    out["ok"] = all(c["ok"] for c in out["checks"])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="验 MCP 客户端能不能接第三方 server")
    ap.add_argument("--server", default="",
                    help="server 脚本路径（默认 demo/mcp-demo/server.py）")
    ap.add_argument("--root", default="", help="给客户端的 root（默认当前目录）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    a = ap.parse_args(argv)

    here = Path(__file__).resolve().parent.parent
    server = Path(a.server) if a.server else (here / "demo" / "mcp-demo" / "server.py")
    if not server.is_file():
        print("× 找不到 server：%s" % server, file=sys.stderr)
        return 2
    root = Path(a.root).resolve() if a.root else Path.cwd()

    r = run(server, root)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r["ok"] else 1

    print("验的是：能不能按规范跟一个**别人写的** server 说话。")
    print("server：%s\n" % server)
    for c in r["checks"]:
        print("  %s %s" % ("✓" if c["ok"] else "✗", c["name"]))
        if c["detail"]:
            print("      %s" % c["detail"].replace("\n", "\n      ")[:500])
    print("\n%s" % ("四条全过 —— 「支持 MCP」现在有依据了。" if r["ok"]
                    else "有不过的 —— 这就是卡住的地方，别当环境问题绕过去。"))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
