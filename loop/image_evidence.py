"""Bounded snapshots for image-reading actions, separate from tool logs."""

# 主要引用方包括 `projects.py` 和 `evidence.py`。

import base64
from io import BytesIO
import os
from pathlib import Path
import re
import tempfile
import warnings

from . import workbench as W
from .filelock import FileLock

FORMATS = {".png": ("PNG", "image/png"), ".jpg": ("JPEG", "image/jpeg"),
           ".jpeg": ("JPEG", "image/jpeg"), ".webp": ("WEBP", "image/webp"),
           ".gif": ("GIF", "image/gif")}
MAX_BYTES = 4 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
TOOLS = {"read_project_resource", "inspect_code", "read_image", "read_artifact"}


def decode(data, expected, *, thumbnail=False):
    from PIL import Image, ImageOps

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as image:
            if image.format != expected or image.width * image.height > 20_000_000:
                raise ValueError("image format or dimensions")
            if getattr(image, "n_frames", 1) > 1:
                raise ValueError("animated images are not supported")
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
            if not thumbnail:
                return image.size
            image = ImageOps.exif_transpose(image)
            image.thumbnail((560, 320))
            result = BytesIO()
            image.convert("RGBA").save(result, format="PNG")
            return result.getvalue()


def read(path, *, limit, expected_hash=None, logical_path=None):
    path = Path(path)
    logical = logical_path or path
    format_info = FORMATS.get(logical.suffix.lower())
    if not format_info:
        raise W.Problem("image_invalid", "支持 PNG、JPEG、WebP 和静态 GIF 图片")
    with path.open("rb") as stream:
        data = stream.read(min(limit, MAX_BYTES) + 1)
    if len(data) > min(limit, MAX_BYTES):
        raise W.Problem("image_limit", "图片超过允许的读取体积", 413)
    digest = W.digest(data)
    if expected_hash and digest != expected_hash:
        raise W.Problem("resource_changed", "导入图片已经变化，请重新导入", 409)
    fmt, mime = format_info
    try:
        width, height = decode(data, fmt)
    except ImportError as error:
        raise W.Problem("image_unavailable", "图片解码组件不可用", 503) from error
    except Exception as error:
        raise W.Problem("image_invalid", "图片损坏、格式不符或超出解码尺寸", 415) from error
    return dict(name=logical.name, path=str(logical), bytes=len(data), mime=mime, format=fmt,
                width=width, height=height, hash=digest, data_url=data_url(data, mime))


def data_url(data, mime):
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def store_path(session_dir, digest):
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise W.Problem("image_missing", "图片没有可核对的保留版本", 410)
    directory = Path(session_dir).resolve() / "evidence-images"
    if directory.resolve() != directory:
        raise W.Problem("path_escape", "图片证据目录已经变化", 403)
    target = directory / (digest + ".bin")
    if target.resolve() != target:
        raise W.Problem("path_escape", "图片证据文件已被重定向", 403)
    return target


def preserve(session, result, tool):
    if not isinstance(result, dict) or tool not in TOOLS or not isinstance(result.get("image"), dict):
        return
    image = result["image"]
    metadata = {k: image[k] for k in ("name", "bytes", "hash", "mime", "format", "width", "height", "source") if k in image}
    result["imageView"] = dict(metadata, retained=False)
    if tool == "read_artifact" and result.get("version"):
        result["imageView"].update(artifactId=result["id"], versionId=result["version"]["id"])
        return
    if session is None or not image.get("source") or not image.get("hash"):
        return
    try:
        target = store_path(session.dir, image["hash"])
        data = base64.b64decode(image["data_url"].split(",", 1)[1], validate=True)
        if len(data) > MAX_BYTES or W.digest(data) != image["hash"]:
            return
        with FileLock(Path(session.dir) / "evidence-images.lock"):
            target.parent.mkdir(exist_ok=True)
            previous = target.stat().st_size if target.exists() else 0
            with target.open("rb") if target.exists() else BytesIO() as stored:
                valid = W.digest(stored.read(MAX_BYTES + 1)) == image["hash"]
            if not valid:
                if sum(p.stat().st_size for p in target.parent.glob("*.bin")) - previous + len(data) > MAX_TOTAL:
                    return
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                    staged = Path(stream.name)
                    try:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    except BaseException:
                        stream.close()
                        staged.unlink(missing_ok=True)
                        raise
                try:
                    os.replace(staged, target)
                finally:
                    staged.unlink(missing_ok=True)
            result["imageView"]["retained"] = True
    except (W.Problem, OSError, ValueError, KeyError, IndexError):
        return


