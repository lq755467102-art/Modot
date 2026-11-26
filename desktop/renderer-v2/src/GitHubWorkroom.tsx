// GitHub 的原网页和账号设置共用独立应用外壳，右栏借用原项目会话。
import {Suspense, useLayoutEffect, useRef, useState, type MutableRefObject, type ReactNode} from "react";
import {createPortal} from "react-dom";
import {useQuery,useQueryClient} from "@tanstack/react-query";
import {api} from "./client";
import {BrowserPane, type GitHubBrowserPointer} from "./BrowserPane";
import {WorkroomConversation} from "./WorkroomConversation";
import {useUiLanguage} from "./UiLanguage";
import type {WebSource} from "./ConversationSideTabs";
import type {Conversation} from "./generated";
import "./workroom-conversation.css";
import "./github-workroom.css";

type Props = {
  conversationId: string;
  visible: boolean;
  pluginId: string;
  pluginRevision: number;
  canQuote: boolean;
  onOpenConversation: () => void;
  connection: ReactNode;
  onQuote: (owner: string, text: string, source: WebSource) => void;
};

export default function GitHubWorkroom({conversationId,visible,pluginId,pluginRevision,canQuote,onOpenConversation,connection,onQuote}: Props) {
  const {t} = useUiLanguage();
  const [page,setPage] = useState<"web"|"connection">("web");
  const [toolbar,setToolbar] = useState<HTMLDivElement|null>(null);
  const [webConversation,setWebConversation] = useState(conversationId);
  const browserPointer=useRef<GitHubBrowserPointer|null>(null);
  // 只在进入应用时换成当前会话的网页。会话列表里的选择变化不在后台打开网页。
  useLayoutEffect(() => {if (visible) setWebConversation(conversationId);}, [visible,conversationId]);
  const currentWebView = webConversation === conversationId;
  const chat = useQuery<{threads:Conversation[]}>({
    queryKey:["conversations"],
    queryFn:()=>api("/api/v2/conversations/threads"),
    enabled:visible&&!!conversationId,
  }).data?.threads.find(thread=>thread.id===conversationId);

  return <section className="github-workroom" aria-label={t("GitHub 工作间")}>
    <header className="github-workroom-toolbar">
      <h1>GitHub</h1>
      <nav aria-label={t("GitHub 工作间")}>
        <button type="button" aria-current={page==="web"?"page":undefined} onClick={()=>setPage("web")}>{t("网页")}</button>
        <button type="button" aria-current={page==="connection"?"page":undefined} onClick={()=>setPage("connection")}>{t("账号与连接")}</button>
      </nav>
      <div className="github-workroom-chat-control" ref={setToolbar}/>
    </header>
    <div className="github-workroom-body">
      <div className="github-workroom-content">
        <div className="workroom-web-page" hidden={page!=="web"}>
          <GitHubWebPage key={webConversation} visible={visible&&page==="web"&&currentWebView} owner={webConversation}
            pluginId={pluginId} pluginRevision={pluginRevision} browserPointer={browserPointer} onQuote={canQuote&&currentWebView
            ? (text,source)=>onQuote(webConversation,text,source)
            : undefined}/>
        </div>
        {page==="connection"&&<div className="github-workroom-connections">
          <section className="github-login-help">
            <p>{t("GitHub App 授权用于 Agent 读取仓库。网页账号请在网页标签切换。")}</p>
          </section>
          <Suspense fallback={<p role="status">{t("正在打开 GitHub…")}</p>}>{connection}</Suspense>
        </div>}
      </div>
      <WorkroomConversation key={conversationId} conversation={chat} workspace="github" matched={!!chat}
        visible={visible} drawerId="github-main-chat" showFileAttachment={false}
        captureGitHubPage={()=>visible&&page==="web"&&currentWebView
          ?browserPointer.current?.capture()??Promise.resolve(null):Promise.resolve(null)}
        navigateGitHubPage={async id=>{
          if(visible&&page==="web"&&currentWebView)await browserPointer.current?.navigate(id);
        }}
        renderToggle={control=>toolbar?createPortal(control,toolbar):null}
        openConversation={onOpenConversation}
        unbound={<><p>{t("选择一个会话后即可在这里发送消息。")}</p>
          <button type="button" onClick={onOpenConversation}>{t("前往对话")}</button></>}/>
    </div>
  </section>;
}

