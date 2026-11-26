// The active CodeMirror document supplies both human and Agent previews. Each edit,
// including undo, changes the revision; a late receipt cannot replace later typing.
// `EditorView.tsx` 挂载格式化面板。`formattingScope.ts` 提供当前授权范围，`applicationTesting.ts` 提供应用的子插件和授权状态。
import { useContext, useEffect, useRef, useState } from 'react';
import {createPortal} from 'react-dom';
import { isolateHistory } from '@codemirror/commands';
import { StateField, Transaction } from '@codemirror/state';
import type { EditorView } from '@codemirror/view';
import { useUiLanguage } from './UiLanguage';
import { api, APIError } from './client';
import { documentLanguage } from './editorSnippets';
import { formattingSlot, useAppChildren, workroomApp, type HostedChild } from './applicationTesting';

import { FormattingScope, type Scope } from './formattingScope';
// 直接使用 CodeMirror 的文档事务推进版本。草稿内容变化会使旧预览失效，单纯移动光标不会。
export const formatRevision = StateField.define<number>({
  create: () => 0,
  update: (revision, tr) => revision + Number(tr.docChanged),
});
// `documentId` 绑定草稿版本，`previewId` 标识一次任务。`diff` 用于审阅，`text` 供本编辑器应用，失败时返回 `error`。
type Review = {
  previewId: string;
  documentId: string;
  state: string;
  diff?: string;
  text?: string;
  error?: string;
  changed?: boolean;
};
// 状态轮询返回任务身份和阶段。`needsDraft` 是后端请求本编辑器交付这版草稿正文的信号。
type Status = { documentId: string; previewId: string | null; state: string | null; needsDraft: boolean };
// `EditorView.tsx` 传入工作间的草稿与文件路径，并通过 `view` 提供实时 CodeMirror 实例。
type Props = { value: string; path: string; view: () => EditorView | undefined; toolsTarget?: HTMLElement | null };

