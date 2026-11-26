// 承载桌面设置各页并管理模型档案、权限模式与窗口行为等全局设置。
// 主要引用方包括 `ApplicationSettings.tsx`。

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, workbenchBootstrapPath } from "./client";
import { useUiLanguage, type LanguageChoice } from "./UiLanguage";
import { isUiMessage, type UiMessage } from "./uiMessages";
import {
  DEFAULT_SHORTCUTS,
  chord,
  bound,
  type ShortcutName,
} from "./shortcuts";
import {ConversationSettings,type DisplayPreferences} from "./ConversationSettings";
import {permissionOptions} from './ComposerPermissions';
import {PermissionReview} from './PermissionReview';
import type {ExecutionConfig, VisionCorePreset} from "./generated";
import {LocalStorageSettings} from "./LocalStorageSettings";
import {UiIcon} from "./UiIcon";
import {WorkFolderSettings} from './NewFolderProject';
import {InterfaceZoomControl,type InterfaceZoom} from './InterfaceZoom';

export type Theme = "light"|"dark"|"contrast";
// 本机展示偏好。
// `system` 让外观跟随系统，`shortcuts` 只存用户覆盖项，其余从默认快捷键表读取。
export type Preferences = {
  fontSize: number;
  codeSize?: number;
  theme: Theme|"system";
  compact: boolean;
  shortcuts?: Partial<typeof DEFAULT_SHORTCUTS>;
};
const groups = [
  ["应用",[["general","常规","settings"],["appearance","外观","gear"],["shortcuts","快捷键","terminal"]]],
  ["Agent",[["conversation","对话与执行","compose"],["resources","会话资料与任务","sources"],["models","模型","model"],["permissions","权限","shield"]]],
  ["管理",[["storage","存储与归档","memory"],["extensions","扩展与连接","extensions"],["worktrees","本地环境与工作树","branch"]]],
] as const;
const pages=groups.flatMap(([,items])=>[...items]);
const options = [
  ["general","language","界面语言","language English 中文 语言 locale"],
  ["general","general","常规显示","输出 展示"],
  ["general","close-window","关闭窗口时","退出 后台 托盘 保留"],
  ["general","work-folders","工作总目录","文件夹 项目 会话 路径 保存 工作文件"],
  ["appearance","theme","主题","深色 浅色 颜色 跟随系统 高对比度 dark light"],
  ["appearance","zoom","界面缩放","比例 120% 放大 缩小 scale"],
  ["appearance","font","界面字号","字体 大小 font"],
  ["appearance","code","代码字号","终端 差异 字体 font"],
  ["shortcuts","shortcuts","键盘快捷键","按键 Ctrl Enter Ctrl P Ctrl S"],
  ["conversation","execution","对话与执行","工具 动作 输出 角色 上下文"],
  ["resources","project-scope","项目与资料范围","授权 成果 任务与验收 运行记录 scope sources"],
  ["models","key","供应商与 API Key","模型 密钥 凭据 连接 DeepSeek"],
  ["models","vision-core","视觉核心模型","任务核心 视觉 图片 投影 读图 模型 供应商"],
  ["permissions","permission","默认权限","批准 审批 完全访问"],
  ["storage","storage","存储空间","磁盘 容量 占用 刷新 storage"],
  ["storage","archive","已归档的会话","历史 恢复 archive"],
  ["extensions","extensions","插件与外部工具","MCP Skill Hook 连接"],
  ["worktrees","worktree","托管工作树","worktree git checkout 本地环境"],
] as const;
// 从 `lifecycle-v2.cjs` 读取并保存窗口关闭方式，控件只显示宿主确认的值。
function WindowCloseSetting() {
  const { t } = useUiLanguage();
  const query = useQueryClient();
  const current = useQuery({
    queryKey: ["window-close-behavior"],
    queryFn: () => window.workbench.closeBehavior(),
  });
  const save = useMutation({
    mutationFn: (value: "background" | "quit") => window.workbench.closeBehavior(value),
    onSuccess: value => query.setQueryData(["window-close-behavior"], value),
  });
  return <>
    <label className="settings-line">{t("关闭窗口时")}
      <select aria-label={t("关闭窗口时")} value={save.isPending ? save.variables : current.data ?? ""}
        disabled={!current.data || save.isPending}
        onChange={event => save.mutate(event.currentTarget.value as "background" | "quit")}>
        {!current.data && <option value="">{t("读取中")}</option>}
        <option value="background">{t("保留后台运行")}</option>
        <option value="quit">{t("结束并退出")}</option>
      </select>
    </label>
    <small>{t("保留后台时，任务继续运行，可从系统托盘重新打开。")}</small>
    {current.isError && <p role="alert">{t("无法读取关闭方式。")}<button onClick={() => void current.refetch()}>{t("重试")}</button></p>}
    {save.isError && <p role="alert">{t("保存关闭方式失败，请重试。")}</p>}
  </>;
}

