// Build-process only. Electron-builder sometimes gets a Windows UNKNOWN while
// opening its freshly copied executable to write ASAR integrity. Retrying that
// exact open leaves integrity validation intact; other errors/files fail fast.
// 主要引用或加载入口包括 `build-release.cjs`。
const fs = require('node:fs');
const path = require('node:path');
const target = process.env.HARNESS_BUILD_EXE;
if (process.platform === 'win32' && target && path.isAbsolute(target)) {
  const expected = path.resolve(target).toLowerCase();
  const write = fs.promises.writeFile.bind(fs.promises);
  fs.promises.writeFile = async function(file, data, ...options) {
    const ownExecutable = typeof file === 'string'
      && path.resolve(file).toLowerCase() === expected;
    let attempt = 0;
    while (true) {
      try { return await write(file, data, ...options); }
      catch (error) {
        if (!ownExecutable || error.code !== 'UNKNOWN' || error.syscall !== 'open' || attempt >= 6)
          throw error;
        const delay = Math.min(2000, 100 * 2 ** attempt++);
        process.stderr.write(`[package-io] retry fresh executable open ${attempt}/6 after ${delay}ms\n`);
        await new Promise(resolve => setTimeout(resolve, delay));
      }
    }
  };
}
