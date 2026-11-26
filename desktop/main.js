// Electron 主进程：启动/核对服务、受约束 IPC、窗口与后台运行生命周期。
//
// 为什么界面不直接调 Python 函数：那样界面和内核焊死，换前端要重写。
// 现在的边界是 HTTP（bin/server.py），所以这个文件的全部职责就是"当个启动器"。
// 界面崩了、重开、换一版，都不影响正在跑的任务。

// 主要引用或加载入口包括 `preview-app.cjs` 和 `package.json`。

require('./stdio-guard.cjs')()
const { app, BrowserWindow, shell, Menu } = require('electron')
const { spawn } = require('child_process')
const path = require('path')
const fs = require('fs')
const http = require('http')
if (process.env.HARNESS_USER_DATA) app.setPath('userData',path.resolve(process.env.HARNESS_USER_DATA))
if (app.isPackaged && !app.requestSingleInstanceLock()) {
  app.quit()
  return
}

const ROOT = path.resolve(__dirname, '..')       // 仓库根（开发时 = 程序根）

// ★ 程序根 / 数据根（2026-10-26）。打包后：
//   程序在 app.asar 里（只读，而且 python 读不到 asar）⇒ 真正的磁盘目录是 app.asar.unpacked；
//   数据必须落到用户目录。开发时**两者都退回仓库根**，行为一个字不变。
//   这两件事只在 `paths.py` 里判断，这里只负责把环境变量递过去。
function appDir() {
  if (compiledCore) return path.dirname(compiledCore)
  if (!app.isPackaged) return ROOT
  return path.join(process.resourcesPath, 'app.asar.unpacked', 'backend')
}

// 返回当前启动方式所用的用户数据根，后端和桥接共用该位置。
function dataDir() {
  return path.join(app.getPath('userData'), 'data')
}

