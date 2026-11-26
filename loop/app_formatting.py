"""Share an editor's versioned, unsaved document with a VS Code formatting provider.

The workroom owns the draft and the save action. The Agent and the editor can
request the same preview. Only the editor that supplied it can accept it.
"""
# 主要引用方包括 `app_hosted_extensions.py` 和 `extension_host.py`。

# 供 `app_hosted_extensions.py` 挂接格式化槽位。编辑器与 Agent 使用同一份预览，保存仍由工作间处理。
import contextvars
import difflib
import json
from pathlib import Path
import re
import threading
import time
import uuid

from . import extension_host, plugins, project_workroom, workbench as W
from .testing_vsix import archive_info

SLOT = "vscode.formatting/1"
CATEGORY = "Formatters"
ARCHIVE_FILE = "original.vsix"
MARKER = "lib/formatting-v1.ready"
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_TEXT = 256 * 1024
LEASE = 45
_lock = threading.RLock()
# 以编辑器实例索引草稿登记。条目持有会话、文件范围、插件授权版本、租约和当前预览任务。
_editors = {}
# 运行中的原生宿主共用两个名额，任务结束或失败后在 `finish` 中释放。
_hosts = threading.BoundedSemaphore(2)
TOOLS = [
    extension_host.tool("current_document", "读取与本会话绑定的当前工作间文件名和草稿版本。", {}, True),
    extension_host.tool(
        "preview_format",
        "请原扩展格式化此版未保存草稿，返回 previewId。只生成差异，用户在工作间确认。每次运行需批准。",
        {"document_id": "current_document 返回的完整 documentId"},
        False,
    ),
    extension_host.tool(
        "read_format_preview",
        "读取格式化进度和差异。running 时可再读。用户可以在工作间查看完整差异，再应用到草稿并保存。",
        {"preview_id": "preview_format 返回的 previewId"},
        True,
    ),
]


# 由安装与运行入口调用。复用 `testing_vsix.py` 的原包检查，限定格式化类别及整个包的字节上限。
def inspect_archive(raw, destination=None):
    return archive_info(raw, destination, category=CATEGORY, maximum=MAX_ARCHIVE)


# 通过 `extension_host.py` 查找含格式化桥接标记的宿主。
def runtime():
    return extension_host.runtime(MARKER)


# 统一标记草稿版本、文件范围或授权已经变化的回执，提示操作者在当前文件重新预览。
def stale():
    return W.Problem("format_stale", "草稿、文件或授权已变化，请在当前文件重新预览。", 409)


# 从 `plugins.py` 读取实时登记，要求包仍启用并具有运行授权，且版本、所属应用和槽位与本次登记相符。
def check_package(doc):
    pkg = plugins.state()["plugins"].get(doc["package"])
    if (
        not pkg
        or pkg.get("removed")
        or not pkg.get("remoteEnabled")
        or not pkg.get("writeEnabled")
        or pkg["revision"] != doc["revision"]
    ):
        raise W.Problem("plugin_denied", "格式化工具已停用或授权已变化。", 403)
    binding = pkg.get("applicationBinding", {})
    if (binding.get("parentId"), binding.get("slot")) != (doc["parent"], SLOT):
        raise W.Problem("app_slot", "此扩展不属于所选应用的格式化槽位。", 403)
    return pkg


# 调用 `project_workroom.py` 复核最新范围版本。`workbench.py` 解析资源下的路径，要求实际文件与登记路径一致。
def location(doc):
    scope = project_workroom.context(
        doc["conversation"] or None, None if doc["conversation"] else doc["project"]
    )
    root = next((r for r in scope["roots"] if r["resourceId"] == doc["resource"]), None)
    if scope["status"] != "ready" or scope["scopeRevision"] != doc["scope"] or not root:
        raise stale()
    base = Path(root["path"])
    file = W.file_path_from_root(base, doc["path"])
    if base.resolve() != base or base / doc["path"] != file or not file.is_file():
        raise stale()
    return base, file


