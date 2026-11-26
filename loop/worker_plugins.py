"""Public worker contract over installed packages and the existing child queue.

No scheduler or second history store. Each delegation keeps its protocol state
inside its subtask row. Unknown execution is reconciled, never retried by query.
"""
# 主要引用方包括 `agent_gateway.py` 和 `subtasks.py`。
from copy import deepcopy
import json
from pathlib import Path
import time
import uuid

from . import workbench as W, plugins, projects, subtasks as S, parallel_budget as B, task_core_models
from .filelock import FileLock
from .worker_contract import PROTOCOL, CAPABILITIES, encoded, validate


def registration(parent):
    from .context_registry import ScopedRegistry
    registry=ScopedRegistry()
    for package in plugins.grants(parent):
        if package.get('workersEnabled'):
            for worker in package.get('workers',[]):
                registry.register(package['id']+'/'+worker['name'],(package,worker))
    return registry.resolve()


def _model_status(worker, parent_identity=None):
    role = worker.get('modelRole')
    if not role:
        return dict(core_type='sub_agent', available=True)
    try:
        model = task_core_models.public(task_core_models.bind(role, parent_identity))
        return dict(core_type='task_core', model=model, available=True)
    except W.Problem as error:
        return dict(core_type='task_core', available=False, reason=error.code)


def discover(parent):
    from .tools import current_call

    identity = (current_call() or {}).get('parentModelIdentity')
    return dict(protocol=PROTOCOL, capabilities=sorted(CAPABILITIES), workers=[dict(
        plugin_id=p['id'],worker=name, fingerprint=p['fingerprint'], revision=p['revision'],
        **{k: deepcopy(v) for k, v in w.items() if k != 'method'},
        **_model_status(w, identity),
        unavailableOptional=sorted(set(w.get('optional',[]))-CAPABILITIES))
        for name,(p,w) in registration(parent).items()],
        unsupported=['recursive-delegation'])


def check_grant(row):
    spec=row.get('worker')
    if not spec:return
    package=plugins.authorized(row['parentId'],spec['pluginId'],'workersEnabled')
    if package['fingerprint']!=spec['fingerprint'] or package['revision']!=spec['pluginRevision']:
        raise W.Problem('worker_revoked','委派贡献已停用、卸载或授权已变化',403)


def owner(parent, sv, *, live=True):
    from .tools import current_call
    source=current_call() or {}
    thread=projects.conversation(parent)
    if source.get('sessionId')!=parent or thread.get('subtaskOf') or thread.get('readonlyEvidence'):
        raise W.Problem('worker_owner','仅所属父任务可操作此委派',403)
    directory=Path(sv._resolve_sid(parent))
    runtime=W.RT.read(directory)
    if live and (runtime.get('run_id')!=source.get('runId') or not W.RT.status(directory)['active']):
        raise W.Problem('wrong_run','父运行已变化或停止',409)
    state=W.read(directory/'session.json')
    task=state.get('taskInput') or {}
    # task_input is persisted by the native entry; fall back only for a new,
    # pre-checkpoint run, not to another chat's or another logical task's state.
    task_id=task.get('id') or state.get('executorWork',{}).get('taskId') or source.get('runId')
    return source,dict(id=task_id,revision=task.get('revision') or state.get('executorWork',{}).get('demandRevision',1))


