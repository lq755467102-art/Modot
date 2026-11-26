// ── 正在跑的计时 ──
// ★ 用户 2026-10-25 的原话是这个功能的规格：
//   「是否可以有一个计时器，像 workbuddy 这样计时。有两个好处，一个是告诉用户
//     agent 还在思考，比『在跑』这种静态文字描述要好得多；其次，这个计时器前面可以加
//     正在跑的 agent —— 如果自我和本我都在跑，那就能让用户一眼看出来我们这个双线
//     harness 是存在的，而不是噱头。也可以让用户知道现在是哪个 agent 在思考，不至于
//     出现本我实际在跑、但是用户以为他没跑或者跑完了的情况。」
// ★ 为什么这一条特别值钱：本我要跑 1~3 分钟，而自我通常早就说完了。
//   只写"在跑"（或者写"空闲"）的时候，两种完全不同的状态长得一模一样：
//      ① 活干完了、本我也核完了（真的收工）
//      ② 活干完了、本我还在这 71 秒里读流水（还在干活）
//   用户分不出来，于是只能猜 —— 而这一轮的实测数据是：本我跑了 71 秒才出结论。
//   把"谁在跑、跑了多久"显示出来，这个歧义就不存在了。
// 主要引用或加载入口包括 `desktop/renderer/index.html`。
let _selfSince = 0, _benniSince = 0, _bModel = '';
// 这一轮（最近一条 say）花了多久。★ 用全局而不是塞进事件：事件是接口数据，
//   往里挂界面状态，下次序列化或比较就多出一个不该有的字段。
let _turnMs = 0;
// ★ 挂着的那个提问（没回答就一直挂着）。放在这里而不是弹窗那一节，
//   因为**状态标签要读它**：paintPill 靠它判断"现在该说等你回话还是收尾中"。
//   声明必须早于第一次 paintPill 调用，否则是 TDZ 报错（不是 undefined，是直接抛）。
let _pendingAsk = null;
// 这个提问属于哪个会话。★ 单独一个变量、不塞进 _pendingAsk：
//   事件对象是从接口拿来的数据，往上挂界面状态会让"数据"和"状态"混在一起，
//   下次序列化或比较时就多出一个不该有的字段。
let _askFor = '';
// 一次性提示（"凭据已保存""服务断了"…）。★ 为什么也要走 paintPill：
//   原来这些地方都是直接写 `$('state').textContent = ...`，而计时器一秒刷一次 ——
//   提示刚出现就被刷掉。人看到的是"什么也没发生"，而其实刚才真的发生了事。
//   ms=0 表示一直留着（服务断了这种不许自己消失）。
let _stateNote = '', _stateNoteUntil = 0;
function stateNote(text, ms) {
  _stateNote = String(text || '');
  _stateNoteUntil = ms ? Date.now() + ms : 0;
  paintPill();
  // ★ 到期必须自己清一次并重画（2026-10-26）：
  //   paintPill 里的"过期就丢"只在**有人调它**的时候才发生，而空闲时
  //   定时器不跑（它只在有秒表时刷）—— 于是那句提示会一直挂在那儿，
  //   直到下一次状态变化。那时它已经不是"刚才发生的事"了。
  if (ms) {
    setTimeout(() => {
      if (_stateNoteUntil && Date.now() >= _stateNoteUntil) {
        _stateNote = ''; _stateNoteUntil = 0;
        paintPill();
      }
    }, ms + 30);
  }
}
// 上一轮各跑了多久。★ 为什么要存：空闲时那个位置原来只写"空闲"，
// 和改动之前一模一样 —— 人看不出计时器到底生效了没有，也分不清
// "没生效"和"现在确实没在跑"。留一个上轮的脚印，两个问题一起解决。
let _lastSelfMs = 0, _lastBenniMs = 0;
function _mmss(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  // 短于 1 分钟只写秒：pill 空间有限，"0:07" 不如 "7s" 一眼能读
  return s < 60 ? s + 's'
                : Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's';
}
function runningBits() {
  const now = Date.now(), bits = [];
  if (_selfSince) bits.push('执行者 ' + _mmss(now - _selfSince));
  if (_benniSince) bits.push('测试者 ' + _mmss(now - _benniSince));
  return bits;
}
// ★★ 输入框旁那个小标签（在跑 / 等你回话 / 收尾中 / 空闲）。
//   名字必须和下面抽屉那个 paintState 分开（2026-10-26）：
//   原来两个都叫 paintState，而 JS 的函数声明是**后者覆盖前者** ——
//   于是"画状态标签"这一版一次都没执行过，屏幕上永远是 HTML 里写死的「空闲」，
//   计时器和「等你回话」全部失效。这就是用户那句「我并没有看到这个计时器改动生效」的真相。
//   ⇒ 本项目第五次踩「同名不同义」（前四次：text 字段、.body 类、step 变量…）。
//     新增函数前先 grep 一遍名字有没有被占。
function paintPill() {
  paintDuo();
  // ★ 当前动作条的可见性**只有这一个出口**（2026-10-28）：
  //   在跑 且 有内容 ⇒ 显示；其余（空闲 / 没动作 / 会话切走）⇒ 收起。
  const _dg2 = $('doing');
  if (_dg2) _dg2.hidden = !(running && _dg2.innerText.trim());
  const el = $('state');
  if (!el) return;
  // ★ 留一个"这一帧是按哪条规则画的"标记：状态标签有四个来源（持久提示 / 等人回话 /
  //   在跑 / 空闲），互相覆盖过一次（"等你回话"被计时器刷掉）。
  //   光看屏幕上的字分不清"没生效"和"生效了又被盖掉"——这个 dataset 能分。
  //   探针读它，人也能在 DevTools 里一眼看到。
  el.dataset.mode = 'idle';
  // ★★ 「等你回话」优先于一切（2026-10-26 修的真 bug）：
  //   showAsk 里原来是直接 `$('state').textContent = '等你回话'`，而 paintPill()
  //   每秒都会跑（有计时器时）—— 于是这句话**一秒后就被覆盖**成「收尾中 · 本我 1m」。
  //   人看到的永远是"它在干活"，而它其实正在等他。
  //   ⇒ 状态文案只有一个出口（paintPill）；"有没有挂着的提问"只有一个真源
  //     （_pendingAsk）—— 不再额外开一个 _asking 旗，两个变量迟早会漂。
  // ★ 持久提示（_stateNoteUntil=0，比如"服务断了"）排在最前：
  //   它说的是"这个世界出事了"，比"某个会话在等人"更需要先看见。
  if (_stateNote && !_stateNoteUntil) {
    el.textContent = _stateNote;
    el.className = 'pill warn';
    el.title = _stateNote;
    el.dataset.mode = 'note';
    return;
  }
  if (_pendingAsk) {
    const b0 = runningBits();
    el.textContent = '等你回话' + (b0.length ? ' · ' + b0.join(' ＋ ') : '');
    el.className = 'pill warn';
    el.title = '它停下来等问题了：不回答就不会往下走，也不会超时　'
             + String(_pendingAsk.question || '').slice(0, 120);
    el.dataset.mode = 'ask';
    return;
  }
  // 一次性提示（过期的自动丢掉）
  if (_stateNote) {
    if (_stateNoteUntil && Date.now() > _stateNoteUntil) {
      _stateNote = '';
    } else {
      el.textContent = _stateNote;
      el.className = 'pill warn';
      el.title = _stateNote;
      el.dataset.mode = 'note';
      return;
    }
  }
  const b = runningBits();
  // 自我已经停了、本我还在这 ⇒ 说的是"收尾中"，不是"空闲"。
  // ★ 这一句就是用户问的那个问题的答案：「收尾部分见不到本我，是他没话说还是跑完了」
  //   —— 现在界面上直接写着。
  let base;
  // ★ 进程还活着、但自我和本我都跑完了（状态已经是 success/paused），
  //   那段是在收尾：写索引、提炼记忆、落盘。实测有 12~15 秒。
  //   这时候还写「在跑」，人会盯着一个不动的标签等 —— 说「收尾中」才是实话。
  const _finishing = running && !_selfSince && !_benniSince
    && !!(_stat.status && _stat.status !== 'running');
  if (running) base = _stopping ? '暂停中' : (_finishing ? '收尾中' : '在跑');
  else if (b.length) base = '收尾中';
  else base = '空闲';
  // ★★ 秒表搬走了（2026-10-26 用户：「计时器可否放在 say 里面，这样更直观」）：
  //   底部这个标签只回答"现在是什么状态"，不再跳秒 ——
  //   秒表跟着**那一条回复**走（气泡右下角），以及本我那一行（"正在读… 12s"）。
  //   理由：底部一个跳动的数字，和"哪一段花了多久"没有对应关系；
  //   而回看历史时人要的正是后者。
  const tail = '';
  el.textContent = base + tail;
  el.className = 'pill' + (running ? ' warn' : '');
  el.dataset.mode = running ? 'run' : (b.length ? 'wrap' : 'idle');
  el.title = [
    _selfSince ? '执行者' + (_stat.model ? '（' + _stat.model + '）' : '') + ' 已经跑了 ' + _mmss(Date.now() - _selfSince) : '',
    _benniSince ? '测试者' + (_bModel ? '（' + _bModel + '）' : '') + ' 已经跑了 ' + _mmss(Date.now() - _benniSince) : '',
  ].filter(Boolean).join('　｜　');
}
// 每秒把"本我在读…"那行的时间补上。★ 它是**唯一**能证明"本我还活着"的东西：
// 本我要跑 1~3 分钟，界面一片安静的话，人只会得出"本我根本没跑"这个结论。
function tickBenni() {
  const el = document.querySelector('.bennirun .bsec');
  if (!el) return;
  el.textContent = _benniSince ? ' · ' + _mmss(Date.now() - _benniSince) : '';
}
setInterval(() => {
  if (_selfSince || _benniSince) {
    paintPill();
    // 自我的秒表现在挂在 say 气泡上（那条已经画完了，不用每秒重画）；
    // 本我还在跑 ⇒ 它那一行要一直跳，否则看不出它在动。
    if (_benniSince) tickBenni();
  }
}, 1000);

