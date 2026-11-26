#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""habits.py —— 跨项目的习惯区：我一贯怎么做。

三层记忆里它排第一（2026-10-26 用户定）
    习惯区（这里）      跨项目。我的做事风格、偏好。**人写**，每一条影响所有项目。
    规则区（记忆树）     本项目。这个项目必须遵守什么。人写 + distill。
    事实区（agent memory）本项目。入口在哪、上次那个坑。agent 自己写。

★ 两级结构：组（`##`）→ 叶子（`###`）
    2026-10-26 用户提「一个习惯一片叶子」，理由是可視化和编辑都接得住。
    采纳了，但**没有拆成一个个文件**：一轮要读几十个文件太贵。
    单文件 + 两级标记，解析出来就是一棵小树。

★ 选取：**不靠算法，靠元数据**
    原来想做一个"按任务挑习惯"的算法 —— 不做。那等于让机器判断"这条习惯这次用不用得上"，
    判断错了还不报错，而这正是我们明确要避免的（"用数字/规则代替判断"）。
    现在只有两条可核的规则：
      ① 组可以整组关（配置 `habits_off_groups`）—— 人说了算
      ② 其余全带
    超预算怎么办：**照样全带，只报警**。规则被悄悄丢掉，比多花两千字糟得多。

★ 硬上限是保底，不是省钱
    软上限（`habits_max_chars`）超了只提示；硬上限（`habits_hard_chars`）超了才
    按组从后往前丢，并且**明说丢了哪几组** —— 悄悄丢等于骗人。
"""
# 主要引用方包括 `tools.py` 和 `baseline.py`。
from __future__ import annotations

import json
import os
import threading
from functools import wraps
from pathlib import Path

_mutex = threading.RLock()
_owner = threading.local()


def _locked(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        from .filelock import FileLock
        with _mutex:
            if getattr(_owner, 'active', False):
                return fn(*args, **kwargs)
            with FileLock(index_path().with_suffix('.lock')):
                _owner.active = True
                try:
                    return fn(*args, **kwargs)
                finally:
                    _owner.active = False
    return call

from . import settings as SET

TEMPLATE = """# 我的习惯

> 这一份是**跨项目**的：不管在哪个仓库、哪个任务，都该这么做。
> 写「我希望 agent 怎么做」，不写「这个项目有什么」（那是记忆树的事）。
> 一个习惯一个小节（`###`）；`##` 是组，可以把整组关掉。

## 说话

### 结论先行

先给结论，再给理由。

### 给数字，不给形容词

「快了 3 倍」而不是「快了很多」。

## 动手

### 改之前先读

没读过就不改；没读全就不改远处。

## 不要

### 不要新建副本

