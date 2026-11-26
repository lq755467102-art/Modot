"""Human preferences and metadata captured at the actual provider request boundary.

No request bodies, credentials or tool-result plaintext are stored here. Hashes
identify the bytes in a request; they do not imply model understanding or delivery.
"""

# 主要引用方包括 `workbench.py` 和 `provider.py`。

from contextvars import ContextVar
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid
from . import workbench as W, projects
from .filelock import FileLock, atomic_json, read_text
from .evidence_redaction import redact

DEFAULTS = dict(facts=True, skills=True)
active_request = ContextVar("context_request_owner", default=None)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def store_path():
    return W.home() / "context-preferences.json"


def load_preferences():
    path = store_path()
    if not path.exists():
        return dict(version=1, scopes={}, changes=[])
    try:
        if path.stat().st_size > 512 * 1024:
            raise ValueError("capacity")
        row = json.loads(read_text(path))
        if (
            row.get("version") != 1
            or not isinstance(row["scopes"], dict)
            or not isinstance(row["changes"], list)
        ):
            raise ValueError("version")
        for value in row["scopes"].values():
            if (
                not isinstance(value, dict)
                or set(value) - set(DEFAULTS)
                or any(type(v) is not bool for v in value.values())
            ):
                raise ValueError("invalid values")
        return row
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise W.Problem(
            "context_config_unavailable",
            "上下文偏好不可读取，原配置保留；没有用默认值覆盖",
            503,
        ) from error


def preferences(ident):
    thread = projects.conversation(ident)
    row = load_preferences()
    layers = [
        ("global", "全局默认"),
        ("project:" + thread["projectId"], "关联项目")
        if thread.get("projectId")
        else (None, "未关联项目"),
        ("conversation:" + ident, "本会话后续运行"),
    ]
    effective = dict(DEFAULTS)
    sources = {key: "产品默认" for key in DEFAULTS}
    displayed = []
    for key, label in layers:
        values = row["scopes"].get(key, {}) if key else {}
        effective.update(values)
        sources.update({k: label for k in values})
        displayed.append(dict(scope=key, label=label, values=values))
    return dict(
        effective=effective,
        sources=sources,
        layers=displayed,
        revision=fingerprint(dict(layers=displayed, changes=len(row["changes"]))),
        notice="只控制后续系统提示中的事实区/技能清单；不清除历史消息，不授予资料读取、写入或命令执行权限",
    )


# `workbench.py` 的会话路由传入用户偏好。
# 空值表示继承，布尔值显式覆盖；受控旁问和子任务的范围由创建方决定。
def configure(ident, body):
    thread = projects.conversation(ident)
    if thread.get("readonlyEvidence") or thread.get("subtaskOf"):
        raise W.Problem("managed_context", "受控旁问和子任务的上下文不能由此扩展", 403)
    if set(body) - {"scope", "expectedRevision", "values", "_request_id"}:
        raise W.Problem(
            "invalid_context_settings", "上下文偏好不接受权限或模型凭据字段"
        )
    values = body.get("values")
    if (
        not isinstance(values, dict)
        or set(values) - set(DEFAULTS)
        or any(v is not None and type(v) is not bool for v in values.values())
    ):
        raise W.Problem(
            "invalid_context_settings", "只能设置facts/skills布尔值，null表示继承"
        )
    scope = body.get("scope")
    key = (
        "global"
        if scope == "global"
        else "conversation:" + ident
        if scope == "conversation"
        else "project:" + thread["projectId"]
        if scope == "project" and thread.get("projectId")
        else None
    )
    if not key:
        raise W.Problem("invalid_context_scope", "此设置范围不可用")
    with W._lock, FileLock(store_path().with_suffix(".lock")):
        if body.get("expectedRevision") != preferences(ident)["revision"]:
            raise W.Problem("stale_context_settings", "设置已变化，请刷新后核对", 409)
        row = load_preferences()
        if len(row["changes"]) >= 1000 or (
            key not in row["scopes"] and len(row["scopes"]) >= 2000
        ):
            raise W.Problem(
                "context_settings_capacity",
                "偏好变更记录达到容量，请先导出核对；未覆盖旧值",
                409,
            )
        old = dict(row["scopes"].get(key, {}))
        new = dict(old)
        for name, value in values.items():
            if value is None:
                new.pop(name, None)
            else:
                new[name] = value
        row["scopes"][key] = new
        row["changes"].append(
            dict(
                id=uuid.uuid4().hex,
                actor="human",
                scope=key,
                before=old,
                after=new,
                at=time.time(),
            )
        )
        if len(json.dumps(row).encode()) > 512 * 1024:
            raise W.Problem(
                "context_settings_capacity", "设置记录容量不足，未更改旧配置", 409
            )
        atomic_json(store_path(), row)


