"""Project each receipt using earlier observations. Originals stay in action evidence."""
# 主要引用方包括 `context_manager.py`。
from copy import copy
import json
from pathlib import Path

from .tool_results import command_summary


READS = {'read_project_resource', 'inspect_code'}
CHECKS = {'run_project_check', 'read_check_output'}
WRITES = {'edit_project_file', 'write_project_file', 'create_project_file'}


def _listing(value, resource, ref, listings):
    rows = value.get('entries')
    if (not isinstance(rows, list) or not rows or not value.get('listingRevision')
            or value.get('ok') is False or value.get('complete') is False
            or any(value.get(k) for k in ('error', 'errors', 'truncated', 'hasMore', 'after', 'next'))):
        return None
    parent = value.get('path')
    if not isinstance(parent, str):
        return None
    prefix = '' if parent in ('', '.') else parent.rstrip('/') + '/'
    names = []
    for item in rows:
        if not isinstance(item, dict) or set(item) != {'name', 'path', 'directory'}:
            return None
        name = item['name']
        if not isinstance(name, str) or item['path'] != prefix + name or type(item['directory']) != bool:
            return None
        names.append(name + ('/' if item['directory'] else ''))
    out = {k:v for k,v in value.items() if k != 'entries'}
    out['entryCount'] = len(names)
    key = (resource, parent, out['listingRevision'], tuple(names)) if isinstance(out['listingRevision'], str) else None
    earlier = listings.setdefault(key, ref) if key is not None else ref
    if earlier != ref:
        out['directoryCount'] = sum(r['directory'] for r in rows)
        out['sameListingAs'] = earlier
        out['entriesView'] = '同一目录修订的清单已在先前回执交付。'
    else:
        out['names'] = names
        out['entriesView'] = '相对于 path，目录名以 / 结尾。'
    return out


# 在上下文组装阶段把工具调用和回执按原调用 ID 对齐，再识别本轮可合并的投影。
def for_request(session, body, *, reuse=True):
    from . import projects, workbench as W
    from .subtasks import validate_child

    calls, rows = {}, []
    for i, msg in enumerate(body['messages']):
        batch = msg.get('tool_calls') or []
        if batch:
            for call in batch:
                fn = call.get('function') or {}
                calls[call.get('id')] = (i, len(batch), fn)
        if msg.get('role') != 'tool' or msg.get('tool_call_id') not in calls:
            continue
        group, count, fn = calls[msg['tool_call_id']]
        name = fn.get('name')
        if name not in READS | CHECKS | WRITES:
            continue
        try:
            value = json.loads(msg['content'])
            args = json.loads(fn.get('arguments') or '{}')
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict) or not isinstance(args, dict):
            continue
        ref = value.get('_harnessEvidence')
        if not isinstance(ref, dict) or not ref.get('actionId') or ref.get('runId') != session.run_id:
            continue
        if name in READS and not isinstance(value.get('entries'), list):
            continue
        if isinstance(args.get('resource_id'), str):
            rows.append((i, group, count, name, args['resource_id'], value, ref))
    if not rows:
        return body
    ident = Path(session.dir).name
    try:
        readable = set(projects.history_read_resources(ident))
        for resource in readable.copy() & {r[4] for r in rows}:
            try:
                validate_child(ident, resource)
            except W.Problem:
                readable.discard(resource)
    except W.Problem:
        readable = set()

    replacements, listings, duplicates, write_batches = {}, {}, {}, {}
    for i, group, count, name, resource, value, ref in rows:
        out = None
        if resource not in readable:
            out = dict(state='scope_changed', _harnessEvidence=ref)
        elif reuse:
            if name in READS:
                out = _listing(value, resource, ref, listings)
            elif name in CHECKS:
                operation, version = value.get('operationId'), value.get('receiptVersion')
                if (isinstance(operation, str) and operation and isinstance(version, str) and version
                        and not value.get('sameCheckFacts')):
                    key = (resource, operation, version)
                    page_fields = ('text', 'after', 'next', 'hasMore', 'truncated')
                    page = tuple(value.get(k) for k in (*page_fields, 'stream'))
                    if (not any(value.get(k) for k in ('error', 'errors', 'query', 'matches'))
                            and all(v is None or isinstance(v, (str, int)) for v in page)):
                        # Empty polls repeat terminal facts. Log pages also need identical
                        # bytes and cursor, even when a different page intervenes.
                        page_key = (key, page) if any(k in value for k in page_fields) else key
                        earlier = duplicates.setdefault(page_key, ref)
                        if earlier != ref:
                            out = dict(operationId=operation, receiptVersion=version,
                                       sameCheckFacts=earlier, _harnessEvidence=ref)
            elif count > 1 and value.get('ok') is True and value.get('revision') and value.get('path'):
                # Every individual write retains its target, version and undo identity.
                # Deduplicate only identical baseline limitations in the same batch.
                shared = ('projectBaseline', 'historyCoverage', 'projectBaselineReason')
                key = (group, resource, tuple(value.get(k) for k in shared))
                if key in write_batches:
                    out = {k:v for k,v in value.items() if k not in shared}
                    out['sameBaselineAs'] = write_batches[key]
                else:
                    write_batches[key] = ref['actionId']
        # Compaction also consumes a full, non-deduplicated source view. Give it
        # the same distinction between an exit code and a test report.
        if out is None and name in CHECKS and (summary := command_summary(value)) and summary != value.get('summary'):
            out = dict(value, summary=summary)
        if out is not None:
            text = json.dumps(out, ensure_ascii=False)
            if (resource not in readable or name in CHECKS and 'summary' in out
                    or len(text.encode()) < len(body['messages'][i]['content'].encode())):
                replacements[i] = text
    if not replacements:
        return body
    result = copy(body)
    result['messages'] = list(body['messages'])
    if hasattr(body, 'parts'):
        result.parts = dict(body.parts)
    for i, content in replacements.items():
        result['messages'][i] = dict(result['messages'][i], content=content)
        if hasattr(result, 'parts'):
            result.parts.pop(i, None)
    return result
