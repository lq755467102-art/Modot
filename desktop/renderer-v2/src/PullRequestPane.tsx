// 在工作间展示与当前工作区关联的拉取请求及操作入口。
// 主要引用方包括 `WorkroomPage.tsx`。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, wsPath } from "./client";
import type { GitState } from "./domain";
export function PullRequestPane({
  workspaceId,
  git,
  onError,
}: {
  workspaceId: string;
  git: GitState;
  onError: (e: unknown) => void;
}) {
  const capability = useQuery({
    queryKey: ["pr-capability", workspaceId],
    queryFn: () => api(wsPath(workspaceId, "pull-request")),
  });
  const [title, setTitle] = useState(""),
    [body, setBody] = useState(""),
    [base, setBase] = useState("master"),
    [url, setURL] = useState(""),
    [busy, setBusy] = useState(false);
  async function create() {
    if (
      !confirm(
        "从 " +
          git.branch +
          " 向 " +
          base +
          " 创建远端草稿 PR？提交内容以已推送分支为准。",
      )
    )
      return;
    setBusy(true);
    try {
      const r = await api(wsPath(workspaceId, "pull-request"), {
        title,
        body,
        base,
        confirm: true,
        expectedRevision: git.revision,
      });
      setURL(r.url);
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <details className="pr-pane">
      <summary>创建草稿 PR</summary>
      {!capability.data?.available ? (
        <p>{capability.data?.reason || "正在检测 GitHub CLI…"}</p>
      ) : (
        <>
          <p>来源：{git.branch}。请先提交、推送并审阅 diff 与检查证据。</p>
          <input
            aria-label="PR目标分支"
            value={base}
            onChange={(e) => setBase(e.target.value)}
          />
          <input
            aria-label="PR标题"
            placeholder="变更解决了什么问题"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <textarea
            aria-label="PR说明"
            placeholder="行为变化、验证与限制"
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />
          <button
            disabled={
              busy || !title.trim() || git.protected || !!git.files.length
            }
            onClick={create}
          >
            {busy ? "创建并核对回执…" : "确认后创建草稿 PR"}
          </button>
          {url && (
            <a
              href={url}
              onClick={(e) => {
                e.preventDefault();
                if (/^https:\/\/github\.com\//.test(url))
                  window.open(url, "_blank");
              }}
            >
              查看已创建的 PR
            </a>
          )}
        </>
      )}
      {capability.error && <p role="alert">{String(capability.error)}</p>}
    </details>
  );
}
