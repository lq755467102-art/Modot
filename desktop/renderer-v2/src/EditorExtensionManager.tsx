// 代码工作间卡片与编辑菜单共用这一个管理面板。预览包不会安装，卸载只删除选中的子插件记录。
import { useRef, useState } from 'react';
import { Dialog } from './Dialog';
import { LanguagePluginRow } from './LanguagePluginRow';
import { useLanguageStatus } from './workroomLanguage';
import { useEditorPacks, changeEditorPack, removeEditorPacks } from './editorExtensionStore';
import type { EditorPack } from './editorSnippets';
import {
  workroomApp,
  formattingSlot,
  slotLabel,
  hostedChildStatus,
  useAppChildren,
  useRefreshChildren,
  installHosted,
  changeHosted,
  type HostedPreview,
} from './applicationTesting';
import './editor-extensions.css';

// 点已安装的子插件时，应用卡片传入它的身份。编辑菜单和“添加”入口直接打开包列表。
export function EditorExtensionManager({ initialId, onClose }: { initialId?: string; onClose: () => void }) {
  const packs = useEditorPacks(),
    input = useRef<HTMLInputElement>(null);
  const hosted = useAppChildren(workroomApp),
    refresh = useRefreshChildren(workroomApp);
  const listing = hosted.isError ? undefined : hosted.data;
  const language = useLanguageStatus();
  const [testPreview, setTestPreview] = useState<{ info: HostedPreview; file: File }>();
  const [removeTest, setRemoveTest] = useState<string>();
  const [selected, select] = useState(initialId),
    [candidate, preview] = useState<{ pack: EditorPack; previous?: string }>();
  const [remove, confirmRemove] = useState<EditorPack>(),
    [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(''),
    [error, setError] = useState('');
  const current = packs.data?.find((p) => p.id === selected);
  const [groupRemoval, setGroupRemoval] =
    useState<Pick<EditorPack, 'id' | 'name' | 'version' | 'digest'>[]>();
  async function operate(action: () => Promise<void>) {
    setError('');
    setMessage('');
    setBusy(true);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : '编辑器扩展操作失败。');
    } finally {
      setBusy(false);
    }
  }
  // 文件来自本机选择器。记录此刻的旧包指纹，用户确认替换时由存储层再查一次。
  function inspect(file: File) {
    preview(undefined);
    setTestPreview(undefined);
    setRemoveTest(undefined);
    confirmRemove(undefined);
    return operate(async () => {
      const { readEditorVsix } = await import('./readEditorVsix');
      const pack = await readEditorVsix(file);
      if ('kind' in pack) {
        setTestPreview({ info: pack, file });
        return;
      }
      const existing = packs.data?.find((p) => p.id === pack.id);
      if (existing?.digest === pack.digest) {
        select(pack.id);
        setMessage('已安装相同的扩展包。');
        return;
      }
      preview({ pack, previous: existing?.digest });
    });
  }
  return (
    <div className="editor-pack-overlay">
      <Dialog label="代码工作间的子插件" className="editor-pack-dialog" onClose={onClose}>
        <header>
          <h2>代码工作间的子插件</h2>
          <button type="button" aria-label="关闭子插件管理" onClick={onClose}>
            ×
          </button>
        </header>
        <p>选择原版 VSIX，安装编辑器片段、测试或格式化子插件。</p>
        {language.data?.map(plugin=><LanguagePluginRow key={plugin.id} status={language} plugin={plugin}/>)}
        {language.isError&&<p role="alert">无法读取语言插件。<button onClick={()=>void language.refetch()}>重试</button></p>}
        <input
          ref={input}
          type="file"
          accept=".vsix"
          aria-label="选择 VSIX 扩展包"
          hidden
          onChange={(e) => {
            const file = e.target.files?.[0];
            e.target.value = '';
            if (file) void inspect(file);
          }}
        />
        <button
          type="button"
          disabled={busy || !packs.isSuccess || !!groupRemoval}
          onClick={() => input.current?.click()}
        >
          选择 .vsix 文件
        </button>
        {packs.isError && (
          <p role="alert">
            无法读取已安装的子插件。<button onClick={() => void packs.refetch()}>重试</button>
          </p>
        )}
        {candidate && (
          <section className="editor-pack-preview">
            <strong>{candidate.pack.name}</strong>
            <p>
              {candidate.pack.id} · v{candidate.pack.version} · {candidate.pack.snippets.length} 个可用片段
            </p>
            {candidate.pack.skipped > 0 && (
              <p>{candidate.pack.skipped} 个片段使用了当前编辑器未支持的写法，安装时会跳过。</p>
            )}
            <p>
              扩展声明的 VS Code 版本范围为 <code>{candidate.pack.engine}</code>。此包只提供编辑器片段。
            </p>
            <button
              disabled={busy}
              onClick={() =>
                void operate(async () => {
                  await changeEditorPack(candidate.pack.id, candidate.previous, { install: candidate.pack });
                  select(candidate.pack.id);
                  preview(undefined);
                  setMessage('已安装。');
                })
              }
            >
              {candidate.previous ? '替换已安装版本' : '安装子插件'}
            </button>
            <button disabled={busy} onClick={() => preview(undefined)}>
              取消
            </button>
          </section>
        )}
        {testPreview && (
          <section className="editor-pack-preview">
            <strong>{testPreview.info.name}</strong>
            <p>
              {testPreview.info.extensionId} · v{testPreview.info.version}
            </p>
            <p>{testPreview.info.kind === 'formatting' ? '格式化扩展安装后默认停用。启用后可从编辑器或会话预览当前草稿的差异。' : '测试扩展安装后默认停用。启用后 Agent 可以申请在已授权目录运行测试，并读取失败断言。'}</p>
            <button
              disabled={busy}
              onClick={() =>
                void operate(async () => {
                  const child = await installHosted(workroomApp, testPreview.file, testPreview.info);
                  await refresh();
                  select(child.packageId);
                  setTestPreview(undefined);
                  setMessage(slotLabel(child.slot)+'子插件已安装。');
                })
              }
            >
              安装{testPreview.info.kind === 'formatting' ? '格式化' : '测试'}子插件
            </button>
            <button disabled={busy} onClick={() => setTestPreview(undefined)}>
              取消
            </button>
          </section>
        )}
        {hosted.isLoading && <p role="status">正在读取工作间槽位…</p>}
        {hosted.isError && (
          <p role="alert">
            工作间槽位读取失败。
            <button type="button" disabled={hosted.isFetching} onClick={() => void hosted.refetch()}>
              {hosted.isFetching ? '正在重试…' : '重试读取槽位'}
            </button>
          </p>
        )}
        {!groupRemoval &&
          listing?.children.map((child) => (
            <section className="editor-pack-detail" key={child.packageId}>
              <button
                aria-pressed={selected === child.packageId}
                onClick={() => {
                  select(child.packageId);
                  setRemoveTest(undefined);
                }}
              >
                {child.name}
              </button>
              <small>
                {' '}
                v{child.version} · {slotLabel(child.slot)} · {hostedChildStatus(child, listing.slots[child.slot]?.available === true)}
              </small>
              {selected === child.packageId && (
                <div>
                  <p>
                    {child.extensionId} · Agent 工具 <code>{child.slot === formattingSlot ? 'current_document、preview_format、read_format_preview' : 'run_tests、read_test_run'}</code>
                  </p>
                  <p>
                    扩展以当前系统用户身份运行。Agent 每次执行都需批准，宿主在实际调用时启动，完成后退出。
                  </p>
                  {!listing.slots[child.slot]?.available && (
                    <p role="status">
                      请先准备可选的 Theia 扩展宿主，安装步骤见仓库中的 docs/application-testing.md。
                    </p>
                  )}
                  {!(child.enabled && child.canRun) && (
                    <button
                      disabled={busy || !listing.slots[child.slot]?.available}
                      onClick={() =>
                        void operate(async () => {
                          await changeHosted(child, 'enable');
                          await refresh();
                          setMessage('已启用，下一轮会话可以发现'+slotLabel(child.slot)+'工具。');
                        })
                      }
                    >
                      {child.slot === formattingSlot ? '启用格式化工具' : '启用 Agent 测试工具'}
                    </button>
                  )}
                  {child.enabled && (
                    <button
                      disabled={busy}
                      onClick={() =>
                        void operate(async () => {
                          await changeHosted(child, 'disable');
                          await refresh();
                          setMessage(slotLabel(child.slot)+'工具已撤下。');
                        })
                      }
                    >
                      停用{slotLabel(child.slot)}工具
                    </button>
                  )}
                  <button disabled={busy} onClick={() => setRemoveTest(child.packageId)}>
                    卸载{slotLabel(child.slot)}子插件…
                  </button>
                  {removeTest === child.packageId && (
                    <>
                      <button
                        disabled={busy}
                        onClick={() =>
                          void operate(async () => {
                            await changeHosted(child, 'remove');
                            await refresh();
                            setRemoveTest(undefined);
                            select(undefined);
                            setMessage(slotLabel(child.slot)+'子插件已卸载。');
                          })
                        }
                      >
                        确认卸载{slotLabel(child.slot)}子插件
                      </button>
                      <button onClick={() => setRemoveTest(undefined)}>取消卸载</button>
                    </>
                  )}
                </div>
              )}
            </section>
          ))}
        <ul aria-label="已安装的工作间子插件">
          {packs.data?.map((pack) => (
            <li key={pack.id}>
              <button
                disabled={busy || !!groupRemoval}
                aria-pressed={selected === pack.id}
                onClick={() => {
                  select(pack.id);
                  confirmRemove(undefined);
                }}
              >
                {pack.name}
              </button>
              <small>
                v{pack.version} · {pack.enabled ? '已启用' : '已停用'}
              </small>
            </li>
          ))}
        </ul>
        {(packs.data?.length ?? 0) > 1 && !groupRemoval && (
          <button
            disabled={busy}
            onClick={() => {
              preview(undefined);
              setTestPreview(undefined);
              setRemoveTest(undefined);
              confirmRemove(undefined);
              setGroupRemoval(
                packs.data?.map(({ id, name, version, digest }) => ({ id, name, version, digest })),
              );
            }}
          >
            {hosted.data?.children.length || language.data?.some(p=>p.installed) ? '卸载这组片段子插件…' : '卸载这组子插件…'}
          </button>
        )}
        {groupRemoval && (
          <section className="editor-pack-preview">
            <strong>从代码工作间卸载以下 {groupRemoval.length} 个子插件？</strong>
            <ul>
              {groupRemoval.map((p) => (
                <li key={p.id}>
                  {p.name} · v{p.version}
                </li>
              ))}
            </ul>
            <button
              disabled={busy}
              onClick={() =>
                void operate(async () => {
                  try {
                    await removeEditorPacks(groupRemoval);
                    select(undefined);
                    setMessage('已卸载所列子插件。');
                  } finally {
                    setGroupRemoval(undefined);
                  }
                })
              }
            >
              确认卸载所列子插件
            </button>
            <button disabled={busy} onClick={() => setGroupRemoval(undefined)}>
              取消批量卸载
            </button>
          </section>
        )}
        {current && !groupRemoval && (
          <section className="editor-pack-detail">
            <h3>{current.name}</h3>
            <p>
              {current.id} · {current.snippets.length} 个片段
            </p>
            <p>此子插件提供编辑器片段，没有 Agent 工具。选中插入的代码，可以从工作间交给会话。</p>
            <button
              disabled={busy}
              onClick={() =>
                void operate(async () => {
                  await changeEditorPack(current.id, current.digest, { enabled: !current.enabled });
                  setMessage(current.enabled ? '已停用。' : '已启用。');
                })
              }
            >
              {current.enabled ? '停用' : '启用'}
            </button>
            <button disabled={busy} onClick={() => confirmRemove(current)}>
              卸载
            </button>
            {remove?.id === current.id && (
              <div>
                <p>卸载 {remove.name}？已经插入文件的内容会保留。</p>
                <button
                  disabled={busy}
                  onClick={() =>
                    void operate(async () => {
                      await changeEditorPack(remove.id, remove.digest, { remove: true });
                      select(undefined);
                      confirmRemove(undefined);
                      setMessage('已卸载。');
                    })
                  }
                >
                  确认卸载
                </button>
                <button disabled={busy} onClick={() => confirmRemove(undefined)}>
                  取消卸载
                </button>
              </div>
            )}
          </section>
        )}
        {busy && <p role="status">处理中…</p>}
        {message && <p role="status">{message}</p>}
        {error && <p role="alert">{error}</p>}
      </Dialog>
    </div>
  );
}
