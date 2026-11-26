// 编辑持续委托约定，并操作其版本确认、执行和交付状态。
// 主要引用方包括 `AgentHome.tsx`。
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import type {
  Delegation,
  DelegationSpec,
  DelegationCurrent,
} from "./generated";

// 生成新持续委托的编辑初值，使草稿在提交前拥有明确的目标、验收和停下条件。
export function initialAgreement(goal: string, calls = 32): DelegationSpec {
  return {
    goal,
    boundaries:
      "仅使用本会话明确选中且已授权的资料；范围外事项、外部发布和新增预算需要用户决定。",
    criteria: [
      {
        id: "goal",
        text: "由用户核查目标是否实现；尚未定义充分的自动检查。",
        kind: "manual",
      },
    ],
    maxRounds: 4,
    // L4 has its own explicitly confirmed, finite agreement. A conversation's
    // unlimited default is not a valid value for that agreement's budget.
    modelCalls: calls > 0 ? calls : 32,
  };
}
const states: Record<string, string> = {
  draft: "约定草案，未获执行授权",
  agreement_changed: "有新约定待确认",
  ready: "约定已人锁，等待开始",
  running: "持续推进中",
  stopping: "在执行边界停止中",
  paused: "已暂停，可核对后继续",
  unknown: "控制器或轮次结果未知，不自动重放",
  blocked: "需要处理阻塞或补充能力",
  budget_stopped: "预算停止，未完成要求仍保留",
  checks_met: "预设检查已满足，等待你裁定",
  accepted: "用户已接受此版本",
  returned: "用户已退回此版本",
};
const results: Record<string, string> = {
  passed: "预设检查通过",
  failed: "检查失败",
  unknown: "证据无法核对",
  stale: "证据对应旧版本",
  uncovered: "尚未覆盖",
  needs_human: "需要人工核查",
};

