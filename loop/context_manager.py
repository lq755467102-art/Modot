"""Shared request-capacity accounting, independent of execution mode.

Capacity estimates are not a tokenizer. Portable checkpoints retain original
messages separately and never grant permission, reset budgets, or replay tools.
"""
# 主要引用方包括 `provider.py` 和 `working_context.py`。
import json
import math
import hashlib
from pathlib import Path
import time
import uuid
import re
from .filelock import FileLock, atomic_json, read_text

MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024
MAX_STORED_BYTES = 128 * 1024 * 1024


class ContextBlocked(RuntimeError):
    """The request could not fit safely; original session evidence remains intact."""


class HistoryCapacityBlocked(ContextBlocked):
    """The existing history compaction could not produce a smaller fitting view."""


class RequestBody(dict):
    """Wire payload with local-only source annotations indexed by message."""

    def __init__(self, body, parts):
        super().__init__(body)
        self.parts = parts


def json_field_parts(fields):
    """Compose an existing JSON prompt and mark the serialized field spans."""
    parts = [(kind if value else "protocol", json.dumps({name: value}, ensure_ascii=False)[1:-1])
             for name, value, kind in fields]
    return "{" + ", ".join(value for _, value in parts) + "}", parts


def content_spans(content, parts):
    """Resolve each annotated fragment to a non-overlapping span in the actual text."""
    if not isinstance(content, str):
        return []
    spans = []
    for kind, fragment in parts:
        if not fragment:
            continue
        start = content.find(fragment)
        for _, left, right in sorted(spans, key=lambda span: span[1]):
            if start < 0 or start + len(fragment) <= left:
                break
            if start < right:
                start = content.find(fragment, right)
        if start >= 0:
            spans.append((kind, start, start + len(fragment)))
    return spans


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode("utf8")).hexdigest()


def validate_pairs(messages):
    pending = set()
    for message in messages:
        if message.get("role") == "tool":
            ident = message.get("tool_call_id")
            if ident not in pending:
                raise ContextBlocked("context_blocked: 工具回执缺少对应调用，未发送请求")
            pending.remove(ident)
        elif pending:
            raise ContextBlocked("context_blocked: 工具调用尚未收到完整回执，未发送请求")
        for call in message.get("tool_calls", []):
            ident = call.get("id")
            if not ident or ident in pending:
                raise ContextBlocked("context_blocked: 工具调用身份冲突，未发送请求")
            pending.add(ident)
    if pending:
        raise ContextBlocked("context_blocked: 工具回执未齐，不整理进行中的调用")


def runtime_update_name(message, parts=()):
    """Only annotated local data may be folded; user-written markers aren't authority."""
    kinds = {kind for kind, _ in parts}
    if not kinds.intersection({'runtimeUpdate', 'projectActivity'}):
        return None
    content = message.get('content')
    match = re.match(r'^\[context-update:([\w-]+):[^\]\n]+\]\n', content) if isinstance(content,str) else None
    return match[1] if match else '__runtime__' if 'runtimeUpdate' in kinds else None


def candidate(body, keep_tools=6, checkpoint_id=None):
    """Portable, extractive compaction. User/system text is never summarized.

    Retain recent protocol groups byte for byte. Older observed tool results
    become versioned evidence references; unreferenced results remain intact.
    Public assistant statements are retained, not promoted into instructions.
    """
    result = json.loads(json.dumps(body))
    messages = result.get("messages", [])
    validate_pairs(messages)
    if checkpoint_id is not None:
        return indexed_candidate(result, keep_tools, checkpoint_id, runtime_indices=[
            i for i, spans in getattr(body, 'parts', {}).items() if runtime_update_name(messages[i],spans) is not None])
    assistant = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    tools = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    tool_indices = set(tools)
    recent_assistant = set(assistant[-max(2, keep_tools):])
    recent_tools = set(tools[-keep_tools:])
    references = []
    for index, message in enumerate(messages):
        if message.get("role") == "assistant" and index not in recent_assistant:
            if "reasoning_content" in message:
                message["reasoning_content"] = ""
        if index not in tool_indices or index in recent_tools:
            continue
        content = message.get("content")
        if not isinstance(content, str) or len(content) < 1000:
            continue
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            continue
        ref = value.get("_harnessEvidence") if isinstance(value, dict) else None
        if not isinstance(ref, dict) or not ref.get("actionId") or not ref.get("runId"):
            continue
        if value.get("compacted") is True:
            continue
        ref = {key: str(ref[key])[:128] for key in ("actionId", "runId", "role") if key in ref}
        references.append(dict(index=index, source=ref, originalHash=fingerprint(content),
                               observedOk=value.get("ok") if type(value.get("ok")) is bool else None))
        message["content"] = json.dumps(dict(ok=value.get("ok"), compacted=True,
            orig_chars=len(content), _harnessEvidence=ref,
            note="历史输出已另存。细节使用 read_action_evidence 按 action_id/run_id 只读回取；不能重放原工具，不能将摘要当作当前文件或测试通过。"), ensure_ascii=False)
    # This handoff is an index of facts, not a model-authored task judgement.
    handoff = dict(version=1, strategy="portable-evidence-index",
        userMessages=[dict(index=i, hash=fingerprint(m)) for i, m in enumerate(messages) if m.get("role") == "user"],
        instructionHashes=[fingerprint(m) for m in messages if m.get("role") in {"system", "developer"}],
        historicalActions=references,
        preserved="全部用户原文、当前指令、公开助手陈述、最近工具配对原样保留。旧工具细节由原动作引用回取。",
        unknown="未完成事项、假设和下一步不由长度裁剪推断；以保留的用户要求、任务记录及实际工具证据为准。")
    validate_pairs(messages)
    return result, handoff


