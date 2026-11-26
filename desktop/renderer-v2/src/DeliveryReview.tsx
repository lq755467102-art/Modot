// 管理当前会话的版本化交付要求、证据覆盖、发现与人工验收决定。
// 主要引用方包括 `AgentHome.tsx`。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, APIError, loadJSON, saveJSON } from "./client";
import type {
  Artifact,
  ConversationScope,
  DeliveryOverview,
  DeliveryTarget,
  DeliveryRequirement,
  EvidencePage,
} from "./generated";

// 由交付状态面板提供要求和证据候选，维护当前要提交的关联选择。
function CoverageEditor({
  row,
  actions,
  disabled,
  onRecord,
}: {
  row: DeliveryRequirement;
  actions: EvidencePage | undefined;
  disabled: boolean;
  onRecord: (fields: Record<string, unknown>) => Promise<void>;
}) {
  const [kind, setKind] = useState("human"),
    [note, setNote] = useState(""),
    [action, setAction] = useState("");
  return (
    <fieldset>
      <legend>给这条要求补充检查或人工确认</legend>
      <label>
        来源
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="human">我的人工确认</option>
          <option value="automatic">关联真实自动检查</option>
        </select>
      </label>
      {kind === "automatic" && (
        <label>
          实际工具动作
          <select
            aria-label="关联检查动作"
            value={action}
            onChange={(e) => setAction(e.target.value)}
          >
            <option value="">选择当前会话动作</option>
            {actions?.actions
              .filter((a) => a.tool === "run_project_check")
              .map((a) => (
                <option key={a.id} value={a.id}>
                  {a.tool} · {a.state} · {a.id}
                </option>
              ))}
          </select>
        </label>
      )}
      <label>
        依据与覆盖限制
        <textarea
          aria-label="覆盖依据"
          value={note}
          maxLength={2000}
          onChange={(e) => setNote(e.target.value)}
        />
      </label>
      <button
        disabled={disabled || !note.trim() || (kind === "automatic" && !action)}
        onClick={async () => {
          await onRecord({
            operation: "coverage",
            requirementId: row.id,
            kind,
            note,
            ...(kind === "automatic" ? { actionId: action } : {}),
          });
        }}
      >
        保存此项核对
      </button>
    </fieldset>
  );
}

