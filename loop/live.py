#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live.py —— **执行者 loop 内部的自检点**：跟着执行者的节拍走，它停它就停。

★★ 它原来叫"本我的并行观察者"，2026-10-29 改了身份（用户定的「方案 2」）
    原因：它的产出**一直**是交给执行者的（`drain()` → 主循环的 `peer`），
    只有事件标签写着 `benni`（测试者）。于是界面上显示"测试者在过程中批注"，
    而按三条 loop 串行的模型，测试者是**执行者跑完之后**才登场的另一条线 ——
    一个身份两个职能，人和模型都分不清"这是谁在说话、它属于哪一层"。

    分清之后：
      本文件 = 执行者的**自检**，属于执行层，产出只给执行者，不进测试者的判断
      review.py = **测试者**，属于测试层，执行者跑完之后才登场，产出是给人看的判断
    模型仍用另一份（独立性有价值），但那只是"用什么看"，不影响"它属于哪一层"。

这个文件和 reflex.py / review.py 的分工
    reflex.py  一次性判断：「这句话 vs 这一轮做过的动作」对不对得上。
    review.py  收尾的全量复核（读整条轨迹，实测要 3 分钟）。
    live.py    **这个文件**：一个常驻的观察者，串起上面那两种判断。
               注意它属于**执行层**（自检），不是测试层 —— 见文件头。

用户的原话（**这份口径已废弃**，2026-10-29 改成三条 loop 串行之后不再成立，
留着只为追溯"为什么会写成并行"）
    「一个模型做，另一个模型并行审查，如果自我做到一半停下思考，
      那么它（自检）也停下，等执行者动了再动。」

    ★ 现在为什么不能并行：三条线各有各的**收敛判据**（判据写出来了 /
      判据红变绿 / 证据指全了），而这些判据是不同层的产物。
      执行者和测试者同时跑，同一时间窗里就有两个判据在生效，
      于是测试者验的是**一个已经不存在的版本** —— 而结论看起来正常。
      自检不受这条限制：它用的是**执行者这一层**的判据（"我做的和我说的对得上吗"），
      所以它可以留在执行者循环里。

★ 怎么做到"自我停它就停"——不是靠信号，是靠**没有东西可读**
    自我每一次动作都会 `Session.emit()` 一行到 events.jsonl。
    自检按**文件偏移量**读增量：读不到新行，就 sleep 25ms，什么都不做。
    所以：
      执行者在模型调用里（几十秒）→ 没有新行 → 自检这几十秒**零成本空转**
      自我在跑一个长工具（两分钟）→ 同样零成本
      执行者说了一段话 / 做完一批动作 → 新行 → 自检立刻看一眼
    这比"定时轮询模型"省得多，而且**节拍天然对齐**：它动一下，它看一次。

★ 为什么用线程而不是另起进程
    `Session.emit` 已经有 `_SEQ_LOCK`（进程内线程锁）＋"文件被别人变大了就重新对齐序号"，
    多线程写同一个 events.jsonl 是它**已经处理过**的情况（当初 subagent-probe 踩过 40 行 20 个重号）。
    另起进程就要自己造一套 IPC，而且看不到主进程里的 actions —— 不值。

★ 成本闸（说清它拦的是什么）
    本地账实核对：每条事件都跑，免费。
    模型复核：只在「它说了一段话」和「攒够 12 条工具动作」时调，且总数封顶 30 次。
    ★ 封顶是**成本闸**，不是判据 —— 它拦的是"这个任务烧太多钱"，
      不是"活干完没有"。到顶之后会**明说**（emit 一条），不是静默降级。
