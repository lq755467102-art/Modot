// 工作间与格式化面板间的范围握手。`ProjectWorkroom.tsx` 提供绑定，`DocumentFormatting.tsx` 用它登记当前草稿。
import { createContext } from 'react';
// `conversation`、`project`、`resource` 指明会话与代码根归属，`scope` 是后端确认的范围版本。仅按项目打开时会话 ID 为空。
export type Scope = { conversation: string; project: string; resource: string; scope: string };
// 工作间尚未拿到已打开目录的回执时默认为空，格式化入口等绑定就绪后再出现。
export const FormattingScope = createContext<Scope | null>(null);
