// ── 任务委托页（2026-10-29 文档《前端接什么》）─────────────────────────────
// 后端是 `job.py` 那一套：一次委托一个目录，人最后判过没过。
// 本页**只读 JSON**（/api/jobs…），不解析任何命令行输出。
//
// 一屏该显什么（文档第 7 节）：
//   档案 → 结局（人的裁定）→ 停止理由 → 提醒 → 轮次表（含方向）→ 人裁定按钮。
// 三条容易踩的规则照文档实现：
//   ① 契约有**三态**：没验 / 没过 / 全过（"没验"不许画成"没过"）；
//   ② benni_ran=false 是"测试者没跑"，不是"通过"；
//   ③ 没裁定时 verdict.json 不存在，显示「等人看」，不显示"通过"。

// 主要引用或加载入口包括 `desktop/renderer/index.html`。

let _jobOn = false;
let _jobList = [];
let _jobFilter = 'all';
let _jobCur = null;
let _jobRounds = [];
let _jobViewVersion = 0, _jobRoundVersion = 0, _jobListVersion = 0;
// L4 的正文流（2026-11-01）：和任务页分开。任务页回答"跑了几轮 / 过没过"，
// 这里回答"它这一轮具体做了什么" —— 用户明确要能读过程，不只读一张结论表。
let _jobLive = null;

function jobMode(on) {
  _jobViewVersion++; _jobRoundVersion++;
  if (on) jobLiveStop({ keepFeed: true });
  _jobOn = !!on;
  const p = $('jobpage');
  if (!p) return;
  p.hidden = !on;
  document.body.classList.toggle('jobmode', on);
  if (on) {
    const sp = $('skillpage'); if (sp) sp.hidden = true;
    _skillOn = false;
    document.body.classList.remove('skillmode');
    loadJobs();
  }
  syncFlowVisibility();
}

async function loadJobs() {
  const el = $('jlist');
  if (!el) return;
  const version = ++_jobListVersion;
  const r = await api('/jobs');
  if (version !== _jobListVersion || r.status !== 200) return;
  _jobList = (r.body.jobs || []);
  paintJobList();
}

function paintJobList() {
  const el = $('jlist');
  if (!el) return;
  const tabs = [['all', '全部'], ['pending', '等人看'], ['过', '过'], ['没过', '没过']];
  const rows = _jobList.filter(j =>
    _jobFilter === 'all' ? true
    : (_jobFilter === 'pending' ? !j.verdict : j.verdict === _jobFilter));
  el.innerHTML = '<div class="jtabs">'
    + tabs.map(t => '<button class="jtab' + (_jobFilter === t[0] ? ' on' : '')
      + '" data-jf="' + t[0] + '">' + t[1] + '</button>').join('') + '</div>'
    + (rows.length ? rows.map(j =>
        '<div class="jrow' + (j.id === _jobCur ? ' on' : '') + '" data-jid="' + esc(j.id) + '">'
        + '<div class="jtask">' + esc(j.task || j.id) + '</div>'
        + '<div class="jmeta"><span class="jcrew">' + esc(j.crew || '') + '</span>'
        + '<span>' + esc(JOB_PHASES[j.phase] || ((j.rounds || 0) + ' 轮')) + '</span>'
        + (j.passed != null ? '<span>契约 ' + j.passed + '/' + j.total + '</span>' : '')
        + '<span class="jvd ' + (j.verdict ? (j.verdict === '过' ? 'ok' : 'no') : 'wait') + '">'
        + (j.verdict || '等人看') + '</span></div></div>').join('')
      : '<div class="jempty">这一类还没有委托</div>');
  el.querySelectorAll('.jtab').forEach(b => {
    b.onclick = () => { _jobFilter = b.dataset.jf; paintJobList(); };
  });
  el.querySelectorAll('.jrow').forEach(b => {
    b.onclick = () => openJob(b.dataset.jid);
  });
}

// 契约的三态（文档第 4 节）：{} = 没验；ok:false = 没过；ok:true = 全过
function contractText(c) {
  if (!c || typeof c !== 'object' || !Object.keys(c).length) return ['没验', 'wait'];
  if (c.ok === true) return ['全过', 'ok'];
  if (c.ok === false) return ['没过', 'no'];
  return ['没验', 'wait'];
}

function contractDetail(c) {
  const inner = c && c.contract;
  if (!inner) return '';
  if (inner.items == null) return '（没拿到逐条结果）';
  const items = inner.items || [];
  const passed = items.filter(x => x.ok).length;
  return passed + '/' + (inner.total != null ? inner.total : items.length);
}

const JOB_PHASES = {
  recovering: '进程状态待核对', interrupted: '运行中断，需核对结果',
  new: '还没起草契约', drafting: '规划者起草契约中', draft_failed: '起草失败',
  awaiting_lock: '契约未锁定', ready: '契约已锁定',
  running: '自动多轮运行中', stopped: '已停下，等人看', judged: '人已裁定',
};

