// 以项目身份对会话分组。同名项目使用身份后缀区分显示。
// 主要引用方包括 `ConversationSidebar.tsx` 和 `NewConversationSetup.tsx`。
import type { Conversation, Project } from "./generated";

type ProjectGroup = {
  id: string;
  name: string;
  label: string;
  known: boolean;
  removed: boolean;
  rows: Conversation[];
};

export function groupSidebarConversations(threads: readonly Conversation[], projects: readonly Project[]): {
  ordinary: Conversation[];
  projects: ProjectGroup[];
} {
  const ordinary: Conversation[] = [];
  const byProject = new Map<string, Omit<ProjectGroup, "label">>(
    projects.map(project => [project.id, {
      id: project.id, name: project.name, known: true, removed: !!project.removed, rows: [],
    }]),
  );
  for (const thread of threads) {
    if (!thread.projectId) {
      ordinary.push(thread);
      continue;
    }
    let group = byProject.get(thread.projectId);
    if (!group) {
      group = { id: thread.projectId, name: "", known: false, removed: false, rows: [] };
      byProject.set(group.id, group);
    }
    if (!group.name && thread.projectName) group.name = thread.projectName;
    group.rows.push(thread);
  }

  const counts = new Map<string, number>();
  const groups = [...byProject.values()];
  for (const group of groups) {
    group.name ||= "项目名称待核对";
    counts.set(group.name, (counts.get(group.name) ?? 0) + 1);
  }
  const shortId = (id: string) => id.length > 8 ? "…" + id.slice(-6) : id;
  return {
    ordinary,
    projects: groups.map(group => {
      if (counts.get(group.name) === 1) return { ...group, label: group.name };
      const short = shortId(group.id);
      const duplicate = groups.some(other => other !== group && other.name === group.name && shortId(other.id) === short);
      const identity = duplicate ? group.id : short;
      return { ...group, label: group.name + " · " + identity };
    }),
  };
}

export function sidebarConversationsInScope(threads: readonly Conversation[], archived: boolean, query: string, projectName = "") {
  const pinned: Conversation[] = [];
  const recent: Conversation[] = [];
  for (const thread of threads) {
    if (!!thread.archived !== archived) continue;
    const names = [projectName, thread.projectName ?? ""];
    if (query && !names.some(name => `${thread.title} ${name}`.toLocaleLowerCase().includes(query))) continue;
    (thread.pinned ? pinned : recent).push(thread);
  }
  return [...pinned, ...recent];
}
