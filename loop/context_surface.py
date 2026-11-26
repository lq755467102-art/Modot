"""One model-view editor and preparation seam, retaining legacy wire policy.

Source identity and positions live outside the provider dictionary. Canonical
messages/checkpoints stay the audit and resume source in this migration stage.
"""
# 主要引用方包括 `context_manager.py` 和 `memory_runtime.py`。
from copy import deepcopy

from .context_registry import ROOT, ScopedRegistry, request_scope


# `source` 保留 `context_manager.RequestBody` 的原消息和来源。
# `rows` 的每项保存消息、来源片段、原位置和贡献名，`scope` 仅用于选择贡献。
class ContextSurface:
    def __init__(self, body, *, scope=ROOT, empty=False):
        self.source = body
        self.scope = scope
        self.rows = []
        if not empty:
            for index, message in enumerate(body['messages']):
                self.rows.append((message, body.parts.get(index, ()), index, 'history'))

    def append_source(self, index, message=None):
        value = deepcopy(self.source['messages'][index]) if message is None else message
        self.rows.append((value, self.source.parts.get(index, ()), index, 'history'))

    def append(self, name, message, parts=()):
        self.insert(len(self.rows), [(name, message, parts)])

    def insert(self, index, contributions):
        registry = ScopedRegistry()
        for name, message, parts in contributions:
            registry.register(name, (message, parts, None, name), scope=self.scope)
        self.rows[index:index] = registry.resolve(self.scope).values()

    def remove(self, predicate):
        self.rows = [row for row in self.rows if not predicate(row[0])]

    # `runtime_facts.ensure_projected` 从当前会话补回运行事实。
    # 这里只给模型视图增补行，保留原消息索引。
    def restore_runtime(self, session):
        from .runtime_facts import ensure_projected, message_in_scope, scope_version
        value = getattr(session, 'runtime_facts', None)
        if value is None:
            return
        scope = scope_version(value)
        self.rows = [row for row in self.rows if message_in_scope(row[0], row[1], scope)]
        current = self.build()
        count = len(self.rows)
        ensure_projected(session, current['messages'], current.parts)
        for index in range(count, len(current['messages'])):
            self.append('runtime-facts', current['messages'][index], current.parts.get(index, ()))

    def build(self):
        from .context_manager import RequestBody
        parts = {i: row[1] for i, row in enumerate(self.rows)}
        body = RequestBody(dict(self.source, messages=[row[0] for row in self.rows]), parts)
        body.source_positions = {i: row[2] for i, row in enumerate(self.rows) if row[2] is not None}
        # Local provenance is not serialized by json.dumps(RequestBody).
        body.context_surface = dict(scope=self.scope, nodes=tuple(
            (row[3], row[2]) for row in self.rows))
        return body


def prepare_context(session, role, body):
    """Resolve the existing request contributors once in their original order."""
    from .memory_runtime import prepare_body
    from .context_view import enabled, project

    scope = request_scope(session, role)
    registry = ScopedRegistry().register('selected-memory', prepare_body)
    if enabled(session, role):
        from .workspace_instructions import prepare_body as prepare_instructions
        registry.register('project-instructions', prepare_instructions, scope=scope)
        registry.register('working-view', project, scope=scope)
    stages = registry.resolve(scope)
    for prepare in stages.values():
        body = prepare(session, role, body)
    from .context_manager import RequestBody
    if isinstance(body, RequestBody):
        body.context_assembly = dict(scope=scope, contributors=tuple(stages))
    return body
