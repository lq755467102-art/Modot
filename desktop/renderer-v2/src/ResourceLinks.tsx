// 按原会话范围路由网页与文件链接，并提供工作间和只读预览入口。
// 主要引用方包括 `Message.tsx`、`AgentHome.tsx` 和 `App.tsx`。
import { useUiLanguage } from "./UiLanguage";
import {createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type MouseEvent, type ReactNode} from "react";
import {createPortal} from "react-dom";
import {api} from "./client";
import type {FileContent,LinkedWorkroomFile} from "./generated";
import type {WorkroomFileTarget} from "./WorkroomChangeLink";
import {resourceOpeners, type ResourceLink, type ResourceOpener, type ResourceTarget} from "./resourceOpeners";
import "./workroom-file-context-menu.css";

export type WebLinkPage = {kind:"web"; id:string; owner:string; url:string; label:string};
export type LinkedFileTarget = Extract<ResourceTarget,{kind:"file"}>&{file:LinkedWorkroomFile};
export type ImageArtifactPage = {kind:"artifact-image"; id:string; owner:string; label:string; artifactId:string; versionId:string};
export type ResourceSidePage = WebLinkPage | ImageArtifactPage | {kind:"file"; id:string; owner:string; label:string; target:LinkedFileTarget};
export const ResourceLinkOwner = createContext<string | undefined>(undefined);

type LinkActions = {
  activate:(event:MouseEvent<HTMLAnchorElement>,link:ResourceLink,owner?:string)=>void;
  showMenu:(event:MouseEvent<HTMLAnchorElement>,link:ResourceLink,owner?:string)=>void;
  showFileMenu:(event:MouseEvent<HTMLElement>,target:LinkedFileTarget)=>void;
};
export const useResourceLinks = () => useContext(Actions);
const Actions = createContext<LinkActions | undefined>(undefined);

type Menu = {
  serial:number; scope:string; target:ResourceTarget; opener:HTMLElement;
  x:number; y:number; pending:boolean; error?:string;
};

function fileQuery(conversationId:string,path:string,file?:LinkedWorkroomFile,expectedPath?:string) {
  return new URLSearchParams({conversationId,path,...(file?{resourceId:file.resourceId,scopeRevision:file.scopeRevision}:{}),
    ...(expectedPath?{absolutePath:expectedPath}:{})}).toString().replaceAll("..","%2E%2E");
}

export function readLinkedFile(file:LinkedWorkroomFile) {
  return api<FileContent>("/api/v2/workroom/linked-file/content?"+fileQuery(file.conversationId,file.path,file,file.absolutePath));
}

async function resolveFile(target:ResourceTarget):Promise<LinkedFileTarget> {
  if(target.kind!=="file"||!target.conversationId)throw Error("文件链接需要所属会话");
  const previous=target.file;
  const file=await api<LinkedWorkroomFile>("/api/v2/workroom/linked-file?"+fileQuery(target.conversationId,previous?.path??target.path,previous));
  if(previous&&(file.resourceId!==previous.resourceId||file.scopeRevision!==previous.scopeRevision||
    file.absolutePath!==previous.absolutePath))throw Error("文件所在的检出目录或位置已变化，请重新打开链接");
  return {...target,file};
}

function reviewable(target:ResourceTarget):target is LinkedFileTarget {
  return target.kind==="file"&&!!target.file?.revision&&target.file.mode!=="binary";
}

type NativeAction = Parameters<Window["workbench"]["linkedFileAction"]>[0]["action"];

