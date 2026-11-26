// 在插件管理入口嵌入旧版管理界面，并桥接该界面的操作与焦点。
// 主要引用方包括 `PluginManagementShell.tsx`。
import {useEffect,useRef,useState,type ReactNode} from "react";
import {RetainedDialog} from "./RetainedDialog";
export function LegacyManagementPane({section,open,onClose,theme,conversationId,children,embedded=false}:{section:string;open:boolean;onClose:()=>void;theme:string;conversationId?:string;children?:ReactNode;embedded?:boolean}){
  const region=useRef<HTMLDivElement>(null),owner=useRef('');const [id,setId]=useState(''),[error,setError]=useState('');
  useEffect(()=>{let active=true;
    setId('');window.workbench.management({operation:'open',section,theme,conversationId}).then(result=>{if(!active){window.workbench.management({operation:'close',id:result.id}).catch(()=>{});return;}owner.current=result.id;setId(result.id);setError('');}).catch(e=>{if(active)setError(e.message);});
    return()=>{active=false;if(owner.current)window.workbench.management({operation:'close',id:owner.current}).catch(()=>{});owner.current='';};
  },[section,conversationId]);
  useEffect(()=>{if(!id||!open||section!=='execution')return;let active=true;const refresh=()=>window.workbench.management({operation:'refresh',id}).catch(e=>{if(active)setError(e.message);});refresh();const timer=setInterval(refresh,3000);return()=>{active=false;clearInterval(timer);};},[id,open,section]);
  useEffect(()=>{if(id)window.workbench.management({operation:'theme',id,theme}).catch(e=>setError(e.message));},[id,theme]);
  useEffect(()=>{
    if(!id)return;
    let active=true,previous='';
    const update=()=>{
      if(!active||!region.current)return;
      const r=region.current.getBoundingClientRect();
      const bounds={x:Math.max(0,r.x),y:Math.max(0,r.y),width:r.width,height:r.height,visible:open&&!!r.width&&!!r.height&&
        !region.current.closest('[hidden],[inert]')&&!document.querySelector('.application-menu-items')};
      const key=JSON.stringify(bounds);
      if(key===previous)return;
      previous=key;
      window.workbench.management({operation:'bounds',id,...bounds}).catch(e=>{if(active){previous='';setError(e.message);}});
    };
    update();
    if(!open)return ()=>{active=false;};
    const resize=new ResizeObserver(update);
    if(region.current)resize.observe(region.current);
    const menus=new MutationObserver(update);
    const titlebar=document.querySelector('.application-titlebar');
    if(titlebar)menus.observe(titlebar,{childList:true,subtree:true});
    window.addEventListener('resize',update);
    window.addEventListener('scroll',update,true);
    const timer=setInterval(update,500);
    return ()=>{active=false;resize.disconnect();menus.disconnect();window.removeEventListener('resize',update);window.removeEventListener('scroll',update,true);clearInterval(timer);};
  },[id,open]);
  const content=<>
    {error&&<p role="alert">{error}</p>}{!id&&!error&&<p role="status">正在读取…</p>}
    {children}
    <div ref={region} className="legacy-management-region" aria-label="原页面管理内容" style={embedded?{flex:1,minHeight:180}:{height:'65vh',minHeight:220}}/>
  </>;
  return embedded?<section className="embedded-management">{content}</section>:<RetainedDialog open={open} label={section==='skills'?'装了什么':'状态'} onClose={onClose}>{content}</RetainedDialog>;
}
