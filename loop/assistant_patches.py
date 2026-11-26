"""Task-scoped reviewer edits; immutable patches, executor-only application.

Drafts are content-store overlays, not grants to a live host directory. Checks
materialize those overlays read-only in AppContainer. No shell writes enter a
patch. The existing coordinator owns persistence and model-boundary delivery.
"""
# 主要引用方包括 `loop.py` 和 `role_coordination.py`。
from copy import deepcopy
import difflib
import json
from pathlib import Path
import stat
import time
import uuid

from . import change_history as H, code_service as C, leaks, review_units, workbench as W
from .filelock import FileLock
from .provider import Msg
from .tools import Tool, current_call, current_stop, set_stop, set_call, reset_call


def _role(owner, role):
    owner._ensure_running()
    if role == 'reviewer' and owner._review_stop():
        raise W.Problem('cancelled', '本次复核已经停止，保留修复草稿', 409)
    call = current_call() or {}
    if (call.get("role") != role or call.get("sessionId") != owner.session.dir.name
            or call.get("runId") != owner.session.run_id):
        raise W.Problem("repair_authority", "此动作不属于当前角色任务", 403)


def _path(value):
    from .role_coordination import _excluded
    path = review_units.relative_path(value)
    if path == "." or _excluded(path) or any(":" in p or p.endswith((".", " ")) for p in path.split("/")):
        raise W.Problem("repair_scope", "修复范围需要明确的普通源码文件", 403)
    return path


def _text(value):
    if not isinstance(value, str) or len(value.encode("utf8")) > 256 * 1024 or leaks.has_secret(value):
        raise W.Problem("repair_text", "文本过大或包含受保护内容", 400)
    return value


