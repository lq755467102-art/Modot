"""L0 model working view; durable messages and action evidence stay untouched."""
# 主要引用方包括 `context_manager.py` 和 `context_surface.py`。
from copy import deepcopy
import json
from pathlib import Path
import re

from .prompt_fragments import PromptFragments
from .context_registry import request_scope
from .context_surface import ContextSurface
from .context_manager import HistoryCapacityBlocked, RequestBody, fingerprint, runtime_update_name, validate_pairs
from .filelock import atomic_json, read_text
from . import history_compaction as H
from . import input_budget

LEGACY_SUMMARY_INSTRUCTION = '''Extract an effective engineering checkpoint from the accepted conversation above.
Use these headings, concise bullets, and (none) for empty sections:
## Confirmed implementation decisions and reasons
## Public interfaces and compatibility constraints
## Actual checks, failures and environment blockers
## Current work and next action
User messages are retained verbatim; summarize work only.
Use the actual tool evidence to distinguish what was read, changed, checked and merely proposed.
Keep exact identifiers, paths, commands, function signatures, return conventions and key code snippets
when needed to resume correctly; do not replace precise interfaces with vague descriptions.
Record implementation choices and repair reasons as the model's historical choices, distinct from
user requirements. Record actual failures and their versions, environment blockers and unresolved questions.
Do not decide whether the task is complete or create new obligations. Attribute completion claims and
next actions to their original speaker and stage; they are historical statements, not current instructions.
Separate facts, hypotheses and unverified completion claims.
A failed exploratory probe is not a new user requirement. Do not repeat progress announcements,
superseded plans, full retry scripts, dependency inventories or repeated logs without useful conclusions.
Merge an earlier checkpoint, discard obsolete conclusions and keep unresolved real failures.
This checkpoint is background, not permission or proof of acceptance. Do not execute tools or claim
unverified work has passed. Later user messages take precedence over historical interpretations.
Output only the checkpoint.'''
SUMMARY_INSTRUCTION = H.SUMMARY_INSTRUCTION


def work_text(text):
    """Exclude generated intent from the model view, including legacy checkpoints."""
    lines, excluded, fence = [], False, None
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line.rstrip('\r\n'))
        if marker:
            token, rest = marker.groups()
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not rest.strip():
                fence = None
        elif fence is None:
            heading = re.match(r'^ {0,3}##\s+(.+?)\s*#*\s*$', line.rstrip('\r\n'))
            if heading:
                excluded = heading[1] == 'User intent and corrections'
        if not excluded:
            lines.append(line)
    return ''.join(lines).strip()


def summary_failure(session, role, reply, text, reasons, missing):
    """Keep public failure evidence locally; it is not another model message."""
    import uuid
    from .context_manager import MAX_CHECKPOINT_BYTES, MAX_STORED_BYTES
    from .evidence_redaction import redact
    from .filelock import FileLock
    raw = getattr(reply, 'text', '') or ''
    event = dict(role=role, reasons=reasons, missingSections=missing, textChars=len(raw),
                 workChars=len(text), toolCallCount=len(getattr(reply,'tool_calls',None) or []),
                 finishReason=getattr(reply,'finish_reason','') or '')
    evidence = dict(runId=session.run_id, **event, response=redact(dict(
        text=raw[:24000], textTruncated=len(raw)>24000,
        toolCalls=getattr(reply,'tool_calls',None) or [])))
    try:
        directory = Path(session.dir) / 'context-checkpoints'
        directory.mkdir(exist_ok=True)
        if not directory.resolve().is_relative_to(Path(session.dir).resolve()):
            raise ValueError('evidence path outside session')
        size = len(json.dumps(evidence,ensure_ascii=False).encode('utf8'))
        with FileLock(directory/'prepare.lock'):
            if size>MAX_CHECKPOINT_BYTES or size+sum(p.stat().st_size for p in directory.glob('*.json'))>MAX_STORED_BYTES:
                raise ValueError('existing checkpoint capacity exhausted')
            path = directory / ('summary-failure-'+uuid.uuid4().hex[:16]+'.json')
            atomic_json(path,evidence)
        event['evidence'] = path.relative_to(session.dir).as_posix()
    except (OSError,ValueError) as error:
        event['evidenceError'] = type(error).__name__
    session.emit('context_summary_failed', **event)


