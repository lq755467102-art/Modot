#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路径登记 —— 谁在动这个文件；**第一次撞上只警告，第二次执意才拦**

它到底是什么
    不是一把常驻的锁。**登记永远成功**，不会因为"有人也在改"就拒绝你登记；
    但 `loop/tools.py` 在真正写文件之前会问一次：你已经被提醒过了吗？
    被提醒过还要写，而且对方没让路 ⇒ **这一次写不执行**。

    ★ 为什么不一开始就拦：那样会退回"伪装成并行的串行"（2026-10-26 已否过）。
      先警告一次，把"让不让"留给模型自己决定；只有它执意、对方也没让，
      才拦这一次。保护的是**对方正在进行的工作**，不是文件本身。
    ★ 为什么拦了也不停任务、不要人授权：拦的目的是让至少一边能顺畅走下去。
      被拦的那一方换别的文件继续做就行；它需要知道的是"我被谁拦了"，
      而不是"我要等谁批我"。

谁能看见（三个读者，一条写入）
    ① 正在写的那个人：工具结果里直接回 `others` / `blocked_by`；
    ② 另一个正在跑的委托：`bin/watchtower.py` 扫到重叠就投通知，
       被拦时对方也会收到"你拦住了谁"（`notify`）；
    ③ 人：`runs/watch.jsonl` 一行一条（争用 `path_conflict`、拦截 `write_blocked`），
       任务页顶上也有一颗胶囊。

为什么一个（路径, 委托）一个文件
    并发写同一个 json 是"先读后写"，中间那个窗口就是丢数据。
    拆成"谁登记谁写自己的文件"，两个进程各写各的，glob 一次就是全集 ——
    没有合并、没有锁、没有中间态。代价是同一路径会散成 N 个小文件，
    收口靠心跳，不靠删除。

心跳：和租约同一条判据
    **看过期时间，不看文件在不在**。进程被 kill -9，登记文件会留着；
    15 分钟后自动不算数，不用人来清 —— 否则一次异常退出就会留下一条
    永远"有人占着"的假象，比没登记更坏。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from . import paths as P

ROOT = P.data_root()                 # 数据根：开发时就是仓库根，打包后是用户目录
DIR = ROOT / "runs" / "claims"

DEFAULT_TTL = 900                    # 和 lease 同值：比一次最长模型调用(300s)宽，比"人离开"短


def _key(target) -> str:
    """路径 → 稳定短名。**必须哈希**：Windows 路径本身塞不进文件名。"""
    p = str(Path(target).resolve()).lower()          # Windows 大小写不敏感
    return hashlib.sha1(p.encode("utf-8")).hexdigest()[:12]


def _safe(s) -> str:
    s = "".join(c for c in str(s or "") if c.isalnum() or c in "-_.")
    return s[:40]


def ident() -> tuple[str, int]:
    """这次写算是谁的。

    ★ 以**进程环境**为准，不用线程局部：工具跑在线程池里，主线程 bind 的值传不过去。
      委托进程启动时由 `bin/job.py` 把 `HARNESS_JOB` 放进环境（见 `cmd_run` 顶部），
      整棵树（含它起的子进程）都继承得到。命令行直接跑没有它 ⇒ 记 `solo`，
      这时它只跟自己撞，不影响判断。
    """
    return (_safe(os.environ.get("HARNESS_JOB")) or "solo"), os.getpid()


def path_for(target, job: str | None = None) -> Path:
    j = _safe(job) if job is not None else ident()[0]
    return DIR / ("%s~%s.json" % (_key(target), j or "solo"))


def _age_sec(stamp) -> float:
    try:
        t = dt.datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return 10 ** 9
    return (dt.datetime.now() - t).total_seconds()


