// 根据会话中的导入失败回执提供目录授权、SSH 身份和同源重试入口。
// 主要引用方包括 `AgentHome.tsx`。
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, APIError } from "./client";
import { parentEvent } from "./conversationProjection";
import type { ConversationScope, EventRow } from "./generated";
import { useUiLanguage } from "./UiLanguage";
import type { UiMessage } from "./uiMessages";
import { chooseWorkFolder, workFolderQuery } from "./workFolders";
import "./repository-import.css";
import {identityRoute, SshIdentityActions, type SshImportIssue} from "./SshIdentityActions";

const folderErrors = new Set(["destination_not_authorized", "import_destination_changed", "project_import_permission", "project_import_location"]);
const messages: Partial<Record<string, readonly [UiMessage, UiMessage]>> = {
  destination_not_authorized: ["先选择工作总目录", "请选择 Modot 和现有 Git 检出目录之外的本地文件夹。导入会在这里新建项目文件夹，并单独请求批准。"],
  import_destination_changed: ["导入位置已变化", "请核对工作总目录，再发起新的导入请求。"],
  project_import_permission: ["当前项目目录需要写入权限", "确认本会话已选中项目目录，在下一轮重试导入。"],
  project_import_location: ["当前项目目录无法用于导入", "请在项目资料中选择可写的本地项目根目录。"],
  ssh_access_denied: ["SSH 身份验证失败", "请选择已有身份，或在本机创建密钥并到托管平台登记公钥。"],
  ssh_host_untrusted: ["SSH 主机身份待核对", "请在本机 SSH 客户端核对托管方公布的主机指纹，然后重试。"],
  import_network: ["无法连接到仓库", "请核对仓库地址和网络连接，然后重试。"],
  ssh_repository_unavailable: ["无法通过 SSH 读取仓库", "请核对网络、仓库地址和账号权限，然后重试。"],
  repository_address_denied: ["仓库地址不在支持的网络范围内", "请核对仓库域名和网络设置，再发起新的导入请求。"],
  approval_denied: ["已拒绝这次导入", "仓库没有导入。再次发起请求时，需要重新批准。"],
  approval_revoked: ["这次导入批准已撤销", "请核对当前目录与权限。重新发起导入时，需要再次批准。"],
  approval_expired: ["这次导入批准已过期", "请重新发起导入，并核对新的批准请求。"],
  ssh_identity_locked: ["本机 SSH 身份不可用", "请解锁该身份，或选择另一个已获授权的身份。"],
  ssh_identity_unavailable: ["本机 SSH 身份不可用", "请重新选择本次仓库使用的身份。"],
  ssh_repository_path: ["托管方未提供该仓库", "请核对仓库地址和账号的读取权限。"],
};

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

export function repositoryImportIssue(events: readonly EventRow[]) {
  const isImport = (event: EventRow) => event.act === "action" && event.tool === "import_private_repository" &&
    event.role === "executor" && parentEvent(event);
  const attempts = events.filter(isImport);
  const last = attempts.at(-1);
  const code = record(last?.impact)?.code;
  if (last?.state !== "failed" || !last.run_id || !last.action_id || typeof code !== "string") return;
  const copy = Object.hasOwn(messages, code) ? messages[code] : undefined;
  if (!copy) return;
  const started = attempts.find(event => event.run_id === last.run_id && event.action_id === last.action_id && event.state === "running");
  const args = record(started?.args);
  const url = typeof args?.url === "string" && /^(?:git@|ssh:\/\/git@)\S+$/.test(args.url) ? args.url : undefined;
  const name = typeof args?.name === "string" ? args.name : undefined;
  return { runId: last.run_id, actionId: last.action_id, code, copy, url, name, ssh: code.startsWith("ssh_") || code === "import_network" };
}

type ImportHelpProps = {
  issue: NonNullable<ReturnType<typeof repositoryImportIssue>>;
  active: boolean;
  hasDraft: boolean;
  projectId?: string | null;
  sessionId: string;
  onSend: (text: string) => void;
  scope?: ConversationScope;
  onScope: () => void;
  onRetry: (text: string) => void;
};

export function RepositoryImportHelp(props: ImportHelpProps) {
  const { t } = useUiLanguage();
  const [open, setOpen] = useState(false);
  return <details className="repository-import-recovery" data-import-action={props.issue.actionId} data-history-run={props.issue.runId}
    onToggle={event => { if (event.target === event.currentTarget) setOpen(event.currentTarget.open); }}>
    <summary>{t("处理导入失败")}</summary>
    {open && <ImportReceipt {...props}/>}
  </details>;
}

