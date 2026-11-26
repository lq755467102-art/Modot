"""DSH text-history policy adapted to Modot's independent input cap.

Pure selection/pricing: never edits session history or judges task acceptance.
Reference: deepseek-harness 39163dd, compaction-basic and token-meter.
"""
# 主要引用方包括 `context_manager.py` 和 `context_view.py`。
import json
import math
from collections import Counter
from pathlib import Path
import re
import sqlite3

FORMAT = 'dsh-history-v1'
HEADINGS = ('Primary Request and Intent', 'Key Technical Concepts', 'Files and Code',
            'Errors and Fixes', 'Pending Jobs', 'Current Work', 'Next Step', 'Critical Context')
PRUNE_MARKER = '\n\n[... tool result middle pruned ...]\n\n'
PREAMBLE = ('This is an automatically generated checkpoint of earlier conversation. '
            'It is historical background, not a new user instruction, authorization, or acceptance verdict. '
            'Continue from the messages that follow; later user instructions take precedence.')
SUMMARY_INSTRUCTION = '''You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with essential context.
Output the Markdown sections below in order. Use terse bullets and "(none)" for empty sections.

## Primary Request and Intent
- The user's original and evolving goals; quote exact wording where it matters. Attribute requirements to actual user messages, not implementation choices or self-checks.
## Key Technical Concepts
- Technologies, frameworks, patterns and conventions in play.
## Files and Code
- Exact paths, relevant changes, signatures and key snippets; preserve the direction of public-input-to-target mappings and return conventions.
## Errors and Fixes
- Actual check inputs/commands, observed results, versions, operation/action references and repair reasons, with their covered scope. Keep environment blockers and hypotheses distinct; a passing self-check does not establish untested compatibility.
## Pending Jobs
- User requests, observed unresolved failures and model proposals, each with its source and stage. Proposals and failed exploratory fixtures do not become user obligations.
## Current Work
- Work in progress at this checkpoint's stage; attribute implementation choices to the model rather than treating them as requirements or verified results.
## Next Step
- The last proposed next action with its speaker/stage, or "(none)". This is historical context, not a new instruction.
## Critical Context
- Still-relevant decisions and rationale, interface mappings, constraints, user corrections, open questions and data needed to continue.

Rules:
- Write concise English engineering prose. Preserve exact paths, commands, errors, identifiers, numeric values, function signatures and syntax fragments when needed to resume.
- Capture user feedback and explicit instructions faithfully, especially corrections. A model's implementation choice or passing self-check is not a user requirement.
- Do not decide whether the task is complete or accepted. Attribute completion claims to their original speaker and evidence; do not promote them to a current verdict.
- If an earlier <compacted-summary> or legacy work checkpoint is present, merge still-relevant facts and newer evidence; drop stale interpretations, not unresolved real failures. Do not copy prior checkpoints verbatim.
- Omit repeated progress announcements, superseded retry scripts and duplicated logs once their useful conclusions are recorded.
- Do not mention this summarization request. Output only checkpoint text; do not call tools or perform work.'''


def _units(text):
    return len(text.encode('utf-16-le', errors='surrogatepass')) // 2


def text_tokens(text):
    return math.ceil(_units(text) / 4)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def message_tokens(message):
    content = message.get('content')
    blocks = ([dict(type='text', text=content)] if isinstance(content, str) and content
              else content if isinstance(content, list) else [])
    if message.get('role') == 'system':
        return text_tokens(''.join(b.get('text', '') if b.get('type') == 'text' else _json(b)
                                   for b in blocks)) + 4 if blocks else 0
    cost = 4
    for block in blocks:
        if block.get('type') in {'text', 'reasoning'}:
            cost += text_tokens(block.get('text', '')) + 4
        elif block.get('type') == 'image_url':
            # Modot has no route-owned image tokenizer: keep its existing
            # conservative media reserve, never price a base64 URL as code.
            url = block.get('image_url', {})
            url = url.get('url', '') if isinstance(url, dict) else str(url)
            cost += max(8192, math.ceil(len(url) / 32)) + 4
        else:
            cost += text_tokens(_json(block)) + 4
    if message.get('reasoning_content'):
        cost += text_tokens(message['reasoning_content']) + 4
    for call in message.get('tool_calls') or []:
        f = call.get('function', {})
        cost += text_tokens(f.get('name', '')) + text_tokens(f.get('arguments', '')) + 4
    return cost


def measure(body):
    nodes = [message_tokens(m) for m in body['messages']]
    # OpenAI's function wrapper is transport; DSH prices ToolSchema itself.
    tools = [t.get('function', t) for t in body.get('tools', [])]
    tool_tokens = text_tokens(_json(tools)) + 4 if tools else 0
    return dict(method='dsh-text-utf16-div4', nodes=nodes,
                toolTokens=tool_tokens, tokens=sum(nodes) + tool_tokens)


