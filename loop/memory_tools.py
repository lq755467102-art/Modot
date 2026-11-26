"""Memory tools inherit the original memory_tiers; callers cannot pick a project."""

# 主要引用方包括 `tools.py`。

from . import (
    execution_policy as E,
    memperms as MP,
    memory_service as M,
)
from .memory_tree import error


# 从 `memory_providers.py` 取得当前会话的模型租约，提供该实现实际支持的记忆工具。
def tools(mem, root):
    from .memory_providers import acquire
    from .tools import current_call
    thread = E.binding(root) if root else None
    if not thread:
        return []
    lease = acquire(thread['id'], model=True)
    if not lease.available:
        return []
    result = _tree_tools(mem, root) if lease.identity['adapter'] == 'tree' else _notes_tools(mem, root, lease)
    # 为原提供方工具封装运行与授权核对。
    def bind(fn, write):
        # 每次调用重验租约与原模型动作，切换提供方后的旧工具句柄不可用。
        def call(**kw):
            owner = current_call() or {}
            if owner.get('sessionId') != thread['id'] or not owner.get('runId'):
                error('记忆调用需要当前会话动作身份', 'memory_denied',403)
            return lease.execute(lambda: fn(**kw), write=write)
        return call
    for tool in result:
        tool.fn = bind(tool.fn, not tool.readonly)
        tool.memory_provider = lease
    return result


# 按 `memperms.py` 组装内置树工具，对读、写和候选操作分开授予入口。
def _tree_tools(mem, root):
    from .tools import Tool, current_call

    thread = E.binding(root) if root else None
    if not thread:
        return []
    ident = thread["id"]
    out = []

    # 使用当前动作和节点范围分流内置树操作，禁止绕开会话身份直接写库。
    def invoke(operation, **body):
        owner = current_call() or {}
        if owner.get("sessionId") != ident or not owner.get("runId"):
            error("缺少当前动作身份", "memory_denied", 403)
        if not MP.at_least(mem, "facts", "edit"):
            error("没有事实维护权限", "memory_denied", 403)
        store, _, _ = (M.target(ident) if operation == 'compare' else M.node_target(ident, body.get('id')))
        existing = store.load()['nodes'].get(body.get('id'))
        if existing and not M.can_read_node(existing, mem):
            error('没有此记忆分支的读取权限', 'memory_denied', 403)
        if operation == 'promote' and existing:
            target_scope = body.get('targetScope') or ('project' if existing.get('scope') == 'session' else 'habit')
            branch = 'rules' if target_scope == 'project' else 'habits'
            if not MP.at_least(mem, branch, 'propose'):
                error('没有目标记忆分支的提案权限', 'memory_denied', 403)
        return dict(
            ok=True,
            **M.mutate(
                ident,
                dict(
                    body,
                    operation=operation,
                    expectedRevision=body.get(
                        "expectedRevision", store.load()["revision"]
                    ),
                ),
                "model",
            ),
        )

    # 在权限档允许时用同一入口包装注册 schema 和业务函数。
    def add(name, desc, props, required, fn, readonly=False):
        out.append(
            Tool(
                name,
                desc,
                dict(type="object", properties=props, required=required),
                fn,
                zone="offflow",
                readonly=readonly,
                kind="read" if readonly else "output",
            )
        )

    S = {"type": "string"}
    if MP.at_least(mem, "facts", "read"):
        add(
            "read_memory_tree",
            "查看本会话/项目/习惯节点、作用域与来源。id/version 可定点取回有权但超预算的正文；关闭或撤权不可回取。storeRevision 是该节点所属库的编辑版本。",
            {"offset": {"type": "integer"}, "id": S, "version": {"type": ['integer','string']}},
            [],
            lambda offset=0, id=None, version=None: dict(ok=True, **M.model_page(
                ident, offset, tiers=mem, node_id=id, version=version)),
            True,
        )
    if MP.at_least(mem, "facts", "edit") and not thread.get("readonlyEvidence"):
        add(
            "remember",
            "将可复用事实采集到本会话；不自动共享到项目。保留当前动作来源，空白无需保存。",
            dict(text=S),
            ["text"],
            lambda text: M.tool_remember(root, mem, text),
        )
        add(
            "organize_memory_fact",
            "将可复用知识增量凝练到本会话；推断不得声称已验证。更新用原 id 和 expectedVersion，或节点的 storeRevision 作为 expectedRevision；保留关闭/退役状态。",
            dict(
                id=S,
                title=S,
                body=S,
                key=S,
                layer={"type": "array", "items": S},
                priority={"type": "integer"},
                weight={"type": "integer"},
                expectedRevision={"type": "integer"},
                expectedVersion={"type": "integer"},
                kind={"enum": ["observation", "decision", "inference"]},
                entryType={"enum": ['fact', 'rule']},
            ),
            ["title", "body"],
            lambda body, **kw: M.tool_remember(root, mem, body, **kw),
        )
        add(
            "record_memory_evidence",
            "记录真实检查、实际使用或具体反例尝试。applied 需检查 operationId 和适用说明 note；challenge 需 attempt 与 found/not_found。不能伪造用户确认或反例解决。",
            dict(id=S, operationId=S, kind={'enum': ['check', 'applied', 'challenge', 'counterexample']},
                note=S, attempt=S, outcome={'enum': ['found', 'not_found']}),
            ["id"],
            lambda kind='check', **kw: invoke("evidence", kind=kind, **kw),
        )
        add(
            "compare_memory_choices",
            "两条适用且互斥的做法需明确裁决时调用。近权重将暂停写入并询问用户；不能比较无关事实。",
            dict(ids={"type": "array", "items": S}, question=S),
            ["ids", "question"],
            lambda **kw: invoke("compare", **kw),
        )
        if MP.at_least(mem, "rules", "propose") or MP.at_least(mem, 'habits', 'propose'):
            add(
                "propose_learned_habit",
                "为会话到项目、项目到用户习惯提出升格候选。实际使用须有独立复发/跨项目依据；准则须有具体反例尝试。沿用人工采纳，不直接生效，不阻塞主任务。",
                dict(id=S, title=S, body=S, targetScope={'enum': ['project', 'habit']}),
                ["id"],
                lambda **kw: invoke("promote", **kw),
            )
    return out


