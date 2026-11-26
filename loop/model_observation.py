"""Best-effort response evidence. Never consulted by the loop or context builder.

The existing request snapshot names a physical attempt; requestVersion only names
its payload. Bodies are opt-in, redacted structured copies, not HTTP transcripts.
No headers, private reasoning text, or exception bodies are archived here.
"""
# 主要引用方包括 `provider.py` 和 `session.py`。
from __future__ import annotations

from collections import Counter, deque
from contextvars import ContextVar
from functools import wraps
import gzip
import json
import os
from pathlib import Path
import re
import threading
import time

from . import leaks, private_keys
from .evidence_redaction import MASK, SENSITIVE, redact
from .filelock import FileLock, atomic_json

_current = ContextVar('model_response_observation', default=None)
_lock = threading.RLock()
_signals = ('phase', 'end_turn', 'finish_reason', 'stop_reason', 'status')
_private = {'reasoning', 'reasoning_content', 'reasoning_details', 'thinking',
            'analysis', 'chain_of_thought', 'encrypted_content'}
_credentials = re.compile(r'(?i)headers?|cookies?|set-cookie')
_protocol = {
    'root': {'id','response_id','object','created','model','choices','usage','error'},
    'choices': {'index','delta','message','text'},
    'delta': {'role','content','tool_calls','refusal'},
    'message': {'role','content','tool_calls','refusal'},
    'tool_calls': {'index','id','type','function'},
    'function': {'name','arguments'},
    'error': {'type','code','message'},
}


