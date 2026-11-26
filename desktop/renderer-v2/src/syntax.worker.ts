// 在独立渲染线程处理语法着色请求，将结果按请求身份返回。
// 主要引用方包括 `useSourceSyntax.ts`。
import { tokenize } from "./syntaxEngine";
// No filesystem, application bridge, model or network authority is available here.
self.onmessage=async(event:MessageEvent)=>{
  const {id,source,path,language}=event.data||{};
  if(typeof id!=='number')return;
  if(typeof source!=='string'||typeof path!=='string'||path.length>4096){
    self.postMessage({id,tokens:null});
    return;
  }
  try{const tokens=await tokenize(source,path,typeof language==="string"?language:undefined);self.postMessage({id,tokens:tokens&&JSON.stringify(tokens).length<=4*1024*1024?tokens:null});}catch{self.postMessage({id,tokens:null});}
};
