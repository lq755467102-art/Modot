// Exercise the actual GitHub rail, browser and main conversation in hidden Electron.
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {once} = require('node:events');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {Driver, connect, wait} = require('./electron_cdp.cjs');

const mode = process.argv[2] || 'public';
const acceptance = process.env.HARNESS_TEST_ROOT;
const exe = process.env.HARNESS_TEST_ELECTRON;
const python = process.env.HARNESS_TEST_PYTHON;
assert.ok(['public', 'fixed', 'real', 'binding', 'pr'].includes(mode), 'Choose public, fixed, real, binding or pr');
assert.ok(acceptance && exe && python, 'Set HARNESS_TEST_ROOT, HARNESS_TEST_ELECTRON and HARNESS_TEST_PYTHON');
const out = fs.mkdtempSync(path.join(path.resolve(acceptance), 'github-workroom-' + mode + '-'));
const profile = path.join(out, 'profile');
const data = path.join(profile, 'data');
const liveViews=path.join(data,'runs','workbench','github-views');
const models = path.join(profile, 'models.json');
const entry = path.join(__dirname, '../desktop');
const testEnvironment = {HARNESS_PYTHON:python, HARNESS_GIT:process.env.HARNESS_TEST_GIT || 'git'};
const driver = new Driver({exe, out, profile, entry, env:{...testEnvironment,HARNESS_DATA:data}});
const rail = '[data-plugin-application="github"][data-available="true"]';
const normal = '[data-builtin-app="agent"]';
const code = '[data-builtin-app="workbench"]';
const room = '[aria-label="GitHub 工作间"].github-workroom';
const browser = room + ' .browser-pane';
const chat = '#github-main-chat';
const approval = '[aria-label="安全批准请求"]';
const threads = '/api/v2/conversations/threads';
const repository = 'https://github.com/electron/electron';
const publicPR=process.env.HARNESS_TEST_GITHUB_PR||'https://github.com/python/cpython/pull/162468';
const realProvider=mode==='real'||mode==='pr';
const result = {mode,checks:[]};
let fixture, pendingStream, approvalResource, calls = 0, fault = '';
let recoverable = false;