def _target(ws, path):
    # Even a junction pointing *inside* the project could redirect an allowed
    # spelling to another, unauthorized file. Recheck at the actual write too.
    current = Path(ws["path"]).resolve()
    for part in _path(path).split("/"):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if (current.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
            raise W.Problem("repair_scope", "修复范围不能通过链接或目录重定向访问其他文件", 403)
    return W.file_path_from_root(Path(ws["path"]).resolve(), path)


def _store(owner):
    return owner.state.setdefault("assistantRepairs", dict(grants={}, patches={}))


def _grant(owner, grant_id, row=None):
    grant = _store(owner)["grants"].get(grant_id)
    if (not grant or grant["state"] != "active" or grant["binding"] != owner._task_binding()
            or row and (grant["resourceId"] != row["resourceId"] or grant["sourcePath"] != row["sourcePath"]
                        or row.get('repairGrantId', grant_id) != grant_id
                        or grant.get('mode') == 'task' and grant['candidateId'] != row['id']
                           and row.get('repairGrantId') != grant_id)):
        raise W.Problem("repair_authority", "任务修复授权不存在、过期或已撤销", 403)
    _, _, ws, _ = owner._resource(grant["resourceId"], "write")
    if ws["path"] != grant["sourcePath"]:
        raise W.Problem("repair_checkout", "工作区已切换，修复授权不再适用", 409)
    return grant


def task_scope(owner, row, item_id, code=None):
    """Derive an overlay scope from a real candidate finding, never from blueprint prose.

    No model approval or extra repair worker: the reviewer continues in its current
    context. Existing grants remain readable for recovery of older conversations.
    """
    if not owner.executor_led or not row.get('snapshot'):
        return None
    try:
        _, _, ws, _ = owner._resource(row['resourceId'], 'write')
    except W.Problem:
        return None  # A read-only review must still be able to report its findings.
    if ws['path'] != row.get('sourcePath'):
        return None
    with owner.lock:
        item = owner.work.state['items'].get(item_id)
        if not item or item.get('resourceId') != row['resourceId']:
            return None
        with H.database() as db:
            manifest = H.load_snapshot(db, row['snapshot'])
            base_id = row.get('handoff', {}).get('baseline')
            baseline = H.load_snapshot(db, base_id) if base_id else None
        paths = {p for p, meta in manifest['files'].items()
                 if baseline and meta.get('hash') != baseline['files'].get(p, {}).get('hash')}
        if code:
            paths.add(code['path'])
        safe = []
        for path in sorted(paths):
            if manifest['files'].get(path, {}).get('kind') != 'text':
                continue
            try:
                _target(ws, path)
            except W.Problem:
                continue
            safe.append(path)
        if not safe:
            return None
        key = H.version(dict(task=owner._task_binding(), candidate=row['id'], mode='reviewer_repair'))
        store = _store(owner)['grants']
        if key not in store:
            store[key] = dict(id=key, mode='task', candidateId=row['id'], role='reviewer',
                resourceId=row['resourceId'], sourcePath=row['sourcePath'], paths=[],
                goal=owner.task[:2000], itemId=item_id, itemIds=[], binding=owner._task_binding(),
                state='active', drafts={}, created=time.time())
        scope = store[key]
        if scope['state'] != 'active':
            return None
        changed = item_id not in scope['itemIds'] or not set(safe) <= set(scope['paths'])
        scope['paths'] = sorted(set(scope['paths']) | set(safe))
        if item_id not in scope['itemIds']:
            scope['itemIds'].append(item_id)
        item['repairScopeId'] = key
        owner.save()
    if changed:
        owner.session.emit('assistant_repair_started', candidateId=row['id'], itemId=item_id, paths=safe)
    return scope


def _current_scope(owner, row, grant_id=None):
    if grant_id is not None:  # Backward-compatible tools in retained checkpoints.
        return _grant(owner, grant_id, row)['id']
    with owner.lock:
        scope = next((g for g in _store(owner)['grants'].values()
                      if g.get('mode') == 'task' and g.get('candidateId') == row['id']), None)
        if scope:
            return _grant(owner, scope['id'], row)['id']
    raise W.Problem('repair_scope', '先用当前候选的真实代码或失败检查证据登记缺陷；任务范围内可直接修副本，无需执行者授权', 403)


def authorize(owner, resource_id, paths, goal, item_id=None):
    _role(owner, "executor")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 32:
        raise W.Problem("repair_scope", "每个修复任务指定1至32个文件", 400)
    goal = _text(goal).strip()
    if not goal or len(goal) > 2000:
        raise W.Problem("repair_goal", "请明确委派目标", 400)
    paths = sorted(set(_path(p) for p in paths))
    _, _, ws, _ = owner._resource(resource_id, "write")
    for path in paths:
        _target(ws, path)
    with owner.lock:
        if item_id is not None:
            item = owner.work.state["items"].get(item_id)
            if not item or item.get("resourceId") != resource_id:
                raise W.Problem("repair_item", "委派事项不属于本资源", 409)
        for old in _store(owner)["grants"].values():
            if (old["state"] == "active" and old["binding"] == owner._task_binding()
                    and old["resourceId"] == resource_id and old["sourcePath"] == ws["path"] and old["paths"] == paths
                    and old["goal"] == goal and old.get("itemId") == item_id):
                return dict(ok=True, grant=old, reused=True)
        grant = dict(id=uuid.uuid4().hex, role="reviewer", resourceId=resource_id, sourcePath=ws["path"],
                     paths=paths, goal=goal, itemId=item_id, binding=owner._task_binding(),
                     state="active", drafts={}, created=time.time())
        revision = W.current_revision(ws)
        prior = next((r for r in reversed(list(owner.state['candidates'].values()))
                      if r['resourceId'] == resource_id and r.get('sourcePath') == ws['path']
                      and r['revision'] == revision and r.get('snapshot')), None)
        if prior:
            snapshot = prior['snapshot']
        else:
            with H.database() as db:
                snapshot, manifest = H.snapshot(db, ws)
                revision = manifest['workspaceRevision']
        grant['job'] = dict(state='queued', row=dict(id='repair-' + grant['id'], resourceId=resource_id,
            sourcePath=ws['path'], revision=revision, snapshot=snapshot, repairGrantId=grant['id'],
            attempts=1, checks={}))
        _store(owner)["grants"][grant["id"]] = grant
        owner.save()
    owner.session.emit("assistant_repair_authorized", grantId=grant["id"], paths=paths)
    owner._schedule()
    return dict(ok=True, grant=grant)


def revoke(owner, grant_id):
    _role(owner, "executor")
    with owner.lock:
        grant = _store(owner)["grants"].get(grant_id)
        if not grant:
            raise W.Problem("repair_authority", "修复授权不存在", 404)
        grant["state"] = "revoked"
        from .repair_collaboration import update
        for item_id in grant.get('itemIds', []):
            item = owner.work.state['items'].get(item_id, {})
            if item.get('repair', {}).get('role') == 'reviewer':
                update(owner, item_id, 'interrupted', reason='修复范围已撤销')
        owner.save()
    return dict(ok=True)


def view(owner, row=None):
    token = None
    if current_call() is None:
        token = set_call(owner._identity("reviewer" if row else "executor", "assistant_patch_context"))
    try:
        return _view(owner, row)
    finally:
        if token is not None:
            reset_call(token)


def _verification(owner, patch, grant):
    """Project retained candidate evidence; never start or repeat a check here."""
    applied = patch.get("application", {})
    if applied.get("state") == "applied" and applied.get("currentFiles"):
        for candidate in reversed(list(owner.state.get("candidates", {}).values())):
            if (candidate.get("resourceId") != grant["resourceId"] or candidate.get("sourcePath") != grant["sourcePath"]
                    or candidate.get("unitId") or candidate.get("state") != "completed"
                    or candidate.get("verdict") != "pass" or not candidate.get("snapshot")
                    or candidate["revision"] != owner.state.get("dirty", {}).get(grant["resourceId"])):
                continue
            try:
                with H.database() as db:
                    files = H.load_snapshot(db, candidate["snapshot"])["files"]
                if all(files.get(path, {}).get("hash") == rev for path, rev in applied.get("currentFiles", {}).items()):
                    return dict(state="passed", evidenceKind="candidate_review", candidateId=candidate["id"], revision=candidate["revision"])
            except W.Problem:
                continue
    return dict(state="pending", notice="合并或修复自检不代表独立复验；原缺陷仍由现有队列核验关闭")


def _view(owner, row=None):
    grants = []
    with owner.lock:
        for grant in _store(owner)["grants"].values():
            if row and grant["resourceId"] != row["resourceId"]:
                continue
            try:
                _grant(owner, grant["id"], row)
            except W.Problem:
                continue
            candidate = owner.state.get('candidates', {}).get(grant.get('candidateId'), {})
            grants.append(dict({k: grant[k] for k in ("id", "goal", "paths", "itemId", "binding", "state")},
                               mode=grant.get('mode', 'legacy'), itemIds=grant.get('itemIds', []),
                               reviewState=candidate.get('state'),
                               job={k:v for k,v in grant.get('job', {}).items() if k != 'row'}))
        patches = [dict(id=p["id"], grantId=p["grantId"], summary=p["summary"],
                        files=[f["path"] for f in p["files"]], decision=p.get("decision"),
                        application=p.get("application"), verification=_verification(owner, p, _store(owner)["grants"][p["grantId"]]))
                   for p in _store(owner)["patches"].values()
                   if p["grantId"] in {g["id"] for g in grants}]
    return dict(grants=grants, patches=patches,
                boundary="测试者可直接修当前任务候选差异和已登记缺陷证据涉及的文件，无需逐项授权。仅编辑隔离副本；不能改主目录、事实区、扩展功能或放宽验收。范围外事项报告给执行者，不自行扩大。")


def _draft(owner, grant, row):
    return grant["drafts"].setdefault(row["id"], dict(baseSnapshot=row["snapshot"], files={}, checks={}))


def _manifest(draft):
    with H.database() as db:
        manifest = deepcopy(H.load_snapshot(db, draft["baseSnapshot"]))
    manifest["files"].update(draft["files"])
    manifest["workspaceRevision"] = H.version(dict(head=manifest.get("head"), files=manifest["files"]))
    return manifest


def read_draft(owner, row, grant_id, relative_path):
    _role(owner, "reviewer")
    with owner.lock:
        grant = _grant(owner, grant_id, row)
        path = _path(relative_path)
        draft = _draft(owner, grant, row)
        manifest = _manifest(draft)
        meta = manifest["files"].get(path)
        if meta is None:
            return dict(ok=True, exists=False, revision="absent", text="")
        return dict(review_units.source_file(meta, path), ok=True)


def edit(owner, row, grant_id, relative_path, expected_revision, old_text, new_text, item_id=None):
    _role(owner, "reviewer")
    with owner.lock:
        grant = _grant(owner, grant_id, row)
        path = _path(relative_path)
        if path not in grant["paths"]:
            raise W.Problem("repair_scope", "文件不在当前候选变更或缺陷证据范围内，不能借蓝图扩大修改", 403)
        draft = _draft(owner, grant, row)
        manifest = _manifest(draft)
        meta = manifest["files"].get(path)
        if meta is None or meta.get("kind") != "text":
            raise W.Problem("repair_scope", "当前修复工具仅编辑候选已有文本文件", 403)
        if expected_revision != meta["hash"]:
            raise W.Problem("revision_conflict", "请读取当前修复副本后编辑", 409)
        raw = H.raw_content(meta)
        _text(raw.decode("utf-8-sig"))
        changed = C.edit_bytes(raw, _text(old_text), _text(new_text))
        _text(changed.decode("utf-8-sig"))
        from .repair_collaboration import reviewer_begin
        claimed = reviewer_begin(owner, row, grant, path, changed, item_id)
        with H.database() as db:
            digest = H.put_object(db, changed)
        draft["files"][path] = dict(meta, hash=digest, bytes=len(changed))
        if claimed:
            draft.setdefault('repairItems', {})[path] = claimed
        owner.save()
    return dict(ok=True, revision=digest, scope="isolated_repair", mainWorkspaceChanged=False)


def check(owner, row, grant_id, argv, timeout=30, writable_copy=False):
    _role(owner, "reviewer")
    with owner.lock:
        grant = _grant(owner, grant_id, row)
        owner._resource(grant["resourceId"], "execute")
        from .repair_collaboration import guard_draft
        guard_draft(owner, row, grant)
        draft = _draft(owner, grant, row)
        manifest = _manifest(draft)
        revision = manifest["workspaceRevision"]
        # Each tested version has its own receipts; an edit cannot inherit pass.
        review = draft["checks"].setdefault(revision, dict(id=uuid.uuid4().hex,
            resourceId=row["resourceId"], sourcePath=row["sourcePath"], revision=revision,
            attempts=1, checks={}, repairGrantId=grant_id))
        owner.save()
    registry, _ = owner._review_tools(review, None, manifest=manifest, repair_only=True)
    result = registry.get("run_project_check")({"resource_id": row["resourceId"], "argv": argv, "timeout": timeout,
                                               "writable_copy": writable_copy})
    with owner.lock:
        _grant(owner, grant_id, row)
        owner.save()
    return dict(result, evidenceKind="repair_self_check", originalCandidateUnchanged=True)


def publish(owner, row, grant_id, summary):
    _role(owner, "reviewer")
    summary = _text(summary).strip()
    if not summary or len(summary) > 3000:
        raise W.Problem("repair_summary", "请说明修复意图和未覆盖项", 400)
    with owner.lock:
        grant = _grant(owner, grant_id, row)
        draft = _draft(owner, grant, row)
        manifest = _manifest(draft)
        with H.database() as db:
            base = H.load_snapshot(db, draft["baseSnapshot"])
        files = [dict(path=p, before=base["files"][p], after=m) for p, m in draft["files"].items()
                 if m["hash"] != base["files"][p]["hash"]]
        if not files or any(f["path"] not in grant["paths"] for f in files):
            raise W.Problem("repair_scope", "没有范围内的实际修改", 409)
        ident = H.version(dict(grant=grant_id, candidate=row["id"], files=files))
        patches = _store(owner)["patches"]
        if ident not in patches:
            from .repair_collaboration import guard_draft
            guard_draft(owner, row, grant)
            patches[ident] = dict(id=ident, grantId=grant_id, candidateId=row["id"],
                baseRevision=row["revision"], revision=manifest["workspaceRevision"], files=files,
                summary=summary, checks=deepcopy(draft["checks"].get(manifest["workspaceRevision"], {}).get("checks", {})),
                originalChecks=deepcopy(row.get("checks", {})),
                itemIds=sorted(set(draft.get('repairItems', {}).values())),
                created=time.time(), application=dict(state="pending", files={}))
            owner.save()
            owner.session.emit("assistant_patch_ready", patchId=ident, role="reviewer", summary=summary)
            from .repair_collaboration import update
            for item_id in set(draft.get('repairItems', {}).values()):
                update(owner, item_id, 'patch_ready', patchId=ident)
        if grant.get('job', {}).get('state') in {'queued', 'running'}:
            grant['job']['state'] = 'completed'
            owner.save()
    return dict(ok=True, patchId=ident, mainWorkspaceChanged=False, verification="pending")


def patch_view(owner, patch_id):
    _role(owner, "executor")
    with owner.lock:
        p = _store(owner)["patches"].get(patch_id)
        if not p:
            raise W.Problem("repair_patch", "补丁不存在", 404)
        _grant(owner, p["grantId"])
        result = deepcopy(p)
    for f in result["files"]:
        before = H.raw_content(f["before"]).decode("utf-8-sig")
        after = H.raw_content(f["after"]).decode("utf-8-sig")
        f["diff"] = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                fromfile=f["path"], tofile=f["path"]))
    return dict(ok=True, patch=result)


