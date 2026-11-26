#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""会话 —— 让"一次任务"成为一个**能看、能停、能接着跑**的东西

为什么需要它
    在这之前，`loop.run()` 的全部状态只活在一个函数里：返回了就没有了。
    `trace` 要到 `_finish` 才一次性写盘 ⇒ **跑的过程中磁盘上什么都没有**。
    所以 UI 想实时显示，无从读起；`Ctrl+C` 之后想接着跑，也无从接起。

三样东西，各自解决一件事
    events.jsonl   每一条动作立刻追加一行。为了**看**：UI 可以 tail，不用等结束。
    session.json   每一轮把 msgs 和状态存一次。为了**接**：中断之后能续上。
    paused 状态    第五个出口。为了**停**：知道自己停在哪，不是"崩了"。

一条设计约束：事件必须每步立刻落盘
    和 `bin/swap.py` 的 journal 是同一条教训 —— 攒到最后写，崩了就只知道"开始了"，
    不知道"做到第几步"。区别只是那边防的是半截文件，这边防的是"看不见它在干什么"。

一条克制：不传 session 就退化成现在这样
    验收里有 366 条用例直接调 `LOOP.run()`，它们不需要落盘，也不该被强迫落盘。
    所以 session 是可选的 —— 但**默认路径（run-task）必须传**，否则地基等于没搭。
