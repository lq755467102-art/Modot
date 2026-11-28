"""One execution seam: isolated task copy, no network capability, no host fallback.

Checks never copy generated files back. Host execution is an explicit Full Access
choice; terminal/stdio transports cannot silently opt out of isolation.
"""

# 主要引用方包括 `workbench.py` 和 `tools.py`。

import atexit
from concurrent.futures import ThreadPoolExecutor, wait
import hashlib
from contextlib import ExitStack, nullcontext
import os
import re
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
import time
from . import workbench as W, permissions, sandbox_lifecycle as lifecycle
from .core_runtime import tool_python

EXCLUDED = {".git", ".harness", ".harness-check", ".workbuddy", ".codex", ".ssh", ".aws", ".azure", ".venv", "venv", "__pycache__", "plugin-connections"}
SECRET_NAMES = {"api-token", "credentials.env", "models.json", "grants.jsonl", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
MAX_BYTES = 512 * 1024 * 1024
MAX_FILES = 50000
_cleanup_slots = threading.BoundedSemaphore(2)
_python_lock = threading.Lock()
_python_cache = {}
_python_failures = {}
_runtime_signature_lock = threading.Lock()
_runtime_signatures = {}
_runtime_fingerprint_workers = ThreadPoolExecutor(thread_name_prefix="runtime-fingerprint")


def check_options():
    """Host-selected check runtime options; never populated from tool arguments."""
    utf8 = os.environ.get('HARNESS_CHECK_PYTHON_UTF8', '')
    if utf8 not in {'', '0', '1'}:
        raise W.Problem('sandbox_configuration', 'HARNESS_CHECK_PYTHON_UTF8 只能为 0 或 1', 503)
    options = {'python_utf8': True} if utf8 == '1' else {}
    memory = os.environ.get('HARNESS_CHECK_MEMORY_MIB', '')
    if memory:
        if (not re.fullmatch(r'[0-9]{1,20}', memory)
                or not 1 <= int(memory) <= (2**(8*struct.calcsize('P'))-1)//(1024**2)):
            raise W.Problem('sandbox_configuration', 'HARNESS_CHECK_MEMORY_MIB 须为有效的正整数 MiB', 503)
        options['memory'] = int(memory) * 1024**2
    return options


def _runtime_fingerprint(runtime):
    from .directory_generation import generation
    from .tools import current_stop

    runtime = Path(runtime).resolve()
    key = str(runtime)
    stop = current_stop()
    for _ in range(3):
        before = generation(runtime)
        if before is None:
            # A missing watch cannot certify reuse, even between adjacent calls.
            _require_fingerprint_running(stop)
            fingerprint = _hash_runtime(runtime, stop)
            _require_fingerprint_running(stop)
            return fingerprint
        with _runtime_signature_lock:
            cached = _runtime_signatures.get(key)
            if cached is not None and cached[0] > before:
                # This caller observed an older generation before taking the lock.
                continue
            if cached is None or cached[0] != before:
                job = _runtime_fingerprint_workers.submit(_scan_runtime_generation, runtime, before)
                _runtime_signatures[key] = (before, job)
            else:
                job = cached[1]
            pending = not job.done()

        if pending and stop is not None:
            while not job.done():
                _require_fingerprint_running(stop)
                wait((job,), timeout=.05)
            _require_fingerprint_running(stop)
        fingerprint = job.result()
        # A completed scan was certified before becoming cacheable. Callers
        # that waited may also have missed a change or a watch eviction after that.
        if fingerprint is not None and (not pending or generation(runtime) == before):
            return fingerprint
        _require_fingerprint_running(stop)
    raise W.Problem("sandbox_runtime", "运行时在计算指纹期间持续变化，未执行", 409)


def _require_fingerprint_running(stop):
    if stop is not None and stop():
        raise W.Problem("cancelled", "用户已停止运行时指纹校验", 409)


def _scan_runtime_generation(runtime, generation):
    from .directory_generation import generation as observe

    # The scan belongs to the runtime generation. A caller can stop waiting
    # without aborting a scan needed by another check.
    try:
        fingerprint = _hash_runtime(runtime)
        return fingerprint if observe(runtime) == generation else None
    except BaseException:
        # Share the original failure with current waiters, but allow a retry.
        key = str(runtime)
        with _runtime_signature_lock:
            cached = _runtime_signatures.get(key)
            if cached is not None and cached[0] == generation:
                del _runtime_signatures[key]
        raise


def _hash_runtime(runtime, stop=None):
    rows = []
    for folder, dirs, files in os.walk(runtime):
        _require_fingerprint_running(stop)
        dirs[:] = sorted(name for name in dirs if name != "__pycache__")
        for name in sorted(files):
            path = Path(folder) / name
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise W.Problem("sandbox_runtime", "运行时包含重定向文件", 503)
            with path.open("rb") as stream:
                content = hashlib.file_digest(stream, "sha256").hexdigest()
            rows.append((path.relative_to(runtime).as_posix(), info.st_size,
                         info.st_mtime_ns, info.st_ctime_ns, content))
            if len(rows) > MAX_FILES:
                raise W.Problem("sandbox_capacity", "运行时清单超过容量限制", 413)
    return W.digest(repr(rows).encode())


def _health_python(target):
    code = "import ssl,sqlite3,importlib.util; ssl.create_default_context(); " \
           "exec('import requests' if importlib.util.find_spec('requests') else 'pass'); print('runtime-ok')"
    env = {k:v for k,v in os.environ.items() if k.upper() in {"SYSTEMROOT","WINDIR","COMSPEC","PATH"}}
    env.update(TMPDIR=str(target.parent / "scratch"), TEMP=str(target.parent / "scratch"))
    result = subprocess.run([str(target / "python.exe"), "-I", "-B", "-c", code], env=env,
                            capture_output=True, timeout=20, creationflags=0x08000000 if os.name == "nt" else 0)
    if result.returncode or b"runtime-ok" not in result.stdout:
        error = W.Problem("sandbox_runtime", "隔离运行时健康检查失败：" + result.stderr.decode("utf8", "replace")[-2000:], 503)
        error.deterministic_runtime_health = True
        raise error
    files = [p for p in [target / "Lib/ssl.py", target / "Lib/codex-rejected-cas.pem",
                         target / "Lib/site-packages/certifi/cacert.pem"] if p.is_file()]
    return {str(p.relative_to(target)): W.digest(p.read_bytes()) for p in files}


def command_line(argv):
    if Path(argv[0]).name.lower() in {"cmd", "cmd.exe"}:
        for index, value in enumerate(argv):
            if value.lower() in {"/c","/k"} and index+2==len(argv):
                return subprocess.list2cmdline(argv[:index+1])+' "'+argv[-1]+'"'
    return subprocess.list2cmdline(argv)


def _powershell_workspace(argv):
    if len(argv) < 3 or Path(argv[0]).name.lower() != "powershell.exe":
        return argv
    option, script = argv[-2:]
    startup_flags = {"-nologo", "-noprofile", "-noninteractive"}
    if (option.lower() not in {"-command", "-c"} or script == "-"
            or any(arg.lower() not in startup_flags for arg in argv[1:-2])):
        return argv
    # The process starts in the task copy, but Windows PowerShell's FileSystem
    # provider starts at C:\ and cannot navigate the copy's private ancestors.
    # Mount only the already accessible copy as a session-local PSDrive.
    setup = (
        "try { New-PSDrive -Name HarnessWorkspace -PSProvider FileSystem "
        "-Root ([Environment]::CurrentDirectory) -ErrorAction Stop | Out-Null; "
        "Set-Location -LiteralPath 'HarnessWorkspace:\\' -ErrorAction Stop } "
        "catch { Write-Error $_; exit 1 }"
    )
    return [*argv[:-1], setup + "; & {\n" + script + "\n}"]


def _remove(base, *, parent=None):
    base = Path(base)
    parent = Path(tempfile.gettempdir()).resolve() if parent is None else Path(parent)
    if base.is_symlink() or getattr(base,"is_junction",lambda:False)() or base.resolve().parent != parent or not base.name.startswith("harness-task-"):
        raise W.Problem("sandbox_cleanup", "执行目录身份变化，未递归清理", 503)
    target = str(base.resolve())
    if os.name=='nt' and not target.startswith('\\\\?\\'):
        target='\\\\?\\UNC\\'+target[2:] if target.startswith('\\\\') else '\\\\?\\'+target
    deadline=time.monotonic()+1
    while True:
        try:shutil.rmtree(target);return
        except FileNotFoundError:
            if not base.exists():return
            raise
        except OSError as error:
            if os.name!='nt' or getattr(error,'winerror',None) not in {5,32,145} or time.monotonic()>=deadline:raise
            time.sleep(.05)


def _public_runtime_data(path, source):
    relative = path.relative_to(source).as_posix().casefold()
    if relative not in {"lib/codex-rejected-cas.pem", "lib/site-packages/certifi/cacert.pem"}:
        return False
    if path.stat().st_size > 4 * 1024 * 1024:
        return False
    data = path.read_bytes()
    return (bool(re.search(rb"-----BEGIN (?:TRUSTED )?CERTIFICATE-----", data))
            and not re.search(rb"-----BEGIN (?!(?:TRUSTED )?CERTIFICATE-----)[^-]+-----", data))


def _copy(source, destination, *, runtime=False, trusted=False, public_data=False, incremental=False,
          previous=None, changed=None, excluded=frozenset()):
    count, total = 0, 0
    source = Path(source).resolve()
    if os.name == "nt" and not str(source).startswith("\\\\?\\"):
        value = str(source)
        source = Path("\\\\?\\UNC\\"+value[2:] if value.startswith("\\\\") else "\\\\?\\"+value)
    pending = [(source, destination)]
    retained = set()
    sizes = dict(previous or {})
    # 只有已有可信文件清单时才按变更集同步；通知丢失走完整复制，不能把未知变更当作没有变化。
    selected = changed if previous is not None else None
    selected_keys = {p.casefold() for p in selected} if selected is not None else None
    ancestors = {parent.as_posix().casefold() for p in selected or () for parent in Path(p).parents}
    def affected(name):
        return selected_keys is None or name.casefold() in selected_keys or any(
            parent.as_posix().casefold() in selected_keys for parent in Path(name).parents)
    old_affected = {name for name in sizes if affected(name)}
    for name in old_affected:
        del sizes[name]
    count, total = len(sizes), sum(sizes.values())
    if selected is not None:
        from .protected_copy import remove
        for name in selected:
            if not (source / name).exists() and not (source / name).is_symlink():
                remove(destination, name)
    if incremental and previous is None:
        for old in destination.rglob("*"):
            info = old.lstat()
            if (old.is_symlink() or getattr(old, "is_junction", lambda: False)()
                    or getattr(info,"st_file_attributes",0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or stat.S_ISREG(info.st_mode) and info.st_nlink>1):
                raise W.Problem("cache_changed", "验证区含重定向，未复用", 409)
    from .tools import current_stop
    stop = current_stop()
    while pending:
        if stop and stop():
            raise W.Problem("sandbox_cancelled", "用户已停止任务，未继续创建执行副本", 409)
        folder, target = pending.pop()
        if not folder.resolve().is_relative_to(source):
            raise W.Problem("unsafe_link", "执行副本目录已被重定向，未运行", 403)
        if target.is_file():
            target.unlink()
        target.mkdir(parents=True, exist_ok=True)
        with os.scandir(folder) as entries:
            for item in entries:
                if folder == source and item.name in excluded:
                    continue
                relative = (Path(item.path).relative_to(source)).as_posix()
                if selected is not None and not (affected(relative) or relative.casefold() in ancestors):
                    continue
                name = item.name.casefold()
                is_dir = item.is_dir(follow_symlinks=False)
                if is_dir and (name in EXCLUDED or name.startswith(".env") or runtime and
                        name in {"site-packages","test","tests","idlelib","turtledemo","ensurepip","doc","include","libs","scripts","tcl"}):
                    continue
                info = item.stat(follow_symlinks=False)
                # 复制边界拒绝符号链接和重解析点，避免资料实际来自授权根目录之外。
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise W.Problem("unsafe_link", "执行副本包含链接或目录联接，未运行", 403)
                path = Path(item.path)
                if is_dir:
                    pending.append((path, target/item.name))
                    continue
                if name in EXCLUDED or name in SECRET_NAMES or name.startswith(".env"):
                    continue
                if name.endswith((".pem",".key",".pfx",".p12")) and not (trusted and public_data and _public_runtime_data(path, source)):
                    continue
                if not stat.S_ISREG(info.st_mode) or not trusted and not path.resolve().is_relative_to(source):
                    raise W.Problem("unsafe_file", "执行资料不是普通文件，未运行", 403)
                count, total = count+1, total+info.st_size
                if count > MAX_FILES:
                    raise W.Problem("sandbox_capacity", f"准备执行副本的文件数超过 {MAX_FILES:,} 个", 413)
                if total > MAX_BYTES:
                    raise W.Problem("sandbox_capacity", f"准备执行副本的文件总量超过 {MAX_BYTES / (1024 * 1024):g} MiB", 413)
                output = target / item.name
                if output.is_dir():
                    shutil.rmtree(output)
                if previous is not None or not incremental or not output.is_file() or W.digest(output.read_bytes()) != W.digest(path.read_bytes()):
                    shutil.copyfile(path, output)
                retained.add(output.relative_to(destination).as_posix())
                sizes[relative] = info.st_size
    if previous is not None:
        from .protected_copy import remove
        retained_keys = {p.casefold() for p in retained}
        retained_parents = {parent.as_posix().casefold() for p in retained for parent in Path(p).parents}
        for name in sorted(old_affected - retained, key=lambda p: len(Path(p).parts), reverse=True):
            # A parent may have become a regular file in this update.
            key = name.casefold()
            if key not in retained_keys and key not in retained_parents and not any(
                    parent.as_posix().casefold() in retained_keys for parent in Path(name).parents):
                remove(destination, name)
    elif incremental:
        for output in sorted(destination.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if output.is_file() and output.relative_to(destination).as_posix() not in retained:
                output.unlink()
            elif output.is_dir() and not any(output.iterdir()):
                output.rmdir()
    return sizes

# Windows CPython's 0700 directory ACL grants the owner but omits the
# AppContainer SID. tempfile may retry that denied mkdir until the job expires.
# The task's private scratch already has an inheritable AppContainer ACL.
_PYTHON_TEMP_BOOTSTRAP = """\
import os
import sys

# Load only the staged stdlib, even if the workspace has matching module names.
sys.path.insert(0, os.path.dirname(__file__))
try:
    from pathlib import Path
    import tempfile
finally:
    sys.path.pop(0)

# Windows supplies a profile-specific TEMP/TMP to a fresh AppContainer.
# TMPDIR is the private scratch explicitly supplied by the controller.
scratch = Path(os.environ["TMPDIR"])
private_root = scratch.parent
private_directories = (scratch, private_root / "workspace")
inaccessible_parents = {os.path.normcase(str(p)) for p in private_root.parents}
_is_dir = Path.is_dir
_path_mkdir = Path.mkdir

def sandbox_is_dir(self):
    try:
        return _is_dir(self)
    except PermissionError:
        # Older pytest scans parent directories during collection. An
        # inaccessible host ancestor cannot supply a package or conftest.
        if os.path.normcase(str(self.absolute())) in inaccessible_parents:
            return False
        raise

Path.is_dir = sandbox_is_dir

def private_dir_mode(path, mode):
    if mode == 0o700:
        try:
            parent = Path(os.fsdecode(path)).parent.resolve(strict=True)
        except (OSError, ValueError):
            pass
        else:
            if any(parent.is_relative_to(directory) for directory in private_directories):
                return 0o777
    return mode

def sandbox_path_mkdir(self, mode=0o777, parents=False, exist_ok=False):
    return _path_mkdir(self, private_dir_mode(self, mode), parents, exist_ok)

Path.mkdir = sandbox_path_mkdir

class TemporaryOS:
    def __getattr__(self, name):
        return getattr(os, name)

    def mkdir(self, path, mode=0o777):
        return os.mkdir(path, private_dir_mode(path, mode))

tempfile._os = TemporaryOS()
"""


def _bootstrap_python(target):
    bootstrap = target / "Lib" / "_harness_task_temp.py"
    bootstrap.write_text(_PYTHON_TEMP_BOOTSTRAP, encoding="ascii")
    packages = bootstrap.parent / "site-packages"
    packages.mkdir(exist_ok=True)
    (packages / "_harness_task_temp.pth").write_text(
        "import runpy; runpy.run_path(" + ascii(str(bootstrap)) + ")\n", encoding="ascii")


def _shared_python(runtime, minimal):
    key = (runtime, minimal, _runtime_fingerprint(runtime))
    with _python_lock:
        if key in _python_failures:
            raise W.Problem("sandbox_runtime", _python_failures[key], 503)
        if key in _python_cache:
            cached, health = _python_cache[key]
            if any(not (cached / name).is_file() or W.digest((cached / name).read_bytes()) != digest
                   for name, digest in health.items()):
                del _python_cache[key]
                # Another owned command may still be importing from this immutable copy.
                # Quarantine it until normal process shutdown; rebuild into a new directory.
        if key not in _python_cache:
            base, _ = lifecycle.allocate("runtime")
            target = base / "python"
            try:
                # Expose only the trusted bundled runtime, with no write rights
                # for AppContainers. Workspaces and scratch use per-task SIDs.
                _acl(base, "S-1-15-2-1", "RX")
                _acl(base, "S-1-15-2-2", "RX")
                sizes = _copy(runtime, target, runtime=minimal, trusted=True, public_data=True)
                if _runtime_fingerprint(runtime) != key[2]:
                    raise W.Problem("sandbox_runtime", "运行时准备期间依赖发生变化，未启动命令", 409)
                _bootstrap_python(target)
                health = _health_python(target)
                lifecycle.runtime_ready(base, sizes)
            except BaseException as error:
                if getattr(error, "deterministic_runtime_health", False):
                    _python_failures[key] = str(error.message)
                    while len(_python_failures) > 16:
                        del _python_failures[next(iter(_python_failures))]
                lifecycle.retire_owned(base)
                raise
            atexit.register(lifecycle.retire_owned, base)
            _python_cache[key] = (target, health)
        return _python_cache[key][0]


def _clear_runtime(base, name='runtime'):
    if name not in {'runtime','node-runtime'}:
        raise W.Problem('cache_changed', '无效的运行时目录', 409)
    base=Path(base);target=base/name
    if (base.is_symlink() or getattr(base,'is_junction',lambda:False)()
            or base.resolve().parent != Path(tempfile.gettempdir()).resolve() or not base.name.startswith('harness-task-pool-')
            or target.is_symlink() or getattr(target,'is_junction',lambda:False)() or target.resolve().parent != base.resolve()):
        raise W.Problem('cache_changed','运行时目录身份变化，未递归清理',409)
    path=str(target.resolve())
    if os.name=='nt' and not path.startswith('\\\\?\\'):path='\\\\?\\'+path
    shutil.rmtree(path)


def _runtime(base, argv, minimal=False, *, paths, shared=False, lease=None):
    """Use the bundled runtime or a staged, explicitly configured development runtime."""
    exe = Path(argv[0]).name.lower()
    if exe in {"cmd","cmd.exe","powershell","powershell.exe"}:
        if any("python" in arg.lower() for arg in argv[1:]):
            _runtime(base,["python","-c","pass"], minimal, paths=paths, shared=shared,lease=lease)
        if any(re.search(r'(?i)(?<![\w.-])node(?:\.exe)?(?![\w.-])', arg) for arg in argv[1:]) and shutil.which('node'):
            _runtime(base,['node'], minimal, paths=paths, shared=shared, lease=lease)
    if Path(argv[0]).is_absolute() and Path(argv[0]).is_relative_to(base/"workspace"):
        return argv
    if exe in {"node", "node.exe"}:
        resolved = shutil.which(argv[0])
        if not resolved:
            raise W.Problem("command_not_found", "未找到可供隔离执行的 Node 运行时", 404)
        target = base/"node-runtime"
        preload = Path(__file__).with_name('node_appcontainer.cjs')
        staged = target / preload.name
        if lease is not None:
            spec = dict(executable=resolved, fingerprint=W.digest(Path(resolved).read_bytes()),
                        preload=W.digest(preload.read_bytes()))
            lease['runtimeReused'] = (lease.get('nodeRuntimeSpec') == spec
                                      and (target/'node.exe').is_file() and staged.is_file())
        if lease is None or not lease['runtimeReused']:
            if target.exists():
                _clear_runtime(base, 'node-runtime')
            target.mkdir()
            shutil.copyfile(resolved, target/'node.exe')
            shutil.copyfile(preload, staged)
            if lease is not None:
                lease['nodeRuntimeSpec'] = spec
        paths.append(target)
        return [str(target/"node.exe"),*argv[1:]]
    if exe not in {"python", "python3", "python.exe", "python3.exe", Path(tool_python()).name.lower()}:
        resolved = shutil.which(argv[0])
        if not resolved:
            raise W.Problem("command_not_found", "在此执行环境找不到指定程序", 404)
        # System binaries have OS ACLs; other executables must come from the task copy.
        if not Path(resolved).resolve().is_relative_to(Path(os.environ.get("SystemRoot", "C:/Windows")).resolve()):
            raise W.Problem("unsupported_runtime", "此运行时尚未适配强制隔离，未在宿主执行", 503)
        return [resolved, *argv[1:]]
    app = W.P.app_root()
    candidates = [app/"desktop"/"python", app.parent/"python"]
    runtime = next((p for p in candidates if (p/"python.exe").is_file()), None)
    staged = os.environ.get("HARNESS_SANDBOX_PYTHON_ROOT")
    if runtime is None and staged and (app/"desktop"/"main.js").is_file():
        directory = Path(staged).resolve()
        if (directory/"python.exe").is_file() and (directory/"Lib"/"os.py").is_file():
            runtime = directory
    if runtime is None:
        raise W.Problem("sandbox_runtime", "缺少随包Python运行时，未使用本机解释器替代", 503)
    if lease is not None:
        spec=dict(root=str(runtime),minimal=minimal,fingerprint=_runtime_fingerprint(runtime))
        target=base/'runtime'
        lease['runtimeReused']=lease.get('runtimeSpec') == spec and (target/'python.exe').is_file()
        if not lease['runtimeReused']:
            if target.exists():_clear_runtime(base)
            sizes=_copy(runtime,target,runtime=minimal,trusted=True,public_data=True)
            _bootstrap_python(target)
            _health_python(target)
            if spec['fingerprint'] != _runtime_fingerprint(runtime):
                raise W.Problem('sandbox_runtime','运行时在准备期间变化，未执行',409)
            for file in (target/'Lib/_harness_task_temp.py',target/'Lib/site-packages/_harness_task_temp.pth'):
                sizes[file.relative_to(target).as_posix()]=file.stat().st_size
            lease.update(runtimeSpec=spec,runtimeBytes=sum(sizes.values()))
    elif shared:
        target = _shared_python(runtime, minimal)
    else:
        target = base/"runtime"
        _copy(runtime, target, runtime=minimal, trusted=True, public_data=True)
        _bootstrap_python(target)
    paths.append(target)
    return [str(target/"python.exe"), *argv[1:]]


def _acl(path, sid, access, *, recursive=True):
    from .tools import current_stop
    exe = str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/icacls.exe')
    process = subprocess.Popen([exe,str(path),'/grant:r',f'*{sid}:(OI)(CI){access}',*(['/T'] if recursive else []),'/Q'],
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=0x08000000)
    deadline = time.monotonic()+60
    try:
        while process.poll() is None:
            stop = current_stop()
            if stop and stop():
                raise W.Problem('sandbox_cancelled','用户停止了执行准备',409)
            if time.monotonic()>deadline:
                raise W.Problem('sandbox_acl','副本权限设置超时，未运行',503)
            time.sleep(.05)
        if process.returncode:
            raise W.Problem('sandbox_acl','无法授予任务副本权限，未运行',503)
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)


def _delete_profile(name):
    import ctypes
    from ctypes import wintypes as wt
    api = ctypes.WinDLL("userenv", use_last_error=True)
    api.DeleteAppContainerProfile.argtypes = [wt.LPCWSTR]
    api.DeleteAppContainerProfile.restype = ctypes.c_long
    return api.DeleteAppContainerProfile(name)


class Output:
    def __init__(self, stream, owner):
        self.stream, self.owner, self.used = stream, owner, 0

    def read1(self, size=65536):
        chunk = self.stream.read1(min(max(1,size),32*1024*1024-self.used+1))
        self.used += len(chunk)
        if self.used > 32*1024*1024:
            self.owner.outputLimited = True
            self.owner.kill()
            return b""
        return chunk

    def read(self, size=-1):
        if size >= 0:
            return self.read1(size)
        chunks = []
        while True:
            chunk = self.read1()
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def close(self):
        self.stream.close()


# `sandbox_windows.launch` 返回原进程和进程族句柄，由此对象持有。
# `workspace_lease` 管理验证目录复用，`retained_output` 只记录收集结果，退出与清理分别确认。
class Process:
    def __init__(self, native, family, base, name, out, err, time_limit=120, stdin=None, *, defer_cleanup=False,
                 retain_output=None, workspace_lease=None, project_mount=None):
        self.native, self.family, self.base, self.name = native, family, base, name
        self.pid, self.stdin = native.pid, stdin
        self.stdout, self.stderr = Output(out,self), Output(err,self)
        self.outputLimited = False
        self.returncode = None
        self._lock = threading.RLock()
        self._closed = False
        self._cleanup_done = threading.Event()
        self._defer_cleanup = defer_cleanup
        self.cleanup_error = None
        self.retain_output = retain_output
        self.retained_output = None
        self.workspace_lease = workspace_lease
        self.project_mount = project_mount
        self.started = time.monotonic()
        self.exited = None
        self.cleaned = None
        self.startup_timings = {}
        self._deadline = threading.Timer(time_limit, self.kill)
        self._deadline.daemon = True
        self._deadline.start()
        self.isolation = "appcontainer-project-overlay" if project_mount else "appcontainer-task-copy"

    def poll(self):
        with self._lock:
            if self.returncode is None:
                self.returncode = self.native.poll()
            return self.returncode

    def wait(self, timeout=None):
        code = self.wait_for_exit(timeout)
        self.wait_cleanup()
        return code

    def wait_for_exit(self, timeout=None):
        until = None if timeout is None else time.monotonic()+timeout
        while self.poll() is None:
            if until is not None and time.monotonic() > until:
                raise subprocess.TimeoutExpired("sandbox command", timeout)
            time.sleep(.02)
        # Kill descendants before readers wait for EOF, including successful runs.
        self._finish()
        return self.returncode

    def wait_cleanup(self):
        self._cleanup_done.wait()

    def phase_timings(self):
        timings = dict(self.startup_timings)
        if self.exited is not None:
            timings["command"] = round((self.exited - self.started) * 1000)
        if self.cleaned is not None:
            timings["cleanup"] = round((self.cleaned - self.exited) * 1000)
        return timings

    def _finish(self):
        with self._lock:
            if self._closed:
                return
            self.family.close()
            self._deadline.cancel()
            self._closed = True
            self.native._k_close()
            self.exited = time.monotonic()
            if self.retain_output is not None and (self.returncode == 0 or getattr(self.retain_output, 'collect_on_failure', False)):
                try:
                    result_root = (self.project_mount.material.root if self.project_mount and self.project_mount.material
                                   else self.base / "workspace")
                    self.retained_output = self.retain_output(result_root)
                except Exception as error:
                    self.retained_output = {"error": str(error)[:300]}

            if self.project_mount is not None:
                try:
                    self.project_mount.close()
                except (OSError, ValueError, W.Problem) as error:
                    self.cleanup_error = str(error)
        # 原生进程族和句柄先关闭，再释放或删除执行目录；退出事实与目录清理完成分别对外观察。
        def cleanup():
            try:
                if self.cleanup_error:
                    return
                if self.workspace_lease:
                    from .verification_workspace import release
                    release(self.workspace_lease)
                else:
                    outcome, _ = lifecycle.retire_owned(self.base)
                    if outcome not in {"removed", "missing"}:
                        self.cleanup_error = outcome
            except (OSError, ValueError, W.Problem) as error:
                self.cleanup_error = str(error)
            finally:
                self.cleaned = time.monotonic()
                self._cleanup_done.set()

        if self._defer_cleanup and _cleanup_slots.acquire(blocking=False):
            def background_cleanup():
                try:
                    cleanup()
                finally:
                    _cleanup_slots.release()
            try:
                threading.Thread(target=background_cleanup, daemon=False, name="sandbox-cleanup").start()
                return
            except RuntimeError:
                _cleanup_slots.release()
        cleanup()

    def kill(self):
        with self._lock:
            if not self._closed:
                self.family.close()
                self.returncode = self.native.wait(timeout=5)
                self._finish()

    terminate = kill

    def communicate(self, input=None, timeout=None):
        if input is not None and (self.stdin is None or not isinstance(input,bytes) or len(input)>128*1024):
            raise ValueError("sandbox stdin is closed")
        data = [b"", b""]
        readers = []
        for index, stream in enumerate((self.stdout, self.stderr)):
            if stream is None:
                continue
            def read(i=index, s=stream):
                data[i] = s.read()
            thread = threading.Thread(target=read, daemon=True)
            thread.start()
            readers.append(thread)
        if self.stdin:
            def send():
                try:
                    data=input or b'';offset=0
                    while offset<len(data):
                        written=self.stdin.write(data[offset:])
                        if not written:break
                        offset+=written
                except OSError:pass
                finally:self.stdin.close()
            writer=threading.Thread(target=send,daemon=True)
            writer.start();readers.append(writer)
        self.wait(timeout)
        for thread in readers:
            thread.join(timeout=5)
            if thread.is_alive():
                raise W.Problem("sandbox_output", "隔离进程输出未完成回收", 503)
        for stream in (self.stdout, self.stderr):
            if stream:
                stream.close()
        return tuple(data)


def _prepare_source(root, target, lease, *, source_revision, readonly_source, trusted_source,
                    populate_workspace, source_guard, validate, verify_copy, sid, excluded=frozenset(),
                    conversation_folder=False):
    timings = {}
    stage = time.monotonic()
    was_protected = bool(lease and lease.get('preparedSource'))
    if target.is_symlink() or getattr(target, 'is_junction', lambda: False)():
        raise W.Problem('cache_changed', '验证根目录包含重定向', 409)
    # Only a versioned source protected from the sandbox can skip revalidation.
    # Restored clean leases need fresh integrity proof; writable sources never get this path.
    token = (str(root), source_revision) if readonly_source and source_revision and (trusted_source or source_guard) else None
    from . import protected_copy, directory_changes
    # Writable bookkeeping belongs only to this idle in-process lease. Never
    # persist it as a protected source, or reuse it after failed preparation.
    writable = bool(lease is not None and not readonly_source and not populate_workspace
                    and source_guard)
    writable_state = lease.pop('writableState', None) if lease is not None else None
    state = lease.get('copyState') if lease and was_protected and readonly_source else None
    if writable:
        state = writable_state
    source_kind = 'manifest' if populate_workspace else 'live'
    if state and (state.get('root') != str(root) or state.get('sourceKind') != source_kind
                  or set(state.get('excluded', ())) != excluded):
        state = None
    cursor, changed = (None, None)
    def relevant(paths):
        if paths is None:
            return None
        return {name for name in paths if name not in excluded and not (
            conversation_folder and name.startswith('.modot-record-') and name.endswith('.tmp')
            and name not in (state or {}).get('files', {}) and not os.path.lexists(root / name)
        ) and not any(
            p.casefold() in EXCLUDED or p.casefold() in SECRET_NAMES or p.casefold().startswith('.env')
            for p in Path(name).parts)}
    if (readonly_source or writable) and not populate_workspace:
        cursor, changed = directory_changes.changes(root, state.get('cursor') if state else None)
        changed = relevant(changed)
        if readonly_source and state and lease.pop('restoredSource',False) and lease.get('preparedSource') == token and lease.get('sourceStamp'):
            from .verification_workspace import fingerprint
            if fingerprint(root,source=True) == lease.get('sourceStamp'):
                latest,delta=directory_changes.changes(root,cursor)
                if relevant(delta) == set():cursor,changed=latest,set()
        if cursor is None:
            token = None
    dirty = None
    if writable and state:
        _, dirty = directory_changes.changes(target, state.get('targetCursor'))
        if dirty is None:
            state = None
        else:
            # Check every notified path before removal/copying, including
            # excluded generated files. Source filters must not hide dirt.
            from pathlib import PureWindowsPath
            for name in dirty:
                relative = PureWindowsPath(name)
                path = target / name
                if (relative.is_absolute() or relative.drive or '..' in relative.parts
                        or not path.parent.resolve().is_relative_to(target.resolve())):
                    raise W.Problem('cache_changed', '验证路径已被重定向', 409)
                if path.exists() or path.is_symlink():
                    info = path.lstat()
                    if (path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
                            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                            or stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
                        raise W.Problem('cache_changed', '验证区含重定向，未复用', 409)
    direct = bool(token and state and lease.get("preparedSource") == token
                  and (populate_workspace or changed == set()))
    def stable_source():
        nonlocal cursor
        with W.git_observation.phase('source_version'):
            validate()
        if cursor is not None:
            latest, delta = directory_changes.changes(root, cursor)
            if relevant(delta) != set():
                raise W.Problem('cache_changed', '源码在准备期间变化，请重试检查', 409)
            cursor = latest
    if lease:
        prior = lease.get('preparedSource')
        lease['sourceReuseReason'] = ('same_protected_version' if direct else
            'writable_source' if not readonly_source else
            'source_identity_changed' if prior and token and prior[0] != token[0] else
            'version_changed' if prior and token and prior[1] != token[1] else
            'directory_changed' if prior and token and prior != token else
            'version_or_source_changed' if lease.get('preparedSource') else 'new_or_untrusted_lease')
    # Full rebuild only for a new lease, a lost change journal or a source/mode
    # change. A writable test copy never earns protected-copy bookkeeping.
    if state and not populate_workspace and changed is None:
        state = None
    # 可写副本的测试产物不成为下次源码：先移除副本改动，再合并源变更，避免旧产物遮蔽新代码。
    if writable and state:
        for name in sorted(dirty, key=lambda p: len(Path(p).parts), reverse=True):
            protected_copy.remove(target, name)
        changed = changed | dirty
        lease['sourceReuseReason'] = 'writable_delta'
    if not direct and state is None:
        if lease and lease.get('reused'):
            shutil.rmtree(target)
            target.mkdir()
            _acl(target, sid, 'RX' if readonly_source else 'M', recursive=False)
        state = dict(root=str(root), sourceKind=source_kind, files={}, excluded=sorted(excluded))
    # Stage fixtures live in the sole writable subtree; remove only that subtree.
    if lease and lease.pop('stagedMaterial', False):
        material = target / '.harness-check'
        if material.is_symlink() or getattr(material, 'is_junction', lambda: False)():
            raise W.Problem('cache_changed', '验证输出目录身份变化', 409)
        if material.exists():
            shutil.rmtree(material)
    if direct:
        if not target.is_dir() or target.is_symlink() or getattr(target, "is_junction", lambda: False)():
            raise W.Problem("cache_changed", "只读候选目录身份变化", 409)
        if source_guard:
            with source_guard():
                stable_source()
                state['cursor'] = cursor
    else:
        if lease:
            lease.pop("preparedSource", None)
        with source_guard() if source_guard else nullcontext():
            if source_guard:
                stable_source()
            if populate_workspace:
                context = protected_copy.current.set(state if readonly_source else None)
                try:
                    populate_workspace(target)
                finally:
                    protected_copy.current.reset(context)
            else:
                state['files'] = _copy(root, target, trusted=trusted_source,
                    previous=state['files'] if readonly_source or writable else None,
                    changed=changed if state.get('cursor') else None, excluded=excluded)
                state['cursor'] = cursor
            if source_guard:
                stable_source()
                state['cursor'] = cursor
    timings['sourceSync'] = round((time.monotonic()-stage)*1000)
    stage = time.monotonic()
    verify_copy(target)
    timings['materialStage'] = round((time.monotonic()-stage)*1000)
    stage = time.monotonic()
    if readonly_source:
        if (target / ".harness-check").is_dir():
            _acl(target / ".harness-check", sid, "M")
            if lease:
                lease["stagedMaterial"] = True
        if lease and token:
            lease["preparedSource"] = token
            lease['copyState'] = state
    elif lease:
        lease.pop('copyState', None)
    if writable:
        if (target / '.harness-check').is_dir():
            lease['stagedMaterial'] = True
        target_cursor, _ = directory_changes.changes(target)
        if cursor is not None and target_cursor is not None:
            state['targetCursor'] = target_cursor
            lease['writableState'] = state
    timings['sourcePermissions'] = round((time.monotonic()-stage)*1000)
    stage = time.monotonic()
    if readonly_source and lease and token and not direct:
        sizes = state['files']
        lease['protectedBytes'] = sum(sizes.values())
        lease['protectedFiles'] = len(sizes)
    timings['sourceAccounting'] = round((time.monotonic()-stage)*1000)
    if lease:
        lease['sourceTimings'] = timings
    return direct


def start(argv, *, cwd, env=None, merge=False, validate=lambda:None, memory=1024*1024*1024, processes=32, time_limit=120, minimal_runtime=False, duplex=False, verify_copy=lambda _path:None, dsh_probe=False, defer_cleanup=False, trusted_source=False, retain_output=None, populate_workspace=None, workspace_scope=None, source_guard=None, environment_id=None, readonly_source=False, source_revision=None, python_utf8=False, record_view=None, include_record=False, working_directory='.', project_view=None):
    """Start an actual Windows AppContainer or fail before any user code runs."""
    if os.name != "nt":
        raise W.Problem("sandbox_unavailable", "此平台尚无已验证隔离执行器，未降级执行", 503)
    if not isinstance(argv, list) or not argv or any(not isinstance(x,str) or '\0' in x for x in argv):
        raise W.Problem("invalid_command", "命令必须为argv字符串数组")
    if type(python_utf8) is not bool:
        raise W.Problem('sandbox_configuration', 'Python UTF-8 运行配置须为布尔值', 503)
    python_command = Path(argv[0]).name.lower() in {
        'python', 'python.exe', 'python3', 'python3.exe', Path(tool_python()).name.lower()}
    from . import contain as CN
    from . import netguard as NG
    from .sandbox_windows import launch
    start_at = time.monotonic()
    from . import verification_workspace as pool
    if environment_id is not None and (workspace_scope is None or not defer_cleanup):
        raise W.Problem("invalid_scope", "准备环境只能用于有明确任务范围的隔离检查")
    pooled = workspace_scope is not None and defer_cleanup and (project_view is None or environment_id is not None)
    mount = None
    runtime_preparation = ExitStack()
    try:
        if pooled:
            # Register before checkout can retire an old idle directory.
            runtime_preparation.enter_context(pool.preparing_runtime())
        lease = pool.checkout(tuple(workspace_scope), environment_id) if pooled else None
        if lease:
            base, name = lease["base"], lease["name"]
        else:
            base, name = lifecycle.allocate("sandbox")
    except BaseException:
        runtime_preparation.close()
        raise
    profile = None
    spawned = False
    launched = result = None
    try:
        root = Path(cwd).resolve(strict=True)
        target = base/"workspace"
        profile = lease.get("profile") if lease else None
        profile = profile or CN.ensure_profile(name=name)
        profile = dict(profile, jobName=lifecycle.job_name(name))
        if not profile.get("ok"):
            raise W.Problem("sandbox_profile", "无法建立独立执行身份，未运行", 503)
        sid = CN.sid_str(profile["sid"])
        # These are new, owned directories. Set the inheritable entries before
        # populating them, rather than walking the full tree twice with icacls.
        scratch = base / "scratch"
        if lease and lease["reused"]:
            pool.reset_scratch(lease)
            _acl(scratch, sid, "M")
            if project_view is not None:
                if os.path.lexists(target):
                    if target.is_junction() or target.is_symlink() or any(target.iterdir()):
                        raise W.Problem('cache_changed','上一次的挂载目录未释放',409)
                else:
                    target.mkdir()
                _acl(target, sid, 'RX' if readonly_source else 'M', recursive=False)
        else:
            _acl(base, sid, "RX")
            target.mkdir()
            _acl(target, sid, 'RX' if readonly_source else 'M', recursive=False)
            scratch.mkdir()
            _acl(scratch, sid, "M")
        if lease:
            lease["profile"] = profile
        preparation = base / "environment" if lease and lease.get("environmentId") else None
        if preparation is not None:
            if preparation.exists():
                for path in [preparation, *preparation.rglob("*")]:
                    info = path.lstat()
                    if (path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                            or stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
                        raise W.Problem("cache_changed", "准备环境包含重定向，未复用", 409)
            else:
                preparation.mkdir()
                _acl(preparation, sid, "M")
        command = []
        for arg in argv:
            try:
                p = Path(arg)
                mapped = str(target/p.resolve().relative_to(root)) if p.is_absolute() and p.resolve().is_relative_to(root) else arg
            except (OSError, ValueError):
                mapped = arg
            command.append(mapped)
        runtime_start = time.monotonic()
        runtime_paths = []
        with runtime_preparation:
            command = _runtime(base, command, minimal_runtime, paths=runtime_paths, shared=defer_cleanup,
                               lease=lease if lease and pool.task_scope(lease['scope']) else None)
        if python_utf8 and python_command:
            # This is an interpreter default for the selected check environment;
            # a caller's explicit later -X option keeps normal Python precedence.
            command = [command[0], '-X', 'utf8', *command[1:]]
        copy_start = time.monotonic()
        # Runtime preparation above and command waiting below never hold the
        # source writer gate. Only copy the selected source under that gate.
        with record_view() if record_view else nullcontext((None, None, None)) as (record, _, stage):
            excluded = frozenset(name for name in (stage, None if include_record else record) if name)
            if project_view is None:
                source_reused = _prepare_source(root, target, lease, source_revision=source_revision,
                    readonly_source=readonly_source, trusted_source=trusted_source,
                    populate_workspace=populate_workspace, source_guard=source_guard,
                    validate=validate, verify_copy=verify_copy, sid=sid, excluded=excluded,
                    conversation_folder=record_view is not None)
            else:
                from .project_mount import Mount
                validate()
                if project_view.project != root or populate_workspace:
                    raise W.Problem('unsupported_project_view', '该执行环境不能复用当前项目暂存', 409)
                mount = Mount(project_view, target, sid, readonly_source, scratch=scratch, excluded=excluded)
                verify_copy(target)
                source_reused = False
        copied = time.monotonic()
        if not Path(command[0]).is_relative_to(target):
            command = _powershell_workspace(command)
        # 子进程仅继承明确列出的基础环境；用户目录和临时目录重定向到任务副本，不透传宿主凭据。
        safe = {k:v for k,v in (os.environ if env is None else env).items() if k.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}}
        safe["SystemRoot"] = os.environ.get("SystemRoot", "C:/Windows")
        safe["WINDIR"] = safe["SystemRoot"]
        safe["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
        if mount is None:
            execution_cwd = W.file_path_from_root(target.resolve(), working_directory)
            valid_cwd = execution_cwd.is_dir()
        else:
            relative = '' if working_directory == '.' else project_view.normalize(working_directory)
            execution_cwd = target / relative
            valid_cwd = stat.S_ISDIR(project_view.info(relative).st_mode)
        if not valid_cwd:
            raise W.Problem('invalid_directory', '所选目录不存在于本次隔离副本', 409)
        staged_node = base/'node-runtime' in runtime_paths
        if staged_node:
            # The copy omits links. Resolve modules inside it without walking inaccessible host ancestors.
            preload = (base/'node-runtime'/'node_appcontainer.cjs').as_posix()
            safe['NODE_OPTIONS'] = f'--preserve-symlinks-main --preserve-symlinks --require="{preload}"'
        safe['PYTHONIOENCODING'] = 'utf-8'
        # Redirected Python streams stay UTF-8. Unconfigured checks keep the
        # Windows locale for native child pipes; a host UTF-8 profile is opt-in.
        safe['PYTHONUTF8'] = '1' if python_utf8 and python_command else '0'
        if readonly_source:
            safe['PYTHONDONTWRITEBYTECODE'] = '1'
        # A script starts with its own directory on sys.path. Add the project
        # roots from the copy so it can import sibling modules without host paths.
        safe["PYTHONPATH"] = os.pathsep.join((str(target), str(target/"src")))
        safe.update(TMPDIR=str(scratch), TEMP=str(scratch), TMP=str(scratch), USERPROFILE=str(target), HOME=str(target),
                    LOCALAPPDATA=str(target), APPDATA=str(target),
                    PATH=os.pathsep.join(map(str, [*runtime_paths, Path(os.environ.get("SystemRoot","C:/Windows"))/"System32"])))
        if preparation is not None:
            safe["MODOT_TEST_ENV"] = str(preparation)
        if dsh_probe:
            # This profile is used only by the offline SDK diagnostic, never a
            # model-facing execution path or arbitrary environment passthrough.
            home=base/'dsh-home';home.mkdir();_acl(home,sid,'M')
            cache=base/'n';cache.mkdir();_acl(cache,sid,'M')
            safe.update(DSH_HOME=str(home),NARB_NATIVE_CACHE_DIR=str(cache),
                        DEEPSEEK_API_KEY='owned-offline-probe',DEEPSEEK_BASE_URL='http://127.0.0.1:9')
            if dsh_probe=='no-cache':safe['NARB_DISABLE_NATIVE_CACHE']='1'
        with W.git_observation.phase('before_spawn'):
            validate()
        lifecycle.link_runtime(base, runtime_paths)
        spawned = True  # No retry once process creation has been attempted.
        launched = launch(command, execution_cwd, safe, profile, merge, memory, processes, duplex) if duplex else launch(command, execution_cwd, safe, profile, merge, memory, processes)
        native, family, out, err = launched[:4]
        applied_memory = family.memory_limit()
        if applied_memory != memory:
            raise W.Problem('sandbox_configuration', '隔离进程实际内存额度与宿主配置不一致，未运行', 503)
        child_identity = W.RT.identity(native.pid)
        lifecycle.record_child(base, native.pid, child_identity)
        if lease:
            lease.update(childPid=native.pid, childIdentity=child_identity, lastCommand=argv)
            pool.record(lease)
        result = Process(native, family, base, name, out, err, time_limit, launched[4] if duplex else None,
                         defer_cleanup=defer_cleanup, retain_output=retain_output, workspace_lease=lease, project_mount=mount)
        result.execution_environment = dict(cwd=str(execution_cwd), workingDirectory=working_directory, temporaryDirectory=str(scratch),
            sourceMode=('readonly_overlay' if readonly_source else 'project_overlay') if mount else ('readonly' if readonly_source else 'writable_copy'),
            memoryLimitBytes=applied_memory,
            writableDirectories=[str(scratch), str(target/'.harness-check')] if readonly_source else [str(scratch), str(target)])
        if staged_node:
            result.execution_environment['nodeTestWorkers'] = 'native; older libuv uses a separate process per named test file with inherited output'
        if python_command:
            result.execution_environment['pythonUtf8Default'] = python_utf8
        elif Path(argv[0]).name.lower() == 'powershell.exe':
            # Windows PowerShell writes redirected native output using the
            # Windows console code page, independently of Python's file mode.
            import ctypes
            result.execution_environment['outputEncoding'] = 'cp' + str(ctypes.WinDLL('kernel32').GetOEMCP())
        if not native.resume():
            result.kill()
            raise W.Problem("sandbox_resume", "隔离进程未能启动，未降级执行", 503)
        result.startup_timings = dict(profile=round((runtime_start-start_at)*1000),
                                      runtimeCopy=round((copy_start-runtime_start)*1000),
                                      workspaceCopy=round((copied-copy_start)*1000),
                                      validationAndSpawn=round((time.monotonic()-copied)*1000))
        result.startup_timings.update(sourceDirectReused=source_reused, workspaceReused=bool(lease and lease["reused"]),
                                     runtimeReused=bool(lease and lease.get('runtimeReused')),
                                     sourceMode=result.execution_environment['sourceMode'] if mount else ("protected" if readonly_source else "writable_copy"),
                                     candidateDirect=bool(populate_workspace),
                                     sourceReuseReason=lease.get('sourceReuseReason') if lease else 'unpooled')
        result.startup_timings.update(lease.get('sourceTimings', {}) if lease else {})
        # Reap on process exit independently of output readers (descendant pipes).
        def reap():
            try:
                result.wait()
            except Exception:
                result.kill()
        threading.Thread(target=reap, daemon=True).start()
        def disk_watch():
            while result.poll() is None:
                try:
                    protected = bool(lease and lease.get('preparedSource'))
                    total = lease.get('protectedBytes', 0) if protected else 0
                    count = lease.get('protectedFiles', 0) if protected else 0
                    pending = [scratch, *([preparation] if preparation is not None else [])]
                    if mount is None:
                        pending.append(target / '.harness-check' if protected else target)
                    while pending and result.poll() is None:
                        try:
                            with os.scandir(pending.pop()) as entries:
                                for entry in entries:
                                    try:
                                        info = entry.stat(follow_symlinks=False)
                                    except FileNotFoundError:
                                        continue
                                    if (stat.S_ISDIR(info.st_mode) and not
                                            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                                        pending.append(entry.path)
                                    else:
                                        count += 1
                                        total += info.st_size
                                        if total > MAX_BYTES or count > MAX_FILES:
                                            result.kill()
                                            return
                        except FileNotFoundError:
                            continue
                except OSError:
                    result.kill()
                    return
                time.sleep(.5)
        threading.Thread(target=disk_watch,daemon=True).start()
        return result
    except Exception as error:
        runtime_preparation.close()
        if launched is not None and result is None:
            # Creation is suspended. A failed lease receipt must not strand an
            # unowned process or its executable before Process takes ownership.
            native,family=launched[:2]
            try:
                family.close()
                native.wait(timeout=5)
            finally:
                native._k_close()
                for stream in launched[2:]:
                    if stream is not None:stream.close()
        if mount is not None:
            mount.close()
        if lease:
            pool.discard(lease)
            # 仅自动选取的旧环境、且尚未尝试创建进程时可重建；指定环境或可能已启动的命令不能悄悄重跑。
            if environment_id is None and lease["reused"] and not spawned and (getattr(error, "code", None) == "cache_changed"
                    or isinstance(error, OSError) and getattr(error, "errno", None) != 28):
                result = start(argv, cwd=cwd, env=env, merge=merge, validate=validate, memory=memory,
                    processes=processes, time_limit=time_limit, minimal_runtime=minimal_runtime, duplex=duplex,
                    verify_copy=verify_copy, dsh_probe=dsh_probe, defer_cleanup=defer_cleanup,
                    trusted_source=trusted_source, retain_output=retain_output, populate_workspace=populate_workspace,
                    workspace_scope=workspace_scope, source_guard=source_guard, readonly_source=readonly_source,
                    source_revision=source_revision, python_utf8=python_utf8, record_view=record_view,
                    include_record=include_record, working_directory=working_directory, project_view=project_view)
                result.startup_timings["reuseFallback"] = getattr(error, "code", "reset_failed")
                return result
            raise
        lifecycle.retire_owned(base)
        raise
    finally:
        runtime_preparation.close()


def popen(argv, **kw):
    if permissions.effective_sandbox() == "danger-full-access":
        return _host(argv, **kw)
    if kw.get("shell") or kw.get("stdin") != subprocess.DEVNULL or kw.get("stdout") != subprocess.PIPE or kw.get("stderr") not in {subprocess.PIPE, subprocess.STDOUT} or kw.get("text"):
        raise W.Problem("sandbox_transport", "此交互执行方式尚未支持强制隔离，未在宿主运行", 503)
    return start(argv, cwd=kw.get("cwd") or os.getcwd(), env=kw.get("env"), merge=kw.get("stderr")==subprocess.STDOUT)


def run(argv, *, cwd=None, timeout=120, text=False, encoding="utf8", errors="replace", isolated=False):
    from . import netguard
    env, _ = netguard.for_child()
    p = start(argv,cwd=cwd or os.getcwd(),env=env) if isolated else popen(argv,cwd=cwd or os.getcwd(),env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        out,err = p.communicate(timeout=timeout)
        if text:
            out,err = out.decode(encoding,errors),err.decode(encoding,errors)
        return subprocess.CompletedProcess(argv,p.returncode,out,err)
    except (subprocess.TimeoutExpired,TimeoutError):
        p.kill()
        raise subprocess.TimeoutExpired(argv,timeout)
    finally:
        for stream in (p.stdout,p.stderr):
            if stream:stream.close()


def _host(argv, **kw):
    """Full Access remains revocable and owns the complete process family."""
    if os.name != "nt" or kw.get("shell"):
        raise W.Problem("unsupported_host", "此宿主执行方式尚未验证，未启动", 503)
    from .process_family import Family
    from . import isolate
    from .tools import current_stop
    session_id = permissions.call_session()
    revision = permissions.profile(session_id)["revision"]
    stop = current_stop()
    kw["creationflags"] = kw.get("creationflags",0)|0x00000004|0x08000000
    command = command_line(argv) if isinstance(argv,list) and Path(argv[0]).name.lower() in {"cmd","cmd.exe"} else argv
    p = subprocess.Popen(command,**kw)
    try:
        family = Family(p)
        if permissions.profile()["revision"] != revision or permissions.effective_sandbox() != "danger-full-access":
            family.close()
            raise W.Problem("permission_changed", "启动期间权限变化，未执行", 409)
        if not isolate.resume(p.pid):
            raise OSError("宿主命令未能恢复")
    except Exception:
        p.kill();p.wait(timeout=5)
        raise
    class Host:
        isolation = "host-full-access"
        def __getattr__(self,name):return getattr(p,name)
        def poll(self):return p.poll()
        def wait(self,timeout=None):
            code=p.wait(timeout=timeout)
            with lock:family.close()
            if threading.current_thread() is not monitor_thread:
                monitor_thread.join(timeout=6)
            return code
        def kill(self):
            with lock:family.close()
            p.wait(timeout=5)
        terminate=kill
        def communicate(self,*args,**kwargs):
            result=p.communicate(*args,**kwargs)
            with lock:family.close()
            return result
    lock=threading.RLock()
    owned=Host()
    def monitor():
        try:
            while p.poll() is None:
                if permissions.profile(session_id)["revision"] != revision or (stop and stop()):
                    owned.kill();return
                time.sleep(.1)
            with lock:family.close()
        except Exception:
            owned.kill()
    monitor_thread=threading.Thread(target=monitor,daemon=True)
    monitor_thread.start()
    return owned
