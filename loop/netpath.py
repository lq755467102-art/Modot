"""网络出口 —— 一处决定，几处共用。

为什么需要这个文件（2026-10-27）
    这台机器上一直挂着 `HTTP_PROXY=http://127.0.0.1:<随机端口>`（某个本机代理软件），
    而它**每次重启都换端口**。

    界面里的后端是 Electron 起的子进程，继承的是**主进程启动那一刻**的环境变量。
    ⇒ 它一直在连一个早就关掉的端口，现象是**稳定复现**的 `WinError 10054`；
      而同一个请求在命令行里（新环境）却是 200 ——
      这就是"命令行好好的、界面一直坏"的全部原因。查了很久，值得写下来。

    ⇒ 出口由 harness 自己定：
      · 默认**直连**（模型 API 本来就在公网，代理只是多一个会坏的中转）；
      · 真要代理，显式设 `HARNESS_PROXY`；
      · `HTTP_PROXY` 和系统代理**都不采纳** —— 它们的生命周期不归我们管，
        而"代理换了端口"这种事的后果，是整个 harness 不能用。
"""

# 主要引用方包括 `provider.py` 和 `mcp.py`。

from __future__ import annotations

import os
import urllib.request


def proxy_setting() -> str:
    """显式指定的代理（空 = 直连）。只认 `HARNESS_PROXY`。"""
    return (os.environ.get("HARNESS_PROXY") or "").strip()


def env_proxy() -> str:
    """环境/系统里那个代理（**只当兜底**，不当默认）。

    ★ 什么时候用它（2026-10-27）：
      直连失败、而环境里确实有代理时，再走它试一次。
      这样两种网络都能用：
        · 代理坏了（端口换了）→ 直连就成功，根本走不到这里；
        · 直连不通、必须靠代理（比如境外模型）→ 这里兜住。
      `HTTP_PROXY` 不能当**默认**（它的端口什么时候变我们不知道），
      但当**失败后的第二次尝试**是合适的：那时我们已经没有别的路。
    """
    try:
        px = urllib.request.getproxies() or {}
    except Exception:
        return ""
    v = str(px.get("https") or px.get("http") or "").strip()
    if not v:
        return ""
    # socks 不算：urllib 的 ProxyHandler 只处理 http/https。
    if v.lower().startswith("socks"):
        return ""
    return v


def opener(proxy: str = "", *, handlers=()):
    """统一的网络出口。

    ★ 不传 `proxy` 时读 `HARNESS_PROXY`；都没有就是直连。
      特意**不**回落到环境里的 HTTP_PROXY：见上面那段。
    """
    p = (proxy or proxy_setting()).strip()
    if p:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": p, "https": p}), *handlers)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), *handlers)

