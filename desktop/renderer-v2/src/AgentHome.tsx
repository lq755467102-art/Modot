// 对话页协调草稿、资料、运行和证据入口；轮次状态、执行配置与授权范围分别读取后端记录。
// 主要引用方包括 `App.tsx`。

import { useUiLanguage } from "./UiLanguage";
import {createPortal} from "react-dom";
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useState, useRef, useImperativeHandle, type Ref } from "react";
import { UiIcon } from "./UiIcon";
import { LegacyPage, useNarrowConversationSidebar } from "./LegacyPage";
import { useConversationDock, type GitHubPageReference } from "./ConversationDock";
import { nextRunLabel, ScheduledTasks, useScheduledPlans } from "./ScheduledTasks";
import { RetainedDialog } from "./RetainedDialog";
import { Dialog } from "./Dialog";
import { LegacyContextButton, RuntimeModelBadge } from "./LegacyRunStatus";
import { ComposerModel, useComposerModel } from "./ComposerModel";
import {callResult,groupToolActivity,timelineToolCall} from "./toolActivityGroups";
import { parentEvent, pendingQuestion, projectConversation, recordedNativeRuns, roleLabel } from "./conversationProjection";
import { CodeReviewDrawer, type ReviewBookmark } from "./CodeReviewDrawer";
import { RunningChanges, useRunChanges } from "./RunChanges";
import { BrowserPane } from "./BrowserPane";
import { ConversationSidebar } from "./ConversationSidebar";
import type { ReviewLocation } from "./InlineCodeChanges";
import { ContextInspection } from "./ContextInspection";
import { SubtaskPane } from "./SubtaskPane";
import { AgentOverviewPane, RelatedChatPane, SideStartPage, SideTabs, SideToolbar, SourcesPane, WorkspaceSidePane, agentReadKey, emptySideState, sideLabels, useSubtaskOverview, type SearchSource, type SideState, type SideTab, type WebSource } from "./ConversationSideTabs";
import { ConversationWorkspace, hasSelectedWorktree, prepareConversationWorktree, type WorktreeChoice } from "./ConversationWorkspace";
import { IntegrationPane } from "./IntegrationPane";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, APIError, loadJSON, saveJSON, reconcileReceipts } from "./client";
import { useThreadReader } from "./useThreadReader";
import type { EventRow } from "./domain";
import { ConversationTimeline, type PendingUserMessage } from "./ConversationTimeline";
import { useConversationScroll } from "./useConversationScroll";
import {ForkOrigin} from "./ForkOrigin";
import { HistoryControls } from "./HistoryControls";
import {ConversationHistoryRail, roundRoute, type HistoryRound, type RoundPage} from "./ConversationHistoryRail";
import type {ChromeCommand} from "./App";
import { ComposerApproval, permissionPath, usePermissionRequests } from "./PermissionReview";
import { RepositoryImportHelp, repositoryImportIssue } from "./RepositoryImportHelp";
import { Message } from "./Panels";
import { useApplicationSettings } from "./ApplicationSettings";
import {executorBody,executorConfig,needsExecutor} from "./executorEntry";
import "./conversation-composer.css";
import { appendDraftText, type DraftTarget, type PendingHandoff, type WriteActiveDraft } from "./ToolHandoff";
import type {
  Conversation,
  Project,
  ProjectDirectory,
  Attachment,
  ConversationScope,
  RestorationPlan,
  EvidenceAction,
  ExecutionView,
  ExecutionConfig,
  Delegation,
} from "./generated";
import { DelegationControl, initialAgreement } from "./DelegationControl";
import { RestorationHistory } from "./RestorationControl";
import { ProjectScope } from "./ProjectScope";
import { chooseFolderProject } from "./projectFolder";
import {NewFolderProject} from './NewFolderProject';
import { NewConversationHome, newChatWorktreeRoots, type CreationAttempt, type SetupDraft } from "./NewConversationSetup";
import { WorkroomChangeLinks, type WorkroomFileTarget } from "./WorkroomChangeLink";
import {ResourceLinkOwner, type ResourceSidePage} from "./ResourceLinks";
import { ProjectActivity } from "./ProjectActivity";
import { AttachmentPane } from "./AttachmentPane";
import {attachmentImageKey,MessageAttachments} from "./ComposerInputs";
import {ComposerAddMenu} from "./ComposerAddMenu";
import { ArtifactPane } from "./ArtifactPane";
import ImageArtifactTab from "./ImageArtifactTab";
import { CodeActivity } from "./CodeActivity";
import { DebugEvidence } from "./DebugEvidence";
import { StageProgress } from "./StageProgress";
import type { EvidenceGap } from "./checkEvidence";
import { RoleHeartbeat } from "./RoleHeartbeat";
import { ComposerPermissions, type PermissionMode } from "./ComposerPermissions";
import { ComposerFeedback } from "./ComposerRunStatus";
import { ConversationRecordStatus } from "./ConversationRecord";
import { EvidenceInspector } from "./EvidenceInspector";
import { CorrectionQueue } from "./Corrections";
import { DeliveryReview } from "./DeliveryReview";
import { ApplicationPane } from "./ApplicationPane";
import {
  DiscussionContext,
  DiscussionDraftDialog,
  type DiscussionDraft,
} from "./DiscussionContext";
const base = "/api/v2/conversations";
type NewChatRoute = { entry: string | null; returnTo: string };
type PendingWorktree = WorktreeChoice & { reason?: string };
const defaultNewChatRoute: NewChatRoute = { entry: null, returnTo: "" };
const conversationsKey = ["conversations"] as const;
const projectsKey = ["projects"] as const;
const setupKey = (entry: string | null) => entry ? "project-draft:" + entry : "";

function creationMayHaveCompleted(error: unknown) {
  return !(error instanceof APIError) || ["unknown", "protocol", "client_version", "data_version"].includes(error.code) ||
    error.status === 202 || error.status === 408 || error.status >= 500;
}

