"""Bind a scheduled run to a registered checkout and the live conversation API."""
# 主要引用方包括 `scheduled_tasks.py`。

# `scheduled_tasks.py` 用本模块确认计划、登记固定会话和准备某次运行，`projects.py` 用它裁定运行期资源。
from pathlib import Path
import time

from . import permissions, projects, workbench as W
from .conversation_worktrees import common_dir, git_text


# 登记的是规范目录的设备和文件标识。恢复或派发时可发现路径相同、实体已被替换的目录。
def _directory_identity(path):
    try:
        directory = Path(path)
        if directory.resolve(strict=True) != directory or not directory.is_dir():
            raise OSError("directory changed")
        stat = directory.stat()
        return [stat.st_dev, stat.st_ino]
    except (OSError, RuntimeError, ValueError) as error:
        raise W.Problem("schedule_checkout_moved", "计划绑定的目录已移动或不可用", 409) from error


# 将来自计划表单或已确认计划的登记 ID 解析成检出目录。普通会话的目录在会话物化时单独建立。
def _target(selection):
    project_id = selection.get("projectId")
    if project_id is None:
        if selection.get("resourceId") is not None or selection.get("checkoutConversationId") is not None:
            raise W.Problem("invalid_schedule_target", "普通会话使用独立工作目录")
        return dict(projectId=None), None
    if not isinstance(project_id, str):
        raise W.Problem("invalid_schedule_target", "请选择已登记项目")
    registry = W.registry()
    project = registry.get("projects", {}).get(project_id)
    if not project or project.get("kind") == "subtask":
        raise W.Problem("schedule_project_unavailable", "计划绑定的项目不存在或已失效", 409)
    resource_id = selection.get("resourceId", project.get("folderResourceId"))
    owner = selection.get("checkoutConversationId")
    if not isinstance(resource_id, str) or owner is not None and not isinstance(owner, str):
        raise W.Problem("invalid_schedule_target", "请选择项目内已授权的代码目录")
    origin = project.get("resources", {}).get(resource_id)
    if not origin or origin["kind"] != "code" or origin["permission"] != "read":
        raise W.Problem("schedule_access_revoked", "计划绑定的代码目录已失去读取授权", 403)
    origin_path = origin["path"]
    origin_identity = _directory_identity(origin_path)
    selected = dict(projectId=project_id, resourceId=resource_id, checkoutConversationId=owner,
                    originPath=origin_path, originIdentity=origin_identity, path=origin_path,
                    identity=origin_identity, checkoutResourceId=resource_id)
    # 从指定会话复制工作树绑定前，复核项目归属、授权、Git 共同目录和当前分支。`conversation_worktrees.py` 提供 Git 查询。
    if owner is not None:
        thread = registry["threads"].get(owner, {})
        binding = thread.get("codeWorktrees", {}).get(resource_id, {})
        if (thread.get("projectId") != project_id or thread.get("archived") or thread.get("subtaskOf")
                or thread.get("readonlyEvidence") or not binding.get("active")
                or binding.get("projectId") != project_id or binding.get("sourcePath") != origin_path
                or resource_id not in thread.get("resourceIds", [])):
            raise W.Problem("schedule_checkout_changed", "指定会话的工作树已切换或失去授权", 409)
        path = binding["path"]
        try:
            workspace = W.workspace(binding["workspaceId"])
            if workspace["path"] != path or common_dir(workspace) != common_dir(dict(path=origin_path)):
                raise ValueError("checkout identity")
            branch = git_text(workspace, "symbolic-ref", "--short", "HEAD")
        except (W.Problem, OSError, ValueError) as error:
            raise W.Problem("schedule_checkout_changed", "原工作树登记、仓库或分支已失效", 409) from error
        selected.update(path=path, identity=_directory_identity(path), workspaceId=workspace["id"],
                        branch=branch, checkoutResourceId=binding["id"])
    return selected, origin


