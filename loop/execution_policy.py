"""Next-turn preferences and immutable, bounded execution snapshots.

Route, resource grants and logical model-call budget are independent. This is
not a currency cap: transport retries/fallbacks may incur additional charges.
"""

# 主要引用方包括 `workbench.py` 和 `tools.py`。

import json
from pathlib import Path
import time
from . import workbench as W, projects
from .filelock import FileLock, atomic_json

ROUTES = [
    dict(
        id="l0",
        name="直接处理",
        planner=False,
        reviewer=False,
        mechanism="原生 L0",
    ),
]


ROLE_LAYOUT_VERSION = 2
MIN_CONTEXT_LIMIT = 128 * 1024


# 已存配置的额度仍可供新任务使用。旧档位只留在原有运行记录中。
def saved_config(thread):
    previous=dict(thread.get("execution", defaults()))
    limit=previous.get('contextLimit',0)
    if type(limit) is int and 0<limit<MIN_CONTEXT_LIMIT:previous['contextLimit']=MIN_CONTEXT_LIMIT
    return validate(dict(previous, crew="l0", purpose="execute", subtaskId=None))


# 按执行模式定义返回声明的角色组合，执行者始终存在。
def roles_for_crew(crew):
    if crew not in ("l0", "l4"):
        raise W.Problem("invalid_execution", "未知执行方式")
    return dict(planner=crew == "l4", executor=True, reviewer=crew == "l4")


class BudgetExhausted(RuntimeError):
    pass


