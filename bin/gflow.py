#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全 Git 工作流 —— 让 agent 不用直接拼 rebase / push

它是干嘛的
    多人同时改一个仓库时，最怕的不是冲突，是**分叉长到最后才处理**。
    这个入口把纪律做成三个机械动作：
      start   从最新 main 切出 `agent/<任务>` 分支
      sync    把当前分支 rebase 到最新 main（每天/每次提交前）
      finish  再 rebase 一次 + 跑测试，然后才允许推送

它替人做什么、不替人做什么
    · 它做：fetch、rebase、数领先/落后、列出冲突文件、跑测试。
    · 它不做：**不解冲突**。冲突意味着"两个功能改到一处"，
      选哪边是功能判断，必须人来看 —— 所以冲突时它返回 3 并列出文件，
      绝不自己挑一边。
    · `finish --push` 只允许 `agent/` 前缀的分支，且只用 `--force-with-lease`；
      main / master / develop 一律不许从这里推。

给 agent 的用法（run_script）
    script = "bin/gflow.py"
    args = ["start", "修 subtotal"]
    args = ["sync"]
    args = ["finish", "--test", "python bin/accept.py"]     # 可选
    args = ["finish", "--push"]                            # 推自己的 agent 分支
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SHARED = {"main", "master", "develop", "dev", "release"}


def _git(args, cwd, check=True):
    p = subprocess.run(["git"] + list(args), cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if check and p.returncode != 0:
        raise RuntimeError("git %s 失败：%s" % (" ".join(args), out[-400:]))
    return p.returncode, out


def _branch(cwd) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)[1].strip()


def _dirty(cwd) -> bool:
    return bool(_git(["status", "--porcelain"], cwd)[1].strip())


def _ahead_behind(cwd, base) -> tuple[int, int]:
    behind = int(_git(["rev-list", "--count", "HEAD..%s" % base], cwd)[1] or 0)
    ahead = int(_git(["rev-list", "--count", "%s..HEAD" % base], cwd)[1] or 0)
    return ahead, behind


def _conflicts(cwd) -> list[str]:
    out = _git(["diff", "--name-only", "--diff-filter=U"], cwd, check=False)[1]
    return [x for x in out.splitlines() if x.strip()]


def _need_feature(cwd) -> str:
    b = _branch(cwd)
    if b in SHARED:
        raise RuntimeError("当前在共享分支 `%s` 上 —— 请先用 `start` 切一个 agent 分支" % b)
    return b


def _rebase_or_stop(cwd, base) -> int:
    """rebase 到 base。冲突就停下，**不解**，返回 3。"""
    rc, out = _git(["rebase", base], cwd, check=False)
    if rc == 0:
        return 0
    files = _conflicts(cwd)
    print("✗ rebase 有冲突 —— 这需要人来做功能取舍，我不会替你选。", file=sys.stderr)
    for f in files[:20]:
        print("  · %s" % f, file=sys.stderr)
    print("", file=sys.stderr)
    print("  怎么处理（人来）：", file=sys.stderr)
    print("    ① 逐个文件看两边各改了什么，决定保留哪一版；", file=sys.stderr)
    print("    ② 改完 `git add <文件>`，再 `git rebase --continue`；", file=sys.stderr)
    print("    ③ 想放弃这次同步：`git rebase --abort`。", file=sys.stderr)
    return 3


def cmd_start(a) -> int:
    cwd = Path(a.cwd).resolve()
    _git(["fetch", "origin"], cwd)
    if _dirty(cwd):
        print("✗ 工作区还有未提交的改动 —— 先提交或 stash，再开新分支。", file=sys.stderr)
        return 2
    name = a.name.strip().replace(" ", "-")
    branch = name if name.startswith("agent/") else "agent/" + name
    rc, out = _git(["rev-parse", "--verify", branch], cwd, check=False)
    if rc == 0:
        print("✗ 分支 `%s` 已经存在 —— 换个名字，或先 sync 它。" % branch, file=sys.stderr)
        return 2
    _git(["switch", "-c", branch, a.base], cwd)
    print("✓ 已切到 `%s`（基于 %s）" % (branch, a.base))
    print("  接下来每天/每次提交前跑一次：python bin/gflow.py sync")
    return 0


