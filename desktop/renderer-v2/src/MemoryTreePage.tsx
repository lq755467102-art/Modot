// 显示记忆树的节点、来源和范围，并处理编辑、复核和导航。
// 主要引用方包括 `ManagementPages.tsx`。
import {useEffect,useLayoutEffect,useRef,useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {api} from './client';
import type {MemoryView,MemoryNode} from './generated';
import './memory-tree.css';

type Inventory={projects:{id:string;name:string}[];conversations:{id:string;title:string;projectId?:string}[]};
type DisplayNode=MemoryNode & {available?:boolean};
// 这是历史请求选中的记忆与提供方状态，`available` 决定原节点版本能否回查。
type Snapshot={nodes:DisplayNode[];snapshot:{loaded?:boolean;provider?:{identity?:{adapter?:string}}};notice:string};
// 列出可用来佐证记忆的代码操作，`nextBefore` 属于所选会话的检查历史。
type CheckPage={operations:{id:string;operation:string;started?:number;state:string;result?:{process?:{exitCode?:number}}}[];hasMore:boolean;nextBefore?:number};
type Scope='session'|'project'|'habit'|'unknown';
// 节点编辑草稿包含正文、权重、佐证来源和反例尝试；切换检查会话时分页也随之重置。
type Draft={body:string;weight:number;useThread:string;useOperation:string;useNote:string;attempt:string;checkBefore?:number};
// `selection.context` 将详情选择绑定到当前提供方或历史请求，避免跨来源复用旧节点。
type PageState={scope:Scope;request:string;offset:number;selectedId?:string;selection?:{context:string;node:DisplayNode}};
// 每个会话或预览范围分别保存页码、滚动、草稿和折叠状态。
type SavedView={page:PageState;scroll:Record<string,number>;drafts:Record<string,Draft>;expanded:Record<string,boolean>};
const scopes:{id:Scope;label:string;description:string}[]=[
 {id:'session',label:'会话',description:'仅本会话可用'},
 {id:'project',label:'项目',description:'本项目有读取权限的会话可用'},
 {id:'habit',label:'习惯',description:'跨项目习惯，仍遵守来源授权'},
 {id:'unknown',label:'作用域未记录',description:'原记录未提供作用域，不推断归属'},
];
// 节点范围由记录给出；界面把未识别的范围保留为待核对。
const scopeOf=(node:DisplayNode):Scope=>scopes.some(s=>s.id===node.scope&&s.id!=='unknown')?node.scope as Scope:'unknown';
const reasons:Record<string,string>={selected:'预计带入',state:'参考或退役',represented_by_habit:'同内容由习惯提供',branch:'分支不适用',permission:'来源未授权',not_selected:'已关闭',budget:'本次预算未选中',preference_disabled:'记忆读取已关闭',memory_permission:'当前角色未启用',user_habit:'用户习惯选择'};
const labels:Record<string,string>={conversationId:'来源会话',projectId:'来源项目',actor:'记录者',actionId:'来源操作',runId:'来源轮次',factId:'来源节点',factVersion:'来源版本',resourceIds:'来源资料',kind:'类型',note:'依据',at:'时间',created:'创建时间',resolvedAt:'解决时间',version:'版本',body:'正文',title:'标题',weight:'权重',priority:'优先级',state:'状态',scope:'作用域',id:'记录标识',sourceEvidence:'来源证据',status:'状态',counterexampleReview:'反例审查',entryType:'内容类别'};
const values:Record<string,string>={session:'仅本会话',project:'项目',habit:'跨项目习惯',model:'Agent',human:'用户',adopted:'已采纳',pending:'待采纳',active:'有效',reference:'仅供参考',retired:'退役',injected:'运行中带入（不证明采用）',reuse:'历史带入（不证明采用）',check:'实际检查记录（不证明语义适用）','human-confirmed':'用户确认可复用',counterexample:'反例','human-decision':'本次用户决定',applied:'实际使用',challenge:'反例尝试',not_reviewed:'尚未审查反例',tried_not_found:'已尝试，未找到反例；不代表绝对正确',found_unresolved:'存在未解决的反例'};
// 给首次进入的会话或预览建立独立可变的界面状态。
function emptyView():SavedView{return {page:{scope:'session',request:'',offset:0},scroll:{},drafts:{},expanded:{}};}

// 按照本轮会话或预览范围选择记忆上下文；各范围的界面状态独立缓存。
export function MemoryTreePage({conversationId,visible}:{conversationId:string;visible:boolean}){
 const [chosenId,setChosenId]=useState(conversationId),[project,setProject]=useState(''),[preview,setPreview]=useState(false),[mode,setMode]=useState('full');
 const saved=useRef(new Map<string,SavedView>());
 const inventory=useQuery<Inventory>({queryKey:['memory-inventory'],queryFn:()=>api('/api/v2/memory/inventory'),enabled:visible,refetchInterval:visible?5000:false});
 useEffect(()=>{setChosenId(conversationId);setProject('');},[conversationId]);
 const sessions=(inventory.data?.conversations||[]).filter(s=>project==='unassociated'?!s.projectId:!project||s.projectId===project);
 const owner=inventory.error?undefined:sessions.find(s=>s.id===chosenId);
 const id=owner?.id||'';
 const contextKey=preview?JSON.stringify(['preview',project,mode]):JSON.stringify(['thread',id]);
 if(!saved.current.has(contextKey))saved.current.set(contextKey,emptyView());
 return <div className="memory-tree-page">
  <div className="memory-context-bar" aria-label="记忆查看范围">
   <label>筛选项目<select aria-label="记忆项目" value={preview&&project==='unassociated'?'':project} onChange={e=>{
    const next=e.target.value;setProject(next);
    if(!preview&&(next==='unassociated'?owner?.projectId:next&&owner?.projectId!==next))setChosenId('');
   }}>
    <option value="">{preview?'未关联项目':'全部项目'}</option>{!preview&&<option value="unassociated">未关联项目</option>}
    {inventory.data?.projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}
   </select></label>
   {!preview&&<label>会话<select aria-label="记忆会话" value={id} onChange={e=>setChosenId(e.target.value)}>
    <option value="">选择会话</option>{sessions.map(s=><option key={s.id} value={s.id}>{s.title}</option>)}
   </select></label>}
   <button className="memory-preview-toggle" aria-pressed={preview} onClick={()=>setPreview(!preview)}>{preview?'返回会话记忆':'新会话预览'}</button>
  </div>
  {inventory.error&&<p className="memory-error" role="alert">会话列表：{inventory.error.message} <button onClick={()=>void inventory.refetch()}>重试</button></p>}
  {preview||id?<MemoryScopeBrowser key={contextKey} id={id} preview={preview} project={project} mode={mode} onMode={setMode} visible={visible} inventory={inventory.data} saved={saved.current.get(contextKey)!}/>:<p className="memory-empty">{inventory.isPending?'正在读取会话…':'选择一个会话，查看它可用的会话、项目与习惯记忆。'}</p>}
 </div>;
}

// 实时树、按项目预览和历史请求使用不同取数途径。提供方变化时丢弃原实时选中项。
function MemoryScopeBrowser({id,preview,project,mode,onMode,visible,inventory,saved}:{id:string;preview:boolean;project:string;mode:string;onMode:(mode:string)=>void;visible:boolean;inventory?:Inventory;saved:SavedView}){
 const [page,setPage]=useState(saved.page),[error,setError]=useState(''),[feedback,setFeedback]=useState(''),[busy,setBusy]=useState(false);
 const {request,offset}=page;
 const view=useQuery<MemoryView>({queryKey:['memory-trees',id,preview,project,mode,offset],queryFn:()=>preview?api('/api/v2/memory/preview',{projectId:project&&project!=='unassociated'?project:undefined,mode,offset}):api('/api/v2/conversations/threads/'+id+'/memory?offset='+offset),enabled:visible,refetchInterval:visible&&!page.selectedId?5000:false});
 const snapshot=useQuery<Snapshot>({queryKey:['memory-snapshot',id,request],queryFn:()=>api('/api/v2/conversations/threads/'+id+'/memory/snapshot?id='+encodeURIComponent(request)),enabled:visible&&!!id&&!!request&&!preview});
 const data=view.data,historical=!!request&&!preview;
 const nodes:DisplayNode[]=historical?(snapshot.error?[]:snapshot.data?.nodes||[]):(view.error?[]:data?.nodes||[]);
 const singleTree=(historical?snapshot.data?.snapshot.provider?.identity?.adapter:data?.provider?.identity.adapter)==='session-notes';
 const hasUnknown=nodes.some(n=>scopeOf(n)==='unknown');
 const visibleScopes=scopes.filter(s=>s.id==='unknown'?hasUnknown:!singleTree||s.id==='session');
 const scope=visibleScopes.some(s=>s.id===page.scope)?page.scope:visibleScopes[0].id;
 const currentScope=scopes.find(s=>s.id===scope)!;
 const providerToken=JSON.stringify(data?.provider?.identity||null),previousProvider=useRef<string|undefined>(undefined);
 const candidate=nodes.find(n=>n.id===page.selectedId&&scopeOf(n)===scope);
 const selectionContext=historical?request:providerToken;
 // Keep the opened version and its draft for CAS errors, but never retain a revoked body or another provider's row.
 const selected=candidate&&(candidate.reason==='permission'||candidate.available===false||candidate.readonly?candidate:page.selection?.context===selectionContext&&page.selection.node.id===candidate.id?page.selection.node:candidate);
 useEffect(()=>{
  if(!data)return;
  if(previousProvider.current&&previousProvider.current!==providerToken&&!historical){setPage(p=>({...p,selectedId:undefined,offset:0}));setError('');}
  previousProvider.current=providerToken;
 },[providerToken,historical,data]);
 useLayoutEffect(()=>{saved.page=page;},[page,saved]);
 const list=useRef<HTMLDivElement>(null),detail=useRef<HTMLElement>(null);
 const listKey=JSON.stringify([request,scope,offset]);
 const nodeKey=selected?JSON.stringify([historical?request:providerToken,selected.id,selected.version]):'';
 const ready=historical?!snapshot.isPending:!view.isPending;
 useLayoutEffect(()=>{
  const element=list.current;
  if(!element||!visible||!ready)return;
  const restore=()=>{if(element.clientHeight)element.scrollTop=saved.scroll[listKey]||0;};
  restore();const observer=new ResizeObserver(restore);observer.observe(element);
  return ()=>observer.disconnect();
 },[visible,ready,listKey,!!selected,saved]);
 useLayoutEffect(()=>{if(visible&&detail.current)detail.current.scrollTop=saved.scroll[nodeKey]||0;},[visible,nodeKey,saved]);
 const cannotEdit=busy||preview||historical||!data||!!view.error||data?.provider?.available===false||!!selected?.readonly||selected?.reason==='permission';
 const capabilities=data?.provider?.capabilities;
 // 只允许写当前可编辑范围，提交时带当前记忆版本，成功后刷新节点信息。
 async function change(payload:Record<string,unknown>){
  if(cannotEdit||!data)return;
  setBusy(true);setError('');
  try{
   const node=data.nodes.find(n=>n.id===payload.id);
   const control=['mode','habit-weight','habit-configure','compare','decide','adopt'].includes(String(payload.operation));
   const revision=control?data.revision:node?.storeRevision??data.scopeRevisions?.session??data.revision;
   const result=await api<{qualification?:{eligible:boolean;reasons:string[]};notice?:string}>('/api/v2/conversations/threads/'+id+'/memory',{expectedRevision:revision,...(node?{expectedVersion:node.version}:{}),...payload});
   setFeedback(result.qualification?(result.qualification.eligible?'已形成升格候选，等待采纳；原来源与证据保留。':'尚不能升格：'+result.qualification.reasons.join('；')):result.notice||'记忆已更新');
   await view.refetch();setPage(p=>({...p,selectedId:undefined}));
  }catch(e){setError((e as Error).message);await view.refetch();}finally{setBusy(false);}
 }
 const rows=nodes.filter(n=>scopeOf(n)===scope);
 const paged=!historical&&((data?.totalNodes||0)>nodes.length||offset>0);
 const status=(n:DisplayNode)=>n.available===false?'历史正文不可用':historical?'实际请求记录':reasons[n.reason]||n.reason||'未记录带入状态';
 function move(next:Partial<PageState>){setError('');setFeedback('');setPage(p=>({...p,...next}));}
 return <section className={'memory-scope-browser'+(selected?' has-detail':'')+(singleTree?' memory-single-tree':'')}>
  <div className="memory-scope-toolbar">
   <div role="tablist" aria-label="记忆作用域">{visibleScopes.map(s=><button key={s.id} role="tab" id={'memory-tab-'+s.id} aria-controls="memory-scope-list" aria-selected={scope===s.id} tabIndex={scope===s.id?0:-1} onClick={()=>move({scope:s.id,selectedId:undefined})} onKeyDown={e=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
    const buttons=Array.from(e.currentTarget.parentElement!.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
    const index=buttons.indexOf(e.currentTarget),next=e.key==='Home'?0:e.key==='End'?buttons.length-1:(index+(e.key==='ArrowLeft'?-1:1)+buttons.length)%buttons.length;
    e.preventDefault();buttons[next].click();buttons[next].focus();
   }}>{s.label}</button>)}</div>
   <details className="memory-view-options"><summary aria-label="记忆查看选项">查看选项</summary><div>
    <label>带入模式<select aria-label="记忆选择模式" disabled={busy||(!preview&&cannotEdit)} value={preview?mode:data?.mode||mode} onChange={e=>{if(preview)onMode(e.target.value);else void change({operation:'mode',mode:e.target.value});}}><option value="full">完整</option><option value="lean">精简</option><option value="off">关闭</option></select></label>
    {!preview&&<label>请求记录<select aria-label="记忆请求记录" value={request} onChange={e=>move({request:e.target.value,selectedId:undefined})}><option value="">当前预计选择</option>{data?.requests.map(r=><option key={r.id} value={r.id}>{new Date(r.at*1000).toLocaleString()} · {r.role}</option>)}</select></label>}
    <small>查看作用域不会改变带入模式。历史请求仅供审查。</small>
   </div></details>
  </div>
  {error&&<p className="memory-message" role="alert">{error}</p>}{feedback&&<p className="memory-message" role="status">{feedback}</p>}
  <div className="memory-scope-body">
   <div className="memory-tree-content" ref={list} onScroll={e=>{if(e.currentTarget.clientHeight)saved.scroll[listKey]=e.currentTarget.scrollTop;}} role="tabpanel" id="memory-scope-list" aria-labelledby={'memory-tab-'+scope}>
    <header className="memory-scope-heading"><div><h2>{currentScope.label}记忆</h2><p>{scope==='project'&&!historical&&data?(inventory?.projects.find(p=>p.id===data.projectId)?.name||'未关联项目')+' · ':''}{currentScope.description}</p></div>{ready&&<small>{paged?'当前页 ':''}{rows.length} 条</small>}</header>
    {[{name:'记忆',error:view.error,refetch:view.refetch},{name:'历史请求',error:historical?snapshot.error:null,refetch:snapshot.refetch}].map(s=>s.error&&<p role="alert" key={s.name}>{s.name}：{s.error.message} <button onClick={()=>void s.refetch()}>重试</button></p>)}
    {!ready&&<p role="status">正在读取记忆…</p>}
    {data?.provider?.available===false&&<p role="status">{data.notice} 仍可在查看选项中审查历史请求。</p>}
    {data?.provider?.available&&capabilities?.promote===false&&<p className="memory-note">{data.notice}</p>}
    {preview&&<p className="memory-note">新会话只读预览，不创建会话或调用模型；正式请求的选择可能因任务、模型和版本变化。</p>}
    {historical&&<p className="memory-readonly-notice" role="status">历史请求 · 只读。{snapshot.data&&!snapshot.error?(snapshot.data.snapshot.loaded?'该请求构建了以下记忆材料。':'该请求未带入记忆材料。'):''}{snapshot.data?.notice} 未记录的详情不会用当前状态补齐。</p>}
    {ready&&(historical||!view.error)&&!(historical&&snapshot.error)&&(historical||data?.provider?.available!==false)&&<>
     <div className="memory-node-list">{rows.map(n=><button className="memory-node-row" key={n.id} data-memory-id={n.id} aria-pressed={selected?.id===n.id} onClick={()=>move({selectedId:n.id,selection:{context:selectionContext,node:n}})}>
      <span><strong>{n.title}</strong><span className="memory-node-excerpt">{n.body||((n.reason==='permission')?'来源不在当前读取范围':n.available===false?'此版本正文不可用':'尚无正文')}</span></span><small>{status(n)}</small>
     </button>)}</div>
     {!rows.length&&<p className="memory-empty">{paged?'本页没有此作用域的节点，可翻页继续查看。':historical?'该请求没有记录此作用域的记忆。':scope==='session'?'本会话还没有采集记忆。':scope==='project'?'当前没有可读取的项目记忆。':scope==='habit'?'当前没有可读取的跨项目习惯。':'没有未标记作用域的记录。'}</p>}
    </>}
    {paged&&<nav className="memory-pagination" aria-label="记忆分页"><button disabled={busy||!offset} onClick={()=>move({offset:Math.max(0,offset-80),selectedId:undefined})}>上一页</button><span>{offset+1}–{Math.min(offset+80,data?.totalNodes||0)} / {data?.totalNodes}</span><button disabled={busy||!data?.hasMore} onClick={()=>move({offset:data?.nextOffset??offset+80,selectedId:undefined})}>下一页</button></nav>}
    {!!data?.sessionMemory&&!preview&&!historical&&scope==='session'&&<details className="memory-session-notes"><summary>会话记录（未升格）</summary><pre>{data.sessionMemory}</pre></details>}
    {!preview&&!historical&&data?.conflicts.filter(c=>c.state==='pending').map(c=><section className="memory-conflict" key={String(c.id)}><strong>{String(c.question)}</strong><p>存在待处理的记忆冲突，请选择本次做法。</p>{(c.nodes as {id:string;title:string}[]).map(n=><button key={n.id} disabled={cannotEdit} onClick={()=>void change({operation:'decide',conflictId:c.id,selected:n.id})}>{n.title}</button>)}</section>)}
   </div>
   {selected&&<aside className="memory-node-detail" aria-label="记忆节点详情" ref={detail} onScroll={e=>{saved.scroll[nodeKey]=e.currentTarget.scrollTop;}}>
    <header><button onClick={()=>move({selectedId:undefined})}>关闭详情</button><small>{historical?'历史请求 · 只读':preview?'预览 · 只读':selected.reason==='permission'?'来源未授权 · 只读':selected.readonly?'只读节点':'节点详情'}</small></header>
    <h3>{selected.title}</h3><p className="memory-detail-scope">{scopes.find(s=>s.id===scopeOf(selected))!.description}</p>
    {selected.available===false?<p>此版本的历史正文不可用。</p>:<pre>{selected.body}</pre>}
    <MemoryNodeDetails key={nodeKey} node={selected} nodeKey={nodeKey} saved={saved} id={id} historical={historical} preview={preview} cannotEdit={cannotEdit} capabilities={capabilities} mode={data?.mode||mode} inventory={inventory} visible={visible} status={status(selected)} change={change}/>
   </aside>}
  </div>
 </section>;
}

// 按提供方或请求、节点 ID 和版本键挂载详情。证据分页只为当前可查询的节点开启。
function MemoryNodeDetails({node:n,nodeKey,saved,id,historical,preview,cannotEdit,capabilities,mode,inventory,visible,status,change}:{node:DisplayNode;nodeKey:string;saved:SavedView;id:string;historical:boolean;preview:boolean;cannotEdit:boolean;capabilities?:Record<string,boolean>;mode:string;inventory?:Inventory;visible:boolean;status:string;change:(p:Record<string,unknown>)=>Promise<void>}){
 const [draft,setDraft]=useState<Draft>(()=>saved.drafts[nodeKey]||{body:n.body,weight:n.weight??70,useThread:id,useOperation:'',useNote:'',attempt:''});
 const checks=useQuery<CheckPage>({queryKey:['memory-checks',draft.useThread,draft.checkBefore],queryFn:()=>api('/api/v2/conversations/threads/'+draft.useThread+'/code-operations'+(draft.checkBefore?'?before='+draft.checkBefore:'')),enabled:visible&&!!draft.useThread&&!preview&&!historical&&n.adapter==='managed'&&capabilities?.evidence!==false});
 // 同时更新界面草稿与范围缓存，返回此节点时保留未提交输入。
 function edit(next:Partial<Draft>){setDraft(d=>{const value={...d,...next};saved.drafts[nodeKey]=value;return value;});}
 const mutable=!historical&&!preview&&!n.readonly&&n.reason!=='permission';
 const managed=n.adapter==='managed';
 // 为当前节点版本保留每个详情分区的展开状态。
 function fold(key:string,title:string,children:React.ReactNode){return <details open={saved.expanded[nodeKey+key]||false} onToggle={e=>{saved.expanded[nodeKey+key]=e.currentTarget.open;}}><summary>{title}</summary>{children}</details>;}
 // 规范化后端可能缺失或扩展的值，方便按事实显示。
 function value(key:string,item:unknown):string{
  if(item===null||item===undefined)return '未记录';
  if(key==='conversationId')return inventory?.conversations.find(c=>c.id===item)?.title||String(item);
  if(key==='projectId')return inventory?.projects.find(p=>p.id===item)?.name||String(item);
  if(['at','created','resolvedAt'].includes(key)&&typeof item==='number')return new Date(item*1000).toLocaleString();
  if(typeof item==='object')return JSON.stringify(item,null,2);
  return ['kind','state','status','scope','actor','counterexampleReview'].includes(key)?values[String(item)]||String(item):String(item);
 }
 function record(row:Record<string,unknown>){return <dl className="memory-record">{Object.entries(row).map(([k,v])=><div key={k}><dt>{labels[k]||k}</dt><dd>{value(k,v)}</dd></div>)}</dl>;}
 const absent=(name:string)=><p className="memory-note">{historical?'该请求未记录':'尚未记录'}{name}。</p>;
 return <div className="memory-detail-sections">
  {fold('sources','来源与依据',<>
   {n.sources?.length?<><h4>来源</h4>{n.sources.map((s,i)=><div key={i}>{record(s)}</div>)}</>:absent('来源')}
   {n.evidence?.length?<><h4>使用与验证依据</h4>{n.evidence.map((e,i)=><div key={i}>{record(e)}</div>)}</>:absent('使用与验证依据')}
  </>)}
  {fold('weight','权重与带入',<>
   <dl className="memory-record"><div><dt>带入状态</dt><dd>{status}</dd></div><div><dt>权重</dt><dd>{n.weight??'未记录'}</dd></div><div><dt>状态</dt><dd>{n.state?values[n.state]||n.state:'未记录'}</dd></div>{n.priority!==undefined&&<div><dt>优先级</dt><dd>{n.priority}</dd></div>}</dl>
   {mutable&&<><label className="memory-check-label"><input type="checkbox" aria-label={'带入 '+n.title} checked={!!n.selected} disabled={cannotEdit||['permission','memory_permission'].includes(n.reason)} onChange={()=>void change(managed?{operation:'configure',id:n.id,on:!n.selected,mode}:{operation:'habit-configure',id:n.id,expectedVersion:n.version})}/>选择带入</label>
    {capabilities?.weights!==false&&<label>重要性 {draft.weight}<input aria-label={managed?'记忆权重':'习惯权重'} type="range" min="0" max="100" value={draft.weight} disabled={cannotEdit} onChange={e=>edit({weight:Number(e.target.value)})}/></label>}
    {!managed&&capabilities?.weights!==false&&<button disabled={cannotEdit} onClick={()=>void change({operation:'habit-weight',id:n.id,expectedVersion:n.version,weight:draft.weight})}>保存权重</button>}
    {managed&&<><select aria-label="节点状态" value={n.state} disabled={cannotEdit} onChange={e=>void change({operation:'configure',id:n.id,state:e.target.value})}><option value="active">有效</option><option value="reference">仅供参考</option><option value="retired">退役</option></select>{capabilities?.weights!==false&&<button disabled={cannotEdit} onClick={()=>void change({...n,weight:draft.weight,operation:'upsert'})}>保存权重</button>}</>}
   </>}
  </>)}
  {fold('promotion','升格与采纳记录',<>
   {n.habitProposal?record(n.habitProposal):absent('升格或采纳记录')}
   {n.learning&&Object.keys(n.learning).length>0&&record(n.learning)}
   {mutable&&managed&&capabilities?.promote!==false&&<><button disabled={cannotEdit} onClick={()=>void change({operation:'promote',id:n.id})}>{n.scope==='session'?'检查项目升格资格':'检查跨项目习惯资格'}</button>
    {n.habitProposal?.status==='adopted'&&<p>此版本已采纳，来源与采用记录保留。</p>}
    {typeof n.habitProposal?.id==='string'&&n.habitProposal.status!=='adopted'&&<button disabled={cannotEdit} onClick={()=>void change({operation:'adopt',proposalId:n.habitProposal!.id})}>{n.habitProposal.scope==='project'?'采纳到项目':'采纳到跨项目习惯'}</button>}
   </>}
  </>)}
  {fold('maintenance','版本与维护',<>
   <dl className="memory-record"><div><dt>版本</dt><dd>{n.version??'未记录'}</dd></div><div><dt>原分类路径</dt><dd>{n.layer?.join(' / ')||'未记录'}</dd></div>{n.entryType&&<div><dt>内容类别</dt><dd>{n.entryType}</dd></div>}</dl>
   {mutable&&managed&&<><label>正文<textarea aria-label="记忆正文" value={draft.body} disabled={cannotEdit} onChange={e=>edit({body:e.target.value})}/></label><button disabled={cannotEdit} onClick={()=>void change({...n,body:draft.body,weight:draft.weight,operation:'upsert'})}>保存版本</button>
    {capabilities?.evidence!==false&&<>
     <button disabled={cannotEdit} onClick={()=>void change({operation:'evidence',id:n.id,kind:'human-confirmed',note:'用户确认此工作区事实可复用'})}>确认此事实可复用</button>
     <details><summary>记录使用与反例</summary><p>带入不等于采用，检查通过不证明语义适用；请记录具体依据。</p>
      <label>使用会话<select aria-label="记忆使用会话" value={draft.useThread} disabled={cannotEdit} onChange={e=>edit({useThread:e.target.value,useOperation:'',checkBefore:undefined})}>{inventory?.conversations.map(t=><option key={t.id} value={t.id}>{t.title} · {inventory?.projects.find(p=>p.id===t.projectId)?.name||'无项目'}</option>)}</select></label>
      <label>实际检查（如有）<select aria-label="记忆使用检查" value={draft.useOperation} disabled={cannotEdit} onChange={e=>edit({useOperation:e.target.value})}><option value="">用户记录实际使用，不声称运行过检查</option>{checks.data?.operations.filter(op=>op.operation==='check').map(op=><option key={op.id} value={op.id}>{op.started?new Date(op.started*1000).toLocaleString():'项目检查'} · {op.state} · 退出码 {op.result?.process?.exitCode??'未知'}</option>)}</select></label>
      {checks.data?.hasMore&&<button disabled={cannotEdit} onClick={()=>edit({checkBefore:checks.data?.nextBefore})}>更早的检查</button>}{draft.checkBefore&&<button onClick={()=>edit({checkBefore:undefined})}>最新检查</button>}
      {checks.error&&<p role="alert">检查记录：{checks.error.message}</p>}
      <textarea aria-label="实际使用依据" placeholder="在哪个任务采用，依据是什么" value={draft.useNote} disabled={cannotEdit} onChange={e=>edit({useNote:e.target.value})}/><button disabled={cannotEdit||!draft.useThread||!draft.useNote.trim()} onClick={()=>void change({operation:'evidence',id:n.id,kind:'applied',conversationId:draft.useThread,...(draft.useOperation?{operationId:draft.useOperation}:{}),note:draft.useNote})}>记录实际使用</button>
      <textarea aria-label="具体反例尝试" placeholder="具体尝试和适用条件" value={draft.attempt} disabled={cannotEdit} onChange={e=>edit({attempt:e.target.value})}/><button disabled={cannotEdit||!draft.attempt.trim()} onClick={()=>void change({operation:'evidence',id:n.id,kind:'challenge',attempt:draft.attempt,outcome:'not_found'})}>记录已尝试，未找到反例</button><button disabled={cannotEdit||!draft.attempt.trim()} onClick={()=>void change({operation:'evidence',id:n.id,kind:'challenge',attempt:draft.attempt,outcome:'found'})}>记录发现的反例</button>
     </details>
     <button disabled={cannotEdit} onClick={()=>void change({operation:'evidence',id:n.id,kind:'counterexample',note:'用户标记此事实需要复核'})}>标记需要复核</button>
     {n.evidence?.some(e=>e.kind==='counterexample'&&!e.resolvedAt)&&<button disabled={cannotEdit} onClick={()=>void change({operation:'evidence',id:n.id,kind:'resolve-counterexample',note:'用户确认原反例已解决'})}>确认反例已解决</button>}
    </>}
   </>}
   {n.versions?.length?<details><summary>历史版本{historical?'':'与回退'}</summary>{n.versions.map(v=><section key={String(v.version)}>{record(v)}{mutable&&managed&&<button disabled={cannotEdit} onClick={()=>void change({operation:'configure',id:n.id,restoreVersion:v.version})}>恢复 v{String(v.version)}</button>}</section>)}</details>:absent('历史版本')}
  </>)}
 </div>;
}
