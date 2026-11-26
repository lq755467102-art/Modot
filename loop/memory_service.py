"""Workspace binding, two-tree views and controlled writes. No path comes from a client."""

# 主要引用方包括 `workbench.py` 和 `tools.py`。

from pathlib import Path
import time
from . import workbench as W, projects, execution_policy as E
from .memory_tree import TreeStore, digest, error, select


# 管理状态使用会话或项目对应的记忆库，由当前会话关联决定。
def target(ident=None, project_id=None, active=False, scope=None):
    thread = projects.conversation(ident) if ident else None
    if thread:
        from .subtasks import validate_child, audit_child

        child = validate_child(ident) if active else audit_child(ident)
        project_id = child["projectId"] if child else thread.get("projectId")
    if project_id:
        projects.project(project_id)
    if scope not in {None, 'session', 'project'} or scope == 'session' and not ident:
        error('记忆范围无效')
    key = ('session-' + ident) if scope == 'session' else (
        "project-" + project_id
        if project_id
        else "session-" + ident
        if ident
        else "preview-unassociated"
    )
    return TreeStore(key), thread, project_id


def scoped_rows(ident=None, project_id=None):
    """One selection view; each body still has exactly one owning store."""
    store, thread, pid = target(ident, project_id)
    rows = [('project' if pid else 'session', store, store.load())]
    if ident and pid:
        local = target(ident, scope='session')[0]
        rows.append(('session', local, local.load()))
    return rows, thread, pid


# 节点更新优先查原节点所在范围，新节点按请求作用域选仓。
def node_target(ident, node_id=None, scope=None):
    rows, thread, pid = scoped_rows(ident)
    if node_id:
        found = next(((store, row) for _, store, row in rows if node_id in row['nodes']), None)
        if not found:
            error('当前会话范围内没有此节点', 'not_found', 404)
        return found[0], thread, pid
    return target(ident, scope=scope or 'session')


# 将当前会话、项目和资料关系保存为节点来源。
def source_for(ident, actor="human", action_id=None):
    t = projects.conversation(ident)
    return dict(
        conversationId=ident,
        projectId=target(ident, active=actor == "model")[2],
        actor=actor,
        actionId=action_id,
        at=time.time(),
        resourceIds=source_resources(t),
    )


# 逐项核对节点的会话、项目和实际资料授权。已采纳习惯只放行归纳正文，证据另核对。
def permitted(node, thread, project_id):
    if node.get('scope') == 'session' and thread and node.get('ownerId') != thread['id']:
        return False
    if node.get('scope') == 'habit' and node.get('adopted'):
        return True  # Human-approved generalized body; evidence is separately scoped.
    for source in node.get("sources", []):
        if source.get("projectId") != project_id:
            return False
        if source.get("resourceIds"):
            p = projects.project(project_id) if project_id else {}
            selected = set(source_resources(thread)) if thread else set()
            for rid in source["resourceIds"]:
                r = p.get("resources", {}).get(rid)
                if (
                    not r
                    or r.get("permission") != "read"
                    or (thread is not None and rid not in selected)
                ):
                    return False
    return True


