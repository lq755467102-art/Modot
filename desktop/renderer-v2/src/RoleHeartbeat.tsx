// 把当前轮次有证据支持的角色状态展示在会话页。
// 主要引用方包括 `AgentHome.tsx`。
import {useQuery} from '@tanstack/react-query';
import {api} from './client';
import type {EventRow,Runtime,StageOverview} from './generated';
import {presentRun, roleName, roleStatus} from './runPresentation';
import './l0-run-presentation.css';

const endLabels: Record<string, string> = {
  success: "执行已结束", completed: "执行已结束", paused: "已暂停", needs_human: "待回应",
  error: "未成功结束", failed: "未成功结束", interrupted: "执行已中断", stuck: "执行受阻", budget: "已达到运行上限",
};

export function RoleHeartbeat({conversationId,runtime,summaryRole,summaryRunId,awaitingApproval,nativeEnd}:{
  conversationId:string;runtime?:Runtime;summaryRole?:string;summaryRunId?:string;awaitingApproval?:boolean;nativeEnd?:EventRow;
}){
  // Share the existing progress query with the detailed review; no new endpoint or model calls.
  const overview=useQuery<StageOverview>({queryKey:['stage-progress',conversationId],
    queryFn:()=>api('/api/v2/conversations/threads/'+conversationId+'/progress'),
    enabled:!!conversationId,refetchIntervalInBackground:true,
    refetchInterval:()=>runtime?.active?500:2000});
  const data=overview.data;
  const view=presentRun(data,{loading:overview.isPending,error:!!overview.error,empty:!conversationId,
    currentRuntime:runtime,requireCurrentRuntime:true});
  const outcome = nativeEnd?.run_id && nativeEnd.run_id === runtime?.run_id ? nativeEnd.status || "unknown" : undefined;
  const statusClass = "role-heartbeats l0-run-presentation" + (runtime?.active ? " live" : "");
  // A matching end event may arrive before both progress queries catch up.
  if (outcome && (view.state !== "ready" || view.runtime.active || !view.run.outcome)) {
    return <div id="state" className={statusClass} role="status" aria-label="本轮进展" data-run-outcome={outcome}>
      {endLabels[outcome] || "执行已结束 · 状态待核对"}
    </div>;
  }
  if(view.state==='idle')return null;
  if(view.state!=='ready')return <div id="state" className={statusClass} role="status" aria-label="本轮进展" data-progress-state={view.state}>{view.message}</div>;
  const {run}=view;
  const displayMatches=summaryRunId===run.id&&run.crew!=='l0'&&!run.parallelRoles;
  const summaryIndex=view.roles.findIndex(([role])=>role===summaryRole);
  const candidateLabels:Record<string,string>={queued:'待复核',running:'复核中',failed:'复核未完成',paused:'已暂停',timed_out:'复核超时'};
  return <div id="state" className={statusClass} role="group" aria-label="本轮角色状态" data-run-outcome={outcome}>
    {view.roles.map(([role,phase],index)=>{
      const {state,label}=roleStatus(role,phase,view.runtime);
      const displayAllowed=displayMatches&&['running','completed','waiting_human'].includes(state);
      const note=run.crew!=='l0'&&role==='executor'&&awaitingApproval&&runtime?.active&&['running','waiting_human'].includes(state)?'等待安全批准':
        displayAllowed&&role===summaryRole?'收尾显示中':
        displayAllowed&&summaryIndex>=0&&index>summaryIndex?'正文等待展示':'';
      return <span key={role} className="role-heartbeat" data-role={role} data-state={state}
        aria-label={roleName(role)+'：'+label+(note?'；'+note:'')} title={phase.reason||label}>
        <i aria-hidden="true"/><span>{roleName(role)}</span><small>{label}</small>
        {note&&<small className="l0-display-note">{note}</small>}
      </span>;
    })}
    {run.parallelRoles&&run.latestCandidate&&<small aria-label="最近候选复核状态">最近候选 · {run.latestCandidate.state==='completed'?
      (run.latestCandidate.verdict==='pass'?'复核通过':run.latestCandidate.verdict==='issues'?'发现问题':'证据不足'):(candidateLabels[String(run.latestCandidate.state)]||'复核状态待核对')}</small>}
  </div>;
}