# 调用方持有 `_lock`，到期后移除登记。运行中的任务会在下一次核对登记身份时发现失效。
def prune():
    now = time.monotonic()
    for key in list(_editors):
        if _editors[key]["expires"] <= now:
            del _editors[key]


# 输入由桌面 `DocumentFormatting.tsx` 提供，登记只有文件、范围与草稿版本，正文在预览任务请求后另行交付。
def register(parent, editor, body):
    fields = ("package", "conversation", "project", "resource", "scope", "path", "language")
    if any(not isinstance(body.get(k), str) for k in fields) or any(len(body[k]) > 500 for k in fields):
        raise W.Problem("format_arguments", "当前编辑器的文件范围不完整。")
    seq, revision = body.get("seq"), body.get("revision")
    relative = body["path"]
    if (
        not re.fullmatch("[a-zA-Z0-9_+-]{1,60}", body["language"])
        or type(seq) is not int
        or not 0 <= seq < 2**53
        or type(revision) is not int
        or not relative
        or re.search(r"[\\:\x00-\x1f]", relative)
        or any(part.lower() in {"", ".", "..", ".git"} for part in relative.split("/"))
    ):
        raise W.Problem("format_arguments", "草稿标识无效。")
    doc = {k: body[k] for k in fields}
    doc.update(
        parent=parent,
        editor=editor,
        seq=seq,
        revision=revision,
        token=f"{editor}.{seq}",
        expires=time.monotonic() + LEASE,
        job=None,
    )
    check_package(doc)
    location(doc)
    with _lock:
        prune()
        old = _editors.get(editor)
        if old:
            if old["seq"] > seq or any(
                old.get(k) != doc[k]
                for k in (
                    "parent",
                    "resource",
                    "scope",
                    "path",
                    "conversation",
                    "project",
                    "package",
                    "revision",
                )
            ):
                raise stale()
            # 重复登记同一版本只续租，以保留已经运行或等待审阅的预览。
            if old["seq"] == seq:
                old["expires"] = doc["expires"]
                return {"documentId": old["token"]}
        if not old and len(_editors) >= 32:
            raise W.Problem("format_capacity", "打开的格式化编辑器已达上限。", 409)
        _editors[editor] = doc
    return {"documentId": doc["token"]}


# 核对内存中的登记对象仍是原对象。新版本即使沿用编辑器 ID，也会使旧任务失效。
def active(doc):
    with _lock:
        if _editors.get(doc["editor"]) is not doc or doc["expires"] <= time.monotonic():
            raise stale()


# Agent 得到限长差异，编辑器使用 `full` 读取完整差异和结果正文，供用户应用到同一版草稿。
def preview_view(job, full=False):
    data = {
        key: job[key]
        for key in ("previewId", "documentId", "path", "state", "provider", "language", "edits", "error")
        if key in job
    }
    if job["state"] == "ready":
        diff = job["diff"]
        data.update(
            changed=bool(diff), diff=diff if full else diff[:8000], truncated=not full and len(diff) > 8000
        )
        if full:
            data["text"] = job["text"]
    return data


