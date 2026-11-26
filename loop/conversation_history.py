"""Discover retained, role-local evidence without replaying the action or reading its output."""
# 主要引用方包括 `tools.py`。
import json
from . import context_archive as archive, event_index, evidence, projects, workbench as W
from .context_manager import fingerprint
from .evidence_redaction import redact, terminal_command


ACTION_LABELS = ('relative_path', 'path', 'label', 'name', 'purpose', 'scope_reason')


def _action(row, records, thread, root, directory, allowed, upper, role, scopes, needle):
    tool = row.get('tool')
    if row.get('act') == 'ask_human' and not row.get('action_id'):
        return dict(kind='action', tool='ask_human', runId=row.get('run_id'), seq=row.get('seq'),
                    at=row.get('at'), state='question_recorded', access='record_only')
    if not isinstance(tool, str) or row.get('act') not in {'tool', 'action', 'not_run', 'needs_human'}:
        return None
    run, aid = row.get('run_id'), row.get('action_id')
    item = dict(kind='action', tool=tool, runId=run, seq=row.get('seq'), at=row.get('at'), state=row.get('state'))
    if not run or not isinstance(aid, str) or not evidence.ACTION_ID.fullmatch(aid):
        return dict(item, access='record_only')
    record = records.get(aid)
    refs = (record or {}).get('sourceRefs') or []
    if (not refs or refs[0]['offset'] != row['sourceOffset'] or record.get('role') != role
            or record.get('runId') != run):
        return None
    if refs[-1]['offset'] >= upper:
        raise W.Problem('evidence_changed', '此页历史动作已更新，请重新查找', 409)
    item.update(actionId=aid, state=record['state'])
    if record.get('sourceUnverified'):
        return dict(item, access='source_unverified')
    args = record.get('input')
    try:
        evidence.check_history_scope(thread['id'], root, record, allowed, scopes=scopes, archive_dir=directory)
    except W.Problem as error:
        item['historicalState'] = item.pop('state')
        return dict(item, access=error.code, accessMessage=redact(error.message))
    ref = dict(action_id=aid, run_id=run, evidence_revision=evidence.revision(record))
    retained = record.get('output') or {}
    has_output = retained.get('state') == 'retained' and retained.get('id') == aid
    if has_output:
        output_file = W.file_path({'path': str(directory)}, 'evidence-output/' + aid + '.json')
        has_output = output_file.is_file()
    item.update(access='available' if has_output else 'output_missing',
                read=dict(tool='read_action_evidence', arguments=ref if has_output else dict(ref, view='input')))
    # Only labels leave discovery. Matching retained input requires the same grant as reading it.
    for field in ACTION_LABELS:
        value = args.get(field)
        if isinstance(value, str) and value:
            item[field] = redact(value)[:160]
    if needle:
        for field in ('script', 'command', 'argv'):
            value = args.get(field)
            visible = terminal_command(value) if isinstance(value, list) else redact(value)
            if isinstance(visible, str) and needle in visible.casefold():
                item['matchedField'] = field
                break
    return item


def _checkpoint(row, root, directory, thread, allowed, role):
    ident = row.get('checkpointId')
    item = dict(kind='checkpoint', runId=row.get('run_id'), seq=row['seq'], at=row.get('at'))
    try:
        record = archive._archive_record(directory, ident, role)
        if record.get('runId') != item['runId']:
            return dict(item, access='evidence_changed')
        messages = record['original']['messages']
        span = (record.get('workSummary') or {}).get('archive') or {}
        groups = record.get('handoff', {}).get('retiredGroups', [])
        if groups:
            first = groups[0]['firstIndex']
            archive._checked_message(root, directory, thread, role, ident, first, allowed, set(), record=record)
            return dict(item, access='available', firstIndex=first, endExclusive=groups[-1]['endExclusive'],
                        retiredGroupCount=len(groups), retiredRanges=[
                            dict(firstIndex=g['firstIndex'], endExclusive=g['endExclusive']) for g in groups[:20]],
                        read=dict(tool='read_context_history', arguments=dict(checkpoint_id=ident, message_index=first)))
        end = min(len(messages), span.get('end_message_index', len(messages)))
        retired = set(record.get('handoff', {}).get('retiredUserIndices', []))
        index = next((i for i in range(end) if messages[i].get('role') in {'assistant', 'tool'} or i in retired), None)
        if index is None:
            return dict(item, access='record_only')
        archive._checked_message(root, directory, thread, role, ident, index, allowed, set(), record=record)
        return dict(item, access='available', firstIndex=index, endExclusive=end,
                    read=dict(tool='read_context_history', arguments=dict(checkpoint_id=ident, message_index=index)))
    except W.Problem as error:
        return dict(item, access=error.code)


