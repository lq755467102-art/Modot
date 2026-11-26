// 启动器。存在的唯一理由是：宿主环境里可能有 ELECTRON_RUN_AS_NODE=1，
// 它会让 electron.exe 退化成 node 去跑 main.js —— 于是 require('electron')
// 拿到的是一个**路径字符串**而不是 API，报 `Cannot read properties of undefined
// (reading 'whenReady')`，而这个错看起来像代码写错了。
// （实测：这台机器上 ELECTRON_RUN_AS_NODE=1，electron --version 返回的是 node 的版本。）
//
// 所以启动前必须把这个变量删掉。用 node 包一层，比写 .bat 好：
// 跨平台，而且 `npm start` 直接可用。

// 主要引用或加载入口包括 `package.json`。

delete process.env.ELECTRON_RUN_AS_NODE

const { spawn } = require('child_process')
const path = require('path')

// 在 node 环境里 require('electron') 返回的是可执行文件路径（见 electron/index.js）
const exe = require('electron')

const p = spawn(exe, [path.join(__dirname, '.')], {
  cwd: __dirname,
  stdio: 'inherit',
  env: process.env,
})
p.on('exit', code => process.exit(code == null ? 0 : code))
