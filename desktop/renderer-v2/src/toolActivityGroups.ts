// 把保留的工具事件整理成消息时间线与动作审查共同使用的分组和状态。
// 主要引用方包括 `ActionFileReview.tsx` 和 `AuditRoundSummary.tsx`。
import type { ConversationItem } from "./conversationProjection";
import { translator, type UiMessage, type UiTranslator } from "./uiMessages";
import type { EventRow, EvidenceAction } from "./generated";

export type ToolStatus = { label: string; tone?: "waiting" | "failed" };

// These tools only read existing records. Actions that can change state stay visible.
const readLabels: Record<string, string> = {
  read_action_evidence: "读取历史动作证据",
  read_conversation_dialogue: "回看公开对话",
  read_memory_tree: "读取记忆树",
  read_code_history: "读取代码历史",
  list_plugins: "查看已启用插件",
  list_installed_plugins: "查看已安装插件",
  list_plugin_catalog: "查看插件目录",
};

// 时间线或证据列表提供的工具记录。
// `id` 标识原动作，`tool/run/role/state` 用来判断相邻的已完成或失败动作能否合组。
type Call = {
  id: string;
  tool: unknown;
  run: unknown;
  role: unknown;
  state: unknown;
};

export function groupToolActivity<T>(items: T[], describe: (item: T) => Call | undefined) {
  const groups: { items: T[]; call?: Call; label?: string }[] = [];
  for (const item of items) {
    const call = describe(item);
    const label = call && typeof call.tool === "string" && Object.hasOwn(readLabels, call.tool)
      ? readLabels[call.tool] : undefined;
    const eligible = label && call?.run && call.role &&
      (call.state === "completed" || call.state === "failed") ? call : undefined;
    const last = groups.at(-1);
    if (eligible && last?.call && last.call.id !== eligible.id &&
        last.call.tool === eligible.tool && last.call.run === eligible.run &&
        last.call.role === eligible.role && last.call.state === eligible.state) {
      last.items.push(item);
    } else {
      groups.push({items: [item], call: eligible, label: eligible ? label : undefined});
    }
  }
  return groups;
}

export function timelineToolCall(item: ConversationItem): Call | undefined {
  const e = item.event;
  if (item.kind !== "activity" || item.explanation || item.pendingExplanation ||
      item.thoughtOnly || (Array.isArray(e.approvalRefs) && e.approvalRefs.length > 0) || typeof e.action_id !== "string") return;
  return {id: e.action_id, tool: e.tool, run: e.run_id, role: item.role, state: e.state};
}

const stepScope = (item: ConversationItem) => JSON.stringify([
  item.event.run_id, item.role, item.event.childId || item.event.agent_id,
]);

export function groupConversationSteps(items: ConversationItem[], foldGeneratedNote?: (item: ConversationItem)=>boolean) {
  const groups: {id: string; items: ConversationItem[]; batch: boolean}[] = [];
  let batchTurn: unknown;
  for (const item of items) {
    const e = item.event;
    const batch = item.kind === "activity" && !item.thoughtOnly && !e.eventCard &&
      (typeof e.tool === "string" || typeof e.action_id === "string");
    const previous = groups.at(-1);
    const previousItem = previous?.items.at(-1);
    const sameScope = previousItem && stepScope(previousItem) === stepScope(item);
    // A receipt from an older log may omit its turn. It must not join two known turns.
    const sameTurn = batchTurn === undefined || e.turn === undefined || batchTurn === e.turn;
    // Only the generic note generated for a silent tool call belongs to its batch.
    const note = e.toolPrelude === true && e.toolPreludeFallback === true &&
      item.kind === "message" && foldGeneratedNote?.(item);
    if (note && previous?.batch && sameScope && sameTurn && previousItem?.event.toolPrelude === true) {
      previous.items.push(item);
    } else if (note) {
      groups.push({id:`${item.id}:tools`,items:[item],batch:true});
      batchTurn=e.turn;
    } else if (batch && previous?.batch && sameScope && sameTurn) {
      previous.items.push(item);
      if (batchTurn === undefined) batchTurn = e.turn;
    } else {
      const prelude = batch && sameScope && sameTurn && !previous?.batch && previousItem?.event.toolPrelude === true ? previousItem : undefined;
      groups.push({id: prelude ? `${prelude.id}:tools` : item.id, items: [item], batch});
      batchTurn = e.turn;
    }
  }
  return groups;
}

