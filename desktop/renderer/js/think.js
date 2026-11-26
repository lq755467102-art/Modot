// ── 流式：思考 + 正文 ──
// ★ 和 WorkBuddy 一样：**不做打字机动画**。
//   它的观感来自 token 流本身（取到什么显示什么）。我们原来自己加了 rAF 逐字推进，
//   结果是"字一个一个挤出来"，跟它不像 —— 用户的原话是「他不要动画我们也不要」。
//   仍然 120ms 取一次：那是**取数频率**，不是动画。
// 主要引用或加载入口包括 `desktop/renderer/index.html`。
let _stErrAt = 0;
let _streamBusy = false;
// 上一轮已经"收掉"的思考正文。★ 为什么需要它：轮末把活的块收成静态块之后，
// /think 里那段草稿**还在**（后端要等下一轮 on_turn 才清），于是 120ms 后
// 它又被写回来、把刚收起的那块重新顶开 —— 表现为"思考收不住"。
let _thinkDone = '';

// ★★ 思考正文要不要"跟着底部走"。
//   原来 thSetDraft 每 120ms 无条件 `body.scrollTop = body.scrollHeight` ——
//   于是人往上拉一下，120 毫秒后又被拽回底部。现象就是**滚动条被锁死、拉不动**，
//   而代码里没有任何地方写着"禁止滚动"。根子是"自动跟随"没有让位给"人在看"。
//   现在：只有本来就贴着底部时才跟随；人一往上拉就停手，直到他自己滚回底部。
let _thinkStick = true;

// 上一条插入的纯文本行（.line）的 HTML。用来吞掉连续重复的流水账（见渲染循环里那段）。
let _lastLineHtml = '';

// 下一次轮询拿到的是不是"打开会话时重放的历史"。clearFeed 里置 true，
// 第一批事件处理完就落回 false。用来区分"历史里问过的"和"刚刚问的"。
let _booting = true;

function _thinkBindScroll() {
  const b = $('tbody');
  if (!b || b.dataset.stick === '1') return;
  b.dataset.stick = '1';
  b.addEventListener('scroll', () => {
    _thinkStick = (b.scrollHeight - b.scrollTop - b.clientHeight) < 24;
  }, { passive: true });
}

const RARROW = '<svg class="rarrow" width="12" height="12" viewBox="0 0 16 16" fill="none">'
             + '<path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.4" '
             + 'stroke-linecap="round"/></svg>';

async function streamTick() {
  // 120ms 是取数频率，不是允许并发；慢请求没回来就跳过这一拍。
  if (_streamBusy) return;
  _streamBusy = true;
  try { await _streamTickOnce(); } finally { _streamBusy = false; }
}

async function _streamTickOnce() {
  const sid = cur;
  const viewVersion = _viewVersion;
  if (!sid) return;
  // 没在跑就别请求（120ms 一次、两个请求，空闲时是实打实的电）
  if (!running) { stHide(); thSetDraft(''); return; }
  try {
    // ★ 一次请求拿两样（2026-10-27）：原来是 /think + /stream 各一次，
    //   120ms 一轮就是 16 个请求/秒。合成 /live 之后减半，行为不变。
    const lr = await api('/sessions/' + encodeURIComponent(sid) + '/live');
    if (cur !== sid || _viewVersion !== viewVersion || lr.status !== 200) return;
    thSetDraft((lr.body.think || '').trim());
    const stext = lr.body.stream || '';
    if (!stext.trim() || stext.trim() === _lastSay) { stHide(); return; }
    stShow(stext);
  } catch (e) {
    // ★ 不许静默。这个循环一失败就是"界面上什么都不动"，
    //   而人要猜是模型没写、还是轮询挂了 —— 5 秒报一次，不刷屏。
    if (!_stErrAt || Date.now() - _stErrAt > 5000) {
      _stErrAt = Date.now();
      console.warn('流式轮询失败：', e);
    }
  }
}