# 来源由编辑器实例或 Agent 的运行与动作确定。同来源可拿回已有回执，其他来源收到忙碌回执。
def start(doc, live, origin):
    active(doc)
    package = check_package(doc)
    root, file = location(doc)
    if not runtime():
        raise W.Problem("app_host_unavailable", "请先构建带格式化接口的可选 Theia 宿主。", 409)
    with _lock:
        active(doc)
        old = doc["job"]
        if old and old["state"] in ("running", "ready"):
            if old["origin"] == origin:
                return preview_view(old)
            raise W.Problem("format_busy", "当前草稿已有待处理的格式化预览。", 409)
        if not _hosts.acquire(blocking=False):
            raise W.Problem("app_host_busy", "格式化宿主正忙，请稍后再试。", 409)
        preview_id = uuid.uuid4().hex
        job = dict(
            previewId=preview_id,
            documentId=doc["token"],
            path=doc["path"],
            state="running",
            origin=origin,
            source=threading.Event(),
            done=threading.Event(),
        )
        doc["job"] = job

    # 原生宿主启动、轮询与收结果时都复验运行授权、工作目录及该预览的身份。
    def check():
        live()
        check_package(doc)
        if location(doc) != (root, file):
            raise stale()
        with _lock:
            active(doc)
            if doc["job"] is not job or job["state"] != "running":
                raise stale()

    # 后台任务收取本版草稿后调用 `extension_host.py` 的原生宿主，最终将回执置为可审阅或失败。
    def finish():
        try:
            # Metadata discovery does not read the buffer. The live editor must confirm
            # this exact revision after approval, before the native host can start.
            deadline = time.monotonic() + 15
            while not job["source"].wait(0.35):
                check()
                if time.monotonic() >= deadline:
                    raise W.Problem(
                        "format_editor_unavailable", "当前编辑器未能确认草稿，请打开文件后重试。", 409
                    )
            with _lock:
                source = job.pop("draft")
            receipt = extension_host.run(
                dict(package["applicationBinding"], runId=preview_id),
                package,
                root,
                file,
                check,
                inspect=inspect_archive,
                maximum=MAX_ARCHIVE,
                prefix="modot-app-format-",
                request={"slot": SLOT, "language": doc["language"]},
                draft=source,
                result_limit=2 * 1024 * 1024,
                marker=MARKER,
            )
            output = receipt.get("text")
            if (
                receipt.get("provider") != package["applicationBinding"]["extensionId"]
                or not isinstance(output, str)
                or len(output.encode("utf8")) > MAX_TEXT
            ):
                raise W.Problem("format_result", "格式化结果超出范围或来源不符。", 409)
            # CodeMirror models use LF internally. Keep the comparison and the accepted draft identical.
            output = output.replace("\r\n", "\n").replace("\r", "\n")
            diff = unified(source, output, doc["path"])
            check()
            with _lock:
                job.update(
                    state="ready",
                    text=output,
                    diff=diff,
                    provider=receipt["provider"],
                    edits=receipt["edits"],
                    language=receipt["language"],
                )
        except Exception as exc:
            with _lock:
                job.update(state="failed", error=getattr(exc, "message", str(exc))[:1400])
        finally:
            job["done"].set()
            _hosts.release()

    # 把调用方的上下文传到后台线程，让宿主启动与后续核验用同一运行环境。
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(finish,), daemon=True, name="app-format-" + preview_id[:8]).start()
    return preview_view(job)


# 对同一文件的两版正文生成差异，保留文件末尾有没有换行这一事实。
def unified(before, after, path):
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=path + " (草稿)",
        tofile=path + " (格式化)",
    )
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines
    )


# `app_hosted_extensions.py` 将桌面请求交给这里。`parts` 含编辑器实例与操作，正文和确认需要绑定文档及预览 ID。
def editor_route(parent, parts, method, body):
    """The human-only surface holds the editor instance and grant revision through acceptance."""
    if len(parts) != 2 or not re.fullmatch("[0-9a-f]{32}", parts[0]):
        raise W.Problem("format_arguments", "缺少编辑器身份。")
    editor, op = parts
    if method == "POST" and op == "document":
        return register(parent, editor, body)
    with _lock:
        prune()
        doc = _editors.get(editor)
        if not doc or doc["parent"] != parent:
            raise stale()
        if method == "POST" and op == "close":
            del _editors[editor]
            return {"closed": True}
        check_package(doc)
        # 编辑器轮询续租。任务等待正文时返回 `needsDraft`，由客户端交付当前版本的文本。
        if method == "GET" and op == "status":
            doc["expires"] = time.monotonic() + LEASE
            job = doc["job"]
            return dict(
                documentId=doc["token"],
                previewId=job and job["previewId"],
                state=job and job["state"],
                needsDraft=bool(job and job["state"] == "running" and not job["source"].is_set()),
            )
        if method == "GET" and op == "review" and doc["job"]:
            location(doc)
            return preview_view(doc["job"], full=True)
        if method == "POST" and body.get("documentId") == doc["token"]:
            if op == "preview":
                return start(doc, lambda: None, ("editor", editor))
            job = doc["job"]
            if job and body.get("previewId") == job["previewId"]:
                if op == "source" and job["state"] == "running" and not job["source"].is_set():
                    source = body.get("text")
                    if not isinstance(source, str) or len(source.encode("utf8")) > MAX_TEXT:
                        raise W.Problem("format_capacity", "此格式化槽位支持 256 KiB 以内的草稿。", 413)
                    job["draft"] = source
                    job["source"].set()
                    return {"confirmed": True}
                # 确认或放弃清除后端预览。应用时的撤销、草稿更新和保存分别由桌面入口负责。
                if op == "dismiss" or op == "accept" and job["state"] == "ready":
                    location(doc)
                    doc["job"] = None
                    return {"documentId": doc["token"], "previewId": job["previewId"]}
        raise stale()


