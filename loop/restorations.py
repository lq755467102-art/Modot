"""Human-authorized restoration of a single tracked UTF-8 file from verified backups."""

# 主要引用方包括 `workbench.py` 和 `tools.py`。

import base64
import difflib
import json
import os
import stat
from pathlib import Path
import time
import uuid
from . import workbench as W, projects, code_service as C
from .filelock import FileLock, atomic_json

MUTABLE = {
    "planRevision",
    "state",
    "approvedAt",
    "expiresAt",
    "note",
    "operationId",
    "source",
    "started",
    "result",
    "finished",
}


# 计划版本锁定恢复目标和影响预览；授权时间、执行状态等生命周期字段不会改变这份影响身份。
def revision(value):
    return W.digest(
        json.dumps(
            {k: v for k, v in value.items() if k not in MUTABLE},
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    )


def plan_path(ident):
    if (
        not isinstance(ident, str)
        or len(ident) != 32
        or any(c not in "0123456789abcdef" for c in ident)
    ):
        raise W.Problem("invalid_plan", "恢复计划编号无效")
    root = W.home() / "restoration-plans"
    if not root.resolve().is_relative_to(W.home().resolve()):
        raise W.Problem("path_escape", "恢复记录路径已变化", 403)
    return root / (ident + ".json")


def load(ident, plan_id):
    projects.conversation(ident)
    path = plan_path(plan_id)
    if not path.is_file() or path.stat().st_size > 256 * 1024:
        raise W.Problem("plan_missing", "恢复计划不存在或超出限制", 404)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise W.Problem("plan_unreadable", "恢复记录不可读，未重置", 503)
    if (
        not isinstance(value, dict)
        or not {
            "id",
            "conversationId",
            "resourceId",
            "sourceOperationId",
            "path",
            "state",
            "currentRevision",
            "restoreRevision",
            "projectRevision",
            "planRevision",
        }.issubset(value)
        or value["id"] != plan_id
        or revision(value) != value["planRevision"]
    ):
        raise W.Problem(
            "plan_unreadable", "恢复计划结构或影响预览已变化，请重新准备", 503
        )
    if value.get("conversationId") != ident:
        raise W.Problem("wrong_source", "恢复计划不属于此会话", 403)
    return value


def resource(ident, resource_id, write=False):
    if projects.conversation(ident).get("readonlyEvidence"):
        raise W.Problem("readonly_question", "只读追问不能准备或授权恢复", 403)
    selected = next(
        (
            r
            for r in projects.scope(ident)["resources"]
            if r["id"] == resource_id
            and r["kind"] == "code"
            and r["permission"] == "read"
        ),
        None,
    )
    if not selected or write and not selected.get("write"):
        raise W.Problem(
            "resource_denied", "请先明确选择代码资源并授权所需的读取/写入能力", 403
        )
    root = Path(selected["path"])
    if (
        not root.is_dir()
        or str(root.resolve()) != selected["path"]
        or W.local_writer_root(root) != root.resolve()
        or os.path.lexists(root / ".git") and W.writer_root(root) != root.resolve()
    ):
        raise W.Problem("resource_moved", "代码工作树根目录已变化", 409)
    return selected, W.register(str(root))


# 仅恢复已确认写入前的既有文件字节；新文件和外部副作用不具备可自动撤销的文件基线。
def baseline(ident, operation_id):
    op = C.operation(ident, operation_id)
    result = op.get("result") or {}
    if (
        op["operation"] not in {"write", "restore"}
        or op["state"] != "completed"
        or result.get("backupId") != op["id"]
        or not result.get("previousRevision")
    ):
        raise W.Problem(
            "not_restorable",
            "此动作没有可核对的既有文件基线；新文件、导出和外部动作不自动撤销",
            409,
        )
    root = W.home() / "code-backups"
    if not root.resolve().is_relative_to(W.home().resolve()):
        raise W.Problem("path_escape", "备份目录已变化", 403)
    path = W.file_path({"path": str(root)}, op["id"] + ".json")
    if not path.is_file() or path.stat().st_size > 3 * 1024 * 1024:
        raise W.Problem("backup_missing", "备份已丢失或超出限制", 409)
    try:
        backup = json.loads(path.read_text(encoding="utf-8"))
        raw = base64.b64decode(backup["original"], validate=True)
        text = raw.decode("utf-8-sig")
    except (ValueError, UnicodeError, KeyError, TypeError):
        raise W.Problem("backup_invalid", "备份不完整或不是可恢复的UTF-8文件", 409)
    relative = result.get("path") or op.get("target", {}).get("relative_path")
    if (
        not isinstance(relative, str)
        or backup.get("path") != relative
        or len(raw) > W.LIMIT
        or b"\0" in raw
        or W.digest(raw) != result["previousRevision"]
    ):
        raise W.Problem("backup_invalid", "备份内容与原动作基线不一致", 409)
    return op, relative, raw, text


def checked_path(ws, relative):
    W.agent_file_path(ws, relative, write=True)
    cursor = Path(ws["path"])
    for part in Path(relative).parts:
        cursor = cursor / part
        info = cursor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise W.Problem("linked_file", "链接或重解析路径不支持自动恢复", 409)
    if cursor.stat().st_nlink != 1:
        raise W.Problem("linked_file", "硬链接文件不支持自动恢复", 409)
    return cursor


def inspect_file(ident, operation_id, write=False):
    op, relative, raw, text = baseline(ident, operation_id)
    selected, ws = resource(ident, op["resourceId"], write)
    plain = (op.get("fileOnly") and op.get("gitContext") is None
             and not os.path.lexists(Path(ws["path"]) / ".git"))
    if not plain:
        tracked = W.safe_git(
            ws, ["ls-files", "--error-unmatch", "--stage", "--", relative], check=False
        )
        if tracked.returncode:
            raise W.Problem("untracked", "未跟踪文件不能自动覆盖或删除，请另存并单独核对", 409)
        if any(line.startswith(b"120000 ") for line in tracked.stdout.splitlines()):
            raise W.Problem("linked_file", "Git符号链接条目不支持自动恢复", 409)
    checked_path(ws, relative)
    current = W.file_read(ws, relative)
    # 原动作之后的任何文件修改都受保护，恢复不能把后续人工或 Agent 编辑当成原动作一起撤销。
    if current["revision"] != op["result"].get("revision"):
        raise W.Problem(
            "external_edit", "文件在原动作后又被修改，未覆盖外部或后续编辑", 409
        )
    return op, selected, ws, relative, raw, text, current


def prepare(ident, operation_id, actor="user", *, own_source=None, resource_id=None):
    if os.name != "nt":
        raise W.Problem(
            "unsupported_platform", "首批恢复仅支持Windows独占文件写入", 501
        )
    op, selected, ws, relative, raw, text, current = inspect_file(ident, operation_id, bool(own_source))
    state = W.check_source_state(ws)
    if state.get("folderObservation") and not state["revision"]:
        raise W.Problem('folder_version_unavailable', '无法确认恢复预览的目录版本', 409)
    if op.get("fileOnly"):
        from . import change_history
        if W.file_workspace_state(ws).get("gitContext") != op.get("gitContext"):
            comparison = "changed"
        else:
            matches = change_history.workspace_matches_write(ident, op, ws)
            comparison = "unavailable" if matches is None else "matches" if matches else "changed"
    elif op["result"].get("projectBaseline") == "unavailable":
        comparison = "unavailable"
    else:
        expected = op["result"].get("projectRevision") or op["result"].get("projectRevisionAfter")
        comparison = "matches" if (state["revision"], state["branch"], state["head"]) == (
            expected, op.get("branch"), op.get("head")
        ) else "changed"
    # 自行撤销权限更窄：只限本轮、同资源、执行者自己的最后一次已确认写入，且工作树仍一致。
    if own_source:
        source = op.get("source") or {}
        run = own_source.get("runId")
        if (
            not run or own_source.get("role") != "executor"
            or source.get("actor") != "agent" or source.get("role", "executor") != "executor"
            or source.get("runId") != run or op["operation"] != "write"
            or selected["id"] != resource_id or op["workspaceId"] != ws["id"]
            or op["result"].get("previousRevision") == current["revision"]
            or C.later_write_exists(op, relative)
        ):
            raise W.Problem("not_own_latest_write", "仅可撤销本轮同一代码资源中执行者自己的最后一次已确认写入", 409)
        if comparison == "unavailable":
            raise W.Problem("project_baseline_unavailable", "仅保留单文件版本，请由用户核对并授权恢复", 409)
        if comparison == "changed":
            raise W.Problem("external_edit", "写入后项目状态有变化，需用户核对恢复预览", 409)
    left = current["text"][:30000].splitlines(True)
    right = text[:30000].splitlines(True)
    clipped = (
        len(current["text"]) > 30000
        or len(text) > 30000
        or len(left) > 300
        or len(right) > 300
    )
    diff = "".join(
        difflib.unified_diff(
            left[:300],
            right[:300],
            fromfile="当前文件",
            tofile="恢复基线",
        )
    )
    value = dict(
        id=uuid.uuid4().hex,
        conversationId=ident,
        resourceId=selected["id"],
        sourceOperationId=operation_id,
        sourceActionId=op.get("source", {}).get("actionId"),
        path=relative,
        state="draft",
        actor=actor,
        created=time.time(),
        currentRevision=current["revision"],
        restoreRevision=W.digest(raw),
        projectRevision=state["revision"],
        branch=state["branch"],
        head=state["head"],
        projectAdvanced=comparison == "changed",
        workspaceComparison=comparison,
        protected=state.get("protected") or state.get("branch") == "(detached)",
        diff=diff[:20000],
        diffTruncated=clipped or len(diff) > 20000,
        warning="只恢复此受控文件；不会撤销已发生的进程、网络、安装、推送或其他外部副作用。需要补偿时单列操作。",
        writeMethod="恢复期间独占文件；若写入中断可能保留部分内容，结果未知时须核对原备份，不自动重放。",
    )
    value["planRevision"] = revision(value)
    if own_source:
        value["automaticRunId"] = own_source["runId"]
        value["planRevision"] = revision(value)
        value.update(state="authorized", expiresAt=time.time()+60,
                     note="仅撤销本轮执行者已确认的最后一次单文件写入")
    with FileLock(W.home() / ("restore-create-" + ident + ".lock")):
        if len(projects.conversation(ident).get("restorationPlans", [])) >= 100:
            raise W.Problem(
                "plan_limit", "本会话恢复计划已达保留上限，请建立新事项", 413
            )
        atomic_json(plan_path(value["id"]), value)
        W.change_registry(
            lambda data: data["threads"][ident]
            .setdefault("restorationPlans", [])
            .append(value["id"])
        )
    return value


def verify_project(ws, value):
    state = W.check_source_state(ws)
    if any(
        state.get(key) != value.get(field)
        for key, field in [
            ("revision", "projectRevision"),
            ("branch", "branch"),
            ("head", "head"),
        ]
    ):
        raise W.Problem(
            "revision_conflict", "代码分支、HEAD或工作树版本已变化，请重新预览恢复", 409
        )
    return state


def approve(ident, plan_id, body):
    path = plan_path(plan_id)
    with FileLock(path.with_suffix(".lock")):
        value = load(ident, plan_id)
        if (
            body.get("confirm") is not True
            or body.get("expectedPlanRevision") != value["planRevision"]
        ):
            raise W.Problem("confirmation", "请核对恢复影响和计划版本后明确授权", 409)
        if value["state"] != "draft":
            if value["state"] == "authorized":
                return value
            raise W.Problem("plan_used", "此计划已处理，请核对记录或重新预览", 409)
        _, _, ws, _, _, _, current = inspect_file(
            ident, value["sourceOperationId"], True
        )
        state = verify_project(ws, value)
        if (
            (value["projectAdvanced"] or value.get("workspaceComparison") == "unavailable")
            and body.get("acknowledgeProjectAdvance") is not True
        ):
            raise W.Problem(
                "project_advanced",
                "请核对本次恢复仅涉及目标文件，其他文件保持当前状态",
                409,
            )
        # 截断的预览不代表只恢复展示片段；实际恢复整个文件，因此另需确认部分预览这一边界。
        if value["diffTruncated"] and body.get("acknowledgePartialDiff") is not True:
            raise W.Problem(
                "partial_diff",
                "差异只展示部分内容；可分页查看基线，核对后明确确认恢复整个文件",
                409,
            )
        if value["protected"] or state["protected"]:
            raise W.Problem(
                "protected_branch",
                "保护分支不能执行恢复，请先切到功能分支后重新预览",
                403,
            )
        value.update(
            state="authorized",
            approvedAt=time.time(),
            expiresAt=time.time() + 600,
            note="仅授权这个文件/基线/当前版本；仍需当前和本轮写入权限。",
        )
        atomic_json(path, value)
        return value


def execute(thread, selected, ws, source, operation, plan_id, on_write=None, *, retain_history=None, project_view=None):
    path = plan_path(plan_id)
    with FileLock(path.with_suffix(".lock")):
        value = load(thread["id"], plan_id)
        if value["resourceId"] != selected["id"]:
            raise W.Problem("wrong_source", "恢复计划不属于当前代码资源", 403)
        if value.get("automaticRunId") and (
            value["automaticRunId"] != source.get("runId") or source.get("role") != "executor"
        ):
            raise W.Problem("wrong_source", "自行撤销只在原执行轮次有效", 403)
        # 只有已完成计划可以幂等取回结果；applying/unknown 或过期授权都不能据重试再次写入。
        if value["state"] == "completed":
            return dict(
                value["result"], idempotentReplay=True, restorationPlanId=plan_id
            )
        if value["state"] != "authorized" or value.get("expiresAt", 0) < time.time():
            raise W.Problem(
                "authorization_required",
                "恢复尚未授权、已过期或结果未知；不会自动重放",
                403,
            )
        old, _, _, relative, raw, text, current = inspect_file(
            thread["id"], value["sourceOperationId"], True
        )
        verify_project(ws, value)
        allowed, why = C.Policy("workspace", root=Path(ws["path"])).check(
            "write_file", {"path": relative}, kind="write"
        )
        if not allowed:
            raise W.Problem("policy_denied", why, 403)
        replaced = W.file_path(ws, relative).read_bytes()
        if W.digest(replaced) != current["revision"]:
            raise W.Problem("external_edit", "核对期间文件已变化", 409)
        if project_view is not None:
            if project_view.read(relative, 0, W.LIMIT + 1) != replaced:
                raise W.Problem('revision_conflict', '暂存还有后续编辑，不能套用旧恢复计划', 409)
            if on_write:
                on_write()
            project_view.replace(relative, raw)
            result = dict(state='staged',staged=True,path=relative,revision=W.digest(raw),
                          restoresOperationId=old['id'],restorationPlanId=plan_id,
                          projectRevision=project_view.revision())
            value.update(state='applying',source=source,operationId=operation['id'],result=result)
            atomic_json(path, value)
            project_view.record.setdefault('restores', []).append(plan_id)
            project_view.save()
            return result
        if retain_history is not None:
            retain_history(replaced, raw)
            verify_project(ws, value)
        atomic_json(
            W.home() / "code-backups" / (operation["id"] + ".json"),
            {
                "path": relative,
                "original": base64.b64encode(replaced).decode(),
                "conversationId": thread["id"],
                "resourceId": selected["id"],
                "restoresOperationId": old["id"],
            },
        )
        # 在写文件前持久标记 applying 并保留被替换字节；中断后可回查，不能默认回到尚未执行。
        value.update(
            state="applying",
            operationId=operation["id"],
            source=source,
            started=time.time(),
        )
        atomic_json(path, value)
        error = None
        try:
            if on_write is not None:
                on_write()
            saved = W.save_bytes(ws, relative, raw, current["revision"])
            observed = saved["revision"]
            status = "completed" if observed == value["restoreRevision"] else "unknown"
        except (W.Problem, OSError, ValueError) as failure:
            error = str(failure)[:500]
            try:
                observed = W.file_read(ws, relative)["revision"]
            except (W.Problem, OSError):
                observed = None
            # 能证明未写入或观察到原字节未变才判 failed；可能部分落盘时保留 unknown，禁止自动重放。
            known_no_write = isinstance(failure, W.Problem) and failure.code in {
                "revision_conflict",
                "file_busy",
                "linked_file",
                "unsupported_platform",
                "path_escape",
                "large_file",
                "resource_denied",
                "cancelled",
            }
            status = (
                "failed"
                if known_no_write or observed == current["revision"]
                else "unknown"
            )
        result = dict(
            state=status,
            path=relative,
            previousRevision=current["revision"],
            backupId=operation["id"],
            restoresOperationId=old["id"],
            restorationPlanId=plan_id,
            authorizationMode="own_write" if value.get("automaticRunId") else "user",
            warning=value["warning"],
        )
        if error is not None:
            result["error"] = error
        if observed is not None:
            result["revision"] = observed
        try:
            result["projectRevision"] = W.check_source_state(ws)["revision"]
        except (W.Problem, OSError, ValueError):
            status = "unknown"
            result.update(
                state="unknown",
                error="文件写入后无法核对项目版本，请核对实际文件和备份",
            )
        value.update(state=status, result=result, finished=time.time())
        atomic_json(path, value)
        return result


def public(ident, value):
    value = dict(value)
    if value["state"] == "authorized" and value.get("expiresAt", 0) < time.time():
        value["state"] = "expired"
    # 失去原运行所有者时仅把展示降为 unknown；这不是恢复成功或可以重新执行的证明。
    if value["state"] == "applying":
        directory = C.RuntimeView()._resolve_sid(ident)
        owner = W.RT.status(directory) if directory else {}
        try:
            op = C.operation(ident, value["operationId"])
        except W.Problem:
            op = {}
        if (
            not owner.get("active")
            or owner.get("run_id") != value.get("source", {}).get("runId")
            or op.get("state") in {"unknown", "failed", "completed"}
        ):
            value["state"] = "unknown"
    try:
        _, ws = resource(ident, value["resourceId"])
        W.agent_file_path(ws, value["path"])
        current = W.file_read(ws, value["path"])
        value.update(
            observedFileRevision=current["revision"],
            fileMatches=current["revision"]
            == (
                value["restoreRevision"]
                if value["state"] == "completed"
                else value["currentRevision"]
            ),
            verification="available",
        )
    except (W.Problem, OSError):
        value.update(verification="unavailable")
    return value


def baseline_page(ident, plan_id, after):
    value = load(ident, plan_id)
    resource(ident, value["resourceId"])
    _, _, raw, text = baseline(ident, value["sourceOperationId"])
    if type(after) is not int or not 0 <= after <= len(text):
        raise W.Problem("invalid_cursor", "基线文字偏移无效")
    return dict(
        text=text[after : after + 4000],
        next=after + 4000 if after + 4000 < len(text) else len(text),
        hasMore=after + 4000 < len(text),
        hash=W.digest(raw),
        historical=True,
    )


def route(ident, parts, method, body, q=None):
    if not parts and method == "GET":
        before = int(((q or {}).get("before") or ["101"])[0])
        ids = projects.conversation(ident).get("restorationPlans", [])
        rows = []
        selected = [
            (i + 1, pid) for i, pid in reversed(list(enumerate(ids))) if i + 1 < before
        ]
        for position, pid in selected[:30]:
            try:
                value = load(ident, pid)
                row = {
                    key: value[key]
                    for key in (
                        "id",
                        "sourceOperationId",
                        "path",
                        "state",
                        "actor",
                        "created",
                    )
                }
                if row["state"] == "applying":
                    row["state"] = public(ident, value)["state"]
                if (
                    row["state"] == "authorized"
                    and value.get("expiresAt", 0) < time.time()
                ):
                    row["state"] = "expired"
            except (W.Problem, OSError):
                row = dict(
                    id=pid,
                    path="未记录",
                    state="unknown",
                    actor="unknown",
                    error="恢复记录不可读，请核对，不能作为执行许可",
                )
            rows.append(dict(row, position=position))
        return 200, dict(
            plans=rows,
            hasMore=len(selected) > 30,
            nextBefore=selected[29][0] if len(selected) > 30 else None,
        )
    if not parts and method == "POST":
        return 200, prepare(ident, body.get("operationId"))
    if len(parts) == 1 and method == "GET":
        return 200, public(ident, load(ident, parts[0]))
    if len(parts) == 2 and parts[1] == "baseline" and method == "GET":
        return 200, baseline_page(
            ident, parts[0], int(((q or {}).get("after") or ["0"])[0])
        )
    if len(parts) == 2 and parts[1] == "approve" and method == "POST":
        return 200, approve(ident, parts[0], body)
    if len(parts) == 2 and parts[1] == "revoke" and method == "POST":
        path = plan_path(parts[0])
        with FileLock(path.with_suffix(".lock")):
            value = load(ident, parts[0])
            # 撤权只能阻止尚未开始的计划；已产生影响后必须核对实际结果，撤权本身不能撤销文件变化。
            if value["state"] not in {"draft", "authorized", "revoked"}:
                raise W.Problem(
                    "already_started",
                    "恢复已开始或已结束，撤销授权不能代替核对实际结果",
                    409,
                )
            value.update(state="revoked", note="用户撤销了尚未执行的恢复授权")
            atomic_json(path, value)
            return 200, value
    raise W.Problem("not_found", "恢复入口不存在", 404)


def tool_prepare(root, operation_id):
    from .tools import current_call

    source = current_call() or {}
    thread = projects.conversation(source.get("sessionId"))
    old = C.operation(thread["id"], operation_id)
    C.context_resource(root, old["resourceId"])
    return dict(ok=True, plan=prepare(thread["id"], operation_id, actor="agent"))
