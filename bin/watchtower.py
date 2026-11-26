#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""观察哨 —— 看着所有在跑的委托，发现争用就投一条通知，**不动任何人的手**

它解决什么
    两个委托在同一个目录里改同一个文件，谁都不会报错：后写的覆盖先写的，
    两边都以为自己改成功了。`loop/lease.py` 只防"同一个会话跑两遍"，
    它明确写着"不同会话改同一个源文件 ⇒ 不拦" —— 那条判断是对的
    （按目录加锁实测会变成"伪装成并行的串行"），但**空出来的这一格没人看着**。

它做什么
    每 10 秒读一遍 `runs/claims/*.json`（写文件时由 `loop/tools.py` 登记），
    同一路径上出现两个以上不同委托 ⇒ 给这几个委托**正在跑的那一轮**投一条通知。
    通知在下一个执行步开头进上下文（`loop/loop.py` 的 `_take_notices`）。

它不做什么（三条都是刻意的）
    · 不停任务、不要人授权 —— 拦的是**那一次写**，不是这个委托；
    · 不调模型 —— 常驻判断全是确定性规则。监督者本身要是 agent，
      它的上下文就是新的污染源，而且 7×24 烧钱；
    · 不判断"谁对" —— 只记"谁和谁在同一时间碰了同一个东西、谁拦了谁"。

★ 它会拦吗：会，但不在这一层。真正的拦在 `loop/tools.py` 的 `_cross_check`：
  第一次撞上只警告，被提醒过还执意要写、对方也没让路，那**一次写**不执行。
  本文件只负责发现、通知、留账。

判活的判据
    只看心跳（15 分钟），**不探 pid**。Windows 上探一个别人的 pid 要额外的权限和
    一堆兼容分支，而心跳已经够用：真死了，15 分钟后自然不算数。

用法
    python bin/watchtower.py --once          # 扫一遍（默认）
    python bin/watchtower.py --every 10      # 常驻
    python bin/watchtower.py --once --json   # 给验收脚本吃
