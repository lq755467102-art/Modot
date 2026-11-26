// 提供插件包选择、启用、工具浏览和连接管理面板。
import {ExtensionCard, ExtensionDetails} from "./ExtensionCard";
import {useEffect,useState} from 'react';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {api} from './client';
import type {PluginPackage,PluginPage,PluginToolsPage} from './generated';
import {MemoryProviderControl,memoryProviderKey} from './MemoryProviderControl';
const endpoint='/api/v2/plugins';
export function PluginPane({onUseSkill,memoryFocus=0,visible=true}:{onUseSkill?:(value:{title:string;text:string})=>void;memoryFocus?:number;visible?:boolean}){
  const client=useQueryClient();
  const [after,setAfter]=useState(0),[path,setPath]=useState(''),[preview,setPreview]=useState<PluginPackage>();
  const [selectedId,setSelectedId]=useState('');
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[note,setNote]=useState('');
  const [tools,setTools]=useState<PluginToolsPage>();
  const [login,setLogin]=useState<{path:string;id:string}>();
  const [scopeChoice,setScopeChoice]=useState<{target:string;scopes:string[];selected:string[]}>();
  const [tokenTarget,setTokenTarget]=useState(''),[token,setToken]=useState('');
  const list=useQuery<PluginPage>({queryKey:['plugins',after],queryFn:()=>api(endpoint+'?after='+after)});
  const selected=list.data?.plugins.find(plugin=>plugin.id===selectedId);
  useEffect(()=>{if(memoryFocus){closeDetails();document.querySelector<HTMLElement>('[data-memory-extensions]')?.scrollIntoView({block:'start'});}},[memoryFocus]);
  function closeDetails() {
    setSelectedId('');
    setTools(undefined);
    setScopeChoice(undefined);
    setToken('');
    setTokenTarget('');
  }
  async function perform(action:()=>Promise<void>){if(busy)return;setBusy(true);setError('');setNote('');
    try{await action();await client.invalidateQueries({queryKey:memoryProviderKey});await client.invalidateQueries({queryKey:['memory-trees']});}catch(e){setError((e as Error).message);await list.refetch();}finally{setBusy(false);}}
  async function choose(){await perform(async()=>{const selected=await window.workbench.chooseProject();if(!selected)return;
    setPath(selected);setPreview(await api<PluginPackage>(endpoint+'/preview',{path:selected}));});}
  async function change(p:PluginPackage,body:Record<string,unknown>){await perform(async()=>{
    const updated=await api<PluginPackage>(endpoint+'/'+p.id,{expectedRevision:p.revision,...body});
    if(updated.removed)closeDetails();
    await list.refetch();setTools(undefined);setNote(updated.removed?'已卸载启用关系，保留版本供历史审查；来源包没有删除。':'设置已保存。新启用只影响下一轮，停用会阻止后续读取和调用。');});}
  async function inspect(p:PluginPackage,server:string,offset=0,catalogHash?:string){await perform(async()=>{
    setTools(await api<PluginToolsPage>(endpoint+'/'+p.id+'/tools',{server,after:offset,...(catalogHash?{catalog_hash:catalogHash}:{})}));});}
  function connection(p:PluginPackage,server:string){return endpoint+'/'+p.id+'/connections/'+encodeURIComponent(server);}
  useEffect(()=>{
    if(!login)return;
    let active=true;
    const timer=window.setInterval(async()=>{
      try{
        const value=await api<{state:string}>(login.path+'/status',{id:login.id},true);
        if(active&&value.state!=='waiting'){
          setLogin(undefined);void list.refetch();
          if(value.state==='connected')setNote('已连接。新的凭据从下一轮起可用。');
          else setError('登录未完成，请重新连接。');
        }
      }catch(e){if(active){setLogin(undefined);setError((e as Error).message);}}
    },1500);
    return ()=>{active=false;window.clearInterval(timer);};
  },[login]);
  return <section aria-label="插件管理" className="plugin-management">
    <MemoryProviderControl visible={visible}/>
    <button disabled={busy} onClick={choose}>选择插件包目录</button>
    {preview&&<ExtensionDetails title="插件安装预览" onClose={()=>setPreview(undefined)} busy={busy}><h3>{preview.name} · {preview.version}</h3>
      <p>{preview.description}</p><p>{preview.format} · {preview.fileCount} 个文件 · {preview.bytes} 字节</p>
      <p>版本指纹 <code>{preview.fingerprint}</code></p>
      <p>技能 {preview.skills.length} 项，MCP {preview.servers.length} 项{preview.contextStores?.length?`，会话状态 ${preview.contextStores.length} 项`:''}</p>
      {preview.servers.map(s=><p key={s.name}>{s.name} · {s.displayURL||s.transport} · {s.supported?(s.transport==='stdio'?'可在隔离副本运行':'可连接') :s.reason}</p>)}
      {preview.unsupported.map((reason,i)=><p key={i}>未适配：{reason}</p>)}
      {preview.omitted.length>0&&<p>未复制分发无关目录：{preview.omitted.join('、')}</p>}
      <button disabled={busy} onClick={()=>perform(async()=>{await api(endpoint+'/install',{path,fingerprint:preview.fingerprint});setPreview(undefined);await list.refetch();setNote('已安装，所有组件仍停用；请选择需要的组件。');})}>安装（默认停用）</button>
      {error&&<p role="alert">{error}</p>}
      <button disabled={busy} onClick={()=>setPreview(undefined)}>取消安装预览</button>
    </ExtensionDetails>}
    {list.error&&<p role="alert">{list.error.message}</p>}
    {list.data?.plugins.length===0&&<p>还没有安装插件。</p>}
    <div className="extension-grid">{list.data?.plugins.map(plugin=>{
      const id=plugin.id;
      if(!id)return null;
      const enabled=plugin.skillsEnabled||plugin.remoteEnabled||plugin.hooksEnabled||plugin.contextEnabled||plugin.memoryEnabled||plugin.workersEnabled;
      const remote=plugin.servers.filter(server=>server.supported&&server.transport!=="stdio");
      const pending=plugin.remoteEnabled&&remote.some(server=>!server.connected);
      const status=enabled?(pending?"已启用 · 待连接":"已启用"):"已停用";
      return <ExtensionCard key={plugin.id} pluginId={plugin.id} name={plugin.name}
        description={plugin.description} status={status} onOpen={()=>{setError('');setNote('');setSelectedId(id);}}/>;
    })}</div>
    {selected&&renderDetails(selected)}
    {after>0&&<button disabled={busy} onClick={()=>setAfter(Math.max(0,after-20))}>上一页插件</button>}
    {list.data?.hasMore&&<button disabled={busy} onClick={()=>setAfter(list.data!.next)}>后续插件</button>}
    {!selected&&<>{busy&&!preview&&<p role="status">正在核对插件组件…</p>}{error&&!preview&&<p role="alert">{error}</p>}{note&&<p role="status">{note}</p>}</>}
  </section>;

  function renderDetails(p:PluginPackage) {
    return <ExtensionDetails key={p.id} title={p.name+" · "+p.version}
      onClose={closeDetails} busy={busy}>
      <p>启用从下一轮生效。MCP 写操作逐次批准。</p>
      <p>{p.description}</p><p>{p.compatibility==='partial'?'部分组件可用':'已支持所列公共组件'} · {p.format}</p>
      {p.memoryProvider&&<p>提供记忆实现 · {p.memoryEnabled?'当前已选用':'尚未选用'}。在本页“记忆实现”中统一选择；卸载后不会回退到其他实现。</p>}
      {!!p.skills.length&&<label><input type="checkbox" aria-label={`启用技能 ${p.name}`} disabled={busy} checked={!!p.skillsEnabled}
        onChange={e=>change(p,{skillsEnabled:e.target.checked,confirm:e.target.checked})}/>启用技能（下一轮）</label>}
      {!!p.servers.length&&<label><input type="checkbox" aria-label={`启用只读 MCP ${p.name}`} disabled={busy||!p.servers.some(s=>s.supported)} checked={!!p.remoteEnabled}
        onChange={e=>change(p,{remoteEnabled:e.target.checked,confirm:e.target.checked})}/>启用 MCP（下一轮）</label>}
      {p.remoteEnabled&&<label><input type="checkbox" disabled={busy} checked={!!p.writeEnabled}
        onChange={e=>change(p,{writeEnabled:e.target.checked,confirm:e.target.checked})}/>允许申请写操作</label>}
      {!!p.hooks?.length&&<label><input type="checkbox" disabled={busy} checked={!!p.hooksEnabled}
        onChange={e=>change(p,{hooksEnabled:e.target.checked,confirm:e.target.checked})}/>启用隔离观察 Hook（下一轮）</label>}
      {!!p.contextStores?.length&&<label><input type="checkbox" aria-label={`启用上下文状态 ${p.name}`} disabled={busy} checked={!!p.contextEnabled}
        onChange={e=>change(p,{contextEnabled:e.target.checked,confirm:e.target.checked})}/>启用会话状态（仅插件数据，下一轮）</label>}
      {!!p.workers?.length&&<label><input type="checkbox" aria-label={`${p.workers.some(worker=>worker.modelRole) ? '启用任务核心贡献' : '启用子 Agent 贡献'} ${p.name}`} disabled={busy} checked={!!p.workersEnabled}
        onChange={e=>change(p,{workersEnabled:e.target.checked,confirm:e.target.checked})}/>{p.workers.some(worker=>worker.modelRole) ? '启用任务核心贡献' : '启用子 Agent 贡献'}（下一轮生效）</label>}
      {p.servers.map(s=><div key={s.name}><p>{s.name} · {s.displayURL||s.transport}{s.reason?' · '+s.reason:''}</p>
        {s.supported&&<button disabled={busy||!p.remoteEnabled} onClick={()=>inspect(p,s.name)}>查看 {s.name} 工具</button>}
        {s.supported&&s.transport!=='stdio'&&<>
          {s.displayURL==='https://api.githubcopilot.com/mcp/'&&<p>此宿主尚未配置 GitHub OAuth 应用，请使用下方访问令牌连接。</p>}
          {s.displayURL!=='https://api.githubcopilot.com/mcp/'&&<button disabled={busy||!!login} onClick={()=>perform(async()=>{const target=connection(p,s.name);const value=await api<{scopes:string[]}>(target+'/scopes',{expectedRevision:p.revision},true);setScopeChoice({target,scopes:value.scopes,selected:[]});setNote(value.scopes.length?'请选择此连接需要的权限范围，再继续登录。':'此服务未声明权限范围，可以继续登录。');})}>查看登录范围</button>}
          {s.displayURL!=='https://api.githubcopilot.com/mcp/'&&scopeChoice?.target===connection(p,s.name)&&<div aria-label={s.name+' 登录范围'}>
            {scopeChoice.scopes.map(scope=><label key={scope}><input type="checkbox" checked={scopeChoice.selected.includes(scope)} onChange={e=>setScopeChoice(old=>old?{...old,selected:e.target.checked?[...old.selected,scope]:old.selected.filter(x=>x!==scope)}:old)}/>{scope}</label>)}
            <button disabled={busy||!!login||(scopeChoice.scopes.length>0&&scopeChoice.selected.length===0)} onClick={()=>perform(async()=>{const path=connection(p,s.name);const value=await api<{id:string;url:string}>(path+'/oauth',{expectedRevision:p.revision,scopes:scopeChoice.selected},true);await window.workbench.openOAuth(value.url);setLogin({path,id:value.id});setScopeChoice(undefined);setNote('请在浏览器完成登录。');})}>{s.connected?'重新登录':'登录连接'}</button>
          </div>}
          {s.connected&&<button disabled={busy} onClick={()=>perform(async()=>{await api(connection(p,s.name)+'/disconnect',{expectedRevision:p.revision});await list.refetch();setTools(undefined);setNote('连接已断开。');})}>断开</button>}
          <details onToggle={e=>{if(!e.currentTarget.open&&tokenTarget===connection(p,s.name)){setToken('');setTokenTarget('');}}}><summary>用访问令牌连接</summary>
            <input aria-label={`${s.name} 访问令牌`} type="password" autoComplete="off" value={tokenTarget===connection(p,s.name)?token:''} onChange={e=>{setTokenTarget(connection(p,s.name));setToken(e.target.value);}}/>
            <button disabled={busy||tokenTarget!==connection(p,s.name)||!token} onClick={()=>perform(async()=>{const value=token;setToken('');setTokenTarget('');await api(connection(p,s.name)+'/token',{expectedRevision:p.revision,token:value},true);await list.refetch();setTools(undefined);setNote('连接已保存。');})}>保存令牌</button>
          </details></>}
      </div>)}
      <details><summary>技能与兼容详情</summary>{p.skills.map(s=><p key={s.name}>{s.name}：{s.description}
        {s.modelInvocable===false&&<small> · 手动调用</small>}
        {onUseSkill&&p.skillsEnabled&&s.userInvocable!==false&&!s.unsupported?.length&&<button disabled={busy} onClick={()=>perform(async()=>{
          const draft=await api<{title:string;text:string}>(endpoint+'/'+p.id+'/skill-draft',{name:s.name,expectedRevision:p.revision},true);
          closeDetails();
          onUseSkill(draft);
        })}>加入对话草稿</button>}</p>)}
        {p.hooks?.map((h,i)=><p key={i}>{h.event} · {h.entry} · 隔离观察</p>)}
        {p.unsupported.map((reason,i)=><p key={i}>未适配：{reason}</p>)}<small>{p.notice}</small>
      </details>
      <button disabled={busy} onClick={()=>change(p,{remove:true})}>卸载 {p.name}</button>
    {tools&&<section aria-label="插件真实工具目录"><h3>{tools.server} · 实际 MCP 目录</h3><p>{tools.notice}</p>
      {tools.tools.map(t=><details key={String(t.spec.name)}><summary>{String(t.spec.name)} · {t.callable?(t.requiresHuman?'需逐次人工批准':'声明只读'):'未开放'}</summary>
        <p>{t.notice}</p><pre>{JSON.stringify(t.spec,null,2)}</pre><small>schema {t.schemaHash}</small></details>)}
      {tools.hasMore&&<button disabled={busy} onClick={()=>{const owner=list.data?.plugins.find(p=>p.id===tools.pluginId);if(owner)void inspect(owner,tools.server,tools.next,tools.catalogHash);}}>继续读取工具目录</button>}
    </section>}
    {busy&&<p role="status">正在核对插件组件…</p>}
    {error&&<p role="alert">{error}</p>}{note&&<p role="status">{note}</p>}
    </ExtensionDetails>;
  }
}
