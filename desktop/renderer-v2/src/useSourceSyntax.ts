// 按源码版本和差异侧读取文本，并协调语法着色。
// 主要引用方包括 `InlineCodeChanges.tsx` 和 `CodeBlock.tsx`。
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import type { SourceFilePage } from "./generated";
import type { SyntaxLines } from "./syntaxEngine";
import { codeURL,type CodeTarget } from "./sourceCode";

// 按源码版本和差异侧保留的高亮结果。`null` 表示该侧无可用高亮，只有文件含暂存侧时才请求 `staged`。
export type SourceSyntax={before?:SyntaxLines|null;after?:SyntaxLines|null;staged?:SyntaxLines|null};
export const MAX_SYNTAX_CHARS=131072;
let sequence=0;
let worker:Worker|undefined;
const pending=new Map<number,{resolve:(tokens:SyntaxLines|null)=>void;timer:ReturnType<typeof setTimeout>}>();
function clearWorker(){worker?.terminate();worker=undefined;for(const job of pending.values()){clearTimeout(job.timer);job.resolve(null);}pending.clear();}
// 文本来自保留源码页或 `CodeBlock.tsx` 传入的代码块。
// 由 `syntax.worker.ts` 异步分词，回执按请求 ID 匹配；工作线程出错或超时会结束在途分词，让调用方退回原文。
export async function colorSource(source:string,path:string,language?:string):Promise<SyntaxLines|null> {
  if(source.length>MAX_SYNTAX_CHARS||path.length>4096)return null;
  try{
    if(!worker){
      worker=new Worker(new URL('./syntax.worker.ts',import.meta.url),{type:'module'});
      worker.onmessage=event=>{const job=pending.get(event.data.id);if(!job)return;clearTimeout(job.timer);pending.delete(event.data.id);job.resolve(event.data.tokens);};
      worker.onerror=event=>{event.preventDefault();clearWorker();};
    }
    const id=++sequence;
    return await new Promise(resolve=>{const timer=setTimeout(clearWorker,10000);pending.set(id,{resolve,timer});worker!.postMessage({id,source,path,language});});
  }catch{clearWorker();return null;}
}
async function read(target:CodeTarget,view:'before'|'after'|'staged') {
  if(target.file[view]?.kind!=='text')return null;
  if((target.file[view]?.bytes||0)>MAX_SYNTAX_CHARS)return null;
  let text='',after=0;
  do{
    const page=await api<SourceFilePage>(`${codeURL(target)}?revision=${target.revision}&view=${view}&after=${after}`);
    text+=page.text;if(text.length>MAX_SYNTAX_CHARS)return null;
    if(!page.hasMore)break;
    if(page.next<=after)throw Error('语法正文分页没有前进');after=page.next;
  }while(true);
  return colorSource(text,target.file.path);
}
/** Coloring is optional; displayed bytes always come from the retained page. */
export function useSourceSyntax(target:CodeTarget) {
  return useQuery<SourceSyntax>({queryKey:['source-syntax',target.conversationId,target.kind,target.id,target.revision,target.index,target.accessVersion],
    staleTime:Infinity,gcTime:0,retry:false,
    queryFn:async()=>({before:await read(target,'before'),after:await read(target,'after'),...(target.file.staged?{staged:await read(target,'staged')}:{})}),
  });
}
