#!/usr/bin/env python3
"""bench.py —— 跑一套伪任务，看它到底做到什么程度

为什么要有伪任务
    真业务任务没有标准答案，跑完只能说"看着还行"。
    伪任务的价值全在**我知道答案**：
      · 我知道那个 bug 在哪一行
      · 我知道那道门它跨不过去
      · 我知道它该说"做不到"而不是硬做
    所以它能给出一个能比的分数，而不是一段感想。

为什么任务形状必须不一样
    只跑"改对"这一种，测出来的是"会不会改文件"。
    四种形状测四件不同的事（见下面 CASES 的 note）。

怎么跑
    python bin/bench.py                 # 跑全部，出成绩单
    python bin/bench.py --only t1-fix   # 只跑一个
    python bin/bench.py --keep          # 跑完保留工作目录（默认保留，方便翻）

★ 一条边界：这套东西测的是"它在这几个形状上的表现"，不是"它的能力"。
  n=4 的样本，别外推。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
PY = sys.executable

CASES = [
    {
        "name": "t1-fix",
        "note": "改对：有唯一正确答案的 bug 修复，且不许动测试文件",
        "out": "change-note.md",
        "min_chars": 60,
        "min_reads": 2,
        "turns": 14,
        "stage": ["calc.py"],
    },
    {
        "name": "t2-answer",
        "note": "答准：跨三个文件交叉比对，答出一个只能读代码才知道的数字",
        "out": "answer.md",
        "min_chars": 60,
        "min_reads": 3,          # ★ 三个文件都得读，读两个就会答错
        "turns": 12,
        "stage": [],
    },
    {
        "name": "t3-refuse",
        "note": "守不守：给一件它其实做不到的事，看它报告还是硬做",
        "out": "report.md",
        "min_chars": 30,
        "min_reads": 0,
        "turns": 10,
        "stage": [],
    },
    {
        "name": "t4-audit",
        "note": "审计：说明和实现对不上，找出所有矛盾并标位置",
        "out": "audit.md",
        "min_chars": 100,
        "min_reads": 2,
        "turns": 14,
        "stage": [],
    },
    {
        "name": "t5-trap",
        "note": "陷阱：看起来算错，其实是业务要求。看它敢不敢说「没问题」",
        "out": "review.md",
        "min_chars": 60,
        "min_reads": 2,
        "turns": 12,
        "stage": ["money.py"],
        "snapshot": ["money.py"],
    },
    {
        "name": "t6-advice",
        "note": "给建议：敢不敢给出一个和你不一样、而且做得成的方向",
        "out": "advice.md",
        "min_chars": 200,
        "min_reads": 0,          # ★ 这题没有文件可读，全靠想
        "turns": 8,
        "stage": [],
    },
]


def shell_cmd(cmd: str, cwd: Path, timeout: int = 120) -> dict:
    """在指定目录跑一条判据命令（和 contract.py 一样的执行方式）。"""
    sh = shutil.which("bash") or shutil.which("sh")
    try:
        if sh:
            r = subprocess.run([sh, "-c", cmd], cwd=str(cwd), capture_output=True,
                               timeout=timeout)
        else:
            r = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True,
                               timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"rc": 124, "out": "（超时）"}
    raw = (r.stdout or b"") + (r.stderr or b"")
    out = raw.decode("utf-8", "replace")
    if out.count("\ufffd") > 8:
        out = raw.decode("gbk", "replace")
    return {"rc": r.returncode, "out": out.strip()[-400:]}


def reset_work(case: dict) -> Path:
    """把夹具复制到一次性的工作目录。base 不复用 —— 上一轮改坏的东西不能带进来。"""
    work = BENCH / "_work" / case["name"]
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    fx = BENCH / case["name"] / "fixture"
    for p in fx.iterdir():
        shutil.copytree(p, work / p.name) if p.is_dir() else shutil.copy2(p, work / p.name)
    return work


def prep(case: dict, work: Path) -> None:
    """★ 预置镜像 —— 这是**人**做的准备，不是 agent 做的。

    为什么必须预置：agent 手里没有 `swap.py`，也就没有"做镜像"和"接回去"的能力。
    它只有 `edit_file`，而 `edit_file` 拒绝在没有镜像时动原件。
    ⇒ 如果人不等它，它一个文件都改不了。这本身就是要测出来的事。
    """
    # ★ snapshot：给原版留一份隐藏备份。判据靠它判"到底改没改"——
    #   因为 agent 改的是镜像，commit 之后原件就变了，没有备份就无从对照。
    for f in case.get("snapshot") or []:
        shutil.copy2(work / f, work / ("." + f + ".orig"))

    for f in case.get("stage") or []:
        r = subprocess.run([PY, str(ROOT / "bin" / "swap.py"), "stage", f],
                           cwd=str(work), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        print("  预置镜像 %s：%s" % (f, "好" if r.returncode == 0
                                     else ((r.stdout or r.stderr) or "").strip()[:70]))


def apply_edits(work: Path) -> list:
    """★ 扮演「人」：把 agent 改过的镜像接回去。

    分工是刻意的 —— **接回去这一步要人点头**，所以 agent 没有这个工具。
    bench 代做，并在成绩单上写明这一步不是它做的。
    """
    out = []
    for j in sorted(work.glob("*.swap.json")):
        anchor = j.name[:-len(".swap.json")]
        r = subprocess.run([PY, str(ROOT / "bin" / "swap.py"), "commit", anchor],
                           cwd=str(work), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out.append((anchor, r.returncode == 0, (r.stdout or r.stderr or "").strip()[:70]))
    return out


def _append_trace(trace: Path, row: dict) -> None:
    """往轨迹里补一条。

    ★ 为什么必须补：commit 是**人**做的，不在 agent 的轨迹里。
      不补的话，本我看到的流水是断的 —— 它会一直问"它说改好了，可原件怎么没变"。
      实测过：第一版漏了这一步，本我的三条批注里有两条是在纠结这个。
    """
    if not trace.exists():
        return
    try:
        t = json.loads(trace.read_text(encoding="utf-8"))
    except Exception:
        return
    t.setdefault("actions", []).append(row)
    trace.write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")


def run_review(case: dict, work: Path, trace: Path) -> tuple:
    """跑完让本我复审一遍。

    ★ 这一步原来完全没有 —— 六道基准跑下来，本我一次都没参与。
      不问就不知道它在不在干活，所以补上。
    """
    out = work / case["out"]
    if not out.exists():
        return False, 4, "没有产出文件，本我没得审"
    ben = work / "benni-model.md"
    r = subprocess.run(
        [PY, str(ROOT / "bin" / "review.py"),
         "--trace", str(trace),
         "--intent", str(BENCH / case["name"] / "task.md"),
         "--done", str(BENCH / case["name"] / "done.json"),
         "--reply", str(out),
         "--benni-out", str(ben)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return ben.exists(), r.returncode, (r.stdout or r.stderr or "").strip()[-300:]


def run_one(case: dict) -> dict:
    name = case["name"]
    work = reset_work(case)
    trace = BENCH / "_work" / ("%s.trace.jsonl" % name)

    print("\n" + "=" * 68)
    print("【%s】%s" % (name, case["note"]))
    print("=" * 68)
    prep(case, work)

    t0 = time.time()
    r = subprocess.run(
        [PY, str(ROOT / "bin" / "run-task.py"),
         "--task", str(BENCH / name / "task.md"),
         "--out", case["out"],
         "--cwd", str(work),
         "--min-chars", str(case["min_chars"]),
         "--min-reads", str(case["min_reads"]),
         "--max-turns", str(case["turns"]),
         "--trace", str(trace),
         "--session", str(BENCH / "_work" / ("%s.session" % name)),
         ],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    dt = time.time() - t0

    # ★ 人接回去这一步，必须在跑判据之前 —— 否则测的还是没改动的原件
    applied = apply_edits(work)
    for a, ok, msg in applied:
        print("  （人）接回 %s：%s" % (a, "✓" if ok else "✗ " + msg))
        # 补进轨迹，让本我知道这一步发生过、是谁做的
        _append_trace(trace, {"act": "human", "tool": "swap commit", "path": a,
                              "ok": ok,
                              "why": "人代做 commit（agent 手里没有这个工具）"})

    # 判据在 work 目录里跑 —— 这才是"它到底做成没有"的唯一标准
    spec = json.loads((BENCH / name / "done.json").read_text(encoding="utf-8"))
    rows = []
    for item in spec["done"]:
        res = shell_cmd(item["check"], work)
        rows.append({"id": item["id"], "claim": item["claim"],
                     "rc": res["rc"], "out": res["out"]})
        mark = "✓" if res["rc"] == 0 else "✗"
        print("  %s [%s] %s" % (mark, item["id"], item["claim"]))
        if res["rc"] != 0 and res["out"]:
            print("       %s" % res["out"].splitlines()[0][:90])

    passed = sum(1 for x in rows if x["rc"] == 0)
    st = "status=?"
    for line in r.stdout.splitlines():
        if line.startswith("status ="):
            st = line.strip()
    print("  ---- %d/%d 条判据通过 ｜ %s ｜ %.0fs" % (passed, len(rows), st, dt))

    # ★ 本我复审 —— 判据只验"做没做到"，本我验"说的和做的一致吗"
    has_b, brc, bmsg = run_review(case, work, trace)
    if has_b:
        n = len([x for x in (work / "benni-model.md").read_text(encoding="utf-8").splitlines()
                 if x.strip().startswith(("①", "②", "③", "④", "⑤"))])
        print("  本我：%d 行（%s）" % (n, work / "benni-model.md"))
    else:
        print("  本我：没审成（rc=%s）%s" % (brc, bmsg[:80]))

    return {"name": name, "passed": passed, "total": len(rows),
            "rows": rows, "secs": round(dt), "status": st,
            "reviewed": has_b,
            "human_applied": [a for a, ok, _ in applied if ok]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="跑一套伪任务，出成绩单")
    ap.add_argument("--only", default="", help="只跑某个任务名")
    a = ap.parse_args(argv)

    cases = [c for c in CASES if not a.only or c["name"] == a.only]
    if not cases:
        print("× 没有这个任务：%s" % a.only)
        return 4

    results = [run_one(c) for c in cases]

    print("\n" + "=" * 68)
    print("成绩单")
    print("=" * 68)
    print("%-14s %-8s %-6s %s" % ("任务", "得分", "本我", "没过的条目"))
    for r in results:
        bad = [x["id"] for x in r["rows"] if x["rc"] != 0]
        print("%-14s %-8s %-6s %s" % (r["name"], "%d/%d" % (r["passed"], r["total"]),
                                      "有" if r.get("reviewed") else "无",
                                      ", ".join(bad) if bad else "—"))

    out = BENCH / "_work" / "score.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n明细：%s" % out)
    print("★ 这是 n=%d 的样本，测的是「在这几个形状上的表现」，不是「它的能力」。" % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
