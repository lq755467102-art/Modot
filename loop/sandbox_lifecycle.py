"""Durable ownership and recovery for Windows AppContainer task resources.

The per-user index only owns resources it reserved, or legacy leases which
have a journal and matching Windows evidence. Lease payloads and handoffs
remain in their originating data directory.
"""

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from . import paths, runtime
from .filelock import FileLock, atomic_json

_LOG = logging.getLogger(__name__)
_PREFIX = {"runtime": "harness-task-runtime-", "lease": "harness-task-pool-", "sandbox": "harness-task-"}
_CONTAINER = re.compile(r"Harness\.Task\.[0-9a-f]{32}", re.IGNORECASE)
_KEY = re.compile(r"[0-9a-f]{64}")
_RUNTIME_PREFIX = _PREFIX["runtime"]
_MAX_RECORD = 8 * 1024 * 1024  # Older handoff journals may carry a source manifest.
_BATCH = 32
_RETRIES = (0, .3, 1.0)
_start_lock = threading.Lock()
_worker = None


class Unproven(ValueError):
    """A record or its current Windows evidence cannot authorize reclamation."""


def _absolute(path):
    path = Path(path)
    if not path.is_absolute() or path.drive.startswith("\\\\") or ".." in path.parts:
        raise Unproven("unsafe_path")
    return path


def _reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _directory(path):
    path = _absolute(path)
    info = path.lstat()
    if _reparse(info) or not stat.S_ISDIR(info.st_mode) or path.resolve(strict=True) != path or not info.st_ino:
        raise Unproven("unsafe_path")
    return [info.st_dev, info.st_ino]


def _mkdir(path):
    # Check the existing ancestor before creating any missing directories.
    if path.exists():
        return _directory(path)
    _mkdir(path.parent)
    path.mkdir(exist_ok=True)
    return _directory(path)


def _root():
    return _absolute(paths.sandbox_lifecycle_root())


def _ensure():
    root = _root()
    for name in ("records", "profiles"):
        _mkdir(root / name)
    if os.name != "nt":
        _mkdir(root / "locks")
    return root


def _key(path):
    return hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()


def _record(base, root=None):
    return (root or _root()) / "records" / (_key(base) + ".json")


