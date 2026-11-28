"""Bounded scans of authorized project UTF-8 text. Cursors locate bytes; callers recheck scope."""

import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path

from . import workbench as W
from .filelock import FileLock, atomic_json

SCAN_BYTES = 48 * 1024 * 1024
SCAN_SECONDS = 5
READ_BYTES = 50 * 1024
LINE_CHARS = 2000


def _signature(payload, binding):
    # A model can copy the short position across tool calls and process restarts.
    # Authenticate the line/byte mapping before treating it as source evidence.
    key_path = W.home() / "project-text-cursor-key.json"
    if not key_path.exists():
        with FileLock(key_path.with_suffix(".lock")):
            if not key_path.exists():
                atomic_json(key_path, os.urandom(32).hex())
    key = bytes.fromhex(json.loads(key_path.read_text(encoding="utf8")))
    return hmac.new(key, f"{binding}:{payload}".encode(), hashlib.sha256).hexdigest()[:24]


def locator(kind, positions, binding):
    payload = ".".join([kind, *(str(value) for value in positions)])
    return payload + "." + _signature(payload, binding)


def positions(token, kind, count):
    parts = token.split(".") if isinstance(token, str) and len(token) <= 200 else []
    if (len(parts) != count + 2 or parts[0] != kind or not re.fullmatch("[a-f0-9]{24}", parts[-1])
            or any(not re.fullmatch("[0-9]{1,19}", part) for part in parts[1:-1])):
        raise W.Problem("invalid_cursor", "请原样使用此操作返回的 nextCursor")
    return tuple(int(part) for part in parts[1:-1])


def verify(token, binding, code):
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _signature(payload, binding)):
        raise W.Problem(code, "续查位置与当前文件版本或查询范围不符，请从头读取", 409)


def _decode(raw, start, size):
    if b"\0" in raw:
        raise W.Problem("binary", "发现二进制或 UTF-16 数据，未完成 UTF-8 文本扫描", 415)
    try:
        text, end = W.decode_utf8_page(raw, start, size)
    except UnicodeDecodeError as error:
        raise W.Problem("encoding", "此位置不是 UTF-8 文本，未完成文本扫描", 415) from error
    return text, end


def _block(stream, pos, size, budget):
    stream.seek(pos)
    raw = stream.read(min(W.TEXT_PAGE_BYTES, budget))
    text, end = _decode(raw, pos, size)
    if raw and end == pos and budget < 4:
        # A budget boundary can bisect the first UTF-8 character in this block.
        return b"", ""
    if end <= pos:
        raise W.Problem("revision_conflict", "读取期间文件已变更，请重新读取", 409)
    return raw[:end-pos], text


def _excerpt(stream, pos, number, size, context_lines):
    # At most 4 UTF-8 bytes per character; never materialize a whole long line.
    radius = 16008 if context_lines else 1050
    start = max(0, pos - radius)
    stream.seek(start)
    raw = stream.read(pos - start)
    while raw and 0x80 <= raw[0] < 0xc0:
        raw = raw[1:]
    before, _ = _decode(raw, pos-len(raw), pos)
    if start == 0:
        before = before.removeprefix("\ufeff")
    after, end = _decode(stream.read(radius), pos, size)
    left, right = before.split("\n"), after.split("\n")
    snippet = left[-1][-100:] + right[0][:260].rstrip("\r")
    if not context_lines:
        return snippet, None
    previous = left[:-1][-context_lines:]
    if end == size and after.endswith("\n"):
        right.pop()
    following = right[1:context_lines+1]
    joined = "\n".join([*previous, left[-1] + right[0], *following])
    excerpt = joined[:4000]
    first = number - len(previous)
    clipped = (len(joined) > len(excerpt) or start > 0 and len(left) <= context_lines + 1
               or end < size and len(right) <= context_lines + 1)
    return snippet, dict(startLine=first, endLine=first + excerpt.count("\n"), text=excerpt, truncated=clipped)


def search(ws, relative, query, context_lines, limit, output_chars, scan_budget, deadline, stopped,
           index, binding, prior_skips, cursor=None):
    needle = query.encode("utf-8")
    matches = []
    with W.text_file_snapshot(ws, relative, expected_path=Path(ws["path"]) / relative) as (stream, revision, size):
        binding += revision
        if cursor:
            _, pos, line, handled, _ = positions(cursor, "s1", 5)
            verify(cursor, binding, "search_cursor_stale")
        else:
            pos, line, handled = 0, 1, 0
        start = pos

        def page(reason=None):
            resume = locator("s1", (index, pos, line, int(handled), prior_skips), binding) if reason else None
            return matches, revision, pos - start, reason, resume

        while pos < size:
            if stopped():
                raise W.Problem("cancelled", "项目搜索已取消", 409)
            if pos > start and (pos - start >= scan_budget or time.monotonic() >= deadline):
                return page("bytes" if pos - start >= scan_budget else "time")
            budget = scan_budget - (pos - start)
            try:
                raw, _ = _block(stream, pos, size, budget)
            except W.Problem as error:
                error.scanned_bytes = pos - start + min(W.TEXT_PAGE_BYTES, budget, size-pos)
                raise
            if not raw:
                return page("bytes")
            # Only this block owns match starts; look ahead for a literal split across blocks.
            stream.seek(pos + len(raw))
            window = raw + stream.read(len(needle)-1)
            base, last = pos, 0
            at = 0
            if handled:
                newline = raw.find(b"\n")
                at = len(raw) if newline < 0 else newline + 1
            while (hit := window.find(needle, at)) >= 0 and hit < len(raw):
                line += raw.count(b"\n", last, hit)
                last = hit
                pos, handled = base + hit, False
                if len(matches) >= limit:
                    return page("matches")
                snippet, context = _excerpt(stream, pos, line, size, context_lines)
                match = dict(path=relative, line=line, text=snippet)
                if context is not None:
                    match["context"] = context
                chars = len(json.dumps(match, ensure_ascii=False)) + (0 if matches else len(relative)+len(revision)+16)
                if chars > output_chars and (matches or output_chars < 14000):
                    return page("output")
                output_chars -= chars
                matches.append(match)
                handled = True
                newline = raw.find(b"\n", hit)
                if newline < 0:
                    break
                at = newline + 1
            if raw.find(b"\n", last) >= 0:
                handled = False
            line += raw.count(b"\n", last)
            pos = base + len(raw)
        return page()