def enabled(session, role):
    return role == 'executor' and getattr(session, 'executor_core', None) is not None


def _scopes(session):
    from . import projects, workbench as W
    try:
        live = projects.scope(Path(session.dir).name)
    except W.Problem:
        return None, None, None
    grants = sorted((r['id'],r.get('path'),r.get('permission'),r.get('write'),r.get('execute'))
                    for r in live['resources'])
    captured = getattr(session,'context_scope',{}) or {}
    history = fingerprint([Path(session.dir).name, live.get('projectId'), grants, captured.get('plugins',[])])
    return fingerprint([getattr(session,'task_input',{}).get('id'), grants]), history, grants


def scope_key(session):
    return _scopes(session)[0]


def summary_path(session):
    return Path(session.dir) / 'context-work-summary.json'


def _state(session):
    """Only a committed, role-local view belonging to the live task can apply."""
    try:
        row = json.loads(read_text(summary_path(session)))
        if row.get('scope') is not None and row['scope'] == scope_key(session):
            return row
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {}


def prefix_version(body, cutoff):
    return fingerprint([m for i,m in enumerate(body['messages'][:cutoff])
        if m.get('role') != 'system' and runtime_update_name(m,body.parts.get(i,())) is None])


def committed_record(session, ident):
    """Read a local immutable checkpoint, shared by scope and usage verification."""
    if not isinstance(ident,str) or not re.fullmatch('[0-9a-f]{32}',ident):
        return {}
    from .context_manager import MAX_CHECKPOINT_BYTES
    path = Path(session.dir)/'context-checkpoints'/('cp-'+ident+'.json')
    if path.is_symlink() or not path.resolve().is_relative_to(Path(session.dir).resolve()) or path.stat().st_size > MAX_CHECKPOINT_BYTES:
        return {}
    record = json.loads(read_text(path))
    digest = record.pop('recordVersion',None)
    return record if record.get('id') == ident and fingerprint(record) == digest else {}


def _same_history_scope(session, row, history_scope, grants):
    if row.get('summaryFormat') != H.FORMAT or not row.get('archive') or history_scope is None:
        return False
    if 'historyScope' in row:
        return row['historyScope'] == history_scope
    # Previously committed checkpoints bind resources to the original task ID.
    key = (fingerprint(row),history_scope)
    if getattr(session,'_verified_history_scope',None) == key:
        return True
    record = committed_record(session,row.get('checkpointId'))
    old = record.get('resourceScope',{})
    current = getattr(session,'context_scope',{}) or {}
    if (record.get('role') != 'executor' or record.get('workSummary') != row
            or old.get('projectId') != current.get('projectId') or old.get('plugins',[]) != current.get('plugins',[])
            or fingerprint([record.get('runId'),grants]) != row.get('scope')):
        return False
    session._verified_history_scope = key
    return True


