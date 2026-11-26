"""Disk-backed offsets over append-only JSONL files; bounded SQLite page cache.

The log remains authoritative. Indexes rebuild on replacement/truncation and do
not consume an unfinished last line. Reconnection uses the same sequence cursor.
"""

# 主要引用方包括 `workbench.py` 和 `legacy_migration.py`。

from __future__ import annotations
from collections import OrderedDict
import json
from pathlib import Path
import threading

_lock = threading.RLock()
_counts = OrderedDict()


def count(path):
    """Compatibility lower bound; callers needing completeness use count_snapshot."""
    return count_snapshot(path)["count"]


def count_snapshot(path):
    """Bounded listing work, separate from the authoritative event page index.

    Read at most 4MiB per call and retain no event body. Oversized records are
    discarded in bounded chunks; an unfinished ordinary line waits for append.
    """
    p = Path(path)
    key = str(p.resolve())
    with _lock:
        entry = _counts.get(key)
        if entry is None:
            entry = {"lock": threading.Lock(), "state": None}
            _counts[key] = entry
        _counts.move_to_end(key)
        while len(_counts) > 4096:
            _counts.popitem(last=False)
    with entry["lock"]:
        try:
            stat = p.stat()
        except OSError:
            entry["state"] = None
            return dict(count=0, catchingUp=False, skippedLines=0, incompleteLine=False)
        signature = (stat.st_dev, stat.st_ino)
        ix = entry["state"]
        if (
            ix is None
            or ix["identity"] != signature
            or stat.st_size < ix["offset"]
            or (stat.st_size == ix["size"] and stat.st_mtime_ns != ix["mtime"])
        ):
            ix = dict(
                identity=signature,
                offset=0,
                size=0,
                mtime=0,
                count=0,
                skipped=0,
                discarding=False,
            )
        budget = 4 * 1024 * 1024
        incomplete = False
        with p.open("rb") as f:
            f.seek(ix["offset"])
            while budget > 0:
                line = f.readline(min(MAX_LINE + 1, budget))
                budget -= len(line)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    if ix["discarding"] or len(line) > MAX_LINE:
                        ix["discarding"] = True
                        ix["offset"] = f.tell()
                        continue
                    # Re-read this bounded prefix next time; never retain it in RAM.
                    incomplete = f.tell() >= stat.st_size
                    break
                ix["offset"] = f.tell()
                if ix["discarding"] or len(line) > MAX_LINE:
                    ix["discarding"] = False
                    ix["skipped"] += 1
                    continue
                try:
                    row = json.loads(line)
                    int(row.get("seq") or 0)
                except (ValueError, TypeError, AttributeError, RecursionError):
                    ix["skipped"] += 1
                    continue
                ix["count"] += 1
        ix["size"], ix["mtime"] = stat.st_size, stat.st_mtime_ns
        entry["state"] = ix
        return dict(
            count=ix["count"],
            catchingUp=ix["offset"] < stat.st_size and not incomplete,
            skippedLines=ix["skipped"],
            incompleteLine=incomplete,
        )


MAX_LINE = 2 * 1024 * 1024
PAGE_BYTES = 2 * 1024 * 1024


def _hash_prefix(path, length):
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = length
        while remaining:
            part = stream.read(min(131072, remaining))
            if not part:
                return None
            digest.update(part)
            remaining -= len(part)
    return digest


_verification_lock = threading.Lock()
_proof_local = threading.local()
_verification_cache = OrderedDict()
_ingest_targets = OrderedDict()
_verification_slots = threading.BoundedSemaphore(2)


class VerificationPending(Exception):
    pass


