// 提供项目工作间的文件树、源码区和运行区域布局。
// 主要引用方包括 `WorkroomPage.tsx`。
import { useUiLanguage } from "./UiLanguage";
import {createContext,useContext,useEffect,useRef,useState,type ReactNode} from 'react';
import {createPortal} from 'react-dom';
import {UiIcon} from './UiIcon';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {api,loadJSON,saveJSON} from './client';
import type {ConversationScope,WorkroomContext,WorkroomOpened,Project} from './generated';
import type { WorkroomFileRequest } from "./WorkroomChangeLink";
import { FormattingScope } from './formattingScope';
import './project-workroom.css';

const ToolbarTarget=createContext<HTMLElement|null>(null);
export function WorkroomToolbar({children}:{children:ReactNode}){
  const target=useContext(ToolbarTarget);return target?createPortal(children,target):null;
}

// 解析当前工作间的检出目录，并保存各项目最近选择的代码根。
export function ProjectWorkroom({conversationId,visible,children,projectRequest=0,fileRequest,onClearFileRequest}:{
  conversationId:string;visible:boolean;
  children:(opened:WorkroomOpened,context:WorkroomContext,followConversation:()=>void)=>ReactNode;
  projectRequest?:number;
  fileRequest?:WorkroomFileRequest;
  onClearFileRequest:()=>void;
}){
  const { t } = useUiLanguage();
  const queryClient = useQueryClient();
  const [selection,setSelection]=useState(()=>loadJSON<string>('workroom-selection','follow'));
  const [roots,setRoots]=useState(()=>loadJSON<Record<string,string>>('workroom-project-roots',{}));
  const [opened,setOpened]=useState<{key:string;value:WorkroomOpened}>();
  const [error,setError]=useState('');
  const [retry,setRetry]=useState(0);
  const [toolbar,setToolbar]=useState<HTMLDivElement|null>(null);
  const picker=useRef<HTMLDetailsElement>(null);
  const projectHandled=useRef(0);
  useEffect(()=>{if(visible&&projectRequest>projectHandled.current){projectHandled.current=projectRequest;if(picker.current){picker.current.open=true;picker.current.querySelector('select')?.focus();}}},[visible,projectRequest]);
  useEffect(()=>{if(!visible)picker.current?.removeAttribute('open');},[visible]);
  useEffect(()=>{const outside=(event:PointerEvent)=>{if(!picker.current?.contains(event.target as Node))picker.current?.removeAttribute('open');};document.addEventListener('pointerdown',outside);return()=>document.removeEventListener('pointerdown',outside);},[]);
  useEffect(()=>saveJSON('workroom-selection',selection),[selection]);
  const source:Record<string,string>=fileRequest?{conversationId:fileRequest.conversationId}:
    selection==='follow'?(conversationId?{conversationId}:{}):{projectId:selection};
  const sourceKey=JSON.stringify(source);
  const projects=useQuery<{projects:Project[]}>({queryKey:['workroom-projects'],queryFn:()=>api('/api/v2/projects'),enabled:visible});
  const view=useQuery<WorkroomContext>({queryKey:['workroom-context',sourceKey,fileRequest?.scopeRevision],
    queryFn:()=>api('/api/v2/workroom?'+new URLSearchParams(source).toString()),
    enabled:visible,refetchInterval:visible?2500:false});
  const context=view.data;
  const chatSource={conversationId};
  const pinned=visible && !!source.projectId && !!conversationId;
  const chatView=useQuery<WorkroomContext>({
    queryKey:['workroom-context',JSON.stringify(chatSource),undefined],
    queryFn:()=>api('/api/v2/workroom?'+new URLSearchParams(chatSource).toString()),
    enabled:pinned,refetchInterval:pinned?2500:false,
  });
  const liveChat=source.conversationId ? context : pinned&&!chatView.error ? chatView.data : undefined;
  useEffect(()=>{
    if(!visible || !liveChat?.conversationId) return;
    const queryKey=["scope",liveChat.conversationId];
    const cached=queryClient.getQueryData<ConversationScope>(queryKey);
    if(cached && cached.revision!==liveChat.scopeRevision)
      void queryClient.invalidateQueries({queryKey,exact:true});
  },[visible,liveChat?.conversationId,liveChat?.scopeRevision,queryClient]);
  const rootPreferenceKey=sourceKey+":"+JSON.stringify(context?.importLocation??null);
  const fileScopeMatches = !fileRequest?.scopeRevision || fileRequest.scopeRevision === context?.scopeRevision;
  const selectedRoot=fileRequest ? (fileScopeMatches && context?.conversationId===fileRequest.conversationId ? context.roots.find(r=>r.resourceId===fileRequest.resourceId) : undefined)
    : context?.roots.find(r=>r.originResourceId===roots[rootPreferenceKey]) ||
      context?.roots.find(r=>r.resourceId===context.importLocation?.resourceId) ||
      context?.roots.find(r=>r.originResourceId===roots[context.project?.id||'']) || context?.roots[0];
  const chatRoot=pinned && liveChat?.status==='ready' && liveChat.conversationId===conversationId && liveChat.project?.id===source.projectId
    ? liveChat.roots.find(item=>item.originResourceId===selectedRoot?.originResourceId) : undefined;
  const root=chatRoot??selectedRoot;
  const bindingContext=chatRoot?liveChat:context;
  const bindingSource=chatRoot?chatSource:source;
  const locationUnavailable=!!fileRequest && !!context && !root;
  const key=JSON.stringify(bindingSource)+':'+(bindingContext?.scopeRevision||'')+':'+(root?.resourceId||'');
  useEffect(()=>{
    let live=true;
    if(!visible||!bindingContext||bindingContext.status!=='ready'||!root||opened?.key===key&&!error)return;
    setError('');
    api<WorkroomOpened>('/api/v2/workroom/open',{...bindingSource,resourceId:root.resourceId,scopeRevision:bindingContext.scopeRevision})
      .then(value=>{if(live)setOpened({key,value});})
      .catch(reason=>{if(live)setError(reason instanceof Error?reason.message:String(reason));});
    return()=>{live=false;};
  },[key,visible,retry]);
  const ready=!view.error&&!error&&context?.status==='ready'&&opened?.key===key&&opened.value.resourceId===root?.resourceId;
  return <ToolbarTarget.Provider value={toolbar}><section className="project-workroom" aria-label={t("项目工作间")}>
    <header className="project-workroom-binding">
      <details ref={picker} className="workroom-project-picker" onKeyDown={event=>{if(event.key==='Escape'){event.stopPropagation();picker.current?.removeAttribute('open');picker.current?.querySelector('summary')?.focus();}}}>
      <summary aria-label={t("选择项目和目录")} title={root?.path||t("选择项目")}><UiIcon name="folder-open"/><strong>{context?.project?.name||root?.name||t("工作间")}</strong></summary>
      <div className="workroom-project-options"><label>{t("项目范围")}
      <select aria-label={t("工作间项目")} value={fileRequest?'follow':selection} onChange={e=>{onClearFileRequest();setError('');setSelection(e.target.value);picker.current?.removeAttribute('open');}}>
        <option value="follow">{t("跟随当前会话")}{context?.conversationId&&context.project?' · '+context.project.name:''}</option>
        {projects.data?.projects.map(project=><option key={project.id} value={project.id}>{project.name}</option>)}
      </select>
      </label>
      {context?.project&&context.roots.length>1&&<select aria-label={t("项目代码目录")} value={root?.originResourceId||''} onChange={e=>{
        if(fileRequest){setSelection('follow');onClearFileRequest();}
        const value={...roots,[rootPreferenceKey]:e.target.value};setRoots(value);saveJSON('workroom-project-roots',value);
      }}>{context.roots.map(item=><option key={item.resourceId} value={item.originResourceId}>{item.name}</option>)}</select>}
      {visible&&context?.status==="ready"&&context.project&&context.conversationId&&<button type="button" onClick={async()=>{
        const origin=context.conversationId!;
        try {
          const path=await window.workbench.chooseProject();
          if(!path)return;
          await api<{resourceId:string}>('/api/v2/workroom/attach-repository',
            {conversationId:origin,path,scopeRevision:context.scopeRevision,confirm:true});
          await Promise.all([
            queryClient.invalidateQueries({queryKey:["scope",origin],exact:true}),
            view.refetch(),
          ]);
          picker.current?.removeAttribute('open');
        } catch(reason){setError(reason instanceof Error?reason.message:String(reason));}
      }}>{t("关联已有仓库（只读）")}</button>}
      {root&&<p className="project-workroom-path" title={root.path}>{root.path}</p>}
      <button type="button" onClick={()=>{setError('');setOpened(undefined);setRetry(value=>value+1);void view.refetch();void projects.refetch();}}>{t("刷新")}</button>
      </div></details>
      {root?.checkout==='worktree'&&<small className="workroom-checkout">{t("独立工作树")}</small>}
      <div className="workroom-toolbar" ref={setToolbar}/>
    </header>
    {view.error||error?<p role="alert">{error||(view.error instanceof Error?view.error.message:'无法读取项目范围。')}</p>:
      !context?<p role="status">{t("正在读取项目范围…")}</p>:
      locationUnavailable?<p role="alert">{fileRequest?.kind==="conversation-record" ?
        t("此会话的记录目录暂不可用。") : t("目标检出目录已变化，请重新打开原链接或改动。")}<button type="button" onClick={onClearFileRequest}>{t("返回工作间选择")}</button></p>:
      context.status!=='ready'?<p role="status">{context.notice}{context.status==='no_project'&&conversationId?<button onClick={()=>void api('/api/v2/workroom/folders',{conversationId}).then(()=>view.refetch()).catch(e=>setError(e.message))}>{t("创建会话工作文件夹")}</button>:t("可新建会话，或在上方选择已有项目。")}</p>:
      !ready?<p role="status">{t("正在打开项目实际目录…")}</p>:null}
    {ready&&bindingContext&&<FormattingScope.Provider value={{conversation:bindingContext.conversationId||'', project:opened!.value.projectId||'', resource:opened!.value.resourceId, scope:opened!.value.scopeRevision}}>
      {children(opened!.value,bindingContext,()=>{onClearFileRequest();setSelection('follow');picker.current?.removeAttribute('open');})}
    </FormattingScope.Provider>}
  </section></ToolbarTarget.Provider>;
}
