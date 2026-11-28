"""Windows Dokan 2 adapter for the task's copy-on-write view. The system driver supplies the mount."""

import ctypes as C
from ctypes import wintypes as T
from functools import lru_cache
import os
from pathlib import Path
import re
import stat
import subprocess

from . import workbench as W

P = C.c_void_p
U32, U64 = C.c_uint32, C.c_uint64
PTR = C.POINTER
FileTime = T.FILETIME


class Options(C.Structure):
    _fields_ = [('version', C.c_ushort), ('singleThread', C.c_ubyte), ('flags', U32), ('context', U64),
                ('mount', T.LPCWSTR), ('unc', T.LPCWSTR), ('timeout', U32), ('allocation', U32),
                ('sector', U32), ('securityLength', U32), ('security', C.c_ubyte * 16384)]


class FileInfo(C.Structure):
    _fields_ = [('context', U64), ('internal', U64), ('options', P), ('processing', P), ('pid', U32),
                ('directory', C.c_ubyte), ('deletePending', C.c_ubyte), ('paging', C.c_ubyte),
                ('synchronous', C.c_ubyte), ('noCache', C.c_ubyte), ('append', C.c_ubyte)]


class ByHandle(C.Structure):
    _fields_ = [('attributes', U32), ('created', FileTime), ('accessed', FileTime), ('written', FileTime),
                ('volume', U32), ('sizeHigh', U32), ('sizeLow', U32), ('links', U32),
                ('indexHigh', U32), ('indexLow', U32)]


class Operations(C.Structure):
    _fields_ = [(v, P) for v in (
        'create cleanup close read write flush info find find_pattern set_attributes set_time '
        'delete_file delete_dir move end allocation lock unlock space volume mounted unmounted '
        'get_security set_security streams').split()]


@lru_cache(maxsize=1)
def _driver():
    try:
        lib = C.WinDLL('dokan2.dll', winmode=0x800, use_last_error=True)  # System32 only
        lib.DokanInit.argtypes, lib.DokanInit.restype = [], None
        lib.DokanCreateFileSystem.argtypes = [PTR(Options), PTR(Operations), PTR(P)]
        lib.DokanCreateFileSystem.restype = C.c_int
        lib.DokanCloseHandle.argtypes, lib.DokanCloseHandle.restype = [P], None
        lib.DokanRemoveMountPoint.argtypes, lib.DokanRemoveMountPoint.restype = [T.LPCWSTR], T.BOOL
        lib.DokanInit()
        return lib
    except (OSError, AttributeError) as e:
        raise W.Problem('project_driver_missing', '项目命令需要安装并启动 Dokan 2 文件系统驱动。可在调用日志中安装驱动。', 503) from e


@lru_cache(maxsize=1)
def _user_sid():
    result = subprocess.check_output([str(Path(os.environ['WINDIR']) / 'System32' / 'whoami.exe'),
                                      '/user', '/fo', 'csv', '/nh'], creationflags=subprocess.CREATE_NO_WINDOW)
    match = re.search(rb'"(S-1-[0-9-]+)"\s*$', result)
    if not match:
        raise PermissionError('无法核对暂存视图所属用户')
    return match[1].decode('ascii')


def _descriptor(sandbox_sid):
    user = _user_sid()
    entries = [user, 'SY']
    if sandbox_sid:
        if not re.fullmatch(r'S-1-15-2(?:-\d+)+', sandbox_sid):
            raise PermissionError('无效的隔离身份')
        entries.append(sandbox_sid)
    sddl = 'O:' + user + 'G:' + user + 'D:P' + ''.join('(A;;FA;;;' + s + ')' for s in entries)
    adv = C.WinDLL('advapi32', use_last_error=True)
    convert = adv.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [T.LPCWSTR, U32, PTR(P), PTR(U32)]
    pointer, size = P(), U32()
    if not convert(sddl, 1, C.byref(pointer), C.byref(size)):
        raise C.WinError(C.get_last_error())
    try:
        return C.string_at(pointer, size.value)
    finally:
        free = C.WinDLL('kernel32').LocalFree
        free.argtypes, free.restype = [P], P
        free(pointer)


def _time(ns):
    value = ns // 100 + 116444736000000000
    return FileTime(value & 0xffffffff, value >> 32)