// ── 动作 ──
// ★ 这里只选一件事：**上下文窗口多大**（市面档位 300K / 600K / 1M）。
//   不选"干点活 / 长任务"那种抽象分类 —— 抽象描述没有可核的参照，
//   而且它背后藏着一堆我们自己造的限制（比如"要写够多少字"）。
//   活干到哪算完，由模型自己判断（二次询问）+ 契约决定，不由这里预设。
const depthOf = () => ({ ctx: Number($('depth').value) || 300000 });

// ── 斜杠命令补全（2026-10-26）──
// 输入 `/` 就弹候选，选一个就补成 `/name `，接着打参数。
// ★ 展开在**发送时**做、且由服务端做（`/api/commands/expand`）：
//   规则只有一份。前端再写一遍"占位符怎么替换"就是第二套规则，
//   改了忘一处，同一个命令两次发出去的内容就不一样。
let _cmds = null;         // 命令清单缓存（第一次拉，装/删命令后不刷新也无所谓，重启即生效）
let _cmdHit = [];
let _cmdPick = 0;
let _cmdOpen = false;

async function loadCommands(force) {
  if (_cmds && !force) return _cmds;
  const r = await api('/commands');
  _cmds = (r.body && r.body.commands) || [];
  return _cmds;
}

// 只有"以 / 开头且还没打空格"才在补全范围里。
// 打了空格之后 `/accept --slow` 是**参数**，不该再弹候选盖住它。
function _cmdToken() {
  const m = /^\/([^\s]*)$/.exec($('task').value || '');
  return m ? m[1] : null;
}

function cmdClose() {
  const b = $('cmdmenu');
  if (b) b.hidden = true;
  _cmdOpen = false;
  _cmdHit = [];
  _cmdPick = 0;
}

function cmdMark(i) {
  const items = $('cmdmenu').querySelectorAll('.cmditem');
  _cmdPick = Math.max(0, Math.min(items.length - 1, i));
  items.forEach((el, k) => el.classList.toggle('on', k === _cmdPick));
  // 键盘选到下面看不见的项时，列表要跟着滚 —— 否则"选了但看不到"
  const cur = items[_cmdPick];
  if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
}

async function refreshCmdMenu() {
  const tok = _cmdToken();
  if (tok === null) { cmdClose(); return; }
  const list = await loadCommands();
  const low = tok.toLowerCase();
  const hit = list.filter(c => c.name.toLowerCase().includes(low)
                            || String(c.desc || '').toLowerCase().includes(low))
                   .slice(0, 8);
  const box = $('cmdmenu');
  if (!hit.length) { cmdClose(); return; }
  _cmdHit = hit;
  _cmdPick = 0;
  _cmdOpen = true;
  box.innerHTML = hit.map((c, i) =>
    `<div class="cmditem${i === 0 ? ' on' : ''}" data-i="${i}">`
    + `<span class="cmdname">/${esc(c.name)}</span>`
    + `<span class="cmddesc">${esc(c.desc || '（没有说明）')}</span>`
    + (c.args ? `<span class="cmdargs">${esc(c.args)}</span>` : '')
    + `</div>`).join('');
  box.hidden = false;
  box.querySelectorAll('.cmditem').forEach(el => {
    // mousedown 而不是 click：click 之前 textarea 会先失焦，候选已经收起来了
    el.onmousedown = ev => { ev.preventDefault(); pickCmd(hit[Number(el.dataset.i)]); };
  });
}

function pickCmd(c) {
  if (!c) return;
  const t = $('task');
  t.value = '/' + c.name + ' ';
  cmdClose();
  t.focus();
  t.dispatchEvent(new Event('input'));   // 让"跟着内容长高"重算一次
}

