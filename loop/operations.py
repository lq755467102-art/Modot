"""Durable HTTP command receipts. A receipt completes at dispatch, not task completion.

An interrupted dispatch is UNKNOWN and is never automatically repeated: an external
side effect may already have happened. Queued commands are recovered separately.
"""
# 主要引用方包括 `bin/server.py`。
from __future__ import annotations
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from .filelock import FileLock, atomic_json, read_text

STATES = ('processing', 'queued', 'completed', 'failed', 'unknown', 'not_dispatched')

# The original waiting file remains the FIFO head for sessions saved by older versions.
PENDING_HEAD = 'pending-after.json'
PENDING_TAIL = 'pending-followups'
ARCHIVED_INPUT = {'code': 'archived_conversation', 'error': '会话已归档，这条排队消息未启动。'}


def followup_receipt(request, result):
    """Retain accepted input until it has a canonical message in a launched run."""
    response = dict(result)
    if request.get("text") and not result.get("ok"):
        response["messageText"] = request["text"]
    outcome = response.get("out") or {}
    if outcome.get("error"):
        response["error"] = outcome["error"]
    if outcome.get("runId"):
        response["runId"] = outcome["runId"]
    return response


def waiting_inputs(directory):
    head = directory / PENDING_HEAD
    tail = directory / PENDING_TAIL
    return ([head] if head.is_file() else []) + (sorted(tail.glob('[0-9]*.json')) if tail.is_dir() else [])


def cancel_waiting_inputs(root, directory):
    """Persist cancellation before a restore can make this thread runnable again."""
    with FileLock(directory / 'pending.lock'):
        for path in waiting_inputs(directory):
            record = json.loads(read_text(path))
            record['cancelled'] = True
            atomic_json(path, record)
            result = {'ok': False, 'code': 409, 'out': ARCHIVED_INPUT}
            request = record.get('request') or {}
            finish(root, request.get('_request_id'), 409, followup_receipt(request, result), 'not_dispatched')


@contextmanager
def database(root):
    p = Path(root) / 'runs' / 'operations.sqlite3'
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with FileLock(p.with_suffix('.init.lock')):
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS operations
              (id TEXT PRIMARY KEY, path TEXT NOT NULL, digest TEXT NOT NULL,
               state TEXT NOT NULL, code INTEGER, response TEXT, updated REAL NOT NULL)''')
        yield db
    finally:
        db.close()

def _decode(row):
    if row is None:
        return None
    r = dict(row)
    r.pop('digest', None)
    r['response'] = json.loads(r['response']) if r['response'] else None
    return r

# 同一事务领取编号；已领取编号校验原路由和正文，封存为未分派的编号始终拒绝再次执行。
def claim(root, ident, path, body):
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with database(root) as db:
        with db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM operations WHERE id=?', (ident,)).fetchone()
            if old:
                if old['state'] == 'not_dispatched':
                    return False, _decode(old)
                if old['path'] != path or old['digest'] != digest:
                    raise ValueError('request id reused with different content')
                return False, _decode(old)
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                       (ident, path, digest, 'processing', None, None, time.time()))
    return True, None

def finish(root, ident, code, response, state=None):
    if not ident:
        return
    # 这里的 completed 是 HTTP 分派已完成，后台任务和用户验收仍有独立状态。
    state = state or ('queued' if response.get('queued') or (response.get('resume') or {}).get('queued')
                      else ('completed' if code < 400 else 'failed'))
    if state not in STATES:
        raise ValueError(state)
    with database(root) as db, db:
        # A worker can complete before the HTTP receipt is written. Never regress.
        db.execute('UPDATE operations SET state=?,code=?,response=?,updated=? WHERE id=?'
                   + (" AND state IN ('processing','queued')" if state == 'queued' else ''),
                   (state, code, json.dumps(response, ensure_ascii=False), time.time(), ident))

def get(root, ident):
    with database(root) as db:
        return _decode(db.execute('SELECT * FROM operations WHERE id=?', (ident,)).fetchone())

def reconcile(root, ids, seal_missing=False):
    with database(root) as db, db:
        # 封存未送达编号会阻止它日后被迟到请求重新领取；普通轮询不应随意关闭尚在送达的请求。
        if seal_missing:
            # A delayed dispatch already has a receipt or will see this
            # durable rejection before doing any work.
            db.execute('BEGIN IMMEDIATE')
            response = json.dumps(dict(code='request_not_dispatched',
                                       error='原请求未送达，已关闭该请求编号'), ensure_ascii=False)
            db.executemany(
                'INSERT OR IGNORE INTO operations VALUES (?,?,?,?,?,?,?)',
                [(ident, '', '', 'not_dispatched', 409, response, time.time()) for ident in ids],
            )
        rows = db.execute(
            'SELECT id,state FROM operations WHERE id IN (' + ','.join('?' for _ in ids) + ')',
            ids,
        )
        return {row['id']: row['state'] for row in rows}


def recover(root):
    with database(root) as db, db:
        db.execute("UPDATE operations SET state='unknown',updated=? WHERE state='processing'", (time.time(),))
