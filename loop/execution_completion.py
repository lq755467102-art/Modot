"""Explicit delivery declarations and evidence-backed replacement of broken check entry points."""
# 主要引用方包括 `loop.py` 和 `role_coordination.py`。
import json
from pathlib import Path
import time
from . import code_service as C, evidence, workbench as W
from .tools import Tool


def overview(state):
    """Project existing gate facts; no checks, scheduling or filesystem reads."""
    if state.get("serialReadonlyReview"):
        from .serial_review import overview
        return overview(state)
    pending = []
    completed = []

    def title(text):
        return next((line.strip()[:180] for line in str(text).splitlines()
                     if line.strip() and line.strip() not in {"[goal]", "[amend]", "[answer]", "[answer_amend]"}), "当前任务要求")

    def missing(kind, ident, role, text, status="pending"):
        pending.append(dict(kind=kind, id=ident, role=role, text=text, state=status))

    dirty = state.get("dirty", {})
    # 脏资源版本优先；只有未被后续修改取代的候选才参与当前交付，旧候选通过不能覆盖新版本缺口。
    targets = dict(dirty)
    selected_rows = {}
    for key, entry in state.get("candidates", {}).items():
        row = dict(entry, id=entry.get("id", key))
        if not row.get("unitId") and row.get("state") != "superseded":
            rid = row["resourceId"]
            if rid not in dirty:
                targets[rid] = row["revision"]
            if row["revision"] == targets.get(rid):
                selected_rows[rid] = row
    declaration = state.get("executionDeclaration", {})
    if state.get('runtimeCompletion'):
        for index, item in enumerate(declaration.get('remaining', [])):
            missing('implementation', 'remaining:' + str(index), 'executor', title(item))
    if not state.get('runtimeCompletion') and (dirty or state.get('completionDeclarationRequired')) and (
            declaration.get("status") != "ready" or declaration.get("revisions") != dirty):
        missing("implementation", "executor", "executor", "执行者尚未确认当前改动已完整交付")
    elif declaration.get("status") == "ready":
        completed.append("执行者已提交交付声明")
    for resource in state.get("executorChecks", {}).get("resources", []):
        if (state.get('nativeExecutor') and resource.get('checks')
                and resource.get('status') != 'unknown'):
            # Ordinary receipts are observations. Explicit requirements below
            # still own their evidence gaps; unknown execution remains visible.
            if resource.get('status') == 'checked':
                completed.append('执行者检查命令执行成功，覆盖范围需按实际回执核对')
            continue
        if (state.get('nativeExecutor') and resource.get('status') == 'unverified'
                and not resource.get('pending')
                and all(c.get('purpose') == 'exploration' for c in resource['checks'])):
            # An observed probe is not a newly invented acceptance requirement.
            # Unknown execution and explicit user requirements remain separate.
            continue
        if resource.get("status") == "checked" and (state.get('runtimeCompletion') or resource.get("revision") == dirty.get(resource["resourceId"])):
            completed.append("执行者检查命令执行成功，覆盖范围需按实际回执核对")
        elif resource.get("status") != "checked" and not resource.get('pending'):
            missing("check", resource["resourceId"], "executor", "执行者检查尚未通过或缺少有效回执", resource.get("status", "unknown"))
        for gap in resource.get('pending', []):
            missing('check',gap.get('operationId', resource['resourceId']), 'executor',
                    gap.get('text') or '当前版本复验未完成',gap.get('status','unknown'))
    for item in state.get("executorAcceptance", {}).get("requirements", []):
        if item["status"] not in {"passed", "confirmed"}:
            missing("requirement", item["id"], "executor", title(item["text"]) + "：尚缺当前版本证据", item["status"])
    if state.get("executorAcceptance", {}).get("ok") is False and not state["executorAcceptance"].get("requirements"):
        missing("check", "acceptance", "executor", "必验项状态暂时无法核对", "unknown")
    if state.get("backgroundReviewer", True):
        for rid, revision in targets.items():
            row = selected_rows.get(rid)
            if row and row.get("state") == "completed" and row.get("verdict") == "pass":
                completed.append("当前候选已提交独立通过意见")
            elif row:
                labels = {"queued": "尚未开始", "running": "正在核对", "paused": "已暂停", "timed_out": "已超时"}
                text = labels.get(row["state"], "需要补充证据或修复问题")
                covered = {c.get("requirementId") for c in row.get("coverage", []) if c.get("status") == "covered" and c.get("actionIds")}
                gaps = [c for c in row.get("criteria", []) if c["id"] not in covered]
                if row.get("state") in {"queued", "running"} or not gaps:
                    missing("review", row["id"], "reviewer", "独立复核" + text, row["state"])
                if row.get("state") not in {"queued", "running"}:
                    for criterion in gaps:
                        missing("review_requirement", criterion["id"], "reviewer", title(criterion["text"]) + "：独立复核尚未覆盖")
            else:
                missing("review", rid, "reviewer", "当前改动尚未送交独立复核", "not_run")
        for key in state.get("reviewUnits", {}).values():
            row = state.get("candidates", {}).get(key, {})
            if row.get("resourceId") in targets and row.get("verdict") != "pass":
                missing("review_unit", key, "reviewer", row.get("reason", "验收项") + "：尚无独立通过回执", row.get("state", "unknown"))
    for item in state.get("workQueue", {}).get("items", {}).values():
        if (item.get("kind") == "defect" and item.get("state") != "verified"
                and item.get("resourceId") in targets
                and (item.get("sourcePath") is None or item.get("sourcePath") == selected_rows.get(item["resourceId"], {}).get("sourcePath"))):
            missing("defect", item["id"], "reviewer", item["text"] + "：尚待当前版本定点复验", item["state"])
    outcome = state.get("executorOutcome", {})
    repairs = state.get("assistantRepairs", {})
    for grant in repairs.get('grants', {}).values():
        job = grant.get('job', {})
        if grant.get('state') == 'active' and job.get('state') in {'queued', 'running', 'paused', 'failed'}:
            missing('repair', grant['id'], 'reviewer', '已授权修复尚未交付：' + title(grant['goal']), job['state'])
    for p in repairs.get("patches", {}).values():
        grant = repairs.get("grants", {}).get(p["grantId"], {})
        if (grant.get("state") == "active" and p.get("application", {}).get("state") != "applied"
                and (p.get("decision") or {}).get("value") != "reject"):
            missing("implementation", p["id"], "executor", "助手补丁尚待处理：" + title(p["summary"]))
    if outcome.get("status") == "needs_human":
        missing("question", "user", "user", outcome.get("why") or "等待你的答复", "waiting_human")
    phase = state.get("state", "running")
    # 原生执行者正常结束可与验收缺口并存；完成卡分别呈现运行结束、检查事实和已登记要求。
    if state.get('runtimeCompletion') and phase == 'success':
        status = 'completed'
        label = ('本轮已结束 · 仍有未验收事项' if pending else
                 '本轮已结束 · 有失败或未确认检查' if state.get('checkSummary') else
                 '本轮已结束 · 已登记验收条件满足' if state.get('executorAcceptance',{}).get('requirements') and completed else
                 '本轮已结束 · 检查命令已完成' if completed else '本轮已结束')
    elif phase == "success" and not pending:
        status, label = "completed", "已完成 · 验收条件已满足"
    elif phase == "needs_human":
        status, label = "waiting_user", "等待你的答复"
    elif phase in {"paused", "needs_human", "interrupted", "failed", "error", "budget", "stuck"}:
        status, label = "paused", "已暂停 · 尚有事项未完成"
        if phase in {'failed', 'error'}:
            status, label = 'failed', '运行出错 · 成果未验收'
        elif phase == 'budget':
            label = '输出或调用额度受限 · 本轮未正常结束'
        elif phase == 'stuck':
            label = '执行停滞 · 保留进度与回执'
        if not pending:
            missing("execution", "executor", "executor", outcome.get("why") or state.get("pause", {}).get("reason") or "运行未正常完成，请核对实际回执", phase)
    elif state.get('waitingForAssistance'):
        status, label = 'waiting', '等待测试者结果 · 尚未完成验收'
    elif declaration.get("status") == "ready" and pending:
        status, label = "waiting", "执行已提交 · 等待验收"
    else:
        status, label = "running", "进行中"
    result = dict(status=status, label=label, completed=list(dict.fromkeys(completed)), pending=pending)
    if state.get('checkSummary'):
        result['checkSummary'] = state['checkSummary']
    return result


