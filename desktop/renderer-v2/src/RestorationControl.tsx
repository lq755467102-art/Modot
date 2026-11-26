// 展示单文件恢复预览、计划授权和执行后的核对状态。
// 主要引用方包括 `AgentHome.tsx` 和 `EvidenceInspector.tsx`。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, loadJSON, saveJSON } from "./client";
import type {
  RestorationPlan,
  RestorationBaseline,
  RestorationPage,
} from "./generated";

const labels: Record<RestorationPlan["state"], string> = {
  draft: "等待你核对授权",
  authorized: "已授权，尚未确认执行",
  applying: "恢复处理中，结果待核对",
  completed: "恢复已执行并核对",
  failed: "恢复未能执行",
  unknown: "恢复结果未知，不会自动重放",
  expired: "授权已过期，请重新预览",
  revoked: "恢复授权已撤销",
};
export function RestorationHistory({
  conversationId,
  onDelegate,
}: {
  conversationId: string;
  onDelegate: (plan: RestorationPlan) => Promise<void>;
}) {
  const [selected, setSelected] = useState(""),
    [before, setBefore] = useState<number | null>(null);
  const query = useQuery<RestorationPage>({
    queryKey: ["restoration-plans", conversationId, before],
    queryFn: () =>
      api(
        `/api/v2/conversations/threads/${conversationId}/restorations` +
          (before ? "?before=" + before : ""),
      ),
    refetchInterval: 3000,
  });
  if (!query.error && !query.data?.plans.length) return null;
  const active = query.data?.plans.find((p) => p.id === selected);
  return (
    <details className="restoration-history">
      <summary>恢复预览与结果</summary>
      {query.error && <p role="alert">{query.error.message}</p>}
      <ul>
        {query.data?.plans.map((p) => (
          <li key={p.id}>
            {p.path} · {labels[p.state as RestorationPlan["state"]] || p.state}
            <small> · {p.actor === "agent" ? "Agent准备" : "用户准备"}</small>
            {p.error && <p role="alert">{p.error}</p>}
            <button
              disabled={!p.sourceOperationId}
              onClick={() => setSelected(p.id)}
            >
              核对这个恢复计划
            </button>
          </li>
        ))}
      </ul>
      {active?.sourceOperationId && (
        <RestorationControl
          key={active.id}
          conversationId={conversationId}
          operationId={active.sourceOperationId}
          initialPlanId={active.id}
          onDelegate={onDelegate}
        />
      )}
      {query.data?.hasMore && (
        <button onClick={() => setBefore(query.data!.nextBefore)}>
          更早恢复计划
        </button>
      )}
      {before && <button onClick={() => setBefore(null)}>最新恢复计划</button>}
    </details>
  );
}
export function RestorationControl({
  conversationId,
  operationId,
  onDelegate,
  initialPlanId = "",
}: {
  conversationId: string;
  operationId: string;
  onDelegate: (plan: RestorationPlan) => Promise<void>;
  initialPlanId?: string;
}) {
  const key = "restoration-plan-" + conversationId + "-" + operationId;
  const [expanded, setExpanded] = useState(!!initialPlanId);
  const [planId, setPlanId] = useState(
      () => initialPlanId || loadJSON<string>(key, ""),
    ),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [advanced, setAdvanced] = useState(false),
    [partial, setPartial] = useState(false),
    [showBaseline, setShowBaseline] = useState(false),
    [after, setAfter] = useState(0);
  const base = `/api/v2/conversations/threads/${conversationId}/restorations`;
  const query = useQuery<RestorationPlan>({
    queryKey: ["restoration", conversationId, planId],
    queryFn: () => api(base + "/" + planId),
    enabled: !!planId,
    refetchInterval: planId ? 3000 : false,
  });
  const baseline = useQuery<RestorationBaseline>({
    queryKey: ["restoration-baseline", conversationId, planId, after],
    queryFn: () => api(base + "/" + planId + "/baseline?after=" + after),
    enabled: !!planId && showBaseline,
  });
  const plan = query.error ? undefined : query.data;
  async function prepare() {
    setBusy(true);
    setError("");
    try {
      const created = await api<RestorationPlan>(base, { operationId });
      setPlanId(created.id);
      saveJSON(key, created.id);
      setAdvanced(false);
      setPartial(false);
      setShowBaseline(false);
      setAfter(0);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  async function delegate() {
    if (!plan) return;
    setBusy(true);
    setError("");
    try {
      const authorized = await api<RestorationPlan>(
        base + "/" + plan.id + "/approve",
        {
          confirm: true,
          expectedPlanRevision: plan.planRevision,
          acknowledgeProjectAdvance: advanced,
          acknowledgePartialDiff: partial,
        },
      );
      await query.refetch();
      await onDelegate(authorized);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <details
      className="restoration-control"
      open={expanded}
      onToggle={(e) => setExpanded(e.currentTarget.open)}
    >
      <summary>恢复到这次写入之前</summary>
      <p>
        仅恢复有可核对备份的本地已跟踪文件。先预览影响，明确授权后再交给
        Agent；不会删除新建或未跟踪文件。
      </p>
      <button
        disabled={
          busy ||
          plan?.state === "authorized" ||
          plan?.state === "applying" ||
          plan?.state === "unknown"
        }
        onClick={prepare}
      >
        准备恢复影响预览
      </button>
      {error && <p role="alert">{error}</p>}
      {query.error && <p role="alert">{query.error.message}</p>}
      {plan && (
        <section aria-label="受控恢复预览">
          <strong>{labels[plan.state]}</strong>
          <p>
            文件：{plan.path} · 分支 {plan.branch}
          </p>
          <pre>{plan.diff || "没有文本差异；仍将按已核验字节基线处理。"}</pre>
          {plan.diffTruncated && (
            <p role="status">
              差异仅展示前300行/有限文字；授权会恢复整个文件，可先分页查看基线。
            </p>
          )}
          <details>
            <summary>版本与来源</summary>
            <p>恢复计划 {plan.id}</p>
            <p>原写入 {plan.sourceOperationId}</p>
            <p>当前核对 SHA256 {plan.currentRevision}</p>
            <p>将恢复 SHA256 {plan.restoreRevision}</p>
            <p>
              最近读取的文件 SHA256 {plan.observedFileRevision || "无法核对"}
            </p>
          </details>
          {plan.verification === "unavailable" && (
            <p role="status">
              当前文件或读取权限无法核对；历史预览保留，不代表当前状态。
            </p>
          )}
          {plan.fileMatches === false && (
            <p role="status">
              当前文件已与此计划记录的版本不同，请核对后续改动。
            </p>
          )}
          <p>{plan.warning}</p>
          <p>{plan.writeMethod}</p>
          <button onClick={() => setShowBaseline(!showBaseline)}>
            分页查看备份基线
          </button>
          {showBaseline && (
            <div aria-label="恢复基线正文">
              {baseline.error && <p role="alert">{baseline.error.message}</p>}
              {baseline.data && (
                <>
                  <small>历史基线 SHA256 {baseline.data.hash}</small>
                  <pre>{baseline.data.text}</pre>
                  {baseline.data.hasMore && (
                    <button onClick={() => setAfter(baseline.data!.next)}>
                      继续查看基线
                    </button>
                  )}
                  {after > 0 && (
                    <button onClick={() => setAfter(0)}>基线开头</button>
                  )}
                </>
              )}
            </div>
          )}
          {(plan.projectAdvanced || plan.workspaceComparison === "unavailable") && (
            <label>
              <input
                type="checkbox"
                checked={advanced}
                onChange={(e) => setAdvanced(e.target.checked)}
              />
              {plan.workspaceComparison === "unavailable"
                ? "完整工程基线不可用。我已核对目标文件，同意仅恢复此文件。"
                : "原动作之后项目已有其他变化。我已核对本次只恢复该文件。"}
            </label>
          )}
          {plan.diffTruncated && (
            <label>
              <input
                type="checkbox"
                checked={partial}
                onChange={(e) => setPartial(e.target.checked)}
              />
              我已核对有限差异和基线，明确恢复整个文件
            </label>
          )}
          {plan.protected && (
            <p role="alert">
              保护分支不能执行恢复，请先切到功能分支后重新预览。
            </p>
          )}
          <button
            disabled={
              busy ||
              plan.state !== "draft" ||
              plan.protected ||
              ((plan.projectAdvanced || plan.workspaceComparison === "unavailable") && !advanced) ||
              (plan.diffTruncated && !partial)
            }
            onClick={delegate}
          >
            授权并交给 Agent 恢复
          </button>
          {plan.state === "authorized" && (
            <button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await onDelegate(plan);
                } catch (e) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              核对并继续交给 Agent
            </button>
          )}
          {["draft", "authorized"].includes(plan.state) && (
            <button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setError("");
                try {
                  await api(base + "/" + plan.id + "/revoke", {});
                  await query.refetch();
                } catch (e) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              撤销尚未执行的恢复授权
            </button>
          )}
          {plan.result?.error && <p role="alert">{plan.result.error}</p>}
        </section>
      )}
    </details>
  );
}
