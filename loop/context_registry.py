"""Host-owned named contributions. Scope selects definitions, never authority.

Insertion order is deliberate: legacy tool schemas must keep their wire order.
Prompt consumers may explicitly sort the resolved values by order and name.
"""

# 主要引用方包括 `tools.py` 和 `memory_providers.py`。

ROOT = ()


def checked_scope(scope):
    if not isinstance(scope, tuple) or any(not isinstance(p, str) or not p for p in scope):
        raise ValueError('Scope must be a tuple of non-empty host-owned names')
    return scope


class ScopedRegistry:
    def __init__(self, *, root=None):
        self._scopes = {ROOT: root if root is not None else {}}

    def bind_root(self, values):
        """Bridge legacy registries whose root dictionary can be replaced."""
        self._scopes[ROOT] = values

    def register(self, name, value, *, scope=ROOT, replace=False):
        checked_scope(scope)
        if not isinstance(name, str) or not name:
            raise ValueError('Contribution requires a name')
        values = self._scopes.setdefault(scope, {})
        if name in values and not replace:
            raise ValueError('Duplicate contribution: ' + name)
        values[name] = value
        return self

    def resolve(self, scope=ROOT):
        checked_scope(scope)
        values = {}
        for length in range(len(scope) + 1):
            values.update(self._scopes.get(scope[:length], {}))
        return values

    def effect(self, name, value, *, scope=ROOT):
        """Register one owned definition; its idempotent disposer cannot remove a replacement."""
        self.register(name, value, scope=scope)
        disposed = False
        def dispose():
            nonlocal disposed
            if disposed:
                return
            disposed = True
            values = self._scopes.get(scope, {})
            if name in values and values[name] is value:
                del values[name]
                if scope and not values:
                    self._scopes.pop(scope, None)
        return dispose

    def copy(self):
        other = ScopedRegistry()
        other._scopes = {scope: dict(values) for scope, values in self._scopes.items()}
        return other


def request_scope(session, role):
    """Local provenance only; no paths, ids or scopes are added to model text."""
    from .context_manager import fingerprint
    owner = fingerprint(str(session.dir))
    task = getattr(session, 'task_input', {}).get('id') or ''
    project = getattr(session, 'context_project_id', None)
    prefix = ('project:' + str(project),) if project else ROOT
    return prefix + ('session:' + owner, 'role:' + role, 'task:' + str(task))
