// 展示后端归集的完成状态和待核对事项，沿原回执定位检查证据。
// 主要引用方包括 `StageProgress.tsx`。
import type {CompletionGap, CompletionStatus} from './generated';
import {canResolveGap, type EvidenceGap} from './checkEvidence';
import './l0-run-presentation.css';

export function CompletionSummary({data,compact=false,onInspectGap}:{
  data?:CompletionStatus|null;compact?:boolean;onInspectGap?:(gap:EvidenceGap)=>void;
}) {
  if(!data)return <section className="completion-summary l0-run-presentation" aria-label="交付记录" role="status">
    <p>尚未提供交付状态，可查看本轮证据。</p>
  </section>;
  const pending=compact?data.pending.slice(0,3):data.pending;
  const gapRow = (item:CompletionGap, index:number) => {
    const label = item.kind === 'check' ? '查看检查日志' : '查看原始证据';
    return <li key={item.kind+item.id+index}>
      {item.text}
      {onInspectGap && canResolveGap(item) && <>{' '}
        <button type="button" onClick={()=>onInspectGap({kind:item.kind, id:item.id})}
          aria-label={label + ' · ' + item.text}>{label}</button>
      </>}
    </li>;
  };
  const facts=data.facts;
  return <section className="completion-summary l0-run-presentation" aria-label="交付状态与剩余事项" role="status">
    {facts?<dl className="l0-completion-facts">
      <dt>运行状态</dt><dd>{facts.run}</dd>
      <dt>检查结果</dt><dd>{facts.checks}</dd>
      <dt>需求验收</dt><dd>{facts.acceptance}</dd>
    </dl>:<>
      <strong>{data.label}</strong>
      <p className="l0-check-summary"><span>检查记录：</span>{data.checkSummary||'尚无检查摘要'}</p>
      {!!data.completed.length&&!compact&&<p>已记录完成项：{data.completed.join('；')}</p>}
    </>}
    {!!data.pending.length&&<><p>待核对与未完成事项 · {data.pending.length} 项：</p>
      <ul>{pending.map(gapRow)}</ul>
      {compact&&data.pending.length>3&&<details><summary>查看其余 {data.pending.length-3} 项</summary>
        <ul>{data.pending.slice(3).map(gapRow)}</ul>
      </details>}
    </>}
  </section>;
}
