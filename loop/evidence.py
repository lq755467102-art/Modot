"""Rebuildable action projection over physical log order, with immutable outputs."""

# 主要引用方包括 `workbench.py` 和 `tools.py`。

import codecs
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from . import workbench as W, projects
from .filelock import FileLock
from .evidence_redaction import redact, redact_tool_arguments

MAX_OUTPUT = 8 * 1024 * 1024
MAX_LINE = 2 * 1024 * 1024
PROJECTION_VERSION = 7
ACTION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
TERMINAL = {
    "failed",
    "completed",
    "cancelled_before_start",
    "waiting_approval",
    "unknown",
}


# `result` 是实际工具回执。
# 动作索引只保存关联审查所需的一组引用与可见状态，原始输出另由 `capture` 保留。
def impact(result):
    if not isinstance(result, dict):
        return {}
    value = {
        key: result[key]
        for key in [
            "code",
            "path",
            "readKind",
            "written",
            "edited",
            "operationId",
            "changeId",
            "backupId",
            "restorationPlanId",
            "restoresOperationId",
            "authorizationMode",
            "idempotentReplay",
            "projectRevision",
            "projectRevisionAfter",
            "revision",
        ]
        if isinstance(result.get(key), (str, int, float, bool))
    }
    if isinstance(result.get("verification"), dict):
        value["verification"] = {
            key: result["verification"][key] for key in ("status", "revision", "operationId", "actionId", "reason")
            if isinstance(result["verification"].get(key), str)
        }
    if isinstance(result.get("imageView"), dict):
        value["imageView"] = result["imageView"]
    elif str(result.get("code", "")).startswith("image_"):
        value["imageView"] = {"retained": False}
    version = result.get("version")
    if (result.get("ok") is True and not result.get("image") and isinstance(result.get("id"), str) and isinstance(version, dict)
            and str(version.get("mime", "")).startswith("image/") and isinstance(version.get("id"), str)):
        value["imageArtifact"] = dict(id=result["id"], versionId=version["id"])
    for key in ["process", "preview", "artifact", "version", "debugger"]:
        if isinstance(result.get(key), dict):
            value[key] = {
                k: result[key][k]
                for k in [
                    "id",
                    "versionId",
                    "revision",
                    "hash",
                    "state",
                    "exitCode",
                    "resourceId",
                    "workspaceId",
                ]
                if k in result[key]
            }
    if "pluginId" in result and isinstance(result.get("tool"), str):
        from .search_sources import citations

        sources = citations(result)
        if sources:
            value["search"] = {**{k: result.get(k) for k in ("pluginId", "fingerprint", "revision", "tool")},
                               "items": sources[:6]}
    return _small(value)


