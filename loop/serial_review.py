"""Post-execution read-only observations; never a delivery gate or repair worker."""
# 主要引用方包括 `role_coordination.py` 和 `execution_completion.py`。
import json
import time
from pathlib import Path

from . import code_service as C, scoped_roles
from .provider import Msg
from .tools import Registry


def overview(state):
    outcome = state.get('executorOutcome', {})
    ended = outcome.get('status') == 'success'
    checks = state.get('executorChecks') or {}
    return dict(status='completed' if ended else state.get('state', 'running'),
                label=('本轮执行已结束 · ' + ('已有检查通过' if checks.get('status') == 'checked' else '检查未通过或未完成')) if ended else '进行中或已中断',
                completed=['执行已结束'] if ended else [], pending=[],
                verification=checks.get('status', 'unverified'),
                review=state.get('serialReview', {}).get('state', 'not_run'))


def install_executor(owner, registry):
    # The former declaration is not needed to stop the model loop.
    registry._tools.pop('report_execution_status', None)
    from .working_context import install
    install(owner.session, 'executor', registry)


def feedback(owner, messages):
    delivery = owner.investigation.feedback(messages) if owner.investigation else {}
    # Existing troubleshooting/scout workflow is retained, without review dispatch.
    from .executor_workflow import feedback as progress_feedback, dispatch
    dispatch(owner)
    progress_feedback(owner, messages)
    for file in sorted(owner.path.parent.glob('*.json')):
        if file == owner.path:
            continue
        state = json.loads(file.read_text(encoding='utf8'))
        report = state.get('serialReview', {})
        if not report.get('text'):
            continue
        try:
            for rid in state.get('dirty', {}):
                owner._resource(rid)
        except C.W.Problem:
            continue
        if not state.get('dirty'):
            continue
        marker = '[previous-readonly-review:' + state['runId'] + ']'
        if not any(marker in str(m.content) for m in messages):
            messages.append(Msg('user', marker + ' 历史只读审查意见，仅供核对当前任务，'
                '先判断是否与本轮相关；不相关可忽略。不代表当前版本失败或通过，不会自动派发修复。'
                '完整意见按sourceRunId用read_conversation_dialogue回取。\n'
                + json.dumps(dict(sourceRunId=state['runId'], excerpt=report['text'][:1200]), ensure_ascii=False)))
    return delivery


def review_stopped(owner):
    # Read-only observations cannot consume the controller's stop/receipt window.
    cutoff = getattr(owner, 'work_deadline', None)
    return owner._review_stop() or cutoff is not None and time.monotonic() >= cutoff - 10


def finish(owner, result):
    owner.planning_closed.set()
    owner.planning.shutdown(wait=True)
    owner.queue.shutdown(wait=True)
    owner.unit_workers.shutdown(wait=True)
    owner.state.update(state=result['status'], finished=time.time())
    try:
        if not owner.state.get('executorChecks'):
            owner.state['executorChecks'] = C.executor_verification(
                Path(owner.session.dir).name, owner.session.run_id) or dict(status='unverified')
    except (OSError, ValueError, C.W.Problem) as error:
        owner.state['executorChecks'] = dict(status='unknown', reason=str(error)[:400])
    owner.save()
    owner.session.emit('execution_settled', status=result['status'],
                       verification=owner.state['executorChecks']['status'])
    if (result['status'] != 'success' or owner.stop() or review_stopped(owner)
            or not owner.background_reviewer or not owner.state['dirty']):
        return
    # Persist before entering the worker; replayed finish must not repeat a model call.
    if owner.state.get('serialReview'):
        return
    report = owner.state['serialReview'] = dict(state='running', started=time.time(),
                                              sourceRun=owner.session.run_id)
    owner.save()
    try:
        with C.database() as db:
            operations = [json.loads(raw) for raw, in db.execute(
                "SELECT data FROM operations WHERE thread=? AND json_extract(data,'$.source.runId')=? ORDER BY seq",
                (Path(owner.session.dir).name, owner.session.run_id))]
        changes = [dict(operationId=o['id'], actionId=o.get('source', {}).get('actionId'),
                        resourceId=o.get('resourceId'), target=o.get('target'),
                        result=o.get('result')) for o in operations if o['operation'] in {'write', 'restore'}]
        if not changes:
            report.update(state="skipped", reason="本轮无代码改动")
            return
        reg = Registry()
        source = scoped_roles.registry(owner.root, role='reviewer')
        for name in ('inspect_code', 'search_project_code', 'read_check_output',
                     'read_action_evidence', 'read_project_activity', 'read_conversation_dialogue'):
            tool = source.get(name)
            if tool and tool.readonly:
                reg.add(tool)
        from .tools import default_registry
        reg.add(default_registry(owner.root).get('read_code_history'))
        text = scoped_roles.run(owner.root, owner._role_session(), 'reviewer',
            json.dumps(dict(task=owner.task_brief(), changes=changes,
                            checks=owner.state['executorChecks']), ensure_ascii=False),
            tools=reg, should_stop=lambda: review_stopped(owner), readonly_review=True,
            binding_fields=dict(sourceRunId=owner.session.run_id))
        report.update(state='interrupted' if review_stopped(owner) else 'completed', text=text)
        owner.session.emit('role_phase', role='reviewer', state=report['state'], independentTest=False)
        owner.session.emit('benni', ok=True, body=text, reviewVerdict='readonly',
                           independentTest=False, sourceRunId=owner.session.run_id)
    except Exception as error:
        report.update(state='interrupted' if review_stopped(owner) else 'failed', error=str(error)[:800])
        owner.session.emit('role_phase', role='reviewer', state=report['state'], reason=report['error'])
    finally:
        report['finished'] = time.time()
        owner.review_closed.set()
        owner.save()
