#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hooks.py —— 在固定时机跑用户自己的命令。

为什么能有这个
    hooks 本质就一句话："在这个时机，跑一个外部命令，把上下文用 JSON 从 stdin 给它"。
    本文件的旧命令入口用于本机用户配置。导入的 Claude/Codex 插件
    只走 plugin_hooks 的受限观察适配，不使用旧命令入口。现有挂点：
        SessionStart    ← run-task 开工
        UserPromptSubmit ← 人的输入进来（`--task` 与 `--say` 两个入口都接）
        PostToolUse     ← 每个工具执行完（policy.check 之后、rec("tool") 处）
        PreCompact      ← 上下文压缩**之前**（压完再看只剩"少了什么"的疑问）
        Stop            ← _done() 收尾

★ 两条边界，必须说清（不然这个功能会变成后门）
    ① **command 只从配置文件来，不由模型提供。** 模型能影响的是 payload 里的工具参数，
       而那些只走 stdin 的 JSON —— 绝不拼进命令行。拼了就等于"模型能执行任意命令"。
    ② **旧用户命令与导入插件的边界不同。** 旧命令只在明确授权的宿主路径启用；
       导入插件在无网络的任务副本内运行。
       反过来，它也**不能**拿来否决工具（那是 PreToolUse 的权限，见下）。
       这条必须写在文档里：装一个别人给的 hooks.json = 让他的命令在你的机器上跑。

★ 为什么先不接 PreToolUse 的"否决权"
    我们已经有 policy（三档：readonly / workspace / trusted）在调用前拦。
    再接一个 hook 否决，就是**两套门禁**，它们打架时谁赢？没想清楚之前不接 ——
    接错了的表现是"有时候拦得住有时候拦不住"，而那种不可靠比没有更糟。
    所以这一版只做**观察型**（跑完记一笔，不改行为）。
