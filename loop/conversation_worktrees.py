"""User-controlled, per-conversation checkout binding and local environment setup."""

# 主要引用方包括 `workbench.py`。

from contextlib import ExitStack
import json
import os
from pathlib import Path

from . import projects, workbench as W
from .filelock import FileLock


def selected(ident, origin):
    thread = projects.conversation(ident)
    if not thread.get("projectId") or thread.get("subtaskOf") or thread.get("readonlyEvidence"):
        raise W.Problem("workspace_denied", "此对话没有可切换的代码工作区", 403)
    resource = projects.retained_code(ident, origin)
    if not resource or resource.get("worktreeOf"):
        raise W.Problem("workspace_denied", "请选择此对话已授权的原代码工作区", 403)
    if not Path(resource["path"]).is_dir() or str(Path(resource["path"]).resolve()) != resource["path"]:
        raise W.Problem("workspace_moved", "原工作区目录已移动", 409)
    return thread, resource


def environment(resource):
    data = W.registry().get("localEnvironments", {}).get(resource["path"], {})
    return dict(setup=data.get("setup", ""), actions=data.get("actions", []), revision=data.get("revision", "empty"))


def argv(script):
    return (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
            if os.name == "nt" else ["/bin/sh", "-c", script])


def current_workspace(thread, resource):
    binding = thread.get("codeWorktrees", {}).get(resource["id"], {})
    if binding.get("active"):
        workspace = W.workspace(binding["workspaceId"])
        if str(Path(workspace["path"]).resolve()) != binding["path"]:
            raise W.Problem("workspace_moved", "工作树目录已移动", 409)
        return workspace
    return W.register(resource["path"])


def environment_route(thread, resource, method, body):
    config = environment(resource)
    if method == "GET":
        binding = thread.get("codeWorktrees", {}).get(resource["id"], {})
        path = binding["path"] if binding.get("active") else resource["path"]
        return 200, dict(config, path=path)
    if method != "POST" or body.get("confirm") is not True:
        raise W.Problem("confirmation", "请核对本地环境操作后确认")
    if config["revision"] != body.get("revision"):
        raise W.Problem("environment_changed", "本地环境已修改，请重新核对", 409)
    if body.get("operation") == "run":
        index = body.get("index")
        if type(index) is not int or not 0 <= index < len(config["actions"]):
            raise W.Problem("invalid_action", "找不到此操作")
        return 200, W.launch(current_workspace(thread, resource), argv(config["actions"][index]["script"]))
    setup, actions = body.get("setup"), body.get("actions")
    if (body.get("operation") != "save" or not isinstance(setup, str) or len(setup) > 4000 or "\0" in setup
            or not isinstance(actions, list) or len(actions) > 5
            or any(not isinstance(action, dict) or set(action) != {"name", "script"}
                   or not isinstance(action["name"], str) or not 1 <= len(action["name"].strip()) <= 40
                   or not isinstance(action["script"], str) or not 1 <= len(action["script"].strip()) <= 4000
                   or "\0" in action["script"] for action in actions)):
        raise W.Problem("invalid_environment", "初始化脚本或常用操作无效")
    value = dict(setup=setup.strip(), actions=actions)
    value["revision"] = W.digest(json.dumps(value, sort_keys=True).encode())
    W.change_registry(lambda data: data.setdefault("localEnvironments", {}).__setitem__(resource["path"], value))
    return 200, dict(value, path=current_workspace(thread, resource)["path"])


def git_text(ws, *args):
    return W.safe_git(ws, list(args)).stdout.decode("utf-8", "replace").strip()