def decide(owner, patch_id, decision, reason, operation_ids=None):
    _role(owner, "executor")
    if decision not in {"defer", "reject", "adjusted"}:
        raise W.Problem("repair_decision", "使用defer、reject或adjusted；调整合并须提供实际写入回执")
    reason = _text(reason).strip()
    if not reason:
        raise W.Problem("repair_decision", "请说明处理原因")
    with owner.lock:
        p = _store(owner)["patches"].get(patch_id)
        if not p:
            raise W.Problem("repair_patch", "补丁不存在", 404)
        grant = _grant(owner, p["grantId"])
        if decision == "adjusted":
            # This records an executor's manual integration, not a second write
            # path. Receipts must prove every patch file's actual current bytes.
            if not isinstance(operation_ids, list) or not 1 <= len(operation_ids) <= 32:
                raise W.Problem("repair_receipt", "提供每个补丁文件的实际写入operationId", 409)
            _, _, ws, _ = owner._resource(grant["resourceId"], "write")
            files = {}
            for oid in operation_ids:
                op = C.operation(owner.session.dir.name, oid)
                path = op.get("target", {}).get("relative_path")
                revision = (op.get("result") or {}).get("revision")
                if (op.get("operation") != "write" or op.get("state") != "completed"
                        or op.get("resourceId") != grant["resourceId"] or op.get("workspaceId") != ws["id"]
                        or op.get("source", {}).get("role") != "executor" or op.get("started", 0) < p["created"]
                        or path not in {f["path"] for f in p["files"]}
                        or not revision or W.file_read(ws, path)["revision"] != revision):
                    raise W.Problem("repair_receipt", "写入回执未覆盖补丁的当前实际版本，不能标为已整合", 409)
                _target(ws, path)
                files[path] = dict(state="applied", operationId=oid, revision=revision)
            if set(files) != {f["path"] for f in p["files"]}:
                raise W.Problem("repair_receipt", "仍有补丁文件未提供整合回执", 409)
            p["application"].update(state="applied", adjusted=True, files=files,
                currentFiles={path: f["revision"] for path, f in files.items()}, at=time.time())
        p["decision"] = dict(value=decision, reason=reason, at=time.time())
        if decision in {'reject', 'adjusted'}:
            from .repair_collaboration import update
            for item_id in p.get('itemIds', []):
                update(owner, item_id, 'awaiting_verification' if decision == 'adjusted' else 'interrupted', reason=reason)
        owner.save()
    return dict(ok=True, application=deepcopy(p["application"]), verification="pending", defectClosed=False)


