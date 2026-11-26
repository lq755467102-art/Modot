#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""netguard.py —— 子进程的网络与凭据环境：**能给的那部分才给**。

它做什么（两件事，都不叫"沙箱"）
    ① **洗环境**（默认开）：把凭据类环境变量从子进程环境里拿掉
       （`*_API_KEY` / `*_TOKEN` / `*_SECRET*` / `*PASSWORD*` / `AWS_*` / `*PROXY*` …）。
       理由很直接：`run_script` 跑的任何一段代码都能 `os.environ` 全读一遍 ——
       一个写着"帮我把日志发出去"的脚本，顺手就能把模型的 key 一起发出去。
       这不是它有没有恶意的问题，是**不该给它**。
    ② **离线开关**（默认关）：打开时把 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY`
       指向一个**没人听**的端口（127.0.0.1:9），并清空 `NO_PROXY`。
       凡是"按规范读代理环境变量"的库（requests / urllib / curl / pip）就出不去。

★ 必须说清的边界（不然就是吹）
    ① 是**降低概率**，不是出口控制：硬编码 socket / 自带 HTTP 栈 / 忽略代理变量的程序
       照样出得去。真正的出口控制要 WFP 过滤或一个强制代理，那需要管理员权限或改架构。
    ② 洗环境只洗**环境变量**：写死在代码里的 key、文件里的凭据，它照样读得到
       （那条靠路径规则和"停下问人"挡）。
    ③ 所以这一层的定位是「**别顺手把钥匙带出去**」，不是"断网"。
"""
# 主要引用方包括 `workbench.py` 和 `tools.py`。
from __future__ import annotations

import os
import re

# 凭据类环境变量（名字匹配就算）。★ 只认**名字**，不猜内容 ——
# 猜内容会在 base64 和普通字符串之间反复误判，而名字是写死在这台机器上的。
# ★ 只认名字，且**不用 `pwd`**（那是工作目录，不是密码）、
#   `auth` 要独立成段（否则 `GIT_AUTHOR_NAME` 也会被误伤 —— 探针 B1 逮到的）。
_SECRET_RE = re.compile(
    r"(api[_-]?key|access[_-]?key|secret|token|passw|credential"
    r"|private[_-]?key|session[_-]?id|cookie"
    r"|(?<![a-z])auth(?![a-z]))",
    re.I)
# 明确要留的（它们的名字像凭据，但它们是**我们的配置**，不是用户的钥匙）
_KEEP = {"HARNESS_MODEL", "HARNESS_BASE_URL", "HARNESS_REVIEW_MODEL",
         "HARNESS_REVIEW_BASE_URL", "HARNESS_CHARTER", "HARNESS_DATA",
         "HARNESS_APP", "HARNESS_MCP", "HARNESS_HABITS", "HARNESS_GRANTS",
         "HARNESS_PROPOSED", "HARNESS_MEM_AUDIT", "HARNESS_OFFLINE"}
# 代理类：单独处理（离线开关要改写它们，不是删掉）
_PROXY_RE = re.compile(r"(http_proxy|https_proxy|all_proxy|no_proxy)", re.I)

# 离线时指向的"黑洞"：本机 9 号端口，正常没人听
DEAD = "http://127.0.0.1:9"


def _cfg() -> dict:
    try:
        from . import settings as SET
        return SET.load().get("sandbox") or {}
    except Exception:
        return {}


def offline_on() -> bool:
    if os.environ.get("HARNESS_OFFLINE"):
        return True
    return bool(_cfg().get("offline", False))


def clean(env: dict | None = None) -> tuple[dict, list[str]]:
    """洗掉凭据类变量。返回 (新环境, 被拿掉的名字)。"""
    src = dict(os.environ if env is None else env)
    gone = []
    for k in list(src):
        if k in _KEEP:
            continue
        if _PROXY_RE.search(k):
            continue                       # 代理留给 offline() 处理
        if _SECRET_RE.search(k):
            src.pop(k, None)
            gone.append(k)
    return src, gone


def offline(env: dict) -> dict:
    """把出口指向黑洞（只对读代理变量的人有效，边界见文件头）。"""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        env[k] = DEAD
    env["NO_PROXY"] = ""
    env["no_proxy"] = ""
    return env


def for_child(extra: dict | None = None) -> tuple[dict, dict]:
    """给子进程用的环境。返回 (env, 说明)。

    ★ 说明里带上"拿掉了几个、离线开没开" —— 启动日志要能回答
      "它到底看得见什么"，否则"洗过"和"没洗"从外面一模一样。
    """
    env, gone = clean()
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    off = offline_on()
    if off:
        offline(env)
    return env, {"dropped": sorted(gone), "offline": off,
                 "dead": DEAD if off else ""}


def describe(gone: list[str] | None = None, off: bool | None = None) -> str:
    off = offline_on() if off is None else off
    # ★ 先落到**同一个变量**上再格式化（2026-10-26 实跑踩到）：
    #   原来 `n = ... if gone is not None else len(clean()[1])` 只算了长度，
    #   下面 `sorted(gone)` 拿到的还是 None ⇒ `'NoneType' object is not iterable`，
    #   而它被外层 try 吞成"执行层隔离初始化失败" —— 报错指错了地方，最难查。
    items = list(gone) if gone is not None else clean()[1]
    n = len(items)
    s = "子进程环境：拿掉了 %d 个凭据类变量（%s）" % (
        n, "、".join(sorted(items)[:4]) + ("…" if n > 4 else "") if n else "没有")
    if off:
        s += "；离线模式：代理指向 %s（只挡「读代理变量」的程序，不算出口控制）" % DEAD
    else:
        s += "；网络：没限制（联网靠内容闸 + 停下问人）"
    return s