# `agent_gateway.py` 提交原会话的工具调用。
# `owner` 从 `tools.py` 的动作身份和宿主会话记录取得 `source` 与逻辑任务版本。
def delegate(parent, args, sv):
    source,task=owner(parent,sv)
    allowed={'worker','request_key','input','resource_ids','kind','expected_revision','model_calls'}
    if set(args)-allowed or not {'worker','request_key','input','resource_ids','kind'}<=set(args):
        raise W.Problem('worker_arguments','委派字段缺失或未知')
    found=registration(parent).get(args['worker'])
    if not found:raise W.Problem('worker_unavailable','委派贡献未在本轮注册或已撤权',403)
    package,contract=found
    validate(args['input'],contract['inputSchema']);encoded(args['input'],8000)
    caps=set(contract['requires'])|set(contract.get('optional',[]))&CAPABILITIES
    if args['kind']=='code' and 'isolated-write' not in caps:
        raise W.Problem('worker_capability','此贡献未声明隔离写入能力',403)
    # 委派记录随 `subtasks.py` 的队列行持久化。
    # 插件 ID、版本和摘要绑定原贡献，`taskId` 与 `demandRevision` 标识委派要求，`messages`、`results`、`requests` 保存投递及去重状态。
    spec=dict(protocol=PROTOCOL,pluginId=package['id'],pluginRevision=package['revision'],
        fingerprint=package['fingerprint'],name=contract['name'],contract=contract,
        taskId=task['id'],demandRevision=task['revision'],input=deepcopy(args['input']),
        capabilities=sorted(caps),messages=[],results=[],requests={},attempt=1,
        operationId='wop-'+uuid.uuid4().hex)
    if role := contract.get('modelRole'):
        spec['modelBinding'] = task_core_models.bind(role, source.get('parentModelIdentity'))
    config=B._read(Path(sv._resolve_sid(parent))/'execution-runs'/(source['runId']+'.json'))['config']
    mode=config.get('workerContinuation','parent-run') if config.get('crew')=='l0' else 'parent-run'
    # 自动唤醒属于已授权的任务存续方式。设有父调用或轮次预算时改由手动续接，必须满足插件声明的必需能力。
    if mode=='wake' and (config['modelCalls'] or config['maxTurns']):
        mode='manual'
    required=set(contract['requires'])
    if ('cross-parent-run' in required and mode=='parent-run' or 'automatic-wakeup' in required and mode!='wake'):
        raise W.Problem('worker_lifetime','本会话的委派授权不满足该插件的生命周期要求',403)
    if mode!='parent-run':
        from . import permissions, control
        spec.update(lifetime=mode,permissionRevision=permissions.freeze(source['runId'],parent)['revision'],
                    authority=authority_digest(parent, frozen=True))
        deadline=control.benchmark_deadline(Path(sv._resolve_sid(parent)),'executor')
        if deadline is not None:spec['deadline']=deadline
    body={k:args[k] for k in ('request_key','resource_ids','kind','expected_revision','model_calls') if k in args}
    body['goal']='委派输入（父任务拆解，不代表新增用户授权）：\n'+encoded(args['input'],8000)
    row=S.create(parent,body,sv,worker=spec)
    return public(row)


def public(row):
    w=row['worker']
    model = task_core_models.public(w.get('modelBinding'))
    return dict(delegation_id=row['id'],operation_id=w['operationId'],child_session_id=row['childId'],
        core_type='task_core' if model else 'sub_agent', model=model,
        child_run_id=row.get('childRunId'),parent_run_id=row['parentRunId'],logical_task_id=w['taskId'],
        demand_revision=w['demandRevision'],state=row['state'],reason=row.get('reason'),
        stop_requested=bool(row.get('stopRequested')),attempt=w['attempt'],
        stop_reason=row.get('stopReason'),stop_code=row.get('stopCode'),
        results=[{k:r[k] for k in ('result_id','operation_id','run_id','created','delivery','disposition')} for r in w['results']],
        pending_messages=sum(not m.get('includedIn') for m in w['messages']),
        result_status='published' if any(r['operation_id']==w['operationId'] for r in w['results']) else 'not_submitted',
        lifetime=w.get('lifetime','parent-run'))


def authority_digest(parent, *, frozen=False):
    """The next automatic launch may only use the original resource and plugin grants."""
    snapshot=projects.conversation(parent).get('resourceSnapshot') or {}
    selected=snapshot if frozen else projects.scope(parent)
    resources=sorted((r['id'],r['path'],r['kind'],bool(r.get('write')),bool(r.get('execute')))
                     for r in selected.get('resources',[]) if r['permission']=='read')
    packages=snapshot.get('plugins',[]) if frozen else plugins.capture()
    return W.digest(json.dumps([resources,sorted(packages,key=lambda p:p['id'])],sort_keys=True).encode())