function jobPhaseHtml(phase, contract) {
  const goal = contract.done && (contract.done.intent || contract.done.goal || contract.done.task);
  return '<div class="jcard notice"><b>旧任务记录</b>' + esc(JOB_PHASES[phase] || phase || '阶段未知')
    + ' · 只读' + (goal ? '<div class="jhint">' + esc(goal) + '</div>' : '') + '</div>';
}

// ── 三条路线的可视化（2026-10-29 用户：「那三条 harness 路线有做可视化吗」）──
// 规划者 / 执行者 / 测试者原来只是散在轮次表和 memo 里的字段，
// 看不出"每条线各自干了什么、跑到哪、产出是什么"。
// 现在每条线一张卡，数据全部来自已有字段，不新造：
//   规划者 = memo.plan（含 by=planner/fallback，兜底要标出来）；
//   执行者 = summary.rounds[n]（回合/工具/耗时/token/真改了哪些）；
//   测试者 = benni-record.json（judgment / 新写 / 改了哪些 / needs_eye）。
function lane(kind, name, status, line1, line2) {
  return '<div class="lane ' + kind + '"><div class="lnhead"><b>' + name + '</b>'
    + '<span class="lnstatus">' + esc(status) + '</span></div>'
    + '<div class="lnline">' + esc(line1 || '') + '</div>'
    + (line2 ? '<div class="lnwhy">' + esc(line2) + '</div>' : '') + '</div>';
}

function lanesHtml(r0, memo, tester) {
  const plan = (memo && memo.plan) || null;
  const focus = (memo && memo.focus) || null;
  const planBy = plan ? plan.by : (focus ? focus.by : 'none');
  const planState = planBy === 'fallback' ? '程序兜底'
    : (plan && plan.plan ? '给了方向' : (focus && focus.ids ? '给了优先级' : '没派'));
  const planText = (plan && plan.plan)
    || (focus && focus.ids ? '先做：' + focus.ids.join('、') : '这一轮没派规划');
  const planWhy = (plan && plan.why) || (focus && focus.why) || '';
  const ran = r0.benni_ran !== false;
  // judgment 是**结构化对象**（view/summary/doubts/not_checked），不是字符串 ——
  // 直接 esc 会显示成 [object Object]（截图里就是这样）。
  const j = (tester && tester.judgment) || null;
  const judge = j ? (j.view || j.summary || '') : (r0.verdict || '');
  const doubts = j && (j.doubts || []).length ? '疑点：' + j.doubts.join('；')
    : (j && j.not_checked ? '没查：' + j.not_checked : '');
  const newF = (tester && tester.new_files) || [];
  const modF = (tester && tester.modified_files) || [];
  const changed = _jobPaths(r0.changed || []);
  const tk = r0.tokens && r0.tokens.prompt
    ? Math.round((r0.tokens.prompt + (r0.tokens.completion || 0)) / 1000) + 'k' : '—';
  return '<div class="lanes">'
    + lane('planner', '规划者', planState, planText, planWhy)
    + lane('executor', '执行者', r0.status || '',
        (r0.turns || 0) + ' 回合 · ' + (r0.actions || 0) + ' 次工具 · '
        + (r0.elapsed_s || 0) + 's · ' + tk,
        changed.length ? '真变了：' + changed.slice(0, 4).join('、') : '没有文件改动')
    + lane('tester', '测试者', ran ? (judge || '没给判断') : '没跑',
        ran ? ('新写 ' + newF.length + ' 个 · 改 ' + modF.length + ' 个'
          + ((tester && tester.needs_eye) ? ' · 要人看' : ''))
          : '（这档不派测试，或没有轨迹）',
        ran ? doubts : '')
    + '</div>';
}

