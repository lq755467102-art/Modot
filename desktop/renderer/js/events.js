// ── 事件流 ──
// setInterval 不会等 async 函数结束。以前每 500ms 都新开一轮请求，
// 服务稍慢就会堆队；旧响应晚回来时，已经把结束事件画完的界面又会补画旧增量，
// 看起来正是「流程结束后思考/正文才一次性跳出来」。同一时间只保留一轮。
// 主要引用或加载入口包括 `desktop/renderer/index.html`。
async function poll() {
  if (pollBusy) return;
  pollBusy = true;
  try { await _pollOnce(); } finally { pollBusy = false; }
}

// 旧会话页增量读取 `bin/server.py` 的会话状态与事件；先更新真实运行态，再把新事件分发到正文和过程。
async function _pollOnce() {
  const sid = cur;
  const viewVersion = _viewVersion;             // 请求期间人可能切会话；响应只能画回发起时的那个会话
  if (!sid) return;

  // ★ 先拿"还在不在跑"，再渲染。
  //   原来顺序反了：渲染一旦抛异常，setRunning 就执行不到 ——
  //   任务早就结束了，输入框却一直禁着,人只能看着"在跑"发不出去。
  //   状态更新必须是最不容易失败的那一步，所以放最前面。
  const all = await api('/sessions/' + encodeURIComponent(sid) + '/runtime');
  if (cur !== sid || _viewVersion !== viewVersion) return;
  const me = all.status === 200 ? all.body : null;
  if (me) {
    const pending = $('pending-status');
    if (pending) pending.hidden = !me.pending_unknown || !!me.running;
    const resolve = $('pending-resolve');
    if (resolve) resolve.onclick = async () => {
      if (!window.confirm('确认已检查这条待办的日志和实际产出？归档只释放队列，不会重跑或撤销原操作。')) return;
      resolve.disabled = true;
      try {
        const reply = await api('/sessions/' + encodeURIComponent(sid) + '/pending/resolve',
          {method:'POST',body:{confirm:true}});
        if (cur !== sid || _viewVersion !== viewVersion) return;
        if (reply.status !== 200) showErr(reply.body.error || '归档未完成');
        else { pending.hidden = true; await pollReceipts(); }
      } finally { resolve.disabled = false; }
    };
    setRunning(!!me.running);
    if (me.runtime?.state === 'unknown') stateNote('当前运行状态待核对，请检查日志和产出后再继续', 0);
    if (me.runtime?.state === 'stopping') _stopping = true;
    _stat.status = me.status || "";  // ★ 服务端给的状态（事件流里也会再算一遍，见下）
  }

  const { status, body } = await api('/sessions/' + encodeURIComponent(sid)
                                     + '/events?limit=200&after=' + seen);
  if (cur !== sid || _viewVersion !== viewVersion) return;
  if (status === 200 && !validateProtocol('EventsPage',body)) {
    showErr('事件分页协议不兼容，请更新并重启服务与界面'); return;
  }
  if (status === 200 && Array.isArray(body.events) && body.events.length) {
    const feed = $('feed');
    const atBottom = _stickBottom;
    // ★ 这一批是不是"打开会话时重放历史"（见 ask_human 那一支）。
    //   只有第一批算历史；之后每次轮询拿到的都是刚发生的。
    const _histBatch = _booting;
    _booting = !!body.has_more;
    // ★★ 提问要不要弹，**用这一批事件自己判**（2026-10-26 修的偶发漏弹）：
    //   原来是看 `_stat.status`，而它来自上面那次 `/api/sessions` ——
    //   那次请求偶发失败（上一行 `if (me)` 就不成立），status 保持空，
    //   于是"打开一个正在等人的会话"有时弹有时不弹。**同一件事不该依赖第二个请求。**
    //   ⇒ 这一批里记下：有没有新提问、最后一条 end 是什么结论。
    const reduced = body.events.reduce(reduceEventState, _eventState);
    _eventState = reduced;
    body.events.forEach(e => {
      if (!validateProtocol('Event', e)) {
        showErr('收到格式不兼容的事件，请核对前后端版本');
        return;
      }
      // 单-flight 是主防线；序号过滤是兜底。任何迟到的旧批次都不能重画。
      if ((e.seq || 0) <= seen) return;
      // ★ 规划这条线的状态（2026-10-29 用户：「规划者是一条新的 harness 路线」）。
      //   它原来只体现在契约卡上，状态条里看不见 —— 现在跟自我/本我一样占一条。
      // ★★ 但 `prep` **不等于「规划者在起草」**（2026-10-29 当天就踩了）：
      //   prep 是"准备中"（读总纲、装配工具），后端每轮都发；
      //   而原来这里把它当成"起草中"的信号，于是**任务一开跑，
      //   规划者那个位就显示「起草中」，而后台根本没起草**。
      //   现在只认 prep 自己带的 `drafting` 字段 —— 真起草才为 true。
      if (e.act === 'prep') {
        _planInfo = { drafting: !!e.drafting, locked: false, count: 0, model: '' };
      }
      // ★ 只认**带判据的契约**（contract）。charter 是"本轮注入"，
      //   没有 items、没有 locked —— 让它也更新会把定约状态冲成「未起草」。
      if (e.act === 'contract') {
        _planInfo = { drafting: false, locked: !!e.locked,
                      count: (e.items || []).length, model: e.model || '' };
      }

      // ★ 单条渲染失败不能带崩整条轮询 —— 一个事件字段长得不对，
      //   就让人再也输入不了，这个代价完全不成比例。
      // ★ 不认识的 act ⇒ 兜底显示，**不静默丢**（见上面 mountUnknown 那段）。
      try {
        if (!_KNOWN_ACTS.has(e.act)) {
          mountUnknown(e); seen = Math.max(seen, e.seq || 0); return;
        }
        // ★ `delta` 是旧名；`agent_message_chunk` / `agent_thought_chunk` 是 ACP 的名字。
        //   两种都认（老会话里是旧名，新会话是 ACP 名）。
        if (e.act === 'delta' || e.act === 'agent_message_chunk' || e.act === 'agent_thought_chunk') {
          mountDelta(e); seen = Math.max(seen, e.seq || 0); return;
        }
        if (e.act === 'benni') { mountBenni(e); seen = Math.max(seen, e.seq || 0); return; }
        // ★ 执行者的**自检**批注（2026-10-29）：它原来是 `benni + live`，
        //   于是界面上写着"测试者在过程中批注" —— 而按三条 loop 串行，
        //   测试者是执行者跑完之后才登场的另一条线。
        //   自检属于执行层（它的产出只给执行者），所以单独一个事件身份。
        if (e.act === 'check') { mountCheck(e); seen = Math.max(seen, e.seq || 0); return; }
        // ★ 需要人回答 ⇒ 开**弹窗**（不往流里插一行输入框）。
        if (e.act === 'ask_human') {
          // 2026-10-25：只要会话**现在**在等人，就应该弹窗 —— 不管这条是历史还是刚发生的。
          // 旧逻辑"历史不弹"的前提是"那件事早就过去了"，但 needs_human 状态说明
          // **它现在还在等** —— 人打开会话第一眼就该看到问题。
          _pendingAsk = e;
          finalizeThink(); closeProcFold();
          seen = Math.max(seen, e.seq || 0);
          return;
        }
        // ★ 每来一条工具调用，就把"它现在在干什么"写进思考块顶部。
        //   数据源用工具调用而不是另调模型：模型自己不知道自己在第几步，
        //   而工具名 + 参数是准的。WorkBuddy 那种把「深度思考」换成具体动作，就是这条路。
        // ★ 派工单（2026-10-26）：派出去的每一趟立一张卡，不混进过程流 ——
        //   混进去就分不清"谁做的"，而并行一开，这个问题会立刻变成主要问题。
        // ★ 闸门拦下来 ⇒ 立一张卡（2026-10-26）。它和 ask_human 不是一回事：
        //   问的是"这一步能不能做"，所以除了文字还要给**按钮**。
        //   没有签名（脚本读不出来）就只给解释、不给按钮 —— 按钮点了也没用。
        if (e.act === 'needs_human' && e.blocked_by) {
          mountBlock(e);
          seen = Math.max(seen, e.seq || 0);
          return;
        }
        if (e.act === 'spawn' || e.act === 'sub_end') {
          if (e.act === 'spawn') mountSpawn(e); else mountSubEnd(e);
          seen = Math.max(seen, e.seq || 0);
          return;
        }
        // ★ 谁在答 / 换过没有（2026-10-26）：进横幅，不进过程块 ——
        //   理由见 mountModel 上面那段注释。
        if (e.act === 'model' || e.act === 'model_switch') {
          mountModel(e);
          seen = Math.max(seen, e.seq || 0);
          return;
        }
        if (e.act === 'tool') {
          // ★ 工具一出现，当前的正文/思考块**封口**（2026-10-29，抄 WorkBuddy）。
          //   不封口的话，工具前后的两段话会粘成一坨，
          //   看起来"它一口气说了很多"，而中间其实隔着一次工具调用。
          sealLive();
          mountTool(e);            // 并进当前那一组「过程」
          tallyEvent(e);
          seen = Math.max(seen, e.seq || 0);
          return;
        }
        // ★ 规划者/其他角色单独推上来的思考（2026-10-29）。
        //   原来只有执行者的 llm.reasoning 能显示，规划者在 think.txt 里想的那段
        //   只表现为"空白里偶尔闪一下" —— 用户的原话是「我完全不知道这个流程还有没有在动」。
        if (e.act === 'think') {
          appendThink(e.text, e);
          seen = Math.max(seen, e.seq || 0);
          return;
        }
        if (e.act === 'llm') {
          // ★ 定稿到了 ⇒ 对应的思考增量块让位，否则同一段思考会显示两遍。
          dropLiveForFinal('think', String(e.role || 'executor'));
          // ★ 一条 llm 事件 = 上一轮想完了、下一轮开始 ⇒ 秒表重置。
          //   这样显示的永远是"**这一轮**已经想了多久"，不是从开会话算起的总时长。
          _selfSince = Date.now();
          appendThink(e.reasoning, e);     // 单独成一小块，标题写「在琢磨什么」
          tallyEvent(e);
          paintPill();
          seen = Math.max(seen, e.seq || 0);
          return;
        }
        // 它开始说话 / 这一轮要收尾 ⇒ 思考合一块收起、过程收起
        if (e.act === 'say') {
          // ★ 定稿到了 ⇒ 对应的正文增量块让位；只封口不替换会同时显示两份。
          dropLiveForFinal('say', String(e.role || 'executor'));
          // ★ 定稿的渲染**不在这里**：它由 `renderEvent()` 统一负责。
          //   我一度在这里又加了一份渲染，结果同一段话显示两遍（2026-10-29 长截图）。
          //   改渲染要改 `renderEvent`，不要在分支里另挂一份。
          // 从最近一次 llm 起算 —— 那才是"这一段想了多久"，不是从开会话算起
          _turnMs = _selfSince ? (Date.now() - _selfSince) : 0;
        }
        if (e.act === 'say') { _turnHadSay = true; }
        if (e.act === 'user') { _turnHadSay = false; detachProc(); }
        if (e.act === 'user') { _finalArchived = false; _pendingArchive = false; resetThinkPanel(); }
        if (e.act === 'say' || e.act === 'end') { finalizeThink(); closeProcFold(); }
        // ★ 收尾 ⇒ 自我的秒表停。但**本我的秒表可能还在走**（它常在这之后才开跑），
        //   两个秒表各有各的开关 —— 这正是"双线"能看得见的原因。
        if (e.act === 'end') {
          dropLiveKind('say');       // 正文只留定稿；迟到残片不许越过结束线
          if (_selfSince) _lastSelfMs = Date.now() - _selfSince;   // 留下脚印
          _selfSince = 0;
          // ★★ 这一轮已经收尾 ⇒ 之前那个提问**不再是"等着人回答"**（2026-10-27 修的真 bug）。
          //   原来 `_pendingAsk` 只在"人回答 / 手动关弹窗"时清，于是两天前的一个提问
          //   会一直挂在状态标签上写着「等你回话」；而弹窗那条路要求状态是 needs_human，
          //   现在状态是 error ⇒ 不弹。人看到的就是"它说在等我，可它压根没问我"，
          //   而且没有回答的入口 —— 那句话既不真，也无路可走。
          //   ★ `needs_human` 是唯一例外：它本身就是"因为问了才停"，
          //     清掉等于把还没答的问题弄丢了。
          if (_pendingAsk && String(e.status || '') !== 'needs_human') { dropAsk(); }
          paintPill();
        }
        const html = renderEvent(e);
        if (html) {
          // ★★ 连续相同的一行不重复插。
          //   实测：一个 13 轮的会话，"总纲 13993 字 · 本轮跳过注入" 那行出现了 **13 次**，
          //   每次内容一模一样。它把执行流切成一截一截的，人看到的是"很碎、很粘"。
          //   这不是信息，是流水账 —— 状态没变就不该再占一行。
          //   只对纯文本行（.line）去重：正文里两段一样的话是真事，不能吞。
          if (html.startsWith('<div class="line">') && html === _lastLineHtml) {
            seen = Math.max(seen, e.seq || 0);
            return;
          }
          _lastLineHtml = html.startsWith('<div class="line">') ? html : '';
          // ★★★ 只有"谁说了什么"才进流（2026-10-27 做减法）。
          //   样本：6 个会话里 say 7 条，而 tool 101 / charter 79 / llm 79 条。
          //   也就是说流里 97% 的节点是过程，正文被埋掉 —— 用户原话
          //   「我根本就看不到任何正文」「契约、停下来问人…一堆输出，有用信息为 0」。
          //   ⇒ 契约、校验、注入记录、钩子、记忆候选 全部进**过程容器**，
          //     流里只留 user / say（以及本我那一条旁注）。
          //     想看细节点开那一行就行，信息一点没少。
          if (PROSE_ACTS.has(e.act)) feedPutHTML(html);
          else if (e.act === 'distill') {
            // ★ 记忆候选不是"这一轮做了什么"，是"等你点头"。它常常在 end 之后才到，
            //   于是会另起一个过程块。给它一句说清摘要，然后收起来 ——
            //   待办不能藏死，但也不该占正文的地方（2026-10-27 做减法）。
            const _f = procAppendTail(html);
            const _n = (e.items || []).length;
            if (_f) {
              const _s = _f.querySelector('.psum');
              // ★ 追加，不是覆盖：这个容器里可能已经有工具动作和拦截卡，
              //   把摘要整个换掉等于把前面做过的事从标题上抹掉（2026-10-27 实测）。
              if (_s && !_s.textContent.includes('记忆候选')) {
                _s.textContent = (_s.textContent || '过程')
                  + ' · 记忆候选 ' + _n + ' 条';
              }
              // ★ 容器里还有拦截卡时不许收（收起等于把按钮藏起来，而屏幕上
              //   只剩一句「等你决定」，人找不到点哪儿 —— 用户报的就是这个）。
              if (!_pendingBlock && !_f.querySelector('.blk')) {
                _f.classList.remove('open');
              }
            }
          } else procAppend(html);
          // ★ 高亮：只处理**新增**的块（打 data-hl 标记），不重扫全文档 ——
          //   否则每 500ms 轮询都要把历史代码块重新高亮一遍，越用越卡。
          //   就绪之前先跳过，下一轮自动补上（shiki 初始化是异步的，要加载 wasm）。
          highlightNew();
        }
        tallyEvent(e);
        // 执行者 end 后还可能有测试者；先登记，等整个进程停了再归档。
        if (e.act === 'end' && e.status === 'success') _pendingArchive = true;
        else if (_finalArchived && !PROSE_ACTS.has(e.act)) archiveThoughtFlow(false);
      } catch (err) {
        console.error('渲染这条事件失败', e, err);
        const fallback = document.createElement('details');
        const title = document.createElement('summary');
        title.textContent = '事件 #' + e.seq + ' 显示失败，展开查看原始记录';
        const raw = document.createElement('pre');
        raw.textContent = JSON.stringify(e, null, 2);
        fallback.append(title, raw);
        feed.appendChild(fallback);
      }
      seen = Math.max(seen, e.seq || 0);
      // ★ 以前这里写的是「llm 事件到了就把临时思考块藏掉」—— 那是错的。
      //   llm 事件记的是**上一轮**的完整思考，而临时块显示的是**正在进行的下一轮**
      //   的草稿（on_turn 每轮会清空 think.txt）。所以每来一条 llm 就藏，
      //   等于把下一轮正在写的思考误杀。
      //   实测：一轮多工具任务 13 次 llm ⇒ 临时块只活了 4 秒，
      //   人看到的是"13 段思考整块跳出来"。
      //   ⇒ 不藏。让它自己退：on_turn 清空草稿后，streamTick 发现空自然就隐藏了。
      if (e.act === 'say') {
        _lastSay = (e.text || '').trim();
        // 正式正文已经进执行流了，临时气泡立刻让位 ——
        // 不等下一轮 streamTick（最多 120ms），那边会先看到两段一样的字。
        stHide();
      }
    });
    _stat.status = reduced.status || _stat.status;
    // ★★ 这一批看完了 ⇒ 决定弹不弹（2026-10-26）。
    //   规则（只有一条事实来源：这一批事件本身）：
    //     新发生的提问        ⇒ 一定弹（刚问的，人得看见）
    //     历史提问 + 现在等它  ⇒ 弹（last end 说 needs_human，说明"它还在这一步"）
    //     历史提问 + 已经过去了 ⇒ 不弹（旧会话重开时别拿几天前的问题糊脸）
    //   `endStatus` 为空时按"还在等"处理：那是"进程停在 ask_human 之后、end 之前"，
    //   或者这一批只截到了提问 —— 两种都意味着它现在确实没在往下走。
    if (reduced.ask && !body.has_more) {
      const _st = reduced.endStatus || _stat.status || '';
      if (!reduced.askStale && (!_histBatch || _st === 'needs_human' || !_st)) {
        showAsk(reduced.ask);
      } else if (reduced.askStale) {
        // 过期了就**把弹窗收掉**：不收的话，人切走再切回来会看见一个旧问题，
        // 而它对应的那一轮早就往下跑了。
        dropAsk();
      }
    }
    // 只在人本来就在底部时才跟着滚 —— 他往上翻看历史的时候拽回去很烦
    if (atBottom) feed.scrollTop = feed.scrollHeight;
  }
  if (status === 200 && body.has_more) { setTimeout(poll, 0); return; }
  if (status === 200) _booting = false;
  // ★ 子进程的原样输出单独放抽屉，**不塞进执行流**。
  //   之前把它当事件追加，结果每轮的启动信息（档位 / 压缩阈值 / 会话路径 / 模型名）
  //   变成一大坨红字挤在执行流里 —— 那些是正常的，不是错误。
  //   现在抽屉默认收起；只有「一条事件都没有」时才自动展开，因为那意味着没跑起来。
  const lr = await api('/sessions/' + encodeURIComponent(sid) + '/log');
  if (cur !== sid || _viewVersion !== viewVersion || lr.status !== 200) return;
  const lt = lr.body.log || '';
  if (lt !== lastLog) {
    lastLog = lt;
    $('logbody').textContent = lt;
    if (!(body.events || []).length && lt.trim() && $('logwrap')) $('logwrap').open = true;
  }

  // running 来自服务端进程表：执行者、测试者及收尾全部结束后才为 false。
  // 历史会话重放时它本来就是 false，因此会在同一批事件后完成归档。
  if (_pendingArchive && !running) {
    archiveThoughtFlow(true);
    _pendingArchive = false;
  }
}

