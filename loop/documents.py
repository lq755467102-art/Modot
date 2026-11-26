"""Owned, bounded document jobs with attempt identities and source locators."""

# 主要引用方包括 `projects.py` 和 `attachments.py`。

import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import uuid
import time
from . import workbench as W, projects
from .filelock import atomic_json

FORMATS = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
FORMAT_BY_MIME = {mime: name for name, mime in FORMATS.items()}

_jobs = {}
_guard = threading.Lock()
_slots = threading.BoundedSemaphore(2)


def _memory_job(process):
    if os.name != "nt":
        return None
    from . import isolate

    lib = isolate._lib()
    kernel = lib["k32"]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise OSError("无法建立解析资源限制")
    limits = lib["EXT"]()
    limits.BasicLimitInformation.LimitFlags = (
        isolate.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | isolate.JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | isolate.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    )
    limits.BasicLimitInformation.ActiveProcessLimit = 1
    limits.ProcessMemoryLimit = 256 * 1024 * 1024
    if not kernel.SetInformationJobObject(
        handle,
        isolate.JobObjectExtendedLimitInformation,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ) or not kernel.AssignProcessToJobObject(handle, int(process._handle)):
        kernel.CloseHandle(handle)
        raise OSError("无法应用解析内存限制，未处理文档")
    return (kernel, handle)


def start(ident, rid):
    item = projects.conversation(ident).get("attachments", {}).get(rid)
    if not item or item["permission"] != "read":
        raise W.Problem("not_found", "资料不存在", 404)
    file_format = FORMAT_BY_MIME.get(item["mime"])
    if not file_format:
        raise W.Problem("unsupported_document", "此资料没有可用的文档或表格解析器")
    cancel(ident, rid)
    with _guard:
        if len(_jobs) >= 32:
            W.change_registry(
                lambda data: data["threads"][ident]["attachments"][rid].update(
                    document={
                        "state": "failed",
                        "attempt": uuid.uuid4().hex,
                        "sourceHash": item["hash"],
                        "error": "解析队列已满，请稍后重新解析",
                    }
                )
            )
            raise W.Problem("parser_busy", "解析队列已满，请稍后重试", 429)
    token = uuid.uuid4().hex
    state = {
        "attempt": token,
        "state": "queued",
        "sourceHash": item["hash"],
        "created": time.time(),
    }

    def update(data):
        data["threads"][ident]["attachments"][rid]["document"] = state

    W.change_registry(update)
    stopped = threading.Event()
    job = {"cancel": stopped, "process": None}
    key = (str(W.home().resolve()), ident, rid)
    root = Path(item["path"])
    result_path = root / "parse-result.json"

    def publish(value):
        with W._lock:
            current = projects.conversation(ident).get("attachments", {}).get(rid)
            if not current or current.get("document", {}).get("attempt") != token:
                return
            summary = dict(value)
            summary.pop("segments", None)
            if value.get("state") in {"ready", "partial"}:
                if stopped.is_set() or current["permission"] != "read":
                    return
                atomic_json(result_path, dict(value, attempt=token))
                summary["count"] = len(value.get("segments", []))
            W.change_registry(
                lambda data: data["threads"][ident]["attachments"][rid].update(
                    document=dict(
                        summary,
                        attempt=token,
                        sourceHash=item["hash"],
                        created=state["created"],
                    )
                )
            )

    def run():
        process = None
        memory = None
        acquired = False
        terminal = None
        try:
            while not acquired:
                if stopped.is_set():
                    terminal = {"state": "cancelled"}
                    return
                acquired = _slots.acquire(timeout=0.1)
            if stopped.is_set():
                terminal = {"state": "cancelled"}
                return
            publish({"state": "running"})
            if stopped.is_set():
                terminal = {"state": "cancelled"}
                return
            request = {
                "path": str(W.file_path({"path": str(root)}, item["file"])),
                "hash": item["hash"],
                "format": file_format,
            }
            from . import parser_process
            process = parser_process.start('document', request)
            with _guard:
                job["process"] = process
            if stopped.is_set():
                terminal = {"state": "cancelled"}
                return
            output, _ = process.communicate(json.dumps(request).encode(), timeout=8)
            if stopped.is_set():
                terminal = {"state": "cancelled"}
            elif process.returncode or len(output) > 1024 * 1024:
                terminal = {
                    "state": "failed",
                    "error": "解析进程触及资源限制或异常结束",
                }
            else:
                terminal = json.loads(output.decode("utf-8"))
        except subprocess.TimeoutExpired:
            terminal = {"state": "failed", "error": "解析超过 8 秒时间上限"}
        except Exception as error:
            terminal = {
                "state": "cancelled" if stopped.is_set() else "failed",
                "error": str(error)[:500],
            }
        finally:
            # No terminal-state publication can wait on API locks until the
            # owned child is dead. Keep its slot until resource cleanup ends.
            try:
                if process and process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
            finally:
                if memory:
                    memory[0].CloseHandle(memory[1])
                if process and process.poll() is None:
                    process.wait(timeout=3)
                if process:
                    for stream in (process.stdin,process.stdout,process.stderr):
                        if stream:stream.close()
                if acquired:
                    _slots.release()
                try:
                    if terminal is not None:
                        publish(terminal)
                finally:
                    with _guard:
                        if _jobs.get(key) is job:
                            _jobs.pop(key, None)

    with _guard:
        _jobs[key] = job
    threading.Thread(target=run, name="document-parser", daemon=True).start()
    return state


