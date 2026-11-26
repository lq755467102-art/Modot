// 管理会话侧栏的页签状态，并组织工作区、资料、关联会话和子任务视图。
// 主要引用方包括 `AgentHome.tsx` 和 `BrowserPane.tsx`。
import { useUiLanguage } from "./UiLanguage";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, loadJSON, queryString, saveJSON, wsPath } from "./client";
import { HistoryControls } from "./HistoryControls";
import { Message, TerminalPane } from "./Panels";
import { LineCounts } from "./RunChanges";
import { UiIcon } from "./UiIcon";
import {browserAddress} from "./browserAddress";
import {useConversationScroll} from "./useConversationScroll";
import {useThreadReader} from "./useThreadReader";
import type { Conversation, GitState, ProcessRun, ProjectResource, RunChangeSummary, SubtaskOverview, Workspace } from "./generated";

export type SideTab = "start" | "changes" | "browser" | "agents" | "sources" | "git" | "terminal" | "environment" | "handoff" | `chat:${string}` | `link:${string}`;
// `AgentHome.tsx` 按会话持有该状态。
// `tabs` 记录已开页签，`active` 是当前页签，`collapsed` 只控制展开状态。
export type SideState = { tabs: SideTab[]; active?: SideTab; collapsed: boolean };
export const emptySideState: SideState = { tabs: [], collapsed: false };
export const sideLabels = { start:"新标签页", changes: "变更", browser: "浏览器", agents: "子 Agent", sources: "来源", git: "Git 操作", terminal: "终端", environment: "本地环境", handoff: "工作树" } as const;

export function SideStartPage({hasAgents,hasWorkspace,related,unread,onTab,onWeb,onWorkbench}:{
  hasAgents:boolean;hasWorkspace:boolean;related:Conversation[];unread:ReadonlySet<string>;
  onTab:(tab:SideTab)=>void;onWeb:(url:URL)=>void;onWorkbench:()=>void;
}) {
  const {t}=useUiLanguage();
  const [address,setAddress]=useState(""),[invalid,setInvalid]=useState(false);
  const shortcut=(tab:SideTab,label:string,icon:Parameters<typeof UiIcon>[0]["name"])=>
    <button key={tab} type="button" onClick={()=>onTab(tab)}><UiIcon name={icon}/><span>{label}</span>
      {unread.has(tab)&&<i className="shortcut-unread" aria-label={t("有未读更新")}/>}</button>;
  return <section className="side-start" aria-label={t("新标签页")}>
    <div><form onSubmit={e=>{e.preventDefault();const url=browserAddress(address);setInvalid(!url);if(url)onWeb(url);}}>
      <UiIcon name="browser"/><input autoFocus inputMode="url" spellCheck={false} value={address}
        aria-label={t("网页地址")} placeholder={t("输入网址或 localhost:端口")} aria-invalid={invalid||undefined}
        onChange={e=>{setAddress(e.target.value);setInvalid(false);}} required/>
      <button title={t("打开网页")} aria-label={t("打开网页")}><UiIcon name="forward"/></button>
    </form>
    {invalid&&<p role="alert">{t("请输入有效的 HTTP 或 HTTPS 网页地址")}</p>}
    <nav aria-label={t("快捷入口")}>
      {shortcut("changes",t("变更"),"code")}{shortcut("sources",t("来源"),"sources")}
      {hasWorkspace&&shortcut("terminal",t("终端"),"terminal")}
      {hasWorkspace&&<button onClick={onWorkbench}><UiIcon name="folder"/>{t("代码工作间")}</button>}
      {shortcut("browser",t("浏览器"),"browser")}
      {hasAgents&&shortcut("agents",t("子 Agent"),"agents")}
      {related.map(chat=>shortcut(`chat:${chat.id}`,chat.title,"compose"))}
    </nav></div>
  </section>;
}
// 浏览器摘录先进入会话草稿。`sent` 标明已有提交关联，列表可以区分尚在草稿与已发送的来源。
export type WebSource = { url: string; title: string; at: number; sent?: boolean };
// 从会话搜索回执提取的来源。
// `runId` 和 `tool` 保留发生轮次与工具，URL 本身不足以证明本轮曾读取。
export type SearchSource = { url: string; title: string; runId: string; tool: string };
const threadPath = (id: string) => "/api/v2/conversations/threads/" + queryString(id);
const activeStates = new Set(["queued", "preparing", "running", "stopping", "unknown"]);
export const agentReadKey=(task:SubtaskOverview["tasks"][number])=>
  JSON.stringify([task.id,task.state,task.summary,task.resultDelivery]);
