// Semantic queries are tied to an authenticated shell, a mounted checkout and an installed revision.
// 主要引用方包括 `bridge-v2.cjs`。
const { Worker } = require('node:worker_threads');
const path = require('node:path');
const fs = require('node:fs');
const { randomUUID } = require('node:crypto');
const { NativeLanguageClient } = require('./native-language-client.cjs');
const route = '/api/v2/plugins/applications/code-workroom/languages';
const supported = new Set(['typescript', 'cpp', 'rust', 'python']);
module.exports = function languageHost(request, dataRoot) {
  const clients = new Map(),
    observers = new Set();
  let watcher;
  const revisions = new Map(),
    stops = new Map();
  async function call(args) {
    const r = await request(args);
    if (r.status !== 200)
      throw Error(
        typeof r.body?.error === 'string' ? r.body.error : '语言服务不可用，请重新打开工作间。',
      );
    return r.body;
  }
  function notify(state) {
    for (const wc of observers) if (!wc.isDestroyed()) wc.send('workbench:language-changed', state);
  }
  function stop(session) {
    if (!session || session.closed) return session?.stopping ?? Promise.resolve();
    session.closed = true;
    if (clients.get(session.sender) === session) clients.delete(session.sender);
    for (const item of session.pending.values()) item.resolve(null);
    session.pending.clear();
    session.stopping =
      session.native?.close() ?? session.worker?.terminate().catch(() => {}) ?? Promise.resolve();
    const stopping = session.stopping;
    stops.set(stopping, session.language);
    stopping.finally(() => stops.delete(stopping)).catch(() => {});
    return stopping;
  }
  function stopOld(state) {
    for (const session of [...clients.values()]) {
      if (session.language === state.id && (!state.enabled || session.scope.revision !== state.revision))
        void stop(session);
    }
    return Promise.all([...stops].filter(([, language]) => language === state.id).map(([done]) => done));
  }
  function changed(state) {
    const previous = revisions.get(state.id);
    if (previous && previous.revision >= state.revision) return previous.notification;
    const notification = stopOld(state).then(async () => {
      notify(state);
      // Another window can own the final Windows image handle. Collect retired files after it closes.
      if (!state.installed && state.revision > 0) await call({ path: route });
    });
    revisions.set(state.id, { revision: state.revision, notification });
    return notification;
  }
  function stopWatching() {
    watcher?.close();
    watcher = undefined;
  }
  function observe(file) {
    if (watcher) return;
    // The registry is atomically replaced; watch its parent to observe changes from other instances.
    watcher = fs.watch(file, { persistent: false }, (_event, name) => {
      if (name && String(name) !== 'installed.json') return;
      void call({ path: route })
        .then((catalog) => Promise.all(catalog.languages.map(changed)))
        .catch(() => {
          void Promise.all([...clients.values()].map(stop));
        });
    });
    watcher.on('error', () => {
      stopWatching();
      void Promise.all([...clients.values()].map(stop));
    });
  }
  function track(wc) {
    if (observers.has(wc)) return;
    observers.add(wc);
    const drop = () => {
      void stop(clients.get(wc));
    };
    wc.on('did-start-navigation', (_event, _url, inPlace, mainFrame) => {
      if (mainFrame && !inPlace) drop();
    });
    wc.on('render-process-gone', drop);
    wc.once('destroyed', () => {
      observers.delete(wc);
      drop();
      if (!observers.size) stopWatching();
    });
  }
  function start(session, auth) {
    observe(path.join(dataRoot, 'runs', 'workbench', 'plugins'));
    if (session.language !== 'typescript') {
      session.native = new NativeLanguageClient(session.language, auth, () => {
        if (!session.closed) void stop(session);
      });
      return;
    }
    const worker = new Worker(path.join(__dirname.replace(/app\.asar$/, 'app.asar.unpacked'), 'typescript-language-worker.cjs'), {
      workerData: { root: auth.root, library: auth.library },
    });
    session.worker = worker;
    worker.on('message', (reply) => {
      const work = session.pending.get(reply.id);
      if (!work) return;
      session.pending.delete(reply.id);
      if (reply.error) work.reject(Error(reply.error));
      else work.resolve(reply.value);
    });
    worker.on('error', () => {
      void stop(session);
    });
    worker.once('exit', () => {
      if (!session.closed) void stop(session);
    });
  }
  function authority(session) {
    return call({
      path: route + '/' + session.language + '/authority?' + new URLSearchParams(session.scope),
    });
  }
  async function run(sender, cmd) {
    track(sender);
    const language = cmd?.language ?? 'typescript';
    if (!supported.has(language)) throw Error('未知的语言插件');
    if (cmd?.op === 'catalog' || cmd?.op === 'status') {
      const state = await call({ path: cmd.op === 'catalog' ? route : route + '/' + language });
      if (observers.has(sender)) observe(path.join(dataRoot, 'runs', 'workbench', 'plugins'));
      return state;
    }
    if (cmd?.op === 'change') {
      const state = await call({
        path: route + '/' + language,
        method: 'POST',
        requestId: randomUUID(),
        body: { action: cmd.action, expectedRevision: cmd.revision },
      });
      await changed(state);
      return state;
    }
    const current = clients.get(sender);
    if (cmd?.op === 'open') {
      if (!/^[\w-]{12,64}$/.test(cmd.token) || !cmd.scope || typeof cmd.scope !== 'object')
        throw Error('无效的语言会话');
      if (current) void stop(current);
      clients.set(sender, {
        sender,
        language,
        token: cmd.token,
        scope: cmd.scope,
        closed: false,
        pending: new Map(),
        seq: 0,
      });
      return true;
    }
    if (cmd?.op === 'release') {
      if (current?.token === cmd.token) await stop(current);
      return true;
    }
    if (cmd?.op !== 'query') throw Error('无效的语言请求');
    if (!current || current.token !== cmd.token) return null;
    let auth;
    try {
      auth = await authority(current);
    } catch (e) {
      await stop(current);
      throw e;
    }
    if (current.closed) return null;
    if (!current.worker && !current.native) start(current, auth);
    const value = current.native
      ? await current.native.query(cmd.query)
      : await new Promise((resolve, reject) => {
          const id = ++current.seq;
          current.pending.set(id, { resolve, reject });
          try {
            current.worker.postMessage({ id, query: cmd.query });
          } catch (e) {
            current.pending.delete(id);
            reject(e);
          }
        });
    if (current.closed) return null;
    try {
      await authority(current);
    } catch (e) {
      await stop(current);
      throw e;
    }
    if (current.closed) return null;
    return value;
  }
  return {
    run,
    close: () => {
      stopWatching();
      for (const client of [...clients.values()]) void stop(client);
      return Promise.all([...stops.keys()]);
    },
  };
};
