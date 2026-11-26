import { api } from "./client";

type WorkFolders = {
  path: string;
  defaultPath: string;
  projects: string;
  conversations: string;
  selectionRevision?: string | null;
};

const route = "/api/v2/workroom/folders";
export const workFolderQuery = {
  queryKey: ["work-folders"],
  queryFn: () => api<WorkFolders>(route),
};

export async function chooseWorkFolder() {
  const path = await window.workbench.chooseProject("work-root");
  return path ? api<WorkFolders>(route, { path }) : null;
}