def apply(owner, patch_id):
    _role(owner, "executor")
    # Serialize only patch application; each actual write retains the existing
    # checkout/file locks and expected-content check, including user races.
    with FileLock(owner.path.parent / "assistant-patch-apply.lock", check=owner._ensure_running):
        with owner.lock:
            p = _store(owner)["patches"].get(patch_id)
            if not p:
                raise W.Problem("repair_patch", "补丁不存在", 404)
            grant = _grant(owner, p["grantId"])
            _, _, ws, _ = owner._resource(grant["resourceId"], "write")
            if p["application"]["state"] == "applied":
                return dict(ok=True, reused=True, application=deepcopy(p["application"]), verification="pending")
            plans, conflicts = [], []
            for f in p["files"]:
                path = f["path"]
                if path not in grant["paths"]:
                    raise W.Problem("repair_scope", "补丁越过授权范围", 403)
                _target(ws, path)
                current = W.file_read(ws, path)["revision"]
                entry = p["application"]["files"].get(path, {})
                if entry.get("state") in {"writing", "applied"} and current == f["after"]["hash"]:
                    plans.append((f, True))
                elif current == f["before"]["hash"] and entry.get("state") != "applied":
                    plans.append((f, False))
                else:
                    conflicts.append(dict(path=path, base=f["before"]["hash"], current=current, proposed=f["after"]["hash"]))
            if conflicts:
                p["application"].update(state="conflict", conflicts=conflicts)
                owner.save()
                return dict(ok=False, code="repair_conflict", conflicts=conflicts, error="当前文件已变化，请执行者核对差异；未覆盖当前内容")
        for f, already in plans:
            with owner.lock:
                _grant(owner, p["grantId"])
                entry = p["application"]["files"].setdefault(f["path"], {})
                entry["state"] = "applied" if already else "writing"
                p["application"]["state"] = "applying"
                owner.save()  # durable intent before the effect
            if not already:
                raw = H.raw_content(f["after"])
                prior_stop = current_stop()
                def authorized_stop():
                    if prior_stop and prior_stop():
                        return True
                    _grant(owner, p["grantId"])
                    _target(ws, f["path"])
                    return False
                set_stop(authorized_stop)
                try:
                    result = C.invoke(owner.root, "edit", grant["resourceId"], relative_path=f["path"],
                        expected_revision=f["before"]["hash"],
                        old_text=H.raw_content(f["before"]).decode("utf-8-sig"), new_text=raw.decode("utf-8-sig"))
                finally:
                    set_stop(prior_stop)
                if not result.get("ok"):
                    with owner.lock:
                        p["application"].update(state="conflict", error=result)
                        owner.save()
                    return result
                with owner.lock:
                    entry.update(state="applied", operationId=result.get("operationId"), revision=result.get("revision"))
                    owner.save()
            else:
                result = dict(ok=True, fileChangeRevision="pending:" + patch_id)
            owner.observe([dict(i=0, name="edit_project_file", action_id=(current_call() or {}).get("actionId"),
                                args=dict(resource_id=grant["resourceId"], relative_path=f["path"]))], {0: (result, 0)})
        with owner.lock:
            # Final file evidence, not a claim that integration tests passed.
            versions = {f["path"]: W.file_read(ws, f["path"])["revision"] for f in p["files"]}
            if any(versions[f["path"]] != f["after"]["hash"] for f in p["files"]):
                p["application"].update(state="conflict", currentFiles=versions)
                owner.save()
                return dict(ok=False, code="repair_conflict", error="应用期间文件再次变化，需核对实际版本")
            p["application"].update(state="applied", currentFiles=versions, at=time.time())
            p["decision"] = dict(value="merge", at=time.time())
            from .repair_collaboration import update
            for item_id in p.get('itemIds', []):
                update(owner, item_id, 'awaiting_verification', role='executor', patchId=patch_id)
            owner.save()
        owner.session.emit("assistant_patch_applied", patchId=patch_id, files=versions)
        return dict(ok=True, application=deepcopy(p["application"]), verification="pending", defectClosed=False)


