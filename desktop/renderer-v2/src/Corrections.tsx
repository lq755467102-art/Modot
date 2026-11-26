// 提交以原消息、动作或成果版本定位的纠正，并展示待处理和已应用记录。
// 主要引用方包括 `AgentHome.tsx` 和 `ArtifactPane.tsx`。
import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { Correction, CorrectionPage, CorrectionTarget } from "./generated";

export function CorrectionForm({
  conversationId,
  target,
}: {
  conversationId: string;
  target: CorrectionTarget;
}) {
  const [text, setText] = useState(""),
    [busy, setBusy] = useState(false),
    [note, setNote] = useState("");
  const id = useRef(crypto.randomUUID());
  const [boundTarget, setBoundTarget] = useState(target);
  const targetChanged = JSON.stringify(target) !== JSON.stringify(boundTarget);
  const queries = useQueryClient();
  return (
    <fieldset>
      <legend>这一步需要怎么调整</legend>
      {targetChanged && (
        <p role="alert">
          对应的记录已更新，请先确认是否按新记录提出修改。
          <button
            onClick={() => {
              setBoundTarget(target);
              id.current = crypto.randomUUID();
            }}
          >
            使用更新后的记录
          </button>
        </p>
      )}
      <label>
        希望 Agent 如何调整
        <textarea
          aria-label="纠正意见"
          value={text}
          maxLength={4000}
          disabled={busy}
          onChange={(e) => setText(e.target.value)}
        />
      </label>
      <button
        disabled={busy || !text.trim() || targetChanged}
        onClick={async () => {
          setBusy(true);
          setNote("");
          try {
            const value = await api<Correction>(
              `/api/v2/conversations/threads/${conversationId}/corrections`,
              { id: id.current, target: boundTarget, text },
            );
            setNote(value.note);
            setText("");
            id.current = crypto.randomUUID();
            queries.invalidateQueries({
              queryKey: ["corrections", conversationId],
            });
          } catch (e) {
            setNote(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        交给 Agent 调整
      </button>
      {note && <p role="status">{note}</p>}
    </fieldset>
  );
}
const states: Record<Correction["state"], string> = {
  queued: "已排队",
  applying: "正在写入上下文，结果待核对",
  applied_at_boundary: "已在步骤边界交给 Agent",
  superseded: "已被替代",
  unable: "未能应用，需核对",
};
export function CorrectionQueue({
  conversationId,
  active,
  onResume,
  onInspect,
}: {
  conversationId: string;
  active: boolean;
  onResume: () => Promise<void>;
  onInspect: (id: string) => void;
}) {
  const [before, setBefore] = useState<number | null>(null),
    [replacement, setReplacement] = useState(""),
    [note, setNote] = useState(""),
    [busy, setBusy] = useState(false);
  const base = `/api/v2/conversations/threads/${conversationId}/corrections`;
  const queue = useQuery<CorrectionPage>({
    queryKey: ["corrections", conversationId, before],
    queryFn: () => api(base + (before ? "?before=" + before : "")),
    refetchInterval: 2000,
  });
  const replacementId = useRef(crypto.randomUUID());
  if (!queue.error && !queue.data?.corrections.length) return null;
  return (
    <details className="correction-queue">
      <summary>纠正意见与落实进度</summary>
      <p>
        “已交给
        Agent”仅表示已进入持久上下文。修复完成需核对后续工具证据和成果；暂停期间不会自动继续。
      </p>
      {queue.error && <p role="alert">{queue.error.message}</p>}
      <ul>
        {queue.data?.corrections.map((row) => (
          <li key={row.id}>
            <strong>{states[row.state]}</strong>
            <p>{row.text}</p>
            <small>
              已接收 {new Date(row.acceptedAt * 1000).toLocaleString()} · 目标{" "}
              {row.target.id}
            </small>
            <p>{row.note}</p>
            {row.target.kind === "action" && (
              <button onClick={() => onInspect(row.target.id)}>
                查看纠正目标证据
              </button>
            )}
            {row.state === "queued" && (
              <button
                disabled={busy || !replacement.trim()}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await api(base, {
                      id: replacementId.current,
                      target: row.target,
                      text: replacement,
                      supersedes: row.id,
                    });
                    replacementId.current = crypto.randomUUID();
                    setReplacement("");
                    await queue.refetch();
                  } catch (e) {
                    setNote(e instanceof Error ? e.message : String(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                用下方新意见替代这条
              </button>
            )}
          </li>
        ))}
      </ul>
      <label>
        替代尚未投递的意见
        <textarea
          aria-label="替代纠正意见"
          value={replacement}
          maxLength={4000}
          onChange={(e) => setReplacement(e.target.value)}
        />
      </label>
      <button
        disabled={
          active ||
          busy ||
          !queue.data?.corrections.some((r) => r.state === "queued")
        }
        onClick={async () => {
          setBusy(true);
          try {
            await onResume();
          } catch (e) {
            setNote(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        继续并处理排队纠正
      </button>
      {queue.data?.hasMore && (
        <button onClick={() => setBefore(queue.data!.nextBefore)}>
          更早纠正意见
        </button>
      )}
      {before && <button onClick={() => setBefore(null)}>最新纠正意见</button>}
      {note && <p role="alert">{note}</p>}
    </details>
  );
}