const deliveryLabels = {available:"成果可读",notified:"已通知Modot",delivered:"成果已进入父上下文"};
// 任务执行状态与成果投递状态来自两项后端事实，分别显示。
function taskStatus(task: SubtaskOverview["tasks"][number]){
  const delivery=task.resultDelivery;
  return (states[task.state] || "待核对")+(delivery?" · "+deliveryLabels[delivery]:"");
}
const states: Record<string, string> = {
  queued: "排队中", preparing: "准备中", running: "进行中", stopping: "正在停止", unknown: "待核对",
  completed: "已完成", failed: "失败", blocked: "受阻", cancelled: "已取消", paused: "已暂停",
};

// 以父会话标识轮询子任务全局概述，供工具栏和详情共用。
export function useSubtaskOverview(id: string) {
  return useQuery({
    queryKey: ["subtask-overview", id],
    queryFn: () => api<SubtaskOverview>(threadPath(id) + "/subtasks/overview"),
    enabled: !!id,
    refetchInterval: 3000,
    refetchIntervalInBackground: true,
  });
}

// 按子任务身份稳定选色，列表重排或刷新后仍可辨认同一项。
function AgentAvatar({id}:{id:string}) {
  const palette=["violet","teal","amber","rose","blue","green"];
  const hash=Array.from(id).reduce((n,letter)=>Math.imul(n^letter.charCodeAt(0),16777619),2166136261)>>>0;
  const shapes=["M9 2 16 6v6l-7 4-7-4V6Z","M3 3h12l-6 6 6 6H3l6-6Z","M9 1l2 6 6 2-6 2-2 6-2-6-6-2 6-2Z","M9 2a7 7 0 1 0 0 14A7 7 0 1 0 9 2m0 0v14M2 9h14"];
  return <span className="subagent-avatar" data-avatar-color={palette[hash%palette.length]} aria-hidden="true"><svg width="16" height="16" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.4"><path d={shapes[Math.floor(hash/palette.length)%shapes.length]}/></svg></span>;
}

