"""Observe selected lifecycle events with approved, isolated plugin scripts."""
# 主要引用方包括 `hooks.py` 和 `plugin_packages.py`。
import json
from pathlib import Path
import shlex
import subprocess
from . import plugin_packages as P, plugins, workbench as W, safe_executor as X
from .evidence_redaction import redact

EVENTS={'SessionStart','UserPromptSubmit','PostToolUse','PreCompact','Stop'}


def describe(root,declared):
    if not declared and (root/'hooks/hooks.json').is_file():declared='./hooks/hooks.json'
    if not declared:return [],[]
    rows=[];missing=[]
    try:
        config=P.read_json(P.inside(root,declared)) if isinstance(declared,str) else declared
        config=config.get('hooks',config)
        if not isinstance(config,dict):raise ValueError()
    except (OSError,AttributeError,ValueError,W.Problem):return [],['hooks 清单无法解析']
    from .plugin_stdio import descriptor
    for event,groups in config.items():
        if event not in EVENTS or not isinstance(groups,list):missing.append(f'hooks {event} 事件尚未适配');continue
        for group in groups:
            matcher=group.get('matcher','') if isinstance(group,dict) else None
            handlers=group.get('hooks',[]) if isinstance(group,dict) else []
            # Exact tool names or a simple name alternation. No user regex runs
            # in the host on data-controlled strings.
            import re
            if not isinstance(matcher,str) or len(matcher)>256 or not re.fullmatch(r'\*|[A-Za-z0-9_.|:-]*',matcher) or not isinstance(handlers,list):
                missing.append(f'hooks {event} 匹配器尚未适配');continue
            for handler in handlers:
                try:
                    if not isinstance(handler,dict) or handler.get('type')!='command' or set(handler)-{'type','command','timeout','statusMessage'}:raise ValueError()
                    command=handler.get('command','')
                    if not isinstance(command,str) or len(command)>4096 or any(c in command for c in ('&',';','|','>','<','`','\n','\r')):raise ValueError()
                    argv=shlex.split(command)
                    if not argv:raise ValueError()
                    process=descriptor(root,dict(command=argv[0],args=argv[1:]))
                    timeout=handler.get('timeout',10)
                    if type(timeout) not in {int,float} or not 1<=timeout<=15:raise ValueError()
                    if len(rows)>=12:raise ValueError()
                    rows.append(dict(event=event,matcher=matcher,process=process,timeout=timeout,entry=process['args'][0]))
                except (ValueError,W.Problem):missing.append(f'hooks {event} 仅适配 1–15 秒的包内 Python 观察命令')
    return rows,sorted(set(missing))


def run(ident,event,payload):
    if not ident or event not in EVENTS:return []
    from . import permissions
    from .tools import current_stop
    output=[]
    for package in plugins.grants(ident):
        if not package.get('hooksEnabled'):continue
        for hook in package.get('hooks',[]):
            if hook['event']!=event or hook['matcher'] not in {'','*'} and str(payload.get('tool','')) not in hook['matcher'].split('|'):continue
            row=dict(event=event,pluginId=package['id'],command=hook['entry'],ok=False,rc=None,out='',err='',timeout=False)
            proc=None
            try:
                def validate():
                    if (stop:=current_stop()) and stop():raise W.Problem('cancelled','已取消插件 Hook',409)
                    permissions.effective_mode()
                    plugins.authorized(ident,package['id'],'hooksEnabled')
                def verify(path):
                    if P.inspect(str(path))['fingerprint']!=package['fingerprint']:raise W.Problem('plugin_changed','Hook 副本与授权版本不一致',409)
                data=dict(hook_event_name=event,session_id=ident,cwd='.',tool_name=payload.get('tool'),
                    tool_input=payload.get('args'),tool_response={'ok':payload.get('ok'),'path':payload.get('path')},prompt=payload.get('prompt'))
                raw=json.dumps(redact(data),ensure_ascii=False).encode()
                if len(raw)>128*1024:raise W.Problem('plugin_capacity','Hook 输入超过保留范围',413)
                validate()
                proc=X.start(['python',*hook['process']['args']],cwd=package['root'],duplex=True,minimal_runtime=True,
                    memory=256*1024*1024,processes=1,time_limit=hook['timeout'],validate=validate,verify_copy=verify)
                out,_=proc.communicate(raw,timeout=hook['timeout']+1)
                validate()
                row['rc']=proc.returncode
                row['ok']=proc.returncode==0
                if len(out)>32768:raise W.Problem('plugin_capacity','Hook 输出超过保留范围',413)
                value=out.decode('utf8','replace').strip()
                try:structured=json.loads(value)
                except ValueError:structured=None
                if isinstance(structured,dict):
                    # Recorded user-visible observation only. Decision/control
                    # directives never become policy or model instructions.
                    if structured.get('suppressOutput') is not True:row['out']=redact(str(structured.get('systemMessage','')))[:2000]
                    if set(structured)-{'systemMessage','suppressOutput'}:row['err']='已忽略不支持的 Hook 控制字段'
                else:row['out']=redact(value)[:2000]
            except subprocess.TimeoutExpired:row.update(ok=False,timeout=True,err='Hook 已超时')
            except (OSError,ValueError,W.Problem) as error:row.update(ok=False,err=getattr(error,'message','Hook 执行失败'))
            finally:
                if proc:
                    proc.kill()
                    for stream in (proc.stdin,proc.stdout,proc.stderr):
                        if stream:stream.close()
            output.append(row)
            if len(output)>=12:return output
    return output
