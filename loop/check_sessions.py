"""Host-owned check lifetimes over the existing code operation and process records.

The worker uses the synchronous check implementation, including its sole
finalizer. Only the caller's wait is yielded; no model or check is scheduled by
completion. In-memory jobs are handles, not an alternative durable state store.
"""
# 主要引用方包括 `agent_gateway.py` 和 `executor_core.py`。
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path

from . import workbench as W, code_service as C, directory_changes

observer = ContextVar('owned_check_observer', default=None)
_jobs = {}
_lock = threading.Lock()
_waiters_lock = threading.Lock()
_waiters = {}
ACTIVE = {'starting', 'running', 'stopping', 'finalizing'}
TASK_CHECKS = {'check-start', 'check-output', 'check-stop', 'check-join'}


def task_owns_check(operation, context, args):
    from . import project_task_view
    if operation not in TASK_CHECKS:
        return False
    resource = args.get('resource_id')
    if operation in {'check-start', 'check-join'}:
        return isinstance(resource, str) and project_task_view.lookup(context['sessionId'], resource) is not None
    # The durable reservation keeps retries on the same owner after its job completes.
    oid, pid = args.get('operation_id'), args.get('process_id')
    try:
        if oid is None and isinstance(pid, str) and len(pid) == 32:
            oid = W.process_record(pid).get('operationId')
        if not isinstance(oid, str) or len(oid) != 32:
            return False
        op = C.operation(context['sessionId'], oid)
        return (op.get('backgroundCheck') and op.get('resourceId') == resource
                and op.get('ownerPid') == os.getpid() and op.get('ownerIdentity') == W.RT.identity(os.getpid()))
    except W.Problem:
        return False


def pending(value):
    return (isinstance(value, dict) and value.get('background') is True
            and (value.get('execution') or {}).get('state') in ACTIVE | {'unknown'})


def reused(value):
    return isinstance(value, dict) and value.get('reused') is True and value.get('executedThisCall') is False


# 启动身份由会话、轮次和动作共同确定；同一调用重试仍指向原操作，不能据此再启动一份命令。
def operation_identity(context):
    return hashlib.sha256(json.dumps([context['sessionId'], context['runId'], context['actionId']]).encode()).hexdigest()[:32]


def _key(oid):
    return str(W.home().resolve()), oid


@contextmanager
def _waiting_for_results(parent, run_id):
    key = (_key(parent), run_id)
    with _waiters_lock:
        wake = threading.Event()
        _waiters.setdefault(key, set()).add(wake)
    try:
        yield wake
    finally:
        with _waiters_lock:
            waiting = _waiters[key]
            waiting.remove(wake)
            if not waiting:
                del _waiters[key]


def notify_waiters(parent, run_id):
    with _waiters_lock:
        for wake in _waiters.get((_key(parent), run_id), ()):
            wake.set()


def _milliseconds(value, name, default=0):
    value = default if value is None else value
    if type(value) is not int or not 0 <= value <= 30000:
        raise W.Problem('invalid_arguments', name + '需为0—30000毫秒', 400)
    return value


def _wait_deadline(context, wait_ms):
    from .completion_review import remaining_time

    now = time.monotonic()
    remaining = remaining_time(context)
    seconds = wait_ms / 1000
    return now + (seconds if remaining is None else min(seconds, remaining))


def result(op):
    raw = op.get('result') or {}
    return dict(C.check_tool_result(raw), operationId=op['id'],
                ok=(raw.get('verification') or {}).get('status') == 'passed')


def _preparing(oid, *, stopping=False):
    return dict(ok=True, code='stopping' if stopping else 'preparing', background=True, operationId=oid,
                execution=dict(state='stopping' if stopping else 'starting', exitCode=None),
                summary=('已请求停止；请按operation_id查询终态。' if stopping else
                         '检查正在准备；请按operation_id查询，不要重新启动。'))


def _dialogue_source(ws, include):
    from .conversation_record import check_view

    # Admission holds no job or checkout lock, never walks a tree and does not
    # wait on a publisher. The worker still verifies the durable source revision.
    with check_view(ws, timeout=0) as (record, digest, stage):
        excluded = frozenset(name for name in (record, stage) if name)
        scope = ws['recordBinding'][0]
        generation = directory_changes.source_generation(ws['path'], projection=(scope, excluded))
        if generation is not None:
            return ('dialogue-folder', generation, digest if include else None)
        return None


def _preparation_source(ws, args):
    root = Path(ws['path']).resolve()
    is_folder = not os.path.lexists(root / '.git')
    if is_folder and W.local_writer_root(root) != root:
        return None
    if is_folder and ws.get('recordBinding'):
        return _dialogue_source(ws, C.includes_record(args))
    generation = directory_changes.source_generation(ws['path'])
    if generation is None:
        return None
    if is_folder:
        # Joining a live job happens inside its owning host. The durable content
        # fingerprint is checked by the worker before the command actually starts.
        return 'folder', generation
    observed = W.git_observation.identity(ws)
    state = W.git_status(ws)
    if (not observed or not state.get('head') or observed != W.git_observation.identity(ws)
            or generation != directory_changes.source_generation(ws['path'])):
        return None
    # A Git status read establishes the index and version even after Git-only
    # notifications. The source watch also covers ignored files the copy may use.
    return observed, generation, state['branch'], state['revision']


