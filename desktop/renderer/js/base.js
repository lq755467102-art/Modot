
// 旧会话页面的共用接口、状态与 DOM 操作，由按顺序加载的渲染脚本共享。
// 主要引用或加载入口包括 `desktop/renderer/index.html`。

const servicePort = Number(new URLSearchParams(location.search).get('port') || window.__PORT || 8788);
if (!Number.isInteger(servicePort) || servicePort < 1 || servicePort > 65535) throw new Error('Invalid service port');
const API = 'http://127.0.0.1:' + servicePort + '/api';
// ★ 接口的钥匙（2026-10-26）：外壳在开窗口时把它放进 query（`?token=...`），
//   这里读出来，每个请求都带上。**没有它一律 401** —— 那个 401 不是 bug，
//   是"你打开的这页不是从应用里来的"（比如直接用浏览器打开 file://）。
//   ★ 不走 localStorage / 不写回磁盘：钥匙已经在文件里了，页面只当一次性的携带者。
const TOKEN = new URLSearchParams(location.search).get('token') || '';
// ★ 读完就把钥匙从地址栏抹掉（2026-10-26）：留着的话，它会被截图、
//   被任何一次 `location.href` 的打印带出去。页面已经拿到手了，不需要再挂在 URL 上。
//   （启动日志里那一条 CSP 警告仍会带一次 —— 那发生在抹掉之前，是 query 方案的已知代价。）
if (TOKEN) {
  try {
    const u = new URL(location.href);
    u.searchParams.delete('token');
    history.replaceState(null, '', u.pathname + (u.search ? u.search : '')
                        + (u.hash || ''));
  } catch (e) { /* 抹不掉也不影响用 */ }
}
const $ = id => document.getElementById(id);

// 事件绑定助手。**必须在最先加载的文件里**（2026-10-27 拆文件踩到）：
//   它原来和启动代码一起放在最后那个文件，而前面文件的顶层就在 `on('mself', ...)`，
//   函数声明提升只在同一个脚本内有效 —— 跨文件就是 ReferenceError。
//   凡是"顶层就要用"的助手，都得住在这第一个文件里。
function on(id, ev, fn) {
  const el = $(id);
  if (!el) { console.error('绑定失败，页面上没有 #' + id); return; }
  el[ev] = fn;
}

let cur = null;        // 当前会话 id

// ★ 计时**按会话记**（2026-10-28 用户：「本我自我的计时器在切换会话之后就重新计时了」）。
//   原来 _selfSince/_benniSince 是单例：切走时清零、切回时从 0 重算，
//   于是"它到底跑了多久"每次切会话都从头数 —— 看着像它刚开工。
//   现在每个会话各存一份：切走前存、切回来后恢复。
const _clocks = new Map();
function _saveClock() {
  if (cur == null) return;
  _clocks.set(cur, { self: _selfSince, benni: _benniSince,
                     selfMs: _lastSelfMs, benniMs: _lastBenniMs });
}
function _restoreClock() {
  const c = _clocks.get(cur);
  _selfSince = c ? c.self : 0;
  _benniSince = c ? c.benni : 0;
  _lastSelfMs = c ? c.selfMs : 0;
  _lastBenniMs = c ? c.benniMs : 0;
}
// 当前项目 = 新会话在哪个文件夹里干活。空字符串表示用服务端默认（仓库根）。
// ★ 这就是"项目"的全部含义，没有第二种定义 —— 一个文件夹就是一个项目。
let curProj = localStorage.getItem('harness.proj') || '';
let seen = 0;          // 已渲染到第几条 seq
let lastLog = '';       // 上一次取到的 run.log（整段替换，不追加）
let running = false;
let pollBusy = false; // 上一次事件轮询还没回来时，不再叠加下一次

// Persist only opaque request ids and payload hashes; never persist credentials/body.
const _receiptKey = 'harness.receipts.' + servicePort;
let _receipts = {}, _receiptStorageBroken=false;
try {
  const saved = JSON.parse(localStorage.getItem(_receiptKey) || '{}');
  if (!saved || typeof saved !== 'object' || Array.isArray(saved) || Object.values(saved).some(r=>!r || typeof r.id!=='string' || !/^[A-Za-z0-9_-]{8,128}$/.test(r.id))) throw Error('invalid receipt storage');
  _receipts=saved;
} catch (_) { _receiptStorageBroken=true; }
function saveReceipts() { if(_receiptStorageBroken)return false; try { localStorage.setItem(_receiptKey, JSON.stringify(_receipts)); return true; } catch (_) { _receiptStorageBroken=true; return false; } }
let _viewVersion = 0;
let _connectionLost = false;
let _protocolReady = false;

