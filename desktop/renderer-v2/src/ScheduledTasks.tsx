// 管理定时计划、运行状态与分页历史，并用运行回执定位关联会话。
// 主要引用方包括 `AgentHome.tsx`。
import { useEffect, useRef, useState, type FormEvent, type RefObject } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, APIError, loadJSON, queryString, saveJSON } from "./client";
import { permissionOptions } from "./ComposerPermissions";
import type { Conversation, ConversationScope, Project } from "./generated";
import { projectCodeRoots } from "./NewConversationSetup";
import { UiIcon } from "./UiIcon";
import { useUiLanguage, type Locale } from "./UiLanguage";
import { translator, type UiMessage, type UiTranslator } from "./uiMessages";
import "./scheduled-tasks-preview.css";

const base = "/api/v2/scheduled-tasks";
const schedulesKey = ["scheduled-tasks"] as const;
const frequencies = { once: "单次", daily: "每天", weekly: "每周" } as const;
const weekdays = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"] as const;
const states = {
  queued: "待分派", dispatching: "正在分派", running: "运行中", waiting: "待回应",
  completed: "已完成", failed: "失败", skipped: "已跳过", missed: "已错过",
  stopped: "已停止", unknown: "结果待核对",
} as const;
const dispatchStates = {
  queued: "已领取，等待分派", sending: "正在分派", accepted: "已分派", rejected: "分派失败",
  not_dispatched: "未分派", unknown: "分派待核对",
} as const;
const sources = { scheduled: "按计划", manual: "立即运行", catch_up: "补跑" } as const;
const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const timeZones = [...new Set([localZone, "UTC", "Asia/Hong_Kong", ...Intl.supportedValuesOf("timeZone")])];

type PermissionMode = typeof permissionOptions[number][0];
type PermissionProfile = { mode: PermissionMode; revision: number };
type Limits = { mode: PermissionMode; write: boolean; execute: boolean };
type Timing = { frequency: keyof typeof frequencies; timezone: string; time: string; date?: string; weekday?: number };
type Target = { projectId: string | null; resourceId?: string; checkoutConversationId?: string | null };
type PlanFields = { title: string; instructions: string; timing: Timing };
type Plan = PlanFields & {
  id: string; revision: number; enabled: boolean; nextRunAt: number | null;
  conversationId?: string | null; bindingPending?: boolean;
  target: Target & { path?: string; branch?: string };
  authority: Limits & { revision: number };
};
type SavePlan = PlanFields & { target: Target; authority: Limits & { expectedRevision: number }; confirm: true };
type Run = {
  id: string; planId: string; planRevision: number; plan: Plan;
  state: keyof typeof states; dispatchState: keyof typeof dispatchStates; source: keyof typeof sources;
  scheduledAt: number | null; triggeredAt: number; missedRunId: string | null;
  reason: string | null; conversationId: string | null; runId: string | null; activeRunId: string | null;
  execution: { path: string; authority: Limits } | null;
};
type History = { runs: Run[]; nextCursor: string | null };
type ManualAttempt = { requestId: string; expectedRevision: number };
type View =
  | { page: "plan"; planId: string }
  | { page: "form"; plan?: Plan }
  | { page: "run"; planId: string; runId: string };

type Destinations = {
  projects: Project[]; projectsUnavailable: boolean;
  conversations: Conversation[]; conversationsUnavailable: boolean;
};

const planPath = (id: string) => base + "/" + queryString(id);
const runKey = (planId: string, runId: string) => [...schedulesKey, planId, "run", runId];
const manualKey = (planId: string) => "scheduled-manual:" + planId;
const message = (error: Error | null) => error?.message ?? "";

function dateTime(at: number, timezone: string, locale: Locale) {
  const options = {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    hourCycle: "h23", timeZoneName: "shortOffset",
  } as const;
  try {
    return new Intl.DateTimeFormat(locale, { ...options, timeZone: timezone }).format(at * 1000);
  } catch {
    return new Intl.DateTimeFormat(locale, { ...options, timeZone: "UTC" }).format(at * 1000);
  }
}