def model_message(image, action, run):
    from .provider import Msg

    return Msg("user", [{"type": "text", "text": "（请核对图片「%s」）" % image["name"]},
                        {"type": "image_url", "image_url": {"url": image["data_url"]}}],
               image_action=dict(id=action, run=run, bytes=image.get("bytes"), hash=image.get("hash"), source=image.get("source")))


def for_submission(messages, question, images, attachment_ids=()):
    """Create a request-only user item. The canonical question and per-file receipts keep their identities."""
    from copy import copy
    from .context_manager import ContextBlocked

    if question is None or question.role != "user" or question not in messages:
        raise ContextBlocked("无法核对附件所属的本轮问题，请重新选择附件后发送。")
    index = messages.index(question)
    current = copy(question)
    # The empty host annotation carries identity only and is never serialized to the provider.
    current.context_parts = (*question.context_parts, ("currentSend", ""),
                             *(("submissionAttachment", rid) for rid in attachment_ids))
    if images:
        content = question.content
        blocks = [dict(type="text", text=content)] if isinstance(content, str) else list(content)
        # Keep the source actions on the merged wire row for the local receipt ledger.
        current.context_parts += tuple(("imageAction", image.image_action["id"])
                                       for image in images if image.image_action)
        for image in images:
            blocks.extend(image.content)
        current.content = blocks
    return [*messages[:index], current, *messages[index + 1:]]


def _part_url(part):
    if isinstance(part, dict) and part.get("type") == "image_url":
        image = part.get("image_url")
        if isinstance(image, dict) and isinstance(image.get("url"), str):
            return image["url"]


def request_image_actions(body):
    """Local message annotations follow projection and never enter provider JSON."""
    included = set()
    for index, message in enumerate(body["messages"]):
        content = message.get("content")
        if isinstance(content, list) and any(_part_url(part) for part in content):
            included.update(value for kind, value in getattr(body, "parts", {}).get(index, ())
                            if kind == "imageAction")
    return included


def request_image_urls(messages):
    """Images in the request after provider-specific context projection."""
    urls = set()
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            urls.update(url for part in content if (url := _part_url(part)))
    return urls


def confirm_model_images(emit, requested, messages, role, *, input_images=None):
    confirmed = set()
    for message in requested:
        marker = getattr(message, "image_action", None)
        if (marker and not marker.get("confirmed") and marker["id"] not in confirmed
                and isinstance(message.content, list)
                and any((url := _part_url(part)) and (input_images is None or marker["id"] in input_images)
                        for part in message.content)):
            if marker.get("attachment"):
                emit("attachment_input", attachmentId=marker["id"], run_id=marker["run"], role=role, bytes=marker["bytes"])
            else:
                emit("image", action_id=marker["id"], run_id=marker["run"], role=role, bytes=marker["bytes"])
            confirmed.add(marker["id"])
    for message in messages:
        if (getattr(message, "image_action", None) or {}).get("id") in confirmed:
            message.image_action = dict(message.image_action, confirmed=True)


def drop_revoked_images(ident, messages):
    for message in messages[:]:
        marker = getattr(message, "image_action", None) or {}
        source = marker.get("source") or {}
        try:
            if source.get("artifactId"):
                from .artifacts import artifact

                artifact(ident, source["artifactId"])
            elif source.get("resourceId"):
                authorize(ident, marker)
        except W.Problem:
            messages.remove(message)


