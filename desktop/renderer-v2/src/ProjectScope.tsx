// 编辑会话所选项目、资料和代码资源的读取、写入与执行范围。
// 主要引用方包括 `AgentHome.tsx`。
import { useState } from "react";
import { useUiLanguage } from "./UiLanguage";
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import { chooseFolderProject } from './projectFolder';
import type { Project, ConversationScope as Scope } from "./generated";
export function ProjectScope({
  conversationId,
  onChange,
  onError,
  onBusyChange,
}: {
  conversationId: string;
  onChange: () => void;
  onError: (e: unknown) => void;
  onBusyChange: (value: boolean) => void;
}) {
  const {t} = useUiLanguage();
  const [kind, setKind] = useState("documents"),
    [busy, setBusy] = useState(false);
  const projects = useQuery<{ projects: Project[] }>({
    queryKey: ["projects"],
    queryFn: () => api("/api/v2/projects"),
    refetchInterval: 4000,
  });
  const url =
    "/api/v2/conversations/threads/" +
    encodeURIComponent(conversationId) +
    "/scope";
  const scope = useQuery<Scope>({
    queryKey: ["scope", conversationId],
    queryFn: () => api(url),
  });
  const project = projects.data?.projects.find(
    (p) => p.id === scope.data?.projectId,
  );
  const selectedIds = scope.data?.resources.map(resource => resource.worktreeOf || resource.id) ?? [];
  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    onBusyChange(true);
    try {
      await action();
      await projects.refetch();
      await scope.refetch();
      onChange();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
      onBusyChange(false);
    }
  }
  async function bind(projectId: string | null, resourceIds: string[]) {
    await api(url, { projectId, resourceIds });
  }
  return (
    <section className="project-scope" aria-label="项目与资料范围">
      <h2>项目与资料范围</h2>
      <p>{t("项目主目录或当前工作树随会话权限档授权。额外关联的目录可单独选择和授权。")}</p>
      {(projects.error || scope.error) && (
        <p role="alert">{projects.error?.message || scope.error?.message}</p>
      )}
      <label>
        所属项目{" "}
        <select
          aria-label="所属项目"
          disabled={busy || scope.isPending}
          value={scope.data?.projectId || ""}
          onChange={(e) => {const p=projects.data?.projects.find(p=>p.id===e.target.value);
            const root=p?.resources[p.folderResourceId||''];
            void run(() => bind(p?.id||null,root?.permission==='read'?[root.id]:[]));}}
        >
          <option value="">独立对话</option>
          {projects.data?.projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      <button
        disabled={busy}
        onClick={() =>
          run(async () => {
            const p = await chooseFolderProject();
            if(p)await bind(p.id,p.folderResourceId?[p.folderResourceId]:[]);
          })
        }
      >
        选择项目文件夹
      </button>
      {project && (
        <>
          <label>
            资料类别{" "}
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="documents">资料目录</option>
              <option value="code">代码目录</option>
            </select>
          </label>
          <button
            disabled={busy}
            onClick={() =>
              run(async () => {
                const path = await window.workbench.chooseProject();
                if (path)
                  await api("/api/v2/projects/" + project.id + "/resources", {
                    path,
                    kind,
                  });
              })
            }
          >
            关联目录
          </button>
          {Object.values(project.resources).map((r) => {
            const active = scope.data?.resources.find(item => (item.worktreeOf || item.id) === r.id);
            const primary = r.id === project.folderResourceId;
            const inherits = !!active && scope.data?.checkoutAccess?.resourceId === active.id;
            return <div key={r.id}>
              <label>
                <input
                  type="checkbox"
                  disabled={
                    busy ||
                    (r.permission !== "read" &&
                      !selectedIds.includes(r.id))
                  }
                  checked={selectedIds.includes(r.id)}
                  onChange={(e) =>
                    run(() =>
                      bind(
                        project.id,
                        e.target.checked
                          ? [...selectedIds, r.id]
                          : selectedIds.filter((id) => id !== r.id),
                      ),
                    )
                  }
                />
                {t("在本会话使用 {name}", {name: r.name})} · {t(r.kind === "code" ? "代码" : "资料")} ·{" "}
                {inherits ? t("读写和运行随会话权限档") :
                  r.permission !== "read" ? t("尚未授权") : primary && !active ? t("尚未选入本会话") : t(r.write && r.execute ? "目录允许读取、修改和运行" :
                    r.write ? "目录允许读取和修改" : r.execute ? "目录允许读取和运行" : "目录仅允许读取")}
              </label>
              <small>{active?.path || r.path}</small>
              {r.kind === "code" && r.permission === "read" && !primary && (
                <details><summary>{t("目录权限")}</summary>
                  <label>
                    <input
                      type="checkbox"
                      disabled={busy}
                      checked={!!r.write}
                      onChange={(e) =>
                        run(() =>
                          api(
                            `/api/v2/projects/${project.id}/resources/${r.id}/permission`,
                            {
                              permission: "read",
                              write: e.target.checked,
                              confirm: true,
                            },
                          ),
                        )
                      }
                    />
                    允许 Agent 修改此代码目录
                  </label>
                  <label>
                    <input
                      type="checkbox"
                      disabled={busy}
                      checked={!!r.execute}
                      onChange={(e) =>
                        run(() =>
                          api(
                            `/api/v2/projects/${project.id}/resources/${r.id}/permission`,
                            {
                              permission: "read",
                              execute: e.target.checked,
                              confirm: true,
                            },
                          ),
                        )
                      }
                    />
                    {t("允许运行项目命令")}
                  </label>
                  <small>
                    {t("此目录设置适用于项目中选择它的会话。新增权限从下一轮生效，撤销立即生效。")}
                  </small>
                </details>
              )}
              <button
                disabled={busy}
                onClick={() =>
                  run(() =>
                    api(
                      "/api/v2/projects/" +
                        project.id +
                        "/resources/" +
                        r.id +
                        "/permission",
                      {
                        permission: r.permission === "read" ? "none" : "read",
                        confirm: true,
                      },
                    ),
                  )
                }
              >
                {primary
                  ? t(r.permission === "read" ? "撤销项目访问" : "按会话权限档允许访问项目")
                  : t(r.permission === "read" ? "撤销读取" : "允许读取此目录")}
              </button>
            </div>;
          })}
        </>
      )}
      <p>
        当前选择：{scope.data?.resources.map((r) => r.name).join("、") || "无"}
        。授权范围用于核对工具可以读取哪些资料。
      </p>
      {scope.data?.lastRun && (
        <p>
          上一轮可用资料：
          {scope.data.lastRun.resources.map((r) => r.name).join("、") || "无"}
          （范围记录不代表已读取）
        </p>
      )}
    </section>
  );
}
