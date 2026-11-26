// 列出当前会话的成果和版本，并提供预览、导出与纠正入口。
// 主要引用方包括 `AgentHome.tsx`。
import { useRef, useState, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, loadJSON, saveJSON } from "./client";
import { Message } from "./Panels";
import { CorrectionForm } from "./Corrections";
import type { Artifact, ArtifactRead } from "./generated";

// 正文由成果版本接口给出，图片用已授权的预览数据，Markdown 交给 `Panels.tsx` 的消息渲染。
function Preview({ value }: { value: ArtifactRead }) {
  if (value.dataUrl)
    return (
      <img
        alt={value.title}
        src={value.dataUrl}
        style={{ maxWidth: "100%", maxHeight: 240 }}
      />
    );
  return value.version.mime === "text/markdown" ? (
    <Message text={value.text || ""} />
  ) : (
    <pre>{value.text}</pre>
  );
}
// 按具体成果版本读取正文、比对和提交纠正。翻页和并发比对状态随版本切换重置。
function ArtifactVersionView({
  conversationId,
  item,
  versionId,
  onAsk,
  onError,
}: {
  conversationId: string;
  item: Artifact;
  versionId: string;
  onAsk: (reference: string) => void;
  onError: (e: unknown) => void;
}) {
  const base =
    "/api/v2/conversations/threads/" +
    encodeURIComponent(conversationId) +
    "/artifacts/" +
    item.id;
  const [offset, setOffset] = useState(0);
  const compareEpoch = useRef(0);
  const view = useQuery<ArtifactRead>({
    queryKey: ["artifact-version", conversationId, item.id, versionId, offset],
    queryFn: () => api(base + "/versions/" + versionId + "?offset=" + offset),
  });
  const [left, setLeft] = useState(
      item.versions.filter((v) => v.id !== versionId).at(-1)?.id || "",
    ),
    [comparison, setComparison] = useState<{
      left: ArtifactRead;
      right: ArtifactRead;
      diff: string;
      truncated: boolean;
    } | null>(null),
    [busy, setBusy] = useState(false),
    [note, setNote] = useState("");
  return (
    <div>
      {view.error && (
        <p role="alert">
          {view.error.message}
          <button onClick={() => view.refetch()}>重新核对成果</button>
        </p>
      )}
      {view.data && (
        <>
          <CorrectionForm
            key={versionId}
            conversationId={conversationId}
            target={{ kind: "artifact", id: item.id, versionId }}
          />
          <small>
            SHA256 {view.data.version.hash} · 来源{" "}
            {view.data.version.source.actor} · 动作{" "}
            {view.data.version.source.actionId || "未记录"}
          </small>
          {view.data.version.width != null && (
            <p>
              输出：{view.data.version.width}×{view.data.version.height} ·{" "}
              {view.data.version.mime}
              {view.data.version.inputResource?.width != null && (
                <span>
                  ；原图：
                  {view.data.version.inputResource.width}×
                  {view.data.version.inputResource.height} ·{" "}
                  {view.data.version.inputResource.format}
                </span>
              )}
            </p>
          )}
          <Preview value={view.data} />
        </>
      )}
      {offset > 0 && (
        <button onClick={() => setOffset(Math.max(0, offset - 8000))}>
          上一段成果正文
        </button>
      )}
      {view.data?.nextOffset != null && (
        <button onClick={() => setOffset(view.data!.nextOffset!)}>
          继续查看成果正文
        </button>
      )}
      <button
        disabled={!view.data}
        onClick={() =>
          window.workbench
            .exportOutput({ conversationId, artifactId: item.id, versionId })
            .then((r) =>
              setNote(
                r.cancelled
                  ? "已取消导出"
                  : "已导出到 " +
                      r.path +
                      (r.backup ? "；原件备份 " + r.backup : ""),
              ),
            )
            .catch(onError)
        }
      >
        导出这个版本
      </button>
      <button
        onClick={() =>
          onAsk(
            `成果「${item.title}」，artifact_id=${item.id}，version_id=${versionId}。请用 read_artifact 读取这个版本后回答。我的问题：`,
          )
        }
      >
        就此成果追问
      </button>
      {item.versions.length > 1 && (
        <>
          <label>
            比较版本{" "}
            <select
              aria-label="比较成果版本"
              disabled={busy}
              value={left}
              onChange={(e) => {
                compareEpoch.current++;
                setLeft(e.target.value);
                setComparison(null);
              }}
            >
              {item.versions
                .filter((v) => v.id !== versionId)
                .map((v, i) => (
                  <option key={v.id} value={v.id}>
                    版本 {item.versions.findIndex((x) => x.id === v.id) + 1} ·{" "}
                    {v.hash.slice(0, 8)}
                  </option>
                ))}
            </select>
          </label>
          <button
            disabled={busy || !left || left === versionId}
            onClick={async () => {
              setBusy(true);
              setComparison(null);
              const epoch = ++compareEpoch.current;
              try {
                const value = await api(base + "/compare", {
                  left,
                  right: versionId,
                });
                if (epoch === compareEpoch.current) setComparison(value);
              } catch (e) {
                onError(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            比较两版
          </button>
        </>
      )}
      {comparison && (
        <section aria-label="成果版本对照">
          <small>
            左版本 {comparison.left.version.id} · 右版本{" "}
            {comparison.right.version.id}
          </small>
          <p>
            {comparison.left.version.hash === comparison.right.version.hash
              ? "两版内容相同"
              : "两版内容不同"}
          </p>
          {comparison.left.dataUrl || comparison.right.dataUrl ? (
            <div className="artifact-image-comparison">
              <Preview value={comparison.left} />
              <Preview value={comparison.right} />
            </div>
          ) : (
            <pre>{comparison.diff}</pre>
          )}
          {comparison.truncated && <p>差异展示已截断，请分别查看完整版本。</p>}
        </section>
      )}
      {note && <p role="status">{note}</p>}
    </div>
  );
}
// `AgentHome.tsx` 可以指定证据关联的成果版本。
// 原选择失效时显示可用版本并提醒重新核对。
export function ArtifactPane({
  conversationId,
  focus,
  onAsk,
  onError,
}: {
  conversationId: string;
  focus?: {id: string; version: string};
  onAsk: (reference: string) => void;
  onError: (e: unknown) => void;
}) {
  const base =
    "/api/v2/conversations/threads/" +
    encodeURIComponent(conversationId) +
    "/artifacts";
  const list = useQuery<{ artifacts: Artifact[] }>({
    queryKey: ["artifacts", conversationId],
    queryFn: () => api(base),
    refetchInterval: 2000,
  });
  const selectionKey = "artifact-selection-" + conversationId;
  const [choice, setChoice] = useState<{
    selected: string;
    versions: Record<string, string>;
  }>(() => loadJSON(selectionKey, { selected: "", versions: {} }));
  useEffect(() => {
    if (focus) setChoice(old=>({selected:focus.id,versions:{...old.versions,[focus.id]:focus.version}}));
  }, [focus]);
  useEffect(() => saveJSON(selectionKey, choice), [selectionKey, choice]);
  const selected = choice.selected;
  const item =
    list.data?.artifacts.find((a) => a.id === selected) ||
    list.data?.artifacts.at(-1);
  if (list.error) return <p role="alert">{list.error.message}</p>;
  if (!item) return null;
  const version = choice.versions[item.id] || "";
  const current =
    item.versions.find((v) => v.id === version) || item.versions.at(-1)!;
  return (
    <section className="artifact-pane" aria-label="成果">
      <h2>成果</h2>
      <p>文件生成与任务检查、用户验收分别记录。</p>
      {((selected && selected !== item.id) ||
        (version && !item.versions.some((v) => v.id === version))) && (
        <p role="status">
          原选择已不可用；当前显示可用版本，请重新核对后再作决定。
        </p>
      )}
      <label>
        成果{" "}
        <select
          aria-label="选择成果"
          value={item.id}
          onChange={(e) => {
            const id = e.target.value;
            setChoice((old) => ({ ...old, selected: id }));
          }}
        >
          {list.data?.artifacts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.title}
            </option>
          ))}
        </select>
      </label>
      <label>
        版本{" "}
        <select
          aria-label="成果版本"
          value={current.id}
          onChange={(e) => {
            const value = e.target.value;
            setChoice((old) => ({
              ...old,
              selected: item.id,
              versions: { ...old.versions, [item.id]: value },
            }));
          }}
        >
          {item.versions.map((v, i) => (
            <option key={v.id} value={v.id}>
              版本 {i + 1} · {v.state} · {v.hash.slice(0, 8)}
            </option>
          ))}
        </select>
      </label>
      <button
        onClick={() =>
          api(base + "/" + item.id + "/permission", {
            permission: item.permission === "read" ? "none" : "read",
            confirm: true,
          })
            .then(() => list.refetch())
            .catch(onError)
        }
      >
        {item.permission === "read" ? "撤销成果读取" : "恢复成果读取"}
      </button>
      {item.permission !== "read" ? (
        <p>成果读取已撤销，当前不显示内容。</p>
      ) : current.state !== "ready" ? (
        <p>成果状态：{current.state}，尚不能作为完整文件交付。</p>
      ) : (
        <ArtifactVersionView
          key={item.id + ":" + current.id}
          conversationId={conversationId}
          item={item}
          versionId={current.id}
          onAsk={onAsk}
          onError={onError}
        />
      )}
    </section>
  );
}