export function ResourceLinks({owner,scope,onWeb,onFile,onReview,children}:{
  owner:string; scope:string;
  onWeb:(target:ResourceTarget, place:"sidebar" | "workroom")=>void;
  onFile:(target:WorkroomFileTarget)=>void;
  onReview:(target:LinkedFileTarget)=>void;
  children:ReactNode;
}) {
  const { t } = useUiLanguage();
  const [menu,setMenu]=useState<Menu>();
  const [notice,setNotice]=useState("");
  const serial=useRef(0);
  const currentScope=useRef(scope);
  currentScope.current=scope;
  const panel=useRef<HTMLDivElement>(null);
  const inAppWeb=(t:ResourceTarget)=>t.kind==="web"&&!!owner;
  const localFile=(t:ResourceTarget):t is LinkedFileTarget=>t.kind==="file"&&!!t.file;
  const openFile=(target:ResourceTarget,readonly:boolean)=>{
    if(target.kind!=="file"||!target.file)return;
    const {conversationId,resourceId,path,scopeRevision,revision}=target.file;
    onFile({conversationId,resourceId,path,scopeRevision,revision,line:target.line,
      kind:readonly?"linked-file-readonly":undefined});
  };
  const native=(action:NativeAction)=>async(target:ResourceTarget)=>{
    if(target.kind!=="file"||!target.file)return;
    const {conversationId,resourceId,scopeRevision,path,absolutePath}=target.file;
    const result=await window.workbench.linkedFileAction({action,conversationId,resourceId,scopeRevision,path,absolutePath,line:target.line});
    if(action==="save"&&result.path)setNotice("文件副本已保存");
  };
  const copy=async(text:string,message:string)=>{
    await navigator.clipboard.writeText(text);
    setNotice(message);
  };
  const builtins:ResourceOpener[]=[
    {id:"right",label:t("在右栏新建标签页"),accepts:inAppWeb,open:t=>onWeb(t,"sidebar")},
    {id:"workroom-web",label:t("在工作间打开网页"),accepts:inAppWeb,open:t=>onWeb(t,"workroom")},
    {id:"file-review",label:t("在右栏审查"),accepts:reviewable,open:t=>{if(reviewable(t))onReview(t);}},
    {id:"workroom-edit",label:t("在工作间编辑"),accepts:t=>t.file?.mode==="edit",open:t=>openFile(t,false)},
    {id:"workroom-read",label:t("在工作间只读查看"),accepts:t=>t.file?.mode==="readonly"||t.file?.mode==="binary",open:t=>openFile(t,true)},
    {id:"file-open",label:t("使用默认应用打开"),accepts:t=>!!t.file?.defaultOpen,open:native("open")},
    {id:"file-choose",label:t("选择应用打开…"),accepts:localFile,open:native("choose")},
    {id:"file-reveal",label:t("在资源管理器中定位"),accepts:localFile,open:native("reveal")},
    {id:"file-save",label:t("另存为…"),accepts:localFile,open:native("save")},
    {id:"file-copy-path",label:t("复制路径"),accepts:localFile,open:t=>{if(localFile(t))return copy(t.file.absolutePath,"文件路径已复制");}},
    {id:"file-copy-content",label:t("复制文件内容"),accepts:reviewable,open:async t=>{
      if(reviewable(t))await copy((await readLinkedFile(t.file)).text,"文件内容已复制");
    }},
    {id:"system",label:t("在系统浏览器中打开"),accepts:t=>t.kind==="web",open:t=>{window.open(t.href,"_blank");}},
    {id:"mail",label:t("使用系统邮件应用打开"),accepts:t=>t.kind==="mail",open:t=>{window.open(t.href,"_blank");}},
    {id:"copy",label:t("复制链接"),accepts:()=>true,open:t=>copy(t.href,"链接已复制")},
  ];
  const owns=(value:Menu)=>serial.current===value.serial&&currentScope.current===value.scope;
  function close() {
    serial.current++;
    setMenu(undefined);
  }
  useEffect(()=>{
    setMenu(value=>value?.scope===scope?value:undefined);
  },[scope]);
  useEffect(()=>{
    if(!notice)return;
    const timer=setTimeout(()=>setNotice(""),1800);
    return ()=>clearTimeout(timer);
  },[notice]);

  function linkTarget(event:MouseEvent<HTMLAnchorElement>,link:ResourceLink,source=owner):ResourceTarget {
    return {...link,conversationId:source,label:event.currentTarget.textContent?.trim()||link.href};
  }
  function menuAt(event:MouseEvent<HTMLElement>,target:ResourceTarget):Menu {
    event.preventDefault();
    event.stopPropagation();
    const opener=event.currentTarget;
    const rect=opener.getBoundingClientRect();
    const keyboard=event.type==="click"&&opener.tagName==="BUTTON"||event.clientX===0&&event.clientY===0;
    return {serial:++serial.current,scope,target,opener,pending:target.kind==="file",
      x:keyboard?rect.left:event.clientX,y:keyboard?rect.bottom:event.clientY};
  }
  function resolveMenuFile(next:Menu,onResolved:(resolved:LinkedFileTarget)=>void){
    void resolveFile(next.target).then(file=>{
      if(owns(next))onResolved(file);
    },error=>{
      if(owns(next))setMenu({...next,target:{...next.target,file:undefined},pending:false,
        error:String(error instanceof Error?error.message:error)});
    });
  }
  function showTargetMenu(event:MouseEvent<HTMLElement>,target:ResourceTarget) {
    const next=menuAt(event,target);
    setMenu(next);
    if(next.pending)resolveMenuFile(next,file=>setMenu({...next,target:file,pending:false}));
  }
  function showMenu(event:MouseEvent<HTMLAnchorElement>,link:ResourceLink,source?:string) {
    showTargetMenu(event,linkTarget(event,link,source));
  }
  function activate(event:MouseEvent<HTMLAnchorElement>,link:ResourceLink,source?:string) {
    event.preventDefault();
    const target=linkTarget(event,link,source);
    if(target.kind==="file"){
      const next=menuAt(event,target);
      setMenu(undefined);
      resolveMenuFile(next,resolved=>{
        if(reviewable(resolved))onReview(resolved);
        else setMenu({...next,target:resolved,pending:false});
      });
      return;
    }
    if(inAppWeb(target)){
      onWeb(target,"sidebar");
      return;
    }
    window.open(link.href,"_blank");
  }

  const visible=menu?.scope===scope?menu:undefined;
  useLayoutEffect(()=>{
    const node=panel.current;
    if(!visible||!node)return;
    node.style.left=Math.max(8,Math.min(visible.x,innerWidth-node.offsetWidth-8))+"px";
    node.style.top=Math.max(8,Math.min(visible.y,innerHeight-node.offsetHeight-8))+"px";
    if(!node.contains(document.activeElement))(node.querySelector<HTMLElement>("button:enabled")||node).focus({preventScroll:true});
  },[visible]);
  useEffect(()=>{
    if(!visible)return;
    const outside=(event:PointerEvent)=>{if(!panel.current?.contains(event.target as Node))close();};
    const escape=(event:KeyboardEvent)=>{
      if(event.key!=="Escape")return;
      event.preventDefault();
      event.stopPropagation();
      close();
      if(visible.opener.isConnected)visible.opener.focus({preventScroll:true});
    };
    document.addEventListener("pointerdown",outside);
    document.addEventListener("keydown",escape,true);
    window.addEventListener("resize",close);
    return ()=>{
      document.removeEventListener("pointerdown",outside);
      document.removeEventListener("keydown",escape,true);
      window.removeEventListener("resize",close);
    };
  },[visible]);

  async function choose(id:string,origin:Menu) {
    if(origin.pending||!owns(origin))return;
    setMenu({...origin,pending:true,error:undefined});
    try {
      let target=origin.target;
      if(target.kind==="file"&&id!=="copy"){
        if(!target.file)throw Error("文件位置尚未核对");
        target=await resolveFile(target);
      }
      if(!owns(origin))return;
      const opener=resourceOpeners(target,builtins).find(item=>item.id===id);
      if(!opener)throw Error("此打开方式已不可用");
      await opener.open(target);
      if(owns(origin)){
        close();
        if((id==="copy"||id.startsWith("file-copy"))&&origin.opener.isConnected)origin.opener.focus({preventScroll:true});
      }
    }catch(error){
      if(owns(origin))setMenu({...origin,pending:false,error:String(error instanceof Error?error.message:error)});
    }
  }

  return <Actions.Provider value={{activate,showMenu,showFileMenu:showTargetMenu}}>{children}
    {visible&&createPortal(<div className="workroom-file-context-menu resource-link-menu" aria-label={t("链接打开方式")}
      role="group" tabIndex={-1} ref={panel}>
      {resourceOpeners(visible.target,builtins).map(opener=><button key={opener.id} type="button"
        disabled={visible.pending} onClick={()=>void choose(opener.id,visible)}>{opener.label}</button>)}
      {visible.pending&&<p role="status">正在处理链接…</p>}
      {visible.error&&<p role="alert">{visible.error}</p>}
    </div>,document.body)}
    {notice&&createPortal(<p className="resource-link-feedback" role="status">{notice}</p>,document.body)}
  </Actions.Provider>;
}