def behavior_enabled():
    import os
    return os.environ.get("HARNESS_ASSISTANCE_BEHAVIOR", "full") != "performance-only"


def wait_for_assistance(owner, should_stop=None):
    """Collect an already running review at a model boundary, without declaring ready.

    No new review is scheduled here. A patch can wake the executor before the
    reviewer finishes; optional scouting alone never holds final delivery open.
    """
    from .assistant_patches import jobs, pending
    stopped = lambda: owner._review_stop() or bool(should_stop and should_stop())
    if getattr(owner, "serial_review", False) or not owner.executor_led or stopped() or pending(owner):
        return False
    with owner.lock:
        reviews = [r for r in owner.state['candidates'].values() if r['state'] in {'queued', 'running'}]
        repairs = [g['job'] for g in jobs(owner)]
        if not reviews and not repairs:
            return False
        watched = reviews + repairs
        owner.state['waitingForAssistance'] = dict(started=time.time(), candidates=[r['id'] for r in reviews])
        owner.save()
    started = time.monotonic()
    owner.session.emit('assistant_wait', role='reviewer', phase='continuation', state='waiting')
    try:
        # The existing reviewer deadline and stop signal remain authoritative.
        # Workers themselves retain their bounded model/tool timeouts.
        while not stopped():
            with owner.lock:
                if any(r['state'] not in {'queued', 'running'} for r in watched):
                    break
            if pending(owner):
                break
            time.sleep(.1)
    finally:
        with owner.lock:
            owner.state.pop('waitingForAssistance', None)
            owner.save()
        owner.session.emit('assistant_wait', role='reviewer', phase='continuation', state='returned',
                           elapsedMs=round((time.monotonic() - started)*1000))
    return True