"""
# 主要引用方包括 `server.py`。
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT_HINT = Path(__file__).resolve().parent.parent
if str(ROOT_HINT) not in sys.path:
    sys.path.insert(0, str(ROOT_HINT))

from loop import claims as CL          # noqa: E402
from loop import paths as P            # noqa: E402

RUNS = None                            # 延迟到 main 里定：测试要能改 HARNESS_DATA


def _runs() -> Path:
    global RUNS
    if RUNS is None:
        RUNS = P.data_root() / "runs"
    return RUNS


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _running_session(jid: str) -> Path | None:
    """这个委托**正在跑**的那一轮目录。没有就跑完了 / 没开始 —— 都不投通知。

    ★ 只投"正在跑"的：投给已经停下的会话，那条通知永远不会被读，
      却会让 watch.jsonl 看起来"已经处理过了"。
    """
    base = _runs() / "jobs" / jid
    best = (0, None)
    try:
        dirs = list(base.glob("r*"))
    except OSError:
        return None
    for p in dirs:
        n = re.sub(r"\D", "", p.name)
        if not n:
            continue
        s = _json(p / "session.json")
        if str(s.get("status") or "") == "running" and int(n) > best[0]:
            best = (int(n), p)
    return best[1]


def _key_of(path: str) -> str:
    return hashlib.sha1(str(Path(path).resolve()).lower().encode("utf-8")).hexdigest()[:10]


def conflicts(rows: list[dict]) -> list[dict]:
    """同一路径上 ≥2 个不同委托 ⇒ 一条争用。**同委托多轮不算**（那是它自己）。"""
    g: dict[str, list] = {}
    for h in rows:
        try:
            k = str(Path(str(h.get("path"))).resolve()).lower()
        except Exception:
            continue
        g.setdefault(k, []).append(h)
    out = []
    for k, hs in g.items():
        jobs = sorted({str(h.get("job") or "") for h in hs if h.get("job")})
        if len(jobs) < 2:
            continue
        out.append({"key": _key_of(k), "path": str(hs[0].get("path")),
                    "jobs": jobs, "holders": hs})
    out.sort(key=lambda x: (-len(x["jobs"]), x["path"]))
    return out


def _state_path() -> Path:
    return _runs() / "watch-state.json"


def _cooled(key: str, jobs: list[str], seconds: int) -> bool:
    """这个组合刚通知过吗。冷却期内不重复投 —— 每 10 秒投一次等于刷屏。"""
    st = _json(_state_path())
    at = str((st.get("%s|%s" % (key, ",".join(jobs))) or {}).get("at") or "")
    if not at:
        return False
    try:
        t = dt.datetime.strptime(at, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    return (dt.datetime.now() - t).total_seconds() < seconds


def _mark(key: str, jobs: list[str]) -> None:
    p = _state_path()
    st = _json(p)
    st["%s|%s" % (key, ",".join(jobs))] = {"at": _now()}
    # 只留最近 500 条：这是冷却表，不是账本；账本在 watch.jsonl
    if len(st) > 500:
        for k in list(st)[:len(st) - 500]:
            st.pop(k, None)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".%d.tmp" % os.getpid())
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def _text_for(c: dict, jid: str) -> str:
    others = [x for x in c["jobs"] if x != jid]
    return ("`%s` 这个文件，同时还有 %s 在改。谁后写谁覆盖，而且两边都不会报错。"
            "要继续动它，先 read_file 看一遍磁盘上现在是什么；"
            "如果你这段已经做完了，可以让路给它（run_script `bin/yield.py`，"
            "args=[路径, 对方委托号]）；如果你这一轮不碰它，回一句「无关」就继续。"
            % (c["path"], "、".join(others)))


def _deliver(c: dict) -> list[str]:
    """给这条争用涉及的每个**正在跑**的委托投一条通知。返回投给了谁。"""
    got = []
    for jid in c["jobs"]:
        sess = _running_session(jid)
        if sess is None:
            continue
        box = sess / "notices"
        try:
            box.mkdir(parents=True, exist_ok=True)
            name = "%s-%s-%s.json" % (
                dt.datetime.now().strftime("%Y%m%d-%H%M%S"), c["key"],
                re.sub(r"[^A-Za-z0-9_.-]", "_", jid)[:24])
            obj = {"at": _now(), "kind": "path_conflict",
                   "path": c["path"], "jobs": c["jobs"], "for": jid,
                   "text": _text_for(c, jid)}
            tmp = box / (name + ".tmp")
            tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, box / name)
            got.append(jid)
        except OSError:
            continue
    return got


def scan_once(cooldown: int = 1800, dry: bool = False) -> dict:
    rows = CL.live()
    cs = conflicts(rows)
    acted, skipped = [], []
    for c in cs:
        if _cooled(c["key"], c["jobs"], cooldown):
            skipped.append({"path": c["path"], "jobs": c["jobs"], "why": "冷却中"})
            continue
        got = [] if dry else _deliver(c)
        if not dry:
            _mark(c["key"], c["jobs"])
            try:
                line = {"at": _now(), "kind": "path_conflict", "path": c["path"],
                        "jobs": c["jobs"], "delivered": got}
                p = _runs() / "watch.jsonl"
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
            except OSError:
                pass
        acted.append({"path": c["path"], "jobs": c["jobs"], "delivered": got})
    return {"checked": len(rows), "conflicts": len(cs),
            "acted": acted, "skipped": skipped, "dry": dry}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="看着所有在跑的委托，发现争用就投通知")
    ap.add_argument("--every", type=int, default=0, help="常驻，每 N 秒扫一遍（0=只扫一次）")
    ap.add_argument("--once", action="store_true", help="只扫一遍（默认行为）")
    ap.add_argument("--json", action="store_true", help="输出 json，给验收脚本吃")
    ap.add_argument("--dry", action="store_true", help="只报告，不投通知、不写日志")
    ap.add_argument("--cooldown", type=int, default=1800, help="同一组合多少秒内不重复通知")
    ap.add_argument("--prune", action="store_true", help="顺带清掉过期登记文件")
    a = ap.parse_args(argv)

    if a.every > 0 and not a.once:
        print("观察哨启动：每 %d 秒扫一遍（Ctrl-C 退出）" % a.every, flush=True)
        try:
            while True:
                r = scan_once(a.cooldown, a.dry)
                if r["conflicts"]:
                    print("[%s] 登记 %d，争用 %d，投递 %d"
                          % (dt.datetime.now().strftime("%H:%M:%S"), r["checked"],
                             r["conflicts"],
                             sum(len(x["delivered"]) for x in r["acted"])), flush=True)
                time.sleep(max(2, a.every))
        except KeyboardInterrupt:
            print("\n观察哨停止。", flush=True)
            return 0

    r = scan_once(a.cooldown, a.dry)
    if a.prune:
        r["pruned"] = CL.prune()
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0

    print("登记 %d 条，争用 %d 组" % (r["checked"], r["conflicts"]))
    for x in r["acted"]:
        print("  · %s ← %s（投给：%s）"
              % (x["path"], "、".join(x["jobs"]), "、".join(x["delivered"]) or "无人在跑"))
    for x in r["skipped"]:
        print("  · %s（%s）" % (x["path"], x["why"]))
    if not r["conflicts"]:
        print("  （没有两个委托同时碰同一个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