def indexed_candidate(result, keep_tools, checkpoint_id, runtime_indices=()):
    """Archive whole completed call groups, including large write arguments.

    The index is observational data, never a substitute for user instructions.
    Every omitted public message remains retrievable by immutable source index.
    """
    original=result['messages'];groups=[]
    summary_indices = [i for i,m in enumerate(original) if '[working-summary]' in str(m.get('content','')) and m.get('role') in {'assistant','user'} and not m.get('tool_calls')]
    latest_summary = summary_indices[-1] if summary_indices else None
    work_indices = [i for i,m in enumerate(original)
                    if str(m.get('content', '')).startswith('[context-update:executor-work:')]
    latest_work = work_indices[-1] if work_indices else None
    latest_runtime = {}
    for index in runtime_indices:
        content = original[index].get('content','')
        match = re.match(r'^\[context-update:([\w-]+):',content) if isinstance(content,str) else None
        latest_runtime[match[1] if match else '__runtime__'] = index
    retained_runtime = {*latest_runtime.values(),latest_summary,latest_work}
    # Only at the existing capacity-triggered compaction: a retained delta must
    # become a complete state before its earlier updates are archived.
    from .working_context import fold_state_updates, state_snapshot_content
    work_state = fold_state_updates(original[i].get('content','') for i in runtime_indices
        if str(original[i].get('content','')).startswith('[context-update:executor-work:'))
    for i,message in enumerate(original):
        if message.get('role')=='assistant' and message.get('tool_calls'):
            end=i+1+len(message['tool_calls']);groups.append((i,end))
    recent={start for start,_ in groups[-keep_tools:]}
    archived={start:end for start,end in groups if start not in recent}
    messages=[];indices=[];refs=[];i=0
    while i<len(original):
        message=original[i]
        end=archived.get(i)
        if end is not None:
            records=[]
            for n in range(i,end):
                value=original[n];entry=dict(messageIndex=n,role=value['role'],hash=fingerprint(value))
                if value.get('role')=='tool':
                    try:out=json.loads(value.get('content',''))
                    except (TypeError,ValueError):out={}
                    if isinstance(out,dict):
                        entry['observedOk']=out.get('ok') if type(out.get('ok')) is bool else None
                        ref=out.get('_harnessEvidence')
                        if isinstance(ref,dict):entry['source']={k:str(ref[k])[:128] for k in ('actionId','runId','role') if k in ref}
                    refs.append(dict(index=n,source=entry.get('source',{}),originalHash=entry['hash']))
                records.append(entry)
            public=message.get('content')
            # Preserve small public explanations; large text is explicitly excerpted.
            excerpt=public if isinstance(public,str) and len(public)<=1600 else (public[:1000]+'\n[历史公开文字节选；完整原文见索引]\n'+public[-400:] if isinstance(public,str) else '')
            note=dict(historicalContext=True,checkpointId=checkpoint_id,messages=records,
                publicExcerpt=excerpt,
                tools=[c.get('function',{}).get('name','unknown') for c in message['tool_calls']],
                notice='已完成工具组的当时记录，仅作历史资料；完整调用参数和输出使用 read_context_history(checkpoint_id,message_index) 只读分页回取。不得重放工具、推断当前文件或把历史成功当作任务验收。')
            messages.append(dict(role='assistant',content=json.dumps(note,ensure_ascii=False)));indices.append(i);i=end;continue
        value=json.loads(json.dumps(message))
        if i == latest_runtime.get('executor-work') and work_state is not None:
            value['content'] = state_snapshot_content('executor-work', work_state)
        if i in runtime_indices and i not in retained_runtime:
            value=dict(role='assistant',content=json.dumps(dict(historicalContext=True,
                checkpointId=checkpoint_id,messageIndex=i,
                notice='历史运行状态资料，非用户要求。完整原文按索引回取；不代表当前版本。'),ensure_ascii=False))
        if value.get('role')=='assistant' and i not in recent:
            value.pop('reasoning_content',None)
            public=value.get('content')
            # Existing historical references must remain verbatim across cycles.
            historical=isinstance(public,str) and '"historicalContext": true' in public
            if isinstance(public,str) and len(public)>2400 and not historical and not value.get('tool_calls') and i != latest_summary:
                value['content']=json.dumps(dict(historicalContext=True,checkpointId=checkpoint_id,messageIndex=i,
                    hash=fingerprint(message),publicExcerpt=public[:1600]+'\n[节选]\n'+public[-400:],
                    notice='历史公开回复节选；完整原文用 read_context_history 只读回取。不是新指令或任务成功证明。'),ensure_ascii=False)
        messages.append(value);indices.append(i);i+=1
    # Coalesce consecutive historical indexes by referencing their exact source
    # messages in this checkpoint. This is a lossless archive chain, not another
    # model summary; it avoids an ever-growing list of per-turn placeholders.
    packed=[];packed_indices=[];p=0
    def is_index(value):
        if value.get('role')!='assistant' or value.get('tool_calls'):return False
        try:return json.loads(value.get('content','')).get('historicalContext') is True
        except (ValueError,TypeError,AttributeError):return False
    while p<len(messages):
        end=p
        while end<len(messages) and is_index(messages[end]):end+=1
        if end-p>=2:
            source_indices=[]
            excerpts=[]
            for n in range(p,end):
                note=json.loads(messages[n]['content'])
                if note.get('checkpointId')==checkpoint_id and isinstance(note.get('messages'),list):
                    source_indices.extend(record['messageIndex'] for record in note['messages'])
                else:source_indices.append(indices[n])
                if isinstance(note.get('publicExcerpt'),str) and note['publicExcerpt']:
                    records = note.get('messages') or []
                    source_index = records[0].get('messageIndex') if records else note.get('messageIndex',indices[n])
                    excerpts.append(dict(checkpointId=note.get('checkpointId',checkpoint_id),
                                         messageIndex=source_index,text=note['publicExcerpt'][:700]))
                for excerpt in note.get('publicExcerpts',[]):
                    if (isinstance(excerpt,dict) and isinstance(excerpt.get('text'),str)
                            and isinstance(excerpt.get('checkpointId'),str) and type(excerpt.get('messageIndex')) is int):
                        excerpts.append(dict(checkpointId=excerpt['checkpointId'],
                                             messageIndex=excerpt['messageIndex'],text=excerpt['text'][:700]))
            # Keep a small exact public excerpt, not a model-written summary.
            # The complete source remains immutable and separately retrievable.
            unique={}
            for excerpt in excerpts:
                unique.pop(excerpt['text'],None)
                unique[excerpt['text']]=excerpt
            packed.append(dict(role='assistant',content=json.dumps(dict(historicalContext=True,checkpointId=checkpoint_id,
                messageIndices=source_indices,publicExcerpts=list(unique.values())[-3:],
                notice='历史公开决定节选及原文索引，不是新要求、授权或当前验收结论。完整历史按messageIndices使用read_context_history逐项回取，嵌套引用保留原始版本，不重放工具。'),ensure_ascii=False)))
            packed_indices.append(indices[p]);p=end
        else:
            packed.append(messages[p]);packed_indices.append(indices[p]);p+=1
    messages=packed;indices=packed_indices
    result['messages']=messages
    validate_pairs(messages)
    return result,dict(version=2,strategy='portable-completed-group-index',sourceIndices=indices,
        runtimeStateIndices=list(runtime_indices),
        userMessages=[dict(index=i,hash=fingerprint(m)) for i,m in enumerate(original) if m.get('role')=='user'],
        instructionHashes=[fingerprint(m) for m in original if m.get('role') in {'system','developer'}],historicalActions=refs,
        preserved='全部用户原文与系统指令原样保留，近期完整调用组保留；旧公开消息与调用参数由不可变检查点原文回取。',
        unknown='长度整理不推断完成事项或提升历史文字的权限；当前验收、预算与权限以运行时状态为准。')