def _read(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def who(target, ttl: int = DEFAULT_TTL) -> list[dict]:
    """这个路径上**还活着**的登记。过期的不返回，但文件不删（留给下一轮覆盖）。"""
    f = path_for(target)
    out = []
    try:
        cand = [f] if f.exists() else []
    except OSError:
        cand = []
    # 同一路径可能有多个委托登记（文件名只差 `~job`），所以按 key 前缀扫
    try:
        pref = _key(target) + "~"
        cand = [x for x in DIR.glob(pref + "*.json")]
    except OSError:
        pass
    for x in cand:
        h = _read(x)
        if not h or not h.get("path"):
            continue
        if _age_sec(h.get("heartbeat")) > int(ttl):
            continue
        out.append(h)
    return out


def live(ttl: int = DEFAULT_TTL) -> list[dict]:
    """所有还活着的登记（跨路径）。watchtower 就吃这一份。"""
    out = []
    try:
        files = list(DIR.glob("*.json"))
    except OSError:
        return out
    for x in files:
        h = _read(x)
        if not h or not h.get("path"):
            continue
        if _age_sec(h.get("heartbeat")) > int(ttl):
            continue
        out.append(h)
    return out


def own(target) -> dict | None:
    """我自己在这条路径上的登记（不管有没有过期）。

    用它记"我被提醒过谁"：即使我自己的心跳过期了，那一次提醒仍然是发生过的，
    不能因为过期就当成第一次 —— 否则双方轮流过期，就永远到不了"拦"这一步。
    """
    return _read(path_for(target)) or None


def own_all(job: str | None = None) -> list[dict]:
    """我（这个委托）登记过的所有路径。命令层拿它当候选集 ——
    只有"我被提醒过"的路径才需要跟命令文本对，通常一个都没有。"""
    j = _safe(job) if job is not None else ident()[0]
    out = []
    try:
        files = list(DIR.glob("*~%s.json" % (j or "solo")))
    except OSError:
        return out
    for f in files:
        h = _read(f)
        if h and h.get("path"):
            out.append(h)
    return out


def running_session(jid: str) -> Path | None:
    """这个委托**正在跑**的那一轮目录。没有就是没开始或已停下。"""
    import re
    base = ROOT / "runs" / "jobs" / jid
    best = (0, None)
    try:
        dirs = list(base.glob("r*"))
    except OSError:
        return None
    for p in dirs:
        n = re.sub(r"\D", "", p.name)
        if not n:
            continue
        s = _read(p / "session.json") or {}
        if str(s.get("status") or "") == "running" and int(n) > best[0]:
            best = (int(n), p)
    return best[1]


def notify(jid: str, text: str, kind: str = "info", path: str = "") -> bool:
    """往一个委托**正在跑**的会话里投一条旁路通知。

    没投成功返回 False，**不抛** —— 对方没在跑是常态，不是错误。
    """
    sess = running_session(jid)
    if sess is None:
        return False
    box = sess / "notices"
    try:
        box.mkdir(parents=True, exist_ok=True)
        name = "%s-%s.json" % (dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
                              uuid.uuid4().hex[:8])
        obj = {"at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "kind": kind, "path": path, "for": jid, "text": text}
        tmp = box / (name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, box / name)
        return True
    except OSError:
        return False


def record_block(path, holders: list[str], blocked: str) -> None:
    """记一笔"谁拦了谁"。只追加，**不停任何人的任务**。"""
    try:
        p = ROOT / "runs" / "watch.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "kind": "write_blocked", "path": str(path),
               "jobs": sorted(set(list(holders) + [blocked])),
               "holders": holders, "blocked": blocked, "delivered": []}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def yield_path(target, to_job: str, job: str | None = None) -> dict:
    """主动让路：告诉对方"这条路径我不拦你了"。

    ★ 为什么要有这个动作：拦是为了**保护正在做的工作**，不是为了抢文件。
      我这一段做完了、或者根本不在乎，就该能明确说一句"你改吧"，
      否则对方只能等我 15 分钟心跳过期 —— 那不是保护，是拖延。
    """
    t = Path(target).resolve()
    f = path_for(t, job)
    rec = _read(f) or {}
    j = _safe(job) if job is not None else ident()[0]
    if rec and _safe(rec.get("job")) != j:
        return {"ok": False, "error": "这条路径不是你登记的，不能替别人让路"}
    ys = set(rec.get("yield_to") or [])
    ys.add(_safe(to_job))
    rec.update({"path": str(t), "job": j or "solo",
                "yield_to": sorted(ys),
                "heartbeat": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    rec.setdefault("at", rec["heartbeat"])
    rec.setdefault("pid", os.getpid())
    rec.setdefault("mode", "rw")
    rec.setdefault("ttl", DEFAULT_TTL)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".%s.tmp" % uuid.uuid4().hex[:8])
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, f)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(t), "yield_to": rec["yield_to"],
            "note": "已让出：%s 可以改这条路径，我不会拦它" % to_job}


