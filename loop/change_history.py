"""Immutable observed source versions, independent of Git HEAD and live files.

This store grants no execution or write capability. A prepared record is saved
before an effect; missing final observation remains unknown, never replayed.
"""
# 主要引用方包括 `workbench.py` 和 `code_service.py`。
from contextlib import contextmanager
import difflib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import threading
from . import workbench as W, projects
from .filelock import FileLock

MAX_FILES = 10000
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_OBJECT_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024


class SourceSnapshotCapacity(W.Problem):
    # A source scan exceeded its bound. The history store may still retain exact targets.
    def __init__(self, message):
        super().__init__("history_capacity", message, 409)


def metadata_capacity(db, reserve=0):
    size=db.execute('PRAGMA page_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]
    if size+reserve>MAX_METADATA_BYTES:
        raise W.Problem('history_capacity','修改历史元数据容量已满；保留已有历史，不自动删除',409)


@contextmanager
def database():
    db = None
    try:
        # First readers and recorder can race on a brand-new database. SQLite
        # cannot upgrade journal mode while another initializer holds a read
        # lock, even with busy_timeout. Serialize initialization across owners.
        with FileLock(W.home() / "change-schema.lock"):
            db = sqlite3.connect(W.home() / "change-history.sqlite3", timeout=10)
            if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS objects (hash TEXT PRIMARY KEY, bytes INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS snapshots (hash TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS changes (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, thread TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS changes_thread ON changes(thread,seq)")
            db.execute("CREATE TABLE IF NOT EXISTS baselines (thread TEXT, resource TEXT, snapshot TEXT NOT NULL, PRIMARY KEY(thread,resource))")
            db.execute("CREATE TABLE IF NOT EXISTS run_baselines (thread TEXT, resource TEXT, run TEXT, snapshot TEXT NOT NULL, PRIMARY KEY(thread,resource,run))")
            db.execute("CREATE TABLE IF NOT EXISTS observations (seq INTEGER PRIMARY KEY, change_id TEXT UNIQUE NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS ranges (id TEXT PRIMARY KEY, thread TEXT NOT NULL, data TEXT NOT NULL)")
            from .project_activity import ensure_tables

            ensure_tables(db)
            db.commit()
        yield db
        db.commit()
    except sqlite3.OperationalError as error:
        if "locked" in str(error).lower() or "busy" in str(error).lower():
            raise W.Problem("history_busy", "代码历史正忙，请稍后重新读取；未重放原操作", 409) from error
        raise
    finally:
        if db is not None: db.close()


def version(value):
    return W.digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf8"))


def relative_name(ws,path):
    root=Path(ws['path']).resolve()
    lexical=Path(os.path.abspath(root/str(path)))
    try:return lexical.relative_to(root).as_posix()
    except ValueError:raise W.Problem('path_escape','历史源路径不在所选工作区内',403)


def object_path(digest, *, root=None):
    if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
        raise W.Problem("invalid_version", "源文件版本引用无效")
    root = (W.home() / "change-objects").resolve() if root is None else root
    return W.file_path_from_root(root, digest[:2] + "/" + digest[2:])


def put_object(db, raw, *, root=None):
    digest = W.digest(raw)
    path = object_path(digest, root=root)
    indexed = db.execute("SELECT 1 FROM objects WHERE hash=?", (digest,)).fetchone()
    if not indexed:
        size = db.execute("SELECT COALESCE(SUM(bytes),0) FROM objects").fetchone()[0]
        if size + len(raw) > MAX_OBJECT_BYTES:
            raise W.Problem("history_capacity", "修改历史保留容量已满；未开始新的写入", 409)
    if path.exists():
        if path.is_symlink() or path.stat().st_size != len(raw) or W.digest(path.read_bytes()) != digest:
            raise W.Problem("history_changed", "已保留的源文件版本发生变化", 409)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    db.execute("INSERT OR IGNORE INTO objects VALUES(?,?)", (digest, len(raw)))
    return digest


def filesystem_sources(ws, *, check=None):
    """Bounded no-Git baseline; excluded dependency/output directories are explicit."""
    paths = set()
    for base, dirs, names in os.walk(ws["path"], followlinks=False):
        if check is not None:
            check()
        dirs[:] = [name for name in dirs if name not in W.IGNORE
                   and not (Path(base) / name).is_symlink()
                   and not getattr(Path(base) / name, "is_junction", lambda: False)()]
        for name in names:
            if name == ".git":
                continue
            paths.add((Path(base) / name).relative_to(ws["path"]).as_posix())
            if len(paths) > MAX_FILES:
                raise SourceSnapshotCapacity("源文件数量超过历史保留边界，未取得完整基线")
    return paths


def snapshot(db, ws, extra=(), explicit=False, *, check=None, reference=None, git_optional=False):
    if check is not None:
        check()
    git_source = not explicit and W.git_exe() and (Path(ws["path"]) / ".git").exists()
    head_before = None
    if git_source:
        try:
            head = W.safe_git(ws, ["rev-parse", "HEAD"], check=False)
            git_source = head.returncode == 0
            head_before = head.stdout.decode().strip() if git_source else None
        except (W.Problem, OSError):
            if not git_optional:
                raise
            git_source = False
    # Git-visible source and explicitly targeted ignored files. Generated and
    # ignored command outputs are outside this source snapshot, stated below.
    path_listing = W.safe_git(ws, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"]).stdout if git_source else b''
    try:
        paths = set(path_listing.decode("utf8").split("\0")) - {""}
    except UnicodeDecodeError:
        raise W.Problem("history_encoding", "源文件路径不是 UTF-8，无法建立可核对历史", 409)
    if not explicit and not git_source:
        paths = filesystem_sources(ws, check=check)
    source_paths = set(paths)
    paths.update(relative_name(ws,p) for p in extra if isinstance(p, str) and p)
    if len(paths) > MAX_FILES:
        raise SourceSnapshotCapacity("源文件数量超过历史保留边界，未取得完整基线")
    index_modes={}
    if paths and git_source:
        index_args=['ls-files','--stage','-z']
        for entry in W.safe_git(ws,index_args).stdout.split(b'\0'):
            if not entry:continue
            fields,name=entry.split(b'\t',1)
            mode,_,stage=fields.split()
            if stage==b'0':index_modes[name.decode('utf8')]=mode.decode('ascii')
        if os.name=='nt':
            canonical={name.casefold():name for name in index_modes}
            paths={canonical.get(path.casefold(),path) for path in paths}
    elif os.name == 'nt' and reference:
        # Windows file writes preserve executable/index modes from the retained
        # baseline; they do not perform an index operation or infer a chmod.
        index_modes = {p: meta.get('mode', '100644') for p, meta in reference['files'].items()}
        canonical = {name.casefold(): name for name in index_modes}
        paths = {canonical.get(path.casefold(), path) for path in paths}
    files, total, observed_stats = {}, 0, {}
    from . import baseline
    workspace = Path(ws["path"])
    root = workspace.resolve()
    object_root = (W.home() / "change-objects").resolve()
    protected = baseline.read_snapshot_guard()
    objects = {}

    def stamp(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    for relative in sorted(paths):
        if check is not None:
            check()
        try:
            if index_modes.get(relative)=='120000':
                files[relative]=dict(kind='symlink',hash=None,mode='120000')
                continue
            lexical = root / relative
            path = W.file_path_from_root(root, relative)
            if protected(path):
                files[relative] = dict(kind="protected", hash=None)
                continue
            # Check original lexical path as well as resolved target.
            if lexical.is_symlink():
                files[relative] = dict(kind="symlink", hash=None)
                continue
            relative = path.relative_to(root).as_posix()
            try:
                initial_stat = path.stat()
            except (FileNotFoundError, NotADirectoryError):
                files[relative] = dict(kind="absent", hash=None)
                continue
            if not stat.S_ISREG(initial_stat.st_mode):
                files[relative] = dict(kind="unsupported", hash=None)
                continue
            size = initial_stat.st_size
            if size > W.LIMIT:
                files[relative] = dict(kind="large", hash=None, bytes=size)
                continue
            with path.open("rb") as stream:
                raw = stream.read(W.LIMIT + 1)
            info = path.stat()
            observed_stats[lexical.relative_to(root)] = (path, stamp(info))
            if len(raw) != info.st_size or stamp(initial_stat) != stamp(info):
                raise W.Problem("history_race", "读取历史版本时文件发生变化", 409)
            total += len(raw)
            if total > MAX_SOURCE_BYTES:
                raise SourceSnapshotCapacity(
                    f"本次源文件快照上限为 {MAX_SOURCE_BYTES / (1024 * 1024):g} MiB，"
                    f"已读取 {total} 字节，未取得完整基线"
                )
            kind = "text"
            try:
                if b"\0" in raw: raise UnicodeError()
                raw.decode("utf-8-sig")
            except UnicodeError:
                kind = "binary"
            digest = W.digest(raw)
            objects.setdefault(digest, raw)
            files[relative] = dict(kind=kind, hash=digest, bytes=len(raw),
                bom=raw.startswith(b'\xef\xbb\xbf'), eol="CRLF" if b'\r\n' in raw else "LF",
                mode=index_modes.get(relative,'100644') if os.name=='nt' else '100755' if info.st_mode&0o111 else '100644')
        except W.Problem as error:
            if error.code in {"path_escape", "protected_app_state", "protected_runtime"}:
                files[relative] = dict(kind="protected", hash=None)
            else:
                raise
    if check is not None:
        check()
    paths_after = W.safe_git(ws, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"]).stdout if git_source else b''
    head_after = W.safe_git(ws, ["rev-parse", "HEAD"]).stdout.decode().strip() if git_source else None
    if paths_after != path_listing:
        raise W.Problem("history_race", "核对历史期间源文件集合发生变化", 409)
    if not explicit and not git_source and filesystem_sources(ws, check=check) != source_paths:
        raise W.Problem("history_race", "核对历史期间源文件集合发生变化", 409)
    for relative, (original, observed) in observed_stats.items():
        if check is not None:
            check()
        path = W.file_path_from_root(root, relative)
        try:
            current = stamp(path.stat())
        except (FileNotFoundError, NotADirectoryError):
            current = None
        if path != original or observed != current:
            raise W.Problem("history_race", "保存历史期间源文件发生变化", 409)
    if head_before != head_after or workspace.resolve() != root:
        raise W.Problem("history_race", "保存历史期间工作区发生变化，未将混合版本当作完整快照", 409)
    row = dict(version=1, files=files, workspaceRevision=version(dict(head=head_after,files=files)), head=head_after,
        coverage="explicit-targets" if explicit else "git-visible-source-and-explicit-targets" if git_source else "filesystem-source-excluding-dependency-and-output-directories",
        ignoredFilesIncluded=not explicit and not bool(git_source),
        unsupported=sum(f["kind"] not in {"text", "absent"} for f in files.values()))
    digest = store_snapshot(db, row)
    # Confirm the scan and its bounds before persisting any of its objects.
    for raw in objects.values():
        if check is not None:
            check()
        put_object(db, raw, root=object_root)
    return digest, row


def store_snapshot(db, row):
    encoded = json.dumps(row, ensure_ascii=False)
    if len(encoded.encode('utf8')) > MAX_SNAPSHOT_BYTES:
        raise SourceSnapshotCapacity('源版本清单超过保留边界，未取得完整基线')
    digest = version(row)
    db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?)", (digest, encoded))
    return digest


def load_snapshot(db, digest):
    found = db.execute("SELECT data FROM snapshots WHERE hash=?", (digest,)).fetchone()
    if not found: raise W.Problem("history_missing", "源文件历史快照已缺失", 410)
    value = json.loads(found[0])
    if version(value) != digest: raise W.Problem("history_changed", "源文件历史快照已变化", 409)
    return value


def retain_file_effect(db, before, images):
    old, new = images
    path, original = next(iter(before["files"].items()))
    if original["kind"] != ("absent" if old is None else "text") or original.get("hash") != (
        None if old is None else W.digest(old)
    ):
        raise W.Problem("revision_conflict", "保存前目标文件已变化，重新读取后再编辑", 409)
    info = W._file_text(path, new)
    target = dict(kind="text", hash=put_object(db, new), bytes=len(new), bom=info["bom"], eol=info["eol"],
                  mode=original.get("mode", "100644"))
    files = {path: target}
    return store_snapshot(db, dict(before, files=files, workspaceRevision=version(dict(head=None, files=files))))


def prepare(ident, resource, ws, operation, *, check=None, file_effect=None, targets_only=False):
    from .project_activity import project_id

    activity_project = project_id(projects.conversation(ident))
    with FileLock(W.home() / "change-store.lock", check=check), database() as db:
        metadata_capacity(db,16*1024*1024)
        if db.execute("SELECT 1 FROM changes WHERE id=?", (operation["id"],)).fetchone():
            raise W.Problem("history_exists", "动作历史已存在，不覆盖也不重放", 409)
        target=operation.get("target", {})
        targets=[relative_name(ws,p) for p in [target.get("relative_path"), *target.get("relative_paths", [])] if isinstance(p,str) and p]
        if targets_only and not targets:
            raise ValueError("an explicit before-image requires file targets")
        baseline=db.execute("SELECT snapshot FROM baselines WHERE thread=? AND resource=?",(ident,resource['id'])).fetchone()
        run=operation['source'].get('runId')
        run_base=db.execute('SELECT snapshot FROM run_baselines WHERE thread=? AND resource=? AND run=?',(ident,resource['id'],run)).fetchone()
        initial = None
        branch_only = operation['operation'] == 'branch'
        # A new branch points to the same HEAD. Retain its operation without
        # inventing a file baseline; capture that at the first source write.
        if not targets_only and not branch_only and (not baseline or not run_base):
            try:
                initial, _ = snapshot(db, ws, targets, check=check, git_optional=operation.get('fileOnly', False))
            except W.Problem as error:
                if file_effect is None or not (isinstance(error, SourceSnapshotCapacity) or error.code == "history_race"):
                    raise
                initial = store_snapshot(db, dict(version=1, coverage="baseline-unavailable",
                                                  files={}, reason=error.message))
            db.execute("INSERT OR IGNORE INTO baselines VALUES(?,?,?)", (ident, resource['id'], initial))
            db.execute("INSERT OR IGNORE INTO run_baselines VALUES(?,?,?,?)", (ident, resource['id'], run, initial))
        explicit = bool(targets) or branch_only
        before, planned_after = initial, None
        prior = initial or (run_base or baseline or (None,))[0]
        reference = load_snapshot(db, prior) if targets and prior else None
        if explicit or before is None:
            before, observed = snapshot(db, ws, targets, explicit=explicit, check=check, reference=reference)
            if file_effect is not None:
                planned_after = retain_file_effect(db, observed, file_effect)
        baseline = baseline[0] if baseline else (initial or before)
        value = dict(id=operation["id"], conversationId=ident, resourceId=resource["id"],
            activityProject=activity_project,
            workspaceId=ws["id"], resourcePath=resource["path"], source=operation["source"],
            operation=operation["operation"], started=time.time(), state="prepared",
            branch=operation.get("branch"), head=operation.get("head"),
            capturePaths=targets if explicit else None,
            before=before, baseline=baseline, after=None, files=[],
            notice="尚未保留操作后版本，不能宣称修改已完成；历史不会触发原操作重放。")
        if planned_after is not None:
            value["plannedAfter"] = planned_after
            value["projectBaseline"] = "unavailable" if reference["coverage"] == "baseline-unavailable" else "captured"
            if value["projectBaseline"] == "unavailable":
                value["projectBaselineReason"] = reference["reason"]
        db.execute("INSERT INTO changes(id,thread,data) VALUES(?,?,?)", (value["id"], ident, json.dumps(value, ensure_ascii=False)))
        metadata_capacity(db,16*1024*1024)
    return value["id"]


def get_record(db, ident, cid):
    found = db.execute("SELECT data FROM changes WHERE thread=? AND id=?", (ident, cid)).fetchone()
    if not found: raise W.Problem("history_missing", "修改记录不属于当前会话", 404)
    return json.loads(found[0])


def mark_unknown(ident,cid):
    with FileLock(W.home()/'change-store.lock'),database() as db:
        row=get_record(db,ident,cid)
        if row['state']!='prepared':return
        row.pop("plannedAfter", None)
        row.update(state='unknown',finished=time.time(),notice='进程或历史后版本无法确认；原始前版本保留，不重放操作。')
        row['observationOrder']=db.execute('INSERT INTO observations(change_id) VALUES(?)',(cid,)).lastrowid
        row['revision']=version(row)
        db.execute('UPDATE changes SET data=? WHERE thread=? AND id=?',(json.dumps(row,ensure_ascii=False),ident,cid))


def completion(ident,cid,ws):
    """One final observation after the actual owned process family closes."""
    data_root=W.P.data_root().resolve()
    lock=threading.Lock()
    called=False
    def complete():
        nonlocal called
        with lock:
            if called:return
            called=True
        # A runtime root change must never redirect a background receipt.
        if W.P.data_root().resolve()!=data_root:return
        try:
            finish(ident,cid,ws)
        except Exception:
            try:mark_unknown(ident,cid)
            except Exception:pass
    return complete


def finish(ident, cid, ws, outcome="observed", extra=(), verified=(), *, cancelled_before_effect=False):
    with FileLock(W.home() / "change-store.lock"), database() as db:
        metadata_capacity(db)
        row = get_record(db, ident, cid)
        if row["workspaceId"] != ws["id"] or Path(row["resourcePath"]).resolve() != Path(ws["path"]).resolve():
            raise W.Problem("wrong_source", "修改历史不属于此工作区", 403)
        if row["state"] != "prepared":
            raise W.Problem("history_sealed", "历史修改已封存，不用当前文件覆盖", 409)
        planned_after = row.pop("plannedAfter", None)
        if cancelled_before_effect or (planned_after and outcome != "observed"):
            notice = ("仅保留目标操作前版本，未确认操作后版本。" if planned_after
                      else "操作在产生影响前已取消，保留操作前版本。")
            row.update(state="failed" if cancelled_before_effect else outcome, finished=time.time(), notice=notice)
            row["revision"] = version(row)
            db.execute("UPDATE changes SET data=? WHERE thread=? AND id=?",
                       (json.dumps(row, ensure_ascii=False), ident, cid))
            return row
        paths=row.get('capturePaths')
        old = load_snapshot(db, row["before"])
        key=lambda p:relative_name(ws,p).casefold() if os.name=='nt' else relative_name(ws,p)
        verified_paths={key(v["path"]):v for v in verified}
        if planned_after:
            after = planned_after
            current = load_snapshot(db, after)
            path, target = next(iter(current["files"].items()))
            receipt = verified_paths.get(key(path))
            if not receipt or receipt["before"] != old["files"][path]["hash"] or receipt["after"] != target["hash"]:
                raise W.Problem("history_unconfirmed", "单文件写入回执与已保留的目标版本不一致", 409)
        else:
            after, current = snapshot(db, ws, paths if paths is not None else extra, explicit=paths is not None, reference=old)
        changed = []
        for path in sorted(set(old["files"]) | set(current["files"])):
            a = old["files"].get(path, dict(kind="absent",hash=None))
            b = current["files"].get(path, dict(kind="absent",hash=None))
            if a == b and a["kind"] in {"text", "absent", "binary"}: continue
            expected=verified_paths.get(key(path))
            attributed=bool(expected and a.get("hash")==expected.get("before") and b.get("hash")==expected.get("after"))
            changed.append(dict(path=path, before=a, after=b,
                status="unknown" if a["kind"] not in {"text","binary","absent"} or b["kind"] not in {"text","binary","absent"} else "added" if a["kind"] == "absent" else "deleted" if b["kind"] == "absent" else "modified",
                attribution="verified_file_write" if attributed else "observed_during_action"))
        row.update(after=after, finished=time.time(), state=outcome, files=changed,
            coverage=current["coverage"], unsupported=current["unsupported"],
            notice="保留的是明确写入目标或动作期间观察到的 Git 可见源文件变化。外部编辑并不受应用锁控制；未单独核实的变化不能全部归功于 Agent。Git 忽略文件（除明确目标）不包含在全源快照中。")
        if row.get("projectBaseline") == "unavailable":
            row["notice"] = "仅核对本操作指定文件的前后字节。未取得完整工程基线。"
        row['observationOrder']=db.execute('INSERT INTO observations(change_id) VALUES(?)',(cid,)).lastrowid
        row["revision"] = version(row)
        db.execute("UPDATE changes SET data=? WHERE thread=? AND id=?", (json.dumps(row, ensure_ascii=False), ident, cid))
        from .project_activity import record_change

        record_change(db, row)
        metadata_capacity(db)
    return row


def candidate_baseline(db, ident, resource_id, source_path, runs):
    for run in dict.fromkeys(runs):
        row = db.execute(
            """SELECT snapshot FROM run_baselines WHERE thread=? AND resource=? AND run=?
               AND ?=(SELECT json_extract(data,'$.resourcePath') FROM changes WHERE thread=? AND
                   json_extract(data,'$.resourceId')=? AND json_extract(data,'$.source.runId')=?
                   ORDER BY seq LIMIT 1)""",
            (ident, resource_id, run, source_path, ident, resource_id, run),
        ).fetchone()
        if row:
            return None if load_snapshot(db, row[0])["coverage"] == "baseline-unavailable" else row[0]
    return None


def workspace_matches_write(ident, operation, ws):
    """At a restoration boundary, compare the whole source with retained events.

    File writes no longer carry a guessed whole-project Git revision. Reuse
    the original full baseline and observed after-images, up to this write.
    Any unrecorded external edit remains a project advance requiring review.
    """
    with FileLock(W.home() / "change-store.lock"), database() as db:
        row = get_record(db, ident, operation["id"])
        run = row["source"].get("runId")
        baseline = db.execute("SELECT snapshot FROM run_baselines WHERE thread=? AND resource=? AND run=?",
                              (ident, row["resourceId"], run)).fetchone()
        if not baseline or not row.get("observationOrder"):
            return None
        base = load_snapshot(db, baseline[0])
        if base["coverage"] == "baseline-unavailable":
            return None
        expected = dict(base["files"])
        records = db.execute("""SELECT data FROM changes WHERE thread=?
            AND json_extract(data,'$.resourceId')=? AND json_extract(data,'$.source.runId')=?
            AND json_extract(data,'$.observationOrder')<=? ORDER BY json_extract(data,'$.observationOrder')""",
            (ident, row["resourceId"], run, row["observationOrder"]))
        for (raw,) in records:
            record = json.loads(raw)
            if record["resourcePath"] != ws["path"] or record["state"] != "observed":
                return False
            for file in record["files"]:
                expected[file["path"]] = file["after"]
        try:
            _, current = snapshot(db, ws, git_optional=bool(operation.get("fileOnly") and operation.get("gitContext") is None))
        except SourceSnapshotCapacity:
            return None
        if any(meta.get("hash") is None and meta["kind"] != "absent"
               for files in (expected, current["files"]) for meta in files.values()):
            return None
        def fingerprints(files):
            return {p: (m["kind"], m.get("hash"), m.get("mode"))
                    for p, m in files.items() if m["kind"] != "absent"}
        return fingerprints(expected) == fingerprints(current["files"])


def authorize(ident, row):
    thread = projects.conversation(ident)
    from .subtasks import audit_child
    audit_child(ident, row["resourceId"])
    resource = projects.retained_code(ident, row["resourceId"])
    captured = next((r for r in thread.get("resourceSnapshot", {}).get("resources", []) if r["id"] == row["resourceId"]), None)
    transitioned = projects.switched_code(thread, row["resourceId"])
    if (not resource or resource["path"] != row["resourcePath"]
            or (not transitioned and (not captured or captured.get("path") != row["resourcePath"]))):
        raise W.Problem("resource_denied", "修改历史资料已不在当前读取范围内", 403)
    return resource


def authorized_record(ident, cid):
    with database() as db: row = get_record(db, ident, cid)
    authorize(ident, row)
    if row.get("revision"):
        raw = {k:v for k,v in row.items() if k != "revision"}
        if version(raw) != row["revision"]: raise W.Problem("history_changed", "修改记录已变化", 409)
    return row


def detail(ident, cid, files_offset=0):
    row = authorized_record(ident, cid)
    if type(files_offset) is not int or not 0 <= files_offset <= len(row["files"]):
        raise W.Problem("invalid_cursor", "修改文件列表游标无效")
    # Private absolute resource locations stay inside the store.
    public = {k:v for k,v in row.items() if k not in {"resourcePath", "plannedAfter"}}
    files, totals = summarize_files(row["files"])
    public.update(files=files[files_offset:files_offset+100], fileOffset=files_offset, totals=totals,
        fileCount=len(row["files"]), hasMoreFiles=files_offset+100<len(row["files"]))
    return public


def listing(ident, before=None, action_id=None):
    projects.conversation(ident)
    try: cursor = int(before) if before is not None else 9223372036854775807
    except (ValueError, TypeError): raise W.Problem("invalid_cursor", "修改列表游标无效")
    with database() as db:
        if action_id is not None and (not isinstance(action_id,str) or not 1<=len(action_id)<=256):
            raise W.Problem('invalid_action','动作身份无效')
        rows = db.execute("SELECT seq,data FROM changes WHERE thread=? AND seq<?"+
            (" AND json_extract(data,'$.source.actionId')=?" if action_id is not None else "")+
            " ORDER BY seq DESC LIMIT 31", (ident,cursor,action_id) if action_id is not None else (ident,cursor)).fetchall()
    items=[]
    for seq, raw in rows[:30]:
        row=json.loads(raw)
        public={k:row.get(k) for k in ("id","source","operation","started","state","revision","resourceId","notice")}
        try:
            authorize(ident,row)
            public.update(fileCount=len(row["files"]), files=[dict(index=i,path=f["path"],status=f["status"]) for i,f in enumerate(row["files"][:10])], filesExcerpt=len(row["files"])>10, access="readable")
        except W.Problem as error:
            if error.code != "resource_denied": raise
            public.update(access="denied",files=[],fileCount=None)
        items.append(public)
    return dict(changes=items, hasMore=len(rows)>30, nextBefore=rows[29][0] if len(rows)>30 else None,
        notice="历史源版本独立于当前 Git 差异。执行命令期间观察到的变化仍需核对外部编辑；忽略输出和不支持文件明确标记。")


def raw_content(meta):
    if meta["kind"] == "absent": return None
    if meta["kind"] != "text" or not meta.get("hash"):
        raise W.Problem("history_unsupported", "该版本为二进制、大文件、链接或不可读资料；不以当前文件代替历史", 415)
    path = object_path(meta["hash"])
    if not path.is_file() or path.is_symlink() or path.stat().st_size > W.LIMIT:
        raise W.Problem("history_missing", "源文件版本已缺失或超出边界", 410)
    raw = path.read_bytes()
    if W.digest(raw) != meta["hash"]: raise W.Problem("history_changed", "源文件版本内容已变化", 409)
    return raw


def content(meta):
    return (raw_content(meta) or b'').decode('utf-8-sig')


def diff_inputs(item, side, redact=lambda value: value):
    a = redact(content(item["before"])).replace('\r\n', '\n')
    b = redact(content(item[side])).replace('\r\n', '\n')
    al, bl = a.splitlines(keepends=True), b.splitlines(keepends=True)
    if len(al) + len(bl) > 200000:
        raise W.Problem("diff_capacity", "差异行数过多，请分页读取完整前后文件；未截断为完整差异", 413)
    return a, b, al, bl


def summarize_files(files):
    """Return bounded line counts for retained text versions, never live files."""
    result = []
    additions = deletions = uncounted = size = 0
    for index, item in enumerate(files):
        entry = dict(item)
        a, b = item["before"], item["after"]
        size += (a.get("bytes") or 0) + (b.get("bytes") or 0)
        try:
            if index >= 250 or size > 24 * 1024 * 1024 or item["status"] == "unknown":
                raise W.Problem("diff_unavailable", "未统计差异行数")
            _, _, al, bl = diff_inputs(item, "after")
            added = removed = 0
            for tag, first, last, start, end in difflib.SequenceMatcher(None, al, bl).get_opcodes():
                if tag != "equal":
                    added += end - start
                    removed += last - first
            entry.update(additions=added, deletions=removed)
            additions += added
            deletions += removed
        except W.Problem:
            uncounted += 1
        result.append(entry)
    return result, dict(additions=additions, deletions=deletions, uncounted=uncounted)


def file_page(ident, cid, index, revision, view="diff", after=0, context=3, line=None,hunk=None):
    row = authorized_record(ident, cid)
    return read_file_page(row,index,revision,view,after,context,line,hunk)


def read_file_page(row,index,revision,view='diff',after=0,context=3,line=None,hunk=None,*,redact=lambda value: value):
    if not row.get("revision") or row["revision"] != revision:
        raise W.Problem("history_changed", "请选择已封存的同一修改版本", 409)
    if type(index) is not int or not 0 <= index < len(row["files"]) or type(after) is not int or after < 0:
        raise W.Problem("invalid_cursor", "修改历史文件或游标无效")
    item = row["files"][index]
    if view in {"before", "after", "staged"}:
        if view not in item:raise W.Problem('history_missing','该记录未保留所选代码层，不用当前文件代替',404)
        text = redact(content(item[view]))
        if line is not None:
            if type(line) is not int or not 1<=line<=text.count('\n')+1:raise W.Problem('invalid_cursor','目标代码行超出所选版本范围')
            after=0
            for _ in range(line-1):after=text.find('\n',after)+1
        if after > len(text): raise W.Problem("invalid_cursor", "文件游标超出范围")
        return dict(text=text[after:after+16000], next=min(len(text),after+16000),
            hasMore=after+16000<len(text), revision=revision, complete=True, path=item["path"], view=view,
            lineStart=text.count('\n',0,after)+1,columnStart=after-text.rfind('\n',0,after)-1,totalTextLines=text.count('\n')+1)
    if view not in {"diff","staged-diff"}: raise W.Problem("invalid_view", "未知修改历史视图")
    if view=='staged-diff' and 'staged' not in item:raise W.Problem('history_missing','该记录未保留暂存差异',404)
    # Byte versions and EOL metadata remain exact; code-line counts do not
    # turn every CRLF line into a semantic source edit.
    a, b, al, bl = diff_inputs(item, "staged" if view == "staged-diff" else "after", redact)
    if type(context) is not int or context not in {3,10,100000}:raise W.Problem('invalid_context','请选择 3 行、10 行或全部差异上下文')
    label=json.dumps(item['path'],ensure_ascii=False) if any(ord(c)<32 or ord(c)==127 for c in item['path']) else item['path']
    source_lines = list(difflib.unified_diff(al, bl, fromfile="a/"+label, tofile="b/"+label, n=context))
    additions=sum(line.startswith('+') for line in source_lines[2:])
    deletions=sum(line.startswith('-') for line in source_lines[2:])
    lines=[]
    for line in source_lines:
        lines.append(line if line.endswith('\n') else line+'\n')
        if not line.endswith('\n'):
            lines.append('\\ No newline at end of file\n')
    hunk_offsets=[i for i,value in enumerate(lines) if value.startswith('@@ ')]
    if hunk is not None:
        if type(hunk) is not int or not 0<=hunk<len(hunk_offsets):raise W.Problem('invalid_cursor','改动区块编号无效')
        after=hunk_offsets[hunk]
    if after > len(lines): raise W.Problem("invalid_cursor", "差异游标超出范围")
    old_line=new_line=0;in_hunk=False
    for value in lines[:after]:
        match=re.match(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@',value)
        if match:old_line,new_line=map(int,match.groups());in_hunk=True
        elif in_hunk:
            if value.startswith('-'):old_line+=1
            elif value.startswith('+'):new_line+=1
            elif value.startswith(' '):old_line+=1;new_line+=1
    return dict(text="".join(lines[after:after+300]), next=min(len(lines),after+300),
        hasMore=after+300<len(lines), complete=True, revision=revision, path=item["path"], view=view,
        additions=additions, deletions=deletions, totalLines=len(lines),oldLineStart=old_line,
        newLineStart=new_line,inHunk=in_hunk,oldTextLines=a.count('\n')+1,newTextLines=b.count('\n')+1,
        totalHunks=len(hunk_offsets),currentHunk=max([i for i,offset in enumerate(hunk_offsets) if offset<=after] or [0]))


def search_file(row,index,revision,view,query,after=0,case_sensitive=False):
    if row.get('revision')!=revision:raise W.Problem('history_changed','搜索需要同一保留代码版本',409)
    if type(index) is not int or not 0<=index<len(row['files']) or view not in {'before','after','staged'}:
        raise W.Problem('invalid_view','请选择要搜索的完整文件版本')
    if not isinstance(query,str) or not 1<=len(query)<=256 or type(after) is not int or after<0:
        raise W.Problem('invalid_query','搜索文字或位置无效')
    item=row['files'][index]
    if view not in item:raise W.Problem('history_missing','该记录未保留所选文件层',404)
    text=content(item[view])
    if after>len(text):raise W.Problem('invalid_cursor','搜索位置超出文件范围')
    pattern=re.compile(re.escape(query),0 if case_sensitive else re.IGNORECASE)
    matches=[]
    for match in pattern.finditer(text,after):
        if len(matches)>=21:break
        start=text.rfind('\n',0,match.start())+1
        end=text.find('\n',match.end());end=len(text) if end<0 else end
        matches.append(dict(offset=match.start(),end=match.end(),line=text.count('\n',0,match.start())+1,
            column=match.start()-start,preview=text[start:min(end,start+200)],previewClipped=end-start>200))
    return dict(matches=matches[:20],next=matches[19]['end'] if len(matches)>20 else len(text),
        hasMore=len(matches)>20,complete=True,revision=revision,path=item['path'],view=view)


def route(ident, parts, method, query):
    if method != "GET": raise W.Problem("not_found", "修改历史仅提供只读接口", 404)
    get=lambda key,default=None:(query.get(key) or [default])[0]
    if not parts: return 200,listing(ident,get("before"),get('actionId'))
    if len(parts)==1: return 200,detail(ident,parts[0],int(get("fileOffset","0")))
    if len(parts)==3 and parts[1]=="files":
        return 200,file_page(ident,parts[0],int(parts[2]),get("revision"),get("view","diff"),int(get("after","0")),int(get('context','3')),int(get('line')) if get('line') is not None else None,int(get('hunk')) if get('hunk') is not None else None)
    if len(parts)==4 and parts[1]=='files' and parts[3]=='search':
        return 200,search_file(authorized_record(ident,parts[0]),int(parts[2]),get('revision'),get('view','after'),get('query'),int(get('after','0')),get('case')=='1')
    raise W.Problem("not_found", "修改历史接口不存在", 404)
