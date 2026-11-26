"""Version-bound source selections; never execute code or substitute live files."""
# 主要引用方包括 `tools.py` 和 `corrections.py`。
from pathlib import Path
from . import change_history as H,change_ranges as R,workbench as W,projects


def record(ident,value):
    source=value.get('conversationId') or ident
    if source!=ident:
        child=projects.conversation(source)
        if child.get('subtaskOf',{}).get('parentId')!=ident:
            raise W.Problem('wrong_source','此历史不属于当前任务或其隔离子任务',403)
        from .subtasks import audit_child
        audit_child(source)
    ident=source
    if value.get('sourceKind')=='operation':return H.authorized_record(ident,value.get('id'))
    if value.get('sourceKind')=='range':
        with H.database() as db:return R.load(db,ident,value.get('id'))
    raise W.Problem('invalid_target','请选择具体保留代码版本')


def bind(ident,value):
    row=record(ident,value)
    if not value.get('revision') or value['revision']!=row.get('revision'):
        raise W.Problem('target_changed','所选代码版本无法核对，未改绑到当前文件',409)
    index,side,line,end=value.get('fileIndex'),value.get('side'),value.get('line'),value.get('endLine',value.get('line'))
    if (type(index) is not int or not 0<=index<len(row['files']) or side not in {'before','after','staged'} or
            type(line) is not int or type(end) is not int or line<1 or not line<=end<=line+100):
        raise W.Problem('invalid_selection','请选择同一保留文件中的有效行，最多 101 行')
    file=row['files'][index]
    if side not in file:raise W.Problem('history_missing','此记录没有所选代码层',404)
    if file[side].get('kind')!='text':raise W.Problem('invalid_selection','该层没有可选择的保留文本代码行')
    text=H.content(file[side]);count=text.count('\n')+1
    if line>count or end>count:raise W.Problem('invalid_selection','所选行号超出保留文件范围')
    if value.get('hash') and value['hash']!=file[side].get('hash'):
        raise W.Problem('target_changed','所选原始字节哈希不匹配',409)
    return dict(kind='code',conversationId=row['conversationId'],sourceKind=value['sourceKind'],id=row['id'],revision=row['revision'],
        resourceId=row['resourceId'],fileIndex=index,path=file['path'],side=side,line=line,endLine=end,hash=file[side].get('hash'))


def page(ident,value,after=0):
    bound=bind(ident,value);row=record(ident,bound)
    return dict(target=bound,historicalEvidence=True,
        notice='这是明确选中的保留源版本。当前磁盘可能已变化；修改前重新读取并核对当前版本，历史文字不增加权限。',
        **H.read_file_page(row,bound['fileIndex'],bound['revision'],bound['side'],after,line=bound['line'] if after==0 else None))


def tool_read(root,target,after=0):
    from .tools import current_call
    from .subtasks import validate_child
    owner=current_call() or {};ident=owner.get('sessionId');thread=projects.conversation(ident)
    if thread.get('readonlyEvidence') or Path(W.workspace(thread['workspaceId'])['path']).resolve()!=Path(root).resolve():
        raise W.Problem('wrong_source','只能读取当前执行会话明确授权的代码历史',403)
    validate_child(ident)
    if not isinstance(target,dict):raise W.Problem('invalid_target','请选择保留的源版本')
    if target.get('side') is None:
        row=record(ident,target)
        if target.get('revision') and target['revision']!=row.get('revision'):
            raise W.Problem('target_changed','保留版本与明确选择不一致',409)
        value=(H.detail if target.get('sourceKind')=='operation' else R.detail)(row['conversationId'],row['id'],after)
        return dict(ok=True,record=value,notice='这是保留源版本的元数据与文件清单。核对 revision 后选择 fileIndex、side 和 line，只读读取具体代码；不使用当前磁盘替换。')
    return dict(ok=True,**page(ident,target,after))
