// 读取当前运行的变更概况与文件记录，并生成完整审查视图的定位参数。
// 主要引用方包括 `ConversationSideTabs.tsx` 和 `AgentHome.tsx`。
import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryString } from "./client";
import type { ReviewLocation } from "./InlineCodeChanges";
import type { RunChangeFile, RunChangeSummary } from "./generated";
import { UiIcon } from "./UiIcon";
import { useWorkroomChangeMenu } from "./WorkroomChangeLink";

export function useRunChanges(conversationId:string, runId:string, accessVersion?:string, live=false, offset=0, frames?:string[]) {
  const version=frames?.join(",")||"";
  return useQuery<RunChangeSummary>({
    queryKey:["run-changes",conversationId,runId,accessVersion,offset,live,version],
    queryFn:()=>api(`/api/v2/conversations/threads/${queryString(conversationId)}/change-ranges?scope=run-summary&runId=${queryString(runId)}&fileOffset=${offset}${version?`&frames=${version}`:""}`),
    enabled:!!conversationId&&!!runId&&accessVersion!==undefined,
    refetchInterval:live?2000:false,
    refetchIntervalInBackground:live,
    refetchOnWindowFocus:live,
  });
}

export function LineCounts({ additions, deletions, uncounted }: {additions?:number|null;deletions?:number|null;uncounted?:number}) {
  if(additions==null||deletions==null)return <small className="change-uncounted" title="无法统计此文件的增删行数">未统计</small>;
  return <span className="change-counts" title={uncounted?`另有 ${uncounted} 个文件未统计行数`:undefined}>
    <span className="added">+{additions}</span> <span className="deleted">−{deletions}</span>{!!uncounted&&<span aria-label={`${uncounted} 个文件未统计`}> *</span>}
  </span>;
}

export const changeStatus = (status:string) => ({added:"新增",deleted:"删除",modified:"修改",unknown:"待核对"}[status]??status);
export const runFileLocation = (conversationId:string, runId:string, file:RunChangeFile):ReviewLocation =>
  ({conversationId,runId,kind:"range",changeId:file.changeId,revision:file.revision,fileIndex:file.fileIndex,resourceId:file.resourceId});
export const runOverviewLocation = (conversationId:string, runId:string):ReviewLocation =>
  ({conversationId,runId,kind:"range",changeId:""});

function ChangeFiles({summary,limit,conversationId,onReview}:{summary:RunChangeSummary;limit:number;conversationId:string;onReview:(location:ReviewLocation)=>void}) {
  const openMenu = useWorkroomChangeMenu();
  const multiple=new Set(summary.files.map(file=>file.resourceId)).size>1;
  return <ul className="run-change-files">
    {summary.files.slice(0,limit).map(file=><li key={`${file.resourceId}:${file.path}`}>
      <button type="button" title={file.path} onClick={()=>onReview(runFileLocation(conversationId,summary.runId,file))}
        onContextMenu={event=>openMenu?.(event,{...file,conversationId,kind:"range"})}>
        <span className="change-path">{multiple&&file.resourceName?<small>{file.resourceName} / </small>:null}{file.path}</span>
        <small title={file.attribution==="derived_verified_writes"?"与保留写入核对一致":"动作期间观察到的改动"}>{changeStatus(file.status)}</small>
        <LineCounts {...file}/>
      </button>
    </li>)}
  </ul>;
}

// 会话面板传入当前运行与可见范围版本。`onReview` 把带原版本的审查位置交回调用者。
type RunChangeProps={conversationId:string;runId:string;accessVersion?:string;onReview:(location:ReviewLocation)=>void};
export function RunningChanges({conversationId,runId,accessVersion,onReview}:RunChangeProps) {
  const query=useRunChanges(conversationId,runId,accessVersion,true);
  const [pinned,setPinned]=useState(false);
  const [hovered,setHovered]=useState(false);
  const trigger=useRef<HTMLButtonElement>(null);
  const summary=accessVersion!==undefined&&!query.error?query.data:undefined;
  if(!summary?.fileCount)return null;
  const open=pinned||hovered;
  function close(){setHovered(false);setPinned(false);}
  function review(location:ReviewLocation){
    close();
    trigger.current?.focus();
    onReview(location);
  }
  return <div className="running-changes" onMouseEnter={()=>setHovered(true)} onMouseLeave={()=>setHovered(false)}
    onBlur={event=>{if(!event.currentTarget.contains(event.relatedTarget))close();}}
    onKeyDown={event=>{if(event.key==="Escape"&&open){event.stopPropagation();close();trigger.current?.focus();}}}>
    <button ref={trigger} type="button" aria-expanded={open} title="查看本轮已记录的文件变化"
      onClick={()=>{setPinned(!pinned);if(pinned)setHovered(false);}}>
      <UiIcon name="code"/>{summary.fileCount} 个文件已变更 <LineCounts {...summary.totals}/>
    </button>
    {open&&<section className="running-change-popover" aria-label="本轮正在变更的文件">
      <ChangeFiles summary={summary} limit={summary.files.length} conversationId={conversationId} onReview={review}/>
      {summary.incomplete&&<small>另有改动待核对。</small>}
      <button type="button" onClick={()=>review(runOverviewLocation(conversationId,runId))}>查看本轮全部变更</button>
    </section>}
  </div>;
}

export function RunChangeCard({conversationId,runId,accessVersion,status,onReview}:RunChangeProps&{status:string}) {
  const query=useRunChanges(conversationId,runId,accessVersion);
  const [expanded,setExpanded]=useState(false);
  const [offset,setOffset]=useState(0);
  const summary=accessVersion!==undefined&&!query.error?query.data:undefined;
  const next=useRunChanges(conversationId,offset?runId:"",accessVersion,false,offset,summary?.frames);
  if(!summary?.fileCount)return null;
  const page=offset&&!next.error&&next.data?.runId===runId?next.data:summary;
  const label=status==="success"||status==="completed"?"已编辑":status==="error"||status==="failed"?"失败前已改动":"中断前已改动";
  return <section className="run-change-card" aria-label="本轮文件变更" data-run={runId}>
    <header><UiIcon name="code"/><strong>{label} {summary.fileCount} 个文件</strong><LineCounts {...summary.totals}/>
      <button type="button" onClick={()=>onReview(runOverviewLocation(conversationId,runId))}>查看变更</button>
    </header>
    <ChangeFiles summary={page} limit={expanded?page.files.length:3} conversationId={conversationId} onReview={onReview}/>
    {summary.fileCount>3&&<button type="button" aria-expanded={expanded} onClick={()=>{setExpanded(!expanded);setOffset(0);}}>
      {expanded?"收起文件":`再显示 ${summary.fileCount-3} 个文件`}
    </button>}
    {expanded&&offset>0&&<button type="button" onClick={()=>setOffset(Math.max(0,offset-100))}>上一页文件</button>}
    {expanded&&page.nextOffset!==null&&<button type="button" disabled={!!offset&&next.isFetching} onClick={()=>setOffset(page.nextOffset!)}>后续文件</button>}
    {expanded&&next.error&&<small role="alert">{next.error.message}</small>}
    {summary.incomplete&&<small>另有改动待核对，可在审查中查看来源。</small>}
  </section>;
}