def _prefix_state(path, length, background=False):
    # 大前缀核验可返回 pending 等待后台完成；它表示证据尚未核实，不表示日志为空或索引已完整。
    if not background or length <= 4 * 1024 * 1024:
        before = path.stat()
        value = _hash_prefix(path, length)
        _proof_local.stat = before
        return value
    stat = path.stat()
    signature = (
        str(path.resolve()),
        stat.st_dev,
        stat.st_ino,
        length,
    )
    with _verification_lock:
        if signature in _verification_cache:
            value = _verification_cache[signature]
            _verification_cache.move_to_end(signature)
            if value is not None:
                if value is False:
                    return None
                proof = value["stat"]
                if (
                    stat.st_size == proof.st_size
                    and stat.st_mtime_ns != proof.st_mtime_ns
                ):
                    _verification_cache.pop(signature, None)
                else:
                    _proof_local.stat = proof
                    return value["hash"].copy()
            if signature in _verification_cache:
                raise VerificationPending()
        if not _verification_slots.acquire(False):
            raise VerificationPending()
        _verification_cache[signature] = None
        while len(_verification_cache) > 16:
            _verification_cache.popitem(last=False)

    def verify():
        result = False
        try:
            before = path.stat()
            digest = _hash_prefix(path, length)
            result = dict(hash=digest, stat=before) if digest else False
            end = path.stat()
            if (end.st_dev, end.st_ino) != signature[1:3] or end.st_size < length:
                result = False
        except OSError:
            pass
        finally:
            with _verification_lock:
                if signature in _verification_cache:
                    _verification_cache[signature] = result or False
            _verification_slots.release()

    threading.Thread(target=verify, daemon=True, name="history-verification").start()
    raise VerificationPending()


def _prefix(path, length, background=False):
    value = _prefix_state(path, length, background)
    return value.hexdigest() if value is not None else None


