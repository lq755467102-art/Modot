"""Per-run Agent RPC capabilities; never authorize human management APIs."""

# 主要引用方包括 `worker_plugins.py` 和 `tools.py`。

import hashlib
import hmac
import json
import os
import secrets
import threading
import urllib.request
import urllib.error
from . import workbench as W

_caps = {}
_lock = threading.Lock()
origin = None


def environment(ident):
    if not origin or not W.registry()["threads"].get(ident, {}).get("projectless"):
        return {}
    secret = secrets.token_urlsafe(32)
    with _lock:
        _caps[ident] = {"token": ident + ":" + secret, "pid": None}
    return {
        "HARNESS_AGENT_TOKEN": ident + ":" + secret,
        "HARNESS_AGENT_URL": origin + "/api/agent-tools",
        "HARNESS_AGENT_HOST_PID": str(os.getpid()),
        "HARNESS_AGENT_HOST_ID": W.RT.identity(os.getpid()),
    }


def bind(ident, process):
    with _lock:
        if ident in _caps:
            _caps[ident]["pid"] = process.pid


def retire(ident, *, pid=None, token=None):
    """Remove only the exited or failed-to-launch run's capability."""
    with _lock:
        cap = _caps.get(ident)
        if cap and ((pid is not None and cap["pid"] == pid)
                    or (token is not None and cap["token"] == token)):
            _caps.pop(ident)


def authorize(token, server):
    if not isinstance(token, str) or not token.isascii() or len(token) > 300:
        return None
    ident = token.split(":", 1)[0]
    with _lock:
        cap = dict(_caps.get(ident, {}))
    process = getattr(server, "PROCS", {}).get(ident)
    if (
        not cap
        or not hmac.compare_digest(cap["token"], token)
        or not process
        or process.pid != cap["pid"]
        or process.poll() is not None
    ):
        return None
    return ident


def request_id(context):
    return (
        "agent-"
        + hashlib.sha256(
            json.dumps(
                [context["sessionId"], context["runId"], context["actionId"]]
            ).encode()
        ).hexdigest()
    )


# `ident` 来自宿主已授权的会话。
# `context` 由 `loop/tools.py` 建立，`args` 是模型提供并仍需业务模块校验的工具参数。
def validate_call(ident, body, receipt_id=None):
    context = body.get("context", {})
    operation = body.get("operation")
    tools = {
        "check-start": "run_project_check",
        "check-join": "run_project_check",
        "check-output": "read_check_output",
        "check-settle": "__completion_review_wait__",
        "check-stop": "stop_project_check",
        "check-environments": "review_test_environment",
        "subtask-create": "create_subtask",
        "subtask-status": "inspect_subtasks",
        "subtask-cancel": "cancel_subtask",
        "worker-discover": "inspect_worker_plugins",
        "worker-delegate": "delegate_plugin_worker",
        "worker-manage": "manage_plugin_worker",
        "preview-start": "start_application",
        "preview-status": "inspect_application",
        "preview-stop": "stop_application",
        "debug-start": "start_python_debug",
        "debug-status": "inspect_python_debug",
        "debug-query": "collect_python_debug",
        "debug-control": "control_python_debug",
        "debug-evaluate": "evaluate_python_debug",
        "debug-stop": "stop_python_debug",
        "plugin-inspect": "inspect_plugin_tools",
        "plugin-call": "call_plugin_tool",
        "plugin-github-repositories": "find_github_repositories",
        "plugin-catalog": "list_plugin_catalog",
        "plugin-inventory": "list_installed_plugins",
        "plugin-install": "install_catalog_plugin",
        "plugin-preview-local": "preview_local_plugin",
        "plugin-install-local": "install_local_plugin",
        "plugin-enable": "enable_plugin_components",
    }
    if (
        not isinstance(context, dict)
        or context.get("sessionId") != ident
        or not context.get("runId")
        or not context.get("actionId")
        or operation not in tools
        or context.get("tool") != tools[operation]
        or not isinstance(body.get("args"), dict)
        or any(
            not isinstance(context.get(key), str) or not 1 <= len(context[key]) <= 128
            for key in ("sessionId", "runId", "actionId", "tool")
        )
    ):
        raise W.Problem("invalid_agent_call", "Agent 能力身份或操作无效", 403)
    if any(
        key.startswith("_") or key in {"root", "operation", "validate_live"}
        for key in body["args"]
    ):
        raise W.Problem("invalid_agent_call", "Agent 工具包含保留参数", 403)
    if receipt_id is not None and receipt_id != request_id(context):
        raise W.Problem("invalid_receipt", "Agent 回执不属于此会话动作", 403)
    return context, operation


