// 工作间侧栏挂载当前主会话的时间线、输入和批准组件。
// 由 `WorkroomPage.tsx` 按项目检出目录和主会话挂载。通过 `ConversationDock.tsx` 借用对话页的同一组节点。
import { useUiLanguage } from "./UiLanguage";
import {useCallback, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode} from 'react';
import {useApplicationSettings} from './ApplicationSettings';
import {loadJSON, saveJSON} from './client';
import {useRegisterConversationDock, type ConversationDock} from './ConversationDock';
import type {Conversation} from './generated';
import {usePermissionRequests} from './PermissionReview';
import {UiIcon} from './UiIcon';

// `workspace` 是工作间实际目录标识，`matched` 表示工作间与会话授权匹配，`legacyOpen` 表示原有右侧视图已占用该区域。
// `onOpen`、`chooseProject` 与 `attach` 由 `WorkroomPage.tsx` 提供，分别处理右栏展开、范围选择和当前文件引用。
type Props = {
  conversation?: Conversation; workspace: string; matched: boolean; visible: boolean; legacyOpen?: boolean;
  attach?: ()=>void; onOpen?: ()=>void; chooseProject?: ()=>void; openConversation: ()=>void;
  renderToggle: (control: ReactNode) => ReactNode;
  drawerId?: string; unbound?: ReactNode; showFileAttachment?: boolean;
  captureGitHubPage?: ConversationDock['captureGitHubPage'];
  navigateGitHubPage?: ConversationDock['navigateGitHubPage'];
};
// 按会话和工作间保存展开状态。没有保存偏好时，首次进入窄窗默认收起。
export function WorkroomConversation({conversation,workspace,matched,visible,legacyOpen,attach,onOpen,chooseProject,openConversation,
  renderToggle,drawerId='workroom-main-chat',unbound,showFileAttachment=true,captureGitHubPage,navigateGitHubPage}: Props) {
  const { t } = useUiLanguage();
  const owner=conversation?.id??'';
  const storage='workroom-main-chat:'+workspace+':'+owner;
  const [open,setOpen]=useState(()=>loadJSON(storage,!matchMedia('(max-width:900px)').matches));
  const expanded=visible&&open&&!legacyOpen;
  const {theme,preferences}=useApplicationSettings();
  const drawer=useRef<HTMLElement>(null);
  const feed=useRef<HTMLDivElement>(null);
  const composer=useRef<HTMLDivElement>(null), approval=useRef<HTMLDivElement>(null);
  const toggle=useRef<HTMLButtonElement>(null);
  const openConversationRef=useRef(openConversation);
  openConversationRef.current=openConversation;
  const capture=useRef(captureGitHubPage);
  capture.current=captureGitHubPage;
  const navigate=useRef(navigateGitHubPage);
  navigate.current=navigateGitHubPage;
  const register=useRegisterConversationDock();
  // 收起时保留原挂载位置，离开工作间或目录失配时交还给对话页。
  useLayoutEffect(()=>{
    if(!visible||!matched||!owner||!feed.current||!composer.current||!approval.current)return;
    const dock={
      owner,expanded,feed:feed.current,composer:composer.current,approval:approval.current,
      openConversation:()=>openConversationRef.current(),
      ...(capture.current?{captureGitHubPage:()=>capture.current?.()??Promise.resolve(null)}:{}),
      ...(navigate.current?{navigateGitHubPage:(id:string)=>navigate.current?.(id)??Promise.resolve()}:{}),
    };
    register(dock);
    // 旧容器卸载时只撤回自己的登记，避免覆盖刚切换进去的新会话容器。
    return ()=>register(current=>current===dock?null:current);
  },[visible,matched,owner,expanded,register]);
  const permissions=usePermissionRequests(false,owner);
  const runtime=conversation?.runtime;
  // `PermissionReview.tsx` 提供审批列表，收起状态只提示当前会话、当前运行的待批准请求。
  const pending=matched&&runtime?.active&&!permissions.error&&permissions.data?.requests.some(row=>
    row.sessionId===owner&&row.runId===runtime.run_id&&row.state==='pending');
  // 切换侧栏显示，并在展开后聚焦主输入框。收起后把键盘焦点交回工作间的会话按钮。
  const change=useCallback((show:boolean)=>{
    setOpen(show);saveJSON(storage,show);
    if(show) {
      onOpen?.();
      requestAnimationFrame(()=>composer.current?.querySelector('textarea')?.focus({preventScroll:true}));
    } else toggle.current?.focus();
  },[onOpen,storage]);
  useLayoutEffect(()=>{
    const node=drawer.current;
    if(!node||!expanded)return;
    // 时间线的 React 事件仍冒泡到对话页，用实际侧栏节点接收 Escape。
    function dismiss(event:KeyboardEvent) {
      if(event.key!=='Escape'||event.defaultPrevented||event.isComposing)return;
      event.stopPropagation();
      change(false);
    }
    node.addEventListener('keydown',dismiss);
    return ()=>node.removeEventListener('keydown',dismiss);
  },[expanded,change]);
  const label=pending?'待批准':runtime?.active?'执行中':'';
  return <>
    {visible&&renderToggle(<button ref={toggle} type="button" className="workroom-chat-toggle" aria-label="工作间会话"
      aria-controls={drawerId} aria-expanded={expanded} title={expanded?'收起当前会话':'展开当前会话'}
      onClick={()=>change(!expanded)}><UiIcon name="right-sidebar"/>{t("会话")}
      {!expanded&&label&&<small role="status">{label}</small>}</button>)}
    <aside ref={drawer} id={drawerId} className="workroom-chat-drawer" hidden={!expanded} aria-label="工作间会话侧栏">
      <header><div><small>当前主会话</small><strong title={conversation?.title}>{conversation?.title||t("会话")}</strong></div>
        {matched&&<button type="button" aria-label="在对话页打开" title="在对话页打开" onClick={openConversation}><UiIcon name="open"/></button>}
        <button type="button" aria-label="收起工作间会话" title="收起工作间会话" onClick={()=>change(false)}><UiIcon name="right-sidebar"/></button></header>
      <div ref={feed} className={`workroom-conversation-feed legacy-conversation restored-legacy theme-${theme}`}
        data-theme={theme} data-density={preferences.compact?'compact':'normal'} hidden={!matched}
        style={{'--fs':preferences.fontSize+'px'} as CSSProperties}/>
      {!matched&&
        <div className="workroom-chat-unbound">{unbound??<><p>{owner?t('当前会话没有使用这个目录。'):'选择一个主会话后可在这里发送指令。'}</p>
          {owner&&chooseProject&&<button type="button" onClick={chooseProject}>{t('返回会话工作目录')}</button>}</>}</div>}
      <div className="workroom-approval-host" ref={approval}/>
      <div className="workroom-command-box" hidden={!matched} aria-label="工作间主会话输入栏">
        <div className="workroom-input-host" ref={composer}/>
        {showFileAttachment&&<button type="button" className="workroom-attach" disabled={!attach} aria-label="附上当前文件或选区"
          title="附上当前文件或选区" onClick={attach}><UiIcon name="paperclip"/></button>}
      </div>
    </aside>
  </>;
}