// 旧页面发起可核对的本机请求，写入请求沿待确认回执身份交给服务端。
async function api(path, opt) {
  const o = Object.assign({}, opt || {});
  o.headers = Object.assign({}, o.headers || {});
  if (TOKEN) o.headers['X-Harness-Token'] = TOKEN;
  if (o.body && typeof o.body === 'object' && !(o.body instanceof FormData)) {
    o.headers['Content-Type'] ||= 'application/json';
    o.body = JSON.stringify(o.body);
  }
  const writing = (o.method || 'GET').toUpperCase() === 'POST';
  o.headers["X-Harness-Client-Version"]="1";
  if (writing && ["/models","/policy"].includes(path)) return {status:409,body:{error:"此写入口已迁移并验收，请使用返回新版按钮进入设置；其他兼容管理仍保留。"}};
  let fingerprint = '', requestId = '';
  const safeStop=/^\/(?:sessions|jobs)\/[A-Za-z0-9._-]{1,100}\/stop$/.test(path);
  if (writing && !safeStop && _receiptStorageBroken) return {status:409,body:{error:"回执存储损坏或不可写，未发送新请求；请先保留输入和原存储核对，不能清空后直接重放。"}};
  if (writing && !safeStop) {
    const health = await api('/health?full=1');
    if (health.status !== 200 || health.body.protocol !== HARNESS_PROTOCOL_VERSION || health.body.agentFirstClient!==2 || health.body.dataFormat?.compatible!==true || !health.body.instance) {
      return { status: 409, body: { error: '服务版本或连接未确认，请重开应用后重试；输入已保留' } };
    }
    _protocolReady = true;
  }
  if (writing) {
    const bytes = new TextEncoder().encode(path + '\n' + (o.body || ''));
    fingerprint = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)))
      .map(n => n.toString(16).padStart(2, '0')).join('');
    requestId = _receipts[fingerprint]?.id || crypto.randomUUID();
    _receipts[fingerprint] = { id: requestId, state: 'sending', path };
    if (!saveReceipts() && !safeStop) return {status:409,body:{error:"回执无法持久保存，尚未发送。请保留输入。"}};
    o.headers['X-Request-ID'] = requestId;
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), path === '/probe' ? 260000 : 15000);
  o.signal = controller.signal;
  try {
    const r = await fetch(API + path, o);
    const body = await r.json();
    if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('接口返回的不是 JSON 对象');
    _connectionLost = false;
    if ($('connection-status')) $('connection-status').hidden = true;
    if (writing) {
      if ((r.status === 202 || body.queued || body.resume?.queued) && _receipts[fingerprint]) {
        _receipts[fingerprint].state = body.queued || body.resume?.queued ? 'queued' : (body.state || 'processing');
      } else { delete _receipts[fingerprint]; }
      saveReceipts();
    }
    if (r.status === 401) showErr('接口鉴权失败，请重新打开应用以加载正确的接口钥匙。');
    return { status: r.status, body };
  } catch (e) {
    _connectionLost = true;
    if ($('connection-status')) $('connection-status').hidden = false;
    _protocolReady = false;
    if (writing && _receipts[fingerprint]) { _receipts[fingerprint].state = 'unknown'; saveReceipts(); }
    return { status: 0, body: { request_id: requestId || undefined, uncertain: writing,
      error: writing ? '请求结果待确认，输入已保留。重试同样内容会核对原请求，避免重复执行。'
                     : '本地服务暂时不可用：' + e.message } };
  } finally { clearTimeout(timer); }
}

let _receiptBusy = false;
// 定期核对旧页面仍未确认的写入，以原操作编号更新结果。
async function pollReceipts() {
  if (_receiptBusy) return;
  _receiptBusy = true;
  try {
    for (const [key, saved] of Object.entries(_receipts)) {
      if (saved.state === 'confirmed') continue;
      const r = await api('/operations/' + encodeURIComponent(saved.id));
      if (r.status !== 200 || !r.body.operation) continue;
      const op = r.body.operation;
      if (!validateProtocol('Operation', op)) { showErr('操作回执格式不兼容'); continue; }
      if (op.response?.reconciled) {
        delete _receipts[key];
      } else if (op.state === 'failed') {
        showErr('排队请求处理失败（' + saved.id + '）：' +
          (op.response?.error || op.response?.out?.error || '请查看会话日志；原请求未自动重发'));
        delete _receipts[key];
      } else if (op.state === 'completed') {
        if (saved.state !== 'sending') stateNote('原请求已收讫；Agent是否完成请核对会话实际进度', 7000);
        if (['unknown', 'notified'].includes(saved.state)) saved.state = 'confirmed';
        else delete _receipts[key];
      } else if (op.state === 'unknown' && saved.state !== 'notified') {
        showErr('请求 ' + saved.id + ' 在服务中断时结果未确认。请先核对会话及产出，系统不会自动重放。');
        saved.state = 'notified';
      }
      saveReceipts();
    }
  } finally { _receiptBusy = false; }
}

function showErr(msg) {
  const b = $('boot');
  b.textContent = msg;
  b.hidden = false;
}

function clearErr() {
  $('boot').hidden = true;
  $('boot').textContent = '';
}

// 主进程在启动时栽的跟头，通过 query 带进来（找不到 Python / 服务没起）
const bootMsg = new URLSearchParams(location.search).get('bootError');
if (bootMsg) showErr(bootMsg);

// ── 总况 ──
let _stateReadVersion = 0;
// 读取前端所需的实际服务配置和运行状态，驱动旧页面切换。
async function loadState() {
  const version = ++_stateReadVersion;
  const { status, body } = await api('/state');
  if (status !== 200 || version !== _stateReadVersion) return;
  const active = _sessById.get(cur);
  const effective = active?.effective_policy;
  $('gate').innerHTML = (active?.running ? '当前运行 <b>' + esc(effective ? labelOf(effective) : '待核对') + '</b> · ' : '')
    + '下次运行 <b>' + esc(labelOf(body.policy)) + '</b>';
  $('gate').className = 'pill' + (body.policy === 'trusted' ? ' warn' : '');
  // ★ 悬停就说清"这一档到底留了什么"（2026-10-26）：学习成本要落在"想知道的时候能问到"，
  //   而不是落在"必须先被拦一次"。
  $('gate').title = (body.policy === 'trusted')
    ? '全信任档：不再逐条问你。但**底线清单**仍然拒 —— 凭据/私钥（读写都拒）、'
      + '写系统目录、改「允许哪些动作」的配置（charter / sandbox_rules / policy）。'
      + '这三类连「允许」这个选项都不给。'
    : '门禁档位（内部名 policy）：决定这一轮要不要逐条问你。'
      + '选 trusted 也只是「不问」，底线不会跟着放开。';
  // 输入框上方也显示一份：人决定"这句话敢不敢发"的时候，眼睛在输入框那儿。
  // ★ 现在它是**能选的下拉**（2026-10-27），所以这里只负责把当前值摆对，
  //   不再拼字符串 —— 拼 innerHTML 会把 select 冲掉。
  const cgSel = $('cgatesel');
  if (cgSel && document.activeElement !== cgSel) cgSel.value = body.policy || 'workspace';
  const cgLbl = $('cgatestate');
  if (cgLbl) cgLbl.title = '下次运行的默认权限（「' + labelOf(body.policy) + '」）。'
    + '改到「信任」会先确认一次 —— 底线清单仍然拦凭据读写、写系统目录、改放权配置这三类。';
  _policy = body.policy || '';
  paintStatusBar();

  let h = '';
  h += '<div class="hr"></div><p class="mini">权限</p>';
  h += '<div class="li">' + esc(labelOf(body.policy)) + '</div>';
  h += '<div class="li" style="color:var(--faint);margin-top:4px">'
     + '这是门禁不是沙箱：它挡住越界的动作，但跑起的命令能在系统里做更远的事。</div>';

  if ($('sideextra')) $('sideextra').innerHTML = h;
}

