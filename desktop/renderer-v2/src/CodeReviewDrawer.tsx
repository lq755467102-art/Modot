// 在同一审查视图内定位工作区源码、动作相关变更、差异与原始证据。
// 主要引用方包括 `AgentHome.tsx`。
import { useEffect,useLayoutEffect,useMemo,useRef,useState,type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api,queryString } from "./client";
import type { ConversationScope,SourceChange,SourceChangePage,SourceRange,CorrectionTarget,RunChangeFile } from "./generated";
import { CorrectionForm } from "./Corrections";
import { SourceCodeView,initialCodeReading,type CodeAnchor,type CodeReading,type CodeTarget } from "./SourceCodeView";
import type { ReviewLocation } from "./InlineCodeChanges";
import { changeStatus, LineCounts, useRunChanges } from "./RunChanges";
import { UiIcon } from "./UiIcon";
import {ReviewPopover} from "./ReviewPopover";
import {useUiLanguage} from "./UiLanguage";
import { useWorkroomChangeMenu, workroomLine } from "./WorkroomChangeLink";

// `target` 由 `sourceCode.ts` 约束到保留文件及版本。
// `view` 选择源码侧，`key` 区分独立页签的阅读位置。
type SourceTab={key:string;target:CodeTarget;view:"before"|"after"|"staged"};
const sideTitle={before:"改前",after:"改后",staged:"暂存"};
type ReviewDocument=SourceChange|SourceRange;
type PinnedReview = {id:string; revision:string; kind:"operation"|"range"};
// A conversation remembers the reader's location. Code and access decisions are fetched on opening.
export type ReviewBookmark = {
  scope: string; resource: string; run: string; operation: string;
  frozen?: PinnedReview; expected?: string; fileOffset: number; issueOffset: number; index: number | null;
  frames?: string[]; selection?: CorrectionTarget; reading?: {key:string; position:CodeReading};
};

const operationTitle = (operation:string) => ({
  write:"修改文件", restore:"恢复文件", check:"运行项目检查", branch:"准备分支",
  initialize:"初始化代码目录", integrate:"整合任务改动",
}[operation] || "代码操作");
const operationState = (state:string) => ({
  observed:"已记录", failed:"失败", unknown:"待核对", prepared:"执行中",
}[state] || "待核对");

// 读范围页时先用 `client.ts` 校验当前范围版本和文件，再沿 `sourceIds` 定位原操作。
// 抽屉停在旧版本时不会直接跳到新记录。
function RelatedCalls({location, path, accessVersion, onSelect}:{
  location:ReviewLocation;path:string;accessVersion?:string;
  onSelect:(change:SourceChange)=>void;
}){
  const [open,setOpen]=useState(false);
  const [page,setPage]=useState(0);
  const base="/api/v2/conversations/threads/"+queryString(location.conversationId);
  const query=useQuery({
    queryKey:["related-file-calls",location.changeId,location.revision,location.fileIndex,accessVersion,page],
    enabled:open&&accessVersion!==undefined,
    gcTime:0,
    queryFn:async()=>{
      const index=location.fileIndex??0;
      const range=await api<SourceRange>(base+"/change-ranges/"+queryString(location.changeId)+"?fileOffset="+Math.floor(index/100)*100);
      const file=range.files[index-range.fileOffset];
      if(range.revision!==location.revision||file?.path!==path)throw Error("此文件的保留范围已更新，请重新打开。");
      const ids=file.sourceIds||[];
      const results=await Promise.allSettled(ids.slice(page*8,page*8+8).map(id=>
        api<SourceChange>(base+"/changes/"+queryString(id))));
      return {results,next:ids.length>(page+1)*8};
    },
  });
  return <details onToggle={event=>setOpen(event.currentTarget.open)}>
    <summary>查看相关调用</summary>
    {open&&query.isPending&&<small>正在读取相关调用…</small>}
    {open&&query.error&&<small role="status">{query.error.message}</small>}
    {open&&!query.error&&query.data&&<>
      {!query.data.results.length&&<small>没有可追溯的调用。</small>}
      {query.data.results.map((result,index)=>result.status==="fulfilled"?
        <button key={result.value.id} onClick={()=>onSelect(result.value)}>
          {operationTitle(result.value.operation)} · {path} · {operationState(result.value.state)}
          {result.value.started ? " · "+new Date(result.value.started*1000).toLocaleTimeString() : ""}
        </button>:<small key={index}>有一条调用记录暂不可读。</small>)}
      {page>0&&<button data-keep-menu onClick={()=>setPage(n=>n-1)}>上一页调用</button>}
      {query.data.next&&<button data-keep-menu onClick={()=>setPage(n=>n+1)}>后续调用</button>}
    </>}
  </details>;
}

