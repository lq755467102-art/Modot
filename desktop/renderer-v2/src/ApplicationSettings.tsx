// 持有应用级偏好与设置弹窗，负责保存显示配置、页面定位及焦点归还。
// 主要引用方包括 `App.tsx` 和 `AgentHome.tsx`。

import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { loadJSON, saveJSON } from "./client";
import { Settings, type Preferences, type Theme } from "./Settings";
import {defaultExecution} from "./ExecutionControl";
import {executorConfig} from "./executorEntry";
import type {ExecutionConfig} from "./generated";
import {useInterfaceZoom,type InterfaceZoom} from "./InterfaceZoom";

// 应用壳共用的 React 上下文。
// `theme` 是已解析的主题，`newExecution` 是新会话的默认执行草稿，`closeSettings` 用布尔值报告是否通过离开检查。
type SharedSettings = {
  display: {output:string;think:string};
  reducedMotion: boolean;
  setDisplay: (value:{output:string;think:string})=>void;
  preferences: Preferences;
  zoom: InterfaceZoom;
  theme: Theme;
  openSettings: () => void;
  openConversationSettings: () => void;
  openSettingsAt:(tab:string)=>void;
  closeSettings:(restore?:boolean)=>boolean;
  settingsVisible:boolean;
  workflowSettings:HTMLDivElement;
  catalogRequest:number;
  settingsPage: ReactNode;
  newExecution:ExecutionConfig;
  setNewExecution:(value:ExecutionConfig)=>void;
  defaultPolicy: string;
  setDefaultPolicy: (value: string) => void;
};
const Context = createContext<SharedSettings | null>(null);
function oldLook():Preferences{try{const value=JSON.parse(localStorage.getItem('harness.look.v1')||'{}');return {
  fontSize:typeof value.fs==='number'&&value.fs>=11&&value.fs<=22?value.fs:14,
  theme:['light','dark','contrast'].includes(value.theme)?value.theme:'system',compact:value.density==='compact'};}catch{return {fontSize:14,theme:'system',compact:false};}}
function oldDisplay(){try{const value=JSON.parse(localStorage.getItem('harness.disp')||'{}');return {output:['type','block'].includes(value.output)?value.output:'type',think:['collapse','expand','hide'].includes(value.think)?value.think:'collapse'};}catch{return {output:'type',think:'collapse'};}}


// 为 `App.tsx` 提供设置弹窗，设置页面的离开条件与打开位置由同一控制器持有。
export function ApplicationSettings({ children,conversationId }: { children: ReactNode;conversationId:string }) {
  const [newExecution,setNewExecution]=useState<ExecutionConfig>(() => {
    const previous = loadJSON('new-conversation-execution', defaultExecution);
    return executorConfig(loadJSON('new-conversation-execution-v2', previous));
  });
  useEffect(()=>saveJSON('new-conversation-execution-v2',newExecution),[newExecution]);
  const [initialTab,setInitialTab]=useState('models');
  const [settingsVisit,setSettingsVisit]=useState(0);
  const [workflowSettings]=useState(()=>document.createElement("div"));
  const [preferences, setPreferences] = useState<Preferences>(() => {
    const defaults=oldLook();
    const saved=loadJSON<Partial<Preferences>|null>("preferences",null);
    const theme=saved?.theme;
    return {...defaults,...saved,theme:theme&&["light","dark","contrast","system"].includes(theme)?theme:defaults.theme};
  });
  const [systemDark,setSystemDark]=useState(()=>matchMedia("(prefers-color-scheme: dark)").matches);
  const theme=preferences.theme==="system"?(systemDark?"dark":"light"):preferences.theme;
  useEffect(()=>{
    const media=matchMedia("(prefers-color-scheme: dark)");
    const update=()=>setSystemDark(media.matches);
    media.addEventListener("change",update);
    update();
    return()=>media.removeEventListener("change",update);
  },[]);
  const [display,setDisplay]=useState(()=>loadJSON("legacy-display",oldDisplay()));
  const [reducedMotion, setReducedMotion] = useState(() => matchMedia("(prefers-reduced-motion: reduce)").matches);
  useEffect(() => {
    const media = matchMedia("(prefers-reduced-motion: reduce)");
    const change = () => setReducedMotion(media.matches);
    change();
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  useEffect(()=>saveJSON("legacy-display",display),[display]);
  const [open, setOpen] = useState(false);
  const [catalogRequest,setCatalogRequest] = useState(0);
  const opener = useRef<HTMLElement | null>(null);
  const canLeave = useRef<(() => boolean) | null>(null);
  const restoreFocus = useRef(false);
  const [error, setError] = useState("");
  const [defaultPolicy, setDefaultPolicy] = useState("ask");
  const query = useQueryClient();
  const zoom = useInterfaceZoom();
  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.setProperty("--app-code-size",(preferences.codeSize??12)+"px");
  }, [theme,preferences.codeSize]);
  useEffect(() => saveJSON("preferences", preferences), [preferences]);
  useLayoutEffect(()=>{
    if(!open&&restoreFocus.current){restoreFocus.current=false;if(opener.current?.isConnected)opener.current.focus({preventScroll:true});}
  },[open]);
  // 先调用当前设置页登记的离开检查，再按触发方要求恢复焦点。
  function close(restore = true) {
    if (!open) return true;
    if (canLeave.current && !canLeave.current()) return false;
    restoreFocus.current=restore;
    setOpen(false);
    void query.invalidateQueries({ queryKey: ["models"] });
    void query.invalidateQueries({ queryKey: ["policy-v2"] });
    return true;
  }
  // 设置内的跳转沿用离开检查，关闭时焦点仍归还给最初的入口。
  function openAt(tab:string) {
    if(open&&canLeave.current&&!canLeave.current())return;
    if(!open)opener.current=document.activeElement as HTMLElement;
    setInitialTab(tab);setSettingsVisit(n=>n+1);setError("");setOpen(true);
  }
  const settingsPage = open && <div className={"settings-page legacy-conversation theme-" + theme} data-theme={theme} style={{ fontSize: preferences.fontSize }}>
      <Settings key={settingsVisit} initialTab={initialTab} conversationId={conversationId} workflowSettings={workflowSettings} defaults={newExecution} onDefaults={setNewExecution} display={display} onDisplay={setDisplay} preferences={preferences} zoom={zoom} onChange={setPreferences} onClose={()=>close()} canLeave={canLeave}
        onOpenPlugins={()=>{if(close(false))setCatalogRequest(n=>n+1);}}
        onError={e => setError(e instanceof Error ? e.message : String(e))}
        onPolicy={setDefaultPolicy} />
      {error && <div className="shared-settings-error" role="alert">{error}<button onClick={() => setError("")}>关闭提示</button></div>}
    </div>;
  return <Context.Provider value={{ display,reducedMotion,setDisplay,preferences,zoom,theme,newExecution,setNewExecution, openSettings: () => openAt('models'),openConversationSettings:()=>openAt('conversation'),openSettingsAt:openAt,closeSettings:close,settingsVisible:open,workflowSettings,settingsPage,catalogRequest, defaultPolicy, setDefaultPolicy }}>
    {children}
  </Context.Provider>;
}

// 在应用壳共享持久展示偏好与设置回调。
export function useApplicationSettings() {
  const value = useContext(Context);
  if (!value) throw Error("Application settings provider missing");
  return value;
}
