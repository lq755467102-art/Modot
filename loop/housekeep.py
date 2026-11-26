#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""housekeep.py —— 「只增不减」的那几处统一收口。

它解决什么
    有一类文件是**为了出错时能退回去**才写的：改规则前的留底、改记忆前的留底、
    删文件时的回收区。它们的设计目的就决定了"会一直涨" —— 而没人会记得去清。
    用三周之后：几万个小文件、几百 MB，而且**没人知道是它**。

★ 为什么默认只算不删
    这个模块唯一的动作是**删除**，而删除不可逆。所以：
      · `plan()` 只算，`apply()` 才删；
      · 命令行默认是 `list`（等于 plan），要 `apply` 才动手；
      · 启动时只**报**（`describe()` 进日志），绝不自动删 ——
        "程序自己决定删掉哪些备份"这件事，没有任何一个正确的时机。

★ 只碰白名单里的目录
    目标表写死在 `TARGETS` 里，每个都必须落在数据根下、且路径与表里一字不差。
    不接受"给我一个路径我帮你清" —— 那等于给一个递归删除的用户输入框。
    删除范围是**每个目标目录的直接子项**，不递归下钻找东西删。

★ 保留规则：keep 是**保底**，days 是**门槛**（两个都满足才删）
    · 按 mtime 倒序，前 `keep` 个**永远留着**（不然"退回去"就没得退了）；
    · 排在 `keep` 之后的，还要**超过 `days` 天**才删；`days=0` 表示不看天、超了就删。
    为什么不是"超出 keep 就删"（那样更省空间）：今天刚写的 40 份备份里，
    第 31 份可能正是"改坏之前那一版" —— 只因为排在后面就删掉它，是拿"省几 MB"
    换掉"唯一能退回去的那一步"。**先保底、再看新旧**，这个方向不能反。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import paths as P

# id, 子路径（相对数据根）, 说明, keep（至少留几个）, days（超过几天算旧，0=不看天）
TARGETS = (
    {"id": "rule-backups", "rel": ".harness/rule-backups",
     "为什么": "改规则前的留底（`ruleedit` 每次写之前存一份）",
     "keep": 20, "days": 0},
    {"id": "mem-backups", "rel": ".harness/mem-backups",
     "为什么": "改记忆前的留底（`memperms` / `facts` 删改之前存一份）",
     "keep": 30, "days": 90},
    {"id": "trash", "rel": "runs/_trash",
     "为什么": "删文件时的回收区（`tools` 的删除先挪到这里）",
     "keep": 50, "days": 14},
    # 诊断包（2026-10-26）：只在「刚出问题」那一刻有价值，过了那一阵就是磁盘占用。
    # 留 5 个：够对比「上次也是这样吗」，又不会攒成一堆。
    {"id": "diagnose", "rel": "runs/diagnose",
     "为什么": "打出来的诊断包（`bin/diagnose.py`）",
     "keep": 5, "days": 7},
    # 委托记录（2026-10-29）：一次委托 = 一个目录，每轮一个 rN（session/trace/changes/副本）。
    # L4 跑 200 轮就是几百 MB 到几 GB —— 这正是"只增不减"的典型，而它之前不在表里，
    # 等于整套 harness 自己开了个不受管的口子。
    # ★ keep 给 10 而不是 5：委托记录的价值不在"最近那一份"，在**能对比**——
    #   "上次那个类似的活为什么没过"往往要往回翻几份。而 10 份之外还有 days 门槛兜着。
    # ★ 索引不在这个目录里（`runs/jobs-index.jsonl`），所以清目录不会碰它。
    {"id": "jobs", "rel": "runs/jobs",
     "为什么": "一次委托的完整记录（每轮含 session / trace / changes / 测试者副本）",
     "keep": 10, "days": 30},
    # 备份包（2026-10-27）：`bin/backup.py` 每导一次就是一份整份现场，
    # 早期一份几百 KB 到几十 MB。留 5 个够「前几天那份还在吗」，
    # 但**不要给 0**：出事那天最想要的可能正是上一份。
    {"id": "backups", "rel": "runs/backups",
     "为什么": "导出过的整份现场（`bin/backup.py`）",
     "keep": 5, "days": 30},
)


def _dir(t: dict) -> Path:
    return P.data_root() / t["rel"]


def _safe(t: dict) -> bool:
    """这个目标目录现在能不能碰。三道：在数据根下、和表里一字不差、不是根本身。"""
    try:
        base = P.data_root().resolve()
        d = _dir(t).resolve()
    except (OSError, RuntimeError):
        return False
    if d == base or str(d) in ("", ".", "/"):
        return False
    try:
        d.relative_to(base)              # 必须在数据根内部
    except ValueError:
        return False
    return str(d) == str(Path(base) / t["rel"])