def capture(session, action_id, result, *, tool=None):
    """Record an observed result, never ask the model to reconstruct it later."""
    from .image_evidence import preserve

    preserve(session, result, tool)
    if session is None:
        return {"state": "missing", "reason": "此执行没有持久会话"}
    try:
        if not re.fullmatch("[0-9a-f]{32}", action_id):
            raise ValueError("invalid action identity")
        if isinstance(result, dict) and isinstance(result.get("image"), dict):
            result = {k: v for k, v in result.items() if k not in {"image", "imageView"}}
        data = json.dumps(redact(result), ensure_ascii=False, default=str).encode(
            "utf-8"
        )
        if len(data) > MAX_OUTPUT:
            return {
                "state": "missing",
                "reason": "实际输出超过8MiB保留上限",
                "bytes": len(data),
            }
        root = Path(session.dir) / "evidence-output"
        root.mkdir(exist_ok=True)
        if not root.resolve().is_relative_to(Path(session.dir).resolve()):
            raise ValueError("evidence path escaped session")
        path = W.file_path({"path": str(root)}, action_id + ".json")
        # 原始输出按动作身份排他创建，已留存内容不覆盖；写满/失败则显式标记缺失，不能重建成模型记忆。
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return {
            "state": "retained",
            "id": action_id,
            "hash": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
    except (OSError, ValueError, TypeError, RecursionError) as error:
        return {"state": "missing", "reason": redact(str(error))[:300]}


def directory(ident, sv):
    projects.conversation(ident)
    root = sv._resolve_sid(ident)
    if root is None:
        raise W.Problem("not_found", "会话原始证据目录已不存在", 404)
    return Path(root)


def _small(value, limit=16000):
    safe = redact(value)
    raw = json.dumps(safe, ensure_ascii=False, default=str)
    if len(raw.encode("utf-8")) <= limit:
        return safe
    return {
        "truncated": True,
        "preview": raw.encode("utf-8")[: limit // 2].decode("utf-8", "ignore"),
    }


# `row` 是本模块读入的脱敏事件，`source_ref` 指到同一行原字节。
# 展示输出、图片送达和动作终态分别合并，来源身份不同不应互相替代。
def _merge(db, row, position, source_ref):
    kind = row.get("act")
    if kind not in {"action", "tool", "needs_human", "not_run", "grant_used", "image", "model_output"}:
        return
    aid = row.get("action_id")
    legacy = not isinstance(aid, str) or not ACTION_ID.fullmatch(aid)
    if kind == "image" and legacy:
        return
    if legacy:
        # This is a stable record reference, explicitly not a recovered action ID.
        aid = (
            "legacy-"
            + hashlib.sha256(
                json.dumps(row, sort_keys=True, default=str).encode()
            ).hexdigest()[:32]
        )
    found = db.execute("SELECT data FROM actions WHERE id=?", (aid,)).fetchone()
    if kind == 'model_output':
        # A separate presentation observation must not rewrite execution state
        # or consume the legacy action-event source coverage allowance.
        if not found:return
        value=json.loads(found[0]);meta=row.get('formattedOutput')
        if (value.get('state') not in TERMINAL or value.get('runId')!=row.get('run_id')
                or value.get('role')!=row.get('role') or value.get('tool')!=row.get('tool')
                or not isinstance(meta,dict) or meta.get('id')!=aid
                or meta.get('state')!='retained' or meta.get('format')!='model-json-v1'
                or not re.fullmatch('[0-9a-f]{64}',str(meta.get('hash','')))
                or not re.fullmatch('[0-9a-f]{64}',str(meta.get('accessVersion','')))
                or type(meta.get('bytes')) is not int or not 0<=meta['bytes']<=MAX_OUTPUT):
            return
        ids=meta.get('resourceIds');plugin=meta.get('pluginId')
        if (ids is not None and (not isinstance(ids,list) or not all(isinstance(r,str) for r in ids))
                or plugin is not None and not isinstance(plugin,str)):
            return
        kept={k:meta[k] for k in ('state','id','hash','bytes','format','accessVersion','resourceIds','pluginId','renderer') if k in meta}
        if value.get('formattedOutput') and value['formattedOutput']!=kept:
            value.update(formattedOutputConflict=True,formattedOutputConflictSource=source_ref)
        elif not value.get('formattedOutput'):
            value.update(formattedOutput=kept,formattedOutputSource=source_ref)
        db.execute('UPDATE actions SET data=? WHERE id=?',(json.dumps(value,ensure_ascii=False),aid))
        return
    if kind == "image":
        if found:
            value = json.loads(found[0])
            if (value["state"] == "completed" and value["impact"].get("imageView")
                    and value["runId"] == row.get("run_id") and value["role"] == row.get("role")
                    and "imageDelivery" not in value):
                value["imageDelivery"] = source_ref
                db.execute("UPDATE actions SET data=? WHERE id=?", (json.dumps(value, ensure_ascii=False), aid))
        return
    value = (
        json.loads(found[0])
        if found
        else dict(
            id=aid,
            identityKind="legacy-record" if legacy else "action",
            firstPosition=position,
            state="unknown",
            tool=None,
            role=None,
            runId=None,
            input=None,
            location=None,
            startedAt=None,
            finishedAt=None,
            output=None,
            error=None,
            impact={},
            approvalRefs=[],
            summary=None,
        )
    )
    if row.get('backgroundPending') and value.get('observedTerminal') and value['state']!='unknown':
        return  # A late start acknowledgement cannot replace the final output.
    if row.get('backgroundCheck') and row.get('state') in TERMINAL:
        # A confirmed observation supersedes a transient start-reply error;
        # its original event remains available in the audit trail.
        value['error'] = row.get('error')
    for source, target in [
        ("tool", "tool"),
        ("role", "role"),
        ("run_id", "runId"),
        ("args", "input"),
        ("location", "location"),
        ("startedAt", "startedAt"),
        ("finishedAt", "finishedAt"),
        ("outputEvidence", "output"),
        ("error", "error"),
    ]:
        if row.get(source) is not None:
            if source == "args":
                value[target] = _small(row[source])
            elif source in {"startedAt", "finishedAt"}:
                value[target] = (
                    row[source] if type(row[source]) in {int, float} else None
                )
            elif source == "outputEvidence":
                meta = row[source]
                value[target] = (
                    {
                        k: meta[k]
                        for k in ["state", "id", "hash", "bytes", "reason"]
                        if k in meta and isinstance(meta[k], (str, int))
                    }
                    if isinstance(meta, dict)
                    else None
                )
            else:
                value[target] = str(row[source])[:4000]
    # 显式动作状态比旧 tool.ok 推断优先；同级已知终态冲突记 unknown，迟到的非终态不能回退终态。
    state = "waiting_approval" if kind == "needs_human" else row.get("state")
    priority = 2
    if kind == "tool":
        value["summary"] = str(row.get("out") or row.get("summary") or "")[:4000]
        if state is None and type(row.get("ok")) is bool:
            state = "completed" if row["ok"] else "failed"
            priority = 1
    if state is not None:
        if not isinstance(state, str) or state not in TERMINAL | {
            "running",
            "starting",
            "stopping",
        }:
            state = "unknown"
            value["invalidState"] = True
        old = value["state"]
        old_terminal = value.get("observedTerminal", False)
        old_priority = value.get("statePriority", 0)
        if not old_terminal or (state in TERMINAL and priority >= old_priority):
            if value.get("stateConflict") and priority <= old_priority:
                pass
            elif (
                old_terminal
                and priority == old_priority
                and old in TERMINAL - {"unknown"}
                and state in TERMINAL - {"unknown"}
                and old != state
            ):
                value.update(
                    state="unknown", stateConflict=True, statePriority=priority
                )
            else:
                value.update(state=state, statePriority=priority)
                if priority > old_priority:
                    value.pop("stateConflict", None)
            if state in TERMINAL:
                value["observedTerminal"] = True
    if row.get("approvalRefs"):
        value["approvalRefs"] = list(
            dict.fromkeys(
                value["approvalRefs"]
                + [s[:128] for s in row["approvalRefs"] if isinstance(s, str)]
            )
        )[:20]
    if row.get("impact"):
        value["impact"] = _small(row["impact"])
    missing = [
        key
        for key in [
            "role",
            "runId",
            "input",
            "location",
            "startedAt",
            "finishedAt",
            "output",
        ]
        if value.get(key) is None
    ]
    if legacy:
        missing.append("actionIdentity")
    if not value["approvalRefs"]:
        missing.append("approvalAssociation")
    value["missingFields"] = missing
    # 有界保留来源引用；超出覆盖上限要标记未核实，不能只留下前几条却宣称整条动作已核验。
    references = value.setdefault("sourceRefs", [])
    if len(references) < 8:
        references.append(source_ref)
    else:
        value["sourceUnverified"] = True
        value["missingFields"].append("sourceCoverage")
    value["lastPosition"] = position
    value = redact(value)
    db.execute(
        "INSERT OR REPLACE INTO actions(id,position,tool,role,run,state,data) VALUES(?,?,?,?,?,?,?)",
        (
            aid,
            value["firstPosition"],
            value["tool"],
            value["role"],
            value["runId"],
            value["state"],
            json.dumps(value, ensure_ascii=False),
        ),
    )


@contextmanager
def projection(root):
    path = root / "evidence.sqlite3"
    with FileLock(path.with_suffix(".lock")):
        db = sqlite3.connect(path, timeout=5)
        try:
            db.execute("PRAGMA cache_size=-1024")
            db.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS seen (hash TEXT PRIMARY KEY, seq INTEGER)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS seen_seq ON seen(seq)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY,position INTEGER,tool TEXT,role TEXT,run TEXT,state TEXT,data TEXT)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS action_position ON actions(position)"
            )
            from . import progress
            progress.create(db)
            p = root / "events.jsonl"
            stat = p.stat() if p.exists() else None
            saved = db.execute("SELECT value FROM meta WHERE key='state'").fetchone()
            state = json.loads(saved[0]) if saved else {}
            identity = str((stat.st_dev, stat.st_ino)) if stat else None
            # 索引只是缓存；源文件替换、截断或已索引字节变化时重建动作和进展，旧游标/摘要不能续用。
            prefix_changed = False
            if stat and state.get("anchorBytes"):
                with p.open("rb") as original:
                    prefix_changed = hashlib.sha256(
                        original.read(state["anchorBytes"])
                    ).hexdigest() != state.get("anchorHash")
                    if not prefix_changed and state.get("boundaryBytes"):
                        original.seek(state["offset"] - state["boundaryBytes"])
                        prefix_changed = hashlib.sha256(
                            original.read(state["boundaryBytes"])
                        ).hexdigest() != state.get("boundaryHash")
            if (
                state.get("version") != PROJECTION_VERSION
                or prefix_changed
                or state.get("identity") != identity
                or (
                    stat
                    and (
                        stat.st_size < state.get("offset", 0)
                        or (
                            stat.st_size == state.get("size")
                            and stat.st_mtime_ns != state.get("mtime")
                        )
                    )
                )
            ):
                db.execute("DELETE FROM actions")
                db.execute("DELETE FROM seen")
                db.execute("DELETE FROM progress_runs")
                db.execute("DELETE FROM progress_summaries")
                db.execute("DELETE FROM progress_attempts")
                state = {}
            state.setdefault("offset", 0)
            state.setdefault("records", 0)
            state.setdefault("duplicateCount", 0)
            state.setdefault("malformedCount", 0)
            state.setdefault("outOfOrderCount", 0)
            state.setdefault("maxSeq", 0)
            scanned = 0
            if stat:
                with p.open("rb") as stream:
                    stream.seek(state["offset"])
                    while scanned < 4 * 1024 * 1024:
                        line = stream.readline(MAX_LINE + 1)
                        if not line:
                            break
                        # 普通未完成尾行等追加后重读；超长坏行分块丢弃，避免卡住后续记录或占用无界内存。
                        if not line.endswith(b"\n"):
                            if len(line) > MAX_LINE or state.get("discardingLine"):
                                if not state.get("discardingLine"):
                                    state["malformedCount"] += 1
                                    state["records"] += 1
                                state["discardingLine"] = True
                                state["offset"] = stream.tell()
                                scanned += len(line)
                                continue
                            break
                        state["offset"] = stream.tell()
                        if state.pop("discardingLine", False):
                            scanned += len(line)
                            continue
                        state["records"] += 1
                        scanned += len(line)
                        if len(line) > MAX_LINE:
                            state["malformedCount"] += 1
                            continue
                        try:
                            raw = json.loads(line)
                            if not isinstance(raw, dict):
                                raise ValueError("not an event")
                            seq = (
                                raw.get("seq")
                                if type(raw.get("seq")) is int
                                and 0 < raw["seq"] < 2**63
                                else None
                            )
                            fingerprint = hashlib.sha256(
                                json.dumps(raw, sort_keys=True, default=str).encode()
                            ).hexdigest()
                            # 只去重内容相同的事件；相同 seq 的不同内容仍保留为冲突，物理日志顺序独立于序号。
                            if db.execute(
                                "SELECT 1 FROM seen WHERE hash=?", (fingerprint,)
                            ).fetchone():
                                state["duplicateCount"] += 1
                                continue
                            db.execute(
                                "INSERT INTO seen VALUES(?,?)", (fingerprint, seq)
                            )
                            if seq is not None:
                                if seq < state["maxSeq"]:
                                    state["outOfOrderCount"] += 1
                                state["maxSeq"] = max(state["maxSeq"], seq)
                            else:
                                state["malformedCount"] += 1
                            progress.observe(db,redact(raw),state["records"],dict(offset=state["offset"]-len(line),bytes=len(line),hash=hashlib.sha256(line).hexdigest()))
                            _merge(
                                db,
                                redact(raw),
                                state["records"],
                                dict(
                                    offset=state["offset"] - len(line),
                                    bytes=len(line),
                                    hash=hashlib.sha256(line).hexdigest(),
                                ),
                            )
                        except (ValueError, TypeError, RecursionError):
                            state["malformedCount"] += 1
            seqs = db.execute(
                "SELECT COUNT(DISTINCT seq),MAX(seq) FROM seen WHERE seq IS NOT NULL"
            ).fetchone()
            state["gapCount"] = max(0, (seqs[1] or 0) - seqs[0])
            state["sequenceConflicts"] = db.execute(
                "SELECT COUNT(*) FROM (SELECT seq FROM seen WHERE seq IS NOT NULL GROUP BY seq HAVING COUNT(*)>1)"
            ).fetchone()[0]
            if stat and state["offset"]:
                state["anchorBytes"] = min(4096, state["offset"])
                with p.open("rb") as original:
                    state["anchorHash"] = hashlib.sha256(
                        original.read(state["anchorBytes"])
                    ).hexdigest()
                    state["boundaryBytes"] = min(4096, state["offset"])
                    original.seek(state["offset"] - state["boundaryBytes"])
                    state["boundaryHash"] = hashlib.sha256(
                        original.read(state["boundaryBytes"])
                    ).hexdigest()
            state.update(
                version=PROJECTION_VERSION,
                identity=identity,
                size=stat.st_size if stat else 0,
                mtime=stat.st_mtime_ns if stat else 0,
                catchingUp=bool(stat and state["offset"] < stat.st_size),
            )
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES('state',?)", (json.dumps(state),)
            )
            db.commit()
            yield db, state
        finally:
            db.close()