function FileActions({path,before,after,onSource,onDiff,related}:{path:string;before:string;after:string;onSource:(side:"before"|"after")=>void;onDiff:()=>void;related?:ReactNode}){
  return <>
    <button type="button" disabled={before!=="text"&&after!=="text"} aria-label={`打开保留源码 ${path}`} title="打开保留源码"
      onClick={()=>onSource(after==="text"?"after":"before")}><UiIcon name="open"/></button>
    <details className="review-file-options"><summary title="文件操作" aria-label={`文件操作 ${path}`}><UiIcon name="more"/></summary>
      <div onClick={event=>{if((event.target as HTMLElement).closest("button:not([data-keep-menu])")){const menu=event.currentTarget.parentElement as HTMLDetailsElement;menu.open=false;menu.querySelector("summary")?.focus();}}}>
        {before==="text"&&<button onClick={()=>onSource("before")}>打开改前文件</button>}
        {after==="text"&&<button onClick={()=>onSource("after")}>打开改后文件</button>}
        <button onClick={onDiff}>查看文件差异</button>
        {related}
      </div>
    </details>
  </>;
}

/** Opening is owned by an explicit user action. Background refresh never chooses a new version. */
export function CodeReviewDrawer({conversationId,runId,running,initial,bookmark,onRemember,onClose,onEvidence,onFeedback,onDisplayedFrames,allowCorrections=false,activation}:{bookmark?:ReviewBookmark;onRemember?:(value:ReviewBookmark)=>void;conversationId:string;runId?:string;running?:boolean;initial?:ReviewLocation;allowCorrections?:boolean;activation?:number;
  onDisplayedFrames?:(owner:string,run:string,frames:string[])=>void;
  onClose:()=>void;onEvidence:(owner:string,action:string|null,run?:string)=>void;onFeedback:(text:string)=>void}) {
  const base=`/api/v2/conversations/threads/${queryString(conversationId)}`;
  const openWorkroomMenu = useWorkroomChangeMenu();
  const visible = activation !== undefined;
  const [scopeChoice,setScope]=useState(bookmark?.scope ?? (initial?.kind==="range"?"run":initial?"operation":""));
  const [resource,setResource]=useState(bookmark?.resource ?? initial?.resourceId ?? "");const [run,setRun]=useState(bookmark?.run ?? initial?.runId ?? runId ?? "");
  const [operation,setOperation]=useState(bookmark?.operation ?? initial?.changeId ?? "");
  const [frozen,setFrozen]=useState<PinnedReview|undefined>(bookmark ? bookmark.frozen :
    initial?.kind==="range"&&initial.changeId&&initial.revision?{id:initial.changeId,revision:initial.revision,kind:"range"}:undefined);
  const [fileOffset,setFileOffset]=useState(bookmark?.fileOffset ?? Math.floor((initial?.fileIndex||0)/100)*100);const [issueOffset,setIssueOffset]=useState(bookmark?.issueOffset ?? 0);
  const [overviewFrames,setOverviewFrames]=useState<string[]|undefined>(bookmark ? bookmark.frames : initial?.frames);
  const [index,setIndex]=useState<number|null>(bookmark ? bookmark.index : initial?.changeId?(initial.fileIndex??0):null);
  const [older,setOlder]=useState<number|null>(null);
  const [filter,setFilter]=useState("");
  const [filterOpen,setFilterOpen]=useState(false);
  const {t}=useUiLanguage();
  const [toolbar,setToolbar]=useState<HTMLSpanElement|null>(null);
  const [sort,setSort]=useState(false);
  const [refresh,setRefresh]=useState(0);const [loadedVersion,setLoadedVersion]=useState("");
  const [selection,setSelection]=useState<CorrectionTarget|undefined>(bookmark?.selection);
  const [tabs,setTabs]=useState<SourceTab[]>([]);
  const [activeTab,setActiveTab]=useState("");
  const [pendingSource,setPendingSource]=useState<"before"|"after">();
  const [expectRevision,setExpectRevision]=useState(bookmark ? bookmark.expected : initial?.revision);
  const readings=useRef(new Map<string,CodeReading>(bookmark?.reading ? [[bookmark.reading.key,bookmark.reading.position]] : []));const heading=useRef<HTMLHeadingElement>(null);
  const retained=useRef<{data:ReviewDocument;accessVersion?:string}|undefined>(undefined);
  useEffect(()=>{if(visible)heading.current?.focus();},[visible]);
  // A new opening has its own scope request, including when a previous
  // opening closed with a request still in flight.
  const opening = useMemo(() => visible ? crypto.randomUUID() : null, [visible]);
  const selectedScope=useQuery<ConversationScope>({
    queryKey:["review-scope",conversationId,activation,opening],
    queryFn:()=>api(base+"/scope?review=1"),enabled:visible,gcTime:0,refetchInterval:visible?3000:false,
    refetchIntervalInBackground:visible,
  });
  const access = visible && !selectedScope.error ? selectedScope.data : undefined;
  const currentResources=access?.resources.filter(r=>r.kind==="code"&&r.permission==="read")||[];
  const resources=[...currentResources,...(access?.historicCode ?? [])];
  const sources=useQuery<SourceChangePage>({queryKey:["review-operations",conversationId,older,access?.revision],
    queryFn:()=>api(base+"/changes"+(older?`?before=${older}`:"")),enabled:!!access,refetchInterval:access&&!older?3000:false});
  const latest=sources.data?.changes.map(s=>`${s.id}:${s.state}:${s.revision}`).join("|")||"";
  const availableRuns=[...new Set([runId,...(sources.data?.changes.map(c=>c.source.runId as string)||[])].filter(Boolean))] as string[];
  if(run&&!availableRuns.includes(run))availableRuns.unshift(run);
  const choice=operation||sources.data?.changes[0]?.id||"";
  const selectedRun=run||availableRuns[0]||"";
  const scope=scopeChoice||(selectedRun?"run":"task");
  const selectable=scope==="workspace"?currentResources:resources;
  const rid=(selectable.some(item=>item.id===resource)?resource:selectable[0]?.id)||"";
  const aggregate=scope==="run"&&!resource&&!frozen;
  const overview=useRunChanges(conversationId,aggregate?selectedRun:"",access?.revision,false,fileOffset,overviewFrames);
  function reset() {setActiveTab("");setTabs([]);setPendingSource(undefined);setExpectRevision(undefined);setFrozen(undefined);setOverviewFrames(undefined);setFileOffset(0);setIssueOffset(0);setIndex(null);setFilter("");setRefresh(n=>n+1);}
  const ready=!!access&&!aggregate&&(!!scopeChoice||!sources.isPending)&&(scope==="operation"?!!choice:resources.some(r=>r.id===rid)&&(scope!=="run"||!!selectedRun));
  const document=useQuery<ReviewDocument>({
    queryKey:["review-document",conversationId,scope,rid,choice,selectedRun,refresh,frozen?.id,fileOffset,issueOffset,selectedScope.data?.revision],
    enabled:visible&&ready,staleTime:Infinity,gcTime:0,
    placeholderData:()=>{const prior=retained.current;
      return frozen&&prior?.data.id===frozen.id&&prior.accessVersion===selectedScope.data?.revision&&prior.data.fileOffset===fileOffset&&
        (!("issueOffset" in prior.data)||prior.data.issueOffset===issueOffset)?prior.data:undefined;},
    queryFn:()=>api(frozen?`${base}/${frozen.kind==="operation"?"changes":"change-ranges"}/${frozen.id}?fileOffset=${fileOffset}&issueOffset=${issueOffset}`:
      scope==="operation"?`${base}/changes/${queryString(choice)}?fileOffset=${fileOffset}`:
      `${base}/change-ranges?resourceId=${queryString(rid)}&scope=${scope}${scope==="run"?`&runId=${queryString(selectedRun)}`:""}`),
  });
  const readableDocument = document.data && resources.some(r=>r.id===document.data?.resourceId) ? document.data : undefined;
  useEffect(()=>{const d=readableDocument;if(d){retained.current={data:d,accessVersion:selectedScope.data?.revision};}if(d&&!frozen&&d.revision&&(!expectRevision||expectRevision===d.revision)){
    setFrozen({id:d.id,revision:d.revision,kind:scope==="operation"?"operation":"range"});setLoadedVersion(latest);
  }},[readableDocument,frozen,scope,latest,expectRevision,selectedScope.data?.revision]);
  const pinnedRevision=frozen?.revision||expectRevision;
  const revisionMismatch=!!pinnedRevision&&!!document.data&&document.data.revision!==pinnedRevision;
  const d=document.error||revisionMismatch?undefined:readableDocument;
  const isRange=d&&"scope" in d;
  function targetAt(i:number):CodeTarget|undefined{
    const file=d?.files[i-(d.fileOffset||0)];
    return d?.revision&&file?{conversationId,kind:isRange?"range":"operation",id:d.id,revision:d.revision,index:i,file,accessVersion:selectedScope.data?.revision}:undefined;
  }
  const target=index===null?undefined:targetAt(index);
  const readingKey=target?`${target.id}:${target.revision}:${index}`:"";
  const source=isRange?undefined:(d as SourceChange|undefined)?.source;
  const currentTab=tabs.find(tab=>tab.key===activeTab);
  const viewer=currentTab&&access&&resources.some(r=>r.id===rid)?{...currentTab.target,accessVersion:selectedScope.data?.revision}:target;
  const viewerKey=currentTab?.key||readingKey;
  const leave = useRef<() => void>(() => {});
  useLayoutEffect(() => {
    leave.current = () => {
      const position = readings.current.get(viewerKey);
      onRemember?.({scope:scopeChoice, resource, run, operation, frozen, expected:expectRevision, fileOffset, issueOffset,
        index, frames:overviewFrames, selection, reading:position && readingKey ? {key:readingKey, position} : undefined});
    };
  });
  useLayoutEffect(() => () => leave.current(), []);
  const initialAnchor=initial?.changeId===target?.id&&initial?.fileIndex===index?initial:undefined;
  const initialReading=readings.current.get(viewerKey)??{
    ...initialCodeReading,anchor:initialAnchor?.anchor,after:initialAnchor?.diffOffset??0,
  };
  function openView(view:string,anchor?:CodeAnchor, selected=viewer){
    if(!selected)return;
    const baseKey=`${selected.id}:${selected.revision}:${selected.index}`;
    if(view==="before"||view==="after"||view==="staged"){
      const key=`${baseKey}:${view}`;
      const saved=readings.current.get(key);
      const diffAnchor=readings.current.get(baseKey)?.anchor;
      if(!anchor&&diffAnchor?.side===view)anchor=diffAnchor;
      if(!saved||(anchor&&(saved.anchor?.line!==anchor.line||saved.anchor?.offset!==anchor.offset)))
        readings.current.set(key,{
          ...initialCodeReading,wrap:saved?.wrap??initialReading.wrap,view,
          after:anchor?.offset??0,line:anchor&&anchor.offset===undefined?Math.max(1,anchor.line-12):undefined,anchor,
        });
      setTabs(old=>old.some(tab=>tab.key===key)?old:[...old,{key,target:selected,view}]);
      setActiveTab(key);
      setIndex(selected.index);
      return;
    }
    const saved=readings.current.get(baseKey)||initialCodeReading;
    if(saved.view!==view)readings.current.set(baseKey,{...saved,view,after:0,line:undefined,hunk:undefined,scrollTop:undefined,scrollLeft:undefined});
    setActiveTab("");
    if(index!==selected.index){setFileOffset(Math.floor(selected.index/100)*100);setIndex(selected.index);}
  }
  function closeTab(key:string){
    const index=tabs.findIndex(tab=>tab.key===key);
    setTabs(old=>old.filter(tab=>tab.key!==key));
    if(activeTab===key)setActiveTab(tabs[index-1]?.key||tabs[index+1]?.key||"");
  }
  useEffect(()=>{
    if(target&&pendingSource){
      openView(pendingSource,undefined,target);
      setPendingSource(undefined);
    }
  },[target?.id,target?.index,pendingSource]);
  function openRunFile(file:RunChangeFile,view?:"before"|"after"){
    reset();
    setResource(file.resourceId);
    setFrozen({id:file.changeId,revision:file.revision,kind:"range"});
    setExpectRevision(file.revision);
    setFileOffset(Math.floor(file.fileIndex/100)*100);
    setIndex(file.fileIndex);
    setPendingSource(view);
  }
  const summary=aggregate&&access&&!overview.error?overview.data:undefined;
  useEffect(()=>{
    if(visible&&summary)onDisplayedFrames?.(conversationId,selectedRun,summary.frames);
  },[visible,summary,conversationId,selectedRun,onDisplayedFrames]);
  function overviewPage(offset:number){
    setOverviewFrames(summary?.frames);
    setFileOffset(offset);
  }
  const totals=summary?.totals||d?.totals;
  const count=summary?.fileCount??d?.fileCount;
  const filterFiles=<>{filterOpen&&<input autoFocus aria-label="筛选当前页代码文件" placeholder="筛选当前页文件" value={filter} onChange={e=>setFilter(e.target.value)}/>}</>;
  function visibleFiles<T extends {path:string}>(files:T[]){
    const rows=files.map((file,i)=>({file,i})).filter(({file})=>file.path.toLowerCase().includes(filter.toLowerCase()));
    return sort?rows.sort((a,b)=>a.file.path.localeCompare(b.file.path,undefined,{numeric:true})):rows;
  }
  function relatedCalls(location:ReviewLocation,path:string){
    return <RelatedCalls key={location.changeId+":"+location.revision+":"+location.fileIndex} location={location} path={path} accessVersion={access?.revision}
      onSelect={change=>{
        if(typeof change.source.actionId==="string"){
          onEvidence(conversationId,change.source.actionId,String(change.source.runId||""));
          return;
        }
        reset();setScope("operation");setOperation(change.id);setResource(change.resourceId);
        setExpectRevision(change.revision);
      }}/>;
  }
  useEffect(()=>{
    if(!visible)return;
    const closeMenus=(event:PointerEvent)=>{
      for(const menu of heading.current?.closest("aside")?.querySelectorAll<HTMLDetailsElement>(".review-options[open],.review-file-options[open]")??[])
        if(!menu.contains(event.target as Node))menu.open=false;
    };
    window.addEventListener("pointerdown",closeMenus);
    return ()=>window.removeEventListener("pointerdown",closeMenus);
  },[visible]);
  function backToFiles(){
    if(scope==="run"&&resource){setResource("");reset();}
    else {setIndex(null);setActiveTab("");}
  }
  return <aside className="code-review-drawer" hidden={!visible} style={visible?undefined:{display:"none"}} aria-label="完整代码审查" onKeyDown={e=>{
    if(e.key!=="Escape")return;
    e.stopPropagation();
    if(e.currentTarget.querySelector(".review-flyout:popover-open"))return;
    const menu=(e.target as HTMLElement).closest<HTMLDetailsElement>(".review-options[open],.review-file-options[open]");
    if(menu){menu.open=false;menu.querySelector("summary")?.focus();return;}
    if(filterOpen&&(e.target as HTMLElement).matches("input[aria-label='筛选当前页代码文件']")){
      setFilterOpen(false);setFilter("");e.currentTarget.querySelector<HTMLButtonElement>(".review-actions button")?.focus();return;
    }
    onClose();
  }}>
    <header className="review-scope-controls"><h2 className="sr-only" ref={heading} tabIndex={-1}>{t("变更")}</h2>
      {(viewer||scope==="run"&&resource)&&<button className="review-back" onClick={backToFiles} title="返回文件列表" aria-label="返回文件列表"><UiIcon name="back"/></button>}
      <select aria-label="代码审查范围" value={scope} onChange={e=>{setScope(e.target.value);setResource("");reset();}}>
        <option value="run">{selectedRun===runId&&running?"本轮":selectedRun===availableRuns[0]?"上一轮":"历史轮次"}</option><option value="operation">单次操作</option><option value="task">会话累计</option><option value="workspace">工作区</option></select>
      {count!==undefined&&<span className="review-totals">{count} 个文件 {totals&&<LineCounts {...totals}/>}</span>}
      <div className="review-actions">
        <button type="button" title="筛选当前页代码文件" aria-label="筛选当前页代码文件" aria-pressed={filterOpen} onClick={()=>{setFilterOpen(!filterOpen);if(filterOpen)setFilter("");}}><UiIcon name="search"/></button>
        {d&&<ReviewPopover icon="info" label={t("审查信息")}>
          <div className="review-version-notice"><strong>保留版本</strong><p>{d.notice}</p>
          {initial&&<small>来源会话 {conversationId} · 原操作 {initial.changeId}</small>}</div>
          {viewer&&<div className="review-file-metadata"><strong>{viewer.file.path}</strong>
            <p>{viewer.file.before.kind} → {viewer.file.after.kind} · 行尾 {viewer.file.before.eol||"无"} → {viewer.file.after.eol||"无"} · 模式 {viewer.file.before.mode||"无"} → {viewer.file.after.mode||"无"}</p>
            <p>{viewer.file.attribution==="verified_file_write"?"与受控写入字节核对一致":viewer.file.attribution==="derived_verified_writes"?"由已核对的写入合成":viewer.file.attribution==="workspace_user_and_agent"?"包含用户与 Agent 的工作区变化":"观察到的变化，来源需核对"}</p>
          </div>}
          {typeof source?.actionId==="string"&&<button onClick={()=>onEvidence(conversationId,String(source.actionId),String(source.runId||""))}>返回原动作的步骤与结果</button>}
        </ReviewPopover>}
        <span className="review-reader-tools" ref={setToolbar}/>
      </div>
      <details className="review-options"><summary title="轮次、资料与来源" aria-label="轮次、资料与来源"><UiIcon name="more"/></summary>
      <div>
      <button type="button" aria-label="显式刷新目标版本" disabled={!ready&&!aggregate||document.isFetching} onClick={()=>{reset();if(aggregate)void overview.refetch();}}>刷新到最新版本</button>
      <button type="button" aria-label="按当前页文件名排序" aria-pressed={sort} onClick={()=>setSort(!sort)}>按文件名排序</button>
      {viewer&&<button type="button" aria-label="折叠文件差异" onClick={()=>{setIndex(null);setActiveTab("");}}>折叠文件差异</button>}
      {scope==="operation"?<><label>操作 <select aria-label="代码来源操作" value={choice} onChange={e=>{setOperation(e.target.value);reset();}}>
        {operation&&!sources.data?.changes.some(c=>c.id===operation)&&<option value={operation}>当前所选操作</option>}
        {sources.data?.changes.map(c=><option key={c.id} value={c.id}>{operationTitle(c.operation)}{c.files[0]?" · "+c.files[0].path:""} · {operationState(c.state)}{c.started?" · "+new Date(c.started*1000).toLocaleTimeString():""}</option>)}
      </select></label>{sources.data?.hasMore&&<button onClick={()=>setOlder(sources.data!.nextBefore)}>查找更早操作</button>}
      {older&&<button onClick={()=>setOlder(null)}>最新操作列表</button>}</>:
      <><label>资料 <select aria-label="审查代码资料" value={scope==="run"?resource:rid} onChange={e=>{setResource(e.target.value);reset();}}>
        {scope==="run"&&<option value="">本轮全部代码资料</option>}
        {selectable.map(r=><option key={r.id} value={r.id}>{r.name}</option>)}
      </select></label>{scope==="run"&&<label>轮次 <select aria-label="代码审查轮次" value={selectedRun} onChange={e=>{setRun(e.target.value);reset();}}>
        {availableRuns.map((r,i)=>{
          const started=sources.data?.changes.find(c=>c.source.runId===r&&c.started)?.started;
          return <option key={r} value={r}>{r===runId&&running?"本轮":i===(running?1:0)?"上一轮":"更早一轮"}{started?" · "+new Date(started*1000).toLocaleString():""}</option>;
        })}
      </select></label>}</>}
      </div></details>
      <button onClick={onClose} title="收起代码审查" aria-label="收起代码审查"><UiIcon name="close"/></button>
    </header>
    {loadedVersion&&latest!==loadedVersion&&<small role="status">已有新操作记录；当前阅读版本保持固定，刷新后再查看。</small>}
    {visible&&selectedScope.isPending&&<p role="status">正在核对当前代码读取权限…</p>}
    {access&&!resources.length&&<p>当前没有可读代码资料。可先在“项目与资料”选择代码目录；普通对话无需配置代码工程。</p>}
    {(document.error||selectedScope.error||sources.error)&&<p role="alert">{document.error?.message||selectedScope.error?.message||sources.error?.message}</p>}
    {revisionMismatch&&<p role="status">原动作版本已更新。可刷新后检查最新版本。</p>}
    {document.isPending&&ready&&<p role="status">正在核对保留的代码版本…</p>}
    {aggregate&&overview.error&&<p role="alert">{overview.error.message}</p>}
    {aggregate&&selectedRun&&overview.isPending&&access&&<p role="status">正在读取本轮变更…</p>}
    {summary&&<div className="review-file-list">{filterFiles}
      {visibleFiles(summary.files).map(({file})=><div className="review-file-row" key={file.resourceId+":"+file.path}>
        <button type="button" className="review-file" onClick={()=>openRunFile(file)} title={file.path}
          onContextMenu={event=>openWorkroomMenu?.(event,{...file,conversationId,kind:"range"})}>
          <span className="change-path">{file.path}</span>{resources.length>1&&<small>{resources.find(r=>r.id===file.resourceId)?.name}</small>}
          <small>{changeStatus(file.status)}</small><LineCounts {...file}/>
        </button>
        <FileActions path={file.path} before={file.beforeKind} after={file.afterKind} onSource={side=>openRunFile(file,side)} onDiff={()=>openRunFile(file)}
          related={relatedCalls({conversationId,changeId:file.changeId,revision:file.revision,fileIndex:file.fileIndex},file.path)}/>
      </div>)}
      {fileOffset>0&&<button onClick={()=>overviewPage(Math.max(0,fileOffset-100))}>上一页文件</button>}
      {summary.nextOffset!==null&&<button onClick={()=>overviewPage(summary.nextOffset!)}>后续文件</button>}
      {!summary.fileCount&&<p>此轮没有可确认的净代码改动。</p>}
      {summary.incomplete&&<small>另有待核对的改动，可按代码资料查看缺口。</small>}
    </div>}
    {d&&<>{d.projectBaseline==='unavailable'&&<p role="status">完整工程基线不可用。以下范围仅涵盖已保留的文件版本。</p>}
      {isRange&&(d as SourceRange).incomplete&&<details><summary>此范围存在 {(d as SourceRange).totalIssues} 项缺口</summary>
        {(d as SourceRange).issues.map((issue,i)=><p key={i}>{issue.path||"范围"} · {issue.code} · {issue.changeId}</p>)}
        {(d as SourceRange).hasMoreIssues&&<button onClick={()=>setIssueOffset(n=>n+20)}>继续读取缺口</button>}
        {!!issueOffset&&<button onClick={()=>setIssueOffset(0)}>缺口开头</button>}</details>}
      <div className="review-file-list">{filterFiles}
        {visibleFiles(d.files).filter(({i})=>!viewer||filterOpen||d.fileOffset+i===viewer.index).map(({file:f,i:row})=>{const i=d.fileOffset+row;const fileTarget=targetAt(i);return <div className="review-file-row" key={i}>
          <button className="review-file" title={f.path} aria-expanded={index===i&&!activeTab} aria-current={index===i?"true":undefined} onClick={()=>{setActiveTab("");setIndex(index===i&&!activeTab?null:i);}}
            onContextMenu={event=>{if(d.revision)openWorkroomMenu?.(event,{conversationId,resourceId:d.resourceId,
              kind:isRange?"range":"operation",path:f.path,afterKind:f.after.kind,changeId:d.id,revision:d.revision,fileIndex:i});}}>
            <span className="change-path">{f.path}</span><small>{changeStatus(f.status)}</small><LineCounts {...f}/>
          </button>
          {fileTarget&&<FileActions path={f.path} before={f.before.kind} after={f.after.kind} onSource={side=>openView(side,undefined,fileTarget)}
            onDiff={()=>{setIndex(i);setActiveTab("");}}
            related={isRange&&f.sourceIds?.length?relatedCalls({conversationId,changeId:d.id,revision:d.revision,fileIndex:i},f.path):undefined}/>}
        </div>;})}
        {d.fileOffset>0&&<button onClick={()=>{setFileOffset(Math.max(0,d.fileOffset-100));setIndex(null);}}>上一页文件</button>}
        {d.hasMoreFiles&&<button onClick={()=>{setFileOffset(d.fileOffset+100);setIndex(null);}}>后续文件</button>}
        {!d.fileCount&&<p>此范围没有可确认的净代码改动。</p>}
      </div>
      {tabs.length>0&&<nav className="source-tabs" aria-label="代码标签页">
        <button aria-current={!activeTab?"page":undefined} onClick={()=>setActiveTab("")}>改动</button>
        {tabs.map(tab=><div className="source-tab" key={tab.key}>
          <button title={tab.target.file.path} aria-current={tab.key===activeTab?"page":undefined}
            onClick={()=>setActiveTab(tab.key)}>{tab.target.file.path.split("/").at(-1)} · {sideTitle[tab.view]}</button>
          <button aria-label={`关闭 ${tab.target.file.path} · ${sideTitle[tab.view]}`} onClick={()=>closeTab(tab.key)}>×</button>
        </div>)}
      </nav>}
      {viewer&&<div className="review-reading-area" onContextMenu={event=>{
        const line = workroomLine(event.target);
        const view = readings.current.get(viewerKey)?.view ?? initialReading.view;
        if (d?.id===viewer.id && line && (view==="diff" || view==="after"))
          openWorkroomMenu?.(event,{conversationId,resourceId:d.resourceId,kind:viewer.kind,changeId:viewer.id,
            revision:viewer.revision,fileIndex:viewer.index,path:viewer.file.path,afterKind:viewer.file.after.kind,line});
      }}><SourceCodeView key={viewerKey} target={viewer} initial={initialReading} toolbar={toolbar}
        onReading={state=>readings.current.set(viewerKey,state)} onView={openView}
        onFeedback={(side,line)=>{const view=readings.current.get(viewerKey)?.view||'diff';const selectedSide=side==='after'&&view.startsWith('staged')?'staged':side;
          setSelection({kind:'code',conversationId,sourceKind:viewer.kind,id:viewer.id,revision:viewer.revision,fileIndex:viewer.index,path:viewer.file.path,side:selectedSide,line,endLine:line,hash:viewer.file[selectedSide]?.hash||undefined});}}/></div>}
      {selection&&<section className="code-selection-feedback" aria-label="所选代码反馈"><p>选中 {selection.path} · {selection.side==="before"?sideTitle.before:selection.side==="staged"?sideTitle.staged:sideTitle.after} 第 {selection.line} 行</p>
        {selection.id!==viewer?.id||selection.fileIndex!==viewer?.index||selection.revision!==viewer?.revision?<p>当前阅读位置已变化，下面意见仍绑定先前选中的保留版本。</p>:null}
        <button onClick={()=>onFeedback(`请解释这个明确保留的代码版本，先用 read_code_history 只读核对：\n${JSON.stringify(selection)}\n历史不是当前文件；请说明这行的作用和可能的问题，不要自动改动。`)}>将此版本交给 Agent 解释</button>
        {allowCorrections&&<CorrectionForm conversationId={conversationId} target={selection}/>}
        <button onClick={()=>setSelection(undefined)}>取消代码选择</button></section>}
    </>}
  </aside>;
}
