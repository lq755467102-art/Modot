"""Bind an isolated checkout to the live L0 run and its existing project authority."""

from contextlib import nullcontext
from contextvars import copy_context
import threading
from pathlib import Path
from uuid import uuid4

from . import baseline, code_service as C, permissions, project_transaction, workbench as W
from .tools import current_call, current_stop

_lock = threading.RLock()
_views = {}
_gates = {}


def store():
    return W.home() / 'project-write-stages'


def action_gate(resource):
    source = current_call() or {}
    if not source.get('nativeExecutor') or not source.get('runId'):
        return nullcontext()
    key = source.get('sessionId'), source['runId'], resource
    with _lock:
        return _gates.setdefault(key, threading.RLock())


def lookup(conversation, resource):
    source = current_call() or {}
    if source.get('sessionId') != conversation:
        return None
    with _lock:
        return _views.get((conversation, source.get('runId'), resource))


def for_action(root, thread, resource, ws, source, capability):
    key = thread['id'], source.get('runId'), resource['id']
    if not source.get('nativeExecutor') or not key[1]:
        return None
    with _lock:
        v = _views.get(key)
        if v is not None:
            v.guard()
            return v
        v = project_transaction.View.create(store(), Path(ws['path']), list(key))
        revision = permissions.profile(thread['id'])['revision']
        captured = copy_context()
        stop = current_stop()
        v.checkout = W.file_workspace_state(ws)
        def access(need):
            if (stop and stop()) or permissions.profile(key[0])['revision'] != revision:
                raise W.Problem('approval_revoked', '任务已停止或本轮的权限已变化', 403)
            _, actual, _, _ = C.context_resource(root, key[2], need, register_workspace=False)
            if actual['path'] != ws['path']:
                raise W.Problem('resource_denied', '工作目录或授权已变化', 403)
        v.guard = lambda: captured.copy().run(access, None)
        def publish_guard():
            captured.copy().run(access, 'write')
            state = W.file_workspace_state(ws)
            if state != v.checkout or state.get('protected') or state.get('branch') == '(detached)':
                raise W.Problem('protected_checkout', '工作树或分支已变化，暂存未写回', 409)
            W.guard_workspace_writer(v.project, C.RuntimeView(), exclude_thread=thread['id'])
        v.publish_guard = publish_guard
        v.path_guard = lambda name, write: baseline.hit('write' if write else 'read', v.project / name, root=v.project)
        v.metadata = thread, resource, ws, source
        _views[key] = v
        return v


def disposable(v):
    child = project_transaction.View.create(v.store, v.project, v.record['task'])
    child.parent = v
    child.guard, child.path_guard = v.guard, v.path_guard
    return child


def note_check(v, process, report, *, passed, check_key):
    if v is None:
        return
    with v.lock:
        if report.get('state') == 'failed':
            v.failed_checks.add(check_key)
            v.verified = None
        elif not passed:
            v.verified = None
        elif report.get('state') == 'passed':
            v.failed_checks.discard(check_key)
            if not v.failed_checks:
                v.verified = v.revision()
                v.last_check = process


def _publish(v):
    from . import change_history
    thread, resource, ws, source = v.metadata
    receipt = {}
    def prepare(plan):
        names = [e['name'] for e in plan if 'directory' not in (e['before'], e['after'])]
        if names:
            operation = dict(id=uuid4().hex, operation='write', source=source,
                             branch=v.checkout.get('branch'), target=dict(relative_paths=names))
            receipt['changeId'] = change_history.prepare(thread['id'], resource, ws, operation,
                                                         check=v.publish_guard, targets_only=True)
    def finish(plan):
        if receipt:
            verified = [dict(path=e['name'], before=e['before'], after=e['after']) for e in plan
                        if 'directory' not in (e['before'], e['after'])]
            change_history.finish(thread['id'], receipt['changeId'], ws, verified=verified)
    try:
        applied = v.publish(v.publish_guard, prepare, finish)
    except BaseException:
        if receipt:
            change_history.mark_unknown(thread['id'], receipt['changeId'])
        raise
    return dict(state='published', paths=applied, verificationProcess=v.last_check, **receipt)


def settle(session, run, status):
    outcomes = []
    with _lock:
        selected = [(k,v) for k,v in list(_views.items()) if k[:2] == (session,run)]
        gates = {k: _gates.get(k, threading.RLock()) for k,_ in selected}
    for key,v in selected:
        gate = gates[key]
        gate.acquire()
        try:
            if not v.changes():
                continue
            if status == 'success' and v.verified == v.revision():
                outcomes.append(dict(resourceId=key[2], **_publish(v)))
            else:
                reason = ('本轮未成功结束' if status != 'success' else
                          '本轮仍有失败的测试未复验通过' if v.failed_checks else '当前暂存版本尚无通过的测试回执')
                outcomes.append(dict(resourceId=key[2], state='discarded', reason=reason))
        except (OSError, ValueError, W.Problem) as exc:
            outcomes.append(dict(resourceId=key[2], state='withheld', reason=str(getattr(exc,'message',exc))))
        finally:
            try:
                if v.record.get('restores'):
                    from . import restorations
                    from .filelock import FileLock
                    committed = bool(outcomes and outcomes[-1].get('resourceId') == key[2] and outcomes[-1]['state'] == 'published')
                    for plan in v.record['restores']:
                        path = restorations.plan_path(plan)
                        with FileLock(path.with_suffix('.lock')):
                            value = restorations.load(session, plan)
                            if value.get('state') == 'applying' and value.get('source', {}).get('runId') == run:
                                path_result = value['result']
                                written = (v.project / path_result['path'])
                                completed = committed and written.is_file() and W.digest(written.read_bytes()) == path_result['revision']
                                value['state'] = path_result['state'] = 'completed' if completed else 'failed'
                                path_result['staged'] = False
                                W.atomic_json(path, value)
                if v.record['phase'] == 'open' and not v.record.get('mounts'):
                    v.discard()
            finally:
                with _lock:
                    _views.pop(key, None)
                    _gates.pop(key, None)
                gate.release()
    return outcomes