def refresh(owner, *, targets=None):
    """Refresh existing receipts before asking the executor to settle delivery."""
    from .delivery_review import executor_gate
    summary = C.executor_verification(Path(owner.session.dir).name, owner.session.run_id)
    acceptance = executor_gate(owner.session.dir, owner.task_input, **({'targets': targets} if targets is not None else {})) if owner.task_input else dict(ok=True, requirements=[])
    with owner.lock:
        owner.state['executorChecks'] = summary or dict(status='checked', resources=[])
        owner.state['executorAcceptance'] = acceptance
        owner.save()
    return summary, acceptance


def preflight(owner):
    """Return gaps to the model once per changed evidence set; never forgive a failure."""
    try:
        checks, acceptance = refresh(owner)
        gaps = dict(checks=checks, acceptance=acceptance)
    except (OSError, ValueError, W.Problem) as error:
        gaps = dict(unavailable=str(error)[:400])
        checks, acceptance = dict(status='unknown'), dict(ok=False)
    if (not checks or checks['status'] == 'checked') and acceptance['ok']:
        owner.state.pop('deliveryGaps', None)
        owner.state.pop('deliveryGapFingerprint', None)
        return None
    fingerprint = W.digest(json.dumps(gaps, sort_keys=True, ensure_ascii=False).encode())
    repeated = owner.state.get('deliveryGapFingerprint') == fingerprint
    with owner.lock:
        owner.state.update(deliveryGaps=gaps, deliveryGapFingerprint=fingerprint)
        owner.state.pop('deliveryReady', None)
        owner.save()
    owner.session.emit('delivery_gaps', repeated=repeated, **gaps)
    return ('paused' if repeated else 'repair', '当前交付缺项未解决，保留失败回执' if repeated else
            '请处理当前版本缺项：修复真实失败或补齐必验；探索受阻不代表验收通过')