def authorize(ident, metadata):
    from . import projects, subtasks

    source = metadata.get("source") or {}
    rid = source.get("resourceId")
    resource = next((item for item in projects.scope(ident)["resources"] if item["id"] == rid), None)
    if resource is None and rid:
        resource = projects.retained_code(ident, rid)
    if (not resource or resource["permission"] != "read" or resource["path"] != source.get("root")
            or (resource.get("kind") == "attachment"
                and resource.get("hash") != source.get("originalHash", metadata.get("hash")))):
        raise W.Problem("resource_denied", "图片资料已不在当前读取范围", 403)
    subtasks.audit_child(ident, rid)


# 工作间用会话与原动作 ID 读取图片。
# 先由 `evidence.py` 确认动作和影响，再从 `artifacts.py` 或原图记录按版本取回；缩略图、原图和元数据共用同一归属校验。
def preview(ident, action, sv, full=False, metadata_only=False):
    from . import evidence

    value = evidence.detail(ident, action, sv)
    impact = value.get("impact") or {}
    tool = value.get("tool")
    image = impact.get("imageView") or {}
    reference = (impact.get("imageArtifact") if tool in {"publish_artifact", "transform_image"} else
                 {"id": image["artifactId"], "versionId": image["versionId"]} if tool == "read_artifact" and image.get("artifactId") else None)
    if value["state"] == "completed" and reference:
        from . import artifacts
        from .subtasks import audit_child

        audit_child(ident)
        artifact = artifacts.artifact(ident, reference["id"])
        version = next((v for v in artifact["versions"] if v["id"] == reference["versionId"]), None)
        args = value.get("input") or {}
        matches = (args.get("artifact_id") == reference["id"] and args.get("version_id") in {None, reference["versionId"]}
                   if tool == "read_artifact" else bool(version and version.get("source", {}).get("actionId") == action))
        if not version or not matches or not version["mime"].startswith("image/"):
            raise W.Problem("image_missing", "没有与此动作对应的图片成果", 410)
        result = dict(name=version.get("title", artifact["title"]), hash=version["hash"],
                      artifactId=reference["id"], versionId=reference["versionId"],
                      edited=tool == "transform_image", modelDelivered=bool(value.get("imageDelivery")))
        if metadata_only:
            if artifacts.project_version(ident, reference["id"], version)["state"] != "ready":
                raise W.Problem("image_missing", "图片成果已不可用", 410)
            return result
        item = artifacts.read_version(ident, reference["id"], reference["versionId"])
        if full and version["bytes"] <= MAX_BYTES:
            return dict(result, dataUrl=item["dataUrl"])
        data = base64.b64decode(item["dataUrl"].split(",", 1)[1])
        fmt = FORMATS[version["extension"]][0]
        return dict(result, dataUrl=data_url(decode(data, fmt, thumbnail=True), "image/png"))
    metadata = impact.get("imageView")
    if value.get("tool") not in TOOLS or not isinstance(metadata, dict):
        raise W.Problem("image_missing", "原图未保留，无法从历史重建", 410)
    if not metadata.get("source"):
        raise W.Problem("image_missing", "图片来源无法核对，未保留预览", 410)
    authorize(ident, metadata)
    if value["state"] != "completed" or not metadata.get("retained"):
        raise W.Problem("image_missing", "这次读取没有可显示的保留图片", 410)
    target = store_path(evidence.directory(ident, sv), metadata["hash"])
    info = dict(name=metadata["name"], hash=metadata["hash"], width=metadata["width"], height=metadata["height"],
                modelDelivered=bool(value.get("imageDelivery")))
    if metadata_only:
        if not target.is_file() or target.stat().st_size != metadata["bytes"]:
            raise W.Problem("image_missing", "保留图片已不可用", 410)
        return info
    try:
        with target.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
    except OSError as error:
        raise W.Problem("image_missing", "保留图片已不可用", 410) from error
    if len(data) > MAX_BYTES or W.digest(data) != metadata["hash"]:
        raise W.Problem("image_changed", "保留图片版本不一致", 409)
    mime = metadata["mime"]
    if not full:
        data, mime = decode(data, metadata["format"], thumbnail=True), "image/png"
    authorize(ident, metadata)
    return dict(info, dataUrl=data_url(data, mime))