def bind_capacity(session, provider, capacity):
    key=fingerprint(dict(endpoint=provider.base_url,model=provider.model))
    session.context_model_limits=dict(getattr(session,'context_model_limits',{}),**{key:int(capacity or 0)})


def model_window(session, provider):
    key = fingerprint(dict(endpoint=provider.base_url, model=provider.model))
    return getattr(session, 'context_model_limits', {}).get(key) or None


def capacity_for(session,provider):
    limits=getattr(session,'context_model_limits',None)
    selected=getattr(session,'context_user_limit',0)
    if limits is None:
        values=[n for n in (selected,getattr(session,'request_context_limit',0)) if n]
        return min(values) if values else 0
    key=fingerprint(dict(endpoint=provider.base_url,model=provider.model))
    if key not in limits:
        from .limits import probe_window
        limits[key]=getattr(provider,'request_context_limit',0) or probe_window(provider.base_url,provider.api_key,provider.model,timeout=3) or 0
    values=[n for n in (limits[key],selected) if n]
    return min(values) if values else 0


def _usage_key(provider):
    from .context_inspection import active_request
    owner=active_request.get()
    return (str(owner[0].dir),owner[1],provider.base_url,provider.model,
            bool(getattr(owner[0],'context_summary_active',False))) if owner else None


def _input_header(body, *, include_tools=True):
    # Output ceilings and streaming transport do not identify the input prefix.
    return fingerprint({k:v for k,v in body.items()
        if k not in {'messages','max_tokens','max_completion_tokens','stream','stream_options'}
        and (include_tools or k!='tools')})


def _input_identity(body):
    return _input_header(body),[fingerprint(message) for message in body.get('messages',[])]


def previous_dialogue_meter(session):
    if not hasattr(session,'_previous_dialogue_meter'):
        try:
            session._previous_dialogue_meter = session.load().get('dialogueInputMeter')
        except (AttributeError,OSError,ValueError):
            session._previous_dialogue_meter = None
    return session._previous_dialogue_meter


def _owned_dialogue_meter(session, owner):
    meter = getattr(session,'dialogue_input_meter',None) or previous_dialogue_meter(session)
    return meter if isinstance(meter,dict) and meter.get('owner') == owner else {}


def dialogue_meter_for(provider):
    from .context_inspection import active_request
    key = _usage_key(provider)
    if key is None or key[1] != 'executor':
        return {}
    return _owned_dialogue_meter(active_request.get()[0], fingerprint(key[:-1]))