同一个东西只留一份，副本一定会漂。
"""


INDEX = "habits.json"


def index_path() -> Path:
    """开关表。**用户级、跨项目一份**（2026-10-26 用户拍板）。

    ★ 为什么不做成"一个项目一份"：多数人就是一份表走天下，而一份表意味着
      **只有一份真源** —— 改一条、所有会话立刻生效，也不会有"新习惯在某个项目没同步"。
      项目级覆盖留着（`<repo>/.harness/habits.json`），但**默认不存在就不看**，
      所以它不会变成日常负担。
    """
    env = os.environ.get("HARNESS_HABITS_INDEX")
    return Path(env) if env else (Path.home() / ".harness" / INDEX)


def load_index() -> dict:
    """读开关表。**只有全局一份，没有项目级**（2026-10-26 用户确认）。

    ★ 为什么不留项目覆盖：留一个"以后可能要"的分支，等于留一段没人测的代码 ——
      而它一旦出错，表现是"某个项目的开关莫名其妙不对"，最难查。
      真要按项目区分，加回来是十几行的事，那时再加。
    """
    idx = {"mode": "all", "on": [], "off": []}
    p = index_path()
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                idx.update({k: raw[k] for k in ("mode", "on", "off", "memory", "revision") if k in raw})
                if not isinstance(idx.get('memory', {}), dict) or type(idx.get('revision', 0)) is not int:
                    raise ValueError('invalid habit metadata')
            else:
                raise ValueError('invalid habit index')
    except (OSError, ValueError):
        from .memory_tree import error
        error('习惯索引不可读取，原数据保留', 'memory_unavailable', 503)
    return idx


@_locked
def save_index(idx: dict) -> dict:
    """写全局表。只留非默认内容，保持文件小、可 review。"""
    p = index_path()
    clean = {"mode": idx.get("mode") or "all",
             "on": sorted(set(idx.get("on") or [])),
             "off": sorted(set(idx.get("off") or []))}
    if 'memory' in idx:
        clean['memory'] = idx['memory']
    clean['revision'] = idx.get('revision', 0) + 1
    from .memory_tree import MAX_BYTES, error
    if len(json.dumps(clean, ensure_ascii=False).encode('utf8')) > MAX_BYTES:
        error('习惯索引容量已满，原数据未改', 'memory_capacity', 409)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        from .filelock import atomic_json
        atomic_json(p, clean)
    except OSError as e:
        return {"ok": False, "error": "写不进开关表：%s" % e}
    return {"ok": True, "path": str(p), "index": clean}


@_locked
def toggle(title: str) -> dict:
    """翻一条。只改那一份全局表。"""
    t = str(title or "").strip()
    if not t:
        return {"ok": False, "error": "没写要翻哪一条"}
    idx = load_index()
    mode = idx.get("mode") or "all"
    lst = "off" if mode == "all" else "on"
    cur = set(idx.get(lst) or [])
    if t in cur:
        cur.discard(t)
    else:
        cur.add(t)
    # ★ 翻完之后这条到底开不开：all 模式看"不在 off 里"，only 模式看"在 on 里"。
    #   第一版把这里写反了（加进 off 却回报 on=True），界面上会显示相反的状态。
    now = (t not in cur) if mode == "all" else (t in cur)
    idx[lst] = sorted(cur)
    r = save_index(idx)
    r["title"] = t
    r["on"] = now
    return r


@_locked
def rename(old: str, new: str) -> dict:
    """改一条习惯的标题，并**把开关表里那条记录一起搬过去**。

    ★ 为什么必须一起搬（2026-10-26）：开关是按标题映射的。只改文件不改表，
      那条习惯会从「我关掉的」变成「默认开」—— 人以为自己只是改了个名字，
      而开关已经在管另一条（已经不存在的）标题了。这种错不报错，只会让人
      觉得「我明明关过它」。

    noqa: R4 —— 这里读写的是 **markdown**（HABITS.md），不是 Python 源码，
    所以"要不要 ast.parse"这条对它没意义。R4 的另一半（"验替换命中数"）
    是做到了的：下面先数 `hits`，不是 1 条就直接返回错误，一个字都不写。
    """
    o = str(old or "").strip()
    n = str(new or "").strip()
    if not o or not n:
        return {"ok": False, "error": "旧标题和新标题都要写"}
    if o == n:
        return {"ok": False, "error": "新旧标题一样，没得改"}
    if "\n" in n or n.startswith("#"):
        return {"ok": False, "error": "新标题里不能有换行或 #"}
    p = path()
    if not p.exists():
        return {"ok": False, "error": "还没有习惯区：%s" % p}
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "error": "读不出来：%s" % e}
    lines = raw.replace("\r\n", "\n").splitlines()

    hits = [i for i, ln in enumerate(lines)
            if ln.startswith("### ") and ln[4:].strip() == o]
    if not hits:
        return {"ok": False, "error": "没有标题叫「%s」的习惯" % o}
    if len(hits) > 1:
        # ★ 同名两条，开关本来就分不清（按标题认），改名只会把混乱藏得更深。
        return {"ok": False, "error": "有 %d 条都叫「%s」，先改掉其中一条"
                                      % (len(hits), o)}
    clash = [i for i, ln in enumerate(lines)
             if ln.startswith("### ") and ln[4:].strip() == n]
    if clash:
        return {"ok": False,
                "error": "已经有一条叫「%s」了（开关按标题认，两条同名会一起开一起关）" % n}

    # ★ 只换那一行的标题，其余字节原样保留 —— 重新渲染整份文件会顺手
    #   改掉空行和缩进，而那是人写的，不该被程序规矩化。
    lines[hits[0]] = "### " + n
    try:
        p.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": "写不进习惯区：%s" % e}

    idx = load_index()
    moved = []
    if o in idx.get('memory', {}):
        idx['memory'][n] = idx['memory'].pop(o)
        idx['memory'][n]['version'] += 1
    for k in ("on", "off"):
        cur = list(idx.get(k) or [])
        if o in cur:
            idx[k] = sorted({x for x in cur if x != o} | {n})
            moved.append(k)
    r = save_index(idx)
    return {"ok": bool(r.get("ok")), "from": o, "to": n, "path": str(p),
            "switch_moved": moved,
            "index": r.get("index") or idx,
            "error": r.get("error", "")}


def _lines() -> list[str] | None:
    """读成行。文件不存在返 None（**不创建**：读的时候偷偷建＝替人做决定）。"""
    p = path()
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace").replace(
            "\r\n", "\n").splitlines()
    except OSError:
        return None


def _save_lines(lines: list[str]) -> dict:
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": "写不进习惯区：%s" % e}
    return {"ok": True, "path": str(p)}


def _layout(lines: list[str]) -> list[dict]:
    """把行号映射出来：每个组从哪一行开始、到哪一行结束；每个叶子同理。

    ★ 为什么要这个（2026-10-26）：增/删/改都要求「只动那一段，其余字节原样」。
      整份 parse→_render 会把空行、缩进、人写的排版一起规矩化 ——
      那是人的文件，不该被程序重排。
    """
    groups: list[dict] = []
    cur = None
    for i, ln in enumerate(lines):
        if ln.startswith("## ") and not ln.startswith("### "):
            cur = {"name": ln[3:].strip(), "start": i, "leaves": []}
            groups.append(cur)
        elif ln.startswith("### "):
            if cur is None:
                cur = {"name": "（未分组）", "start": 0, "leaves": []}
                groups.append(cur)
            cur["leaves"].append({"title": ln[4:].strip(), "start": i})
    for k, g in enumerate(groups):
        nxt = groups[k + 1]["start"] if k + 1 < len(groups) else len(lines)
        g["end"] = nxt
        for j, lf in enumerate(g["leaves"]):
            lf["end"] = (g["leaves"][j + 1]["start"]
                         if j + 1 < len(g["leaves"]) else nxt)
    return groups


def _find(title: str):
    """按标题找那一条。返回 (行, 叶子, 所在组) 或 None。"""
    ls = _lines()
    if ls is None:
        return None
    for g in _layout(ls):
        for lf in g["leaves"]:
            if lf["title"] == title:
                return ls, lf, g
    return None


def _titles() -> list[str]:
    ls = _lines()
    if ls is None:
        return []
    return [lf["title"] for g in _layout(ls) for lf in g["leaves"]]


def _check_title(t: str) -> str:
    """标题的格式检查。返回错误文本，空串＝通过。"""
    if not t:
        return "标题不能空"
    if "\n" in t:
        return "标题里不能有换行"
    if t.startswith("#"):
        return "标题不用带 #，直接写文字"
    return ""


@_locked
def add(title: str, body: str = "", group: str = "") -> dict:
    """加一条习惯。★ 这是**人点的**，所以文件不存在时可以直接建。"""
    t = str(title or "").strip()
    b = str(body or "").strip()
    g = str(group or "").strip() or "习惯"
    err = _check_title(t)
    if err:
        return {"ok": False, "error": err}

    ls = _lines()
    if ls is None:                                  # 还没有文件：这就是"建一份"
        ls = ["# 我的习惯", ""]
        groups = []
    else:
        groups = _layout(ls)
    if any(lf["title"] == t for gg in groups for lf in gg["leaves"]):
        return {"ok": False,
                "error": "已经有「%s」了（开关按标题认，两条同名会一起开一起关）" % t}

    body_lines = b.splitlines() if b else []
    tgt = next((x for x in groups if x["name"] == g), None)
    if tgt is None:
        while ls and not ls[-1].strip():
            ls.pop()
        ls += (["", "## " + g, "", "### " + t]
               + ([""] + body_lines if body_lines else []))
    else:
        at = tgt["end"]
        while at - 1 > tgt["start"] and not ls[at - 1].strip():
            at -= 1
        # ★ 把这些多余的尾随空行**删掉**再插（只挪 `at` 不删，原来那个空行还留着，
        #   于是插进去之后变成两个空行 —— 第一次就是这么写错的）。
        del ls[at:tgt["end"]]
        block = ["", "### " + t] + ([""] + body_lines if body_lines else [])
        # 后面还有内容（下一个组）就补一个空行分隔；到文件末尾就不补。
        ls[at:at] = block + ([""] if at < len(ls) else [])

    r = _save_lines(ls)
    if not r.get("ok"):
        return r

    # ★ 开关表必须跟着走（2026-10-26）：
    #   ① 同名标题以前被关过（删了又加回来）⇒ 那条死记录会让新写的**静默不生效**，
    #      所以 add 一律把它从 off 里摘掉 —— 人刚手写了这一条，意思就是要它生效。
    #   ② mode=only（白名单）下新条目默认没人勾 ⇒ 同样"写了没反应"，所以要显式勾上。
    idx = load_index()
    mode = idx.get("mode") or "all"
    changed = False
    if t in (idx.get("off") or []):
        idx["off"] = sorted(x for x in idx["off"] if x != t)
        changed = True
    activated = False
    if mode == "only" and t not in (idx.get("on") or []):
        idx["on"] = sorted(set(idx.get("on") or []) | {t})
        activated = True
        changed = True
    if changed:
        save_index(idx)
    return {"ok": True, "title": t, "group": g, "path": r.get("path"),
            "activated": activated, "index": idx}


@_locked
def edit_body(title: str, body: str) -> dict:
    """改一条习惯的正文。标题不动 ⇒ 开关表不用动（开关是按标题认的）。"""
    t = str(title or "").strip()
    hit = _find(t)
    if hit is None:
        return {"ok": False, "error": "没有标题叫「%s」的习惯" % t}
    ls, lf, _g = hit
    body_lines = str(body or "").strip().splitlines()
    seg = [""] + body_lines + [""] if body_lines else [""]
    ls[lf["start"] + 1:lf["end"]] = seg
    r = _save_lines(ls)
    if r.get("ok"):
        r["title"] = t
        index = load_index()
        meta = index.get('memory', {}).get(t)
        if meta:
            from .memory_tree import digest
            meta.update(version=meta['version']+1, bodyHash=digest(str(body or '').strip()), evidence=[])
            saved = save_index(index)
            if not saved.get('ok'):
                return saved
    return r


@_locked
def remove(title: str) -> dict:
    """删一条习惯，**连开关表里那条记录一起删**。

    ★ 只删文件不删表，表里就多一条死名字（2026-10-26 实测过）：
      它不报错、也不影响当下，但下次有人写一个同名习惯，那条会被上一条的开关状态接管，
      表现是"我新写的这条怎么是关着的"。
    """
    t = str(title or "").strip()
    hit = _find(t)
    if hit is None:
        return {"ok": False, "error": "没有标题叫「%s」的习惯" % t}
    ls, lf, _g = hit
    del ls[lf["start"]:lf["end"]]
    r = _save_lines(ls)
    if not r.get("ok"):
        return r
    idx = load_index()
    moved = []
    if t in idx.get('memory', {}):
        del idx['memory'][t]
        moved.append('memory')
    for k in ("on", "off"):
        cur = list(idx.get(k) or [])
        if t in cur:
            idx[k] = [x for x in cur if x != t]
            moved.append(k)
    if moved:
        save_index(idx)
    return {"ok": True, "title": t, "switch_cleared": moved, "path": r.get("path")}


def dead() -> list[str]:
    """开关表里那些**文件里已经没有**的标题（删了习惯、或改名前留下的）。"""
    idx = load_index()
    have = set(_titles())
    return sorted({x for x in list(idx.get("on") or []) + list(idx.get("off") or [])
                   if x not in have})


@_locked
def prune() -> dict:
    """把死条目清掉。★ 只清表，不动文件。"""
    idx = load_index()
    bad = set(dead())
    if not bad:
        return {"ok": True, "removed": [], "path": str(index_path())}
    for k in ("on", "off"):
        idx[k] = [x for x in (idx.get(k) or []) if x not in bad]
    r = save_index(idx)
    r["removed"] = sorted(bad)
    return r


def path() -> Path:
    env = os.environ.get("HARNESS_HABITS")
    return Path(env) if env else (Path.home() / ".harness" / "HABITS.md")


def parse(text: str) -> list[dict]:
    """`##` 组 → `###` 叶子。返回 [{name, intro, leaves:[{title, body, chars}]}]。

    ★ 散在组外面的内容算进"（未分组）"，不丢 —— 人写到一半的草稿也是内容，
      丢掉之后再也没人记得那句草稿。
    """
    groups: list[dict] = []
    cur_g = None
    cur_l = None

    def new_group(name: str) -> dict:
        g = {"name": name, "intro": "", "leaves": [], "chars": len(name) + 4}
        groups.append(g)
        return g

    for ln in text.replace("\r\n", "\n").splitlines():
        if ln.startswith("# ") and not ln.startswith("## "):
            continue                       # 文件一级标题不是习惯
        if ln.startswith("## "):
            cur_g = new_group(ln[3:].strip())
            cur_l = None
        elif ln.startswith("### "):
            if cur_g is None:
                cur_g = new_group("（未分组）")
            cur_l = {"title": ln[4:].strip(), "body": "", "chars": len(ln) + 1}
            cur_g["leaves"].append(cur_l)
        elif cur_l is not None:
            cur_l["body"] += ln + "\n"
            cur_l["chars"] += len(ln) + 1
            cur_g["chars"] += len(ln) + 1
        elif cur_g is not None:
            cur_g["intro"] += ln + "\n"
            cur_g["chars"] += len(ln) + 1
        elif ln.strip() and not ln.startswith("#") and not ln.startswith(">"):
            # ★ 文件的一级标题和 `>` 引言不是习惯（2026-10-26：它们会变成两个空的
            #   「（未分组）」组，界面上看着像有东西，点开全是空）。
            cur_g = new_group("（未分组）")
            cur_g["intro"] += ln + "\n"
            cur_g["chars"] += len(ln) + 1
    for g in groups:
        g["intro"] = g["intro"].strip()
        for lf in g["leaves"]:
            lf["body"] = lf["body"].strip()
    return [g for g in groups if g["leaves"] or g["intro"]]


def _render(groups: list[dict]) -> str:
    out = []
    for g in groups:
        out.append("## " + g["name"])
        if g["intro"]:
            out.append(g["intro"])
        for lf in g["leaves"]:
            out.append("### " + lf["title"])
            if lf["body"]:
                out.append(lf["body"])
    return "\n\n".join(out).strip()


def _apply_switch(groups: list[dict], idx: dict) -> tuple[list[dict], int]:
    """按开关表过滤叶子。返回 (留着的组, 开关前的总条数)。

    ★ 组的开关在界面上做成"批量点叶子"，不单独存组状态 ——
      组状态 + 叶子例外会产生两种真源，而它们迟早会不一致。
    """
    mode = idx.get("mode") or "all"
    on = set(idx.get("on") or [])
    off = set(idx.get("off") or [])
    total = 0
    kept = []
    for g in groups:
        gl = []
        for lf in g["leaves"]:
            total += 1
            t = lf["title"]
            lf["on"] = (t not in off) if mode == "all" else (t in on)
            if lf["on"]:
                gl.append(lf)
        # ★ **不删叶子，只标记**：界面要能把它开回来。
        #   第一版直接过滤掉，结果"关掉的条目"在界面上根本不存在，永远开不回来。
        kept.append(dict(g, leaves=g["leaves"], on_leaves=len(gl)))
    return kept, total


def read(cfg: dict | None = None) -> dict:
    """读习惯区，按组/叶子解析。返回结构 + 预算状态。

    ★ 不给 cfg 就用配置里的 memory 段（默认值在 settings，"一个数字只写一处"）。
    """
    cfg = SET.memory() if cfg is None else cfg
    soft = int(cfg.get("habits_max_chars") or SET.DEFAULT_MEMORY["habits_max_chars"])
    hard = int(cfg.get("habits_hard_chars") or SET.DEFAULT_MEMORY["habits_hard_chars"])
    off = [str(x) for x in (cfg.get("habits_off_groups") or [])]
    p = path()
    out = {"path": str(p), "text": "", "chars": 0, "groups": [], "missing": False,
           "hint": "", "soft": soft, "hard": hard, "over_soft": False,
           "over_hard": False, "dropped": [], "off": off}
    if not p.exists():
        out["missing"] = True
        out["hint"] = ("还没有习惯区。要建就写一份 %s，"
                       "内容是「我希望 agent 跨项目都这么做」的那几条。" % p)
        return out
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        out["missing"] = True
        out["hint"] = "习惯区读不出来：%s" % e
        return out

    groups = parse(raw)
    # ★ 同名叶子：开关按**标题**映射，两条同名会"一起开一起关" ——
    #   那种事不会报错，只是人点了半天发现另一条也跟着变了（2026-10-26）。
    _seen: dict[str, int] = {}
    for g in groups:
        for lf in g["leaves"]:
            _seen[lf["title"]] = _seen.get(lf["title"], 0) + 1
    out["dups"] = sorted(t for t, c in _seen.items() if c > 1)
    idx = load_index()
    groups, total = _apply_switch(groups, idx)
    out["index"] = idx
    out["total"] = total
    out["on_count"] = sum(1 for g in groups for lf in g["leaves"] if lf.get("on"))
    if off:
        kept = []
        for g in groups:
            if g["name"] in off:
                out["dropped"].append(g["name"] + "（配置里关掉了）")
            else:
                kept.append(g)
        groups = kept

    # 注入用的文本只含**开着的**条目；界面用的结构里保留全部（带 on 标记）
    _on_groups = [{**g, "leaves": [lf for lf in g["leaves"] if lf.get("on")]}
                  for g in groups]
    # ★ 只有 intro、没有叶子的组也要留（2026-10-26）：整份习惯区可能就写成
    #   一段话没分小节，把它过滤掉等于"写了但一个字都没注入"，而人看不到任何提示。
    _on_groups = [g for g in _on_groups if g["leaves"] or g.get("intro")]
    text = _render(_on_groups)
    over = len(text) > soft
    if len(text) > hard:
        # ★ 只有到**硬上限**才丢，而且按组从后往前丢、明说丢了谁。
        kept, used = [], 0
        for g in groups:
            if used + g["chars"] + 4 > hard and kept:
                out["dropped"].append(g["name"] + "（超硬上限 %d 字）" % hard)
                continue
            kept.append(g)
            used += g["chars"] + 4
        groups = kept
        text = _render([{**g, "leaves": [lf for lf in g["leaves"] if lf.get("on")]}
                        for g in groups if g["leaves"]])
        out["over_hard"] = True
        # ★ 丢掉的组要在**注入文本里**说一句（2026-10-26 审计时发现）。
        #   原来只写进 `dropped` 字段 —— 那是给界面看的，模型看不到，
        #   于是它会以为自己拿到了全部规则。这正是我们反复写要避免的"假装"。
        if out["dropped"]:
            text += ("\n\n…（习惯区超过硬上限 %d 字，下面这几组**没有带**：%s —— "
                     "要它们生效，先整理小的。）"
                     % (hard, "、".join(out["dropped"])))

    out["groups"] = groups
    out["chars"] = len(text)
    out["over_soft"] = over and not out["over_hard"]
    if out["over_soft"]:
        text += ("\n\n…（习惯区 %d 字，超过软上限 %d。**没有截掉任何一条** —— "
                 "规则不该被悄悄丢。但该整理了：能删的删，跨项目不成立的挪回项目规则。）"
                 % (len(text), soft))
    out["text"] = text
    return out


def outline(cfg: dict | None = None) -> dict:
    """给界面用：组 → 叶子（带字数 / 开关状态）+ 预算状态。"""
    r = read(cfg)
    nodes = []
    for g in r["groups"]:
        for lf in g["leaves"]:
            nodes.append({"title": lf["title"], "chars": lf["chars"],
                          "group": g["name"]})
    # ★ 预算条要能回答"超了该删谁"，所以顺带给出**最长的三条**。
    #   只给一个「1200/2000」的话，人知道超了但不知道从哪下手（2026-10-26）。
    top = sorted(({"title": lf["title"], "chars": lf["chars"], "group": g["name"]}
                  for g in r["groups"] for lf in g["leaves"] if lf.get("on")),
                 key=lambda x: -x["chars"])[:3]
    soft = r["soft"] or 1
    # ★ 开关表里那些文件已经没有的标题（2026-10-26）：界面要给一个「清掉」的入口，
    #   否则表会只增不减，慢慢变成一份对不上的老名字清单。
    _have = {lf["title"] for g in r["groups"] for lf in g["leaves"]}
    _idx = r.get("index") or {}
    deadt = sorted({x for x in list(_idx.get("on") or []) + list(_idx.get("off") or [])
                    if x not in _have})
    return {"path": r["path"], "missing": r["missing"], "hint": r["hint"],
            "chars": r["chars"], "soft": r["soft"], "hard": r["hard"],
            "over_soft": r["over_soft"], "over_hard": r["over_hard"],
            "dropped": r["dropped"], "groups": r["groups"], "nodes": nodes,
            "index": r.get("index") or {}, "total": r.get("total") or 0,
            "on_count": r.get("on_count") or 0, "dups": r.get("dups") or [],
            "top": top, "pct": int(round(100.0 * r["chars"] / soft)),
            "dead": deadt}


def ensure(cfg: dict | None = None) -> dict:
    """没有就建一份模板。**只在人明确要「建一个」时调用**，不读的时候偷偷写。"""
    p = path()
    if p.exists():
        return {"ok": True, "created": False, "path": str(p)}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TEMPLATE, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "created": False, "path": str(p),
                "error": "建不了：%s" % e}
    return {"ok": True, "created": True, "path": str(p)}


def memory_nodes():
    """Metadata refers to HABITS.md; it never stores a second habit body."""
    from copy import deepcopy
    from .memory_tree import digest
    index = load_index()
    metadata = index.get('memory', {})
    if not metadata:
        return []
    result = []
    for group in outline()['groups']:
        for leaf in group['leaves']:
            meta = metadata.get(leaf['title'])
            if not meta:
                continue
            body = leaf.get('body', '')
            # A human edit is a new content identity, not inherited verification.
            unchanged = digest(body) == meta['bodyHash']
            result.append(dict(deepcopy(meta), tree='habits', scope='habit',
                key=meta['id'], title=leaf['title'], body=body,
                version=meta['version'] if unchanged else digest(body),
                evidence=deepcopy(meta.get('evidence', [])) if unchanged else [],
                layer=['用户习惯', group['name']], state='active' if leaf.get('on', True) else 'reference',
                branches=[], kind='decision', entryType='rule', adopted=True,
                storeRevision=index.get('revision', 0), versions=[]))
    return result


@_locked
def adopt_memory(title, body, metadata):
    """Human adoption, replay-safe across a crash before proposal removal."""
    from .filelock import FileLock
    from .memory_tree import digest, error
    with FileLock(index_path().with_suffix('.memory.lock')):
        index = load_index()
        old = index.get('memory', {}).get(title)
        current = next((n for n in memory_nodes() if n['title'] == title), None)
        if old and old.get('proposalId') == metadata['proposalId']:
            if current and current['body'] == body:
                return dict(ok=True, id=old['id'], duplicate=True)
            if current:
                error('同名习惯正文已变化，原数据未覆盖', 'memory_stale', 409)
        elif old or _find(title):
            error('同名习惯已存在，请由用户核对，未覆盖', 'memory_duplicate', 409)
        # Publish the reference first. Until the matching body exists it is inert.
        value = dict(metadata, bodyHash=digest(body), version=1)
        index.setdefault('memory', {})[title] = value
        saved = save_index(index)
        if not saved.get('ok'):
            return saved
        result = add(title, body, group='凝练经验')
        return dict(result, id=value['id'])


@_locked
def record_memory_injection(ident, run_id, items):
    from .memory_tree import digest
    import time
    from . import projects
    pid = projects.conversation(ident).get('projectId')
    selected = {i['id']: i['version'] for i in items if isinstance(i, dict)}
    current = {n['id']: n for n in memory_nodes() if selected.get(n['id']) == n['version']}
    index = load_index()
    changed = False
    for meta in index.get('memory', {}).values():
        n = current.get(meta['id'])
        if not n:
            continue
        events = meta.setdefault('evidence', [])
        if not any(e['kind']=='injected' and e.get('runId')==run_id and e.get('factVersion')==n['version'] for e in events):
            events.append(dict(kind='injected', conversationId=ident, runId=run_id,
                projectId=pid, factVersion=n['version'], at=time.time()))
            changed = True
    if changed:
        result = save_index(index)
        if not result.get('ok'):
            from .memory_tree import error
            error('习惯带入记录未保存', 'memory_unavailable', 503)