def read(ws, relative, args, stopped):
    """Seek with bounded scans, then return only the requested lines or line fragments."""
    cursor = args.get("cursor")
    with W.text_file_snapshot(ws, relative, expected_path=Path(ws["path"]) / relative) as (stream, revision, size):
        if cursor is not None:
            pos, line, target, char_skip, remaining = positions(cursor, "r1", 5)
            verify(cursor, revision, "revision_conflict")
        else:
            pos, line = 0, 1
            target, char_skip = args.get("line", 1), args.get("offset", 0)
            remaining = args.get("line_count", 20)
        initial = pos
        wanted_count = remaining
        deadline = time.monotonic() + SCAN_SECONDS
        text_parts = []
        output_bytes = 0
        line_chars = 0
        first = last = None
        reason = None
        # Byte length gives a cheap upper bound even before any line scan.
        beyond_size = target > size + 1 or char_skip > size
        while not beyond_size and pos < size and remaining:
            if stopped():
                raise W.Problem("cancelled", "项目读取已取消", 409)
            if pos > initial and (pos - initial >= SCAN_BYTES or time.monotonic() >= deadline):
                reason = "bytes" if pos - initial >= SCAN_BYTES else "time"
                break
            _, text = _block(stream, pos, size, SCAN_BYTES-(pos-initial))
            if not text:
                reason = "bytes"
                break
            if pos == 0 and text.startswith("\ufeff"):
                pos += 3
                text = text[1:]
            if char_skip:
                cut = min(len(text), char_skip)
                prefix, text = text[:cut], text[cut:]
                char_skip -= cut
                pos += len(prefix.encode())
                line += prefix.count("\n")
            if line < target and text:
                newlines = text.count("\n")
                if line + newlines < target:
                    line += newlines
                    pos += len(text.encode())
                    continue
                cut = 0
                for _ in range(target - line):
                    cut = text.index("\n", cut) + 1
                pos += len(text[:cut].encode())
                text, line = text[cut:], target
            while text and remaining:
                nl = text.find("\n")
                span = min(len(text), nl+1 if nl >= 0 else len(text), LINE_CHARS-line_chars)
                part = text[:span]
                data = part.encode()
                if len(data) > READ_BYTES - output_bytes:
                    part = data[:READ_BYTES-output_bytes].decode("utf-8", errors="ignore")
                    data = part.encode()
                if not part:
                    reason = "output"
                    break
                if first is None:
                    first = line
                last = line
                text_parts.append(part)
                output_bytes += len(data)
                pos += len(data)
                line_chars += len(part)
                if part.endswith("\n"):
                    remaining -= 1
                    line += 1
                    line_chars = 0
                elif pos == size:
                    remaining -= 1
                elif line_chars == LINE_CHARS or len(part) < span:
                    # Continue at the next UTF-8 byte, including partway through a long line.
                    reason = "line_fragment" if line_chars == LINE_CHARS else "output"
                    break
                text = text[len(part):]
            if reason or output_bytes >= READ_BYTES and remaining:
                reason = reason or "output"
                break
        more = not beyond_size and pos < size
        if not more:
            reason = None
        result = dict(path=relative, revision=revision, revisionKind="file_stamp", sizeBytes=size,
                      readMode="stream", readKind="text" if text_parts else "scan_progress" if more else "text",
                      text="".join(text_parts), startLine=first, endLine=last, scannedBytes=pos-initial,
                      hasMore=more, truncated=bool(reason), nextOffset=None)
        if more:
            result["nextCursor"] = locator("r1", (pos, line, target if reason else line,
                                                  char_skip, remaining if reason else wanted_count), revision)
            result["nextLine"] = line
        if reason:
            result["truncationReason"] = reason
        if not more and not text_parts and (target > line or char_skip):
            result.update(pastEndOfFile=True)
            if not char_skip:
                result["requestedLine"] = target
        if args.get("definition"):
            result["definition"] = dict(available=False, complete=False, reason="large_file_line_window")
        return result
