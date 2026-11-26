// 展示会话已记录的代码动作和回执，允许定位源码与变更。
// 主要引用方包括 `AgentHome.tsx`。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CodeBlock } from "./CodeBlock";
import { api } from "./client";
import { callResult } from "./toolActivityGroups";
import type { CodeOperation, CodeResult } from "./generated";
function CheckOutput({
  conversationId,
  op,
}: {
  conversationId: string;
  op: CodeOperation;
}) {
  const [offset, setOffset] = useState(0);
  const page = useQuery<CodeResult>({
    queryKey: ["check-output", conversationId, op.id, offset],
    enabled: offset > 0,
    queryFn: () =>
      api(
        "/api/v2/conversations/threads/" +
          conversationId +
          "/code-operations/" +
          op.id +
          "/output?after=" +
          offset,
      ),
  });
  const value = offset ? page.data : op.result;
  return (
    <div>
      {page.error && <p role="alert">{page.error.message}</p>}
      {(value?.truncated || op.result?.truncated) && (
        <p role="status">
          {value?.outputNotice ||
            op.result?.outputNotice ||
            "检查输出达到保留上限，后续内容未留存；退出码不能证明日志完整。"}
        </p>
      )}
      {value?.text && <CodeBlock text={value.text} label="检查输出"/>}
      {value?.hasMore && (
        <button
          disabled={page.isFetching}
          onClick={() => setOffset(value.next!)}
        >
          继续查看检查输出
        </button>
      )}
      {offset > 0 && (
        <button onClick={() => setOffset(0)}>返回检查输出开头</button>
      )}
      {value?.hasMore && <small>当前仅显示一段输出，请继续核对。</small>}
    </div>
  );
}
export function CodeActivity({ conversationId }: { conversationId: string }) {
  const [before, setBefore] = useState<number | null>(null);
  const activity = useQuery<{
    operations: CodeOperation[];
    hasMore: boolean;
    nextBefore: number | null;
  }>({
    queryKey: ["code-activity", conversationId, before],
    queryFn: () =>
      api(
        "/api/v2/conversations/threads/" +
          conversationId +
          "/code-operations" +
          (before ? "?before=" + before : ""),
      ),
    refetchInterval: before ? false : 2000,
  });
  if (activity.error) return <p role="alert">{activity.error.message}</p>;
  if (!activity.data?.operations.length) return null;
  return (
    <details className="code-activity">
      <summary>
        代码处理与实际检查 · 当前页 {activity.data.operations.length} 步
      </summary>
      <p>检查进程的结果只说明该次检查，任务是否满足要求仍需审查。</p>
      {activity.data.hasMore && (
        <button onClick={() => setBefore(activity.data!.nextBefore)}>
          更早代码处理
        </button>
      )}
      {before && <button onClick={() => setBefore(null)}>最新代码处理</button>}
      {activity.data.operations.map((op) => (
        <article key={op.id}>
          <strong>
            {{ write: "修改文件", branch: "创建功能分支", check: "运行检查" }[
              op.operation
            ] || op.operation}{" "}
            · {callResult(op.state)}
          </strong>
          {op.result?.path && (
            <p>{op.result.path}</p>
          )}
          {op.result?.process && (
            <p>进程{callResult(op.result.process.state)} · 退出码 {op.result.process.exitCode ?? "尚未退出"}</p>
          )}
          {op.result?.process && (
            <CheckOutput key={op.id} conversationId={conversationId} op={op} />
          )}
          {op.result?.error && <p role="alert">{op.result.error}</p>}
          <details><summary>查看版本与来源</summary>
            <p>动作 <code>{op.source.actionId}</code></p>
            {(op.result?.revision||op.result?.process?.revision)&&<p>版本 <code>{op.result?.revision||op.result?.process?.revision}</code></p>}
          </details>
        </article>
      ))}
    </details>
  );
}
