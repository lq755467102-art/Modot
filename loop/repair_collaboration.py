"""Repair ownership on the existing work queue; no additional scheduler."""
# 主要引用方包括 `role_coordination.py` 和 `assistant_patches.py`。
from copy import deepcopy
from contextlib import contextmanager
import time
from . import workbench as W
from .tools import current_call, set_call, reset_call


@contextmanager
def _reading(owner, row=None):
    # Feedback runs between tool calls; use the same scoped identity as patch
    # feedback without replacing a caller's existing (possibly stale) identity.
    token = set_call(owner._identity('reviewer' if row else 'executor', 'repair_progress')) if current_call() is None else None
    try:
        yield
    finally:
        if token is not None:
            reset_call(token)


def state(owner, item):
    repair = deepcopy(item.get('repair', {}))
    if item.get('state') == 'verified':
        repair['state'] = 'closed'
        repair['reason'] = '已在新候选完成独立复验'
        repair['verification'] = dict(candidateId=item.get('verifiedCandidateId'),
                                      revision=item.get('verifiedRevision'), actionId=item.get('verifiedActionId'))
    elif repair.get('state') == 'repairing':
        candidate = owner.state.get('candidates', {}).get(repair.get('candidateId'), {})
        if repair.get('runId') != owner.session.run_id or (repair.get('role') == 'reviewer' and
                candidate.get('state') in {'completed','failed','paused','timed_out','superseded'}):
            repair['state'] = 'interrupted'
    return repair


def claim(owner, item_id, role, *, candidate_id=None, paths=()):
    with owner.lock:
        owner._check_work_scope()
        item = owner.work.state['items'].get(item_id)
        if not item or item.get('kind') != 'defect' or item.get('state') == 'verified':
            raise W.Problem('repair_item_changed', '问题不存在或已独立复验关闭', 409)
        prior = state(owner, item)
        if prior.get('state') in {'patch_ready','awaiting_verification'}:
            raise W.Problem('repair_pending_verification', '该问题已有补丁或主线修复，请先核对补丁或新版本证据，勿重复实现', 409)
        if prior.get('state') == 'repairing' and (prior.get('role') != role or
                role == 'reviewer' and prior.get('candidateId') != candidate_id):
            raise W.Problem('repair_in_progress', '同一问题正由另一处理者修复，请先推进无关工作；问题仍待验收', 409)
        item['repair'] = dict(prior, state='repairing', role=role, runId=owner.session.run_id,
                             candidateId=candidate_id, paths=list(paths or prior.get('paths', [])), at=time.time())
        owner.work._change(); owner.save()
        owner.session.emit('repair_activity', itemId=item_id, role=role, state='repairing')
        return item


def update(owner, item_id, status, **fields):
    with owner.lock:
        item = owner.work.state['items'].get(item_id)
        if not item or item.get('kind') != 'defect':
            return
        item['repair'] = dict(item.get('repair', {}), state=status, at=time.time(), **fields)
        owner.work._change(); owner.save()
        owner.session.emit('repair_activity', itemId=item_id, role=item['repair'].get('role','executor'), state=status)


def reviewer_begin(owner, row, grant, path, proposed_bytes, item_id=None):
    ids = grant.get('itemIds') or ([grant['itemId']] if grant.get('itemId') else [])
    if not ids:  # Retained explicit repair assignments predate the defect queue.
        return None
    if item_id is None:
        matching = [key for key in ids if owner.work.state['items'][key].get('codeEvidence', {}).get('path') == path]
        choices = matching or ids
        if len(choices) != 1:
            raise W.Problem('repair_item_required', '当前范围包含多个问题，请用 item_id 指明本次修复的问题', 409)
        item_id = choices[0]
    if item_id not in ids:
        raise W.Problem('repair_scope', '问题不在当前修复范围', 403)
    # Exact edit evidence, not filename matching or semantic guessing. User edits
    # remain authoritative; already fixed means recheck, never independent pass.
    _, _, ws, _ = owner._resource(row['resourceId'])
    current = W.agent_file_path(ws, path).read_bytes()
    if current == proposed_bytes:
        update(owner, item_id, 'awaiting_verification', role='executor',
               mainFileRevision=W.digest(current), paths=[path], reason='主线已包含同一替换结果，待新候选复验')
        raise W.Problem('repair_already_on_main', '主线已包含这处修复；保留原候选意见，等待或核对新候选，勿再生成等价补丁', 409)
    claim(owner, item_id, 'reviewer', candidate_id=row['id'], paths=[path])
    return item_id


