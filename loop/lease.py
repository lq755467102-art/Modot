#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""会话租约 —— 同一个会话不许被跑两遍

先说清楚这解决的不是什么
    **不是"让多个会话同时跑"**，也不是"防两个会话改同一个文件"。
    它只防一件事：**同一个会话目录被两轮同时写** —— events / 存档会互相覆盖，
    而那种覆盖是静默的（看起来只是"它好像忘了刚才的事"）。

★ 锁的是**会话目录**，不是工作目录（2026-10-26 更正，这一段原来写反了）
    第一版按 `work_root` 加锁，效果是"同一项目里第二个会话只能干等" ——
    用户的原话是「本质上是**伪装成并行的串行**」。那条路已经废掉。
    现在的判据：
      · 同一个会话跑两遍      ⇒ 拦（events 会互相覆盖，这是硬冲突）
      · 不同会话各跑各的      ⇒ 放（各自写自己的会话目录，不冲突）
      · 不同会话改同一个源文件 ⇒ **不拦，也不该由一把锁替人决定**
        那是"人在同一时间做了两件事"的判断，交给人和 git。
    所以这个文件里提"目录"的地方，指的都是**会话目录**。

★ 2026-11-01 补：第三条那一格现在有**目击者 + 半静默拦截**了，但仍然没有锁
    `loop/claims.py` 记"谁在动哪个文件"（登记永远成功，不排斥任何人），
    `bin/watchtower.py` 发现两个委托重叠就往双方投一条通知，
    在下一个执行步开头进上下文（`loop/loop.py` 的 `_take_notices`）。
    `loop/tools.py` 在写之前多问一句：第一次撞上只警告，被提醒过还执意要写、
    对方也没让路 ⇒ **那一次写不执行**，但任务不停、不要人授权。
    **它拦的是那一次写，不是这个会话** —— 要不要并行仍然是人的决定。

进程生命周期由操作系统文件锁保护。活进程不会因为心跳过期而被接管；
进程退出后操作系统释放锁。JSON 元数据仅用于诊断，原子替换不会替换锁文件。
旧版（无 lock_version）的租约仍按心跳 TTL 兼容；升级前先停止旧进程。
锁域是本机文件系统，不提供跨主机分布式一致性。
"""
# 主要引用方包括 `runtime.py` 和 `bin/server.py`。
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from pathlib import Path

from .filelock import FileLock, atomic_json
from . import paths as P            # 程序根 / 数据根，只此一处
ROOT = P.data_root()               # 数据根（打包后是用户目录；开发时就是仓库根）
LOCKS = ROOT / "runs" / "locks"

DEFAULT_TTL = 900          # 15 分钟。比一次最长模型调用（300 秒）宽，比"人离开"短。


def _key(cwd: str | Path) -> str:
    """目录 → 稳定的短名。用 sha1 前 12 位，避免路径太长/带非法字符。"""
    p = str(Path(cwd).resolve()).lower()          # Windows 大小写不敏感
    return hashlib.sha1(p.encode("utf-8")).hexdigest()[:12]


def path_for(cwd: str | Path) -> Path:
    return LOCKS / ("%s.json" % _key(cwd))


def _age_sec(stamp: str) -> float:
    try:
        t = dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return 10 ** 9
    return (dt.datetime.now() - t).total_seconds()


class Lease:
    """一个会话目录的独占租约。拿不到就返回持有者信息，**不许静默继续**。"""

    def __init__(self, cwd: str | Path, session_id: str, ttl: int = DEFAULT_TTL):
        self.cwd = str(Path(cwd).resolve())
        self.path = path_for(cwd)
        self.session_id = session_id
        self.ttl = ttl
        self.held = False
        self._lock = FileLock(self.path.with_suffix(".lck"), timeout=0)
        # 每一次运行都有独立所有者。同名会话也不许覆盖活租约；
        # 旧进程的 touch/release 更不能改写或删掉新进程的租约。
        self.owner = uuid.uuid4().hex

    # ── 读 ──
    def holder(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def alive(self) -> dict | None:
        """还活着就返回持有者；过期或不存在返回 None。"""
        h = self.holder()
        probe = FileLock(self.path.with_suffix(".lck"), timeout=0)
        if not probe.acquire():
            return h or {"session": self.session_id, "state": "starting"}
        probe.release()
        if h and h.get("lock_version") == 1:
            return None  # kernel ownership ended, even if heartbeat is recent
        return h if h and _age_sec(h.get("heartbeat") or "") <= self.ttl else None

    # ── 拿 ──
    def acquire(self) -> tuple[bool, dict | None, str]:
        """返回 (拿到了吗, 持有者, 说明)。

        ★ 过期租约接管要留痕：`took_over_from` 写进文件，
          这样事后能回答"这个目录上一次是谁在用、为什么没释放"。
        """
        if self.held:
            return False, self.holder(), "这个目录已经有会话在跑"
        if not self._lock.acquire():
            return False, self.holder(), "这个目录已经有会话在跑"
        old = self.holder()
        if old and old.get("lock_version") != 1 and _age_sec(old.get("heartbeat") or "") <= self.ttl:
            self._lock.release()
            return False, old, "这个目录有旧版本活租约，等待其退出"
        old = None

        took = ""
        prev = self.holder()
        if prev and not old:
            took = prev.get("session") or "?"
            # 留痕：追加到同一个文件的历史里，不吞掉
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "cwd": self.cwd,
            "session": self.session_id,
            "owner": self.owner,
            "lock_version": 1,
            "pid": os.getpid(),
            "started": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "heartbeat": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ttl": self.ttl,
        }
        if took:
            rec["took_over_from"] = took
            rec["took_over_at"] = rec["started"]
        try:
            atomic_json(self.path, rec)
        except Exception:
            self._lock.release()
            raise
        self.held = True
        return True, None, ("接管了一个过期租约（上一个：%s）" % took) if took else "拿到"

    # ── 续 / 放 ──
    def touch(self) -> None:
        """刷新诊断元数据；实际存活判据是内核文件锁。"""
        if not self.held:
            return
        h = self.holder() or {}
        if h.get("owner") != self.owner:
            self.held = False
            self._lock.release()
            return
        h["heartbeat"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            atomic_json(self.path, h)
        except Exception:
            pass

    def release(self) -> None:
        """只在自己是持有者时才删 —— 否则会把别人的租约删掉。"""
        if not self.held:
            return
        h = self.holder() or {}
        if h.get("owner") == self.owner:
            try:
                self.path.unlink()
            except Exception:
                pass
        self.held = False
        self._lock.release()


def describe(h: dict | None) -> str:
    if not h:
        return "（没有租约）"
    age = _age_sec(h.get("heartbeat") or "")
    return ("会话 %s（pid %s，开始于 %s，最后一次心跳 %.0f 秒前）"
            % (h.get("session") or "?", h.get("pid") or "?",
               h.get("started") or "?", age))