"""
# 主要引用方包括 `check_sessions.py` 和 `subagent.py`。
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import uuid
from pathlib import Path

from . import paths as P            # 程序根 / 数据根，只此一处
ROOT = P.data_root()               # 数据根（打包后是用户目录；开发时就是仓库根）
SESSIONS = ROOT / "runs" / "sessions"

# 写事件时的那把锁。**只保护"算序号 + 追加一行"这一小段**，不保护整个任务 ——
# 子 agent 该并行还得并行，只是不许同时改同一个文件。
_SEQ_LOCK = threading.Lock()

# ★ 哪些事件必须**扛住强杀**（flush + fsync 到磁盘）。
#   普通事件丢了只是"少看一行过程"，而这几条丢了，人会得出**相反**的结论：
#     end                     —— 没有它，界面上这一轮永远"在跑"
#     error                   —— 没有它，崩了看起来像卡住
#     paused / paused_mid_tools —— 没有它，人不知道自己按的暂停生效了没
#     needs_human / ask_human —— 停在这里等人，事件没了就等于没在等
#     snapshot_failed / denied —— 安全相关：这次改动没底稿 / 这一步被拒了
#   ★ 为什么不全量 fsync：一次任务几百条事件，每条一次 fsync 是几十毫秒级的
#     磁盘等待，而且落在**每一步之间**。关键事件少（每轮 1~3 条），付得起；
#     普通事件 flush 就够 —— 进程活着就一定写进去了，强杀/断电才会丢最后几条，
#     而那几条恰恰不是判据。
_FSYNC_ACTS = {"end", "error", "paused", "paused_mid_tools", "needs_human",
               "ask_human", "snapshot_failed", "denied", "stuck", "model_output"}


def new_id() -> str:
    """新的会话号。

    ★ 为什么带 4 位随机尾巴（2026-10-27 改）
      原来只有 `%Y%m%d-%H%M%S` —— **秒级精度**。同一秒里建两个会话就同名，
      而 `Session.__init__` 是 `mkdir(exist_ok=True)`，于是第二个会话
      **静默复用第一个的目录**：events 混写、session.json 互相覆盖、
      租约互相顶掉，界面上只表现为"会话少了 / 顺序乱了"。
      实测：连续 5 次调用拿到同一个号。

      `bin/server.py` 自己补了 `-2`、`-3`，但那是**调用方补救** ——
      CLI、探针、子 agent 目录任何一处忘了补就复发。唯一性该由这里保证。

    ★ 尾巴为什么是 8 位十六进制（32 bit）而不是 4 位：4 位只有 16 bit，
      按生日问题，200 次抽取里撞一次的概率约 **26%** —— 验收第一次就把我抓了
      （IDS 199/200）。8 位在同样规模下约 0.005%，而目录名只长 4 个字符。
      ★ 但**不能只靠它**：`Session.__init__` 还留了"目录已存在就换一个"的硬保护，
        两道防线都要在 —— 概率这件事只负责把碰撞变得罕见，不负责正确。
    """
    return "%s-%s" % (dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
                      uuid.uuid4().hex[:8])


from .filelock import FileLock, atomic_json

MAX_EVENT_LOG = 128 * 1024 * 1024
MAX_EVENT_LINE = 2 * 1024 * 1024

def ensure_event_capacity(directory):
    directory = Path(directory)
    events = directory / "events.jsonl"
    if (directory / "history-capacity.json").exists() or (events.exists() and events.stat().st_size >= MAX_EVENT_LOG):
        raise RuntimeError("会话证据容量已满；保留原始记录，未开始新工作。请导出并另开会话")



class Session:
    """一次任务的落盘载体。**每个事件、每一轮都立刻写。**"""

    def __init__(self, path: Path | str | None = None, ident: str = "",
                 on_event=None):
        if path is not None or ident:
            # 显式指定 ⇒ 沿用原语义（续跑/指定名字时**要**落回同一个目录）
            self.dir = Path(path) if path else (SESSIONS / ident)
            self.dir.mkdir(parents=True, exist_ok=True)
        else:
            # 自动编号 ⇒ 绝不复用别人的目录。
            # ★ 用 `exist_ok=False` 让"撞名"变成异常，而不是静默合流：
            #   哪怕 new_id() 以后又被改回秒级，这里也挡住（两道防线）。
            base = new_id()
            self.dir = SESSIONS / base
            n = 2
            while True:
                try:
                    self.dir.mkdir(parents=True, exist_ok=False)
                    break
                except FileExistsError:
                    if n > 50:
                        raise
                    self.dir = SESSIONS / ("%s-%d" % (base, n))
                    n += 1
        ensure_event_capacity(self.dir)
        self.events_path = self.dir / "events.jsonl"
        self.state_path = self.dir / "session.json"
        from . import runtime as RT
        owner = RT.read(self.dir)
        creation = RT.identity(os.getpid())
        self.run_id = owner.get('run_id') if creation not in (None,'unknown') and owner.get('pid') == os.getpid() and owner.get('identity') == creation else None
        # ★ 订阅者。命令行拿它打印进度，将来的 UI 拿它刷界面 —— **同一份事件**。
        #   这就是为什么先做事件流再做界面：界面只是第二个订阅者，不是另一套数据。
        self.on_event = on_event
        # 续跑时接着原文件往后写，不覆盖 —— 覆盖就等于把上一次的历史擦掉了
        with FileLock(self.dir / 'events.lock'):
            if not self.events_path.exists():
                self.events_path.write_text("", encoding="utf-8")
            # ★★ seq 必须接着已有的往下数。实测踩到：续跑时从 1 重来，
            #   于是同一个文件里出现两批 seq=1,2,3…… **按序号排/去重/断点续读全废**，
            #   而界面上看起来只是"顺序有点乱"，很难联想到是计数器没接上。
            self.seq = self._last_seq()
            # 记着上次写完时文件多大。下次 emit 前对一下 —— 变大了就说明**别人也写过**，
            # 得把序号接上去，否则两个 Session 会各自从自己的计数往下数（实测会重号）。
            try:
                self._size = self.events_path.stat().st_size
            except OSError:
                self._size = 0

    def _last_seq(self) -> int:
        """接着已有事件往下数。**取「最大 seq」和「总行数」里的大者。**

        为什么两个都要：只看最大 seq，遇到字段缺失或历史坏档（曾经重过号）就会接回一个
        已经用过的数；只看行数，遇到被手工删过几行的文件又会重号。
        两个取大是最笨也最稳的 —— **宁可跳号，不可重号**：跳号只是看着有个空档，
        重号会让"按序号排/去重/断点续读"全错，而界面上只表现为"顺序有点乱"。
        """
        if not self.events_path.exists():
            return 0
        last = count = 0
        discarding = False
        with self.events_path.open("rb") as stream:
            while True:
                line = stream.readline(MAX_EVENT_LINE + 1)
                if not line:
                    break
                if not line.endswith(b"\n") and len(line) > MAX_EVENT_LINE:
                    discarding = True
                    continue
                if discarding:
                    if line.endswith(b"\n"):
                        count += 1
                        discarding = False
                    continue
                if not line.strip():
                    continue
                count += 1
                try:
                    last = max(last, int(json.loads(line).get("seq") or 0))
                except (ValueError, TypeError, AttributeError, RecursionError):
                    continue
        return max(last, count)

    # ── 事件：每一条立刻落盘 ──
    def emit(self, act: str, **kw) -> dict:
        from .private_keys import clean
        kw = clean(kw)
        if self.run_id:
            kw.setdefault('run_id',self.run_id)
        # ★ 两把锁，防的是两件不同的事：
        #   ① 进程内多线程（父子 agent 各写各的会话）—— 用 threading.Lock
        #   ② 同目录被多个进程打开 —— 文件锁保护重读序号和追加的完整操作
        # 实测（subagent-probe.py）：不加这两下时，两个 Session 写同一目录会出现
        # **40 行里 20 个重号**，而界面上只表现为"顺序有点乱"，很难联想到是计数器没接上。
        with _SEQ_LOCK, FileLock(self.dir / "events.lock"):
            try:
                size_now = self.events_path.stat().st_size if self.events_path.exists() else 0
            except OSError:
                size_now = self._size
            if size_now != self._size:
                # 别人往这个文件里写过 ⇒ 把序号接上去，别各自从自己的计数往下数
                self.seq = max(self.seq, self._last_seq())
                self._size = size_now
            ensure_event_capacity(self.dir)
            row = {"seq": self.seq + 1, "t": dt.datetime.now().strftime("%H:%M:%S"), "at":dt.datetime.now().timestamp(), "act": act}
            row.update(kw)
            encoded = json.dumps(row, ensure_ascii=False) + "\n"
            if len(encoded.encode("utf8")) > MAX_EVENT_LINE or size_now + len(encoded.encode("utf8")) > MAX_EVENT_LOG:
                atomic_json(self.dir / "history-capacity.json", dict(state="capacity", limitBytes=MAX_EVENT_LOG, notice="证据容量不足，原记录保留，后续执行已拒绝；请导出后另开会话"))
                raise RuntimeError("会话证据容量不足，未追加事件或开始下一步；原记录保留")
            self.seq += 1
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(encoded)
                # ★ 文档一直写的是"每条事件**立刻**落盘"，而这里原来只是 `write()`
                #   —— 它进的是进程缓冲区，**不在盘上**（2026-10-27 深审）。
                #   进程被强杀/断电时，最后几条可能还没出去，于是
                #   "状态说结束了、事件流没有 end"或反过来。
                #   ⇒ 普通事件 flush（进程一活着它就是可读的，UI 也就不必等）；
                #     关键事件再加 fsync（见 _FSYNC_ACTS 的理由）。
                f.flush()
                if act in _FSYNC_ACTS or act == 'say' and kw.get('completionReview'):
                    try:
                        os.fsync(f.fileno())
                    except (OSError, ValueError):
                        pass      # 某些文件系统/管道不支持 fsync，别为它带崩这一轮
            try:
                self._size = self.events_path.stat().st_size
            except OSError:
                pass
        if act in ("tool_cancelling", "needs_human", "prep", "benni_start"):
            from . import runtime as RT
            try:
                RT.update_owned(self.dir,
                    state={"tool_cancelling":"stopping", "needs_human":"waiting_approval"}.get(act),
                    phase={"prep":"preparing", "benni_start":"reviewing"}.get(act))
            except (OSError, TimeoutError) as error:
                # The durable event is authoritative; a projection failure must
                # not erase the tool outcome or stop event delivery.
                import warnings
                warnings.warn("runtime projection failed: %s" % error, RuntimeWarning)
        if self.on_event is not None:
            try:
                self.on_event(row)
            except Exception:
                pass          # 订阅者炸了不能带崩 agent —— 看是旁路，不是主路
        # Diagnostic sidecar only; neither model context nor primary events change.
        try:
            from .model_observation import observe_event
            observe_event(self, row)
        except Exception:
            pass
        if act in {'user', 'say', 'ask_human', 'end', 'paused', 'paused_mid_tools', 'error',
                   'provider_stream_interrupted', 'role_phase'}:
            try:
                from .conversation_record import notify
                notify(self.dir, row)
            except Exception:
                pass  # The primary event has already been written.
        return row

    # ── 状态：每轮存一次（这就是 checkpoint） ──
    def owned_run_id(self):
        from . import runtime as RT
        owner = RT.read(self.dir)
        creation = RT.identity(os.getpid())
        return self.run_id if self.run_id and owner.get('run_id')==self.run_id and owner.get('pid')==os.getpid() and creation not in (None,'unknown') and owner.get('identity')==creation else None

    def save(self, **state) -> None:
        from .private_keys import clean
        with FileLock(self.dir/'runtime.lock'), FileLock(self.dir / "state.lock"):
            owned = self.owned_run_id()
            if owned:
                state['run_id'] = owned
            elif self.run_id:
                raise RuntimeError('会话运行所有权已变化，不能覆盖新轮次状态')
            old = {}
            if self.state_path.exists():
                try:
                    old = json.loads(self.state_path.read_text(encoding="utf-8"))
                except Exception:
                    old = {}
            if meter := getattr(self,'dialogue_input_meter',None):
                state['dialogueInputMeter'] = meter
            update = clean(state)
            start = getattr(self, 'task_history_start', 0)
            if start and 'msgs' in update:
                previous = old.get('msgs') or []
                if len(previous) < start:
                    raise RuntimeError('不可核实已归档的会话前缀，未覆盖原记录')
                update['msgs'] = previous[:start] + update['msgs']
            old.update(update)
            old["updated"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            old.setdefault("created", old["updated"])
            old["id"] = old.get("id") or self.dir.name
            # ★ 原子写：先写 .tmp 再换名。半截的 session.json 比没有更坏 ——
            #   它会让"续跑"读到一份不完整的状态，然后跑出更奇怪的结果。
            atomic_json(self.state_path, old)

    def load(self) -> dict:
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    # ── 读事件：给人看（也给自己排障） ──
    def events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        out = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue          # 半截行（写的时候被 killed）跳过，不要带崩读取
        return out


def list_sessions() -> list[dict]:
    """扫磁盘列会话 —— 和 board.py 一个路子：**不维护索引，永远扫真实文件**。"""
    if not SESSIONS.exists():
        return []
    rows = []
    for d in sorted(SESSIONS.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        sp = d / "session.json"
        if sp.exists():
            try:
                s = json.loads(sp.read_text(encoding="utf-8"))
            except Exception:
                s = {"id": d.name, "status": "(读不出来)"}
        else:
            s = {"id": d.name, "status": "(没有状态文件)"}
        s["dir"] = str(d)
        n = 0
        ep = d / "events.jsonl"
        if ep.exists():
            n = sum(1 for x in ep.read_text(encoding="utf-8").splitlines() if x.strip())
        s["events"] = n
        rows.append(s)
    return rows


def fmt_event(r: dict) -> str:
    """把一条事件压成**人一眼能读的一行**。

    这个格式是共享的：命令行直接打印它，将来的 UI 也用它（或它的字段）。
    两边共用一份 —— 否则界面上的和日志里的会慢慢不同步，
    而"界面显示的和实际发生的不是一回事"是最坏的一类 bug。
    """
    act = r.get("act") or "?"
    n = r.get("seq")
    t = r.get("t") or ""
    if act == "llm":
        return "[%s] %s 模型 第%s轮  in=%s out=%s" % (
            n, t, r.get("turn"), r.get("prompt_tokens"), r.get("completion_tokens"))
    if act == "charter":
        return "[%s] %s 总纲 %s 字符（第%s轮）" % (n, t, r.get("chars"), r.get("turn"))
    if act == "say":
        s = (r.get("text") or "").replace("\n", " ")
        return "[%s] %s 说   %s" % (n, t, s[:70])
    if act == "distill":
        # ★ 这一步必须出现在日志里：它是"这轮有什么值得留下"的唯一入口，
        #   看不见就等于机器还是只往外掏、不往回递。
        if r.get("skipped"):
            return "[%s] %s 提炼 没跑：%s" % (n, t, str(r.get("reason") or "")[:80])
        _its = r.get("items") or []
        if not _its:
            return "[%s] %s 提炼 0 条（%s）" % (n, t, str(r.get("reason") or "")[:60])
        return "[%s] %s 提炼 %d 条候选，等你点头（bin/distill.py list）" % (
            n, t, len(_its))
    if act == "prep":
        return "[%s] %s %s" % (n, t, str(r.get("note") or "准备中"))
    if act == "contract":
        # ★ 契约这一步必须出现在日志里：它决定"这一轮按什么判完成"，
        #   看不到就等于判据是隐形的。
        if r.get("interview"):
            # 开工前的采访：它在说"我还不知道什么"。这不是失败，是流程的一步。
            return "[%s] %s 开工前先问人：%s" % (
                n, t, "／".join(r.get("questions") or [])[:120]
                or str(r.get("reason") or "")[:80])
        if r.get("ok"):
            return "[%s] %s 契约 %d 条%s" % (
                n, t, len(r.get("items") or []),
                "（已锁）" if r.get("locked") else "（没锁上）")
        return "[%s] %s 契约 没起草出来：%s" % (
            n, t, str(r.get("reason") or "")[:80])
    if act == "benni":
        # ★ 本我的三种时刻要分开说：开始读（running）、过程中发现可疑（live）、
        #   收尾总结。混成一句"benni"的话，日志里看不出它到底出过几种东西。
        if r.get("running"):
            # ★ 带上传来的那句话（2026-10-29 实测撞到）：这里原来**写死**
            #   "本我在读这一轮的流水…"，于是 run-task 逐条转发的进度
            #   （"本我第 2 轮：给了 9 个工具"）在命令行上全变成同一句话，
            #   7 条一模一样的输出 —— 看起来像重复打印，实际内容被吞了。
            #   前端取的是 `reason`，这里取的是固定文案：**同一件事两套解释**，
            #   而两边长得都不一样的时候，只有一边是错的，你还不知道该信哪边。
            _why = str(r.get("reason") or "").strip()
            for _boring in ("本我在读这一轮的流水（另一家模型，1~3 分钟）",):
                if _why == _boring:
                    _why = ""
            if _why:
                return "[%s] %s %s" % (n, t, _why[:110])
            return "[%s] %s 本我在读这一轮的流水…" % (n, t)
        if r.get("live"):
            _its = r.get("items") or []
            _head = str((_its[0] or {}).get("text") or "")[:70] if _its else ""
            return "[%s] %s 本我（过程）指出 %d 处：%s" % (n, t, len(_its), _head)
        if r.get("skipped"):
            return "[%s] %s 本我 没跑：%s" % (n, t, str(r.get("reason") or "")[:80])
        return "[%s] %s 本我 复核完成" % (n, t)
    if act == "ask_human":
        return "[%s] %s ★ 停下来问人（%s）：%s" % (
            n, t, "开工前采访" if r.get("source") == "interview" else "干到一半",
            str(r.get("question") or "")[:100])
    if act == "paused_mid_tools":
        return "[%s] %s ⏸ 工具跑到一半被叫停：%s" % (
            n, t, str(r.get("note") or "")[:90])
    if act == "tool":
        ok = "成" if r.get("ok") else "败"
        return "[%s] %s 工具 %s %s  %s" % (
            n, t, r.get("tool"), ok, str(r.get("out") or "")[:60].replace("\n", " "))
    if act == "not_run":
        return "[%s] %s 未执行 %s（%s）" % (n, t, r.get("tool"), r.get("why") or "")
    if act == "needs_human":
        return "[%s] %s ★ 要人点头：%s 落在 %s 档（%s/%s 个调用被挡）" % (
            n, t, r.get("tool"), r.get("zone"), r.get("blocked"), r.get("total"))
    if act == "stuck":
        return "[%s] %s ★ 原地打转：%s 连续 %s 次" % (n, t, r.get("tool"), r.get("repeat"))
    if act == "error":
        return "[%s] %s ✗ 出错：%s" % (n, t, r.get("error"))
    if act == "end":
        return "[%s] %s —— 结束：%s  %s" % (n, t, r.get("status"), r.get("why") or "")
    if act == "session_save_failed":
        return "[%s] %s ⚠ 存档失败：%s" % (n, t, r.get("error"))
    keys = " ".join("%s=%s" % (k, v) for k, v in r.items()
                    if k not in ("seq", "t", "act"))
    return "[%s] %s %s %s" % (n, t, act, keys[:80])


# ───────────────────── 两套流水，一套读法 ─────────────────────

def normalize_harness(r: dict) -> dict:
    """把**循环里**的事件压成统一的五字段。

    统一成 {t, src, kind, name, ok, detail} 是为了让"读的人"只需要认一套字段。
    ★ 不能直接拼原始行：两边都有 `act`，但意思完全不同 ——
      钩子那边 `act` 是**工具名**（run / read / write），
      这边 `act` 是**事件类型**（tool / llm / say），工具名在 `tool` 里。
      直接合并会把"动作类型"和"工具名"当成同一列，读起来全错。
    """
    act = r.get("act") or ""
    t = r.get("t") or ""
    if act == "tool":
        return {"t": t, "src": "harness", "kind": "tool", "name": r.get("tool") or "",
                "ok": r.get("ok"), "detail": _one_line(r.get("out"))}
    if act == "llm":
        return {"t": t, "src": "harness", "kind": "llm", "name": "",
                "ok": None,
                "detail": "in=%s out=%s" % (r.get("prompt_tokens"), r.get("completion_tokens"))}
    if act == "say":
        return {"t": t, "src": "harness", "kind": "say", "name": "",
                "ok": None, "detail": _one_line(r.get("text"))}
    if act == "end":
        return {"t": t, "src": "harness", "kind": "end", "name": r.get("status") or "",
                "ok": r.get("status") in ("success",), "detail": r.get("why") or ""}
    return {"t": t, "src": "harness", "kind": act, "name": r.get("tool") or "",
            "ok": None, "detail": _one_line(r.get("note") or r.get("why") or "")}


def normalize_platform(r: dict) -> dict:
    """把**平台钩子**记录的流水压成同一套五字段。

    ★ `act` 在这里是工具名 —— 映射到 `name`，不是 `kind`。这是这套规范化唯一的意义所在。
    `rc` 为空表示"这个工具没有返回码概念"（比如写文件），不是"失败"。
    """
    rc = r.get("rc")
    ok = None if rc in ("", None) else (str(rc) == "0")
    detail = r.get("cmd") or ""
    if r.get("out"):
        detail = (detail + " ⇒ " + str(r.get("out")))[:120]
    return {"t": (r.get("t") or "")[-8:], "src": "platform", "kind": "tool",
            "name": r.get("act") or "", "ok": ok, "detail": _one_line(detail)}


def _one_line(v, limit: int = 100) -> str:
    return str(v or "").replace("\n", " ").replace("\r", " ")[:limit]


def read_platform_actions(day: str, root: Path | None = None) -> list[dict]:
    """读某一天的平台流水（钩子写的那个文件）。读不到就是空 —— 不报错。"""
    base = root or (ROOT / "runs")
    p = Path(base) / ("actions-%s.jsonl" % day)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue           # 半截行跳过，不带崩读取
    return out


def timeline(sess, with_platform: bool = True, root: Path | None = None) -> list[dict]:
    """把一次会话的事件、以及当天的平台流水，按时间排成一条线。

    ★ 为什么是"并读"而不是"合并成一个文件"：
      两者记的**不是同一个主体** —— 钩子记的是"平台里的 agent 做了什么"，
      会话记的是"harness 里的模型做了什么"。缝成一个文件就分不清谁做的了。
      并读 + 标 `src` 才既看得全、又分得清。
    """
    rows = [normalize_harness(r) for r in sess.events()]
    if with_platform:
        st = sess.load() or {}
        day = (st.get("created") or "")[:10].replace(" ", "-")
        if not day:
            day = dt.date.today().isoformat()
        rows += [normalize_platform(r) for r in read_platform_actions(day, root)]
    rows.sort(key=lambda x: x.get("t") or "")
    return rows