def revision(value):
    return W.digest(json.dumps({key: child for key, child in value.items() if key not in {"projection", "evidenceRevision", "imageDelivery"}}, sort_keys=True, ensure_ascii=False).encode())


def detail(ident, aid, sv, *, verify_owner=False):
    if not isinstance(aid, str) or not ACTION_ID.fullmatch(aid):
        raise W.Problem("invalid_evidence", "证据引用无效")
    root = directory(ident, sv)
    with projection(root) as (db, state):
        row = db.execute("SELECT data FROM actions WHERE id=?", (aid,)).fetchone()
        if not row:
            if state["catchingUp"]:
                raise W.Problem(
                    "evidence_indexing",
                    "原始证据仍在补齐索引，尚不能判断引用是否缺失",
                    425,
                )
            raise W.Problem(
                "evidence_missing", "证据不存在或原记录已删除；不会补造关联", 404
            )
        value = json.loads(row[0])
        _verify_source(root, value, db, verify_owner=verify_owner)
        value.update(conversationId=ident, projection=state)
        value["evidenceRevision"] = revision(value)
        return value


def tool_read(root, action_id, run_id, after=0, evidence_revision=None, view='raw'):
    """Retrieve an observed result in the caller's own current resource scope.

    Deliberately has no conversation/path/command selector and never invokes the
    original tool. A readonly discussion keeps its narrower selected reader.
    """
    from .tools import current_call
    from .code_service import RuntimeView

    owner = current_call() or {}
    if view not in ('raw','formatted','input') or type(after) is not int or after < 0:
        raise W.Problem('invalid_view','请选择有效的历史视图和分页位置')
    if not owner.get("sessionId") or not owner.get("runId"):
        raise W.Problem("identity_required", "历史证据读取需要实际执行身份", 403)
    thread = projects.conversation(owner["sessionId"])
    if (thread.get("readonlyEvidence") or
            Path(W.workspace(thread["workspaceId"])["path"]).resolve() != Path(root).resolve()):
        raise W.Problem("wrong_source", "只能回取当前执行会话的历史证据", 403)
    sv = RuntimeView()
    value = detail(thread["id"], action_id, sv)
    if value.get('role') != (owner.get('role') or 'executor'):
        raise W.Problem('wrong_role', '只能读取当前角色有可核实来源的历史动作', 403)
    if not isinstance(run_id, str) or value.get("runId") != run_id:
        check_history_scope(thread["id"], root, value)
        raise W.Problem("wrong_run", "轮次不匹配。该动作的原轮次为 " + str(value.get("runId")), 409)
    if (after and not evidence_revision) or (evidence_revision and evidence_revision != value["evidenceRevision"]):
        raise W.Problem("evidence_changed", "继续读取需使用同一证据版本", 409)
    if view=='formatted':
        from .tool_spill import read_page
        page=read_page(directory(thread['id'],sv), value, after,
            ident=thread['id'], role=owner.get('role'), root=root)
        return dict(ok=True,historicalEvidence=True,source=dict(conversationId=thread['id'],
            actionId=action_id,runId=run_id,role=value.get('role'),state=value.get('state')),**page)
    check_history_scope(thread["id"], root, value)
    if view == 'input':
        data = json.dumps(redact_tool_arguments(value["input"]), ensure_ascii=False).encode('utf-8')
        page = _output_page(data, after, value, complete=True)
    else:
        page = output(thread["id"], action_id, after, sv)
    if page["evidenceRevision"] != value["evidenceRevision"]:
        raise W.Problem("evidence_changed", "读取期间历史证据已变化", 409)
    return dict(ok=True, historicalEvidence=True, source=dict(
        conversationId=thread["id"], actionId=action_id, runId=run_id,
        role=value.get("role"), state=value.get("state")), **page)