function labelOf(p) {
  // ★ `trusted` 原来叫「全放（trusted）」—— 那个词本身就在教人"这一步什么都没有了"。
  //   实际它只关掉"逐条问你"，底线（凭据/系统写/自我放权）不动（2026-10-26）。
  //   措辞跟着改：名字要说出它**实际保留了什么**，否则人只能靠踩一次才知道。
  return p === 'readonly' ? '只读'
       : p === 'workspace' ? '工作目录内'
       : '信任·底线仍在';
}

// ── 会话 ──
// ★★ 这一块是"会话会自己打开又关掉"的根因。
//   原来每 1.5 秒 `box.innerHTML = 整段 html` —— 把所有节点推倒重建：
//     ① 用户手动折叠的项目组，下一次重建就全展开了（看起来像自己打开）；
//     ② hover / 选中 / 列表滚动位置全部丢失；
//     ③ 正在跑的那个会话 mtime 每秒都在变 ⇒ 签名一直在变 ⇒ 每秒重建 1~2 次，
//        视觉上就是列表在抖、在闪。
//   ⇒ 现在分两种更新：
//     结构没变（同一批会话、同样的顺序）⇒ **只改文字**，一个节点都不重建；
//     结构变了（新建 / 删除 / 换了项目）⇒ 才整体重建，并把折叠状态带回去。
let _sessStruct = '';

// ── 左栏：项目 → 会话 ──
// 布局与交互按成熟产品（ChatGPT 的会话列表 / Codex 的文件树 / Cursor 的侧栏）的通行做法：
//   顶部：当前项目 + 加项目
//   搜索：本地过滤
//   新会话：一条主按钮
//   列表：按项目分组（可折叠），每组里标题 + 相对时间，hover 右侧出「⋯」
function _relTime(t) {
  const ms = Number(t || 0) * 1000;
  if (!ms) return '';
  const d = new Date(ms), now = new Date();
  const pad = n => String(n).padStart(2, '0');
  const diff = now - d;
  // ★ 照 WorkBuddy 左栏的写法（截图里是「11分钟前」/「10月17日」）：
  //   近的说"多久之前"，远的说日期。`toLocaleString()` 那种「2026/10/26 01:15:14」
  //   一屏五行读不出远近 —— 而"多久之前"正是人排序时唯一在用的信息。
  if (diff >= 0) {
    if (diff < 60e3) return '刚刚';
    if (diff < 3600e3) return Math.floor(diff / 60e3) + '分钟前';
    if (d.toDateString() === now.toDateString()) return Math.floor(diff / 3600e3) + '小时前';
  }
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return '昨天';
  if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + '月' + d.getDate() + '日';
  return d.getFullYear() + '年' + (d.getMonth() + 1) + '月';
}

// 每组默认露 8 条，多的折进「查看更多 (n)」（WorkBuddy 也是这个做法）。
// ★ 用它而不是折叠整组：折叠会把"这一组有什么"一起藏掉，人还得点开才知道有没有找过。
const _SESS_CAP = 8;
let _showAll = new Set();

function _sessHtml(s) {
  const lb = _sessLabel(s);
  // ★ 旧格式标记（2026-10-26）：10-24 之前的会话轨迹叫 `trace.jsonl`
  //   （整段一行、循环结束才落盘），现在读的是逐行的 `events.jsonl`。
  //   这种会话**读得出、续不了** —— 不标出来，人会点「继续」然后收到一条看不懂的报错。
  //   标成"只读"比藏起来好：翻旧账是真实需求，藏起来等于把过去删了。
  return '<div class="sess' + (s.id === cur ? ' on' : '') + '" data-id="' + esc(s.id)
       + '" title="' + esc(lb + '　' + s.id
           + (s.legacy ? '　（旧格式：能看，不能续跑）' : '')) + '">'
       + (s.running ? '<span class="dot"></span>' : '')
       + (s.legacy ? '<span class="lgc">旧</span>' : '')
       + '<span class="ntx">' + esc(lb) + '</span>'
       + '<span class="mt">' + _relTime(s.mtime) + '</span>'
       + '<button class="more" data-more="' + esc(s.id) + '" title="更多">⋯</button>'
       + '</div>';
}

function _groupHtml(key, title, list) {
  const open = _showAll.has(key);
  const show = open ? list : list.slice(0, _SESS_CAP);
  let h = '<div class="proj" data-proj="' + esc(key) + '">'
        + '<div class="pjhead"><span class="pjname" title="' + esc(title) + '">'
        + esc(title) + '</span><span class="pjnum">(' + list.length + ')</span></div>'
        + '<div class="pjlist">';
  show.forEach(s => { h += _sessHtml(s); });
  const left = list.length - show.length;
  if (left > 0) {
    h += '<button class="smore" data-more-group="' + esc(key) + '">查看更多 (' + left + ')</button>';
  }
  return h + '</div></div>';
}

// 左栏那一行的**可见文字**：人写的那句话，没有才退回目录名。
// ★ 抽成函数而不是两处各写一遍：原来"整体重建"写 `s.title || s.id`、
//   "增量更新"写 `s.id`，于是标题只活到下一次轮询（同一件事两种写法，看不出错）。
// ★ 不再在后面拼「· 进行中」—— 换成前面一个绿点（见 .sess .dot）：
//   拼在标题后面会把长标题挤掉一截，而"在跑"这件事扫一眼就该看到，不用读字。
function _sessLabel(s) {
  const t = String((s && s.title) || '').trim();
  return t || String((s && s.id) || '');
}

// 分组键：**归一化后**的目录。
// ★★ 这是 2026-10-26 查出来的真 bug：原来直接拿 `s.cwd` 当 key，
//   而同一个目录会有几种写法（反斜杠 / 正斜杠 / 双斜杠 / 大小写），
//   于是**同一个项目被拆成好几组**，而显示名都取最后两段 —— 左栏看起来就是
//   三行一模一样的「WorkBuddy/agent-harness 1」，每行下面挂一个会话。
//   Windows 路径大小写不敏感，所以统一小写；显示仍用原始字符串。
function _projKey(cwd) {
  return String(cwd || '').replace(/\\/g, '/').replace(/\/+/g, '/')
                           .replace(/\/+$/, '').toLowerCase();
}

let _sessQ = '', _sessQTimer = 0, _sessById = new Map();