type RepositoryChoice={id:string;label:string;directory:string;checkout:'local'|'worktree';remote:string;repository:string};
type Binding={scopeRevision:string;revision:string;candidates:RepositoryChoice[];
  selected:RepositoryChoice|null;suggested:RepositoryChoice|null;expired:boolean};

function GitHubWebPage({visible,owner,pluginId,pluginRevision,browserPointer,onQuote}: {
  visible: boolean;
  owner:string;
  pluginId:string;
  pluginRevision:number;
  browserPointer:MutableRefObject<GitHubBrowserPointer|null>;
  onQuote?: (text:string, source:WebSource) => void;
}) {
  const {t} = useUiLanguage();
  const [open,setOpen] = useState(true);
  const [quoted,setQuoted] = useState(false);
  const [navigation,setNavigation]=useState<{url:string}>();
  const [startUrl,setStartUrl]=useState("https://github.com/");
  const [busy,setBusy]=useState(false),[notice,setNotice]=useState("");
  const queryClient=useQueryClient();
  const bindingKey=["github-workroom","binding",owner];
  const endpoint="/api/v2/github-workroom/binding";
  const binding=useQuery<Binding>({queryKey:bindingKey,
    queryFn:()=>api(endpoint+"?conversationId="+encodeURIComponent(owner)),enabled:!!owner&&visible});
  const selected=binding.data?.selected;
  const suggestion=binding.data?.suggested;
  async function choose(value:string|null) {
    if(!binding.data||busy)return;
    setBusy(true);setNotice("");
    try {
      const next=await api<Binding>(endpoint,{conversationId:owner,revision:binding.data.revision,
        scopeRevision:binding.data.scopeRevision,selected:value});
      queryClient.setQueryData(bindingKey,next);
    } catch(error) {
      setNotice((error as Error).message);void binding.refetch();
    } finally {setBusy(false);}
  }
  function navigate(url:string) {
    if(open)setNavigation({url});
    else {setStartUrl(url);setNavigation(undefined);setOpen(true);}
  }
  async function clearLogin() {
    setBusy(true);setNotice("");
    try {
      await window.workbench.browser({operation:"github-clear"});
      setOpen(false);setQuoted(false);setNotice(t("已清除这个档案的 GitHub 网页登录。"));
    } catch(error) {setNotice((error as Error).message);}
    finally {setBusy(false);}
  }

  return <>
    <div className="github-repository-toolbar">
      {!!owner&&<>
        <select aria-label={t("绑定 GitHub 仓库")} value={selected?.id||""} disabled={busy||binding.isPending||!!binding.error}
          onChange={event=>void choose(event.target.value||null)}>
          <option value="">{t("仅浏览网页")}</option>
          {binding.data?.candidates.map(row=><option key={row.id} value={row.id}>
            {row.repository} · {row.label}{row.directory?"/"+row.directory:""} · {row.remote}{row.checkout==="worktree"?" · "+t("独立工作树"):""}
          </option>)}
        </select>
        {suggestion&&<button type="button" disabled={busy} onClick={()=>void choose(suggestion.id)}>
          {t("绑定建议仓库")} · {suggestion.repository}</button>}
        {selected&&<button type="button" onClick={()=>navigate("https://github.com/"+selected.repository)}>
          {t("打开已绑定仓库")}</button>}
      </>}
      <div className="github-login-controls">
        <button type="button" onClick={()=>navigate("https://github.com/login")}>{t("网页登录")}</button>
        <button type="button" disabled={busy} onClick={()=>void clearLogin()}>{t("清除网页登录")}</button>
      </div>
    </div>
    {(notice||binding.error||binding.data?.expired)&&<p className="github-quote-status" role="status">
      {notice||binding.error?.message||t("之前绑定的仓库已离开当前会话范围。")}</p>}
    {open ? <>
    {quoted&&<p className="github-quote-status" role="status">{t("已将网页引用加入草稿。")}</p>}
    <BrowserPane conversationId="" visible={visible} initialUrl={startUrl} navigation={navigation}
      github={{owner,pluginId,pluginRevision}} browserPointer={browserPointer}
      onClose={()=>{setOpen(false);setQuoted(false);}}
      onQuote={onQuote && ((text,source)=>{onQuote(text,source);setQuoted(true);})}/>
    </> : <div className="github-workroom-open">
      <button type="button" onClick={()=>navigate("https://github.com/")}>{t("打开 GitHub")}</button>
    </div>}
  </>;
}
