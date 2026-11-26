// 使已访问面板在隐藏时保留本地状态，并退出键盘焦点交互。
// 主要引用方包括 `AgentHome.tsx` 和 `ConversationHistoryRail.tsx`。
import { useLayoutEffect, useRef, type ReactNode } from "react";
import {captureDialogReturn} from './useDialogFocus';

// Keep unsaved tool forms mounted outside the original conversation column.
export function RetainedDialog({open,label,onClose,children,compact=false}: {open:boolean;label:string;onClose:()=>void;children:ReactNode;compact?:boolean}) {
  const ref=useRef<HTMLDialogElement>(null);
  const composing=useRef(false);
  useLayoutEffect(()=>{
    const el=ref.current!;
    if(open&&!el.open){
      composing.current=false;const restore=captureDialogReturn();el.showModal();
      return()=>{composing.current=false;if(el.open)el.close();restore();};
    }else if(!open&&el.open)el.close();
  },[open]);
  return <dialog ref={ref} className={'legacy-tool-dialog'+(compact?' compact-dialog':'')} role={open?"dialog":undefined} aria-modal={open||undefined} aria-label={label}
    onCompositionStartCapture={()=>{composing.current=true;}} onCompositionEndCapture={()=>{composing.current=false;}}
    onCancel={e=>{e.preventDefault();if(!composing.current)onClose();}}>
    <header className="tool-title"><h2>{label}</h2><button type="button" onClick={onClose}>返回对话</button></header>
    {children}
  </dialog>;
}
