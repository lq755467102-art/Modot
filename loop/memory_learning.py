"""Evidence-based promotion and durable, version-bound near-weight conflicts."""

# 主要引用方包括 `tools.py` 和 `projects.py`。

import time
from . import memory_service as M, workbench as W
from .memory_tree import digest, error, text

POLICY = dict(version=2, closeMargin=5, independentOccurrences=2)


# 升格依据限定在当前节点版本的合格佐证；实际采用、反例与单纯带入分别处理。
def eligible(node, target_scope=None):
    evidence = [
        e
        for e in node.get("evidence", [])
        if e.get("factVersion", node["version"]) == node["version"]
    ]
    target_scope = target_scope or ('project' if node.get('scope') == 'session' else 'habit')
    uses = [e for e in evidence if e['kind'] == 'applied' and
        (e.get('operationId') and e.get('runId') or e.get('reportedBy') == 'human')]
    independent = {(e.get('conversationId'), e.get('logicalTaskId')) for e in uses}
    projects = {e.get('projectId') for e in uses if e.get('projectId')}
    reasons = []
    if node['tree'] != 'facts' and not (node.get('scope') == 'project' and target_scope == 'habit'):
        reasons.append('仅会话采集或项目条目可升格')
    if node["state"] != "active":
        reasons.append("节点已关闭或退役")
    if target_scope == 'project' and len(independent) < POLICY['independentOccurrences']:
        reasons.append('缺少两个独立任务的实际使用依据；同任务的带入/检查/重试不算复发')
    if target_scope == 'habit' and len(projects) < 2:
        reasons.append('缺少第二项目中独立适用的实际使用与检查依据')
    if target_scope == 'habit' and node.get('branches'):
        reasons.append('原条目仍限定分支，未自动扩大为跨项目习惯；需用户先核对适用范围')
    if (target_scope == 'habit' or node.get('entryType') == 'rule') and not any(
            e['kind'] == 'challenge' and e.get('outcome') == 'not_found' for e in evidence):
        reasons.append('尚未记录具体反例尝试；没有反例记录不等于已经审查')
    if not any(e["kind"] == "human-confirmed" for e in evidence):
        reasons.append("泛化适用性尚未得到用户确认")
    if any(
        e["kind"] == "counterexample" and not e.get("resolvedAt")
        for e in node.get("evidence", [])
    ):
        reasons.append("存在未解决反例")
    return dict(
        eligible=not reasons,
        reasons=reasons,
        policy=POLICY,
        scope=target_scope,
        notice="资格仅用于候选筛选，不等于自动批准；带入不证明采用，检查不证明与记忆的语义关联或任务验收。人工确认与反例仍须核对。",
    )


def record_injection(ident, run_id, ids):
    # Metadata updates belong to each source store, not the selection control row.
    groups = {}
    for item in ids:
        nid = item['id'] if isinstance(item, dict) else item
        try:
            store = M.node_target(ident, nid)[0]
        except W.Problem as exc:
            if exc.code == 'not_found':
                continue
            raise
        groups.setdefault(str(store.path), (store, []))[1].append(item)
    for store, selected in groups.values():
        _record_injection(store, ident, run_id, selected)
    from . import habits as H
    H.record_memory_injection(ident, run_id, ids)


# 计算当前节点版本的带入机会和实际采用；未解决反例的状态跨版本保留。
def learning_stats(node):
    current = [e for e in node.get('evidence', []) if e.get('factVersion') == node['version']]
    applied = [e for e in current if e['kind'] == 'applied']
    opportunities = {(e.get('conversationId'), e.get('runId')) for e in current if e['kind'] == 'injected'}
    review = ('found_unresolved' if any(e['kind']=='counterexample' and not e.get('resolvedAt') for e in node.get('evidence', []))
        else 'tried_not_found' if any(e['kind']=='challenge' and e.get('outcome')=='not_found' for e in current)
        else 'not_reviewed')
    return dict(opportunities=len(opportunities), applied=len({e.get('operationId') or 'human:'+e['conversationId'] for e in applied}),
        by_project={pid: len({(e['conversationId'], e.get('logicalTaskId')) for e in applied if e.get('projectId')==pid})
            for pid in {e.get('projectId') for e in applied} if pid},
        counterexampleReview=review, adoptions=node.get('adoptions', {}),
        notice='带入机会与显式使用分开计数；次数不决定真实性或自动权重')


