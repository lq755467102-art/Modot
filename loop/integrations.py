"""Version-bound child deliveries: technical merge, human choices, then new checks.

The filtered dirty snapshot is the baseline, not the parent's original HEAD.
Applying files is deliberately checkpointed rather than pretending a multi-file
transaction is atomic. A partial or unknown application is never replayed.
"""

# 主要引用方包括 `workbench.py` 和 `tools.py`。

import difflib
import os
import json
from pathlib import Path
import time
import uuid
from . import workbench as W, projects, subtasks, code_service as C, authority_paths
from .filelock import FileLock, atomic_json

LIMIT = 8 * 1024 * 1024


def folder(ident):
    if (
        not isinstance(ident, str)
        or len(ident) != 32
        or any(c not in "0123456789abcdef" for c in ident)
    ):
        raise W.Problem("invalid_plan", "整合计划编号无效")
    return W.home() / "integrations" / ident


# 根据整合计划的来源、目标、版本、文件和决定计算确认指纹。
def fingerprint(row):
    return W.digest(
        json.dumps(
            {
                k: row[k]
                for k in (
                    "id",
                    "parentId",
                    "taskId",
                    "resourceId",
                    "sourceRevision",
                    "targetRevision",
                    "sourceBaseline",
                    "sourceHead",
                    "sourceBranch",
                    "targetHead",
                    "targetBranch",
                    "files",
                    "commits",
                )
            },
            sort_keys=True,
        ).encode()
    )


def save(row):
    row["planHash"] = fingerprint(row)
    if (
        len(json.dumps(row, ensure_ascii=False, indent=1).encode("utf8")) + 1
        > 512 * 1024
    ):
        raise W.Problem("plan_large", "整合元数据超过512KiB，保留原记录", 413)
    atomic_json(folder(row["id"]) / "plan.json", row)


# 读取父会话持有的原整合计划，核对记录归属。
def load(parent, ident):
    target = folder(ident) / "plan.json"
    try:
        if target.stat().st_size > 512 * 1024:
            raise ValueError("large")
        row = json.loads(target.read_text(encoding="utf8"))
        if row["parentId"] != parent or fingerprint(row) != row["planHash"]:
            raise ValueError("identity or hash")
        return row
    except (OSError, ValueError, KeyError):
        raise W.Problem("unknown_plan", "整合计划无法核对，保留原记录且不重放", 409)


# 从 `projects.py` 查父会话现有代码资源，实际写入再要求写权限。
def target(parent, rid, write=False):
    t = projects.conversation(parent)
    if t.get("subtaskOf") or t.get("readonlyEvidence"):
        raise W.Problem("parent_required", "请从父任务处理交付整合", 403)
    r = next(
        (
            r
            for r in projects.scope(parent)["resources"]
            if r["id"] == rid and r["kind"] == "code" and r["permission"] == "read"
        ),
        None,
    )
    if not r or write and not r.get("write"):
        raise W.Problem("resource_denied", "整合目标需要当前明确选择的代码授权", 403)
    path = Path(r["path"])
    if (
        not path.is_dir()
        or str(path.resolve()) != r["path"]
        or authority_paths.protected(path, "write")
    ):
        raise W.Problem(
            "protected_target", "目标目录已移动、为链接或属于宿主运行环境", 403
        )
    return r, W.register(r["path"])


# 从 `subtasks.py` 查子任务归属。仅已结束且留有隔离基线的代码任务可以准备整合。
def source(parent, task_id, rid, sv):
    row = subtasks.load(parent, task_id)
    if (
        row["kind"] != "code"
        or row["state"] not in {"completed", "failed", "blocked", "cancelled"}
        or not row.get("snapshot")
    ):
        raise W.Problem("undelivered", "子任务仍活动、未知或没有可核对代码交付", 409)
    if not any(r["id"] == rid and r["kind"] == "code" for r in row["resources"]):
        raise W.Problem("wrong_target", "目标不是该子任务的原始明确资源", 403)
    d = sv._resolve_sid(row["childId"])
    if d and W.RT.status(d)["active"]:
        raise W.Problem("active_child", "子进程尚未确认结束", 409)
    owner = W.RT.read(d) if d else {}
    if row.get("childRunId") and (
        owner.get("run_id") != row["childRunId"] or W.RT.status(d)["state"] == "unknown"
    ):
        raise W.Problem("unknown_child", "子任务运行记录已变化或无法核对，不整合", 409)
    path = Path(row["snapshot"]["path"])
    expected = (W.P.data_root() / "task-worktrees" / task_id).resolve()
    if path.resolve() != expected or path != path.resolve() or not path.is_dir():
        raise W.Problem("source_moved", "子工作树路径已变化或已回收", 409)
    live, ws = target(parent, rid)
    original = next(r for r in row["resources"] if r["id"] == rid)
    if original["path"] != live["path"]:
        raise W.Problem("target_moved", "原资源路径与当前目标不同，需重新派发", 409)
    cws = W.register(str(path))

    def common(w):
        raw = (
            W.safe_git(w, ["rev-parse", "--git-common-dir"])
            .stdout.decode("utf8")
            .strip()
        )
        return (Path(w["path"]) / raw).resolve()

    if common(ws) != common(cws):
        raise W.Problem("wrong_repository", "交付工作树不属于原始仓库", 403)
    return row, cws


