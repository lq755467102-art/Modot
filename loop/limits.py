"""limits.py —— 窗口上限**问接口**，不由我们拍一个数。

★ 为什么要有这个文件（2026-10-28 用户：「那就抬到模型的真实窗口上限，我们本身不去做限制」）
    配置里原来写着 `context_limit: 150000`。这个数**没有任何地方能证明它是对的** ——
    它不是模型说的，是我们某天写进去的。实测问了一次接口：
    自我的模型报 `context_window = 1048576`（1M），我们写的 15 万只有它的 1/7。
    ⇒ 代价不是"少用点"，而是**每 6% 就触发一次压缩**（压缩阈值是窗口的 60%），
      把还很新鲜的上下文反复摘要掉。人看到的是"它怎么老忘事"。

★ 所以这里的规矩
    1. 窗口**以接口上报为准**（`GET /models` 里的 `context_window` /
       `context_length` / `max_context_tokens`，名字各家不一样，都认）；
    2. 接口不报（比如智谱那家不给这个字段）⇒ 退回配置值，并**记一句"这是配置值，不是实测"**；
    3. 永远不猜：拿不到就返回 None，让调用方自己决定怎么降级。

★ 不联网的场合（探针、离线）：`probe_window(..., timeout=0)` 直接返回 None，不抛。
"""
# 主要引用方包括 `context_manager.py` 和 `bin/run-task.py`。
from __future__ import annotations

import json
import urllib.request

# 进程内缓存：一个模型问一次就够，别每条命令都去问
_CACHE: dict[str, int | None] = {}

# 各家给窗口用的字段名（都见过，不收白不收）
_FIELDS = ("context_window", "context_length", "max_context_tokens",
           "max_input_tokens", "max_model_len")


def _pick(item: dict) -> int | None:
    for f in _FIELDS:
        v = item.get(f)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    # 有些网关把它塞在嵌套里：{"limits": {"context": 128000}}
    lim = item.get("limits") or item.get("capabilities") or {}
    if isinstance(lim, dict):
        for f in _FIELDS + ("context", "window", "max_tokens"):
            v = lim.get(f)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
    return None


def probe_window(base_url: str, api_key: str, model: str,
                 timeout: float = 20.0) -> int | None:
    """问接口：这个模型的上下文窗口到底多大。拿不到返回 None，永不抛。"""
    if not (base_url and model) or not timeout:
        return None
    key = "%s|%s" % (base_url.rstrip("/"), model)
    if key in _CACHE:
        return _CACHE[key]
    got: int | None = None
    try:
        url = base_url.rstrip("/") + "/models"
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + (api_key or ""),
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        items = body.get("data") or body.get("models") or []
        if isinstance(items, dict):            # 有的网关给 {"models": {...}}
            items = list(items.values())
        for it in items:
            if not isinstance(it, dict):
                continue
            if str(it.get("id") or it.get("name") or "") == str(model):
                got = _pick(it)
                break
    except Exception:
        got = None
    _CACHE[key] = got
    return got


def probe_output_max(base_url: str, api_key: str, model: str,
                     timeout: float = 20.0) -> int | None:
    """单次回复最多能出多少 token。拿不到返回 None（调用方保留自己的默认）。"""
    if not (base_url and model) or not timeout:
        return None
    key = "out|%s|%s" % (base_url.rstrip("/"), model)
    if key in _CACHE:
        return _CACHE[key]
    got: int | None = None
    try:
        url = base_url.rstrip("/") + "/models"
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + (api_key or ""),
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        items = body.get("data") or body.get("models") or []
        if isinstance(items, dict):
            items = list(items.values())
        for it in items:
            if isinstance(it, dict) and str(it.get("id") or it.get("name") or "") == str(model):
                for f in ("max_output_tokens", "max_completion_tokens", "output_tokens"):
                    v = it.get(f)
                    if isinstance(v, (int, float)) and v > 0:
                        got = int(v)
                        break
                break
    except Exception:
        got = None
    _CACHE[key] = got
    return got