def _preparation_key(root, context, args, binding=None):
    # Explicit preparation leases can contain mutable inputs. Their IDs alone
    # cannot establish that two requests would run in the same environment.
    if (context.get('role') != 'executor' or not context.get('logicalTaskId')
            or not isinstance(args.get('resource_id'), str) or args.get('environment_id') is not None):
        return None
    from .permissions import profile
    from .tools import reset_call, set_call

    token = set_call(context)
    try:
        thread, resource, ws, _ = binding or C.context_resource(
            root, args['resource_id'], 'execute', register_workspace=False)
        from . import project_task_view
        if project_task_view.lookup(thread['id'], resource['id']) is not None:
            # The checkout watcher only sees original files. Never join a test of a different staged revision.
            return None
        if 'script_operation_id' in args:
            reference = args['script_operation_id']
            if not isinstance(reference,str):
                return None
            previous = C.operation(thread['id'],reference)
            # Another origin may have lost its grant. Only the worker validates
            # both origins under the checkout lock; do not join it beforehand.
            if (previous.get('resourceId') != resource['id']
                    or previous.get('target',{}).get('scriptSourceOperationId')):
                return None
        workspace = _preparation_source(ws, args)
        if workspace is None:
            return None
        environment = C.check_environment_observation()
        if environment is None:
            return None
        options = dict(timeout=30, purpose='acceptance', writable_copy=False,
                       capture_changes=False, expected_revision=None)
        options.update(args)
        options['directory'] = C.check_directory(thread, resource['id'], ws, options)
        options.pop('environment_id', None)
        reason = options.pop('scope_reason', '')
        if not isinstance(reason, str) or len(reason) > 320:
            return None
        identity = tuple(context.get(key) for key in ('sessionId', 'logicalTaskId', 'demandRevision'))
        captured = next(r for r in thread['resourceSnapshot']['resources'] if r['id'] == resource['id'])
        grant = (resource.get('permissionAt'), resource.get('write'), captured.get('write'))
        request = (str(W.home().resolve()), identity, environment, profile(thread['id'])['revision'], grant,
                   json.dumps(options, sort_keys=True, ensure_ascii=False, allow_nan=False))
        return request, workspace
    except (OSError, TypeError, ValueError, W.Problem, subprocess.SubprocessError):
        return None  # Ordinary validation will report the original request's error.
    finally:
        reset_call(token)


def _matching_check(key, run_id, *, preparing_only, references=None):
    # The caller holds _lock while choosing a new owner or joining an existing one.
    references = references or {}
    if key is not None:
        return next((job for job in reversed(_jobs.values())
                     if not job.done.is_set() and not job.cancel.is_set() and job.preparation_key == key
                     and (job.context['runId'] == run_id or
                          references.get(job.operation_id, {}).get('runId') == job.context['runId'])
                     and (not preparing_only or not job.ready.is_set())), None)
    return None


def _joined_receipt(job, receipt):
    return dict(receipt, reused=True, executedThisCall=False, operationId=job.operation_id,
                evidenceActionId=job.context['actionId'])


