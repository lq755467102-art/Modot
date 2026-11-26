"""Human workroom binding: reuse project roots and the existing file service.

Resolution is metadata-only. Opening registers the same directory; it does not
copy source, initialize Git, grant Agent capabilities or choose an executor.
"""
# 主要引用方包括 `workbench.py`。
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from .filelock import FileLock
from . import projects, workbench as W


# 从会话进入时复用该会话的资源与范围版本。明确选择项目时，从项目登记中取得可读的代码根。
def context(conversation_id=None, project_id=None):
    if any(value is not None and (not isinstance(value, str) or not value) for value in (conversation_id, project_id)):
        raise W.Problem('workroom_scope', '项目和会话身份须为非空字符串')
    if conversation_id and project_id:
        raise W.Problem('workroom_scope', '请只选择当前会话或一个项目')
    thread = projects.conversation(conversation_id) if conversation_id else None
    if thread:
        scope = projects.scope(thread['id'])
        project_id = scope['projectId']
        resources = scope['resources']
        revision = scope['revision']
    else:
        owner = projects.project(project_id) if project_id else None
        resources = list((owner or {}).get('resources', {}).values())
        revision = W.digest(json.dumps(resources, sort_keys=True).encode())
    owner = projects.project(project_id) if project_id else None
    roots = []
    for resource in resources:
        if resource.get('kind') != 'code' or resource.get('permission') != 'read':
            continue
        origin = resource.get('worktreeOf') or resource['id']
        source = (owner or {}).get('resources', {}).get(origin, resource)
        roots.append(dict(resourceId=resource['id'], originResourceId=origin,
                          name=source['name'], path=resource['path'],
                          checkout='worktree' if resource.get('worktreeOf') else 'local'))
    location = (thread or {}).get("workroomLocation") or {}
    preferred = next((r for r in roots if r["resourceId"] == location.get("resourceId")
                      and location.get("projectId") == project_id), None)
    initial = None
    if preferred:
        try:
            relative = location["directory"]
            target = W.file_path({"path": preferred["path"]}, relative or ".")
            if target.is_dir() and target == Path(preferred["path"]) / relative:
                initial = dict(resourceId=preferred["resourceId"], directory=relative)
        except (W.Problem, OSError, KeyError, ValueError):
            pass
    readonly = bool(thread and (thread.get('readonlyEvidence') or thread.get('subtaskOf')))
    status = 'readonly' if readonly else 'ready' if roots else 'no_code' if owner else 'no_project'
    return dict(project=dict(id=owner['id'], name=owner['name']) if owner else None,
                conversationId=conversation_id or None, scopeRevision=revision,
                roots=roots, status=status, **({'importLocation': initial} if initial else {}),
                notice='历史或委托审查请继续使用原审查入口。' if readonly else
                '' if roots else '未选择项目。' if not owner else
                '此范围没有可读的代码目录。' if not roots else '')


def open_root(body):
    if set(body) - {'conversationId', 'projectId', 'resourceId', 'scopeRevision', '_request_id'}:
        raise W.Problem('workroom_arguments', '工作间选择包含未知字段')
    cid, pid = body.get('conversationId'), body.get('projectId')
    view = context(cid, pid)
    if view['status'] == 'readonly':
        raise W.Problem('readonly_evidence', view['notice'], 403)
    if not isinstance(body.get('scopeRevision'), str) or body['scopeRevision'] != view['scopeRevision']:
        raise W.Problem('workroom_changed', '项目或会话范围已变化，请刷新后选择', 409)
    root = next((r for r in view['roots'] if r['resourceId'] == body.get('resourceId')), None)
    if not root:
        raise W.Problem('resource_denied', '此代码目录不属于当前工作间范围', 403)
    path = Path(root['path'])
    if not path.is_absolute() or not path.is_dir() or path.resolve() != path:
        raise W.Problem('resource_moved', '代码目录不存在或路径已变化，未打开其他目录', 409)
    # Only the existing registry is updated; no file content or model grant changes.
    workspace = W.register(str(path))
    if context(cid, pid)['scopeRevision'] != view['scopeRevision']:
        raise W.Problem('workroom_changed', '打开期间范围已变化，请重新选择', 409)
    return dict(workspaceId=workspace['id'], projectId=view['project']['id'] if view['project'] else None,
                resourceId=root['resourceId'], path=str(path), checkout=root['checkout'],
                scopeRevision=view['scopeRevision'])


