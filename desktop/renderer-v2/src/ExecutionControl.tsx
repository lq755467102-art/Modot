// 编辑会话下一次运行所用的执行与预算配置，同时显示当前运行记录。
// 主要引用方包括 `ApplicationSettings.tsx` 和 `ConversationSettings.tsx`。

import {useExecutionDraft} from "./useExecutionDraft";
import type {ExecutionConfig,ExecutionView} from "./generated";
import './l0-run-presentation.css';
import {executorConfig,needsExecutor} from './executorEntry';
export const conversationCrews = [
  ["l0", "执行者"],
] as const;

export const defaultExecution:ExecutionConfig={revision:1,crew:'l0',maxTurns:0,modelCalls:0,charter:'off'};
const guards={unlimited:{name:'不限调用次数',maxTurns:0,modelCalls:0},short:{name:'较短',maxTurns:10,modelCalls:16},standard:{name:'标准',maxTurns:20,modelCalls:32},extended:{name:'较长',maxTurns:40,modelCalls:120}};
const crewLabel=(crew:ExecutionConfig['crew'])=>({l0:'L0 仅执行',l4:'持续委托'}[crew]);
const limits=(config:ExecutionConfig)=>(config.maxTurns>0?config.maxTurns+' 轮':'轮数不限')+' · '+(config.modelCalls>0?config.modelCalls+' 次调用':'调用次数不限');
// 使用 `useExecutionDraft.ts` 保存下一次运行偏好；当前运行状态只从后端运行记录读出。
export function ExecutionControl({view,initial,onSave,disabled,draftKey}:{view?:ExecutionView;initial:ExecutionConfig;onSave:(config:ExecutionConfig)=>Promise<ExecutionConfig>;disabled:boolean;draftKey?:string}){
  const {form:recorded,dirty,saving,error,change,discard,save}=useExecutionDraft(draftKey,initial,config=>onSave(executorConfig(config)));
  const form=executorConfig(recorded),migration=needsExecutor(initial);
  const stale=!!view&&form.revision!==view.next.revision;
  const selected=Object.entries(guards).find(([,p])=>p.maxTurns===form.maxTurns&&p.modelCalls===form.modelCalls)?.[0]||'custom';
  const current=view?.current;
  return <section className="execution-control l0-run-presentation">
    {current&&<details className="l0-recorded-config"><summary>已记录运行配置</summary>
      {current?.config?<><p>{crewLabel(current.config.crew)} · {limits(current.config)}</p>
        {(current.config.backgroundPlanner||current.config.parallelRoles)&&<p>记录的后台偏好：{current.config.backgroundPlanner?'后台规划 ':''}{current.config.parallelRoles?'独立复核':''}</p>}
        <small>来自该次运行保存的配置，后续偏好在下方单独保存。</small></>:
        <p>{current?.reason||'未提供运行配置；下一轮偏好不代表已经运行的设置。'}</p>}
    </details>}
    <fieldset disabled={disabled||saving}>
      <legend>{view?'当前会话下一轮偏好':'新对话默认偏好'}</legend>
      <p className="l0-next-config">{crewLabel(form.crew)} · {limits(form)}</p>
      <label>任务推进范围<select aria-label="任务推进范围" value={selected} onChange={e=>{const p=guards[e.target.value as keyof typeof guards];if(p)change({...form,maxTurns:p.maxTurns,modelCalls:p.modelCalls});}}>
        {selected==='custom'&&<option value="custom">保留现有设置</option>}
        {Object.entries(guards).map(([id,p])=><option key={id} value={id}>{p.name}</option>)}
      </select></label>
      <label>委派存续方式<select aria-label="委派存续方式" value={form.workerContinuation??'parent-run'} onChange={e=>change({...form,workerContinuation:e.target.value as ExecutionConfig['workerContinuation']})}>
        <option value="parent-run">随本轮结束</option>
        <option value="manual">后台完成，下次继续时收回</option>
        <option value="wake">后台完成，有新成果时唤醒</option>
      </select></label>
      {form.workerContinuation==='wake'&&(form.maxTurns>0||form.modelCalls>0)&&<small>已设置轮数或调用额度时，成果留待手动续接。</small>}
      {migration&&<p role="status">下一次新运行使用执行者。保存偏好或发送新任务时会核对版本，已记录运行保持原配置。</p>}
      <small>{view?'保存仅影响本会话后续运行。':'保存用于之后创建的新对话。'}轮数与调用次数的 0 表示不限；当前运行保持原配置。</small>
      {stale&&<p role="alert">设置已变化，未保存的选择已保留。请重新载入后核对。</p>}
      {dirty&&<p role="status">{saving?'正在保存运行偏好…':'未保存的选择在本次使用期间保留，保存后才生效。'}</p>}
      <button type="button" disabled={(!dirty&&!migration)||stale} onClick={()=>{if(!dirty)change(form);void save();}}>保存运行偏好</button>
      {dirty&&<button type="button" onClick={discard}>{stale?'重新载入设置':'放弃未保存的选择'}</button>}
    </fieldset>
    {error&&<p role="alert">{error}</p>}
    {!!current?.reviewerReserve&&<small>已记录运行中，测试者已用 {current.byRole?.reviewer??0} / {current.reviewerReserve} 次独立调用。</small>}
    {current?.state==='exhausted'&&<p role="status">已记录运行有角色达到调用上限。</p>}
  </section>;
}