def external_habits(metadata=None):
    """User-authored HABITS entries; never the shipped preset-rule library."""
    from . import habits as H

    nodes = []
    try:
        managed_habits = {n['title']: n for n in H.memory_nodes()}
        outline = H.outline()
        for group in outline.get("groups", []):
            for leaf in group.get("leaves", []):
                nodes.append(
                    dict(
                        id="habit-" + digest(leaf["title"])[:24],
                        legacyId=leaf["title"],
                        tree="habits",
                        layer=["用户习惯", group.get("name", "")],
                        title=leaf["title"],
                        body=leaf.get("body", ""),
                        version=digest(leaf),
                        state=leaf.get("state", "active"),
                        selected=leaf.get("on", True),
                        adapter="habits",
                        reason="user_habit",
                        readonly=False,
                    )
                )
                current = managed_habits.get(leaf['title'])
                if current:
                    nodes[-1].update({k: current[k] for k in ('id', 'version', 'weight', 'scope')})
    except OSError:
        raise W.Problem("memory_unavailable", "原习惯区不可读取", 503)
    refs = (metadata or {}).get("legacyRefs", {})
    assigned = set()
    for ident, ref in refs.items():
        candidates = [
            i
            for i, n in enumerate(nodes)
            if n.get('scope') != 'habit' and n["adapter"] == ref["adapter"] and n["title"] == ref["title"]
        ]
        if not candidates:
            candidates = [
                i
                for i, n in enumerate(nodes)
                if n.get('scope') != 'habit' and n["adapter"] == ref["adapter"]
                and digest(n["body"]) == ref["bodyHash"]
            ]
        if len(candidates) == 1 and candidates[0] not in assigned:
            nodes[candidates[0]]["id"] = ident
            assigned.add(candidates[0])
    for node in nodes:
        node['weight'] = (metadata or {}).get('legacyWeights', {}).get(
            node['id'], node.get('weight', 70))
    return nodes


