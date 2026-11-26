#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backup.py（命令）—— 导出 / 查看 / 校验 / 还原

    python bin/backup.py plan   [--with-secrets]        先看会带哪些（不写文件）
    python bin/backup.py export [--out 路径] [--with-secrets]
    python bin/backup.py list   [包]                    看包里的清单（不还原）
    python bin/backup.py check  <包>                    核对完整性（改过就红）
    python bin/backup.py restore <包> [--force] [--home] [--with-secrets]

★ 为什么还原只有一个入口，而且不在界面上
    导出是读，还原是**覆盖整份现场**。给它一条 HTTP 入口，等于给 agent
    一条"把脚下的地板换掉"的路。这条只能由人在终端里按下。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import backup as B      # noqa: E402


def _mb(n) -> str:
    n = int(n or 0)
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024)
    return "%.1f MB" % (n / 1024 / 1024)


def _latest() -> Path | None:
    d = B.P.data_root() / B.DEFAULT_SUBDIR
    try:
        zs = sorted(d.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return zs[0] if zs else None


def cmd_plan(a) -> int:
    pl = B.plan(a.with_secrets)
    print("会带走 %d 个文件，共 %s" % (len(pl["files"]), _mb(pl["bytes"])))
    print("数据根：%s" % B.P.data_root())
    for a_ in pl["absent"]:
        print("  · 没有：%s/%s（%s）"
              % (a_["root"], a_["rel"], "空目录" if a_.get("empty") else a_["why"]))
    print("不带（%d 项）：" % len(B.SKIP))
    for k, why in B.SKIP:
        print("  - %-22s %s" % (k, why))
    if not a.with_secrets:
        print("钥匙类默认不带（要带加 --with-secrets）：")
        for root, rel, _k, why in B.SECRET_ITEMS:
            print("  - %-46s %s" % (rel, why))
    return 0


def cmd_export(a) -> int:
    r = B.export(out=a.out, with_secrets=a.with_secrets)
    if not r["ok"]:
        print("导出失败：%s" % r["error"])
        return 2
    print("导出完成：%s" % r["path"])
    print("  %d 个文件，%s；自检通过" % (r["files"], _mb(r["bytes"])))
    print("  整包指纹 sha256：%s" % r["sha256"])
    if r["redacted"]:
        print("  其中 %d 个文件含疑似凭据，**已在包里打码**（原文件没动）" % r["redacted"])
    for ab in r["absent"]:
        print("  · 这台机器上没有：%s（%s）" % (ab["name"], ab["state"]))
    for w in r["warnings"]:
        print("  ⚠ %s" % w)
    return 0


def cmd_list(a) -> int:
    p = Path(a.zip) if a.zip else _latest()
    if not p or not p.exists():
        print("没有找到备份包（%s 下还没有）。" % (B.P.data_root() / B.DEFAULT_SUBDIR))
        return 2
    try:
        m = B.inspect(p)
    except Exception as e:                     # noqa: BLE001
        print("读不出来：%s" % e)
        return 2
    if m.get("error"):
        print("✗ %s" % m["error"])
        return 2
    print("%s  （%s）" % (p.name, _mb(m.get("size", 0))))
    print("  打包时间：%s" % m.get("created"))
    print("  来源：%s" % (m.get("source") or {}).get("data_root"))
    c = m.get("counts") or {}
    print("  内容：%d 个文件，%s；打码 %s 个；没导 %d 项"
          % (c.get("files", 0), _mb(c.get("bytes", 0)),
             c.get("redacted", 0), len(m.get("skipped") or [])))
    v = m.get("verified") or {}
    print("  完整性：%s" % ("通过" if v.get("ok") else "**不通过**（%s）"
                          % (v.get("error") or "；".join(v.get("bad") or [])[:200])))
    if m.get("with_secrets"):
        print("  ⚠ 这份包里有明文钥匙，别传给别人。")
    print("  没导的（%d）：" % len(m.get("skipped") or []))
    for s in m.get("skipped") or []:
        print("    - %-22s %s" % (s.get("path"), s.get("why")))
    return 0 if v.get("ok") else 1


def cmd_check(a) -> int:
    v = B.verify(a.zip)
    if v["ok"]:
        print("✓ 完整：%d 个文件逐条哈希一致" % v["files"])
        print("  sha256：%s" % v["sha256"])
        return 0
    print("✗ 不完整：%s" % (v.get("error") or "；".join(v["bad"][:5])))
    return 1


def cmd_restore(a) -> int:
    r = B.restore(a.zip, force=a.force, with_home=a.home,
                  no_secrets=not a.with_secrets)
    if not r["ok"]:
        print("没有还原：%s" % r["error"])
        return 2
    print("还原完成：%d 个文件，%s" % (r["restored"], _mb(r["bytes"])))
    if r["backup"]:
        print("  覆盖前已把现场挪到：%s" % r["backup"])
        print("  （要退回去，就从那儿搬回来）")
    for s in r["skipped"]:
        print("  · 跳过：%s" % s)
    if not a.home:
        print("  用户目录那部分没还原（要一起，加 --home）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="把丢了就补不回来的东西打成包")
    sub = ap.add_subparsers(dest="cmd")

    p1 = sub.add_parser("plan", help="先看会带哪些（不写文件）")
    p1.add_argument("--with-secrets", action="store_true")
    p1.set_defaults(fn=cmd_plan)

    p2 = sub.add_parser("export", help="导出")
    p2.add_argument("--out", default="")
    p2.add_argument("--with-secrets", action="store_true")
    p2.set_defaults(fn=cmd_export)

    p3 = sub.add_parser("list", help="看包里的清单")
    p3.add_argument("zip", nargs="?", default="")
    p3.set_defaults(fn=cmd_list)

    p4 = sub.add_parser("check", help="核对完整性")
    p4.add_argument("zip")
    p4.set_defaults(fn=cmd_check)

    p5 = sub.add_parser("restore", help="还原（会覆盖，先留底）")
    p5.add_argument("zip")
    p5.add_argument("--force", action="store_true", help="确认覆盖已有文件")
    p5.add_argument("--home", action="store_true", help="连用户目录一起还原")
    p5.add_argument("--with-secrets", action="store_true", help="连钥匙类一起还原")
    p5.set_defaults(fn=cmd_restore)

    a = ap.parse_args()
    if not getattr(a, "fn", None):
        ap.print_help()
        return 0
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