async function openJob(id) {
  const version = ++_jobViewVersion;
  _jobRoundVersion++;
  _jobCur = id;
  paintJobList();
  const el = $('jmain');
  if (!el) return;
  el.innerHTML = '<div class="jempty">读这一屏…</div>';
  const [r, cr] = await Promise.all([
    api('/jobs/' + encodeURIComponent(id)),
    api('/jobs/' + encodeURIComponent(id) + '/contract'),
  ]);
  if (version !== _jobViewVersion || _jobCur !== id) return;
  if (r.status !== 200 || cr.status !== 200) {
    el.textContent = r.body.error || cr.body.error || '任务读取失败，请重试'; return;
  }
  const d = r.body || {};
  const contract = cr.body || {};
  const s = d.summary || {};
  const vd = d.verdict;
  const rounds = s.rounds || [];
  _jobRounds = rounds;
  const last = rounds[rounds.length - 1] || {};

  // ── 结局：人的裁定在最上面（文档：没裁定就显「等人看」，别显"通过"）──
  const verdictHtml = vd
    ? '<div class="jcard verdict ' + (vd.verdict === '过' ? 'ok' : 'no') + '">'
      + '<div class="jvh">' + (vd.verdict === '过' ? '✓ 过' : '✗ 没过')
      + '<span class="jvtime">' + esc(vd.at || '') + '</span></div>'
      + '<div class="jvreason">' + esc(vd.reason || '') + '</div>'
      + (vd.stop ? '<div class="jvstop">机器当时说：' + esc(vd.stop) + '</div>' : '')
      + '</div>'
    : '<div class="jcard verdict wait"><div class="jvh">' + esc(JOB_PHASES[d.phase] || '等人看') + '</div>'
      + '<div class="jvreason">未记录人工裁定。</div></div>';

  // ── 档案行 ──
  const scope = (s.scope || []).join('、') || '不限';
  const cinfo = last.contract ? contractDetail(last.contract) : '';
  // ★ 争用提示（2026-11-01）：只显示"最近一次"，且标题里放具体路径。
  //   不放条数 —— 条数多了会变成噪音，人只想知道"现在和谁撞了"。
  const wt = (d.watch || [])[0];
  // ★ 只显示**对方**：把当前 job 也列出来的话，读起来像"跟自己撞"。
  const wtNames = wt
    ? ((wt.others && wt.others.length) ? wt.others : (wt.jobs || []).filter(x => x !== id))
    : [];
  const wtHtml = wtNames.length
    ? '<span class="jpill jwarn" title="' + esc(wt.path || '') + '">与 '
      + esc(wtNames.join(' / ')) + ' 争用</span>'
    : '';
  const head = '<div class="jhead"><b>' + esc(id) + '</b>'
    + '<span class="jpill">' + esc(s.crew || '') + '</span>'
    + '<span class="jpill">' + (s.n_rounds || 0) + '/' + (s.max_rounds || 0) + ' 轮</span>'
    + '<span class="jpill">范围 ' + esc(scope) + '</span>'
    + '<span class="jpill">' + esc(JOB_PHASES[d.phase] || d.phase || '') + '</span>'
    + (cinfo ? '<span class="jpill">契约 ' + esc(cinfo) + '</span>' : '')
    + wtHtml
    + '<span class="jspacer"></span>'
    + '<button class="pill" data-jlive="1" title="把每一轮的规划、正文、测试和工具明细放进主内容区">在正文里查看</button>'
    + '</div>';

  const stopHtml = s.stop
    ? '<div class="jcard stop"><b>停止</b>' + esc(s.stop) + '</div>' : '';
  const noticeHtml = s.notice
    ? '<div class="jcard notice"><b>提醒</b>' + esc(s.notice) + '</div>' : '';

  // ── 轮次表：每轮一行 + 一行方向（plan_hint）──
  const rowsHtml = rounds.map(r0 => {
    const cs = contractText(r0.contract);
    const tester = r0.benni_ran === false ? '没跑' : (r0.verdict || '没给');
    const tk = r0.tokens && r0.tokens.prompt
      ? Math.round((r0.tokens.prompt + (r0.tokens.completion || 0)) / 1000) + 'k' : '—';
    const viol = (r0.scope_violation || []).length;
    return '<tr data-rn="' + r0.n + '"><td>r' + r0.n + '</td>'
      + '<td>' + esc(r0.status || '') + (viol ? ' <span class="jbad">越界</span>' : '') + '</td>'
      + '<td>' + (r0.turns || 0) + '</td>'
      + '<td><span class="jvd ' + cs[1] + '">' + cs[0] + '</span></td>'
      + '<td>' + esc(tester) + '</td>'
      + '<td>' + (r0.elapsed_s || 0) + 's</td><td>' + tk + '</td></tr>'
      + (r0.plan_hint ? '<tr class="jplan"><td></td><td colspan="6">'
        + esc(r0.plan_hint) + '</td></tr>' : '');
  }).join('');

  el.innerHTML = head + verdictHtml + jobPhaseHtml(d.phase, contract) + stopHtml + noticeHtml
    + '<div id="jlanes"></div>'
    + '<table class="jtable"><thead><tr><th>轮</th><th>状态</th><th>回合</th>'
    + '<th>契约</th><th>测试者</th><th>耗时</th><th>token</th></tr></thead>'
    + '<tbody>' + rowsHtml + '</tbody></table>'
    + '<div class="jhint">点某一行看这一轮为什么这么干（memo）和测试者看到什么。</div>'
    + '<div id="jround"></div>';

  const jl = el.querySelector('[data-jlive]');
  if (jl) jl.onclick = () => jobLiveOpen(id);
  el.querySelectorAll('tr[data-rn]').forEach(tr => {
    tr.onclick = () => openRound(id, Number(tr.dataset.rn));
  });
  // 默认展开**最后一轮**：三条路线和"为什么这么干"一屏就该看见，不用先点一下
  // ★ 已停止/已裁定时，memo 是那一轮**开工前**写的，里面的「下一步该谁」已经不是现状。
  //   不标出来，人会把「该执行者」读成"它还要接着干"。
  if (rounds.length) openRound(id, last.n || rounds.length,
    ['stopped', 'judged'].includes(d.phase));
}

