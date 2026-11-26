// 展示导入文档的解析状态、目录和可分页的文本摘录。
// 主要引用方包括 `AttachmentPane.tsx`。
import { useEffect, useRef, useState } from "react";
import { api } from "./client";
import type { Attachment, DocumentPreview } from "./generated";
const labels: Record<string, string> = {
  queued: "排队",
  running: "解析中",
  ready: "完成",
  partial: "部分完成",
  failed: "失败",
  cancelled: "已取消",
};
export function DocumentPane({
  item,
  url,
  onRefresh,
  onError,
}: {
  item: Attachment;
  url: string;
  onRefresh: () => Promise<unknown>;
  onError: (error: unknown) => void;
}) {
  const [view, setView] = useState<DocumentPreview | null>(null),
    [busy, setBusy] = useState(false);
  const info = item.document;
  const attempt = useRef(info?.attempt);
  attempt.current = info?.attempt;
  useEffect(() => setView(null), [info?.attempt]);
  if (!info) return null;
  async function open(locator?: string, offset = 0) {
    const owner = attempt.current;
    setBusy(true);
    try {
      const result = await api<DocumentPreview>(url, {
        preview: item.id,
        locator,
        offset,
      });
      if (owner === attempt.current) setView(result);
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }
  async function action(command: string) {
    setBusy(true);
    setView(null);
    try {
      await api(url, { [command]: item.id, expectedAttempt: info?.attempt });
      await onRefresh();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="document-pane" aria-label={"文档解析 " + item.name}>
      <p role="status">
        解析：{labels[info.state] || info.state}{" "}
        {info.count !== undefined ? `· ${info.count} 个来源位置` : ""}
      </p>
      {info.error && <p role="alert">{info.error}</p>}
      {info.warnings?.map((warning, i) => (
        <p key={i}>未覆盖：{warning}</p>
      ))}
      {info.notes?.map((note, i) => (
        <p key={i}>{note}</p>
      ))}
      {["queued", "running"].includes(info.state) ? (
        <button disabled={busy} onClick={() => action("cancelParse")}>
          取消解析
        </button>
      ) : (
        <button disabled={busy} onClick={() => action("parse")}>
          重新解析
        </button>
      )}
      {["ready", "partial"].includes(info.state) && (
        <button disabled={busy} onClick={() => open()}>
          查看来源
        </button>
      )}
      {view && (
        <section aria-label="文档来源预览">
          <small>
            来源：{view.source.locator} · 原件 SHA256 {view.source.hash} ·{" "}
            {view.source.parserVersion}
          </small>
          <div>
            {view.locations.map((locator) => (
              <button
                disabled={busy}
                key={locator}
                onClick={() => open(locator)}
              >
                {view.sheets?.find(sheet => sheet.locator === locator)?.name ?? locator}
              </button>
            ))}
          </div>
          {view.nextLocationOffset != null && (
            <button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                const owner = attempt.current;
                const previous = view;
                try {
                  const next = await api<{
                    locations: string[];
                    nextOffset: number | null;
                  }>(url, {
                    preview: item.id,
                    locator: "locations",
                    offset: view.nextLocationOffset,
                  });
                  if (owner === attempt.current)
                    setView((current) =>
                      current === previous
                        ? {
                            ...current,
                            locations: [
                              ...current.locations,
                              ...next.locations,
                            ],
                            nextLocationOffset: next.nextOffset,
                          }
                        : current,
                    );
                } catch (e) {
                  onError(e);
                } finally {
                  setBusy(false);
                }
              }}
            >
              更多来源位置
            </button>
          )}
          <pre>{view.text}</pre>
          {view.nextOffset != null && (
            <button
              disabled={busy}
              onClick={() => open(view.source.locator, view.nextOffset!)}
            >
              继续读取该位置
            </button>
          )}
          {view.partial && (
            <p>这是部分解析结果，仍需核对原件中的未覆盖内容。</p>
          )}
        </section>
      )}
    </div>
  );
}