# 合并当前会话、项目范围与适用习惯，标明每个节点的可用性，再按载入模式和预算选择内容。
def resolve(
    ident=None,
    project_id=None,
    mode=None,
    max_chars=16000,
    branch=None,
    include_external_habits=False,
    details=True,
):
    rows, thread, pid = scoped_rows(ident, project_id)
    store, base = rows[0][1:]
    from copy import deepcopy
    row = deepcopy(base)
    revisions = {}
    for scope, owner, source in rows:
        revisions[scope] = source['revision']
        for nid, node in source['nodes'].items():
            row['nodes'][nid] = dict(deepcopy(node), scope=scope,
                ownerId=ident if scope == 'session' else pid, storeRevision=source['revision'])
        if owner.path != store.path:
            row['conflicts'].update(source['conflicts'])
            for mode_name, choices in source['choices'].items():
                dest = row['choices'].setdefault(mode_name, {'on': [], 'off': []})
                for k in ('on', 'off'):
                    dest[k] = list(set(dest[k]) | set(choices[k]))
            for cid, modes in source.get('conversationChoices', {}).items():
                for name, choices in modes.items():
                    dest = row.setdefault('conversationChoices', {}).setdefault(cid, {}).setdefault(name, {'on': [], 'off': []})
                    for k in ('on', 'off'):
                        dest[k] = list(set(dest[k]) | set(choices[k]))
    from . import habits as H
    for node in H.memory_nodes():
        node['weight'] = row.get('legacyWeights', {}).get(node['id'], node['weight'])
        row['nodes'][node['id']] = node
    selection_id = (thread or {}).get("subtaskOf", {}).get("parentId", ident)
    mode = mode or row.get("sessionModes", {}).get(selection_id, "full")
    if mode == "lean":
        max_chars = min(max_chars, 6000)
    if not isinstance(mode, str) or not mode or len(mode) > 40:
        error("记忆模式无效")
    if type(max_chars) is not int or not 0 <= max_chars <= 100000:
        error("记忆预算无效")
    if branch is not None and (not isinstance(branch, str) or len(branch) > 150):
        error("分支条件无效")
    from . import memperms as memory_permissions, charter as charter_config
    from .context_inspection import preferences, load_preferences
    tiers = memory_permissions.main_tiers(charter_config.load_config())
    if ident:
        enabled = preferences(ident)["effective"]["facts"]
    else:
        scopes = load_preferences()["scopes"]
        enabled = (scopes.get("project:" + pid, {}).get("facts", scopes.get("global", {}).get("facts", True))
                   if pid else scopes.get("global", {}).get("facts", True))
    readable = thread if thread is not None else {"resourceIds": []}
    # Reuse only inside this view; a subsequent request rechecks every source.
    access = {n['id']:permitted(n, readable, pid) for n in row['nodes'].values()}
    if branch is None:
        from .memory_tree import configured_ids
        keep = configured_ids(row, mode, selection_id)
        needs_branch = max_chars > 0 and any(
            n['id'] in keep and n['state'] == 'active' and n['branches']
            and (n['tree'] != 'facts' or enabled) and can_read_node(n, tiers)
            and access[n['id']] for n in row['nodes'].values())
        branch = current_branch(ident) if ident and needs_branch else ""
    if not isinstance(branch, str) or len(branch) > 150:
        error("分支条件无效")
    from .memory_tree import configured_ids
    keep = configured_ids(row, mode, selection_id)
    represented, seen = set(), {}
    rank = lambda n: ({'habit': 0, 'project': 1, 'session': 2}.get(n.get('scope'), 3), n['tree'] != 'habits')
    for n in sorted(row['nodes'].values(), key=rank):
        if (n['id'] not in keep or n['state'] != 'active' or not access[n['id']]
                or not can_read_node(n, tiers) or n['tree']=='facts' and not enabled
                or n['branches'] and branch not in n['branches']):
            continue
        key = n['body'].strip()
        if key in seen and rank(seen[key]) < rank(n):
            represented.add(n['id'])
        else:
            seen.setdefault(key, n)
    nodes = select(
        row,
        mode,
        max_chars,
        branch,
        lambda n: access[n['id']],
        conversation_id=selection_id,
        represented=represented,
    )
    for node in nodes:
        node["adapter"] = 'habits' if node.get('scope') == 'habit' else "managed"
        node["readonly"] = False
        # Display reasons must never decide whether source data is readable.
        if node["reason"] == "permission" or not access[node['id']]:
            node.update(
                selected=False,
                reason="permission",
                body="",
                title="来源不在当前读取范围",
                key="",
                layer=["来源不可读"],
                branches=[],
                evidence=[],
                sources=[],
                versions=[],
            )
        if node.get('scope') == 'habit':
            # The generalized body is public to this user, not its source materials.
            node['sources'] = [s for s in node.get('sources', [])
                if permitted({'sources': [s]}, thread, pid)]
            node['evidence'] = [e for e in node.get('evidence', [])
                if e.get('projectId') == pid]
    for n in nodes:
        if not can_read_node(n, tiers):
            n.update(selected=False, reason="memory_permission")
    if not enabled:
        for n in nodes:
            if n["tree"] == "facts":
                n.update(selected=False, reason="preference_disabled")
    legacy = [n for n in external_habits(row) if n['id'] not in row['nodes']] if include_external_habits else []
    from .memory_learning import current_conflict

    available = {n["id"]: n for n in legacy + nodes if n["selected"] and n["state"] == "active"}
    from .memory_learning import learning_stats
    for n in nodes:
        n['learning'] = learning_stats(n)
    return dict(
        requests=requests(ident) if details else [],
        sessionMemory=private_notes(thread) if details else "",
        revision=row["revision"],
        scopeRevisions=revisions,
        workspaceContextId="project:" + pid if pid else None,
        projectId=pid,
        conversationId=ident,
        mode=mode,
        nodes=legacy + nodes,
        selectionVersion=digest(
            [
                mode,
                max_chars,
                branch,
                [(n["id"], n["version"], n["selected"], n["reason"],
                  n.get("scope"), n.get("weight"), n.get("priority"),
                  n.get('learning', {}).get('counterexampleReview'),
                  digest(n["body"])) for n in nodes],
            ]
        ),
        conflicts=[
            v for v in row["conflicts"].values()
            if v.get("conversationId") == ident
            and (v["state"] != "pending" or current_conflict(v, available))
        ],
        history=row["history"][-30:],
        historyArchives=row.get('historyArchives', []),
        notice="可用/预计选中不等于模型已读取；原树仍由原机制注入。",
    )


