import {useRef,useState} from "react";
import {useQuery} from "@tanstack/react-query";
import type {ReactCodeMirrorRef} from "@uiw/react-codemirror";
import {useApplicationSettings} from "./ApplicationSettings";
import {EditorView} from "./EditorView";
import {readLinkedFile,useResourceLinks,type LinkedFileTarget} from "./ResourceLinks";
import "./linked-file-review.css";

export default function LinkedFileReview({target,visible}:{target:LinkedFileTarget;visible:boolean}) {
  const {theme,preferences}=useApplicationSettings();
  const actions=useResourceLinks();
  const editor=useRef<ReactCodeMirrorRef>(null);
  const [line,setLine]=useState(target.line??1);
  const file=target.file;
  const content=useQuery({
    queryKey:["linked-file-content",file.conversationId,file.scopeRevision,file.resourceId,file.path,file.absolutePath],
    queryFn:()=>readLinkedFile(file),enabled:visible,retry:false,refetchOnWindowFocus:true,
  });
  return <section className="side-content linked-file-review" aria-label="文件审查">
    <header onContextMenu={event=>actions?.showFileMenu(event,target)}>
      <div><h2 title={file.absolutePath}>{file.path}</h2><small>只读审查 · 第 {line} 行</small></div>
      <button type="button" aria-label="文件操作" onClick={event=>actions?.showFileMenu(event,target)}>文件操作</button>
    </header>
    {content.isPending&&<p role="status">正在读取文件…</p>}
    {content.error&&<p role="alert">{content.error.message} <button onClick={()=>void content.refetch()}>重新读取</button></p>}
    {content.data&&!content.error&&<EditorView key={content.data.revision} editorRef={editor} path={file.path}
      value={content.data.text} theme={theme==="light"?"light":"dark"} codeSize={preferences.codeSize??12}
      lineJump={visible&&target.line?{line:target.line,serial:1}:undefined} onLine={setLine} readOnly/>}
  </section>;
}
