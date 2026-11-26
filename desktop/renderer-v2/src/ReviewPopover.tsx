// 向变更审查提供共享的折叠式工具菜单和可访问的展开状态。
// 主要引用方包括 `CodeReviewDrawer.tsx` 和 `SourceCodeView.tsx`。
import {useId,type ReactNode} from "react";
import {UiIcon} from "./UiIcon";

export function ReviewPopover({label,icon,children}:{label:string;icon:"info"|"settings";children:ReactNode}) {
  const id=useId();
  return <>
    <button type="button" popoverTarget={id} aria-label={label} title={label}><UiIcon name={icon}/></button>
    <div id={id} popover="auto" className="review-flyout" role="group" aria-label={label}>
      {children}
    </div>
  </>;
}
