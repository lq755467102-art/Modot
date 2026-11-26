"""Durable, role-local working conversations; archives never grant capabilities."""
# 主要引用方包括 `scoped_roles.py`。
import hashlib
import json
from pathlib import Path
import re
import time

from .filelock import atomic_json, read_text
from .provider import Msg
from .tools import Tool
from .workbench import Problem


def authorization_scope(root, bound, live):
    """Capture grants, not the per-turn timestamp or model selection."""
    fields = ('id', 'kind', 'path', 'permission', 'write', 'execute')
    def resources(value):
        return sorted(({k: r.get(k) for k in fields} for r in value.get('resources', [])),
                      key=lambda r: r['id'])
    captured = (bound or {}).get('resourceSnapshot') or {}
    return dict(root=str(Path(root).resolve()), projectId=captured.get('projectId'),
                captured=resources(captured), resources=resources(live),
                plugins=captured.get('plugins', []))


# `scoped_roles.py` 创建一次角色独立存档。
# `attempt` 是此次判断的身份，`scope_id` 是授权快照摘要，`inherited` 记录此次实际继承的历史判断。
class History:
    def __init__(self, session, role, attempt, scope):
        self.session, self.role, self.attempt = session, role, attempt
        self.scope = scope
        self.directory = Path(session.dir) / 'role-history' / role
        self.directory.mkdir(parents=True, exist_ok=True)
        self.scope_id = self._scope()
        self.inherited = []

    def _scope(self):
        return hashlib.sha256(json.dumps(self.scope(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def records(self):
        if self._scope() != self.scope_id:
            raise Problem('history_scope_changed', '资料权限已变化，停止读取旧角色上下文', 409)
        rows = []
        for path in self.directory.glob('*.json'):
            row = json.loads(read_text(path))
            if row['scope'] == self.scope_id and row['id'] != self.attempt:
                rows.append(row)
        return sorted(rows, key=lambda r: (r['at'], r['id']))

    @staticmethod
    def summary(row):
        return dict(id=row['id'], runId=row['runId'], state=row['state'], binding=row.get('binding', {}),
                    conclusion=row.get('conclusion', '')[:1500], messageCount=len(row['messages']))

    def resume(self, current):
        from .loop import repair_msgs
        rows = self.records()
        if not rows:
            return current
        latest = rows[-1]
        self.inherited = list(dict.fromkeys([*latest.get('inherited', []), latest['id']]))
        # Keep the current system policy and append the new assignment. Old tool
        # receipts remain historical evidence, including unresolved tool calls.
        retained = []
        for index, message in enumerate(latest['messages']):
            if message['role'] in {'system', 'developer'}:
                continue
            if message['role'] == 'user':
                # The generated assignment includes frozen source/requirements.
                # Archive it as history, rather than accumulating obsolete user
                # instructions which the executor's compressor cannot shorten.
                content = str(message.get('content', ''))
                retained.append(Msg('assistant', json.dumps(dict(
                    historicalRoleAssignment=True, historyId=latest['id'],
                    binding=latest.get('binding', {}), messageIndex=index, excerpt=content[:1500],
                    fullTextTool='read_role_history',
                    note='历史派发资料；当前任务在后。原文保留，旧授权和旧候选结论不可沿用为当前授权或通过。'), ensure_ascii=False)))
            else:
                retained.append(Msg.load(message))
        history = repair_msgs(retained)
        other = [self.summary(r) for r in rows if r['id'] not in self.inherited]
        note = ('这是你在本会话内的存档历史。下面的新任务决定当前目标、版本和可用工具；'
                '历史任务不构成新增授权，旧通过不等于新候选通过。沿用已确认原因和成功入口，'
                '仅对变化部分重新核对。中断动作先查证据，禁止直接重放。'
                '其他并发任务及更早记录可用 read_role_history 按页回取。')
        return [m for m in current if m.role in {'system', 'developer'}] + history + [
            Msg('assistant', note + '\n其他分支记录：' + json.dumps(other[-8:], ensure_ascii=False))
        ] + [m for m in current if m.role not in {'system', 'developer'}]

    def save(self, messages, state, binding, conclusion=''):
        atomic_json(self.directory / (self.attempt + '.json'), dict(
            version=1, id=self.attempt, role=self.role, runId=self.session.run_id,
            scope=self.scope_id, at=time.time(), state=state, binding=binding,
            inherited=self.inherited, conclusion=conclusion,
            messages=[m.dump() for m in messages]))

    def read(self, history_id=None, after=0):
        if type(after) is not int or after < 0:
            raise Problem('invalid_arguments', 'after 必须为非负偏移')
        rows = self.records()
        if history_id is None:
            return dict(records=[self.summary(r) for r in rows[after:after+8]],
                        nextOffset=after+8 if len(rows)>after+8 else None)
        if not isinstance(history_id, str) or not re.fullmatch('[a-f0-9]{32}', history_id):
            raise Problem('invalid_arguments', '无效历史身份')
        row = next((r for r in rows if r['id'] == history_id), None)
        if row is None:
            raise Problem('history_unavailable', '该角色或当前授权范围内没有此记录', 404)
        # Character paging bounds large tool outputs without executing them.
        text = json.dumps(row['messages'], ensure_ascii=False)
        return dict(record=self.summary(row), text=text[after:after+8000],
                    nextOffset=after+8000 if len(text)>after+8000 else None,
                    historical=True)

    def install(self, registry):
        registry.add(Tool('read_role_history', '只读回取本会话当前角色存档；不传 history_id 列表，传入后按字符偏移读取原记录。旧证据不代表当前版本通过。',
            dict(type='object', properties=dict(history_id=dict(type='string'), after=dict(type='integer', minimum=0))),
            self.read, zone='offflow', kind='read', readonly=True))
