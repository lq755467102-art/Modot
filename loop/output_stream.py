"""Batch adjacent deltas without changing their observable order."""

# 主要引用方包括 `scoped_roles.py` 和 `bin/run-task.py`。

import time


# `emit` 由运行方提供。
# `kind` 和 `pending` 保存尚未发出的相邻片段，`last_emit` 用于限制等待时间，`received` 记录收到过的类别。
class OrderedOutput:
    def __init__(self, emit):
        self.emit = emit
        self.kind = None
        self.pending = ""
        self.last_emit = 0.0
        self.received = set()

    def add(self, kind, text):
        changed = kind != self.kind
        if changed:
            self.flush()
            self.kind = kind
        if kind not in {"think", "say"} or not text:
            return
        self.received.add(kind)
        self.pending += str(text)
        if changed or len(self.pending) >= 160 or time.monotonic() - self.last_emit >= 0.25:
            self.flush()

    def flush(self):
        if self.pending:
            self.emit(self.kind, self.pending)
            self.pending = ""
            self.last_emit = time.monotonic()
