"""Versioned cumulative review frames. Never substitute live files for history."""
# 主要引用方包括 `workbench.py` 和 `code_feedback.py`。
import json
from pathlib import Path
import re
import tempfile
import time
from . import baseline as protection
from . import change_history as H, workbench as W, projects
from .filelock import FileLock

ABSENT=dict(kind='absent',hash=None)


def resource(ident,rid,*,historical=True):
    value=projects.retained_code(ident,rid) if historical else next(
        (item for item in projects.scope(ident)['resources'] if item['id']==rid),None)
    if not value or value.get('kind')!='code':raise W.Problem('resource_denied','请选择本会话的代码资料',403)
    H.authorize(ident,dict(resourceId=rid,resourcePath=value['path']))
    return value


def equivalent(a,b):
    return a.get('kind')==b.get('kind') and a.get('hash')==b.get('hash') and a.get('mode')==b.get('mode')


def merge(base,current,incoming):
    """Merge only retained bytes in private temporary files, never a worktree."""
    if current==incoming:return current,False
    if current==base:return incoming,False
    if incoming==base:return current,False
    if None in (base,current,incoming):return None,True
    parent=W.home()/'range-merges';parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as name:
        directory=Path(name).resolve()
        if not directory.is_relative_to(parent.resolve()):raise W.Problem('path_escape','累计差异临时目录不属于应用',403)
        paths=[directory/n for n in ('current','base','incoming')]
        for path,raw in zip(paths,(current,base,incoming)):path.write_bytes(raw)
        result=W.safe_git({'path':str(directory)},['merge-file','-p',*[str(p) for p in paths]],check=False)
        if result.returncode not in (0,1):raise W.Problem('range_merge_failed','累计差异技术合成失败，原始版本保留',409)
        # Validate the owned target again before TemporaryDirectory removes it.
        if not directory.is_relative_to(parent.resolve()):raise W.Problem('path_escape','累计差异临时目录发生变化',403)
        return (result.stdout,False) if result.returncode==0 else (None,True)


def meta(db,raw):
    if raw is None:return dict(ABSENT)
    if len(raw)>W.LIMIT:raise W.Problem('range_capacity','合成文件超过可审查边界，原操作差异仍可逐项查看',413)
    try:
        if b'\0' in raw:raise UnicodeError()
        raw.decode('utf-8-sig')
    except UnicodeError:
        return dict(kind='binary',hash=H.put_object(db,raw),bytes=len(raw))
    return dict(kind='text',hash=H.put_object(db,raw),bytes=len(raw),bom=raw.startswith(b'\xef\xbb\xbf'),eol='CRLF' if b'\r\n' in raw else 'LF')


def store(db,ident,value):
    value['revision']=H.version(value)
    encoded=json.dumps(value,ensure_ascii=False)
    if len(encoded.encode('utf8'))>8*1024*1024:raise W.Problem('range_capacity','累计差异清单过大，未截断为完整版本',413)
    H.metadata_capacity(db)
    db.execute('INSERT OR IGNORE INTO ranges VALUES(?,?,?)',(value['id'],ident,encoded))
    return load(db,ident,value['id'])


def load(db,ident,version):
    row=db.execute('SELECT data FROM ranges WHERE thread=? AND id=?',(ident,version)).fetchone()
    if not row:raise W.Problem('range_missing','累计差异版本不属于当前会话或已缺失',404)
    value=json.loads(row[0])
    if H.version({k:v for k,v in value.items() if k!='revision'})!=value.get('revision'):
        raise W.Problem('range_changed','累计差异的保留清单发生变化',409)
    H.authorize(ident,value)
    return value


