"""Immutable app-owned outputs. No model-supplied filesystem destinations."""

# 主要引用方包括 `workbench.py` 和 `tools.py`。

import base64
import difflib
import os
from pathlib import Path
import time
import uuid
from . import workbench as W, projects, runtime as RT
from .filelock import FileLock


# 按会话登记和成果 ID 查原条目，版本内容需要另走读取入口。
def artifact(ident, aid):
    item = projects.conversation(ident).get("artifacts", {}).get(aid)
    if not item:
        raise W.Problem("not_found", "当前对话中未找到此成果", 404)
    if item["permission"] != "read":
        raise W.Problem("artifact_revoked", "此成果读取权限已撤销", 403)
    return item


# 从已登记版本构造宿主保留路径，拒绝指向成果目录外的记录。
def path_for(ident, aid, version):
    for value in (aid, version["id"]):
        if (
            not isinstance(value, str)
            or len(value) != 32
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise W.Problem("invalid_artifact", "成果标识无效")
    path = W.home() / "artifacts" / ident / aid / (version["id"] + version["extension"])
    if not path.resolve().is_relative_to(
        (W.home() / "artifacts").resolve()
    ) or not path.resolve().is_relative_to(W.home().resolve()):
        raise W.Problem("path_escape", "成果存储路径已变化", 403)
    return path


# 把存储版本转换成前端可展示的状态摘要，正文与证据由版本接口单独读取。
def project_version(ident, aid, version):
    value = dict(version)
    if value["state"] == "creating":
        owner = value["producer"]
        actual = RT.identity(owner["pid"])
        if actual is None or (actual != "unknown" and actual != owner["identity"]):
            value["state"] = "interrupted"
        elif actual == "unknown":
            value["state"] = "unknown"
    if value["state"] == "ready" and not path_for(ident, aid, version).is_file():
        value["state"] = "missing"
    return value


# 成果发布保留原动作 `source`，如有图片，调用方可先解码并传入 `prepared_image`。
# 进入会话锁后重新核对输入与 `expectedVersion`，再追加版本。
def publish(ident, body, source, *, prepared_image=None, validate_input=None):
    title = str(body.get("title") or "成果")[:120]
    with FileLock(W.home() / ("artifact-" + ident + ".lock")):
        if validate_input:
            validate_input()
        thread = projects.conversation(ident)
        aid = body.get("artifactId")
        current = artifact(ident, aid) if aid else None
        if current and body.get("expectedVersion") != current["versions"][-1]["id"]:
            raise W.Problem("artifact_changed", "成果已有新版本，请核对后再修改", 409)
        entries = thread.get("artifacts", {})
        retained = [v for a in entries.values() for v in a["versions"]]
        if len(entries) >= 20 and not current or len(retained) >= 50:
            raise W.Problem("artifact_limit", "本对话成果或版本数已达上限", 413)
        source_id = body.get("sourceAttachmentId")
        if source_id and body.get("text") is not None:
            raise W.Problem("invalid_artifact", "正文与源资料只能选择一种")
        extra = {}
        if prepared_image is not None:
            data, mime, extra = prepared_image
            if mime not in {"image/png", "image/jpeg"} or len(data) > 2 * 1024 * 1024:
                raise W.Problem(
                    "invalid_image_output", "图片处理输出格式或体积超限", 413
                )
            extension = ".png" if mime == "image/png" else ".jpg"
        elif source_id:
            item = thread.get("attachments", {}).get(source_id)
            selected = thread.get("resourceIds", [])
            if source.get("actor") == "agent":
                selected = [
                    r["id"]
                    for r in thread.get("resourceSnapshot", {}).get("resources", [])
                ]
            if not item or source_id not in selected or item["permission"] != "read":
                raise W.Problem("resource_denied", "输入资料未获本轮读取授权", 403)
            if item["mime"] not in {
                "image/png",
                "image/jpeg",
                "text/plain",
                "text/markdown",
            }:
                raise W.Problem("unsupported_artifact", "此资料请使用另存原件")
            candidate = W.file_path({"path": item["path"]}, item["file"])
            if candidate.stat().st_size > 2 * 1024 * 1024:
                raise W.Problem("artifact_limit", "资料过大", 413)
            data = candidate.read_bytes()
            if W.digest(data) != item["hash"]:
                raise W.Problem("resource_changed", "输入资料已变化", 409)
            mime = item["mime"]
            extension = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "text/plain": ".txt",
                "text/markdown": ".md",
            }[mime]
            extra = {"inputResource": {"id": source_id, "hash": item["hash"]}}
            for key in ("width", "height"):
                if key in item:
                    extra[key] = item[key]
        else:
            text = body.get("text")
            if not isinstance(text, str) or not text.strip() or "\x00" in text:
                raise W.Problem("invalid_artifact", "请输入成果正文")
            data = text.encode("utf-8")
            if len(data) > 1024 * 1024:
                raise W.Problem("artifact_limit", "文本成果最多 1 MiB", 413)
            form = body.get("format", "markdown")
            if form not in {"markdown", "text"}:
                raise W.Problem("invalid_format", "成果格式必须是 Markdown 或纯文本")
            mime = "text/markdown" if form == "markdown" else "text/plain"
            extension = ".md" if form == "markdown" else ".txt"
        if sum(v["bytes"] for v in retained) + len(data) > 25 * 1024 * 1024:
            raise W.Problem("artifact_limit", "成果保留体积已达上限", 413)
        aid = aid or uuid.uuid4().hex
        version = {
            "id": uuid.uuid4().hex,
            "hash": W.digest(data),
            "bytes": len(data),
            "mime": mime,
            "extension": extension,
            "created": time.time(),
            "source": dict(source, taskId=thread["taskId"], threadId=ident),
            "state": "creating",
            "producer": {"pid": os.getpid(), "identity": RT.identity(os.getpid())},
            **extra,
        }
        version["title"] = title
        destination = path_for(ident, aid, version)
        destination.parent.mkdir(parents=True, exist_ok=True)

        def reserve(registry):
            artifacts = registry["threads"][ident].setdefault("artifacts", {})
            item = artifacts.setdefault(
                aid, {"id": aid, "title": title, "permission": "read", "versions": []}
            )
            item["title"] = title
            item["versions"].append(version)

        W.change_registry(reserve)
        try:
            with destination.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            W.change_registry(
                lambda registry: registry["threads"][ident]["artifacts"][aid][
                    "versions"
                ][-1].update(state="ready")
            )
            version = dict(version, state="ready")
        except Exception as error:
            destination.unlink(missing_ok=True)
            W.change_registry(
                lambda registry: registry["threads"][ident]["artifacts"][aid][
                    "versions"
                ][-1].update(state="failed", error=str(error)[:300])
            )
            raise
        return {"id": aid, "title": title, "version": version}