export function nextRunLabel(plan: Plan, locale: Locale = "zh-CN") {
  const t = translator(locale);
  if (plan.bindingPending) return t("会话待恢复");
  if (!plan.enabled) return t("已暂停");
  return plan.nextRunAt === null ? t("无后续触发") : dateTime(plan.nextRunAt, plan.timing.timezone, locale);
}

export function useScheduledPlans(active: boolean) {
  return useQuery({
    queryKey: schedulesKey, queryFn: () => api<{ plans: Plan[] }>(base),
    enabled: active, refetchInterval: 4000, refetchIntervalInBackground: true, retry: false,
  });
}

function projectLabel(plan: Plan, projects: Project[], t: UiTranslator) {
  if (!plan.target.projectId) return t("普通会话");
  return projects.find(project => project.id === plan.target.projectId)?.name ?? t("原项目");
}

function initialTiming(): Timing {
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  const date = [tomorrow.getFullYear(), tomorrow.getMonth() + 1, tomorrow.getDate()]
    .map(value => String(value).padStart(2, "0")).join("-");
  return { frequency: "daily", date, time: "09:00", timezone: localZone, weekday: 1 };
}

function LimitsSummary({ limits }: { limits: Limits }) {
  const { t } = useUiLanguage();
  const permission = permissionOptions.find(([id]) => id === limits.mode);
  return <>{permission ? t(permission[1]) : limits.mode} · {t(limits.write ? "允许写入" : "只读")} · {t(limits.execute ? "允许命令" : "禁止命令")}</>;
}