def _record_injection(store, ident, run_id, ids):
    """A successful request carried selected facts; it does not prove adoption."""
    initial = store.load()
    pending = []
    for item in ids:
        nid = item["id"] if isinstance(item, dict) else item
        n = initial["nodes"].get(nid)
        if not n or isinstance(item, dict) and n["version"] != item["version"]:
            continue
        if not any(
            e["kind"] == "injected"
            and e["runId"] == run_id
            and e.get("factVersion") == n["version"]
            for e in n["evidence"]
        ):
            pending.append((nid, n["version"]))
    if not pending:
        return

    def apply(row):
        for nid, version in pending:
            if row["nodes"].get(nid, {}).get("version") == version:
                row["nodes"][nid]["evidence"].append(
                    dict(
                        kind="injected",
                        runId=run_id,
                        conversationId=ident,
                        factVersion=version,
                        at=time.time(),
                    )
                )
        return {}

    try:
        store.change(initial["revision"], "runtime", apply)
    except W.Problem as e:
        if e.code != "memory_stale":
            raise


# 这段把人工确认、反例和工具佐证整理成候选证据。
# 模型提交的反例还须用 `tools.current_call` 绑定到当前会话和运行。
def evidence(ident, actor, body):
    kind = body.get("kind")
    if (
        kind in {"human-confirmed", "counterexample", "resolve-counterexample"}
        and actor == "human"
    ):
        return dict(
            kind=kind,
            conversationId=ident,
            note=text(body.get("note", "用户确认此范围可复用")),
            at=time.time(),
            projectId=M.target(ident)[2],
        )
    if kind in {'challenge', 'counterexample'}:
        from .tools import current_call
        owner = current_call() or {}
        if actor != 'human' and (owner.get('sessionId') != ident or not owner.get('runId')):
            error('反例记录需要当前真实动作身份', 'memory_evidence_denied', 403)
        attempt = text(body.get('attempt'), 2000)
        outcome = 'found' if kind == 'counterexample' else body.get('outcome')
        if outcome not in {'found', 'not_found'}:
            error('反例结果必须明确为 found 或 not_found')
        return dict(kind='counterexample' if outcome == 'found' else 'challenge',
            attempt=attempt, outcome=outcome, note=text(body.get('note', attempt), 2000),
            conversationId=ident, projectId=M.target(ident)[2],
            actionId=owner.get('actionId'), runId=owner.get('runId'), at=time.time())
    if kind not in {'check', 'applied'}:
        error("验证证据不能由模型自行声明", "memory_evidence_denied", 403)
    from .code_service import operation

    used_by = body.get('conversationId', ident)
    if used_by != ident and actor != 'human':
        error('模型只能报告当前会话的实际使用', 'memory_evidence_denied', 403)
    target = M.projects.conversation(used_by)
    if target.get('readonlyEvidence') or target.get('subtaskOf'):
        error('审查/子任务不能充当独立复发', 'memory_evidence_denied', 403)
    if kind == 'applied' and not body.get('operationId') and actor == 'human':
        return dict(kind='applied', reportedBy='human', conversationId=used_by,
            projectId=target.get('projectId'), note=text(body.get('note'), 2000), at=time.time())
    op = operation(used_by, body.get("operationId"))
    result = op.get("result", {})
    process = result.get("process", {})
    if op.get("operation") != "check":
        error("仅接受真实检查操作", "memory_evidence_denied", 403)
    selected = {
        r["id"]
        for r in M.projects.scope(used_by)["resources"]
        if r["permission"] == "read"
    }
    if op.get("resourceId") not in selected:
        error("检查来源已不在读取范围", "memory_evidence_denied", 403)
    if (
        op.get("state") not in {"completed", "succeeded"}
        or result.get("ok") is False
        or process.get("exitCode", result.get("exitCode")) != 0
    ):
        error("检查未实际完成或未通过，不能记成验证", "memory_evidence_denied", 409)
    return dict(
        kind=kind,
        operationId=op["id"],
        runId=op.get("source", {}).get("runId") or op.get("runId") or op.get("run_id"),
        logicalTaskId=op.get('source', {}).get('logicalTaskId'),
        conversationId=used_by,
        projectId=target.get('projectId'),
        resourceId=op['resourceId'],
        ok=True,
        at=time.time(),
        note=text(body.get('note'), 2000) if kind == 'applied' else "真实检查通过；与事实的语义关联仍需复核",
    )


# 为比较冲突保留当前会话可读和可用的候选节点。
def available_choices(ident):
    return {
        node["id"]: node
        for node in M.resolve(ident, details=False)["nodes"]
        if node["selected"] and node["state"] == "active"
    }