async function start() {
  let t = $('task').value.trim();
  // ★ 静默 return 会让人以为"回车坏了"。要说一句，而且要说清现在是什么状态。
  if (!t) return;
  if (_starting) return;          // 连按两下回车不该建出两个会话
  // 正在暂停时不要再插一句（服务端还没收下断点）；正常运行中允许插话，
  // 后端会在当前执行步结束后接住它，不能在前端先把这条路堵死。
  if (running && _stopping) {
    showErr('正在暂停，停完再发 —— 现在发会把这一轮的话排到它后面，容易乱');
    return;
  }
  const targetSid = cur, targetProject = curProj, targetVersion = _viewVersion;
  const sentInput = $('task').value;
  const sentImages = _imgs.slice();
  const sentOptions = depthOf();
  const wasRunning = running;
  _starting = true;
  $('go').disabled = true;
  try {
    // ★ 斜杠命令：`/name 参数` 展开成正文再发。
    //   输入框里打的是三个字，实际发出去的是几百字 —— **必须说一句**，
    //   否则人以为只发了三个字，看到它去跑验收会莫名其妙。
    const _cm = /^\/([^\s/]+)(?:\s+([\s\S]*))?$/.exec(t);
    if (_cm) {
      try {
        const r = await api('/commands/expand', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: _cm[1], args: _cm[2] || '' }),
        });
        if (r.status === 200 && r.body && r.body.text) {
          t = r.body.text;
          if (cur === targetSid && _viewVersion === targetVersion)
            stateNote('命令 /' + _cm[1] + ' 已展开（' + t.length + ' 字）', 5000);
        }
      } catch (e) {
        // 展开失败就按原文发 —— 发出去一句 "/xxx" 至少能看懂是命令没生效，
        // 比"点了开始什么也没发生"好
      }
    }
    // ★ 有当前会话就「接着说」，没有才「开新的」。
    //   这一条是「会话」这个词的含义 —— 之前每次输入都新开一个目录，
    //   上一句的上下文全丢，那根本不叫会话。
    const cont = !!targetSid;
    // ★★ 「莫名其妙多出一个会话」要能自己解释清楚。
    //   实测过的成因：那一刻 cur 是空的（界面刚打开/刚清屏/会话被删），
    //   于是这一次输入走了"新建"而不是"接着说"。
    //   这行不是装饰 —— 它是发生时要留的证据，否则事后只能靠猜。
    if (!cont) {
      console.warn('[harness] 新一轮开在新会话里：cur 是空的');
    }
    const { status, body } = cont
      ? await api('/sessions/' + encodeURIComponent(targetSid) + (wasRunning ? '/interrupt' : '/say'), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: t, ...sentOptions }),
        })
      : await api('/sessions', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          // 新会话落在选中的项目文件夹里。空 = 服务端默认（仓库根）。
          body: JSON.stringify({ task: t, cwd: targetProject || undefined, ...sentOptions }),
        });
    // ★★ 运行中说的话 = **步边界插话**（2026-10-31 用户明确的最小粒度）：
    //   当前执行步（模型调用 + 它要的工具）完整结束后，在下一执行步开始前接。
    //   不等整个任务循环结束，也不截断当前执行步；等待不限时。
    if (cur !== targetSid || _viewVersion !== targetVersion) return;
    if (cont && wasRunning && status === 200) {
      stateNote(body && body.queued
        ? '已收到插话：当前执行步完成后继续，不限时'
        : '当前执行步已完成，正在重新收揽上下文', 7000);
    }
    if (status !== 200) {
      showErr((cont ? '没能接着说：' : '没能开始：') + (body.error || ('状态 ' + status)));
      return;
    }
    // ★ 这一轮还在收尾，服务端把它**排队**了（2026-10-28）。
    //   原来这种情况服务端直接 409，界面上只看到"没能接着说：这个会话还在跑"，
    //   而人刚点完「允许」或者刚发完一句 —— 看起来就是"它不动了"。
    if (body && body.queued) {
      clearErr();
      if (_imgs.length) { _imgs = _imgs.filter(img => !sentImages.includes(img)); _renderChips(); }
      if(window.harnessCompatDraftSent) window.harnessCompatDraftSent(targetSid,sentInput);
      if ($('task').value === sentInput) $('task').value = '';
      const fq = $('feed');
      if (fq) fq.insertAdjacentHTML('beforeend',
        '<div class="line">' + esc(body.note || '这一轮还在收尾，收完会接着跑你刚说的这句') + '</div>');
      paintStatusBar();
      await loadSessions();
      return;
    }
    clearErr();
    // 图已经随这次请求交给服务端落盘（runs/<会话>/img-N.ext），界面不再挂着
    if (_imgs.length) { _imgs = _imgs.filter(img => !sentImages.includes(img)); _renderChips(); }
    if(window.harnessCompatDraftSent) window.harnessCompatDraftSent(targetSid,sentInput);
    if ($('task').value === sentInput) $('task').value = '';
    if (!cont) {
      // 新会话：清空视图；续聊：保留已渲染的部分，只接着追加（seen 不清零）
      if(window.harnessCompatContextChange) window.harnessCompatContextChange(body.session);
      cur = body.session; seen = 0; lastLog = '';
      _stat = { model: '', tokens: 0 };
      clearFeed();
      // ★ 说清"为什么开了新会话"。这一行只在真的新建时出现 ——
      //   它是"莫名其妙多出一个会话"这个疑问的当场答案，事后也能对着看。
      const f0 = $('feed');
      if (f0) f0.insertAdjacentHTML('beforeend',
        '<div class="line">新会话（这一刻没有选中任何会话，所以这一句开了一个新的）</div>');
      $('logbody').textContent = '';
      paintStatusBar();
    }
    setRunning(true);
    const acceptedVersion = _viewVersion;
    await loadSessions();
    if (_viewVersion !== acceptedVersion) return;
    // ★ 刚发完话，视口必须钉到底。
    //   原来不滚：如果这个会话很长（实测有 6 万字符历史），
    //   新内容长在底部，而人停在顶部 —— 于是**整个生成过程他都看不见**，
    //   等跑完自己滚下去，看到的是已经成型的整段，观感就是"思考完才跳出来"。
    scrollFeedToBottom();
    poll();
  } catch (e) {
    showErr('启动失败：' + (e && e.message ? e.message : e));
  } finally {
    _starting = false;
    if (!running) $('go').disabled = false;
  }
}

// 开一个新会话：不删旧的，只是不再往它上面接
async function newSession() {
  const viewVersion = _viewVersion;
  if (typeof jobLiveStop === 'function') jobLiveStop();
  // ★★ 点「新对话」必须**立刻多出一条会话**。
  //   原来的做法是只清空视图、等他说第一句话才真的建目录 ——
  //   于是他点了没反应，以为按钮坏了（原话：「我点击新对话并不会新产生一个对话窗口」）。
  //   WorkBuddy 那种做法就是"点了就有一条新的"，这里照它：先建目录（blank，不启动进程），
  //   再把这个空会话选中。第一句话进 /say 时，服务端发现没有存档，就当它的第一轮任务跑。
  //
  // ★ 不再因为 running 就拒绝 —— 界面卡住的时候，人必须有一条出路。
  //   正在跑的那一轮不受影响（任务在独立子进程里），只是界面不再跟着它。
  if (!curProj && !cur) { /* 没有项目也能建，落在仓库根 */ }
  const r = await api('/sessions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ blank: true, cwd: curProj || undefined }),
  });
  if (r.status !== 200) {
    showErr('没能新建会话：' + ((r.body && r.body.error) || ('HTTP ' + r.status)));
    return;
  }
  if (_viewVersion !== viewVersion) { await loadSessions(); return; }
  if(window.harnessCompatContextChange) window.harnessCompatContextChange(r.body.session);
  cur = r.body.session;
  _stopping = false;
  seen = 0; lastLog = '';
  _booting = false;             // 新会话 ⇒ 事件都是刚发生的，该弹的就要弹
  _stat = { model: '', tokens: 0 };
  _lastSay = '';
  setRunning(false);
  dropAsk();                    // 旧会话的提问不许跟着跑到新会话里
  clearFeed('<div class="empty">新会话。说一句话就开始。</div>');
  // ★ 不能写 $('thinking').textContent = '' —— 那会把它的**子元素一起删掉**
  //   （#thead 标题和 #tbody 正文），删完 getElementById('tbody') 就是 null，
  //   打字机从此没有画布：界面表现为"正在思考"块一直空着，而且不报错。
  resetThink();
  $('logbody').textContent = '';
  // ★ 判空（2026-10-26 静态核对时逮到）：`#logwrap` 这个元素**在 HTML 里不存在**
  //   （CSS 里还留着 `.logwrap`，说明以前是 `<details>`，后来改成右侧进度抽屉了）。
  //   不判空的话这一行会抛 TypeError，而它在 `newChat()` 的**中间**——
  //   于是后面那句「新会话」提示、清错、刷新会话列表、聚焦输入框**全都不执行**，
  //   表现是"点了新建没反应"，而且控制台才看得见。引用了不存在的东西，必须显式判空。
  if ($('logwrap')) $('logwrap').open = false;
  stateNote('新会话', 4000);
  paintStatusBar();
  clearErr();
  await loadSessions();
  const t = $('task');
  if (t) t.focus();
}