def feedback(owner, messages, row=None):
    marker = "[assistant-patches]"
    material = view(owner, row)
    from .repair_collaboration import progress
    material['projectProgress'] = progress(owner, row)
    material['coordination'] = '先核对同一问题的处理者和主线修复结果；正在修复时推进无关工作，已有主线修复时改为新版本复验。修复不等于验收通过。'
    if (material["grants"] or material["patches"] or material['projectProgress']['resources'] or material['projectProgress']['repairs']
            or any(str(m.content).startswith('[context-update:assistant-patches:') for m in messages)):
        from .working_context import append_update
        append_update(messages, 'assistant-patches', json.dumps(material, ensure_ascii=False),
                      prefix=marker + ' 修复进度与补丁资料，不扩展授权。')


def pending(owner):
    return [p for p in view(owner)["patches"] if p["application"]["state"] != "applied"
            and (p.get("decision") or {}).get("value") != "reject"]


def jobs(owner, states=('queued', 'running')):
    with owner.lock:
        return [g for g in _store(owner)['grants'].values() if g['state'] == 'active'
                and g['binding'] == owner._task_binding() and g.get('job', {}).get('state') in states]


def run_job(owner, grant):
    """Use the existing review worker; a repair task never rewrites its original verdict."""
    from . import scoped_roles
    from .provider import Interrupted
    from .tools import Registry
    job, row = grant['job'], grant['job']['row']
    token = set_call(owner._identity('reviewer', 'repair_assignment'))
    try:
        _grant(owner, grant['id'], row)
        registry = Registry()
        install_reviewer(owner, row, registry)
        reason = lambda: owner.stop_reason('reviewer') if owner._review_stop() else dict(
            kind='authorization_revoked', text='修复授权已撤销，停止副本修改')
        stopped = lambda: owner._review_stop() or grant['state'] != 'active'
        text = scoped_roles.run(owner.root, owner._role_session(), 'reviewer',
            '执行者明确委派修复；只完成以下目标和文件范围。先读取当前修复任务与副本，再做最小修改、'
            '自检并 publish_repair_patch。不能直接修改主目录或把修复自检当作独立验收通过。'
            '如果无需修改或无法完成，说明具体原因，不伪造补丁。\n'
            + json.dumps(dict(grantId=grant['id'], goal=grant['goal'], paths=grant['paths'], itemId=grant['itemId']), ensure_ascii=False),
            tools=registry, should_stop=stopped, stop_reason=reason, preparing=True,
            binding_fields=dict(repairGrantId=grant['id']))
        with owner.lock:
            published = any(p['grantId'] == grant['id'] for p in _store(owner)['patches'].values())
            job.update(state='completed' if published else 'failed', result=text[:2000])
    except Interrupted as exc:
        job.update(state='paused', error=str(exc), stopKind=owner.stop_reason()['kind'])
    except Exception as exc:
        job.update(state='failed', error=str(exc)[:600])
    finally:
        with owner.lock:
            owner.save()
        owner.session.emit('assistant_repair_job', grantId=grant['id'], state=job['state'],
                           reason=job.get('error', job.get('result', '')))
        reset_call(token)


