// Older libuv cannot create its child stdio pipes in a Windows AppContainer.
// Use Node's own test runner in a fresh process per explicitly named file.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
// Windows rewrites TEMP/TMP on entry to an AppContainer. Its assigned
// scratch already has the permissions needed by os.tmpdir() and child tools.
process.env.TEMP = process.env.TMP = path.resolve(__dirname, '..', 'scratch');
const [major, minor] = process.versions.uv.split('.').map(Number);
const flags = process.execArgv;
if (major === 1 && minor < 53 && flags.includes('--test') &&
    !flags.some(flag => /^--(?:experimental-)?test-isolation=none$/.test(flag)) &&
    process.allowedNodeEnvironmentFlags.has('--experimental-test-isolation')) {
  const files = process.argv.slice(1);
  if (flags.length === 1 && files.length && files.every(file =>
    !file.startsWith('-') && fs.statSync(file, {throwIfNoEntry: false})?.isFile())) {
    let failed = false;
    for (const file of files) {
      // The Windows test runner also treats backslashes in absolute file
      // arguments as glob escapes. Use a portable spelling of the same file.
      const testPath = path.relative(process.cwd(), path.resolve(file)).split(path.sep).join('/');
      const child = spawnSync(process.execPath,
        ['--test', '--experimental-test-isolation=none', '--', testPath], {stdio: 'inherit'});
      if (child.error) process.stderr.write(child.error.message + '\n');
      if (child.status !== 0) failed = true;
    }
    process.exit(failed ? 1 : 0);
  }
  // Discovery, watch, sharding and reporter options have native semantics.
  // Return an environment error instead of dropping options or waiting for a pipe.
  process.stderr.write('Node AppContainer: this libuv cannot start test workers with pipes. ' +
    'For the default runner, pass explicit test files. Requested options were not run.\n');
  process.exit(2);
}
