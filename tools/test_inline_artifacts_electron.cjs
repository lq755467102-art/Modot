// Offscreen regression for retained outputs referenced inside real conversation replies.
const assert = require("node:assert/strict");
const {execFileSync} = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const root = path.resolve(__dirname, "..");

if (process.type === "browser") {
  guardWindow(process.env.MODOT_INLINE_GUARD);
  require("../desktop/main.js");
} else {
  run().catch(error => { console.error(error.stack); process.exitCode = 1; });
}

async function run() {
  const { Driver, wait } = require("./electron_cdp.cjs");
  const out = fs.mkdtempSync(path.join(process.env.MODOT_INLINE_TEST_OUTPUT || os.tmpdir(), "inline-artifact-"));
  const guard = path.join(out, "offscreen.json");
  const report = { passed: false, checks: [] };
  let reply = "";
  let deliveredFlowchartGuide = false;
  const provider = http.createServer(async (req, res) => {
    if (req.method === "GET") {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ data: [{ id: "inline-fixture" }] }));
      return;
    }
    let input = "";
    for await (const chunk of req) input += chunk;
    const messages = JSON.parse(input).messages ?? [];
    if (messages.some(message => message.role === "system" &&
        String(message.content).includes("flowchart TD") && String(message.content).includes("Markdown 的 ```mermaid"))) {
      deliveredFlowchartGuide = true;
    }
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    res.end("data: " + JSON.stringify({
      id: "inline-fixture",
      choices: [{ index: 0, delta: { content: reply }, finish_reason: "stop" }],
    }) + "\n\ndata: [DONE]\n\n");
  });
  await new Promise(resolve => provider.listen(0, "127.0.0.1", resolve));
  const driver = new Driver({
    exe: require(path.join(root, "desktop/node_modules/electron")),
    out,
    entry: __filename,
    env: {
      HARNESS_DATA: path.join(out, "data"),
      HARNESS_PYTHON: process.env.HARNESS_PYTHON,
      HARNESS_GIT: process.env.HARNESS_GIT,
      HARNESS_SANDBOX_PYTHON_ROOT: process.env.HARNESS_SANDBOX_PYTHON_ROOT,
      HARNESS_BASE_URL: "http://127.0.0.1:" + provider.address().port + "/v1",
      HARNESS_API_KEY: "local-mock-placeholder",
      HARNESS_MODEL: "inline-fixture",
      MODOT_INLINE_GUARD: guard,
      PYTHONUTF8: "1",
      PYTHONDONTWRITEBYTECODE: "1",
    },
  });
  const threads = "/api/v2/conversations/threads";
  const q = JSON.stringify;
  const evaluate = expression => driver.evaluate(expression);
  const node = selector => "document.querySelector(" + q(selector) + ")";
  const feed = node("#feed");
  const select = async id => {
    const row = node("[data-conversation-id=" + q(id) + "] > button");
    await wait(() => evaluate("!!" + row), 10000);
    await evaluate(row + ".click()");
  };
  async function api(route, body, status = 200) {
    const result = await driver.request(route, body);
    assert.equal(result.status, status, route + " " + (result.body?.code || ""));
    return result.body;
  }
  async function check(name, operation) {
    try {
      const detail = await operation();
      report.checks.push({ name, passed: true, detail });
    } catch (error) {
      report.checks.push({ name, passed: false, error: error.message });
      throw error;
    }
  }
  const scrollToLabel = label => evaluate("Array.from(document.querySelectorAll('#feed .message-reading p'))" +
    ".find(e=>e.textContent.includes(" + q(label) + "))?.scrollIntoView({block:'center'})");
  async function loadImage(label) {
    const img = node("#feed .message-reading img[alt=" + q(label) + "]");
    await scrollToLabel(label);
    await wait(() => evaluate("(()=>{const e=" + img + ";return e?.complete && e.naturalWidth>0})()"), 5000);
    return img;
  }
  async function send(conversation, text, content, marker) {
    reply = content + "\n\n" + marker;
    await api(conversation + "/send", {text});
    await wait(() => evaluate(feed + "?.textContent.includes(" + q(marker) + ")"), 30000);
    const id = conversation.split("/").at(-1);
    await wait(async () => (await api(threads)).threads.some(item => item.id === id && !item.runtime?.active), 30000);
  }

  try {
    await driver.start();
    await evaluate("window.workbench.displayLanguage('zh-CN')");
    const thread = await api(threads, { title: "Inline artifact fixture" });
    const other = await api(threads, { title: "Separate conversation fixture" });
    const conversation = threads + "/" + thread.id;
    const otherConversation = threads + "/" + other.id;
    const png = fs.readFileSync(process.env.MODOT_INLINE_FIXTURE_PNG || path.join(root, "desktop/icons/modot.png"));
    const jpegFile = path.join(out, "fixture.jpg");
    execFileSync(process.env.HARNESS_PYTHON || "python", ["-c",
      "from PIL import Image; import sys; Image.new('RGB',(48,32),'#6d91cd').save(sys.argv[1])", jpegFile],
      {windowsHide:true});
    const jpg = fs.readFileSync(jpegFile);
    const attachment = await api(conversation + "/attachments", { name: "diagram.png", data: png.toString("base64") });
    const jpegAttachment = await api(conversation + "/attachments", { name: "second.jpg", data: jpg.toString("base64") });
    await api(conversation + "/scope", { resourceIds: [attachment.id, jpegAttachment.id] });
    const image = await api(conversation + "/artifacts", { sourceAttachmentId: attachment.id, title: "result.png" });
    const jpeg = await api(conversation + "/artifacts", { sourceAttachmentId: jpegAttachment.id, title: "second.jpg" });
    const note = "Pinned output from the later text version.";
    const correction = await api(conversation + "/artifacts", {
      artifactId:image.id, expectedVersion:image.version.id, title:"readme.txt", format:"text", text:note,
    });
    const reference = "retained-output:" + image.id;
    const alt = "工作台助手运作示意图";
    const jpegLabel = "另一张图片";
    await evaluate("location.reload()");
    await select(thread.id);
    await send(conversation, "Show the retained diagram.", [
      "![" + alt + "](" + reference + ")",
      "![" + jpegLabel + "](retained-output:" + jpeg.id + "/" + jpeg.version.id + ")",
      "[查看说明文件](" + reference + "/" + correction.version.id + ")",
      "![外网地址](https://example.invalid/third-party.png)",
      "![直接数据地址](data:image/png;base64,AAAA)",
      "![本地文件链接](diagram.png)",
      "<img alt='原始 HTML 图片' src='https://example.invalid/raw.png'>",
    ].join("\n\n"), "INLINE_PICTURE_COMPLETE");
    await check("The saved PNG renders at the Markdown image reference", async () => {
      const img = await loadImage(alt).catch(async () => {
        const placeholder = await evaluate(feed + "?.textContent.includes(" + q("[图片：" + alt + "]") + ")");
        throw Error(placeholder ? "The original Markdown image became the screenshot's text placeholder." : "The expected inline image was absent or did not decode.");
      });
      assert.equal(await evaluate(img + ".getAttribute('src')===" + q("data:image/png;base64," + png.toString("base64"))), true,
        "The unversioned reference must retain the original PNG after a correction");
      return await evaluate("(()=>{const e=" + img + ";return {width:e.naturalWidth,height:e.naturalHeight}})()");
    });
    await check("The PNG opens its original version in a reusable right-hand image tab", async () => {
      const open = await loadImage(alt);
      const details = node(".image-artifact-tab .image-viewport img");
      const currentTab = node(".conversation-side-tabs nav [aria-current=page]");
      await evaluate(open + ".closest('button').click()");
      await wait(() => evaluate("(()=>{const e=" + details + ";return e?.complete&&e.naturalWidth>0})()"), 10000);
      assert.equal(await evaluate(details + ".getAttribute('src')===" + q("data:image/png;base64," + png.toString("base64"))), true);
      assert.equal(await evaluate(currentTab + ".textContent"), alt);
      assert.equal(await evaluate(node(".application-shell") + ".dataset.settingsOpen"), "false");
      const zoom = "Array.from(document.querySelectorAll('.image-artifact-tab .image-view-controls button')).find(e=>e.textContent==='100%')";
      await wait(() => evaluate("!(" + zoom + ")?.disabled"), 5000);
      await evaluate("(" + zoom + ").click()");
      await wait(() => evaluate(details + ".style.width===" + details + ".naturalWidth+'px'"), 5000);

      await evaluate(node(".side-toolbar button[aria-label='新标签页']") + ".click()");
      await wait(() => evaluate(currentTab + "?.textContent==='新标签页'"), 5000);
      await evaluate(open + ".closest('button').click()");
      await wait(() => evaluate(currentTab + "?.textContent===" + q(alt)), 5000);
      assert.equal(await evaluate("Array.from(document.querySelectorAll('.conversation-side-tabs nav button')).filter(e=>e.textContent===" +
        q(alt) + ").length"), 1);
    });
    await check("An explicit JPEG version renders using the stored bytes", async () => {
      const img = await loadImage(jpegLabel);
      assert.equal(await evaluate(img + ".getAttribute('src')===" + q("data:image/jpeg;base64," + jpg.toString("base64"))), true);
    });
    await check("Raw HTML, remote images and unsourced data URLs stay blocked. Local paths stay clickable.", async () => {
      for (const label of ["外网地址", "直接数据地址", "原始 HTML 图片", "本地文件链接"]) {
        assert.equal(await evaluate("!!" + node("#feed .message-reading img[alt=" + q(label) + "]")), false);
      }
      assert.equal(await evaluate("Array.from(document.querySelectorAll('#feed .message-reading a.message-resource-link')).some(a=>a.textContent==='本地文件链接')"), true);
    });
    await check("Clicking a text output opens exactly the referenced version in the existing file panel", async () => {
      const button = "Array.from(document.querySelectorAll('#feed .message-artifact-open')).find(e=>e.textContent==='查看说明文件')";
      await scrollToLabel("查看说明文件");
      await wait(() => evaluate("!!(" + button + ")"), 10000);
      await evaluate("(" + button + ").click()");
      await wait(() => evaluate(node(".artifact-pane [aria-label='成果版本']") + "?.value===" + q(correction.version.id) +
        " && " + node(".artifact-pane") + "?.textContent.includes(" + q(note) + ")"), 10000);
      await driver.page.call("Input.dispatchKeyEvent", {type:"keyDown",key:"Escape",code:"Escape",windowsVirtualKeyCode:27});
      await driver.page.call("Input.dispatchKeyEvent", {type:"keyUp",key:"Escape",code:"Escape",windowsVirtualKeyCode:27});
    });
    await check("Revoking and restoring output reading removes and restores the inline image and its tab", async () => {
      const permission = conversation + "/artifacts/" + image.id + "/permission";
      await api(permission, {permission:"none"});
      const img = node("#feed img[alt=" + q(alt) + "]");
      await wait(() => evaluate("!" + img + " && " + feed + "?.textContent.includes('成果不可用')"), 7000);
      await wait(() => evaluate("!" + node(".image-artifact-tab img") + " && " +
        node(".image-artifact-tab") + "?.textContent.includes('成果不可用')"), 7000);
      await api(permission, {permission:"read", confirm:true});
      await loadImage(alt);
      await wait(() => evaluate(node(".image-artifact-tab img") + "?.naturalWidth>0"), 7000);
    });
    await check("A previous chat's image reference is unavailable in a different chat", async () => {
      await select(other.id);
      await send(otherConversation, "Try the other chat's URI.", "![" + alt + "](" + reference + ")", "OTHER_PICTURE_COMPLETE");
      await scrollToLabel(alt);
      await wait(() => evaluate(feed + "?.textContent.includes('成果不可用')"), 7000);
      assert.equal(await evaluate("!!" + node("#feed img[alt=" + q(alt) + "]")), false);
      assert.equal(await evaluate("!!" + node(".image-artifact-tab")), false);
      await select(thread.id);
      await loadImage(alt);
    });
    await check("A previously saved reply can display its original artifact after reload", async () => {
      await evaluate("location.reload()");
      await select(thread.id);
      await wait(() => evaluate(feed + "?.textContent.includes(" + q(alt) + ")"), 10000);
      await loadImage(alt);
    });
    const block = lines => ["```mermaid", ...lines, "```"].join("\n");
    await driver.page.call("Emulation.setDeviceMetricsOverride",{width:1000,height:680,deviceScaleFactor:1,mobile:false});
    const stages = Array.from({length:24},(_,i) => 'S'+i+'["执行阶段 '+i+'"] --> S'+(i+1));
    await send(conversation, "Use a flowchart to explain these branches.", [
      block(["flowchart TD", 'A["接收请求"] --> B{"已经批准？"}', 'B -->|是| C["完成"]', 'B -->|否| D["等待"]',
        "C --> S0", ...stages]),
      block(["flowchart TD", "A --> ["]),
      block(["flowchart TD", "A --> B", 'click A "https://example.invalid/diagram-link"']),
    ].join("\n\n"), "FLOWCHART_REPLY_COMPLETE");
    await check("Native L0 receives the flowchart formatting instruction", async () => {
      assert.equal(deliveredFlowchartGuide, true);
    });
    await check("The Mermaid block becomes a readable flowchart with its source available", async () => {
      const chart = node("#feed .message-flowchart");
      await evaluate(chart + "?.scrollIntoView({block:'center'})");
      await wait(() => evaluate(chart + "?.querySelector('img')?.naturalWidth>0"), 15000);
      assert.equal(await evaluate("decodeURIComponent(" + chart + ".querySelector('img').src).includes('接收请求')"), true);
      const summary = chart + ".querySelector('summary')";
      await evaluate(summary + ".click()");
      await wait(() => evaluate(chart + ".querySelector('details').open && " + chart +
        ".querySelector('code').textContent.includes('flowchart TD')"), 5000);
    });
    await check("A tall flowchart uses the conversation scroll and opens a zoomable preview", async () => {
      const chart = node("#feed .message-flowchart");
      await evaluate(chart + ".scrollIntoView({block:'center'})");
      const geometry = await evaluate("(() => {const view="+chart+".querySelector('.message-flowchart-view');"+
        "const img=view.querySelector('img');const feed=document.querySelector('#feed');"+
        "return {width:img.width,height:img.height,naturalWidth:img.naturalWidth,naturalHeight:img.naturalHeight,"+
        "feedHeight:feed.clientHeight,viewWidth:view.clientWidth,scrollWidth:view.scrollWidth};})()");
      assert.ok(geometry.height > geometry.feedHeight, "The conversation must scroll through the tall diagram");
      assert.ok(geometry.scrollWidth <= geometry.viewWidth + 1, "The diagram must stay within the message");
      assert.ok(Math.abs(geometry.width - Math.min(geometry.naturalWidth, geometry.viewWidth)) <= 2,
        "The diagram must use the available width without enlarging a smaller original");
      assert.ok(Math.abs(geometry.width / geometry.naturalWidth - geometry.height / geometry.naturalHeight) < 0.01);
      await evaluate(chart + ".querySelector('.message-flowchart-view').click()");
      await wait(() => evaluate("!!document.querySelector('.message-flowchart-dialog[open] .image-viewport img')?.naturalWidth"),10000);
      const original = "Array.from(document.querySelectorAll('.message-flowchart-dialog .image-view-controls button')).find(e=>e.textContent==='100%')";
      await wait(() => evaluate("!(" + original + ")?.disabled"),5000);
      await evaluate("(" + original + ").click()");
      const view = "document.querySelector('.message-flowchart-dialog .image-viewport img')";
      await wait(() => evaluate(view+".style.height==="+view+".naturalHeight+'px'"),5000);
      await driver.page.call("Input.dispatchKeyEvent", {type:"keyDown",key:"Escape",code:"Escape",windowsVirtualKeyCode:27});
      await driver.page.call("Input.dispatchKeyEvent", {type:"keyUp",key:"Escape",code:"Escape",windowsVirtualKeyCode:27});
      await wait(() => evaluate("!document.querySelector('.message-flowchart-dialog[open]')"),5000);
    });
    await check("Invalid and interactive flowcharts keep readable source without displaying active content", async () => {
      const invalid = "Array.from(document.querySelectorAll('#feed .message-flowchart')).slice(1)";
      await evaluate(invalid + "[0]?.scrollIntoView({block:'center'})");
      await wait(() => evaluate(invalid + ".length===2 && " + invalid +
        ".every(e=>e.dataset.state==='source' && !e.querySelector('img') && e.querySelector('code'))"), 10000);
    });
    report.passed = true;
  } finally {
    await driver.abort();
    await new Promise(resolve => provider.close(resolve));
    if (fs.existsSync(guard)) report.offscreen = JSON.parse(fs.readFileSync(guard, "utf8"));
    report.passed = report.passed && !!report.offscreen?.windows.length &&
      report.offscreen.windows.every(win => !win.visible && win.offscreen) &&
      !report.offscreen.displayAttempts.length && !report.offscreen.visibleEvents && !report.offscreen.dialogs.length;
    if (!report.passed) process.exitCode = 1;
    const result = path.join(out, "result.json");
    fs.writeFileSync(result, JSON.stringify(report, null, 2));
    console.log(result);
  }
}