def _tool(registry, name, description, fields, fn, optional=()):
    registry.add(Tool(name, description, dict(type="object", properties=fields,
        required=[k for k in fields if k not in optional]), fn, zone="offflow", kind="meta"))


def install_executor(owner, registry):
    from .repair_collaboration import wrap_executor
    wrap_executor(owner, registry)
    s = dict(type="string")
    _tool(registry, "revoke_reviewer_repair", "撤销测试者修复权限，保留已提交成果。", dict(grant_id=s), lambda **kw: revoke(owner, **kw))
    _tool(registry, "read_assistant_patch", "读取补丁差异、原基线与修复自检证据。", dict(patch_id=s), lambda **kw: patch_view(owner, **kw))
    _tool(registry, "apply_assistant_patch", "应用已核对补丁。遇当前内容冲突保留用户改动；应用不等于验收通过。", dict(patch_id=s), lambda **kw: apply(owner, **kw))
    _tool(registry, "record_assistant_patch_decision", "defer暂缓、reject拒绝；adjusted记录手工调整合并，须给全部补丁文件实际写入operation_ids和调整原因。不自动关闭原缺陷。",
          dict(patch_id=s, decision=s, reason=s, operation_ids=dict(type="array", items=s)),
          lambda **kw: decide(owner, **kw), ("operation_ids",))