def _status(error):
    if isinstance(error, FileNotFoundError):
        return 0xc0000034
    if isinstance(error, FileExistsError):
        return 0xc0000035
    if isinstance(error, NotADirectoryError):
        return 0xc0000103
    if isinstance(error, IsADirectoryError):
        return 0xc00000ba
    if isinstance(error, W.Problem) and error.code == 'stage_capacity':
        return 0xc000007f
    return 0xc0000022


def detach(record):
    """Remove only the recorded NTFS junction itself, never descend into its target."""
    from . import repository_staging as staging
    from .project_transaction import _key
    p = Path(record['path'])
    if not os.path.lexists(p) or not p.is_junction():
        return
    with staging._pin(staging._kernel(), p.parent):
        volume = os.readlink(p)
        if (_key(p.parent) != record['parentKey'] or _key(p) != record['key']
                or volume != record['volume'] or not re.fullmatch(r'.{4}Volume\{[0-9A-Fa-f-]{36}\}\\?', volume)):
            raise W.Problem('project_unmount_busy', '无法核对本次挂载的目录身份', 409)
        os.rmdir(p)


def _name(raw):
    if not raw.startswith('\\') or raw.startswith('\\\\'):
        raise PermissionError('invalid volume-relative path')
    return raw[1:].replace('\\', '/')


class CommandScratch:
    """Command-only files have no lower checkout and cannot be published."""
    def __init__(self, parent):
        from uuid import uuid4
        from .project_transaction import _key
        self.root = parent / ('c' + uuid4().hex[:12])
        self.root.mkdir()
        self.key = _key(self.root)
        self.sizes = {}

    def normalize(self, name):
        from .project_transaction import _check_path
        return _check_path(name)

    def path(self, name):
        if not name:
            return self.root
        parts = self.normalize(name).split('/')
        if parts[0].casefold() != 'material':
            raise PermissionError(name)
        return self.root.joinpath('.harness-check', *parts[1:])

    def info(self, name):
        return self.path(name).stat()

    def list(self, name=''):
        return [('material' if not name and p.name == '.harness-check' else p.name) for p in self.path(name).iterdir()]

    def mkdir(self, name):
        self.path(name).mkdir()

    def read(self, name, offset, count):
        with self.path(name).open('rb') as f:
            f.seek(offset)
            return f.read(count)

    def _size(self, name, value):
        from .safe_executor import MAX_BYTES, MAX_FILES
        if (len(self.sizes) >= MAX_FILES and name not in self.sizes
                or sum(self.sizes.values()) - self.sizes.get(name, 0) + value > MAX_BYTES):
            raise W.Problem('stage_capacity', '命令临时产物超过额度', 413)
        self.sizes[name] = value

    def write(self, name, offset, data):
        self._size(name, max(self.sizes.get(name, 0), offset + len(data)))
        with self.path(name).open('r+b') as f:
            f.seek(offset)
            return f.write(data)

    def replace(self, name, data):
        self._size(name, len(data))
        self.path(name).write_bytes(data)

    def resize(self, name, size):
        self._size(name, size)
        with self.path(name).open('r+b') as f:
            f.truncate(size)

    def remove(self, name):
        p = self.path(name)
        p.rmdir() if p.is_dir() else p.unlink()
        self.sizes.pop(name, None)

    def discard(self):
        from . import repository_staging as staging
        staging._remove_entry(staging._kernel(), self.root, tuple(self.key))


