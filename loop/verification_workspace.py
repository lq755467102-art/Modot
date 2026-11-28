"""Bounded AppContainer leases. Only clean task handoffs can survive restart."""
# 主要引用方包括 `code_service.py` 和 `safe_executor.py`。
import atexit
from contextlib import contextmanager
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid
from . import workbench as W, sandbox_lifecycle as lifecycle
from .filelock import atomic_json

_lock = threading.RLock()
_cleanup_ready = threading.Condition(_lock)
_leases = []
_cleanup_worker = None
_cleanup_pending = set()
_runtime_preparations = 0
MAX_LEASES = 8
MAX_RETAINED_BYTES = 512 * 1024 * 1024


def task_scope(scope):
    return len(scope) >= 3 and str(scope[1]).startswith('task:')


def fingerprint(root, *, source=False, projection=None, _retry=0):
    """Bounded handoff integrity, never per-tool polling or a semantic verdict."""
    from . import safe_executor as S, directory_generation as D
    import stat
    root = Path(root)
    before = D.generation(root)
    rows=[];total=0;pending=[root]
    while pending:
        path=pending.pop();info=path.lstat()
        if path.is_symlink() or getattr(info,'st_file_attributes',0) & 0x400 or stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise W.Problem('cache_changed','准备目录包含重定向，未复用',409)
        relative=path.relative_to(root).as_posix()
        if projection is not None and path == root:
            # Publishing a sibling changes the directory times. Membership and each visible
            # entry are still fingerprinted, including the root's identity and permissions.
            rows.append((relative, info.st_dev, info.st_ino, info.st_mode))
        else:
            rows.append((relative,info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns))
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir(),reverse=True):
                if projection and path == root and child.name in projection:
                    continue
                name=child.name.casefold()
                if (name == '.harness-check' and (source or child == root/'workspace'/'.harness-check')
                        or not source and path == root and name == 'scratch'):continue
                if source and (name in S.EXCLUDED or name in S.SECRET_NAMES or name.startswith('.env')
                               or child.suffix.lower() in {'.pem','.key','.pfx','.p12'}):continue
                pending.append(child)
        elif stat.S_ISREG(info.st_mode):
            total+=info.st_size
            if total > MAX_RETAINED_BYTES:
                scope = "源目录版本核对" if source else "保留目录完整性核对"
                raise W.Problem("cache_capacity", f"{scope}计入的文件总量超过 {MAX_RETAINED_BYTES / (1024 * 1024):g} MiB", 413)
            with path.open('rb') as stream:rows.append(hashlib.file_digest(stream,'sha256').hexdigest())
        else:raise W.Problem('cache_changed','准备材料不是普通文件',409)
        if len(rows) > S.MAX_FILES * 2:
            raise W.Problem("cache_capacity", f"目录完整性核对超过 {S.MAX_FILES * 2:,} 条指纹记录", 413)
    if before is not None and before != D.generation(root) and _retry < 2:
        return fingerprint(root,source=source,projection=projection,_retry=_retry+1)
    if before is None or before != D.generation(root):
        raise W.Problem('cache_changed','准备目录在交接核对期间变化，未复用',409)
    return W.digest(repr(rows).encode())


def valid_base(row):
    base=Path(row['base'])
    return (base.resolve().parent == Path(tempfile.gettempdir()).resolve()
            and not base.is_symlink() and not getattr(base,'is_junction',lambda:False)()
            and re.fullmatch(r'harness-task-pool-[\w-]+',base.name)
            and re.fullmatch(r'Harness\.Task\.[0-9a-f]{32}',row['name']))