"""

# 主要引用方包括 `context_manager.py` 和 `loop.py`。

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

# 支持的事件名（和 Claude Code 对齐，方便直接转配置）
EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "PreCompact",
          "Stop", "SessionEnd")

_CACHE: dict[str, tuple[float, dict]] = {}


def _user_path() -> Path:
    return Path(os.environ.get("HARNESS_HOOKS")
                or (Path.home() / ".harness" / "hooks.json"))


def _read_one(p: Path) -> dict:
    """读一个 hooks.json。★ 读坏了**只警告，不让整轮跑不起来** ——
    一个配置手滑不该把整个任务挡住。"""
    if not p.exists():
        return {}
    try:
        st = p.stat().st_mtime
        cached = _CACHE.get(str(p))
        if cached and cached[0] == st:
            return cached[1]
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        hooks = data.get("hooks") if isinstance(data, dict) else None
        out = {k: v for k, v in (hooks or {}).items() if k in EVENTS and v}
        _CACHE[str(p)] = (st, out)
        return out
    except Exception:
        return {}


def load(repo_root: Path) -> dict:
    """合并两级配置：项目级在前（同名事件**都跑**，不是覆盖）。

    ★ 为什么不覆盖：hooks 是"额外的观察"，项目里加一条不该把用户级的顶掉。
      这和技能不同（技能是"用哪个版本"，项目级该盖住用户级）。
    """
    # ★ 项目根以 HARNESS_ROOT 为准（run-task 会设它）。
    #   为什么不让调用方各自传：loop.py 那一层拿不到仓库根（它只拿到会话目录），
    #   而"项目级 hooks.json 在哪"必须和"这次任务在哪个仓库跑"是同一个答案 ——
    #   两个地方各算一次迟早不一致，表现是"配了 hook 有时生效有时不生效"。
    env = os.environ.get("HARNESS_ROOT")
    root = Path(env) if env else Path(repo_root)

    merged: dict[str, list] = {}
    for p in (root / ".harness" / "hooks.json", _user_path()):
        for ev, items in _read_one(p).items():
            if isinstance(items, list):
                merged.setdefault(ev, []).extend(items)
    return merged


def _match(spec: dict, payload: dict) -> bool:
    """matcher 的正则匹配**工具名**；没有工具的事件退到**事件名**；不写就是全匹配。

    ★ 为什么要退到事件名（2026-10-26 接 UserPromptSubmit / PreCompact 时发现的）：
      原来只匹配 `payload["tool"]`，而这两个事件根本没有 tool ——
      于是**只要配了 matcher，钩子就永远不触发**，而且没有任何提示。
      配的人只会觉得"这个钩子时灵时不灵"，查半天查不到原因。
      ⇒ 没有 tool 时拿事件名去匹配（`"matcher": "PreCompact"` 也能用），
        不配 matcher 的老行为完全不变（照样全匹配）。
    """
    m = str(spec.get("matcher") or "").strip()
    if not m:
        return True
    try:
        return re.search(m, str(payload.get("tool") or payload.get("event") or "")) is not None
    except re.error:
        return False


def _kill_tree(p) -> None:
    """把一个 hook 进程**连同它的子进程**一起结束。

    ★ 为什么不能只 `p.kill()`（2026-10-26 探针逮到的真问题）：
      hooks 的 command 是人写的 shell 行，`shell=True` 起出来的进程树是
      `cmd.exe → 真正的命令`。只杀 `cmd.exe` 的话，真正的那个进程还活着 ——
      而父进程在等管道关闭（它继承了 stdout），于是**超时形同虚设**：
      实测 `timeout=0.5` 配一个 `sleep 5` 的钩子，实际拖了 **5.2 秒**。
      这正是超时要防的那件事，必须连树一起杀。
    """
    try:
        if getattr(p,"isolation",None):
            p.kill()
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, timeout=5)
            return
    except Exception:
        pass
    try:
        p.kill()
    except Exception:
        pass


def run(event: str, payload: dict, repo_root: Path, timeout: float = 15.0) -> list[dict]:
    """跑这个时机下匹配的 hooks。返回每次的结果（没人配就是空列表）。

    ★ 超时必须有：hook 里写了个死循环 / 等输入的命令，不能把整轮任务拖死。
      默认 15 秒，够跑一个 lint，不够跑一次全量测试（那本来就该用工具跑）。
    ★ stdin 只给 JSON，不给命令行参数：模型给的东西绝不进 shell。
    ★ 超时要**连进程树一起杀**（见 `_kill_tree`）：只杀 shell 等于没杀。
    """
    from . import evidence_questions, workbench as W
    from .execution_policy import draft_session, binding
    if payload.get('session') and draft_session(payload['session']):return []
    private=binding(Path(os.environ.get('HARNESS_ROOT') or repo_root))
    if private and draft_session(W.P.data_root()/'runs'/private['id']):return []
    thread = W.registry()["threads"].get(Path(str(payload.get("session") or "")).name, {})
    if thread.get("readonlyEvidence") or evidence_questions.binding(Path(os.environ.get("HARNESS_ROOT") or repo_root)):
        return []
    ident=thread.get('id') or (private or {}).get('id')
    from . import plugin_hooks
    out = plugin_hooks.run(ident,event,payload)
    for spec in (load(repo_root).get(event) or []):
        if not isinstance(spec, dict):
            continue
        cmd = spec.get("command")
        if not cmd or not _match(spec, payload):
            continue
        from . import permissions, safe_executor, netguard
        if permissions.effective_sandbox() != "danger-full-access":
            raise W.Problem("sandbox_transport", "此项目配置了尚未隔离的Hook，已停止；未在宿主执行脚本", 503)
        item = {"event": event, "command": str(cmd)[:200], "ok": True,
                "rc": None, "out": "", "err": "", "timeout": False}
        p = None
        try:
            env, _ = netguard.for_child()
            p = safe_executor.popen(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", str(cmd)], shell=False, cwd=str(repo_root), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace")
            _o, _e = p.communicate(json.dumps(payload, ensure_ascii=False),
                                   timeout=timeout)
            item["rc"] = p.returncode
            item["ok"] = p.returncode == 0
            item["out"] = (_o or "").strip()[:2000]
            item["err"] = (_e or "").strip()[:800]
        except subprocess.TimeoutExpired:
            if p is not None:
                _kill_tree(p)
                # 杀完再收一次；收不到就放弃（不能让"清理"本身又阻塞一轮）。
                try:
                    p.communicate(timeout=1.0)
                except Exception:
                    pass
            item.update(ok=False, timeout=True,
                        err="超过 %g 秒还没结束，已终止（连子进程）" % timeout)
        except Exception as e:
            item.update(ok=False, err="%s: %s" % (type(e).__name__, e))
        out.append(item)
    return out


def brief(item: dict) -> str:
    """一条 hook 结果写成一句话（给事件流用）。"""
    ev = item.get("event") or "hook"
    if item.get("timeout"):
        return "%s 钩子超时" % ev
    tag = "成" if item.get("ok") else ("返回 %s" % item.get("rc"))
    body = (item.get("out") or item.get("err") or "").replace("\n", " ").strip()
    return "%s 钩子 → %s%s" % (ev, tag, ("｜" + body[:120]) if body else "")
