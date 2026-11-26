// 在正文中解析并呈现工具引用，按需读取对应的原始证据。
// 主要引用方包括 `ConversationTimeline.tsx` 和 `Message.tsx`。
import {createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode} from "react";
import {createPortal} from "react-dom";
import {useQuery} from "@tanstack/react-query";
import {api, APIError, queryString} from "./client";
import {UiIcon} from "./UiIcon";
import {useUiLanguage} from "./UiLanguage";
import type {UiMessage} from "./uiMessages";
import "./answer-citation.css";

type Source = {conversationId?:string; accessVersion?:string};
const SourceContext = createContext<Source & {open:(id:string)=>void} | null>(null);
type Status = "timeout"|"not_started"|"child_not_started"|"failed"|"interrupted"|"cancelled_before_start"|"approval_expired"|
  "unknown"|"waiting_approval"|"starting"|"running"|"stopping"|"completed";
type Header = {reference:string; runId:string; actionId:string; revision:string; status:Status; isTest:boolean; partial:boolean; shell:boolean};
type LogPage = Header & {command?:string; after:number; text:string; next:number; hasMore:boolean; notice?:string;
  reason?:string; exitCode?:number; counts:Partial<Record<"passed"|"failed"|"errors"|"skipped",number>>};
const labels:Record<Status,UiMessage> = {
  timeout:"检查超时", not_started:"未能启动", child_not_started:"脚本报告子进程未启动", failed:"执行失败",
  interrupted:"运行中断", cancelled_before_start:"启动前已取消", unknown:"终态未确认",
  approval_expired:"审批过期，未执行",
  waiting_approval:"等待审批", starting:"正在启动", running:"执行中", stopping:"正在停止", completed:"运行已结束",
};
function label(header:Header):UiMessage {
  if(header.status==="timeout" && header.isTest)return "测试超时";
  if(header.status==="timeout" && header.shell)return "命令超时";
  return labels[header.status];
}
const pending = (status?:Status) => status==="running" || status==="starting" || status==="stopping" || status==="waiting_approval";
const path = (thread:string,id:string) => "/api/v2/conversations/threads/"+queryString(thread)+"/evidence/"+queryString(id)+"/citation";
function useHeader(source:Source, id:string, enabled:boolean, open=false) {
  return useQuery<Header>({
    queryKey:["answer-citation",source.conversationId,source.accessVersion,id],
    queryFn:()=>api(path(source.conversationId!,id)), enabled:enabled && !!source.accessVersion && !!source.conversationId,
    staleTime:30_000, gcTime:0,
    retry:(attempt,error)=>error instanceof APIError && error.code==="evidence_indexing" && attempt<4,
    refetchInterval:q=>enabled && pending(q.state.data?.status) ? 2000 : open ? 3000 : false,
  });
}

export function AnswerCitations({conversationId,accessVersion,children}:Source & {children:ReactNode}) {
  const [selected,select]=useState<{thread:string;id:string}>();
  const source={conversationId,accessVersion};
  return <SourceContext.Provider value={{...source,open:id=>conversationId && select({thread:conversationId,id})}}>
    {children}
    {selected && selected.thread===conversationId && createPortal(<CitationDialog key={JSON.stringify([conversationId,accessVersion,selected.id])}
      source={source} id={selected.id} close={()=>select(undefined)}/>,document.body)}
  </SourceContext.Provider>;
}

export function AnswerCitationLink({id,fallback}:{id:string;fallback?: (id:string)=>void}) {
  const {t}=useUiLanguage();
  const source=useContext(SourceContext);
  const button=useRef<HTMLButtonElement>(null);
  const [visible,setVisible]=useState(false);
  useEffect(()=>{
    const observer=new IntersectionObserver(([entry])=>setVisible(entry.isIntersecting),{rootMargin:"100px"});
    if(button.current)observer.observe(button.current);
    return ()=>observer.disconnect();
  },[]);
  const header=useHeader(source??{},id,visible);
  const result=header.error ? undefined : header.data;
  const state=result?.status;
  const wording=result && state!=="completed" ? label(result) : header.error ? "引用未能核对" : undefined;
  const partial=result?.partial?" · "+t("部分结果"):"";
  const title=wording ? t(wording)+partial+" · "+t("打开引用记录") : t("打开引用记录");
  return <button ref={button} type="button" className="answer-citation" data-citation={id} data-status={state}
    data-marked={!!wording || undefined} aria-label={title} title={title} aria-haspopup="dialog"
    onClick={()=>source?.conversationId ? source.open(id) : fallback?.(id)}>
    <UiIcon name="sources"/>{wording && <span>{t(wording)}{partial && <span className="citation-partial">{partial}</span>}</span>}
  </button>;
}

