// 解析流式 Markdown，复用已稳定的正文块并处理仍在生长的尾部。
// 主要引用方包括 `Message.tsx` 和 `StreamingMessage.tsx`。
import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import { unified } from "unified";

const parser = unified().use(remarkParse).use(remarkGfm).freeze();
const fenceLine = /^[ \t]*(?:>[ \t]*)*(?:(?:[-+*]|\d+[.)])[ \t]+)?(?:`{3,}[^`]*|~{3,}[^\n]*|`{1,2}[ \t]*|~{1,2}[ \t]*)$/;
export type MarkdownTree = ReturnType<typeof parser.parse>;

type Node = {
  type: string;
  children?: Node[];
  position?: { start: { offset?: number }; end: { offset?: number } };
};
// A partition can start later than the source origin of its parser offsets.
type Part = { start: number; origin: number; tree: MarkdownTree };
type OpenFence = { marker: string; contentStart: number };
export type MarkdownStream = {
  source: string;
  suffix: string;
  stable: readonly Part[];
  tail: Part;
  documentReferences: boolean;
  openFence?: OpenFence;
};

function lineStart(source: string, offset: number): number {
  if (offset === 0) return 0;
  return Math.max(source.lastIndexOf("\n", offset - 1), source.lastIndexOf("\r", offset - 1)) + 1;
}

type InlineSpan = {
  start: number;
  end: number;
  contentStart: number;
  contentEnd: number;
  close: string;
};
type DraftMarkdown = { available: number; spans: InlineSpan[] };
const inlineDelimiters: Readonly<Record<string,number>> = { strong:2, emphasis:1, delete:2 };
const atomicInline = new Set(["inlineCode","link","image","linkReference","imageReference"]);

// Look ahead only in text already received. A known closing delimiter can
// be borrowed for painting without advancing the cursor or changing the log.
export function planDraftMarkdown(source: string, streaming: boolean): DraftMarkdown {
  const plan: DraftMarkdown = { available:source.length, spans:[] };
  if (!/[*~\x60\[]|(?:^|[^\p{L}\p{N}])_/u.test(source)) return plan;
  const lastLine = lineStart(source, source.length);
  function visit(node:Node) {
    const start=node.position?.start.offset, end=node.position?.end.offset;
    if(start!==undefined&&end!==undefined){
      const width=inlineDelimiters[node.type];
      if(width){
        const marker=source[start];
        const size=node.type==="delete"&&source[start+1]!==marker?1:width;
        plan.spans.push({start,end,contentStart:start+size,contentEnd:end-size,close:source.slice(end-size,end)});
      }else if(atomicInline.has(node.type)){
        // Keep link destinations and code delimiters out of incomplete text.
        plan.spans.push({start,end,contentStart:end,contentEnd:end,close:""});
        return;
      }else if(streaming&&node.type==="text"&&end>lastLine){
        // Only hold a likely opener on the unfinished line. A long literal
        // or an unmatched marker in the final source still remains readable.
        for(const match of source.slice(Math.max(start,lastLine),end).matchAll(/\*+|_+|~{2}/g)){
          const at=Math.max(start,lastLine)+match.index;
          const before=source[at-1], after=source[at+match[0].length];
          const boundary=!before||/[\s\p{P}\p{Script=Han}]/u.test(before);
          if(boundary&&before!=="\\"&&(!after||!/\s/u.test(after))&&source.length-at<=120){
            plan.available=Math.min(plan.available,at);
            break;
          }
        }
      }
    }
    node.children?.forEach(visit);
  }
  visit(parser.parse(source));
  return plan;
}

export function draftMarkdownPrefix(source:string, through:number, plan:DraftMarkdown) {
  let end=Math.min(through,plan.available);
  const open=plan.spans.filter(span=>span.start<end&&span.end>end);
  let suffix="";
  for(const span of open.reverse()){
    end=Math.min(end,span.contentEnd);
    while(end>span.contentStart&&/[\s\\]/u.test(source[end-1]))end--;
    if(end<=span.contentStart)end=span.start;
    else suffix+=span.close;
  }
  return {text:source.slice(0,end),suffix};
}

function containsDefinitions(node: Node): boolean {
  return node.type === "definition" || node.type === "footnoteDefinition" ||
    (node.children?.some(containsDefinitions) ?? false);
}

function possibleFenceEnd(source: string, marker: string): boolean {
  return source.split("\n").some(line => line.trimStart().startsWith(marker));
}

function codeValue(source: string, start: number, end = source.length): string {
  const contentEnd = end > start && source[end - 1] === "\n" ? end - 1 : end;
  return source.slice(start, contentEnd);
}

function detectOpenFence(source: string, start: number, tree: MarkdownTree): OpenFence | undefined {
  const code = tree.children[0];
  if (tree.children.length !== 1 || code.type !== "code" || !code.lang || /[\r\0]/.test(source)) return;
  const marker = /^(?:`{3,}|~{3,})/.exec(source)?.[0];
  const breakAt = source.indexOf("\n");
  if (!marker || breakAt < 0) return;
  const body = breakAt + 1;
  if (possibleFenceEnd(source.slice(body), marker) || code.value !== codeValue(source, body)) return;
  return { marker, contentStart: start + body };
}

