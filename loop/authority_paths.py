"""Filesystem tools cannot rewrite the host, its import roots, or control state.

This guards the file APIs. Explicit local-process execution remains user-level
execution, not an operating-system sandbox.
"""

# 主要引用方包括 `baseline.py` 和 `workbench.py`。

import os
from pathlib import Path
import site
import sys
from . import paths as P


def canonical(path):
    return normalized(Path(path).resolve())


def normalized(path):
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith(("\\\\?\\", "\\\\.\\")):
        value = value[4:]
    return Path(value)


# `workbench.py` 和 `baseline.py` 等入口传入文件和动作类型。
# 读取保护会话状态，写入还检查宿主和导入根。
# 删除也要限制包含受保护内容的祖先目录。
def protected(path, action, scoped_app_resource=False, workspace_root=None):
    target = canonical(path)
    state = canonical(P.data_root() / "runs")
    if not scoped_app_resource and (
        target.is_relative_to(state)
        or action == "delete"
        and state.is_relative_to(target)
    ):
        return "protected_app_state"
    if action not in {"write", "delete"}:
        return None
    roots = [
        P.app_root(),
        Path(sys.prefix),
        Path(sys.base_prefix),
        P.data_root() / "harness",
        P.data_root() / ".harness",
        P.data_root() / "memory",
        Path.home() / ".harness",
    ]
    if workspace_root:
        roots.append(Path(workspace_root) / ".harness")
    roots += [Path(value or os.getcwd()) for value in sys.path]
    user_site = site.getusersitepackages()
    roots += [
        Path(value)
        for value in (user_site if isinstance(user_site, list) else [user_site])
        if value
    ]
    for root in roots:
        root = canonical(root)
        if (
            target.is_relative_to(root)
            or action == "delete"
            and root.is_relative_to(target)
        ):
            return "protected_runtime"
    files = [P.token_file()] + [
        Path(os.environ[key])
        for key in (
            "HARNESS_HOOKS",
            "HARNESS_MODELS",
            "HARNESS_CREDS",
            "HARNESS_GRANTS",
        )
        if os.environ.get(key)
    ]
    if any(
        target == canonical(path)
        or action == "delete"
        and canonical(path).is_relative_to(target)
        for path in files
    ):
        return "protected_app_state"
    return None