// `AgentHome.tsx` 提供当前运行、变更摘要和侧栏状态。
// 此处只计算展示与导航，将打开审查、定位子任务和读资料交给对应回调。
export function SideToolbar({ state, disabled, unread, projectName, summary, summaryUnavailable, rangeLabel,
  agents, currentRun, related, seen, sources, searchCount, hasWorkspace, hasWorktree, canSwitchWorkspace, onOpen, onAgent, onChanges, onToggle, onAddSource, onReadSources, trigger }: {
  state: SideState; disabled: boolean; unread: string; projectName: string;
  summary?: RunChangeSummary; summaryUnavailable: boolean; rangeLabel: string;
  agents?: SubtaskOverview; currentRun?:string; related: Conversation[]; seen: Record<string, number>; sources: number; searchCount: number;
  hasWorkspace: boolean; hasWorktree: boolean; canSwitchWorkspace: boolean; onOpen: (tab: SideTab) => void; onChanges: (snapshot: boolean) => void;
  onToggle: () => void; onAddSource: () => void; onReadSources: () => void;
  onAgent:(id?:string)=>void;
  trigger: React.RefObject<HTMLButtonElement | null>;
}) {
  const { t } = useUiLanguage();
  const [popup, setPopup] = useState<"overview">();
  const [workspaceMenu, setWorkspaceMenu] = useState(false);
  const overviewButton = useRef<HTMLButtonElement>(null);
  function show(menu: typeof popup) { setPopup(popup === menu ? undefined : menu); setWorkspaceMenu(false); }
  // 先关闭侧栏菜单再派发动作，避免菜单遮挡刚打开的页签。
  function choose(action: () => void) { setPopup(undefined); setWorkspaceMenu(false); action(); }
  const tasks = agents?.tasks.filter(task=>task.runId===currentRun) ?? [];
  const running = tasks.filter(task => activeStates.has(task.state) && task.state !== "unknown").length;
  const completed = tasks.filter(task => task.state === "completed").length;
  const pending = tasks.filter(task => task.state === "unknown").length;
  const other = tasks.length - running - completed - pending;
  return <div className="side-toolbar panel-switcher" role="group" aria-label="查看面板" onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) setPopup(undefined);
  }} onKeyDown={event => {
    if (event.key === "Escape" && popup) {
      event.stopPropagation();
      if (workspaceMenu) {setWorkspaceMenu(false); event.currentTarget.querySelector<HTMLButtonElement>(".side-workspace-more")?.focus();}
      else {setPopup(undefined); overviewButton.current?.focus();}
    }
  }}>
    <button type="button" aria-label="查看代码改动" title="查看代码改动" disabled={disabled}
      aria-pressed={!state.collapsed && state.active === "changes"} onClick={() => choose(() => onChanges(false))}><UiIcon name="code"/></button>
    <button ref={trigger} type="button" aria-label={t("新标签页")} title={t("新标签页")} disabled={disabled}
      onClick={() => choose(()=>onOpen("start"))}><UiIcon name="plus"/></button>
    <button ref={overviewButton} type="button" aria-label="对话总览" title="对话总览" disabled={disabled}
      aria-expanded={popup === "overview"} onClick={() => show("overview")}><UiIcon name="overview"/></button>
    <button type="button" className="side-toggle" aria-label={state.collapsed || !state.active ? "展开右侧栏" : "收起右侧栏"}
      title={unread|| (state.collapsed || !state.active ? "展开右侧栏" : "收起右侧栏")} disabled={disabled}
      aria-description={unread||undefined} aria-expanded={!!state.active && !state.collapsed} onClick={() => choose(onToggle)}>
      <UiIcon name="right-sidebar"/>{unread && <i className="side-unread" aria-label={unread}/>}
    </button>
    {popup === "overview" && <div className="side-popup side-overview" aria-label="对话总览内容">
      <header><strong title={projectName}>{projectName}</strong><button className="side-workspace-more" title="工作区操作"
        aria-label="工作区操作" aria-expanded={workspaceMenu} disabled={!hasWorkspace} onClick={() => setWorkspaceMenu(!workspaceMenu)}><UiIcon name="more"/></button></header>
      {workspaceMenu && <div className="side-workspace-menu">
        {canSwitchWorkspace && <button onClick={() => choose(() => onOpen("environment"))}>设置本地环境</button>}
        <button onClick={() => choose(() => onOpen("git"))}><UiIcon name="branch"/>Git 操作</button>
        <button onClick={() => choose(() => onOpen("terminal"))}><UiIcon name="terminal"/>打开终端</button>
        {canSwitchWorkspace && <button onClick={() => choose(() => onOpen("handoff"))}>{hasWorktree ? "切回本地／切换工作树" : "移动到工作树"}</button>}
      </div>}
      <button className="side-overview-row" onClick={() => choose(() => onChanges(true))}>
        <UiIcon name="code"/><span>{t("变更")} <small>{rangeLabel}</small></span>
        {summary && !summaryUnavailable && summary.frames.length ? <LineCounts {...summary.totals}/> : <small>{summaryUnavailable ? "暂无可核对统计" : "暂无记录"}</small>}
      </button>
      <section><h3>侧边聊天</h3>
        {!related.length && <small>暂无关联讨论</small>}
        {related.map(chat => <button className="side-overview-row" key={chat.id} onClick={() => choose(() => onOpen(`chat:${chat.id}`))}>
          <UiIcon name="compose"/><span>{chat.title}</span>{(chat.eventCursor || 0) > (seen[chat.id] || 0) && <small>有更新</small>}
        </button>)}
      </section>
      {tasks.length > 0 && <section><h3>子 Agent</h3>
        <div className="side-agent-icons">{tasks.slice(0,4).map(task=><button key={task.id} aria-label={`查看 ${task.nickname} 概述`} title={`${task.nickname} · ${states[task.state]??"待核对"}`} onClick={()=>choose(()=>onAgent(task.id))}><AgentAvatar id={task.id}/></button>)}
        <button className="side-overview-row" aria-label="查看子 Agent 概述" onClick={() => choose(() => onAgent())}>
          <span>{[
            running && `${running} 进行中`, completed && `${completed} 完成`,
            pending && `${pending} 待核对`, other && `${other} 其他终态`,
          ].filter(Boolean).join(" · ")}</span>
        </button></div>
      </section>}
      <section><header><h3>来源</h3><button aria-label="添加资料" title="添加资料" onClick={() => choose(onAddSource)}><UiIcon name="plus"/></button></header>
        {searchCount > 0 && <button className="side-overview-row" onClick={() => choose(onReadSources)}><UiIcon name="browser"/><span>网页搜索</span><small>{searchCount} 项</small></button>}
        <button className="side-overview-row" aria-label="查看全部" onClick={() => choose(onReadSources)}>
          <UiIcon name="sources"/><span>查看全部</span><small>{sources} 项</small>
        </button>
      </section>
    </div>}
  </div>;
}

