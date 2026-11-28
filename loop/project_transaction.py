"""Per-task checkout view. Lower files are opened on demand. Only mutations have a journal."""

from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import threading
import uuid

from . import exclusive_file, repository_staging as staging, safe_executor, workbench as W
from .filelock import FileLock


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _conflict(name):
    return W.Problem('revision_conflict', '项目文件或工作树已变化：' + name, 409)


def _check_path(name):
    if not isinstance(name, str) or '\0' in name:
        raise PermissionError('无效的项目路径')
    path = PureWindowsPath(name)
    if path.anchor or not path.parts or any(
        part == '..' or os.path.isreserved(part) or part.endswith(('.', ' '))
        or part.lower() in safe_executor.EXCLUDED | safe_executor.SECRET_NAMES
        or part.lower().startswith(('.env', '.modot-record-'))
        or part.lower().endswith(('.pem', '.key', '.pfx', '.p12'))
        for part in path.parts
    ):
        raise PermissionError('此路径不属于可用项目文件')
    return path.as_posix()


def _key(path):
    return list(staging._identity(path.lstat()))


def _ordinary(info):
    if (info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
            or stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
        raise PermissionError('不能跟随重解析点、硬链接或特殊文件')


class View:
    def __init__(self, store, marker, record):
        self.store, self.marker, self.record = store, marker, record
        self.directory = store / marker.stem
        self.project = Path(record['project'])
        self.entries = record['entries']
        self.lock = threading.RLock()
        self.guard = lambda: None
        self.path_guard = lambda name,write: None
        self.readonly = False
        self.parent = None
        self.generation = 0
        self.verified = None
        self.failed_checks = set()
        self.undo_edits = {}

    @classmethod
    def create(cls, store, project, task):
        store, project = Path(store).absolute(), Path(project).absolute()
        store.mkdir(parents=True, exist_ok=True)
        if (project != project.resolve(strict=True) or store.resolve(strict=True) != store
                or store.is_relative_to(project) or project.is_relative_to(store)):
            raise PermissionError('项目和任务暂存必须是独立的本机目录')
        owner = W.RT.identity(os.getpid())
        if not owner or owner == 'unknown':
            raise W.Problem('owner_unknown', '不能核对任务进程身份', 503)
        key = uuid.uuid4().hex
        path = store / key
        with FileLock(store / 'registry.lock'):
            path.mkdir()
            record = dict(version=1, task=task, project=str(project), rootKey=_key(project),
                          stageKey=_key(path), storeKey=_key(store), pid=os.getpid(), owner=owner,
                          phase='open', entries={}, applying=[])
            view = cls(store, store / (key + '.json'), record)
            try:
                view.save()
            except BaseException:
                path.rmdir()
                raise
        return view

    def revision(self):
        return 'stage:' + self.marker.stem + ':' + str(self.generation)

    def changed(self):
        self.generation += 1
        self.verified = None

    def save(self):
        W.atomic_json(self.marker, self.record)

    def _usable(self, write=False):
        self.guard()
        if self.record['phase'] != 'open':
            raise W.Problem('project_stage_closed', '本轮的暂存已关闭', 409)
        if write and self.readonly:
            raise PermissionError('项目当前只读')

    def normalize(self, name):
        name = _check_path(name)
        if self.path_guard(name, False):
            raise PermissionError('项目读取保护：' + name)
        return name

    @contextmanager
    def pinned(self, name):
        """Hold existing directory identities while resolving a lower entry."""
        with ExitStack() as stack:
            kernel = staging._kernel()
            _, info = stack.enter_context(staging._pin(kernel, self.project))
            if list(staging._identity(info)) != self.record['rootKey'] or self.project.resolve() != self.project:
                raise _conflict(str(self.project))
            parent = self.project
            for part in Path(name).parts[:-1]:
                parent /= part
                try:
                    stack.enter_context(staging._pin(kernel, parent))
                except FileNotFoundError:
                    break
            yield self.project / name

    def _data(self, entry, kind='upper'):
        return self.directory / (entry['id'] + '.' + kind)

    def _select(self, name, lower):
        parts = PureWindowsPath(name).parts
        for n in range(1, len(parts)):
            parent = self.entries.get('/'.join(parts[:n]).casefold())
            if parent and parent['kind'] != 'dir':
                raise FileNotFoundError(name)
        e = self.entries.get(name.casefold())
        if e:
            if e['kind'] == 'deleted':
                raise FileNotFoundError(name)
            return self._data(e)
        return lower

    @contextmanager
    def existing(self, name):
        # Pin the final entry too. This prevents a concurrent substitution by a junction.
        with self.pinned(name) as lower:
            path = self._select(name, lower)
            if self.parent and path == lower:
                with self.parent.existing(name) as entry:
                    yield entry
                return
            with staging._pin(staging._kernel(), path) as (_, info):
                _ordinary(info)
                yield path, info

    def info(self, name=''):
        with self.lock:
            self._usable()
            if not name:
                with self.pinned('') as root:
                    return root.stat()
            with self.existing(self.normalize(name)) as (_, info):
                return info

    def list(self, name=''):
        with self.lock:
            self._usable()
            name = self.normalize(name) if name else ''
            if not stat.S_ISDIR(self.info(name).st_mode):
                raise NotADirectoryError(name)
            names = {}
            if self.parent:
                try:
                    names = {n.casefold():n for n in self.parent.list(name)}
                except FileNotFoundError:
                    pass
            with self.pinned(name + '/_' if name else '_') as sentinel:
                folder = sentinel.parent
                if self.parent is None and folder.is_dir():
                    for p in folder.iterdir():
                        try:
                            self.normalize(p.relative_to(self.project).as_posix())
                            _ordinary(p.lstat())
                        except (OSError, ValueError):
                            continue
                        names[p.name.casefold()] = p.name
            for e in self.entries.values():
                parent, _, leaf = e['name'].rpartition('/')
                if parent.casefold() == name.casefold():
                    if e['kind'] == 'deleted':
                        names.pop(leaf.casefold(), None)
                    else:
                        names[leaf.casefold()] = leaf
            return list(names.values())

    def read(self, name, offset, count):
        if offset < 0 or count < 0:
            raise ValueError('invalid byte range')
        with self.lock:
            self._usable()
            with self.existing(self.normalize(name)) as (p, _), p.open('rb') as f:
                f.seek(offset)
                return f.read(count)

    def _capacity(self, entries, extra=0):
        undo_bytes = sum(len(previous) for _, _, previous in self.undo_edits.values() if isinstance(previous, bytes))
        if len(entries) > safe_executor.MAX_FILES or extra + undo_bytes + sum(
            e.get('size', 0) + e.get('beforeSize', 0) for e in entries.values()
        ) > safe_executor.MAX_BYTES:
            raise W.Problem('stage_capacity', '本轮修改文件及其回退副本超出暂存额度', 413)

    def _entry(self, name):
        if self.path_guard(name, True):
            raise PermissionError('项目写入保护：' + name)
        key = name.casefold()
        if key in self.entries:
            return self.entries[key]
        parent = name.rpartition('/')[0]
        if not stat.S_ISDIR(self.info(parent).st_mode):
            raise NotADirectoryError(parent)
        e = dict(id=uuid.uuid4().hex, name=name, kind='deleted', before=None, size=0, beforeSize=0)
        self._capacity({**self.entries, key: e})
        with self.pinned(name) as lower:
            try:
                info = self.parent.info(name) if self.parent else lower.lstat()
            except FileNotFoundError:
                info = None
            if info:
                _ordinary(info)
                if stat.S_ISDIR(info.st_mode):
                    e.update(before='directory', kind='dir')
                    self._data(e).mkdir()
                else:
                    self._capacity(self.entries, 2 * info.st_size)
                    with self.existing(name) as (p, _), p.open('rb') as src, self._data(e, 'before').open('xb') as dest:
                        remaining = info.st_size
                        while remaining:
                            block = src.read(min(1024 * 1024, remaining))
                            if not block:
                                raise _conflict(name)
                            dest.write(block)
                            remaining -= len(block)
                        if src.read(1):
                            raise _conflict(name)
                    shutil.copyfile(self._data(e, 'before'), self._data(e))
                    e.update(before=_digest(self._data(e, 'before')), kind='file', size=info.st_size, beforeSize=info.st_size)
        self.entries[key] = e
        self.save()
        return e

    def _file(self, name, size):
        e = self._entry(self.normalize(name))
        if e['kind'] == 'dir' or e['before'] == 'directory':
            raise IsADirectoryError(name)
        self._capacity(self.entries, size - e['size'])
        p = self._data(e)
        if e['kind'] == 'deleted':
            p.write_bytes(b'')
        e.update(kind='file', size=size)
        self.changed()
        return p

    def write(self, name, offset, data):
        with self.lock:
            self._usable(True)
            if offset < 0:
                raise ValueError('invalid offset')
            e = self._entry(self.normalize(name))
            with self._file(name, max(e['size'], offset + len(data))).open('r+b') as f:
                f.seek(offset)
                return f.write(data)

    def replace(self, name, data):
        with self.lock:
            self._usable(True)
            self._file(name, len(data)).write_bytes(data)

    def resize(self, name, size):
        with self.lock:
            self._usable(True)
            with self._file(name, size).open('r+b') as f:
                f.truncate(size)

    def mkdir(self, name):
        with self.lock:
            self._usable(True)
            e = self._entry(self.normalize(name))
            if e['kind'] != 'deleted':
                raise FileExistsError(name)
            self._data(e).mkdir(exist_ok=True)
            e['kind'] = 'dir'
            self.changed()
            self.save()

    def remove(self, name):
        with self.lock:
            self._usable(True)
            e = self._entry(self.normalize(name))
            if e['kind'] == 'deleted':
                raise FileNotFoundError(name)
            if e['kind'] == 'dir' and self.list(name):
                raise OSError(39, '目录非空')
            p = self._data(e)
            p.rmdir() if e['kind'] == 'dir' else p.unlink()
            e.update(kind='deleted', size=0)
            self.changed()
            self.save()

    def _final(self, e):
        if e['kind'] == 'file':
            return _digest(self._data(e))
        return 'directory' if e['kind'] == 'dir' else None

    def edit(self, name, op, previous, data):
        """Keep one undo point per file and replace only a fully written candidate."""
        with self.lock:
            self._usable(True)
            key = self.normalize(name).casefold()
            entry = self._entry(name)
            expected = W.digest(previous) if previous is not None else None
            if self._final(entry) != expected:
                raise _conflict(name)
            # The journal already owns the original bytes. Later edits may need
            # their previous staged content until this tool operation is undone.
            undo = 'before' if previous is not None and expected == entry['before'] else previous
            prior = self.undo_edits.get(key)
            self.undo_edits[key] = op, W.digest(data), undo
            temporary = self._data(entry, 'pending')
            try:
                self._capacity(self.entries, len(data))
                temporary.write_bytes(data)
                os.replace(temporary, self._data(entry))
            except BaseException:
                if prior is None:
                    self.undo_edits.pop(key)
                else:
                    self.undo_edits[key] = prior
                raise
            finally:
                temporary.unlink(missing_ok=True)
            entry.update(kind='file', size=len(data))
            self.changed()
            self.save()

    def undo(self, op):
        with self.lock:
            self._usable(True)
            for key, (ident, expected, previous) in self.undo_edits.items():
                if op != ident:
                    continue
                e = self.entries[key]
                if self._final(e) != expected:
                    raise _conflict(e['name'])
                if previous is None:
                    self.remove(e['name'])
                else:
                    content = self._data(e, 'before').read_bytes() if previous == 'before' else previous
                    self.undo_edits.pop(key)
                    try:
                        self.replace(e['name'], content)
                    except BaseException:
                        self.undo_edits[key] = ident, expected, previous
                        raise
                self.undo_edits.pop(key, None)
                return dict(path=e['name'], staged=True, state='completed', revision=self._final(e),
                            restoresOperationId=op, projectRevision=self.revision())
        raise W.Problem('not_own_latest_write', '只可撤销本轮在该文件上的最后一次暂存编辑', 409)

    def changes(self):
        with self.lock:
            return {e['name']: e['kind'] for e in self.entries.values() if e['before'] != self._final(e)}

    def _current(self, path):
        try:
            with staging._pin(staging._kernel(), path) as (_, info):
                _ordinary(info)
                return 'directory' if stat.S_ISDIR(info.st_mode) else _digest(path)
        except FileNotFoundError:
            return None

    def _apply(self, e, before, after, data):
        with self.pinned(e['name']) as dst:
            if after == 'directory':
                dst.mkdir()
            elif before == 'directory':
                dst.rmdir()
            else:
                exclusive_file.write(dst, self.project, data.read_bytes() if after else None,
                                     before, safe_executor.MAX_BYTES)

    def _rollback(self):
        conflicts = []
        for key in reversed(self.record['applying']):
            e = self.entries[key]
            with self.pinned(e['name']) as dst:
                current = self._current(dst)
                if current == e['before']:
                    continue
                if current != e['after']:
                    conflicts.append(e['name'])
                    continue
                try:
                    self._apply(e, current, e['before'], self._data(e, 'before'))
                except (OSError, exclusive_file.Conflict):
                    conflicts.append(e['name'])
        if conflicts:
            raise _conflict(', '.join(conflicts))
        self.record.update(phase='open', applying=[])
        self.save()

    def publish(self, live, before=None, after=None):
        if self.parent:
            raise W.Problem('disposable_check', '临时检查层只能丢弃', 409)
        with self.lock, FileLock(W.writer_lock_path(self.project), check=live):
            self._usable()
            if self.record.get('mounts'):
                raise W.Problem('project_unmount_busy', '本轮仍有活动命令，尚未写回', 409)
            live()
            changed = []
            for key, e in self.entries.items():
                e['after'] = self._final(e)
                if e['before'] == e['after']:
                    continue
                with self.pinned(e['name']) as dst:
                    if self._current(dst) != e['before']:
                        raise _conflict(e['name'])
                changed.append(key)
            changed.sort(key=lambda k: (0, k.count('/'), k) if self.entries[k]['after'] == 'directory'
                         else (2, -k.count('/'), k) if self.entries[k]['before'] == 'directory' else (1, 0, k))
            plan = [self.entries[k] for k in changed]
            if plan and before:
                before(plan)
            self.record['phase'] = 'publishing'
            self.save()
            try:
                for key in changed:
                    live()
                    e = self.entries[key]
                    self.record['applying'].append(key)
                    self.save()  # Intent reaches disk before the checkout write.
                    self._apply(e, e['before'], e['after'], self._data(e))
                live()
                if plan and after:
                    after(plan)
                live()
                self.record['phase'] = 'published'
                self.save()
            except BaseException:
                self._rollback()
                raise
            self.discard()
            return [e['name'] for e in plan]

    def discard(self):
        with self.lock:
            if self.record['phase'] == 'publishing':
                raise _conflict('需先核对中断的写回')
            if self.record.get('mounts'):
                raise _conflict('暂存卷仍被进程持有')
            with staging._pin(staging._kernel(), self.store) as (_, info):
                if list(staging._identity(info)) != self.record['storeKey'] or self.store.resolve() != self.store:
                    raise _conflict('暂存目录')
                try:
                    staging._remove_entry(staging._kernel(), self.directory, tuple(self.record['stageKey']))
                except FileNotFoundError:
                    pass
                self.marker.unlink(missing_ok=True)
            self.record['phase'] = 'closed'


def recover(store):
    store = Path(store).absolute()
    if not store.is_dir() or store.resolve() != store:
        return []
    reclaimed = []
    with FileLock(store / 'registry.lock'):
        for marker in store.glob('*.json'):
            if not re.fullmatch(r'[0-9a-f]{32}', marker.stem):
                continue
            try:
                _ordinary(marker.lstat())
                record = json.loads(marker.read_bytes())
                if (record.get('version') != 1 or record.get('storeKey') != _key(store)
                        or not staging._key(record.get('stageKey')) or not staging._key(record.get('rootKey'))
                        or type(record.get('pid')) is not int or not str(record.get('owner', '')).isdigit()
                        or W.RT.identity(record['pid']) in (record['owner'], 'unknown')):
                    continue
                view = View(store, marker, record)
                if record.get('mounts'):
                    from .project_mount import detach
                    for mount in record['mounts']:
                        detach(mount)
                    record['mounts'] = []
                    view.save()
                if record['phase'] == 'publishing':
                    if _key(view.directory) != record['stageKey'] or _key(view.project) != record['rootKey']:
                        continue
                    if not isinstance(view.entries, dict) or not all(
                        key == _check_path(e['name']).casefold() and re.fullmatch(r'[0-9a-f]{32}', e['id'])
                        and all(v in (None, 'directory') or isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v)
                                for v in (e.get('before'), e.get('after')))
                        for key,e in view.entries.items()
                    ):
                        continue
                    with FileLock(W.writer_lock_path(view.project)):
                        view._rollback()
                elif record['phase'] not in ('open', 'published'):
                    continue
                view.discard()
                reclaimed.append(marker.stem)
            except (OSError, ValueError, KeyError, W.Problem):
                continue
    return reclaimed
