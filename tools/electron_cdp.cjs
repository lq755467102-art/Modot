// Test-host CDP only. Never moves the operating-system mouse or keyboard.
const fs = require("node:fs");
const path = require("node:path");
const cp = require("node:child_process");
const net = require("node:net");
const { once } = require("node:events");
const crypto = require("node:crypto");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function wait(fn, timeout = 60000) {
  const end = Date.now() + timeout;
  let error;
  while (Date.now() < end) {
    try {
      const value = await fn();
      if (value) return value;
    } catch (e) {
      error = e;
    }
    await sleep(25);
  }
  throw error || Error("Timed out: " + String(fn).slice(0, 200));
}
async function ports() {
  const servers = [];
  for (let i = 0; i < 2; i++) {
    const s = net.createServer();
    s.listen(0, "127.0.0.1");
    await once(s, "listening");
    servers.push(s);
  }
  const result = servers.map((s) => s.address().port);
  await Promise.all(
    servers.map((s) => new Promise((resolve) => s.close(resolve))),
  );
  return result;
}
async function connect(url) {
  const socket = new WebSocket(url);
  await once(socket, "open");
  let seq = 0;
  const pending = new Map(),
    events = [];
  socket.onmessage = (e) => {
    const m = JSON.parse(e.data),
      p = pending.get(m.id);
    if (!p) {
      if (m.method) {
        events.push(m);
        if (events.length > 1000) events.shift();
      }
      return;
    }
    pending.delete(m.id);
    clearTimeout(p.timer);
    if (m.error) p.reject(Error(m.error.message));
    else p.resolve(m.result);
  };
  socket.onclose = () => {
    for (const p of pending.values()) {
      clearTimeout(p.timer);
      p.reject(Error("CDP closed"));
    }
    pending.clear();
  };
  return {
    socket,
    events,
    call(method, params = {}) {
      const id = ++seq;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(Error("CDP timeout: " + method));
        }, 30000);
        pending.set(id, { resolve, reject, timer });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
  };
}
class Driver {
  constructor({ exe, out, profile, env = {}, entry }) {
    this.exe = path.resolve(exe);
    this.out = out;
    this.profile = profile || path.join(out, "profile");
    this.overrides = env;
    this.entry = entry;
  }
  async start() {
    fs.mkdirSync(this.out, { recursive: true });
    [this.port, this.cdpPort] = await ports();
    const env = Object.fromEntries(
      Object.entries(process.env).filter(
        ([k]) =>
          !k.startsWith("HARNESS_") &&
          !k.startsWith("PYTHON") &&
          !["ELECTRON_RUN_AS_NODE", "NODE_OPTIONS"].includes(k),
      ),
    );
    Object.assign(env, {
      HARNESS_USER_DATA: this.profile,
      HARNESS_TEST_ROOT: this.out,
      HARNESS_START_HIDDEN: "1",
      HARNESS_PORT: String(this.port),
      HARNESS_CREDS: path.join(this.profile, "no-credentials.env"),
      HARNESS_MODELS: path.join(this.profile, "models.json"),
      HARNESS_HABITS: path.join(this.profile, "owned-habits.md"),
      HARNESS_HABITS_INDEX: path.join(this.profile, "owned-habits.json"),
      HARNESS_HOOKS: path.join(this.profile, "no-hooks.json"),
      HARNESS_BASELINE_HITS: path.join(this.profile, "hits.jsonl"),
      ...this.overrides,
    });
    const stdout = fs.openSync(path.join(this.out, "main.stdout.log"), "a"),
      stderr = fs.openSync(path.join(this.out, "main.stderr.log"), "a");
    this.started = performance.now();
    try {
      this.child = cp.spawn(
        this.exe,
        [
          ...(this.entry ? [path.resolve(this.entry)] : []),
          "--remote-debugging-port=" + this.cdpPort,
          "--remote-debugging-address=127.0.0.1",
          "--enable-precise-memory-info",
        ],
        { env, windowsHide: true, stdio: ["ignore", stdout, stderr] },
      );
    } finally {
      fs.closeSync(stdout);
      fs.closeSync(stderr);
    }
    const rows = await wait(async () => {
      if (this.child.exitCode !== null)
        throw Error("Packaged app exited before readiness");
      const r = await (
        await fetch("http://127.0.0.1:" + this.cdpPort + "/json")
      ).json();
      return r.some((t) => t.url.includes("/renderer-v2/dist/index.html"))
        ? r
        : null;
    });
    this.targetMs = performance.now() - this.started;
    this.target = rows.find((t) =>
      t.url.includes("/renderer-v2/dist/index.html"),
    );
    this.page = await connect(this.target.webSocketDebuggerUrl);
    await this.page.call("Runtime.enable");
    await this.page.call("Performance.enable");
    await wait(() =>
      this.evaluate("!!document.querySelector('#agent-composer')"),
    );
    const boot = await this.request("/api/v2/bootstrap");
    if (boot.status !== 200)
      throw Error("Bootstrap failed: " + JSON.stringify(boot.body));
    this.firstScreenMs = performance.now() - this.started;
    const version = await (
      await fetch("http://127.0.0.1:" + this.cdpPort + "/json/version")
    ).json();
    this.browser = await connect(version.webSocketDebuggerUrl);
    return this;
  }
  async evaluate(expression) {
    const r = await this.page.call("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (r.exceptionDetails)
      throw Error(
        r.exceptionDetails.exception?.description || r.exceptionDetails.text,
      );
    return r.result.value;
  }
  request(route, body) {
    return this.evaluate(
      "window.workbench.request(" +
        JSON.stringify({
          path: route,
          method: body === undefined ? "GET" : "POST",
          body,
          requestId: body === undefined ? undefined : crypto.randomUUID(),
        }) +
        ")",
    );
  }
  click(text) {
    return this.evaluate(
      `(()=>{const e=Array.from(document.querySelectorAll('button')).find(e=>(e.textContent===${JSON.stringify(text)}||e.getAttribute('aria-label')===${JSON.stringify(text)})&&e.getClientRects().length);if(!e||e.disabled)throw Error('Button unavailable: '+${JSON.stringify(text)});e.focus();e.click();return true})()`,
    );
  }
  type(text) {
    return this.evaluate(
      `(()=>{const e=document.querySelector('#agent-composer');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(e,${JSON.stringify(text)});e.dispatchEvent(new Event('input',{bubbles:true}));return true})()`,
    );
  }
  async close() {
    if (this.child && this.child.exitCode === null) {
      if (this.browser)
        await this.browser.call("Browser.close").catch(() => {});
      try {
        await wait(
          () => this.child.exitCode !== null || this.child.signalCode !== null,
          15000,
        );
      } catch (e) {
        this.child.kill();
        throw e;
      }
    }
    this.page?.socket.close();
    this.browser?.socket.close();
  }
  async abort() {
    try {
      await this.close();
    } catch {
      this.page?.socket.close();
      this.browser?.socket.close();
    }
  }
}
module.exports = { Driver, connect, wait, sleep };