def materialize(ident,rid,scope,run_id=None):
    if scope=='workspace':return workspace(ident,rid)
    value=resource(ident,rid)
    if scope not in {'run','task'}:raise W.Problem('invalid_scope','请选择本轮或会话任务累计差异')
    if scope=='run' and (not isinstance(run_id,str) or not run_id or len(run_id)>128):
        raise W.Problem('wrong_run','本轮累计差异需要明确轮次身份')
    with FileLock(W.home()/'change-store.lock'),H.database() as db:
        baseline=(db.execute('SELECT snapshot FROM run_baselines WHERE thread=? AND resource=? AND run=?',(ident,rid,run_id)).fetchone()
            if scope=='run' else db.execute('SELECT snapshot FROM baselines WHERE thread=? AND resource=?',(ident,rid)).fetchone())
        if not baseline:raise W.Problem('baseline_missing','此范围尚无已保留基线；不会用当前文件冒充操作前版本',404)
        filters = " AND json_extract(data,'$.source.runId')=?" if scope == "run" else ""
        parameters = (ident, rid, run_id) if scope == "run" else (ident, rid)
        rows=db.execute("SELECT data FROM changes WHERE thread=? AND json_extract(data,'$.resourceId')=?"+filters+" ORDER BY seq LIMIT 4097",parameters).fetchall()
        if len(rows)>4096:raise W.Problem('range_capacity','源操作过多，请按轮次或原操作审查；未截断为全部历史',413)
        changes=[json.loads(row[0]) for row in rows]
        for r in changes:
            H.authorize(ident,r)
            if r.get('revision') and H.version({k:v for k,v in r.items() if k!='revision'})!=r['revision']:
                raise W.Problem('history_changed','累计差异引用的原操作记录发生变化',409)
        signature=dict(format=1,conversationId=ident,resourceId=rid,scope=scope,runId=run_id if scope=='run' else None,
            baseline=baseline[0],sources=[dict(id=r['id'],revision=r.get('revision'),state=r['state']) for r in changes])
        version=H.version(signature)
        if db.execute('SELECT 1 FROM ranges WHERE thread=? AND id=?',(ident,version)).fetchone():return load(db,ident,version)
        H.metadata_capacity(db,8*1024*1024)
        snapshot = H.load_snapshot(db, baseline[0])
        scoped = snapshot["coverage"] == "baseline-unavailable"
        initial = dict(snapshot["files"])
        current=dict(initial);issues={};attributions={};links={};unresolved=set();external=[]
        def issue(code,path=None,cid=None):
            issues[(code,path,cid)]=dict(code=code,path=path,changeId=cid)
        if scoped:
            issue('project_baseline_unavailable')
        # Observation order handles a long-lived debugger containing several
        # narrower operations. Start order would replay their changes twice.
        finished=[r for r in changes if r.get('after')]
        if any(not isinstance(r.get('observationOrder'),int) for r in finished):
            issue('legacy_observation_order_unknown')
            finished.sort(key=lambda r:r.get('finished',r['started']))
        else:finished.sort(key=lambda r:r['observationOrder'])
        for r in changes:
            if not r.get('after') and r["state"] != "failed":
                issue('after_version_unconfirmed',cid=r['id'])
        for row in finished:
            for f in row['files']:
                path=f['path'];before=f['before'];after=f['after']
                if scoped and path not in initial:
                    # Use the first retained before-image. A missing entry in a
                    # scoped baseline says nothing about the file's existence.
                    initial[path] = current[path] = before
                now=current.get(path,ABSENT)
                links.setdefault(path,[]).append(row['id'])
                attributions.setdefault(path,set()).add(f['attribution'])
                if f['status']=='unknown':issue('file_version_unavailable',path,row['id']);unresolved.add(path);continue
                if equivalent(now,after):continue
                if equivalent(now,before):current[path]=after;continue
                if path in unresolved:continue
                try:
                    merged,conflict=merge(H.raw_content(before),H.raw_content(now),H.raw_content(after))
                    if conflict:
                        issue('external_overlap_unresolved',path,row['id']);unresolved.add(path)
                    else:
                        mode=now.get('mode') if before.get('mode')==after.get('mode') else after.get('mode')
                        current[path]=dict(meta(db,merged),**({'mode':mode} if mode else {}))
                        external.append(dict(path=path,changeId=row['id']))
                except W.Problem:
                    issue('cumulative_file_unavailable',path,row['id']);unresolved.add(path)
        files=[]
        for path in sorted(set(initial)|set(current)|unresolved):
            a=initial.get(path,ABSENT);b=current.get(path,ABSENT)
            if path in unresolved:b=dict(kind='unresolved',hash=None)
            if equivalent(a,b) and path not in unresolved:continue
            files.append(dict(path=path,before=a,after=b,
                status='unknown' if path in unresolved else 'added' if a['kind']=='absent' else 'deleted' if b['kind']=='absent' else 'modified',
                attribution='derived_verified_writes' if attributions.get(path)=={'verified_file_write'} else 'observed_during_actions',
                sourceIds=list(dict.fromkeys(links.get(path,[])))))
        result=dict(id=version,conversationId=ident,resourceId=rid,resourcePath=value['path'],scope=scope,
            runId=signature['runId'],baseline=baseline[0],sourceVersions=signature['sources'],
            files=files,issues=list(issues.values()),incomplete=bool(issues),derived=True,
            externalEditsSeparated=external,created=time.time(),
            notice='累计版本由保留的前后版本技术合成，排除可分离的外部编辑；不是当前磁盘文件。命令期间观测变化不自动归功于 Agent。重叠、缺失和未结束动作明确列出。')
        if scoped:
            result.update(projectBaseline="unavailable", projectBaselineReason=snapshot["reason"],
                          coverage="explicit-targets",
                          notice="仅比较从动作历史保留的文件前后版本。完整工程基线不可用，其他文件的改动范围未知。"
                                 + result["notice"])
        return store(db,ident,result)


