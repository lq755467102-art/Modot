"""Bounded anonymous streamable-HTTP MCP; no models, local process or secrets.

Only a separately enabled endpoint is contacted. Read-only annotations are a
publisher claim checked before calls, not an OS or remote-server sandbox.
"""
# 主要引用方包括 `plugin_stdio.py` 和 `plugin_connections.py`。
import json
import re
import time
import urllib.request
import urllib.parse
from . import netpath,workbench as W

MAX_RESPONSE=1024*1024
GITHUB_RESOURCE = 'https://api.githubcopilot.com/mcp/'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*_args,**_kwargs):raise W.Problem('plugin_redirect','MCP 端点发生重定向，未把工具参数或会话标识转交其他地址',409)


# `plugin_runtime.py` 为当前插件连接构造此客户端。
# `session` 记录服务端会话，`seq` 对应 RPC 请求，`deadline` 约束请求时窗，`validate_live` 在调用前复查连接权限。
class Client:
    def __init__(self,url,deadline=None,validate_live=lambda:None,authorization=None):
        self.url=url;self.session='';self.protocol='2025-06-18';self.seq=0;self.deadline=deadline or time.monotonic()+25
        self.validate_live=validate_live
        self.authorization=authorization
        proxy=netpath.proxy_setting()
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy,'https':proxy} if proxy else {}),NoRedirect())

    def request(self,method,params=None,notify=False):
        self.validate_live()
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise W.Problem('plugin_timeout','插件请求超过本次 25 秒总时限；未自动重试',504)
        self.seq+=1;payload=dict(jsonrpc='2.0',method=method)
        if params is not None:payload['params']=params
        if not notify:payload['id']=self.seq
        raw=json.dumps(payload,ensure_ascii=False).encode()
        if len(raw)>128*1024:raise W.Problem('plugin_capacity','插件请求参数超过 128KiB',413)
        headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream','MCP-Protocol-Version':self.protocol}
        if self.authorization:headers['Authorization']=self.authorization
        if self.session:headers['Mcp-Session-Id']=self.session
        request=urllib.request.Request(self.url,data=raw,headers=headers,method='POST')
        try:
            with self.opener.open(request,timeout=min(10,remaining)) as response:
                self.validate_live()
                if time.monotonic() >= self.deadline:
                    raise W.Problem('plugin_timeout','MCP 响应超过总时限，未自动重试',504)
                if response.headers.get('Content-Encoding','identity')!='identity':raise W.Problem('plugin_encoding','MCP 响应使用未支持编码，未作为完整内容读取',502)
                sid=response.headers.get('Mcp-Session-Id')
                if sid:
                    if len(sid)>512 or any(ord(c)<32 for c in sid):raise W.Problem('plugin_session','MCP 会话标识无效',502)
                    self.session=sid
                if notify:return {}
                total=0;buffer=b'';event=[]
                streaming='text/event-stream' in response.headers.get('Content-Type','').lower()
                while True:
                    self.validate_live()
                    remaining=self.deadline-time.monotonic()
                    if remaining<=0:raise W.Problem('plugin_timeout','MCP 响应超过总时限，未自动重试',504)
                    # Bound each actual socket read as well as total transfer.
                    sock=getattr(getattr(getattr(response,'fp',None),'raw',None),'_sock',None)
                    if sock is not None:sock.settimeout(min(2,remaining))
                    chunk=response.read1(8192)
                    if not chunk:break
                    total+=len(chunk)
                    if total>MAX_RESPONSE:raise W.Problem('plugin_capacity','MCP 响应超过 1MiB，未声称完整结果',413)
                    buffer+=chunk
                    if streaming:
                        while b'\n' in buffer:
                            line,buffer=buffer.split(b'\n',1);line=line.rstrip(b'\r')
                            if line.startswith(b'data:'):event.append(line[5:].lstrip())
                            elif not line and event:
                                result=self.reply(b'\n'.join(event));event=[]
                                if result is not None:return result
                if streaming:
                    if buffer.startswith(b'data:'):event.append(buffer[5:].strip())
                    result=self.reply(b'\n'.join(event)) if event else None
                else:result=self.reply(buffer)
                if result is None:raise W.Problem('plugin_protocol','未收到与当前请求匹配的完整 MCP 回执',502)
                return result
        except W.Problem:raise
        except Exception as error:
            code=getattr(error,'code',None)
            if hasattr(error,'close'):error.close()
            if isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError):
                raise W.Problem('plugin_timeout','MCP 连接或读取超时，未自动重试',504) from error
            if code in (401,403):raise W.Problem('plugin_auth_required','MCP 端点要求另行认证；当前匿名适配未提供凭据，也未自动重试',409) from error
            raise W.Problem('plugin_unavailable','MCP 连接或响应未确认（'+('HTTP '+str(code) if isinstance(code,int) else type(error).__name__)+'）；未自动重试',502) from error

    def reply(self,raw):
        value=json.loads(raw.decode('utf8'))
        if not isinstance(value,dict) or value.get('id')!=self.seq:return None
        if 'error' in value:raise W.Problem('plugin_remote_error','MCP 服务返回错误，未重试',502)
        result=value.get('result')
        if not isinstance(result,dict):raise W.Problem('plugin_protocol','MCP result 不是对象',502)
        authorization=getattr(self,'authorization',None)
        if authorization and authorization[7:] in json.dumps(result,ensure_ascii=False):
            raise W.Problem('plugin_credential_echo','MCP 回执包含连接凭据，未交给模型或写入回执',502)
        return result

    def start(self):
        result=self.request('initialize',dict(protocolVersion='2025-06-18',capabilities={},clientInfo=dict(name='harness-plugin-adapter',version='0.1')))
        protocol=result.get('protocolVersion')
        if not isinstance(protocol,str) or not re.fullmatch(r'20\d\d-\d\d-\d\d',protocol):raise W.Problem('plugin_protocol','MCP 协议协商回执无效',502)
        self.protocol=protocol
        self.request('notifications/initialized',notify=True)
        return result

    def tools(self):
        rows=[];cursor=None;seen=set()
        for _ in range(25):
            page=self.request('tools/list',dict(cursor=cursor) if cursor else {})
            tools=page.get('tools')
            if not isinstance(tools,list):raise W.Problem('plugin_protocol','MCP 工具清单无效',502)
            for item in tools:
                if not isinstance(item,dict) or not isinstance(item.get('name'),str) or len(item['name'])>128:raise W.Problem('plugin_protocol','MCP 工具身份无效',502)
                if item['name'] in seen:raise W.Problem('plugin_protocol','MCP 工具身份重复',502)
                seen.add(item['name']);rows.append(item)
            if len(rows)>1000 or len(json.dumps(rows).encode())>MAX_RESPONSE:raise W.Problem('plugin_capacity','MCP 工具目录超过保留边界，未作为完整目录使用',413)
            following=page.get('nextCursor')
            if not following:return rows
            if not isinstance(following,str) or len(following)>4096 or following==cursor:raise W.Problem('plugin_protocol','MCP 分页游标无效',502)
            cursor=following
        raise W.Problem('plugin_capacity','MCP 工具目录超过 25 页，未截断为完整目录',413)

    def close(self):
        remaining = self.deadline - time.monotonic()
        if not self.session or remaining <= 0:
            return
        try:
            self.validate_live()
            headers={'Mcp-Session-Id':self.session}
            if self.authorization:headers['Authorization']=self.authorization
            with self.opener.open(urllib.request.Request(self.url,method='DELETE',headers=headers),timeout=min(2,remaining)):pass
        except Exception:pass


def readonly(spec, *, endpoint=None):
    hints=spec.get('annotations',{})
    if not isinstance(hints,dict) or hints.get('readOnlyHint') is not True:
        return False
    # GitHub omits the destructive hint on its read-only tools.
    return hints.get('destructiveHint') is False or (endpoint==GITHUB_RESOURCE and 'destructiveHint' not in hints)
