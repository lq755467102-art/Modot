// 显示子任务的输入范围、运行状态及成果交接，并提供控制操作。
// 主要引用方包括 `AgentHome.tsx`。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import type { Subtask, SubtaskPage, Conversation } from "./generated";

// 以父会话和可选子任务标识轮询原始任务记录，详情不沿用其他父任务的结果。
export function useSubtasks(conversationId: string) {
  return useQuery({
    queryKey: ["subtasks", conversationId],
    queryFn: () => api<SubtaskPage>(
      `/api/v2/conversations/threads/${encodeURIComponent(conversationId)}/subtasks`,
    ),
    enabled: !!conversationId,
    refetchInterval: 1500,
  });
}

// 供侧栏读取原子任务和具体成果。用户停止等操作以该任务的原始 ID 提交。
export function SubtaskPane({
  conversationId,
  thread,
  onSelect,
  onOpenTask,
  onDelegate,
}: {
  conversationId: string;
  thread?: Conversation;
  onSelect: (id: string) => void;
  onOpenTask: (task: Subtask) => void;
  onDelegate: (text: string) => void;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [runFilter, setRunFilter] = useState("current");
  const parent = thread?.subtaskOf;
  const query = useSubtasks(parent ? "" : conversationId);
  if (parent)
    return (
      <aside className="agent-scope">
        <p>隔离子任务审查会话 · 执行和取消由父任务控制，未自动整合代码。</p>
        <button onClick={() => onSelect(parent.parentId)}>返回父任务</button>
      </aside>
    );
  if (!query.data?.tasks.length && !query.error) return null;
  const currentRun = thread?.runtime?.run_id;
  const tasks = query.data?.tasks || [];
  const visible = runFilter === "current" && currentRun
    ? tasks.filter(task => task.parentRunId === currentRun)
    : tasks;
  return (
    <details className="subtask-pane">
      <summary>参与的子 Agent · {visible.length}</summary>
      <label>查看范围 <select value={runFilter} onChange={e => setRunFilter(e.target.value)}>
        <option value="current">{currentRun ? "本轮" : "本轮身份不可用 · 显示会话记录"}</option>
        <option value="all">整个会话</option>
      </select></label>
      <p>{query.data?.budgetNotice} 并发上限：每父任务2项、后端4项。</p>
      {(error || query.error) && (
        <p role="alert">{error || String(query.error)}</p>
      )}
      {visible.map((task) => (
        <article key={task.id}>
          <h3>{task.goal}</h3>
          <p>
            {task.kind === "code" ? "隔离代码工作树" : "只读资料与应用内成果"} ·{" "}
            {(
              {
                queued: "排队",
                preparing: "核对隔离基线",
                running: "执行中",
                stopping: "等待停止确认",
                unknown: "状态待核对",
                completed: "执行循环结束，交付待核查",
                failed: "执行失败",
                blocked: "受阻",
                cancelled: "已确认取消",
              } as Record<string, string>
            )[task.state] || task.state}
          </p>
          <button onClick={() => onOpenTask(task)}>在右侧查看子 Agent 概述</button>
          {task.kind === "code" &&
            ["completed", "failed", "blocked", "cancelled"].includes(
              task.state,
            ) && (
              <button
                onClick={() =>
                  onDelegate(
                    "请为代码子任务 " +
                      task.id +
                      " 和原目标资源 " +
                      task.resources?.find((r) => r.kind === "code")?.id +
                      " 准备完整整合预览。冲突需要用户取舍，整合后重查目标版本。",
                  )
                }
              >
                让 Agent 预览整合
              </button>
            )}
          {["queued", "preparing", "running", "stopping", "unknown"].includes(
            task.state,
          ) && (
            <button
              disabled={!!busy}
              onClick={async () => {
                setBusy(task.id);
                setError("");
                try {
                  await api(
                    `/api/v2/conversations/threads/${conversationId}/subtasks/${task.id}/cancel`,
                    {},
                  );
                  await query.refetch();
                } catch (e) {
                  setError(String(e));
                } finally {
                  setBusy("");
                }
              }}
            >
              请求取消
            </button>
          )}
        </article>
      ))}
    </details>
  );
}