async function stop() {
  if (!cur || _stopping) return;
  const stopSid = cur, stopVersion = _viewVersion;
  // ★ 先改界面，再发请求：请求本身要几十毫秒，而人需要一个"收到了"的确认。
  //   改完再发是因为——如果发请求先失败，界面会停在"正在暂停"上骗人，所以
  //   失败时要退回去（见下面 r.status 判断）。
  _stopping = true;
  setRunning(true);
  const r = await api('/sessions/' + encodeURIComponent(stopSid) + '/stop', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ why: '界面点了暂停' }),
  });
  if (cur !== stopSid || _viewVersion !== stopVersion) return;
  if (r.status !== 200) {
    _stopping = false;
    setRunning(running);
    showErr('暂停请求没送到：' + (r.body && r.body.error || ('HTTP ' + r.status)));
    return;
  }
  clearErr();
  stateNote((r.body && r.body.note) || '将在当前执行步完成后暂停', 5000);
}

// ── 工具 ──
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmt(n) {
  n = Number(n || 0);
  return n < 1000 ? String(n) : (n / 1000).toFixed(1) + 'k';
}

// 服务挂了要让页面说人话，而不是让人对着转圈的界面猜
window.__serverDown = code => {
  // 0 = 不自动消失。服务挂了这件事不许被任何提示顶掉。
  stateNote('服务断了（code ' + code + '）', 0);
  setRunning(false);
  showErr('本地服务退出了（退出码 ' + code + '）。关掉窗口重开；'
          + '如果反复这样，看 desktop/python-path.txt 指向的解释器对不对。');
};

// ── 模型档案（2026-10-26）──
// ★ 这一层只做两件事：把服务端的档案画到输入框上方、把人的选择发回去。
//   key 永远只经手掩码 —— 界面上没有任何地方能读到明文。
// ★ 为什么不做"输入框里直接改模型名"：模型名和 key、端点是一套东西。
//   让人只改一处，另一处还指着旧端点，就是"配了不生效"的经典形态。
let _models = null;

const _roleCn = { main: '执行者', review: '测试者', plan: '规划者' };

function _pLabel(p) {
  // ★★ 下拉要回答的是「**跑在哪个模型上**」，不是「你给这套凭据起了什么名字」。
  //   原来优先显示档案名，而导入时生成的名字带角色（自我（x）/ 本我（y））——
  //   一点 ⇄ 互换，界面就成了「自我 本我（glm-5.3-flash）」，看着像开关坏了，
  //   其实是换成功了（用户报的「模型切换按钮依旧有问题」就是这个）。
  //   角色由下拉的位置表达，名字只说这套**是什么**：模型名优先。
  const m = String(p.model || '').trim();
  const nm = String(p.name || '').trim();
  if (!m) return nm || '(没名字)';
  // 名字里带「自我/本我」的不要 —— 那是角色，不是这套凭据的属性
  if (!nm || nm === m || /执行者|测试者|规划者/.test(nm)) return m;
  return m + ' · ' + nm;
}

function paintModels(st) {
  if (st) _models = st;
  if (!_models) return;
  const list = _models.profiles || [];
  const fill = (sel, role) => {
    if (!sel) return;
    const cur = String(_models[role] || '');
    // ★ 这一项原来只写「用旧凭据文件 / 环境变量」—— 人看不出自己到底跑在哪个模型上，
    //   而"我现在用的是谁"正是这一行要回答的事。把生效的模型名写在选项里。
    const eff = ((_models.effective || {})[role]) || {};
    const base0 = eff.model ? ('凭据文件 · ' + eff.model) : '（用旧凭据文件 / 环境变量）';
    sel.innerHTML = '<option value="">' + esc(base0) + '</option>'
      + list.map(p => '<option value="' + esc(p.id) + '">' + esc(_pLabel(p))
        + '</option>').join('');
    // ★ 选中的那份可能已经被删了。这时候**不能**留一个看不见的选择（浏览器会
    //   默默跳到第一项，而服务端还在用旧 id）—— 退回空项，并在提示条上说清。
    sel.value = list.some(p => p.id === cur) ? cur : '';
  };
  fill($('mself'), 'main');
  fill($('mreview'), 'review');
  fill($('mplan'), 'plan');
  // 收起状态下那个按钮要回答「这一轮谁在跑」—— 所以写**实际生效**的模型名，
  // 不写档案名（档案名是给人区分"哪套 key"的，回答不了"用谁"）。
  const _msn = $('msetname');
  if (_msn) {
    const _em = ((_models.effective || {}).main || {}).model || '未配';
    _msn.textContent = _em;
    _msn.title = _em;
  }

  const warns = [];
  ['main', 'review', 'plan'].forEach(r => {
    const e = (_models.effective || {})[r] || {};
    if ((e.overridden || []).length) {
      warns.push(_roleCn[r] + ' 被环境变量盖住：' + e.overridden.join(' / '));
    }
  });
  // ★ 这条原来只看「档案列表空不空」（2026-10-27 修）：用 credentials.env 的人
  //   档案列表本来就是空的，于是明明能跑，界面上却一直挂着「还没存过 key」。
  //   判据换成"这个角色到底有没有模型可用"—— 那才是"能不能跑"。
  const _lack = ['main', 'review', 'plan'].filter(r => {
    const e = (_models.effective || {})[r] || {};
    return !e.model;
  });
  if (_lack.length) {
    warns.push(_roleCn[_lack[0]] + ' 还没有可用的凭据：点「档案」加一份，'
      + '或写进 credentials.env');
  }
  if (!list.length && !_lack.length) {
    warns.push('现在用 credentials.env 那套；想换/加一份 key 就点「档案」');
  }
  const w = $('mwarn');
  if (w) {
    w.hidden = !warns.length;
    w.textContent = warns.join('；');
    w.title = warns.join('\n');
  }
}

async function loadModels() {
  const r = await api('/models');
  if (r.status !== 200) { return null; }
  paintModels(r.body || {});
  return r.body;
}

// ── 上下文用量（2026-10-27）────────────────────────────────────────
// 数据是**发送那一刻**记下的快照（`runs/<会话>/context.json`），这里只渲染。
// ★ 六个来源的颜色只定义一次：按钮、条形图、列表三处必须同色，
//   否则人会以为条形图里那块橙和列表里的橙是两个东西。
const CTX_COLOR = {
  system: '#6366f1', memory: '#10b981', tools: '#f59e0b',
  mcp: '#8b5cf6', skills: '#ec4899', messages: '#94a3b8'
};
let _ctx = null;