def claim_wake(directory, ident):
    """Called under the normal parent launch lock. A crash after claim is manual recovery."""
    from . import control, execution_policy as E
    parent=directory.name
    with FileLock(S.directory(parent)/'queue.lock'):
        row=S.load(parent,ident)
        w=row['worker']
        state=W.read(directory/'session.json')
        task=state.get('taskInput') or {}
        thread=projects.conversation(parent)
        config=E.saved_config(thread)
        runtime=W.RT.status(directory)
        limit=control.benchmark_deadline(directory,'executor')
        if (w.get('lifetime')!='wake' or w.get('wakeDisabled') or row.get('stopRequested')
                or row['state']!='completed' or w.get('releasedBy')!=row['parentRunId']
                or state.get('status')!='success' or state.get('run_id')!=runtime.get('run_id')
                or runtime['active'] or runtime.get('state')!='completed'
                or task.get('id')!=w['taskId'] or task.get('revision')!=w['demandRevision']
                or config.get('workerContinuation')!='wake' or config['crew']!='l0'
                or config!=B._read(row['rootBudget'])['config']
                or thread.get('archived') or thread.get('readonlyEvidence') or thread.get('subtaskOf')
                or thread.get('projectId')!=row['projectId'] or authority_digest(parent)!=w['authority']
                or W.read(control.path(directory)).get('stop') or (directory/'pending-after.json').exists()
                or (directory/'pending-after.claimed.json').exists() and not (directory/'pending-after.result.json').exists()
                or limit is not None and time.monotonic()>=limit
                or not any(not r['delivery'].get('notifiedIn') for r in w['results'])
                or any(r['worker'].get('wakeClaim') for r in S.listing(parent,row['parentRunId']) if r.get('worker'))):
            raise W.Problem('worker_wake_ineligible','保留成果，当前状态由用户续接',409)
        S._source(parent,row)
        w['wakeClaim']=dict(afterRun=state['run_id'],at=time.time())
        S.save(row)
        # A wake processes the existing task once; it grants no chain of further wakes.
        return dict(config,workerContinuation='manual')


def on_worker_finished(parent, ident, sv):
    if not hasattr(sv,'_active_proc'):return
    directory=Path(sv._resolve_sid(parent))
    row=S.load(parent,ident)
    w=row['worker']
    if (w.get('lifetime')!='wake' or w.get('wakeDisabled') or w.get('wakeClaim')
            or row['state']!='completed' or not any(not r['delivery'].get('notifiedIn') for r in w['results'])):
        return
    # Wait on the existing OS process after releasing the worker slot, outside
    # all locks. Normal next-request delivery takes precedence while it works.
    proc=sv._active_proc(directory)
    if proc is not None:
        proc.wait()
    with W._lock:
        status,reply=sv._continue(
            directory, '继续', crew='l0', user_input=False, worker_wake=ident, wait_for_launch=False
        )
        with FileLock(S.directory(parent)/'queue.lock'):
            row=S.load(parent,ident)
            claim=row['worker'].get('wakeClaim')
            if claim and 'outcome' not in claim:
                claim.update(outcome='started' if status==200 else 'manual',runId=W.RT.read(directory).get('run_id') if status==200 else None,
                             reason=reply.get('code'))
                S.save(row)


# `args.action` 选择查询、读成果、发送补充、续接、取消或确认接收。
# 只读证据与会触发状态变化的动作各有授权边界，写动作按请求键去重。
def _wait_for_result(parent, ident, task_id, wait_ms, validate_live):
    deadline = time.monotonic() + wait_ms / 1000
    while time.monotonic() < deadline:
        validate_live()
        row = S.load(parent, ident)
        worker = row.get('worker')
        if not worker or worker['taskId'] != task_id:
            raise W.Problem('worker_owner', '委派不属于本逻辑任务', 403)
        if row['state'] not in {'queued', 'preparing', 'running', 'stopping'}:
            return
        if any(result['operation_id'] == worker['operationId'] for result in worker['results']):
            return
        time.sleep(min(.25, max(0., deadline - time.monotonic())))
    validate_live()