// A partial pipe row can recalculate every column. Wait for its newline.
function holdPartialTableRow(tree: MarkdownTree, line: number, end: number) {
  if (line === end) return;
  let node: Node | undefined = tree.children.at(-1);
  while (node && node.type !== "table") node = node.children?.at(-1);
  const rows = node?.children;
  const lastRow = rows?.at(-1)?.position?.start.offset;
  if (rows && rows.length > 1 && lastRow !== undefined && lastRow >= line) rows.pop();
}

// Keep one mutable top-level block behind the latest complete line. The
// parser supplies the boundary so lists, quotes, and fences stay together.
export function appendMarkdown(previous: MarkdownStream | undefined, source: string, suffix = ""): MarkdownStream {
  if (source === previous?.source && suffix === previous.suffix) return previous;
  const prior = previous && source.startsWith(previous.source) ? previous : undefined;
  const start = prior?.tail.start ?? 0;
  const rawTail = source.slice(start);
  const lastLine = lineStart(rawTail, rawTail.length);
  // An opening or closing marker may still be arriving. Keep it in the
  // source so withholding the display never invalidates the prefix cache.
  const pending = fenceLine.test(rawTail.slice(lastLine)) ? rawTail.slice(0, lastLine) : rawTail;
  if (prior?.openFence && !suffix) {
    // Only a simple, confirmed open top-level fence can accept raw code.
    // Let the Markdown parser interpret any possible close or normalization.
    const change = source.slice(lineStart(source, prior.source.length));
    const code = prior.tail.tree.children[0];
    if (code?.type === "code" && !/[\r\0]/.test(change) && !possibleFenceEnd(change, prior.openFence.marker)) {
      const value = codeValue(source, prior.openFence.contentStart, start + pending.length);
      return { ...prior, source, suffix, tail: { ...prior.tail, tree: { type: "root", children: [{ ...code, value }] } } };
    }
  }
  const tree = parser.parse(pending + suffix);
  const stable = prior?.stable ?? [];
  if (prior?.documentReferences || containsDefinitions(tree)) {
    // Definitions anywhere in the document can change links in sealed blocks.
    const fullEnd = start + pending.length;
    const full = start ? parser.parse(source.slice(0, fullEnd) + suffix) : tree;
    holdPartialTableRow(full, start + lastLine, fullEnd);
    return { source, suffix, stable: [], tail: { start: 0, origin: 0, tree: full }, documentReferences: true };
  }

  const blocks = tree.children;
  let lastComplete = blocks.length - 1;
  while (lastComplete >= 0 && (blocks[lastComplete].position?.start.offset ?? Infinity) >= lastLine) {
    lastComplete--;
  }
  holdPartialTableRow(tree, lastLine, pending.length);
  const commit = lastComplete > 0;
  const boundary = commit ? lineStart(pending, blocks[lastComplete].position!.start.offset!) : 0;
  const tailTree: MarkdownTree = commit ? { type: "root", children: blocks.slice(lastComplete) } : tree;
  const tailStart = start + boundary;
  return {
    source,
    suffix,
    stable: commit ? [...stable, { start, origin:start, tree: { type: "root", children: blocks.slice(0, lastComplete) } }] : stable,
    tail: { start: tailStart, origin:start, tree: tailTree },
    documentReferences: false,
    openFence: detectOpenFence(rawTail.slice(boundary), tailStart, tailTree),
  };
}
