// 应用外壳负责视图切换和资料交接；工程区维护文件草稿、版本冲突与人工运行。
// 执行和授权结果由后端提供，本地存储保留界面状态及用户尚未提交的内容。
// 主要引用方包括 `main.tsx` 和 `AgentHome.tsx`。

import {lazy,Suspense,useEffect,useRef,useState} from "react";
import {useQueryClient} from '@tanstack/react-query';
import {loadJSON,saveJSON} from "./client";
import {DEFAULT_SHORTCUTS,bound,type ShortcutName} from "./shortcuts";
import {ApplicationSettings,useApplicationSettings} from "./ApplicationSettings";
import {InterfaceZoomControl} from "./InterfaceZoom";
import { UiLanguageProvider, useUiLanguage } from "./UiLanguage";
import type { UiMessage } from "./uiMessages";
import type {DraftTarget,PendingHandoff,ToolHandoff,WriteActiveDraft} from "./ToolHandoff";
import {AgentHome} from "./AgentHome";
import {ConversationDockProvider} from "./ConversationDock";
import {Dialog} from "./Dialog";
import {MemoryPage} from "./ManagementPages";
import {UiIcon} from "./UiIcon";
import {PanelBoundary} from "./Panels";
import desktopPackage from "../../package.json";
import {ApplicationNavigation} from './ApplicationNavigation';
import {builtinApplications} from './builtinApplications';
import type {ManagementViewRequest} from './PluginManagementShell';
import {useDesktopApplications} from './useDesktopApplications';
import {usePluginLogin} from './usePluginLogin';
import {catalogReadKey, type CatalogRead, type InstalledApplication} from './pluginApplications';
import type { WorkroomFileRequest, WorkroomFileTarget } from "./WorkroomChangeLink";
import {ResourceLinks, type ImageArtifactPage, type LinkedFileTarget, type ResourceSidePage, type WebLinkPage} from "./ResourceLinks";
import type {ResourceTarget} from "./resourceOpeners";
import {BrowserPane} from "./BrowserPane";
import "./workroom-link-pages.css";
const WorkroomPage=lazy(()=>import("./WorkroomPage"));
const PluginManagementShell=lazy(()=>import('./PluginManagementShell'));
const PluginApplicationHome=lazy(()=>import('./PluginApplicationHome'));
const DataPluginApplication=lazy(()=>import('./DataPluginApplication'));
const GitHubWorkroom=lazy(()=>import('./GitHubWorkroom'));

// 菜单命令由应用壳交给工作间或会话。`serial` 每次递增，区分连续两次相同操作。
export type ChromeCommand = { action: "new-chat" | "new-project" | "workspace" | "right-sidebar" | "history"; serial: number };
const menus = {file:"文件", edit:"编辑", view:"视图", help:"帮助"} as const;
const editCommands = {undo:"撤销", redo:"重做", cut:"剪切", copy:"复制", paste:"粘贴", selectAll:"全选"} as const;
const helpPages = {shortcuts:"快捷键", about:"关于 Modot"} as const;

// 冷启动进入新聊天，将用户在当前窗口选中的会话同步给设置和工作间。
export function App() {
  const [conversationId,setConversationId]=useState("");
  return <UiLanguageProvider><ApplicationSettings conversationId={conversationId}><ConversationDockProvider owner={conversationId}>
    <ApplicationShell conversationId={conversationId} onSelection={setConversationId}/>
  </ConversationDockProvider></ApplicationSettings></UiLanguageProvider>;
}

