// 汇集工作间与会话共用的面板组件，并提供面板故障边界。
// 主要引用方包括 `App.tsx` 和 `ConversationSideTabs.tsx`。
import {
  Component,
  lazy,
  Suspense,
  type ComponentProps,
  type ReactNode,
} from "react";
import { Message as MarkdownMessage } from "./Message";
const Terminal = lazy(() =>
  import("./TerminalPane").then((m) => ({ default: m.TerminalPane })),
);
const Debug = lazy(() =>
  import("./DebugPane").then((m) => ({ default: m.DebugPane })),
);
const Editor = lazy(() =>
  import("./EditorView").then((m) => ({ default: m.EditorView })),
);
export function Message(props: ComponentProps<typeof MarkdownMessage>) {
  return (
    <PanelBoundary>
      <MarkdownMessage {...props} />
    </PanelBoundary>
  );
}
export function EditorView(
  props: ComponentProps<typeof import("./EditorView").EditorView>,
) {
  return (
    <PanelBoundary>
      <Suspense fallback={<p>加载编辑器…</p>}>
        <Editor {...props} />
      </Suspense>
    </PanelBoundary>
  );
}
export class PanelBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <div role="alert" className="error">
        此面板渲染失败，后台运行不受影响。
        <button onClick={() => this.setState({ failed: false })}>
          重建面板
        </button>
      </div>
    ) : (
      this.props.children
    );
  }
}
export function TerminalPane(
  props: ComponentProps<typeof import("./TerminalPane").TerminalPane>,
) {
  return (
    <PanelBoundary>
      <Suspense fallback={<p>加载终端…</p>}>
        <Terminal {...props} />
      </Suspense>
    </PanelBoundary>
  );
}
export function DebugPane(
  props: ComponentProps<typeof import("./DebugPane").DebugPane>,
) {
  return (
    <PanelBoundary>
      <Suspense fallback={<p>加载调试器…</p>}>
        <Debug {...props} />
      </Suspense>
    </PanelBoundary>
  );
}