def manage(parent,args,sv,validate_live=lambda: None):
    source,task=owner(parent,sv)
    action=args.get('action')
    allowed={'action','delegation_id','request_key','text','result_id','disposition','wait_ms'}
    if set(args)-allowed or action not in {'query','message','resume','cancel','result','receipt'}:
        raise W.Problem('worker_arguments','未知操作或字段')
    fields={'query':set(),'cancel':set(),'message':{'request_key','text'},'resume':{'request_key','text'},
            'result':{'result_id'},'receipt':{'request_key','result_id','disposition'}}[action]
    supplied = set(args)-{'action','delegation_id'}
    if supplied != fields and not (action == 'query' and supplied == {'wait_ms'}):
        raise W.Problem('worker_arguments','操作字段必须与所选 action 一致')
    ident=args.get('delegation_id')
    wait_ms = args.get('wait_ms', 0)
    if type(wait_ms) is not int or not 0 <= wait_ms <= 30000:
        raise W.Problem('worker_arguments', 'wait_ms 须为 0 到 30000 毫秒')
    if wait_ms:
        _wait_for_result(parent, ident, task['id'], wait_ms, validate_live)
    # `subtasks.py` 持有归属与队列状态。先核对 `taskId` 并归集既有状态，再处理本次主动操作。
    with W._lock,FileLock(S.directory(parent)/'queue.lock'):
        row=S.load(parent,ident)
        if not row.get('worker') or row['worker']['taskId']!=task['id']:
            raise W.Problem('worker_owner','委派不属于本逻辑任务',403)
        row=S.reconcile(row,sv)
        # Reading retained evidence is allowed after disabling the package; code
        # resources still undergo their current read grants. Mutation never does.
        if action!='cancel' and row.get('derivedProjectId'):S.audit_child(row['childId'])
        if action in {'query','result'}:
            if action=='query':return public(row)
            result=next((r for r in row['worker']['results'] if r['result_id']==args.get('result_id')),None)
            if not result:raise W.Problem('worker_result','须使用此委派的 result_id，不能传 actionId/operationId',404)
            result['delivery']['readActionId']=source.get('actionId')
            S.save(row)
            reply = dict(delegation_id=ident, **deepcopy(result))
            if _usable_transcription(result, task['revision']):
                reply['writeParts'] = [_transcription_part(ident, result['result_id'])]
            return reply
        if action=='cancel':pass
        else:
            if row['parentRunId']!=source['runId'] and not S.task_scoped(row):
                raise W.Problem('worker_lifetime','该委派只授权本次父运行',409)
            if action in {'message','resume'} and task['revision']!=row['worker']['demandRevision']:
                raise W.Problem('worker_revision','需求已变更，新的调查须另行委派',409)
            check_grant(row)
            if action!='receipt':S._source(parent,row)
            key=args.get('request_key')
            if not isinstance(key,str) or not 1<=len(key)<=100:
                raise W.Problem('worker_arguments','写操作须提供稳定 request_key')
            signature=W.digest(encoded(args).encode())
            previous=row['worker']['requests'].get(key)
            if previous:
                if previous['hash']!=signature:raise W.Problem('request_changed','此键已绑定不同操作',409)
                return dict(previous['reply'],duplicate=True)
            if len(row['worker']['requests'])>=128:
                raise W.Problem('worker_capacity','委派操作记录已满，未丢弃旧证据',413)
            reopened=False
            if action in {'message','resume'}:
                text=args.get('text')
                if not isinstance(text,str) or not 1<=len(text)<=8000:
                    raise W.Problem('worker_arguments','补充或续接须提供明确文字')
                if len(row['worker']['messages'])>=64:
                    raise W.Problem('worker_capacity','补充消息记录已满',413)
                if action=='message' and row['state'] not in {'queued','preparing','running'}:
                    raise W.Problem('worker_inactive','运行已结束；请明确续接',409)
                if action=='resume':
                    if row['id'] in S._workers or W.RT.status(sv._resolve_sid(row['childId']))['active'] or row['state'] in {'queued','preparing','running','stopping'}:
                        raise W.Problem('worker_active','上一执行尚未确认退出，未重派',409)
                    if row.get('stopRequested'):
                        raise W.Problem('worker_cancelled','已取消委派不能被晚到消息重新启动',409)
                    if not row.get('derivedProjectId') or not W.read(Path(sv._resolve_sid(row['childId']))/'session.json').get('msgs'):
                        raise W.Problem('worker_unknown','没有可核对历史的启动不得重放；新工作使用新委派',409)
                    reopened=bool(B._read(row['rootBudget'])['allocations'][row['id']]['terminal'])
                    B.reopen(row['rootBudget'],row['id'])
                    row['worker']['attempt']+=1
                    row['worker']['operationId']='wop-'+uuid.uuid4().hex
                    row.update(state='queued',reason='显式续接原子会话；保留原历史和隔离目录',resumeText='继续：'+text,
                        previousChildRunId=row.get('childRunId'),childRunId=None,childPid=None,childIdentity=None)
                else:
                    row['worker']['messages'].append(dict(id='wmsg-'+uuid.uuid4().hex,text=text,source=source,created=time.time()))
            elif action=='receipt':
                result=next((r for r in row['worker']['results'] if r['result_id']==args.get('result_id')),None)
                if not result or args.get('disposition') not in {'accepted','partial','rejected','deferred'}:
                    raise W.Problem('worker_result','须指定真实成果和采用处置')
                result['disposition']=dict(value=args['disposition'],source=source,time=time.time())
            reply=public(row)
            row['worker']['requests'][key]=dict(hash=signature,reply=reply)
            try:
                S.save(row)
            except Exception:
                if reopened:B.release(row['rootBudget'],row['id'],confirmed=True)
                raise
    if action=='cancel':return public(S.cancel(parent,ident,sv))
    if action=='resume':S.schedule(parent,ident,sv)
    return reply


