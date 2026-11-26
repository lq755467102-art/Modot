// 摘要展示指定运行的角色复核、检查事实和证据定位入口。
// 主要引用方包括 `ActionFileReview.tsx` 和 `EvidenceInspector.tsx`。
import {useQuery} from "@tanstack/react-query";
import {api, queryString} from "./client";
import {roleLabel} from "./conversationProjection";
import {UiIcon} from "./UiIcon";
import {callResult} from "./toolActivityGroups";

// 把后端证据状态映射到审查标签；额外次数来自同组事实。
export function AuditStatus({state, count}: {state: string; count?: number}) {
  const symbol = state === "completed" ? "✓" : state === "failed" ? "!" : state === "running" || state === "waiting_approval" ? "…" : "·";
  return <span className="evidence-status" data-state={state}>
    <span aria-hidden="true">{symbol}</span>{count ? count + " 次" : ""}{callResult(state)}
  </span>;
}

// `summary/round` 从工具记录归集动作身份。
// `Fact` 的 `actions` 可打开原证据，`Note.basis` 负责标明说明是测试结果、记录依据还是待核对。
type Fact = {tool: string | null; title: string; state: string; actions: string[]};
type Note = {text: string; actions: string[]; basis: "test" | "record" | "unverified"};
// 按角色汇总该轮的工具记录和说明。
// `independent` 区分独立审查，`hiddenGroups` 告知有多少组折叠或未呈现的活动。
type RoleReview = {
  role: string; phase?: string; reason?: string; count: number; hiddenGroups: number;
  facts: Fact[]; notes: Note[]; independent: boolean; blocked: boolean;
};
// `runId` 是当前被审查的运行，`runs` 供切换目标。
// `incomplete` 提醒摘要尚未涵盖全部记录。
type RoundReview = {runId: string; runs: string[]; runTimes?: Record<string,number|null>; actionLabels?:Record<string,{label:string;startedAt:number|null}>; roles: RoleReview[]; incomplete: boolean};

// 按选中的运行读取角色审查摘要。证据跳转随返回摘要的运行 ID，避免切换后错用当前运行。
export function AuditRoundSummary({base,run,onRun,onEvidence}:{
  base:string;run:string;onRun:(run:string)=>void;
  onEvidence:(action:string,run:string)=>void;
}){
  const query = useQuery<RoundReview>({
    queryKey:["audit-round",base,run],
    queryFn:()=>api(base+"/summary/round"+(run?"?run="+queryString(run):"")),
    refetchInterval:4000,
  });
  if(query.error) return <p role="alert">摘要暂不可核对。 <button onClick={()=>query.refetch()}>重试</button></p>;
  const view = query.data;
  if(!view) return <p role="status">正在整理本轮工具记录…</p>;
  // 原证据有多条时逐条提供动作入口，保留后端记录的动作时间和标签。
  const evidence = (actions:string[], label="查看依据") => {
    if(actions.length===1) return <button onClick={()=>onEvidence(actions[0],view.runId)}>{label}</button>;
    if(!actions.length) return null;
    return <details className="audit-references"><summary>{label} · {actions.length} 次</summary>
      <ul>{actions.map(id=>{
        const item=view.actionLabels?.[id];
        return <li key={id}><button onClick={()=>onEvidence(id,view.runId)}>{item?.label||"查看相关调用"}
          {item?.startedAt?" · "+new Date(item.startedAt*1000).toLocaleTimeString():""}</button></li>;
      })}</ul>
    </details>;
  };
  // 说明文字按依据标出来源，并把引用动作交给统一证据入口。
  const noteLine = (note:Note) => <div>
    <p><small>{note.basis==="test"?"含本人实测":note.basis==="record"?"引用本轮记录":"待核对"}</small> · {note.text}</p>
    {evidence(note.actions)}
  </div>;
  return <section className="audit-round-summary" aria-label="本轮工具摘要">
    {!!view.runs.length&&<label>审查轮次 <select aria-label="审查轮次" value={run} onChange={e=>onRun(e.target.value)}>
      <option value="">最近一轮</option>
      {view.runs.map((id,index)=>{
        const time=view.runTimes?.[id];
        return <option key={id} value={id}>{index===0?"本轮":index===1?"上一轮":"更早一轮"}{time?" · "+new Date(time*1000).toLocaleString():""}</option>;
      })}
    </select></label>}
    {view.incomplete&&<p role="status">目前只显示可核对的记录，部分步骤尚未覆盖。</p>}
    {!view.roles.length&&<p>本轮暂无可汇总的角色记录。</p>}
    {view.roles.map(role=><article className="audit-role-summary" key={role.role} data-audit-role={role.role}>
      <h3 className="evidence-heading"><UiIcon name="activity"/>{role.role==="unknown"?"角色未记录":roleLabel(role.role)}</h3>
      {role.reason&&<p>{role.reason}</p>}
      <ul>{role.facts.map(fact=><li key={fact.actions[0]} className="evidence-heading">
        <strong>{fact.title}</strong>
        <AuditStatus state={fact.state} count={fact.actions.length > 1 ? fact.actions.length : undefined}/>
        {evidence(fact.actions)}
      </li>)}</ul>
      {role.hiddenGroups>0&&<small>其余 {role.hiddenGroups} 组可在全部步骤查看</small>}
      {!!role.notes.length&&noteLine(role.notes[0])}
      {role.notes.length>1&&<details>
        <summary>更多角色说明</summary>
        {noteLine(role.notes[1])}
      </details>}
      {role.role==="reviewer"&&role.phase!=="skipped"&&<p className="audit-review-status">
        {role.phase==="timed_out"?"复核超时。已有检查回执可单独核对。"
          :role.phase==="failed"||role.phase==="paused"||role.phase==="waiting_human"?"复核未完成。已有检查回执可单独核对。"
          :role.independent?"本轮有测试者的实测回执。"
          :role.blocked?"回查受阻，未取得可核对的独立实测回执。"
          :"本轮未记录可核对的独立实测回执。"}
      </p>}
    </article>)}
  </section>;
}
