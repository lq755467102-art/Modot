// Fresh staging only: no personal Python packages, .pth files or source data.
// 主要引用或加载入口包括 `build-release.cjs` 和 `desktop/package.json`。
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawnSync } = require("node:child_process");
const { findPython } = require("../find-python.cjs");
const desktop = path.resolve(__dirname, "..");
const repo = path.resolve(desktop, "..");
const videoRuntime = require("../installer/video-runtime.json");
const videoSourcesName = `Modot-${require("../package.json").version}-Video-Sources-and-Notices.zip`;

// 复制发行清单中的文件，遇到链接拒绝打包，避免发行物依赖源机器上的路径。
function copy(from, to, filter = () => true) {
  const stat = fs.lstatSync(from);
  if (stat.isSymbolicLink()) throw Error("发行文件不能是链接: " + from);
  if (!filter(from, stat)) return;
  if (stat.isDirectory()) {
    fs.mkdirSync(to, { recursive: true });
    for (const entry of fs.readdirSync(from))
      copy(path.join(from, entry), path.join(to, entry), filter);
  } else if (stat.isFile()) {
    fs.mkdirSync(path.dirname(to), { recursive: true });
    fs.copyFileSync(from, to, fs.constants.COPYFILE_EXCL);
  } else throw Error("不支持的发行文件类型: " + from);
}
// 保留运行所需的 Python 包内容，剔除测试、缓存和非目标组件。
function packageFilter(file, stat) {
  if (stat.isDirectory())
    return !["__pycache__", "tests", "test", ".git"].includes(
      path.basename(file),
    );
  return (
    !/\.(pyc|pth|env)$/i.test(file) &&
    !["RECORD", "direct_url.json"].includes(path.basename(file))
  );
}
// Ship only the optional TS/JS plugin's declaration data, package metadata and original licenses.
function stageWorkroomTypes(stage) {
  const pins = require("../package.json").devDependencies;
  for (const name of ["@types/node", "undici-types"]) {
    const source = path.join(desktop, "node_modules", name);
    const meta = JSON.parse(fs.readFileSync(path.join(source, "package.json"), "utf8"));
    if (meta.name !== name || meta.version !== pins[name] || !fs.existsSync(path.join(source, "index.d.ts")))
      throw Error("工作间语言声明与锁定版本不一致: " + name);
    const target = path.join(stage, "workroom-typescript-libs", path.basename(name));
    copy(source, target, (file, stat) =>
      stat.isDirectory() || file.endsWith(".d.ts") || ["LICENSE", "package.json"].includes(path.basename(file)));
    if (!fs.existsSync(path.join(target, "LICENSE"))) throw Error("缺少语言声明许可证: " + name);
  }
}
// 为发行完整性清单递归返回暂存区内的相对文件路径。
function files(root, prefix = "") {
  return fs
    .readdirSync(path.join(root, prefix), { withFileTypes: true })
    .flatMap((e) => {
      const p = path.join(prefix, e.name);
      return e.isDirectory() ? files(root, p) : [p];
    });
}
// 从 `find-python.cjs` 选中的解释器和发行清单构建独立运行时，校验锁定依赖与 Git 可执行后再为暂存文件写哈希清单。
function prepare(sandboxRuntimeOnly = false, releaseOutput) {
  const info = findPython();
  if (!info) throw Error("需要可运行的 CPython 3.13 x64 来准备发行运行时");
  const probe = spawnSync(
    info.exe,
    [
      "-I",
      "-c",
      "import sys,platform,json;print(json.dumps(dict(prefix=sys.base_prefix,site=__import__('sysconfig').get_path('purelib'),version=list(sys.version_info[:3]),implementation=platform.python_implementation(),machine=platform.machine(),stdlib=sorted(sys.stdlib_module_names))))",
    ],
    { encoding: "utf8", windowsHide: true },
  );
  if (probe.status !== 0) throw Error("Python来源检查失败");
  const py = JSON.parse(probe.stdout);
  if (
    py.implementation !== "CPython" ||
    py.version[0] !== 3 ||
    py.version[1] !== 13 ||
    py.machine !== "AMD64"
  )
    throw Error("当前锁定 CPython3.13 x64，其他ABI不能复用二进制依赖");
  const parent = process.env.HARNESS_BUILD_ROOT
    ? path.join(path.resolve(process.env.HARNESS_BUILD_ROOT), "stages")
    : path.join(desktop, ".release-stage");
  fs.mkdirSync(parent, { recursive: true });
  const stage = fs.mkdtempSync(path.join(parent, "build-"));
  const runtime = path.join(stage, "python");
  const source = fs.realpathSync(py.prefix);
  fs.mkdirSync(runtime);
  for (const name of [
    "python.exe",
    "python3.dll",
    "python313.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "LICENSE.txt",
  ])
    copy(path.join(source, name), path.join(runtime, name));
  const stdlib = new Set(py.stdlib);
  const supportDLLs = new Set([
    "libcrypto-3.dll",
    "libffi-8.dll",
    "libssl-3.dll",
    "sqlite3.dll",
    "zlib1.dll",
  ]);
  copy(
    path.join(source, "DLLs"),
    path.join(runtime, "DLLs"),
    (p, s) =>
      s.isDirectory() ||
      (p.endsWith(".pyd")
        ? stdlib.has(path.basename(p).split(".")[0]) &&
          !/^(_test|_tkinter)/i.test(path.basename(p))
        : supportDLLs.has(path.basename(p))),
  );
  const libRoot = path.join(source, "Lib");
  copy(
    libRoot,
    path.join(runtime, "Lib"),
    (p, s) =>
      (p === libRoot ||
        stdlib.has(
          path.relative(libRoot, p).split(path.sep)[0].replace(/\.py$/, ""),
        )) &&
      (s.isDirectory()
        ? ![
            "site-packages",
            "test",
            "tests",
            "__pycache__",
            "idlelib",
            "tkinter",
            "turtledemo",
            "ensurepip",
            "venv",
          ].includes(path.basename(p))
        : p.endsWith(".py")),
  );
  const roots = (process.env.HARNESS_WORKBENCH_DEPENDENCIES || "")
    .split(path.delimiter)
    .filter(Boolean)
    .map((p) => fs.realpathSync(p));
  roots.push(py.site, path.join(source, "Lib", "site-packages"));
  const pins = [
    ["winpty", "pywinpty-3.1.2.dist-info"],
    ["debugpy", "debugpy-1.8.22.dist-info"],
    ["PIL", "pillow-12.3.0.dist-info"],
    ["pypdf", "pypdf-6.21.4.dist-info"],
    ["openpyxl", "openpyxl-3.1.5.dist-info"],
    ["et_xmlfile", "et_xmlfile-2.0.0.dist-info"],
    ["defusedxml", "defusedxml-0.7.1.dist-info"],
    ["yaml", "pyyaml-6.0.3.dist-info"],
    ["tzdata", "tzdata-2026.6.dist-info"],
    ["dulwich", "dulwich-1.2.21.dist-info"],
    ["urllib3", "urllib3-2.8.0.dist-info"],
  ];
  if (sandboxRuntimeOnly) pins.push(["av", "av-19.0.2.dist-info", "av.libs"]);
  for (const [name, metadata, ...companions] of pins) {
    const entries = [name, metadata, ...companions];
    const root = roots.find(
      (r) => entries.every((entry) => fs.existsSync(path.join(r, entry))),
    );
    if (!root) throw Error("缺少锁定ABI依赖: " + metadata);
    for (const entry of entries)
      copy(
        path.join(root, entry),
        path.join(runtime, "Lib", "site-packages", entry),
        packageFilter,
      );
  }
  const cleanEnv = { ...process.env, PYTHONNOUSERSITE: "1" };
  for (const key of ["PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"])
    delete cleanEnv[key];
  if (!sandboxRuntimeOnly) {
    const assets = process.env.MODOT_RELEASE_ASSETS;
    if (!assets) throw Error("Set MODOT_RELEASE_ASSETS to the directory of the pinned video wheel and source archive.");
    const args = [
      "-I", "-B", path.join(__dirname, "stage-video.py"),
      path.join(desktop, "installer", "video-runtime.json"), path.resolve(assets), stage,
    ];
    if (releaseOutput) args.push(path.join(releaseOutput, videoSourcesName));
    const video = spawnSync(path.join(runtime, "python.exe"), args, {
      encoding: "utf8", windowsHide: true, env: cleanEnv,
    });
    if (video.error) throw video.error;
    if (video.status !== 0) throw Error("Video runtime staging failed: " + video.stderr);
  }
  const videoCheck = sandboxRuntimeOnly ? "import av;" :
    `import av;assert av.__version__==${JSON.stringify(videoRuntime.version)};assert av.Codec("libdav1d","r").is_decoder;assert not {"libx264","libx265"}.intersection(av.codecs_available);`;
  const check = spawnSync(
    path.join(runtime, "python.exe"),
    [
      "-I",
      "-B",
      "-c",
      videoCheck + "import sys,sqlite3,ssl,winpty,debugpy,PIL,pypdf,openpyxl,defusedxml,yaml,dulwich.client,urllib3;from zoneinfo import ZoneInfo;ZoneInfo('Asia/Hong_Kong');print(sys.prefix)",
    ],
    { encoding: "utf8", windowsHide: true, env: cleanEnv },
  );
  if (
    check.status !== 0 ||
    path.resolve(check.stdout.trim()) !== path.resolve(runtime)
  )
    throw Error("内嵌依赖自检失败: " + check.stderr);
  if (sandboxRuntimeOnly) return runtime;
  const sourceManifest = JSON.parse(
    fs.readFileSync(path.join(desktop, "release-files.json"), "utf8"),
  );
  for (const relative of sourceManifest.backend) {
    if (
      !/^(bin|loop|harness)\/[A-Za-z0-9_./-]+$/.test(relative) ||
      relative.split("/").includes("..")
    )
      throw Error("非法发行清单路径");
    copy(path.join(repo, relative), path.join(stage, "backend", relative));
  }
  for (const relative of sourceManifest.desktop)
    copy(path.join(desktop, relative), path.join(stage, relative));
  for (const seed of [".harness/policy.json", ".harness/commands/accept.md", ".harness/commands/review.md"]) {
    copy(path.join(repo, seed), path.join(stage, "backend", seed));
  }
  stageWorkroomTypes(stage);
  copy(
    path.join(desktop, "renderer-v2", "dist"),
    path.join(stage, "renderer-v2", "dist"),
  );
  const pkg = JSON.parse(
    fs.readFileSync(path.join(desktop, "package.json"), "utf8"),
  );
  delete pkg.devDependencies;
  delete pkg.scripts;
  fs.writeFileSync(
    path.join(stage, "package.json"),
    JSON.stringify(pkg, null, 2),
  );
  fs.copyFileSync(
    path.join(desktop, "THIRD_PARTY.md"),
    path.join(stage, "THIRD_PARTY.md"),
  );
  fs.copyFileSync(path.join(repo, "LICENSE"), path.join(stage, "LICENSE"));
  copy(path.join(repo, "LICENSE.zh-CN.md"), path.join(stage, "LICENSE.zh-CN.md"));
  copy(path.join(repo, "docs", "licenses", "previous-modot-mit.txt"),
    path.join(stage, "licenses", "previous-modot-mit.txt"));
  const components = [];
  const lock = JSON.parse(
    fs.readFileSync(
      path.join(desktop, "renderer-v2", "package-lock.json"),
      "utf8",
    ),
  );
  for (const [relative, value] of Object.entries(lock.packages || {})) {
    if (
      !relative.startsWith("node_modules/") ||
      value.dev ||
      relative.split("/").includes("..")
    )
      continue;
    const dir = path.join(desktop, "renderer-v2", relative);
    const metadata = JSON.parse(
      fs.readFileSync(path.join(dir, "package.json"), "utf8"),
    );
    components.push({
      name: metadata.name,
      version: metadata.version,
      license: metadata.license,
    });
    for (const file of fs.readdirSync(dir))
      if (
        /^(licen[cs]e|copying|notice)(\.|$)/i.test(file) &&
        fs.lstatSync(path.join(dir, file)).isFile()
      )
        copy(
          path.join(dir, file),
          path.join(stage, "licenses", "renderer", ...relative.split("/").filter(part => part !== "node_modules"), file),
        );
  }
  fs.mkdirSync(path.join(stage, "licenses"), { recursive: true });
  fs.writeFileSync(
    path.join(stage, "licenses", "renderer-components.json"),
    JSON.stringify(components, null, 2),
  );
  const manifest = {
    format: 1,
    created: new Date().toISOString(),
    python: py.version,
    installTimeComponents: require("../installer/runtime-assets.json"),
    bundledComponents: [{ ...videoRuntime, sources: { ...videoRuntime.sources, file: videoSourcesName } }],
    dependencies: pins.map((p) => p[1]),
    files: {},
  };
  for (const relative of files(stage))
    manifest.files[relative.replaceAll(path.sep, "/")] = crypto
      .createHash("sha256")
      .update(fs.readFileSync(path.join(stage, relative)))
      .digest("hex");
  fs.writeFileSync(
    path.join(stage, "release-manifest.json"),
    JSON.stringify(manifest, null, 2),
  );
  fs.writeFileSync(
    path.join(parent, "latest.json"),
    JSON.stringify({ stage }, null, 2),
  );
  return stage;
}
module.exports = { prepare, stageWorkroomTypes };
if (require.main === module) console.log(prepare(process.argv.includes("--sandbox-runtime")));
