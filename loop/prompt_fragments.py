"""Named, ordered prompt contributions. Text never grants resource authority."""
# 主要引用方包括 `loop.py` 和 `memory_runtime.py`。
from dataclasses import dataclass
from .context_registry import ROOT, ScopedRegistry


# `PromptFragments` 注册的不可变贡献。
# `name` 是作用域内的键，`order` 控制拼接顺序，`category` 用于请求来源归因，`text` 是发往模型的正文。
@dataclass(frozen=True)
class Fragment:
    name: str
    text: str
    order: int
    category: str = 'system'


class PromptFragments:
    # 注册器按确定的作用域组织提示词贡献。
    def __init__(self, *, scope=ROOT):
        self.scope = scope
        self._registry = ScopedRegistry()

    # 用名称和类别标识贡献，并将顺序与正文交给注册器。
    def add(self, name, text, order=0, *, category='system', replace=False, scope=None):
        if not isinstance(name, str) or not name or not isinstance(text, str) or type(order) is not int:
            raise ValueError('Invalid prompt fragment')
        self._registry.register(name, Fragment(name, text.strip(), order, category),
            scope=self.scope if scope is None else scope, replace=replace)
        return self

    # 复制现有贡献供一次请求增减，原注册器继续服务其他请求。
    def copy(self):
        other = PromptFragments(scope=self.scope)
        other._registry = self._registry.copy()
        return other

    # 按贡献排序值和注册键给出稳定组装次序。
    def ordered(self):
        return sorted((f for f in self._registry.resolve(self.scope).values() if f.text), key=lambda f: (f.order, f.name))

    # 只需要提示正文时将排序后的贡献拼接成系统文字。
    def render(self):
        return '\n\n'.join(f.text for f in self.ordered())

    # 构造带类别的段供 `context_manager.py` 保留请求归因。
    def parts(self):
        return tuple((f.category, f.text) for f in self.ordered())