async function openRound(id, n, frozen = false) {
  const version = ++_jobRoundVersion, jobVersion = _jobViewVersion;
  const el = $('jround');
  if (!el) return;
  const r = await api('/jobs/' + encodeURIComponent(id) + '/round/' + n);
  if (version !== _jobRoundVersion || jobVersion !== _jobViewVersion) return;
  if (r.status !== 200) { el.textContent = r.body.error || '轮次读取失败，请重试'; return; }
  const d = r.body || {};
  const r0 = _jobRounds.find(x => x.n === n) || {};
  const lanes = $('jlanes');
  if (lanes) lanes.innerHTML = lanesHtml(r0, d.memo, d.tester);
  const memo = d.memo || {};
  const parts = [];
  if (memo.next_why && !frozen) {
    parts.push('<b>下一步该谁：</b>' + esc(memo.next_role || '') + ' —— ' + esc(memo.next_why));
  } else if (memo.next_why && frozen) {
    parts.push('<span class="scdim">这一轮开工时写的是「' + esc(memo.next_role || '')
      + '」；任务已经停下，这条不再执行。</span>');
  }
  if (memo.plan && memo.plan.plan) {
    const by = memo.plan.by || '';
    parts.push('<b>这一轮干什么：</b>' + esc(memo.plan.plan)
      + (by === 'fallback' ? ' <span class="jbad">（程序兜底给的，不是规划者想的）</span>' : ''));
  }
  if (memo.focus && memo.focus.ids) {
    parts.push('<b>先做哪几条：</b>' + memo.focus.ids.join('、')
      + (memo.focus.by === 'fallback' ? ' <span class="jbad">（程序兜底）</span>' : ''));
  }
  // ★ "这一轮明确不做"（2026-11-01）：它和"还没过的判据"是两回事 ——
  //   没过的只是还没轮到，declined 是规划者判断这一轮不该碰。
  const dec = (memo.focus && memo.focus.declined) || (memo.plan && memo.plan.declined) || [];
  if (dec.length) {
    parts.push('<b>这一轮不做：</b>' + dec.slice(0, 6).map(x =>
      esc(x.what || '') + (x.why ? '（' + esc(x.why) + '）' : '')).join('；'));
  }
  const def = (memo.focus && memo.focus.deferred) || [];
  if (def.length) {
    parts.push('<b>留到下一轮：</b>第 ' + def.join('、') + ' 条（每轮派发有上限）');
  }
  const ch = d.changes || {};
  const changed = _jobPaths(ch.changed || ch.added || ch.files || []);
  if (changed.length) parts.push('<b>真变了：</b>' + changed.slice(0, 8).join('、'));
  if (memo.before && (memo.before.open_claims || []).length) {
    parts.push('<b>还没过的判据：</b>' + memo.before.open_claims.join('、'));
  }
  el.innerHTML = '<div class="jcard">'
    + (parts.length ? parts.map(p => '<div class="jline">' + p + '</div>').join('')
      : '这一轮没有更多说明') + '</div>';
}

// ── L4 正文流 ───────────────────────────────────────────────────────
// 为什么另做一页而不是把任务页的表格拉长：这两件事回答的问题不同。
//   任务页 = 档案：跑了几轮、契约几条、最后由谁裁定；
//   正文流 = 过程：这一轮规划了什么、它写了什么、调了哪些工具、测试者怎么反驳。
// ★ 正文流的每一轮**先落结论，再按需展开工具流水**：自动全量铺开 1200 条事件，
//   人只会看到一屏日志，反而读不出这一轮做成了什么。
function _jobPaths(v) {
  return (Array.isArray(v) ? v : []).map(x => {
    if (typeof x === 'string') return x;
    return String((x && (x.path || x.file || x.name)) || '');
  }).filter(Boolean);
}

function _jobRatio(r) {
  const c = (r && r.contract) || {};
  const inner = c.contract || {};
  const items = inner.items;
  if (!Array.isArray(items)) return '';
  const n = items.filter(x => x && x.ok).length;
  return n + '/' + (inner.total == null ? items.length : inner.total);
}

function jobLiveStop() {
  if (_jobLive && _jobLive.timer) clearInterval(_jobLive.timer);
  _jobLive = null;
  document.body.classList.remove('joblive');
}