def git_version(db,ws,revision,path):
    """Read immutable Git blobs without executing textconv or external diffs."""
    target=W.agent_file_path(ws,path)
    if protection.hit('read',target):return dict(kind='protected',hash=None)
    if revision==':':
        rows=W.safe_git(ws,['ls-files','--stage','-z','--',path]).stdout.split(b'\0')
    else:
        rows=W.safe_git(ws,['ls-tree','-z',revision,'--',path]).stdout.split(b'\0')
    entries=[entry for entry in rows if entry]
    if not entries:return dict(ABSENT)
    # Unmerged index stages do not identify one definitive staged version.
    if len(entries)!=1:return dict(kind='unmerged',hash=None)
    fields=entries[0].split(b'\t',1)[0].split()
    mode=fields[0].decode('ascii')
    if revision==':' and fields[2]!=b'0':return dict(kind='unmerged',hash=None)
    oid=fields[1] if revision==':' else fields[2]
    if mode=='120000':return dict(kind='symlink',hash=None,mode=mode)
    if mode not in {'100644','100755'}:return dict(kind='submodule' if mode=='160000' else 'unsupported',hash=None,mode=mode)
    size=int(W.safe_git(ws,['cat-file','-s',oid.decode('ascii')]).stdout)
    if size>W.LIMIT:return dict(kind='large',hash=None,bytes=size,mode=mode)
    raw=W.safe_git(ws,['cat-file','blob',oid.decode('ascii')]).stdout
    return dict(meta(db,raw),mode=mode)


def workspace(ident,rid):
    r=resource(ident,rid,historical=False);ws=W.register(r['path'])
    with FileLock(W.home()/'change-store.lock'),H.database() as db:
        state=W.git_status(ws)
        if not state.get('available') or not state.get('head'):raise W.Problem('git_required','当前资料尚无 Git 基准',409)
        version=H.version(dict(format=1,conversationId=ident,resourceId=rid,scope='workspace',head=state['head'],workspaceRevision=state['revision']))
        if db.execute('SELECT 1 FROM ranges WHERE thread=? AND id=?',(ident,version)).fetchone():return load(db,ident,version)
        H.metadata_capacity(db,8*1024*1024)
        paths=sorted({name for f in state['files'] for name in (f['path'],f.get('oldPath')) if name})
        _,current=H.snapshot(db,ws,paths,explicit=True)
        files=[];issues=[]
        for path in paths:
            a=git_version(db,ws,state['head'],path);b=current['files'].get(path,ABSENT);staged=git_version(db,ws,':',path)
            # On Windows working-tree executability is represented by Git.
            if 'mode' not in b:b=dict(b,mode=staged.get('mode') or '100644')
            unsupported=any(m['kind'] not in {'text','binary','absent'} for m in (a,b,staged))
            if unsupported:issues.append(dict(code='workspace_layer_unavailable',path=path,changeId=None))
            files.append(dict(path=path,before=a,after=b,staged=staged,
                status='unknown' if unsupported else 'added' if a['kind']=='absent' else 'deleted' if b['kind']=='absent' else 'modified',
                attribution='workspace_user_and_agent',sourceIds=[]))
        if W.current_revision(ws)!=state['revision']:raise W.Problem('history_race','读取工作区期间版本已变化，请显式刷新；未混用两个版本',409)
        return store(db,ident,dict(id=version,conversationId=ident,resourceId=rid,resourcePath=r['path'],
            scope='workspace',runId=None,baseline=state['head'],workspaceRevision=state['revision'],
            sourceVersions=[],files=files,issues=issues,incomplete=bool(issues),derived=False,
            externalEditsSeparated=[],created=time.time(),
            notice='这是明确读取时的工作区与暂存版本，相对 Git HEAD；包含用户原有修改，不代表全部由 Agent 完成。历史读取后不会自动跟随磁盘变化。'))


def detail(ident,version,file_offset=0,issue_offset=0):
    with H.database() as db:row=load(db,ident,version)
    if type(file_offset) is not int or not 0<=file_offset<=len(row['files']) or type(issue_offset) is not int or not 0<=issue_offset<=len(row['issues']):
        raise W.Problem('invalid_cursor','累计差异列表游标无效')
    public={k:v for k,v in row.items() if k not in {'resourcePath','sourceVersions','externalEditsSeparated'}}
    files, totals = H.summarize_files(row['files'])
    public.update(files=files[file_offset:file_offset+100],totals=totals,fileOffset=file_offset,fileCount=len(files),
        hasMoreFiles=file_offset+100<len(row['files']),issues=row['issues'][issue_offset:issue_offset+20],
        issueOffset=issue_offset,totalIssues=len(row['issues']),hasMoreIssues=issue_offset+20<len(row['issues']),
        sourceCount=len(row['sourceVersions']),sources=row['sourceVersions'][:100],sourcesExcerpt=len(row['sourceVersions'])>100,
        separatedExternalEdits=len(row['externalEditsSeparated']))
    return public


