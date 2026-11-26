"""Role-local corrections to model notes; never edit requirements or receipts."""
# 主要引用方包括 `working_context.py` 和 `context_view.py`。
from copy import deepcopy
import json
from pathlib import Path
import re

from .context_manager import fingerprint, MAX_CHECKPOINT_BYTES
from .filelock import atomic_json, read_text

NOTICE = ('模型对自己旧判断的撤回／替代，不是用户要求、授权或验收。'
          '替代内容仍是有来源的模型自述；原源码、脚本、失败回执保持原状。')


def _invalid(message='更正引用、来源或所属任务无法核对；保留原记录'):
    from .workbench import Problem
    raise Problem('invalid_correction', message)


def _version(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)


def _file(session, role, version):
    if not _version(version) or role not in {'executor', 'planner', 'reviewer'}:
        _invalid()
    path = Path(session.dir) / 'working-summaries' / (role + '-records') / (version + '.json')
    if not path.resolve().is_relative_to(Path(session.dir).resolve()) or path.is_symlink():
        _invalid()
    return path


def note_record(row):
    return {k: v for k, v in row.items() if k not in {'corrections', 'summaryVersion'}}


def archive_note(session, role, row):
    """Use the existing role store and atomic writer, not a second state database."""
    record = note_record(row)
    version = fingerprint(record)
    path = _file(session, role, version)
    if path.is_file():
        if json.loads(read_text(path)) != record:
            _invalid('工作记录原版本已变化；未覆盖原文')
    else:
        atomic_json(path, record)
    return version


def load(session, role):
    from .working_context import summary_path, note_scope
    path = summary_path(session, role)
    if not path.is_file():
        return {}
    row = json.loads(read_text(path))
    if (row.get('task') == getattr(session, 'task_input', {}).get('id')
            and row.get('role') == role
            and row.get('scope') == note_scope(session, native=row.get('scopeKind') == 'task' and role == 'executor')):
        return row
    return {}


def _target_text(session, role, target, previous, records):
    """Resolve only immutable own statements; tool outputs/user text are not targets."""
    from .working_context import note_scope
    if set(target) == {'summary_version', 'quote'}:
        version = target['summary_version']
        path = _file(session, role, version)
        if path.is_file() and path.stat().st_size <= MAX_CHECKPOINT_BYTES:
            row = json.loads(read_text(path))
        elif previous and fingerprint(note_record(previous)) == version:
            row = note_record(previous)
        else:
            _invalid('工作记录版本不存在；请使用回执中的 summaryVersion')
        if (fingerprint(row) != version or row.get('task') != getattr(session, 'task_input', {}).get('id')
                or row.get('role') != role
                or row.get('scope') != note_scope(session, native=row.get('scopeKind') == 'task' and role == 'executor')):
            _invalid()
        return row.get('summary', ''), None
    if set(target) == {'correction_id', 'quote'}:
        old = next((r for r in records if r['id'] == target['correction_id']), None)
        if old is None or 'replacement' not in old:
            _invalid('只能定位本任务已有替代自述；撤回本身没有新结论可撤回')
        return old['replacement'], None
    if set(target) == {'checkpoint_id', 'message_index', 'message_version', 'quote'}:
        from .context_view import scope_key
        ident, index = target['checkpoint_id'], target['message_index']
        if (not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{32}', ident)
                or type(index) is not int or not _version(target['message_version'])):
            _invalid()
        path = Path(session.dir) / 'context-checkpoints' / ('cp-' + ident + '.json')
        if (not path.resolve().is_relative_to(Path(session.dir).resolve()) or path.is_symlink()
                or not path.is_file() or path.stat().st_size > MAX_CHECKPOINT_BYTES):
            _invalid()
        row = json.loads(read_text(path))
        scope = scope_key(session)
        if (row.get('id') != ident or row.get('role') != role or scope is None
                or row.get('workSummary', {}).get('scope') != scope
                or fingerprint(row.get('original')) != row.get('sourceVersion')
                or fingerprint({k: v for k, v in row.items() if k != 'recordVersion'}) != row.get('recordVersion')):
            _invalid()
        messages = row['original'].get('messages', [])
        if not 0 <= index < len(messages):
            _invalid()
        message = messages[index]
        public = {k: message[k] for k in ('role', 'content', 'tool_calls', 'tool_call_id') if k in message}
        if message.get('role') != 'assistant' or fingerprint(public) != target['message_version']:
            _invalid('只能撤回自己的模型自述，不能更改用户要求或真实工具回执')
        if index in row.get('handoff',{}).get('runtimeStateIndices',[]):
            # An archived own note includes host identity/scope metadata. Only
            # its summary is authored by the model; other runtime facts are
            # not retractable by selecting a quote in an assistant envelope.
            _, marker, raw = str(message.get('content','')).partition('[working-summary]\n')
            if not marker:
                _invalid('宿主状态不是模型自述；撤回替代内容请使用 correction_id')
            try:
                return json.loads(raw)['summary'], 'summary'
            except (ValueError, KeyError, TypeError):
                _invalid()
        return message.get('content', ''), None
    _invalid()


