// 将会话事件投影成时间线条目，并按会话、轮次和角色关联流式片段、原动作和定稿。
// 主要引用方包括 `toolActivityGroups.ts` 和 `ActionFileReview.tsx`。

import type { EventRow } from "./generated";

// 时间线读取的事件视图，`event` 保留原日志标识，`streaming` 指尚在增长的临时片段，`pendingExplanation` 指仍和后续动作等待配对。
export type ConversationItem = {
  id: string;
  kind: "message" | "activity" | "error";
  role: string;
  text: string;
  event: EventRow;
  explanation?: string;
  thoughtOnly?: boolean;
  pendingExplanation?: boolean;
  thoughtBoundaries?: { offset: number; thoughtId: string }[];
  streaming?: boolean;
  roleSummary?: boolean;
};

const roles: Record<string, string> = {
  planner: "规划者", executor: "执行者", reviewer: "测试者",
};
export const roleLabel = (role: string) => roles[role] || role;
export const parentEvent = (event: EventRow) => !event.agent_id && !event.childId;

function eventOwner(event: EventRow) {
  return `${event.run_id || "legacy"}:${event.agent_id || event.childId || "root"}`;
}

// 从会话事件和当前配置提取已确认为原生 L0 的运行，供后续按轮次解释事件。
export function recordedNativeRuns(events: EventRow[], current?: { runId?: string; config?: unknown } | null) {
  const native = (config: unknown) => {
    if (!config || typeof config !== "object") return false;
    const value = config as { crew?: string; purpose?: string };
    return value.crew === "l0" && value.purpose === "execute";
  };
  const runs = new Set<string>();
  if (current?.runId && native(current.config)) runs.add(current.runId);
  for (const event of events) {
    if (event.act !== "execution_config" || !event.run_id || !parentEvent(event)) continue;
    if (native(event.config)) runs.add(event.run_id);
    else runs.delete(event.run_id);
  }
  return runs;
}

// 只从当前运行的原终态判断是否需要用户回应，旧问题必须在有效消息边界内。
export function pendingQuestion(events: EventRow[], runId?: string) {
  if (!runId) return null;
  const current = events.filter(e => e.run_id === runId && parentEvent(e));
  const end = [...current].reverse().find(e => e.act === "end");
  if (end?.status !== "needs_human") return null;
  const lastUser = current.map(e => e.act).lastIndexOf("user");
  const ask = current.slice(lastUser + 1).reverse().find(e => e.act === "ask_human");
  if (!ask || typeof ask.question !== "string" || !ask.question.trim()) return null;
  return {
    question: ask.question,
    options: Array.isArray(ask.options)
      ? ask.options.filter((item): item is string => typeof item === "string" && !!item.trim())
      : [],
  };
}
function completeStreamText(event: EventRow, streamed: string) {
  const prefix = event.text;
  if (event.truncated !== true || !event.run_id || typeof event.turn !== "number" ||
      typeof event.full_len !== "number" || !prefix) return;
  const candidate = streamed.trim();
  // The writer counts Unicode code points, including supplementary characters.
  if (candidate.length > prefix.length && candidate.startsWith(prefix) &&
      Array.from(candidate).length === event.full_len) return candidate;
}

