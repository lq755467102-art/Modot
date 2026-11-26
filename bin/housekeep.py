#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""housekeep.py（命令行）—— 看留底/回收区现在多大了，要清再清。

默认是 `list`：**只算**，什么都不删。删要显式 `apply`。

为什么默认不删
    这个脚本干的事不可逆。默认删的后果是"我以为是看看，结果清了" ——
    而那些留底是用来"改错了退回去"的，清掉之后那条退路就没了。

三个动作
    list [--json]            现在几处、各多少、能清出多少（不删）
    apply [--id X] [--dry]   真删（`--dry` 只报"本来要删什么"）
    where                    这几处到底在哪（排查"东西去哪了"用）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import housekeep as HK     # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="housekeep.py", description="留底与回收区清理（人用）")
    ap.add_argument("act", nargs="?", default="list", choices=("list", "apply", "where"))
    ap.add_argument("--id", default="", help="只处理某一处（rule-backups / mem-backups / trash）")
    ap.add_argument("--dry", action="store_true", help="只报会删什么，不真删")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    ids = [a.id] if a.id else None

    if a.act == "where":
        for t in HK.TARGETS:
            print("  %-14s %s" % (t["id"], HK._dir(t)))
        return 0

    if a.act == "list":
        p = HK.plan(ids)
        if a.json:
            print(json.dumps(HK.report() if not ids else p, ensure_ascii=False, indent=1,
                             default=str))
            return 0
        print("留底与回收区（默认只算不删）")
        for r in p["rows"]:
            print("  %-14s %3d 项 / %-9s 上限 %-3d%s%s"
                  % (r["id"], r["count"], HK._mb(r["total_bytes"]), r["keep"],
                     (" · 超 %d 天也清" % r["days"]) if r["days"] else "",
                     "" if r["safe"] else "  ⚠ 路径不在白名单，跳过"))
            if r["drop"]:
                print("                 可清 %d 项 / %s：%s%s"
                      % (len(r["drop"]), HK._mb(r["drop_bytes"]),
                         "、".join(x["name"] for x in r["drop"][:3]),
                         "…" if len(r["drop"]) > 3 else ""))
        print()
        print("  %s" % HK.describe())
        if p["drop_count"]:
            print("  要清：python bin/housekeep.py apply"
                  + ((" --id " + ids[0]) if ids else ""))
        return 0

    # ── apply ──
    r = HK.apply(ids, dry_run=a.dry)
    if a.json:
        print(json.dumps({k: (list(v) if isinstance(v, list) else v)
                          for k, v in r.items()}, ensure_ascii=False, indent=1))
    elif a.dry:
        print("只算不删：本来会删 %d 项 / %s"
              % (len(r["would_remove"]), HK._mb(r["would_free"])))
        for _i, _n in r["would_remove"]:
            print("  · [%s] %s" % (_i, _n))
    else:
        print("删了 %d 项，腾出 %s" % (len(r["removed"]), HK._mb(r["freed"])))
        for _i, _n in r["removed"][:10]:
            print("  · [%s] %s" % (_i, _n))
        if len(r["removed"]) > 10:
            print("  · …另外 %d 项" % (len(r["removed"]) - 10))
    for e in r["errors"]:
        print("  ⚠ %s" % e, file=sys.stderr)
    return 0 if not r["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
