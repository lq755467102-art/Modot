"""Managed L4: draft, human mandate, bounded rounds, then human judgement.

The existing job/contract directory and mandate verifier remain the authority.
Private rounds use the normal conversation runner and scoped three-role tools.
Controller threads are owned by this backend only; restart never replays them.
"""

# 主要引用方包括 `workbench.py` 和 `tools.py`。

from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from . import workbench as W, projects, runtime as RT, mandate as MD
from .filelock import FileLock, atomic_json, read_text

MAX_BYTES = 1024 * 1024
ACTIVE = {"running", "stopping"}
UNSETTLED = {"running", "unknown", "reserved"}
_controllers = {}
_controller_job = ContextVar("delegation_controller_job", default=None)
_controller_reply = ContextVar("delegation_controller_reply", default=None)


def directory(jid):
    if not isinstance(jid, str) or not re.fullmatch("[0-9a-f]{32}", jid):
        raise W.Problem("invalid_delegation", "委托编号无效")
    root = W.P.data_root() / "runs" / "jobs"
    target = root / jid
    if not target.resolve().is_relative_to(root.resolve()):
        raise W.Problem("path_escape", "委托存储路径已变化", 403)
    return target


# 从委托目录读取记录，并核对它仍归属请求会话。
def load(ident, jid):
    projects.conversation(ident)
    path = directory(jid) / "delegation.json"
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("over limit")
        value = json.loads(read_text(path))
        if value["conversationId"] != ident:
            raise W.Problem("wrong_source", "委托不属于当前会话", 403)
        if (
            not isinstance(value["versions"], list)
            or len(value["versions"]) > 20
            or not isinstance(value["rounds"], list)
            or len(value["rounds"]) > 8
            or not isinstance(value["progress"], list)
            or len(value["progress"]) > 200
        ):
            raise ValueError("shape")
        return value
    except FileNotFoundError:
        raise W.Problem("not_found", "委托记录不存在", 404)
    except (ValueError, KeyError, TypeError, OSError) as error:
        detail = type(error).__name__
        if isinstance(error, OSError):
            detail += f" / {getattr(error, 'winerror', None) or error.errno}"
        raise W.Problem(
            "delegation_unreadable", f"委托记录不可读（{detail}），保留原记录，未继续执行", 503
        ) from error


# 持久化同一委托的当前版本和轮次状态。
def save(value):
    if len(json.dumps(value, ensure_ascii=False, indent=1).encode()) > MAX_BYTES:
        raise W.Problem("delegation_full", "委托记录达到保留上限，本次尚未接收", 413)
    atomic_json(directory(value["id"]) / "delegation.json", value)


# 将用户约定归一为可锁定的目标、边界和验收项。
# 使用 `projects.py` 核对选中资料和可执行范围。
def spec_for(ident, spec):
    if not isinstance(spec, dict):
        raise W.Problem("invalid_agreement", "约定须为结构化对象")
    allowed = {"goal", "boundaries", "criteria", "maxRounds", "modelCalls"}
    if set(spec) - allowed:
        raise W.Problem("invalid_agreement", "约定包含不支持的字段")
    result = {}
    for key in ("goal", "boundaries"):
        text = spec.get(key)
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise W.Problem("invalid_agreement", "目标和边界各须为1—4000字")
        result[key] = text.strip()
    for key, high in [("maxRounds", 8), ("modelCalls", 120)]:
        value = spec.get(key)
        if type(value) is not int or not 1 <= value <= high:
            raise W.Problem(
                "invalid_agreement", f"{key}须为1—{high}的整数，不能无限续跑"
            )
        result[key] = value
    criteria = spec.get("criteria")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 20:
        raise W.Problem("invalid_agreement", "验收要求须为1—20项")
    selected = {r["id"]: r for r in projects.scope(ident)["resources"]}
    result["criteria"] = []
    seen = set()
    for row in criteria:
        if not isinstance(row, dict) or set(row) - {
            "id",
            "text",
            "kind",
            "resourceId",
            "argv",
            "contains",
        }:
            raise W.Problem("invalid_criterion", "验收条目字段无效")
        item = {k: row.get(k) for k in ("id", "text", "kind")}
        if (
            not isinstance(item["id"], str)
            or not re.fullmatch("[A-Za-z0-9_-]{1,40}", item["id"])
            or item["id"] in seen
        ):
            raise W.Problem("invalid_criterion", "验收条目编号无效或重复")
        seen.add(item["id"])
        if (
            not isinstance(item["text"], str)
            or not 1 <= len(item["text"]) <= 2000
            or not isinstance(item["kind"], str)
            or item["kind"] not in {"manual", "code_check", "artifact_contains"}
        ):
            raise W.Problem("invalid_criterion", "验收要求或类型无效")
        if item["kind"] == "code_check":
            resource = (
                selected.get(row.get("resourceId"))
                if isinstance(row.get("resourceId"), str)
                else None
            )
            argv = row.get("argv")
            if (
                not resource
                or resource["kind"] != "code"
                or resource["permission"] != "read"
                or not resource.get("execute")
            ):
                raise W.Problem(
                    "resource_denied", "检查仅限当前选中且明确允许运行的代码资料", 403
                )
            if (
                not isinstance(argv, list)
                or not 1 <= len(argv) <= 32
                or any(not isinstance(a, str) or not a or len(a) > 2000 for a in argv)
            ):
                raise W.Problem("invalid_criterion", "检查命令须为有限argv数组")
            item.update(resourceId=resource["id"], argv=argv)
        if item["kind"] == "artifact_contains":
            if (
                not isinstance(row.get("contains"), str)
                or not 1 <= len(row["contains"]) <= 500
            ):
                raise W.Problem("invalid_criterion", "文字检查须有1—500字明确片段")
            item["contains"] = row["contains"]
        result["criteria"].append(item)
    return result