// ── 渲染：对齐 Codex 的五类内容 ──
// 用户消息 / 思考（折叠）/ 工具调用（卡片）/ 回复 / 结论。
// 状态栏放模型、累计 token、门禁 —— Codex 底部也有一条。
// ★ 为什么不再平铺成一行行：那样"它说了什么"和"它跑了什么命令"混在一起，
//   读的人分不清哪句是结论、哪句是过程。Codex 用结构解决这件事，抄它就行。

// 工具名的中文说法。★ 界面上不该出现 read_file / run_check 这种词 ——
// 那是给我看的，不是给用的人看的。原名留在 title 里，技术用户悬停能看到。
const TOOL_CN = {
  read_file: '读文件', write_file: '写文件', edit_file: '改文件',
  run_check: '运行检查', run_acceptance: '跑验收', recent_runs: '查最近几轮',
};
const cn = t => TOOL_CN[t] || t || '';

// ── 极简 Markdown ──
// ★ 为什么不引库：这是个离线桌面应用，引一个第三方渲染器要么打包进来（大），
//   要么走 CDN（断网就废）。而对话里出现的 markdown 就那几种，
//   自己写 60 行，可控、可读、出问题能改。
// ★ 顺序很要紧：先把代码块和行内代码抠成占位符，再转义，最后替换。
//   反过来的话，代码里的 `<` 会被当标签、`**` 会被当粗体 —— 而代码里本来就该原样显示。
function mdInline(s) {
  return s
    .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
    .replace(/__([^_\n]+)__/g, '<b>$1</b>')
    .replace(/(^|[^*\w])\*([^*\n]+)\*/g, '$1<i>$2</i>')
    // ★ url 里不许有括号：`[点我](javascript:alert(1))` 这种嵌套括号会让贪婪匹配
    //   切错位置、把半个括号留在正文里。不许括号的代价是极少数含括号的链接会原样显示，
    //   换来的是"匹配不到就保持纯文本"——安全的那一侧。
    .replace(/\[([^\]\n]+)\]\(([^()\s]+)\)/g, (m, txt, url) => {
      // 只放行 http/https/mailto —— 别让模型输出一个 javascript: 链接
      return /^(https?:|mailto:)/i.test(url)
        ? `<a href="${url}" target="_blank" rel="noreferrer">${txt}</a>`
        : txt;
    });
}

// GFM 表格：第一行表头，第二行是 |---|---| 分隔（可以有对齐冒号，这里忽略）
function renderTable(rows) {
  const cells = r => r.trim().replace(/^\|/, '').replace(/\|$/, '')
    .split('|').map(x => x.trim());
  if (rows.length < 2) return `<p>${mdInline(rows[0] || '')}</p>`;
  const head = cells(rows[0]);
  const body = rows.slice(2).map(cells);
  return '<table><thead><tr>'
    + head.map(c => `<th>${mdInline(c)}</th>`).join('')
    + '</tr></thead><tbody>'
    + body.map(r => '<tr>' + r.map(c => `<td>${mdInline(c)}</td>`).join('') + '</tr>').join('')
    + '</tbody></table>';
}

function mdToHtml(src) {
  if (!src) return '';
  let s = String(src).replace(/\r\n/g, '\n');
  let tableBuf = [];

  // ① 代码块先抠出来（里面的内容不参与任何后续处理）
  const blocks = [];
  s = s.replace(/```([^\n`]*)\n([\s\S]*?)(?:```|$)/g, (m, lang, code) => {
    blocks.push({ lang: (lang || '').trim(), code });
    return '\u0000B' + (blocks.length - 1) + '\u0000';
  });

  // ② 行内代码同理
  const codes = [];
  s = s.replace(/`([^`\n]+)`/g, (m, code) => {
    codes.push(code);
    return '\u0000C' + (codes.length - 1) + '\u0000';
  });

  // ③ 转义（此后 s 里的 < > & 都是安全的）
  s = esc(s);

  // ④ 逐行处理块级。每行一个块，不做嵌套 —— 嵌套容易写错，而对话里很少用到
  const out = [];
  let list = null;                      // 'ul' | 'ol' | null
  const closeList = () => { if (list) { out.push('</' + list + '>'); list = null; } };
  for (const raw of s.split('\n')) {
    const ln = raw.trim();
    if (!ln) { closeList(); continue; }

    let m;
    // ★ 整行就是一个代码块占位符 ⇒ 直接放行，**不能包 <p>**：
    //   <p><div> 是不合法嵌套，浏览器会自己闭合 p，把后面的排版全带歪。
    if (/^\u0000B\d+\u0000$/.test(ln)) { closeList(); out.push(ln); continue; }
    if ((m = /^(#{1,4})\s+(.*)$/.exec(ln))) {
      closeList();
      const n = m[1].length;
      out.push(`<h${n}>${mdInline(m[2])}</h${n}>`);
      continue;
    }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(ln)) { closeList(); out.push('<hr>'); continue; }
    // ★★ 表格：连续以 | 开头的行聚成一块。
    //   原来**只有 CSS 没有解析器** —— 所以 markdown 表格原样显示成 `| a | b |`
    //   一排竖线，用户看到的就是"一堆字挤在一起"。
    if (/^\s*\|.*\|\s*$/.test(ln)) {
      closeList();
      tableBuf.push(ln);
      continue;
    }
    if (tableBuf.length) { out.push(renderTable(tableBuf)); tableBuf = []; }
    if ((m = /^[-*+]\s+(.*)$/.exec(ln))) {
      if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; }
      out.push(`<li>${mdInline(m[1])}</li>`);
      continue;
    }
    if ((m = /^\d+[.)]\s+(.*)$/.exec(ln))) {
      if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; }
      out.push(`<li>${mdInline(m[1])}</li>`);
      continue;
    }
    if ((m = /^&gt;\s?(.*)$/.exec(ln))) {
      closeList();
      out.push(`<blockquote>${mdInline(m[1])}</blockquote>`);
      continue;
    }
    closeList();
    out.push(`<p>${mdInline(ln)}</p>`);
  }
  closeList();
  if (tableBuf.length) out.push(renderTable(tableBuf));
  s = out.join('\n');

  // ⑤ 把占位符换回来
  s = s.replace(/\u0000C(\d+)\u0000/g, (m, i) => `<code>${esc(codes[+i])}</code>`);
  s = s.replace(/\u0000B(\d+)\u0000/g, (m, i) => {
    const b = blocks[+i];
    const lang = b.lang ? ` data-lang="${esc(b.lang)}"` : '';
    return `<div class="codebox"${lang}><button class="copybtn" title="复制">复制</button>`
         + `<pre><code${b.lang ? ` class="language-${esc(b.lang)}"` : ""}>`
         + `${esc(b.code)}</code></pre></div>`;
  });
  return s;
}

// ★ 收尾总结：这一轮到底做了什么。
//   用户的原话是「我想知道他做了什么得一步步翻思考过程」——
//   事件流能证明每一步，但人不想读流水账；所以要给一份算过的摘要。
function renderSummary() {
  const files = [], cmds = [];
  _tools.forEach(x => {
    if (/read_file|write_file|edit_file/.test(x.tool)) { if (x.arg) files.push(x.arg); }
    else if (x.tool) cmds.push(x);
  });
  const uniq = [...new Set(files)];
  // ★ 只显示**文件名**，不显示整条路径。
  //   原来 8 条完整路径 = 4 行 `C:\Users\Administrator\WorkBuddy\agent-harness\...`，
  //   读的人一个都记不住，还把这一轮真正做了什么挤到看不见。全文放 title。
  // ★★ 用户：「比如这一轮做了什么，只保留 token 消耗就行，别的用户也不关心」。
  //   原来这里列了"碰过 16 个文件：app-README.md live run-t accept benni-int …"
  //   和"313 次检查/命令：recent_runs la, C://Users//..."—— 一屏全是路径碎片，
  //   既读不完也没人看。现在只留 token；明细全部进 title（想看的人悬停）。
  const detail = [];
  if (uniq.length) detail.push('碰过 ' + uniq.length + ' 个文件：' + uniq.join(' '));
  if (cmds.length) detail.push(cmds.length + ' 次检查/命令');
  if (!_tools.length) detail.push('这一轮没有调用任何工具，只是回答');
  return `<div class="summary" title="${esc(detail.join('｜'))}">`
       + (detail.length ? `<span class="summin">${esc(detail[0].split('：')[0])}</span>` : '')
       + `</div>`;
}

// ── 本我复核：结构化 + 按锚点挂批注 ──
// review.py 的输出本来就有结构：
//   ① 对得上吗 ② 我看到的 ③ 我怀疑的（每条带「证据 / 复核 / 锚点：第 N 步」）④ 我没查的 ⑤ 我提醒的
// ★ 之前我把 ①②③ 压成一行行，把 ③ 里的子条目全拼成一坨 —— 结构没了，人也就不看了。
// ★ 「锚点：第 N 步」是挂批注的抓手：第 N 步 = 第 N 次工具调用，能对到具体那张卡片上。
function parseBenni(raw) {
  const clean = String(raw || '').replace(/^<!--[\s\S]*?-->\s*/, '');
  const seg = {};                       // ①②③ → 文本
  let curKey = '';
  for (const ln of clean.split('\n')) {
    const m = /^([①②③④⑤⑥⑦⑧⑨])\s*(.*)$/.exec(ln.trim());
    if (m) { curKey = m[1]; seg[curKey] = (seg[curKey] ? seg[curKey] + '\n' : '') + m[2]; }
    else if (curKey) seg[curKey] += '\n' + ln;
  }
  // ③ 里按「- 」分条，每条抽锚点
  const items = [];
  if (seg['③']) {
    let cur = null;
    for (const ln of seg['③'].split('\n')) {
      if (/^\s*[-·]\s+/.test(ln)) {
        if (cur) items.push(cur);
        cur = { text: ln.replace(/^\s*[-·]\s+/, '').trim(), anchor: null, rest: [] };
      } else if (cur && ln.trim()) {
        const ma = /锚点[：:]\s*第\s*(\d+)\s*步/.exec(ln);
        if (ma) cur.anchor = parseInt(ma[1], 10);
        else if (/^\s*(证据|复核)[：:]/.test(ln)) cur.rest.push(ln.trim());
        else cur.rest.push(ln.trim());
      }
    }
    if (cur) items.push(cur);
  }
  return { seg, items };
}

function renderAnnotation(it, n, quote) {
  // ★ data-quote：它在正文里批的是哪一句（逐字片段）。drawNoteLines 拿它定位、画线。
  const q = String(quote || it.quote || '').replace(/"/g, '&quot;');
  // ★★ 只渲染**一句话**。证据/复核不再铺在界面上 —— 用户：
  //    「这个也需要极简化」「信息密度依旧读不下去」。
  //    但它们不能删（可复核性是这套东西的根），所以挂进 title：hover 才看得到。
  const detail = (it.rest || []).filter(Boolean).join(' · ');
  return `<div class="benninote"${q ? ` data-quote="${q}"` : ''}`
       + `${detail ? ` title="${esc(detail.slice(0, 300))}"` : ''}>`
       + `<div class="bnhead">复核 · 第 ${n} 步</div>`
       + `<div class="bntext">${esc(it.text)}</div>`
       + `</div>`;
}

// ── 批注的引线：从被批的那句话，连到右边的批注 ──
// 用户的原话：「这个批注是否可以再精准一点，类似我图片中这种，
// 连一根线到具体他批注的位置？」
//
// 怎么定位"哪一句"：批注自带一个 quote（**它在正文里逐字抄下来的片段**），
// 在这里搜出来、包一个 span 标一下，再从那个位置画一条贝塞尔到批注左上角。
// ★ 为什么用片段而不是"第几个字"：正文会重渲染（markdown、代码高亮、思考块收放），
//   偏移量一改就指到别处；**片段是能搜到的**，搜不到就干脆不画。
function _clearQuotes(root) {
  root.querySelectorAll('.bnq').forEach(s => {
    s.replaceWith(document.createTextNode(s.textContent));
  });
  // ★ normalize 必须做：拆出来的文本节点是散的，indexOf 在单个节点里
  //   搜不到跨节点的片段 —— 不合并的话第二次就再也标不上了。
  root.normalize();
}

function _markQuote(root, q) {
  if (!q || q.length < 2) return null;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
  let n;
  while ((n = walker.nextNode())) {
    const i = (n.nodeValue || '').indexOf(q);
    if (i < 0) continue;
    const r = document.createRange();
    r.setStart(n, i);
    r.setEnd(n, i + q.length);
    try {
      const span = document.createElement('span');
      span.className = 'bnq';
      r.surroundContents(span);
      return span.getBoundingClientRect();
    } catch (e) {
      // 片段跨了元素（一半在 <b> 里之类）—— 标不了，但位置还能用
      return r.getBoundingClientRect();
    }
  }
  return null;
}

function drawNoteLines(turn) {
  if (!turn || !turn.isConnected) return;
  const slot = turn.querySelector('.bncol');
  const bodyEl = turn.querySelector('.msg .body');
  if (!slot || !bodyEl) return;

  let svg = turn.querySelector('.bnsvg');
  if (!svg) {
    svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'bnsvg');
    turn.appendChild(svg);
  }
  svg.innerHTML = '';
  _clearQuotes(bodyEl);

  const tb = turn.getBoundingClientRect();
  const bb = bodyEl.getBoundingClientRect();

  const notes = [...slot.querySelectorAll('.benninote')];
  notes.forEach(n => {
    // ★★ 没有 quote 就从批注正文里**抠一个能定位的锚点**。
    //   为什么必须这么做：旧会话里的 benni 事件是加 quote 之前写的，
    //   items 里没有 quote —— 那些批注永远画不出线，而人看到的是
    //   "批注一大堆、一条线都没有"。三轮都没解决，根子在这里。
    //   ⇒ 退一步：从"它说改过 bin/contract.py"里抠出 bin/contract.py 去正文里搜。
    //     搜到照样高亮 + 连线；搜不到才真的没有。
    let q = n.dataset.quote || '';
    if (!q) {
      const txt = (n.querySelector('.bntext') || {}).textContent || '';
      const m = txt.match(/[\w./\\-]+\.(?:py|js|cjs|mjs|ts|html|css|md|json|yml|yaml|txt)/);
      if (m) q = m[0];
    }
    if (!q) return;
    const r = _markQuote(bodyEl, q);
    if (!r) return;
    const nb = n.getBoundingClientRect();
    // ★ 起点放在**这一行的下沿**（行间隙），不是字的中间 ——
    //   从中间出发，斜线必然穿过同一行后面的字。用户：
    //   「哪怕不会挡他链接的字本身，他链接过程中也会穿透一些字体。
    //     实在不行让他先从行之间的缝隙里出来，然后在右边没有正文的地方再找归属」。
    const x1 = Math.round(r.right - tb.left + 5);
    const y1 = Math.round(r.bottom - tb.top + 2);
    const x2 = Math.round(nb.left - tb.left - 5);
    const y2 = Math.round(nb.top - tb.top + 13);
    // ① 先在行间隙里水平走一小段（不碰任何字），② 再从那里弯到批注左上角
    const run = Math.min(30, Math.max(10, Math.round((x2 - x1) * .18)));
    const mid = Math.max(14, Math.round((x2 - x1 - run) / 2));
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', `M ${x1} ${y1} L ${x1 + run} ${y1} `
      + `C ${x1 + run + mid} ${y1}, ${x2 - mid} ${y2}, ${x2} ${y2}`);
    svg.appendChild(p);
    const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    c.setAttribute('cx', x1);
    c.setAttribute('cy', y1);
    c.setAttribute('r', 2.2);
    svg.appendChild(c);
  });

  // ★ 批注是绝对定位的，不占高度：批注比正文长时会溢到下一轮上面。
  //   这里把 turn 的最小高度撑到批注的高度。
  const need = slot.offsetHeight + 10;
  if (need > turn.clientHeight) turn.style.minHeight = need + 'px';
}

// 窗口大小变了，线就指错地方了 —— 重画（节流，别每像素算一次）
let _redrawT = null;
window.addEventListener('resize', () => {
  clearTimeout(_redrawT);
  _redrawT = setTimeout(() => {
    document.querySelectorAll('#feed .turn').forEach(drawNoteLines);
  }, 160);
});