def check_history_scope(ident, root, value, allowed=None, *, scopes=None, archive_dir=None):
    """Apply the same live resource checks to history discovery and retrieval."""
    from .code_service import context_resource
    args = value.get("input")
    if not isinstance(args, dict) or args.get("truncated"):
        raise W.Problem("scope_unknown", "历史动作授权范围无法核实，请通过审查入口查看", 403)
    ids = args.get("resource_ids", [])
    if "resource_id" in args:
        ids = [args["resource_id"], *ids] if isinstance(ids, list) else None
    if not isinstance(ids, list) or any(not isinstance(r, str) for r in ids):
        raise W.Problem("scope_unknown", "历史资料范围无效", 403)
    # Unscoped legacy shell/file tools may contain formerly granted material.
    # Their output is not automatically exposed by this new capability.
    safe_metadata = {"inspect_subtasks", "inspect_delivery", "record_delivery_evidence",
                     "publish_artifact", "read_artifact",
                     "record_delegation_progress"}
    if not ids and value.get("tool") not in safe_metadata:
        raise W.Problem("scope_unknown", "历史动作缺少可核实的资料身份", 403)
    if allowed is None:
        allowed = projects.history_read_resources(ident)
    from .code_service import RuntimeView
    key = value.get('runId'), value.get('role')
    if scopes is None:
        scopes = {}
    if key not in scopes:
        if archive_dir is None:
            archive_dir = directory(ident, RuntimeView())
        scopes[key] = projects.retained_run_reads(archive_dir, *key)
    previous = scopes[key]
    current = projects.read_bindings(allowed.values())
    if not ids:
        if previous is None:
            raise W.Problem('scope_unknown', '无法核实该历史动作附带内容的原读取范围', 403)
        if not projects.covers_reads(current, previous):
            raise W.Problem('resource_denied', '该历史动作附带内容已不在本轮读取授权内', 403)
    elif previous is not None and (not set(ids).issubset(previous) or
            not projects.covers_reads(current, {rid: previous[rid] for rid in ids})):
        raise W.Problem('resource_denied', '原操作的资料身份与本轮读取范围不符', 403)
    from .subtasks import validate_child
    for rid in set(ids):
        validate_child(ident, rid)
        resource = allowed.get(rid)
        if not resource:
            raise W.Problem("resource_denied", "该历史资料已不在本轮读取授权内", 403)
        if resource.get("kind") == "code":
            context_resource(root, rid)