def extend(session, role, previous, submitted, *, scope=None):
    records = deepcopy(previous.get('corrections', []))
    changed = []
    if not isinstance(submitted, list) or len(submitted) > 20:
        _invalid('每次提交最多20条有明确引用的更正')
    if submitted and not getattr(session,'task_input',{}).get('id'):
        _invalid('更正必须绑定当前任务')
    for item in submitted:
        if (not isinstance(item, dict) or set(item) - {'operation', 'target', 'reason', 'sources', 'replacement'}
                or item.get('operation') not in {'withdraw', 'replace'} or not isinstance(item.get('target'), dict)
                or not isinstance(item.get('reason'), str) or not 1 <= len(item['reason']) <= 1200
                or not isinstance(item.get('sources'), list) or not 1 <= len(item['sources']) <= 20
                or any(not isinstance(s, str) or not 1 <= len(s) <= 256 for s in item['sources'])):
            _invalid()
        replacement = item.get('replacement')
        if ((item['operation'] == 'replace' and (not isinstance(replacement, str) or not 1 <= len(replacement) <= 6000))
                or (item['operation'] == 'withdraw' and 'replacement' in item)):
            _invalid('替代需要新自述；没有新答案时只撤回，不填替代内容')
        target = item['target']
        quote = target.get('quote')
        text, field = _target_text(session, role, target, previous, records)
        if not isinstance(quote, str) or not 1 <= len(quote) <= 6000 or not isinstance(text, str) or text.count(quote) != 1:
            _invalid('原文片段须在所引用的自述版本中恰好出现一次')
        identity = fingerprint(dict(task=getattr(session, 'task_input', {}).get('id'), role=role,
                                    scope=scope if scope is not None else previous.get('scope'), correction=item))
        if any(r['id'] == identity for r in records):
            continue
        record = dict(id=identity, **deepcopy(item), supersedes=[r['id'] for r in records if r['target'] == target])
        if field:record['targetField']=field
        if 'correction_id' in target:
            record['supersedes'].append(target['correction_id'])
        records.append(record)
        changed.append(record)
    return records, changed


def state(session, role):
    row = load(session, role)
    records = row.get('corrections')
    return dict(taskId=row['task'], records={r['id']: r for r in records}) if records else None


def snapshot(values):
    from .working_context import state_snapshot_content
    return state_snapshot_content('working-corrections', values, notice=NOTICE)


def for_summary(session, role, messages, *, append_state=True):
    """A compaction-only projection. Raw messages, receipts and sent prefixes stay intact."""
    values = state(session, role)
    if not values:
        return messages
    result = deepcopy(messages)
    records = list(values['records'].values())
    calls={c['id']:c.get('function',{}).get('name') for m in messages for c in m.get('tool_calls') or []}
    for message in result:
        content = message.get('content')
        if not isinstance(content, str):
            continue
        public_version = fingerprint({k: message[k] for k in ('role', 'content', 'tool_calls', 'tool_call_id') if k in message})
        for record in records:
            target = record['target']
            marker = '[已' + ('撤回' if record['operation'] == 'withdraw' else '替代') + '；更正引用 ' + record['id'] + '；原文保留在归档]'
            if 'checkpoint_id' in target:
                if public_version == target['message_version']:
                    if record.get('targetField')=='summary':
                        head,_,raw=content.partition('[working-summary]\n')
                        note=json.loads(raw)
                        note['summary']=note['summary'].replace(target['quote'],marker,1)
                        message['content']=head+'[working-summary]\n'+json.dumps(note,ensure_ascii=False)
                    else:
                        message['content'] = content.replace(target['quote'], marker, 1)
            elif ('summary_version' in target and (message.get('role')=='assistant'
                  or message.get('role')=='tool' and calls.get(message.get('tool_call_id'))=='record_working_summary')):
                # Touch only an identified model-note field. Never blank an
                # entire check receipt because an interpretation was wrong.
                raw = content.split('[working-summary]\n', 1)[-1]
                try:
                    note = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(note, dict) and note.get('summaryVersion') == target['summary_version']:
                    note['summary'] = note.get('summary', '').replace(target['quote'], marker, 1)
                    message['content'] = content[:len(content) - len(raw)] + json.dumps(note, ensure_ascii=False)
            content = message.get('content')
    # Corrections can be in the retained tail, outside the prefix being
    # summarized. Send the committed state after that prefix as model data.
    if append_state:
        result.append(dict(role='assistant', content=snapshot(values)))
    return result