def _active_review(context, directory):
    from types import SimpleNamespace
    from .completion_review import CompletionReview, valid_run_id

    run_id, review_id = context['runId'], context.get('completionReviewId')
    deadline = context.get('completionReviewDeadline')
    if (not context.get('nativeExecutor') or context.get('role') != 'executor'
            or not valid_run_id(review_id) or W.RT.read(directory).get('run_id') != run_id
            or type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise W.Problem('wrong_source', '需要当前运行拥有的复核记录', 403)
    review = CompletionReview(SimpleNamespace(dir=directory), run_id, review_id=review_id)
    state = review.state or {}
    if (not review.active or state.get('ownerRunId', review_id) != run_id
            or state['draft']['basis']['task'] != [context.get('logicalTaskId'), context.get('demandRevision')]):
        raise W.Problem('wrong_source', '需要当前任务拥有的复核记录', 403)
    return review, min(deadline, review.deadline)


def join_preparing(root, context, args, directory, validate_live):
    """Follow an owned host job, including one carried across a runner restart."""
    review, deadline = _active_review(context, directory)
    if time.monotonic() >= deadline:
        return dict(reused=False)
    args = dict(args)
    _milliseconds(args.pop('yield_time_ms', None), 'yield_time_ms')
    key = _preparation_key(root, context, args)
    validate_live()
    if time.monotonic() >= deadline:
        return dict(reused=False)
    with _lock:
        job = _matching_check(key, context['runId'], preparing_only=False,
                              references=review.state.get('checkOperations'))
    if job and job.reservation is not None:
        return _joined_receipt(job, job.snapshot(include_output=False))
    return dict(reused=False)


# 宿主管理的一次后台检查。
# `context` 是工具身份，`op` 是 `code_service.py` 的回执，`process` 是 `workbench.py` 确认的进程记录。
# `ready` 表示启动已知，`done` 表示回执已收尾，`yielded` 决定是否另发原动作终态。
class Job:
    def __init__(self, root, context, args, directory, oid, preparation_key=None):
        self.root, self.context, self.args = Path(root), deepcopy(context), deepcopy(args)
        self.working_directory = None
        self.directory, self.operation_id = Path(directory), oid
        self.preparation_key = preparation_key
        self.key = _key(oid)
        self.admitted = threading.Event()
        self.reservation = None
        self.ready, self.done, self.cancel = threading.Event(), threading.Event(), threading.Event()
        self.lock = threading.RLock()
        self.op = self.process = self.result = None
        self.yielded = False
        self.started = time.time()

    def confirm_preparation_source(self, ws, state):
        if self.preparation_key is not None:
            if state.get('folderObservation') and ws.get('recordBinding'):
                try:
                    observed = _dialogue_source(ws, C.includes_record(self.args))
                except (OSError, W.Problem):
                    observed = None
            elif state.get('folderObservation'):
                observed = ('folder', directory_changes.source_generation(ws['path']))
            else:
                generation = directory_changes.source_generation(ws['path'])
                observed = (W.git_observation.identity(ws), generation, state['branch'], state['revision'])
            if observed != self.preparation_key[1]:
                self.preparation_key = None

    def recorded(self, op):
        with self.lock:
            self.op = deepcopy(op)

    def mark_yielded(self):
        with C.database() as db:
            db.execute("UPDATE operations SET data=json_set(data,'$.backgroundYielded',json('true')) WHERE thread=? AND id=?",
                       (self.context['sessionId'], self.operation_id))
        self.yielded = True

    def preparing_operation(self):
        return dict(id=self.operation_id, operation='check', source=self.context,
                    resourceId=self.args.get('resource_id'), workspaceId=None, beforeRevision=None,
                    started=self.started, state='running', target=dict(checkLabel='后台检查'),
                    backgroundCheck=True, backgroundArgs=self.args, preparationOnly=True,
                    ownerPid=os.getpid(), ownerIdentity=W.RT.identity(os.getpid()))

    def launched(self, process):
        with self.lock:
            self.process = deepcopy(process)
            self.ready.set()
        notify_waiters(self.context['sessionId'], self.context['runId'])

    def snapshot(self, *, include_output=True):
        with self.lock:
            if self.done.is_set():
                return deepcopy(self.result)
            if self.process is None:
                return _preparing(self.operation_id, stopping=self.cancel.is_set())
            process = W.process_record(self.process['id'])
            # 进程退出后还需收集输出和落盘回执；done 前保持 finalizing，不提前暴露已确认的终态。
            state = process['state'] if process['state'] in ACTIVE else 'finalizing'
            page = C.output_page(self.context['sessionId'], process['id']) if include_output else dict(process=process)
            return dict(page, ok=True, background=True, operationId=self.operation_id,
                processId=process['id'], execution=dict(executed=True, processId=process['id'],
                    state=state, exitCode=None, sourceRevision=self.op['beforeRevision']),
                verification=dict(status='running', revision=self.op['beforeRevision']),
                environment=self.op['target'].get('environment'), purpose=self.op['target'].get('purpose', 'acceptance'),
                summary=self.op['target'].get('checkLabel') or '后台检查',
                note='检查仍在运行；需要结果时按process_id查询，不代表检查通过。')

    def run(self):
        from . import control, permissions
        from .completion_review import remaining_time
        from .tools import set_call, reset_call, set_stop
        context_token = set_call(self.context)
        token = observer.set(self)
        watch = control.StopWatch(control.path(self.directory))
        permission_revision = None
        last_check = [0.0]
        def stopped():
            if self.cancel.is_set() or watch() or remaining_time(self.context) == 0:
                self.cancel.set()
                return True
            # 后台检查继续核对所属会话的权限修订和资源授权。
            if time.monotonic() - last_check[0] >= .25:
                last_check[0] = time.monotonic()
                try:
                    if permissions.profile(ident)['revision'] != permission_revision:
                        self.cancel.set()
                    else:
                        # 授权轮询只读，工作区登记由检查准备路径负责。
                        C.context_resource(self.root, self.args['resource_id'], 'execute', register_workspace=False)
                except W.Problem:
                    if self.op is None:
                        # Preflight can still return the resource error before an operation exists.
                        raise
                    self.cancel.set()
                except (OSError, ValueError):
                    self.cancel.set()
            return self.cancel.is_set()
        ident = self.context['sessionId']
        old_stop = set_stop(stopped)
        try:
            permission_revision = permissions.profile(ident)['revision']
            reservation = self.preparing_operation()
            prior = C.reserve_check(ident, reservation)
            if prior is not None:
                self.result = _previous_start(prior, self.args)
                return
            self.reservation = reservation
            self.admitted.set()

            args = dict(self.args)
            if self.working_directory is not None:
                args['directory'] = self.working_directory
            rid = args.pop('resource_id', None)
            requested = args.get('timeout', 30)
            remaining = remaining_time(self.context)
            if remaining is not None and type(requested) is int and 1 <= requested <= 120:
                args['timeout'] = min(requested, max(1, math.ceil(remaining)))
            from . import project_task_view
            with project_task_view.check_scope(ident, rid):
                self.result = C.invoke(self.root, 'check', rid, **args)
            if self.op is None:
                self.finish_preparation()
        except W.Problem as error:
            self.result = dict(ok=False, code=error.code, error=error.message)
            if self.reservation is not None and self.op is None:
                self.finish_preparation()
        except Exception as error:
            self.result = dict(ok=False, code='unknown' if self.op else 'check_start_failed',
                state='unknown' if self.op else 'failed',
                error=('检查回执未确认：' if self.op else '检查启动前失败，未执行命令：') + str(error)[:300])
            if self.op:
                self.result['operationId'] = self.operation_id
                C.finish(ident, self.operation_id, 'unknown', self.result)
            elif self.reservation is not None:
                self.finish_preparation()
        finally:
            set_stop(old_stop)
            observer.reset(token)
            reset_call(context_token)
            with self.lock:
                self.done.set()
                self.ready.set()
                self.admitted.set()
            notify_waiters(ident, self.context['runId'])
            try:
                if self.yielded:
                    publish_result(ident, self.operation_id, self.directory)
            finally:
                with _lock:
                    if _jobs.get(self.key) is self:
                        del _jobs[self.key]

    def finish_preparation(self):
        state = 'cancelled' if self.result.get('code') == 'cancelled' else 'failed'
        self.result = dict(self.result, operationId=self.operation_id,
                           execution=dict(executed=False, state=state, exitCode=None))
        C.finish(self.context['sessionId'], self.operation_id, state, self.result)


def _previous_start(prior, args):
    if not prior.get('backgroundCheck') or prior.get('backgroundArgs') != args:
        raise W.Problem('check_identity_conflict', '同一启动身份不能用于不同检查', 409)
    if prior['state'] != 'running':
        return result(prior)
    return dict(ok=False, code='unknown', state='unknown', operationId=prior['id'],
                processId=prior.get('processId'), error='原检查仍待确认，未重复启动。')


def _operation(ident, oid, job):
    owned = job and job.context['sessionId'] == ident
    if owned and job.reservation is not None and job.op is None and not job.done.is_set():
        return job.reservation
    return C.operation(ident, oid)


def settle_review(root, context, args, directory, validate_live=lambda: None):
    """Wait for the original durable terminal receipts, including during preparation."""
    from . import control

    parent, run_id = context['sessionId'], context['runId']
    review, deadline = _active_review(context, directory)
    references = review.state.get('checkOperations', {})
    if not references or args != {'checks': list(references.values())}:
        raise W.Problem('wrong_source', '收尾等待需要本轮复核登记的检查', 403)
    deadline -= .25
    watch = control.StopWatch(control.path(directory))

    def status(reference):
        oid, resource_id = reference['operationId'], reference['resourceId']
        _, _, workspace, _ = C.context_resource(root, resource_id, 'execute')
        with _lock:
            job = _jobs.get(_key(oid))
        try:
            op = _operation(parent, oid, job)
        except W.Problem as error:
            if error.code != 'not_found':
                raise
            return dict(reference, status='unknown')
        source = op['source']
        if (not op.get('backgroundCheck') or op['resourceId'] != resource_id
                or op.get('workspaceId') not in (None, workspace['id'])
                or source.get('runId') != reference.get('runId', review.review_id)
                or any(source.get(key) != context.get(key) for key in
                       ('sessionId', 'logicalTaskId', 'demandRevision', 'role'))):
            raise W.Problem('wrong_source', '检查不属于本次复核的资源和轮次', 403)
        if op['state'] == 'running':
            owner = op.get('ownerIdentity')
            live_owner = (owner and owner != 'unknown' and op.get('ownerPid')
                          and W.RT.identity(op['ownerPid']) == owner)
            return dict(reference, status='running' if job or live_owner else 'unknown')
        if op.get('backgroundYielded') and not op.get('backgroundPublished') and time.monotonic() < deadline:
            # The existing finalizer is idempotent even if its worker is also publishing.
            publish_result(parent, oid, directory)
        value = op.get('result') or {}
        return dict(reference, status=(value.get('verification') or {}).get('status', op['state']),
                    revision=op.get('beforeRevision'))

    with _waiting_for_results(parent, run_id) as wake:
        while True:
            wake.clear()
            if watch():
                return dict(reason='stopped', checks=[])
            validate_live()
            checks = [status(reference) for reference in references.values()]
            if all(item['status'] != 'running' for item in checks):
                return dict(reason='settled' if all(item['status'] != 'unknown' for item in checks)
                            else 'unconfirmed', checks=checks)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return dict(reason='deadline', checks=checks)
            wake.wait(min(.2, remaining))


def publish_result(ident, oid, directory):
    """One immutable final output, appended to the original action's lifecycle."""
    from .session import Session
    from . import evidence
    from .filelock import FileLock
    with FileLock(Path(directory) / 'background-checks.lock'):
        op = C.operation(ident, oid)
        if op['state'] == 'running' or op.get('backgroundPublished'):
            return
        source = op['source'];aid = source['actionId']
        session = Session(directory);session.run_id = source['runId']
        path = Path(directory) / 'evidence-output' / (aid + '.json')
        value = result(op)
        # 输出先不可变落盘，再追加原动作的终态事件，最后标记已发布；崩溃窗口只补缺失步骤。
        if path.exists():
            data = path.read_bytes()
            expected = json.dumps(evidence.redact(value), ensure_ascii=False, default=str).encode('utf8')
            if data != expected:
                raise W.Problem('evidence_changed', '原始检查回执已有不同内容，未覆盖或伪造终态', 409)
            meta = dict(state='retained',id=aid,hash=hashlib.sha256(data).hexdigest(),bytes=len(data))
            # Only the crash window between immutable output and event needs
            # this scan. Ordinary completion uses the operation's durable flag.
            recorded = False
            with FileLock(Path(directory) / 'events.lock'):
                lines = session.events_path.read_text('utf8').splitlines()
            for line in lines:
                row = json.loads(line)
                if row.get('action_id')==aid and row.get('backgroundCheck') and row.get('outputEvidence')==meta:
                    recorded=True
                    break
        else:
            meta = evidence.capture(session, aid, value, tool='run_project_check')
            recorded=False
        if not recorded:
            session.emit('action', action_id=aid, tool='run_project_check', role=source.get('role', 'executor'),
                state='unknown' if op['state'] == 'unknown' else 'completed' if value['ok'] else 'failed',
                finishedAt=time.time(), outputEvidence=meta, impact=evidence.impact(value),
                operationId=oid, backgroundCheck=True, summary=op['target'].get('checkLabel') or '隔离检查',
                checkState=(value.get('verification') or {}).get('status'), error=value.get('error'))
        with C.database() as db:
            db.execute("UPDATE operations SET data=json_set(data,'$.backgroundPublished',json('true')) WHERE thread=? AND id=?", (ident,oid))


def start(root, context, args, directory, validate_live=lambda: None):
    from .completion_review import remaining_time

    args = deepcopy(args)
    wait_ms = _milliseconds(args.pop('yield_time_ms', None), 'yield_time_ms', 3000)
    deadline = _wait_deadline(context, wait_ms)
    if args.get('capture_changes'):
        raise W.Problem('invalid_arguments', '可回写检查仍需同步独占执行', 400)
    if not context.get('nativeExecutor'):
        raise W.Problem('invalid_agent_call', '后台检查仅用于本轮原生执行者', 403)
    validate_live()
    oid = operation_identity(context)
    if remaining_time(context) == 0:
        return dict(ok=False, code='completion_review_budget', error='收尾复核时限已到')
    registry_key = _key(oid)
    with _lock:
        job = _jobs.get(registry_key)
    joined = False
    if job is None:
        # A previous owner may already have exited. Consult its durable record
        # without holding the job registry. Reservation also enforces uniqueness
        # if another caller finishes between this lookup and insertion.
        try:
            prior = C.operation(context['sessionId'], oid)
        except W.Problem as error:
            if error.code != 'not_found':
                raise
        else:
            with _lock:
                job = _jobs.get(registry_key)
            if job is None:
                return _previous_start(prior, args)
        if job is None:
            from .tools import set_call, reset_call
            token = set_call(context)
            try:
                binding = C.context_resource(
                    root, args.get('resource_id'), 'execute', register_workspace=False)
                thread, resource, workspace, _ = binding
                working_directory = C.check_directory(thread, resource['id'], workspace, args)
            except W.Problem as error:
                return dict(ok=False, code=error.code, error=error.message)
            finally:
                reset_call(token)
            preparation_key = _preparation_key(root, context, dict(args, directory=working_directory), binding)
            if remaining_time(context) == 0:
                return dict(ok=False, code='completion_review_budget', error='收尾复核时限已到')
            candidate = Job(root, context, args, directory, oid, preparation_key)
            candidate.working_directory = working_directory
            with _lock:
                job = _jobs.get(registry_key)
                if job is None:
                    if 'completionReviewDeadline' not in context:
                        job = _matching_check(preparation_key, context['runId'], preparing_only=True)
                    joined = job is not None
                    if not joined:
                        _jobs[registry_key] = job = candidate
            if job is candidate:
                try:
                    from . import project_task_view
                    stage = project_task_view.lookup(context['sessionId'], args.get('resource_id'))
                    threading.Thread(target=job.run, name='owned-check', daemon=stage is None).start()
                except RuntimeError:
                    with _lock:
                        del _jobs[registry_key]
                    job.result = dict(ok=False, code='check_start_failed', error='无法创建后台检查线程，未执行命令。')
                    job.done.set()
                    job.admitted.set()
    if not joined and (job.context != context or job.args != args or job.root.resolve() != Path(root).resolve()):
        raise W.Problem('check_identity_conflict', '同一启动身份不能用于不同检查', 409)
    # Durable admission is required even when the caller requests a zero
    # execution wait. Only callers of this check wait for its registration.
    job.admitted.wait()
    # The caller's wait covers preparation and execution. The host retains the
    # running job even when the caller has to yield before it has a process ID.
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if job.ready.wait(min(.05, remaining)):
            job.done.wait(max(0, deadline - time.monotonic()))
            break
        try:
            validate_live()
        except Exception:
            if not joined:
                job.cancel.set()
            raise
    with job.lock:
        if job.done.is_set():
            receipt = deepcopy(job.result)
        else:
            try:
                validate_live()
            except Exception:
                if not joined:
                    job.cancel.set()
                raise
            if remaining_time(context) == 0 and not joined:
                job.cancel.set()
            # The owner still publishes a synchronous result if another caller yields first.
            if not joined and not job.yielded:
                job.mark_yielded()
            receipt = job.snapshot()
        if joined:
            validate_live()
            return _joined_receipt(job, receipt)
        return receipt


def _query_page(ident, process, args, stopped=lambda: False):
    if 'query' in args:
        return C.search_check_output(process, args['query'], stopped=stopped)
    return C.output_page(ident, process['id'], args.get('after', 0), args.get('stream'))


def _output_version(pid, stream):
    if stream is not None and stream not in ('stdout', 'stderr'):
        raise W.Problem('invalid_stream', 'stream为stdout或stderr')
    path = W.record_path(pid).with_suffix(f'.{stream}.log' if stream else '.log')
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except FileNotFoundError:
        return None


# `job` 是本模块持有的后台句柄。
# `op` 按 `code_service.py` 的操作 ID 查账，`process` 来自 `workbench.py`。
# 等待期限同时受调用方请求和 `completion_review.py` 的收尾预算限制。
def _wait_for_result(job, context, op, process, args, deadline):
    from . import control, subtasks
    from .tools import current_stop

    parent, run_id = context['sessionId'], context['runId']
    watch = control.StopWatch(control.path(job.directory),
                              deadline=control.benchmark_deadline(job.directory, 'executor'))
    halt = current_stop()
    output_wait = process is not None and any(key in args for key in ('after', 'query', 'stream'))
    version = object()
    state = process['state'] if process is not None else None

    def stopped():
        if watch.expired():
            return 'run_deadline'
        if watch() or (halt and halt()):
            return 'stopped'
        return None

    next_probe = 0.0
    notified = True
    with _waiting_for_results(parent, run_id) as wake:
        while True:
            wake.clear()
            if reason := stopped():
                return op, reason, None
            if op['state'] != 'running':
                return op, 'check_ready', None
            if job.done.is_set():
                op = _operation(parent, op['id'], job)
                return op, 'check_ready' if op['state'] != 'running' else 'unconfirmed', None
            if time.monotonic() >= deadline:
                return op, 'wait_timeout', None
            if notified or time.monotonic() >= next_probe:
                next_probe = time.monotonic() + .25
                # `subtasks.py` 通知本轮子任务有结果时可提前唤醒调用方。返回前再次读检查回执，已确认的检查终态优先。
                if subtasks.pending_result(parent, run_id, job.directory):
                    op = _operation(parent, op['id'], job)
                    if reason := stopped():
                        return op, reason, None
                    if op['state'] != 'running':
                        return op, 'check_ready', None
                    if time.monotonic() >= deadline:
                        return op, 'wait_timeout', None
                    return op, 'subtask_ready', None
            if process is None and op.get('processId') and job.ready.is_set():
                return op, 'process_ready', None
            if output_wait:
                current = _output_version(process['id'], args.get('stream'))
                if current != version:
                    version = current
                    try:
                        page = _query_page(parent, process, args,
                                           lambda: stopped() or time.monotonic() >= deadline)
                    except W.Problem as error:
                        if error.code != 'cancelled':
                            raise
                        return op, stopped() or 'wait_timeout', None
                    if page.get('text') or page.get('matches'):
                        return op, 'output', page
                if W.process_record(process['id'])['state'] != state:
                    return op, 'state_changed', None
            # Fall back to durable reads if the notification is lost.
            notified = wake.wait(min(.05, max(0, deadline - time.monotonic())))
            op = _operation(parent, op['id'], job)


def environments(root, context, args, directory):
    if (set(args) - {'resource_id', 'environment_id', 'remove'}
            or not isinstance(args.get('resource_id'), str)
            or args.get('environment_id') is not None and not isinstance(args['environment_id'], str)
            or type(args.get('remove', False)) is not bool):
        raise W.Problem('invalid_arguments', '准备环境请求参数无效', 400)
    if W.RT.read(directory).get('run_id') != context['runId']:
        raise W.Problem('wrong_run', '所属运行已变化，未操作准备环境', 409)
    session = W.read(Path(directory) / 'session.json')
    task = session.get('taskInput') if isinstance(session, dict) else None
    if not isinstance(task, dict) or not task.get('id') or task['id'] != context.get('logicalTaskId'):
        raise W.Problem('wrong_task', '准备环境只对当前任务可用', 403)
    return C.review_check_environments(root, **args)


# `agent_gateway.py` 传入已核对的会话上下文。
# 查询与停止都可沿操作或进程身份找回原检查；查询超时不结束后台检查。
def query(root, context, args, *, stop=False, validate_live=lambda: None):
    args = dict(args)
    allowed = {'resource_id', 'process_id', 'operation_id'} if stop else {'resource_id', 'process_id', 'operation_id', 'after', 'query', 'stream', 'wait_ms'}
    if set(args) - allowed or 'query' in args and (set(args) & {'after', 'stream'}):
        raise W.Problem('invalid_arguments', '未声明的检查查询参数，query与after/stream分别使用', 400)
    wait_ms = _milliseconds(args.pop('wait_ms', None), 'wait_ms')
    deadline = _wait_deadline(context, wait_ms)
    rid, pid = args.pop('resource_id', None), args.get('process_id')
    # 读输出需要当前读取授权；停止路径仍严格绑定原会话/资源/进程，允许撤权后请求收尾。
    if not stop:
        thread, _, ws, _ = C.context_resource(root, rid)
    oid = args.pop('operation_id', None)
    if (pid is None) == (oid is None):
        raise W.Problem('invalid_arguments', 'process_id与operation_id需且只能提供一个', 400)
    reason = None
    if oid is not None:
        if not isinstance(oid,str) or len(oid)!=32 or any(c not in '0123456789abcdef' for c in oid):
            raise W.Problem('invalid_arguments', 'operation_id需复制原启动回执', 400)
        key = _key(oid)
        with _lock:
            owned = _jobs.get(key)
        try:
            recorded = _operation(context['sessionId'], oid, owned)
        except W.Problem as error:
            if error.code != 'not_found':raise
            return dict(ok=False,code='unknown',operationId=oid,execution=dict(state='unknown',exitCode=None),
                error='未找到原检查的确认记录，未重新执行。')
        if recorded['resourceId']!=rid or not recorded.get('backgroundCheck'):
            raise W.Problem('wrong_source', '操作不属于当前资源的后台检查', 403)
        pid = recorded.get('processId')
        if not pid and wait_ms and owned and recorded['state'] == 'running':
            recorded, reason, _ = _wait_for_result(owned, context, recorded, None, args, deadline)
            pid = recorded.get('processId')
        if not pid:
            if not stop:
                if wait_ms:
                    _, _, ws, _ = C.context_resource(root, rid)
                if recorded.get('workspaceId') not in (None, ws['id']):
                    raise W.Problem('wrong_source', '检查工作区已变化', 403)
            validate_live()
            if stop and owned and not owned.done.is_set() and recorded['state'] == 'running':
                owned.cancel.set()
                with C.database() as db:
                    db.execute("UPDATE operations SET data=json_set(data,'$.cancelRequested',json('true'))"
                               " WHERE thread=? AND id=? AND json_extract(data,'$.state')='running'",
                               (context['sessionId'], oid))
                return dict(_preparing(oid, stopping=True), state='stopping')
            if recorded['state'] != 'running':
                value = result(recorded)
                reason = reason or ('check_ready' if wait_ms else None)
            elif owned and not owned.done.is_set():
                value = _preparing(oid, stopping=owned.cancel.is_set())
            else:
                value = dict(ok=False, code='unknown', background=True, operationId=oid,
                             execution=dict(state='unknown', exitCode=None),
                             summary='检查尚无已确认进程，不要重新启动。')
            return dict(value, waitReason=reason) if reason else value
        args['process_id'] = pid
    process = W.process_record(pid)
    if (process.get('conversationId') != context['sessionId']
            or oid is not None and process.get('operationId') != oid):
        raise W.Problem('wrong_source', '检查不属于当前会话和所选资源', 403)
    oid = process.get('operationId')
    op = C.operation(context['sessionId'], oid)
    if op['resourceId'] != rid or op['workspaceId'] != process.get('workspaceId'):
        raise W.Problem('wrong_source', '检查不属于所选资源', 403)
    if not stop and process.get('workspaceId') != ws['id']:
        raise W.Problem('wrong_source', '检查工作区已变化', 403)
    if not op.get('backgroundCheck'):
        if stop:
            raise W.Problem('not_background_check', '此记录不属于后台检查', 409)
        return C.invoke(root, 'output', rid, **args)
    key = _key(oid)
    with _lock:
        job = _jobs.get(key)
    # stop 只发取消/停止请求，最终退出及输出仍由原工作线程收尾，返回值不代表已经退出。
    if stop:
        if job:
            job.cancel.set()
        return dict(W.stop_process(pid), operationId=oid, processId=pid)
    if reason == 'process_ready':
        reason = None
    page = None
    if wait_ms and reason is None:
        if job and op['state'] == 'running':
            op, reason, page = _wait_for_result(job, context, op, process, args, deadline)
        elif op['state'] != 'running':
            reason = 'check_ready'
    # A long wait cannot retain read access revoked while it was waiting.
    _, _, current_ws, _ = C.context_resource(root, rid)
    if current_ws['id'] != process.get('workspaceId'):
        raise W.Problem('wrong_source', '检查工作区已变化', 403)
    validate_live()
    if wait_ms and job:
        op = C.operation(thread['id'], oid)
        if op['state'] != 'running' and reason not in {'stopped', 'run_deadline', 'wait_timeout'}:
            reason = 'check_ready'
    if op['state'] != 'running':
        directory = job.directory if job else W.P.data_root() / 'runs' / thread['id']
        if (op.get('backgroundYielded') and directory.is_dir()
                and not (directory/'evidence-output'/(op['source']['actionId']+'.json')).exists()):
            publish_result(thread['id'],oid,directory)
        value = result(op)
        page = _query_page(thread['id'], process, args)
        return dict(page, **value, **({'waitReason': reason} if reason else {}))
    # 持久记录仍在 running 却没有本进程持有者时保留不确定性，裸 PID 不能证明所有权。
    if not job:
        return dict(ok=False, code='unknown', operationId=oid, processId=pid,
            execution=dict(state='unknown', exitCode=None), error='检查持有者已丢失，未接管PID或重放检查。')
    explicit_page = any(key in args for key in ('after', 'query', 'stream'))
    value = job.snapshot(include_output=not wait_ms and not explicit_page)
    if page is None and explicit_page and reason not in {'stopped', 'run_deadline', 'wait_timeout'}:
        page = _query_page(thread['id'], process, args)
    if page is not None:
        value.update({key: v for key, v in page.items() if key not in {'ok', 'process'}})
    if reason:
        value['waitReason'] = reason
    return value


def completed(ident, task_id):
    with C.database() as db:
        rows = db.execute("""SELECT data FROM operations WHERE thread=?
            AND json_extract(data,'$.backgroundCheck')=1
            AND json_extract(data,'$.source.logicalTaskId')=?
            AND json_extract(data,'$.state')!='running' ORDER BY seq""", (ident, task_id)).fetchall()
    return [json.loads(raw) for raw, in rows]


def completion_view(ident, run, completion):
    """Refresh late check facts in the UI; never infer user acceptance or run Git."""
    pending = [p for p in completion.get('pending',[]) if p.get('kind')=='check' and p.get('state')=='running']
    if not pending:
        return completion
    with C.database() as db:
        rows=db.execute("""SELECT data FROM operations WHERE thread=?
            AND json_extract(data,'$.backgroundCheck')=1
            AND json_extract(data,'$.source.runId')=?""",(ident,run)).fetchall()
    ops={op['id']:op for raw, in rows for op in [json.loads(raw)]}
    # 晚到结果只更新完成卡中的检查事实；它属于当时源码版本，不重算用户需求是否验收通过。
    view=deepcopy(completion)
    active=returned=0
    for gap in view.get('pending',[]):
        op=ops.get(gap.get('id'))
        if not op or gap.get('state')!='running':continue
        if op['state']=='running':
            active+=1
            gap['text']='后台检查仍在运行：'+op['target'].get('checkLabel','检查')
        else:
            returned+=1
            gap.update(state='returned',text='后台检查已返回：'+str((op.get('result',{}).get('verification') or {}).get('status','unknown'))+
                '；结果属于当时源版本，请查看回执，不代表需求验收。')
    if view.get('status')=='success' and (active or returned):
        view['label']='本轮已结束 · '+(str(active)+'项检查仍在运行' if active else '后台检查已返回')
    return view


def owned_jobs():
    home = str(W.home().resolve())
    with _lock:
        return [job for (root, _), job in _jobs.items() if root == home and not job.done.is_set()]


def recover():
    """A restarted host observes abandoned operations; it never respawns them."""
    with C.database() as db:
        rows = db.execute("""SELECT thread,data FROM operations
            WHERE json_extract(data,'$.backgroundCheck')=1
            AND (json_extract(data,'$.state')='running' OR
                (json_extract(data,'$.backgroundYielded')=1 AND coalesce(json_extract(data,'$.backgroundPublished'),0)=0))""").fetchall()
    for ident, raw in rows:
        op = json.loads(raw)
        key = _key(op['id'])
        with _lock:
            if key in _jobs:
                continue
        if op['state'] == 'running' and op.get('ownerPid'):
            owner = W.RT.identity(op['ownerPid'])
            if owner is not None and owner in ('unknown', op.get('ownerIdentity')):
                continue
        process = None
        try:
            directory = W.P.data_root() / 'runs' / ident
            if op['state']!='running':
                if directory.is_dir():publish_result(ident,op['id'],directory)
                continue
            if op.get('preparationOnly'):
                cancelled = op.get('cancelRequested') is True
                state = 'cancelled' if cancelled else 'interrupted'
                value = dict(ok=False, code='cancelled' if cancelled else 'check_interrupted',
                             operationId=op['id'], execution=dict(executed=False, state=state, exitCode=None),
                             error='检查在准备阶段结束，未启动进程。')
                C.finish(ident, op['id'], state, value)
                if directory.is_dir():
                    publish_result(ident, op['id'], directory)
                continue
            if op.get('processId'):
                try:
                    process = W.process_record(op['processId'])
                except W.Problem as error:
                    if error.code != 'not_found':raise
            value = dict(ok=False, code='unknown', operationId=op['id'],
                process=process, execution=dict(state='unknown', exitCode=(process or {}).get('exitCode'),
                    sourceRevision=op['beforeRevision']),
                verification=dict(status='unknown',revision=op['beforeRevision']),
                error='后台检查持有者已退出；保留原进程和输出事实，未接管PID或重新执行。')
            C.finish(ident, op['id'], 'unknown', value)
            if directory.is_dir():
                publish_result(ident, op['id'], directory)
        except (W.Problem, OSError, ValueError) as error:
            import warnings
            warnings.warn('后台检查恢复未完整确认：'+op['id']+' '+type(error).__name__, RuntimeWarning)
