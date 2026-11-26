// 按缺项绑定的动作和版本打开只读证据。每页都使用当前授权读取。
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { APIError, api, queryString } from "./client";
import { CodeBlock } from "./CodeBlock";
import { matchesCheckEvidence, readRecordedCheckLog, type EvidenceGap } from "./checkEvidence";
import type { EvidenceAction, EvidenceOutput, EvidenceTarget, EvidenceTargetResult } from "./generated";
import { toolTitle } from "./toolActivityGroups";

const notices: Record<string, string> = {
  evidence_unbound: "这一项没有关联可核对的本轮原证据。",
  evidence_ambiguous: "这一项关联了多条原证据。",
  evidence_missing: "原证据未保留或已无法读取。",
  evidence_indexing: "原证据仍在建立索引。",
  evidence_unverifiable: "原证据的关联暂时无法核对。",
  evidence_changed: "原证据的回执版本已变化，请重新选择。",
  wrong_source: "原证据的会话、轮次或适用版本无法核对。",
  resource_denied: "原证据已不在当前读取范围内。",
};

function wrongSource() {
  return new APIError(409, "wrong_source", notices.wrong_source);
}

async function resolveTarget(conversationId: string, runId: string, gap: EvidenceGap, signal: AbortSignal) {
  const base = "/api/v2/conversations/threads/" + queryString(conversationId) + "/evidence";
  const result = await api<EvidenceTargetResult>(base + "/targets/resolve?" + new URLSearchParams({runId, kind:gap.kind, id:gap.id}));
  signal.throwIfAborted();
  if (result.conversationId !== conversationId || result.runId !== runId || result.kind !== gap.kind || result.id !== gap.id) {
    throw wrongSource();
  }
  const target = result.target;
  if (target) {
    const version = target.version;
    const pinned = version.kind === "code" ? !!version.revision : !!version.versionId && !!version.hash;
    if (target.conversationId !== conversationId || target.runId !== runId || !target.actionId ||
        !target.evidenceRevision || !version.id || !pinned ||
        (target.kind === "check-log" && (!target.operationId || !target.workspaceId || version.kind !== "code"))) {
      throw wrongSource();
    }
  }
  return result;
}

async function readTargetPage(target: EvidenceTarget, after: number, signal: AbortSignal) {
  const {conversationId, runId, actionId, evidenceRevision, operationId, workspaceId, version} = target;
  if (target.kind === "check-log") {
    const output = await readRecordedCheckLog({
      conversationId, runId, operationId: operationId!, target: {actionId, revision:version.revision!, workspaceId:workspaceId!},
      evidenceRevision, after, signal, pinRevision:true,
    });
    return {title:"检查日志", output};
  }

  const base = "/api/v2/conversations/threads/" + queryString(conversationId) + "/evidence/" + queryString(actionId);
  const action = await api<EvidenceAction & {review?: {restricted: boolean}; displayTitle?: string}>(base + "?fileOffset=0");
  signal.throwIfAborted();
  if (action.review?.restricted !== false) throw new APIError(403, "resource_denied", notices.resource_denied);
  if (action.id !== actionId || action.conversationId !== conversationId || action.runId !== runId ||
      action.identityKind !== "action" || (operationId && !matchesCheckEvidence(action, conversationId, runId, operationId, actionId))) {
    throw wrongSource();
  }
  if (action.evidenceRevision !== evidenceRevision) throw new APIError(409, "evidence_changed", notices.evidence_changed);
  const output = await api<EvidenceOutput>(base + "/output?format=readable&after=" + after);
  signal.throwIfAborted();
  if (output.evidenceRevision !== evidenceRevision) throw new APIError(409, "evidence_changed", notices.evidence_changed);
  if (typeof output.bytes !== "number") throw new APIError(410, "evidence_missing", notices.evidence_missing);
  return {title:action.displayTitle || toolTitle(action.tool), output};
}

export function GapEvidence({conversationId, runId, gap, onUnavailable}: {
  conversationId: string; runId: string; gap: EvidenceGap;
  onUnavailable: (notice: string, endpointMissing: boolean) => void;
}) {
  const lookup = useQuery({
    queryKey: ["gap-evidence-target", conversationId, runId, gap.kind, gap.id],
    queryFn: ({signal}) => resolveTarget(conversationId, runId, gap, signal),
    retry: false,
  });
  const target = lookup.isSuccess ? lookup.data.target : null;
  const [after, setAfter] = useState(0);
  const page = useQuery({
    queryKey: ["gap-evidence-page", conversationId, runId, target, after],
    enabled: !!target,
    queryFn: ({signal}) => readTargetPage(target!, after, signal),
    retry: false,
    refetchInterval: 3000,
    refetchIntervalInBackground: true,
  });
  const endpointMissing = lookup.error instanceof APIError && lookup.error.status === 404 && lookup.error.code === "not_found";
  const error = lookup.error || page.error;
  const reason = error instanceof APIError ? error.status === 403 ? "resource_denied" : error.code : lookup.data?.reason;
  const unavailable = error || (lookup.isSuccess && !target);
  const notice = unavailable ? (reason && Object.hasOwn(notices, reason) ? notices[reason] : "暂时无法定位这一项的原证据。") : "";
  useEffect(() => {
    if (notice) onUnavailable(notice, endpointMissing);
  }, [notice, endpointMissing, onUnavailable]);

  if (notice) return <p role="status">{notice}</p>;
  if (!target || !page.data) return <p role="status">正在核对原证据…</p>;
  const {title, output} = page.data;
  const {version} = target;
  return <section className="evidence-card-body" aria-label={target.kind === "check-log" ? "原检查日志" : "原动作回执"}>
    <h3>{title}</h3>
    {lookup.data?.archived && <p>归档会话</p>}
    {version.kind === "code" ? <p>当时的源码版本 <code>{version.revision}</code></p>
      : <p>当时的成果版本 <code>{version.versionId}</code> · <code>{version.hash}</code></p>}
    {output.text ? <CodeBlock text={output.text} label={title}/> : <p>这一页没有文本。</p>}
    {!output.complete && output.reason && <p role="status">{output.reason}</p>}
    {output.hasMore && Number.isSafeInteger(output.next) && output.next > after &&
      <button type="button" disabled={page.isFetching} onClick={() => setAfter(output.next)}>继续查看</button>}
    {after > 0 && <button type="button" onClick={() => setAfter(0)}>回到输出开头</button>}
  </section>;
}
