"""Scoped image processing; only immutable app-owned output is writable."""

# 主要引用方包括 `tools.py`。

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from . import workbench as W, projects, artifacts

_slots = threading.BoundedSemaphore(2)


def tool_transform(
    root,
    *,
    source_attachment_id=None,
    artifact_id=None,
    version_id=None,
    title="处理后的图片",
    crop=None,
    width=None,
    height=None,
    format="png",
):
    from .tools import current_call, current_stop
    from .documents import _memory_job

    context = current_call()
    process = memory = None
    acquired = False
    stopped = current_stop() or (lambda: False)
    try:
        if not context:
            raise W.Problem("identity_required", "图片处理需要实际工具动作身份", 403)
        thread = projects.conversation(context["sessionId"])
        if (
            Path(W.workspace(thread["workspaceId"])["path"]).resolve()
            != Path(root).resolve()
        ):
            raise W.Problem("resource_denied", "图片不属于当前会话", 403)
        if bool(source_attachment_id) == bool(artifact_id):
            raise W.Problem("invalid_input", "请选择一个附件或一个明确成果版本")
        if source_attachment_id:
            item = thread.get("attachments", {}).get(source_attachment_id)
            captured = thread.get("resourceSnapshot", {}).get("resources", [])
            if (
                not item
                or item["permission"] != "read"
                or source_attachment_id not in thread.get("resourceIds", [])
                or source_attachment_id not in {r["id"] for r in captured}
            ):
                raise W.Problem("resource_denied", "输入图片未在本轮授权范围", 403)
            if item["mime"] not in {"image/png", "image/jpeg"}:
                raise W.Problem("unsupported_image", "此资料不是 PNG/JPEG")
            path = W.file_path({"path": item["path"]}, item["file"])
            source = {"id": item["id"], "hash": item["hash"]}
        else:
            if not version_id:
                raise W.Problem("version_required", "处理成果需提供明确 version_id")
            value = artifacts.read_version(thread["id"], artifact_id, version_id)
            item = value["version"]
            if item["mime"] not in {"image/png", "image/jpeg"}:
                raise W.Problem("unsupported_image", "此成果不是图片")
            path = artifacts.path_for(thread["id"], artifact_id, item)
            source = {"id": artifact_id, "versionId": version_id, "hash": item["hash"]}
        deadline = time.monotonic() + 8
        while not acquired:
            if stopped():
                raise W.Problem("cancelled", "图片处理已取消", 409)
            if time.monotonic() > deadline:
                raise W.Problem("image_busy", "图片处理繁忙，请稍后重试", 429)
            acquired = _slots.acquire(timeout=0.1)
        if stopped():
            raise W.Problem("cancelled", "图片处理已取消", 409)
        request = json.dumps(
            {
                "path": str(path),
                "hash": item["hash"],
                "crop": crop,
                "width": width,
                "height": height,
                "format": format,
            }
        ).encode()
        from . import parser_process
        process = parser_process.start('image', json.loads(request))
        deadline = time.monotonic() + 8
        while True:
            if stopped():
                raise W.Problem("cancelled", "图片处理已取消", 409)
            if time.monotonic() > deadline:
                raise W.Problem("image_timeout", "图片处理超过 8 秒上限", 409)
            try:
                output, _ = process.communicate(request, timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                request = None
        if process.returncode or len(output) > 3 * 1024 * 1024:
            raise W.Problem("image_failed", "图片工具异常结束或触及资源限制", 409)
        value = json.loads(output)
        if not value.get("ok"):
            raise W.Problem("image_failed", value.get("error", "图片工具不可用"), 409)
        if stopped():
            raise W.Problem("cancelled", "图片处理已取消", 409)

        def validate_input():
            if stopped():
                raise W.Problem("cancelled", "图片处理已取消", 409)
            if source_attachment_id:
                current = projects.conversation(thread["id"])
                live = current.get("attachments", {}).get(source_attachment_id)
                if (
                    not live
                    or live["permission"] != "read"
                    or live["hash"] != source["hash"]
                    or source_attachment_id not in current.get("resourceIds", [])
                ):
                    raise W.Problem(
                        "resource_denied", "输入资料授权已撤销或版本已变化", 403
                    )
            else:
                artifacts.read_version(thread["id"], artifact_id, version_id)

        metadata = {
            "width": value["width"],
            "height": value["height"],
            "inputResource": dict(source, **value["before"]),
            "operation": {
                "crop": crop,
                "width": width,
                "height": height,
                "format": format,
            },
        }
        # Transforming a historical immutable version forks a new artifact.
        # A concurrent head advance after this observation still conflicts.
        target = artifact_id
        if (
            artifact_id
            and artifacts.artifact(thread["id"], artifact_id)["versions"][-1]["id"]
            != version_id
        ):
            target = None
        published = artifacts.publish(
            thread["id"],
            {"title": title, "artifactId": target, "expectedVersion": version_id},
            dict(context, actor="agent"),
            prepared_image=(base64.b64decode(value["data"]), value["mime"], metadata),
            validate_input=validate_input,
        )
        return dict(published, ok=True)
    except (W.Problem, ImportError, OSError, ValueError) as error:
        return {
            "ok": False,
            "code": getattr(error, "code", "image_unavailable"),
            "error": getattr(error, "message", str(error)),
        }
    finally:
        try:
            if process and process.poll() is None:
                process.kill()
                process.wait(timeout=3)
        finally:
            try:
                if memory:
                    memory[0].CloseHandle(memory[1])
            finally:
                if acquired:
                    _slots.release()
