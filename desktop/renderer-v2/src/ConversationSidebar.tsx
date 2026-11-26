// 按项目 ID 组织会话入口，项目内保留置顶、搜索、改名与归档。
// 主要引用方包括 `AgentHome.tsx`。
import { useUiLanguage } from "./UiLanguage";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { UiIcon } from "./UiIcon";
import type { Conversation, Project } from "./generated";
import { api, loadJSON, queryString, saveJSON } from "./client";
import { groupSidebarConversations, sidebarConversationsInScope } from "./conversationSidebarGroups";
import "./sidebar-interactions.css";

// revision 来自列表中的组织版本，token 仅标识当前界面编辑。
type RenameDraft = { id: string; title: string; revision: number; token: number };
const scopes = [["all", "全部会话"], ["project", "仅项目会话"], ["ordinary", "仅普通会话"]] as const;
type SidebarScope = typeof scopes[number][0];
const expansionKey = "conversation-project-expansion";
const emptyProjects: readonly Project[] = [];
const rowSelector = (id: string) => '[data-conversation-id="' + CSS.escape(id) + '"]';
const projectSelector = (id: string) => '[data-project-group="' + CSS.escape(id) + '"]';
const ordinaryHeading = "#sidebar-ordinary-heading";
const projectHeading = "#sidebar-projects-heading";
const menuSelector = 'details[name="conversation-sidebar-menus"][open]';

function initialExpansion() {
  const saved = loadJSON<unknown>(expansionKey, {});
  return new Map<string, boolean>(
    saved && typeof saved === "object" && !Array.isArray(saved)
      ? Object.entries(saved).filter((entry): entry is [string, boolean] => typeof entry[1] === "boolean")
      : [],
  );
}

function threadStatus(thread: Conversation): { state: string; text: import('./uiMessages').UiMessage } | undefined {
  const runtime = thread.runtime;
  if (runtime?.state === "waiting_approval" || runtime?.state === "needs_human") return { state: "waiting", text: "待回应" };
  if (runtime?.state === "unknown") return { state: "unknown", text: "状态待核对" };
  if (runtime?.active) return { state: "running", text: "运行中" };
  if (runtime?.state === "failed") return { state: "failed", text: "运行失败" };
  if (runtime?.state === "interrupted") return { state: "stopped", text: "已停止" };
}

function StatusDot({ state, text }: { state: string; text: string }) {
  return <span className="sidebar-status-dot" data-state={state} title={text} aria-hidden="true"/>;
}