// ── 正文流（2026-10-29 简化）──
//
// ★ 边写边看由 **delta 事件**接管了（`events.js` 的 `mountDelta`），
//   它和工具、判据在同一条 seq 序列里 —— 那才是"不跳"的正解。
//   这里只留一个**兜底**：老会话（没有 delta）、或后端没开流式时，
//   取到什么显示什么。
//
// ★★ 删掉了"平滑打字"那套（`_stQueue` / `_stTypeTick`，约 100 行）：
//   它和 delta 是**两套机制同时写同一个容器**，互相干扰。
//   联调时用户报"整体输出很不流畅"，其中一部分就是这两套在抢。
//   留一套，不留两套。
function stShow(text) {
  const w = $('streaming'), b = $('sbody'), f = $('feed');
  if (!w || !b) return;
  // 先判"人是不是在底部"，再改内容 —— 反过来判就永远跟不上
  const follow = f ? (f.scrollHeight - f.scrollTop - f.clientHeight) < 140 : false;
  if (w.hidden) { w.hidden = false; w.classList.add('feedpos'); }
  liftTail();
  const t = String(text || '');
  if (b.dataset.text !== t) {
    b.dataset.text = t;
    b.innerHTML = mdToHtml(t);
  }
  b.scrollTop = b.scrollHeight;
  if (follow && f) f.scrollTop = f.scrollHeight;
}

function stHide() {
  const w = $('streaming');
  if (w) w.hidden = true;
  const b = $('sbody');
  if (b) { b.textContent = ''; b.dataset.text = ''; }
}

// ── 思考：一轮合成**一块** ──
// ★ 为什么要合成一块
//   WorkBuddy 一轮结束后会把思考清掉、只留回复。用户不想清屏，但提了个更好的做法：
//   「输出后把所有的思考块用一个大的块折叠起来」。
//   所以：#thinking 是**这一轮**的思考块，一轮里每次 llm 的 reasoning 都作为
//   一个 .rseg 段塞进同一个正文里（段间一条淡线），轮末整体转成一块**收起**的静态块。
// 一轮开始：把活的思考块挪到**执行流末尾**，它才算"这一轮的开头"。
// ★ 为什么必须挪：#thinking 在 HTML 里固定在 feed 靠前的位置；一直在那儿的话，
//   轮末把静态块插到它前面，静态块就跑到历史最上面（实测：思考块跑到用户提问上面）。
//   挪到末尾之后，"插在它前面" = "插在这一轮的末尾"，位置才对。
//   #streaming 排在它后面（那段话还没说完，属于末尾）。
//   只在它从隐藏变可见时挪一次 —— 每次都挪会把思考块甩到过程折叠下面。
function openThink(box) {
  const wasHidden = box.hidden;
  box.hidden = false;
  // 流式那一块也标上模型 —— 否则跑的时候看不出是谁在想
  const _hd = $('thead');
  if (_hd) {
    const who = String(_stat.model || '').trim();
    _hd.textContent = who ? ('思考中 · ' + who) : '思考中';
  }
  _thinkBindScroll();          // 绑一次就够（dataset 里记着），重复调用无副作用
  if (!wasHidden) return;
  const f = $('feed');
  if (!f) return;
  f.appendChild(box);
  const st = $('streaming');
  if (st && st.isConnected) f.appendChild(st);
}

// 思考块的标题：从「思考原文前 30 字」改成「这一轮在琢磨什么」。
//
// ★ 为什么要改（2026-10-25 用户原话）：
//   「这个思考过程是否可以用概括性语言说出 agent 目前正在干什么或者想什么？
//     要不然放眼过去全是思考，看不出区别」
//   原来是这样截的：`t2.slice(0,30)` ⇒ 七条并排全是
//   `The run dir now has trace.json...` / `Big finding: ...` / `Excellent: ...`
//   —— 那不是概括，是**同一副长相重复七遍**。
//
// ★ 为什么不用模型去概括：每条思考都要多一次调用，几十条就是几十次；
//   而且概括错了比不概括更糟 —— 原文至少是事实，编出来的"它在想什么"不是。
// ★ 所以只做**零猜测**的提炼，按信噪比分三级：
//   ① 中文意图句（「接下来要先…」）—— 它自己说了要干什么，最准
//   ② 文件名 —— 它在琢磨哪个东西，客观、可核
//   ③ 都没有 ⇒ **退原文截断**（fallbackChars 给了才退），绝不返回空
//
// ★★ 为什么必须有 ③（2026-10-26 验收报出来的真问题）：
//   只留 ①② 的话，一个既没有意图句、也没提到文件的思考会拿到空标题 ——
//   于是 10 个块并排全是「思考」两个字。那正是这个函数一开始要治的病。
//   退原文截断虽然不好看，但它是**事实**，而且彼此不同 —— 至少分得出来是哪一轮。
//   ⇒ 两个调用方都传 fallbackChars：宁可丑，不可重复。
function _thinkGist(t, fallbackChars) {
  const s = String(t || '').replace(/\s+/g, ' ').trim();
  if (!s) return '';
  const m = /(?:我先|先要|接下来|下一步|需要先|然后我|我打算|我再)[^。！？；,，]{4,40}/.exec(s);
  if (m) {
    const x = m[0].trim();
    return x.length > 28 ? x.slice(0, 28) + '…' : x;
  }
  const f = /([\w.-]+\.(?:py|jsonl|json|js|cjs|html|css|md|txt|yml))/.exec(s);
  if (f) return '关于 ' + f[1];
  if (fallbackChars) {
    return s.slice(0, fallbackChars) + (s.length > fallbackChars ? '…' : '');
  }
  return '';
}