def install_reviewer(owner, row, registry):
    s = dict(type="string")
    registry.assistant_feedback = lambda messages: feedback(owner, messages, row)
    def repair_pending():
        from .repair_collaboration import state
        with owner.lock:
            for grant in view(owner,row)['grants']:
                if grant['state'] != 'active':
                    continue
                for key in grant.get('itemIds', []):
                    item = owner.work.state['items'].get(key, {})
                    repair = state(owner, item)
                    if (item and item.get('state') != 'verified' and
                            repair.get('state') not in {'closed','patch_ready','awaiting_verification'} and
                            not (repair.get('state') == 'repairing' and repair.get('role') != 'reviewer')):
                        return True
            return False
    registry.repair_pending = repair_pending
    def direct(fn, grant_id=None, **kw):
        from .repair_collaboration import progress
        # Recheck at the tool boundary, including calls emitted in one batch.
        owner._ensure_running()
        result = fn(owner, row, _current_scope(owner, row, grant_id), **kw)
        return dict(result, projectProgress=progress(owner,row))
    def assignment():
        from .repair_collaboration import progress
        return dict(view(owner,row), projectProgress=progress(owner,row))
    _tool(registry, "read_repair_assignment", "查看当前任务的副本修复范围和执行者合并回传。登记真实缺陷后直接修复，无需申请授权。", {}, assignment)
    _tool(registry, "read_repair_file", "读取修复副本的当前内容和版本；不读取主目录。",
          dict(relative_path=s), lambda **kw: direct(read_draft, **kw))
    _tool(registry, "edit_repair_file", "直接局部修复任务范围内已发现的缺陷，无需执行者批准。仅编辑副本；不能扩展需求或削弱原测试。",
          dict(relative_path=s, expected_revision=s, old_text=s, new_text=s, item_id=s), lambda **kw: direct(edit, **kw), ('item_id',))
    _tool(registry, "run_repair_check", "在修复版本运行自检。源码只读；临时输出使用TEMP。结果不是原候选独立复核证据。",
          dict(argv=dict(type="array", items=s), timeout=dict(type="integer"), writable_copy=dict(type="boolean")),
          lambda **kw: direct(check, **kw), ("timeout", "writable_copy"))
    _tool(registry, "publish_repair_patch", "提交当前任务副本的实际补丁、自检证据与未覆盖说明，执行者下一请求接收并决定合并。",
          dict(summary=s), lambda **kw: direct(publish, **kw))
