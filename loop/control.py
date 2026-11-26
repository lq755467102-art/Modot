#!/usr/bin/env python3
"""控制通道 —— 让界面（或另一个终端）能叫停一个正在跑的任务

为什么要有这个
    事件流是单向的：agent → 文件 → 人。人能看见发生了什么，**但没法让它停**。
    做成窗口应用之后这是硬伤 —— 点了"暂停"却只能等它自己跑完，那不叫应用。

为什么用文件，不用 socket
    ① 界面可以换任何技术（Qt / Tk / 网页 / 另一个终端），协议只有一个 json 文件；
    ② 不占端口，不会撞车，也不会因为端口被占就整个用不了；
    ③ 和这套东西的哲学一致：**文件和事件流就是真源**，内存里那份不是。
    ④ 副作用很小：任务崩了、界面关了，control.json 还在，能查是谁在什么时候叫停的。

★ 它到底停在哪里
    should_stop 在模型调用的轮询里被检查（每 0.1 秒一次），所以停的粒度是
    「最多 0.1 秒 + 一个 delta」。**已经发出去的请求照样计费** ——
    这里省的是人的等待，不是这一次调用花掉的钱。

★ 为什么不复用信号
    信号是进程内的，跨进程做不到。窗口应用和 harness 是两个进程，
    按"谁持有真源"来分，控制请求也该落在文件上。

用法（界面脚本也是这么用）
    from loop import control as CTRL
    CTRL.request_stop(sess_dir, why="界面点了暂停")
    # harness 侧的 should_stop 里用 StopWatch(...)()
"""
# 主要引用方包括 `workbench.py` 和 `code_service.py`。
from __future__ import annotations

import json
import datetime as _dt
import math
import os
import time
from pathlib import Path

NAME = "control.json"


def benchmark_deadline(sess_dir, role, *, entering=False):
    """Read a deadline supplied by an isolated, opt-in benchmark runner."""
    if os.environ.get("HARNESS_BENCHMARK_DEADLINES") != "1":
        return None
    try:
        data = json.loads((Path(sess_dir) / "benchmark-deadline.json").read_text(encoding="utf-8"))
        cutoff = data["work"]
        review = data.get("review", cutoff)
        if all(type(value) in (int, float) and math.isfinite(value) for value in (cutoff, review)):
            if role == "reviewer" and (not entering or time.monotonic() < cutoff):
                return max(cutoff, review)
            return cutoff
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def path(sess_dir) -> Path:
    return Path(sess_dir) / NAME


def request_stop(sess_dir, why: str = "", after_turn: bool = False) -> Path:
    """写一个停止请求。**只写不删** —— 停过这件事本身要留痕。

    `after_turn=True` 是「步边界暂停/插话」：当前执行步（模型调用 + 它要的工具）
    必须完整做完，到下一执行步开始前才停；立即停只保留给底层特殊调用方。
    """
    p = path(sess_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {"stop": True, "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "by": "outside", "why": why}
    if after_turn:
        obj["after_turn"] = True
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def clear(sess_dir) -> None:
    """清掉旧的停止请求。

    ★ 每轮开始必须清：否则上一轮点过的"停"会在这一轮开跑的第一秒就把自己停掉，
      而现象是"刚启动就退出"，看起来像崩了，实际是旧请求没清。
    """
    # ★ 两种入参都要认：会话目录，或 control.json 本身。
    #   实测踩到：调用方传的是 `CTRL.path(...)`（文件路径），
    #   而这里又拼了一次 NAME ⇒ 变成 `control.json/control.json`，
    #   永远不存在 ⇒ **clear 从来没有真正生效过**，
    #   于是点过一次暂停的会话会被永久锁死：每次开跑第一秒就被旧请求停掉。
    #   这种"看起来在清、其实没清"比不写更坏 —— 它让人以为问题不在这一层。
    pp = Path(sess_dir)
    target = pp if pp.name == NAME else path(pp)
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass
    return target


class StopWatch:
    """可调用的停止检查器，**按 mtime 缓存**。

    为什么不在每次调用时都读文件：它每 0.1 秒被问一次，一次模型调用最长 300 秒
    ⇒ 单次调用最多问 3000 次。文件虽小，但这是纯浪费，也会让磁盘一直抖。
    mtime 变了才重新读，其余时候直接返回上次的结论。
    """

    def __init__(self, p, *, deadline=None):
        self.path = Path(p)
        self.deadline = deadline
        self._mtime = None
        self._val = False

    def __call__(self) -> bool:
        if self.expired():
            return True
        try:
            m = self.path.stat().st_mtime
        except OSError:
            # 没有这个文件 = 没人要求停。**不是错误**，别抛。
            # ★ 而且必须把 _val 复位：文件被 clear 掉之后，同一个 watch 要恢复成"不停"。
            #   不复位的话，界面上"取消暂停"永远取消不掉 —— 而现象是
            #   "点了继续，它还是停"，没人会想到是缓存里留着上一份 True。
            self._mtime = None
            self._val = False
            return False
        if m != self._mtime:
            self._mtime = m
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                self._val = bool(d.get("stop"))
            except Exception:
                self._val = False       # 读坏了当作没要求，但不会覆盖掉已知的 True
        return self._val

    def after_turn(self) -> bool:
        """这是否是「当前执行步做完再停」的插话请求。"""
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return bool(d.get("stop") and d.get("after_turn"))
        except Exception:
            return False

    def immediate(self) -> bool:
        """供模型调用和工具执行使用：步边界插话不许在这里截断工作。"""
        return self.expired() or bool(self() and not self.after_turn())

    def expired(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    def why(self) -> str:
        try:
            return str(json.loads(self.path.read_text(encoding="utf-8")).get("why") or "")
        except Exception:
            return ""