# Agent 槽位适配器由 `app_hosted_extensions.py` 创建，沿用 `extension_host.Client` 保存会话、安装包及授权核验回调。
class Client(extension_host.Client):
    # 工具目录只宣告可用操作。真正读取登记、启动和取回结果都走 `request`。
    def tools(self):
        return TOOLS

    # 只接受当前会话中同一授权版本的唯一编辑器，且 `extension_host.py` 验证可执行的目录与文件绑定一致。
    def document(self):
        with _lock:
            prune()
            docs = [
                d
                for d in _editors.values()
                if (d["conversation"], d["package"], d["revision"])
                == (self.ident, self.package["id"], self.package["revision"])
            ]
        if len(docs) != 1:
            raise W.Problem("format_document", "请在跟随本会话的一个工作间窗口打开需要格式化的文件。", 409)
        doc = docs[0]
        if extension_host.checkout(self.ident, doc["resource"], "execute") != location(doc)[0]:
            raise stale()
        return doc

    # 为批准界面生成扩展与目标文件说明。若提交的文档 ID 已变化，先要求重新获取当前文档。
    def destination(self, name, args):
        if name == "preview_format":
            doc = self.document()
            if args != {"document_id": doc["token"]}:
                raise stale()
            return (
                self.package["applicationBinding"]["extensionId"]
                + " · 本机原生扩展 · "
                + str(location(doc)[1])
                + " · 预览当前草稿"
            )
        return self.package["applicationBinding"]["extensionId"]

    # 从 `tools.py` 获取当前 Agent 调用，依次路由发现文档、启动预览和读取结果。
    def request(self, method, params):
        from .tools import current_call

        call = current_call()
        if method != "tools/call" or not self.ident or not call:
            raise W.Problem("app_agent_required", "需要当前 Agent 动作。", 403)
        self.live()
        doc = self.document()
        name, args = params["name"], params["arguments"]
        if name == "current_document" and not args:
            data = dict(documentId=doc["token"], path=doc["path"], resourceId=doc["resource"])
        elif name == "preview_format" and args == {"document_id": doc["token"]}:
            data = start(doc, self.live, ("agent", call["runId"], call["actionId"]))
        elif name == "read_format_preview":
            job = doc["job"]
            if (
                not job
                or job["origin"][:2] != ("agent", call["runId"])
                or args != {"preview_id": job["previewId"]}
            ):
                raise stale()
            # 本次运行可以短暂等待任务，仍在运行时返回当前状态。等待期间如有撤权或草稿变化，取回前会再次发现。
            job["done"].wait(3)
            self.live()
            active(doc)
            with _lock:
                if doc["job"] is not job:
                    raise stale()
                data = preview_view(job)
        else:
            raise stale()
        return dict(
            isError=data.get("state") == "failed",
            content=[dict(type="text", text=json.dumps(data, ensure_ascii=False))],
        )
