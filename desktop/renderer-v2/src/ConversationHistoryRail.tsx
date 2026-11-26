// 提供轮次历史的索引和跳转请求，由会话视图负责切页及滚动定位。
// 主要引用方包括 `AgentHome.tsx`。
import { useUiLanguage } from "./UiLanguage";
import {useEffect, useRef, useState, type ReactNode, type RefObject} from "react";
import {useQuery} from "@tanstack/react-query";
import {api, queryString} from "./client";
import {RetainedDialog} from "./RetainedDialog";

// 后端 `progress.py` 提供轮次摘要。
// `started` 是 Unix 秒，`offset` 和 `before` 是跳转原对话消息所需的历史位置。
export type HistoryRound = {
  id: string; position: number; started: number | null; goal: string;
  result: string | null; blocker: string | null; outcome: string | null;
  active: boolean; offset: number | null; before: number | null;
};
// `nextBefore` 只能沿本会话的轮次接口翻页。`catchingUp` 表示进度索引仍在追赶原记录。
export type RoundPage = {rounds: HistoryRound[]; nextBefore: number | null; catchingUp: boolean};
const status: Record<string, string> = {running:"进行中", preparing:"准备中", stopping:"正在停止", waiting_approval:"等待批准", needs_human:"等待回应", success:"已完成", completed:"已完成", failed:"未完成", paused:"已暂停", interrupted:"已停止"};
const time = (stamp: number | null) => stamp ? new Date(stamp * 1000).toLocaleString() : "时间未记录";
export const roundRoute = (id: string) => `/api/v2/conversations/threads/${queryString(id)}/progress/rounds`;

