const assert = require('node:assert/strict');
const {BrowserWindow} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const {wait: eventually} = require('./electron_cdp.cjs');

const clientId = 'Iv23fixtureModot';
const grant = 'urn:ietf:params:oauth:grant-type:device_code';
const availableRepos = ['selected-private','unselected-private'];
const installationId = 71;
const repoSpec = {name:'get_file_contents',description:'Read an installed repository file',
  annotations:{readOnlyHint:true},inputSchema:{type:'object',
    properties:{owner:{type:'string'},repo:{type:'string'},path:{type:'string'}},required:['owner','repo','path']}};

function respond(res, value, status = 200) {
  res.writeHead(status, {'Content-Type':'application/json'});
  res.end(JSON.stringify(value));
}

function githubDeviceFixture({live = false} = {}) {
  const opened = [], attempts = new Map(), reads = [], errors = [], access = [];
  let nextAttempt = {}, scenario, issued = 0;
  let installed = false;
  let granted = new Set();
  return {
    opened, attempts, reads, errors, access,
    async open(url) { opened.push(new URL(url)); },
    next(options) { nextAttempt = options; },
    readRepositories(id, repositories, catalog) {
      scenario = {id, repositories, step:0, marker:'GITHUB_DEVICE_CHECK_' + Date.now(),
        inspected:!!catalog,catalog,tool:catalog?.tools.find(t=>t.spec.name===repoSpec.name)};
      return scenario;
    },
    findRepository(id, query, read = false) {
      const run = this.readRepositories(id, []);
      run.lookup = {query, read, pages:[]};
      return run;
    },
    reply(req, res, raw) {
      const url = new URL(req.url, 'http://127.0.0.1');
      if (live || !url.pathname.startsWith('/github/')) return false;
      try {
        if (url.pathname.startsWith('/github/api/') || url.pathname === '/github/mcp') access.push(url.pathname);
        if (url.pathname === '/github/device-code') {
          assert.equal(req.method, 'POST');
          assert.deepEqual(Object.fromEntries(new URLSearchParams(raw)), {client_id:clientId});
          if (nextAttempt.disabled) {
            nextAttempt = {};
            respond(res, {error:'device_flow_disabled'}, 400);
          } else {
            const n = attempts.size + 1;
            const attempt = {deviceCode:'fixture-device-'+n,userCode:`MDOT-${String(n).padStart(4,'0')}`,
              interval:1, ttl:20, polls:[], ...nextAttempt};
            nextAttempt = {};
            attempts.set(attempt.deviceCode, attempt);
            respond(res, {device_code:attempt.deviceCode,user_code:attempt.userCode,
              verification_uri:attempt.badUrl ? 'https://example.invalid/login/device' : `http://${req.headers.host}/github/device`,
              expires_in:attempt.ttl,interval:attempt.interval});
          }
        } else if (url.pathname === '/github/device' && req.method === 'GET') {
          res.writeHead(200, {'Content-Type':'text/html; charset=utf-8'});
          res.end('<!doctype html><form method="post"><label>Device code <input name="code"></label>' +
            '<button name="decision" value="allow">Authorize</button><button name="decision" value="deny">Deny</button></form>');
        } else if (url.pathname === '/github/device' && req.method === 'POST') {
          const data = new URLSearchParams(raw);
          const attempt = [...attempts.values()].find(a => a.userCode === data.get('code'));
          assert.ok(attempt);
          attempt.decision = data.get('decision');
          respond(res, {received:true});
        } else if (url.pathname === '/github/device-token') {
          const params = Object.fromEntries(new URLSearchParams(raw));
          assert.deepEqual(params, {client_id:clientId,device_code:params.device_code,grant_type:grant});
          const attempt = attempts.get(params.device_code);
          assert.ok(attempt);
          attempt.polls.push(Date.now());
          if (attempt.slowDown && attempt.polls.length === 1) respond(res,{error:'slow_down'},429);
          else if (attempt.decision === 'deny') respond(res,{error:'access_denied'});
          else if (attempt.decision !== 'allow') respond(res,{error:'authorization_pending'});
          else {
            const send = () => {
              attempt.release = undefined;
              attempt.token = 'fixture-github-access-'+(++issued);
              respond(res,{access_token:attempt.token,token_type:'bearer',scope:'',expires_in:28800});
            };
            if (attempt.hold) attempt.release = send;
            else send();
          }
        } else if (url.pathname.startsWith('/github/api/')) {
          const authenticated = [...attempts.values()].some(a => a.token && req.headers.authorization === 'Bearer '+a.token);
          if (!authenticated) respond(res, {}, 401);
          else if (url.pathname === '/github/api/user') respond(res,{login:'modot-fixture'});
          else if (url.pathname === '/github/api/user/installations') {
            const installations = installed ? [{id:installationId,app_slug:'modot-l',target_type:'User',account:{login:'modot-fixture'}}] : [];
            respond(res,{total_count:installations.length,installations});
          } else if (installed && url.pathname === `/github/api/user/installations/${installationId}/repositories`) {
            const repositories = [...granted].map(repo=>({full_name:'modot-fixture/'+repo,private:true}));
            respond(res,{total_count:repositories.length,repositories});
          } else respond(res,{},404);
        } else if (url.pathname === '/github/apps/modot-l/installations/new' ||
                   url.pathname === `/github/settings/installations/${installationId}`) {
          if (req.method === 'POST') {
            granted = new Set(new URLSearchParams(raw).getAll('repository').filter(name=>availableRepos.includes(name)));
            installed = true;
            res.writeHead(200,{'Content-Type':'text/plain'});
            res.end('Repository selection saved');
          } else {
            res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});
            res.end('<!doctype html><form method="post">'+availableRepos.map(name=>
              `<label><input type="checkbox" name="repository" value="${name}"${granted.has(name)?' checked':''}>${name}</label>`
            ).join('')+'<button>Save repositories</button></form>');
          }
        } else if (url.pathname === '/github/mcp') {
          const attempt = [...attempts.values()].find(a => a.token && req.headers.authorization === 'Bearer '+a.token);
          if (!attempt) respond(res, {}, 401);
          else {
            const rpc = JSON.parse(raw);
            let result;
            if (rpc.method === 'notifications/initialized') {res.writeHead(202);res.end();return true;}
            if (rpc.method === 'initialize') result = {protocolVersion:'2025-06-18',capabilities:{tools:{}}};
            if (rpc.method === 'tools/list') result = {tools:[repoSpec]};
            if (rpc.method === 'tools/call') {
              assert.equal(rpc.params.name,'get_file_contents');
              const args = rpc.params.arguments;
              reads.push({repo:args.repo, attempt:attempt.userCode});
              assert.equal(args.owner,'modot-fixture');
              assert.equal(args.path,'README.md');
              result = granted.has(args.repo)
                ? {content:[{type:'text',text:'selected-repository-evidence'}]}
                : {isError:true,content:[{type:'text',text:'repository not accessible to this installation'}]};
            }
            assert.ok(result);
            respond(res, {jsonrpc:'2.0',id:rpc.id,result});
          }
        } else respond(res, {}, 404);
      } catch (error) {
        errors.push(error);
        if (!res.headersSent) respond(res, {}, 500);
      }
      return true;
    },
    model(body, res, tool) {
      if (!scenario || !body.messages.some(m => m.role==='user' && String(m.content).includes(scenario.marker))) return false;
      const records = body.messages.filter(m=>m.role==='tool');
      let value = records.length ? JSON.parse(records.at(-1).content) : {};
      const call = (name,args) => tool(res,name,args,'github-device-'+(++scenario.step));
      const names = body.tools?.map(t => t.function?.name) || [];
      const base = {plugin_id:scenario.id,server:'github'};
      const finish = () => {
        scenario.finished = true;
        const chunk = {choices:[{delta:{content:'GitHub device fixture completed.'},finish_reason:'stop'}]};
        res.writeHead(200, {'Content-Type':'text/event-stream'});
        res.end(`data: ${JSON.stringify(chunk)}\n\ndata: [DONE]\n\n`);
        return true;
      };
      if (scenario.plain) {
        scenario.initialTools = names;
        scenario.containsRepository = body.messages.some(m=>String(m.content).includes('modot-fixture/selected-private'));
        return finish();
      }
      const lookup = scenario.lookup;
      if (lookup && !lookup.finished) {
        if (scenario.step === 0 && !names.includes('find_github_repositories')) return call('use_tool_group',{group:'plugins'}),true;
        if (!lookup.listed) {
          lookup.listed = true;
          return call('list_plugins',{}),true;
        }
        if (!lookup.args) {
          const pkg = value.plugins?.find(p=>p.name==='github-mcp' && p.remoteEnabled);
          assert.equal(pkg?.id,scenario.id);
          assert.ok(names.includes('find_github_repositories'));
          lookup.args = {plugin_id:pkg.id,server:pkg.servers[0].name,query:lookup.query};
          return call('find_github_repositories',lookup.args),true;
        }
        lookup.pages.push(value);
        if (value.ok && value.next) return call('find_github_repositories',{...lookup.args,cursor:value.next}),true;
        lookup.finished = true;
        lookup.matches = lookup.pages.flatMap(page=>page.repositories || []);
        if (!lookup.read || lookup.matches.length!==1) return finish();
        const [owner,repo] = lookup.matches[0].fullName.split('/');
        scenario.repositories.push({owner,repo});
      }
      if (scenario.step === 0 && !names.includes('inspect_plugin_tools')) return call('use_tool_group',{group:'plugins'}),true;
      if (!scenario.inspected) {
        scenario.inspected = true;
        return call('inspect_plugin_tools',base),true;
      }
      if (value.spill) {
        const {load,read} = value.spill;
        scenario.spill = {read,pages:[],loading:!names.includes(read.tool)};
        const next = scenario.spill.loading ? load : read;
        return call(next.tool,next.arguments),true;
      }
      if (scenario.spill) {
        const {read,pages,loading} = scenario.spill;
        if (loading) {
          scenario.spill.loading = false;
          return call(read.tool,read.arguments),true;
        }
        if (value.ok && typeof value.text==='string') {
          pages.push(value.text);
          if (value.hasMore) return call(read.tool,{...read.arguments,after:value.next,evidence_revision:value.evidenceRevision}),true;
          value=JSON.parse(pages.join(''));
        }
        scenario.spill=undefined;
      }
      if (!scenario.catalog || scenario.fetchingPage) {
        scenario.fetchingPage = false;
        scenario.catalog = value;
        scenario.tool = value.tools?.find(t => t.spec.name === repoSpec.name);
        if (!scenario.tool && value.ok && value.hasMore && value.next > 0) {
          scenario.fetchingPage = true;
          return call('inspect_plugin_tools',{...base,after:value.next,catalog_hash:value.catalog_hash}),true;
        }
      } else if (scenario.awaiting) {
        scenario.awaiting.result = value;
        scenario.awaiting = undefined;
      }
      const target = scenario.repositories.find(repo => !repo.result);
      if (scenario.tool?.callable && target) {
        scenario.awaiting = target;
        call('call_plugin_tool',{...base,tool:repoSpec.name,schema_hash:scenario.tool.schema_hash,
          arguments:{owner:target.owner,repo:target.repo,path:'README.md'}});
        return true;
      }
      return finish();
    },
  };
}

