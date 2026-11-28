"""Capability-checked adapters over shared file/Git/process services."""

# 本层把代码动作绑定到会话身份、授权资源和版本回执；实际文件/Git/进程操作使用已有宿主服务。
# 主要引用方包括 `executor_core.py` 和 `workbench.py`。
import base64
import codecs
import os
from pathlib import Path
import re
import stat
import time
import uuid
import json
import sqlite3
import shutil
from collections import Counter, deque
from contextlib import contextmanager, nullcontext
from . import workbench as W, projects, conversation_record
from .evidence_redaction import redact
from .filelock import FileLock, atomic_json
from .policy import Policy


# 供共享宿主函数读取路径和会话的轻量适配。
# `APP` 是应用根，`RUNS` 和 `JOBS` 按当前数据目录解析。
class RuntimeView:
    APP = W.P.app_root()

    @property
    def RUNS(self):
        return W.P.data_root() / "runs"

    @property
    def JOBS(self):
        return self.RUNS / "jobs"

    def _resolve_sid(self, ident):
        path = self.RUNS / ident
        return path if path.is_dir() else None

    def _is_visible(self, directory):
        return not directory.name.startswith("_")


# 总策略由当前代码资源的写入/执行开关汇总；具体动作仍需核对本轮资源快照，证据追问保持只读。
def effective_policy(ident):
    thread = projects.conversation(ident)
    if thread.get("readonlyEvidence"):
        return "readonly"
    return (
        "workspace"
        if any(
            r["kind"] == "code"
            and r["permission"] == "read"
            and (r.get("write") or r.get("execute"))
            for r in projects.scope(ident)["resources"]
        )
        else "readonly"
    )


def selected_write_paths(ident):
    if not W.registry()["threads"].get(ident, {}).get("projectless"):
        return []
    if effective_policy(ident) == "readonly":
        return []
    return [
        r["path"]
        for r in projects.scope(ident)["resources"]
        if r["kind"] == "code"
        and r["permission"] == "read"
        and r.get("write")
    ]


def context_resource(root, resource_id, capability=None, *, register_workspace=True):
    # `root` 绑定启动时的工作区，`resource_id` 来自工具参数。
    # `tools.current_call` 提供实际动作归属，授权以 `projects.py` 的实时范围和本轮快照为准。
    from .tools import current_call

    source = current_call()
    if not source:
        raise W.Problem("identity_required", "代码工具需要实际动作身份", 403)
    thread = projects.conversation(source["sessionId"])
    from .subtasks import validate_child

    child_task = validate_child(thread["id"], resource_id)
    if capability == "write":
        from .permissions import effective_sandbox
        if effective_sandbox() == "read-only":
            raise W.Problem("sandbox_read_only", "当前会话只读，不能写入项目", 403)
    if (
        Path(W.workspace(thread["workspaceId"])["path"]).resolve()
        != Path(root).resolve()
    ):
        raise W.Problem("wrong_source", "代码工具不属于当前会话", 403)
    # 资源路径必须同时存在于当前范围和本轮快照；写入/执行也要求两份记录都已授权。
    # 撤权立即参与本次校验，新增授权不能自行扩大已捕获的本轮范围。
    resource = next((item for item in projects.scope(thread["id"])["resources"] if item["id"] == resource_id), None)
    captured = next(
        (
            r
            for r in thread.get("resourceSnapshot", {}).get("resources", [])
            if r["id"] == resource_id
        ),
        None,
    )
    if (
        not resource
        or not captured
        or resource["kind"] != "code"
        or resource["permission"] != "read"
        or captured.get("path") != resource["path"]
    ):
        raise W.Problem("resource_denied", "代码资源不在本轮授权范围内", 403)
    if capability and (
        thread.get("readonlyEvidence")
        or not resource.get(capability)
        or not captured.get(capability)
    ):
        raise W.Problem(
            "resource_denied",
            "本轮未授权代码" + ("写入" if capability == "write" else "执行"),
            403,
        )
    path = Path(resource["path"])
    if not path.is_dir() or str(path.resolve()) != resource["path"]:
        raise W.Problem("resource_moved", "代码目录已移动或链接目标变化", 409)
    ws = W.register(str(path)) if register_workspace else dict(path=str(path))
    ws = conversation_record.check_workspace(ws, thread, resource_id)
    if child_task:
        ws = dict(ws, isolatedTask=child_task["id"])
    if capability and W.local_writer_root(path) != path.resolve():
        raise W.Problem(
            "repository_root_required",
            "代码写入和运行请授权 Git 工作树根目录，子目录可作为只读资料",
            403,
        )
    return (
        thread,
        resource,
        ws,
        dict(source, actor="agent", taskId=thread["taskId"], threadId=thread["id"]),
    )


# 只有初始化和 WAL 切换需要跨进程建表锁。已初始化的读写各走自己的事务。
@contextmanager
def database():
    connection = None
    try:
        connection = sqlite3.connect(W.home() / "code-operations.sqlite3", timeout=5)
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
        tables = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name IN ('operations','operations_thread')"
        ).fetchone()[0]
        if journal != "wal" or tables != 2:
            with FileLock(W.home() / "code-schema.lock"):
                if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                    connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS operations (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, thread TEXT NOT NULL, data TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS operations_thread ON operations(thread,seq)"
                )
                connection.commit()
        yield connection
        connection.commit()
    except sqlite3.OperationalError as error:
        if "locked" in str(error).lower() or "busy" in str(error).lower():
            raise W.Problem("code_busy", "代码操作记录正忙，请稍后读取；未重放操作", 409) from error
        raise
    finally:
        if connection is not None:
            connection.close()


def record(ident, op):
    with database() as db:
        db.execute(
            "INSERT INTO operations(id,thread,data) VALUES(?,?,?)",
            (op["id"], ident, json.dumps(op)),
        )


def reserve_check(ident, op):
    """Claim the action before acknowledging it or attempting to launch."""
    with database() as db:
        inserted = db.execute(
            "INSERT INTO operations(id,thread,data) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING",
            (op["id"], ident, json.dumps(op)),
        )
        if inserted.rowcount:
            return None
        prior = _read_operation(db, ident, op["id"])
        if prior is None:
            raise W.Problem('check_identity_conflict', '检查启动身份已被占用', 409)
        return prior


def prepare_check(ident, op):
    with database() as db:
        updated = db.execute(
            "UPDATE operations SET data=json_patch(data,json(?)) WHERE thread=? AND id=?"
            " AND json_extract(data,'$.preparationOnly')=1 AND json_extract(data,'$.state')='running'",
            (json.dumps(op), ident, op['id']),
        )
        if not updated.rowcount:
            raise W.Problem("check_identity_conflict", "找不到原检查的准备记录，未执行", 409)


def _read_operation(db, ident, oid):
    row = db.execute(
        "SELECT data FROM operations WHERE thread=? AND id=?", (ident, oid)
    ).fetchone()
    return json.loads(row[0]) if row else None


def operation(ident, oid):
    with database() as db:
        op = _read_operation(db, ident, oid)
    if op is None:
        raise W.Problem("not_found", "当前会话未找到该 operationId；请核对原操作回执的 operationId", 404)
    return op


def reconcile_interrupted_writes(ident, locked_paths):
    """Observe abandoned writes after the caller establishes writer ownership."""
    from . import change_history

    with database() as db:
        pending = db.execute(
            """SELECT data FROM operations WHERE thread=?
               AND json_extract(data,'$.operation') IN ('write','restore')
               AND json_extract(data,'$.state')='running' ORDER BY seq""", (ident,),
        ).fetchall()
    if not pending:
        return []
    workspaces = W.registry()["workspaces"]
    # 仅核对调用方已取得写入归属的工作区；中断结果记 unknown，保留当前文件并等待核对。
    allowed = {Path(path).resolve() for path in locked_paths}
    observations = []
    for (raw,) in pending:
        op = json.loads(raw)
        ws = workspaces.get(op["workspaceId"])
        if not ws or Path(ws["path"]).resolve() not in allowed:
            continue
        plain_file = op.get("fileOnly") and not (Path(ws["path"]) / ".git").exists()
        revision = None if plain_file else W.current_revision(ws)
        if not revision and not plain_file:
            raise W.Problem("write_recovery_unverified", "无法核对中断写入所在工作区的当前版本", 409)
        try:
            change_history.mark_unknown(ident, op["id"])
        except W.Problem as error:
            if error.code != "history_missing":
                raise
        detail = dict(state="unknown", operationId=op["id"], projectRevision=revision,
                      path=op["target"].get("relative_path"), ownerStopped=True,
                      error="旧写入没有完整回执。已核对工作区当前版本，保留文件，不自动重放。")
        if plain_file:
            try:
                detail["observedFileRevision"] = W.file_read(ws, op["target"]["relative_path"])["revision"]
            except (W.Problem, OSError):
                detail["observedFileRevision"] = None
            detail["error"] = "旧写入没有完整回执。保留普通目录的当前文件与原始记录，不自动重放；未声明整仓版本。"
        finish(ident, op["id"], "unknown", detail)
        observations.append(detail)
    return observations


def operations(ident, before=None, limit=30):
    limit = max(1, min(50, int(limit)))
    with database() as db:
        rows = db.execute(
            "SELECT seq,data FROM operations WHERE thread=? AND seq<? ORDER BY seq DESC LIMIT ?",
            (ident, int(before) if before else 9223372036854775807, limit + 1),
        ).fetchall()
    return {
        "operations": [json.loads(row[1]) for row in reversed(rows[:limit])],
        "hasMore": len(rows) > limit,
        "nextBefore": rows[limit - 1][0] if len(rows) > limit else None,
    }


def later_write_exists(op, path):
    with database() as db:
        return db.execute(
            """SELECT 1 FROM operations
               WHERE seq > (SELECT seq FROM operations WHERE id=?)
               AND json_extract(data,'$.workspaceId')=?
               AND json_extract(data,'$.operation') IN ('write','restore')
               AND json_extract(data,'$.state') IN ('completed','unknown')
               AND coalesce(json_extract(data,'$.result.path'),json_extract(data,'$.target.relative_path'))=?
               LIMIT 1""",
            (op["id"], op["workspaceId"], path),
        ).fetchone() is not None


def check_blocker(output):
    if not isinstance(output, str):
        return None
    for kind, pattern in (
        ("missing_certificate", r"could not find a suitable TLS CA certificate bundle"),
        ("missing_fixture", r"fixture ['\"][^'\"]+['\"] not found"),
        ("missing_dependency", r"ModuleNotFoundError"),
        ("unsupported_option", r"unrecognized arguments"),
        ("permission", r"PermissionError|WinError 5"),
        ('database_access', r'OperationalError: (?:unable to open database file|attempt to write a readonly database)'),
        ('missing_check_material', r"can't open file .*\.harness-check.*No such file"),
    ):
        if re.search(pattern, output, re.I):
            return kind
    return None


def check_command_key(argv):
    """Only discard trailing pytest options that change its report formatting."""
    executable = Path(argv[0]).stem.lower()
    python_pytest = executable in {"python", "python3"} and argv[1:3] == ["-m", "pytest"]
    if (executable != "pytest" and not python_pytest) or "--" in argv:
        return argv
    prefix = 3 if python_pytest else 1
    end = len(argv)
    while end > prefix:
        arg = argv[end - 1]
        if re.fullmatch(r"--tb=(?:auto|long|short|line|native|no)|-r[fsxXEpPaAN]+", arg):
            end -= 1
        elif end - 2 >= prefix and (
            argv[end - 2] == "--tb" and arg in {"auto", "long", "short", "line", "native", "no"}
            or argv[end - 2] == "-r" and re.fullmatch(r"[fsxXEpPaAN]+", arg)
        ):
            end -= 2
        else:
            break
    return argv[:end]


def _check_environment(runtime_identity):
    from .core_runtime import tool_python
    from .permissions import effective_sandbox
    from .safe_executor import check_options

    app = W.P.app_root()
    runtimes = (app / "desktop" / "python", app.parent / "python",
                Path(os.environ.get("HARNESS_SANDBOX_PYTHON_ROOT", ".")))
    signatures = []
    for runtime in runtimes:
        try:
            binary = (runtime / "python.exe").stat()
        except OSError:
            continue
        identity = runtime_identity(runtime)
        if identity is None:
            return None
        signatures.append((str(runtime.resolve()), binary.st_size, binary.st_mtime_ns, identity))
        break  # Match the execution seam's first available runtime; do not bind unused alternatives.
    values = [str(app), tool_python(), effective_sandbox(), signatures]
    node = shutil.which('node')
    if node:
        try:
            binary = Path(node).stat()
            values.append(('node', node, binary.st_size, binary.st_mtime_ns,
                           Path(__file__).with_name('node_appcontainer.cjs').stat().st_mtime_ns))
        except OSError:
            return None
    options = check_options()
    if options:
        values.append(options)
    return W.digest(json.dumps(values).encode())


def check_environment():
    from .safe_executor import _runtime_fingerprint

    return _check_environment(_runtime_fingerprint)


def check_environment_observation():
    """Observe runtime changes before preparation without fingerprinting its files."""
    from .directory_generation import generation

    return _check_environment(generation)