def feedback(owner, messages):
    if owner.state.get('deliveryGaps'):
        from .working_context import append_update
        append_update(messages, 'delivery-gaps', '交付前发现以下缺项，请核对。真实失败不能忽略；'
            '错误测试入口可用同版本等价成功回执调用 replace_failed_check_entry。'
            '不能把已失败的验收改成探索来放行。无法补齐时明确报告阻塞，保留未完成状态。\n'
            + json.dumps(owner.state['deliveryGaps'], ensure_ascii=False))


def install(owner, registry):
    string = dict(type="string")
    registry.add(Tool("report_execution_status", "工程执行进展与最终交付协议。remaining 列执行者尚未完成的实现/合并/自检，不包含助手正在进行的复核。"
        "实现和自检完成可记录ready（remaining=[]），后台复核另记结果；ready不表示整轮验收通过。in_progress 表示实现仍需继续。"
        "可用此工具记录进展，不是结束本轮的必要声明。最终答复结束运行，不表示检查或独立验收通过。没有证据不要声称完成。"
        "先补 completion 中的必要缺项；测试者的非阻塞建议不自动成为需求。采纳额外改动会产生新版本与验证责任。",
        dict(type="object", properties=dict(status=dict(type="string", enum=["in_progress", "ready"]),
            summary=string, remaining=dict(type="array", items=string)), required=["status", "summary", "remaining"]),
        lambda **args: report(owner, **args), zone="offflow", kind="meta"))
    registry.add(Tool("replace_failed_check_entry", "为错误测试入口登记有效替代，不删除原失败。仅允许环境/入口失败被同版本、相同测试选择的成功入口替代；业务断言失败不能这样关闭。",
        dict(type="object", properties=dict(failed_action_id=string, passed_action_id=string, reason=string),
             required=["failed_action_id", "passed_action_id", "reason"]),
        lambda **args: replace(owner, **args), zone="offflow", kind="meta"))


# ready 是绑定当前任务和代码版本的交付声明；它不能替代自动证据、独立复核或用户验收。
def report(owner, status, summary, remaining):
    owner._ensure_running()
    if (status not in {"in_progress", "ready"} or not isinstance(summary, str) or not 1 <= len(summary) <= 3000
            or not isinstance(remaining, list) or len(remaining) > 64
            or any(not isinstance(item, str) or not 1 <= len(item) <= 1000 for item in remaining)
            or status == "ready" and remaining):
        raise W.Problem("invalid_completion", "ready 不允许剩余待办；提供进展摘要和 remaining 清单")
    with owner.lock:
        resources = set(owner.state["dirty"])
    revisions = {rid: owner._current_revision(rid) for rid in resources}
    if any(value is None for value in revisions.values()):
        raise W.Problem("completion_version_unknown", "无法确认交付代码版本", 409)
    if status == "ready":
        from .delivery_review import executor_gate
        try:
            _, acceptance = refresh(owner,
                targets={("code", rid): dict(kind="code", id=rid, revision=revision) for rid, revision in revisions.items()})
        except (W.Problem, OSError, ValueError):
            acceptance = dict(ok=False, requirements=[])
    with owner.lock:
        if status == "ready":
            owner.state["executorAcceptance"] = acceptance
        owner.state["executionDeclaration"] = dict(status=status, summary=evidence.redact(summary),
            remaining=evidence.redact(remaining), revisions=revisions, taskBinding=owner._task_binding())
        owner.save()
    return dict(ok=True, status=status, revisions=revisions, completion=owner.state.get("completion"),
                instruction="继续执行" if status == "in_progress" else "执行已提交，按 completion 核对剩余项；等待复核不代表验收通过")


