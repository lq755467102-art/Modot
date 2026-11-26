// 列出工作间可用的工作区以及切换或相关管理入口。
// 主要引用方包括 `WorkroomPage.tsx`。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, wsPath } from "./client";
import type { GitHead, Workspace, WorkspaceSummary } from "./domain";
export function WorkspacesPane({
  workspace,
  workspaces,
  git,
  draftWorkspaces,
  onSelect,
  onRefresh,
  onError,
}: {
  workspace: WorkspaceSummary;
  workspaces: WorkspaceSummary[];
  git?: GitHead;
  draftWorkspaces: Set<string>;
  onSelect: (id: string) => void;
  onRefresh: () => Promise<void>;
  onError: (e: unknown) => void;
}) {
  const [source, setSource] = useState("");
  const legacy = useQuery({
    queryKey: ["legacy", workspace.id],
    queryFn: () => api(wsPath(workspace.id, "legacy-sessions")),
  });
  async function importSession(s: any) {
    try {
      await api(wsPath(workspace.id, "legacy-sessions"), {
        sessionId: s.id,
        title: s.title,
      });
      await legacy.refetch();
      await onRefresh();
    } catch (e) {
      onError(e);
    }
  }
  const preview = useQuery({
    queryKey: ["handoff", source],
    queryFn: () => api(wsPath(source, "handoff")),
    enabled: !!source,
    refetchInterval: 5000,
  });
  const status = useQuery({
    queryKey: ["worktree-status", source],
    queryFn: () => api<Workspace>(wsPath(source)),
    enabled: !!source,
    refetchInterval: 4000,
  });
  const children = workspaces.filter((w) => w.parent === workspace.id);
  async function integrate() {
    try {
      if (
        !preview.data ||
        !confirm(
          "将预览中的所有提交引入当前功能分支？如冲突，将保留冲突供你处理。",
        )
      )
        return;
      const target = await api<Workspace>(wsPath(workspace.id));
      await api(wsPath(workspace.id, "git"), {
        action: "applyCommit",
        sourceWorkspaceId: source,
        sourceCommit: preview.data.sourceCommit,
        expectedRevision: target.git.revision,
      });
      await onRefresh();
    } catch (e) {
      onError(e);
      await onRefresh();
    }
  }
  async function remove() {
    try {
      if (draftWorkspaces.has(source))
        throw Error("来源工作区仍有编辑草稿，请先保存或关闭草稿");
      if (
        !status.data ||
        !confirm("回收此干净工作树？Git 分支及任务记录会保留。")
      )
        return;
      await api(wsPath(source, "remove-worktree"), {
        confirm: true,
        expectedRevision: status.data.git.revision,
      });
      setSource("");
      await onRefresh();
    } catch (e) {
      onError(e);
    }
  }
  async function resolve(action: string) {
    try {
      const target = await api<Workspace>(wsPath(workspace.id));
      await api(wsPath(workspace.id, "git"), {
        action,
        expectedRevision: target.git.revision,
      });
      await onRefresh();
    } catch (e) {
      onError(e);
    }
  }
  return (
    <div className="workspace-management">
      <h3>工程工作区</h3>
      <p>
        并行环境从 Git
        提交创建，不复制前台未提交改动、依赖与密钥。进入工作树后用运行预设准备环境。
      </p>
      {workspace.parent && (
        <button onClick={() => onSelect(workspace.parent!)}>
          返回前台工作区
        </button>
      )}
      {children.map((w) => (
        <div className="finding" key={w.id}>
          <b>{w.name}</b>
          <small>{w.path}</small>
          <button onClick={() => onSelect(w.id)}>进入</button>
          <button onClick={() => setSource(w.id)}>审查与交付</button>
        </div>
      ))}
      {!children.length && (
        <p>此工作区暂无并行工作树，可从任务或变更面板创建。</p>
      )}
      {source && (
        <>
          <h4>来源 {status.data?.git.branch}</h4>
          <p>
            {status.data?.git.files.length || 0} 个未提交改动 ·{" "}
            {preview.data?.sourceCommit?.slice(0, 12)}
          </p>
          <pre>{preview.data?.commits.join("\n") || "无新提交"}</pre>
          <details>
            <summary>查看交付差异</summary>
            <pre>{preview.data?.text}</pre>
            {preview.data?.truncated && (
              <p>差异已截断，请通过外部 Git 工具完成全量审查。</p>
            )}
          </details>
          <button
            disabled={
              !preview.data?.commits.length ||
              !!status.data?.git.files.length ||
              !git?.available || git.protected
            }
            onClick={integrate}
          >
            引入已审阅的全部提交
          </button>
          <button onClick={remove}>回收已交付工作树</button>
        </>
      )}
      <details>
        <summary>关联此工程的历史会话</summary>
        <p>
          只列出明确记录了相同目录的最近会话。关联后按只读审查继续，不改变原始记录。
        </p>
        {legacy.data?.sessions.map((s: any) => (
          <div key={s.id}>
            {s.title}
            <button disabled={s.active} onClick={() => importSession(s)}>
              关联为只读审查
            </button>
          </div>
        ))}
      </details>
      <div>
        <button onClick={() => resolve("continueIntegration")}>
          冲突解决并暂存后继续引入
        </button>
        <button
          onClick={() => {
            if (confirm("中止工作台正在进行的引入？将由 Git 恢复引入前状态。"))
              resolve("abortIntegration");
          }}
        >
          中止当前引入
        </button>
      </div>
      {(preview.error || status.error) && (
        <p role="alert">{String(preview.error || status.error)}</p>
      )}
    </div>
  );
}
