// 将会话事件渲染为正文、工具过程、证据和轮次变更。
// 主要引用方包括 `AgentHome.tsx`。
import { useUiLanguage } from "./UiLanguage";
import type { UiTranslator } from "./uiMessages";
import {batchSummary, groupConversationSteps, timelineToolStatus, type ToolStatus} from "./toolActivityGroups";
import {Fragment,useCallback,useEffect,useLayoutEffect,useMemo,useRef,useState,type ReactNode} from "react";
import {createPortal} from "react-dom";
import {UiIcon} from "./UiIcon";
import {useApplicationSettings} from "./ApplicationSettings";
import {StreamingMessage} from "./StreamingMessage";
import {AnswerCitations} from "./AnswerCitation";
import { Message } from "./Panels";
import { parentEvent, projectConversation, roleLabel, type ConversationItem } from "./conversationProjection";
import type { EventRow,ConversationScope,EvidenceOutput,MessageAttachment } from "./generated";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api,queryString } from "./client";
import { InlineCodeChanges,type ReviewLocation } from "./InlineCodeChanges";
import { RunChangeCard } from "./RunChanges";
import type { ActionReview } from "./ActionFileReview";
import { ConversationImageCard } from "./ConversationImageCard";
import { MessageArtifacts, type OpenArtifact } from "./MessageArtifact";
import {StepDisclosure} from "./StepDisclosure";
import {MessageAttachments} from "./ComposerInputs";
import {QueuedFollowups} from "./QueuedFollowups";
import {InputBudgetDetails} from "./InputBudgetDetails";

// 思考正文由运行投影传入；跟随其局部滚动，展开期间保留读者的位置。
function ThoughtBlock({text,revealing,streaming,settle,onRevealed}:{text:string;revealing:boolean;streaming:boolean;settle:boolean;onRevealed?:()=>void}) {
  const { t } = useUiLanguage();
  const viewport=useRef<HTMLDivElement>(null), body=useRef<HTMLDivElement>(null);
  const follow=useRef(true);
  useEffect(()=>{
    const view=viewport.current, content=body.current;
    if(!view||!content)return;
    const observer=new ResizeObserver(()=>{
      if(revealing&&follow.current&&view.clientHeight)view.scrollTop=view.scrollHeight;
    });
    observer.observe(content);
    return ()=>observer.disconnect();
  },[revealing]);
  return <div ref={viewport} className="thought-block" role="region" tabIndex={0} aria-label={t("思考内容")}
    onScroll={e=>{const v=e.currentTarget;follow.current=v.scrollHeight-v.clientHeight-v.scrollTop<=16;}}>
    <div ref={body}><StreamingMessage text={text} streaming={streaming} reveal={revealing}
      immediate={!revealing} settle={settle} reasoning onRevealed={onRevealed}/></div>
  </div>;
}

function CallIcon({label, icon, activate}: {
  label: string; icon: "search" | "log-end"; activate: () => void;
}) {
  return <button type="button" title={label} aria-label={label}
    onKeyDown={event=>{
      if(event.key!=="Enter"&&event.key!==" ")return;
      event.preventDefault();
      event.stopPropagation();
      if(!event.repeat)activate();
    }}
    onClick={event=>{event.preventDefault();event.stopPropagation();activate();}}>
    <UiIcon name={icon}/>
  </button>;
}

// 协调同一工具记录的展开和输出定位，把原证据跳转留给上层。
function CallDetails({open, onToggle, title, status, running, evidence, onEvidence, children}: {
  open: boolean; onToggle: (open: boolean) => void; title: string; status: ToolStatus; running: boolean;
  evidence?: string; onEvidence: () => void; children: (tail: HTMLSpanElement | null, showThought:boolean) => ReactNode;
}) {
  const [tail, setTail] = useState<HTMLSpanElement | null>(null);
  return <StepDisclosure className="step-disclosure" open={open} onOpen={onToggle}
    summary={<summary aria-expanded={open}>
      <span className="tool-call-title" title={title}><span className="conversation-step-label" data-running={running||undefined}>{title}</span></span>
      <span className="tool-call-actions">
        <span ref={setTail}/>
        {evidence && <CallIcon label={evidence} icon="search" activate={onEvidence}/>}
      </span>
      {status.label && <small className="tool-call-status" data-tone={status.tone}>{status.label}</small>}
    </summary>}>
    {present=><div className="tool-call-body">{children(tail,present)}</div>}
  </StepDisclosure>;
}