const visionPresetPath = "/api/v2/plugins/presets/vision";

function VisionCoreSwitch({modelRevision}: {modelRevision?: string}) {
  const {t} = useUiLanguage();
  const client = useQueryClient();
  const queryKey = ["plugins", "vision-core-preset", modelRevision] as const;
  const preset = useQuery({
    queryKey,
    queryFn: () => api<VisionCorePreset>(visionPresetPath),
  });
  const toggle = useMutation({
    mutationFn: (change: {enabled: boolean; fingerprint: string; expectedRevision: number | null}) =>
      api<VisionCorePreset>(visionPresetPath, change),
    onSuccess: result => {
      client.setQueryData(queryKey, result);
      void client.invalidateQueries({queryKey: ["plugins"]});
    },
    onError: () => { void preset.refetch(); },
  });
  const current = preset.data;
  return <>
    <label className="simple-switch">{t("启用视觉核心")}
      <input type="checkbox" role="switch" checked={current?.enabled ?? false}
        disabled={!current || preset.isError || toggle.isPending}
        onChange={event => current && toggle.mutate({
          enabled: event.currentTarget.checked, fingerprint: current.fingerprint, expectedRevision: current.revision,
        })}/>
    </label>
    {current?.model && <small>{t("当前使用 {provider} · {model}", {provider: current.model.provider, model: current.model.model})}</small>}
    {current?.enabled && current.modelState === "worker_model_missing" &&
      <small>{t("请先为视觉核心选择模型连接。")}</small>}
    {current?.enabled && current.modelState === "worker_model_same" &&
      <small>{t("请选择与默认 L0 不同的模型。")}</small>}
    {preset.isError && <small role="alert">{t("无法读取视觉核心预设。")}</small>}
    {toggle.isError && <small role="alert">{toggle.error.message}</small>}
  </>;
}