const edits = new Set(["write_project_file", "edit_project_file", "restore_project_file", "undo_own_project_write", "write_file", "edit_file", "apply_patch"]);

const defaultText = translator("zh-CN");
export function batchSummary(items: ConversationItem[], active: boolean, t: UiTranslator = defaultText) {
  const changed = items.filter(item => typeof item.event.tool === "string" && edits.has(item.event.tool) && item.event.state === "completed").length;
  const unsuccessful = items.filter(item => item.event.state === "failed");
  const cancelled = unsuccessful.filter(item => item.event.checkState === "cancelled").length;
  const timedOut = unsuccessful.filter(item => item.event.checkState === "timed_out").length;
  const failed = unsuccessful.length - cancelled - timedOut;
  const waiting = items.some(item => item.event.state === "waiting_approval");
  const running = active ? items.find(item => item.event.state === "running") : undefined;
  const label = t(changed ? "代码改动与工具调用 · {n} 步" : "工具调用 · {n} 步",{n:items.length});
  const statuses: ToolStatus[] = [];
  if (running) statuses.push({label: t("执行中")});
  if (waiting) statuses.push({label: t("等待批准"), tone: "waiting"});
  if (cancelled) statuses.push({label: t("{n} 步已取消",{n:cancelled})});
  if (timedOut) statuses.push({label: t("{n} 步已超时",{n:timedOut}), tone: "failed"});
  if (failed) statuses.push({label: t("{n} 步失败",{n:failed}), tone: "failed"});
  return {label, statuses, running};
}

export function batchLabel(items: ConversationItem[], active: boolean) {
  const {label, statuses} = batchSummary(items, active);
  return [label, ...statuses.map(status => status.label)].join(" · ");
}

export function timelineToolStatus(event: EventRow): (ToolStatus & {label:UiMessage}) | undefined {
  if (event.checkState === "cancelled") return {label: "已取消"};
  if (event.checkState === "timed_out") return {label: "已超时", tone: "failed"};
  if (event.state === "waiting_approval") return {label: "等待批准", tone: "waiting"};
  if (event.tool === "run_project_check" && event.state === "unknown") {
    return event.backgroundPending === true ? {label: "启动回执待确认", tone: "waiting"} : {label: "待核对"};
  }
  if (event.ok === false || event.state === "failed" || event.error) return {label: "未成功", tone: "failed"};
  if (event.state === "unknown") return {label: "待核对"};
  if (event.state === "running") return {label: "执行中"};
  if (event.tool === "stop_project_check" && event.state === "completed") return {label: "请求已返回"};
}

export function evidenceToolCall(action: EvidenceAction): Call | undefined {
  if (action.identityKind === "legacy-record" || action.approvalRefs?.length) return;
  return {id: action.id, tool: action.tool, run: action.runId, role: action.role, state: action.state};
}

export function evidenceActionState(action: Pick<EvidenceAction, "tool" | "state" | "impact">) {
  const verification = action.impact?.verification;
  if (action.tool === "run_project_check" && action.state === "failed" && verification
    && typeof verification === "object" && "status" in verification &&
    (verification.status === "cancelled" || verification.status === "timed_out")) return verification.status;
  return action.state;
}

export function callResult(state: unknown) {
  switch (state) {
    case "completed": return "完成";
    case "failed": return "失败";
    case "running": return "执行中";
    case "waiting_approval": return "等待批准";
    case "cancelled": return "已取消";
    case "cancelled_before_start": return "启动前取消";
    case "timed_out": return "已超时";
    default: return "待核对";
  }
}


const actionLabels: Record<string,string> = {
  ...readLabels,
  install_catalog_plugin: "安装插件",
  install_local_plugin: "安装本地插件",
  enable_plugin_components: "启用插件组件",
  inspect_plugin_tools: "读取插件工具清单",
  call_plugin_tool: "调用插件工具",
  run_project_shell: "运行终端命令",
  inspect_git_repository: "查看 Git 仓库",
  run_project_check: "运行项目检查",
  stop_project_check: "请求停止检查",
  write_project_file: "修改项目文件",
  edit_project_file: "局部编辑项目文件",
  undo_own_project_write: "撤销本轮自己的写入",
};
export function toolTitle(tool: string | null | undefined) {
  return tool && Object.hasOwn(actionLabels,tool) ? actionLabels[tool] : "工具操作";
}