def cancel(ident, rid):
    key = (str(W.home().resolve()), ident, rid)
    with _guard:
        job = _jobs.get(key)
        if not job:
            return
        job["cancel"].set()
        process = job["process"]
    if process and process.poll() is None:
        process.kill()
        process.wait(timeout=3)


def reconcile(ident):
    t = projects.conversation(ident)
    with _guard:
        keys = set(_jobs)
    for rid, item in t.get("attachments", {}).items():
        info = item.get("document", {})
        if (
            info.get("state") in {"queued", "running"}
            and time.time() - info.get("created", 0) > 2
            and (str(W.home().resolve()), ident, rid) not in keys
        ):
            W.change_registry(
                lambda data, rid=rid: data["threads"][ident]["attachments"][rid][
                    "document"
                ].update(state="failed", error="解析服务已中断，请重试")
            )


def _table_rows(segments, sheets, locator):
    if locator == "all":
        return segments
    match = re.fullmatch(r"(sheet-[1-9]\d{0,2})(?:-row-([1-9]\d{0,6})|-rows-([1-9]\d{0,6})-([1-9]\d{0,6}))?", locator)
    if not match or not any(sheet["locator"] == match[1] for sheet in sheets):
        raise W.Problem("not_found", "没有这个工作表或行位置", 404)
    sheet, row, start, end = match.groups()
    first = int(row or start or 1)
    last = int(row or end) if row or end else None
    if last is not None and last < first:
        raise W.Problem("invalid_cursor", "行范围的结束位置小于起始位置")
    selected = [segment for segment in segments if segment["sheet"] == sheet
                and segment["row"] >= first and (last is None or segment["row"] <= last)]
    if not selected and (row or start):
        raise W.Problem("not_found", "该行不在已解析范围，请核对工作表的已读取行数", 404)
    return selected



# `attachments.py` 或 `projects.py` 提供导入资料登记。
# 部分解析可读已保留内容，每次读取先核对原件哈希，再用调用方提供的定位和偏移分页。
def read(item, locator=None, offset=0):
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise W.Problem("invalid_cursor", "读取偏移无效")
    info = item.get("document", {})
    if info.get("state") not in {"ready", "partial"}:
        raise W.Problem(
            "document_pending",
            info.get("error") or "文档尚未解析完成，请查看状态或重试",
            409,
        )
    root = Path(item["path"])
    source = W.file_path({"path": str(root)}, item["file"])
    if (
        source.stat().st_size > 2 * 1024 * 1024
        or W.digest(source.read_bytes()) != item["hash"]
    ):
        raise W.Problem("document_changed", "原件已变化，请重新导入", 409)
    target = W.file_path({"path": str(root)}, "parse-result.json")
    result = W.read(target)
    if result.get("sourceHash") != item["hash"] or result.get("attempt") != info.get(
        "attempt"
    ):
        raise W.Problem("document_changed", "解析结果与当前资料不匹配，请重试", 409)
    segments = result.get("segments", [])
    sheets = result.get("sheets")
    locations = [item["locator"] for item in (sheets if sheets is not None else segments)]
    table = {"sheets": sheets} if sheets is not None else {}
    if locator == "locations":
        return {
            "locations": locations[offset : offset + 40],
            "totalLocations": len(locations),
            "nextOffset": offset + 40 if offset + 40 < len(locations) else None,
            **table,
        }
    if sheets is not None:
        if not locator or locator in {item["file"], "."}:
            locator = sheets[0]["locator"] if sheets else "all"
        selected = _table_rows(segments, sheets, locator)
    elif locator and locator not in {item["file"], ".", "all"}:
        selected = [s for s in segments if s["locator"] == locator]
        if not selected:
            raise W.Problem("not_found", "没有这个来源位置", 404)
    else:
        selected = segments
    text = "\n\n".join(
        ("[" + s["locator"] + "]\n" if len(selected) > 1 else "") + s["text"]
        for s in selected
    )
    return {
        "text": text[offset : offset + 4000],
        "nextOffset": offset + 4000 if offset + 4000 < len(text) else None,
        "totalChars": len(text),
        "source": {
            "id": item["id"],
            "hash": item["hash"],
            "locator": locator or "all",
            "parserVersion": result["parserVersion"],
        },
        "locations": locations[:40],
        "nextLocationOffset": 40 if len(locations) > 40 else None,
        "totalLocations": len(locations),
        "warnings": result["warnings"][:20],
        "notes": result.get("notes", []),
        "partial": info["state"] == "partial",
        "contentRole": "source_material",
        **table,
    }