def checkpoint(session, source):
    try:
        row = json.loads(read_text(summary_path(session)))
        cutoff = row['sourceCount']
        if type(row.get('historyCount')) is int:
            meaningful = [i for i,m in enumerate(source['messages'])
                if m.get('role')!='system' and runtime_update_name(m,source.parts.get(i,())) is None]
            count=row['historyCount']
            cutoff = meaningful[count-1]+1 if 0<count<=len(meaningful) else 0 if count==0 else -1
        if (type(cutoff) is int and 0 <= cutoff <= len(source['messages']) and row.get('scope') is not None
                and row['sourceVersion'] == prefix_version(source, cutoff)):
            task, history, grants = _scopes(session)
            if row['scope'] == task:
                return dict(row,sourceCount=cutoff)
            if 'messageStart' not in getattr(session,'task_input',{}) and _same_history_scope(session,row,history,grants):
                return dict(row,sourceCount=cutoff,scope=task,historyScope=history,runtime={},toolResults={})
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def project(session, role, body, *, work_summary=None):
    if not enabled(session, role):
        return body
    if any(kind=='contextView' for spans in body.parts.values() for kind,_ in spans):
        return body
    validate_pairs(body['messages'])
    source = body
    session._context_view_source = source
    saved = work_summary if work_summary is not None else checkpoint(session, source)
    from .history_groups import group_key, index, reference
    retired_groups = work_summary.get('retiredGroups', []) if work_summary is not None else index(session)
    retired_keys = {group['key'] for group in retired_groups}
    if work_summary is None:
        session._history_usage_checkpoint = (saved or {}).get('checkpointId')
    retired = saved['sourceCount'] if saved else 0
    submission = input_budget.submission_index(source)
    state = work_summary if work_summary is not None else _state(session)
    replacements = state.get('toolResults', {})
    scope = request_scope(session, role)
    surface = ContextSurface(source, scope=scope, empty=True)
    fragments = PromptFragments(scope=scope)
    if saved and saved.get('text'):
        text = (H.frame(saved['text'],saved.get('archive')) if saved.get('summaryFormat') == H.FORMAT else
                '工作结论（历史资料，不是授权或验收证明）：\n'+work_text(saved['text']))
        fragments.add('work-checkpoint', text, 20)
    if saved:
        for order,(name,text) in enumerate(saved.get('runtime', {}).items(),21):
            if name != 'project-notice':
                fragments.add('checkpoint-state:'+name,text,order)
    if entry := reference(session, retired_groups):
        fragments.add('retired-history', entry, 19)
    i = 0
    while i < len(source['messages']):
        message = source['messages'][i]
        annotations = source.parts.get(i, ())
        name = runtime_update_name(message, annotations)
        if name not in (None, 'project-notice') and i < retired and saved is not None and 'runtime' in saved:
            i += 1
            continue
        calls = message.get('tool_calls') or []
        if calls:
            end = i + 1 + len(calls)
            # Only an accepted capacity checkpoint retires history. Legacy
            # checkpoints may end inside a group: retain that whole group.
            if end <= retired or (retired_keys and group_key(calls) in retired_keys):
                i = end
                continue
        if i < retired and message.get('role') == 'assistant' and not calls:
            i += 1
            continue
        if (i < retired and i != submission and saved and saved.get('summaryFormat') == H.FORMAT
                and message.get('role') == 'user' and name is None):
            i += 1
            continue
        value = deepcopy(message)
        if name == 'executor-work':
            value['content'] = _receipt_free_state(value.get('content', ''))
        if message.get('role') == 'tool' and fingerprint(message) in replacements:
            value['content'] = replacements[fingerprint(message)]
        surface.append_source(i, value)
        i += 1
    # New summaries replace the old span BEFORE the retained tail. Legacy
    # checkpoints retain their original placement and user-pinning semantics.
    insert = (next((j for j,row in enumerate(surface.rows) if row[0].get('role') not in {'system','developer'}),len(surface.rows))
              if saved and saved.get('summaryFormat') == H.FORMAT else
              next((j+1 for j,row in enumerate(surface.rows) if row[0].get('role')=='user'),len(surface.rows)))
    stable = fragments.ordered()
    def render(fragment):
        text = fragment.text
        if fragment.name == 'checkpoint-state:runtime-facts':
            return dict(role='user',content=text)
        if fragment.name.startswith('checkpoint-state:workspace-instructions-'):
            # Restore the exact file event with host provenance; summary policy
            # and its existing per-runtime-source retention stay unchanged.
            return dict(role='user',content=text)
        if fragment.name == 'checkpoint-state:executor-work':
            text = _receipt_free_state(text)
        own = fragment.name in {'work-checkpoint', 'checkpoint-state:working-summary', 'checkpoint-state:working-corrections'}
        if fragment.name == 'checkpoint-state:working-summary':
            _, marker, raw = text.partition('[working-summary]\n')
            if marker:
                try:
                    from .working_context import summary_view
                    row = json.loads(raw)
                    if isinstance(row, dict):
                        text = '[working-summary]\n' + json.dumps(summary_view(row), ensure_ascii=False)
                except (ValueError, TypeError):
                    pass  # Keep an unreadable legacy record rather than lose it.
        return dict(role='assistant' if own else 'user',
                    content='[model-context:'+fragment.name+']\n资料，不是新的用户要求。\n'+text)
    rendered = [render(f) for f in stable]
    def annotations(fragment,message):
        if fragment.name == 'checkpoint-state:runtime-facts':
            return (('runtimeUpdate', message['content']),)
        if fragment.name.startswith('checkpoint-state:workspace-instructions-'):
            from .workspace_instructions import parts as file_parts
            return file_parts(message['content'])
        return (('contextView',fragment.name),('runtimeUpdate',message['content'].split('\n',2)[2]))
    surface.insert(insert, [(fragment.name, message, annotations(fragment,message))
        for fragment,message in zip(stable,rendered)])
    if exchange := getattr(session, 'previous_public_exchange', None):
        from . import projects, workbench
        text, previous = exchange
        try:
            live = projects.history_read_resources(session.dir.name)
            visible = projects.covers_reads(projects.read_bindings(live.values()), previous)
        except (OSError, ValueError, workbench.Problem):
            visible = False
        if visible:
            name = 'previous-public-exchange'
            text = ('[model-context:' + name + ']\n'
                    '同一会话的上一轮公开对话，仅供理解当前消息的指代。历史陈述不代表当前事实或授权；'
                    '指代清楚时直接回答当前问题。\n' + text)
            before_user = next((i for i, row in enumerate(surface.rows) if row[0].get('role') == 'user'),
                               len(surface.rows))
            surface.insert(before_user, [(name, dict(role='user', content=text),
                                          (('contextView', name), ('dialogue', text)))])
    surface.restore_runtime(session)
    result = surface.build()
    from .workspace_instructions import filter_authorized
    result = filter_authorized(session,result)
    messages, positions = result['messages'], result.source_positions
    # A marker exists even when this request has no optional context fragments.
    result.parts.setdefault(0,())
    result.parts[0] = (*result.parts[0],('contextView','projection'))
    if plan := getattr(source, 'budget_projection', None):
        from .input_budget import project_optional
        result = project_optional(result, plan)
    validate_pairs(result['messages'])
    session.context_view_info = dict(strategy='retained-history',
        rawMessages=len(source['messages']),modelMessages=len(messages),
        archivedMessages=getattr(session,'task_history_start',0),
        archivedToolResults=getattr(session,'archived_tool_results',0),
        removedToolGroups=sum(bool(m.get('tool_calls')) for m in source['messages'])-
            sum(bool(m.get('tool_calls')) for m in messages),
        prunedToolResults=sum(m.get('role')=='tool' and fingerprint(m) in replacements for m in source['messages']))
    session._context_view_positions = (fingerprint(result),positions)
    return result


