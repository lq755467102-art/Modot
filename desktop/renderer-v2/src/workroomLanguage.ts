// 同步语言插件状态，并将当前检出目录和编辑器草稿绑定到语义查询。
// 主要引用方包括 `WorkroomPage.tsx`、`EditorView.tsx` 和 `LanguagePluginRow.tsx`。
import { useEffect, useMemo } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { WorkroomContext, WorkroomOpened } from './generated';
import type { WorkroomFile } from './WorkroomFileTabs';

export const languages = {
  typescript: { name: 'TypeScript / JavaScript', short: 'TS / JS' },
  cpp: { name: 'C / C++', short: 'C / C++' },
  rust: { name: 'Rust', short: 'Rust' },
  python: { name: 'Python', short: 'Python' },
} as const;
export type LanguageId = keyof typeof languages;
export type LanguageStatus = {
  id: LanguageId;
  version: string;
  revision: number;
  installed: boolean;
  enabled: boolean;
  source: 'system' | 'managed' | 'mixed' | null;
};
const statusKey = ['workroom-languages'] as const;
const bridge = () => window.workbench?.language;
export function languageFor(path: string): LanguageId | undefined {
  if (/\.[cm]?[jt]sx?$/i.test(path)) return 'typescript';
  if (/\.(?:c|cc|cp|cpp|cxx|c\+\+|h|hh|hpp|hxx|ipp|inl|ixx|cppm)$/i.test(path)) return 'cpp';
  if (/\.rs$/i.test(path)) return 'rust';
  if (/\.pyi?$/i.test(path)) return 'python';
}
export function useLanguageStatus() {
  const client = useQueryClient();
  useEffect(
    () =>
      window.workbench?.onLanguageChanged?.((state) => {
        client.setQueryData<LanguageStatus[]>(statusKey, (previous) =>
          previous?.map((item) =>
            item.id === state.id && state.revision > item.revision ? state : item,
          ),
        );
      }),
    [client],
  );
  return useQuery({
    queryKey: statusKey,
    enabled: !!bridge(),
    queryFn: async () =>
      ((await bridge()({ op: 'catalog' })) as { languages: LanguageStatus[] }).languages,
  });
}
export async function changeLanguage(
  language: LanguageId,
  action: 'install' | 'enable' | 'disable' | 'uninstall',
  revision: number,
) {
  return (await bridge()({ op: 'change', language, action, revision })) as LanguageStatus;
}
export type LanguageSession = {
  active: () => boolean;
  query: <T>(
    kind: string,
    file: string,
    text: string,
    pos: number,
    more?: Record<string, unknown>,
  ) => Promise<T | null>;
};
export function useLanguageSession(
  binding: WorkroomOpened,
  context: WorkroomContext,
  language: LanguageId | undefined,
  tabs: { current: WorkroomFile[] },
) {
  const status = useLanguageStatus();
  const plugin = status.data?.find((s) => s.id === language);
  const scope = JSON.stringify({
    workspaceId: binding.workspaceId,
    resourceId: binding.resourceId,
    scopeRevision: binding.scopeRevision,
    ...(context.conversationId
      ? { conversationId: context.conversationId }
      : { projectId: binding.projectId }),
    revision: plugin?.revision,
  });
  const session = useMemo(() => {
    if (!language || !plugin?.enabled) return;
    let token = crypto.randomUUID(),
      opened: Promise<unknown> | undefined,
      active = true;
    return {
      active: () => active,
      mount() {
        active = true;
      },
      close() {
        active = false;
        if (opened) void bridge()({ op: 'release', token }).catch(() => {});
        opened = undefined;
        token = crypto.randomUUID();
      },
      async query<T>(
        kind: string,
        file: string,
        text: string,
        pos: number,
        more?: Record<string, unknown>,
      ): Promise<T | null> {
        if (!active) return null;
        opened ??= bridge()({ op: 'open', language, scope: JSON.parse(scope), token });
        const requestToken = token;
        await opened;
        if (!active || token !== requestToken) return null;
        const snapshots = () =>
          tabs.current.flatMap((t) =>
            !t.readOnly &&
            t.workspaceId === binding.workspaceId &&
            t.path !== file &&
            (languageFor(t.path) === language ||
              (language === 'typescript' && /\.json$/i.test(t.path))) &&
            t.draft !== t.text
              ? [{ path: t.path, text: t.draft.replace(/\r\n?/g, '\n') }]
              : [],
          );
        const others = snapshots();
        const value = await bridge()({
          op: 'query',
          token,
          query: { kind, file, pos, documents: [...others, { path: file, text }], ...more },
        });
        if (!active || token !== requestToken) return null;
        const latest = snapshots();
        return latest.length === others.length &&
          latest.every((d, i) => d.path === others[i].path && d.text === others[i].text)
          ? (value as T)
          : null;
      },
    };
  }, [language, plugin?.enabled, scope, binding.workspaceId, tabs]);
  useEffect(() => {
    session?.mount();
    return () => session?.close();
  }, [session]);
  return session;
}