def begin(session, root, requested_roles):
    from .execution_policy import binding

    thread = binding(root)
    if not thread:
        return
    session.context_preferences = preferences(thread["id"])
    session.context_project_id = thread.get('projectId')
    session.context_scope = json.loads(json.dumps(thread.get("resourceSnapshot", {})))
    from .projects import selected_import_location
    session.context_import_location = selected_import_location(thread, session.context_scope)
    session.context_sources = []
    from .provider import OpenAICompatProvider

    session.context_role_providers = {}
    for role, profile in (("planner", "HARNESS_PLAN"), ("reviewer", "HARNESS_REVIEW")):
        if not requested_roles.get(role):
            continue
        try:
            session.context_role_providers[role] = OpenAICompatProvider(profile=profile)
        except Exception as error:
            session.context_role_providers[role] = error


def source(session, name, kind, text="", status=None, origin="", note=""):
    if session is None or not hasattr(session, "context_preferences"):
        return
    session.context_sources.append(
        dict(
            name=name,
            kind=kind,
            text=text,
            status=status or ("loaded" if text else "not_loaded"),
            origin=origin,
            note=note,
        )
    )


@contextmanager
def request_owner(session, role):
    token = active_request.set((session, role))
    try:
        yield
    finally:
        active_request.reset(token)


