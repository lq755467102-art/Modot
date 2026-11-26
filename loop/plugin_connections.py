"""Per-server MCP credentials and user-started OAuth connections."""
# 主要引用方包括 `plugins.py` 和 `plugin_runtime.py`。

import base64
import ctypes
from ctypes import wintypes as wt
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import secrets
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs,urlencode,urlsplit,urlunsplit
from urllib.request import Request,build_opener,ProxyHandler
from . import workbench as W, plugins, plugin_mcp as M, netpath
from .filelock import atomic_json, read_text

GITHUB_RESOURCE = M.GITHUB_RESOURCE
GITHUB_DEVICE_CODE = "https://github.com/login/device/code"
GITHUB_DEVICE_VERIFY = "https://github.com/login/device"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
GITHUB_APP_CLIENT_ID = "Iv23liJ8ZUAIkJBiYmCD"  # Public GitHub App identifier.
GITHUB_APP_SLUG = "modot-l"
GITHUB_WEB = "https://github.com"
GITHUB_API = "https://api.github.com"
GITHUB_PAGE_SIZE = 20

_flows={}
_lock=threading.RLock()


# Windows DPAPI 使用的字节缓冲区。
# `length` 是有效字节数，`data` 指向缓冲区，系统返回的内存由 `crypt` 释放。
class Blob(ctypes.Structure):
    _fields_=[('length',wt.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]


# 在当前 Windows 账户下保护或解开凭据字节，并释放 Windows 分配的返回缓冲区。
def crypt(value,encrypt):
    if os.name!='nt':raise W.Problem('plugin_vault','此平台尚无凭据保护实现',503)
    raw=(ctypes.c_ubyte*len(value)).from_buffer_copy(value)
    inp,out=Blob(len(value),raw),Blob()
    api=ctypes.WinDLL('crypt32',use_last_error=True)
    call=api.CryptProtectData if encrypt else api.CryptUnprotectData
    call.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wt.DWORD,ctypes.POINTER(Blob)]
    call.restype=wt.BOOL
    if not call(ctypes.byref(inp),None,None,None,None,1,ctypes.byref(out)):
        raise W.Problem('plugin_vault','无法使用当前 Windows 账户保护或读取凭据',503)
    free=ctypes.WinDLL('kernel32').LocalFree
    free.argtypes=[ctypes.c_void_p];free.restype=ctypes.c_void_p
    try:return ctypes.string_at(out.data,out.length)
    finally:free(out.data)


# 通过不透明的连接引用定位宿主受保护目录中的保留文件。
def vault(ref):
    if not re.fullmatch('[a-f0-9]{32}',ref or ''):raise W.Problem('plugin_vault','连接身份无效',503)
    root=W.home()/'security'/'plugin-connections'
    root.mkdir(parents=True,exist_ok=True)
    path=root/(ref+'.json')
    if not root.resolve().is_relative_to(W.home().resolve()) or path.is_symlink():
        raise W.Problem('plugin_vault','连接保留路径已变化',503)
    return path


# 从 `plugins.py` 的当前安装登记取得服务器，只为已支持的 HTTP MCP 传输管理授权。
def target(pid,server,value=None):
    value=value or plugins.state()['plugins'].get(pid)
    endpoint=next((s for s in (value or {}).get('servers',[]) if s['name']==server),None)
    if not value or value.get('removed') or not endpoint or not endpoint['supported'] or endpoint['transport'] not in {'http','streamable-http'}:
        raise W.Problem('plugin_connection','仅可连接已保留的 HTTP MCP 服务',409)
    return value,endpoint


# 在插件登记锁内核对原安装修订。先写凭据再提交引用，提交失败清理新凭据，成功后清理旧引用。
def attach(pid,server,expected,secret):
    ref=None
    with plugins.store_write():
        data=plugins.state();value,endpoint=target(pid,server,data['plugins'].get(pid))
        if type(expected) is not int or value['revision']!=expected:raise W.Problem('plugin_changed','插件设置已变化，请重新核对连接',409)
        rows=value.setdefault('connections',{})
        old=rows.get(server)
        if secret:
            ref=secrets.token_hex(16)
            record=dict(secret,url=endpoint['url'],pluginId=pid,server=server)
            atomic_json(vault(ref),dict(data=base64.b64encode(crypt(json.dumps(record).encode(),True)).decode()))
            rows[server]=dict(ref=ref,method=secret['method'],expiresAt=secret.get('expiresAt'))
        else:rows.pop(server,None)
        value['revision']+=1
        try:plugins.save(data)
        except Exception:
            if ref:vault(ref).unlink(missing_ok=True)
            raise
        if old:plugins.forget_connections({server:old})
    return dict(ok=True,connected=bool(secret),revision=value['revision'])