def _verify_source(root, value, db, *, verify_owner=False):
    if value.get("sourceUnverified"):
        raise W.Problem(
            "evidence_unverifiable",
            "此动作关联原始记录超出核验上限，请检查原日志；不能作为已核实证据",
            409,
        )
    # 从原日志按字节范围重算指纹；校验失败清空可重建索引并返回冲突，不把缓存继续当证据。
    with (root / "events.jsonl").open("rb") as stream:
        refs = value.get("sourceRefs", [])
        if value.get("imageDelivery"):
            refs = [*refs, value["imageDelivery"]]
        for name in ('formattedOutputSource','formattedOutputConflictSource'):
            if value.get(name):refs=[*refs,value[name]]
        for ref in refs:
            stream.seek(ref["offset"])
            raw = stream.read(ref["bytes"])
            if hashlib.sha256(raw).hexdigest() != ref["hash"]:
                for table in ["actions", "seen", "meta", "progress_runs", "progress_summaries", "progress_attempts"]:
                    db.execute("DELETE FROM " + table)
                db.commit()
                raise W.Problem(
                    "evidence_changed", "原始动作记录已变化，正在重新建立证据索引", 409
                )
            if verify_owner:
                event = json.loads(raw)
                if event.get("action_id") != value["id"] or event.get("run_id") != value["runId"]:
                    raise W.Problem("wrong_source", "引用对应的运行不唯一", 409)