def _transcription_part(delegation_id, result_id):
    return dict(delegation_id=delegation_id, result_id=result_id)


def _usable_transcription(result, revision):
    disposition = (result.get('disposition') or {}).get('value')
    text = (result.get('data') or {}).get('transcription')
    return (result.get('demand_revision') == revision and disposition not in {'rejected', 'deferred'}
            and bool((result.get('delivery') or {}).get('readActionId'))
            and isinstance(text, str) and bool(text.rstrip('\r\n')))


def default_transcription_parts(thread, source, proposed, *, guard_changed_copy=False):
    """Resolve implicit adoption without silently substituting any proposed characters."""
    revision = source.get('demandRevision')
    task_id = source.get('logicalTaskId')
    if thread.get('subtaskOf') or thread.get('readonlyEvidence') or not task_id or type(revision) is not int:
        return None

    try:
        rows = [row for row in S.listing(thread['id'])
                if (row.get('worker') or {}).get('taskId') == task_id
                and row['worker'].get('demandRevision') == revision]
        rows = S._readable_results(thread['id'], rows)
    except W.Problem:
        # A legacy/corrupt row cannot make ordinary text writes depend on a default.
        return None

    available = [(row, result) for row in rows for result in row['worker']['results']
                 if _usable_transcription(result, revision)]
    if not available:
        return None

    if proposed is None:
        accepted = [(row, result) for row, result in available
                    if (result.get('disposition') or {}).get('value') == 'accepted']
        if len(accepted) > 1:
            raise W.Problem('transcription_selection_required', '已有多个已采用成果，请用 parts 指定所需版本和片段', 409)
        if not accepted:
            return None
        row, result = accepted[0]
        return [_transcription_part(row['id'], result['result_id'])]

    body = proposed.rstrip('\r\n')
    exact = [(row, result) for row, result in available
             if result['data']['transcription'].rstrip('\r\n') == body]
    if exact:
        accepted = [(row, result) for row, result in exact
                    if (result.get('disposition') or {}).get('value') == 'accepted']
        choices = accepted or exact
        if len(choices) != 1:
            raise W.Problem('transcription_selection_required', '有多份正文一致的成果，请用 parts 选择实际采用的成果', 409)
        row, result = choices[0]
        reference = _transcription_part(row['id'], result['result_id'])
        if result['data']['transcription'] == proposed:
            return [reference]
        suffix = proposed[len(body):]
        return [dict(reference, start=0, end=len(body))] + ([dict(text=suffix)] if suffix else [])

    # This only detects a near-copy on a new, single-line delivery. A proposal
    # selected as unrelated or explicitly rejected can still use ordinary text.
    if not guard_changed_copy or len(body) < 12 or len(body.splitlines()) != 1:
        return None
    for row, result in available:
        original = result['data']['transcription'].rstrip('\r\n')
        if len(original) != len(body):
            continue
        changed = [i + 1 for i, (a, b) in enumerate(zip(original, body)) if a != b]
        if len(changed) * 5 <= len(body):
            if row.get('derivedProjectId'):
                S.audit_child(row['childId'])
            ref = json.dumps([_transcription_part(row['id'], result['result_id'])], separators=(',', ':'))
            positions = '、'.join(map(str, changed[:8])) + (' 等' if len(changed) > 8 else '')
            raise W.Problem('transcription_reference_required',
                            f'正文与已读取候选仅在第 {positions} 位有差异。'
                            f'按原候选交付用 parts={ref}，如需换行单独追加 text 项。'
                            '有新证据时在 parts 中仅替换相应区间，其余仍引用原版本。'
                            '确认整份候选不适用时，先记录 rejected 再独立写入。', 409)
    return None


