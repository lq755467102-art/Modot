// Exercise the Preview catalog with real installed manifests in hidden Electron.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {Driver, wait} = require('./electron_cdp.cjs');

const {HARNESS_TEST_ROOT, HARNESS_TEST_ELECTRON, HARNESS_TEST_PYTHON} = process.env;
assert.ok(HARNESS_TEST_ROOT && HARNESS_TEST_ELECTRON && HARNESS_TEST_PYTHON);
const out = fs.mkdtempSync(path.join(path.resolve(HARNESS_TEST_ROOT), 'preview-plugin-filters-'));
const profile = path.join(out, 'profile');
const root = path.join(__dirname, '..');
const driver = new Driver({exe:HARNESS_TEST_ELECTRON, out, profile, entry:path.join(root,'desktop'),
  env:{HARNESS_DATA:path.join(profile,'data'), HARNESS_PYTHON:HARNESS_TEST_PYTHON, HARNESS_GIT:'git'}});

const checks = [];
const result = {checks};
const filters = '.catalog-filters[aria-label="筛选插件"]';
const rows = '.catalog-grid [data-catalog-key]';
const search = 'input[aria-label="搜索插件"]';
const query = selector => 'document.querySelector('+JSON.stringify(selector)+')';

async function post(route, body) {
  const reply = await driver.request(route, body);
  assert.equal(reply.status, 200, reply.body?.code || route);
  return reply.body;
}
async function install(directory) {
  const pkg = await post('/api/v2/plugins/preview', {path:directory});
  return post('/api/v2/plugins/install', {path:directory, fingerprint:pkg.fingerprint});
}
async function filter(name) {
  const names = JSON.stringify(filters+' button');
  const label = JSON.stringify(name);
  await driver.evaluate(`[...document.querySelectorAll(${names})].find(button=>button.textContent===${label}).click()`);
  await wait(()=>driver.evaluate(query(filters+' [aria-pressed=true]')+'?.textContent==='+label),4000);
}
const hasPackage = id => driver.evaluate('!!'+query(rows+'[data-catalog-key="'+id+'"]'));

async function run() {
  await driver.start();
  const technical = path.join(out,'fact-only');
  fs.mkdirSync(technical);
  fs.writeFileSync(path.join(technical,'plugin.json'),JSON.stringify({
    name:'catalog-fact-fixture', version:'1.0.0', runtimeFacts:[{name:'sample', value:{source:'fixture'}}],
  }));
  const fact = await install(technical);
  const studio = await install(path.join(root,'examples/plugin-catalog/studio-v1'));
  const notes = await install(path.join(root,'examples/memory-notes'));
  const scout = await install(path.join(root,'examples/worker-scout'));
  const skill = await install(path.join(root,'examples/plugin-catalog/legacy-skill'));
  await driver.evaluate(query('[data-builtin-app="extensions"]')+'.click()');
  await wait(()=>hasPackage(fact.id));
  const labels = await driver.evaluate('[...document.querySelectorAll('+JSON.stringify(filters+' button')+')].map(button=>button.textContent)');
  assert.deepEqual(labels,['全部','技能','工具','委派','记忆','应用']);
  for(const [kind,pkg] of [['技能',skill],['工具',studio],['委派',scout],['记忆',notes],['应用',studio]]) {
    await filter(kind);
    assert.ok(await hasPackage(pkg.id), kind+' lost its installed sample');
    assert.ok(!await hasPackage(fact.id),kind+' included an unrelated contribution');
  }
  assert.ok(await driver.evaluate('document.querySelector(".catalog-page").innerText.includes("代码工作间")'));
  assert.ok(await driver.evaluate('!!document.querySelector(\'[data-plugin-application="github"][data-available=true]\')'));
  checks.push('六个一级筛选均可用，代码工作间与 GitHub 应用仍可找到。');
  await filter('全部');
  await driver.evaluate(`(()=>{
    const input=${query(search)};
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'运行事实');
    input.dispatchEvent(new Event('input',{bubbles:true}));
  })()`);
  await wait(async()=>(await hasPackage(fact.id))&&!(await hasPackage(scout.id)),4000);
  await driver.evaluate(query(rows+'[data-catalog-key="'+fact.id+'"]')+'.click()');
  await wait(()=>driver.evaluate('document.querySelector(".catalog-detail")?.innerText.includes("运行事实")'));
  checks.push('纯运行事实插件可用旧术语搜到，技术贡献仍在包详情。');
}

run().then(()=>{result.passed=true;}).catch(error=>{
  result.passed=false;
  result.error=error.message;
  process.exitCode=1;
}).finally(async()=>{
  await driver.abort();
  const report=path.join(out,'result.json');
  fs.writeFileSync(report,JSON.stringify(result,null,2));
  console.log(report);
  console.log(JSON.stringify(result));
});
