// 展示可分页的源码或差异，保持行号定位和异步语法着色一致。
// 主要引用方包括 `InlineCodeChanges.tsx` 和 `CodeReviewDrawer.tsx`。
import { useEffect, useRef, useState } from "react";
import {createPortal} from "react-dom";
import {ReviewPopover} from "./ReviewPopover";
import {useUiLanguage} from "./UiLanguage";
import { useQuery } from "@tanstack/react-query";
import { api, queryString } from "./client";
import type { SourceFilePage, SourceSearchPage } from "./generated";
import { diffLines, fullLines, splitLines, type CodeLine } from "./codeDiff";
import { useSourceSyntax,type SourceSyntax } from "./useSourceSyntax";
import { SyntaxText } from "./SyntaxText";

import { codeURL,initialCodeReading,type CodeAnchor,type CodeReading,type CodeSide,type CodeTarget } from "./sourceCode";
export { codeURL,initialCodeReading,type CodeAnchor,type CodeReading,type CodeSide,type CodeTarget } from "./sourceCode";
export function CodeRows({lines,split=false,onLine,syntax,full=false,anchor,afterSide='after'}:{lines:CodeLine[];split?:boolean;onLine?:(side:"before"|"after",line:number)=>void;syntax?:SourceSyntax;full?:boolean;anchor?:CodeAnchor;afterSide?:'after'|'staged'}) {
  const code=(line:CodeLine|undefined,side:'before'|'after')=>{
    if(!line)return null;
    const n=side==='before'?line.oldLine:line.newLine;
    const tokens=n?syntax?.[side==='after'?afterSide:side]?.[n-1]:undefined;
    return <SyntaxText text={line.text} tokens={tokens}/>;
  };
  const number=(value:number|undefined,side:"before"|"after")=>value===undefined?"":onLine?
    <button tabIndex={0} className="code-line-number" aria-label={`${side==="before"?"改前":"改后"}第 ${value} 行`} onClick={()=>onLine(side,value)}>{value}</button>:value;
  const selected=(side:"before"|"after",line?:number)=>anchor?.side===(side==="after"?afterSide:side)&&anchor.line===line;
  return <table className={`source-code-table ${split?"split":"unified"}`} aria-label={full?"完整源码":split?"左右对照代码":"代码增减"}><tbody>
    {split?splitLines(lines).map((row,i)=>row.meta?<tr key={i} className={row.meta.kind}><td colSpan={6}>{row.meta.text}</td></tr>:
      <tr key={i}>
        <td className={`number ${row.left?.kind||"empty"}`}>{number(row.left?.oldLine,"before")}</td>
        <td className={`sign ${row.left?.kind||"empty"}`}>{row.left?.kind==="deletion"?"-":""}</td>
        <td className={`code-text ${row.left?.kind||"empty"}`} data-old-line={row.left?.oldLine} data-code-anchor={selected("before",row.left?.oldLine)||undefined}>{code(row.left,'before')}</td>
        <td className={`number ${row.right?.kind||"empty"}`} data-new-line={row.right?.newLine}>{number(row.right?.newLine,"after")}</td>
        <td className={`sign ${row.right?.kind||"empty"}`} data-new-line={row.right?.newLine}>{row.right?.kind==="addition"?"+":""}</td>
        <td className={`code-text ${row.right?.kind||"empty"}`} data-new-line={row.right?.newLine} data-code-anchor={selected("after",row.right?.newLine)||undefined}>{code(row.right,'after')}</td>
      </tr>):lines.map((line,i)=>line.kind==="hunk"||line.kind==="notice"?<tr key={i} className={line.kind}><td colSpan={4}>{line.text}</td></tr>:
      <tr key={i} className={line.kind} data-old-line={line.oldLine} data-new-line={line.newLine} data-code-anchor={!!anchor&&(selected("before",line.oldLine)||selected("after",line.newLine))||undefined}>
        {(!full||line.oldLine!==undefined)&&<td className="number">{number(line.oldLine,"before")}</td>}
        {(!full||line.newLine!==undefined)&&<td className="number">{number(line.newLine,"after")}</td>}
        {!full&&<td className="sign">{line.kind==="addition"?"+":line.kind==="deletion"?"-":""}</td>}<td className="code-text">{code(line,line.newLine===undefined?'before':'after')}</td>
      </tr>)}
  </tbody></table>;
}

