// EditorExtensionManager 在用户选包后才加载 ZIP 和 JSONC 解析器，只把已声明的片段读进编辑器。
import { BlobReader, ZipReader, type FileEntry } from '@zip.js/zip.js/lib/zip-core-custom.js';
import { parse, type ParseError } from 'jsonc-parser';
import { snippetTemplate, type EditorPack, type Snippet } from './editorSnippets';
import type { HostedPreview } from './applicationTesting';

const MiB = 1024 * 1024;
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === 'object' && !Array.isArray(value);
const identifier = (value: unknown): value is string =>
  typeof value === 'string' && /^[a-z0-9][a-z0-9_-]{0,127}$/i.test(value);
function strings(value: unknown): string[] {
  if (typeof value === 'string') return [value];
  return Array.isArray(value) && value.every((item) => typeof item === 'string') ? value : [];
}
// 清单路径相对于包内的 extension 目录，必须精确对应一项归档内容。
function asset(path: unknown) {
  if (typeof path !== 'string') throw Error('片段声明缺少包内路径。');
  const relative = path.replace(/^\.\//, '');
  if (
    !relative ||
    relative.length > 300 ||
    /[\\:\x00-\x1f]/.test(relative) ||
    relative.split('/').some((p) => !p || p === '.' || p === '..')
  )
    throw Error('片段路径必须位于扩展包内。');
  return 'extension/' + relative;
}
function json(text: string, path: string) {
  const errors: ParseError[] = [];
  const value: unknown = parse(text.replace(/^\uFEFF/, ''), errors, { allowTrailingComma: true });
  if (errors.length || !object(value)) throw Error('无法读取包内 JSON 文件 ' + path);
  return value;
}
// file 是用户选中的原包。返回供确认的预览，是否写入安装记录由管理面板决定。
async function archiveHash(file: Blob) {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer()));
  return Array.from(digest, (b) => b.toString(16).padStart(2, '0')).join('');
}
export async function readEditorVsix(file: Blob): Promise<EditorPack | HostedPreview> {
  if (!file.size || file.size > 16 * MiB) throw Error('代码片段 VSIX 须在 16 MiB 以内。');
  const zip = new ZipReader(new BlobReader(file), { useWebWorkers: false, useCompressionStream: true });
  try {
    const entries = new Map<string, FileEntry>();
    let count = 0,
      total = 0;
    for await (const entry of zip.getEntriesGenerator()) {
      if (++count > 4096) throw Error('扩展包中的文件过多。');
      if (entry.directory) continue;
      if (entries.has(entry.filename)) throw Error('扩展包有重名文件。');
      entries.set(entry.filename, entry);
    }
    // 归档声明的长度和实际解压字节都要受限，多个贡献共用本次导入的总额度。
    async function bytes(path: string, limit = MiB) {
      const entry = entries.get(path);
      if (
        !entry ||
        entry.encrypted ||
        entry.uncompressedSize > limit ||
        total + entry.uncompressedSize > 3 * MiB
      )
        throw Error('包内文件缺失、加密或超过片段大小限制：' + path);
      let size = 0;
      const result = new Uint8Array(entry.uncompressedSize);
      await entry.getData(
        new WritableStream<Uint8Array>({
          write(chunk) {
            if (chunk.byteLength > result.length - size) throw Error('片段解压大小超过声明。');
            result.set(chunk, size);
            size += chunk.byteLength;
          },
        }),
        { checkSignature: true },
      );
      if (size !== entry.uncompressedSize) throw Error('包内文件不完整：' + path);
      total += size;
      return result;
    }
    async function read(path: string) {
      return json(new TextDecoder('utf-8', { fatal: true }).decode(await bytes(path)), path);
    }
    // 图标可缺省。仅接收包内小型 PNG，并在交给浏览器解码前检查尺寸。
    async function icon(value: unknown) {
      if (typeof value !== 'string' || !value.toLowerCase().endsWith('.png')) return;
      try {
        const data = await bytes(asset(value), 64 * 1024),
          header = new DataView(data.buffer);
        if (
          data.length < 33 ||
          header.getUint32(0) !== 0x89504e47 ||
          header.getUint32(4) !== 0x0d0a1a0a ||
          header.getUint32(12) !== 0x49484452
        )
          return;
        const width = header.getUint32(16),
          height = header.getUint32(20);
        if (!width || !height || width > 512 || height > 512) return;
        let binary = '';
        for (const byte of data) binary += String.fromCharCode(byte);
        return 'data:image/png;base64,' + btoa(binary);
      } catch {
        return undefined;
      }
    }
    if (!entries.has('extension.vsixmanifest')) throw Error('请选择原始 .vsix 扩展包。');
    const manifest = await read('extension/package.json');
    if (
      !identifier(manifest.publisher) ||
      !identifier(manifest.name) ||
      typeof manifest.version !== 'string' ||
      !manifest.version ||
      manifest.version.length > 80 ||
      !object(manifest.engines) ||
      typeof manifest.engines.vscode !== 'string'
    )
      throw Error('VSIX 缺少可识别的发布者、名称、版本或 VS Code 版本声明。');
    if (
      typeof manifest.main === 'string' &&
      Array.isArray(manifest.categories) &&
      (manifest.categories.includes('Testing') || manifest.categories.includes('Formatters'))
    ) {
      const formatting = manifest.categories.includes('Formatters');
      const limit = formatting ? 8 : 2;
      if (file.size > limit * MiB) throw Error('此类扩展的原包上限为 '+limit+' MiB。');
      if ([manifest.extensionDependencies, manifest.extensionPack].some((v) => Array.isArray(v) && v.length))
        throw Error('宿主当前支持独立扩展，无法装载带扩展依赖的包。');
      return {
        kind: formatting ? 'formatting' : 'testing',
        name:
          typeof manifest.displayName === 'string' && !manifest.displayName.startsWith('%')
            ? manifest.displayName.slice(0, 120)
            : manifest.name,
        extensionId: (manifest.publisher + '.' + manifest.name).toLowerCase(),
        version: manifest.version,
        sha256: await archiveHash(file),
      };
    }
    if (
      manifest.main ||
      manifest.browser ||
      manifest.enabledApiProposals ||
      ['extensionDependencies', 'extensionPack', 'activationEvents'].some(
        (k) =>
          manifest[k] !== undefined && (!Array.isArray(manifest[k]) || (manifest[k] as unknown[]).length > 0),
      )
    )
      throw Error('此扩展需要完整的 VS Code 扩展宿主，轻量编辑器无法运行。');
    if (
      !object(manifest.contributes) ||
      Object.keys(manifest.contributes).some((key) => key !== 'snippets') ||
      !Array.isArray(manifest.contributes.snippets) ||
      !manifest.contributes.snippets.length
    )
      throw Error('轻量编辑器可安装仅提供代码片段的 VSIX。此扩展还需要其他宿主能力。');
    const sources = new Map<string, Set<string>>();
    for (const item of manifest.contributes.snippets) {
      if (!object(item) || (item.language !== undefined && !identifier(item.language)))
        throw Error('片段的语言声明无效。');
      const path = asset(item.path),
        languages = sources.get(path) ?? new Set<string>();
      languages.add(typeof item.language === 'string' ? item.language : '*');
      sources.set(path, languages);
    }
    const snippets: Snippet[] = [];
    let skipped = 0;
    for (const [path, declared] of sources) {
      const definitions = await read(path);
      for (const [name, item] of Object.entries(definitions)) {
        if (snippets.length + skipped >= 1200) throw Error('单包片段超过 1200 条。');
        if (!object(item)) {
          skipped++;
          continue;
        }
        const body = strings(item.body).join('\n');
        const prefixes = [...new Set(strings(item.prefix))].filter((p) => p.length > 0 && p.length <= 100);
        if (
          !prefixes.length ||
          prefixes.length > 16 ||
          name.length > 160 ||
          snippetTemplate(body) === undefined
        ) {
          skipped++;
          continue;
        }
        const local =
          typeof item.scope === 'string'
            ? item.scope
                .split(',')
                .map((s) => s.trim())
                .filter(Boolean)
            : [];
        const languages = local.length
          ? local.filter((l) => declared.has('*') || declared.has(l))
          : [...declared];
        if (!languages.length) {
          skipped++;
          continue;
        }
        snippets.push({
          name,
          prefixes,
          body,
          languages,
          description: strings(item.description).join(' ').slice(0, 400),
        });
      }
    }
    if (!snippets.length) throw Error('包内没有轻量编辑器可使用的片段。');
    const id = (manifest.publisher + '.' + manifest.name).toLowerCase();
    return {
      id,
      name:
        typeof manifest.displayName === 'string' && !manifest.displayName.startsWith('%')
          ? manifest.displayName.slice(0, 120)
          : manifest.name,
      version: manifest.version,
      engine: manifest.engines.vscode.slice(0, 120),
      digest: await archiveHash(file),
      enabled: true,
      snippets,
      skipped,
      icon: await icon(manifest.icon),
    };
  } finally {
    await zip.close();
  }
}