# 按成果和版本读取已保留内容，每次检查读取授权和内容哈希。
def read_version(ident, aid, vid, download=False):
    item = artifact(ident, aid)
    version = next((v for v in item["versions"] if v["id"] == vid), None)
    if not version:
        raise W.Problem("not_found", "成果版本不存在", 404)
    projected = project_version(ident, aid, version)
    if projected["state"] != "ready":
        raise W.Problem(
            "artifact_unavailable",
            "成果状态为 " + projected["state"] + "，不能作为完整交付读取",
            409,
        )
    file = path_for(ident, aid, version)
    if file.stat().st_size != version["bytes"]:
        raise W.Problem("artifact_changed", "成果文件已变化", 409)
    data = file.read_bytes()
    if W.digest(data) != version["hash"]:
        raise W.Problem("artifact_changed", "成果内容与已登记版本不一致", 409)
    title = version.get("title", item["title"])
    result = {
        "id": aid,
        "title": title,
        "version": projected,
        "fileName": title + version["extension"],
    }
    if download:
        result.update(
            data=base64.b64encode(data).decode(), protectedRoot=str(W.home().resolve())
        )
    elif version["mime"].startswith("image/"):
        result["dataUrl"] = (
            "data:" + version["mime"] + ";base64," + base64.b64encode(data).decode()
        )
    else:
        result["text"] = data.decode("utf-8-sig")
    return result


# 从 `tools.current_call` 取得实际动作会话，确认宿主和子任务归属后再发布。
def tool_publish(root, body):
    from .tools import current_call

    context = current_call()
    if not context:
        return {"ok": False, "error": "成果必须关联实际工具动作"}
    data = W.registry()
    for thread in data["threads"].values():
        workspace = data["workspaces"].get(thread["workspaceId"], {})
        if (
            thread.get("projectless")
            and Path(workspace.get("path", "")).resolve() == Path(root).resolve()
            and thread["id"] == context["sessionId"]
        ):
            try:
                from .subtasks import validate_child

                return dict(
                    publish(
                        thread["id"],
                        body,
                        dict(context, actor="agent"),
                        validate_input=lambda: validate_child(thread["id"]),
                    ),
                    ok=True,
                )
            except W.Problem as error:
                return {"ok": False, "code": error.code, "error": error.message}
    return {"ok": False, "error": "此工具无权写入当前会话成果"}