// 为 `bin/server.py` 准备运行时环境，已安装版本以自身配置隔离开发环境的模型与凭据。
function childEnv() {
  const env = { ...process.env, HARNESS_APP: appDir() }
  if (app.isPackaged) {
    env.HARNESS_DATA = dataDir()
    for (const key of ["PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "HARNESS_PYTHON", "HARNESS_SANDBOX_PYTHON_ROOT", "HARNESS_COMPILED_CORE"]) delete env[key]
    // Installed profiles own their credentials, including when the developer
    // account on the same machine has configured the CLI or an unpackaged build.
    for (const key of Object.keys(env)) {
      if (/^HARNESS_(?:(?:PLAN|REVIEW|VISION)_)?(?:BASE_URL|API_KEY|MODEL|REASONING|CTX|MAXTOK|FALLBACK_MODELS)$/i.test(key)
        || /^HARNESS_(?:MODELS|CREDS)$/i.test(key)) delete env[key]
    }
    env.HARNESS_MODELS = path.join(env.HARNESS_DATA, 'models.json')
    env.HARNESS_CREDS = path.join(env.HARNESS_DATA, 'credentials.env')
    env.PYTHONNOUSERSITE = "1"
    env.HARNESS_GIT = path.join(process.resourcesPath, "app.asar.unpacked", "git", "cmd", "git.exe")
  }
  return env
}
const PORT = Number(process.env.HARNESS_PORT || 18791)
const compiledCore = !app.isPackaged && process.env.HARNESS_COMPILED_CORE
  ? path.resolve(process.env.HARNESS_COMPILED_CORE) : null

// ── 接口的钥匙（2026-10-26）──────────────────────────────────────────────
// ★ 钥匙由 Python 服务自己生成并写进数据目录（`loop/paths.py` 的 `token_file()`）；
//   外壳只**读**，不生成、不保管第二份 —— 两份就是两个真源。
// ★ 怎么给页面：走 `loadFile(..., {query:{token}})`。页面里 `api()` 读出来带上。
//   为什么不用 executeJavaScript 注入：那要等页面脚本跑完，而页面一加载就开始拉数据，
//   中间那段会全是 401（现象是"开起来一片红，过几秒自己好了"）。
// 服务令牌在数据目录中按实际运行配置定位。
function tokenFile() {
  return path.join(app.isPackaged ? dataDir() : (process.env.HARNESS_DATA || ROOT), 'api-token')
}

function readToken() {
  try {
    return fs.readFileSync(tokenFile(), 'utf8').trim()
  } catch (e) {
    // 读不到不是致命错：页面会拿到 401 并自己说清楚。
    // 但主进程日志要留一行 —— 否则"界面什么都读不出来"只能靠人猜。
    log('读不到接口钥匙：', tokenFile(), String(e && e.message))
    return ''
  }
}
const { findPython } = require(path.join(__dirname, 'find-python.cjs'))

let pyProc = null
let pyExit = null        // 服务进程的退出码（null = 还活着 / 还没退）
let win = null
let quitting = false
let pyInfo = null
let bootError = ''      // 空 = 正常；否则是**给用户看的一句话**（会显示在页面上）
const lifecycle = require('./lifecycle-v2.cjs')({getWindow:()=>win,isQuitting:()=>quitting,quit:()=>app.quit()})
const bridge = require('./bridge-v2.cjs')({getWindow:()=>win,port:PORT,readToken,root:ROOT,
  packageRoot:app.isPackaged ? appDir() : ROOT, dataRoot:path.dirname(tokenFile()),reconnectBackend,isQuitting:()=>quitting,
  closeBehavior:lifecycle.closeBehavior,displayLanguage:lifecycle.displayLanguage,interfaceZoom:lifecycle.interfaceZoom})
if (app.isPackaged) app.on('second-instance', lifecycle.show)

// ★ 这个应用只访问 127.0.0.1，不该走系统代理。
//   实测环境里有 http_proxy，连本地端口都被转发（curl 直连 8788 返回 502 Bad Gateway），
//   而 Electron 用的是同一套系统代理设置 —— 不关掉的话，页面里所有请求都会
//   `Failed to fetch`，看起来就是「输入发不出去」。
app.commandLine.appendSwitch('no-proxy-server')

function log(...a) { console.log('[harness]', ...a) }

function startPython() {
  // ★★ 不要每次都传 `--policy`（2026-10-29 用户：「权限每次点开都要重新设置 trust」）。
  //   原来这里写死 `process.env.HARNESS_POLICY || 'workspace'`，于是每次启动都带
  //   `--policy workspace` —— 而服务端是"给了参数就**不看保存的**"
  //   （`POLICY = a.policy or _load_policy()`）。
  //   结果：界面上改成 trusted、存进 policy.json，下次启动又被这个参数打回 workspace。
  //   ⇒ 保存机制是好的，是**启动器把它覆盖了**。
  //   现在：只在显式设了 HARNESS_POLICY 时才传（那是"这次就要这一档"的意思），
  //   否则让服务端读保存的档位。
  const entry = compiledCore ? ['owned-process'] : ['-I', 'bin/owned-process.py']
  const args = [...entry, '--server', '--port', String(PORT)]
  pyExit = null
  if (process.env.HARNESS_POLICY) args.push('--policy', process.env.HARNESS_POLICY)
  // pyInfo.args 不能漏：Windows 的 `py` 要靠 `-3` 才知道用哪个版本
  pyProc = spawn(compiledCore || pyInfo.cmd, compiledCore ? args : [...pyInfo.args, ...args],
                 { cwd: appDir(), windowsHide: true, env: childEnv() })
  pyProc.on('error', error => {
    pyExit = 1
    log('无法启动本地后端：', error.message)
  })
  pyProc.stdout.on('data', d => log(String(d).trim()))
  pyProc.stderr.on('data', d => log('[err]', String(d).trim()))
  pyProc.on('exit', (code, signal) => {
    // A signal-terminated process also has a null exit code; null alone is
    // reserved for a controller that has not exited yet.
    pyExit = code === null ? (signal || 'terminated') : code
    log('python 退出，code =', code)
    // 服务是界面的地基。它没了，界面就是空壳 —— 说清楚，别让人对着一个
    // 转圈的页面猜。
    if (!quitting && win && !win.isDestroyed()) {
      win.webContents.executeJavaScript(
        `window.__serverDown && window.__serverDown(${code})`).catch(() => {})
    }
  })
}

let reconnecting = null
// 合并并发重连。只有由当前应用启动且已退出的后端才允许重启，外部服务仍由其原启动入口恢复。
function reconnectBackend() {
  if (reconnecting) return reconnecting
  reconnecting = new Promise((resolve, reject) => {
    probeExisting(ok => {
      if (ok) return resolve({connected:true,restarted:false})
      if (!pyProc) return reject(Error('外部服务不可用，请在其原启动入口恢复；本窗口不会接管或重放任务。'))
      if (pyExit === null) return reject(Error('自有服务控制器仍运行，请先核对其状态；未重复启动。'))
      startPython()
      waitReady(ready => ready ? resolve({connected:true,restarted:true}) : reject(Error('服务恢复失败，历史与未知操作保留，未重放。')))
    })
  }).finally(() => { reconnecting = null })
  return reconnecting
}
// 在有限轮询时间内核对后端健康；窗口启动或重连使用同一条件。
function waitReady(done, tries = 80) {
  probeExisting(ok => {
    if (ok) return done(true)
    if (tries <= 0) return done(false)
    setTimeout(() => waitReady(done, tries - 1), 250)
  })
}

// 检查既有端口上的服务是否与当前 Modot 数据根和客户端身份匹配。
function probeExisting(done) {
  let settled = false
  const finish = value => { if (!settled) { settled = true; done(value) } }
  const req = http.get({ host: '127.0.0.1', port: PORT, path: '/api/health?full=1',
    headers: { 'X-Harness-Token': readToken() }, timeout: 1200 }, res => {
    let raw = ''
    res.on('data', d => { raw += d; if (raw.length > 65536) { req.destroy(); finish(false) } })
    res.on('end', () => {
      try {
        const b = JSON.parse(raw)
        const expected = app.isPackaged ? dataDir() : (process.env.HARNESS_DATA || ROOT)
        finish(res.statusCode === 200 && b.protocol === 1 && b.agentFirstClient === 2 && !!b.instance &&
          path.resolve(b.paths.data).toLowerCase() === path.resolve(expected).toLowerCase() &&
          (!compiledCore || path.resolve(b.paths.app).toLowerCase() === appDir().toLowerCase()))
      } catch { finish(false) }
    })
  })
  req.on('error', () => finish(false))
  req.on('timeout', () => { req.destroy(); finish(false) })
}

// 创建加载新版渲染器与隔离预加载脚本的主窗口，关闭设置交给 `lifecycle-v2.cjs`。
function createWindow() {
  const zoomFactor = lifecycle.interfaceZoom().percent / 100
  win = new BrowserWindow({
    width: 1260, height: 830,
    minWidth: 700, minHeight: 620,
    backgroundColor: '#fafafa',
    title: 'Modot',
    icon: path.join(__dirname, 'icons', 'modot.png'),
    // ★ 边框内化（2026-10-28 用户：「Windows 自带的边框太丑了，能像 Codex 那样内化吗」）。
    //   系统标题栏和菜单栏整个去掉，最小化/最大化/关闭变成**页面右上角的小图标**，
    //   颜色跟我们的界面走 —— 那条紫色系统栏和 File/Edit 菜单都没有了。
    // ★ 为什么不用 frame:false + 自绘按钮：那要开一条 IPC 让页面调窗口动作，
    //   而我们刻意保持 sandbox:true、页面里没有 Node —— 不为三个按钮开那个口。
    titleBarStyle: 'hidden',
    titleBarOverlay: { color: '#fafafa', symbolColor: '#22252a', height: Math.floor(34 * zoomFactor) },
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      preload:path.join(__dirname,'preload-v2.cjs'),
      zoomFactor,
      // 界面不需要 Node —— 它只发 HTTP。关掉这两个，等于把"页面能碰系统"
      // 这条攻击面直接去掉。
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      // ★ 关掉**后台节流**（2026-10-29 用户：窗口切走后一片空白）。
      //   Electron 默认 `backgroundThrottling: true`：窗口不可见时，
      //   页面里的定时器会被**压慢甚至暂停**。
      //   我们的流式渲染靠 `setTimeout` 合并帧，一被节流，
      //   数据到了但那一帧没画上去 —— 界面上就是一排空块。
      //   这不是"让它在后台猛跑"，是让"你切回来看的时候，内容是全的"。
      backgroundThrottling: false,
    },
  })
  if (lifecycle) lifecycle.attach(win)
  // 菜单栏**彻底去掉**（不是 Alt 还能叫出来的那种隐藏）：
  //   它是系统外观的一部分，留着就等于边框只内化了一半。
  Menu.setApplicationMenu(null)
  // 由 Electron 执行系统编辑命令，输入栏、正文和工作间沿用原生选区。
  win.webContents.on('context-menu', (_event, { isEditable, mediaType, editFlags, frame, menuSourceType }) => {
    if (!isEditable && (mediaType !== 'none' || !(editFlags.canCopy || editFlags.canSelectAll))) return
    const english = lifecycle.displayLanguage().locale === 'en'
    const item = (role, zh, en, enabled) => ({ role, label: english ? en : zh, enabled })
    const copy = item('copy', '复制', 'Copy', editFlags.canCopy)
    const selectAll = item('selectAll', '全选', 'Select All', editFlags.canSelectAll)
    const actions = isEditable ? [
      item('undo', '撤销', 'Undo', editFlags.canUndo),
      item('redo', '重做', 'Redo', editFlags.canRedo),
      { type: 'separator' },
      item('cut', '剪切', 'Cut', editFlags.canCut),
      copy,
      item('paste', '粘贴', 'Paste', editFlags.canPaste),
      { type: 'separator' },
      selectAll,
    ] : [copy, selectAll]
    Menu.buildFromTemplate(actions).popup({ window: win, frame: frame ?? undefined, sourceType: menuSourceType })
  })
  win.once('ready-to-show', () => { if(process.env.HARNESS_START_HIDDEN!=='1')win.show() })
  // ★ 启动期的问题要带进页面。只打在主进程日志里，用户看不到 ——
  //   而"输入发不出去"当时的真相就是：服务没起，页面上一个字都不说。
  const query = { port: String(PORT) }
  if (bootError) query.bootError = bootError
  // ★★ 启动时**清掉页面缓存**（2026-10-30 用户：「前端没有更新」「空白块依旧有」）。
  //   Electron 会把 renderer 的 JS/CSS 缓存下来 —— 于是改了代码、重启应用，
  //   界面跑的还是**上一次那份**。我连着修了几轮，用户那边一次都没生效。
  //   这类"改了没用"的排查成本极高（会误以为是自己改错了），必须从根上断掉。
  // ★ `clearCache()` 是**异步**的：不等它清完就 loadFile，
  //   页面还是会从**还没清掉的缓存**里读 —— 那这行就等于没写。
  Promise.resolve(win.webContents.session.clearCache())
    .catch(e => log('清缓存失败（继续加载）', e))
    .then(() => win.loadFile(path.join(__dirname, 'renderer-v2/dist', 'index.html'), { query }))

  // 渲染进程的报错也转到主进程日志里，否则前端出错只能靠人开 DevTools 才看得见
  win.webContents.on('console-message', (_e, level, message, line, source) => {
    if (level >= 2) log('[page]', message, `(${source}:${line})`)
  })

  // 外链走系统浏览器，不在应用里开新窗口 —— 否则用户会看到一个没有地址栏的
  // 窗口，不知道自己被带到哪去了。
  win.webContents.setWindowOpenHandler(({ url }) => {
    try { if (['https:','http:','mailto:'].includes(new URL(url).protocol)) shell.openExternal(url) } catch {}
    return { action: 'deny' }
  })
  win.webContents.on('will-navigate',(event,url)=>{
    try {
      const u = new URL(url)
      const target = require("url").fileURLToPath(u)
      const allowed = [path.join(__dirname,"renderer-v2","dist","index.html")]
      if(u.protocol!=="file:" || u.host || !allowed.some(p => path.resolve(p).toLowerCase() === path.resolve(target).toLowerCase())) event.preventDefault()
    } catch {event.preventDefault()}
  })
}

app.whenReady().then(() => {
  if (compiledCore && (!process.env.HARNESS_DATA || !process.env.HARNESS_USER_DATA)) {
    bootError = '编译后端测试需要单独指定数据目录与 Electron 配置目录。'
    createWindow()
    return
  }
  probeExisting(already => {
    if (already) {
      // 开发时热重载、双击第二个图标都会撞到这里。健康服务应当复用，
      // 而不是再 spawn 一个让它以 code 2 退出，再把错误显示给使用者。
      log('复用已在运行的 harness 服务，端口', PORT)
      createWindow()
      return
    }
    if (compiledCore && !fs.existsSync(compiledCore)) {
      bootError = '找不到指定的编译后端。'
      createWindow()
      return
    }
    if (!compiledCore) pyInfo = findPython({packaged: app.isPackaged})
    if (!compiledCore && !pyInfo) {
      // ★ 这是最常见的启动失败：机器上没有 3.10+ 的 Python。
      //   （实测：系统 PATH 里 python 是应用商店的空壳、py -3 只有 3.7。）
      //   说清"缺什么、怎么办"，别让人对着空界面猜。
      bootError = app.isPackaged ? '内嵌 Python 缺失或不可运行，请核对发行包完整性。' : '找不到能用的 Python（需要 3.10 或更高）。'
        + ' 这台机器上的 python 是应用商店的执行别名，py -3 只有 3.7。'
        + ' 办法：在 desktop/python-path.txt 里写一行真实解释器的绝对路径。'
      log('没有可用的 Python')
      createWindow()
      return
    }
    log(compiledCore ? '编译后端：' : 'Python：', compiledCore || pyInfo.exe)
    startPython()
    waitReady(ok => {
      if (pyExit !== null && pyExit !== 0) {
        bootError = '端口 ' + PORT + ' 上已有 Modot 服务运行。'
          + '关掉那个窗口再开，或者设环境变量 HARNESS_PORT 换个端口。'
      } else if (!ok) {
        bootError = '本地服务没起来。看窗口里「执行流」下方的输出，或主进程日志。'
      }
      createWindow()
    })
  })
})

app.on('window-all-closed', () => {
  app.quit()
})

let cleanupDone = false
app.on('before-quit', event => {
  if (cleanupDone) return
  event.preventDefault()
  if (quitting) return
  quitting = true
  Promise.allSettled([
    bridge.closeLanguages(),
    pyProc && pyExit === null ? bridge.request({path:'/api/v2/lifecycle',method:'POST',body:{confirm:true}}) : null,
  ]).then(results => {
    for (const result of results) if (result.status === 'rejected') log('清理后端会话：', String(result.reason))
  })
    .finally(async () => {
      if (pyProc && pyExit === null) {
        const owned = pyProc
        try { owned.stdin.end('stop\n') } catch {}
        await new Promise(resolve => {
          if (owned.exitCode !== null) return resolve()
          const timeout = setTimeout(() => { try { owned.kill() } catch {}; resolve() }, 8000)
          owned.once('exit', () => { clearTimeout(timeout); resolve() })
        })
      }
      lifecycle?.dispose()
      cleanupDone = true
      app.quit()
    })
})
