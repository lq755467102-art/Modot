// 把检查编号解析为可核对的动作或进程，再查询对应记录的日志。
// 主要引用方包括 `CompletionSummary.tsx` 和 `EvidenceInspector.tsx`。
import {APIError, api, queryString} from "./client";
import type {ArtifactSource, CodeOperation, CodeResult, CompletionGap, EvidenceAction, EvidenceOutput, ProcessRun} from "./generated";

export type EvidenceGap = Pick<CompletionGap, "kind" | "id">;
const resolvableKinds = new Set(["check", "requirement", "coverage", "review", "finding", "defect"]);

export function canResolveGap(gap: EvidenceGap) {
  // The completion summary also uses a fixed marker when the acceptance record has no identity.
  return resolvableKinds.has(gap.kind) && gap.id.length > 0 && gap.id.length <= 128 &&
    !(gap.kind === "check" && gap.id === "acceptance");
}

// `code-operations` 由后端代码服务分页。翻页时直接传回 `nextBefore`。
type OperationPage = {
  operations: CodeOperation[];
  hasMore: boolean;
  nextBefore: number | null;
};
const operationIdPattern = /^[0-9a-f]{32}$/;

// 用原检查回执解析出的三元组。
// `actionId` 定位动作，`revision` 约束检查源码版本，`workspaceId` 校验进程归属。
type CheckTarget = {actionId: string; revision: string | null; workspaceId: string};
type CheckAction = EvidenceAction & {review?: {restricted: boolean}};
type CheckProcess = ProcessRun & {conversationId?: string; operationId?: string; source?: ArtifactSource};

export function checkOperationCandidate(gap: EvidenceGap) {
  // A resource ID can have the same form. Only a matching operation can resolve it.
  return gap.kind === "check" && operationIdPattern.test(gap.id);
}

// 完成摘要传入后端缺口 ID。
// 先由 `client.ts` 查同会话代码操作，再核对轮次和动作身份，只有格式匹配还不能直接打开日志。
export async function findCheckEvidence(conversationId: string, runId: string, operationId: string, signal: AbortSignal): Promise<CheckTarget | null> {
  if (!runId || !operationIdPattern.test(operationId)) return null;
  const base = `/api/v2/conversations/threads/${queryString(conversationId)}/code-operations`;
  let before: number | null = null;
  while (true) {
    signal.throwIfAborted();
    const page: OperationPage = await api(base + (before === null ? "" : "?before=" + before));
    signal.throwIfAborted();
    const operation = page.operations.find(row => row.id === operationId);
    if (operation) {
      const {source} = operation;
      if (operation.operation !== "check" || source.threadId !== conversationId || source.runId !== runId || !source.actionId) return null;
      if (source.sessionId && source.sessionId !== conversationId) return null;
      return {actionId: source.actionId, revision: operation.beforeRevision, workspaceId: operation.workspaceId};
    }
    if (!page.hasMore) return null;
    const next = page.nextBefore;
    if (next === null || !Number.isSafeInteger(next) || next < 1 || (before !== null && next >= before)) return null;
    before = next;
  }
}

export function matchesCheckEvidence(action: EvidenceAction, conversationId: string, runId: string, operationId: string, actionId: string) {
  return action.id === actionId && action.identityKind === "action" && action.tool === "run_project_check"
    && action.conversationId === conversationId && action.runId === runId
    && action.impact?.operationId === operationId;
}

export async function readRecordedCheckLog({conversationId, runId, operationId, target, evidenceRevision, after, signal, pinRevision = false}: {
  conversationId: string; runId: string; operationId: string; target: CheckTarget;
  evidenceRevision: string; after: number; signal: AbortSignal; pinRevision?: boolean;
}): Promise<EvidenceOutput> {
  const thread = `/api/v2/conversations/threads/${queryString(conversationId)}`;
  const detail = `${thread}/evidence/${queryString(target.actionId)}?fileOffset=0`;
  async function confirmAction() {
    signal.throwIfAborted();
    const action = await api<CheckAction>(detail);
    signal.throwIfAborted();
    if (action.review?.restricted !== false) {
      throw new APIError(403, "resource_denied", "原检查的日志已不在当前读取范围内。");
    }
    if (!matchesCheckEvidence(action, conversationId, runId, operationId, target.actionId)) {
      throw new APIError(409, "wrong_source", "原检查的动作与轮次暂时无法核对。");
    }
    if (action.evidenceRevision !== evidenceRevision) {
      throw new APIError(409, "evidence_changed", "原检查的日志版本暂时无法核对。");
    }
    return action;
  }
  // Completed check receipts can omit the text. Read the process owned by the original check,
  // rechecking the scoped action on both sides so a revoked page never reaches the UI cache.
  const action = await confirmAction();
  const processId = (action.impact?.process as {id?: unknown} | undefined)?.id;
  if (typeof processId !== "string" || !processId) {
    throw new APIError(410, "evidence_missing", "原检查没有可核对的进程日志。");
  }
  const params = new URLSearchParams({after: String(after)});
  if (pinRevision) params.set("evidenceRevision", evidenceRevision);
  const log = await api<CodeResult & {process: CheckProcess}>(`${thread}/code-operations/${queryString(operationId)}/output?${params}`);
  const process = log.process;
  if (process.id !== processId || process.kind !== "check" || process.workspaceId !== target.workspaceId ||
      process.conversationId !== conversationId || process.operationId !== operationId ||
      process.source?.sessionId !== conversationId || process.source?.runId !== runId || process.source?.actionId !== target.actionId ||
      (target.revision && process.revision !== target.revision)) {
    throw new APIError(409, "wrong_source", "原检查的进程与源码版本暂时无法核对。");
  }
  if (!log.text || log.next === undefined || log.next <= after) {
    throw new APIError(410, "evidence_missing", "原检查的日志未保留或暂不可读取。");
  }
  await confirmAction();
  return {
    text: log.text,
    next: log.next,
    hasMore: log.hasMore === true,
    complete: log.truncated !== true,
    reason: log.truncated ? log.outputNotice || "这次检查的日志只保留了部分内容。" : undefined,
    evidenceRevision,
  };
}
