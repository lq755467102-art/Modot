const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { wait: eventually } = require("./electron_cdp.cjs");

const clientId = "Iv23.fixture-Modot";
const clientSecret = "modot-oauth-fixture-secret";
const redirect = "http://127.0.0.1:17889/callback";

function githubFixture() {
  const opened = [];
  const codes = new Map();
  let issued = 0;
  return {
    async open(url) { opened.push(new URL(url)); },
    reply(req, res, body) {
      const url = new URL(req.url, "http://127.0.0.1");
      if (url.pathname === "/github/authorize") {
        const params = url.searchParams;
        assert.equal(params.get("redirect_uri"), redirect);
        assert.equal(params.get("client_id"), clientId);
        assert.equal(params.get("code_challenge_method"), "S256");
        assert.equal(params.has("client_secret"), false);
        assert.equal(params.has("resource"), false);
        assert.equal(params.has("scope"), false);
        const code = "modot-fixture-code-" + (issued + 1);
        codes.set(code, params.get("code_challenge"));
        const next = new URL(redirect);
        next.searchParams.set("state", params.get("state"));
        next.searchParams.set("code", code);
        res.writeHead(302, {Location: next.href});
        res.end();
        return true;
      }
      if (url.pathname === "/github/token") {
        const params = new URLSearchParams(body);
        assert.equal(req.method, "POST");
        assert.equal(params.get("client_id"), clientId);
        assert.equal(params.get("client_secret"), clientSecret);
        assert.equal(params.get("redirect_uri"), redirect);
        assert.equal(params.get("grant_type"), "authorization_code");
        assert.equal(params.has("resource"), false);
        assert.equal(createHash("sha256").update(params.get("code_verifier") ?? "").digest("base64url"), codes.get(params.get("code")));
        assert.ok(codes.delete(params.get("code")));
        res.writeHead(200, {"Content-Type": "application/json"});
        res.end(JSON.stringify({access_token: "modot-fixture-token-" + (++issued),
          token_type: "bearer", scope: "", expires_in: 28800, refresh_token: "modot-fixture-refresh"}));
        return true;
      }
      return false;
    },
    opened,
    count: () => issued,
  };
}