def _use_lock(path, timeout=0):
    _directory(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if _reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Unproven("unsafe_lock")
    return FileLock(path, timeout=timeout)


def lease_lock(journal):
    return _use_lock(journal.with_suffix(".use.lock"))


def _lock(root, token, timeout=0):
    if os.name == "nt":
        from .windows_sandbox_evidence import mutex

        return mutex(_key(root / token), timeout)
    return _use_lock(root / "locks" / (token + ".lock"), timeout)


def _read(path):
    info = path.lstat()
    if _reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _MAX_RECORD:
        raise Unproven("unsafe_record")
    row = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(row, dict):
        raise Unproven("invalid_record")
    return row


def _current_owner():
    pid = os.getpid()
    creation = runtime.identity(pid)
    if not creation or creation == "unknown":
        raise Unproven("owner_unknown")
    return {"pid": pid, "identity": creation}


def _ended(pid, observed):
    if type(pid) is not int or pid <= 0 or not isinstance(observed, str) or observed in ("", "unknown"):
        return "identity_unknown"
    current = runtime.identity(pid)
    if current == "unknown":
        return "identity_unknown"
    return "live" if current == observed else None


def _policy(row, path):
    kind = row.get("kind")
    if row.get("version") != 1 or kind not in _PREFIX or row.get("state") not in ("active", "retired"):
        raise Unproven("invalid_record")
    base = _absolute(row["base"])
    parent = _absolute(row["parent"])
    prefix = _PREFIX[kind]
    suffix = base.name[len(prefix):]
    if (base.parent != parent or not base.name.startswith(prefix) or path.stem != _key(base)
            or not re.fullmatch(r"[0-9a-z_-]+", suffix)):
        raise Unproven("unsafe_path")
    if kind == "runtime":
        if row.get("name") is not None or not re.fullmatch("[0-9a-f]{32}", suffix):
            raise Unproven("invalid_record")
    elif not _CONTAINER.fullmatch(str(row.get("name", ""))):
        raise Unproven("invalid_container")
    elif not row.get("legacy") and row["name"] != "Harness.Task." + suffix:
        raise Unproven("invalid_container")
    return base


def _base_id(row, base):
    try:
        if _directory(base.parent) != row["parentId"]:
            raise Unproven("parent_replaced")
    except FileNotFoundError:
        return None
    try:
        actual = _directory(base)
    except FileNotFoundError:
        return None
    if row["baseId"] is not None and actual != row["baseId"]:
        raise Unproven("base_replaced")
    return actual


def allocate(kind, lease_dir=None):
    """Reserve the target before mkdir, ACL grants, container creation or copying."""
    root = _ensure()
    parent = Path(tempfile.gettempdir()).resolve(strict=True)
    ident = uuid.uuid4().hex
    base = parent / (_PREFIX[kind] + ident)
    row = dict(version=1, kind=kind, state="active", base=str(base), parent=str(parent),
               parentId=_directory(parent), baseId=None, name=None if kind == "runtime" else "Harness.Task." + ident,
               leaseDir=str(lease_dir) if lease_dir is not None else None, **_current_owner())
    if lease_dir is not None:
        _mkdir(_absolute(lease_dir))
    path = _record(base, root)
    with _lock(root, path.stem, 2):
        if base.exists() or base.is_symlink() or path.exists():
            raise Unproven("allocation_collision")
        atomic_json(path, row)
        base.mkdir()
        row["baseId"] = _directory(base)
        atomic_json(path, row)
    return base, row["name"]


def _edit(base, **changes):
    root = _ensure()
    path = _record(base, root)
    with _lock(root, path.stem, 2):
        row = _read(path)
        if _policy(row, path) != base or row["state"] != "active":
            raise Unproven("resource_retired")
        row.update(changes)
        atomic_json(path, row)


def claim_lease(base, name, lease_dir):
    """Refresh an indexed handoff under its existing per-lease use lock."""
    root = _ensure()
    path = _record(base, root)
    with _lock(root, path.stem, 2):
        try:
            row = _read(path)
        except FileNotFoundError:
            # The caller has already verified the clean legacy handoff's content stamp.
            row = _legacy_row(base, name, lease_dir, base.parent)
            from . import windows_sandbox_evidence as windows
            if not windows.acl_has_container(base, windows.container_sid(name)):
                raise Unproven("legacy_acl_unverified")
        if (_policy(row, path) != base or row["kind"] != "lease" or row["name"] != name
                or Path(row["leaseDir"]) != lease_dir or row["state"] != "active"
                or _base_id(row, base) != row["baseId"]):
            raise Unproven("resource_retired")
        row.update(_current_owner())
        atomic_json(path, row)


def link_runtime(base, runtime_paths):
    # A shared copy's retirement must see pending consumers as well as running ones.
    targets = sorted({path.parent for path in runtime_paths if path.parent.name.startswith(_RUNTIME_PREFIX)})
    if not targets:
        return
    root = _ensure()
    with ExitStack() as locks:
        for target in targets:
            record = _record(target, root)
            locks.enter_context(_lock(root, record.stem, 2))
            row = _read(record)
            if _policy(row, record) != target or row["kind"] != "runtime" or row["state"] != "active":
                raise Unproven("runtime_retired")
        _edit(base, runtimeUses=[str(target) for target in targets])


def record_child(base, pid, identity):
    if not identity or identity == "unknown":
        raise Unproven("child_identity_unknown")
    _edit(base, childPid=pid, childIdentity=identity)


def release_child(base):
    _edit(base, childPid=None, childIdentity=None, runtimeUses=[])


def runtime_ready(base, sizes):
    if isinstance(sizes, dict):
        _edit(base, bytes=sum(sizes.values()))


def job_name(container):
    if not _CONTAINER.fullmatch(str(container)):
        raise Unproven("invalid_container")
    return "Global\\Modot." + container


def _journal(row, base, stack):
    if row["kind"] != "lease":
        return None, None
    directory = _absolute(row["leaseDir"])
    try:
        _directory(directory)
    except FileNotFoundError:
        return None, None
    stack.enter_context(lease_lock(directory / (base.name + ".json")))
    found = []
    for suffix in (".retired", ".json"):
        path = directory / (base.name + suffix)
        try:
            payload = _read(path)
        except FileNotFoundError:
            continue
        if Path(payload["base"]) != base or payload["name"] != row["name"]:
            raise Unproven("journal_conflict")
        found.append((path, payload))
    if len(found) > 1:
        raise Unproven("journal_conflict")
    return found[0] if found else (None, None)


def _handoff(row):
    return (row.get("clean") is True and row.get("busy") is False
            and not row.get("pid") and not row.get("childPid"))


def _quiescent(row, journal=None, explicitly_retired=False):
    active = [row] + ([journal] if journal is not None else [])
    if not explicitly_retired:
        for record in active:
            reason = _ended(record.get("pid"), record.get("identity"))
            if reason:
                return "owner_" + reason
    for record in active:
        child = record.get("childPid")
        if child is not None:
            reason = _ended(child, record.get("childIdentity"))
            if reason:
                return "child_" + reason
    if row.get("name"):
        from .process_family import job_quiet
        if not job_quiet(job_name(row["name"])):
            return "job_active_or_unknown"
    return None


def _tree_bytes(base):
    """Never traverse a reparse point, even when only measuring a directory."""
    scan_root = "\\\\?\\" + str(base) if os.name == "nt" else base
    total, pending = 0, [scan_root]
    while pending:
        directory = pending.pop()
        entry_info = os.lstat(directory)
        if _reparse(entry_info) or not stat.S_ISDIR(entry_info.st_mode):
            raise Unproven("nested_reparse_point")
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if _reparse(info):
                    raise Unproven("nested_reparse_point")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(entry.path)
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                else:
                    raise Unproven("unsafe_leaf")
    return total


def _has_consumer(root, base):
    # While this runtime's lock is held, new links cannot be published.
    for path in (root / "records").glob("*.json"):
        try:
            row = _read(path)
        except FileNotFoundError:
            continue
        if str(base) in row.get("runtimeUses", []):
            return True
    return False


def _destroy(root, path, row, base, journal_path):
    from . import safe_executor as executor
    from . import windows_sandbox_evidence as windows

    actual = _base_id(row, base)
    sid, present = None, False
    if row["name"]:
        sid = windows.container_sid(row["name"])
        present = windows.container_present(row["name"], sid)
    if row["baseId"] is None and present:
        raise Unproven("unconfirmed_directory")
    if row["state"] != "retired":
        row["state"] = "retired"
        atomic_json(path, row)
    if journal_path is not None and journal_path.suffix == ".json":
        journal_path = journal_path.rename(journal_path.with_suffix(".retired"))
    if actual is not None:
        if row["baseId"] is not None:
            row["bytes"] = _tree_bytes(base)
            atomic_json(path, row)
        if actual != _base_id(row, base):
            raise Unproven("base_replaced")
        if row["baseId"] is None:
            # A kill between mkdir and stamping can only have left an empty reservation.
            base.rmdir()
        elif base.parent == Path(tempfile.gettempdir()).resolve():
            executor._remove(base)
        else:
            executor._remove(base, parent=base.parent)
    if present:
        result = executor._delete_profile(row["name"])
        if windows.container_present(row["name"], sid):
            raise OSError(f"AppContainer removal was not confirmed (0x{result & 0xFFFFFFFF:08x})")
    if journal_path is not None:
        journal_path.unlink(missing_ok=True)
    path.unlink()
    return "removed", row.get("bytes")


def _reclaim(root, path, *, owned=False, background=True):
    try:
        with ExitStack() as stack:
            if background:
                stack.enter_context(_lock(root, "recovery"))
            stack.enter_context(_lock(root, path.stem))
            _directory(path.parent)
            try:
                row = _read(path)
            except FileNotFoundError:
                return "missing", None
            base = _policy(row, path)
            journal_path, journal = _journal(row, base, stack)
            if journal_path is not None and journal_path.suffix == ".json" and _handoff(journal):
                return "retained_handoff", None
            retired = row["state"] == "retired"
            if journal_path is not None and journal_path.suffix == ".retired":
                # An abandoned owner can leave busy=True in its old receipt.
                # Only our durable retirement may override that legacy bit.
                if not retired and journal.get("busy") is not False:
                    raise Unproven("retired_busy")
                retired = True
            if owned:
                owner = _current_owner()
                if any(row.get(key) != value for key, value in owner.items()):
                    raise Unproven("not_owner")
                retired = True
            if reason := _quiescent(row, journal, retired):
                return reason, None
            if row["kind"] == "runtime" and _has_consumer(root, base):
                return "runtime_in_use", None
            return _destroy(root, path, row, base, journal_path)
    except TimeoutError:
        return "locked", None
    except Unproven as error:
        return str(error), None
    except (OSError, TypeError, ValueError, KeyError) as error:
        _LOG.debug("Sandbox recovery retained %s: %s", path.name, error)
        return "retry", None


def abandon(base):
    try:
        _edit(base, state="retired")
    except (OSError, ValueError, KeyError):
        _LOG.exception("Failed to retire an uncommitted sandbox reservation")


def retire_owned(base):
    """A synchronous reclaim attempt for the caller who just closed its job."""
    return _reclaim(_root(), _record(base), owned=True, background=False)


def _legacy_row(base, name, directory, parent):
    if (base.parent != parent or not re.fullmatch(r"harness-task-pool-[\w-]+", base.name)
            or not _CONTAINER.fullmatch(str(name))):
        raise Unproven("unsafe_legacy_path")
    parent_id = _directory(parent)
    try:
        base_id = _directory(base)
    except FileNotFoundError:
        base_id = None
    return dict(version=1, kind="lease", state="active", base=str(base), parent=str(parent),
                parentId=parent_id, baseId=base_id, name=name, leaseDir=str(directory), legacy=True)


def reclaim_journal(path, parent=None, *, background=True):
    """Also recognize existing .retired and abandoned per-profile journals."""
    try:
        root = _ensure()
        _directory(_absolute(path.parent))
        original = _read(path)
        base = _absolute(original["base"])
        if path.stem != base.name:
            return "legacy_mismatch", None
        record = _record(base, root)
        if not record.exists():
            with _lock(root, record.stem), lease_lock(path):
                if not record.exists():
                    journal = _read(path)
                    if journal != original:
                        return "journal_changed", None
                    retired = path.suffix == ".retired" and journal.get("busy") is False
                    if path.suffix == ".retired" and not retired:
                        return "retired_busy", None
                    if not retired and _handoff(journal):
                        return "retained_handoff", None
                    row = _legacy_row(base, journal["name"], path.parent,
                                      parent or Path(tempfile.gettempdir()).resolve())
                    row.update(pid=journal.get("pid"), identity=journal.get("identity"),
                               childPid=journal.get("childPid"), childIdentity=journal.get("childIdentity"))
                    if reason := _quiescent(row, explicitly_retired=retired):
                        return reason, None
                    from . import windows_sandbox_evidence as windows
                    sid = windows.container_sid(row["name"])
                    present = windows.container_present(row["name"], sid)
                    if row["baseId"] is not None and not windows.acl_has_container(base, sid):
                        return "legacy_acl_unverified", None
                    if row["baseId"] is None and not present:
                        # Both sides were already removed by an earlier collector.
                        path.unlink()
                        return "journal_only", None
                    row["state"] = "retired" if retired else "active"
                    atomic_json(record, row)
        return _reclaim(root, record, background=background)
    except FileNotFoundError:
        return "missing", None
    except TimeoutError:
        return "locked", None
    except Unproven as error:
        return str(error), None
    except (OSError, ValueError, KeyError, TypeError) as error:
        _LOG.debug("Sandbox recovery retained journal %s: %s", path.name, error)
        return "retry", None


def _profile(root, data, temp):
    data = data.resolve(strict=True)
    directory = data / "runs" / "workbench" / "verification-leases"
    _directory(data)
    _directory(temp)
    atomic_json(root / "profiles" / (_key(data) + ".json"), dict(data=str(data), temp=str(temp)))
    return directory


def _profile_entries(root):
    for path in (root / "profiles").glob("*.json"):
        try:
            row = _read(path)
            data, temp = _absolute(row["data"]), _absolute(row["temp"])
            if path.stem != _key(data):
                continue
            _directory(data)
            _directory(temp)
            yield data / "runs" / "workbench" / "verification-leases", temp
        except (OSError, ValueError, KeyError, TypeError):
            continue


def _diagnose(root, parents):
    """Sample legacy names without reclaiming a directory without a record."""
    owned, known_names = set(), set()
    for path in (root / "records").glob("*.json"):
        try:
            row = _read(path)
            owned.add(_policy(row, path))
            if row.get("name"):
                known_names.add(row["name"].casefold())
        except (OSError, ValueError, KeyError, TypeError):
            continue
    for directory, parent in _profile_entries(root):
        try:
            _directory(directory)
            for path in directory.glob("*"):
                if path.suffix in (".json", ".retired"):
                    row = _read(path)
                    base = Path(row["base"])
                    if base == parent / path.stem and _CONTAINER.fullmatch(str(row.get("name"))):
                        owned.add(base)
                        known_names.add(row["name"].casefold())
        except (OSError, ValueError, KeyError, TypeError):
            continue
    unknown = []
    for parent in parents:
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    if entry.name.startswith("harness-task-") and Path(entry.path) not in owned and entry.is_dir(follow_symlinks=False):
                        unknown.append({"path": entry.path, "reason": "ownership_unverified", "bytes": None})
                        if len(unknown) == 65:
                            break
        except OSError:
            continue
        if len(unknown) == 65:
            break
    from . import windows_sandbox_evidence as windows
    registrations, registry_truncated = windows.task_registrations() if os.name == "nt" else ([], False)
    unverified = [{"name": name, "sid": sid, "reason": "ownership_unverified"}
                  for name, sid in registrations if name.casefold() not in known_names and _CONTAINER.fullmatch(name)]
    return dict(unverified=unknown[:64], unverifiedTruncated=len(unknown)>64,
                unverifiedContainers=unverified[:64],
                unverifiedContainersTruncated=registry_truncated or len(unverified)>64)


def recover(data, temp=None):
    """Bound the work of a startup sweep. Return a diagnostic receipt."""
    from .verification_workspace import recovery_turn

    root = _ensure()
    temp = temp or Path(tempfile.gettempdir()).resolve(strict=True)
    _profile(root, data, temp)
    profiles = list(_profile_entries(root))
    candidates = [(p, None) for p in sorted((root / "records").glob("*.json")) if _KEY.fullmatch(p.stem)]
    for directory, parent in profiles:
        try:
            _directory(directory)
            candidates.extend((p, parent) for p in sorted(directory.glob("*")) if p.suffix in (".retired", ".json"))
        except (OSError, ValueError):
            continue
    # Prefer a rotating slice so a backlog of live/unknown records cannot hide later entries.
    progress = root / "recovery-cursor.json"
    try:
        last = _read(progress).get("cursor")
    except (OSError, ValueError):
        last = None
    candidates.sort(key=lambda entry: str(entry[0]))
    rotation = next((i for i, entry in enumerate(candidates) if last and str(entry[0]) > last), 0)
    ordered = candidates[rotation:] + candidates[:rotation]
    pending = ordered[:_BATCH]
    outcomes = {}
    sizes = {}
    for delay in _RETRIES:
        if not pending:
            break
        if delay:
            time.sleep(delay)
        retry = []
        for path, parent in pending:
            if recovery_turn():
                result, size = reclaim_journal(path, parent) if parent else _reclaim(root, path)
            else:
                result, size = "runtime_preparing", None
            outcomes[str(path)] = result
            if size is not None and result == "removed":
                sizes[str(path)] = size
            if result in {"locked", "retry", "runtime_in_use", "job_active_or_unknown", "child_live", "runtime_preparing"}:
                retry.append((path, parent))
        pending = retry
    if ordered:
        atomic_json(progress, {"cursor": str(ordered[min(len(ordered), _BATCH) - 1][0])})
    diagnostic = _diagnose(root, {temp, *(parent for _, parent in profiles)})
    receipt = dict(time=time.time(), outcomes=dict(Counter(outcomes.values())), logicalBytes=sum(sizes.values()),
                   measured=len(sizes), pending=len(pending), moreCandidates=max(0, len(candidates) - _BATCH),
                   **diagnostic)
    atomic_json(root / "last-recovery.json", receipt)
    return receipt


def start_recovery(data):
    if os.name != "nt":
        return
    global _worker

    def background():
        try:
            receipt = recover(data)
            # Process the initial backlog in batches, yielding to foreground work.
            remaining = receipt["moreCandidates"]
            while remaining > 0:
                time.sleep(.3)
                recover(data)
                remaining -= _BATCH
        except (OSError, ValueError):
            _LOG.exception("Could not run sandbox recovery")

    with _start_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=background, daemon=True, name="sandbox-resource-recovery")
        _worker.start()
