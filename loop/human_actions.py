"""Authenticated user actions: receipt identity, bounded metadata, shared writers.

This identifies the local profile/API authority, not a multi-user login identity.
Bodies (including terminal input, code and model keys) are never retained here.
"""

# 主要引用方包括 `workbench.py` 和 `project_activity.py`。

from contextlib import contextmanager, nullcontext
import json
import os
import re
import sqlite3
import time
import uuid
from . import workbench as W, runtime as RT
from .filelock import FileLock
from .evidence_redaction import redact

PROFESSIONAL = re.compile(
    r"^/api/v2/workspaces/([^/]+)/(file|git|worktree|remove-worktree|pull-request|checks|terminals|debuggers)(?:/|$)"
)


def safe_stop(path, body):
    return (
        path == "/api/v2/lifecycle"
        or bool(
            re.fullmatch(
                r"/api/(?:sessions/[A-Za-z0-9._-]{1,100}|jobs/[A-Za-z0-9._-]{1,100}|v2/processes/[0-9a-f]{32}|v2/conversations/threads/[A-Za-z0-9._-]{1,100}|v2/workspaces/[^/]+/threads/[A-Za-z0-9._-]{1,100})/stop",
                path,
            )
        )
        or bool(
            re.fullmatch(
                r"/api/v2/conversations/threads/[A-Za-z0-9._-]{1,100}/previews/[0-9a-f]{32}/stop",
                path,
            )
        )
        or bool(
            re.fullmatch(r"/api/v2/debug/[0-9a-f]{32}", path)
            and body.get("command") == "disconnect"
        )
    )


def professional(path):
    return PROFESSIONAL.match(path)


@contextmanager
def database():
    db = sqlite3.connect(W.home() / "human-actions.sqlite3", timeout=5)
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS actions(seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, data TEXT NOT NULL)"
        )
        yield db
        db.commit()
    finally:
        db.close()


# `bin/server.py` 在已认证的用户动作进入业务路由前写起始记录。
# `receiptId` 关联请求，初始状态 `unconfirmed` 表示还没有可记录的结果。
def begin(path, body):
    ident = uuid.uuid4().hex
    parts = path.strip("/").split("/")
    row = dict(
        id=ident,
        actor="human",
        source="authenticated_user_api",
        principal="local_profile:"
        + W.digest(str(W.P.data_root().resolve()).encode())[:16],
        path=path,
        receiptId=body.get("_request_id"),
        state="unconfirmed",
        started=time.time(),
        ownerPid=os.getpid(),
        ownerIdentity=RT.identity(os.getpid()),
        workspaceId=parts[3] if len(parts) > 3 and parts[2] == "workspaces" else None,
        conversationId=parts[4]
        if len(parts) > 4 and parts[2:4] == ["conversations", "threads"]
        else None,
        requestVersion=W.digest(
            json.dumps(
                {k: v for k, v in body.items() if k != "_request_id"},
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ),
        notice="人工接口动作与Agent动作分开；接口回执不等于命令完成。单机配置身份不是独立用户登录认证",
    )
    if path.endswith("/input"):
        row["inputBytes"] = len(str(body.get("text", "")).encode())
    if isinstance(body.get("action"), str) and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_-]{0,63}", body["action"]
    ):
        row["operation"] = body["action"]
    if isinstance(body.get("command"), str):
        row["command"] = body["command"][:100]
    if isinstance(body.get("expectedRevision"), str):
        row["expectedRevision"] = body["expectedRevision"][:128]
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] >= 100000:
            raise W.Problem(
                "human_audit_capacity",
                "人工动作记录达到10万项，未开始新的副作用；请先导出核对",
                409,
            )
        db.execute(
            "INSERT INTO actions(id,data) VALUES(?,?)",
            (ident, json.dumps(row, ensure_ascii=False)),
        )
    return ident