# 比较冲突当时选中的节点版本是否仍与当前可用候选一致。
def current_conflict(conflict, choices):
    return all(
        choices.get(node["id"], {}).get("version") == node["version"]
        for node in conflict["nodes"]
    )


# 分流候选升格、佐证和冲突决定；有实际工具身份的事件先解析，再进入版本锁。
def mutate(ident, store, expected, actor, body):
    op = body["operation"]
    if op == "promote":
        return propose(ident, store, expected, actor, body)
    event = evidence(ident, actor, body) if op == "evidence" else None

    # 在记忆库的当前行内重验节点和授权。佐证与决定保留原版本绑定。
    def apply(row):
        node = row["nodes"].get(body.get("id"))
        if op in {"evidence", "promote"}:
            if not node or not M.permitted(
                node,
                M.projects.conversation(ident),
                M.target(ident)[2],
            ):
                error("事实不可读取", "memory_denied", 403)
        if op == "evidence":
            if event["kind"] == "resolve-counterexample":
                for old in node["evidence"]:
                    if old["kind"] == "counterexample":
                        old.update(resolvedAt=time.time(), resolvedBy="human")
            event["factVersion"] = node["version"]
            key = digest({k: v for k, v in event.items() if k not in {"at", "note"}})
            if not any(e.get("key") == key for e in node["evidence"]):
                node["evidence"].append(dict(event, key=key))
            return dict(id=node["id"], qualification=eligible(node))
        if op == "compare":
            ids = body.get("ids", [])
            if (not isinstance(ids, list) or len(ids) != 2
                    or any(not isinstance(i, str) for i in ids) or ids[0] == ids[1]):
                error("请选择两个互斥做法的节点")
            choices = available_choices(ident)
            if any(i not in choices for i in ids):
                error("冲突节点不适用", "memory_denied", 403)
            nodes = [choices[i] for i in ids]
            question = text(body.get("question"), 1000)
            signature = digest(
                [ident, [(n["id"], n["version"], n["weight"]) for n in nodes], question]
            )
            existing = row["conflicts"].get(signature)
            if existing:
                pending = existing["state"] == "pending"
                return dict(conflict=existing, needs_human=pending, question=question,
                            options=[n["title"] for n in existing["nodes"]] if pending else [],
                            memoryConflictId=existing["id"] if pending else None)
            close = (
                abs(nodes[0]["weight"] - nodes[1]["weight"]) <= POLICY["closeMargin"]
            )
            value = dict(
                id=signature,
                conversationId=ident,
                question=question,
                nodes=[
                    dict(
                        id=n["id"],
                        version=n["version"],
                        title=n["title"],
                        weight=n["weight"],
                    )
                    for n in nodes
                ],
                state="pending" if close else "recommended",
                selected=None if close else max(nodes, key=lambda n: n["weight"])["id"],
                policy=POLICY,
                at=time.time(),
            )
            row["conflicts"][signature] = value
            return dict(
                conflict=value,
                needs_human=close,
                memoryConflictId=value["id"] if close else None,
                question=question,
                options=[n["title"] for n in nodes] if close else [],
            )
        if op == "decide":
            if actor != "human":
                error("只能由用户解决冲突", "memory_denied", 403)
            conflict = row["conflicts"].get(body.get("conflictId"))
            if not conflict or conflict["conversationId"] != ident:
                error("冲突不存在", "not_found", 404)
            if not current_conflict(conflict, available_choices(ident)):
                error("冲突依据已更新，请重新核对", "memory_stale", 409)
            if body.get("selected") not in [n["id"] for n in conflict["nodes"]]:
                error("请选择其中一个做法")
            for n in conflict["nodes"]:
                if n["id"] in row["nodes"]:
                    row["nodes"][n["id"]]["evidence"].append(
                        dict(
                            kind="human-decision",
                            conversationId=ident,
                            selected=body["selected"] == n["id"],
                            at=time.time(),
                            conflictId=conflict["id"],
                        )
                    )
            conflict.update(
                state="resolved",
                selected=body["selected"],
                actor="human",
                resolvedAt=time.time(),
            )
            return dict(
                conflict=conflict, notice="仅当前会话/此版本决定，不自动改写习惯"
            )
        error("未知学习操作")

    return store.change(expected, actor, apply)


