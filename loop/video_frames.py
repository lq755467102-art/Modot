"""Read a video attachment as a bounded set of timestamped image evidence."""
# 主要引用方包括 `attachments.py` 和 `projects.py`。

import base64
import json
import math
import subprocess
import threading
from pathlib import Path

from . import image_evidence, parser_process, workbench as W


FORMATS = {"mp4": "video/mp4", "webm": "video/webm"}
FORMAT_BY_MIME = {mime: name for name, mime in FORMATS.items()}
_slots = threading.BoundedSemaphore(2)


def valid_signature(file_format, data):
    if file_format == "mp4":
        return data[4:8] == b"ftyp"
    return data.startswith(b"\x1a\x45\xdf\xa3") and b"\x42\x82\x84webm" in data[:1024]


def _range(value):
    invalid = W.Problem("invalid_video_range", "请提供有限的起止秒数，起点不晚于终点；每次可请求 1 至 4 帧")
    if not isinstance(value, dict) or value.keys() - {"start_seconds", "end_seconds", "frame_count"}:
        raise invalid
    start = value.get("start_seconds")
    end = value.get("end_seconds", start)
    count = value.get("frame_count", 1 if start == end else 3)
    if (type(start) not in (int, float) or type(end) not in (int, float)
            or not 0 <= start <= end < math.inf
            or type(count) is not int or not 1 <= count <= 4):
        raise invalid
    return dict(start_seconds=start, end_seconds=end, frame_count=1 if start == end else count)


def _decode(request):
    if not _slots.acquire(timeout=8):
        raise W.Problem("video_busy", "视频抽帧繁忙，请稍后重试", 429)
    process = None
    try:
        process = parser_process.start("video", request)
        output, _ = process.communicate(timeout=8)
        if process.returncode or len(output) > 2 * 1024 * 1024:
            raise W.Problem("video_decode_failed", "视频解析进程异常结束或触及资源限制", 409)
        return json.loads(output)
    except subprocess.TimeoutExpired as error:
        raise W.Problem("video_decode_timeout", "视频解析超过时间上限", 409) from error
    except (OSError, ValueError) as error:
        raise W.Problem("video_decode_failed", "无法读取视频原件或解码结果", 409) from error
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream:
                        stream.close()
        finally:
            _slots.release()


def read(conversation_id, attachment, path, video=None):
    file_format = FORMAT_BY_MIME[attachment["mime"]]
    query = _range(video) if video is not None else None
    request = dict(path=str(path), hash=attachment["hash"], format=file_format, video=query)
    result = _decode(request)
    source = dict(resourceId=attachment["id"], root=attachment["path"], originalHash=attachment["hash"])
    # 解析器退出后再核对原附件授权，把抽帧期间的撤权落实到返回结果前。
    image_evidence.authorize(conversation_id, {"source": source})
    if not result.get("ok"):
        raise W.Problem(result["code"], result["error"], 409)
    locator = "metadata" if query is None else f'{query["start_seconds"]}–{query["end_seconds"]} s'
    payload = dict(ok=True, name=attachment["name"], durationSeconds=result["durationSeconds"],
                   source=dict(id=attachment["id"], hash=attachment["hash"], locator=locator))
    if query is None:
        return dict(payload, readKind="video_metadata", summary="查看视频信息",
                    notice="可为此附件指定 video.start_seconds、end_seconds 和 frame_count 读取画面。")
    data = base64.b64decode(result["image"], validate=True)
    width, height = image_evidence.decode(data, "JPEG")
    image = dict(name=Path(attachment["name"]).stem + " · " + locator + ".jpg", mime="image/jpeg",
                 format="JPEG", width=width, height=height, bytes=len(data), hash=W.digest(data),
                 source=source, data_url=image_evidence.data_url(data, "image/jpeg"))
    return dict(payload, readKind="video_frames", summary="读取视频画面", frames=result["frames"], image=image)