class Mount:
    """A single process family gets access through a task-only volume security descriptor."""
    def __init__(self, view, target, sandbox_sid=None, readonly=False, scratch=None, excluded=()):
        self.view, self.target = view, Path(target).absolute()
        self.readonly = readonly
        self.excluded = frozenset(name.casefold() for name in excluded)
        self.material = None
        if scratch is not None:
            self.material = CommandScratch(view.directory)
        self.lib = _driver()
        self.security = _descriptor(sandbox_sid)
        self.options, self.ops, self.handle = Options(), Operations(), P()
        self.options.version = 231
        self.options.flags = 128 | (8 if readonly and not self.material else 0)
        self.options.mount = str(self.target)
        self.options.timeout = 30000
        self.options.securityLength = len(self.security)
        self.options.security[:len(self.security)] = self.security
        self.callbacks = []
        self.errors = []
        self.open_names = {}
        self.counter = 0
        self._callbacks()
        result = self.lib.DokanCreateFileSystem(C.byref(self.options), C.byref(self.ops), C.byref(self.handle))
        if result in {-3, -4, -7}:  # driver unavailable, failed to start, or incompatible
            raise W.Problem("project_driver_missing", f"项目命令需要可用的 Dokan 2 驱动（Dokan {result}）。可在调用日志中安装或检查驱动。", 503)
        if result:
            raise W.Problem('project_mount_unavailable', f'未能挂载本轮文件级暂存（Dokan {result}）', 503)
        from .project_transaction import _key
        try:
            self.attachment = dict(path=str(self.target), parentKey=_key(self.target.parent),
                                   key=_key(self.target), volume=os.readlink(self.target))
            self.view.record.setdefault('mounts', []).append(self.attachment)
            self.view.save()
        except BaseException:
            self.lib.DokanCloseHandle(self.handle)
            raise

    def close(self):
        if self.handle:
            self.lib.DokanRemoveMountPoint(str(self.target))
            self.lib.DokanCloseHandle(self.handle)
            self.handle = P()
            detach(self.attachment)
            if self.material:
                self.material.discard()
            self.view.record['mounts'].remove(self.attachment)
            self.view.save()

    def _callbacks(self):
        w, f = T.LPCWSTR, PTR(FileInfo)
        fill_type = C.WINFUNCTYPE(C.c_int, PTR(T.WIN32_FIND_DATAW), f)

        def bind(field, result, *types):
            def register(fn):
                def safe(*args):
                    try:
                        with self.view.lock:
                            fn_result = fn(*args)
                        return (fn_result or 0) if result else None
                    except Exception as error:
                        if not isinstance(error, (FileNotFoundError, FileExistsError, PermissionError)):
                            if len(self.errors) < 8:
                                self.errors.append((field, type(error).__name__, str(error)[:150]))
                        return _status(error) if result else None
                callback = C.WINFUNCTYPE(result, *types)(safe)
                self.callbacks.append(callback)
                setattr(self.ops, field, C.cast(callback, P).value)
                return fn
            return register

        def current(raw, fi):
            return self.open_names.get(fi.contents.context, _name(raw))

        def mapping(name):
            first, slash, tail = name.partition('/')
            if first.casefold() in self.excluded:
                raise FileNotFoundError(name)
            if self.material and first.casefold() == '.harness-check':
                return self.material, 'material' + slash + tail
            return self.view, name

        def writable(name):
            if self.readonly and mapping(name)[0] is self.view:
                raise PermissionError('只读视图')
            self.view._usable(True)

        @bind('create', U32, w, P, U32, U32, U32, U32, U32, f)
        def create(raw, _security, access, _attributes, _share, disposition, options, fi):
            name = _name(raw)
            storage, local = mapping(name)
            if name:
                storage.normalize(local)
            directory = bool(options & 1)
            try:
                info = storage.info(local)
            except FileNotFoundError:
                if disposition in (1, 4):  # FILE_OPEN, FILE_OVERWRITE
                    raise
                writable(name)
                if directory:
                    storage.mkdir(local)
                else:
                    storage.replace(local, b'')
                exists = False
            else:
                exists = True
                is_dir = stat.S_ISDIR(info.st_mode)
                if directory and not is_dir:
                    raise NotADirectoryError(name)
                if options & 0x40 and is_dir:
                    raise IsADirectoryError(name)
                directory = is_dir
                if disposition == 2:
                    raise FileExistsError(name)
                if not is_dir and disposition in (0, 4, 5):
                    writable(name)
                    storage.resize(local, 0)
            if options & 0x1000:
                writable(name)
            self.counter += 1
            fi.contents.context = self.counter
            fi.contents.directory = directory
            self.open_names[self.counter] = name
            # OPEN_IF and OVERWRITE_IF report whether the existing entry was opened.
            if exists and disposition in (3, 5):
                return 0xc0000035

        @bind('cleanup', None, w, f)
        def cleanup(raw, fi):
            if fi.contents.deletePending:
                name = current(raw, fi)
                writable(name)
                storage, local = mapping(name)
                storage.remove(local)

        @bind('close', None, w, f)
        def close(raw, fi):
            self.open_names.pop(fi.contents.context, None)

        @bind('read', U32, w, P, U32, PTR(U32), C.c_int64, f)
        def read(raw, buffer, count, done, offset, fi):
            storage, local = mapping(current(raw, fi))
            data = storage.read(local, offset, count)
            C.memmove(buffer, data, len(data))
            done[0] = len(data)

        @bind('write', U32, w, P, U32, PTR(U32), C.c_int64, f)
        def write(raw, buffer, count, done, offset, fi):
            name = current(raw, fi)
            writable(name)
            storage, local = mapping(name)
            if fi.contents.append or offset == -1:
                offset = storage.info(local).st_size
            done[0] = storage.write(local, offset, C.string_at(buffer, count))

        @bind('flush', U32, w, f)
        def flush(raw, fi):
            self.view._usable()

        @bind('info', U32, w, PTR(ByHandle), f)
        def info(raw, output, fi):
            storage, local = mapping(current(raw, fi))
            s = storage.info(local)
            output[0] = ByHandle(16 if stat.S_ISDIR(s.st_mode) else 128,
                                 _time(s.st_birthtime_ns), _time(s.st_atime_ns), _time(s.st_mtime_ns),
                                 1, s.st_size >> 32, s.st_size & 0xffffffff, 1,
                                 (s.st_ino >> 32) & 0xffffffff, s.st_ino & 0xffffffff)

        @bind('find', U32, w, fill_type, f)
        def find(raw, fill, fi):
            name = current(raw, fi)
            storage, local = mapping(name)
            children = storage.list(local)
            if not name and self.material and 'material' in self.material.list():
                children.append('.harness-check')
            for child in children:
                try:
                    child_store, path = mapping((name + '/' if name else '') + child)
                    s = child_store.info(path)
                except FileNotFoundError:
                    continue
                row = T.WIN32_FIND_DATAW()
                row.dwFileAttributes = 16 if stat.S_ISDIR(s.st_mode) else 128
                row.ftCreationTime, row.ftLastAccessTime, row.ftLastWriteTime = (
                    _time(s.st_birthtime_ns), _time(s.st_atime_ns), _time(s.st_mtime_ns))
                row.nFileSizeLow, row.nFileSizeHigh = s.st_size & 0xffffffff, s.st_size >> 32
                row.cFileName = child
                if fill(C.byref(row), fi):
                    break

        @bind('end', U32, w, C.c_int64, f)
        def end(raw, size, fi):
            name = current(raw, fi)
            writable(name)
            storage, local = mapping(name)
            storage.resize(local, size)

        @bind('allocation', U32, w, C.c_int64, f)
        def allocation(raw, size, fi):
            name = current(raw, fi)
            writable(name)
            storage, local = mapping(name)
            if size < storage.info(local).st_size:
                storage.resize(local, size)

        @bind('delete_file', U32, w, f)
        def delete_file(raw, fi):
            name = current(raw, fi)
            writable(name)
            storage, local = mapping(name)
            if stat.S_ISDIR(storage.info(local).st_mode):
                raise IsADirectoryError(raw)

        @bind('delete_dir', U32, w, f)
        def delete_dir(raw, fi):
            name = current(raw, fi)
            writable(name)
            storage, local = mapping(name)
            if storage.list(local):
                return 0xc0000101

        @bind('move', U32, w, w, T.BOOL, f)
        def move(raw, new_raw, replace, fi):
            old_name, new_name = current(raw, fi), _name(new_raw)
            writable(old_name)
            writable(new_name)
            storage, old = mapping(old_name)
            dest, new = mapping(new_name)
            if storage is not dest:
                return 0xc00000d4
            if old.casefold() == new.casefold():
                return 0 if old == new else 0xc00000bb
            try:
                storage.info(new)
                if not replace:
                    raise FileExistsError(new)
            except FileNotFoundError:
                pass
            info = storage.info(old)
            if stat.S_ISDIR(info.st_mode):
                return 0xc00000bb  # Directory tree moves need a subtree journal.
            if info.st_size > 512 * 1024 * 1024:
                return 0xc000007f
            data = storage.read(old, 0, info.st_size)
            storage.replace(new, data)
            storage.remove(old)
            for context, name in self.open_names.items():
                if name.casefold() == old_name.casefold():
                    self.open_names[context] = new_name

        @bind('get_security', U32, w, PTR(U32), P, U32, PTR(U32), f)
        def security(raw, requested, buffer, size, required, fi):
            required[0] = len(self.security)
            if size < required[0]:
                return 0x80000005
            C.memmove(buffer, self.security, len(self.security))

        @bind('set_security', U32, w, PTR(U32), P, U32, f)
        def set_security(*args):
            raise PermissionError('命令不能更改任务视图授权')

        @bind('space', U32, PTR(U64), PTR(U64), PTR(U64), f)
        def space(available, total, free, fi):
            from .safe_executor import MAX_BYTES
            used = sum(e['size'] + e['beforeSize'] for e in self.view.entries.values())
            total[0] = MAX_BYTES
            free[0] = available[0] = max(0, MAX_BYTES - used)