let _sessionListVersion = 0;
// 根据后端会话归属与当前筛选重建侧栏。
async function loadSessions() {
  const version = ++_sessionListVersion;
  const { status, body } = await api('/sessions?limit=80');
  if (status !== 200 || version !== _sessionListVersion) return;
  const all = body.sessions || [];
  const box = $('sessions');
  _sessById = new Map(all.map(s => [s.id, s]));

  // ★ 空会话不列：点几次「新建会话」就会留几个「新对话 · 0 条」，
  //   它们没有任何内容、也永远不会跑，却把列表顶下去。
  //   当前选中那个例外 —— 人刚建的必须看得见，否则会以为没建上。
  const q = _sessQ.trim().toLowerCase();
  const shown = all.filter(s => {
    if (q) return (_sessLabel(s) + ' ' + (s.cwd || '')).toLowerCase().includes(q);
    return !s.empty || s.id === cur;
  });

  // ★ 置顶单独一组（照 WorkBuddy 的「置顶 (2)」）。置顶的那些**不再出现在**项目组里 ——
  //   同一个会话出现两次，人会以为存在两个。
  const pinned = shown.filter(s => s.pinned);
  const rest = shown.filter(s => !s.pinned);

  // ★ 按 cwd 分组（key 已归一化，见 _projKey）。组里留**原始** cwd：显示它、新建也用它。
  const groups = new Map();
  rest.forEach(s => {
    const k = _projKey(s.cwd) || '(unknown)';
    if (!groups.has(k)) groups.set(k, { cwd: s.cwd || '', list: [] });
    groups.get(k).list.push(s);
  });
  fillProjects([...groups.values()].map(g => g.cwd).filter(Boolean));

  // 结构签名：哪些会话、哪些置顶、在哪个组、是否在跑、搜索词、哪组展开着。
  // ★ mtime / 条数故意不进来 —— 它们每秒都变，进来就等于每轮重建（重建会丢滚动位置）。
  const struct = 'q:' + q + '|o:' + [..._showAll].sort().join(',') + '|'
    + 'pin>' + pinned.map(s => s.id + (s.running ? '*' : '')).join(',') + '|'
    + [...groups].map(([k, g]) =>
        k + '>' + g.list.map(s => s.id + (s.running ? '*' : '')).join(',')).join('|');

  if (struct === _sessStruct && box.querySelector('.sess')) {
    // ── 结构没变：只更新文字（时间、选中态、标题；改名走的也是这里）──
    for (const s of shown) {
      const el = box.querySelector('.sess[data-id="' + CSS.escape(s.id) + '"]');
      if (!el) continue;
      if (el.classList.contains('on') !== (s.id === cur)) el.classList.toggle('on', s.id === cur);
      const tx = el.querySelector('.ntx');
      const lb = _sessLabel(s);
      if (tx && tx.textContent !== lb) { tx.textContent = lb; el.title = lb; }
      const mt = el.querySelector('.mt');
      const t = _relTime(s.mtime);
      if (mt && mt.textContent !== t) mt.textContent = t;
    }
  } else {
    // ── 结构变了：整体重建 ──
    _sessStruct = struct;
    const live = new Set([...(groups.keys()), '__pin__']);
    [..._showAll].forEach(k => { if (!live.has(k)) _showAll.delete(k); });

    let html = '';
    if (pinned.length) html += _groupHtml('__pin__', '置顶', pinned);
    for (const [k, g] of groups) html += _groupHtml(k, shortPath(g.cwd), g.list);
    box.innerHTML = html || ('<div class="empty" style="padding:14px;color:var(--faint)">'
      + (_sessQ ? '没有匹配的会话。' : '还没有会话。上面点「新建会话」开始。') + '</div>');

    box.querySelectorAll('.sess').forEach(el => {
      el.onclick = ev => {
        if (ev.target.closest && ev.target.closest('.more')) return;
        pick(el.dataset.id);
      };
      // 右键也出同一个菜单（成熟产品都有；而且它比 hover 的「⋯」更好按中）
      el.oncontextmenu = ev => {
        ev.preventDefault(); ev.stopPropagation();
        showSessMenu(el, el.dataset.id);
      };
    });
    box.querySelectorAll('.more').forEach(el => {
      el.onclick = ev => { ev.stopPropagation(); showSessMenu(el, el.dataset.more); };
    });
    box.querySelectorAll('.smore').forEach(el => {
      el.onclick = ev => {
        ev.stopPropagation();
        _showAll.add(el.dataset.moreGroup);
        _sessStruct = '';
        loadSessions();
      };
    });
  }

  // ★ 首屏：没有当前会话就自动选中最近那个。
  //   否则中间一片空白 —— 看起来像"它没去读会话目录"，实际是没选中。
  // ★★ 但 L4 正文流也把 cur 置空（它不是普通会话）。这里若不拦住，
  //   每 1.5 秒一次的会话刷新会把最近会话自动选中并 clearFeed ——
  //   表现就是"正文刚出来又整块消失"。2026-11-01 实测撞到。
  const _inJobLive = (typeof _jobLive !== 'undefined' && !!_jobLive);
  if (!cur && all.length && !_inJobLive) {
    // ★ 首屏自动选中最近会话时，第一批事件是**历史重放** ——
    //   必须算历史，否则重开软件就会把几天前的采访弹窗糊到脸上。
    //   上一轮为了修"新会话里采访不弹"，把这里也改成了 false，于是修好一个、
    //   撞坏另一个：重开就弹旧采访。这两处需求相反，不能一起改。
    if(window.harnessCompatContextChange) window.harnessCompatContextChange(all[0].id);
    cur = all[0].id; seen = 0; lastLog = ''; _booting = true;
    _restoreClock();          // 首屏：如果这个会话在跑，秒表接着算（要在 cur 赋值之后）
    _stat = { model: '', tokens: 0 };
    _lastSay = ''; _tools = []; _nodes = []; paintNodes();
    clearFeed(); $('thinking').hidden = true; $('streaming').hidden = true;
    paintStatusBar();
    _sessStruct = '';            // 选中态要重画一次
    loadSessions();
    poll();
  }
}

// 路径太长，只显示最后两段（…/agent-harness），完整路径放 title
function shortPath(p) {
  const parts = String(p || '').split(/[\\/]/).filter(Boolean);
  return parts.length <= 1 ? (p || '(未指定)') : parts.slice(-2).join('/');
}

