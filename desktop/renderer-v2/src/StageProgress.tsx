// 按运行身份显示阶段事实、交付进度并连接原始证据入口。
// 主要引用方包括 `AgentHome.tsx`。
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import type { RoleProgress, Runtime, StageOverview } from "./generated";
import {CompletionSummary} from './CompletionSummary';
import type {EvidenceGap} from './checkEvidence';
import {presentRun, roleName, roleStatus} from './runPresentation';
import './l0-run-presentation.css';

// 从已匹配运行的角色进展记录提取状态，同时保留其来源供核对。
function RoleRecord({role,phase,runtime}:{role:string;phase:RoleProgress;runtime:Runtime}){
  const status=roleStatus(role,phase,runtime);
  return <article data-role={role} data-state={status.state}>
    <strong>{roleName(role)} · {status.label}</strong><p>{phase.reason}</p>
    <small>记录到 {phase.attempts} 次启动</small>
    <details><summary>来源事实</summary><pre>{JSON.stringify(phase.source||'缺少关联事件',null,2)}</pre></details>
  </article>;
}
// 按会话与可选运行读后端进展，显示前由 `runPresentation.ts` 核对记录与当前运行。
export function StageProgress({
  conversationId,
  onInspect,
}: {
  conversationId: string;
  onInspect: (run: string, gap?: EvidenceGap) => void;
}) {
  const overview = useQuery<StageOverview>({
    queryKey: ["stage-progress", conversationId],
    queryFn: () =>
      api("/api/v2/conversations/threads/" + conversationId + "/progress"),
    refetchInterval: 2000,
  });
  const data = overview.data;
  const view=presentRun(data,{loading:overview.isPending,error:!!overview.error,empty:!conversationId});
  if(view.state!=='ready')return <section className="stage-progress l0-run-presentation" aria-label="运行进展">
    <p role={view.state==='disconnected'?'alert':'status'}>{view.message}</p>
    {view.previousRunId&&<button onClick={()=>onInspect(view.previousRunId!)}>查看上次已记录轮次</button>}
  </section>;
  if(!data)return null;
  const {run}=view;
  return (
    <section className="stage-progress l0-run-presentation" aria-label="运行、检查与交付记录">
      <h2>{view.historical?'上次运行记录':'本轮进展'}</h2>
      {run.goal && (
        <p>
          目标{run.goalExcerpt ? "（节选）" : ""}：{run.goal}
        </p>
      )}
      {!data.completion&&<p>{data.summary?.text || "概览暂不可用，请核对角色事实。"}</p>}
      <CompletionSummary data={data.completion} onInspectGap={gap=>onInspect(run.id,gap)}/>
      {data.evidenceIncomplete && (
        <p role="status">
          概览包含未补齐或尚未逐项核验的记录，请按需审查具体步骤。
        </p>
      )}
      {data.previousSummaryStale && (
        <small>旧摘要已过期，现已按新记录更新。</small>
      )}
      <div className="stage-roles">
        {view.roles.map(([role,phase])=><RoleRecord key={role} role={role} phase={phase} runtime={view.runtime}/>)}
      </div>
      {!!view.inactive.length&&<details className="l0-inactive-roles"><summary>查看未启用角色 · {view.inactive.length}</summary>
        <div className="stage-roles">{view.inactive.map(([role,phase])=><RoleRecord key={role} role={role} phase={phase} runtime={view.runtime}/>)}</div>
      </details>}
      {run.blocker && <p role="status">当前阻塞：{run.blocker}</p>}
      {data.runtime.state === "stopping" && (
        <p role="status">已请求停止，仍在等待当前步骤结束或确认终止。</p>
      )}
      <p>
        记录到 {run.actionCount} 个工具动作，{run.failedActions} 个失败、
        {run.unknownActions} 个结果未知。阶段结束不等于检查通过或你已验收。
      </p>
      {data.checksMissing && !data.completion && <p>仍有检查未完成或证据待核对。</p>}
      <button onClick={() => onInspect(run.id)}>{view.historical?'查看该轮证据':'查看本轮证据'}</button>
      <details><summary>查看进展记录范围</summary>
        <small>记录 {data.evidenceRange.fromPosition}—{data.evidenceRange.throughPosition} · {run.id}</small>
      </details>
      {!!run.judgments.length && (
        <details>
          <summary>Agent 的判断（原文节选，不能代替验证）</summary>
          {run.judgments.map((j, i) => (
            <blockquote key={i}>
              <small>
                {roleName(j.role)}
              </small>
              <p>{j.text}</p>
            </blockquote>
          ))}
        </details>
      )}
    </section>
  );
}