async function submitInBrowser(url, script, confirmation) {
  const browser = new BrowserWindow({show:false,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false}});
  try {
    await browser.loadURL(String(url));
    const finished = new Promise(resolve => browser.webContents.once('did-finish-load',resolve));
    await browser.webContents.executeJavaScript(script);
    await finished;
    assert.match(await browser.webContents.executeJavaScript('document.body.textContent'),confirmation);
    assert.equal(browser.isVisible(),false);
  } finally { browser.destroy(); }
}

function approveInBrowser(url, code, decision = 'allow') {
  return submitInBrowser(url,`(() => {
    document.querySelector('input[name=code]').value = ${JSON.stringify(code)};
    document.querySelector('button[value=${decision}]').click();
  })()`,/received/);
}

function chooseRepositoriesInBrowser(url, repositories) {
  return submitInBrowser(url,`(() => {
    for (const option of document.querySelectorAll('input[name=repository]')) {
      option.checked = ${JSON.stringify(repositories)}.includes(option.value);
    }
    document.querySelector('button').click();
  })()`,/Repository selection saved/);
}

async function githubDeviceAcceptance({js, wait, click, input, passed, fixture, live = false}) {
  const root = '/api/v2/plugins';
  const get = route => js(`window.workbench.request({path:${JSON.stringify(route)}})`);
  const post = (route,body) => js(`window.workbench.request({path:${JSON.stringify(route)},method:'POST',
    body:${JSON.stringify(body)},requestId:crypto.randomUUID()})`);
  const installed = async () => (await get(root)).body.plugins.find(pkg=>pkg.name==='github-mcp');
  async function openGitHubAccount() {
    await click('GitHub');
    await click('账号与连接');
  }
  await wait("document.querySelector('[data-plugin-application=github]')?.dataset.available==='true'");
  await openGitHubAccount();
  const initial = await installed();
  const target = root+'/'+initial.id+'/connections/github';
  const vault = path.join(process.env.HARNESS_DATA,'runs','workbench','security','plugin-connections');
  if (!initial.remoteEnabled) {
    const rejected = await post(root+'/'+initial.id+'/tools',{server:'github',expectedRevision:initial.revision});
    assert.equal(rejected.body.code,'plugin_denied');
    assert.equal(fs.existsSync(vault),false);
    passed.push('GitHub MCP is opt-in and the bundled public Client ID needs no local secret');
  }
  assert.equal(initial.servers[0].githubAppConfigured,true);

  async function enableMcp() {
    if ((await installed()).remoteEnabled) return;
    await openGitHubAccount();
    await click('管理插件');
    await wait('!!document.querySelector(\'.catalog-management\')');
    await js('document.querySelector(\'input[aria-label="启用 MCP github-mcp"]\').click()');
    await eventually(async()=>(await installed()).remoteEnabled,8000);
  }

  async function beginLogin() {
    const n = fixture.opened.length;
    await openGitHubAccount();
    const loginSelector = "Array.from(document.querySelectorAll('.plugin-home-service button')).find(e=>['连接 GitHub 账号','重新登录 GitHub'].includes(e.textContent)&&!e.disabled)";
    await wait('!!('+loginSelector+')');
    await js('('+loginSelector+').click()');
    const code = await eventually(async()=>{
      const current = await js("document.querySelector('[aria-label=\"GitHub 设备授权码\"]')?.textContent");
      const problem = await js("Array.from(document.querySelectorAll('.plugin-home-service p[role=status]')).map(e=>e.textContent).find(t=>t.includes('请在 GitHub App')||t.includes('拒绝')||t.includes('失败')||t.includes('无法核对'))");
      if (!current && problem) throw Error(problem);
      return current;
    },10000);
    return {code,url:await eventually(()=>fixture.opened[n],8000)};
  }

  async function runAgent(result, request = '') {
    const permission = await get('/api/v2/permissions');
    if (permission.body.mode!=='full') {
      const saved = await post('/api/v2/permissions',{mode:'full',confirm:true,expectedRevision:permission.body.revision});
      assert.equal(saved.status,200);
    }
    await input(`${result.marker} ${request}`.trim());
    await click('发送');
    await eventually(()=>result.finished,65000);
    await wait("document.querySelector('.agent-messages')?.textContent.includes('GitHub device fixture completed.')");
    await wait("!document.querySelector('#state.live')");
    assert.ok(result.finished);
    return result;
  }
  const runReads = (repositories,catalog) => runAgent(fixture.readRepositories(initial.id,repositories,catalog));

  if (live) {
    async function authorizeLive() {
      const login = await beginLogin();
      assert.equal(login.url.href,'https://github.com/login/device');
      console.log('MODOT_GITHUB_DEVICE_CODE '+login.code);
      await wait("document.querySelector('.plugin-home-service-state')?.textContent==='已登录'",240000);
    }
    if (!initial.servers[0].connected) {
      await authorizeLive();
      passed.push('real GitHub device authorization established a Modot connection');
    } else {
      await wait("document.querySelector('.plugin-home-service-state')?.textContent==='已登录'");
      passed.push('real GitHub connection resumed from isolated profile after Electron restart');
    }
    const repo = process.env.HARNESS_TEST_GITHUB_REPOSITORY.split('/');
    const reads = [{owner:repo[0],repo:repo[1]}];
    if (process.env.HARNESS_TEST_GITHUB_DENIED_REPOSITORY) {
      const [owner,name] = process.env.HARNESS_TEST_GITHUB_DENIED_REPOSITORY.split('/');
      reads.push({owner,repo:name});
    }
    await enableMcp();
    const outcome = await runReads(reads);
    assert.ok(outcome.catalog.ok,`MCP catalog did not load (${outcome.catalog.code})`);
    assert.ok(reads[0].result?.ok,`selected repository read failed (${reads[0].result?.code || 'missing read result'})`);
    passed.push('read README.md in the real selected repository through the normal Modot MCP tool');
    if (reads[1]) {
      assert.equal(reads[1].result?.ok,false);
      passed.push('real GitHub MCP refused the unselected private repository');
    }
    if (process.env.HARNESS_TEST_GITHUB_RECONNECT==='1') {
      await disconnect();
      passed.push('real GitHub MCP requires authentication again immediately after disconnect');
      await authorizeLive();
      const [selected] = reads;
      const retried = await runReads([{owner:selected.owner,repo:selected.repo}]);
      assert.ok(retried.repositories[0].result?.ok);
      await disconnect();
      passed.push('real GitHub reauthorization restores the selected repo; the temporary connection was disconnected');
    }
    return;
  }

  const first = await beginLogin();
  const attempt = [...fixture.attempts.values()].at(-1);
  const duplicate = await post(target+'/oauth',{expectedRevision:initial.revision});
  assert.equal(duplicate.body.code,'github_device_pending');
  await eventually(()=>attempt.polls.length>0,5000);
  assert.equal((await installed()).servers[0].connected,false);
  await approveInBrowser(first.url,first.code);
  await wait("document.querySelector('.plugin-home-service-state')?.textContent==='已登录'");
  assert.equal(fs.readdirSync(vault).length,1);
  assert.ok(fs.readdirSync(vault).every(name=>!fs.readFileSync(path.join(vault,name),'utf8').includes(attempt.token)));
  passed.push('browser only authorizes the short code; waiting and duplicate requests do not attach a token');
  await wait("document.querySelector('.github-repository-access')?.textContent.includes('@modot-fixture') && document.querySelector('.github-repository-access')?.textContent.includes('尚未授权仓库')");
  const signedIn = await installed();
  assert.equal(signedIn.remoteEnabled,false);
  assert.equal(signedIn.servers[0].githubAccountConnected,true);
  const stillBlocked = await post(root+'/'+initial.id+'/tools',{server:'github',expectedRevision:signedIn.revision});
  assert.equal(stillBlocked.body.code,'plugin_denied');
  passed.push('the account is signed in with no installation or repository and MCP still off');

  const pageIndex=fixture.opened.length;
  await click('添加仓库');
  const installPage=await eventually(()=>fixture.opened[pageIndex],8000);
  assert.equal(installPage.pathname,'/github/apps/modot-l/installations/new');
  await chooseRepositoriesInBrowser(installPage,['selected-private']);
  await js("window.dispatchEvent(new Event('focus'))");
  await wait("document.querySelector('[aria-label=已授权仓库]')?.textContent.includes('modot-fixture/selected-private')");
  assert.equal(await js("document.querySelector('[aria-label=已授权仓库]')?.textContent.includes('unselected-private')"),false);
  assert.equal((await installed()).revision,signedIn.revision);
  passed.push('Modot opens the App repository chooser and reloads exactly the selected repositories when returning from GitHub');

  await enableMcp();
  await openGitHubAccount();
  await wait("document.querySelector('[aria-label=已授权仓库]')?.textContent.includes('selected-private')");
  await click('对话');
  await wait("!!document.querySelector('#agent-composer') && !document.querySelector('.github-repository-access')");
  const requestCount = fixture.access.length;
  const plain = fixture.readRepositories(initial.id, []);
  plain.plain = true;
  await runAgent(plain, '写一句欢迎语。');
  assert.equal(plain.initialTools.includes('find_github_repositories'),false);
  assert.equal(plain.containsRepository,false);
  assert.deepEqual(fixture.access.slice(requestCount),[]);
  passed.push('ordinary L0 tasks do not receive repository names or query GitHub');

  const byName = await runAgent(fixture.findRepository(initial.id,'selected-private',true),
    '帮我读 GitHub 仓库 selected-private 的 README，我只记得仓库名。');
  assert.deepEqual(byName.lookup.matches,[{fullName:'modot-fixture/selected-private',private:true}]);
  assert.equal(byName.repositories[0].result?.result?.content[0].text,'selected-repository-evidence');
  assert.equal(fixture.reads.at(-1).repo,'selected-private');
  assert.equal(fixture.opened.length,pageIndex+1);
  passed.push('L0 loads the GitHub repository lookup on demand and uses its resolved owner to read README without binding a repo in Modot');

  const readOffset = fixture.reads.length;
  const repos = ['selected-private','unselected-private'].map(repo=>({owner:'modot-fixture',repo}));
  const selected = await runReads(repos);
  assert.equal(repos[0].result?.ok,true,repos[0].result?.code || 'selected fixture repository did not return content');
  assert.equal(repos[0].result.result.content[0].text,'selected-repository-evidence');
  assert.equal(repos[1].result?.ok,false);
  assert.match(repos[1].result.result.content[0].text,/not accessible/);
  assert.deepEqual(fixture.reads.slice(readOffset).map(row=>row.repo),repos.map(row=>row.repo));
  passed.push('normal Agent MCP call reads the selected repository and preserves the denial for an unselected private repository');

  await openGitHubAccount();
  await wait("document.querySelector('[aria-label=已授权仓库]')?.textContent.includes('selected-private')");
  const lastList=await installed();
  const settingsIndex=fixture.opened.length;
  await click('管理仓库');
  const settingsPage=await eventually(()=>fixture.opened[settingsIndex],8000);
  assert.equal(settingsPage.pathname,`/github/settings/installations/${installationId}`);
  await chooseRepositoriesInBrowser(settingsPage,[]);
  const removed = await runAgent(fixture.findRepository(initial.id,'selected-private'),
    '查一下现在还能不能在 GitHub 找到仓库 selected-private。');
  assert.deepEqual(removed.lookup.matches,[]);
  assert.match(removed.lookup.pages.at(-1).notice,/添加仓库/);
  assert.equal(fixture.opened.length,settingsIndex+1);
  passed.push('the next Agent lookup reflects GitHub revocation without a Modot refresh and explains how to add repository access');
  const revoked={owner:'modot-fixture',repo:'selected-private'};
  await runReads([revoked],selected.catalog);
  assert.equal(revoked.result?.ok,false);
  assert.match(revoked.result.result.content[0].text,/not accessible/);
  assert.equal(fixture.reads.at(-1).attempt,attempt.userCode);
  assert.equal((await installed()).revision,lastList.revision);
  await openGitHubAccount();
  await click('刷新已授权仓库');
  await wait("document.querySelector('.github-repository-access')?.textContent.includes('尚未授权仓库')");
  assert.equal((await installed()).servers[0].githubAccountConnected,true);
  passed.push('revoking in GitHub immediately denies the same repo with the same token and tool schema; refreshing clears the repo without logging out');

  const disconnected = await disconnect();

  fixture.next({slowDown:true});
  const second = await beginLogin();
  const retry = [...fixture.attempts.values()].at(-1);
  assert.notEqual(first.code,second.code);
  await approveInBrowser(second.url,second.code);
  await wait("document.querySelector('.plugin-home-service-state')?.textContent==='已登录'");
  assert.ok(retry.polls[1]-retry.polls[0]>=5950);
  assert.notEqual(retry.token,attempt.token);
  passed.push('disconnect removes the old credential; reconnect obtains a fresh token and honors GitHub slow_down');

  fixture.next({hold:true});
  const pending = await beginLogin();
  const held = [...fixture.attempts.values()].at(-1);
  await approveInBrowser(pending.url,pending.code);
  await eventually(()=>held.release,5000);
  await click('取消登录');
  await wait("document.querySelector('.plugin-home-service')?.textContent.includes('登录已取消')");
  held.release();
  const afterCancel = await installed();
  assert.equal(afterCancel.servers[0].connected,true);
  assert.equal(afterCancel.revision,disconnected.revision+1);
  passed.push('cancelling reauthorization while the response is in flight leaves the previous credential active');

  fixture.next({ttl:2});
  await beginLogin();
  await wait("document.querySelector('.plugin-home-service')?.textContent.includes('授权已过期')");
  assert.equal((await installed()).revision,afterCancel.revision);
  passed.push('expired device codes cannot replace an existing connection');

  const opened = fixture.opened.length;
  fixture.next({disabled:true});
  const disabled = await post(target+'/oauth',{expectedRevision:afterCancel.revision});
  assert.equal(disabled.body.code,'github_device_disabled');
  fixture.next({badUrl:true});
  const wrongPage = await post(target+'/oauth',{expectedRevision:afterCancel.revision});
  assert.equal(wrongPage.body.code,'github_device_response');
  assert.equal(fixture.opened.length,opened);
  const refused = await beginLogin();
  await approveInBrowser(refused.url,refused.code,'deny');
  await wait("document.querySelector('.plugin-home-service')?.textContent.includes('GitHub 未同意')");
  assert.equal((await installed()).revision,afterCancel.revision);
  assert.deepEqual(fixture.errors,[]);
  passed.push('disabled device flow, unexpected verification page, and browser denial leave the saved connection intact');

  async function disconnect() {
    await openGitHubAccount();
    await click('管理插件');
    await click('断开连接');
    const pkg = await eventually(async()=>{const p=await installed();return !p.servers[0].connected && p;},8000);
    assert.equal(pkg.servers[0].githubAppConfigured,true);
    assert.equal(fs.readdirSync(vault).length,0);
    const denied = await post(root+'/'+initial.id+'/tools',{server:'github',expectedRevision:pkg.revision});
    assert.equal(denied.body.code,'plugin_auth_required');
    return pkg;
  }
}

module.exports = {githubDeviceFixture,githubDeviceAcceptance};