def capture(provider, body):
    owner = active_request.get()
    if not owner:
        return
    session, role = owner
    from .context_manager import assess, capacity_for, fingerprint as request_fingerprint
    # Record metadata only. All role requests reach this common boundary.
    capacity = assess(body, capacity_for(session,provider))
    if budget := getattr(body, "input_budget", None):
        capacity["budget"] = budget
    run = session.run_id
    if (
        not isinstance(run, str)
        or len(run) != 32
        or any(c not in "0123456789abcdef" for c in run)
    ):
        raise W.Problem(
            "context_owner_unknown", "模型请求无法关联实际运行身份，未发送", 409
        )
    directory = Path(session.dir) / "context-requests"
    directory.mkdir(exist_ok=True)
    with FileLock(directory / "capture.lock"):
        existing = list(directory.glob("*.json"))
        if (
            len(existing) >= 4096
            or sum(p.name.startswith(run + "-") for p in existing) >= 512
        ):
            raise W.Problem(
                "context_capacity", "请求快照达到容量，尚未发送；请先导出历史核对", 409
            )
        messages = body.get("messages", [])
        system = "\n".join(
            m.get("content", "")
            for m in messages
            if m.get("role") == "system" and isinstance(m.get("content"), str)
        )
        sources = [
            dict(
                name="实际请求系统消息集合",
                kind="system",
                status="loaded" if system else "not_loaded",
                version=fingerprint(system),
                chars=len(system),
                origin="本次实际请求体",
            )
        ]
        for item in getattr(session, "context_sources", []):
            text = item["text"]
            state = item["status"]
            if state in {"loaded", "clipped"} and text and text not in system:
                if budget and item["kind"] in {"skills", "plugins"} and item["kind"] in budget["deferred"]:
                    from .input_budget import catalogue_excerpt
                    excerpt = catalogue_excerpt(body, item["kind"])
                else:
                    excerpt = None
                state = "clipped" if excerpt else "not_loaded"
                if excerpt:
                    text = excerpt
            sources.append(
                dict(
                    name=item["name"],
                    kind=item["kind"],
                    status=state,
                    origin=item["origin"],
                    note=item["note"],
                    chars=len(text),
                    version=fingerprint(text),
                )
            )
        if role != "executor":
            sources = [
                dict(
                    name="角色专用系统提示",
                    kind="system",
                    status="loaded",
                    version=fingerprint(system),
                ),
                dict(
                    name="执行者专用技能",
                    kind="scope",
                    status="not_loaded",
                    version=fingerprint(""),
                    note="规划/复核角色固定使用独立范围",
                ),
            ]
        from .workspace_instructions import inspection_sources
        sources.extend(inspection_sources(body))
        results = []
        calls = {}
        for message in messages:
            for call in message.get("tool_calls", []) or []:
                calls[call.get("id")] = call.get("function", {})
            if message.get("role") != "tool":
                continue
            function = calls.get(message.get("tool_call_id"), {})
            content = message.get("content")
            try:
                value = json.loads(content) if isinstance(content, str) else {}
            except (ValueError, TypeError):
                value = {}
            if not isinstance(value, dict):
                value = {}
            state = (
                "failed"
                if value.get("ok") is False
                else "clipped"
                if value.get("truncated")
                or isinstance(value.get('spill'),dict) and value['spill'].get('format')=='model-json-v1'
                or value.get("compacted")
                or value.get("out_ref")
                or value.get("truncated_chars")
                or value.get("hasMore")
                or value.get("nextOffset") is not None
                or (
                    "hasMore" not in value and isinstance(value.get("next"), (int, str))
                )
                else "loaded"
            )
            reference = value.get("_harnessEvidence")
            reference = reference if isinstance(reference, dict) else {}
            spill = value.get("spill")
            spill_hash = (
                spill.get("hash") if isinstance(spill, dict) and spill.get("format") == "model-json-v1" else None
            )
            results.append(
                dict(
                    name=function.get("name", "unattributed_tool"),
                    callId=message.get("tool_call_id"),
                    status=state,
                    version=fingerprint(content),
                    actionId=reference.get("actionId"),
                    **({"actionRunId": reference["runId"]} if isinstance(reference.get("runId"), str) else {}),
                    **({"spillHash": spill_hash} if isinstance(spill_hash, str) else {}),
                    note=("实际请求中保留失败状态与头尾预览；中间正文按spill引用回取。" if value.get('ok') is False
                          else "实际请求中的头尾预览；中间正文按spill引用回取。")
                         if isinstance(value.get('spill'),dict) and value['spill'].get('format')=='model-json-v1'
                         else "实际请求中保留的工具结果页；不代表读取完整文件",
                )
            )
        url = urlsplit(str(getattr(provider, "base_url", "")))
        endpoint = (
            (
                url.scheme
                + "://"
                + (url.hostname or "")
                + (":" + str(url.port) if url.port else "")
            )
            if url.hostname
            else "未提供"
        )
        # Preserve the full run prefix for per-run quotas while avoiding a
        # 65-character basename that fails inside deep Windows data roots.
        ident=run+'-'+uuid.uuid4().hex[:16]
        for _ in range(3):
            if not (directory/(ident+'.json')).exists():break
            ident=run+'-'+uuid.uuid4().hex[:16]
        else:raise W.Problem('context_identity','请求快照身份发生冲突，未覆盖原记录',409)
        row = redact(
            dict(
                id=ident,
                runId=run,
                role=role,
                purpose='context-summary' if getattr(session,'context_summary_active',False) else 'dialogue',
                at=time.time(),
                state="prepared",
                notice="实际请求构建快照，不保证网络送达或模型理解；摘要/截断后的正文哈希按实际请求记录",
                model=dict(
                    name=str(getattr(provider, "model", ""))[:256],
                    endpoint=endpoint,
                    source=str(getattr(provider, "model_source", "unknown"))[:64],
                ),
                requestVersion=fingerprint(body),
                capacity=capacity,
                checkpointId=getattr(session, "context_checkpoint_refs", {}).get(role + ":" + request_fingerprint(body)),
                systemVersion=fingerprint(system),
                messageCount=len(messages),
                toolNames=[
                    t.get("function", {}).get("name", "") for t in body.get("tools", [])
                ][:256],
                preferences=getattr(session, "context_preferences", {}),
                resourceScope=getattr(session, "context_scope", {}),
                sources=sources,
                results=results[-200:],
                omittedResults=max(0, len(results) - 200),
            )
        )
        from .memory_runtime import capture as capture_memory
        memory = capture_memory(session, role, body)
        if getattr(session,'context_view_info',None) and role=='executor' and not getattr(session,'context_summary_active',False):
            row['workingView'] = dict(session.context_view_info)
        assembly = getattr(body, 'context_assembly', None)
        if assembly:
            # Local provenance only; never part of the provider request body.
            surface = getattr(body, 'context_surface', {})
            row['assembly'] = dict(version=1, scopeVersion=fingerprint(assembly['scope']),
                contributors=list(assembly['contributors']),
                surfaceVersion=fingerprint((row['requestVersion'], surface.get('nodes', ()))),
                modelMessages=len(messages))
        if memory is not None:
            row["memory"] = memory
        # Resource private paths are unnecessary in an inspection response.
        for resource in row.get("resourceScope", {}).get("resources", []):
            if path := resource.pop("path", None):
                resource["pathVersion"] = W.digest(path.encode())
        if len(json.dumps(row).encode()) > 128 * 1024:
            # Optional per-file provenance cannot reduce the legacy request
            # inspection capacity. The actual messages/hash remain unchanged.
            row['sources']=[s for s in row['sources']
                if s.get('origin')!='实际请求中的文件级项目说明']
        if 'assembly' in row and len(json.dumps(row).encode()) > 128 * 1024:
            # Optional migration provenance must not block an otherwise valid
            # legacy request at the pre-existing inspection capacity boundary.
            row.pop('assembly')
        if len(json.dumps(row).encode()) > 128 * 1024:
            raise W.Problem(
                "context_snapshot_capacity",
                "快照元数据过大，未发送未经记录的模型请求",
                409,
            )
        atomic_json(directory / (ident + ".json"), row)
        if getattr(session,'worker_contract',False) and row['purpose']=='dialogue':
            from .worker_plugins import included
            included(session,body,ident)
        event = session.emit(
            "context_request",
            snapshot_id=ident,
            role=role,
            model=row["model"],
            requestVersion=row["requestVersion"],
        )
        return dict(snapshot_id=ident, runId=run, role=role, purpose=row['purpose'],
                    requestVersion=row['requestVersion'],
                    requestEventSeq=event.get('seq') if isinstance(event,dict) else None)


