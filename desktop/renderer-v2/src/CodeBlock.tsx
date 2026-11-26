// 呈现消息或证据中的代码块，统一处理复制、换行和语法着色。
// 主要引用方包括 `CodeActivity.tsx` 和 `ContextResultEvidence.tsx`。
import {memo,useEffect,useLayoutEffect,useRef,useState} from "react";
import {colorSource, MAX_SYNTAX_CHARS} from "./useSourceSyntax";
import {SyntaxText} from "./SyntaxText";
import type {SyntaxLines} from "./syntaxEngine";
import './message-reading.css';

export const CodeBlock=memo(function CodeBlock({text,language="",label,streaming=false}:{text:string;language?:string;label?:string;streaming?:boolean}) {
  const [syntax,setSyntax]=useState<{language:string;tokens:SyntaxLines}>();
  const [copyStatus,setCopyStatus]=useState("");
  const copyRequest=useRef(0);
  useLayoutEffect(()=>{
    copyRequest.current++;setCopyStatus('');
    return()=>{copyRequest.current++;};
  },[text]);
  const [wrap,setWrap]=useState(false);
  const viewport=useRef<HTMLDivElement>(null);
  const follow=useRef(true);
  const latestText=useRef(text);
  latestText.current=text;
  const canColor=!!language && text.length>0 && text.length<=MAX_SYNTAX_CHARS;
  const finalText=streaming?undefined:text;
  useEffect(()=>{
    if(streaming&&follow.current&&viewport.current)viewport.current.scrollTop=viewport.current.scrollHeight;
  },[streaming,text]);
  useEffect(()=>{
    if(!canColor)return;
    let cancelled=false;
    let timer:number;
    let requested:string|undefined;
    async function update() {
      const source=finalText??latestText.current;
      if(source!==requested) {
        requested=source;
        const tokens=await colorSource(source,"",language);
        if(!cancelled)setSyntax(tokens?{language,tokens}:undefined);
      }
      if(streaming&&!cancelled)timer=window.setTimeout(update,200);
    }
    timer=window.setTimeout(update,streaming?200:0);
    return()=>{cancelled=true;window.clearTimeout(timer);};
  },[canColor,language,finalText,streaming]);
  return <div className="message-code-block" data-code-language={language}>
    <div className="message-code-toolbar">
      <span>{label||language||"纯文本"}</span>
      <button type="button" aria-pressed={wrap} onClick={()=>setWrap(value=>!value)}>换行</button>
      <button type="button" aria-label="复制代码" onClick={async()=>{
        const request=++copyRequest.current;setCopyStatus('正在复制…');
        try{await navigator.clipboard.writeText(text);if(copyRequest.current===request)setCopyStatus("已复制");}
        catch{if(copyRequest.current===request)setCopyStatus("复制失败");}
      }}>复制</button>
      <span className="message-copy-feedback" role="status" aria-atomic="true">{copyStatus}</span>
    </div>
    <div ref={viewport} className={`message-code-scroll ${wrap?"wrap":""}`} role="region" aria-label="代码内容" tabIndex={0}
      onScroll={event=>{const view=event.currentTarget;follow.current=view.scrollHeight-view.scrollTop-view.clientHeight<16;}}>
      <pre><code>{canColor&&syntax?.language===language?text.split("\n").map((line,index)=><span key={index}>
        {index>0?"\n":""}<SyntaxText text={line} tokens={syntax.tokens[index]}/>
      </span>):text}</code></pre>
    </div>
  </div>;
});
