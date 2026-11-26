// 汇集代理档位、模型、授权与维护等管理入口。
// 主要引用方包括 `ApplicationSettings.tsx`。
import { useState } from "react";
import { useDialogFocus } from "./useDialogFocus";
import { useQuery } from "@tanstack/react-query";
import { api, queryString } from "./client";
import type {
  ManagementView,
  MigrationPlan,
  LegacyEventPage,
  HumanActionPage,
} from "./generated";

export function ManagementCenter({
  onClose,
  onSettings,
  onError: reportParent,
}: {
  onClose: () => void;
  onSettings: () => void;
  onError: (e: unknown) => void;
}) {
  const [localError, setLocalError] = useState("");
  function onError(e: unknown) {
    setLocalError(e instanceof Error ? e.message : String(e));
    reportParent(e);
  }
  const [busy, setBusy] = useState(false),
    [plan, setPlan] = useState<MigrationPlan | null>(null),
    [history, setHistory] = useState("");
  const [historyOffset, setHistoryOffset] = useState(0),
    [audit, setAudit] = useState(false),
    [before, setBefore] = useState<number | null>(null);
  const [eventBefore, setEventBefore] = useState(0),
    [eventGeneration, setEventGeneration] = useState("");
  const management = useQuery<ManagementView>({
    queryKey: ["management", historyOffset],
    gcTime: 0,
    queryFn: () => api("/api/v2/migration?offset=" + historyOffset),
  });
  const legacy = useQuery<LegacyEventPage>({
    queryKey: ["legacy-history", history, eventBefore, eventGeneration],
    gcTime: 0,
    enabled: !!history,
    refetchInterval: (query) => (query.state.data?.catchingUp ? 1000 : false),
    queryFn: () =>
      api(
        "/api/v2/migration/history/" +
          queryString(history) +
          (eventBefore
            ? "?before_offset=" +
              eventBefore +
              "&generation=" +
              queryString(eventGeneration)
            : ""),
      ),
  });
  const actions = useQuery<HumanActionPage>({
    queryKey: ["human-actions", before],
    gcTime: 0,
    enabled: audit,
    queryFn: () =>
      api("/api/v2/human-actions" + (before ? "?before=" + before : "")),
  });
  async function change(
    operation: string,
    current: MigrationPlan | null = plan,
  ) {
    if (busy) return;
    if (
      operation !== "preview" &&
      !confirm(
        "确认此迁移预览？只建立只读历史索引，备份受影响元数据；不会授予目录权限或重跑旧任务。",
      )
    )
      return;
    setBusy(true);
    try {
      const next = await api<MigrationPlan>("/api/v2/migration", {
        operation,
        ...(current
          ? { planId: current.id, planHash: current.planHash, confirm: true }
          : {}),
      });
      setPlan(next);
      await management.refetch();
    } catch (e) {
      onError(e);
      await management.refetch();
      if (current)
        try {
          setPlan(await api("/api/v2/migration/plans/" + current.id));
        } catch {}
    } finally {
      setBusy(false);
    }
  }
  const data = management.data;
  const dialogRef = useDialogFocus(onClose, !busy);
  return (
    <div className="modal-backdrop">
      <section
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label="管理与历史"
        className="settings-dialog"
      >
        <div className="tool-title">
          <h2>管理与历史</h2>
          <button onClick={onClose}>关闭管理</button>
        </div>
        {management.isPending && <p role="status">正在核对管理与历史记录…</p>}
        {localError && <p role="alert">{localError}</p>}
        {management.error && (
          <p role="alert">
            管理状态不可读取：{management.error.message}。已有数据保留。
          </p>
        )}
        {data && (
          <>
            <p>{data.notice}</p>
            <details>
              <summary>数据版本与兼容状态</summary>
              <pre>{JSON.stringify(data.format, null, 2)}</pre>
            </details>
            <h3>管理入口与迁移状态</h3>
            <table>
              <thead>
                <tr>
                  <th>能力</th>
                  <th>当前入口</th>
                  <th>旧入口</th>
                </tr>
              </thead>
              <tbody>
                {data.inventory.map((row) => (
                  <tr key={row.id}>
                    <td>{row.name}</td>
                    <td>
                      {row.entry} ·{" "}
                      {row.state === "new_entry_verified"
                        ? "新版已验收"
                        : "兼容功能保留"}
                    </td>
                    <td>
                      {row.legacyWrites ? "继续保留" : "已迁移，旧写入关闭"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <button disabled={busy} onClick={onSettings}>
              打开新版设置
            </button>
            <p>记忆与原树管理位于左侧“记忆”，Skills与外部工具位于“扩展”；运行事实可从会话的工具步骤审查查看。</p>
            <h3>只读历史迁移</h3>
            <button disabled={busy} onClick={() => change("preview", null)}>
              预览下一批旧历史
            </button>
            <p>
              每批最多100项/16MiB元数据，已索引内容不重复导入；原会话、成果和两套界面草稿都保留。
            </p>
            {!!data.plans.length && (
              <details>
                <summary>已有迁移计划与中断记录</summary>
                {data.plans.map((p: any) => (
                  <p key={p.id}>
                    <button
                      disabled={busy}
                      onClick={async () => {
                        setPlan(null);
                        setBusy(true);
                        try {
                          setPlan(await api("/api/v2/migration/plans/" + p.id));
                        } catch (e) {
                          onError(e);
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      {p.id} · {p.state}
                    </button>
                  </p>
                ))}
              </details>
            )}
            {plan && (
              <article>
                <h4>
                  预览 {plan.id} · {plan.state}
                </h4>
                <p>{plan.notice}</p>
                <p>
                  确认版本 <code>{plan.planHash}</code>
                </p>
                <table>
                  <thead>
                    <tr>
                      <th>原会话</th>
                      <th>关联依据</th>
                      <th>结果</th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.candidates.map((item) => (
                      <tr key={item.id}>
                        <td>{item.title || item.id}</td>
                        <td>
                          {item.cwd || "工作目录未知"}
                          <details>
                            <summary>原元数据版本</summary>
                            <pre>{JSON.stringify(item.hashes, null, 2)}</pre>
                          </details>
                        </td>
                        <td>
                          {item.workspaceId
                            ? "匹配已有工作区"
                            : "未自动关联项目"}
                          ；{item.reason}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <button
                  disabled={busy || plan.state !== "prepared"}
                  onClick={() => change("apply")}
                >
                  确认备份并建立只读索引
                </button>
                {["backing_up", "unknown", "applying"].includes(plan.state) && (
                  <button disabled={busy} onClick={() => change("reconcile")}>
                    核对中断迁移
                  </button>
                )}
                {plan.backup && <p>迁移备份：{plan.backup}</p>}
                {plan.hasMore && <p>此批之外仍有历史；完成后可预览下一批。</p>}
              </article>
            )}
            <h3>原始历史（只读，不代表已获项目权限）</h3>
            {data.history.slice(0, 50).map((item) => (
              <button
                key={item.id}
                onClick={() => {
                  setHistory(item.id);
                  setEventBefore(0);
                  setEventGeneration("");
                }}
              >
                {item.title || item.id} ·{" "}
                {item.workspaceId ? "有cwd关联" : "关联未知"}
              </button>
            ))}
            {!data.history.length && (
              <p>尚未建立旧历史索引，或当前数据没有旧会话。</p>
            )}
            <button
              disabled={!historyOffset}
              onClick={() => setHistoryOffset(Math.max(0, historyOffset - 50))}
            >
              上一页历史
            </button>
            <button
              disabled={historyOffset + 50 >= (data.totalHistory || 0)}
              onClick={() => setHistoryOffset(historyOffset + 50)}
            >
              下一页历史
            </button>
            {history && (
              <div>
                <button
                  onClick={() => {
                    setEventBefore(0);
                    setEventGeneration("");
                    legacy.refetch();
                  }}
                >
                  最新原记录
                </button>
                <button
                  disabled={!legacy.data?.hasMore || !legacy.data?.beforeOffset}
                  onClick={() => {
                    setEventBefore(legacy.data!.beforeOffset!);
                    setEventGeneration(legacy.data!.generation);
                  }}
                >
                  更早原记录
                </button>
              </div>
            )}
            {history && legacy.isPending && (
              <p role="status">正在读取原历史记录…</p>
            )}
            {legacy.error && (
              <p role="alert">原历史不可核对：{legacy.error.message}。</p>
            )}
            {legacy.data && (
              <details open>
                <summary>只读原记录 · {legacy.data.item.id}</summary>
                <p>
                  {legacy.data.historyNotice ||
                    "当前仅显示最近200条原记录；不补造旧工具身份。"}
                </p>
                {legacy.data.item.legacyTrace && (
                  <p>该旧版仅有trace格式，原文件保留，请从兼容管理查看。</p>
                )}
                <pre>{JSON.stringify(legacy.data.events, null, 2)}</pre>
              </details>
            )}
            <h3>人工接管记录</h3>
            <button aria-expanded={audit} onClick={() => setAudit(!audit)}>
              查看人工接口动作
            </button>
            {audit && (
              <>
                <p>{actions.data?.notice}</p>
                {actions.error && (
                  <p role="alert">人工记录不可核对：{actions.error.message}</p>
                )}
                {actions.data?.items.map((row) => (
                  <details key={row.id}>
                    <summary>
                      {new Date(row.started * 1000).toLocaleString()} · 人工 ·{" "}
                      {row.path} ·{" "}
                      {row.state === "acknowledged"
                        ? "接口已收讫"
                        : "结果待核对"}
                    </summary>
                    <pre>{JSON.stringify(row, null, 2)}</pre>
                  </details>
                ))}
                <button
                  onClick={() => {
                    setBefore(null);
                    actions.refetch();
                  }}
                >
                  最新人工记录
                </button>
                <button
                  disabled={!actions.data?.hasMore}
                  onClick={() => setBefore(actions.data?.nextBefore ?? null)}
                >
                  较早人工记录
                </button>
              </>
            )}
          </>
        )}
      </section>
    </div>
  );
}
