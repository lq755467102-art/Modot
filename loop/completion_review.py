"""One persisted review opportunity at the native executor's final reply."""
# 主要引用方包括 `provider.py` 和 `loop.py`。
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy
import json
from math import ceil
import os
from pathlib import Path
import re
import time
import uuid

from .filelock import FileLock, atomic_json


MAX_REQUESTS = 2
MAX_CHECKS = 1
MAX_SECONDS = 60
_attempt = ContextVar('completion_review_attempt', default=None)


class ReviewLimit(RuntimeError):
    pass


def valid_run_id(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) is not None


def record_path(directory, review_id):
    if not valid_run_id(review_id):
        raise ValueError('invalid completion review id')
    return Path(directory) / 'completion-reviews' / (review_id + '.json')


def remaining_time(context):
    deadline = context.get('completionReviewDeadline')
    return max(0, deadline - time.monotonic()) if type(deadline) in (int, float) else None


def before_request():
    """Charge retries and nested requests at the actual provider boundary."""
    attempt = _attempt.get()
    if attempt is None:
        return
    review, first = attempt
    review.require_time()
    if first:
        _attempt.set((review, False))
    else:
        review.reserve('modelRequests', review.state['requestLimit'])


def actionable(checks, acceptance):
    from .evidence_redaction import redact

    requirements = acceptance.get('requirements', [])
    result = []
    for resource in (checks or {}).get('resources', []):
        revision = resource.get('revision')
        if not revision or resource.get('status') == 'unknown':
            continue
        gaps = {item['operationId']: item for item in resource.get('pending', [])
                if item.get('operationId') and item.get('revision') == revision}
        required = [dict(id=item['id'], text=redact(str(item['text']))[:240])
                    for item in requirements if item['status'] == 'failed'
                    and (item.get('target') or {}).get('id') == resource['resourceId']
                    and (item.get('target') or {}).get('revision') == revision]
        for check in resource.get('checks', []):
            if (check.get('status') != 'failed' or check.get('purpose') == 'exploration'
                    or check.get('environmentBlocker') or check.get('replaced')):
                continue
            gap = gaps.get(check['operationId'], {})
            diagnostics = gap.get('diagnostics') or {}
            first = (diagnostics.get('firstFailure') or {}).get('text')
            if not first:
                tail = diagnostics.get('logTail') or []
                first = '\n'.join(str(line.get('text', '')) for line in tail[-3:])
            failure = dict(
                operationId=check['operationId'], actionId=check.get('actionId'),
                resourceId=resource['resourceId'], revision=revision, purpose=check.get('purpose'),
                failure=redact(str(first or gap.get('text') or '交付检查失败，请查阅对应回执'))[:480],
                requirements=required)
            cases = check.get('assertionFailures') or []
            if cases:
                failure['testCases'] = [redact(case['selector']) for case in cases[:4]]
                if not first or len(cases)!=len([case for case in (diagnostics.get('testReport') or {}).get('cases', [])
                                                if case['state']=='failed']):
                    failure['failure'] = redact(cases[0]['text'])[:480]
                offset = cases[0].get('after')
                if type(offset) is int and offset>=0:
                    source = dict(process_id=check['processId']) if check.get('processId') else dict(operation_id=check['operationId'])
                    failure['readOutput'] = dict(tool='read_check_output',resource_id=resource['resourceId'],after=offset,**source)
            result.append(failure)
    return result[:4]


