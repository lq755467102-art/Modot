// 读取并切换记忆提供方，刷新有关记忆目录和插件状态的查询。
// 主要引用方包括 `CatalogPackageManagement.tsx` 和 `ManagementPages.tsx`。
import {useState,type ReactNode} from 'react';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {api} from './client';
import type {MemoryProviderSettings} from './generated';
import './memory-provider.css';

export const memoryProviderKey=['memory-provider-settings'];
export function MemoryProviderControl({compact=false,visible=true,onManage,heading}:{compact?:boolean;visible?:boolean;onManage?:()=>void;heading?:ReactNode}){
 const query=useQuery<MemoryProviderSettings>({queryKey:memoryProviderKey,queryFn:()=>api('/api/v2/plugins/memory'),enabled:visible,refetchInterval:visible?5000:false});
 const client=useQueryClient();
 const [busy,setBusy]=useState(false),[error,setError]=useState(''),[note,setNote]=useState('');
 const active=query.data?.providers.find(p=>p.id===query.data?.providerId&&p.installed);
 async function change(body:Record<string,unknown>){if(!query.data||busy)return;setBusy(true);setError('');setNote('');try{
  const next=await api<MemoryProviderSettings>('/api/v2/plugins/memory',{expectedRevision:query.data.revision,confirm:true,...body});
  client.setQueryData(memoryProviderKey,next);
  await client.invalidateQueries({queryKey:['memory-trees']});await client.invalidateQueries({queryKey:['plugins']});
  setNote('设置已保存。原记忆保留，停用立即生效；新的实现从下一轮使用。');
 }catch(e){setError((e as Error).message);await query.refetch();}finally{setBusy(false);}}
 if(compact)return <div className="memory-provider-summary"><span>记忆实现 · {query.isPending?'读取中':query.error?'状态不可用':active?.name||'未启用'}</span><button onClick={onManage}>管理记忆扩展</button>{query.error&&<span role="alert">无法读取实现状态</span>}</div>;
 return <section className="memory-provider-control" data-memory-extensions="true" aria-label="记忆扩展管理">
  <div>{heading ?? <h2>记忆实现</h2>}<p>选择记忆的保存与维护方式。一次使用一个实现，切换不迁移或删除原数据。</p></div>
  <label>当前实现<select aria-label="当前记忆实现" disabled={busy||query.isFetching||!query.data} value={query.data?.providerId||''} onChange={e=>void change({providerId:e.target.value||null})}>
   <option value="">不启用记忆</option>{query.data?.providers.filter(p=>p.installed).map(p=><option key={p.id} value={p.id}>{p.name}</option>)}
  </select></label>
  {active&&<p>{active.description}</p>}
  {!query.data?.providerId&&query.data&&<p>普通任务照常运行。原记忆和审查记录仍保留，新的记忆不会保存到隐藏的后备位置。</p>}
  {query.data&&<button disabled={busy} onClick={()=>void change({operation:query.data!.builtinInstalled?'uninstall-builtin':'restore-builtin'})}>{query.data.builtinInstalled?'卸载默认记忆树':'恢复默认记忆树组件'}</button>}
  <small>卸载只移除实现的启用关系；恢复组件后仍由你选择是否使用。</small>
  {query.error&&<p role="alert">{query.error.message}</p>}{error&&<p role="alert">{error}</p>}{note&&<p role="status">{note}</p>}
 </section>;
}