def check_output_access(ident, value):
    from . import action_review

    metadata = (value.get("impact") or {}).get("imageView")
    if metadata and (metadata.get("source") or {}).get("resourceId"):
        from .image_evidence import authorize

        authorize(ident, metadata)
        return

    if value.get("tool") in action_review.SCOPED_OUTPUT_TOOLS:
        review = action_review.graph(ident, value, 0)
        if review["restricted"]:
            raise W.Problem("resource_denied", review.get("reason") or "这次调用的资料已不在读取范围内", 403)


def output(ident, aid, after, sv, *, readable=False, live=False):
    if type(after) is not int or after < 0:
        raise W.Problem("invalid_cursor", "证据输出游标无效")
    value = detail(ident, aid, sv)
    check_output_access(ident, value)
    meta = value.get("output")
    shell = None
    if readable and value.get("tool") == "run_project_shell":
        from .evidence_redaction import terminal_command
        shell = {"command": terminal_command((value.get("input") or {}).get("argv")),
                 "state": value.get("state"), "exitCode": None}
    def process_transcript(process):
        page = W.process_output(process["id"])
        row = page["process"]
        # Only complete lines are publishable while output or a multiline credential is incomplete.
        text = page["text"]
        pending = row["state"] in {"running", "stopping", "starting", "unknown"}
        clipped = page.get("hasMore") or page.get("truncated")
        if pending or clipped:
            text = text.rpartition("\n")[0]
        shell.update(processState=row["state"], exitCode=row.get("exitCode"))
        check_output_access(ident, value)
        return _output_page(redact(text).encode("utf-8"), after, value, terminal=shell,
                            complete=not pending and not clipped,
                            reason="仅显示已保留的部分输出。" if clipped else "正在等待终端退出。" if pending else "")
    if shell is not None and live and value.get("state") == "running":
        process = W.shell_process_for_action(ident, value.get("runId"), aid)
        if process:
            return process_transcript(process)
    if live and value.get("state") == "running" and value.get("tool") == "run_project_check":
        from .code_service import live_check_output

        log = live_check_output(ident, aid, value.get("runId"))
        if log is not None:
            data = redact(log).encode("utf-8")
            return _output_page(data, after, value, complete=False, reason="检查运行中，显示已收到的日志")
    # 摘要只是回退展示，complete 必须为 False；没有原输出时不能把摘要包装成完整执行证据。
    if not meta or meta.get("state") != "retained" or meta.get("id") != aid:
        return {
            **({"terminal": shell} if shell is not None else {}),
            "text": "" if shell is not None else (value.get("summary") or ""),
            "next": 0,
            "hasMore": False,
            "complete": False,
            "reason": "原始输出未保留；这里只是历史摘要",
            "evidenceRevision": value["evidenceRevision"],
        }
    root = directory(ident, sv) / "evidence-output"
    if not root.resolve().is_relative_to(directory(ident, sv).resolve()):
        raise W.Problem("path_escape", "证据路径已经变化", 403)
    path = W.file_path({"path": str(root)}, aid + ".json")
    if not path.is_file() or path.stat().st_size > MAX_OUTPUT:
        raise W.Problem("evidence_missing", "原始输出已删除或超出保留限制", 410)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != meta.get("hash"):
        raise W.Problem("evidence_changed", "原始证据内容已变化，不能作为当时输出", 409)
    if value.get("tool") == "inspect_subtasks":
        receipt = json.loads(data)
        scope = receipt.get("statusScope") if isinstance(receipt, dict) else None
        if isinstance(scope, dict) and scope.get("revision") != projects.scope(ident)["revision"]:
            raise W.Problem("resource_denied", "该子任务回执的读取范围已变化", 403)
    from .image_evidence import TOOLS

    if value.get("tool") in TOOLS:
        original = json.loads(data)
        if isinstance(original, dict) and isinstance(original.get("image"), dict):
            data = json.dumps({key: item for key, item in original.items() if key != "image"}, ensure_ascii=False).encode("utf-8")
    notice = None
    if readable:
        result = json.loads(data)
        if isinstance(result, dict):
            logs = [(key, result[key]) for key in ("text", "stdout", "stderr", "output")
                    if isinstance(result.get(key), str) and result[key]]
            if shell is not None:
                shell.update(exitCode=result.get("exitCode"), error=result.get("error"),
                             timedOut=result.get("code") == "shell_timeout", processState=result.get("processState"))
                if shell["timedOut"]:
                    process = W.shell_process_for_action(ident, value.get("runId"), aid, process_id=result.get("processId"),
                                                         historical=not result.get("processId"))
                    if process:
                        return process_transcript(process)
                data = b""
            if logs:
                if result.get("hasMore") is True:
                    notice = "这次工具回执只保留了部分进程日志。"
                data = "\n\n".join(f"{key}\n{text}" if len(logs) > 1 else text
                                    for key, text in logs).encode("utf-8")
            elif shell is None and len(data) <= 16000:
                data = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
        elif isinstance(result, str):
            data = result.encode("utf-8")
        elif len(data) <= 16000:
            data = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
    metadata = {"reason": notice} if notice else {}
    if shell is not None:
        metadata["terminal"] = shell
    return _output_page(data, after, value, complete=not notice, hash=meta["hash"], **metadata)


