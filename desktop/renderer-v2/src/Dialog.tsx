// 提供共用模态对话框，统一焦点进入、退出和归还行为。
// 主要引用方包括 `ToolHandoff.tsx` 和 `App.tsx`。
import type { ReactNode } from "react";
import { useDialogFocus } from "./useDialogFocus";

export function Dialog({
  label,
  className,
  onClose,
  children,
}: {
  label: string;
  className: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useDialogFocus(onClose);
  return (
    <section
      ref={ref}
      tabIndex={-1}
      role="dialog"
      aria-modal="true"
      aria-label={label}
      className={className}
      onClick={(event) => event.stopPropagation()}
    >
      {children}
    </section>
  );
}