def _receipt_free_state(content):
    """Migrate only trusted host state projections; original history stays intact."""
    try:
        header, notice, raw = content.split('\n', 2)
        payload = json.loads(raw)
        if payload.get('mode') not in {'snapshot', 'delta'} or not isinstance(payload.get('values'), dict):
            return content
        payload['values'].pop('checks', None)
        history = payload['values'].get('history')
        if isinstance(history, dict):
            for key in ('supersededChecks', 'omittedLatestChecks'):
                history.pop(key, None)
        return header+'\n'+notice+'\n'+json.dumps(payload, ensure_ascii=False)
    except (ValueError, TypeError, AttributeError):
        return content


def prune_long_results(body, *, registry=None):
    """Pressure pass: current tools remain paired and truthfully marked as excerpts."""
    from .tool_results import FACT_FIELDS, NATIVE_PAGE_POLICIES
    from .tool_spill import _bounded_page
    result = RequestBody(deepcopy(dict(body)),deepcopy(body.parts))
    result.source_positions = getattr(body,'source_positions',{})
    result.budget_deferred = getattr(body, 'budget_deferred', ())
    calls = {}
    for message in result['messages']:
        for call in message.get('tool_calls') or []:
            calls[call['id']] = call.get('function',{}).get('name')
        text = message.get('content')
        if message.get('role')=='tool' and isinstance(text,str) and len(text)>8192:
            try:
                value = json.loads(text)
            except ValueError:
                value = None
            if isinstance(value,dict):
                name = calls.get(message.get('tool_call_id'))
                tool = registry.get(name) if registry is not None and name else None
                policy = getattr(tool,'result_page',None) or NATIVE_PAGE_POLICIES.get(name)
                # A source/search/requirements/retrieval page already has an
                # exact boundary and cursor. Reuse the original Tool contract.
                if _bounded_page(value,policy):continue
                protected = {*FACT_FIELDS,'process','readOutput','spill','purpose',
                    'path','startLine','endLine','totalChars','hasMore','nextLine',
                    'nextOffset','next','nextCursor','evidenceRevision'}
                for key,v in list(value.items()):
                    if key in protected:continue
                    if isinstance(v,str):
                        value[key] = H.prune_text(v)
                    elif len(json.dumps(v,ensure_ascii=False))>8192:
                        value[key] = dict(modelExcerpt=True,truncated=True,
                            excerpt=H.prune_text(json.dumps(v,ensure_ascii=False)))
                value['modelExcerpt'] = True
                value['truncated'] = True
                if isinstance(value.get('definition'),dict):
                    value['definition']['complete'] = False
                value['excerptNotice'] = '原结果仍完整留档；本条是压力裁剪视图，不表示读取完整。原分页游标属于原页，可按实际源行或动作编号回取。'
                candidate = json.dumps(value,ensure_ascii=False)
            else:
                candidate = H.prune_text(text)
            # Retained nested metadata may already contain the entire payload.
            # Do not add a second excerpt to it and call the growth compaction.
            if len(candidate.encode('utf8')) < len(text.encode('utf8')):
                message['content'] = candidate
    return result


