"""Backend-owned local application instances, distinct from delivery acceptance."""

# 主要引用方包括 `workbench.py` 和 `agent_gateway.py`。

import json
from pathlib import Path
import re
import socket
import time
from urllib.parse import quote, urlsplit
import uuid
import sqlite3
from . import workbench as W, code_service, projects
from .filelock import FileLock, atomic_json
from .port_owner import owned_listener


STARTUP_TIMEOUT = 20


def directory(ident):
    projects.conversation(ident)
    root = W.home() / "previews" / ident
    root.mkdir(parents=True, exist_ok=True)
    if not root.resolve().is_relative_to(W.home().resolve()):
        raise W.Problem("path_escape", "预览记录路径已变化", 403)
    return root


# 以原预览 ID 查持久记录，读写均留在该会话目录。
def record_path(ident, aid):
    if not isinstance(aid, str) or not re.fullmatch("[0-9a-f]{32}", aid):
        raise W.Problem("invalid_preview", "预览标识无效")
    return W.file_path({"path": str(directory(ident))}, aid + ".json")


def load(ident, aid):
    value = W.read(record_path(ident, aid))
    if not value or value.get("conversationId") != ident:
        raise W.Problem("not_found", "应用预览不属于当前对话", 404)
    return value


# 先借 `port_owner.py` 核对本机端口属于原进程，再确认应用能响应。
def probe(value, process):
    deadline = time.monotonic() + 0.8
    try:
        with socket.create_connection(
            ("127.0.0.1", value["port"]), timeout=0.2
        ) as connection:
            if not owned_listener(
                value["port"],
                process["pid"],
                process["identity"],
                connection.getsockname()[1],
            ):
                return None
            path = urlsplit(value.get('readyUrl') or value['url'])
            target = (path.path or "/") + ("?" + path.query if path.query else "")
            connection.settimeout(0.1)
            connection.sendall(
                (
                    "GET "
                    + target
                    + " HTTP/1.1\r\nHost: 127.0.0.1:"
                    + str(value["port"])
                    + "\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
            )
            headers = b""
            while time.monotonic() < deadline and len(headers) < 8192:
                try:
                    data = connection.recv(min(1024, 8192 - len(headers)))
                except socket.timeout:
                    continue
                if not data:
                    break
                headers += data
                if b"\r\n\r\n" in headers:
                    match = re.match(
                        rb"HTTP/1\.[01] ([0-9]{3}) ", headers.split(b"\r\n", 1)[0]
                    )
                    return int(match[1]) if match else None
    except (OSError, ValueError):
        return None
    return None


# 用原预览和宿主进程记录更新可用、退出或待核对状态；版本检查依据实际检出目录。
def inspect(ident, aid, include_version=True):
    value = load(ident, aid)
    pid = value.get("processId")
    value["canStop"] = False
    if not pid:
        return value
    process = W.process_record(pid)
    value["process"] = process
    owned = W._processes.get(pid)
    value["canStop"] = owned is not None and owned.poll() is None
    if include_version:
        output = W.process_output(pid)
        value["log"] = (
            output.get("outputNotice", "") + "\n" if output.get("truncated") else ""
        ) + output["text"][:4000]
    ready_file = record_path(ident, aid).with_suffix(".ready")
    observation = W.read(ready_file)
    was_ready = (
        isinstance(observation, dict)
        and observation.get("processId") == pid
        and observation.get("url") == value["url"]
        and observation.get('readyUrl',observation.get('url')) == value.get('readyUrl',value['url'])
        and observation.get("revision") == value["revision"]
    )
    value["state"] = "not_ready"
    if ready_file.exists() and not was_ready:
        value["state"] = "unknown"
        value["error"] = "应用就绪记录无法核对，未推断启动成功或失败"
    elif value.get("startupTimedOut"):
        ended = process["state"] in {"completed", "failed", "interrupted"}
        value["state"] = "failed" if ended else "unknown"
        value["error"] = (
            "应用启动超时，进程已结束。请查看日志。"
            if ended else "应用启动超时，正在核对进程是否已停止。"
        )
    elif process["state"] in {"completed", "failed", "interrupted"}:
        value["state"] = (
            "expired" if was_ready or process.get("stopRequested") else "failed"
        )
        if value["state"] == "failed":
            value["error"] = (
                "应用进程在HTTP就绪前结束，未验证可用；请核对实际退出码和日志"
            )
    elif process["state"] == "unknown" or pid not in W._processes:
        value["state"] = "unknown"
    else:
        status = probe(value, process)
        if status is not None:
            value["httpStatus"] = status
            if 200 <= status < 300:
                if not was_ready:
                    atomic_json(
                        ready_file,
                        dict(
                            processId=pid,
                            url=value["url"],
                            readyUrl=value.get('readyUrl',value['url']),
                            revision=value["revision"],
                            observedAt=time.time(),
                        ),
                    )
                value["state"] = "ready"
        if value["state"] != "ready":
            value["error"] = (
                "未核实自有 IPv4 连接和 HTTP 2xx。请查看日志并让应用显式绑定 127.0.0.1；默认 :: 监听暂未接入。"
            )
    if include_version:
        ws = W.workspace(value["workspaceId"])
        value["stale"] = W.current_revision(ws) != value["revision"]
    return value


# 返回本会话最近的预览及其当前可核对状态。
def list_previews(ident):
    files = sorted(
        directory(ident).glob("*.json"),
        key=lambda p: p.stat().st_mtime_ns,
        reverse=True,
    )
    return {
        "previews": [inspect(ident, path.stem) for path in files[:20]],
        "hasMore": len(files) > 20,
    }


# 向操作系统探询临时本机端口，启动时仍要核对归属。
def available_port(requested):
    for candidate in (requested, 0) if requested else (0,):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
                return probe.getsockname()[1]
            except OSError:
                continue
    raise W.Problem("port_unavailable", "无法分配本地端口", 409)


# 仅请求停止宿主仍持有的原进程，结果交给 `inspect` 确认。
def stop(ident, aid):
    value = load(ident, aid)
    controlled = value.get("processId") in W._processes
    if controlled:
        W.stop_process(value["processId"])
    return dict(inspect(ident, aid), stopRequested=controlled)


# `agent_gateway.py` 转入应用启动、查看、输出与停止。
# 启动资源和版本由 `code_service.py` 确认。
def invoke(root, operation, *, validate_live=lambda: None, **args):
    try:
        allowed = {
            "preview-start": {
                "resource_id",
                "argv",
                "expected_revision",
                "requested_port",
                "ready_path",
                "entry_path",
            },
            "preview-status": {"resource_id", "preview_id"},
            "preview-stop": {"resource_id", "preview_id"},
        }
        if operation not in allowed or set(args) - allowed[operation]:
            raise W.Problem("invalid_arguments", "预览工具包含未声明参数")
        validate_live()
        if operation == "preview-stop":
            from .tools import current_call

            context = current_call()
            if not context:
                raise W.Problem("identity_required", "停止应用需要实际会话身份", 403)
            thread = projects.conversation(context["sessionId"])
            if (
                Path(W.workspace(thread["workspaceId"])["path"]).resolve()
                != Path(root).resolve()
            ):
                raise W.Problem("wrong_source", "应用不属于此会话", 403)
            value = load(thread["id"], args["preview_id"])
            if args["resource_id"] != value["resourceId"]:
                raise W.Problem("wrong_source", "应用不属于所指资源", 403)
            stopped = stop(thread["id"], value["id"])
            return {
                "ok": bool(
                    stopped.get("stopRequested") or stopped["state"] == "expired"
                ),
                "preview": stopped,
            }
        thread, resource, ws, source = code_service.context_resource(
            root,
            args["resource_id"],
            "execute" if operation == "preview-start" else None,
        )
        if operation != "preview-start":
            value = load(thread["id"], args["preview_id"])
            if value["resourceId"] != resource["id"]:
                raise W.Problem("wrong_source", "预览不属于所选代码资源", 403)
            return {
                "ok": True,
                "preview": stop(thread["id"], value["id"])
                if operation == "preview-stop"
                else inspect(thread["id"], value["id"]),
            }
        from . import permissions

        W.require_host_transport("preview", permissions.effective_sandbox())
        with FileLock(W.writer_lock_path(ws["path"])):
            thread, resource, ws, source = code_service.context_resource(
                root, args["resource_id"], "execute"
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
                raise W.Problem(
                    "protected_branch", "启动应用请先使用有版本的功能分支", 403
                )
            current = list_previews(thread["id"])["previews"]
            if (
                sum(v["state"] in {"ready", "not_ready", "unknown"} for v in current)
                >= 3
            ):
                raise W.Problem(
                    "preview_limit", "当前对话已有三个未结束的应用，请先停止", 429
                )
            if len(list(directory(thread["id"]).glob("*.json"))) >= 100:
                raise W.Problem(
                    "preview_limit", "当前对话应用记录已达上限，请使用新会话", 413
                )
            argv = args.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(v, str) for v in argv)
                or not any("{port}" in arg for arg in argv)
            ):
                raise W.Problem(
                    "invalid_command", "应用命令需为 argv 数组并包含 {port} 端口占位符"
                )
            requested = args.get("requested_port", 0)
            if type(requested) is not int or not 0 <= requested <= 65535:
                raise W.Problem("invalid_port", "预览端口无效")
            path = args.get("ready_path", "/")
            if (
                not isinstance(path, str)
                or not path.startswith("/")
                or len(path) > 512
                or "#" in path
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in path)
            ):
                raise W.Problem(
                    "invalid_preview", "就绪检查需使用当前应用的相对 URL 路径"
                )
            path = quote(path, safe="/%?=&:+,;~!$'()*@-._")
            entry=args.get('entry_path','/')
            if (not isinstance(entry,str) or not entry.startswith('/') or len(entry)>512 or
                    any(c.isspace() or ord(c)<32 or ord(c)==127 for c in entry)):
                raise W.Problem('invalid_preview','应用页面需使用当前应用的相对 URL 路径')
            entry=quote(entry,safe="/%?=&:+,;~!$'()*@-._#")
            port = available_port(requested)
            argv = [part.replace("{port}", str(port)) for part in argv]
            from .policy import Policy

            allowed, why = Policy("workspace", root=Path(ws["path"])).check(
                "run_check", {"cmd": argv}, kind="exec"
            )
            if not allowed:
                raise W.Problem("policy_denied", why, 403)
            value = {
                "id": uuid.uuid4().hex,
                "conversationId": thread["id"],
                "resourceId": resource["id"],
                "workspaceId": ws["id"],
                "revision": state["revision"],
                "port": port,
                "url": f"http://127.0.0.1:{port}" + entry,
                "readyUrl":f"http://127.0.0.1:{port}"+path,
                "source": source,
                "state": "starting",
                "created": time.time(),
            }
            from . import change_history as history
            change_id=history.prepare(thread['id'],resource,ws,dict(id=uuid.uuid4().hex,
                operation='preview',source=source,branch=state['branch'],head=state['head'],target={}))
            value['changeId']=change_id
            destination = record_path(thread["id"], value["id"])
            atomic_json(destination, value)
            process=None
            try:
                validate_live()
                process = W.launch(ws, argv, kind="preview",on_finished=history.completion(thread['id'],change_id,ws))
                process.update(
                    source=source, conversationId=thread["id"], previewId=value["id"]
                )
                atomic_json(W.record_path(process["id"]), process)
                value.update(processId=process["id"], state="not_ready")
                atomic_json(destination, value)
            except (OSError, W.Problem) as error:
                stop_sent=False
                if process is not None:
                    value['processId']=process['id']
                    try:
                        W.stop_process(process['id'])
                        stop_sent=True
                    except (OSError,W.Problem,TimeoutError):pass
                if process is None:
                    try:history.mark_unknown(thread['id'],change_id)
                    except Exception:pass
                value.update(state="unknown" if process is not None else "failed", error=str(error))
                atomic_json(destination, value)
                return {
                    "ok": False,
                    "preview": value,
                    "error": (("应用已启动但登记失败，已请求停止自有进程，请核对记录：" if stop_sent else "应用已启动但登记失败，停止回执尚未确认，请核对自有进程：") if process is not None else "应用进程未启动：") + str(error),
                }
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            projected = inspect(thread["id"], value["id"], include_version=False)
            if projected["state"] in {"ready", "expired", "unknown", "failed"}:
                break
            time.sleep(0.1)
        if projected["state"] == "not_ready":
            # Recheck persisted readiness before retiring a startup. A UI probe
            # may have observed readiness while this request was waiting.
            projected = inspect(thread["id"], value["id"], include_version=False)
            if projected["state"] == "not_ready" and not destination.with_suffix(".ready").exists():
                value["startupTimedOut"] = True
                try:
                    W.stop_process(process["id"])
                finally:
                    atomic_json(destination, value)
        projected = inspect(thread["id"], value["id"])
        return {
            "ok": projected["state"] == "ready",
            "preview": projected,
            "changeId": change_id,
            "note": "仅表示进程、端口归属和 HTTP 就绪，不代表功能验收通过",
        }
    except (W.Problem, OSError, ValueError, KeyError, TimeoutError,sqlite3.Error) as error:
        return {
            "ok": False,
            "code": getattr(error, "code", "preview_failed"),
            "error": getattr(error, "message", str(error)),
        }


# 列出或读取原预览，下载子路由交给 `downloads.py` 处理。
def route(ident, parts, method, body,query=None):
    if len(parts)>=2 and parts[1]=='downloads':
        from . import downloads
        return downloads.route(ident,parts[0],parts[2:],method,query or {},body)
    if not parts and method == "GET":
        return 200, list_previews(ident)
    if len(parts) == 1 and method == "GET":
        return 200, inspect(ident, parts[0])
    if len(parts) == 2 and parts[1] == "authority" and method == "GET":
        return 200, inspect(ident, parts[0], include_version=False)
    if len(parts) == 2 and parts[1] == "stop" and method == "POST":
        return 200, stop(ident, parts[0])
    raise W.Problem("not_found", "没有此预览命令", 404)