# `tools.current_call` 提供实际工具会话。
# 先核对 `root` 与会话工作区，再按成果和版本读回；有子任务时还经 `subtasks.py` 核验归属。
def tool_read(root, aid, vid=None, offset=0):
    from .tools import current_call

    context = current_call()
    if not context:
        return {"ok": False, "error": "读取成果必须关联实际会话"}
    data = W.registry()
    thread = data["threads"].get(context["sessionId"])
    if (
        not thread
        or not thread.get("projectless")
        or Path(data["workspaces"][thread["workspaceId"]]["path"]).resolve()
        != Path(root).resolve()
    ):
        return {"ok": False, "error": "成果不属于此会话"}
    try:
        from .subtasks import validate_child

        validate_child(thread["id"])
        item = artifact(thread["id"], aid)
        vid = vid or item["versions"][-1]["id"]
        value = read_version(thread["id"], aid, vid)
        if value["version"]["mime"].startswith("image/"):
            return {
                "ok": True,
                "id": aid,
                "version": value["version"],
                "image": {
                    "name": value["title"],
                    "data_url": value["dataUrl"],
                    "bytes": value["version"]["bytes"],
                    "source": {"artifactId": aid},
                },
            }
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise W.Problem("invalid_cursor", "文字偏移无效")
        text = value["text"]
        return {
            "ok": True,
            "id": aid,
            "version": value["version"],
            "text": text[offset : offset + 4000],
            "totalChars": len(text),
            "nextOffset": offset + 4000 if offset + 4000 < len(text) else None,
        }
    except W.Problem as error:
        return {"ok": False, "code": error.code, "error": error.message}


# 按会话处理列出、发布、版本预览、比较和授权。预览偏移以字符计，比较结果按行数和字符上限截断。
def route(ident, parts, method, query, body):
    if not parts:
        if method == "POST":
            return 200, publish(
                ident,
                body,
                {
                    "actor": "user",
                    "actionId": body.get("_request_id") or "user-" + uuid.uuid4().hex,
                    "runId": None,
                },
            )
        values = []
        for value in projects.conversation(ident).get("artifacts", {}).values():
            values.append(
                dict(
                    value,
                    versions=[
                        project_version(ident, value["id"], v)
                        for v in value["versions"]
                    ],
                )
            )
        return 200, {"artifacts": values}
    aid = parts[0]
    if len(parts) == 3 and parts[1] == "versions" and method == "GET":
        download = (query.get("download") or [""])[0] == "1"
        value = read_version(ident, aid, parts[2], download)
        if not download and "text" in value:
            try:
                offset = max(0, int((query.get("offset") or ["0"])[0]))
            except (ValueError, TypeError):
                raise W.Problem("invalid_cursor", "成果预览偏移无效")
            text = value["text"]
            value.update(
                text=text[offset : offset + 8000],
                totalChars=len(text),
                offset=offset,
                nextOffset=offset + 8000 if offset + 8000 < len(text) else None,
            )
        return 200, value
    if len(parts) == 2 and parts[1] == "compare" and method == "POST":
        left = read_version(ident, aid, body["left"])
        right = read_version(ident, aid, body["right"])

        # 归一比较的字符和行数上限，原文是否超出上限由 `truncated` 表示。
        def lines(value):
            return value[:60000].splitlines(True)[:500]

        lt = left.get("text", "")
        rt = right.get("text", "")
        ll = lines(lt)
        rl = lines(rt)
        diff = "".join(
            difflib.unified_diff(ll, rl, fromfile=body["left"], tofile=body["right"])
        )
        truncated = len(diff) > 100000 or "".join(ll) != lt or "".join(rl) != rt
        for value in (left, right):
            if "text" in value:
                text = value["text"]
                value.update(
                    text=text[:8000],
                    totalChars=len(text),
                    nextOffset=8000 if len(text) > 8000 else None,
                )
        return 200, {
            "left": left,
            "right": right,
            "diff": diff[:100000],
            "truncated": truncated,
        }
    if len(parts) == 2 and parts[1] == "permission" and method == "POST":
        value = body.get("permission")
        if (
            value not in {"none", "read"}
            or value == "read"
            and body.get("confirm") is not True
        ):
            raise W.Problem("confirmation", "恢复成果读取需要明确确认")
        if aid not in projects.conversation(ident).get("artifacts", {}):
            raise W.Problem("not_found", "成果不存在", 404)
        W.change_registry(
            lambda data: data["threads"][ident]["artifacts"][aid].update(
                permission=value
            )
        )
        return 200, {"ok": True}
    raise W.Problem("not_found", "没有这个成果接口", 404)