function jobLiveOpen(jid) {
  jobLiveStop();
  if (!jid) return;
  if (typeof jobMode === 'function') jobMode(false);
  _jobCur = jid;
  _jobLive = { id: jid, rendered: new Set(), active: 0, after: 0,
               timer: 0, busy: false, done: false, activeEl: null };
  document.body.classList.add('joblive');
  // 正文流是独立展示：不属于某个普通会话，退出时必须把它清掉。
  cur = null;
  _saveClock();
  clearFeed();
  $('thinking').hidden = true;
  $('streaming').hidden = true;
  $('doing').hidden = true;
  const f = $('feed');
  f.insertAdjacentHTML('beforeend',
    '<section class="jlive" id="jlive">'
    + '<div class="jlivehead">'
    + '<div class="jlhmain"><span class="jltag">L4</span>'
    + '<b class="jlid" id="jlid">' + esc(jid) + '</b>'
    + '<span class="jlp" id="jlphase">读取中</span>'
    + '<span class="jlm" id="jlmeta"></span></div>'
    + '<button class="pill" id="jlview" title="契约、轮次表、人工裁定在这里">任务详情</button>'
    + '</div>'
    + '<div class="jlnote" id="jlnote"></div>'
    + '<div class="jlrounds" id="jlrounds"></div>'
    + '<div class="jltail" id="jltail"></div>'
    + '</section>');
  $('jlview').onclick = () => { jobLiveStop(); jobMode(true); openJob(jid); };
  // 进来先贴底：正在跑时人要看的是最新一轮；回看历史也从最后一轮读起。
  // 往上翻立即脱离吸附（base.js 的 _stickBottom 规则），不会被新内容拽回去。
  scrollFeedToBottom();
  stateNote('L4 正文已接入主内容区', 3500);
  jobLiveTick();
  _jobLive.timer = setInterval(jobLiveTick, 1500);
}

function _jlSetNote(html) {
  const el = $('jlnote');
  if (!el) return;
  if (el.dataset.html === html) return;
  el.dataset.html = html;
  el.innerHTML = html;
  const go = el.querySelector('[data-jltask]');
  if (go) go.onclick = () => {
    const jid = _jobLive && _jobLive.id;
    jobLiveStop(); jobMode(true); if (jid) openJob(jid);
  };
}

async function jobLiveTick() {
  if (!_jobLive || _jobLive.busy) return;
  if (!document.body.classList.contains('joblive')) return;
  const L = _jobLive;
  L.busy = true;
  try {
    const r = await api('/jobs/' + encodeURIComponent(L.id));
    if (_jobLive !== L || r.status !== 200) return;
    const d = r.body || {};
    const s = d.summary || {};
    const job = d.job || {};
    const phase = String(d.phase || '');
    const rounds = s.rounds || [];
    const last = rounds[rounds.length - 1] || {};
    const ratio = _jobRatio(last);
    const max = s.max_rounds || job.max_rounds || 0;
    $('jlphase').textContent = JOB_PHASES[phase] || phase;
    $('jlmeta').textContent = (s.n_rounds || 0) + '/' + (max || '?') + ' 轮'
      + (ratio ? ' · 契约 ' + ratio : '');

    if (phase === 'recovering' || phase === 'interrupted') {
      _jlSetNote('运行结果待核对。请检查各轮日志和实际产出；系统不会自动重跑。');
    } else if (phase === 'drafting' || phase === 'new') {
      _jlSetNote('规划者正在起草契约。<b>起草完成后需要你先审阅、锁定，才会开始多轮。</b>');
    } else if (phase === 'awaiting_lock') {
      _jlSetNote('契约已起草，<b>等你审阅并锁定</b>。'
        + '<button class="pill" data-jltask="1">去审阅契约</button>');
    } else if (phase === 'ready') {
      _jlSetNote('契约已锁定，尚未开始多轮。'
        + '<button class="pill" data-jltask="1">查看旧任务</button>');
    } else if (phase === 'running') {
      const act = Number(d.active_round || 0);
      _jlSetNote('自动多轮运行中' + (act ? ' · 第 ' + act + ' 轮' : '')
        + ' · 每轮结束后正文会追加到这里，当前轮可展开看工具动作。');
    } else {
      _jlSetNote('');
    }

    // 已完成的轮次按顺序追加。后端每次最多返回一个轮目录的正文，不开 N 个并发，
    // 免得刚跑完时十几个请求同时抢 SQLite / 文件句柄。
    for (const row of rounds) {
      if (!L.rendered.has(Number(row.n))) await _jobLiveAddRound(L, row);
    }

    if (phase === 'running') {
      const n = Number(d.active_round || 0);
      if (n && !L.rendered.has(n)) {
        _jobLiveEnsureActive(L, n);
        await _jobLivePullActive(L, n);
      }
    }

    const finished = (phase === 'stopped' || phase === 'judged');
    if (finished && rounds.every(row => L.rendered.has(Number(row.n)))) {
      L.done = true;
      clearInterval(L.timer);
      L.timer = 0;
      _jobLiveTail(d);
    }
    // 所有轮次和结尾提示都插完之后再贴底一次。否则"最后一张卡片已经出现、
    // 但结尾那句停在视口外"，人以为还没结束。
    if (_stickBottom && $('feed')) $('feed').scrollTop = $('feed').scrollHeight;
  } catch (e) {
    _jlSetNote('正文读取失败：' + esc(e && e.message ? e.message : e));
  } finally {
    L.busy = false;
  }
}