// 按会话、动作和当前读取范围加载工具原输出；轮询及滚动跟随与实际运行态绑定。
function ToolCallLog({conversationId, actionId, accessVersion, running, tail}: {
  conversationId: string; actionId: string; accessVersion: string; running: boolean; tail: HTMLSpanElement | null;
}) {
  const { t } = useUiLanguage();
  const viewport = useRef<HTMLPreElement>(null);
  const follow = useRef(true);
  const [away, setAway] = useState(false);
  const evidencePath = `/api/v2/conversations/threads/${queryString(conversationId)}/evidence/${queryString(actionId)}`;
  // The action may depend on another conversation's live grant, which is
  // not represented in this conversation's own scope revision.
  const access = useQuery<{ review: ActionReview }>({
    queryKey: ["tool-call-log-access", conversationId, actionId, accessVersion],
    queryFn: () => api(evidencePath),
    enabled: !running,
    refetchInterval: 3000,
    refetchIntervalInBackground: true,
    gcTime: 0,
  });
  const allowed = running || (!!access.data && !access.error && !access.data.review.restricted);
  const output = useInfiniteQuery({
    queryKey: ["tool-call-log", conversationId, actionId, accessVersion, running],
    initialPageParam: 0,
    queryFn: ({pageParam}) => api<EvidenceOutput>(
      `${evidencePath}/output?format=readable&live=${running ? 1 : 0}&after=${pageParam}`
    ),
    getNextPageParam: page => page.hasMore ? page.next : undefined,
    refetchInterval: running ? 1500 : false,
    enabled: allowed,
    gcTime: 0,
  });
  const pages = allowed && !output.error ? output.data?.pages : undefined;
  const page = pages?.at(-1);
  const text = pages?.map(part => part.text).join("");
  // 重新将本条工具输出的阅读锚点固定到最新内容。
  const toLatest = () => {
    const log = viewport.current;
    if (log) log.scrollTop = log.scrollHeight;
    follow.current = true;
    setAway(false);
  };
  // 记录用户是否仍位于工具流末尾，为续页后滚动行为提供依据。
  const updatePosition = () => {
    const log = viewport.current;
    if (!log) return;
    follow.current = log.scrollHeight - log.clientHeight - log.scrollTop < 12;
    setAway(!follow.current && log.scrollHeight > log.clientHeight);
  };
  useLayoutEffect(() => {
    if (running && follow.current) toLatest();
    updatePosition();
  }, [text, running]);
  useEffect(()=>{
    if (!viewport.current) return;
    const observer = new ResizeObserver(updatePosition);
    observer.observe(viewport.current);
    return ()=>observer.disconnect();
  },[!!text]);
  const terminal=page?.terminal;
  const endLabel = t(output.hasNextPage ? "到已加载末尾" : "回到日志末尾");
  return <section className={terminal ? "tool-call-log shell-transcript" : "tool-call-log"} aria-label={terminal ? "Shell" : t("调用日志")}>
    <h4>{terminal ? "Shell" : t("调用日志")}</h4>
    {terminal&&<pre className="shell-command" aria-label={t("实际命令")}>{terminal.command ? "$ "+terminal.command : t("这条旧记录没有保留命令。")}</pre>}
    {!running && access.error && <p role="alert">{access.error.message} <button onClick={() => access.refetch()}>{t("重试")}</button></p>}
    {!running && access.data?.review.restricted && <p role="alert">{access.data.review.reason || t("这次调用的资料已不在读取范围内。")}</p>}
    {(allowed ? output.isPending : access.isPending) && <small role="status">{t("正在读取工具输出…")}</small>}
    {allowed && output.error && <p role="alert">{output.error.message} <button onClick={() => output.refetch()}>{t("重试")}</button></p>}
    {text && <pre ref={viewport} tabIndex={0} aria-label={t("工具调用日志")}
      onScroll={updatePosition}>{text}</pre>}
    {page && !page.complete && <small>{running && !text ? t("执行中，等待可读取的输出。") : page.reason}</small>}
    {page?.complete && !text && <small>{t("工具没有文本输出。")}</small>}
    {terminal&&<footer><span>{terminal.timedOut ? t("已超时") : terminal.error}</span>
      <span>{typeof terminal.exitCode==="number" ? t("退出码 {n}",{n:terminal.exitCode}) : running ? t("执行中") : t("未记录退出码")}</span></footer>}
    {pages && output.hasNextPage && <button disabled={output.isFetchingNextPage} onClick={() => output.fetchNextPage()}>{t("加载更多输出")}</button>}
    {text && tail && away && createPortal(<CallIcon label={endLabel} icon="log-end" activate={toLatest}/>,tail)}
  </section>;
}

type StepGroup = ReturnType<typeof groupConversationSteps>[number];