# 从受控代码目录读取计划材料并保留文件状态，供三方比较。
def _bytes(ws, name):
    path = W.agent_file_path(ws, name)
    import stat

    cursor = Path(ws["path"])
    for part in Path(name).parts:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise W.Problem("linked_file", "链接路径不能自动整合：" + name, 403)
    from . import baseline

    if baseline.hit("read", path):
        raise W.Problem(
            "protected_resource", "交付包含受保护路径，未读取或整合：" + name, 403
        )
    if not path.exists():
        return None
    import stat

    info = (Path(ws["path"]) / name).lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & 0x400
        or info.st_nlink != 1
    ):
        raise W.Problem("linked_file", "链接文件不能自动整合：" + name, 403)
    if not path.is_file() or path.stat().st_size > W.LIMIT:
        raise W.Problem(
            "unsupported_file", "整合仅支持每文件2MiB以内的UTF-8普通文件：" + name, 413
        )
    raw = path.read_bytes()
    _text(raw, name)
    return raw


def _text(raw, name):
    if raw is None:
        return ""
    try:
        text = raw.decode("utf8")
        if "\0" in text:
            raise ValueError("binary")
        from . import leaks

        if leaks.has_secret(text):
            raise W.Problem(
                "credential_content", "交付含识别出的凭据，未自动传播：" + name, 403
            )
        return text
    except (ValueError, UnicodeError):
        raise W.Problem(
            "unsupported_file",
            "二进制或非UTF-8变更请独立审查处理，未静默跳过：" + name,
            409,
        )