function _jobLiveEnsureActive(L, n) {
  if (L.active === n || !$('jlrounds')) return;
  const el = document.createElement('section');
  el.className = 'jlround jlactive';
  el.dataset.round = String(n);
  el.innerHTML = '<div class="jlrh"><span class="jln">第 ' + n + ' 轮</span>'
    + '<span class="jlv">执行中…</span><span class="jlstate"></span></div>'
    + '<div class="jlwork" data-livework="1"></div>';
  $('jlrounds').appendChild(el);
  L.active = n;
  L.activeEl = el;
  L.after = 0;
  L.liveState = { lastSay: null, think: new Map() };
}

async function _jobLivePullActive(L, n) {
  const res = await api('/jobs/' + encodeURIComponent(L.id)
    + '/round/' + n + '/events?after=' + (L.after || 0));
  if (_jobLive !== L || res.status !== 200) return;
  const d = res.body || {};
  const work = L.activeEl && L.activeEl.querySelector('[data-livework]');
  if (!work) return;
  const follow = _stickBottom;
  if (Array.isArray(d.events) && d.events.length) {
    _jobEventLog(d.events, work, { live: true, state: L.liveState });
  }
  L.after = Number(d.after || L.after || 0);
  const st = L.activeEl.querySelector('.jlstate');
  if (st) st.textContent = (d.turns || 0) + ' 回合' + (d.status ? ' · ' + d.status : '');
  if (follow) $('feed').scrollTop = $('feed').scrollHeight;
}

async function _jobLiveAddRound(L, row) {
  const n = Number(row.n);
  if (!n || L.rendered.has(n)) return;
  const res = await api('/jobs/' + encodeURIComponent(L.id) + '/round/' + n);
  if (_jobLive !== L) return;
  if (res.status !== 200 || !res.body || typeof res.body !== 'object') {
    _jlSetNote('第 ' + n + ' 轮读取失败，稍后自动重试：' + esc(res.body?.error || ('HTTP ' + res.status)));
    return;
  }
  const d = res.body;
  const memo = d.memo || {};
  const focus = memo.focus || memo.plan || {};
  const plan = focus.ids && focus.ids.length
    ? ('先做第 ' + focus.ids.join('、') + ' 条' + (focus.by === 'fallback' ? '（程序兜底）' : ''))
    : (focus.plan || '这一轮没有规划者说明');
  const planWhy = focus.why || '';
  const ratio = _jobRatio(row);
  const files = _jobPaths((d.changes && (d.changes.changed || d.changes.added)) || row.changed || []);
  const out = String(d.out || '');
  const tm = String(d.tester_md || '');
  const el = document.createElement('section');
  el.className = 'jlround';
  el.dataset.round = String(n);
  el.innerHTML = '<div class="jlrh"><span class="jln">第 ' + n + ' 轮</span>'
    + '<span class="jlv">' + esc(row.verdict || (row.benni_ran === false ? '测试者没跑' : '测试者没给判断')) + '</span>'
    + '<span class="jlc">' + (ratio ? '契约 ' + esc(ratio) : '契约没验') + '</span>'
    + '<span class="jlst">' + esc(row.status || '') + ' · '
    + (row.turns || 0) + ' 回合 · ' + (row.actions || 0) + ' 次工具</span></div>'
    + '<div class="jlplan"><b>规划者</b> ' + esc(plan)
    + (planWhy ? '<div class="jlwhy">' + esc(planWhy) + '</div>' : '') + '</div>'
    + (out ? '<div class="md jlout">' + mdToHtml(out) + '</div>'
           : '<div class="jlwhy">这一轮没有产出正文。</div>')
    + (files.length ? '<div class="jlfiles"><b>真正变了</b> '
        + esc(files.slice(0, 8).join('、')) + (files.length > 8 ? ' 等 ' + files.length + ' 个' : '')
        + '</div>' : '<div class="jlfiles">这一轮没有文件改动</div>')
    + (tm ? '<details class="jlsec"><summary>测试者全文</summary>'
        + '<div class="md jltester">' + mdToHtml(tm) + '</div></details>' : '')
    + '<details class="jlsec jlworkfold"><summary>执行明细 · '
        + (row.turns || 0) + ' 回合 / ' + (row.actions || 0) + ' 次工具</summary>'
        + '<div class="jlwork">点开时读取…</div></details>';
  const host = $('jlrounds');
  if (!host) return;
  // 可能已经开了下一轮，而这一轮的「执行中」卡还挂着 —— 按轮号删，
  // 不能只看 L.activeEl（它已经被下一轮覆盖）。否则同一轮会出现两份。
  const old = host.querySelector('.jlround[data-round="' + n + '"]');
  if (old) old.remove();
  if (L.active === n) { L.active = 0; L.activeEl = null; }
  host.appendChild(el);
  L.rendered.add(n);
  el.querySelectorAll('details.jlsec').forEach(det => {
    det.addEventListener('toggle', async () => {
      if (!det.open) return;
      const box = det.querySelector('.jlwork') || det.querySelector('.jltester');
      if (!box || det.dataset.loaded === '1') return;
      det.dataset.loaded = '1';
      if (!det.classList.contains('jlworkfold')) return;
      box.textContent = '正在读取这一轮的执行流水…';
      const ev = await api('/jobs/' + encodeURIComponent(L.id) + '/round/' + n + '/events?after=0');
      if (_jobLive !== L || ev.status !== 200) {
        delete det.dataset.loaded;
        box.textContent = '读取失败：' + ((ev.body && ev.body.error) || ('HTTP ' + ev.status));
        return;
      }
      box.innerHTML = '';
      _jobEventLog((ev.body && ev.body.events) || [], box, { live: false });
    });
  });
  if (_stickBottom) $('feed').scrollTop = $('feed').scrollHeight;
}