def _protocol_copy(value, node='root'):
    if isinstance(value, list):
        return [_protocol_copy(item, node) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key.lower() in _private:
            out[key] = _reason(item)
        elif key in _protocol.get(node, set()) or key in _signals:
            out[key] = _protocol_copy(item, key)
        elif node == 'usage' and (isinstance(item, (int,float,dict)) or item is None):
            out[key] = _protocol_copy(item, node)
        else:
            # Unknown extensions retain their field position/type, not private values.
            out[key] = dict(omitted='unknown_field', type=type(item).__name__)
    return out


def _quiet(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as error:
            # No exception text: it can contain upstream credentials/content.
            self.row['observationError'] = type(error).__name__
            self.row['complete'] = False
            if method.__name__ == 'finish':
                self.frames.clear()
                self.assembled = None
                self.row['bodyCoverage'] = 'unavailable'
            if method.__name__ != 'persist':
                self.persist()
    return run


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf8'))


def _ref(value):
    return value if isinstance(value,str) and len(value) <= 256 else None


def _index(value):
    if value is None or type(value) is bool or type(value) is int and abs(value)<2**53:
        return value
    if isinstance(value,str) and len(value)<=64:
        return value
    return {'omitted':'index_type_or_size','type':type(value).__name__}


def _limit(name, default):
    try:
        return max(0, min(int(os.environ.get(name, default)), default))
    except (ValueError, TypeError):
        return default


def _reason(value):
    if isinstance(value, dict) and set(value)=={'privateReasoning','present','chars'} and value.get('privateReasoning') is True and type(value.get('present')) is bool and type(value.get('chars')) is int:
        return value
    return dict(privateReasoning=True, present=value is not None,
                chars=len(value) if isinstance(value, str) else _size(value))


def _copy(value, path=(), groups=None, depth=0):
    """Copy before adapter mutation; group SSE strings by semantic field/index.

    Masking a whole field's fragments is deliberate: a secret split between
    packets must not survive as several individually innocuous strings.
    """
    if depth > 40:
        return {'omitted': 'depth_limit'}
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key = str(key)
            if key.lower() in _private:
                out[key] = _reason(item)
            elif SENSITIVE.search(key) or _credentials.fullmatch(key):
                out[key] = MASK
            else:
                out[key] = _copy(item, path+(key,), groups, depth+1)
                if isinstance(item, str) and groups is not None:
                    groups.setdefault(path+(key,), []).append((out, key, item))
        return out
    if isinstance(value, list):
        out = []
        for index, item in enumerate(value):
            slot = item.get('index', index) if isinstance(item, dict) else index
            out.append(_copy(item, path+(str(slot),), groups, depth+1))
            if isinstance(item, str) and groups is not None:
                groups.setdefault(path+('*',), []).append((out, index, item))
        return out
    return value


def _sensitive(text, key):
    if key and key in text or private_keys.text(text) != text or leaks.scan(text) or re.search(
            r'(?i)[?&](?:api[_-]?key|access[_-]?token|auth[_-]?token|token|key)=|://[^/\s]+@',text):
        return True
    # JSON arguments can use short passwords, which pattern matching cannot see.
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return bool(re.search(r'''(?i)["'](?:password|passwd|secret|api[_-]?key|authorization|access[_-]?token)["']\s*:''', text))
    return isinstance(value, (dict, list)) and redact(value) != value


def _sanitize(value, key):
    groups = {}
    out = _copy(value, groups=groups)
    for fragments in groups.values():
        if _sensitive(''.join(item[2] for item in fragments), key):
            for parent, field, _ in fragments:
                parent[field] = MASK
        else:
            for parent, field, text in fragments:
                parent[field] = redact(text)
    return out


# 单个模型请求快照对应的观测。
# `ref` 来自请求记录，`row` 保存帧、工具身份与覆盖范围，`frames` 只在允许且未达限额时保留脱敏正文。
class Observation:
    def __init__(self, session, ref, provider, recovery, mode):
        self.session = session
        self.key = str(getattr(provider, 'api_key', '') or '')
        self.directory = Path(session.dir)/'model-responses'
        self.directory.mkdir(exist_ok=True)
        self.path = self.directory/(ref['snapshot_id']+'.json')
        self.enabled = os.environ.get('HARNESS_MODEL_RESPONSE_LOG') == '1'
        self.response_limit = _limit('HARNESS_MODEL_RESPONSE_MAX_BYTES', 4*1024*1024)
        self.run_limit = _limit('HARNESS_MODEL_RESPONSE_RUN_BYTES', 128*1024*1024)
        self.frames = []
        self.frame_bytes = 0
        self.head = []
        self.tail = deque(maxlen=4)
        self.actions = set()
        self.source_calls = {}
        self.turn = None
        self.closed = False
        self.assembled = None
        self.row = dict(version=1, **ref, mode=mode, state='recording', at=time.time(),
            recoveryRequestId=getattr(recovery, 'id', None),
            attempt=getattr(recovery, 'attempt', 0)+1,
            signals={name:dict(present=False, values=[]) for name in _signals},
            adapterFields={name:'Reply.finish_reason' if name=='finish_reason' else 'observed_only'
                           for name in _signals},
            frames=0, receivedBytes=0, malformedFrames=0, doneMarker=False, complete=False,
            upstreamIds=[], signalCoverage='incomplete',
            sourceCalls=[], sourceCallsCoverage='complete',
            redactionPolicy='Credentials masked; private reasoning lengths only; unknown extension values omitted.',
            bodyCapture='enabled' if self.enabled else 'disabled',
            links=[], linksComplete=False)
        self.persist()

    @_quiet
    def json_bytes(self, count, malformed=False):
        if not self.closed:
            self.row['receivedBytes'] = count
            self.row['malformedFrames'] += int(malformed)

    @_quiet
    def persist(self):
        row = _sanitize(self.row, self.key)
        if len(json.dumps(row,ensure_ascii=False,indent=1).encode('utf8')) > 64*1024:
            # Metadata has its own fixed ceiling, even when body capture is off.
            for field in ('links','sourceCalls'):
                items = row[field]
                row[field] = items[:2]+items[-2:] if len(items)>4 else items
            for signal in row['signals'].values():
                items = signal['values']
                signal['values'] = items[:1]+items[-1:] if len(items)>2 else items
            if 'reply' in row:
                row['reply']['toolCalls'] = row['reply']['toolCalls'][:4]
                row['reply']['duplicateCallIds'] = row['reply'].get('duplicateCallIds',[])[:4]
            if 'actionCalls' in row:
                row['actionCalls'] = dict(list(row['actionCalls'].items())[:4])
                row['actionCallCoverage'] = 'incomplete'
            row.update(metadataClipped=True, complete=False, linksComplete=False,
                       signalCoverage='incomplete', sourceCallsCoverage='incomplete')
        atomic_json(self.path, row)

    @_quiet
    def received(self, obj):
        if self.closed:
            return
        self.row['frames'] += 1
        number = self.row['frames']
        if isinstance(obj,dict):
            for field in ('id','response_id'):
                value = obj.get(field)
                if isinstance(value,str) and len(value) <= 256:
                    item = dict(path='$.'+field, value=value)
                    if item not in self.row['upstreamIds'] and len(self.row['upstreamIds']) < 16:
                        self.row['upstreamIds'].append(item)
            # 按上游 choice 和工具槽位关联流式片段。这里只记录原协议出现的字段，实际工具动作由循环另行关联。
            for choice_position, choice in enumerate(obj.get('choices') or ()):
                if not isinstance(choice,dict):
                    continue
                choice_index = choice.get('index',choice_position)
                message = choice.get('delta') or choice.get('message') or {}
                if not isinstance(message,dict):
                    continue
                for position, call in enumerate(message.get('tool_calls') or ()):
                    if not isinstance(call,dict):
                        continue
                    index = call.get('index',position)
                    slot = (str(choice_index),str(index))
                    if slot not in self.source_calls:
                        if len(self.source_calls) >= 256:
                            self.row['sourceCallsCoverage'] = 'incomplete'
                            continue
                        item = dict(choiceIndex=_index(choice_index),toolIndex=_index(index),firstFrame=number,
                                    fragments=0,idPresent=False,idValues=[],argumentChars=0)
                        self.source_calls[slot] = item
                        self.row['sourceCalls'].append(item)
                    item = self.source_calls[slot]
                    item['fragments'] += 1
                    item['lastFrame'] = number
                    item['idPresent'] |= 'id' in call
                    ident = _ref(call.get('id'))
                    if ident is not None and ident not in item['idValues'] and len(item['idValues']) < 8:
                        item['idValues'].append(ident)
                    fn = call.get('function') or {}
                    if isinstance(fn,dict) and isinstance(fn.get('arguments'),str):
                        item['argumentChars'] += len(fn['arguments'])
        def visit(value, path='$', depth=0):
            if depth > 40:
                self.row['signalOverflow'] = True
                return
            if isinstance(value, dict):
                for field, item in value.items():
                    if field in _signals:
                        signal = self.row['signals'][field]
                        signal['present'] = True
                        if len(signal['values']) < 128:
                            location = path+'.'+field
                            signal['values'].append(dict(frame=number, path=location if len(location)<=512 else '[path_omitted]',
                                value=item if item is None or isinstance(item, (bool, int, float))
                                or isinstance(item, str) and len(item) <= 256
                                else {'type':type(item).__name__, 'omitted':True}))
                        else:
                            self.row['signalOverflow'] = True
                    if field.lower() not in _private:
                        visit(item, path+'.'+field, depth+1)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    visit(item, path+'[%d]'%index, depth+1)
        visit(obj)
        preview = dict(frame=number, type=type(obj).__name__)
        if len(self.head) < 4:
            self.head.append(preview)
        else:
            self.tail.append(preview)
        if self.enabled and self.row['bodyCapture'] == 'enabled':
            frame = _protocol_copy(obj)
            self.frame_bytes += _size(frame)
            if self.frame_bytes <= max(self.response_limit, 4*1024*1024):
                self.frames.append(frame)
            else:
                self.frames.clear()
                self.row['bodyCapture'] = 'buffer_limit'

    @_quiet
    def line(self, raw, obj):
        self.row['receivedBytes'] += len(raw)
        if obj == 'DONE':
            self.row['doneMarker'] = True
        elif obj is not None:
            self.received(obj)
        elif raw.lstrip().startswith(b'data:') and raw.lstrip()[5:].strip():
            self.row['malformedFrames'] += 1

    @_quiet
    def parsed(self, reply):
        # Aggregation can succeed before the existing strict validator rejects it.
        self.assembled = reply

    @_quiet
    def finish(self, reply=None, error=None):
        if self.closed:
            return
        self.closed = True
        _current.set(self)  # A nested summary must not own its caller's later events.
        self.row.update(state='parsed' if reply is not None else 'error', finishedAt=time.time(),
                        complete=not bool(self.row.get('observationError')))
        self.row['replyReturned'] = reply is not None and error is None
        self.row['signalCoverage'] = ('complete' if (self.row['doneMarker'] or self.row['mode']=='json' and self.row['frames'])
            and not self.row['malformedFrames'] and not self.row.get('signalOverflow') else 'incomplete')
        parsed = None
        candidate = reply if reply is not None else self.assembled
        if candidate is not None:
            usage = _protocol_copy(candidate.usage, 'usage')
            parsed = dict(text=candidate.text, tool_calls=candidate.tool_calls,
                          reasoning=_reason(candidate.reasoning), usage=usage,
                          finish_reason=candidate.finish_reason)
            calls = candidate.tool_calls
            self.row['reply'] = dict(textChars=len(candidate.text), toolCallCount=len(calls),
                toolCalls=[dict(id=_ref(c.get('id')), name=_ref(c.get('name')),
                                argumentChars=_size(c.get('args')),
                                argumentRef='body.reply.tool_calls[%d].args'%i)
                           for i,c in enumerate(calls[:256])],
                omittedCalls=max(0,len(calls)-256), reasoning=_reason(candidate.reasoning),
                usage=usage if _size(usage)<=4096 else {'omitted':'metadata_limit'},
                finishReason=_ref(candidate.finish_reason))
            counts = Counter(c.get('id') for c in calls if _ref(c.get('id')) is not None)
            self.row['reply']['duplicateCallIds'] = [key for key,count in counts.items() if count>1][:256]
        if error is not None:
            self.row['error'] = dict(type=type(error).__name__, kind=getattr(error, 'kind', None),
                                     httpStatus=getattr(error, 'status', None))
        # Signal strings can also be fragmented. Never persist a partial secret.
        for signal in self.row['signals'].values():
            strings = [v for v in signal['values'] if isinstance(v['value'], str)]
            if _sensitive(''.join(v['value'] for v in strings), self.key):
                for v in strings:
                    v['value'] = MASK
        if self.row['bodyCapture'] == 'enabled':
            groups = {}
            frames = [_copy(frame, groups=groups) for frame in self.frames]
            for fragments in groups.values():
                if _sensitive(''.join(v[2] for v in fragments), self.key):
                    for parent, field, _ in fragments:
                        parent[field] = MASK
            body = _sanitize(dict(format='model-response-v1', frames=frames, reply=parsed), self.key)
            payload = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf8')
            if len(payload) > self.response_limit:
                self.row['bodyCapture'] = 'response_quota'
            else:
                quota_path = self.directory/('quota-'+self.row['runId']+'.json')
                with _lock, FileLock(self.directory/('quota-'+self.row['runId']+'.lock'), timeout=0):
                    quota = json.loads(quota_path.read_text(encoding='utf8')) if quota_path.exists() else {'bytes':0}
                    if quota['bytes']+len(payload) > self.run_limit:
                        self.row['bodyCapture'] = 'run_quota'
                    else:
                        # Reserve before writing: a crash can overcount, never exceed the quota.
                        atomic_json(quota_path, dict(bytes=quota['bytes']+len(payload), limit=self.run_limit))
                        target = self.path.with_suffix('.json.gz')
                        temporary = target.with_suffix('.tmp')
                        temporary.write_bytes(gzip.compress(payload, mtime=0))
                        os.replace(temporary, target)
                        self.row.update(bodyRef='model-responses/'+target.name, bodyBytes=len(payload),
                                        bodyCapture='stored')
        if self.row['bodyCapture'] != 'stored':
            self.row['bodyCoverage'] = 'unavailable'
            self.row['frameHeadTail'] = self.head+list(self.tail)
        else:
            self.row['bodyCoverage'] = 'redacted_structured_copy'
        self.frames.clear()
        self.source_calls.clear()
        self.assembled = None
        self.row['complete'] = (self.row['bodyCapture']=='stored' and not error
            and not self.row.get('observationError') and not self.row['malformedFrames'])
        self.row = _sanitize(self.row, self.key)
        # Retain the in-memory key for redacting later call/action IDs too. It is
        # never persisted (including its hash), and leaves with this run's owner.
        self.persist()

    @_quiet
    def link(self, event, branch=None):
        if len(self.row['links']) >= 512:
            self.row['linksComplete'] = False
            self.row['omittedLinks'] = self.row.get('omittedLinks', 0)+1
            return
        fields = ('seq','act','turn','call_id','action_id','operationId','operation_id','state','status')
        item = {k:(dict(omitted='metadata_limit',chars=len(event[k]))
                   if isinstance(event[k],str) and len(event[k])>256 else event[k])
                for k in fields if k in event}
        if branch:
            item['branch'] = branch
        if item not in self.row['links']:
            self.row['links'].append(item)
        self.row['linksThroughSeq'] = event.get('seq')


class _NoObservation:
    def json_bytes(self, *args, **kwargs): pass
    def received(self, *args): pass
    def parsed(self, *args): pass
    def line(self, *args): pass
    def finish(self, *args, **kwargs): pass


def begin(req, provider, recovery, mode):
    """This boundary must never turn a logging failure into a provider failure."""
    try:
        from .context_inspection import active_request
        owner = active_request.get()
        ref = getattr(req, '_context_observation', None)
        if not owner or not ref:
            _current.set(None)
            return _NoObservation()
        session, _ = owner
        with _lock:
            previous = _current.get()
            if previous is not None:
                previous.persist()
            obs = Observation(session, ref, provider, recovery, mode)
            entries = getattr(session, '_response_observations', None)
            if entries is None:
                entries = session._response_observations = []
            entries.append(obs)
            # The request log already has a 512 attempts/run limit. Retire old runs.
            entries[:] = [r for r in entries if r.row['runId'] == ref['runId']]
            _current.set(obs)
            req._response_observation = obs
        return obs
    except Exception:
        _current.set(None)
        return _NoObservation()


def observe_event(session, row):
    """Reference existing events, without adding fields to their primary log."""
    try:
        if row.get('act') not in {'context_request','llm','say','action','tool','not_run',
                'ask_human','needs_human','tool_missing','empty','truncated','error','paused',
                'stuck','provider_recovery','interrupted','end','role_attempt_end'}:
            return
        with _lock:
            entries = getattr(session, '_response_observations', ())
            current = _current.get()
            run = row.get('run_id')
            active = current if current in entries and current.row['runId'] == run else None
            if active and active.row['purpose'] != 'dialogue' and row.get('act') != 'provider_recovery':
                active = None
            act = row.get('act')
            if act == 'provider_recovery':
                for entry in entries:
                    if entry.row['runId']==run and entry.row['recoveryRequestId']==row.get('requestId') and entry.row['attempt']==row.get('attempt'):
                        entry.link(row, 'provider_recovery')
                        entry.persist()
                return
            if act == 'context_request' and active:
                active.persist()
                return
            if act == 'llm' and active:
                active.turn = row.get('turn')
                active.row['loopTurn'] = active.turn
                active.link(row, 'reply_usage')
            elif act == 'say' and active and row.get('role', active.row['role']) == active.row['role']:
                active.turn = row.get('turn', active.turn)
                active.row['loopTurn'] = active.turn
                active.link(row, 'tools_requested' if row.get('toolPrelude') else
                            'text_reply' if row.get('roleSummary') else 'say')
            else:
                action = row.get('action_id')
                matches = [e for e in entries if e.row['runId'] == run and action and action in e.actions]
                if not matches and action and row.get('turn') is not None:
                    candidates = [e for e in entries if e.row['runId'] == run
                        and e.turn == row['turn'] and e.row['role'] == row.get('role','executor')
                        and e.row['purpose'] == 'dialogue']
                    if len(candidates) == 1:
                        matches = candidates
                        matches[0].actions.add(action)
                if not matches and active and action and (row.get('turn') == active.turn and active.turn is not None):
                    active.actions.add(action)
                    matches = [active]
                if act == 'tool' and row.get('call_id'):
                    candidates = [e for e in entries if e.row['runId'] == run
                        and row.get('role',e.row['role']) == e.row['role']
                        and any(c.get('id') == row['call_id'] for c in e.row.get('reply',{}).get('toolCalls',()))
                        and (row.get('turn') is None or e.turn is None or row['turn'] == e.turn)]
                    if active in candidates:
                        candidates = [active]
                    if len(candidates) == 1:
                        matches = candidates
                        if action:
                            matches[0].actions.add(action)
                            ident = _ref(row['call_id'])
                            matches[0].row.setdefault('actionCalls',{})[action] = [ident] if ident is not None else []
                            if ident is None:
                                matches[0].row['actionCallCoverage'] = 'incomplete'
                if active and action and not matches and act in {'not_run','needs_human'}:
                    matches = [active]
                    active.actions.add(action)
                for entry in matches:
                    entry.link(row, 'human_question' if act == 'ask_human' else None)
                    if act in {'tool','not_run','ask_human','needs_human'}:
                        entry.persist()
                if active and not matches and act in {'ask_human','needs_human','tool_missing','empty','truncated','error','paused','stuck','provider_recovery','interrupted'}:
                    active.link(row, 'human_question' if act == 'ask_human' else act)
                    active.persist()
            if act in {'end','role_attempt_end'}:
                for entry in entries:
                    if entry.row['runId'] == run and (act=='end' or row.get('role',entry.row['role'])==entry.row['role']):
                        entry.link(row, 'run_end')
                        entry.row['linksComplete'] = not bool(entry.row.get('omittedLinks'))
                        entry.persist()
    except Exception:
        pass