def search(root, contains='', kind='all', run_id=None, cursor=None, access=None):
    owner, thread, directory = archive._owned_history(root)
    role = owner.get('role') or 'executor'
    if (not isinstance(contains, str) or len(contains)>120 or kind not in ('all','action','dialogue','checkpoint')
            or (run_id is not None and (not isinstance(run_id,str) or len(run_id)>128))
            or (access is not None and (not isinstance(access, str) or not 0 < len(access) <= 80))):
        raise W.Problem('invalid_filter', '历史筛选条件无效')
    filters = [thread['id'], role, kind, contains, run_id, owner['runId']]
    if access is not None:
        filters.append(access)
    query = fingerprint(filters)
    events = directory / 'events.jsonl'
    upper = events.stat().st_size if events.is_file() else 0
    before = upper
    if cursor is not None:
        if (not isinstance(cursor, dict) or cursor.get('query') != query
                or type(cursor.get('before')) is not int or type(cursor.get('upper')) is not int
                or not 0 < cursor['before'] <= cursor['upper'] <= upper
                or cursor.get('version') != fingerprint([query, cursor.get('generation'),
                                                         cursor['before'], cursor['upper']])):
            raise W.Problem('invalid_cursor', '历史游标与当前查询不符')
        before, upper = cursor['before'], cursor['upper']
    needle = contains.strip().casefold()
    # A pasted action reference has a verified byte position in the existing projection.
    # Other queries keep chronological pagination over the public event stream.
    by_id = kind in ('action', 'all') and evidence.ACTION_ID.fullmatch(needle) and cursor is None
    if by_id:
        with evidence.projection(directory) as (db, state):
            found = db.execute('SELECT data FROM actions WHERE id=? AND role=?', (needle, role)).fetchone()
            if found:
                record = json.loads(found[0])
                refs = record.get('sourceRefs') or []
                if refs and not record.get('sourceUnverified'):
                    evidence._verify_source(directory, record, db)
                    before = min(before, refs[0]['offset'] + 1)
                else:
                    by_id = False
            else:
                by_id = False
    generation = cursor.get('generation') if cursor else None
    gap = catching_up = False
    allowed = projects.history_read_resources(thread['id'])
    scopes, items = {}, []
    # The event index already limits bytes per physical page. Bound sparse searches as well.
    for _ in range(8):
        rows, _, meta = event_index.page(events, tail=True, limit=256, before_position=before)
        if generation is not None and generation != meta['generation']:
            raise W.Problem('evidence_changed', '历史索引已改变，请从首页重新查找', 409)
        generation = meta['generation']
        before = rows[0]['sourceOffset'] if rows else 0
        rows = [row for row in rows if not (row.get('agent_id') or row.get('childId'))
                and archive.own_public_role(row, role)
                and (row.get('run_id') == run_id if run_id else row.get('run_id') != owner['runId'])]
        for position, item in _matches(rows, kind, needle, root, thread, directory, allowed, upper, role, scopes, meta):
            if access is not None and item.get('access') != access:
                continue
            items.append(item)
            if len(items) == 12:
                before = position
                break
        gap |= meta['gap']
        catching_up |= meta['catchingUp']
        if not before or by_id or len(items) == 12:
            break
    next_page = None
    if before and not by_id:
        next_page = dict(query=query, generation=generation, before=before, upper=upper,
                         version=fingerprint([query, generation, before, upper]))
    return dict(ok=True, items=items, nextCursor=next_page, hasMore=next_page is not None,
                catchingUp=catching_up, gap=gap)


def _matches(rows, kind, needle, root, thread, directory, allowed, upper, role, scopes, meta):
    records = {}
    ids = {r['action_id'] for r in rows if kind in ('all', 'action') and isinstance(r.get('action_id'), str)}
    if ids:
        with evidence.projection(directory) as (db, state):
            params = ','.join('?' for _ in ids)
            records = {aid: json.loads(data) for aid, data in
                       db.execute(f'SELECT id,data FROM actions WHERE id IN ({params})', tuple(ids))}
            for record in records.values():
                if not record.get('sourceUnverified'):
                    evidence._verify_source(directory, record, db)
                record['conversationId'] = thread['id']
            meta['catchingUp'] = meta['catchingUp'] or state.get('catchingUp', False)
    for row in reversed(rows):
        position = row['sourceOffset']
        item = (_action(row, records, thread, root, directory, allowed, upper, role, scopes, needle)
                if kind in ('all', 'action') else None)
        match = ''
        if item:
            match = ' '.join(str(item.get(k) or '') for k in (
                'tool', 'actionId', 'runId', 'state', 'historicalState', 'access', 'accessMessage', *ACTION_LABELS,
            ))
        elif (kind in ('all', 'checkpoint') and row.get('act') == 'context_compaction'
              and row.get('state') == 'completed'):
            match = 'checkpoint 压缩检查点'
            if needle and needle not in match:
                continue
            item = _checkpoint(row, root, directory, thread, allowed, role)
        elif kind in ('all', 'dialogue') and (turn:=archive.public_turn(row, 160)):
            if not archive._reply_visible(row, directory, allowed, scopes):
                continue
            match, truncated = archive.dialogue_source(row)
            item = dict(kind='dialogue', role=turn['role'], runId=row.get('run_id'), seq=row['seq'],
                        at=row.get('at'), excerpt=match[:160], sourceTruncated=truncated,
                        read=dict(tool='read_conversation_dialogue', arguments=dict(position=position,
                            message_version=fingerprint([thread['id'], position, row['seq'], match]))))
        if item and (not needle or item.get('matchedField') or needle in match.casefold()):
            yield position, item
