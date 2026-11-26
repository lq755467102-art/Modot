// 展示讨论的原会话背景，并编辑新讨论草稿及项目关联。
// 主要引用方包括 `AgentHome.tsx`。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import type { DiscussionBackground } from "./generated";

// `AgentHome.tsx` 保存尚未提交的讨论草稿。
// `sourceId` 是来源会话，`projectId` 是选择时的项目快照，`associateProject` 表示是否另行关联。
// `excerpted` 提醒背景被节选。
export type DiscussionDraft = {
  sourceId: string;
  projectId?: string | null;
  title: string;
  background: string;
  associateProject: boolean;
  excerpted: boolean;
};
export function DiscussionDraftDialog({
  value,
  busy,
  error,
  onChange,
  onCreate,
}: {
  value: DiscussionDraft;
  busy: boolean;
  error?: string;
  onChange: (value: DiscussionDraft) => void;
  onCreate: () => void;
}) {
  const tooLarge = new TextEncoder().encode(value.background).length > 32768;
  return (
    <section className="discussion-draft">
      <p>把这段回复带到新会话，继续提问。</p>
      {value.excerpted && <p role="status">回复较长，已节选带入的文字。</p>}
      <details>
        <summary>编辑标题和引用内容</summary>
        <label>会话标题
          <input maxLength={100} value={value.title} disabled={busy}
            onChange={e => onChange({ ...value, title: e.target.value })}/>
        </label>
        <label>带入的文字背景
          <textarea aria-label="带入的文字背景" maxLength={10000} rows={5} value={value.background}
            disabled={busy} onChange={e => onChange({ ...value, background: e.target.value })}/>
        </label>
        {(value.projectId || value.associateProject) && <label className="discussion-association">
          <input type="checkbox" checked={value.associateProject} disabled={busy}
            onChange={e => onChange({ ...value, associateProject: e.target.checked })}/>
          关联原项目，资料权限单独选择
        </label>}
      </details>
      {tooLarge && <p role="alert">引用超过 32 KiB，请在上面缩短内容。</p>}
      {error && <p role="alert">{error}</p>}
      <button type="button" className="discussion-create"
        disabled={busy || !value.background.trim() || tooLarge} onClick={onCreate}>
        {busy ? "正在创建…" : "新建会话"}
      </button>
    </section>
  );
}

export function DiscussionContext({
  conversationId,
  onParent,
}: {
  conversationId: string;
  onParent: (id: string) => void;
}) {
  const [offset, setOffset] = useState(0);
  const query = useQuery<DiscussionBackground>({
    queryKey: ["discussion-background", conversationId, offset],
    queryFn: () =>
      api(
        `/api/v2/conversations/threads/${conversationId}/discussions?offset=${offset}`,
      ),
  });
  return (
    <details className="discussion-context">
      <summary>本会话背景快照 · 仅用户确认的文字</summary>
      {query.error && <p role="alert">{query.error.message}</p>}
      {query.data && (
        <>
          <p>{query.data.notice}</p>
          <pre>{query.data.text}</pre>
          <p>
            来源会话：{query.data.source.title} · 文字 SHA256{" "}
            {query.data.source.hash}
          </p>
          <button onClick={() => onParent(query.data!.source.conversationId)}>
            返回背景来源会话
          </button>
          {query.data.nextOffset !== null && (
            <button onClick={() => setOffset(query.data!.nextOffset!)}>
              继续阅读背景
            </button>
          )}
          {offset > 0 && (
            <button onClick={() => setOffset(0)}>回到背景开头</button>
          )}
        </>
      )}
    </details>
  );
}