function _fmtK(n) {
  n = Number(n || 0);
  if (n >= 1000000) { return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M'; }
  if (n >= 1000) { return Math.round(n / 1000) + 'K'; }
  return String(n);
}

/* 圆环：周长 2πr（r=8 → 50.27）。用 stroke-dashoffset 画"用了多少"，
   剩下的就是底圈的灰白 —— 和面板里那条横条是同一套语言。 */
const RING_C = 2 * Math.PI * 8;

function paintRing(pct) {
  const ring = $('ctxring');
  const btn = $('ctxbtn');
  const real = Math.max(0, Math.min(100, Number(pct) || 0));
  // ★ 画的时候给一个下限（2026-10-27 看截图才发现的）：2.8% 在 26px 的环上只有
  //   1.4px 弧，看起来就是"空圈"，而它并不等于 0。下限 6%（约 3px）——
  //   这是**唯一一处故意画得不等于真值**的地方，所以准确数字写进提示与无障碍文本。
  const p = real > 0 ? Math.max(6, real) : 0;
  if (ring) {
    ring.style.strokeDasharray = RING_C.toFixed(2);
    ring.style.strokeDashoffset = (RING_C * (1 - p / 100)).toFixed(2);
  }
  // 过 60% 转琥珀色：那时候压缩快触发了（和原来那个黄边一个意思）
  if (btn) {
    btn.classList.toggle('warn', real >= 60);
    btn.title = real
      ? '上下文已用 ' + real.toFixed(1) + '%：点开看这一轮发出去的内容按来源拆开'
      : '上下文用量：点开看这一轮发出去的内容按来源拆开';
  }
}

async function refreshCtx() {
  const btn = $('ctxbtn');
  if (!btn) { return null; }
  const q = cur ? ('?session=' + encodeURIComponent(cur)) : '';
  const r = await api('/context' + q);
  const d = (r && r.body) || {};
  _ctx = d;
  const lab = $('ctxpct');
  if (d.ok) {
    lab.textContent = (Number(d.pct) || 0).toFixed(1) + '%';
    paintRing(d.pct);
    btn.title = '这一轮发出去 ' + d.used_tokens + ' / ' + d.budget_tokens
      + ' token（按字符估）—— 点开看花在哪';
    // 过 60% 就转黄：那时候压缩快触发了，人该知道
    btn.classList.toggle('warn', (Number(d.pct) || 0) >= 60);
  } else {
    lab.textContent = '—';
    btn.title = d.why || '还没有上下文可看';
    paintRing(0);
  }
  return d;
}

function renderCtx() {
  const d = _ctx || {};
  const box = $('ctxlist'), bar = $('ctxbar');
  if (!box || !bar) { return; }
  if (!d.ok) {
    $('ctxnum').textContent = '—';
    $('ctxcap').textContent = '';
    bar.innerHTML = '';
    box.innerHTML = '';
    $('ctxnote').textContent = d.why || '还没有上下文可看。发一句话之后再看。';
    syncLimit();
    return;
  }
  const used = Number(d.used_tokens) || 0;
  // ★ 分母用**当前选的**上限，不用快照里那一次的（2026-10-27 自测逮到）：
  //   面板上是「已使用 5.6K/ 150.0K」而拉条写「上限 300K」—— 同一块面板里两个上限，
  //   人不知道该信哪个。快照记的是"那一次按多少跑的"，拉条是"现在按多少跑"，
  //   两者不一样时在脚注里说明，正文统一按现在这个算。
  const selCap = Number((($('depth') || {}).value) || 0);
  const snapCap = Number(d.budget_tokens) || 0;
  const cap = selCap || snapCap;
  const pct = cap ? Math.round(used * 1000 / cap) / 10 : 0;
  $('ctxnum').textContent = pct.toFixed(1) + '%';
  const _lab = $('ctxpct');
  if (_lab) { _lab.textContent = pct.toFixed(1) + '%'; }
  paintRing(pct);
  // ★ 和 WorkBuddy 一样的写法（2026-10-27）：「已使用 353.2K/ 1000.0K」——
  //   一个数说"吃了多少"，一个数说"能吃多少"，都不省略。
  $('ctxcap').textContent = '已使用 ' + _k1(used) + '/ ' + _k1(cap);

  const by = {};
  (d.segs || []).forEach(x => { by[x.k] = x; });
  const tk = k => (by[k] ? by[k].tokens : 0);
  // ★ 五项，和 WorkBuddy 的行名一字不差（用户：「我想要的是 workbuddy 一模一样的形式」）。
  //   记忆在物理上是系统提示词的一部分，所以并进第一行，再用子行写出它自己占多少。
  // ★ 每行的数按**占窗口**算（2026-10-27 对齐 WorkBuddy）：五行的数加起来正好等于
  //   头顶那个百分比。按"占已用"算的话加起来是 100%，而头顶写 35.3% ——
  //   两个数永远对不上，人会以为其中一个是错的。
  const rows = [
    { cn: '系统提示词', c: '#6366f1', t: tk('system') + tk('memory'),
      sub: tk('memory') ? '其中记忆 ' + _pctOf(tk('memory'), cap) + '%' : '' },
    { cn: '工具及子智能体', c: '#10b981', t: tk('tools') },
    { cn: '对话消息', c: '#f59e0b', t: tk('messages') },
    { cn: '连接器及MCP', c: '#8b5cf6', t: tk('mcp') },
    { cn: '技能', c: '#ec4899', t: tk('skills') }
  ];
  // ★ 条画的是**窗口**：每一段的宽度 = 它占窗口多少，剩下的灰底就是没用到的部分。
  //   原来按"占已用"铺满，条永远是满的 —— 那样看不出还剩多少，等于没画。
  bar.innerHTML = rows.filter(r => r.t > 0).map(r =>
    '<i style="width:' + (cap ? Math.max(0.4, r.t * 100 / cap) : 0) + '%;background:'
    + r.c + '" title="' + esc(r.cn) + ' ' + _k1(r.t) + '"></i>').join('');

  box.innerHTML = rows.map(r => '<div class="ctxrow">'
    + '<span class="ctxdot" style="background:' + r.c + '"></span>'
    + '<span class="ctxnm">' + esc(r.cn)
    + (r.sub ? '<span class="ctxsub">' + esc(r.sub) + '</span>' : '') + '</span>'
    + '<span class="ctxvl">' + _pctOf(r.t, cap) + '%</span></div>').join('');

  $('ctxnote').textContent = '按字符估，不是接口回来的用量'
    + (d.turn ? ' · 第 ' + d.turn + ' 轮' : '')
    + (snapCap && cap && snapCap !== cap ? '（那一轮按 ' + _k1(snapCap) + ' 跑的）' : '');
  syncLimit();
}

/* 某一段占**窗口**的百分比。分母没有时（还没跑过）退回 0，不瞎猜。 */
function _pctOf(t, cap) {
  const v = cap ? t * 100 / cap : 0;
  // ★ 一位小数就够了；真有比 0.05% 还小的，写「<0.1」而不是「0」——
  //   写成 0 等于说"它没占"，而它占了（2026-10-27）。
  if (!v) { return '0'; }
  return v < 0.05 ? '<0.1' : v.toFixed(1);
}

/* 已用/上限的数字：和 WorkBuddy 一样写成 353.2K / 1000.0K */
function _k1(n) {
  n = Number(n) || 0;
  if (n >= 1000000) { return (n / 1000000).toFixed(1) + 'M'; }
  return (n / 1000).toFixed(1) + 'K';
}

/* 上限三档。和输入框下面原来那个下拉是同一个值（#depth），
   只是现在只有面板这一处能改 —— 一个数不该有两个入口。 */
const LIMITS = [
  { v: 300000, t: '300K 上下文 · 标准' },
  { v: 600000, t: '600K 上下文 · 长' },
  { v: 1000000, t: '1M 上下文 · 超长' }
];

/* ★ 上限要**记住**（2026-10-31 盘点：这一项原来只读 DOM，刷新就回 300K）。
   存 localStorage，与外观/显示偏好同层：这是"你这台机器上的工作习惯"，
   不是任务数据，不进后端、不进会话。 */
const DEPTH_KEY = 'harness.depth.v1';

function _restoreLimit() {
  const saved = Number(localStorage.getItem(DEPTH_KEY) || 0);
  if (!saved || !LIMITS.some(x => x.v === saved)) return;
  const sel = $('depth');
  if (sel) sel.value = String(saved);
}
// 脚本在 body 末尾加载，#depth 已经在 DOM 里；直接恢复一次。
_restoreLimit();

function _limitIdx() {
  const sel = $('depth');
  const v = Number(sel && sel.value) || 300000;
  const i = LIMITS.findIndex(x => x.v === v);
  return i < 0 ? 0 : i;
}

function syncLimit() {
  const hit = $('limit_hit'), fill = $('limit_fill'), knob = $('limit_knob');
  const t = $('limit_t');
  if (!hit || !fill || !knob) { return; }
  const i = _limitIdx();
  hit.value = String(i);
  const at = i * 50;                       // 0% / 50% / 100%
  fill.style.width = at + '%';
  knob.style.left = at + '%';
  if (t) {
    t.innerHTML = '上限 <b>' + esc(LIMITS[i].t) + '</b>';
  }
}

/* 拖了拉条就改上限：写进 #depth（发送时读的就是它），再让面板上的百分比跟着重算 */
function setLimit(i) {
  const sel = $('depth');
  const k = LIMITS[Math.max(0, Math.min(LIMITS.length - 1, Number(i) || 0))];
  if (sel) { sel.value = String(k.v); }
  try { localStorage.setItem(DEPTH_KEY, String(k.v)); } catch (e) {}
  // 本地重算一遍百分比：不等下一次发送，拖完立刻能看见分母变了
  if (_ctx && _ctx.ok) {
    const used = Number(_ctx.used_tokens) || 0;
    _ctx.budget_tokens = k.v;
    _ctx.pct = k.v ? Math.round(used * 1000 / k.v) / 10 : 0;
  }
  renderCtx();
  const lab = $('ctxpct');
  if (lab && _ctx && _ctx.ok) { lab.textContent = (Number(_ctx.pct) || 0).toFixed(1) + '%'; }
}

async function pickModel(role, id) {
  const r = await api('/models', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ act: 'select', role: role, id: id }),
  });
  const b = r.body || {};
  if (r.status !== 200 || !b.ok) {
    showErr('没切过去：' + (b.error || r.status));
    loadModels();                 // 拉回来，别让界面停在错的状态
    return;
  }
  clearErr();
  paintModels(b.state);
  stateNote(b.note || '切好了（下一次任务生效）', 5000);
}

