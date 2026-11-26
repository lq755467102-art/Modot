"""Create an empty review baseline without adding or committing user files.

Caller holds the canonical writer lock and has revalidated the current run's
explicit code-write grant. Existing repositories with history are untouched.
"""
# 主要引用方包括 `code_service.py`。
import time
import uuid
from . import workbench as W


def initialize(thread,resource,ws,source,args):
    from . import code_service as service
    state=W.git_status(ws)
    if state.get('head'):
        return dict(ok=False,code='already_initialized',error='已有 Git 历史；请使用现有版本创建功能分支，不重建基线',git=state)
    with service.database() as db:
        uncertain=db.execute("SELECT id FROM operations WHERE json_extract(data,'$.workspaceId')=? AND json_extract(data,'$.operation')='initialize' AND json_extract(data,'$.state') IN ('running','unknown') LIMIT 1",(ws['id'],)).fetchone()
    if uncertain:
        return dict(ok=False,code='initialization_unknown',state='unknown',operationId=uncertain[0],
            error='该目录已有未核对的初始化记录，请先在工程工具核对；未重新执行初始化')
    W.check_revision(ws,args.get('expected_revision'))
    branch=args.get('branch','')
    if not isinstance(branch,str) or not branch.startswith('agent/') or W.safe_git(ws,['check-ref-format','--branch',branch],check=False).returncode:
        raise W.Problem('invalid_branch','新项目需合法的 agent/ 功能分支')
    if state.get('available') and W.safe_git(ws,['for-each-ref','--format=%(refname)']).stdout.strip():
        raise W.Problem('existing_history','当前 HEAD 缺失但仓库有其他历史，请从工程工具核对，不自动重建',409)
    op=dict(id=uuid.uuid4().hex,operation='initialize',resourceId=resource['id'],workspaceId=ws['id'],source=source,
        started=time.time(),beforeRevision=W.current_revision(ws),branch=state.get('branch'),head=None,state='running',target=dict(branch=branch))
    service.record(thread['id'],op)
    effect=False
    try:
        effect=True
        if not state.get('available'):W.safe_git(ws,['init','-b',branch])
        # Plumbing creates the empty tree/commit independently of the user's
        # index. No git add, checkout, hooks, global identity or network calls.
        tree=W.safe_git(ws,['hash-object','-w','-t','tree','--stdin'],input=b'').stdout.decode().strip()
        commit=W.safe_git(ws,['-c','user.name=Harness Agent','-c','user.email=agent@harness.invalid','-c','commit.gpgSign=false',
            'commit-tree',tree],input=b'Initialize empty review baseline; existing files remain uncommitted.\n').stdout.decode().strip()
        W.safe_git(ws,['update-ref','refs/heads/'+branch,commit,'0'*len(commit)])
        W.safe_git(ws,['symbolic-ref','HEAD','refs/heads/'+branch])
        result=dict(ok=True,operationId=op['id'],git=W.git_status(ws),
            notice='已创建独立功能分支与空基线；现有文件和暂存内容未提交、未覆盖。后续修改仍需逐步保留版本。')
        service.finish(thread['id'],op['id'],'completed',result)
        return result
    except Exception:
        # The initial running receipt deliberately becomes unknown. Partial
        # .git metadata stays inspectable; initialization is never auto-retried.
        result=dict(ok=False,operationId=op['id'],state='unknown' if effect else 'failed',
            code='initialization_unknown',error='初始化可能已部分发生，请核对 Git 状态；不会自动重放或删除元数据')
        service.finish(thread['id'],op['id'],result['state'],result)
        return result
