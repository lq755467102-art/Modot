// 准备并运行桌面发布构建，将生成物写入对应打包目录。
// 主要引用或加载入口包括 `desktop/package.json`。
const path = require("node:path");
const fs = require("node:fs");
const { spawnSync } = require("node:child_process");
const { prepare } = require("./prepare-python.cjs");
const desktop = path.resolve(__dirname, "..");
const signed = process.argv.includes("--signed");
const thumbprint = process.env.MODOT_SIGNING_THUMBPRINT || "";
if (signed && !/^[0-9a-f]{40}$/i.test(thumbprint)) throw Error("Set the intended code-signing certificate thumbprint for a public build.");
const renderer = path.join(desktop, "renderer-v2");
for (const args of [
  [path.join(renderer, "node_modules", "typescript", "bin", "tsc"), "--noEmit"],
  [path.join(renderer, "node_modules", "vite", "bin", "vite.js"), "build", "--configLoader", "runner"],
]) {
  const result = spawnSync(process.execPath, args, {
    cwd: renderer,
    stdio: "inherit",
    windowsHide: true,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status || 1);
}
const outputParent = process.env.HARNESS_BUILD_ROOT
  ? path.resolve(process.env.HARNESS_BUILD_ROOT)
  : path.join(desktop, "..", "..", "acceptance");
fs.mkdirSync(outputParent, { recursive: true });
const output = fs.mkdtempSync(
  path.join(outputParent, "release-build-"),
);
const stage = prepare(false, output);
const mode = process.argv.includes("--nsis")
  ? ["--win", "nsis", "--x64"]
  : ["--dir", "--win", "--x64"];
const args = [
  path.join(
    desktop,
    "node_modules",
    "electron-builder",
    "out",
    "cli",
    "cli.js",
  ),
  ...mode,
  "--publish", "never",
  "--config",
  path.join(desktop, "electron-builder.yml"),
  "--config.directories.app=" + stage,
  "--config.directories.output=" + output,
];
if (process.env.HARNESS_ELECTRON_DIST)
  args.push(
    "--config.electronDist=" + path.resolve(process.env.HARNESS_ELECTRON_DIST),
  );
if (signed) args.push("--config.forceCodeSigning=true", "--config.win.signtoolOptions.certificateSha1=" + thumbprint,
  "--config.win.signtoolOptions.rfc3161TimeStampServer=https://timestamp.digicert.com");
else args.push("--config.win.signtoolOptions.sign=" + path.join(__dirname, "unsigned-build.cjs"));
const result = spawnSync(process.execPath, ["--require", path.join(__dirname, "windows-package-io.cjs"), ...args], {
  cwd: desktop,
  env: {
    ...process.env,
    PATH:
      path.dirname(process.execPath) +
      path.delimiter +
      (process.env.PATH || ""),
    CSC_IDENTITY_AUTO_DISCOVERY: "false",
    HARNESS_BUILD_EXE: path.join(output, "win-unpacked", "Modot Preview.exe"),
  },
  stdio: "inherit",
  windowsHide: true,
});
fs.writeFileSync(
  path.join(output, "build-source.json"),
  JSON.stringify({ stage, exitCode: result.status }, null, 2),
);
if (signed && result.status === 0) {
  const files = [path.join(output, "win-unpacked", "Modot Preview.exe"),
    ...fs.readdirSync(output).filter(name => name.endsWith("-Setup.exe")).map(name => path.join(output, name))];
  if (files.length !== 2) throw Error("Expected both the application and installer");
  for (const file of files) {
    const verify = spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-File",
      path.join(__dirname, "verify-signature.ps1"), "-Path", file, "-Thumbprint", thumbprint],
      { stdio: "inherit", windowsHide: true });
    if (verify.status !== 0) throw Error("Publisher verification failed");
  }
}
console.log(JSON.stringify({ stage, output, exitCode: result.status }));
process.exitCode = result.status || (result.error ? 1 : 0);
