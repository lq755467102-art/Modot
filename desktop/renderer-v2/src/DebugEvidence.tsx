// 展示会话内已记录的调试进程和其可回查的状态、输出或位置。
// 主要引用方包括 `AgentHome.tsx`。
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import type { AgentDebugger } from "./generated";

export function DebugEvidence({
  conversationId,
  onWorkbench,
}: {
  conversationId: string;
  onWorkbench: () => void;
}) {
  const list = useQuery<{ debuggers: AgentDebugger[]; hasMore: boolean }>({
    queryKey: ["agent-debuggers", conversationId],
    queryFn: () =>
      api(
        "/api/v2/conversations/threads/" + conversationId + "/agent-debuggers",
      ),
    refetchInterval: 3000,
  });
  if (list.error) return <p role="alert">{list.error.message}</p>;
  if (!list.data?.debuggers.length) return null;
  return (
    <details aria-label="Python 调试证据">
      <summary>Python 调试证据 · {list.data.debuggers.length} 次</summary>
      <p>
        Agent
        负责调试与解释。变量展开及表达式求值需要执行授权，可能改变程序状态。
      </p>
      {list.data.debuggers.map((d) => (
        <article key={d.id}>
          <strong>
            {d.program} · {d.state}
          </strong>
          {d.evidenceTruncated && (
            <p role="status">
              部分调试证据超出大小限制；请让 Agent
              缩小采集范围，当前内容不完整。
            </p>
          )}
          <p>
            启动版本 {d.revision} · 来源动作 {d.source.actionId}
          </p>
          {d.state === "unknown" && (
            <p>
              后端无法接管原调试句柄；保留的栈是历史证据，不代表程序当前状态。
            </p>
          )}
          <ol>
            {d.stack.map((f) => (
              <li key={f.id}>
                {f.name} · {String(f.source?.path || "来源未知")}:{f.line}
              </li>
            ))}
          </ol>
          {d.lastEvidence && (
            <details>
              <summary>最近采集证据</summary>
              <pre>{JSON.stringify(d.lastEvidence, null, 2)}</pre>
            </details>
          )}
          {d.output && (
            <details>
              <summary>程序输出（最近 8000 字符）</summary>
              <pre>{d.output}</pre>
            </details>
          )}
          <small>调试器标识 {d.id}</small>
        </article>
      ))}
      {list.data.hasMore && <p>仅显示最近 20 次调试，原始记录仍保留。</p>}
      <button onClick={onWorkbench}>打开专业工具接管</button>
    </details>
  );
}
