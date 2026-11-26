"""Inspect common plugin packages without starting any bundled code.

An installed package is an immutable, bounded copy. Compatibility describes
individual components; metadata parsing never claims host integration works.
"""
# 主要引用方包括 `plugins.py` 和 `plugin_stdio.py`。
import json
import os
from pathlib import Path,PurePosixPath
import re
import urllib.parse
from . import workbench as W

MAX_FILES=800
MAX_BYTES=40*1024*1024
SKIP={'.git','.hg','.svn','__pycache__','node_modules'}


class ManifestError(W.Problem):
    # 附带插件清单的文件和诊断，供安装入口指向实际来源。
    def __init__(self, code, field, message):
        super().__init__(code, message, 409)
        self.field = field

    def __str__(self):
        return self.message


# 按清单格式读取 JSON，重复字段会造成解释不唯一，因此由解析器拒绝。
def parse_json(raw, field):
    # 在原对象的键序列上识别重复键，避免解码后旧值已丢失。
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ManifestError("duplicate_key", field, f"{field} 中的 JSON 键 {key!r} 重复。")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)


# 将清单声明的相对路径解析到插件根目录以内。
def inside(root,relative):
    if not isinstance(relative,str) or not relative.startswith('./') or '\\' in relative or ':' in relative or any(p in {'..',''} for p in relative[2:].split('/') if p!=''):
        raise W.Problem('plugin_path','插件路径需以 ./ 开头且位于包内')
    parts=PurePosixPath(relative[2:]).parts
    if not parts or '..' in parts or any(p in SKIP for p in parts):raise W.Problem('plugin_path','插件资源路径无效')
    path=root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()):raise W.Problem('plugin_path','插件资源超出包范围',403)
    return path


# 在读取清单前确认文件大小和路径，错误保留对应清单来源。
def read_json(path):
    if path.stat().st_size>512*1024:raise W.Problem('plugin_capacity','插件清单超过 512KiB',413)
    with path.open('rb') as handle:raw=handle.read(512*1024+1)
    if len(raw)>512*1024:raise W.Problem('plugin_capacity','插件清单读取期间变大',413)
    value=parse_json(raw, path.name)
    if not isinstance(value,dict):raise W.Problem('plugin_manifest','插件清单须为 JSON 对象')
    return value


# 收集受控插件目录的文件和摘要，供后续安装与运行校验。
def files(root):
    result={};omitted=[];total=0
    paths=[]
    for base,dirs,names in os.walk(root,followlinks=False):
        for name in list(dirs):
            path=Path(base)/name
            if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):raise W.Problem('plugin_link','插件包含目录链接或联接',403)
            if name in SKIP:dirs.remove(name);omitted.append(path.relative_to(root).as_posix())
        for name in names:
            paths.append(Path(base)/name)
            if len(paths)>MAX_FILES:raise W.Problem('plugin_capacity','插件文件数超过 800',413)
    for path in sorted(paths):
        relative=path.relative_to(root).as_posix()
        if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):raise W.Problem('plugin_link','插件包含链接或目录联接，未跟随读取',403)
        if path.is_dir():continue
        if path.name.startswith('.env') or path.name.lower() in {'api-token','credentials.env','model-profiles.json','models.json'}:
            raise W.Problem('plugin_credentials','插件包内含凭据或模型配置文件，请选择干净的分发包',403)
        if not path.is_file():raise W.Problem('plugin_file','插件包含不可保留的特殊文件')
        size=path.stat().st_size;total+=size
        if total>MAX_BYTES or len(result)>=MAX_FILES:raise W.Problem('plugin_capacity','插件超过 40MiB / 800 个文件边界',413)
        with path.open('rb') as handle:raw=handle.read(size+1)
        if len(raw)!=size or not path.resolve().is_relative_to(root):raise W.Problem('plugin_changed','读取期间插件文件变化',409)
        result[relative]=dict(hash=W.digest(raw),bytes=len(raw))
    return result,total,omitted