def native_check_scope(thread, source, ws, state):
    if (source.get('nativeExecutor') and source.get('logicalTaskId')
            and source.get('role') == 'executor' and not ws.get('isolatedTask')):
        return (thread['id'],'task:'+source['logicalTaskId'],str(Path(ws['path']).resolve()),
                state.get('branch'),state.get('gitContext') or W.file_workspace_state(ws).get('gitContext'))


def review_check_environments(root, resource_id, environment_id=None, *, remove=False):
    thread, _, ws, source = context_resource(root, resource_id, 'execute')
    scope = native_check_scope(thread, source, ws, W.file_workspace_state(ws))
    if not scope:
        raise W.Problem('invalid_scope', '准备环境仅属于当前 L0 任务', 409)
    from .verification_workspace import environments
    return environments(scope, environment_id, remove=remove)


def candidate_receipts(ident, resource, workspace, runs, revision, *, role="executor"):
    from .evidence_redaction import redact

    with database() as db:
        rows = db.execute(
            """SELECT data FROM operations WHERE thread=?
               AND json_extract(data,'$.operation')='check'
               AND json_extract(data,'$.source.role')=?
               AND json_extract(data,'$.source.runId') IN (?,?)
               AND json_extract(data,'$.resourceId')=?
               AND json_extract(data,'$.workspaceId')=?
               ORDER BY (json_extract(data,'$.beforeRevision')=?) DESC, seq DESC LIMIT 12""",
            (ident, role, *runs, resource, workspace, revision),
        ).fetchall()
    receipts = []
    for (raw,) in rows:
        op = json.loads(raw)
        result = op.get("result") or {}
        verification = result.get("verification") or {}
        argv = op["target"]["argv"]
        # Give a test selector when it is a script or module name. Inline
        # programs and arbitrary flag values can contain private material.
        selector = argv[2] if len(argv) >= 3 and argv[1] == "-m" else argv[1] if len(argv) >= 2 else ""
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,160}", selector):
            selector = ""
        selectors = []
        if argv[1:3] == ["-m", "pytest"]:
            for index, part in enumerate(argv[3:], 3):
                if re.fullmatch(r"[\w./-]+\.py(?:::[\w.\[\]-]+)*", part):
                    selectors.append(part)
                elif argv[index - 1] in {"-k", "-m"} and re.fullmatch(r"[\w .()!-]{1,160}", part):
                    selectors.append(argv[index - 1] + " " + part)
        receipts.append(dict(actionId=op["source"].get("actionId"), runId=op["source"]["runId"], operationId=op["id"],
                             revision=op["beforeRevision"], matchesCandidate=op["beforeRevision"] == revision,
                             command=Path(argv[0]).name, selector=selector,
                             status=verification.get("status", "unknown" if op["state"] == "running" else op["state"]),
                             exitCode=(result.get("process") or {}).get("exitCode"),
                             environmentBlocker=result.get("environmentBlocker") or check_blocker(result.get("text")),
                             testSelection=redact([part[:180] for part in selectors[:10]])))
    return receipts


def check_label(argv, *, script=None, scope_reason=None):
    if scope_reason and scope_reason.strip():
        return re.sub(r'\s+', ' ', redact(scope_reason)).strip()[:160]
    if isinstance(script, str):
        lines = [line.strip() for line in script.splitlines() if line.strip()]
        assertion = next((line for line in lines if line.startswith('assert ')), None)
        title = assertion or next((line.lstrip('# ').strip() for line in lines if line.startswith('#') and not line.startswith('#!')), None)
        title = title or '临时Python脚本'
        return redact(f'{title[:130]} [脚本{W.digest(script.encode())[:8]}]')
    argv = argv or []
    if '-c' in argv and Path(argv[0]).name.lower().startswith('python'):
        index=argv.index('-c')
        if index+1<len(argv):return check_label(argv, script=argv[index+1])
    return ' '.join(str(x) for x in argv[:4] if re.fullmatch(r'[\w./\\:-]{1,180}',str(x))) or '检查命令'


# 同条件回执的身份包含环境、脚本/材料、输出保留、用途和写回选项，不能只按命令名去重。
def check_key(target):
    values=[target.get(k) for k in ('argv','environment','material','scriptHash','retain_output')]
    if target.get('directory', '.') != '.':
        values.append({'directory': target['directory']})
    if target.get('include_conversation_record'):
        values.append('conversation-record')
    if target.get('environment_id') is not None:values.append(target['environment_id'])
    return W.digest(json.dumps([*values,target.get('purpose','acceptance'),target.get('writable_copy',False),
                                target.get('capture_changes',False)], sort_keys=True).encode())


def check_directory(thread, resource_id, workspace, args):
    # Host-staged checks address .harness-check relative to the resource root.
    staged = args.get('capture_changes') or any(key in args for key in
        ('script', 'script_operation_id', 'scenario', 'material', 'retain_output'))
    value = args.get('directory', '.')
    if 'directory' not in args and not staged:
        selected = projects.selected_import_location(thread, thread.get('resourceSnapshot') or {})
        if selected and selected['resourceId'] == resource_id:
            value = selected['directory']
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise W.Problem('invalid_directory', '请使用授权资源内的相对目录')
    if value == '.':
        return value
    root = Path(workspace['path']).resolve(strict=True)
    directory = W.file_path_from_root(root, value)
    if not directory.is_dir() or staged and directory != root:
        raise W.Problem('invalid_directory', '目录不存在，或此检查使用资源根的暂存材料')
    return directory.relative_to(root).as_posix()


def includes_record(args):
    pinned = W.record_revision(args.get('expected_revision'))
    include = args.get('include_conversation_record', pinned)
    if type(include) is not bool or pinned and not include:
        raise W.Problem('invalid_arguments', '记录版本需要包含会话记录；include_conversation_record 使用布尔值')
    return include


def review_check_receipt(root, resource_id, args, *, prior_operations=()):
    """Look up current P1 evidence without preparing or starting a workroom."""
    from . import check_material

    opts = {'argv', 'script', 'scenario', 'parameters', 'material', 'retain_output', 'environment_id',
            'expected_revision', 'scope_reason', 'writable_copy', 'capture_changes', 'purpose',
            'timeout', 'yield_time_ms', 'include_conversation_record', 'directory'}
    # Named preparation directories can be changed by other checks with the same ID.
    if (args.keys() - opts or args.get('capture_changes', False) is not False
            or args.get('environment_id') is not None or args.get('purpose', 'acceptance') != 'acceptance'):
        return None
    if (type(args.get('writable_copy', False)) is not bool
            or type(args.get('timeout', 30)) is not int or not 1 <= args.get('timeout', 30) <= 120
            or type(args.get('yield_time_ms', 0)) is not int or not 0 <= args.get('yield_time_ms', 0) <= 30000
            or not isinstance(args.get('scope_reason', ''), str) or len(args.get('scope_reason', '')) > 320):
        return None
    thread, _, ws, source = context_resource(root, resource_id, 'execute')
    if not source.get('nativeExecutor') or not source.get('logicalTaskId') or source.get('role') != 'executor':
        return None
    state = W.check_source_state(ws)
    revision = W.selected_check_revision(state, includes_record(args))
    if (not revision or not (state.get('head') or state.get('folderObservation')) or state.get('protected') or state['branch'] == '(detached)'
            or args.get('expected_revision') not in (None, revision)):
        return None

    scenario = None
    if 'scenario' in args:
        if {'argv', 'script', 'material'} & args.keys() or not native_check_scope(thread, source, ws, state):
            return None
        from . import test_workroom
        script, scenario = test_workroom.resolve(thread['id'], source['logicalTaskId'],
                                                args['scenario'], args.get('parameters'))
        args = dict(args, script=script)
    elif 'parameters' in args:
        return None
    argv = args.get('argv')
    if 'script' in args:
        if 'argv' in args and argv != ['python', '.harness-check/input.py']:
            return None
        argv = ['python', '.harness-check/input.py']
    if (not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv)
            or not argv[0].strip() or argv[0].lstrip().startswith('-') or check_changes_stash(argv)):
        return None
    material, output = check_material.prepare(thread['id'], args)
    target = dict(argv=argv, directory=check_directory(thread, resource_id, ws, args),
                  environment=check_environment(), material=args.get('material'), retain_output=output,
                  writable_copy=args.get('writable_copy', False))
    if W.record_revision(revision):
        target['include_conversation_record'] = True
    if 'script' in args:
        target['scriptHash'] = W.digest(material)
    key = check_key(target)
    run_match = "json_extract(data,'$.source.runId')=?"
    if prior_operations:
        # On recovery only consult operations already registered by this review.
        placeholders = ','.join('?' for _ in prior_operations)
        run_match = f"({run_match} OR id IN ({placeholders}))"
    with database() as db:
        rows = db.execute(
            f"""SELECT data FROM operations WHERE thread=?
               AND json_extract(data,'$.operation')='check'
               AND json_extract(data,'$.resourceId')=?
               AND json_extract(data,'$.workspaceId')=?
               AND json_extract(data,'$.beforeRevision')=?
               AND {run_match}
               AND json_extract(data,'$.source.logicalTaskId')=?
               AND json_extract(data,'$.source.demandRevision')=?
               AND json_extract(data,'$.source.role')='executor'
               AND json_extract(data,'$.source.nativeExecutor')=1
               ORDER BY seq DESC""",
            (thread['id'], resource_id, ws['id'], revision, source['runId'], *prior_operations, source['logicalTaskId'],
             source.get('demandRevision')),
        ).fetchall()
    for (raw,) in rows:
        op = json.loads(raw)
        if check_key(op['target']) != key or op['target'].get('scenario') != scenario:
            continue
        if op['state'] == 'running' and op.get('backgroundCheck'):
            return op, None
        result = _review_passed_result(thread['id'], op, revision)
        if result is None:
            return None
        return op, dict(check_tool_result(result), ok=True, reused=True, executedThisCall=False,
                        operationId=op['id'], evidenceActionId=op['source']['actionId'],
                        summary='复用同版本、同条件的已归档通过回执。覆盖以原回执为准。')
    return None


def _review_passed_result(ident, op, revision):
    """Use the same affirmative proof for the review context and actual reuse."""
    if op['target'].get('environment_id') is not None:
        return None
    result = op.get('result') or {}
    execution = result.get('execution') or {}
    diagnostics = result.get('diagnostics') or {}
    verdict = result.get('verification') or {}
    report = diagnostics.get('testReport') or {}
    counts = (result.get('assessment') or {}).get('testCounts') or {}
    cases = report.get('cases', [])
    if (op['state'] != 'completed' or verdict.get('status') != 'passed' or verdict.get('revision') != revision
            or execution.get('executed') is not True or execution.get('state') != 'completed'
            or execution.get('exitCode') != 0 or execution.get('sourceRevision') != revision
            or execution.get('sourceRevisionAfter') != revision or execution.get('timedOut') or execution.get('cancelled')
            or result.get('environmentBlocker') or diagnostics.get('environmentBlockers')
            or report.get('state') != 'passed' or counts.get('skipped') or not (
                counts.get('passed') or any(case.get('state') == 'passed' for case in cases))
            or any(case.get('state') != 'passed' for case in cases)):
        return None
    if op['target'].get('retain_output'):
        from . import artifacts
        retained = (result.get('process') or {}).get('retainedOutput') or {}
        if not retained.get('artifactId') or not retained.get('versionId'):
            return None
        try:
            saved = artifacts.read_version(ident, retained['artifactId'], retained['versionId'])
        except (W.Problem, OSError, ValueError):
            return None
        if saved['version']['hash'] != retained.get('hash'):
            return None
    return result


def _assertion_history(ops, workspace_id, state, environment):
    """Keep each observed assertion until its own result is observed again."""
    # `ops` 是数据库按新到旧读出的代码操作。
    # 用同任务、需求修订、检查材料和测试选择器归组，普通命令成功不能冲掉另一条旧断言。
    current_cases, earlier_failures = {}, {}
    if state['revision'] is None:
        return {}, [], []
    for op in ops:
        if op['operation']!='check' or op['workspaceId']!=workspace_id or op['state']!='completed':
            continue
        source, target = op['source'], op['target']
        if (not source.get('nativeExecutor') or target.get('purpose','acceptance')!='acceptance'
                or target.get('environment')!=environment or target.get('capture_changes')):
            continue
        result = op.get('result') or {}
        execution = result.get('execution') or {}
        before = op['beforeRevision']
        if (not execution.get('executed') or execution.get('state') not in {'completed','failed'}
                or execution.get('sourceRevisionAfter') != before or result.get('environmentBlocker')
                or result.get('verification',{}).get('status') not in {'passed','failed'}):
            continue
        diagnostics = result.get('diagnostics') or {}
        if diagnostics.get('environmentBlockers'):
            continue
        report = diagnostics.get('testReport') or {}
        group = check_key(target)
        for case in report.get('cases', []):
            key = (group, source.get('logicalTaskId'), source.get('demandRevision'), case['framework'], case['selector'])
            if before == W.selected_check_revision(state, W.record_revision(before)):
                current_cases.setdefault(key, (op,case))
            elif case['state']=='failed':
                earlier_failures.setdefault(key, (op,case))

    active, repairs, pending = {}, [], []
    for op, case in current_cases.values():
        if case['state']=='failed':
            active.setdefault(op['id'], []).append(case)
    for key, (red,case) in earlier_failures.items():
        current = W.selected_check_revision(state, W.record_revision(red['beforeRevision']))
        observed = current_cases.get(key)
        if observed and observed[1]['state']=='failed':
            continue
        script = red['target'].get('scriptHash')
        # Same project-file selector across source revisions does not prove unchanged test material.
        comparable = script and (case['framework']=='workroom' or
                    case['framework']=='unittest' and case['selector'].startswith('__main__.'))
        if observed and comparable:
            green,_ = observed
            repairs.append(dict(selector=case['selector'],framework=case['framework'],scriptHash=script,
                before=dict(operationId=red['id'],revision=red['beforeRevision']),
                after=dict(operationId=green['id'],revision=current)))
        else:
            pending.append(dict(kind='assertion', operationId=red['id'], actionId=red['source'].get('actionId'),
                revision=red['beforeRevision'], currentRevision=current, status='stale',
                text=case['selector']+' 缺当前版本的同一断言证据'))
    return active, repairs, pending