# 输入来自 `scheduled_tasks.py` 的保存入口，使用 `permissions.py` 的当前版本验证模式及写入、执行上限。
def confirm_plan(target, authority, confirmed):
    if confirmed is not True:
        raise W.Problem("confirmation", "请核对计划说明、检出目录和权限上限后确认", 403)
    if not isinstance(target, dict) or set(target) - {"projectId", "resourceId", "checkoutConversationId"}:
        raise W.Problem("invalid_schedule_target", "使用项目和工作树登记身份选择执行目录")
    if not isinstance(authority, dict) or set(authority) - {"mode", "expectedRevision", "write", "execute"}:
        raise W.Problem("invalid_schedule_authority", "计划需要明确的权限上限")
    profile = permissions.default_profile()
    if type(authority.get("expectedRevision")) is not int or authority["expectedRevision"] != profile["revision"]:
        raise W.Problem("permission_changed", "当前权限已变化，请重新核对计划", 409)
    mode = authority.get("mode")
    if not isinstance(mode, str) or mode not in permissions.MODES:
        raise W.Problem("invalid_schedule_authority", "请选择有效的权限模式")
    if permissions.RANK[mode] > permissions.RANK[profile["mode"]]:
        raise W.Problem("schedule_authority_exceeded", "计划上限不能超过当前权限模式", 403)
    selected, origin = _target(target)
    ceiling = dict(mode=mode, revision=profile["revision"], sandboxMode=(
        "read-only" if profile["sandboxMode"] == "read-only" or not authority.get("write") else "workspace-write"))
    for key in ("write", "execute"):
        wanted = authority.get(key, False)
        if type(wanted) is not bool:
            raise W.Problem("invalid_schedule_authority", "写入和执行上限必须是布尔值")
        if wanted and origin is not None and not origin.get(key):
            raise W.Problem("schedule_authority_exceeded", "目标目录尚无相应的写入或执行授权", 403)
        ceiling[key] = wanted
    return selected, ceiling


# `projects.py` 查询本轮资源时，要求项目、资源、路径和目录实体同时匹配，再将当前授权与已存上限取交集。
def scoped_resource(run, project_id, resource):
    limit = run["resource"]
    if (project_id != limit["projectId"] or resource["id"] != limit["id"]
            or resource["path"] != limit["path"]):
        return None
    try:
        if _directory_identity(resource["path"]) != limit["identity"]:
            return None
    except W.Problem:
        return None
    flags = run["authority"]
    return dict(resource, write=bool(resource.get("write") and flags["write"]),
                execute=bool(resource.get("execute") and flags["execute"]))


# 根据计划 ID 预留一条稳定的会话身份。普通会话同时用 `work_folders.py` 确定将来建立的工作目录。
def reserve(plan, request_id=None):
    """Record the identities before either physical directory is created."""
    assert "conversationId" not in plan or plan["conversationId"] is None
    folder = None
    if plan["target"]["projectId"] is None:
        from .work_folders import settings

        folder = str(Path(settings()["conversations"]) / ("定时任务-" + plan["id"]))
    return dict(plan, conversationId="app-schedule-" + plan["id"],
                _binding=dict(folder=folder, requestId=request_id))


# 返回会话登记所需字段。项目资源 ID 用于声明范围，`scheduledRun.resource` 另外固定实际检出的 ID、路径和目录身份。
def _scope_fields(plan, target, authority, resource, run_id=None):
    project_id = target["projectId"]
    if project_id:
        actual = dict(id=target["checkoutResourceId"], projectId=project_id, path=target["path"],
                      identity=target["identity"])
        resource_ids = [target["resourceId"]]
    else:
        actual = dict(id=resource["id"], projectId=None, path=resource["path"],
                      identity=_directory_identity(resource["path"]))
        resource_ids = [resource["id"]]
    return dict(resourceIds=resource_ids,
                scheduledRun=dict(id=run_id, planId=plan["id"], authority=authority, resource=actual))


# 复制仍生效且路径吻合的工作树登记，供定时会话解析选定资源。
def _with_checkout(data, fields, target):
    checkouts = {}
    if owner := target.get("checkoutConversationId"):
        resource_id = target["resourceId"]
        checkout = data["threads"].get(owner, {}).get("codeWorktrees", {}).get(resource_id, {})
        if checkout.get("active") and checkout.get("path") == target["path"]:
            checkouts[resource_id] = dict(checkout)
    return dict(fields, codeWorktrees=checkouts)


