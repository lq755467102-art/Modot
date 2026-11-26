// 登记资源链接的解析器，将受支持的地址交给对应的打开入口。
// 主要引用方包括 `Message.tsx`、`ResourceLinks.tsx` 和 `App.tsx`。
import type {LinkedWorkroomFile} from "./generated";

export type ResourceLink =
  | {kind:"web" | "mail"; href:string}
  | {kind:"file"; href:string; path:string; line?:number};

export type RetainedOutputReference = {id:string; versionId?:string};

export function parseRetainedOutput(value?:string):RetainedOutputReference | undefined {
  const match = /^retained-output:([0-9a-f]{32})(?:\/([0-9a-f]{32}))?$/.exec(value ?? "");
  if (match) return {id:match[1], versionId:match[2]};
}

export type ResourceTarget = ResourceLink & {
  conversationId:string;
  label:string;
  file?:LinkedWorkroomFile;
};

export type ResourceOpener = {
  id:string;
  label:string;
  accepts:(target:ResourceTarget)=>boolean;
  open:(target:ResourceTarget)=>void | Promise<void>;
};

const registered = new Map<string,ResourceOpener>();

// A trusted application registers its opener for the lifetime of its host.
// Registration does not grant access. Local files arrive resolved by the
// current conversation's workroom and are resolved again before dispatch.
export function registerResourceOpener(opener:ResourceOpener) {
  if(registered.has(opener.id)) throw Error("重复登记资源打开方式");
  registered.set(opener.id,opener);
  return ()=>{
    if(registered.get(opener.id)===opener)registered.delete(opener.id);
  };
}

export function resourceOpeners(target:ResourceTarget, builtins:readonly ResourceOpener[]) {
  const choices = new Map(builtins.map(opener=>[opener.id,opener]));
  if(target.kind!=="file"||target.file){
    for(const [id,opener] of registered)if(!choices.has(id))choices.set(id,opener);
  }
  return [...choices.values()].filter(opener=>opener.accepts(target));
}

export function parseResourceLink(value:string):ResourceLink | undefined {
  if(!value||value.length>8192||/[\u0000-\u001f\u007f]/.test(value)||value.startsWith("#"))return;
  const scheme=/^[a-z][a-z0-9+.-]*:/i.exec(value)?.[0].toLowerCase();
  const drive=/^[a-z]:[/\\]/i.test(value);
  const bareFileLine=/^[a-z\d_-]+(?:\.[a-z\d_-]+)+:[1-9]\d*(?::[1-9]\d*)?$/i.test(value);
  if(scheme&&!drive&&!bareFileLine&&scheme!=="file:"&&!["https:","http:","mailto:"].includes(scheme))return;
  try {
    if(scheme==="https:"||scheme==="http:"||scheme==="mailto:"){
      const url=new URL(value);
      if(url.username||url.password)return;
      return {kind:scheme==="mailto:"?"mail":"web",href:url.href};
    }
    const url=scheme==="file:"?new URL(value):undefined;
    if(url?.hostname||url?.search)return;
    const hash=url?.hash ?? value.slice(value.includes("#")?value.indexOf("#"):value.length);
    let path=decodeURIComponent(url?.pathname ?? value.slice(0,value.length-hash.length)).replace(/\\/g,"/");
    if(!path||path.startsWith("//")||/[\u0000-\u001f\u007f]/.test(path))return;
    if(/^\/[a-z]:\//i.test(path))path=path.slice(1);
    const fragmentLine=/^#L([1-9]\d*)$/i.exec(hash)?.[1];
    const trailingLine=/:([1-9]\d*)(?::[1-9]\d*)?$/.exec(path);
    if(!hash&&trailingLine)path=path.slice(0,trailingLine.index);
    if(!path||path.replace(/^[a-z]:\//i,"").includes(":"))return;
    const line=Number(fragmentLine||(!hash&&trailingLine?.[1]));
    return {kind:"file",href:value,path,line:Number.isSafeInteger(line)&&line>0?line:undefined};
  }catch{return;}
}