# 统一核对原令牌可用于 Bearer 头的格式和长度，调用者按连接身份保存或使用。
def bearer(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*',value) or len(value)>8192:
        raise W.Problem('plugin_credential','连接凭据格式无效',400)
    return value


# 打开登记引用后再次核对安装包、服务器和实际地址，避免把原连接用于新的服务。
def read_secret(package,endpoint,saved):
    try:
        path=vault(saved['ref'])
        if path.stat().st_size>65536:raise ValueError('oversize')
        row=json.loads(crypt(base64.b64decode(json.loads(read_text(path))['data'],validate=True),False))
        if (row['pluginId'],row['server'],row['url'])!=(package['id'],endpoint['name'],endpoint['url']):
            raise ValueError('connection owner changed')
        return row
    except (OSError,ValueError,KeyError,TypeError) as error:
        raise W.Problem('plugin_vault','连接凭据无法核对，请重新连接',409) from error


# 由 `plugin_runtime.py` 的 HTTP 连接使用，只转交仍在有效期内且归属一致的凭据。
def authorization(package,endpoint):
    saved=package.get('connections',{}).get(endpoint['name'])
    if not saved:return None
    if saved.get('expiresAt') is not None and saved['expiresAt']<=time.time()+10:
        raise W.Problem('plugin_auth_expired','MCP 登录已过期，请重新连接',409)
    return 'Bearer '+bearer(read_secret(package,endpoint,saved).get('accessToken'))


# 只为 GitHub 官方目标取客户端身份，服务的本地设置优先于随包配置。
def github_client_id(package,endpoint):
    if endpoint['url']!=GITHUB_RESOURCE:return None
    config=package.get('githubApps',{}).get(endpoint['name'],{})
    return config.get('clientId') or (GITHUB_APP_CLIENT_ID if package['name']=='github-mcp' else None)


# 管理页只能给 GitHub 目标配置公开 Client ID。
# 变更提交新的插件修订，并撤下旧连接引用。
def github_app_config(pid,server,body):
    remove=body.get('remove') is True
    if set(body)-{'expectedRevision','_request_id','remove' if remove else 'clientId'}:
        raise W.Problem('github_app_client','GitHub 设备授权只接受公开的 Client ID',400)
    client_id=body.get('clientId')
    if not remove and (not isinstance(client_id,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}',client_id)):
        raise W.Problem('github_app_client','GitHub App Client ID 无效',400)
    expected=body.get('expectedRevision')
    with plugins.store_write():
        data=plugins.state();value,endpoint=target(pid,server,data['plugins'].get(pid))
        if type(expected) is not int or value['revision']!=expected:
            raise W.Problem('plugin_changed','插件设置已变化，请重新核对连接',409)
        if endpoint['url']!=GITHUB_RESOURCE:
            raise W.Problem('plugin_connection','此服务不使用 GitHub App 配置',409)
        configs=value.setdefault('githubApps',{})
        old=configs.get(server)
        new=None if remove else dict(clientId=client_id)
        if old!=new:
            configs.pop(server,None)
            if new:configs[server]=new
            connection=value.get('connections',{}).pop(server,None)
            value['revision']+=1
            plugins.save(data)
            plugins.forget_connections({k:v for k,v in (('config',old),('connection',connection)) if v and 'ref' in v})
        return dict(ok=True,githubAppConfigured=bool(github_client_id(value,endpoint)),revision=value['revision'])


# 供 OAuth 发现、令牌交换和客户端注册检查服务地址，拒绝含账户信息、片段和非法字符的地址。
def valid_url(url):
    if not isinstance(url,str):raise W.Problem('plugin_oauth','OAuth 地址无效',409)
    try:p=urlsplit(url)
    except (TypeError,ValueError):p=None
    if not p or not p.hostname or p.username or p.password or p.fragment or len(url)>2048 or any(ord(c)<=32 or ord(c)==127 for c in url) or (p.scheme!='https' and not (p.scheme=='http' and p.hostname in {'127.0.0.1','localhost','::1'})):
        raise W.Problem('plugin_oauth','OAuth 地址必须为 HTTPS 或本机服务',409)
    return url


# 使用 `netpath.py` 的代理设置读取有界 JSON。
# GitHub REST 列表携带账号授权，设备轮询则可读取服务声明的 OAuth 错误。
def request_json(url,fields=None,form=False,*,oauth_errors=False,authorization=None,max_bytes=None):
    valid_url(url)
    proxy=netpath.proxy_setting()
    opener=build_opener(ProxyHandler({'http':proxy,'https':proxy} if proxy else {}),M.NoRedirect())
    headers={'Accept':'application/json'};body=None
    if authorization:
        headers.update({'Accept':'application/vnd.github+json','Authorization':authorization,
                        'X-GitHub-Api-Version':'2026-03-10'})
    limit=max_bytes or (1024*1024 if authorization else 65536)
    if fields is not None:
        headers['Content-Type']='application/x-www-form-urlencoded' if form else 'application/json'
        body=(urlencode(fields) if form else json.dumps(fields)).encode()
    try:
        deadline=time.monotonic()+10
        request=Request(url,data=body,headers=headers)
        http_error=False
        try:
            response=opener.open(request,timeout=10)
        except HTTPError as error:
            if not oauth_errors or error.code not in (400,401,403,429):raise
            response=error
            http_error=True
        with response:
            raw=bytearray()
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('OAuth response deadline')
                sock=getattr(getattr(getattr(response,'fp',None),'raw',None),'_sock',None)
                if sock is not None:sock.settimeout(min(2,remaining))
                chunk=response.read1(8192)
                if not chunk:break
                raw.extend(chunk)
                if len(raw)>limit:raise ValueError('oversize')
        row=json.loads(raw)
        if not isinstance(row,dict):raise ValueError('not an object')
        if http_error:
            if not isinstance(row.get('error'),str):raise ValueError('missing OAuth error')
            return {'error':row['error']}
        return row
    except Exception as error:
        if isinstance(error,HTTPError):error.close()
        raise W.Problem('plugin_oauth','OAuth 服务返回失败或无法核对',502) from error


# 以当前账号授权请求 GitHub REST 路径，区分登录失效、限速、拒绝访问和暂时不可读。
def github_get(path,auth,*,max_bytes=None):
    try:
        options = {"max_bytes": max_bytes} if max_bytes is not None else {}
        return request_json(GITHUB_API+path,authorization=auth,**options)
    except W.Problem as error:
        cause=error.__cause__
        if isinstance(cause,HTTPError):
            if cause.code==401:
                raise W.Problem('plugin_auth_required','GitHub 账号授权已失效，请重新登录',401) from error
            if cause.code==429 or (cause.code==403 and cause.headers.get('X-RateLimit-Remaining')=='0'):
                raise W.Problem('github_repository_unavailable','GitHub 请求已被限速，请稍后刷新',429) from error
            if cause.code in (403,404):
                raise W.Problem('github_repository_access','GitHub 拒绝访问此授权范围，请核对 App 安装与仓库权限',403) from error
        raise W.Problem('github_repository_unavailable','暂时无法读取 GitHub 授权，请重试',502) from error


# 校验 REST 列表的分页形状。只有当前页有条目且总数仍未读完时才返回下一页。
def github_page(data,key,page):
    rows,total=data.get(key),data.get('total_count')
    if (not isinstance(rows,list) or len(rows)>GITHUB_PAGE_SIZE or type(total) is not int
            or total<0 or any(not isinstance(row,dict) for row in rows)):
        raise W.Problem('github_repository_unavailable','GitHub 返回的授权列表无法核对',502)
    return rows, page+1 if rows and page*GITHUB_PAGE_SIZE<total else None


# 读取解析后的查询字符串。分页、修订和安装编号只接受一个正整数值。
def github_query_number(query,key,default=None):
    values=query.get(key,[])
    if not values and default is not None:return default
    if len(values)!=1 or not re.fullmatch('[1-9][0-9]{0,14}',values[0]):
        raise W.Problem('invalid_cursor','GitHub 授权查询参数无效',400)
    return int(values[0])


# 从当前 GitHub 账号实时读取 App 安装页。每项保留安装编号、账号和类型，供选择授权范围。
def github_installation_page(auth, page):
    params = urlencode(dict(per_page=GITHUB_PAGE_SIZE, page=page))
    rows, next_page = github_page(github_get('/user/installations?' + params, auth), 'installations', page)
    installations = []
    for row in rows:
        ident, account = row.get('id'), row.get('account')
        owner = account.get('login') if isinstance(account, dict) else None
        if (type(ident) is not int or ident <= 0 or not isinstance(owner, str)
                or not re.fullmatch('[A-Za-z0-9_-]{1,100}', owner)):
            raise W.Problem('github_repository_unavailable', 'GitHub 返回的安装记录无法核对', 502)
        installations.append(dict(id=ident, owner=owner, slug=row.get('app_slug'), type=row.get('target_type')))
    return installations, next_page


# 按原安装编号实时读取授权内仓库。只向调用方返回完整仓库名和私有属性。
def github_repository_page(auth, ident, page):
    params = urlencode(dict(per_page=GITHUB_PAGE_SIZE, page=page))
    rows, next_page = github_page(github_get(f'/user/installations/{ident}/repositories?' + params, auth),
                                  'repositories', page)
    repositories = []
    for row in rows:
        name = row.get('full_name')
        if (not isinstance(name, str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}/[A-Za-z0-9_.-]{1,100}', name)
                or type(row.get('private')) is not bool):
            raise W.Problem('github_repository_unavailable', 'GitHub 返回的仓库信息无法核对', 502)
        repositories.append(dict(fullName=name, private=row['private']))
    return repositories, next_page


# 账号与仓库查询必须持有 GitHub 设备授权。手动保存的访问令牌仍可用于相应 MCP 连接。
def github_account_authorization(value, endpoint):
    if endpoint['url'] != GITHUB_RESOURCE:
        raise W.Problem('plugin_connection', '此服务不支持 GitHub 仓库授权', 409)
    if value.get('connections', {}).get(endpoint['name'], {}).get('method') != 'github-device':
        raise W.Problem('plugin_auth_required', '请先在 GitHub 插件页连接 GitHub 账号', 401)
    return authorization(value, endpoint)


# `plugin_runtime.py` 在模型按需定位仓库时调用。
# 续页位置绑定本次查询、安装修订和服务器，查找时仍读实时授权。
def github_find_repositories(value, endpoint, query, cursor, validate_access, approve_network):
    """Read one live page of authorized repositories. A cursor grants no access."""
    if not isinstance(query, str) or len(query) > 201 or any(ord(c) < 32 for c in query):
        raise W.Problem('plugin_arguments', '请提供仓库名、关键词或 owner/repo')
    query = query.strip().casefold()
    owner, separator, _ = query.partition('/')
    scope = W.digest(json.dumps([value['id'], value['revision'], endpoint['name'], query]).encode())[:16]
    install_page, offset, repo_page, previous_installation = 1, 0, 1, 0
    if cursor is not None:
        position = re.fullmatch(r'([0-9a-f]{16}):([1-9][0-9]{0,14}):([0-9]{1,2}):([1-9][0-9]{0,14}):([0-9]{1,20})',
                                cursor) if isinstance(cursor, str) else None
        if not position or position[1] != scope:
            raise W.Problem('invalid_cursor', 'GitHub 查询或连接已变化，请从首页重新查找', 409)
        install_page, offset, repo_page, previous_installation = map(int, position.groups()[1:])
    auth = github_account_authorization(value, endpoint)
    approve_network()
    validate_access()
    installs, next_install_page = github_installation_page(auth, install_page)
    validate_access()
    if cursor is not None and (offset >= len(installs) or previous_installation not in (0, installs[offset]['id'])):
        raise W.Problem('invalid_cursor', 'GitHub 安装范围已变化，请从首页重新查找', 409)

    candidates = [(index, row['id']) for index, row in enumerate(installs)
                  if index >= offset and (not separator or row['owner'].casefold() == owner)]
    matches, next_position = [], None
    if next_install_page:
        next_position = (next_install_page, 0, 1, 0)
    if candidates:
        index, ident = candidates[0]
        repositories, next_repo_page = github_repository_page(auth, ident, repo_page)
        validate_access()
        matches = [row for row in repositories
                   if ((row['fullName'].casefold() == query) if separator else
                       (query in row['fullName'].casefold().split('/', 1)[1]))]
        if next_repo_page:
            next_position = (install_page, index, next_repo_page, ident)
        elif len(candidates) > 1:
            next_index, next_ident = candidates[1]
            next_position = (install_page, next_index, 1, next_ident)
    continuation = ':'.join([scope, *map(str, next_position)]) if next_position else None
    result = dict(ok=True, repositories=matches, next=continuation, revision=value['revision'])
    if not matches and not continuation:
        result['notice'] = '已到此查询的最后一页。若此前各页均无目标私库，请在 GitHub 插件页添加仓库。'
    return result


# `plugin_runtime.py` 为界面转发账号安装页或具体仓库页。
# 查询前后都核对安装修订，防止返回已切换账号的旧数据。
def github_access(pid,server,operation,query):
    account=operation=='github-account'
    allowed={'revision','page'} if account else {'revision','page','installationId'}
    if set(query)-allowed:raise W.Problem('invalid_cursor','GitHub 授权查询参数无效',400)
    revision=github_query_number(query,'revision')
    page=github_query_number(query,'page',1)
    value,endpoint=target(pid,server)
    if value['revision']!=revision:
        raise W.Problem('plugin_changed','插件或 GitHub 账号已变化，请刷新',409)
    auth=github_account_authorization(value,endpoint)
    if account:
        login=github_get('/user',auth).get('login')
        rows,next_page=github_installation_page(auth,page)
        if not isinstance(login,str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}',login):
            raise W.Problem('github_repository_unavailable','GitHub 返回的账号身份无法核对',502)
        installs=[]
        slug=GITHUB_APP_SLUG if github_client_id(value,endpoint)==GITHUB_APP_CLIENT_ID else None
        for row in rows:
            ident,name,row_slug=row['id'],row['owner'],row['slug']
            if not slug and isinstance(row_slug,str) and re.fullmatch('[a-z0-9-]{1,100}',row_slug):
                slug=row_slug
            manage=None
            if row['type']=='Organization':
                manage=f'{GITHUB_WEB}/organizations/{name}/settings/installations/{ident}'
            elif name.casefold()==login.casefold():
                manage=f'{GITHUB_WEB}/settings/installations/{ident}'
            installs.append(dict(id=ident,owner=name,manageUrl=manage))
        install_url=f'{GITHUB_WEB}/apps/{slug}/installations/new' if slug else None
        result=dict(login=login,installations=installs,installUrl=install_url,nextPage=next_page)
    else:
        ident=github_query_number(query,'installationId')
        repos,next_page=github_repository_page(auth,ident,page)
        result=dict(installationId=ident,repositories=repos,nextPage=next_page)
    current,_=target(pid,server)
    if current['revision']!=revision:
        raise W.Problem('plugin_changed','读取期间 GitHub 账号或插件已变化，请刷新',409)
    return dict(result,revision=revision)


# 从已核对的服务地址得到发现元数据位置，保留服务部署路径。
def well_known(url,name):
    p=urlsplit(valid_url(url))
    return urlunsplit((p.scheme,p.netloc,'/.well-known/'+name+p.path.rstrip('/'),'',''))


# 收集资源和授权方元数据，要求身份一致并声明所用的 PKCE 能力，再把可选权限交给调用方。
def discover(resource):
    meta=request_json(well_known(resource,'oauth-protected-resource'))
    if meta.get('resource')!=resource or not isinstance(meta.get('authorization_servers'),list) or len(meta['authorization_servers'])!=1:
        raise W.Problem('plugin_oauth','资源或授权服务身份不可核对',409)
    issuer=valid_url(meta['authorization_servers'][0])
    auth=request_json(well_known(issuer,'oauth-authorization-server'))
    if auth.get('issuer')!=issuer or 'S256' not in auth.get('code_challenge_methods_supported',[]):
        raise W.Problem('plugin_oauth','授权服务身份或 PKCE S256 支持不可核对',409)
    for k in ('authorization_endpoint','token_endpoint'):valid_url(auth.get(k))
    scopes=meta.get('scopes_supported',[])
    if not isinstance(scopes,list) or len(scopes)>32 or any(not isinstance(s,str) or not re.fullmatch(r'[\x21\x23-\x5b\x5d-\x7e]{1,128}',s) for s in scopes) or len(scopes)!=len(set(scopes)):
        raise W.Problem('plugin_oauth','服务声明的权限范围无效',409)
    return dict(auth,resource_scopes=scopes)


# 用同一锁添加待授权记录并回收过期终态。设备授权同一安装修订和服务器只允许一个等待中的尝试。
def register_flow(ident,row):
    with _lock:
        for key,previous in list(_flows.items()):
            if previous.get('finishedAt',previous['expiresAt'])<time.time()-60:
                _flows.pop(key)
        if row.get('device') and any(
            previous['state']=='waiting' and not previous['cancelled'].is_set()
            and (previous['pluginId'],previous['server'],previous['revision'])==
                (row['pluginId'],row['server'],row['revision'])
            for previous in _flows.values()
        ):
            raise W.Problem('github_device_pending','已有 GitHub 授权在等待，请先完成或取消',409)
        if len(_flows)>=16:
            finished=next((key for key,previous in _flows.items() if previous['state']!='waiting'),None)
            if finished is None:raise W.Problem('plugin_oauth','登录会话过多，请稍后再试',429)
            _flows.pop(finished)
        _flows[ident]=row


# 将 GitHub 设备授权的服务错误转换成界面可处理的稳定业务码。
def github_device_error(error):
    if error=='device_flow_disabled':
        return W.Problem('github_device_disabled','请在 GitHub App 的设置中启用 Device Flow',409)
    if error=='incorrect_client_credentials':
        return W.Problem('github_app_client','GitHub 拒绝此 Client ID，请核对应用配置',409)
    return W.Problem('github_device_failed','GitHub 设备授权未完成，请重新登录',502)


# 账户可在启用 MCP 或选择仓库前登录。
# 宿主保留设备轮询，页面只接收原尝试、浏览器地址、用户码和到期时间。
def github_device_start(pid,server,value,endpoint):
    client_id=github_client_id(value,endpoint)
    if not client_id:
        raise W.Problem('github_app_required','请先配置 GitHub App 的 Client ID',409)
    expected=value['revision']
    ident=secrets.token_hex(16)
    cancelled=threading.Event()
    row=dict(pluginId=pid,server=server,revision=expected,state='waiting',
             expiresAt=time.time()+900,cancelled=cancelled,device=True)
    register_flow(ident,row)

    # 轮询和保存前重查原安装修订。更换 App 设置后的旧尝试不可写入当前连接。
    def validate_installation():
        current,_=target(pid,server)
        if current['revision']!=expected:
            raise W.Problem('plugin_changed','插件安装或设置已变化，请重新登录',409)

    try:
        result=request_json(GITHUB_DEVICE_CODE,{'client_id':client_id},True,oauth_errors=True)
        if 'error' in result:raise github_device_error(result['error'])
        user_code=result.get('user_code')
        lifetime=result.get('expires_in')
        interval=result.get('interval',5)
        if (result.get('verification_uri')!=GITHUB_DEVICE_VERIFY
                or not isinstance(user_code,str) or not re.fullmatch(r'[A-Z0-9]{4}-[A-Z0-9]{4}',user_code)
                or type(lifetime) is not int or lifetime<=0
                or type(interval) is not int or interval<1):
            raise W.Problem('github_device_response','GitHub 返回的设备授权资料无法核对',502)
        device_code=bearer(result.get('device_code'))
        lifetime=min(lifetime,900)
        deadline=time.monotonic()+lifetime
        with _lock:
            validate_installation()
            if cancelled.is_set():raise W.Problem('plugin_changed','此登录已取消，请重新连接',409)
            row['expiresAt']=time.time()+lifetime
    except Exception:
        with _lock:_flows.pop(ident,None)
        raise

    # 按 GitHub 返回的等待间隔轮询，限流时延后；只有仍有效且未取消的尝试才通过 `attach` 保存连接。
    def poll():
        delay=interval
        state='expired'
        message=None
        try:
            fields=dict(client_id=client_id,device_code=device_code,grant_type=GITHUB_DEVICE_GRANT)
            while time.monotonic()<deadline:
                if cancelled.wait(min(delay,max(0,deadline-time.monotonic()))):break
                if time.monotonic()>=deadline:break
                validate_installation()
                token=request_json(GITHUB_TOKEN,fields,True,oauth_errors=True)
                error=token.get('error')
                if error=='authorization_pending':continue
                if error=='slow_down':
                    suggested=token.get('interval')
                    delay=max(delay+5,suggested) if type(suggested) is int else delay+5
                    continue
                if error in ('expired_token','token_expired'):break
                if error=='access_denied':
                    state='denied'
                    message='GitHub 未同意此设备授权，可以重新登录。'
                    break
                if 'error' in token:raise github_device_error(error)
                if str(token.get('token_type','')).lower()!='bearer' or token.get('scope','')!='':
                    raise W.Problem('github_device_response','无法核对 GitHub 返回的令牌类型或权限',502)
                seconds=token.get('expires_in')
                if 'expires_in' in token and (type(seconds) is not int or not 0<seconds<31536000):
                    raise W.Problem('github_device_response','无法核对 GitHub 返回的令牌有效期',502)
                expires=time.time()+seconds if seconds is not None else None
                with _lock:
                    if cancelled.is_set() or time.monotonic()>=deadline:break
                    attach(pid,server,expected,dict(accessToken=bearer(token.get('access_token')),
                                                   method='github-device',expiresAt=expires))
                    state=row['state']='connected'
                break
        except W.Problem as error:
            state,message='failed',error.message
        except Exception:
            state,message='failed','未能保存 GitHub 连接，请重新登录。'
        finally:
            with _lock:
                row['state']='cancelled' if cancelled.is_set() else state
                row['finishedAt']=time.time()
                if message and row['state']!='cancelled':row['message']=message

    threading.Thread(target=poll,daemon=True).start()
    return dict(id=ident,url=GITHUB_DEVICE_VERIFY,userCode=user_code,expiresAt=row['expiresAt'])


# 入口按官方地址区分 GitHub 设备授权与通用 MCP OAuth。
# 通用流程先发现授权能力，再为当前尝试创建本机一次性回调。
def oauth_start(pid,server,body):
    value,endpoint=target(pid,server)
    expected=body.get('expectedRevision')
    if type(expected) is not int or expected!=value['revision']:raise W.Problem('plugin_changed','插件版本已变化，请刷新',409)
    if endpoint['url']==GITHUB_RESOURCE:
        return github_device_start(pid,server,value,endpoint)
    auth=discover(endpoint['url'])
    state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48)
    scopes=body.get('scopes',[])
    if not isinstance(scopes,list) or len(scopes)>32 or any(not isinstance(s,str) or s not in auth['resource_scopes'] for s in scopes) or len(scopes)!=len(set(scopes)):
        raise W.Problem('plugin_oauth','请选择服务声明的权限范围',409)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    ident=secrets.token_hex(16);row=dict(pluginId=pid,server=server,revision=expected,state='waiting',expiresAt=time.time()+300)
    completed=threading.Event()
    cancelled=threading.Event()
    row['cancelled']=cancelled
    class Callback(BaseHTTPRequestHandler):
        # 避免把 OAuth 回调请求的查询参数写进默认 HTTP 日志。
        def log_message(self,*args):pass
        # 给本机回调连接设置读写时限，避免未完成的浏览器请求占住处理线程。
        def setup(self):
            super().setup()
            self.connection.settimeout(3)
        # 只处理当前仍有效的授权回调。
        # 核对原尝试和服务声明后交换令牌，落盘仍由 `attach` 校验安装修订。
        def do_GET(self):
            self.connection.settimeout(3)
            p=urlsplit(self.path);query=parse_qs(p.query)
            if self.headers.get('Host')!=f'127.0.0.1:{self.server.server_port}' or p.path!='/callback' or query.get('state')!=[state] or completed.is_set() or cancelled.is_set() or time.time()>=row['expiresAt']:
                self.send_error(400);return
            completed.set()
            try:
                codes=query.get('code',[])
                if len(codes)!=1 or len(codes[0])>8192 or 'error' in query or ('iss' in query and query['iss']!=[auth['issuer']]):raise ValueError('denied')
                fields=dict(grant_type='authorization_code',code=codes[0],client_id=client_id,
                            code_verifier=verifier,redirect_uri=redirect)
                fields['resource']=endpoint['url']
                result=request_json(auth['token_endpoint'],fields,True)
                if str(result.get('token_type','')).lower()!='bearer':raise ValueError('token type')
                if 'scope' in result and (not isinstance(result['scope'],str) or set(result['scope'].split())!=set(scopes)):
                    raise ValueError('unexpected token scope')
                lifetime=result.get('expires_in');expires=time.time()+lifetime if type(lifetime) is int and 0<lifetime<31536000 else None
                with _lock:
                    if cancelled.is_set():raise ValueError('cancelled')
                    attach(pid,server,expected,dict(accessToken=bearer(result.get('access_token')),method='oauth',expiresAt=expires))
                    row['outcome']='connected'
                message=b'Connected. You can close this tab.'
            except Exception:
                with _lock:
                    row['outcome']='cancelled' if cancelled.is_set() else 'failed'
                message=b'Connection was not saved. Return to Modot to retry.'
            self.send_response(200);self.send_header('Content-Type','text/plain; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer');self.end_headers();self.wfile.write(message)
    host=HTTPServer(('127.0.0.1',0),Callback)
    host.timeout=.5
    redirect=f'http://127.0.0.1:{host.server_port}/callback'
    try:
        client_id=body.get('clientId')
        if not client_id:
            registration=auth.get('registration_endpoint')
            if not registration:raise W.Problem('plugin_oauth_client','服务未开放客户端自动注册，需要该服务提供的客户端 ID',409)
            client_id=request_json(registration,dict(client_name='Harness',redirect_uris=[redirect],token_endpoint_auth_method='none',grant_types=['authorization_code'],response_types=['code'])).get('client_id')
        if not isinstance(client_id,str) or not 1<=len(client_id)<=512:raise W.Problem('plugin_oauth_client','OAuth 客户端身份无效',409)
        params=dict(response_type='code',client_id=client_id,redirect_uri=redirect,code_challenge=challenge,code_challenge_method='S256',state=state)
        params['resource']=endpoint['url']
        if scopes:params['scope']=' '.join(scopes)
        url=auth['authorization_endpoint']+('&' if '?' in auth['authorization_endpoint'] else '?')+urlencode(params)
        register_flow(ident,row)
    except Exception:
        host.server_close();raise
    # 在回调、取消或到期时关闭本机监听，将回调结果归并为原尝试终态。
    def serve():
        try:
            while not completed.is_set() and not cancelled.is_set() and time.time()<row['expiresAt']:host.handle_request()
        finally:
            host.server_close()
            with _lock:
                row['state']=row.pop('outcome','cancelled' if cancelled.is_set() else 'expired')
                row['finishedAt']=time.time()
    threading.Thread(target=serve,daemon=True).start()
    return dict(id=ident,url=url,expiresAt=row['expiresAt'])


# 由 `plugin_runtime.py` 转发连接管理。
# 状态和取消以原尝试归属核对，断开连接也会取消旧修订下的未完成授权。
def route(pid,server,operation,body):
    if operation=='scopes':
        value,endpoint=target(pid,server)
        if body.get('expectedRevision')!=value['revision']:raise W.Problem('plugin_changed','插件设置已变化，请刷新',409)
        return dict(scopes=discover(endpoint['url'])['resource_scopes'])
    if operation=='oauth':return oauth_start(pid,server,body)
    if operation=='github-app':return github_app_config(pid,server,body)
    if operation in {'status','cancel'}:
        ident=body.get('id')
        with _lock:
            row=_flows.get(ident) if isinstance(ident,str) else None
            if not row or (row['pluginId'],row['server'])!=(pid,server):raise W.Problem('not_found','登录会话不存在',404)
            if operation=='cancel' and row['state']=='waiting' and row.get('outcome')!='connected':
                row['cancelled'].set()
                if row.get('device'):row['state']='cancelled'
            return {key:row[key] for key in ('state','expiresAt','message') if key in row}
    if operation=='token':return attach(pid,server,body.get('expectedRevision'),dict(method='token',accessToken=bearer(body.get('token'))))
    if operation=='disconnect':
        expected=body.get('expectedRevision')
        result=attach(pid,server,expected,None)
        with _lock:
            for row in _flows.values():
                if ((row['pluginId'],row['server'])==(pid,server) and row['revision']<=expected
                        and row['state']=='waiting' and row.get('outcome')!='connected'):
                    row['cancelled'].set()
                    if row.get('device'):row['state']='cancelled'
        return result
    raise W.Problem('not_found','没有此连接操作',404)
