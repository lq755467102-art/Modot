#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sandbox.py —— 给"能动手"的子 agent 一间隔离的房间。

它解决什么
    只读子 agent 永远只能"告诉我"，不能"替我改"。而让它改主工作区是不行的：
    它可能改错，而主工作区里有人正在改的东西。
    所以要一间**副本房**：它在那里面随便改，改完只把 diff 交出来，人看过再决定要不要。

★ 为什么不是 `git worktree`（这条是实测后的结论，别再推翻）
    Claude Code 的 `isolation: worktree` 是 git worktree —— 干净、便宜，但它检出的是
    **最后一次提交**。而现实里工作区几乎总有未提交的改动：
    我们自己的仓库现在就有 14 个改动未提交，其中 `loop/agents.py` 是刚写的、一次都没提交过。
    用 worktree 的话，派个 worker 去"接着改 agents.py"，它在里面根本找不到这个文件。
    那不是一个技术细节，那是最常见的用法直接不成立。
    ⇒ 所以这里用**目录副本**：拷的是"现在这个工作区"，包含未提交改动和未跟踪文件。
      代价是每次派活要拷一份（排除大目录后几 MB，可以接受）。

★ 它**不**做的事
    不自动合并。diff 交出来就结束，合不合、怎么合是人决定的。
    "改对了自动合"听起来省事，实际是把"谁批准了这次改动"这个问题悄悄删掉了。