// 挂载已打开的侧栏页签并切换显隐，可隐藏的内容保留其本地状态。
export function SideTabs({ state, labels, onOpen, onClose, children }: {
  state: SideState; labels: (tab: SideTab) => string; onOpen: (tab: SideTab) => void;
  onClose: (tab: SideTab) => void; children: ReactNode;
}) {
  const nav = useRef<HTMLElement>(null);
  useEffect(() => {
    const bar = nav.current;
    const current = bar?.querySelector('[aria-current="page"]')?.parentElement;
    if (!bar || !current || state.collapsed) return;
    const bounds = bar.getBoundingClientRect(), item = current.getBoundingClientRect();
    if (item.right > bounds.right) bar.scrollLeft += item.right - bounds.right + 6;
    if (item.left < bounds.left) bar.scrollLeft += item.left - bounds.left - 6;
  }, [state.active, state.collapsed]);
  return <aside className="conversation-side-tabs" aria-label="右侧标签区" hidden={state.collapsed || !state.active}>
    <nav ref={nav} aria-label="右侧标签">{state.tabs.map(tab => <div key={tab} data-current={state.active === tab}>
      <button type="button" title={labels(tab)} aria-current={state.active === tab ? "page" : undefined} onClick={() => onOpen(tab)}>{labels(tab)}</button>
      <button type="button" aria-label={`关闭${labels(tab)}标签`} onClick={() => onClose(tab)}><UiIcon name="close"/></button>
    </div>)}</nav>
    {children}
  </aside>;
}