# `bin/server.py` 交入活动运行的检查器和宿主服务。
# 这里还原工具身份，再按动作类型交给各自的业务模块。
def dispatch(ident, body, validate_live=lambda: None, server=None):
    from .tools import set_call, reset_call
    from . import previews, debugging

    context, operation = validate_call(ident, body)
    thread = W.registry()["threads"].get(ident)
    if not thread or not thread.get("projectless"):
        raise W.Problem("wrong_source", "Agent 对话已不存在", 403)
    if thread.get("readonlyEvidence"):
        raise W.Problem("readonly_question", "证据追问不能使用执行或生命周期服务", 403)
    root = W.workspace(thread["workspaceId"])["path"]
    token = set_call(context)
    try:
        from . import permissions
        permissions.effective_mode()
        # `check_sessions` 持有后台检查及其原进程。
        # 启动和环境操作需要宿主会话目录，读取和停止沿原检查身份处理。
        if operation.startswith('check-'):
            from . import check_sessions
            validate_live()
            if operation in {'check-start', 'check-join', 'check-environments', 'check-settle'}:
                directory = server._resolve_sid(ident) if server else None
                if not directory:
                    raise W.Problem('host_required', '检查需要宿主持有会话目录', 503)
                if operation == 'check-environments':
                    return check_sessions.environments(root, context, body['args'], directory)
                if operation == 'check-settle':
                    return check_sessions.settle_review(root, context, body['args'], directory, validate_live)
                if operation == 'check-join':
                    return check_sessions.join_preparing(root, context, body['args'], directory, validate_live)
                return check_sessions.start(root, context, body['args'], directory, validate_live)
            return check_sessions.query(root, context, body['args'], stop=operation == 'check-stop',
                                        validate_live=validate_live)
        if operation.startswith('plugin-'):
            from . import plugin_runtime
            if thread.get('subtaskOf'):raise W.Problem('plugin_denied','隔离子任务不继承主会话插件连接授权',403)
            if operation in {'plugin-catalog','plugin-inventory','plugin-install','plugin-preview-local','plugin-install-local','plugin-enable'}:
                return plugin_runtime.manage(ident,operation[7:],body['args'],validate_live)
            return plugin_runtime.invoke(ident,operation[7:],body['args'],validate_live)
        # `worker_plugins.py` 处理公开委派契约，`subtasks.py` 处理普通子任务。
        # 两者都使用宿主持有的队列。
        if operation.startswith('worker-'):
            from .worker_plugins import invoke
            if server is None:raise W.Problem('host_required','委派操作需要现有宿主',503)
            validate_live()
            return invoke(ident,operation,body['args'],server,validate_live=validate_live)
        if operation.startswith("subtask-"):
            from . import subtasks

            if server is None:
                raise W.Problem("host_required", "需要后端调度器", 503)
            return subtasks.invoke(
                ident, operation, body["args"], server, validate_live
            )
        service = debugging if operation.startswith("debug-") else previews
        return service.invoke(
            root, operation, validate_live=validate_live, **body["args"]
        )
    finally:
        reset_call(token)


def adapter(operation):
    def invoke(**args):
        return call(operation, **args)

    return invoke


def call(operation, **args):
    result = _request(operation, **args)
    if (operation=='check-start' and isinstance(result,dict)
            and (result.get('code')=='unknown' or not result.get('operationId') and result.get('state') in {'running','starting','claimed','unknown'})):
        from .tools import current_call
        from .check_sessions import operation_identity
        source=current_call()
        if source:
            result=dict(result,background=True,operationId=operation_identity(source),
                execution=dict(state='unknown',exitCode=None),
                readOutput=dict(tool='read_check_output',resource_id=args.get('resource_id'),
                    operation_id=operation_identity(source)))
    return result


def _request(operation, **args):
    from .tools import current_call

    context = current_call()
    endpoint, capability = (
        os.environ.get("HARNESS_AGENT_URL"),
        os.environ.get("HARNESS_AGENT_TOKEN"),
    )
    if not context or not endpoint or not capability:
        return {
            "ok": False,
            "code": "host_required",
            "error": "此操作需要应用后端拥有的运行服务，请从 Electron 会话启动",
        }
    ident = request_id(context)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(
            {"operation": operation, "context": context, "args": args}
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Harness-Agent-Token": capability,
            "X-Request-ID": ident,
        },
    )
    # `completion_review.py` 提供同一次收尾复核的剩余时限，请求等待也必须受该时限约束。
    from .completion_review import remaining_time
    remaining = remaining_time(context)
    if remaining == 0:
        return {"ok": False, "code": "completion_review_budget", "error": "收尾复核时限已到"}
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=270 if remaining is None else min(270, remaining)
        ) as response:
            return json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as error:
        try:
            return dict(json.loads(error.read(8192)), ok=False)
        except ValueError:
            return {"ok": False, "error": "运行服务拒绝了此次操作"}
    except (OSError, ValueError):
        return {
            "ok": False,
            "code": "unknown",
            "error": "运行操作回执未确认，请用对应 inspect 工具核对；不会自动重放启动命令",
        }