def common_dir(ws):
    return Path(git_text(ws, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


def contains(ws, newer, older):
    result = W.safe_git(ws, ["merge-base", "--is-ancestor", older, newer], check=False)
    if result.returncode not in (0, 1):
        raise W.Problem("git_history", "无法核对两侧提交关系", 409)
    return result.returncode == 0


def overlaps_ignored(target, before, after):
    def names(args):
        raw = W.safe_git(target, args).stdout
        return {os.path.normcase(os.fsdecode(name)).replace(os.sep, "/").rstrip("/")
                for name in raw.split(b"\0") if name}

    def parents(path):
        while "/" in path:
            path = path.rsplit("/", 1)[0]
            yield path

    incoming = names(["diff", "--name-only", "--no-renames", "--diff-filter=AT", "-z", before, after, "--"])
    if not incoming:
        return False
    ignored = names(["ls-files", "--others", "--ignored", "--exclude-standard", "-z"])
    return bool(incoming & ignored
                or any(parent in ignored for path in incoming for parent in parents(path))
                or any(parent in incoming for path in ignored for parent in parents(path)))


def idle(thread, sv):
    from . import delegations, subtasks

    if thread.get("archived"):
        raise W.Problem("archived_conversation", "请先恢复已归档对话", 409)
    subtasks.ensure_send(thread["id"])
    delegations.ensure_send_allowed(thread["id"])
    directory = sv._resolve_sid(thread["id"])
    if directory:
        state = W.RT.status(directory)
        if state["active"] or state.get("pendingApproval") or W.pending_approval(directory):
            raise W.Problem("active_run", "请先结束本轮执行或处理审批", 409)


def checkout_location(thread, resource):
    key = W.digest((thread["id"] + ":" + resource["id"]).encode())[:24]
    data = W.P.data_root().resolve()
    root = data.with_name(data.name + "-conversation-worktrees")
    if root.resolve() != root:
        raise W.Problem("workspace_moved", "对话工作树根目录已重定向", 409)
    return root / ("chat-" + key), "agent/chat-" + key


def persistent_setup_completed(process):
    return bool(process and process.get("state") == "completed" and process.get("exitCode") == 0
                and process.get("isolation") == "host-full-access")


def handoff_plan(thread, resource, sv):
    origin = resource["id"]
    binding = thread.get("codeWorktrees", {}).get(origin, {})
    local = W.register(resource["path"])
    if W.writer_root(resource["path"]) != Path(resource["path"]):
        raise W.Problem("repository_root_required", "请选择 Git 工作树根目录，子目录资料不参与切换", 403)
    destination, branch = checkout_location(thread, resource)
    isolated = dict(path=str(destination)) if destination.exists() else None
    if isolated and (destination.resolve() != destination or common_dir(isolated) != common_dir(local)
                     or (binding.get("path") != str(destination) and git_text(isolated, "branch", "--show-current") != branch)):
        raise W.Problem("workspace_changed", "工作树目录或分支与此对话的记录不一致", 409)
    returning = bool(binding.get("active"))
    if returning and not isolated:
        raise W.Problem("workspace_moved", "对话关联的工作树目录已移动", 409)
    source, target = (isolated, local) if returning else (local, isolated)
    source_status = W.git_status(source)
    target_status = W.git_status(target) if target else None
    config = environment(resource)
    setup = binding.get("setup") or {}
    process = W.process_record(setup["id"]) if setup.get("id") else None
    # Previously a successful check in a temporary copy could set used=True.
    legacy_copy = (process and process.get("exitCode") == 0
                   and not persistent_setup_completed(process) and not binding.get("setupSkipped"))
    if setup.get("revision") != config["revision"] and (not process or process["state"] != "running"):
        process = None
    need_setup = bool(not returning and (not binding.get("used") or legacy_copy) and config["setup"])
    setup_ready = persistent_setup_completed(process)
    blocked = []
    try:
        idle(thread, sv)
    except W.Problem as error:
        blocked.append(error.message)
    if not source_status.get("head") or (target_status and not target_status.get("head")):
        blocked.append("工作树切换需要已有 Git 提交")
    for checkout, status, label in ((source, source_status, "源"), (target, target_status, "目标")):
        if status and status.get("available") and W.safe_git(checkout, ["status", "--porcelain", "-z"]).stdout:
            blocked.append(label + "工作区有未提交改动")
        if checkout:
            try:
                W.guard_workspace_writer(checkout["path"], sv, exclude_thread=thread["id"])
            except W.Problem as error:
                blocked.append(label + "工作区：" + error.message)
    if not isolated and source_status.get("head") and W.safe_git(local, ["show-ref", "--verify", "--quiet", "refs/heads/" + branch], check=False).returncode == 0:
        blocked.append("该对话的工作树分支已存在，请先核对分支占用或找回工作树目录")
    if process and process["state"] == "running":
        blocked.append("初始化正在运行")
    fast_forward = False
    commits = []
    if not blocked and target_status and source_status["head"] != target_status["head"]:
        old, new = target_status["head"], source_status["head"]
        if contains(source, new, old):
            if target_status["protected"] or target_status["branch"] == "(detached)":
                blocked.append("目标是保护分支，请先切到可接收提交的功能分支")
            elif overlaps_ignored(target, old, new):
                blocked.append("目标工作区有会被覆盖的忽略文件，请先在本地保留并处理冲突")
            else:
                fast_forward = True
                commits = git_text(source, "log", "-20", "--format=%h %s", old + ".." + new).splitlines()
        elif not contains(source, old, new):
            blocked.append("两个检出已分叉，请先在 Git 中整合提交")
    token = W.digest(json.dumps([thread["projectId"], origin, resource["permissionAt"] if "permissionAt" in resource else 0,
                                 returning, source_status.get("branch"), source_status["revision"],
                                 target_status and target_status["branch"], target_status and target_status["revision"],
                                 config["revision"], process and [process["id"], process["state"]]], sort_keys=True).encode())
    view = dict(token=token, returning=returning, source=source["path"], target=local["path"] if returning else str(destination),
                head=source_status.get("head"), blocked=list(dict.fromkeys(blocked)), commits=commits, needSetup=need_setup,
                setup=config["setup"] if need_setup else "", setupReady=setup_ready,
                setupRevision=config["revision"], process=process,
                sourceFiles=source_status["files"][:30], targetFiles=(target_status or {}).get("files", [])[:30],
                targetHead=(target_status or {}).get("head"))
    return view, local, source, target, branch, fast_forward


def handoff(thread, resource, method, body, sv):
    if method == "GET":
        return 200, handoff_plan(thread, resource, sv)[0]
    if method != "POST" or body.get("confirm") is not True:
        raise W.Problem("confirmation", "请核对工作树预览后确认")
    destination, _ = checkout_location(thread, resource)
    lock_ids = {W.digest(os.path.normcase(str(Path(path))).encode())[:20]
                for path in (resource["path"], str(destination))}
    with ExitStack() as locks:
        for lock_id in sorted(lock_ids):
            locks.enter_context(FileLock(W.home() / (lock_id + ".git.lock")))
        view, local, source, target, branch, fast_forward = handoff_plan(thread, resource, sv)
        if view["token"] != body.get("token") or view["blocked"]:
            raise W.Problem("handoff_changed", "；".join(view["blocked"]) or "预览已变化，请重新核对", 409)
        if not target:
            destination.parent.mkdir(exist_ok=True)
            W.safe_git(local, ["worktree", "add", "-b", branch, str(destination), view["head"]])
            target = W.register(str(destination), "worktree", local["id"])
        if fast_forward:
            W.safe_git(target, ["merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", view["head"]])
            from .project_activity import record_checkout_handoff

            record_checkout_handoff(thread, target, view["targetHead"], view["head"])
        binding = dict(thread.get("codeWorktrees", {}).get(resource["id"], {}))
        if not view["returning"]:
            isolated = W.register(target["path"], "worktree", local["id"])
            binding.update(id=binding.get("id", W.digest(str(destination).encode())[:32]), path=isolated["path"],
                           workspaceId=isolated["id"], sourcePath=resource["path"], projectId=thread["projectId"])
        def store():
            W.change_registry(lambda data: data["threads"][thread["id"]].setdefault("codeWorktrees", {}).__setitem__(resource["id"], binding))

        skip_setup = view["needSetup"] and body.get("skipSetup") is True
        if view["needSetup"] and not view["setupReady"] and not skip_setup:
            store()
            run = W.launch(isolated, argv(view["setup"]))
            binding["setup"] = dict(id=run["id"], revision=view["setupRevision"])
            store()
            return 200, dict(state="setup", process=run)
        if view["needSetup"]:
            binding["setupSkipped"] = skip_setup
        binding.update(active=not view["returning"], used=True)
        store()
        return 200, dict(state="switched", path=target["path"])


def route(ident, operation, method, query, body, sv):
    origin = (query.get("resource") or [None])[0] if method == "GET" else body.get("resourceId")
    if not isinstance(origin, str):
        raise W.Problem("invalid_resource", "请选择代码工作区")
    thread, resource = selected(ident, origin)
    if operation == "local-environment":
        return environment_route(thread, resource, method, body)
    return handoff(thread, resource, method, body, sv)