def _items(d: Path, sizes: bool = True) -> list[dict]:
    """目标目录的直接子项（文件或目录），带大小与时间。

    ★ `sizes=False` 不算大小（2026-10-26）：启动日志每次都要调 `describe()`，
      而算目录大小要 `rglob` 一遍 —— 回收区里可能是几百 MB 的会话目录，
      为了打一行日志去走一遍磁盘不值。**日常启动只数项数，清之前才算大小。**
    """
    out = []
    try:
        for p in d.iterdir():
            try:
                st = p.stat()
                size = 0
                if sizes and p.is_dir() and not p.is_symlink():
                    # 目录要算总大小才知道占了多大（副本房就是目录）
                    for f in p.rglob("*"):
                        try:
                            if f.is_file():
                                size += f.stat().st_size
                        except OSError:
                            pass
                else:
                    size = st.st_size
                out.append({"path": p, "name": p.name, "size": size,
                            "mtime": st.st_mtime, "dir": p.is_dir()})
            except OSError:
                continue
    except OSError:
        return []
    out.sort(key=lambda x: -x["mtime"])
    return out


def plan(ids=None, sizes: bool = True) -> dict:
    """算一遍：每个目标现在多少、该删哪些。**不删任何东西。**"""
    now = time.time()
    rows = []
    for t in TARGETS:
        if ids and t["id"] not in ids:
            continue
        d = _dir(t)
        ok = _safe(t)
        items = _items(d, sizes=sizes) if ok else []
        keep = int(t["keep"] or 0)
        days = int(t["days"] or 0)
        drop = []
        for i, it in enumerate(items):
            if i < keep:
                continue
            if days and (now - it["mtime"]) < days * 86400:
                continue
            drop.append({"name": it["name"], "size": it["size"],
                          "days": int((now - it["mtime"]) / 86400),
                          "dir": it["dir"]})
        rows.append({
            "id": t["id"], "path": str(d), "safe": ok,
            "count": len(items), "keep": keep, "days": days,
            "total_bytes": sum(x["size"] for x in items),
            "drop": drop,
            "drop_bytes": sum(x["size"] for x in drop),
            "why": t["为什么"],
        })
    return {"rows": rows,
            "drop_bytes": sum(r["drop_bytes"] for r in rows),
            "drop_count": sum(len(r["drop"]) for r in rows)}


def apply(ids=None, dry_run: bool = False) -> dict:
    """真删。`dry_run=True` 时只返回"本来会删什么"。"""
    p = plan(ids)
    removed, freed, errors = [], 0, []
    if dry_run:
        return {"ok": True, "dry_run": True, "removed": [],
                "would_remove": [(r["id"], x["name"]) for r in p["rows"] for x in r["drop"]],
                "freed": 0, "would_free": p["drop_bytes"], "errors": []}
    for r in p["rows"]:
        if not r["safe"]:
            errors.append("%s：路径不在白名单里，跳过（%s）" % (r["id"], r["path"]))
            continue
        d = Path(r["path"])
        # ★ 再算一次、逐项删：plan 和 apply 之间可能有人刚刚改过。
        #   这里用名字重新定位，而不是拿 plan 里的 Path 对象直接删。
        now = time.time()
        cur = _items(d)
        for i, it in enumerate(cur):
            if i < int(r["keep"] or 0):
                continue
            days = int(r["days"] or 0)
            if days and (now - it["mtime"]) < days * 86400:
                continue
            try:
                if it["dir"] and not it["path"].is_symlink():
                    shutil.rmtree(str(it["path"]), ignore_errors=False)
                else:
                    it["path"].unlink()
                removed.append((r["id"], it["name"]))
                freed += it["size"]
            except OSError as e:
                errors.append("%s/%s：%s" % (r["id"], it["name"], e))
    return {"ok": True, "dry_run": False, "removed": removed, "freed": freed,
            "errors": errors}


def _mb(n: int) -> str:
    return ("%.1f MB" % (n / 1048576.0)) if n >= 1048576 else ("%d KB" % max(1, n // 1024))


def describe() -> str:
    """一行说清"这几处现在有多少、要不要清"（进启动日志/健康页）。

    ★ 不算大小（`sizes=False`）：启动时打一行日志，不值得为它走一遍磁盘。
      要数字（"能清出多少 MB"）时用 `plan()`，那是准备动手的时候。
    """
    p = plan(sizes=False)
    over = [r for r in p["rows"] if r["drop"]]
    if not over:
        return "留底与回收区：%d 处都在上限内（清的理由只有'超了'，不是'想清'）" % len(p["rows"])
    return ("留底与回收区：%d 处超了，共 %d 项可以清 —— 跑 `python bin/housekeep.py apply`"
            "（只算不删；`apply` 才动手）"
            % (len(over), p["drop_count"]))


def report() -> dict:
    """给界面/命令行的结构化版本。"""
    p = plan()
    return {"rows": [{k: v for k, v in r.items() if k != "drop"} |
                     {"drop_n": len(r["drop"]),
                      "drop_preview": [x["name"] for x in r["drop"][:5]]}
                     for r in p["rows"]],
            "drop_bytes": p["drop_bytes"], "drop_count": p["drop_count"],
            "line": describe()}