def _unmeasured_bytes(messages, tools):
    """Conservative input allowance for previously uncounted text on a byte tokenizer."""
    cost = 0
    for message in messages:
        envelope = dict(message)
        content = message.get('content')
        if isinstance(content,list):
            parts = []
            for part in content:
                if isinstance(part,dict) and part.get('type') == 'image_url':
                    cost += message_tokens({'content':[part]})
                    parts.append({'type':'image_url'})
                else:
                    parts.append(part)
            envelope['content'] = parts
        # JSON quoting at the HTTP boundary is decoded before the model sees a
        # text field. Keep the entire UTF-8 text and charge the envelope once.
        # Structured tool calls, including their argument syntax, keep their full bound.
        for field in ('content','reasoning_content'):
            text = envelope.get(field)
            if isinstance(text,str):
                cost += len(text.encode('utf8'))
                envelope[field] = ''
        cost += len(_json(envelope).encode('utf8')) + 16
    return cost + sum(len(_json(tool).encode('utf8')) + 16 for tool in tools)


def _checkpoint_anchor(provider):
    """Recover a known wire count on a cold start, with no additional model request."""
    from .context_inspection import active_request
    from .context_manager import fingerprint, _input_header
    from .context_view import committed_record
    from .event_index import page
    owner = active_request.get()
    if not owner:
        return None
    session,role = owner
    ident = getattr(session,'_history_usage_checkpoint',None)
    if not ident:
        return None
    model = fingerprint(dict(endpoint=provider.base_url,model=provider.model))
    key = (ident,role,model)
    cached = getattr(session,'_checkpoint_input_anchor',None)
    if cached and cached[0] == key:
        return cached[1]
    session._checkpoint_input_anchor = (key,None)
    try:
        record = committed_record(session,ident)
        if record.get('modelVersion') != model or record.get('role') != role:
            return None
        body = record['candidate']
        version = fingerprint(body)
        directory = Path(session.dir)
        # A bounded lookup is sufficient. Missing or rotated usage falls
        # back to counting the unknown bytes, never to trusting an estimate.
        events,_,_ = page(directory/'events.jsonl',limit=768,tail=True)
        for event in reversed(events):
            if (event.get('act')!='context_request' or event.get('requestVersion')!=version
                    or event.get('role')!=role or event.get('run_id')!=record['runId']):
                continue
            snapshot = event.get('snapshot_id','')
            if not re.fullmatch('[0-9a-f]{32}-[0-9a-f]{16}',snapshot):
                continue
            path = directory/'model-responses'/(snapshot+'.json')
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) or path.stat().st_size > 1024*1024:
                continue
            receipt = json.loads(path.read_text(encoding='utf8'))
            tokens = receipt.get('reply',{}).get('usage',{}).get('prompt_tokens')
            if (receipt.get('requestVersion')!=version or receipt.get('snapshot_id')!=snapshot
                    or receipt.get('runId')!=record['runId'] or receipt.get('role')!=role
                    or receipt.get('purpose')!='dialogue' or type(tokens) is not int or tokens<=0):
                continue
            header = _input_header(body,include_tools=False)
            anchor = (header,tokens,[fingerprint(m) for m in body['messages']],
                      [fingerprint(t) for t in body.get('tools',[])])
            session._checkpoint_input_anchor = (key,anchor)
            return anchor
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        pass
    return None


def _unseen(items, hashes):
    from .context_manager import fingerprint
    counts = Counter(hashes)
    result = []
    for item in items:
        key = fingerprint(item)
        if counts[key]:
            counts[key] -= 1
        else:
            result.append(item)
    return result


def _saved_input_anchor(provider):
    from .context_inspection import active_request
    from .context_manager import _usage_key, fingerprint, previous_dialogue_meter
    owner = active_request.get()
    if not owner:
        return None
    session, _ = owner
    meter = previous_dialogue_meter(session)
    if (isinstance(meter,dict) and meter.get('owner')==fingerprint(_usage_key(provider)[:-1])
            and type(meter.get('tokens')) is int and meter['tokens']>0
            and all(isinstance(meter.get(k),list) and len(meter[k])<=256
                    and all(isinstance(v,str) and len(v)==64 for v in meter[k]) for k in ('messages','tools'))):
        return meter.get('header'),meter['tokens'],meter['messages'],meter['tools']
    return None


