"""One selected memory adapter. Plugin storage/config owns its lifetime.

No code loader, migration, fallback, model loop or history ownership lives here.
Trusted host adapters register at the existing ScopedRegistry seam.
"""
# 主要引用方包括 `charter.py` 和 `plugins.py`。
from copy import deepcopy
from contextlib import nullcontext
from pathlib import Path

from . import workbench as W, plugins
from .context_registry import ScopedRegistry
from .filelock import FileLock

BUILTIN = 'builtin-memory-tree'
NAMES = {'remember', 'organize_memory_fact', 'read_memory_tree', 'record_memory_evidence',
         'compare_memory_choices', 'propose_learned_habit', 'edit_memory', 'propose_habit'}
_adapters = ScopedRegistry()


# 宿主注册受信任的适配器工厂。插件目录里的声明由 declaration 单独验证。
def register_adapter(name, factory):
    if not isinstance(name, str) or not name or not callable(factory):
        raise ValueError('Memory adapter requires a name and factory')
    return _adapters.effect(name, factory)


def declaration(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'adapter'} or value['adapter'] != 'session-notes':
        raise ValueError('memoryProvider supports the data-only session-notes adapter; no paths or executable code')
    return deepcopy(value)


def _config(data):
    value = deepcopy(data.get('memoryProvider', dict(providerId=BUILTIN, revision=0, generation=0, builtinInstalled=True)))
    if (set(value) != {'providerId','revision','generation','builtinInstalled'} or type(value['revision']) is not int
            or value['revision'] < 0 or type(value['builtinInstalled']) is not bool
            or type(value['generation']) is not int or value['generation'] < 0
            or value['providerId'] is not None and not isinstance(value['providerId'], str)):
        raise W.Problem('memory_configuration', '记忆实现配置不可核对，未重置或回退', 503)
    return value


def settings():
    data = plugins.state()
    config = _config(data)
    providers = [dict(id=BUILTIN, name='默认记忆树', adapter='tree', builtin=True,
        installed=config['builtinInstalled'], description='会话采集、项目升格、用户习惯和反例/权重管理。')]
    providers += [dict(id=p['id'], name=p['name'], adapter=p['memoryProvider']['adapter'], builtin=False,
        installed=True, description='独立的会话便签，只保存/读取本会话，不提供项目或习惯升格。')
        for p in data['plugins'].values() if not p.get('removed') and p.get('memoryProvider')]
    return dict(config, providers=providers, notice='选择实现不迁移数据；停用/卸载保留原记忆。新启用下一轮生效，撤销立即生效。')


def _select(data, ident):
    config = _config(data)
    if ident == BUILTIN:
        if not config['builtinInstalled']:
            raise W.Problem('memory_unavailable', '默认记忆树已卸载，请先明确恢复安装', 409)
    elif ident is not None:
        p = data['plugins'].get(ident)
        if not p or p.get('removed') or not p.get('memoryProvider'):
            raise W.Problem('memory_unavailable', '所选记忆实现未安装或不支持记忆接口', 409)
    if config['providerId'] == ident:
        return config
    for p in data['plugins'].values():
        enabled = p['id'] == ident
        if bool(p.get('memoryEnabled')) != enabled:
            p['memoryEnabled'] = enabled
            p['revision'] += 1
    config.update(providerId=ident, revision=config['revision'] + 1, generation=config['generation']+1)
    data['memoryProvider'] = config
    return config


def change(body):
    if set(body) - {'providerId','operation','expectedRevision','confirm','_request_id'}:
        raise W.Problem('memory_configuration', '记忆实现配置包含未知字段')
    with FileLock(plugins.directory()/'store.lock'):
        data = plugins.state()
        config = _config(data)
        if body.get('expectedRevision') != config['revision']:
            raise W.Problem('memory_stale', '记忆实现设置已变化，请刷新后核对', 409)
        operation = body.get('operation', 'select')
        if operation in {'uninstall-builtin','restore-builtin'}:
            installed = operation == 'restore-builtin'
            if not installed and config['providerId'] == BUILTIN:
                config = _select(data, None)
            config = dict(config, builtinInstalled=installed, revision=config['revision']+1)
            data['memoryProvider'] = config
        elif operation == 'select' and 'providerId' in body:
            ident = body['providerId']
            if ident is not None and ident != BUILTIN:
                p = data['plugins'].get(ident)
                if not p or plugins.packages.inspect(p['root'])['fingerprint'] != p['fingerprint']:
                    raise W.Problem('plugin_changed', '记忆插件版本已变化，未启用', 409)
            _select(data, ident)
        else:
            raise W.Problem('memory_configuration', '没有此记忆实现设置动作')
        plugins.save(data)
    return settings()


def default_active():
    try:
        config = _config(plugins.state())
        return config['providerId'] == BUILTIN and config['builtinInstalled']
    except (W.Problem, OSError, ValueError):
        return False


class TreeAdapter:
    capabilities = dict(read=True, save=True, configure=True, promote=True, evidence=True, weights=True)

    def __init__(self, lease):
        self.lease = lease

    def view(self, **kwargs):
        from .memory_service import resolve
        return resolve(self.lease.ident, **kwargs)

    def prepare(self, session, role, body):
        from .memory_runtime import _prepare_tree_body
        return _prepare_tree_body(session, role, body)


register_adapter('tree', TreeAdapter)