# 按操作选择管理库或节点所在库。用户与模型权限、节点版本和来源验证决定具体写入路径。
def mutate(ident, body, actor="human", source=None):
    op = body.get('operation', 'upsert')
    control = op in {'mode', 'habit-weight', 'habit-configure', 'decide', 'compare', 'adopt'}
    store, thread, pid = (target(ident) if control else
        node_target(ident, body.get('id'), body.get('scope')))
    if thread.get("readonlyEvidence") or thread.get("subtaskOf") and actor == "human":
        error("此会话仅供审查", "memory_denied", 403)
    expected = body.get("expectedRevision")
    existing = store.load()["nodes"].get(body.get("id"))
    if (
        existing
        and body.get("expectedVersion") is not None
        and body["expectedVersion"] != existing["version"]
    ):
        error("节点版本已变化，请重新打开核对；编辑草稿保留", "memory_stale", 409)
    if existing and not permitted(existing, thread, pid):
        error("节点来源不在当前范围", "memory_denied", 403)
    if op == "mode":
        if actor != "human" or body.get("mode") not in {"full", "lean", "off"}:
            error("记忆模式无效")
        return store.change(
            expected,
            actor,
            lambda row: row.setdefault("sessionModes", {}).update(
                {ident: body["mode"]}
            ),
        )
    if op == "habit-weight":
        if (
            actor != "human"
            or type(body.get("weight")) is not int
            or not 0 <= body["weight"] <= 100
        ):
            error("权重无效")
        node = next(
            (n for n in external_habits(store.load()) if n["id"] == body.get("id")), None
        )
        if not node or node["version"] != body.get("expectedVersion"):
            error("原节点已更新", "memory_stale", 409)
        return store.change(
            expected,
            actor,
            lambda row: remember_habit_weight(row, node, body["weight"]),
        )
    if op == "habit-configure":
        if actor != "human":
            error("原树选择由用户设置", "memory_denied", 403)
        from . import habits as H

        node = next(
            (n for n in external_habits(store.load()) if n["id"] == body.get("id")), None
        )
        if not node or node["version"] != body.get("expectedVersion"):
            error("原节点已更新，请刷新", "memory_stale", 409)
        return H.toggle(node["legacyId"])
    if op == "upsert":
        local = store.path == target(ident, scope='session')[0].path
        if actor == 'model' and not local:
            error('模型只维护本会话采集条目；项目知识使用升格提案', 'memory_denied', 403)
        payload = dict(body, scope='session' if local else 'project',
            ownerId=ident if local else pid)
        return dict(store.upsert(expected, actor, payload, source or source_for(ident, actor)),
            scope=payload['scope'], notice='已保存会话记忆，尚未升格' if local else '用户直接维护项目记忆')
    if op == "configure":
        return store.configure(
            expected, actor, body.get("id"), body, conversation_id=ident
        )
    if op == "adopt":
        if actor != "human":
            error("采纳需要用户决定", "memory_denied", 403)
        from . import proposals as P

        proposal = P.get(W.home(), body.get("proposalId", ""))
        if not proposal or proposal.get("memorySource", {}).get("projectId") != pid:
            error("候选不属于此工作区", "memory_denied", 403)
        return P.adopt(W.home(), None, proposal["id"])
    if op in {"evidence", "promote", "compare", "decide"}:
        from . import memory_learning as L

        return L.mutate(ident, store, expected, actor, body)
    error("未知记忆操作")