# `scheduled_tasks.py` 在保存或恢复计划时调用。核对已有会话的归属，或由 `workbench.py` 创建预留 ID 对应的会话。
# 使用计划中已保留的会话 ID。重试先核对原会话的计划和项目归属，再决定是否需要创建。
def materialize(plan, sv):
    ident = plan["conversationId"]
    target = plan["target"]
    data = W.registry()
    thread = data["threads"].get(ident)
    if thread:
        if (thread.get("scheduledPlanId") != plan["id"] or thread.get("projectId") != target["projectId"]
                or not sv._resolve_sid(ident)):
            raise W.Problem("schedule_conversation_unavailable", "原定时会话身份无法核对", 409)
        return
    folder = plan["_binding"]["folder"]
    directory = W.home() / "conversations" / ("scheduled-" + plan["id"])
    own = None
    try:
        if folder:
            path = Path(folder)
            if path.resolve() != path:
                raise W.Problem("schedule_checkout_moved", "定时会话的工作目录已移动", 409)
            path.mkdir(parents=True, exist_ok=True)
            own = dict(id=plan["id"], name=path.name, path=str(path), kind="code",
                       permission="read", permissionAt=time.time(), permissionSource="user",
                       write=True, execute=True)
        if directory.resolve() != directory:
            raise W.Problem("schedule_conversation_unavailable", "原定时会话目录已移动", 409)
        directory.mkdir(parents=True, exist_ok=True)
        w = W.register(str(directory), kind="conversation")
        fields = dict(id=ident, projectless=True, projectId=target["projectId"], scheduledPlanId=plan["id"],
                      **_scope_fields(plan, target, plan["authority"], own))
        if own:
            fields["workResource"] = own
        code, result = W._create_thread(w, plan["title"], "discussion", sv,
                                        binding=_with_checkout(data, fields, target))
        if code != 200:
            raise W.Problem(result.get("code", "schedule_conversation_failed"),
                            result.get("error", "无法建立定时会话"), code)
    except (OSError, RuntimeError, ValueError) as error:
        raise W.Problem("schedule_conversation_failed", "无法建立定时会话，请核对工作目录", 409) from error


# `record` 是本次运行的冻结计划。重新核对目录及会话，按实时权限收紧已批准上限，最后返回发送参数与实际执行范围。
def prepare(record, ident, sv):
    """Recheck the plan before attaching the scope of this particular run."""
    # 本次运行使用计划确认的检出身份，授权还受会话与默认配置的当前上限约束。
    plan = record["plan"]
    signed = plan["target"]
    current, origin = _target({key: signed[key] for key in ("projectId", "resourceId", "checkoutConversationId")
                               if key in signed})
    if signed != current:
        raise W.Problem("schedule_checkout_changed", "当前检出目录身份与计划确认时不同", 409)
    saved = plan["authority"]
    profile = permissions.default_profile()
    authority = dict(saved, mode=permissions._ceiling(saved, profile))
    if origin is not None:
        for key in ("write", "execute"):
            authority[key] = bool(saved[key] and origin.get(key))
    project_id = current["projectId"]
    thread = W.registry()["threads"].get(ident, {})
    directory = sv._resolve_sid(ident)
    if (thread.get("scheduledPlanId") != plan["id"] or thread.get("projectId") != project_id
            or thread.get("archived") or not directory):
        raise W.Problem("schedule_conversation_unavailable", "请恢复计划绑定的定时会话", 409)
    active_process = getattr(sv, "_active_proc", None)
    if active_process and active_process(directory) is not None:
        raise W.Problem("active_run", "定时会话仍有运行正在执行", 409)
    own = thread.get("workResource")
    if project_id is None and (not own or own.get("permission") != "read"
                               or not scoped_resource(thread["scheduledRun"], None, own)):
        raise W.Problem("schedule_access_revoked", "定时会话工作目录已变化或撤权", 403)
    fields = _scope_fields(plan, current, authority, own, record["id"])
    actual = fields["scheduledRun"]["resource"]

    # 通过 `workbench.py` 的登记更新绑定本轮运行。发送前再以 `projects.py` 读出的实际范围校验读取权限。
    def attach(data):
        thread = data["threads"][ident]
        thread.update(_with_checkout(data, fields, current))

    W.change_registry(attach)
    scope = projects.scope(ident)
    if not any(resource["id"] == actual["id"] and resource["permission"] == "read" for resource in scope["resources"]):
        raise W.Problem("schedule_access_revoked", "会话执行范围无法核对，未发送", 403)
    route = f"/api/v2/conversations/threads/{ident}/send"
    body = dict(text=plan["instructions"], crew="l0", expectedScopeRevision=scope["revision"])
    execution = dict(authority=authority, projectId=project_id, path=actual["path"], resourceId=actual["id"],
                     checkoutConversationId=current.get("checkoutConversationId"))
    return ident, route, body, execution
