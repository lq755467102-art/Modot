// 工作间借用主会话的时间线、输入和批准组件。切换挂载位置不控制 Agent 的运行。
// `App.tsx` 提供当前会话，`WorkroomConversation.tsx` 登记容器，`LegacyPage.tsx` 移动时间线和输入栏，`PermissionReview.tsx` 在登记的容器里显示审批。
import {createContext, useContext, useState, type Dispatch, type ReactNode, type SetStateAction} from 'react';

export type GitHubPageReference = {id:string; version:number};

// `owner` 标识主会话，`feed`、`composer`、`approval` 承接同一会话的时间线、输入栏与审批节点。
export type ConversationDock = {
  owner: string;
  expanded: boolean;
  feed: HTMLElement;
  composer: HTMLElement;
  approval: HTMLElement;
  openConversation: () => void;
  captureGitHubPage?: () => Promise<GitHubPageReference|null>;
  navigateGitHubPage?: (requestId:string) => Promise<void>;
};

const Destination = createContext<ConversationDock | null>(null);
const Register = createContext<Dispatch<SetStateAction<ConversationDock | null>>>(() => {});
// 对话布局与审批入口读取当前会话可用的挂载位置。
export const useConversationDock = () => useContext(Destination);
// 工作间登记或撤回挂载位置。登记方负责在切换会话或退出工作间时清理。
export const useRegisterConversationDock = () => useContext(Register);

// 弹出层或工作间偶尔先于会话切换完成。仅在登记的 `owner` 和当前会话一致时公开容器。
export function ConversationDockProvider({owner, children}: {owner: string; children: ReactNode}) {
  const [dock, setDock] = useState<ConversationDock | null>(null);
  return <Register.Provider value={setDock}><Destination.Provider value={dock?.owner === owner ? dock : null}>
    {children}
  </Destination.Provider></Register.Provider>;
}