def executor_verification(ident, run_id, *, task=None, prior_run_ids=(), include_review_receipts=False):
    """Summarize actual code checks without declaring user requirements accepted."""
    # `run_id` 选取本轮执行记录。
    # 原生 L0 从 `loop.py` 额外传入逻辑任务和需求修订，避免更正需求后沿用此前检查。
    run_ids = (run_id, *prior_run_ids)
    parameters = ','.join('?' for _ in run_ids)
    with database() as db:
        records = db.execute(
            f"""SELECT data FROM operations WHERE thread=?
               AND json_extract(data,'$.source.runId') IN ({parameters})
               AND coalesce(json_extract(data,'$.source.role'),'executor')='executor'
               ORDER BY seq DESC""", (ident, *run_ids),
        ).fetchall()
    relevant = {}
    by_id = {}
    for (raw,) in records:
        op = json.loads(raw)
        if op.get('preparationOnly'):
            continue  # No observed checkout or command yet; use the lifecycle receipt.
        source = op['source']
        if source['runId'] != run_id and (not task or not source.get('nativeExecutor')
                or source.get('logicalTaskId') != task['id'] or source.get('demandRevision') != task['revision']):
            continue
        if task and source.get('nativeExecutor') and source.get('logicalTaskId') and (
                source['logicalTaskId']!=task.get('id') or source.get('demandRevision')!=task.get('revision')):
            continue
        by_id[op["id"]] = op
        if op["operation"] in {"write", "restore", "check"}:
            relevant.setdefault(op["resourceId"], []).append(op)
    if not relevant:
        return None
    paths = {r["id"]: r["path"] for r in projects.scope(ident)["resources"]
             if r["kind"] == "code" and r["permission"] == "read"}
    resources = []
    current_environment = check_environment()
    for resource_id, ops in relevant.items():
        workspace_id = ops[0]["workspaceId"]
        ws = W.registry()["workspaces"].get(workspace_id)
        state = {'revision': None}
        if ws and ws["path"] == paths.get(resource_id):
            try:
                has_checks = any(op["operation"] == "check" and op["workspaceId"] == workspace_id for op in ops)
                if has_checks:
                    ws = conversation_record.check_workspace(ws, projects.conversation(ident), resource_id)
                state = W.check_source_state(ws, fingerprint_folder=has_checks)
            except (W.Problem, OSError):
                pass
        checks = []
        active_assertions, repairs, pending = _assertion_history(ops, workspace_id, state, current_environment)
        passed_revisions = set()
        seen = set()
        # 记录按新到旧读取，每组条件只取最近一次；旧源码、旧环境或缺失的保留输出不能算当前通过。
        for op in ops:
            if op["operation"] != "check" or op["workspaceId"] != workspace_id:
                continue
            target = op["target"]
            current = W.selected_check_revision(state, W.record_revision(op['beforeRevision']))
            key = check_key(target)
            active = active_assertions.get(op['id'])
            if key in seen and not active:
                continue
            seen.add(key)
            if current != op['beforeRevision']:
                if target.get('purpose', 'acceptance') != 'exploration':
                    pending.append(dict(kind='recheck', operationId=op['id'], actionId=op['source'].get('actionId'), revision=op['beforeRevision'],
                        currentRevision=current, status='stale', argv=target.get('argv'),
                        text='原检查 '+check_label(target.get('argv'))+'：尚缺当前版本复验'))
                continue
            result = op.get("result") or {}
            status = result.get("verification", {}).get("status", "unknown")
            if op.get('backgroundCheck') and op['state'] == 'running':
                status = 'running'
            if target.get("environment") is not None and target["environment"] != current_environment:
                status = "stale_environment"
            if status == "passed" and target.get("retain_output") and not result.get("process", {}).get(
                "retainedOutput", {}
            ).get("versionId"):
                status = "output_unavailable"
            replacement = op.get("entryReplacement", {})
            substitute = by_id.get(replacement.get("operationId"), {})
            replaced = (replacement.get("revision") == current
                and substitute.get("resourceId") == resource_id and substitute.get("beforeRevision") == current
                and substitute.get("result", {}).get("verification", {}).get("status") == "passed")
            if active:
                status, replaced = 'failed', False
            row = dict(operationId=op["id"], actionId=op["source"].get("actionId"), status=status,
                               purpose=target.get("purpose", "acceptance"), replaced=bool(replaced),
                               assessment=result.get('assessment'),
                               argv=target.get("argv"), environmentBlocker=result.get("environmentBlocker")
                               or check_blocker(result.get("text")))
            if (include_review_receipts and status == 'passed' and target.get('purpose', 'acceptance') == 'acceptance'
                    and op['source'].get('nativeExecutor') and target.get('environment') == current_environment
                    and _review_passed_result(ident, op, current) is not None):
                row['reviewReusable'] = True
            if active:
                row['assertionFailures'] = active
            if result.get('execution'):
                row['exitCode'] = result['execution'].get('exitCode')
                row['processId'] = result['execution'].get('processId')
                row['testReportState'] = ((result.get('diagnostics') or {}).get('testReport') or {}).get('state')
            checks.append(row)
            if status == 'passed' and row['purpose'] != 'exploration' and not replaced:
                passed_revisions.add(current)
            if status != 'passed' and target.get('purpose','acceptance') != 'exploration' and not replaced:
                pending.append(dict(kind='check', operationId=op['id'], actionId=op['source'].get('actionId'), status=status, revision=current,
                    argv=target.get('argv'), text=check_label(target.get('argv'))+'：'+status,
                    diagnostics=result.get('diagnostics') or {}))
        states = {check["status"] for check in checks if check["purpose"] != "exploration" and not check["replaced"]}
        # Old commands are context for the next check, not newly invented
        # requirements. Existing explicit acceptance/defect ledgers own coverage.
        pending = [gap for gap in pending if gap['kind'] != 'recheck' or gap['currentRevision'] not in passed_revisions]
        current = state['revision']
        # checked 只汇总已有检查，不代表需求验收；尚未确认的写入/恢复仍使资源状态不确定。
        unresolved = any(op["state"] in {"running", "unknown"}
            and not (op.get('backgroundCheck') and op['operation']=='check') for op in ops)
        status = ("unknown" if current is None or unresolved else
                  "incomplete" if states - {"passed"} or pending else "checked" if states else "unverified")
        row = dict(resourceId=resource_id, revision=current, status=status, checks=checks[:20], pending=pending[:20])
        if repairs:
            row['assertionRepairs'] = repairs[:20]
        resources.append(row)
    return dict(status="checked" if all(row["status"] == "checked" for row in resources) else "incomplete",
                resources=resources)


# 旧路径仅复用同轮次、角色、资源和版本等条件匹配的回执；原生执行者在调用点跳过此复用。
def check_boundary(ident, resource_id, workspace_id, source, state, argv, timeout,
                   capture_changes=False, environment=None, material=None, retain_output=None, scope_reason=None, purpose="acceptance", writable_copy=False, script=None, environment_id=None, directory='.'):
    with database() as db:
        rows = db.execute(
            """SELECT data FROM operations WHERE thread=?
               AND json_extract(data,'$.operation')='check'
               AND json_extract(data,'$.resourceId')=?
               AND json_extract(data,'$.workspaceId')=?
               AND json_extract(data,'$.source.runId')=?
               AND coalesce(json_extract(data,'$.source.role'),'executor')=?
               AND json_extract(data,'$.beforeRevision')=?
               AND json_extract(data,'$.branch')=? AND json_extract(data,'$.head')=?
               ORDER BY seq DESC LIMIT 30""",
            (ident, resource_id, workspace_id, source["runId"], source.get("role", "executor"),
             state["revision"], state["branch"], state["head"]),
        ).fetchall()
    script_hash = W.digest(script.encode('utf8')) if script is not None else None
    command = argv if capture_changes else check_command_key(argv)
    for (raw,) in rows:
        op = json.loads(raw)
        target = op["target"]
        if (target.get("capture_changes", True) != capture_changes or target.get("environment") != environment
                or target.get("purpose", "acceptance") != purpose
                or target.get('writable_copy', False) != writable_copy
                or target.get('environment_id') != environment_id or target.get('directory', '.') != directory
                or target.get('scriptHash') != script_hash):
            continue
        result = op.get("result") or {}
        status = result.get("verification", {}).get("status")
        if status == "stale":
            break
        if target.get("material") != material or target.get("retain_output") != retain_output:
            continue
        previous = target["argv"] if capture_changes else check_command_key(target["argv"])
        if status == "passed":
            if retain_output and not result.get("process", {}).get("retainedOutput", {}).get("versionId"):
                continue
            if previous == command:
                return op, "passed", "当前版本已有相同命令的通过回执；总结结果与未覆盖范围"
        if status in {"failed", "timed_out", "unknown", "blocked"}:
            if previous == command and target.get("timeout", 30) == timeout:
                return op, status, "当前版本已执行过相同检查。核对原回执，或在有新证据时采用不同检查"
    return None