def _measured_prefixes(meter):
    prefixes = meter.get('prefixes')
    return prefixes if isinstance(prefixes,dict) and len(prefixes) <= 64 else {}


def measured_wire_prefix(meter, identity):
    """Find the longest actual dialogue input at the start of this wire request."""
    prefixes = _measured_prefixes(meter)
    if not prefixes:
        return None
    header, messages = identity
    digest = hashlib.sha256(header.encode('ascii'))
    match = None
    for count, message in enumerate(messages,1):
        digest.update(message.encode('ascii'))
        tokens = prefixes.get(digest.hexdigest())
        if type(tokens) is int and tokens > 0:
            match = count, tokens
    return match


def record_usage(provider,body,usage):
    """Remember an actual returned count for this role/model, never an estimate."""
    key=_usage_key(provider)
    if not isinstance(usage,dict):return
    tokens=usage.get('prompt_tokens',usage.get('input_tokens'))
    if key is None or type(tokens) is not int or tokens<0:return
    header,messages=_input_identity(body)
    anchor=dict(tokens=tokens,header=header,messages=messages,
                headerWithoutTools=_input_header(body,include_tools=False),
                tools=[fingerprint(t) for t in body.get('tools',[])])
    provider.__dict__.setdefault('_context_input_usage',{})[key]=anchor
    from .context_inspection import active_request
    session, role = active_request.get()
    if role=='executor' and not key[-1] and getattr(session,'executor_core',None) is not None:
        # Published by the ordinary owned Session.save, never in model messages.
        # Retaining fewer hashes only makes a future cold bound more conservative.
        owner = fingerprint(key[:-1])
        previous = _owned_dialogue_meter(session,owner)
        prefixes = dict(_measured_prefixes(previous))
        digest = hashlib.sha256((header+''.join(messages)).encode('ascii')).hexdigest()
        prefixes.pop(digest,None)
        prefixes[digest] = tokens
        if len(prefixes) > 64:
            del prefixes[next(iter(prefixes))]
        # A whole paid prompt is a conservative upper bound for its system message
        # and tools alone, even when compaction replaces every subsequent message.
        overhead = previous.get('overhead',{})
        if messages and body['messages'][0]['role']=='system' and tokens>0:
            if (not isinstance(overhead,dict) or overhead.get('header')!=anchor['headerWithoutTools']
                    or overhead.get('system')!=messages[0] or type(overhead.get('tokens')) is not int
                    or tokens<overhead['tokens']):
                overhead = dict(header=anchor['headerWithoutTools'],system=messages[0],
                                tools=anchor['tools'][:256],tokens=tokens)
        session.dialogue_input_meter=dict(owner=owner,tokens=tokens, prefixes=prefixes,overhead=overhead,
            header=anchor['headerWithoutTools'],messages=messages[-256:],tools=anchor['tools'][:256])


def observed_input(provider,body,identity=None):
    counter=getattr(provider,'count_input_tokens',None)
    if callable(counter):
        tokens=counter(body)
        if type(tokens) is int and tokens>=0:return dict(tokens=tokens,source='verified_counter',exact=True)
    previous=getattr(provider,'_context_input_usage',{}).get(_usage_key(provider))
    if not previous:return None
    header,messages=identity or _input_identity(body)
    if header!=previous['header'] or messages[:len(previous['messages'])]!=previous['messages']:return None
    return dict(tokens=previous['tokens'],source='provider_usage',exact=len(messages)==len(previous['messages']))


def adopt(session, role, working, requested=None):
    """Use the durable working candidate while retaining the original archive.

    Temporary request-only instructions are not added to the session. An index
    and object identity check prevent replacing another role's message list.
    """
    pending=getattr(session,'context_adoptions',{}).pop(role,None)
    if not pending:return False
    if pending.get('workingView'):
        # The native model view never replaces canonical audit/resume messages.
        return False
    requested=working if requested is None else requested
    source=pending['original']['messages']
    request_only=pending.get('requestOnly',())
    persistent=[i for i in range(len(source)) if i not in request_only]
    previous_receipts=pending.get("previousToolReceipts", {})
    expected=[previous_receipts.get(i, source[i]) for i in persistent]
    if fingerprint([m.to_dict() for m in requested])!=fingerprint(expected):
        raise ContextBlocked('context_blocked: 活动消息已变化，保留原会话且不采纳候选')
    by_index=dict(zip(persistent,requested))
    owners={id(m) for m in working};candidate=pending['candidate']['messages'];indices=pending['handoff']['sourceIndices']
    from .provider import Msg
    replacement=[]
    for value,index in zip(candidate,indices):
        previous=by_index.get(index)
        if previous is None or id(previous) not in owners:
            continue
        parts=previous.context_parts if value.get("content")==source[index].get("content") else ()
        if (not parts and runtime_update_name(source[index], previous.context_parts) == 'executor-work'
                and str(value.get('content','')).startswith('[context-update:executor-work:')):
            parts=(("runtimeUpdate",value['content']),)
        if not parts and is_history_reference(value):
            parts=(("compactedHistory", value["content"]),)
        replacement.append(Msg.load(dict(value, reasoning=value.get('reasoning_content',''),
                                         context_parts=parts, image_action=getattr(previous, "image_action", None))))
    # No original user/system message can disappear or change through adoption.
    fixed=lambda rows:[m.to_dict() for m in rows if m.role in {'user','system','developer'}
                       and runtime_update_name(m.to_dict(),m.context_parts) is None]
    if fixed(replacement)!=fixed(working):raise ContextBlocked('context_blocked: 现行指令核对失败，未采纳候选')
    working[:]=replacement
    return True


