"""Short-lived VSIX transfer across the desktop bridge's per-request size limit."""
# 主要引用方包括 `app_hosted_extensions.py`。

# 供 `app_hosted_extensions.py` 复用。应用卡片经桌面桥接上载超出单请求容量的 VSIX，再交给安装入口。
import base64
import binascii
import re
import threading
import time
import uuid
from . import workbench as W

CHUNK = 1024 * 1024
_lock = threading.Lock()
# 上传票据绑定应用、槽位、大小和指纹，按顺序积累正文分段。空闲超过租约后由新上传请求回收。
_uploads = {}


# 同一入口负责申请票据及追加分段，`adapter` 由已登记的应用槽位提供上限。
def transfer(parent, adapter, body):
    with _lock:
        now = time.monotonic()
        for key in list(_uploads):
            if _uploads[key]["until"] < now:
                del _uploads[key]
        token = body.get("upload")
        if token is None:
            size, sha = body.get("size"), body.get("sha256")
            if (
                type(size) is not int
                or not 0 < size <= adapter.MAX_ARCHIVE
                or not isinstance(sha, str)
                or not re.fullmatch("[0-9a-f]{64}", sha)
            ):
                raise W.Problem("vsix_capacity", "原包的大小或指纹无效。")
            if len(_uploads) >= 4:
                raise W.Problem("app_host_busy", "已有多个原包正在传输，请稍后重试。", 409)
            token = uuid.uuid4().hex
            _uploads[token] = dict(
                parent=parent, slot=adapter.SLOT, size=size, sha=sha, parts=[], until=now + 90
            )
            return {"upload": token}
        record = _uploads.get(token) if isinstance(token, str) else None
        if not record or (record["parent"], record["slot"]) != (parent, adapter.SLOT):
            raise W.Problem("app_upload", "原包传输已失效，请重新选择。", 409)
        # 依据已接收段数要求下一段顺序，并按剩余字节计算末段长度。只在收到完整分段后续租。
        index, encoded = body.get("index"), body.get("chunk")
        parts = record["parts"]
        length = min(CHUNK, record["size"] - len(parts) * CHUNK)
        if (
            type(index) is not int
            or index != len(parts)
            or length <= 0
            or not isinstance(encoded, str)
            or len(encoded) != (length + 2) // 3 * 4
        ):
            raise W.Problem("app_upload", "原包分段的顺序或大小不符。", 409)
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise W.Problem("app_upload", "原包分段编码无效。") from exc
        if len(raw) != length:
            raise W.Problem("app_upload", "原包分段不完整。")
        parts.append(raw)
        record["until"] = now + 90
        return {"received": index + 1}


# 安装入口按应用、槽位和指纹领取完整原包。大小一致时消费票据，原包内容和实际指纹由槽位安装校验复核。
def finish(parent, slot, token, sha):
    with _lock:
        record = _uploads.get(token) if isinstance(token, str) else None
        if (
            not record
            or (parent, slot, sha) != (record["parent"], record["slot"], record["sha"])
            or record["until"] < time.monotonic()
        ):
            raise W.Problem("app_upload", "原包传输或指纹已变化。", 409)
        raw = b"".join(record["parts"])
        if len(raw) != record["size"]:
            raise W.Problem("app_upload", "原包传输尚未完成。", 409)
        del _uploads[token]
        return raw