// 用父组件传入的委托版本作草稿，具体保存和确认交回控制面板。
function AgreementEditor({
  initial,
  onSave,
  onCancel,
}: {
  initial: DelegationSpec;
  onSave: (spec: DelegationSpec) => Promise<void>;
  onCancel: () => void;
}) {
  const [form, setForm] = useState(initial),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  return (
    <section aria-label="编辑委托约定">
      <label>
        目标
        <textarea
          aria-label="委托目标"
          maxLength={4000}
          value={form.goal}
          onChange={(e) => setForm({ ...form, goal: e.target.value })}
        />
      </label>
      <label>
        边界与限制
        <textarea
          aria-label="委托边界"
          maxLength={4000}
          value={form.boundaries}
          onChange={(e) => setForm({ ...form, boundaries: e.target.value })}
        />
      </label>
      <p>
        文字边界用于审查；实际文件与命令权限仍由资料授权控制。这里不会增加权限。
      </p>
      {form.criteria.map((c, i) => (
        <fieldset key={c.id}>
          <legend>
            要求 {i + 1} · {c.id}
          </legend>
          <label>
            需要实现什么
            <textarea
              maxLength={2000}
              value={c.text}
              onChange={(e) =>
                setForm({
                  ...form,
                  criteria: form.criteria.map((r, n) =>
                    n === i ? { ...r, text: e.target.value } : r,
                  ),
                })
              }
            />
          </label>
          <p>
            {c.kind === "code_check"
              ? "真实代码检查：" +
                (c.argv || []).map((a) => JSON.stringify(a)).join(" ")
              : c.kind === "artifact_contains"
                ? "文字片段检查，仅能核对指定片段，不能证明语义质量"
                : "人工核查，不会被自动算作通过"}
          </p>
          {c.kind === "artifact_contains" && (
            <label>
              必须包含的文字
              <input
                value={c.contains || ""}
                maxLength={500}
                onChange={(e) =>
                  setForm({
                    ...form,
                    criteria: form.criteria.map((r, n) =>
                      n === i ? { ...r, contains: e.target.value } : r,
                    ),
                  })
                }
              />
            </label>
          )}
          <button
            type="button"
            disabled={form.criteria.length <= 1}
            onClick={() =>
              setForm({
                ...form,
                criteria: form.criteria.filter((_, n) => n !== i),
              })
            }
          >
            移除此草案要求
          </button>
        </fieldset>
      ))}
      <button
        type="button"
        disabled={form.criteria.length >= 20}
        onClick={() =>
          setForm({
            ...form,
            criteria: [
              ...form.criteria,
              {
                id: "r" + crypto.randomUUID().slice(0, 8),
                text: "",
                kind: "manual",
              },
            ],
          })
        }
      >
        新增人工核查要求
      </button>
      <label>
        总执行轮次上限
        <input
          aria-label="委托总轮次"
          type="number"
          min={1}
          max={8}
          value={form.maxRounds}
          onChange={(e) =>
            setForm({ ...form, maxRounds: Number(e.target.value) })
          }
        />
      </label>
      <label>
        跨角色总模型调用上限
        <input
          aria-label="委托总调用预算"
          type="number"
          min={1}
          max={120}
          value={form.modelCalls}
          onChange={(e) =>
            setForm({ ...form, modelCalls: Number(e.target.value) })
          }
        />
      </label>
      <p>
        新约定不清零已用轮次和预算。更换目标、验收标准或增加总预算，都需要重新人锁。
      </p>
      {error && <p role="alert">{error}</p>}
      <button
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError("");
          try {
            await onSave(form);
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        保存待确认约定
      </button>
      <button disabled={busy} onClick={onCancel}>
        取消编辑
      </button>
    </section>
  );
}

// 协调委托约定草稿、锁定执行、运行记录和人工交付决定。后端的待确认版本与现行版本分别显示。
export function DelegationControl({
  conversationId,
  visible,
  runActive,
  onDraft,
  onInspect,
}: {
  conversationId: string;
  visible: boolean;
  runActive: boolean;
  onDraft: (job: Delegation) => Promise<void>;
  onInspect: (action: string) => void;
}) {
  const base = `/api/v2/conversations/threads/${conversationId}/delegations`;
  const query = useQuery<DelegationCurrent>({
    queryKey: ["delegation", conversationId],
    queryFn: () => api(base),
    enabled: visible,
    refetchInterval: 2000,
  });
  const job = query.error ? undefined : query.data?.current;
  const [expanded, setExpanded] = useState(false),
    [edit, setEdit] = useState<{
      spec: DelegationSpec;
      revision: number;
      jobId: string | null;
    } | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [confirm, setConfirm] = useState(false),
    [weak, setWeak] = useState(false),
    [reason, setReason] = useState(""),
    [gaps, setGaps] = useState(false),
    [pin, setPin] = useState<{
      revision: number;
      signature: string | null;
      version: string | null;
    } | null>(null),
    [count, setCount] = useState(10);
  useEffect(() => {
    if (job?.id) setExpanded(true);
  }, [job?.id]);
  useEffect(() => {
    setConfirm(false);
    setWeak(false);
    setPin(null);
  }, [job?.pendingVersion]);
  const shown = job?.versions.find(
    (v) => v.id === (job.pendingVersion || job.activeVersion),
  );
  const active = job?.versions.find((v) => v.id === job.activeVersion);
  // 确认界面固定打开时的约定修订与证据签名；记录变化后需要重新核对。
  const stale =
    !!pin &&
    (!job ||
      pin.revision !== job.revision ||
      pin.signature !== job.evaluation?.signature ||
      pin.version !== job.activeVersion);
  // 委托按钮共用忙碌和错误状态；后端操作成功后重读当前委托以反映实际流转。
  async function act(work: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await work();
      await query.refetch();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <details
      className="delegation-control"
      open={expanded}
      onToggle={(e) => setExpanded(e.currentTarget.open)}
    >
      <summary>
        持续委托
        {job
          ? " · " + (states[job.state] || job.state)
          : " · 先确认约定再自动多轮"}
      </summary>
      {query.error && (
        <p role="alert">委托记录无法核对：{query.error.message}</p>
      )}
      {error && <p role="alert">{error}</p>}
      <p>
        Agent负责推进，你确认目标与边界、核对证据并裁定结果。达到预设检查条件和你接受成果分别记录。起草另用本轮预算（最多6次调用），不计入尚未锁定的委托执行总额。
      </p>
      {!job && !query.error && !edit && (
        <button
          onClick={() =>
            setEdit({ spec: initialAgreement(""), revision: 0, jobId: null })
          }
        >
          建立委托草案
        </button>
      )}
      {edit && (
        <AgreementEditor
          key={edit.revision + ":" + (edit.jobId || "new")}
          initial={edit.spec}
          onCancel={() => setEdit(null)}
          onSave={async (spec) => {
            if (edit.jobId !== (job?.id || null))
              throw Error(
                "当前委托已改变，旧表单已失效；请取消后核对新的委托。",
              );
            setBusy(true);
            try {
              await api(base + (edit.jobId ? "/" + edit.jobId : ""), {
                spec,
                expectedRevision: edit.revision,
              });
              setEdit(null);
              setConfirm(false);
              setWeak(false);
              setPin(null);
              await query.refetch();
            } finally {
              setBusy(false);
            }
          }}
        />
      )}
      {job && shown && (
        <>
          <section aria-label="持续委托约定">
            <h3>{states[job.state] || job.state}</h3>
            <p>{job.reason}</p>
            <strong>{shown.spec.goal}</strong>
            <p>{shown.spec.boundaries}</p>
            <p>
              总上限：{shown.spec.maxRounds}轮 / {shown.spec.modelCalls}
              次逻辑模型调用，包含规划、执行和复核；不是费用上限。
            </p>
            <ol>
              {shown.spec.criteria.map((c) => (
                <li key={c.id}>
                  {c.text} ·{" "}
                  {c.kind === "manual"
                    ? "人工核查"
                    : c.kind === "code_check"
                      ? "代码检查"
                      : "文字片段检查"}
                  {c.argv && (
                    <pre>{c.argv.map((a) => JSON.stringify(a)).join(" ")}</pre>
                  )}
                  {c.contains && <pre>{c.contains}</pre>}
                </li>
              ))}
            </ol>
            <small>
              版本 {shown.id} ·{" "}
              {shown.actor === "agent" ? "Agent起草，尚需用户确认" : "用户草案"}
            </small>
            {job.pendingVersion && job.activeVersion && (
              <p role="status">
                上个已锁版本 {job.activeVersion}{" "}
                保留，新草案尚未替换执行授权；当前执行先到边界停止。
              </p>
            )}
            <button
              disabled={busy}
              onClick={() =>
                setEdit({
                  jobId: job.id,
                  spec: structuredClone(shown.spec),
                  revision: job.revision,
                })
              }
            >
              修改为新约定
            </button>
            <button
              disabled={
                busy || job.state === "running" || job.state === "stopping"
              }
              onClick={() => act(() => onDraft(job))}
            >
              请 Agent 补全约定
            </button>
            {job.pendingVersion && (
              <>
                <label>
                  <input
                    type="checkbox"
                    checked={confirm}
                    onChange={(e) => setConfirm(e.target.checked)}
                  />
                  我已核对这份目标、全部要求、资料边界和总预算
                </label>
                {shown.spec.criteria.some((c) => c.kind !== "code_check") && (
                  <label>
                    <input
                      type="checkbox"
                      checked={weak}
                      onChange={(e) => setWeak(e.target.checked)}
                    />
                    我知道人工要求和文字片段检查不能自动证明语义质量
                  </label>
                )}
                <button
                  disabled={
                    busy ||
                    runActive ||
                    !confirm ||
                    (shown.spec.criteria.some((c) => c.kind !== "code_check") &&
                      !weak)
                  }
                  onClick={() =>
                    act(() =>
                      api(base + "/" + job.id + "/lock", {
                        confirm: true,
                        version: job.pendingVersion,
                        expectedRevision: job.revision,
                        acknowledgeWeakChecks: weak,
                      }),
                    )
                  }
                >
                  锁定这份约定
                </button>
              </>
            )}
            {job.reconcileRunId && (
              <button disabled={busy||runActive} onClick={() => act(() => api(base + "/" + job.id + "/reconcile", {
                runId: job.reconcileRunId,
                expectedRevision: job.revision,
              }))}>
                核对已结束轮次
              </button>
            )}
            {!job.pendingVersion && !job.reconcileRunId &&
              ["ready", "paused", "blocked", "budget_stopped"].includes(
                job.state,
              ) && (
                <button
                  disabled={busy||runActive}
                  onClick={() =>
                    act(() =>
                      api(base + "/" + job.id + "/start", { confirm: true }),
                    )
                  }
                >
                  按已锁约定开始或继续
                </button>
              )}
            {["running", "stopping", "unknown"].includes(job.state) && (
              <button
                disabled={busy || job.state === "stopping"}
                onClick={() =>
                  act(() => api(base + "/" + job.id + "/stop", {}))
                }
              >
                停止持续委托
              </button>
            )}
          </section>
          {job.budget && (
            <p aria-label="委托剩余预算">
              已计入/保留 {job.budget.chargedCalls} 次；剩余{" "}
              {job.budget.remainingCalls} 次 / {job.budget.remainingRounds} 轮。
              {job.budget.unconfirmed ? "存在未确认分配，不退款、不重放。" : ""}
            </p>
          )}
          {job.evaluation && (
            <section aria-label="委托检查与缺口">
              <h3>检查与未完成要求</h3>
              <p>{job.evaluation.notice}</p>
              <ul>
                {job.evaluation.criteria.map((r) => (
                  <li key={r.criterion.id}>
                    {r.criterion.text} · {results[r.result] || r.result}
                    {r.reason && <p>{r.reason}</p>}
                    {r.claim && (
                      <p>
                        Agent记录：{String(r.claim.disposition)} ·{" "}
                        {String(r.claim.note)}
                      </p>
                    )}
                    {!!r.claim?.proof &&
                      typeof (r.claim.proof as Record<string, unknown>)
                        .actionId === "string" && (
                        <button
                          onClick={() =>
                            onInspect(
                              (r.claim!.proof as Record<string, string>)
                                .actionId,
                            )
                          }
                        >
                          查看对应工具证据
                        </button>
                      )}
                  </li>
                ))}
              </ul>
            </section>
          )}
          {!!job.rounds.length && (
            <details>
              <summary>执行轮次与来源</summary>
              <ol>
                {job.rounds.map((r, i) => (
                  <li key={r.runId}>
                    第{i + 1}轮 · {r.state} · 已核对
                    {r.chargedCalls ?? r.observedCalls ?? "?"}次 / 分配
                    {r.allocatedCalls}次 · 运行 {r.runId} · 约定 {r.version}
                  </li>
                ))}
              </ol>
            </details>
          )}
          {!!job.progress.length && (
            <details>
              <summary>推进记录（保留不做、延期和阻塞）</summary>
              <ol>
                {job.progress.slice(-count).map((r, i) => (
                  <li key={String(r.id) || i}>
                    {String(r.criterionId)} · {String(r.disposition)} ·{" "}
                    {String(r.note)}
                  </li>
                ))}
              </ol>
              {job.progress.length > count && (
                <button onClick={() => setCount(count + 10)}>
                  查看更多推进记录
                </button>
              )}
            </details>
          )}
          <details>
            <summary>约定版本历史</summary>
            {job.versions.map((v) => (
              <p key={v.id}>
                {v.id} · {v.actor} · {v.spec.goal} · 总{v.spec.maxRounds}轮/
                {v.spec.modelCalls}次
              </p>
            ))}
          </details>
          {active &&
            !job.pendingVersion &&
            !["running", "stopping", "unknown"].includes(job.state) &&
            job.evaluation && (
              <section aria-label="用户裁定持续委托">
                <button
                  onClick={() => {
                    setPin({
                      revision: job.revision,
                      signature: job.evaluation!.signature,
                      version: job.activeVersion,
                    });
                    setGaps(false);
                  }}
                >
                  核对当前交付以裁定
                </button>
                {pin && (
                  <>
                    <p>
                      绑定已锁版本 {pin.version}；轮询不会自动改绑。
                      {stale ? "记录或成果已变化，请重新核对。" : ""}
                    </p>
                    <label>
                      决定理由
                      <textarea
                        aria-label="委托裁定理由"
                        value={reason}
                        maxLength={2000}
                        onChange={(e) => setReason(e.target.value)}
                      />
                    </label>
                    {!job.evaluation.allChecksMet && (
                      <label>
                        <input
                          type="checkbox"
                          checked={gaps}
                          onChange={(e) => setGaps(e.target.checked)}
                        />
                        我已核对尚未完成/未覆盖要求，明确带缺口作出决定
                      </label>
                    )}
                    {(["accept", "return"] as const).map((decision) => (
                      <button
                        key={decision}
                        disabled={
                          busy ||
                          stale ||
                          !reason.trim() ||
                          (!job.evaluation!.allChecksMet && !gaps)
                        }
                        onClick={() =>
                          act(() =>
                            api(base + "/" + job.id + "/decide", {
                              confirm: true,
                              decision,
                              reason,
                              expectedRevision: pin.revision,
                              evaluationSignature: pin.signature,
                              acknowledgeGaps: gaps,
                            }),
                          )
                        }
                      >
                        {decision === "accept"
                          ? "接受这次交付"
                          : "退回这次交付"}
                      </button>
                    ))}
                  </>
                )}
                {job.decision && (
                  <p>
                    用户决定：{String(job.decision.decision)} ·{" "}
                    {String(job.decision.reason)}
                    {job.decisionStale ? " · 此决定对应旧版本，需重核" : ""}
                  </p>
                )}
              </section>
            )}
        </>
      )}
    </details>
  );
}