# 预算为复核角色保留的份额取决于角色是否启用以及本轮调用总量。
def reviewer_reserve(config):
    roles = roles_for_crew(config["crew"])
    if not roles["reviewer"]:
        return 0
    calls = config["modelCalls"]
    return max(0, min(6, calls // 2, calls - 1 - int(roles["planner"])))


# 创建未单独设置时使用的默认 L0 配置。零预算项沿运行默认解释。
def defaults():
    return dict(
        revision=1,
        crew="l0",
        maxTurns=0,
        modelCalls=0,
        charter="off",
        contextLimit=0,
        parallelRoles=False,
        backgroundPlanner=False,
        workerContinuation="parent-run",
        purpose="execute",
        delegationId=None,
        subtaskId=None,
    )


# UI 偏好不是任意角色组合授权；运行入口还要验证真实子任务/委托身份，并收束不适用的并行开关。
def runner_config(value, root=None):
    value = validate(value)
    if value.get("subtaskId"):
        from . import subtasks

        thread = binding(root) if root else None
        row = subtasks.validate_child(thread["id"]) if thread else None
        if (
            not row
            or row["id"] != value["subtaskId"]
            or value != subtasks.execution_config(row)
        ):
            raise W.Problem("invalid_child", "子任务配置不属于实际派发", 403)
    if value["crew"] == "l4":
        if not value.get("delegationId") or root is None:
            raise W.Problem(
                "delegation_required",
                "L4需要人锁约定和持续任务容器，不能作为普通run-task运行",
                409,
            )
        from . import delegations

        thread = binding(root)
        if not thread:
            raise W.Problem("wrong_source", "持续委托不属于私有会话", 403)
        delegations.authorize_config(value, thread["id"])
    return value


# 核对执行方式、预算与可交付的运行字段，只有通过校验的设置才会进入会话。
def validate(value):
    if not isinstance(value, dict) or set(value) - set(defaults()):
        raise W.Problem(
            "invalid_execution", "只能设置执行方式、轮次、调用预算及总纲注入"
        )
    value = dict(defaults(), **value)
    # Historical run records retain their original config for review. Future
    # execution never enables the retired preset charter, regardless of crew.
    value['charter'] = 'off'
    if value['workerContinuation'] not in ('parent-run', 'manual', 'wake'):
        raise W.Problem('invalid_execution', '未知的委派存续方式')
    if type(value["parallelRoles"]) is not bool:
        raise W.Problem("invalid_execution", "并行角色设置必须是布尔值")
    if type(value["backgroundPlanner"]) is not bool:
        raise W.Problem("invalid_execution", "后台规划设置必须是布尔值")
    if value.get("subtaskId") is not None and (
        not isinstance(value["subtaskId"], str)
        or len(value["subtaskId"]) != 32
        or any(c not in "0123456789abcdef" for c in value["subtaskId"])
    ):
        raise W.Problem("invalid_child", "子任务编号无效")
    if (
        not isinstance(value["purpose"], str)
        or value["purpose"] not in {"execute", "delegation_draft"}
        or (
            value["delegationId"] is not None
            and (
                not isinstance(value["delegationId"], str)
                or len(value["delegationId"]) != 32
            )
        )
    ):
        raise W.Problem("invalid_execution", "运行用途或委托标识无效")
    if (
        not isinstance(value["crew"], str)
        or not isinstance(value["charter"], str)
        or value["crew"] not in {"l0", "l4"}
        or value["charter"]
        not in {
            "legacy",
            "off",
        }
    ):
        raise W.Problem("invalid_execution", "未知执行方式或总纲策略")
    for key, low, high in [
        ("contextLimit", 0, 2000000),
        ("maxTurns", 0, 1000000),
        ("modelCalls", 0, 1000000),
        ("revision", 1, 1000000000),
    ]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise W.Problem(
                "invalid_execution", f"{key} 必须为 {low}—{high} 的整数；调用和轮数的 0 表示不限"
            )
    if 0<value['contextLimit']<MIN_CONTEXT_LIMIT:
        raise W.Problem('invalid_execution','手动上下文上限最低128K（131072 token）；0表示按模型自动')
    value.update(parallelRoles=False, backgroundPlanner=False)
    if value['crew'] == 'l0':
        value['delegationId'] = None
    return value


def require_conversation_crew(crew):
    if crew == "l4":
        raise W.Problem("delegation_required", "请在会话的持续委托面板确认约定后启动", 409)
    if crew != "l0":
        raise W.Problem("invalid_execution", "旧执行档位已退役，请使用 L0")


# 配置修订只作用下一轮，用 expectedRevision 防止并发覆盖；当前轮次使用独立的已保存运行快照。
def configure(ident, body):
    def change(data):
        thread = data["threads"][ident]
        if thread.get("readonlyEvidence"):
            raise W.Problem("readonly_question", "证据追问固定直接处理和只读", 403)
        old = saved_config(thread)
        if body.get("expectedRevision") != old["revision"]:
            raise W.Problem("execution_changed", "下一轮设置已变化，请刷新后核对", 409)
        patch = {
            key: value
            for key, value in body.items()
            if key not in {"expectedRevision", "_request_id"}
        }
        if set(patch) & {"revision", "purpose", "delegationId", "subtaskId"}:
            raise W.Problem("invalid_execution", "设置版本由系统维护")
        require_conversation_crew(patch.get("crew", "l0"))
        value = validate(dict(old, **patch))
        value["revision"] += 1
        thread["execution"] = value
        thread["executionRolesVersion"] = ROLE_LAYOUT_VERSION
        return value

    projects.conversation(ident)
    return W.change_registry(change)


# 从会话下一轮设置与发送条件生成此次运行配置。受控子任务需由其父方调度。
def for_send(ident, body):
    thread = projects.conversation(ident)
    if thread.get("subtaskOf"):
        raise W.Problem("managed_child", "子任务由父任务调度，不能普通发送", 403)
    value = saved_config(thread)
    if (
        body.get("expectedExecutionRevision") is not None
        and body["expectedExecutionRevision"] != value["revision"]
    ):
        raise W.Problem("execution_changed", "执行设置已变化，请查看后重新发送", 409)
    require_conversation_crew(body.get('crew', 'l0'))
    if body.get("max_turns") is not None:
        value = validate(dict(value, maxTurns=body["max_turns"]))
    if body.get("purpose") == "delegation_draft":
        value.update(
            crew="l0",
            purpose="delegation_draft",
            charter="off",
            modelCalls=min(value["modelCalls"], 6),
            delegationId=None,
        )
    elif body.get("purpose") not in (None, "execute"):
        raise W.Problem("invalid_execution", "未知发送用途")
    return value


# 按实际检出目录在宿主登记中查会话绑定，提供给工具的作用域核对。
def binding(root):
    root = Path(root).resolve()
    data = W.registry()
    for thread in data["threads"].values():
        if (
            thread.get("projectless")
            and Path(
                data["workspaces"].get(thread["workspaceId"], {}).get("path", "")
            ).resolve()
            == root
        ):
            return projects.with_resource_snapshot(thread)
    return None


def draft_session(directory):
    """Hooks use the actual run snapshot, not mutable next-turn preferences."""
    directory = Path(directory)
    if W.registry()["threads"].get(directory.name, {}).get("subtaskOf"):
        return True
    run_id = W.RT.read(directory).get("run_id")
    if (
        not isinstance(run_id, str)
        or len(run_id) != 32
        or any(c not in "0123456789abcdef" for c in run_id)
    ):
        return False
    target = directory / "execution-runs" / (run_id + ".json")
    try:
        if target.stat().st_size > 32768:
            return True
        row = json.loads(target.read_text(encoding="utf8"))
        return (
            row.get("runId") != run_id
            or row["config"].get("purpose") == "delegation_draft"
        )
    except (OSError, ValueError, KeyError):
        return bool(W.registry()["threads"].get(directory.name, {}).get("projectless"))


def resume_root(directory):
    """Resolve private CLI continuation before any config/provider is created."""
    directory = Path(directory).resolve()
    thread = W.registry()["threads"].get(directory.name)
    if not thread or not thread.get("projectless"):
        return None
    if directory != (W.P.data_root() / "runs" / thread["id"]).resolve():
        raise W.Problem("wrong_source", "私有会话目录与登记身份不一致", 403)
    return Path(W.workspace(thread["workspaceId"])["path"]).resolve()


# 按会话和运行 ID 定位本轮的持久预算事实。
def path(session, run_id=None):
    run_id = run_id or session.run_id
    if (
        not isinstance(run_id, str)
        or len(run_id) != 32
        or any(c not in "0123456789abcdef" for c in run_id)
    ):
        raise W.Problem("unknown_owner", "本轮运行身份无法核对，不能分配预算", 409)
    return Path(session.dir) / "execution-runs" / (run_id + ".json")


def checkpoint_config(directory, checkpoint):
    if not isinstance(checkpoint, dict) or not checkpoint:
        return {}
    directory = Path(directory)
    run_id = checkpoint.get('run_id')
    if isinstance(run_id, str) and len(run_id) == 32 and all(c in '0123456789abcdef' for c in run_id):
        target = directory / 'execution-runs' / (run_id + '.json')
        try:
            row = W.read(target) if target.stat().st_size <= 32768 else {}
        except OSError:
            row = {}
        config = row.get('config') if isinstance(row, dict) and row.get('runId') == run_id else None
        if isinstance(config, dict) and config.get('crew'):
            return config
    # Old unmanaged runs only recorded this mode marker. It can block replay, never authorize it.
    try:
        with (directory / 'crew.txt').open('rb') as stream:
            marker = stream.read(8).strip()
        return {'crew': marker.decode('ascii')} if marker in (b'l1', b'l2', b'l3', b'l4') else {}
    except OSError:
        return {}


def legacy_checkpoint(config):
    return config.get('crew') in ('l1', 'l2', 'l3', 'l4') or config.get('purpose') == 'delegation_draft'


def retired_checkpoint(config):
    crew = config.get("crew")
    return crew in ("l1", "l2", "l3") or (crew == "l4" and not config.get("delegationId"))


# 先由 `permissions.py` 冻结已绑定会话的执行权限，再持久化本轮预算与状态。
def begin(session, config, root):
    from . import permissions
    permissions.freeze(session.run_id, Path(session.dir).name)
    if not binding(root):
        return None
    config = validate(config)
    from . import context_inspection
    context_inspection.begin(session, root, roles_for_crew(config['crew']))
    if config.get("delegationId"):
        from . import delegations

        delegations.reserve_run(session, config)
    row = dict(
        runId=session.run_id,
        config=config,
        contextPreferences=getattr(session,"context_preferences",{}),
        usedCalls=0,
        byRole={},
        reviewerReserve=reviewer_reserve(config),
        state="available",
        at=time.time(),
        policy="resource_capabilities",
        charterEffective="off",
    )
    target = path(session)
    # 同一运行的预算只创建一次；重启/恢复不能借重新 begin 清零已经消耗的调用额度。
    with FileLock(target.with_suffix(".lock")):
        if target.exists():
            raise W.Problem("duplicate_run", "运行预算已建立，不重置已用额度", 409)
        atomic_json(target, row)
    session.context_user_limit = config.get("contextLimit",0)
    session.execution_budget = target
    if config.get("subtaskId"):
        from . import subtasks

        child = binding(root)
        task = subtasks.validate_child(child["id"])
        session.parent_budget = (task["rootBudget"], task["id"])
    session.emit("execution_config", **row)
    return row


# 在模型调用前登记主运行及相应角色预算；委派共享额度由 `parallel_budget.py` 确认。
def consume(session, role):
    target = getattr(session, "execution_budget", None)
    if not target:
        return
    from . import parallel_budget as B

    parent = getattr(session, "parent_budget", None)
    # 子任务先计入根预算，再检查自身预算；根额度约束所有角色/子任务，不能靠拆任务绕开。
    if parent:
        B.consume(parent[0], role, parent[1])
    if not parent:
        row = B.consume(target, role,
                        review_unit=role == "reviewer" and getattr(session, "review_unit", False),
                        defect_retest=role == "reviewer" and getattr(session, "defect_retest", False))
        session.emit(
            "model_call_budget",
            role=role,
            used=row["usedCalls"],
            limit=row["config"]["modelCalls"],
        )
        return
    with FileLock(target.with_suffix(".lock")):
        row = json.loads(target.read_text(encoding="utf8"))
        if row["config"]["modelCalls"] and row["usedCalls"] >= row["config"]["modelCalls"]:
            row.update(state="exhausted", exhaustedRole=role)
            atomic_json(target, row)
            session.emit(
                "budget_exhausted",
                role=role,
                used=row["usedCalls"],
                limit=row["config"]["modelCalls"],
                reason="本轮模型调用额度耗尽；需要用户确认下一轮，不自动续跑",
            )
            raise BudgetExhausted("本轮模型调用额度耗尽，未发起新请求，不自动补充预算")
        row["usedCalls"] += 1
        row["byRole"][role] = row["byRole"].get(role, 0) + 1
        atomic_json(target, row)
    session.emit(
        "model_call_budget",
        role=role,
        used=row["usedCalls"],
        limit=row["config"]["modelCalls"],
    )


class BudgetProvider:
    def __init__(self, wrapped, session, role):
        object.__setattr__(self, "_budget_args", (wrapped, session, role))

    def __getattr__(self, name):
        return getattr(self._budget_args[0], name)

    def __setattr__(self, name, value):
        setattr(self._budget_args[0], name, value)

    # 通过运行预算登记后转发非流式模型请求。
    def chat(self, *args, **kwargs):
        wrapped, session, role = self._budget_args
        consume(session, role)
        from .context_inspection import request_owner
        with request_owner(session, role):
            reply = wrapped.chat(*args, **kwargs)
            from .memory_runtime import delivered
            delivered(session, role)
            return reply

    # 流式请求也在发出前登记一次调用，而每个流片段沿用这次请求。
    def chat_stream(self, *args, **kwargs):
        wrapped, session, role = self._budget_args
        consume(session, role)
        from .context_inspection import request_owner
        with request_owner(session, role):
            reply = wrapped.chat_stream(*args, **kwargs)
            from .memory_runtime import delivered
            delivered(session, role)
            return reply


# 优先使用本轮冻结的角色模型，缺失时从当前角色配置构造。
def role_provider(session, role):
    from .provider import OpenAICompatProvider
    frozen=getattr(session,"context_role_providers",{})
    if role in frozen:
        value=frozen[role]
        if isinstance(value,Exception):
            raise value
        return value
    return OpenAICompatProvider(profile="HARNESS_PLAN" if role=="planner" else "HARNESS_REVIEW")


# 只有持有会话预算时才包装模型，保留普通独立调用入口。
def provider(wrapped, session, role):
    return (
        BudgetProvider(wrapped, session, role)
        if getattr(session, "execution_budget", None)
        else wrapped
    )


# 界面读取的下一轮配置与当前运行记录分开，仅本轮快照表示实际已用配置。
def view(ident, sv):
    thread = projects.conversation(ident)
    config = saved_config(thread)
    if thread.get("readonlyEvidence"):
        config = validate(dict(config, crew='l0'))
    directory = sv._resolve_sid(ident)
    current = None
    if directory:
        owner = W.RT.read(directory)
        run_id = owner.get("run_id")
        if run_id:
            try:

                class LocatedSession:
                    dir = directory

                target = path(LocatedSession, run_id)
                if target.stat().st_size > 32768:
                    raise ValueError("over limit")
                current = json.loads(target.read_text(encoding="utf8"))
                if current.get("runId") != run_id:
                    raise ValueError("wrong run")
                recorded = current["config"]
                if isinstance(recorded, dict) and retired_checkpoint(recorded):
                    current = dict(state="retired", runId=run_id, reason="旧执行档位已退役，历史仍可查看")
                else:
                    validate(recorded)
            except (ValueError, KeyError, OSError, AttributeError, TypeError, W.Problem):
                current = dict(
                    state="unknown",
                    runId=run_id,
                    reason="本轮配置或预算记录尚未建立或不可读；没有沿用旧轮额度",
                )
    # next 是可变偏好，current 只来自对应 run 的持久快照；读取失败保留 unknown，不拿 next 补作当前事实。
    return dict(
        next=config,
        current=current,
        routes=ROUTES,
        nextReviewerReserve=0,
        permissions=projects.scope(ident)["resources"],
        budgetNotice="启用测试者时，从本轮总额度内预留复核调用，其他角色和子任务不能使用。预算为逻辑请求次数，并非金额上限；接口重试或备用模型可能另计费用。修改只作用下一轮。",
        migration="新的普通任务使用默认 L0。执行方式不会扩大资料授权。",
    )


# 读配置和保存后重读共用同一会话入口。
def route(ident, method, body, sv):
    if method == "POST":
        configure(ident, body)
    elif method != "GET":
        raise W.Problem("not_found", "执行配置入口不存在", 404)
    return 200, view(ident, sv)