def _linked_path(conversation_id, path, resource_id=None, scope_revision=None):
    """Resolve the original link or revalidate an already selected checkout."""
    if not conversation_id or not isinstance(path, str) or not path or len(path) > 4096 or any(ord(c) < 32 for c in path):
        raise W.Problem('invalid_file_link', '文件链接须包含会话和有效路径')
    target = Path(path)
    if ('..' in target.parts or target.anchor and not target.is_absolute()
            or any(':' in piece for piece in target.parts if piece != target.anchor)):
        raise W.Problem('invalid_file_link', '文件链接须指向当前检出目录中的文件', 403)
    view = context(conversation_id)
    if scope_revision is not None and scope_revision != view['scopeRevision']:
        raise W.Problem('workroom_changed', '会话检出目录已变化，请重新打开链接', 409)
    if view['status'] != 'ready':
        raise W.Problem('file_link_unavailable', view['notice'], 403)
    matches = []
    for root in view['roots']:
        if resource_id is not None and root['resourceId'] != resource_id:
            continue
        base = Path(root['path'])
        if target.is_absolute() and not target.is_relative_to(base):
            continue
        relative = (target.relative_to(base) if target.is_absolute() else target).as_posix()
        try:
            if not base.is_absolute() or base.resolve() != base or not base.is_dir():
                continue
            file = W.file_path_from_root(base, relative)
            if file.is_file():
                matches.append((root, relative, file))
        except (W.Problem, OSError, RuntimeError):
            continue
    if not matches:
        raise W.Problem('file_link_unavailable', '当前检出目录没有这个可读取文件', 404)
    if target.is_absolute():
        matches.sort(key=lambda match: len(Path(match[0]['path']).parts), reverse=True)
    elif len(matches) != 1:
        raise W.Problem('ambiguous_file_link', '多个检出目录中有同名文件，请使用完整路径', 409)
    return view['scopeRevision'], *matches[0]


# 打开或交接文件前再核对会话范围和原路径，以免检出切换后读到另一份同名文件。
def _check_link_scope(conversation_id, scope_revision, base, relative, file):
    if (context(conversation_id)['scopeRevision'] != scope_revision
            or W.file_path_from_root(Path(base), relative) != file):
        raise W.Problem('workroom_changed', '会话检出目录或文件位置已变化，请重新打开链接', 409)


# Shell association must only open data. Source and script files can be viewed
# in Modot or passed to an application explicitly selected by the user.
_DEFAULT_OPEN = frozenset(('txt log md markdown csv tsv json ts tsx css xml yaml yml toml '
                           'pdf rtf docx xlsx pptx odt ods odp png jpg jpeg gif webp bmp ico tif tiff '
                           'mp3 wav ogg flac m4a mp4 webm mov mkv').split())