// 过程批注：一句话 + 一条线，连到它批的那句上。
// ★ 它属于**执行层**（自检），不是测试层 —— 所以标签写「自检」不写「测试者」。
function mountCheck(e) {
  const items = e.items || [];
  if (!items.length) return;
  const turns = $('feed').querySelectorAll('.turn');
  let turn = null;
  for (let i = turns.length - 1; i >= 0; i--) {
    if (turns[i].querySelector('.bncol')) { turn = turns[i]; break; }
  }
  if (!turn) return;
  const slot = turn.querySelector('.bncol');
  // ★ quote 必须在**生成 HTML 时**就写进 data-quote。
  //   原来是插入之后按索引回填，一旦有过"超过 6 条、删掉旧的"这一步，索引就偏了 ——
  //   回填到别人身上、或者填不上，而**界面看不出任何异常**：批注在，线没了。
  // ★ 一行一条，不带"依据"：批注比正文还长是本末倒置，依据留在收尾那份复核里。
  const html = items.map(it =>
    `<div class="benninote live"${it.quote ? ` data-quote="${esc(it.quote)}"` : ''}>`
    + `<div class="bnhead">自检</div>`
    + `<div class="bntext">${esc(it.text || '')}</div>`
    + `</div>`).join('');
  slot.insertAdjacentHTML('beforeend', html);
  // 只留最近 6 条：过程中看的是"眼下有没有偏"，不是攒一本账
  const all = slot.querySelectorAll('.benninote.live');
  for (let i = 0; i < all.length - 6; i++) all[i].remove();
  drawNoteLines(turn);
}


// ── 流式增量（2026-10-29，参考 WorkBuddy 的 ACPMessageAccumulator）──
//
// ★ 正文和思考是两种不同的 chunk，但**在同一条流里**（同一个 seq 序列）。
//   所以"思考显示了、正文没跟上"这类错位在结构上不可能发生。
//
// ★ 三条规则，全部抄自 WorkBuddy：
//   ① 同类连续 ⇒ 接着写进当前那块；类型/轮次一变 ⇒ 新开一块
//   ② **工具一出现 ⇒ 当前文本块封口**（`sealLive`）
//   ③ 定稿事件（`llm` / `say`）来了 ⇒ 用完整内容替换，增量块让位
const _blocks = [];                 // [{kind, role, el, text}] —— contentBlocks 有序数组
let _lastThinkIdx = -1, _lastSayIdx = -1;

function resetLiveBlocks() {
  // clearFeed 只清 DOM 不够：旧数组会在下一次 _paintLive 时把上个会话贴回来。
  _blocks.splice(0);
  _lastThinkIdx = -1;
  _lastSayIdx = -1;
}
// ★ 三种角色都要能进正文流（2026-10-29 用户：「say 里面不止有执行者的输出」）。
//   原来只显示"执行者"，规划者和测试者的话没有位置。
//   角色放在标签上，正文按时间顺序排 —— 和 WorkBuddy 的 ACP 一致。
const ROLE_CN = { planner: '规划者', executor: '执行者', reviewer: '测试者' };

function sealLive(kind) {
  // ★ 封口 = **索引失效**（照 WorkBuddy：`lastTextIndex = -1`）。
  //   不删块、不改数组 —— 下一段文本会新开一项 push 到数组末尾。
  if (!kind || kind === 'say') _lastSayIdx = -1;
  if (!kind || kind === 'think') _lastThinkIdx = -1;
}

// 定稿（llm/say）和增量（chunk）是同一段内容的两种状态，不是两条话。
// 定稿到了要**替换**对应的临时块；只封口不删除，就会一份流式、一份定稿同时显示。
function _syncRound(round) {
  if (!round || !round.isConnected) return;
  const body = round.querySelector('.thinkbody');
  const n = body ? body.children.length : 0;
  const t = round.querySelector('.tpsummary');
  const summaries = body ? [...body.querySelectorAll('.psum')] : [];
  const concrete = summaries.map(el => el.textContent.trim()).filter(text =>
    text && !['过程', '正在处理…'].includes(text));
  if (t) t.textContent = concrete.at(-1) || '正在回应';
  // ★ 空的就藏起来（2026-10-31 重放实测）：临时思考块会在定稿时被删掉，
  //   留下一个空容器 —— 不藏的话正文上面会挂一条没有内容的「思考过程」。
  round.hidden = n === 0;
}

// 临时块被删掉之后，外层容器可能就空了：顺手把它的标题藏掉。
function _hideIfEmptyRound(body) {
  if (!body || !body.classList || !body.classList.contains('thinkbody')) return;
  if (body.children.length) return;
  _syncRound(body.closest('details.thinkblock'));
}

// 正文已经出现 = 这一轮的思考写完了。此时仍然空着的容器**永远不会再有内容**
// （后面来的思考会按新的一轮另起一块），留着只会变成一条没有内容的
// 「思考过程」标题。所以就地收走，并把当前轮指针清掉（2026-10-31 实测：
// 一段正文后面能挂两个容器，其中一个是空的）。
function _pruneEmptyRounds() {
  const feed = $('feed');
  if (!feed) return;
  for (const d of [...feed.querySelectorAll(':scope > details.thinkblock')]) {
    const body = d.querySelector('.thinkbody');
    if (body && body.children.length) continue;
    if (d === _thinkRound) {
      _thinkRound = null;
      _roundHadSay = false;
      _thinkAutoFolded = false;
    }
    d.remove();
  }
}

// 只用对应角色的定稿替换流式内容，保留同轮其他角色或其余种类片段。
function dropLiveForFinal(kind, role) {
  for (let i = _blocks.length - 1; i >= 0; i--) {
    const b = _blocks[i];
    if (b.kind !== kind || b.role !== role) continue;
    const par = b.el.parentElement;
    if (b.el.isConnected) b.el.remove();
    _blocks.splice(i, 1);
    _lastThinkIdx = -1;
    _lastSayIdx = -1;
    _hideIfEmptyRound(par);
    return;
  }
}

function dropLiveKind(kind) {
  // 结束时不该还有正文草稿。旧后端留下的迟到残片也在这里清掉；
  // 思考不动：某些角色只有 chunk、没有 llm 定稿，删掉会丢原文。
  for (let i = _blocks.length - 1; i >= 0; i--) {
    const b = _blocks[i];
    if (b.kind !== kind) continue;
    const par = b.el.parentElement;
    if (b.el.isConnected) b.el.remove();
    _blocks.splice(i, 1);
    _hideIfEmptyRound(par);
  }
  if (kind === 'say') _lastSayIdx = -1;
  if (kind === 'think') _lastThinkIdx = -1;
}

function _newBlockEl(kind, role) {
  const who = ROLE_CN[role] || role;
  const el = document.createElement('div');
  el.dataset.role = role;
  const _dp = (typeof DP !== 'undefined' && DP.get) ? DP.get('think') : 'collapse';
  el.className = (kind === 'think')
    ? ('reasoning live-think' + (_dp === 'expand' ? '' : ' collapse'))
    : 'msg live-say';
  if (kind === 'think' && _dp === 'hide') el.style.display = 'none';
  el.innerHTML = (kind === 'think')
    ? '<div class="rhead"><span class="ricon">◌</span><span class="rt">思考</span>'
      + '<span class="rm">' + esc(who) + '</span></div><div class="rbody"></div>'
    : '<div class="shead">' + esc(who) + '</div><div class="mbody"></div>';
  const _hd = el.querySelector('.rhead');
  if (_hd) _hd.addEventListener('click', () => el.classList.toggle('collapse'));
  return el;
}

function mountDelta(e) {
  const kind = (e.kind === 'think') ? 'think' : 'say';
  const role = String(e.role || 'executor');
  const txt = String(e.text || '');
  if (!txt) return;
  const idx = (kind === 'think') ? _lastThinkIdx : _lastSayIdx;
  const cur = idx >= 0 ? _blocks[idx] : null;
  // ★ 同类同角色 ⇒ **接着写进当前那块**（不新建）。
  //   这就是 WorkBuddy 的"同类连续就接着写"：一次回答里可以有多次模型调用，
  //   但它们属于**同一段思考/正文**，合在一块，而不是每次调用弹一块。
  if (cur && cur.role === role) {
    cur.text += txt;
  } else {
    const el = _newBlockEl(kind, role);
    _blocks.push({ kind, role, el, text: txt });
    if (kind === 'think') _lastThinkIdx = _blocks.length - 1;
    else _lastSayIdx = _blocks.length - 1;
  }
  _scheduleLivePaint();
}

let _livePaintScheduled = false;
function _scheduleLivePaint() {
  if (_livePaintScheduled) return;
  _livePaintScheduled = true;
  // ★ 不用 requestAnimationFrame（窗口不可见时不执行，会留下空块）。
  setTimeout(() => { _livePaintScheduled = false; _paintLive(); }, 16);
}

// 按本轮流式块的次序绘制待定稿片段，替换时尽量保留原节点。
function _paintLive() {
  const feed = $('feed');
  if (!feed) return;
  const follow = _stickBottom;   // 只在**人此刻贴底**时跟随（2026-10-31：吸力改只在底部生效）
  for (const b of _blocks) {
    const body = b.el.querySelector(b.kind === 'think' ? '.rbody' : '.mbody');
    if (body && body.textContent !== b.text) body.textContent = b.text;
    if (body && body.scrollHeight > body.clientHeight) body.scrollTop = body.scrollHeight;
    // ★ 位置**只在新建时**插一次，之后只更新文字。
    //   思考进折叠块、正文进 feed —— 从写入那一刻就分开，不靠事后搬运。
    if (!b.el.isConnected) {
      if (b.kind === 'think') {
        const host = procHost();
        if (host) host.appendChild(b.el);
        // ★ 往哪个块里写，就同步哪个块（不一定是 _thinkRound）：否则块被写过之后
        //   仍停在 hidden，思考就白写了。
        _syncRound(host && host.closest ? host.closest('details.thinkblock') : null);
      } else if (typeof feedPut === 'function') {
        feedPut(b.el);
      } else {
        feed.appendChild(b.el);
      }
    }
    if (b.kind === 'say') foldThinkPanel();      // 正文一出，思考整块收起
  }
  if (_thinkRound && _thinkRound.open) {
    const tb = _thinkRound.querySelector('.thinkbody');
    if (tb) tb.scrollTop = tb.scrollHeight;   // 展开时跟着最新思考走
  }
  if (follow) feed.scrollTop = feed.scrollHeight;
}

// ── 兜底：不认识的 act 也要显示（2026-10-29）──
//
// ★ 为什么必须有它：盘点下来后端发 44 种事件，前端只认 23 种，
//   而**没接的那 21 种几乎全是「出事了才发」的** ——
//   `session_save_failed`（话没存下来）、`snapshot_failed`（副本没建成）、
//   `compact`（前面的对话被压缩了）、`verify_unmet`（判据没满足）……
//   原来的行为是 `return` 直接跳过 ⇒ **出事了，界面上什么都没有**。
//
// ★ 这条兜底不是"把它们做漂亮"，是**让它们不再凭空消失**。
//   要好看可以之后单独做；"看不见"和"不好看"是两种完全不同的问题。
//
// ★ 白名单是"有专门渲染"的那批，不是"后端会发哪些"的清单 ——
//   后端加新事件时**不用改这里**，新事件自动走兜底。
const _KNOWN_ACTS = new Set([
  'model', 'prep', 'charter', 'llm', 'tool', 'tools_batch', 'say', 'verify',
  'end', 'changes', 'benni', 'check', 'ask_human', 'distill', 'contract',
  'spawn', 'sub_end', 'status', 'needs_human', 'hook', 'think', 'user',
  'denied', 'deny', 'never', 'delete', 'pin', 'rename', 'ttl', 'model_switch', 'delta',
  'agent_message_chunk', 'agent_thought_chunk',
]);

function mountUnknown(e) {
  const act = String(e.act || '?');
  // 只挑**人看得懂**的字段：错误、原因、说明。别把整个 JSON 倒出来。
  const bits = [];
  for (const k of ['error', 'reason', 'note', 'text', 'summary', 'why', 'tool', 'path']) {
    const v = e[k];
    if (v === undefined || v === null || v === '') continue;
    bits.push(String(v).slice(0, 120));
    if (bits.length >= 2) break;
  }
  const line = bits.length ? (act + '：' + bits.join(' ｜ ')) : act;
  procAppend(`<div class="line unk"><span class="tag">${esc(act)}</span> ${esc(bits.join(' ｜ '))}</div>`);
}

