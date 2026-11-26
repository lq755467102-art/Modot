const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { setTimeout: sleep } = require('node:timers/promises');

const read = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const call = (name, args = {}) => ({ name, args });
const unadvertisedCall = (name, args) => ({ name, args, unadvertised: true });
const names = body => body.tools.map(t => t.function.name);
const last = body => JSON.parse(body.messages.findLast(m => m.role === 'tool').content);
const text = body => JSON.stringify(body.messages);
const checkWorkerMethod = '在获准的隔离副本上实际运行一次固定检查。';
const samples = Object.fromEntries(['planner', 'scout'].map(role => {
  const dir = path.join(__dirname, '..', 'examples', 'worker-' + role);
  return [role, { dir, worker: read(path.join(dir, 'plugin.json')).workers[0], ...read(path.join(dir, 'example.json')) }];
}));

function final(res, message) {
  const event = { choices: [{ index: 0, delta: { content: message }, finish_reason: 'stop' }] };
  res.writeHead(200, { 'Content-Type': 'text/event-stream' });
  res.end('data: ' + JSON.stringify(event) + '\n\ndata: [DONE]\n\n');
}

function notice(body, delegation) {
  for (const m of body.messages) {
    if (m.role !== 'user' || !m.content.startsWith('[worker-result:')) continue;
    const value = JSON.parse(m.content.slice(m.content.indexOf('{')));
    if (value.delegation_id === delegation) return value.result_id;
  }
  assert.fail('missing model-visible result notice for ' + delegation);
}

function facts(body) {
  const m = body.messages.find(m => m.content?.startsWith?.('[context-update:runtime-facts:'));
  return JSON.parse(m.content.slice(m.content.indexOf('\n{') + 1));
}

function comparableRequest(request) {
  const body = structuredClone(request);
  for (const m of body.messages) {
    if (m.content?.startsWith?.('[context-update:runtime-facts:')) {
      const value = facts({ messages: [m] });
      assert.match(value.scope.taskId, /^[a-f0-9]{32}$/);
      value.scope.taskId = '<current task>';
      m.content = m.content.slice(0, m.content.indexOf('\n{')).replace(/:([a-f0-9]{64})\]/, ':<snapshot digest>]') + '\n' + JSON.stringify(value);
    } else if (m.role === 'tool') {
      const receipt = JSON.parse(m.content);
      for (const k of ['runId', 'actionId']) {
        assert.match(receipt._harnessEvidence[k], /^[a-f0-9]{32}$/);
        receipt._harnessEvidence[k] = '<' + k + '>';
      }
      m.content = JSON.stringify(receipt);
    }
  }
  return body;
}

