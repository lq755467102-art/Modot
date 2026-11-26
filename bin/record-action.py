#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""record-action.py —— 钩子：把每次工具调用追加进 actions-YYYY-MM-DD.jsonl

用途
    这是双 harness 的**根**。本我的全部输入都从这份流水来；
    流水只要是人手写的，后面所有环节都是演。

为什么要做成钩子而不是"我自己记得写"
    因为"靠自觉的动作不算数" —— 这是 2026-10-22 一整天的结论。
    钩子由平台在每次工具调用后触发，不依赖我记性。

设计约束（每条都有来源）
    1. **fail open**：这是日志，不是护栏。任何异常都必须吞掉、退出 0。
       反例：Claude 的实测发现策略钩子抛错会被静默吞掉 ⇒ 策略没执行却没人知道。
       所以这里反过来：**明确决定"出错就放行"，而不是继承默认行为。**
    2. **不往 stdout 打印**：钩子的 stdout 可能被当作返回内容。
    3. **单条要短**：输出截断到 --max-chars，否则一天下来流水读不动。
    4. **只追加**：绝不修改已有行。本我靠"读前 N 行"做快照（FRAMEWORK.md §2）。

用法（注册到钩子时）
    python <此文件>            # stdin 读钩子 JSON
    echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' | python record-action.py --dry

输出位置
    $WB_AGENT_RUN_DIR/actions-YYYY-MM-DD.jsonl
    默认 = <cwd>/.workbuddy/agent-run/
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

MAX_CHARS = 400


def _flat(v, limit: int = MAX_CHARS) -> str:
    """把任意值压成单行短字符串。"""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            s = json.dumps(v, ensure_ascii=False)
        except Exception:
            s = str(v)
    else:
        s = str(v)
    s = " ".join(s.split())
    return s[:limit] + ("…" if len(s) > limit else "")


def _run_dir() -> Path:
    """流水写在哪 —— **只按脚本自身位置推，绝不看 cwd**。

    ★ 2026-10-23 实盘教训：钩子载荷里的 `cwd` 是**会话工作区**
      （`.../WorkBuddy/2026-10-11-15-31-56`），不是项目根。
      原来按 cwd 向上找 `.workbuddy` ⇒ 记录被写进了**另一个工作区**，
      而本目录的流水看起来"一条没记" —— 而且不报错。
      这正是自己在 `runs/_good.md` ③ 里预判过的那个 bug，20 分钟后应验。
    """
    env = os.environ.get("WB_AGENT_RUN_DIR")
    if env:
        return Path(env)
    # <...>/agent-run/bin/record-action.py  →  <...>/agent-run
    return Path(__file__).resolve().parent.parent


def _pick(payload: dict, *names, default=None):
    for n in names:
        if n in payload and payload[n] not in (None, "", [], {}):
            return payload[n]
    return default