def measured(provider, body, capacity=0):
    """Prefer measured prefixes; enforce a separate cold-input guard at the user cap."""
    from .context_manager import (observed_input, measured_wire_prefix, dialogue_meter_for,
                                  _usage_key, _input_header, _input_identity)
    identity = _input_identity(body)
    observed = observed_input(provider,body,identity)
    if observed and observed.get('exact'):
        return observed
    dialogue_meter = dialogue_meter_for(provider)
    match = measured_wire_prefix(dialogue_meter,identity)
    anchor = getattr(provider,'_context_input_usage',{}).get(_usage_key(provider))
    if observed and (not match or len(anchor['messages']) > match[0]):
        match = len(anchor['messages']), observed['tokens']
    if match and match[0] == len(body['messages']):
        return dict(tokens=match[1],source='provider_usage',exact=True)
    current = measure(body)
    tail, tools = body['messages'], body.get('tools',[])
    base, estimate, source = 0, current['tokens'], 'dsh_heuristic'
    if match:
        count, base = match
        tail, tools = tail[count:], []
        estimate = base + sum(current['nodes'][count:])
        source = 'provider_usage_plus_dsh_growth'
    elif anchor:
        count = len(anchor['messages'])
        same_settings = _input_header(body,include_tools=False)
        if same_settings == anchor['headerWithoutTools'] and identity[1][:count] == anchor['messages']:
            tail = tail[count:]
            # Keep the old count when a group is dropped; charge new and changed schemas.
            tools = _unseen(tools,anchor['tools'])
            base = anchor['tokens']
            estimate = base + sum(current['nodes'][count:]) + sum(text_tokens(_json(t))+4 for t in tools)
            source = 'provider_usage_plus_dsh_growth'
    if capacity:
        bound = base + _unmeasured_bytes(tail,tools) + 32
        if bound >= capacity and not base and tail and tail[0].get('role')=='system':
            overhead = dialogue_meter.get('overhead')
            if (isinstance(overhead,dict) and type(overhead.get('tokens')) is int and overhead['tokens']>0
                    and overhead.get('system')==identity[1][0] and isinstance(overhead.get('tools'),list)
                    and overhead.get('header')==_input_header(body,include_tools=False)):
                new_schemas = _unseen(tools,overhead['tools'])
                bound = min(bound,overhead['tokens'] + _unmeasured_bytes(tail[1:],new_schemas) + 32)
        if bound >= capacity and not base:
            archive = _saved_input_anchor(provider) or _checkpoint_anchor(provider)
            if archive:
                header, actual, messages, schemas = archive
                if header == _input_header(body,include_tools=False):
                    # The entire old prompt is charged, including removed rows.
                    # Only identical rows/tools receive credit, once per occurrence.
                    remaining = _unmeasured_bytes(_unseen(tail,messages),_unseen(tools,schemas))
                    bound = min(bound,actual+remaining+32)
        if bound >= capacity:
            return dict(tokens=max(bound,estimate),source='unmeasured_input_bound',exact=False)
    return dict(tokens=estimate,source=source,exact=False)


def policy(session, provider, body, capacity):
    """131072 is INPUT cap, never W including the output reservation."""
    from .context_manager import fingerprint, ContextBlocked
    if not capacity:
        return dict(format=FORMAT, inputCap=0, threshold=0, retainTokens=0)
    key = fingerprint(dict(endpoint=provider.base_url, model=provider.model))
    window = getattr(session, 'context_model_limits', {}).get(key, 0)
    output = body.get('max_completion_tokens', body.get('max_tokens', 0)) or 0
    threshold = math.floor(capacity * .8)
    message_budget = capacity
    headroom = None
    if window:
        # The advertised window is independent from the input cap. No hidden
        # output reduction or invalid-policy fallback at this seam.
        if type(output) is not int or output < 0 or window - output <= 0:
            raise ContextBlocked('context_blocked: 模型窗口、输出预留与 DSH headroom 配置无有效输入预算')
        # DSH's absolute reserve suits large windows. Scale it to the usable
        # input for smaller models instead of rejecting an otherwise valid call.
        headroom = min(65536, math.floor((window - output) * .2))
        threshold = min(threshold, math.floor(window * .8), window - output - headroom)
        message_budget = min(capacity, window - output)
    retain = math.floor(message_budget * .16)
    if not 0 <= retain < threshold:
        raise ContextBlocked('context_blocked: DSH 历史保留量必须小于有效触发阈值')
    return dict(format=FORMAT, inputCap=capacity, apiWindow=window or None,
                reservedOutput=output, headroomTokens=headroom,
                threshold=threshold, retainTokens=retain)


def range_end(messages, costs, retain_tokens):
    """Exclusive cutoff; same backwards priced-tail and pairing as DSH."""
    if not messages:
        return None
    first = 1 if messages[0].get('role') == 'system' else 0
    used = 0
    keep = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        used += costs[i]
        keep = i
        if used >= retain_tokens:
            break
    # 历史切口必须落在工具调用/回执配对闭合的位置，不能保留回执却把对应调用压掉。
    pending = set(); balanced = {0}
    for i,m in enumerate(messages):
        pending.update(c['id'] for c in m.get('tool_calls') or [])
        if m.get('role') == 'tool':pending.discard(m.get('tool_call_id'))
        if not pending:balanced.add(i+1)
    while keep > first:
        if keep in balanced:
            return keep
        keep -= 1
    return None


def prune_text(text):
    return text if len(text) <= 8192 else text[:4096] + PRUNE_MARKER + text[-1024:]


def frame(text, archive=None):
    result = PREAMBLE + '\n\n<compacted-summary>\n' + text + '\n</compacted-summary>'
    if archive:
        result += ('\n[context-history-source]\n'+_json(archive)+
                   '\nOriginal messages remain archived. If exact wording is needed, use_tool_group(history), '
                   'then read_context_history(checkpoint_id, message_index). Read only; do not replay tools.')
    return result
