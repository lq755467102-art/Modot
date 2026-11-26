"""Recoverable native tool text, after rendering and before model delivery.

Text retention follows DSH 39163dd spill-policy: heuristic UTF-16 length/4 +
four framing tokens, reserve the notice, split head/tail budgets evenly. The
JSON envelope is a Modot adapter preserving factual status outside excerpts.
This does not change request-capacity accounting or history compaction.
"""
# 主要引用方包括 `evidence.py` 和 `loop.py`。
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

MAX_INLINE_TOKENS=12500
MAX_BYTES=8*1024*1024  # Existing action-output persistence ceiling; failures stay inline.
PAGE_BYTES=50*1024
FORMAT='model-json-v1'
GAP='\n\n[...]\n\n'


def estimate_text(text):
    units=len(text.encode('utf-16-le','surrogatepass'))//2
    return (units+3)//4+4


def retain_text(text,budget,price=estimate_text):
    """Contiguous ends, no overlap; Python slices preserve Unicode code points."""
    def fit(value,allowance,tail=False):
        low,high=0,len(value)
        while low<high:
            middle=(low+high+1)//2
            candidate=value[-middle:] if tail else value[:middle]
            if price(candidate)<=allowance:low=middle
            else:high=middle-1
        return (value[-low:] if tail else value[:low]) if low else ''
    head=fit(text,(budget+1)//2)
    tail=fit(text[len(head):],budget//2,True)
    return head,tail,len(text.encode('utf8'))-len(head.encode('utf8'))-len(tail.encode('utf8'))


def access_version(ident,resource_ids=None,plugin_id=None):
    """Snapshot the existing read scope/grants, never infer scope from result text."""
    from . import projects,plugins
    resources=projects.scope(ident)['resources']
    if resource_ids is not None:resources=[r for r in resources if r['id'] in resource_ids]
    resource_keys=sorted((r['id'],r.get('kind'),r.get('path'),r.get('permission')) for r in resources)
    grants=[p for p in plugins.grants(ident) if p['id']==plugin_id] if plugin_id else []
    plugin_keys=sorted((p['id'],p.get('fingerprint'),p.get('revision'),
        bool(p.get('skillsEnabled')),bool(p.get('remoteEnabled')),bool(p.get('hooksEnabled'))) for p in grants)
    from .context_manager import fingerprint
    return fingerprint([resource_keys,plugin_keys])


def _path(directory,action_id):
    from . import workbench as W
    if not isinstance(action_id,str) or not re.fullmatch('[0-9a-f]{32}',action_id):
        raise W.Problem('invalid_evidence','格式化输出动作引用无效')
    root=Path(directory)/'model-output'
    if not root.resolve().is_relative_to(Path(directory).resolve()):
        raise W.Problem('path_escape','格式化输出目录已变化',403)
    return W.file_path({'path':str(root)},action_id+'.txt')


def save(directory,action_id,data):
    """Exclusive immutable blob; publish its event only after fsync succeeds."""
    if len(data)>MAX_BYTES:raise ValueError('formatted output exceeds persistence capacity')
    path=_path(directory,action_id);path.parent.mkdir(exist_ok=True)
    digest=hashlib.sha256(data).hexdigest()
    created=False
    try:
        with path.open('xb') as stream:
            created=True;stream.write(data);stream.flush();os.fsync(stream.fileno())
    except FileExistsError:
        if path.stat().st_size!=len(data) or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError('immutable formatted output identity conflict')
        with path.open('r+b') as stream:os.fsync(stream.fileno())
    except Exception:
        if created:
            try:path.unlink()
            except OSError:pass
        raise
    return dict(state='retained',id=action_id,hash=digest,bytes=len(data),format=FORMAT)


def _bounded_page(out,policy):
    if not isinstance(out,dict):return False
    if policy=='code':
        return (isinstance(out.get('text'),str) and len(out['text'].encode('utf8'))<=PAGE_BYTES
            and type(out.get('startLine')) is int and type(out.get('endLine')) is int
            or isinstance(out.get('matches'),list) and 'fileRevisions' in out and len(json.dumps(out,ensure_ascii=False))<=18000)
    if policy in {'requirements','reference'}:
        return (isinstance(out.get('text'),str) and len(out['text'])<=12000
            and type(out.get('next')) is int and type(out.get('hasMore')) is bool)
    if policy=='history':
        return (isinstance(out.get('text'),str) and len(out['text'])<=4000
            and type(out.get('nextOffset')) is int and type(out.get('hasMore')) is bool
            and isinstance(out.get('messageVersion'),str))
    return (policy=='formatted' and (out.get('format')==FORMAT or isinstance(out.get('evidenceRevision'),str)) and isinstance(out.get('text'),str)
        and len(out['text'].encode('utf8'))<=PAGE_BYTES and type(out.get('next')) is int
        and type(out.get('hasMore')) is bool)


def _facts(out):
    if not isinstance(out,dict):return {}
    facts={k:deepcopy(out[k]) for k in ('ok','error','code','needs_human','question','state',
        'execution_state','verification','revision','operationId','approvalRefs',
        '_harnessEvidence','_harnessProjection') if k in out}
    if isinstance(out.get('process'),dict):
        facts['process']={k:deepcopy(out['process'][k]) for k in ('id','state','exitCode','revision') if k in out['process']}
    if isinstance(out.get('diagnostics'),dict):
        diagnostics=out['diagnostics']
        if len(json.dumps(diagnostics,ensure_ascii=False))<=12000:
            facts['diagnostics']=deepcopy(diagnostics)
        else:
            facts['diagnostics']={k:deepcopy(diagnostics[k]) for k in ('counts','firstFailure','testReport') if k in diagnostics}
            facts['diagnosticsPartial']=True
    return facts


def payload(out,session,*,action_id,run_id,tool_name,args=None,page_policy=None,recoverable=False,
            record=None,renderer='legacy_json',cap=MAX_INLINE_TOKENS):
    raw=json.dumps(out,ensure_ascii=False)
    if _bounded_page(out,page_policy) or estimate_text(raw)<=cap:return raw
    try:
        if session is None or not recoverable or record is None:
            raise ValueError('no owned reachable formatted-output reader')
        data=raw.encode('utf8')
        digest=hashlib.sha256(data).hexdigest()
        ref=dict(format=FORMAT,hash=digest,bytes=len(data),omittedBytes=len(data),
            load=dict(tool='use_tool_group',arguments=dict(group='history')),
            read=dict(tool='read_action_evidence',arguments=dict(action_id=action_id,run_id=run_id,view='formatted')),
            note='中间内容已省略；可回取当时完整格式化模型正文。原始回执用view=raw；有hasMore时按next和evidenceRevision继续。')
        preview=dict(_facts(out),head='',gap=GAP,tail='',spill=ref)
        if tool_name == "inspect_subtasks" and isinstance(out.get("statusScope"), dict):
            preview["statusScope"] = out["statusScope"]
        preview.setdefault('_harnessEvidence',dict(actionId=action_id,runId=run_id))
        reserve=estimate_text(json.dumps(preview,ensure_ascii=False))
        if reserve>cap:raise ValueError('factual fields and recovery notice exceed inline budget')
        # JSON excerpts have escaping overhead beyond DSH text blocks. Price
        # that real representation too, rather than undercounting the envelope.
        price=lambda text:estimate_text(json.dumps(text,ensure_ascii=False)[1:-1])
        head,tail,omitted=retain_text(raw,max(0,cap-reserve),price)
        preview.update(head=head,tail=tail);ref['omittedBytes']=omitted
        content=json.dumps(preview,ensure_ascii=False)
        if estimate_text(content)>cap:raise ValueError('preview exceeds inline budget')
        arguments=args or {}
        ids=arguments.get('resource_ids',[])
        ids=[*ids] if isinstance(ids,list) and all(isinstance(r,str) for r in ids) else []
        if isinstance(arguments.get('resource_id'),str):ids.append(arguments['resource_id'])
        ids=sorted(set(ids)) or None
        plugin=arguments.get('plugin_id') if isinstance(arguments.get('plugin_id'),str) else None
        access=access_version(Path(session.dir).name,ids,plugin)
        meta=save(session.dir,action_id,data)
        meta.update(accessVersion=access,resourceIds=ids,pluginId=plugin,renderer=str(renderer)[:80])
        record('model_output',action_id=action_id,tool=tool_name,role='executor',formattedOutput=meta)
        return content
    except Exception as error:
        if record is not None:
            try:record('tool_spill_warning',action_id=action_id,tool=tool_name,
                errorType=type(error).__name__,note='结果外存未发布；保留完整模型正文内联，不重放工具。')
            except Exception:pass
        return raw


def read_page(directory, value, after, *, ident, role, root=None):
    """Called only after the existing history reader verifies the action source."""
    from . import evidence as E,projects,plugins,workbench as W
    from .code_service import context_resource
    from .subtasks import audit_child, validate_child
    meta=value.get('formattedOutput')
    if type(after) is not int or after<0:raise W.Problem('invalid_cursor','格式化输出游标无效')
    if value.get('formattedOutputConflict'):
        raise W.Problem('evidence_changed','同一动作的格式化输出关联冲突，未选择任一版本',409)
    if not isinstance(meta,dict) or meta.get('state')!='retained' or meta.get('format')!=FORMAT:
        raise W.Problem('formatted_output_missing','当时格式化全文未保留；不能用原始回执重新生成冒充',410)
    if not role or role != value.get('role'):
        raise W.Problem('wrong_role','只能回取当前角色自己的格式化输出',403)
    ids,plugin=meta.get('resourceIds'),meta.get('pluginId')
    resources=projects.scope(ident)['resources']
    selected=[r for r in resources if ids is None or r['id'] in ids]
    if ((ids is not None and set(ids)!={r['id'] for r in selected})
            or any(r.get('permission')!='read' for r in selected)):
        raise W.Problem('resource_denied','该格式化输出关联的资料当前无读取授权',403)
    if plugin is not None:
        grant=next((p for p in plugins.grants(ident) if p['id']==plugin),{})
        names=(('skillsEnabled',) if value.get('tool') in {'read_plugin_skill','read_plugin_file'} else
            ('remoteEnabled',) if value.get('tool') in {'call_plugin_tool','inspect_plugin_tools','find_github_repositories'} else
            ('skillsEnabled','remoteEnabled','hooksEnabled'))
        if not any(grant.get(name) for name in names):
            raise W.Problem('resource_denied','该格式化输出关联的插件能力当前未启用',403)
    if meta.get('accessVersion')!=access_version(ident,ids,plugin):
        raise W.Problem('resource_denied','该输出保存时的读取范围或插件授权已变化',403)
    if value.get('tool')=='read_artifact' or (value.get('impact') or {}).get('imageView'):
        E.check_output_access(ident,value)
    if root is None:
        audit_child(ident)
        E.check_output_access(ident, value)
        for resource in selected:
            audit_child(ident, resource["id"])
    else:
        for resource in selected:
            validate_child(ident, resource["id"])
            if resource.get("kind") == "code":
                context_resource(root, resource["id"])
    path=_path(directory,value['id'])
    if (meta.get('id')!=value['id'] or not path.is_file() or type(meta.get('bytes')) is not int
            or path.stat().st_size!=meta['bytes'] or meta['bytes']>MAX_BYTES):
        raise W.Problem('formatted_output_missing','格式化全文已删除或不完整',410)
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=meta.get('hash'):
        raise W.Problem('evidence_changed','格式化全文与当时保留的内容哈希不一致',409)
    if root is None:
        from .evidence_redaction import redact
        data = json.dumps(redact(json.loads(data)), ensure_ascii=False).encode("utf-8")
    return E._output_page(data,after,value,complete=True,hash=meta['hash'],format=FORMAT,page_size=PAGE_BYTES)
