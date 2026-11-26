// 显示插件数据页，并把用户确认的示例交给当前会话草稿。
// 主要引用方包括 `App.tsx`。
import {useEffect, useLayoutEffect, useRef, useState} from "react";
import {useQuery, useQueryClient} from "@tanstack/react-query";
import {api} from "./client";
import {installationIsCurrent, refreshInstalledCatalog} from "./extensionCatalog";
import type {PluginDataPageDraft} from "./generated";
import {matchesPage, pageRevision, pageRoute, readPluginPage, stalePage} from "./pluginDataPages";
import PluginApplicationHome from "./PluginApplicationHome";
import type {InstalledApplication} from "./pluginApplications";
import type {DraftTarget, WriteActiveDraft} from "./ToolHandoff";
import type {PluginLogin} from "./usePluginLogin";

export default function DataPluginApplication({app, fontSize, target, interactive, onAppend, onManage, login}: {
  app: InstalledApplication & {dataPageRevision: number};
  fontSize: number;
  target?: DraftTarget;
  interactive: boolean;
  onAppend: WriteActiveDraft;
  onManage: () => void;
  login: PluginLogin;
}) {
  const {pkg, pageId, dataPageRevision} = app;
  const {id: owner, title: conversationTitle} = target ?? {};
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const interaction = useRef<AbortController | undefined>(undefined);
  // 草稿交接属于当前页面和会话。任一身份变化都会取消仍在等待的请求。
  useLayoutEffect(() => {
    const controller = new AbortController();
    interaction.current = interactive && owner ? controller : undefined;
    return () => { controller.abort(); interaction.current = undefined; };
  }, [app.id, interactive, owner]);

  const page = useQuery({
    queryKey: ["installed-data-page", app.id],
    queryFn: ({signal}) => readPluginPage(pkg, pageId, dataPageRevision, signal),
    refetchInterval: 2500,
    retry: false,
    enabled: interactive,
  });
  const invalid = stalePage(page.error);
  useEffect(() => {
    if (!invalid) return;
    void refreshInstalledCatalog(client);
    onManage();
  }, [invalid, client, onManage]);

  const descriptor = page.isSuccess ? page.data : undefined;
  const allowsDraft = descriptor?.actions.includes("append-to-draft") === true;
  async function append(index: number) {
    const scope = interaction.current;
    if (inFlight.current || !scope || !owner || !allowsDraft || !installationIsCurrent(client, pkg)) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    try {
      const approval = await api<PluginDataPageDraft>(pageRoute(pkg.id, pageId) + "/draft", {
        ...pageRevision(pkg, dataPageRevision), conversationId:owner, exampleIndex:index,
      }, true);
      scope.signal.throwIfAborted();
      if (!matchesPage(approval, pkg, pageId, dataPageRevision) || approval.conversationId !== owner ||
          approval.exampleIndex !== index) throw Error("草稿来源已变化，请重新选择示例。");

      // The first response can arrive after an install was revoked in another view.
      const current = await readPluginPage(pkg, pageId, dataPageRevision, scope.signal);
      scope.signal.throwIfAborted();
      if (!installationIsCurrent(client, pkg) || !current.actions.includes("append-to-draft") ||
          current.examples[index]?.text !== approval.text) throw Error("页面状态已变化，请重新选择示例。");
      onAppend(owner, approval.text);
    } catch (failure) {
      if (scope.signal.aborted) return;
      if (stalePage(failure)) {
        void refreshInstalledCatalog(client);
        onManage();
      } else {
        setError(failure instanceof Error ? failure.message : String(failure));
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  if (!descriptor) return <div className="plugin-home">
    <div className="plugin-home-page">
      <h1>{app.name}</h1>
      {page.error ? <p role="alert">{page.error.message}{" "}<button type="button"
        disabled={page.isFetching} onClick={() => void page.refetch()}>重新读取页面</button></p>
        : <p role="status">正在读取当前安装的页面…</p>}
      <button type="button" onClick={onManage}>管理插件</button>
    </div>
  </div>;

  return <PluginApplicationHome
    app={{...app, name:descriptor.title, description:descriptor.description, prompts:descriptor.examples}}
    fontSize={fontSize} login={login} onManage={onManage}
    onDraft={allowsDraft ? (_, index) => void append(index) : undefined}
    draftDisabled={busy || !owner || !interactive}
    draftNotice={allowsDraft && descriptor.examples.length ?
      (busy ? "正在核对页面与草稿…" : conversationTitle ? "加入草稿 · " + conversationTitle : "请先在会话页选择可编辑的会话。")
      : undefined}
    draftError={error}/>;
}
