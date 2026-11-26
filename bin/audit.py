#!/usr/bin/env python3
"""audit.py —— 把本我 ③ 里每条「复核：」命令**真的跑一遍**。

解决什么：
    业务方问过一句最狠的：「你本我为了拿高分，是否有意在编造反驳？」

    这个问题靠"我保证没编"是回答不了的。唯一的办法是让每条反驳都带一条**能重跑**的命令，
    再由第三方真的跑它。跑出来的东西如果和批注对不上，那条批注就是编的。

    这套脚本**不判断对错**，只把命令的真实输出摆出来 —— 判对错仍然靠人和检查器。

用法：
    python bin/audit.py runs/render-round/benni.md
    python bin/audit.py <文件> --json

退出码：
    0 全部条目都有复核命令，且都跑成功
    1 有条目缺复核命令，或有命令跑失败（失败本身就是信号）
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

MARKS = ("①", "②", "③", "④", "⑤")

# 允许当作命令首词的内建/常见工具（其余一律不当命令跑，避免把正则当命令）
ALLOW = {
    "printf", "echo", "cd", "test", "true", "false",
    "python", "python3", "py", "node", "grep", "egrep", "rg", "ls", "cat",
    "wc", "git", "find", "sed", "awk", "sort", "head", "tail", "diff", "stat",
}
TIMEOUT = 120


def body_of(text: str, mark: str) -> str:
    """取某一标记的正文（只认行首标记）。"""
    lines = text.split("\n")
    start = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith(mark):
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j].lstrip()
        if any(s.startswith(m) for m in MARKS):
            end = j
            break
    return "\n".join(lines[start + 1:end])


def split_items(body: str) -> list[list[str]]:
    groups: list[list[str]] = []
    for line in body.split("\n"):
        if not line.strip():
            continue
        if re.match(r"^\s*(?:[-·•*]|\d+[.)])\s+", line) or not groups:
            groups.append([line])
        else:
            groups[-1].append(line)
    return groups


def commands_in(item: list[str]) -> list[str]:
    """从「复核：」那一行里取出可以跑的命令（反引号里的、首词认识的）。"""
    line = ""
    for ln in item:
        if re.search(r"复核[：:]", ln):
            line = ln
            break
    if not line:
        return []
    # ★ 先取反引号里的；**没有反引号就取冒号之后整段**。
    #   要求必须带反引号，就是拿标点当结构 —— 模型写成 `复核：grep ...`（无反引号）
    #   内容完全对，却被判"没有可重跑的复核命令"（实测踩过，第三次同类）。
    segs = re.findall(r"`([^`]+)`", line)
    if not segs:
        m = re.search(r"复核[：:]\s*(.+)", line)
        cand = m.group(1).strip() if m else ""
        segs = [s.strip() for s in re.split(r"或|；|;", cand) if s.strip()]
    out = []
    for seg in segs:
        seg = seg.strip()
        try:
            first = shlex.split(seg)[0]
        except (ValueError, IndexError):
            continue
        if first not in ALLOW and not shutil.which(first):
            continue                                  # 像正则，不像命令
        out.append(seg)
    return out


def _shell() -> str | None:
    for c in ("bash", "sh"):
        p = shutil.which(c)
        if p:
            return p
    return None


def run_cmd(cmd: str) -> dict:
    """跑一条复核命令。

    ★ 两个 Windows 坑，都踩过：
      ① `shell=True` 在 Windows 用 cmd.exe，而 **cmd 不把单引号当引号**
        ⇒ `grep -nE '^\\s+(import|from) '` 会被拆成两条命令，报"不是内部或外部命令"。
        改用 bash 执行（本机就有 Git Bash），引号和管道才按作者的意思走。
      ② 输出编码：cmd/Git Bash 混着 GBK 和 UTF-8，固定用 UTF-8 解码会出乱码。
        先按 UTF-8 解，替换字符多了再按 GBK 重解。
    """
    sh = _shell()
    try:
        if sh:
            r = subprocess.run([sh, "-c", cmd], capture_output=True, timeout=TIMEOUT)
        else:
            r = subprocess.run(cmd, shell=True, capture_output=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"cmd": cmd, "rc": 124, "out": "（超时 %d 秒）" % TIMEOUT}
    raw = (r.stdout or b"") + (r.stderr or b"")
    out = raw.decode("utf-8", "replace")
    if out.count("\ufffd") > 8:
        out = raw.decode("gbk", "replace")
    return {"cmd": cmd, "rc": r.returncode, "out": out.strip()[-1200:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    p = Path(a.file)
    if not p.exists():
        print("文件不存在：%s" % p, file=sys.stderr)
        return 2
    text = io.open(p, encoding="utf-8").read()

    body = body_of(text, "③")
    if not body.strip():
        print("③ 是空的 —— 没有可复核的批注。", file=sys.stderr)
        return 2

    groups = split_items(body)
    rows = []
    no_cmd = 0
    failed = 0
    for i, g in enumerate(groups, 1):
        head = re.sub(r"^\s*(?:[-·•*]|\d+[.)])\s*", "", g[0]).strip()
        cmds = commands_in(g)
        is_retract = "撤回" in head
        row = {"n": i, "claim": head[:110], "retracted": is_retract,
               "commands": [], "has_cmd": bool(cmds)}
        if not cmds:
            no_cmd += 1
        for c in cmds:
            res = run_cmd(c)
            row["commands"].append(res)
            # ★ 非 0 退出码不等于失败：grep 没匹配就是 1，那常常正是这条复核要的结果。
            #   真正的"跑不起来"只有这两种（命令不存在 / 不可执行）。
            #   把 rc=1 算成失败，等于把"没找到"报成"出错了"—— 今天第二次栽在同一个坑。
            if res["rc"] in (124, 126, 127):
                failed += 1
        rows.append(row)

    if a.json:
        print(json.dumps({"file": str(p), "items": rows,
                          "missing_command": no_cmd, "failed_commands": failed},
                         ensure_ascii=False, indent=1))
    else:
        print("复核清单：%s" % p)
        print("共 %d 条批注（其中撤回 %d 条）｜缺复核命令 %d 条｜命令跑不起来 %d 次\n"
              % (len(rows), sum(1 for r in rows if r["retracted"]), no_cmd, failed))
        print("（rc 非 0 不算失败：grep 没匹配就是 1，那常常正是要的结果）\n")
        for r in rows:
            tag = "【撤回】" if r["retracted"] else ""
            print("%d. %s%s" % (r["n"], tag, r["claim"]))
            if not r["has_cmd"]:
                print("   ✗ 没有可重跑的复核命令 —— 这条无法验证")
            for c in r["commands"]:
                print("   $ %s" % c["cmd"])
                print("     rc=%d" % c["rc"])
                for ln in (c["out"] or "(无输出)").split("\n"):
                    print("     │ %s" % ln)
            print()

        print("★ 这套脚本不判断对错，只把命令的真实输出摆出来。")
        print("  如果某条批注说的和它自己的复核结果对不上 —— 那条就是编的。")

    return 0 if (no_cmd == 0 and failed == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
