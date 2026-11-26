// 打开隔离的应用预览窗口，按预览身份查询来源并协调原生下载确认。
// 主要引用方包括 `bridge-v2.cjs`。
const { BrowserWindow, dialog, app: electronApp } = require("electron");
const crypto = require("node:crypto");
const path = require("node:path");
const fs = require("node:fs");

module.exports = async function openPreview({ lookup, download }) {
  const app = await lookup();
  if (app.state !== "ready")
    throw Error("应用尚未就绪或已经失效，请核对运行记录");
  const source = new URL(app.url);
  if (
    source.protocol !== "http:" ||
    source.hostname !== "127.0.0.1" ||
    Number(source.port) !== app.port
  )
    throw Error("预览地址与已核验端口不一致");
  const window = new BrowserWindow({
    width: 1050,
    height: 760,
    show: false,
    title: "应用预览 · " + app.revision.slice(0, 10),
    webPreferences: {
      partition: "preview-" + crypto.randomUUID(),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      nodeIntegrationInSubFrames: false,
      nodeIntegrationInWorker: false,
      webSecurity: true,
      allowRunningInsecureContent: false,
      safeDialogs: true,
      safeDialogsMessage: "阻止此页面继续弹出确认框",
      disableDialogs: false,
      webviewTag: false,
    },
  });
  window.setMenuBarVisibility(false);
  window.webContents.setWebRTCIPHandlingPolicy("disable_non_proxied_udp");
  const session = window.webContents.session;
  const allowed = (value) => {
    try {
      const url = new URL(value);
      return (
        ["http:", "ws:"].includes(url.protocol) &&
        url.hostname === "127.0.0.1" &&
        url.port === source.port
      );
    } catch {
      return false;
    }
  };
  session.setPermissionRequestHandler((_contents, _permission, callback) =>
    callback(false),
  );
  session.setPermissionCheckHandler(() => false);
  let activeDownloads = 0;
  let slot = null;
  let downloadNotice = "";
  const notice = (text) => {
    downloadNotice = text;
    if (!window.isDestroyed()) window.setTitle("应用预览 · " + text);
  };
  const reserve = async () => {
    if (!download || window.isDestroyed()) return;
    try {
      const value = await download("begin", {fileName: "download.pending"});
      if (window.isDestroyed()) await download("cancel", {}, value.download.id);
      else slot = value;
    } catch (error) { notice(error.message); }
  };
  await reserve();
  session.on("will-download", (event, item, contents) => {
    const url = item.getURL();
    const ownPage = contents === window.webContents && allowed(contents.getURL());
    const ownDownload = allowed(url) || url.startsWith("blob:" + source.origin + "/") || url.startsWith("data:");
    if (!slot || !ownPage || !ownDownload || activeDownloads) {
      event.preventDefault();
      notice("下载未接收，请核对来源或等待当前下载结束");
      return;
    }
    activeDownloads++;
    const reserved = slot;
    slot = null;
    const id = reserved.download.id, maximum = reserved.maximumBytes;
    let oversized = false;
    // Electron only permits setSavePath synchronously inside will-download.
    // The backend reserved this private path before navigation; no page path
    // is ever accepted and no native dialog can race the async registration.
    item.setSavePath(reserved.stagingPath);
    const fail = async (error) => {
      notice(String(error.message || error).slice(0, 180));
      if (id) await download("fail", {reason: String(error.message || error)}, id).catch(() => {});
    };
    item.on("updated", () => {
      if (maximum && item.getReceivedBytes() > maximum) {
        oversized = true;
        item.cancel();
      }
    });
    item.once("done", async (_e, state) => {
      try {
        if (!id) throw Error("下载尚未登记，未报告保存成功");
        if (state !== "completed") {
          if (oversized) throw Error("下载超过 25MiB，未保存不完整文件");
          await download("cancel", {}, id);
          notice("下载已取消，未保存到目标位置");
          return;
        }
        await download("seal", {fileName: path.basename(item.getFilename())}, id);
        if (window.isDestroyed()) throw Error("预览窗口已关闭，完整内容保留但尚未另存");
        const name = path.basename(item.getFilename());
        const parsed = path.parse(name);
        let target = path.join(electronApp.getPath("downloads"), name), suffix = 1;
        while (fs.existsSync(target)) {
          target = path.join(electronApp.getPath("downloads"), `${parsed.name} (${suffix++})${parsed.ext}`);
          if (suffix > 10000) throw Error("请清理下载目录后另存");
        }
        const choice = await dialog.showSaveDialog(window, {
          title: "另存下载（保留现有文件）", defaultPath: target,
          buttonLabel: "保存新文件",
        });
        if (choice.canceled || !choice.filePath) {
          await download("cancel", {}, id);
          notice("下载已取消，未保存到目标位置");
          return;
        }
        const receipt = await download("save", {destination: choice.filePath}, id);
        notice(receipt.state === "saved" ? "下载已保存并核对完整文件" : receipt.notice);
      } catch (error) {
        await fail(error);
      } finally {
        activeDownloads--;
        await reserve();
      }
    });
    if (item.getTotalBytes() > maximum) { oversized = true; item.cancel(); }
    else notice("正在接收下载，尚未保存到所选位置");
  });
  session.webRequest.onHeadersReceived((details, callback) => {
    const headers = { ...details.responseHeaders };
    const csp =
      Object.keys(headers).find(
        (key) => key.toLowerCase() === "content-security-policy",
      ) || "Content-Security-Policy";
    headers[csp] = [
      ...(headers[csp] || []),
      `default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; connect-src ${source.origin} ws://127.0.0.1:${source.port}; frame-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'self'; form-action 'self'`,
    ];
    headers["X-DNS-Prefetch-Control"] = ["off"];
    callback({ responseHeaders: headers });
  });
  session.webRequest.onBeforeRequest((details, callback) => {
    if (details.resourceType === "subFrame") return callback({ cancel: true });
    if (details.url.startsWith("data:") || details.url.startsWith("blob:"))
      return callback({ cancel: false });
    if (!allowed(details.url)) return callback({ cancel: true });
    // Check every network request as well as polling. A reused port must not
    // become an open route to an unrelated local application.
    lookup(true).then(
      (current) => {
        const cancel =
          current.state !== "ready" ||
          new URL(current.url).origin !== source.origin;
        callback({ cancel });
        if (cancel && !window.isDestroyed()) window.close();
      },
      () => {
        callback({ cancel: true });
        if (!window.isDestroyed()) window.close();
      },
    );
  });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  for (const event of ["will-navigate", "will-redirect"])
    window.webContents.on(event, (e, url) => {
      if (!allowed(url)) e.preventDefault();
    });
  window.webContents.on("will-attach-webview", (e) => e.preventDefault());
  window.webContents.on("will-frame-navigate", (details) => {
    if (!details.isMainFrame) details.preventDefault();
  });
  let checking = false;
  const timer = setInterval(async () => {
    if (checking || window.isDestroyed()) return;
    checking = true;
    try {
      const current = await lookup();
      if (current.state !== "ready") window.close();
      else
        window.setTitle(
          "应用预览 · " +
            current.revision.slice(0, 10) +
            (current.stale ? " · 代码已有变化" : "") +
            (downloadNotice ? " · " + downloadNotice : ""),
        );
    } catch {
      if (!window.isDestroyed()) window.close();
    } finally {
      checking = false;
    }
  }, 2000);
  timer.unref();
  window.on("closed", () => {
    clearInterval(timer);
    session.clearStorageData().catch(() => {});
    if (slot) { download("cancel", {}, slot.download.id).catch(() => {}); slot = null; }
  });
  try {
    await window.loadURL(app.url);
    if (process.env.HARNESS_START_HIDDEN !== "1") window.showInactive();
  } catch (error) {
    window.destroy();
    throw error;
  }
  return { previewId: app.id };
};
