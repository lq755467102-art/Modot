// 显示工作间已打开文件、当前文件和未保存标记，并传递切换与关闭动作。
// 主要引用方包括 `WorkroomPage.tsx`。
import { useUiLanguage } from "./UiLanguage";
import {useLayoutEffect,useRef} from 'react';
import type {OpenFile} from './domain';
import {FileTypeIcon} from './FileTypeIcon';

// `instance` 对应一次打开文件的实例。可编辑标签沿用 `domain.ts` 的 `OpenFile`，只读标签只携带目录、路径和定位。
export type WorkroomFile =
  | (OpenFile & { instance: string; readOnly?: false })
  | { instance: string; workspaceId: string; path: string; line?: number; readOnly: true };
// `WorkroomPage.tsx` 提供标签与当前项。关闭时回传完整文件对象，由工作间处理未保存确认。
export function WorkroomFileTabs({files,active,onSelect,onClose}:{files:WorkroomFile[];active:string;onSelect:(key:string)=>void;onClose:(file:WorkroomFile)=>void}){
  const { t } = useUiLanguage();
  const root=useRef<HTMLDivElement>(null);
  // 当前标签因切换或新增而移到可视区外时，只水平滚动标签栏。隐藏布局暂不测量。
  useLayoutEffect(()=>{
    const host=root.current,item=host?.querySelector<HTMLElement>('[aria-current="page"]');
    if(!host||!item||!host.clientWidth)return;
    const box=host.getBoundingClientRect(),tab=item.getBoundingClientRect();
    if(tab.left<box.left)host.scrollLeft-=box.left-tab.left;
    else if(tab.right>box.right)host.scrollLeft+=tab.right-box.right;
  },[active,files.length]);
  return <div ref={root} className="editor-tabs" aria-label={t("已打开文件")} onKeyDown={event=>{
    // 输入法组合期间保留按键给输入法。标签自己的方向键和首末跳转与鼠标选中共用 `onSelect`。
    if(event.nativeEvent.isComposing||event.keyCode===229||!(event.target as HTMLElement).hasAttribute('data-file-select'))return;
    const index=files.findIndex(file=>file.workspaceId+':'+file.path===active);
    const next=event.key==='Home'?0:event.key==='End'?files.length-1:event.key==='ArrowLeft'?Math.max(0,index-1):event.key==='ArrowRight'?Math.min(files.length-1,index+1):-1;
    if(next<0||!files[next])return;event.preventDefault();const key=files[next].workspaceId+':'+files[next].path;onSelect(key);
    root.current?.querySelectorAll<HTMLButtonElement>('[data-file-select]')[next]?.focus();
  }}>{files.map(file=>{const key=file.workspaceId+':'+file.path,dirty=!file.readOnly&&file.draft!==file.text;return <div key={file.instance} className={'editor-tab'+(active===key?' selected':'')}>
    <button type="button" data-file-select={key} aria-current={active===key?'page':undefined} title={file.path} aria-label={file.path+(dirty?' · 未保存':'')} onClick={()=>onSelect(key)}><FileTypeIcon path={file.path}/><span>{file.path.split('/').pop()}</span>{dirty&&<i aria-hidden="true" className="workroom-dirty-dot"/>}</button>
    <button type="button" className="file-tab-close" aria-label={'关闭 '+file.path} onClick={()=>onClose(file)}>×</button>
  </div>;})}</div>;
}