module.exports = async ({ win, js, wait, request, out, passed, setFixture }) => {
  const runs = path.join(out, 'data', 'runs');
  const wire = [], timeline = [], cases = {}, faults = [], joinTimings = {};
  const mark = name => timeline.push({ name, time: Date.now() });
  const get = async p => (await js(`window.workbench.request({path:${JSON.stringify(p)}})`)).body;
  const post = async (p, body) => {
    const r = await request(p, body);
    assert.equal(r.status, 200, JSON.stringify(r.body));
    return r.body;
  };
  const rows = threadId => {
    const dir = path.join(runs, 'workbench', 'subtasks', threadId);
    return fs.existsSync(dir) ? fs.readdirSync(dir).filter(f => f.endsWith('.json')).map(f => read(path.join(dir, f))) : [];
  };
  async function until(condition, label) {
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      if (faults.length) throw Error(faults[0]);
      if (condition()) return;
      await sleep(40);
    }
    throw Error('fixture barrier timed out: ' + label);
  }

  async function run(id, title, prompt, handlers, project) {
    const thread = await post('/api/v2/conversations/threads', { title, projectId: project?.id });
    cases[id] = thread.id;
    const route = '/api/v2/conversations/threads/' + thread.id;
    if (project) await post(route + '/scope', { projectId: project.id, resourceIds: [project.resource.id] });
    const config = await get(route + '/execution');
    await post(route + '/execution', { expectedRevision: config.next.revision, crew: 'l0', charter: 'off', maxTurns: 0, modelCalls: 0, contextLimit: 131072 });
    const sequences = new Map();
    let callNumber = 0;
    setFixture((body, res, tool) => {
      const system = body.messages[0].content;
      const role = (system.includes(checkWorkerMethod) ? 'checkWorker' : null)
        || Object.keys(samples).find(k => system.includes(samples[k].worker.method))
        || (system.includes('你在独立隔离子任务中工作') ? 'native' : 'parent');
      const callId = 'example-' + (++callNumber);
      wire.push({ case: id, role, body });
      (async () => {
        assert.ok(handlers[role], 'unexpected ' + role + ' request during ' + id);
        if (!sequences.has(role)) sequences.set(role, handlers[role](body, thread.id));
        const { done, value } = await sequences.get(role).next(body);
        if (res.destroyed) return;
        if (value?.upstreamFailure) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: { message: value.upstreamFailure, type: 'invalid_request_error' } }));
        } else if (done) {
          assert.equal(typeof value, 'string', 'unexpected extra request after scripted completion');
          final(res, value);
        } else {
          assert.equal(names(body).includes(value.name), !value.unadvertised, `unexpected visibility in ${role} request: ${value.name}`);
          tool(res, value.name, value.args, callId);
        }
      })().catch(e => { faults.push(e.stack); if (!res.headersSent) final(res, 'FIXTURE_ERROR'); });
      return true;
    });
    try {
      await post(route + '/send', { text: prompt });
      await wait(`window.workbench.request({path:${JSON.stringify(route + '/progress')}}).then(r=>!r.body.runtime.active&&!['starting','running'].includes(r.body.runtime.state))`, 130000);
      assert.deepEqual(faults, []);
      assert.equal((await get(route + '/progress')).runtime.state, 'completed', id);
      return thread.id;
    } finally { setFixture(null); }
  }

  const ordinaryPrompt = '核对当前会话可用的工作间、记忆和既有技能。';
  async function* ordinary(body) {
    assert.ok(!names(body).includes('inspect_worker_plugins'));
    for (const s of Object.values(samples)) assert.ok(!text(body).includes(s.worker.method));
    body = yield call('use_tool_group', { group: 'workroom' });
    assert.ok(names(body).includes('test_workroom'));
    body = yield call('use_tool_group', { group: 'memory' });
    assert.ok(names(body).includes('remember') || names(body).some(n => n.includes('memory')));
    body = yield call('load_skill', { name: 'native-child-fixture' });
    assert.ok(JSON.stringify(last(body)).includes('NATIVE_SKILL_BODY'));
    return '工作间、记忆和技能可用。';
  }

  async function install(s) {
    const preview = await post('/api/v2/plugins/preview', { path: s.dir });
    const p = await post('/api/v2/plugins/install', { path: s.dir, fingerprint: preview.fingerprint });
    assert.equal(p.workersEnabled, false);
    return post('/api/v2/plugins/' + p.id, { expectedRevision: p.revision, workersEnabled: true, confirm: true });
  }

  try {
    await run('baseline', '00 · 默认 L0 对照', ordinaryPrompt, { parent: ordinary });
    const planner = await install(samples.planner);
    await run('plannerOnly', '01 · 只发现规划者，可以直接结束', '查看本轮任务核心目录，无需委派。', { parent: async function* () {
      yield call('use_tool_group', { group: 'plugins' });
      const body = yield call('inspect_worker_plugins');
      assert.deepEqual(last(body).workers.map(w => w.worker), [planner.id + '/plan']);
      return '已查看目录，当前任务可直接结束。';
    } });
    let plannerStarted = false;
    await run('closing', '01b · 规划仍在途，执行者独立收尾', '可委派规划；主任务已有结论时直接结束。', {
      parent: async function* () {
        yield call('use_tool_group', { group: 'plugins' });
        const directory = yield call('inspect_worker_plugins');
        const delegated = yield call('delegate_plugin_worker', { worker: last(directory).workers[0].worker, request_key: 'planning', input: samples.planner.input, resource_ids: [], kind: 'report' });
        assert.ok(last(delegated).delegation_id);
        await until(() => plannerStarted, 'planner request reached provider');
        return '主任务可独立结束，尚无规划成果。';
      },
      planner: async function* (body, threadId) {
        assert.ok(names(body).includes('submit_worker_result'));
        plannerStarted = true;
        await until(() => rows(threadId).some(r => r.stopRequested), 'parent closes before planner responds');
        return '已收到停止请求。';
      },
    });
    await until(() => rows(cases.closing)[0]?.state === 'cancelled', 'child cancellation acknowledged');
    const closing = rows(cases.closing)[0];
    assert.equal(closing.stopCode, 'parent_ended'); assert.equal(closing.worker.results.length, 0);
    passed.push('parent final with planner request in flight: parent completed, active child cancelled with parent_ended and no fabricated result');
    const scout = await install(samples.scout);
    await run('ordinaryEnabled', '02 · 两个样例均可用，独立处理', ordinaryPrompt, { parent: ordinary });
    assert.equal(rows(cases.plannerOnly).length + rows(cases.ordinaryEnabled).length, 0);
    passed.push('discovery and enabled samples do not dispatch either assistant');

    const root = path.join(out, 'export-project');
    fs.mkdirSync(root);
    const file = 'export.cfg';
    fs.writeFileSync(path.join(root, file), 'default=legacy\n');
    fs.writeFileSync(path.join(root, 'export_api.py'), 'from pathlib import Path\n\ndef public_format(explicit=None):\n    if explicit is not None:\n        return explicit\n    return Path(__file__).with_name("export.cfg").read_text().strip().split("=", 1)[1]\n');
    fs.writeFileSync(path.join(root, 'export_cli.py'), 'from export_api import public_format\n\nif __name__ == "__main__":\n    print(public_format("legacy"))\n');
    const notes = path.join(root, 'user-notes.txt');
    fs.writeFileSync(notes, 'user notes\n');
    for (const args of [['init', '-b', 'agent/official-samples'], ['add', '.'], ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'initial']]) {
      execFileSync(process.env.HARNESS_GIT, args, { cwd: root, windowsHide: true, stdio: 'ignore' });
    }
    const dirty = 'user notes\nUNCOMMITTED_HUMAN_TEXT\n';
    fs.writeFileSync(notes, dirty);
    const project = await post('/api/v2/projects', { name: '官方任务核心样例 P2 · 父任务内回收' });
    project.resource = await post(`/api/v2/projects/${project.id}/resources`, { kind: 'code', path: root });
    await post(`/api/v2/projects/${project.id}/resources/${project.resource.id}/permission`, { permission: 'read', write: true, execute: true, confirm: true });
    const resource = project.resource.id;
    const received = {}, held = new Set();
    let edited = false, oldRevision, newRevision;
    const configInquiry = { question: '配置声明的默认格式是什么？', scope: file, tried: [] };
    const configMissing = '调用方是否覆盖配置尚无证据。';
    const manage = (role, action, args = {}) => call('manage_plugin_worker', { delegation_id: received[role], action, ...args });
    const readParent = () => call('inspect_code', { resource_id: resource, relative_path: file });

    async function* parent(body, threadId) {
      body = yield readParent();
      oldRevision = last(body).revision;
      assert.equal(last(body).text, 'default=legacy\n');
      yield call('use_tool_group', { group: 'plugins' });
      body = yield call('inspect_worker_plugins');
      const directory = last(body).workers;
      assert.equal(directory.length, 2);
      for (const s of Object.values(samples)) assert.ok(!text(body).includes(s.worker.method));
      for (const role of ['scout', 'planner']) {
        const entry = directory.find(w => w.name === samples[role].worker.name);
        assert.deepEqual(entry.inputSchema, samples[role].worker.inputSchema);
        body = yield call('delegate_plugin_worker', { worker: entry.worker, request_key: 'explicit-' + role, input: role === 'scout' ? configInquiry : samples.planner.input, kind: 'report', resource_ids: role === 'scout' ? [resource] : [] });
        assert.equal(last(body).ok, true, JSON.stringify(last(body)));
        received[role] = last(body).delegation_id;
      }
      await until(() => held.size === 2, 'both actual assistant requests in flight');
      mark('parent-edit-requested');
      body = yield call('edit_project_file', { resource_id: resource, relative_path: file, old_text: 'default=legacy', new_text: 'default=modern', expected_revision: oldRevision });
      assert.equal(last(body).ok, true, JSON.stringify(last(body)));
      assert.equal(fs.readFileSync(path.join(root, file), 'utf8'), 'default=modern\n');
      assert.ok(rows(threadId).every(r => r.state === 'running' && !r.worker.results.length));
      mark('parent-edit-on-disk');
      edited = true;
      await until(() => rows(threadId).every(r => r.state === 'completed'), 'both submissions and child processes finished');
      body = yield readParent();
      assert.equal(last(body).text, 'default=modern\n');
      newRevision = last(body).revision;
      assert.notEqual(newRevision, oldRevision);
      const planResult = notice(body, received.planner), scoutResult = notice(body, received.scout);
      assert.ok(!text(body).includes(samples.planner.output.options[0].steps[0]));
      assert.ok(!text(body).includes(configMissing));
      assert.ok(rows(threadId).every(r => r.worker.results[0].disposition === null));
      body = yield manage('planner', 'result', { result_id: planResult });
      assert.deepEqual(last(body).data, samples.planner.output);
      yield manage('planner', 'receipt', { request_key: 'use-first-option', result_id: planResult, disposition: 'partial' });
      body = yield manage('scout', 'result', { result_id: scoutResult });
      assert.equal(last(body).data.findings[0].revision, oldRevision);
      assert.ok(last(body).data.findings[0].observation.includes('default=legacy'));
      body = yield manage('scout', 'receipt', { request_key: 'obsolete-observation', result_id: scoutResult, disposition: 'rejected' });
      assert.equal(last(body).ok, true);
      body = yield call('run_project_check', { resource_id: resource, purpose: 'exploration', timeout: 30, yield_time_ms: 30000,
        script: 'from runpy import run_path\nexport = run_path("export_api.py")["public_format"]\nassert export() == "modern"\nassert export("legacy") == "legacy"\nprint("PUBLIC_EXPORT_CHECKED")' });
      assert.equal(last(body).process?.exitCode, 0, JSON.stringify(last(body)));
      mark('parent-verified-selected-option');
      return '已改为 modern，并通过所选公共入口验证。显式 legacy 仍生效。探路结果对应旧文件版本。';
    }

    function checkChild(body, role) {
      assert.ok(names(body).includes('submit_worker_result'));
      assert.ok(!names(body).includes('delegate_plugin_worker'));
      assert.ok(!text(body).includes('PARENT_ONLY'));
      const other = role === 'scout' ? 'planner' : 'scout';
      assert.ok(!body.messages[0].content.includes(samples[other].worker.method));
      assert.ok(!text(body).includes(Object.values(samples[other].input)[0]));
      const submit = body.tools.find(t => t.function.name === 'submit_worker_result');
      assert.deepEqual(submit.function.parameters.properties.data, samples[role].worker.outputSchema);
    }
    async function* plan(body) {
      checkChild(body, 'planner');
      assert.deepEqual(facts(body).facts['host.workspace'].value, []);
      held.add('planner'); mark('planner-in-flight');
      await until(() => edited, 'parent persisted own edit');
      body = yield call('submit_worker_result', { submission_key: 'options', data: samples.planner.output });
      assert.equal(last(body).saved, true); mark('planner-published');
      return '已提交两条可选验证路线。';
    }
    async function* investigate(body) {
      checkChild(body, 'scout');
      const [source] = facts(body).facts['host.workspace'].value;
      assert.equal(source.write, false); assert.equal(source.execute, false); assert.notEqual(source.resourceId, resource);
      for (const denied of ['edit_project_file', 'write_project_file', 'run_project_check', 'record_working_summary']) {
        assert.ok(!names(body).includes(denied), 'read-only request advertises ' + denied);
      }
      body = yield call('use_tool_group', { group: 'workroom' });
      assert.deepEqual(last(body).tools, []);
      assert.ok(!names(body).includes('test_workroom'));
      body = yield call('inspect_code', { resource_id: source.resourceId, relative_path: file });
      const observed = last(body);
      assert.equal(observed.text, 'default=legacy\n'); assert.equal(observed.revision, oldRevision);
      body = yield unadvertisedCall('run_project_check', { resource_id: source.resourceId, script: 'print("MUST_NOT_RUN")', purpose: 'exploration' });
      assert.equal(last(body).ok, false); assert.equal(last(body).denied, true);
      body = yield unadvertisedCall('edit_project_file', { resource_id: source.resourceId, relative_path: file, old_text: 'default=legacy', new_text: 'default=unauthorized', expected_revision: observed.revision });
      assert.equal(last(body).ok, false);
      assert.equal(last(body).denied, true);
      assert.equal(fs.readFileSync(path.join(root, file), 'utf8'), observed.text);
      held.add('scout'); mark('scout-holds-observation');
      await until(() => edited, 'parent persisted own edit');
      const data = { findings: [{ observation: '配置含 default=legacy。', source: file + ':1', revision: observed.revision }], ruledOut: [], unknowns: [configMissing] };
      body = yield call('submit_worker_result', { submission_key: 'observations', data });
      assert.equal(last(body).saved, true); mark('scout-published');
      return '已提交观察及所见文件版本。';
    }

    await run('overlap', '03 · 执行者继续修改，规划部分采用，旧版探路结果拒绝', 'PARENT_ONLY：把导出默认值改为 modern，保留用户笔记。可委派规划与只读调查，自行决定是否采用。', { parent, planner: plan, scout: investigate }, project);
    const tasks = rows(cases.overlap);
    assert.equal(tasks.length, 2); assert.equal(new Set(tasks.map(r => r.childId)).size, 2);
    for (const role of ['planner', 'scout']) {
      const row = tasks.find(r => r.id === received[role]);
      assert.equal(row.kind, 'report'); assert.equal(row.state, 'completed');
      assert.equal(row.worker.results.length, 1);
      const result = row.worker.results[0];
      assert.ok(result.delivery.notifiedIn && result.delivery.includedIn && result.delivery.readActionId);
      assert.equal(result.disposition.value, role === 'planner' ? 'partial' : 'rejected');
      assert.equal(result.disposition.source.sessionId, cases.overlap);
      assert.equal(read(path.join(runs, row.childId, 'execution-runs', row.childRunId + '.json')).config.contextLimit, 131072);
    }
    assert.equal(fs.readFileSync(notes, 'utf8'), dirty);
    passed.push('parent guarded edit persisted while both children had unfinished requests; parent rejected stale evidence, partially adopted planning and verified both public calls in an isolated check');

    let callerReading = false, apiChanged = false;
    let deciding = false, firstResult, laterResult;
    await run('split', '06 · 委派一个调用方，主方继续修改接口', '调整导出接口的显式格式输入，同时查清旧 CLI 是否传固定格式。', {
      parent: async function* () {
        let body = yield call('inspect_code', { resource_id: resource, relative_path: 'export_api.py' });
        const api = last(body);
        assert.match(api.text, /return explicit/);
        yield call('use_tool_group', { group: 'plugins' });
        body = yield call('inspect_worker_plugins');
        const scoutEntry = last(body).workers.find(w => w.worker === scout.id + '/investigate');
        assert.match(scoutEntry.inputSchema.properties.question.description, /一个/);
        assert.match(scoutEntry.inputSchema.properties.scope.description, /主执行者/);
        assert.match(scoutEntry.inputSchema.properties.tried.description, /已查证据/);
        body = yield call('delegate_plugin_worker', { worker: scoutEntry.worker, request_key: 'cli', input: samples.scout.input, resource_ids: [resource], kind: 'report' });
        const delegation = last(body).delegation_id;
        const readCaller = () => rows(cases.split).find(r => r.id === delegation);
        await until(() => callerReading, 'child reading the CLI while the API is still untouched');
        body = yield call('edit_project_file', { resource_id: resource, relative_path: 'export_api.py', old_text: 'return explicit', new_text: 'return explicit.strip()', expected_revision: api.revision });
        assert.equal(last(body).ok, true, JSON.stringify(last(body)));
        assert.equal(readCaller().state, 'running');
        assert.ok(!text(body).includes('[worker-result:'));
        assert.equal(readCaller().worker.results.length, 0);
        mark('split-progress-without-result');
        apiChanged = true; mark('split-api-edited');
        await until(() => firstResult, 'first CLI evidence submitted and retried');
        assert.equal(readCaller().state, 'running');
        body = yield call('read_task_requirements');
        assert.equal(notice(body, delegation), firstResult);
        assert.equal(body.messages.filter(m => m.role === 'user' && m.content.startsWith('[worker-result:' + firstResult + ']')).length, 1);
        mark('split-first-notice');
        body = yield call('manage_plugin_worker', { delegation_id: delegation, action: 'result', result_id: firstResult });
        assert.equal(last(body).data.findings[0].source, 'export_cli.py 第 4 行');
        assert.ok(readCaller().worker.results[0].delivery.includedIn);
        assert.equal(readCaller().worker.results[0].disposition, null);
        assert.equal(readCaller().state, 'running');
        mark('split-first-body');
        body = yield call('run_project_check', {
          resource_id: resource, purpose: 'exploration', timeout: 30, yield_time_ms: 30000,
          script: 'from export_api import public_format\nassert public_format(" legacy ") == "legacy"\nprint("CLI_CONTRACT_PRESERVED")',
        });
        assert.equal(last(body).process?.exitCode, 0, JSON.stringify(last(body)));
        assert.equal(readCaller().state, 'running');
        mark('split-parent-verified-after-first-body');
        deciding = true;
        await until(() => laterResult && readCaller().state === 'completed', 'supplementary result finished');
        body = yield call('read_task_requirements');
        assert.ok(body.messages.some(m => m.role === 'user' && m.content.startsWith('[worker-result:' + laterResult + ']')));
        assert.equal(readCaller().worker.results[1].delivery.includedIn, undefined);
        return '接口接受带空白的格式输入。已核对旧 CLI 仍显式传入 legacy。';
      },
      scout: async function* (body) {
        assert.match(body.messages[0].content, /先提交.*证据/);
        assert.ok(text(body).includes(samples.scout.input.tried[0]));
        const [src] = facts(body).facts['host.workspace'].value;
        body = yield call('inspect_code', { resource_id: src.resourceId, relative_path: 'export_cli.py' });
        const caller = last(body);
        assert.match(caller.text, /public_format\("legacy"\)/);
        callerReading = true; mark('split-cli-read');
        await until(() => apiChanged, 'parent has made an independent edit');
        const data = structuredClone(samples.scout.output);
        data.findings[0].revision = caller.revision;
        const submit = call('submit_worker_result', { submission_key: 'cli-contract', data });
        body = yield submit;
        const saved = last(body);
        assert.equal(saved.saved, true);
        body = yield submit;
        assert.equal(last(body).duplicate, true);
        assert.equal(last(body).result_id, saved.result_id);
        firstResult = saved.result_id; mark('split-first-submitted');
        body = yield call('inspect_code', { resource_id: src.resourceId, relative_path: file });
        const config = last(body);
        await until(() => deciding, 'parent has verified the public contract using the first result');
        body = yield call('submit_worker_result', { submission_key: 'fallback-config', data: {
          findings: [{ observation: '省略格式时配置默认值为 modern。', source: file + ':1', revision: config.revision }],
          ruledOut: [], unknowns: [],
        } });
        laterResult = last(body).result_id; mark('split-second-submitted');
        return '已提交 CLI 和默认值的证据。';
      },
    }, project);
    const splitWire = wire.filter(w => w.case === 'split');
    assert.ok(!splitWire.some(w => w.role === 'parent' && w.body.messages.some(m => m.role === 'assistant'
      && m.tool_calls?.some(c => c.function.name === 'inspect_code' && c.function.arguments.includes('export_cli.py')))));
    passed.push('published scout fields show question/scope/known evidence; parent edits the API without rereading the delegated CLI');
    const splitRow = rows(cases.split)[0];
    assert.equal(splitRow.worker.results.length, 2);
    assert.ok(splitRow.worker.results[0].delivery.includedIn);
    assert.ok(splitRow.worker.results[1].delivery.notifiedIn);
    assert.equal(splitRow.worker.results[1].delivery.includedIn, undefined);
    passed.push('first evidence reached the parent before its actual verification while the child was still active; retry deduped and the later result could be skipped');

    await run('coupled', '07 · 同一接口的两个分支由主方核对', '核对显式格式与配置默认值的优先级，两项都在同一分支判断里。', {
      parent: async function* () {
        const body = yield call('inspect_code', { resource_id: resource, relative_path: 'export_api.py' });
        assert.match(last(body).text, /return explicit.strip\(\)/);
        assert.match(last(body).text, /export.cfg/);
        return '显式格式优先，省略时读取配置。';
      },
    }, project);
    assert.equal(rows(cases.coupled).length, 0);
    passed.push('coupled lookup completed through the existing L0 entry while the scout was enabled and never started');

    for (const arm of ['native', 'plugin']) {
      const native = arm === 'native';
      const stamp = joinTimings[arm] = {};
      const id = 'join-' + arm;
      const childRole = native ? 'native' : 'scout';
      await run(id, '08 · 同一调查延迟 · ' + arm, '单独核对 export.cfg 的默认值，主方继续核对任务要求。', {
        parent: async function* () {
          yield call('use_tool_group', { group: native ? 'subtasks' : 'plugins' });
          if (!native) yield call('inspect_worker_plugins');
          stamp.dispatch = Date.now();
          const input = { question: 'export.cfg 的默认值是什么？', scope: file, tried: [] };
          let body = yield (native
            ? call('create_subtask', { request_key: id, kind: 'report', resource_ids: [resource], goal: JSON.stringify(input) })
            : call('delegate_plugin_worker', { request_key: id, kind: 'report', resource_ids: [resource], worker: scout.id + '/investigate', input }));
          assert.equal(last(body).ok, true, JSON.stringify(last(body)));
          stamp.nextParentRequest = Date.now();
          assert.equal(stamp.releaseChild, undefined);
          assert.ok(!text(body).includes('已查到默认值 modern'));
          body = yield call('read_task_requirements');
          assert.ok(!text(body).includes('[worker-result:'));
          await until(() => rows(cases[id]).some(r => r.state === 'completed'), 'child completed after the shared observation delay');
          if (native) {
            body = yield call('inspect_code', { resource_id: resource, relative_path: 'export_api.py' });
            assert.equal(body.messages.filter(m=>typeof m.content==='string'&&m.content.startsWith('系统实际子任务汇总')&&m.content.includes('已查到默认值 modern')).length,1);
          }
          return '已完成对照。';
        },
        [childRole]: async function* (body) {
          const [src] = facts(body).facts['host.workspace'].value;
          body = yield call('inspect_code', { resource_id: src.resourceId, relative_path: file });
          const observed = last(body);
          assert.equal(observed.text, 'default=modern\n');
          stamp.observed = Date.now();
          await sleep(2000);
          stamp.releaseChild = Date.now();
          if (!native) yield call('submit_worker_result', { submission_key: id, data: {
            findings: [{ observation: '已查到默认值 modern', source: file + ':1', revision: observed.revision }],
            ruledOut: [], unknowns: [],
          } });
          return '已查到默认值 modern。';
        },
      }, project);
    }
    passed.push('equal two-second child delay: ordinary and plugin parents both continue their independent work before child completion');

    const checkDir = path.join(out, 'check-worker-fixture');
    fs.mkdirSync(checkDir);
    const checkManifest = {
      name: 'worker-check-fixture', version: '1',
      workers: [{ ...samples.scout.worker, name: 'check', method: checkWorkerMethod,
        requires: [...samples.scout.worker.requires, 'isolated-write', 'isolated-check'] }],
    };
    fs.writeFileSync(path.join(checkDir, 'plugin.json'), JSON.stringify(checkManifest));
    const checkPackage = await install({ dir: checkDir });
    await run('grantedCheck', '09 · 任务核心获得隔离检查授权', '在有授权的隔离副本上核对当前默认格式。', {
      parent: async function* () {
        let body = yield call('inspect_code', { resource_id: resource });
        const revision = last(body).projectRevision;
        yield call('use_tool_group', { group: 'plugins' });
        body = yield call('delegate_plugin_worker', { worker: checkPackage.id + '/check', request_key: 'isolated-check',
          input: configInquiry, resource_ids: [resource], kind: 'code', expected_revision: revision });
        assert.equal(last(body).ok, true, JSON.stringify(last(body)));
        const delegation = last(body).delegation_id;
        await until(() => rows(cases.grantedCheck).some(r => r.state === 'completed'), 'granted plugin check and child exit');
        body = yield call('read_task_requirements');
        body = yield call('manage_plugin_worker', { delegation_id: delegation, action: 'result', result_id: notice(body, delegation) });
        assert.equal(last(body).data.findings[0].observation, '隔离检查退出码为 0。');
        return '已收到隔离检查成果。';
      },
      checkWorker: async function* (body) {
        const [src] = facts(body).facts['host.workspace'].value;
        assert.equal(src.execute, true);
        assert.ok(names(body).includes('run_project_check'));
        body = yield call('run_project_check', { resource_id: src.resourceId, purpose: 'exploration', timeout: 30,
          yield_time_ms: 30000, script: 'from pathlib import Path\nassert Path("export.cfg").read_text().strip() == "default=modern"' });
        const checked = last(body);
        assert.equal(checked.process?.exitCode, 0, JSON.stringify(checked));
        body = yield call('submit_worker_result', { submission_key: 'checked', data: {
          findings: [{ observation: '隔离检查退出码为 0。', source: file, revision: checked.sourceRevision || '未知' }],
          ruledOut: [], unknowns: [],
        } });
        assert.equal(last(body).saved, true);
        return '已提交隔离检查结果。';
      },
    }, project);
    await post('/api/v2/plugins/' + checkPackage.id, { expectedRevision: checkPackage.revision, remove: true });
    assert.equal(fs.readFileSync(notes, 'utf8'), dirty);
    passed.push('authorized plugin code worker keeps the isolated check tool and returns exit 0; user notes are unchanged');

    await post('/api/v2/plugins/' + planner.id, { expectedRevision: planner.revision, remove: true });
    await run('scoutFailure', '04 · 规划者卸载，探路者上游失败，父任务仍可收尾', '调查一项资料。调查失败时如实报告，让主任务独立收尾。', {
      parent: async function* (body, threadId) {
        yield call('use_tool_group', { group: 'plugins' });
        body = yield call('inspect_worker_plugins');
        assert.deepEqual(last(body).workers.map(w => w.worker), [scout.id + '/investigate']);
        body = yield call('delegate_plugin_worker', { worker: last(body).workers[0].worker, request_key: 'separate', input: samples.scout.input, resource_ids: [], kind: 'report' });
        const delegation = last(body).delegation_id;
        await until(() => rows(threadId).some(r => ['blocked', 'failed'].includes(r.state)), 'actual assistant failure recorded');
        body = yield call('manage_plugin_worker', { delegation_id: delegation, action: 'query' });
        assert.ok(['blocked', 'failed'].includes(last(body).state)); assert.deepEqual(last(body).results, []);
        return '助手请求失败，没有可采用的调查成果。';
      },
      scout: async function* () { yield { upstreamFailure: 'P2_FIXED_PROVIDER_REJECTION' }; },
    });
    const failed = rows(cases.scoutFailure)[0];
    assert.equal(failed.state, 'failed');
    assert.equal(failed.worker.results.length, 0);
    const failedSession = read(path.join(runs, failed.childId, 'session.json'));
    assert.equal(failedSession.status, 'error'); assert.match(failedSession.why, /HTTP 400/);
    passed.push('uninstalling planner leaves scout callable; child failure preserved without synthesizing an accepted result or blocking parent final');

    await post('/api/v2/plugins/' + scout.id, { expectedRevision: scout.revision, workersEnabled: false });
    await run('disabled', '05 · 停用后的默认 L0', ordinaryPrompt, { parent: ordinary });
    const stream = id => wire.filter(w => w.case === id).map(w => w.body);
    const before = stream('baseline'), after = stream('disabled');
    assert.equal(before.length, after.length);
    assert.deepEqual(before.map(comparableRequest), after.map(comparableRequest));
    const groupHint = b => b.tools.find(t => t.function.name === 'use_tool_group').function.description;
    assert.ok(groupHint(stream('ordinaryEnabled')[0]).includes('可委派任务核心'));
    assert.ok(!groupHint(after[0]).includes('可委派任务核心'));
    assert.ok(!JSON.stringify(after).includes('inspect_worker_plugins'));
    assert.equal(rows(cases.disabled).length, 0); assert.equal(win.isVisible(), false);
    passed.push('disabled vs empty baseline: full provider bodies equal on every request after normalizing run/action/task identities and snapshot digest; workroom/memory groups, actual Skill load and normal final preserved');
    fs.writeFileSync(path.join(out, 'official-workers-cases.json'), JSON.stringify({ cases, project: project.id, root, received, oldRevision, newRevision, timeline, joinTimings, failedState: failed.state }, null, 2));
  } finally {
    setFixture(null);
    fs.writeFileSync(path.join(out, 'official-workers-wire.json'), JSON.stringify({ wire, timeline, faults, cases }, null, 2));
  }
};
