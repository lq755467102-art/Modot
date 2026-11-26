#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""job.py —— 一次委托 = 一个 job 目录，里面每一轮一个 rN/

为什么要有它
    现在"一轮任务"就是一次 run-task 调用，产物落在 runs/app-<时间>/。
    单轮够用，但无人值守要跑多轮，于是有三个问题答不上来：
      · 这一族轮次属于同一次委托吗？（不然两轮的产物放一起会认错）
      · 第 N 轮比第 N-1 轮好了没有？（自主暂停要看这个）
      · 这一轮的人锁参数是什么，中途有没有被改过？
    这三件事都需要一个**容器**，不是更多命令行参数 ——
    run-task 已经有 25 个参数，再加档位/范围/轮数会到 30+，
    而"哪些是人锁的"就完全看不出来了。

目录
    runs/jobs/<job-id>/
      job.json      启动参数（**人写、人改；agent 只读**）
      task.md       任务书
      r1/ r2/ ...   每一轮的完整会话（就是原来的 runs/app-* 那套结构）
      summary.json  各轮汇总：耗时、token、判据校验、测试者判断

★ 参数文件化就是"人锁"的实现形式：文件是人在开工前写的，agent 只读。
  不需要另造一套权限机制 —— 一道门比十句嘱咐可靠。

三步
    new     建 job（写 job.json + 拷任务书）
    run     跑下一轮（自动递增 rN）
    status  看各轮汇总，以及"按现在的规则该不该停"

退出码
    0 正常 / 2 用法错 / 3 轮数用尽（或 job 不存在）