// 接收应用偏好与会话默认值，将模型配置、权限与外观提交到各自的拥有者。
export function Settings({
  preferences,
  zoom,
  initialTab,conversationId,workflowSettings,defaults,onDefaults,display,onDisplay,
  onChange,
  onClose,
  canLeave,
  onError,
  onPolicy,
  onOpenPlugins,
}: {
  preferences: Preferences;
  zoom: InterfaceZoom;
  workflowSettings: HTMLDivElement;
  initialTab:string;conversationId:string;defaults:ExecutionConfig;onDefaults:(v:ExecutionConfig)=>void;
  display:DisplayPreferences;onDisplay:(v:DisplayPreferences)=>void;
  onChange: (p: Preferences) => void;
  onClose: () => void;
  canLeave: RefObject<(() => boolean) | null>;
  onError: (e: unknown) => void;
  onPolicy: (policy: string) => void;
  onOpenPlugins: () => void;
}) {
  const { t, locale, preference, choose, saving: savingLanguage, error: languageError } = useUiLanguage();
  const pageName = (id: string) => { const entry = pages.find(row => row[0] === id); return entry ? t(entry[1]) : ""; };
  const [tab,setTab]=useState(initialTab);
  const [listOpen,setListOpen]=useState(initialTab==="appearance");
  const [search,setSearch]=useState("");
  const [jump,setJump]=useState<{item:string}>();
  const settingRoot=useRef<HTMLElement>(null);
  const searchField=useRef<HTMLInputElement>(null);
  const heading=useRef<HTMLHeadingElement>(null);
  const content=useRef<HTMLElement>(null);
  const mountWorkflow=useCallback((host:HTMLDivElement|null)=>{
    if(host)host.append(workflowSettings);
    else workflowSettings.remove();
  },[workflowSettings]);
  const words=search.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const score=(name:string,aliases:string)=>{
    if(words.every(word=>name.toLocaleLowerCase().includes(word)))return 0;
    if(words.every(word=>(name+" "+aliases).toLocaleLowerCase().includes(word)))return 1;
    return 2;
  };
  const matches=options.filter(([page,,name,aliases])=>words.every(word=>
    (t(name)+" "+name+" "+aliases+" "+page+" "+pageName(page)).toLocaleLowerCase().includes(word)))
    .sort((a,b)=>score(t(a[2]),a[3])-score(t(b[2]),b[3]));
  const [advanced,setAdvanced]=useState(false);
  const [connectionError,setConnectionError]=useState("");
  const policy = useQuery({
    queryKey: ["policy-v2"],
    queryFn: () => api(workbenchBootstrapPath),
  });
  const permissionChoice = policy.isError ? undefined
    : permissionOptions.find(([mode]) => mode === policy.data?.permissions?.mode);
  async function changePolicy(value: string) {
    const current = policy.data?.permissions;
    if (!current || (current.mode === value && current.sandboxMode !== "read-only")) return;
    if (value === "full" && !window.confirm(t("为新会话默认开启完全访问权限？新会话的终端将能访问本机文件和互联网。"))) return;
    try {
      const result = await api("/api/v2/permissions", {mode:value, expectedRevision:current.revision, confirm:value === "full"});
      onPolicy(result.mode);
      setNote("仅用于之后新建的会话");
    } catch (error) {
      onError(error);
    } finally {
      await policy.refetch();
    }
  }
  const state = useQuery({
    queryKey: ["models"],
    queryFn: () => api("/api/models"),
  });
  const [name, setName] = useState(""),
    [url, setURL] = useState(""),
    [model, setModel] = useState(""),
    [key, setKey] = useState(""),
    [note, setNote] = useState<UiMessage | "">("");
  const [importCount, setImportCount] = useState(0);
  const [editId, setEditId] = useState("");
  const [editRevision, setEditRevision] = useState<string | undefined>();
  const [modelBusy, setModelBusy] = useState(false);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [providerChoice,setProviderChoice]=useState('');
  const providers=state.data?.providers||[];
  const providerId=providerChoice||providers.find((p:any)=>p.base_url&&[p.base_url,...(p.aliases||[])].includes(url.replace(/\/$/,'')))?.id||(editId||url?'custom':'pick');
  const selectedProvider=providers.find((p:any)=>p.id===providerId);
  const blockedProvider=selectedProvider?.runtime_supported===false;
  const [discovery,setDiscovery]=useState<{list_url:string;fetched_at:number;provider?:{name:string;docs_url:string};availability:string;omitted:number;has_more?:boolean}>();
  useEffect(()=>{setAvailableModels([]);setDiscovery(undefined);},[url,key,editId]);
  // 按原档案与配置修订开始编辑。密钥和地址的输入草稿会按切换规则重置。
  function editProfile(p:any){
    if((name||url||model||key)&&(editId!==p.id||name!==p.name||model!==p.model||url||key)&&!confirm(t("切换编辑将放弃本表单尚未保存的字段，继续？")))return false;
    setProviderChoice(providers.find((v:any)=>v.base_url&&[v.base_url,...(v.aliases||[])].includes(p.base_url))?.id||'custom');
    setEditId(p.id);setEditRevision(state.data?.revision);setName(p.name);setModel(p.model);setURL("");setKey("");setAvailableModels([]);setDiscovery(undefined);setAdvanced(false);setConnectionError('');return true;
  }
  // 将模型操作绑定到已读取修订，按新增、更新或探测结果刷新配置及界面反馈。
  async function write(body: object) {
    if (modelBusy) return;
    setModelBusy(true);
    try {
      const result = await api("/api/models", {
        ...body,
        expectedRevision:
          (body as any).act === "save" && editId
            ? editRevision
            : state.data?.revision,
      }, (body as any).act === "probe");
      if (result.ok === false) throw Error(result.error || result.why || "模型配置未保存");
      const action = (body as {act:string}).act;
      if (action === "import_env") setImportCount(result.ids?.length ?? 0);
      setNote(action === "probe" ? "已读取模型列表，未更改配置" : action === "save" ? "模型连接已保存"
        : action === "select" ? ((body as {role?: string}).role === "vision" ? "视觉核心模型已更新" : "默认模型已更新")
        : action === "del" ? "模型连接已删除" : "已导入 {count} 份模型配置");
      if ((body as any).act === "save") {
        setKey("");
        setEditId("");
        setEditRevision(undefined);
        setName("");
        setURL("");
        setModel("");
        setProviderChoice('');
      }
      if (Array.isArray(result.models)){
        setConnectionError("");
        setAvailableModels(
          result.models
            .map((m: any) => (typeof m === "string" ? m : m.id))
            .filter(Boolean),
        );
        setDiscovery(result);
        if(!model&&result.models.length===1)setModel(typeof result.models[0]==='string'?result.models[0]:result.models[0].id);
      }
      await state.refetch({ throwOnError: true });
    } catch (e) {
      if((body as any).act==='probe'){setAvailableModels([]);setDiscovery(undefined);}
      const message=e instanceof Error?e.message:String(e);
      if((body as any).act==='probe'){
        setConnectionError(message);
        setNote(message.includes('401')?'连接失败（HTTP 401），请检查 Key 和账户。':message.includes('404')?'此服务未提供模型目录，可手动填写模型。':'连接未完成，请检查服务与 Key。');
      }else{setNote("");onError(e);}
    } finally {
      setModelBusy(false);
    }
  }
  const back = useRef<HTMLButtonElement>(null);
  useEffect(() => { back.current?.focus(); }, []);
  useLayoutEffect(()=>{
    if(!jump)return;
    const target=settingRoot.current?.querySelector<HTMLElement>('[data-setting-item="'+jump.item+'"]');
    if(!target)return;
    if(target instanceof HTMLDetailsElement)target.open=true;
    const focus=target.querySelector<HTMLElement>("input:enabled,select:enabled,button:enabled,summary,[tabindex='-1']")||heading.current;
    focus?.focus({preventScroll:true});
    target.scrollIntoView({block:"nearest"});
  },[jump]);
  const saved = state.data?.profiles?.find((p:any) => p.id === editId);
  const dirty = !!(url || key || (editId ? name !== saved?.name || model !== saved?.model : name || model));
  useLayoutEffect(() => {
    canLeave.current = () => !modelBusy && (!dirty || confirm(t("放弃尚未保存的模型连接修改？")));
    return () => { canLeave.current = null; };
  }, [canLeave, dirty, modelBusy, t]);
  function leave(action: () => void) {
    if (canLeave.current?.()) action();
  }
  const chooseProvider=(id:string)=>{
    const p=providers.find((p:any)=>p.id===id);setProviderChoice(id);
    setURL(p?.base_url||'');setKey(p?.local?'local-not-required':'');setModel('');
    setNote('');setConnectionError('');setAdvanced(id==='custom'||!!p?.requires_endpoint);
  };
  function go(page:string,item?:string){
    setTab(page);setListOpen(false);setSearch("");setNote("");setJump(item?{item}:undefined);
    if(!item)requestAnimationFrame(()=>{content.current?.scrollTo(0,0);heading.current?.focus({preventScroll:true});});
  }
  const highlight=(id:string)=>jump?.item===id||undefined;
  return <section ref={settingRoot} className="settings-dialog focused-settings" data-list-open={listOpen} aria-label={t("应用设置")} onKeyDown={e=>{
    if(e.key==='Escape'&&!e.defaultPrevented&&!e.nativeEvent.isComposing){
      e.stopPropagation();
      if(search&&e.target===searchField.current){e.preventDefault();setSearch("");}else onClose();
    }
  }}>
    <header className="tool-title"><button ref={back} disabled={modelBusy} onClick={onClose}>{t("返回")}</button><h3>{t("设置")}</h3></header>
    <aside className="settings-navigation">
      <label className="settings-search"><UiIcon name="search"/><input ref={searchField} type="search" aria-label={t("搜索设置")} placeholder={t("搜索设置")} value={search} onChange={e=>setSearch(e.target.value)}
        onKeyDown={e=>{if(e.key==="Enter"&&!e.nativeEvent.isComposing&&words.length&&matches.length){e.preventDefault();go(matches[0][0],matches[0][1]);}}}/></label>
      <nav className="settings-tabs" aria-label={t("设置分类")} hidden={!!words.length}>
        {groups.map(([group,items])=><section key={group}><h4>{group === "Agent" ? group : t(group)}</h4>{items.map(([id,label,icon])=>
          <button key={id} aria-current={tab===id?"page":undefined} aria-controls={'settings-'+id} id={'tab-'+id} onClick={()=>go(id)}><UiIcon name={icon}/>{t(label)}</button>)}</section>)}
      </nav>
      {!!words.length&&<div className="settings-search-results">
        <p role="status">{matches.length?t("匹配项：{count}", {count:matches.length}):t("没有匹配的设置。")}</p>
        {matches.map(([page,item,name])=><button key={item} onClick={()=>go(page,item)}>{t(name)}<small>{pageName(page)}</small></button>)}
      </div>}
    </aside>
    <main ref={content} className="settings-content">
    <button className="settings-categories-back" onClick={()=>{setListOpen(true);requestAnimationFrame(()=>searchField.current?.focus({preventScroll:true}));}}><UiIcon name="back"/>{t("设置分类")}</button>
    <h2 ref={heading} tabIndex={-1}>{pageName(tab)}</h2>
    <section id="settings-general" hidden={tab!=='general'}>
      <div className="settings-card" data-setting-item="language" data-highlight={highlight("language")}>
        <label className="settings-line">{t("界面语言")}<select id="display-language" value={preference} disabled={savingLanguage} onChange={e=>void choose(e.currentTarget.value as LanguageChoice)}>
          <option value="system">{t("跟随系统")}</option><option value="zh-CN" lang="zh-CN">简体中文</option><option value="en" lang="en">English</option>
        </select></label>
        {languageError && <p role="alert">{t("无法读取或保存语言设置。")} <button type="button" onClick={()=>void choose(preference)}>{t("重试")}</button></p>}
      </div>
      <div className="settings-card" data-setting-item="close-window" data-highlight={highlight('close-window')}>{tab==='general'&&<WindowCloseSetting/>}</div>
      <div className="settings-card" data-setting-item="work-folders" data-highlight={highlight('work-folders')}>{tab==='general'&&<WorkFolderSettings/>}</div>
      <div className="settings-card" data-setting-item="general" data-highlight={highlight("general")}>
        <h3>{t("阅读与导航")}</h3>
        <label className="simple-switch">{t("紧凑导航")}<input type="checkbox" role="switch" checked={preferences.compact} onChange={e=>onChange({...preferences,compact:e.target.checked})}/></label>
      </div>
    </section>
    <section id="settings-conversation" role="tabpanel" aria-labelledby="tab-conversation" hidden={tab!=='conversation'}>
      <div className="settings-card" data-setting-item="execution" data-highlight={highlight("execution")}>{tab==='conversation'&&<ConversationSettings key={conversationId} id={conversationId} defaults={defaults} onDefaults={onDefaults} display={display} onDisplay={onDisplay}/>}</div>
    </section>
    <section id="settings-resources" role="tabpanel" aria-labelledby="tab-resources" hidden={tab!=='resources'}>
      {tab==='resources'&&(conversationId?<div ref={mountWorkflow}/>
        : <p className="settings-intro">{t("打开一条会话后可管理它的资料与任务。")}</p>)}
    </section>
    <section id="settings-appearance" role="tabpanel" aria-labelledby="tab-appearance" hidden={tab!=='appearance'}>
      <p className="settings-intro">{t("统一调整对话、设置、代码审查和终端的外观。")}</p>
      <div className="settings-card">
        <label className="settings-line" data-setting-item="theme" data-highlight={highlight("theme")}>{t("主题")}<select aria-label={t("主题")} value={preferences.theme} onChange={e=>onChange({...preferences,theme:e.target.value as Preferences["theme"]})}><option value="light">{t("浅色")}</option><option value="dark">{t("深色")}</option><option value="system">{t("跟随系统")}</option><option value="contrast">{t("高对比度")}</option></select></label>
        <InterfaceZoomControl zoom={zoom} className="settings-line" settingItem="zoom" highlighted={highlight("zoom")}/>
        <label className="settings-line" data-setting-item="font" data-highlight={highlight("font")}>{t("界面字号")}<span><input type="range" aria-label={t("字号")} min={11} max={22} step={1} value={preferences.fontSize} onChange={e=>onChange({...preferences,fontSize:Number(e.target.value)})}/><output>{preferences.fontSize} px</output></span></label>
        <label className="settings-line" data-setting-item="code" data-highlight={highlight("code")}>{t("代码字号")}<span><input type="range" aria-label={t("代码字号")} min={11} max={22} step={1} value={preferences.codeSize??12} onChange={e=>onChange({...preferences,codeSize:Number(e.target.value)})}/><output>{preferences.codeSize??12} px</output></span></label>
        <div className="settings-line"><code className="settings-code-sample">+ return round(total, 2)</code>
          <button disabled={zoom.pending!==undefined} onClick={()=>{
            onChange({...preferences,theme:"system",fontSize:14,codeSize:12});
            if(zoom.setting && zoom.setting.percent!==zoom.setting.defaultPercent) zoom.change(zoom.setting.defaultPercent);
          }}>{t("恢复默认外观")}</button>
        </div>
      </div>
    </section>
    <section id="settings-shortcuts" hidden={tab!=='shortcuts'}>
      <div className="settings-card" data-setting-item="shortcuts" data-highlight={highlight("shortcuts")}>{Object.keys(DEFAULT_SHORTCUTS).map(name=><label className="settings-row" key={name}>{t(({files:'文件',commands:'命令',terminal:'终端',send:'发送',save:'保存',previous:'前个会话',next:'下个会话'} as Record<string,UiMessage>)[name])}
        <input readOnly value={bound(name as ShortcutName,preferences.shortcuts)} onKeyDown={e=>{if(e.key==='Tab'||e.key==='Escape'||(!e.ctrlKey&&!e.altKey&&!e.metaKey))return;e.preventDefault();const value=chord(e.nativeEvent);if(!value)return;if(Object.keys(DEFAULT_SHORTCUTS).some(other=>other!==name&&bound(other as ShortcutName,preferences.shortcuts)===value)){onError(Error('此组合已分配给其他命令'));return;}onChange({...preferences,shortcuts:{...preferences.shortcuts,[name]:value}});}}/>
      </label>)}<button onClick={()=>onChange({...preferences,shortcuts:{}})}>{t("恢复默认快捷键")}</button></div>
    </section>
    <section id="settings-storage" hidden={tab!=='storage'}>
      <p className="settings-intro">{t("查看和管理此设备上的 Modot 数据。")}</p>
      {tab==='storage'&&<LocalStorageSettings onWorktrees={()=>go('worktrees','worktree')} focusItem={jump?.item}/>}
    </section>
    <section id="settings-worktrees" hidden={tab!=='worktrees'}>
      <p className="settings-intro">查看托管工作树。初始化脚本、命令和切换从项目会话右侧总览打开。</p>
      {tab==='worktrees'&&<LocalStorageSettings worktreesOnly onWorktrees={()=>go('storage','storage')} focusItem={jump?.item}/>}
    </section>
    <section id="settings-extensions" hidden={tab!=='extensions'} data-setting-item="extensions" data-highlight={highlight("extensions")}>
      {tab==='extensions'&&<button onClick={onOpenPlugins}>{t("打开插件管理")}</button>}
    </section>
    <section id="settings-permissions" role="tabpanel" aria-labelledby="tab-permissions" hidden={tab!=='permissions'}>
      <label className="settings-line settings-card" data-setting-item="permission" data-highlight={highlight("permission")}>
        {t("默认权限")}
        <select aria-label={t("默认权限")} value={policy.data?.permissions?.sandboxMode === "read-only" ? "read-only" : permissionChoice?.[0] ?? ""} disabled={!permissionChoice}
          onChange={e=>changePolicy(e.target.value)}>
          {!permissionChoice&&<option value="">{policy.isFetching?t("正在读取权限…"):t("权限读取失败")}</option>}
          {policy.data?.permissions?.sandboxMode === "read-only" && <option value="read-only" disabled>{t("只读")}</option>}
          {permissionOptions.map(([value,label])=><option key={value} value={value}>{t(label)}</option>)}
        </select>
      </label>
      <small>{t("仅用于之后新建的会话")}</small>
      {!permissionChoice&&!policy.isFetching&&<button type="button" onClick={()=>void policy.refetch()}>{t("重试读取权限")}</button>}
      <PermissionReview/>
    </section>
    <section id="settings-models" role="tabpanel" aria-labelledby="tab-models" hidden={tab!=='models'}>
      <fieldset disabled={modelBusy}>
        {state.data?.error&&<div role="alert">{t("模型配置暂不可读取。")}<details><summary>{t("查看原因")}</summary>{state.data.error}</details></div>}
        {!!state.data?.profiles?.length&&<label>{t("L0 默认模型")}<select aria-label={t("L0 默认模型")} value={state.data?.main||''} onChange={e=>write({act:'select',role:'main',id:e.target.value})}><option value="">{t("环境配置")}</option>{state.data.profiles.map((p:any)=><option key={p.id} value={p.id}>{p.name}</option>)}</select><small>{t("之后所有会话的新运行都用它，不改变已有记录")}</small></label>}
        <div className="settings-card" data-setting-item="vision-core" data-highlight={highlight("vision-core")}>
          <h3>{t("任务核心预设")}</h3>
          <label>{t("视觉核心模型")}
            <select aria-label={t("视觉核心模型")} value={state.data?.vision || ""}
              disabled={!state.isSuccess} onChange={event => void write({act: "select", role: "vision", id: event.currentTarget.value})}>
              <option value="">{t("使用环境配置或保持关闭")}</option>
              {(state.data?.profiles ?? []).filter((profile: {has_key: boolean}) => profile.has_key)
                .map((profile: {id: string; name: string; model: string; provider_name?: string}) =>
                  <option key={profile.id} value={profile.id}>
                    {[profile.provider_name, profile.name, profile.model].filter(Boolean).join(" · ")}
                  </option>)}
            </select>
            <small>{t("视觉核心的选图、复核及独立精确读图都使用这里选定的模型。可先连接其他供应商，再返回选择。")}</small>
          </label>
          {tab === "models" && <VisionCoreSwitch modelRevision={state.data?.revision}/>}
        </div>
        <h4>{editId?t("修改连接"):t("连接模型")}</h4>
        <label>{t("供应商")}<select aria-label={t("模型供应商")} value={providerId} onChange={e=>chooseProvider(e.target.value)}><option value="pick" disabled>{t("选择供应商")}</option><option value="custom">{t("自定义兼容端点")}</option>
          {Array.from(new Set(providers.map((p:any)=>p.group||'其他服务'))).map(group=><optgroup key={String(group)} label={typeof group === 'string' && isUiMessage(group) ? t(group) : String(group)}>{providers.filter((p:any)=>(p.group||'其他服务')===group).map((p:any)=><option key={p.id} value={p.id}>{isUiMessage(p.name) ? t(p.name) : p.name}</option>)}</optgroup>)}
        </select></label>
        <label data-setting-item="key" data-highlight={highlight("key")}>API Key<input autoComplete="off" type="password" placeholder={editId?t("留空沿用已保存的 Key"):t("粘贴 API Key")} value={key} disabled={providerId==='pick'||blockedProvider} onChange={e=>setKey(e.target.value)}/></label>
        <button className="primary" disabled={blockedProvider||(!editId&&(!url||!key))} onClick={()=>write({act:'probe',base_url:url,api_key:key,id:editId||undefined})}>{modelBusy?t("连接中…"):t("连接并获取模型")}</button>
        <label>{t("模型")}<select id="settings-model-select" aria-label={t("可用模型")} disabled={!availableModels.length} value={availableModels.includes(model)?model:''} onChange={e=>setModel(e.target.value)}><option value="">{availableModels.length?t("选择模型"):t("连接后自动读取")}</option>{availableModels.map(m=><option key={m} value={m}>{m}</option>)}</select></label>
        {discovery&&model&&!availableModels.includes(model)&&<small>{t("原模型已保留，可重新选择。")}</small>}
        <button disabled={blockedProvider||!model||(!editId&&(!url||!key))} onClick={()=>write({act:'save',id:editId,name:name.trim()||model,base_url:url,model,api_key:key})}>{t("保存模型")}</button>
        {editId&&<button onClick={()=>{if(confirm(t('取消未保存的修改？'))){setEditId('');setEditRevision(undefined);setName('');setURL('');setModel('');setKey('');setProviderChoice('');setAdvanced(false);}}}>{t("取消编辑")}</button>}
        {blockedProvider&&<p role="status">{t("这个接口暂未适配，请选择其他供应商。")}</p>}
        <details className="model-advanced" open={advanced} onToggle={e=>setAdvanced(e.currentTarget.open)}><summary>{t("高级连接选项")}</summary><div className="settings-fields">
          <input autoComplete="off" placeholder={t("档案名称")} aria-label={t("显示名称（可选）")} value={name} onChange={e=>setName(e.target.value)}/>
          <input autoComplete="off" placeholder={t("API 地址")} value={url} onChange={e=>{setURL(e.target.value);if(!selectedProvider?.requires_endpoint)setProviderChoice('custom');}}/>
          <input autoComplete="off" placeholder={t("模型名称")} list="settings-model-list" value={model} onChange={e=>{setModel(e.target.value);}}/>
          <small>{t("自定义地址或未提供模型目录时使用。")}{selectedProvider?.requires_endpoint&&selectedProvider.endpoint_hint}</small>
        </div></details>
        <datalist id="settings-model-list">{availableModels.map(m=><option key={m} value={m}/>)}</datalist>
        <details><summary>{t("已保存连接")}</summary>
          <button onClick={()=>{if(confirm(t('导入本机已有模型配置？')))write({act:'import_env'});}}>{t("导入已有环境配置")}</button>
          {(state.data?.profiles||[]).map((p:any)=><div className="saved-model" key={p.id}><strong>{p.name}</strong><small>{p.model}</small><div>
            <button onClick={()=>editProfile(p)}>{t("编辑档案")}</button><button onClick={()=>{if(editProfile(p))void write({act:'probe',id:p.id});}}>{t("读取该端点模型列表")}</button>
            <button onClick={()=>{if(confirm(t('删除此连接？')))write({act:'del',id:p.id});}}>{t("删除档案")}</button></div></div>)}
          <button onClick={()=>state.refetch()}>{t("刷新模型配置")}</button>
        </details>
        {selectedProvider&&<details aria-label={t("供应商接入说明")}><summary>{t("服务信息")}</summary><p>{selectedProvider.note||t("模型以接口返回为准。")}</p><a href={selectedProvider.docs_url} target="_blank" rel="noreferrer">{t("官方接入说明")}</a></details>}
        {discovery&&<details aria-label={t("模型列表来源")}><summary>{t("模型来源 · {count} 项",{count:availableModels.length})}</summary><p>{discovery.list_url} · {new Date(discovery.fetched_at*1000).toLocaleString(locale)}</p>
          {discovery.provider?<a href={discovery.provider.docs_url} target="_blank" rel="noreferrer">{t("官方模型文档")}</a>:<p>{t("自定义网关：尚未核实官方网页。")}</p>}<p>{isUiMessage(discovery.availability) ? t(discovery.availability) : discovery.availability}{discovery.omitted ? ' '+t('{count} 项未展示。',{count:discovery.omitted}) : ''}{discovery.has_more ? ' '+t('还有后续目录页。') : ''}</p>
        </details>}
        {connectionError&&<details><summary>{t("连接详情")}</summary><p>{connectionError}</p></details>}
      </fieldset>
    </section>
    {note&&<p className="settings-note" role="status">{t(note,{count:importCount})}</p>}
    </main>
  </section>;
}