# 识别插件清单的宿主格式、工具和 Skill 贡献。安装身份由实际文件内容与清单一起确定。
def inspect(raw):
    if not isinstance(raw,str) or not Path(raw).is_absolute():raise W.Problem('plugin_path','请选择插件目录的绝对路径')
    root=Path(raw)
    if not root.is_dir() or root.is_symlink() or (hasattr(root,'is_junction') and root.is_junction()):raise W.Problem('plugin_path','请选择实际插件目录')
    root=root.resolve()
    portable=root/'plugin.json';compat=root/'.codex-plugin/plugin.json';cursor=root/'.cursor-plugin/plugin.json';claude=root/'.claude-plugin/plugin.json'
    dsh=root/'package.json'
    native_dsh=not any(p.is_file() for p in (portable,compat,cursor,claude)) and dsh.is_file()
    if not native_dsh and not any(p.is_file() for p in (portable,compat,cursor,claude)):raise W.Problem('plugin_manifest','未找到插件清单，请选择插件包根目录')
    inventory,total,omitted=files(root)
    if native_dsh:
        manifest=read_json(dsh)
        if not isinstance(manifest.get('dsh'),dict):raise W.Problem('plugin_manifest','普通 npm 包不是可识别的 DSH 插件')
        format='dsh-native';settings={};skills=root/'skills';mcp=None
    elif portable.is_file():
        manifest=read_json(portable);format='agent-plugins'
        schema=manifest.get('$schema')
        if schema and schema!='https://agent-plugins.org/schemas/1.0.0/plugin.schema.json':raise W.Problem('plugin_schema','尚未适配此插件 schema 版本')
        extension=manifest.get('extensions',{}).get('com.openai') if isinstance(manifest.get('extensions',{}),dict) else None
        settings=extension if isinstance(extension,dict) else read_json(compat) if compat.is_file() else {}
        skills=root/'skills';mcp=root/'mcp.json'
    else:
        entry=compat if compat.is_file() else claude if claude.is_file() else cursor
        if not entry.is_file():raise W.Problem('plugin_manifest','未找到 plugin.json 或 .codex-plugin/plugin.json')
        manifest=read_json(entry);settings=manifest;format='codex' if entry==compat else 'claude' if entry==claude else 'cursor-common-components'
        paths=manifest.get('skills','./skills/')
        paths=[paths] if isinstance(paths,str) else paths
        if not isinstance(paths,list) or not paths or len(paths)>32 or any(not isinstance(p,str) for p in paths):raise W.Problem('plugin_manifest','skills 须为包内路径或路径数组')
        if entry==claude and './skills/' not in paths:paths=['./skills/',*paths]
        skills=[inside(root,p) for p in paths]
        declared=manifest.get('mcpServers')
        if declared is not None and not isinstance(declared,str):raise W.Problem('plugin_manifest','mcpServers 须指向包内 JSON 配置')
        mcp=inside(root,declared) if declared else root/'.mcp.json' if entry==claude else None
    name=manifest.get('name');version=manifest.get('version','unversioned')
    if not isinstance(name,str) or not re.fullmatch((r'(?:@[A-Za-z0-9._-]{1,64}/)?' if native_dsh else '')+r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',name):raise W.Problem('plugin_manifest','插件 name 无效')
    if not isinstance(version,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._+-]{0,63}',version):raise W.Problem('plugin_manifest','插件版本无效')
    unsupported=[];skill_rows=[];servers=[]
    if native_dsh:unsupported.append('原生 DSH 组件需要独立运行时；当前只预览清单与可移植技能')
    skill_roots=skills if isinstance(skills,list) else [skills]
    sources=sorted({source for directory in skill_roots if directory.is_dir() for source in directory.glob('*/SKILL.md')})
    if sources:
        from .skills import _norm_meta
        for source in sources:
            if source.stat().st_size>256*1024:raise W.Problem('plugin_capacity','单个技能正文超过 256KiB',413)
            with source.open('rb') as handle:raw=handle.read(256*1024+1)
            if len(raw)>256*1024 or W.digest(raw)!=inventory[source.relative_to(root).as_posix()]['hash']:raise W.Problem('plugin_changed','技能读取期间变化，未发布安装',409)
            try:
                meta,_body=_norm_meta(raw.decode('utf8'))
            except ModuleNotFoundError as error:
                if error.name != 'yaml':
                    raise
                raise W.Problem(
                    'plugin_yaml_unavailable',
                    '当前 Python 缺少 PyYAML，无法读取插件技能。请为 Harness 使用的解释器安装 requirements-workbench.txt 中的依赖。',
                    503,
                ) from error
            except ValueError as error:
                raise W.Problem('plugin_skill', '插件技能元数据无效：'+str(error)) from error
            skill_name=meta.get('name') or source.parent.name
            if not isinstance(skill_name,str) or not 1<=len(skill_name)<=100 or any(ord(c)<32 or ord(c)==127 for c in skill_name):raise W.Problem('plugin_skill','技能名称无效')
            unavailable=[k for k in ('context','agent','hooks') if k in meta]
            if any(s['name']==skill_name for s in skill_rows):raise W.Problem('plugin_skill','插件包含重复技能名')
            skill_rows.append(dict(name=skill_name,description=str(meta.get('description',''))[:1000],path=source.relative_to(root).as_posix(),hash=inventory[source.relative_to(root).as_posix()]['hash'],modelInvocable=not meta.get('disable-model-invocation',False),userInvocable=meta.get('user-invocable',True),unsupported=unavailable))
            unsupported.extend(skill_name+'：'+k+' 需要宿主适配' for k in unavailable)
            if len(skill_rows)>128:raise W.Problem('plugin_capacity','单个插件技能超过 128 项',413)
    if mcp is not None and mcp.is_file():
        config=read_json(mcp).get('mcpServers',{})
        if not isinstance(config,dict) or len(config)>50:raise W.Problem('plugin_manifest','MCP 服务清单无效或超过 50 项')
        for server,cfg in config.items():
            if not isinstance(server,str) or not re.fullmatch('[A-Za-z0-9_.-]{1,100}',server) or not isinstance(cfg,dict):raise W.Problem('plugin_manifest','MCP 服务身份或配置无效')
            reason='';url=cfg.get('url');kind=cfg.get('type') or ('http' if url else 'stdio');process=None
            if cfg.get('disabled'):reason='此服务在包配置中停用，未连接'
            elif kind=='stdio':
                from .plugin_stdio import descriptor
                try:process=descriptor(root,cfg)
                except W.Problem as error:reason=error.message
            elif kind not in {'http','streamable-http'} or not isinstance(url,str) or len(url)>2048 or any(ord(c)<=32 or ord(c)==127 for c in url):reason='此传输、本机进程或超长/无效地址尚未适配'
            elif cfg.get('headers') or cfg.get('env') or cfg.get('extensions') or cfg.get('auth'):reason='此服务需要单独适配认证/环境配置，不自动继承凭据'
            elif set(cfg)-{'url','type','disabled'}:reason='服务包含未适配设置：'+', '.join(sorted(set(cfg)-{'url','type','disabled'}))[:200]
            else:
                parsed=urllib.parse.urlsplit(url)
                if parsed.scheme not in {'https','http'} or (parsed.scheme=='http' and parsed.hostname not in {'127.0.0.1','localhost','::1'}) or parsed.username or parsed.password or parsed.fragment:
                    reason='服务地址不符合匿名 HTTPS 或本机 HTTP 范围'
                elif any(re.search('key|token|secret|auth',k,re.I) for k,_v in urllib.parse.parse_qsl(parsed.query)):reason='地址包含认证参数，需单独连接配置'
            public_url=''
            if isinstance(url,str):
                parsed=urllib.parse.urlsplit(url);host=parsed.hostname or ''
                try:port=parsed.port
                except ValueError:port=None;reason='服务端口无效'
                authority=('['+host+']' if ':' in host else host)+(':'+str(port) if port else '')
                public_url=urllib.parse.urlunsplit((parsed.scheme,authority,parsed.path,'',''))
            servers.append(dict(name=server,transport=kind,url=url if not reason else None,displayURL=public_url,supported=not reason,reason=reason,process=process))
    elif mcp is not None and manifest.get('mcpServers') and not portable.is_file():unsupported.append('声明的 MCP 配置文件缺失')
    for key in ('apps',):
        if settings.get(key):unsupported.append(key+' 需要独立宿主适配')
    from .plugin_hooks import describe
    hooks,hook_reasons=([],[]) if native_dsh else describe(root,settings.get('hooks'))
    unsupported.extend(hook_reasons)
    if manifest.get('bundledContentVariant'):unsupported.append('此包声明宿主专用内容变体，未模拟其运行能力')
    for key in ('agents','commands','lspServers','requires_local_executor'):
        if manifest.get(key):unsupported.append(key+' 需要单独适配')
    for component in ('agents','commands'):
        if (root/component).is_dir() and not any(x.startswith(component+' ') for x in unsupported):unsupported.append(component+' 默认目录需要单独适配')
    if format=='cursor-common-components':unsupported.append('仅适配此包的公共技能与 MCP 组件，不提供 Cursor 专用宿主能力')
    unsupported.extend(s['name']+'：'+s['reason'] for s in servers if not s['supported'])
    fingerprint=W.digest(json.dumps(inventory,sort_keys=True,separators=(',',':')).encode())
    from .runtime_facts import plugin_declarations
    try:runtime_facts=plugin_declarations(manifest.get('runtimeFacts',[]))
    except (ValueError,TypeError) as error:raise W.Problem('plugin_manifest',str(error)) from error
    from .context_plugins import declarations
    try:context_stores=declarations(manifest.get('contextStores',[]))
    except (ValueError,TypeError) as error:raise W.Problem('plugin_manifest',str(error)) from error
    from .memory_providers import declaration
    try:memory_provider=declaration(manifest.get('memoryProvider'))
    except (ValueError,TypeError) as error:raise W.Problem('plugin_manifest',str(error)) from error
    from .worker_contract import declarations as worker_declarations
    workers=worker_declarations(manifest.get('workers',[]))
    return dict(root=str(root),name=name,version=version,description=str(manifest.get('description',''))[:2000],
        format=format,skills=skill_rows,servers=servers,hooks=hooks,unsupported=unsupported,runtimeFacts=runtime_facts,contextStores=context_stores,memoryProvider=memory_provider,workers=workers,
        compatibility='partial' if unsupported else 'components-supported',
        fingerprint=fingerprint,bytes=total,fileCount=len(inventory),files=inventory,omitted=omitted,
        notice='清单只说明组件。HTTP 与隔离本机服务分别核对；服务行为仍依赖其实现。')
