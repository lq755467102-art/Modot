// 协调会话附件的导入、解析、撤销和读取，保留未确认操作的状态。
// 主要引用方包括 `AgentHome.tsx`。
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, APIError, loadJSON, saveJSON } from "./client";
import type { Attachment } from "./generated";
import { DocumentPane } from "./DocumentPane";
import { useUiLanguage } from "./UiLanguage";
// 承接 `AgentHome.tsx` 传入的拖放资料，也支持本面板选取文件；保留会话级取消状态以便重新打开后继续核对。
export function AttachmentPane({
  conversationId,
  onBusyChange,
  onError,
  onChange,
  incoming,
  onIncomingAccepted,
  draftIds,
  onAttach,
  onRemoved,
}: {
  incoming?: {id:string;files:File[]};
  onIncomingAccepted: (id: string) => void;
  conversationId: string;
  onBusyChange: (busy: boolean) => void;
  onError: (e: unknown) => void;
  onChange: () => Promise<unknown>;
  draftIds: readonly string[];
  onAttach: (id: string, imported: boolean) => void;
  onRemoved: (id: string) => void;
}) {
  const {t}=useUiLanguage();
  const url =
    "/api/v2/conversations/threads/" +
    encodeURIComponent(conversationId) +
    "/attachments";
  const attachments = useQuery<{ attachments: Attachment[] }>({
    queryKey: ["attachments", conversationId],
    queryFn: () => api(url),
    refetchInterval: 3000,
  });
  const [busy, setBusy] = useState(false),
    [note, setNote] = useState(""),
    [image, setImage] = useState<{id: string; dataUrl: string} | null>(null);
  const imageVisible = image && !attachments.error && attachments.data?.attachments.some(
    a => a.id === image.id && a.permission === "read",
  );
  const cancelled = useRef(false);
  const reader = useRef<FileReader | null>(null);
  const activeImport = useRef("");
  const cancellation = useRef<Promise<void> | null>(null);
  const pendingKey = "attachment-cancel-" + conversationId;
  const [pendingCancel, setPendingCancel] = useState(() =>
    loadJSON<string>(pendingKey, ""),
  );
  // 删除已登记资料后重新核对列表和会话范围，避免旧图片预览继续显示。
  async function removeAttachment(id: string) {
    setBusy(true);
    onBusyChange(true);
    try {
      await api(url, { remove: id });
      setImage(current => current?.id === id ? null : current);
      onRemoved(id);
    } catch (error) {
      onError(error);
    } finally {
      try {
        await attachments.refetch();
        await onChange();
      } catch (error) {
        onError(error);
      } finally {
        setBusy(false);
      }
    }
  }
  useEffect(() => {
    onBusyChange(busy || !!pendingCancel);
  }, [busy, pendingCancel, onBusyChange]);
  // 用同一导入 ID 反复核对撤销。取消存在未确认结果时保留该 ID 供下次打开续查。
  function cancelImport(id: string) {
    if (cancellation.current) return cancellation.current;
    saveJSON(pendingKey, id);
    setPendingCancel(id);
    cancellation.current = (async () => {
      for (let attempt = 0; ; attempt++) {
        try {
          // The client retains the request identity when a reply is lost.
          await api(url, { remove: id });
          break;
        } catch (e) {
          if (attempt >= 4 || !(e instanceof APIError) ||
              (e.code !== "unknown" && e.status !== 202)) throw e;
          await new Promise(resolve => setTimeout(resolve, 400 * (attempt + 1)));
        }
      }
      // A refetch may join a GET that started before the removal.
      const current = await api<{ attachments: Attachment[] }>(url);
      if (current.attachments.some(a => a.id === id))
        throw Error("取消结果尚未确认，请重试核对。");
      saveJSON(pendingKey, "");
      setPendingCancel("");
      await attachments.refetch();
    })().finally(() => { cancellation.current = null; });
    return cancellation.current;
  }
  useEffect(() => {
    const id = loadJSON<string>(pendingKey, "");
    if (id) cancelImport(id).then(onChange).catch(onError);
  }, []);
  // 文件逐个通过 `attachments.py` 先暂存再启用。
  // 取消可能发生在两次请求之间或回执返回之后，均沿原导入 ID 暂停并核对。
  async function upload(files: File[]) {
    if (busy || pendingCancel || !files.length) return;
    setBusy(true);
    onBusyChange(true);
    cancelled.current = false;
    let importId = "";
    setNote("正在校验并导入…");
    try {
      for (const file of files) {
        if (cancelled.current) break;
        if (file.size > 2 * 1024 * 1024) throw Error("单个资料上限为 2 MiB");
        const encoded = await new Promise<string>((resolve, reject) => {
          const r = new FileReader();
          reader.current = r;
          r.onload = () => resolve(String(r.result).split(",")[1]);
          r.onerror = () => reject(Error("读取文件失败"));
          r.onabort = () => reject(Error("已取消导入"));
          r.readAsDataURL(file);
        });
        if (cancelled.current) break;
        importId = crypto.randomUUID();
        activeImport.current = importId;
        await api<Attachment>(url, {
          importId,
          staged: true,
          name: file.name,
          data: encoded,
        });
        if (cancelled.current) {
          await cancelImport(importId);
          importId = "";
          break;
        }
        await api(url, { activate: importId });
        if (cancelled.current) {
          await cancelImport(importId);
          importId = "";
          break;
        }
        onAttach(importId,true);
        importId = "";
      }
      setNote(cancelled.current ? "已取消导入" : "已导入。资料内容尚未读取。");
    } catch (e) {
      onError(e);
      if (cancelled.current && importId) {
        try {
          await cancelImport(importId);
          setNote("已取消导入");
        } catch (cleanup) {
          onError(cleanup);
          setNote("取消尚未确认，请核对后继续。");
        }
      } else
        setNote(
          cancelled.current
            ? "已取消导入"
            : "导入结果未确认。未启用的资料保持暂存状态，请核对后启用或移除。",
        );
    } finally {
      reader.current = null;
      activeImport.current = "";
      try {
        await attachments.refetch();
        await onChange();
      } finally {
        setBusy(false);
      }
    }
  }
  const received=useRef("");
  useEffect(()=>{
    if (!incoming || received.current===incoming.id || busy || pendingCancel) return;
    received.current=incoming.id;
    void upload(incoming.files);
    // 导入面板接手这批文件后清理交接，切回会话时无需重读。
    onIncomingAccepted(incoming.id);
  },[incoming,busy,pendingCancel,onIncomingAccepted]);
  return (
    <section className="attachment-pane" aria-label={t("管理附件")}>
      {pendingCancel && (
        <button
          onClick={() =>
            cancelImport(pendingCancel)
              .then(() => setNote("已核对取消"))
              .catch(onError)
          }
        >
          重试核对取消
        </button>
      )}
      <label>
        添加文件{" "}
        <input
          aria-label="添加文件"
          type="file"
          multiple
          accept=".txt,.md,.png,.jpg,.jpeg,.pdf,.docx,.csv,.tsv,.xlsx,.mp4,.webm"
          disabled={busy || !!pendingCancel}
          onChange={(e) => {
            const files = Array.from(e.target.files || []);
            e.target.value = "";
            upload(files);
          }}
        />
      </label>
      {busy && (
        <button
          onClick={() => {
            cancelled.current = true;
            if (activeImport.current) {
              saveJSON(pendingKey, activeImport.current);
              setPendingCancel(activeImport.current);
              cancelImport(activeImport.current).catch(onError);
            }
            reader.current?.abort();
            setNote("正在取消并清理…");
          }}
        >
          取消导入
        </button>
      )}
      <p role="status">{note}</p>
      {attachments.error && <p role="alert">{attachments.error.message}</p>}
      {attachments.data?.attachments
        .filter((a) => a.permission === "read" || ["staged", "cancelled"].includes(a.status))
        .map((a) => a.status === "cancelled" ? (
          <article key={a.id}>
            <strong>{a.name}</strong> <small>已取消，等待清理</small>
            <button disabled={busy} onClick={() => void removeAttachment(a.id)}>重试清理</button>
          </article>
        ) : (
          <article key={a.id}>
            <strong>{a.name}</strong>
            {a.permission === "read" && <button type="button" disabled={busy || draftIds.includes(a.id)} onClick={()=>onAttach(a.id,false)}>
              {t(draftIds.includes(a.id) ? "已附到当前消息" : "附到当前消息")}
            </button>}
            {a.status === "staged" && (
              <button
                disabled={busy || !!pendingCancel}
                onClick={() => {
                  setBusy(true);
                  api(url, { activate: a.id })
                    .then(() => attachments.refetch())
                    .then(onChange)
                    .catch(onError)
                    .finally(() => setBusy(false));
                }}
              >
                确认启用暂存资料
              </button>
            )}
            <small>
              {" "}
              · {a.bytes} 字节 · {a.width ? `${a.width}×${a.height}` : a.mime.startsWith("video/") ? "视频" : "文本"} ·{" "}
              {a.lastReadAt ? "已提供执行上下文" : "校验完成，尚未读取"}
            </small>
            {a.preview && <pre>{a.preview}</pre>}
            <button
              disabled={busy || a.permission !== "read"}
              onClick={() =>
                window.workbench
                  .exportOutput({ conversationId, attachmentId: a.id })
                  .then((r) =>
                    setNote(
                      r.cancelled ? "已取消另存" : "原件已另存到 " + r.path,
                    ),
                  )
                  .catch(onError)
              }
            >
              另存原件
            </button>
            {!a.document && !a.mime.startsWith("application/") && !a.mime.startsWith("video/") && (
              <button
                disabled={busy || a.permission !== "read"}
                onClick={() => {
                  setBusy(true);
                  api(
                    "/api/v2/conversations/threads/" +
                      conversationId +
                      "/artifacts",
                    { title: a.name, sourceAttachmentId: a.id },
                  )
                    .then(() => setNote("已保存为成果"))
                    .catch(onError)
                    .finally(() => setBusy(false));
                }}
              >
                保存为成果
              </button>
            )}
            {a.document && (
              <DocumentPane
                item={a}
                url={url}
                onRefresh={async () => {
                  await attachments.refetch();
                  await onChange();
                }}
                onError={onError}
              />
            )}
            {a.mime.startsWith("image/") && (
              <button
                disabled={busy}
                onClick={() =>
                  api<{ dataUrl: string }>(url, { preview: a.id })
                    .then((r) => setImage({id: a.id, dataUrl: r.dataUrl}))
                    .catch(onError)
                }
              >
                预览图片
              </button>
            )}
            <button
              disabled={busy}
              onClick={() => void removeAttachment(a.id)}
            >
              移出本对话
            </button>
          </article>
        ))}
      {image && imageVisible && (
        <div>
          <img
            alt="所选资料预览"
            src={image.dataUrl}
            style={{ maxWidth: "100%", maxHeight: 300 }}
          />
          <button onClick={() => setImage(null)}>关闭图片预览</button>
        </div>
      )}
    </section>
  );
}
