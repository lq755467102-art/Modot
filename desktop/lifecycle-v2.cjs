// The Electron main process and its backend remain alive in background mode.
// A renderer reload or hidden window never claims to restart a PTY or debugger.
// 主要引用方包括 `main.js`。

const { app, Tray, Menu, nativeImage } = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const zoomPercentages = [80, 90, 100, 110, 120, 130, 140, 150];
const defaultZoom = 100;
const ICON =
  "iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAANklEQVR4nGMwTZ71Hxv+8O0/HONSQ4w6hlELRi0YtYA4C4jFoxbQzoKhn4pGLRi1YNQCyiwAAHsjEt+XuQlzAAAAAElFTkSuQmCC";
// 由 `main.js` 持有整个 Electron 生命周期，窗口关闭行为按本用户数据目录持久化。
module.exports = function lifecycle({
  getWindow,
  isQuitting,
  quit,
}) {
  const preference = path.join(app.getPath("userData"), "window-close.json");
  const languageFile = path.join(app.getPath("userData"), "display-language.json");
  const zoomFile = path.join(app.getPath("userData"), "interface-zoom.json");
  const languageOptions = ["system", "zh-CN", "en"];
  let language = "system";
  try {
    const saved = JSON.parse(fs.readFileSync(languageFile, "utf8"));
    if (languageOptions.includes(saved)) language = saved;
  } catch (error) {
    if (error.code !== "ENOENT") console.warn("Could not read display language:", error.message);
  }
  let tray = null;
  let behavior = "background";
  try {
    if (JSON.parse(fs.readFileSync(preference, "utf8")) === "quit") behavior = "quit";
  } catch {}
  let zoom = defaultZoom;
  try {
    const saved = JSON.parse(fs.readFileSync(zoomFile, "utf8"));
    if (zoomPercentages.includes(saved)) zoom = saved;
  } catch (error) {
    if (error.code !== "ENOENT") console.warn("Could not read interface zoom:", error.message);
  }
  // 未给参数时只读当前设置。变更先安全替换偏好文件，再改变内存行为。
  function closeBehavior(value) {
    if (value === undefined) return behavior;
    if (value !== "background" && value !== "quit") throw Error("无效的窗口关闭方式");
    if (value === behavior) return behavior;
    fs.mkdirSync(path.dirname(preference), { recursive: true });
    const temporary = preference + ".tmp";
    fs.writeFileSync(temporary, JSON.stringify(value));
    fs.renameSync(temporary, preference);
    behavior = value;
    return behavior;
  }
  function displayLanguage(value) {
    if (value !== undefined && !languageOptions.includes(value)) throw Error("Invalid display language");
    if (value !== undefined && value !== language) {
      fs.mkdirSync(path.dirname(languageFile), { recursive: true });
      fs.writeFileSync(languageFile + ".tmp", JSON.stringify(value));
      fs.renameSync(languageFile + ".tmp", languageFile);
      language = value;
    }
    const supported = app.getPreferredSystemLanguages().find(tag => /^(?:en|zh)(?:-|$)/i.test(tag));
    const locale = language === "system" ? (/^zh/i.test(supported || "") ? "zh-CN" : "en") : language;
    if (tray && trayLocale !== locale) updateTray(locale);
    return { preference: language, locale };
  }
  function interfaceZoom(value) {
    if (value !== undefined && !zoomPercentages.includes(value)) throw Error("Invalid interface zoom");
    if (value !== undefined && value !== zoom) {
      fs.mkdirSync(path.dirname(zoomFile), { recursive: true });
      fs.writeFileSync(zoomFile + ".tmp", JSON.stringify(value));
      fs.renameSync(zoomFile + ".tmp", zoomFile);
      zoom = value;
    }
    return { percent: zoom, percentages: zoomPercentages, defaultPercent: defaultZoom };
  }
  let trayLocale;
  function updateTray(locale) {
    const en = locale === "en";
    tray.setToolTip(en ? "Modot · Running in the background" : "Modot · 后台运行");
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: en ? "Open Modot" : "打开 Modot", click: show },
      { label: en ? "Quit Modot" : "结束并退出", click: quit },
    ]));
    trayLocale = locale;
  }
  // 托盘点击时恢复原窗口，保留已有的会话与工作间状态。
  function show() {
    const w = getWindow();
    if (w && !w.isDestroyed()) {
      if (w.isMinimized()) w.restore();
      w.show();
      w.focus();
    }
  }
  // 首次隐藏时建立托盘入口，后端继续运行并保留原窗口。
  function background() {
    if (!tray) {
      const icon = nativeImage.createFromPath(path.join(__dirname, "icons", "modot.png"));
      tray = new Tray(icon.isEmpty() ? nativeImage.createFromBuffer(Buffer.from(ICON, "base64")) : icon.resize({ width: 24 }));
      tray.on("double-click", show);
    }
    displayLanguage();
    getWindow()?.hide();
  }
  return {
    displayLanguage,
    interfaceZoom,
    closeBehavior,
    show,
    // 拦截普通关闭并按偏好后台驻留或退出；应用正在退出和隔离测试走原生关闭。
    attach(win) {
      win.on("close", (e) => {
        if (isQuitting() || process.env.HARNESS_TEST_ROOT) return;
        e.preventDefault();
        if (behavior === "background") {
          try { background(); }
          catch (error) { console.error("保留后台窗口失败：", error.message); }
        } else quit();
      });
    },
    // 主进程清理时释放原生托盘句柄。
    dispose() {
      tray?.destroy();
      tray = null;
    },
  };
};