# `bin/server.py` 把路由状态码和业务结果交回此处。
# 按 `begin` 的原动作 ID 更新记录，只收录可审查的效果字段。
def finish(ident, code, result):
    if not ident:
        return
    with database() as db:
        record = db.execute("SELECT data FROM actions WHERE id=?", (ident,)).fetchone()
        if not record:
            raise W.Problem(
                "human_audit_missing", "人工动作开始记录缺失，结果需核对", 409
            )
        row = json.loads(record[0])
        effect = {
            k: result[k]
            for k in (
                "id",
                "path",
                "revision",
                "resultRevision",
                "exitCode",
                "state",
                "backup",
                "sha256",
                "sourceCommit",
                "workspaceId",
                "parentId",
                "seq",
                "branch",
                "head",
                "familyTerminationConfirmed",
            )
            if k in result
            and isinstance(result[k], (str, int, float, bool, type(None)))
        }
        if row["path"].endswith(("/checks", "/terminals")) and result.get("id"):
            effect["processId"] = result["id"]
        if row["path"].endswith("/debuggers") and result.get("id"):
            effect["debuggerId"] = result["id"]
        row.update(
            state="acknowledged"
            if code < 400 and code != 202 and result.get("ok") is not False
            else "unconfirmed",
            httpStatus=code,
            finished=time.time(),
            effect=redact(effect),
            responseVersion=W.digest(
                json.dumps(result, ensure_ascii=False, sort_keys=True).encode()
            ),
        )
        if result.get("code"):
            row["resultCode"] = str(result["code"])[:128]
        db.execute(
            "UPDATE actions SET data=? WHERE id=?",
            (json.dumps(row, ensure_ascii=False), ident),
        )


def perform(path, body, sv, call):
    from .legacy_migration import ensure_compatible

    # Original owned stop handles remain usable if metadata becomes incompatible.
    control = safe_stop(path, body)
    if not control:
        ensure_compatible()
    try:
        ident = begin(path, body)
    except (OSError, sqlite3.Error, W.Problem):
        if control:
            code, result = call()
            return code, dict(
                result,
                auditState="unavailable",
                notice="自有停止操作已尝试；人工审计存储不可用，请核对实际状态",
            )
        raise
    try:
        match = professional(path)
        if match:
            workspace = W.workspace(match.group(1))
            file_save = path.endswith("/file")
            root = W.local_writer_root(workspace["path"]) if file_save else W.writer_root(workspace["path"])
            W.guard_workspace_writer(workspace["path"], sv, target_root=root)
            file_lock = (FileLock(W.file_write_lock_path(workspace, body["path"]).with_suffix(".operation.lock"))
                         if file_save else nullcontext())
            with FileLock(W.writer_lock_path(root, canonical=True), shared=file_save), file_lock:
                W.guard_workspace_writer(workspace["path"], sv, target_root=root)
                code, result = call()
                if code == 200 and path.endswith("/file"):
                    from .project_activity import record_human

                    try:
                        record_human(workspace, body, result, ident)
                    except (W.Problem, OSError, sqlite3.Error):
                        result = dict(result, activityState="unavailable")
        else:
            code, result = call()
        finish(ident, code, result)
        # Business receipts (delivery/correction/etc.) already carry their own
        # stable identity. A fresh audit-attempt id must not change an
        # idempotent response when the same business command is retried.
        return code, dict(result, humanActionId=ident) if professional(path) else result
    except W.Problem:
        raise
    except Exception:
        # Dispatch or its acknowledgement may have happened. Preserve the same
        # receipt identity and require reconciliation, never issue a fresh replay.
        return 202, dict(
            state="unknown",
            humanActionId=ident,
            error="人工操作或其结果留底未确认；请核对实际文件/进程及原回执，系统不会自动重放",
        )


def listing(query):
    before = int((query.get("before") or [str(2**63 - 1)])[0])
    with database() as db:
        records = db.execute(
            "SELECT seq,data FROM actions WHERE seq<? ORDER BY seq DESC LIMIT 51",
            (before,),
        ).fetchall()
    items = []
    for seq, data in records[:50]:
        row = json.loads(data)
        row["position"] = seq
        process = (row.get("effect") or {}).get("processId")
        if process:
            try:
                observed = W.process_record(process)
                row["actualProcess"] = {
                    k: observed.get(k)
                    for k in (
                        "id",
                        "state",
                        "exitCode",
                        "started",
                        "finished",
                        "revision",
                        "stopRequested",
                    )
                }
            except (W.Problem, OSError, ValueError):
                row["actualProcess"] = dict(id=process, state="unknown")
        items.append(redact(row))
    return dict(
        items=items,
        hasMore=len(records) > 50,
        nextBefore=records[49][0] if len(records) > 50 else None,
        notice="操作记录保留当时的实际身份与版本；收讫不代表命令成功，unconfirmed不自动重放。正文和终端输入仅保留哈希/长度",
    )
