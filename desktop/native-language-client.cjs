// A single read-only LSP client belongs to a single editor window and checkout.
// 主要引用方包括 `workroom-language.cjs`。
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const MAX_FRAME = 32 * 1024 * 1024;
const cppFile = /\.(?:c|cc|cp|cpp|cxx|c\+\+|h|hh|hpp|hxx|ipp|inl|ixx|cppm)$/i;
const documentTypes = { cpp: cppFile, rust: /\.rs$/i, python: /\.pyi?$/i };
function inside(root, file) {
  const rel = path.relative(root, file);
  return (
    !path.isAbsolute(rel) &&
    !rel.split(path.sep).some((part) => ['..', '.git', '.harness'].includes(part.toLowerCase()))
  );
}
function documentUri(root, name, language) {
  if (
    typeof name !== 'string' ||
    path.isAbsolute(name) ||
    /[:\0]/.test(name) ||
    !documentTypes[language]?.test(name)
  )
    throw Error('无效的语言文档');
  const full = path.resolve(root, name);
  if (!inside(root, full) || !inside(root, fs.realpathSync.native(full)))
    throw Error('文档已离开当前工程');
  return pathToFileURL(full).href;
}
function textMap(text) {
  const lines = [0];
  for (let p = text.indexOf('\n'); p >= 0; p = text.indexOf('\n', p + 1)) lines.push(p + 1);
  function offset(p) {
    if (
      !Number.isInteger(p?.line) ||
      p.line < 0 ||
      p.line >= lines.length ||
      !Number.isInteger(p.character) ||
      p.character < 0
    )
      return -1;
    const value = lines[p.line] + p.character;
    return value <= (lines[p.line + 1] === undefined ? text.length : lines[p.line + 1] - 1) ? value : -1;
  }
  function position(pos) {
    let low = 0,
      high = lines.length;
    while (low + 1 < high) {
      const mid = Math.floor((low + high) / 2);
      if (lines[mid] <= pos) low = mid;
      else high = mid;
    }
    return { line: low, character: pos - lines[low] };
  }
  return { offset, position };
}
function completion(result, map) {
  const list = Array.isArray(result) ? result : result?.items || [];
  const hits = [];
  for (const item of list) {
    const name = (item.filterText || item.label || '').trim().split('(')[0];
    const edit = item.textEdit,
      range = edit?.replace || edit?.range;
    const insert = edit?.newText ?? item.insertText ?? name;
    if (!name || (item.insertTextFormat === 2 && /\$\{?[\w]+/.test(insert))) continue;
    let span;
    if (range) {
      const start = map.offset(range.start),
        end = map.offset(range.end);
      if (start < 0 || end < start) continue;
      span = { start, length: end - start };
    }
    const edits = (item.additionalTextEdits || []).map((e) => ({
      from: map.offset(e.range?.start),
      to: map.offset(e.range?.end),
      insert: e.newText,
    }));
    if (edits.some((e) => e.from < 0 || e.to < e.from || typeof e.insert !== 'string')) continue;
    hits.push({
      name,
      kind: item.kind === 2 ? 'method' : item.kind === 3 ? 'function' : 'variable',
      insert,
      span,
      edits,
      description: item.detail || item.label,
    });
  }
  return hits;
}
function signature(result) {
  const s = result?.signatures?.[result.activeSignature || 0] ?? result?.signatures?.[0];
  if (!s) return null;
  let cursor = 0;
  const ranges = (s.parameters || []).map((p) => {
    const range = Array.isArray(p.label) ? p.label : [s.label.indexOf(p.label, cursor)];
    const start = range[0],
      end = range[1] ?? start + p.label.length;
    cursor = end;
    return [start, end];
  });
  const active = s.activeParameter ?? result.activeParameter ?? 0;
  if (!ranges.length || ranges.some(([a, b]) => a < 0 || b < a || b > s.label.length))
    return {
      prefix: s.label,
      suffix: '',
      parameters: [],
      separator: '',
      active,
      count: result.signatures.length,
    };
  return {
    prefix: s.label.slice(0, ranges[0][0]),
    suffix: s.label.slice(ranges.at(-1)[1]),
    separator: ranges.length > 1 ? s.label.slice(ranges[0][1], ranges[1][0]) : '',
    parameters: ranges.map(([a, b]) => s.label.slice(a, b)),
    active,
    count: result.signatures.length,
  };
}
function hover(result, map) {
  if (!result?.contents) return null;
  const contents = Array.isArray(result.contents) ? result.contents : [result.contents];
  const blocks = [];
  for (const part of contents) {
    const value = typeof part === 'string' ? part : part?.value;
    if (typeof value !== 'string' || !value.trim()) continue;
    if (part?.language || part?.kind === 'plaintext') {
      blocks.push({ code: !!part.language, text: value.trim() });
      continue;
    }
    // Some servers return Markdown even when the client prefers plain text.
    const sections = value.split(/^`{3,}[^\n]*$/m);
    sections.forEach((text, i) => {
      if (text.trim()) blocks.push({ code: i % 2 === 1, text: text.trim() });
    });
  }
  if (!blocks.length) return null;
  const from = map.offset(result.range?.start), to = map.offset(result.range?.end);
  return from >= 0 && to > from ? { from, to, blocks } : { blocks };
}
function launch(language, auth) {
  const env = { ...process.env };
  if (language === 'python') {
    for (const key of Object.keys(env)) {
      if (['NODE_OPTIONS', 'NODE_PATH', 'PYTHONPATH', 'PYTHONHOME'].includes(key.toUpperCase())) delete env[key];
    }
    env.ELECTRON_RUN_AS_NODE = '1';
    env.PYTHONSAFEPATH = '1';
    return {
      command: process.execPath,
      env,
      args: [auth.server, '--stdio'],
      options: {},
      settings: {
        python: { pythonPath: auth.pythonPath, analysis: { diagnosticMode: 'openFilesOnly' } },
      },
    };
  }
  if (language === 'cpp')
    return {
      env,
      args: [
        '--completion-parse=always',
        '--background-index=0',
        '--pch-storage=memory',
        '--clang-tidy=0',
        '--enable-config=0',
        '--log=error',
      ],
      options: {},
    };
  Object.assign(env, {
    CARGO: auth.cargo,
    RUSTC: auth.rustc,
    RUST_SRC_PATH: auth.sources,
    RUSTUP_AUTO_INSTALL: '0',
    RUSTC_WRAPPER: '',
    RUSTC_WORKSPACE_WRAPPER: '',
    CARGO_NET_OFFLINE: 'true',
  });
  // Windows environment keys are case-insensitive. Pass exactly one PATH to the child.
  const pathKey = Object.keys(env).find((k) => k.toUpperCase() === 'PATH') || 'PATH';
  env[pathKey] = [path.dirname(auth.rustc), path.dirname(auth.cargo), env[pathKey] || ''].join(
    path.delimiter,
  );
  const locked = fs.existsSync(path.join(auth.root, 'Cargo.lock'));
  return {
    env,
    args: [],
    options: {
      cargo: {
        allTargets: false,
        sysroot: auth.sysroot,
        sysrootSrc: auth.sources,
        noDeps: !locked,
        metadataExtraArgs: locked ? ['--locked'] : [],
        buildScripts: { enable: false },
      },
      checkOnSave: false,
      procMacro: { enable: false },
    },
  };
}
function bounded(promise, ms) {
  let timer;
  return Promise.race([
    promise,
    new Promise((resolve) => {
      timer = setTimeout(resolve, ms);
      timer.unref();
    }),
  ]).finally(() => clearTimeout(timer));
}

class NativeLanguageClient {
  constructor(language, auth, onExit) {
    this.language = language;
    this.root = auth.root;
    this.folder = { uri: pathToFileURL(auth.root).href, name: path.basename(auth.root) };
    this.pending = new Map();
    this.documents = new Map();
    this.indexWaiters = new Set();
    this.projectReady = false;
    this.buffer = Buffer.alloc(0);
    this.sequence = 0;
    this.closed = false;
    this.exited = false;
    const { env, args, options, settings, command = auth.server } = launch(language, auth);
    this.options = options;
    this.settings = settings;
    this.child = spawn(command, args, {
      cwd: auth.root,
      env,
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'ignore'],
    });
    this.exit = new Promise((resolve) => {
      this.child.once('close', () => {
        this.exited = true;
        this.failPending(Error('语言服务已退出'));
        this.wakeIndex();
        resolve();
        onExit();
      });
    });
    this.child.on('error', (e) => this.failPending(e));
    this.child.stdin.on('error', (e) => this.failPending(e));
    this.child.stdout.on('data', (chunk) => {
      try {
        this.frames(chunk);
      } catch (e) {
        this.failPending(e);
        void this.close().catch(() => {});
      }
    });
    this.ready = this.request(
      'initialize',
      {
        processId: process.pid,
        rootUri: this.folder.uri,
        workspaceFolders: [this.folder],
        initializationOptions: options,
        capabilities: {
          general: { positionEncodings: ['utf-16'] },
          experimental: { serverStatusNotification: true },
          workspace: { configuration: true, workspaceFolders: true },
          window: { workDoneProgress: true },
          textDocument: {
            hover: { contentFormat: ['plaintext', 'markdown'] },
            completion: { completionItem: { snippetSupport: false } },
            signatureHelp: {
              signatureInformation: { parameterInformation: { labelOffsetSupport: true } },
            },
          },
        },
      },
      45000,
    ).then(result => {
      this.supportsHover = !!result?.capabilities?.hoverProvider;
      if (!this.closed) this.send({ method: 'initialized', params: {} });
    });
    this.ready.catch(() => {
      void this.close().catch(() => {});
    });
  }
  frames(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (this.buffer.length) {
      const e = this.buffer.indexOf('\r\n\r\n');
      if (e < 0) {
        if (this.buffer.length > 8192) throw Error('无效的语言服务消息头');
        return;
      }
      const match = /^Content-Length:\s*(\d+)\s*$/im.exec(this.buffer.toString('ascii', 0, e));
      const size = match ? Number(match[1]) : -1;
      if (e > 8192 || size < 0 || size > MAX_FRAME) throw Error('无效的语言服务消息长度');
      const start = e + 4;
      if (this.buffer.length < start + size) return;
      const message = JSON.parse(this.buffer.toString('utf8', start, start + size));
      this.buffer = this.buffer.subarray(start + size);
      this.receive(message);
    }
  }
  send(data) {
    if (this.exited || this.child.stdin.destroyed) throw Error('语言服务已断开');
    const msg = Buffer.from(JSON.stringify({ jsonrpc: '2.0', ...data }));
    this.child.stdin.write(
      Buffer.concat([Buffer.from('Content-Length: ' + msg.length + '\r\n\r\n'), msg]),
    );
  }
  request(method, params, ms = 15000) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(Error('语言服务响应超时'));
        try {
          this.send({ method: '$/cancelRequest', params: { id } });
        } catch {}
      }, ms);
      timer.unref();
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.send({ id, method, params });
      } catch (e) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(e);
      }
    });
  }
  failPending(error) {
    for (const p of this.pending.values()) {
      clearTimeout(p.timer);
      p.reject(error);
    }
    this.pending.clear();
  }
  wakeIndex() {
    for (const wake of this.indexWaiters) wake();
  }
  receive(m) {
    if (m.method && m.id !== undefined) {
      let result = null;
      if (m.method === 'workspace/configuration')
        result = (m.params?.items || []).map((i) => {
          if (this.settings)
            return i.section
              ? i.section.split('.').reduce((v, k) => v?.[k], this.settings) ?? null
              : this.settings;
          if (!i.section || i.section === 'rust-analyzer') return this.options;
          return (
            i.section
              .replace(/^rust-analyzer\./, '')
              .split('.')
              .reduce((v, k) => v?.[k], this.options) ?? null
          );
        });
      else if (m.method === 'workspace/workspaceFolders') result = [this.folder];
      else if (m.method === 'workspace/applyEdit') result = { applied: false };
      else if (
        ![
          'window/workDoneProgress/create',
          'client/registerCapability',
          'client/unregisterCapability',
          'workspace/semanticTokens/refresh',
          'workspace/inlayHint/refresh',
          'workspace/codeLens/refresh',
          'workspace/diagnostic/refresh',
        ].includes(m.method)
      ) {
        this.send({ id: m.id, error: { code: -32601, message: 'Unsupported client method' } });
        return;
      }
      this.send({ id: m.id, result });
      return;
    }
    if (m.method === 'textDocument/publishDiagnostics' && this.language === 'cpp') {
      const doc = this.documents.get(m.params?.uri);
      if (doc && (m.params.version == null || m.params.version === doc.version)) {
        doc.parsed = true;
        this.wakeIndex();
      }
    }
    if (m.method === 'experimental/serverStatus') {
      this.projectReady = m.params?.quiescent === true;
      if (this.projectReady) this.wakeIndex();
    }
    if (m.method || m.id === undefined) return;
    const p = this.pending.get(m.id);
    if (!p) return;
    this.pending.delete(m.id);
    clearTimeout(p.timer);
    if (m.error) p.reject(Error(m.error.message));
    else p.resolve(m.result);
  }
  sync(query) {
    const next = new Map();
    if (!Array.isArray(query.documents)) throw Error('缺少当前文档');
    for (const d of query.documents) {
      const uri = documentUri(this.root, d.path, this.language);
      if (typeof d.text !== 'string' || next.has(uri)) throw Error('无效的文档草稿');
      next.set(uri, { text: d.text, file: d.path });
    }
    for (const uri of this.documents.keys())
      if (!next.has(uri)) {
        this.send({ method: 'textDocument/didClose', params: { textDocument: { uri } } });
        this.documents.delete(uri);
      }
    for (const [uri, d] of next) {
      const existing = this.documents.get(uri);
      if (existing?.text === d.text) continue;
      const version = (existing?.version || 0) + 1;
      if (existing)
        this.send({
          method: 'textDocument/didChange',
          params: { textDocument: { uri, version }, contentChanges: [{ text: d.text }] },
        });
      else
        this.send({
          method: 'textDocument/didOpen',
          params: {
            textDocument: {
              uri,
              version,
              languageId: this.language === 'cpp' && d.file.endsWith('.c') ? 'c' : this.language,
              text: d.text,
            },
          },
        });
      this.documents.set(uri, { text: d.text, version, parsed: existing?.parsed ?? false });
    }
    this.wakeIndex();
  }
  async query(q) {
    await this.ready;
    if (this.closed) return null;
    this.sync(q);
    const uri = documentUri(this.root, q.file, this.language),
      doc = this.documents.get(uri);
    if (!doc || !Number.isInteger(q.pos) || q.pos < 0 || q.pos > doc.text.length)
      throw Error('当前文档已变化');
    const map = textMap(doc.text),
      params = { textDocument: { uri }, position: map.position(q.pos) };
    const methods = { hover: 'textDocument/hover', signature: 'textDocument/signatureHelp', complete: 'textDocument/completion' };
    if (!Object.hasOwn(methods, q.kind)) throw Error('未知的语言请求');
    if (q.kind === 'hover' && !this.supportsHover) return null;
    const get = () => this.request(methods[q.kind], q.kind === 'complete' ? { ...params, context: { triggerKind: 1 } } : params);
    const indexed = () =>
      this.language === 'python' || (this.language === 'rust' ? this.projectReady : doc.parsed);
    const cold = !indexed();
    const waitForIndex = async () => {
      if (indexed()) return;
      let wake;
      const wait = new Promise((resolve) => {
        wake = () => {
          if (this.closed || this.exited || doc !== this.documents.get(uri) || indexed()) resolve();
        };
        this.indexWaiters.add(wake);
      });
      await bounded(wait, 8000);
      this.indexWaiters.delete(wake);
    };
    // On a cold clangd file the first completion can snapshot a partial header index.
    if (cold && this.language === 'cpp') await waitForIndex();
    if (this.closed || doc !== this.documents.get(uri)) return null;
    let result = await get();
    const hasResult = q.kind === 'hover' ? !!result?.contents : !!(result?.signatures || result?.items || result)?.length;
    if (!hasResult && cold && !this.closed) {
      if (this.language === 'rust') await waitForIndex();
      else await new Promise((resolve) => setTimeout(resolve, 500));
      if (!this.closed && doc === this.documents.get(uri)) result = await get();
    }
    if (this.closed || doc !== this.documents.get(uri)) return null;
    if (q.kind === 'hover') return hover(result, map);
    return q.kind === 'signature' ? signature(result) : completion(result, map);
  }
  close() {
    if (this.closing) return this.closing;
    this.closed = true;
    this.wakeIndex();
    this.failPending(Error('语言会话已关闭'));
    this.closing = (async () => {
      if (this.exited) return;
      try {
        await this.request('shutdown', null, 2200);
        this.send({ method: 'exit' });
      } catch {}
      this.child.stdin.end();
      await bounded(this.exit, 1500);
      if (!this.exited) {
        this.child.kill('SIGKILL');
        await bounded(this.exit, 2000);
        if (!this.exited) throw Error('语言服务的进程退出尚未确认');
      }
    })();
    return this.closing;
  }
}
module.exports = { NativeLanguageClient };