def restore(scope, environment_id=None, *, busy=True):
    """Claim an idle, clean handoff; unknown/live processes are never adopted."""
    failure=None
    for path in (W.home()/'verification-leases').glob('*.json'):
        try:
            lock = lifecycle.lease_lock(path)
            if not lock.acquire():
                continue
        except (OSError, lifecycle.Unproven):
            continue
        try:
            if path.stat().st_size > 8*1024*1024:continue
            row=json.loads(path.read_text(encoding='utf8'))
            if (row.get('cacheVersion') != 1 or tuple(row.get('scope',[])) != tuple(scope) or not row.get('clean') or row.get('busy')
                    or row.get('environmentId') != environment_id or not valid_base(row)):continue
            # 跨重启只领取干净且明确空闲的交接；存在宿主/子进程身份的记录不能直接接管成可复用环境。
            if row.get('pid') or row.get('childPid'):continue
            base=Path(row['base'])
            if not base.is_dir() or fingerprint(base) != row.get('baseStamp'):
                raise W.Problem('environment_changed','准备目录内容或身份已变化，未复用；可显式选择 new 重建',409)
            row.update(base=base,journal=path,profile=None,scope=tuple(scope),busy=busy,reused=True,
                       clean=False,pid=os.getpid(),identity=W.RT.identity(os.getpid()),useLock=lock,
                       restoredSource=True,bytes=row.get('bytes') or 0)
            if row.get('preparedSource'):row['preparedSource']=tuple(row['preparedSource'])
            if row.get('copyState'):
                # Notification serials are process-local and may coincidentally
                # repeat after restart. Only a fresh integrity check can bridge.
                row['copyState']['cursor']=None
            # The per-lease file lock protects the integrity scan above. Only
            # capacity and publishing the claimed lease need the registry lock.
            with _lock:
                if len(_leases)>=MAX_LEASES or sum(l.get('bytes',0) for l in _leases)+row['bytes']>MAX_RETAINED_BYTES:
                    raise W.Problem('cache_capacity','准备目录容量已满，请释放空闲环境后再复用',409)
                try:
                    lifecycle.claim_lease(base, row['name'], path.parent)
                except (OSError, lifecycle.Unproven) as error:
                    raise W.Problem('environment_changed', '准备目录的所有权无法确认，未复用', 409) from error
                record(row)
                _leases.append(row)
            lock=None
            return row
        except W.Problem as error:
            if environment_id is not None:failure=error
        except (OSError,ValueError,KeyError):
            continue
        finally:
            if lock:lock.release()
    if failure:raise failure


def record(lease):
    if not lease.get("journal"):
        root = W.home() / "verification-leases"
        root.mkdir(exist_ok=True)
        lease["journal"] = root / (lease["base"].name + ".json")
    fields=('base','name','scope','busy','bytes','pid','identity','childPid','childIdentity','environmentId')
    if task_scope(lease['scope']):
        fields+=('clean','cacheVersion')
    # 完整复用元数据只在干净交接时持久化；busy/未清理记录不携带可直接相信的源码副本证明。
    if task_scope(lease['scope']) and lease.get('clean'):
        fields+=('baseStamp','sourceStamp','preparedSource','copyState','protectedBytes','protectedFiles',
                 'runtimeSpec','runtimeBytes','lastRevision','lastCommand','retainedAt','stagedMaterial','cacheVersion')
    atomic_json(lease["journal"], {key: str(lease[key]) if key == "base" else lease.get(key) for key in fields})


def retained_roots(data):
    roots = []
    for path in (Path(data)/"runs/workbench/verification-leases").glob("*"):
        if path.suffix not in {".json", ".retired"}:
            continue
        try:
            row = json.loads(path.read_text(encoding="utf8"))
            base = Path(row["base"])
            if base.resolve().parent == Path(tempfile.gettempdir()).resolve() and base.name.startswith("harness-task-pool-"):
                roots.append(base)
        except (OSError, ValueError, KeyError):
            continue
    return roots


def _clean_unused(paths):
    for path in paths:
        lifecycle.reclaim_journal(path)


@contextmanager
def preparing_runtime():
    """Give new runtime setup priority over the next retired-directory deletion."""
    global _runtime_preparations
    with _cleanup_ready:
        _runtime_preparations += 1
    try:
        yield
    finally:
        with _cleanup_ready:
            _runtime_preparations -= 1
            if not _runtime_preparations:
                _cleanup_ready.notify()


def recovery_turn():
    with _cleanup_ready:
        return _cleanup_ready.wait_for(lambda: not _runtime_preparations, timeout=.25)


def _drain_cleanup():
    global _cleanup_worker
    while True:
        with _cleanup_ready:
            while _cleanup_pending:
                # Recheck between directories. A deletion already in progress
                # finishes independently of new checkouts.
                path = next((p for p in _cleanup_pending
                             if p.suffix != ".retired" or not _runtime_preparations), None)
                if path is not None:
                    _cleanup_pending.remove(path)
                    break
                _cleanup_ready.wait()
            else:
                _cleanup_worker = None
                return
        _clean_unused((path,))


def _schedule_cleanup(paths):
    """Publish work with the registry lock held. The worker removes files outside it."""
    global _cleanup_worker
    _cleanup_pending.update(paths)
    if not _cleanup_pending:
        return
    if _cleanup_worker is not None and _cleanup_worker.is_alive():
        _cleanup_ready.notify()
        return
    worker = threading.Thread(target=_drain_cleanup, daemon=True, name="verification-lease-cleanup")
    try:
        worker.start()
    except RuntimeError:
        pass  # A later checkout can retry using the journals.
    else:
        _cleanup_worker = worker