def native_receipts(session, body, *, reuse=True):
    from .code_read_receipts import for_request as code_receipts
    from .subtask_status import for_request as subtask_receipts
    from .current_receipts import for_request as current_receipts
    body = code_receipts(session,subtask_receipts(session,body,reuse=reuse),reuse=reuse)
    return current_receipts(session, body, reuse=reuse)


def prepare(provider, body, should_stop=None, *, limit_evidence=None, keep_tools=6):
    """Prepare one actual model request across all managed roles and modes.

    Original loop messages stay untouched and remain the resume source. The
    independently persisted request checkpoint is switched only after fsync.
    No vendor-specific opaque compaction items are synthesized or transferred.
    """
    from .context_inspection import active_request
    from .provider import Interrupted
    owner = active_request.get()
    if not owner:
        return body
    session, role = owner
    if role == 'vision':
        # This request is owned by the run for receipts, but its input is one image.
        return body
    from .context_view import enabled as native_view
    working_view = native_view(session,role)
    if getattr(session,'context_summary_active',False):
        return native_receipts(session, body) if working_view else body
    if not getattr(session,'context_compaction_enabled',True):
        return native_receipts(session,body) if working_view else body
    if role == "executor" and isinstance(body, RequestBody):
        source_kinds = {"memory": "memoryPrivate", "skills": "skills", "plugins": "plugins"}
        spans = [(source_kinds[item["kind"]], item["text"])
                 for item in getattr(session, "context_sources", [])
                 if item["kind"] in source_kinds and item["text"]]
        if spans:
            for i, message in enumerate(body["messages"]):
                if message.get("role") == "system":
                    body.parts[i] = [*body.parts.get(i, ()), *spans]
    if working_view and any(kind=='contextView' for spans in getattr(body,'parts',{}).values() for kind,_ in spans):
        # API overflow retries may carry a prior projected request. Rebase on
        # canonical messages so committed pruning/checkpoints make progress.
        source=getattr(session,'_context_view_source',None)
        if source is not None:
            body=RequestBody(dict(body,messages=json.loads(json.dumps(source['messages']))),dict(source.parts))
    if working_view:
        # Auxiliary summaries also use the live source and its current grants.
        body = native_receipts(session,body, reuse=False)
    tool_receipts = {message["tool_call_id"]: message for message in body["messages"]
                     if message.get("role") == "tool" and message.get("tool_call_id")}
    from .context_surface import prepare_context
    body = prepare_context(session,role,body)
    wire = native_receipts(session,body) if working_view else body
    capacity = capacity_for(session,provider)
    # A failed preceding request cannot leave a candidate for a later call.
    getattr(session,'context_adoptions',{}).pop(role,None)
    original = body
    history_policy = None
    if working_view:
        from . import history_compaction as H
        from . import input_budget as B
        try:
            history_policy = H.policy(session,provider,wire,capacity)
        except ContextBlocked as error:
            before = assess(wire, capacity)
            window = model_window(session, provider)
            if window and before['reservedOutput'] >= window:
                budget = B.report(session, wire, capacity, dict(apiWindow=window, threshold=0))
                B.block(session, role, budget, "output", limit_evidence)
            session.emit('context_blocked',role=role,state='blocked',reason=str(error),before=before)
            raise
        plan = B.plan_optional(session, provider, wire, capacity, history_policy)
        if plan:
            body = B.project_optional(body, plan)
            wire = B.project_optional(wire, plan)
            session._context_view_source.budget_projection = plan
        wire.input_budget = B.report(session, wire, capacity, history_policy)
        required_only = B.check_required(session, provider, role, wire, wire.input_budget, limit_evidence)
        if not limit_evidence and required_only:
            return wire
        input_limit = wire.input_budget['inputLimit']
        if limit_evidence and limit_evidence.get('kind') == 'api_context_limit':
            guard = input_limit if required_only else history_policy['threshold']
            if (wire.input_budget['deferred'] and input_limit is not None
                    and H.measured(provider, wire, input_limit)['tokens'] < guard):
                wire.budget_optional_retry = True
                return wire
            if required_only:
                B.block(session, role, wire.input_budget, "provider", limit_evidence)
    if limit_evidence is None:
        if working_view:
            measured = H.measured(provider,wire,capacity)
            threshold = history_policy['threshold']
        else:
            measured=observed_input(provider,wire)
            threshold = capacity
        if not capacity or not measured or measured['tokens']<threshold:return wire
        limit_evidence=dict(kind='dsh_pressure' if working_view else measured['source'],
            measurement=measured['source'],inputTokens=measured['tokens'],limit=capacity,threshold=threshold)
    if should_stop and should_stop():
        raise Interrupted("用户请求停止，未开始整理上下文")
    run = session.run_id
    if role not in {"executor", "planner", "reviewer"} or not isinstance(run, str) or len(run) != 32 or any(c not in "0123456789abcdef" for c in run):
        raise ContextBlocked("context_blocked: 无法核对上下文所有者，未发送请求")
    before = assess(wire, capacity)
    if working_view:
        before.update(historyMeasurement=H.measure(wire), historyPolicy=history_policy, budget=wire.input_budget)
    directory = Path(session.dir) / "context-checkpoints"
    try:
        # Preserve the existing observation hook at the actual limit boundary.
        from . import hooks
        try:
            for result in hooks.run('PreCompact',dict(event='PreCompact',role=role,
                    tokens=limit_evidence.get('inputTokens'),messages=len(body['messages']),limit=capacity),session.dir):
                session.emit('hook',**result,note=hooks.brief(result))
        except Exception as error:
            session.emit('hook',event='PreCompact',ok=False,err=type(error).__name__)
        session.emit("context_compaction", role=role, state="preparing", before=before,trigger=limit_evidence)
        # Bounded retries rebuild from the same original, never summaries of
        # summaries. The newest protocol group is always kept unchanged.
        ident = uuid.uuid4().hex
        original_bytes=len(json.dumps(body,ensure_ascii=False).encode('utf8'))
        if working_view:
            from .context_view import compact
            try:
                compacted,handoff = compact(provider,session,role,body,should_stop,capacity,
                    policy=history_policy,overflow=limit_evidence.get('kind')=='api_context_limit',checkpoint_id=ident)
                verified = native_receipts(session, compacted)
                threshold = history_policy['threshold'] or capacity
                if ((threshold and H.measured(provider, verified, capacity)['tokens'] >= threshold)
                        or len(json.dumps(verified, ensure_ascii=False).encode('utf8')) >= original_bytes):
                    raise HistoryCapacityBlocked('context_blocked: 整理后的历史仍超出预算或没有缩减收益')
            except HistoryCapacityBlocked as exhausted:
                from .history_groups import retire
                compacted, handoff, original = retire(provider, session, role, body, original,
                    history_policy, capacity, ident, should_stop, exhausted)
            # Rebuild references from the retained full receipts so a removed
            # anchor cannot leave a short receipt dangling after compaction.
            compacted = native_receipts(session,compacted)
            after=assess(compacted,capacity)
            after['historyMeasurement'] = H.measure(compacted)
            after['historyPolicy'] = history_policy
            measured_after=H.measured(provider,compacted,capacity)
        else:
            for keep in [n for n in (6,3,1) if n<=keep_tools]:
                compacted, handoff = candidate(body, keep, ident)
                parts = {}
                for index, source_index in enumerate(handoff["sourceIndices"]):
                    current = compacted["messages"][index]
                    if current.get("content") == body["messages"][source_index].get("content"):
                        parts[index] = getattr(body, "parts", {}).get(source_index, ())
                    elif runtime_update_name(body['messages'][source_index], getattr(body,'parts',{}).get(source_index,())) == 'executor-work':
                        parts[index] = (("runtimeUpdate", current['content']),)
                    elif is_history_reference(current):
                        parts[index] = (("compactedHistory", current["content"]),)
                compacted = RequestBody(compacted, parts)
                handoff["recentToolResults"] = keep
                after = assess(compacted, capacity)
                measured_after=observed_input(provider,compacted)
                fits=not measured_after or not capacity or measured_after['tokens']<capacity
                if fits and len(json.dumps(compacted,ensure_ascii=False).encode('utf8'))<original_bytes:
                    break
        if measured_after and capacity and measured_after['tokens']>=capacity:
            raise ContextBlocked("context_blocked: 可回取历史整理后仍超过容量；原文已保留，请缩小当前输入或调整模型窗口")
        if working_view and history_policy['threshold'] and measured_after['tokens']>=history_policy['threshold']:
            raise ContextBlocked('context_blocked: 加入历史回取信息后仍达到 DSH 触发阈值，未切换检查点')
        if len(json.dumps(compacted,ensure_ascii=False).encode('utf8'))>=original_bytes:
            raise ContextBlocked("context_blocked: 整理无回收收益，未重复请求")
        directory.mkdir(exist_ok=True)
        if not directory.resolve().is_relative_to(Path(session.dir).resolve()):
            raise ContextBlocked("context_blocked: 检查点路径不属于会话")
        model = fingerprint(dict(endpoint=provider.base_url, model=provider.model))
        # Immutable source snapshot contains no API credentials or headers.
        record = dict(version=1, id=ident, runId=run, role=role, modelVersion=model,workingView=working_view,
            at=time.time(), sourceVersion=fingerprint(original), candidateVersion=fingerprint(compacted),
            original=original, candidate=compacted, handoff=handoff, before=before, after=after,trigger=limit_evidence,
            resourceScope=getattr(session, "context_scope", {}))
        if working_view and 'messageStart' in getattr(session, 'task_input', {}):
            record['taskInput'] = {key: session.task_input[key] for key in ('id', 'messageStart')}
        if working_view and getattr(compacted,'work_summary',None):
            record['workSummary'] = dict(compacted.work_summary,
                checkpointId=ident,candidateVersion=record['candidateVersion'])
        budget = getattr(session, "execution_budget", None)
        if budget:
            record["executionSnapshot"] = json.loads(read_text(budget))
        record["recordVersion"] = fingerprint(record)
        size = len(json.dumps(record, ensure_ascii=False).encode("utf8"))
        if size > MAX_CHECKPOINT_BYTES:
            raise ContextBlocked("context_blocked: 原始上下文超过检查点保留容量，未替换旧检查点")
        with FileLock(directory / "prepare.lock"):
            records = list(directory.glob("cp-*.json"))
            if len(records) >= 256 or size + sum(p.stat().st_size for p in records) > MAX_STORED_BYTES:
                raise ContextBlocked("context_blocked: 检查点存储已满，未删除审查历史")
            if should_stop and should_stop():
                raise Interrupted("用户请求停止，未切换上下文")
            atomic_json(directory / ("cp-" + ident + ".json"), record)
            # Verify the durable candidate before publishing the pointer.
            saved = json.loads(read_text(directory / ("cp-" + ident + ".json")))
            if (fingerprint(saved["candidate"]) != record["candidateVersion"]
                    or fingerprint(saved["original"]) != record["sourceVersion"]
                    or saved.pop("recordVersion", None) != record["recordVersion"]
                    or fingerprint(saved) != record["recordVersion"]):
                raise ContextBlocked("context_blocked: 候选检查点校验失败")
            if should_stop and should_stop():
                raise Interrupted("用户请求停止，保留原上下文且未发送")
            if record.get('workSummary'):
                from .context_view import summary_path
                # Native resume reads this file. Publish its summary and commit
                # identity in ONE atomic replacement, not two independent files.
                atomic_json(summary_path(session),record['workSummary'])
            else:
                # Legacy role/model pointers keep their existing format.
                owner_key=fingerprint(dict(runId=run,role=role,modelVersion=model))[:24]
                atomic_json(directory / ("p-"+owner_key+"-latest.json"),
                            dict(id=ident, candidateVersion=record["candidateVersion"],runId=run,role=role,modelVersion=model))
        if working_view and handoff['strategy']=='tool-result-prune':
            session._context_prune_version = fingerprint(body)
        session.emit("context_compaction", role=role, state="completed", checkpointId=ident,
                     before=before, after=after, strategy=handoff["strategy"],trigger=limit_evidence)
        session.context_checkpoint_refs = dict(getattr(session, "context_checkpoint_refs", {}),
            **{role + ":" + fingerprint(compacted): ident})
        request_only = [
            i for i, spans in getattr(body, "parts", {}).items()
            if body["messages"][i].get("role") == "system"
            and any(kind in {"projectActivity", "memoryGuide"} for kind, _ in spans)
        ]
        # Memory can refresh a stale receipt for the wire request. Validate
        # adoption against its pre-refresh identity, then adopt the fresh text.
        previous_receipts = {
            i: previous for i, message in enumerate(body["messages"])
            if message.get("role") == "tool"
            and (previous := tool_receipts.get(message.get("tool_call_id"))) is not None
            and previous != message
        }
        session.context_adoptions=dict(getattr(session,'context_adoptions',{}),
                                       **{role:dict(record, requestOnly=request_only,
                                                    previousToolReceipts=previous_receipts)})
        if working_view:
            compacted.input_budget = B.report(session, compacted, capacity, history_policy)
        return compacted
    except Interrupted:
        raise
    except (ContextBlocked, OSError, ValueError, TypeError, TimeoutError) as error:
        # Do not expose arbitrary payloads or file exception text to the stream.
        reason = str(error) if isinstance(error, ContextBlocked) else "context_blocked: 检查点未能持久保存，未发送请求且未切换上下文"
        diagnostics = dict(inputBudget=wire.input_budget) if working_view and isinstance(error, ContextBlocked) else {}
        session.emit("context_blocked", role=role, state="blocked", reason=reason,
                     before=before, **diagnostics)
        raise ContextBlocked(reason) from None


