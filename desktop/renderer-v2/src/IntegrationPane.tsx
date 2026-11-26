// 处理隔离子任务成果的比较、合入预览、冲突选择和结果核对。
// 主要引用方包括 `AgentHome.tsx`。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import type {
  IntegrationPage,
  IntegrationPlan,
  IntegrationFilePage,
} from "./generated";

function ConflictChoice({
  plan,
  path,
  onRefresh,
}: {
  plan: IntegrationPlan;
  path: string;
  onRefresh: () => void;
}) {
  const [pinned, pin] = useState(plan.planHash);
  const [choice, choose] = useState("target");
  const [reason, setReason] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const stale = pinned !== plan.planHash;
  return (
    <section aria-label="冲突取舍">
      <p>重叠变更需要你的取舍。Agent 的技术合并不能替代需求或架构判断。</p>
      {stale && (
        <p role="alert">
          预览版本已变化，输入已保留。
          <button onClick={() => pin(plan.planHash)}>核对后使用当前预览</button>
        </p>
      )}
      <label>
        候选
        <select value={choice} onChange={(e) => choose(e.target.value)}>
          <option value="target">保留目标内容</option>
          <option value="source">采用子任务内容（删除项会删除目标文件）</option>
          <option value="manual">提供完整合并正文（高级）</option>
        </select>
      </label>
      <label>
        取舍理由
        <textarea
          aria-label="冲突取舍理由"
          maxLength={2000}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </label>
      {choice === "manual" && (
        <label>
          完整合并正文
          <textarea
            value={text}
            maxLength={100000}
            onChange={(e) => setText(e.target.value)}
          />
        </label>
      )}
      {error && <p role="alert">{error}</p>}
      <button
        disabled={busy || stale || plan.targetStale || !reason.trim()}
        onClick={async () => {
          setBusy(true);
          setError("");
          try {
            await api(
              `/api/v2/conversations/threads/${plan.parentId}/integrations/${plan.id}/resolve`,
              {
                expectedHash: pinned,
                path,
                choice,
                reason,
                ...(choice === "manual" ? { text } : {}),
              },
            );
            onRefresh();
          } catch (e) {
            setError(String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        确认此文件的取舍
      </button>
    </section>
  );
}

export function IntegrationPane({
  conversationId,
  onDelegate,
  onSelect,
}: {
  conversationId: string;
  onDelegate: (text: string) => void;
  onSelect: (id: string) => void;
}) {
  const base = `/api/v2/conversations/threads/${conversationId}/integrations`;
  const [chosen, select] = useState(""),
    [before, setBefore] = useState<number | null>(null);
  const [index, setIndex] = useState(0),
    [offset, setOffset] = useState(0),
    [view, setView] = useState("diff");
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [managementDrafts, setManagementDrafts] = useState<
    Record<
      string,
      { token: string; abandon: boolean; reason: string; abortReason: string }
    >
  >({});
  const list = useQuery({
    queryKey: ["integrations", conversationId, before],
    queryFn: () =>
      api<IntegrationPage>(base + (before ? `?before=${before}` : "")),
    refetchInterval: 2000,
  });
  const id = chosen || list.data?.plans[0]?.id || "";
  const management = managementDrafts[id] || {
    token: "",
    abandon: false,
    reason: "",
    abortReason: "",
  };
  const {
    token: recycleToken,
    abandon,
    reason: discardReason,
    abortReason: reason,
  } = management;
  function patchManagement(patch: Partial<typeof management>) {
    setManagementDrafts((old) => ({
      ...old,
      [id]: { ...(old[id] || management), ...patch },
    }));
  }
  const setRecycleToken = (token: string) => patchManagement({ token });
  const setAbandon = (abandon: boolean) => patchManagement({ abandon });
  const setDiscardReason = (reason: string) => patchManagement({ reason });
  const setReason = (abortReason: string) => patchManagement({ abortReason });
  const detail = useQuery({
    queryKey: ["integration", conversationId, id],
    queryFn: () => api<IntegrationPlan>(base + "/" + id),
    enabled: open && !!id,
    refetchInterval: open ? 2000 : false,
  });
  const plan = detail.data;
  const page = useQuery({
    queryKey: [
      "integration-file",
      conversationId,
      id,
      plan?.planHash,
      index,
      view,
      offset,
    ],
    queryFn: () =>
      api<IntegrationFilePage>(
        `${base}/${id}/files/${index}?view=${view}&after=${offset}`,
      ),
    enabled: open && !!plan?.files[index],
  });
  if (!list.data?.plans.length && !list.error) return null;
  const file = plan?.files[index];
  const freshPage =
    page.data?.planHash === plan?.planHash ? page.data : undefined;
  return (
    <details
      className="integration-pane"
      onToggle={(e) => {
        setOpen(e.currentTarget.open);
        if (e.currentTarget.open && !chosen && id) select(id);
      }}
    >
      <summary>子任务交付整合</summary>
      {(error || list.error || detail.error || page.error) && (
        <p role="alert">
          {error || String(list.error || detail.error || page.error)}
        </p>
      )}
      <label>
        整合计划
        <select
          aria-label="整合计划"
          value={id}
          onChange={(e) => {
            select(e.target.value);
            setIndex(0);
            setOffset(0);
          }}
        >
          {list.data?.plans.map((p) => (
            <option key={p.id} value={p.id}>
              {p.state} · {p.fileCount} 文件 · {p.id.slice(0, 8)}
            </option>
          ))}
        </select>
      </label>
      {list.data?.hasMore && (
        <button
          onClick={() => {
            setBefore(list.data!.nextBefore!);
            select("");
          }}
        >
          更早整合计划
        </button>
      )}
      {before && (
        <button
          onClick={() => {
            setBefore(null);
            select("");
          }}
        >
          最新整合计划
        </button>
      )}
      {plan && (
        <>
          <p>{plan.notice}</p>
          <p>
            状态：{plan.state} · 已处理 {plan.applied.length}/
            {plan.files.length} · 冲突 {plan.conflicts}
          </p>
          <p>
            目标分支 {plan.targetBranch} · 来源分支 {plan.sourceBranch} ·
            来源基线 {plan.sourceBaseline} · 来源 HEAD {plan.sourceHead} ·{" "}
            {plan.commits.length} 个交付提交
          </p>
          {plan.targetStale && (
            <p role="alert">
              目标版本已推进或不可访问；原预览与旧检查已失效，需要重新预览。
            </p>
          )}
          <details>
            <summary>全部交付提交</summary>
            <pre>
              {plan.commits.join("\n") ||
                "无新增提交；包含相对基线的未提交变更"}
            </pre>
          </details>
          <label>
            全部变更文件
            <select
              aria-label="整合变更文件"
              value={index}
              onChange={(e) => {
                setIndex(Number(e.target.value));
                setOffset(0);
              }}
            >
              {plan.files.map((f, i) => (
                <option key={f.path} value={i}>
                  {f.path} · {f.kind} ·{" "}
                  {f.conflict
                    ? "需取舍"
                    : f.choice
                      ? "用户已决定"
                      : "技术合并候选"}
                </option>
              ))}
            </select>
          </label>
          <label>
            审查内容
            <select
              aria-label="整合审查内容"
              value={view}
              onChange={(e) => {
                setView(e.target.value);
                setOffset(0);
              }}
            >
              <option value="diff">差异</option>
              <option value="base">来源基线</option>
              <option value="target">目标现有内容</option>
              <option value="source">子任务内容</option>
              <option value="merged">合并候选</option>
            </select>
          </label>
          {file && (
            <>
              <pre>{freshPage?.text ?? "正在核对正文版本…"}</pre>
              <p>
                当前正文第 {offset} 字符起 · 共{" "}
                {freshPage?.totalChars ?? "待核对"} 字符
              </p>
              {offset > 0 && (
                <button onClick={() => setOffset(Math.max(0, offset - 4000))}>
                  上一页正文
                </button>
              )}
              {freshPage?.next !== null && freshPage?.next !== undefined && (
                <button onClick={() => setOffset(freshPage.next!)}>
                  下一页正文
                </button>
              )}
              {file.conflict && (
                <ConflictChoice
                  key={plan.id + ":" + file.path}
                  plan={plan}
                  path={file.path}
                  onRefresh={() => {
                    detail.refetch();
                    list.refetch();
                  }}
                />
              )}
            </>
          )}
          <button
            onClick={() =>
              onDelegate(
                `请为子任务 ${plan.taskId}、原目标代码资源 ${plan.resourceId} 重新准备整合预览，核对完整变更和当前授权。`,
              )
            }
          >
            让 Agent 重新预览
          </button>
          {plan.state === "prepared" && !plan.targetStale && (
            <button
              onClick={() =>
                onDelegate(
                  `请按已核对的整合计划 ${plan.id} 和 expected_hash=${plan.planHash} 应用；完成后必须在目标新版本运行真实检查，并用 inspect_subtask_integration 绑定 check_operation_id。存在未知或版本变化时停止核对，不重放。`,
                )
              }
            >
              让 Agent 整合并重查
            </button>
          )}
          {plan.resultRevision && (
            <button
              onClick={() =>
                onDelegate(
                  `请为整合 ${plan.id} 在目标资源 ${plan.resourceId} 的当前新版本运行真实 run_project_check，再用返回的 operationId 调用 inspect_subtask_integration(check_operation_id)。源检查不适用；若目标已变请先重新核对。`,
                )
              }
            >
              让 Agent 重查目标版本
            </button>
          )}
          <p>
            整合后所选检查：{plan.targetCheck?.status || "尚未绑定新目标检查"}
            。通过所选命令与充分覆盖、用户接受分别核对。
          </p>
          <details>
            <summary>来源检查证据</summary>
            <p>{plan.sourceChecksNotice}</p>
            <pre>{JSON.stringify(plan.sourceChecks, null, 2)}</pre>
            <button onClick={() => onSelect(plan.childId!)}>
              审查子任务全部动作
            </button>
          </details>
          <details>
            <summary>回收隔离工作树</summary>
            <p>
              先保存交付归档，再把完整目录移入保留区，不删除目录；原有提交、暂存区、未提交内容和成果记录均保留。活进程、未知状态、新变更或忽略内容会阻止回收。
            </p>
            <label>
              <input
                type="checkbox"
                checked={recycleToken === plan.id + ":" + plan.sourceRevision}
                onChange={(e) =>
                  setRecycleToken(
                    e.target.checked ? plan.id + ":" + plan.sourceRevision : "",
                  )
                }
              />
              我确认回收此明确来源版本的隔离目录
            </label>
            <label>
              <input
                type="checkbox"
                checked={abandon}
                onChange={(e) => {
                  setAbandon(e.target.checked);
                  setRecycleToken("");
                }}
              />
              明确放弃尚未整合的交付（仍保留归档）
            </label>
            {abandon && (
              <label>
                放弃理由
                <input
                  maxLength={2000}
                  value={discardReason}
                  onChange={(e) => setDiscardReason(e.target.value)}
                />
              </label>
            )}
            <button
              disabled={
                busy ||
                recycleToken !== plan.id + ":" + plan.sourceRevision ||
                (abandon && !discardReason.trim())
              }
              onClick={async () => {
                setBusy(true);
                setError("");
                try {
                  await api(
                    `/api/v2/conversations/threads/${conversationId}/subtasks/${plan.taskId}/recycle`,
                    {
                      confirm: true,
                      expectedRevision: plan.sourceRevision,
                      expectedBranch: plan.sourceBranch,
                      expectedHead: plan.sourceHead,
                      ...(abandon
                        ? { confirmAbandon: true, reason: discardReason }
                        : {}),
                    },
                  );
                  await detail.refetch();
                } catch (e) {
                  setError(String(e));
                } finally {
                  setBusy(false);
                  setRecycleToken("");
                }
              }}
            >
              保留归档后回收
            </button>
            {plan.cleanup && <pre>{JSON.stringify(plan.cleanup, null, 2)}</pre>}
          </details>
          <label>
            中止理由
            <input
              maxLength={2000}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          <button
            disabled={busy || !reason.trim()}
            onClick={async () => {
              setBusy(true);
              setError("");
              try {
                await api(`${base}/${plan.id}/abort`, {
                  expectedHash: plan.planHash,
                  reason,
                });
                await detail.refetch();
              } catch (e) {
                setError(String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            请求中止此整合（保留已写文件）
          </button>
        </>
      )}
    </details>
  );
}