def _retire(journal):
    # The rename removes the lease from restore's scan and survives a host exit
    # before the worker has removed the directory.
    return journal.rename(journal.with_suffix(".retired"))


def scavenge():
    root = W.home() / "verification-leases"
    retiring = list(root.glob("*.retired"))
    retained = []
    abandoned = []
    current_journals = {lease.get("journal") for lease in _leases}
    for path in root.glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf8"))
            if (row.get("clean") and row.get("busy") is False and not row.get("pid")
                    and not row.get("childPid") and valid_base(row)):
                retained.append((row.get("retainedAt", 0), path, row))
            elif row.get("pid") and path not in current_journals:
                abandoned.append(path)
        except (OSError, ValueError, KeyError, TypeError):
            continue

    count = len(retained) + len(_leases)
    total = sum(row.get("bytes", 0) for _, _, row in retained) + sum(lease.get("bytes", 0) for lease in _leases)
    for _, path, row in sorted(retained):
        if (count <= MAX_LEASES and total <= MAX_RETAINED_BYTES) or len(retiring) >= MAX_LEASES:
            break
        try:
            with lifecycle.lease_lock(path):
                # A different controller may have claimed the handoff since the scan.
                if json.loads(path.read_text(encoding="utf8")) != row:
                    continue
                retiring.append(_retire(path))
                count -= 1
                total -= row.get("bytes", 0)
        except (OSError, ValueError):
            continue
    _schedule_cleanup([*retiring, *abandoned])
    return retiring


def discard(lease, *, synchronous=True):
    with _lock:
        if lease not in _leases:
            return
        lease["busy"] = False
        record(lease)
        path = _retire(lease["journal"])
        _leases.remove(lease)
        if lock := lease.pop("useLock", None):
            lock.release()
        if not synchronous:
            _schedule_cleanup([path])
    if synchronous:
        lifecycle.reclaim_journal(path, background=False)


def checkout(scope, environment_id=None):
    if task_scope(scope) and environment_id != 'new':
        restored = restore(scope, environment_id)
        if restored:
            return restored
    with _lock:
        retiring = scavenge()
        # 显式环境 ID 必须命中本任务且空闲；不可用/忙碌时报告原事实，不换一份目录冒充该环境。
        if environment_id not in {None, "new"}:
            lease = next((row for row in _leases if row.get("environmentId") == environment_id and row["scope"] == scope), None)
            if lease is None:
                raise W.Problem("environment_unavailable", "指定准备环境已释放、重启后不可用或不属于本任务；请自行选择重新创建", 409)
            if lease["busy"]:
                raise W.Problem("environment_busy", "指定环境仍有进程使用，未替换为其他环境", 409)
            lease.update(busy=True, reused=True)
            record(lease)
            return lease
        for lease in _leases:
            if environment_id is None and not lease.get("environmentId") and lease["scope"] == scope and not lease["busy"]:
                lease.update(busy=True, reused=True)
                record(lease)
                return lease
        # 容量回收仅淘汰空闲租约；全部忙碌时失败返回，不能为腾空间删除仍在执行的环境。
        while len(_leases) >= MAX_LEASES or sum(row.get("bytes", 0) for row in _leases) > MAX_RETAINED_BYTES:
            idle = next((row for row in _leases if not row["busy"]), None)
            if idle is None:
                raise W.Problem("cache_capacity", "验证区均在运行，未复用未知进程工作区", 409)
            # Bound outstanding deletions independently of the eight usable leases.
            if len(retiring) >= MAX_LEASES:
                raise W.Problem("cache_capacity", "已有空闲验证区待清理，请稍后继续", 409)
            path = _retire(idle["journal"])
            _leases.remove(idle)
            retiring.append(path)
            if lock := idle.pop("useLock", None):
                lock.release()
            _schedule_cleanup([path])
        if shutil.disk_usage(tempfile.gettempdir()).free < 64 * 1024 * 1024:
            raise W.Problem("cache_disk_full", "磁盘空间不足，未创建验证区", 409)
        directory = W.home() / "verification-leases"
        base, name = lifecycle.allocate("lease", directory)
        lease = dict(scope=scope, busy=True, reused=False, base=base, name=name, profile=None,
                     pid=os.getpid(), identity=W.RT.identity(os.getpid()), cacheVersion=1)
        if environment_id == "new":
            lease["environmentId"] = uuid.uuid4().hex
        journal = directory / (base.name + ".json")
        lock = None
        try:
            lock = lifecycle.lease_lock(journal)
            lock.__enter__()
            lease.update(journal=journal, useLock=lock)
            record(lease)
        except BaseException:
            if lock is not None:
                lock.release()
            # The reservation remains indexed even without a per-profile journal.
            lifecycle.abandon(base)
            raise
        _leases.append(lease)
        return lease