// 页面首次访问后保持挂载，切换视图或打开设置时隐藏/禁用交互，保留草稿和局部状态。
function ApplicationShell({conversationId,onSelection}:{conversationId:string;onSelection:(id:string)=>void}) {
  const {t} = useUiLanguage();
  const {preferences,zoom,theme,openSettingsAt,closeSettings,settingsPage,catalogRequest}=useApplicationSettings();
  useEffect(()=>{window.workbench.chromeTheme(theme).catch(()=>{});},[theme]);
  const [memoryVisited,setMemoryVisited]=useState(false),[extensionsVisited,setExtensionsVisited]=useState(false);
  const [managementRequest,setManagementRequest]=useState<ManagementViewRequest>();
  const [handoff, setHandoff] = useState<PendingHandoff>();
  const [draftTarget,setDraftTarget] = useState<DraftTarget>();
  const draftWriter = useRef<WriteActiveDraft>(null);
  const [workroomFile, setWorkroomFile] = useState<WorkroomFileRequest>();
  const [sidebarPages,setSidebarPages]=useState<ResourceSidePage[]>([]);
  const [workroomPages,setWorkroomPages]=useState<WebLinkPage[]>([]);
  const [workroomTabs,setWorkroomTabs]=useState<Record<string,string>>({});
  const ownedWorkroomPages=workroomPages.filter(page=>page.owner===conversationId);
  const activeWorkroomPage=ownedWorkroomPages.find(page=>page.id===workroomTabs[conversationId]);
  const fileSerial = useRef(0);
  const handoffSerial = useRef(0);
  const [surface, setSurface] = useState("agent");
  const queryClient = useQueryClient();
  const {catalog, apps, shortcuts, github, offer, error:bundleError, retry:retryBundle} = useDesktopApplications(theme);
  const login = usePluginLogin(catalog.complete, catalog.packages);
  const pluginSurface = surface.startsWith('plugin:');
  const activeApp = apps.find(app => surface === 'plugin:' + app.id);
  const [visitedGitHub, setVisitedGitHub] = useState("");
  const githubSurface = github && 'plugin:' + github.id;
  // 用已登记的应用实例 ID 切换页面，包内同名页仍有独立身份。
  const openApplication = (id:string) => {
    if (switchSurface('plugin:' + id) && id === github?.id) setVisitedGitHub(id);
  };
  useEffect(() => {
    if (surface === 'extensions' || pluginSurface) void queryClient.invalidateQueries({queryKey:['plugins','catalog']});
  }, [surface,pluginSurface,queryClient]);
  useEffect(() => {
    if (!settingsPage && pluginSurface && catalog.complete && !activeApp) openCatalog({page:'packages'});
  }, [settingsPage,pluginSurface,catalog.complete,activeApp]);
  const [toolsVisited, setToolsVisited] = useState(false);
  const [leftOpen, setLeftOpen] = useState(() => loadJSON("conversation-left-open", !matchMedia("(max-width: 780px)").matches));
  const [command, setCommand] = useState<ChromeCommand>();
  const [newChatBlockedReason, setNewChatBlockedReason] = useState("");
  const [menu, setMenu] = useState<"" | keyof typeof menus>("");
  const [help, setHelp] = useState<"" | keyof typeof helpPages>("");
  const titlebar = useRef<HTMLElement>(null);
  const editable = useRef<HTMLElement | null>(null);
  const [canEdit, setCanEdit] = useState(false);
  const editableSelector = 'textarea:not([readonly]):not([disabled]),input:is([type="text"],[type="search"],[type="url"],:not([type])):not([readonly]):not([disabled]),[contenteditable="true"]';
  useEffect(() => saveJSON("conversation-left-open", leftOpen), [leftOpen]);
  useEffect(() => {
    // 在标题栏菜单打开前跟踪原可编辑元素，用于后续原生编辑命令。
    function focus(event: FocusEvent) {
      const target = event.target as HTMLElement;
      if (titlebar.current?.contains(target)) return;
      const field = target.closest<HTMLElement>(editableSelector);
      editable.current = field;
      setCanEdit(!!field);
    }
    // 标题栏之外的指针操作退出已展开菜单。
    function close(event: PointerEvent) { if (!titlebar.current?.contains(event.target as Node)) setMenu(""); }
    document.addEventListener("focusin", focus);
    document.addEventListener("pointerdown", close);
    return () => {document.removeEventListener("focusin", focus);document.removeEventListener("pointerdown", close);};
  }, []);
  // 离开应用设置须通过未保存编辑的离开检查。已访问应用保留本地状态。
  function switchSurface(value: string) {
    if (!closeSettings(false)) return false;
    setMenu("");
    if (value !== "workbench") setWorkroomFile(undefined);
    if (value === "workbench") setToolsVisited(true);
    if(value==='memory')setMemoryVisited(true);
    if(value==='extensions')setExtensionsVisited(true);
    setSurface(value);
    return true;
  }
  function openWorkroomFile(file: WorkroomFileTarget) {
    if (settingsPage) return;
    setWorkroomTabs(old=>({...old,[conversationId]:""}));
    setWorkroomFile({ ...file, serial: ++fileSerial.current });
    switchSurface("workbench");
  }
  function openWebPage(target:ResourceTarget,place:"sidebar"|"workroom") {
    if(settingsPage||target.kind!=="web"||!conversationId)return;
    const page:WebLinkPage={kind:"web",id:crypto.randomUUID(),owner:conversationId,url:target.href,label:target.label};
    if(place==="workroom"){
      setWorkroomPages(old=>[...old,page]);
      setWorkroomTabs(old=>({...old,[conversationId]:page.id}));
    }else setSidebarPages(old=>[...old,page]);
    switchSurface(place==="workroom"?"workbench":"agent");
  }
  function openFileReview(target:LinkedFileTarget) {
    if(settingsPage||!conversationId)return;
    const label=target.file.path.split("/").at(-1)||target.label;
    setSidebarPages(old=>[...old,{kind:"file",id:crypto.randomUUID(),owner:conversationId,label,target}]);
    switchSurface("agent");
  }
  function openImageArtifact(owner:string, artifactId:string, versionId:string, label:string) {
    if (!owner || owner !== conversationId || settingsPage) return;
    const id = "image:" + owner + ":" + artifactId + ":" + versionId;
    const page:ImageArtifactPage = {kind:"artifact-image", id, owner, label, artifactId, versionId};
    setSidebarPages(old => old.some(item => item.id === id) ? old : [...old, page]);
    switchSurface("agent");
    return id;
  }
  function closeWorkroomPage(id:string) {
    setWorkroomPages(old=>old.filter(page=>page.id!==id));
    setWorkroomTabs(old=>old[conversationId]===id?{...old,[conversationId]:""}:old);
  }
  // 扩展管理用序号区分连续打开的同一个目标页。
  function openCatalog(target: ManagementViewRequest['target']) {
    if (!switchSurface('extensions')) return;
    setManagementRequest(old => ({serial:(old?.serial ?? 0) + 1, target}));
  }
  useEffect(() => { if (catalogRequest) openCatalog({page:'packages'}); }, [catalogRequest]);
  // 插件页面的内容交给原会话草稿，归属已切换时丢弃迟到的交接。
  function addToDraft(value:ToolHandoff, owner:string) {
    if (owner !== conversationId) return;
    setHandoff({...value,id:++handoffSerial.current,owner});switchSurface('agent');
  }
  function appendDataPageDraft(owner: string, text: string) {
    if (settingsPage || owner !== conversationId || !draftWriter.current) throw Error('当前会话草稿已变化。');
    draftWriter.current(owner, text);
    switchSurface('agent');
  }
  function applicationHome(app: InstalledApplication) {
    const server = app.pkg.servers.find(item => item.name === app.connection?.server);
    const loginPackage = catalog.packages.find(pkg => pkg.id === app.packageId && pkg.revision === app.pkg.revision);
    const catalogRead = app.connection ? queryClient.getQueryData<CatalogRead>(catalogReadKey(app.pkg,app.connection.server)) : undefined;
    return <PluginApplicationHome key={app.id} app={app} theme={theme} fontSize={preferences.fontSize} catalogRead={catalogRead}
      onManage={()=>openCatalog({page:'package',id:app.packageId,manage:true})}
      onConnect={catalog.complete && loginPackage && server?.githubAppConfigured
        ? () => void login.start(loginPackage, server.name, app.name) : undefined}
      login={login}
      onDraft={value=>addToDraft(value,conversationId)}/>;
  }
  // 同一个菜单动作也递增序号，让使用方能区分连续两次相同的用户操作。
  function dispatchChrome(action: ChromeCommand["action"]) {
    if (!switchSurface(action === "workspace" ? "workbench" : "agent")) return;
    setCommand(old => ({action,serial:(old?.serial ?? 0)+1}));
  }
  // 应用菜单会取得焦点；先记住原输入框，执行编辑命令前再确认它仍可交互。
  function captureEditTarget() {
    const active = document.activeElement;
    const field = active?.closest<HTMLElement>(editableSelector);
    if(field)editable.current=field;
    else if(!active||!titlebar.current?.contains(active))editable.current=null;
    setCanEdit(!!editable.current?.isConnected && !editable.current.closest("[inert],[hidden]"));
  }
  // 原焦点元素仍在活动界面时才交给宿主编辑；编辑选择全部使用输入框原选择能力。
  function editField(id: Parameters<typeof window.workbench.edit>[0]) {
    const target=editable.current;
    if(!target?.isConnected||target.closest("[inert],[hidden]"))return;
    target.focus({preventScroll:true});
    if(id==="selectAll"&&(target instanceof HTMLTextAreaElement||target instanceof HTMLInputElement))target.select();
    else void window.workbench.edit(id).catch(()=>{});
  }
  const navEntries = builtinApplications.map(entry=>[entry.id,entry.name] as const);
  const panelDisabled = !!settingsPage || surface !== "agent";
  // 先关闭标题栏菜单再执行命令，保留禁用原因的可访问描述。
  const menuButton = (id: string, label: UiMessage, fn: () => void, disabled = false, describedBy?: string) =>
    <button key={id} data-menu-command={id} type="button" disabled={disabled} aria-describedby={describedBy} onClick={()=>{setMenu("");fn();}}>{t(label)}</button>;
  const inWorkroom=surface==='workbench'&&!settingsPage;
  const navigation=<ApplicationNavigation surface={surface} theme={theme} settingsOpen={!!settingsPage} onSelect={switchSurface} onSettings={()=>{setMenu("");openSettingsAt('appearance');}}
    applications={shortcuts} onSelectApplication={openApplication} onApplications={()=>openCatalog({page:'applications'})}/>;
  return (
    <ResourceLinks owner={conversationId} scope={conversationId+":"+surface+":"+!!settingsPage}
      onWeb={openWebPage} onFile={openWorkroomFile} onReview={openFileReview}>
    <div className={'application-shell theme-'+theme} data-settings-open={!!settingsPage} data-workroom-open={inWorkroom}>
      <header ref={titlebar} className="application-titlebar" aria-label={t("应用顶栏")} onKeyDown={e=>{if(e.key==="Escape"&&menu){e.stopPropagation();e.currentTarget.querySelector<HTMLButtonElement>(`[data-app-menu="${menu}"]`)?.focus();setMenu("");}}}>
        {inWorkroom?<><strong className="workroom-brand"><b aria-hidden="true">m</b>Modot</strong>
          <span className="workroom-window-title">{activeWorkroomPage?t("工作间"):t("代码工作间")}</span></>:
          <><button type="button" className="conversation-sidebar-toggle" disabled={panelDisabled} aria-label={leftOpen?t("收起会话侧栏"):t("展开会话侧栏")} aria-expanded={leftOpen} aria-controls="conversation-sidebar" onClick={()=>{setMenu("");setLeftOpen(open=>!open);}}><UiIcon name="sidebar"/></button>
          {(!leftOpen || panelDisabled) && <strong className="application-brand">Modot</strong>}</>}
        <nav aria-label={t("应用菜单")}>{(Object.keys(menus) as (keyof typeof menus)[]).map(id=><div className="application-menu" key={id}>
          <button type="button" data-app-menu={id} aria-expanded={menu===id} onPointerDown={id==="edit"?captureEditTarget:undefined} onClick={()=>{if(id==="edit")captureEditTarget();setMenu(menu===id?"":id);}}>{t(menus[id])}</button>
          {menu===id&&<div className="application-menu-items" aria-label={t("{name}菜单",{name:t(menus[id])})}>
            {id==="file"&&<>
              {menuButton("new-chat","新建对话",()=>dispatchChrome("new-chat"),!!newChatBlockedReason,newChatBlockedReason?"menu-new-chat-status":undefined)}
              {newChatBlockedReason&&<small id="menu-new-chat-status" className="application-menu-status" role="status">{newChatBlockedReason}</small>}
              {menuButton("new-project","新建项目",()=>dispatchChrome("new-project"))}
              {menuButton("workspace","打开工作区",()=>dispatchChrome("workspace"))}
            </>}
            {id==="edit"&&(Object.keys(editCommands) as (keyof typeof editCommands)[]).map(command=>menuButton(command,editCommands[command],()=>editField(command),!canEdit))}
            {id==="view"&&<>
              <InterfaceZoomControl zoom={zoom} className="application-menu-zoom"/>
              {menuButton("left-sidebar",leftOpen?"收起会话侧栏":"展开会话侧栏",()=>setLeftOpen(open=>!open),panelDisabled)}
              {menuButton("right-sidebar","切换右侧栏",()=>dispatchChrome("right-sidebar"),panelDisabled||!conversationId)}
              {menuButton("history","对话历史",()=>dispatchChrome("history"),panelDisabled||!conversationId)}
              {navEntries.map(([app,name])=>menuButton(app,name,()=>switchSurface(app)))}
            </>}
            {id==="help"&&(Object.keys(helpPages) as (keyof typeof helpPages)[]).map(page=>menuButton(page,helpPages[page],()=>setHelp(page)))}
          </div>}
        </div>)}</nav>
      </header>
      {navigation}
      {github && visitedGitHub === github.id && <div className="application-surface" data-surface={githubSurface}
        inert={!!settingsPage} hidden={surface!==githubSurface}>
        <PanelBoundary><Suspense fallback={<p role="status">{t("正在打开 GitHub…")}</p>}>
          <GitHubWorkroom key={github.id} conversationId={conversationId} visible={surface===githubSurface&&!settingsPage}
            pluginId={github.packageId} pluginRevision={github.pkg.revision}
            canQuote={draftTarget?.id===conversationId && !!conversationId}
            onOpenConversation={()=>switchSurface("agent")} connection={applicationHome(github)}
            onQuote={(owner,text,source)=>{
              if (!draftWriter.current) throw Error(t("当前会话草稿已变化。"));
              draftWriter.current(owner,text,source);
            }}/>
        </Suspense></PanelBoundary>
      </div>}
      {pluginSurface && surface!==githubSurface && <div className="application-surface" inert={!!settingsPage} data-surface={surface}>
        <PanelBoundary><Suspense fallback={<p role="status">正在打开插件应用…</p>}>{activeApp
          ? activeApp.dataPageRevision !== undefined
            ? <DataPluginApplication key={activeApp.id} app={{...activeApp,dataPageRevision:activeApp.dataPageRevision}}
                fontSize={preferences.fontSize} login={login} interactive={!settingsPage}
                target={draftTarget?.id === conversationId ? draftTarget : undefined} onAppend={appendDataPageDraft}
                onManage={()=>{if(!settingsPage)openCatalog({page:'package',id:activeApp.packageId,manage:true});}}/>
            : applicationHome(activeApp)
          : <p role="status">{catalog.error || t("正在核对安装记录…")} {catalog.error && <button onClick={()=>void catalog.retry()}>{t("重新读取插件")}</button>} <button onClick={()=>openCatalog({page:'packages'})}>{t("查看插件目录")}</button></p>}
        </Suspense></PanelBoundary>
      </div>}
      <AgentHome chatActive={surface==="agent"} schedulesActive={surface==="schedules"}
          onOpenSchedules={()=>switchSurface("schedules")} onOpenChat={()=>switchSurface("agent")} sidebarOpen={leftOpen} onHideSidebar={()=>setLeftOpen(false)} chromeCommand={command} onWorkbench={() => switchSurface("workbench")} onOpenWorkroomFile={openWorkroomFile} handoff={handoff}
          linkPages={sidebarPages} onCloseLinkPage={id=>setSidebarPages(old=>old.filter(page=>page.id!==id))}
          onOpenImageArtifact={openImageArtifact}
          onOpenWeb={(owner,url)=>{if(owner===conversationId)openWebPage({kind:"web",href:url.href,label:url.host,conversationId:owner},"sidebar");}}
          onHandoffHandled={() => setHandoff(undefined)} onSelection={onSelection}
          onDraftTarget={setDraftTarget} draftWriter={draftWriter} onNewChatBlockedReason={setNewChatBlockedReason} />
      {memoryVisited&&<div className="application-surface" inert={!!settingsPage} data-surface="memory" hidden={surface!=='memory'}><MemoryPage visible={surface==='memory'} conversationId={conversationId} onManageMemory={()=>openCatalog({page:'memory'})}/></div>}
      {extensionsVisited&&<div className="application-surface" inert={!!settingsPage} data-surface="extensions" hidden={surface!=='extensions'}>
        <PanelBoundary><Suspense fallback={<p role="status">正在打开插件管理…</p>}>
          <PluginManagementShell visible={surface==='extensions'&&!settingsPage} request={managementRequest} conversationId={conversationId}
            catalog={catalog} offer={offer} bundleError={bundleError} onRetryBundle={retryBundle} login={login}
            applications={apps} onOpenApplication={openApplication} onWorkroom={()=>switchSurface('workbench')} onUseSkill={addToDraft}/>
        </Suspense></PanelBoundary>
      </div>}
      {toolsVisited && <div className="application-surface workroom-link-surface" inert={!!settingsPage} hidden={surface !== "workbench"} data-surface="workbench">
        {!!ownedWorkroomPages.length&&<nav className="workroom-web-tabs" aria-label={t("工作间标签")}>
          <button type="button" aria-current={!activeWorkroomPage?"page":undefined} onClick={()=>setWorkroomTabs(old=>({...old,[conversationId]:""}))}>{t("文件")}</button>
          {ownedWorkroomPages.map(page=><div key={page.id} data-current={activeWorkroomPage?.id===page.id}>
            <button type="button" title={page.label} aria-current={activeWorkroomPage?.id===page.id?"page":undefined}
              onClick={()=>setWorkroomTabs(old=>({...old,[conversationId]:page.id}))}>{page.label}</button>
            <button type="button" aria-label={`关闭${page.label}标签`} onClick={()=>closeWorkroomPage(page.id)}><UiIcon name="close"/></button>
          </div>)}
        </nav>}
        <div className="workroom-file-view" hidden={!!activeWorkroomPage}>
        <PanelBoundary><Suspense fallback={<p role="status">正在打开工作间…</p>}><WorkroomPage conversationId={conversationId} chromeCommand={command}
          fileRequest={workroomFile} onClearFileRequest={()=>setWorkroomFile(undefined)} onOpenConversation={()=>switchSurface('agent')}
          visible={inWorkroom && !activeWorkroomPage} onHandoff={(value,newDraft,stayInWorkroom)=>{
          setHandoff({...value,id:++handoffSerial.current,owner:newDraft?"":conversationId});
          if(newDraft || !stayInWorkroom)switchSurface("agent");
        }}/></Suspense></PanelBoundary>
        </div>
        {workroomPages.map(page=><div className="workroom-web-page" key={page.id} hidden={activeWorkroomPage?.id!==page.id}>
          <BrowserPane conversationId={page.owner} initialUrl={page.url} visible={inWorkroom&&activeWorkroomPage?.id===page.id}
            onClose={()=>closeWorkroomPage(page.id)}/>
        </div>)}
      </div>}
      {settingsPage}
      {help&&<div className="modal-backdrop"><Dialog className="application-help" label={t(helpPages[help])} onClose={()=>setHelp("")}>
        <header><h2>{t(helpPages[help])}</h2><button onClick={()=>setHelp("")}>{t("关闭")}</button></header>
        {help==="shortcuts"?<dl>{(Object.keys(DEFAULT_SHORTCUTS) as ShortcutName[]).map(key=><div key={key}><dt>{t(({files:"打开文件",commands:"打开命令",terminal:"终端",send:"发送",save:"保存",previous:"前个会话",next:"下个会话"} as const)[key])}</dt><dd><kbd>{bound(key,preferences.shortcuts)}</kbd></dd></div>)}</dl>:<p>Modot {desktopPackage.version}</p>}
      </Dialog></div>}
    </div>
    </ResourceLinks>
  );
}