def ready(owner):
    if getattr(owner, "runtime_completion", False) or getattr(owner, "serial_review", False) or not owner.executor_led or not behavior_enabled():
        return True
    with owner.lock:
        dirty = dict(owner.state["dirty"])
        declaration = owner.state.get("executionDeclaration", {})
    return (declaration.get("status") == "ready" and declaration.get("taskBinding") == owner._task_binding()
            and set(declaration.get("revisions", {})) == set(dirty)
            and all(owner._current_revision(rid) == revision for rid, revision in declaration.get("revisions", {}).items()))


def _selectors(argv):
    """Only known test runners; do not infer arbitrary shell command equivalence."""
    if not isinstance(argv,list) or not argv or any(not isinstance(v,str) for v in argv):return None
    args = list(argv[1:])
    if len(args)==2 and args[0]=='-c':
        from .check_entry import django_wrapper
        recognized=django_wrapper(args[1])
        return _selectors(recognized) if recognized else None
    if args[:3] == ["-m", "django", "test"]:
        family = "django"
        args = args[3:]
    elif args[:2] == ['-m', 'tests.runtests']:
        family = 'django'
        args = args[2:]
    elif args and args[0].replace("\\", "/") in {"tests/runtests.py", "./tests/runtests.py"}:
        family = "django"
        args = args[1:]
    elif args[:2] in (["-m", "pytest"], ["-m", "unittest"]):
        family = args[1]
        args = args[2:]
    else:
        return None
    result, skip = [], False
    settings = 'test_sqlite' if family=='django' else None
    if '--settings' in args:
        index=args.index('--settings')
        if index+1>=len(args):return None
        settings=args[index+1]
    for arg in args:
        if arg.startswith('--settings='):settings=arg.split('=',1)[1]
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in {"--settings", "-v", "--verbosity", "-n", "--parallel"}:
            skip = True
        elif arg.startswith("--settings=") or arg in {'-q', '--quiet', '--verbose', '--noinput'}:
            continue
        elif arg.startswith("-"):
            return None
        else:
            result.append(arg)
    return (family, tuple(sorted(result)), settings) if result and not skip else None


def replace(owner, failed_action_id, passed_action_id, reason):
    owner._ensure_running()
    if not isinstance(reason, str) or not 1 <= len(reason) <= 1000:
        raise W.Problem("invalid_arguments", "说明错误入口和替代入口的等价范围")
    ident = Path(owner.session.dir).name
    with C.database() as db:
        rows = [json.loads(raw) for raw, in db.execute("SELECT data FROM operations WHERE thread=?", (ident,))]
    get = lambda action: next((row for row in rows if row.get("source", {}).get("actionId") == action
                               and row.get("source", {}).get("runId") == owner.session.run_id
                               and row.get("source", {}).get("role", "executor") == "executor"), None)
    failed, passed = get(failed_action_id), get(passed_action_id)
    if not failed or not passed or failed["operation"] != "check" or passed["operation"] != "check":
        raise W.Problem("check_replacement_denied", "需要本轮执行者的真实检查回执", 409)
    replacement=validate_entry_replacement(failed,passed,owner._current_revision(failed['resourceId']),reason)
    failed["entryReplacement"] = replacement
    with C.database() as db:
        db.execute("UPDATE operations SET data=? WHERE thread=? AND id=?",
                   (json.dumps(failed), ident, failed["id"]))
    with owner.lock:
        for row in owner.state.get("troubleshooting", {}).values():
            if row.get("latestActionId") == failed_action_id or row.get("blockedArgv") == failed["target"]["argv"]:
                row.update(state="closed", closure=dict(reason="equivalent_entry_passed", actionId=passed_action_id,
                    revision=passed["beforeRevision"]))
        owner.save()
    return dict(ok=True, originalFailureRetained=True, replacement=replacement)