function fillProjects(keys) {
  const sel = $('proj');
  const opts = keys.slice();
  if (curProj && !opts.includes(curProj)) opts.unshift(curProj);
  if (!opts.length) opts.push(curProj || '');
  if (!opts.includes(curProj) && opts.length) curProj = opts[0];
  // ★ 项目集合没变就别重建 `innerHTML` —— 每 1.5 秒重建一次会让正在展开的
  //   下拉框自己收起，而人会以为"我点错了"。
  const sig = opts.join('\u0001');
  if (sel.dataset.sig === sig && sel.options.length === opts.length) {
    if (curProj && sel.value !== curProj) sel.value = curProj;
    return;
  }
  sel.dataset.sig = sig;
  sel.innerHTML = opts.map(k =>
    `<option value="${esc(k)}"${k === curProj ? ' selected' : ''}>${esc(shortPath(k))}</option>`
  ).join('');
}

function askPath() {
  return new Promise(res => {
    const d = $('dlg');
    $('dlgpath').value = curProj || '';
    d.returnValue = '';
    d.onclose = () => res(d.returnValue === 'ok' ? ($('dlgpath').value || '').trim() : '');
    d.showModal();
  });
}

async function addProject() {
  const p = await askPath();
  if (!p) return;
  curProj = p;
  localStorage.setItem('harness.proj', p);
  const keys = [...new Set([...$('proj').options].map(o => o.value).concat([p]))];
  fillProjects(keys);
  $('proj').value = p;
  stateNote('新会话会放在 ' + shortPath(p), 6000);
}

// ── 会话的「⋯」菜单：重命名 / 删除 ──
// ★ 为什么把删除从"hover 就出现在鼠标路径上的 ×"收进菜单：
//   两个动作的风险完全不对称 —— 删除不可逆，改名可逆。
//   把不可逆的那个做成最容易误点的形状，是设计上的错配。
//   （ChatGPT / Cursor / Codex 都是收进菜单的，不是巧合。）
let _menuSid = '';

function showSessMenu(anchor, id) {
  const m = $('sessmenu');
  if (!m) return;
  _menuSid = id;
  const _s = _sessById.get(id) || {};
  // ★ 置顶（照 WorkBuddy 的「置顶 (2)」）。它是**可逆**动作，所以和"重命名"同层；
  //   删除单独放最下并标红 —— 三个动作里只有它不可逆。
  m.innerHTML = '<button data-act="pin">' + (_s.pinned ? '取消置顶' : '置顶') + '</button>'
              + '<button data-act="rename">重命名</button>'
              + '<button data-act="delete" class="danger">删除</button>';
  m.hidden = false;
  // 贴着按钮右下：靠近右边界时往左让，避免出屏
  const r = anchor.getBoundingClientRect();
  const w = m.offsetWidth || 132;
  m.style.left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8)) + 'px';
  const h = m.offsetHeight || 64;
  let top = r.bottom + 4;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
  m.style.top = top + 'px';
}

function hideSessMenu() {
  const m = $('sessmenu');
  if (m && !m.hidden) m.hidden = true;
  _menuSid = '';
}

// ── 技能分页（2026-10-26）──
// 和执行流占同一个位置，开哪个显示哪个。`body.skillmode` 只负责"谁让位"，
// 不改 DOM 结构 —— 搬元素会丢滚动位置和事件绑定，而这里是同一个页面的两个视图。
let _skillOn = false;

// 现在这一页叫「装了什么」，两个页签：技能 / 外部工具（MCP）。
// ★ 为什么把 MCP 也放进来：它和技能回答的是同一个问题——
//   "我这套系统现在多了哪些本事"。原来技能在界面里、MCP 只在配置文件里，
//   等于一半能看见、一半只能靠记。
let _spTab = 'skills';

function skillMode(on) {
  if (on && typeof jobMode === 'function') jobMode(false);   // 视图互斥
  _skillOn = !!on;
  const p = $('skillpage');
  if (!p) return;
  p.hidden = !on;
  document.body.classList.toggle('skillmode', on);
  syncFlowVisibility();
  if (on) loadPanel();
}

function spTab(name) {
  _spTab = (name === 'mcp') ? 'mcp' : 'skills';
  document.querySelectorAll('#sptabs .sptab').forEach(b => {
    b.classList.toggle('on', b.dataset.tab === _spTab);
  });
  // 「装一个」只对技能有意义：MCP 没有安装接口，它是改配置文件的事。
  // 藏起来而不是禁用 —— 一个灰掉的按钮会让人反复点它试为什么不行。
  const inst = $('spinstall');
  if (inst) inst.hidden = _spTab !== 'skills';
  if (_spTab === 'skills') loadSkills(); else loadMcp();
}

function loadPanel() {
  spTab(_spTab);
}

// 外部工具（MCP）：只读配置，不连接 —— 连一次要起进程/发请求，
// 面板一打开就全连，一个卡住的 server 会让整页转圈。
async function loadMcp() {
  const box = $('splist');
  if (!box) return;
  const r = await api('/mcp');
  const list = (r.body && r.body.servers) || [];
  const cfgPath = (r.body && r.body.config) || '';
  _tabCounts.mcp = list.length;
  paintTabCounts();
  if (!list.length) {
    box.innerHTML = '<div class="skempty">还没配外部工具。<br>'
      + '配置文件：<code>' + esc(cfgPath) + '</code><br><br>'
      + '写一段这样的进去，<b>下一轮任务</b>就会多出这些工具：<br>'
      + '<code>{"mcpServers": {"github": {"command": "npx", '
      + '"args": ["-y", "@modelcontextprotocol/server-github"], '
      + '"deny": ["delete_repo"]}}}</code><br>'
      + '托管型的服务器把 <code>command</code> 换成 <code>url</code> 就行。</div>';
    return;
  }
  box.innerHTML = list.map(s => {
    const transport = s.transport === 'http' ? 'HTTP' : '本地进程';
    const flags = [];
    if (s.allow.length) flags.push('放行 ' + s.allow.join('、'));
    if (s.deny.length) flags.push('禁 ' + s.deny.join('、'));
    flags.push('默认' + (s.approval === 'auto' ? '免审批' : '要点头'));
    flags.push('输出上限 ' + s.budget + ' token');
    return extensionCard(s.name, transport, s.disabled ? '已停用' : '已配置',
      'data-mcp="' + esc(s.name) + '" data-target="' + esc(s.target) + '" data-flags="' + esc(flags.join(' · ')) + '"');
  }).join('');
}

