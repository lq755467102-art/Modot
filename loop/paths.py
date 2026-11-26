#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paths.py —— 「程序在哪」和「数据在哪」是两个问题，这个文件只回答这两个。

为什么必须分开（2026-10-26 用户拍板要打包）
    开发时两者恰好都是仓库根，所以一直看不出区别。打包之后：
      · 程序在 `app.asar` 里 —— **只读**，而且 python 根本读不到 asar；
      · `runs/`、`memory/`、`harness/charter.json`、`.harness/*.json`
        全都要写，写进 asar 就是当场失败。
    所以：`app_root()` 随安装走（只读，放代码和模板），`data_root()` 进用户目录（可写）。

只有一个入口
    环境变量只在这里读。别的模块一律 `P.data_root()` / `P.app_root()`，
    不许自己再写 `Path(__file__).parent.parent` —— 那样打包时又要满地找。

开发时行为**一个字不变**
    没有 `HARNESS_DATA` ⇒ `data_root() == app_root() == 仓库根`。
    所以这一层是纯加法的：验收、探针、日常开发都不用改。
"""
# 主要引用方包括 `workbench.py` 和 `baseline.py`。
from __future__ import annotations

import os
import shutil
from pathlib import Path

# ★ 这两个是**只读一次**的：打包时由 Electron 主进程在 spawn 前注入，
#   进程起来之后不会再变。测试要换根，就换个进程跑（那也更接近真实）。
_APP = Path(__file__).resolve().parent.parent


def app_root() -> Path:
    """程序根：代码、模板、`bin/*.py` 都在这儿。打包后是只读的。"""
    env = os.environ.get("HARNESS_APP")
    return Path(env) if env else _APP


def data_root() -> Path:
    """数据根：会话、记忆树、档位配置、勾选表、提案表。打包后是用户目录。

    ★ 不设 `HARNESS_DATA` 就退回程序根 —— 这是**开发时的语义**，
      不是"忘了配"。所以打包时必须显式给，不然会写进 asar 然后失败。
    """
    env = os.environ.get("HARNESS_DATA")
    return Path(env) if env else _APP


def sandbox_lifecycle_root() -> Path:
    """Per-user resource ownership survives a change of application data directory."""
    override = os.environ.get("HARNESS_SANDBOX_LIFECYCLE_ROOT")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "Modot" / "SandboxLifecycle"


def token_file() -> Path:
    """接口的钥匙文件（2026-10-26 加鉴权）。

    ★ 放**数据根**，不放程序根：打包后程序根是只读的（asar.unpacked），
      而且钥匙是"这台机器这一份"的东西，本来就该跟着用户数据走。
    ★ 谁要这把钥匙都从这里拿（界面外壳、探针、`--token`），
      **不要各自拼路径** —— 两处各拼一次，换形态时必有一处指错。
    """
    return data_root() / "api-token"


def is_split() -> bool:
    """程序和数据是不是分开了（= 跑在打包形态里）。"""
    try:
        return app_root().resolve() != data_root().resolve()
    except OSError:
        return str(app_root()) != str(data_root())


# 首次启动要播种的东西：**只拷缺的，绝不覆盖**。
# 覆盖等于把用户改过的记忆树/配置冲回出厂值 —— 那是数据事故，不是升级。
SEED = (
    ("harness/charter.json", False),
    # ★ 沙箱规则必须播种（2026-10-26 盘点时发现的洞）：打包后程序根在 asar 里
    #   （python 读不到），`sandbox_rules.rules_path()` 会退到它；
    #   数据根要是也没有这份文件，规则集会**空**，一切都落到 `default_action=ask` ——
    #   安全上算 fail-closed，但功能等于废掉：用户装上之后每条命令都要点一次允许。
    #   这份文件以前也没进版本库，所以这个洞在开发机上永远看不见。
    ("harness/sandbox_rules.json", False),
    (".harness/policy.json", False),
    (".harness/commands", True),
    (".harness/agents", True),
)


def seed() -> dict:
    """打包后第一次启动：把程序和模板拷到数据目录。开发时是空操作。"""
    out = {"split": is_split(), "copied": [], "kept": [], "error": ""}
    if not out["split"]:
        return out
    src_root, dst_root = app_root(), data_root()
    try:
        dst_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        out["error"] = "数据目录建不起来：%s" % e
        return out

    def _copy_one(rel: str, is_dir: bool) -> None:
        src = src_root / rel
        dst = dst_root / rel
        if not src.exists():
            return
        if is_dir:
            if not src.is_dir():
                return
            n = 0
            for f in src.rglob("*"):
                if not f.is_file():
                    continue
                d = dst / f.relative_to(src)
                if d.exists():
                    out["kept"].append(str(d.relative_to(dst_root)))
                    continue
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, d)
                n += 1
            if n:
                out["copied"].append("%s（%d 个文件）" % (rel, n))
        else:
            if dst.exists():
                out["kept"].append(rel)
                return
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            out["copied"].append(rel)

    for rel, is_dir in SEED:
        try:
            _copy_one(rel, is_dir)
        except OSError as e:
            out["error"] = (out["error"] + "；" if out["error"] else "") + \
                           "%s: %s" % (rel, e)
    return out


def describe() -> dict:
    """给界面/健康检查用：现在到底在哪。装错了、写不进，一眼能看出来。"""
    d = data_root()
    return {"app": str(app_root()), "data": str(d), "split": is_split(),
            "data_writable": _writable(d),
            # ★ 把"会话会写到哪"也报出来（2026-10-26）：打包后最常见的困惑是
            #   "会话列表是空的" —— 而它可能是写到另一个根去了。
            "runs": str(d / "runs")}


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write-probe"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False
