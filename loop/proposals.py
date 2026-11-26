#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""proposals.py —— agent 提的**规则提案**：还没成为规则的东西。

为什么要有这一格（2026-10-26 用户定的判据）
    「这条写入会不会在无人在场时改变下一步做什么？」
      会（两条路要定夺、范围/取舍变了）⇒ 不外委，人来定
      不会（补一条事实、记一个坑、补一条命令怎么跑）⇒ agent 可以自己写
    提案区就是"不会立刻改变行为"的那一格：**它不参与任何注入**，
    人不采纳就等于不存在。所以 agent 在这里可以自由一点，代价也小。

为什么不做"直接写进树"
    树每轮都注入 —— 写脏一条，每一轮都被带偏，而且没人知道是谁写的。
    提案 + 人采纳这个中间步骤，是"机器不写教训"那条铁律唯一能松口的地方：
    松的是**提议权**，不是**生效权**。

去重与上限
    同层同名只留一条（再提就更新正文并记次数）；已经在树里的直接拒。
    上限是**队列长度**，不是"这条该不该提"的判断 —— 超了拒收并让人先处理。
"""
# 主要引用方包括 `tools.py` 和 `memory_service.py`。
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import settings as SET


from functools import wraps
import threading
from .filelock import FileLock, atomic_json
_proposal_mutex = threading.RLock()
_proposal_owner = threading.local()


def locked(fn):
    @wraps(fn)
    def call(store, *args, **kwargs):
        with _proposal_mutex:
            if getattr(_proposal_owner, 'active', False):
                return fn(store, *args, **kwargs)
            with FileLock(path(store).with_suffix('.lock')):
                p = path(store)
                if p.exists():
                    raw = json.loads(p.read_text(encoding='utf-8'))
                    entries = raw.get('items') if isinstance(raw, dict) else raw
                    if not isinstance(entries, list) or any(not isinstance(v, dict) or not v.get('id') for v in entries):
                        raise ValueError('提案库格式异常，未覆盖')
                _proposal_owner.active = True
                try:
                    return fn(store, *args, **kwargs)
                finally:
                    _proposal_owner.active = False
    return call


def path(store) -> Path:
    env = os.environ.get("HARNESS_PROPOSED")
    if env:
        return Path(env)
    base = Path(store) if store else Path.cwd()
    return base / ".harness" / "proposed.json"


def _id(layer: str, title: str, kind: str = "rule") -> str:
    # ★ kind 进哈希（2026-10-26 加了习惯提案）：习惯和规则可以**同名**，
    #   只按 `层::标题` 算 id 的话，提一条叫「先读再改」的习惯会覆盖掉
    #   叫「先读再改」的规则提案 —— 两条完全不同的事挤成一个 id，采纳时必错。
    return hashlib.sha1(("%s::%s::%s" % (kind, layer, title))
                        .encode("utf-8")).hexdigest()[:8]


def _cap() -> int:
    try:
        return max(1, int(SET.agents().get("max_proposals") or 20))
    except Exception:
        return 20


def load(store) -> list[dict]:
    p = path(store)
    try:
        if not p.exists():
            return []
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("items") or []
        return [x for x in raw if isinstance(x, dict) and x.get("id")]
    except Exception:
        return []


def save(store, items: list[dict]) -> dict:
    p = path(store)
    clean = [{"id": x["id"], "kind": x.get("kind") or "rule",
              "layer": x.get("layer", ""), "title": x.get("title", ""),
              "body": x.get("body", ""), "why": x.get("why", ""),
              "src": x.get("src", ""), "at": x.get("at", ""),
              "times": int(x.get("times") or 1),
              # ★ `allow`（2026-10-26）：免问提案的那份结构化范围。
              #   不写进 clean 的话，存一次就丢了 —— 而"提案还在、范围没了"
              #   会让人采纳出一条没有范围的东西（比没有更坏）。
              "allow": x.get("allow") or {},
              # ★ 改过的痕迹也要保住（2026-10-26）：丢了的话"改一下"这个动作
              #   在存盘之后就没了痕迹，人下次看到那条会以为从来没人动过。
              "edited": int(x.get("edited") or 0),
              "prev_body": x.get("prev_body") or "",
              "memorySource": x.get("memorySource")} for x in items]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(p, {"items": clean})
    except OSError as e:
        return {"ok": False, "error": "写不进提案表：%s" % e}
    return {"ok": True, "path": str(p), "count": len(clean)}


@locked
def add(store, tree_root, layer: str, title: str, body: str,
        why: str = "", src: str = "", kind: str = "rule",
        allow=None) -> dict:
    """加一条提案。★ 这里**永远不碰树、也不碰习惯文件** —— 只写提案表。

    ★ `kind`（2026-10-26）：`rule` 进规则树，`habit` 进跨项目习惯区。
      两个区的"采纳"落点不同，但**提议这一侧是同一张表** —— 因为它们要的性质一样：
      不生效、等人点头、来源可查、重复可合。再造一张习惯提案表就是两份真源。

    ★ `kind="allow"`（2026-10-26 加）：**免问提案** —— 「以后这类都别问」。
      它是这张表里唯一一种**采纳之后会扩大权力**的提案，所以：
        · 只有人点界面按钮才产生（agent 没有提它的工具）；
        · 采纳走的不是"写一条规矩"，而是 `ruleedit` 往规则文件里加**例外**；
        · 范围是命中规则算出来的，不是谁手填的 glob。
      为什么不干脆让 agent 用 `propose_rule` 提一条"以后都别问 X"的规矩：
        那会把"扩大权力"藏进一条自然语言里，而审的人看不到它到底放开了什么。
    """
    from . import charter as CH

    kd = "habit" if str(kind or "").strip().lower() == "habit" else "rule"
    _al = allow if isinstance(allow, dict) and allow.get("domain") else None
    if _al:
        kd = "allow"
    name = str(layer or "").strip()
    t = " ".join(str(title or "").split())[:60]
    b = str(body or "").strip()
    if not t:
        return {"ok": False, "error": "标题不能空（一句话说清这条规矩是什么）"}
    if len(b) < 10:
        return {"ok": False, "error": "正文太短（%d 字）—— 采纳的人要能看懂它管什么" % len(b)}
    if kd == "habit":
        # ★ 习惯的"层"是组名。组不存在没关系（`habits.add` 会建），
        #   但同名习惯必须挡掉：开关按标题认，两条同名会一起开一起关。
        from . import habits as HB
        for g in HB.parse((HB.path().read_text(encoding="utf-8", errors="replace")
                           if HB.path().exists() else "")):
            for lf in g.get("leaves") or []:
                if lf.get("title") == t:
                    return {"ok": False,
                            "error": "习惯区已经有「%s」了（同名两条会一起开一起关）" % t}
    else:
        if tree_root is not None and name not in CH.all_layers(tree_root):
            return {"ok": False, "error": "没有这一层：%s（可选：%s）"
                                          % (name or "（空）",
                                             "、".join(CH.all_layers(tree_root)) or "无")}
        # 已经在树里 ⇒ 不用提
        if tree_root is not None:
            for sec in CH.layer_sections(tree_root, name):
                if sec["title"] == t:
                    return {"ok": False, "error": "树里已经有「%s」了（同层同名）" % t}

    items = load(store)
    # ★ 免问提案按**范围**去重（不是按标题）：两个不同路径的"以后别问"是两件事，
    #   而同一段目录被点两次应当合成一条（`times` 数上去）。
    pid = (_id("allow", _scope_key(_al), "allow") if _al else _id(name, t, kd))
    for it in items:
        if it["id"] == pid:
            it["body"] = b
            it["why"] = why or it.get("why", "")
            it["times"] = int(it.get("times") or 1) + 1
            it["at"] = _now()
            save(store, items)
            return {"ok": True, "id": pid, "dup": True, "allow": it.get("allow") or {},
                    "note": "这条之前提过，已更新（第 %d 次）" % it["times"]}

    if len(items) >= _cap():
        return {"ok": False,
                "error": "待采纳的提案已经 %d 条了（上限 %d）—— 先让人处理掉几条再提"
                         % (len(items), _cap())}

    items.append({"id": pid, "kind": kd, "layer": name, "title": t, "body": b,
                  "why": why, "src": src, "at": _now(), "times": 1,
                  "allow": _al or {}})
    r = save(store, items)
    if not r.get("ok"):
        return r
    return {"ok": True, "id": pid, "count": len(items), "allow": _al or {},
            "note": ("已记为免问提案。**它现在还不会生效** —— 采纳之后才不问。"
                     if _al else
                     "已记为提案。**它现在还不会生效**，要人采纳才进树。")}


def _scope_key(allow) -> str:
    """一条免问提案的身份：domain + 动作 + 范围。"""
    a = allow or {}
    d = str(a.get("domain") or "")
    if d == "file":
        return "file|%s|%s" % (a.get("action") or "", a.get("path") or "")
    if d == "script":
        return "script|%s|%s" % (a.get("file") or "", a.get("also_file") or "")
    if d == "mcp":
        return "mcp|%s|%s|%s" % (a.get("server") or "", a.get("tool") or "",
                                 a.get("action") or "")
    return d


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def get(store, pid: str) -> dict | None:
    for it in load(store):
        if it["id"] == pid:
            return it
    return None


@locked
def adopt(store, tree_root, pid: str, layer: str = "") -> dict:
    """采纳一条：真的写进树，然后从提案表里删掉。

    ★ 写树走 `tree.add_node`（唯一的写树入口），不在这里自己拼文件。
    """
    from . import tree as TR

    it = get(store, pid)
    if it is None:
        return {"ok": False, "error": "没有这条提案（可能已经处理过）"}
    if it.get("memorySource"):
        from .memory_learning import adopt_scoped
        result = adopt_scoped(it)
        if result.get("ok"): save(store, [x for x in load(store) if x["id"] != pid])
        return result
    if (it.get("kind") or "rule") == "allow":
        # ★★ 免问提案的采纳：走 `ruleedit`（它先留底再写、且过一遍底线校验）。
        #   这里**不自己拼 JSON** —— 规则文件只有一个写入口，多一个就是两份真源。
        from . import ruleedit as RE
        al = it.get("allow") or {}
        dom = str(al.get("domain") or "")
        why = str(it.get("why") or "") or "界面里点的「以后这类都别问」"
        # ★ 采纳这一侧**再验一遍**：上游（界面/提案表）都可能是旧的、或被手改过。
        #   上游的校验是体验，这里的校验才是边界。
        _v = RE.validate_scope(al)
        if not _v.get("ok"):
            return {"ok": False, "error": _v.get("error") or "范围不成立"}
        if dom == "file":
            r = RE.add_file(str(al.get("action") or "read"), str(al.get("path") or ""),
                            why=why, src="proposal:" + pid)
        elif dom == "script":
            r = RE.add_script(str(al.get("file") or ""), why=why,
                              src="proposal:" + pid)
            if r.get("ok") and al.get("also_file"):
                r2 = RE.add_file(str(al.get("also_action") or "read"),
                                 str(al.get("also_file")), why=why,
                                 src="proposal:" + pid)
                if r2.get("ok"):
                    r["also"] = r2.get("note")
        elif dom == "mcp":
            r = RE.add_mcp(str(al.get("server") or ""), str(al.get("tool") or ""),
                           str(al.get("action") or "read"), why=why,
                           src="proposal:" + pid)
        else:
            return {"ok": False, "error": "这条免问提案的范围认不出来（domain=%s）" % dom}
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "写不进规则文件", "detail": r}
        save(store, [x for x in load(store) if x["id"] != pid])
        return {"ok": True, "adopted": it["title"], "kind": "allow", "domain": dom,
                "scope": al, "note": (r.get("note") or "已记为例外") +
                (("；" + r["also"]) if r.get("also") else "") +
                "。下一次同样的动作就不再问人。"}
    if (it.get("kind") or "rule") == "habit":
        # ★ 习惯的采纳落在**跨项目**那一份文件上，跟树没关系 ——
        #   所以这里不看 tree_root（习惯区可以在没有记忆树的机器上单独用）。
        from . import habits as HB
        r = HB.add(it["title"], it["body"], group=layer or it.get("layer") or "")
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "写不进习惯区", "detail": r}
        save(store, [x for x in load(store) if x["id"] != pid])
        return {"ok": True, "adopted": it["title"], "layer": r.get("group") or "",
                "kind": "habit",
                "note": "已写进习惯区（跨项目）——下一条消息起就生效"}
    if tree_root is None:
        return {"ok": False, "error": "树的根没找到"}
    r = TR.add_node(tree_root, layer or it["layer"], it["title"], it["body"])
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "写不进树", "detail": r}
    save(store, [x for x in load(store) if x["id"] != pid])
    return {"ok": True, "adopted": it["title"], "layer": it["layer"], "kind": "rule",
            "note": "已写进树。下一次任务开始时生效（树是每轮现拼的）。"}


@locked
def edit_body(store, pid: str, body: str = "", why: str = "") -> dict:
    """改一条提案的**正文**（和可选的"凭什么"）。

    ★ 为什么不支持改标题（2026-10-26）：id 是按 `层::标题` 算的 ——
      改标题就是换一条，于是"提过几次 / 哪一轮提的 / 来源"全对不上。
      而人真正想改的通常是正文（措辞太重、少了一句限定、多了个例子）。
      真要换标题，就拒了重提，那才是"另一条"。
    ★ 为什么留一份旧正文：改错了要能看见**原来那句**。
      只留最近一次的 `prev_body` 就够（不是版本库，只是"刚才那句是什么"）。
    """
    it = get(store, pid)
    if it is None:
        return {"ok": False, "error": "没有这条提案（可能已经处理过）"}
    b = str(body or "").strip()
    if b and len(b) < 10:
        return {"ok": False, "error": "正文太短（%d 字）—— 采纳的人要能看懂它管什么" % len(b)}
    if not b and not str(why or "").strip():
        return {"ok": False, "error": "什么都没改"}
    items = load(store)
    for x in items:
        if x["id"] != pid:
            continue
        if b and b != x.get("body"):
            x["prev_body"] = x.get("body") or ""
            x["body"] = b
        if str(why or "").strip():
            x["why"] = str(why).strip()
        x["at"] = _now()
        x["edited"] = int(x.get("edited") or 0) + 1
        r = save(store, items)
        if not r.get("ok"):
            return r
        return {"ok": True, "id": pid, "edited": x["edited"],
                "note": "改好了（还没采纳，仍然不影响任何行为）"}
    return {"ok": False, "error": "没有这条提案"}


@locked
def reject(store, pid: str, why: str = "") -> dict:
    it = get(store, pid)
    if it is None:
        return {"ok": False, "error": "没有这条提案"}
    save(store, [x for x in load(store) if x["id"] != pid])
    return {"ok": True, "rejected": it["title"], "why": why}


def overview(store) -> dict:
    items = load(store)
    return {"count": len(items), "cap": _cap(), "path": str(path(store)),
            "items": sorted(items, key=lambda x: x.get("at") or "", reverse=True)}



@locked
def enqueue_memory(store, layer, title, body, source):
    # Scope and proposal are published together. A crash cannot leave a scoped
    # candidate looking like an ordinary cross-project habit proposal.
    items = load(store)
    pid = _id(layer, title, 'habit')
    item = next((v for v in items if v['id'] == pid), None)
    if item is None:
        if len(items) >= _cap():
            return {'ok': False, 'error': '习惯候选队列已满，请先处理已有候选'}
        item = dict(id=pid,kind='habit',layer=layer,title=title,times=0)
        items.append(item)
    item.update(body=body,why='记忆升格候选；目标范围：'+source['targetScope'],src=source['conversationId'],at=_now(),times=int(item.get('times',0))+1,memorySource=source)
    saved = save(store, items)
    return dict(ok=True,id=pid,dup=item['times']>1) if saved.get('ok') else saved
