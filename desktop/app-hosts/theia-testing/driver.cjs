// One approved application tool call owns one private Theia instance and one original extension.
// The parent attaches the process family before publishing the request in this private job directory.
const { app, BrowserWindow, session } = require('electron');
const { fork } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
app.disableHardwareAcceleration();
app.setPath('userData', path.join(process.cwd(), 'profile'));
app.on('window-all-closed', () => {});

async function execute(job) {
  const token = randomBytes(32).toString('hex');
  const env = {
    ...process.env,
    THEIA_CONFIG_DIR: path.join(process.cwd(), 'config'),
    MODOT_HOST_SESSION: token,
  };
  // This fresh profile belongs exclusively to the project and extension approved for this run.
  fs.mkdirSync(env.THEIA_CONFIG_DIR, { recursive: true });
  fs.writeFileSync(
    path.join(env.THEIA_CONFIG_DIR, 'settings.json'),
    JSON.stringify({ 'security.workspace.trust.enabled': false, 'files.autoSave': 'off' }),
  );
  const backend = fork(
    path.join(job.runtime, 'lib/backend/main.js'),
    [job.workspace, '--hostname=127.0.0.1', '--port=0', '--plugins=local-dir:' + job.plugins],
    {
      execPath: job.node,
      execArgv: [],
      env,
      cwd: job.runtime,
      windowsHide: true,
      stdio: [
        'ignore',
        process.env.MODOT_TEST_HOST_DEBUG ? 'inherit' : 'ignore',
        process.env.MODOT_TEST_HOST_DEBUG ? 'inherit' : 'ignore',
        'ipc',
      ],
    },
  );
  let window;
  try {
    const port = await new Promise((resolve, reject) => {
      backend.once('error', reject);
      backend.once('exit', (code) => reject(Error('扩展宿主启动失败，退出码 ' + code)));
      backend.on('message', (m) => {
        if (Number.isInteger(m?.port) && m.port > 0 && m.address === '127.0.0.1') resolve(m.port);
      });
    });
    const origin = 'http://127.0.0.1:' + port;
    if ((await fetch(origin)).status !== 403) throw Error('可选宿主未启用私有会话验证。');
    const ownSession = session.fromPartition('app-test-' + job.runId);
    ownSession.setPermissionRequestHandler((_wc, _permission, decide) => decide(false));
    ownSession.webRequest.onBeforeSendHeaders((details, callback) => {
      const url = new URL(details.url);
      if (['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol)) {
        if (url.hostname !== '127.0.0.1' || url.port !== String(port)) return callback({ cancel: true });
        details.requestHeaders['x-modot-host-session'] = token;
      }
      callback({ requestHeaders: details.requestHeaders });
    });
    window = new BrowserWindow({
      show: false,
      webPreferences: {
        session: ownSession,
        sandbox: true,
        nodeIntegration: false,
        contextIsolation: true,
        backgroundThrottling: false,
      },
    });
    if (process.env.MODOT_TEST_HOST_DEBUG)
      window.webContents.on('console-message', (_event, _level, message) => console.error(message));
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    window.webContents.on('will-navigate', (event, url) => {
      if (new URL(url).origin !== origin) event.preventDefault();
    });
    await window.loadURL(
      origin + '/#' + encodeURI(job.workspace.replaceAll('\\', '/')).replaceAll('#', '%23'),
    );
    const deadline = Date.now() + 35000;
    while (
      !(await window.webContents.executeJavaScript('typeof globalThis.modotAppTesting === "function"'))
    ) {
      if (Date.now() >= deadline) throw Error('可选宿主未登记测试接口。');
      await pause(160);
    }
    const args = {
      extension: job.extension,
      ...(job.slot === 'vscode.formatting/1' ? { slot: job.slot, language: job.language, draft: fs.readFileSync('draft.utf8', 'utf8') } : {}),
      workspace: pathToFileURL(job.workspace).href,
      file: pathToFileURL(job.file).href,
      runId: job.runId,
    };
    return await window.webContents.executeJavaScript(
      'globalThis.modotAppTesting(' + JSON.stringify(args) + ')',
    );
  } finally {
    window?.destroy();
    if (backend.exitCode === null) backend.kill();
  }
}
async function main() {
  const until = Date.now() + 10000;
  while (!fs.existsSync('request.json')) {
    if (Date.now() > until) throw Error('宿主没有收到运行请求。');
    await pause(50);
  }
  if (fs.statSync('request.json').size > 32768) throw Error('宿主请求过长。');
  const job = JSON.parse(fs.readFileSync('request.json', 'utf8'));
  await app.whenReady();
  let response;
  try {
    response = { result: await execute(job) };
  } catch (error) {
    response = { error: String(error.message ?? error).slice(0, 1400) };
  }
  fs.writeFileSync('result.tmp', JSON.stringify({ protocol: 1, runId: job.runId, ...response }));
  fs.renameSync('result.tmp', 'result.json');
}
main().then(
  () => app.exit(0),
  (error) => {
    console.error(String(error?.message ?? error));
    app.exit(1);
  },
);