function _jobEventLog(events, host, opt) {
  opt = opt || {};
  const live = !!opt.live;
  const st = opt.state || (opt.state = { lastSay: null, think: new Map() });
  let shown = 0;
  for (const e of events || []) {
    if (!e || !e.act) continue;
    const act = String(e.act);
    if (act === 'agent_thought_chunk' || act === 'think') {
      const key = String(e.turn || '') + '|' + String(e.role || 'executor');
      let box = st.think.get(key);
      if (!box) {
        const det = document.createElement('details');
        det.className = 'jlthink';
        det.innerHTML = '<summary>思考 · 第 ' + esc(String(e.turn || '?')) + ' 回合</summary>'
          + '<div class="jlthinkbody"></div>';
        host.appendChild(det);
        box = det.querySelector('.jlthinkbody');
        st.think.set(key, box);
      }
      box.textContent += String(e.text || '');
      st.lastSay = null;
      continue;
    }
    if (act === 'agent_message_chunk' || act === 'delta') {
      if (!live) continue;             // 历史轮次用定稿正文；chunk 只服务当前轮
      const role = String(e.role || 'executor');
      const txt = String(e.text || '');
      if (!txt) continue;
      if (st.lastSay && st.lastSay.role === role) {
        st.lastSay.el.textContent += txt;
      } else {
        const div = document.createElement('div');
        div.className = 'jlmsg';
        div.innerHTML = '<span class="jlwho">' + esc(ROLE_CN[role] || role) + '</span>'
          + '<div class="jlmtext"></div>';
        div.querySelector('.jlmtext').textContent = txt;
        host.appendChild(div);
        st.lastSay = { role, el: div.querySelector('.jlmtext') };
      }
      continue;
    }
    if (act === 'tool') {
      const line = document.createElement('div');
      const ok = e.ok !== false;
      line.className = 'jltool' + (ok ? '' : ' bad');
      line.innerHTML = '<span class="jltk">' + (ok ? '✓' : '✕') + '</span>'
        + '<span class="jltn">' + esc(e.tool || '工具') + '</span>'
        + '<span class="jltd">' + esc(e.summary || e.line || '') + '</span>';
      host.appendChild(line);
      st.lastSay = null;
      continue;
    }
    if (act === 'say' && !live) {
      if (!e.text) continue;
      const div = document.createElement('div');
      div.className = 'md jlsay';
      div.innerHTML = mdToHtml(String(e.text));
      host.appendChild(div);
      continue;
    }
    if (act === 'verify') {
      const line = document.createElement('div');
      line.className = 'jlstatus ' + (e.ok ? 'ok' : 'bad');
      line.textContent = e.ok ? '契约检查通过' : ('契约检查未过：' + String(e.reason || '').slice(0, 200));
      host.appendChild(line);
      continue;
    }
    if (act === 'changes') {
      const fs = _jobPaths((e.changed || []).concat(e.added || []));
      if (fs.length) {
        const line = document.createElement('div');
        line.className = 'jlstatus';
        line.textContent = '这一轮真正变了 ' + fs.length + ' 个文件：' + fs.slice(0, 6).join('、');
        host.appendChild(line);
      }
      continue;
    }
    if (act === 'end') {
      const line = document.createElement('div');
      line.className = 'jlstatus';
      line.textContent = '本轮结束 · ' + String(e.status || '?')
        + (e.turns ? ' · ' + e.turns + ' 个模型回合' : '');
      host.appendChild(line);
      continue;
    }
    if (act === 'benni' && !e.running) {
      const line = document.createElement('div');
      line.className = 'jlstatus';
      line.textContent = '测试者完成复核';
      host.appendChild(line);
      continue;
    }
    shown += 1;
  }
  if (!host.children.length && !shown) {
    const p = document.createElement('div');
    p.className = 'jlwhy';
    p.textContent = '还没有可显示的执行事件。';
    host.appendChild(p);
  }
  if (host.querySelectorAll('.jltool').length > 600) {
    const all = host.querySelectorAll('.jltool');
    for (let i = 0; i < all.length - 600; i++) all[i].remove();
  }
}

