// 从上下文结果引用定位同一会话的原动作，并按版本分页读取保留证据。
// 主要引用方包括 `ContextInspection.tsx`。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { APIError, api, queryString } from "./client";
import { CodeBlock } from "./CodeBlock";
import type { ContextSource, EvidenceAction, EvidenceOutput } from "./generated";

type ResultReference = ContextSource & { actionId: string };
type ScopedAction = EvidenceAction & { review?: { restricted: boolean } };

// These reads have resource-level checks on both the action and output routes.
export function canReadContextResult(result: ContextSource): result is ResultReference {
  return (result.name === "inspect_code" || result.name === "read_project_resource") &&
    typeof result.actionId === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(result.actionId);
}

// `ContextInspection.tsx` 传入历史快照中的动作引用。
// 先查原动作及当前授权，再按证据修订读取正文；游标仅对原修订有效。
export function ContextResultEvidence({ conversationId, result }: {
  conversationId: string;
  result: ResultReference;
}) {
  const [page, setPage] = useState<{ revision: string; after: number }>();
  const actionId = result.actionId;
  const base = `/api/v2/conversations/threads/${queryString(conversationId)}/evidence/${queryString(actionId)}`;
  const review = useQuery<ScopedAction>({
    queryKey: ["context-result-review", conversationId, actionId],
    queryFn: () => api(base + "?fileOffset=0"),
    refetchInterval: 3000,
    refetchIntervalInBackground: true,
    retry: false,
  });
  const action = review.error ? undefined : review.data;
  const resourceId = (action?.input as { resource_id?: unknown } | undefined)?.resource_id;
  const revision = action?.id === actionId && action.conversationId === conversationId &&
    action.tool === result.name && action.identityKind === "action" && !!action.runId &&
    typeof resourceId === "string" && !!resourceId && action.review?.restricted === false
    ? action.evidenceRevision : undefined;
  const after = page && page.revision === revision ? page.after : 0;
  const output = useQuery<EvidenceOutput>({
    queryKey: ["context-result-output", conversationId, actionId, revision, after],
    queryFn: () => api(base + "/output?format=readable&after=" + after),
    enabled: !!revision,
    retry: false,
  });

  const error = review.error || output.error;
  if (action?.review?.restricted || (error instanceof APIError && error.status === 403)) {
    return <p role="status">原回执已超出当前读取范围。</p>;
  }
  if (error) {
    const missing = error instanceof APIError && (error.status === 404 || error.status === 410);
    return <p role="alert">
      {missing ? "找不到保留的原回执。" : "原回执读取失败。"}
      {" "}<button type="button" onClick={() => void (review.error ? review.refetch() : output.refetch())}>重试</button>
    </p>;
  }
  if (review.isPending) return <p role="status">正在核对原动作…</p>;
  if (!revision) return <p role="status">原动作身份或资料范围无法核对。</p>;
  const entry = output.data;
  if (!entry) return <p role="status">正在读取原回执…</p>;
  if (entry.evidenceRevision !== revision) return <p role="status">原回执版本暂时无法核对。</p>;
  if (typeof entry.bytes !== "number") return <p role="status">原回执正文未保留。</p>;

  return <section className="context-result-evidence" aria-label="原动作回执">
    <p>原回执按当前授权读取。请求内的保留情况见上方快照记录。</p>
    {entry.text ? <CodeBlock text={entry.text} label="原回执正文"/> : <p>这一页没有文本。</p>}
    {!entry.complete && entry.reason && <p role="status">{entry.reason}</p>}
    <div className="context-inspection-controls">
      {entry.hasMore && entry.next > after && <button type="button" disabled={output.isFetching}
        onClick={() => setPage({ revision, after: entry.next })}>继续查看原回执</button>}
      {after > 0 && <button type="button" onClick={() => setPage(undefined)}>回到回执开头</button>}
    </div>
  </section>;
}
