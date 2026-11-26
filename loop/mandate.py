#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""授权凭证 —— 锁契约不只是"锁过一次"，而是一张能**重新校验**的凭证

它解决什么
    现在的 `done.lock` 只证明"人锁过一次"。续跑时没人问：
    当初凭什么让它干？契约被改了还算不算？范围变大了还算不算？
    于是一份几个月前的锁，可以给一份完全不同的契约背书。

它是什么
    锁契约时写一份 `contract/mandate.json`，记三样：
      · policy_version —— 契约内容 + 范围的哈希，代表"批准的是哪一份"
      · scope          —— 允许动哪些地方
      · strength       —— `explicit`（人锁的）还是 `inferred`（程序补记的）
    续跑前 `check()` 重新算一遍当前契约和范围，对不上就不许静默继续。

两条边界
    · 契约变了 ⇒ 授权失效，必须重新锁（重新签发）。
    · 范围**扩大** ⇒ 授权失效；范围**缩小** ⇒ 放行，但记一句提示。
      缩小只是少做，不需要重新授权；扩大是多拿了权力，必须重新批。

老委托怎么办
    没有 mandate.json 的，由 `bin/job.py` 在**真跑之前**按当前契约补一张
    `inferred`，并明确打印"这是补记的，不是人当时批的"。
    不补就没法跑老委托；补成 explicit 则是伪造人当时的批准。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from pathlib import Path

NAME = "mandate.json"


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".%s.tmp" % uuid.uuid4().hex[:8])
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _contract_file(job_dir: Path) -> Path:
    return Path(job_dir) / "contract" / "done.json"


def path_for(job_dir: Path) -> Path:
    return Path(job_dir) / "contract" / NAME


def _scope_of(job_dir: Path) -> list:
    j = _read(Path(job_dir) / "job.json")
    sc = j.get("scope")
    if isinstance(sc, list):
        return sorted({str(x) for x in sc})
    return sorted({str(sc)}) if sc else []


def _contract_sha(job_dir: Path) -> str:
    try:
        raw = _contract_file(job_dir).read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(raw).hexdigest()


def policy_version(job_dir: Path) -> str:
    """"批准的是哪一份"的指纹：契约条目 + 范围，一起算。

    ★ 为什么不直接用 `done.lock` 里的 sha256：那只覆盖契约文件本身。
      范围变了而契约没变时，那份 sha 一动不动 —— 但它恰恰是需要重新批的情形。
    """
    obj = _read(_contract_file(job_dir))
    payload = {"done": obj.get("done") or [], "scope": _scope_of(job_dir)}
    canon = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def issue(job_dir: Path, strength: str = "explicit", by: str = "") -> dict:
    """签发一张授权凭证。返回 {ok, path, mandate}。"""
    job_dir = Path(job_dir)
    if not _contract_file(job_dir).exists():
        return {"ok": False, "error": "没有契约，不能签发授权"}
    strength = strength if strength in ("explicit", "inferred") else "inferred"
    rec = {"policy_version": policy_version(job_dir),
           "contract_sha": _contract_sha(job_dir),
           "scope": _scope_of(job_dir),
           "strength": strength,
           "by": by or "",
           "created_at": _now()}
    try:
        _write(path_for(job_dir), rec)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(path_for(job_dir)), "mandate": rec}


def check(job_dir: Path) -> dict:
    """续跑前的重新校验。返回 {ok, missing?, reasons, notes, mandate, current}。"""
    job_dir = Path(job_dir)
    m = _read(path_for(job_dir))
    if not m:
        return {"ok": False, "missing": True, "reasons": ["没有授权记录"],
                "notes": [], "mandate": {}, "current": {}}
    cur = {"policy_version": policy_version(job_dir),
           "contract_sha": _contract_sha(job_dir),
           "scope": _scope_of(job_dir)}
    reasons, notes = [], []
    if cur["contract_sha"] != str(m.get("contract_sha") or ""):
        reasons.append("契约内容在授权之后变过")
    old_scope = set(m.get("scope") or [])
    new_scope = set(cur["scope"])
    grew = sorted(new_scope - old_scope)
    shrank = sorted(old_scope - new_scope)
    # ★ 范围单独判，不靠 policy_version：它把 scope 也哈希进去了，
    #   于是"范围缩小"也会让它对不上 —— 而缩小只是少做，不该拦（实测踩到）。
    if grew:
        reasons.append("范围扩大了：%s" % "、".join(grew[:5]))
    if shrank:
        notes.append("范围缩小了：%s（少做不需要重新授权）" % "、".join(shrank[:5]))
    return {"ok": not reasons, "reasons": reasons, "notes": notes,
            "mandate": m, "current": cur}


def describe(m: dict) -> str:
    if not m:
        return "（没有授权凭证）"
    return ("%s 签发 · %s · scope %d 条 · policy %s…"
            % (m.get("created_at") or "?", m.get("strength") or "?",
               len(m.get("scope") or []), str(m.get("policy_version") or "")[:12]))
