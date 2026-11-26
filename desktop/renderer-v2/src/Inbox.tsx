// 在工作间聚合需要用户处理的授权和操作提示。
// 主要引用方包括 `WorkroomPage.tsx`。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, wsPath } from "./client";
export function Inbox({
  workspaceId,
  onThread,
  onError,
  onDone,
}: {
  workspaceId: string;
  onThread: (id: string) => void;
  onError: (e: unknown) => void;
  onDone: () => void;
}) {
  const base = wsPath(workspaceId);
  const [notice, setNotice] = useState("");
  const data = useQuery({
    queryKey: ["inbox", workspaceId],
    queryFn: () => api(base + "/inbox"),
    refetchInterval: 2500,
  });
  async function decide(item: any, decision: string) {
    try {
      if (
        decision === "resolve" &&
        !confirm(
          "已核对日志和实际产出？此操作仅归档未知待办，不重跑、不判定成功。",
        )
      )
        return;
      const result = await api(
        base +
          "/threads/" +
          item.thread.id +
          "/" +
          (decision === "resolve" ? "resolve" : "approval"),
        { sig: item.approval?.sig, decision, confirm: true },
      );
      setNotice(result.note || "待办已归档，原执行结果仍为未知。");
      onThread(item.thread.id);
      onDone();
      data.refetch();
    } catch (e) {
      onError(e);
    }
  }
  return (
    <div className="inbox">
      <h4>待处理</h4>
      {notice && <p>{notice}</p>}
      {!data.data?.items.length && (
        <p className="muted">当前没有需要核对的审批或未知待办。</p>
      )}
      {data.data?.items.map((item: any) => (
        <div className="finding" key={item.thread.id + item.kind}>
          <strong>{item.thread.title}</strong>
          {item.kind === "approval" ? (
            <>
              <p>
                {item.approval.label || item.approval.why || item.approval.tool}
              </p>
              <pre>{JSON.stringify(item.approval.args, null, 2)}</pre>
              <small>仅针对该签名的当前动作。会话原权限保持不变。</small>
              <div>
                <button onClick={() => decide(item, "allow")}>允许一次</button>
                <button onClick={() => decide(item, "deny")}>拒绝</button>
              </div>
            </>
          ) : (
            <>
              <p>派发结果未知，需要检查实际产出。</p>
              <button onClick={() => decide(item, "resolve")}>
                已核对，归档待办
              </button>
            </>
          )}
          <button onClick={() => onThread(item.thread.id)}>定位会话</button>
        </div>
      ))}
    </div>
  );
}