# 根据待确认候选的来源重找记忆库，项目变化时要求重新提出候选。
def adopt_scoped(proposal):
    source = proposal["memorySource"]
    ident = source["conversationId"]
    store, t, pid = M.node_target(ident, source['factId'])
    if not pid or pid != source["projectId"]:
        error("候选工作区已变化", "memory_stale", 409)
    row = store.load()
    published = False
    def apply(current):
        nonlocal published
        result = _adopt_from_row(proposal, current, t, pid)
        published = result.get('ok', True)
        return result
    try:
        return store.change(row['revision'], 'human', apply)
    except W.Problem as error:
        if published:
            error.memory_write_unknown = True
        raise


# 只有候选仍指向当前可用节点和同一证据时才升格。结果保留源记录及来源权重。
def _adopt_from_row(proposal, row, t, pid):
    source = proposal['memorySource']
    ident = source['conversationId']
    node = row["nodes"].get(source["factId"])
    if (
        not node
        or not M.permitted(node, t, pid)
        or node["version"] != source["factVersion"]
        or not qualification(node, source['targetScope'])["eligible"]
        or evidence_version(node) != source["evidenceVersion"]
    ):
        error("候选依据已变化，请重新评估", "memory_stale", 409)
    if node.get("habitProposal", {}).get("id") != proposal["id"]:
        error("候选未完成提交", "memory_stale", 409)
    target_scope = source['targetScope']
    lineage = dict(sourceId=node['id'], sourceVersion=node['version'],
        sourceScope=node.get('scope'), conversationId=ident, projectId=pid, proposalId=proposal['id'])
    if target_scope == 'habit':
        from . import habits as H
        result = H.adopt_memory(proposal['title'], proposal['body'], dict(
            id='habit-' + digest([pid, node['id']])[:24], proposalId=proposal['id'],
            weight=node['weight'], priority=node['priority'], promotion=lineage,
            sources=node['sources'], evidence=[dict(e, factVersion=1, origin=lineage)
                for e in node['evidence'] if e.get('factVersion') == node['version'] and e['kind'] != 'injected'],
            created=time.time(), updated=time.time()))
    else:
        destination = M.target(ident, scope='project')[0]
        dest_row = destination.load()
        key = 'derived:' + node['id']
        present = next((n for n in dest_row['nodes'].values() if n['key'] == key), None)
        if present and present.get('promotion') == lineage:
            result = dict(id=present['id'], version=present['version'], duplicate=True)
        else:
            is_rule = node.get('entryType') == 'rule'
            layer = (['项目规则'] + node['layer'][:5]) if is_rule and node['layer'][0] != '项目规则' else node['layer']
            result = destination.upsert(dest_row['revision'], 'human', dict(
                tree='habits' if is_rule else 'facts', key=key, title=proposal['title'], body=proposal['body'],
                layer=layer, weight=node['weight'], priority=node['priority'],
                branches=node.get('branches', []), kind=node.get('kind', 'observation'),
                scope='project', ownerId=pid, entryType=node.get('entryType', 'fact'),
                promotion=lineage), dict(conversationId=ident, projectId=pid, actor='human',
                    factId=node['id'], factVersion=node['version'],
                    resourceIds=sorted({rid for s in node['sources'] for rid in s.get('resourceIds', [])})),
                adoption=dict(lineage=lineage, sourceVersion=node['version'], evidence=node['evidence']))
    if result.get('ok') is False:
        return result
    node.setdefault('adoptions', {})[target_scope] = dict(lineage, targetId=result['id'])
    node['habitProposal']['status'] = 'adopted'
    return dict(
        result, ok=True,
        notice='已采纳到跨项目习惯；正文仍只存 HABITS.md' if target_scope == 'habit' else '已采纳到项目；会话来源和证据保留',
    )


# 把待确认候选和当前版本的证据绑定，供采纳期间重验。
def evidence_version(node):
    return digest([e for e in node.get('evidence', []) if e['kind'] != 'injected'])


def qualification(node, scope):
    """Recheck operation/source grants at submission and human adoption."""
    from copy import deepcopy
    fresh = deepcopy(node)
    current = []
    for e in fresh['evidence']:
        if e['kind'] == 'applied':
            try:
                checked = evidence(e['conversationId'], 'human', dict(kind='applied',
                    operationId=e.get('operationId'), note=e['note']))
                if checked.get('projectId') != e.get('projectId'):
                    continue
            except W.Problem:
                continue
        current.append(e)
    fresh['evidence'] = current
    return eligible(fresh, target_scope=scope)


