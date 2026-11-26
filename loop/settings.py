#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""settings.py —— 会变的那些数字，集中在一处。

★ 为什么不能继续散在各模块的模块级常量里（2026-10-26 用户拍板）
    一开始写死是对的：先让它能跑起来。
    但这个 agent 要**能给别人用**之后，它们就变成了一道隐形的墙 ——
    别人的机器、别人的项目、别人的预算，跟我们不一样。
    成熟厂商把每一样都做成可配（Claude 的 frontmatter、Codex 的 `[agents]` 段），
    原因不是它优雅，而是**一个数字不可能同时适合所有人和所有机器**。

读哪里
    `harness/charter.json` 的 `agents` / `sandbox` 两段。
    和 `charter.context_limit` 是**同一个文件、不同段** —— 一个配置入口，不是两个真源。
    没写就用默认值；写错了（类型不对）用默认值并记一条警告，**不静默**。

★ 刻意不做的
    不做环境变量覆盖。两个入口就意味着"到底哪个生效"要靠猜，
    而配置文件本来就能按机器不同各写一份。
"""
# 主要引用方包括 `tools.py` 和 `habits.py`。
from __future__ import annotations

import json
from pathlib import Path

from . import paths as P            # 程序根 / 数据根，只此一处
ROOT = P.data_root()               # 数据根（打包后是用户目录；开发时就是仓库根）
CONFIG = ROOT / "harness" / "charter.json"

DEFAULT_AGENTS = {
    "max_parallel": 4,          # 同时最多几个子 agent（超出的排队，不拒绝）
    "max_turns": 0,             # 子任务轮数上限。0 = 不限（2026-10-28 改）。★ 它只是成本闸，不是"干完没有"的判据
    "max_chars": 4000,          # 子任务结论的截断长度（父的上下文要装得下）
    "max_depth": 0,             # 子 agent 能不能再派：0=不能，1=能派一层（见下面注释）
    # 待采纳的规则提案最多几条。★ 这是**队列长度**，不是"这条该不该提"的判断 ——
    #   超了只是让人先处理，不是否决 agent 的眼光。
    "max_proposals": 20,
}

# 记忆三层（2026-10-26 用户拍板）：
#   习惯区（跨项目，人写）· 规则区（本项目，人 + distill）· 事实区（本项目，agent 自己写）
# ★ 三个区的**预算**都在这儿，因为它们会互相挤：三层都往上下文里塞，
#   最后谁都读不到 —— "注入了但被挤到后面"等于没注入。
DEFAULT_MEMORY = {
    "habits_max_chars": 2000,      # 软上限：超了**照样全带**，只报警（规则不该被悄悄丢）
    "habits_hard_chars": 8000,     # 硬上限：超了才按组从后往前丢，并明说丢了哪组
    "facts_max_files": 200,        # 事实区（agent memory）最多读几个文件
    "facts_max_chars": 25000,      # 事实区注入上限（照 Claude 的 25KB 经验值）
    "facts_ttl_days": 90,          # 事实多久没验证就标「可能已过期」
}

# run_check 的白名单（2026-10-26）
# ★ 这里只有**追加**，没有"替换"：写一份不完整的 allow 表等于把自己锁在门外，
#   而锁死的表现是「子 agent 回来说这事我做不到」，日志里只有一条 rc=126。
DEFAULT_RUNCHECK = {
    "extra_bins": [],      # 额外放行的可执行名（追加到内置名单上）
}

DEFAULT_SANDBOX = {
    "max_files": 20000,         # 隔离副本的文件数上限（防"派个活把机器卡死"）
    "max_bytes": 300 * 1024 * 1024,
    "only": [],                 # 只拷这些子树（空＝整个工作区）
    # 执行层隔离（`loop/isolate.py`，2026-10-26）：给子进程套 Job Object。
    # ★ 键名和默认值都写在这里，写错的键会被 `_merge` 报出来 —— 这一段也吃这条规矩。
    "isolate": {"enabled": True, "ui_limits": True,
                "max_procs": 64, "mem_mb": 3072},
    # 离线开关（`loop/netguard.py`）：把子进程的代理指向一个没人听的端口。
    # ★ 默认关：它只挡"读代理变量"的程序，而开了会让"本来要联网的活"全失败。
    "offline": False,
    # ★ 容器档（`loop/contain.py`，2026-10-26）：默认关。
    #   ★ 这个键**必须写在这里**：`_merge` 只保留默认值里有的键，
    #     写进 charter.json 但这里没有 ⇒ 配了等于没配（2026-10-26 实测踩到：
    #     `contain.enabled()` 永远 False，而界面上什么都看不出来）。
    "container": False,
}

# 类型校验：键 → 期望的类型。写错了就用默认值，并留下一条能查的警告。
_KIND = {
    "max_parallel": int, "max_turns": int, "max_chars": int, "max_depth": int,
    "max_files": int, "max_bytes": int, "only": list,
    "max_proposals": int,
    "habits_max_chars": int, "habits_hard_chars": int,
    "facts_max_files": int, "facts_max_chars": int,
    "facts_ttl_days": int,
    "extra_bins": list,
}


def _merge(section: str, base: dict, raw) -> tuple[dict, list[str]]:
    out = dict(base)
    warns: list[str] = []
    if raw is None:
        return out, warns
    if not isinstance(raw, dict):
        return out, ["配置里的 `%s` 应该是一个对象，实际是 %s —— 这一段全部按默认值"
                     % (section, type(raw).__name__)]
    for k, v in raw.items():
        if k.startswith("_"):
            continue                     # `_说明` 这类注释键，不当配置
        if k not in base:
            warns.append("配置里的 `%s.%s` 不认识（可能是拼错了），已忽略" % (section, k))
            continue
        want = _KIND.get(k)
        if want is int:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                warns.append("`%s.%s` 应该是整数，给的是 %r —— 用默认值 %r"
                             % (section, k, v, base[k]))
                continue
            if iv < 0:
                warns.append("`%s.%s` 不能是负数（%r）—— 用默认值" % (section, k, iv))
                continue
            out[k] = iv
        elif want is list:
            if not isinstance(v, list):
                warns.append("`%s.%s` 应该是一个列表，给的是 %s —— 用默认值"
                             % (section, k, type(v).__name__))
                continue
            out[k] = [str(x) for x in v if str(x).strip()]
        else:
            out[k] = v
    return out, warns


def _read() -> dict:
    try:
        if CONFIG.exists():
            return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def load(path: Path | None = None) -> dict:
    """一次读出两段 + 警告。给需要同时看两边的地方用（比如界面上的设置页）。"""
    cfg = _read() if path is None else _load_path(Path(path))
    a, wa = _merge("agents", DEFAULT_AGENTS, cfg.get("agents"))
    s, ws = _merge("sandbox", DEFAULT_SANDBOX, cfg.get("sandbox"))
    m, wm = _merge("memory", DEFAULT_MEMORY, cfg.get("memory"))
    rc, wr = _merge("run_check", DEFAULT_RUNCHECK, cfg.get("run_check"))
    return {"agents": a, "sandbox": s, "memory": m, "run_check": rc,
            "warns": wa + ws + wm + wr}


def _load_path(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def agents(path: Path | None = None) -> dict:
    return load(path)["agents"]


def sandbox(path: Path | None = None) -> dict:
    return load(path)["sandbox"]


def memory(path: Path | None = None) -> dict:
    return load(path)["memory"]


def run_check(path: Path | None = None) -> dict:
    """`run_check` 的白名单配置（目前只有 `extra_bins`）。"""
    return load(path)["run_check"]


def warns(path: Path | None = None) -> list[str]:
    """配置写错的地方。★ 界面该把它显示出来 —— 静默忽略等于让人以为改生效了。"""
    return load(path)["warns"]