function _jobLiveTail(d) {
  const box = $('jltail');
  if (!box) return;
  const s = d.summary || {};
  const v = d.verdict || {};
  const stop = s.stop || '';
  const body = v.verdict
    ? '<b>' + esc(v.verdict === '过' ? '人已裁定：过' : '人已裁定：没过') + '</b> '
      + esc(v.reason || '')
    : '<b>已停止，等你裁定</b> ' + esc(stop || '没有自动停止理由');
  box.innerHTML = '<div class="jlend">' + body
    + '<button class="pill" data-jltask="1">去任务页看证据</button></div>';
  const go = box.querySelector('[data-jltask]');
  const jid = _jobLive && _jobLive.id;
  if (go) go.onclick = () => { jobLiveStop(); jobMode(true); if (jid) openJob(jid); };

}

// 起草/多轮期间自动刷新；待人锁、裁定时不反复重建页面，避免打断人的操作。
// ★ 不再每 2 秒重建整个 openJob：那会把正在读的正文和滚动位置都冲掉。
//   只有"轮数或阶段真的变了"才重绘任务页；正文流自己按轮追加。
let _jobSig = '';
setInterval(() => {
  if (!_jobOn) return;
  loadJobs();
  const curJob = _jobList.find(j => j.id === _jobCur);
  if (!curJob) return;
  const sig = curJob.id + '|' + curJob.phase + '|' + (curJob.rounds || 0);
  if (sig !== _jobSig && (curJob.phase === 'drafting' || curJob.phase === 'running'
      || curJob.phase === 'stopped' || curJob.phase === 'awaiting_lock'
      || curJob.phase === 'ready' || curJob.phase === 'judged')) {
    _jobSig = sig;
    openJob(_jobCur);
  }
}, 2000);

// 顶栏「任务」按钮（JS 建，不动 HTML 结构）
(function mountJobs() {
  const bar = document.querySelector('.bar');
  if (!bar) return;
  const existing = $('openjobs');
  if (existing) return;
  const btn = document.createElement('button');
  btn.id = 'openjobs';
  btn.className = 'pill';
  btn.textContent = '任务';
  btn.title = '委托：跑了几轮、契约过没过、最后由人裁定';
  const anchor = $('openstate');
  if (anchor) bar.insertBefore(btn, anchor); else bar.appendChild(btn);
  btn.onclick = () => jobMode(!_jobOn);

  // ★ Git 模板（2026-11-03）：CODEOWNERS / PR 模板要**看得见、改得了**，
  //   不是写死在代码里的字符串。放在独立 dialog 里，避免被任务列表的定时刷新冲掉。
  const gf = document.createElement('button');
  gf.className = 'pill';
  gf.textContent = 'Git 模板';
  gf.title = 'CODEOWNERS / PR 模板：可视化、可手动修改（保存在 harness/gitflow.json）';
  if (anchor) bar.insertBefore(gf, anchor); else bar.appendChild(gf);
  gf.onclick = async () => {
    let dlg = $('gfdlg');
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.id = 'gfdlg';
      dlg.innerHTML = '<h3>Git 模板（保存在 harness/gitflow.json）</h3>'
        + '<div class="gfrow"><label>CODEOWNERS</label><textarea id="gfco" rows="7"></textarea></div>'
        + '<div class="gfrow"><label>PR 模板</label><textarea id="gfpr" rows="9"></textarea></div>'
        + '<div class="gfrow"><button class="pill" id="gfsave">保存</button>'
        + '<button class="pill" id="gfclose">关闭</button><span id="gfnote"></span></div>';
      document.body.appendChild(dlg);
      $('gfclose').onclick = () => dlg.close();
      $('gfsave').onclick = async () => {
        const r = await api('/gflow/config', {
          method: 'POST',
          body: {codeowners: $('gfco').value, pr_template: $('gfpr').value},
        });
        const b = r.body || {};
        $('gfnote').textContent = b.note || b.error || '';
      };
    }
    const r = await api('/gflow/config');
    const b = r.body || {};
    $('gfco').value = b.codeowners || '';
    $('gfpr').value = b.pr_template || '';
    if (dlg) dlg.showModal();
  };
})();

