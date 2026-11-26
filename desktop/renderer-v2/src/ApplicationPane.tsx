// 管理当前会话的应用预览、打开与停止操作，并展示预览下载状态。
// 主要引用方包括 `AgentHome.tsx`。
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import type { ApplicationPreview } from "./generated";
// `downloads.py` 返回的原下载记录。
// 暂存成功后有 `bytes` 和 `hash`，`path` 表示提交目标，仍需 `state` 确认是否保存成功。
type Download = {id:string;fileName:string;state:string;notice:string;bytes?:number;path?:string;hash?:string};
// 展示当前预览的下载回执。
// `preview-window.cjs` 在文件名未确认时先登记占位记录，这里隐藏尚未产生实际文件名的接收或取消条目。
function Downloads({base}:{base:string}) {
  const rows=useQuery<{downloads:Download[];hasMore:boolean}>({queryKey:["preview-downloads",base],
    queryFn:()=>api(base+"/downloads"),refetchInterval:3000});
  if(rows.error)return <p role="alert">下载记录：{rows.error.message}</p>;
  const visible=rows.data?.downloads.filter(r=>r.fileName!=="download.pending" || !["receiving","cancelled"].includes(r.state));
  if(!visible?.length)return null;
  return <details><summary>下载结果（{visible.length}）</summary>{visible.map(r=><article key={r.id}>
    <strong>{r.fileName} · {({receiving:"接收中",staged:"待另存",committing:"保存中",saved:"已保存",cancelled:"已取消",failed:"失败",unknown:"待核对"} as Record<string,string>)[r.state]||r.state}</strong>
    <p>{r.notice}</p>{r.path&&<p>位置：{r.path}</p>}{r.hash&&<p>SHA256：<code>{r.hash}</code> · {r.bytes} 字节</p>}
  </article>)}{rows.data?.hasMore&&<p>当前显示最近 20 条下载记录。</p>}</details>;
}
// 接收会话标识并查询后端预览。浏览器打开与结束进程按后端预览记录决定可用入口。
export function ApplicationPane({
  conversationId,
  onFeedback,
  onError,
}: {
  conversationId: string;
  onFeedback: (text: string) => void;
  onError: (e: unknown) => void;
}) {
  const base = "/api/v2/conversations/threads/" + conversationId + "/previews";
  const list = useQuery<{ previews: ApplicationPreview[]; hasMore: boolean }>({
    queryKey: ["previews", conversationId],
    queryFn: () => api(base),
    refetchInterval: 4000,
  });
  if (list.error) return <p role="alert">{list.error.message}</p>;
  if (!list.data?.previews.length) return null;
  return (
    <section className="application-pane" aria-label="应用预览成果">
      <h2>应用预览</h2>
      <p>
        仅表示进程和 HTTP
        服务就绪，使用效果仍需检查。预览与主窗口隔离，应用服务使用你已授权的本机运行权限。
      </p>
      {list.data.previews.map((app) => (
        <article key={app.id}>
          <strong>
            {{
              ready: "服务就绪",
              not_ready: "尚未就绪",
              expired: "已失效",
              unknown: "预览状态待核对",
              failed: "启动失败",
              starting: "启动中",
            }[app.state] || app.state}
          </strong>
          <p>
            端口 {app.port} · 版本 {app.revision} · 来源动作{" "}
            {app.source.actionId}
          </p>
          {app.stale && <p>代码已经变化，此实例记录的启动版本保持不变。</p>}
          {app.process && (
            <p>
              进程：{app.process.state}
              {app.canStop
                ? "；可在此停止"
                : ["completed", "failed", "interrupted"].includes(
                      app.process.state,
                    )
                  ? "；已结束"
                  : "；停止权限待核对"}
            </p>
          )}
          <button
            disabled={app.state !== "ready"}
            onClick={() =>
              window.workbench
                .openPreview({ conversationId, previewId: app.id })
                .catch(onError)
            }
          >
            打开隔离预览
          </button>
          <button
            onClick={() =>
              onFeedback(
                "应用预览 " +
                  app.id +
                  "，进程 " +
                  app.processId +
                  "，启动版本 " +
                  app.revision +
                  "。我的观察与修改要求：",
              )
            }
          >
            反馈运行效果
          </button>
          <button
            disabled={!app.canStop}
            onClick={() =>
              api(base + "/" + app.id + "/stop", {})
                .then(() => list.refetch())
                .catch(onError)
            }
          >
            停止应用
          </button>
          {(app.log || app.error) && (
            <details>
              <summary>启动日志与错误</summary>
              {app.error && <p role="alert">{app.error}</p>}
              {app.log && <pre>{app.log}</pre>}
            </details>
          )}
          <Downloads base={base+"/"+app.id}/>
        </article>
      ))}
      {list.data.hasMore && <p>当前显示最近 20 个预览实例。</p>}
    </section>
  );
}