function mountBenni(e) {
  if (e.running) {
    // ★ "本我在看"这件事本身要看得见。不然它跑 1~3 分钟，
    //   界面一片安静，人只会得出"本我根本没有"这个结论。
    // ★ 一定要挂「本我」标签（2026-10-25）：这一行原来和上面那个思考块挨着，
    //   而思考块的头是「思考中 · deepseek-flash」—— 人看到的是两个不同主体
    //   的内容贴在一起，分不清"这是自我在想，还是本我在跑"。用户就是这么问的：
    //   「这个位置的思考块确定是自我的模型吗？还是说是本我的模型但是标识写成了自我的？」
    //   ⇒ 主体的名字必须出现在**这一行自己身上**，不能靠上下文推断。
    const _r = String(e.reason || '正在读这一轮的流水…').replace(/^本我/, '');
    // ★ 计时从这一行出现开始 —— 用户要的就是"它还在跑"这件事有可核的参照，
    //   而不是一句静态的"在跑"。本我结果到了（下面）才清零。
    _benniModel = e.model || (typeof _benniModel !== 'undefined' ? _benniModel : '');
    // ★ 秒表**不在这里重置**（2026-10-29）：下面改成"只占一行"之后，
    //   每来一条进度都跑到这里 —— 在这里重置的话秒表永远是 0，
    //   而"它跑了多久"正是人判断"它是不是卡住了"的唯一参照。
    //   起点改在**新建那一行**的时候设（见下面 `if (!_runl)` 里面）。
    if (!_benniSince) _benniSince = Date.now();
    paintPill();
    // ★★ 只占一行，**原地更新文字**（2026-10-29 联调实测）：
    //   后端的复核进度现在是一条一条推上来的（"本我第 1 轮：给了 7 个工具"…），
    //   而这里原来是**每来一条就插一行** —— 一次复核在正文流里堆了 8 行"测试者"，
    //   每行还都带一个秒表。那不是"看得见在动"，那是把正文挤走。
    //   ⇒ 已有这一行就只换文字，没有才新建；秒表从第一次开始算，不重置。
    let _runl = $('feed').querySelector('.bennirun');
    if (!_runl) {
      $('feed').insertAdjacentHTML('beforeend',
        '<div class="line bennirun"><span class="spk spk-b">测试者</span>'
        + '<span class="bnote"></span><span class="bsec"></span></div>');
      _runl = $('feed').querySelector('.bennirun');
      if (_benniSince) _benniSince = Date.now();   // 秒表只从这一行的出现开始算
    }
    const _bn = _runl ? _runl.querySelector('.bnote') : null;
    if (_bn && _r) _bn.textContent = ' · ' + _r;
    return;
  }
  // ★★ 过程批注：挂在**它刚说的那段**右边，并连一条线到它批的那句话。
  //   2026-10-29：这批注的身份从「测试者」改成了「自检」（`act: 'check'`）。
  //   老事件（`benni` + `live: true`）也走这里 —— 重放历史会话时不能两边不一样。
  if (e.live) { return mountCheck(e); }
  if (e.skipped) {
    // ★ 跳过也是一种"跑完了"（只是没跑成）—— 计时必须停，
    //   否则右上角会永远挂着"本我 3:12"不动，比不显示更糟。
    if (_benniSince) _lastBenniMs = Date.now() - _benniSince;
    _benniSince = 0;
    paintPill();
    // ★ 这里原来是整条命令原样铺开（`Command ['C:\...', '--benni-out', ...] timed out`），
    //   五行字，全是路径。人看到的是"一坨"。
    //   现在只留一句人话，全文放 title —— 要看细节可以悬停，不占正文。
    const why = String(e.reason || '');
    // ★ 这句是**过程信息**，不是结论（2026-10-27 用户：收进过程卡里）。
    //   原来直接插进 #feed，于是"本我没跑，因为没轨迹"和正文并排，
    //   读起来像它给出了一个结论 —— 而它只是说"这一环这次没做"。
    //   进过程卡之后：平时收着，想看的人点开就在，流里不再多一行。
    procAppend(
      `<div class="line badline" title="${esc(why)}">测试者没跑 · `
      + `${esc(why.slice(0, 96))}${why.length > 96 ? '…' : ''}</div>`);
    return;
  }
  const _rawBody = e.body || e.text || '';
  // ★ 结论到了 ⇒ 那句「在读这一轮的流水…」就该撤掉。它只是进度提示（等于 loading）。
  //   不撤的后果实测过：结论块插在正文 div 里，而那句是插在 feed 末尾的 ——
  //   于是「在读…」跑到结论**下面**去了，读起来像"结论出完了它才开始读"。
  const _run = document.querySelector('.bennirun');
  if (_run) _run.remove();
  // 本我出结果了 ⇒ 它不再"在跑"。不清零的话，右上角会永远挂着"本我 3:12"，
  // 比不显示更糟（人会以为它卡住了）。
  if (_benniSince) _lastBenniMs = Date.now() - _benniSince;
  _benniSince = 0;
  const { seg, items } = parseBenni(_rawBody);
  // ★ 模型名一直在 body 的来源头里（review.py 写的 `模型：xxx`），
  //   但界面只写"另一家模型" —— 用户问了才知道是谁。抠出来用。
  const _mm = /模型[：:]\s*([^｜|<>\n]{2,40})/.exec(_rawBody);
  const _bmodel = _mm ? _mm[1].trim() : '';
  if (_bmodel) _bModel = _bmodel;    // 下一次它开跑时，右上角就能写出是谁在跑
  paintPill();
  const feed = $('feed');

  // ★★ 两类东西，两个位置 —— 这是用户点破的一处混淆：
  //   「批注怎么跑到 say 的位置了」
  //   我上一版把整份复核（含"本我批注 · 第 N 步"）一起塞进了正文，
  //   于是**锚点批注也在正文里**，而 drawNoteLines 只认 .bncol 里的批注 ——
  //   所以那些批注既没有引线、又占着正文的地方。
  //   ⇒ 锚点批注（一句话，指到第 N 步）走右边、带线；
  //     总体意见（①②④⑤，一段话）进正文 —— 它本来就是"读完全文的总结"。
  const loose = [];
  let railHtml = '';
  let nRail = 0;              // 有几条"带锚点"的怀疑（放在右侧那一步旁边）
  items.forEach(it => {
    if (it.anchor) { railHtml += renderAnnotation(it, it.anchor, it.quote); nRail += 1; }
    else loose.push(it);
  });

  const part = (k, title) => {
    let v = (seg[k] || '').trim();
    if (!v) return '';
    // ★ review.py 输出的「① 对得上吗」这一行里，标题和内容在同一行 ——
    //   parseBenni 会把「对得上吗」也算成内容，于是界面上同一个意思写两遍。
    if (v.startsWith(title)) v = v.slice(title.length).trim();
    if (!v) return '';
    return `<div class="bnsec"><span class="bnk">${k}</span>${esc(title)}`
         + `<div class="bnbody">${esc(v)}</div></div>`;
  };
  let bodyHtml = '';
  if (seg['①'] || seg['②'] || seg['④'] || seg['⑤'] || loose.length) {
    bodyHtml += '<div class="benniblock flowblock"><div class="bennihead">'
      + '<span class="spk spk-b">测试者</span>'
      + esc(_bmodel || '另一家模型') + ' 读了一遍'
          + '<span class="bennisub">它不能判赢，只能说「这里缺什么」</span></div>';
    bodyHtml += part('①', '对得上吗') + part('②', '我看到的');
    // ★ ③ 不许跳号（2026-10-27 截图看出来的）：两条怀疑都带锚点时，它们被放到
    //   右边那一步旁边，正文里就 ② 后面直接跳 ④ —— 读的人以为漏了一条，
    //   而实际上一条都没少。这里补一行"有几条、在哪看"。
    if (nRail) {
      bodyHtml += '<div class="bnsec"><span class="bnk">③</span>我怀疑的'
        + '<div class="bnbody">' + nRail + ' 条，都标在右边对应的那一步上</div></div>';
    }
    bodyHtml += part('④', '我没查的') + part('⑤', '我提醒的');
    if (loose.length) {
      bodyHtml += '<div class="bnsec"><span class="bnk">③</span>我怀疑的（没对上具体步骤的）'
            + '<ul class="bennilist">' + loose.map(it => `<li>${esc(it.text)}</li>`).join('') + '</ul></div>';
    }
    bodyHtml += '</div>';
  }
  if (!railHtml && !bodyHtml) return;

  // ★★ 不依赖 .turn 一定在最后：直接找**最后一个正文**，比找最后一个 turn 稳。
  //   （实测：偶尔 .turn 之后还插了别的节点，于是 bodyEl 拿到 null，
  //    复核那块就被塞到执行流外面、宽度和正文不一样。）
  // ★★★ 但"最后一个 .body"不等于"最后一个正文"（2026-10-25 查出来的真 bug）：
  //   `.body` 这个 class 被**两处**用着 —— 正式消息的正文，和流式草稿 #streaming 里的
  //   容器（`<div class="body md sbody" id="sbody">`）。
  //   而 streamTick 会把 #streaming 挪到 feed 末尾（feedpos）。于是 .pop() 拿到的是
  //   **那个 hidden 的草稿框**，本我块被插进去 → 人永远看不见。
  //   症状特别有欺骗性：事件流里明明有 `benni ok=true`（本我跑了 71 秒、出了结论），
  //   界面上收尾之后什么都没有 —— 看起来像"这轮本我没话说"。
  //   ⇒ 排除掉草稿容器。**同一个名字指两件事，是这个项目最贵的坑，这是第 N 次。**
  // ★★★ 选择器漏了"谁说的"（2026-10-27 用户报的原话：「为什么用户的输出里面会有本我」）。
  //   原来写的是 `querySelectorAll('.msg .body')`，而**用户消息也是 `.msg .body`**
  //   （见 renderEvent 的 user 分支）。于是本轮自我没说话时，`pop()` 拿到的是
  //   **用户那条气泡** —— 本我就被插进用户消息里，读起来像用户自己写了这段复核。
  //   ⇒ 三件事一起做：① 只认 `.msg.agent .body`；② 必须属于**本轮**（用 seq 卡）；
  //     ③ 本轮没有正文时**新建一块**，绝不借用别人的气泡。
  const _bseq = Number(e.seq || 0);
  const _users = [...feed.querySelectorAll('.msg.user[data-seq]')];
  const _uSeq = _users.length ? Number(_users[_users.length - 1].dataset.seq || 0) : 0;
  const _turns = [...feed.querySelectorAll('.turn[data-seq]')]
    .filter(t => !t.closest('#streaming'))
    .filter(t => {
      const s = Number(t.dataset.seq || 0);
      if (!s) return false;                       // 老会话没有 seq：宁可不挂，也不乱挂
      if (_uSeq && s <= _uSeq) return false;      // 上一轮的正文
      if (_bseq && s >= _bseq) return false;      // 本我之后的东西
      return true;
    });
  const lastTurn = _turns.pop() || null;
  const bodyEl = lastTurn ? lastTurn.querySelector('.msg.agent .body') : null;

  // ① 总体意见进正文（它是"一段话"，塞在右侧窄列里读不成句）
  //   ★ 插在 `.body` **之后**（气泡外），不是里面（2026-10-27）：
  //     在里面的话它长在自我的白卡上，抬头又小，人分不出这是谁说的 ——
  //     用户的原话就是「为什么用户的输出里面会有本我」。
  if (bodyEl && bodyHtml) bodyEl.insertAdjacentHTML('afterend', bodyHtml);

  // ② 锚点批注进右侧栏 + 画线
  if (lastTurn && railHtml) {
    const slot = lastTurn.querySelector('.bncol');
    if (slot) {
      slot.insertAdjacentHTML('beforeend', railHtml);
      slot.dataset.used = '1';
      drawNoteLines(lastTurn);
    }
  }

  // 这一轮还没说话（只有工具）⇒ 退回流末尾，自己起一块
  if (!bodyEl && (bodyHtml || railHtml)) {
    const t = feedPutHTML(
      `<div class="turn"><div class="msg agent">${bodyHtml}</div>`
      + `<aside class="bncol">${railHtml}</aside></div>`);
    if (t) drawNoteLines(t);
  }
}

function paintNodes() {
  const box = $('nodes');
  if (!box) return;
  const cnt = $('npcount');
  if (cnt) cnt.textContent = _nodes.length ? _nodes.length + ' 段' : '';
  const rn = $('railnum');
  if (rn) rn.textContent = _nodes.length ? String(_nodes.length) : '';
  if (!_nodes.length) {
    box.innerHTML = '<span class="li" style="color:var(--faint)">还没开始</span>';
    return;
  }
  // ★ 每条**一行**：· 分隔 + 定宽时间戳 + 单行省略。
  //   原来是自由流式，时间戳和正文一起折行，读起来是"被冲乱的一坨"
  //   —— 用户：「节点没有类似 · 这种分段处理，哪怕带了时间戳也会被行距和不够精简的语言给冲乱」。
  // ★ 节点是"一句话摘要"，不是正文 —— 里面不能带 `##`、`**`、反引号这些标记。
  //   用户：「节点不要有 ## 这种文本，信息密度要高」。
  const clean = t => String(t || '')
    .replace(/^#+\s*/gm, '')
    .replace(/\*\*([^*]*)\*\*/g, '$1')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/\s+/g, ' ')
    .trim();
  // 抽屉里自己滚，所以不用像原来（常驻左栏时）那样只留 24 条 ——
  // 但也不能无限：一个长会话上千条，重建一次 innerHTML 就是一次全量排版。
  box.innerHTML = _nodes.slice(-80).map(n =>
    `<div class="node" data-seq="${esc(String(n.seq || ''))}">`
    + `<span class="nt">${esc(n.t || '')}</span>`
    // ★ 用户：「进度的精简程度太低了，我把鼠标移上去发现是一大串字。尽量精简到 10 个字」
    //   所以显示 12 字、title 也只留一句（40 字），不是把整段原文塞进悬停。
    // ★ 换行代替省略之后，就不再截到 12 字了 —— 一行放不下会自己折到第二行。
    + `<span class="nx" title="${esc(clean(n.text).slice(0, 60))}">`
    + `${esc(clean(n.text).slice(0, 30))}</span></div>`
  ).join('');
}

// 从进度抽屉里的一条，跳回执行流里那次发言。
// ★ 三件事都要做：滚过去、闪一下、**找不到时说实话**。
//   只滚不闪：正文每段长得都差不多，人得读一遍才敢确认自己到了。
//   找不到还硬滚：滚到最像的那一段，看起来成功、其实指错了 —— 那是最坏的一种。
function gotoNode(seq) {
  const feed = $('feed');
  if (!feed) return;
  const id = String(seq || '');
  if (!id) { stateNote('这一条没有锚点（老会话的事件没有 seq），跳不了', 5000); return; }
  const el = feed.querySelector('.turn[data-seq="' + CSS.escape(id) + '"]');
  if (!el) {
    stateNote('这一段已经不在当前视图里了（会话太长，只渲染了最近的部分）', 6000);
    return;
  }
  // ★ 滚到"这一段的开头"，不用 scrollIntoView：
  //   它对**长消息**（超过一屏）会按居中对齐，结果那段话的标题被推到视口上方
  //   （实测 top = -28px：跳对了，但人看到的是半截话，等于没对上）。
  const r = el.getBoundingClientRect();
  const fr = feed.getBoundingClientRect();
  // ★ 用瞬时跳转（2026-10-26 实测）：平滑滚动在**长距离**上要一秒多，
  //   期间读到的位置还没到终点（实测 900ms 后仍在 656px 处）。
  //   而且它会让长会话"飞过一大段"，人反而丢了方位。
  //   这个动作的目的是"我要看那段话"，不是"我要看滚动过程"。
  feed.scrollTop = feed.scrollTop + (r.top - fr.top) - 10;
  // 连续点同一条时动画要能重播：不强制重排的话第二次不会闪，看起来像没反应
  el.classList.remove('flash');
  void el.offsetWidth;
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 1400);
}

// ── 过程折叠（照 WorkBuddy 的 metaFold）──
// 工具调用**并成一组**，head 是一行人话摘要，点开才看每一步。
// 摘要语法照抄它的 i18n：
//   compose      = {verb} {object}      → 「看 README.md」
//   categoryCount= {count} 类动作        → 「3 类动作」
//   multiStage   = {verb1}, {verb2}: …   → 「看、跑：accept.py」
//   running      = {text}…               → 还没结束，末尾加点省略号
// 动词也是它的那套：Read / Write / Edit / List files / Search code / Run command…
// ★ 动词从单字改成 WorkBuddy 那种双字词（2026-10-25）：
//   用户截图里是「运行命令」「运行校验、修改：index.html」——
//   「看 / 改 / 跑」单字更像终端日志，"查看 / 修改 / 运行校验"才是给人读的。
const PROC_VERB = {
  read_file: '查看', write_file: '写入', edit_file: '修改', list_dir: '翻目录',
  run_check: '运行校验', run_acceptance: '跑验收', run_script: '运行脚本',
  recent_runs: '查记录', ask_human: '问', read_url: '读网页', web_fetch: '读网页',
};
// ★ MCP 工具的名字是 `mcp__server__tool`（照 Claude Code 的命名）。
//   不特判的话，界面上会出现「用mcp__fake__echo」这种机器名 ——
//   名词是给人读的，机器名该退到 title 里。
const procVerb = tl => String(tl || '').startsWith('mcp__')
  ? '外部工具'
  : (PROC_VERB[tl] || ('用' + cn(tl)));

// 把工具给的摘要拆成"动词 + 对象"：`查看 index.html` → {v:'查看', o:'index.html'}
function _splitSumm(s) {
  const t = String(s || '').trim();
  if (!t) return null;
  const i = t.indexOf(' ');
  if (i <= 0) return { v: t, o: '' };
  return { v: t.slice(0, i), o: t.slice(i + 1) };
}