# 每个运行最多一次有界复核。
# `path` 指向持久记录，`state` 保存失败事实和已耗额度，`deadline` 是按落盘到期时间恢复的本地定时期限。
class CompletionReview:
    @classmethod
    def enabled(cls, session, run_id):
        if session is None or os.environ.get('HARNESS_L0_COMPLETION_REVIEW') != '1':
            return None
        fresh = cls(session, run_id)
        task = getattr(session, 'task_input', None) or {}
        if fresh.state is not None or task.get('kind') != 'resume' or task.get('previousStatus') != 'running':
            return fresh
        checkpoint = session.load()
        # A forced exit changes the runner identity, but the logical task,
        # accepted operations, already charged budget and publication survive.
        for review_id in (task.get('previousRunId'), checkpoint.get('completionReviewId')):
            if not valid_run_id(review_id) or review_id == run_id:
                continue
            prior = cls(session, run_id, review_id=review_id)
            if not prior.state:
                continue
            with FileLock(prior.path.with_suffix('.lock')):
                state = prior.read()
                if not state or not (state['status'] == 'started' or state.get('publication')):
                    continue
                if state['draft']['basis']['task'] != [task.get('id'), task.get('revision')]:
                    continue
                if state.get('ownerRunId') != run_id:
                    state['ownerRunId'] = run_id
                    atomic_json(prior.path, state)
                prior.state = state
                return prior
        return fresh

    def __init__(self, session, run_id, *, review_id=None):
        self.session = session
        self.run_id = run_id
        self.review_id = review_id if review_id is not None else run_id
        self.preparing_resources = set()
        self.path = record_path(session.dir, self.review_id)
        self.state = self.read()
        self.deadline = (None if self.state is None else time.monotonic() +
                         max(0, min(MAX_SECONDS, self.state['expiresAt'] - time.time())))

    def read(self):
        try:
            return json.loads(self.path.read_text(encoding='utf8'))
        except FileNotFoundError:
            return None

    @property
    def active(self):
        return bool(self.state and self.state['status'] == 'started')

    def time_left(self):
        return self.deadline - time.monotonic()

    def timed_out(self):
        return self.active and self.time_left() <= 0

    def require_time(self):
        if self.timed_out():
            raise ReviewLimit('deadline')

    def start(self, failures, turns_left, *, draft, checks, acceptance, task):
        from .control import benchmark_deadline
        from .evidence_redaction import redact
        from .parallel_budget import remaining

        if self.state is not None:
            return 'already_used'
        if not failures:
            return 'no_actionable_failure'
        # `parallel_budget.py` 与 `control.py` 给出主运行剩余额度和截止时间。
        # 复核只使用其剩余范围。
        target = getattr(self.session, 'execution_budget', None)
        requests = min(MAX_REQUESTS, turns_left, remaining(target) if target else MAX_REQUESTS)
        now = time.monotonic()
        cutoff = benchmark_deadline(self.session.dir, 'executor')
        seconds = min(MAX_SECONDS, cutoff - now) if cutoff is not None else MAX_SECONDS
        if requests <= 0 or seconds <= 0:
            return 'run_budget'
        with FileLock(self.path.with_suffix('.lock')):
            if self.read() is not None:
                return 'already_used'
            started_at = time.time()
            self.state = dict(status='started', ownerRunId=self.run_id, startedAt=started_at, expiresAt=started_at + seconds,
                              requestLimit=requests, modelRequests=0, checkRequests=0, failures=failures,
                              draft=dict(text=draft, basis=delivery_basis(checks, acceptance, task)),
                              passed=[redact(dict(resourceId=row['resourceId'], revision=row['revision'],
                                           operationId=item['operationId'], actionId=item.get('actionId'),
                                           argv=item.get('argv'), assessment=item.get('assessment'),
                                           testReportState=item.get('testReportState')))
                                      for row in (checks or {}).get('resources', [])
                                      for item in row.get('checks', []) if item.get('reviewReusable')][:8])
            atomic_json(self.path, self.state)
        self.deadline = now + seconds
        return 'started'

    def reserve(self, key, limit, *, reference=None):
        self.require_time()
        with FileLock(self.path.with_suffix('.lock'), check=self.require_time):
            state = self.read()
            if state['status'] != 'started' or state.get('ownerRunId', self.review_id) != self.run_id or state[key] >= limit:
                raise ReviewLimit(key)
            state[key] += 1
            if reference:
                state.setdefault('checkOperations', {})[reference['operationId']] = reference
            atomic_json(self.path, state)
            self.state = state

    @contextmanager
    def model_call(self):
        self.reserve('modelRequests', self.state['requestLimit'])
        token = _attempt.set((self, True))
        try:
            yield
        finally:
            _attempt.reset(token)

    def start_check(self, arguments, reference=None):
        self.reserve('checkRequests', MAX_CHECKS, reference=reference)
        if reference:
            # The host matches the requested inputs and caps the actual run.
            return
        timeout = arguments.get('timeout', 30)
        if type(timeout) is int and 1 <= timeout <= 120:
            arguments['timeout'] = min(timeout, max(1, ceil(self.time_left())))

    def follow_check(self, resource_id, receipt, run_id):
        with FileLock(self.path.with_suffix('.lock'), check=self.require_time):
            state = self.read()
            if state['status'] != 'started' or state.get('ownerRunId', self.review_id) != self.run_id:
                raise ReviewLimit('checkRequests')
            oid = receipt['operationId']
            state.setdefault('checkOperations', {}).setdefault(oid, dict(
                operationId=oid, resourceId=resource_id, runId=run_id))
            atomic_json(self.path, state)
            self.state = state
        return receipt

    def run_check(self, tool, root, arguments):
        from . import agent_gateway, code_service, workbench
        from .check_sessions import operation_identity
        from .tools import current_call, reset_call, set_call

        def invoke(resource_id=None, fresh=False, **args):
            if type(fresh) is not bool:
                return dict(ok=False, code='invalid_arguments', error='fresh 须为布尔值')
            owned = [ref for ref in self.state.get('checkOperations', {}).values()
                     if ref['resourceId'] == resource_id]
            if (not fresh and isinstance(resource_id, str) and (resource_id in self.preparing_resources or owned)
                    and args.get('environment_id') is None and not args.get('capture_changes')
                    and os.environ.get('HARNESS_AGENT_URL')):
                # Check the live owner first. If it has already finished, the
                # durable lookup below sees its result without a second launch.
                token = set_call(dict(current_call(), actionId=uuid.uuid4().hex, completionReviewId=self.review_id))
                try:
                    receipt = agent_gateway.call('check-join', resource_id=resource_id, **args)
                finally:
                    reset_call(token)
                if receipt.get('reused') and valid_run_id(receipt.get('operationId')):
                    return self.follow_check(resource_id, receipt, self.run_id)
            prior = None
            if not fresh and isinstance(resource_id, str):
                try:
                    prior = code_service.review_check_receipt(root, resource_id, args, prior_operations=tuple(
                        ref['operationId'] for ref in owned if ref.get('runId', self.review_id) != self.run_id))
                except (OSError, ValueError, workbench.Problem):
                    pass  # The normal check path owns validation and any resulting error.
            if prior:
                op, receipt = prior
                if receipt is not None:
                    return receipt
                receipt = dict(ok=True, reused=True, executedThisCall=False, background=True,
                               operationId=op['id'], evidenceActionId=op['source']['actionId'],
                               resourceId=resource_id, execution=dict(state='running', exitCode=None),
                               summary='同条件检查已在途，请沿原操作等终态。')
                return self.follow_check(resource_id, receipt, op['source']['runId'])

            reference = None
            if os.environ.get('HARNESS_AGENT_URL') and isinstance(resource_id, str) and not args.get('capture_changes'):
                reference = dict(operationId=operation_identity(current_call()), resourceId=resource_id, runId=self.run_id)
            self.start_check(args, reference)
            result = tool.fn(resource_id=resource_id, **args)
            if (reference and isinstance(result, dict) and result.get('ok') is False
                    and result.get('code') not in (None, 'unknown') and not result.get('operationId')):
                # The host refused the command before accepting an operation.
                # An uncertain response keeps the original reference for recovery.
                with FileLock(self.path.with_suffix('.lock')):
                    state = self.read()
                    state['checkOperations'].pop(reference['operationId'], None)
                    atomic_json(self.path, state)
                    self.state = state
            return result

        checked = copy(tool)
        checked.fn = invoke
        return checked(arguments)

    def schemas(self, schemas):
        return [dict(row, parameters=dict(row['parameters'], properties=dict(row['parameters']['properties'],
                    fresh=dict(type='boolean', description='仅用户明确要求重新实跑时传 true。'
                               '缺少适用证据或针对不同用例时，正常按原参数检查。'))))
                if row['name'] == 'run_project_check' else row for row in schemas]

    def event_fields(self):
        return dict(reviewId=self.review_id, **{key: value for key, value in self.state.items()
                    if key not in {'draft', 'publication', 'ownerRunId'}})

    def prior_run_ids(self):
        references = (self.state or {}).get('checkOperations', {}).values()
        owners = {reference.get('runId', self.review_id) for reference in references}
        return (owners | {self.review_id}) - {self.run_id}

    def wait_for_checks(self, task):
        from . import agent_gateway
        from .tools import reset_call, set_call

        checks = list(self.state.get('checkOperations', {}).values())
        if not checks:
            return None
        # The host stops waiting early enough to return before the caller's
        # original deadline. Each read has a new RPC identity, not a new check.
        if self.time_left() <= 0:
            return dict(reason='deadline', checks=checks)
        token = set_call(dict(sessionId=Path(self.session.dir).name, runId=self.run_id, actionId=uuid.uuid4().hex,
                              role='executor', tool='__completion_review_wait__', nativeExecutor=True,
                              logicalTaskId=(task or {}).get('id'), demandRevision=(task or {}).get('revision'),
                              completionReviewId=self.review_id, completionReviewDeadline=self.deadline))
        try:
            result = agent_gateway.call('check-settle', checks=checks)
        finally:
            reset_call(token)
        if result.get('reason') in {'settled', 'stopped', 'deadline', 'unconfirmed'}:
            return result
        return dict(reason='deadline' if self.time_left() <= 0 else 'unconfirmed', checks=checks)

    def contradicted(self, settlement, messages):
        """A negative result unseen by the model can invalidate its proposed answer."""
        uncertain = {item['operationId'] for item in settlement['checks'] if item.get('status') != 'passed'}
        if not uncertain:
            return False
        for message in messages:
            if message.role != 'tool' or not isinstance(message.content, str) or 'operationId' not in message.content:
                continue
            try:
                receipt = json.loads(message.content)
            except ValueError:
                continue
            if (isinstance(receipt, dict) and receipt.get('operationId') in uncertain
                    and (receipt.get('execution') or {}).get('state') in {'completed', 'failed', 'cancelled', 'timed_out'}):
                uncertain.discard(receipt['operationId'])
        return bool(uncertain)

    def publish(self, text, reason, emit, *, refresh_unsent=False):
        """Retain the exact reply across the event-append/session-save crash window."""
        with FileLock(self.path.with_suffix('.lock')):
            state = self.read()
            publication = state.get('publication')
            if publication is None:
                publication = state['publication'] = dict(text=text, reason=reason)
                atomic_json(self.path, state)
                prior = None
            elif publication.get('eventSeq') is not None:
                self.state = state
                return publication['text']
            else:
                prior = next((event for event in reversed(self.session.events())
                              if event.get('completionReview') == self.review_id and event.get('roleSummary')), None)
                if prior is None and refresh_unsent:
                    publication['text'] = text
                    atomic_json(self.path, state)
            event = prior or emit(publication['text'])
            publication['eventSeq'] = event['seq']
            atomic_json(self.path, state)
            self.state = state
        return publication['text']

    def feedback(self, messages):
        from .working_context import append_update

        append_update(messages, 'completion-review',
            '提交前请复核下面仍有效的交付检查。针对原失败的同一行为，修正并定向检查一次，'
            '或给出原前提无效的依据。另一项绿灯只证明它检查过的行为。'
            '优先使用现有回执，复核最多两次模型请求、一次检查。'
            '下面列出当前通过回执。只需引用时直接引用。用户明确要求实跑才传 fresh=true。'
            '到限时列明仍失败、受阻或缺少当前版本证据的内容，按需求验收实际状态总结。\n'
            + json.dumps(dict(failures=self.state['failures'], passed=self.state.get('passed', [])), ensure_ascii=False))

    def finish(self, reason, ended_at):
        with FileLock(self.path.with_suffix('.lock')):
            state = self.read()
            if state['status'] == 'started':
                state.update(status='finished', reason=reason,
                             elapsedMs=max(0, round((ended_at - state['startedAt']) * 1000)))
                atomic_json(self.path, state)
            self.state = state
        return self.event_fields()

    def draft_text(self, verification, task):
        draft = self.state.get('draft') or {}
        if not draft:
            return ''
        current = delivery_basis((verification or {}).get('codeChecks'), (verification or {}).get('acceptance'), task)
        before = draft['basis']
        if (current['known'] and current['task'] == before['task'] and current['versions'] == before['versions']
                and (current['acceptanceOk'] or not before.get('acceptanceOk', True))
                and set(current['failures']) <= set(before['failures'])
                and set(current['unmet']) <= set(before['unmet'])):
            return draft['text']
        return ''

    def candidate_unverified(self, verification, task):
        checks = (verification or {}).get('codeChecks') or {}
        current = delivery_basis(checks, (verification or {}).get('acceptance'), task)
        before = self.state['draft']['basis']
        return (current['task'] != before['task']
                or checks.get('status') != 'checked'
                and (not current['known'] or current['versions'] != before['versions']))


