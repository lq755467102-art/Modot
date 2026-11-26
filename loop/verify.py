#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证器 —— 「这一轮到底算不算做完」由谁判

两种判据，**不是二选一的口味问题，是真判据和代理指标的差别**：

    verify_by_contract   跑 done.json 里逐条 check。判的是「做对没有」。
    verify_by_proxy      数读过几个文件、写出多少字。判的是「做了多少」。

★ 有真判据的时候还用代理指标，就是降级。
  所以 `run-task` 的规则是：**给了契约就用契约，没给才退回代理指标**，并且明说用的是哪个。
  "这次按什么判完成"不能靠猜 —— 猜错的代价是它做了一堆事、却没人知道算不算完。

一个不显眼但重要的返回值：`blocked`
    契约被人改过（哈希对不上）时，`contract.py` 会返回 rc=2。那不是模型的错，
    是人的错。这时候**不该让 agent 继续"努力达标"** —— 它达的标可能是错的，越努力越远。
    所以返回 `blocked: True`，由 loop 走 needs_human 出口，把人叫过来。
"""
# 主要引用方包括 `bin/run-task.py` 和 `bin/accept.py`。
from __future__ import annotations

import json

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import paths as P
APPDIR = P.app_root()              # ★ 程序根：bin/*.py 在这儿，打包后是只读的


def verify_by_contract(contract_dir, work_root, py: str | None = None) -> dict:
    """用契约判完成。调的是 `bin/contract.py run`，不在这里重写一遍。

    为什么复用而不是重写：`contract.py run` 会**先查哈希锁再逐条跑 check**。
    重写一份就一定会漏掉"先查锁"这一步，而漏掉它等于契约可以被偷偷改。
    """
    cdir = Path(contract_dir).resolve()
    done = cdir / "done.json"
    if not done.exists():
        return {"ok": False, "blocked": True,
                "reason": "契约目录里没有 done.json：%s\n"
                          "⇒ 这是配置问题，不是你的问题。" % cdir}

    # ★ 顺带取一份**逐条**结果（2026-10-28）：纪要里的规划者要挑"这一轮先做哪几条"，
    #   这需要知道上一轮到底哪几条没过。原来只有一段 reason 文本 ——
    #   从人看的输出里往回抠 `[r2] rc=3` 是能抠，但那种解析会在改一次措辞后静默失效，
    #   而失效的样子是"规划者以为全过了"，比报错难查得多。
    #   ★ 用临时文件：放进契约目录会改到 job 的指纹与改动清单。
    _tmp = ""
    try:
        _fd, _tmp = tempfile.mkstemp(prefix="verify-res-", suffix=".json")
        os.close(_fd)
    except OSError:
        _tmp = ""

    _cmd = [py or sys.executable, str(APPDIR / "bin" / "contract.py"),
            "run", "--dir", str(cdir)]
    if _tmp:
        _cmd += ["--result", _tmp]
    r = subprocess.run(_cmd, cwd=str(work_root), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    per = []
    if _tmp:
        try:
            per = json.loads(Path(_tmp).read_text(encoding="utf-8")).get("items") or []
        except Exception:
            per = []
        try:
            os.unlink(_tmp)
        except OSError:
            pass

    if r.returncode == 0:
        # ★ 通过时也要说清"过了几条、过的是哪几条"（2026-10-28）。
        #   原来返回 `{"ok": True, "reason": ""}` —— 跑没跑、跑了几条，全都看不见。
        #   而"判据过了"如果只用一个 true 表示，人没法复核它是不是真跑了：
        #   **一个空契约也是 0 条全过**。这两件事必须分得开。
        n, claims = 0, []
        try:
            obj = json.loads(done.read_text(encoding="utf-8"))
            for it in (obj.get("done") or []):
                if isinstance(it, dict) and it.get("claim"):
                    claims.append(str(it["claim"])[:40])
            n = len(obj.get("done") or [])
        except Exception:
            pass
        if not n:
            said = "契约里 **0 条判据** —— 等于没验（不然会以为「全过」）"
        else:
            said = "契约 %d 条判据全过" % n
            if claims:
                said += "：" + "、".join(claims[:6])
        return {"ok": True, "reason": said,
                "contract": {"total": n, "claims": claims,
                             # ★ None = 没拿到逐条（不是"0 条"）。这两种必须分得开，
                             #   否则下一步（规划者挑聚焦）会把"没拿到"当成"全过了"。
                             "items": (per or None)}}

    tail = [x for x in ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
            if x.strip()][-6:]
    body = "\n".join(tail)
    if r.returncode == 2:
        return {"ok": False, "blocked": True,
                "reason": "契约不可信（contract.py rc=2）：\n%s\n"
                          "⇒ 这是人的问题，不是你的问题；别去改契约，也别硬凑。" % body}
    if r.returncode == 4:
        return {"ok": False, "blocked": True,
                "reason": "契约跑不起来（contract.py rc=4，多半是命令写错）：\n%s\n"
                          "⇒ 这是写契约的人写错了，不是你的问题。" % body}
    return {"ok": False, "reason": "契约没过（contract.py rc=%d）：\n%s"
                                   % (r.returncode, body),
            # ★ 逐条结果一起带出去（2026-10-28）：没过的时候**更**需要它 ——
            #   纪要里的规划者要靠它知道"哪几条还没过"，不然它只能瞎猜。
            "contract": {"total": len(per), "items": (per or None)}}


def verify_by_proxy(actions: list[dict], out_path, min_reads: int = 0,
                    min_chars: int = 0) -> dict:
    """代理指标：数读过几个**不同的**文件、写出多少字。

    ★ 数"不同的文件"而不是"调用次数" —— 这是自我在自述里自己挑出来的洞：
      原来只数次数，「把 README 读四遍」就能满足"读过 4 个文件"。
    """
    reads = [x for x in actions
             if x.get("act") == "tool" and x.get("tool") == "read_file"]
    files = {x.get("path") for x in reads if x.get("path")}
    if len(files) < min_reads:
        return {"ok": False,
                "reason": "只读过 %d 个不同的文件（共 %d 次 read_file），至少要 %d 个 —— "
                          "同一份读十遍不算读过十个" % (len(files), len(reads), min_reads)}
    p = Path(out_path)
    if not p.exists():
        return {"ok": False, "reason": "还没有把东西写进 %s" % p}
    n = len(p.read_text(encoding="utf-8", errors="replace"))
    if n < min_chars:
        return {"ok": False,
                "reason": "%s 只有 %d 字，至少要有 %d 字" % (p, n, min_chars)}
    return {"ok": True, "reason": ""}