function procObj(e) {
  const a = e.args || {};
  const leaf = v => String(v || '').split(/[\\/]/).filter(Boolean).pop() || '';
  if (a.path) return leaf(a.path);
  if (a.cmd) {
    const s = Array.isArray(a.cmd) ? a.cmd.join(' ') : String(a.cmd);
    // ★ 先看有没有脚本名；没有就按它**真正在做的事**说一句，
    //   别把一整条 `python -c "import os;..."` 铺在标题上
    //   —— 截图里那 5 条全是一长串命令，和 WorkBuddy 的「运行校验：index.html」差得远。
    const m = /([\w.-]+\.(?:py|sh|js|cjs))/.exec(s);
    if (m) return m[1];
    if (/\bgrep\b|\brg\b|findstr/.test(s)) return '搜索内容';
    if (/\bfind\b/.test(s)) return '查找文件';
    if (/os\.listdir|\bls\b/.test(s)) return '列出目录';
    if (/wc -l/.test(s)) return '统计行数';
    if (/\bcat\b|\.read\(/.test(s)) return '读文件内容';
    if (/os\.walk/.test(s)) return '遍历目录';
    // 兜底：一段内联脚本（python -c "..."）标题上只留"python 脚本"。
    // 完整命令在下面那行，点开能看到 —— 标题里铺一整串等于没标题。
    const first = String(s.split(/\s+/)[0] || '').replace(/\\/g, '/').split('/').pop();
    if (/^(python|python3|py)/i.test(first)) return 'python 脚本';
    return s.length > 30 ? s.slice(0, 30) + '…' : s;
  }
  if (a.pattern) return String(a.pattern).slice(0, 40);
  if (a.dir) return leaf(a.dir);
  return '';
}

function procSummary(acts, live) {
  if (!acts.length) return live ? '正在处理…' : '过程';
  const verbs = [...new Set(acts.map(x => x.verb))];
  let s;
  if (acts.length === 1) {
    s = acts[0].verb + (acts[0].obj ? ' ' + acts[0].obj : '');
  } else if (verbs.length === 1) {
    // 同一个动作做了多次：带上第一个对象 —— 「跑 2 项」这种话不告诉人任何事
    s = acts[0].verb + (acts[0].obj ? '：' + acts[0].obj : '')
      + (acts.length > 1 ? ' 等 ' + acts.length + ' 项' : '');
  } else if (verbs.length > 3) {
    // ★ 收起来之后这一行就是**唯一**看得见的东西（2026-10-27）：
    //   只说「5 类动作」，人不知道它做了多少步、有没有失败。补上步数与失败数。
    s = verbs.length + ' 类动作 · ' + acts.length + ' 步';
  } else {
    const last = acts[acts.length - 1];
    s = verbs.slice(0, 2).join('、') + (last.obj ? '：' + last.obj : '');
  }
  const bad = acts.filter(x => x.ok === false).length;
  if (bad) s += ' · ' + bad + ' 步失败';
  return live ? s + '…' : s;
}

let _procFold = null, _procActs = [];
// 过程容器里塞了几块「非工具」的东西（思考、契约、校验、拦截、记忆候选）。
// 没有工具动作的一轮也得有个说得过去的摘要 —— 否则收起后只剩「过程」两个字。
let _procRows = 0;
// 这一轮它说过话吗？没有的话，收尾要**明说**（否则屏幕上就是一片过程，
// 用户只会得出「正文没了」/「界面坏了」——2026-10-27 他就是这么报的）。
let _turnHadSay = false;

// ★ 把一条"流水账"塞进当前的过程折叠，而不是让它独占一行。
//
//   为什么需要：`判据通过` / `总纲 13993 字 · 本轮注入` / `verify_overruled …`
//   这些是**每轮一条**的机器记录。一个 10 轮的会话，光这两类就是 20 行一样的字，
//   而且它们夹在正文中间 —— 人看到的就是"很碎、很粘、不成篇"。
//   WorkBuddy 的过程折叠只露一行摘要，明细点开才看；这里照同一个道理做。
//
//   返回 true 表示"塞进去了"（调用方就不用再画一行）。没有折叠可塞时返回 false。
let _noteSeen = new Set();
// 上一条过程行。同类动作要并进同一行（WorkBuddy 的「运行校验 ×4」），
// 而不是一条条铺开 —— 铺开之后 4 行长得一模一样，人只看得出"重复了"，
// 看不出"它重试了 4 次"这件事本身。
let _procLast = null;
function noteToProc(text) {
  const t = String(text || '').trim();
  if (!t || !_procFold || !_procFold.isConnected) return false;
  // ★ 同一句话整轮只留一条（2026-10-25 从截图里一眼看到的噪音）：
  //   「总纲 3254 字 · 本轮跳过注入 (once)」在 5 个轮次里一字不差，
  //   插了 5 遍，把 5 条工具调用切成 10 段。状态没变就不是新信息。
  if (_noteSeen.has(t)) return false;
  _noteSeen.add(t);
  const body = _procFold.querySelector('.procbody');
  if (!body) return false;
  body.insertAdjacentHTML('beforeend',
    `<div class="procrow note"><span class="pact">·</span>`
    + `<span class="pobj">${esc(t.slice(0, 220))}</span></div>`);
  return true;
}

// ── 这一轮谁在答、换过没有（2026-10-26）──
// ★ 为什么单独一条横幅，而不是并进「过程」那一群工具行：
//   过程块是**可折叠**的，而"换模型"恰恰是折叠之后最容易被误解的那件事 ——
//   人看着一段风格突变的回答，想查"是不是换模型了"，却要先去展开一个折起来的东西。
//   它也不该和工具混在一起：工具是"它做了什么"，换模型是"**谁**在做"。
let _modelChain = [], _modelNow = '', _modelSw = 0;

function _kindCn(k) {
  return ({ rate_limit: '限流', auth: '凭据不对', bad_request: '请求被拒',
            server: '对面故障', network: '连不上' })[k] || k;
}

function _paintModel() {
  const el = $('mdl');
  if (!el) return;
  if (!_modelChain.length) { el.hidden = true; return; }
  el.hidden = false;
  // ★ 这个 `+N` 是「**还没试过**的备用有几个」，不是「链上一共有几个备用」。
  //   切到第二个之后还显示 +2 会让人以为后面还排着两个 —— 实际只剩一个（2026-10-26 修）。
  const cur = _modelNow || _modelChain[0];
  const at = Math.max(0, _modelChain.indexOf(cur));
  const spare = Math.max(0, _modelChain.length - 1 - at);
  el.innerHTML = '模型 <b>' + esc(cur) + '</b>'
    + (spare ? ' <span class="spare">+' + spare + '</span>' : '');
  el.classList.toggle('warn', _modelSw > 0);
  el.title = '这一轮在答的模型' + (_modelSw ? '（换过 ' + _modelSw + ' 次）' : '')
    + '\n链：' + _modelChain.join(' → ')
    + (spare ? '\n后面还有 ' + spare + ' 个备用' : '\n已经是链上的最后一个');
}

function mountModel(e) {
  if (e.act === 'model') {
    _modelChain = Array.isArray(e.chain) ? e.chain.filter(Boolean).map(String) : [];
    _modelNow = _modelChain[0] || '';
    _modelSw = 0;                    // 重放历史时这条在切换之前 ⇒ 计数从这里重新起
    _paintModel();
    return;
  }
  const from = String(e.from || ''), to = String(e.to || '');
  if (to) _modelNow = to;
  if (to && _modelChain.indexOf(to) < 0) _modelChain.push(to);
  _modelSw += 1;
  _paintModel();
  const feed = $('feed');
  if (!feed) return;
  const kind = String(e.kind || '');
  const why = String(e.why || '').trim();
  const div = document.createElement('div');
  div.className = 'mswitch';
  div.innerHTML = '<span class="mk">⚠ 换模型</span>'
    + '<span class="mt">' + esc(from || '?') + ' → <b>' + esc(to || '?') + '</b></span>'
    + (kind ? '<span class="mkind">' + esc(_kindCn(kind)) + '</span>' : '')
    + (why ? '<span class="mwhy" title="' + esc(why) + '">' + esc(why) + '</span>' : '');
  // 排在「正在说」的气泡之前 —— 那段话是切换**之后**的模型说的
  const st = $('streaming');
  const atBottom = _stickBottom;
  feedPut(div);
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}

// ── 派工单：父 agent 派出去的每一趟（2026-10-26）──
// ★ 为什么每趟一张卡，而不是把子 agent 的动作并进父的过程流：
//   并进来就分不清"谁做的"。三个并行子任务时，过程流会变成十几行混在一起的工具调用，
//   人只能看出"很忙"，看不出"谁在干什么" —— 那时速度快了、可见性差了，这笔交易是亏的。
//   卡片先立住"这是第几趟、派谁、干什么、完了没有"，细节按需展开
//   （展开读的是**它自己的** events.jsonl，不是父的事件流）。
function _subCard(n) {
  return document.querySelector('#feed .subcard[data-sub="' + n + '"]');
}

function _subHead(card) {
  const run = card.querySelector('.srun');
  if (!run) return;
  const st = card.dataset.status || 'running';
  const map = { running: '进行中', success: '完成', paused: '叫停了',
                budget: '轮数用尽', stuck: '卡住了', needs_human: '在等人',
                fail: '没跑成', crash: '崩了' };
  run.textContent = map[st] || st;
  run.className = 'srun' + (st === 'success' ? ' ok' : (st === 'running' ? '' : ' no'));
  card.classList.toggle('done', st === 'success');
  card.classList.toggle('bad', st !== 'running' && st !== 'success');
}

// 「这类」到底是哪一类 —— 卡片上的悬停说明和提案区用的是同一句话。
// ★ 一句话里必须同时有**动作**和**范围**：只说"这个目录"人会以为是全部操作。
function _scopeCn(sc) {
  const a = sc || {};
  const d = String(a.domain || '');
  if (d === 'file') {
    return '以后「' + (a.action === 'write' ? '写' : '读') + ' ' + String(a.path || '') + '」不问';
  }
  if (d === 'script') {
    return '以后这份脚本不问（按内容签哈希，改了重新问）'
      + (a.also_file ? '，并且放开 ' + a.also_file : '');
  }
  if (d === 'mcp') {
    return '以后工具 ' + String(a.server || '') + ' / ' + String(a.tool || '') + ' 不问';
  }
  return '以后这类不问';
}

// ── 闸门拦住了：停下来问的那张卡（2026-10-26 一次性许可）──
// ★ 为什么用"下标登记表"而不是把签名塞进 data 属性：
//   上一轮刚被这个坑咬过 —— `data-fdrop="project|main|2"` 用 `|` 拼，
//   而键名自己就带 `|`，取出来是 `NaN`，点删除一点反应都没有。
//   签名和标签都可能含任意字符，塞进属性里再 split 一定出事。
//   ⇒ 卡片只带一个数字，其余全在 JS 里查表。
let _blkJobs = [];
// 这一轮的"等回话"是不是闸门拦的（决定那条 waitbar 给不给「打开问题」）
let _pendingBlock = false;

function mountBlock(e) {
  const feed = $('feed');
  if (!feed) return;
  const i = _blkJobs.push({
    sig: String(e.sig || ''),
    kind: String(e.gkind || ''),
    label: String(e.label || ''),
    tool: String(e.tool || ''),
    block: String(e.blocked_by || ''),
    session: cur,
    // ★ 「以后这类都别问」的范围（2026-10-26）。由**命中的规则**算出来，
    //   不是让人填一个 glob —— 手填通配符等于给一把"把闸关掉"的钥匙。
    //   为空 ⇒ 不给这个按钮（deny 级和底线那两类本来就没有这种说法）。
    scope: e.gscope || null,
  }) - 1;
  const j = _blkJobs[i];
  // `why` 里是闸门给的原文（"这一步要对外部世界产生后果" + 命中那几行）。
  // 不截断到 700 字以内：再长就不是"看两眼能判断"的东西了，该去看文件。
  const detail = String(e.why || '').slice(0, 700);
  const can = !!j.sig;
  const html = '<div class="blk" data-blk="' + i + '">'
    + '<div class="blkh"><span class="blktag">拦住了一步</span>'
      + '<span class="blkwhy">' + esc(j.label || j.tool || '这一步') + '</span></div>'
    + (detail
        // ★ 那 700 字原文默认**收起来**（2026-10-27 做减法）。
        //   截图里这一块把整屏占满：脚本清单、`rules.py sign` 建议、5 行命中位置 ——
        //   用户的原话是「停下来问人…一堆输出，但是有用信息为 0」。
        //   人真正要先回答的只有"这一步给不给"，所以：标题 + 三个按钮先给，
        //   "为什么"点一下再看。它一行都没少，只是不再抢位置。
        ? '<details class="blkmore"><summary>为什么拦这一步</summary>'
          + '<div class="blkdetail">' + esc(detail) + '</div></details>'
        : '')
    + '<div class="blkb">'
    + (can
        ? '<button class="gbtn gmain" data-g="once">允许这一次</button>'
          + '<button class="gbtn" data-g="ttl">10 分钟内都允许</button>'
          + (j.scope
              ? '<button class="gbtn" data-g="never" title="这一步现在就放行；'
                + '并且把「' + esc(_scopeCn(j.scope)) + '」记成待采纳的免问例外'
                + '（采纳后才不问，可以撤）">以后这类都别问</button>'
              : '')
          + '<button class="gbtn gno" data-g="deny">不给</button>'
          // ★ 那句「只放行这一份内容；改了就得重新问」原来**常驻在按钮旁边**，
          //   和按钮同一行、同样显眼 —— 但它是"第二遍解释"，看一次就够（2026-10-27）。
          //   移进 title：想确认的人悬停就看得到，不想看的人不必每次读一遍。
          + '<span class="gnote" hidden>只放行这一份内容；改了就得重新问'
          + (j.scope ? ' · 「以后这类都别问」会先进待采纳区' : '') + '</span>'
        : '<span class="gnote">这一步给不了一次性许可（没能给它签名）——'
          + '先让它能被看清（比如把脚本写成文件），或者请人直接来做。</span>')
    + '</div></div>';
  // ★★ 拦截卡**不进过程容器，直接放流里**（2026-10-27 第二次修正）。
  //   上一版把它塞进过程容器，结果是：屏幕只剩一行「它停在这里，等你决定上面那一步」，
  //   而**按钮被折在容器里**，人根本找不到点哪儿（用户原话「有没有卡着也不清楚」）。
  //   它是唯一"人必须动手"的东西，而且是这一轮**唯一的进展** —— 它属于对话，不属于过程。
  //   ⇒ 流里：user 气泡 → 这张卡 → 本我旁注。干净，且一眼看得出卡在哪一步。
  // ★ 拿**刚插进去那一块**，不能拿 feed.lastElementChild（2026-10-27）：
  //   活块在末尾时，最后那个子节点是「正在说」的气泡，不是这张卡 ——
  //   于是按钮绑到别人身上，且不报错。
  // ★★ 拦截卡落在**输入栏正上方的停靠区**（2026-10-31 用户：别再进思考流）。
  //   它是唯一"人必须动手"的东西，和"给不给"是发送侧的决定 —— 贴着输入栏才对。
  //   拿不到 dock 时才退回正文流（老页面 / 测试环境没有它）。
  const dock = $('askdock');
  let box = null;
  if (dock) {
    dock.innerHTML = html;
    dock.hidden = false;
    box = dock.firstElementChild;
  } else {
    box = feedPutHTML(html);
  }
  // ★ 记一笔：这一轮的"等回话"是**闸门拦的**，不是模型在提问（2026-10-26）。
  _pendingBlock = true;
  if (!box) return;
  box.querySelectorAll('[data-g]').forEach(b => {
    b.onclick = async () => {
      const viewVersion = _viewVersion;
      const act = b.dataset.g;
      box.querySelectorAll('[data-g]').forEach(x => { x.disabled = true; });
      const r = await api('/grants', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          act: (act === 'deny') ? 'deny' : (act === 'never' ? 'never' : 'allow'),
          mode: (act === 'ttl') ? 'ttl' : 'once',
          ttl: 600, sig: j.sig, kind: j.kind, label: j.label,
          session: j.session,
          // 只有「以后这类都别问」需要范围；服务端按它记提案
          scope: (act === 'never') ? j.scope : null,
          why: (act === 'deny') ? '界面里点的「不给」'
            : (act === 'never' ? '界面里点的「以后这类都别问」' : '界面里点的「允许」'),
        }),
      });
      if (_viewVersion !== viewVersion) return;
      const d = r.body || {};
      if (r.status !== 200 || !d.ok) {
        box.querySelectorAll('[data-g]').forEach(x => { x.disabled = false; });
        showErr(d.error || ('没批成（' + r.status + '）'));
        return;
      }
      const word = act === 'deny' ? '记下了：这一步不给'
        : (act === 'never' ? '已允许这一步 · 「以后这类都别问」进了待采纳区'
        : (act === 'ttl' ? '已允许（10 分钟内不再问）' : '已允许（只这一次）'));
      // ★ 三种结果要分开说（2026-10-28）：真的续上了 / 排队等它收尾 / 没续上（带原因）。
      //   上一版只有「续上」和「重新发一次」两句，而"没续上"的真实原因
      //   （这一轮还在收尾）被丢掉了 —— 人看到的只是"批了就不动了"。
      const rs = d.resume || {};
      const queued = !!rs.queued;
      const resumed = !!(d.resume && !rs.error && !rs.queued);
      const rsErr = rs && rs.error ? String(rs.error) : '';
      // ★ 免问提案没记成时要当场说 —— 人以为"以后不问"了，其实什么都没发生，
      //   而下一次被拦时他会觉得"这按钮是假的"。
      const pr = d.proposal || null;
      const prNote = (act === 'never')
        ? (pr && pr.ok ? '（采纳之后才真的不问，可以撤）'
                       : ('（**没记上**：' + ((pr && pr.error) || '原因不明') + '）'))
        : '';
      box.outerHTML = '<div class="blkok">' + esc(word) + esc(prNote)
        + (act === 'deny' ? ' · 已经让它换个做法'
                          : (queued ? ' · 这一轮还在收尾，收完会自动接着跑'
                             : (resumed ? ' · 已经让它接着跑'
                                : (' · **没续上**：' + (rsErr || '原因不明')
                                   + '（许可已经记下了，再点一次或直接说一句「继续」都行）'))))
        + '</div>';
      paintPill();
    };
  });
}

// 将原子任务派发事件放在当前轮的过程组中，同一任务号用于之后的状态更新。
function mountSpawn(e) {
  const feed = $('feed');
  const n = Number(e.sub || 0);
  if (!feed || !n) return;
  const had = _subCard(n);
  if (had) {                       // 重放：卡片已经在，只把状态拨回去
    had.dataset.status = 'running';
    _subHead(had);
    return;
  }
  const card = document.createElement('div');
  card.className = 'subcard';
  card.dataset.sub = String(n);
  card.dataset.status = 'running';
  card.innerHTML =
    '<div class="sh"><span class="stag">' + esc(e.kind || 'general') + '</span>'
    + '<span class="sname">' + esc(e.label || '查一件事') + '</span>'
    + '<span class="srun">进行中</span>'
    + '<span class="stime"></span>'
    + '<button class="sbtn" data-ssteer="' + n + '">说一句</button>'
    + '<button class="sbtn" data-sstop="' + n + '">叫停</button>'
    + '<button class="sbtn" data-sopen="' + n + '">看过程</button></div>'
    + '<div class="steerbox"><input class="steerin" '
    + 'placeholder="补一句：换个方向、别做什么、注意哪一点…">'
    + '<button class="sbtn" data-send="' + n + '">发过去</button>'
    + '<span class="steernote"></span></div>'
    + '<div class="sbody"></div>';
  card.querySelector('.sh').onclick = (ev) => {
    const t = ev.target;
    if (t.dataset && t.dataset.sstop) {
      ev.stopPropagation();
      _stopSub(Number(t.dataset.sstop));
      return;
    }
    if (t.dataset && t.dataset.ssteer) {
      ev.stopPropagation();
      const box = card.querySelector('.steerbox');
      box.classList.toggle('on');
      if (box.classList.contains('on')) {
        const inp = box.querySelector('.steerin');
        if (inp) inp.focus();
      }
      return;
    }
    _openSub(Number(card.dataset.sub));
  };
  const _sb = card.querySelector('.steerbox');
  if (_sb) {
    _sb.querySelector('[data-send]').onclick = (ev) => {
      ev.stopPropagation();
      _sendSteer(Number(card.dataset.sub));
    };
    _sb.querySelector('.steerin').onkeydown = (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); _sendSteer(Number(card.dataset.sub)); }
      ev.stopPropagation();
    };
  }
  const atBottom = _stickBottom;
  feedPut(card);
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}

// 按原子任务号标记完成并移除只在执行中可用的控制。
function mountSubEnd(e) {
  const card = _subCard(Number(e.sub || 0));
  if (!card) return;
  card.dataset.status = e.ok ? 'success' : 'fail';
  if (e.why) card.dataset.why = String(e.why);
  _subHead(card);
  const t = card.querySelector('.stime');
  if (t) {
    t.textContent = (e.ms ? (Number(e.ms) / 1000).toFixed(1) + 's · ' : '')
      + (Number(e.turns) || 0) + ' 轮'
      // ★ 这趟花了多少（2026-10-26）。不给这个数，并行一开就没法判断值不值 ——
      //   人只知道"派了三个人"，不知道"花了多少"。
      + (Number(e.tokens) ? ' · ' + fmt(Number(e.tokens)) + ' tok' : '');
  }
  const stop = card.querySelector('[data-sstop]');
  if (stop) stop.remove();          // 已经跑完了，还留着"叫停"就是骗人
  const stb = card.querySelector('[data-ssteer]');
  if (stb) stb.remove();            // "说一句"同理：跑完了还能插话就是骗人
  const sbox = card.querySelector('.steerbox');
  if (sbox) sbox.remove();
}