async function acceptGithubOAuth({js, wait, click, passed, fixture}) {
  const root = "/api/v2/plugins";
  const get = route => js(`window.workbench.request({path:${JSON.stringify(route)}})`);
  const post = (route, body) => js(`window.workbench.request({
    path:${JSON.stringify(route)}, method:'POST', body:${JSON.stringify(body)}, requestId:crypto.randomUUID()
  })`);
  async function installed() {
    const result = await get(root);
    return result.body.plugins.find(pkg => pkg.name === "github-mcp");
  }
  const find = label => `[aria-label=${JSON.stringify(label)}]`;
  async function setField(label, value) {
    await js(`(() => {
      const node = document.querySelector(${JSON.stringify(find(label))});
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(node,${JSON.stringify(value)});
      node.dispatchEvent(new Event('input',{bubbles:true}));
    })()`);
  }
  async function openGitHubAccount() {
    await click('GitHub');
    await click('账号与连接');
  }
  await wait("document.querySelector('[data-plugin-application=github]')?.dataset.available==='true'");
  await openGitHubAccount();
  await wait("document.querySelector('.plugin-home-service')?.textContent.includes('连接 GitHub')");
  await click("连接 GitHub");
  await wait("!!document.querySelector('.catalog-management')");
  const initial = await installed();
  const target = root + "/" + initial.id + "/connections/github";
  const denied = await post(target + "/oauth", {expectedRevision: initial.revision, scopes: []});
  assert.equal(denied.body.code, "plugin_denied");
  await js("document.querySelector('input[aria-label=\"启用 MCP github-mcp\"]').click()");
  const enabled = await eventually(async () => {
    const pkg = await installed();
    return pkg.remoteEnabled && pkg;
  }, 8000);
  const missing = await post(target + "/oauth", {expectedRevision: enabled.revision, scopes: []});
  assert.equal(missing.body.code, "github_app_required");
  await js("Array.from(document.querySelectorAll('summary')).find(el=>el.textContent==='配置 GitHub App').click()");
  await setField("GitHub App Client ID", clientId);
  await setField("GitHub App Client Secret", clientSecret);
  await click("保存 GitHub App");
  const configured = await eventually(async () => {
    const pkg = await installed();
    return pkg.servers[0].githubAppConfigured && pkg;
  }, 8000);
  assert.equal(JSON.stringify(configured).includes(clientSecret), false);
  const pluginDir = path.join(process.env.HARNESS_DATA, "runs", "workbench");
  const vault = path.join(pluginDir, "security", "plugin-connections");
  assert.equal(fs.readdirSync(vault).length, 1);
  assert.ok(fs.readdirSync(vault).every(name => !fs.readFileSync(path.join(vault,name), "utf8").includes(clientSecret)));
  passed.push("GitHub App is opt-in, validates MCP enablement and stores the secret with Windows DPAPI");

  async function browserLogin(index, prior = index, stale) {
    await openGitHubAccount();
    await wait("Array.from(document.querySelectorAll('.plugin-home-service button')).some(e=>e.textContent==='在浏览器登录 GitHub'&&!e.disabled)");
    await click("在浏览器登录 GitHub");
    const url = await eventually(() => fixture.opened[index], 8000);
    const invalid = await fetch(redirect + "?state=invalid&code=fixture");
    assert.equal(invalid.status, 400);
    await invalid.arrayBuffer();
    if (stale) {
      const expired = await fetch(stale);
      assert.equal(expired.status, 400);
      await expired.arrayBuffer();
    }
    assert.equal(fixture.count(), prior);
    const reply = await fetch(url);
    assert.equal(reply.status, 200);
    assert.match(await reply.text(), /Connected/);
    await wait("document.querySelector('.plugin-home-service-state')?.textContent==='已连接'");
    assert.equal(fixture.count(), prior + 1);
  }
  await browserLogin(0);
  assert.equal(fs.readdirSync(vault).length, 2);
  passed.push("single click passes browser URL through native IPC, validates state and PKCE, and updates the GitHub page");

  await click("管理插件");
  await wait("Array.from(document.querySelectorAll('.catalog-management button')).some(e=>e.textContent==='断开连接'&&!e.disabled)");
  await click("断开连接");
  const disconnected = await eventually(async () => {
    const pkg = await installed();
    return !pkg.servers[0].connected && pkg;
  }, 8000);
  assert.equal(disconnected.servers[0].githubAppConfigured, true);
  assert.equal(fs.readdirSync(vault).length, 1);
  await browserLogin(1);
  assert.notEqual(fixture.opened[0].searchParams.get("state"), fixture.opened[1].searchParams.get("state"));
  assert.equal(fs.readdirSync(vault).length, 2);
  passed.push("disconnect removes the previous token and reconnects in a new browser authorization without reentering the app configuration");

  await click("重新登录 GitHub");
  await eventually(() => fixture.opened[2], 8000);
  await wait("Array.from(document.querySelectorAll('.plugin-home-service button')).some(e=>e.textContent==='取消登录')");
  await click("取消登录");
  await wait("document.querySelector('.plugin-home-service')?.textContent.includes('登录已取消')");
  assert.equal((await installed()).servers[0].connected, true);
  assert.equal(fixture.count(), 2);
  passed.push("cancelling a reconnect closes the pending callback and keeps the previous valid connection");

  await click("重新登录 GitHub");
  const stale = await eventually(() => fixture.opened[3], 8000);
  await click("管理插件");
  await wait("Array.from(document.querySelectorAll('.catalog-management button')).some(e=>e.textContent==='断开连接'&&!e.disabled)");
  await click("断开连接");
  await wait("document.querySelector('.catalog-management')?.textContent.includes('待连接')");
  await browserLogin(4, 2, stale);
  passed.push("disconnect during pending authorization abandons the old callback and allows a fresh login on the same port");

  const thread = await post("/api/v2/conversations/threads", {title:"Native execution verification"});
  const execution = "/api/v2/conversations/threads/" + thread.body.id + "/execution";
  const view = await get(execution);
  assert.equal(view.body.next.crew, "l0");
  assert.ok(view.body.routes.every(route => route.id !== "l3"));
  const retired = await post(execution, {crew:"l3", expectedRevision:view.body.next.revision});
  assert.equal(retired.body.code, "invalid_execution");
  const start = await post(execution.replace(/\/execution$/, "/send"), {text:"removed mode",crew:"l3"});
  assert.equal(start.status, 400);
  passed.push("default L0 remains available and the removed L3 cannot be selected or started");
}

module.exports = {githubFixture, acceptGithubOAuth};
