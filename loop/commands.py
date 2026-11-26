"""commands.py —— 斜杠命令：把一个短名字展开成一段预设的提示词。

★ 它解决什么
    每次开新会话都要重打一遍"先跑验收、全绿再提交、提交信息写清为什么" ——
    这些话是固定的，但人不愿意每次抄。而且抄的时候会漏一两句，
    漏掉的那句就是下次事故的入口。
    ⇒ 命令 = 一段**写下来、可复用、可见**的提示词，输入 `/commit fix typo` 就展开。

★ 格式（照 Claude Code 的 `.claude/commands/*.md`，不自创）
    文件名就是命令名：`commit.md` → `/commit`
    开头可以有 frontmatter（可选）：

        ---
        description: 一句话说明它做什么（补全列表里显示这句）
        args: 提交信息
        ---

    正文里 `$ARGUMENTS` 会被替换成参数；没有这个占位符时，参数**另起一行追加**。

★ 存哪
    项目级 `<repo>/.harness/commands/`  —— 跟这个项目绑的（比如"跑本仓库验收"）
    用户级 `~/.harness/commands/`       —— 跨项目（比如"写周报"）
    同名时**项目级优先**：项目里那份是针对这个项目改过的。

★ 为什么展开放在**前端**（发送前替换）
    这样输入框里/事件流里存的是**展开后的完整文本**。
    回看历史时能看懂当时到底发出去什么；如果存 `/commit`，三天后没人知道它展开了什么。
"""
# 主要引用方包括 `bin/server.py` 和 `bin/accept.py`。
from __future__ import annotations

import os
import re
from pathlib import Path

# ★ 用 HOME 环境变量的方式拿用户目录 —— 和 skills.py / policy.py 保持一致，
#   三处各算一种"用户目录"就是三个真源。
HOME = Path.home()

# 正文里的参数占位符：两种写法都认（不同厂商用不同写法，两边都收不花钱）
ARG_KEYS = ("$ARGUMENTS", "{{args}}", "{{ARGS}}")


def _norm_meta(text: str) -> tuple[dict, str]:
    """拆 frontmatter。**统一换行**再判 —— Windows 上编辑出来的是 CRLF，
    不统一的话 `startswith('---')` 直接失效，而且不报错（技能那边踩过同一个坑）。"""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    if not t.startswith("---"):
        return {}, t
    end = t.find("\n---", 3)
    if end < 0:
        return {}, t
    head = t[3:end].strip("\n")
    body = t[end + 4:].lstrip("\n")
    meta: dict = {}
    for line in head.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip().lower()
        v = v.strip().strip('"').strip("'")
        if k:
            meta[k] = v
    return meta, body


def roots(repo_root=None) -> list[tuple[str, Path]]:
    """[(层级, 目录)]，用户级在前、项目级在后（后面的覆盖前面的）。"""
    out = [("用户", HOME / ".harness" / "commands")]
    if repo_root:
        out.append(("项目", Path(repo_root) / ".harness" / "commands"))
    return out


def scan(repo_root=None) -> list[dict]:
    """列出所有命令（名字 / 一句说明 / 从哪来）。按名字排序。"""
    found: dict[str, dict] = {}
    for level, d in roots(repo_root):
        if not d.is_dir():
            continue
        try:
            files = sorted(d.glob("*.md"))
        except OSError:
            continue
        for f in files:
            name = f.stem.strip()
            if not name or name.startswith("_"):
                continue        # 下划线开头 = 草稿，不列出来
            try:
                meta, body = _norm_meta(
                    f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            found[name] = {
                "name": name,
                "desc": str(meta.get("description") or "").strip(),
                "args": str(meta.get("args") or "").strip(),
                "level": level,
                "file": str(f),
                # 列表里带正文长度：人一眼知道"这是个短指令还是长流程"
                "chars": len(body.strip()),
            }
    return [found[k] for k in sorted(found)]


def load(name: str, repo_root=None) -> dict:
    """取一个命令的正文。项目级优先。"""
    nm = str(name or "").strip().lstrip("/")
    if not nm:
        return {"ok": False, "error": "没说要哪个命令"}
    # ★ 名字里不许有路径分隔符：`../../etc` 这种顺着 `load` 就能读到仓库外
    if "/" in nm or "\\" in nm or nm.startswith("."):
        return {"ok": False, "error": "命令名不合法：%r" % name}
    hit = None
    for _level, d in roots(repo_root):
        p = d / (nm + ".md")
        if p.is_file():
            hit = p            # 后面的覆盖前面的 ⇒ 项目级胜出
    if hit is None:
        have = [c["name"] for c in scan(repo_root)]
        return {"ok": False, "error": "没有这个命令：%s" % nm,
                "hint": ("有的是：%s" % "、".join(have)) if have
                        else "这个项目还没建过命令（.harness/commands/*.md）"}
    meta, body = _norm_meta(hit.read_text(encoding="utf-8", errors="replace"))
    return {"ok": True, "name": nm, "desc": str(meta.get("description") or ""),
            "args_hint": str(meta.get("args") or ""), "body": body.strip(),
            "file": str(hit)}


def render(name: str, args_text: str = "", repo_root=None) -> dict:
    """把 `/name 参数` 展开成最终要发出去的文本。

    ★ 没有占位符就把参数**另起一行追加**，而不是丢掉 ——
      丢了参数是最糟的一种：命令照跑，但跑的是"没带参数"的版本，
      而人以为带上了。
    """
    r = load(name, repo_root)
    if not r.get("ok"):
        return r
    body = r["body"]
    a = str(args_text or "").strip()
    hit = ""
    for k in ARG_KEYS:
        if k in body:
            hit = k
            break
    if hit:
        text = body.replace(hit, a)
    elif a:
        text = body + "\n\n" + a
    else:
        text = body
    return {"ok": True, "name": r["name"], "text": text.strip(),
            "used_args": bool(a), "had_placeholder": bool(hit)}
