"""Manual preview downloads: private staging, current authority, fresh destinations.

Web content never receives a filesystem path or the application bridge. Saving
is a human action and never attributes a project change to the Agent.
"""
# 主要引用方包括 `previews.py`。
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from . import workbench as W,change_history as H,baseline
from .filelock import FileLock,atomic_json,read_text

MAX_FILE=25*1024*1024
MAX_STORED=128*1024*1024
MAX_RECORDS=500

def filename(name):
    if (not isinstance(name,str) or not 1<=len(name)<=180 or name in {'.','..'} or
            '/' in name or '\\' in name or any(ord(c)<32 or ord(c)==127 for c in name)):
        raise W.Problem('invalid_download','下载文件名称无效')
    return name


def directory():
    root=W.home()/'downloads';root.mkdir(exist_ok=True)
    if not root.resolve().is_relative_to(W.home().resolve()):raise W.Problem('path_escape','下载保留目录已变化',403)
    return root


def paths(ident):
    if not isinstance(ident,str) or not re.fullmatch('[0-9a-f]{32}',ident):raise W.Problem('invalid_download','下载身份无效')
    root=directory();return W.file_path({'path':str(root)},ident+'.json'),W.file_path({'path':str(root)},ident+'.part')


def load(ident,conversation,preview):
    meta,stage=paths(ident)
    if not meta.is_file() or meta.stat().st_size>16384:raise W.Problem('download_missing','下载记录不可读',410)
    value=json.loads(read_text(meta))
    if value.get('conversationId')!=conversation or value.get('previewId')!=preview:
        raise W.Problem('wrong_source','下载不属于所选应用',403)
    return value,meta,stage


def authority(conversation,preview,live=False):
    from . import previews
    value=previews.inspect(conversation,preview,include_version=False) if live else previews.load(conversation,preview)
    H.authorize(conversation,dict(resourceId=value['resourceId'],resourcePath=W.workspace(value['workspaceId'])['path']))
    if live and value['state']!='ready':raise W.Problem('preview_unavailable','应用来源已失效，本次下载未保存',409)
    return value


def public(value):
    return {k:v for k,v in value.items() if k not in {'ownerIdentity','ownerPid'}}


def listing(conversation,preview,offset=0):
    authority(conversation,preview)
    if type(offset) is not int or offset<0:raise W.Problem('invalid_cursor','下载列表位置无效')
    rows=[]
    for path in directory().glob('*.json'):
        if path.stat().st_size>16384:raise W.Problem('download_corrupt','下载记录超过边界，未截断为完整列表',409)
        value=json.loads(read_text(path))
        if value.get('conversationId')==conversation and value.get('previewId')==preview:rows.append(value)
    rows.sort(key=lambda r:r['created'],reverse=True)
    for value in rows[offset:offset+20]:
        if value['state'] in {'receiving','committing'} and W.RT.identity(value['ownerPid'])!=value['ownerIdentity']:
            value.update(state='unknown',notice='下载控制器已经结束；保留已接收内容，不重放或覆盖目标')
    return dict(downloads=[public(r) for r in rows[offset:offset+20]],hasMore=offset+20<len(rows),next=offset+20)


def begin(conversation,preview,body):
    app=authority(conversation,preview,live=True)
    name=filename(body.get('fileName'));pid=body.get('ownerPid')
    if type(pid) is not int or pid<=0:
        raise W.Problem('invalid_download','下载名称或控制器无效')
    owner=W.RT.identity(pid)
    if owner in (None,'unknown'):raise W.Problem('download_owner','下载控制器身份不可核对',409)
    with FileLock(directory()/'store.lock'):
        records=list(directory().glob('*.json'));stages=list(directory().glob('*.part'))
        reserved=0
        for path in records:
            if path.stat().st_size>16384:raise W.Problem('download_corrupt','下载记录超过边界，未接收新下载',409)
            prior=json.loads(read_text(path));_,old_stage=paths(prior['id'])
            size=old_stage.stat().st_size if old_stage.is_file() else 0
            reserved+=MAX_FILE if prior['state']=='receiving' and W.RT.identity(prior['ownerPid'])==prior['ownerIdentity'] else size
        if len(records)>=MAX_RECORDS or reserved+MAX_FILE>MAX_STORED:
            raise W.Problem('download_capacity','下载保留区已满，本次下载尚未接收',413)
        ident=uuid.uuid4().hex;meta,stage=paths(ident)
        value=dict(id=ident,conversationId=conversation,previewId=preview,resourceId=app['resourceId'],
            previewRevision=app['revision'],sourceURL=app['url'],fileName=name,ownerPid=pid,ownerIdentity=owner,
            state='receiving',created=time.time(),actor='human',notice='正在接收至应用私有保留区，尚未保存到所选位置')
        with stage.open('xb'):pass
        atomic_json(meta,value)
    return dict(download=public(value),stagingPath=str(stage),maximumBytes=MAX_FILE)


def owner(value,body):
    if body.get('ownerPid')!=value['ownerPid'] or W.RT.identity(value['ownerPid'])!=value['ownerIdentity']:
        raise W.Problem('download_owner','下载控制器已变化，未接管或重放',409)


def stage_bytes(stage):
    if not stage.is_file() or stage.is_symlink() or stage.stat().st_size>MAX_FILE:
        raise W.Problem('download_capacity','下载内容缺失、超过 25MiB 或保留路径已变化',413)
    data=stage.read_bytes()
    if len(data)>MAX_FILE:raise W.Problem('download_capacity','下载超过 25MiB，未保存不完整文件',413)
    return data