function extensionCard(name, description, status, attributes) {
  return '<button type="button" class="skcard" aria-haspopup="dialog" ' + attributes
    + '><strong class="sk-name">' + esc(name) + '</strong>'
    + '<span class="sk-desc">' + esc(description) + '</span>'
    + '<small class="sk-status">' + esc(status) + '</small></button>';
}

function showExtensionDetail(card) {
  const name = card.dataset.name || card.dataset.mcp;
  const dialog = $('extensionDetail'), content = $('extensionDetailContent');
  $('extensionDetailTitle').textContent = name;
  content.replaceChildren();
  const description = document.createElement('p');
  description.textContent = card.querySelector('.sk-desc').textContent;
  content.append(description);
  const source = document.createElement('p');
  source.className = 'sk-meta';
  source.textContent = card.dataset.target || card.dataset.directory || '';
  content.append(source);
  const body = document.createElement('div');
  body.className = 'sk-body';
  if (card.dataset.mcp) {
    const flags = document.createElement('p');
    flags.textContent = card.dataset.flags;
    content.append(flags);
    const probe = document.createElement('button');
    probe.className = 'pill';
    probe.textContent = '查看工具';
    probe.onclick = async () => {
      probe.disabled = true;
      body.hidden = false;
      try {await probeMcp(name, body);} finally {probe.disabled = false;}
    };
    content.append(probe);
    body.hidden = true;
  } else {
    const remove = document.createElement('button');
    remove.className = 'pill danger';
    remove.textContent = '卸载';
    remove.onclick = async () => {
      remove.disabled = true;
      try {if(await removeSkill(name))dialog.close();}
      finally {remove.disabled = false;}
    };
    content.append(remove);
  }
  content.append(body);
  dialog.showModal();
  if (!card.dataset.mcp) void revealSkill(name, body);
}

// The server is contacted only after the user chooses to inspect its tools.
async function probeMcp(name, body) {
  body.textContent = '正在连 ' + name + '…（要起进程或发请求，可能几秒）';
  const r = await api('/mcp/' + encodeURIComponent(name) + '/probe');
  const b = r.body || {};
  if (r.status !== 200 || b.ok === false) {
    body.textContent = '连不上：' + (b.error || ('HTTP ' + r.status));
    return;
  }
  const tools = b.tools || [];
  if (!tools.length) { body.textContent = '连上了，但它的工具清单是空的。'; return; }
  body.innerHTML = tools.map(t => {
    const cls = t.denied ? 'deny' : (t.need_approval ? 'ask' : 'ok');
    const txt = t.denied ? '不注册（模型看不见）' : (t.need_approval ? '要点头' : '免审批');
    return '<div class="tk"><span class="tkname">' + esc(t.name) + '</span>'
      + '<span class="tkd">' + esc(t.desc || '') + '</span>'
      + '<span class="tkflag ' + cls + '">' + txt + '</span></div>';
  }).join('');
}

async function loadSkills() {
  const r = await api('/skills');
  const list = (r.body && r.body.skills) || [];
  _tabCounts.skills = list.length;
  paintTabCounts();
  const box = $('splist');
  if (!box) return;
  if (!list.length) {
    box.innerHTML = '<div class="skempty">还没装技能。<br>'
      + '把技能的 <code>.zip</code> 拖进输入区，或者点右上「装一个」。<br>'
      + '技能包必须有 <code>SKILL.md</code>（开头是 '
      + '<code>--- name / description ---</code>）。</div>';
    return;
  }
  box.innerHTML = list.map(s => extensionCard(s.name, s.desc || '暂无用途说明', s.level,
    'data-name="' + esc(s.name) + '" data-directory="' + esc(s.dir) + '"')).join('');
}

async function revealSkill(name, body) {
  body.textContent = '读取中…';
  const r = await api('/skills/' + encodeURIComponent(name));
  if (r.status !== 200) {
    body.textContent = '读不到：' + ((r.body && r.body.error) || ('HTTP ' + r.status));
    return;
  }
  const b = r.body || {};
  body.textContent = (b.body || '（这份 SKILL.md 只有 frontmatter，没有正文）')
    + (b.files && b.files.length ? '\n\n—— 附带文件 ——\n' + b.files.join('\n') : '');
}

async function removeSkill(name) {
  // ★ 卸载不可逆（目录直接删）。确认框是必须的，和删会话同一条道理。
  if (!confirm('卸载技能「' + name + '」？\n\n它所在的目录会被删掉，收不回来。')) return;
  const r = await api('/skills/remove', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, confirm: true }),
  });
  if (r.status !== 200) {
    showErr('卸载失败：' + ((r.body && r.body.error) || ('HTTP ' + r.status)));
    return;
  }
  clearErr();
  stateNote('技能「' + name + '」已卸载', 6000);
  loadSkills();
  return true;
}

// 左栏那个数字：没装时不显示（显示 0 只是噪音）
// 左栏那个数字要**两边都算**：只看技能数会漏掉 MCP ——
// 人看到"3"却不知道其中两样是外部工具。
const _tabCounts = { skills: 0, mcp: 0 };

function paintTabCounts() {
  const sk = $('tabsk'), mc = $('tabmcp'), el = $('skillnum');
  if (sk) sk.textContent = _tabCounts.skills ? String(_tabCounts.skills) : '';
  if (mc) mc.textContent = _tabCounts.mcp ? String(_tabCounts.mcp) : '';
  if (!el) return;
  const a = _tabCounts.skills, b = _tabCounts.mcp;
  // 两边都有时写成 `2+1`：单看一个数字分不清这 3 个是技能还是服务器
  el.textContent = (a && b) ? (a + '+' + b) : (a ? String(a) : (b ? String(b) : ''));
}

async function refreshSkillCount() {
  const r = await api('/skills');
  _tabCounts.skills = ((r.body && r.body.skills) || []).length;
  try {
    const m = await api('/mcp');
    _tabCounts.mcp = ((m.body && m.body.servers) || []).length;
  } catch (e) { /* MCP 读不到不影响技能那块显示 */ }
  paintTabCounts();
}

async function togglePin(id) {
  const s = _sessById.get(id) || {};
  const r = await api('/sessions/' + encodeURIComponent(id) + '/pin', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pinned: !s.pinned }),
  });
  if (r.status !== 200) {
    showErr('置顶没改成：' + ((r.body && r.body.error) || ('HTTP ' + r.status)));
    return;
  }
  clearErr();
  _sessStruct = '';           // 分组变了，必须重建
  loadSessions();
}

