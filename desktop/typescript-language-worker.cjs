// Only the pinned TypeScript compiler API runs here. Project code and tsconfig plugins are never executed.
// 主要引用方包括 `workroom-language.cjs`。
const { parentPort, workerData } = require('node:worker_threads');
const path = require('node:path');
const fs = require('node:fs');
const { isBuiltin } = require('node:module');
const { root, library } = workerData;
const ts = require(path.join(library, 'typescript.js'));
const declarationRoots = [root, library];
const registry = ts.createDocumentRegistry(ts.sys.useCaseSensitiveFileNames, root);
const canonical = (s) =>
  ts.sys.useCaseSensitiveFileNames ? path.normalize(s) : path.normalize(s).toLowerCase();
const within = (base, file) => {
  const rel = path.relative(base, file);
  return !path.isAbsolute(rel) && rel !== '..' && !rel.startsWith('..' + path.sep);
};
const readable = (base, file) =>
  within(base, file) &&
  !path
    .relative(base, file)
    .split(path.sep)
    .some((part) => ['.git', '.harness'].includes(part.toLowerCase()));
const stamp = (file) => {
  try {
    const s = fs.statSync(file, { bigint: true });
    return [s.mtimeNs, s.ctimeNs, s.size].join(':');
  } catch {
    return '';
  }
};
function allowed(file) {
  const full = path.resolve(file);
  const base = declarationRoots.find((dir) => within(dir, full));
  if (!base || !readable(base, full)) return false;
  let target = full;
  while (within(base, target)) {
    try {
      return readable(base, fs.realpathSync.native(target));
    } catch (e) {
      if (e.code !== 'ENOENT' && e.code !== 'ENOTDIR') return false;
    }
    if (target === base) break;
    target = path.dirname(target);
  }
  return false;
}
let overlays = new Map(),
  generation = 0,
  config,
  typeScope,
  service,
  nodeService,
  nodeFiles,
  bundledTypes,
  projectNode,
  settings,
  inputFiles,
  observation = new Map();
const observedVersion = (file) => overlays.get(canonical(file))?.version ?? stamp(file);
const read = (file) => {
  if (!allowed(file)) return undefined;
  if (path.basename(file) === 'package.json') observation.set(file, observedVersion(file));
  return overlays.get(canonical(file))?.text ?? ts.sys.readFile(file);
};
const fileExists = (file) => allowed(file) && (overlays.has(canonical(file)) || ts.sys.fileExists(file));
function entries(dir) {
  const files = [],
    directories = [];
  if (allowed(dir)) {
    observation.set(dir, stamp(dir));
    try {
      for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, e.name);
        if (e.name === '.git' || e.name === '.harness' || (e.isSymbolicLink() && !allowed(full)))
          continue;
        let s = e;
        if (e.isSymbolicLink()) {
          try {
            s = fs.statSync(full);
          } catch {
            continue;
          }
        }
        if (s.isDirectory()) directories.push(e.name);
        else if (s.isFile()) files.push(e.name);
      }
    } catch (e) {
      if (!['ENOENT', 'ENOTDIR', 'EPERM', 'EACCES'].includes(e.code)) throw e;
    }
  }
  return { files, directories };
}
const realpath = (f) => (allowed(f) ? (ts.sys.realpath?.(f) ?? f) : f);
const directoryExists = (d) => allowed(d) && ts.sys.directoryExists(d);
const readDirectory = (dir, extensions, excludes, includes, depth) =>
  ts.matchFiles(
    dir,
    extensions,
    excludes,
    includes,
    ts.sys.useCaseSensitiveFileNames,
    root,
    depth,
    entries,
    realpath,
  );
