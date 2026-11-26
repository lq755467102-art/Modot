// Code-workroom cards and their manager share this host registry. Agent grants live on the backend.
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from './client';

export const workroomApp = 'code-workroom';
export const testingSlot = 'vscode.testing/1';
export const formattingSlot = 'vscode.formatting/1';
export const slotLabel = (slot: string) => slot === formattingSlot ? '格式化' : '测试';
export type HostedPreview = {
  kind: 'testing' | 'formatting';
  name: string;
  extensionId: string;
  version: string;
  sha256: string;
};
export type HostedChild = Omit<HostedPreview, 'kind'> & {
  packageId: string;
  parentId: string;
  slot: string;
  revision: number;
  enabled: boolean;
  canRun: boolean;
};

export function hostedChildStatus(child: HostedChild, hostReady: boolean) {
  if (!child.enabled) return '已停用';
  if (!hostReady) return '宿主未就绪';
  return child.canRun ? '可运行' : '运行权限未启用';
}

type Listing = { children: HostedChild[]; slots: Record<string, { available: boolean }> };
const key = (parent: string) => ['application-children', parent] as const;

export function useAppChildren(parent: string) {
  return useQuery({
    queryKey: key(parent),
    queryFn: () => api<Listing>('/api/v2/plugins/applications/' + parent + '/children'),
    staleTime: 0,
    refetchOnWindowFocus: true,
  });
}
export function useRefreshChildren(parent: string) {
  const cache = useQueryClient();
  return async () => {
    await Promise.all([
      cache.invalidateQueries({ queryKey: key(parent) }),
      cache.invalidateQueries({ queryKey: ['plugins'] }),
    ]);
  };
}
// The IPC request carries the unchanged original archive one time, after the user confirms the preview.
function encode(blob: Blob) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(Error('无法读取所选 VSIX。'));
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.readAsDataURL(blob);
  });
}
export async function installHosted(parent: string, file: File, info: HostedPreview) {
  const slot = info.kind === 'formatting' ? formattingSlot : testingSlot;
  const {sha256} = info;
  const base = '/api/v2/plugins/applications/' + parent;
  const chunk = 1024 * 1024;
  if (file.size > 2 * chunk) {
    const {upload} = await api<{upload:string}>(base + '/uploads', {slot, size:file.size, sha256}, true);
    for (let offset = 0; offset < file.size; offset += chunk) {
      await api(base + '/uploads', {slot, upload, index:offset / chunk, chunk:await encode(file.slice(offset, offset+chunk))}, true);
    }
    return api<HostedChild>(base + '/children', {slot, upload, sha256}, true);
  }
  const archive = await encode(file);
  return api<HostedChild>(
    '/api/v2/plugins/applications/' + parent + '/children',
    { slot, archive, sha256 },
    true,
  );
}
export function changeHosted(child: HostedChild, action: 'enable' | 'disable' | 'remove') {
  const fields =
    action === 'remove'
      ? { remove: true }
      : { remoteEnabled: action === 'enable', writeEnabled: action === 'enable' };
  return api(
    '/api/v2/plugins/' + child.packageId,
    { expectedRevision: child.revision, confirm: true, ...fields },
    true,
  );
}