// 组织操作使用原会话 ID，编辑框确认后才保存并重读列表。
async function renameSession(id) {
  const s = _sessById.get(id);
  const d = $('renamedlg');
  if (!d) return;
  $('renametxt').value = (s && s.title) || '';
  d.returnValue = '';
  d.showModal();
  // 一次性监听：dialog 的 close 不会自己解绑，重复改名会叠加回调
  const onDone = async () => {
    d.removeEventListener('close', onDone);
    if (d.returnValue !== 'ok') return;
    const title = ($('renametxt').value || '').trim();
    const r = await api('/sessions/' + encodeURIComponent(id) + '/rename', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    });
    if (r.status !== 200) {
      showErr('改名没成功：' + ((r.body && r.body.error) || ('HTTP ' + r.status)));
      return;
    }
    clearErr();
    _sessStruct = '';           // 标题变了，强制重建一次
    loadSessions();
  };
  d.addEventListener('close', onDone);
}

async function delSession(id) {
  if (running && id === cur) { showErr('这个会话还在跑，停下再删'); return; }
  if (!confirm('删除会话 ' + id + ' ？\n\n它的记录和产物都会删掉，收不回来。')) return;
  const r = await api('/sessions/' + encodeURIComponent(id) + '/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm: true }),
  });
  if (r.status !== 200) { showErr('没删掉：' + (r.body.error || r.status)); return; }
  clearErr();
  if (id === cur) {
    // ★ 删掉的正好是当前会话 —— **先挑一个邻居**，没有才新建（2026-10-27 用户：
    //   「删掉会话的时候，有时候会自动跳新建会话，很奇怪」）。
    //   原来这里无条件 `newSession()`：人只是想清掉一个旧会话，却被弹到一个
    //   空白新会话上，左上角的上下文也跟着跳走。列表全是会话时，最自然的落点
    //   是"最近的那个"，而不是"新开一个"。
    await loadSessions();
    const next = [..._sessById.values()].find(s => s.id !== id);
    if (next) pick(next.id);
    else newSession();
    return;
  }
  loadSessions();
}

// 切换旧会话时先切换计时和草稿归属，再重置需要从历史恢复的界面状态。
function pick(id) {
  if (typeof jobMode === 'function' && _jobOn) jobMode(false);
  if (id === cur) return;
  // 从 L4 正文流切回普通会话：先停掉任务轮询，否则它会把会话正文当成
  // L4 的卡片继续往上追加（看起来像两个会话混在一起）。
  if (typeof jobLiveStop === 'function') jobLiveStop();
  _saveClock();                 // 切走前把**这个会话**的秒表存下来
  if(window.harnessCompatContextChange) window.harnessCompatContextChange(id);
  cur = id; seen = 0; lastLog = '';
  _restoreClock();              // 切回来接着算，不是从 0 重新计
  _booting = true;              // 点开的是**旧会话** ⇒ 这一批事件是历史重放
  _stat = { model: '', tokens: 0 };
  _lastSay = '';
  // ★★ 换会话必须把挂着的提问一起收掉（2026-10-26 修的真 bug）：
  //   弹窗原来不属于任何会话 —— 它只是读全局 `cur` 去 POST /say。
  //   于是"在 A 的提问上点了选项、但人已经切到 B" ⇒ **答案发给了 B**。
  //   不报错，B 收到一句莫名其妙的话就接着干 —— 这是最难查的那类错。
  //   问题本身不会丢：它挂在 A 的事件流里，下次打开 A 还会弹（status=needs_human）。
  dropAsk();
  clearFeed();
  const selected = _sessById.get(id);
  _stopping = selected?.runtime?.state === 'stopping';
  setRunning(!!selected?.running);
  // ★ 进度节点必须跟着会话走（2026-10-27 用户报「右边的进度浮卡也串到别的会话了」）。
  //   原来只在首屏自动选中时清过一次 —— 手动切会话时 `_nodes` 还留着上一个会话的
  //   步数，于是新会话明明什么都没跑，右边却显示"上一个会话的第 7 步"。
  //   这类错的可怕之处是它**看起来合理**：数字在、进度在，只是不属于这个会话。
  _nodes = []; _tools = []; _lastSay = '';
  paintNodes();
  // ★ 不能写 $('thinking').textContent = '' —— 那会把它的**子元素一起删掉**
  //   （#thead 标题和 #tbody 正文），删完 getElementById('tbody') 就是 null，
  //   打字机从此没有画布：界面表现为"正在思考"块一直空着，而且不报错。
  //   实测就是这么空跑了一整轮。清内容用 resetThink()，它只清正文、保结构。
  resetThink();
  paintStatusBar();
  loadSessions();
  poll();
  scrollFeedToBottom();
}

// ★★ 磁吸只在底部生效（2026-10-31 用户：「置底吸力太大了，人来拽都有点拽不动」）。
//   规则只有一句：**此刻贴底就跟着走，离开底部就不再拉**。
//   不区分"这次滚动是人干的还是程序干的"：程序置底后 gap 仍是 0，标记保持 true；
//   人往上拖，gap 立刻 >2，标记变 false，之后来多少新内容都不会被拽回去；
//   人自己拖回底部，标记重新变 true，吸附恢复。
let _stickBottom = true;

function _bindStickBottom() {
  const f = $('feed');
  if (!f) return;
  f.addEventListener('scroll', () => {
    _stickBottom = (f.scrollHeight - f.scrollTop - f.clientHeight) <= 2;
  }, { passive: true });
}

// 钉到底。★ 单独一个函数是因为它出现在三处，而"到底"的定义只该有一处。
//   显式调它（回到底部按钮 / 切会话）= 人明确要回底，顺便把吸附重新打开。
function scrollFeedToBottom() {
  const f = $('feed');
  _stickBottom = true;
  if (f) f.scrollTop = f.scrollHeight;
}
_bindStickBottom();

// ★ 清空执行流，但**留住**那两个"活的"块（正在思考 / 正在说）。
//   直接 innerHTML='' 会把它们一起删掉，删完之后 $('thinking') 是 null，
//   流式就永久失效 —— 而且界面看起来一切正常，只是"再也不动了"。
//   所以：先取引用，再清空，最后挂回末尾。
let _flowTab = 'execution';       // 已废弃：2026-10-31 起改回单页 + 思考折叠块
let _finalArchived = false;
let _pendingArchive = false;