"""
# 主要引用方包括 `subagent.py` 和 `bin/review.py`。
from __future__ import annotations

import difflib
import os
import shutil
from pathlib import Path

from . import settings as SET

# 不拷的东西：要么大，要么是缓存，要么是另一套数据。
# ★ `runs` 是会话记录，几十上百 MB，而且子 agent 改它没意义。
#   `node_modules` 更大。`.git` 拷过去会让子 agent 的 run_check 里出现另一个仓库的 git 状态。
# ★ `.cache` / `code-index` / `_trash` 是 2026-10-28 补的：实测一个副本 196MB，
#   其中 **158MB 是 `desktop/.cache/electron.zip`**（一个可重新下载的安装包），
#   26MB 是代码索引库（重建只要几秒）。它们既不是源码、也不是这一轮要审的东西，
#   拷过去只是把"每轮一次的大拷贝"变成"每轮一次的磁盘压力"。
SKIP_DIRS = {".git", "runs", "node_modules", "__pycache__", ".workbuddy",
             "dist", "build", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             ".cache", "code-index", "_trash"}

# 凭据类文件**一律不拷**（2026-10-28 实测发现的真问题）：
#   仓库里躺着一个旧的 `runs/benni-box/`，193MB，里面有一份 `api-token` ——
#   副本是"交给本我动手的工作室"，而它会被人 `--keep-box` 留下、
#   被谁打包、被上传，那时钥匙就跟着走了。
#   本我也不需要真钥匙：它要验的是行为，不是凭据。
SKIP_FILES = {"api-token", "credentials.env", ".env", ".env.local",
              "id_rsa", "id_ed25519"}
SKIP_SUFFIX = (".key", ".pem", ".pfx", ".p12")

# 上限：不是为省磁盘，是防"派个活把机器卡死"。
# ★ 默认值在这儿，实际生效值从 `harness/charter.json` 的 `sandbox` 段读。
MAX_FILES = SET.DEFAULT_SANDBOX["max_files"]
MAX_BYTES = SET.DEFAULT_SANDBOX["max_bytes"]


def _limits() -> tuple[int, int, list]:
    c = SET.sandbox()
    return (int(c.get("max_files") or MAX_FILES),
            int(c.get("max_bytes") or MAX_BYTES),
            [str(x) for x in (c.get("only") or [])])


def _walk_roots(src: Path, only: list) -> list:
    """要拷的根。`only` 为空＝整个工作区；非空＝只那几个子树。

    ★ 为什么要有 `only`（2026-10-26）：全量拷一份仓库，在几十万文件的仓库上
      光复制就要几十秒 —— 派活的开销会超过活本身。
      而多数派活只碰一两个目录，没必要把整个仓库搬过去。
    ★ `only` 一旦生效，scan / create / **diff** 三处必须用同一份根，
      否则 diff 会把"没拷过来的目录"当成"它删了"。
    """
    if not only:
        return [src]
    # ★ 两边都要 resolve 之后再比（2026-10-26 实测）：
    #   `tempfile.mkdtemp()` 在 Windows 上给的是 8.3 短名（ADMINI~1），
    #   而 `.resolve()` 会展开成 Administrator —— 于是 `relative_to(src)` 失败，
    #   这里就**静默回退成"拷整个工作区"**，配置看着生效了，其实没生效。
    try:
        base_src = Path(src).resolve()
    except OSError:
        base_src = Path(src)
    out = []
    for sub in only:
        p = (base_src / sub).resolve()
        try:
            p.relative_to(base_src)     # 不许跑出工作区
        except ValueError:
            continue
        if p.exists():
            out.append(p)
    return out or [src]

# diff 的行数上限：父的上下文要装得下
MAX_DIFF_LINES = 800


class SandboxError(RuntimeError):
    pass


def _scan(src: Path, only: list) -> tuple[int, int]:
    """先数一遍：超限就别开拷（拷到一半失败更难收拾）。"""
    n = 0
    size = 0
    for base in _walk_roots(src, only):
        if base.is_file():
            n += 1
            try:
                size += base.stat().st_size
            except OSError:
                pass
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                # 跟拷贝用同一套排除规则，否则"数的时候算了、拷的时候没拷"，
                # 容量报出来比实际大，看着像超限。
                if f in SKIP_FILES or f.endswith(SKIP_SUFFIX):
                    continue
                n += 1
                try:
                    size += (Path(root) / f).stat().st_size
                except OSError:
                    pass
                if n > _limits()[0] or size > _limits()[1]:
                    return n, size
    return n, size


def create(src: Path, dst: Path) -> dict:
    """把当前工作区拷一份到 dst。返回 {files, bytes, root}。"""
    src = Path(src).resolve()
    dst = Path(dst)
    if not src.is_dir():
        raise SandboxError("工作区不存在：%s" % src)
    cap_n, cap_b, only = _limits()
    n, size = _scan(src, only)
    if n > cap_n or size > cap_b:
        raise SandboxError(
            "工作区太大（%d 个文件 / %.0f MB，排除 %s 之后仍然超限），"
            "没开隔离副本 —— 换个目录派活，或者先在配置里把这些目录排除掉"
            % (n, size / 1024 / 1024, "、".join(sorted(SKIP_DIRS))))
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    ig = shutil.ignore_patterns(*SKIP_DIRS, *SKIP_FILES,
                                *["*" + s for s in SKIP_SUFFIX])
    for base in _walk_roots(src, only):
        rel = base.relative_to(src) if base != src else Path(".")
        target = dst if rel == Path(".") else dst / rel
        if base.is_dir():
            shutil.copytree(base, target, ignore=ig, symlinks=True,
                            dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base, target)
    return {"files": n, "bytes": size, "root": str(dst), "only": only}


def _text_of(p: Path) -> str | None:
    """能当文本读就返回内容，否则 None（二进制、太大、读不了）。"""
    try:
        if p.stat().st_size > 2 * 1024 * 1024:
            return None
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def diff(src: Path, dst: Path) -> dict:
    """比两棵树。返回 {text, stat, files}。

    stat = {added, modified, removed}，给卡片写"改了 3 个文件 / +42 −7"。
    """
    # ★ 两端都 resolve：短名 vs 长名混着比，`relative_to` 会抛，key 也对不上。
    src, dst = Path(src).resolve(), Path(dst).resolve()
    lines: list[str] = []
    added = modified = removed = 0
    plus = minus = 0

    def rel(p: Path, base: Path) -> str:
        return str(p.relative_to(base)).replace("\\", "/")

    old = {}
    _, _, only = _limits()
    # ★ old 也要按 `only` 收紧：只拷了某几个目录，diff 却拿整个 src 去比，
    #   会把"没拷过来的"全判成删除 —— 那是假消息，而且看起来很吓人。
    for base in _walk_roots(src, only):
        if base.is_file():
            old[rel(base, src)] = base
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                p = Path(root) / f
                old[rel(p, src)] = p
    new = {}
    for root, dirs, files in os.walk(dst):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            p = Path(root) / f
            new[rel(p, dst)] = p

    for name in sorted(set(old) | set(new)):
        a, b = old.get(name), new.get(name)
        if a is None:
            added += 1
            ta = _text_of(b)
            if ta is None:
                lines.append("~ %s（新增二进制文件，diff 里没法带内容）" % name)
                continue
            body = ta.splitlines()
            # ★ 新增文件也必须走 unified_diff：光把行前面加 `+` **不是** diff，
            #   `git apply` 读不出文件名和行号范围，咬不动（第一版就这么写的）。
            for ln in difflib.unified_diff([], body, fromfile="/dev/null",
                                           tofile="b/" + name, lineterm="", n=3):
                lines.append(ln)
                if ln.startswith("+") and not ln.startswith("+++"):
                    plus += 1
            continue
        if b is None:
            removed += 1
            ta = _text_of(a)
            if ta is None:
                lines.append("~ %s（删除二进制文件）" % name)
                continue
            for ln in difflib.unified_diff(ta.splitlines(), [], fromfile="a/" + name,
                                           tofile="/dev/null", lineterm="", n=3):
                lines.append(ln)
                if ln.startswith("-") and not ln.startswith("---"):
                    minus += 1
            continue
        ta, tb = _text_of(a), _text_of(b)
        if ta is None or tb is None:
            try:
                same = a.read_bytes() == b.read_bytes()
            except OSError:
                same = True
            if not same:
                modified += 1
                lines.append("~ %s（二进制文件有变化）" % name)
            continue
        if ta == tb:
            continue
        modified += 1
        d = list(difflib.unified_diff(ta.splitlines(), tb.splitlines(),
                                      fromfile="a/" + name, tofile="b/" + name,
                                      lineterm="", n=3))
        for ln in d:
            lines.append(ln)
            if ln.startswith("+") and not ln.startswith("+++"):
                plus += 1
            elif ln.startswith("-") and not ln.startswith("---"):
                minus += 1

    cut = False
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES]
        cut = True
    text = "\n".join(lines)
    # ★ 最后一行必须有换行：`git apply` 遇到"最后一行没换行"的补丁，报的是
    #   `corrupt patch at <stdin>:16` —— 那条报错完全没提换行，很容易查错方向（实测过）。
    if text and not text.endswith("\n"):
        text += "\n"
    if cut:
        text += "…（diff 超过 %d 行，后面截掉了）\n" % MAX_DIFF_LINES
    return {"text": text,
            "stat": {"added": added, "modified": modified, "removed": removed,
                     "plus": plus, "minus": minus},
            "files": sorted(set(new) - set(old)) + sorted(
                n for n in set(old) & set(new) if old[n] != new[n])}


def remove(dst: Path) -> None:
    """用完就别留着 —— 一个副本几 MB，留着几十个就是几百 MB 的垃圾。"""
    shutil.rmtree(Path(dst), ignore_errors=True)
