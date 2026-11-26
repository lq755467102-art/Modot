// 承载工作间的文件和命令快速打开界面。
// 主要引用方包括 `WorkroomPage.tsx`。
import { useUiLanguage } from "./UiLanguage";
import {useEffect,useRef,useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {api,queryString} from './client';
import {Dialog} from './Dialog';

export function WorkroomQuickOpen({base,kind,value,onChange,commands,onOpen,onClose}:{base:string;kind:string;value:string;onChange:(value:string)=>void;commands:readonly(readonly[string,()=>unknown])[];onOpen:(path:string,line?:number)=>Promise<boolean>;onClose:()=>void}){
  const { t } = useUiLanguage();
  const [index,setIndex]=useState(0),[opening,setOpening]=useState(false);
  const input=useRef<HTMLInputElement>(null),list=useRef<HTMLDivElement>(null),locked=useRef(false);
  const [query,setQuery]=useState(value);
  useEffect(()=>{
    const timer=window.setTimeout(()=>setQuery(value),150);
    return ()=>window.clearTimeout(timer);
  },[value]);
  const result=useQuery<{matches:{path:string}[]}>({queryKey:['quick-file',base,query],queryFn:()=>api(base+'/search?q='+queryString(query)+'&content=0'),enabled:!!base&&kind==='file'&&!!query&&value===query});
  const items=kind==='command'?commands.filter(([label])=>label.toLowerCase().includes(value.toLowerCase())).map(([label,action])=>({label,detail:'',action:async()=>{action();onClose();}})):
    (value===query?result.data?.matches??[]:[]).map(match=>({label:match.path,detail:'',action:()=>onOpen(match.path)}));
  useEffect(()=>setIndex(0),[kind,value]);
  useEffect(()=>setIndex(old=>Math.min(old,Math.max(0,items.length-1))),[items.length]);
  useEffect(()=>{list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({block:'nearest'});},[index]);
  async function activate(at:number){if(locked.current||!items[at])return;locked.current=true;setOpening(true);try{await items[at].action();}finally{locked.current=false;setOpening(false);}}
  return <div className="modal-backdrop" onClick={onClose}><Dialog className="command-palette" label={kind==='file'?t("快速打开文件"):'工程命令'} onClose={onClose}>
    <div className="quick-open-input"><input ref={input} aria-label={kind==='file'?t("快速打开文件"):t("搜索工程命令")} aria-controls="quick-open-results" aria-activedescendant={items[index]?'quick-open-'+index:undefined} placeholder={kind==='file'?t("输入文件名或相对路径"):t("搜索工程命令")} value={value} onChange={event=>onChange(event.target.value)} onKeyDown={event=>{
      if(event.nativeEvent.isComposing||event.keyCode===229)return;
      if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();setIndex(old=>Math.max(0,Math.min(items.length-1,old+(event.key==='ArrowDown'?1:-1))));}
      if(event.key==='Enter'){event.preventDefault();void activate(index);}
    }}/>{value&&<button type="button" aria-label={t("清除快速打开条件")} onClick={()=>{onChange('');input.current?.focus();}}>{t("清除")}</button>}</div>
    <p className="quick-open-status" role={result.error&&kind==='file'?'alert':'status'}>{opening?t("正在打开…"):kind==='file'?(!value?t("输入文件名或相对路径"):!base?t("正在读取当前工作区…"):value!==query||result.isFetching?t("正在查找文件…"):result.error?t("文件查找失败。"):items.length===200?t("已显示前 200 项，请输入更多字符缩小范围。"):items.length?t("已显示 {count} 个结果",{count:items.length}):t("当前查询没有匹配结果。")):t("匹配 {count} 个工程命令",{count:items.length})}{kind==='file'&&result.error&&<button type="button" onClick={()=>void result.refetch()}>{t("重试查找")}</button>}</p>
    <div ref={list} id="quick-open-results" className="quick-open-results" role="listbox" aria-label={t("快速打开结果")}>{items.map((item,at)=><button type="button" id={'quick-open-'+at} key={at+':'+item.label} role="option" aria-selected={index===at} disabled={opening} onFocus={()=>setIndex(at)} onClick={()=>void activate(at)}><strong>{item.label}</strong>{item.detail&&<small>{item.detail}</small>}</button>)}</div>
  </Dialog></div>;
}