"""
# 主要引用方包括 `legacy_entry.py` 和 `bin/accept.py`。
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

# 本地核对每来一条 say 都跑（免费）；模型复核的触发条件：
WAKE_ON_SAY = True
WAKE_EVERY_TOOLS = 12
MAX_MODEL_CALLS = 30


def _may_write(ev: dict) -> bool:
    """这条动作**有可能**改动文件吗（用来让"上次的绿"过期）。

    ★ 判断方向要保守：宁可把"可能"算成"改过"，也不要让一个过期的"绿"挂在那儿 ——
      前者只是多判几次过期（本我多说一句"这结论过期了"），后者是**假装结论还有效**。
    ★★ 为什么脚本类也算（2026-10-26）：
      原来只有 write_file / edit_file 会 +1。但 `run_script` 跑一个会写东西的脚本时，
      快照号不动 ⇒ 本我认为上一轮的结论仍然成立。脚本里改了什么我们看不见，
      所以只能按"可能改"处理。
    ★ `run_check` 不算：它的定位就是只读核验（还带命令黑名单）。
    """
    t = ev.get("tool")
    return t in ("write_file", "edit_file", "run_script", "run_acceptance",
                 "apply_patch", "delete_file")


class Watcher:
    """执行者的自检点（原称「本我的并行观察者」）。

    用法：
        w = Watcher(sess, task=task, prov_get=callable_返回provider或None)
        w.start()
        try:
            ...跑自我...
        finally:
            w.stop()
    """

    # 声称"验证通过了"的**窄**词表。
    # 为什么不复用 reflex 的 _CLAIM_DONE：那个是"声称做完了"（含"改好了 X"），
    # 而这里要的是"声称验证通过"。混用会拿"改好了 index.html"去触发复跑，
    # 白跑一次 95 秒的验收。
    # 一轮里最多复跑几次。★ 这是成本闸，不是判据：
    # 复跑一次验收 95 秒，不加盖子的话"一路改一路说改好了"能跑十几次。
    MAX_RECHECKS = 3

    _CLAIM_PASS = re.compile(
        r"(验收(全)?绿|全部通过|都过了|跑通了|测试(全)?通过|没有任何问题|全绿|绿了|过了)")

    def __init__(self, sess, task: str = "", prov_get=None,
                 interval: float = 0.25, model_timeout: float = 60.0,
                 exec_fn=None, recheck_fn=None):
        self.sess = sess
        self.task = (task or "")[:400]
        self.prov_get = prov_get
        self.interval = interval
        self.model_timeout = model_timeout

        self.path: Path = Path(sess.events_path)
        self._pos = 0
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

        # 本我自己的状态（只在这个线程里改，所以不用锁）
        self._actions: list[dict] = []      # 抄一份动作摘要，给账实核对用
        self._verify: dict | None = None    # 最近一次判据结果（"说做完但没过"要用）
        self._says = 0
        self._tools_since_wake = 0
        self._calls = 0
        self._capped = False
        self._notes_total = 0

        # 测试者的家当（2026-10-25 转向：取证者 → 测试者）
        self.exec_fn = exec_fn            # 受限执行器（复用 tools._run_check）
        # ★ 把"一条动作还原成能重跑的命令"这件事交给 tools 层（2026-10-26）。
        #   放这里自己写一套映射，就会出现"跑的是 .sh、复跑却用 python"——
        #   不报错，只是白跑一次，然后本我说"复跑失败"。
        self.recheck_fn = recheck_fn
        self._last_check = None           # (cmd 列表, 自我那次跑成没成)
        self._rechecked = set()           # 复跑过的「命令+文件状态」（同一状态只跑一次）
        self._dirty = 0                   # 文件改动计数：用来判断"上次验收的结论过没过期"
        self._tokens = 0                  # 本我自己花掉的 token（原来一个字都没记）

        # ★★ 待投递给**自我**的批注。
        #   为什么要有这个：本我只看不说，那它永远只是个旁白 ——
        #   自我下一轮开口时并不知道"刚才有人在旁边指出你这句对不上"。
        #   反驳要真的构成反驳，就得回到它的上下文里，让它解释或修正。
        #   ★ 用锁：写在本我线程，读在主循环线程。
        self._pending: list[str] = []
        self._plock = threading.Lock()

    # ── 对外 ──
    def start(self) -> None:
        self._pos = self._size()          # 从当前末尾开始，不重读历史
        self._th = threading.Thread(target=self._run, name="benni-live", daemon=True)
        self._th.start()

    def stop(self, wait: float = 5.0) -> None:
        """停下来。**先把最后几行读完**再停。

        ★ 为什么不能直接 set 就退出：最后一段话和最后一批动作是**结论所在**，
          漏掉它们等于整个过程中最该看的地方没看。实测里"它最后说做完了"
          正好是最后一条 say。
        ★ 但最后这一次**只做本地核对，不叫模型** —— 收尾那次 review.py 会读全量，
          这里再等一次模型只是拖慢退出（人已经在等结果了）。
        """
        try:
            rows = self._read_new()
            if rows:
                self._handle(rows, allow_model=False)
        except Exception:
            pass
        self._stop.set()
        if self._th is not None:
            self._th.join(wait)

    # ── 内部 ──
    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def _read_new(self) -> list[dict]:
        """读上次之后新写的行。文件被截断过就从 0 开始。"""
        try:
            size = self._size()
            if size < self._pos:
                self._pos = 0
            if size == self._pos:
                return []
            with self.path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self._pos)
                chunk = f.read()
                self._pos = f.tell()
        except OSError:
            return []
        rows = []
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows

    def _run(self) -> None:
        while not self._stop.is_set():
            rows = self._read_new()
            if not rows:
                # ★ 这一行就是"自我停，本我也停"。
                #   没有新事件 = 自我在思考/在等工具/在人回话，这一刻**不该有本我的动作**。
                self._stop.wait(self.interval)
                continue
            try:
                self._handle(rows)
            except Exception:
                # 观察者自己出问题，绝不能把自我带崩 —— 它只是旁听。
                pass

    def _handle(self, rows: list[dict], allow_model: bool = True) -> None:
        from loop import reflex as RF

        fresh_say = ""
        for r in rows:
            act = r.get("act")
            # ★ 本我自己写的事件**必须挡掉**。
            #   不挡的话下次读增量会把自己上一轮的批注当成"自我又动了"，
            #   于是它审自己 → 再写一条 → 再审自己，一路滚下去（而且越滚越慢）。
            if act == "benni":
                continue
            if act == "tool":
                self._actions.append({
                    "act": "tool", "tool": r.get("tool"),
                    "args": r.get("args") or {}, "ok": r.get("ok"),
                    "out": r.get("out"), "path": r.get("path"),
                })
                self._tools_since_wake += 1
                # 记下它最近跑过的**检查命令原文** —— 那是本我复跑时要跑的同一条。
                # 存原文而不是"解析出来的意图"：只有同一个字符串，两次结果才有可比性。
                #
                # ★★ 不能只认 run_check（2026-10-26 修的）：加了 run_script 之后，
                #   用它跑的检查一个都进不了 _last_check ⇒ 本我的"自己复跑核实"对
                #   新工具永远不触发。**两种执行工具，只有一种能被复跑。**
                #   改成一个统一的 `_cmd_of()`：谁有"一条能重跑的命令"，谁就进来。
                _cmd = None
                if self.recheck_fn is not None:
                    try:
                        _cmd = self.recheck_fn(r)
                    except Exception:
                        _cmd = None        # 还原不出来就退化成"只记 dirty"，不硬凑
                elif r.get("tool") == "run_check":
                    _c = (r.get("args") or {}).get("cmd")
                    if _c:
                        _cmd = list(_c) if isinstance(_c, (list, tuple)) else [str(_c)]
                if _cmd:
                    # 第三个字段是"做这次检查时文件改过几次" —— 快照号。
                    # 之后文件一改，这个号就对不上，结论自动过期。
                    # ★ 存的是**跑之前**的号：先记快照，再累加 dirty。
                    self._last_check = (_cmd, bool(r.get("ok")), self._dirty)
                # ★★ 这两件事不是二选一（原写成 if/elif，于是能复跑的就不再记 dirty）：
                #   一次 run_script 既"可以重跑"，也"可能改了文件" —— 两个判断都要走。
                if _may_write(r):
                    self._dirty += 1
            elif act == "say":
                fresh_say = str(r.get("text") or "")
                self._says += 1
            elif act == "verify":
                # ★ 判据结果也要跟：没有它，"说做完了但判据没过"这一条就判不出来。
                self._verify = {"ok": bool(r.get("ok")),
                                "reason": str(r.get("reason") or "")}
            # 本我自己写的事件不要再读回来（否则自己审自己，越滚越大）
            # 其它类型（llm/charter…）本我不关心：它看的是"说了什么、做了什么、判过没有"

        if not fresh_say and self._tools_since_wake < WAKE_EVERY_TOOLS:
            return

        # ⓪ 复跑：它说"过了"，我就自己跑一遍**同一条命令**。
        #   这是本我第一次真的"动手"，也是它从取证者变成测试者的那一步。
        #   只读命令（复用 run_check 的黑名单与可中断），且同一条只跑一次。
        notes = self._recheck(fresh_say) if fresh_say else []
        # ① 账实核对：免费，先跑
        if fresh_say:
            notes = list(notes) + RF.check_claims(fresh_say, self._actions, self._verify)

        # ② 模型复核：到点才醒
        want_model = allow_model and (
            bool(fresh_say and WAKE_ON_SAY)
            or self._tools_since_wake >= WAKE_EVERY_TOOLS)
        if want_model:
            self._tools_since_wake = 0
            if self._calls >= MAX_MODEL_CALLS:
                if not self._capped:
                    self._capped = True
                    self._emit([{
                        "level": "fact",
                        "text": "已经看了 %d 次，到成本上限了；后面只做账实核对，不再叫模型。"
                                % self._calls,
                        "why": "MAX_MODEL_CALLS = %d（成本闸，不是判据）" % MAX_MODEL_CALLS,
                    }], note="自检 · 到了成本上限")
            else:
                extra = self._ask_model(fresh_say)
                if extra:
                    notes = list(notes) + list(extra)
                # `self._calls += 1` 已经移到 _ask_model 里 —— 调用就算成本，
                # 不管它有没有说出东西。原来只在"有输出"时计数，
                # 于是模型每次说"没问题"都不计费、闸门永不落下，会无限次重复调。

        if notes:
            self._emit(notes, note="自检 · 过程中")

    def _recheck(self, say: str) -> list[dict]:
        """自己跑一遍它声称跑过的命令，比对结果。

        为什么只跑**它自己跑过的那一条、同一个字符串**：
          各跑各的，结果不一致就没法归因（是它读错了输出，还是这中间世界变了？）。
          同一条命令才有可比性 —— 这是唯一能得出结论的对照。

        为什么只在它声称"通过"时才跑：验收一次 95 秒，这是成本闸。
          它说没过的时候，复跑改变不了什么。

        结果一致时**不刷屏**（用户要极简），但确实跑了 —— 次数记在 rechecks 里。
        """
        if self.exec_fn is None or not self._last_check:
            return []
        cmd, said_ok, snap = self._last_check
        # key 用**当前**文件状态（不是"检查时"的快照）。
        # ★ 这里第一版写错了：用 snap 当 key，而 snap 在复跑后不更新 ⇒
        #   文件改过一次之后 key 永远是同一个，复跑只会发生一次，
        #   后面"改一次、说一句"全被去重吞掉 —— 而那次恰恰是最该跑的。
        key = "%s|%d" % (" ".join(str(c) for c in cmd), self._dirty)
        if key in self._rechecked:
            return []
        # 它得**声称自己完成了/通过了**才值得复跑（成本闸：一次 95 秒）。
        from loop import reflex as _RF
        if not (self._CLAIM_PASS.search(say or "") or _RF._CLAIM_DONE.search(say or "")):
            return []
        if len(self._rechecked) >= self.MAX_RECHECKS:
            return [{"level": "fact",
                     "text": "复跑到上限了，剩下的不再跑。",
                     "why": "MAX_RECHECKS = %d（成本闸）" % self.MAX_RECHECKS}]
        # 核完把快照推到当前状态：这个状态已经核过了，别再跑一遍。
        # （`stale` 的判断用的是**旧** snap，所以要在推之前算）
        _stale = snap < self._dirty
        try:
            r = self.exec_fn(cmd)
        except Exception as e:
            return [{"level": "warn",
                     "text": "我想复跑一遍，没跑起来。",
                     "why": "%s: %s" % (type(e).__name__, str(e)[:80])}]
        if not isinstance(r, dict):
            return []
        if r.get("skipped"):
            # 自我正在跑命令，这次让开了。**不占名额**：这个状态还没核过，
            # 下一句它再声称完成时还要再试。
            return []
        self._rechecked.add(key)
        self._last_check = (cmd, said_ok, self._dirty)
        ok2 = bool(r.get("ok"))
        if ok2 == said_ok:
            return []
        tail = ""
        for ln in str(r.get("out") or r.get("error") or "").strip().splitlines()[::-1]:
            if ln.strip():
                tail = ln.strip()[:70]
                break
        # ★ 两种不同的坏，分开说 —— 它们的处置也不同：
        #   ① 它那次本来就是红的却说过（读错/撒谎）
        #   ② 它那次是绿的，但**之后又改了文件**，所以现在是红的（结论过期）
        if _stale:
            text = "它那次是绿的，之后又改了文件，现在是红的。"
        else:
            text = "你说过了，我复跑没过（退出码 %s）。" % (r.get("rc"),)
        return [{"level": "warn", "text": text,
                 "why": "同一条命令：%s ｜ 我这边：%s" % (key[:80], tail)}]

    def _ask_model(self, say: str) -> list[dict]:
        """叫一次模型。**硬超时**：复核再重要也不能拖住它自己（更不能拖住自我）。"""
        if self.prov_get is None:
            return []
        try:
            prov = self.prov_get()
        except Exception:
            prov = None
        if prov is None:
            return []
        from loop import reflex as RF

        box = {"v": [], "u": {}}

        def _go():
            box["v"] = RF.model_check(prov, say, self._actions, None, self.task,
                                      usage_out=box["u"])

        t = threading.Thread(target=_go, daemon=True)
        t.start()
        t.join(self.model_timeout)
        # 调用就计成本（不管有没有输出、也不管有没有超时）。
        # 这是成本闸能被相信的前提：闸门数的是"花了几次"，不是"说了几句"。
        self._calls += 1
        self._tokens += int((box.get("u") or {}).get("total_tokens") or 0)
        if t.is_alive():
            return []
        return box["v"] or []

    def _emit(self, notes: list[dict], note: str = "") -> None:
        if not notes:
            return
        self._notes_total += len(notes)
        try:
            # ★★ 事件身份从 `benni` 改成 `check`（2026-10-29 用户定的"方案 2"）：
            #   它的产出**一直**是交给执行者的（`drain()` → 主循环的 `peer`），
            #   只有事件标签写着"测试者" —— 于是界面上显示的是"测试者在过程中批注"，
            #   而按三条 loop 串行的模型，测试者是**执行者跑完之后**才登场的另一条线。
            #   一个身份两个职能，人就分不清"这是谁在说话、它属于哪一层"。
            #   现在：它是**执行者 loop 内部的自检点**，身份是 `check`。
            #   模型仍用另一份（独立性有价值），但那不影响它属于哪一层。
            self.sess.emit("check", items=notes, note=note,
                           calls=self._calls, checked_says=self._says,
                           tokens=self._tokens, rechecks=len(self._rechecked))
        except Exception:
            pass
        # 同时挂进待投递队列 —— 自我下一轮开口前会拿到它。
        with self._plock:
            for it in notes:
                t = str(it.get("text") or "").strip()
                if t:
                    self._pending.append(t)

    def drain(self) -> list[str]:
        """本我攒下的、还没送到自我眼前的批注。主循环每轮取一次。

        ★ 取走即清空：同一句话不需要每轮重复提醒它（重复只会变成背景噪音）。
        """
        with self._plock:
            out = list(self._pending)
            self._pending = []
        return out

    # ── 给收尾用的汇总 ──
    def stats(self) -> dict:
        return {"calls": self._calls, "says": self._says,
                "notes": self._notes_total, "capped": self._capped,
                "actions": len(self._actions)}
