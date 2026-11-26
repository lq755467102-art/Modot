// 承载会话级偏好，包括执行配置、上下文和消息显示设置。
// 主要引用方包括 `Settings.tsx`。
import {useState} from "react";
import {useQuery,useQueryClient} from "@tanstack/react-query";
import {api} from "./client";
import {ContextPreferenceSettings} from "./ContextInspection";
import {ExecutionControl} from "./ExecutionControl";
import {useUiLanguage} from "./UiLanguage";
import {executorBody,executorConfig} from './executorEntry';
import type {Conversation,ExecutionConfig,ExecutionView} from "./generated";
export type DisplayPreferences={output:string;think:string};
export function ConversationSettings({id,defaults,onDefaults,display,onDisplay}:{id:string;defaults:ExecutionConfig;onDefaults:(value:ExecutionConfig)=>void;display:DisplayPreferences;onDisplay:(value:DisplayPreferences)=>void}){
  const [scope,setScope]=useState<"conversation"|"defaults">("conversation");
  const editDefaults=!id||scope==="defaults";
  const queryClient=useQueryClient();
  const {t}=useUiLanguage();
  const execution=useQuery<ExecutionView>({queryKey:['execution',id],queryFn:()=>api('/api/v2/conversations/threads/'+encodeURIComponent(id)+'/execution'),enabled:!!id,refetchInterval:3000});
  const conversations=useQuery<{threads:Conversation[]}>({queryKey:['conversations'],queryFn:()=>api('/api/v2/conversations/threads')});
  const owner=conversations.data?.threads.find(t=>t.id===id);
  async function save(config:ExecutionConfig){
    const saved=await api<ExecutionView>('/api/v2/conversations/threads/'+encodeURIComponent(id)+'/execution',executorBody(config));
    queryClient.setQueryData(['execution',id],saved);
    await execution.refetch();return saved.next;
  }
  return <>
    <label>输出方式<select id="dp-output" value={display.output} onChange={e=>onDisplay({...display,output:e.target.value})}><option value="type">逐字显示</option><option value="block">整段显示</option></select></label>
    <label>执行过程<select id="dp-think" value={display.think} onChange={e=>onDisplay({...display,think:e.target.value})}><option value="collapse">{t("默认收起")}</option><option value="expand">保持展开</option><option value="hide">隐藏思考</option></select></label>
    <details className="conversation-run-advanced"><summary>高级运行设置</summary>
      {!!id&&<label>{t("应用范围")}<select aria-label={t("应用范围")} value={scope} onChange={e=>setScope(e.target.value as typeof scope)}>
        <option value="conversation">{t("当前会话")}{owner?.title ? " · "+owner.title : ""}</option>
        <option value="defaults">{t("新会话默认值")}</option>
      </select></label>}
      {editDefaults ? <ExecutionControl key="default" draftKey="defaults" initial={defaults} disabled={false} onSave={async config=>{const value=executorConfig(config);onDefaults(value);return value;}}/> : <>
        {execution.error&&<p role="alert">暂时无法读取最新运行设置，已有内容与未保存的选择保留。<button onClick={()=>execution.refetch()}>重试</button></p>}
        {!execution.data&&!execution.error&&<p role="status">读取中…</p>}
        {execution.data&&<ExecutionControl key={id} draftKey={'conversation:'+id} view={execution.data} initial={execution.data.next} disabled={!owner||!!owner.readonlyEvidence||!!owner.subtaskOf||!!owner.archived} onSave={save}/>}</>}
    </details>
    {owner&&!owner.readonlyEvidence&&!owner.subtaskOf&&!owner.archived&&<ContextPreferenceSettings id={id}/>}
  </>;
}
