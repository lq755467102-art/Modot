// 组织工作间的检出目录、文件编辑、运行和审查入口。
import { useUiLanguage } from "./UiLanguage";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore, type Dispatch, type SetStateAction } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReactCodeMirrorRef } from "@uiw/react-codemirror";
import {
  APIError,
  reconcileReceipts,
  api,
  workbenchBootstrapPath,
  wsPath,
  queryString,
  loadJSON,
  saveJSON,
} from "./client";
import {
  initialUI,
  reduceUI,
  type WorkspaceSummary,
  type Thread,
  type GitState,
  type GitHead,
  type GitChangesPage,
  type OpenFile,
  type EventRow,
  type ProcessRun,
  type UIState,
  type UIAction,
} from "./domain";
import { chord, bound } from "./shortcuts";
import { PullRequestPane } from "./PullRequestPane";
import {useConversationScroll} from "./useConversationScroll";
import {useThreadReader} from "./useThreadReader";
import {HistoryControls} from "./HistoryControls";
import { parentEvent, projectConversation } from "./conversationProjection";
import { ProblemsPane } from "./ProblemsPane";
import { DiffReview } from "./DiffReview";
import { WorkspacesPane } from "./WorkspacesPane";
import { TerminalPane, DebugPane, EditorView, Message } from "./Panels";
import { Inbox } from "./Inbox";
import { JobsPane } from "./JobsPane";
import { useApplicationSettings } from "./ApplicationSettings";
import { ToolHandoffDialog, toolExcerpt, type ToolHandoff } from "./ToolHandoff";
import { Dialog } from "./Dialog";
import {UiIcon} from "./UiIcon";
import {ProjectWorkroom,WorkroomToolbar} from "./ProjectWorkroom";
import { WorkroomConversationRecord } from "./ConversationRecord";
import type {ChromeCommand} from "./App";
import type {Conversation,ConversationScope,FileContent,WorkroomContext,WorkroomOpened} from "./generated";
import "./workroom-layout.css";
import {normalizeEditorText,type EditorPosition} from "./EditorView";
import type { WorkroomFileRequest } from "./WorkroomChangeLink";
import {WorkroomFileTabs,type WorkroomFile} from './WorkroomFileTabs';
import {useLanguageSession,languageFor} from './workroomLanguage';
import {persistentDraftOwner,windowTabs,persistWindowTabs} from './workroomWindowDrafts';
import {WorkroomFileReader} from "./WorkroomTextReader";
import {WorkroomQuickOpen} from './WorkroomQuickOpen';
import {WorkroomPresetSelect} from './WorkroomPresetSelect';
import {commandArguments} from './runCommand';
import {WorkroomConversation} from './WorkroomConversation';
import {WorkroomFileTree, type FileEntry} from './WorkroomFiles';
import {documentLanguage} from './editorSnippets';
import './workroom-conversation.css';

type Handoff = (value: ToolHandoff, newDraft: boolean, stayInWorkroom?: boolean) => void;

function needsFileReader(error: unknown) {
  return error instanceof APIError && ["large_file", "binary", "encoding"].includes(error.code);
}

function createWorkspaceCache(){return {values:new Map<string,unknown>(),listeners:new Map<string,Set<()=>void>>()};}
const WorkspaceCache=createContext(createWorkspaceCache());
// 在工作间归属下保存和恢复选中的工作区布局与阅读位置。
function useWorkspaceState<T>(workspace:string,name:string,initial:T|(()=>T)):[T,Dispatch<SetStateAction<T>>]{
  const cache=useContext(WorkspaceCache),key=workspace+':'+name;
  if(!cache.values.has(key))cache.values.set(key,typeof initial==='function'?(initial as ()=>T)():initial);
  const snapshot=useCallback(()=>cache.values.get(key) as T,[cache,key]);
  const subscribe=useCallback((listener:()=>void)=>{let listeners=cache.listeners.get(key);if(!listeners){listeners=new Set();cache.listeners.set(key,listeners);}listeners.add(listener);return()=>{listeners.delete(listener);if(!listeners.size)cache.listeners.delete(key);};},[cache,key]);
  const value=useSyncExternalStore(subscribe,snapshot);
  const update=useCallback<Dispatch<SetStateAction<T>>>(action=>{const previous=cache.values.get(key) as T,next=typeof action==='function'?(action as (value:T)=>T)(previous):action;if(Object.is(previous,next))return;cache.values.set(key,next);cache.listeners.get(key)?.forEach(listener=>listener());},[cache,key]);
  return [value,update];
}

export default function WorkroomPage({conversationId,visible,chromeCommand,onHandoff,fileRequest,onClearFileRequest,onOpenConversation}:{conversationId:string;visible:boolean;chromeCommand?:ChromeCommand;onHandoff:Handoff;fileRequest?:WorkroomFileRequest;onClearFileRequest:()=>void;onOpenConversation:()=>void}){
  const { t } = useUiLanguage();
  const cache=useRef(createWorkspaceCache());
  const [primary,setPrimary]=useState<boolean>();
  useEffect(()=>{let live=true;void persistentDraftOwner().then(v=>{if(live)setPrimary(v)});return()=>{live=false}},[]);
  return <WorkspaceCache.Provider value={cache.current}><ProjectWorkroom conversationId={conversationId} visible={visible}
    projectRequest={chromeCommand?.action==='workspace'?chromeCommand.serial:0} fileRequest={fileRequest} onClearFileRequest={onClearFileRequest}>
    {(binding,context,followConversation)=>fileRequest?.kind === "conversation-record" ?
      <WorkroomConversationRecord key={fileRequest.serial} request={fileRequest} visible={visible} onClose={onClearFileRequest}/> :
      fileRequest?.kind === "linked-file-readonly" ?
        <div className="workroom-linked-file" aria-label={t("工作间只读文件")}>
          <header><strong title={fileRequest.path}>{fileRequest.path}</strong>
            <button type="button" onClick={onClearFileRequest}>{t("关闭只读查看")}</button></header>
          <WorkroomFileReader key={fileRequest.serial} workspace={binding.workspaceId} path={fileRequest.path} visible={visible}/>
        </div> :
      primary===undefined?null:<WorkbenchApp key={binding.workspaceId} primary={primary} initialWorkspaceId={binding.workspaceId} binding={binding}
        context={context} conversationId={conversationId} visible={visible} onHandoff={onHandoff} fileRequest={fileRequest} onOpenConversation={onOpenConversation} followConversation={followConversation}/>}
  </ProjectWorkroom></WorkspaceCache.Provider>;
}

