// 在会话内展示文件变更摘要，并生成交给完整审查页的定位信息。
// 主要引用方包括 `RunChanges.tsx` 和 `ActionFileReview.tsx`。
import { useState,useEffect,useRef } from "react";
import { useInfiniteQuery,useQuery } from "@tanstack/react-query";
import { api,APIError,queryString } from "./client";
import type { SourceChange,SourceChangePage,SourceChangeSummary,SourceFilePage } from "./generated";
import { CodeRows,type CodeTarget,codeURL } from "./SourceCodeView";
import { diffLines,type CodeLine } from "./codeDiff";
import type { CodeAnchor } from "./sourceCode";
import { useSourceSyntax } from "./useSourceSyntax";
import { useWorkroomChangeMenu, workroomLine } from "./WorkroomChangeLink";

// 由时间线、证据和变更卡片传给审查抽屉。会话和原变更身份确定取证入口，版本及文件位置约束定位。
export type ReviewLocation={conversationId:string;changeId:string;revision?:string;fileIndex?:number;anchor?:CodeAnchor;diffOffset?:number;kind?:"range";runId?:string;resourceId?:string;frames?:string[]};
function ColoredInline({target,rows,onLine}:{target:CodeTarget;rows:CodeLine[];onLine:(side:"before"|"after",line:number)=>void}) {
  const syntax=useSourceSyntax(target);
  return <CodeRows lines={rows} onLine={onLine} syntax={syntax.error?undefined:syntax.data}/>;
}
function InlineFile({target,resourceId,onReview}:{target:CodeTarget;resourceId:string;onReview:(location:ReviewLocation)=>void}) {
  const openMenu = useWorkroomChangeMenu();
  const element=useRef<HTMLElement>(null);const [visible,setVisible]=useState(false);
  useEffect(()=>{const observer=new IntersectionObserver(entries=>setVisible(entries.some(e=>e.isIntersecting)),{rootMargin:'150px'});
    if(element.current)observer.observe(element.current);return()=>observer.disconnect();},[]);
  const query=useInfiniteQuery({
    queryKey:["inline-code",target.conversationId,target.id,target.revision,target.index,target.accessVersion],
    initialPageParam:0,
    queryFn:({pageParam})=>api<SourceFilePage>(`${codeURL(target)}?revision=${target.revision}&after=${pageParam}`),
    getNextPageParam:page=>page.hasMore?page.next:undefined,staleTime:Infinity,gcTime:0,retry:false,
  });
  const denied=query.error instanceof APIError && [403,404,409].includes(query.error.status);
  const pages=denied?undefined:query.data?.pages;
  const decodedPages=pages?.map(diffLines)??[];
  const page=pages?.[0],rows=decodedPages.flat();
  const review=(anchor?:CodeAnchor,diffOffset?:number)=>onReview({conversationId:target.conversationId,changeId:target.id,
    revision:target.revision,fileIndex:target.index,anchor,diffOffset});
  const openLine=(side:"before"|"after",line:number)=>{
    const pageIndex=decodedPages.findIndex(lines=>lines.some(row=>(side==="before"?row.oldLine:row.newLine)===line));
    const offset=query.data?.pageParams[pageIndex];
    review({side,line},typeof offset==="number"?offset:undefined);
  };
  const workroomTarget = { conversationId:target.conversationId,resourceId,path:target.file.path,kind:target.kind,
    changeId:target.id,revision:target.revision,fileIndex:target.index,afterKind:target.file.after.kind };
  return <section ref={element} className="inline-code-file" aria-label={`代码改动 ${target.file.path}`}>
    <div className="inline-code-heading" onContextMenu={denied?undefined:event=>openMenu?.(event,workroomTarget)}><strong>{target.file.path}</strong>
      {page&&<span><span className="added">+{page.additions}</span> <span className="deleted">−{page.deletions}</span></span>}
      <button onClick={()=>review()}>完整审查</button></div>
    {query.isPending&&<small>正在读取实际改动…</small>}
    {query.error&&<p role="alert">{query.error.message} <button type="button" onClick={()=>void (
      query.isFetchNextPageError&&!denied?query.fetchNextPage():query.refetch())}>重试读取差异</button></p>}
    {page&&<><div className="inline-code-scroll" tabIndex={0} role="region" aria-label="局部代码增减"
      onContextMenu={event=>openMenu?.(event,{...workroomTarget,line:workroomLine(event.target)})}
      onScroll={event=>{
        const scroll=event.currentTarget;
        if(query.hasNextPage&&!query.isFetching&&!query.error&&scroll.scrollHeight-scroll.clientHeight-scroll.scrollTop<32)
          void query.fetchNextPage({cancelRefetch:false});
      }}>
      {visible?<ColoredInline target={target} rows={rows} onLine={openLine}/>:<CodeRows lines={rows} onLine={openLine}/>}
      </div>
      {rows.length>0&&<small role="status">{query.isFetchingNextPage?"正在读取后续差异…":query.hasNextPage?"滚动到底部继续读取":"已到差异末尾"}</small>}
      {!rows.length&&<small>无文本行增减。文件模式 {target.file.before.mode} → {target.file.after.mode}；行尾 {target.file.before.eol} → {target.file.after.eol}。</small>}</>}
  </section>;
}
function InlineChange({conversationId,change,onReview,accessVersion}:{accessVersion:string;conversationId:string;change:SourceChangeSummary;onReview:(location:ReviewLocation)=>void}) {
  const detail=useQuery<SourceChange>({queryKey:["inline-change",conversationId,change.id,change.revision,accessVersion],
    queryFn:()=>api<SourceChange>(`/api/v2/conversations/threads/${queryString(conversationId)}/changes/${queryString(change.id)}`).then(result=>{
      if(result.revision!==change.revision)throw Error("动作版本已更新，正在等待新记录");
      return result;
    }),
    enabled:change.access==="readable"&&!!change.revision&&!!change.fileCount,staleTime:Infinity,gcTime:0});
  if(change.access!=="readable")return <small>该动作的代码权限已变化，请重新核对资料范围。</small>;
  if(!change.fileCount)return null;
  const files=detail.error?[]:detail.data?.files||[];
  return <div className="inline-code-changes">
    {change.state!=="observed"&&<small>操作后版本待核对</small>}
    {detail.error&&<p role="alert">{detail.error.message}</p>}
    {files.slice(0,3).map((file,index)=>["text","absent"].includes(file.before.kind)&&["text","absent"].includes(file.after.kind)?
      <InlineFile key={index} target={{conversationId,kind:"operation",id:change.id,revision:change.revision!,index,file,accessVersion}} resourceId={detail.data!.resourceId} onReview={onReview}/>:
      <p key={index}>{file.path} · {file.status}（{file.before.kind} → {file.after.kind}）<button onClick={()=>onReview({conversationId,changeId:change.id,revision:change.revision!,fileIndex:index})}>核对保留状态</button></p>)}
    {change.fileCount!>3&&<button onClick={()=>onReview({conversationId,changeId:change.id,revision:change.revision!})}>查看全部 {change.fileCount} 个文件</button>}
    {files.some(f=>f.attribution!=="verified_file_write")&&<small>命令期间观察到的改动需核对来源，未全部标为 Agent 写入。</small>}
  </div>;
}
export function InlineCodeChanges({conversationId,actionId,active,onReview,accessVersion}:{accessVersion:string;conversationId:string;actionId:string;active?:boolean;onReview:(location:ReviewLocation)=>void}) {
  const [before,setBefore]=useState<number|null>(null);
  const query=useQuery<SourceChangePage>({queryKey:["inline-changes",conversationId,actionId,before,accessVersion],
    queryFn:()=>api(`/api/v2/conversations/threads/${queryString(conversationId)}/changes?actionId=${queryString(actionId)}${before?`&before=${before}`:""}`),
    refetchInterval:active?2500:3000,refetchIntervalInBackground:true});
  useEffect(()=>{if(active===false)void query.refetch();},[active]);
  const hasChanges = query.data?.changes.some(change=>!!change.fileCount);
  return <>{query.error&&<small role="status">代码版本暂不可读：{query.error.message}</small>}
    {hasChanges&&<h4 className="inline-code-label">代码改动</h4>}
    {!query.error&&query.data?.changes.map(change=><InlineChange key={`${change.id}:${change.revision}`} conversationId={conversationId} accessVersion={accessVersion} change={change} onReview={onReview}/>)}
    {query.data?.hasMore&&<button onClick={()=>setBefore(query.data!.nextBefore)}>查看此动作更早的版本记录</button>}
    {before&&<button onClick={()=>setBefore(null)}>返回最新动作版本</button>}</>;
}