def build_record(payload: dict) -> dict | None:
    tool = _pick(payload, "tool_name", "tool", "name", default="")
    if not tool:
        # 不是一次工具调用 ⇒ 不写。宁可少记一条，也别往流水里塞垃圾行
        # （流水一脏，本我读到的"事实"就不可信了）
        return None
    tin = _pick(payload, "tool_input", "input", "args", default={})
    tout = _pick(payload, "tool_response", "tool_result", "response", "result", default="")

    # 尽力从工具入参里抽出"这条动作干的是什么"
    cmd = ""
    files: list[str] = []
    if isinstance(tin, dict):
        cmd = _pick(tin, "command", "cmd", "notebook_path", "path", "file_path", default="") or ""
        for k in ("file_path", "path", "notebook_path"):
            v = tin.get(k)
            if isinstance(v, str) and v:
                files.append(os.path.basename(v) or v)
    if not cmd:
        cmd = _flat(tin, 160)

    # 返回码 / 输出。
    # ★ 实盘字段：Bash 的 tool_response 是
    #   {"exitCode":0,"signal":null,"interrupted":false,"sandboxDenied":false,
    #    "stderrBytesTruncated":0,"stdoutBytesTruncated":0,"tool_error_code":"0"}
    #   —— **里面没有 stdout**。所以别再指望从这里拿到命令输出。
    rc = ""
    out = ""
    if isinstance(tout, dict):
        for k in ("exitCode", "returncode", "exit_code", "rc", "code"):
            if k in tout:
                rc = str(tout[k])
                break
        # 只把"异常"写进 out，正常就别塞一坨元数据
        notes = []
        if tout.get("sandboxDenied"):
            notes.append("sandboxDenied")
        if tout.get("interrupted"):
            notes.append("interrupted")
        ec = str(tout.get("tool_error_code") or "")
        if ec and ec not in ("0", "None", ""):
            notes.append("error_code=" + ec)
        if tout.get("signal"):
            notes.append("signal=" + str(tout.get("signal")))
        # 万一某个工具真的把文本放进来了，才用
        parts = [tout.get(k) for k in ("stdout", "stderr") if tout.get(k)]
        if parts:
            notes.append(_flat("\n".join(str(p) for p in parts), 200))
        out = " · ".join(notes)[:240]
    elif isinstance(tout, str):
        out = _flat(tout, 240)

    return {
        "t": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "act": str(tool),
        "cmd": _flat(cmd, 200),
        "rc": rc,          # 空 = 这个工具没有返回码概念（如 Write），不是"未知"
        "out": out,        # ★ 一般拿不到 stdout；只有异常信息或工具自己回传的文本
        "files": files[:6],
        "model": _flat(payload.get("model") or "", 40),
        "agent": _flat(payload.get("agent_type") or "", 20),
        # 这两样是为了**将来能回查真实输出**：载荷里没有 stdout，
        # 但 transcript_path 指向会话记录，可按 tool_use_id 把输出对回来。
        "uid": _flat(payload.get("tool_use_id") or "", 40),
        "src": "live",     # 与事后重建的 recon 区分：只有钩子写出来的才叫 live
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--dry", action="store_true", help="只打印，不落盘")
    ap.add_argument("--run-dir", default="", help="覆盖输出目录")
    ap.add_argument("--probe-once", default="",
                    help="把**第一次**收到的原始载荷存到这个路径（文件已存在就不再写）。"
                         "用途：核实本平台的钩子字段格式，一次就够，不常驻。")
    a = ap.parse_args(argv)

    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    if not raw.strip():
        return 0

    # 一次性探针：核实本平台真实字段名。文件存在就不再写（不会无限长）
    if a.probe_once:
        pp = Path(a.probe_once)
        if not pp.exists():
            try:
                pp.parent.mkdir(parents=True, exist_ok=True)
                pp.write_text(raw[:20000], encoding="utf-8")
            except Exception:
                pass

    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {"raw": payload}
    except Exception:
        payload = {"raw": raw[:2000]}

    rec = build_record(payload)
    if rec is None:
        return 0

    if a.dry:
        sys.stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return 0

    # ★ 硬护栏：**没有 session_id 的载荷一律不落盘**。
    # 理由：2026-10-23 我手工把测试夹具（runs/_t1.json）喂给这条命令的**非 dry** 路径，
    #   结果流水里多了一条"python scripts/deploy-sync.py --apply --forgot · rc=0"，
    #   标着 src=live，与真记录**完全分不开** —— 而那条命令当天从没跑过。
    #   真钩子载荷一定有 session_id（本平台实测 15 个键里就有），夹具没有。
    #   ⇒ 用这个字段当"我是真的钩子调用"的凭据。
    if not payload.get("session_id"):
        return 0

    run_dir = Path(a.run_dir) if a.run_dir else _run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_dir / ("actions-%s.jsonl" % dt.date.today().isoformat())
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # 日志钩子：任何情况下都放行，且不污染 stdout
        sys.exit(0)