// 以 `AgentHome.tsx` 传入的运行和焦点为默认筛选。
// 定位原子任务时固定所属轮次，当前运行推进后仍可审阅原项。
export function AgentOverviewPane({ conversationId, query, currentRun, focus,visible,onRead }: {
  conversationId: string; query: ReturnType<typeof useSubtaskOverview>; currentRun?: string; focus?:{id?:string;serial:number};
  visible:boolean;onRead:(owner:string,keys:string[])=>void;
}) {
  const storageKey = "subtask-overview-all-" + conversationId;
  const [all, setAll] = useState(() => loadJSON(storageKey, !!query.data?.tasks.length&&!query.data.tasks.some(task=>task.runId===currentRun)));
  useEffect(() => saveJSON(storageKey, all), [storageKey, all]);
  const [opened, setOpened] = useState<string>();
  const [pinnedRun, setPinnedRun] = useState<string>();
  const opener = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const position = useRef(0);
  useEffect(()=>{if(focus){setAll(false);setPinnedRun(currentRun);setOpened(focus.id);}},[focus]);
  const detail = query.error ? undefined : query.data?.tasks.find(task => task.id === opened);
  const runId = pinnedRun || currentRun;
  const groups = new Map<string, SubtaskOverview["tasks"]>();
  for (const task of query.error ? [] : query.data?.tasks ?? []) {
    if (!all && task.runId !== runId) continue;
    groups.set(task.runId, [...groups.get(task.runId) ?? [], task]);
  }
  const displayed=JSON.stringify((opened?(detail?[detail]:[]):[...groups.values()].flat()).map(agentReadKey).sort());
  useEffect(()=>{if(visible&&!query.error&&query.isSuccess)onRead(conversationId,JSON.parse(displayed));},
    [visible,query.error,query.isSuccess,conversationId,displayed,onRead]);
  // 从详情退回前次列表滚动位置，把键盘焦点还给触发项。
  function back() {
    setOpened(undefined);
    requestAnimationFrame(() => {
      const viewport = list.current?.closest(".side-tab-body");
      if (viewport) viewport.scrollTop = position.current;
      opener.current?.focus({ preventScroll: true });
    });
  }
  return <section className="side-content agent-overview" aria-label="子 Agent 概述">
    {query.error ? <p role="alert">暂时无法读取子 Agent 概述。 <button onClick={() => query.refetch()}>重试</button></p>
      : query.isPending ? <p role="status">正在读取子 Agent 概述…</p> : <>
        <div ref={list} hidden={!!opened}>
          <header><h2>子 Agent</h2>
            <select aria-label="子 Agent 范围" value={all ? "all" : pinnedRun && pinnedRun !== currentRun ? "past" : "current"}
              onChange={event => {setAll(event.target.value === "all"); setPinnedRun(undefined);}}>
              <option value="current">当前轮次</option><option value="all">整个对话</option>
              {pinnedRun && pinnedRun !== currentRun && <option value="past">刚才查看的轮次</option>}
            </select>
          </header>
          {!groups.size && <p>这个范围还没有委派子 Agent。</p>}
          {[...groups].map(([run, tasks]) => <section key={run} className="side-round">
            {all && <h3>{run === currentRun ? "当前轮次" : "历史轮次"} {tasks[0].created ? new Date(tasks[0].created * 1000).toLocaleString() : ""}</h3>}
            {[true, false].map(active => {
              const items = tasks.filter(task => activeStates.has(task.state) === active);
              return items.length > 0 && <div key={String(active)}><h4>{active ? "进行中与待核对" : "已结束"} · {items.length}</h4>
                {items.map(task => <button key={task.id} className="side-agent-row" onClick={event => {
                  opener.current = event.currentTarget;
                  position.current = list.current?.closest(".side-tab-body")?.scrollTop ?? 0;
                  setOpened(task.id);
                  if (!all) setPinnedRun(runId);
                }}>
                  <AgentAvatar id={task.id}/><span><strong>{task.nickname} · {task.title}</strong><small>{task.summary || (active ? "等待任务结果" : "暂无概述")}</small></span>
                  <small>{taskStatus(task)}</small>
                </button>)}</div>;
            })}
          </section>)}
        </div>
        {opened && <section className="side-agent-detail" aria-label="单个子 Agent 概述">
          <button onClick={back}><UiIcon name="back"/>返回列表</button>
          {detail ? <><h2><AgentAvatar id={detail.id}/>{detail.nickname} · {detail.title}</h2><small>{taskStatus(detail)}</small>
            <p>{detail.summary || (activeStates.has(detail.state) ? "等待任务结果" : "暂无概述")}</p></>
            : <p>此任务的概述暂不可用。</p>}
        </section>}
      </>}
  </section>;
}

