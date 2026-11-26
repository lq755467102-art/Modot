"""Optional audit metadata in the existing final reply."""

# 主要引用方包括 `loop.py` 和 `scoped_roles.py`。

import json
import re

from .evidence_redaction import redact

START = "<harness-review-notes>"
END = "</harness-review-notes>"
INSTRUCTION = (
    "\n最终公开回复后，可以另起一行附上供工具审查使用的短摘要。只在收尾时附加，"
    "最多两条，每条用自然语言说明做了什么、实际结果或未能核对的事项，"
    "actions 使用工具回执中的真实 actionId。只引用本轮记录，不推断未完成的验证。"
    "测试者说明自己的实际检查，回查别人的记录时明确说是回查。"
    "格式为 " + START + '[{"text":"说明","actions":["动作ID"]}]' + END
)
ACTION = re.compile(r"[A-Za-z0-9_-]{1,128}")


def normalize(items):
    if not isinstance(items, list):
        return []
    result = []
    for item in items[:2]:
        if not isinstance(item, dict):
            continue
        text, refs = item.get("text"), item.get("actions")
        if (not isinstance(text, str) or not text.strip() or not isinstance(refs, list)
                or len(refs) > 8 or any(not isinstance(ref, str) or not ACTION.fullmatch(ref) for ref in refs)):
            continue
        result.append(dict(text=redact(" ".join(text.split())[:320]), actions=list(dict.fromkeys(refs))))
    return result


def split(text):
    public, marker, tail = (text or "").partition(START)
    if not marker:
        return public, []
    payload, closed, trailing = tail.partition(END)
    if not closed or trailing.strip() or len(payload) > 4096:
        return public.rstrip(), []
    try:
        return public.rstrip(), normalize(json.loads(payload))
    except (ValueError, RecursionError):
        return public.rstrip(), []


class PublicStream:
    """Hold only a possible marker prefix, including across arbitrary chunks."""

    def __init__(self, forward, *, private_start=()):
        self.forward = forward
        self.markers = (START, *private_start)
        self.pending = ""
        self.private = False

    def add(self, kind, value):
        if kind == 'interrupted':
            self.pending = ''
            self.private = False
            self.forward(kind, value)
            return
        if kind != "text" or not isinstance(value, str):
            self.forward(kind, value)
            return
        if self.private:
            return
        text = self.pending + value
        self.pending = ""
        starts = [index for marker in self.markers if (index := text.find(marker)) >= 0]
        if starts:
            self.private = True
            text = text[:min(starts)]
        else:
            limit = min(len(text), max(map(len, self.markers)) - 1)
            for length in range(limit, 0, -1):
                if any(marker.startswith(text[-length:]) for marker in self.markers):
                    self.pending = text[-length:]
                    text = text[:-length]
                    break
        if text:
            self.forward(kind, text)

    def flush(self):
        if self.pending:
            self.forward("text", self.pending)
            self.pending = ""
