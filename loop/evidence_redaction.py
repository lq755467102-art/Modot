"""Safe evidence projection; never use redacted values as source file contents."""

# 主要引用方包括 `workbench.py` 和 `code_service.py`。

import re
import subprocess
from . import leaks, private_keys

SENSITIVE = re.compile(
    r"(?i)(?:password|passwd|secret|api[_-]?key|authorization|credential|access[_-]?token|auth[_-]?token|private[_-]?key|^token$)"
)
MASK = "«凭据已遮»"


def redact(value):
    if isinstance(value, str):
        return leaks.redact(private_keys.text(value))[0]
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        named_secret = bool(SENSITIVE.search(str(value.get("name", ""))))
        return {
            k: 0
            if named_secret and k == "variablesReference"
            else MASK
            if SENSITIVE.search(k)
            or (named_secret and k in {"value", "result", "evaluateName"})
            else redact(v)
            for k, v in value.items()
        }
    return value


def _safe_command_arguments(argv):
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
        return None
    rendered = []
    secret_flag = re.compile(r"(?i)^--?(?:password|passwd|token|access-token|api[-_]?key|secret|user|u)$")
    mask_next = False
    for arg in argv:
        safe = MASK if mask_next else redact(arg)
        if not mask_next:
            safe = re.sub(r"(?i)((?:https?|ssh)://)[^/\s@]+:[^/\s@]+@", r"\1" + MASK + "@", safe)
            safe = re.sub(r"(?i)((?:proxy-)?authorization\s*:\s*(?:basic|bearer)\s+)[^\s'\x22]+", r"\1" + MASK, safe)
            safe = re.sub(r"(?i)(--?(?:password|passwd|token|access-token|api[-_]?key|secret|user)(?:=|\s+))([^\s'\x22]+|'[^']*'|\x22[^\x22]*\x22)", r"\1" + MASK, safe)
        rendered.append(safe)
        mask_next = bool(secret_flag.fullmatch(arg)) or bool(re.search(r"(?i)authorization:\s*(?:bearer|basic)?\s*$",arg))
    return rendered


def terminal_command(argv):
    """Display the original Windows argv after removing credentials across argument boundaries."""
    safe = _safe_command_arguments(argv)
    return subprocess.list2cmdline(safe) if safe is not None else None


def redact_tool_arguments(arguments):
    visible = redact(arguments)
    if isinstance(visible, dict) and 'argv' in visible:
        safe = _safe_command_arguments(visible['argv'])
        if safe is not None:
            visible['argv'] = safe
    return visible