// 网页草稿、已选资料和搜索动作来自不同入口，搜索来源按原运行过滤。
export function SourcesPane({ resources, web, search, currentRun, focusedRun, onAdd }: {
  resources: ProjectResource[]; web: WebSource[]; search: SearchSource[]; currentRun?: string; focusedRun?: string; onAdd: () => void;
}) {
  const [choice, setChoice] = useState<{requested?: string; range: string}>();
  const range = choice?.requested === focusedRun ? choice?.range ?? focusedRun : focusedRun;
  const viewedRun = range && range !== "all" && range !== "current" ? range : currentRun;
  const runs = [...new Set(search.map(source => source.runId))];
  return <section className="side-content" aria-label="来源汇总">
    <header><h2>来源</h2><button onClick={onAdd}>添加资料</button></header>
    {!resources.length && !web.length && !search.length && <p>当前对话暂无可核对来源。</p>}
    {!!resources.length && <section><h3>已选资料</h3><ul>{resources.map(resource => <li key={resource.id}>{resource.name}</li>)}</ul></section>}
    {!!web.length && <section><h3>网页引用</h3><ul>{web.map(item => <li key={item.url}>
      <strong>{item.title || "网页"}</strong> <small>{item.sent ? "已随消息发送" : "已加入草稿"}</small>
      <div className="side-source-url">{item.url}</div>
    </li>)}</ul></section>}
    {!!search.length && <section><header><h3>网页搜索</h3><select aria-label="搜索来源范围" value={range || "current"} onChange={e => setChoice({requested:focusedRun,range:e.target.value})}>
      <option value="current">当前轮次</option><option value="all">整个对话</option>
      {focusedRun && <option value={focusedRun}>所选轮次</option>}
    </select></header>
      {range !== "all" && !runs.includes(viewedRun || "") && <p>所选轮次暂无可核对的搜索来源。</p>}
      {runs.filter(run => range === "all" || run === viewedRun).map((run, index) => <div key={run}>
        <h4>{run === currentRun ? "当前轮次" : `历史搜索轮次 ${index + 1}`}</h4>
        <ul>{search.filter(item => item.runId === run).map(item => <li key={item.url} title={item.tool}>
          <a href={item.url} target="_blank" rel="noreferrer">{item.title}</a><div className="side-source-url">{item.url}</div></li>)}</ul>
      </div>)}
    </section>}
  </section>;
}

// 轮询关联会话的公开历史。只在最新页追到已知事件游标时更新已读状态。
export function RelatedChatPane({ chat, visible, onRead }: { chat: Conversation; visible:boolean; onRead:(cursor:number)=>void }) {
  const feed=useRef<HTMLElement>(null);
  const history=useThreadReader("/api/v2/conversations", chat.id, !!chat.runtime?.active, feed,visible);
  useConversationScroll(feed,chat.id,String(history.visibleHistory?.events.at(-1)?.sourceOffset),!visible,history.historyBefore===0);
  useEffect(()=>{if(visible&&!history.historyBefore&&!history.history.error&&history.history.data&&!history.history.data.catchingUp&&typeof chat.eventCursor==="number"&&
    (chat.eventCursor===0||history.history.data.events.some(event=>(event.seq??0)>=chat.eventCursor!)))onRead(chat.eventCursor);},[visible,history.historyBefore,history.history.error,history.history.data,chat.eventCursor,onRead]);
  return <section ref={feed} className="side-content related-chat-feed" aria-label="关联聊天记录">
    <header><h2>{chat.title}</h2><small>只读</small></header>
    <HistoryControls state={history}/>
    {history.history.error && <p role="alert">暂时无法读取关联聊天。</p>}
    {history.visibleHistory?.events.filter(event => ["user", "say", "ask_human"].includes(event.act) && event.text)
      .map((event, index) => <article key={event.sourceOffset ?? index} data-conversation-item-id={event.sourceOffset ?? index}>
        <small>{event.act === "user" ? "你" : "Agent"}</small><Message text={String(event.text)}/>
      </article>)}
  </section>;
}