# 将绝对路径转换为 Windows 原生长路径形式，其他平台保持绝对路径。
def native(path):
    path = Path(path).absolute()
    name = str(path)
    return Path(
        ("\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\") else "\\\\?\\" + name)
        if os.name == "nt" and not name.startswith("\\\\?\\")
        else name
    )


def _blob(row, raw):
    if raw is None:
        return None
    sha = W.digest(raw)
    path = native(folder(row["id"]) / "content" / sha[:24])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if W.digest(path.read_bytes()) != sha:
            raise W.Problem("blob_collision", "正文索引前缀冲突，未覆盖已有正文", 409)
    else:
        if (
            sum(p.stat().st_size for p in path.parent.iterdir() if p.is_file())
            + len(raw)
            > 32 * 1024 * 1024
        ):
            raise W.Problem(
                "content_full", "该整合计划保留正文已达32MiB，需建立新预览", 413
            )
        with path.open("xb") as stream:
            stream.write(raw)
    return sha


def _content(row, sha):
    if sha is None:
        return None
    raw = native(folder(row["id"]) / "content" / sha[:24]).read_bytes()
    if len(raw) > W.LIMIT or W.digest(raw) != sha:
        raise W.Problem("content_changed", "整合正文校验失败，未应用", 409)
    return raw


# 按基线判断双方是否各自改变；两侧都改动时才尝试三方合并并记录冲突。
def _merge(row, base, current, child):
    if current == child:
        return current, False
    if current == base:
        return child, False
    if child == base:
        return current, False
    if None in (base, current, child):
        return None, True
    temp = folder(row["id"]) / "merge"
    temp.mkdir(parents=True, exist_ok=True)
    paths = [temp / n for n in ("target", "base", "child")]
    for path, raw in zip(paths, (current, base, child)):
        path.write_bytes(raw)
    result = W.safe_git(
        {"path": str(temp)}, ["merge-file", "-p", *[str(p) for p in paths]], check=False
    )
    if result.returncode not in (0, 1):
        raise W.Problem("merge_failed", "Git技术合并未完成，未修改目标文件", 409)
    return (result.stdout, False) if result.returncode == 0 else (None, True)


# 计划的工作树、分支与版本都要和实际目标一致，避免把旧预览应用到新状态。
def bound_revision(ws, revision, branch, head):
    actual = W.git_status(ws)
    if actual.get("branch") != branch:
        raise W.Problem("branch_changed", "分支已切换，旧预览不能套用到另一个分支", 409)
    if actual.get("head") != head or actual.get("revision") != revision:
        raise W.Problem("revision_conflict", "源或目标版本已推进，请重新核对预览", 409)
    return actual


# 用父目录、子任务结果和保存的隔离基线生成只读预览，返回两侧修订和待确认计划。
def prepare(parent, task_id, rid, sv):
    resource, ws = target(parent, rid)
    child, cws = source(parent, task_id, rid, sv)
    source_idle(cws, sv, child["childId"])
    ts, cs = W.git_status(ws), W.git_status(cws)
    before, cbefore = ts["revision"], cs["revision"]
    baseline = child["snapshot"]["baseline"]
    if len(projects.conversation(parent).get("integrationPlans", [])) >= 100:
        raise W.Problem("plans_full", "整合计划达到100项上限，请先管理历史", 409)
    names = W.safe_git(
        cws, ["diff", "--name-only", "--no-renames", "-z", baseline, "--"]
    ).stdout.split(b"\0")
    names += W.safe_git(
        cws, ["ls-files", "--others", "--exclude-standard", "-z"]
    ).stdout.split(b"\0")
    names = sorted(set(names) - {b""})
    if child.get("workItemId") and any(os.path.normcase(name.decode("utf8")) not in child["writePaths"] for name in names):
        raise W.Problem("subtask_write_scope", "子任务交付含未分配的文件；尚未整合", 403)
    raw_changes = W.safe_git(
        cws, ["diff", "--raw", "--no-abbrev", "--no-renames", "-z", baseline, "--"]
    ).stdout.split(b"\0")
    for index in range(0, len(raw_changes) - 1, 2):
        meta = raw_changes[index].split()
        if not meta:
            continue
        old_mode, new_mode = meta[0][1:], meta[1]
        if (
            new_mode not in {b"000000", b"100644", b"100755"}
            or (new_mode == b"100755" and old_mode != b"100755")
            or old_mode not in {b"000000", new_mode}
            and new_mode != b"000000"
        ):
            raise W.Problem(
                "mode_change",
                "链接/子模块/执行位等模式变化需单独审查，未静默忽略："
                + raw_changes[index + 1].decode("utf8"),
                409,
            )
    if len(names) > 100:
        raise W.Problem(
            "delivery_large", "交付超过100项文件变更，需拆分明确交付范围；尚未应用", 413
        )
    commits = (
        W.safe_git(cws, ["log", "--format=%H", baseline + "..HEAD", "--max-count=201"])
        .stdout.decode("utf8")
        .splitlines()
    )
    if len(commits) > 200:
        raise W.Problem(
            "delivery_large", "交付提交超过200项，需先拆分；未截断来源历史", 413
        )
    row = dict(
        id=uuid.uuid4().hex,
        parentId=parent,
        taskId=task_id,
        resourceId=rid,
        sourceRevision=cbefore,
        targetRevision=before,
        sourceBaseline=baseline,
        sourceHead=cs["head"],
        sourceBranch=cs["branch"],
        targetHead=ts["head"],
        targetBranch=ts["branch"],
        files=[],
        commits=commits,
        state="prepared",
        revision=1,
        applied=[],
        created=time.time(),
        checks="needs_new_target_check",
        notice="以过滤后的dirty基线整合全部提交及未提交变更；不整合Git忽略文件。技术非重叠合并不证明架构兼容；重叠/增删冲突须用户明确取舍。整合后必须对目标新版本重新检查，不能沿用子任务通过。",
    )
    total = 0
    for raw_name in names:
        name = raw_name.decode("utf8")
        W.agent_file_path(ws, name, write=True)
        result = W.safe_git(cws, ["show", baseline + ":" + name], check=False)
        base = result.stdout if result.returncode == 0 else None
        if base is not None and len(base) > W.LIMIT:
            raise W.Problem("large_file", "交付基线文件超过2MiB", 413)
        _text(base, name)
        value, current = _bytes(cws, name), _bytes(ws, name)
        if base == value:
            continue
        total += sum(len(v) for v in (base, value, current) if v is not None)
        if total > LIMIT:
            raise W.Problem("delivery_large", "整合正文总量超过8MiB，请拆分交付", 413)
        merged, conflict = _merge(row, base, current, value)
        if merged is not None and len(merged) > W.LIMIT:
            raise W.Problem(
                "merged_large", "合并后单文件超过2MiB，尚未应用：" + name, 413
            )
        row["files"].append(
            dict(
                path=name,
                baseHash=_blob(row, base),
                currentHash=_blob(row, current),
                sourceHash=_blob(row, value),
                mergedHash=_blob(row, merged),
                conflict=conflict,
                choice=None,
                kind="added"
                if base is None
                else "deleted"
                if value is None
                else "modified",
            )
        )
    bound_revision(ws, before, ts["branch"], ts["head"])
    bound_revision(cws, cbefore, cs["branch"], cs["head"])
    row["conflicts"] = sum(f["conflict"] for f in row["files"])
    row["state"] = "needs_choice" if row["conflicts"] else "prepared"
    save(row)
    W.change_registry(
        lambda data: data["threads"][parent]
        .setdefault("integrationPlans", [])
        .append(row["id"])
    )
    return row


# 重验确认过的计划哈希，再用 `code_service.py` 核对写入授权与源码版本，持久化每项整合结果。
def apply(parent, ident, root, sv, expected_hash=None):
    row = load(parent, ident)
    if expected_hash != row["planHash"]:
        raise W.Problem("plan_changed", "整合预览已变化，需重新核对", 409)
    thread, resource, ws, provenance = C.context_resource(
        root, row["resourceId"], "write"
    )
    if thread["id"] != parent:
        raise W.Problem("wrong_source", "整合计划不属于当前执行", 403)
    with (
        FileLock(W.writer_lock_path(ws["path"])),
        FileLock(folder(ident) / "apply.lock"),
    ):
        row = load(parent, ident)
        if expected_hash != row["planHash"]:
            raise W.Problem("plan_changed", "等待写入锁期间整合预览发生变化", 409)
        if row["state"] == "needs_check":
            return row
        if row["state"] != "prepared" or any(f["conflict"] for f in row["files"]):
            raise W.Problem(
                "not_prepared",
                "整合存在未决定冲突、部分应用或未知动作；需新预览，不重放",
                409,
            )
        C.context_resource(root, row["resourceId"], "write")
        target(parent, row["resourceId"], write=True)
        W.guard_workspace_writer(ws["path"], sv, exclude_thread=parent)
        state = W.git_status(ws)
        if state["protected"] or state["branch"] == "(detached)":
            raise W.Problem(
                "protected_branch", "请先切到独立功能分支，不能整合到保护分支", 403
            )
        child, cws = source(parent, row["taskId"], row["resourceId"], sv)
        source_idle(cws, sv, child["childId"])
        bound_revision(
            ws, row["targetRevision"], row["targetBranch"], row["targetHead"]
        )
        bound_revision(
            cws, row["sourceRevision"], row["sourceBranch"], row["sourceHead"]
        )
        oid = uuid.uuid4().hex
        op = dict(
            id=oid,
            operation="integrate",
            resourceId=row["resourceId"],
            workspaceId=ws["id"],
            source=provenance,
            started=time.time(),
            state="running",
            beforeRevision=row["targetRevision"],
            target=dict(planId=ident,relative_paths=[f['path'] for f in row['files']]),
        )
        C.record(parent, op)
        from . import change_history as history
        try:
            change_id=history.prepare(parent,resource,ws,op)
        except Exception:
            C.finish(parent,oid,"failed",dict(error="整合前无法保留源版本，尚未开始写入"))
            raise
        row.update(state="applying", operationId=oid, source=provenance, changeId=change_id)
        save(row)
        expected = row["targetRevision"]
        try:
            from . import exclusive_file

            for index, f in enumerate(row["files"]):
                from .tools import current_stop

                if (current_stop() or (lambda: False))() or W.read(
                    folder(ident) / "cancel.json"
                ).get("planHash") == row["planHash"]:
                    raise W.Problem(
                        "cancelled", "整合已在文件边界停止；已有改动保留", 409
                    )
                C.context_resource(root, row["resourceId"], "write")
                target(parent, row["resourceId"], write=True)
                bound_revision(ws, expected, row["targetBranch"], row["targetHead"])
                bound_revision(
                    cws, row["sourceRevision"], row["sourceBranch"], row["sourceHead"]
                )
                path = W.agent_file_path(ws, f["path"], write=True)
                from .policy import Policy
                from . import baseline

                allowed, why = Policy("workspace", root=Path(ws["path"])).check(
                    "write_file", {"path": f["path"]}, kind="write"
                )
                if not allowed or baseline.hit("write", path):
                    raise W.Problem("policy_denied", why or "受保护的整合目标", 403)
                before = _bytes(ws, f["path"])
                if (W.digest(before) if before is not None else None) != f[
                    "currentHash"
                ]:
                    raise W.Problem(
                        "revision_conflict", "目标文件已变化，停止整合", 409
                    )
                content = _content(row, f["mergedHash"])
                if before == content:
                    row.setdefault("alreadyPresent", []).append(f["path"])
                    row["applied"].append(f["path"])
                    save(row)
                    continue
                backup = folder(ident) / "backups" / str(index)
                backup.parent.mkdir(parents=True, exist_ok=True)
                if before is not None:
                    backup.write_bytes(before)
                row["inFlight"] = f["path"]
                save(row)
                path.parent.mkdir(parents=True, exist_ok=True)
                exclusive_file.write(
                    path, Path(ws["path"]), content, f["currentHash"], W.LIMIT
                )
                row["applied"].append(f["path"])
                row.pop("inFlight", None)
                expected = W.current_revision(ws)
                row["partialRevision"] = expected
                save(row)
            history.finish(parent,change_id,ws,"observed",[f['path'] for f in row['files']],
                [dict(path=f['path'],before=f['currentHash'],after=f['mergedHash']) for f in row['files'] if f['path'] in row['applied']])
            row.update(
                state="needs_check", resultRevision=expected, finished=time.time()
            )
            save(row)
            C.finish(
                parent,
                oid,
                "completed",
                dict(
                    state="needs_check",
                    projectRevisionAfter=expected,
                    integrationPlanId=ident,
                    changeId=change_id,
                ),
            )
            return row
        except Exception as error:
            try:
                history.finish(parent,change_id,ws,"unknown",[f['path'] for f in row['files']])
            except Exception:
                pass  # Prepared record stays unknown; never replay an integration.
            if (
                isinstance(error, exclusive_file.Conflict)
                and f["currentHash"] is not None
            ):
                row.pop("inFlight", None)
            row.update(
                state="unknown" if row.get("inFlight") else "partial",
                reason=str(error)[:1000],
            )
            save(row)
            C.finish(
                parent,
                oid,
                row["state"],
                dict(
                    error=row["reason"], applied=row["applied"], integrationPlanId=ident,changeId=change_id
                ),
            )
            raise W.Problem(
                row["state"],
                "整合未完整结束；保留备份和已应用清单，不重放。" + row["reason"],
                409,
            )


def resolve(parent, ident, body, sv):
    """Human management only. No model tool is registered for this interface."""
    with FileLock(folder(ident) / "apply.lock"):
        row = load(parent, ident)
        _, ws = target(parent, row["resourceId"])
        _, cws = source(parent, row["taskId"], row["resourceId"], sv)
        if (
            body.get("expectedHash") != row["planHash"]
            or row["state"] != "needs_choice"
        ):
            raise W.Problem("plan_changed", "冲突预览已变化，尚未记录选择", 409)
        bound_revision(
            ws, row["targetRevision"], row["targetBranch"], row["targetHead"]
        )
        bound_revision(
            cws, row["sourceRevision"], row["sourceBranch"], row["sourceHead"]
        )
        f = next(
            (
                f
                for f in row["files"]
                if f["path"] == body.get("path") and f["conflict"]
            ),
            None,
        )
        choice, reason = body.get("choice"), body.get("reason")
        if (
            not f
            or choice not in {"target", "source", "manual"}
            or not isinstance(reason, str)
            or not 1 <= len(reason.strip()) <= 2000
            or row["revision"] >= 100
        ):
            raise W.Problem(
                "invalid_choice", "须选择明确冲突文件、候选及取舍理由（最多100次变更）"
            )
        if choice == "manual":
            text = body.get("text")
            if not isinstance(text, str) or len(text.encode("utf8")) > W.LIMIT:
                raise W.Problem("invalid_content", "手工方案须为2MiB以内文字")
            raw = text.encode("utf8")
            _text(raw, f["path"])
            sha = _blob(row, raw)
        else:
            sha = f["currentHash"] if choice == "target" else f["sourceHash"]
        f.update(
            mergedHash=sha,
            conflict=False,
            choice=dict(
                actor="user",
                choice=choice,
                reason=reason.strip(),
                at=time.time(),
                targetRevision=row["targetRevision"],
                sourceRevision=row["sourceRevision"],
            ),
        )
        row["revision"] += 1
        row["conflicts"] = sum(f["conflict"] for f in row["files"])
        row["state"] = "needs_choice" if row["conflicts"] else "prepared"
        save(row)
        return row


# 以计划文件索引和视图请求原差异页，每次访问重查父会话的资源范围。
def file_page(parent, ident, index, after=0, view="diff"):
    row = load(parent, ident)
    target(parent, row["resourceId"])
    if (
        type(index) is not int
        or not 0 <= index < len(row["files"])
        or type(after) is not int
        or after < 0
        or view not in {"base", "target", "source", "merged", "diff"}
    ):
        raise W.Problem("invalid_cursor", "整合预览页选择无效")
    f = row["files"][index]
    contents = {
        name: _text(_content(row, f[key]), f["path"])
        for name, key in [
            ("base", "baseHash"),
            ("target", "currentHash"),
            ("source", "sourceHash"),
            ("merged", "mergedHash"),
        ]
    }
    text = (
        "".join(
            difflib.unified_diff(
                contents["target"].splitlines(True),
                contents["merged" if not f["conflict"] else "source"].splitlines(True),
                fromfile="target/" + f["path"],
                tofile="proposal/" + f["path"],
            )
        )
        if view == "diff"
        else contents[view]
    )
    if after > len(text):
        raise W.Problem("invalid_cursor", "整合正文偏移超出范围")
    return dict(
        planId=ident,
        planHash=row["planHash"],
        path=f["path"],
        view=view,
        text=text[after : after + 4000],
        next=after + 4000 if after + 4000 < len(text) else None,
        totalChars=len(text),
        notice="完整正文保留，当前仅一页；删除候选正文为空，文件种类另标deleted。",
    )


# 放弃已核对的计划，按计划哈希指定目标并保留原因。
def abort(parent, ident, body, sv):
    row = load(parent, ident)
    target(parent, row["resourceId"])
    reason = body.get("reason")
    if (
        body.get("expectedHash") != row["planHash"]
        or not isinstance(reason, str)
        or not 1 <= len(reason.strip()) <= 2000
    ):
        raise W.Problem("plan_changed", "须核对明确整合预览并填写中止理由", 409)
    request = dict(
        planHash=row["planHash"], reason=reason.strip(), actor="user", at=time.time()
    )
    atomic_json(folder(ident) / "cancel.json", request)
    lock = FileLock(folder(ident) / "apply.lock", timeout=0)
    if not lock.acquire():
        return dict(
            row,
            abortRequested=True,
            notice="已请求在文件边界停止；尚未确认退出。已有写入不会回滚。",
        )
    try:
        row = load(parent, ident)
        if body["expectedHash"] != row["planHash"]:
            raise W.Problem("plan_changed", "整合预览已变化，中止未改写当前计划", 409)
        if row["state"] in {"aborted", "aborted_unknown"}:
            return row
        row.update(
            state="aborted_unknown"
            if row["state"] in {"unknown", "applying"}
            else "aborted",
            abortDecision=request,
        )
        save(row)
        return row
    finally:
        lock.release()


def _archive_files(ws):
    """Capture all ordinary worktree files, including detecting ignored content."""
    result, total, name_bytes, entry_count = {}, 0, 0, 0

    def visit(directory, depth=0):
        nonlocal total, name_bytes, entry_count
        if depth > 32:
            raise W.Problem("archive_large", "目录深度超过32层，未回收", 413)
        with os.scandir(directory) as entries:
            for entry in entries:
                relative = Path(entry.path).relative_to(ws["path"]).as_posix()
                if relative == ".git":
                    continue
                entry_count += 1
                if entry_count > 4000:
                    raise W.Problem(
                        "archive_large", "工作树目录条目超过4000，未回收", 413
                    )
                info = entry.stat(follow_symlinks=False)
                import stat

                if (
                    stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400
                ):
                    raise W.Problem(
                        "linked_file", "回收遇到链接或重解析点，保留工作树", 409
                    )
                if entry.is_dir(follow_symlinks=False):
                    visit(entry.path, depth + 1)
                    continue
                name_bytes += len(relative.encode("utf8"))
                if len(result) >= 2000 or name_bytes > 512 * 1024:
                    raise W.Problem(
                        "archive_large", "工作树超过2000文件或路径索引上限，未回收", 413
                    )
                raw = _bytes(ws, relative)
                total += len(raw)
                if total > 32 * 1024 * 1024:
                    raise W.Problem("archive_large", "工作树归档超过32MiB，未回收", 413)
                if relative == "__harness_delivery_manifest__.json":
                    raise W.Problem(
                        "reserved_name", "工作树包含归档保留名称，未回收", 409
                    )
                result[relative] = raw

    visit(ws["path"])
    if result:
        ignored = W.safe_git(
            ws,
            ["check-ignore", "-z", "--stdin"],
            input=b"".join(name.encode("utf8") + b"\0" for name in result),
            check=False,
        )
        if ignored.returncode not in (0, 1) or ignored.stdout:
            raise W.Problem(
                "ignored_content",
                "存在Git忽略内容或无法核对其来源；未自动清除依赖、环境或其他忽略文件",
                409,
            )
    return result


def source_idle(ws, sv, child_id):
    W.guard_workspace_writer(ws["path"], sv, exclude_thread=child_id)
    # Maintenance must also honor human terminals and the child's own preview.
    # An unknown record cannot attest that descendants or open pipes are gone.
    for path in (W.home() / "processes").glob("*.json"):
        process = W.read(path)
        if process.get("workspaceId") == ws["id"] and process.get("state") in {
            "running",
            "starting",
            "stopping",
            "unknown",
        }:
            raise W.Problem(
                "writer_busy",
                "子工作树仍有应用、检查、终端或待核对进程，不能整合或回收",
                409,
            )


def recycle(parent, task_id, body, sv):
    """Explicit human maintenance; archive and move the intact directory."""
    task = subtasks.load(parent, task_id)
    if body.get("confirm") is not True or any(
        not isinstance(body.get(key), str)
        for key in ("expectedRevision", "expectedBranch", "expectedHead")
    ):
        raise W.Problem("confirmation", "回收须确认明确子工作树及其版本")
    if task.get("cleanup", {}).get("state") == "recycled":
        return dict(task, historical=True)
    if task.get("cleanup", {}).get("state") in {"archiving", "recycling", "unknown"}:
        raise W.Problem(
            "unknown_cleanup", "上次回收未确认，先核对归档及工作树，不重放", 409
        )
    rid = next((r["id"] for r in task["resources"] if r["kind"] == "code"), None)
    _, ws = target(parent, rid, write=True)
    task, cws = source(parent, task_id, rid, sv)
    expected = body["expectedRevision"]
    with FileLock(W.writer_lock_path(cws["path"])):
        task, cws = source(parent, task_id, rid, sv)
        source_idle(cws, sv, task["childId"])
        bound_revision(cws, expected, body["expectedBranch"], body["expectedHead"])
        plans = [
            load(parent, pid)
            for pid in projects.conversation(parent).get("integrationPlans", [])
        ]
        delivered = [
            p
            for p in plans
            if p["taskId"] == task_id
            and p["sourceBranch"] == body["expectedBranch"]
            and p["sourceHead"] == body["expectedHead"]
            and p["sourceRevision"] == expected
            and p.get("resultRevision")
            and len(p["applied"]) == len(p["files"])
            and p["state"] not in {"unknown", "applying", "aborted_unknown"}
        ]
        abandoning = (
            body.get("confirmAbandon") is True
            and isinstance(body.get("reason"), str)
            and 1 <= len(body["reason"].strip()) <= 2000
        )
        if not delivered and not abandoning:
            raise W.Problem(
                "undelivered",
                "交付尚未完整整合；只有人明确确认放弃并填写理由才可回收",
                409,
            )
        state = W.git_status(cws)
        if state["protected"] or not state["branch"].startswith("agent/"):
            raise W.Problem(
                "protected_branch", "仅回收应用隔离工作树的agent/功能分支", 403
            )
        files = _archive_files(cws)
        manifest = {name: W.digest(raw) for name, raw in files.items()}
        bound_revision(cws, expected, body["expectedBranch"], body["expectedHead"])
        cleanup = dict(
            state="archiving",
            sourceRevision=expected,
            sourceBranch=body["expectedBranch"],
            sourceHead=body["expectedHead"],
            actor="user",
            reason=body.get("reason", "已整合交付"),
            abandoned=not bool(delivered),
            planIds=[p["id"] for p in delivered],
            at=time.time(),
        )
        task["cleanup"] = cleanup
        subtasks.save(task)
        archive_dir = W.home() / "deliveries"
        archive_dir.mkdir(exist_ok=True)
        archive = archive_dir / (task_id + ".zip")
        try:
            import zipfile

            with zipfile.ZipFile(
                archive, "x", compression=zipfile.ZIP_DEFLATED
            ) as output:
                for name, raw in files.items():
                    output.writestr(name, raw)
                output.writestr(
                    "__harness_delivery_manifest__.json",
                    json.dumps(
                        dict(taskId=task_id, sourceRevision=expected, files=manifest),
                        ensure_ascii=False,
                    ),
                )
            cleanup.update(
                archive=str(archive),
                archiveHash=W.digest(archive.read_bytes()),
                fileCount=len(files),
                state="recycling",
            )
            subtasks.save(task)
            actual = {name: W.digest(raw) for name, raw in _archive_files(cws).items()}
            if actual != manifest:
                raise W.Problem(
                    "source_changed", "归档期间工作树有新增或变化，保留目录", 409
                )
            bound_revision(cws, expected, body["expectedBranch"], body["expectedHead"])
            source_idle(cws, sv, task["childId"])
            target(parent, rid, write=True)
            # Keep the original index, branch and all dirty files intact.
            # The archive and retained directory preserve the delivery; there
            # is no need to mutate a branch merely to retire a directory.
            bound_revision(cws, expected, body["expectedBranch"], body["expectedHead"])
            # Move the intact directory: git remove can erase ignored content
            # appearing after archiving. https://git-scm.com/docs/git-worktree
            root = Path(cws["path"]).resolve()
            allowed = (W.P.data_root() / "task-worktrees").resolve()
            if (
                root != allowed / task_id
                or not root.is_relative_to(allowed)
                or root == allowed
            ):
                raise W.Problem(
                    "unsafe_cleanup", "回收目标不在明确的应用子工作树目录内", 403
                )
            target(parent, rid, write=True)
            retained_root = (W.P.data_root() / "recycled-worktrees").resolve()
            retained_root.mkdir(parents=True, exist_ok=True)
            retained = retained_root / task_id
            if retained.exists() or retained.resolve().parent != retained_root:
                raise W.Problem(
                    "retained_exists", "保留区目标已存在或已变化；不覆盖、不删除", 409
                )
            cleanup["retainedPath"] = str(retained)
            subtasks.save(task)
            W.safe_git(ws, ["worktree", "move", "--", str(root), str(retained)])
            cleanup.update(
                state="recycled",
                retention="entire_directory_preserved",
                finished=time.time(),
            )
            subtasks.save(task)
            return task
        except Exception as error:
            cleanup.update(
                state="unknown",
                reason="回收未完整确认；归档和隔离暂存区保留：" + str(error)[:1000],
            )
            subtasks.save(task)
            raise W.Problem("unknown_cleanup", cleanup["reason"], 409)


def check(parent, ident, operation_id, sv):
    """Bind a new actual target check. This never means human acceptance."""
    with FileLock(folder(ident) / "apply.lock"):
        row = load(parent, ident)
        _, ws = target(parent, row["resourceId"])
        operation = C.operation(parent, operation_id)
        if (
            not row.get("resultRevision")
            or operation.get("operation") != "check"
            or operation.get("resourceId") != row["resourceId"]
            or operation.get("started", 0) < row.get("finished", float("inf"))
        ):
            raise W.Problem(
                "wrong_check",
                "须选择整合完成后、父任务在目标资源执行的实际检查，不能沿用子任务或旧检查",
                409,
            )
        from . import evidence, delivery_review

        action = operation.get("source", {}).get("actionId")
        fact = evidence.detail(parent, action, sv)
        proof = dict(actionId=action, evidenceRevision=fact["evidenceRevision"])
        status = delivery_review.automatic_result(
            parent,
            proof,
            dict(kind="code", id=row["resourceId"], revision=row["resultRevision"]),
            sv,
        )
        ts = W.git_status(ws)
        if (
            ts["revision"] != row["resultRevision"]
            or ts["branch"] != row["targetBranch"]
            or ts["head"] != row["targetHead"]
        ):
            status = "stale"
        row["targetCheck"] = dict(
            status=status,
            operationId=operation_id,
            proof=proof,
            targetRevision=row["resultRevision"],
            at=time.time(),
            notice="只表示所选命令的实际结果；覆盖充分性、架构质量和用户接受分别核对。",
        )
        save(row)
        return row


# 返回原计划并附上当前源与目标的有效性，供界面确认旧计划是否仍可用。
def view(parent, ident, sv):
    row = dict(load(parent, ident))
    try:
        _, ws = target(parent, row["resourceId"])
        state = W.git_status(ws)
        actual = state["revision"]
        row["currentTargetRevision"] = actual
        row["targetStale"] = (
            actual != row.get("resultRevision", row["targetRevision"])
            or state["branch"] != row["targetBranch"]
            or state["head"] != row["targetHead"]
        )
    except W.Problem as error:
        row.update(
            targetStale=True,
            availability="unavailable",
            availabilityReason=error.message,
        )
    if row.get("targetCheck"):
        from . import delivery_review

        try:
            status = (
                "stale"
                if row["targetStale"]
                else delivery_review.automatic_result(
                    parent,
                    row["targetCheck"]["proof"],
                    dict(
                        kind="code",
                        id=row["resourceId"],
                        revision=row["resultRevision"],
                    ),
                    sv,
                )
            )
            row["targetCheck"] = dict(row["targetCheck"], status=status)
        except (W.Problem, OSError, ValueError, KeyError) as error:
            row["targetCheck"] = dict(
                row["targetCheck"],
                status="unknown",
                reason="原始检查证据无法重新核对：" + str(error)[:500],
            )
    task = subtasks.load(parent, row["taskId"])
    recent = C.operations(task["childId"], limit=20)
    row["sourceChecks"] = [r for r in recent["operations"] if r["operation"] == "check"]
    row["sourceChecksNotice"] = (
        "仅最近20条代码操作中的检查摘要；全部原始检查在子任务审查会话按游标读取。源检查不能证明整合目标通过。"
    )
    row["sourceChecksMore"] = recent["hasMore"]
    row["childId"] = task["childId"]
    row["cleanup"] = task.get("cleanup")
    row["abortRequested"] = bool(W.read(folder(ident) / "cancel.json"))
    if row["state"] == "applying":
        owner = row.get("source", {})
        d = sv._resolve_sid(parent)
        if (
            not d
            or W.RT.read(d).get("run_id") != owner.get("runId")
            or not W.RT.status(d)["active"]
        ):
            row["state"] = "unknown"
            row["reason"] = (
                "整合动作不再有可核对的活动所属轮次；原记录与备份保留，不重放"
            )
    from .evidence_redaction import redact

    return redact(row)


# 核对 `tools.py` 的真实动作与当前父会话范围，再选择准备、读取或提交整合。
def tool(root, operation, **body):
    from .tools import current_call
    from .execution_policy import binding

    source_call, bound = current_call(), binding(root)
    if not source_call or not bound or source_call["sessionId"] != bound["id"]:
        return dict(ok=False, code="wrong_source", error="整合工具须属于实际父会话动作")
    sv = C.RuntimeView()
    parent = bound["id"]
    try:
        allowed = {
            "prepare": {"task_id", "resource_id"},
            "apply": {"plan_id", "expected_hash"},
            "inspect": {"plan_id", "check_operation_id"},
            "file": {"plan_id", "index", "after", "view"},
        }
        if operation not in allowed or set(body) - allowed[operation]:
            raise W.Problem("invalid_arguments", "整合工具包含未声明操作或参数", 403)
        rid = (
            body.get("resource_id")
            if operation == "prepare"
            else load(parent, body.get("plan_id"))["resourceId"]
        )
        C.context_resource(root, rid)
        if operation == "prepare":
            row = prepare(parent, body["task_id"], body["resource_id"], sv)
        elif operation == "apply":
            row = apply(parent, body["plan_id"], root, sv, body["expected_hash"])
        elif operation == "inspect":
            row = (
                check(parent, body["plan_id"], body["check_operation_id"], sv)
                if body.get("check_operation_id")
                else load(parent, body["plan_id"])
            )
        elif operation == "file":
            return dict(
                ok=True,
                **file_page(
                    parent,
                    body["plan_id"],
                    body["index"],
                    body.get("after", 0),
                    body.get("view", "diff"),
                ),
            )
        else:
            raise W.Problem("invalid_operation", "模型没有该整合或裁定操作", 403)
        result = dict(ok=True, plan=view(parent, row["id"], sv))
        if operation == "apply" and row.get("resultRevision") and row.get("changeId"):
            from .subtasks import load as load_subtask

            child = load_subtask(parent, row["taskId"])
            result.update(resourceId=rid, projectRevision=row["resultRevision"], changeId=row["changeId"],
                          workItemId=child.get("workItemId"))
        return result
    except (W.Problem, ValueError, OSError, TimeoutError) as error:
        return dict(
            ok=False,
            code=getattr(error, "code", "integration_failed"),
            error=getattr(error, "message", str(error)),
        )


# 按父会话分发整合列表、计划读取、确认操作和相关检查。
def route(parent, parts, method, body, q, sv):
    projects.conversation(parent)
    if not parts and method == "GET":
        ids = projects.conversation(parent).get("integrationPlans", [])
        before = int((q.get("before") or [str(len(ids) + 1)])[0])
        selected = [
            (index + 1, pid)
            for index, pid in reversed(list(enumerate(ids)))
            if index + 1 < before
        ]
        plans = []
        for position, pid in selected[:10]:
            row = load(parent, pid)
            item = {
                key: row[key]
                for key in (
                    "id",
                    "taskId",
                    "resourceId",
                    "state",
                    "planHash",
                    "conflicts",
                    "created",
                    "targetStale",
                )
                if key in row
            }
            item.update(
                position=position,
                fileCount=len(row["files"]),
                appliedCount=len(row["applied"]),
            )
            plans.append(item)
        return 200, dict(
            plans=plans,
            hasMore=len(selected) > 10,
            nextBefore=selected[9][0] if len(selected) > 10 else None,
        )
    if parts == ["preview"] and method == "POST":
        row = prepare(parent, body.get("taskId"), body.get("resourceId"), sv)
        return 200, view(parent, row["id"], sv)
    if not parts:
        raise W.Problem("not_found", "整合入口不存在", 404)
    ident = parts[0]
    if len(parts) == 1 and method == "GET":
        return 200, view(parent, ident, sv)
    if len(parts) == 3 and parts[1] == "files" and method == "GET":
        return 200, file_page(
            parent,
            ident,
            int(parts[2]),
            int((q.get("after") or ["0"])[0]),
            (q.get("view") or ["diff"])[0],
        )
    if len(parts) == 2 and method == "POST":
        if parts[1] == "resolve":
            row = resolve(parent, ident, body, sv)
        elif parts[1] == "abort":
            row = abort(parent, ident, body, sv)
        elif parts[1] == "check":
            row = check(parent, ident, body.get("operationId"), sv)
        else:
            raise W.Problem("not_found", "整合管理操作不存在", 404)
        return 200, view(parent, row["id"], sv) if not row.get(
            "abortRequested"
        ) else row
    raise W.Problem("not_found", "整合入口不存在", 404)