class Lease:
    def __init__(self, ident, identity, factory=None, *, session=None, model=False, historical=False):
        self.ident, self.identity, self.factory = ident, identity, factory
        self.session, self.model, self.historical = session, model, historical
        self.adapter = factory(self) if factory else None

    # 运行中的读写继续核对所选实现与启用代次。只读历史经过原适配器和原包指纹的检查。
    def check(self):
        if not self.factory or _adapters.resolve().get(self.identity['adapter']) is not self.factory:
            raise W.Problem('memory_unavailable', '记忆实现未注册或已撤销；主任务可正常继续', 409)
        try:
            data = plugins.state()
        except (OSError, ValueError) as error:
            raise W.Problem('memory_unavailable','记忆实现状态不可读取，未回退或重置',503) from error
        if not self.historical:
            config = _config(data)
            if (config['providerId'] != self.identity['id'] or config['generation'] != self.identity['generation']
                    or self.identity['id'] == BUILTIN and not config['builtinInstalled']):
                raise W.Problem('memory_unavailable', '记忆实现已停用、替换或卸载；不会回退保存', 409)
        if self.identity['id'] != BUILTIN:
            p = data['plugins'].get(self.identity['id'])
            if not p or p['fingerprint'] != self.identity['fingerprint'] or not self.historical and p.get('removed'):
                raise W.Problem('memory_unavailable', '原记忆插件身份不可核对，未改用其他存储', 409)
            if self.model and not self.historical:
                plugins.authorized(self.ident, p['id'], 'memoryEnabled')

    @property
    def available(self):
        try:
            self.check()
            return True
        except W.Problem:
            return False

    def unavailable(self, *, write=False, unknown=False):
        return dict(ok=False, code='memory_write_unknown' if unknown else 'memory_unavailable',
            memoryProvider=deepcopy(self.identity), nodes=[],
            **(dict(memoryWrite=dict(status='unknown' if unknown else 'not_saved')) if write else {}),
            notice='记忆未保存或结果未知；不会重放到其他实现。主任务可正常继续。' if write else '当前记忆实现不可用；主任务可正常继续。')

    def execute(self, fn, *, write=False):
        returned, result = False, {}
        try:
            # Same lock as select/uninstall: either the original provider commits
            # before revocation, or it observes revocation without writing.
            with FileLock(plugins.directory()/'store.lock') if write else nullcontext():
                self.check()
                if write and self.historical:
                    raise W.Problem('memory_denied', '历史记忆只能审查', 403)
                result = fn()
                returned = True
                self.check()
            if not self.available:
                return self.unavailable(write=write, unknown=write)
            return dict(result, memoryProvider=deepcopy(self.identity),
                **(dict(memoryWrite=result.get('memoryWrite',dict(status='saved' if result.get('ok', True) else 'not_saved'))) if write else {}))
        except W.Problem as error:
            unknown = write and (getattr(error, 'memory_write_unknown', False) or
                returned and result.get('ok', True) and result.get('memoryWrite',{}).get('status') != 'not_saved')
            receipt = self.unavailable(write=write, unknown=unknown)
            return dict(receipt, code=receipt['code'] if unknown else error.code,
                error=error.message, sourceError=error.code)
        except (OSError, ValueError):
            return self.unavailable(write=write, unknown=write)


def acquire(ident=None, *, session=None, model=False, identity=None, historical=False):
    try:
        data = plugins.state()
        config = _config(data)
    except (W.Problem, OSError, ValueError):
        lease = Lease(ident,dict(id=None,generation=0,adapter=None,fingerprint=None),session=session,model=model)
        lease.reason = '记忆实现配置不可读取，原文件保留；没有重置或启用后备实现。'
        return lease
    if identity is None:
        pid = config['providerId']
        p = data['plugins'].get(pid, {})
        adapter = 'tree' if pid == BUILTIN else (p.get('memoryProvider') or {}).get('adapter')
        identity = dict(id=pid, generation=config['generation'], adapter=adapter,
            fingerprint='tree-v1' if pid == BUILTIN else p.get('fingerprint'))
    factory = _adapters.resolve().get(identity.get('adapter'))
    # Invalid/revoked selections remain unavailable; never choose the default.
    lease = Lease(ident, deepcopy(identity), factory, session=session, model=model, historical=historical)
    return lease


# 同一轮请求复用绑定的实现。启停或替换后，原绑定由 Lease.check 判定为已撤销。
def request_lease(session):
    lease = getattr(session, 'memory_provider', None)
    if lease is None:
        lease = acquire(Path(session.dir).name, session=session, model=True)
        session.memory_provider = lease
    return lease


def empty_view(ident=None, project_id=None, *, lease=None):
    from .memory_tree import digest
    from .memory_service import requests
    return dict(revision=0, scopeRevisions={}, workspaceContextId=None, projectId=project_id,
        conversationId=ident, mode='off', nodes=[], selectionVersion=digest(['no-memory', lease.identity if lease else None]),
        conflicts=[], history=[], historyArchives=[], sessionMemory='', requests=requests(ident),
        notice=getattr(lease,'reason','未启用可用的记忆实现。已有数据保留，普通任务无需等待配置。'),
        provider=dict(identity=lease.identity if lease else None, available=False, capabilities={}))


def public_view(lease, **kwargs):
    from .memory_service import page
    if not lease.available:
        return page(empty_view(lease.ident, kwargs.get('project_id'), lease=lease))
    result = lease.execute(lambda: lease.adapter.view(**kwargs))
    if result.get('ok') is False:
        raise W.Problem(result['code'], result.get('error', result['notice']), 503)
    result['provider'] = dict(identity=lease.identity, available=True, capabilities=lease.adapter.capabilities)
    return result


from .memory_notes import NotesAdapter
register_adapter('session-notes', NotesAdapter)