// 展开旧子任务卡时按当前会话和子任务号首次读取后端元数据与事件；是否能应用补丁取自原任务记录。
async function _openSub(n) {
  const card = _subCard(n);
  if (!card) return;
  const open = !card.classList.contains('open');
  card.classList.toggle('open', open);
  if (!open || card.dataset.loaded === '1') return;
  const body = card.querySelector('.sbody');
  body.innerHTML = '<div class="sline">读它的过程…</div>';
  const sid = encodeURIComponent(cur);
  const one = await api('/sessions/' + sid + '/sub/' + n);
  const two = await api('/sessions/' + sid + '/sub/' + n + '/events?after=0');
  const meta = (one.body && one.body.meta) || {};
  const res = (one.body && one.body.result) || '';
  const rows = (((two.body && two.body.events) || []).filter(
    r => r.act === 'tool' || r.act === 'say')).slice(0, 60);
  const acts = rows.map(r => r.act === 'tool'
    ? '<div class="sline">· ' + esc(r.summary || r.tool || '') + '</div>'
    : '<div class="sline say">' + esc(String(r.text || '').slice(0, 400)) + '</div>').join('');
  card.dataset.loaded = '1';
  const ch = meta.change || {};
  const nChg = Number(ch.added || 0) + Number(ch.modified || 0) + Number(ch.removed || 0);
  let applyHtml = '';
  if (nChg > 0) {
    applyHtml = meta.applied
      ? '<div class="swhy">✓ 这些改动已经应用到工作区</div>'
      : '<div class="sapply-w"><button class="sbtn" data-sapply="' + n + '">'
        + '应用这些改动</button>'
        + '<span class="sline">先试一遍能不能打上；打不上就一个字都不动</span></div>';
  }
  body.innerHTML =
    (meta.prompt ? '<div class="swhy">派它去：' + esc(meta.prompt) + '</div>' : '')
    + (meta.model ? '<div class="sline">用的模型：' + esc(meta.model) + '</div>' : '')
    + (meta.usage && meta.usage.total_tokens
        ? '<div class="sline">花了 ' + fmt(meta.usage.total_tokens) + ' tok / '
          + (meta.usage.calls || 0) + ' 次调用</div>' : '')
    + (acts || '<div class="sline">（它还没动过手）</div>')
    + (res ? '<div class="swhy">结论</div><div class="sres">' + esc(res) + '</div>' : '')
    + (nChg > 0 ? '<div class="swhy">改了 ' + nChg + ' 个文件'
        + (ch.plus || ch.minus ? '（+' + (ch.plus || 0) + ' −' + (ch.minus || 0) + ' 行）' : '')
        + '</div>' : '')
    + applyHtml;
  body.onclick = (ev) => {
    const b = ev.target && ev.target.closest ? ev.target.closest('[data-sapply]') : null;
    if (b) _applySub(Number(b.dataset.sapply), b);
  };
}

// 把补充指令绑定到卡片的运行中子任务，后端确认接收后才清理编辑框。
async function _sendSteer(n) {
  const card = _subCard(n);
  if (!card) return;
  const inp = card.querySelector('.steerin');
  const txt = (inp && inp.value || '').trim();
  if (!txt) return;
  const r = await api('/sessions/' + encodeURIComponent(cur) + '/sub/' + n + '/steer',
    { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: txt }) });
  const note = card.querySelector('.steernote');
  if (r.status === 200) {
    inp.value = '';
    if (note) note.textContent = '已送达，它下一次开口前会看到';
    return;
  }
  const b = r.body || {};
  if (note) note.textContent = '';
  showErr(b.error || '这句没送出去');
}

// 对原子任务申请应用保留补丁，由后端先核对可应用性，再更新卡片回执。
async function _applySub(n, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '应用…'; }
  const r = await api('/sessions/' + encodeURIComponent(cur) + '/sub/' + n + '/apply',
    { method: 'POST' });
  if (r.status === 200) {
    if (btn) {
      btn.textContent = '已应用';
      btn.className = 'sbtn done';
      btn.removeAttribute('data-sapply');
    }
    return;
  }
  const b = r.body || {};
  if (btn) { btn.disabled = false; btn.textContent = '应用这些改动'; }
  showErr((b.error || '应用失败') + (b.detail ? '\n' + b.detail : ''));
}

// 向当前会话的指定子任务发停止请求，成功后更新原卡片。
async function _stopSub(n) {
  const card = _subCard(n);
  const r = await api('/sessions/' + encodeURIComponent(cur) + '/sub/' + n + '/stop',
    { method: 'POST' });
  if (r.status !== 200) {
    showErr((r.body && r.body.error) || '叫停没成功');
    return;
  }
  if (card) {
    const b = card.querySelector('[data-sstop]');
    if (b) { b.textContent = '停止中…'; b.disabled = true; }
  }
}

// 流里只放「谁说了什么」和「这一轮的结论」。其余一切 —— 工具、思考、契约、
// 校验、注入记录、钩子、记忆候选 —— 都属于「过程」，走过程容器。
// ★ 这张表就是这一轮做减法的**唯一判据**：以后新增事件类型时，
//   先问"它是一句话、一个结论，还是一件过程"，别默认往流里塞。
// ★ `end` 留在流里：它常常是「它停在这里，等你决定」这条状态条 ——
//   那是结论不是过程，藏进折叠等于把"卡住了"这件事藏起来（用户就抱怨过"有没有卡着也不清楚"）。
const PROSE_ACTS = new Set(['user', 'say', 'end']);

// 过程块：一轮里所有"过程"共用**一个**，收尾时收起只留一行。
// ★ 抽成独立函数（2026-10-27）：原来这段内联在 mountTool 里，于是**只有工具调用**
//   才会建过程块；而思考、契约、校验、拦截这些同样属于"过程"的东西
//   各自往流里插一块 —— 一轮十几行，正文被埋在中间。
//   现在它们全部走进这一个容器，流里只留"谁说了什么"。
// 往执行流的**内容末尾**放一块东西（流里所有插入都走这两个口）。
//
// ★ 为什么不能再各写各的（2026-10-27 用户：「say 里面的思考会滚到用户发言后面，
//   有点割裂」）。查出来的真相不是"思考块乱跑"，是**插入锚点不可靠**：
//   活块（#thinking / #streaming）会被 openThink 搬到末尾，而它此刻是显示还是
//   隐藏并不稳定。重放历史时 #streaming 停在中途却仍然可见，于是新建的过程卡
//   走 `insertBefore(卡, #streaming)` ⇒ 插到了它前面，正好跨过上一句用户发言。
//   实测顺序变成：你的发言[4] → 正文 → 过程卡 → 你的发言[14]，
//   读起来像"第 2 轮的动作发生在第 2 句之前"。
// ⇒ 定一条不变量：**内容按时间顺序排，活块永远在内容之后**。
//   每次插入先把活块摆正到末尾，再把新内容放在活块前面。
function __liveBlocks() {
  return [$('thinking'), $('streaming'), $('doing')].filter(x => x && x.isConnected);
}

function __liveAtTail(f) {
  const live = __liveBlocks();
  if (!live.length) return true;
  const tail = [...f.children].slice(-live.length);
  return tail.length === live.length && tail.every((x, i) => x === live[i]);
}

function feedPut(node) {
  const f = $('feed');
  if (!f || !node) return null;
  const live = __liveBlocks();
  // 已经在末尾就别动 —— 移动 DOM 会打断正在选中的文字，也会让滚动跳一下
  if (!__liveAtTail(f)) live.forEach(x => f.appendChild(x));
  if (live.length) f.insertBefore(node, live[0]);
  else f.appendChild(node);
  return node;
}

function feedPutHTML(html) {
  const f = $('feed');
  if (!f || !html) return null;
  foldThinkPanel();                 // 定稿正文出现，同样把思考整块收起
  const t = document.createElement('template');
  t.innerHTML = html;
  const frag = t.content;
  const first = frag.firstElementChild;
  if (!first) return null;
  const live = __liveBlocks();
  if (!__liveAtTail(f)) live.forEach(x => f.appendChild(x));
  if (live.length) f.insertBefore(frag, live[0]);
  else f.appendChild(frag);
  return first;
}

function archiveThoughtFlow(showExecution) {
  // ★ 2026-10-31：不再搬运。思考与工具**从一开始就写在折叠块里**，
  //   上一版「结束后把节点搬到另一个分页」的做法随之取消 ——
  //   搬运会让"正文出现后又产生的新思考"再次插回正文中间，边界又碎了。
  //   这里只做收尾：刷新摘要、保持折叠（人这时在读正文）。
  foldThinkPanel();
  _finalArchived = true;
  _pendingArchive = false;
  return 0;
}

// ── 思考折叠块：按**轮次**排在正文上方（2026-10-31 用户定稿）──────────────
// 形态：思考块A → 正文A → 思考块B → 正文B ……
// 这样"这一段话是哪段思考换来的"一眼对得上，不用去另一个区域找。
// ★ 不做成全局一个块：全局块只能回答"这一整场想了什么"，回答不了"这段正文"。
// ★ 思考仍然**流式**写进当前这一块（打开状态）；正文一来就把它折在自己上方。
let _thinkRound = null;          // 当前轮的 <details.thinkblock>
let _roundHadSay = false;        // 这一轮的正文是否已经开始
let _thinkAutoFolded = false;    // 这一轮是否已经自动折过

function _newThinkRound() {
  const feed = $('feed');
  if (!feed) return null;
  const d = document.createElement('details');
  d.className = 'thinkblock';
  // ★ 默认收起（2026-10-31 用户：「小思考块默认闭合吧。想看再去打开」）。
  //   思考仍然流式写进来，只是**不主动摊开**：想看哪一轮，点它的摘要即可。
  d.open = false;
  d.hidden = true;                        // 还没内容时不占位
  d.innerHTML = '<summary><span class="tp-ico">◌</span>'
    + '<span class="tpsummary">正在回应</span></summary><div class="thinkbody"></div>';
  feed.appendChild(d);
  _thinkRound = d;
  _thinkAutoFolded = false;
  _roundHadSay = false;        // 新的一块 = 新的一轮；不重置会让之后每段思考都另起一块
  return d;
}

function resetThinkPanel() {
  _thinkRound = null;
  _roundHadSay = false;
  _thinkAutoFolded = false;
}

function syncThinkSummary() {
  _syncRound(_thinkRound);
}

function procHost() {
  // 当前轮的思考容器。
  // ★ 上一轮**正文已经开始**之后又来了新思考 ⇒ 另起一块，放在新正文之前。
  //   塞进旧块的话，新思考会显示在旧正文上方，两段话就对错了。
  if (!_thinkRound || !_thinkRound.isConnected || _roundHadSay) _newThinkRound();
  return _thinkRound ? _thinkRound.querySelector('.thinkbody') : $('feed');
}

function foldThinkPanel() {
  _roundHadSay = true;             // 正文开始：之后的思考属于下一轮
  syncThinkSummary();
  _pruneEmptyRounds();             // 这一轮已经写完，空容器收走（要在提前返回之前）
  if (_thinkAutoFolded) return;
  _thinkAutoFolded = true;
  // ★ 不看"里面有没有内容"：第一段正文到来时这一块可能还没节点，
  //   若那时跳过，标记却已置位，整轮都不会再折（2026-10-31 重放踩过）。
  if (_thinkRound) _thinkRound.open = false;
}

function ensureProc() {
  // ★★ 先看"已有的过程卡还能不能接着用"，再决定要不要新开一轮（2026-10-31 重放实测）。
  //   原来顺序反了：先 procHost() 建块，再发现 _procFold 已连接就 return ——
  //   于是**每次工具调用都留下一个空块**，一段正文后面能挂两个「思考过程」，
  //   其中一个还可见（空标题）。建块是有副作用的动作，得放在确认需要之后。
  if (_procFold && _procFold.isConnected) return _procFold;
  const host = procHost();
  if (!host) return null;
  const wrap = document.createElement('div');
  wrap.innerHTML =
    '<div class="procfold open"><div class="prochead"><span class="psum"></span>'
    + '<svg class="parrow" width="12" height="12" viewBox="0 0 16 16" fill="none">'
    + '<path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.4" '
    + 'stroke-linecap="round"/></svg></div><div class="procbody"></div></div>';
  // ★ 先取引用再插入：插进 DOM 之后 wrap 就空了，wrap.firstElementChild 是 null。
  const fold = wrap.firstElementChild;
  // 过程卡落在**思考折叠块**里，不占正文的位置。
  host.appendChild(fold);
  syncThinkSummary();
  _procFold = fold;
  _procActs = [];
  _procRows = 0;
  _noteSeen = new Set();
  // ★ 这里**不能**清 _narrPending（2026-10-26 踩过）：过程块常在"第一个工具调用"
  //   那一刻建，而这一轮的叙述句正是同一时刻由 appendThink 留下的。
  _procLast = null;
  const hd = fold.querySelector('.prochead');
  // ★ 从 DOM 上找，不用闭包里的 _procFold —— 它在一轮结束时就置 null 了，
  //   而"跑完了想回看做了什么"恰恰是那一刻。
  hd.onclick = () => {
    const f = hd.closest('.procfold');
    if (!f) return;
    f.classList.toggle('open');
    // 开合之后标题的写法要跟着变（展开说步数、收起说做了什么）
    if (f === _procFold) _paintProcSum(!!running);
    else {
      const _s = f.querySelector('.psum');
      const _a = (f.dataset.acts ? JSON.parse(f.dataset.acts) : []);
      if (_s) _s.textContent = f.classList.contains('open')
        ? (_a.length ? _a.length + ' 步' : '过程') : (_s.dataset.full || _s.textContent);
    }
  };
  return fold;
}

// 把一块"过程"塞进当前过程容器（没有就建一个）。
// 契约 / 校验 / 思考 / 拦截 / 记忆候选 都走这里。
function procAppend(html) {
  const f = ensureProc();
  if (!f || !html) return f;
  const body = f.querySelector('.procbody');
  if (body) body.insertAdjacentHTML('beforeend', html);
  _procRows += 1;
  _paintProcSum(!!running);
  return f;
}

// 把一块过程并回**最后那张过程卡**（收尾那一段：改动清单 / 记忆候选）。
//
// ★ 为什么不能直接用 procAppend（2026-10-27 实测）：`end` 会把容器引用清掉
//   （`_procFold = null`），于是 end 之后才到的 `changes` / `distill` 各起一张
//   新卡 —— 真跑一轮出现 **4 张「过程」卡**（开工前 / 干活 / 校验 / 收尾尾巴），
//   人看着像跑了好几遍。它们其实是同一段「收尾」，该在同一张卡里。
// ★ 判据是"卡之后有没有新的用户发言"：有，说明那是上一轮的卡，不能并进去
//   （并进去就等于把这一轮的东西写回上一轮）。
function procAppendTail(html) {
  const f = $('feed');
  if (!f || !html) return procAppend(html);
  const kids = [...f.children];
  let li = -1;
  kids.forEach((x, i) => { if (x.classList && x.classList.contains('procfold')) li = i; });
  if (li < 0) return procAppend(html);
  const crossed = kids.slice(li + 1).some(x => x.classList
    && x.classList.contains('msg') && x.classList.contains('user'));
  if (crossed) return procAppend(html);
  const body = kids[li].querySelector('.procbody');
  if (!body) return procAppend(html);
  body.insertAdjacentHTML('beforeend', html);
  _procRows += 1;
  _paintProcSum(!!running);
  return kids[li];
}

// 同上，但收的是已经建好的节点（思考块是节点，不是字符串）
function procAppendNode(node) {
  const f = ensureProc();
  if (!f || !node) return f;
  const body = f.querySelector('.procbody');
  if (body) body.appendChild(node);
  _procRows += 1;
  _paintProcSum(!!running);
  return f;
}

// 摘要：有工具动作就按动作说，没有就按「塞了几条」说。
// ★ 收起之后这一行是**唯一**看得见的东西，不能只剩「过程」两个字。
function _paintProcSum(live) {
  if (!_procFold) return;
  const el = _procFold.querySelector('.psum');
  if (!el) return;
  // ★ 展开时**不要**再把第一条动作写进标题（2026-10-27 用户截图）：
  //   标题写「查看 isolate.py」、卡内第一行也是「查看 isolate.py」——
  //   同一句话在一屏里出现两次，看着像渲染重了。收起时标题是唯一可见的那行，
  //   所以那时候才需要详情；展开时它只该说"这一段有多长"。
  if (_procActs.length) {
    // ★ 跑的时候标题就写**正在做的那件事**（2026-10-28 用户：「看不出来在干活」）。
    //   「过程 · 3 步」说明不了它在干什么；「读 loop/tools.py…」才行。
    if (live) {
      const a = _procActs[_procActs.length - 1];
      el.textContent = (a.verb || '处理') + (a.obj ? ' ' + a.obj : '') + '…';
      return;
    }
    // 详情版存一份：收起/展开切换时要用它，不能靠"当前显示的文字"反过来推
    const full = procSummary(_procActs, live);
    el.dataset.full = full;
    if (_procFold.classList.contains('open')) {
      const bad = _procActs.filter(x => x.ok === false).length;
      const kinds = new Set(_procActs.map(x => x.verb)).size;
      el.textContent = (kinds > 1 ? kinds + ' 类动作 · ' : '')
        + _procActs.length + ' 步' + (bad ? ' · ' + bad + ' 步失败' : '');
      return;
    }
    el.textContent = full;
    return;
  }
  el.textContent = _procRows ? ('过程 · ' + _procRows + ' 条')
    : (live ? '正在处理…' : '过程');
}