# 为约定快照附上独立版本、来源和时间。
def version(spec, actor):
    return dict(id=uuid.uuid4().hex, spec=spec, actor=actor, at=time.time())


# 在当前会话发起委托；新记录先保持草稿，等待明确锁定。
def create(ident, spec, actor="human"):
    thread = projects.conversation(ident)
    if thread.get("readonlyEvidence"):
        raise W.Problem("readonly_question", "证据追问不能建立执行委托", 403)
    checked = spec_for(ident, spec)
    with W._lock:
        current = thread.get("activeDelegation")
        if current and load(ident, current)["state"] not in {
            "accepted",
            "returned",
            "cancelled",
        }:
            raise W.Problem(
                "existing_delegation", "此事项已有委托，请修改其待确认版本", 409
            )
        if len(thread.get("delegationIds", [])) >= 20:
            raise W.Problem("delegation_full", "此会话已有20份委托记录", 413)
        draft = version(checked, actor)
        value = dict(
            id=uuid.uuid4().hex,
            conversationId=ident,
            revision=1,
            state="draft",
            versions=[draft],
            pendingVersion=draft["id"],
            activeVersion=None,
            rounds=[],
            progress=[],
            decision=None,
            decisions=[],
            created=time.time(),
        )
        save(value)
        W.change_registry(
            lambda data: data["threads"][ident].update(
                activeDelegation=value["id"],
                delegationIds=thread.get("delegationIds", []) + [value["id"]],
            )
        )
        return value


# 在原委托修订上追加待确认版本，正在执行的原版本保留到用户确认。
def propose(ident, jid, spec, expected, actor="human"):
    checked = spec_for(ident, spec)
    with FileLock(directory(jid) / "delegation.lock"):
        value = load(ident, jid)
        if projects.conversation(ident).get("activeDelegation") != jid:
            raise W.Problem(
                "target_changed", "当前委托已改变，旧编辑不能写入其他委托", 409
            )
        if value["revision"] != expected:
            raise W.Problem("agreement_changed", "约定记录已变化，请刷新后核对", 409)
        if len(value["versions"]) >= 20:
            raise W.Problem("delegation_full", "约定已保留20个版本", 413)
        draft = version(checked, actor)
        value["versions"].append(draft)
        value.update(
            pendingVersion=draft["id"],
            state="agreement_changed" if value["activeVersion"] else "draft",
            revision=value["revision"] + 1,
        )
        save(value)
    # Already-started work reaches a tool boundary; no new round uses old terms.
    from .control import request_stop

    d = W.P.data_root() / "runs" / ident
    if value.get("activeVersion") and RT.status(d)["active"]:
        request_stop(d, why="有新的约定待用户确认", after_turn=True)
    return value


# 对照 `mandate.py` 的锁和内容哈希，确认验收约定仍与锁定版本一致。
def mandate_status(jid):
    result = MD.check(directory(jid))
    lock = W.read(directory(jid) / "contract" / "done.lock")
    contract = W.read(directory(jid) / "contract" / "done.json")
    if lock.get("version") != contract.get("version") or lock.get("sha256") != W.digest(
        json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()
    ):
        result = dict(
            result,
            ok=False,
            reasons=result.get("reasons", []) + ["人锁文件缺失或与约定不一致"],
        )
    return result


# 按 `activeVersion` 找实际用于执行的约定。
def active_spec(value):
    return next(
        v["spec"] for v in value["versions"] if v["id"] == value["activeVersion"]
    )


# 只接受用户明确确认的待定版本。核对会话、范围与版本后冻结预算和验收约定。
def lock(ident, jid, body):
    with W._lock, FileLock(directory(jid) / "delegation.lock"):
        value = load(ident, jid)
        if projects.conversation(ident).get("activeDelegation") != jid:
            raise W.Problem("target_changed", "当前委托已改变，请重新核对", 409)
        if body.get("confirm") is not True:
            raise W.Problem("confirmation", "请明确核对目标、要求、边界和总预算")
        if (
            value["revision"] != body.get("expectedRevision")
            or body.get("version") != value["pendingVersion"]
        ):
            raise W.Problem("agreement_changed", "待确认版本已变化，未锁定", 409)
        if (
            jid in _controllers
            or value["state"] in ACTIVE
            or RT.status(W.P.data_root() / "runs" / ident)["active"]
        ):
            raise W.Problem("active_run", "请等当前执行边界停止后再锁定新约定", 409)
        if _reconcile_target(value):
            raise W.Problem("reconcile_required", "先核对已登记轮次，再锁定新约定", 409)
        draft = next(v for v in value["versions"] if v["id"] == body["version"])
        checked = spec_for(ident, draft["spec"])
        if (
            any(c["kind"] != "code_check" for c in checked["criteria"])
            and body.get("acknowledgeWeakChecks") is not True
        ):
            raise W.Problem(
                "acknowledgement",
                "人工要求和文字片段检查不能证明语义质量，请明确知悉后锁定",
            )
        from .execution_policy import saved_config

        execution = saved_config(projects.conversation(ident))
        scoped = projects.scope(ident)
        contract = dict(
            version=draft["id"],
            goal=checked["goal"],
            boundaries=checked["boundaries"],
            done=checked["criteria"],
            limits=dict(
                maxRounds=checked["maxRounds"], modelCalls=checked["modelCalls"]
            ),
            scopeRevision=scoped["revision"],
            execution=dict(execution, crew="l4", purpose="execute", delegationId=jid),
        )
        root = directory(jid)
        atomic_json(
            root / "job.json",
            dict(
                id=jid,
                managedConversation=ident,
                crew="l4",
                cwd=W.workspace(projects.conversation(ident)["workspaceId"])["path"],
                scope=[r["id"] for r in scoped["resources"]],
                kind="task",
            ),
        )
        atomic_json(root / "contract" / "done.json", contract)
        atomic_json(
            root / "contract" / "done.lock",
            dict(
                version=draft["id"],
                at=time.time(),
                by="human",
                sha256=W.digest(
                    json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()
                ),
            ),
        )
        issued = MD.issue(root, strength="explicit", by="human application command")
        if not issued["ok"]:
            raise W.Problem("mandate_failed", "授权凭证未能写入，未开始执行", 503)
        value.update(
            activeVersion=draft["id"],
            pendingVersion=None,
            state="ready",
            stopRequested=False,
            revision=value["revision"] + 1,
        )
        save(value)
        return value