# 通过 `execution_policy.binding` 与 `tools.current_call` 绑定宿主动作，按 `memperms.py` 控制模型可写范围。
def tool_remember(root, mem, text_value, **fields):
    from . import memperms as MP
    from .tools import current_call

    thread = E.binding(root)
    owner = current_call() or {}
    if not thread:
        return None
    if not MP.at_least(mem, "facts", "edit") or thread.get("readonlyEvidence"):
        error("没有事实编辑权限", "memory_denied", 403)
    if owner.get("sessionId") != thread["id"] or not owner.get("runId"):
        error("缺少实际运行身份", "memory_denied", 403)
    store, _, _ = node_target(thread['id'], fields.get('id'), 'session')
    row = store.load()
    if fields.get("id") and not {'expectedRevision', 'expectedVersion'} & fields.keys():
        error("更新节点需要读取版本", "memory_stale", 409)
    if thread.get("subtaskOf"):
        old = row["nodes"].get(fields.get("id"))
        if old and any(
            s.get("conversationId") != thread["id"] for s in old.get("sources", [])
        ):
            error("子任务不能覆盖父会话的事实，请新增带来源观察", "memory_denied", 403)
        fields["key"] = (
            "child:"
            + thread["subtaskOf"]["taskId"]
            + ":"
            + fields.get("key", fields.get("title", text_value[:100]))
        )
        branch = current_branch(thread["id"])
        if branch:
            fields["branches"] = [branch]
    payload = dict(
        fields,
        title=fields.get("title", text_value[:100]),
        body=text_value,
        tree="facts",
        scope='session',
        expectedRevision=fields.pop("expectedRevision", row["revision"]),
    )
    source = source_for(thread["id"], "model", owner.get("actionId"))
    source["runId"] = owner["runId"]
    if owner.get('logicalTaskId'):
        source['logicalTaskId'] = owner['logicalTaskId']
    return dict(ok=True, **mutate(thread["id"], payload, "model", source))


# 返回前端可选的项目和会话范围，供记忆界面切换。
def inventory():
    data = W.registry()
    return dict(
        projects=[
            dict(id=p["id"], name=p["name"])
            for p in data.get("projects", {}).values()
            if p.get("kind") != "subtask"
        ],
        conversations=[
            dict(id=t["id"], title=t["title"], projectId=t.get("projectId"))
            for t in data["threads"].values()
            if not t.get("subtaskOf")
        ],
    )


# 从会话或全局路由分流记忆浏览、预览和历史快照，具体提供方负责可用能力。
def route(method, parts, q, body, ident=None):
    if parts == ['inventory'] and method == 'GET':
        return 200, inventory()
    if parts and parts[0] == 'snapshot' and ident and method == 'GET':
        return 200, read_snapshot(ident, (q.get('id') or [''])[0])
    from .memory_providers import acquire, public_view
    lease = acquire(ident)
    if parts == ['archive']:
        # Explicit historical review uses retained host storage, never the
        # currently selected replacement's mutation interface.
        return _tree_route(method, parts, q, body, ident)
    if method == 'GET':
        return 200, page(public_view(lease, project_id=(q.get('projectId') or [None])[0],
            mode=(q.get('mode') or [None])[0], branch=(q.get('branch') or [None])[0]),
            int((q.get('offset') or ['0'])[0]))
    if parts == ['preview']:
        return 200, page(public_view(lease, project_id=body.get('projectId'), mode=body.get('mode','full'),
            max_chars=body.get('maxChars',16000), branch=body.get('branch')), body.get('offset',0))
    if not ident:
        error('修改记忆需要指定会话')
    if not lease.available:
        return 200, lease.unavailable(write=True)
    mutate_fn = (lambda: mutate(ident, body)) if lease.identity['adapter']=='tree' else lambda:lease.adapter.mutate(body)
    result = lease.execute(mutate_fn, write=True)
    if result.get('ok') is False and result.get('code') not in {'memory_unavailable','memory_write_unknown'}:
        error(result.get('error',result['notice']), result['code'], 409)
    return 200, result


# 内置记忆树使用自己的读写及分页入口。
def _tree_route(method, parts, q, body, ident=None):
    if parts == ['archive'] and method=='GET':
        store, _, _ = target(ident, project_id=(q.get('projectId') or [None])[0])
        return 200, store.history_page((q.get('hash') or [''])[0], int((q.get('offset') or ['0'])[0]))
    error('没有此原记忆归档入口', 'not_found', 404)