function CitationDialog({source,id,close}:{source:Source;id:string;close:()=>void}) {
  const {t}=useUiLanguage();
  const dialog=useRef<HTMLDialogElement>(null);
  useLayoutEffect(()=>{
    const element=dialog.current!;
    element.showModal();
    return ()=>element.close();
  },[]);
  const header=useHeader(source,id,true,true);
  const bound=header.error ? undefined : header.data;
  const [positions,setPositions]=useState<(number|"cited")[]>(["cited"]);
  const after=positions.at(-1)!;
  const page=useQuery<LogPage>({
    queryKey:["answer-citation-log",source.conversationId,source.accessVersion,id,bound?.revision,bound?.status,after],
    queryFn:()=>api(path(source.conversationId!,id)+"?revision="+queryString(bound!.revision)+"&after="+after),
    enabled:!!bound && !!source.accessVersion, retry:false,gcTime:0,
    // The small header rechecks access while open. Only a live page needs another log read.
    refetchInterval:pending(bound?.status) ? 2000 : false,
  });
  const log=!page.error && bound && page.data?.actionId===bound.actionId && page.data?.runId===bound.runId ? page.data : undefined;
  const problem=header.error || page.error;
  return <dialog ref={dialog} className="citation-backdrop" aria-label={t("引用记录")}
    onClick={event=>{if(event.target===event.currentTarget)close();}} onCancel={event=>{event.preventDefault();close();}}>
    <section className="citation-panel" data-citation-action={bound?.actionId} data-citation-run={bound?.runId}>
      <header><h2>{t("引用记录")}</h2><button onClick={close} aria-label={t("关闭")}><UiIcon name="close"/></button></header>
      {!source.accessVersion ? <p role="status">{t("暂时无法核对读取权限。")}</p>
        : problem ? <p role="alert">{problem.message} <button onClick={()=>{void header.refetch();if(bound)void page.refetch();}}>{t("重试")}</button></p>
        : !log ? <p role="status">{t("正在读取…")}</p> : <>
          <p className="citation-result" data-status={log.status}>{t(label(log))}
            {typeof log.exitCode==="number" && <small>{t("退出码 {n}",{n:log.exitCode})}</small>}</p>
          {typeof log.counts.passed==="number" && typeof log.counts.failed==="number" &&
            <p>{t(log.status==="timeout"?"超时前记录了 {passed} 项通过、{failed} 项失败":"已记录 {passed} 项通过、{failed} 项失败",
              {passed:log.counts.passed,failed:log.counts.failed})}
              {!!log.counts.errors && " · "+t("{n} 项出错",{n:log.counts.errors})}
              {!!log.counts.skipped && " · "+t("{n} 项跳过",{n:log.counts.skipped})}</p>}
          {log.command && <pre className="citation-command" aria-label={t("实际命令")}>$ {log.command}</pre>}
          {log.reason && <p>{log.reason}</p>}
          {log.text ? <pre key={log.after} className="citation-output" tabIndex={0} aria-label={t("工具调用日志")}>{log.text}</pre>
            : <p>{t("这次运行没有可显示的日志。")}</p>}
          {log.notice && <small>{log.notice}</small>}
          {(log.after>0 || log.hasMore || positions.length>1) && <nav>
            {log.after>0 && <button onClick={()=>setPositions([0])}>{t("日志开头")}</button>}
            {positions.length>1 && <button onClick={()=>setPositions(old=>old.slice(0,-1))}>{t("上一页")}</button>}
            {log.hasMore && log.next>log.after && <button onClick={()=>setPositions(old=>[...old,log.next])}>{t("下一页")}</button>}
          </nav>}
        </>}
    </section>
  </dialog>;
}