// 汇集当前目标下的交付要求、证据、发现和人工决定。审阅过的目标与版本单独保留，供提交时校验。
export function DeliveryReview({
  conversationId,
  scope,
  onInspect,
}: {
  conversationId: string;
  scope?: ConversationScope;
  onInspect: (id: string) => void;
}) {
  const [open, setOpen] = useState(false),
    [selection, setSelection] = useState(""),
    [before, setBefore] = useState<number | null>(null);
  const [findingBefore, setFindingBefore] = useState<number | null>(null),
    [decisionBefore, setDecisionBefore] = useState<number | null>(null);
  const [text, setText] = useState(""),
    [required, setRequired] = useState(false),
    [reason, setReason] = useState(""),
    [finding, setFinding] = useState(""),
    [acknowledge, setAcknowledge] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [reviewed, setReviewed] = useState<{
    target: DeliveryTarget;
    revision: number;
  } | null>(null);
  const base = `/api/v2/conversations/threads/${conversationId}`;
  const pendingKey = "delivery-pending-" + conversationId;
  const [pending, setPending] = useState<Record<string, unknown> | null>(() =>
    loadJSON(pendingKey, null),
  );
  const outputs = useQuery<{ artifacts: Artifact[] }>({
    queryKey: ["delivery-artifacts", conversationId],
    queryFn: () => api(base + "/artifacts"),
    enabled: open,
    refetchInterval: open ? 5000 : false,
  });
  const [kind, id] = selection.split(":");
  const query = useQuery<DeliveryOverview>({
    queryKey: [
      "delivery",
      conversationId,
      selection,
      before,
      findingBefore,
      decisionBefore,
    ],
    queryFn: () =>
      api(
        base +
          "/delivery?" +
          new URLSearchParams({
            ...(id ? { kind, target: id } : {}),
            ...(before ? { before: String(before) } : {}),
            ...(findingBefore ? { finding_before: String(findingBefore) } : {}),
            ...(decisionBefore
              ? { decision_before: String(decisionBefore) }
              : {}),
          }),
      ),
    enabled: open,
    refetchInterval: open ? 5000 : false,
  });
  const actions = useQuery<EvidencePage>({
    queryKey: ["delivery-actions", conversationId],
    queryFn: () => api(base + "/evidence?tool=run_project_check"),
    enabled: open && !!id,
    refetchInterval: open ? 5000 : false,
  });
  const state = query.error ? undefined : query.data;
  const changed =
    !!reviewed &&
    (!state ||
      JSON.stringify(state.target) !== JSON.stringify(reviewed.target) ||
      state.revision !== reviewed.revision);
  const disabled = busy || !reviewed || changed || !!query.error || !!pending;
  // 发送前保留完整动作身份和参数。明确拒绝后清除待处理请求，超时或结果不明时留给原请求继续核对。
  async function execute(body: Record<string, unknown>) {
    setPending(body);
    saveJSON(pendingKey, body);
    setBusy(true);
    setError("");
    try {
      await api(base + "/delivery", body);
      setPending(null);
      saveJSON(pendingKey, null);
      await query.refetch();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      if (
        e instanceof APIError &&
        e.status >= 400 &&
        e.status < 500 &&
        e.status !== 408
      ) {
        setPending(null);
        saveJSON(pendingKey, null);
      }
    } finally {
      setBusy(false);
    }
  }
  // 依据动作类型决定是否必须绑定已审阅的目标；首次发送时分配可追踪的请求 ID。
  async function command(
    fields: Record<string, unknown>,
    requireReview = true,
  ) {
    if (!state || pending || (requireReview && disabled)) return;
    await execute({
      id: crypto.randomUUID(),
      expectedRevision: requireReview ? reviewed!.revision : state.revision,
      ...(requireReview ? { target: reviewed!.target } : {}),
      ...fields,
    });
  }
  const labels: Record<string, string> = {
    passed: "自动检查通过",
    failed: "自动检查失败",
    unknown: "检查无法核实",
    stale: "已过期",
    opinion: "模型判断，未代替验证",
    confirmed: "用户人工确认",
    target_unselected: "请选择目标后核对适用版本",
  };
  return (
    <details
      className="delivery-review"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>需求与版本验收</summary>
      <p>
        执行结束、自动检查、模型判断和你的接受分别记录。登记要求不会自动改变当前执行；改变做法请提交纠正。
        自动检查通过只说明对应命令及断言通过，是否充分覆盖要求需查看原始证据。
      </p>
      <label>
        审查目标
        <select
          aria-label="验收目标"
          value={selection}
          onChange={(e) => {
            setSelection(e.target.value);
            setReviewed(null);
            setBefore(null);
            setFindingBefore(null);
            setDecisionBefore(null);
            setAcknowledge(false);
          }}
        >
          <option value="">请选择成果或代码</option>
          {outputs.data?.artifacts.map((a) => (
            <option key={a.id} value={"artifact:" + a.id}>
              成果：{a.title}
            </option>
          ))}
          {scope?.resources
            .filter((r) => r.kind === "code" && r.permission === "read")
            .map((r) => (
              <option key={r.id} value={"code:" + r.id}>
                代码：{r.name}
              </option>
            ))}
        </select>
      </label>
      {query.error && <p role="alert">{query.error.message}</p>}
      {outputs.error && <p role="alert">{outputs.error.message}</p>}
      {error && <p role="alert">{error}</p>}
      {pending && (
        <p role="status">
          上次提交结果未确认，当前保留同一请求身份。
          <button disabled={busy} onClick={() => execute(pending)}>
            核对未确认提交
          </button>
        </p>
      )}
      {state && (
        <>
          <p>
            执行生命周期：{state.execution.state} ·{" "}
            {state.execution.active ? "仍在运行" : "当前无已确认活动进程"}
          </p>
          {state.target && (
            <div aria-label="当前验收版本">
              <code>{state.target.versionId || state.target.revision}</code>
              <button
                disabled={busy}
                onClick={() => {
                  setReviewed({
                    target: state.target!,
                    revision: state.revision,
                  });
                  setAcknowledge(false);
                }}
              >
                核对此版本与要求
              </button>
            </div>
          )}
          {changed && (
            <p role="alert">
              版本或审查记录已更新。原确认尚未改绑，请重新核对。
            </p>
          )}
          <fieldset>
            <legend>验收要求</legend>
            <label>
              新增要求
              <textarea
                aria-label="新增验收要求"
                value={text}
                maxLength={2000}
                onChange={(e) => setText(e.target.value)}
              />
            </label>
            <label>
              <input type="checkbox" checked={required}
                onChange={(e) => setRequired(e.target.checked)} />
              必验项（单Modot交付前检查）
            </label>
            <button
              disabled={busy || !!pending || !text.trim() || (required && !state.target)}
              onClick={() => command({
                operation: "requirement", text,
                ...(required ? { required: true, target: state.target } : {}),
              }, false)}
            >
              登记要求
            </button>
          </fieldset>
          <p>
            当前展开 {state.requirements.length}/{state.totalRequirements}{" "}
            条要求；{state.openFindings} 条发现尚未关闭。
          </p>
          {!state.requirements.length && (
            <p>尚未登记要求；不会从“一切通过”的文案推断已覆盖。</p>
          )}
          <ol>
            {state.requirements.map((row) => (
              <li key={row.id}>
                <strong>{row.text}</strong>
                <small>
                  {" "}
                  · {row.required ? "必验项 · " : ""}提出者 {row.actor === "agent" ? "Agent" : "你"}
                </small>
                {row.uncovered && <p>未覆盖</p>}
                <ul>
                  {row.coverage.map((c) => (
                    <li key={c.id}>
                      <strong>{labels[c.status] || c.status}</strong>
                      <p>{c.note}</p>
                      {c.actionId ? (
                        <button onClick={() => onInspect(c.actionId!)}>
                          打开对应检查证据
                        </button>
                      ) : (
                        <small>未关联工具动作；这是该来源的判断</small>
                      )}
                    </li>
                  ))}
                </ul>
                <CoverageEditor
                  row={row}
                  actions={actions.data}
                  disabled={disabled}
                  onRecord={(fields) => command(fields)}
                />
              </li>
            ))}
          </ol>
          {state.hasMore && (
            <button onClick={() => setBefore(state.nextBefore)}>
              更早验收要求
            </button>
          )}
          {before && (
            <button onClick={() => setBefore(null)}>最新验收要求</button>
          )}
          {(state.gaps || state.gapsProvisional) && (
            <p role="status">
              仍有未覆盖、失败、无法核实的检查，或尚未展开的要求；模型意见不会自动填平缺口。
            </p>
          )}
          <fieldset>
            <legend>用户验收决定</legend>
            <label>
              接受或退回的依据
              <textarea
                aria-label="验收决定依据"
                value={reason}
                maxLength={2000}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
            <label>
              <input
                type="checkbox"
                checked={acknowledge}
                onChange={(e) => setAcknowledge(e.target.checked)}
              />
              我已核对并愿意接受当前明确列出的缺口和未展开部分
            </label>
            <button
              disabled={
                disabled ||
                state.execution.active ||
                state.execution.state === "unknown" ||
                !reason.trim() ||
                ((state.gaps || state.gapsProvisional) && !acknowledge)
              }
              onClick={() =>
                command({
                  operation: "accept",
                  reason,
                  confirm: true,
                  acknowledgeGaps: acknowledge,
                })
              }
            >
              接受这个版本
            </button>
            <button
              disabled={disabled || state.execution.active || !reason.trim()}
              onClick={() =>
                command({ operation: "reject", reason, confirm: true })
              }
            >
              退回这个版本
            </button>
          </fieldset>
          <ul aria-label="用户验收历史">
            {state.decisions.map((d) => (
              <li key={d.id}>
                {d.decision === "accept" ? "用户接受" : "用户退回"} ·{" "}
                {d.scopeUnknown
                  ? "请先选择目标核对适用性"
                  : d.stale
                    ? "旧版本或旧要求，已过期"
                    : "对应当前核对版本"}
                <p>{d.reason}</p>
              </li>
            ))}
          </ul>
          {state.decisionsHasMore && (
            <button
              onClick={() => setDecisionBefore(state.decisionsNextBefore!)}
            >
              更早验收决定
            </button>
          )}
          {decisionBefore && (
            <button onClick={() => setDecisionBefore(null)}>
              最新验收决定
            </button>
          )}
          <fieldset>
            <legend>记录问题发现</legend>
            <textarea
              aria-label="问题发现"
              value={finding}
              maxLength={2000}
              onChange={(e) => setFinding(e.target.value)}
            />
            <button
              disabled={disabled || !finding.trim()}
              onClick={() => command({ operation: "finding", text: finding })}
            >
              登记发现
            </button>
          </fieldset>
          <ul>
            {state.findings.map((f) => (
              <li key={f.id}>
                {f.state === "open" ? "未关闭" : "已关闭"}
                {f.scopeUnknown
                  ? " · 目标尚未核对"
                  : f.stale
                    ? " · 旧版本"
                    : ""}
                <p>{f.text}</p>
                <button
                  disabled={disabled || !reason.trim()}
                  onClick={() =>
                    command({
                      operation: "finding_state",
                      findingId: f.id,
                      state: f.state === "open" ? "closed" : "open",
                      reason,
                    })
                  }
                >
                  {f.state === "open" ? "按上方依据关闭" : "按上方依据重新打开"}
                </button>
              </li>
            ))}
          </ul>
          {state.findingsHasMore && (
            <button onClick={() => setFindingBefore(state.findingsNextBefore!)}>
              更早问题发现
            </button>
          )}
          {findingBefore && (
            <button onClick={() => setFindingBefore(null)}>最新问题发现</button>
          )}
        </>
      )}
    </details>
  );
}
