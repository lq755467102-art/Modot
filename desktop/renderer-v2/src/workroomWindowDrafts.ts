// 按工作区与桌面窗口保存文件标签和未提交的编辑草稿。
// 主要引用方包括 `WorkroomPage.tsx`。
import { loadJSON, saveJSON } from './client';
import type { WorkroomFile } from './WorkroomFileTabs';

// One shell may restore the last desktop session. Other windows keep independent session storage.
let ownership: Promise<boolean> | undefined;
let reportedFull = false;
export function persistentDraftOwner() {
  return (ownership ??= new Promise<boolean>((resolve) => {
    if (sessionStorage.getItem('modot-secondary-workroom') === '1' || !navigator.locks) {
      resolve(false);
      return;
    }
    void navigator.locks
      .request('modot-workroom-draft-owner', { ifAvailable: true }, async (lock) => {
        if (!lock) sessionStorage.setItem('modot-secondary-workroom', '1');
        resolve(!!lock);
        if (lock)
          await new Promise<void>((done) =>
            window.addEventListener('pagehide', () => done(), { once: true }),
          );
      })
      .catch(() => resolve(false));
  }));
}
export function windowTabs(workspace: string, primary: boolean): WorkroomFile[] {
  const key = 'workroom-tabs:' + workspace;
  if (primary)
    return (
      loadJSON<WorkroomFile[] | null>(key, null) ??
      loadJSON<WorkroomFile[]>('tabs', []).filter((f) => f.workspaceId === workspace)
    );
  try {
    return JSON.parse(sessionStorage.getItem(key) || '[]');
  } catch {
    return [];
  }
}
export function persistWindowTabs(workspace: string, primary: boolean, tabs: WorkroomFile[]) {
  if (primary) {
    saveJSON('workroom-tabs:' + workspace, tabs);
    return;
  }
  try {
    sessionStorage.setItem('workroom-tabs:' + workspace, JSON.stringify(tabs));
  } catch {
    if (!reportedFull) {
      reportedFull = true;
      window.dispatchEvent(new Event('workbench-storage-error'));
    }
  }
}