export function ConversationHistoryRail({conversation, feed, revision, running, onJump, onLatest, progress, menuOpen, onCloseMenu}: {
  conversation: string; feed: RefObject<HTMLElement | null>; revision: string; running: boolean;
  onJump: (round: HistoryRound) => void; onLatest: () => void; progress: (close: () => void) => ReactNode;
  menuOpen: boolean; onCloseMenu: () => void;
}) {
  const { t } = useUiLanguage();
  const [older, setOlder] = useState<RoundPage[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [current, setCurrent] = useState("");
  const [hover, setHover] = useState<{round: HistoryRound; y: number}>();
  const [showProgress, setShowProgress] = useState(false);
  const latest = useQuery({queryKey:["history-rounds",conversation],queryFn:()=>api<RoundPage>(roundRoute(conversation)),
    refetchInterval:running ? 1400 : 4500,refetchIntervalInBackground:running});
  const records = new Map([...older.flatMap(page=>page.rounds),...latest.data?.rounds ?? []].map(round=>[round.id,round]));
  const rounds = [...records.values()].sort((a,b)=>a.position-b.position);
  const next = (older.at(-1) ?? latest.data)?.nextBefore;
  const track = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const focusedRail = useRef(false);
  const progressOpener = useRef<HTMLElement | null>(null);
  function revealCurrent(){
    const scroller=list.current;
    const mark=scroller?.querySelector<HTMLElement>('[aria-current="true"]');
    if(!scroller?.clientWidth||!mark)return;
    const bounds=scroller.getBoundingClientRect(), target=mark.getBoundingClientRect();
    if(target.top<bounds.top)scroller.scrollTop-=bounds.top-target.top;
    else if(target.bottom>bounds.bottom)scroller.scrollTop+=target.bottom-bounds.bottom;
  }
  useEffect(revealCurrent,[current,latest.data]);
  function focusView(){document.querySelector<HTMLElement>('[data-app-menu="view"]')?.focus({preventScroll:true});}
  useEffect(()=>{
    if(!track.current)return;
    const observer=new ResizeObserver(()=>{
      if(track.current?.clientWidth){revealCurrent();return;}
      setHover(undefined);
      if(focusedRail.current&&(document.activeElement===document.body||track.current?.contains(document.activeElement))){focusedRail.current=false;focusView();}
    });
    observer.observe(track.current);
    return()=>observer.disconnect();
  },[]);
  useEffect(()=>{
    const node=feed.current;
    if(!node) return;
    let frame=0;
    function update(){
      cancelAnimationFrame(frame);
      frame=requestAnimationFrame(()=>{
        const top=node!.getBoundingClientRect().top;
        const anchor=[...node!.querySelectorAll<HTMLElement>("[data-history-run]")].find(el=>el.getClientRects().length&&el.getBoundingClientRect().bottom>top+8);
        setCurrent(anchor?.dataset.historyRun ?? "");
      });
    }
    update();node.addEventListener("scroll",update,{passive:true});
    return()=>{cancelAnimationFrame(frame);node.removeEventListener("scroll",update);};
  },[feed,revision]);
  async function loadOlder(){
    if(!next||loading) return;
    setLoading(true);setError("");
    try {const page=await api<RoundPage>(roundRoute(conversation)+"?before="+next);setOlder(old=>old.length?[...old,page]:latest.data?[latest.data,page]:[page]);}
    catch(e){setError((e as Error).message);}
    finally{setLoading(false);}
  }
  function preview(round:HistoryRound, button:HTMLButtonElement){
    setHover({round,y:button.getBoundingClientRect().top-(track.current?.getBoundingClientRect().top ?? 0)});
  }
  function closeMenu(){onCloseMenu();requestAnimationFrame(focusView);}
  function openProgress(fromMenu:boolean){
    progressOpener.current=fromMenu?null:document.activeElement as HTMLElement;
    if(fromMenu)onCloseMenu();
    setHover(undefined);setShowProgress(true);
  }
  function closeProgress(){
    setShowProgress(false);
    requestAnimationFrame(()=>{
      const opener=progressOpener.current;
      if(opener?.isConnected&&opener.getClientRects().length)opener.focus({preventScroll:true});
      else focusView();
    });
  }
  return <div className="history-rail" ref={track} onPointerLeave={()=>setHover(undefined)} onBlur={e=>{if(!e.currentTarget.contains(e.relatedTarget))setHover(undefined);}}>
    <nav aria-label="对话历史轮次" className="history-rail-marks" onFocus={()=>{focusedRail.current=true;}} onBlur={e=>{if(e.relatedTarget&&!e.currentTarget.contains(e.relatedTarget))focusedRail.current=false;}}>
      {!!next&&<button className="history-rail-control" aria-label="加载更早轮次" title="加载更早轮次" disabled={loading} onClick={loadOlder}>···</button>}
      <div className="history-rail-scroll" ref={list} onScroll={()=>setHover(undefined)}>{rounds.map(round=><button key={round.id} type="button" className="history-rail-mark" data-active={round.active||undefined}
        aria-current={current===round.id?"true":undefined} aria-label={`${time(round.started)} ${round.goal}`}
        onPointerEnter={e=>preview(round,e.currentTarget)} onFocus={e=>preview(round,e.currentTarget)} onClick={()=>{setHover(undefined);onJump(round);}}><i aria-hidden="true"/></button>)}</div>
      {rounds.length>0&&<button className="history-rail-control" aria-label="回到最新轮次" title="回到最新轮次" onClick={onLatest}>↓</button>}
      {rounds.length>0&&<button className="history-rail-control" aria-label="本轮角色进度" title="本轮角色进度" onClick={()=>openProgress(false)}>≡</button>}
      {latest.error&&<button className="history-rail-control" aria-label="重试加载轮次" title="重试加载轮次" onClick={()=>latest.refetch()}>↻</button>}
    </nav>
    {error&&<span className="history-rail-note" role="alert">{error}</span>}
    {hover&&<div className="history-rail-preview" role="tooltip" style={{top:`clamp(0px, ${hover.y}px, max(0px, 100% - 130px))`}}>
      <small>{time(hover.round.started)} · {status[hover.round.outcome||""] ?? (hover.round.active?"进行中":"结果待核对")}</small>
      <strong>{hover.round.goal || "原问题未保留"}</strong><span>{hover.round.blocker || hover.round.result || (hover.round.active?"等待本轮进展":"暂无保留结果")}</span>
    </div>}
    <RetainedDialog open={menuOpen} label={t("对话历史")} onClose={closeMenu} compact>
      <div className="history-menu-actions">
        <button type="button" onClick={()=>{closeMenu();onLatest();}}>回到最新轮次</button>
        <button type="button" onClick={()=>openProgress(true)}>本轮角色进度</button>
      </div>
      {latest.isPending&&<p role="status">正在读取轮次…</p>}
      {latest.error&&<p role="alert">{latest.error.message} <button onClick={()=>latest.refetch()}>{t("重试")}</button></p>}
      {latest.isSuccess&&!rounds.length&&<p>暂无可定位的轮次。</p>}
      <ol className="history-menu-list">{[...rounds].reverse().map(round=><li key={round.id}>
        <button type="button" aria-current={current===round.id?"true":undefined} onClick={()=>{closeMenu();onJump(round);}}>
          <small>{time(round.started)} · {status[round.outcome||""]??"结果待核对"}</small>
          <strong>{round.goal || "原问题未保留"}</strong>
          <span>{round.blocker || round.result || (round.active?"等待本轮进展":"暂无保留结果")}</span>
        </button>
      </li>)}</ol>
      {error&&<p role="alert">{error}</p>}
      {!!next&&<button type="button" disabled={loading} onClick={loadOlder}>加载更早轮次</button>}
    </RetainedDialog>
    <RetainedDialog open={showProgress} label="本轮角色进度" onClose={closeProgress} compact>{progress(closeProgress)}</RetainedDialog>
  </div>;
}