function guardWindow(reportPath) {
  const Module = require("node:module");
  const { app, BrowserWindow, dialog } = require("electron");
  const report = { electron: process.versions.electron, windows: [], visibleEvents: 0, displayAttempts: [], dialogs: [] };
  for (const method of ["show", "showInactive"]) {
    BrowserWindow.prototype[method] = function () {
      report.displayAttempts.push(method);
      throw Error("A test window tried to become visible");
    };
  }
  for (const method of ["showOpenDialog", "showOpenDialogSync", "showSaveDialog", "showSaveDialogSync", "showMessageBox", "showMessageBoxSync", "showErrorBox"]) {
    dialog[method] = function () {
      report.dialogs.push(method);
      throw Error("A test tried to open a native dialog");
    };
  }
  app.on("browser-window-created", (_event, win) => {
    report.windows.push({ visible: win.isVisible(), offscreen: win.webContents.isOffscreen() });
    win.on("show", () => { report.visibleEvents++; win.hide(); });
  });
  const main = path.join(root, "desktop/main.js");
  const compile = Module.prototype._compile;
  Module.prototype._compile = function (source, filename) {
    if (filename === main) {
      const marker = "webPreferences: {";
      if (!source.includes(marker)) throw Error("Could not require offscreen rendering");
      source = source.replace(marker, marker + "\n      offscreen: true,");
    }
    return compile.call(this, source, filename);
  };
  const save = () => fs.writeFileSync(reportPath, JSON.stringify(report));
  app.on("will-quit", save);
  process.on("exit", save);
}
