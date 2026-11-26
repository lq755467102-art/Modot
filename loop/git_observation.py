"""Short-lived Git observations, scoped to one operation, never an authorization cache."""
# 主要引用方包括 `workbench.py`。
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import time

_current = ContextVar("git_operation", default=None)


@contextmanager
def operation():
    old = _current.get()
    if old is not None:
        yield old
        return
    value = dict(filters={}, reads={}, states={}, gitProcesses=0, gitSeconds=0.0, observationHits=0)
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)


def current():
    return _current.get()


def summary(value):
    return {k:v for k,v in value.items() if k not in {'filters', 'reads', 'states', 'phase'}}


@contextmanager
def phase(name):
    value = current()
    old = value.get('phase') if value is not None else None
    if value is not None:
        value['phase'] = name
    try:
        yield
    finally:
        if value is not None:
            value['phase'] = old


def workspace_token(ws, *, include_config=True):
    """Only a continuous OS watch can certify reuse within one operation."""
    from .directory_generation import generation
    try:
        observed = identity(ws)
        if observed is None:
            return None
        roots = {observed[0]}
        # Worktrees keep their index/refs outside the source root. Watch those
        # directories too; HEAD/index stamps alone cannot certify unchanged files.
        for entry in observed[1]:
            path = Path(entry[1])
            roots.add(str(path if path.is_dir() else path.parent))
        for name in ('GIT_INDEX_FILE','GIT_WORK_TREE'):
            if os.environ.get(name):
                path=Path(os.environ[name])
                if path.drive.startswith('\\\\'):return None
                path=path if path.is_absolute() else Path(observed[0])/path
                roots.add(str(path if name=='GIT_WORK_TREE' else path.parent))
        tokens = tuple((root, generation(root)) for root in sorted(roots) if Path(root).is_dir())
        environment=hashlib.sha256(repr(sorted((k,v) for k,v in os.environ.items() if k.upper().startswith('GIT_')
            or k.upper() in {'HOME','USERPROFILE','XDG_CONFIG_HOME','PATH','HARNESS_GIT'})).encode()).hexdigest()
        value=current()
        configs=tuple(sorted(config_identity(row[1]) for row in value['filters'].values())) if value and include_config else ()
        return (observed,tokens,environment,configs) if tokens and all(t[1] is not None for t in tokens) else None
    except (OSError, ValueError):
        return None


def identity(ws):
    root = Path(ws["path"]).resolve()
    paths = []
    for parent in (root, *root.parents):
        marker = parent / ".git"
        paths.append(marker)
        if marker.is_file():
            text = marker.read_text(encoding="utf8").strip()
            if not text.startswith("gitdir: "):
                return None
            directory = (parent / text[8:]).resolve()
            break
        if marker.is_dir():
            directory = marker.resolve()
            break
    else:
        return None
    common_file = directory / "commondir"
    common = (directory / common_file.read_text(encoding="utf8").strip()).resolve() if common_file.is_file() else directory
    # Only HEAD/top-level/worktree-membership reads are memoized. Index/status/diff
    # observations are never cached, so an index refresh cannot invalidate config parsing.
    paths += [common_file, directory / "HEAD", common / "packed-refs", common / "config",
              directory / "config.worktree"]
    head = (directory / "HEAD").read_text(encoding="utf8") if (directory / "HEAD").is_file() else ""
    if head.startswith("ref: "):
        reference = head[5:].strip()
        if Path(reference).is_absolute() or ".." in Path(reference).parts:
            return None
        paths += [common / reference]
    stamps = []
    for p in paths:
        observed = stamp(p)
        # Creation/deletion of .git/index.lock changes the directory mtime, not its identity.
        stamps.append(observed[:4] if p.is_dir() else observed)
    return str(root), tuple(stamps)


def stamp(path, *, content=False):
    path = Path(path)
    try:
        resolved = path.resolve()
        info = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if content and path.is_file() and info.st_size <= 2*1024*1024 else None
        if content and path.is_file() and digest is None:
            raise ValueError("config too large for a reusable observation")
        return str(path), str(resolved), info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size, digest
    except FileNotFoundError:
        return str(path), str(path.resolve()), None


def config_identity(paths):
    env = tuple(sorted((k, v) for k, v in os.environ.items()
                       if k.upper().startswith("GIT_") or k.upper() in {"HOME", "USERPROFILE", "XDG_CONFIG_HOME", "PATH"}))
    return hashlib.sha256(repr((env, [stamp(p, content=True) for p in sorted(paths)])).encode()).hexdigest()


def config_paths(raw, exe, ws):
    """Include every observed origin and include target, including inactive/missing includes."""
    home = Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or Path.home())
    prefix = Path(exe).resolve().parent.parent
    paths = {home / ".gitconfig", Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "git/config",
             Path('/etc/gitconfig'), prefix / "etc/gitconfig", prefix / "mingw64/etc/gitconfig",
             Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Git/config"}
    for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        if os.environ.get(key):
            paths.add(Path(os.environ[key]))
    tokens = raw.decode("utf8").split("\0")
    for index in range(0, len(tokens)-1, 2):
        origin, entry = tokens[index:index+2]
        key, _, value = entry.partition("\n")
        if origin.startswith("file:"):
            file = Path(origin[5:])
            if not file.is_absolute():
                file = Path(ws["path"]) / file
            paths.add(file)
        elif origin not in {"command line:", "standard input:"}:
            raise ValueError("unknown config origin")
        if key.lower() == "include.path" or key.lower().startswith("includeif.") and key.lower().endswith(".path"):
            if not origin.startswith("file:") or "%(" in value:
                raise ValueError("cannot establish include dependency")
            include = Path(os.path.expanduser(value))
            paths.add(include if include.is_absolute() else file.parent / include)
        if key.lower() == 'core.excludesfile':
            if '%(' in value:raise ValueError('unresolved excludes path')
            exclude=Path(os.path.expanduser(value))
            paths.add(exclude if exclude.is_absolute() else Path(ws['path'])/exclude)
    return {str(p.absolute()) for p in paths}
