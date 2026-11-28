// 用操作编号跟踪已接收的输入，直到对应运行出现在正文或原回执说明无法启动。
import {useEffect, useState} from "react";
import {useQuery} from "@tanstack/react-query";
import {api, followupIds, trackFollowup} from "./client";
import {parentEvent, type ConversationItem} from "./conversationProjection";
import {useUiLanguage} from "./UiLanguage";

type DispatchReceipt = {
  id: string;
  path: string;
  state: string;
  response?: {messageText?: string; error?: string; out?: {runId?: string; error?: string; code?: string}};
};

function FollowupRow({owner, id, items}: {owner: string; id: string; items: readonly ConversationItem[]}) {
  const {t} = useUiLanguage();
  const query = useQuery({
    queryKey: ["followup-receipt", owner, id],
    queryFn: async () => {
      const {operation} = await api<{operation: DispatchReceipt}>("/api/operations/" + encodeURIComponent(id));
      if (operation.id !== id || operation.path !== "/api/v2/conversations/threads/" + encodeURIComponent(owner) + "/send")
        throw Error("Receipt does not match this conversation");
      return operation;
    },
    refetchInterval: query => {
      const state = query.state.data?.state;
      return !state || state === "processing" || state === "queued" ? 750 : false;
    },
    refetchIntervalInBackground: true,
  });
  const op = query.data;
  const run = op?.response?.out?.runId;
  const shown = op?.state === "completed" && !!run && items.some(item =>
    item.role === "user" && parentEvent(item.event) && item.event.run_id === run);
  useEffect(() => { if (shown) trackFollowup(owner, id, false); }, [owner, id, shown]);
  if (shown) return null;
  const waiting = !op || op.state === "queued" || op.state === "processing";
  const status = op?.state === "completed" ? t("已启动") :
    op?.state === "unknown" || query.isError ? t("派发结果待核对") :
    !op ? t("正在核对派发回执…") : waiting ? t("已接收，等待派发") : t("未启动");
  const error = op?.response?.out?.error || op?.response?.error;
  const label = op?.response?.out?.code === "archived_conversation"
    ? t("会话已归档，这条排队消息未启动。")
    : error ? status + " · " + error : status;
  return <article className="conversation-item message" data-role="user" data-followup-receipt={id}
    data-delivery-state={op?.state || "checking"}>
    <div className="msg user">
      {op?.response?.messageText && <div className="body">{op.response.messageText}</div>}
      <div className="followup-delivery">
        <small role="status">{label}</small>
        {!waiting && <button type="button" aria-label={t("收起回执")} title={t("收起回执")}
          onClick={() => trackFollowup(owner, id, false)}>×</button>}
      </div>
    </div>
  </article>;
}

export function QueuedFollowups({owner, items}: {owner: string; items: readonly ConversationItem[]}) {
  const [ids, setIds] = useState(() => followupIds(owner));
  useEffect(() => {
    const update = () => setIds(followupIds(owner));
    const receiptChanged = (event: Event) => { if ((event as CustomEvent).detail === owner) update(); };
    const storageChanged = (event: StorageEvent) => {
      if (event.key === "workbench-v2-followup-receipts:" + owner || event.key === null) update();
    };
    window.addEventListener("modot-followup-receipts", receiptChanged);
    window.addEventListener("storage", storageChanged);
    update();
    return () => {
      window.removeEventListener("modot-followup-receipts", receiptChanged);
      window.removeEventListener("storage", storageChanged);
    };
  }, [owner]);
  return <>{ids.map(id => <FollowupRow key={id} owner={owner} id={id} items={items}/>)}</>;
}
