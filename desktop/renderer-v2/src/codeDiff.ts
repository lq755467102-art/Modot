// 将统一差异或源码拆成可渲染行，保留新旧侧行号和行类型。
// 主要引用方包括 `InlineCodeChanges.tsx` 和 `SourceCodeView.tsx`。
import type { SourceFilePage } from "./generated";

// 保留差异页的一行展示记录。
// `oldLine` 和 `newLine` 分别指改前、改后的源码行号，只有该侧实际存在的代码行才有对应值。
export type CodeLine = { kind: "context" | "addition" | "deletion" | "hunk" | "notice";
  text: string; oldLine?: number; newLine?: number };

/** Continuation positions come from the retained diff, not the current file. */
export function diffLines(page: SourceFilePage): CodeLine[] {
  let oldLine=page.oldLineStart || 0, newLine=page.newLineStart || 0;
  let inHunk=page.inHunk || false;
  const result:CodeLine[]=[];
  for (const raw of page.text.split("\n")) {
    const match=raw.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (match) {
      oldLine=Number(match[1]);newLine=Number(match[2]);inHunk=true;
      result.push({kind:"hunk",text:raw});continue;
    }
    if (!inHunk) continue;
    if (raw.startsWith("\\ No newline")) result.push({kind:"notice",text:"文件末尾没有换行"});
    else if (raw.startsWith("+")) result.push({kind:"addition",text:raw.slice(1),newLine:newLine++});
    else if (raw.startsWith("-")) result.push({kind:"deletion",text:raw.slice(1),oldLine:oldLine++});
    else if (raw.startsWith(" ")) result.push({kind:"context",text:raw.slice(1),oldLine:oldLine++,newLine:newLine++});
  }
  return result;
}

export function splitLines(lines:CodeLine[]):{left?:CodeLine;right?:CodeLine;meta?:CodeLine}[] {
  const rows:{left?:CodeLine;right?:CodeLine;meta?:CodeLine}[]=[];
  for (let i=0;i<lines.length;) {
    const line=lines[i];
    if (line.kind==="context") {rows.push({left:line,right:line});i++;continue;}
    if (line.kind==="hunk" || line.kind==="notice") {rows.push({meta:line});i++;continue;}
    const removed:CodeLine[]=[],added:CodeLine[]=[];
    while (lines[i]?.kind==="deletion") removed.push(lines[i++]);
    while (lines[i]?.kind==="addition") added.push(lines[i++]);
    for (let n=0;n<Math.max(removed.length,added.length);n++) rows.push({left:removed[n],right:added[n]});
  }
  return rows;
}

export function fullLines(page:SourceFilePage):CodeLine[] {
  const values=page.text.split("\n");
  if (page.hasMore && values.at(-1)==="") values.pop();
  return values.map((text,i)=>({kind:"context",text:text.replace(/\r$/,""),
    ...(page.view==="before"?{oldLine:(page.lineStart || 1)+i}:{newLine:(page.lineStart || 1)+i})}));
}