def run_summary(ident, run_id, offset, frames=None):
    selected = projects.scope(ident)
    names = {resource['id']: resource['name'] for resource in selected['resources']+selected['historicCode'] if resource.get('kind')=='code'}
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= 128 or offset < 0:
        raise W.Problem('invalid_cursor', '轮次或文件游标无效')
    if frames is not None and (len(frames)>100 or any(not re.fullmatch(r'[a-f0-9]{64}', frame) for frame in frames)):
        raise W.Problem('invalid_cursor', '保留变更范围无效')
    with H.database() as db:
        resources = db.execute(
            'SELECT resource FROM run_baselines WHERE thread=? AND run=? ORDER BY resource',
            (ident, run_id),
        ).fetchall() if frames is None else []
        retained = []
        for frame in dict.fromkeys(frames or []):
            try:
                row = load(db, ident, frame)
            except W.Problem as error:
                if error.code == 'resource_denied':
                    continue
                raise
            if row['scope'] != 'run' or row['runId'] != run_id:
                raise W.Problem('wrong_run', '保留变更范围不属于所选轮次', 409)
            retained.append(row)
    files = []
    totals = dict(additions=0, deletions=0, uncounted=0)
    incomplete = False
    changed_file_frames = []
    for (rid,) in resources:
        try:
            row = materialize(ident, rid, 'run', run_id)
        except W.Problem as error:
            if error.code != 'resource_denied':
                incomplete = True
            continue
        retained.append(row)
    for row in retained:
        rid = row['resourceId']
        # Unresolved records signal gaps but are not counted as confirmed writes.
        selected = [(index, file) for index, file in enumerate(row['files']) if file['status'] != 'unknown']
        if selected:
            changed_file_frames.append(row['id'])
        entries, counts = H.summarize_files([file for _, file in selected])
        incomplete |= row['incomplete']
        for key, value in counts.items():
            totals[key] += value
        for (index, _), file in zip(selected, entries):
            files.append(dict(
                path=file['path'], status=file['status'], attribution=file['attribution'],
                beforeKind=file['before']['kind'], afterKind=file['after']['kind'],
                additions=file.get('additions'), deletions=file.get('deletions'),
                resourceId=rid, resourceName=names.get(rid, ''), changeId=row['id'], revision=row['revision'], fileIndex=index,
            ))
    return dict(runId=run_id, files=files[offset:offset+100], fileCount=len(files),
                nextOffset=offset+100 if offset+100<len(files) else None,
                frames=[row['id'] for row in retained],changedFileFrames=changed_file_frames,totals=totals,incomplete=incomplete)


def file_page(ident,version,index,revision,view='diff',after=0,context=3,line=None,hunk=None):
    with H.database() as db:row=load(db,ident,version)
    return H.read_file_page(row,index,revision,view,after,context,line,hunk)


def route(ident,parts,method,query):
    if method!='GET':raise W.Problem('not_found','累计差异只提供只读接口',404)
    get=lambda key,default=None:(query.get(key) or [default])[0]
    if not parts:
        if get('scope') == 'run-summary':
            frames = get('frames')
            return 200,run_summary(ident,get('runId'),int(get('fileOffset','0')),frames.split(',') if frames else None)
        row=materialize(ident,get('resourceId'),get('scope','task'),get('runId'))
        return 200,detail(ident,row['id'])
    if len(parts)==1:return 200,detail(ident,parts[0],int(get('fileOffset','0')),int(get('issueOffset','0')))
    if len(parts)==3 and parts[1]=='files':
        return 200,file_page(ident,parts[0],int(parts[2]),get('revision'),get('view','diff'),int(get('after','0')),int(get('context','3')),int(get('line')) if get('line') is not None else None,int(get('hunk')) if get('hunk') is not None else None)
    if len(parts)==4 and parts[1]=='files' and parts[3]=='search':
        with H.database() as db:row=load(db,ident,parts[0])
        return 200,H.search_file(row,int(parts[2]),get('revision'),get('view','after'),get('query'),int(get('after','0')),get('case')=='1')
    raise W.Problem('not_found','累计差异接口不存在',404)