function syncFlowVisibility() {
  const covered = !!_skillOn || (typeof _jobOn !== 'undefined' && !!_jobOn);
  const f = $('feed');
  if (f) f.hidden = covered;
  const bottom = $('tobottom');
  if (bottom) bottom.hidden = covered || !f
    || (f.scrollHeight - f.scrollTop - f.clientHeight) <= 220;
}

// 清空旧正文和过程容器时同步重置事件缓存与流式锚点。
function clearFeed(msg) {
  _viewVersion++;
  if (typeof resetEventState === 'function') resetEventState();
  if ($('pending-status')) $('pending-status').hidden = true;
  const f = $('feed');
  if (!f) return;
  // ★ 保留清单必须包含**所有**活块：新增 #doing（当前动作条）时漏了它，
  //   于是新建会话那一刻它被删掉，之后 `$('doing').hidden` 直接抛 TypeError。
  const keep = [$('thinking'), $('streaming'), $('doing')].filter(Boolean);
  f.innerHTML = msg || '';
  // 拦截卡停在输入栏上方，切会话/新建时必须一起收走，否则会把上一会话的
  // 「给不给」带到这一会话的输入栏上（2026-10-31）。
  const dock = $('askdock');
  if (dock) { dock.hidden = true; dock.innerHTML = ''; }
  if (typeof resetLiveBlocks === 'function') resetLiveBlocks();
  _finalArchived = false;
  _pendingArchive = false;
  if (typeof resetThinkPanel === 'function') resetThinkPanel();
  keep.forEach(el => { el.hidden = true; f.appendChild(el); });
  const sb = $('sbody');
  if (sb) sb.textContent = '';
  // ★ 过程并组的状态也要清：不清的话下一次工具会 append 到已删除的节点上，
  //   表现是"工具调用不显示"（而且不报错）。
  _procFold = null; _procActs = [];
  // 去重状态也一样：换会话/清屏后，上一条 .line 已经不在文档里了。
  _lastLineHtml = '';
  // ★ 这里**不再**无条件设 _booting。
  //   原来在这里设 true，后果是：新建会话后的第一批事件（包括"开工前采访"的 ask_human）
  //   被当成"历史重放"吞掉，**弹窗不出现** —— 用户看到的是"契约写完就没反应了"。
  //   ⇒ 由调用方决定：点开旧会话（pick）才算重放，新会话/首屏都是新事件。
}

// ★ 那两个"活的"块永远待在执行流末尾。
//   事件是 append 进来的，不搬一下，新事件就会插到它们后面 ——
//   于是"正在思考"跑到历史上面去了。
function liftTail() {
  // ★ 只搬"正在说"的气泡。思考块**不搬** ——
  //   它是一轮的开头，搬到最后会让过程跑到思考上面（因果颠倒）：
  //   实测截图就是「看 README.md…」压在「思考中」上面。
  //   同一个函数 120ms 调一次，已经在末尾就别动（白搬一次就是白重排一次）。
  const f = $('feed');
  const el = $('streaming');
  if (!f || !el || el.hidden || !el.isConnected) return;
  if (f.lastElementChild === el) return;
  f.appendChild(el);
}


// ── 外观：暗色 / 密度 / 字号（2026-10-28 用户）──────────────────────────────
// ★ 放在**最先加载**的这个文件里，并且在末尾立刻 applyLook()：
//   晚于它渲染的话，页面会先按默认色画一遍再变，开屏闪一下。
// ★ 设置只存 localStorage：这是"这台机器上你看着舒服"的偏好，不是项目数据，
//   不进后端、不进仓库、不跟着会话走。
const LOOK_KEY = 'harness.look.v1';
let look = { theme: 'light', density: 'cozy', fs: 14 };
try { Object.assign(look, JSON.parse(localStorage.getItem(LOOK_KEY) || '{}')); } catch (e) {}

// 按已保存的旧页面显示偏好更新主题及过程展示。
function applyLook() {
  const r = document.documentElement;
  r.dataset.theme = look.theme;
  r.dataset.density = look.density;
  r.style.setProperty('--fs', look.fs + 'px');
  try { localStorage.setItem(LOOK_KEY, JSON.stringify(look)); } catch (e) {}
  const pop = $('lookpop');
  if (pop) {
    pop.querySelectorAll('.lopt').forEach(b => {
      const on = (b.dataset.k === 'theme' && b.dataset.v === look.theme)
        || (b.dataset.k === 'density' && b.dataset.v === look.density)
        || (b.dataset.k === 'fs' && Number(b.dataset.v) === Number(look.fs));
      b.classList.toggle('on', on);
    });
  }
}

function setLook(k, v) { look[k] = v; applyLook(); }

(function buildLook() {
  const bar = document.querySelector('.bar');
  if (!bar) return;
  const row = (label, k, opts) => '<div class="lrow"><span>' + label + '</span>'
    + '<span class="lseg">'
    + opts.map(o => '<button class="lopt" data-k="' + k + '" data-v="' + o[0] + '">'
      + o[1] + '</button>').join('')
    + '</span></div>';

  const btn = document.createElement('button');
  btn.id = 'lookbtn';
  btn.className = 'pill lookbtn';
  btn.textContent = '外观';
  btn.title = '暗色 / 密度 / 字号（只存在这台机器上）';
  const anchor = $('openstate');
  if (anchor) bar.insertBefore(btn, anchor); else bar.appendChild(btn);

  const pop = document.createElement('div');
  pop.id = 'lookpop';
  pop.className = 'lookpop';
  pop.hidden = true;
  pop.innerHTML = row('主题', 'theme', [['light', '浅色'], ['dark', '深色']])
    + row('密度', 'density', [['cozy', '舒适'], ['compact', '紧凑']])
    + row('字号', 'fs', [[13, '小'], [14, '标准'], [16, '大']]);
  document.body.appendChild(pop);

  btn.onclick = ev => { ev.stopPropagation(); pop.hidden = !pop.hidden; };
  pop.onclick = ev => {
    const b = ev.target.closest('.lopt');
    if (b) setLook(b.dataset.k, b.dataset.k === 'fs' ? Number(b.dataset.v) : b.dataset.v);
  };
  document.addEventListener('click', ev => {
    if (!pop.hidden && !pop.contains(ev.target) && ev.target !== btn) pop.hidden = true;
  });
})();

applyLook();