async function swapModels() {
  // ★ 这里原来有一条前端短路：`if (!_models.main && !_models.review) 弹一句话就返回`。
  //   它和后端 `models.swap()` 的判断**重复**，而且文案不一样 ——
  //   两处判断同一件事，迟早一边改了另一边没改（这次就是：后端已经能解释"为什么换不了、
  //   下一步做什么"，前端却先拦下来，只丢一句「没什么可换的」）。
  //   ⇒ 判断只留一处（后端，它知道档案、也知道回落顺序），前端只负责显示。
  const r = await api('/models', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ act: 'swap' }),
  });
  const b = r.body || {};
  if (r.status !== 200 || !b.ok) {
    // ★ 失败要把"为什么"和"下一步"一起说（2026-10-27）：只报「没法换」，
    //   人会以为是按钮坏了；实际是"两边都还在用凭据文件，没有档案可换"。
    //   而且这条原来**根本走不到** —— 后端在空手互换时返回了 ok=True。
    showErr('没法换：' + (b.error || ('HTTP ' + r.status))
      + (b.hint ? '。' + b.hint : ''));
    return;
  }
  clearErr();
  paintModels(b.state);
  const e = (b.state || {}).effective || {};
  stateNote('换过来了 —— 执行者用 ' + ((e.main || {}).model || '?')
    + '，测试者用 ' + ((e.review || {}).model || '?') + '（下一次任务生效）', 7000);
}

// ── 档案管理 ──
function _clearEdit() {
  $('c_id').value = '';
  $('c_name').value = ''; $('c_base').value = '';
  $('c_model').value = ''; $('c_key').value = '';
  $('c_key').placeholder = 'sk-…';
  $('cfgedit_ttl').textContent = '新增一份';
}

function paintProfiles(st) {
  if (st) _models = st;
  if (!_models) return;
  const list = _models.profiles || [];
  $('cfgpath').textContent = _models.path || 'models.json';
  const box = $('plist');
  if (!list.length) {
    box.innerHTML = '<div class="sempty">还没有档案。下面填一份，'
      + '或者点「导入当前配置」把已经能用的那套存下来。</div>';
    return;
  }
  box.innerHTML = list.map(p => {
    const onM = p.id === _models.main, onR = p.id === _models.review;
    // ★ 规划者也能选模型（2026-10-30 用户：「没法给规划者配模型」）。
    //   这里原来只有"自我/本我"两个按钮，第三个角色根本不在界面上。
    //   措辞一并改：自我→执行者、本我→测试者（这套名字早就不用了）。
    const onP = p.id === _models.plan;
    return '<div class="prow' + ((onM || onR || onP) ? ' on' : '') + '">'
      + '<span class="pn" title="' + esc(p.name) + '">' + esc(p.name) + '</span>'
      + '<span class="pm">' + esc(p.model) + '</span>'
      + '<span class="pk" title="key 只显示掩码">' + esc(p.key_masked || '无 key') + '</span>'
      + '<span class="psp"></span>'
      + '<button class="pbtn' + (onM ? ' on' : '') + '" data-puse="main" '
      + 'data-pid="' + esc(p.id) + '">' + (onM ? '执行者 ✓' : '用于执行者') + '</button>'
      + '<button class="pbtn' + (onR ? ' on' : '') + '" data-puse="review" '
      + 'data-pid="' + esc(p.id) + '">' + (onR ? '测试者 ✓' : '用于测试者') + '</button>'
      + '<button class="pbtn' + (onP ? ' on' : '') + '" data-puse="plan" '
      + 'data-pid="' + esc(p.id) + '">' + (onP ? '规划者 ✓' : '用于规划者') + '</button>'
      + '<button class="pbtn" data-pedit="' + esc(p.id) + '">改</button>'
      + '<button class="pbtn" data-pdel="' + esc(p.id) + '">删</button>'
      + '</div>';
  }).join('');

  box.querySelectorAll('[data-puse]').forEach(b => {
    b.onclick = async () => {
      await pickModel(b.dataset.puse, b.dataset.pid);
      paintProfiles();
    };
  });
  box.querySelectorAll('[data-pedit]').forEach(b => {
    b.onclick = () => {
      const p = list.find(x => x.id === b.dataset.pedit);
      if (!p) return;
      $('c_id').value = p.id;
      $('c_name').value = p.name || '';
      $('c_base').value = p.base_url || '';
      $('c_model').value = p.model || '';
      $('c_key').value = '';
      $('c_key').placeholder = p.has_key
        ? ('已配 ' + p.key_masked + ' ｜ 留空 = 不改') : '没配 key';
      $('cfgedit_ttl').textContent = '改：' + (p.name || p.model);
      $('c_name').focus();
    };
  });
  box.querySelectorAll('[data-pdel]').forEach(b => {
    b.onclick = async () => {
      const p = list.find(x => x.id === b.dataset.pdel);
      if (!p) return;
      if (!confirm('删掉这份档案？\n\n' + (p.name || '') + '（' + (p.model || '') + '）\n\n'
        + '正在用它的话，那个角色会变成空着。')) return;
      b.disabled = true;
      const r = await api('/models', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ act: 'del', id: p.id }),
      });
      const rb = r.body || {};
      if (r.status !== 200 || !rb.ok) {
        b.disabled = false;
        showErr('没删成：' + (rb.error || r.status));
        return;
      }
      _clearEdit();
      paintProfiles(rb.state);
      paintModels(rb.state);
      stateNote(rb.note || '删了', 5000);
    };
  });
}

