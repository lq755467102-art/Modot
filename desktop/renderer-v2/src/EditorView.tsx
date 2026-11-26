// 将可编辑文档和定位要求接入 CodeMirror，向上层报告输入变化。
// 主要引用方包括 `Panels.tsx` 和 `WorkroomPage.tsx`。
import CodeMirror, { type ReactCodeMirrorRef } from "@uiw/react-codemirror";
import {useCallback,useEffect,useMemo,useRef,useState,type RefObject} from "react";
import {createPortal} from 'react-dom';
import {EditorView as CodeView,lineNumbers,type ViewUpdate} from '@codemirror/view';
import { python } from "@codemirror/lang-python";
import { javascript, javascriptLanguage } from "@codemirror/lang-javascript";
import {loadJSON,saveJSON} from './client';
import {DocumentFormatting,formatRevision} from './DocumentFormatting';
import {EditorActions,editingKeys} from './EditorActions';
import {useEditorPacks} from './editorExtensionStore';
import {editorSnippetExtensions} from './editorSnippetCompletions';
import {semanticExtensions} from './semanticCompletion';
import type {LanguageSession} from './workroomLanguage';
// JSON 文档从表达式入口着色，复用编辑器已有语法包。保存和格式校验仍由各自的入口完成。
const jsonHighlight=javascriptLanguage.configure({top:'SingleExpression'});
const selectLineByNumber = lineNumbers({
  domEventHandlers: {
    mousedown(view, block, event) {
      if (!(event instanceof MouseEvent) || event.button !== 0 || event.ctrlKey || event.metaKey || event.altKey) return false;
      const doc = view.state.doc, line = doc.lineAt(block.from);
      const end = Math.min(doc.length, line.to + 1);
      const anchor = event.shiftKey ? view.state.selection.main.anchor : line.from;
      view.dispatch({selection: {anchor, head: anchor <= line.from ? end : line.from}, userEvent: 'select.pointer'});
      view.focus();
      return true;
    },
  },
});
// CodeMirror 的光标锚点和选区末端为文档偏移，`top`、`left` 是滚动坐标。
// 调用方可按文件保存并恢复这四项。
export type EditorPosition={anchor:number;head:number;top:number;left:number};
// Editor offsets and selections are measured against CodeMirror's LF document.
export const normalizeEditorText=(value:string)=>value.replace(/\r\n?/g,"\n");
// `WorkroomPage.tsx` 提供当前标签的草稿、字号、定位和保存选区回调，`Panels.tsx` 负责懒加载。
export function EditorView({
  editorRef,
  path,
  value,
  theme,
  codeSize,
  line,
  lineJump,
  onChange,
  onLine,
  position,onPosition,toolsTarget,language,
  readOnly=false,
}: {
  editorRef: RefObject<ReactCodeMirrorRef | null>;
  path: string;
  value: string;
  theme: "light" | "dark";
  codeSize: number;
  line?: number;
  lineJump?: {line:number;serial:number};
  onChange?: (text: string) => void;
  onLine: (line: number) => void;
  position?:EditorPosition;
  onPosition?:(position:EditorPosition)=>void;
  toolsTarget?:HTMLElement|null;
  language?:LanguageSession;
  readOnly?:boolean;
}) {
  const callbacks=useRef({onChange,onLine,onPosition});callbacks.current={onChange,onLine,onPosition};
  // 草稿每次输入都会让父页重渲染；稳定回调可保留 CodeMirror 当前的片段占位和查找面板。
  const change=useCallback((text:string)=>callbacks.current.onChange?.(text),[]);
  // 选区变化时才回报当前行号和位置。滚动位置由下方的滚动处理器单独同步。
  const update=useCallback((event:ViewUpdate)=>{
    if(!event.selectionSet)return;
    remember(event.view);callbacks.current.onLine(event.state.doc.lineAt(event.state.selection.main.head).number);
  },[]);
  const {data:packs}=useEditorPacks(!readOnly);
  // CodeMirror stores LF internally. Comparing a CRLF value to the normalized
  // document would replace it on mount and shift the selected source line.
  const editorValue=useMemo(()=>normalizeEditorText(value),[value]);
  const [wrap,setWrap]=useState(()=>loadJSON('editor-soft-wrap',false));
  const appliedJump=useRef(0);
  // 同一条行号定位请求只执行一次。依据 CodeMirror 当前文档裁剪行号，再滚动并聚焦目标行。
  function reveal(view:CodeView,jump:{line:number;serial:number}){
    if(jump.serial===appliedJump.current)return;
    appliedJump.current=jump.serial;
    const line=view.state.doc.line(Math.max(1,Math.min(jump.line,view.state.doc.lines)));
    view.dispatch({selection:{anchor:line.from},effects:CodeView.scrollIntoView(line.from,{y:"center"})});
    view.focus();
  }
  useEffect(()=>{const view=editorRef.current?.view;if(view&&lineJump)reveal(view,lineJump);},[lineJump?.serial]);
  // 将光标、选区和滚动位置回传给 `WorkroomPage.tsx`，供切换文件后恢复。
  function remember(view:CodeView){const selection=view.state.selection.main;callbacks.current.onPosition?.({anchor:selection.anchor,head:selection.head,top:view.scrollDOM.scrollTop,left:view.scrollDOM.scrollLeft});}
  // 字号作为 CodeMirror 主题配置更新，编辑器会重新测量行高并同步行号。
  const fontTheme=useMemo(()=>CodeView.theme({".cm-scroller":{fontSize:`${codeSize}px`}}),[codeSize]);
  // 按路径装配语言支持，从 `editorSnippetCompletions.ts` 接入插件片段，并接入 `DocumentFormatting.tsx` 的草稿版本。
  const extensions=useMemo(()=>[
    fontTheme,
    selectLineByNumber,
    ...(/\.pyi?$/i.test(path)?[python()]:/\.[cm]?[jt]sx?$/.test(path)?[javascript({typescript:true,jsx:true})]:/\.jsonc?$/i.test(path)?[jsonHighlight]:[]),
    ...(readOnly?[]:[formatRevision,editingKeys]),
    wrap?CodeView.lineWrapping:[],
    ...(readOnly?[]:[editorSnippetExtensions(packs??[],path)]),
    ...(!readOnly&&language?semanticExtensions(language,path):[]),
    CodeView.domEventHandlers({scroll:(_event,view)=>{remember(view);return false;}}),
  ],[fontTheme,path,packs,wrap,readOnly,language]);
  const actions=<EditorActions view={()=>editorRef.current?.view} wrap={wrap} onWrap={()=>{saveJSON('editor-soft-wrap',!wrap);setWrap(!wrap);}}/>;
  return (
    <div className="workroom-text-editor">
    {!readOnly&&<>
      {toolsTarget?createPortal(actions,toolsTarget):actions}
      <DocumentFormatting value={value} path={path} view={()=>editorRef.current?.view} toolsTarget={toolsTarget}/>
    </>}
    <CodeMirror
      ref={editorRef}
      value={editorValue}
      readOnly={readOnly}
      theme={theme}
      height="100%"
      extensions={extensions}
      onChange={change}
      onCreateEditor={(view) => {
        // 显式定位优先，其次恢复标签保存的选区，最后使用打开文件时附带的初始行号。
        if(lineJump){
          reveal(view,lineJump);
        }else if(position){
          // 上次保存的位置可能超出新文档长度，传给 CodeMirror 前先裁剪偏移。
          const clamp=(value:number)=>Math.max(0,Math.min(view.state.doc.length,value));
          view.dispatch({selection:{anchor:clamp(position.anchor),head:clamp(position.head)}});
          // 等编辑器布局可用再恢复滚动。文件已经切走时，旧节点不再接收这次更新。
          requestAnimationFrame(()=>{if(view.dom.isConnected){view.scrollDOM.scrollTop=position.top;view.scrollDOM.scrollLeft=position.left;}});
        }else if (line) {
          const target = view.state.doc.line(
            Math.max(1, Math.min(line, view.state.doc.lines)),
          );
          view.dispatch({
            selection: { anchor: target.from },
            scrollIntoView: true,
          });
        }
      }}
      onUpdate={update}
    /></div>
  );
}
