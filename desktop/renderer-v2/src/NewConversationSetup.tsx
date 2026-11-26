// 准备新会话的项目、检出目录、权限和模型选择。
// 主要引用方包括 `AgentHome.tsx` 和 `ScheduledTasks.tsx`。
import { useUiLanguage } from "./UiLanguage";
import type { PermissionMode } from "./ComposerPermissions";
import { useRef, type ReactNode, type RefObject } from "react";
import type { Project } from "./generated";
import { groupSidebarConversations } from "./conversationSidebarGroups";
import { UiIcon } from "./UiIcon";
import { PendingMessageBubble, type PendingUserMessage } from "./ConversationTimeline";
import "./new-conversation-setup.css";

export type ConversationSetup = { projectId: string | null; title: string; workFolder: boolean };
export type CreationAttempt = {
  id: string;
  body: ConversationSetup & { permissionMode?: PermissionMode; confirmFullAccess?: boolean };
  worktreeResourceId?: string;
  firstMessage?: string;
};
export type SetupDraft = ConversationSetup & { permissionMode?: PermissionMode; worktreeResourceId?: string; attempt?: CreationAttempt };

export function projectCodeRoots(project?: Project) {
  return Object.values(project?.resources ?? {}).filter(root => root.kind === "code" && root.permission === "read" && !root.worktreeOf);
}

export function newChatWorktreeRoots(project?: Project) {
  const roots = projectCodeRoots(project);
  return project?.folderResourceId ? roots.filter(root => root.id === project.folderResourceId) : roots;
}

export function NewConversationHome({ draft, text, projects, heading, blockedReason, sendBlocked, busy, error, pendingMessage, onChange, onText, onConfirm, onReturn, onNewProject, permissions, model, children }: {
  draft: SetupDraft;
  text: string;
  projects: readonly Project[];
  heading: RefObject<HTMLHeadingElement | null>;
  blockedReason: string;
  sendBlocked: string;
  busy: boolean;
  error: string;
  pendingMessage?: PendingUserMessage;
  onChange: (value: SetupDraft) => void;
  onText: (value: string) => void;
  onConfirm: (sendFirst: boolean) => void;
  onReturn?: () => void;
  onNewProject: () => void;
  permissions: ReactNode;
  model: ReactNode;
  children?: ReactNode;
}) {
  const { t } = useUiLanguage();
  const composing = useRef(false);
  const settings = draft.attempt?.body ?? draft;
  const project = projects.find(item => item.id === settings.projectId);
  const roots = newChatWorktreeRoots(project);
  const worktree = (draft.attempt ? draft.attempt.worktreeResourceId : draft.worktreeResourceId) ?? "";
  const choices = groupSidebarConversations([], projects).projects;
  const feedback = busy ? t("正在准备会话，请稍候。") : blockedReason ||
    (draft.attempt ? t("创建结果待确认，草稿已保留。") : "");
  const sendDisabled = feedback || sendBlocked || (!text.trim() ? t("输入内容后发送。") : "");
  const shownFeedback = feedback || (text.trim() && sendBlocked);

  return <section className="new-conversation-home" aria-labelledby="new-conversation-title">
    {onReturn && <button type="button" className="new-conversation-return" title={t("返回上个会话")}
      aria-label={t("返回上个会话")} disabled={busy} onClick={onReturn}><UiIcon name="back"/></button>}
    <div className="new-conversation-welcome" data-sending={!!pendingMessage||undefined}>
      <h1 id="new-conversation-title" className={pendingMessage?"sr-only":undefined} ref={heading} tabIndex={-1}>{t("今天想做什么？")}</h1>
      {pendingMessage && <PendingMessageBubble message={pendingMessage}/>}
    </div>
    <form className="new-conversation-setup" aria-label={t("开始新会话")} aria-busy={busy}
      onCompositionStart={() => { composing.current = true; }}
      onCompositionEnd={() => { composing.current = false; }}
      onSubmit={event => {
        event.preventDefault();
        if (!composing.current && !sendDisabled) onConfirm(true);
      }}>
      {children}
      <fieldset disabled={busy || !!draft.attempt} className="new-conversation-destination">
        <legend className="sr-only">{t("会话归属和工作位置")}</legend>
        <label className="new-conversation-destination-item" title={t("会话归属")}>
          <UiIcon name="folder"/>
          <select aria-label={t("归属")} value={settings.projectId ?? ""} onChange={event => onChange({
            ...draft, projectId: event.target.value || null, worktreeResourceId: undefined,
          })}>
            <option value="">{t("普通会话")}</option>
            {settings.projectId && !project && <option value={settings.projectId} disabled>{t("项目暂未列出")}</option>}
            {choices.map(choice => <option key={choice.id} value={choice.id}>{choice.label}</option>)}
          </select>
        </label>
        <button type="button" className="new-conversation-destination-item new-conversation-project" onClick={onNewProject}
          title={t("在当前草稿中创建项目")}>
          <UiIcon name="plus"/>{t("新建项目")}
        </button>
        {settings.projectId ? <label className="new-conversation-destination-item" title={t("工作位置")}>
          <UiIcon name="branch"/>
          <select aria-label={t("工作位置")} value={worktree}
            onChange={event => onChange({ ...draft, worktreeResourceId: event.target.value || undefined })}>
            <option value="">{t("项目目录")}</option>
            {worktree && !roots.some(root => root.id === worktree) && <option value={worktree} disabled>{t("工作树来源暂不可用")}</option>}
            {roots.map(root => <option key={root.id} value={root.id}>{t("新建独立工作树")}{roots.length > 1 ? " · " + root.name : ""}</option>)}
          </select>
        </label> : <span className="new-conversation-destination-item"><UiIcon name="folder-open"/>{t("独立会话目录")}</span>}
      </fieldset>
      <div className="new-conversation-composer">
        <label className="sr-only" htmlFor="agent-composer">{t("首条消息")}</label>
        <textarea id="agent-composer" value={text} placeholder={t("输入第一条消息…")} onChange={event => onText(event.target.value)}
          onKeyDown={event => {
            if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || event.keyCode === 229 || composing.current) return;
            event.preventDefault();
            if (!sendDisabled) onConfirm(true);
          }}/>
        <div className="composer-toolbar new-conversation-toolbar">
          {permissions}
          {model}
          <button type="button" className="new-conversation-confirm" disabled={busy || !!blockedReason}
            title={draft.attempt ? undefined : t("创建会话并保留草稿")}
            aria-describedby={feedback ? "new-conversation-feedback" : undefined}
            onClick={() => { if (!composing.current) onConfirm(false); }}>
            {busy ? t("准备中…") : draft.attempt ? t("核对创建回执") : t("创建会话")}
          </button>
          <button id="go" className="primary" type="submit" aria-label={t("发送")} title={sendDisabled || t("发送并开始会话")}
            disabled={!!sendDisabled}><UiIcon name="send"/></button>
        </div>
        {error && <p className="new-conversation-feedback" role="alert">{error}</p>}
        <p className="new-conversation-feedback" id="new-conversation-feedback" role="status">{shownFeedback}</p>
      </div>
    </form>
  </section>;
}