def checkpoint_page(directory, offset=0):
    """Bounded inspection metadata only; original contents never cross this API."""
    from . import workbench as W
    if type(offset) is not int or offset < 0 or offset > 256:
        raise W.Problem("invalid_cursor", "上下文检查点游标无效")
    root = Path(directory) / "context-checkpoints"
    if not root.resolve().is_relative_to(Path(directory).resolve()):
        raise W.Problem("path_escape", "上下文检查点目录已变化", 403)
    files = list(root.glob("cp-*.json"))
    if len(files) > 256:
        raise W.Problem("context_capacity", "上下文检查点超过保留边界，未假装列出全部")
    files.sort(key=lambda p:p.stat().st_mtime_ns, reverse=True)
    records = []
    for path in files[offset:offset+20]:
        ident = path.stem[3:]
        if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident) or path.is_symlink():
            raise W.Problem("context_corrupt", "上下文检查点身份无法核对", 409)
        if path.stat().st_size > MAX_CHECKPOINT_BYTES:
            raise W.Problem("context_corrupt", "上下文检查点超出大小限制", 409)
        row = json.loads(read_text(path))
        if (row.get("id") != ident or fingerprint(row.get("candidate")) != row.get("candidateVersion")
                or fingerprint(row.get("original")) != row.get("sourceVersion")
                or fingerprint({k:v for k,v in row.items() if k!='recordVersion'}) != row.get('recordVersion')):
            raise W.Problem("context_changed", "上下文检查点内容已变化，原文件保留", 409)
        records.append(dict(id=ident, runId=row["runId"], role=row["role"], at=row["at"],
            strategy=row["handoff"]["strategy"], before=row["before"], after=row["after"],
            actionReferences=len(row["handoff"].get("historicalActions",[])),
            preservedUserMessages=len(row["handoff"]["userMessages"])-len(row['handoff'].get('retiredUserIndices',[])),
            **(dict(archivedUserMessages=len(row['handoff']['retiredUserIndices']))
               if row['handoff'].get('summaryFormat') else {})))
    return dict(checkpoints=records, checkpointOffset=offset,
                checkpointsHasMore=offset+20<len(files), totalCheckpoints=len(files))