const node = selector => 'document.querySelector(' + JSON.stringify(selector) + ')';
const visible = (selector,app=driver) => app.evaluate('!!' + node(selector) + '?.getClientRects().length');
const read = (selector,app=driver) => app.evaluate(node(selector)+'?.innerText || ""');
const draft = (app=driver) => app.evaluate('document.querySelector("#agent-composer")?.value || ""');
async function press(selector,app=driver) {
  await wait(() => visible(selector,app), 5500).catch(()=>{throw Error('Control unavailable: '+selector);});
  await app.evaluate(node(selector) + '.click()');
}
async function fill(selector,text,app=driver) {
  await wait(()=>visible(selector,app),8000);
  await app.evaluate('(()=>{const el='+node(selector)+
    ';const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;'+
    'Object.getOwnPropertyDescriptor(proto,"value").set.call(el,'+
    JSON.stringify(text)+');el.dispatchEvent(new Event("input",{bubbles:true}));})()');
}
async function request(route,body,app=driver) {
  const reply=await app.request(route,body);
  assert.equal(reply.status,200,route+' '+(reply.body?.code||reply.status));
  return reply.body;
}
async function select(thread,app=driver) {
  const row = '[data-conversation-id="'+thread.id+'"] > button';
  await app.evaluate('window.dispatchEvent(new Event("focus"))');
  await press(row,app);
  await wait(() => app.evaluate(node(row)+'.getAttribute("aria-current")==="page"'),6000);
}
async function openChat(app=driver) {
  await press(rail,app);
  const toggle=room+' [aria-controls="github-main-chat"]';
  await wait(() => visible(toggle,app), 5000);
  if (!await visible(chat,app)) await press(toggle,app);
  await wait(() => visible(chat+' #agent-composer',app),6000);
}
async function navigate(url) {
  const field=browser+' input[aria-label="网页地址"]';
  await fill(field,url);
  await driver.evaluate(node(field)+'.form.requestSubmit()');
}
async function browserTitle(title) {
  await wait(async () => (await read(browser+' h2')).includes(title), 18000)
    .catch(() => {throw Error('Missing browser title '+title);});
}
async function browse() {
  await press(rail);
  assert.ok(await wait(() => visible(room), 3000).catch(() => false), 'GitHub rail did not open a workroom');
  await wait(() => visible(browser + ' .browser-content'), 7000);
  await navigate(repository);
  await browserTitle('electron/electron');
  result.checks.push('The GitHub application shows a real repository in its own browser view.');
  if (!await visible(chat)) await press(room+' [aria-controls="github-main-chat"]');
  await driver.click('前往对话');
  await wait(()=>visible('[data-surface="agent"]'),6000);
  assert.equal((await request(threads)).threads.length,0);
  result.checks.push('An unbound GitHub workspace routes to ordinary chat selection without creating a conversation.');
}
function packet(res,delta,finish_reason=null) {
  res.write('data: '+JSON.stringify({choices:[{index:0,delta,finish_reason}]})+'\n\n');
}
function finish(res,content) {
  packet(res,{content},'stop');
  res.end('data: [DONE]\n\n');
}
function tool(res,name,args,id) {
  packet(res,{tool_calls:[{index:0,id,type:'function',function:{name,arguments:JSON.stringify(args)}}]},'tool_calls');
  res.end('data: [DONE]\n\n');
}
async function startFixture() {
  fixture=http.createServer(async (req,res)=>{
    if(req.url==='/recoverable') {
      if(!recoverable) {res.destroy();return;}
      res.writeHead(200,{'Content-Type':'text/html'}).end('<title>GitHub workspace retry fixture</title><p>Recovered</p>');
      return;
    }
    if(!req.url.endsWith('/chat/completions')) {res.writeHead(404).end('{}');return;}
    try {
      let raw='';
      for await (const chunk of req) raw+=chunk;
      const body=JSON.parse(raw);
      const latest=body.messages.findLast(message=>message.role==='user' &&
        !String(message.content).startsWith('[context-update:'));
      const user=JSON.stringify(latest?.content||'');
      const names=new Set((body.tools||[]).map(item=>item.function.name));
      calls++;
      res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache'});
      if(user.includes('GH_FIXTURE_STREAM')) {
        assert.equal(pendingStream,undefined,'Stream was requested twice');
        assert.match(user,/github.com\/electron\/electron/);
        assert.match(user,/读取时间/);
        pendingStream=res;
        packet(res,{content:'GH_FIXTURE_ALPHA'});
        return;
      }
      if(user.includes('GH_STALE_VIEW')) {
        assert.ok(!body.messages.some(m=>m.role==='tool'&&m.tool_call_id==='github-stale-read'),
          'A revoked pending approval should stop before the next model call');
        assert.ok(names.has('read_current_github_pr'));
        return tool(res,'read_current_github_pr',{},'github-stale-read');
      }
      assert.match(user,/GH_FIXTURE_APPROVAL/);
      const executed=body.messages.findLast(m=>m.role==='tool'&&m.tool_call_id==='github-workroom-shell');
      if(executed) {
        const receipt=JSON.parse(executed.content);
        assert.equal(receipt.ok,true);
        assert.match(receipt.output,/git version/i);
        return finish(res,'GH_APPROVAL_DONE');
      }
      assert.ok(names.has('run_project_shell'),'Project shell is missing from the actual tool offer');
      return tool(res,'run_project_shell',{
        resource_id:approvalResource,argv:['git','--version'],outside_reason:'核对本机 Git 版本',
      },'github-workroom-shell');
    } catch(error) {
      fault=String(error.message);
      if(!res.headersSent) res.writeHead(500);
      res.end();
    }
  });
  fixture.listen(0,'127.0.0.1');
  await once(fixture,'listening');
  return 'http://127.0.0.1:'+fixture.address().port;
}
function writeProfile(url) {
  const id='github-workroom-test';
  let model={name:'Local fixture',model:'fixture',base_url:url+'/v1',api_key:'local-fixture'};
  if (realProvider) {
    const file=process.env.HARNESS_TEST_PROVIDER_PROFILE;
    assert.ok(file,'Set HARNESS_TEST_PROVIDER_PROFILE to an authorized DeepSeek Flash profile');
    const source=JSON.parse(fs.readFileSync(file,'utf8').replace(/^\uFEFF/,''));
    const selected=source.profiles.find(item=>/deepseek.*flash/i.test(item.model||'') || /deepseek.*flash/i.test(item.name||''));
    assert.ok(selected?.api_key&&selected.base_url,'An authorized DeepSeek Flash profile is required');
    const {name,model:modelName,base_url,api_key}=selected;
    model={name,model:modelName,base_url,api_key};
  }
  fs.mkdirSync(data,{recursive:true});
  fs.writeFileSync(models,JSON.stringify({main:id,profiles:[{...model,id,context_window:131072,max_output_tokens:4096}]}));
}
async function projectThread() {
  const dir=path.join(out,'project');
  fs.mkdirSync(dir);
  fs.writeFileSync(path.join(dir,'readme.txt'),'A disposable GitHub workroom project.\n');
  const project=await request('/api/v2/projects',{name:'GitHub 工作间验收'});
  const resources='/api/v2/projects/'+project.id+'/resources';
  const resource=await request(resources,{kind:'code',path:dir});
  approvalResource=resource.id;
  await request(resources+'/'+resource.id+'/permission',{permission:'read',write:true,execute:true,confirm:true});
  const thread=await request(threads,{title:'GitHub 共享会话',projectId:project.id});
  await request(threads+'/'+thread.id+'/scope',{projectId:project.id,resourceIds:[resource.id]});
  const toggle='[data-project-group="'+project.id+'"] .sidebar-project-toggle';
  await wait(() => visible(toggle),5500);
  if(await driver.evaluate(node(toggle)+'.getAttribute("aria-expanded")==="false"')) await press(toggle);
  return thread;
}
function localGit(dir,...args) {
  return execFileSync(testEnvironment.HARNESS_GIT,['-C',dir,...args],{encoding:'utf8',windowsHide:true}).trim();
}
async function checkoutProject(remote, withSecond=false) {
  const p=await request('/api/v2/projects',{name:'GitHub 检出验收'});
  const resources='/api/v2/projects/'+p.id+'/resources', ids=[], directories=[];
  for (const [name,origin] of [['first',remote],...(withSecond?[['second','git@github.com:electron/electron.git']]:[])]) {
    const dir=path.join(out,name);
    fs.mkdirSync(path.join(dir,'Modules'),{recursive:true});
    fs.writeFileSync(path.join(dir,'Modules','_datetimemodule.c'),'/* The acceptance checkout intentionally differs from the PR. */\n');
    localGit(dir,'init','-b','fixture');
    localGit(dir,'add','.');
    localGit(dir,'-c','user.name=Modot Fixture','-c','user.email=fixture@example.invalid','commit','-m','Fixture');
    localGit(dir,'remote','add','origin',origin);
    const r=await request(resources,{kind:'code',path:dir});
    await request(resources+'/'+r.id+'/permission',{permission:'read',confirm:true});
    ids.push(r.id);directories.push(dir);
  }
  const owner=await request(threads,{title:'GitHub PR 验收会话',projectId:p.id});
  await request(threads+'/'+owner.id+'/scope',{projectId:p.id,resourceIds:ids});
  const toggle='[data-project-group="'+p.id+'"] .sidebar-project-toggle';
  await wait(()=>visible(toggle),5500);
  if(await driver.evaluate(node(toggle)+'.getAttribute("aria-expanded")==="false"'))await press(toggle);
  return {owner,project:p,ids,directories,resources};
}
const bindingRoute='/api/v2/github-workroom/binding';
const bindingFor=owner=>request(bindingRoute+'?conversationId='+owner.id);
const changeBinding=(owner,view,selected)=>request(bindingRoute,{
  conversationId:owner.id,scopeRevision:view.scopeRevision,revision:view.revision,selected,
});
async function webTarget(needle,app=driver) {
  const target=await wait(async()=> {
    const rows=await (await fetch('http://127.0.0.1:'+app.cdpPort+'/json/list')).json();
    return rows.find(item=>item.url.includes(needle)&&item.webSocketDebuggerUrl);
  },12000);
  return connect(target.webSocketDebuggerUrl);
}
async function pageValue(needle,expression,app=driver) {
  const web=await webTarget(needle,app);
  try {
    const value=await web.call('Runtime.evaluate',{expression,returnByValue:true});
    return value.result.value;
  } finally {web.socket.close();}
}
function visibleGitHubViews() {
  if(!fs.existsSync(liveViews))return [];
  return fs.readdirSync(liveViews).filter(name=>name.endsWith('.json')).flatMap(name=>{
    try {
      const {active,version,status,page,id,receipt}=JSON.parse(fs.readFileSync(path.join(liveViews,name),'utf8'));
      return [{id,active,version,status,url:page?.url,receipt}];
    } catch {return [];}
  });
}
function waitForPR(url) {
  return wait(()=>visibleGitHubViews().some(view=>view.active&&view.status==='ready'&&view.url===url),15000);
}
async function send(text) {
  await driver.type(text);
  await press(chat+' #go:not(:disabled)');
}
async function hasText(selector,marker,timeout=12000) {
  await wait(async () => (await read(selector)).includes(marker),timeout)
    .catch(() => {throw Error('Missing '+marker+' in '+selector);});
}
async function readyCodeRoom() {
  await press(code);
  const toggle='[data-surface="workbench"] [aria-controls="workroom-main-chat"]';
  await wait(() => visible(toggle),6500);
  if(!await visible('#workroom-main-chat')) await press(toggle);
  await wait(()=>visible('#workroom-main-chat #agent-composer'),7000);
}
async function narrowOtherWindow() {
  const otherOut=path.join(out,'second-window');
  const otherProfile=path.join(otherOut,'profile');
  const other=new Driver({exe,out:otherOut,profile:otherProfile,entry,env:{
    ...testEnvironment,HARNESS_DATA:path.join(otherProfile,'data'),
  }});
  try {
    await other.start();
    await other.page.call('Emulation.setDeviceMetricsOverride',{width:710,height:720,deviceScaleFactor:1,mobile:false});
    const thread=await request(threads,{title:'另一窗口的普通会话'},other);
    await select(thread,other);
    await openChat(other);
    await other.type('ONLY_SECOND_WINDOW');
    assert.equal(await draft(other),'ONLY_SECOND_WINDOW');
    assert.equal(await draft(),'KEEP_GITHUB_DRAFT');
    assert.ok(!await visible(browser+' .browser-content',other),'Narrow chat must not be covered by the native browser view');
    await press(chat+' [aria-label="收起工作间会话"]',other);
    await wait(() => visible(browser+' .browser-content',other),6000);
    await openChat(other);
    assert.equal(await draft(other),'ONLY_SECOND_WINDOW');
    result.checks.push('Two isolated windows retain their own drafts; a narrow window switches between browser and chat.');
  } finally {await other.abort();}
}
async function runFixed(url) {
  const owner=await projectThread();
  const other=await request(threads,{title:'另一会话'});
  await select(owner);
  await openChat();
  await navigate(repository);
  await browserTitle('electron/electron');
  await wait(()=>pageValue('/electron/electron',`(()=>{const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
    for(let text=walker.nextNode();text;text=walker.nextNode()){
      if(text.textContent.trim()!=='electron')continue;
      const range=new Range();range.selectNodeContents(text);if(!range.getBoundingClientRect().width)continue;
      const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
      if(selection.toString())return true;
    }
    return false;
  })()`),8000);
  await press(browser+' button[aria-label="引用页面或选中文字"]:not(:disabled)');
  await wait(async () => /github.com\/electron\/electron/.test(await draft()),6000);
  const excerpt=await draft();
  assert.match(excerpt,/网页引用/);
  await send('GH_FIXTURE_STREAM\n'+excerpt);
  await wait(()=>pendingStream||fault,12000);
  assert.equal(fault,'');
  await hasText(chat+' #feed','GH_FIXTURE_ALPHA');
  const paragraphs=Array.from({length:22},(_,i)=>'第 '+(i+1)+' 行用于验证增加内容时的滚动位置。').join('\n\n');
  packet(pendingStream,{content:'\n\n'+paragraphs+'\n\nGH_FIXTURE_BETA'});
  await hasText(chat+' #feed','GH_FIXTURE_BETA');
  await driver.type('KEEP_GITHUB_DRAFT');
  const scrollSnapshot=()=>driver.evaluate(
    '({top:reviewedFeed.scrollTop,remaining:reviewedFeed.scrollHeight-reviewedFeed.clientHeight-reviewedFeed.scrollTop})');
  const point=await driver.evaluate(`(()=>{window.reviewedFeed=document.querySelector("#feed");
    const rect=reviewedFeed.getBoundingClientRect();
    return {x:Math.round(rect.x+rect.width/2),y:Math.round(rect.y+rect.height/2),
      distance:Math.max(100,reviewedFeed.scrollTop-110)};})()`);
  await driver.page.call('Input.dispatchMouseEvent',{type:'mouseWheel',x:point.x,y:point.y,deltaY:-point.distance,deltaX:0});
  await wait(()=>driver.evaluate('reviewedFeed.scrollTop>40&&reviewedFeed.scrollHeight-reviewedFeed.clientHeight-reviewedFeed.scrollTop>80'),6000);
  result.readingPosition={github:await scrollSnapshot()};
  await press(normal);
  await hasText('[data-surface="agent"] #feed','GH_FIXTURE_BETA');
  result.readingPosition.conversation=await scrollSnapshot();
  await readyCodeRoom();
  await hasText('#workroom-main-chat #feed','GH_FIXTURE_BETA');
  result.readingPosition.code=await scrollSnapshot();
  await openChat();
  result.readingPosition.returned=await scrollSnapshot();
  assert.ok(await driver.evaluate('document.querySelector("#github-main-chat #feed")===window.reviewedFeed'));
  assert.ok(await driver.evaluate('reviewedFeed.scrollHeight-reviewedFeed.clientHeight-reviewedFeed.scrollTop>60'),'Reading position returned to the bottom while switching');
  assert.equal(await draft(),'KEEP_GITHUB_DRAFT');
  await browserTitle('electron/electron');
  finish(pendingStream,'\n\nGH_FIXTURE_FINAL');
  await hasText(chat+' #feed','GH_FIXTURE_FINAL');
  await wait(async () => !(await request(threads+'/'+owner.id+'/progress')).runtime.active,10000);
  assert.equal((await read(chat+' #feed')).split('GH_FIXTURE_FINAL').length-1,1);
  assert.equal(calls,1);
  result.checks.push('A sourced web quote, live stream, saved draft and reading position survive all three surfaces.');

  await narrowOtherWindow();
  await send('GH_FIXTURE_APPROVAL');
  await hasText(chat+' '+approval,'核对本机 Git 版本',20000);
  await press(normal);
  await select(other);
  assert.ok(!await visible(approval),'Approval appeared in a different conversation');
  await select(owner);
  await readyCodeRoom();
  assert.ok(await visible('#workroom-main-chat '+approval));
  await openChat();
  assert.ok(await visible(chat+' '+approval));
  await press(chat+' '+approval+' button:first-of-type:not([disabled])');
  await hasText(chat+' #feed','GH_APPROVAL_DONE',18000);
  result.checks.push('The real backend approval follows the owning conversation and resumes only after approval.');

  await navigate(url+'/recoverable');
  await hasText(browser,'请在系统浏览器中打开站外地址',8000);
  assert.equal(await driver.evaluate(node(browser+' input')+'.value'),url+'/recoverable');
  await navigate('https://github.com/electron');
  await browserTitle('Electron');
  await press(browser+' [aria-label="后退"]:not([disabled])');
  await browserTitle('electron/electron');
  await press(browser+' [aria-label="收起浏览器"]');
  assert.ok(!await visible(browser+' .browser-content'));
  await driver.click('打开 GitHub');
  await wait(()=>visible(browser+' .browser-content'),6000);
  await driver.click('账号与连接');
  await wait(()=>visible(room+' .plugin-home'),6000);
  assert.ok(!await visible(browser+' .browser-content'));
  assert.ok(/GitHub/.test(await read(room+' .plugin-home')));
  result.checks.push('GitHub keeps its own site boundary and back navigation, releases the tab and returns to account controls.');

  await driver.click('网页');
  await navigate(publicPR);
  await waitForPR(publicPR);
  await send('GH_STALE_VIEW');
  await hasText(chat+' '+approval,'api.github.com',15000);
  await navigate(repository);
  await browserTitle('electron/electron');
  const eventsRoute=threads+'/'+owner.id+'/events?limit=120';
  await wait(async()=> (await request(eventsRoute)).events.some(row=>row.act==='action'
    && row.tool==='read_current_github_pr'&&row.state==='failed'&&row.impact?.code==='approval_revoked'),12000);
  result.checks.push('Changing the PR while the read awaits approval revokes the original request and leaves the new page alone.');

  const host=body=>driver.evaluate('window.workbench.browser('+JSON.stringify(body)+')');
  const generic=await host({operation:'open',url:url+'/recoverable'});
  await wait(async()=> (await host({operation:'state',id:generic.id})).loadFailed,8000);
  recoverable=true;
  await host({operation:'refresh',id:generic.id});
  await wait(async()=> (await host({operation:'state',id:generic.id})).title==='GitHub workspace retry fixture',8000);
  await host({operation:'close',id:generic.id});
  result.checks.push('An ordinary isolated HTTP tab can still retry a failed localhost preview.');
}
async function runBinding() {
  const setup=await checkoutProject('https://github.com/python/cpython.git',true);
  const {owner,ids,directories,resources}=setup;
  localGit(directories[0],'remote','add','upstream','ssh://git@github.com/pallets/flask.git');
  localGit(directories[0],'remote','add','with-credentials','https://fixture:pretend-password@github.com/example/test.git');
  const initial=await bindingFor(owner);
  assert.equal(initial.candidates.length,3);
  assert.equal(initial.suggested,null);
  assert.ok(!JSON.stringify(initial).includes('pretend-password'));
  const trees=threads+'/'+owner.id+'/worktree-session';
  const handoff=await request(trees+'?resource='+ids[0]);
  assert.deepEqual(handoff.blocked,[]);
  assert.equal((await request(trees,{confirm:true,resourceId:ids[0],token:handoff.token})).state,'switched');
  const isolated=await bindingFor(owner);
  const target=isolated.candidates.find(row=>row.repository==='python/cpython');
  assert.equal(target.checkout,'worktree');
  assert.notEqual(target.resourceId,ids[0]);
  await select(owner);
  await openChat();
  const chooser='select[aria-label="绑定 GitHub 仓库"]';
  await wait(()=>driver.evaluate(node(chooser)+'?.options.length===4'));
  await driver.evaluate(`${node(chooser)}.value=${JSON.stringify(target.id)};${node(chooser)}.dispatchEvent(new Event('change',{bubbles:true}));`);
  await wait(async()=> (await bindingFor(owner)).selected?.id===target.id);
  await wait(()=>driver.evaluate(node(chooser)+'.value==='+JSON.stringify(target.id)));
  await driver.click('打开已绑定仓库');
  await browserTitle('python/cpython');
  await navigate(repository);
  await browserTitle('electron/electron');
  assert.equal((await bindingFor(owner)).selected.repository,'python/cpython');
  result.checks.push('Two repositories, several fetch remotes and the real linked worktree are selectable; browsing preserves the binding.');
  const cookieName='modot_acceptance_profile';
  const cookie={url:repository,name:cookieName,value:'isolated-fixture',secure:true,expires:(Date.now()+3600000)/1000};
  async function hasCookie(app=driver,write=false){
    const web=await webTarget('/electron/electron',app);
    try {
      if(write)assert.ok((await web.call('Network.setCookie',cookie)).success);
      return (await web.call('Network.getCookies',{urls:[repository]})).cookies.some(row=>row.name===cookieName);
    } finally {web.socket.close();}
  }
  assert.ok(await hasCookie(driver,true));
  await press(browser+' [aria-label="收起浏览器"]');
  await driver.click('打开 GitHub');
  await navigate(repository);
  await browserTitle('electron/electron');
  assert.ok(await hasCookie());
  const otherOut=path.join(out,'other-profile'),otherProfile=path.join(otherOut,'profile');
  const other=new Driver({exe,out:otherOut,profile:otherProfile,entry,env:{...testEnvironment,HARNESS_DATA:path.join(otherProfile,'data')}});
  try {
    await other.start();
    await press(rail,other);
    await fill(browser+' input',repository,other);
    await other.evaluate(node(browser+' input')+'.form.requestSubmit()');
    assert.equal(await hasCookie(other),false);
  } finally {await other.abort();}
  await driver.close();
  await driver.start();
  await select(owner);
  await openChat();
  await navigate(repository);
  await browserTitle('electron/electron');
  assert.ok(await hasCookie());
  assert.equal((await bindingFor(owner)).selected.id,target.id);
  await driver.click('清除网页登录');
  await hasText(room,'已清除这个档案的 GitHub 网页登录');
  await driver.click('打开 GitHub');
  await navigate(repository);
  await browserTitle('electron/electron');
  assert.equal(await hasCookie(),false);
  result.checks.push('Real native browser storage survives close and restart in one profile, has no cookie in another profile and clears on sign-out.');
  await request(resources+'/'+ids[0]+'/permission',{permission:'none',confirm:true});
  const revoked=await bindingFor(owner);
  assert.equal(revoked.selected,null);
  assert.ok(revoked.expired);
  const late=await driver.request(bindingRoute,{conversationId:owner.id,scopeRevision:isolated.scopeRevision,
    revision:initial.revision,selected:target.id});
  assert.equal(late.status,409);
  result.checks.push('Revoking the checkout invalidates the binding and rejects a late selection.');
}
async function runPR() {
  const repoUrl=publicPR.split('/pull/')[0];
  const {owner}=await checkoutProject(repoUrl+'.git');
  const view=await bindingFor(owner);
  assert.ok(view.suggested);
  await changeBinding(owner,view,view.suggested.id);
  await select(owner);
  await openChat();
  await navigate(publicPR);
  await waitForPR(publicPR);
  const run=threads+'/'+owner.id+'/progress';
  async function ask(message) {
    const observations=[];
    let last='', lastAt=0;
    let selectedCheck;
    await send(message);
    const started=Date.now(),approved=new Set();
    let wasActive=false;
    await wait(async()=>{
      if(await visible(chat+' '+approval)) {
        const allowed=await driver.evaluate(`(()=>{const panel=${node(chat+' '+approval)};const txt=panel?.innerText||"";
          if(!/github\\.com/i.test(txt))return "";
          const button=panel.querySelector('button:first-of-type:not([disabled])');if(!button)return "";
          button.click();return txt;})()`);
        if(allowed)approved.add(allowed.includes('api.github.com')?'REST':'website');
      }
      const progress=await request(run);
      if(Date.now()-lastAt>200){
        lastAt=Date.now();
        const views=visibleGitHubViews();
        const state=JSON.stringify(views);
        if(state!==last&&observations.length<30){observations.push({atMs:Date.now()-started,views});last=state;}
        const check=views.find(view=>view.active&&view.receipt?.url===view.url&&view.url?.startsWith(publicPR+'/checks?check_run_id='));
        if(!selectedCheck&&check) {
          const selected=await pageValue(check.url,`(()=>({url:location.href,label:document.querySelector('#js-selected-check-run')?.innerText?.slice(0,220)}))()`);
          if(selected?.url===check.url&&selected.label)selectedCheck=selected;
        }
      }
      wasActive ||= progress.runtime.active;
      return wasActive&&!progress.runtime.active;
    },120000);
    return {durationMs:Date.now()-started,approved:[...approved],observations,selectedCheck};
  }
  result.provider='DeepSeek Flash';
  result.pr=publicPR;
  result.first=await ask('这个 PR 为什么红了？');
  const feed=await read(chat+' #feed');
  result.reply=feed.slice(-2600);
  assert.match(feed,/All required checks pass|失败|failure/);
  assert.ok(result.first.approved.includes('REST'),'No request for GitHub fact reading reached the approval UI');
  if(!result.first.selectedCheck) {
    result.explicitNavigation=await ask('请在这个 PR 的 Checks 页定位刚才发现的失败检查。');
  }
  const selected=result.first.selectedCheck||result.explicitNavigation?.selectedCheck;
  assert.ok(selected?.url.startsWith(publicPR+'/checks?check_run_id=')&&selected.label,'The native browser did not confirm a specific check');
  result.selected=selected;
  result.checks.push('The real model read the current public PR and the native browser verified the exact selected check.');
  result.files=await ask('请把当前 PR 切到 Files changed。');
  await wait(async()=> (await driver.evaluate(node(browser+' input')+'.value')).split('?')[0]===publicPR+'/files',12000);
  result.checks.push('The native browser confirmed Files changed on the same PR.');
  result.local=await ask('在代码工作间打开这个 PR 对应的本地文件 Modules/_datetimemodule.c，并告诉我本地版本和 PR 的提交是否一致。');
  await hasText('[data-surface="workbench"]','当前检出与 PR 提交不同',20000);
  assert.ok((await read('[data-surface="workbench"]')).includes('_datetimemodule.c'));
  result.checks.push('The local file opened in the actual code workroom, with a visible checkout mismatch.');
}
async function runReal() {
  const owner=await request(threads,{title:'DeepSeek Flash GitHub 工作间实时输出'});
  await select(owner);
  await openChat();
  result.provider='DeepSeek Flash';
  const begun=Date.now();
  await send('请直接输出纯文本。第一行仅写 GH_REAL_STREAM_START，随后依次写二十四行，各用一句话说明可读代码的具体习惯。最后一行仅写 GH_REAL_STREAM_END。');
  const agentText=()=>driver.evaluate(
    '[...document.querySelectorAll("#github-main-chat #feed .msg.agent")].map(row=>row.innerText).join("\\n")');
  await wait(async () => (await agentText()).includes('GH_REAL_STREAM_START'),120000);
  const first=await agentText();
  result.firstTextMs=Date.now()-begun;
  assert.ok(!first.includes('GH_REAL_STREAM_END'),'Final marker arrived with the first visible output');
  await wait(async () => (await agentText()).includes('GH_REAL_STREAM_END'),120000);
  await wait(async () => !(await request(threads+'/'+owner.id+'/progress')).runtime.active,15000);
  result.finishedMs=Date.now()-begun;
  assert.equal((await agentText()).split('GH_REAL_STREAM_END').length-1,1);
  result.checks.push('DeepSeek Flash streamed first and last markers into the GitHub chat, without switching surfaces.');
}

(async () => {
  const url=mode==='fixed'?await startFixture():'';
  if(realProvider||mode==='fixed') writeProfile(url);
  await driver.start();
  if(mode==='public') await browse();
  if(mode==='fixed') await runFixed(url);
  if(mode==='real') await runReal();
  if(mode==='binding') await runBinding();
  if(mode==='pr') await runPR();
  assert.equal(fault,'');
  result.passed=true;
})().catch(error=>{
  result.passed=false;
  result.error=(fault||String(error.message)).slice(0,400);
  process.exitCode=1;
}).finally(async()=>{
  await driver.abort();
  if(fixture) {fixture.closeAllConnections();fixture.close();}
  if(realProvider&&fs.existsSync(models)) fs.unlinkSync(models);
  const file=path.join(out,'result.json');
  fs.writeFileSync(file,JSON.stringify(result,null,2));
  console.log(JSON.stringify({mode,passed:result.passed,checks:result.checks,error:result.error}));
  console.log(file);
});
