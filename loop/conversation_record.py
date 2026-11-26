"""Publish the retained public dialogue into an ordinary conversation's folder."""
# 主要引用方包括 `session.py` 和 `workbench.py`。

import hashlib
from contextlib import contextmanager
import itertools
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
import uuid

from . import event_index, evidence_redaction, projects, workbench as W
from .filelock import FileLock, atomic_json


STATE = 'conversation-record.json'
PUBLICATION_LOCK = 'conversation-record.publish.lock'
NAME = re.compile(r'会话记录(?: \((?:[2-9]|[1-9][0-9]+)\))?\.md')
ROLES = {'user': '用户', 'executor': 'Agent', 'planner': '规划者', 'reviewer': '测试者'}
log = logging.getLogger(__name__)


class SourceChanged(Exception):
    pass


def binding(thread):
    resource = thread.get('workResource') or {}
    if (not thread.get('projectless') or any(thread.get(key) for key in
            ('projectId', 'readonlyEvidence', 'subtaskOf', 'archived'))
            or resource.get('id') not in thread.get('resourceIds', [])
            or resource.get('kind') != 'code' or resource.get('permission') != 'read'
            or resource.get('write') is not True):
        return None
    return {'resourceId': resource['id'], 'root': resource['path']}


def _stamp(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _source_stamp(directory):
    try:
        return _stamp((directory / 'events.jsonl').stat())
    except FileNotFoundError:
        return None


def _root(bound, expected=None):
    root = Path(bound['root'])
    try:
        info = root.lstat()
        identity = [info.st_dev, info.st_ino]
        if (not root.is_absolute() or root.resolve(strict=True) != root or not stat.S_ISDIR(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or expected is not None and identity != expected):
            raise OSError('directory changed')
    except (OSError, RuntimeError, ValueError) as error:
        raise W.Problem('record_directory_unavailable', '会话工作目录不可用或位置已变化', 409) from error
    return root, identity


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise W.Problem('record_conflict', '记录位置已有其他文件或链接', 409)
    return info


def _file_stamp(path):
    try:
        identity = _stamp(_regular(path))[:4]
        with path.open('rb') as handle:
            observed = list(W.file_text_stamp(handle))
        if observed[:4] != identity:
            raise W.Problem('record_conflict', '核对时记录文件已变化', 409)
        return observed
    except FileNotFoundError:
        return None


def _file_digest(path):
    first = _file_stamp(path)
    if first is None:
        return None, None
    with path.open('rb') as handle:
        if list(W.file_text_stamp(handle)) != first:
            raise W.Problem('record_conflict', '核对时记录文件已变化', 409)
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if list(W.file_text_stamp(handle)) != first or _stamp(_regular(path))[:4] != first[:4]:
            raise W.Problem('record_conflict', '核对时记录文件已变化', 409)
    return digest, first


def _state(directory, bound):
    value = W.read(directory / STATE)
    return value if isinstance(value, dict) and value.get('binding') == bound else {'binding': bound}


def _path(root, name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise W.Problem('record_conflict', '保存的记录文件名无法核对', 409)
    return root / name


def check_workspace(workspace, thread, resource_id):
    bound = binding(thread)
    if bound and bound['resourceId'] == resource_id and bound['root'] == workspace['path']:
        return dict(workspace, recordBinding=(W.P.data_root() / 'runs' / thread['id'], bound))
    return workspace


@contextmanager
def _publication(directory):
    lock = FileLock(directory / PUBLICATION_LOCK, timeout=.25, create_parent=False)
    if not lock.acquire():
        # A large source copy can still be holding its shared view. The existing
        # record worker reschedules SourceChanged without marking a failed save.
        raise SourceChanged()
    try:
        yield
    finally:
        lock.release()


@contextmanager
def check_view(workspace, *, timeout=10):
    """Freeze publication only while observing or copying its actual disk inputs."""
    directory, bound = workspace['recordBinding']
    with FileLock(directory / PUBLICATION_LOCK, timeout=timeout, shared=True, create_parent=False):
        state = _state(directory, bound)
        root, identity = _root(bound)
        name = revision = stage = None
        if state.get('rootIdentity') == identity and state.get('owner'):
            pending = state.get('pendingWrite') or {}
            # A killed publisher may have staged or renamed the file before committing metadata.
            temporary = pending.get('temporary', '')
            if re.fullmatch(r'\.modot-record-[A-Za-z0-9_]+\.tmp', temporary):
                try:
                    info = _regular(root / temporary)
                    if [info.st_dev, info.st_ino] == pending.get('temporaryIdentity'):
                        stage = temporary
                except (FileNotFoundError, W.Problem):
                    pass
            candidate = pending.get('relativePath') or state.get('relativePath')
            if candidate and NAME.fullmatch(candidate):
                try:
                    stamp = _file_stamp(root / candidate)
                    if stamp and stamp == state.get('fileStamp') and candidate == state.get('relativePath'):
                        name, revision = candidate, state['syncedRevision']
                    elif stamp and stamp[:2] == pending.get('temporaryIdentity'):
                        digest, _ = _file_digest(root / candidate)
                        if digest == pending.get('revision'):
                            name, revision = candidate, digest
                except W.Problem:
                    pass  # An unproven file/link stays in the ordinary source and its safety checks.
        yield name, revision, stage


def _complete_stream_text(row, streamed):
    prefix = row.get('text')
    length = row.get('full_len')
    if (row.get('truncated') is True and isinstance(prefix, str) and prefix
            and type(length) is int and len(streamed) == length and length > len(prefix)
            and streamed.startswith(prefix)):
        return streamed
    return None


class Dialogue:
    """Group retained stream fragments and committed text in physical event order."""

    def __init__(self, *, include_children=False):
        self.include_children = include_children
        self.items = []
        self.streams = {}
        self.closed = {}
        self.questions = set()

    def close(self, scope):
        item = self.streams.pop(scope, None)
        if item is not None:
            item['settled'] = True
            self.closed[scope, item['turn']] = item

    def add(self, row):
        agent = row.get('agent_id') or row.get('childId') or None
        if agent is not None and (not self.include_children or not isinstance(agent, str)):
            return
        act, run = row.get('act'), row.get('run_id')
        if not isinstance(act, str):
            return
        role = 'user' if act == 'user' else (row.get('role') or 'executor')
        if not isinstance(role, str) or role not in ROLES or run is not None and not isinstance(run, str):
            return
        scope = (run, role, agent)
        turn = row.get('turn')
        turn = turn if type(turn) is int else None
        if act == 'user' or act in {'end', 'error', 'paused', 'paused_mid_tools'}:
            for key in list(self.streams):
                if key[2] == agent and (act == 'user' or key[0] == run):
                    self.close(key)
        if act == 'user':
            for key in list(self.closed):
                if key[0][2] == agent:
                    del self.closed[key]
        if act == 'provider_stream_interrupted':
            opened = self.streams.get(scope)
            if opened is not None and (turn is None or opened['turn'] == turn):
                self.close(scope)
            for key in list(self.closed):
                if key[0] == scope and (turn is None or key[1] == turn):
                    del self.closed[key]
        if act == 'role_phase' and row.get('state') in {'completed', 'failed', 'paused', 'timed_out', 'waiting_human', 'skipped'}:
            self.close(scope)
        if act in {'agent_message_chunk', 'delta'} and row.get('kind') != 'think':
            text = row.get('text')
            if not isinstance(text, str) or not text:
                return
            entry = self.streams.get(scope)
            if entry is None or entry['turn'] != turn:
                self.close(scope)
                entry = {'role': role, 'time': row.get('t'), 'turn': turn, 'parts': [],
                         'partial': True, 'settled': False, 'truncated': False}
                self.streams[scope] = entry
                self.items.append(entry)
            entry['parts'].append(text)
            return
        if act not in {'user', 'say', 'ask_human'} or row.get('toolPreludeFallback'):
            return
        full_user = row.get('recordText') if act == 'user' else None
        text = full_user or row.get('text')
        if act == 'ask_human' and not text:
            text = row.get('question')
        if not isinstance(text, str) or not text.strip():
            return
        if act == 'ask_human':
            action_id = row.get('action_id')
            action = (scope, action_id) if isinstance(action_id, str) and action_id else None
            if action in self.questions:
                return
            if action is not None:
                self.questions.add(action)
        truncated = bool(row.get('truncated') and not full_user)
        restored = None
        entry = None
        if act == 'say':
            opened = self.streams.get(scope)
            if opened is not None and (turn is None or opened['turn'] == turn):
                entry = self.streams.pop(scope)
            elif turn is not None:
                former = self.closed.get((scope, turn))
                if former is not None:
                    streamed = ''.join(former['parts']).strip()
                    if text.startswith(streamed) or _complete_stream_text(row, streamed) is not None:
                        entry = former
            self.closed.pop((scope, turn), None)
            if entry is not None and truncated:
                restored = _complete_stream_text(row, ''.join(entry['parts']).strip())
                if restored is not None:
                    text, truncated = restored, False
        if entry is None:
            entry = {'role': role, 'time': row.get('t')}
            self.items.append(entry)
        entry.update(parts=[text], partial=False, settled=True, truncated=truncated)
        return restored

    def markdown(self, owner):
        chunks = [f'# 会话记录\n\n<!-- Modot record {owner} -->\n']
        truncated = False
        for item in self.items:
            if not item['settled']:
                continue
            text = evidence_redaction.redact(''.join(item['parts'])).replace('\r\n', '\n').replace('\r', '\n')
            text = text.replace('\x00', '\ufffd')
            if not text.strip():
                continue
            timestamp = item['time']
            label = f"## {ROLES[item['role']]}"
            if isinstance(timestamp, str) and re.fullmatch(r'\d{2}:\d{2}:\d{2}', timestamp):
                label += ' · ' + timestamp
            if item['partial']:
                label += '（未完成）'
            if item['truncated']:
                truncated = True
                text += '\n\n> 原历史中的这条正文已截断。'
            chunks.append(f'\n{label}\n\n{text}\n')
        return (''.join(chunks).encode('utf-8') if len(chunks) > 1 else b''), truncated


def _restored_replies(rows, offsets):
    dialogue = Dialogue(include_children=True)
    replies = {}
    for row in rows:
        text = dialogue.add(row)
        offset = row['sourceOffset']
        if text is not None and offset in offsets:
            replies[offset] = text
    return replies


def timeline_events(path, rows, metadata):
    """Restore verified reply text with at most one extra bounded history page."""
    from .provider_recovery import public_event

    rows = [public_event(row) for row in rows]
    truncated = {
        row['sourceOffset'] for row in rows
        if row.get('act') == 'say' and row.get('truncated') is True
        and isinstance(row.get('run_id'), str) and row['run_id']
        and type(row.get('turn')) is int and type(row.get('full_len')) is int
    }
    if not truncated or metadata.get('gap'):
        return rows

    recovered = _restored_replies(rows, truncated)
    missing = truncated - recovered.keys()
    first = rows[0]['sourceOffset']
    if missing and first > 0:
        try:
            earlier, _, history = event_index.page(path, tail=True, after_position=-1, before_position=first)
        except OSError:
            pass  # The current page remains readable if the optional lookback fails.
        else:
            if earlier and history['generation'] == metadata['generation'] and not history['gap']:
                recovered.update(_restored_replies(itertools.chain(earlier, rows), missing))

    # Keep the extra text within the normal event-page byte budget.
    remaining = event_index.PAGE_BYTES
    for index, row in enumerate(rows):
        text = recovered.get(row['sourceOffset'])
        if text is not None:
            size = len(json.dumps(text, ensure_ascii=False).encode('utf-8'))
            if size <= remaining:
                rows[index] = dict(row, text=text, truncated=False)
                remaining -= size
    return rows


def _snapshot(directory, owner, source):
    dialogue = Dialogue()
    cursor, generation = -1, None
    empty_pages = 0
    while True:
        rows, _, meta = event_index.page(directory / 'events.jsonl', limit=240, after_position=cursor,
                                         before_position=source[2] if source else None)
        if meta.get('verificationPending'):
            raise SourceChanged()  # The event index is verifying a large prefix in the background.
        if meta['gap']:
            raise W.Problem('record_history_gap', '原会话日志有缺口，上一份记录保留', 409)
        if generation is not None and generation != meta['generation']:
            raise SourceChanged()
        if rows:
            generation = meta['generation']
            cursor = rows[-1]['sourceOffset']
            empty_pages = 0
            for row in rows:
                dialogue.add(row)
        if not meta['catchingUp'] and len(rows) < 240 and not meta.get('pageClipped'):
            break
        if not rows:
            empty_pages += 1
            if empty_pages == 5:
                if _source_stamp(directory) != source:
                    raise SourceChanged()
                raise W.Problem('record_history_incomplete', '原会话日志尚未完整落盘', 409)
    if not W.RT.status(directory)['active']:
        for scope in list(dialogue.streams):
            dialogue.close(scope)
    if _source_stamp(directory) != source:
        raise SourceChanged()
    raw, truncated = dialogue.markdown(owner)
    return raw, {'historyRevision': W.digest(raw) if raw else None, 'hasHistoricalTruncation': truncated,
                 'source': source, 'generation': generation, 'sourceOffset': cursor}


def _finish_write(state, pending, observed):
    state.update(relativePath=pending['relativePath'], syncedRevision=pending['revision'],
                 fileStamp=observed, sizeBytes=observed[2], errorCode=None)
    state.pop('pendingWrite', None)


def _forget_stage(root, pending):
    temporary = pending.get('temporary', '')
    if re.fullmatch(r'\.modot-record-[A-Za-z0-9_]+\.tmp', temporary):
        stage = root / temporary
        try:
            info = stage.lstat()
            if stat.S_ISREG(info.st_mode) and [info.st_dev, info.st_ino] == pending.get('temporaryIdentity'):
                stage.unlink()
        except OSError:
            pass


def _recover(directory, root, state):
    pending = state.get('pendingWrite')
    if not pending:
        return
    _forget_stage(root, pending)
    target = _path(root, pending['relativePath'])
    try:
        digest, observed = _file_digest(target)
    except W.Problem:
        if pending['previous'] is not None:
            raise
        digest, observed = None, None  # A new same-name file never belongs to this record.
    if observed is not None and digest == pending['revision']:
        _finish_write(state, pending, observed)
    else:
        state.pop('pendingWrite')
    atomic_json(directory / STATE, state)


# 首次同步保留目录中已有的同名文件。续写用上一版摘要识别外部修改，冲突时保留磁盘内容。
def _publish(directory, bound, root, state, raw):
    revision = state['historyRevision']
    previous = state.get('syncedRevision')
    names = ([state['relativePath']] if previous else
             ['会话记录.md', *(f'会话记录 ({i}).md' for i in range(2, 1001))])
    for name in names:
        target = _path(root, name)
        if previous is None and os.path.lexists(target):
            continue
        with FileLock(W.file_write_lock_path({'path': str(root)}, name), timeout=2):
            if previous is None and os.path.lexists(target):
                continue
            digest, before = _file_digest(target)
            if digest != previous:
                raise W.Problem('record_conflict', '上次保存的记录已被修改或移走', 409)
            if digest == revision:
                _finish_write(state, {'relativePath': name, 'revision': revision}, before)
                break
            fd, temp_name = tempfile.mkstemp(prefix='.modot-record-', suffix='.tmp', dir=root)
            stage = Path(temp_name)
            info = os.fstat(fd)
            pending = dict(relativePath=name, previous=previous, revision=revision, temporary=stage.name,
                           temporaryIdentity=[info.st_dev, info.st_ino])
            try:
                with os.fdopen(fd, 'wb') as stream:
                    state['pendingWrite'] = pending
                    atomic_json(directory / STATE, state)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                with FileLock(W.home() / 'registry.lock', timeout=2):
                    _root(bound, state['rootIdentity'])
                    if binding(projects.conversation(directory.name)) != bound:
                        raise W.Problem('record_scope_changed', '会话工作目录授权已变化', 409)
                    if previous is None:
                        try:
                            if os.name == 'nt':
                                os.rename(stage, target)  # Windows rename is exclusive.
                            else:
                                os.link(stage, target)
                                stage.unlink()
                        except FileExistsError:
                            state.pop('pendingWrite', None)
                            continue
                    else:
                        if _file_stamp(target) != before:
                            raise W.Problem('record_conflict', '发布前记录文件已变化', 409)
                        os.replace(stage, target)
                _finish_write(state, pending, _file_stamp(target))
                break
            finally:
                _forget_stage(root, pending)
    else:
        raise W.Problem('record_conflict', '工作目录中的会话记录候选名已被占用', 409)


def _sync(directory):
    # An event may outlive its session, or come from a project with no dialogue projection.
    if not directory.is_dir() or not binding(W.registry()['threads'].get(directory.name, {})):
        return
    with FileLock(directory / 'conversation-record.lock', timeout=4, create_parent=False):
        bound = binding(projects.conversation(directory.name))
        if bound is None:
            return
        state = _state(directory, bound)
        try:
            source = _source_stamp(directory)
            root, identity = _root(bound, state.get('rootIdentity'))
            state['rootIdentity'] = identity
            if state.get('pendingWrite'):
                with _publication(directory):
                    _recover(directory, root, state)
            if state.get('relativePath'):
                previous = _path(root, state['relativePath'])
                if _file_stamp(previous) != state.get('fileStamp'):
                    digest, observed = _file_digest(previous)
                    if digest is None or digest != state.get('syncedRevision'):
                        raise W.Problem('record_conflict', '已保存记录在磁盘上发生变化', 409)
                    state['fileStamp'] = observed
                    atomic_json(directory / STATE, state)
            if not state.get('errorCode') and 'source' in state and state['source'] == source:
                if state.get('historyRevision') == state.get('syncedRevision'):
                    return
            owner = state.setdefault('owner', uuid.uuid4().hex)
            raw, history = _snapshot(directory, owner, source)
            if (not raw and state.get('syncedRevision')) or (source is None and state.get('source')):
                raise W.Problem('record_history_gap', '无法核对原历史，已有记录保留', 409)
            state.update(history, errorCode=None)
            if raw:
                with _publication(directory):
                    _publish(directory, bound, root, state, raw)
                    atomic_json(directory / STATE, state)
                return
        except W.Problem as error:
            state['errorCode'] = error.code
        except OSError:
            state['errorCode'] = 'record_write_failed'
        atomic_json(directory / STATE, state)


_jobs = {}
_queue = threading.Condition()
_worker = None


def schedule(directory, *, immediate=False):
    global _worker
    with _queue:
        deadline = time.monotonic() + (0 if immediate else .18)
        _jobs[directory] = min(deadline, _jobs.get(directory, deadline))
        if _worker is None:
            _worker = threading.Thread(target=_work, name='conversation-record', daemon=True)
            _worker.start()
        _queue.notify()


def _work():
    global _worker
    while True:
        with _queue:
            if not _jobs:
                _worker = None
                return
            directory = min(_jobs, key=_jobs.get)
            delay = _jobs[directory] - time.monotonic()
            if delay > 0:
                _queue.wait(delay)
                continue
            _jobs.pop(directory)
        try:
            _sync(directory)
        except SourceChanged:
            schedule(directory)
        except Exception as error:
            # Retire the job, including when the private checkpoint cannot be saved.
            # Never stop the event writer or leave the scheduler pinned to a dead thread.
            log.warning('Conversation record job failed (%s)', type(error).__name__)


def notify(directory, row):
    if (not row.get('agent_id') and not row.get('childId')
            and directory.parent == W.P.data_root() / 'runs'):
        schedule(directory)


def recover(resolve_session):
    for ident, thread in W.registry()['threads'].items():
        if binding(thread) and (directory := resolve_session(ident)) is not None:
            schedule(directory)


def status(thread, directory):
    bound = binding(thread)
    result = dict(status='not_applicable', resourceId=None, relativePath=None, historyRevision=None,
                  syncedRevision=None, errorCode=None, hasHistoricalTruncation=False, sizeBytes=None)
    if bound is None:
        return result
    result['resourceId'] = bound['resourceId']
    if directory is None:
        return dict(result, status='blocked', errorCode='record_history_unavailable')
    state = _state(directory, bound)
    result.update({key: state[key] for key in result if key in state and key not in {'status', 'resourceId', 'relativePath', 'sizeBytes'}})
    try:
        source = _source_stamp(directory)
        if state.get('source') != source:
            result['historyRevision'] = None
        root, _ = _root(bound, state.get('rootIdentity'))
        if state.get('relativePath'):
            stamped = _file_stamp(_path(root, state['relativePath']))
            if stamped == state.get('fileStamp') and stamped is not None:
                result.update(relativePath=state['relativePath'], sizeBytes=state['sizeBytes'])
            elif not state.get('pendingWrite'):
                return dict(result, status='blocked', errorCode='record_conflict')
    except W.Problem as error:
        return dict(result, status='blocked', errorCode=error.code)
    except OSError:
        return dict(result, status='blocked', errorCode='record_read_failed')
    if result['errorCode']:
        return dict(result, status='blocked')
    if state.get('source') and (source is None or not source[2]) and state.get('syncedRevision'):
        return dict(result, status='blocked', errorCode='record_history_gap')
    if state.get('pendingWrite') or source and source[2] and state.get('source') != source:
        return dict(result, status='pending', historyRevision=None)
    if result['relativePath'] and result['historyRevision'] == result['syncedRevision']:
        return dict(result, status='saved')
    return dict(result, status='empty' if not result['historyRevision'] else 'pending')


def _read(thread, directory, query):
    revision, after = query.get('revision', []), query.get('after', ['0'])
    if (set(query) - {'revision', 'after'} or len(revision) != 1 or len(after) != 1
            or not re.fullmatch('[a-f0-9]{64}', revision[0]) or not re.fullmatch('[0-9]{1,12}', after[0])):
        raise W.Problem('invalid_cursor', '请提供记录版本及有效的字节游标')
    if directory is None:
        raise W.Problem('record_unavailable', '会话记录尚未保存', 409)
    offset = int(after[0])
    with FileLock(directory / 'conversation-record.lock', timeout=2):
        thread = projects.conversation(thread['id'])
        view = status(thread, directory)
        if not view['relativePath'] or revision[0] != view['syncedRevision']:
            raise W.Problem('record_changed', '记录已改变，请刷新后从首页重新读取', 409)
        if offset > view['sizeBytes']:
            raise W.Problem('invalid_cursor', '记录游标超出文件范围')
        bound = binding(thread)
        state = _state(directory, bound)
        file = _path(_root(bound, state.get('rootIdentity'))[0], view['relativePath'])
        expected = state['fileStamp']
        with file.open('rb') as stream:
            if list(W.file_text_stamp(stream)) != expected:
                raise W.Problem('record_changed', '记录已改变，请刷新后从首页重新读取', 409)
            stream.seek(offset)
            chunk = stream.read(W.TEXT_PAGE_BYTES)
            if list(W.file_text_stamp(stream)) != expected or _stamp(_regular(file))[:4] != expected[:4]:
                raise W.Problem('record_changed', '记录已改变，请刷新后从首页重新读取', 409)
        try:
            text, next_offset = W.decode_utf8_page(chunk, offset, view['sizeBytes'])
        except UnicodeDecodeError as error:
            raise W.Problem('invalid_cursor', '记录游标未对齐 UTF-8 字符') from error
        return dict(text=text, revision=revision[0], nextOffset=next_offset,
                    hasMore=next_offset < view['sizeBytes'], sizeBytes=view['sizeBytes'])


def route(thread, directory, suffix, method, query, body):
    if not suffix and method == 'GET' and not query:
        return 200, status(thread, directory)
    if suffix == ['sync'] and method == 'POST' and not query and not set(body)-{'_request_id'}:
        view = status(thread, directory)
        if directory is None or view['status'] == 'not_applicable':
            raise W.Problem('record_unavailable', '此会话没有可同步的独立工作目录', 409)
        schedule(directory, immediate=True)
        return 202, dict(view, status='pending', errorCode=None)
    if suffix == ['text'] and method == 'GET':
        return 200, _read(thread, directory, query)
    raise W.Problem('not_found', '没有此会话记录操作', 404)
