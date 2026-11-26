// 从检查日志识别错误位置，并把文本位置转换成可供工作间跳转的范围。
// 主要引用方包括 `ProblemsPane.tsx` 和 `TerminalPane.tsx`。
import type {IBufferLine} from '@xterm/xterm';

// 从日志显示文本提取的导航候选。
// `start` 和 `end` 是去除终端控制码后的字符位置，`line` 是候选源文件行号。
export type LogLocation={path:string;line:number;start:number;end:number;text:string};
const extensions='py|tsx?|jsx?|vue|cs|c|cpp|h|rs|go';
const filename=String.raw`(?:"[^"\r\n]+\.(?:${extensions})"|'[^'\r\n]+\.(?:${extensions})'|(?:[A-Za-z]:[\\/]|\\\\|\.{1,2}[\\/]|/)[^"'<>\r\n:()]*?\.(?:${extensions})|[^\s"'<>:()=]+?\.(?:${extensions}))`;
const general=new RegExp(String.raw`(^|[\s\[(])(${filename})(?::(\d+)(?::\d+)?|\((\d+)(?:,\d+)?\))(?=$|[\s,;)\]}]|:(?:\s|$))`,'g');
export function logDisplayText(value:string){
  return value.replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g,'').replace(/(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]/g,'');
}
function localPath(value:string){
  return !!value.trim()&&!/[\x00-\x1f\x7f-\x9f<>]/.test(value)&&(!/^[A-Za-z][A-Za-z\d+.-]*:/.test(value)||/^[A-Za-z]:[\\/]/.test(value));
}
/** Heuristic navigation only; the raw output remains owned by its original reader. */
export function logLocations(raw:string):LogLocation[]{
  const text=logDisplayText(raw),locations:LogLocation[]=[];
  function add(path:string,line:string,start:number,end:number){
    const number=Number(line);
    if(localPath(path)&&Number.isSafeInteger(number)&&number>0&&!locations.some(item=>start<item.end&&end>item.start))locations.push({path,line:number,start,end,text});
  }
  for(const match of text.matchAll(/\bFile "([^"\r\n]+)", line (\d+)\b/g))add(match[1],match[2],match.index!,match.index!+match[0].length);
  for(const match of text.matchAll(general)){
    const name=match[2],quoted=name.startsWith('"')||name.startsWith("'");
    add(quoted?name.slice(1,-1):name,match[3]||match[4],match.index!+match[1].length,match.index!+match[0].length);
  }
  return locations.sort((a,b)=>a.start-b.start);
}

/** UTF-16 offsets must be translated through xterm's actual glyph widths. */
export function terminalLocationRange(line:IBufferLine,start:number,end:number){
  let offset=0,left:number|undefined,right:number|undefined;
  for(let x=0;x<line.length;x++){
    const cell=line.getCell(x);if(!cell||!cell.getWidth())continue;
    const next=offset+(cell.getChars()||' ').length;
    if(next>start&&offset<end){left??=x+1;right=x+cell.getWidth();}
    offset=next;if(offset>=end)break;
  }
  return left!==undefined&&right!==undefined?{start:left,end:right}:undefined;
}