const host = {
  getCurrentDirectory: () => root,
  useCaseSensitiveFileNames: () => ts.sys.useCaseSensitiveFileNames,
  fileExists,
  readFile: read,
  directoryExists,
  readDirectory,
  realpath,
  getDirectories: (d) => entries(d).directories,
  getDefaultLibFileName: (o) => path.join(library, ts.getDefaultLibFileName(o)),
  getCompilationSettings: () => settings,
  getScriptFileNames: () => [
    ...new Set([...inputFiles, ...[...overlays.values()].map((d) => d.path).filter((f) => /\.[cm]?[jt]sx?$/i.test(f))]),
  ],
  getProjectVersion: () => String(generation),
  getScriptVersion: (f) => overlays.get(canonical(f))?.version ?? (allowed(f) ? stamp(f) : ''),
  getScriptSnapshot: (f) => {
    const s = read(f);
    return s === undefined ? undefined : ts.ScriptSnapshot.fromString(s);
  },
};
function configure(file) {
  let next = '';
  for (let d = path.dirname(file); within(root, d); d = path.dirname(d)) {
    next = ['tsconfig.json', 'jsconfig.json'].map((n) => path.join(d, n)).find(fileExists) || '';
    if (next || d === root) break;
  }
  const scope = path.dirname(next || file);
  if (service && next === config && scope === typeScope &&
      [...observation].every(([f, s]) => s === observedVersion(f))) return;
  config = next;
  typeScope = scope;
  observation = new Map();
  const defaults = {
    allowJs: true,
    checkJs: false,
    noEmit: true,
    target: ts.ScriptTarget.ESNext,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    jsx: ts.JsxEmit.ReactJSX,
  };
  const configHost = {
    ...host,
    useCaseSensitiveFileNames: ts.sys.useCaseSensitiveFileNames,
    readFile: (f) => {
      if (!allowed(f)) return undefined;
      observation.set(f, observedVersion(f));
      return read(f);
    },
  };
  const definition = next
    ? ts.readConfigFile(next, configHost.readFile)
    : { config: { include: ['**/*'], exclude: ['node_modules', 'dist', 'build', '.git'] } };
  if (next && definition.error) throw Error('无法读取当前工程的 ' + path.basename(next));
  const parsed = ts.parseJsonConfigFileContent(
    definition.config || {},
    configHost,
    next ? path.dirname(next) : root,
    undefined,
    next || undefined,
  );
  settings = { ...defaults, ...parsed.options, allowJs: true, noEmit: true, plugins: [] };
  // An inferred project may be a checkout nested below the workroom root.
  settings.typeRoots = (settings.typeRoots ?? ts.getEffectiveTypeRoots(
    { configFilePath: (next || file).split(path.sep).join('/') }, host,
  )).filter(dir => within(root, path.resolve(dir)));
  for (const dir of settings.typeRoots) {
    const pkg = path.join(dir, 'node');
    if (allowed(pkg)) observation.set(pkg, observedVersion(pkg));
  }
  projectNode = ts.resolveTypeReferenceDirective('node', file, settings, host).resolvedTypeReferenceDirective;
  inputFiles = parsed.fileNames.filter((f) => allowed(f) && within(root, f));
  service?.dispose();
  nodeService?.dispose();
  nodeService = undefined;
  service = ts.createLanguageService(host, registry);
}

function bundledNodeTypes() {
  if (bundledTypes) return bundledTypes;
  const assets = path.join(__dirname, 'workroom-typescript-libs');
  const staged = fs.existsSync(assets);
  const dependencies = path.join(__dirname, 'node_modules');
  const node = fs.realpathSync.native(staged ? path.join(assets, 'node') : path.join(dependencies, '@types', 'node'));
  const undici = fs.realpathSync.native(path.join(staged ? assets : dependencies, 'undici-types'));
  declarationRoots.push(node, undici);
  bundledTypes = { node: path.join(node, 'index.d.ts'), undici: path.join(undici, 'index.d.ts') };
  return bundledTypes;
}

function quickInfo(language, file, pos) {
  const info = language.getQuickInfoAtPosition(file, pos);
  if (!info || info.documentation?.length) return info;
  // For callable variables the selected overload may have no comment; the exported
  // symbol itself can still carry JSDoc (for example fs.statSync).
  const program = language.getProgram();
  const source = program?.getSourceFile(file);
  if (!source) return info;
  const checker = program.getTypeChecker();
  const token = ts.getTokenAtPosition(source, pos);
  let symbol = checker.getSymbolAtLocation(token);
  if (symbol && (symbol.flags & ts.SymbolFlags.Alias)) symbol = checker.getAliasedSymbol(symbol);
  return {
    ...info,
    documentation: symbol?.getDocumentationComment(checker),
    unresolved: !symbol?.declarations?.length && !!(checker.getTypeAtLocation(token).flags & ts.TypeFlags.Any),
  };
}

function importsNode(program, file) {
  const pending = [program.getSourceFile(file)];
  const seen = new Set();
  while (pending.length) {
    const source = pending.pop();
    if (!source || seen.has(source)) continue;
    seen.add(source);
    for (const ref of source.imports || []) {
      if (isBuiltin(ref.text)) return true;
      const target = program.getResolvedModuleFromModuleSpecifier(ref, source)?.resolvedModule;
      if (target && !target.isExternalLibraryImport && within(root, target.resolvedFileName))
        pending.push(program.getSourceFile(target.resolvedFileName));
    }
  }
  return false;
}

