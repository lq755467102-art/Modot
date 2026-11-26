"""Bounded, validated imports owned by one conversation; no arbitrary paths."""

# 主要引用方包括 `workbench.py` 和 `projects.py`。

import base64
import binascii
from io import BytesIO
import os
from pathlib import Path
from subprocess import TimeoutExpired
import uuid
import warnings
import re
from . import documents, projects, video_frames, workbench as W

REVOCATION_NOTICE_LIMIT = 20


def message_attachments(ident, ids):
    """Resolve an explicit message selection to public, conversation-local metadata."""
    if ids is None:
        return []
    if (not isinstance(ids, list) or len(ids) > 20
            or any(not isinstance(rid, str) or not re.fullmatch(r"[a-fA-F0-9-]{32,36}", rid) for rid in ids)
            or len(set(ids)) != len(ids)):
        raise W.Problem("invalid_attachment_selection", "消息附件选择无效")
    if not ids:
        return []
    thread = projects.conversation(ident)
    readable = {resource["id"] for resource in projects.scope(ident)["resources"]
                if resource["kind"] == "attachment" and resource["permission"] == "read"}
    result = []
    for rid in ids:
        item = thread.get("attachments", {}).get(rid)
        if not item or item.get("status") != "ready" or rid not in readable:
            raise W.Problem("attachment_unavailable", "消息中的附件已移除或不可用，请重新选择", 409)
        result.append({key: item[key] for key in ("id", "name", "mime", "bytes", "width", "height") if key in item})
    return result


def message_images(ident, selected, run_id):
    """Read the images from this send, using the normal project scope and content checks."""
    from . import image_evidence

    images = []
    for item in selected:
        if not item["mime"].startswith("image/"):
            continue
        payload = projects.read_resource(ident, item["id"])["image"]
        message = image_evidence.model_message(payload, item["id"], run_id)
        message.image_action["attachment"] = True
        images.append(message)
    return images


# 附件上限从宿主配置读取，解析失败沿用内置上限。
def limit(name, default):
    try:
        return max(1, min(default, int(os.environ.get(name, default))))
    except ValueError:
        return default


# 将附件 ID 定位到受控的独立导入目录。
def import_directory(ident, rid):
    if not isinstance(rid, str) or not re.fullmatch(r"[a-fA-F0-9-]{32,36}", rid):
        raise W.Problem("invalid_import", "导入标识无效")
    directory = W.home().resolve() / "attachments" / ident / rid
    if directory.resolve() != directory:
        raise W.Problem("invalid_path", "资料存储路径无效", 403)
    return directory