async function openCfg() {
  const st = await loadModels();
  if (!st) { showErr('读不到模型档案'); return; }
  _clearEdit();
  paintProfiles(st);
  $('cfgdialog').showModal();
}

async function saveCfg() {
  const body = {
    act: 'save',
    id: $('c_id').value.trim(),
    name: $('c_name').value.trim(),
    base_url: $('c_base').value.trim(),
    api_key: $('c_key').value.trim(),
    model: $('c_model').value.trim(),
  };
  const r = await api('/models', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const b = r.body || {};
  if (r.status !== 200 || !b.ok) { showErr('没保存：' + (b.error || r.status)); return; }
  clearErr();
  _clearEdit();
  paintProfiles(b.state);
  paintModels(b.state);
  stateNote((b.note || '存好了') + '（下一次任务生效）', 6000);
}

async function importCfg() {
  const r = await api('/models', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ act: 'import_env' }),
  });
  const b = r.body || {};
  if (r.status !== 200 || !b.ok) { showErr('没导成：' + (b.error || r.status)); return; }
  clearErr();
  paintProfiles(b.state);
  paintModels(b.state);
  stateNote(b.note || '导入了', 6000);
}

// 代码块的复制按钮：委托到 feed 上 —— 代码块是动态插入的，逐个绑会漏
// 「它在问你」的回答框：点一下就把这句话续上去。
// ── 需要人回答的时候：弹窗 ──
// ★★ 用户两条原话：
//   ①「需要人的环节应该弹窗等人回答之后无休止地停止，而不是超时之后自己往下继续」
//   ②「该设计成弹窗的部分就用弹窗，不要全部依赖输入框」
//   所以这里**没有任何定时器**：不开超时、不自动关、不自动继续。
//   关掉弹窗只是把问题收起来（_pendingAsk 还在），问题不会因此消失、也不会被跳过。
// ★ _pendingAsk 的声明已经上移到计时器那一节：paintPill 要读它，
//   而同一个作用域里 `let` 声明两次是 SyntaxError（整页脚本直接不执行）。

// 把这个挂着的提问收掉（**不发答案**）。切会话 / 开新会话时用。
// ★ 问题不会被丢掉：它还在那个会话的事件流里，下次打开照旧弹（status=needs_human）。
function dropAsk() {
  _pendingAsk = null;
  _askFor = '';
  const d = $('askdlg');
  if (d && d.open) d.close();
  paintPill();
}

function showAsk(e) {
  _pendingAsk = e || {};
  // ★ 记下"这个提问属于哪个会话"（_askFor 是模块级，见下方声明）。
  //   发送时拿它对 cur 核一遍 —— 弹窗是页面上唯一一个跨会话存活的东西，
  //   不核对就可能把答案发到别人那儿。
  _askFor = cur;
  const interview = _pendingAsk.source === 'interview';
  $('askttl').textContent = interview ? '开工前先问清楚' : '它在问你';
  const q = String(_pendingAsk.question || '');
  // 多个问题（采访是"；"拼起来的）拆成有序列表 —— 一坨里读不出有几问
  const parts = q.split(/[；;]\s*/).map(s => s.trim()).filter(Boolean);
  $('askqs').innerHTML = parts.length > 1
    ? '<ol>' + parts.map(s => `<li>${esc(s)}</li>`).join('') + '</ol>'
    : esc(q);

  // ★ 渲染候选按钮（2026-10-25）。模型给的 options 必须是 2~3 条走法不同的选项，
  //   界面上按钮和自定义输入都能用，选哪个都等于一句话回答。
  // ★ 读参数 `e`、不读全局 `_pendingAsk`（2026-10-26）：两者此刻是同一个对象，
  //   但"同一个东西有两个来源"就是这个项目最贵的坑 —— 换个调用顺序就会取到上一次的问题。
  const opts = Array.isArray(e && e.options) ? e.options : [];
  const optsEl = $('askopts');
  if (opts.length) {
    optsEl.innerHTML = opts.map((t, i) => {
      const s = String(t || '').trim();
      return s ? `<button data-idx="${i}">${esc(s)}</button>` : '';
    }).join('');
    // 点一个候选 = 把它的文字填进输入框再立刻发送
    optsEl.querySelectorAll('button').forEach(btn => {
      btn.onclick = () => {
        const ta = $('askin');
        ta.value = btn.textContent || '';
        // 直接发送（不用再点下面那个"回答并继续"）
        sendAskAnswer();
      };
    });
    $('askorcust').textContent = '或者自己输入：';
  } else {
    optsEl.innerHTML = '';
    $('askorcust').textContent = '';
  }

  const ta = $('askin');
  ta.value = '';
  ta.disabled = false;
  const btn = $('askgo');
  btn.disabled = false;
  btn.textContent = '回答并继续';
  const d = $('askdlg');
  // ★ show() 而不是 showModal()：模态层会吞滚轮、锁焦点、挡住页面点击，
  //   而"边看上下文边决定"正需要这三样都能用（见 .askdlg 的注释）。
  if (!d.open) d.show();
  setTimeout(() => ta.focus(), 30);
  // 状态标签跟着变：不然人看着"空闲"以为它在偷懒，其实是在等他。
  // ★ 只调 paintPill，不直接写 #state —— 直接写的那一句会在 1 秒后被覆盖。
  paintPill();
}

async function sendAskAnswer() {
  if (!cur) return;
  // ★ 答非所问的护栏：这条提问是给另一个会话的，就不许发。
  //   正常路径上 dropAsk 已经在切会话时清掉了，这里再核一遍是防"漏掉某个入口"——
  //   这类错一旦发生就是"答案发给错的会话、那边莫名其妙接着干"，事后极难看出。
  if (_askFor && _askFor !== cur) {
    showErr('这个提问属于另一个会话（' + _askFor + '）。切回去再答，'
            + '否则答案会发到当前这个会话上。');
    return;
  }
  const ta = $('askin');
  const text = (ta.value || '').trim();
  if (!text) { ta.focus(); return; }
  const btn = $('askgo');
  btn.disabled = true;
  btn.textContent = '发送中…';
  const r = await api('/sessions/' + encodeURIComponent(cur) + '/say', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, ...(_imgs.length ? { images: _imgs.map(x => ({ name: x.name, data: x.data })) } : {}) }),
  });
  if (r.status === 200) {
    // 回答已送达 ⇒ 提问收掉（和切会话走同一个收尾，别写第二套）
    dropAsk();
    setRunning(true);          // 它接着跑了，状态立刻回来
  } else {
    btn.disabled = false;
    btn.textContent = '回答并继续';
    showErr('回答没发出去：' + ((r.body && r.body.error) || ('HTTP ' + r.status))
            + (_stopping ? '（正在暂停，停完就能发）' : ''));
  }
}

