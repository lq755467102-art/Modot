"""Small host adapter: scoped contributions, owned state and ordinary tools.

DSH-inspired registration/lifetime contracts, implemented on Modot's existing
registries and atomic storage. No plugin code loader, scheduler or history setter.
"""
# 主要引用方包括 `plugin_packages.py` 和 `executor_core.py`。
from copy import copy, deepcopy
import json
import re

from . import workbench as W
from .context_registry import ScopedRegistry, request_scope
from .filelock import FileLock, atomic_json
from .tools import Tool

LIMIT = 8192


def declarations(value):
    """Data-only example adapter; future trusted host modules can use ContextHost."""
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError('contextStores requires at most eight stores')
    names = set()
    for row in value:
        if (not isinstance(row, dict) or set(row) != {'name', 'description', 'initial'}
                or not isinstance(row['name'], str) or not re.fullmatch('[a-z][a-z0-9_]{0,15}', row['name'])
                or row['name'] in names or not isinstance(row['description'], str)
                or len(row['description']) > 240):
            raise ValueError('contextStores requires distinct names, descriptions and JSON initial values')
        names.add(row['name'])
    _json(value)
    return deepcopy(value)


def _json(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise W.Problem('context_data', '上下文状态必须是有效 JSON') from None
    if len(raw.encode('utf8')) > LIMIT:
        raise W.Problem('context_capacity', '上下文状态超过8KiB，原状态保留', 413)
    return raw


class OwnedState:
    def __init__(self, module, name, initial, *, lifetime='task'):
        _json(initial)
        self.module, self.initial = module, deepcopy(initial)
        self.owner = dict(pluginId=module.name, fingerprint=module.source['fingerprint'],
                          taskId=module.host.task, role=module.host.role, name=name)
        if lifetime not in {'task', 'session'}:
            raise ValueError('Unsupported state lifetime')
        if lifetime == 'session':
            self.owner.update(taskId=None, lifetime='session')
        self.path = module.host.session.dir / 'context-plugins' / (W.digest(
            json.dumps(self.owner, sort_keys=True).encode()) + '.json')

    def read(self):
        self.module.check()
        if not self.path.resolve().is_relative_to(self.module.host.session.dir.resolve()):
            raise W.Problem('context_path', '上下文存储位置变化', 403)
        if not self.path.exists():
            return dict(revision=0, value=deepcopy(self.initial))
        if self.path.stat().st_size > LIMIT + 4096:
            raise W.Problem('context_capacity', '已有上下文状态超过边界，未重置', 413)
        with self.path.open('rb') as stream:
            raw = stream.read(LIMIT + 4097)
        if len(raw) > LIMIT + 4096:
            raise W.Problem('context_capacity', '上下文状态读取期间变大，未截断或重置', 413)
        row = json.loads(raw.decode('utf8'))
        if row.get('owner') != self.owner or type(row.get('revision')) is not int or row['revision'] < 1:
            raise W.Problem('context_state', '上下文状态归属或版本不可核对，未重置', 409)
        _json(row['value'])
        return dict(revision=row['revision'], value=deepcopy(row['value']))

    def write(self, value, expected_revision):
        _json(value)
        if type(expected_revision) is not int or expected_revision < 0:
            raise W.Problem('context_revision', '更新上下文须提供读取到的整数 revision', 409)
        self.module.check()
        root = self.path.parent
        root.mkdir(exist_ok=True)
        if not root.resolve().is_relative_to(self.module.host.session.dir.resolve()):
            raise W.Problem('context_path', '上下文存储位置变化', 403)
        with FileLock(root / 'state.lock'):
            current = self.read()
            if expected_revision != current['revision']:
                raise W.Problem('context_conflict', '上下文状态已变化，请重读当前版本', 409)
            session = self.module.host.session
            # Same ownership check as Session.save: an old worker cannot publish.
            if session.run_id and not session.owned_run_id():
                raise W.Problem('context_owner', '运行所有权已变化，未保存上下文', 403)
            self.module.check()
            row = dict(owner=self.owner, revision=current['revision'] + 1, value=deepcopy(value))
            atomic_json(self.path, row)
        session.emit('context_plugin_write', pluginId=self.module.name, name=self.owner['name'],
                     revision=row['revision'])
        return dict(revision=row['revision'], value=deepcopy(value))


class ContextModule:
    """Host-owned handle. A module cannot choose another module's name or scope."""
    def __init__(self, host, name, source, validate):
        self.host, self.name, self.source, self.validate = host, name, deepcopy(source), validate
        self.disposers, self.closed = [], False

    def check(self):
        if self.closed or request_scope(self.host.session, self.host.role) != self.host.scope:
            raise W.Problem('context_revoked', '上下文注册已撤销或任务范围已变化', 403)
        self.validate()

    def state(self, name, initial=None, *, lifetime='task'):
        if not re.fullmatch('[a-z][a-z0-9_]{0,15}', name):
            raise ValueError('invalid local state name')
        self.check()
        return OwnedState(self, name, initial, lifetime=lifetime)

    def context(self, name, read, *, order=0):
        self.check()
        if not re.fullmatch('[a-z][a-z0-9_]{0,15}', name) or type(order) is not int or not callable(read):
            raise ValueError('invalid context provider')
        def current():
            self.check()
            return read()
        dispose = self.host.providers.effect('plugin.' + self.name + '.context.' + name,
            (order, current, self.source), scope=self.host.scope)
        self.disposers.append(dispose)
        return dispose

    def tool(self, tool):
        self.check()
        if not tool.name.startswith('ctx_' + self.name + '_') or len(tool.name) > 64:
            raise ValueError('tool must use its owning context namespace')
        tool = copy(tool)  # A failed registration must not mutate an existing caller's tool.
        original = tool.fn
        def execute(**args):
            self.check()
            return original(**args)
        tool.fn = execute
        dispose = self.host.tools.register(tool)
        self.disposers.append(dispose)
        return dispose

    def close(self):
        self.closed = True
        for dispose in reversed(self.disposers):
            dispose()
        self.disposers.clear()


class ContextHost:
    def __init__(self, session, tools, *, role='executor'):
        self.session, self.tools, self.role = session, tools, role
        self.scope = request_scope(session, role)
        self.task = getattr(session, 'task_input', {}).get('id')
        if not self.task:
            raise ValueError('context plugins require a host task identity')
        self.providers, self.modules = ScopedRegistry(), {}

    def mount(self, name, setup, *, source, validate):
        if not re.fullmatch('[0-9a-f]{32}', name) or name in self.modules:
            raise ValueError('invalid or duplicate context plugin')
        module = ContextModule(self, name, source, validate)
        try:
            module.check()
            setup(module)
        except BaseException:
            module.close()
            raise
        self.modules[name] = module
        def dispose():
            module.close()
            if self.modules.get(name) is module:
                del self.modules[name]
        return dispose

    def contribute(self, facts):
        # Revoked handles disappear before constructing the complete snapshot.
        for name, module in list(self.modules.items()):
            try:
                module.check()
            except W.Problem:
                module.close()
                del self.modules[name]
        for name, (_, read, source) in sorted(self.providers.resolve(self.scope).items(),
                                              key=lambda item: (item[1][0], item[0])):
            facts.register(name, read, source=source, trust='plugin-data')

    def close(self):
        for module in self.modules.values():
            module.close()
        self.modules.clear()


def install(session, tools, packages):
    """Mount only explicitly enabled, captured packages; never import their code."""
    from . import plugins
    host = ContextHost(session, tools)
    for package in packages:
        if not package.get('contextEnabled'):
            continue
        pid = package['id']
        def validate(pid=pid):
            plugins.authorized(session.dir.name, pid, 'contextEnabled')
        def setup(module, package=package):
            for row in declarations(package.get('contextStores', [])):
                store = module.state(row['name'], row['initial'])
                prefix = 'ctx_' + module.name + '_' + row['name']
                read_name, write_name = prefix + '_read', prefix + '_write'
                def read(store=store):
                    return dict(ok=True, **store.read())
                def write(value, expected_revision, store=store):
                    return dict(ok=True, **store.write(value, expected_revision))
                module.tool(Tool(read_name, '读取本任务插件状态：' + row['description'],
                    dict(type='object', properties={}, additionalProperties=False), read,
                    zone='offflow', kind='read', readonly=True))
                module.tool(Tool(write_name, '更新本任务插件状态，不修改项目文件：' + row['description'],
                    dict(type='object', properties=dict(value={}, expected_revision=dict(type='integer', minimum=0)),
                         required=['value', 'expected_revision'], additionalProperties=False), write,
                    zone='offflow', kind='meta'))
                module.context(row['name'], lambda store=store, r=read_name, w=write_name:
                    dict(**store.read(), readTool=r, writeTool=w))
        try:
            host.mount(pid, setup, source=dict(pluginId=pid, fingerprint=package['fingerprint']), validate=validate)
        except BaseException:
            host.close()
            raise
    return host