def synchronize(value):
    """Unconfirmed allocations stay charged; only confirmed ends free unused calls."""
    for row in value["rounds"]:
        root = Path(row["directory"])
        budget = root / "execution-runs" / (row["runId"] + ".json")
        checkpoint = W.read(root / "session.json")
        runtime = RT.read(root)
        actual = RT.identity(row["pid"])
        alive = actual not in (None, "unknown") and actual == row["identity"]
        if row["state"] in {"completed", "failed", "paused", "budget"}:
            continue
        try:
            if budget.stat().st_size > 8192:
                raise ValueError("over limit")
            recorded = json.loads(read_text(budget))
            config = recorded["config"]
            if (
                recorded["runId"] != row["runId"]
                or not isinstance(config, dict)
                or config.get("crew") != "l4"
                or config.get("delegationId") != value["id"]
                or config.get("modelCalls") != row["allocatedCalls"]
                or type(recorded["usedCalls"]) is not int
                or not 0 <= recorded["usedCalls"] <= row["allocatedCalls"]
            ):
                raise ValueError("identity")
            row["observedCalls"] = recorded["usedCalls"]
            row["budgetState"] = recorded.get("state")
            row.pop("budgetReadIssue", None)
            if (
                not alive
                and actual != "unknown"
                and checkpoint.get("run_id") == row["runId"]
                and checkpoint.get("status")
                in {"success", "error", "stuck", "paused", "needs_human", "budget"}
            ):
                row.update(
                    state="completed"
                    if checkpoint["status"] == "success"
                    else "paused"
                    if checkpoint["status"] in {"paused", "needs_human"}
                    else "budget"
                    if checkpoint["status"] == "budget"
                    else "failed",
                    chargedCalls=recorded["usedCalls"],
                )
            elif alive and runtime.get("run_id") == row["runId"]:
                row["state"] = "running"
            else:
                row["state"] = "unknown"
        except (OSError, ValueError, KeyError, TypeError) as error:
            row["state"] = "unknown"
            row["budgetReadIssue"] = type(error).__name__
    charged = sum(r.get("chargedCalls", r["allocatedCalls"]) for r in value["rounds"])
    return dict(
        chargedCalls=charged,
        remainingCalls=max(0, active_spec(value)["modelCalls"] - charged),
        remainingRounds=max(0, active_spec(value)["maxRounds"] - len(value["rounds"])),
        unconfirmed=any(
            r["state"] in UNSETTLED for r in value["rounds"]
        ),
    )


# 续接前确认当前会话、有效约定与保留的锁对应，待定修订按调用方约束处理。
def locked_contract(ident, jid, value, *, allow_pending=False):
    if projects.conversation(ident).get("activeDelegation") != jid:
        raise W.Problem(
            "wrong_source", "这不是当前事项的委托，不能隐式切换执行目标", 409
        )
    if (value.get("pendingVersion") and not allow_pending) or not value.get("activeVersion"):
        raise W.Problem(
            "agreement_required", "需要用户锁定当前约定；新要求不能静默覆盖", 409
        )
    if not mandate_status(jid)["ok"]:
        raise W.Problem("mandate_changed", "人锁授权与当前约定不一致", 409)
    contract = W.read(directory(jid) / "contract" / "done.json")
    if (
        contract.get("version") != value["activeVersion"]
        or contract.get("scopeRevision") != projects.scope(ident)["revision"]
    ):
        raise W.Problem(
            "scope_changed", "资料范围或权限已变化，请重新核对并锁定约定", 409
        )
    return contract


def _round_call_limit(contract, budget):
    # A zero conversation limit leaves the locked delegation total in charge.
    per_round = contract["execution"]["modelCalls"] or contract["limits"]["modelCalls"]
    return min(per_round, budget["remainingCalls"])


# 受控执行配置必须指向同一会话的已锁定委托并复用其预算约束。
def authorize_config(config, ident):
    jid = config.get("delegationId")
    value = load(ident, jid)
    if value["state"] not in {"ready", "running", "paused", "blocked", "budget_stopped"}:
        raise W.Problem("agreement_required", "当前委托状态不能启动执行", 409)
    contract = locked_contract(ident, jid, value)
    budget = synchronize(value)
    if budget["unconfirmed"]:
        raise W.Problem(
            "unknown_round", "上次执行尚未确认结束，不能重放或启动新轮", 409
        )
    if not budget["remainingRounds"] or not budget["remainingCalls"]:
        raise W.Problem(
            "budget_exhausted", "委托总轮次或调用额度耗尽，需要用户确认新的约定", 409
        )
    turns = config.get("maxTurns", 1)
    turn_limit = contract["execution"]["maxTurns"]
    if (not 1 <= config.get("modelCalls", 1) <= _round_call_limit(contract, budget)
            or turn_limit and not 1 <= turns <= turn_limit):
        raise W.Problem("budget_exceeded", "本轮额度超过已锁定约定", 409)
    return contract, budget


