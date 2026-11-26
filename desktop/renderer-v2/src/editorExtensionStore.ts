// 代码工作间在当前桌面配置中拥有这些安装记录。IndexedDB 事务核对旧包指纹，防止覆盖另一窗口刚更新的包。
import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { EditorPack } from './editorSnippets';

const key = ['workroom-editor', 'snippet-packs'] as const;
const changed = new EventTarget();
let connection: Promise<IDBDatabase> | undefined;
let channel: BroadcastChannel | undefined;
// IndexedDB 在同一桌面配置的窗口间共享。广播只提示重读，不直接传播安装状态。
function broadcast() {
  if (!channel && typeof BroadcastChannel !== 'undefined') {
    channel = new BroadcastChannel('modot-workroom-editor-packs-v1');
    channel.onmessage = () => changed.dispatchEvent(new Event('change'));
  }
  return channel;
}
function database() {
  if (!connection) {
    connection = new Promise<IDBDatabase>((resolve, reject) => {
      const req = indexedDB.open('modot-workroom-editor', 1);
      req.onupgradeneeded = () => req.result.createObjectStore('packs', { keyPath: 'id' });
      req.onsuccess = () => {
        req.result.onversionchange = () => {
          req.result.close();
          connection = undefined;
        };
        resolve(req.result);
      };
      req.onerror = () => reject(req.error ?? Error('编辑器扩展存储不可用。'));
    }).catch((error) => {
      connection = undefined;
      throw error;
    });
  }
  return connection;
}
async function listPacks(): Promise<EditorPack[]> {
  const db = await database();
  return new Promise((resolve, reject) => {
    const req = db.transaction('packs').objectStore('packs').getAll();
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
// 应用卡片、管理面板和编辑器订阅同一份安装记录，停用后下次补全即采用最新状态。
export function useEditorPacks(enabled=true) {
  const client = useQueryClient();
  useEffect(() => {
    if(!enabled)return;
    broadcast();
    const refresh = () => {
      void client.invalidateQueries({ queryKey: key });
    };
    changed.addEventListener('change', refresh);
    return () => changed.removeEventListener('change', refresh);
  }, [client,enabled]);
  return useQuery({ queryKey: key, queryFn: listPacks, staleTime: 0, enabled });
}
type PackChange = { install: EditorPack } | { enabled: boolean } | { remove: true };
type PackEdit = { id: string; expected: string | undefined; change: PackChange };
// expected 来自用户看到的包。整组读写共用一个事务，安装、替换和卸载都能发现过期选择。
async function applyChanges(edits: readonly PackEdit[]) {
  const db = await database();
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction('packs', 'readwrite'),
      store = tx.objectStore('packs');
    let reason: Error | undefined;
    tx.oncomplete = () => resolve();
    tx.onabort = () => reject(reason ?? tx.error ?? Error('编辑器扩展操作未保存。'));
    for (const { id, expected, change } of edits) {
      const req = store.get(id);
      req.onsuccess = () => {
        const old: EditorPack | undefined = req.result;
        if ('install' in change && old?.digest === change.install.digest) return;
        if (old?.digest !== expected || (!old && !('install' in change))) {
          reason = Error('安装记录已变化，请重新选择。');
          tx.abort();
          return;
        }
        if ('install' in change) store.put({ ...change.install, enabled: old?.enabled ?? true });
        else if ('remove' in change) store.delete(id);
        else store.put({ ...old!, enabled: change.enabled });
      };
    }
  });
  changed.dispatchEvent(new Event('change'));
  broadcast()?.postMessage('changed');
}
export function changeEditorPack(id: string, expected: string | undefined, change: PackChange) {
  return applyChanges([{ id, expected, change }]);
}
// 用户确认的是名单快照。任何一项被替换都会回滚整组，确认后才出现的包也不会被移除。
export function removeEditorPacks(packs: readonly Pick<EditorPack, 'id' | 'digest'>[]) {
  return applyChanges(packs.map((p) => ({ id: p.id, expected: p.digest, change: { remove: true } })));
}
