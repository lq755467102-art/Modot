// 在工作间读取旧任务及各轮证据。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, wsPath } from "./client";

export function JobsPane({ workspaceId }: { workspaceId: string }) {
  const [selected, setSelected] = useState("");
  const [round, setRound] = useState(0);
  const list = useQuery({
    queryKey: ["jobs", workspaceId],
    queryFn: () => api(wsPath(workspaceId, "jobs")),
    refetchInterval: 3000,
  });
  const detail = useQuery({
    queryKey: ["job", selected],
    queryFn: () => api("/api/jobs/" + selected),
    enabled: !!selected,
    refetchInterval: 2500,
  });
  const contract = useQuery({
    queryKey: ["contract", selected],
    queryFn: () => api("/api/jobs/" + selected + "/contract"),
    enabled: !!selected,
    refetchInterval: 4000,
  });
  const evidence = useQuery({
    queryKey: ["round", selected, round],
    queryFn: () => api("/api/jobs/" + selected + "/round/" + round),
    enabled: !!selected && round > 0,
  });
  return (
    <div className="jobs-pane">
      <div className="run-config">
        <select
          aria-label="旧任务记录"
          value={selected}
          onChange={(e) => {
            setSelected(e.target.value);
            setRound(0);
          }}
        >
          <option value="">选择已有任务</option>
          {list.data?.jobs.map((j: any) => (
            <option key={j.id} value={j.id}>
              {j.task} · {j.phase}
            </option>
          ))}
        </select>
      </div>
      {list.isError && <button onClick={() => list.refetch()}>重试读取旧任务记录</button>}
      {list.isSuccess && !list.data?.jobs.length && <p>此工作区暂无旧任务记录</p>}
      {selected && (
        <>
          <p>{detail.data?.phase} · 只读记录</p>
          <details>
            <summary>目标、判据和检查证据</summary>
            <pre>{JSON.stringify(contract.data, null, 2)}</pre>
          </details>
          <div className="rounds">
            {detail.data?.summary?.rounds?.map((r: any) => (
              <button key={r.n} onClick={() => setRound(r.n)}>
                第 {r.n} 轮 · {r.stop || r.status || "查看证据"}
              </button>
            ))}
          </div>
          {round > 0 && (
            <div>
              <h4>第 {round} 轮证据</h4>
              {evidence.isError ? (
                <button onClick={() => evidence.refetch()}>
                  读取失败，重试
                </button>
              ) : (
                <pre>{JSON.stringify(evidence.data, null, 2)}</pre>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
