// 插件管理目录的一张卡对应一个应用。下排只显示这个应用确实拥有的安装记录。
import { useState } from 'react';
import { useEditorPacks } from './editorExtensionStore';
import { EditorExtensionManager } from './EditorExtensionManager';
import {languages,useLanguageStatus} from './workroomLanguage';
import { PackageDisplayIcon } from './ApplicationIcon';
import type { InstalledApplication } from './pluginApplications';
import { UiIcon } from './UiIcon';
import { hostedChildStatus, slotLabel, useAppChildren, workroomApp } from './applicationTesting';
import './editor-extensions.css';

// PluginManagementShell 提供宿主登记的应用及导航动作。代码工作间从自己的安装记录取得子项。
export function ApplicationUnits({
  applications,
  onWorkroom,
  onOpen,
  onManage,
  search = '',
  theme = 'default',
}: {
  applications: InstalledApplication[];
  onWorkroom: () => void;
  onOpen: (id: string) => void;
  onManage: (packageId: string) => void;
  search?: string;
  theme?: string;
}) {
  const packs = useEditorPacks(),
    [manager, setManager] = useState<{ id?: string }>();
  const hosted = useAppChildren(workroomApp);
  const language = useLanguageStatus();
  const listing = hosted.isError ? undefined : hosted.data;
  const query = search.trim().toLocaleLowerCase();
  const showCode =
    !query ||
    (
      '代码工作间 编辑器 VS Code VSIX TypeScript JavaScript C/C++ clangd Rust rust-analyzer 语言插件 ' +
      (packs.data ?? []).map((p) => p.name + ' ' + p.id).join(' ') +
      ' ' +
      (hosted.data?.children ?? []).map((p) => p.name + ' ' + p.extensionId).join(' ')
    )
      .toLocaleLowerCase()
      .includes(query);
  const others = applications.filter((a) => (a.name + ' ' + a.packageId).toLocaleLowerCase().includes(query));
  if (!showCode && !others.length) return null;
  return (
    <section aria-label="应用与子插件">
      <h2>应用与子插件</h2>
      <div className="workroom-unit-grid">
        {showCode && (
          <article className="workroom-unit" aria-label="代码工作间应用">
            <button className="workroom-unit-owner" onClick={onWorkroom}>
              <span className="workroom-unit-icon">
                <UiIcon name="code" />
              </span>
              <span>
                <strong>代码工作间</strong>
                <small>预设应用</small>
              </span>
            </button>
            <div className="workroom-unit-children" aria-label="代码工作间的子插件">
              {language.data?.filter(p=>p.installed).map(p=><button key={p.id} className="workroom-child" data-child-id={'modot.'+p.id}
                data-disabled={!p.enabled||undefined} onClick={()=>setManager({})}>
                <UiIcon name="code"/><span>{languages[p.id].short}</span><small>{p.enabled?'已启用':'已停用'}</small>
              </button>)}
              {packs.data?.map((p) => (
                <button
                  key={p.id}
                  data-child-id={p.id}
                  className="workroom-child"
                  data-disabled={!p.enabled || undefined}
                  title={p.name + ' · ' + (p.enabled ? '已启用' : '已停用')}
                  onClick={() => setManager({ id: p.id })}
                >
                  <PackageDisplayIcon
                    packageId={'workroom-editor:' + p.id}
                    defaultImageDataUrl={p.icon}
                    kind="extension"
                  />
                  <span>{p.name}</span>
                  {!p.enabled && <small>已停用</small>}
                </button>
              ))}
              {listing?.children.map((child) => {
                const status = hostedChildStatus(child, listing.slots[child.slot]?.available === true);
                return (
                  <button
                    key={child.packageId}
                    data-child-id={child.extensionId}
                    className="workroom-child"
                    data-disabled={!child.enabled || undefined}
                    title={child.name + ' · ' + slotLabel(child.slot) + ' · ' + status}
                    onClick={() => setManager({ id: child.packageId })}
                  >
                    <PackageDisplayIcon packageId={child.packageId} kind="tool" />
                    <span>{child.name}</span>
                    <small>{status}</small>
                  </button>
                );
              })}
              <button
                className="workroom-child workroom-child-add"
                aria-label="添加工作间子插件"
                onClick={() => setManager({})}
              >
                <UiIcon name="plus" />
                <span>添加</span>
              </button>
            </div>
            {packs.isError && <button onClick={() => void packs.refetch()}>重新读取子插件</button>}
            {hosted.isLoading && <small role="status">正在读取工作间槽位…</small>}
            {hosted.isError && (
              <div className="workroom-unit-feedback" role="alert">
                工作间槽位读取失败。
                <button type="button" disabled={hosted.isFetching} onClick={() => void hosted.refetch()}>
                  {hosted.isFetching ? '正在重试…' : '重试读取槽位'}
                </button>
              </div>
            )}
          </article>
        )}
        {others.map((app) => (
          <article className="workroom-unit" key={app.id} aria-label={app.name + '应用'}>
            <div className="workroom-unit-head">
              <button className="workroom-unit-owner" onClick={() => onOpen(app.id)}>
                <span className="workroom-unit-icon">
                  <PackageDisplayIcon
                    packageId={app.dataPageRevision === undefined ? app.packageId : undefined}
                    installation={app.dataPageRevision === undefined ? app.pkg : undefined}
                    theme={theme}
                    imageDataUrl={app.imageDataUrl}
                    defaultImageDataUrl={app.defaultImageDataUrl}
                    kind={app.icon}
                  />
                </span>
                <span>
                  <strong>{app.name}</strong>
                  <small>{app.packageId}</small>
                </span>
              </button>
              <button
                className="workroom-unit-manage"
                aria-label={'管理应用 ' + app.name}
                title="管理应用"
                onClick={() => onManage(app.packageId)}
              >
                <UiIcon name="more" />
              </button>
            </div>
            <small>暂无已登记的子插件</small>
          </article>
        ))}
      </div>
      {manager && <EditorExtensionManager initialId={manager.id} onClose={() => setManager(undefined)} />}
    </section>
  );
}
