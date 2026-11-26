// 管理对话框的初始焦点、键盘循环和关闭后的焦点归还。
// 主要引用方包括 `Dialog.tsx` 和 `RetainedDialog.tsx`。
import { useEffect, useRef } from "react";

const modalStack: HTMLElement[] = [];

function usable(element: HTMLElement | null): element is HTMLElement {
  // 折叠的 details 内容仍可能有布局矩形，首个 summary 保持可聚焦。
  return !!element?.isConnected
    && !element.matches(':disabled')
    && !element.closest('[hidden],[inert],details:not([open]) > :not(summary:first-of-type)')
    && element.getClientRects().length > 0
    && getComputedStyle(element).visibility === 'visible';
}
function controls(dialog:HTMLElement) {
  return Array.from(dialog.querySelectorAll<HTMLElement>('button,input,select,textarea,a[href],summary,[tabindex]')).filter(e=>e.tabIndex>=0&&usable(e));
}

export function wrapDialogTab(event: KeyboardEvent, dialog: HTMLElement) {
  if (event.key !== "Tab" || event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
  const items = controls(dialog);
  const first = items[0] || dialog, last = items.at(-1) || dialog;
  const active = document.activeElement;
  if (!items.length || !dialog.contains(active) || active === (event.shiftKey ? first : last)) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  }
}
function returnable(element:HTMLElement|null):element is HTMLElement {
  return usable(element)&&element.matches('button,input,select,textarea,a[href],summary,[tabindex],dialog,[contenteditable="true"]')&&!element.matches('[aria-disabled="true"]');
}
function topModal() {
  const native=document.activeElement?.closest<HTMLElement>('dialog:modal')||Array.from(document.querySelectorAll<HTMLElement>('dialog:modal')).at(-1);
  const custom=[...modalStack].reverse().find(usable);
  return native&&(!custom||!native.contains(custom))?native:custom||native;
}

/** Capture before opening; defer restoration until replacement dialogs have mounted. */
export function captureDialogReturn() {
  const opener=document.activeElement instanceof HTMLElement?document.activeElement:null;
  const scopes:HTMLElement[]=[];
  for(let scope=opener?.parentElement;scope;scope=scope.parentElement)scopes.push(scope);
  return()=>queueMicrotask(()=>{
    const parent=topModal()||Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).filter(usable).at(-1);
    if(parent){
      if(usable(document.activeElement as HTMLElement|null)&&parent.contains(document.activeElement))return;
      (returnable(opener)&&parent.contains(opener)?opener:controls(parent)[0]||parent).focus();
    }else if(returnable(opener))opener.focus();
    else{
      for(const scope of scopes){
        if(!scope.isConnected)continue;
        const target=controls(scope)[0];
        if(target){target.focus();if(document.activeElement===target)break;}
      }
    }
  });
}

/** Modal focus stays in the dialog; dismissal returns to its actual opener. */
export function useDialogFocus(onClose: () => void, canClose = true) {
  const ref = useRef<HTMLElement | null>(null);
  const close = useRef(onClose);
  const allowed = useRef(canClose);
  close.current = onClose;
  allowed.current = canClose;
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const dialog: HTMLElement = element;
    const restore = captureDialogReturn();
    modalStack.push(dialog);
    if(topModal()===dialog)(controls(dialog)[0] || dialog).focus();
    function key(event: KeyboardEvent) {
      if (topModal() !== dialog) return;
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        if(allowed.current)close.current();
      }
      wrapDialogTab(event, dialog);
    }
    function focus(event: FocusEvent) {
      if (topModal() !== dialog) return;
      if (event.target instanceof Node && !dialog.contains(event.target))
        (controls(dialog)[0] || dialog).focus();
    }
    document.addEventListener("keydown", key, true);
    document.addEventListener("focusin", focus);
    return () => {
      document.removeEventListener("keydown", key, true);
      document.removeEventListener("focusin", focus);
      const index = modalStack.indexOf(dialog);
      if (index >= 0) modalStack.splice(index, 1);
      restore();
    };
  }, []);
  return ref;
}