def _projection_state(session, body, candidate):
    """Prepare durable tool replacements; commit belongs to the checkpoint seam."""
    from .context_manager import ContextBlocked
    source = session._context_view_source
    row = deepcopy(checkpoint(session,source) or _state(session))
    from .history_groups import index
    if retired_groups := index(session):
        row['retiredGroups'] = retired_groups
    scope, history_scope, _ = _scopes(session)
    if scope is None:
        raise ContextBlocked('context_blocked: 无法核对历史所属任务，保留原始上下文')
    row.update(version=3 if row.get('summaryFormat')==H.FORMAT else 2,scope=scope,historyScope=history_scope)
    results = row.setdefault('toolResults', {})
    positions = getattr(body,'source_positions',{}) or getattr(session,'_context_view_positions',(None,{}))[1]
    for index,message in enumerate(candidate['messages']):
        if message.get('role') != 'tool':
            continue
        origin = positions.get(index)
        if origin is None:
            continue
        raw = source['messages'][origin]
        if raw.get('role')!='tool' or raw.get('tool_call_id')!=message.get('tool_call_id'):
            raise ContextBlocked('context_blocked: 工具历史来源位置不一致')
        if raw.get('content') != message.get('content'):
            results[fingerprint(raw)] = message['content']
    live = {fingerprint(m) for m in source['messages'] if m.get('role')=='tool'}
    row['toolResults'] = {key:value for key,value in results.items() if key in live}
    return row