function mountTool(e) {
  const feed = $('feed');
  if (!feed) return;
  // ★★ 它真的动手了 ⇒ 把这一轮思考块的标题改写成"在干什么"（2026-10-26 补上）。
  //   `_noteThinkAction` 之前**定义了但从来没被调用** —— 于是"动作优先、思考兜底"
  //   这条规则一次都没生效，所有标题永远是「思考：关于 xxx.py」。
  //   这类"写好了没接上"的错最难看见：函数在、注释在、逻辑对，就是没人叫它。
  _noteThinkAction(e);
  ensureProc();
  // ★ 工具在跑 ⇒ 过程卡**保持展开**（2026-10-29 用户：「有时候就是一个状态条，
  //   完全不知道他在干什么」）。模型每说一句 closeProcFold 会收起，
  //   而工具还在跑 —— 于是只剩一行「列出 …」。收尾时它才会真的收起。
  if (_procFold) _procFold.classList.add('open');

  // ── 叙述行：它自己说要干什么，就写在动作前面 ──
  // ★ 放在"过程块建好之后、工具行之前"：过程块是第一次工具调用才建的，
  //   更早插没有容器；更晚插就跑到动作下面去了（叙述说的是"接下来"，不该在马后炮）。
  if (_narrPending) {
    const say = _narrPending;
    _narrPending = '';                       // 一句话只当一次叙述
    if (!_narrShown.has(say)) {
      _narrShown.add(say);
      const body = _procFold.querySelector('.procbody');
      if (body) {
        body.insertAdjacentHTML('beforeend',
          `<div class="procrow narr"><span class="pobj">${esc(say)}</span></div>`);
      }
    }
  }

  const ok = e.ok !== false;
  // ★★ 摘要优先用**事件里带的那个**（工具层 tools.describe 写的），而不是前端猜
  //   （2026-10-26）。`procObj` 那套只剩兜底作用：老事件没有 summary 字段，
  //   或者以后加了新工具忘了写 describe —— 那时候界面还能显示个大致，不是空白。
  //
  // ★ 但 summary 是**一句完整的话**（"查看 index.html"），里面已经有动词了 ——
  //   再拼一次 PROC_VERB 就成了「查看 查看 index.html」（实测截图里就是这样）。
  //   所以把它按第一个空格拆成两段，保住 WorkBuddy 那种"动词 / 对象"不同的淡色，
  //   又不重复。没有空格（"跑全部验收""等你拍板"）就整段当动词，对象留空。
  const _sp = _splitSumm(e.summary);
  const _v = _sp ? _sp.v : procVerb(e.tool);
  // ★ 摘要里没有对象时，从工具参数里再兜一次（2026-10-29 用户截图：
  //   折叠行只有一个「列出 …」，看不出它在列哪个目录）。
  const _o = (_sp && _sp.o) || procObj(e);
  // ★ 当前动作条（2026-10-28 用户：「看不出来他在干活」）。
  //   过程卡是折叠的，思考块只写「思考中」——所以"它正在读哪个文件、跑哪条命令"
  //   得有一条**不用点开**就能看见的地方。每次工具调用就更新它。
  //   顺序：动作（事实）在前，它自己说的意图（_narrPending）附在后面。
  const dg = $('doing');
  if (dg) {
    // ★ 不在这里碰 hidden：可见性只有一个出口（paintPill 按「在跑+有内容」判）。
    //   实测 mountTool 显示、end 又隐藏、重放再隐藏，动作条时隐时现。
    dg.innerHTML = '<i class="dudot"></i><span class="doverb">' + esc(_v) + '</span>'
      + '<span class="doobj">' + esc(_o) + '</span>'
      + (_narrPending ? '<span class="dowhy">' + esc(_narrPending) + '</span>' : '');
  }
  const _key = _v + '|' + _o;
  // ★ 同类动作并进上一行 —— WorkBuddy 就是这么做的（一组并成一行）。
  //   实测那一轮 4 条命令全是「遍历目录」（失败后在重试），铺开就是 4 行一模一样的字：
  //   人只看出"重复了"，看不出"它重试了 4 次"这件事本身。合并之后 `×4` 才说得清。
  if (_procLast && _procLast.key === _key && _procLast.row.isConnected) {
    _procLast.n += 1;
    _procLast.ms += (e.elapsed_ms || 0);
    let c = _procLast.row.querySelector('.pcount');
    if (!c) {
      c = document.createElement('span');
      c.className = 'pcount';
      _procLast.row.appendChild(c);
    }
    c.textContent = '×' + _procLast.n;
    const msEl = _procLast.row.querySelector('.pms');
    if (msEl) msEl.textContent = _procLast.ms + 'ms' + (ok ? '' : ' · 失败');
    if (!ok) _procLast.row.classList.add('bad');
    // 合并的是"同一件事跑了几次"，证据一条都不丢：每次的命令原文都进详情。
    const at2 = e.args ? briefArgs(e.args) : '';
    if (at2 && _procLast.det) {
      _procLast.det.insertAdjacentHTML('beforeend', `<pre>${esc(at2)}</pre>`);
    }
    _procActs.push({ verb: _v, obj: _o, ok: (e.ok !== false) });
    _procFold.querySelector('.psum').textContent = procSummary(_procActs, true);
    return;
  }
  const row = document.createElement('div');
  row.className = 'procrow' + (ok ? '' : ' bad');
  // ★ 改了文件就报规模（2026-10-26）。原来这一行只有「修改 tools.py」——
  //   改了什么、动了多少，界面里一个字都看不到，人只能切到编辑器。
  //   有了 `+12 −3` 至少能一眼看出"这是小改还是重写"。
  const _df = e.diff || null;
  row.innerHTML = THINK_ICON + `<span class="pverb">${esc(_v)}</span>`
    + `<span class="pobj" title="${esc(briefArgs(e.args) || '')}">${esc(_o)}</span>`
    + (_df ? `<span class="pdiff">+${_df.add || 0} −${_df.del || 0}</span>` : '')
    + `<span class="pms">${e.elapsed_ms || 0}ms${ok ? '' : ' · 失败'}</span>`;
  // 点一下展开参数与输出（默认收起：过程细节不该和对话抢注意力）
  const det = document.createElement('div');
  det.hidden = true;
  const at = e.args ? briefArgs(e.args) : '';
  const ot = e.out || '';
  det.innerHTML = (at ? `<pre>${esc(at)}</pre>` : '')
                + (ot ? `<pre>${esc(ot.slice(0, 4000))}</pre>` : '');
  // 点开：先给 diff（人最想看的是"改了什么"），再看参数和输出。
  // ★ 全文从接口按需拉 —— 事件里只有统计，所以第一次点开才去取。
  let _diffOn = false;
  row.onclick = ev => {
    ev.stopPropagation();
    det.hidden = !det.hidden;
    if (det.hidden || !_df || !_df.file || _diffOn) return;
    _diffOn = true;
    det.insertAdjacentHTML('afterbegin',
      '<pre class="diffload">正在读改动…</pre>');
    api('/sessions/' + encodeURIComponent(cur) + '/diff/'
        + encodeURIComponent(_df.file)).then(r => {
      const pre = det.querySelector('.diffload');
      if (!pre) return;
      if (r.status !== 200) {
        pre.textContent = '读不到这份改动：'
          + ((r.body && r.body.error) || ('HTTP ' + r.status));
        return;
      }
      pre.className = 'diffpre';
      // ★ 按行着色（2026-10-26）：只靠行首的 -/+ 区分，扫一遍要逐行看第一个字符；
      //   上了底色之后"哪几行删了、哪几行加了"是一眼的事。配色和 git / GitHub 一致
      //   （这段是代码 diff，不适用红涨绿跌那套）。
      const _lines = String((r.body && r.body.text) || '（这份改动没有内容）').split('\n');
      pre.innerHTML = _lines.map(ln => {
        let cls = '';
        if (ln.startsWith('+++') || ln.startsWith('---')) cls = 'dh';
        else if (ln.startsWith('+')) cls = 'da';
        else if (ln.startsWith('-')) cls = 'dd';
        else if (ln.startsWith('@@')) cls = 'dk';
        return cls ? '<span class="' + cls + '">' + esc(ln) + '</span>' : esc(ln);
      }).join('\n');
    }).catch(err => {
      const pre = det.querySelector('.diffload');
      if (pre) pre.textContent = '读不到：' + (err && err.message);
    });
  };
  const body = _procFold.querySelector('.procbody');
  body.appendChild(row);
  body.appendChild(det);
  _procLast = { key: _key, n: 1, ms: (e.elapsed_ms || 0), row, det };

  _procActs.push({ verb: _v, obj: _o });
  _procFold.querySelector('.psum').textContent = procSummary(_procActs, true);
}

// 一轮结束（它开始说话/收尾）⇒ 过程收起，只留那一行摘要。
// 上一轮的过程容器**就此封口**：不收、不搬，只断掉引用。
//
// ★ 为什么需要它（2026-10-27 实测，用户：「思考会滚到用户发言后面，有点割裂」）：
//   `closeProcFold()` 只在本轮 `say` / `end` 时执行，而 `distill`（记忆候选）
//   常常在 `end` 之后才到 —— 它会**新建**一个容器。这个尾巴容器没人关，于是
//   下一轮的 charter / 工具动作全被塞了进去；那张卡的位置又停在这句话的上方。
//   实测顺序：… 正文 → 卡（里面混着下一轮的工具）→ 你的第二句发言。
//   ⇒ 收到 `user`（新一轮开始）就断开引用，谁也别往上一轮的卡里写。
function detachProc() {
  if (!_procFold) return;
  _procFold = null;
  _procActs = [];
}

function closeProcFold() {
  if (!_procFold) return;
  _paintProcSum(false);
  // ★★ 有「等你决定」的卡片时**不许收起**（2026-10-27）。
  //   拦截卡现在住在过程容器里，一收就等于把按钮藏起来 ——
  //   而屏幕上只剩一句「它停在这里，等你决定上面那一步给不给」，
  //   人根本找不到点哪儿。用户原话：「这里也是有没有卡着也不清楚」。
  //   `_pendingBlock` 在 end 那段被消费成 false，所以下一轮照常收起。
  if (_pendingBlock) return;
  // ★ 再查一遍 DOM（2026-10-27）：重放历史时 `_pendingBlock` 的时序靠不住 ——
  //   它会被 end 那段消费成 false，可那张**卡还在容器里**。
  //   只要容器里还有「等你决定」的卡，就不能收：收起 = 把按钮藏起来。
  if (_procFold.querySelector('.blk')) return;
  // ★★ 收尾时**收起**（2026-10-27 改回来）。
  //   上一版（2026-10-25）故意不收起，理由是"WorkBuddy 是常驻一行一条"。
  //   但那条判断是在"只有两三行"的想象下拍的；用户打开一个真会话看到的是
  //   **11 条流水铺满一屏**，而自我这一轮只说了「现在处理 accept.py 的 d184 组：」一句。
  //   于是他的原话是：「自我的输出直接没了，替代的是一堆流程，显得画面特别脏」。
  //   ⇒ 分工改成：**跑的时候展开**（看得见它在干什么，可见性不打折），
  //     **说完了收起**（只留一行「5 类动作 · 11 步」，想看细节点一下）。
  //     过程属于"过程"，不该长期占着正文的地盘。
  _procFold.classList.remove('open');
  _procFold = null;
  _procActs = [];
}

function argsSummary(a) {
  if (!a) return '';
  const v = a.path || a.file || a.cmd || a.command || a.query || a.pattern || '';
  return String(v).slice(0, 56);
}

// 界面上不显示的参数。
// ★ `max_chars` 已经**不再截断**（2026-10-27 用户：「不要在读取和输出上做任何限制」）。
//   但模型有时候还会带它，而屏幕上出现一行「max_chars = 2500」，
//   人只会读成"你又限制了我" —— 参数已经没有意义，就别再显示它。
const HIDE_ARGS = new Set(['max_chars']);

function briefArgs(a) {
  if (!a) return '';
  if (typeof a === 'string') return a.slice(0, 160);
  const parts = [];
  for (const k in a) {
    if (HIDE_ARGS.has(k)) continue;
    let v = a[k];
    if (typeof v === 'string') v = v.length > 90 ? v.slice(0, 90) + '…' : v;
    else if (typeof v === 'object') v = JSON.stringify(v).slice(0, 90);
    parts.push(k + ' = ' + v);
  }
  return parts.join(' · ');
}

