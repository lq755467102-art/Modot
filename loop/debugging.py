"""Conversation-owned Python debugging over the shared DAP transport."""

# 主要引用方包括 `workbench.py` 和 `agent_gateway.py`。

from pathlib import Path
import json
import os
import threading
import time
import uuid
import sqlite3
from . import workbench as W, projects, code_service
from .filelock import FileLock, atomic_json
from .evidence_redaction import redact

_locks = {}
_lock = threading.Lock()
_cancel_epoch = {}


# 将调试器返回值裁到可写入证据且可供模型读取的范围。
def bounded(value, depth=0):
    if isinstance(value, str):
        return value if len(value) <= 4000 else value[:4000] + "\n[已截断]"
    if depth > 8:
        return "[超出证据展开深度]"
    if isinstance(value, list):
        return [bounded(v, depth + 1) for v in value[:100]]
    if isinstance(value, dict):
        return {k: bounded(v, depth + 1) for k, v in list(value.items())[:100]}
    return value


def directory(ident):
    projects.conversation(ident)
    root = W.home() / "agent-debuggers" / ident
    root.mkdir(parents=True, exist_ok=True)
    return W.file_path({"path": str(W.home())}, str(root.relative_to(W.home())))


# 用会话与调试身份定位记录，阻止跨会话读原进程。
def record_path(ident, did):
    if (
        not isinstance(did, str)
        or len(did) != 32
        or any(c not in "0123456789abcdef" for c in did)
    ):
        raise W.Problem("invalid_debugger", "调试会话标识无效")
    return W.file_path({"path": str(directory(ident))}, did + ".json")


def load(ident, did):
    value = W.read(record_path(ident, did))
    if not value or value.get("conversationId") != ident:
        raise W.Problem("not_found", "调试会话不属于当前对话", 404)
    return redact(value)


# 合并持久记录与宿主当前调试器；内存句柄缺失且终态未确认时报告待核对。
def inspect(ident, did):
    value = load(ident, did)
    d = W._debuggers.get(did)
    if d is None:
        if value["state"] not in {"exited", "failed"}:
            value["state"] = "unknown"
    elif d.p.poll() is not None:
        try:
            d.close()
            value["state"] = "exited"
        except OSError:
            value["state"] = "unknown"
    else:
        value["state"] = d.phase
    return value