# 工作间路由传入宿主快照查询。
# `context_manager.checkpoint_page` 分页读取检查点，当前偏好与历史请求各自取值。
def view(ident, query, sv):
    prefs = preferences(ident)
    directory = sv._resolve_sid(ident)
    choices = []
    files = []
    offset = 0
    snapshot = None
    from .context_manager import checkpoint_page
    checkpoints = checkpoint_page(directory, int(query.get("checkpointOffset", ["0"])[0])) if directory else dict(
        checkpoints=[], checkpointOffset=0, checkpointsHasMore=False, totalCheckpoints=0)
    if directory:
        files = list((Path(directory) / "context-requests").glob("*.json"))
        if len(files) > 4096:
            raise W.Problem(
                "context_capacity", "请求历史超出边界，未截断成完整记录", 409
            )
        files.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        selected = query.get("snapshot", [None])[0]
        role = query.get("role", [None])[0]
        run = query.get("run", [None])[0]
        if role is not None and role not in {"planner", "executor", "reviewer"}:
            raise W.Problem("invalid_role", "上下文角色无效")
        offset = max(0, min(4096, int(query.get("offset", ["0"])[0])))
        selected_files = files[offset : offset + 20]
        def read_snapshot(path):
            if path.stat().st_size > 128 * 1024:
                raise W.Problem("context_corrupt", "请求快照超出边界，原文件保留", 409)
            return json.loads(read_text(path))

        rows = {path: read_snapshot(path) for path in selected_files}
        choices = [{key: row[key] for key in ("id", "runId", "role", "at", "model")}
                   for row in rows.values()]
        if selected:
            chosen = next((p for p in files if p.stem == selected), None)
            if chosen:
                snapshot = rows.get(chosen) or read_snapshot(chosen)
        elif files:
            latest = rows.get(files[0]) or read_snapshot(files[0])
            if run or role:
                target_run = run or latest["runId"]
                for path in files:
                    if not path.name.startswith(target_run + "-"):
                        continue
                    row = rows.get(path) or read_snapshot(path)
                    if row["runId"] == target_run and (not role or row["role"] == role):
                        snapshot = row
                        break
            else:
                snapshot = latest
        if selected and snapshot is None:
            raise W.Problem(
                "context_missing", "所选快照缺失，未用当前设置推测旧轮次", 404
            )
    if snapshot is not None:
        from .context_evidence import delivery
        snapshot = dict(snapshot, delivery=delivery(directory, snapshot))
    return dict(
        **checkpoints,
        snapshot=snapshot,
        snapshots=choices,
        offset=offset,
        hasMore=offset + 20 < len(files),
        totalSnapshots=len(files),
        preferences=prefs,
        authority=projects.scope(ident),
        preferenceChanges=load_preferences()["changes"][-20:],
        managementNotice="记忆正文、技能安装和规则裁定仍走原管理边界；这里的偏好不能修改授权或规则正文",
    )


def route(ident, method, body, query, sv):
    if method == "POST":
        configure(ident, body)
    elif method != "GET":
        raise W.Problem("not_found", "上下文接口不存在", 404)
    return 200, view(ident, query, sv)
