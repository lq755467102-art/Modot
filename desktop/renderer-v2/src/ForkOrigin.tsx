import {useState} from "react";
import {useQuery} from "@tanstack/react-query";
import {api,queryString} from "./client";
import type {Conversation} from "./generated";
import {useUiLanguage} from "./UiLanguage";

export function ForkOrigin({chat,onOpen}:{chat:Conversation;onOpen:(id:string)=>void}) {
  const {t}=useUiLanguage();
  const [open,setOpen]=useState(false);
  const source=chat.forkOf!;
  const page=useQuery<{turns:{role:string;text:string;seq:number;nextOffset:number|null}[]}>({
    queryKey:["fork-origin",chat.id,source.generation,source.before],
    queryFn:()=>api("/api/v2/conversations/threads/"+queryString(chat.id)+"/fork"),enabled:open,gcTime:0,
  });
  return <details className="fork-origin" open={open} onToggle={e=>setOpen(e.currentTarget.open)}>
    <summary>{t("分支来源")} · {source.title}</summary>
    <div><small>{t("分支起点前的最近问答")}</small> <button type="button" onClick={()=>onOpen(source.conversationId)}>{t("打开来源会话")}</button></div>
    {open&&page.error&&<p role="alert">{page.error.message}</p>}
    {open&&!page.error&&page.data?.turns.map(turn=><p key={turn.seq}><b>{turn.role==="user"?t("你"):"Modot"}</b> {turn.text}{turn.nextOffset!==null?"…":""}</p>)}
  </details>;
}