// The final message can own Markdown split by a later thought in the source
// events. Extract that thought as well, without crossing another visible row.
function completedProcess(groups: StepGroup[], ready: (item: ConversationItem) => boolean) {
  let reply = groups.length-1;
  while (reply>=0 && !groups[reply].items[0].roleSummary) reply--;
  if (reply < 0 || !ready(groups[reply].items[0])) return;
  let start = reply;
  while (start>0) {
    const item = groups[start-1].items[0];
    if (item.kind==="error" || item.event.act==="ask_human" || item.event.act==="needs_human") break;
    start--;
  }
  let end = reply+1;
  while (groups[end]?.items[0].thoughtOnly) end++;
  if (end>reply+1 || groups.slice(start,reply).some(group=>group.items.some(item=>
    item.kind==="activity" || item.event.toolPrelude===true)))
    return {start,reply,end};
}

function processDuration(start: EventRow | undefined, end: EventRow | undefined, t: UiTranslator) {
  if (typeof start?.at!=="number" || typeof end?.at!=="number") return;
  const seconds = Math.floor(end.at-start.at);
  if (!Number.isFinite(seconds) || seconds<0) return;
  const hours = Math.floor(seconds/3600);
  const minutes = Math.floor(seconds/60)%60;
  return [hours && t("{n} 小时",{n:hours}), minutes && t("{n} 分",{n:minutes}), t("{n} 秒",{n:seconds%60})].filter(Boolean).join(" ");
}

export type PendingUserMessage = {
  id: string;
  text: string;
  submittedAt: number;
  runId?: string;
  attachments?: readonly MessageAttachment[];
};

export function PendingMessageBubble({message,conversationId}: {message:PendingUserMessage; conversationId?:string}) {
  const {t} = useUiLanguage();
  const {reducedMotion} = useApplicationSettings();
  const body = useRef<HTMLDivElement>(null);
  useLayoutEffect(()=>{
    const elapsed = Math.max(0,Date.now()-message.submittedAt);
    if (reducedMotion || !body.current || elapsed>=220) return;
    const animation = body.current.animate([
      {transform:"translateY(10px)",opacity:0.65},
      {transform:"translateY(0)",opacity:1},
    ],{duration:220,easing:"cubic-bezier(.2,.75,.32,1)"});
    animation.currentTime = elapsed;
    return ()=>animation.cancel();
  },[message.id,message.submittedAt,reducedMotion]);
  return <article className="conversation-item message" data-role="user" data-pending-message>
    <div className="msg user">
      <span className="sr-only">{t("正在发送消息")}</span>
      <div className="body md" ref={body}>
        {conversationId && !!message.attachments?.length &&
          <MessageAttachments conversationId={conversationId} items={message.attachments}/>}
        <Message text={message.text}/>
      </div>
    </div>
  </article>;
}