def check_changes_stash(argv):
    if Path(argv[0]).stem.lower() != "git":
        return False
    skip_value = False
    for index, arg in enumerate(argv[1:], 1):
        if skip_value:
            skip_value = False
        elif arg.startswith("-"):
            skip_value = arg in {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix"}
        else:
            return arg == "stash" and argv[index + 1:index + 2] not in (["list"], ["show"])
    return False


def output_page(ident, process_id, after=0, stream=None):
    if type(after) is not int or after < 0:
        raise W.Problem("invalid_cursor", "输出偏移无效")
    process = W.process_record(process_id)
    if process.get("conversationId") != ident:
        raise W.Problem("wrong_source", "检查输出不属于当前对话", 403)
    value = W.process_output(process_id, after, stream=stream) if stream else W.process_output(process_id, after)
    if len(value["text"]) > 4000:
        text = value["text"][:4000]
        value.update(text=text, next=after + len(text.encode("utf-8")), hasMore=True)
    return value


_TEST_FAILURE = re.compile(r"^(?:FAILED|ERROR)\s+(.+?::.+?)(?:\s+-\s+|$)")
_UNITTEST_FAILURE = re.compile(r"^FAIL:\s+(.+)")
_UNITTEST_ERROR = re.compile(r"^ERROR:\s+(\S+\s+\([\w.]+\))(?=\s|$)")
_UNITTEST_PASS = re.compile(r"^(\w+\s+\([\w.]+\))\s+\.\.\.\s+ok$")
_PYTEST_PASS = re.compile(r"^(\S+::.+?)\s+PASSED(?:\s|$)")
_TEST_OUTCOMES = r"passed|failed|errors?|skipped|deselected|xfailed|xpassed"
_TEST_COUNTS = re.compile(rf"\b(\d+) ({_TEST_OUTCOMES})\b")
_PYTEST_ITEM = rf"\d+ (?:{_TEST_OUTCOMES}|warnings?|reruns?)"
_PYTEST_SUMMARY = re.compile(rf"{_PYTEST_ITEM}(?:,\s+{_PYTEST_ITEM})* in \d+(?:\.\d+)?s(?: \(\d+:\d{{2}}:\d{{2}}\))?")
_KEY_ERROR = re.compile(r"^(?:E\s+)?(?:[\w.]+(?:Error|Exception):|ERROR\b|FAIL:|panic:)", re.I)
_ANSI_STYLE = re.compile(r"\x1b\[[0-9;]*m")


def _check_log_lines(process_id):
    path = W.record_path(process_id).with_suffix(".log")
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return
    with stream:
        line_number = 1
        while chunk := stream.readline(65536):
            after = stream.tell() - len(chunk)
            yield line_number, after, _ANSI_STYLE.sub("", chunk.decode("utf-8", "replace")).strip()
            while len(chunk) == 65536 and not chunk.endswith(b"\n"):
                chunk = stream.readline(65536)
            line_number += 1


def _check_log_entry(line, after, text):
    return dict(line=line, after=after, text=redact(text)[:360])


def _check_case_entry(line, after, text, identity, framework):
    entry = _check_log_entry(line, after, text)
    if framework == 'unittest':
        if 'unittest.loader._FailedTest' in identity:
            return entry  # Import/lookup failure placeholder, not an actual test entry.
        match = re.fullmatch(r'(\w+)\s+\(([\w.]+)\)', identity)
        if not match:
            return entry
        method, qualified = match.groups()
        identity = qualified if qualified.endswith('.' + method) else qualified + '.' + method
    # Incomplete, ambiguous or redacted identities are not safe to copy as selectors.
    if (len(identity) <= 360 and identity.count('[') == identity.count(']')
            and '...' not in identity and redact(identity) == identity):
        entry.update(selector=identity, framework=framework)
    return entry


# 日志诊断独立于进程退出码，只列可识别的测试报告事实，不从命令成功推导测试或需求通过。
# 首个失败允许沿同一异常链推进定位并保留包装上下文，独立用例失败另存有界列表。
def check_diagnostics(output):
    failures, errors, blockers, tail = {}, {}, {}, []
    assertion_failures = {}
    assertion_passes = {}
    report_framework = None
    before = deque(maxlen=4)
    first_failure = None
    first_strength = 0
    wrapper = None
    report_closed = False
    next_traceback = False
    chain_traceback = False
    remaining_context = 0
    no_tests = False
    counts = {}
    unittest_total = None
    django_clone = False
    argv = output.get('argv') or output['process'].get('argv') or []
    node = bool(argv and Path(argv[0]).name.lower() in {'node','node.exe'})
    node_counts, node_block, named = Counter(), {}, False
    for line, after, text in _check_log_lines(output["process"]["id"]):
        if not text:
            continue
        if node:
            # Count closed reports from Node (spec or TAP), never a standalone count.
            if re.match(r'^(?:[✔✖▶] |(?:not )?ok \d+ - )', text):
                named = True
            match = re.fullmatch(r'(?:ℹ|#) (tests|pass|fail|cancelled|skipped|todo) (\d+)', text)
            if match:
                node_block[match[1]] = int(match[2])
            if re.match(r'^(?:ℹ|#) duration_ms ', text):
                if (named and set(node_block) == {'tests','pass','fail','cancelled','skipped','todo'}
                        and node_block['tests'] == sum(v for k,v in node_block.items() if k != 'tests')):
                    node_counts.update(passed=node_block['pass'], failed=node_block['fail'],
                        errors=node_block['cancelled'], skipped=node_block['skipped'] + node_block['todo'])
                named, node_block = False, {}
        django_clone = django_clone or '_clone_test_db' in text
        if first_strength and not report_closed:
            if text.startswith(('During handling of the above exception', 'The above exception')):
                next_traceback = True
            elif text.startswith('Traceback (most recent call last):'):
                report_closed = not next_traceback
                chain_traceback = next_traceback
                next_traceback = False
        # Stay inside the first failing report, including its exception chain.
        # A later testcase or report section must not replace its location.
        if first_failure is not None and (_TEST_FAILURE.match(text) or _UNITTEST_FAILURE.match(text)
                or _UNITTEST_ERROR.match(text) or first_strength and re.match(r'^(?:[-=]{5,}|_{3,}|Ran \d+ tests? in )',text)):
            report_closed = True
        if first_failure is not None and remaining_context and not report_closed:
            first_failure['context'].append(_check_log_entry(line,after,text))
            remaining_context -= 1
        strong = bool(re.match(r'^(?:E\s+)?[\w.]+(?:Error|Exception):',text))
        if (first_failure is None and (_KEY_ERROR.match(text) or _TEST_FAILURE.match(text))
                or strong and not report_closed and (not first_strength or chain_traceback)):
            current = _check_log_entry(line,after,text)
            context = [_check_log_entry(*entry) for entry in before]
            if wrapper and all(entry['line']!=wrapper['line'] for entry in context):
                context.insert(0,wrapper)
            first_failure = dict(current, context=[*context,current])
            remaining_context = 8-len(first_failure['context'])
            first_strength = int(strong)
            chain_traceback = False
            if strong and wrapper is None:
                wrapper = current
                next_traceback = text.startswith('ImportError: Failed to import test module:')
        before.append((line,after,text))
        no_tests = no_tests or bool(re.search(r'\bno tests (?:ran|collected)\b|\bcollected 0 items\b',text,re.I))
        if len(tail) == 3:
            tail.pop(0)
        tail.append((line, after, text[:1024]))
        if match := re.match(r'^Ran (\d+) tests? in ', text):
            unittest_total = int(match[1])
            no_tests = no_tests or unittest_total == 0
        if unittest_total is not None and re.match(r'^(?:OK|FAILED)(?:\s|$|\()', text):
            report={}
            for label, key in [('failures','failed'),('errors','errors'),('skipped','skipped'),
                               ('expected failures','xfailed'),('unexpected successes','xpassed')]:
                if match := re.search(r'(?:\(|,\s*)'+label+r'=(\d+)', text): report[key]=int(match[1])
            report['passed']=max(0, unittest_total-sum(report.values()))
            if unittest_total or not counts:
                counts=report
                report_framework = 'unittest'
        failure = _TEST_FAILURE.match(text)
        failed_case = failure if failure else (
            _UNITTEST_FAILURE.match(text) or _UNITTEST_ERROR.match(text))
        if failed_case and len(failures) < 8:
            failures.setdefault(failed_case[1][:360], _check_case_entry(
                line, after, text, failed_case[1], 'pytest' if failure else 'unittest'))
            entry = failures[failed_case[1][:360]]
            if (failure and text.startswith('FAILED ') or _UNITTEST_FAILURE.match(text)) and entry.get('selector'):
                assertion_failures.setdefault(entry['selector'], dict(entry, state='failed'))
        if len(assertion_passes)<16:
            pytest_pass = _PYTEST_PASS.match(text)
            passed_case = pytest_pass or _UNITTEST_PASS.match(text)
            if passed_case:
                entry = _check_case_entry(line, after, text, passed_case[1], 'pytest' if pytest_pass else 'unittest')
                if entry.get('selector'):
                    assertion_passes.setdefault(entry['selector'], dict(entry, state='passed'))
        if (_KEY_ERROR.match(text) or failure) and len(errors) < 6:
            errors.setdefault(text[:360], (line, after, text))
        if (kind := check_blocker(text)) and kind not in blockers:
            blockers[kind] = (line, after, text)
        # Pytest includes the elapsed time even in its quiet summary. Bare
        # counts from ordinary output must stay in the log.
        summary = text.strip("= ")
        if _PYTEST_SUMMARY.fullmatch(summary):
            if matches := _TEST_COUNTS.findall(summary):
                counts = {"errors" if kind == "error" else kind: int(n) for n, kind in matches}
                report_framework = 'pytest'
    if node_counts:
        counts, report_framework = dict(node_counts), 'node:test'
    diagnostics = dict(
        counts=counts,
        failedCases=list(failures.values()),
        keyErrors=[_check_log_entry(*entry) for entry in errors.values()],
        environmentBlockers=[dict(_check_log_entry(*entry), kind=kind) for kind, entry in blockers.items()],
    )
    if first_failure:
        diagnostics['firstFailure'] = first_failure
    serial_requested = '--parallel=1' in argv or any(
        arg=='--parallel' and argv[i+1:i+2]==['1'] for i,arg in enumerate(argv))
    if (django_clone and 'database_access' in blockers and not serial_requested
            and any(str(a).replace('\\','/').lower().endswith('tests/runtests.py') for a in argv)):
        # Suggest, never rewrite or run a different command on the user's behalf.
        removed=set()
        for i,arg in enumerate(argv):
            if arg=='--parallel':
                removed.add(i)
                if i+1<len(argv) and re.fullmatch(r'[+-]?\d+',str(argv[i+1])):removed.add(i+1)
            elif str(arg).startswith('--parallel='):removed.add(i)
        serial=[arg for i,arg in enumerate(argv) if i not in removed]
        diagnostics['retryHint'] = dict(argv=[*serial,'--parallel','1'],
            reason='Django并行克隆SQLite遇到只读副本；原测试入口显式--parallel 1，避免克隆数据库，不需放开源码写入。')
    # 全部跳过、未收集和未报告单独呈现；报告状态与命令退出状态、需求验收保持分离。
    diagnostics['testReport'] = dict(state=(
        'failed' if counts.get('failed',0) or counts.get('errors',0) else
        'passed' if counts.get('passed',0) else
        'all_skipped' if counts.get('skipped',0) and not counts.get('xfailed',0) and not counts.get('xpassed',0) else
        'no_tests' if ((no_tests and not any(v for k,v in counts.items() if k!='deselected'))
                       or (counts.get('deselected',0) and set(counts)=={'deselected'})) else
        'reported' if counts else 'not_reported'),
        requirementAcceptance='not_inferred')
    if node_counts:
        diagnostics['testReport']['framework'] = 'node:test'
    if report_framework and not output.get('truncated'):
        cases = [case for case in assertion_failures.values() if case['framework']==report_framework]
        if len(cases)>counts.get('failed', 0):
            cases = []
        passed = [case for key,case in assertion_passes.items()
                  if case['framework']==report_framework and key not in assertion_failures]
        if len(passed)<=counts.get('passed',0):
            cases.extend(passed)
        if cases:
            diagnostics['testReport'].update(framework=report_framework, cases=cases)
    if not failures and not errors and not blockers:
        diagnostics["logTail"] = [_check_log_entry(*entry) for entry in tail]
    return diagnostics


def check_tool_result(result):
    if result.get("diagnostics") is None:
        return result
    return {key: value for key, value in result.items() if key not in {"text", "next", "hasMore"}}


def search_check_output(process, query, stopped=lambda: False):
    if not isinstance(query, str) or not query.strip() or len(query) > 240 or "\n" in query or "\r" in query:
        raise W.Problem("invalid_arguments", "日志查询需要 1–240 个字符的单行文本")
    matches = []
    for line, after, text in _check_log_lines(process["id"]):
        if stopped():
            raise W.Problem("cancelled", "日志查询已取消", 409)
        if query in text:
            matches.append(_check_log_entry(line, after, text))
            if len(matches) == 8:
                break
    return dict(ok=True, processId=process["id"], matches=matches)


def live_check_output(ident, action_id, run_id):
    """Read only the process bound to this exact scoped tool invocation."""
    with database() as db:
        row = db.execute(
            "SELECT data FROM operations WHERE thread=? AND json_extract(data,'$.source.actionId')=? "
            "AND json_extract(data,'$.source.runId')=? AND json_extract(data,'$.operation')='check' "
            "ORDER BY seq DESC LIMIT 1",
            (ident, action_id, run_id),
        ).fetchone()
    process_id = json.loads(row[0]).get("processId") if row else None
    if not process_id:
        return None
    process = W.process_record(process_id)
    source = process.get("source") or {}
    if (process.get("conversationId") != ident or source.get("sessionId") != ident or
            source.get("actionId") != action_id or source.get("runId") != run_id):
        return None
    page = W.process_output(process_id)
    # The last partial line may still contain an unfinished credential.
    return page["text"].rpartition("\n")[0] + "\n" if "\n" in page["text"] else ""


# 经宿主网关的原生普通检查由后端持有后台生命周期；直接调用和写回检查仍走原有同步入口。
def adapter(root, operation):
    def call(resource_id=None, **args):
        from .tools import current_call
        source = current_call() or {}
        if source.get('nativeExecutor') and os.environ.get('HARNESS_AGENT_URL'):
            if operation == 'check' and not args.get('capture_changes'):
                from .agent_gateway import call as hosted
                return hosted('check-start', resource_id=resource_id, **args)
            if operation in {'output', 'stop_check'}:
                from .agent_gateway import call as hosted
                return hosted('check-stop' if operation == 'stop_check' else 'check-output',
                              resource_id=resource_id, **args)
            if operation == 'check_environments':
                from .agent_gateway import call as hosted
                return hosted('check-environments', resource_id=resource_id, **args)
        if operation == 'check_environments':
            return review_check_environments(root, resource_id, **args)
        if operation == 'check' and args.get('capture_changes') and 'yield_time_ms' in args:
            from .check_sessions import _milliseconds
            _milliseconds(args.pop('yield_time_ms'), 'yield_time_ms')
        elif operation == 'check' and 'yield_time_ms' in args:
            return dict(ok=False,code='host_required',error='后台检查需要应用后端持有；请从Electron运行，或省略yield_time_ms使用同步检查。')
        if operation == 'undo' and 'operation_id' in args:
            args['source_operation_id'] = args.pop('operation_id')
        return invoke(root, operation, resource_id, **args)

    return call


def edit_bytes(raw, old_text, new_text):
    return edit_hunks(raw, [dict(old_text=old_text, new_text=new_text)])


def _edit_range(raw, old_text, new_text):
    if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
        raise W.Problem("invalid_edit", "局部编辑需要非空原文和替换文本")
    if max(len(old_text), len(new_text)) > W.LIMIT or "\0" in old_text + new_text:
        raise W.Problem("invalid_edit", "局部编辑文本过长或含无效字符")
    # 匹配兼容 LF/CRLF 并保留原换行；必须唯一定位，防止重复片段被误替换。
    literal = old_text.replace("\r\n", "\n").encode("utf-8")
    pattern = re.escape(literal).replace(b"\\\n", b"\r?\n")
    start = len(codecs.BOM_UTF8) if raw.startswith(codecs.BOM_UTF8) else 0
    matches = re.compile(pattern).finditer(raw, start)
    match = next(matches, None)
    if not match:
        raise W.Problem("edit_not_found", "未找到原文，请读取当前目标片段后核对", 409)
    # Search again after the first byte, including overlapping matches.
    if match.re.search(raw, match.start() + 1):
        raise W.Problem("ambiguous_edit", "原文匹配多处，请增加相邻上下文以唯一定位", 409)
    replacement = new_text.replace("\r\n", "\n").encode("utf-8")
    if b"\r\n" in match.group() or b"\n" not in match.group() and b"\r\n" in raw:
        replacement = replacement.replace(b"\n", b"\r\n")
    return match.start(), match.end(), replacement


def edit_hunks(raw, edits):
    """Validate all ranges against one immutable input before constructing any output."""
    if not isinstance(edits, list) or not 1 <= len(edits) <= 64:
        raise W.Problem("invalid_edit", "edits 需要 1–64 个原文与替换片段")
    ranges = []
    for index, item in enumerate(edits):
        try:
            if not isinstance(item, dict) or set(item) != {"old_text", "new_text"}:
                raise W.Problem("invalid_edit", "每处编辑只提供 old_text 和 new_text")
            ranges.append((*_edit_range(raw, item["old_text"], item["new_text"]), index))
        except W.Problem as error:
            raise W.Problem(error.code, f"edits[{index}]: {error.message}", error.status) from error
    ranges.sort()
    if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
        raise W.Problem("overlapping_edit", "多个片段范围重叠，整批未写入", 409)
    result, cursor = [], 0
    for start, end, replacement, _ in ranges:
        result.extend((raw[cursor:start], replacement))
        cursor = end
    value = b"".join([*result, raw[cursor:]])
    if len(value) > W.LIMIT:
        raise W.Problem("large_file", "修改后文件超过 2 MiB", 413)
    return value


# 定义定位仅支持可解析的 Python，失败仍按原窗口读取；是否完整取决于实际返回范围。
def _read_code_page(result, args):
    text = result.get("text")
    if not isinstance(text, str):
        return result
    definition = None
    definition_end = None
    if args.get('definition'):
        import ast
        definition = dict(available=False, complete=False, reason='unsupported_language')
        if Path(result.get('path') or args.get('relative_path') or '').suffix.lower() == '.py':
            try:
                tree = ast.parse(text)
                nodes = []
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        continue
                    first = min([node.lineno, *[d.lineno for d in node.decorator_list]])
                    if first <= args['line'] <= node.end_lineno:
                        nodes.append((node.end_lineno-first, first, node))
                if nodes:
                    _, first, node = min(nodes, key=lambda item: item[:2])
                    definition = dict(available=True, startLine=first, endLine=node.end_lineno,
                                      name=node.name, kind=type(node).__name__, complete=False)
                    lines = text.split('\n')
                    definition_end = sum(len(part)+1 for part in lines[:node.end_lineno])
                    definition_end = min(len(text), definition_end)
                    args = dict(args, line=first, line_count=min(args.get('line_count',2000),2000,node.end_lineno-first+1))
                else:
                    definition['reason'] = 'no_enclosing_definition'
            except (SyntaxError, ValueError, RecursionError):
                definition['reason'] = 'unparseable_source'
    start = args.get("offset", 0)
    end = len(text)
    line = args.get("line")
    if line is not None:
        lines = text.split("\n")
        start = min(len(text), sum(len(part) + 1 for part in lines[:line - 1]))
        end = min(len(text), start + sum(len(part) + 1 for part in lines[line - 1:line - 1 + args.get("line_count", 2000)]))
    else:
        end = min(len(text), start + sum(len(part) + 1 for part in text[start:].split('\n')[:2000]))
    requested_end = definition_end if definition_end is not None else end
    # Match the model-facing DSH read window, without cutting a UTF-8 codepoint.
    # If a long line is clipped, the cursor still points into the original text.
    selected = text[start:end]
    bounded = []
    consumed = 0
    used_bytes = 0
    for part in selected.splitlines(keepends=True):
        take = part[:2000]
        remaining = 50 * 1024 - used_bytes
        take = take.encode('utf8')[:remaining].decode('utf8', 'ignore')
        bounded.append(take)
        consumed += len(take)
        used_bytes += len(take.encode('utf8'))
        if len(take) < len(part) or remaining <= len(take.encode('utf8')):
            break
    page = ''.join(bounded)
    end = start + consumed
    start_line = text.count("\n", 0, start) + 1
    end_line = start_line + page.count("\n") - int(page.endswith("\n")) if page else start_line
    page_result = dict(result, text=page, totalChars=len(text),
                startLine=start_line, endLine=end_line,
                truncated=end < requested_end, hasMore=end < len(text),
                nextLine=(end_line + int(page.endswith("\n"))) if end < len(text) else None,
                nextOffset=end if end < len(text) else None)
    if line is not None and line > len(lines):
        page_result.update(pastEndOfFile=True, requestedLine=line, totalLines=len(lines))
    if definition is not None:
        definition['complete'] = bool(definition.get('available') and end >= definition_end)
        page_result['definition'] = definition
    return page_result


# 搜索页证明各已读文件的版本，不构造整仓一致快照；续查绑定查询、文件列表和当前文件指纹。
def _search_code(ws, args, stopped):
    from . import baseline, project_text

    query = args.get("query")
    path = args.get("relative_path", ".")
    limit = args.get("limit", 20)
    context_lines = args.get('context_lines', 0)
    if type(context_lines) is not int or not 0 <= context_lines <= 20:
        raise W.Problem('invalid_arguments', 'context_lines允许0–20行；总输出有界，截断后可续查')
    offset = args.get("offset", 0)
    cursor = args.get('cursor')
    if not isinstance(query, str) or not query.strip() or len(query) > 200 or "\n" in query or "\r" in query:
        raise W.Problem("invalid_arguments", "query 需要 1–200 个字符的非空单行字面文本")
    if not isinstance(path, str):
        raise W.Problem("invalid_arguments", "relative_path 需要目录或文件的相对路径字符串")
    if type(limit) is not int or not 1 <= limit <= 30:
        raise W.Problem("invalid_arguments", f"limit={limit!r} 无效；允许整数 1–30，省略时为 20")
    if type(offset) is not int or not 0 <= offset <= 100000:
        raise W.Problem("invalid_arguments", "offset 需要 0–100000 的整数；续查使用 nextOffset")
    if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048 or offset):
        raise W.Problem('invalid_cursor', 'cursor 使用上页的 nextCursor，不能同时提供非零 offset')
    root = Path(ws["path"])
    scope = W.agent_file_path(ws, path)
    if not scope.exists() or baseline.hit("read", scope):
        raise W.Problem("resource_denied", "搜索路径不存在或受保护", 403)
    if scope.is_file():
        paths = [scope.relative_to(root).as_posix()]
    elif W.git_exe() and W.safe_git(ws, ["rev-parse", "--is-inside-work-tree"], check=False).returncode == 0:
        listed = W.safe_git(ws, ["--literal-pathspecs", "ls-files", "--cached", "--others",
                                 "--exclude-standard", "-z", "--", scope.relative_to(root).as_posix()])
        paths = dict.fromkeys(filter(None, listed.stdout.decode("utf-8").split("\0")))
    else:
        def walk():
            for folder, dirs, files in os.walk(scope):
                dirs[:] = sorted(d for d in dirs if d not in W.IGNORE and not (Path(folder) / d).is_symlink()
                                 and not getattr(Path(folder) / d, "is_junction", lambda: False)())
                for name in sorted(files):
                    yield (Path(folder) / name).relative_to(root).as_posix()
        paths = walk()
    paths = sorted(set(paths))
    query_key = W.digest(json.dumps([str(root), scope.relative_to(root).as_posix(), query], ensure_ascii=False).encode())
    listing_key = W.digest(json.dumps(paths, ensure_ascii=False).encode())
    first_line, expected_file = 1, None
    prior_skips = 0
    stream_cursor = cursor if cursor and cursor.startswith("s1.") else None
    if stream_cursor:
        offset, _, _, _, prior_skips = project_text.positions(stream_cursor, "s1", 5)
        if offset >= len(paths):
            raise W.Problem('search_cursor_stale', '搜索文件列表已变化，请重新搜索', 409)
    elif cursor is not None:
        try:
            state = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            index, line = state['index'], state['line']
            prior_skips = state.get('skips', 0)
            if (state.get('version') != 1 or type(index) is not int or not 0 <= index < len(paths)
                    or type(prior_skips) is not int or not 0 <= prior_skips <= index
                    or type(line) is not int or not 1 <= line <= W.LIMIT + 1
                    or state.get('revision') is not None and not isinstance(state['revision'], str)):
                raise ValueError('invalid position')
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise W.Problem('invalid_cursor', '搜索续查位置无效；使用同一查询上页返回的 nextCursor')
        if state.get('query') != query_key:
            raise W.Problem('search_cursor_mismatch', '查询或资料范围已变化，请重新搜索')
        if state.get('listing') != listing_key:
            raise W.Problem('search_cursor_stale', '搜索文件列表已变化，请重新搜索；旧页仍只证明当时内容', 409)
        offset, first_line, expected_file = index, line, state.get('revision')
    protected = baseline.read_snapshot_guard()
    parents = {}
    matches, scanned, size, result_chars = [], 0, 0, 0
    revisions = {}
    skipped = {}
    deadline = time.monotonic() + project_text.SCAN_SECONDS

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    def page(index=None, line=1, revision=None, reason=None, stream_position=None):
        skips = prior_skips + sum(skipped.values())
        result = dict(ok=True, matches=matches, filesScanned=scanned, truncated=index is not None,
                      fileRevisions=revisions, consistency='per_file', scannedBytes=size,
                      pageCoverage='partial' if index is not None else 'has_skips' if skips else 'complete')
        if skips:
            result.update(skippedFileCount=skips, skippedByReason=skipped, coverageNote='跳过的文件未验证是否命中；原因按本页统计')
        if stream_position:
            result.update(nextCursor=stream_position, truncationReason=reason)
            return result
        if index is not None:
            position = dict(version=1, query=query_key, listing=listing_key,
                            index=index, line=line, revision=revision)
            if skips:
                position['skips'] = skips
            result.update(nextCursor=base64.urlsafe_b64encode(json.dumps(position).encode()).decode(),
                          truncationReason=reason)
            # Legacy offsets address files, and must not skip remaining matches
            # in this file. Exact continuation always uses nextCursor.
            if line == 1:
                result['nextOffset'] = index
        return result

    for index, relative in enumerate(paths):
        if stopped():
            raise W.Problem("cancelled", "代码搜索已取消", 409)
        if index < offset:
            continue
        if index > offset and (index - offset >= 12000 or size >= project_text.SCAN_BYTES or time.monotonic() >= deadline):
            reason = 'files' if index-offset >= 12000 else 'bytes' if size >= project_text.SCAN_BYTES else 'time'
            return page(index, reason=reason)
        continuing = index == offset and (expected_file or stream_cursor)
        try:
            path = Path(relative)
            if path.parent not in parents:
                parent = W.file_path_from_root(root, path.parent)
                parents[path.parent] = parent if parent == root / path.parent else None
            parent = parents[path.parent]
            if parent is None:
                if continuing:
                    raise W.Problem('resource_denied', '续查文件路径已不在可读取范围内', 403)
                skip('unavailable')
                continue
            candidate = parent / path.name
            if not candidate.is_relative_to(scope) or protected(candidate):
                if continuing:
                    raise W.Problem('resource_denied', '续查文件已不在可读取范围内', 403)
                skip('unavailable')
                continue
            info = candidate.lstat()
            if (not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                if continuing:
                    raise W.Problem('search_cursor_stale', '续查文件类型或大小已变化，请重新搜索', 409)
                skip('not_regular')
                continue
            if info.st_size > W.LIMIT:
                if index == offset and expected_file:
                    raise W.Problem('search_cursor_stale', '续查文件大小已变化，请重新搜索', 409)
                found, revision, consumed, reason, position = project_text.search(
                    ws, relative, query, context_lines, limit-len(matches), 14000-result_chars,
                    project_text.SCAN_BYTES-size, deadline, stopped, index, query_key+listing_key,
                    prior_skips + sum(skipped.values()), stream_cursor if index == offset else None)
                size += consumed
                scanned += 1
                if found:
                    matches.extend(found)
                    revisions[relative] = revision
                    result_chars += sum(len(json.dumps(m, ensure_ascii=False)) for m in found) + len(relative)+len(revision)+16
                if position:
                    return page(index, reason=reason, stream_position=position)
                continue
            if index == offset and stream_cursor:
                raise W.Problem('search_cursor_stale', '续查文件大小已变化，请重新搜索', 409)
            with candidate.open('rb') as stream:
                raw = stream.read(W.LIMIT + 1)
            if len(raw) > W.LIMIT:
                raise W.Problem('search_cursor_stale', '搜索期间文件大小改变，请重新搜索', 409)
            revision = W.digest(raw) if index == offset and expected_file else None
            if index == offset and expected_file and revision != expected_file:
                raise W.Problem('search_cursor_stale', '续查文件已变化，请从新版本重新搜索', 409)
            size += len(raw)
            if b"\0" in raw:
                skip('binary')
                continue
            text = raw.decode("utf-8-sig")
        except W.Problem as error:
            if error.code in ('binary','encoding'):
                size += getattr(error, 'scanned_bytes', 0)
                skip(error.code)
                continue
            if continuing or error.code in ('cancelled','revision_conflict','search_cursor_stale'):
                raise
            skip('unavailable')
            continue
        except (OSError, UnicodeError) as error:
            if continuing:
                raise W.Problem('search_cursor_stale', '续查文件已变化或不可读取，请重新搜索', 409)
            skip('encoding' if isinstance(error, UnicodeError) else 'unavailable')
            continue
        scanned += 1
        checked_match = False
        lines = text.split('\n')
        for number, content in enumerate(lines, 1):
            if index == offset and number < first_line:
                continue
            if number % 256 == 0:
                if stopped():
                    raise W.Problem('cancelled', '代码搜索已取消', 409)
                if time.monotonic() >= deadline:
                    return page(index, number, revision or W.digest(raw), 'time')
            at = content.find(query)
            if at >= 0:
                revision = revision or W.digest(raw)
                if not checked_match:
                    try:
                        if W.file_path_from_root(root, relative) != candidate:
                            break
                    except (W.Problem, OSError):
                        break
                    checked_match = True
                if len(matches) >= limit:
                    return page(index, number, revision, 'matches')
                match = dict(path=relative, line=number,
                             text=content.removesuffix("\r")[max(0, at - 100):at + 260])
                if context_lines:
                    start = max(1, number-context_lines)
                    excerpt='\n'.join(lines[start-1:number+context_lines])
                    excerpt = excerpt[:4000]
                    match['context'] = dict(startLine=start, endLine=start+excerpt.count('\n'), text=excerpt,
                        truncated=len('\n'.join(lines[start-1:number+context_lines])) > len(excerpt))
                chars = len(json.dumps(match, ensure_ascii=False)) + (0 if relative in revisions else len(relative)+len(revision)+16)
                if matches and result_chars + chars > 14000:
                    return page(index, number, revision, 'output')
                matches.append(match)
                result_chars += chars
                revisions[relative] = revision
    return page()


def invoke(root, operation, resource_id, **args):
    with W.git_observation.operation() as costs:
        result = _invoke(root, operation, resource_id, **args)
        if isinstance(result, dict):
            result["operationCosts"] = W.git_observation.summary(costs)
        return result


def _invoke(root, operation, resource_id, **args):
    from .tools import current_stop

    # 先归一化动作，再按宿主调用身份校验所需能力；工具参数中的路径或名称不构成授权。
    if operation in {"edit", "undo"}:
        operation = {"edit": "write", "undo": "restore"}[operation]
    capability = {
        "initialize": "write",
        "write": "write",
        "restore": "write",
        "branch": "write",
        "check": "execute",
    }.get(operation)
    op = thread = background = None
    history_id = None
    result_sources = None
    effect_started = False
    stopped = current_stop() or (lambda: False)
    started = time.monotonic()

    def require_running():
        if stopped():
            raise W.Problem("cancelled", "代码操作已取消", 409)

    try:
        allowed_args = {
            "initialize": {"branch", "expected_revision"},
            "inspect": {"relative_path", "offset", "line", "line_count", "definition", "include_check_revision", "cursor"},
            "search": {"query", "relative_path", "limit", "offset", "context_lines", "cursor"},
            "output": {"process_id", "after", "query", "stream"},
            "branch": {"branch", "expected_revision"},
            "write": {"relative_path", "text", "parts", "expected_revision", "create", "old_text", "new_text", "edits"},
            "check": {"argv", "directory", "script", "script_operation_id", "expected_revision", "include_conversation_record", "timeout", "capture_changes", "material", "retain_output", "scope_reason", "purpose", "writable_copy", "environment_id", "scenario", "parameters"},
            "restore": {"plan_id", "source_operation_id"},
        }
        if operation not in allowed_args:
            raise W.Problem("invalid_arguments", "未声明的代码操作")
        if set(args) - allowed_args[operation]:
            unknown = sorted(set(args) - allowed_args[operation])
            hint = "；路径请使用 relative_path" if "resource_path" in unknown else ""
            raise W.Problem("invalid_arguments", "未声明参数：" + ", ".join(unknown) + hint)
        if not isinstance(resource_id, str) or not resource_id:
            raise W.Problem("invalid_arguments", "请提供当前已授权代码资源的 resource_id")
        if operation == 'check' and args.get('environment_id') is not None:
            environment_id = args['environment_id']
            if not isinstance(environment_id, str) or not re.fullmatch(r'new|[0-9a-f]{32}', environment_id):
                raise W.Problem('invalid_arguments',
                    'environment_id 仅接收 new 或已返回的32位environment_id（process.environmentId）；'
                    'environmentFingerprint/旧environment是指纹，不能当ID。普通检查请省略此参数，自动复用同任务运行时。')
        if operation == "write" and (
            not isinstance(args.get("relative_path"), str)
            or set(args) & {"old_text", "new_text", "edits"} and set(args) & {"text", "parts", "create"}
            or "edits" in args and set(args) & {"old_text", "new_text"}
            or "parts" in args and "text" in args
            or "text" in args and not isinstance(args["text"], str)
        ):
            raise W.Problem("invalid_arguments", "整文件写入须在 text 与 parts 中选一项，局部编辑使用编辑字段")
        require_running()
        thread, resource, ws, source = context_resource(root, resource_id, capability)
        if operation == 'check':
            args['directory'] = check_directory(thread, resource_id, ws, args)
        if operation == "search":
            result = _search_code(ws, args, stopped)
            context_resource(root, resource_id, register_workspace=False)
            return result
        if operation == "output":
            process = W.process_record(args["process_id"])
            if process["conversationId"] != thread["id"] or process["workspaceId"] != ws["id"]:
                raise W.Problem("wrong_source", "检查输出不属于所选代码资源", 403)
            if "query" in args:
                if "after" in args or 'stream' in args:
                    raise W.Problem("invalid_arguments", "日志查询与定点偏移需分别使用")
                return search_check_output(process, args["query"], stopped)
            return dict(output_page(thread["id"], args["process_id"], args.get("after", 0), args.get('stream')), ok=True)
        if operation == "inspect":
            path = args.get("relative_path")
            if 'cursor' in args and (not isinstance(args['cursor'], str) or not path or path == '.'
                                      or set(args) & {'line','offset','line_count','definition','include_check_revision'}):
                raise W.Problem('invalid_cursor', '按行续读只传资源、文件相对路径和上次的 nextCursor')
            if (type(args.get('include_check_revision', False)) is not bool
                    or args.get('include_check_revision') and path not in (None, '', '.')):
                raise W.Problem('invalid_arguments', 'include_check_revision 只用于资源根目录')
            line, count, offset = args.get("line"), args.get("line_count"), args.get("offset", 0)
            if ('definition' in args and type(args['definition']) is not bool
                    or args.get('definition') and line is None):
                raise W.Problem('invalid_arguments', 'definition为可选布尔值，启用时需传line；不支持时仍按行读取')
            if (type(offset) is not int or offset < 0
                    or "line" in args and (type(line) is not int or line < 1 or "offset" in args)
                    or "line_count" in args and (line is None or type(count) is not int or not 1 <= count <= 2000)):
                raise W.Problem("invalid_arguments", "line（从1开始）与 offset（字符偏移）不能同时提供；line_count 仅与 line 一起使用，允许1–2000")
            result = projects.read_resource(
                thread["id"], resource_id, path if path is not None else ".", offset, confirm=True, line=line,
                text_page=args, stopped=stopped
            )
            require_running()
            page = result if result.get('readMode') == 'stream' else _read_code_page(result, args)
            if "text" in page:
                return dict(page, ok=True)
            selected = Path(ws['path']).resolve()
            if W.local_writer_root(selected) != selected:
                git = dict(available=False, files=[], revision=None, reason='所选资源是 Git 工作树的子目录')
            elif not os.path.lexists(selected / ".git"):
                extra = {}
                if args.get('include_check_revision'):
                    check_state = W.check_source_state(ws)
                    extra['checkRevision'] = check_state['revision']
                    if check_state.get('recordRevision'):
                        extra['recordCheckRevision'] = check_state['recordRevision']
                return dict(page, ok=True, git=dict(available=False, files=[], revision=None),
                            projectRevision="non-git", **extra)
            else:
                git = W.git_status(ws)
            return dict(
                page,
                ok=True,
                git=git,
                projectRevision=git["revision"] or "non-git",
            )
        # File edits share the checkout gate, but serialize capture/write/receipt
        # for the same target. Snapshots and worktree switches use it exclusively.
        file_lock = (FileLock(W.file_write_lock_path(ws, args["relative_path"]).with_suffix(".operation.lock"), check=require_running)
                     if operation == "write" else nullcontext())
        with FileLock(W.writer_lock_path(ws["path"]), check=require_running, shared=operation == "write") as writer_lock, file_lock:
            locked = time.monotonic()
            # Recheck grants after waiting for the cross-process writer lock.
            thread, resource, ws, source = context_resource(
                root, resource_id, capability
            )
            require_running()
            W.guard_workspace_writer(
                ws["path"], RuntimeView(), exclude_thread=thread["id"]
            )
            isolated_check = operation == "check" and args.get("capture_changes", False) is False
            with W.git_observation.phase('preflight'):
                if isolated_check or operation == "restore":
                    state = W.check_source_state(ws)
                    if isolated_check:
                        state['revision'] = W.selected_check_revision(state, includes_record(args))
                    if state.get('folderObservation') and not state['revision']:
                        raise W.Problem('folder_version_unavailable', '无法确认此目录的当前版本，未执行操作', 409)
                elif operation == "write":
                    state = W.file_workspace_state(ws)
                else:
                    state = W.git_status(ws)
            if operation == "initialize":
                from . import code_bootstrap
                return code_bootstrap.initialize(thread,resource,ws,source,args)
            if operation != "write" and not state.get("folderObservation") and (not state.get("available") or not state.get("head")):
                raise W.Problem(
                    "git_required",
                    "此操作需要已提交的 Git 基线。普通目录可直接隔离运行命令，并用文件编辑工具保存改动。",
                    409,
                )
            if operation != "branch" and not isolated_check and (
                state.get("protected") or state.get("branch") == "(detached)"
            ):
                raise W.Problem(
                    "protected_branch",
                    "请先让 Agent 创建 agent/ 功能分支，禁止直接修改保护分支",
                    403,
                )
            if operation == "branch":
                branch = args.get("branch", "")
                if (not isinstance(branch, str) or not branch.startswith("agent/")
                        or W.safe_git(ws, ["check-ref-format", "--branch", branch], check=False).returncode):
                    raise W.Problem("invalid_branch", "分支需为合法的 agent/ 功能分支")
                if args.get("expected_revision") != state["revision"]:
                    raise W.Problem("revision_conflict", "代码版本已变化，请刷新并重新审阅", 409)
            if operation == "check":
                from . import check_material
                scenario_identity = None
                script_origin = None
                if 'script_operation_id' in args:
                    if set(args) & {'argv','script','material','scenario','parameters'} or args.get('capture_changes'):
                        raise W.Problem('invalid_arguments','引用旧脚本时只选择 script_operation_id，不可混用其他检查入口或回写')
                    script, script_origin = check_material.operation_script(root,thread['id'],resource_id,ws['id'],
                                                                          args['script_operation_id'])
                    args = dict(args,script=script)
                elif 'scenario' in args:
                    if any(k in args for k in ('argv','script','material')) or args.get('capture_changes'):
                        raise W.Problem('invalid_arguments','scenario与argv/script/material或写回不能同时使用')
                    if not native_check_scope(thread,source,ws,state):
                        raise W.Problem('scenario_scope','场景运行需要当前L0任务身份',403)
                    from . import test_workroom
                    staged, scenario_identity = test_workroom.resolve(thread['id'],source['logicalTaskId'],args['scenario'],args.get('parameters'))
                    args = dict(args,script=staged)
                elif 'parameters' in args:
                    raise W.Problem('invalid_arguments','parameters只用于场景')
                if 'script' in args:
                    if 'argv' in args and args['argv'] != ['python', '.harness-check/input.py']:
                        raise W.Problem('invalid_arguments', 'script 会直接作为隔离副本 .harness-check/input.py 执行，请省略argv')
                    args = dict(args, argv=['python', '.harness-check/input.py'])
                argv, timeout = args.get("argv"), args.get("timeout", 30)
                if args.get("purpose", "acceptance") not in {"exploration", "acceptance"}:
                    raise W.Problem("invalid_arguments", "purpose 为 exploration 或 acceptance")
                scope_reason = args.get("scope_reason", "")
                if not isinstance(scope_reason, str) or len(scope_reason) > 320:
                    raise W.Problem("invalid_arguments", "补充检查理由不能超过 320 字")
                # 可写隔离副本只影响副本；capture_changes 会影响源工作区，另需执行者身份和写权限。
                capture_changes = args.get("capture_changes", False)
                check_scope = native_check_scope(thread, source, ws, state)
                environment_id = args.get('environment_id')
                if environment_id is not None and (not check_scope or capture_changes):
                    raise W.Problem('invalid_scope','准备环境只用于当前 L0 任务的隔离检查',409)
                writable_copy = args.get('writable_copy', False)
                if type(writable_copy) is not bool:
                    raise W.Problem('invalid_arguments', 'writable_copy 必须为布尔值')
                if type(capture_changes) is not bool:
                    raise W.Problem("invalid_arguments", "capture_changes 必须是布尔值")
                if capture_changes:
                    if source.get("role", "executor") != "executor":
                        raise W.Problem("policy_denied", "复核者只可在隔离副本内执行检查", 403)
                    context_resource(root, resource_id, "write")
                if (not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv)
                        or not argv[0].strip() or argv[0].lstrip().startswith("-")):
                    raise W.Problem("invalid_arguments", "argv 需要非空的字符串数组")
                if type(timeout) is not int or not 1 <= timeout <= 120:
                    raise W.Problem("invalid_timeout", "检查时限需为 1—120 秒")
                # 普通检查绑定锁内观察到的当前版本；显式候选或写回必须匹配预期版本，冲突时不启动。
                expected = args.get("expected_revision")
                if expected is None and not capture_changes:
                    args = dict(args, expected_revision=state["revision"])
                elif state["revision"] != expected:
                    version_field = ('recordCheckRevision' if W.record_revision(state['revision']) else 'checkRevision'
                                     if state.get('folderObservation') else 'projectRevision')
                    return dict(ok=False, code="revision_conflict",
                                error=f"版本已变化，未启动检查。指定版本请重新核对 {version_field}；普通检查可省略 expected_revision。",
                                expectedProjectRevision=expected, projectRevision=state["revision"])
                if check_changes_stash(argv):
                    raise W.Problem("check_changes_git_state",
                                    "检查工具不能暂存、应用或清理 Git stash。保留当前改动，在独立工作树核对基线。", 403)
                environment = None if capture_changes else check_environment()
                material_data, retain_output = check_material.prepare(thread["id"], args)
                boundary = None if source.get('nativeExecutor') or script_origin else check_boundary(thread["id"], resource_id, ws["id"], source, state, argv, timeout,
                                          capture_changes, environment, args.get("material"), retain_output,
                                          scope_reason=scope_reason if source.get("role", "executor") == "executor" else None,
                                          purpose=args.get("purpose", "acceptance"), writable_copy=writable_copy,
                                          script=args.get('script'),environment_id=environment_id, directory=args['directory'])
                if boundary and environment_id is None:
                    previous, status, message = boundary
                    prior = previous.get("result") or {}
                    return dict(check_tool_result(prior), ok=status=='passed', reused=True,
                                summary='复用同版本、同条件的原命令回执；覆盖范围仍需核对。',
                                verification=dict(status=status, revision=state['revision']),
                                operationId=previous['id'], evidenceActionId=previous['source'].get('actionId'))
            if operation == "restore":
                from . import restorations

                # 原动作撤销与人工恢复计划二选一；范围、授权和版本校验交给既有恢复流程。
                plan_id = args.get("plan_id")
                if "source_operation_id" in args:
                    if plan_id:
                        raise W.Problem("invalid_arguments", "仅提供原写入操作编号或人工批准的恢复计划之一")
                    plan = restorations.prepare(
                        thread["id"], args["source_operation_id"], actor="agent",
                        own_source=source, resource_id=resource_id,
                    )
                    plan_id = plan["id"]
                    args["plan_id"] = plan_id
                restore_target = restorations.load(thread["id"], plan_id)["path"]
            # 先记录 running 动作，再准备基线并触发效果；中途失败仍可通过原 operationId 核对。
            op = {
                "id": uuid.uuid4().hex,
                "operation": operation,
                "resourceId": resource_id,
                "workspaceId": ws["id"],
                "source": source,
                "started": time.time(),
                "beforeRevision": state["revision"],
                "branch": state["branch"],
                "head": state["head"],
                "state": "running",
                "fileOnly": operation == "write" or operation == "restore" and bool(state.get('folderObservation')),
                "gitContext": state.get("gitContext"),
                "target": {
                    key: args[key]
                    for key in ("relative_path", "expected_revision", "argv", "branch", "timeout", "material", "retain_output", "scope_reason")
                    if key in args
                },
            }
            from .check_sessions import observer
            background = observer.get() if operation == 'check' else None
            if background:
                op.update(id=background.operation_id, started=background.started, preparationOnly=False)
            if operation == "check":
                op["target"]["capture_changes"] = capture_changes
                op["target"]["directory"] = args['directory']
                if W.record_revision(state['revision']):
                    op['target']['include_conversation_record'] = True
                op['target']['writable_copy'] = writable_copy
                op["target"]["purpose"] = args.get("purpose", "acceptance")
                op["target"]["environment"] = environment
                if 'script' in args:
                    op['target']['scriptHash'] = W.digest(material_data)
                    if script_origin:
                        op['target']['scriptSourceOperationId'] = script_origin
                    elif not background and not scenario_identity:
                        op['checkScript'] = args['script']
                if scenario_identity:
                    op['target']['scenario'] = scenario_identity
                op['target']['checkLabel'] = check_label(argv,
                    script=material_data.decode('utf8',errors='replace') if material_data else args.get('script'),
                    scope_reason=scope_reason)
                if scenario_identity:op['target']['checkLabel']='测试场景：'+scenario_identity['name']
                if environment_id is not None:
                    op['target']['environment_id'] = environment_id
                if background:
                    background.confirm_preparation_source(ws, state)
            if operation == "restore":
                op["target"]["relative_path"] = restore_target
            if background:
                prepare_check(thread["id"], op)
                background.recorded(op)
            else:
                record(thread["id"], op)
            file_effect = None
            if operation == "write":
                path = args["relative_path"]
                target = W.agent_file_path(ws, path, write=True)
                allowed, why = Policy("workspace", root=Path(ws["path"])).check(
                    "write_file", {"path": path}, kind="write")
                if not allowed:
                    raise W.Problem("policy_denied", why, 403)
                create = args.get("create") is True
                if create and target.exists():
                    raise W.Problem("revision_conflict", "文件已存在，未覆盖", 409)
                original = None if create else W.editable_bytes(ws, path)
                old = W._file_text(path, original) if original is not None else None
                if old and old["revision"] != args.get("expected_revision"):
                    raise W.Problem("revision_conflict", "文件版本已变化，请重新读取后再编辑", 409)
                if "edits" in args:
                    replacement = edit_hunks(original, args["edits"])
                elif "old_text" in args or "new_text" in args:
                    replacement = edit_bytes(original, args.get("old_text"), args.get("new_text"))
                else:
                    from .worker_plugins import compose_transcription, default_transcription_parts
                    text = args.get("text")
                    parts = args.get("parts")
                    if "parts" not in args and (text is None or not old or not old["bom"] and old["eol"] != "CRLF"):
                        parts = default_transcription_parts(thread, source, text, guard_changed_copy=create)
                    if parts is not None or "parts" in args:
                        text, result_sources = compose_transcription(thread, source, parts)
                    if text is None:
                        raise W.Problem("invalid_arguments", "提供 parts、唯一已采用的转写成果或完整 text")
                    if old:
                        previous = old["text"]
                        removed = len(previous.splitlines()) - len(text.splitlines())
                        if (removed >= 100 or removed >= 30 and len(text) * 2 < len(previous)
                                or len(previous) >= 2048 and len(text) * 4 < len(previous)):
                            raise W.Problem("unsafe_replacement", "整文件保存会丢失大量原文。使用 edit_project_file 提供唯一原文片段和替换文本。", 409)
                    replacement = W.file_text_bytes(text, None if result_sources else old)
                file_effect = original, replacement
            from . import change_history as history
            history_start = time.monotonic()
            if operation in {"branch", "write"} or operation == "check" and capture_changes:
                history_id = history.prepare(thread["id"], resource, ws, op, check=require_running,
                                             file_effect=file_effect)
            prepared = time.monotonic()
            require_running()
            if operation == "branch":
                W.check_revision(ws, args.get("expected_revision"))
                if state["branch"] != branch:
                    require_running()
                    effect_started = True
                    W.safe_git(ws, ["switch", "-c", branch])
                result = {"git": W.git_status(ws)}
            elif operation == "write":
                from .project_activity import watch

                watch(thread, resource, target.relative_to(Path(ws["path"])).as_posix())
                atomic_json(W.home() / "code-backups" / (op["id"] + ".json"), {
                    "path": path,
                    "original": base64.b64encode(original).decode() if original is not None else None,
                })
                require_running()
                _, _, current, _ = context_resource(root, resource_id, "write")
                if (current["path"] != ws["path"] or W.agent_file_path(ws, path, write=True) != target
                        or W.file_workspace_state(ws).get("gitContext") != state.get("gitContext")):
                    raise W.Problem("revision_conflict", "文件准备期间工作树或分支变化，未写入", 409)
                effect_started = True
                saved = (W.create_file_bytes(ws, path, replacement) if create
                         else W.save_bytes(ws, path, replacement, old["revision"]))
                result = {
                    "path": path,
                    "revision": saved["revision"],
                    "previousRevision": old["revision"] if old else None,
                    # A file hash/event is not a project revision. Resolve the
                    # latter only at a project/check/candidate boundary.
                    "projectRevision": None,
                    "fileChangeRevision": "pending:" + op["id"],
                    "backupId": op["id"],
                }
                if result_sources:
                    if W.editable_bytes(ws, path) != replacement:
                        raise W.Problem("write_unverified", "写入后读取到的字节与组装正文不同，请核对文件版本", 409)
                    result["resultSources"] = result_sources
            elif operation == "restore":
                def retain_restoration(before, after):
                    nonlocal history_id
                    history_id = history.prepare(thread["id"], resource, ws, op, check=require_running,
                                                 file_effect=(before, after))

                def mark_restore_write():
                    nonlocal effect_started
                    require_running()
                    _, _, current, _ = context_resource(root, resource_id, "write")
                    if current["path"] != ws["path"]:
                        raise W.Problem("revision_conflict", "恢复准备期间工作树变化，未写入", 409)
                    effect_started = True
                result = restorations.execute(
                    thread, resource, ws, source, op, plan_id, on_write=mark_restore_write,
                    retain_history=retain_restoration
                )
            elif operation == "check":
                from .control import benchmark_deadline
                from .project_activity import watch

                if capture_changes:
                    watch(thread, resource, "")
                    context_resource(root, resource_id, "write")
                    W.check_revision(ws, args.get("expected_revision"))
                argv = args["argv"]
                from .subtasks import command_policy

                command_policy(thread["id"], argv)
                allowed, why = Policy("workspace", root=Path(ws["path"])).check(
                    "run_check", {"cmd": argv}, kind="exec"
                )
                if not allowed:
                    raise W.Problem("policy_denied", why, 403)
                timeout = args.get("timeout", 30)
                if type(timeout) is not int or not 1 <= timeout <= 120:
                    raise W.Problem("invalid_timeout", "检查时限需为 1—120 秒")
                launch_start = time.monotonic()
                cutoff = benchmark_deadline(W.P.data_root() / "runs" / thread["id"], source.get("role"))
                effect_started = True
                material_hooks = {}
                if not capture_changes and check_scope:
                    material_hooks['workspace_scope'] = check_scope
                    material_hooks['environment_id'] = environment_id
                if not capture_changes and not writable_copy:
                    material_hooks['readonly_source'] = True
                if material_data is not None or retain_output or not capture_changes and not writable_copy:
                    material_hooks["stage_material"] = lambda target: check_material.stage(target, material_data)
                if retain_output:
                    artifact_source = dict(source, actor="agent", codeRevision=state["revision"],
                                           checkOperationId=op["id"], material=args.get("material"))
                    material_hooks["retain_output"] = lambda target: check_material.collect(
                        thread["id"], target, retain_output, artifact_source
                    )
                if scenario_identity:
                    previous_collector = material_hooks.get('retain_output')
                    def collect_scenario(target):
                        from .test_workroom import collect
                        try: report = collect(target)
                        except (OSError,ValueError) as error: report = dict(status='unknown',error=str(error)[:300])
                        kept = previous_collector(target) if previous_collector and (Path(target)/'.harness-check'/retain_output).is_file() else {}
                        return dict(kept,workroom=report)
                    collect_scenario.collect_on_failure = True
                    material_hooks['retain_output'] = collect_scenario
                # 隔离准备可能耗时，真正启动前再次核对执行权、写回权和运行时，防止沿用过期授权/环境。
                def authorize_check():
                    require_running()
                    context_resource(root, resource_id, "execute")
                    if capture_changes:
                        context_resource(root, resource_id, "write")
                    if not capture_changes and check_environment() != environment:
                        raise W.Problem("sandbox_runtime", "检查准备期间运行时或依赖版本已变化，未启动命令", 409)
                try:
                    # 普通检查释放工程写锁后在隔离环境运行；源版本由启动/退出时的观察继续核对。
                    if not capture_changes:
                        writer_lock.release()
                    with W.git_observation.phase('preparation'):
                        if source.get('nativeExecutor') and not capture_changes:
                            material_hooks['separate_output'] = True
                        process = W.launch(ws, argv, source=source, operation_id=op["id"],
                                           check=authorize_check, start_deadline=cutoff,
                                           force_isolated=not capture_changes, expected_revision=state["revision"],
                                           working_directory=args['directory'],
                                           **material_hooks)
                    if environment_id == 'new' and process.get('environmentId'):
                        op['target']['environment_id'] = process['environmentId']
                except W.Problem as error:
                    # The runtime check happens while preparing the sandbox, before spawning.
                    if error.code in {"sandbox_runtime", "cancelled", "command_not_found", "unsupported_runtime"}:
                        effect_started = False
                    raise
                launched = time.monotonic()
                op["processId"] = process["id"]
                try:
                    with database() as db:
                        db.execute(
                            "UPDATE operations SET data=json_set(data,'$.processId',?,'$.target',json(?)) WHERE thread=? AND id=?",
                            (process['id'], json.dumps(op['target']), thread["id"], op["id"]),
                        )
                except (W.Problem, OSError, sqlite3.Error):
                    # The live view is optional. Still supervise the launched check.
                    pass
                if background:
                    background.launched(process)
                deadline = time.monotonic() + timeout
                timed_out = False
                cancelled = False
                while W.process_record(process["id"])["state"] in {
                    "running",
                    "starting",
                    "stopping",
                }:
                    if stopped() or time.monotonic() >= deadline:
                        timed_out = time.monotonic() >= deadline or cutoff is not None and time.monotonic() >= cutoff
                        cancelled = not timed_out
                        W.stop_process(process["id"])
                        terminal_deadline = time.monotonic() + 3
                        while (
                            W.process_record(process["id"])["state"]
                            in {"running", "starting", "stopping"}
                            and time.monotonic() < terminal_deadline
                        ):
                            time.sleep(0.05)
                        break
                    time.sleep(0.05)
                receipt_start = time.monotonic()
                result = output_page(thread["id"], process["id"])
                if result["process"]["state"] in {"running", "starting", "stopping"}:
                    result["process"] = dict(result["process"], state="unknown")
                    result["error"] = (
                        "尚未确认检查进程退出，请核对运行记录；不会宣称检查通过"
                    )
                revision_start = time.monotonic()
                with W.git_observation.phase('after_exit'):
                    result["projectRevisionAfter"] = W.selected_check_revision(W.check_source_state(ws),
                        W.record_revision(state["revision"]))
                process = result["process"]
                cancelled = cancelled or bool(process.get('stopRequested')) or bool(background and background.cancel.is_set())
                timed_out = timed_out or cutoff is not None and process.get("exitCode") == 124 and stopped()
                # 版本变化及未确认终态优先于超时/取消和退出码；passed 是命令层事实，测试与验收另记。
                verification = (
                    "stale" if state["revision"] != result["projectRevisionAfter"] else
                    "unknown" if process["state"] == "unknown" else
                    "timed_out" if timed_out else
                    "cancelled" if cancelled else
                    "passed" if not cancelled and process["state"] == "completed" and process["exitCode"] == 0 else
                    "failed"
                )
                result["verification"] = dict(status=verification, revision=state["revision"])
                result["environment"] = environment
                result['diagnostics'] = check_diagnostics(result)
                if scenario_identity:
                    report = (process.get('retainedOutput') or {}).get('workroom') or dict(status='unknown',error='场景未提交结果')
                    result['workroom'] = dict(scenario=scenario_identity,**report)
                    if verification == 'passed':
                        status = report.get('status')
                        if status in {'failed','blocked','preparation_failed','error'}: verification = 'failed'
                        elif status != 'passed' and not (report.get('commands',0) and result['diagnostics']['counts'].get('passed',0)): verification = 'unknown'
                        result['verification']['status'] = verification
                    if report.get('status') == 'blocked':result['environmentBlocker']='scenario_environment'
                result['assessment'] = dict(kind='test_report' if result['diagnostics']['counts'] else 'command',
                    testCounts=result['diagnostics']['counts'], requirementAcceptance='not_inferred',
                    purpose=args.get('purpose', 'acceptance'))
                if scenario_identity and verification == 'unknown':
                    result['assessment'].update(kind='unconfirmed',testCounts={})
                if verification != "passed":
                    blockers = result["diagnostics"]["environmentBlockers"]
                    if verification == "failed" and blockers:
                        result["environmentBlocker"] = blockers[0]["kind"]
                result["summary"] = {
                    "passed":"命令执行成功（退出码0）。测试数量见diagnostics.counts；未自动判定需求验收通过。",
                    "stale":"检查期间项目版本变化，结果不能作为新版本已通过的凭据。",
                    "failed":"检查未通过。核对失败原因，只在修正原因或能扩大有效覆盖时再检查。",
                    "timed_out":"检查超时。保留输出，改用范围明确的检查或说明未验证范围。",
                    "cancelled":"检查已取消。保留当前版本和已确认回执。",
                    "unknown":"检查退出状态未确认，需核对原回执。",
                }[verification]
                result['executionEnvironment'] = process.get('executionEnvironment')
                if script_origin:
                    result['scriptSourceOperationId'] = script_origin
                    result['scriptHash'] = op['target']['scriptHash']
                if source.get('nativeExecutor'):
                    report = result['diagnostics'].setdefault('testReport',dict(state='not_reported',requirementAcceptance='not_inferred'))
                    if args.get('purpose', 'acceptance') == 'acceptance':
                        workroom_report = result.get('workroom') or {}
                        if workroom_report.get('status') in {'passed','failed'}:
                            selectors = Counter(row.get('selector') for row in workroom_report['assertions'])
                            assertions = [dict(selector=row['selector'], framework='workroom',
                                               state='passed' if row['passed'] else 'failed',text=redact(row['name']))
                                          for row in workroom_report['assertions'] if row.get('selector')
                                          and selectors[row['selector']]==1 and redact(row['selector'])==row['selector']]
                            if assertions:
                                report['cases'] = report.get('cases', []) + assertions[:24]
                                if report['state']!='failed':
                                    report['state'] = 'failed' if any(not r['passed'] for r in workroom_report['assertions']) else 'passed'
                                result['assessment']['kind'] = 'test_report'
                        if (verification in {'passed','failed'} and process.get('exitCode')==0
                                and not result['diagnostics']['environmentBlockers']
                                and any(case['state']=='failed' for case in report.get('cases', []))):
                            verification = 'failed'
                            result['verification'].update(status=verification, reason='assertion_failed')
                    result['execution'] = dict(executed=True, processId=process['id'],
                        state=process['state'], exitCode=process.get('exitCode'),
                        timedOut=timed_out, cancelled=cancelled,
                        sourceRevision=state['revision'], sourceRevisionAfter=result['projectRevisionAfter'])
                    result['summary'] = ('命令状态 '+process['state']+'（退出码 '+str(process.get('exitCode'))+
                        '）。检查判定 '+verification+'，测试报告 '+report['state']+'。')
                    from .tool_results import command_summary
                    result['summary'] = command_summary(result) or result['summary']
                result['checkKey'] = check_key(op['target'])
                result['checkLabel'] = op['target']['checkLabel']
                if scenario_identity and verification == 'unknown':result['summary']='场景已运行但没有有效断言/测试报告，或结果不完整；未证明验收通过。'
                if any(str(arg).replace('\\','/').startswith('.harness-check/') for arg in args.get('argv',[])) and not args.get('material') and 'script' not in args:
                    result['summary'] += ' .harness-check为隔离保留目录，工作区同名文件不会复制；临时探针请直接传script，或使用material指定脚本成果。'
            else:
                raise W.Problem("invalid_operation", "未知代码操作")
            finalize_start = time.monotonic()
            if history_id:
                outcome = result.get("state", result.get("process", {}).get("state"))
                sealed = history.finish(thread["id"], history_id, ws,
                    "unknown" if outcome == "unknown" else "failed" if operation == "restore" and outcome == "failed" else "observed",
                    [args.get("relative_path")],
                    [dict(path=result["path"], before=result.get("previousRevision"), after=result["revision"])]
                        if operation in {"write", "restore"} and result.get("path") and result.get("revision") and outcome not in {"unknown", "failed"} else [])
                result["changeId"] = history_id
                if sealed.get("projectBaseline"):
                    result.update(projectBaseline=sealed["projectBaseline"], historyCoverage="explicit-targets")
                    if sealed.get("projectBaselineReason"):
                        result["projectBaselineReason"] = sealed["projectBaselineReason"]
            if operation == "check":
                def ms(begin, end):
                    return round((end - begin) * 1000)

                result["timingsMs"] = dict(lockWait=ms(started, locked),
                    preflight=ms(locked, history_start) + ms(prepared, launch_start),
                    historyBefore=ms(history_start, prepared) if history_id else 0, launch=ms(launch_start, launched),
                    waitAndRead=ms(launched, finalize_start), historyAfter=ms(finalize_start, time.monotonic()) if history_id else 0,
                    outputRead=ms(receipt_start, revision_start), revisionAfter=ms(revision_start, finalize_start),
                    sandbox=process.get("timingsMs", {}))
            else:
                result["timingsMs"] = dict(lockWait=round((locked-started)*1000),
                    preflight=round((history_start-locked)*1000), historyBefore=round((prepared-history_start)*1000),
                    effectAndObservation=round((finalize_start-prepared)*1000),
                    historyAfter=round((time.monotonic()-finalize_start)*1000))
            cost = W.git_observation.current()
            if cost is not None:
                result["operationCosts"] = W.git_observation.summary(cost)
            # 操作 completed 表示动作及回执已收尾，检查命令仍可失败；工具 ok 按检查状态另行判断。
            finish(
                thread["id"],
                op["id"],
                "unknown"
                if result.get("process", {}).get("state") == "unknown"
                or result.get("state") == "unknown"
                else "failed"
                if operation == "restore" and result.get("state") == "failed"
                else "completed",
                result,
            )
            ok = operation != "check" or result["verification"]["status"] == "passed"
            if operation == "restore":
                ok = result.get("state") == "completed"
            return dict(check_tool_result(result), ok=ok, operationId=op["id"]) if operation == "check" else dict(
                result, ok=ok, operationId=op["id"])
    except (W.Problem, OSError, ValueError, TimeoutError, sqlite3.Error) as error:
        result = {
            "ok": False,
            "code": getattr(error, "code", "code_failed"),
            "error": getattr(error, "message", str(error)),
        }
        if op is not None:
            state = "failed"
            if operation == 'check':
                result['checkKey'] = check_key(op['target'])
                result['checkLabel'] = op['target'].get('checkLabel') or check_label(op['target'].get('argv'))
                result['argv'] = op['target'].get('argv')
                if background and not effect_started:
                    if result['code'] == 'cancelled':
                        state = 'cancelled'
                    result['operationId'] = op['id']
                    result['execution'] = dict(executed=False, state=state, exitCode=None)
                elif result['code'] in {"command_not_found", "unsupported_runtime"}:
                    result['execution'] = dict(executed=False, state=state, exitCode=None)
            if operation == "check" and result["code"] in {"sandbox_runtime", "sandbox_unavailable", "sandbox_transport", "unsupported_runtime", "command_not_found", "cache_changed", "cache_capacity", "cache_disk_full"}:
                result["verification"] = dict(status="blocked", revision=op["beforeRevision"])
            if history_id:
                result["changeId"] = history_id
                # 已可能产生副作用却无法完整记账时保留 unknown；记录失败不代表文件未改变，不能自动重放。
                uncertain = effect_started and getattr(error, "code", None) not in {
                    "revision_conflict", "invalid_branch", "invalid_timeout", "policy_denied", "cancelled"}
                try:
                    history.finish(thread["id"], history_id, ws, "unknown" if uncertain else "failed",
                                   [args.get("relative_path")],
                                   cancelled_before_effect=not effect_started and result["code"] == "cancelled")
                except (W.Problem, OSError, ValueError, TimeoutError, sqlite3.Error):
                    uncertain = effect_started
                if uncertain:
                    state = "unknown"
                    result.update(state="unknown", error="操作或历史回执无法完整确认，请核对保留记录与实际文件；不会自动重放。")
            if operation == "restore" and args.get("plan_id"):
                try:
                    from . import restorations

                    plan = restorations.load(thread["id"], args.get("plan_id"))
                    if plan["state"] in {"applying", "unknown", "completed"}:
                        state = "unknown"
                        result.update(
                            state="unknown",
                            restorationPlanId=plan["id"],
                            backupId=op["id"],
                            error="恢复回执无法确认，请核对恢复记录及实际文件；不会自动重放",
                        )
                except (W.Problem, OSError, ValueError):
                    state = "unknown"
                    result["state"] = "unknown"
            finish(thread["id"], op["id"], state, result)
        return result


def finish(ident, oid, state, result):
    with database() as db:
        updated = db.execute(
            "UPDATE operations SET data=json_set(data,'$.state',?,'$.finished',?,'$.result',json(?)) "
            "WHERE thread=? AND id=?", (state, time.time(), json.dumps(result), ident, oid)
        )
        if not updated.rowcount:
            raise W.Problem("not_found", "代码处理证据不存在", 404)