export function ConversationSidebar({
  threads, selected, onSelect, onStartNewChat, revealedChat, scopeBusy, newChatBlockedReason, seen, onChanged,
  error, onSkills, entries, projects = emptyProjects, removedProjects = emptyProjects,
  onManageProjects, onRemoveProject, onRestoreProject, onCloseDrawer, conversationActive = true,
}: {
  threads: Conversation[];
  selected: string;
  onSelect: (id: string) => void;
  onStartNewChat: (projectId: string | null) => void;
  revealedChat?: Pick<Conversation, "id" | "projectId">;
  scopeBusy: boolean;
  newChatBlockedReason: string;
  seen: Record<string, number>;
  onChanged: () => Promise<unknown>;
  error?: string;
  onSkills?: () => void;
  entries?: ReactNode;
  projects?: readonly Project[];
  removedProjects?: readonly Project[];
  onManageProjects?: () => void;
  onRemoveProject: (project: Project) => void;
  onRestoreProject: (project: Project) => Promise<void>;
  onCloseDrawer?: () => void;
  conversationActive?: boolean;
}) {
  const { t, locale } = useUiLanguage();
  const [search, setSearch] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchProject, setSearchProject] = useState<string | null>(null);
  const [scope, setScope] = useState<SidebarScope>("all");
  const [archived, setArchived] = useState(false);
  const [removedOpen, setRemovedOpen] = useState(false);
  const [restoringProject, setRestoringProject] = useState("");
  const [restoreError, setRestoreError] = useState("");
  const [ordinaryExpanded, setOrdinaryExpanded] = useState(false);
  const [expansion, setExpansion] = useState(initialExpansion);
  const [searchCollapsed, setSearchCollapsed] = useState<Set<string>>(() => new Set());
  const [editing, setEditing] = useState<RenameDraft | null>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const pendingForks=useRef(new Set<string>());
  const currentSelection=useRef(selected);
  currentSelection.current=selected;
  const searchField = useRef<HTMLInputElement>(null);
  const searchButton = useRef<HTMLButtonElement>(null);
  const searchOpener = useRef<HTMLElement | null>(null);
  const listMenu = useRef<HTMLDetailsElement>(null);
  const sidebar = useRef<HTMLElement>(null);
  const searchComposing = useRef(false);
  const renameField = useRef<HTMLInputElement>(null);
  const renameComposing = useRef(false);
  const renameDraft = useRef<RenameDraft | null>(null);
  const renameToken = useRef(0);
  const renamePending = useRef(new Set<string>());
  const newChatStatusId = "sidebar-new-chat-status";
  const newChatDescription = newChatBlockedReason ? newChatStatusId : undefined;
  const current = threads.find(thread => thread.id === selected);
  const query = search.trim().toLocaleLowerCase();
  const activeScope = searchProject === null ? scope : "project";
  const groups = useMemo(() => groupSidebarConversations(threads, [...projects, ...removedProjects]), [threads, projects, removedProjects]);
  const scoped = useMemo(() => {
    const ordinary = activeScope === "project" ? [] : sidebarConversationsInScope(groups.ordinary, archived, query);
    const projectSections = activeScope === "ordinary" ? [] : groups.projects
      .filter(project => searchProject === null || project.id === searchProject)
      .map(project => ({ ...project, matches: sidebarConversationsInScope(project.rows, archived, query, project.name) }))
      .filter(project => project.matches.length || (!archived && !query && project.known) || project.id === searchProject);
    return { ordinary, projectSections };
  }, [activeScope, archived, groups, query, searchProject]);
  const limitOrdinary = scope === "all" && !archived && !query && !ordinaryExpanded;
  const visibleOrdinary = limitOrdinary ? scoped.ordinary.filter((thread, index) => index < 6 || thread.id === selected) : scoped.ordinary;
  const listedProjects = scoped.projectSections.filter(project => !project.removed);
  const removedSections = scoped.projectSections.filter(project => project.removed);
  const matchingRemoved = !!query || removedSections.some(project => project.id === searchProject);
  const showRemoved = removedOpen || matchingRemoved;
  const matchCount = scoped.ordinary.length + scoped.projectSections.reduce(
    (count, project) => count + (!project.removed || showRemoved ? project.matches.length : 0), 0);
  const searchedProject = groups.projects.find(project => project.id === searchProject);

  useEffect(() => {
    function dismissOutside(event: Event) {
      if (!(event.target instanceof Node)) return;
      const target = event.target;
      for (const menu of sidebar.current?.querySelectorAll<HTMLDetailsElement>(menuSelector) ?? []) {
        if (!menu.contains(target)) menu.open = false;
      }
    }
    document.addEventListener("pointerdown", dismissOutside, true);
    document.addEventListener("focusin", dismissOutside);
    return () => {
      document.removeEventListener("pointerdown", dismissOutside, true);
      document.removeEventListener("focusin", dismissOutside);
    };
  }, []);
  useEffect(() => saveJSON(expansionKey, Object.fromEntries(expansion)), [expansion]);
  useEffect(() => {
    if (revealedChat?.id !== selected) return;
    const projectId = revealedChat.projectId;
    setArchived(false);
    setScope(previous => (projectId && previous === "ordinary") || (!projectId && previous === "project") ? "all" : previous);
    closeSearch(false);
    if (projectId) {
      rememberExpansion(projectId, true);
      if (removedProjects.some(project => project.id === projectId)) setRemovedOpen(true);
    }
  }, [revealedChat]);
  useLayoutEffect(() => {
    if (editing) renameField.current?.focus();
  }, [editing?.token]);

  function visibleElement(selector: string) {
    const element = sidebar.current?.querySelector<HTMLElement>(selector);
    return element?.getClientRects().length && !element.matches(":disabled") ? element : undefined;
  }

  function focusAfterRender(target: () => HTMLElement | null | undefined) {
    requestAnimationFrame(() => {
      if (sidebar.current?.getClientRects().length && !sidebar.current.closest("[inert]")) target()?.focus();
    });
  }

  function setRename(value: RenameDraft | null) {
    renameDraft.current = value;
    setEditing(value);
  }

  function beginRename(thread: Conversation) {
    renameComposing.current = false;
    setErrors(old => ({ ...old, [thread.id]: "" }));
    setRename({ id: thread.id, title: thread.title, revision: thread.organizationRevision || 1, token: ++renameToken.current });
  }

  function finishRename(token: number, restore = true) {
    const draft = renameDraft.current;
    if (draft?.token !== token) return;
    renameComposing.current = false;
    setRename(null);
    if (restore) focusAfterRender(() => visibleElement(rowSelector(draft.id) + " summary") ?? searchField.current ?? searchButton.current);
  }

  // 旧保存回执只能结束发起请求时的编辑。
  async function saveRename(draft: RenameDraft) {
    const owner = threads.find(thread => thread.id === draft.id);
    if (!owner || renamePending.current.has(draft.id) || (owner.organizationRevision || 1) !== draft.revision || !draft.title.trim()) return;
    renamePending.current.add(draft.id);
    setBusy(old => ({ ...old, [draft.id]: true }));
    try {
      await api("/api/v2/conversations/threads/" + queryString(draft.id) + "/organization", { title: draft.title, expectedRevision: draft.revision });
      await onChanged();
      setErrors(old => ({ ...old, [draft.id]: "" }));
      if (renameDraft.current?.token === draft.token) {
        const restore = document.activeElement === document.body || !!renameField.current?.closest("form")?.contains(document.activeElement);
        finishRename(draft.token, restore);
      }
    } catch (failure) {
      setErrors(old => ({ ...old, [draft.id]: (failure as Error).message }));
      await onChanged().catch(() => {});
    } finally {
      renamePending.current.delete(draft.id);
      setBusy(old => ({ ...old, [draft.id]: false }));
    }
  }

  function updateSearch(value: string) {
    setSearch(value);
    setSearchCollapsed(new Set());
  }

  function openSearch(opener: HTMLElement, projectId: string | null = null) {
    searchOpener.current = opener;
    if (!searchOpen || searchProject !== projectId) updateSearch("");
    setSearchProject(projectId);
    setSearchOpen(true);
    focusAfterRender(() => searchField.current);
  }

  function closeSearch(restore = true) {
    const opener = searchOpener.current;
    searchOpener.current = null;
    searchComposing.current = false;
    setSearchOpen(false);
    setSearchProject(null);
    updateSearch("");
    if (restore) focusAfterRender(() => opener?.getClientRects().length ? opener : searchButton.current);
  }

  function projectExpanded(id: string) {
    return query ? !searchCollapsed.has(id) : (expansion.get(id) ?? (current?.projectId === id));
  }

  function rememberExpansion(id: string, expanded: boolean) {
    setExpansion(old => new Map(old).set(id, expanded));
  }

  function toggleProject(id: string) {
    const expanded = projectExpanded(id);
    if (query) {
      setSearchCollapsed(old => {
        const next = new Set(old);
        if (expanded) next.add(id);
        else next.delete(id);
        return next;
      });
    } else {
      rememberExpansion(id, !expanded);
    }
  }

  function closeMenu(menu: HTMLDetailsElement | null, restore = true) {
    if (!menu) return;
    menu.open = false;
    const summary = menu.querySelector<HTMLElement>("summary");
    if (restore) summary?.focus();
    return summary;
  }

  function locateCurrent() {
    if (!current) return;
    closeMenu(listMenu.current, false);
    closeSearch(false);
    setScope("all");
    setArchived(!!current.archived);
    if (current.projectId) {
      rememberExpansion(current.projectId, true);
      if (removedProjects.some(project => project.id === current.projectId)) setRemovedOpen(true);
    }
    focusAfterRender(() => visibleElement(rowSelector(current.id) + " > button") ?? searchButton.current);
  }

  async function fork(thread:Conversation, button:HTMLButtonElement) {
    if(busy[thread.id]||pendingForks.current.has(thread.id))return;
    pendingForks.current.add(thread.id);
    const startedFrom=currentSelection.current;
    closeMenu(button.closest("details"),false);
    setBusy(old=>({...old,[thread.id]:true}));
    try {
      const child=await api<Conversation>("/api/v2/conversations/threads/"+queryString(thread.id)+"/fork",{});
      await onChanged();
      if(currentSelection.current===startedFrom)onSelect(child.id);
    } catch(error) {
      setErrors(old=>({...old,[thread.id]:(error as Error).message}));
    } finally {pendingForks.current.delete(thread.id);setBusy(old=>({...old,[thread.id]:false}));}
  }

  async function change(thread: Conversation, patch: Record<string, unknown>, button: HTMLButtonElement, archive = false) {
    if (busy[thread.id]) return;
    const trigger = closeMenu(button.closest("details"));
    const row = trigger?.closest<HTMLElement>("[data-conversation-id]");
    const adjacent = [row?.nextElementSibling, row?.previousElementSibling]
      .map(element => element?.getAttribute("data-conversation-id"))
      .filter((id): id is string => !!id);
    setBusy(old => ({ ...old, [thread.id]: true }));
    try {
      await api("/api/v2/conversations/threads/" + queryString(thread.id) + (archive ? "/archive" : "/organization"),
        archive ? patch : { ...patch, expectedRevision: thread.organizationRevision || 1 });
      await onChanged();
      setErrors(old => ({ ...old, [thread.id]: "" }));
      focusAfterRender(() => {
        if (document.activeElement !== trigger && document.activeElement !== document.body) return;
        return visibleElement(rowSelector(thread.id) + " summary")
          ?? adjacent.map(id => visibleElement(rowSelector(id) + " > button")).find(Boolean)
          ?? visibleElement(thread.projectId ? projectSelector(thread.projectId) + " .sidebar-project-toggle" : ordinaryHeading)
          ?? visibleElement(projectHeading)
          ?? searchButton.current;
      });
    } catch (failure) {
      setErrors(old => ({ ...old, [thread.id]: (failure as Error).message }));
      await onChanged().catch(() => {});
    } finally {
      setBusy(old => ({ ...old, [thread.id]: false }));
    }
  }

  async function restoreProject(project: Project) {
    if (restoringProject) return;
    setRestoringProject(project.id);
    setRestoreError("");
    try {
      await onRestoreProject(project);
      focusAfterRender(() => document.activeElement === document.body
        ? visibleElement(projectSelector(project.id) + " .sidebar-project-toggle")
        : undefined);
    } catch (failure) {
      setRestoreError((failure as Error).message);
    } finally {
      setRestoringProject("");
    }
  }

  function chooseScope(value: SidebarScope) {
    setScope(value);
    setSearchProject(null);
    setSearchCollapsed(new Set());
  }

  function handleEscape(event: KeyboardEvent<HTMLElement>) {
    if (event.key !== "Escape" || event.defaultPrevented || event.nativeEvent.isComposing || event.keyCode === 229 ||
      searchComposing.current || renameComposing.current) return;
    const menu = event.currentTarget.querySelector<HTMLDetailsElement>(menuSelector);
    if (menu) closeMenu(menu);
    else if (searchOpen) closeSearch();
    else return;
    event.preventDefault();
    event.stopPropagation();
  }

  function renderRow(thread: Conversation) {
    const runtime = threadStatus(thread);
    const lastSeen = seen[thread.id] || 0;
    const cursor = thread.eventCursor ?? 0;
    const record = thread.eventCursor === null || cursor < lastSeen ? { state: "unknown", text: "记录待核对" as const }
      : cursor > lastSeen ? { state: "unread", text: "有新进展" as const } : undefined;
    const dot = runtime ?? record;
    const status = [runtime && t(runtime.text), record && t(record.text), thread.pinned && t("已置顶")].filter(Boolean).join(locale === "en" ? ", " : "，");
    const statusId = status ? "sidebar-thread-status-" + encodeURIComponent(thread.id) : undefined;
    const archivedThread = !!thread.archived;
    return <li key={thread.id} className="conversation-row" data-conversation-id={thread.id} data-pinned={thread.pinned || undefined}>
      <button type="button" disabled={scopeBusy} aria-current={conversationActive && selected === thread.id ? "page" : undefined} aria-describedby={statusId} onClick={() => onSelect(thread.id)}>
        <span className="sidebar-conversation-title" title={thread.title}>{thread.title}</span>
        {thread.scheduledPlanId && <span className="sidebar-schedule-badge" title={t("定时任务")}><UiIcon name="calendar"/>{t("定时任务")}</span>}
        {dot && <StatusDot state={dot.state} text={status}/>}
      </button>
      {statusId && <span id={statusId} hidden>{status}</span>}
      {!thread.subtaskOf && <details className="conversation-menu" name="conversation-sidebar-menus">
        <summary className="sidebar-quiet-action" aria-label={t("管理会话 {name}",{name:thread.title})} title={t("管理会话")}><UiIcon name="more"/></summary>
        <button type="button" disabled={busy[thread.id]} onClick={event => void change(thread, { pinned: !thread.pinned }, event.currentTarget)}>
          {thread.pinned ? t("取消置顶") : t("置顶")}
        </button>
        <button type="button" disabled={busy[thread.id]} onClick={event => { closeMenu(event.currentTarget.closest("details"), false); beginRename(thread); }}>
          {t("重命名")}
        </button>
        {!archivedThread&&!thread.readonlyEvidence&&<button type="button" disabled={busy[thread.id]||scopeBusy}
          onClick={e=>void fork(thread,e.currentTarget)}>{t("从此会话分支")}</button>}
        <button type="button" disabled={busy[thread.id] || !!thread.runtime?.active}
          onClick={event => void change(thread, { archived: !archivedThread }, event.currentTarget, true)}
          title="归档保留历史和草稿；运行中不可归档，已有子任务和调试按原生命周期结束">
          {archivedThread ? t("恢复对话") : t("归档")}
        </button>
      </details>}
      {editing?.id === thread.id && <form className="conversation-rename" aria-label={t("重命名会话")}
        onSubmit={event => { event.preventDefault(); const draft = renameDraft.current; if (draft && !renameComposing.current) void saveRename(draft); }}
        onCompositionStart={() => { renameComposing.current = true; }} onCompositionEnd={() => { renameComposing.current = false; }}
        onKeyDown={event => {
          if (event.key !== "Escape" && event.key !== "Enter") return;
          if (event.nativeEvent.isComposing || event.keyCode === 229 || renameComposing.current) {
            event.preventDefault(); event.stopPropagation(); return;
          }
          if (event.key === "Escape") {
            event.preventDefault(); event.stopPropagation();
            if (!busy[thread.id]) finishRename(editing.token);
          }
        }}>
        <input ref={renameField} aria-label={t("会话新名称")} value={editing.title} disabled={busy[thread.id]} maxLength={100} required
          onChange={event => { const draft = renameDraft.current; if (draft?.id === thread.id) setRename({ ...draft, title: event.target.value }); }}/>
        {editing.revision !== (thread.organizationRevision || 1) && <p role="alert">名称已更新，未保存的文字已保留。请重新载入后核对。</p>}
        <button disabled={busy[thread.id] || !editing.title.trim() || editing.revision !== (thread.organizationRevision || 1)}>{t("保存名称")}</button>
        {editing.revision !== (thread.organizationRevision || 1) && <button type="button" disabled={busy[thread.id] || renamePending.current.has(thread.id)}
          onClick={() => beginRename(thread)}>重新载入名称</button>}
        <button type="button" disabled={busy[thread.id]} onClick={() => finishRename(editing.token)}>{t("取消")}</button>
      </form>}
      {errors[thread.id] && <p role="alert">{errors[thread.id]}</p>}
    </li>;
  }

  function renderProject(project: typeof scoped.projectSections[number]) {
    const expanded = projectExpanded(project.id);
    const record = (project.removed ? removedProjects : projects).find(item => item.id === project.id);
    const listId = "sidebar-project-" + encodeURIComponent(project.id);
    const hasCurrent = conversationActive && !expanded && project.matches.some(thread => thread.id === selected);
    const hasWaiting = !expanded && project.matches.some(thread => threadStatus(thread)?.state === "waiting");
    const indicators = hasCurrent || hasWaiting ? listId + "-state" : undefined;
    return <section key={project.id} className="sidebar-project-group" data-project-group={project.id} aria-label={project.label}>
      <div className="sidebar-project-heading">
        <h3>
          <button type="button" className="sidebar-project-toggle" aria-label={project.label} aria-expanded={expanded} aria-controls={listId}
            aria-describedby={indicators} data-contains-current={hasCurrent || undefined} title={project.label} onClick={() => toggleProject(project.id)}>
            <UiIcon name={expanded ? "folder-open" : "folder"}/>
            <span className="sidebar-project-name">{project.name}</span>
            {hasWaiting && <StatusDot state="waiting" text={t("项目内有会话待回应")}/>}
          </button>
        </h3>
        <details className="sidebar-project-menu" name="conversation-sidebar-menus">
          <summary className="sidebar-quiet-action" aria-label={t("项目操作 {name}", {name: project.label})} title={t("项目操作")}><UiIcon name="more"/></summary>
          <div className="sidebar-list-actions">
            <button type="button" aria-label={t("搜索项目 {name}", {name: project.label})} onClick={event => {
              const trigger = closeMenu(event.currentTarget.closest("details"), false);
              if (trigger) openSearch(trigger, project.id);
            }}>{t("搜索会话")}</button>
            {!archived && project.known && !project.removed && <button type="button" aria-label={t("在项目 {name} 新建聊天", {name: project.label})}
              title={newChatBlockedReason || t("打开预选此项目的新聊天设置")} disabled={!!newChatBlockedReason} aria-describedby={newChatDescription}
              onClick={event => {
                closeMenu(event.currentTarget.closest("details"), false);
                onStartNewChat(project.id);
              }}>{t("新建聊天")}</button>}
            {record && (project.removed
              ? <button type="button" disabled={!!restoringProject} onClick={event => {
                  closeMenu(event.currentTarget.closest("details"));
                  void restoreProject(record);
                }}>{t("恢复项目")}</button>
              : <button type="button" onClick={event => {
                  closeMenu(event.currentTarget.closest("details"));
                  onRemoveProject(record);
                }}>{t("从 Modot 移除项目")}</button>)}
          </div>
        </details>
      </div>
      {indicators && <span id={indicators} hidden>{[hasCurrent && "含当前会话", hasWaiting && "有会话待回应"].filter(Boolean).join("，")}</span>}
      <div className="sidebar-project-content" id={listId} hidden={!expanded}>
        {expanded && (project.matches.length ? <ul className="sidebar-conversation-rows">
          {project.matches.map(renderRow)}
        </ul> : <p className="sidebar-section-empty">{query ? t("没有匹配的会话") : t("暂无会话")}</p>)}
      </div>
    </section>;
  }

  return <nav ref={sidebar} className="col agent-sidebar-list" aria-label={t("会话侧栏")} onKeyDown={handleEscape}>
    <header className="sidebar-controls">
      <div className="sidebar-header">
        <strong className="sidebar-brand">Modot</strong>
        <button ref={searchButton} type="button" className="sidebar-icon sidebar-quiet-action" title={t("搜索会话")} aria-label={t("搜索会话")}
          aria-expanded={searchOpen && !searchProject} aria-controls={searchOpen ? "sidebar-search-panel" : undefined}
          onClick={event => searchOpen && !searchProject ? closeSearch() : openSearch(event.currentTarget)}><UiIcon name="search"/></button>
        <details className="sidebar-list-menu" ref={listMenu} name="conversation-sidebar-menus">
          <summary title={t("会话列表选项")} aria-label={t("会话列表选项")}><UiIcon name="more"/></summary>
          <div className="sidebar-list-actions">
            <button type="button" onClick={locateCurrent} disabled={!current}>{t("定位当前会话")}</button>
            <button type="button" className="archive-filter" aria-pressed={archived} onClick={() => {
              setArchived(!archived);
              setSearchCollapsed(new Set());
              closeMenu(listMenu.current);
            }}>{archived ? t("返回对话") : t("显示归档")}</button>
            <fieldset className="sidebar-conversation-scopes">
              <legend>{t("会话范围")}</legend>
              {scopes.map(([key, label]) => <label key={key}>
                <input type="radio" name="conversation-sidebar-scope" value={key} checked={activeScope === key}
                  onChange={() => chooseScope(key)}
                  onClick={event => { if (event.detail > 0) closeMenu(listMenu.current); }}
                  onKeyDown={event => {
                    if (event.nativeEvent.isComposing || !["Enter", " "].includes(event.key)) return;
                    event.preventDefault();
                    chooseScope(key);
                    closeMenu(listMenu.current);
                  }}/>{t(label)}
              </label>)}
            </fieldset>
          </div>
        </details>
        {onCloseDrawer && <button type="button" className="sidebar-icon sidebar-drawer-close" aria-label={t("关闭会话侧栏")} title={t("关闭会话侧栏")}
          onClick={onCloseDrawer}><UiIcon name="close"/></button>}
      </div>
      {searchOpen && <div id="sidebar-search-panel">
        {searchProject && <p className="sidebar-search-scope" title={searchedProject?.label}>项目内搜索 · {searchedProject?.name ?? "项目名称待核对"}</p>}
        <div className="ssearch conversation-search">
          <input ref={searchField} id="sessq" aria-label={t("搜索对话")} value={search} onChange={event => updateSearch(event.target.value)}
            onCompositionStart={() => { searchComposing.current = true; }} onCompositionEnd={() => { searchComposing.current = false; }}
            placeholder={searchProject ? t("搜索项目会话") : t("搜索会话")}/>
          {search && <button type="button" aria-label={t("清除搜索条件")} title={t("清除搜索条件")} onClick={() => {
            updateSearch(""); searchField.current?.focus();
          }}><UiIcon name="reload"/></button>}
          <button type="button" aria-label={t("关闭搜索")} title={t("关闭搜索")} onClick={() => closeSearch()}><UiIcon name="close"/></button>
        </div>
        <p className="sidebar-search-feedback" role="status" aria-atomic="true">
          {matchCount ? (archived ? "已归档 · " : "") + matchCount + " 个结果" : "没有匹配的会话。"}
        </p>
      </div>}
    </header>
    <button id="newchat" className="sitem" type="button" title={newChatBlockedReason || t("设置新聊天的归属和草稿")}
      aria-current={conversationActive && !selected ? "page" : undefined} disabled={!!newChatBlockedReason} aria-describedby={newChatDescription} onClick={()=>onStartNewChat(null)}>
      <UiIcon name="compose"/><span>{t("新聊天")}</span>
    </button>
    <p id={newChatStatusId} className="sidebar-new-chat-status" role="status">{newChatBlockedReason}</p>
    {entries}
    {onSkills && <button id="openskills" className="sitem" type="button" onClick={onSkills}>{t("装了什么")}</button>}
    {archived && <small className="sidebar-archive-state">{!query && !matchCount && !error ? "暂无已归档的会话" : t("已归档的会话")}</small>}

    {activeScope !== "ordinary" && <section className="conversation-group" data-conversation-section="projects" aria-labelledby="sidebar-projects-heading">
      <div className="sidebar-section-heading">
        <h2 id="sidebar-projects-heading" tabIndex={-1}>{t("项目")}</h2>
        {onManageProjects && !archived && <button id="addproj" className="sidebar-icon sidebar-quiet-action" type="button" aria-label={t("新建项目")} title={t("新建项目")} disabled={scopeBusy || !!newChatBlockedReason}
          onClick={onManageProjects}><UiIcon name="plus"/></button>}
      </div>
      {listedProjects.map(renderProject)}
      {removedSections.length > 0 && <div className="sidebar-removed-projects">
        <button type="button" className="sidebar-show-ordinary" aria-expanded={showRemoved} aria-controls="sidebar-removed-projects"
          disabled={matchingRemoved} onClick={() => setRemovedOpen(!removedOpen)}>{t("已移除的项目")}</button>
        <div id="sidebar-removed-projects" hidden={!showRemoved}>{showRemoved && removedSections.map(renderProject)}</div>
      </div>}
      {!listedProjects.length && !removedSections.length && <p className="sidebar-section-empty">{query ? t("没有匹配的项目会话") : t("暂无项目会话")}</p>}
    </section>}
    {activeScope !== "project" && <section className="conversation-group" data-conversation-section="ordinary" aria-labelledby="sidebar-ordinary-heading">
      <h2 id="sidebar-ordinary-heading" tabIndex={-1}>{t("普通会话")}</h2>
      {visibleOrdinary.length ? <ul className="sidebar-conversation-rows" id="sidebar-ordinary-rows">
        {visibleOrdinary.map(renderRow)}
      </ul> : <p className="sidebar-section-empty">{t("暂无普通会话")}</p>}
      {scope === "all" && !archived && !query && scoped.ordinary.length > 6 &&
        <button type="button" className="sidebar-show-ordinary" aria-expanded={ordinaryExpanded} aria-controls="sidebar-ordinary-rows"
          onClick={() => setOrdinaryExpanded(!ordinaryExpanded)}>{ordinaryExpanded ? t("收起普通会话") : t("查看全部普通会话")}</button>}
    </section>}
    {restoreError && <p role="alert">{restoreError}</p>}
    {error && <p role="alert">{error}</p>}
  </nav>;
}