def linked_file(conversation_id, path, resource_id=None, scope_revision=None):
    scope_revision, root, relative, file = _linked_path(conversation_id, path, resource_id, scope_revision)
    workspace = {'path': root['path']}
    revision = ''
    mime_type = 'text/plain'
    try:
        try:
            revision = W.file_read(workspace, relative, editor=True)['revision']
            writable = bool(file.stat().st_mode & 0o222) and os.access(file, os.W_OK) and os.access(file.parent, os.W_OK)
            mode = 'edit' if writable else 'readonly'
        except W.Problem as error:
            if error.code != 'large_file':
                raise
            W.file_text_page(workspace, relative, None, '0')
            mode = 'readonly'
    except W.Problem as error:
        if error.code not in {'binary', 'encoding'}:
            raise
        mode = 'binary'
        mime_type = mimetypes.guess_type(relative)[0] or 'application/octet-stream'
    except OSError as error:
        raise W.Problem('file_link_unavailable', '无法读取当前检出目录中的文件', 403) from error
    _check_link_scope(conversation_id, scope_revision, root['path'], relative, file)
    return dict(conversationId=conversation_id, resourceId=root['resourceId'], path=relative,
                scopeRevision=scope_revision, revision=revision, mode=mode, mimeType=mime_type,
                absolutePath=str(file), defaultOpen=file.suffix.lower().lstrip('.') in _DEFAULT_OPEN)


def linked_file_content(conversation_id, path, resource_id, scope_revision, absolute_path):
    scope_revision, root, relative, file = _linked_path(conversation_id, path, resource_id, scope_revision)
    if Path(absolute_path) != file:
        raise W.Problem('workroom_changed', '文件位置已变化，请重新打开链接', 409)
    try:
        content = W.file_read({'path': root['path']}, relative)
    except OSError as error:
        raise W.Problem('file_link_unavailable', '无法读取当前检出目录中的文件', 403) from error
    _check_link_scope(conversation_id, scope_revision, root['path'], relative, file)
    return content



# 一次运行可请求打开一个当前授权的文件。只有前台工作间确认后才记为打开。
def _checkout_comparison(root, comparison):
    path = Path(root["path"])
    if comparison["directory"]:
        path = W.file_path_from_root(path, comparison["directory"])
    try:
        result = W.git({"path":str(path)}, ["rev-parse", "HEAD"], check=False)
        actual = result.stdout.decode("ascii", errors="replace").strip()
        head = actual if not result.returncode and re.fullmatch("[a-f0-9]{40}", actual) else None
    except (OSError, subprocess.SubprocessError, W.Problem):
        head = None
    return dict(checkoutPath=str(path), localHead=head, prHead=comparison["head"],
                matches=bool(head and head == comparison["head"]))


def request_file_open(session, root, path, resource_id=None, *, github=None, line=None):
    from . import code_service, tools

    source = tools.current_call()
    ident = session.dir.name
    if not source or source.get("sessionId") != ident or source.get("runId") != session.run_id:
        raise W.Problem("workroom_identity", "打开工作间文件需要当前会话的动作身份", 403)
    target = linked_file(ident, path, resource_id)
    comparison = github.file_comparison(target) if github else None
    if line is not None and (type(line) is not int or line < 1):
        raise W.Problem("workroom_line", "文件行号须为正整数。")
    code_service.context_resource(root, target["resourceId"], register_workspace=False)
    state_path = session.dir / "workroom-open.json"
    lock_path = session.dir / "workroom-open.lock"
    request_id = uuid.uuid4().hex
    deadline = time.time() + 12
    target = {key: target[key] for key in ("conversationId", "resourceId", "path", "scopeRevision", "revision")}
    if line:
        target["line"] = line
    with FileLock(lock_path, timeout=1):
        prior = W.read(state_path)
        if prior.get("state") in {"requested", "claimed"} and prior.get("expiresAt", 0) > time.time():
            raise W.Problem("workroom_open_busy", "工作间正在打开另一个文件", 409)
        W.atomic_json(state_path, dict(id=request_id, runId=session.run_id, state="requested",
                                        expiresAt=deadline, target=target, comparison=comparison))
    session.emit("workroom_open_requested", workroomOpen=dict(id=request_id, expiresAt=deadline))
    should_stop = tools.current_stop()
    while time.time() < deadline:
        state = W.read(state_path)
        if state.get("id") != request_id:
            break
        if state.get("state") == "opened":
            return dict(ok=True, path=target["path"], workroomTabOpened=True,
                        **({"gitComparison":state["gitComparison"]} if state.get("gitComparison") else {}))
        if state.get("state") == "failed" or (should_stop and should_stop()):
            break
        time.sleep(0.1)
    with FileLock(lock_path, timeout=1):
        state = W.read(state_path)
        if state.get("id") == request_id and state.get("state") in {"requested", "claimed"}:
            W.atomic_json(state_path, dict(state, state="failed"))
    return dict(ok=False, code="workroom_open_unconfirmed", path=target["path"],
                workroomTabOpened=False, error="没有收到当前工作间的文件标签页确认。")