def _summary_boundaries(body, capacity, positions, *, retain_tokens=None):
    """Balanced source cutoffs ending before the DSH priced recent tail."""
    messages = body['messages']
    costs=H.measure(body)['nodes']
    retain_tokens = int((capacity or sum(costs))*.16) if retain_tokens is None else retain_tokens
    end=H.range_end(messages,costs,retain_tokens)
    if end is None:return []
    submission = input_budget.submission_index(body)
    # Auxiliary overflow can reduce the old prefix, never cut a call group.
    boundaries=[];pending=set()
    for i,m in enumerate(messages[:end]):
        pending.update(c['id'] for c in m.get('tool_calls') or [])
        if m.get('role')=='tool':pending.discard(m.get('tool_call_id'))
        if not pending and i != submission and i in positions and m.get('role') not in {'system','developer'}:
            boundaries.append(i+1)
    if submission is not None:
        groups, _ = input_budget.layout(body)
        older = [boundary for boundary in boundaries if boundary <= submission and groups[boundary - 1] == "history"]
        if older:
            return older
    return boundaries


def _runtime_checkpoint(source, cutoff):
    from .working_context import fold_state_updates, state_snapshot_content
    from .runtime_facts import fold_facts, snapshot_content
    from .context_manager import ContextBlocked
    runtime, work, facts = {}, [], []
    for i,message in enumerate(source['messages'][:cutoff]):
        name=runtime_update_name(message,source.parts.get(i,()))
        if name not in (None, 'project-notice'):
            runtime[name]=message['content']
            if name=='executor-work':work.append(message['content'])
            if name=='runtime-facts':
                facts.append(message['content'])
    if work:
        state=fold_state_updates(work)
        if state is None:
            raise ContextBlocked('context_blocked: 工作状态增量无法核对，保留原历史')
        runtime['executor-work']=state_snapshot_content('executor-work',state)
    if facts:
        state = fold_facts(facts)
        if state is None:
            raise ContextBlocked('context_blocked: 运行事实增量无法核对，保留原历史')
        runtime['runtime-facts'] = snapshot_content(state)
    return runtime