// 按范围、路径、包和授权版本区分格式化面板。这些标识变化时，从新的编辑器登记开始。
export function DocumentFormatting(props: Props) {
  const { t } = useUiLanguage();
  const scope = useContext(FormattingScope);
  const listing = useAppChildren(workroomApp);
  const [selected, setSelected] = useState('');
  const choices =
    listing.data?.children.filter((p) => p.slot === formattingSlot && p.enabled && p.canRun) ?? [];
  const child = choices.find((p) => p.packageId === selected) ?? choices[0];
  const hostReady = listing.data?.slots[formattingSlot]?.available === true;
  if (!scope || (!child && !listing.isError)) return null;
  const refresh = () => void listing.refetch();
  return (
    <div className="workroom-format">
      {(listing.isError || !hostReady) && (
        <section className="workroom-format-readiness" aria-label={t('格式化槽位')}>
          <span role={listing.isError ? 'alert' : 'status'}>
            {t(listing.isError ? '格式化槽位读取失败。' : '格式化宿主未就绪。')}
          </span>
          <button type="button" disabled={listing.isFetching} onClick={refresh}>
            {t(listing.isFetching ? '正在重读…' : '重试读取槽位')}
          </button>
        </section>
      )}
      {child && choices.length > 1 && (
        <label>
          文档格式化器{' '}
          <select value={child.packageId} onChange={(e) => setSelected(e.target.value)}>
            {choices.map((p) => (
              <option key={p.packageId} value={p.packageId}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {child && (
        <FormatDocument
          key={JSON.stringify([scope, props.path, child.packageId, child.revision])}
          {...props}
          scope={scope}
          child={child}
          canPreview={hostReady && !listing.isError}
          refresh={refresh}
        />
      )}
    </div>
  );
}

// `scope` 和 `child` 固定一次面板登记的归属。`refresh` 在授权被撤销时刷新 `applicationTesting.ts` 的子插件列表。
function FormatDocument({
  path,
  value,
  view,
  scope,
  child,
  toolsTarget,
  canPreview,
  refresh,
}: Props & { scope: Scope; child: HostedChild; canPreview: boolean; refresh: () => void }) {
  const [editor] = useState(() => crypto.randomUUID().replaceAll('-', ''));
  const route = '/api/v2/plugins/applications/' + workroomApp + '/formatting/' + editor + '/';
  const [status, setStatus] = useState<Status>();
  const [review, setReview] = useState<Review>();
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const callbacks = useRef({ view, refresh });
  callbacks.current = { view, refresh };
  const mounted = useRef(false);
  const wake = useRef<() => void>(() => {});
  const received = useRef('');
  const lastApplied = useRef('');
  // 以 CodeMirror 当前事务版本构造文档 ID。这样可拒收输入后返回的旧回执。
  function currentId() {
    const cm = callbacks.current.view();
    return cm && editor + '.' + cm.state.field(formatRevision);
  }

  useEffect(() => {
    let live = true,
      pending = false,
      sent = '',
      awaiting: Promise<void> | undefined;
    mounted.current = true;
    // 每个面板同一时间只协调一次登记或轮询。读取 `client.ts` 的 API 结果后，核对面板仍存在且草稿版本相同。
    const sync = async () => {
      const cm = callbacks.current.view();
      if (pending || !cm || !live) return;
      pending = true;
      try {
        const seq = cm.state.field(formatRevision),
          id = editor + '.' + seq;
        if (sent !== id) {
          const text = cm.state.doc.toString();
          if (new TextEncoder().encode(text).length > 256 * 1024) {
            if (sent) await api(route + 'close', {}, true);
            sent = '';
            if (live) {
              setStatus(undefined);
              setReview(undefined);
              setNote('格式化支持 256 KiB 以内的草稿。');
            }
            return;
          }
          // 先登记标识、语言及授权范围。草稿正文由下方收到 `needsDraft` 后单独交给后端。
          await api(
            route + 'document',
            {
              ...scope,
              path,
              language: documentLanguage(path),
              seq,
              package: child.packageId,
              revision: child.revision,
            },
            true,
          );
          if (!live || currentId() !== id) return;
          if (sent && sent !== lastApplied.current) setNote('草稿已更新，可重新预览。');
          lastApplied.current = '';
          sent = id;
          received.current = '';
          setReview(undefined);
        }
        const next = await api<Status>(route + 'status');
        if (!live || currentId() !== next.documentId) return;
        // 编辑器收到具体预览任务请求时，才按 `documentId` 和 `previewId` 交付这版未保存正文。
        if (next.needsDraft && currentId() === next.documentId) {
          await api(
            route + 'source',
            { documentId: next.documentId, previewId: next.previewId, text: cm.state.doc.toString() },
            true,
          );
          if (!live || currentId() !== next.documentId) return;
        }
        setStatus((old) =>
          old?.documentId === next.documentId && old.previewId === next.previewId && old.state === next.state
            ? old
            : next,
        );
        // 任务结束后读取差异和完整格式化文本。同一预览只取一次，并核对请求期间是否切换文件或写入新草稿。
        if (next.previewId && next.state !== 'running' && received.current !== next.previewId) {
          const result = await api<Review>(route + 'review');
          if (!live || currentId() !== result.documentId || result.previewId !== next.previewId) return;
          received.current = result.previewId;
          setReview(result);
          setNote('');
        } else if (!next.previewId) {
          setReview(undefined);
          received.current = '';
        }
      } catch (e) {
        if (!live) return;
        setStatus(undefined);
        setReview(undefined);
        sent = '';
        if (e instanceof APIError && e.code === 'plugin_denied') callbacks.current.refresh();
        setNote(e instanceof Error ? e.message : '未能同步工作间草稿。');
      } finally {
        pending = false;
      }
    };
    // 编辑和定时轮询共用这个入口，已有同步请求时等下一次唤醒。退出时等最后一条请求结束再撤销登记。
    wake.current = () => {
      if (!pending) awaiting = sync();
    };
    wake.current();
    const timer = setInterval(() => wake.current(), 1000);
    return () => {
      live = false;
      mounted.current = false;
      clearInterval(timer);
      // Close after the last publication so a slow request cannot resurrect an old file tab.
      void Promise.resolve(awaiting)
        .then(() => api(route + 'close', {}, true))
        .catch(() => {});
    };
  }, [route]);

  useEffect(() => {
    wake.current();
  }, [value]);

  // 三个按钮共用操作入口。预览只需要当前文档登记，应用或放弃还要匹配已经展示的预览回执。
  async function action(op: 'preview' | 'accept' | 'dismiss') {
    const documentId = currentId();
    if (
      !status ||
      !documentId ||
      status.documentId !== documentId ||
      (op !== 'preview' && (!review || review.documentId !== documentId))
    ) {
      setNote('草稿已变化，请重新预览。');
      wake.current();
      return;
    }
    setBusy(true);
    try {
      await api(route + op, { documentId, previewId: review?.previewId }, true);
      if (!mounted.current) return;
      const cm = callbacks.current.view();
      if (!cm || currentId() !== documentId) {
        setNote('草稿已变化，请重新预览。');
        return;
      }
      // 后端确认后，以一个独立的 CodeMirror 撤销步骤替换草稿正文。磁盘写入仍由工作间的保存入口发起。
      if (op === 'accept' && review?.text !== undefined && review.changed) {
        lastApplied.current = documentId;
        cm.dispatch({
          changes: { from: 0, to: cm.state.doc.length, insert: review.text },
          annotations: [Transaction.userEvent.of('input.format'), isolateHistory.of('full')],
        });
        setNote('已应用到草稿，可撤销或保存。');
      } else setNote(op === 'dismiss' ? '已放弃预览。' : '正在生成格式化差异…');
      setReview(undefined);
      wake.current();
    } catch (e) {
      if (mounted.current) {
        setNote(e instanceof Error ? e.message : '格式化预览已失效。');
        if (op === 'preview' && e instanceof APIError && e.code === 'app_host_unavailable') refresh();
        wake.current();
      }
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  const valid = !!status && status.documentId === currentId();
  const launch=<button
        type="button"
        aria-controls={review ? 'format-review-' + review.previewId : undefined}
        title={child.name + ' · 启动本机扩展并预览草稿差异'}
        disabled={!canPreview || busy || !valid || status?.state === 'running' || !!review}
        onClick={() => void action('preview')}
      >
        格式化当前文件…
      </button>;
  return (
    <section aria-label="文档格式化">
      {toolsTarget?createPortal(launch,toolsTarget):launch}
      {status?.state === 'running' && <span role="status"> 正在生成格式化差异…</span>}
      {note && <span role="status"> {note}</span>}
      {review && valid && (
        <div
          className="workroom-format-review"
          role="region"
          id={'format-review-' + review.previewId}
          data-document-id={review.documentId}
          aria-label="格式化差异"
        >
          <strong>
            {path} · {child.name}
          </strong>
          {review.state === 'failed' ? (
            <p role="alert">{review.error}</p>
          ) : review.changed ? (
            <pre tabIndex={0}>{review.diff?.split('\n').map((line,index,lines)=><span key={index}
              className={line.startsWith('+')?'format-add':line.startsWith('-')?'format-remove':undefined}>{line}{index+1<lines.length?'\n':''}</span>)}</pre>
          ) : (
            <p>扩展未给出修改。</p>
          )}
          {review.changed && (
            <button disabled={busy} onClick={() => void action('accept')}>
              应用到草稿
            </button>
          )}
          <button disabled={busy} onClick={() => void action('dismiss')}>
            {review.changed ? '放弃预览' : '关闭预览'}
          </button>
        </div>
      )}
    </section>
  );
}