function symbolInfo(file, pos) {
  const info = quickInfo(service, file, pos);
  if (info && (!info.unresolved || info.documentation?.length)) return info;
  const program = service.getProgram();
  if (projectNode && program.getSourceFile(projectNode.resolvedFileName)) return info;
  if (!importsNode(program, file)) return info;

  // Keep bundled ambient declarations out of the project's program. They must never merge
  // with a project's overloads or introduce Node globals into ordinary editor results.
  const bundled = projectNode ? undefined : bundledNodeTypes();
  nodeFiles = [file, projectNode?.resolvedFileName ?? bundled.node];
  if (!nodeService) {
    const options = bundled
      ? { ...settings, paths: { ...settings.paths, 'undici-types': [bundled.undici] } }
      : settings;
    nodeService = ts.createLanguageService({
      ...host,
      getCompilationSettings: () => options,
      getScriptFileNames: () => nodeFiles,
    }, registry);
  }
  return quickInfo(nodeService, file, pos) ?? info;
}
const supported = /\.(?:[cm]?[tj]sx?|json)$/i;
function filename(name) {
  if (
    typeof name !== 'string' ||
    !name ||
    path.isAbsolute(name) ||
    /[:\0]/.test(name) ||
    name.split(/[\\/]/).includes('..') ||
    !supported.test(name)
  )
    throw Error('无效的工程文件');
  const f = path.resolve(root, name);
  if (!within(root, f) || !allowed(f)) throw Error('文件已离开当前工程');
  return f;
}
function query(q) {
  const file = filename(q.file);
  if (!Array.isArray(q.documents)) throw Error('缺少当前编辑内容');
  const next = new Map();
  for (const d of q.documents) {
    const f = filename(d.path),
      k = canonical(f),
      old = overlays.get(k);
    if (typeof d.text !== 'string' || next.has(k)) throw Error('无效的编辑内容');
    next.set(k, {
      path: f,
      text: d.text,
      version: old?.text === d.text ? old.version : 'draft:' + String(generation + 1),
    });
  }
  overlays = next;
  generation++;
  const current = overlays.get(canonical(file));
  if (!current || !Number.isInteger(q.pos) || q.pos < 0 || q.pos > current.text.length)
    throw Error('光标或草稿已变化');
  configure(file);
  if (q.kind === 'hover') {
    const info = symbolInfo(file, q.pos);
    if (!info) return null;
    const { start, length } = info.textSpan;
    return {
      from: start, to: start + length,
      blocks: [
        { code: true, text: ts.displayPartsToString(info.displayParts) },
        { code: false, text: ts.displayPartsToString(info.documentation) },
      ].filter(block => block.text),
    };
  }
  const preferences = {
    includeCompletionsForModuleExports: true,
    includeCompletionsWithInsertText: false,
    importModuleSpecifierPreference: 'relative',
  };
  if (q.kind === 'signature') {
    const help = service.getSignatureHelpItems(file, q.pos, { triggerReason: { kind: 'invoked' } });
    if (!help) return null;
    const item = help.items[help.selectedItemIndex] || help.items[0];
    return {
      prefix: ts.displayPartsToString(item.prefixDisplayParts),
      suffix: ts.displayPartsToString(item.suffixDisplayParts),
      separator: ts.displayPartsToString(item.separatorDisplayParts),
      parameters: item.parameters.map((p) => ts.displayPartsToString(p.displayParts)),
      active: help.argumentIndex,
      count: help.items.length,
    };
  }
  if (q.kind === 'detail') {
    const d = service.getCompletionEntryDetails(
      file,
      q.pos,
      q.name,
      { newLineCharacter: '\n' },
      q.source || undefined,
      preferences,
      q.data,
    );
    if (!d) return null;
    const changes = d.codeActions?.[0]?.changes || [];
    if (changes.some((c) => canonical(c.fileName) !== canonical(file))) return null;
    return {
      signature: ts.displayPartsToString(d.displayParts),
      edits: changes.flatMap((c) =>
        c.textChanges.map((t) => ({
          from: t.span.start,
          to: t.span.start + t.span.length,
          insert: t.newText,
        })),
      ),
    };
  }
  if (q.kind !== 'complete') throw Error('未知的语言请求');
  const prefix = (current.text.slice(0, q.pos).match(/[\p{L}\p{N}_$]*$/u)?.[0] || '').toLowerCase();
  const result = service.getCompletionsAtPosition(file, q.pos, preferences);
  return (result?.entries || [])
    .filter((e) => !e.isSnippet && e.name.toLowerCase().startsWith(prefix))
    .slice(0, 200)
    .map((e) => ({
      name: e.name,
      kind: e.kind,
      source: e.source,
      data: e.data,
      insert: e.insertText || e.name,
      span: e.replacementSpan || result.optionalReplacementSpan,
    }));
}
parentPort.on('message', ({ id, query: request }) => {
  try {
    parentPort.postMessage({ id, value: query(request) });
  } catch (error) {
    parentPort.postMessage({ id, error: error.message });
  }
});