function ImportReceipt(props: ImportHelpProps) {
  const { t } = useUiLanguage();
  const { issue, sessionId, active } = props;
  const saved = useQuery({
    queryKey: ["ssh-import-receipt", sessionId, issue.runId, issue.actionId],
    queryFn: () => api<{issue: SshImportIssue | null}>(identityRoute(sessionId)),
    enabled: issue.ssh && !active, gcTime: 0,
  });
  if (issue.ssh && !active) {
    if (saved.isPending) return <small role="status">{t("正在读取…")}</small>;
    if (saved.isError) return <p role="alert">{saved.error.message} <button type="button" onClick={() => void saved.refetch()}>{t("重试")}</button></p>;
    const latest = saved.data.issue;
    if (latest?.actionId !== issue.actionId || latest.runId !== issue.runId || latest.code !== issue.code)
      return <small>{t("这次导入已有更新的记录。")}</small>;
    return <ImportActions {...props} issue={{...issue, url: latest.url, name: latest.name}}/>;
  }
  return <ImportActions {...props}/>;
}

function ImportActions({ issue, active, hasDraft, projectId, sessionId, scope, onScope, onRetry, onSend }: ImportHelpProps) {
  const { t } = useUiLanguage();
  const needsFolder = folderErrors.has(issue.code);
  const sshFailure = !!issue.url && issue.ssh;
  const useWorkFolder = needsFolder && !projectId;
  const useProject = needsFolder && !!projectId;
  const checking = useProject && (!scope || scope.projectId !== projectId);
  const root = useProject && scope?.projectId === projectId ? scope?.resources.find(r =>
    (r.worktreeOf || r.id) === scope.folderResourceId && r.kind === "code" && r.permission === "read" &&
    (scope.checkoutAccess?.resourceId === r.id || r.write)) : undefined;
  const folders = useQuery({ ...workFolderQuery, enabled: useWorkFolder });
  const query = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<UiMessage>();
  const retryReady = !needsFolder || (useProject ? !!root : !!folders.data?.selectionRevision && !folders.isError);
  let [heading, description]: [UiMessage, UiMessage | undefined] = [...issue.copy];
  if (useProject) {
    if (checking) {
      heading = "正在核对资料范围…";
      description = undefined;
    } else if (issue.code === "project_import_location") {
      [heading, description] = issue.copy;
    } else if (root) {
      heading = "导入到当前项目";
      description = "仓库将放入此目录的新子文件夹，批准时可核对完整路径。";
    } else {
      [heading, description] = messages.project_import_permission!;
    }
  } else if (useWorkFolder && retryReady) {
    heading = "核对导入位置";
  }

  async function choose() {
    if (busy) return;
    setBusy(true);
    setError(undefined);
    try {
      const chosen = await chooseWorkFolder();
      if (chosen) {
        query.setQueryData(workFolderQuery.queryKey, chosen);
        await query.invalidateQueries({ queryKey: workFolderQuery.queryKey });
      }
    } catch (err) {
      setError(err instanceof APIError && ["invalid_path", "protected_runtime"].includes(err.code)
        ? "请选择 Modot 程序目录之外的现有本地文件夹。"
        : "无法保存工作总目录，请重试。");
    } finally { setBusy(false); }
  }

  function retry(immediate = false) {
    if (!issue.url || !retryReady || busy || active || hasDraft) return;
    const submit = immediate ? onSend : onRetry;
    submit(issue.name
      ? t("请重新导入私有仓库 {url}，项目文件夹名称为 {name}。", { url: issue.url, name: issue.name })
      : t("请重新导入私有仓库 {url}。", { url: issue.url }));
  }

  return <section className="repository-import-help" aria-label={t("私库导入")} aria-live="polite" data-import-error={issue.code}>
    <strong>{t(heading)}</strong>
    {root && <p className="repository-import-path"><code>{root.path}</code></p>}
    {description && <p>{t(description)}</p>}
    {useWorkFolder && retryReady && <p className="repository-import-path">
      {t("当前工作总目录")} <code>{folders.data?.path}</code>
    </p>}
    {error && <p role="alert">{t(error)}</p>}
    {sshFailure && ['ssh_access_denied','ssh_identity_locked','ssh_identity_unavailable','ssh_repository_unavailable','ssh_repository_path'].includes(issue.code) &&
      <SshIdentityActions key={sessionId+issue.actionId} sessionId={sessionId} actionId={issue.actionId} disabled={active}/>}
    <div className="repository-import-actions">
      {useProject && !checking && (!root || issue.code === "project_import_location") &&
        <button type="button" disabled={active} onClick={onScope}>{t("打开项目资料")}</button>}
      {useWorkFolder && <button type="button" disabled={busy} onClick={() => void choose()}>
        {t("选择工作总目录")}
      </button>}
      {issue.url && <button type="button" disabled={!retryReady || busy || active || hasDraft} onClick={() => retry(sshFailure)}>
        {t(sshFailure ? "重试同一仓库" : "填入重试请求")}
      </button>}
    </div>
    {retryReady && <small>{t(hasDraft && issue.url
      ? "请先发送或清空当前草稿，再填入重试请求。"
      : sshFailure ? "重试会在当前会话重新请求本次导入授权。" : issue.url ? "核对并发送重试请求后，Modot 会再次请你批准。"
        : "处理后可在这段对话重新发送导入请求。")}</small>}
  </section>;
}
