// 根据发送、运行和提供方状态生成输入区反馈。
// 主要引用方包括 `AgentHome.tsx`。
import type { UiMessage } from "./uiMessages";
import { useUiLanguage } from "./UiLanguage";
import { useMemo, type ReactNode } from "react";
import { parentEvent } from "./conversationProjection";
import type { EventRow, Runtime } from "./generated";
import { UiIcon } from "./UiIcon";

type Status = { kind: "running" | "reconnecting" | "waiting" | "failed"; label: UiMessage };
const providerUnavailable: Status = { kind: "failed", label: "模型服务暂不可用" };
const unconfirmed: Status = { kind: "waiting", label: "运行状态暂未确认" };
const waitingForApproval: Status = { kind: "waiting", label: "等待批准" };
const closingRecoveryStates = new Set(["recovered", "exhausted", "failed", "cancelled"]);
const providerProgress = new Set(["user", "execution_config", "llm", "say", "role_attempt_end"]);

function providerStatus(events: EventRow[] | undefined, runId: string, end?: EventRow): Status | undefined {
  if (!events) return;
  const settled = new Map<string, string>();
  let latestWait: { requestId: string; errorKind: unknown } | undefined;

  for (let index = events.length - 1; index >= 0; index--) {
    const event = events[index];
    if (event.run_id !== runId || !parentEvent(event) || (event.role && event.role !== "executor")) continue;
    if (end && event.seq > end.seq) continue;
    if (event.act === "provider_unavailable") return providerUnavailable;
    if (providerProgress.has(event.act)) break;
    if (event.act !== "provider_recovery" || typeof event.requestId !== "string" || !event.requestId
      || typeof event.state !== "string") continue;

    if (event.state === "waiting") {
      latestWait ??= { requestId: event.requestId, errorKind: event.errorKind };
    } else if (closingRecoveryStates.has(event.state) && !settled.has(event.requestId)) {
      settled.set(event.requestId, event.state);
    }
  }

  // A receipt only settles its own request. A delayed waiting record cannot reopen it.
  if (!latestWait) return;
  const terminal = settled.get(latestWait.requestId);
  if (terminal === "exhausted") return providerUnavailable;
  if (terminal) return;
  if (latestWait.errorKind === "rate_limit") return { kind: "waiting", label: "模型服务限流 · 等待重试" };
  if (latestWait.errorKind === "network") return { kind: "reconnecting", label: "模型连接中断 · 正在重连" };
  return { kind: "reconnecting", label: "模型请求异常 · 正在重试" };
}

type RunFacts = {
  runtime?: Runtime;
  end?: EventRow;
  events?: EventRow[];
  runtimeReadFailed: boolean;
  awaitingApproval: boolean;
};

function presentRunStatus({ runtime, end, events, runtimeReadFailed, awaitingApproval }: RunFacts): Status | undefined {
  const phase = end ? end.status : runtime?.state;
  const runId = runtime?.run_id;

  if (phase === "success" || phase === "completed") return;
  if (phase === "failed" || phase === "error" || phase === "paused") {
    const provider = runId ? providerStatus(events, runId, end) : undefined;
    if (provider?.kind === "failed") return provider;
    return phase === "paused" ? { kind: "waiting", label: "本轮已暂停" } : { kind: "failed", label: "本轮运行失败" };
  }
  if (phase === "interrupted") return { kind: "waiting", label: "本轮已中断" };
  if (phase === "needs_human") return { kind: "waiting", label: "等待回应" };
  if (phase === "stuck") return { kind: "failed", label: "本轮执行受阻" };
  if (phase === "budget") return { kind: "waiting", label: "本轮达到运行上限" };
  if (end) return unconfirmed;

  if (runtimeReadFailed) return runtime?.active ? unconfirmed : undefined;
  if (phase === "waiting_approval") return waitingForApproval;
  if (phase === "unknown" && runId) return unconfirmed;
  if (!runtime?.active) return;
  if (phase === "stopping") return { kind: "waiting", label: "正在停止" };
  if (awaitingApproval) return waitingForApproval;
  if (phase === "preparing") return { kind: "running", label: "准备中" };
  if (phase !== "running" || !runId) return unconfirmed;

  return providerStatus(events, runId) ?? { kind: "running", label: "运行中" };
}

export function ComposerFeedback({ runtime, end, events, runtimeReadFailed, awaitingApproval, sendFeedback, idleHint, error, notice, onLatest, children }: RunFacts & {
  sendFeedback: string;
  idleHint: boolean;
  error?: string;
  notice?: string;
  onLatest?: () => void;
  children?: ReactNode;
}) {
  const { t } = useUiLanguage();
  // The draft can change on every keystroke. Recheck the event window only when its inputs change.
  const status = useMemo(() => presentRunStatus({ runtime, end, events, runtimeReadFailed, awaitingApproval }),
    [runtime, end, events, runtimeReadFailed, awaitingApproval]);
  const message = error || notice;
  return <div className="composer-feedback">
    <div className="composer-feedback-main">
      <span className="composer-run-status" role="status" aria-label={t("本轮运行状态")} data-state={status?.kind} title={status && t(status.label)}>
        {status && <>
          <span className="composer-run-mark" aria-hidden="true">
            {status.kind === "reconnecting" && <UiIcon name="reload" />}
          </span>
          <span>{t(status.label)}</span>
        </>}
      </span>
      <span id="composer-feedback" className="composer-send-feedback" role={error ? "alert" : "status"}
        data-notice={!!message||undefined} title={message}>{message || (status && idleHint ? "" : sendFeedback)}</span>
      {children}
    </div>
    {onLatest
      ? <button type="button" className="composer-latest" onClick={onLatest} aria-label={t("有新内容，返回底部")} title={t("有新内容，返回底部")}>{t("返回底部")}</button>
      : <small className="composer-key-hint">{t("Enter 发送 · Shift+Enter 换行")}</small>}
  </div>;
}