// 工程工具按需挂载；会话输入、文件草稿和运行记录分别保留自己的工作区归属。
function WorkbenchApp({ onHandoff, visible, initialWorkspaceId,binding,context,conversationId,fileRequest,onOpenConversation,followConversation,primary }: { onHandoff: Handoff; visible: boolean; initialWorkspaceId:string;binding:WorkroomOpened;context:WorkroomContext;conversationId:string;fileRequest?:WorkroomFileRequest;onOpenConversation:()=>void;followConversation:()=>void;primary:boolean }) {
  const { t } = useUiLanguage();
  const [bottomOpen,setBottomOpen]=useWorkspaceState(initialWorkspaceId,'bottomOpen',false);
  const [configOpen,setConfigOpen]=useWorkspaceState(initialWorkspaceId,'configOpen',false);
  const [filesOpen,setFilesOpen]=useWorkspaceState(initialWorkspaceId,'filesOpen',()=>!matchMedia('(max-width:900px)').matches);
  function showTool(value:string){setBottom(value);setBottomOpen(true);}
  const container=useRef<HTMLDivElement>(null);
  const [editTools,setEditTools]=useState<HTMLDivElement|null>(null);
  useEffect(()=>{
    const media=matchMedia('(max-width:900px)');
    const compact=()=>{if(media.matches){const active=document.activeElement,root=container.current,inFiles=!!root?.querySelector('.navigation')?.contains(active),inRun=!!root?.querySelector('.bottom-panel')?.contains(active);setFilesOpen(false);setBottomOpen(false);if(inFiles||inRun)queueMicrotask(()=>{if(inRun)root?.querySelector<HTMLButtonElement>('.workroom-run-bar>button')?.focus();else document.querySelector<HTMLButtonElement>('.workroom-toolbar .workroom-tree-toggle')?.focus();});}};
    compact();media.addEventListener('change',compact);return()=>media.removeEventListener('change',compact);
  },[]);
  useEffect(()=>{
    if(!visible)return;
    const menus=()=>Array.from(document.querySelectorAll<HTMLDetailsElement>('.project-workroom .workroom-actions-menu[open]'));
    const outside=(event:PointerEvent)=>{for(const menu of menus())if(!menu.contains(event.target as Node))menu.removeAttribute('open');};
    const escape=(event:KeyboardEvent)=>{if(event.key!=='Escape'||event.defaultPrevented||(event.target as Element)?.closest('[role="dialog"]'))return;const open=menus();if(open.length){event.preventDefault();open.at(-1)?.querySelector('summary')?.focus();open.forEach(menu=>menu.removeAttribute('open'));}};
    document.addEventListener('pointerdown',outside);document.addEventListener('keydown',escape);return()=>{document.removeEventListener('pointerdown',outside);document.removeEventListener('keydown',escape);};
  },[visible]);
  const { preferences, theme, openSettings, defaultPolicy, setDefaultPolicy } = useApplicationSettings();
  const [commandKind, setCommandKind] = useWorkspaceState(initialWorkspaceId,"commandKind","file");
  const [handoffPreview, setHandoffPreview] = useWorkspaceState<ToolHandoff|undefined>(initialWorkspaceId,"handoffPreview",undefined);
  const [handoffBusy, setHandoffBusy] = useWorkspaceState(initialWorkspaceId,"handoffBusy",false);
  const [ui,setUI]=useWorkspaceState<UIState>(initialWorkspaceId,'ui',()=>{
    const saved=loadJSON<UIState>('layout',initialUI),legacy=saved.workspaceId===initialWorkspaceId?saved:reduceUI(saved,{type:'workspace',id:initialWorkspaceId});
    const restored=loadJSON<UIState>('workroom-layout:'+initialWorkspaceId,{...legacy,layout:'develop',navigation:'files',rightVisible:false});
    return ['files','search','changes','tasks','inbox'].includes(restored.navigation)?restored:{...restored,navigation:'files'};
  });
  const applyUI=(action:UIAction)=>setUI(old=>reduceUI(old,action));
  const [tabs, updateTabs] = useWorkspaceState<WorkroomFile[]>(initialWorkspaceId,"tabs",() => {
      const saved = windowTabs(initialWorkspaceId,primary);
      return saved.map(file=>({...file,instance:crypto.randomUUID()}));
    });
  const tabsRef=useRef(tabs);tabsRef.current=tabs;
  function setTabs(update:(old:WorkroomFile[])=>WorkroomFile[]){updateTabs(old=>{const next=update(old);tabsRef.current=next;return next;});}
  const [fileStates,setFileStates]=useWorkspaceState<Record<string,{pending:boolean;message:string;failed?:boolean}>>(initialWorkspaceId,'fileStates',{});
  const [fileLocks]=useWorkspaceState(initialWorkspaceId,'fileLocks',()=>new Set<string>());
  const [creating,setCreating]=useWorkspaceState<{path:string;pending:boolean;error:string}|null>(initialWorkspaceId,'creating',null);
  const [closing,setClosing]=useWorkspaceState<WorkroomFile|null>(initialWorkspaceId,'closing',null);
  const [error, setError] = useWorkspaceState(initialWorkspaceId,"error",() => new URLSearchParams(location.search).get("bootError") || ""),
    [folder, setFolder] = useWorkspaceState(initialWorkspaceId,"folder",""),
    [search, setSearch] = useWorkspaceState(initialWorkspaceId,"search",""),
    [contentSearch, setContentSearch] = useWorkspaceState(initialWorkspaceId,"contentSearch",false);
  const imported = context.importLocation;
  const [lastImport,setLastImport]=useWorkspaceState(initialWorkspaceId,"lastImport","");
  useEffect(()=>{
    if(!visible||fileRequest||!imported?.directory||imported.resourceId!==binding.resourceId)return;
    const key=JSON.stringify([context.conversationId,imported.resourceId,imported.directory]);
    if(key!==lastImport){setFolder(imported.directory);setLastImport(key);}
  },[visible,fileRequest,imported?.directory,imported?.resourceId,binding.resourceId,context.conversationId,lastImport]);
  const [drafts, setDrafts] = useWorkspaceState<Record<string, string>>(initialWorkspaceId,"drafts",() =>
    loadJSON("workroom-drafts:"+initialWorkspaceId,Object.fromEntries(Object.entries(loadJSON<Record<string,string>>("drafts",{})).filter(([key])=>key.startsWith(initialWorkspaceId+":")))));
  const [bottom, setBottom] = useWorkspaceState(initialWorkspaceId,"bottom",() => loadJSON("workroom-bottom:"+initialWorkspaceId, "checks")),
    [selectedRun, setSelectedRun] = useWorkspaceState<ProcessRun | null>(initialWorkspaceId,"selectedRun",null);
  useEffect(() => saveJSON("workroom-bottom:"+initialWorkspaceId, bottom), [bottom]);
  const [argvText, setArgvText] = useWorkspaceState(initialWorkspaceId,"argvText",'["python", "-m", "pytest"]'),
    [presetName, setPresetName] = useWorkspaceState(initialWorkspaceId,"presetName","Python 测试");
  const [presetBaseline,setPresetBaseline]=useWorkspaceState(initialWorkspaceId,'presetBaseline',{name:'Python 测试',argv:'["python", "-m", "pytest"]',saved:false});
  const [pendingPreset,setPendingPreset]=useWorkspaceState<{name:string;argv:string[]}|null>(initialWorkspaceId,'pendingPreset',null);
  const [commandLocks]=useWorkspaceState(initialWorkspaceId,'commandLocks',()=>new Set<string>());
  const [runPending,setRunPending]=useWorkspaceState(initialWorkspaceId,'runPending',false);
  const [presetPending,setPresetPending]=useWorkspaceState(initialWorkspaceId,'presetPending',false);
  const [commandStatus,setCommandStatus]=useWorkspaceState(initialWorkspaceId,'commandStatus','');
  const [quickQueries,setQuickQueries]=useWorkspaceState(initialWorkspaceId,'quickQueries',{file:'',command:''});
  const configDirty=!presetBaseline.saved||presetName!==presetBaseline.name||argvText!==presetBaseline.argv;
  let argvError='';try{commandArguments(argvText);}catch(error){argvError=(error as Error).message;}
  const [diffFile, setDiffFile] = useWorkspaceState(initialWorkspaceId,"diffFile",""),
    [staged, setStaged] = useWorkspaceState(initialWorkspaceId,"staged",false),
    [commitMessage, setCommitMessage] = useWorkspaceState(initialWorkspaceId,"commitMessage","");
  const [reviewText, setReviewText] = useWorkspaceState(initialWorkspaceId,"reviewText",""),
    [reviewLine, setReviewLine] = useWorkspaceState(initialWorkspaceId,"reviewLine",1),
    [timeline, setTimeline] = useWorkspaceState(initialWorkspaceId,"timeline",false),
    [archives, setArchives] = useWorkspaceState(initialWorkspaceId,"archives",false);
  const [commandOpen, setCommandOpen] = useWorkspaceState(initialWorkspaceId,"commandOpen",false),
    [breakLine, setBreakLine] = useWorkspaceState(initialWorkspaceId,"breakLine",1);
  const [conflict, setConflict] = useWorkspaceState<{
    key: string;
    instance:string;
    disk: any;
    draft: string;
  } | null>(initialWorkspaceId,"conflict",null);
  const editor = useRef<ReactCodeMirrorRef>(null);
  const fileHandled = useRef(0);
  const [agentFileResult, setAgentFileResult] = useState<{serial:number; opened:boolean}>();
  const fileChoice = useRef(0);
  const [fileJump,setFileJump] = useState<{path:string;line:number;serial:number}>();
  const [locationNote,setLocationNote] = useState<{path:string;message:string}>();
  const current = useRef(ui);
  current.current = ui;
  // 文件树只需要工作区登记，Git 状态由当前目录的请求单独更新。
  const bootstrap = useQuery<{
    workspaces: WorkspaceSummary[];
    policy: string;
    hostTransportBlocks: { terminal: string | null; debug: string | null };
  }>({
    queryKey: ["policy-v2"],
    queryFn: () => api(workbenchBootstrapPath),
    enabled: visible,
    refetchOnMount: "always",
  });
  const workspaces = bootstrap.data?.workspaces ?? [];
  // 权限尚未读到或读取失败时保持阻断提示，不能把缺少策略当成允许启动终端/调试。
  const unknownTransport = bootstrap.error ? "暂时无法确认运行权限。" : "正在核对运行权限…";
  const terminalBlock = bootstrap.error || !bootstrap.data?.hostTransportBlocks
    ? unknownTransport : bootstrap.data.hostTransportBlocks.terminal;
  const debugBlock = bootstrap.error || !bootstrap.data?.hostTransportBlocks
    ? unknownTransport : bootstrap.data.hostTransportBlocks.debug;
  const query = useQueryClient();
  const currentConversation=useQuery<{threads:Conversation[]}>({queryKey:['conversations'],queryFn:()=>api('/api/v2/conversations/threads'),enabled:visible});
  const currentScope=useQuery<ConversationScope>({queryKey:['scope',conversationId],queryFn:()=>api('/api/v2/conversations/threads/'+queryString(conversationId)+'/scope'),enabled:visible&&!!conversationId});
  const targetConversation=currentConversation.data?.threads.find(item=>item.id===conversationId);
  const canHandoffCurrent=!currentConversation.error&&!!targetConversation&&!targetConversation.archived&&!targetConversation.subtaskOf&&!targetConversation.readonlyEvidence&&!currentScope.error&&currentScope.data?.projectId===binding.projectId&&currentScope.data.resources.some(resource=>resource.permission==='read'&&resource.id===binding.resourceId&&resource.path===binding.path);
  const w = workspaces.find((w) => w.id === ui.workspaceId);
  const base = w ? wsPath(w.id) : "";
  // 同名文件和会话可能分属不同工程；输入及运行设置按工作区和会话共同隔离。
  const draftKey = ui.workspaceId + ":" + ui.threadId;
  const draft = drafts[draftKey] || "";
  const [positions,setPositions]=useWorkspaceState<Record<string,EditorPosition>>(initialWorkspaceId,'positions',()=>loadJSON('workroom-positions:'+initialWorkspaceId,{}));
  useEffect(()=>saveJSON('workroom-positions:'+initialWorkspaceId,positions),[positions,initialWorkspaceId]);
  useEffect(()=>{if(!visible){setCommandOpen(false);setHandoffPreview(undefined);}},[visible]);
  const active = tabs.find(
    (f) => f.workspaceId + ":" + f.path === ui.activeFile,
  );
  const language=useLanguageSession(binding,context,visible&&active&&!active.readOnly?languageFor(active.path):undefined,tabsRef);
  const fail = (e: unknown) =>
    setError(e instanceof Error ? e.message : String(e));
  // 通过 `domain.ts` 应用工作间状态变化，并交给持久化状态管理。
  const dispatch = (action:UIAction) => {
    if(action.type==='workspace'&&action.id!==initialWorkspaceId){
      setError('请在工作间顶部选择项目和代码目录。');return;
    }
    applyUI(action);
  };
  const refresh = () => query.invalidateQueries();
  useEffect(() => {
    saveJSON("workroom-layout:"+initialWorkspaceId, ui);
  }, [ui]);
  useEffect(() => {
    const warn = () =>
      setError(
        "本机草稿存储空间不足。当前编辑仍在内存中，请保存到工程文件后再关闭窗口。",
      );
    window.addEventListener("workbench-storage-error", warn);
    return () => window.removeEventListener("workbench-storage-error", warn);
  }, []);
  useEffect(() => {
    // 写请求的回复可能未确认；核对原回执并显示提示，避免用户误判结果后直接重做。
    if (!visible) return;
    const check = () =>
      reconcileReceipts().then((notes) => {
        if (notes.length) setError(notes.join("\n"));
      });
    check();
    const timer = setInterval(check, 5000);
    return () => clearInterval(timer);
  }, [visible]);
  useEffect(() => {
    const view = editor.current?.view;
    if (view && active?.line && !positions[active.path]) {
      const line = view.state.doc.line(
        Math.max(1, Math.min(active.line, view.state.doc.lines)),
      );
      view.dispatch({ selection: { anchor: line.from }, scrollIntoView: true });
    }
  }, [active?.path, active?.line]);
  useEffect(() => {
    saveJSON("workroom-drafts:"+initialWorkspaceId, drafts);
  }, [drafts]);
  useEffect(() => {
    persistWindowTabs(initialWorkspaceId,primary,tabs);
  }, [initialWorkspaceId,primary,tabs]);
  const policy = bootstrap.data?.policy;
  useEffect(() => {
    if (policy) setDefaultPolicy(policy);
  }, [policy, setDefaultPolicy]);
  const connection = useQuery({
    queryKey: ["connection"],
    queryFn: () => api("/api/health?full=1"),
    enabled: visible,
    refetchInterval: visible ? 10000 : false,
  });
  const disk = useQuery({
    queryKey: ["disk", active?.workspaceId, active?.path],
    queryFn: () =>
      api(
        wsPath(active!.workspaceId, "file") +
          "?path=" +
          queryString(active!.path),
      ),
    enabled: visible && !!active && !active.readOnly && active.workspaceId === ui.workspaceId,
    retry: (count, error) => !needsFileReader(error) && count < 3,
    refetchInterval: visible ? 5000 : false,
  });
  const largeOnDisk = disk.error instanceof APIError && disk.error.code === "large_file";
  useEffect(() => {
    if (!visible || !needsFileReader(disk.error) || !active || active.readOnly || active.draft !== active.text ||
        fileLocks.has(active.instance)) return;
    setTabs(old => old.map(file => {
      if (file.instance !== active.instance || file.readOnly || file.draft !== file.text) return file;
      const { instance, workspaceId, path, line } = file;
      return { instance, workspaceId, path, line, readOnly: true };
    }));
  }, [visible, disk.error, active]);
  const [changeCursor, setChangeCursor] = useState({after: 0, snapshot: ""});
  const gitHead = useQuery<GitHead>({
    queryKey: ["git-head", w?.id],
    queryFn: () => api(base + "/git?view=head"),
    enabled: visible && !!w,
    refetchInterval: visible ? 5000 : false,
  });
  const showingChanges = visible && !!w && ui.navigation === "changes";
  const changes = useQuery<GitChangesPage>({
    queryKey: ["git-changes", w?.id, changeCursor.after, changeCursor.snapshot],
    queryFn: () => api(base + "/git?view=changes&after=" + changeCursor.after +
      (changeCursor.snapshot ? "&snapshot=" + queryString(changeCursor.snapshot) : "")),
    enabled: showingChanges,
    retry: false,
  });
  const git = showingChanges ? changes.data?.git : undefined;
  const previewOnly = !!git?.available && !git.revision;

  const revisionLabel = (revision: string) =>
    git?.revision ? (revision === git.revision ? "当前版本" : "历史版本") : t("记录版本");

  function refreshChanges() {
    setDiffFile("");
    if (changeCursor.after) setChangeCursor({after: 0, snapshot: ""});
    else void changes.refetch();
  }

  const files = useQuery<{entries:FileEntry[]}>({
    queryKey: ["files", w?.id, ''],
    queryFn: () => api(base + "/files?path="),
    enabled: visible && !!w&&ui.navigation==='files',
  });
  const readingDirectory = w ? files.isPending : bootstrap.isPending || bootstrap.isFetching;
  const directoryError = w ? files.isError : !readingDirectory;
  const matches = useQuery({
    queryKey: ["search", w?.id, search, contentSearch],
    queryFn: () =>
      api(
        base +
          "/search?q=" +
          queryString(search) +
          "&content=" +
          (contentSearch ? "1" : "0"),
      ),
    enabled: visible && !!w && ui.navigation==='search'&&search.length > 1,
  });
  const threads = useQuery<{ threads: Thread[] }>({
    queryKey: ["threads", w?.id],
    queryFn: () => api(base + "/threads"),
    enabled: visible && !!w && (ui.rightVisible||ui.navigation==="tasks"||ui.navigation==="inbox"),
    refetchInterval: visible && (ui.rightVisible||ui.navigation==="tasks"||ui.navigation==="inbox") ? 2000 : false,
  });
  const thread = threads.data?.threads.find((t) => t.id === ui.threadId);
  const legacyFeed=useRef<HTMLDivElement>(null);
  const reader=useThreadReader(base,ui.threadId,!!thread?.runtime?.active,legacyFeed,visible&&ui.rightVisible);
  const {history,historyBefore,visibleHistory}=reader;
  useConversationScroll(legacyFeed,ui.threadId+":"+ui.rightVisible,String(visibleHistory?.events.at(-1)?.sourceOffset),!visible||!ui.rightVisible,reader.historyBefore===0);
  const liveOutput = !historyBefore && thread?.runtime?.active && thread.runtime.run_id
    ? projectConversation((history.data?.events ?? []).filter(event =>
        event.run_id === thread.runtime?.run_id && parentEvent(event)))
      .reverse().find(item => item.kind === "message" && item.streaming)?.text
    : undefined;
  const checks = useQuery<{ processes: ProcessRun[] }>({
    queryKey: ["checks", w?.id],
    queryFn: () => api(base + "/checks"),
    enabled: visible && !!w,
    refetchInterval: visible ? 2000 : false,
  });
  // 切换工程后保留的旧选择不属于当前工程，不能作为当前运行展示或交接。
  const currentRun =
    selectedRun?.workspaceId === ui.workspaceId ? checks.data?.processes.find(run=>run.id===selectedRun.id)||selectedRun : null;
  useEffect(() => {
    if (currentRun) saveJSON("selected-run-" + ui.workspaceId, currentRun.id);
  }, [currentRun?.id, ui.workspaceId]);
  useEffect(() => {
    if (!currentRun && checks.data) {
      const saved = loadJSON("selected-run-" + ui.workspaceId, "");
      const found = checks.data.processes.find((p) => p.id === saved);
      if (found) setSelectedRun(found);
    }
  }, [checks.data, ui.workspaceId]);
  const presets = useQuery<{ presets: { name: string; argv: string[] }[] }>({
    queryKey: ["presets", w?.id],
    queryFn: () => api(base + "/presets"),
    enabled: visible&&!!w&&(bottomOpen||configOpen),
  });
  const reviews = useQuery({
    queryKey: ["reviews", w?.id],
    queryFn: () => api(base + "/reviews"),
    enabled: visible&&!!w&&ui.navigation==="changes",
  });
  const diff = useQuery<{ text: string; revision: string | null; truncated: boolean }>(
    {
      queryKey: ["diff", w?.id, diffFile, staged, git?.revision ?? changes.data?.snapshot],
      queryFn: () =>
        api(
          base +
            "/diff?path=" +
            queryString(diffFile) +
            "&staged=" +
            (staged ? "1" : "0") + (previewOnly ? "&view=preview" : ""),
        ),
      enabled: showingChanges && !!git?.available && (!previewOnly || !!diffFile),
      retry: false,
    },
  );
  // 交接的是某个版本、输出字节位置的快照；仍在运行或输出不完整时保留截断标记。
  async function previewRun() {
    if (!currentRun || handoffBusy) return;
    const run = currentRun;
    setHandoffBusy(true);
    try {
      const result = await api("/api/v2/processes/" + encodeURIComponent(run.id) + "/output?after=0");
      setHandoffPreview(toolExcerpt("运行结果：" + run.argv.join(" "),
        { workspace: run.workspaceId, runId: run.id, revision: run.revision, state: result.process.state,
          exitCode: result.process.exitCode, outputBytesThrough: result.next, outputNotice: result.outputNotice || null },
        result.text, !!result.hasMore || result.process.state === "running" || !!result.outputNotice));
    } catch (e) { fail(e); } finally { setHandoffBusy(false); }
  }
  useEffect(() => {
    const e = [
      files.error,
      threads.error,
      history.error,
      checks.error,
    ].find(Boolean);
    if (e) fail(e);
  }, [
    files.error,
    threads.error,
    history.error,
    checks.error,
  ]);
  useEffect(() => {
    setDiffFile("");
    setSearch("");
    setChangeCursor({after: 0, snapshot: ""});
  }, [ui.workspaceId]);
  useEffect(() => {
    // 隐藏工具页、输入法组合和模态框期间不处理工作台快捷键，防止操作落到后台页面。
    const handler = (e: KeyboardEvent) => {
      if (
        !visible ||
        e.isComposing ||
        e.keyCode === 229 ||
        document.querySelector('[aria-modal="true"]')
      )
        return;
      const key = chord(e),
        keys = preferences.shortcuts;
      if (key === bound("files", keys) || key === bound("commands", keys)) {
        e.preventDefault();
        setCommandKind(key === bound("commands", keys) ? "command" : "file");
        setCommandOpen(true);
      }
      if (key === bound("terminal", keys)) {
        e.preventDefault();
        showTool("terminal");
      }
      if (key === bound("save", keys)) {
        e.preventDefault();
        saveFile();
      }
      if (key === bound("previous", keys) || key === bound("next", keys)) {
        e.preventDefault();
        const list = threads.data?.threads.filter((t) => !t.archived) || [];
        const index = list.findIndex((t) => t.id === current.current.threadId);
        if (list.length)
          dispatch({
            type: "thread",
            id: list[
              (index + (key === bound("next", keys) ? 1 : -1) + list.length) %
                list.length
            ].id,
          });
      }
      if (e.key === "Escape") setCommandOpen(false);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [visible, preferences.shortcuts, threads.data, active, w?.id]);
  async function chooseProject() {
    setCommandOpen(false);requestAnimationFrame(()=>{const picker=document.querySelector<HTMLDetailsElement>('.workroom-project-picker');if(picker){picker.open=true;picker.querySelector('select')?.focus();}});
  }
  // 读取较慢的文件可以在后台打开。当前标签由用户最后一次选择决定，也决定附给会话的是哪份草稿。
  async function openFile(path: string, line?: number, jump?: { request:WorkroomFileRequest; active:()=>boolean }) {
    if (!w) return false;
    const choice = ++fileChoice.current;
    const owner = w.id,
      key = owner + ":" + path;
    try {
      const opened = tabsRef.current.some(t => t.workspaceId + ":" + t.path === key);
      let file: WorkroomFile | undefined;
      if (jump || !opened) {
        const tab = { path, workspaceId: owner, instance: crypto.randomUUID() };
        try {
          const content = await api<FileContent>(base + "/file?path=" + queryString(path));
          file = { ...content, ...tab, draft: content.text };
        } catch (error) {
          if (!needsFileReader(error)) throw error;
          file = { ...tab, readOnly: true };
        }
      }
      if (jump && (!jump.active() || choice !== fileChoice.current)) return false;
      const exists = tabsRef.current.find(t => t.workspaceId + ":" + t.path === key);
      if (jump && file) {
        const modified = !!exists && !exists.readOnly && exists.draft !== exists.text;
        const matchesRecord = !file.readOnly && file.revision === jump.request.revision;
        line = !modified && matchesRecord ? jump.request.line : undefined;
        const message = modified ? "已打开未保存的文件草稿。历史行号可在代码审查中查看。"
          : file.readOnly ? "已分页只读打开。历史行号可在代码审查中查看。"
          : !matchesRecord ? "文件已更新，已打开磁盘上的版本。历史行号可在代码审查中查看。" : "";
        const git=jump.request.gitComparison;
        const gitNote=git&&!git.matches
          ? git.localHead
            ? t("当前检出与 PR 提交不同，已打开本地文件。")+" "+git.localHead.slice(0,8)+" / "+git.prHead.slice(0,8)
            : t("未能核对本地 Git 提交，已打开本地文件。")
          : "";
        setLocationNote(message||gitNote ? {path,message:[gitNote,message].filter(Boolean).join(" ")} : undefined);
        setFileJump(line ? {path,line,serial:jump.request.serial} : undefined);
        if (ui.layout === "review") dispatch({type:"layout",layout:"develop"});
        dispatch({ type:"navigation",navigation:"files" });
        setFolder(path.split("/").slice(0,-1).join("/"));
        if (exists && !modified) {
          const refreshed: WorkroomFile = {...file,instance:exists.instance,line};
          setTabs(old=>old.map(t=>t.instance===exists.instance ? refreshed : t));
        }
      } else if (!jump && choice === fileChoice.current) { setLocationNote(undefined); setFileJump(undefined); }
      if(line!==undefined)setPositions(old=>{const next={...old};delete next[path];return next;});
      if (exists && line!==undefined && line !== exists.line)
        setTabs((old) => old.map((f) => (f === exists ? { ...f, line } : f)));
      if (!exists && file) {
        const openedFile = {...file,line};
        setTabs((old) =>
          old.some((f) => f.workspaceId + ":" + f.path === key)
            ? old
            : [...old, openedFile],
        );
      }
      if (current.current.workspaceId === owner && choice === fileChoice.current) {
        dispatch({ type: "file", key });
        setCommandOpen(false);
      }
      return true;
    } catch (e) {
      if (!jump || jump.active()) fail(e);
      return false;
    }
  }
  useEffect(() => {
    if (!visible || !w || !fileRequest || fileRequest.serial === fileHandled.current ||
      fileRequest.resourceId !== binding.resourceId || fileRequest.conversationId !== context.conversationId) return;
    let active = true;
    setLocationNote(undefined);
    setError("");
    void openFile(fileRequest.path,undefined,{request:fileRequest,active:()=>active}).then(opened=>{
      if (!active) return;
      fileHandled.current = fileRequest.serial;
      if (fileRequest.agentRequestId) setAgentFileResult({serial:fileRequest.serial, opened});
    });
    return ()=>{active=false;};
  },[visible,w?.id,fileRequest,binding.resourceId,context.conversationId]);
  // 当前标签页提交到界面后才回报已打开。
  useEffect(() => {
    if (!visible || !fileRequest?.agentRequestId || agentFileResult?.serial !== fileRequest.serial) return;
    if (agentFileResult.opened && active?.path !== fileRequest.path) return;
    void api("/api/v2/workroom/file-open-response", {
      conversationId: fileRequest.conversationId, id: fileRequest.agentRequestId,
      state: agentFileResult.opened ? "opened" : "failed",
    }).catch(() => { /* The originating tool may have already stopped. */ });
    setAgentFileResult(undefined);
  }, [visible, fileRequest, agentFileResult, active?.path]);
  function newFile(){setCreating({path:folder?folder+'/':'',pending:false,error:''});}
  // 新建文件交给选中检出目录的保存入口，成功后定位新标签。
  async function createFile(){
    if(!creating||fileLocks.has('create'))return;
    const target=creating,path=target.path;
    if(!path.trim()){setCreating({...target,error:'请输入文件相对路径。'});return;}
    fileLocks.add('create');setCreating({...target,pending:true,error:''});
    try {
      await api(base + "/file", { path, text: "", create: true });
      setCreating(null);void refresh();
      await openFile(path);
    } catch (e) {
      setCreating({...target,path,pending:false,error:e instanceof Error?e.message:String(e)});
    }finally{fileLocks.delete('create');}
  }
  // 保存绑定读取时的磁盘 revision；版本冲突转入比较流程，由用户决定如何处理。
  function fileState(target:WorkroomFile,value:{pending:boolean;message:string;failed?:boolean}){
    if(tabsRef.current.some(file=>file.instance===target.instance))setFileStates(old=>({...old,[target.instance]:value}));
  }
  // 提交当前编辑草稿和原文件修订；更新返回后确认编辑仍属相同文件和版本。
  async function saveTarget(file:WorkroomFile){
    const target=tabsRef.current.find(item=>item.instance===file.instance);
    if(!target||target.readOnly||fileLocks.has(target.instance))return false;
    if(target.draft===target.text)return true;
    const expected=target.draft;fileLocks.add(target.instance);fileState(target,{pending:true,message:'正在保存…'});
    try {
      // 请求固定本次草稿快照；回包更新已保存版本，等待期间继续输入的新内容保留为草稿。
      const saved = await api(wsPath(target.workspaceId,'file'), {
        path: target.path,
        text: expected,
        expectedRevision: target.revision,
      });
      setTabs((old) =>
        old.map((f) =>
          f.instance===target.instance&&!f.readOnly&&f.revision===target.revision
            ? {
                ...f,
                ...saved,
                draft: f.draft === expected ? saved.text : f.draft,
              }
            : f,
        ),
      );
      const latest=tabsRef.current.find(item=>item.instance===target.instance);
      const clean=!!latest&&!latest.readOnly&&latest.draft===saved.text&&latest.revision===saved.revision;
      fileState(target,{pending:false,message:clean?'已保存':'已保存提交版本，后续编辑仍未保存。'});
      void refresh();return clean;
    } catch (e) {
      let message=e instanceof Error?e.message:String(e);
      if (e instanceof APIError && e.code === "revision_conflict"&&tabsRef.current.some(file=>file.instance===target.instance)) {
        try{
          const disk=await api(wsPath(target.workspaceId,'file')+'?path='+queryString(target.path));
          const latest=tabsRef.current.find(file=>file.instance===target.instance);
          if(latest&&!latest.readOnly)setConflict({key:target.workspaceId+':'+target.path,instance:target.instance,disk,draft:latest.draft});
        }catch(readError){message+='；磁盘版本读取失败：'+(readError instanceof Error?readError.message:String(readError));}
      }
      fileState(target,{pending:false,message,failed:true});return false;
    }finally{fileLocks.delete(target.instance);}
  }
  function saveFile(){return active?saveTarget(active):Promise.resolve(false);}
  // 由页签实例关闭编辑器状态；按原顺序为活动页签找到相邻项，再恢复操作焦点。
  function removeFile(file:WorkroomFile){
    const index=tabsRef.current.findIndex(item=>item.instance===file.instance);if(index<0)return;
    const remaining=tabsRef.current.filter(item=>item.instance!==file.instance),key=file.workspaceId+':'+file.path;
    const next=current.current.activeFile===key?remaining[Math.min(index,remaining.length-1)]:remaining.find(item=>item.workspaceId+':'+item.path===current.current.activeFile);
    const nextKey=next?next.workspaceId+':'+next.path:'';
    setTabs(()=>remaining);if(current.current.activeFile===key){fileChoice.current++;dispatch({type:'file',key:nextKey});}
    setFileStates(old=>{const value={...old};delete value[file.instance];return value;});
    requestAnimationFrame(()=>{const root=container.current;if(!root||root.closest('[hidden],[inert]'))return;
      const target=Array.from(root.querySelectorAll<HTMLButtonElement>('[data-file-select]')).find(item=>item.dataset.fileSelect===nextKey)||root.closest('.project-workroom')?.querySelector<HTMLButtonElement>('[data-workroom-quick-open]');target?.focus();});
  }
  function closeFile(file:WorkroomFile){const latest=tabsRef.current.find(item=>item.instance===file.instance);if(!latest)return;if(!latest.readOnly&&(latest.draft!==latest.text||fileLocks.has(file.instance)))setClosing(latest);else removeFile(latest);}
  // 保留编辑草稿并读回当前文件，帮助核对与磁盘版本的差异。
  async function compareDisk() {
    if (!active || active.readOnly) return;
    try {
      const disk = await api(
        wsPath(active.workspaceId, "file") +
          "?path=" +
          queryString(active.path),
      );
      setConflict({
        key: active.workspaceId + ":" + active.path,
        instance:active.instance,
        disk,
        draft: active.draft,
      });
    } catch (e) {
      fail(e);
    }
  }

  // 以当前工作区解析运行命令并交给后端，结果由原进程记录显示。
  async function runCheck() {
    if(commandLocks.has('run'))return;
    try {
      const argv=commandArguments(argvText);commandLocks.add('run');setRunPending(true);setCommandStatus('正在提交运行…');
      const r = await api<ProcessRun>(base + "/checks", { argv });
      setSelectedRun(r);
      showTool("checks");
      setCommandStatus('已提交运行 · '+presetName);void refresh();
    } catch (e) {
      setCommandStatus(e instanceof Error?e.message:String(e));showTool('checks');if(argvError)setConfigOpen(true);
    }finally{commandLocks.delete('run');setRunPending(false);}
  }
  function attachLog(text:string){setHandoffPreview(toolExcerpt('运行输出',{workspace:initialWorkspaceId,processId:currentRun?.id,revision:currentRun?.revision},text,true));}
  // 把检查栈或日志里的文件位置换算为工作区相对路径，再交给编辑器定位源码。
  function navigateStack(path: string, line: number) {
    const normalized = path.replaceAll("\\", "/"),
      root = (w?.path || "").replaceAll("\\", "/");
    const relative = normalized
      .toLowerCase()
      .startsWith(root.toLowerCase() + "/")
      ? normalized.slice(root.length + 1)
      : normalized;
    openFile(relative, line);
  }
  // 对原环境配置修订提交用户选择的命令预设。
  async function savePreset() {
    if(commandLocks.has('preset'))return;
    const snapshot={name:presetName,argv:argvText};
    try {
      const argv=commandArguments(snapshot.argv);
      if(!snapshot.name.trim())throw Error('请输入预设名称。');
      if(!presets.data||presets.error)throw Error('请先成功读取现有预设，再保存配置。');
      commandLocks.add('preset');setPresetPending(true);setCommandStatus('正在保存配置…');
      await api(base + "/presets", {
        presets: [
          ...presets.data.presets.filter((p) => p.name !== snapshot.name),
          { name: snapshot.name, argv },
        ],
      });
      setPresetBaseline({...snapshot,saved:true});setCommandStatus('配置已保存 · '+snapshot.name);void refresh();
    } catch (e) {
      setCommandStatus(e instanceof Error?e.message:String(e));
    }finally{commandLocks.delete('preset');setPresetPending(false);}
  }
  function applyPreset(preset:{name:string;argv:string[]}){const argv=JSON.stringify(preset.argv);setPresetName(preset.name);setArgvText(argv);setPresetBaseline({name:preset.name,argv,saved:true});setPendingPreset(null);}
  function choosePreset(preset:{name:string;argv:string[]}){if(configDirty)setPendingPreset(preset);else applyPreset(preset);}
  // 选中的工作区决定终端创建、读取与输入的原进程身份。
  async function terminal() {
    if (terminalBlock) {
      showTool("terminal");
      return;
    }
    try {
      const r = await api<ProcessRun>(base + "/terminals", { confirm: true });
      setSelectedRun(r);
      showTool("terminal");
      await refresh();
    } catch (e) {
      fail(e);
    }
  }
  // Git 操作携带当前观察到的 revision，仓库并发变化由后端校验后拒绝或执行。
  async function gitAction(
    action: string,
    paths: string[] = [],
    extra: object = {},
  ) {
    if (!git?.revision) return;
    try {
      await api(base + "/git", {
        action,
        paths,
        expectedRevision: git?.revision,
        ...extra,
      });
      await refresh();
    } catch (e) {
      fail(e);
    }
  }
  // 引用选区或当前草稿，可能包含未保存修改；附带的 revision 仍对应已读取的磁盘版本。
  // 由用户从工作间选中的草稿或选区生成引用，保留磁盘版本与未保存状态供会话核对。
  function fileReference(){
    if(!active||active.readOnly)return;const view=editor.current?.view,range=view?.state.selection.main;
    const excerpt=view&&range&&!range.empty?view.state.sliceDoc(range.from,range.to):active.draft;
    const lines=view&&range&&!range.empty?{fromLine:view.state.doc.lineAt(range.from).number,toLine:view.state.doc.lineAt(Math.max(range.from,range.to-1)).number}:{};
    return toolExcerpt('文件：'+active.path,{workspace:active.workspaceId,path:active.path,...lines,savedRevision:active.revision,unsaved:active.draft!==active.text},excerpt);
  }
  function addContext(){const value=fileReference();if(value)setHandoffPreview(value);}
  // 审查意见绑定正在查看的差异版本，避免刷新前的意见被套到已经变化的补丁上。
  async function addReview() {
    if (!diff.data?.revision || !reviewText.trim()) return;
    try {
      await api(base + "/reviews", {
        path: diffFile,
        line: reviewLine,
        text: reviewText,
        expectedRevision: diff.data.revision,
      });
      setReviewText("");
      await refresh();
    } catch (e) {
      fail(e);
    }
  }
  const commands = [
    ["查看旧任务记录", () => {dispatch({type:"navigation",navigation:"tasks"});if(!ui.rightVisible)dispatch({type:"right"});}],
    ["交给当前会话", addContext],
    ["打开项目", chooseProject],
    ["保存文件", saveFile],
    ["开发布局", () => dispatch({ type: "layout", layout: "develop" })],
    ["排障布局", () => dispatch({ type: "layout", layout: "debug" })],
    ["审查布局", () => dispatch({ type: "layout", layout: "review" })],
    ["新建终端", terminal],
    ["运行当前配置", runCheck],
    ["设置", () => openSettings()],
  ] as const;
  const selectedChecks = checks.data?.processes || [];
  const matched=canHandoffCurrent&&context.conversationId===conversationId&&currentScope.data?.revision===binding.scopeRevision;
  const openFiles=tabs.filter(file=>file.workspaceId===ui.workspaceId);
  const cursorDraft=active&&!active.readOnly?active.draft:'';
  const cursorText=useMemo(()=>normalizeEditorText(cursorDraft),[cursorDraft]);
  const offset=active?Math.min(positions[active.path]?.head??0,cursorText.length):0;
  const cursor=useMemo(()=>{
    const before=cursorText.slice(0,offset);
    return {line:before.split('\n').length,column:offset-before.lastIndexOf('\n')};
  },[cursorText,offset]);
  return (
    <div ref={container}
      className={
        "workbench workroom-workspace layout-" +
        ui.layout +
        (preferences.compact ? " compact" : "")
      }
      data-theme={theme}
      style={
        {
          fontSize: preferences.fontSize + "px",
          "--editor-font": "var(--app-code-size)",
        } as any
      }
    >
      {visible&&<WorkroomToolbar>
        <button type="button" className="workroom-tree-toggle" aria-label={filesOpen?t("收起文件栏"):t("展开文件栏")} aria-expanded={filesOpen} onClick={()=>setFilesOpen(!filesOpen)}><UiIcon name="sidebar"/></button>
        <details className="workroom-actions-menu workroom-utilities"><summary aria-label={t("工作间更多操作")}><UiIcon name="more"/></summary><div onClick={event=>{if((event.target as Element).closest('button'))event.currentTarget.closest('details')?.removeAttribute('open');}}>
          <button type="button" data-workroom-quick-open aria-label={t("快速打开文件")} onClick={()=>{setCommandKind('file');setCommandOpen(true);}}>{t("快速打开文件")}</button>
          <button type="button" aria-label={t("运行当前配置")} onClick={()=>void runCheck()} disabled={!w||runPending||!!argvError}>{t("运行当前配置")}</button>
          <button type="button" onClick={()=>{dispatch({type:'navigation',navigation:'search'});setFilesOpen(true);}}>搜索项目</button>
          <button type="button" onClick={()=>{dispatch({type:'navigation',navigation:'changes'});setFilesOpen(true);}}>代码变更</button>
          <button type="button" disabled={!active || active.readOnly} onClick={addContext}>当前文件或选区</button>
          <button type="button" disabled={!currentRun||handoffBusy} onClick={()=>void previewRun()}>当前运行结果</button>
          <button type="button" disabled={!diff.data||ui.navigation!=='changes'} onClick={()=>diff.data&&setHandoffPreview(toolExcerpt('代码差异：'+(diffFile||'全部文件'),{workspace:w?.id,path:diffFile||null,revision:diff.data.revision,layer:staged?'staged':'working'},diff.data.text,diff.data.truncated))}>当前差异</button>
          <button type="button" onClick={()=>void window.workbench.chooseEditor().catch(fail)}>{t("选择外部编辑器…")}</button>
          <button type="button" onClick={()=>{dispatch({type:'navigation',navigation:'tasks'});setFilesOpen(true);if(!ui.rightVisible)dispatch({type:'right'});}}>旧任务记录</button>
          <button type="button" onClick={()=>{dispatch({type:'navigation',navigation:'inbox'});setFilesOpen(true);}}>待处理授权</button>
          {ui.rightVisible&&<button type="button" onClick={()=>dispatch({type:'right'})}>收起旧任务记录</button>}
          <button type="button" onClick={()=>openSettings()}>{t("应用设置")}</button>
        </div></details>
      </WorkroomToolbar>}
      {error && (
        <div className="error" role="alert">
          <span>{error}</span>
          <button
            onClick={() => {
              setError("");
              refresh();
            }}
          >
            {t("刷新状态")}
          </button>
          <button onClick={() => setError("")}>{t("关闭提示")}</button>
        </div>
      )}
      {locationNote?.path===active?.path && locationNote && <p className="workroom-location-note" role="status">{locationNote.message}</p>}
      <div className="workspace-body">
        <aside className="navigation" hidden={!filesOpen} aria-label={t("项目文件与导航")}>
          <div className="nav-tabs" hidden={ui.navigation==='files'}>
            {[
              ["files", "文件"],
              ["search", "搜索"],
              ["changes", "变更"],
            ].map(([id, label]) => (
              <button
                key={id}
                className={ui.navigation === id ? "selected" : ""}
                onClick={() => dispatch({ type: "navigation", navigation: id })}
              >
                {label}
                {id === "changes" && changes.data?.changeCount
                  ? ` ${changes.data.changeCount}`
                  : ""}
              </button>
            ))}
          </div>
          <div className="nav-content">
            {ui.navigation === "files" && (
              <>
                <div className="section-title workroom-tree-title"><strong>{t("文件")}</strong>
                  <button type="button" className="workroom-find-file" aria-label={t("查找文件")} title={t("查找文件") + " (" + bound("files",preferences.shortcuts) + ")"}
                    onClick={()=>{setCommandKind("file");setCommandOpen(true);}}><UiIcon name="search"/>{t("查找文件")}</button>
                  <button onClick={newFile} aria-label={t("新文件")} title={t("在 {folder} 新建文件",{folder:folder||t("项目根目录")})}><UiIcon name="plus"/></button>
                  <small title={t("根目录条目数")}>{files.data?.entries.length}</small>
                </div>
                {readingDirectory&&<p role="status">{t("正在读取目录…")}</p>}
                {directoryError&&<p role="alert">{t("目录读取失败。")}<button type="button" onClick={()=>void (w ? files : bootstrap).refetch()}>{t("重试目录")}</button></p>}
                {!files.isPending&&!files.error&&files.data?.entries.length===0&&<p className="muted">{t("当前目录没有文件。")}</p>}
                {w&&files.data&&<WorkroomFileTree base={base} workspace={w.id} entries={files.data.entries} active={active?.path}
                  folder={folder} dirty={new Set(openFiles.filter(f=>!f.readOnly&&f.text!==f.draft).map(f=>f.path))}
                  onFolder={setFolder} onFile={path=>void openFile(path)}/>}
              </>
            )}
            {ui.navigation === "search" && (
              <>
                <input
                  id="workroom-file-search"
                  aria-label="工程搜索"
                  placeholder="文件路径或代码内容"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
                {search&&<button type="button" onClick={()=>{setSearch('');container.current?.querySelector<HTMLInputElement>('#workroom-file-search')?.focus();}}>清除工程搜索</button>}
                <label>
                  <input
                    type="checkbox"
                    checked={contentSearch}
                    onChange={(e) => setContentSearch(e.target.checked)}
                  />
                  搜索文件内容
                </label>
                <p role={matches.error?'alert':'status'}>{search.length<2?'输入至少两个字符开始搜索。':matches.isFetching?'正在搜索…':matches.error?'工程搜索失败。':matches.data?.matches.length?'已显示 '+matches.data.matches.length+' 个结果':t("当前查询没有匹配结果。")}{search.length>1&&matches.error&&<button type="button" onClick={()=>void matches.refetch()}>重试搜索</button>}</p>
                {matches.data?.matches.map((m: any, i: number) => (
                  <button
                    className="search-result"
                    key={i}
                    onClick={() => openFile(m.path, m.line)}
                  >
                    <strong>
                      {m.path}:{m.line}
                    </strong>
                    <small>{m.text}</small>
                  </button>
                ))}
              </>
            )}
            {ui.navigation === "changes" && (
              <>
                <div className="section-title">
                  代码变更{" "}
                  <button onClick={() => setStaged(!staged)}>
                    {staged ? "已暂存" : "未暂存"}
                  </button>
                  <button onClick={refreshChanges}>{t("刷新")}</button>
                </div>
                {changes.isError ? (
                  <p role="alert">{changes.error instanceof APIError && changes.error.code === "git_changes_changed"
                    ? t("变更列表已变化，请从开头刷新") : t("Git 变更读取失败，请刷新。")}</p>
                ) : changes.isPending ? (
                  <p role="status">{t("正在读取 Git 变更…")}</p>
                ) : !git?.available ? (
                  <p>{git?.reason}</p>
                ) : (
                  <>
                    {git.revision && <small>revision {git.revision.slice(0, 10)}</small>}
                    {previewOnly && <p className="muted">{t("共 {count} 项变更。点选文件查看差异。", {count: changes.data!.changeCount})}</p>}
                    {git.files.map((f) => (
                      <div className="change-row" key={f.path}>
                        <button onClick={() => setDiffFile(f.path)}>
                          <b>{f.status}</b> {f.path}
                        </button>
                        {git.revision && <><button
                          title="暂存此文件"
                          onClick={() => gitAction("stage", [f.path])}
                        >
                          ＋
                        </button>
                        <button
                          title="取消暂存"
                          onClick={() => gitAction("unstage", [f.path])}
                        >
                          −
                        </button></>}
                      </div>
                    ))}
                    {changeCursor.after > 0 && <button onClick={refreshChanges}>{t("从开头刷新")}</button>}
                    {changes.data?.nextOffset != null && <button onClick={() => {
                      setDiffFile("");
                      setChangeCursor({after: changes.data!.nextOffset!, snapshot: changes.data!.snapshot});
                    }}>{t("下一批变更")}</button>}
                    {git.revision ? <>
                      <input
                        aria-label="提交说明"
                        placeholder="提交说明"
                        value={commitMessage}
                        onChange={(e) => setCommitMessage(e.target.value)}
                      />
                      <button
                        disabled={git.protected}
                        onClick={() =>
                          gitAction("commit", [], { message: commitMessage })
                        }
                      >
                        提交已暂存内容
                      </button>
                      <button
                        disabled={git.protected}
                        onClick={() => {
                          if (confirm("推送当前功能分支到 origin？"))
                            gitAction("push", [], { confirm: true });
                        }}
                      >
                        推送功能分支
                      </button>
                      <PullRequestPane
                        key={ui.workspaceId}
                        workspaceId={ui.workspaceId}
                        git={git}
                        onError={fail}
                      />
                      <button
                        onClick={() => {
                          const branch = prompt(
                            "新功能分支名称",
                            "agent/task-" + Date.now(),
                          );
                          if (branch) gitAction("branch", [], { branch });
                        }}
                      >
                        当前工作区新建功能分支
                      </button>
                    </> : <small className="muted">{t("暂存和提交需要核对完整版本。")}</small>}
                  </>
                )}
              </>
            )}
            {ui.navigation === "tasks" && (
              <>
                <p className="muted">旧任务记录。新要求请在主会话中处理。</p><button onClick={()=>setArchives(!archives)}>{archives?'显示活动任务':'含归档任务'}</button>
                {threads.data?.threads
                  .filter((t) => archives || !(t as any).archived)
                  .map((t) => (
                    <button
                      className={
                        "task-row " + (ui.threadId === t.id ? "selected" : "")
                      }
                      key={t.id}
                      onClick={() => dispatch({ type: "thread", id: t.id })}
                    >
                      <strong>{t.title}</strong>
                      <small>
                        {t.kind === "implementation" ? "可修改" : "只读"} ·{" "}
                        {(t as any).acceptedAt
                          ? !git?.revision ? "已记录验收" : (t as any).acceptedRevision === git.revision
                            ? "已验收"
                            : "验收后代码已变"
                          : t.runtime?.state || "未开始"}
                      </small>
                    </button>
                  ))}
              </>
            )}
            {ui.navigation === "workspaces" && w && (
              <WorkspacesPane
                key={w.id}
                workspace={w}
                workspaces={workspaces}
                git={gitHead.error ? undefined : gitHead.data}
                draftWorkspaces={
                  new Set(
                    tabs
                      .filter((f) => !f.readOnly && f.draft !== f.text)
                      .map((f) => f.workspaceId),
                  )
                }
                onSelect={(id) => dispatch({ type: "workspace", id })}
                onRefresh={refresh}
                onError={fail}
              />
            )}
            {ui.navigation === "inbox" && w && (
              <Inbox
                key={w.id}
                workspaceId={w.id}
                onThread={(id) => dispatch({ type: "thread", id })}
                onError={fail}
                onDone={refresh}
              />
            )}
          </div>
          {(connection.isError||connection.isPending)&&<footer>
            <span>
              {connection.isError
                ? "连接中断 · 保留草稿"
                : connection.isPending
                  ? "正在连接"
                  : "本机后端已连接 · v2"}
            </span>
            <small>
              {w?.kind === "worktree" ? "独立工作区" : "前台工作区"}
            </small>
          </footer>}
        </aside>
        <main className="editor-area">
          <WorkroomFileTabs files={openFiles} active={ui.activeFile} onSelect={key=>{fileChoice.current++;dispatch({type:'file',key});}} onClose={closeFile}/>
          <div className="editor-toolbar">
            <span>
              {ui.navigation === "changes"
                ? diffFile || "工作区全部差异"
                : active?.path.split('/').join(' › ') || t("项目工作台")}
            </span>
            {active&&!active.readOnly&&<span className="file-feedback" role={fileStates[active.instance]?.failed?'alert':'status'}>{fileStates[active.instance]?.message==='已保存'&&active.draft!==active.text?t("未保存"):fileStates[active.instance]?.message||(active.draft!==active.text?t("未保存"):'')}</span>}
            <div className="spacer" />
            <div className="workroom-edit-tools" ref={setEditTools}/>
            {active && (
              <>
                {active.readOnly ? <span>{t("只读")}</span> : <>
                {largeOnDisk ? <small>{t("磁盘文件已超过编辑上限，现有草稿已保留")}</small> :
                  needsFileReader(disk.error) ? <small>{t("磁盘文件已变为二进制，现有草稿已保留")}</small> :
                  disk.data && disk.data.revision !== active.revision && <small>{t("磁盘已更新，请比较后保存")}</small>}
                <button className="workroom-save" disabled={!!fileStates[active.instance]?.pending||active.draft===active.text} onClick={()=>void saveFile()}><UiIcon name="save"/>{fileStates[active.instance]?.pending?t('保存中…'):t("保存")}</button>
                </>}
                <details className="workroom-file-options workroom-actions-menu"><summary aria-label={t("文件操作")}><UiIcon name="more"/></summary><div>
                {!active.readOnly&&<><button onClick={compareDisk}>{t("比较磁盘版本")}</button><button onClick={addContext}>{t("附到对话")}</button></>}
                <button
                  onClick={() =>
                    window.workbench
                      .openIDE(
                        active.workspaceId,
                        active.path,
                        active.line || 1,
                      )
                      .catch(fail)
                  }
                >
                  外部编辑器
                </button></div></details>
              </>
            )}
          </div>
          <div className="editor-content">
            {ui.navigation === "changes" ? (
              <div className="diff-view">
                {diff.isError ? (
                  <p role="alert">{t("差异读取失败。")}<button onClick={() => void diff.refetch()}>{t("重试")}</button></p>
                ) : diff.isPending ? (
                  <p>选择文件查看差异</p>
                ) : (
                  <>
                    <DiffReview
                      text={diff.data?.text || ""}
                      staged={staged}
                      truncated={!!diff.data?.truncated}
                      onStage={diff.data?.revision ? (patch) =>
                        gitAction("stagePatch", [], {
                          patch,
                          expectedRevision: diff.data?.revision,
                        }) : undefined
                      }
                    />
                    {diff.data?.truncated && (
                      <p>差异超过显示上限，请在外部工具查看。</p>
                    )}
                    {diff.data?.revision && <>
                      {diffFile && !staged && (
                        <button
                          disabled={diff.data?.truncated}
                          onClick={() =>
                            gitAction("stagePatch", [], {
                              patch: diff.data?.text,
                              expectedRevision: diff.data?.revision,
                            })
                          }
                        >
                          暂存当前文件完整补丁
                        </button>
                      )}
                      <button
                        disabled={!diffFile}
                        onClick={() => {
                          if (confirm("撤回当前文件的未暂存内容？后端将先备份。"))
                            gitAction("revert", [diffFile], { confirm: true });
                        }}
                      >
                        备份并撤回未暂存改动
                      </button>
                      <div className="review-form">
                        <input
                          type="number"
                          min={1}
                          aria-label="审查行号"
                          value={reviewLine}
                          onChange={(e) => setReviewLine(Number(e.target.value))}
                        />
                        <input
                          placeholder="记录此版本的问题与审查意见"
                          value={reviewText}
                          onChange={(e) => setReviewText(e.target.value)}
                        />
                        <button onClick={addReview}>记录发现</button>
                      </div>
                    </>}
                    {reviews.data?.reviews.map((r: any) => (
                      <div className="finding" key={r.id}>
                        <span>
                          {revisionLabel(r.revision)}{" "}
                          · {r.path}:{r.line}
                        </span>
                        <p>{r.text}</p>
                        <small>
                          {r.state || "open"}
                          {r.resolution ? " · " + r.resolution : ""}
                        </small>
                        {git?.revision && <button
                          onClick={() => {
                            const reason = prompt("记录本次复核依据");
                            if (reason)
                              api(base + "/reviews", {
                                id: r.id,
                                state:
                                  r.state === "resolved" ? "open" : "resolved",
                                reason,
                                expectedRevision: git.revision,
                              })
                                .then(refresh)
                                .catch(fail);
                          }}
                        >
                          {r.state === "resolved" ? "重新打开" : "标记已复核"}
                        </button>}
                        <button onClick={()=>setHandoffPreview(toolExcerpt('审查发现',{workspace:initialWorkspaceId,path:r.path,line:r.line,revision:r.revision},r.text))}>交给会话</button>
                      </div>
                    ))}
                  </>
                )}
              </div>
            ) : active?.readOnly && active.workspaceId === ui.workspaceId ? (
              <WorkroomFileReader key={active.instance} workspace={active.workspaceId} path={active.path} visible={visible}/>
            ) : active && !active.readOnly && active.workspaceId === ui.workspaceId ? (
              <EditorView
                key={active.workspaceId+":"+active.path}
                language={language}
                position={positions[active.path]}
                onPosition={position=>setPositions(old=>{const previous=old[active.path];return previous&&Object.keys(position).every(key=>previous[key as keyof EditorPosition]===position[key as keyof EditorPosition])?old:{...old,[active.path]:position};})}
                editorRef={editor}
                toolsTarget={editTools}
                path={active.path}
                line={active.line}
                lineJump={fileJump?.serial===fileRequest?.serial && fileJump?.path===active.path ? fileJump : undefined}
                value={active.draft}
                theme={theme === "light" ? "light" : "dark"}
                codeSize={preferences.codeSize ?? 12}
                onChange={(text) =>
                  setTabs((old) =>
                    old.map((f) =>
                      !f.readOnly && f.workspaceId === active.workspaceId &&
                      f.path === active.path
                        ? { ...f, draft: text }
                        : f,
                    ),
                  )
                }
                onLine={setBreakLine}
              />
            ) : (
              <div className="welcome"><p>{t('从文件栏选择文件，或按')} <kbd>{bound('files',preferences.shortcuts)}</kbd> {t('快速打开。')}</p></div>
            )}
          </div>
          <section className={'bottom-panel'+(bottomOpen?'':' is-collapsed')} aria-label={t("运行与输出")}>
            <div className="workroom-run-bar">
              <button type="button" aria-label={t(bottomOpen?'收起运行区':'展开运行区')} aria-expanded={bottomOpen} onClick={()=>setBottomOpen(!bottomOpen)}><UiIcon name="terminal"/>{t("运行与输出")} <span aria-hidden="true">{bottomOpen?'⌄':'⌃'}</span></button>
              <span className={'run-result'+(currentRun?.state==='running'||currentRun?.exitCode?' run-attention':'')} role="status">{currentRun?((({running:'运行中',completed:'已结束',failed:'未成功',stopped:'已停止',cancelled:'已取消',timed_out:'已超时'} as Record<string,string>)[currentRun.state]||currentRun.state)+(currentRun.exitCode!==null?' · 退出 '+currentRun.exitCode:'')+(currentRun.revision&&git?.revision&&currentRun.revision!==git.revision?' · 历史版本':'')):t(checks.data?.processes.some(run=>run.state==='running')?'有进程运行中，可展开选择记录':'尚无选中的运行记录')}</span>
              {bottomOpen&&<button type="button" onClick={()=>{setConfigOpen(!(configOpen&&bottom==='checks'));setBottom('checks');}} aria-expanded={configOpen&&bottom==='checks'}>{t("配置")}</button>}
              {runPending&&<small role="status">正在提交运行…</small>}{argvError&&<small>命令配置有误</small>}
            </div>
            <div className="workroom-run-content" hidden={!bottomOpen}>
            <div className="workroom-run-controls">
              <select aria-label="运行面板" value={bottom} onChange={event=>setBottom(event.target.value)}>{[['checks','输出'],['problems','问题位置'],['evidence','历史证据'],['terminal','交互终端'],['debug','Python 调试'],['jobs','旧任务记录']].map(([id,label])=><option key={id} value={id}>{label}</option>)}</select>
              <WorkroomPresetSelect presets={presets.data?.presets} current={presetName} dirty={configDirty} onChoose={choosePreset}/>
              <select className="run-record" aria-label="选择运行" value={currentRun?.id||''} onChange={event=>setSelectedRun(selectedChecks.find(run=>run.id===event.target.value)||null)}><option value="">选择运行记录</option>{selectedChecks.map(run=><option key={run.id} value={run.id}>{run.argv.join(' ')} · {run.state}{run.revision&&git?.revision&&run.revision!==git.revision?' · 历史版本':''}</option>)}</select>
            </div>
            {commandStatus&&<p className="command-feedback" role="status">{commandStatus}</p>}
            {presets.error&&<p className="command-feedback" role="alert">运行预设读取失败。<button type="button" onClick={()=>void presets.refetch()}>重试预设</button></p>}
            {bottom === "problems" && (
              <ProblemsPane
                run={currentRun}
                visible={visible&&bottomOpen}
                onOpen={navigateStack}
                onError={fail}
              />
            )}
            {bottom === "checks" && (
              <>
                {configOpen&&<div className="run-config" aria-label="运行命令配置">
                  <label>预设名称<input aria-label="运行配置名称" value={presetName} onChange={event=>setPresetName(event.target.value)}/></label>
                  <label className="command-arguments">命令参数（JSON 数组）<input aria-label="运行命令 argv" value={argvText} onChange={event=>setArgvText(event.target.value)}/></label>
                  {argvError&&<p role="alert">{argvError}</p>}
                  {!presetName.trim()&&<p role="alert">请输入预设名称。</p>}
                  <small role="status">{configDirty?'配置有未保存的编辑。':'使用已保存预设。'}</small>
                  <button type="button" disabled={presetPending||!!argvError||!presetName.trim()||!presets.data||!!presets.error} onClick={()=>void savePreset()}>{presetPending?'保存配置中…':'保存配置'}</button>
                  <button type="button" onClick={()=>setConfigOpen(false)}>完成配置</button>
                </div>}
                {currentRun ? (
                  <TerminalPane
                    key={currentRun.id}
                    run={currentRun}
                    visible={visible&&bottomOpen}
                    onError={fail}
                    onAttach={attachLog}
                    attachLabel="交给会话"
                    onNavigate={navigateStack}
                  />
                ) : (
                  <p className="muted">
                    选择预设或配置运行命令后开始。输出保留代码版本和退出状态。
                  </p>
                )}
              </>
            )}
            {bottom === "terminal" && (
              <>
                <div className="tool-title">
                  <span>用户交互终端 · 使用当前用户权限</span>
                  <button disabled={!w?.capabilities.pty||!!terminalBlock} onClick={terminal}>
                    新建真实终端
                  </button>
                  {terminalBlock && <small role="status">{terminalBlock}</small>}
                  {!terminalBlock && !w?.capabilities.pty && (
                    <small>需要后端 pywinpty / Windows ConPTY</small>
                  )}
                </div>
                {currentRun?.kind === "terminal" && (
                  <TerminalPane
                    key={currentRun.id}
                    run={currentRun}
                    visible={visible&&bottomOpen}
                    onError={fail}
                    onAttach={attachLog}
                    attachLabel="交给会话"
                    onNavigate={navigateStack}
                  />
                )}
              </>
            )}
            {bottom === "debug" && w && (
              <>
                <label>
                  断点行{" "}
                  <input
                    aria-label="断点行"
                    type="number"
                    min={1}
                    value={breakLine}
                    onChange={(e) => setBreakLine(Number(e.target.value))}
                  />
                </label>
                <DebugPane
                  key={w.id}
                  workspace={w}
                  blockedReason={debugBlock}
                  file={active?.path || ""}
                  line={breakLine}
                  onOpen={openFile}
                  onError={fail}
                />
              </>
            )}
            {bottom === "jobs" && w && (
              <JobsPane key={w.id} workspaceId={w.id} />
            )}
            {bottom === "evidence" && (
              <div className="evidence">
                {thread && (
                  <>
                    <button
                      disabled={thread.runtime?.active}
                      onClick={() => {
                        const reason =
                          prompt("本次验收依据（测试、复核与未完成项）");
                        if (reason)
                          api<GitState>(base + "/git")
                            .then(status => api(base + "/threads/" + thread.id + "/accept", {
                              reason,
                              expectedRevision: status.revision || "non-git",
                            }))
                            .then(refresh)
                            .catch(fail);
                      }}
                    >
                      按当前版本记录人工验收
                    </button>
                    <button
                      disabled={thread.runtime?.active}
                      onClick={() =>
                        api(base + "/threads/" + thread.id + "/archive", {
                          archived: !(thread as any).archived,
                        })
                          .then(refresh)
                          .catch(fail)
                      }
                    >
                      {(thread as any).archived ? "恢复任务" : "归档任务"}
                    </button>
                  </>
                )}
                <p>
                  Task {thread?.taskId || "未选择"} · Thread{" "}
                  {ui.threadId || "未选择"}
                </p>
                {selectedChecks.map((c) => (
                  <button
                    key={c.id}
                    onClick={() => {
                      setSelectedRun(c);
                      setBottom(c.kind === "terminal" ? "terminal" : "checks");
                    }}
                  >
                    {c.state} · {c.argv.join(" ")}
                    {c.revision && <> · {revisionLabel(c.revision)}</>}
                  </button>
                ))}
                {history.data?.events
                  .filter(
                    (e) =>
                      e.act === "action" ||
                      e.act === "needs_human" ||
                      e.act === "end" ||
                      e.act === "contract",
                  )
                  .map((e) => (
                    <details key={e.seq}>
                      <summary>
                        #{e.seq} {e.act} {e.status}
                      </summary>
                      <pre>{JSON.stringify(e, null, 2)}</pre>
                    </details>
                  ))}
              </div>
            )}
            </div>
          </section>
        </main>
        <WorkroomConversation key={conversationId+':'+binding.workspaceId} conversation={targetConversation} workspace={binding.workspaceId}
            visible={visible} matched={matched} legacyOpen={ui.rightVisible} chooseProject={followConversation}
            renderToggle={control=><WorkroomToolbar>{control}</WorkroomToolbar>}
            openConversation={onOpenConversation}
            attach={active&&!active.readOnly?()=>{const value=fileReference();if(matched&&value)onHandoff(value,false,true);}:undefined}
            onOpen={()=>{if(ui.rightVisible)dispatch({type:'right'});}}/>
        {ui.rightVisible && (
          <aside className="conversation">
            <div className="conversation-heading">
              <span>旧任务记录</span>
              <select
                aria-label="当前会话"
                value={ui.threadId}
                onChange={(e) =>
                  dispatch({ type: "thread", id: e.target.value })
                }
              >
                <option value="">选择任务</option>
                {threads.data?.threads.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.title}
                  </option>
                ))}
              </select>
            </div>
            <div className="run-state">
              <span>
                {thread?.kind === "implementation"
                  ? "当前权限：" + (thread.runtime?.policy || defaultPolicy)
                  : "只读讨论"}
              </span>
              <b>{thread?.runtime?.state || "未开始"}</b>
            </div>
            <div ref={legacyFeed} className="conversation-feed">
              <HistoryControls state={reader}/>
              {!thread && (
                <div className="empty-chat">
                  <h3>让问题保持聚焦</h3>
                  <p>这里显示此工作区保存的旧任务及其运行记录。</p>
                </div>
              )}
              {visibleHistory?.events
                .filter(
                  (e) =>
                    timeline ||
                    [
                      "say",
                      "user",
                      "ask_human",
                      "needs_human",
                      "error",
                      "end",
                    ].includes(e.act),
                )
                .map((e) => (
                  <article className={"event " + e.act} key={e.sourceOffset??e.seq} data-conversation-item-id={e.sourceOffset??e.seq}>
                    <small>
                      {e.act === "user"
                        ? t("你")
                        : e.act === "say"
                          ? "Agent"
                          : e.act}{" "}
                      · #{e.seq}
                    </small>
                    <Message
                      text={String(
                        e.text ||
                          e.question ||
                          e.error ||
                          e.status ||
                          (timeline ? JSON.stringify(e) : ""),
                      )}
                    />

                    {e.act === "needs_human" && (
                      <button
                        onClick={() =>
                          dispatch({ type: "navigation", navigation: "inbox" })
                        }
                      >
                        前往待处理，核对授权范围
                      </button>
                    )}
                  </article>
                ))}
            </div>
            {liveOutput && (
              <div className="live-output">
                <small>{t("正在生成")}</small>
                <pre>{liveOutput}</pre>
              </div>
            )}
            {draft&&<details className="legacy-unsent-draft"><summary>旧任务未发送草稿</summary><pre>{draft}</pre><button type="button" onClick={()=>setHandoffPreview(toolExcerpt('旧任务草稿',{workspace:initialWorkspaceId,thread:ui.threadId},draft))}>转到主会话草稿</button></details>}
            {thread?.runtime?.active&&<button type="button" onClick={()=>api(base+'/threads/'+ui.threadId+'/stop',{}).then(refresh).catch(fail)}>暂停旧任务</button>}
          </aside>
        )}
      </div>
      <footer className="workroom-statusbar"><span title="当前 Git 分支"><UiIcon name="branch"/>{gitHead.data?.branch || (gitHead.isError ? t("Git 分支暂不可用") : t("本地目录"))}</span>
        {active&&!active.readOnly&&<><span className="workroom-cursor">行 {cursor.line}，列 {cursor.column}</span><span>{active.bom?'UTF-8 BOM':'UTF-8'}</span></>}
        {active&&<span>{documentLanguage(active.path).toUpperCase()}</span>}</footer>
      {handoffPreview && <ToolHandoffDialog currentAvailable={canHandoffCurrent} value={handoffPreview} onClose={() => setHandoffPreview(undefined)}
        onApply={(value, newDraft) => { setHandoffPreview(undefined); onHandoff(value, newDraft); }} />}
      {creating&&<div className="modal-backdrop"><Dialog className="workroom-file-dialog" label="新建文件" onClose={()=>{if(!creating.pending)setCreating(null);}}>
        <h3>新建文件</h3><p>在当前项目目录创建文件，路径使用相对目录。</p>
        <form onSubmit={event=>{event.preventDefault();void createFile();}}><label>文件相对路径<input aria-label="新文件相对路径" value={creating.path} disabled={creating.pending} onChange={event=>setCreating({...creating,path:event.target.value,error:''})} onKeyDown={event=>{if((event.nativeEvent.isComposing||event.keyCode===229)&&event.key==='Enter')event.preventDefault();}}/></label>
          {creating.error&&<p role="alert">{creating.error}</p>}<button type="submit" disabled={creating.pending}>{creating.pending?'正在创建…':'创建文件'}</button><button type="button" disabled={creating.pending} onClick={()=>setCreating(null)}>{t("取消")}</button>
        </form></Dialog></div>}
      {pendingPreset&&<div className="modal-backdrop"><Dialog className="workroom-file-dialog" label="切换运行预设" onClose={()=>setPendingPreset(null)}>
        <h3>切换运行预设</h3><p>当前配置有未保存的编辑。切换到“{pendingPreset.name}”前请选择如何处理。</p>
        <button type="button" onClick={()=>applyPreset(pendingPreset)}>放弃编辑并切换</button><button type="button" onClick={()=>{setPendingPreset(null);setConfigOpen(true);showTool('checks');}}>继续编辑</button>
      </Dialog></div>}
      {closing&&<div className="modal-backdrop"><Dialog className="workroom-file-dialog" label="关闭未保存文件" onClose={()=>{if(!fileStates[closing.instance]?.pending)setClosing(null);}}>
        <h3>关闭未保存文件</h3><p>{closing.path}</p><p>选择如何处理这份文件的编辑。</p>
        {fileStates[closing.instance]?.message&&<p role={fileStates[closing.instance]?.failed?'alert':'status'}>{fileStates[closing.instance].message}</p>}
        <button type="button" disabled={!!fileStates[closing.instance]?.pending} onClick={async()=>{if(await saveTarget(closing)){setClosing(null);removeFile(closing);}}}>保存后关闭</button>
        <button type="button" disabled={!!fileStates[closing.instance]?.pending} onClick={()=>{setClosing(null);removeFile(closing);}}>丢弃并关闭</button>
        <button type="button" disabled={!!fileStates[closing.instance]?.pending} onClick={()=>setClosing(null)}>取消关闭</button>
      </Dialog></div>}
      {conflict && (
        <div className="modal-backdrop">
          <Dialog
            className="conflict-dialog"
            label="文件版本冲突"
            onClose={() => setConflict(null)}
          >
            <h3>比较磁盘与编辑草稿，选择如何继续</h3>
            <div className="conflict-columns">
              <section>
                <h4>磁盘当前版本</h4>
                <pre>{conflict.disk.text}</pre>
              </section>
              <section>
                <h4>你的编辑草稿</h4>
                <pre>{conflict.draft}</pre>
              </section>
            </div>
            <button
              onClick={() => {
                setTabs((old) =>
                  old.map((f) =>
                    f.instance===conflict.instance && !f.readOnly
                      ? { ...f, ...conflict.disk, draft: conflict.disk.text }
                      : f,
                  ),
                );
                setFileStates(old=>{const value={...old};delete value[conflict.instance];return value;});setConflict(null);
              }}
            >
              使用磁盘版本
            </button>
            <button
              onClick={() => {
                setTabs((old) =>
                  old.map((f) =>
                    f.instance===conflict.instance && !f.readOnly
                      ? { ...f, ...conflict.disk, draft: f.draft }
                      : f,
                  ),
                );
                setFileStates(old=>{const value={...old};delete value[conflict.instance];return value;});setConflict(null);
              }}
            >
              保留草稿，以新版本为基线再审阅
            </button>
            <button onClick={() => setConflict(null)}>
              继续比较，稍后处理
            </button>
          </Dialog>
        </div>
      )}
      {commandOpen&&visible&&<WorkroomQuickOpen base={base} kind={commandKind} value={quickQueries[commandKind==='command'?'command':'file']} onChange={value=>setQuickQueries(old=>({...old,[commandKind==='command'?'command':'file']:value}))} commands={commands} onOpen={openFile} onClose={()=>setCommandOpen(false)}/>}

    </div>
  );
}