# `workbench.py` 的资料路由交入上传正文与导入 ID。
# 相同 ID 复用登记，暂存态关闭读取授权且解析工作随启用流转。
def _import_file(ident, body):
    t = projects.conversation(ident)
    name = body.get("name")
    if (
        not isinstance(name, str)
        or len(name) > 160
        or not name.strip()
        or "/" in name
        or "\\" in name
        or ":" in name
        or name in {".", ".."}
    ):
        raise W.Problem("invalid_attachment", "文件名无效")
    ext = Path(name).suffix.lower()
    file_format = ext[1:]
    if getattr(os.path, "isreserved", lambda value: False)(name):
        raise W.Problem("invalid_attachment", "文件名被系统保留")
    if (
        ext not in {".txt", ".md", ".png", ".jpg", ".jpeg"}
        and file_format not in documents.FORMATS
        and file_format not in video_frames.FORMATS
    ):
        raise W.Problem(
            "unsupported_attachment", "当前支持 TXT、Markdown、PNG/JPEG、PDF、DOCX、CSV、TSV、XLSX、MP4 和 WebM"
        )
    encoded = body.get("data")
    max_bytes = limit("HARNESS_ATTACHMENT_BYTES", 2 * 1024 * 1024)
    if not isinstance(encoded, str) or len(encoded) > ((max_bytes + 2) // 3) * 4:
        raise W.Problem("attachment_limit", "单个资料超出导入体积上限", 413)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise W.Problem("invalid_attachment", "资料编码损坏")
    if not data or len(data) > max_bytes:
        raise W.Problem("attachment_limit", "资料为空或超出体积上限", 413)
    active = list(t.get("attachments", {}).values())
    if len(active) >= limit("HARNESS_ATTACHMENT_COUNT", 20) or sum(
        r["bytes"] for r in active
    ) + len(data) > limit("HARNESS_ATTACHMENT_TOTAL_BYTES", 20 * 1024 * 1024):
        raise W.Problem("attachment_limit", "本会话资料数量或总体积已达上限", 413)
    item = {
        "id": body.get("importId") or uuid.uuid4().hex,
        "name": name,
        "file": name,
        "kind": "attachment",
        "permission": "read",
        "bytes": len(data),
        "hash": W.digest(data),
        "status": "ready",
    }
    root = import_directory(ident, item["id"])
    existing = t.get("attachments", {}).get(item["id"])
    if existing:
        if existing.get("status") == "cancelled":
            raise W.Problem("import_unavailable", "导入已取消，请重新选择文件", 409)
        if existing["hash"] != item["hash"] or existing["name"] != name:
            raise W.Problem("import_conflict", "导入标识对应其他内容", 409)
        return existing
    if body.get("staged") is True:
        item.update(permission="none", status="staged")
    if ext in {".txt", ".md"}:
        try:
            text = data.decode("utf-8-sig")
            if "\x00" in text:
                raise ValueError()
        except (UnicodeError, ValueError):
            raise W.Problem(
                "invalid_attachment", "文本必须是 UTF-8，且不能包含二进制内容"
            )
        item.update(
            mime="text/markdown" if ext == ".md" else "text/plain", preview=text[:4000]
        )
    elif file_format in documents.FORMATS:
        if (ext == ".pdf" and b"%PDF-" not in data[:1024]) or (
            ext in {".docx", ".xlsx"} and not data.startswith(b"PK")
        ):
            raise W.Problem("invalid_document", "文件签名与文档扩展名不符")
        item["mime"] = documents.FORMATS[file_format]
    elif file_format in video_frames.FORMATS:
        if not video_frames.valid_signature(file_format, data):
            raise W.Problem("invalid_video", "视频容器与文件扩展名不符")
        item["mime"] = video_frames.FORMATS[file_format]
    else:
        try:
            from PIL import Image
        except ImportError:
            raise W.Problem("unavailable", "图片校验组件 Pillow 尚未安装", 503)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    expected = "PNG" if ext == ".png" else "JPEG"
                    if image.format != expected:
                        raise ValueError("format")
                    width, height = image.size
                    pixels = width * height
                    if pixels > limit("HARNESS_ATTACHMENT_PIXELS", 20_000_000) or sum(
                        r.get("pixels", 0) for r in active
                    ) + pixels > limit("HARNESS_ATTACHMENT_TOTAL_PIXELS", 40_000_000):
                        raise W.Problem(
                            "attachment_limit", "图片尺寸或解码总预算超限", 413
                        )
                    image.verify()
                with Image.open(BytesIO(data)) as image:
                    image.load()
            item.update(
                mime="image/png" if ext == ".png" else "image/jpeg",
                width=width,
                height=height,
                pixels=pixels,
            )
        except W.Problem:
            raise
        except Exception:
            raise W.Problem("invalid_attachment", "图片损坏或与扩展名不符")
    try:
        root.mkdir(parents=True)
    except FileExistsError as error:
        raise W.Problem(
            "import_unavailable", "导入标识已取消或有未完成的记录，请重新选择文件", 409
        ) from error
    item["path"] = str(root)
    destination = root / name
    try:
        with destination.open("xb") as f:
            f.write(data)

        # 只在同一会话登记表中增加新附件，写入与登记失败时使用同一目录回收。
        def add(registry):
            thread = registry["threads"][ident]
            thread.setdefault("attachments", {})[item["id"]] = item
            if item["permission"] == "read":
                thread.setdefault("resourceIds", []).append(item["id"])

        W.change_registry(add)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    if item["mime"] in documents.FORMAT_BY_MIME and item["permission"] == "read":
        item["document"] = documents.start(ident, item["id"])
    return item


# 列表读取和资料变更各有入口；发布、启用和删除共用会话锁。
def route(ident, method, body):
    if method == "GET":
        return _route(ident, method, body)
    # Publish, activate and remove against the same serialized registry view,
    # including callers that bypass the HTTP dispatch lock.
    with W._lock:
        return _route(ident, method, body)


# 根据请求选择读取、启用、移除或导入。文档重新解析与读取交给 `documents.py`。
def _route(ident, method, body):
    t = projects.conversation(ident)
    if method == "GET":
        documents.reconcile(ident)
        t = projects.conversation(ident)
        return 200, {"attachments": list(t.get("attachments", {}).values())}
    if (body.get("parse") or body.get("cancelParse")) and body.get(
        "expectedAttempt"
    ) is not None:
        target = t.get("attachments", {}).get(
            body.get("parse") or body.get("cancelParse")
        )
        if (
            not target
            or target.get("document", {}).get("attempt") != body["expectedAttempt"]
        ):
            raise W.Problem(
                "parse_changed", "解析任务已变化，请查看当前状态后操作", 409
            )
    if body.get("parse"):
        return 200, {"ok": True, "document": documents.start(ident, body["parse"])}
    if body.get("cancelParse"):
        documents.cancel(ident, body["cancelParse"])
        return 200, {"ok": True}
    if body.get("activate"):
        rid = body["activate"]
        item = t.get("attachments", {}).get(rid)
        if not item or item.get("status") == "cancelled":
            raise W.Problem("not_found", "导入资料不存在或已取消", 404)

        # 从原暂存 ID 激活读取权限，按当前登记状态更新。
        def activate(data):
            thread = data["threads"][ident]
            entry = thread["attachments"][rid]
            entry.update(status="ready", permission="read")
            thread["resourceIds"] = list(
                dict.fromkeys(thread.get("resourceIds", []) + [rid])
            )
            return entry

        entry = W.change_registry(activate)
        if entry["mime"] in documents.FORMAT_BY_MIME:
            entry["document"] = documents.start(ident, rid)
        return 200, entry
    if body.get("preview"):
        item = t.get("attachments", {}).get(body["preview"])
        if not item or item["permission"] != "read":
            raise W.Problem("not_found", "资料不存在或已移除", 404)
        if item["mime"] in documents.FORMAT_BY_MIME:
            return 200, documents.read(item, body.get("locator"), body.get("offset", 0))
        if item["mime"] in video_frames.FORMAT_BY_MIME:
            raise W.Problem("video_range_required", "请在会话中指定时间点或时间段读取视频画面")
        if item["mime"].startswith("image/"):
            from . import image_evidence

            candidate = W.file_path({"path": item["path"]}, item["file"])
            with candidate.open("rb") as source:
                data = source.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024 or W.digest(data) != item["hash"]:
                raise W.Problem("resource_changed", "资料已变化，请重新导入", 409)
            mime = item["mime"]
            if body.get("thumbnail") is True:
                fmt = "PNG" if mime == "image/png" else "JPEG"
                try:
                    data = image_evidence.decode(data, fmt, thumbnail=True)
                except (OSError, ValueError) as error:
                    raise W.Problem("invalid_attachment", "图片预览不可用", 415) from error
                mime = "image/png"
            return 200, {"dataUrl": image_evidence.data_url(data, mime)}
        return 200, {"text": item.get("preview", "")}
    if body.get('download'):
        item=t.get('attachments',{}).get(body['download'])
        if not item or item['permission']!='read':raise W.Problem('not_found','资料不存在或已移除',404)
        candidate=W.file_path({'path':item['path']},item['file'])
        if candidate.stat().st_size>2*1024*1024:raise W.Problem('resource_changed','资料体积已变化',409)
        data=candidate.read_bytes()
        if W.digest(data)!=item['hash']:raise W.Problem('resource_changed','资料内容已变化',409)
        return 200,{'fileName':item['name'],'data':base64.b64encode(data).decode(),'hash':item['hash'],'mime':item['mime'],'protectedRoot':str(W.home().resolve())}
    if body.get("remove"):
        rid = body["remove"]
        if rid not in t.get("attachments", {}):
            # Removed imports keep their directory as a reservation. Reserve
            # it even if cancel arrived before the upload, across reloads.
            import_directory(ident, rid).mkdir(parents=True, exist_ok=True)
            return 200, {"ok": True}

        # 查原附件 ID 后撤销，保留取消所需的会话归属核对。
        def remove(data):
            thread = data["threads"][ident]
            entry = thread["attachments"][rid]
            if entry.get("permission") == "read":
                notices = thread.setdefault("revokedAttachments", [])
                notices.append({key: entry[key] for key in ("id", "name", "mime")})
                del notices[:-REVOCATION_NOTICE_LIMIT]
            entry.update(permission="none", status="cancelled")
            entry.pop("preview", None)
            thread["resourceIds"] = [
                r for r in thread.get("resourceIds", []) if r != rid
            ]

        W.change_registry(remove)

        item = t["attachments"][rid]
        expected = W.home() / "attachments" / ident / rid
        candidate = Path(item["path"]) / item["file"]
        if Path(
            item["path"]
        ).resolve() != expected.resolve() or not candidate.resolve().is_relative_to(
            (W.home() / "attachments").resolve()
        ):
            raise W.Problem(
                "invalid_path", "读取权限已撤销；资料存储路径变化，未删除文件", 409
            )
        try:
            documents.cancel(ident, rid)
            candidate.unlink(missing_ok=True)
            parsed = W.file_path({"path": str(expected)}, "parse-result.json")
            parsed.unlink(missing_ok=True)
        except (OSError, TimeoutExpired) as error:
            raise W.Problem("attachment_cleanup_pending", "资料已取消，请关闭占用文件的程序后重试清理", 409) from error
        W.change_registry(
            lambda data: data["threads"][ident]["attachments"].pop(rid, None)
        )
        return 200, {"ok": True}
    return 200, _import_file(ident, body)