def _output_page(data, after, value, *, complete, page_size=4000, **metadata):
    if after > len(data):
        raise W.Problem("invalid_cursor", "证据输出游标超出范围")
    # 输出游标是 UTF-8 字节偏移，页尾保留未完成码点；调用方必须沿 next 续读同一证据版本。
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        text = decoder.decode(data[after : after + page_size], final=False)
    except UnicodeDecodeError:
        raise W.Problem("invalid_cursor", "证据游标不在文字边界")
    consumed = len(data[after : after + page_size]) - len(decoder.getstate()[0])
    cursor = after + consumed
    return {
        "text": text,
        "next": cursor,
        "hasMore": cursor < len(data),
        "complete": complete,
        "bytes": len(data),
        "evidenceRevision": value["evidenceRevision"],
        **metadata,
    }


def action_position(ident, aid, sv):
    selected = detail(ident, aid, sv)
    if not selected.get("runId") or selected["identityKind"] != "action" or not selected.get("tool"):
        return dict(index=None, total=0, complete=False, nearby=[])
    root = directory(ident, sv)
    with projection(root) as (db, state):
        position = selected["firstPosition"]
        criteria = "run=? AND tool IS NOT NULL AND json_extract(data, '$.identityKind')='action'"
        params = (selected["runId"],)
        total = db.execute("SELECT COUNT(*) FROM actions WHERE " + criteria, params).fetchone()[0]
        index = db.execute(
            "SELECT COUNT(*) FROM actions WHERE " + criteria + " AND position<=?", (*params, position)
        ).fetchone()[0]
        start = max(0, min(index - 5, total - 9))
        rows = db.execute(
            "SELECT id,tool,role,state FROM actions WHERE " + criteria + " ORDER BY position LIMIT 9 OFFSET ?",
            (*params, start),
        ).fetchall()
        if not any(row[0] == aid for row in rows):
            raise W.Problem("evidence_changed", "动作顺序正在重新建立，请稍后重试", 409)
        nearby = [
            dict(id=id_, tool=tool, role=role, state=status, index=start+i)
            for i, (id_, tool, role, status) in enumerate(rows, 1)
        ]
        complete = not any(state.get(key) for key in (
            "catchingUp", "gapCount", "malformedCount", "sequenceConflicts",
        ))
        return dict(index=index, total=total, complete=complete, nearby=nearby)