export function ConversationTimeline({ events, nativeRuns, importRecovery, onEvidence, onDiscuss, active,runId,runStatus,conversationId,onReview,onSummary,onSources,onArtifact,pendingMessage,onMessageSettled,showFollowups }: {
  events: EventRow[];
  nativeRuns: ReadonlySet<string>;
  importRecovery?: {runId: string; actionId: string; panel: ReactNode};
  onEvidence: (actionId: string | null, runId?: string) => void;
  onDiscuss: (text: string) => void;
  active?: boolean;
  runId?: string;
  runStatus?: string;
  conversationId?:string;
  onReview?:(location:ReviewLocation)=>void;
  onSummary?:(role:string|undefined,runId?:string)=>void;
  onSources?:(run:string)=>void;
  onArtifact?:OpenArtifact;
  pendingMessage?: PendingUserMessage;
  showFollowups?: boolean;
  onMessageSettled?: (owner: string, messageId: string) => void;
}) {
  const { t } = useUiLanguage();
  const scope=useQuery<ConversationScope>({queryKey:["scope",conversationId],enabled:!!conversationId,
    queryFn:()=>api(`/api/v2/conversations/threads/${queryString(conversationId!)}/scope`),
    refetchInterval:3000,refetchIntervalInBackground:true});
  const {display:look,reducedMotion}=useApplicationSettings();
  const openedAt=useRef(Date.now());
  const [revealed,setRevealed]=useState<Map<string,string>>(()=>new Map());
  const [details,setDetails]=useState<{defaultLook:string;values:Map<string,boolean>}>(()=>({defaultLook:look.think,values:new Map()}));
  const [openBatches, setOpenBatches] = useState(() => new Set<string>());
  const shown=useRef(new Set<string>());
  const folded=useRef(new Map<string,string>());
  const items=useMemo(()=>projectConversation(events),[events]);
  const historicalCrews=useMemo(()=>new Set(events.filter(e=>parentEvent(e)&&
    (e.role==="planner"||e.role==="reviewer")).map(e=>e.run_id)),[events]);
  const isLive=(item:ConversationItem)=>!!runId && item.event.run_id === runId && parentEvent(item.event) &&
    typeof item.event.at==="number"&&item.event.at*1000>=openedAt.current;
  const isNative = (item:ConversationItem) => parentEvent(item.event) && !!item.event.run_id &&
    nativeRuns.has(item.event.run_id) && ["user","executor"].includes(item.role);
  const disclosureOpen=(id:string,defaultOpen:boolean)=>
    (details.defaultLook===look.think?details.values.get(id):undefined)??defaultOpen;
  const setDisclosure=(id:string,open:boolean)=>setDetails(old=>({
    defaultLook:look.think,values:new Map(old.defaultLook===look.think?old.values:[]).set(id,open),
  }));
  const detailOpen=(item:ConversationItem)=>disclosureOpen(item.id,
    !!item.thoughtOnly && look.think==="expand");
  const candidate=(item:typeof items[number])=>item.kind==="activity"
    ? look.think!=="hide"&&detailOpen(item)?item.explanation:undefined
    : item.kind==="message"&&item.role!=="user"&&look.output==="type"?item.text:undefined;
  // Preserve the playback order of recorded multi-role runs.
  const barrier=reducedMotion ? -1 : items.findIndex(item=>{
    const text=candidate(item);
    return !isNative(item)&&isLive(item)&&!!text&&revealed.get(item.id)!==text&&folded.current.get(item.id)!==text;
  });
  const head=items[barrier];
  const headId=head?.id,headText=head&&candidate(head);
  const rememberRevealed = useCallback((id: string, text: string) => {
    setRevealed(old => old.get(id) === text ? old : new Map(old).set(id, text));
  }, []);
  const finish=useCallback(()=>{
    if(headId&&headText!==undefined) rememberRevealed(headId, headText);
  },[headId,headText,rememberRevealed]);
  const visibleThrough = items.reduce((last, item, index) =>
    shown.current.has(item.id) && !isNative(item) ? Math.max(last, index) : last, barrier);
  const unfinishedThoughts = new Set<string>();
  if (!reducedMotion) {
    for (const item of items) {
      if (item.thoughtOnly && isNative(item) && isLive(item) && candidate(item) &&
          item.explanation !== revealed.get(item.id) && item.explanation !== folded.current.get(item.id))
        unfinishedThoughts.add(item.id);
    }
  }
  const waitingRuns = new Set<string>();
  const bodyLimits = new Map<string, number>();
  if (unfinishedThoughts.size) {
    for (const item of items) {
      const run = item.event.run_id;
      if (!run || !isNative(item) || !isLive(item)) continue;
      if (unfinishedThoughts.has(item.id)) waitingRuns.add(run);
      if (item.kind !== "message" || item.role === "user" ||
          item.event.act === "ask_human" || item.event.act === "needs_human") continue;
      const boundary = item.thoughtBoundaries?.find(part => unfinishedThoughts.has(part.thoughtId));
      if (waitingRuns.has(run)) bodyLimits.set(item.id, 0);
      else if (boundary && boundary.offset < item.text.length) bodyLimits.set(item.id, boundary.offset);
    }
  }
  const visible = items.filter((item, index) => bodyLimits.get(item.id) !== 0 &&
    (barrier < 0 || isNative(item) || index <= visibleThrough));
  const lastActiveRun=useRef<string>(undefined);
  useLayoutEffect(()=>{
    if(active&&runId)lastActiveRun.current=runId;
  },[active,runId]);
  const published = pendingMessage && visible.some(item => item.role==="user" && parentEvent(item.event) &&
    (pendingMessage.runId ? item.event.run_id===pendingMessage.runId :
      typeof item.event.at==="number" && item.event.at*1000>=pendingMessage.submittedAt &&
      item.text===pendingMessage.text));
  const settled = pendingMessage?.runId && pendingMessage.runId===runId && !active &&
    ["failed","interrupted","cancelled","stopped"].includes(runStatus??"");
  useLayoutEffect(()=>{
    if (pendingMessage && conversationId && (settled || pendingMessage.runId && published))
      onMessageSettled?.(conversationId,pendingMessage.id);
  },[pendingMessage,conversationId,published,settled,onMessageSettled]);
  const handoff=head&&(head.roleSummary||items.slice(barrier+1).some(item=>item.role!=="user"&&
    !isNative(item)&&(item.role!==head.role||item.event.run_id!==head.event.run_id)))?head:undefined;
  useLayoutEffect(()=>{
    onSummary?.(handoff?.role,handoff?.event.run_id??undefined);
  },[onSummary,handoff?.role,handoff?.event.run_id]);
  useLayoutEffect(()=>{
    const liveVisible=visible.filter(isLive);
    shown.current=new Set(liveVisible.filter(item=>!isNative(item)).map(item=>item.id));
    const visibleIds = new Set(liveVisible.map(item => item.id));
    for(const id of folded.current.keys())if(!visibleIds.has(id))folded.current.delete(id);
    for(const item of liveVisible){
      if(item.explanation&&(look.think==="hide"||!detailOpen(item)))folded.current.set(item.id,item.explanation);
    }
  },[items,barrier,look.think,details,nativeRuns]);
  let latestThought:string|undefined;
  if(active&&runId){
    for(let i=items.length-1;i>=0;i--){
      const item=items[i];
      if(item.event.run_id!==runId||!parentEvent(item.event))continue;
      if(item.thoughtOnly&&item.pendingExplanation)latestThought=item.id;
      break;
    }
  }
  function renderItem(item: ConversationItem, detailsVisible = true) {
    const e = item.event;
    if(item.thoughtOnly && look.think==='hide')return null;
    const inActiveRun = !!active && !!runId && e.run_id === runId;
    const streaming = !!item.streaming && inActiveRun;
    const action = typeof e.action_id === "string" ? e.action_id : null;
    const role = roleLabel(item.role);
    const label = parentEvent(e) && item.role === "executor" && !historicalCrews.has(e.run_id) ? "" : item.role === "user" ? t("你") : role === "执行者" || role === "规划者" || role === "测试者" ? t(role) : role;
    const open = detailOpen(item);
    const legacyRevealing = item.id === headId;
    const text = candidate(item);
    const nativeRevealing = !reducedMotion && isNative(item) && isLive(item) &&
      !!text && revealed.get(item.id) !== text && folded.current.get(item.id) !== text;
    const revealing = detailsVisible && (legacyRevealing || nativeRevealing);
    const settle = revealing && (!item.pendingExplanation && !item.streaming || legacyRevealing && barrier < items.length - 1);
    const onRevealed = legacyRevealing ? finish : nativeRevealing && text ? () => rememberRevealed(item.id, text) : undefined;
    const running = inActiveRun && item.kind==="activity" && (item.thoughtOnly ? item.id===latestThought :
      e.state==="running" || e.act==="context_compaction" && e.state==="preparing");
    if (e.eventCard) {
      const title = t(e.eventCard === "imageArtifact" ? "图片成果" : e.eventCard === "search" ? "网页搜索" : "查看图片");
      return <article key={item.id} data-conversation-item-id={item.id} data-history-run={e.run_id} data-history-offset={e.sourceOffset} className="conversation-event-card" data-event-card={String(e.eventCard)}>
        <header><span><UiIcon name={e.eventCard === "search" ? "browser" : "image"}/>
          <span className="conversation-step-label" data-running={running||undefined}>{title}</span></span><small>{label}</small>
          {action && <CallIcon label={t("审查这次调用")} icon="search" activate={()=>onEvidence(action,e.run_id??undefined)}/>}
        </header>
        {item.explanation && look.think!=="hide" && <StepDisclosure className="event-explanation" open={open} onOpen={next=>{
          setDisclosure(item.id,next);
          if (!next) setRevealed(old=>new Map(old).set(item.id,item.explanation!));
        }} summary={<summary aria-expanded={open}>{t("此前梳理")}</summary>}>
          {present=><div>{detailsVisible && present && <ThoughtBlock text={item.explanation!} revealing={revealing}
            streaming={false} settle={settle} onRevealed={onRevealed}/>}</div>}
        </StepDisclosure>}
        {e.eventCard === "imageHistory" ? <small>{t("原图未关联，历史预览不可用。")}</small> : e.eventCard === "search" ?
          e.run_id && onSources && <button onClick={()=>onSources(e.run_id!)}>{t("查看本轮来源")}</button> :
          detailsVisible && action && conversationId && <ConversationImageCard conversation={conversationId} action={action}
            version={scope.error ? undefined : scope.data?.revision} title={title} state={e.state} queued={e.imageQueued === true} onArtifact={onArtifact}/>}
      </article>;
    }
    const toolStatus = item.kind === "activity" ? timelineToolStatus(e) : undefined;
    const canStartChat = item.kind === "message" && item.role !== "user" &&
      !item.streaming && e.toolPreludeFallback !== true;
    return <article key={item.id} data-conversation-item-id={item.id} data-role={item.role} data-history-run={e.run_id} data-history-offset={e.sourceOffset}
      data-tool-prelude={e.toolPrelude===true||undefined}
      data-generated-tool-prelude={e.toolPreludeFallback===true||undefined}
      data-live-step={isLive(item)||undefined}
      data-role-summary={item.roleSummary||undefined} aria-label={streaming ? t("正在生成") : undefined} className={`conversation-item ${item.kind}`}>
      {item.kind === "activity" ? <CallDetails open={open} running={running}
        title={item.text}
        status={{label: [label,toolStatus && t(toolStatus.label)].filter(Boolean).join(" · "), tone: toolStatus?.tone}}
        evidence={!item.thoughtOnly ? action ? t("审查这次调用") : e.run_id ? t("查看本轮证据") : undefined : undefined}
        onEvidence={()=>onEvidence(action,e.run_id??undefined)} onToggle={next=>{
        if(next===open)return;
        setDisclosure(item.id,next);
        if(item.explanation)setRevealed(old=>new Map(old).set(item.id,item.explanation!));
      }}>
        {(tail,showThought)=><>
        {detailsVisible && showThought && item.explanation && look.think!=='hide' && <ThoughtBlock text={item.explanation}
          revealing={revealing} streaming={!!item.pendingExplanation&&inActiveRun} settle={settle} onRevealed={onRevealed}/>}
        {detailsVisible && !item.thoughtOnly && action && <>
          {open && typeof e.error === "string" && <p className="tool-call-error">{e.error}</p>}
          {open && conversationId && scope.data && !scope.error && <ToolCallLog key={scope.data.revision}
            conversationId={conversationId} actionId={action} accessVersion={scope.data.revision}
            running={e.state === "running" || (!!active && e.state === "waiting_approval")} tail={tail}/>}
        </>}
        {!item.thoughtOnly && !action && <p>{t("旧记录没有精确动作引用，可从本轮证据核对。")}</p>}
        {detailsVisible&&open&&action&&conversationId&&onReview&&scope.data&&!scope.error&&<InlineCodeChanges key={scope.data.revision} accessVersion={scope.data.revision} conversationId={conversationId} actionId={action} active={active} onReview={onReview}/>}
        </>}
      </CallDetails> : <>
        {item.role==='user'?<div className="msg user">
          <span className="sr-only">{label}</span><div className="body md">
            {conversationId&&!!e.attachments?.length&&<MessageAttachments conversationId={conversationId} items={e.attachments}/>}
            <Message text={item.text}/>
          </div>
        </div>:
          <div className="turn" data-role={item.role}><div className="msg agent">
            {item.role==='executor' ? <span className="sr-only">Agent</span> :
              <div className="mhead"><span className="mname">{label}{item.roleSummary?' · '+t('总结'):''}{typeof e.model==='string'?' · '+e.model:''}</span></div>}
            <div className="body md"><StreamingMessage text={item.text} streaming={streaming}
              reveal={revealing} immediate={!revealing} settle={settle} visibleUntil={bodyLimits.get(item.id)} onRevealed={onRevealed}
              onEvidence={id=>onEvidence(id)}/></div>
          </div></div>}
        {item.streaming && !active && e.run_id===runId && ["failed","interrupted","cancelled","stopped"].includes(runStatus??"") &&
          <small>{t("执行已停止，保留已收到的内容。")}</small>}
        {e.act === "say" && e.truncated === true && <small>{t("原记录只保留了部分正文。")}</small>}
        {item.kind === "error" && (e.act === "context_blocked" && e.inputBudget
          ? <InputBudgetDetails budget={e.inputBudget}/>
          : <p>{t("这一步遇到问题，后续结果请继续核对。")}</p>)}
        {item.role !== "user" && typeof e.t === "string" &&
          <div className="conversation-message-footer"><span className="ts">{e.t}</span></div>}
        {(canStartChat || action) && <div className="conversation-message-side">
          {canStartChat && <button type="button" title={t("用这段回复开新会话")} aria-label={t("用这段回复开新会话")}
            onClick={() => onDiscuss(item.text)}><UiIcon name="compose"/></button>}
          {action && <CallIcon label={t("查看这一步的证据")} icon="search" activate={() => onEvidence(action,e.run_id??undefined)}/>}
        </div>}
      </>}
    </article>;
  }
  const groups = groupConversationSteps(visible.filter(item=>!item.thoughtOnly||look.think!=="hide"),
    item=>look.think!=="expand"&&isNative(item));
  const outcomes = new Map<string,string>();
  const starts = new Map<string,EventRow>();
  const ends = new Map<string,EventRow>();
  for(const event of events) {
    if(!event.run_id || !parentEvent(event))continue;
    if((event.act==="run_config" || event.act==="execution_config" || event.act==="user") && !starts.has(event.run_id))
      starts.set(event.run_id,event);
    if(event.act==="end" && (!event.role||event.role==="executor") &&
      (nativeRuns.has(event.run_id) || !(active && event.run_id===runId))) {
      outcomes.set(event.run_id,String(event.status||"completed"));
      ends.set(event.run_id,event);
    }
  }
  if(!active&&runId&&runStatus&&(["failed","interrupted"].includes(runStatus)||runStatus==="completed"&&!outcomes.has(runId)))
    outcomes.set(runId,runStatus);
  const cardAnchors = new Map<string,{fallback:string;summary?:string}>();
  for(const group of groups) {
    const item=group.items.at(-1)!;
    const run=item.event.run_id;
    if(!run||!outcomes.has(run))continue;
    const previous=cardAnchors.get(run);
    cardAnchors.set(run,{fallback:group.items[0].id,summary:item.role==="executor"&&item.roleSummary?group.items[0].id:previous?.summary});
  }
  const cards=new Map([...cardAnchors].map(([run,anchor])=>[anchor.summary||anchor.fallback,run]));
  const segments: {scope?:string; role?:string; groups:typeof groups; key:string}[] = [];
  for(const group of groups){
    const first = group.items[0], event = first.event;
    // The projection also supplies the role for older executor records.
    const role = ["planner","executor","reviewer"].includes(first.role)
      ? first.role : undefined;
    const scope = role ? JSON.stringify([event.run_id??null,role,event.childId||event.agent_id]) : undefined;
    const previous = segments.at(-1);
    if(scope && previous?.scope===scope) previous.groups.push(group);
    else segments.push({scope,role:scope?role:undefined,groups:[group],key:scope ?? first.id});
  }
  // Prepending a page can change the first event of a run. Keep its rail and
  // disclosure mounted. A following boundary distinguishes earlier segments
  // when a user message or a different role interrupts the same run.
  const laterScopes = new Set<string>();
  for (let i=segments.length-1;i>=0;i--) {
    const segment=segments[i];
    if (!segment.scope) continue;
    if (laterScopes.has(segment.scope)) segment.key += ":"+segments[i+1].groups[0].items[0].id;
    laterScopes.add(segment.scope);
  }
  // 共用同一批次的过程容器，并在可核对的轮次位置附上变更卡。
  function ownsImportRecovery(group: StepGroup) {
    return importRecovery && group.items.some(({event}) => event.run_id === importRecovery.runId &&
      event.action_id === importRecovery.actionId && event.tool === "import_private_repository" &&
      event.state === "failed" && parentEvent(event));
  }
  function renderCard(group: StepGroup) {
    const run=cards.get(group.items[0].id);
    return run&&conversationId&&onReview?<RunChangeCard key={run} conversationId={conversationId} runId={run} status={outcomes.get(run)!}
      accessVersion={scope.error?undefined:scope.data?.revision} onReview={onReview}/>:null;
  }
  function renderGroup(group: StepGroup, detailsVisible = true, withCard = true) {
    const first = group.items[0];
    const card=withCard?renderCard(group):null;
    if (!group.batch) return <Fragment key={first.id}>{renderItem(first,detailsVisible)}{card}</Fragment>;
    const open = openBatches.has(group.id);
    const calls=group.items.filter(item=>item.kind==="activity");
    const summary = batchSummary(calls, !!active && first.event.run_id === runId,t);
    const labels = [...new Set(calls.map(item => item.text))];
    const title = summary.running?.text ??
      (labels.length ? labels.slice(0, 2).join(" · ") : t("步骤说明"));
    const count = calls.length > 1 ? t("{n} 步", {n:calls.length}) : "";
    const block=<StepDisclosure className="tool-activity-group tool-step-batch" key={group.id} open={open} live={isLive(first)}
      onOpen={expanded=>{
        setOpenBatches(old=>{const next=new Set(old); if(expanded)next.add(group.id);else next.delete(group.id);return next;});
      }}
      summary={<summary aria-expanded={open} data-conversation-item-id={group.id+":summary"} data-history-run={first.event.run_id} data-history-offset={first.event.sourceOffset}><span><UiIcon name="activity"/><span className="conversation-step-label" data-running={!!summary.running&&!open||undefined}>{title}</span>
        {count && <span className="tool-batch-status">{" · "}{count}</span>}
        {summary.statuses.map(status => <span key={status.label} className="tool-batch-status" data-tone={status.tone}>{" · "}{status.label}</span>)}
      </span></summary>}>
      {present=><ol>{group.items.map(item => <li key={item.id}>
        {renderItem(item, detailsVisible && present)}
      </li>)}</ol>}
    </StepDisclosure>;
    return <Fragment key={group.id}>{block}{card}</Fragment>;
  }
  function renderSegment(segment: typeof segments[number]) {
    const first = segment.groups[0].items[0];
    if (!segment.role) return renderGroup(segment.groups[0]);
    const run = first.event.run_id;
    const success = !!run && ["success","completed"].includes(outcomes.get(run) ?? "");
    const process = success && !waitingRuns.has(run!) && !(active&&run===runId) && first.role==="executor" && parentEvent(first.event)
      ? completedProcess(segment.groups,item=>!item.streaming &&
        (look.output!=="type" || reducedMotion || !isLive(item) || revealed.get(item.id)===item.text))
      : undefined;
    const renderProcess = (steps: StepGroup[], id: string, duration: string | undefined, completed: boolean) => {
      const readingStep=steps.some(group=>openBatches.has(group.id)||group.items.some(item=>disclosureOpen(item.id,false)));
      const open = disclosureOpen(id,!completed&&(look.think==="expand"||readingStep));
      const failed=steps.flatMap(group=>group.items).filter(item=>item.event.state==="failed").length;
      return <Fragment key={id}><StepDisclosure className="conversation-process" open={open} foldOnMount={run===runId&&lastActiveRun.current===run}
          onOpen={value=>setDisclosure(id,value)} summary={<summary aria-expanded={open} data-conversation-item-id={id} data-history-run={run} data-history-offset={steps[0].items[0].event.sourceOffset}><span>{t("执行过程")}</span>
            {duration && <span className="conversation-process-duration">· {t("用时 {duration}",{duration})}</span>}
            {failed>0 && <span className="tool-batch-status" data-tone="failed">· {t("{n} 步失败",{n:failed})}</span>}
            <span className="conversation-process-chevron" aria-hidden="true">›</span>
          </summary>}>
          {present=><div className="conversation-process-steps">{present ? steps.map(group=>renderGroup(group,true,false)) :
            steps.flatMap(group=>group.items.filter(item=>item.kind==="activity").map(item=>
              <span hidden key={item.id} data-conversation-item-id={item.id} data-event-card={item.event.eventCard || undefined}/>))}</div>}
        </StepDisclosure>{steps.map(renderCard)}</Fragment>;
    };
    const processKey = segment.key+":process";
    function renderProcessRows(groups: StepGroup[], hasCompleteRun = false) {
      // 完成答复发布后，把已标记的中途进度和工具步骤收进同一个过程。
      const blocks: {steps:StepGroup[]; following?:StepGroup}[] = [];
      let steps: StepGroup[] = [];
      for (const group of groups) {
        const canFold = group.items.every(item =>
          (item.kind==="activity" || item.event.toolPreludeFallback===true ||
            hasCompleteRun && item.event.toolPrelude===true) &&
          item.event.state!=="waiting_approval");
        if (canFold) steps.push(group);
        else {
          blocks.push({steps,following:group});
          steps=[];
        }
      }
      if (steps.length) blocks.push({steps});
      const foldedBlocks = blocks.filter(block=>block.steps.length);
      const lastSteps = foldedBlocks.at(-1);
      const duration = hasCompleteRun && foldedBlocks.length===1 && run
        ? processDuration(starts.get(run),ends.get(run),t) : undefined;
      const rows: ReactNode[] = [];
      for (const block of blocks) {
        if (block.steps.length) {
          const id = block===lastSteps ? processKey : processKey+":"+block.following!.id;
          rows.push(renderProcess(block.steps,id,duration,hasCompleteRun));
        }
        if (block.following) rows.push(renderGroup(block.following));
      }
      return rows;
    }
    let rows;
    if (process) {
      const {start,reply,end} = process;
      const steps = [...segment.groups.slice(start,reply),...segment.groups.slice(reply+1,end)];
      rows = [
        ...segment.groups.slice(0,start).map(group=>renderGroup(group)),
        ...renderProcessRows(steps,true),
        renderGroup(segment.groups[reply]),
        ...segment.groups.slice(end).map(group=>renderGroup(group)),
      ];
    } else if (run && segment.role==="executor" && parentEvent(first.event) && !(active && (!runId || run===runId)) && !waitingRuns.has(run)) {
      // The final say/end can be outside the reading window. Fold known
      // execution steps in place without treating the slice as a successful run.
      rows = renderProcessRows(segment.groups);
    } else rows = segment.groups.map(group=>renderGroup(group));
    // Keep the original receipt mounted when the completed run folds its tool steps.
    return <div key={segment.key} className="role-rail" data-rail-role={segment.role}>
      {rows}{importRecovery && segment.groups.some(ownsImportRecovery) && importRecovery.panel}
    </div>;
  }
  return <AnswerCitations conversationId={conversationId} accessVersion={scope.error?undefined:scope.data?.revision}>
    <MessageArtifacts conversationId={conversationId} open={onArtifact}><div className="conversation-timeline">
    {segments.map(renderSegment)}
    {showFollowups && conversationId && <QueuedFollowups key={conversationId} owner={conversationId} items={visible}/>}
    {pendingMessage && !published && !settled &&
      <PendingMessageBubble key={pendingMessage.id} message={pendingMessage} conversationId={conversationId}/>}
  </div></MessageArtifacts></AnswerCitations>;
}