def is_history_reference(message):
    return (message.get("role") == "assistant" and not message.get("tool_calls")
            and isinstance(message.get("content"), str)
            and message["content"].startswith('{"historicalContext": true, "checkpointId":'))


def assess(body, capacity=0, *, groups=None):
    """Account for the actual serialized input, tools, and output reservation."""
    if type(capacity) is not int or capacity < 0:
        raise ValueError("context capacity must be a non-negative integer")
    messages = body.get("messages", [])
    tools = body.get("tools", [])

    def size(value):
        # UTF-8 bytes / 2 is deliberately conservative for mixed code/CJK.
        return len(json.dumps(value, ensure_ascii=False,
                              separators=(",", ":")).encode("utf8"))

    def estimate(value):
        return (size(value) + 1) // 2

    # Base64 image bytes are transport data, not ordinary text tokens. Keep
    # the request untouched and reserve a conservative media allowance in a
    # separate shadow used only for diagnostics. This is not vendor counting.
    shadow = []
    media_by_message = []
    image_tokens = image_count = 0
    for message in messages:
        before_media = image_tokens
        content = message.get("content")
        if not isinstance(content, list):
            shadow.append(message)
            media_by_message.append(0)
            continue
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                image_count += 1
                url = part.get("image_url", {})
                url = url.get("url", "") if isinstance(url, dict) else str(url)
                image_tokens += max(8192, math.ceil(len(url) / 32))
                parts.append(dict(type="image_url", image_url="<media reserve>"))
            else:
                parts.append(part)
        shadow.append(dict(message, content=parts))
        media_by_message.append(image_tokens - before_media)
    message_tokens = estimate(shadow) + image_tokens
    system_messages = [msg for msg in shadow if msg.get("role") in {"system", "developer"}]
    system_tokens = estimate(system_messages) if system_messages else 0
    tool_tokens = estimate(tools) if tools else 0
    envelope = estimate({k: v for k, v in body.items() if k not in {"messages", "tools"}})
    protocol = envelope + 16 * len(messages)
    requested = body.get("max_completion_tokens", body.get("max_tokens", 0))
    output = requested if type(requested) is int and requested > 0 else 0
    reserve_known = output > 0
    # A diagnostic reserve for tool bursts and estimator error; not a hard cap.
    safety = max(4096, math.ceil(capacity * .05)) if capacity else 4096
    total = message_tokens + tool_tokens + protocol + output + safety
    # Partition the same serialized bytes used by the capacity estimator.
    # Annotations only count spans actually present in this prepared message.
    buckets = {"protocol": 2 + max(0, len(shadow) - 1)}
    group_bytes = dict(fixed=buckets["protocol"], current=0, history=0)
    group_reserves = dict.fromkeys(group_bytes, 0)
    for index, message in enumerate(shadow):
        role = message.get("role")
        default = ("system" if role in {"system", "developer"} else
                   "toolResults" if role == "tool" or message.get("tool_calls") else "dialogue")
        remaining = size(message)
        if groups is not None:
            group = groups[index]
            group_bytes[group] += remaining
            group_reserves[group] += media_by_message[index] + 16
        content = message.get("content")
        parts = getattr(body, "parts", {}).get(index, ())
        for kind, start, end in content_spans(content, parts):
            span = size(content[start:end]) - 2
            remaining -= span
            if groups is not None and kind == "runtimeUpdate" and runtime_update_name(message, parts) == "runtime-facts":
                from .runtime_facts import plugin_fact_bytes
                plugin_bytes = min(span, plugin_fact_bytes(content[start:end], size))
                buckets["pluginFacts"] = buckets.get("pluginFacts", 0) + plugin_bytes
                span -= plugin_bytes
            buckets[kind] = buckets.get(kind, 0) + span
        buckets[default] = buckets.get(default, 0) + remaining
    breakdown = {}
    accounted = 0
    for kind, count in buckets.items():
        breakdown[kind] = (accounted + count + 1) // 2 - (accounted + 1) // 2
        accounted += count
    breakdown["protocol"] += protocol
    breakdown.update(toolDefinitions=tool_tokens, attachments=image_tokens,
                     reservedOutput=output, safetyReserve=safety)
    result = dict(version=1, estimated=True, method="utf8_bytes_div_2_with_media_reserve",
                inputTokens=message_tokens, systemTokens=system_tokens, toolTokens=tool_tokens,
                imageTokens=image_tokens, imageCount=image_count,
                protocolTokens=protocol, reservedOutput=output,
                outputReservationKnown=reserve_known, safetyReserve=safety,
                estimatedTotal=total, capacity=capacity or None, breakdown=breakdown,
                state="unknown" if not capacity or not reserve_known else
                "pressure" if total >= capacity else "within_estimate")
    if groups is not None:
        result["inputGroups"] = {}
        seen = 0
        for group, count in group_bytes.items():
            result["inputGroups"][group] = (seen + count + 1) // 2 - (seen + 1) // 2 + group_reserves[group]
            seen += count
        result["inputGroups"]["fixed"] += tool_tokens + envelope
    return result