def compose_transcription(thread, source, parts):
    """Resolve the parent's selection without asking a model to copy the results."""
    if (thread.get('subtaskOf') or thread.get('readonlyEvidence') or not source.get('logicalTaskId')
            or type(source.get('demandRevision')) is not int):
        raise W.Problem('worker_result_scope', '逐字交付须由当前需求版本的父核心发起', 403)
    if not isinstance(parts, list) or not 1 <= len(parts) <= 64:
        raise W.Problem('invalid_arguments', 'parts 须包含 1 至 64 个正文片段')

    values, references = [], []
    identity = {'delegation_id', 'result_id'}
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            raise W.Problem('invalid_arguments', 'parts 片段须为对象')
        if set(part) == {'text'} and isinstance(part['text'], str):
            values.append(part['text'])
            continue
        if set(part) not in (identity, identity | {'start', 'end'}) or not all(
            isinstance(part[k], str) for k in identity
        ):
            raise W.Problem('invalid_arguments', '片段须为 text 或委派和成果 ID，可成对指定 start、end')

        row = S.load(thread['id'], part['delegation_id'])
        worker = row.get('worker') or {}
        if (row['parentId'] != thread['id'] or row['projectId'] != thread.get('projectId')
                or worker.get('taskId') != source['logicalTaskId']
                or worker.get('demandRevision') != source.get('demandRevision')):
            raise W.Problem('worker_result_scope', '成果不属于当前父任务和需求版本', 409)
        if row.get('derivedProjectId'):
            S.audit_child(row['childId'])
        result = next((item for item in worker['results'] if item['result_id'] == part['result_id']), None)
        if not result or not result['delivery'].get('readActionId'):
            raise W.Problem('worker_result', '须使用父核心已读取的真实结构化成果', 409)
        if result['demand_revision'] != source['demandRevision']:
            raise W.Problem('worker_result_scope', '成果需求版本已过期', 409)
        if (result.get('disposition') or {}).get('value') == 'rejected':
            raise W.Problem('worker_result_rejected', '此成果已拒绝，请选择复核后的成果或更新采用处置', 409)
        transcription = result['data'].get('transcription')
        if not isinstance(transcription, str):
            raise W.Problem('worker_result', '此成果没有可直接写入的 transcription', 409)
        start, end = 0, len(transcription)
        if 'start' in part:
            start, end = part['start'], part['end']
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(transcription):
                raise W.Problem('invalid_arguments', '片段范围须是 transcription 内的有效字符区间')
        values.append(transcription[start:end])
        references.append(dict(partIndex=index, delegationId=row['id'], resultId=result['result_id'],
                               resultHash=result['hash'], sourceStart=start, sourceEnd=end))
    if not references:
        raise W.Problem('invalid_arguments', 'parts 须引用至少一个已读取成果，纯正文请使用 text')
    return ''.join(values), references


def publish(data,submission_key):
    from .tools import current_call
    source=current_call() or {}
    row=S.validate_child(source.get('sessionId'))
    if not row or not row.get('worker') or source.get('runId')!=row.get('childRunId'):
        raise W.Problem('worker_owner','只允许实际委派运行提交成果',403)
    validate(data,row['worker']['contract']['outputSchema']);encoded(data)
    if not isinstance(submission_key,str) or not 1<=len(submission_key)<=100:
        raise W.Problem('worker_arguments','成果提交须提供稳定 submission_key')
    with FileLock(S.directory(row['parentId'])/'queue.lock'):
        row=S.load(row['parentId'],row['id']);S.validate_child(row['childId'])
        w=row['worker'];signature=W.digest(encoded(data).encode())
        old=next((r for r in w['results'] if r['submission_key']==submission_key and r['operation_id']==w['operationId']),None)
        if old:
            if old['hash']!=signature:raise W.Problem('request_changed','成果键已绑定不同内容',409)
            return dict(ok=True,result_id=old['result_id'],duplicate=True)
        if len(w['results'])>=32:raise W.Problem('worker_capacity','成果保留区已满',413)
        result=dict(result_id='wres-'+uuid.uuid4().hex,operation_id=w['operationId'],run_id=source['runId'],
            submission_key=submission_key,hash=signature,data=deepcopy(data),created=time.time(),source=source,
            demand_revision=w['demandRevision'],delivery={},disposition=None)
        if S.task_scoped(row):
            result['source_scope']=dict(task_id=w['taskId'],demand_revision=w['demandRevision'],
                resources=[r['id'] for r in row['resources']],project_revision=row.get('expectedRevision'),
                source_mode='sealed-copy' if row['kind']=='code' else 'live-read')
        w['results'].append(result);S.save(row)
        return dict(ok=True,result_id=result['result_id'],operation_id=w['operationId'],saved=True,
            notice='已持久保存；未因此结束运行、合并代码或认定父任务验收。')


