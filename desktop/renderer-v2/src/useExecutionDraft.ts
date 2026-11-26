// 维护下一轮执行设置的未保存草稿，并跟踪在途保存。
// 主要引用方包括 `ExecutionControl.tsx`。
import {useCallback,useEffect,useId,useSyncExternalStore} from 'react';
import type {ExecutionConfig} from './generated';

// `config` 是当前所有者尚未保存的配置。
// `submission` 标识正在保存的提交，异步回执只清理这一次提交；失败时把草稿和错误一起留下。
type Draft={config:ExecutionConfig;saving:boolean;error:string;submission?:symbol};
// Only unsaved or in-flight preferences live here. No disk storage or automatic writes.
const drafts=new Map<string,Draft>();
const listeners=new Map<string,Set<()=>void>>();
function publish(owner:string,value?:Draft){
  if(value)drafts.set(owner,value);else drafts.delete(owner);
  listeners.get(owner)?.forEach(notify=>notify());
}

// `ExecutionControl.tsx` 传入配置基准和保存入口。
// 有所有者键的草稿可以跨组件共用，无键时每个组件持有自己的草稿。
export function useExecutionDraft(ownerKey:string|undefined,initial:ExecutionConfig,onSave:(config:ExecutionConfig)=>Promise<ExecutionConfig>){
  const local=useId();const owner=ownerKey??'local:'+local;
  const subscribe=useCallback((notify:()=>void)=>{
    let group=listeners.get(owner);if(!group){group=new Set();listeners.set(owner,group);}group.add(notify);
    return()=>{group.delete(notify);if(!group.size)listeners.delete(owner);};
  },[owner]);
  const snapshot=useCallback(()=>drafts.get(owner),[owner]);
  const draft=useSyncExternalStore(subscribe,snapshot,snapshot);
  useEffect(()=>()=>{if(!ownerKey)drafts.delete(owner);},[owner,ownerKey]);
  function change(config:ExecutionConfig){
    if(!drafts.get(owner)?.saving)publish(owner,{config,saving:false,error:''});
  }
  function discard(){if(!drafts.get(owner)?.saving)publish(owner);}
  async function save(){
    const current=drafts.get(owner);if(!current||current.saving)return;
    const submission=Symbol();publish(owner,{...current,saving:true,error:'',submission});
    try{
      await onSave(current.config);
      if(drafts.get(owner)?.submission===submission)publish(owner);
    }catch(e){
      const latest=drafts.get(owner);
      if(latest?.submission===submission)publish(owner,{config:latest.config,saving:false,error:e instanceof Error?e.message:String(e)});
    }
  }
  return {form:draft?.config??initial,dirty:!!draft,saving:!!draft?.saving,error:draft?.error??'',change,discard,save};
}