// 兼容老入口：流里那块回答框已经改成弹窗，但点击委托还在
async function onAskSend(ev) {
  // ★★ 「打开问题」必须先判（2026-10-26 修的 bug）。
  //   原来第一句是 `closest('.askgo')`，没有就 return —— 而「打开问题」那个按钮
  //   的 class 是 `.reopen`，不带 `.askgo`，于是**点了永远没反应**，
  //   而它恰恰是"收起弹窗之后唯一的回去的路"。收起来就再也打不开了。
  const reopen = ev.target.closest && ev.target.closest('.reopen');
  if (reopen) {
    if (_pendingAsk) showAsk(_pendingAsk);
    else showErr('这个提问已经不在了（可能是已经在别处回答过）。');
    return;
  }
  const btn = ev.target.closest && ev.target.closest('.askgo');
  if (!btn) return;
  if (btn.id === 'askgo') return;   // 弹窗里的按钮有自己的绑定
  const box = btn.closest('.askblock');
  const ta = box && box.querySelector('.askin');
  const text = (ta && ta.value || '').trim();
  if (!text) { if (ta) ta.focus(); return; }
  btn.disabled = true;
  btn.textContent = '已发送';
  const r = await api('/sessions/' + encodeURIComponent(cur) + '/say', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  });
  if (r.status !== 200) {
    btn.disabled = false;
    btn.textContent = '回答';
    showErr('回答没发出去：' + ((r.body && r.body.error) || ('HTTP ' + r.status)));
  }
}

// 思考标题点击 ⇒ 展开/收起。
// ★ 2026-10-27：活的思考块（streaming）也允许点开。
//   原来这里对 streaming 直接 return，理由是"折了正在写的字就看不见了"——
//   但那是把「默认展开」当成前提写的。现在它**默认收起**（展开时占大半屏，
//   人真正要看的是正文），所以必须留一个"想看就点开"的入口，否则永远看不到。
function onReasoningClick(ev) {
  const h = ev.target.closest && ev.target.closest('.rhead');
  if (!h) return;
  const box = h.parentElement;
  if (!box || !box.classList.contains('reasoning')) return;
  box.classList.toggle('collapse');
}

function onFeedClick(ev) {
  const btn = ev.target.closest && ev.target.closest('.copybtn');
  if (!btn) return;
  const pre = btn.parentElement.querySelector('pre');
  if (!pre) return;
  const done = msg => { btn.textContent = msg; setTimeout(() => { btn.textContent = '复制'; }, 1200); };
  try {
    navigator.clipboard.writeText(pre.textContent).then(() => done('已复制'), () => done('失败'));
  } catch (e) { done('失败'); }
}
if ($('feed')) $('feed').addEventListener('click', onFeedClick);
if ($('feed')) $('feed').addEventListener('click', onReasoningClick);
if ($('feed')) $('feed').addEventListener('click', onAskSend);

// 回到底部：翻上去超过一屏半才出现
$('feed').addEventListener('scroll', () => {
  const f = $('feed');
  $('tobottom').hidden = (f.scrollHeight - f.scrollTop - f.clientHeight) <= 220;
});
on('tobottom', 'onclick', () => {
  const f = $('feed');
  f.scrollTop = f.scrollHeight;
  $('tobottom').hidden = true;
});


// ── 双通道运行状态（2026-10-28）────────────────────────────────────────────
// 用户：「本我和自我哪一个真实在跑、跑了多久，没有明显的动态指标」。
// 原来只有顶栏一个「在跑/空闲」，看不出**是谁**在跑、各自跑了多久。
// 两条各一个真源：_selfSince/_lastSelfMs（自我）、_benniSince/_lastBenniMs（本我），
// 不另开计时器 —— 每次 paintPill() 顺带刷一次（它本来就每秒跑）。
// 规划这条线的状态（charter / prep 事件写）——它是第三条 harness 路线
let _planInfo = null;

function paintDuo() {
  const host = $('duo');
  if (!host) return;
  const now = Date.now();
  const fmt = ms => {
    const s = Math.max(0, Math.round(ms / 1000));
    return s >= 60 ? Math.floor(s / 60) + 'm' + (s % 60) + 's' : s + 's';
  };
  const one = (kind, name, model, since, lastMs, stateOverride) => {
    const live = !!since;
    const state = stateOverride || (live ? '在跑 ' + fmt(now - since)
      : (lastMs ? '完成 ' + fmt(lastMs) : '未跑'));
    return '<span class="duo ' + kind + (live ? ' live' : (lastMs ? ' done' : '')) + '">'
      + '<i class="dudot"></i><b>' + name + '</b>'
      + (model ? '<span class="dumodel">' + esc(model) + '</span>' : '')
      + '<span class="dustate">' + state + '</span></span>';
  };
  // 规划是**定约状态**，不是一直在跑的进程，所以不用秒表，显示定到哪一步
  // ★ 三种状态，别混：「未接入」≠「未起草」（2026-10-29）。
  //   界面上这条路从来没接规划者（`server.py` 里一个 `plan.py` 都没有，
  //   翻遍提交历史也没有）。显示"未起草"会让人以为它是可选的一步、
  //   点一下就有；而真相是这一档根本没有这个角色。
  const pi = _planInfo || {};
  const planState = pi.drafting ? '起草中'
    : (pi.count ? ((pi.locked ? '已锁 ' : '未锁 ') + pi.count + ' 条')
       // ★ 没有契约时按**档位**说（2026-10-29 联调撞到）：
       //   用户选了 L2，界面上却写「未接入」—— 看起来像选错了档。
       //   事实：L2/L4 里**有**规划者，但它只在**多轮任务**里上场；
       //   界面上这条单轮对话的路径不经过它。说清这一点，比两个词都诚实。
       : (_crewHasPlan() ? '多轮任务里上场' : '未接入'));
  host.innerHTML = one('planner', '规划者', pi.model || '', 0, 0, planState)
    + one('self', '执行者', _stat.model || '', _selfSince, _lastSelfMs)
    + one('benni', '测试者', (typeof _benniModel !== 'undefined' ? _benniModel : '') || '',
        _benniSince, _lastBenniMs, _benniStateOverride());
}

// 这一档有没有规划者。数据来自会话目录里的 crew.txt（2026-10-29 才落盘的）。
// ★ 查不到就当没有：猜一个"有"出来，比显示"未接入"更误导人。
function _crewHasPlan() {
  // ★ 会话列表存在 `_sessById`（base.js 的 loadSessions 填的 Map）。
  //   别凭印象写变量名 —— 我第一次写成了 `_sessCache`，那个名字根本不存在，
  //   于是函数永远返回 false，而界面看起来像"档位没生效"。
  const me = (typeof _sessById !== 'undefined' && _sessById && cur)
    ? _sessById.get(cur) : null;
  const c = String((me && me.crew) || '').toLowerCase();
  return c === 'l1' || c === 'l3' || c === 'l4';
}


// 测试者那一格的状态：**跑过没有**由后端给（`sessions` 里的 `benni` 字段）。
// 在跑的时候不交给他（让 one() 显示"在跑"），只有"跑完了/没跑"才需要这份事实。
function _benniStateOverride() {
  if (typeof _benniSince !== 'undefined' && _benniSince) return '';
  const me = (typeof _sessById !== 'undefined' && _sessById && cur) ? _sessById.get(cur) : null;
  return (me && me.benni) ? '完成' : '';
}

(function mountDuo() {
  const crow = document.querySelector('.crow');
  const existing = $('duo');     // 判空要写成判据认得出的形式：先取变量，再 if
  if (existing) return;
  if (!crow) return;
  const d = document.createElement('span');
  d.id = 'duo';
  d.className = 'duobox';
  crow.appendChild(d);
  paintDuo();          // 先画一次「未跑」，别等下一秒才有内容
})();