def delivery_basis(checks, acceptance, task):
    resources = (checks or {}).get('resources', [])
    return dict(task=[(task or {}).get('id'), (task or {}).get('revision')],
                versions={res['resourceId']: res.get('revision') for res in resources},
                known=bool(resources) and all(res.get('revision') and res.get('status') != 'unknown' for res in resources),
                acceptanceOk=(acceptance or {}).get('ok', True),
                failures=[item['operationId'] for row in resources for item in row.get('checks', [])
                          if item.get('purpose') != 'exploration' and not item.get('replaced')
                          and item['status'] not in {'passed', 'running'}],
                unmet=[item['id'] for item in (acceptance or {}).get('requirements', [])
                       if item['status'] not in {'passed', 'confirmed'}])


def closing_text(verification, settlement=None, *, reviewed=True):
    from .evidence_redaction import redact

    verification = verification or {}
    code_checks = verification.get('codeChecks') or {}
    text = '本轮收尾复核已结束。' if reviewed else '本轮执行已结束。'
    gaps = [item for row in code_checks.get('resources', []) for item in row.get('pending', [])]
    if gaps:
        labels = [redact(item.get('text') or '检查待核对')[:160] for item in gaps[:3]]
        text += ' 当前版本需核对的检查：' + '；'.join(labels) + '。'
    elif code_checks.get('status') == 'checked':
        text += ' 已记录的当前版本检查通过。'
    else:
        text += ' 当前版本的检查证据仍待核对。'
    acceptance = verification.get('acceptance') or {}
    if acceptance.get('ok') is False:
        requirements = acceptance.get('requirements', [])
        failed = [redact(row['text'])[:120] for row in requirements if row['status'] == 'failed']
        missing = [redact(row['text'])[:120] for row in requirements
                   if row['status'] not in {'passed', 'confirmed', 'failed'}]
        if failed:
            text += ' 必验项未通过：' + '；'.join(failed[:5]) + '。'
        if missing or not failed:
            text += ' 必验项尚无通过证据' + ('：' + '；'.join(missing[:5]) if missing else '') + '。'
    if settlement and settlement['reason'] == 'unconfirmed':
        text += ' 已提交检查的终态仍待确认。'
    if settlement and settlement['reason'] == 'deadline':
        text += ' 到达复核时限时，已提交检查的终态仍待确认。'
    return text + ' 需求验收以本轮实际回执为准。'