# 从持久待回应状态核对原候选与决定上下文。
def pending_guard(ident):
    if not ident:
        return None
    from .memory_providers import default_active
    if not default_active():
        return None
    try:
        store, t, pid = M.target(ident)
    except W.Problem:
        return None
    row = store.load()
    owners = {ident}
    if t.get("subtaskOf"):
        owners.add(t["subtaskOf"]["parentId"])
    choices = {}
    for value in row["conflicts"].values():
        owner = value["conversationId"]
        if owner not in owners or value["state"] != "pending":
            continue
        if owner not in choices:
            choices[owner] = available_choices(owner)
        if current_conflict(value, choices[owner]):
            return dict(
                ok=False,
                needs_human=True,
                question=value["question"],
                options=[n["title"] for n in value["nodes"]],
                memoryConflictId=value["id"],
                error="记忆依据有接近权重的互斥做法，请先选择",
            )
    return None


# 把人工答复关联到原待回应记录后再允许后续动作。
def answer_pending(ident, answer, *, conflict_id=None):
    from .memory_providers import default_active
    if not default_active():
        return
    store, _, _ = M.target(ident)
    row = store.load()
    matches = []
    choices = None
    for conflict in row["conflicts"].values():
        if conflict["conversationId"] != ident or conflict["state"] != "pending":
            continue
        if conflict_id and conflict["id"] != conflict_id:
            continue
        if choices is None:
            choices = available_choices(ident)
        if not current_conflict(conflict, choices):
            continue
        matches.extend((conflict["id"], node["id"]) for node in conflict["nodes"]
                       if node["title"] == answer.strip())
    if len(matches) == 1:
        conflict, selected = matches[0]
        mutate(ident, store, row["revision"], "human",
               dict(operation="decide", conflictId=conflict, selected=selected))


# 候选携带当前记忆库 revision 和源节点版本。
# 仅按会话到项目、项目到习惯逐级升格；已采纳的同一源版本直接返回原采纳记录。
def propose(ident, store, expected, actor, body):
    row = store.load()
    if row["revision"] != expected:
        error("事实已更新", "memory_stale", 409)
    node = row["nodes"].get(body.get("id"))
    thread = M.projects.conversation(ident)
    pid = thread.get("projectId")
    if thread.get("subtaskOf"):
        error("习惯候选由父会话复核事实证据", "memory_denied", 403)
    if not pid or not node or not M.permitted(node, thread, pid):
        error("候选需要可读的工作区事实", "memory_denied", 403)
    target_scope = body.get('targetScope') or ('project' if node.get('scope') == 'session' else 'habit')
    if target_scope not in {'project', 'habit'} or (target_scope == 'project') != (node.get('scope') == 'session'):
        error('升格按会话到项目、项目到习惯进行')
    adopted = node.get('adoptions', {}).get(target_scope)
    if adopted and adopted['sourceVersion'] == node['version']:
        return dict(id=node['id'], promoted=True, adoption=adopted, revision=row['revision'],
            notice='此版本已经采纳；原来源、权重和采用记录保留')
    qualified = qualification(node, target_scope)
    if not qualified["eligible"]:
        return dict(
            id=node["id"],
            qualification=qualified,
            promoted=False,
            memoryWrite=dict(status='not_saved',reason='candidate_not_eligible'),
            revision=row["revision"],
        )
    from . import proposals as P

    source = dict(
        conversationId=ident,
        projectId=pid,
        factId=node["id"],
        factVersion=node["version"],
        evidenceVersion=evidence_version(node),
        targetScope=target_scope,
        resourceIds=list(thread.get("resourceIds", [])),
    )
    proposal = P.enqueue_memory(
        W.home(),
        ('项目·' if target_scope == 'project' else '用户习惯·') + pid + '·' + node['id'],
        text(body.get("title", node["title"]), 120),
        text(body.get("body", node["body"]), 8000),
        source,
    )
    if not proposal.get("ok"):
        error(proposal.get("error", "候选未保存"), "memory_proposal", 409)

    # 在写锁内复查候选条件，避免以旧佐证发布新版本。
    def apply(current):
        current["nodes"][node["id"]]["habitProposal"] = dict(
            id=proposal["id"],
            factVersion=node["version"],
            scope=target_scope,
            sourceEvidence=source["evidenceVersion"],
        )
        return dict(
            id=node["id"],
            qualification=qualified,
            proposal=current["nodes"][node["id"]]["habitProposal"],
            promoted=False,
        )

    return store.change(expected, actor, apply)


# 先找库内节点，再解析兼容的历史习惯并补回本地权重。
def lookup(row, ident):
    node = row["nodes"].get(ident)
    if node:
        return node
    node = next((n for n in M.external_habits(row) if n["id"] == ident), {})
    if node:
        node["weight"] = row.get("legacyWeights", {}).get(ident, 70)
    return node