def ts_of(h: dict) -> float:
    """这条登记"什么时候第一次在这儿"的排序键。

    ★ 为什么需要毫秒：`at` 只到秒，两个委托同一秒登记就比不出谁先谁后，
      而"拦谁"恰恰取决于这个先后。老记录没有 `at_ts` 时退回秒级时间戳。
    """
    v = h.get("at_ts")
    if isinstance(v, (int, float)) and v:
        return float(v)
    try:
        return dt.datetime.strptime(str(h.get("at") or ""), "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def claim(target, mode: str = "rw", ttl: int = DEFAULT_TTL,
          job: str | None = None, pid: int | None = None,
          warned_about: list[str] | None = None) -> dict:
    """登记"我在动这个路径"。**永远返回 ok=True** —— 冲突只回信息。

    返回里 `others` 是**别的委托**还活着的登记；同委托（同一 job 的上一轮）不算冲突，
    它只会被覆盖成最新的一次 —— 否则同一个人连改两处会自己跟自己撞。
    """
    j, dpid = ident()
    j = _safe(job) if job is not None else j
    dpid = int(pid or dpid)
    t = Path(target).resolve()
    others = [h for h in who(t, ttl=ttl) if h.get("job") != j]

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prev = _read(path_for(t, j)) or {}
    # ★ `at` 保留**第一次**登记的时间：它比的是"谁先在这儿"。
    #   每次写都刷新成现在的话，后到者反而永远变成先到者，拦的方向就反了。
    warned = sorted(set((prev.get("warned_about") or []) + list(warned_about or [])))
    rec = {"path": str(t), "job": j or "solo", "pid": dpid, "mode": mode or "rw",
           "at": prev.get("at") or now, "at_ts": prev.get("at_ts") or time.time(),
           "heartbeat": now, "ttl": int(ttl)}
    if prev.get("yield_to"):
        rec["yield_to"] = prev["yield_to"]
    if warned:
        rec["warned_about"] = warned
    f = path_for(t, j)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时名再 replace：另一个进程正好在读时，不会读到半截 json。
        tmp = f.with_name(f.name + ".%s.tmp" % uuid.uuid4().hex[:8])
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, f)
    except OSError as e:
        # 登记失败**不许影响写文件本身** —— 它只是旁路。
        return {"ok": False, "path": str(t), "others": others, "error": str(e)}

    out = {"ok": True, "path": str(t), "job": rec["job"], "others": others}
    if others:
        out["note"] = ("同一个文件，%s 也登记着在改 —— 你们谁后写谁覆盖，"
                       "动手前先读一遍磁盘上现在是什么。"
                       % "、".join(sorted({str(h.get("job")) for h in others})))
    return out


def release(target, job: str | None = None) -> bool:
    """撤掉自己的登记。**只删自己那条** —— 删别人的等于替别人宣布"我干完了"。"""
    f = path_for(target, job)
    h = _read(f)
    if not h:
        return False
    j = _safe(job) if job is not None else ident()[0]
    if _safe(h.get("job")) != j:
        return False
    try:
        f.unlink()
        return True
    except OSError:
        return False


def describe(h: dict | None) -> str:
    if not h:
        return "（没有登记）"
    return ("%s（pid %s，%s 开始，最后一次心跳 %.0f 秒前）"
            % (h.get("job") or "?", h.get("pid") or "?",
               h.get("at") or "?", _age_sec(h.get("heartbeat"))))


def prune(ttl: int = DEFAULT_TTL) -> int:
    """清掉过期登记文件。只该在收尾时调 —— 跑着的时候删，等于把证据一起删了。"""
    n = 0
    try:
        files = list(DIR.glob("*.json"))
    except OSError:
        return 0
    for x in files:
        h = _read(x)
        if not h or _age_sec(h.get("heartbeat")) > int(ttl):
            try:
                x.unlink()
                n += 1
            except OSError:
                pass
    return n