function PlanForm({
  heading, plan, projects, projectsUnavailable, conversations, conversationsUnavailable,
  active, stale, busy, onSave, onReload, onCancel,
}: Destinations & {
  heading: RefObject<HTMLHeadingElement | null>; plan?: Plan; active: boolean; stale: boolean; busy: boolean;
  onSave: (body: SavePlan) => void; onReload: () => void; onCancel: () => void;
}) {
  const { t } = useUiLanguage();
  const [fields, setFields] = useState<PlanFields>(() => ({
    title: plan?.title ?? "", instructions: plan?.instructions ?? "",
    timing: { ...initialTiming(), ...plan?.timing },
  }));
  const [destination, setDestination] = useState(() => ({
    projectId: plan?.target.projectId ?? "", resourceId: plan?.target.resourceId ?? "",
    worktree: !!plan?.target.checkoutConversationId, owner: plan?.target.checkoutConversationId ?? "",
  }));
  const [limits, setLimits] = useState<Limits>(plan?.authority ?? { mode: "ask", write: false, execute: false });
  const [confirmation, setConfirmation] = useState("");
  const permissions = useQuery({
    queryKey: [...schedulesKey, "permissions"],
    queryFn: () => api<PermissionProfile>("/api/v2/permissions"),
    enabled: active, refetchInterval: 4000,
  });
  const project = projects.find(item => item.id === destination.projectId);
  const roots = projectCodeRoots(project);
  const root = roots.find(item => item.id === destination.resourceId);
  const owners = conversations.filter(item => item.projectId === project?.id && item.id !== plan?.conversationId &&
    !item.archived && !item.readonlyEvidence && !item.subtaskOf);
  const owner = destination.worktree && owners.find(item => item.id === destination.owner);
  const scope = useQuery({
    queryKey: ["scope", owner ? owner.id : ""],
    queryFn: () => api<ConversationScope>("/api/v2/conversations/threads/" + queryString(destination.owner) + "/scope"),
    enabled: active && !!owner && !!root,
    refetchInterval: 4000,
  });
  const checkout = owner && scope.data?.projectId === project?.id
    ? scope.data?.resources.find(item => item.worktreeOf === root?.id && item.kind === "code" && item.permission === "read")
    : undefined;
  const locationIssue = destination.projectId && (
    projectsUnavailable ? t("暂时无法核对项目目录。")
    : !root ? t("请选择有读取授权的代码目录。")
    : destination.worktree && (
      conversationsUnavailable ? t("暂时无法读取工作树所在会话。")
      : !owner ? t("请选择工作树所在会话。")
      : scope.error ? message(scope.error)
      : scope.isPending ? t("正在核对工作树…")
      : !checkout ? t("该会话当前未选中此目录的工作树。")
      : ""
    )
  );
  const profile = permissions.isError ? undefined : permissions.data;
  const maxMode = permissionOptions.findIndex(([id]) => id === profile?.mode);
  const canSetMode = permissionOptions.findIndex(([id]) => id === limits.mode) <= maxMode;
  const canWrite = !destination.projectId || root?.write === true;
  const canExecute = !destination.projectId || root?.execute === true;
  const confirmKey = JSON.stringify([profile?.revision, root?.path, checkout?.id, checkout?.path]);
  const checked = confirmation === confirmKey;
  const ready = !!(profile && !stale && !locationIssue && canSetMode &&
    (!limits.write || canWrite) && (!limits.execute || canExecute) && fields.title.trim() &&
    fields.instructions.trim() && fields.timing.time && fields.timing.timezone.trim());
  const safeUpdate = (update: () => void) => { setConfirmation(""); update(); };
  const setTiming = (patch: Partial<Timing>) => safeUpdate(() => setFields(old => ({ ...old, timing: { ...old.timing, ...patch } })));
  const setLocation = (next: typeof destination) => safeUpdate(() => {
    setDestination(next);
    setLimits(old => ({ ...old, write: false, execute: false }));
  });
  const setLimit = (patch: Partial<Limits>) => safeUpdate(() => setLimits(old => ({ ...old, ...patch })));

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!ready || !checked || !profile || busy) return;
    const { frequency, time, timezone, date, weekday } = fields.timing;
    onSave({
      title: fields.title.trim(), instructions: fields.instructions.trim(),
      timing: { frequency, time, timezone: timezone.trim(),
        ...(frequency === "once" ? { date } : frequency === "weekly" ? { weekday } : {}) },
      target: destination.projectId ? {
        projectId: destination.projectId, resourceId: destination.resourceId,
        ...(destination.worktree ? { checkoutConversationId: destination.owner } : {}),
      } : { projectId: null },
      authority: { mode: limits.mode, write: limits.write, execute: limits.execute, expectedRevision: profile.revision },
      confirm: true,
    });
  }

  return <form className="schedule-preview-form" aria-labelledby="schedule-form-heading" onSubmit={submit}>
    <h2 id="schedule-form-heading" ref={heading} tabIndex={-1}>{t(plan ? "编辑计划" : "新建计划")}</h2>
    {stale && <div role="alert">{t("计划已更新。载入最新版本会替换当前表单。")}
      <div className="schedule-preview-actions"><button type="button" disabled={busy} onClick={onReload}>{t("载入最新版本")}</button></div>
    </div>}
    <fieldset className="schedule-form-fields" disabled={busy}>
      <label>{t("计划名称")}<input required maxLength={100} value={fields.title}
        onChange={event => safeUpdate(() => setFields({ ...fields, title: event.target.value }))}/></label>
      <label>{t("任务说明")}<textarea required maxLength={20000} rows={3} value={fields.instructions}
        onChange={event => safeUpdate(() => setFields({ ...fields, instructions: event.target.value }))}/></label>
      <div className="schedule-preview-fields">
        <label>{t("频率")}<select value={fields.timing.frequency}
          onChange={event => setTiming({ frequency: event.target.value as Timing["frequency"] })}>
          {Object.entries(frequencies).map(([value, label]) => <option value={value} key={value}>{t(label)}</option>)}
        </select></label>
        {fields.timing.frequency === "weekly" && <label>{t("星期")}<select value={fields.timing.weekday}
          onChange={event => setTiming({ weekday: Number(event.target.value) })}>
          {weekdays.map((day, index) => <option key={day} value={index}>{t(day)}</option>)}
        </select></label>}
        {fields.timing.frequency === "once" && <label>{t("日期")}<input type="date" required value={fields.timing.date}
          onChange={event => setTiming({ date: event.target.value })}/></label>}
        <label>{t("时间")}<input type="time" step={1} required value={fields.timing.time}
          onChange={event => setTiming({ time: event.target.value })}/></label>
        <label>{t("时区")}<input list="schedule-timezones" required value={fields.timing.timezone}
          onChange={event => setTiming({ timezone: event.target.value })}/></label>
        <datalist id="schedule-timezones">
          {timeZones.map(zone => <option key={zone} value={zone}/>)}
        </datalist>
      </div>
      <div className="schedule-preview-fields">
        <label>{t("归属")}<select value={destination.projectId} disabled={!!plan} onChange={event => {
          const selected = projects.find(item => item.id === event.target.value);
          const available = projectCodeRoots(selected);
          const preferred = available.find(item => item.id === selected?.folderResourceId) ?? available[0];
          setLocation({ projectId: event.target.value, resourceId: preferred?.id ?? "", worktree: false, owner: "" });
        }}>
          <option value="">{t("普通会话")}</option>
          {destination.projectId && !project && <option value={destination.projectId}>{t("原项目不可用")}</option>}
          {projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
        {project && <label>{t("代码目录")}<select value={destination.resourceId} onChange={event =>
          setLocation({ ...destination, resourceId: event.target.value, owner: "" })}>
          {!root && <option value={destination.resourceId}>{t("请选择代码目录")}</option>}
          {roots.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>}
        {project && <label>{t("工作位置")}<select value={destination.worktree ? "worktree" : "local"} onChange={event =>
          setLocation({ ...destination, worktree: event.target.value === "worktree", owner: "" })}>
          <option value="local">{t("项目原目录")}</option><option value="worktree">{t("已有工作树")}</option>
        </select></label>}
      </div>
      {project && destination.worktree && <label>{t("工作树所在会话")}<select value={destination.owner} onChange={event =>
        setLocation({ ...destination, owner: event.target.value })}>
        {!owner && destination.owner && <option value={destination.owner}>{t("原会话不可用")}</option>}
        <option value="">{t("请选择项目会话")}</option>
        {owners.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}
      </select></label>}
      {locationIssue ? <p className="schedule-preview-meta" role="status">{locationIssue}</p>
        : <p className="schedule-preview-meta">{checkout?.path ?? root?.path ?? t("使用定时会话的专属工作目录")}</p>}
      <div className="schedule-preview-fields">
        <label>{t("权限上限")}<select value={limits.mode} disabled={!profile}
          onChange={event => setLimit({ mode: event.target.value as PermissionMode })}>
          {permissionOptions.map(([id, label], index) => <option key={id} value={id} disabled={index > maxMode && id !== limits.mode}>{t(id === "full" ? "跳过常规审批" : label)}</option>)}
        </select></label>
        <div className="schedule-preview-checks">
          <label><input type="checkbox" checked={limits.write} disabled={!canWrite && !limits.write}
            onChange={event => setLimit({ write: event.target.checked })}/>{t("允许写入")}</label>
          <label><input type="checkbox" checked={limits.execute} disabled={!canExecute && !limits.execute}
            onChange={event => setLimit({ execute: event.target.checked })}/>{t("允许运行命令")}</label>
        </div>
      </div>
      {permissions.error && <p role="alert">{message(permissions.error)} <button type="button" onClick={() => void permissions.refetch()}>{t("重试读取权限")}</button></p>}
      {permissions.isPending && <p className="schedule-preview-meta" role="status">{t("正在读取当前权限…")}</p>}
      {profile && (!canSetMode || (limits.write && !canWrite) || (limits.execute && !canExecute)) &&
        <p role="alert">{t("权限上限超出当前授权，请调整后确认。")}</p>}
      <p className="schedule-preview-meta">{t("每次分派都会重新核对当前权限。")} {limits.mode === "full" && t("完全访问会跳过常规审批。")}</p>
      <label className="schedule-preview-confirm"><input type="checkbox" checked={checked} disabled={!ready}
        onChange={event => setConfirmation(event.target.checked ? confirmKey : "")}/>{t("确认执行时间、工作目录和权限上限")}</label>
    </fieldset>
    <div className="schedule-preview-actions">
      <button type="submit" className="schedule-preview-primary" disabled={busy || !ready || !checked}>
        {t(busy ? "正在保存…" : plan ? "保存计划" : "创建计划")}
      </button>
      <button type="button" disabled={busy} onClick={onCancel}>{t("取消")}</button>
    </div>
  </form>;
}

class ScheduleActionError extends Error {
  constructor(readonly label: UiMessage) { super(label); }
}

function runReceipt(value: unknown, planId: string, missedRunId?: string): value is Run {
  if (!value || typeof value !== "object") return false;
  const row = value as Partial<Run>;
  return row.planId === planId && row.plan?.id === planId && typeof row.id === "string" && /^[a-f0-9]{32}$/.test(row.id) &&
    (missedRunId ? row.source === "catch_up" && row.missedRunId === missedRunId : row.source === "manual") &&
    typeof row.state === "string" && Object.hasOwn(states, row.state) &&
    typeof row.dispatchState === "string" && Object.hasOwn(dispatchStates, row.dispatchState);
}

export function ScheduledTasks({ active, planRequest, returnLabel, onReturn, onOpenConversation, ...destinations }: Destinations & {
  active: boolean; planRequest?: { id: string }; returnLabel: string; onReturn: () => void;
  onOpenConversation: (conversationId: string, runId?: string) => void;
}) {
  const { t, locale } = useUiLanguage();
  const queryClient = useQueryClient();
  const [view, setView] = useState<View>({ page: "plan", planId: "" });
  const [cursor, setCursor] = useState("");
  const [notice, setNotice] = useState<UiMessage | "">("");
  const [error, setError] = useState<Error>();
  const [busy, setBusy] = useState(false);
  const performing = useRef(false);
  const activeRef = useRef(active);
  activeRef.current = active;
  const heading = useRef<HTMLHeadingElement>(null);
  const plans = useScheduledPlans(active);
  useEffect(() => {
    if (!planRequest) return;
    setView({ page: "plan", planId: planRequest.id });
    setCursor("");
    setNotice("");
    setError(undefined);
  }, [planRequest]);
  const selectedId = view.page === "form" ? view.plan?.id : view.planId;
  const plan = plans.data?.plans.find(item => item.id === selectedId) ??
    (!selectedId && view.page === "plan" ? plans.data?.plans[0] : undefined);
  const planId = plan?.id ?? selectedId ?? "";
  const path = planPath(planId);
  const history = useQuery({
    queryKey: [...schedulesKey, planId, "history", cursor],
    queryFn: () => api<History>(path + "/runs?limit=20" + (cursor ? "&cursor=" + queryString(cursor) : "")),
    enabled: active && !!planId && view.page === "plan", refetchInterval: 2000, refetchIntervalInBackground: true, retry: false,
  });
  const runId = view.page === "run" ? view.runId : "";
  const runView = useQuery({
    queryKey: runKey(planId, runId),
    queryFn: () => api<Run>(path + "/runs/" + queryString(runId)),
    enabled: active && !!planId && !!runId, refetchInterval: 2000, refetchIntervalInBackground: true, retry: false,
  });
  const run = runView.data;
  const manualAttempt = plan ? loadJSON<ManualAttempt | null>(manualKey(plan.id), null) : null;
  const focusKey = view.page === "run" ? runId + ":" + !!run : view.page === "form"
    ? "form:" + view.plan?.revision : "plan:" + (plan?.id ?? "");
  useEffect(() => {
    if (active) heading.current?.focus({ preventScroll: true });
  }, [active, focusKey]);

  function goTo(next: View, keepCursor = false) {
    setView(next);
    setNotice("");
    setError(undefined);
    if (!keepCursor) setCursor("");
  }

  function setPlan(saved: Plan) {
    queryClient.setQueryData<{ plans: Plan[] }>(schedulesKey, old => ({
      plans: old?.plans.some(item => item.id === saved.id)
        ? old.plans.map(item => item.id === saved.id ? saved : item) : [saved, ...(old?.plans ?? [])],
    }));
  }

  async function perform(action: () => Promise<void>) {
    if (performing.current) return;
    performing.current = true;
    setBusy(true);
    setError(undefined);
    setNotice("");
    try {
      await action();
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)));
      if (cause instanceof APIError && cause.code === "permission_changed") {
        void queryClient.invalidateQueries({ queryKey: [...schedulesKey, "permissions"] });
      }
    } finally {
      void queryClient.invalidateQueries({ queryKey: schedulesKey });
      performing.current = false;
      setBusy(false);
    }
  }

  function save(body: SavePlan) {
    if (view.page !== "form") return;
    const original = view.plan;
    void perform(async () => {
      const saved = await api<Plan>(original ? planPath(original.id) : base,
        original ? { ...body, expectedRevision: original.revision } : body);
      setPlan(saved);
      goTo({ page: "plan", planId: saved.id });
      setNotice(original ? "计划已保存" : "计划已创建");
      if (!original && saved.conversationId && activeRef.current) onOpenConversation(saved.conversationId);
    });
  }

  function setEnabled(current: Plan) {
    void perform(async () => {
      const saved = await api<Plan>(path + "/enabled", { enabled: !current.enabled, expectedRevision: current.revision });
      setPlan(saved);
      setNotice(saved.enabled ? "计划已恢复" : "计划已暂停");
    });
  }

  function trigger(current: Plan, missedRunId?: string) {
    void perform(async () => {
      const key = manualKey(current.id);
      const manual = missedRunId ? null : loadJSON<ManualAttempt | null>(key, null) ??
        { requestId: crypto.randomUUID(), expectedRevision: current.revision };
      if (manual) {
        saveJSON(key, manual);
        if (loadJSON<ManualAttempt | null>(key, null)?.requestId !== manual.requestId)
          throw new ScheduleActionError("无法保存立即运行请求编号，请恢复本地存储后重试。");
      }
      try {
        // 调度器用原编号去重。传输回执丢失时，可用新传输编号再次核对同一运行。
        const result = await api<Run>(path + (missedRunId ? "/catch-up" : "/run-now"),
          manual ?? { missedRunId, expectedRevision: current.revision }, true, undefined,
          value => runReceipt(value, current.id, missedRunId));
        if (!runReceipt(result, current.id, missedRunId)) throw new ScheduleActionError("运行回执格式不兼容");
        if (manual) saveJSON(key, null);
        queryClient.setQueryData(runKey(current.id, result.id), result);
        goTo({ page: "run", planId: current.id, runId: result.id });
        setNotice("已收到运行记录");
      } catch (cause) {
        if (cause instanceof APIError && [400, 403, 404, 409, 422].includes(cause.status) &&
            !["client_version", "data_version", "unknown"].includes(cause.code)) {
          if (manual) saveJSON(key, null);
          throw cause;
        }
        throw new ScheduleActionError("分派回执未确认。请刷新历史，或再次点击以原编号核对。");
      }
    });
  }

  return <section className="scheduled-tasks-preview" aria-labelledby="schedule-heading">
    <header className="schedule-preview-header">
      <h1 id="schedule-heading">{t("定时任务")}</h1>
      <button type="button" onClick={onReturn}><UiIcon name="back"/>{returnLabel === "返回会话" || returnLabel === "返回新聊天" ? t(returnLabel) : returnLabel}</button>
    </header>
    <div className="schedule-preview-body">
      <section className="schedule-preview-list" aria-label={t("计划列表")}>
        <button type="button" className="schedule-preview-primary" disabled={busy || plans.isPending || plans.isError}
          onClick={() => goTo({ page: "form" })}><UiIcon name="plus"/>{t("新建计划")}</button>
        {plans.isPending && <p className="schedule-preview-meta" role="status">{t("正在读取计划…")}</p>}
        {plans.error && <p role="alert">{message(plans.error)} <button type="button" onClick={() => void plans.refetch()}>{t("重试")}</button></p>}
        <ul>
          {plans.data?.plans.map(item => <li key={item.id}>
            <button type="button" className="schedule-preview-row" disabled={busy} title={item.title}
              aria-current={item.id === planId && view.page !== "form" ? "page" : undefined}
              onClick={() => goTo({ page: "plan", planId: item.id })}>
              <strong>{item.title}</strong>
              <span className="schedule-preview-meta">{projectLabel(item, destinations.projects, t)} · {nextRunLabel(item, locale)}</span>
            </button>
          </li>)}
        </ul>
      </section>
      <div className="schedule-preview-detail" aria-busy={busy}>
        {error && <p className="schedule-preview-notice" role="alert">{error instanceof ScheduleActionError ? t(error.label) : error.message}</p>}
        {notice && <p className="schedule-preview-meta schedule-preview-notice" role="status">{t(notice)}</p>}
        {view.page === "form" ? <PlanForm key={focusKey} {...destinations} heading={heading} active={active}
          plan={view.plan} busy={busy} stale={!!(view.plan && plan && view.plan.revision !== plan.revision)}
          onSave={save} onReload={() => goTo({ page: "form", plan })}
          onCancel={() => goTo({ page: "plan", planId })}/>
          : view.page === "run" ? <section className="schedule-preview-run" aria-labelledby="schedule-run-heading">
            <button type="button" disabled={busy} onClick={() => goTo({ page: "plan", planId }, true)}><UiIcon name="back"/>{t("返回计划")}</button>
            <h2 id="schedule-run-heading" ref={heading} tabIndex={-1}>{t("运行详情")}{run && " · " + run.plan.title}</h2>
            {runView.error && <p role="alert">{message(runView.error)} <button type="button" onClick={() => void runView.refetch()}>{t("重试")}</button></p>}
            {!run && !runView.error && <p role="status">{t("正在读取运行详情…")}</p>}
            {run && <>
              <p className="schedule-preview-meta">{t(sources[run.source])} · {dateTime(run.scheduledAt ?? run.triggeredAt, run.plan.timing.timezone, locale)} · {t("计划版本 {revision}", { revision: run.planRevision })}</p>
              <article className="schedule-preview-message">
                <h3 data-outcome={run.state}>{t(states[run.state] ?? states.unknown)}</h3>
                <p>{t(dispatchStates[run.dispatchState] ?? dispatchStates.unknown)}</p>
                {run.reason && <p>{run.reason}</p>}
              </article>
              <article className="schedule-preview-message">
                <h3>{t("当次任务")}</h3>
                <p>{run.plan.instructions}</p>
                <p className="schedule-preview-meta">{run.execution?.path ?? run.plan.target.path ?? t("独立会话目录")}</p>
                <p className="schedule-preview-meta"><LimitsSummary limits={run.execution?.authority ?? run.plan.authority}/></p>
              </article>
              {run.conversationId && <button type="button" className="schedule-preview-primary"
                onClick={() => {
                  const destination = run.state === "waiting" || run.state === "running"
                    ? run.activeRunId ?? run.runId : run.runId;
                  if (run.conversationId) onOpenConversation(run.conversationId, destination ?? undefined);
                }}>{t("打开关联会话")}<UiIcon name="forward"/></button>}
              {run.state === "missed" && plan && <>
                <p className="schedule-preview-meta">{t("可按当前计划手动补跑最近一次错过的任务。")}</p>
                <button type="button" disabled={busy || plans.isError} onClick={() => trigger(plan, run.id)}>{t("补跑此记录")}</button>
              </>}
            </>}
          </section> : plan ? <section aria-labelledby="schedule-plan-heading">
            <header className="schedule-preview-plan-heading">
              <h2 id="schedule-plan-heading" ref={heading} tabIndex={-1}>{plan.title}</h2>
              <span className="schedule-preview-state" data-paused={!plan.enabled}>{t(plan.enabled ? "已启用" : "已暂停")}</span>
            </header>
            <p className="schedule-preview-instructions">{plan.instructions}</p>
            <dl className="schedule-preview-facts">
              <div><dt>{t("执行位置")}</dt><dd>{projectLabel(plan, destinations.projects, t)} · {t(plan.target.checkoutConversationId ? "工作树" : plan.target.projectId ? "项目原目录" : "独立会话目录")}</dd>
                {plan.target.path && <small>{plan.target.path}</small>}</div>
              <div><dt>{t("频率")}</dt><dd>{t(frequencies[plan.timing.frequency])} {plan.timing.frequency === "weekly" && t(weekdays[plan.timing.weekday ?? 0])} {plan.timing.time}</dd>
                {plan.timing.date && <small><time dateTime={plan.timing.date}>
                  {new Date(plan.timing.date + "T00:00:00Z").toLocaleDateString(locale, { dateStyle: "medium", timeZone: "UTC" })}
                </time></small>}</div>
              <div><dt>{t("下次运行")}</dt><dd>{nextRunLabel(plan, locale)}</dd><small>{plan.timing.timezone}</small></div>
              <div><dt>{t("权限上限")}</dt><dd><LimitsSummary limits={plan.authority}/></dd></div>
            </dl>
            <div className="schedule-preview-actions">
              <button type="button" className="schedule-preview-primary" disabled={busy || plans.isError || plan.bindingPending}
                onClick={() => trigger(plan)}>{t(manualAttempt ? "核对立即运行回执" : "立即运行")}</button>
              <button type="button" disabled={busy || plans.isError || plan.bindingPending} onClick={() => setEnabled(plan)}>{t(plan.enabled ? "暂停计划" : "恢复计划")}</button>
              <button type="button" disabled={busy || plans.isError || plan.bindingPending} onClick={() => goTo({ page: "form", plan })}>{t("编辑计划")}</button>
              {plan.conversationId && <button type="button" disabled={busy}
                onClick={() => { if (plan.conversationId) onOpenConversation(plan.conversationId); }}>{t("打开定时会话")}<UiIcon name="forward"/></button>}
            </div>
            <section className="schedule-preview-history" aria-labelledby="schedule-history-heading">
              <h3 id="schedule-history-heading">{t("运行历史")}</h3>
              {history.error && <p role="alert">{message(history.error)} <button type="button" onClick={() => void history.refetch()}>{t("重试")}</button></p>}
              {history.isPending && <p className="schedule-preview-meta" role="status">{t("正在读取运行历史…")}</p>}
              {history.data && (history.data.runs.length ? <ul>{history.data.runs.map(item => <li key={item.id}>
                <button type="button" className="schedule-preview-history-row" disabled={busy} data-schedule-run-id={item.id}
                  onClick={() => goTo({ page: "run", planId, runId: item.id }, true)}>
                  <span><strong>{dateTime(item.scheduledAt ?? item.triggeredAt, item.plan.timing.timezone, locale)}</strong> <small>{t(sources[item.source])}</small></span>
                  <span data-outcome={item.state}>{t(states[item.state] ?? states.unknown)}</span>
                  <UiIcon name="forward"/>
                </button>
              </li>)}</ul> : <p className="schedule-preview-meta">{t("暂无运行记录")}</p>)}
              {(cursor || history.data?.nextCursor) && <div className="schedule-preview-actions">
                {cursor && <button type="button" disabled={busy} onClick={() => setCursor("")}>{t("回到最新")}</button>}
                {history.data?.nextCursor && <button type="button" disabled={busy} onClick={() => setCursor(history.data?.nextCursor ?? "")}>{t("更早记录")}</button>}
              </div>}
            </section>
          </section> : <h2 ref={heading} tabIndex={-1}>{t(plans.isPending ? "正在读取计划…" : plans.isError ? "暂时无法读取定时任务" : "暂无计划")}</h2>}
      </div>
    </div>
  </section>;
}