# 从实际检出目录读取本地私有便签，按所绑定的会话关联。
def private_notes(thread):
    if not thread:
        return ""
    from . import facts as F

    root = Path(W.workspace(thread["workspaceId"])["path"])
    return F.read("project", "main", root).get("text", "")


# 从会话保留的请求目录读可回查快照索引。
def requests(ident):
    if not ident:
        return []
    directory = W.P.data_root() / "runs" / ident / "context-requests"
    if not directory.is_dir():
        return []
    files = sorted(
        directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:30]
    items = []
    for p in files:
        if p.is_symlink() or p.stat().st_size > 512 * 1024:
            continue
        row = W.read(p)
        if row.get("memory"):
            items.append(
                dict(
                    id=row["id"],
                    runId=row["runId"],
                    role=row["role"],
                    at=row["at"],
                    loaded=row["memory"]["loaded"],
                )
            )
    return items


# 请求快照由上下文记录写入。快照只有节点身份和版本，回读正文须走原记忆提供方或树节点，并按当前授权核对。
def read_snapshot(ident, request_id):
    if (
        not isinstance(request_id, str)
        or len(request_id) > 80
        or any(c not in "0123456789abcdef-" for c in request_id)
    ):
        error("请求编号无效")
    target_store, thread, pid = target(ident)
    path = (
        W.P.data_root() / "runs" / ident / "context-requests" / (request_id + ".json")
    )
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
        error("请求记录不可用", "not_found", 404)
    snapshot = W.read(path).get("memory", {})
    provider = snapshot.get('provider', {}).get('identity')
    if provider and provider.get('adapter') != 'tree':
        # `memory_providers.py` 用快照中的提供方身份查历史实现。
        # 失效时保留审查元数据，不用当前默认提供方的内容补齐。
        from .memory_providers import acquire
        lease = acquire(ident, identity=provider, historical=True)
        if not lease.available:
            return dict(snapshot=dict(snapshot,nodes=[]),nodes=[],notice='原记忆实现已不可用，原始请求及工具审查记录仍保留。')
        view = lease.adapter.view(details=False)
        available = {n['id']:n for n in view['nodes'] if n['reason'] != 'permission'}
        retained = []
        for selected in snapshot.get('nodes', []):
            current = available.get(selected['id'])
            if not current:
                continue
            version = current if current['version']==selected['version'] else next(
                (v for v in current.get('versions',[]) if v['version']==selected['version']),None)
            retained.append(dict(selected,body=version.get('body','') if version else '',available=bool(version),currentState=current['state']))
        return dict(snapshot=dict(snapshot,nodes=[{k:v for k,v in n.items() if k not in {'body','available','currentState'}} for n in retained]),
            nodes=retained, notice='原提供者的历史请求材料，仅供审查；不恢复当前注入或写入授权。')
    rows, _, _ = scoped_rows(ident)
    row = dict(target_store.load(), nodes={nid: n for _, _, r in rows for nid, n in r['nodes'].items()})
    from . import habits as H
    row['nodes'].update({n['id']: n for n in H.memory_nodes()})
    nodes = []
    for selected in snapshot.get("nodes", []):
        from .memory_learning import lookup

        current = lookup(row, selected["id"])
        if not current or not permitted(current, thread, pid):
            continue
        version = (
            current
            if current["version"] == selected["version"]
            else next(
                (
                    v
                    for v in current.get("versions", [])
                    if v["version"] == selected["version"]
                ),
                None,
            )
        )
        nodes.append(
            dict(
                selected,
                body=version["body"] if version else "",
                available=bool(version),
                currentState=current["state"],
            )
        )
    snapshot = dict(
        snapshot,
        nodes=[
            {
                k: v
                for k, v in n.items()
                if k not in {"body", "currentState", "available"}
            }
            for n in nodes
        ],
    )
    return dict(
        snapshot=snapshot,
        nodes=nodes,
        notice="历史请求构建快照；当前已撤权内容不返回，不能证明模型理解",
    )