// 以会话已授权代码资料选择检出目录，再打开 Git 或终端页签。
export function WorkspaceSidePane({ kind, resources }: { kind: "git" | "terminal"; resources: ProjectResource[] }) {
  const [selected, setSelected] = useState("");
  const [opened, setOpened] = useState<{ resource: string; workspace: Workspace; run?: ProcessRun }>();
  const [paths, setPaths] = useState<string[]>([]);
  const [message, setMessage] = useState("");
  const [branch, setBranch] = useState("");
  const [confirmPush, setConfirmPush] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const resource = resources.find(r => r.id === (selected || (resources.length === 1 ? resources[0].id : "")));
  const workspace = resource && opened?.resource === resource.id && resource.path === opened.workspace.path
    ? opened.workspace : undefined;
  const git = useQuery({ queryKey: ["side-git", workspace?.id],
    queryFn: () => api<GitState>(wsPath(workspace!.id, "git")), enabled: !!workspace && kind === "git" });
  // 统一承接异步动作的忙碌和错误状态，交由具体操作核对后端结果。
  async function perform(work: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError("");
    try { await work(); }
    catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  // 从会话资源解析或打开对应的真实工作间目录。
  function open() {
    if (!resource) return;
    void perform(async () => {
      const value = workspace ?? await api<Workspace>("/api/v2/workspaces", { path: resource.path });
      setOpened({ resource: resource.id, workspace: value });
      if (kind === "terminal" && value.capabilities.pty) {
        const run = await api<ProcessRun>(wsPath(value.id, "terminals"), { confirm: true });
        setOpened({ resource: resource.id, workspace: value, run });
      }
    });
  }
  // 把 Git 操作绑定到已打开的工作区，完成后重新读取工作区状态。
  function gitAction(action: string, extra: object = {}) {
    if (!workspace || !git.data?.revision) return;
    void perform(async () => {
      try { await api(wsPath(workspace.id, "git"), { action, paths, expectedRevision: git.data!.revision, ...extra }); }
      finally {setConfirmPush(false); await git.refetch();}
    });
  }
  return <section className="side-content" aria-label={sideLabels[kind]}>
    <header><h2>{sideLabels[kind]}</h2></header>
    {!resources.length ? <p>未关联可读代码工作区。</p> : <header>
      <select aria-label="目标工作区" value={resource?.id || ""} onChange={event => {
        setSelected(event.target.value); setPaths([]); setConfirmPush(false);
      }} disabled={busy}>
        <option value="">选择工作区</option>
        {resources.map(r => <option value={r.id} key={r.id}>{r.name} · {r.path}</option>)}
      </select>
      <button disabled={!resource || busy} onClick={open}>{kind === "git" ? "读取 Git 状态" : "新建真实终端"}</button>
    </header>}
    {(error || git.error) && <p role="alert">{error || git.error?.message}</p>}
    {kind === "terminal" && workspace && !workspace.capabilities.pty && <p>当前后端没有可用的交互终端运行时。</p>}
    {kind === "terminal" && workspace && opened?.run && <TerminalPane key={opened.run.id} run={opened.run} onError={error => setError(String(error))}/>}
    {kind === "git" && git.data && (git.data.available ? <>
      <p>{git.data.branch || "分支未知"} {git.data.protected && " · 保护分支"}</p>
      <button onClick={() => git.refetch()} disabled={busy}>刷新 Git 状态</button>
      {git.data.files.map(file => <label className="side-git-file" key={file.path}>
        <input type="checkbox" checked={paths.includes(file.path)} onChange={event =>
          setPaths(old => event.target.checked ? [...old, file.path] : old.filter(path => path !== file.path))}/>
        <span>{file.path}</span><small>{file.status}</small>
      </label>)}
      <header><button disabled={busy || !paths.length} onClick={() => gitAction("stage")}>暂存</button>
        <button disabled={busy || !paths.length} onClick={() => gitAction("unstage")}>取消暂存</button></header>
      <header><input aria-label="新分支名称" placeholder="agent/任务名称" value={branch} onChange={event => setBranch(event.target.value)}/>
        <button disabled={busy || !branch.startsWith("agent/")} onClick={() => gitAction("branch", { branch })}>新建分支</button></header>
      <header><input aria-label="提交说明" placeholder="提交说明" value={message} onChange={event => setMessage(event.target.value)}/>
        <button disabled={busy || !!git.data.protected || !message.trim()} onClick={() => gitAction("commit", { message })}>提交</button></header>
      {confirmPush ? <header><span>推送当前分支到 origin？</span>
        <button disabled={busy || !!git.data.protected} onClick={() => gitAction("push", { confirm: true })}>确认推送</button>
        <button onClick={() => setConfirmPush(false)}>取消</button></header>
        : <button disabled={busy || !!git.data.protected} onClick={() => setConfirmPush(true)}>推送…</button>}
    </> : <p>{git.data.reason || "此工作区没有可用 Git 状态。"}</p>)}
  </section>;
}