# 落盘保留轮次与预算，再由实际运行写出完成或待核对回执。
def reserve_run(session, config):
    jid = config["delegationId"]
    ident = Path(session.dir).name
    with FileLock(directory(jid) / "delegation.lock"):
        authorize_config(config, ident)
        value = load(ident, jid)
        if any(r["runId"] == session.run_id for r in value["rounds"]):
            raise W.Problem("duplicate_round", "此轮已登记，不能补充额度", 409)
        value["rounds"].append(
            dict(
                runId=session.run_id,
                directory=str(session.dir),
                pid=os.getpid(),
                identity=RT.identity(os.getpid()),
                allocatedCalls=config["modelCalls"],
                state="reserved",
                version=value["activeVersion"],
                at=time.time(),
            )
        )
        value["revision"] += 1
        save(value)


# 由工具记录本轮对某验收项的声明。原动作、约定版本和轮次归属用于后续证据核对。
def record_progress(ident, jid, criterion, disposition, note, source, proof=None):
    if (
        disposition not in {"claimed_done", "not_this_round", "deferred", "blocked"}
        or not isinstance(note, str)
        or not 1 <= len(note) <= 2000
    ):
        raise W.Problem(
            "invalid_progress", "请明确区分本轮不做、延期、阻塞及有证据的完成主张"
        )
    with FileLock(directory(jid) / "delegation.lock"):
        value = load(ident, jid)
        if not value.get("activeVersion"):
            raise W.Problem("agreement_required", "尚未锁定约定", 409)
        if criterion not in {c["id"] for c in active_spec(value)["criteria"]}:
            raise W.Problem("invalid_criterion", "未知验收条目，不会静默删除")
        if len(value["progress"]) >= 200:
            raise W.Problem("delegation_full", "推进记录达到200项", 413)
        if proof is not None and (
            not isinstance(proof, dict) or len(json.dumps(proof)) > 4000
        ):
            raise W.Problem("invalid_proof", "证据引用无效或过大")
        if proof and proof.get("actionId"):
            from . import evidence
            from .code_service import RuntimeView

            fact = evidence.detail(ident, proof["actionId"], RuntimeView())
            if (
                proof.get("evidenceRevision") is not None
                and proof["evidenceRevision"] != fact["evidenceRevision"]
            ):
                raise W.Problem("evidence_changed", "原始检查动作已变化", 409)
            proof = dict(proof, evidenceRevision=fact["evidenceRevision"])
        value["progress"].append(
            dict(
                id=uuid.uuid4().hex,
                version=value["activeVersion"],
                criterionId=criterion,
                disposition=disposition,
                note=note,
                source=source,
                proof=proof,
                at=time.time(),
            )
        )
        value["revision"] += 1
        save(value)
        return dict(
            ok=True,
            record=value["progress"][-1],
            notice="这是推进主张；延期/本轮不做不会从总要求中消失，仍须核对实际证据",
        )


