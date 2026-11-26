#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""leaks.py —— 内容里「是不是一把凭据」，以及怎么处理

为什么需要这一层（和 `baseline.py` 的分工）
    `baseline` 按**路径**判：`~/.ssh/**`、`id_rsa`、`*.env`……
    但真实投喂里最常见的不是"去读私钥"，而是**别的地方躺着一把 key**：
    一段日志、一个 `config.yaml`、别人贴过来的 curl、`git diff` 里的旧配置。
    路径规则对这类完全无效 —— 文件名一点都不敏感，敏感的是内容。

它做什么
    `scan(text)`   找出可疑片段（**高置信度**的几种，不是"凡长串都可疑"）
    `redact(text)` 把值换成 `«凭据已遮:类型»`，并回一份清单（第几行、什么类型）
    `has_mask()`   这段内容里有没有我们遮过的标记（写回文件前必须挡）
    `has_secret()` 这段内容里**有没有该遮的东西**（要遮用 `redact`）

★★ 为什么是"遮"而不是"拒"
    读文件被拒的代价是"这个活干不下去了"，而人想要的通常是"让它看，但别把 key 带出去"。
    遮掉之后：模型照样能分析这段日志（它需要知道"这里有一把 key"，不需要那把 key 的值），
    而这把 key **不会被发到模型服务商**—— 那才是真实伤害。
    `baseline` 那种"直接拒"留给"去读私钥"这种明确不该做的事；这一层管"顺手带出去"。

★★ 遮掉的代价是什么，以及怎么防
    被遮的内容如果被原样写回文件，文件就被写坏了（`«凭据已遮:…»` 会变成真的内容）。
    所以 `write_file` / `edit_file` 一律**拒收带标记的内容**（`has_mask`）。
    这不是补丁，这是这一层成立的前提：**不许把遮过的文本当成原文用**。

它明确不做
    · 不判断"这个 key 还有没有效"（那要联网，而且会把 key 发出去）
    · 不猜未知格式的长串（宁可漏一个没见过的格式，也不要天天误报 ——
      误报多了人会把它关掉，关掉之后连真的也挡不住）