/** One deterministic projection for recorded facts; no tool replay or inference. */
export function projectConversation(events: EventRow[]): ConversationItem[] {
  const items: ConversationItem[] = [];
  const seen = new Set<string>();
  const actions = new Map<string, number>();
  const questions = new Set<string>();
  const explanations = new Map<string, number>();
  const thoughtTurns = new Map<string, unknown>();
  const streams = new Map<string, { turn: unknown; parts: number[] }>();
  const closedReplies = new Map<string, Map<number, number[]>>();
  const contextPreparation = new Map<string, number>();
  const removed = new Set<number>();
  // 思考标题沿用动作或收尾状态，公开进度由原正文承载。
  function finishThoughtText(scope: string, label = "梳理当前步骤") {
    const index = explanations.get(scope);
    if (index !== undefined)
      items[index] = { ...items[index], pendingExplanation: false, text: label };
  }
  function closeThought(scope: string, label?: string) {
    finishThoughtText(scope, label);
    explanations.delete(scope);
  }
  // 同一份 Markdown 可在思考后续写。保留续写位置，供界面暂缓新收到的后半段。
  function extendReply(index: number, scope: string, text: string): ConversationItem {
    const reply = items[index];
    const thought = explanations.get(scope);
    if (thought === undefined || thought <= index || text.length <= reply.text.length)
      return { ...reply, text };
    return { ...reply, text, thoughtBoundaries: [...reply.thoughtBoundaries ?? [],
      { offset: reply.text.length, thoughtId: items[thought].id }] };
  }
  // 把同一作用域的最后一段临时正文标为已收束，保留原内容以待定稿核对。
  function closeText(scope: string) {
    const index = streams.get(scope)?.parts.at(-1);
    if (index !== undefined) items[index] = {...items[index], streaming:false};
  }
  function appendablePart(scope: string, turn: unknown) {
    const stream = streams.get(scope);
    if (!stream || stream.turn !== turn) return;
    const index = stream.parts.at(-1);
    if (index === undefined || !items[index].streaming) return;
    // 同轮思考单独显示，正文仍是同一个 Markdown 源。其他可见条目保留原边界。
    if (index === items.length - 1 || (typeof turn === "number" && items.slice(index + 1).every(item =>
      item.thoughtOnly && item.event.turn === turn && `${eventOwner(item.event)}:${item.role}` === scope))) {
      return index;
    }
  }
  function closeTextBeforeThought(scope: string, turn: unknown) {
    if (typeof turn !== "number" || appendablePart(scope, turn) === undefined) closeText(scope);
  }
  // 用作用域和模型轮次保留已收束回复的位置，使后续原轮次的定稿能找到对应片段。
  function retainReply(scope: string) {
    const stream = streams.get(scope);
    if (typeof stream?.turn === "number") {
      const replies = closedReplies.get(scope) ?? new Map<number, number[]>();
      replies.set(stream.turn, stream.parts);
      closedReplies.set(scope, replies);
    }
    closeText(scope);
    streams.delete(scope);
  }
  for (const [eventIndex, e] of events.entries()) {
    const owner = eventOwner(e);
    const role = String(e.role || "executor");
    // 同名角色可能属于不同轮次/子 Agent；流、思考和动作按三者隔离，避免跨任务拼接。
    const scope = `${owner}:${role}`;
    const identity = e.sourceOffset !== undefined ? `${scope}:offset:${e.sourceOffset}`
      : e.seq ? `${scope}:seq:${e.seq}` : null;
    if (identity && seen.has(identity)) continue;
    if (identity) seen.add(identity);
    const id = identity || `${scope}:legacy:${eventIndex}`;
    const text = String(e.text || e.error || e.question ||
      (e.act === "needs_human" ? e.why || e.label : "") || "");
    if (e.act === "image") {
      if (!e.action_id) {
        items.push({id, kind:"activity", role, text:"图片读取历史 · 原图未关联", event:{...e, eventCard:"imageHistory"}});
      } else {
        const index = actions.get(`${scope}:action:${e.action_id}`);
        if (index !== undefined) items[index] = {...items[index], event:{...items[index].event, imageQueued:true}};
      }
      continue;
    }
    if (e.act === 'provider_stream_interrupted') {
      closeThought(scope, '本次响应中断'); closeText(scope); streams.delete(scope); thoughtTurns.delete(scope);
      if (typeof e.turn === "number") closedReplies.get(scope)?.delete(e.turn);
      else closedReplies.delete(scope);
      items.push({id,kind:'activity',role,text:'上次响应中断，未执行其中的工具；正在重新请求',event:e});
      continue;
    }
    if (e.act === 'provider_recovery') {
      const labels:Record<string,string>={waiting:'正在重连上游，任务进度保留',recovered:'连接已恢复，继续任务',exhausted:'上游暂不可用，进度已保存',cancelled:'已取消重连',failed:'模型请求失败'};
      items.push({id,kind:'activity',role,text:labels[String(e.state)]||'正在连接上游',event:e});
      continue;
    }
    // 上下文准备卡只表示暂态；新准备/完成/阻断会替换或移除旧卡，不留下仍在整理的假象。
    if (e.act === "context_compaction") {
      const previous = contextPreparation.get(scope);
      if (previous !== undefined) removed.add(previous);
      contextPreparation.delete(scope);
      if (e.state === "preparing") {
        contextPreparation.set(scope, items.length);
        items.push({id,kind:"activity",role,text:"正在整理上下文，完成后继续",event:e});
      }
      continue;
    }
    if (e.act === "context_blocked") {
      const previous = contextPreparation.get(scope);
      if (previous !== undefined) removed.add(previous);
      contextPreparation.delete(scope);
      items.push({id,kind:"error",role,text:String(e.reason || "上下文容量不足，原始记录保留；本次请求未发送。"),event:e});
      continue;
    }
    if (e.act === "role_phase" && ["completed","failed","paused","timed_out","waiting_human","skipped"].includes(String(e.state))) {
      const label=e.state==="paused"?"已暂停 · 保留思考":
        e.state==="timed_out"?"复核超时 · 保留思考":
        e.state==="failed"?"本阶段未完成 · 保留思考":
        e.state==="waiting_human"?"等待你的决定":undefined;
      closeThought(scope,label);
      retainReply(scope);
      thoughtTurns.delete(scope);
      const preparation=contextPreparation.get(scope);
      if(preparation!==undefined)removed.add(preparation);
      contextPreparation.delete(scope);
      const reason = e.error || ((e.state === "failed" || e.state === "timed_out") && role !== "executor" && typeof e.reason === "string" ? e.reason : "");
      const previous = items.at(-1);
      if (reason && !(previous?.kind === "error" && previous.role === role &&
        previous.event.run_id === e.run_id && previous.text === reason)) {
        items.push({id,kind:"error",role,text:reason,event:e});
      }
      continue;
    }
    if (["delta", "agent_message_chunk", "agent_thought_chunk"].includes(e.act)) {
      if (!text) continue;
      if (e.kind === "think" || e.act === "agent_thought_chunk") {
        closeTextBeforeThought(scope, e.turn);
        const index = explanations.get(scope);
        if (index !== undefined && index === items.length - 1 && items[index].event.turn === e.turn) {
          items[index] = {...items[index], explanation:items[index].explanation + text, event:e};
        } else {
          closeThought(scope);
          explanations.set(scope, items.length);
          items.push({id,kind:"activity",role,text:"正在梳理当前步骤",event:e,
            thoughtOnly:true,pendingExplanation:true,explanation:text});
        }
        thoughtTurns.set(scope, e.turn);
        continue;
      }
      const prior = streams.get(scope);
      const index = appendablePart(scope, e.turn);
      if (index !== undefined) {
        items[index] = { ...extendReply(index, scope, items[index].text + text), event: e };
        if (index !== items.length - 1) closeThought(scope);
      } else {
        // End the visible thought stream as prose begins and retain its tool-label pairing.
        finishThoughtText(scope);
        closeText(scope);
        const parts = prior && prior.turn === e.turn ? prior.parts : [];
        parts.push(items.length);
        streams.set(scope, {turn:e.turn, parts});
        items.push({id,kind:"message",role,text,event:e,streaming:true});
      }
      continue;
    }
    if (e.act === "think" || (e.act === "llm" && typeof e.reasoning === "string")) {
      const value = String(e.reasoning || e.text || "");
      if (value && !(thoughtTurns.has(scope) && (e.turn === undefined || thoughtTurns.get(scope) === e.turn))) {
        closeTextBeforeThought(scope, e.turn);
        closeThought(scope);
        explanations.set(scope, items.length);
        items.push({id,kind:"activity",role,text:"梳理当前步骤",event:e,thoughtOnly:true,explanation:value});
      }
      continue;
    }
    if (["user", "say", "ask_human", "needs_human"].includes(e.act)) {
      const action = e.action_id ? `${scope}:${e.action_id}` : null;
      if (e.act === "needs_human" && action && questions.has(action)) continue;
      if (e.act === "ask_human" && action && text) questions.add(action);
      const summary = e.act === "say" && e.toolPrelude !== true && e.roleSummary !== false;
      // 最终 say 与已显示的分段属于同一回复时原位收尾，只补尾部，避免把完整正文再次追加。
      const open = e.act === "say" ? streams.get(scope) : undefined;
      const current = open && (e.turn === undefined || open.turn === e.turn) ? open.parts : undefined;
      const turn = e.act === "say" && e.run_id && typeof e.turn === "number" ? e.turn : undefined;
      const closed = turn !== undefined ? closedReplies.get(scope)?.get(turn) : undefined;
      // A tool or end marker may have arrived ahead of the canonical text.
      // An explicit turn and matching text keep the update attached to its original reply.
      const closedText = !current && closed ? closed.map(index => items[index].text).join("").trim() : "";
      const late = !current && closed && (text.startsWith(closedText) || completeStreamText(e, closedText));
      const parts = current ?? (late ? closed : undefined) ?? [];
      const last = parts.at(-1);
      if (last !== undefined && text) {
        if(e.toolPrelude===true)for(const index of parts.slice(0,-1)) {
          const part=items[index];
          items[index]={...part,roleSummary:false,streaming:false,
            event:{...part.event,toolPrelude:true,toolPreludeFallback:e.toolPreludeFallback}};
        }
        const streamed = parts.map(index=>items[index].text).join("");
        const restored = completeStreamText(e, streamed);
        const canonical = restored ?? text;
        const suffix = canonical.startsWith(streamed) ? canonical.slice(streamed.length) : "";
        const finalText = parts.length === 1 ? canonical : items[last].text + suffix;
        items[last] = {...extendReply(last, scope, finalText),
          event:restored ? {...e, truncated:false} : e, streaming:false, roleSummary:summary};
        const visibleSuffix = streamed.trimEnd();
        if (parts.length > 1 && !canonical.startsWith(streamed) && visibleSuffix &&
            canonical.length > visibleSuffix.length && canonical.endsWith(visibleSuffix)) {
          // A history page can begin in the middle of the reply.
          const first = parts[0];
          items[first] = {...items[first], text:canonical.slice(0, -visibleSuffix.length) + items[first].text};
        }
      } else if (text) items.push({id,kind:"message",role:e.act === "user" ? "user" : role,
        text,event:e,roleSummary:summary});
      // 新用户输入结束所有旧回复的展示态，后续内容不能再拼进上一轮未关闭的文本或思考。
      if (e.act === "user") {
        for (const key of streams.keys()) closeText(key);
        for (const key of explanations.keys()) closeThought(key);
        streams.clear();
        closedReplies.clear();
        thoughtTurns.clear();
      } else if (!late && (!open || current)) {
        if (e.act === "say" && text && !summary) finishThoughtText(scope);
        closeText(scope);
        streams.delete(scope);
        if (summary) {
          closeThought(scope, role === "planner" ? "梳理规划方向" : role === "reviewer" ? "核对本轮结果" : "整理执行结果");
          thoughtTurns.delete(scope);
        }
      }
      if (turn !== undefined) closedReplies.get(scope)?.delete(turn);
      continue;
    }
    if (["action", "tool", "tool_start", "tool_started"].includes(e.act) ||
        (e.act === "not_run" && e.state === "waiting_approval")) {
      const key = e.action_id ? `${scope}:action:${e.action_id}` : id;
      const oldIndex = actions.get(key);
      const old = oldIndex === undefined ? undefined : items[oldIndex];
      const event = {...old?.event, ...e};
      const args = event.args && typeof event.args === "object" ? event.args as Record<string, unknown> : {};
      const path = typeof args.relative_path === "string" ? args.relative_path
        : typeof args.path === "string" ? args.path : "";
      const fallback = e.tool === "stop_project_check" ? "请求停止检查"
        : path ? `处理 ${path}` : `调用 ${String(e.tool || "工具")}`;
      const summary = typeof e.summary === "string" && e.summary.trim() ? e.summary
        : old?.text || fallback;
      const stateOf = (row:EventRow) => typeof row.state === "string" ? row.state :
        row.act === "tool" && typeof row.ok === "boolean" ? row.ok ? "completed" : "failed" : "running";
      // 动作卡合并原动作的多次观察；同级终态冲突显示 unknown，后台确认可修正暂时 unknown。
      const terminal = new Set(["completed","failed","unknown","waiting_approval","cancelled_before_start"]);
      const state = stateOf(e), previous = old && stateOf(old.event);
      if(e.backgroundPending&&previous&&terminal.has(previous)&&previous!=='unknown')continue;
      // A late start must not replace an observed terminal result.
      if (previous && terminal.has(previous) && !terminal.has(state)) continue;
      const published = e.backgroundCheck === true && !e.backgroundPending;
      const reconciled = previous === "unknown" && published;
      event.state = previous && terminal.has(previous) && terminal.has(state) && previous !== state && !reconciled ? "unknown" : state;
      if (published && e.act === "action" && e.tool === "run_project_check" &&
        (state === "completed" || state === "failed" || state === "unknown")) {
        // The published receipt replaces the temporary launch acknowledgement.
        event.ok = e.ok;
        event.error = e.error;
        event.backgroundPending = false;
      }
      if (event.state === "unknown" && previous && previous !== state) event.error="动作回执存在冲突，请核对原始证据。";
      if (oldIndex !== undefined) items[oldIndex] = {...old!, text:summary, event};
      else {
        retainReply(scope);
        const thought = explanations.get(scope);
        const adjacent = thought !== undefined && thought === items.length - 1;
        const item: ConversationItem = {id:adjacent ? items[thought].id : key,
          kind:"activity",role,text:summary,event,explanation:adjacent ? items[thought].explanation : undefined};
        closeThought(scope, summary);
        actions.set(key, adjacent ? thought : items.length);
        if (adjacent) items[thought] = item;
        else items.push(item);
      }
      continue;
    }
    if (["end", "paused", "paused_mid_tools", "error"].includes(e.act)) {
      const belongsToOwner = (key: string) => e.role ? key === scope : key.startsWith(`${owner}:`);
      for (const [key,index] of contextPreparation) if (belongsToOwner(key)) {
        removed.add(index);
        contextPreparation.delete(key);
      }
      for (const key of explanations.keys()) if (belongsToOwner(key)) closeThought(key);
      for (const key of streams.keys()) if (belongsToOwner(key)) retainReply(key);
    }
    if (e.error) items.push({ id, kind: "error", role, text, event: e });
  }
  return items.filter((_, index)=>!removed.has(index));
}