# 按有效约定只读取对应轮次的成果、检查与交付证据。用户接受决定由 `decide` 持有。
def evaluate(ident, jid, sv, *, value=None):
    from . import evidence, delivery_review as DR, artifacts

    if value is None:
        value = load(ident, jid)
    if not value.get("activeVersion"):
        return dict(criteria=[], allChecksMet=False, signature=None)
    rows = []
    runs = {r["runId"] for r in value["rounds"]}
    for criterion in active_spec(value)["criteria"]:
        claim = next(
            (
                r
                for r in reversed(value["progress"])
                if r["version"] == value["activeVersion"]
                and r["criterionId"] == criterion["id"]
            ),
            None,
        )
        row = dict(criterion=criterion, claim=claim, result="uncovered")
        proof = (claim or {}).get("proof")
        if claim and claim["disposition"] == "claimed_done" and proof:
            try:
                if criterion["kind"] == "code_check":
                    fact = evidence.detail(ident, proof["actionId"], sv)
                    if (
                        fact.get("runId") not in runs
                        or fact.get("input", {}).get("argv") != criterion["argv"]
                    ):
                        raise W.Problem(
                            "wrong_proof", "检查不属于委托或命令与锁定要求不一致", 409
                        )
                    target = DR.current_target(
                        ident, dict(kind="code", id=criterion["resourceId"])
                    )
                    row.update(
                        result=DR.automatic_result(ident, proof, target, sv),
                        target=target,
                    )
                elif criterion["kind"] == "artifact_contains":
                    item = artifacts.artifact(ident, proof["artifactId"])
                    if item["versions"][-1]["id"] != proof["versionId"]:
                        raise W.Problem(
                            "stale", "成果已有新版，此前文字检查不适用", 409
                        )
                    read = artifacts.read_version(ident, item["id"], proof["versionId"])
                    source = read["version"].get("source", {})
                    if source.get("runId") not in runs:
                        raise W.Problem("wrong_proof", "成果不是本委托轮次产生", 409)
                    row.update(
                        result="passed"
                        if criterion["contains"] in read.get("text", "")
                        else "failed",
                        target=dict(
                            kind="artifact",
                            id=item["id"],
                            revision=read["version"]["hash"],
                            versionId=proof["versionId"],
                        ),
                        weakCheck=True,
                    )
                else:
                    row["result"] = "needs_human"
            except (W.Problem, KeyError, ValueError, OSError) as error:
                row.update(result="unknown", reason=str(error))
        rows.append(row)
    signature = W.digest(
        json.dumps(
            dict(version=value["activeVersion"], criteria=rows),
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    )
    return dict(
        criteria=rows,
        allChecksMet=bool(rows) and all(r["result"] == "passed" for r in rows),
        signature=signature,
        notice="达到预设检查条件也不代表用户接受；文字片段检查不能证明语义质量。",
    )


# 活动委托期间只让受控调度发送到此会话，防止普通消息绕开约定。
def ensure_send_allowed(ident):
    thread = projects.conversation(ident)
    jid = thread.get("activeDelegation")
    if jid and load(ident, jid)["state"] in ACTIVE and _controller_job.get() != jid:
        raise W.Problem(
            "delegation_active",
            "持续委托在推进，请补充可追踪纠正或先停止；不要另起同一事项执行轮",
            409,
        )


CONTROLLER_PROMPT = "继续已由人锁定的L4委托；不得改目标、边界或标准。请先规划、完成当前可推进步骤、记录实际检查或成果证据。\n"


def controller_material(prompt, jid):
    """Recognize the internal handoff only for an authorized delegation launch."""
    if not jid or not prompt.startswith(CONTROLLER_PROMPT):
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(prompt[len(CONTROLLER_PROMPT):])
    except (ValueError, RecursionError):
        return {}
    if isinstance(value, dict) and value.get("jobId") == jid and isinstance(value.get("contract"), dict):
        return value
    return {}


def controller_answer():
    return _controller_reply.get()


def controller_display():
    jid = _controller_job.get()
    return (
        controller_answer() or "持续委托控制器：按已锁定约定推进下一轮；目标、边界和预算可在委托面板核对。"
        if jid
        else None
    )


# 停止控制器推进并请求停止当前运行，原状态仍从保留记录确认。
def stop(ident, jid, reason="用户请求停止"):
    with FileLock(directory(jid) / "delegation.lock"):
        value = load(ident, jid)
        if value["state"] in {"accepted", "returned", "cancelled"}:
            return value
        if projects.conversation(ident).get("activeDelegation") != jid:
            raise W.Problem("wrong_source", "此委托不是当前事项，不停止别的执行", 409)
        value.update(
            stopRequested=True,
            state="stopping"
            if value["state"] in ACTIVE
            else "unknown"
            if value["state"] == "unknown"
            or value.get("activeVersion")
            and synchronize(value)["unconfirmed"]
            else "paused",
            reason=reason,
            revision=value["revision"] + 1,
        )
        save(value)
    from .control import request_stop

    d = W.P.data_root() / "runs" / ident
    if jid in _controllers or RT.read(d).get("run_id") in {
        r["runId"] for r in value["rounds"]
    }:
        request_stop(d, why=reason, after_turn=True)
    return value


def _reconcile_target(value):
    if value.get("reconcileRunId"):
        return value["reconcileRunId"]
    # Earlier builds could leave a reservation after a failed read or a
    # controller restart. Those records can be inspected, never relaunched.
    if value["rounds"] and value["state"] in ACTIVE | {"blocked", "unknown"}:
        row = value["rounds"][-1]
        reason = value.get("reason") or ""
        if (row["state"] in UNSETTLED or value["state"] in ACTIVE and value["id"] not in _controllers
                or "delegation_unreadable" in reason or reason.startswith("角色尚未完成")):
            return row["runId"]
    return None


def _check_state(value, checked):
    if checked["allChecksMet"]:
        latest = value["rounds"][-1] if value["rounds"] else None
        if latest and (latest["state"] != "completed" or
                       any(state != "completed" for state in latest.get("roleStates", {}).values())):
            value.update(state="blocked", reason="验收证据已满足检查，但本轮角色未全部完成。保留已有结果供核对。")
            return True
        value.update(state="checks_met", reason="预设检查条件已满足，等待用户裁定",
                     evaluationSignature=checked["signature"])
        return True
    if any(row["result"] == "unknown" for row in checked["criteria"]):
        raise W.Problem("round_evidence_pending", "既有验收证据暂无法核对，未启动新轮", 409)
    return False


def _read_finished_roles(ident, run_id, sv):
    from . import progress
    deadline = time.monotonic() + 2
    while True:
        try:
            _, snapshot = progress.route(ident, [], "GET", {"run": [run_id]}, sv)
            run = snapshot.get("run") or {}
            if run.get("id") == run_id and not snapshot.get("evidenceIncomplete"):
                states = {role: run["roles"].get(role, {}).get("state", "unknown")
                          for role in ("planner", "executor", "reviewer")}
                verdict = (run.get("latestCandidate") or {}).get("verdict")
                if states["reviewer"] == "completed" and verdict in {"issues", "insufficient"}:
                    states["reviewer"] = verdict
                return states
        except W.Problem as error:
            if error.status not in {404, 425, 503}:
                raise
        if time.monotonic() >= deadline:
            raise W.Problem("round_evidence_pending", "本轮角色事件尚未核对完整，保留运行编号供再次读取", 409)
        time.sleep(0.10)


def _recover_review_pause(ident, value, row, sv):
    """Recover a stopped reviewer only when its executor handoff is durable."""
    from . import evidence

    root = Path(sv._resolve_sid(ident))
    if row.get("budgetReadIssue") or Path(row["directory"]) != root:
        return False
    run_id = row["runId"]
    with FileLock(root / "runtime.lock"), FileLock(root / "state.lock"):
        runtime = RT.status(root)
        if (runtime["active"] or runtime.get("run_id") != run_id
                or runtime.get("pid") != row["pid"] or runtime.get("identity") != row["identity"]
                or row["identity"] in (None, "unknown")):
            return False
        state_path = root / "session.json"
        checkpoint = W.read(state_path)
        handoff = checkpoint.get("roleWait") or {}
        if (checkpoint.get("run_id") != run_id or checkpoint.get("status") != "running"
                or checkpoint.get("waitQuestion") or not checkpoint.get("msgs")
                or handoff.get("role") != "reviewer" or handoff.get("reviewRun") != run_id
                or handoff.get("agreementVersion") != value["activeVersion"]):
            return False
        roles = _read_finished_roles(ident, run_id, sv)
        if (roles["planner"] != "completed" or roles["executor"] != "completed"
                or roles["reviewer"] not in {"running", "unknown"}):
            return False
        with evidence.projection(root) as (db, index):
            if index["catchingUp"] or db.execute(
                """SELECT 1 FROM actions WHERE run=? AND
                   (state IN ('running','unknown','waiting_approval') OR (role='reviewer' AND tool='ask_human')) LIMIT 1""",
                (run_id,),
            ).fetchone():
                return False
        checkpoint.update(status="paused", why="原执行已交接，复核中断，待继续复核")
        atomic_json(state_path, checkpoint)
    return True


def _settle_round(ident, value, sv, run_id, *, recovery=False):
    """Reconcile one exact reservation. This function never launches a round."""
    budget = synchronize(value)
    row = next((r for r in value["rounds"] if r["runId"] == run_id), None)
    recovered_review = bool(recovery and row and row["state"] == "unknown"
                            and _recover_review_pause(ident, value, row, sv))
    if recovered_review:
        budget = synchronize(value)
    if not row or row["state"] in UNSETTLED:
        issue = f"（预算记录 {row['budgetReadIssue']}）" if row and row.get("budgetReadIssue") else ""
        raise W.Problem("unknown_round", f"无法核对本轮退出状态或调用预算{issue}，未启动新轮", 409)
    row.pop("budgetReadIssue", None)
    if value.get("pendingVersion") or value.get("stopRequested"):
        value.update(state="agreement_changed" if value.get("pendingVersion") else "paused",
                     reason="新约定待确认" if value.get("pendingVersion") else "已在执行边界停止")
    elif row["state"] != "completed" or row.get("budgetState") == "exhausted":
        exhausted = row["state"] == "budget" or row.get("budgetState") == "exhausted" or not budget["remainingCalls"]
        value.update(state="budget_stopped" if exhausted else "blocked" if row["state"] == "failed" else "paused",
                     reason="执行轮次或模型调用额度耗尽，未自动续跑" if exhausted else
                     "原执行已交接，复核中断，待继续复核" if recovered_review else "本轮失败或需要决定，未自动继续")
    else:
        row["roleStates"] = _read_finished_roles(ident, run_id, sv)
        missing = [name for name, state in row["roleStates"].items() if state != "completed"]
        if missing:
            value.update(state="blocked", reason="本轮角色未完成：" + "、".join(missing) + "。已保留实际检查缺口。")
        elif not _check_state(value, evaluate(ident, value["id"], sv, value=value)) and recovery:
            value.update(state="paused", reason="已核对原轮次，仍有待完成要求。继续时会沿用原进度。")
    value.pop("reconcileRunId", None)
    value["revision"] += 1
    save(value)
    return value["state"] == "running"


def _record_reconcile_failure(value, run_id, error):
    value.update(state="unknown", reconcileRunId=run_id,
                 reason=f"轮次 {run_id[:12]} 收尾待核对。{getattr(error, 'message', str(error))}"[:1000],
                 revision=value["revision"] + 1)
    save(value)


# 按用户指定的原轮次和修订补核对状态，有未确认的启动也沿原记录处理。
def reconcile(ident, jid, body, sv):
    with W._lock, FileLock(directory(jid) / "delegation.lock"):
        value = load(ident, jid)
        run_id = _reconcile_target(value)
        if (not run_id or body.get("runId") != run_id or
                body.get("expectedRevision") != value["revision"]):
            raise W.Problem("round_changed", "待核对轮次已变化，请刷新委托", 409)
        runtime = RT.status(sv._resolve_sid(ident))
        if jid in _controllers or runtime["active"]:
            raise W.Problem("active_run", "当前执行尚未结束，请等待停止或完成", 409)
        if runtime.get("run_id") and runtime["run_id"] != run_id:
            raise W.Problem("round_changed", "当前运行身份与待核对轮次不一致，未继续委托", 409)
        locked_contract(ident, jid, value, allow_pending=True)
        if not any(r["runId"] == run_id and r["version"] == value["activeVersion"] for r in value["rounds"]):
            raise W.Problem("round_changed", "待核对轮次不属于当前锁定约定", 409)
        try:
            _settle_round(ident, value, sv, run_id, recovery=True)
        except (W.Problem, OSError, TimeoutError) as error:
            _record_reconcile_failure(value, run_id, error)
        return value


# 控制器反复从已锁定约定启动并核对实际轮次，额度与需求变化会结束或暂停续接。
# 启动结果不明时先记录待核对。
def _run(ident, jid, sv, answer=None):
    d = sv._resolve_sid(ident)
    run_id = None
    launch_unconfirmed = False
    first_launch = True
    try:
        while True:
            with W._lock, FileLock(directory(jid) / "delegation.lock"):
                value = load(ident, jid)
                budget = synchronize(value)
                if value.get("stopRequested") or value.get("pendingVersion"):
                    value.update(
                        state="agreement_changed"
                        if value.get("pendingVersion")
                        else "paused",
                        reason="新约定待确认"
                        if value.get("pendingVersion")
                        else "已在执行边界停止",
                    )
                    save(value)
                    return
                if budget["unconfirmed"]:
                    value.update(
                        state="unknown", reason="上次执行状态无法核对，不自动重放"
                    )
                    save(value)
                    return
                checked = evaluate(ident, jid, sv, value=value)
                checkpoint = W.read(Path(d) / "session.json") if first_launch else {}
                last = value["rounds"][-1] if value["rounds"] else {}
                resume_reviewer = (last.get("state") == "paused"
                                   and checkpoint.get("run_id") == last.get("runId")
                                   and (checkpoint.get("roleWait") or {}).get("role") == "reviewer"
                                   and last.get("version") == value["activeVersion"])
                if not ((answer or resume_reviewer) and checked["allChecksMet"]) and _check_state(value, checked):
                    save(value)
                    return
                if not budget["remainingCalls"] or not budget["remainingRounds"]:
                    value.update(
                        state="budget_stopped",
                        reason="总预算/轮次用尽，未完成要求仍保留",
                    )
                    save(value)
                    return
                contract = W.read(directory(jid) / "contract" / "done.json")
                config = dict(
                    contract["execution"],
                    modelCalls=_round_call_limit(contract, budget),
                )
                authorize_config(config, ident)
                value.update(
                    state="running",
                    reason=f"推进第{len(value['rounds']) + 1}轮",
                    revision=value["revision"] + 1,
                )
                save(value)
                prompt = (
                    CONTROLLER_PROMPT
                    + json.dumps(
                        dict(
                            jobId=jid,
                            contract=contract,
                            priorProgress=value["progress"][-20:],
                            checks=checked,
                            userAnswer=answer,
                        ),
                        ensure_ascii=False,
                    )
                )
                token = _controller_job.set(jid)
                reply_token = _controller_reply.set(answer)
                try:
                    launch_unconfirmed = True
                    code, result = sv._continue(
                        d, prompt, crew="l4", execution_config=config, wait_for_launch=False
                    )
                finally:
                    _controller_reply.reset(reply_token)
                    _controller_job.reset(token)
                answer = None
                first_launch = False
                if code != 200:
                    launch_unconfirmed = False
                    raise W.Problem(
                        result.get("code", "launch_failed"),
                        result.get("error", "执行轮未启动"),
                        code,
                    )
                runtime = RT.read(d)
                if runtime.get("pid") != result.get("pid") or not runtime.get("run_id"):
                    raise W.Problem("unknown_round", "启动回执与运行身份无法核对，未重复启动", 409)
                run_id = runtime["run_id"]
                launch_unconfirmed = False
            while (runtime := RT.status(d))["active"]:
                if runtime.get("run_id") and runtime["run_id"] != run_id:
                    raise W.Problem("unknown_round", "当前运行身份已变化，未接管或重放", 409)
                current = load(ident, jid)
                if current.get("stopRequested") or current.get("pendingVersion"):
                    from .control import request_stop

                    request_stop(d, why="持续委托停止/约定变化", after_turn=True)
                time.sleep(0.2)
            with FileLock(directory(jid) / "delegation.lock"):
                value = load(ident, jid)
                if not _settle_round(ident, value, sv, run_id):
                    return
                run_id = None
    except Exception as error:
        try:
            with FileLock(directory(jid) / "delegation.lock"):
                value = load(ident, jid)
                if run_id:
                    _record_reconcile_failure(value, run_id, error)
                    return
                if getattr(error, "code", None) == "round_evidence_pending" and value["rounds"]:
                    _record_reconcile_failure(value, value["rounds"][-1]["runId"], error)
                    return
                value.update(
                    state="unknown"
                    if launch_unconfirmed or getattr(error, "code", None) in {"unknown_round", "round_evidence_pending"}
                    else "blocked",
                    reason=getattr(error, "message", str(error))[:1000],
                    revision=value["revision"] + 1,
                )
                save(value)
        except (OSError, W.Problem, TimeoutError):
            pass
    finally:
        _controllers.pop(jid, None)


# 有待核对轮次时先走 `reconcile`。续接前确认约定存在并保留控制器归属。
def start(ident, jid, sv, *, answer=None, answer_run_id=None):
    with W._lock, FileLock(directory(jid) / "delegation.lock"):
        value = load(ident, jid)
        if _reconcile_target(value):
            raise W.Problem("reconcile_required", "先核对已登记轮次，才能继续委托", 409)
        if not value.get("activeVersion"):
            raise W.Problem("agreement_required", "请先核对并锁定约定", 409)
        if jid in _controllers or value["state"] in ACTIVE:
            raise W.Problem("active_run", "此委托仍在推进或需要核对", 409)
        if value["state"] in {"unknown", "accepted", "returned", "agreement_changed"}:
            raise W.Problem(
                "agreement_required", "请先核对未知执行或确认新约定，不能自动重放", 409
            )
        if RT.status(sv._resolve_sid(ident))["active"]:
            raise W.Problem("active_run", "当前事项仍在运行", 409)
        checkpoint = W.read(Path(sv._resolve_sid(ident)) / "session.json")
        last = value["rounds"][-1] if value["rounds"] else {}
        waiting = (checkpoint.get("status") == "needs_human"
                   and checkpoint.get("run_id") == last.get("runId")
                   and last.get("version") == value.get("activeVersion"))
        if answer is not None:
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 4000:
                raise W.Problem("invalid_answer", "请填写有效的委托回复")
            if not waiting or answer_run_id != last.get("runId"):
                raise W.Problem("stale_answer", "委托等待轮次已变化，请刷新", 409)
        elif waiting and checkpoint.get("waitQuestion"):
            raise W.Problem("answer_required", "委托正在等待回复，请在对话中回答", 409)
        budget = synchronize(value)
        contract = locked_contract(ident, jid, value)
        config = dict(contract["execution"], modelCalls=_round_call_limit(contract, budget))
        authorize_config(config, ident)
        value.update(
            state="running",
            stopRequested=False,
            owner=dict(pid=os.getpid(), identity=RT.identity(os.getpid())),
            revision=value["revision"] + 1,
        )
        save(value)
        worker = threading.Thread(
            target=_run,
            args=(ident, jid, sv, answer),
            daemon=True,
            name="delegation-" + jid[:8],
        )
        _controllers[jid] = worker
        worker.start()
        return value


# 读回约定、运行与验收证据，附上可核对的待处理轮次。
def view(ident, jid, sv):
    value = load(ident, jid)
    owned = jid in _controllers
    if value["state"] in ACTIVE and not owned:
        # A controller can persist its pause or finish between the read and
        # releasing ownership. Check the durable state before reporting a loss.
        with FileLock(directory(jid) / "delegation.lock", shared=True):
            value = load(ident, jid)
            owned = jid in _controllers
    run_id = _reconcile_target(value)
    if run_id and not owned:
        value["reconcileRunId"] = run_id
    if value.get("activeVersion"):
        value["budget"] = synchronize(value)
        value["evaluation"] = evaluate(ident, jid, sv)
        value["mandate"] = mandate_status(jid)
    if value["state"] in ACTIVE and not owned:
        value.update(
            state="unknown", reason="当前后端没有接管此持续控制器；保留记录，不自动重放"
        )
    if value.get("decision") and (
        value.get("pendingVersion")
        or value["decision"].get("version") != value.get("activeVersion")
        or value.get("evaluation", {}).get("signature")
        != value["decision"]["evaluationSignature"]
    ):
        value["decisionStale"] = True
    return value


# 用评估时的约定和证据签名核对人工交付决定，确认前保留原评估状态。
def decide(ident, jid, body, sv):
    with FileLock(directory(jid) / "delegation.lock"):
        value = load(ident, jid)
        if not value.get("activeVersion") or value.get("pendingVersion"):
            raise W.Problem(
                "agreement_required", "先确认待定约定版本，再对交付作出决定", 409
            )
        if (
            body.get("confirm") is not True
            or body.get("decision") not in {"accept", "return"}
            or not isinstance(body.get("reason"), str)
            or not 1 <= len(body["reason"]) <= 2000
        ):
            raise W.Problem("confirmation", "请明确接受或退回并记录理由")
        if (
            value["state"] in ACTIVE
            or RT.status(sv._resolve_sid(ident))["active"]
            or _reconcile_target(value)
            or synchronize(value)["unconfirmed"]
        ):
            raise W.Problem("active_run", "执行尚未确认结束，不能裁定", 409)
        checked = evaluate(ident, jid, sv)
        if (
            body.get("expectedRevision") != value["revision"]
            or body.get("evaluationSignature") != checked["signature"]
        ):
            raise W.Problem(
                "target_changed", "约定、成果或检查证据已变化，请重新核对", 409
            )
        if not checked["allChecksMet"] and body.get("acknowledgeGaps") is not True:
            raise W.Problem(
                "acknowledgement", "仍有未核查或未完成要求，请明确知悉后决定"
            )
        value.update(
            state="accepted" if body["decision"] == "accept" else "returned",
            decision=dict(
                actor="human",
                decision=body["decision"],
                reason=body["reason"],
                version=value["activeVersion"],
                evaluationSignature=checked["signature"],
                at=time.time(),
            ),
            revision=value["revision"] + 1,
        )
        if len(value.get("decisions", [])) >= 20:
            raise W.Problem("delegation_full", "已保留20次用户裁定，不能覆盖历史", 413)
        value.setdefault("decisions", []).append(value["decision"])
        save(value)
        return value


# 工作间会话入口承接约定的创建、锁定、续接、停止、核对和人工决定。
def route(ident, parts, method, body, sv):
    thread = projects.conversation(ident)
    if thread.get("readonlyEvidence"):
        raise W.Problem("readonly_question", "证据追问不能执行持续委托", 403)
    if not parts:
        if method == "GET":
            jid = thread.get("activeDelegation")
            return 200, dict(current=view(ident, jid, sv) if jid else None)
        if method == "POST":
            return 200, create(ident, body.get("spec"))
    jid = parts[0]
    if len(parts) == 1:
        if method == "GET":
            return 200, view(ident, jid, sv)
        if method == "POST":
            return 200, propose(
                ident, jid, body.get("spec"), body.get("expectedRevision")
            )
    if len(parts) == 2 and method == "POST":
        if parts[1] == "lock":
            return 200, lock(ident, jid, body)
        if parts[1] == "start":
            if body.get("confirm") is not True:
                raise W.Problem("confirmation", "请明确开始已锁定约定")
            return 200, start(ident, jid, sv, answer=body.get("answer"),
                              answer_run_id=body.get("answerRunId"))
        if parts[1] == "reconcile":
            return 200, reconcile(ident, jid, body, sv)
        if parts[1] == "stop":
            return 200, stop(ident, jid)
        if parts[1] == "decide":
            return 200, decide(ident, jid, body, sv)
    raise W.Problem("not_found", "没有此委托入口", 404)


# 通过 `tools.py` 的原动作身份和实际检出目录绑定来验证委托工具调用。
def tool(root, operation, **body):
    from .tools import current_call
    from .execution_policy import binding

    source = current_call()
    thread = binding(root)
    if not source or not thread or source.get("sessionId") != thread["id"]:
        raise W.Problem("identity_required", "委托工具需要实际会话动作身份", 403)
    ident = thread["id"]
    if operation == "propose":
        if body.get("job_id"):
            value = propose(
                ident,
                body["job_id"],
                body["spec"],
                body.get("expected_revision"),
                actor="agent",
            )
        else:
            value = create(ident, body["spec"], actor="agent")
        return dict(
            ok=True,
            delegation=value,
            notice="仅待确认草案，不授予预算或权限，不能自锁约定",
        )
    if operation == "progress":
        return record_progress(
            ident,
            body["job_id"],
            body["criterion_id"],
            body["disposition"],
            body["note"],
            source,
            body.get("proof"),
        )
    raise W.Problem(
        "role_denied", "模型只能提案或登记推进主张，不能锁定、启动、接受或追加预算", 403
    )


# 宿主退出时按控制器记录逐个暂停持续委托。
def stop_all():
    for jid in list(_controllers):
        try:
            data = W.read(directory(jid) / "delegation.json")
            stop(data["conversationId"], jid, "应用正在结束，持续委托暂停")
        except (W.Problem, OSError):
            pass