def route(ident, parts, method, q, sv):
    if method != "GET":
        raise W.Problem("readonly_route", "证据接口只读取事实", 405)
    if parts == ["summary", "round"]:
        from . import audit_summary

        return 200, audit_summary.overview(ident, q, sv)
    if parts == ["targets", "resolve"]:
        from .evidence_targets import resolve

        return 200, resolve(ident, q, sv)
    if len(parts) == 1:
        from .audit_summary import title
        from . import action_review

        action = detail(ident, parts[0], sv)
        offset = int((q.get("fileOffset") or ["0"])[0])
        if offset < 0:
            raise W.Problem("invalid_cursor", "调用文件游标无效")
        review = action_review.graph(ident, action, offset)
        if review["restricted"]:
            action.update(input=None, impact={}, summary=None, error=None, output=None)
        return 200, dict(action, displayTitle=title(action), review=review)
    if len(parts) == 2 and parts[1] == "position":
        return 200, action_position(ident, parts[0], sv)
    if len(parts) == 2 and parts[1] == "citation":
        from .answer_citations import citation

        return 200, citation(ident, parts[0], q, sv)
    if len(parts) == 2 and parts[1] == "image":
        from .image_evidence import preview

        return 200, preview(ident, parts[0], sv, full=q.get("full") == ["1"], metadata_only=q.get("meta") == ["1"])
    if len(parts) == 2 and parts[1] == "output":
        after = int((q.get("after") or ["0"])[0])
        return 200, output(ident, parts[0], after, sv, readable=q.get("format") == ["readable"],
                           live=q.get("live") == ["1"])
    if parts:
        raise W.Problem("not_found", "证据路径不存在", 404)
    ascending = q.get("order") == ["asc"]
    clauses = ["position>?" if ascending else "position<?"]
    params = [int((q.get("after") or ["0"])[0]) if ascending else
              int((q.get("before") or [str(2**63 - 1)])[0])]
    for query, column in [("tool", "tool"), ("role", "role"), ("run", "run")]:
        value = (q.get(query) or [""])[0]
        if value:
            if len(value) > 128:
                raise W.Problem("invalid_filter", "筛选值过长")
            clauses.append(column + "=?")
            params.append(value)
    if (q.get("failed") or [""])[0] == "1":
        clauses.append("state='failed'")
    root = directory(ident, sv)
    from . import action_review
    from .audit_summary import title
    allowed = action_review.allowed_resources(ident)
    with projection(root) as (db, state):
        rows = db.execute(
            "SELECT data FROM actions WHERE "
            + " AND ".join(clauses)
            + (" ORDER BY position LIMIT 31" if ascending else " ORDER BY position DESC LIMIT 31"),
            params,
        ).fetchall()
        actions = []
        for row in rows[:30]:
            item = json.loads(row[0])
            if item.get("sourceUnverified"):
                item.update(
                    state="unknown", summary="原始关联超出核验上限；此项不是已核实证据"
                )
            else:
                _verify_source(root, item, db)
            if item.get("tool") in action_review.SCOPED_OUTPUT_TOOLS:
                # Scoped tool summaries may contain source excerpts. Fetch
                # those only through the authorized action detail or output.
                item["summary"] = None
            actions.append(
                dict({
                    key: item[key]
                    for key in [
                        "id",
                        "identityKind",
                        "tool",
                        "role",
                        "runId",
                        "state",
                        "firstPosition",
                        "summary",
                        "missingFields",
                    ]
                }, targetPath=action_review.target_hint(item, allowed), startedAt=item.get("startedAt"),
                    displayTitle=title(item))
            )
        return 200, dict(
            actions=actions,
            hasMore=len(rows) > 30,
            nextBefore=actions[-1]["firstPosition"] if len(rows) > 30 else None,
            **state,
        )