def cmd_sync(a) -> int:
    cwd = Path(a.cwd).resolve()
    branch = _need_feature(cwd)
    _git(["fetch", "origin"], cwd)
    ahead, behind = _ahead_behind(cwd, a.base)
    if behind == 0:
        print("✓ `%s` 已经基于最新 %s（领先 %d 个提交）" % (branch, a.base, ahead))
        return 0
    print("同步：把 `%s` rebase 到 %s（当前落后 %d、领先 %d）"
          % (branch, a.base, behind, ahead), flush=True)
    rc = _rebase_or_stop(cwd, a.base)
    if rc:
        return rc
    print("✓ 已同步到最新 %s —— 冲突是在这次小步里出的，比攒到最后好解" % a.base)
    return 0


def cmd_finish(a) -> int:
    cwd = Path(a.cwd).resolve()
    branch = _need_feature(cwd)
    _git(["fetch", "origin"], cwd)
    print("收尾：先同步到最新 %s" % a.base, flush=True)
    rc = _rebase_or_stop(cwd, a.base)
    if rc:
        return rc
    if a.test:
        print("跑测试：%s" % a.test, flush=True)
        p = subprocess.run(a.test, cwd=str(cwd), shell=True)
        if p.returncode != 0:
            print("✗ 测试没过（rc=%d）—— 不能推送/合并。" % p.returncode, file=sys.stderr)
            return 4
        print("✓ 测试通过")
    else:
        print("（没给 --test，所以这次没有自动验证 —— 合并前请自己确认功能没坏）")
    if a.push:
        if not branch.startswith("agent/"):
            print("✗ 只允许推送 `agent/` 前缀的分支，当前是 `%s`。" % branch, file=sys.stderr)
            return 2
        rc, out = _git(["push", "--force-with-lease", "origin", branch], cwd, check=False)
        if rc != 0:
            print("✗ 推送被拒：%s" % out[-400:], file=sys.stderr)
            print("  共享分支一律不从这里推；自己的分支如果别人也动过，先 sync。", file=sys.stderr)
            return 5
        print("✓ 已推送 `%s`（--force-with-lease）" % branch)
    else:
        print("✓ 可以合并了。要推送自己的分支：python bin/gflow.py finish --push")
    ahead, behind = _ahead_behind(cwd, a.base)
    print("  现在：领先 %d、落后 %d（%s）" % (ahead, behind, a.base))
    return 0


def cmd_templates(a) -> int:
    """把可配置的 CODEOWNERS / PR 模板写进 `.github/`。**不覆盖已有文件**。"""
    import json
    cwd = Path(a.cwd).resolve()
    cfg_p = cwd / "harness" / "gitflow.json"
    cfg = {}
    if cfg_p.exists():
        try:
            cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    co = str(cfg.get("codeowners") or
             "# 目录级负责人：谁最懂这块，谁就是这块的默认评审人\n")
    pr = str(cfg.get("pr_template") or
             "## 这次改了什么\n\n## 什么时候 rebase 到最新 main 的\n\n"
             "## 跑了什么测试（结果）\n\n## 有没有冲突，怎么取舍的\n")
    gh = cwd / ".github"
    gh.mkdir(exist_ok=True)
    for name, content in (("CODEOWNERS", co), ("pull_request_template.md", pr)):
        p = gh / name
        if p.exists() and not a.force:
            print("  已有 %s（不覆盖；要重写加 --force）" % p)
            continue
        p.write_text(content.rstrip("\n") + "\n", encoding="utf-8")
        print("✓ 写好 %s" % p)
    print("  模板来源：harness/gitflow.json（界面里可直接改）")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="安全 Git 工作流：start / sync / finish")
    ap.add_argument("--cwd", default=".", help="仓库目录（默认当前目录）")
    ap.add_argument("--base", default="origin/main", help="同步目标（默认 origin/main）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("start", help="从最新 main 切出 agent 分支")
    p1.add_argument("name", help="任务名，会作为 agent/<任务名> 的分支名")
    p1.set_defaults(fn=cmd_start)
    p2 = sub.add_parser("sync", help="把当前分支 rebase 到最新 main")
    p2.set_defaults(fn=cmd_sync)
    p3 = sub.add_parser("finish", help="收尾：rebase + 测试，可选推送")
    p3.add_argument("--test", default="", help="收尾时要跑的测试命令")
    p3.add_argument("--push", action="store_true", help="推送当前 agent 分支")
    p3.set_defaults(fn=cmd_finish)
    p4 = sub.add_parser("templates", help="把可配置的 CODEOWNERS / PR 模板写进 .github/")
    p4.add_argument("--force", action="store_true", help="覆盖已有文件")
    p4.set_defaults(fn=cmd_templates)
    a = ap.parse_args(argv)
    try:
        return int(a.fn(a))
    except RuntimeError as e:
        print("✗ %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