// 将后端事件分类为消息正文、过程动作、收尾和辅助状态；事件顺序以原日志为准。
function renderEvent(e) {
  const act = e.act || '?';

  if (act === 'user') {
    // ★ 空消息不渲染（2026-10-27）。「新对话」建出来的会话，任务书里只有一个换行，
    //   渲染出来就是一个**空的蓝气泡** —— 用户看到的是"我的话变成空白了"。
    //   它本来就不该占一行：没有内容的消息不是消息。
    if (!String(e.text || '').trim()) { return ''; }
    // ★ data-seq 是给"本我该挂在哪"用的（2026-10-27）：没有它，就分不清
    //   "这一轮的自我说过话没有"，本我只能往"最后一个 .body"上贴 ——
    //   而那个 .body 很可能就是这条用户消息。
    return `<div class="msg user" data-seq="${esc(String(e.seq || ''))}">`
         + `<div class="body md">${mdToHtml(e.text || '')}</div>`
         + `<div class="av">你</div></div>`;
  }
  if (act === 'say') {
    // ★ 角色标签（2026-10-29）：执行者 / 规划者 / 测试者。
    //   原来一律显示"AI + 模型名"，于是规划者和测试者的话看起来都像执行者说的。
    const _role = String(e.role || 'executor');
    const _who = (typeof ROLE_CN !== 'undefined' && ROLE_CN[_role]) || _role;
    // ★ HTML 注释**不显示**（`<!-- 测试者 · 由 review.py 生成 -->` 是生成器的元信息）。
    const _txt = String(e.text || '').replace(/<!--[\s\S]*?-->/g, '').trim();
    // ★ 外面这层 .turn 是**批注轨道**：正文占左边，右边那条竖列留给本我。
    //   原来本我单独占最右一栏（292px），正文被压到 630px，而且批注和它批的那段话
    //   隔着一整列，人得左右对眼神。用户原话：「say 的右边有留白，
    //   完全可以有本我插入批注的空间，没有利用起来」。
    // ★★ 用户：「头像和发言不要在同一行，以后直接头像单独一行。并且头像后面跟着
    //   现在自我所用的模型。这样可以给 say 更多的宽度，直接充满整个中心」。
    //   ⇒ 头像从"正文左边的 40px 栏"改成"正文上方的一行"，
    //     正文因此可以左对齐到 0、吃满剩余宽度（只让右边的批注）。
    // ★ data-seq 是**溯源锚点**（2026-10-26）：进度抽屉里点一条，就靠它找回这段话。
    //   没有锚点的话，"点了没反应"或者"滚到差不多的地方"—— 后者更糟，
    //   因为它看起来成功了，但指向的不是那一段。
    return `<div class="turn" data-seq="${esc(String(e.seq || ''))}" data-role="${esc(_role)}"><div class="msg agent">`
         + `<div class="mhead"><span class="av">AI</span>`
         + `<span class="mname">${esc(_who)}</span></div>`
         + `<div class="body md">${mdToHtml(_txt)}`
         // ★ 截断了必须说出来。否则人看到半句话，分不清是模型不会写还是我们截的
         //   —— 这两种原因的处置完全不同，混在一起就只能猜。
         + (e.truncated
             ? `<div class="truncnote">正文共 ${Number(e.full_len || 0).toLocaleString()} 字，`
               + `这里只显示前 2 万字（后面被截断，不是模型没写完）</div>` : '')
         // ★ 计时跟着这条回复走（2026-10-26 用户：「计时器可否放在 say 里面，这样更直观」）。
         //   原来只有底部一个小标签在跳秒，回看历史时完全不知道"这一段花了多久" ——
         //   而耗时恰恰是判断"它在想还是在卡"的唯一可核参照。
         + ((e.t || _turnMs)
             ? `<div class="ts">${esc(e.t || '')}`
               + (_turnMs ? `${e.t ? ' · ' : ''}用时 ${_mmss(_turnMs)}` : '')
               + `</div>` : '')
         + `</div></div><aside class="bncol"></aside></div>`;
  }
  if (act === 'hook') {
    // ★ 钩子的结果要显形（2026-10-26）。不显示的话，"我配的 hook 到底跑没跑"
    //   就只能靠猜 —— 而它是个外部命令，失败起来一点痕迹都没有。
    const _hok = e.ok !== false;
    const _hb = String(e.out || e.err || '').replace(/\s+/g, ' ').trim().slice(0, 110);
    return `<div class="line ${_hok ? '' : 'badline'}">`
         + `<span class="spk spk-h">钩子</span>${esc(e.event || '')}`
         + (e.command ? ` · ${esc(String(e.command).slice(0, 44))}` : '')
         + (_hb ? `｜${esc(_hb)}` : '')
         + `</div>`;
  }
  if (act === 'prep') {
    // 还没产出任何东西时的第一句话。它不是"结果"，是"它已经在动了" ——
    // 没有这一句，起草契约的那十几秒就是一片空白。
    return `<div class="line">${esc(e.note || e.line || '准备中…')}</div>`;
  }
  if (act === 'changes') {
    // ★ 改动清单（2026-10-27）。原来它掉进兜底，界面上只剩一句
    //   「这一轮工作区里真正变了的文件」——**没有一个文件名**，等于没说。
    //   现在给一行计数 + 悬停看文件名，进过程卡（它是过程信息，不是结论）。
    const _c = (e.changed || []).length, _a = (e.added || []).length,
          _r = (e.removed || []).length;
    const _all = [...(e.changed || []), ...(e.added || []), ...(e.removed || [])];
    const _tip = _all.slice(0, 20).join('\n') + (_all.length > 20 ? '\n…' : '');
    const _txt = '改动清单：改 ' + _c + ' · 新增 ' + _a + ' · 删 ' + _r;
    procAppendTail(`<div class="line" title="${esc(_tip)}">${esc(_txt)}`
      + (_all.length ? `　${esc(_all.slice(0, 3).join('、'))}${_all.length > 3 ? '…' : ''}` : '')
      + `</div>`);
    return '';
  }
  if (act === 'distill') {
    // ★ 候选必须显形，并且要写清"还没进树"。它是提议不是结论 ——
    //   机器不写树（树每轮注入，写错一条就每轮带偏），所以这块的措辞
    //   不能像"已完成"，否则人会以为树已经变了。
    if (e.skipped) {
      return `<div class="line">提炼没跑 · ${esc(e.reason || '')}</div>`;
    }
    const its = e.items || [];
    if (!its.length) {
      return `<div class="line">提炼：0 条（${esc(e.reason || '这一轮没有值得进树的东西')}）</div>`;
    }
    return `<div class="distillblock flowblock"><div class="dshead"><span class="spk spk-d">记忆</span>这轮想进树的 ${its.length} 条 · 还没进，等你点头`
         + (e.model ? `<span class="dsmodel">${esc(e.model)}</span>` : '') + `</div>`
         + its.map(it => `<div class="dsrow"><span class="dslayer">${esc(it.layer || '')}</span>`
             + `<div><div class="dsrule">${esc(it.rule || '')}</div>`
             + (it.why ? `<div class="dswhy">为什么：${esc(it.why)}</div>` : '')
             + `</div></div>`).join('')
         + `<div class="dscmd">收：<code>python bin/distill.py accept ${esc(e.inbox || '')}</code></div>`
         + `</div>`;
  }
  if (act === 'contract') {
    // ★ 契约要显形：它决定"这一轮按什么判完成"，藏起来就等于判据是隐形的。
    if (e.ok === false) {
      return `<div class="line badline">契约没起草出来 · ${esc(e.reason || '')}`
           + `（这一轮只能用代理指标：判的是「做了多少」）</div>`;
    }
    const items = e.items || [];
    return `<div class="contractblock"><div class="cthead">这一轮按什么判完成 · ${items.length} 条`
         + `${e.locked ? ' · 已锁' : ' · 未锁'}</div>`
         + items.map((d, i) => `<div class="ctrow">`
             + `<span class="ctid">${esc(d.id || 'r' + (i + 1))}</span>`
             + `<span class="ctclaim">${esc(d.claim || '')}</span>`
             + `<code class="ctcheck" title="${esc(d.check || '')}">${esc(d.check || '')}</code>`
             + `</div>`).join('')
         + `</div>`;
  }
  if (act === 'llm') {
    // 思考不在这里渲染：一轮里的每一段都要并进**同一个**思考块
    // （见 appendThink）。这里只留空，真正的挂载在轮询里做。
    return '';
  }
  if (act === 'tool') {
    // ★ 工具调用不在这里渲染：它们要**并成一组**，共用一行人话摘要
    //   （WorkBuddy 的 metaFold：head 是 15px 灰字的一行摘要，body 才是每一步）。
    //   一条一个卡片的做法把过程切成碎片，整次工作看起来像几件不相干的事。
    //   真正的挂载在 mountTool()。
    return '';
  }
  if (act === 'denied') {
    return `<div class="procrow bad" style="margin-left:0;padding-left:12px">`
         + `<span class="pverb">被门禁拦下</span>`
         + `<span class="pobj">${esc(cn(e.tool))} · ${esc(e.why || '')}</span></div>`;
  }
  if (act === 'verify') {
    // ★ 每轮一条"判据通过"。10 轮就是 10 行一模一样的字 —— 那不是信息，是噪音。
    //   只有**没通过**才单独占一行：那才是人要停下来看的。
    if (e.ok) { noteToProc('判据通过' + (e.reason ? ' · ' + e.reason : '')); return ''; }
    // ★ 而且没通过也别把 200 字原因铺在流里（那是一条命令 + 一段契约原文）。
    //   留一句准话 + 前 60 字，全文进 title。
    const why = String(e.reason || '');
    return `<div class="line badline" title="${esc(why)}">判据没通过 · `
         + `${esc(why.slice(0, 60))}${why.length > 60 ? '…' : ''}</div>`;
  }
  if (act === 'charter') {
    // 顶栏已经写着"总纲 full · 3254 字"，这里再来一行"总纲 13993 字 · 本轮注入"
    // 就是同一件事说两遍 —— 而且是每轮说一遍。收进过程里。
    noteToProc(`总纲 ${e.chars || 0} 字 · ${e.note || ''}`);
    return '';
  }
  if (act === 'ask_human') {
    // ★ 已经改成**弹窗**（见 showAsk）。这里不再往流里插回答框 ——
    //   两处入口会让人不知道该在哪答，也会让"它到底停没停"变得看不出来。
    //   真正的弹窗由渲染循环拦下来打开（renderEvent 只负责画流，不负责开框）。
    return '';
  }
  if (act === 'benni') {
    // 本我走到这里说明正文已经在 mountBenni 里挂好了（它要按锚点插到前面去，
    // 不能像别的事件那样顺序追加）。这里只留一条兜底提示。
    return '';
  }
  if (act === 'end') {
    // 这一轮发完就把按钮上的百分比更新掉（面板不用打开）——
    // 数字烂在那儿不更新，就是这个功能最容易被讨厌的样子。
    refreshCtx();
    // ★★ needs_human 不该长成"结束：needs_human · 7 轮 · 它在问你：……"
    //   那是一行结论，人读到的是"它做完了，顺便问了一句"。
    //   真实情况正相反：**它什么都没做，卡在这里等人**。
    //   ⇒ 单独一种呈现：一句话说清"在等你"，并给一个把弹窗重新叫出来的入口。
    if (e.status === 'needs_human') {
      // ★ 被闸门拦下的那种，人要做的是"在上面那张卡上点一下"，不是回答问题。
      //   给一个「打开问题」按钮等于给错入口 —— 点了什么都没有。
      // ★★ 被闸门拦下的那种：**不要再补一条"它停在这里"**（2026-10-27 做减法）。
      //   那张卡自己头一行就写着「拦住了一步 · 命令 …」，下面三个按钮 ——
      //   再补一条只说"等你决定上面那一步"的横幅，就是在同一屏里说两遍同一件事，
      //   而第二遍没有按钮，人反而会去点那句空话。
      //   用户原话：「一堆输出，但是有用信息为 0」。
      if (_pendingBlock) {
        _pendingBlock = false;                 // 只用一次：后面的轮次恢复正常
        return '';
      }
      return `<div class="waitbar"><span class="wdot"></span>`
           + `<span class="wtxt">它停在这里，等你回话（没有超时，不会自己往下走）</span>`
           + `<button class="reopen">打开问题</button></div>`;
    }
    if (e.status === 'paused') {
      return `<div class="endblock"><span style="color:var(--dim)">已暂停</span>`
           + ` · 已完成 ${e.turns || 0} 个执行步`
           + (e.why ? ' · ' + esc(String(e.why).slice(0, 100)) : '') + `</div>`
           + renderSummary();
    }
    const ok = e.status === 'success';
    // ★★★ 一个模型调用都没成功 ⇒ 必须说「没连上」，不能说「它没说话」（2026-10-27）。
    //   用户截图里那句「这一轮它没有交代（只动了手，没说话）」就是**错的**：
    //   事件流里是 `error: 连不上或超时 WinError 10054`、`turns=0` ——
    //   它一步都没动，是一个请求都没发出去。
    //   这两件事对用户的意义完全相反：一个"它偷懒"，一个"网络断了，再发一次就行"。
    //   ⇒ 我这句提示原本放在所有分支之前，把真正的错误盖住了。以下三档按此排序。
    if (!ok && e.status === 'error' && !Number(e.turns || 0)) {
      const _w = String(e.why || '');
      return `<div class="line badline">没连上模型 —— 这一轮**一个调用都没发出去**`
           + (_w ? `：${esc(_w.slice(0, 90))}` : '')
           + `　· 再发一次通常就好；连着几次都这样的话，`
           + `先看本机代理（HTTP_PROXY）是不是把连接掐了</div>`;
    }
    // ★★ 正常跑完却一句话没说 ⇒ 明说（2026-10-27）。
    //   用户报的是「我根本就看不到任何正文」——查下来有一轮模型真的只说了 24 个字，
    //   还有一轮只动了手、一个字没说。界面上如果什么都不写，人只会以为界面坏了。
    //   这不是替它解释，是把"它没交代"这件事本身说出来 —— 沉默失败必须显形。
    if (ok && !_turnHadSay) {
      return `<div class="line silent">这一轮它没有交代（只动了手，没说话）`
           + ` · 想看做了什么就点上面那一行</div>`;
    }
    // ★ 状态词中文化。"结束：success · 10 轮"里 success 是给机器看的字段，
    //   人读到的是一个英文单词夹在中文里，像是没做完的半成品。
    const word = ({ success: '跑完了', paused: '已暂停', error: '出错了',
                    stuck: '原地打转', blocked: '被拦住', failed: '没跑成' })[e.status] || e.status || '';
    // 原因只留前 90 字：原来 160 字会把这一块撑成三行，看起来像结论，其实大多是契约原文。
    const why = String(e.why || '');
    // ★★ 不显示"· N 轮"。它是**模型调用次数**（一轮调工具、一轮给结论 = 2 轮），
    //   而用户看到"2 轮"以为是"两轮判定"——那不是同一件事，界面也解释不了。
    //   改成说"验收过没过"这件人真正关心的事；轮数放 title，要查的人能看到。
    const unmet = /判据未过|验收没过/.test(why);
    const line = unmet ? (word + ' · 验收没过') : word;
    // ★ 用户：「不要保留这种块状的范围判定了，用不用字体颜色做区分就行」。
    //   ⇒ 去掉底色和圆角，只留一行小字，靠颜色分好坏。
    const _h = `<div class="endline ${(ok && !unmet) ? 'ok' : 'bad'}"`
         + ` title="${esc(why)}｜共 ${e.turns || 0} 次模型调用">`
         // ★ 这一行原来只有"跑完了 · 30.3K token"，接在正文末尾 —— 读者不知道
         //   这是谁的结论、是正文还是附注。给一个"收尾"标签，和上面"本我""记忆"
         //   同一套写法，正文里就分得清谁在说话。
         + '<span class="spk spk-e">收尾</span>'
         + esc(line)
         // ★★ 理由不再挤进这一行（2026-10-26）。
         //   `line` 已经说了"验收没过"，具体是哪条不过、命令怎么失败的，
         //   上面契约卡里逐条列着（人话 + 原文 + 退出码），这里再截一段
         //   只能是"契约没过（contra…"这种半截话 —— 既占地方又不解决问题。
         //   全文仍然在 title 里，悬停能看全。
         // ★ token 直接挂在这一行上：用户说"token 的表述直接没了"，
         //   而它原来只存在于 summary 里 —— summary 一旦没渲染（比如这一轮
         //   没有 say 事件可挂），token 就跟着消失了。挂在状态行上不会丢。
         + ` · <span class="sumtk">${esc(_tkStr())}</span>`
         + `</div>` + renderSummary();
    // ★★ 「本我加判据加想收进树的这些都进 say 里面」——不再挂在执行流外面，
    //   直接接在最后一条 say 的正文后面。
    const _b = [...$('feed').querySelectorAll('.turn .msg .body')].pop();
    if (_b) _b.insertAdjacentHTML('beforeend', _h);
    else $('feed').insertAdjacentHTML('beforeend', _h);
    return '';
  }
  if (['tools_batch'].includes(act)) return '';      // 只是聚合信息，不占屏
  // ★★ 兜底：未单独处理的事件**默认不占正文**，塞进过程折叠。
  //   原来是把 `e.line`（后端压成的一行摘要，形如 `[616] 21:37:10 verify_overruled …`）
  //   当正文铺出来 —— 于是执行流里混着大量"日志"，人读到的是一堆编号和时间戳。
  //   ★ 但有几种**必须显形**：出事的时候藏起来，人就只能看到一个安静的空屏。
  if (['error', 'blocked', 'stuck', 'interrupted', 'status'].includes(act)) {
    return `<div class="line ${act === 'status' ? '' : 'badline'}">`
         + esc(e.line || e.text || e.why || act) + `</div>`;
  }
  noteToProc(e.line || e.text || act);
  return '';
}

// 状态栏：模型、累计 token、门禁、当前会话
// tokens = 自我花掉的；bTokens = **本我**花掉的（2026-10-25 起分开记）。
// 之前只有一个数字，而它只累加 llm 事件 —— 本我的消耗一个字都没进去，
// 于是"本我是不是在干活"这个问题从来没有数据可答。
let _stat = { model: '', tokens: 0, bTokens: 0, bCalls: 0, bRechecks: 0 };
// 把 token 数量压成 "30.3K" 这种短写法（12.5px 的一行里放得下）
function _shortTk(n) {
  const tk = Number(n || 0);
  if (tk >= 10000) return (tk / 1000).toFixed(1) + 'K';
  return String(tk);
}

function _tkStr() {
  const tk = Number(_stat.tokens || 0);
  const bt = Number(_stat.bTokens || 0);
  // 「— token」在低分辨率下看着像三个撇，而且也不说清是「没有」还是「没统计」
  if (!tk && !bt) return 'token 未统计';
  // 本我 0 的时候不显示 —— "本我 0" 是会误导的（它可能只是这轮没醒）
  return bt ? `${_shortTk(tk)} + 测试者 ${_shortTk(bt)} token` : `${_shortTk(tk)} token`;
}
let _policy = '';
// 最后一条已渲染的 say 正文。用来判断"流式草稿是不是已经变成正式事件了" ——
// 相等就说明这轮结束、正文已进事件流，临时气泡该撤了。
// （不再有按字数自动收起这个阈值：长思考由正文的 max-height:200px 自己滚解决。）
let _lastSay = '';
// 这一轮碰过的工具，用来在收尾时算出"它到底做了什么"
let _tools = [];
// 工程状态（台账）用的两本账：改过哪些文件、跑过哪些检查。
let _edits = [];
let _checks = [];
let _lastVerify = null;
// 工作节点：每次它说一段话记一条，用来填右栏「总况」
let _nodes = [];

// 重放与实时渲染共用事件计数来源，统计只按识别到的事实更新。
function tallyEvent(e) {
  if (e.act === 'say' && (e.text || '').trim()) {
    // 每次它说一段话 = 完成一小节。这是"工作节点"的来源：
    // 人看总况就知道走到哪了，不用读事件流。
    _nodes.push({ t: e.t || '', seq: e.seq || 0,
                  text: (e.text || '').replace(/\s+/g, ' ').trim() });
    paintNodes();
  }
  if (e.act === 'tool') {
    _tools.push({ tool: e.tool || '', arg: argsSummary(e.args), ok: e.ok !== false });
    // 工程状态要用的两样：改过哪些文件、跑过哪些检查。
    // ★ 从已读的事件里算，不另存一份 —— 多一个真源就多一处会漂。
    const _a = e.args || {};
    if (e.tool === 'write_file' || e.tool === 'edit_file') {
      if (_a.path && !_edits.includes(_a.path)) _edits.push(_a.path);
    }
    if (e.tool === 'run_check') {
      const c = Array.isArray(_a.cmd) ? _a.cmd.join(' ') : String(_a.cmd || '');
      _checks.push({ cmd: c, ok: e.ok !== false, rc: (e.out || '').match(/退出码\s*(-?\d+)/) });
    }
  }
  if (e.act === 'verify') _lastVerify = e;
  if (e.act === 'benni') {
    // 本我自己的账：它调了几次模型、复跑了几条命令、花了多少 token。
    // 只取最后一次的快照（后端每轮累计值，不是增量）—— 所以用 max 而不是累加。
    if (typeof e.tokens === 'number') _stat.bTokens = Math.max(_stat.bTokens || 0, e.tokens);
    if (typeof e.calls === 'number') _stat.bCalls = Math.max(_stat.bCalls || 0, e.calls);
    if (typeof e.rechecks === 'number') _stat.bRechecks = Math.max(_stat.bRechecks || 0, e.rechecks);
  }
  if (e.act === 'llm') {
    if (e.model) _stat.model = e.model;
    _stat.calls = (_stat.calls || 0) + 1;
    _stat.tokens += Number(e.total_tokens || 0) || 0;
  }
  paintStatusBar();
}

function paintStatusBar() {
  const bits = [];
  if (_stat.model) bits.push('模型 ' + _stat.model);
  if (_stat.tokens) bits.push('累计 ' + _stat.tokens + ' token');
  // ★ 状态栏不写"门禁 workspace"和会话目录名 —— 顶栏已经有权限，左边已经有会话。
  //   这一行只留"谁在跑、花了多少"，那是人真正会看的。
  $('sbar').textContent = bits.join('   ·   ');
}

// ★ "已经喊停、但进程还没退" 是一个**独立状态**，不能和"在跑"共用一套界面。
//   原来点完暂停只改了左下角一行字（#state），右上角照样是"在跑"、按钮照样可点 ——
//   人只会得出"我点了，它没反应"。而他真正想知道的是"我的请求收到了没有"。
let _stopping = false;
// 正在提交这一句（从按下到服务端收下之间有几百毫秒，这期间再按一次回车
// 会建出第二个会话 —— 这是"莫名其妙多出一个会话"的一种成因）
let _starting = false;

// 由会话实际进程状态驱动按钮和输入区，跨运行切换清理上一轮的瞬时标记。
function setRunning(v) {
  running = v;
  // ★ 测试者在跑时**不要重启执行者的秒表**（2026-10-29 联调时用户报：
  //   「执行者停下来了，测试者自动接入，结果执行者和测试者一起跑」）。
  //   真相：执行者 `end` 时 `_selfSince` 已清零，但这个函数每 3 秒被调一次
  //   （loadState），而会话**整体**还在跑（轮到测试者了），
  //   于是 `_selfSince = 0 || Date.now()` 又把表起回来 —— 界面上两个表同时走。
  //   按三条 loop 串行的模型，执行者停了就是停了，不能因为"会话还没完"就再起。
  if (v && !_benniSince) _selfSince = _selfSince || Date.now();
  if (!v) {
    _stopping = false;               // 进程真退了 ⇒ 暂停这件事完成
    // ★ 自我也一起结账（2026-10-26）：原来只清 _stopping，_selfSince 留着不动 ——
    //   于是"进程已经退了"之后，标签会一直挂着「收尾中 · 自我 1m32s」不动。
    //   正常路径上 end 事件会清它，但 end 有可能不来（任务秒退、/say 没送到、
    //   重放历史时 end 已经在 seen 之前）。**靠一个可能不到的事件来收尾，等于没收尾。**
    //   本我还在跑时不清：那种情况下"收尾中"正是要说的事。
    if (!_benniSince && _selfSince) {
      _lastSelfMs = Date.now() - _selfSince;      // 留个脚印给"上轮"
      _selfSince = 0;
    }
  }
  const stopping = _stopping;
  $('stop').disabled = !v || stopping;
  $('go').disabled = v;
  $('stop').textContent = stopping ? '正在暂停…' : '暂停';
  // ★ 状态标签的文字只在 paintPill 里写（2026-10-26）。
  //   这里原来先手写一遍再调 paintPill，等于同一件事两个出口 ——
  //   一旦两边的判断不一致，屏幕上就是"错的先出现、对的随后覆盖"，看起来像闪。
  paintPill();
}


