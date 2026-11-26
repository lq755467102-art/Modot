"""Task-owned immutable preparation cache; checks still use separate writable copies."""
# 主要引用方包括 `role_coordination.py` 和 `local_storage.py`。
import json
import os
from pathlib import Path
import shutil
import tempfile

from . import workbench as W
from .filelock import FileLock, atomic_json

MAX_CACHE_BYTES = 512 * 1024 * 1024
DISK_RESERVE_BYTES = 64 * 1024 * 1024


def active_names(owner):
    return {getattr(cache[0], "directory", Path()).name for key, cache in owner.source_cache.items()
            if owner.source_leases.get(key)}


def cache_path(session_dir):
    root = Path(os.environ.get("HARNESS_PREPARED_ROOT") or
                (Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "Modot" / "p"))
    session = str(Path(session_dir).resolve())
    identity = W.digest(os.path.normcase(session).encode())
    return root / identity[:24], identity


def cache_root(owner, *, check=True):
    """Keep project-relative path headroom independent of conversation location."""
    base, identity = cache_path(owner.session.dir)
    for parent in (base, *base.parents):
        if linked(parent):
            raise ValueError("preparation cache cannot traverse a link")
    base.mkdir(parents=True, exist_ok=True)
    with FileLock(base / "owner.lock", check=owner._ensure_running if check else None):
        marker = base / "owner.json"
        if linked(marker):
            raise ValueError("preparation cache owner cannot be a link")
        if marker.exists():
            if json.loads(marker.read_text(encoding="utf8")) != {"session": identity}:
                raise ValueError("preparation cache owner changed")
        else:
            atomic_json(marker, dict(session=identity))
    return base


def record_source(owner, record):
    base = cache_root(owner, check=False) if record.get("layout") == "short-v1" else Path(owner.session.dir) / "review-prepared"
    return PreparedSource(base, record["name"])


def evict(owner, records, key):
    name = records[key]["name"]
    record_source(owner, records[key]).cleanup()
    for cache_key, cache in list(owner.source_cache.items()):
        if getattr(cache[0], "directory", Path()).name == name:
            owner.source_cache.pop(cache_key)
            owner.source_leases.pop(cache_key, None)
    with owner.lock:
        del records[key]


def linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


class PreparedSource:
    def __init__(self, base, name):
        for parent in (Path(base), *Path(base).parents):
            if linked(parent):
                raise ValueError("preparation cache cannot traverse a link")
        self.base = Path(base).resolve()
        self.directory = self.base / name
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError("invalid preparation cache identity")

    def safe(self):
        if (any(linked(p) for p in (self.directory, *self.directory.parents))
                or self.directory.resolve().parent != self.base):
            raise ValueError("preparation cache path changed")

    def cleanup(self):
        self.safe()
        if self.directory.exists():
            shutil.rmtree(self.directory)


def load_or_prepare(owner, key, manifest, row):
    """Validate cached bytes before reuse, including after a runner restart."""
    base = cache_root(owner)
    if shutil.disk_usage(base).free < DISK_RESERVE_BYTES:
        raise W.Problem("cache_disk_full", "磁盘可用空间不足，已保留现有材料；释放空间后重试", 409)
    identity = W.digest(json.dumps([owner._task_binding(), key, manifest, "prepared-source-v1"],
                                  sort_keys=True).encode())
    with FileLock(base / "cache.lock", check=owner._ensure_running):
        with owner.lock:
            records = owner.state.setdefault("preparedSources", {})
            old = records.get(identity)
        # Older runners recorded only a name; do not count their retained bytes as zero.
        for record in records.values():
            if "bytes" not in record:
                previous = record_source(owner, record)
                previous.safe()
                size = 0
                for file in (previous.directory / "source").rglob("*"):
                    if linked(file):
                        raise W.Problem("cache_changed", "旧缓存含链接，不能作为可复用准备源", 409)
                    if file.is_file():
                        size += file.stat().st_size
                with owner.lock:
                    record["bytes"] = size
        if old:
            cached = record_source(owner, old)
            cached.safe()
            try:
                meta = json.loads((cached.directory / "index.json").read_text(encoding="utf8"))
                source = cached.directory / "source"
                actual = {}
                for file in source.rglob("*"):
                    if linked(file):
                        raise ValueError("cached source contains a link")
                    if file.is_file():
                        actual[file.relative_to(source).as_posix()] = W.digest(file.read_bytes())
                if (source.is_dir() and not linked(source) and not linked(cached.directory / "index.json")
                        and meta["identity"] == identity and actual == meta["files"]):
                    owner.session.emit("review_snapshot_reused", candidateId=row["id"], snapshotId=key,
                                       files=len(actual))
                    return cached, source, set(meta["omitted"]), set(meta["sanitized"])
            except (OSError, ValueError, KeyError):
                pass
            cached.cleanup()
            with owner.lock:
                del records[identity]
        # Bound materialized copies; immutable historical snapshots remain in the change store.
        if len(records) >= 8:
            active = active_names(owner)
            oldest = next((k for k, value in records.items() if value["name"] not in active), None)
            if oldest is None:
                raise W.Problem("cache_capacity", "当前八份准备源均在使用，保留现有检查，请结束相关检查后重试", 409)
            evict(owner, records, oldest)
        directory = Path(tempfile.mkdtemp(prefix=identity[:16] + "-", dir=base))
        cached = PreparedSource(base, directory.name)
        source = directory / "source"
        source.mkdir()
        try:
            omitted, sanitized = owner._materialize(manifest, source)
            hashes = {p.relative_to(source).as_posix(): W.digest(p.read_bytes())
                      for p in source.rglob("*") if p.is_file()}
            size = sum(p.stat().st_size for p in source.rglob("*") if p.is_file())
            active = active_names(owner)
            while size + sum(v.get("bytes", 0) for v in records.values()) > MAX_CACHE_BYTES:
                oldest = next((k for k, v in records.items() if v["name"] not in active), None)
                if oldest is None:
                    raise W.Problem("cache_capacity", "必要材料超出缓存容量；当前在用源保留，请结束相关检查后再重试", 409)
                evict(owner, records, oldest)
            atomic_json(directory / "index.json", dict(identity=identity, files=hashes,
                                                       omitted=sorted(omitted), sanitized=sorted(sanitized)))
            with owner.lock:
                records[identity] = dict(name=directory.name, snapshotId=key, bytes=size, layout="short-v1")
                owner.save()
        except BaseException:
            with owner.lock:
                records.pop(identity, None)
            cached.cleanup()
            raise
        return cached, source, omitted, sanitized
