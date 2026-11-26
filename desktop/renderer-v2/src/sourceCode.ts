// 定义源码读取目标、行锚点与分页状态，并构造相应接口地址。
// 主要引用方包括 `InlineCodeChanges.tsx` 和 `useSourceSyntax.ts`。
import { queryString } from "./client";
import type { SourceChangeFile } from "./generated";
// 代码读取目标由变更卡片或审查抽屉传入。
// `kind/id/index` 定位原接口记录，`revision` 固定内容，`accessVersion` 绑定本次可见的资料范围。
export type CodeTarget={conversationId:string;kind:"operation"|"range";id:string;revision:string;index:number;file:SourceChangeFile;accessVersion?:string};
export type CodeSide="before"|"after"|"staged";
// `side` 区分改前、改后和暂存，`line` 是所选侧行号，`offset` 对应需要重新加载的页。
export type CodeAnchor={side:CodeSide;line:number;offset?:number};
// 每个源码页签独立保存读取视图、分页游标和滚动位置，切换文件时可以回到原锚点。
export type CodeReading={view:string;after:number;context:number;layout:"unified"|"split";line?:number;wrap?:boolean;hunk?:number;scrollTop?:number;scrollLeft?:number;anchor?:CodeAnchor};
export const initialCodeReading:CodeReading={view:"diff",after:0,context:3,layout:"unified"};
export function codeURL(target:CodeTarget) {
  return `/api/v2/conversations/threads/${queryString(target.conversationId)}/${target.kind==="operation"?"changes":"change-ranges"}/${queryString(target.id)}/files/${target.index}`;
}