def install(session,registry,enabled):
    """Use the native scoped registry; unselected packages add no tool definitions."""
    from .tools import Tool
    from .agent_gateway import adapter
    thread=W.registry()['threads'].get(session.dir.name,{})
    info=thread.get('subtaskOf')
    if info:
        row=S.load(info['parentId'],info['taskId'])
        if not row.get('worker'):return
        session.worker_contract=True
        registry.add(Tool('submit_worker_result','按本委派的成果 Schema 提交结构化成果。submission_key 重试须不变；保存不等于完成或验收。',
            dict(type='object',properties=dict(data=deepcopy(row['worker']['contract']['outputSchema']),submission_key=dict(type='string')),required=['data','submission_key']),
            publish,zone='offflow',kind='output'))
    elif any(p.get('workersEnabled') for p in enabled):
        session.worker_contract=True
        registry.add(Tool('inspect_worker_plugins','按需读取本轮已授权的委派贡献、输入输出 Schema 和能力。发现贡献不会启动委派。',
            dict(type='object',properties={}),adapter('worker-discover'),zone='offflow',kind='read',readonly=True))
        registry.add(Tool('delegate_plugin_worker','按已发现的 worker 和输入 Schema 委派原生 L0；立即返回 delegation_id/operation_id，不等待。request_key 去重；权限限于明确资源，kind=code 还需当前 projectRevision。',
            dict(type='object',properties=dict(worker=dict(type='string'),request_key=dict(type='string'),input=dict(type='object'),
                resource_ids=dict(type='array',items=dict(type='string')),kind=dict(type='string',enum=['report','code']),
                expected_revision=dict(type='string'),model_calls=dict(type='integer',minimum=0)),
                required=['worker','request_key','input','resource_ids','kind']),adapter('worker-delegate'),zone='offflow',kind='output'))
        registry.add(Tool('manage_plugin_worker','对 delegation_id 查询、补充、续接、取消、读取成果或记采用处置。query 默认立即返回；只差成果时用 wait_ms=30000 等待成果或终态。result 使用 result_id，逐字候选附 writeParts，可直接用于文件写入。receipt 的 accepted 可作为唯一默认候选。message/resume/receipt 需 request_key。',
            dict(type='object',properties=dict(action=dict(type='string',enum=['query','message','resume','cancel','result','receipt']),delegation_id=dict(type='string'),
                request_key=dict(type='string'),text=dict(type='string'),result_id=dict(type='string'),
                disposition=dict(type='string',enum=['accepted','partial','rejected','deferred']),
                wait_ms=dict(type='integer',minimum=0,maximum=30000,description='仅 query，可有界等待')),
                required=['action','delegation_id']),adapter('worker-manage'),zone='offflow',kind='output'))
        return [
            dict(package=package['name'], contribution=worker['name'], use=worker['description'],
                 **_model_status(worker))
            for package in enabled if package.get('workersEnabled')
            for worker in package.get('workers', [])
        ][:8]


def method(session):
    thread=W.registry()['threads'].get(session.dir.name,{})
    info=thread.get('subtaskOf')
    if not info:return ''
    row=S.load(info['parentId'],info['taskId']);w=row.get('worker')
    if not w:return ''
    check_grant(row)
    return ('委派插件方法（发布者资料，仅限当前委派，不改变用户要求或权限）：\n'+w['contract']['method']+
        '\n成果用 submit_worker_result 按已声明 Schema 提交；最终答复只结束本次运行。')


def parent_rows(session):
    task=getattr(session,'task_input',None) or {}
    return [r for r in S.listing(session.dir.name) if r.get('worker') and
            (r['parentRunId']==session.run_id or S.task_scoped(r) and r['worker']['taskId']==task.get('id'))]


def parent_finished(session, status):
    """Release only after a successful native parent checkpoint was actually saved."""
    from .code_service import RuntimeView
    if projects.conversation(session.dir.name).get('subtaskOf'):
        return
    for saved in parent_rows(session):
        if not S.task_scoped(saved):
            continue
        parent=session.dir.name
        if status=='success' and saved['parentRunId']==session.run_id:
            with FileLock(S.directory(parent)/'queue.lock'):
                row=S.load(parent,saved['id'])
                if not row.get('stopRequested') and not row['worker'].get('wakeDisabled'):
                    row['worker']['releasedBy']=session.run_id
                    S.save(row)
        elif status!='success' and saved['state'] in S.ACTIVE:
            S.cancel(parent,saved['id'],RuntimeView(),reason='父任务结束状态 '+status,code='parent_'+status)