def validate_entry_replacement(failed, passed, current, reason, failed_script=None, passed_script=None):
    """Validate existing evidence; no state writes or inferred coverage."""
    if not isinstance(reason,str) or not 1<=len(reason)<=1000:
        raise W.Problem('invalid_arguments','说明入口更正依据和相同验证范围')
    if not failed or not passed or failed.get('operation')!='check' or passed.get('operation')!='check':
        raise W.Problem('check_replacement_denied','需要真实检查回执',409)
    f, p = failed.get("result", {}), passed.get("result", {})
    errors = f.get("diagnostics", {})
    # ERROR localizes an errored unittest case; it does not imply an assertion.
    # Environment, selection, version and assertion/count guards still apply.
    failed_cases=[case for case in errors.get('failedCases',[]) or []
        if not (isinstance(case,dict) and C._UNITTEST_ERROR.match(str(case.get('text',''))))]
    blocker = f.get("environmentBlocker") or C.check_blocker(f.get("text"))
    same_selection=(_selectors(failed['target'].get('argv',[])) is not None
        and _selectors(failed['target']['argv'])==_selectors(passed['target'].get('argv',[])))
    import_repair=_probe_import_repair(failed_script,passed_script,f.get('text',''))
    same_selection=same_selection or import_repair
    blocker=blocker or import_repair
    # 更正检查入口仅适用于同版本/环境/选择范围的有效替代；真实断言失败必须保留，不能借替代消除。
    if (not blocker or failed_cases or f.get("verification", {}).get("status") not in {"failed", "blocked"}
            or p.get("verification", {}).get("status") != "passed"
            or any(failed.get(k) != passed.get(k) for k in ("resourceId", "workspaceId", "beforeRevision"))
            or failed.get('beforeRevision') is None or failed["beforeRevision"] != current
            or passed["target"].get("environment") not in {None, C.check_environment()}
            or any(failed["target"].get(k) != passed["target"].get(k) for k in
                ("environment", "material", "retain_output",'environment_id','writable_copy','capture_changes'))
            or failed['target'].get('purpose','acceptance')!=passed['target'].get('purpose','acceptance')
            or errors.get('counts',{}).get('failed') or 'AssertionError' in json.dumps(errors)
            or not same_selection):
        raise W.Problem("check_replacement_denied", "替代需同版本、同环境、相同测试选择和有效通过；断言失败不允许替代", 409)
    return dict(operationId=passed["id"], actionId=passed['source']['actionId'],
        revision=passed["beforeRevision"], reason=evidence.redact(reason))


def _probe_import_repair(before, after, output):
    """Only added imports for a missing name; arbitrary script changes are unknown."""
    import ast
    import re
    missing=re.search(r"NameError: name ['\"](\w+)['\"] is not defined",output or '')
    if not missing or '.harness-check' not in output or 'input.py' not in output:
        return False
    if not isinstance(before,str) or not isinstance(after,str):return False
    try:old,new=ast.parse(before),ast.parse(after)
    except (SyntaxError,ValueError):return False
    imports=lambda tree:[node for node in tree.body if isinstance(node,(ast.Import,ast.ImportFrom))]
    dump=lambda nodes:[ast.dump(n,include_attributes=False) for n in nodes]
    bodies=lambda tree:[node for node in tree.body if not isinstance(node,(ast.Import,ast.ImportFrom))]
    original=dump(imports(old));added=[n for n in imports(new) if ast.dump(n) not in original]
    bound={a.asname or (a.name.split('.')[0] if isinstance(n,ast.Import) else a.name) for n in added for a in n.names}
    return (dump(bodies(old))==dump(bodies(new)) and set(original)<=set(dump(imports(new)))
        and bound=={missing[1]} and any(isinstance(n,ast.Assert) for n in ast.walk(old)))