def file_open_response(body):
    if (set(body) - {"conversationId", "id", "state", "_request_id"}
            or not isinstance(body.get("conversationId"), str)
            or not isinstance(body.get("id"), str)
            or body.get("state") not in ("claimed", "opened", "failed")):
        raise W.Problem("workroom_open_arguments", "工作间打开文件回执参数无效")
    from . import paths, runtime

    ident = projects.conversation(body.get("conversationId"))["id"]
    directory = paths.data_root() / "runs" / ident
    state_path = directory / "workroom-open.json"
    outcome = body["state"]
    with FileLock(directory / "workroom-open.lock", timeout=1, create_parent=False):
        record = W.read(state_path)
        expected = "requested" if outcome == "claimed" else "claimed"
        if (not record or record.get("id") != body.get("id") or record["state"] != expected
                or record["expiresAt"] <= time.time()):
            raise W.Problem("workroom_open_expired", "工作间打开请求已处理或已过期", 409)
        target = record["target"]
        if outcome != "failed":
            live = runtime.status(directory)
            if not live.get("active") or live.get("state") != "running" or live.get("run_id") != record["runId"]:
                raise W.Problem("workroom_open_expired", "原运行已结束或无法核对，工作间没有执行这次请求", 409)
            _, root, _, _ = _linked_path(ident, target["path"], target["resourceId"], target["scopeRevision"])
            if record.get("comparison"):
                record["gitComparison"] = target["gitComparison"] = _checkout_comparison(root, record["comparison"])
                if not target["gitComparison"]["matches"]:
                    target.pop("line", None)
        W.atomic_json(state_path, dict(record, state=outcome))
    return target if outcome == "claimed" else dict(ok=True)

def route(method, parts, query, body):
    if parts == ['file-open-response'] and method == 'POST':
        return 200, file_open_response(body)
    if parts == ['folders']:
        from . import work_folders
        if method == 'GET': return 200, work_folders.settings()
        if method == 'POST':
            if 'conversationId' in body:
                return 200, work_folders.ensure_conversation(body['conversationId'])
            return 200, work_folders.configure(body.get('path'))
    if not parts and method == 'GET':
        if set(query) - {'conversationId', 'projectId'}:
            raise W.Problem('workroom_arguments', '工作间查询包含未知字段')
        return 200, context((query.get('conversationId') or [None])[0],
                            (query.get('projectId') or [None])[0])
    if parts == ['attach-repository'] and method == 'POST':
        from .code_service import RuntimeView
        return 200, projects.attach_repository(body.get("conversationId"), body, RuntimeView())
    if parts == ['open'] and method == 'POST':
        return 200, open_root(body)
    if parts in (['linked-file'], ['linked-file', 'content']) and method == 'GET':
        basic = {'conversationId', 'path'}
        bound = basic | {'resourceId', 'scopeRevision'}
        allowed = (basic, bound) if parts == ['linked-file'] else (bound | {'absolutePath'},)
        if set(query) not in allowed or any(len(values) != 1 or not values[0] for values in query.values()):
            raise W.Problem('workroom_arguments', '文件链接查询需要有效的会话、路径和检出目录')
        args = [query[key][0] for key in ('conversationId', 'path', 'resourceId', 'scopeRevision', 'absolutePath') if key in query]
        return 200, (linked_file if parts == ['linked-file'] else linked_file_content)(*args)
    raise W.Problem('not_found', '没有这个工作间接口', 404)