def destination(value,raw):
    if not isinstance(raw,str) or not raw or len(raw)>4096:raise W.Problem('invalid_destination','请选择保存文件的位置')
    target=Path(raw)
    if not target.is_absolute():raise W.Problem('invalid_destination','保存位置须为明确绝对路径')
    parent=target.parent.resolve(strict=True);target=parent/target.name
    if any(target.is_relative_to(root.resolve()) for root in (W.P.data_root(),W.P.app_root())) or baseline.hit('write',target):
        raise W.Problem('protected_destination','请选择应用程序、内部数据和受保护位置以外的保存位置',403)
    if target.suffix.lower()!=Path(value['fileName']).suffix.lower():raise W.Problem('download_extension','请保留下载文件原有扩展名')
    roots=[]
    registry=W.registry()
    possible=[w['path'] for w in registry['workspaces'].values()]
    possible.extend(r['path'] for p in registry.get('projects',{}).values() for r in p.get('resources',{}).values())
    for path in possible:
        root=Path(path).resolve()
        if target.is_relative_to(root):roots.append(str(root))
    return target,parent,sorted({str(W.writer_root(root)) for root in roots or [str(parent)]})


def update(conversation,preview,ident,operation,body):
    with FileLock(directory()/'store.lock'):
        value,meta,stage=load(ident,conversation,preview);owner(value,body)
        if operation=='cancel':
            if value['state'] in {'saved','committing','unknown'}:return public(value)
            value.update(state='cancelled',notice='下载已取消；未保存到目标位置',updated=time.time())
            if stage.is_file():stage.unlink()
            atomic_json(meta,value);return public(value)
        if operation=='fail':
            if value['state']=='saved':return public(value)
            uncertain=value['state'] in {'committing','unknown'}
            value.update(state='unknown' if uncertain else 'failed',notice=('保存可能已发生；请核对目标文件，未自动重试。' if uncertain else '')+str(body.get('reason') or '下载失败，未报告保存成功')[:400],updated=time.time())
            if stage.is_file() and stage.stat().st_size>MAX_FILE:stage.unlink()
            atomic_json(meta,value);return public(value)
        authority(conversation,preview,live=operation=='seal')
        if operation=='seal':
            if value['state']=='staged':return public(value)
            if value['state']!='receiving':raise W.Problem('download_state','该下载已结束或待核对，不自动重放',409)
            data=stage_bytes(stage)
            if 'fileName' in body:value['fileName']=filename(body['fileName'])
            value.update(state='staged',bytes=len(data),hash=W.digest(data),updated=time.time(),notice='完整下载已保留，等待你选择保存位置')
            atomic_json(meta,value);return public(value)
        if operation!='save':raise W.Problem('not_found','没有此下载操作',404)
        if value['state']=='saved':return public(value)
        if value['state']!='staged':raise W.Problem('download_state','下载未完成或结果未知，不能保存或重放',409)
        data=stage_bytes(stage)
        if W.digest(data)!=value['hash']:raise W.Problem('download_changed','下载保留内容已变化，未保存',409)
        target,parent,roots=destination(value,body.get('destination'))
        effect=False;temporary=None
        try:
            with ExitStack() as stack:
                for root in roots:stack.enter_context(FileLock(W.writer_lock_path(root)))
                from .code_service import RuntimeView
                for root in roots:W.guard_workspace_writer(root,RuntimeView())
                if target.exists() or target.is_symlink():raise W.Problem('destination_exists','目标已存在，请另存为新的文件名；原文件未更改',409)
                value.update(state='committing',path=str(target),updated=time.time(),notice='正在提交完整文件，尚未确认保存成功')
                atomic_json(meta,value)
                descriptor,temporary=tempfile.mkstemp(prefix='.h-download-',dir=parent)
                with os.fdopen(descriptor,'wb') as handle:handle.write(data);handle.flush();os.fsync(handle.fileno())
                if parent.resolve(strict=True)!=parent:raise W.Problem('destination_changed','目标目录已经变化，未保存',409)
                # Atomic fresh-file creation: never overwrite an existing file,
                # including one created after the initial existence check.
                os.link(temporary,target);effect=True
                if target.is_symlink() or W.digest(target.read_bytes())!=value['hash']:
                    raise W.Problem('download_unknown','已发生保存但最终字节无法核对，请检查目标文件',409)
                value.update(state='saved',savedAt=time.time(),notice='已核对保存的完整文件与 SHA256；这不是应用功能验收')
                atomic_json(meta,value)
        except (W.Problem,OSError,TimeoutError) as error:
            value.update(state='unknown' if effect else 'staged',updated=time.time(),
                notice=('保存可能已发生，请核对目标；未自动重试' if effect else '保存未完成，原有目标未覆盖：')+(error.message if isinstance(error,W.Problem) else '目标位置不可安全写入'))
            atomic_json(meta,value)
            raise
        finally:
            if temporary and Path(temporary).is_file():Path(temporary).unlink()
        return public(value)


def route(conversation,preview,parts,method,query,body):
    if not parts:
        if method=='GET':return 200,listing(conversation,preview,int((query.get('offset') or ['0'])[0]))
        if method=='POST':return 200,begin(conversation,preview,body)
    if method=='POST' and len(parts)==2:return 200,update(conversation,preview,parts[0],parts[1],body)
    raise W.Problem('not_found','没有此下载入口',404)
