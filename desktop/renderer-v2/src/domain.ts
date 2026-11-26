// 定义工作区、打开文件和工作间界面状态，并集中处理界面状态更新。
// 主要引用方包括 `useThreadHistory.ts` 和 `AgentHome.tsx`。
import type { FileContent } from "./generated";
export type {
  Workspace,
  WorkspaceSummary,
  GitState,
  GitHead,
  GitChangesPage,
  Thread,
  FileContent,
  ProcessRun,
  EventRow,
} from "./generated";
// `FileContent` 是后端读回的文件版本；`draft` 是尚在编辑的文本，`workspaceId` 固定文件归属。
export interface OpenFile extends FileContent {
  workspaceId: string;
  draft: string;
  line?: number;
}
export type Layout = "develop" | "debug" | "review";
// 当前工作区与会话、编辑文件和布局都属于界面状态；`views` 保留每个工作区的最近会话与文件位置。
export interface UIState {
  workspaceId: string;
  threadId: string;
  layout: Layout;
  navigation: string;
  activeFile: string;
  rightVisible: boolean;
  views?: Record<string, { threadId: string; activeFile: string }>;
}
export type UIAction =
  | { type: "workspace"; id: string }
  | { type: "thread"; id: string }
  | { type: "layout"; layout: Layout }
  | { type: "navigation"; navigation: string }
  | { type: "file"; key: string }
  | { type: "right" };
// 切换工作区先保存当前阅读位置，再恢复目标工作区已保存的会话和文件。
export function reduceUI(state: UIState, action: UIAction): UIState {
  switch (action.type) {
    case "workspace": {
      const views = {
        ...state.views,
        [state.workspaceId]: {
          threadId: state.threadId,
          activeFile: state.activeFile,
        },
      };
      return {
        ...state,
        workspaceId: action.id,
        threadId: views[action.id]?.threadId || "",
        activeFile: views[action.id]?.activeFile || "",
        views,
      };
    }
    case "thread":
      return { ...state, threadId: action.id };
    case "layout":
      return {
        ...state,
        layout: action.layout,
        navigation: action.layout === "review" ? "changes" : state.navigation,
      };
    case "navigation":
      return { ...state, navigation: action.navigation };
    case "file":
      return { ...state, activeFile: action.key };
    case "right":
      return { ...state, rightVisible: !state.rightVisible };
  }
}
export const initialUI: UIState = {
  workspaceId: "",
  threadId: "",
  layout: "develop",
  navigation: "files",
  activeFile: "",
  rightVisible: true,
};