def _query(
    path,
    after=0,
    limit=0,
    *,
    tail=False,
    before=None,
    max_ingest=None,
    after_position=None,
    before_position=None,
    physical=False,
):
    import sqlite3
    import uuid
    import hashlib
    from .filelock import FileLock

    p = Path(path)
    index = p.with_name(p.name + ".index.sqlite3")
    if not p.exists():
        missing = index.exists()
        return (
            [],
            0,
            dict(
                generation="missing" if missing else "empty",
                gap=missing,
                gapCount=0,
                sequenceConflicts=0,
                catchingUp=False,
                historyNotice="原始日志缺失，未推断任务已经完成" if missing else "",
            ),
        )
    with FileLock(index.with_suffix(".lock")):
        db = sqlite3.connect(index, timeout=10)
        try:
            db.execute("PRAGMA cache_size=-1024")
            db.execute(
                "CREATE TABLE IF NOT EXISTS events (seq INTEGER, pos INTEGER PRIMARY KEY, length INTEGER)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS by_seq ON events(seq,pos)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS history_hashes (pos INTEGER PRIMARY KEY, hash TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS history_state (id INTEGER PRIMARY KEY, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS history_seq_counts(seq INTEGER PRIMARY KEY, n INTEGER NOT NULL)"
            )
            record = db.execute("SELECT data FROM history_state WHERE id=1").fetchone()
            old = json.loads(record[0]) if record else None
            stat = p.stat()
            cycle = (
                str(p.resolve()),
                stat.st_dev,
                stat.st_ino,
                (old or {}).get("offset", 0),
            )
            with _verification_lock:
                ingest_end = _ingest_targets.setdefault(cycle, stat.st_size)
                ingest_end = min(ingest_end, stat.st_size)
                _ingest_targets.move_to_end(cycle)
                while len(_ingest_targets) > 16:
                    _ingest_targets.popitem(last=False)
            identity = str((stat.st_dev, stat.st_ino))
            changed = (
                not old
                or old["identity"] != identity
                or stat.st_size != old["size"]
                or stat.st_mtime_ns != old["mtime"]
                or old["offset"] < stat.st_size
            )
            rebuild = (
                not old
                or old.get("version") != 4
                or old["identity"] != identity
                or stat.st_size < old["offset"]
            )
            prefix = None
            if old and changed and not rebuild:
                prefix = _prefix_state(p, old["offset"], bool(max_ingest))
                rebuild = prefix is None or prefix.hexdigest() != old["prefixHash"]
            # 重建生成新的 generation，旧物理游标不能直接拼接新日志；只有核验通过的前缀才可增量续读。
            state = (
                dict(
                    version=4,
                    unique=0,
                    conflicts=0,
                    generation=uuid.uuid4().hex,
                    offset=0,
                    total=0,
                    malformed=0,
                    oversized=0,
                    outOfOrder=0,
                    maxSeen=0,
                )
                if rebuild
                else dict(old)
            )
            with db:
                if rebuild:
                    db.execute("DELETE FROM events")
                    db.execute("DELETE FROM history_hashes")
                    db.execute("DELETE FROM history_seq_counts")
                    changed = True
                if changed:
                    if rebuild:
                        import hashlib

                        prefix = hashlib.sha256()
                    consumed = 0
                    with p.open("rb") as stream:
                        stream.seek(state["offset"])
                        while True:
                            if max_ingest and consumed >= max_ingest:
                                break
                            pos = stream.tell()
                            if pos >= ingest_end:
                                break
                            line = stream.readline(min(MAX_LINE + 1, ingest_end - pos))
                            if not line:
                                break
                            consumed += len(line)
                            # 超长记录分块跳过；正常尾行缺换行则暂不消费，避免把仍在写入的 JSON 当成坏事件。
                            if state.get("skippingLine") or len(line) > MAX_LINE:
                                if not state.get("skippingLine"):
                                    state["oversized"] += 1
                                prefix.update(line)
                                state["skippingLine"] = not line.endswith(b"\n")
                                state["offset"] = stream.tell()
                                if max_ingest and consumed >= max_ingest:
                                    break
                                continue
                            if not line.endswith(b"\n"):
                                break
                            prefix.update(line)
                            state["offset"] = stream.tell()
                            try:
                                value = json.loads(line)
                                seq = value.get("seq")
                                if (
                                    type(seq) is not int
                                    or not 1 <= seq <= 9223372036854775807
                                ):
                                    raise ValueError("missing sequence")
                            except (
                                ValueError,
                                TypeError,
                                AttributeError,
                                RecursionError,
                            ):
                                state["malformed"] += 1
                                continue
                            if seq < state["maxSeen"]:
                                state["outOfOrder"] += 1
                            state["maxSeen"] = max(seq, state["maxSeen"])
                            # 序号重复仍保留每条物理记录并累计冲突；不能靠按 seq 去重掩盖源日志的不一致。
                            prior_row = db.execute(
                                "SELECT n FROM history_seq_counts WHERE seq=?", (seq,)
                            ).fetchone()
                            prior = prior_row[0] if prior_row else 0
                            db.execute(
                                "INSERT OR REPLACE INTO history_seq_counts VALUES(?,?)",
                                (seq, prior + 1),
                            )
                            if prior == 0:
                                state["unique"] += 1
                            elif prior == 1:
                                state["conflicts"] += 1
                            db.execute(
                                "INSERT INTO events VALUES (?,?,?)",
                                (seq, pos, len(line)),
                            )
                            db.execute(
                                "INSERT INTO history_hashes VALUES(?,?)",
                                (pos, hashlib.sha256(line).hexdigest()),
                            )
                            state["total"] += 1
                            if max_ingest and consumed >= max_ingest:
                                break
                    state["prefixHash"] = prefix.hexdigest()
                    if (
                        _prefix(p, state["offset"], bool(max_ingest))
                        != state["prefixHash"]
                    ):
                        raise OSError(
                            "原始日志在索引期间发生变化，请重新读取；没有确认混合记录"
                        )
                end = p.stat()
                proof = getattr(_proof_local, "stat", stat) if changed else stat
                verified = (
                    proof.st_dev,
                    proof.st_ino,
                    proof.st_size,
                    proof.st_mtime_ns,
                ) == (end.st_dev, end.st_ino, end.st_size, end.st_mtime_ns)
                state.update(
                    identity=identity,
                    size=proof.st_size,
                    mtime=proof.st_mtime_ns,
                    observedSize=end.st_size,
                    sourceVerified=verified,
                )
                db.execute(
                    "INSERT OR REPLACE INTO history_state VALUES(1,?)",
                    (json.dumps(state),),
                )
            # A completed transaction consumes this verification cycle. Later
            # file changes must obtain a fresh hash, never reuse an old proof.
            with _verification_lock:
                _ingest_targets.pop(cycle, None)
                for key in list(_verification_cache):
                    if key[0] == str(p.resolve()):
                        _verification_cache.pop(key, None)
            conflicts = state["conflicts"]
            gaps = max(0, state["maxSeen"] - state["unique"])
            query = (
                "SELECT seq,events.pos,length,hash FROM events JOIN history_hashes ON events.pos=history_hashes.pos WHERE "
                + ("events.pos>?" if after_position is not None else "seq>?")
            )
            args = [after_position if after_position is not None else after]
            if before_position is not None:
                query += " AND events.pos<?"
                args.append(before_position)
            elif before is not None:
                query += " AND seq<?"
                args.append(before)
            query += (
                (" ORDER BY events.pos DESC" if tail else " ORDER BY events.pos")
                if physical
                else (
                    " ORDER BY seq DESC,events.pos DESC"
                    if tail
                    else " ORDER BY seq,events.pos"
                )
            )
            if limit:
                query += " LIMIT ?"
                args.append(max(1, int(limit)))
            result, read_conflicts = [], 0
            result_bytes = 0
            page_clipped = False
            with p.open("rb") as stream:
                for seq, pos, length, expected_hash in db.execute(query, args):
                    if physical and result and result_bytes + length > PAGE_BYTES:
                        page_clipped = True
                        break
                    stream.seek(pos)
                    try:
                        raw = stream.read(length)
                        # 索引位置命中还不够，回读原字节必须匹配逐条指纹；读取期间变化要计入不完整提示。
                        if hashlib.sha256(raw).hexdigest() != expected_hash:
                            raise ValueError("changed source bytes")
                        value = json.loads(raw)
                        if value.get("seq") != seq:
                            raise ValueError("changed record")
                        result.append(dict(value, sourceOffset=pos))
                        result_bytes += length
                    except (ValueError, TypeError, AttributeError, RecursionError):
                        read_conflicts += 1
            if tail:
                result.reverse()
            gap = bool(
                gaps
                or conflicts
                or state["malformed"]
                or state["oversized"]
                or state["outOfOrder"]
                or read_conflicts
            )
            meta = dict(
                generation=state["generation"],
                gap=gap,
                gapCount=gaps,
                sequenceConflicts=conflicts,
                pageClipped=page_clipped,
                returnedBytes=result_bytes,
                catchingUp=state["offset"] < state["observedSize"]
                or not state["sourceVerified"],
                historyNotice="当前历史页达到2MiB容量，只显示一页；请沿原字节游标继续读取"
                if page_clipped
                else "日志有缺口、重复、顺序或不可读记录；当前页面不代表完整执行证据"
                if gap
                else "日志仍在补齐或源仍变化，当前页不代表完整记录"
                if state["offset"] < state["observedSize"]
                or not state["sourceVerified"]
                else "",
                malformedCount=state["malformed"],
                oversizedCount=state["oversized"],
            )
            return result, state["total"], meta
        except VerificationPending:
            return (
                [],
                (old or {}).get("total", 0),
                dict(
                    generation=(old or {}).get("generation", "verifying"),
                    gap=False,
                    gapCount=0,
                    sequenceConflicts=0,
                    catchingUp=True,
                    verificationPending=True,
                    historyNotice="历史前缀正在后台复核；当前页面尚不能证明完整，未重放任何动作",
                ),
            )
        except OSError:
            with _verification_lock:
                for key in list(_verification_cache):
                    if key[0] == str(p.resolve()):
                        _verification_cache.pop(key, None)
                for key in list(_ingest_targets):
                    if key[0] == str(p.resolve()):
                        _ingest_targets.pop(key, None)
            raise
        finally:
            db.close()


def rows(path, after=0, limit=0, *, tail=False, before=None):
    result, total, _ = _query(path, after, limit, tail=tail, before=before)
    return result, total


# 前端分页沿原始字节位置及物理顺序读取；序号缺口、容量截断和核验 pending 都通过元数据显式暴露。
def page(
    path,
    after=0,
    limit=0,
    *,
    tail=False,
    before=None,
    after_position=None,
    before_position=None,
):
    return _query(
        path,
        after,
        limit,
        tail=tail,
        before=before,
        max_ingest=4 * 1024 * 1024,
        after_position=after_position,
        before_position=before_position,
        physical=True,
    )