// ── 叙述行：把「它自己说要干什么」写进过程流 ──
// ★ 用户 2026-10-26 圈了 WorkBuddy 的截图问「思考过程是否可以穿插输出这种总结性语言」：
//   它长这样 ——
//       先看现在右栏（进度栏）、主区容器的实际结构，再决定怎么改。
//       ⤷ 运行校验、查看文件：index.html
//       进度栏现在放在左栏下半部分。先看它如何渲染、节点数据从哪里来：
//       ⤷ 运行校验、查看文件：loop.py
//   也就是：**每一步之前有一句人话**，说明这一步打算干什么，工具行跟在后面。
//   没有这一句，一串工具行就是"它在动"，但没有"它在动什么"。
//
// ★ 硬规矩：只取**它自己说过的原话**，不改写、不概括、不拼凑。
//   改写出来的句子是"我以为它要干什么"，而这句话会被人当成事实读。
//   取不出来就不写 —— 少一行，比写一行假的好。
function _narrLine(t) {
  const raw = String(t || '').replace(/\r/g, '');
  if (!raw.trim()) return '';
  // 去掉 markdown 标记：思考里常有 **加粗**、`代码`、## 标题
  const flat = raw.replace(/^#+\s*/gm, '')
                  .replace(/\*\*([^*]*)\*\*/g, '$1')
                  .replace(/`([^`]*)`/g, '$1')
                  .replace(/\s+/g, ' ')
                  .trim();
  // 按句号/感叹/问号/分号切。★ 用 lookbehind 保留标点（切完还能看出是哪种句子）
  const parts = flat.split(/(?<=[。！？!?])/).map(x => x.trim()).filter(Boolean);
  // ★ 认两种情况（2026-10-26 拿真实 reasoning 校准过两轮）：
  //   A. 以**意图开头**（先 / 让我 / 我需要 / 接下来 / 试试 …）
  //   B. 含「动词 + 一下/一遍」（读一下、查一遍）
  //   ★ 为什么不用"含动作词"：真实数据里有一句
  //     「这是一个 agent-harness 自我改进系统。」—— 含"改"，但它是**评价**不是意图，
  //     放到叙述位上，人会以为它下一步要去改什么。
  //   ★ 也不收"验收失败了：d79 …"这类句子：那是结论，不是打算。
  //     结论放在叙述位上，读起来像"下一步要做的事是失败"。
  const LEAD = /^(先|让我|我再|我先|我需要|我们要|我要|我打算|接下来|然后|下一步|现在|试试|需要|得先|准备|看看|查一下|读一下|改一下|跑一下|确认|核对|检查|整理)/;
  const DOIT = /(看|查|读|写|改|跑|试|确认|核对|验证|检查|整理|补|修|加|删|测)(一下|一遍)/;
  const fit = x => x.length >= 10 && x.length <= 90;
  for (const s2 of parts) {
    const x = s2.trim();
    if (!fit(x)) continue;
    if (LEAD.test(x) || DOIT.test(x)) return x;
  }
  return '';
}

// ── "这一轮在干什么具体的事" ──
// ★ 用户 2026-10-25 把意思说清楚了：
//   「我的意思是想改成 workbuddy 在实际运作的时候，他会直接说 agent 这轮在干什么
//     具体的事情，然后 agent 空着思考的时候才会去说他在思考什么」
//   ⇒ 顺位反过来了：**动作优先，思考兜底**。
//     有工具动作 ⇒ 标题写动作（"在读 reflex.py"），"思考"两个字去掉；
//     整轮没有任何动作（模型只是想了想就答了）⇒ 才退回"思考 · 关于 X"。
// ★ 为什么这样更对：动作是**发生过的事实**，思考是**打算**。
//   事实永远比打算更能回答"它现在在干什么"。
function _actionGist(e) {
  const a = e.args || {};
  const p = String(a.path || '').split(/[\\/]/).pop();
  const cmd = Array.isArray(a.cmd) ? a.cmd.join(' ') : String(a.cmd || '');
  const with_ = (v, o) => (o ? v + '：' + o : v);
  switch (e.tool) {
    case 'read_file':   return with_('读文件', p);
    case 'edit_file':   return with_('改文件', p);
    case 'write_file':  return with_('写文件', p);
    case 'run_check': {
      const m = /([\w.-]+\.(?:py|sh|js|cjs))/.exec(cmd);
      if (m) return '运行校验：' + m[1];
      // 没有脚本名时按命令真正在做的事说 —— 一律写"运行命令"等于没说。
      // 实测同一条 python -c 里，find 是找文件、os.listdir 是看目录，差别很大。
      if (/\bgrep\b|\brg\b|findstr/.test(cmd)) return '搜索内容';
      if (/\bfind\b/.test(cmd)) return '查找文件';
      if (/\bls\b|os\.listdir|\bdir\b/.test(cmd)) return '查看目录';
      if (/\bcat\b|\bhead\b|\btail\b|os\.walk|\.read\(/.test(cmd)) return '读取文件';
      return '运行命令';
    }
    case 'recent_runs': return '查看最近几轮';
    case 'ask_human':   return '等你拍板';
    default:            return TOOL_CN[e.tool] || '';
  }
}
// 一个动作在"代表这一轮干的事"这件事上的分量：改东西 > 跑东西 > 看东西。
// 一轮里常有"读 a → 改 b → 跑验收"，最后留下"在跑 accept.py"才对。
const _ACT_RANK = { edit_file: 3, write_file: 3, run_check: 2, read_file: 1 };
let _lastThinkSec = null;
// 这一轮"打算干什么"的那句话（由 appendThink 留，mountTool 用掉就清空）
let _narrPending = '';
// 已经作为叙述行显示过的句子：连续两轮说同样的话时，第二遍不再占一行
let _narrShown = new Set();

function _noteThinkAction(e) {
  const sec = _lastThinkSec;
  if (!sec || !sec.isConnected) return;
  const rank = _ACT_RANK[e.tool] || 0;
  if (rank < Number(sec.dataset.rank || 0)) return;   // 轻的别盖掉重的
  // 同一份摘要：过程行和思考块标题不能各说各的
  const g = e.summary || _actionGist(e);
  if (!g) return;
  sec.dataset.rank = String(rank);
  const t = sec.querySelector('.rtext');
  if (!t) return;
  // ★ 换掉的是**这一句**，不是整行：图标和右边那个模型名不动。
  //   结果是 `[图标] 运行校验：accept.py .......... deepseek-flash [箭头]`
  t.textContent = g;
  sec.dataset.acted = '1';
}

// 标题前面那个小图标。WorkBuddy 每个过程条目前面都有一个，作用不是装饰 ——
// 它让"这一行是一件事"在一眼扫过去时成立，不用读字就知道这是过程不是正文。
const THINK_ICON = '<svg class="ric" width="13" height="13" viewBox="0 0 16 16"'
  + ' fill="none" aria-hidden="true">'
  + '<circle cx="8" cy="8" r="6.3" stroke="currentColor" stroke-width="1.1"/>'
  + '<path d="M5.4 8h5.2" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>'
  + '</svg>';

function appendThink(text, ev) {
  // ★★ 用户：「思考过程我还是想分轮次，现在这个全部集合在一个思考窗口显得不够流程化，
  //   只要最后小的思考窗口都汇聚成一个大的就行」。
  //   ⇒ 原来是"一轮内所有 llm 的思考塞进同一个 #thinking、段间一条淡线"——
  //     跑的时候看不出它在第几步想什么。
  //   ⇒ 现在：**每次 llm 的思考单独成一个小块**插进流里（带这一段的开头做标题），
  //     这一段结束时由 finalizeThink() 把它们收成一个大的折叠块。
  const t2 = String(text || '').trim();
  if (!t2) return;
  const feed = $('feed'), live = $('thinking');
  if (!feed) return;
  const sec = document.createElement('section');
  // ★ 思考块的形式由用户选（2026-10-29）：折叠 / 展开 / 不显示。
  //   这里只管**新建的那一块**；已存在的块由 display.js 统一处理
  //   （改偏好要立刻生效，不能等下一条）。
  const _dp = (typeof DP !== 'undefined') ? DP.get('think') : 'collapse';
  sec.className = 'reasoning complete thinkmini' + (_dp === 'expand' ? '' : ' collapse');
  if (_dp === 'hide') sec.style.display = 'none';
  // ★ 用户：「思考过程最好可以写出来是哪家模型的思考过程，
  //   这样可以更好区分哪个是自我、哪个是本我」。
  //   ★ 用 ev.model（这条事件自己的）优先于 _stat.model（全局最后一条）：
  //     重放历史的时候 _stat.model 已经被后面的事件覆盖了，全局值会把
  //     早先的思考也算到最后一个模型头上。
  // ★ 除了模型名也认 role（2026-10-29）：规划者的思考不是"某个模型"，
  //   是"这条 harness 在想"。三条 harness 串行跑，标出是谁在想比标模型名有用。
  const _who = String((ev && (ev.model || ev.role)) || _stat.model || '').trim();
  const gist = _thinkGist(t2, 30);
  // ★ 这一段思考里"它自己说打算干什么"的那句，留给紧跟着的工具调用当叙述行。
  //   放在这里取而不是在 mountTool 里取：那时思考块的正文已经被收走了，
  //   能取到的只剩标题（30 字），而标题为了排版是截断过的。
  _narrPending = _narrLine(t2);
  // ★ 默认是「思考：关于 X」—— 但这只在**整轮没有任何工具动作**时才是最终形态。
  //   一旦它调了工具，_noteThinkAction 会把这一行整个换成「运行校验：accept.py」。
  //   顺位是"动作优先、思考兜底"，因为动作是发生过的事实，思考只是打算。
  const title = gist ? '思考：' + gist : '思考';
  sec.innerHTML = '<header class="rhead">' + THINK_ICON
                + '<span class="rtext">' + esc(title) + '</span>'
                + (_who ? '<span class="rwho">' + esc(_who) + '</span>' : '')
                + RARROW + '</header><div class="rbody"></div>';
  // 留着三个"底子"：谁、第几轮、以及当前的标题。
  // 后面这条思考要是引出了工具动作，_noteThinkAction 就把标题改写成"在干什么"。
  sec.dataset.who = _who;
  sec.dataset.turn = (ev && ev.turn) ? String(ev.turn) : '';
  sec.dataset.rank = '0';
  _lastThinkSec = sec;
  sec.querySelector('.rbody').textContent = t2;
  // ★★ 思考块不再独立插进流里（2026-10-27 做减法）。
  //   一个 6 会话的样本里 `llm` 事件有 **79 条** —— 每调一次模型就在流里留一行
  //   「思考：关于 X」，而 `say` 只有 7 条。正文被这 79 行挤到看不见，
  //   用户的原话是「我根本就看不到任何正文」「思考过程为什么越来越混乱」。
  //   ⇒ 思考进**过程容器**：流里只留一行摘要，点开能看到每一段的完整思考。
  //     信息一点没少，只是不再和对话抢地盘。
  procAppendNode(sec);
  // 活块清空，留给下一次 llm 的流式
  const body = $('tbody');
  if (body) body.innerHTML = '';
  const hd = $('thead');
  if (hd) hd.classList.remove('loading');
}

// 流式草稿：写在正文末尾的一个 .rdraft 里，不覆盖已有的 .rseg
function thSetDraft(text) {
  const box = $('thinking'), body = $('tbody'), hd = $('thead'), f = $('feed');
  if (!box || !body) return;
  if (!text) {
    const d = body.querySelector('.rdraft');
    if (d) d.remove();
    if (hd) hd.classList.remove('loading');
    if (!body.querySelector('.rseg')) box.hidden = true;
    return;
  }
  // 残留草稿（已经是收掉那段的前缀）⇒ 不重新打开
  if (_thinkDone && (_thinkDone === text || _thinkDone.startsWith(text))) return;
  const follow = f ? (f.scrollHeight - f.scrollTop - f.clientHeight) < 140 : false;
  openThink(box);
  if (hd) hd.classList.add('loading');       // 标题扫光 = 它正在想
  let d = body.querySelector('.rdraft');
  if (!d) { d = document.createElement('div'); d.className = 'rdraft'; body.appendChild(d); }
  if (d.textContent !== text) d.textContent = text;
  // ★ 只有本来就贴底才跟随。见 _thinkStick 那段注释 ——
  //   无条件跟随 = 滚动条被锁死（人往上拉，120ms 后被拽回去）。
  if (_thinkStick) body.scrollTop = body.scrollHeight;
  if (follow && f) f.scrollTop = f.scrollHeight;
}

// 一轮结束：把这一轮的思考转成一块**收起**的静态块，然后把活的块清空待下一轮。
// ★ 为什么克隆而不是直接改原节点：#thinking 的 id 被别处引用（清屏、轮询），
//   保留它继续当"下一轮的画布"，历史那块用一份不带 id 的副本。
function finalizeThink() {
  const box = $('thinking'), body = $('tbody'), hd = $('thead');
  const feed = $('feed');
  if (!box || !body) return;
  // ★ 这一轮结束了 ⇒ 断掉"谁还能被改标题"的指针。
  //   不清的话，下一轮的工具动作会把**上一个**思考块的标题改掉 ——
  //   而那个块已经是历史、人已经读过它了。
  _lastThinkSec = null;
  const draft = body.querySelector('.rdraft');
  if (draft) draft.remove();
  const txt = (body.textContent || '').trim();
  _thinkDone = txt;                 // 记住它，挡住残留草稿（见 _thinkDone 定义处）
  // ★ 不再"把这一轮几段思考再收进一个 details"（2026-10-27）：
  //   那个 details 的作用就是折叠，而现在**过程容器本身就是那个折叠**。
  //   再套一层的结果是"折叠里套折叠"，点两下才看得到一段思考 —— 比不收更糟。
  if (txt) {
    const sec = document.createElement('section');
    const _dp2 = (typeof DP !== 'undefined') ? DP.get('think') : 'collapse';
    sec.className = 'reasoning complete thinkmini grouped' + (_dp2 === 'expand' ? '' : ' collapse');
    if (_dp2 === 'hide') sec.style.display = 'none';
    // ★ 标题带上这一轮"在想什么"的开头。
    //   原来每一块都只写"思考"两个字，一个 10 轮的会话就是 10 行一模一样的"思考"
    //   叠在一起 —— 用户原话「思考全挤在一个大块里了」。
    //   收起状态下人是靠这行字区分它们的，所以这行必须各不相同。
    // ★★ 长相必须和 appendThink 造的那种一致（2026-10-26）：
    //   这里原来用的是旧模板（只有「思考」+ 前 34 字，没有图标、没有模型名），
    //   于是同一个会话里两种思考块交替出现 —— 一种带小图标和「运行校验：accept.py」，
    //   一种只有一行灰字。用户要的是"严格跟 workbuddy 一致"，先一致得自己能一致。
    const gist = _thinkGist(txt, 30);
    const _who = String(_stat.model || '').trim();
    const title = gist ? '思考：' + gist : '思考';
    sec.innerHTML = '<header class="rhead">' + THINK_ICON
                  + '<span class="rtext">' + esc(title) + '</span>'
                  + (_who ? '<span class="rwho">' + esc(_who) + '</span>' : '')
                  + RARROW + '</header>'
                  + '<div class="rbody"></div>';
    sec.querySelector('.rbody').innerHTML = body.innerHTML;
    // 活块可能已经不在文档里（切过会话）—— 那就别插，免得抛错带崩这条轮询
    if (box.parentElement) procAppendNode(sec);
  }
  body.innerHTML = '';
  if (hd) hd.classList.remove('loading');
  box.hidden = true;
}

// 切会话 / 清屏时用：把活的思考块清干净（不生成历史块）
function resetThink() {
  const box = $('thinking'), body = $('tbody'), hd = $('thead');
  if (body) body.innerHTML = '';
  if (hd) hd.classList.remove('loading');
  if (box) box.hidden = true;
  _thinkStick = true;          // 新的一轮：重新从"贴底跟随"开始
}