# 在原调试器消息上用谓词等待确认，保留调用方的超时上限。
def wait(d, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with d.lock:
            result = predicate(list(d.messages))
        if result:
            return result
        if d.p.poll() is not None:
            raise W.Problem("debugger_exited", "调试进程已经退出", 409)
        time.sleep(0.02)
    raise W.Problem("debugger_timeout", "调试响应未确认，请核对状态，不自动重放", 504)


def request(d, command, args):
    # debugpy 1.8.22 returns all children even with DAP start/count. Request
    # unpaged children, then page this bounded transport response ourselves.
    wire_args = dict(args, start=0, count=200) if command == "variables" else args
    seq = d.send(command, wire_args)["seq"]
    response = wait(
        d, lambda rows: next((m for m in rows if m.get("request_seq") == seq), None)
    )
    if not response.get("success"):
        raise W.Problem(
            "dap_failed", str(response.get("message", "调试请求失败"))[:2000], 409
        )
    body = response.get("body", {})
    if command == "variables":
        items = body.get("variables", [])
        offset, count = args.get("start", 0), args.get("count", 30)
        body = dict(
            body,
            variables=items[offset : offset + count],
            total=len(items),
            hasMore=offset + count < len(items),
            nextStart=min(offset + count, len(items)),
        )
    body = redact(bounded(body))
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if len(encoded) > 65536:
        return dict(
            truncated=True,
            preview=encoded[:16000].decode("utf-8", "ignore"),
            error="本次证据超过 64 KiB，请用 count=1 等更小范围重新采集；这不是完整输出",
        )
    return body


# 按当前状态取有限栈和作用域；只有已停止的线程才可检查。
def snapshot(d, value):
    value["state"] = inspect(value["conversationId"], value["id"])["state"]
    if value["state"] == "stopped" and d.stoppedThread:
        frames = request(d, "stackTrace", {"threadId": d.stoppedThread, "levels": 20})
        value["stack"] = frames.get("stackFrames", [])
        value["evidenceTruncated"] = bool(frames.get("truncated"))
    elif value["state"] != "stopped":
        value["stack"] = []
    with d.lock:
        outputs = [
            m.get("body", {}).get("output", "")
            for m in d.messages
            if m.get("event") == "output"
        ]
    value["output"] = redact("".join(outputs)[-8000:])
    atomic_json(record_path(value["conversationId"], value["id"]), value)
    return value


# 仅关闭宿主仍持有的调试会话，无法复核的状态留给 `inspect`。
def stop(ident, did):
    value = load(ident, did)
    d = W._debuggers.get(did)
    if d is None:
        return inspect(ident, did)
    d.close()
    value.update(state="exited", stack=[])
    atomic_json(record_path(ident, did), value)
    process = W.read(W.record_path(did))
    process.update(state="completed", exitCode=d.p.returncode)
    atomic_json(W.record_path(did), process)
    return value


# 先使该会话持有的启动校验过期，再收回已登记调试器。
def stop_conversation(ident):
    with _lock:
        _cancel_epoch[ident] = _cancel_epoch.get(ident, 0) + 1
    for path in directory(ident).glob("*.json"):
        if path.stem in W._debuggers:
            stop(ident, path.stem)


# `agent_gateway.py` 传入后续仍可校验的工具身份。
# 由 `code_service.py` 核对执行资源，原 Windows 进程族用于启动、停止和记录。
def start(root, args, validate_live):
    if os.name != "nt":
        raise W.Problem(
            "owned_debug_unavailable",
            "首批 Agent 调试需要 Windows Job 进程树所有权；此平台未接入",
            503,
        )
    ident = args["resource_id"]
    thread, resource, ws, source = code_service.context_resource(root, ident, "execute")
    with _lock:
        epoch = _cancel_epoch.get(thread["id"], 0)
    original_validate = validate_live

    # 启动步骤同时核对调用的活性与会话取消代次，防止关会话后登记迟到进程。
    def validate_live():
        original_validate()
        with _lock:
            if _cancel_epoch.get(thread["id"], 0) != epoch:
                raise W.Problem("cancelled", "调试启动已取消", 409)

    with FileLock(W.writer_lock_path(ws["path"])):
        thread, resource, ws, source = code_service.context_resource(
            root, ident, "execute"
        )
        validate_live()
        W.guard_workspace_writer(
            ws["path"], code_service.RuntimeView(), exclude_thread=thread["id"]
        )
        W.check_revision(ws, args.get("expected_revision"))
        state = W.git_status(ws)
        if (
            not state["available"]
            or state.get("protected")
            or state.get("branch") == "(detached)"
        ):
            raise W.Problem("protected_branch", "调试请先使用有版本的功能分支", 403)
        program = W.file_path(ws, args["program"])
        if program.suffix.lower() != ".py" or not program.is_file():
            raise W.Problem("invalid_program", "请选择资源内 Python 程序")
        lines = args.get("lines", [])
        argv = args.get("args", [])
        if (
            not isinstance(lines, list)
            or len(lines) > 100
            or any(type(v) is not int or v < 1 or v > 1000000 for v in lines)
        ):
            raise W.Problem("invalid_breakpoints", "断点行无效")
        if (
            not isinstance(argv, list)
            or len(argv) > 100
            or any(not isinstance(v, str) or len(v) > 4000 for v in argv)
        ):
            raise W.Problem("invalid_arguments", "程序参数无效")
        files = list(directory(thread["id"]).glob("*.json"))
        if (
            len(files) >= 100
            or sum(
                inspect(thread["id"], p.stem)["state"] not in {"exited", "failed"}
                for p in files
            )
            >= 2
        ):
            raise W.Problem("debugger_limit", "请先结束已有调试会话，或使用新对话", 429)
        validate_live()
        from . import change_history as history
        change_id=history.prepare(thread['id'],resource,ws,dict(id=uuid.uuid4().hex,
            operation='debug',source=source,branch=state['branch'],head=state['head'],target=dict(relative_path=args['program'])))
        d = W.Debugger(ws,on_closed=history.completion(thread['id'],change_id,ws))
        W._debuggers[d.id] = d
        value = dict(
            id=d.id,
            conversationId=thread["id"],
            resourceId=ident,
            workspaceId=ws["id"],
            revision=state["revision"],
            source=source,
            state="starting",
            program=args["program"],
            stack=[],
            output="",
            created=time.time(),
            changeId=change_id,
        )
        try:
            atomic_json(record_path(thread["id"], d.id), value)
            atomic_json(
                W.record_path(d.id),
                dict(
                    id=d.id,
                    kind="debug",
                    conversationId=thread["id"],
                    workspaceId=ws["id"],
                    pid=d.p.pid,
                    identity=W.RT.identity(d.p.pid),
                    ownerPid=os.getpid(),
                    ownerIdentity=W.RT.identity(os.getpid()),
                    state="running",
                    source=source,
                ),
            )
        except Exception:
            d.close()
            W._debuggers.pop(d.id, None)
            raise

    try:
        validate_live()
        request(
            d,
            "initialize",
            {
                "adapterID": "debugpy",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "pathFormat": "path",
                "supportsVariablePaging": False,
            },
        )
        validate_live()
        code_service.context_resource(root, ident, "execute")
        d.send(
            "launch",
            {
                "program": args["program"],
                "args": argv,
                "justMyCode": True,
                "stopOnEntry": not bool(lines),
            },
        )
        wait(
            d,
            lambda rows: next(
                (m for m in rows if m.get("event") == "initialized"), None
            ),
        )
        validate_live()
        code_service.context_resource(root, ident, "execute")
        breakpoints = request(
            d,
            "setBreakpoints",
            {
                "source": {"path": args["program"]},
                "breakpoints": [{"line": v} for v in lines],
            },
        )
        value["breakpoints"] = breakpoints.get("breakpoints", [])
        validate_live()
        code_service.context_resource(root, ident, "execute")
        request(d, "configurationDone", {})
        wait(
            d,
            lambda rows: next(
                (
                    m
                    for m in rows
                    if m.get("event") in {"stopped", "terminated", "exited"}
                ),
                None,
            ),
        )
        return {"ok": True, "debugger": snapshot(d, value), "changeId":change_id}
    except Exception:
        stop(thread["id"], d.id)
        raise


# 将调试操作分为新建、现有会话查询与控制。影响进程状态的请求在代码操作记录中保留回执。
def invoke(root, operation, *, validate_live=lambda: None, **args):
    change_id=None
    try:
        allowed = {
            "debug-start": {
                "resource_id",
                "program",
                "lines",
                "args",
                "expected_revision",
            },
            "debug-status": {"resource_id", "debugger_id"},
            "debug-query": {
                "resource_id",
                "debugger_id",
                "view",
                "reference",
                "start",
                "count",
            },
            "debug-control": {"resource_id", "debugger_id", "command"},
            "debug-evaluate": {"resource_id", "debugger_id", "expression", "frame_id"},
            "debug-stop": {"resource_id", "debugger_id"},
        }
        if operation not in allowed or set(args) - allowed[operation]:
            raise W.Problem("invalid_arguments", "调试工具包含未声明参数")
        validate_live()
        if operation == "debug-start":
            return start(root, args, validate_live)
        from .tools import current_call

        context = current_call()
        if not context:
            raise W.Problem("identity_required", "调试需要实际会话身份", 403)
        thread = projects.conversation(context["sessionId"])
        if (
            Path(W.workspace(thread["workspaceId"])["path"]).resolve()
            != Path(root).resolve()
        ):
            raise W.Problem("wrong_source", "调试会话来源不一致", 403)
        value = load(thread["id"], args["debugger_id"])
        if value["resourceId"] != args["resource_id"]:
            raise W.Problem("wrong_source", "调试会话不属于所指资源", 403)
        if operation not in {"debug-stop", "debug-status"}:
            code_service.context_resource(root, args["resource_id"], "execute")
        elif operation == "debug-status":
            code_service.context_resource(root, args["resource_id"])
        with _lock:
            lock = _locks.setdefault(value["id"], threading.RLock())
        with lock:
            validate_live()
            if operation == "debug-stop":
                value = stop(thread["id"], value["id"])
                return {"ok": value["state"] == "exited", "debugger": value}
            if operation == "debug-status":
                return {"ok": True, "debugger": inspect(thread["id"], value["id"])}
            code_service.context_resource(root, args["resource_id"], "execute")
            d = W._debuggers.get(value["id"])
            if d is None or d.p.poll() is not None:
                raise W.Problem(
                    "not_owned", "调试句柄不可接管；旧证据可查看，请新建调试", 409
                )
            from . import change_history as history
            history_ws=None
            # 一次调试命令的实际响应与操作状态绑定，失败也写入原回执。
            def observed_request(command,params):
                nonlocal change_id,history_ws
                current,resource,ws,source=code_service.context_resource(root,args['resource_id'],'execute')
                if change_id is None:
                    change_id=history.prepare(thread['id'],resource,ws,dict(id=uuid.uuid4().hex,
                        operation=operation,source=source,target={}))
                    history_ws=ws
                validate_live()
                code_service.context_resource(root,args['resource_id'],'execute')
                return request(d,command,params)
            result = {}
            if operation == "debug-query":
                view = args.get("view", "stack")
                reference = args.get("reference")
                if view not in {"stack", "scopes", "variables"} or (
                    view != "stack" and (type(reference) is not int or reference <= 0)
                ):
                    raise W.Problem("invalid_query", "请选择栈、作用域或变量及有效引用")
                start_at = args.get("start", 0)
                count = args.get("count", 30)
                if type(count) is not int or not 1 <= count <= 30:
                    raise W.Problem("invalid_count", "变量采集条数须为 1 至 30")
                if type(start_at) is not int or start_at < 0:
                    raise W.Problem("invalid_cursor", "变量偏移无效")
                command, params = {
                    "stack": (
                        "stackTrace",
                        {"threadId": d.stoppedThread, "levels": 20},
                    ),
                    "scopes": ("scopes", {"frameId": reference}),
                    "variables": (
                        "variables",
                        {
                            "variablesReference": reference,
                            "start": start_at,
                            "count": count,
                        },
                    ),
                }[view]
                result = observed_request(command, params)
            elif operation == "debug-evaluate":
                expression = args.get("expression")
                if (
                    not isinstance(expression, str)
                    or not 1 <= len(expression) <= 4000
                    or type(args.get("frame_id")) is not int
                ):
                    raise W.Problem("invalid_expression", "求值表达式或栈帧无效")
                result = observed_request(
                    "evaluate",
                    {
                        "expression": expression,
                        "frameId": args["frame_id"],
                        "context": "repl",
                    },
                )
            else:
                command = args.get("command")
                if command not in {"continue", "next", "stepIn", "stepOut", "pause"}:
                    raise W.Problem("invalid_command", "请选择继续、单步或暂停")
                threads = observed_request("threads", {}).get("threads", [])
                tid = d.stoppedThread or (threads[0]["id"] if threads else None)
                if not tid:
                    raise W.Problem("no_thread", "没有可控制的调试线程", 409)
                validate_live()
                code_service.context_resource(root, args["resource_id"], "execute")
                result = observed_request(command, {"threadId": tid})
            value = snapshot(d, value)
            value["lastEvidence"] = result
            value["evidenceTruncated"] = bool(
                value.get("evidenceTruncated") or result.get("truncated")
            )
            value["lastSource"] = dict(
                context, actor="agent", threadId=thread["id"], taskId=thread["taskId"]
            )
            atomic_json(record_path(thread["id"], value["id"]), value)
            if change_id is not None:
                history.finish(thread['id'],change_id,history_ws)
            return {"ok": True, "debugger": value, "result": result, "changeId":change_id}
    except (W.Problem, OSError, ValueError, KeyError, TimeoutError,sqlite3.Error) as error:
        if change_id is not None:
            try:history.mark_unknown(thread['id'],change_id)
            except Exception:pass
        return {
            "ok": False,
            **({'state':'unknown','changeId':change_id} if change_id is not None else {}),
            "code": getattr(error, "code", "debug_failed"),
            "error": getattr(error, "message", str(error)),
        }


# 会话 HTTP 接口只读已登记的调试记录，启动及控制需经对应的授权入口。
def route(ident, parts, method):
    if method != "GET":
        raise W.Problem(
            "readonly_route", "调试控制请使用 Agent 工具或专业调试视图", 405
        )
    if not parts:
        files = sorted(
            directory(ident).glob("*.json"),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        return 200, {
            "debuggers": [inspect(ident, p.stem) for p in files[:20]],
            "hasMore": len(files) > 20,
        }
    if len(parts) == 1:
        return 200, inspect(ident, parts[0])
    raise W.Problem("not_found", "调试证据不存在", 404)
