// 主进程桥接本地页面与后端：检查 IPC 来源、限制代理路由，并承接需原生对话框的操作。
// 资源授权和版本判断仍由后端提供，代理请求时由主进程附加鉴权令牌。
// 主要引用方包括 `main.js`。

const { ipcMain, dialog, shell, app } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const http = require("node:http");
// `main.js` 传入当前 Electron 窗口、后端地址和凭据读取器。
// 本模块集中校验 IPC 发起方，再把请求交给服务或对应原生能力。
module.exports = function install({
  getWindow,
  port,
  readToken,
  root,
  packageRoot = root,
  dataRoot = root,
  reconnectBackend,
  isQuitting = () => false,
  closeBehavior,
  displayLanguage,
  interfaceZoom,
}) {
  const management=require("./legacy-management.cjs")({getWindow,port,readToken,request});
  // 只接受主窗口主 frame 的精确本地入口，防止预览、iframe 或其他窗口借用宿主能力。
  function origin(event) {
    const win = getWindow();
    if (
      !win ||
      event.sender !== win.webContents ||
      event.senderFrame !== event.sender.mainFrame
    )
      throw Error("Invalid sender");
    const url = new URL(event.senderFrame.url);
    if (
      url.protocol !== "file:" ||
      path.resolve(
        decodeURIComponent(url.pathname)
          .replace(/^\//, "")
          .replaceAll("/", path.sep),
      ) !== path.join(__dirname, "renderer-v2", "dist", "index.html")
    )
      throw Error("Invalid renderer origin");
  }
  let driverInstall;
  ipcMain.handle("workbench:install-dokan", async event => {
    origin(event);
    if (!app.isPackaged || process.platform !== "win32") return "unavailable";
    if (!driverInstall) {
      const runtime = path.join(process.resourcesPath, "app.asar.unpacked");
      const { execFile } = require("node:child_process");
      // The renderer cannot choose a command, a package, or an installation path.
      driverInstall = new Promise(resolve => {
        execFile(path.join(runtime, "python", "python.exe"),
          ["-I", "-B", path.join(runtime, "dokan", "dokan-component.py"), "install"],
          { windowsHide: true }, error => {
            const states = { 2: "cancelled", 3: "restart", 5: "busy", 6: "invalid", 7: "newer" };
            resolve(error ? (states[error.code] || "failed") : "ready");
          });
      }).finally(() => { driverInstall = undefined; });
    }
    return driverInstall;
  });
  ipcMain.handle('workbench:close-behavior',(event,value)=>{
    origin(event);
    return closeBehavior(value);
  });
  ipcMain.handle('workbench:display-language', (event, value) => {
    origin(event);
    return displayLanguage(value);
  });
  const chromeColors={light:['#f3f1f3','#222024'],dark:['#1c1a1c','#f1eff1'],contrast:['#101010','#ffffff']};
  let chromeTheme='light';
  function updateChromeOverlay(factor) {
    if(process.platform==='darwin')return;
    const [color,symbolColor]=chromeColors[chromeTheme];
    getWindow().setTitleBarOverlay({color,symbolColor,height:Math.floor(34*factor)});
  }
  ipcMain.handle('workbench:chrome-theme',(event,theme)=>{
    origin(event);
    if(typeof theme!=='string'||!Object.hasOwn(chromeColors,theme))throw Error('Invalid chrome theme');
    chromeTheme=theme;
    updateChromeOverlay(event.sender.getZoomFactor());
    return {theme};
  });
  ipcMain.handle('workbench:interface-zoom',(event,value)=>{
    origin(event);
    const settings=interfaceZoom(value);
    if(value!==undefined){
      const factor=settings.percent/100;
      event.sender.setZoomFactor(factor);
      updateChromeOverlay(factor);
    }
    return settings;
  });
  ipcMain.handle('workbench:edit',(event,command)=>{
    origin(event);
    if (!['undo','redo','cut','copy','paste','selectAll'].includes(command)) throw Error('Invalid edit command');
    event.sender[command]();
  });
  // 代理限定在本地 API 白名单及 GET/POST，渲染页不能据此发起任意地址的 HTTP 请求。
  async function request({ path: route, method = "GET", body, requestId }) {
    if (
      typeof route !== "string" ||
      route.includes("..") ||
      route.includes("#") ||
      !/^\/api\/(v2\/|sessions(?:\/|\?)|jobs(?:\/|\?|$)|operations\/|models(?:\?|$)|policy$|state$|grants(?:\?|$)|health\?full=1$)/.test(
        route,
      )
    )
      throw Error("API route not exposed");
    if (!["GET", "POST"].includes(method)) throw Error("Invalid method");
    const shutdownReply = () => ({status:503,body:{ok:false,code:'shutting_down',error:'应用正在退出'}});
    // 退出阶段仅生命周期入口继续请求，其余调用统一返回正在退出的状态。
    if(isQuitting()&&route!=='/api/v2/lifecycle')return shutdownReply();
    // 停止、断开和生命周期操作豁免下面的写入兼容性预检，版本不兼容时仍可尝试收尾。
    const safeStop =
      /^\/api\/v2\/conversations\/threads\/[A-Za-z0-9._-]{1,100}\/previews\/[0-9a-f]{32}\/stop$/.test(
        route,
      ) ||
      route === "/api/v2/lifecycle" ||
      /^\/api\/(?:sessions\/[A-Za-z0-9._-]{1,100}|jobs\/[A-Za-z0-9._-]{1,100}|v2\/processes\/[0-9a-f]{32}|v2\/conversations\/threads\/[A-Za-z0-9._-]{1,100}|v2\/workspaces\/[^/]+\/threads\/[A-Za-z0-9._-]{1,100})\/stop$/.test(
        route,
      ) ||
      (/^\/api\/v2\/debug\/[0-9a-f]{32}$/.test(route) &&
        body?.command === "disconnect");
    // 普通写操作在发送前核对前后端及数据兼容性，防止旧界面向新数据格式写入。
    if (method === "POST" && !safeStop) {
      const health = await request({ path: "/api/health?full=1" });
      if (
        health.status !== 200 ||
        health.body.agentFirstClient !== 2 ||
        health.body.dataFormat?.compatible !== true
      )
        return {
          status: 409,
          body: {
            ok: false,
            code: "client_version",
            error:
              "前后端或数据版本不兼容，尚未发送写操作；请保留草稿并在原服务入口升级",
          },
        };
    }
    const data = method === "POST" ? JSON.stringify(body || {}) : "";
    if (Buffer.byteLength(data) > 3 * 1024 * 1024)
      throw Error("Request too large");
    return new Promise((resolve, reject) => {
      // 退出引起的连接错误归为关闭状态，其余网络/解析错误保留给调用方处理。
      const failed = error => isQuitting() ? resolve(shutdownReply()) : reject(error);
      const req = http.request(
        {
          host: "127.0.0.1",
          port,
          path: route,
          method,
          timeout: method==='POST' && body?.action==='install' && /^\/api\/v2\/plugins\/applications\/code-workroom\/languages\/(cpp|rust|python)$/.test(route)
            ? 15*60*1000 : /\/api\/v2\/plugins\/[^/]+\/(tools|connections\/[^/]+\/(oauth|github-account))$/.test(route.split('?')[0]) ? 45000 : 20000,
          headers: {
            "X-Harness-Token": readToken(),
            "X-Harness-Client-Version": "2",
            "Content-Type": "application/json",
            "Content-Length": Buffer.byteLength(data),
            ...(requestId ? { "X-Request-ID": requestId } : {}),
          },
        },
        (res) => {
          let data = "";
          // Decode the stream across chunks so a split UTF-8 character survives IPC.
          res.setEncoding("utf8");
          res.on("data", (b) => {
            data += b;
            if (data.length > 6 * 1024 * 1024)
              req.destroy(Error("Response too large"));
          });
          res.on("end", () => {
            try {
              resolve({ status: res.statusCode, body: JSON.parse(data) });
            } catch (e) {
              failed(Error("Invalid backend response"));
            }
          });
        },
      );
      req.on("error", failed);
      req.on("timeout", () => req.destroy(Error("Backend timed out")));
      req.end(data);
    });
  }
  ipcMain.handle("workbench:reconnect", (event) => {
    origin(event);
    if (!reconnectBackend) throw Error("当前入口不支持服务恢复");
    return reconnectBackend();
  });
  ipcMain.handle("workbench:request", (event, data) => {
    origin(event);
    return request(data);
  });
  const languages = require('./workroom-language.cjs')(request, dataRoot);
  ipcMain.handle('workbench:language', (event, command) => {
    origin(event);
    return languages.run(event.sender, command);
  });
  const prepareGitHub = require('./bundled-github.cjs')({request, packageRoot, dataRoot});
  ipcMain.handle('workbench:bundled-github', event => {
    origin(event);
    return prepareGitHub();
  });
  const githubViews=path.join(dataRoot,'runs','workbench','github-views');
  const browser=require('./browser-view.cjs')({getWindow,publishGitHub:(id,value)=>{
    const file=path.join(githubViews,id+'.json');
    if(value===null){try{fs.unlinkSync(file);}catch{}return;}
    const temporary=file+'.tmp';
    try {
      fs.mkdirSync(githubViews,{recursive:true});
      fs.writeFileSync(temporary,JSON.stringify(value),'utf8');
      fs.renameSync(temporary,file);
      return true;
    } catch {
      try{fs.unlinkSync(file);}catch{}
      return false;
    } finally {
      try{fs.unlinkSync(temporary);}catch{}
    }
  },record:value=>{
    const directory=process.env.HARNESS_DATA||(app.isPackaged?path.join(app.getPath('userData'),'data'):root);
    fs.mkdirSync(directory,{recursive:true});
    const file=path.join(directory,'browser-history.jsonl');
    if(fs.existsSync(file)&&fs.statSync(file).size>10*1024*1024)throw Error('浏览记录保留区已满');
    fs.appendFileSync(file,JSON.stringify({...value,at:value.at||new Date().toISOString(),actor:'human'})+'\n','utf8');
  }});
  ipcMain.handle('workbench:browser',async(event,body)=>{
    origin(event);
    if(body?.operation==='github-action'){
      const {id,owner,requestId}=body;
      if(typeof id!=='string'||typeof owner!=='string'||typeof requestId!=='string')return;
      const args={tabId:id,conversationId:owner,id:requestId};
      const route='/api/v2/github-workroom/navigation';
      const claim=await request({path:route,method:'POST',body:{...args,state:'claimed'}});
      if(claim.status!==200)return;
      let outcome='missing';
      try {
        origin(event);
        ({outcome}=await browser.command({operation:'github-navigate',id,owner,requestId,
          version:claim.body.version,url:claim.body.url}));
      } catch {}
      return (await request({path:route,method:'POST',body:{...args,state:'completed',outcome}})).body;
    }
    if(body?.operation==='github-navigate')throw Error('GitHub 网页操作须来自当前运行。');
    let lookupPreview;
    if(body?.operation==='open'&&body.preview){
      const {conversationId,previewId}=body.preview;
      if(!/^[A-Za-z0-9_-]{1,100}$/.test(conversationId)||!/^[0-9a-f]{32}$/.test(previewId))throw Error('应用来源无效');
      // 预览身份只用于定位来源，实际来源和权限从后端 authority 记录查询。
      lookupPreview=async()=>{
        const result=await request({path:`/api/v2/conversations/threads/${conversationId}/previews/${previewId}/authority`});
        if(result.status!==200)throw Error(result.body.error||'预览授权不可用');return result.body;
      };
    }
    return browser.command(body,{lookupPreview});
  });
  ipcMain.handle("workbench:open-preview", async (event, selection) => {
    origin(event);
    if (
      !selection ||
      !/^[A-Za-z0-9_-]{1,100}$/.test(selection.conversationId) ||
      !/^[0-9a-f]{32}$/.test(selection.previewId)
    )
      throw Error("Invalid application identity");
    return require("./preview-window.cjs")({
      // 由宿主转交预览下载确认动作，并带原会话及预览身份。
      download: async (operation, body = {}, id = "") => {
        const route = `/api/v2/conversations/threads/${selection.conversationId}/previews/${selection.previewId}/downloads`;
        const result = await request({path: route + (id ? `/${id}/${operation}` : ""),
          method: "POST", body: {...body, ownerPid: process.pid}});
        if (result.status !== 200) throw Error(result.body.error || "下载结果未确认");
        return result.body;
      },
      // 从宿主核对预览编号的当前状态和端口，用于打开正确的原生视图。
      lookup: async (authority = false) => {
        const value = await request({
          path: `/api/v2/conversations/threads/${selection.conversationId}/previews/${selection.previewId}${authority ? "/authority" : ""}`,
        });
        if (value.status !== 200)
          throw Error(value.body.error || "应用预览不可用");
        return value.body;
      },
    });
  });
  ipcMain.handle("workbench:choose-project", async (event,purpose) => {
    origin(event);
    const workRoot = purpose === "work-root";
    const project = purpose === "project";
    const english = displayLanguage?.().locale === "en";
    let defaultPath;
    if (project) {
      const folders = await request({ path: "/api/v2/workroom/folders" });
      if (folders.status !== 200) throw Error(folders.body.error || "无法读取工作总目录");
      defaultPath = folders.body.path;
      if (!path.isAbsolute(defaultPath)) throw Error("工作总目录无效");
      if (defaultPath === folders.body.defaultPath) await fs.promises.mkdir(defaultPath, { recursive: true });
      if (!(await fs.promises.stat(defaultPath)).isDirectory()) throw Error("工作总目录不可用");
    }
    const r = await dialog.showOpenDialog(getWindow(), {
      title: workRoot ? (english ? "Choose work folder" : "选择工作总目录") : (english ? "Open project folder" : "打开项目文件夹"),
      buttonLabel: english ? "Select folder" : "选择文件夹",
      ...(defaultPath ? {defaultPath} : {}),
      properties: ["openDirectory"],
    });
    return r.canceled ? null : r.filePaths[0];
  });
  // 导出固定本次下载的字节和源版本；用户确认期间发生变化时停止，避免悄悄换成另一份成果。
  ipcMain.handle("workbench:export-output", async (event, selection) => {
    origin(event);
    for (const key of [
      "conversationId",
      ...(selection.attachmentId
        ? ["attachmentId"]
        : ["artifactId", "versionId"]),
    ])
      if (
        typeof selection[key] !== "string" ||
        !/^[A-Za-z0-9_-]{1,100}$/.test(selection[key])
      )
        throw Error("Invalid output identity");
    const base =
      "/api/v2/conversations/threads/" +
      encodeURIComponent(selection.conversationId);
    const downloadRequest = selection.attachmentId
      ? {
          path: base + "/attachments",
          method: "POST",
          body: { download: selection.attachmentId },
        }
      : {
          path:
            base +
            "/artifacts/" +
            selection.artifactId +
            "/versions/" +
            selection.versionId +
            "?download=1",
        };
    const reply = await request(downloadRequest);
    if (reply.status !== 200)
      throw Error(reply.body.error || "Output unavailable");
    const bytes = Buffer.from(reply.body.data, "base64");
    if (bytes.length > 2 * 1024 * 1024) throw Error("Output too large");
    const crypto = require("node:crypto");
    const expected = reply.body.version?.hash || reply.body.hash;
    const hash = (data) =>
      crypto.createHash("sha256").update(data).digest("hex");
    if (hash(bytes) !== expected) throw Error("Output version changed");
    const name = path
      .basename(reply.body.fileName)
      .replace(/[<>:"/\\|?*\x00-\x1f]/g, "_");
    const selected = await dialog.showSaveDialog(getWindow(), {
      title: "导出成果或资料",
      defaultPath: name,
    });
    if (selected.canceled) return { cancelled: true };
    const target = selected.filePath;
    if (!path.isAbsolute(target)) throw Error("Invalid export target");
    if (path.extname(target).toLowerCase() !== path.extname(name).toLowerCase())
      throw Error("导出扩展名需为 " + path.extname(name));
    const parent = fs.realpathSync(path.dirname(target));
    // 按真实父目录判断目标位置，禁止导出写回内部数据区，以免覆盖应用保存的原始资料。
    const relative = path.relative(
      path.resolve(reply.body.protectedRoot),
      path.join(parent, path.basename(target)),
    );
    if (
      !relative ||
      (!relative.startsWith(".." + path.sep) &&
        relative !== ".." &&
        !path.isAbsolute(relative))
    )
      throw Error("请选择应用内部数据目录以外的导出位置");
    // 覆盖依赖内容指纹；确认后及落盘前复核，发现等待期间的内容变化就拒绝覆盖。
    function fingerprint(file) {
      const descriptor = fs.openSync(file, "r");
      try {
        const state = fs.fstatSync(descriptor);
        if (!state.isFile() || state.size > 8 * 1024 * 1024)
          throw Error("目标文件不适合覆盖");
        const digest = crypto.createHash("sha256"),
          chunk = Buffer.alloc(65536);
        let count = 0,
          size;
        while (
          (size = fs.readSync(descriptor, chunk, 0, chunk.length, null)) > 0
        ) {
          count += size;
          if (count > 8 * 1024 * 1024)
            throw Error("目标文件在读取时扩大，未覆盖");
          digest.update(chunk.subarray(0, size));
        }
        return digest.digest("hex");
      } finally {
        fs.closeSync(descriptor);
      }
    }
    let before = null,
      backup = null;
    if (fs.existsSync(target)) {
      const stat = fs.lstatSync(target);
      if (
        !stat.isFile() ||
        stat.isSymbolicLink() ||
        stat.size > 8 * 1024 * 1024
      )
        throw Error("目标不适合覆盖，请选择其他文件名");
      before = fingerprint(target);
      const answer = await dialog.showMessageBox(getWindow(), {
        type: "warning",
        buttons: ["取消", "覆盖并保留备份"],
        defaultId: 0,
        cancelId: 0,
        message: "确认覆盖 " + target,
        detail:
          "原件 SHA256 " + before + "。将先保留备份；目标变化时拒绝覆盖。",
      });
      if (answer.response !== 1) return { cancelled: true };
      if (!fs.existsSync(target) || fingerprint(target) !== before)
        throw Error("目标文件已变化，未覆盖");
    }
    // 保存对话框可能打开很久，写入前再次核对源权限和版本，不能只依赖第一次下载成功。
    const current = await request(downloadRequest);
    if (
      current.status !== 200 ||
      (current.body.version?.hash || current.body.hash) !== expected
    )
      throw Error("来源权限或版本已变化，未导出");
    if (before !== null) {
      if (!fs.existsSync(target) || fingerprint(target) !== before)
        throw Error("目标文件已变化，未覆盖");
      // 先以独立文件保留原件；备份名排他创建，避免覆盖此前保留的备份。
      backup = target + ".harness-backup-" + crypto.randomUUID();
      fs.copyFileSync(target, backup, fs.constants.COPYFILE_EXCL);
    }
    if (fs.realpathSync(path.dirname(target)) !== parent)
      throw Error("目标目录已变化");
    // 新目标排他创建；覆盖先写同目录临时文件，再核对原件并替换，finally 只清理本次临时文件。
    if (before === null) {
      fs.writeFileSync(target, bytes, { flag: "wx" });
    } else {
      const temporary = path.join(
        parent,
        ".harness-export-" + crypto.randomUUID(),
      );
      try {
        fs.writeFileSync(temporary, bytes, { flag: "wx" });
        if (fingerprint(target) !== before)
          throw Error("目标文件已变化，未覆盖");
        fs.renameSync(temporary, target);
      } finally {
        if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
      }
    }
    return { path: target, backup };
  });
  const externalEditor=require('./external-editor.cjs')({app,dialog,getWindow});
  const linkedFileAction=require('./linked-file-actions.cjs')({request,getWindow,dialog,shell,externalEditor,dataRoot});
  ipcMain.handle('workbench:linked-file-action',(event,selection)=>{
    origin(event);
    return linkedFileAction(selection);
  });
  ipcMain.handle('workbench:choose-editor',async event=>{
    origin(event);
    const executable=await externalEditor.choose();
    return executable?{name:path.basename(executable)}:{cancelled:true};
  });
  ipcMain.handle("workbench:open-ide", async (event, args) => {
    origin(event);
    const r = await request({
      path: "/api/v2/workspaces/" + encodeURIComponent(args.workspaceId),
    });
    if (r.status !== 200) throw Error("Workspace missing");
    const base = fs.realpathSync(r.body.path);
    // 先解析真实路径再核对工程归属，防止链接把 IDE 打开目标带出工作区。
    const target=()=>{
      const file=fs.realpathSync(path.resolve(base,args.path||''));
      const relative=path.relative(base,file);
      if(relative==='..'||relative.startsWith('..'+path.sep)||path.isAbsolute(relative))throw Error('Path outside workspace');
      if(!fs.statSync(file).isFile())throw Error('请选择要打开的文件');
      return file;
    };
    target();
    const executable=await externalEditor.selected();
    if(!executable)return {cancelled:true};
    // A native picker can remain open while paths change; validate again before launch.
    return externalEditor.open(executable,target(),args.line,base);
  });
  ipcMain.handle("workbench:management",(event,body)=>{origin(event);return management.command(body);});
  // 本地登录回调允许回环 HTTP；远端保持 HTTPS，且 URL 不接受内嵌凭据和片段。
  ipcMain.handle('workbench:open-oauth',(event,value)=>{
    origin(event);
    if(typeof value!=='string'||value.length>8192)throw Error('Invalid sign-in URL');
    const url=new URL(value);
    if(url.username||url.password||url.hash||!(url.protocol==='https:'||(url.protocol==='http:'&&['127.0.0.1','localhost','[::1]'].includes(url.hostname))))throw Error('Invalid sign-in URL');
    return shell.openExternal(url.href);
  });
  return { request, closeLanguages:languages.close };
};