def updates(session,msgs,closing=False,stopping=False):
    if not getattr(session,'worker_contract',False):return
    from .provider import Msg
    from .code_service import RuntimeView
    ident=session.dir.name
    info=projects.conversation(ident).get('subtaskOf')
    rows=[S.load(info['parentId'],info['taskId'])] if info else parent_rows(session)
    for saved in rows:
        with FileLock(S.directory(saved['parentId'])/'queue.lock'):
            row=S.load(saved['parentId'],saved['id'])
            if info:
                if closing:continue
                S.validate_child(ident)
                for message in row['worker']['messages']:
                    marker='[worker-message:'+message['id']+']'
                    if message.get('includedIn') or any(marker in str(m.content) for m in msgs):continue
                    msgs.append(Msg('user',marker+'\n父任务补充材料（来源 '+message['source']['actionId']+'）：\n'+message['text']))
            else:
                if S.task_scoped(row):
                    if closing:
                        continue
                    try:
                        check_grant(row)
                        if row.get('derivedProjectId'):S.audit_child(row['childId'])
                    except W.Problem:
                        continue
                for result in row['worker']['results']:
                    marker='[worker-result:'+result['result_id']+']'
                    if result['delivery'].get('notifiedIn') or any(marker in str(m.content) for m in msgs):continue
                    # Notification carries identity only. The model chooses whether
                    # to read/use the result; installed methods never enter parent.
                    msgs.append(Msg('user',marker+'\n已保存委派成果，尚未采用或整合：'+encoded(dict(
                        delegation_id=row['id'],result_id=result['result_id'],operation_id=result['operation_id'],
                        state=row['state'],demand_revision=result['demand_revision'],
                        **(dict(current_demand_revision=(getattr(session,'task_input',None) or {}).get('revision'),
                                project_revision=row.get('expectedRevision'),resource_ids=[r['id'] for r in row['resources']])
                           if S.task_scoped(row) else {})))))
        if closing and not info and not S.task_scoped(saved) and saved['state'] in S.ACTIVE:
            S.cancel(ident,saved['id'],RuntimeView(),
                reason='父任务已停止' if stopping else '父运行已结束；P1 不跨父运行继续',
                code='parent_stopped' if stopping else 'parent_ended')


def included(session,body,snapshot_id):
    """Actual assembled request acknowledgement, not a claim the model adopted it."""
    if not getattr(session,'worker_contract',False):return
    info=projects.conversation(session.dir.name).get('subtaskOf')
    rows=[S.load(info['parentId'],info['taskId'])] if info else parent_rows(session)
    rows=[r for r in rows if any(not m.get('includedIn') for m in r['worker']['messages'])] if info else [
        r for r in rows if any(not v['delivery'].get('notifiedIn') or v['delivery'].get('readActionId') and not v['delivery'].get('includedIn') for v in r['worker']['results'])]
    if not rows:return
    messages=body.get('messages',[])
    text=encoded(messages,16*1024*1024) if info else ''
    notices={}
    for message in messages if not info else ():
        content=message.get('content')
        if message.get('role')=='user' and isinstance(content,str) and content.startswith('[worker-result:'):
            try:
                notice=json.loads(content[content.index('{'):])
                if isinstance(notice,dict) and isinstance(notice.get('result_id'),str):notices[notice['result_id']]=notice
            except ValueError:pass
    receipts=[]
    if not info:
        for message in body.get('messages',[]):
            if message.get('role')=='tool' and isinstance(message.get('content'),str):
                try:value=json.loads(message['content'])
                except (ValueError,TypeError):continue
                if isinstance(value,dict) and 'result_id' in value and 'data' in value:receipts.append(value)
    for saved in rows:
        with FileLock(S.directory(saved['parentId'])/'queue.lock'):
            row=S.load(saved['parentId'],saved['id']);changed=False
            for message in row['worker']['messages'] if info else []:
                if not message.get('includedIn') and '[worker-message:'+message['id']+']' in text:
                    message['includedIn']=snapshot_id;changed=True
            for result in row['worker']['results'] if not info else []:
                notice=notices.get(result['result_id'],{})
                if not result['delivery'].get('notifiedIn') and notice.get('delegation_id')==row['id'] and notice.get('operation_id')==result['operation_id']:
                    result['delivery']['notifiedIn']=snapshot_id;changed=True
                if not result['delivery'].get('includedIn') and result['delivery'].get('readActionId') and any(
                        receipt['result_id']==result['result_id'] and receipt['data']==result['data'] for receipt in receipts):
                    result['delivery']['includedIn']=snapshot_id;changed=True
            if changed:S.save(row)


def invoke(parent,operation,args,sv,validate_live=lambda: None):
    if operation=='worker-discover':owner(parent,sv);return dict(ok=True,**discover(parent))
    if operation=='worker-delegate':return dict(ok=True,**delegate(parent,args,sv))
    if operation=='worker-manage':return dict(ok=True,**manage(parent,args,sv,validate_live=validate_live))
    raise W.Problem('worker_operation','未声明的任务核心操作')