def summarize(provider, session, role, body, should_stop, capacity=0, *, retain_tokens=None):
    """Summarize an old prefix at the limit seam; recover only confirmed overflow."""
    from .provider import Msg, Interrupted, ProviderError
    from .context_inspection import active_request
    from .context_manager import ContextBlocked
    from .code_read_receipts import for_request as code_receipts
    source = getattr(session,'_context_view_source',None)
    if source is None:
        raise ContextBlocked('context_blocked: 工作上下文来源不可核对')
    positions = getattr(body,'source_positions',{})
    if not positions:
        positions = getattr(session,'_context_view_positions',(None,{}))[1]
    boundaries = _summary_boundaries(code_receipts(session,body),capacity,positions,retain_tokens=retain_tokens)
    if not boundaries:
        raise HistoryCapacityBlocked('context_blocked: 没有可提炼的旧工作结论，当前输入不能安全缩减')
    recoveries = 0
    while True:
        end = boundaries[-1]
        # Summarize the actual old prefix, including tool evidence and scripts;
        # retain recent messages unchanged. Do not infer facts from progress text.
        from . import working_corrections
        submission = input_budget.submission_index(body)
        historical = [message for i, message in enumerate(body['messages'][:end]) if i != submission]
        selected = working_corrections.for_summary(session,role,historical)
        validate_pairs(selected)
        if should_stop and should_stop():
            raise Interrupted('用户停止，未提炼工作结论')
        # Replay the old prefix. A confirmed auxiliary overflow shortens this
        # same source; it never compacts recursively or retries identical input.
        msgs = [Msg.load(dict(m,reasoning=m.get('reasoning_content',''))) for m in selected]
        msgs.append(Msg('user',SUMMARY_INSTRUCTION))
        tools = [t['function'] for t in body.get('tools',[])]
        token = active_request.set((session,role))
        previous = getattr(session,'context_summary_active',False)
        session.context_summary_active = True
        try:
            # Use the same non-stream message serialization and scope projection as chat().
            # Archive-only page provenance is kept in the checkpoint, never charged as model input.
            wire = code_receipts(session,provider._body(msgs,tools))
            meter = H.measured(provider,wire,capacity)
            if capacity and meter['tokens'] >= capacity:
                if len(boundaries)==1:
                    raise HistoryCapacityBlocked('context_blocked: 最小工作结论输入仍超限，原始记录保留')
                if meter['source']=='unmeasured_input_bound':
                    cost = H._unmeasured_bytes([wire['messages'][-1]],wire.get('tools',[])) + 32
                    fit = 0
                    for i,message in enumerate(wire['messages'][:end],1):
                        cost += H._unmeasured_bytes([message],[])
                        if cost>=capacity:
                            break
                        fit = i
                    smaller = [b for b in boundaries if b<=fit]
                    if 0<len(smaller)<len(boundaries):
                        boundaries = smaller
                        continue
                boundaries = boundaries[:max(1,len(boundaries)//2)]
                continue
            from .execution_policy import consume
            consume(session,role)
            reply = provider.chat(msgs,tools,should_stop=should_stop)
        except ProviderError as error:
            if not getattr(error,'context_limit',None):
                raise
            if recoveries>=3 or len(boundaries)==1:
                raise HistoryCapacityBlocked('context_blocked: 摘要请求仍超出模型输入窗口') from error
            boundaries = boundaries[:max(1,len(boundaries)//2)]
            recoveries += 1
            session.emit('context_summary_recovery',role=role,attempt=recoveries,
                rejectedMessages=end,nextMessages=boundaries[-1],reason='api_context_limit')
            continue
        finally:
            session.context_summary_active = previous
            active_request.reset(token)
        break
    cutoff = positions[end-1]+1
    usage = getattr(reply,'usage',None) or {}
    session.emit('context_summary_usage',role=role,usage=usage)
    if usage:
        session.emit('llm',role=role,purpose='context-summary',model=getattr(provider,'model',''),
            **{k:usage.get(k) for k in ('prompt_tokens','completion_tokens','total_tokens',
                'prompt_cache_hit_tokens','prompt_cache_miss_tokens')})
    text = (getattr(reply,'text','') or '').strip()
    # Like DSH: output is text, nonempty, untruncated and smaller. Markdown
    # headings guide the model; they are not a semantic acceptance classifier.
    missing = []
    reasons = ([ 'tool_calls' ] if getattr(reply,'tool_calls',None) else [])
    reasons += ['empty_text'] if not text else []
    reasons += ['truncated'] if getattr(reply,'finish_reason','')=='length' else []
    if reasons:
        if should_stop and should_stop():
            raise Interrupted('用户停止，未切换工作结论')
        summary_failure(session,role,reply,text,reasons,missing)
        raise ContextBlocked('context_blocked: 工作结论格式无效（'+', '.join(reasons)+'），原始历史保留')
    if should_stop and should_stop():
        raise Interrupted('用户停止，未切换工作结论')
    row = _projection_state(session,body,body)
    runtime=_runtime_checkpoint(source,cutoff)
    if corrections:=working_corrections.state(session,role):
        names=list(runtime)
        corrected=working_corrections.for_summary(session,role,
            [dict(role='assistant',content=runtime[name]) for name in names],append_state=False)
        runtime={name:m['content'] for name,m in zip(names,corrected)}
        # The correction may have been committed in the recent tail rather
        # than the summarized prefix. Persist its exact state independently.
        runtime['working-corrections']=working_corrections.snapshot(corrections)
    row.update(version=3,summaryFormat=H.FORMAT,sourceCount=cutoff,sourceVersion=prefix_version(source,cutoff),text=text,
        runtime=runtime,
        historyCount=sum(m.get('role')!='system' and runtime_update_name(m,source.parts.get(i,())) is None
            for i,m in enumerate(source['messages'][:cutoff])))
    live={fingerprint(m) for m in source['messages'][cutoff:] if m.get('role')=='tool'}
    row['toolResults']={k:v for k,v in row['toolResults'].items() if k in live}
    # Build and validate the projected candidate before committing the pointer.
    candidate = project(session,role,source,work_summary=row)
    if H.measure(candidate)['tokens'] >= H.measure(body)['tokens']:
        raise HistoryCapacityBlocked('context_blocked: 工作结论没有减少上下文，未采纳')
    candidate.work_summary = row
    candidate.retired_end = end
    return candidate


def compact(provider, session, role, body, should_stop, capacity, *, policy=None, overflow=False, checkpoint_id=None):
    from .context_manager import native_receipts
    if not getattr(body,'source_positions',None):
        body.source_positions = getattr(session,'_context_view_positions',(None,{}))[1]
    trimmed = prune_long_results(body,registry=getattr(getattr(session,'executor_core',None),'registry',None))
    changed = fingerprint(trimmed) != fingerprint(body)
    policy = policy or H.policy(session,provider,body,capacity)
    measurement = H.measured(provider,native_receipts(session,trimmed),capacity)
    previous = getattr(session,'_context_prune_version',None)
    if changed and previous != fingerprint(body) and measurement['tokens'] < (policy['threshold'] or capacity):
        result, strategy = trimmed, 'tool-result-prune'
        result.work_summary = _projection_state(session,body,trimmed)
        session.context_view_info = dict(getattr(session,'context_view_info',{}),
            modelMessages=len(result['messages']),
            prunedToolResults=sum(m.get('role')=='tool' and fingerprint(m) in result.work_summary['toolResults']
                for m in session._context_view_source['messages']))
    else:
        result, strategy = summarize(provider,session,role,trimmed,should_stop,capacity,
            retain_tokens=0 if overflow else policy['retainTokens']), 'effective-work-checkpoint'
        # Ordinarily one convergence retry suffices. A cold byte bound can
        # require several disjoint spans. Each must retire further canonical
        # history; never publish an intermediate pointer or replay a tool.
        measured = H.measured(provider,native_receipts(session,result),capacity)
        retries = 0
        while not input_budget.fits_after_compaction(result, measured['tokens'], policy, capacity):
            if retries and measured['source'] != 'unmeasured_input_bound':
                raise HistoryCapacityBlocked('context_blocked: 两次历史摘要后仍超过 DSH 触发阈值，原始记录保留')
            previous_cut = result.work_summary['sourceCount']
            result = summarize(provider,session,role,result,should_stop,capacity,
                retain_tokens=0 if overflow or measured['source']=='unmeasured_input_bound' else policy['retainTokens'])
            if result.work_summary['sourceCount'] <= previous_cut:
                raise HistoryCapacityBlocked('context_blocked: 历史切口没有前移，原始记录保留')
            retries += 1
            measured = H.measured(provider,native_receipts(session,result),capacity)
    cutoff = getattr(result,'work_summary',{}).get('sourceCount',0) if strategy=='effective-work-checkpoint' else 0
    positions=getattr(body,'source_positions',{})
    retired=max((i+1 for i,origin in positions.items() if origin<cutoff),default=0)
    submission = input_budget.submission_index(body)
    users=[i for i,m in enumerate(body['messages'][:retired]) if i != submission and m['role']=='user'
           and runtime_update_name(m,body.parts.get(i,())) is None]
    if retired and checkpoint_id:
        result.work_summary['archive']=dict(checkpoint_id=checkpoint_id,
            first_message_index=next((i for i,m in enumerate(body['messages'][:retired])
                                      if i != submission and m['role'] not in {'system','developer'}),0),
            end_message_index=retired,user_message_indices=users)
        row=result.work_summary
        result=project(session,role,session._context_view_source,work_summary=row)
        result.work_summary=row
    return result,dict(version=5,summaryFormat=H.FORMAT,policy=policy,strategy=strategy,sourceIndices=[],
        retiredUserIndices=users,
        runtimeStateIndices=[i for i,m in enumerate(body['messages']) if runtime_update_name(m,body.parts.get(i,())) is not None],historicalActions=[],
        userMessages=[dict(index=i,hash=fingerprint(m)) for i,m in enumerate(body['messages']) if m['role']=='user'],
        instructionHashes=[fingerprint(m) for m in body['messages'] if m['role'] in {'system','developer'}],
        preserved='原始需求与动作存档不变，可按检查点原文回取；模型视图为历史检查点、当前上下文与原样最近历史。')