# 便签提供方仅开放它所支持的会话正文与设置相关能力。
def _notes_tools(mem, root, lease):
    from .tools import Tool
    thread = E.binding(root)
    common = dict(type='object', properties={})
    out = []
    if MP.at_least(mem, 'facts', 'read'):
        out.append(Tool('read_memory_tree', '读取当前记忆实现的会话便签。可传 id/version 回取有权正文；没有项目或习惯树。',
            dict(type='object', properties=dict(offset=dict(type='integer'),id=dict(type='string'),version=dict(type='integer'))),
            lambda offset=0,id=None,version=None: dict(ok=True, **lease.adapter.model_page(offset,mem,id,version)),
            zone='offflow',readonly=True,kind='read'))
    if MP.at_least(mem, 'facts', 'edit') and not thread.get('readonlyEvidence'):
        out.append(Tool('remember','保存一条本会话便签，不共享到项目。',
            dict(type='object', properties=dict(text=dict(type='string')),required=['text']),
            lambda text:lease.adapter.remember(root,mem,text),zone='offflow',kind='output'))
        out.append(Tool('organize_memory_fact','增量维护本会话便签。更新用原 id 与 expectedVersion；此实现不提供升格。',
            dict(type='object',properties={k:dict(type='integer' if k in {'expectedVersion','expectedRevision'} else 'string')
                for k in ('id','title','body','expectedVersion','expectedRevision')},required=['title','body']),
            lambda body,**kw:lease.adapter.remember(root,mem,body,**kw),zone='offflow',kind='output'))
    return out
