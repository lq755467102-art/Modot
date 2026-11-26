// 找一个**真能跑代码**的 Python。
//
// 为什么不能直接 spawn('python', ...)
//     Windows 上 `python` 常常是应用商店的「执行别名」：
//       C:\Users\<你>\AppData\Local\Microsoft\WindowsApps\python.exe
//     它不是真解释器 —— 跑起来要么弹商店，要么静默退出。
//     实测这台机器就是：Get-Command python 指向 WindowsApps，`python --version` 输出为空。
//     ⇒ 后果是 python 服务根本没起，而界面拿不到数据、也没有任何提示。
//
// 所以判据不是「这个命令存在」，而是「**它能执行代码并且版本够**」。
// 只查存在会选中那个空壳，而那正是最初的故障。

// 主要引用方包括 `preview.cjs` 和 `desktop/scripts/prepare-python.cjs`。

const { execFileSync } = require('child_process')
const fs = require('fs')
const path = require('path')

// 开发机上的「真实解释器在哪」，一行路径，**不进 git**（每台机器不一样）。
// 发布版不用它 —— 发布版靠随包带的 python（见下面候选里的 python/python.exe）。
const LOCAL = path.join(__dirname, 'python-path.txt')

// harness 用了 `str | None` 这类注解（PEP 604）。虽然文件都有
// `from __future__ import annotations` 兜着，但运行时拼接类型的地方仍要求 3.10+。
// 与其运行到一半报错，不如一开始就不要它。
const MIN = [3, 10]

function _run(cmd, args, code) {
  return execFileSync(cmd, [...args, '-c', code], {
    timeout: 8000,
    encoding: 'utf8',
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'ignore'],
  }).trim()
}

function probe(cmd, args) {
  // 只验两件事：能算出结果、版本够。两件都过才算数。
  try {
    if (_run(cmd, args, 'print(1+1)') !== '2') return false
    const vy = _run(cmd, args,
      `import sys;print(1 if sys.version_info >= (${MIN[0]},${MIN[1]}) else 0)`)
    return vy === '1'
  } catch {
    return false
  }
}

function findPython({packaged = false} = {}) {
  const bundled = path.join(__dirname.replace(/app\.asar$/, "app.asar.unpacked"), "python", "python.exe")
  if (packaged) {
    if (!probe(bundled, ["-I"])) return null
    return {cmd: bundled, args: ["-I"], exe: bundled}
  }
  const cands = []
  // ① 本地指定：开发机用，一行路径，不进 git
  try {
    const p = fs.readFileSync(LOCAL, 'utf8').trim()
    if (p) cands.push(p)
  } catch { /* 没有这个文件是正常的 */ }
  // ② 环境变量
  if (process.env.HARNESS_PYTHON) cands.push(process.env.HARNESS_PYTHON)
  // ③ 随包带的（打包后放这里；发布版主要靠这条）
  // Python needs adjacent DLLs and stdlib files; never execute a single EXE
  // extracted from ASAR. The whole runtime is staged in app.asar.unpacked.
  cands.push(path.join(__dirname.replace(/app\.asar$/, 'app.asar.unpacked'), 'python', 'python.exe'))
  // ④ 系统里的。注意顺序：python3 优先，`python` 在 Windows 上最容易是空壳
  cands.push('python3', 'python', 'py')

  for (const c of cands) {
    const args = c === 'py' ? ['-3'] : []
    if (!probe(c, args)) continue
    try {
      const exe = _run(c, args, 'import sys;print(sys.executable)') || c
      // 双重保险：万一它绕过上面的检查还是那个空壳，这里再挡一次。
      if (/WindowsApps/i.test(exe)) continue
      return { cmd: c, args, exe }
    } catch {
      // 拿不到真实路径就当作不可靠，试下一个
    }
  }
  return null
}

module.exports = { findPython }