# 将完整记忆视图限制为一页节点，保留总数和原模式。
def page(view, offset=0, limit=80):
    if type(offset) is not int or offset < 0:
        error("记忆分页位置无效")
    nodes = view["nodes"]
    return dict(
        view,
        nodes=nodes[offset : offset + limit],
        offset=offset,
        totalNodes=len(nodes),
        hasMore=offset + limit < len(nodes),
        nextOffset=offset + limit if offset + limit < len(nodes) else None,
    )


# 只有恰好一个可读代码资源时才尝试取真实分支，无法确认返回空。
def current_branch(ident):
    resources = [
        r
        for r in projects.scope(ident)["resources"]
        if r.get("kind") == "code" and r.get("permission") == "read"
    ]
    if len(resources) != 1:
        return ""
    try:
        result = W.git(
            dict(path=resources[0]["path"]),
            ["-c", "core.fsmonitor=false", "rev-parse", "--abbrev-ref", "HEAD"],
            check=False,
        )
        return (
            result.stdout.decode("utf8", "replace").strip()
            if result.returncode == 0
            else ""
        )
    except (W.Problem, OSError):
        return ""


# 按模型的读权限提供当前可见摘要，历史版本回查再核对原节点和版本。
def model_page(ident, offset=0, tiers=None, node_id=None, version=None):
    resolved = resolve(ident, include_external_habits=False, details=False)
    if node_id is not None:
        node = next((n for n in resolved['nodes'] if n['id'] == node_id), None)
        if (not node or node['reason'] not in {'selected', 'budget', 'represented_by_habit'}
                or (tiers is not None and not can_read_node(node, tiers))):
            error('节点未开启或来源不可读取', 'memory_denied', 403)
        if version is not None:
            if not (type(version) is int and version > 0 or isinstance(version, str) and 0 < len(version) <= 100):
                error('记忆版本无效')
            if version != node['version']:
                old = next((v for v in node.get('versions', []) if v['version'] == version), None)
                if not old:
                    error('此版本正文不可用', 'not_found', 404)
                node = dict(node, **old)
        resolved['nodes'] = [node]
    elif version is not None:
        error('指定版本需要节点 ID')
    view = page(resolved, offset)
    for node in view["nodes"]:
        node.pop("versions", None)
        if (not node["selected"] and node_id is None) or (tiers is not None and not can_read_node(node, tiers)):
            node["body"] = ""
            node["evidence"] = []
            node["sources"] = []
            if node["selected"]:
                node.update(selected=False, reason="memory_permission")
    return view


# 根据实际请求的资料记录汇出来源范围。
def source_resources(thread):
    if not thread:
        return []
    selected = thread.get("resourceIds", [])
    if not thread.get("subtaskOf"):
        return list(selected)
    parent = projects.conversation(thread["subtaskOf"]["parentId"])
    origins = {binding["id"]: origin for origin, binding in parent.get("codeWorktrees", {}).items()}
    resources = projects.project(thread["projectId"]).get("resources", {})
    return [
        origins.get(resources[rid]["originResourceId"], resources[rid]["originResourceId"])
        for rid in selected
        if rid in resources and resources[rid].get("originResourceId")
    ]


def remember_habit_weight(row, node, weight):
    row.setdefault("legacyWeights", {})[node["id"]] = weight
    row.setdefault("legacyRefs", {})[node["id"]] = dict(
        adapter=node["adapter"], title=node["title"], bodyHash=digest(node["body"])
    )
    return dict(id=node["id"])


# 旧的权限档名称映射到当前事实和习惯树分支后再检查读取。
def can_read_node(node, tiers):
    from . import memperms as MP

    # Legacy tier names now denote branch capabilities inside one habit tree.
    branch = (
        "facts"
        if node["tree"] == "facts"
        else "habits"
        if node["layer"][0] == "用户习惯"
        else "rules"
    )
    return MP.at_least(tiers, branch, "read")
