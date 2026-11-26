"""Remove private-key material before bytes are published, paginated or replayed."""
# 主要引用方包括 `provider.py`、`tool_results.py` 和 `workbench.py`。

import re
from functools import lru_cache

MASK = "«凭据已遮:私钥»"
_LABELS = ("OPENSSH PRIVATE KEY", "PRIVATE KEY", "ENCRYPTED PRIVATE KEY",
           "RSA PRIVATE KEY", "EC PRIVATE KEY", "DSA PRIVATE KEY", "PGP PRIVATE KEY BLOCK")
_HEADERS = {"-----BEGIN " + label + "-----": "-----END " + label + "-----" for label in _LABELS}
# Base64 of the OpenSSH binary format's magic bytes, without variable trailing bits.
_RAW = "b3BlbnNzaC1rZXktdjE"
_START = re.compile("|".join(map(re.escape, (*_HEADERS, _RAW))))
_BASE64_END = re.compile(r"[^a-zA-Z0-9+/=\r\n]")
_PREFIXES = {token[:i] for token in (*_HEADERS, _RAW) for i in range(1, len(token))}
_MAX_PREFIX = max(map(len, _PREFIXES))


class PrivateKeyFilter:
    """One instance per logical stdout, stderr, answer or reasoning stream."""
    def __init__(self):
        self.tail = ""
        self.end = None

    def feed(self, chunk):
        if not chunk:
            return ""
        data = self.tail + chunk
        self.tail = ""
        result = []
        offset = 0
        while offset < len(data):
            if self.end == _RAW:
                stop = _BASE64_END.search(data, offset)
                if not stop:
                    break
                offset = stop.start()
                self.end = None
            elif self.end:
                cut = data.find(self.end, offset)
                if cut < 0:
                    # Keep only a partial footer, never a partial body.
                    for i in range(min(len(self.end) - 1, len(data) - offset), 0, -1):
                        if data.endswith(self.end[:i]):
                            self.tail = self.end[:i]
                            break
                    break
                offset = cut + len(self.end)
                self.end = None
                continue
            match = _START.search(data, offset)
            if match:
                result.extend((data[offset:match.start()], MASK))
                self.end = _HEADERS.get(match[0], _RAW)
                offset = match.end()
                continue
            # Emit ordinary tokens immediately. Only a possible marker is held.
            suffix = ""
            for i in range(min(_MAX_PREFIX, len(data) - offset), 0, -1):
                if data[-i:] in _PREFIXES:
                    suffix = data[-i:]
                    break
            self.tail = suffix
            result.append(data[offset:len(data)-len(suffix)])
            break
        return "".join(result)

    def finish(self):
        tail = "" if self.end else self.tail
        self.tail = ""
        self.end = None
        return tail


def text(value):
    if not isinstance(value, str) or ("-----BEGIN " not in value and _RAW not in value):
        return value
    stream = PrivateKeyFilter()
    return stream.feed(value) + stream.finish()


def clean(value):
    if isinstance(value, str):
        return text(value)
    if isinstance(value, dict):
        return {text(key): clean(part) for key, part in value.items()}
    if isinstance(value, list):
        return [clean(part) for part in value]
    return value


@lru_cache(maxsize=8)
def _masked_tail(path, stamp, limit):
    # A legacy key header can precede the entire requested page. Filter before
    # slicing and retain only sanitized bytes. Stable histories are read once.
    stream = PrivateKeyFilter()
    result = bytearray()
    def append(value):
        result.extend(value.encode("utf-8"))
        if len(result) > limit:
            del result[:-limit]
    with path.open(encoding="utf-8", errors="replace", newline="") as file:
        while chunk := file.read(65536):
            append(stream.feed(chunk))
        append(stream.finish())
    return result.decode("utf-8", "ignore")


def live_tail(path, limit=65536):
    try:
        stat = path.stat()
        stamp = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        return _masked_tail(path, stamp, limit), stat.st_size > limit
    except FileNotFoundError:
        return "", False