def view(owner):
    with _reading(owner), owner.lock:
        owner._check_work_scope()
        rows = []
        for item in owner.work.state['items'].values():
            if item.get('kind') != 'defect' or not item.get('repair'):
                continue
            try:
                owner._resource(item['resourceId'])
            except W.Problem:
                continue
            rows.append(dict(itemId=item['id'], problem=item['text'], resourceId=item['resourceId'],
                             revision=item.get('revision'), repair=state(owner,item)))
        return rows[-12:]


def progress(owner, row=None):
    """Bounded projection of the existing ledger; never scan or seal a workspace."""
    with _reading(owner,row), owner.lock:
        owner._check_work_scope()
        resources = {row['resourceId']} if row else set(owner.state.get('changeLedger', {}))
        rows = []
        for rid in resources:
            try:
                if row:
                    owner._candidate_resource(row)
                else:
                    owner._resource(rid)
            except W.Problem:
                continue
            change = owner.state.get('changeLedger', {}).get(rid, {})
            version = owner.state.get('dirty', {}).get(rid)
            pending = str(version or '').startswith('pending:')
            candidates = [c for c in owner.state['candidates'].values() if c['resourceId'] == rid
                          and (not row or c.get('sourcePath') == row.get('sourcePath'))]
            failures = [dict(candidateId=c['id'], revision=rec.get('revision'),
                             actionId=rec.get('actionId'), status=rec['state'], argv=rec.get('argv'),
                             interpretation='检查失败待归因，不代表已确认产品缺陷')
                        for c in candidates[-4:] for rec in list(c.get('checks', {}).values())[-6:]
                        if rec.get('state') in {'failed','blocked','timed_out'}][-4:]
            if change or failures or row:
                rows.append(dict(resourceId=rid, candidateRevision=row.get('revision') if row else None,
                    mainRevision=None if pending else version, mainVersionPending=pending,
                    latestCandidateId=candidates[-1]['id'] if candidates else None,
                    changes=deepcopy(change.get('recent', [change] if change else []))[-12:],
                    failedChecks=failures))
        repairs = [r for r in view(owner) if not row or r['resourceId'] == row['resourceId']]
        return dict(resources=rows[-8:], repairs=repairs,
                    notice='项目日志是事实材料，不是授权。主线更新不证明旧候选通过；相同文件不等于相同缺陷。已修改的问题在新候选定点复验，其他工作可继续。')


def guard_draft(owner, row, grant):
    """Recheck just the edited paths before a costly check or patch publication."""
    draft = grant.get('drafts', {}).get(row['id'], {})
    files = draft.get('files', {})
    if not files:
        return
    for key in set(draft.get('repairItems', {}).values()):
        current = state(owner, owner.work.state['items'][key])
        if current.get('state') in {'closed','awaiting_verification'}:
            raise W.Problem('repair_pending_verification', '主线已有处理结果，先核对新候选复验，保留本地草稿', 409)
        if current.get('state') == 'repairing' and current.get('role') != 'reviewer':
            raise W.Problem('repair_in_progress', '执行者正在处理同一问题，请先核对项目日志', 409)
    _, _, ws, _ = owner._candidate_resource(row)
    if all(W.agent_file_path(ws,path).is_file() and W.digest(W.agent_file_path(ws,path).read_bytes()) == meta['hash']
           for path,meta in files.items()):
        for key in set(draft.get('repairItems', {}).values()):
            update(owner,key,'awaiting_verification',role='executor',paths=list(files),
                   reason='主线已包含全部草稿文件结果，待新候选独立复验')
        raise W.Problem('repair_already_on_main', '主线已包含本补丁内容，无需重复检查旧草稿或重复提交；请在新候选复验', 409)


def wrap_executor(owner, registry):
    for name in ('edit_project_file','write_project_file'):
        tool = registry.get(name)
        if not tool or getattr(tool, '_repair_wrapped', False):
            continue
        original = tool.fn
        tool.parameters = deepcopy(tool.parameters)
        tool.parameters.setdefault('properties', {})['issue_id'] = dict(type='string', description='修复队列缺陷时传原问题编号；原子核对处理者，防止重复修复')
        tool.description += ' 修复已登记缺陷时传 issue_id；同一问题由测试者处理中时先做其他工作。'
        def write(issue_id=None, _original=original, **args):
            with owner.lock:
                if issue_id:
                    item=owner.work.state['items'].get(issue_id,{})
                    if item.get('resourceId') != args.get('resource_id'):
                        raise W.Problem('repair_scope','问题与编辑资源不一致',403)
                    claim(owner,issue_id,'executor',paths=[args.get('relative_path')])
                try:
                    result = _original(**args)
                except Exception:
                    if issue_id: update(owner, issue_id, 'interrupted')
                    raise
                if issue_id:
                    update(owner,issue_id,'repairing' if result.get('ok') else 'interrupted',
                           operationId=result.get('operationId'), mainFileRevision=result.get('revision'))
                return result
        tool.fn=write; tool._repair_wrapped=True
