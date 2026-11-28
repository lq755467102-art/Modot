// 在渲染器隔离环境中公开有限的工作间桥接方法，各动作通过 IPC 交给主进程。
// 主要引用或加载入口包括 `main.js`。

const { contextBridge, ipcRenderer } = require("electron");
// 主进程 `bridge-v2.cjs` 注册固定的 IPC 操作并负责授权；这里只把渲染器参数封装到对应通道。
contextBridge.exposeInMainWorld(
  "workbench",
  Object.freeze({
    installDokan: () => ipcRenderer.invoke("workbench:install-dokan"),
    chromeTheme:theme=>ipcRenderer.invoke('workbench:chrome-theme',theme),
    interfaceZoom:percent=>ipcRenderer.invoke('workbench:interface-zoom',percent),
    closeBehavior:value=>ipcRenderer.invoke('workbench:close-behavior',value),
    displayLanguage:value=>ipcRenderer.invoke('workbench:display-language',value),
    edit:command=>ipcRenderer.invoke('workbench:edit',command),
    reconnect: () => ipcRenderer.invoke("workbench:reconnect"),
    request: (request) => ipcRenderer.invoke("workbench:request", request),
    language: command => ipcRenderer.invoke("workbench:language", command),
    onLanguageChanged: callback => {
      const listener = (_event, state) => callback(state);
      ipcRenderer.on("workbench:language-changed", listener);
      return () => ipcRenderer.removeListener("workbench:language-changed", listener);
    },
    bundledGitHub: () => ipcRenderer.invoke('workbench:bundled-github'),
    chooseProject: (purpose) => ipcRenderer.invoke("workbench:choose-project",purpose),
    chooseEditor: () => ipcRenderer.invoke('workbench:choose-editor'),
    openOAuth: url => ipcRenderer.invoke('workbench:open-oauth',url),
    openPreview: (selection) =>
      ipcRenderer.invoke("workbench:open-preview", selection),
    browser: (command) => ipcRenderer.invoke("workbench:browser",command),
    linkedFileAction: selection => ipcRenderer.invoke("workbench:linked-file-action",selection),
    openIDE: (workspaceId, path, line) =>
      ipcRenderer.invoke("workbench:open-ide", { workspaceId, path, line }),
    management: command=>ipcRenderer.invoke("workbench:management",command),
    exportOutput: (selection) =>
      ipcRenderer.invoke("workbench:export-output", selection),
  }),
);
