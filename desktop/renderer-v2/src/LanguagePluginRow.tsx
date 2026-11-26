// 显示工作间语言插件的来源、健康状态和启停、卸载操作。
// 主要引用方包括 `EditorExtensionManager.tsx`。
import { useState } from 'react';
import {
  changeLanguage,
  languages,
  type LanguageStatus,
  type useLanguageStatus,
} from './workroomLanguage';

export function LanguagePluginRow({
  status,
  plugin: s,
}: {
  status: ReturnType<typeof useLanguageStatus>;
  plugin: LanguageStatus;
}) {
  const name = languages[s.id].name;
  const [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  if (!window.workbench?.language) return null;
  async function change(action: Parameters<typeof changeLanguage>[1]) {
    if (!s || busy) return;
    setBusy(true);
    setError('');
    try {
      await changeLanguage(s.id, action, s.revision);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      await status.refetch();
      setBusy(false);
    }
  }
  return (
    <section className="editor-pack-detail" data-language-plugin={s.id} aria-label={name + ' 语言插件'}>
      <strong>{name}</strong>
      <small> · {s?.installed ? (s.enabled ? '已启用' : '已停用') : s ? '可安装' : ''}</small>
      <p>
        {s.id === 'typescript'
          ? '在工作间补全项目函数、导入和参数签名。'
          : s.installed
            ? {
                system: '使用本机语言服务',
                managed: '语言服务由 Modot 管理',
                mixed: '复用本机工具，缺少的组件由 Modot 管理',
              }[s.source ?? 'managed'] +
              ' · ' +
              s.version
            : '优先使用本机 ' + { cpp: 'clangd', rust: 'rust-analyzer', python: 'Pyright' }[s.id] + '，缺少的组件按需下载。'}
      </p>
      {s &&
        (s.installed ? (
          <>
            <button disabled={busy} onClick={() => void change(s.enabled ? 'disable' : 'enable')}>
              {s.enabled ? '停用语言插件' : '启用语言插件'}
            </button>
            <button disabled={busy} onClick={() => void change('uninstall')}>
              卸载语言插件
            </button>
          </>
        ) : (
          <button disabled={busy} onClick={() => void change('install')}>
            安装并启用
          </button>
        ))}
      {busy && <small role="status">处理中…</small>}
      {(error || status.error) && (
        <p role="alert">
          {error || String(status.error)}{' '}
          <button disabled={busy} onClick={() => void status.refetch()}>
            刷新
          </button>
        </p>
      )}
    </section>
  );
}