def environments(scope, environment_id=None, *, remove=False):
    """Facts and explicit disposal only; applicability is the caller's decision."""
    if task_scope(scope):
        for path in (W.home()/'verification-leases').glob('*.json'):
            try:
                row=json.loads(path.read_text(encoding='utf8'))
                ident=row.get('environmentId')
                if (ident and environment_id in (None, ident) and row.get('clean')
                        and tuple(row.get('scope',[]))==tuple(scope)):
                    restore(scope, ident, busy=False)
            except (OSError, ValueError):
                continue
    with _lock:
        rows = [row for row in _leases if row["scope"] == scope and row.get("environmentId")
                and (environment_id is None or row["environmentId"] == environment_id)]
        if environment_id is not None and not rows:
            raise W.Problem("environment_unavailable", "指定环境不存在或已释放，未自动新建", 409)
        if remove:
            if environment_id is None or len(rows) != 1:
                raise W.Problem("invalid_arguments", "释放环境需要明确的 environment_id")
            if rows[0]["busy"]:
                raise W.Problem("environment_busy", "环境仍有进程使用，不能释放")
            rows[0]["busy"] = True
        else:
            from . import evidence
            return dict(ok=True, environments=[evidence.redact(dict(
                environmentId=row["environmentId"], busy=row["busy"], bytes=row.get("bytes", 0),
                lastCommand=row.get("lastCommand"), lastSourceRevision=row.get("lastRevision"),
                preparationDirectory="MODOT_TEST_ENV", lastIsolation="appcontainer-task-copy",
            )) for row in rows])
    discard(rows[0])
    return dict(ok=True, released=environment_id)


def reset_scratch(lease):
    scratch = lease["base"] / "scratch"
    for path in [scratch, *scratch.rglob("*")]:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise W.Problem("cache_changed", "验证临时目录含重定向，不能复用", 409)
    shutil.rmtree(scratch)
    scratch.mkdir()


def release(lease):
    # The exited process's lease stays busy while its outputs are measured.
    # Only publishing the new idle state needs the registry lock.
    try:
        lifecycle.release_child(lease['base'])
        protected = bool(lease.get('preparedSource'))
        roots = [p for p in lease['base'].iterdir() if (not protected or p.name != 'workspace')
                 and not (lease.get('runtimeSpec') and p.name == 'runtime')]
        if protected:
            roots.append(lease['base'] / 'workspace/.harness-check')
        total = sum(p.stat().st_size
                    for root in roots for p in ([root] if root.is_file() else root.rglob('*'))
                    if p.is_file() and not p.is_symlink())
        total += (lease.get('protectedBytes') or 0) if protected else 0
        total += lease.get('runtimeBytes') or 0
    except (OSError, lifecycle.Unproven):
        discard(lease)
        raise
    with _lock:
        lease.update(bytes=total, released=time.monotonic())
        retire = lease.get("drop") or total > MAX_RETAINED_BYTES
        if not retire:
            lease['busy'] = False
            record(lease)
    if retire:
        discard(lease)


def finish_scope(session, run):
    with _lock:
        for lease in list(_leases):
            if tuple(lease["scope"][:2]) == (session, run):
                # 活跃租约只标记待丢弃，实际回收留到进程族关闭后的 release，避免删除运行中的目录。
                if lease["busy"]:
                    lease["drop"] = True
                else:
                    discard(lease, synchronous=False)


def cleanup():
    with _lock:
        idle = [lease for lease in _leases if not lease['busy']]
        for lease in idle:
            lease['busy'] = True
    for lease in idle:
        try:
            if task_scope(lease['scope']) and not lease.get('drop'):
                # The handoff must run while the directory watcher can still
                # certify an immutable snapshot. Keep this lease exclusive.
                lease['baseStamp'] = fingerprint(lease['base'])
                if lease.get('preparedSource'):
                    from .directory_changes import changes
                    root = lease['preparedSource'][0]
                    _, delta = changes(root, lease.get('copyState', {}).get('cursor'))
                    # Ignored/untracked source modifications also invalidate it.
                    lease['sourceStamp'] = fingerprint(root, source=True) if delta == set() else None
                with _lock:
                    if not lease.get('drop'):
                        handoff = dict(lease, clean=True, busy=False, pid=0, identity=None,
                                       childPid=None, childIdentity=None, retainedAt=time.time())
                        record(handoff)
                        _leases.remove(lease)
                        if lease.get('useLock'):
                            lease.pop('useLock').release()
                        continue
        except (OSError, W.Problem, RuntimeError):
            pass
        discard(lease, synchronous=True)


atexit.register(cleanup)
