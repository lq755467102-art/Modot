"""Human conversation metadata, independent of execution and resource grants."""
# 主要引用方包括 `workbench.py`。
from . import workbench as W,projects


def configure(ident,body):
    item=projects.conversation(ident)
    if item.get('subtaskOf'):raise W.Problem('managed_child','子任务名称由父任务保留，不能从侧栏修改',403)
    if set(body)-{'title','pinned','expectedRevision','_request_id'}:raise W.Problem('invalid_organization','只能修改会话名称和置顶状态')
    if 'title' in body and (not isinstance(body['title'],str) or not 1<=len(body['title'].strip())<=100 or any(ord(c)<32 for c in body['title'])):
        raise W.Problem('invalid_title','会话名称需为 1—100 字，不含控制字符')
    if 'pinned' in body and type(body['pinned']) is not bool:raise W.Problem('invalid_pin','置顶值须为布尔值')
    def update(data):
        value=data['threads'][ident];revision=value.get('organizationRevision',1)
        if type(body.get('expectedRevision')) is not int or body['expectedRevision']!=revision:
            raise W.Problem('organization_changed','会话信息已变化，请刷新后再操作',409)
        if 'title' in body:value['title']=body['title'].strip()
        if 'pinned' in body:value['pinned']=body['pinned']
        value['organizationRevision']=revision+1
        return dict(id=ident,title=value['title'],pinned=value.get('pinned',False),organizationRevision=revision+1)
    return W.change_registry(update)