// 草稿、待确认操作和侧栏按所有者保存，异步回执归属于发起时的会话。
const LinkedFileReview=lazy(()=>import("./LinkedFileReview"));
export function AgentHome({ onWorkbench, onOpenWorkroomFile, handoff, onHandoffHandled, onSelection, onDraftTarget, draftWriter, onNewChatBlockedReason, sidebarOpen, onHideSidebar, chromeCommand, chatActive, schedulesActive, onOpenSchedules, onOpenChat, linkPages, onCloseLinkPage, onOpenWeb, onOpenImageArtifact }: {
  onWorkbench: () => void; onOpenWorkroomFile: (file: WorkroomFileTarget) => void;
  handoff?: PendingHandoff; onHandoffHandled: () => void; onSelection: (id: string) => void;
  onNewChatBlockedReason: (reason: string) => void;
  onDraftTarget?: (target: DraftTarget | undefined) => void;
  draftWriter?: Ref<WriteActiveDraft>;
  sidebarOpen: boolean; onHideSidebar: () => void; chromeCommand?: ChromeCommand; chatActive:boolean; schedulesActive:boolean;
  onOpenSchedules:()=>void; onOpenChat:()=>void;
  linkPages: readonly ResourceSidePage[]; onCloseLinkPage: (id:string) => void;
  onOpenImageArtifact: (owner:string, artifactId:string, versionId:string, label:string) => string | undefined;
  onOpenWeb:(owner:string,url:URL)=>void;
}) {
  const { t, locale } = useUiLanguage();
  const emptyDraftFeedback = t("输入内容后发送。");
  const { preferences, theme, openSettings,openSettingsAt,closeSettings,workflowSettings,newExecution,setNewExecution, display:look, settingsVisible } = useApplicationSettings();
  const visible = chatActive && !settingsVisible;
  const narrowSidebar = useNarrowConversationSidebar();
  const sidebarDrawerOpen = visible && sidebarOpen && narrowSidebar;
  const drawerActive = useRef(sidebarDrawerOpen);
  drawerActive.current = sidebarDrawerOpen;
  const readerHeading = useRef<HTMLHeadingElement>(null);
  const consumedHandoff = useRef(0);
  useEffect(()=>{if(needsExecutor(newExecution))setNewExecution(executorConfig(newExecution));},[newExecution,setNewExecution]);
  const [selected, select] = useState("");
  const registeredDock = useConversationDock();
  const conversationDock = registeredDock?.owner === selected ? registeredDock : null;
  const [scheduleRequest, setScheduleRequest] = useState<{ id: string }>();
  // 同一个会话视口可以挂在工作间右栏，跟随视口启用事件回读。
  const conversationVisible = !settingsVisible && (chatActive || conversationDock?.expanded === true);
  // 请求跨 await 时用户可能切换会话；回写使用发起时的 owner，此引用用于决定是否跟随导航。
  const selectionRef = useRef(selected);
  const composing = useRef(false);
  selectionRef.current = selected;
  const [drafts, updateDrafts] = useState<Record<string, string>>(() =>
    loadJSON("conversation-drafts", {}),
  );
  const draftStore=useRef(drafts);
  const [setupRoute, setSetupRoute] = useState(() => loadJSON("new-chat-route", defaultNewChatRoute));
  const setupRouteRef = useRef(setupRoute);
  const [setupDrafts, setSetupDrafts] = useState<Record<string, SetupDraft>>(() => loadJSON("new-chat-setup", {}));
  const setupStore = useRef(setupDrafts);
  const [worktreeChoices, setWorktreeChoices] = useState<Record<string, PendingWorktree>>(() =>
    loadJSON("new-chat-worktree-choices", {}));
  const worktreeStore = useRef(worktreeChoices);
  function rememberWorktree(owner: string, value?: PendingWorktree) {
    const next = { ...worktreeStore.current };
    if (value) next[owner] = value;
    else delete next[owner];
    worktreeStore.current = next;
    setWorktreeChoices(next);
    saveJSON("new-chat-worktree-choices", next);
  }
  const [revealedChat, setRevealedChat] = useState<Conversation>();
  const busyKeys=useRef(new Set<string>());
  const inputOwner = selected || setupKey(setupRoute.entry);
  const setup = setupDrafts[inputOwner] ?? { projectId: setupRoute.entry, title: "", workFolder: true };
  function setDrafts(update:(old:Record<string,string>)=>Record<string,string>){
    const next=update(draftStore.current);draftStore.current=next;
    updateDrafts(next);
  }
  function saveSetup(owner: string, value?: SetupDraft) {
    const next = { ...setupStore.current };
    if (value) next[owner] = value;
    else delete next[owner];
    setupStore.current = next;
    setSetupDrafts(next);
    saveJSON("new-chat-setup", next);
  }
  // 首页草稿与已建档会话各有归属，回执只迁移发起创建的那一份。
  const [busyOwners, setBusyOwners] = useState<Record<string, boolean>>({});
  const busy = !!busyOwners[inputOwner];
  // 按草稿或已建档会话的所有者记录在途发送，供界面与并发入口共同使用。
  function setBusy(value: boolean, owner = inputOwner) {
    if(value)busyKeys.current.add(owner);else busyKeys.current.delete(owner);
    setBusyOwners((old) => old[owner]===value?old:({ ...old, [owner]: value }));
  }
  const [executionOwners, setExecutionOwners] = useState<
    Record<string, boolean>
  >({});
  const executionBusy = !!executionOwners[selected];
  // 将设置保存中的状态记在调用时的会话，切换会话后仍可核对原保存。
  function setExecutionBusy(value: boolean, owner = selected) {
    setExecutionOwners((old) => old[owner]===value?old:({ ...old, [owner]: value }));
  }
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [pendingMessages, setPendingMessages] = useState<Record<string, PendingUserMessage>>({});
  const pendingMessage = pendingMessages[inputOwner];
  const settleMessage = useCallback((owner: string, id: string, runId?: string) => {
    setPendingMessages(previous => {
      if (previous[owner]?.id !== id) return previous;
      const next = {...previous};
      if (runId) next[owner] = {...previous[owner],runId};
      else delete next[owner];
      return next;
    });
  }, []);
  function presentMessage(owner: string, text: string, attachments?: PendingUserMessage["attachments"]) {
    const message = {id:crypto.randomUUID(),text:text.trim(),submittedAt:Date.now(),attachments};
    setPendingMessages(previous=>({...previous,[owner]:message}));
    return message;
  }
  const [globalError, setGlobalError] = useState("");
  const error = errors[inputOwner] || globalError;
  // 异步错误写入操作发起时的草稿或会话所有者。
  function setError(value: string, owner = inputOwner) {
    setErrors((old) => ({ ...old, [owner]: value }));
  }
  const [seen, setSeen] = useState<Record<string, number>>(() =>
    loadJSON("conversation-seen-seq", {}),
  );
  const [discussionDrafts, setDiscussionDrafts] = useState<
    Record<string, DiscussionDraft>
  >(() => loadJSON("discussion-drafts", {}));
  const [discussionOwners, setDiscussionOwners] = useState<
    Record<string, { draft: DiscussionDraft; busy: boolean; error?: string }>
  >({});
  const discussionDraft = discussionDrafts[selected];
  useEffect(
    () => saveJSON("discussion-drafts", discussionDrafts),
    [discussionDrafts],
  );
  const [workflowPane, setWorkflowPane] = useState("materials");
  const [artifactFocus, setArtifactFocus] = useState<{owner: string; id: string; version: string}>();
  const [sourceFocus, setSourceFocus] = useState<{owner: string; run: string}>();
  const [agentFocus,setAgentFocus]=useState<{owner:string;id?:string;serial:number}>();
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [sideStates, setSideStates] = useState<Record<string, SideState>>(() => loadJSON("conversation-side-tabs", {}));
  const openedLinkPages=useRef(new Set<string>());
  const [reviews, setReviews] = useState<{owner:string;initial?:ReviewLocation;key:number}[]>([]);
  const reviewSerial = useRef(0);
  const reviewBookmarks = useRef(new Map<string,{navigation:number; position:ReviewBookmark}>());
  const newTabButton = useRef<HTMLButtonElement>(null);
  const reviewOpener = useRef<HTMLElement|null>(null);
  const side = (selected ? sideStates[selected] : undefined) ?? emptySideState;
  const visibleTab = side.collapsed ? undefined : side.active;
  const reviewOpen = visibleTab === "changes";
  const [webSources, setWebSources] = useState<Record<string, WebSource[]>>(() => loadJSON("conversation-web-sources", {}));
  const [seenSide, setSeenSide] = useState<Record<string, Record<string, string[]>>>(()=>loadJSON("side-read-content-v1",{}));
  const [displayedChanges,setDisplayedChanges]=useState<{owner:string;run:string;frames:string[]}>();
  const [displayedAgents,setDisplayedAgents]=useState<{owner:string;keys:string[]}>();
  const rememberAgents=useCallback((owner:string,keys:string[])=>setDisplayedAgents({owner,keys}),[]);
  const rememberDisplayedChanges=useCallback((owner:string,run:string,frames:string[])=>{
    setDisplayedChanges({owner,run,frames});
  },[]);
  const [sideChatRead, setSideChatRead] = useState<{tab:SideTab;owner:string;cursor:number}>();
  useEffect(()=>saveJSON("side-read-content-v1",seenSide),[seenSide]);
  useEffect(() => {
    saveJSON("conversation-side-tabs",Object.fromEntries(Object.entries(sideStates).map(([owner,state])=>{
      const tabs=state.tabs.filter(tab=>!tab.startsWith("link:"));
      return [owner,{...state,tabs,active:state.active?.startsWith("link:")?tabs.at(-1):state.active}];
    })));
  }, [sideStates]);
  useLayoutEffect(()=>{
    const added=linkPages.filter(page=>!openedLinkPages.current.has(page.id));
    openedLinkPages.current=new Set(linkPages.map(page=>page.id));
    if(!added.length)return;
    setSideStates(old=>{
      const next={...old};
      for(const page of added){
        const state=next[page.owner]??emptySideState;
        const tab:SideTab=`link:${page.id}`;
        next[page.owner]={tabs:[...state.tabs.filter(t=>t!=="start"&&t!==tab),tab],active:tab,collapsed:false};
      }
      return next;
    });
  },[linkPages]);
  useEffect(() => saveJSON("conversation-web-sources", webSources), [webSources]);
  // 更新当前会话自己的侧栏状态，新会话沿用初始布局。
  function updateSide(update: (state: SideState) => SideState) {
    if (!selected) return;
    setSideStates(old => ({...old, [selected]: update(old[selected] ?? emptySideState)}));
  }
  function openConversationDetails() {
    if (!conversationDock) return;
    conversationDock.openConversation();
  }
  // 从设置进入侧栏时先还原会话视图。
  function openSideTab(tab: SideTab) {
    if (!selected || !closeSettings(false)) return;
    openConversationDetails();
    updateSide(state => {
      const tabs=tab==="start"?state.tabs:state.tabs.filter(t=>t!=="start");
      return {tabs:tabs.includes(tab)?tabs:[...tabs,tab],active:tab,collapsed:false};
    });
  }
  // 关闭当前会话的一项，若它正被选中则回到最后保留的页签。
  function closeSideTab(tab: SideTab) {
    if(tab.startsWith("link:"))onCloseLinkPage(tab.slice(5));
    updateSide(state => {
      const tabs = state.tabs.filter(item => item !== tab);
      if(!tabs.length && tab!=="start")return {tabs:["start"],active:"start",collapsed:false};
      return {...state, tabs, active: state.active === tab ? tabs.at(-1) : state.active};
    });
  }
  // 绑定本会话与 `ReviewLocation` 的变更读取位置，重复定位同一结果也会触发抽屉导航。
  function openCodeReview(initial?:ReviewLocation) {
    if (!selected) return;
    reviewOpener.current=document.activeElement as HTMLElement;
    if (initial) {
      const key = ++reviewSerial.current;
      setReviews(old => [...old.filter(review => review.owner !== selected), {owner:selected, initial, key}]);
    }
    openSideTab("changes");
  }
  // 关闭审查后将焦点还给原触发控件，便于继续之前的阅读。
  function closeCodeReview() {
    closeSideTab("changes");
    const opener=reviewOpener.current;
    (opener?.isConnected?opener:document.querySelector<HTMLElement>('.conversation-header [aria-label="查看代码改动"]'))?.focus();
  }
  // Empty sidebars open the navigation page without implying that a worker ran.
  function toggleSide() {
    if (!side.active) {openSideTab("start"); return;}
    updateSide(state => ({...state, collapsed: !state.collapsed}));
  }
  // 网页选择成为当前草稿及来源记录，发送前用户仍可编辑。
  function quoteWeb(owner: string, text: string, source: WebSource) {
    if (!/^https?:\/\//i.test(source.url)) return;
    setDrafts(old => ({...old, [owner]:(old[owner] || "") + "\n" + text}));
    recordWebSource(owner,source);
  }
  function recordWebSource(owner: string, source: WebSource) {
    setWebSources(old => ({...old, [owner]: [
      {...source, sent: old[owner]?.find(item => item.url === source.url)?.sent},
      ...(old[owner] ?? []).filter(item => item.url !== source.url),
    ].slice(0, 50)}));
  }
  // 原动作可能来自另一个会话，审查目标独立保留 owner/run/action，切会话时关闭旧审查。
  const [evidenceAction, setEvidenceAction] = useState<string | null>(null);
  const [evidenceRun, setEvidenceRun] = useState("");
  const [evidenceOwner, setEvidenceOwner] = useState("");
  const [evidenceRequest, setEvidenceRequest] = useState(0);
  const [evidenceGap, setEvidenceGap] = useState<EvidenceGap>();
  useEffect(() => {
    setEvidenceOpen(false);
    setEvidenceAction(null);
    setEvidenceRun("");
    setEvidenceOwner("");
    setEvidenceGap(undefined);
  }, [selected]);
  // 将原动作、轮次、归属会话和缺项引用传给证据视图。
  const inspectEvidence = (id: string | null, run = "", owner = "", gap?: EvidenceGap) => {
    if(!closeSettings(false))return;
    openConversationDetails();
    setEvidenceOwner(owner);
    setEvidenceAction(id);
    setEvidenceRequest(request => request + 1);
    setEvidenceRun(run);
    setEvidenceGap(gap);
    setEvidenceOpen(true);
  };
  const [attachmentsOpen, setAttachmentsOpen] = useState(false);
  const [attachmentDrafts,setAttachmentDrafts]=useState(()=>loadJSON<Record<string,{id:string;imported:boolean}[]>>("composer-attachments-v1",{}));
  const [removingAttachments,setRemovingAttachments]=useState<Record<string,boolean>>({});
  const selectedAttachmentIds = attachmentDrafts[selected]?.map(item=>item.id) ?? [];
  useEffect(()=>saveJSON("composer-attachments-v1",attachmentDrafts),[attachmentDrafts]);
  const [projectOwners, setProjectOwners] = useState<Record<string, boolean>>(
    {},
  );
  const [attachmentOwners, setAttachmentOwners] = useState<
    Record<string, boolean>
  >({});
  const projectBusy = !!projectOwners[selected],
    attachmentBusy = !!attachmentOwners[selected] || !!removingAttachments[selected];
  // 资料范围和项目创建使用会话独立的忙碌键，避免更新时挡住无关会话。
  function setProjectBusy(value: boolean, owner = selected) {
    setProjectOwners((old) => old[owner]===value?old:({ ...old, [owner]: value }));
  }
  // `AttachmentPane.tsx` 报告导入或撤销仍在处理时，按对应会话标记忙碌。
  function setAttachmentBusy(value: boolean, owner = selected) {
    setAttachmentOwners((old) => old[owner]===value?old:({ ...old, [owner]: value }));
  }
  const scopeBusy = projectBusy || attachmentBusy || executionBusy;
  // 归一异步异常消息并交给发起操作的会话显示。
  const fail = (e: unknown, owner = selected) =>
    setError(e instanceof Error ? e.message : String(e), owner);
  const queryClient = useQueryClient();
  const conversations = useQuery<{ threads: Conversation[] }>({
    queryKey: conversationsKey,
    queryFn: () => api(base + "/threads"),
    refetchIntervalInBackground: true,
    refetchInterval: 1500,
  });
  const sidebarThreads = useMemo(() => conversations.data?.threads.filter(row => !row.subtaskOf) ?? [], [conversations.data?.threads]);
  const modelControl = useComposerModel();
  const { models } = modelControl;
  const thread = conversations.data?.threads.find((t) => t.id === selected);
  const draftOwner = thread && !conversations.error && !thread.archived && !thread.readonlyEvidence && !thread.subtaskOf
    ? thread : undefined;
  useEffect(() => {
    onDraftTarget?.(draftOwner ? {id:draftOwner.id, title:draftOwner.title} : undefined);
  }, [draftOwner?.id, draftOwner?.title, onDraftTarget]);

  // 将插件交接和输入栏添加都追加到同一份当前草稿，保留等待回执期间编辑的文字。
  function appendActiveDraft(owner: string, text: string, source?: WebSource) {
    if (!owner || draftOwner?.id !== owner || selectionRef.current !== owner) {
      throw Error("当前会话已切换或草稿不可编辑，请重新选择。");
    }
    if (source && !/^https?:\/\//i.test(source.url)) throw Error("无法核对网页来源。");
    const next = {...draftStore.current, [owner]:appendDraftText(draftStore.current[owner], text)};
    if (!saveJSON("conversation-drafts", next, true)) throw Error("草稿未能保存，请检查本机存储后重试。");
    setDrafts(() => next);
    if (source) recordWebSource(owner,source);
  }
  useImperativeHandle(draftWriter, () => appendActiveDraft);
  const schedules = useScheduledPlans(conversationVisible && !!thread?.scheduledPlanId);
  const scheduledPlan = schedules.data?.plans.find(plan => plan.id === thread?.scheduledPlanId);
  const agents = useSubtaskOverview(thread?.subtaskOf ? "" : selected);
  useEffect(()=>{
    if(!selected||!agents.isSuccess||agents.error||agents.data.tasks.length||!side.tabs.includes("agents"))return;
    updateSide(state=>{
      const tabs=state.tabs.filter(tab=>tab!=="agents");
      return {...state,tabs:tabs.length?tabs:["start"],active:state.active==="agents"?(tabs.at(-1)??"start"):state.active};
    });
  },[selected,agents.isSuccess,agents.error,agents.data,side.tabs]);
  const related = (conversations.data?.threads ?? []).filter(row => !row.subtaskOf && !row.archived &&
    (row.discussionOf?.conversationId === selected || row.questionOf?.conversationId === selected || row.forkOf?.conversationId === selected));
  useEffect(() => {
    if (thread?.subtaskOf) select(thread.subtaskOf.parentId);
  }, [thread?.subtaskOf]);
  // 切换会话前保留尚未登记的草稿或其在途写入归属，再切换当前选择。
  function showConversation(id: string) {
    if(!closeSettings(false))return;
    if(id!==selected){setHistoryMenuOwner("");setRoundJump(undefined);latestRequested.current=false;}
    selectionRef.current=id;select(id);
  }
  function closeSidebarDrawer() {
    if (!drawerActive.current) return false;
    onHideSidebar();
    return true;
  }
  function openScheduledPage(planId?: string) {
    setHistoryMenuOwner("");
    if (planId) setScheduleRequest({ id: planId });
    onOpenSchedules();
    closeSidebarDrawer();
  }
  function closeScheduledPage() {
    onOpenChat();
    requestAnimationFrame(() => readerHeading.current?.focus({ preventScroll: true }));
  }
  function selectFromSidebar(id: string, focusReader = false) {
    showConversation(id);
    if (closeSidebarDrawer() || focusReader) requestAnimationFrame(() => {
      if (selectionRef.current === id && !drawerActive.current) readerHeading.current?.focus({ preventScroll: true });
    });
  }
  function showNewChatSetup(projectId: string | null) {
    const route = { entry: projectId, returnTo: selectionRef.current || setupRouteRef.current.returnTo };
    setupRouteRef.current = route;
    setSetupRoute(route);
    saveJSON("new-chat-route", route);
    showConversation("");
    closeSidebarDrawer();
    requestAnimationFrame(() => {
      if (setupRouteRef.current === route && !selectionRef.current && !drawerActive.current) readerHeading.current?.focus({ preventScroll: true });
    });
  }
  const execution = useQuery<ExecutionView>({
    queryKey: ["execution", selected],
    queryFn: () => api(base + "/threads/" + selected + "/execution"),
    enabled: !!selected,
    refetchInterval: 3000,
  });
  const currentScope = useQuery<ConversationScope>({
    queryKey: ["scope", selected],
    queryFn: () =>
      api(base + "/threads/" + encodeURIComponent(selected) + "/scope"),
    enabled: !!selected,
    refetchInterval: 3000,
    refetchIntervalInBackground: !!selected,
  });
  const chosenWorktree = worktreeChoices[selected];
  const worktreePending = chosenWorktree && (currentScope.error || !hasSelectedWorktree(currentScope.data, chosenWorktree));
  useEffect(() => {
    if (chosenWorktree && !worktreePending) rememberWorktree(selected);
  }, [selected, chosenWorktree, worktreePending]);
  const messageElement = useRef<HTMLElement>(null);
  const historyState = useThreadReader(base, selected, !!thread?.runtime?.active, messageElement, conversationVisible);
  const { history, visibleHistory, historyBefore } = historyState;
  const canOpenWorkroom = useRef(conversationVisible);
  canOpenWorkroom.current = conversationVisible;
  const lastFileRequest = useRef("");
  const workroomOpen = history.data?.events.filter(event =>
    event.act === "workroom_open_requested" && parentEvent(event)).at(-1)?.workroomOpen as
    { id: string; expiresAt: number } | undefined;
  const openRequestId = workroomOpen?.id;
  const openRequestExpiry = workroomOpen?.expiresAt;
  useEffect(() => {
    if (!selected || !conversationVisible || !openRequestId || !openRequestExpiry ||
        Date.now() >= openRequestExpiry * 1000 || lastFileRequest.current === openRequestId) return;
    lastFileRequest.current = openRequestId;
    const route = "/api/v2/workroom/file-open-response";
    const request = { conversationId: selected, id: openRequestId };
    void api<WorkroomFileTarget>(route, { ...request, state: "claimed" }).then(target => {
      if (selectionRef.current === selected && canOpenWorkroom.current &&
          target.conversationId === selected && Date.now() < openRequestExpiry * 1000) {
        onOpenWorkroomFile({ ...target, agentRequestId: openRequestId });
      } else {
        return api(route, { ...request, state: "failed" });
      }
    }).catch(() => { /* Late and revoked requests never navigate the workroom. */ });
  }, [selected, conversationVisible, openRequestId, openRequestExpiry, onOpenWorkroomFile]);
  const currentRunId = thread?.runtime?.run_id;
  const lastGitHubRequest = useRef("");
  const githubRequest = history.data?.events.filter(event =>
    event.act==="github_page_requested" && event.run_id===currentRunId && parentEvent(event)).at(-1)?.githubPageRequest as
    {id:string;expiresAt:number}|undefined;
  useEffect(()=>{
    if(!conversationDock?.navigateGitHubPage || !conversationVisible || !githubRequest ||
      Date.now()>=githubRequest.expiresAt*1000 || lastGitHubRequest.current===githubRequest.id)return;
    lastGitHubRequest.current=githubRequest.id;
    void conversationDock.navigateGitHubPage(githubRequest.id).catch(()=>{});
  },[conversationDock,conversationVisible,githubRequest]);
  const awaitingQuestion = pendingQuestion(history.data?.events ?? [], currentRunId);
  const answerRunId = awaitingQuestion ? currentRunId : undefined;
  const executionRun = execution.error ? undefined : execution.data?.current;
  const currentConfig = executionRun?.runId === currentRunId ? executionRun?.config : undefined;
  const refetchExecution = execution.refetch;
  useEffect(() => {
    if (answerRunId && executionRun?.runId !== answerRunId) void refetchExecution();
  }, [answerRunId, executionRun?.runId, refetchExecution]);
  const runIdForOverview = thread?.runtime?.run_id || "";
  const currentNativeRuns = useMemo(() => recordedNativeRuns(history.data?.events ?? [], executionRun), [history.data?.events, executionRun]);
  const pastNativeRuns = useMemo(() => recordedNativeRuns(historyBefore ? visibleHistory?.events ?? [] : []), [historyBefore, visibleHistory?.events]);
  const nativeRun = currentNativeRuns.has(runIdForOverview);
  const nativeEnd = nativeRun ? history.data?.events.filter(event =>
    event.act === "end" && event.run_id === runIdForOverview && parentEvent(event) && (!event.role || event.role === "executor")).at(-1) : undefined;
  useEffect(() => {
    if (nativeEnd?.run_id && thread?.runtime?.active) void conversations.refetch();
  }, [nativeEnd?.run_id, thread?.runtime?.active, conversations.refetch]);
  const lastVisible = visibleHistory?.events.at(-1);
  const scroll = useConversationScroll(messageElement, selected,
    `${visibleHistory?.generation}:${lastVisible?.sourceOffset}:${lastVisible?.seq}:${pendingMessage?.id??""}`, settingsVisible, historyBefore === 0);
  const [roundJump, setRoundJump] = useState<{owner:string; id:string; round?:HistoryRound; instant?:boolean}>();
  const [historyMenuOwner, setHistoryMenuOwner] = useState("");
  const landedResult = useRef<{node:HTMLElement; timer:number}|undefined>(undefined);
  useEffect(()=>()=>{
    if(landedResult.current){clearTimeout(landedResult.current.timer);delete landedResult.current.node.dataset.historyLanded;landedResult.current=undefined;}
  },[selected]);
  const latestRequested = useRef(false);
  useLayoutEffect(()=>{if(!historyBefore&&latestRequested.current){latestRequested.current=false;scroll.toLatest();}},[historyBefore]);
  // 轮次定位先切到对应历史页，等内容挂载后再滚动；过期会话的跳转请求不参与当前定位。
  const jumpRequest = roundJump?.owner===selected ? roundJump : undefined;
  const lookup = useQuery({queryKey:["history-round",selected,jumpRequest?.id],
    queryFn:()=>api<RoundPage>(roundRoute(selected)+"?run="+encodeURIComponent(jumpRequest!.id)),
    enabled:!!jumpRequest&&!jumpRequest.round,retry:(count,error)=>"status" in error&&error.status===425&&count<4});
  const jumpRound = jumpRequest?.round ?? lookup.data?.rounds[0];
  useEffect(()=>{
    if(!jumpRequest || !jumpRound || !history.data || !visibleHistory) return;
    const node=messageElement.current;
    if(!node) return;
    const round=jumpRound;
    const matches=(event:EventRow)=>event.run_id===round.id&&(round.active||event.sourceOffset===round.offset);
    if(historyBefore&&(round.active||(!visibleHistory.events.some(matches)&&historyState.liveWindow?.events.some(matches)))){
      setRoundJump({...jumpRequest,instant:true});historyState.setHistoryBefore(0);return;
    }
    if(!visibleHistory.events.some(matches)&&round.before&&historyBefore!==round.before){
      setRoundJump({...jumpRequest,instant:true});historyState.setHistoryBefore(round.before);return;
    }
    const frame=requestAnimationFrame(()=>{
      const items=[...node.querySelectorAll<HTMLElement>(`[data-history-run="${CSS.escape(round.id)}"]`)].filter(item=>item.getClientRects().length);
      const anchor=round.active?items.at(-1):items.find(item=>item.dataset.historyOffset===String(round.offset))??items.at(-1);
      if(anchor){
        const distance=anchor.getBoundingClientRect().top-node.getBoundingClientRect().top-22;
        const reduced=matchMedia("(prefers-reduced-motion: reduce)").matches;
        scroll.jumpTo(node.scrollTop+distance,!reduced&&!jumpRequest.instant&&Math.abs(distance)<node.clientHeight);
        if(landedResult.current){clearTimeout(landedResult.current.timer);delete landedResult.current.node.dataset.historyLanded;}
        if(!reduced){
          anchor.dataset.historyLanded="true";
          landedResult.current={node:anchor,timer:window.setTimeout(()=>{delete anchor.dataset.historyLanded;landedResult.current=undefined;},700)};
        }
      }
      else setError("此轮没有可定位的正文，原始记录可能已移除。");
      setRoundJump(undefined);
    });
    return()=>cancelAnimationFrame(frame);
  },[jumpRequest,jumpRound,history.data,visibleHistory,historyBefore]);
  useEffect(()=>{if(jumpRequest&&lookup.error){setError(lookup.error.message);setRoundJump(undefined);}},[jumpRequest,lookup.error]);
  useEffect(()=>{if(jumpRequest&&jumpRound?.before===historyBefore&&historyState.past.error){setError(historyState.past.error.message);setRoundJump(undefined);}},[jumpRequest,jumpRound,historyBefore,historyState.past.error]);
  // 只有最新历史成功读取后才更新已读游标；浏览旧页不将旧位置写作最新已读位置。
  useEffect(() => {
    if (selected && conversationVisible && !historyBefore && history.data && !history.error) {
      const total = Math.max(
        0,
        ...history.data.events.map((row) => row.seq || 0),
      );
      setSeen((old) =>
        old[selected] === total ? old : { ...old, [selected]: total },
      );
    }
  }, [selected, conversationVisible, historyBefore, history.data, history.error]);
  useEffect(() => saveJSON("conversation-seen-seq", seen), [seen]);
  const draft = drafts[inputOwner] || "";
  // 附件取消可能尚待核对；保留本地标记，重新打开附件流程并暂停发送。
  const pendingCancellation = selected
    ? loadJSON<string>("attachment-cancel-" + selected, "")
    : "";
  useEffect(() => {
    setAttachmentsOpen(!!(selected && loadJSON<string>("attachment-cancel-" + selected, "")));
  }, [selected]);
  useEffect(() => saveJSON("conversation-drafts", drafts), [drafts]);
  useEffect(() => onSelection(selected), [selected, onSelection]);
  useEffect(() => {
    if (!handoff || consumedHandoff.current === handoff.id) return;
    // The owner was captured at the human handoff. A delayed render must not
    // append this reference to a subsequently selected unrelated conversation.
    consumedHandoff.current = handoff.id;
    let owner = handoff.owner;
    if (owner) showConversation(owner);
    else {
      showNewChatSetup(null);
      if (setupStore.current[""]?.attempt) owner = "local-draft:" + crypto.randomUUID();
    }
    setDrafts(old => ({ ...old, [owner]: appendDraftText(old[owner], handoff.text) }));
    onHandoffHandled();
  }, [handoff, onHandoffHandled]);
  useEffect(() => {
    // 收到 `client.ts` 的本地草稿存储告警时提醒保留输入。
    const warn = () =>
      setGlobalError("草稿存储空间不足，请复制保存输入内容后再关闭。");
    window.addEventListener("workbench-storage-error", warn);
    // 轮询 `client.ts` 保留的写入回执，把仍需确认的事实反馈到会话界面。
    const check = () =>
      reconcileReceipts()
        .then((notes) => {
          if (notes.length) setGlobalError(notes.join("\n"));
        })
        .catch((e) =>
          setGlobalError(e instanceof Error ? e.message : String(e)),
        );
    check();
    const timer = setInterval(check, 5000);
    return () => {
      clearInterval(timer);
      window.removeEventListener("workbench-storage-error", warn);
    };
  }, []);
  const modelUnavailable = modelControl.saving ? t("正在保存默认模型…")
    : models.isPending ? t("正在读取模型设置…")
    : models.error || models.data?.error ? t("模型设置读取失败，请在模型入口核对。") : "";
  const firstSendBlocked = modelUnavailable || (!models.data?.effective?.main?.model ? t("请先选择可用模型。") : "");
  // 统一核对会话是否可发送、运行收尾和资料范围。
  function sendReason(text:string){
    if(thread?.archived)return t('此对话已归档，草稿保留。');
    if(thread?.subtaskOf)return t('请返回主会话继续输入。');
    if(busy||busyKeys.current.has(inputOwner))return t('正在提交，草稿仍可继续编辑。');
    if(nativeEnd&&thread?.runtime?.active)return t('正在同步本轮结束状态…');
    if(scopeBusy)return t('正在导入资料或保存设置，请稍候。');
    if(pendingCancellation)return t('附件取消结果待核对，请打开资料处理。');
    if(modelUnavailable)return modelUnavailable;
    if(selected&&(!currentScope.data||currentScope.error))return currentScope.error?t('资料范围读取失败，请打开资料核对。'):t('正在核对资料范围…');
    if(worktreePending)return t("所选工作树待确认，首条消息保存在草稿。");
    if(selected&&(!execution.data||execution.error))return execution.error?t('运行设置读取失败，请打开高级运行设置核对。'):t('正在核对运行设置…');
    if(awaitingQuestion&&!currentConfig&&executionRun?.state!=='retired')return t('正在核对待答轮次…');
    return text.trim() ? "" : emptyDraftFeedback;
  }
  // 将当前应用默认执行偏好作为新会话首份设置写入，续接已知建档身份。
  async function initializeDraftConversation(id:string){
    return api<ExecutionView>(base+'/threads/'+encodeURIComponent(id)+'/execution',executorBody({...newExecution,revision:1}));
  }
  async function sendComposerMessage(owner: string, text: string, next: ExecutionConfig,
    scopeRevision?: string, operationId?: string, attachmentIds?: readonly string[],
    githubPage?: GitHubPageReference|null,
  ) {
    const path = base + "/threads/" + encodeURIComponent(owner) + "/send";
    const result = await api<{ runId?: string; queued?: boolean; request_id?: string; messageText?: string }>(
      path, {
        text, crew: "l0", expectedExecutionRevision: next.revision, expectedScopeRevision: scopeRevision,
        ...(attachmentIds?.length?{attachmentIds}:{}),
        ...(githubPage?{githubPage}:{}),
      }, false, operationId,
    );
    if (result.queued && result.request_id) {
      const key = ["followup-receipt", owner, result.request_id];
      queryClient.setQueryData(key, old => old ?? {id: result.request_id, path, state: "queued", response: result});
    }
    return result;
  }
  // 发送前要求资料/配置读取成功且附件取消已核对；修订号交给后端检查期间发生的并发变化。
  async function send(replyText = draft) {
    if(!selected||sendReason(replyText))return;
    const id = selected, text = replyText;
    const githubPage = conversationDock?.expanded ? conversationDock.captureGitHubPage?.() : undefined;
    const attachmentsForSend = selectedAttachmentIds;
    const outgoing = presentMessage(id,text,attachmentsForSend.flatMap(attachmentId =>
      attachments.data?.attachments.find(item=>item.id===attachmentId && item.permission==="read")??[]));
    if (historyBefore) {
      latestRequested.current = true;
      historyState.setHistoryBefore(0);
    } else scroll.toLatest();
    setBusy(true,id);
    setError('',id);
    try {
      const route = base + "/threads/" + encodeURIComponent(id);
      // The question may arrive before the execution poll catches up. Pin
      // this answer to its actual run before selecting a write endpoint.
      const settings = !answerRunId && thread?.runtime?.active ? execution.data : await api<ExecutionView>(route + '/execution');
      const answerRun = settings?.current;
      if (answerRunId && (answerRun?.runId !== answerRunId || !answerRun.config && answerRun.state !== 'retired'))
        throw new Error('待答轮次已变化或无法核对，请刷新后重试。');
      const answerConfig = answerRunId ? answerRun?.config : undefined;
      if (answerConfig?.crew === 'l4') {
        if(attachmentsForSend.length)throw Error(t("请先移除附件，再答复持续委托。"));
        if (!answerConfig.delegationId) throw new Error('待答轮次的委托身份无法核对。');
        await api(route + '/delegations/' + encodeURIComponent(answerConfig.delegationId) + '/start', {
          confirm: true, answer: text, answerRunId,
        });
        settleMessage(id,outgoing.id);
      } else {
        const next = settings?.next ?? newExecution;
        const queued = await sendComposerMessage(id, text, next, currentScope.data?.revision, outgoing.id, attachmentsForSend,await githubPage);
        settleMessage(id,outgoing.id,queued.runId);
      }
      setWebSources(old => old[id] ? {...old,[id]:old[id].map(item =>
        text.includes("地址：" + item.url + "\n") ? {...item,sent:true} : item)} : old);
      // 成功发送只清空原文，等待期间已被用户改写的草稿继续保留。
      setDrafts((old) => ({ ...old, [id]: old[id] === text ? "" : old[id] }));
      if(attachmentsForSend.length)discardAttachmentSelection(id,attachmentsForSend);
      await conversations.refetch();
      await history.refetch();
    } catch (e) {
      settleMessage(id,outgoing.id);
      fail(e,id);await conversations.refetch();
    } finally {
      setBusy(false,id);
    }
  }
  function openScope(attachments = false, show = true) {
    if (!selected || busy || scopeBusy || busyKeys.current.has(inputOwner)) return;
    if (attachments) {
      if(show)setAttachmentsOpen(true);
    } else {
      setWorkflowPane("materials");
      openSettingsAt("resources");
    }
    return selected;
  }
  const folderOpening=useRef(false);
  const creatingChat = useRef(false);
  const [projectCreation, setProjectCreation] = useState<{
    selection: string; route: NewChatRoute; trigger: HTMLElement | null;
  }>();
  const [projectRemoval, setProjectRemoval] = useState<Project>();
  const [removingProject, setRemovingProject] = useState(false);
  const [projectRemovalError, setProjectRemovalError] = useState("");
  // 所有新建入口共用草稿与创建检查，避免同时登记同一个编辑起点。
  function getNewChatBlockedReason() {
    if (creatingChat.current) return t("正在创建会话，请稍候。");
    if (folderOpening.current) return t("正在准备项目，请稍候。");
    if (busyKeys.current.has(inputOwner)) return t("正在提交，请稍候。");
    if (scopeBusy) return t("正在处理资料或设置，请稍候。");
    return "";
  }
  const newChatBlockedReason = getNewChatBlockedReason();
  useEffect(() => onNewChatBlockedReason(newChatBlockedReason), [newChatBlockedReason, onNewChatBlockedReason]);

  function startNewChat(projectId: string | null = null) {
    if (!getNewChatBlockedReason()) showNewChatSetup(projectId);
  }
  // 使用确认时选择的项目或独立目录建档，回执只迁移发起时的草稿。
  async function confirmNewChat(sendFirst: boolean) {
    if (selectionRef.current || getNewChatBlockedReason() || setupDestinationError || (sendFirst && firstSendBlocked)) return;
    const route = setupRouteRef.current;
    const owner = setupKey(route.entry);
    const requested = setupStore.current[owner] ?? setup;
    const firstText = draftStore.current[owner] || "";
    const attempt: CreationAttempt = requested.attempt ?? {
      id: crypto.randomUUID(),
      body: {
        title: Array.from(firstText.trim().split(/\r?\n/, 1)[0]).slice(0, 60).join("") || requested.title.trim() || t("新对话"),
        projectId: requested.projectId,
        workFolder: !requested.projectId,
        ...(requested.permissionMode && {permissionMode: requested.permissionMode, confirmFullAccess: requested.permissionMode === "full"}),
      },
      worktreeResourceId: requested.projectId ? requested.worktreeResourceId : undefined,
      firstMessage: sendFirst && firstText.trim() ? firstText : undefined,
    };
    const outgoing = attempt.firstMessage ? presentMessage(owner,attempt.firstMessage) : undefined;
    let pendingOwner = owner;
    creatingChat.current = true;
    setBusy(true, owner);
    setError("", owner);
    if (!requested.attempt) saveSetup(owner, { ...requested, attempt });
    let created: Conversation | undefined;
    try {
      const record = await api<Conversation>(base + "/threads", attempt.body, false, attempt.id);
      if (!record.id || record.projectId !== attempt.body.projectId) {
        throw new APIError(502, "protocol", "创建回执中的会话归属待核对，已保留草稿。");
      }
      created = record;
      setBusy(true, record.id);
      const threadRoute = base + "/threads/" + encodeURIComponent(record.id);
      const choice = record.projectId && attempt.worktreeResourceId ?
        { projectId: record.projectId, resourceId: attempt.worktreeResourceId } : undefined;
      if (choice) rememberWorktree(record.id, choice);
      let scope: ConversationScope | undefined;
      let next: ExecutionConfig | undefined;
      try {
        next = (await initializeDraftConversation(record.id)).next;
      } catch (failure) {
        setError("会话已创建，运行偏好未保存。" + (failure instanceof Error ? failure.message : String(failure)), record.id);
      }
      if (choice) {
        try {
          scope = await prepareConversationWorktree(record.id, choice);
          queryClient.setQueryData(["scope", record.id], scope);
          rememberWorktree(record.id);
        } catch (failure) {
          rememberWorktree(record.id, { ...choice, reason: failure instanceof Error ? failure.message : String(failure) });
        }
      }
      setDrafts(old => {
        const text = [...new Set([old[record.id], old[owner]].filter(Boolean))].join("\n\n");
        const drafts = { ...old, [record.id]: text };
        delete drafts[owner];
        return drafts;
      });
      saveJSON("conversation-drafts", draftStore.current);
      saveSetup(owner);
      queryClient.setQueryData<{ threads: Conversation[] }>(conversationsKey, old => ({
        threads: [record, ...(old?.threads.filter(thread => thread.id !== record.id) ?? [])],
      }));
      if (outgoing) {
        pendingOwner = record.id;
        setPendingMessages(previous=>{
          if (previous[owner]?.id !== outgoing.id) return previous;
          const next = {...previous,[record.id]:previous[owner]};
          delete next[owner];
          return next;
        });
      }
      if (!selectionRef.current && setupRouteRef.current === route) {
        showConversation(record.id);
        setRevealedChat(record);
        requestAnimationFrame(() => {
          if (selectionRef.current === record.id) readerHeading.current?.focus({ preventScroll: true });
        });
      }
      if (next && attempt.firstMessage && outgoing && (!choice || scope)) {
        try {
          scope ??= await api<ConversationScope>(threadRoute + "/scope");
          if (scope.projectId !== record.projectId ||
              attempt.body.workFolder && !scope.resources.some(resource => resource.kind === "code" && resource.permission === "read" && resource.write))
            throw Error("会话工作目录待核对，消息已保存在草稿。");
          const result = await sendComposerMessage(record.id, attempt.firstMessage, next, scope.revision, attempt.id + "-first-message");
          settleMessage(record.id,outgoing.id,result.runId);
          setDrafts(old => old[record.id] === attempt.firstMessage ? {...old, [record.id]: ""} : old);
          saveJSON("conversation-drafts", draftStore.current);
        } catch (failure) {
          settleMessage(record.id,outgoing.id);
          fail(failure, record.id);
        }
      } else if (outgoing) settleMessage(record.id,outgoing.id);
      void conversations.refetch();
    } catch (failure) {
      if (outgoing) settleMessage(pendingOwner,outgoing.id);
      const receiptStorageUnavailable = requested.attempt && failure instanceof APIError && failure.code.startsWith("receipt_");
      if (!creationMayHaveCompleted(failure) && !receiptStorageUnavailable)
        saveSetup(owner, { ...requested, attempt: undefined });
      fail(failure, owner);
    } finally {
      creatingChat.current = false;
      setBusy(false, owner);
      if (created) setBusy(false, created.id);
    }
  }
  async function retryNewWorktree() {
    const owner = selected;
    if (!chosenWorktree || busyKeys.current.has(owner)) return;
    setBusy(true, owner);
    try {
      const scope = await prepareConversationWorktree(owner, chosenWorktree);
      queryClient.setQueryData(["scope", owner], scope);
      rememberWorktree(owner);
    } catch (failure) {
      rememberWorktree(owner, { ...chosenWorktree, reason: failure instanceof Error ? failure.message : String(failure) });
      await currentScope.refetch();
    } finally {
      setBusy(false, owner);
    }
  }
  function openProjectCreation() {
    if (getNewChatBlockedReason()) return;
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setProjectCreation({ selection: selectionRef.current, route: setupRouteRef.current, trigger });
    closeSidebarDrawer();
  }

  function closeProjectCreation() {
    const trigger = projectCreation?.trigger;
    setProjectCreation(undefined);
    if (trigger) requestAnimationFrame(() => {
      if (!trigger.closest("[hidden],[inert]") && trigger.getClientRects().length &&
          !document.querySelector('[aria-modal="true"]')) trigger.focus({ preventScroll: true });
    });
  }

  async function updateProjectDirectory(project: Project) {
    await queryClient.cancelQueries({ queryKey: projectsKey });
    queryClient.setQueryData<ProjectDirectory>(projectsKey, current => {
      const projects = (current?.projects ?? []).filter(item => item.id !== project.id);
      const removedProjects = (current?.removedProjects ?? []).filter(item => item.id !== project.id);
      (project.removed ? removedProjects : projects).push(project);
      return { projects, removedProjects };
    });
    void queryClient.invalidateQueries({ queryKey: projectsKey });
  }

  async function adoptFolderProject(project: Project) {
    const origin = projectCreation;
    await updateProjectDirectory(project);
    if (!origin || selectionRef.current !== origin.selection || setupRouteRef.current !== origin.route) return;
    if (origin.selection) {
      showNewChatSetup(project.id);
      return;
    }
    const owner = setupKey(origin.route.entry);
    const current = setupStore.current[owner] ?? { projectId: origin.route.entry, title: "", workFolder: true };
    if (!current.attempt) saveSetup(owner, { ...current, projectId: project.id, worktreeResourceId: undefined });
  }

  async function newFolderProject(name?: string) {
    if (!projectCreation || getNewChatBlockedReason()) return false;
    const owner = projectCreation.selection;
    folderOpening.current = true;
    setProjectBusy(true, owner);
    try {
      const project = name === undefined ? await chooseFolderProject()
        : await api<Project>("/api/v2/projects", { name, createFolder: true });
      if (!project) return false;
      await adoptFolderProject(project);
      return true;
    } finally {
      folderOpening.current = false;
      setProjectBusy(false, owner);
    }
  }

  async function changeProjectVisibility(project: Project, removed: boolean) {
    try {
      const updated = await api<Project>("/api/v2/projects/" + encodeURIComponent(project.id) + "/visibility", {
        removed, expectedRevision: project.visibilityRevision ?? 1, confirm: true,
      });
      await updateProjectDirectory(updated);
    } catch (cause) {
      if (cause instanceof APIError && cause.code === "project_active")
        throw Error(t("请先结束项目中的活动会话，再移除项目。"));
      if (cause instanceof APIError && cause.code === "project_visibility_changed") {
        void projects.refetch();
        throw Error(t("项目列表已变化，请刷新后重试。"));
      }
      throw Error(t(removed ? "无法移除项目，请稍后重试。" : "无法恢复项目，请稍后重试。"));
    }
  }

  async function confirmProjectRemoval() {
    if (!projectRemoval || removingProject) return;
    setProjectRemovalError("");
    setRemovingProject(true);
    try {
      await changeProjectVisibility(projectRemoval, true);
      setProjectRemoval(undefined);
    } catch (cause) {
      setProjectRemovalError((cause as Error).message);
    } finally {
      setRemovingProject(false);
    }
  }
  const chromeHandled=useRef(0);
  useEffect(()=>{
    if(!chromeCommand||chromeCommand.serial===chromeHandled.current)return;
    chromeHandled.current=chromeCommand.serial;
    if(chromeCommand.action==="right-sidebar"&&selected&&!thread?.subtaskOf){toggleSide();}
    if(chromeCommand.action==="history"&&selected){setHistoryMenuOwner(selected);}
    if(chromeCommand.action==="new-chat")startNewChat();
    if(chromeCommand.action==="new-project")openProjectCreation();
  },[chromeCommand]);
  // 将原会话的选中片段与项目快照冻结为讨论草稿，正式创建由后端决定背景范围。
  function openDiscussion(text: string) {
    if (!selected) return;
    openConversationDetails();
    setDiscussionDrafts((old) => ({
      ...old,
      [selected]: {
        sourceId: selected,
        projectId: thread?.projectId ?? null,
        title: "讨论：" + (thread?.title || "当前事项").slice(0, 50),
        background: text.slice(0, 10000),
        excerpted: text.length > 10000,
        associateProject: false,
      },
    }));
  }
  // 讨论使用打开草稿时的项目快照；回包只删除同一份草稿，保留用户随后编辑的新版本。
  async function createDiscussion() {
    const value = discussionDraft;
    if (!value || discussionOwners[value.sourceId]?.busy) return;
    const owner = value.sourceId;
    let failure = "";
    setDiscussionOwners((old) => ({ ...old, [owner]: { draft: value, busy: true } }));
    try {
      if (value.associateProject && value.projectId === undefined)
        throw Error("旧草稿缺少项目关联快照，请重新打开背景后核对。");
      const child = await api<Conversation>(
        base + "/threads/" + owner + "/discussions",
        {
          title: value.title,
          background: value.background,
          associateProject: value.associateProject,
          expectedProjectId: value.projectId,
        },
      );
      setDiscussionDrafts((old) => {
        if (old[owner] !== value) return old;
        const next = { ...old };
        delete next[owner];
        return next;
      });
      await conversations.refetch();
      if (selectionRef.current === owner) select(child.id);
    } catch (e) {
      failure = e instanceof Error ? e.message : String(e);
    } finally {
      setDiscussionOwners((old) => {
        const next = { ...old };
        if (failure) next[owner] = { draft: value, busy: false, error: failure };
        else delete next[owner];
        return next;
      });
    }
  }
  // 用 l0 轮次请求委托草案；草案仍需用户确认，此处仅请求起草，不启动委托。
  async function requestDraft(job: Delegation) {
    setWorkflowPane("task");
    if(!settingsVisible)openSettingsAt("resources");
    const v = job.versions.find(
      (v) => v.id === (job.pendingVersion || job.activeVersion),
    )!;
    const scope = await api<ConversationScope>(
      base + "/threads/" + job.conversationId + "/scope",
    );
    await api(base + "/threads/" + job.conversationId + "/send", {
      text:
        "只起草持续委托约定，不执行代码修改。用propose_delegation提交待用户确认的新版本，不能自锁、启动或验收。job_id=" +
        job.id +
        "，expected_revision=" +
        job.revision +
        "；已有草案：" +
        JSON.stringify(v.spec),
      crew: "l0",
      purpose: "delegation_draft",
      expectedScopeRevision: scope.revision,
    });
    await conversations.refetch();
  }
  // 恢复绑定已预览的计划和原证据版本；先排队纠正，仅在确认原会话不活跃且非 unknown 时唤起。
  async function delegateRestoration(
    plan: RestorationPlan,
    provided?: EvidenceAction,
  ) {
    const owner = plan.conversationId;
    const source =
      provided ||
      (await api<EvidenceAction>(
        base +
          "/threads/" +
          owner +
          "/evidence/" +
          encodeURIComponent(plan.sourceActionId || ""),
      ));
    const text = `请执行我在恢复影响预览中明确授权的单文件恢复：resource_id=${plan.resourceId}，plan_id=${plan.id}。使用 restore_project_file，核对结果；版本冲突或未知时停止，不改成其他恢复命令。`;
    await api(base + "/threads/" + owner + "/corrections", {
      id: plan.id,
      text,
      target: {
        kind: "action",
        id: source.id,
        revision: source.evidenceRevision,
      },
    });
    const listing = await api<{ threads: Conversation[] }>(base + "/threads");
    const actual = listing.threads.find((t) => t.id === owner);
    if (
      actual &&
      !actual.runtime?.active &&
      actual.runtime?.state !== "unknown"
    ) {
      const scope = await api<ConversationScope>(
        base + "/threads/" + owner + "/scope",
      );
      await api(base + "/threads/" + owner + "/send", {
        text:
          "请处理已明确授权的恢复计划 " +
          plan.id +
          "；以排队纠正的具体版本与范围为准。",
        crew: "l0",
        expectedScopeRevision: scope.revision,
      });
    }
    await conversations.refetch();
  }

  const [contextOpen,setContextOpen]=useState(false);
  const [incomingFiles,setIncomingFiles]=useState<{owner:string;files:File[];id:string}>();
  const consumeIncomingFiles=useCallback((id:string)=>{
    setIncomingFiles(batch=>batch?.id===id ? undefined : batch);
  },[]);
  const filePick=useRef<HTMLInputElement>(null);
  const composerInput=useRef<HTMLTextAreaElement>(null);
  function pickComposerFiles(accept: string) {
    const picker=filePick.current;
    if (!picker) return;
    picker.accept=accept;
    picker.click();
  }
  function appendComposerDraft(owner: string, text: string) {
    appendActiveDraft(owner,text);
    requestAnimationFrame(()=>{
      const field=composerInput.current;
      if (selectionRef.current!==owner || !field?.getClientRects().length || field.closest("[hidden],[inert]")) return;
      field.focus({preventScroll:true});
      field.setSelectionRange(field.value.length,field.value.length);
    });
  }
  const projects=useQuery<ProjectDirectory>({queryKey:projectsKey,queryFn:()=>api("/api/v2/projects"),refetchInterval:4000});
  const setupProject = projects.data?.projects.find(project => project.id === setup.projectId);
  const setupDestinationError = setup.attempt || !setup.projectId ? ""
    : projects.error ? t("暂时无法核对项目，请稍后重试或选择普通会话。")
    : projects.isPending ? t("正在读取项目…")
    : !setupProject ? t("该项目已不可用，请重新选择归属。")
    : setup.worktreeResourceId && !newChatWorktreeRoots(setupProject).some(root => root.id === setup.worktreeResourceId)
      ? t("所选工作树来源已不可用，请重新选择工作位置。") : "";
  const attachments=useQuery<{attachments:Attachment[]}>({queryKey:["attachments",selected],queryFn:()=>api(`${base}/threads/${selected}/attachments`),enabled:!!selected,refetchInterval:3000});
  const approvals = usePermissionRequests(true, selected);
  const savedPermission = approvals.isError ? undefined : approvals.data;
  const permissionMode = (!selected && setup.permissionMode) || savedPermission?.mode;
  const readOnlyPermission = !!savedPermission && (selected || !setup.permissionMode) && savedPermission.sandboxMode === "read-only";
  const chosen=execution.data?.next||newExecution;
  useEffect(()=>{setContextOpen(false);setWorkflowPane("materials");},[selected]);
  // 新对话只更新默认偏好；已有会话携带配置 revision 保存下一轮偏好，以识别并发修改。
  async function saveContextLimit(contextLimit:number){
    const owner=selected;setExecutionBusy(true,owner);
    const config={...chosen,contextLimit};
    try{
      if(owner){
        const url=`${base}/threads/${owner}/execution`;
        const persist=(next:ExecutionConfig)=>api<ExecutionView>(url,executorBody(next));
        try { return (await persist(config)).next; }
        catch(error){
          if(!(error instanceof APIError)||error.code!=="execution_changed")throw error;
          const {next}=await api<ExecutionView>(url);
          if(next.contextLimit===contextLimit)return next;
          if(next.contextLimit!==chosen.contextLimit)throw error;
          return (await persist({...next,contextLimit})).next;
        }finally{await execution.refetch();}
      }
      const value=executorConfig(config);setNewExecution(value);return value;
    }finally{setExecutionBusy(false,owner);}
  }
  async function changePolicy(value: PermissionMode) {
    const owner = selected;
    const draftOwner = inputOwner;
    if (!savedPermission || (value === "full" && !window.confirm(t("为这条会话开启完全访问权限？终端将以你的 Windows 身份访问本机文件和互联网。")))) return;
    if (!owner) {
      saveSetup(draftOwner, {...setup, permissionMode: value});
      return;
    }
    try {
      await api(permissionPath(owner), {mode: value, expectedRevision: savedPermission.revision, confirm: value === "full"});
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error), owner);
    } finally {
      await Promise.all([
        queryClient.invalidateQueries({queryKey: ["permission-requests", owner], exact:true}),
        queryClient.invalidateQueries({queryKey: ["scope", owner], exact:true}),
      ]);
    }
  }
  // 先确定会话资料所有者，再由 AttachmentPane 导入。
  function importFiles(files:File[]){
    if(!files.length)return;
    if(busy||scopeBusy){fail(Error("当前仍在导入或核对资料，本次文件未导入；请等待输入区加号可用后再添加。"));return;}
    const id=openScope(true,false);
    if(id)setIncomingFiles({owner:id,files,id:crypto.randomUUID()});
  }
  function addAttachmentSelection(owner:string,id:string,imported:boolean) {
    setAttachmentDrafts(old=>old[owner]?.some(item=>item.id===id)?old:{...old,[owner]:[...old[owner]??[],{id,imported}]});
  }
  function discardAttachmentSelection(owner:string,ids:readonly string[]) {
    setAttachmentDrafts(old=>{
      const remaining=old[owner]?.filter(item=>!ids.includes(item.id));
      if(!remaining||remaining.length===old[owner].length)return old;
      const next={...old};
      if(remaining.length)next[owner]=remaining;else delete next[owner];
      return next;
    });
  }
  function attachmentRemoved(owner:string,id:string) {
    discardAttachmentSelection(owner,[id]);
    void queryClient.invalidateQueries({queryKey:attachmentImageKey(owner)});
  }
  async function removeComposerAttachment(id:string) {
    const owner=selected;
    if(!owner||busy||scopeBusy)return;
    // Removing a reused image from the draft leaves its older message available.
    if(!attachmentDrafts[owner]?.find(item=>item.id===id)?.imported){
      discardAttachmentSelection(owner,[id]);
      return;
    }
    setRemovingAttachments(old=>({...old,[owner]:true}));
    try{
      await api(base+"/threads/"+encodeURIComponent(owner)+"/attachments",{remove:id});
      attachmentRemoved(owner,id);
      await Promise.all([attachments.refetch(),currentScope.refetch()]);
    }catch(error){fail(error,owner);}
    finally{setRemovingAttachments(old=>({...old,[owner]:false}));}
  }
  const sidebar=<ConversationSidebar projects={projects.data?.projects} removedProjects={projects.data?.removedProjects}
    threads={sidebarThreads} selected={selected}
    onRemoveProject={project=>{closeSidebarDrawer();setProjectRemovalError("");setProjectRemoval(project);}}
    onRestoreProject={project=>changeProjectVisibility(project, false)}
    onStartNewChat={startNewChat} revealedChat={revealedChat} onSelect={selectFromSidebar}
    conversationActive={chatActive}
    onCloseDrawer={sidebarDrawerOpen?onHideSidebar:undefined} onManageProjects={openProjectCreation}
 scopeBusy={scopeBusy} newChatBlockedReason={newChatBlockedReason} seen={seen} onChanged={()=>conversations.refetch()} error={conversations.error?.message}/>;
  const captionProjectId = thread?.projectId;
  const captionProjectName = projects.data?.projects.find(project=>project.id===captionProjectId)?.name || thread?.projectName || "项目名称待核对";
  const caption=selected ? <>
    <h1 ref={readerHeading} tabIndex={-1}>{thread?.title || t("会话")}</h1>
    {captionProjectId&&<small className="conversation-project-label" title={t("项目会话")}>{captionProjectName}</small>}
    {thread?.scheduledPlanId && <div className="conversation-schedule">
      <span className="conversation-schedule-label" title={t("定时任务")}><UiIcon name="calendar"/><span>{t("定时任务")}</span></span>
      {scheduledPlan && <small className="conversation-schedule-next">
        {scheduledPlan.nextRunAt !== null ? t("下次运行 {time}",{time:nextRunLabel(scheduledPlan,locale)}) : nextRunLabel(scheduledPlan,locale)}
      </small>}
      <button type="button" onClick={()=>openScheduledPage(thread.scheduledPlanId)}>{t("计划设置")}</button>
    </div>}
    {thread?.projectId&&!thread.readonlyEvidence&&<ProjectActivity key={selected + thread.projectId} conversationId={selected}
      onSelect={(owner,id)=>{showConversation(owner);setRoundJump({owner,id});}}/>}
  </> : null;
  // 进度入口可定位时间线和工具原动作。
  const progress=(close:()=>void)=><><StageProgress conversationId={selected} onInspect={(run,gap)=>{
    close();
    inspectEvidence(null,run,"",gap);
  }}/>
    <nav aria-label="对话进度定位">{groupToolActivity(projectConversation(visibleHistory?.events||[]),timelineToolCall).map(group=>{
      const first = group.items[0];
      if(first.kind!=="activity") return null;
      // 展开目标动作所在分组，再滚动到原记录对应的行。
      const jump = (id:string) => {
        const owner=selected;
        const reveal = () => {
          if(selectionRef.current!==owner)return;
          const target=messageElement.current?.querySelector<HTMLElement>('[data-conversation-item-id="'+CSS.escape(id)+'"]');
          if(!target)return;
          const group=target.closest<HTMLDetailsElement>("details.tool-activity-group:not([open]),details.conversation-process:not([open])");
          if(!group){target.scrollIntoView({block:"center"});return;}
          group.querySelector<HTMLElement>(":scope > summary")?.click();
          requestAnimationFrame(()=>{
            if(group.open)reveal();
          });
        };
        reveal();
      };
      if(group.items.length===1) return <button className="node" key={first.id} onClick={()=>jump(first.id)}>{first.text}</button>;
      return <details className="progress-tool-group" data-tool-group={first.event.tool} key={first.id}>
        <summary>{roleLabel(first.role)} · {group.label} ×{group.items.length} · {callResult(first.event.state)}</summary>
        {group.items.map((item,index)=><button className="node" key={item.id} onClick={()=>{
          jump(item.id);
          inspectEvidence(item.event.action_id!, item.event.run_id ?? "");
        }}>
          第 {index+1} 次{item.event.t ? " · "+item.event.t : ""}
        </button>)}
      </details>;
    })}</nav></>;
  const availableScope = currentScope.error ? undefined : currentScope.data;
  const resources = availableScope?.resources.filter(resource => resource.permission === "read") ?? [];
  const searchSources = useQuery({queryKey: ["search-sources", selected],
    queryFn: () => api<{items: SearchSource[]}>(`/api/v2/conversations/threads/${encodeURIComponent(selected)}/search-sources`),
    enabled: !!selected && !thread?.subtaskOf, refetchInterval: 5000, refetchIntervalInBackground: true});
  const search = searchSources.error ? [] : searchSources.data?.items ?? [];
  const codeResources = resources.filter(resource => resource.kind === "code");
  const changeSummary = useRunChanges(selected, runIdForOverview, availableScope?.revision, !!thread?.runtime?.active);
  const web = (webSources[selected] ?? []).filter(item => item.sent || draft.includes("地址：" + item.url + "\n"));
  // 各来源成功读取后才生成对应已读版本，读取失败不等于内容为空。
  // 变更帧和侧边聊天还需各自展示回调确认，避免仅打开面板就把未展示内容标成已读。
  const versions:Record<string,string[]> = {};
  if (availableScope && (!runIdForOverview || !codeResources.length || changeSummary.isSuccess && !changeSummary.error)) {
    const summary = changeSummary.error ? undefined : changeSummary.data;
    versions.changes = [...(summary?.changedFileFrames ?? (summary?.fileCount ? summary.frames : []))].sort();
  }
  if(agents.isSuccess&&!agents.error) versions.agents=agents.data.tasks.map(agentReadKey).sort();
  if (availableScope && searchSources.isSuccess && !searchSources.error) {
    // Keep the standalone conversation's work folder as context in the Sources panel.
    const supplied = availableScope.projectId ? resources : resources.filter(resource => resource.kind !== "code");
    versions.sources = [
      ...supplied.map(resource => `resource:${resource.id}`),
      ...web.filter(item => item.sent).map(item => `url:${item.url}`),
      ...search.map(item => JSON.stringify([item.runId, item.url])),
    ].sort();
  }
  if(conversations.isSuccess&&!conversations.error) {
    versions.related=[];
    for(const chat of related) if(typeof chat.eventCursor==="number")versions[`chat:${chat.id}`]=chat.eventCursor>0?[String(chat.eventCursor)]:[];
  }
  const relatedSeen = Object.fromEntries(related.map(chat => [chat.id,
    Math.max(seen[chat.id] ?? 0, Number(seenSide[selected]?.[`chat:${chat.id}`]?.[0] ?? 0)),
  ]));
  const versionKey = JSON.stringify(versions);
  useEffect(() => {
    if (!selected) return;
    const values: Record<string, string[]> = JSON.parse(versionKey);
    setSeenSide(old => {
      const prior = old[selected]??{};
      const updates:Record<string,string[]>={};
      for(const [tab,value] of Object.entries(values)){
        const relatedTab=tab.startsWith("chat:");
        const showing=visible&&tab===visibleTab;
        const ready=showing&&tab!=="changes"&&tab!=="agents"&&(!relatedTab||sideChatRead?.owner===selected&&sideChatRead.tab===tab&&String(sideChatRead.cursor)===value[0]);
        if(ready||prior[tab]===undefined&&(!relatedTab||prior.related===undefined)){
          if(JSON.stringify(prior[tab])!==JSON.stringify(value))updates[tab]=value;
        }else if(tab==="agents"&&showing&&displayedAgents?.owner===selected){
          const acknowledged=value.filter(item=>prior.agents?.includes(item)||displayedAgents.keys.includes(item));
          if(JSON.stringify(prior.agents)!==JSON.stringify(acknowledged))updates.agents=acknowledged;
        }else if(tab==="changes"&&showing&&displayedChanges?.owner===selected&&displayedChanges.run===runIdForOverview){
          const seen=new Set([...prior[tab]??[],...displayedChanges.frames]);
          const acknowledged=value.filter(frame=>seen.has(frame));
          if(JSON.stringify(prior[tab])!==JSON.stringify(acknowledged))updates[tab]=acknowledged;
        }
      }
      if(!Object.keys(updates).length)return old;
      const next={...old};delete next[selected];
      return Object.fromEntries([...Object.entries(next),[selected,{...prior,...updates}]].slice(-64));
    });
  }, [selected, visible, visibleTab, versionKey, sideChatRead, displayedChanges, displayedAgents, runIdForOverview]);
  const unreadTabs=new Set(Object.entries(versions).filter(([tab,value])=>{
        const prior=seenSide[selected]?.[tab];
        if (tab.startsWith("chat:")) return !!seenSide[selected]?.related && Number(value[0] ?? 0) > (relatedSeen[tab.slice("chat:".length)] ?? 0);
        return !!prior&&value.some(item=>!prior.includes(item));
  }).map(([tab])=>tab));
  const unreadLabels=[...unreadTabs].map(tab=>tab.startsWith("chat:")?related.find(chat=>chat.id===tab.slice(5))?.title:t(sideLabels[tab as keyof typeof sideLabels])).filter(Boolean);
  const bar=<>
    {models.data?.error&&<button className="pill warn" onClick={openSettings}>{t("模型连接异常")}</button>}<span className="spacer"/>
    <SideToolbar key={selected} state={side} disabled={!selected || !!thread?.subtaskOf} trigger={newTabButton}
      unread={unreadLabels.length?t("未读更新 · {items}",{items:unreadLabels.join("、")}):""}
      projectName={codeResources.length === 1 ? codeResources[0].name : codeResources.length ? thread?.projectName || "多个工作区" : "未关联工作区"}
      summary={changeSummary.data} summaryUnavailable={!!changeSummary.error || !availableScope || !codeResources.length}
      rangeLabel={runIdForOverview ? (thread?.runtime?.active ? "本轮" : "上一轮") : ""}
      agents={agents.error ? undefined : agents.data} currentRun={runIdForOverview} related={related}
      seen={relatedSeen} sources={resources.length + web.length + search.length} searchCount={search.length}
      hasWorkspace={!!codeResources.length} hasWorktree={codeResources.some(resource => !!resource.worktreeOf)}
      canSwitchWorkspace={!!codeResources.length && !thread?.subtaskOf && !thread?.readonlyEvidence && !thread?.scheduledPlanId}
      onOpen={openSideTab} onToggle={toggleSide}
      onAgent={id=>{setAgentFocus(old=>({owner:selected,id,serial:(old?.serial??0)+1}));openSideTab("agents");}}
      onAddSource={() => void openScope(true)} onReadSources={() => openSideTab("sources")}
      onChanges={snapshot => {
        if (snapshot && !changeSummary.error && changeSummary.data?.frames.length)
          openCodeReview({conversationId:selected, changeId:"", kind:"range", runId:runIdForOverview, frames:changeSummary.data.frames});
        else openCodeReview();
      }}/>
  </>;
  const delegationReply = currentConfig?.crew === 'l4' && !!currentConfig.delegationId;
  const question = delegationReply || currentConfig?.crew === 'l0' && currentConfig.purpose !== 'delegation_draft'
    ? awaitingQuestion : undefined;
  const [outputHandoff,setOutputHandoff]=useState<{role:string;runId?:string}>();
  const onHandoffPresentation=useCallback((role:string|undefined,runId?:string)=>{
    setOutputHandoff(old=>old?.role===role&&old?.runId===runId?old:role?{role,runId}:undefined);
  },[]);
  const summaryRole = !nativeRun && !historyBefore && outputHandoff?.runId === runIdForOverview ? outputHandoff?.role : undefined;
  const runtime = thread?.runtime;
  // Only requests owned by the current run can appear in this composer.
  const pendingApproval=summaryRole!=="planner"&&!nativeEnd&&runtime?.active&&!approvals.error ? approvals.data?.requests.find(row=>
    row.state==="pending"&&row.sessionId===selected&&row.runId===runtime.run_id) : undefined;
  const visibleImport = useMemo(() => repositoryImportIssue(visibleHistory?.events ?? []), [visibleHistory?.events]);
  const importIssue = thread && !pendingApproval && !history.error && !thread.readonlyEvidence && !thread.subtaskOf && !thread.archived &&
    (!historyBefore || visibleImport?.ssh) ? visibleImport : undefined;
  const importRecovery = importIssue && {runId: importIssue.runId, actionId: importIssue.actionId,
    panel: <RepositoryImportHelp key={selected + ":" + importIssue.runId + ":" + importIssue.actionId}
      issue={importIssue} active={!!runtime?.active || busy} hasDraft={!!draft.trim()}
      sessionId={selected} projectId={availableScope ? availableScope.projectId : thread?.projectId}
      scope={availableScope} onScope={() => openScope()}
      onSend={text => {
        if (selectionRef.current !== selected) return;
        if (historyBefore) historyState.setHistoryBefore(0);
        void send(text);
      }}
      onRetry={text => {
        if (selectionRef.current !== selected) return;
        setDrafts(old => old[selected]?.trim() ? old : {...old, [selected]: text});
        document.getElementById("agent-composer")?.focus();
      }}/>};
  const sendFeedback = sendReason(draft);
  const permissionControl = !thread?.scheduledPlanId && !thread?.subtaskOf && <ComposerPermissions key={"permissions:" + inputOwner} policy={permissionMode} readOnly={!!readOnlyPermission}
    count={selected ? approvals.data?.requests.filter(row=>row.state==="pending").length ?? 0 : 0}
    pending={approvals.isFetching} disabled={!!(thread?.archived || thread?.readonlyEvidence || (!selected && (busy || setup.attempt)))}
    onChange={changePolicy} onRetry={()=>void approvals.refetch()} onSettings={()=>openSettingsAt('permissions')}/>;
  const recoveredDrafts = Object.entries(drafts).filter(([key])=>key.startsWith("local-draft:"));
  const recoverDrafts = recoveredDrafts.length > 0 && <details className="composer-recovered">
    <summary>未建档的本机草稿 · {recoveredDrafts.length}</summary>
    {recoveredDrafts.map(([key,text])=><div key={key}><pre>{text}</pre><button type="button" disabled={!!thread?.archived||!!thread?.subtaskOf}
      onClick={()=>setDrafts(old=>{const next={...old,[inputOwner]:[old[inputOwner],old[key]].filter(Boolean).join("\n\n")};delete next[key];return next;})}>加入当前草稿</button></div>)}
  </details>;
  const composer=<>
    {thread?.runtime?.active&&thread.runtime.run_id&&<RunningChanges key={selected+":"+thread.runtime.run_id}
      conversationId={selected} runId={thread.runtime.run_id} accessVersion={currentScope.error?undefined:currentScope.data?.revision} onReview={openCodeReview}/>}
          {thread?.readonlyEvidence && (
            <section aria-label="只读证据追问">
              <strong>正在讨论这一步</strong>
              <span>依据已保存的动作记录回答</span>
              <button
                onClick={() => {
                  const source = thread.questionOf!;
                  inspectEvidence(source.actionId, source.runId || "", source.conversationId);
                }}
              >
                返回原始证据
              </button>
            </section>
          )}
    {pendingApproval&&<ComposerApproval key={pendingApproval.id} row={pendingApproval} refetch={approvals.refetch}/>}
    {!pendingApproval&&question&&!busy&&<section className="composer-question" role="group" aria-label={t("需要你的选择")} aria-live="polite">
      <strong>{delegationReply ? t("委托需要你答复") : t("Agent 需要你选择")}</strong><p>{question.question}</p>
      {(question.options.length>0||delegationReply)&&<div className="composer-question-options">
        {question.options.map((option,index)=><button key={index} type="button" disabled={scopeBusy||!!thread?.runtime?.active} onClick={()=>void send(option)}>{option}</button>)}
        {delegationReply&&<button type="button" disabled={!draft.trim()||scopeBusy||!!thread?.runtime?.active} onClick={()=>void send(draft)}>回复委托</button>}
      </div>}
      <small>{t("也可以在下方输入自己的回复。")}</small>
    </section>}
    {recoverDrafts}
    {selected&&<MessageAttachments key={selected} conversationId={selected}
      items={selectedAttachmentIds.flatMap(id=>attachments.data?.attachments.find(a=>a.id===id&&a.permission==="read")??[])}
      busy={busy||scopeBusy} onRemove={id=>void removeComposerAttachment(id)}/>}
    {selected&&<div id="composer-attachment-manager" className="composer-attachment-manager" hidden={!attachmentsOpen}>
      <header><strong>{t("管理附件")}</strong><button type="button" onClick={()=>setAttachmentsOpen(false)}>{t("关闭")}</button></header>
      <AttachmentPane key={"attachments:"+selected} conversationId={selected} onBusyChange={setAttachmentBusy} onError={fail}
        onChange={()=>currentScope.refetch()} incoming={incomingFiles?.owner===selected?incomingFiles:undefined}
        onIncomingAccepted={consumeIncomingFiles}
        draftIds={selectedAttachmentIds} onAttach={(id,imported)=>addAttachmentSelection(selected,id,imported)}
        onRemoved={id=>attachmentRemoved(selected,id)}/>
    </div>}
    <form className="cmain" onSubmit={e=>{e.preventDefault();if(!composing.current)send();}} onDragOver={e=>{if(e.dataTransfer.types.includes('Files'))e.preventDefault();}} onDrop={e=>{if(e.dataTransfer.files.length){e.preventDefault();void importFiles(Array.from(e.dataTransfer.files));}}}>

      <input ref={filePick} type="file" multiple hidden onChange={e=>{const files=Array.from(e.target.files||[]);e.target.value='';void importFiles(files);}}/>
      <label htmlFor="agent-composer" className="sr-only">{t("告诉 Agent 你想做什么")}</label>
      <textarea ref={composerInput} id="agent-composer" rows={1} aria-describedby="composer-feedback" disabled={!!thread?.subtaskOf||!!thread?.archived} value={draft}
        placeholder={t("告诉 Agent 你想做什么…")}
        onCompositionStart={()=>{composing.current=true;}} onCompositionEnd={()=>{composing.current=false;}}
        onPaste={e=>{const files=Array.from(e.clipboardData.files);if(files.length){e.preventDefault();void importFiles(files);}}}
        onKeyDown={e=>{if(e.nativeEvent.isComposing||composing.current||e.keyCode===229)return;if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}}}
        onChange={e=>{const value=e.target.value;setDrafts(old=>({...old,[selected]:value}));}}/>
      <div className="composer-toolbar">
        <ComposerAddMenu owner={selected} active={!settingsVisible && (chatActive || !!conversationDock)}
          disabled={!draftOwner || busy || currentScope.isPending} attachmentsBusy={scopeBusy} scopeRevision={availableScope?.revision}
          existingAttachments={attachments.error ? [] : (attachments.data?.attachments ?? []).filter(item=>
            item.permission==="read" && item.status==="ready" && !selectedAttachmentIds.includes(item.id))}
          onPickFiles={pickComposerFiles} onAttach={id=>addAttachmentSelection(selected,id,false)} onAppend={appendComposerDraft}/>
        {draftOwner && (attachmentsOpen || !!attachments.data?.attachments.length) &&
          <button type="button" aria-label={t("管理附件")} title={t("管理附件")} aria-expanded={attachmentsOpen}
            aria-controls="composer-attachment-manager" onClick={()=>setAttachmentsOpen(value=>!value)}><UiIcon name="document"/></button>}
        {permissionControl}
        <span className="cspacer"/>
        <ComposerModel key={"composer-model:" + selected} control={modelControl} onSettings={openSettings}/>
        <LegacyContextButton key={selected} id={selected} runId={thread?.runtime?.run_id} limit={chosen.contextLimit||0}
          disabled={scopeBusy||!!thread?.subtaskOf||!!thread?.readonlyEvidence||!!thread?.archived||!!selected&&!execution.data}
          onLimitChange={saveContextLimit} onDetails={()=>{
            openConversationDetails();
            setContextOpen(true);
          }}/>
        {runtime?.active&&!nativeEnd&&<button type="button" id="stop" title={t("停止")} aria-label={t("请求停止")} disabled={!!thread?.subtaskOf}
          onClick={()=>api(`${base}/threads/${selected}/stop`,{}).then(()=>conversations.refetch()).catch(fail)}><UiIcon name="stop"/></button>}
        <button id="go" className="primary" type="submit" disabled={!!sendFeedback} aria-label={t("发送")} title={sendFeedback||t("发送")}><UiIcon name="send"/></button>
      </div>
      <ComposerFeedback runtime={runtime} end={nativeEnd} events={history.error && !nativeEnd && runtime?.active ? undefined : history.data?.events}
        runtimeReadFailed={!!conversations.error} awaitingApproval={!!pendingApproval}
        sendFeedback={sendFeedback} idleHint={sendFeedback === emptyDraftFeedback}
        error={error || history.error?.message}
        notice={history.data?.historyNotice || (thread?.archived ? t("此对话已归档，历史与草稿保留。") : undefined)}
        onLatest={historyBefore ? ()=>{latestRequested.current=true;historyState.setHistoryBefore(0);} : scroll.hasNew ? scroll.toLatest : undefined}>
        {selected && thread && !thread.projectId && !thread.subtaskOf && !thread.readonlyEvidence && !thread.archived &&
          <ConversationRecordStatus conversationId={selected} visible={conversationVisible}
            onOpen={file => { if (file.conversationId === selectionRef.current) onOpenWorkroomFile(file); }}/>}
      </ComposerFeedback>
      {chosenWorktree && worktreePending && <div className="conversation-worktree-choice">
        {chosenWorktree.reason && <span role="status">{chosenWorktree.reason}</span>}
        {codeResources.some(resource => (resource.worktreeOf || resource.id) === chosenWorktree.resourceId)
          ? <button type="button" disabled={busy || scopeBusy} onClick={()=>openSideTab("handoff")}>核对工作树</button>
          : <button type="button" disabled={busy || scopeBusy} onClick={()=>void retryNewWorktree()}>重试准备工作树</button>}
        <button type="button" disabled={busy || scopeBusy || !currentScope.data || !!currentScope.error}
          onClick={()=>rememberWorktree(selected)}>使用当前工作范围</button>
      </div>}
    </form></>;
  const review = reviews.find(item => item.owner === selected);
  const reviewNavigation = review?.key ?? 0;
  const savedReview = reviewBookmarks.current.get(selected);
  // 关联会话按当前列表解析标题，其他页签用 `ConversationSideTabs.tsx` 的固定名称。
  const sideLabel = (tab: SideTab) => tab.startsWith("link:")
    ? linkPages.find(page=>page.id===tab.slice(5))?.label || "资源"
    : tab.startsWith("chat:")
    ? related.find(chat => chat.id === tab.slice(5))?.title || "侧边聊天"
    : t(sideLabels[tab as keyof typeof sideLabels]);
  return <WorkroomChangeLinks onOpen={onOpenWorkroomFile} active={conversationVisible?selected:""}><div className="application-surface" hidden={!chatActive} inert={settingsVisible} data-surface="agent"><LegacyPage sidebarOpen={sidebarOpen} sidebarDrawerOpen={sidebarDrawerOpen} onHideSidebar={onHideSidebar} theme={theme} compact={preferences.compact} fontSize={preferences.fontSize} feedRef={messageElement} reviewOpen={!!selected && !!visibleTab} chatSetup={!selected}
    conversationDock={conversationDock} onFeedMoved={scroll.viewportMoved}
    history={selected&&<ConversationHistoryRail key={selected} conversation={selected} feed={messageElement} revision={`${historyBefore}:${lastVisible?.seq}:${visibleHistory?.generation}`} running={!!thread?.runtime?.active}
      menuOpen={historyMenuOwner===selected} onCloseMenu={()=>setHistoryMenuOwner("")}
      progress={close=><>
        <RoleHeartbeat conversationId={selected} runtime={runtime} summaryRole={summaryRole}
          summaryRunId={summaryRole ? outputHandoff?.runId : undefined} awaitingApproval={!!pendingApproval} nativeEnd={nativeEnd}/>
        {progress(close)}
      </>} onJump={round=>setRoundJump({owner:selected,id:round.id,round})} onLatest={()=>{setRoundJump(undefined);if(historyBefore){latestRequested.current=true;historyState.setHistoryBefore(0);}else scroll.toLatest();}}/>}
    slots={{bar:selected ? bar : null,sidebar,caption,composer:selected ? composer : null,
    feed:!selected ? <NewConversationHome key={inputOwner} draft={setup} text={draft} projects={projects.data?.projects ?? []} heading={readerHeading}
            blockedReason={setupDestinationError || newChatBlockedReason} busy={busy && creatingChat.current} error={error} pendingMessage={pendingMessage}
            onNewProject={openProjectCreation} sendBlocked={firstSendBlocked} permissions={permissionControl} model={<ComposerModel control={modelControl} onSettings={openSettings}/>}
            onChange={value=>{if (!setupStore.current[inputOwner]?.attempt) saveSetup(inputOwner, value);}}
            onText={text=>setDrafts(old=>({...old,[inputOwner]:text}))} onConfirm={sendFirst=>void confirmNewChat(sendFirst)}
            onReturn={setupRoute.returnTo && sidebarThreads.some(row=>row.id===setupRoute.returnTo) ? ()=>{
              const previous = setupRoute.returnTo;
              showConversation(previous);
              requestAnimationFrame(()=>{if(selectionRef.current===previous)readerHeading.current?.focus({preventScroll:true});});
            } : undefined}>{recoverDrafts}</NewConversationHome> : <div className={`conversation-reading think-${look.think} output-${look.output}`}>
            {thread?.forkOf && <ForkOrigin key={"origin:"+selected} chat={thread} onOpen={showConversation}/>}
            <HistoryControls state={historyState}/>
            <ConversationTimeline key={selected} events={visibleHistory?.events || []} nativeRuns={historyBefore ? pastNativeRuns : currentNativeRuns}
              importRecovery={importRecovery}
              active={!historyBefore&&!!thread?.runtime?.active&&!nativeEnd} runId={historyBefore?undefined:thread?.runtime?.run_id}
              runStatus={historyBefore?undefined:thread?.runtime?.state}
              pendingMessage={conversationVisible && !historyBefore ? pendingMessage : undefined}
              showFollowups={conversationVisible && !historyBefore}
              onMessageSettled={settleMessage}
              onSummary={historyBefore?undefined:onHandoffPresentation}
              conversationId={selected} onReview={openCodeReview}
              onSources={run=>{setSourceFocus({owner:selected,run});openSideTab("sources");}}
              onArtifact={(id,version,imageLabel)=>{
                if (imageLabel) {
                  const pageId = onOpenImageArtifact(selected,id,version,imageLabel);
                  if (pageId) openSideTab(`link:${pageId}`);
                  return;
                }
                setArtifactFocus({owner:selected,id,version});
                setWorkflowPane("materials");
                openSettingsAt("resources");
              }}
              onDiscuss={openDiscussion}
              onEvidence={(id, run) => {
                inspectEvidence(id, run, id && thread?.questionOf?.actionId === id
                  ? thread.questionOf.conversationId : "");
              }} />
    </div>}}
    review={<SideTabs state={side} labels={sideLabel} onOpen={openSideTab} onClose={closeSideTab}>
      {visibleTab==="start"&&<div className="side-tab-body"><SideStartPage key={selected} related={related}
        hasWorkspace={!!codeResources.length} hasAgents={!agents.error&&!!agents.data?.tasks.length} unread={unreadTabs}
        onTab={tab=>tab==="changes"?openCodeReview():openSideTab(tab)} onWorkbench={onWorkbench} onWeb={url=>onOpenWeb(selected,url)}/></div>}
      {linkPages.map(page=>{
        const tab:SideTab=`link:${page.id}`;
        const shown=page.owner===selected&&visibleTab===tab;
        return <div key={page.id} className="side-tab-body" hidden={!shown}>
          {page.kind==="artifact-image" ? visible && shown && <ImageArtifactTab page={page}/> :
            page.kind==="web"?<BrowserPane conversationId={page.owner} initialUrl={page.url} visible={visible&&shown}
            onClose={()=>closeSideTab(tab)} onQuote={(text, source)=>quoteWeb(page.owner,text,source)}/>:
            <Suspense fallback={<p role="status">正在打开文件审查…</p>}><LinkedFileReview target={page.target}
              visible={visible&&shown}/></Suspense>}
        </div>;
      })}
      {Object.entries(sideStates).filter(([, state]) => state.tabs.includes("browser")).map(([owner]) =>
        <div key={owner} className="side-tab-body" hidden={owner !== selected || visibleTab !== "browser"}>
          <BrowserPane conversationId={owner} visible={visible&&owner===selected&&visibleTab==="browser"}
            onClose={() => {closeSideTab("browser");newTabButton.current?.focus();}} onQuote={(text, source) => quoteWeb(owner,text,source)}/>
        </div>)}
      {side.tabs.includes("changes") && <div className="side-tab-body" hidden={!reviewOpen}>
        <CodeReviewDrawer key={`${selected}:${review?.key ?? 0}`} activation={reviewOpen ? review?.key ?? 0 : undefined}
          conversationId={review?.initial?.conversationId || selected} runId={runIdForOverview}
          bookmark={savedReview?.navigation===reviewNavigation ? savedReview.position : undefined}
          onRemember={position=>reviewBookmarks.current.set(selected,{navigation:reviewNavigation,position})}
          running={!!thread?.runtime?.active} initial={review?.initial} onClose={closeCodeReview}
          onDisplayedFrames={rememberDisplayedChanges}
          allowCorrections={!thread?.readonlyEvidence&&!thread?.subtaskOf&&(!review?.initial||review.initial.conversationId===selected)}
          onEvidence={(owner,action,run)=>inspectEvidence(action,run,owner===selected?"":owner)}
          onFeedback={text=>setDrafts(old=>({...old,[selected]:(old[selected]||"")+"\n"+text}))}/>
      </div>}
      {side.tabs.includes("agents") && <div className="side-tab-body" hidden={visibleTab !== "agents"}>
        <AgentOverviewPane key={selected} conversationId={selected} query={agents} currentRun={runIdForOverview}
          visible={visible&&visibleTab==="agents"} onRead={rememberAgents} focus={agentFocus?.owner===selected?agentFocus:undefined}/>
      </div>}
      {side.tabs.includes("sources") && <div className="side-tab-body" hidden={visibleTab !== "sources"}>
        <SourcesPane key={selected} resources={resources} web={web} search={search} currentRun={runIdForOverview}
          focusedRun={sourceFocus?.owner === selected ? sourceFocus.run : undefined} onAdd={() => void openScope(true)}/>
      </div>}
      {(["git","terminal"] as const).filter(kind => side.tabs.includes(kind)).map(kind =>
        <div className="side-tab-body" key={selected+kind} hidden={visibleTab !== kind}>
          <WorkspaceSidePane kind={kind} resources={codeResources}/>
        </div>)}
      {(["environment","handoff"] as const).filter(kind => side.tabs.includes(kind)).map(kind =>
        <div className="side-tab-body" key={selected+kind} hidden={visibleTab !== kind}>
          <ConversationWorkspace conversation={selected} kind={kind} resources={codeResources} onChanged={() => currentScope.refetch()}
            initialResourceId={kind==="handoff" ? chosenWorktree?.resourceId : undefined}/>
        </div>)}
      {side.tabs.filter(tab => tab.startsWith("chat:")).map(tab => {
        const chat = related.find(item => item.id === tab.slice(5));
        return <div className="side-tab-body" key={tab} hidden={visibleTab !== tab}>
          {chat ? <ResourceLinkOwner.Provider value={chat.id}>
            <RelatedChatPane chat={chat} visible={visible&&visibleTab===tab} onRead={cursor=>setSideChatRead(old=>old?.tab===tab&&old.owner===selected&&old.cursor===cursor?old:{tab,owner:selected,cursor})}/>
          </ResourceLinkOwner.Provider> : <p>此侧边聊天已不可用。</p>}
        </div>;
      })}
    </SideTabs>}>
    {projectCreation && <NewFolderProject onCreate={newFolderProject} onImported={adoptFolderProject}
      onClose={closeProjectCreation}/>}
    {projectRemoval && <div className="new-project-backdrop" onClick={() => { if (!removingProject) setProjectRemoval(undefined); }}>
      <Dialog className="new-project-dialog remove-project-dialog" label={t("从 Modot 移除项目")}
        onClose={() => { if (!removingProject) setProjectRemoval(undefined); }}>
        <header><h2>{t("从 Modot 移除项目")}</h2></header>
        <p>{t("将移除「{name}」的项目入口。目录和已有会话会保留，可从已移除的项目中恢复。", { name: projectRemoval.name })}</p>
        {projectRemoval.folderResourceId && <p className="work-folder-location">
          {projectRemoval.resources[projectRemoval.folderResourceId]?.path}
        </p>}
        {projectRemovalError && <p role="alert">{projectRemovalError}</p>}
        <footer>
          <button type="button" disabled={removingProject} onClick={() => setProjectRemoval(undefined)}>{t("取消")}</button>
          <button type="button" disabled={removingProject} onClick={() => void confirmProjectRemoval()}>
            {removingProject ? t("正在移除…") : t("移除项目")}
          </button>
        </footer>
      </Dialog>
    </div>}
    {selected&&createPortal(<>
      <nav className="workflow-nav" aria-label={t("会话资料与任务")}>
        {([["materials","资料与成果"],["task","任务与验收"],["records","运行记录"]] as const).map(([id,title]) =>
          <button key={id} data-workflow-target={id} aria-pressed={workflowPane===id}
            onClick={()=>setWorkflowPane(id)}>{t(title)}</button>)}
      </nav>
      <section className="workflow-options" data-workflow-pane="materials" hidden={workflowPane!=="materials"}>
          {selected && (
            <div className="settings-card" data-setting-item="project-scope">
            <ProjectScope
              key={"scope:" + selected}
              conversationId={selected}
              onChange={() => conversations.refetch()}
              onError={fail}
              onBusyChange={setProjectBusy}
            />
            </div>
          )}
          {selected && (
            <ArtifactPane
              key={"artifacts:" + selected}
              conversationId={selected}
              focus={artifactFocus?.owner === selected ? artifactFocus : undefined}
              onError={fail}
              onAsk={(reference) =>
                setDrafts((old) => ({
                  ...old,
                  [selected]: (old[selected] || "") + "\n" + reference,
                }))
              }
            />
          )}
      </section>
      <section className="workflow-options" data-workflow-pane="task" hidden={workflowPane!=="task"}>
          {selected && !thread?.subtaskOf && (
            <IntegrationPane
              key={"integrations:" + selected}
              conversationId={selected}
              onSelect={showConversation}
              onDelegate={(text) =>
                setDrafts((old) => ({
                  ...old,
                  [selected]: (old[selected] || "") + "\n" + text,
                }))
              }
            />
          )}
          {selected && !thread?.readonlyEvidence && !thread?.subtaskOf && (
            <DelegationControl
              key={"delegation:" + selected}
              conversationId={selected}
              visible={settingsVisible && workflowPane === 'task'}
              runActive={!!thread?.runtime?.active}
              onDraft={requestDraft}
              onInspect={(id) => inspectEvidence(id)}
            />
          )}
          {selected && !thread?.readonlyEvidence && !thread?.subtaskOf && (
            <CorrectionQueue
              key={"corrections:" + selected}
              conversationId={selected}
              active={!!thread?.runtime?.active}
              onInspect={(id) => inspectEvidence(id)}
              onResume={async () => {
                await api(base + "/threads/" + selected + "/send", {
                  text: "请处理尚未应用的纠正意见，遵守其目标版本与现有授权。",
                  crew: "l0",
                  expectedScopeRevision: currentScope.data?.revision,
                });
                await conversations.refetch();
              }}
            />
          )}
          {selected && !thread?.readonlyEvidence && !thread?.subtaskOf && (
            <DeliveryReview
              key={"delivery:" + selected}
              conversationId={selected}
              scope={currentScope.data}
              onInspect={(id) => inspectEvidence(id)}
            />
          )}
          {selected && !thread?.readonlyEvidence && !thread?.subtaskOf && (
            <RestorationHistory
              key={"restore-history:" + selected}
              conversationId={selected}
              onDelegate={delegateRestoration}
            />
          )}
      {selected&&<SubtaskPane key={selected} conversationId={selected} thread={thread} onSelect={showConversation} onOpenTask={()=>openSideTab("agents")} onDelegate={text=>setDrafts(old=>({...old,[selected]:(old[selected]||'')+'\n'+text}))} />}
      </section>
      <section className="workflow-options" data-workflow-pane="records" hidden={workflowPane!=="records"}>
        <RuntimeModelBadge id={selected} runId={thread?.runtime?.run_id}/>
          {thread?.discussionOf && (
            <DiscussionContext
              key={"context:" + selected}
              conversationId={selected}
              onParent={showConversation}
            />
          )}
          {!models.isPending && (!models.data?.effective?.main?.model || models.error) && (
            <div role="status">
              请检查模型配置后开始对话。
              <button onClick={() => openSettings()}>配置模型</button>
            </div>
          )}
          {execution.error && (
            <p role="alert">执行设置无法核对：{execution.error.message}</p>
          )}
          {selected && (
            <>
              <CodeActivity
                key={"code:" + selected}
                conversationId={selected}
              />
              <DebugEvidence
                key={"debug:" + selected}
                conversationId={selected}
                onWorkbench={onWorkbench}
              />
            </>
          )}
          {selected && (
            <ApplicationPane
              key={"applications:" + selected}
              conversationId={selected}
              onError={fail}
              onFeedback={(text) =>
                setDrafts((old) => ({
                  ...old,
                  [selected]: (old[selected] || "") + "\n" + text,
                }))
              }
            />
          )}

      </section>
    </>,workflowSettings)}
    <RetainedDialog open={contextOpen} label="上下文详细记录" onClose={()=>setContextOpen(false)}>
      {selected&&<ContextInspection key={selected} conversationId={selected} forceOpen={contextOpen}/>}
    </RetainedDialog>
    {discussionDraft&&<RetainedDialog open={true} compact label={t("用这段回复开新会话")} onClose={()=>setDiscussionDrafts(old=>{const next={...old};delete next[selected];return next;})}>
      <DiscussionDraftDialog value={discussionDraft} busy={!!discussionOwners[selected]?.busy}
        error={discussionOwners[selected]?.draft === discussionDraft ? discussionOwners[selected]?.error : undefined}
        onChange={v=>setDiscussionDrafts(old=>({...old,[v.sourceId]:v}))} onCreate={createDiscussion}/>
    </RetainedDialog>}
      {selected && evidenceOpen && (
        <EvidenceInspector
          key={evidenceRequest}
          conversationId={evidenceOwner || selected}
          selected={evidenceAction}
          initialRun={evidenceRun}
          initialGap={evidenceGap}
          onSelect={setEvidenceAction}
          onClose={() => setEvidenceOpen(false)}
          onReview={location => {setEvidenceOpen(false);openCodeReview(location);}}
          allowCorrections={!thread?.readonlyEvidence && !thread?.subtaskOf && !evidenceOwner}
          onRestore={thread?.readonlyEvidence || thread?.subtaskOf || evidenceOwner ? undefined : delegateRestoration}
          onQuestion={
            thread?.readonlyEvidence
              ? undefined
              : async (child) => {
                  setDrafts((old) => ({ ...old, [child.id]: child.text }));
                  await conversations.refetch();
                  if (selectionRef.current === selected) {
                    select(child.id);
                  }
                }
          }
        />
      )}
  </LegacyPage></div>
    <div className="application-surface scheduled-page" data-surface="schedules" hidden={!schedulesActive} inert={settingsVisible}>
      <ScheduledTasks active={schedulesActive&&!settingsVisible} planRequest={scheduleRequest}
        projects={projects.data?.projects ?? []} projectsUnavailable={projects.isError}
        conversations={sidebarThreads} conversationsUnavailable={conversations.isError}
        returnLabel={t(selected ? "返回会话" : "返回新聊天")} onReturn={closeScheduledPage}
        onOpenConversation={(id, runId)=>{
          onOpenChat();
          selectFromSidebar(id,true);
          if(runId) setRoundJump({owner:id,id:runId});
          void conversations.refetch();
        }}/>
    </div></WorkroomChangeLinks>;
}