"""
# 主要引用或加载入口包括 `harness_core.py`。

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

from loop import paths as P                                    # noqa: E402

ROOT = P.data_root()
JOBS = ROOT / "runs" / "jobs"
# ★ 索引故意**不放在 jobs/ 里面**（2026-10-29）：housekeep 会清理 jobs 目录的直接子项，
#   索引混在里面会被当成一个"要删的项"，而且它 mtime 天天新，还会白占一个 keep 名额。
#   它是跨 job 的聚合，不属于任何一个 job。
# ★ 索引**每次都现算**，不存成常量（2026-10-29 实测污染）：
#   测试时把 JOBS 指到临时目录，而 INDEX 还写在真实位置 ——
#   于是临时目录里的测试 job 被写进了生产索引，还留了两条自相矛盾的裁定。
#   "两个路径必须一起变"的地方，就别写成两处；写成常量也不行 ——
#   常量在 import 时就定了，测试改了 JOBS 它不会跟着变。
def _index_path() -> Path:
    return JOBS.parent / "jobs-index.jsonl"

RUN_TASK = APP / "bin" / "run-task.py"

# 人锁的默认值。**都写在 job.json 里**，不藏在代码里 ——
# 藏起来的默认值等于"人以为自己定过，其实没有"。
DEFAULTS = {
    # ★ 这一轮是「派活」还是「提问」（2026-10-28）。
    #   显式声明，不靠猜：漏传参数和"这是聊天"长得一模一样 ——
    #   有人少传一个参数，一个派活就被当成聊天，跑完还说"给出答复即完成"。
    "kind": "task",        # task 派活 / question 提问
    "crew": "l2",          # 执行和独立测试
    "max_rounds": 4,       # 愿意烧多少轮。由人定：出门给 200，先忙别的给 4-5
    "flat_rounds": 2,      # 连续几轮没超过历史最好值就停（自主暂停，见 docs/CREW…§6）
    "out": "out.md",       # 每轮产出文件名（落在 rN/ 里）
    "policy": "workspace",
    "cwd": "",
    "ctx": 0,
    "max_turns": 0,
    "scope": [],           # 允许改的路径。空 = 不敢替你定，等同于"整个工作区"
}


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _die(msg: str, code: int = 2) -> int:
    print("✗ %s" % msg, file=sys.stderr)
    return code


# 读取任务目录中首轮创建的持久任务配置，并补齐旧记录缺省值。
def _load(d: Path) -> dict:
    p = d / "job.json"
    if not p.exists():
        raise SystemExit("没有 %s —— 这不是一个 job 目录" % p)
    job = json.loads(p.read_text(encoding="utf-8"))
    # ★ 缺字段用默认补，但**不写回**文件：写回等于"我替你定了"，
    #   而人下次打开文件会以为那是自己写的。缺什么就在这次跑的时候补什么。
    for k, v in DEFAULTS.items():
        job.setdefault(k, v)
    return job


def _pick(arg: str) -> Path:
    """找 job 目录。不给就取最新的一个 —— 但要说清取了哪个，别让人猜。"""
    if arg:
        d = Path(arg)
        if not d.is_absolute():
            d = (Path.cwd() / d)
        if not (d / "job.json").exists():
            raise SystemExit("不是 job 目录：%s" % d)
        return d.resolve()
    if not JOBS.exists():
        raise SystemExit("还没有任何 job（%s 不存在）" % JOBS)
    cands = [p for p in JOBS.iterdir() if (p / "job.json").exists()]
    if not cands:
        raise SystemExit("还没有任何 job（%s 里没有 job.json）" % JOBS)
    d = max(cands, key=lambda p: p.name)
    print("（没给 --job，用最新的：%s）" % d.name)
    return d.resolve()


def _rounds(d: Path) -> list[Path]:
    return sorted([p for p in d.glob("r[0-9]*") if p.is_dir()],
                  key=lambda p: int(re.sub(r"\D", "", p.name) or 0))


def _next_n(d: Path) -> int:
    """下一轮编号。**上一轮不完整就停下**，不静默往后跳。

    ★ 为什么要有这道检查（2026-10-28）：轮次编号是"这一族轮次"的顺序依据，
      而它只靠目录名。一旦有个空目录（比如某次启动到一半就退了），
      静默 +1 会让"第几轮"整体错位，而事后对不上时极难发现。
    """
    rs = _rounds(d)
    if not rs:
        return 1
    # ★ 检查**每一轮**，不只最后一个（2026-10-28 实测踩到）：
    #   中间某轮空着（比如某次启动到一半退了），只看最后一轮是查不出来的，
    #   而那一轮会永远以"?"的样子留在汇总里 —— 看起来像"跑了一轮没结果"，
    #   实际是"那一轮根本没跑"。
    broken = [p.name for p in rs if not (p / "session.json").exists()]
    if broken:
        raise SystemExit(
            "这些轮次没有 session.json（没跑起来，或跑到一半）：%s\n"
            "  先处理掉（删掉目录，或查它为什么没起来），再往下走。\n"
            "  静默跳过它会让「第几轮」错位，而错位之后很难发现。"
            % "、".join(broken))
    return int(re.sub(r"\D", "", rs[-1].name)) + 1


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _tokens(p: Path) -> dict:
    """这一轮花了多少 token。**从事件流累加**，不猜。

    ★ 拿不到就返回空 dict，不写 0 —— 0 会被读成"没花"，而"没记"是另一件事。
    """
    ev = p / "events.jsonl"
    if not ev.exists():
        return {}
    pt = ct = 0
    seen = False
    try:
        for line in ev.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            # ★ 字段是**平铺**的（2026-10-28 实测）：`prompt_tokens` 直接挂在
            #   事件对象上，不是嵌在 `usage` 里。第一版按嵌套写，结果每一轮都
            #   显示"没记" —— 而"没记"和"花了 0"是两件完全不同的事。
            _pt = r.get("prompt_tokens")
            _ct = r.get("completion_tokens")
            if _pt is not None or _ct is not None:
                pt += int(_pt or 0)
                ct += int(_ct or 0)
                seen = True
    except OSError:
        return {}
    return {"prompt": pt, "completion": ct} if seen else {}


def _dt(s) -> "dt.datetime | None":
    """认两种格式：`2026-10-28 21:14:44`（session）与 `2026-10-28T21:14:54`（summary）。"""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return dt.datetime.strptime(str(s)[:26], fmt)
        except ValueError:
            continue
    return None


def _changed_files(p: Path) -> list:
    """这一轮真正变了的文件（相对工作区的路径）。读早就在写的 changes.json。"""
    ch = _read_json(p / "changes.json")
    out = []
    for k in ("changed", "added", "removed"):
        for it in (ch.get(k) or []):
            q = it.get("path") if isinstance(it, dict) else it
            if q:
                out.append(str(q))
    return out


def _round_facts(p: Path) -> dict:
    """一轮的事实。全部来自盘上已有的文件，**不重算、不推断**。"""
    sm = _read_json(p / "summary.json")
    st = _read_json(p / "session.json")
    rec = _read_json(p / "benni-record.json")
    want = int(st.get("round") or 0) or int(re.sub(r"\D", "", p.name) or 0)
    row = {
        "n": want,
        "dir": str(p),
        "status": sm.get("status") or st.get("status") or "",
        "turns": sm.get("turns", st.get("turns")),
        "actions": sm.get("actions"),
        "why": (sm.get("why") or st.get("why") or "")[:120],
        "at": sm.get("at") or st.get("created") or "",
        "updated": st.get("updated") or "",
        "tokens": _tokens(p),
        # 测试者的判断（自由形态下从"我的判断"那行取，没有就空）
        "verdict": ((rec.get("judgment") or {}).get("view") or ""),
        # ★ "没跑"和"跑了没给判断"必须分开（2026-10-28）：
        #   混成一句"没给判断"，人就会以为测试者看过但没意见 ——
        #   而实际可能是它压根没启动（没有轨迹、没配凭据、档位是 l0）。
        "benni_ran": bool((p / "benni-model.md").exists()),
        # 越界清单（run-task 收尾核对出来的）。空 = 这一轮没跑偏。
        "scope_violation": list(st.get("scope_violation") or []),
        "intent": rec.get("intent") or "",
        # ── 纪要要用到的三样（2026-10-28）──
        # 契约验了没、过了几条。`verify` 在 session.json 里（2026-10-28 才落盘）。
        "contract": (st.get("verify") or {}),
        # 这一轮**真正变了**的文件（内容哈希比出来的，不是模型说的）。
        "changed": _changed_files(p),
        # 测试者自己动了什么：新增（本职）/ 改了已有（要人看一眼）。
        "tester": {
            "new_files": list(rec.get("new_files") or []),
            "modified_files": list(rec.get("modified_files") or []),
            "needs_eye": bool(rec.get("needs_eye")),
        },
    }
    # 耗时：**开始 → 结束**。
    # ★ 第一版写反了（拿 summary 的 at 减 session 的 updated），于是每一轮都是负数：
    #   updated 是"最后一次 checkpoint"，at 是"收尾写完汇总"，前者本来就早于后者。
    #   负数比"没有"更坏 —— 它看起来像个数。
    t0 = _dt(st.get("created"))
    # ★ 结束时间取**这个目录里最晚的那个文件**，不是 summary 的 at（2026-10-28 实测）：
    #   `_write_summary` 在收尾复核**之前**就写了，于是汇总里的 at 早了 40 秒 ——
    #   r4 显示 14 秒，实际跑了 74 秒。差的那 40 秒正是测试者在干活。
    #   一个偏小的耗时比没有更坏：它会让人以为"这一档很便宜"。
    t1 = None
    try:
        for f in p.iterdir():
            if f.is_file():
                m = dt.datetime.fromtimestamp(f.stat().st_mtime)
                t1 = m if (t1 is None or m > t1) else t1
    except OSError:
        pass
    t1 = t1 or _dt(sm.get("at")) or _dt(st.get("updated"))
    row["elapsed_s"] = int((t1 - t0).total_seconds()) if (t0 and t1) else None
    if row["elapsed_s"] is not None and row["elapsed_s"] < 0:
        row["elapsed_s"] = None      # 时钟回拨之类，宁可不报，也不报一个负的

    # ★ 这一轮的方向（2026-10-28 自检时发现）：纪要里明明写着"这一轮干什么"，
    #   但**没有任何地方读它** —— 人想知道"它为什么这么干"，得自己去翻 rN/memo.json。
    #   而这一句正是"它当时被指到哪"的唯一记录；读不到，纪要就白写了。
    row["plan_hint"] = ""
    _mp = p / "memo.json"
    if _mp.exists():
        try:
            _m = json.loads(_mp.read_text(encoding="utf-8"))
            _pl = _m.get("plan") or {}
            _fo = _m.get("focus") or {}
            if _pl.get("plan"):
                row["plan_hint"] = "方向：%s" % str(_pl["plan"])[:100]
            elif _fo.get("ids"):
                row["plan_hint"] = "先做第 %s 条（%s）" % (
                    "、".join(str(x) for x in _fo["ids"]),
                    "规划者挑的" if _fo.get("by") == "planner" else "程序兜底")
        except Exception:
            row["plan_hint"] = ""
    return row


# ────────────────── 这一轮先做哪几条（规划者的第二种活）──────────────────
#
# 为什么"该谁干"由规则推、"先做哪条"要问规划者（2026-10-28）
#     "该谁干"是纪律：契约没过就该执行者干，模型会心软、会说"再试试"。
#     "先做哪条"不是纪律，是**依赖与卡点的判断**：5 条没过，先修哪条取决于
#     谁是谁的前提、谁的报错最具体。规则推不出来，程序也不该假装推得出来。
#
# 但它的自由度被钉在"顺序"这一个维度上（这是防多揽活的机制，不是靠提示词求它）
#     ① 能挑的编号，程序限定在"上一轮没过的那几条"，挑到别处一律丢掉；
#     ② 契约已锁，它改不了；范围是启动参数，它碰不到；
#     ③ 它只说 why，不说 how —— 一说 how 它就是第二个执行者。
#     所以它再怎么飘，也只能飘在"先做哪条"上。
#
# 三档兜底（都不算失败，但必须**明说**是哪一种）
#     crew 不是 l2 / 只剩一条 / 规划者没调通 ⇒ 程序给出"全都一起做"，
#     并在任务书里写明"这次没有规划者说话"。**假装它说了，比不说更坏**：
#     人以后翻纪要会以为顺序是有人想过的。

def _contract_total(d: Path) -> list[int]:
    """契约里一共有几条（编号 1..N）。读不到就返回空 —— 不猜。"""
    try:
        obj = json.loads((d / "contract" / "done.json").read_text(encoding="utf-8"))
        return list(range(1, len(obj.get("done") or []) + 1))
    except Exception:
        return []


def _per_items(last: dict):
    """上一轮**逐条**的判据结果。`None` = 没拿到（不是"0 条"）。"""
    v = (last or {}).get("contract") or {}
    c = v.get("contract") or {}
    items = c.get("items")
    return items if isinstance(items, list) else None


def _open_ids(d: Path, last: dict) -> list[int]:
    """这一轮**还能动**的判据编号。

    ★ "不知道"要当成"全都还要看"，不能当成"只剩几条"（2026-10-28）：
      拿不到逐条结果时若返回空，下游会读成"全过了 ⇒ 没什么要做"，
      于是把一轮真正的活变成"看起来做完了"。宁可保守地说全都还没过。
    """
    total = _contract_total(d)
    per = _per_items(last)
    if per is None:
        return total
    got = [int(x["n"]) for x in per
           if isinstance(x, dict) and str(x.get("n", "")).isdigit() and not x.get("ok")]
    # 上一轮报的是它自己的编号集合；契约被人改多了一条时，多出来的那条也算"没跑过"
    return sorted(set(got) | (set(total) - {int(x["n"]) for x in per
                                            if str(x.get("n", "")).isdigit()}))


def _pick_focus(got: dict, op: list[int]) -> tuple[list[int], list]:
    """从规划者的答复里挑出**能用的**编号。挑到别处的一律丢掉。

    ★ 这是"防多揽活"真正落地的地方（2026-10-28）：
      它挑一条**已经过了的**，意思就是"这条我想再做一遍"；
      挑一条**不存在的**，意思就是"我想加个新目标"。
      两种都不是"顺序建议"，都是在契约外面加活 —— 而契约是锁着的，加不了。
      所以这里不是"宽容地过滤一下"，是**边界**：越出 open 集合的答复等于没说。

    单独抽成函数是为了能被验收直接喂数据（不然只能靠真调模型，那测不稳）。
    """
    raw = (got or {}).get("focus")
    if not isinstance(raw, list):
        raw = [raw] if raw is not None else []
    picked, rejected = [], []
    for x in raw:
        try:
            n = int(str(x).strip())
        except (TypeError, ValueError):
            rejected.append(str(x)[:20])
            continue
        if n not in op:
            rejected.append(n)
            continue
        if n not in picked:
            picked.append(n)
    return sorted(picked), rejected


def _clean_declined(raw, limit: int = 6) -> list:
    """规划者说的「这一轮明确不做」。只保留 {what, why}，限条数、限长度。

    ★ 为什么不叫 rejected：`rejected` 已经被 `_pick_focus` 占用，
      意思是"它挑了不该挑的编号，被程序丢掉" —— 那是**非法输入**，
      而 declined 是**判断**。两个词必须分开，否则"我判断不做"和"你说错了"
      会在纪要里长成同一种东西。
    """
    out = []
    if not isinstance(raw, list):
        return out
    for x in raw[:limit]:
        if isinstance(x, dict):
            what = str(x.get("what") or x.get("claim") or "").strip()
            why = str(x.get("why") or "").strip()
        else:
            what, why = str(x).strip(), ""
        if not what:
            continue
        out.append({"what": what[:160], "why": why[:200]})
    return out


def _prev_declined(d: Path, n: int) -> list:
    """上一轮规划者明确说过不做的。读不到就是 []，不猜。"""
    if n <= 1:
        return []
    try:
        m = json.loads((d / ("r%d" % (n - 1)) / "memo.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    f = m.get("focus") or m.get("plan") or {}
    return _clean_declined(f.get("declined"))


def _cap_focus(ids: list, fmax: int) -> tuple[list, list]:
    """这一轮最多派几条。返回 (这一轮做的, 留到下一轮的)。

    ★ 为什么是硬上限而不是提示词里写"请一轮一个"：
      2026-11-01 实测，规划者会一次挑 6、7、8、9 四条，12 条判据 7 轮就跑完 ——
      任务书写"每轮一个里程碑"拦不住它。限制必须落在程序里。
    ★ 留到下一轮的**不是 declined**：它不是"不该做"，只是"这一轮轮不到"。
    """
    seq = list(ids or [])
    try:
        n = int(fmax or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return seq, []
    return seq[:n], seq[n:]


def _focus(d: Path, n: int, job: dict, memo: dict) -> dict:
    """这一轮先做哪几条判据。**只有 l4 走这里**（l2 走 `_plan`，人在场不给契约）。

    ★ 2026-10-28 改口径：原来 l2 也走这里。但 l2 人在场，契约本来就是可选项，
      让规划者去挑"先做哪几条判据"等于逼它先有一份契约 —— 那是 l4 的形态。
    """
    last = memo.get("before") or {}
    op = _open_ids(d, last)

    # ★ "该人看"优先于"该做什么"（2026-10-28）：越界、契约没过却没人能判、
    #   契约全过而测试者说没过 —— 这些时候的处置是**停下看**，不是继续挑活。
    #   在这一步还去问"先做哪几条"，等于替越界那件事找一个"接着干"的说法。
    if memo.get("next_role") == "human":
        # ★ 括起来（2026-10-28）：next_why 自己以句号收尾，直接拼会成「。。」
        return {"by": "none", "ids": [], "why": "",
                "note": "这一轮该人看，不是挑活干的时候（%s）"
                        % (memo.get("next_why") or "").rstrip("。")}

    if not op:
        # 走到这里说明角色是执行者，但契约里没有"还能动"的条目 ——
        # 只有契约是空的会这样。不派活，也不假装派了。
        return {"by": "none", "ids": [], "why": "",
                "note": "契约里没有还能动的条目 —— 先看契约是不是空的"}

    crew = job.get("crew") or "l2"
    # ★ 每轮派发上限：L4 默认 1 条（一轮一个里程碑），其它档不限。
    #   job.json 里可以显式写 focus_max 改掉；写 0 表示不限。
    _fm = job.get("focus_max")
    try:
        fmax = int(_fm) if _fm is not None else (1 if crew == "l4" else 0)
    except (TypeError, ValueError):
        fmax = 1 if crew == "l4" else 0
    if crew != "l4":
        _ids, _def = _cap_focus(op, fmax)
        return {"by": "fallback", "ids": _ids, "why": "", "deferred": _def,
                "note": "档位是 %s，这一档不靠契约排顺序 —— 所以是程序按"
                        "「还没过的全都做」给的顺序，不是谁想过" % crew}
    if len(op) <= 1:
        return {"by": "fallback", "ids": op, "why": "",
                "note": "只剩 1 条还没过，没得挑"}

    # ── 问规划者 ──
    # ★ 材料走临时文件而不是命令行参数：契约逐条可能几 KB，塞进 argv 在
    #   Windows 上会被引号/编码吃掉，而失败的样子是"规划者看到半截契约"。
    tmp = Path(tempfile.mkdtemp(prefix="focus-"))
    try:
        mp = tmp / "memo.json"
        mp.write_text(json.dumps({
            "round": n,
            "fresh": bool(last),
            "items": _per_items(last) or [],
            "open": op,
            "prev_declined": _prev_declined(d, n),
            "verdict": (last.get("verdict") or ""),
            "changed": (last.get("changed") or []),
        }, ensure_ascii=False), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(APP / "bin" / "plan.py"), "--focus",
             "--memo", str(mp), "--contract", str(d / "contract")],
            cwd=str(APP), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    line = ""
    for x in (r.stdout or "").splitlines():
        if x.startswith("FOCUS "):
            line = x[6:].strip()
    if r.returncode != 0 or not line:
        why = ((r.stderr or "").strip().splitlines() or ["没说话"])[-1]
        _ids, _def = _cap_focus(op, fmax)
        return {"by": "fallback", "ids": _ids, "why": "", "deferred": _def,
                "note": "规划者这次**没说话**（%s）—— 所以是程序给的"
                        "「还没过的全都做」，不是谁想过" % why[:160]}
    try:
        got = json.loads(line)
    except Exception:
        _ids, _def = _cap_focus(op, fmax)
        return {"by": "fallback", "ids": _ids, "why": "", "deferred": _def,
                "note": "规划者说了，但说的东西读不出来 —— 退回程序给的顺序"}
    ids, rejected = _pick_focus(got, op)
    if rejected:
        print("⚠ 规划者挑了不该挑的：%s —— 已丢掉（只能挑还没过的）"
              % "、".join(str(x) for x in rejected), file=sys.stderr, flush=True)
    if not ids:
        _ids, _def = _cap_focus(op, fmax)
        return {"by": "fallback", "ids": _ids, "why": "", "deferred": _def,
                "note": "规划者挑的都不在「还没过」里面，已经丢掉 —— 退回程序给的顺序",
                "rejected": rejected}
    ids, deferred = _cap_focus(ids, fmax)
    return {"by": "planner", "ids": ids,
            "why": str(got.get("why") or "")[:300],
            "declined": _clean_declined(got.get("declined")),
            "deferred": deferred,
            "rejected": rejected,
            "model": got.get("model") or ""}


def _render_focus(f: dict, d: Path) -> str:
    """把"先做哪几条"写成给执行者看的那一节。

    ★ 末句不能省（2026-10-28）：聚焦只是**先后**，不是把验收标准砍到剩两条。
      少了这句，执行者会把"别的判据"读成"这一轮可以不看"，而这正是
      "看起来做完了"最常见的来路。
    """
    if not f or f.get("by") == "none":
        return ""
    total = _contract_total(d)
    ids = f.get("ids") or []
    claims = {}
    try:
        obj = json.loads((d / "contract" / "done.json").read_text(encoding="utf-8"))
        for i, it in enumerate(obj.get("done") or [], 1):
            claims[i] = str(it.get("claim") or "")[:120]
    except Exception:
        pass
    who = "规划者挑的" if f.get("by") == "planner" else "程序兜底给的"
    L = ["", "## 这一轮先做哪几条（%s）" % who, "", "先做：第 %s 条"
         % "、".join(str(x) for x in ids)]
    for i in ids:
        if claims.get(i):
            L.append("- [%d] %s" % (i, claims[i]))
    if f.get("why"):
        L.append("")
        L.append("为什么先这几条：%s" % f["why"])
    if f.get("note"):
        L.append("")
        L.append("（%s）" % f["note"])
    if f.get("deferred"):
        L.append("")
        L.append("这一轮最多做 %d 条，多挑的已留到下一轮：第 %s 条"
                 % (len(ids), "、".join(str(x) for x in f["deferred"])))
    dec = f.get("declined") or []
    if dec:
        L += ["", "## 这一轮明确不做的（规划者说的）"]
        for x in dec:
            L.append("- 不做：%s%s" % (x.get("what") or "",
                                      ("—— " + x["why"]) if x.get("why") else ""))
        L.append("★ 这是**这一轮**不做，不是永远不做；下一轮会重新判断。")
    L += ["", "★ 这是**先后**，不是「其余判据可以不看」。契约 %d 条全过才算过。"
          % (len(total) or "?")]
    return "\n".join(L)


def _plan(d: Path, n: int, job: dict, memo: dict) -> dict:
    """这一轮干什么（一句话）。**L2 用这个，不用契约。**

    ★ 为什么 L2 不该有契约（2026-10-28 用户定的，这条改了口径）：
      契约是**"打断"的替代品**。人在场时，你能在第三轮说"不对，先写测试" ——
      那就是即时纠偏，不需要事先写死"什么算完成"。人不在场时这句话没人说，
      才只能提前写下来。所以 L2 给方向（人随时改），L4 给契约（没人改）。
    ★ 也不该给"判据但只是建议"：锁没锁是程序能读的，而"建议"和"契约"在人眼里
      是同一份东西。模棱两可比两种都坏。
    """
    last = memo.get("before") or {}
    if memo.get("next_role") == "human":
        return {"by": "none", "plan": "", "why": "",
                "note": "这一轮该人看，不该派人干活（%s）"
                        % (memo.get("next_why") or "").rstrip("。")}
    if (job.get("crew") or "l2") != "l1":
        return {"by": "none", "plan": "", "why": "", "note": ""}

    tmp = Path(tempfile.mkdtemp(prefix="plan-"))
    try:
        mp = tmp / "memo.json"
        task_p = d / (job.get("task") or "task.md")
        try:
            task = task_p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            task = ""
        mp.write_text(json.dumps({
            "round": n,
            "task": task,
            "fresh": bool(last),
            "verdict": (last.get("verdict") or ""),
            "changed": (last.get("changed") or []),
        }, ensure_ascii=False), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(APP / "bin" / "plan.py"), "--plan", "--memo", str(mp)],
            cwd=str(APP), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    line = ""
    for x in (r.stdout or "").splitlines():
        if x.startswith("PLAN "):
            line = x[5:].strip()
    if r.returncode != 0 or not line:
        why = ((r.stderr or "").strip().splitlines() or ["没说话"])[-1]
        return {"by": "fallback", "plan": "", "why": "",
                "note": "规划者这次**没说话**（%s）—— 这一轮按任务书干，"
                        "没有方向可用" % why[:160]}
    try:
        got = json.loads(line)
    except Exception:
        return {"by": "fallback", "plan": "", "why": "",
                "note": "规划者说了，但说的东西读不出来 —— 按任务书干"}
    pl = str(got.get("plan") or "").strip()
    if not pl:
        return {"by": "fallback", "plan": "", "why": "",
                "note": "规划者没说出这一轮干什么 —— 按任务书干"}
    return {"by": "planner", "plan": pl[:300],
            "why": str(got.get("why") or "")[:300],
            "model": got.get("model") or ""}


def _render_plan(p: dict) -> str:
    """把"这一轮干什么"写成给执行者看的那一节。

    ★ 末句同样不能省（2026-10-28）：计划不是验收标准。少了这句，
      执行者会把"这一轮先写实现"读成"测试可以不做" —— 而人还在旁边等着看。
    """
    if not p or p.get("by") == "none":
        return ""
    if not p.get("plan"):
        return "\n".join(["", "## 这一轮干什么", "", "（%s）" % (p.get("note")
                                                             or "没有方向可用")])
    L = ["", "## 这一轮干什么（规划者给的方向）", "", p["plan"]]
    if p.get("why"):
        L += ["", "为什么是这儿：%s" % p["why"]]
    L += ["", "★ 这是**方向**，不是验收标准 —— 人到时会自己判断对不对。"
              "方向之外该做的事照样要做（比如测试）。"]
    return "\n".join(L)


# 从任务记录和各轮事实生成可重建摘要，保留已登记的验收与轮次。
def _summary(d: Path) -> dict:
    job = _load(d)
    rows = [_round_facts(p) for p in _rounds(d)]
    return {
        "id": job.get("id") or d.name,
        "updated": _now(),
        "max_rounds": int(job.get("max_rounds") or 0),
        "crew": job.get("crew"),
        "scope": job.get("scope") or [],
        "rounds": rows,
        "n_rounds": len(rows),
        "stop": _stop_reason(job, rows),
        # ★ 不停但要说的事（2026-10-28）：现在只有一种 —— 这几轮的判据根本没跑出来。
        #   它进不了 stop（"不知道"不是"卡住"），但也不能就这么看不见：
        #   「连续两轮没验出结果」是最需要人知道的状态之一。
        "notice": _stall_notice(job, rows),
    }


def _passed_n(r: dict):
    """这一轮的契约通过数：(通过, 总数)。`None` = 这一轮**没拿到逐条结果**。

    ★ 三态压成两态会出人命（2026-10-28）：没有逐条结果时若返回 0，
      「没验出来」就变成了「一条都没过」。这两件事的处置完全相反 ——
      前者去查判据为什么没跑，后者接着改代码。搞反了就是朝错的方向再跑十轮。
    """
    c = r.get("contract") or {}
    items = (c.get("contract") or {}).get("items")
    if not isinstance(items, list):
        return None
    return (sum(1 for x in items if isinstance(x, dict) and x.get("ok")), len(items))


def _stop_reason(job: dict, rows: list) -> str:
    """按现在的规则，该不该停。**只回答"停不停"，从不回答"通过没通过"。**

    ★ 这条界线是刻意的：停止是收敛判断（机器能做），
      通过是裁定（只有人能做）。所以这里只说"停下来叫人"，不说"成了"。
    """
    if not rows:
        return ""
    # ★ 越界排在最前（2026-10-28）：其它停止理由都是"跑够了/没进展"，
    #   只有这条是"它跑出界了" —— 处置是立刻停下看，而不是再来一轮。
    #   放在后面会被"轮数用尽"之类盖掉，而那是最不该盖掉的一条。
    _oob = [r for r in rows if r.get("scope_violation")]
    if _oob:
        _p = []
        for r in _oob[-1:]:
            _p = r["scope_violation"]
        return ("**越界**：第 %s 轮改了范围外的 %d 个文件（%s）—— 立刻停下看，"
                "别让它接着跑" % ("、".join(str(r["n"]) for r in _oob),
                                  len(_p), "、".join(str(x) for x in _p[:3])))
    mx = int(job.get("max_rounds") or 0)
    if mx and len(rows) >= mx:
        return "轮数用尽（%d/%d）：停下来等人看" % (len(rows), mx)
    last = rows[-1]
    # ★ L4 的「测试者说看起来过」只能评价它看到的局部，不能盖过总契约。
    #   2026-11-01 长循环实测：契约只有 3/12，测试者认可本轮 CRUD 后说「看起来过」，
    #   旧规则就在这里停了，把「本轮不错」误当成「整件事做完」。
    #   成功停止必须同时满足：总契约全过 + 测试者认可；其它档位没有总契约，保持原语义。
    _is_l4 = (job.get("crew") or "l2") == "l4"
    _contract_ok = bool((last.get("contract") or {}).get("ok") is True)
    _verdict = (last.get("verdict") or "").strip()
    if _is_l4 and _contract_ok and not _verdict:
        return ("契约全过，但测试者没给判断：停下来交给人 —— "
                "没有新判据要过，再跑一轮也不会产生新的通过项")
    if _is_l4 and _contract_ok and _verdict.startswith("判不了"):
        return ("契约全过，但测试者说「判不了」：停下来交给人 —— "
                "判据已经全部通过，剩下的是测试者无法下结论，不是执行者还能修的东西")
    if (_verdict.startswith("看起来过")
            and (not _is_l4 or _contract_ok)):
        return "测试者说「看起来过」：可以停下来让人裁定了"
    return _stall_reason(job, rows)


def _stall_reason(job: dict, rows: list) -> str:
    """自主暂停：**契约通过数连续 N 轮没超过历史最好**。

    ★ 这是 2026-10-28 换掉的第二版（第一版是占位）。
      占位版看的是"turns 和 actions 都没动"—— 那是**动作量**，不是进展：
      它完全可以每轮改三个文件、动作量天天变，而判据一条都没过。
      真判据只有一条：**离"全过"近了没有**。

    ★ 不拿"没验出来"当"没进展"。不知道的时候就闭嘴 —— 停错了比不停更贵：
      人会以为"它自己判断卡住了"，而去查代码；实际上该查的是判据为什么没跑。
      这种情况单独由 `notice` 报，不进 stop。
    """
    need = int(job.get("flat_rounds") or 0)
    if need < 1 or len(rows) < need:
        return ""
    got = [_passed_n(r) for r in rows]
    tail, before = got[-need:], got[:-need]
    if any(x is None for x in tail):
        return ""
    # 契约被人 amend 过（条数变了）时，通过数不可比 —— 别拿 3/5 去比 4/6
    tot = tail[-1][1]
    if sum(1 for p in tail if p[1] == tot) < need:
        return ""
    same = [p for p in tail if p[1] == tot]
    # ★ 「已经到顶」要**先于**"没超过历史最好"判（2026-10-28 实测踩到）：
    #   已经全过的轮次拿去跟前面的历史最好比，会得出"没超过"——
    #   技术上说对，但那句话会把人送去查"为什么过不了"，而它早就全过了。
    if tot and all(p[0] == tot for p in same):
        # ★ 引用它**实际说的那句**（2026-10-29）：写死"测试者仍说没过"，
        #   而实际可能是"判不了" —— 那种话会把人送去查一个它没说过的东西。
        _v = (rows[-1].get("verdict") or "").strip()
        _said = ("测试者说「%s」" % _v[:30]) if _v else "测试者没给判断"
        return ("连续 %d 轮都是「契约 %d/%d 全过」，可角色还是执行者（%s，轮 %s）"
                " —— 这不是再跑一轮能解决的：判据已经全过，剩下的分歧在测试者那句话上，"
                "停下来看它到底在说什么"
                % (need, tot, tot, _said, "、".join(str(r["n"]) for r in rows[-need:])))
    best = max((p[0] for p in before if p and p[1] == tot), default=0)
    if not all(p[0] <= best for p in same):
        return ""          # 这 N 轮里确实有推进
    rng = "、".join(str(r["n"]) for r in rows[-need:])
    return ("连续 %d 轮契约通过数没超过历史最好（%d/%d，此前最好 %d/%d，轮 %s）—— "
            "它可能卡在同一个地方了。停下来看，别接着烧"
            % (need, tail[-1][0], tot, best, tot, rng))


def _stall_notice(job: dict, rows: list) -> str:
    """不停，但必须让人知道的事：这几轮的判据根本没跑出来。

    ★ 只在**该有契约的档位**说这句话（2026-10-28 改口径）：
      L2/L1/L0 人都在场，契约本来就可选，没有 items 是设计，不是故障。
      在那些档位报"查契约为什么没跑"，等于把人送去查一个不存在的东西。
    """
    if (job.get("crew") or "l2") != "l4":
        return ""
    need = int(job.get("flat_rounds") or 0)
    if need < 1 or len(rows) < need:
        return ""
    if _stop_reason(job, rows):
        return ""          # 已经有停的理由了，别再用提醒盖住它
    tail = rows[-need:]
    miss = [r for r in tail if _passed_n(r) is None]
    if len(miss) == len(tail):
        return ("最近 %d 轮都**没拿到判据结果** —— 不是在推进，是根本不知道"
                "推没推进（查契约为什么没跑）" % need)
    return ""


def _write_summary(d: Path) -> dict:
    s = _summary(d)
    (d / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    return s


# ───────────────────────── 纪要（每轮开工前）─────────────────────────

def _next_role(f: dict) -> tuple[str, str]:
    """这一轮该谁干。**规则推导，不问模型**。

    ★ 为什么不让模型说（2026-10-28）：这是"谁该干什么"的判断，
      而模型天然倾向于"再试试"（它自己写的方案，它下不去手）。规则推导能做到
      "判据全过就闭嘴"，模型做不到 —— 它总会给点建议。
    """
    if not f:
        return "executor", "第一轮：按契约做"
    if f.get("scope_violation"):
        return "human", ("它改了范围外的 %d 个文件（%s）—— 这是越界，不是失败。"
                         "处置是停下来看多改了什么，不是再来一轮。"
                         % (len(f["scope_violation"]), "、".join(f["scope_violation"][:3])))
    c = f.get("contract") or {}
    if not c:
        return "human", ("契约**没验**（上一轮没有判据结果）—— 先查为什么没跑，"
                         "不然这一轮又会是「看起来做了」")
    if not c.get("ok"):
        return "executor", ("契约没过：%s" % str(c.get("reason") or "")[:120])
    v = f.get("verdict") or ""
    if v.startswith("看起来过"):
        return "human", ("契约全过、测试者也说能站住 —— 可以停下来让人裁定了。"
                         "**不要再找活干**：没有新判据要过，就明说做完了")
    # ★ 「判不了」不是「有疑点」（2026-10-29 实测撞到）：它多半是测试者**没给出
    #   机器能读的那一行判断**，不是它发现了什么。让执行者"按疑点再核一遍"是
    #   白跑一轮 —— 而这一轮还会被判据全过，等于每一轮都白烧。
    #   这种时候该看正文的是人。
    if v.startswith("判不了"):
        return "human", ("测试者说「判不了」—— 它自己也没定论（多半是没给出机器能读的"
                         "判断）。这一轮没法说完成，人去看它正文里写了什么")
    if v:
        return "executor", ("判据过了，但测试者说「%s」—— 它看到了你没看到的东西，"
                            "按它的疑点再核一遍" % v)
    return "human", "判据过了，但测试者没给判断（可能没跑）—— 这一轮没法说完成"


def _open_claims(last: dict) -> list[str]:
    """上一轮还没过的判据（原话）。拿不到逐条结果时返回空，并在别处如实说「没拿到」。"""
    per = _per_items(last)
    if not per:
        return []
    return ["[%s] %s" % (x.get("n"), str(x.get("claim") or "")[:80])
            for x in per if isinstance(x, dict) and not x.get("ok")]


def _human_notes(d: Path) -> list[str]:
    """人中途补的细节（L4 也可以是**人主动开着看**的，不只是无人值守）。"""
    out = []
    try:
        lines = (d / "notes.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        if not ln.strip():
            continue
        try:
            t = str(json.loads(ln).get("text") or "").strip()
        except Exception:
            t = ""
        if t:
            out.append(t)
    return out


def _memo(d: Path, n: int) -> tuple[dict, str]:
    """第 n 轮开工前的纪要：上一轮的事实 + 这一轮该谁。

    ★ 事实部分**由程序生成，不做模型总结**（2026-10-28）：
      纪要存在的意义就是"谁做了什么"这个客观事实。让模型复述自己做了什么，
      等于把唯一一个客观来源也变成自述 —— 那就白记了。
    """
    rows = [_round_facts(p) for p in _rounds(d)]
    last = rows[-1] if rows else {}
    role, why = _next_role(last)
    # ★ 原来这里有一段 `still_open`：读 `c.get("claims")`，可 claims 在
    #   `c["contract"]["claims"]` 里 —— 永远空，而且算出来没被用过。
    #   现在由 `_open_claims()` 从**逐条结果**算，并落进 memo["before"]["open_claims"]。
    c = last.get("contract") or {}
    memo = {
        "round": n,
        "at": _now(),
        "before": ({
            "round": last.get("n"),
            "status": last.get("status"),
            "turns": last.get("turns"),
            "changed": last.get("changed") or [],
            "contract": c,
            "verdict": last.get("verdict") or "",
            "tester_ran": last.get("benni_ran"),
            "tester": last.get("tester") or {},
            "scope_violation": last.get("scope_violation") or [],
            # ★ 还没过的是哪几条（2026-10-28）：原来这里读 `c.get("claims")`，
            #   而 claims 在 `c["contract"]["claims"]` 里 —— 于是这个变量**永远是空**，
            #   还长得像"上一轮没有没过的"。这种静默的空比报错难查得多：
            #   人翻 memo.json 会以为"什么都没剩下"。
            "open_claims": _open_claims(last),
        } if last else None),
        "next_role": role,
        "next_why": why,
    }

    # ── 给执行者看的那份（写进本轮的任务书末尾）──
    L = ["", "---", "", "## 第 %d 轮开工前：上一轮留下了什么（程序汇总，不是谁的自述）" % n, ""]
    if not last:
        L += ["这是第 1 轮，没有上一轮。按契约把事做了，做完自己核一遍判据。"]
    else:
        ch = last.get("changed") or []
        L.append("- 上一轮（第 %s 轮）：%s，用了 %s 轮对话"
                 % (last.get("n"), last.get("status") or "?", last.get("turns") if last.get("turns") is not None else "?"))
        L.append("- 真正变了的文件：%s" % ("、".join(ch[:8]) if ch else "**一个都没有**"))
        if c:
            L.append("- 契约：%s" % ("全过（%s 条）" % (c.get("total") or "?") if c.get("ok")
                                     else "**没过** —— %s" % str(c.get("reason") or "")[:160]))
        else:
            L.append("- 契约：**没验**（上一轮没有判据结果，先查为什么）")
        tv = last.get("verdict") or ""
        if tv:
            L.append("- 测试者：%s" % tv)
        else:
            L.append("- 测试者：%s" % ("没跑" if not last.get("benni_ran") else "跑了但没给判断"))
        tm = last.get("tester") or {}
        if tm.get("modified_files"):
            L.append("- ⚠ 测试者在副本里改过已有文件：%s（它可能在替你改）"
                     % "、".join(tm["modified_files"][:4]))
        if last.get("scope_violation"):
            L.append("- ⚠ 越界：改了范围外的 %s" % "、".join(last["scope_violation"][:4]))
        L += ["", "## 这一轮该谁：%s" % {"executor": "你（执行者）",
                                        "human": "人", "tester": "测试者"}[role], "",
              why]
        if role == "executor":
            L += ["", "★ 只改判据没过的地方。**别顺手重构、别顺手加功能** —— "
                      "那些不在这一轮的范围里，改了就是越界。"]
        if role == "human":
            L += ["", "★ 这一轮**不需要你改东西**。如果确实没什么可做，就直说"
                      "「已经做完了，等裁定」，不要自己给自己找活。"]
    # ★ 人补充的细节（2026-11-03）：L4 不只是无人值守 —— 人主动开着看时，
    #   中途补的要求必须进任务书，否则它只存在于对话里，下一轮就丢了。
    _notes = _human_notes(d)
    if _notes:
        L += ["", "## 人补充的细节（优先级高于原计划）"]
        for _t in _notes[-6:]:
            L.append("- %s" % _t)
        L.append("★ 如果和原计划冲突，明说冲突在哪、你打算怎么收，别悄悄改。")
    return memo, "\n".join(L)


# ─────────────────── 人的裁定（过 / 没过）— 已落地 2026-10-29 ───────────────────
#
# 为什么要有这一步
#     这套东西的最后一句话是"判定交给人"。但在此之前，人的判定**只活在当时的对话里** ——
#     job 目录里没有任何地方写着"我看过了，过了"或者"我没过，因为 X"。
#     于是：① 一个 job 永远停在"等人裁定"，过两天翻 status 看不出结局；
#           ② **下一个 job 学不到任何东西** —— 而设计稿里写明规划者该看历史失败。
#
# 裁定的正确形态是「结论 + 一句话理由」，不是一段过程
#     人在场时的裁定天然混着过程（"我看了你的报告，第 3 点说得对，但第 2 点我不同意，
#     另外这个方案还能再优化"）。那种东西给执行者，它就会开始规划。
#     所以这里强制拆成两个字段：`verdict`（过/没过）+ `reason`（**一句话**，为什么）。
#     **没有理由的裁定拒绝写入** —— 因为下一次读到的理由就是唯一的外部锚点；
#     空的理由会让下一个 job 以为"上次没问题"，而实际是"上次没说清"。
#
# ★ 这是**人的动作**，agent 不许调。所以它不在 run/loop 的任何路径上。

def _final_stats(d: Path) -> dict:
    """这次委托最后的成绩：第几轮、契约过了几条。拿不到就说拿不到。"""
    rows = [_round_facts(p) for p in _rounds(d)]
    if not rows:
        return {"rounds": 0, "passed": None, "total": None}
    pn = _passed_n(rows[-1])
    return {"rounds": len(rows),
            "passed": (pn[0] if pn else None),
            "total": (pn[1] if pn else None)}


def _index_append(rec: dict) -> None:
    """往索引里追加一行。索引是**缓存不是真源** —— 丢了能用 `job.py index` 重建。"""
    try:
        JOBS.mkdir(parents=True, exist_ok=True)
        with open(_index_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        # ★ 索引写不进去不能把裁定也吞掉 —— 裁定已经落在 job 目录里了，
        #   那才是真源。这里只提示"跨 job 那一层这次没接上"。
        print("（索引没写成：%s —— 裁定已经落盘，但下一个 job 读不到这一条）" % e,
              file=sys.stderr, flush=True)


def _index_read(limit: int = 50) -> list:
    """读索引里最近的 N 条。文件不存在就是空 —— 不报错，也不假装有。"""
    p = _index_path()
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return out[-limit:]


def cmd_judge(a) -> int:
    """人写裁定。`verdict` 只有两个值：过 / 没过。"""
    d = _pick(a.job)
    verdict = "过" if a.cmd == "accept" else "没过"
    reason = (a.reason or "").strip()
    if not reason:
        print("✗ 裁定要带一句理由（--reason）。", file=sys.stderr, flush=True)
        print("  这不是流程繁琐：**下一件事读到的就只有这一句** ——", file=sys.stderr, flush=True)
        print("  空的理由会让它以为「上次没问题」，而实际是「上次没说清」。",
              file=sys.stderr, flush=True)
        return 2

    st = _final_stats(d)
    job = _load(d)
    s = _write_summary(d)

    # ★ 不许把上一份裁定**悄悄盖掉**（2026-10-29 实测：原来直接覆盖，
    #   第一次的理由就没了；更糟的是索引里留下两条自相矛盾的记录，
    #   以后问「上次为什么没过」，会读到两个答案而无法分辨哪个是最后的）。
    #   改判走 reopen（它留痕），这里只许**在没裁定过的时候**写第一份。
    _old = d / "verdict.json"
    if _old.exists() and not getattr(a, "force", False):
        try:
            _ov = json.loads(_old.read_text(encoding="utf-8"))
        except Exception:
            _ov = {}
        _same = (_ov.get("verdict") == verdict)
        print("✗ 这个 job 已经有一份裁定了：%s（%s）"
              % (_ov.get("verdict") or "?", _ov.get("at") or "?"),
              file=sys.stderr, flush=True)
        print("  理由：%s" % str(_ov.get("reason") or "")[:120], file=sys.stderr, flush=True)
        print("  %s" % ("跟你要写的一样 —— 不用再写一遍。"
                        if _same else
                        "跟你要写的**不一样** —— 改判要留痕，别覆盖："), file=sys.stderr, flush=True)
        if not _same:
            print("     python bin/job.py reopen --job %s --reason \"为什么改判\"" % d,
                  file=sys.stderr, flush=True)
        return 2

    rec = {
        "by": "人",
        "at": _now(),
        "verdict": verdict,
        "reason": reason[:400],
        "rounds": st["rounds"],
        "passed": st["passed"],
        "total": st["total"],
        # ★ 停下来的理由也要留（2026-10-29）：它是"机器当时为什么觉得该停"，
        #   跟人的裁定放一起，事后能看出两边判断差在哪。
        "stop": (s.get("stop") or "")[:300],
        "task": _task_line(d),
        "crew": job.get("crew"),
    }
    (d / "verdict.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n",
                                    encoding="utf-8")
    _index_append({"id": d.name, "at": rec["at"], "crew": rec["crew"],
                   "task": rec["task"], "rounds": rec["rounds"],
                   "passed": rec["passed"], "total": rec["total"],
                   "verdict": verdict, "reason": rec["reason"]})

    print("%s 已经记下：%s（%d 轮，契约 %s/%s）"
          % (d.name, verdict, st["rounds"],
             st["passed"] if st["passed"] is not None else "?",
             st["total"] if st["total"] is not None else "?"))
    print("  理由：%s" % reason[:120])
    print("  写在 %s" % (d / "verdict.json"))
    # ★ 只有"机器叫停"和"人判过"才叫分歧（2026-10-29 实测撞到）：
    #   机器说的如果是"测试者说看起来过，可以停下来让人裁定了"，那正是它期望的结局 ——
    #   把这种也说成"分歧"，会让人开始怀疑每一条警告，而警告一旦不可信就等于没有。
    _stop = s.get("stop") or ""
    if _stop and verdict == "过" and "看起来过" not in _stop:
        print("  ⚠ 机器当时说「%s」，而你判过了 —— 这个分歧留在记录里，"
              "以后想查可以对着看" % _stop[:80])
    return 0


def _task_line(d: Path, n: int = 40) -> str:
    """任务书的第一句非空话（给索引当标题用）。"""
    try:
        for line in (d / "task.md").read_text(encoding="utf-8", errors="replace").splitlines():
            t = line.strip().lstrip("#").strip()
            if t:
                return t[:n]
    except OSError:
        pass
    return d.name


def cmd_index(a) -> int:
    """从各 job 目录**重建**索引。索引是缓存，丢了不该丢东西。"""
    JOBS.mkdir(parents=True, exist_ok=True)
    recs = []
    for p in sorted([x for x in JOBS.iterdir()
                     if x.is_dir() and (x / "job.json").exists()],
                    key=lambda x: x.name):
        v = _read_json(p / "verdict.json")
        if not v:
            # 没裁定的 job 也进索引（带 verdict=""）—— 否则"哪些还等着人看"
            # 这件事就得去翻每个目录才知道了。
            job = _load(p)
            recs.append({"id": p.name, "at": _now(), "crew": job.get("crew"),
                         "task": _task_line(p),
                         "rounds": len(_rounds(p)),
                         "passed": None, "total": None,
                         "verdict": "", "reason": ""})
            continue
        recs.append({"id": p.name, "at": v.get("at") or "", "crew": v.get("crew") or "",
                     "task": v.get("task") or _task_line(p),
                     "rounds": v.get("rounds"), "passed": v.get("passed"),
                     "total": v.get("total"),
                     "verdict": v.get("verdict") or "", "reason": v.get("reason") or ""})
    _index_path().write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + ("\n" if recs else ""),
        encoding="utf-8")
    n_ok = sum(1 for r in recs if r.get("verdict") == "过")
    n_no = sum(1 for r in recs if r.get("verdict") == "没过")
    print("重建了索引：%d 个 job（过 %d / 没过 %d / 还没裁定 %d）"
          % (len(recs), n_ok, n_no, len(recs) - n_ok - n_no))
    if a.lessons:
        print("")
        _print_lessons(_index_read(20), limit=a.lessons)
    return 0


def cmd_reopen(a) -> int:
    """把已有的裁定收进历史，让这个 job 重新可以跑。**必须给理由。"""
    d = _pick(a.job)
    why = (a.reason or "").strip()
    if not why:
        print("✗ reopen 要带一句理由（--reason）。", file=sys.stderr, flush=True)
        print("  这一句是给下一个看这个 job 的人（以及下一个 job）看的："
              "「当时判过了，后来为什么又接着改」。", file=sys.stderr, flush=True)
        return 2

    vf = d / "verdict.json"
    if not vf.exists():
        print("这个 job 还没有裁定 —— 不用 reopen，直接跑就行。", flush=True)
        return 0
    try:
        old = json.loads(vf.read_text(encoding="utf-8"))
    except Exception as e:
        return _die("verdict.json 读不出来（%s）—— 先看一眼它，别急着改名" % e)

    # ★ 先落历史、再删当前（2026-10-29）：顺序反了就会出现
    #   "裁定没了、历史里也没有" —— 那份裁定是人的判断，丢了再也补不回。
    with open(d / "verdict.history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({**old, "reopened_at": _now(), "reopened_why": why[:400]},
                           ensure_ascii=False) + "\n")
    vf.unlink()

    # 索引里那条也要标掉，否则以后读"以前怎么判的"还会把它当成最终结论。
    # 索引是缓存（`job.py index` 能重建），所以这里只追加一条 reopen 记录。
    _index_append({"id": d.name, "at": _now(), "crew": _load(d).get("crew"),
                   "task": _task_line(d), "rounds": old.get("rounds"),
                   "passed": old.get("passed"), "total": old.get("total"),
                   "verdict": "已反悔",
                   "reason": "原来判「%s」：%s ／ 反悔理由：%s"
                             % (old.get("verdict") or "?",
                                str(old.get("reason") or "")[:120], why[:160])})

    print("已经放开：这个 job 可以接着跑了。", flush=True)
    print("  原来那份裁定（%s）收进了 verdict.history.jsonl —— 它没丢，只是不再是结论。"
          % (old.get("verdict") or "?"), flush=True)
    print("  反悔理由：%s" % why[:120], flush=True)
    print("  下一步：python bin/job.py run --job %s" % d, flush=True)
    return 0


def _print_lessons(recs: list, limit: int = 5) -> None:
    """打印"以前人是怎么判的"。**没过排在前面** —— 那才是下一个 job 该看的。"""
    done = [r for r in recs if r.get("verdict")]
    if not done:
        print("（还没有任何裁定 —— 也就没有可学的。这不是「没教训」，是「没记录」）")
        return
    done.sort(key=lambda r: (r.get("verdict") != "没过", r.get("at") or ""))
    for r in done[-limit:] if limit else done:
        print("  %s ｜ %s ｜ 轮 %s ｜ 契约 %s/%s"
              % (r.get("verdict"), str(r.get("task") or "")[:30],
                 r.get("rounds") or "?",
                 r.get("passed") if r.get("passed") is not None else "?",
                 r.get("total") if r.get("total") is not None else "?"))
        if r.get("reason"):
            print("      人说的理由：%s" % str(r["reason"])[:140])


# ─────────────────────────── new ───────────────────────────

# 从 CLI 接收任务材料和预算，在独立目录登记任务及执行布局；实际运行由后续命令启动。
def cmd_new(a) -> int:
    from loop.execution_policy import ROLE_LAYOUT_VERSION

    task_p = Path(a.task)
    if not task_p.is_absolute():
        task_p = Path.cwd() / task_p
    if not task_p.is_file():
        return _die("任务书不存在：%s" % task_p)
    body = task_p.read_text(encoding="utf-8", errors="replace").strip()
    if not body:
        return _die("任务书是空的：%s" % task_p)

    # ★★ 参数防呆（2026-10-29 实测：这里原来是**零校验**，全部 rc=0 建出来）。
    #   为什么值得较真：这些参数是**人锁的**、agent 改不了，所以它们是这道门的
    #   全部保障。写错了不会当场报，只会让后面几轮白跑 —— 而白跑是最贵的错，
    #   因为它看起来一切正常。
    #   原则：能当场判死的当场判死；判不死的至少说出来（别静默）。
    _mx = int(a.rounds)
    if _mx < 1:
        return _die("轮数上限是 %d —— 那这个 job 建出来就停了，一轮都跑不了。\n"
                    "  要「不限」就写一个大数（比如 200），不要写 0：\n"
                    "  0 和「无限」在代码里是两个意思，靠猜迟早猜错。" % _mx)
    if int(a.flat) < 0:
        return _die("「连续几轮没进展就停」是负数（%s）—— 只能 >= 0，0 表示不按这条停。"
                    % a.flat)

    # 工作区：不存在就当场死。★ 实测过「建得出来、run --dry 也 rc=0」——
    #   它会高高兴兴往一个不存在的目录里跑，报错要到很后面才出现。
    _cwd = (a.cwd or "").strip()
    if _cwd:
        _cp = Path(_cwd)
        if not _cp.is_absolute():
            _cp = Path.cwd() / _cp
        if not _cp.is_dir():
            return _die("工作区不存在或不是目录：%s" % _cp)
        # ★ 存**绝对路径**（2026-10-29）：原来原样存 `../demo-price`。
        #   于是同一个 job，今天在 A 目录跑 run、明天在 B 目录跑，它指的是两个地方 ——
        #   而 job.json 上看着一模一样。这是那种「不报错、结果不同」的坑。
        _cwd = str(_cp.resolve())

    JOBS.mkdir(parents=True, exist_ok=True)
    base = "job-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    d = JOBS / base
    n = 2
    while True:
        try:
            d.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            d = JOBS / ("%s-%d" % (base, n))
            n += 1
            if n > 50:
                return _die("建不出目录（%s 撞了 50 次）" % base)

    (d / "task.md").write_text(body + "\n", encoding="utf-8")

    # ★ 范围不替你定。空 = 整个工作区（现状语义），并**明确写出来**，
    #   否则"范围"页面上看着有、实际没生效，比没有更坏。
    scope = [s.strip() for s in (a.scope or "").split(",") if s.strip()]
    job = {
        "id": d.name,
        "created": _now(),
        "task": "task.md",
        "out": a.out or DEFAULTS["out"],
        "kind": a.kind,
        "crew": a.crew,
        "executionRolesVersion": ROLE_LAYOUT_VERSION,
        "max_rounds": int(a.rounds),
        "flat_rounds": int(a.flat),
        "policy": a.policy,
        "cwd": _cwd,
        "ctx": int(a.ctx or 0),
        "max_turns": int(a.max_turns or 0),
        "scope": scope,
        "note": ("scope 为空 = 允许改整个工作区（现状语义）。"
                 "要限制就填目录或文件，逗号分隔。"),
    }
    (d / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    _write_summary(d)
    if getattr(a, "json", False):
        print(json.dumps({"id": d.name, "dir": str(d),
                          "summary": str(d / "summary.json")}, ensure_ascii=False))
        return 0
    print("建好：%s" % d)
    print("  任务书：task.md（%d 字）" % len(body))
    print("  类型：%s ｜ 档位：%s ｜ 轮数上限：%d ｜ 连续没进展就停：%d 轮"
          % ("提问" if a.kind == "question" else "派活",
             job["crew"], job["max_rounds"], job["flat_rounds"]))
    if a.kind == "question" and int(a.rounds) > 1:
        print("  ⚠ 提问 + 多轮：这个组合没有意义（一个问题没有「更接近」这回事，"
              "跑 N 轮只是把同一个问题答 N 遍）。")
        print("    现在就会被拦住；要能跑，得先有「把问题拆成子问题」那一步（还没做）。")
    if scope:
        print("  范围：%s" % "、".join(scope))
        # ★ 范围写错是最难发现的一种（2026-10-29）：写错了不会报错，
        #   只会在每一轮里把执行者的**每个写操作**都拦下来 —— 而拦的理由
        #   听起来像「它越界了」，于是人会去查 agent，而错在参数。
        #   判不死，所以只提示：按仓库根写的时候，工作区里本来就没有这些路径。
        _miss = [s for s in scope
                 if not ((Path(_cwd) / s).exists() if _cwd else Path(s).exists())]
        if _miss and _cwd and len(_miss) == len(scope):
            print("  ⚠ 这些范围在工作区里一个都找不到：%s" % "、".join(_miss))
            print("    如果你是按**仓库根**写的（工作区只是仓库的一部分），这是正常的。")
            print("    如果不是，那这一轮的每个写都会被拦 —— 而理由看起来像「它越界了」。")
    else:
        print("  范围：**没限制**（整个工作区）—— 要收窄就改 job.json 的 scope")
    # ★ 无人值守 + 大轮数（2026-10-29）：不是拦，是让人在开跑前看见那个数。
    #   第四档没有人在旁边喊停，轮数就是唯一的刹车片 —— 它写在 job.json 里，
    #   而人通常只在 new 的时候看一眼。
    if job["crew"] == "l4" and job["max_rounds"] > 30:
        print("  ⚠ 第四档 + %d 轮：它会自己一直跑，中间没有人说「停，这样不对」。"
              % job["max_rounds"])
        print("    轮数就是这一注的注额。后悔成本 = 轮数 × 每轮的钱和时间。")
        print("    拿不准就先给 20，跑完看一轮再改 job.json（改 max_rounds 即可）。")
    print("下一步：python bin/job.py run --job %s" % d)
    return 0


# ───────────────────────── contract（总契约）─────────────────────────

def _contract_dir(d: Path) -> Path:
    p = d / "contract"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cmd_contract(a) -> int:
    """job 级**总契约**：直接转给契约工具，目录固定在 `<job>/contract/`。

    ★ 为什么不另写一套（2026-10-28）：哈希锁、amend 留痕、逐条跑判据都在
      `bin/contract.py` 里。重写一遍就是造第二份真源 —— 这个仓库刚因为
      "名单和注册表分处两地"栽过一次，代价是验收红三条。
    ★ 总契约和每轮纪要的分工（设计稿 §4）：
        总契约 = 什么算完成，人锁，**全程不可动**
        纪要   = 这轮改什么，规划者写，每轮可改
      所以这里只放总契约；纪要在每轮的 rN 里。
    """
    d = _pick(a.job)
    cd = _contract_dir(d)
    args = list(a.rest or [])
    if not args:
        done = cd / "done.json"
        if not done.exists():
            # ★ 按档位说不同的话（2026-10-28）：契约只在第四档需要。
            #   对 L2 说"起草一份"等于把它推去做一件多余的事，
            #   而且做完他会以为自己得到了保证 —— 那份保证没人验过。
            _crew = _load(d).get("crew") or "l2"
            if _crew == "l4":
                print("第四档**要**一份锁死的总契约，现在还没有。两步：")
                print("  ① python bin/plan.py --job %s            # 规划者起草（不锁）" % d)
                print("  ② python bin/job.py contract --job %s lock  # 人看一遍，再锁" % d)
                print("  ★ 出题的人不能同时宣布题目没问题 —— 所以第 ② 步必须是人。")
            else:
                print("这个 job 还没有总契约 —— 档位 %s，**不需要**。" % _crew)
                print("  人在场：规划者每轮给一句话方向，做对没有由你当场判断。")
                print("  契约是「人不在场」时才需要的东西（第四档）。")
                # ★ 这里原来还说"确实想要也可以起草：python bin/plan.py --job …"（2026-10-29 去掉）。
                #   前面说"不需要"、后面给一条命令，人读到的是"它让我去起草" ——
                #   一条可选命令放在"这件事不用做"的句子后面，就不再是选项，是邀请。
            return 0
        obj = json.loads(done.read_text(encoding="utf-8"))
        print("总契约：%s" % done)
        print("  done 判据 %d 条 ｜ 锁定：%s"
              % (len(obj.get("done") or []),
                 "是" if (cd / "done.lock").exists() else "**没有**（不能开工）"))
        for it in (obj.get("done") or [])[:10]:
            print("   · %s" % str(it.get("claim") or "")[:60])
        return 0
    if "--dir" not in args:
        args += ["--dir", str(cd)]
    r = subprocess.run([sys.executable, str(APP / "bin" / "contract.py")] + args,
                       cwd=str(APP))
    _write_summary(d)
    return r.returncode


# ─────────────────────────── run ───────────────────────────

def _l4_no_contract(d: Path) -> None:
    """第四档没有锁死的契约时，把话说完。**只打印，不决定退出码。**

    ★ 抽出来是为了两处都用（2026-10-28）：`cmd_run` 自己要拦，而它在 l4 档
      会**先转发给 cmd_loop** —— 那份拦截就发生在转发之后，于是 loop 里报的是
      "第 1 轮 rc=2"，可实际一轮都没跑。**"没跑起来"和"跑坏了"是两件事。**
    """
    # ★ 拦在开跑之前：第四档 + 没有锁死的契约 = 无人值守 + 没有固定判据 +
    #   只靠轮数刹车。这是最坏的组合，而它跑起来会很像正常 ——
    #   每轮都有产出、都有测试者判断，只有"到底算不算成"没人说得清。
    # ★ 有 done.json 但没锁也拦：哈希锁对不上时 `contract.py run` 会直接拒跑
    #   （rc=2），于是每一轮的判据都跑不了、每轮都判"该人看"。
    #   那不是"半个保障"，是零个 —— 只是看起来有一份契约摆在那儿。
    print("✗ 第四档要一份**锁死的**总契约 —— 拦在开跑之前。",
          file=sys.stderr, flush=True)
    if (d / "contract" / "done.json").exists():
        print("  现在有 done.json 但**没锁**：哈希锁对不上，判据根本跑不起来"
              "（contract.py 会直接拒跑）。", file=sys.stderr, flush=True)
    print("  它要自己跑很多轮，中间没有人说「停，这样不对」。"
          "没有能跑的判据，那些轮次就是在没有靶子的情况下烧钱。", file=sys.stderr, flush=True)
    print("  两种改法：", file=sys.stderr, flush=True)
    print("    ① 出契约 + 锁：python bin/plan.py --job %s" % d, file=sys.stderr, flush=True)
    print("       人看一遍 → python bin/job.py contract --job %s lock" % d,
          file=sys.stderr, flush=True)
    print("    ② 降到 l2：人在场时用不着契约（改 job.json 的 crew）",
          file=sys.stderr, flush=True)


def _scope_for_cwd(scope: list, cwd: str) -> tuple[list, list]:
    """把「仓库相对」的范围，换成「这一轮工作区相对」的范围。

    ★ 为什么必须换（2026-10-29 实测撞到）：范围是人在 `--scope` 里写的，
      他脑子里想的是"仓库里的哪个目录"（`demo/price`）；而门禁把范围按
      **这一轮的工作区根**（`--cwd`）解析。两者在 cwd 不是仓库根时错位：
      `--cwd demo/price --scope demo/price` ⇒ 门禁去比对
      `<repo>/demo/price/demo/price`，永远不匹配，于是**每个写操作都被拒**，
      理由还写着"这一轮的范围是「demo/price」，而「price.py」不在里面"。
    ★ 两个方向都要换：范围在工作区里面 ⇒ 变成相对路径；范围包住工作区 ⇒ 变成 `.`。
      范围完全在工作区外面 ⇒ 这一轮用不上它，得说出来（否则人以为自己限住了）。

    返回（换好的范围, 用不上的那几条）。cwd 为空 / 不在仓库里时原样返回。
    """
    if not scope:
        return scope, []
    cwd = (cwd or "").strip()
    if not cwd:
        return scope, []
    try:
        cwd_abs = Path(cwd).resolve()
        if not cwd_abs.is_absolute():
            cwd_abs = (APP / cwd).resolve()
        rel_cwd = cwd_abs.relative_to(APP.resolve())
    except (OSError, ValueError):
        # 工作区在仓库外面：拿不到"仓库相对"的坐标系，原样给（门禁会按工作区解析）
        return scope, []
    if not rel_cwd.parts:
        return scope, []          # cwd 就是仓库根，不用换

    base = PurePosixPath(rel_cwd.as_posix())
    out, dead = [], []
    for s in scope:
        sp = PurePosixPath(str(s).strip().replace("\\", "/").strip("/"))
        if str(sp) in ("", "."):
            out.append(".")
            continue
        try:
            # 范围在工作区里面（或就是工作区）
            out.append(str(sp.relative_to(base)) or ".")
        except ValueError:
            if base.is_relative_to(sp):
                out.append(".")   # 范围包住工作区：整个工作区都在范围内
            else:
                dead.append(str(s))
    # 去重，保序
    seen, keep = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            keep.append(x)
    return keep, dead


# 先以 `lease.py` 取得任务独占执行身份，再处理本轮启动与回收。
def cmd_run(a) -> int:
    from loop.lease import Lease
    from loop import runtime as RT
    d = _pick(a.job)
    job = _load(d)
    if job.get("crew") == "l3":
        return _die("旧 L3 委托已退役，历史记录仍可查看", 4)
    lease = Lease(d, d.name)
    if not lease.acquire()[0]:
        print("这个委托已有运行，不能重复启动", file=sys.stderr)
        return 7
    try:
        RT.register(d, os.getpid(), phase="running", policy=job.get("policy", "workspace"))
        code = _cmd_run_owned(a, d, job)
        RT.update_owned(d, state="completed" if code == 0 else "failed", phase="finished")
        return code
    except BaseException:
        RT.update_owned(d, state="unknown", phase="finished")
        raise
    finally:
        lease.release()


# 在已持有任务租约时推进一轮。轮次归档、输出和结果关联到当前的持久编号。
def _cmd_run_owned(a, d, job) -> int:
    # ★ 旁路登记（`loop/claims.py`）靠环境认人，不靠线程局部 ——
    #   工具跑在线程池里，主线程 bind 的值传不过去。放在这里而不是 main()：
    #   只有真跑这条路径会写文件，别的子命令登记了也没意义。
    os.environ["HARNESS_JOB"] = d.name

    # ★★ 已经裁定过了，就不许再跑（2026-10-29 实测：原来**照跑**）。
    #   为什么这条比看起来重要：裁定是这一件事的**终点**。终点之后还能继续跑，
    #   那 verdict.json 记的就不再是"结论"，而是"某个时刻的快照" ——
    #   而索引里、下次起草契约时引用的，都是这一句。
    #   真跑下去的话，会出现「人判过了，可它后来又改了 5 轮，而记录上写着过」。
    #   不封死它，等于让「过」这件事会自己失效。
    #   要反悔是正常的（人在后面看到问题），所以给一条明路：reopen 带理由。
    _vf = d / "verdict.json"
    if _vf.exists():
        try:
            _v = json.loads(_vf.read_text(encoding="utf-8"))
        except Exception:
            _v = {}
        _head = ("这个 job 已经由人裁定过了：%s（理由：%s）"
                 % (_v.get("verdict") or "?", str(_v.get("reason") or "")[:100]))
        # `--dry` 是只读，不拦 —— 人要能先看清"reopen 之后这一轮会怎么跑"，
        # 再决定要不要反悔。拦在这里等于逼他先做决定再看依据。
        if getattr(a, "dry", False):
            print("⚠ %s" % _head, flush=True)
            print("  （--dry 只打印，所以不拦。真要跑它会拦住，先 reopen）", flush=True)
        else:
            print("✗ %s" % _head, file=sys.stderr, flush=True)
            print("  裁定是终点 —— 之后再跑，那份裁定就不再是结论，只是某一刻的快照。",
                  file=sys.stderr, flush=True)
            print("  要反悔（发现还有问题、想接着改）：", file=sys.stderr, flush=True)
            print("     python bin/job.py reopen --job %s --reason \"为什么接着跑\"" % d,
                  file=sys.stderr, flush=True)
            print("  没有 --force：绕过留痕的口子开着，就等于没有留痕。",
                  file=sys.stderr, flush=True)
            return 2

    # ★ 第四档（2026-10-28）：`run` 在 l4 档自动转成"跑到底"。
    #   这样是**档位决定行为**，人只需要记一条命令 —— 而不是"l4 要用另一个脚本"。
    #   单步调试的口子叫 `--once`；真要单步，更该把档位降到 l0/l1，
    #   因为 l4 的意义就在于"没人看着"。
    if ((job.get("crew") or "l2") == "l4"
            and not getattr(a, "_single", False)
            and not getattr(a, "once", False)):
        # ★ 先查契约再转（2026-10-28）：拦截在下面那段里，而转发生得更早 ——
        #   于是 loop 会报"第 1 轮 rc=2"，可实际一轮都没跑起来。
        #   "没跑起来"和"跑坏了"是两件事，不能混成一句。
        if not (d / "contract" / "done.lock").exists():
            _l4_no_contract(d)
            return 2
        return cmd_loop(a)

    # ★ job.json 是"人锁"的参数。跑之前算一份指纹，跑完再算一次 ——
    #   变了就说明有人在跑的过程中改了参数，那一轮的结论不能用。
    #   （现在只报出来，不阻断；等契约锁那一步再接成硬的红灯。）
    fp_before = _fingerprint(d)

    try:
        n = _next_n(d)
    except SystemExit as e:
        # ★ 不放过 SystemExit（2026-10-29 自检）：它的默认退出码是 1，
        #   而 1 在这个 CLI 里的意思是"跑坏了"。这里其实是"**用法不对**"——
        #   有一轮残着没跑完，得人先处理掉。前端按退出码分流时会分错。
        print("✗ %s" % e, file=sys.stderr)
        return 2
    mx = int(job.get("max_rounds") or 0)

    # ★★ 「提问 + 多轮」在启动时拦住（2026-10-28）。
    #   无人值守的整个前提是"有一个可判定的目标，一轮一轮逼近它"。
    #   而**一个问题没有「更接近」这回事** —— 答案要么站得住要么站不住，
    #   第 2 轮不会比第 1 轮更对。所以跑 N 轮不是"更努力"，
    #   是"同一个动作重复 N 次"。发现得越晚，浪费越多：跑满 200 轮才明白，
    #   那是几百块和几小时。
    #   ★ 这不是限制人，是这个组合本身没有意义 —— 所以给的是**替代方案**，不是"不许"。
    if (job.get("kind") or "task") == "question" and mx > 1:
        # 以后拆题那一步会写这份清单；它存在就说明"多轮"有推进的依据。
        if not (d / "contract" / "questions.json").exists():
            print("✗ 这是「提问 + %d 轮」，它没有意义 —— 拦在开跑之前。" % mx,
                  file=sys.stderr, flush=True)
            print("  一个问题没有「更接近」这回事：答案要么站得住要么站不住，"
                  "第 2 轮不会比第 1 轮更对。跑 %d 轮只是把同一个问题答 %d 遍。"
                  % (mx, mx), file=sys.stderr, flush=True)
            print("  两种改法：", file=sys.stderr, flush=True)
            print("    ① 轮数设 1：改 %s 的 max_rounds" % (d / "job.json"),
                  file=sys.stderr, flush=True)
            print("    ② 先拆题：让规划者把这个问题拆成子问题，一轮答一个"
                  "（这一步还没做，等纪要之后）", file=sys.stderr, flush=True)
            return 2
    if mx and n > mx:
        return _die("轮数用尽（已跑 %d 轮，上限 %d）。要再跑就改 job.json 的 max_rounds"
                    % (len(_rounds(d)), mx), 3)
    rnd = d / ("r%d" % n)

    task = d / (job.get("task") or "task.md")
    if not task.exists():
        return _die("任务书不见了：%s" % task)
    out = rnd / (job.get("out") or "out.md")

    # ★ 纪要（2026-10-28）：每轮开工前写一份"上一轮留下了什么 + 这一轮该谁"，
    #   拼进**这一轮的任务书**末尾。不新增参数 —— 执行者读任务书就够了。
    #   ★ 为什么拼进任务书而不是塞进提示词：任务书是落盘的文件，
    #     事后能查"它当时看到的到底是什么"，而提示词里的东西跑完就没了。
    memo, memo_text = _memo(d, n)
    round_task = rnd / "task.md"

    cmd = [sys.executable, str(RUN_TASK),
           "--task", str(round_task),
           "--out", str(out),
           "--session", str(rnd),
           "--round", str(n),
           "--job", str(d),
           # ★ 轨迹必须显式给（2026-10-28 实测踩到）：不给的话 run-task 一条都不写，
           #   而测试者读到"没有轨迹"就会**正确地拒绝跑** ——
           #   于是 l1 档名义上有测试者、实际一个都没跑，汇总里只显示"没给判断"。
           #   这不是静默失败，但它长得太像"这一轮没什么好说的"。
           "--trace", str(rnd / "trace.jsonl")]
    # 档位 → 开关。l0 不跑收尾复核（那是测试者的活，省掉它就是省一整段）。
    if (job.get("crew") or "l2") in {"l0", "l1"}:
        cmd.append("--no-benni")
    if job.get("policy"):
        cmd += ["--policy", str(job["policy"])]
    if job.get("cwd"):
        cmd += ["--cwd", str(job["cwd"])]
    if job.get("ctx"):
        cmd += ["--ctx", str(int(job["ctx"]))]
    if job.get("max_turns"):
        cmd += ["--max-turns", str(int(job["max_turns"]))]
    # ★ 范围一路传下去（2026-10-28）：门禁读它拦"写错地方"，收尾再拿
    #   改动清单核一遍拦"命令里写别的地方"。两道都要，只做前者会漏掉一半。
    if job.get("scope"):
        # ★ 范围是"仓库相对"写的，门禁按工作区解析 —— 中间要换一次（2026-10-29）。
        _sc, _dead = _scope_for_cwd(job["scope"], job.get("cwd") or "")
        if _dead:
            print("⚠ 这几条范围不在这一轮的工作区里，用不上：%s"
                  % "、".join(_dead), flush=True)
        if _sc:
            cmd += ["--scope", ",".join(str(x) for x in _sc)]
    # ★ 契约只在**第四档**用（2026-10-28 改口径，用户定的）：
    #   **契约是「打断」的替代品。** 人在场时你能在第三轮说"不对，先写测试"，
    #   那就是即时纠偏，不需要事先写死"什么算完成"。人不在场时这句话没人说，
    #   才只能提前写下来。所以 L0/L1/L2 一律不给它起草契约 ——
    #   原来不传 --contract 时 run-task 会**每轮自己起草一份**，
    #   于是"每轮换一套标准"：3/5 和 3/5 可能根本不是同一组题，
    #   拿它们比"有没有进展"是在比两份不同的考卷。
    _cd = d / "contract"
    _crew = job.get("crew") or "l2"
    if _crew == "l4" and not (_cd / "done.lock").exists():
        _l4_no_contract(d)
        return 2
    if (_cd / "done.json").exists():
        cmd += ["--contract", str(_cd)]
    else:
        # 人在场的档位：**别让它自己起草**。它起草的那份不锁、只在那一轮用，
        # 人看着也认不出"这是临时定的还是我批准的"。
        cmd.append("--no-contract")

    # ★ flush：不刷的话，我们的这句话会卡在缓冲区里，
    #   而子进程的输出直接进终端 —— 于是"第 N 轮"会出现在日志末尾，看着像跑了两次。
    print("第 %d 轮 → %s" % (n, rnd), flush=True)
    if a.dry:
        print("（--dry：只打印命令，不跑）")
        if memo.get("before"):
            # ★ 三态，不是两态（2026-10-28）：`{}`（这一轮没验）和 `"ok": false`
            #   （验了但没过）是两件完全不同的事。写成"没过"会让人以为跑了判据，
            #   而实际连判据都没跑 —— 那正是这一整轮在防的"看起来验过了"。
            _c = memo["before"].get("contract") or {}
            print("  纪要：上一轮 → 契约 %s ｜ 测试者 %s ｜ 这一轮该 %s"
                  % ("没验" if not _c else ("全过" if _c.get("ok") else "没过"),
                     memo["before"].get("verdict") or "没给判断",
                     memo.get("next_role")))
        else:
            print("  纪要：第 1 轮，没有上一轮")
        # ★ dry 只打印、不调模型（问规划者是一次真调用）。但得说清"真跑时会去问" ——
        #   否则人看完 dry 会以为这一轮就只有这些事。
        #   ★ 按档位说不同的话（2026-10-28）：L2 问的是"干什么"，L4 问的是"先做哪几条"。
        #     混着说会让人以为 L2 也要契约。
        _crew = job.get("crew") or "l2"
        if _crew == "l1":
            print("  这一轮干什么：会问规划者一句（人在场，不给契约）")
        elif _crew == "l4":
            _op = _open_ids(d, memo.get("before") or {})
            if not _contract_total(d):
                print("  先做哪几条：**还没有总契约** —— 第四档靠它自转，"
                      "没有判据就只能靠轮数刹车（先 contract new + lock）")
            else:
                print("  先做哪几条：%s（共 %d 条还没过）"
                      % ("会问规划者从 %d 条里挑" % len(_op)
                         if len(_op) > 1 else "不问规划者", len(_op)))
        else:
            print("  这一轮干什么：不问规划者（%s 档没有这一位）" % _crew)
        print("  " + " ".join('"%s"' % c if " " in c else c for c in cmd))
        return 0

    # ★ 目录在这里才建（2026-10-28 实测踩到）：原来建在 dry 判断之前，
    #   于是 `run --dry` 会留下一个空的 r1 —— 真正跑的时候从 r2 开始，
    #   第 1 轮凭空消失。**一个只读操作留下副作用，是最难查的那种**。

    # ★ "这一轮先做哪几条"（2026-10-28）：l2 档会去问规划者，所以要花几秒到几十秒。
    #   放在 mkdir **之前**：问不出结果时不留半截目录。
    #   dry 不走这里 —— 问模型不是只读，`--dry` 说了只打印。
    # ★ 按档位分派（2026-10-28 口径改了）：L2 人在场，规划者只说一句话方向；
    #   L4 人不在场，才用锁死的契约挑"先做哪几条判据"。
    # ★★ 续跑前重新校验授权（2026-11-01）：锁契约时签发的 mandate。
    #   契约变了 / 范围扩大了 ⇒ 不许拿旧授权接着跑。老委托没有凭证的，
    #   按当前契约补一张 inferred，并明说"这是补记的，不是人当时批的"。
    #   放在 dry 之后：--dry 只打印，不该顺手补写凭证。
    if (job.get("crew") or "l2") == "l4":
        from loop import mandate as MD
        mc = MD.check(d)
        if mc.get("missing"):
            ir = MD.issue(d, strength="inferred", by="legacy")
            if ir.get("ok"):
                print("  授权凭证：老委托没有，已按当前契约补记"
                      "（inferred，不是人当时批的）", flush=True)
                mc = MD.check(d)
        if not mc.get("ok"):
            print("✗ 授权已过期：%s" % "；".join(mc.get("reasons") or []),
                  file=sys.stderr, flush=True)
            print("  要接着跑，先重新锁契约（会重新签发授权）：",
                  file=sys.stderr, flush=True)
            print("    python bin/contract.py lock --dir %s" % (d / "contract"),
                  file=sys.stderr, flush=True)
            return 2
        for _nt in mc.get("notes") or []:
            print("  授权提示：%s" % _nt, flush=True)

    if (job.get("crew") or "l2") == "l1":
        think = _plan(d, n, job, memo)
        memo["plan"] = think
        _ttxt = _render_plan(think)
    else:
        think = _focus(d, n, job, memo)
        memo["focus"] = think
        _ttxt = _render_focus(think, d)
    if _ttxt:
        memo_text += "\n" + _ttxt

    # ★ 两个人同时点了「跑下一轮」时，第二个会撞上已经建好的目录（2026-10-29 自检）。
    #   原来这里没人接 FileExistsError，于是它直接 traceback ——
    #   而人看到的是一大段 Python 报错，看不出"是你点了两次"。
    #   ★ 也不能改成 exist_ok=True：那会让两个进程同时往同一轮里写，
    #     产出一份谁也说不清是谁写的记录。
    try:
        rnd.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _die("这一轮（%s）刚被建出来 —— 是不是同时跑了两次？\n"
                    "  等前一个跑完再跑：`job.py status` 看它到底在跑还是已经停了。"
                    % rnd.name, 2)
    (rnd / "memo.json").write_text(json.dumps(memo, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    round_task.write_text(task.read_text(encoding="utf-8", errors="replace").rstrip()
                          + "\n" + memo_text + "\n", encoding="utf-8")
    if memo.get("next_role") == "human":
        print("  纪要说：这一轮该**人**看，不该它自己找活 —— 已经写进任务书。", flush=True)
    if think.get("plan"):
        print("  这一轮：%s" % think["plan"][:70], flush=True)
    elif think.get("ids"):
        print("  先做第 %s 条（%s）"
              % ("、".join(str(x) for x in think["ids"]),
                 "规划者挑的" if think.get("by") == "planner" else "程序兜底"), flush=True)
    rc = subprocess.run(cmd, cwd=str(APP)).returncode
    s = _write_summary(d)
    fp_after = _fingerprint(d)
    if fp_before != fp_after:
        print("⚠ 跑这一轮期间 job.json 被改过 —— 这一轮的参数可能不是最初那份")

    print("\n第 %d 轮结束（rc=%s）" % (n, rc))
    _last = s["rounds"][-1]
    # ★ 别再写"判据校验：过"（2026-10-28 自查）：这里拿到的是**任务状态**，
    #   不是契约判据跑出来的结果。混着说会让人以为判据验过了 ——
    #   而判据通过率要等双层契约那一步才算得出来。
    print("  任务：%s ｜ 测试者：%s"
          % (_last.get("status") or "?", _last.get("verdict") or "（这一档不跑，或没给判断）"))
    if rc == 5:
        print("  这是**越界**（不是失败）：活可能干对了，但它改了范围外的东西 —— 先看多改了什么")
    if s.get("stop"):
        print("  ⏸ %s" % s["stop"])
    elif s.get("notice"):
        print("  ⚠ %s" % s["notice"])
        print("  还能再跑（%d/%d 轮）—— 但先看上面那条" % (s["n_rounds"], mx or 0))
    else:
        print("  还能再跑（%d/%d 轮）" % (s["n_rounds"], mx or 0))
    return rc


# ─────────────────────────── loop（第四档：跑到底）───────────────────────────
#
# 第四档不是"更聪明的档"，是**同一套东西去掉人手**（2026-10-28）。
#     前三档每轮都要人来敲一次 `run`；第四档让它自己接着敲。
#     所以它没有任何新的判断力 —— 停不停还是那五条（越界/轮数/测试者说过/
#     通过数没推进/没验出来），只是从"人看着停"变成"程序看着停"。
#
# ★ 这里刻意**不做**的三件事（做了就等于偷偷加了人手之外的判断）：
#     ① 不重试失败的一轮："模型调不通就再来一次"会让 200 轮变成 400 次调用，
#        而且失败原因通常是额度/凭据 —— 重试解决不了，只烧钱。
#     ② 不问模型"要不要继续"：那等于把停止权交给一个天然倾向于"再试试"的东西。
#     ③ 不在轮与轮之间 sleep：等谁？没有人需要等。等只会让"卡住"这件事更晚被发现。
#
# ★ 每轮之间的全部交接都在**纪要**里（rN/memo.json + 拼进 rN/task.md）：
#   上一轮变了哪些文件、契约过几条、测试者说什么、这轮先做哪几条。
#   循环本身不传任何额外状态 —— 否则"它为什么这么干"就查不到了。

# 在同一任务中续接多个有界轮次，依据实际状态和预算决定何时停止。
def cmd_loop(a) -> int:
    d = _pick(a.job)
    job = _load(d)
    crew = job.get("crew") or "l2"
    if crew == "l3":
        return _die("旧 L3 委托已退役，历史记录仍可查看", 4)
    mx = int(job.get("max_rounds") or 0)
    print("第四档：一轮跑完自己接着跑，直到停止判据说停（上限 %s 轮）"
          % (mx or "不限"), flush=True)
    if crew != "l4":
        print("  ⚠ 档位是 %s，不是 l4 —— 照样会循环，但那份「三问契约」只在 l4 启用"
              % crew, flush=True)
    # ★ --dry 是只读：dry 却循环 200 次打印 200 遍，没有任何意义，
    #   而且真有人会以为它"跑过了"。只打第一轮就退。
    if getattr(a, "dry", False):
        one = copy.copy(a)
        one._single = True
        return cmd_run(one)

    ran = 0
    rc = 0
    why_stopped = ""
    try:
        while True:
            s = _write_summary(d)
            if s.get("stop"):
                print("\n⏸ 停下：%s" % s["stop"])
                why_stopped = "judge"
                break
            if s.get("notice"):
                print("\n⚠ %s" % s["notice"])
            # ★ 单轮调用 marked 一下：`run` 在 l4 档会转进本函数，
            #   不标记就会自己调自己，一路递归到爆栈。
            one = copy.copy(a)
            one._single = True
            _before = len(_rounds(d))
            rc = cmd_run(one)
            # ★ 按"目录里真的多了一轮"算（2026-10-28）：rc≠0 不等于跑过一轮 ——
            #   被拦下（rc=2）或者轮数用尽（rc=3）时也是非零，可一轮都没跑。
            #   报"第 1 轮 rc=2"会让人以为它试过并失败了。
            ran = len(_rounds(d)) - _before
            if ran <= 0:
                print("\n⏸ 停下：这一轮**没跑起来**（rc=%s）—— 上面说了原因" % rc)
                why_stopped = "notrun"
                break
            if rc not in (0,):
                print("\n⏸ 停下：第 %d 轮 rc=%s（不是「没进展」，是这一轮没跑完）"
                      % (len(_rounds(d)), rc))
                if rc == 5:
                    print("  这是越界 —— 先看多改了什么，别接着放它跑")
                why_stopped = "rc"
                break
    except KeyboardInterrupt:
        print("\n⏸ 人叫停（已经跑完 %d 轮）。已跑完的轮次都还在，"
              "接着跑用同一条命令（它会从下一轮继续）" % len(_rounds(d)))
        return 130

    s = _write_summary(d)
    if why_stopped == "notrun":
        # ★ 不许落成"一共 0 轮 ｜ 没有该停的理由"（2026-10-28）：那两句连在一起，
        #   读起来像"它就快跑起来了"。实际是**压根没开始**，上面那段报错才是正事。
        print("\n一共 %d 轮 ｜ 这次**一轮都没跑起来**（rc=%s），不是停止判据说停的"
              % (s["n_rounds"], rc))
        return rc
    if why_stopped == "rc":
        # ★ 别在这时候再说"没有该停的理由"（2026-10-28 实测撞到）：
        #   停止判据确实没说停，可这一轮根本没跑完 —— 两句话同时出现，
        #   人只会读成"它说没理由停，那我接着跑？"。而真正该做的是先修那一轮。
        print("\n一共 %d 轮 ｜ 不是停止判据说停的，是第 %d 轮没跑完（rc=%s）"
              % (s["n_rounds"], s["n_rounds"], rc))
        print("  判据那边：%s" % (s.get("stop") or "确实没说要停 —— 所以更该先修这一轮"))
        return rc
    print("\n一共 %d 轮 ｜ %s" % (s["n_rounds"], s.get("stop") or "没有该停的理由"))
    if not s.get("stop"):
        print("  ★ 还可以再跑 —— 但「还能跑」不等于「该跑」。没有新判据要过就停下来。")
    return rc


# 对任务与轮次的状态证据生成摘要，用于判断连续执行是否取得变化。
def _fingerprint(d: Path) -> str:
    import hashlib
    try:
        b = (d / "job.json").read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(b).hexdigest()[:16]


# ─────────────────────────── status ───────────────────────────

_STATUS_ZH = {"success": "成功", "error": "失败", "paused": "暂停",
              "needs_human": "等人", "running": "在跑"}


# 重新归集任务状态、停下原因和各轮结果，供用户选择后续操作。
def cmd_status(a) -> int:
    d = _pick(a.job)
    if (d / "delegation.json").exists() or _load(d).get("managedConversation"):
        return _die("请在所属会话查看持续委托", 4)
    s = _summary(d)
    print("\n%s" % s["id"])
    print("  类型 %s ｜ 档位 %s ｜ 轮数 %d/%s ｜ 范围 %s"
          % (_load(d).get("kind") or "task", s["crew"], s["n_rounds"],
             s["max_rounds"] or "不限", "、".join(s["scope"]) or "整个工作区"))
    print("")
    # ★ 加了「契约」一列（2026-10-28）：自主暂停的判据是**通过数**，
    #   那这个数就必须在人看的那张表上。判据藏在代码里、人看不见，等于没有。
    print("  %-3s %-9s %-4s %-5s %-7s %-11s %s"
          % ("轮", "任务", "轮次", "动作", "契约", "测试者判断", "耗时/token"))
    for r in s["rounds"]:
        tk = r.get("tokens") or {}
        toks = ("%s+%s" % (tk.get("prompt"), tk.get("completion"))) if tk else "没记"
        el = ("%ds" % r["elapsed_s"]) if r.get("elapsed_s") is not None else "?"
        pn = _passed_n(r)
        ct = ("%d/%d" % pn) if pn else "没验"
        print("  %-3s %-9s %-4s %-5s %-7s %-11s %s / %s"
              % (r["n"], ("越界" if r.get("scope_violation") else
                 _STATUS_ZH.get(r.get("status"), r.get("status") or "?")), r.get("turns") if r.get("turns") is not None else "?",
                 r.get("actions") if r.get("actions") is not None else "?",
                 ct,
                 r.get("verdict") or ("—" if r.get("benni_ran") else "没跑"), el, toks))
    if s.get("stop"):
        print("\n  ⏸ %s" % s["stop"])
    print("\n  旧任务仅供查看。")
    # ★ 每轮被指到哪（2026-10-28）：这一句是"它当时为什么这么干"的唯一记录。
    #   放在表下面而不是塞进表里 —— 表已经 7 列，再加一列就没人读了。
    _hints = [(r["n"], r.get("plan_hint")) for r in s["rounds"] if r.get("plan_hint")]
    if _hints:
        print("\n  每轮的方向：")
        for _n, _h in _hints:
            print("    r%s  %s" % (_n, _h))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读查看旧任务记录")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="查看各轮记录")
    status.add_argument("--job", default="")
    return cmd_status(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