export function SourceCodeView({target,toolbar,initial=initialCodeReading,onReading,onFeedback,onView}:{target:CodeTarget;toolbar:HTMLElement|null;initial?:CodeReading;
  onReading?:(state:CodeReading)=>void;onFeedback?:(side:"before"|"after",line:number)=>void;onView?:(view:string,anchor?:CodeAnchor)=>void}) {
  const [reading,setReading]=useState(initial);
  const {t}=useUiLanguage();
  const [previous,setPrevious]=useState<CodeReading[]>([]);
  const [search,setSearch]=useState("");const [submitted,setSubmitted]=useState("");
  const [searchPage,setSearchPage]=useState({scope:'',after:0});const [caseSensitive,setCaseSensitive]=useState(false);
  const [line,setLine]=useState("");
  const [message,setMessage]=useState("");const scroll=useRef<HTMLDivElement>(null);
  const syntax=useSourceSyntax(target);
  const position=useRef({top:initial.scrollTop,left:initial.scrollLeft});
  const full=["before","after","staged"].includes(reading.view);
  const searchView=reading.view==="before"?"before":reading.view.startsWith("staged")?"staged":"after";
  const searchScope=JSON.stringify([target.conversationId,target.kind,target.id,target.revision,target.index,target.accessVersion,searchView,submitted,caseSensitive]);
  // Reset before querying: an effect would first send the previous condition's cursor.
  const searchAfter=searchPage.scope===searchScope?searchPage.after:0;
  if(searchPage.scope!==searchScope)setSearchPage({scope:searchScope,after:0});
  function change(value:Partial<CodeReading>,keepPrevious=false) {
    if(keepPrevious)setPrevious(old=>[...old,{...reading,scrollTop:position.current.top,scrollLeft:position.current.left}].slice(-256));else setPrevious([]);
    position.current={top:value.anchor?undefined:0,left:0};
    setReading(old=>({...old,after:0,line:undefined,hunk:undefined,anchor:undefined,scrollTop:undefined,scrollLeft:undefined,...value}));setMessage("");
  }
  const notify=useRef(onReading);notify.current=onReading;
  useEffect(()=>{notify.current?.({...reading,scrollTop:position.current.top,scrollLeft:position.current.left});},[reading]);
  const file=useQuery<SourceFilePage>({
    queryKey:["source-file",target.conversationId,target.kind,target.id,target.revision,target.index,target.accessVersion,reading.view,reading.context,reading.after,reading.line,reading.hunk],
    staleTime:Infinity,gcTime:0,
    queryFn:()=>api(`${codeURL(target)}?revision=${target.revision}&view=${reading.view}&after=${reading.after}&context=${reading.context}${reading.line?`&line=${reading.line}`:""}${reading.hunk!==undefined?`&hunk=${reading.hunk}`:""}`),
  });
  const results=useQuery<SourceSearchPage>({
    queryKey:["source-search",target.conversationId,target.kind,target.id,target.revision,target.index,target.accessVersion,searchView,submitted,searchAfter,caseSensitive],
    enabled:!!submitted,staleTime:Infinity,gcTime:0,
    queryFn:()=>api(`${codeURL(target)}/search?revision=${target.revision}&view=${searchView}&query=${queryString(submitted)}&after=${searchAfter}&case=${caseSensitive?1:0}`),
  });
  useEffect(()=>{
    const view=scroll.current;
    if(!view||!file.data)return;
    view.scrollLeft=position.current.left??0;
    if(position.current.top!==undefined){view.scrollTop=position.current.top;return;}
    const row=view.querySelector<HTMLElement>("[data-code-anchor]");
    if(row)view.scrollTop+=row.getBoundingClientRect().top-view.getBoundingClientRect().top-view.clientHeight/2;
  },[file.data,reading.anchor]);
  function focusFeedback(side:"before"|"after",line:number){
    position.current={top:scroll.current?.scrollTop,left:scroll.current?.scrollLeft};
    const anchor:CodeAnchor={side:side==="after"&&reading.view.startsWith("staged")?"staged":side,line};
    setReading(old=>({...old,anchor}));
    onFeedback?.(side,line);
  }
  function jump(value:number,offset?:number) {
    const anchor:CodeAnchor={side:searchView as CodeSide,line:value,offset};
    if(!full&&onView)onView(searchView,anchor);
    else change({view:searchView,after:offset??0,line:offset===undefined?Math.max(1,value-12):undefined,anchor});
  }
  const page=file.error?undefined:file.data;
  const canReset=reading.after>0||(reading.line??1)>1;
  const rows=page?(full?fullLines(page):diffLines(page)):[];
  function anchorFor(view:string):CodeAnchor|undefined{
    if(view!=="before"&&view!=="after"&&view!=="staged")return undefined;
    if(reading.anchor?.side===view)return reading.anchor;
    const changed=rows.find(row=>row.kind===(view==="before"?"deletion":"addition"));
    const value=view==="before"?changed?.oldLine:changed?.newLine;
    return value===undefined?undefined:{side:view,line:value};
  }
  return <section className="source-code-reader" aria-label="保留版本代码阅读">
    {toolbar&&createPortal(<ReviewPopover label={t("阅读选项")} icon="settings"><div className="source-code-controls">
      <label>显示 <select aria-label="代码版本显示" value={reading.view} onChange={e=>{
        const view=e.target.value;
        if(onView&&(full||view==='before'||view==='after'||view==='staged'))onView(view,anchorFor(view));
        else change({view});
      }}>
        <option value="diff">代码增减</option><option value="before">完整改前文件</option><option value="after">完整改后文件</option>
        {target.file.staged&&<><option value="staged-diff">暂存区增减</option><option value="staged">完整暂存文件</option></>}
      </select></label>
      {!full&&<><label>布局 <select aria-label="差异布局" value={reading.layout} onChange={e=>setReading(old=>({...old,layout:e.target.value as "unified"|"split"}))}>
        <option value="unified">合并</option><option value="split">左右对照</option></select></label>
        <label>上下文 <select aria-label="差异上下文" value={reading.context} onChange={e=>change({context:Number(e.target.value)})}>
          <option value={3}>3 行</option><option value={10}>10 行</option><option value={100000}>全部</option></select></label></>}
      <label><input type="checkbox" checked={!!reading.wrap} onChange={e=>setReading(old=>({...old,wrap:e.target.checked}))}/>换行</label>
      {!full&&onView&&<button onClick={()=>onView(searchView,anchorFor(searchView))}>打开完整源码</button>}
      {full&&<form onSubmit={e=>{e.preventDefault();const value=Number(line);if(!Number.isInteger(value)||value<1){setMessage("请输入有效代码行号");return;}jump(value);}}>
        <input aria-label="跳转代码行" type="number" min={1} value={line} onChange={e=>setLine(e.target.value)} placeholder="行号"/>
        <button>跳转</button>
      </form>}
      {page&&!full&&<small className="source-change-counts">本文件完整差异统计 <span className="added">+{page.additions}</span> <span className="deleted">−{page.deletions}</span>{page.hasMore?" · 当前页为节选":""}</small>}
    </div></ReviewPopover>,toolbar)}
    {full&&<form className="source-code-search" onSubmit={e=>{e.preventDefault();setSubmitted(search.trim());setSearchPage({scope:searchScope,after:0});}}>
      <input aria-label="搜索完整保留文件" value={search} maxLength={256} onChange={e=>setSearch(e.target.value)} placeholder="搜索完整文件"/>
      <label><input type="checkbox" checked={caseSensitive} onChange={e=>setCaseSensitive(e.target.checked)}/>区分大小写</label><button disabled={!search.trim()}>查找</button>
      {submitted&&<button type="button" onClick={()=>setSubmitted("")}>清除搜索</button>}
    </form>}
    {submitted&&<p role="status">搜索“{submitted}” · {searchView==='before'?'改前':searchView==='staged'?'暂存':'改后'}保留版本 · {caseSensitive?'区分':'不区分'}大小写{results.isFetching?' · 正在搜索…':''}</p>}
    {submitted&&results.error&&<p role="alert">{results.error.message} <button type="button" disabled={results.isFetching} onClick={()=>results.refetch()}>重试搜索</button></p>}
    {submitted&&results.data&&!results.error&&<div className="source-search-results" aria-busy={results.isFetching}>
      {!results.data.matches.length&&!results.isFetching&&<p>{searchAfter>0?'后续位置未找到更多匹配。':'所选保留版本中未找到匹配。'}</p>}
      {results.data.matches.map(match=><button key={match.offset} onClick={()=>jump(match.line,match.offset)}>
        第 {match.line} 行：{match.preview}{match.previewClipped?"…（节选）":""}</button>)}
      {results.data.hasMore&&<button type="button" disabled={results.isFetching} onClick={()=>{if(!results.isFetching)setSearchPage({scope:searchScope,after:results.data!.next});}}>继续搜索后续位置</button>}
    </div>}
    {message&&<p role="alert">{message}</p>}
    {file.isPending&&<p role="status">正在读取保留的代码版本…</p>}
    {file.error&&<p role="alert">{file.error.message}。可切换完整前后文件或核对原操作，历史不会用当前文件补齐。</p>}
    {page&&<>
      {full&&<p className="source-page-position">{reading.anchor&&<>定位到第 {reading.anchor.line} 行 · </>}第 {page.lineStart} 行{page.columnStart?`第 ${page.columnStart+1} 列续接`:""}起 · {page.hasMore?"当前仅显示一段，可继续读取":"已到文件末尾"}</p>}
      {!page.text&&!full&&<p>此层没有文本增减；请核对文件格式、路径和模式元数据。</p>}
      <div ref={scroll} className={`source-code-scroll ${reading.wrap?"wrap":""}`} tabIndex={0}
        onScroll={e=>{position.current={top:e.currentTarget.scrollTop,left:e.currentTarget.scrollLeft};notify.current?.({...reading,scrollTop:position.current.top,scrollLeft:position.current.left});}}>
        <CodeRows lines={rows} split={!full&&reading.layout==="split"} full={full} anchor={reading.anchor} onLine={onFeedback?focusFeedback:undefined} syntax={syntax.error?undefined:syntax.data} afterSide={reading.view.startsWith('staged')?'staged':'after'}/>
      </div>
      {(previous.length>0||page.hasMore||canReset)&&<div className="source-page-buttons"><button disabled={!previous.length} onClick={()=>{
        const last=previous.at(-1)!;position.current={top:last.scrollTop,left:last.scrollLeft};setPrevious(old=>old.slice(0,-1));setReading(last);
      }}>上一段代码</button><button disabled={!page.hasMore||file.isFetching} onClick={()=>change({after:page.next},true)}>继续读取代码</button>
      {canReset&&<button onClick={()=>change({after:0})}>返回文件开头</button>}</div>}
      {!full&&!!page.totalHunks&&<nav aria-label="代码改动区块"><button disabled={!page.currentHunk} onClick={()=>change({hunk:page.currentHunk!-1})}>上一处改动</button>
        <span>第 {(page.currentHunk||0)+1} / {page.totalHunks} 处</span>
        <button disabled={(page.currentHunk||0)+1>=page.totalHunks} onClick={()=>change({hunk:(page.currentHunk||0)+1})}>下一处改动</button></nav>}
    </>}
  </section>;
}