"""
# 主要引用方包括 `tools.py` 和 `evidence_redaction.py`。
from __future__ import annotations

import ast
import io
import re
import tokenize

MARK = "«凭据已遮:%s»"
MARK_HEAD = "«凭据已遮"

# ── 认什么 ────────────────────────────────────────────────────────
# 三条规矩：
#   ① 只收**形状极强**的（私钥块、`sk-`、`AKIA`、`ghp_`…）——
#      这些误报率极低，宁缺勿滥；
#   ② 加一类"**名字 + 值**"的（`api_key = "…"`、`password: …`），
#      因为配置文件里最普通的就是这种；
#   ③ 值那一组才遮，**名字留着** —— `API_KEY=«凭据已遮»` 照样能看懂。
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "私钥"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "sk-key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws-key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "github-token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "slack-token"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "google-key"),
]

_CONFIG_KIND = "配置里的密钥"
_CONFIG_NAME = re.compile(
    r"(?i)api[_-]?key|apikey|secret|passwd|password|access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key"
)
_CONFIG_ASSIGNMENT = re.compile(r"""(?i)(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+)["']?\s*[:=]\s*["']?""")
_CONFIG_VALUE = re.compile(r"(?i)[A-Za-z0-9_\-+/=.]{16,}")
_WORD_BOUNDARY = re.compile(r"\b")

# 值长成这样才算（上面那条通用规则的补充：一眼是占位符/示例值的放行）
_PLACEHOLDER = re.compile(r"(?i)^(x{4,}|\*{4,}|\.{4,}|changeme|your[_-]?|example|"
                          r"placeholder|todo|\$\{|<.*>)")


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _config_spans(text):
    # Look at each maximal key token once. Retrying a growing prefix from
    # every word boundary makes concatenated streaming IDs quadratic.
    consumed = 0
    for assignment in _CONFIG_ASSIGNMENT.finditer(text):
        if assignment.start() < consumed:
            continue
        start, end = assignment.span(1)
        boundary = _WORD_BOUNDARY.search(text, start, end)
        if boundary is None or not _CONFIG_NAME.search(text, boundary.start(), end):
            continue
        value = _CONFIG_VALUE.match(text, assignment.end())
        if value:
            consumed = value.end()
            if not _PLACEHOLDER.match(value[0]):
                yield value.start(), consumed, _CONFIG_KIND


def _spans(text: str) -> list[tuple[int, int, str]]:
    """所有命中区间，去掉重叠（重叠时留更长的那个）。"""
    raw = list(_config_spans(text))
    raw.extend((m.start(), m.end(), label) for pat, label in _PATTERNS for m in pat.finditer(text))
    raw.sort()
    keep: list[tuple[int, int, str]] = []
    for a, b, lab in raw:
        if keep and a < keep[-1][1]:
            if (b - a) > (keep[-1][1] - keep[-1][0]):
                keep[-1] = (a, b, lab)
            continue
        keep.append((a, b, lab))
    return keep


def scan(text: str) -> list[dict]:
    """有哪些可疑片段。**不返回原文**，只给类型、行号、长度。

    ★ 为什么不给原文：这个结果会被写进事件、界面、诊断包 —— 任何一处留明文，
      这一层就白做了。
    """
    t = str(text or "")
    if not t:
        return []
    out = []
    for a, b, lab in _spans(t):
        out.append({"kind": lab, "line": _line_of(t, a), "chars": b - a})
    return out


def redact(text: str) -> tuple[str, list[dict]]:
    """把值换成占位符，返回 (处理后的文本, 命中清单)。"""
    t = str(text or "")
    sp = _spans(t)
    if not sp:
        return t, []
    buf = []
    last = 0
    hits = []
    for a, b, lab in sp:
        buf.append(t[last:a])
        buf.append(MARK % lab)
        last = b
        hits.append({"kind": lab, "line": _line_of(t, a), "chars": b - a})
    buf.append(t[last:])
    return "".join(buf), hits


def has_mask(text: str) -> bool:
    """这段内容里有没有**我们打过的遮罩标记**（写回文件时必须挡住）。

    ★ 改名（2026-10-27）：原来叫 `is_tainted`，字面像「有脏东西/有密钥」，
      实际语义是「已经被我们遮过了」。备份那边照字面用了它去判断
      「这份文件里有没有密钥」，结果恒为 False —— 包里的 key 原样漏了出去。
      两个意思共用一个名字，代价就是这个。⇒ 现在分成两个：
        `has_mask`   = 已有遮罩（**别把遮罩当原文写回去**）
        `has_secret` = 真有密钥（**该遮**）
    """
    return MARK_HEAD in str(text or "")


def has_secret(text: str) -> bool:
    """这段内容里**有没有该遮的东西**。要遮请用 `redact()`，别自己replaced。"""
    return bool(_spans(str(text or "")))


def redact_source(text: str) -> str:
    """Redact a source file before diffing or slicing it for model context."""
    values = {text[a:b] for a, b, _ in _spans(text)}
    if not values:
        return text
    if len(values) > 128:
        return (MARK % "source") + "\n" * text.count("\n")
    pattern = re.compile("|".join(re.escape(v) for v in sorted(values, key=lambda v: (-len(v), v))))
    return pattern.sub(lambda m: (MARK % "source") + "\n" * m[0].count("\n"), text)


def candidate_python(text: str) -> str | None:
    """Keep importable Python without putting matched credentials in the copy."""
    bom = text.startswith("\ufeff")
    source = text.removeprefix("\ufeff")
    hits = _spans(source)
    if not hits:
        return text
    try:
        tree = ast.parse(source)
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (IndentationError, SyntaxError, tokenize.TokenError):
        return None
    lines = source.split("\n")
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    def offset(point):
        return starts[point[0] - 1] + point[1]

    def ast_offset(line, byte):
        return starts[line - 1] + len(lines[line - 1].encode("utf8")[:byte].decode("utf8"))

    nodes = list(ast.walk(tree))
    names = {node.name for node in nodes if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    names.update(node.asname or node.name.split(".")[0] for node in nodes if isinstance(node, ast.alias))
    names.update(node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
    names.update(node.arg for node in nodes if isinstance(node, ast.arg))
    references = {(ast_offset(node.lineno, node.col_offset), ast_offset(node.end_lineno, node.end_col_offset))
                  for node in nodes if isinstance(node, ast.Attribute)
                  or (isinstance(node, ast.Name) and node.id in names)}
    literals = [(offset(t.start), offset(t.end)) for t in tokens if t.type == tokenize.STRING]
    values = set()
    for a, b, kind in hits:
        if kind == _CONFIG_KIND and (a, b) in references:
            continue
        if not any(start < a and b < end for start, end in literals):
            return None
        values.add(source[a:b])
        if len(values) > 128:
            return None
    replacements = []
    for value in values:
        for match in re.finditer(re.escape(value), source):
            a, b = match.span()
            if not any(start < a and b < end for start, end in literals):
                return None
            replacements.append((a, b))
    spans = []
    for a, b in sorted(replacements):
        if spans and a < spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))
    for a, b in reversed(spans):
        source = source[:a] + "placeholder-harness-redacted" + source[b:]
    try:
        compile(source, "<candidate>", "exec")
    except (SyntaxError, ValueError):
        return None
    return ("\ufeff" if bom else "") + source


def summary(hits: list[dict]) -> str:
    """一句话（给界面/事件）。"""
    if not hits:
        return ""
    kinds: dict[str, int] = {}
    for h in hits:
        kinds[h.get("kind") or "?"] = kinds.get(h.get("kind") or "?", 0) + 1
    return ("内容里有 %d 处疑似凭据（%s），已遮掉 —— 值不会进模型上下文"
            % (len(hits), "、".join("%s %d" % (k, v) for k, v in kinds.items())))


def where(hits: list[dict], limit: int = 6) -> str:
    """第几行 —— 人排查时唯一有用的位置信息。"""
    lines = [str(h.get("line")) for h in hits[:limit]]
    more = "" if len(hits) <= limit else "…"
    return "第 %s 行%s" % ("、".join(lines), more)


def describe() -> str:
    """一行（进启动日志 / 诊断包）。"""
    return ("内容检测：%d 类（私钥 / sk- / AWS / GitHub / Slack / Google / 配置里的密钥），"
            "只遮值不遮名字，写回带标记的内容一律拒" % len(_PATTERNS))
