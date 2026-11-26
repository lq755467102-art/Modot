// ── 代码高亮（Shiki）──
// 用 VS Code 的语法文件和 oniguruma 引擎，着色准（嵌套、模板字符串、正则都对）。
// ★ wasm 是**内联**在 bundle 里的：独立 wasm 要靠 fetch 取，而 file:// 下会被拒，
//   那样高亮会静默失效、代码块退回纯文本 —— 比没有高亮更难查。
// ── 代码高亮：一个够用的逐字符扫描器 ──
// ★ 为什么不用 shiki：`window.SHIKI` 从来没有被加载过（没有 script 标签、没有 vendor 目录），
//   所以 initShiki() 一直在控制台 warn "shiki bundle 没加载"，代码块一直是纯文本 ——
//   用户看到的"代码块太丑"就是这个。而 shiki 整套（语法 + 主题）好几 MB，
//   我们只需要给 python/js/json/bash 上色。
//
// ★ 为什么逐字符扫而不是一串正则：正则套正则最容易在"字符串里有关键字"
//   （`print("# 不是注释")`）和"注释里有引号"（`# 他说"好"`）上出错，
//   而这种错在界面上表现为"颜色错位"，很难发现是自己切错了。
//   状态机只有三种状态，读起来慢一点，但不会错。
// 主要引用或加载入口包括 `desktop/renderer/index.html`。
const HL_KW = new Set(('def class return if elif else for while try except finally with as '
  + 'import from pass raise yield lambda and or not in is None True False async await '
  + 'global nonlocal assert del break continue match case '
  + 'const let var function new this typeof instanceof of switch do throw extends super '
  + 'export default delete void NULL null undefined true false '
  + 'echo cd export source fi then done esac set unset local readonly '
  + 'SELECT FROM WHERE INSERT UPDATE DELETE JOIN GROUP ORDER BY LIMIT').split(' '));

function hlCode(code, lang) {
  const L = String(lang || '').toLowerCase();
  const hash = /py|sh|bash|shell|zsh|yaml|yml|toml|ini|conf|env|r$|rb|perl/.test(L);
  const slash = /js|ts|javascript|typescript|json|css|c$|cpp|java|go|rust|php/.test(L);
  const out = [];
  const esc0 = t => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const buf = [];
  const flush = () => { if (buf.length) { out.push(esc0(buf.join(''))); buf.length = 0; } };
  const push = (cls, t) => { flush(); out.push(`<span class="hl-${cls}">${esc0(t)}</span>`); };
  let i = 0, n = code.length;

  while (i < n) {
    const c = code[i], c2 = code.slice(i, i + 2);
    // 行注释
    if ((hash && c === '#') || (slash && c2 === '//')) {
      let j = i;
      while (j < n && code[j] !== '\n') j++;
      push('cm', code.slice(i, j));
      i = j;
      continue;
    }
    // 块注释
    if (slash && c2 === '/*') {
      const j = code.indexOf('*/', i + 2);
      const end = j < 0 ? n : j + 2;
      push('cm', code.slice(i, end));
      i = end;
      continue;
    }
    // 字符串（含转义）
    if (c === '"' || c === "'" || c === '`') {
      let j = i + 1;
      while (j < n) {
        if (code[j] === '\\') { j += 2; continue; }
        if (code[j] === c) { j++; break; }
        if (code[j] === '\n' && c !== '`') break;   // 单双引号不跨行
        j++;
      }
      push('s', code.slice(i, j));
      i = j;
      continue;
    }
    // 数字
    if (/[0-9]/.test(c) && !/[\w]/.test(code[i - 1] || '')) {
      let j = i;
      while (j < n && /[0-9a-fA-FxX._]/.test(code[j])) j++;
      push('n', code.slice(i, j));
      i = j;
      continue;
    }
    // 标识符
    if (/[A-Za-z_$]/.test(c)) {
      let j = i;
      while (j < n && /[\w$]/.test(code[j])) j++;
      const w = code.slice(i, j);
      if (HL_KW.has(w)) push('k', w);
      else if (code[j] === '(') push('f', w);       // 后面跟括号 ⇒ 当函数名
      else buf.push(w);
      i = j;
      continue;
    }
    buf.push(c);
    i++;
  }
  flush();
  return out.join('');
}

async function initShiki() {
  // 名字保留（调用点不用改），但**已经不用 shiki** 了 —— 见 hlCode 上面那段说明。
  highlightNew();
}

function highlightNew() {
  const feed = $('feed');
  if (!feed) return;
  feed.querySelectorAll('.codebox:not([data-hl])').forEach(box => {
    box.dataset.hl = '1';
    const code = box.querySelector('code');
    if (!code) return;
    const m = /language-([\w+#-]+)/.exec(code.className || '');
    try {
      code.innerHTML = hlCode(code.textContent, m ? m[1] : '');
    } catch (e) {
      // 上色失败就当纯文本，绝不能让一块代码把整条渲染带崩
      code.textContent = code.textContent;
    }
    return;
  });
}

// ★ 绑定一律走 on()：元素不在就跳过并报出来，**不让一个空引用把整段脚本带走**。
//   实测踩过：dialog 写在脚本后面 ⇒ $('cfgsave') 是 null ⇒ 赋值抛错 ⇒
//   后面所有绑定和末尾的「启动即加载」全没执行（表现为：设置关不掉 + 首屏空白）。
on('go', 'onclick', start);
on('newchat', 'onclick', newSession);
// ── 技能分页的入口与动作 ──
// ── 进度抽屉（2026-10-26）──
// 点一条 = 回到那次发言。委托绑在容器上：节点是每次 paintNodes 重建的，
// 绑在节点本身上第二帧就全丢了。
$('nodes').addEventListener('click', ev => {
  const n = ev.target.closest('.node');
  if (n) gotoNode(n.dataset.seq);
});
// 展开/收起。★ 用 mouseenter/mouseleave 在 .railside 上：
//   抽屉是它的子元素，鼠标移进抽屉不会触发 leave —— 所以划过节点时不会突然收回去。
(() => {
  const side = $('railside');
  if (!side) return;
  side.addEventListener('mouseenter', () => side.classList.add('open'));
  side.addEventListener('mouseleave', () => {
    if (!side.classList.contains('pin')) side.classList.remove('open');
  });
})();
on('railpin', 'onclick', () => {
  const r = $('railside');
  if (!r) return;
  r.classList.toggle('pin');
  const btn = $('railpin');
  if (btn) btn.textContent = r.classList.contains('pin') ? '取消钉住' : '钉住';
  // ★ 取消钉住时，如果鼠标已经不在抽屉上，就要立刻收起来 ——
  //   否则它一直开着，而人以为自己已经关掉了（按钮也写着"钉住"了）。
  if (!r.classList.contains('pin') && !r.matches(':hover')) {
    r.classList.remove('open');
  }
});

on('openskills', 'onclick', () => skillMode(true));
$('sptabs').addEventListener('click', ev => {
  const b = ev.target.closest('.sptab');
  if (b) spTab(b.dataset.tab);
});
on('spclose', 'onclick', () => skillMode(false));
on('spinstall', 'onclick', () => $('imgpick').click());
$('splist').addEventListener('click', ev => {
  const card = ev.target.closest('button.skcard');
  if (card) showExtensionDetail(card);
});
refreshSkillCount();
on('addproj', 'onclick', addProject);

// ── 搜索：本地过滤，防抖 120ms ──
// ★ 为什么防抖而不是每个 input 都重建：重建会重写整个列表的 innerHTML，
//   连着敲五个字就是五次全量重排，光标位置和滚动都会被弹掉，手感是"打字卡"。
$('sessq').addEventListener('input', () => {
  _sessQ = $('sessq').value;
  _sessStruct = '';                // 搜索词变了 ⇒ 必须重建，不能走增量更新
  if (_sessQTimer) clearTimeout(_sessQTimer);
  _sessQTimer = setTimeout(() => { _sessQTimer = 0; loadSessions(); }, 120);
});

// ── 「⋯」菜单 ──
$('sessmenu').addEventListener('click', ev => {
  const b = ev.target.closest('button');
  if (!b) return;
  const act = b.dataset.act, id = _menuSid;
  hideSessMenu();
  if (!id) return;
  if (act === 'pin') togglePin(id);
  else if (act === 'rename') renameSession(id);
  else if (act === 'delete') delSession(id);
});
// 点别处 / 按 Esc / 滚动列表都收起来 —— 一个浮层留着自己不走是最烦的
document.addEventListener('click', ev => {
  const m = $('sessmenu');
  if (!m || m.hidden) return;
  if (ev.target.closest && (ev.target.closest('#sessmenu') || ev.target.closest('.more'))) return;
  hideSessMenu();
}, true);
document.addEventListener('keydown', ev => { if (ev.key === 'Escape') hideSessMenu(); });
$('sessions').addEventListener('scroll', hideSessMenu, true);
window.addEventListener('resize', hideSessMenu);
// ★ 模型这一行（2026-10-26）：两个下拉 + 一个互换 + 一个档案入口。
//   `on()` 是"元素可能还没解析出来"的那个安全绑定，别改成直接 addEventListener。
on('mcfg', 'onclick', openCfg);
on('mswap', 'onclick', swapModels);
// ★ 浮窗开关（2026-10-27）：点按钮展开/收起，点外面任何地方收起。
//   不点外面也能关（再点一次按钮），因为"只能靠点空白关掉"在触控板上很难受。
on('mset', 'onclick', (ev) => {
  ev.stopPropagation();
  const row = document.querySelector('.mrow');
  if (!row) return;
  const open = !row.classList.contains('open');
  row.classList.toggle('open', open);
  if (open) { const s = $('mself'); if (s) s.focus(); }
});
document.addEventListener('click', (ev) => {
  const row = document.querySelector('.mrow');
  if (!row || !row.classList.contains('open')) return;
  if (row.contains(ev.target) || (ev.target.closest && ev.target.closest('#mset'))) return;
  row.classList.remove('open');
});
on('mself', 'onchange', () => pickModel('main', $('mself').value));
on('mreview', 'onchange', () => pickModel('review', $('mreview').value));
// ★ 规划者也要存（2026-10-31 盘点发现）：下拉框一直在、也能选，
//   但**没绑事件** —— 界面上改了规划者模型，既不发请求也不持久化，刷新就回退。
on('mplan', 'onchange', () => pickModel('plan', $('mplan').value));

// ★ 权限档位（2026-10-27 用户：这个按钮要能直接下拉选）。三档里只有 trusted
//   要再确认一次 —— 它等于「不再逐条问你」，点错一次就是整轮不问。
//   失败要把下拉**摆回原来的值**，否则界面会显示一个没生效的档位（假成功）。
on('cgatesel', 'onchange', async () => {
  const sel = $('cgatesel');
  const want = sel.value;
  const before = _policy || 'workspace';
  if (want === 'trusted' && !window.confirm(
      '换成「信任 · 底线仍在」？\n\n'
      + '· 它等于：不再逐条问你\n'
      + '· 底线仍然拦：凭据读写、写系统目录、改放权配置\n'
      + '· 只影响后面开始的轮次，正在跑的不会变')) {
    sel.value = before;
    return;
  }
  // ★ 路径不能再带 /api —— `api()` 的前缀里已经有 `/api` 了，
  //   写成 `/api/policy` 会打到 `/api/api/policy`（404）。
  //   「改不了这一档」的真因就是这个，跟 r.ok 那个一起构成双重挡板。
  const r = await api('/policy', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ policy: want, confirm: want === 'trusted' })
  });
  // ★ api() 返回的是 { status, body }，不是后端的响应本身 ——
  //   这里原来判断 `r.ok`，那永远是 undefined，于是**不管后端成没成都报
  //   「改不了这一档」**（用户截图里就是这条）。要看 r.body.ok。
  const _pb = (r && r.body) || {};
  if (!r || r.status !== 200 || _pb.ok !== true) {
    sel.value = before;
    showErr(_pb.error || _pb.why || '改不了这一档');
    return;
  }
  _policy = _pb.policy || want;
  sel.value = _policy;
  const lbl = $('cgatestate');
  if (lbl) lbl.title = '下次运行的默认权限（「' + labelOf(_policy) + '」）。'
    + '改到「信任」会先确认一次 —— 底线清单仍然拦凭据读写、写系统目录、改放权配置这三类。';
  // 提示写在输入区那一行的小字上（`#chint` 原来没人用，正好是它的位置），5 秒后自己消失。
  const hint = $('chint');
  if (hint && _pb.note) {
    hint.textContent = _pb.note;
    setTimeout(() => { if (hint.textContent === _pb.note) hint.textContent = ''; }, 5000);
  }
});
on('cfgsave', 'onclick', saveCfg);
on('cfgimport', 'onclick', importCfg);
on('cfgcancel', 'onclick', () => { _clearEdit(); $('cfgdialog').close(); });

// ★ 点开就刷新一次再画：面板的价值全在"这一次是真的"。
//   拿上一次的缓存画，人会看着一个过期的百分比做决定。
on('ctxbtn', 'onclick', async () => {
  await refreshCtx();
  renderCtx();
  syncLimit();
  const d = $('ctxdialog');
  if (d) { d.showModal(); }
});
on('ctxclose', 'onclick', () => { const d = $('ctxdialog'); if (d) { d.close(); } });

// ★ 拉条（2026-10-27）：拖到哪一档就改哪一档，立刻重算百分比。
//   用 `input` 而不是 `change` —— 拖着的时候就要跟着动，松手才变等于没反馈。
// ★ 事件名必须是 `oninput` 这种**属性名**，不是 `input`（2026-10-27 自测逮到）：
//   `on()` 的实现是 `el[ev] = fn`，写 `'input'` 等于给元素挂一个谁都不读的属性 ——
//   不报错、也不生效，表现就是"拖拉条没反应"。同一个坑全项目只此一处，已加判据守住。
on('limit_hit', 'oninput', () => { setLimit($('limit_hit').value); });

// ★ 拉取可用模型（2026-10-27）：让人从端点给的清单里挑，而不是凭记忆填模型名。
//   ★ 拉不到不算错 —— 有些网关不实现 /models，手填仍然有效。所以提示写着"可以手填"。
on('cprobe', 'onclick', async () => {
  const btn = $('cprobe'), hint = $('cprobe_hint');
  if (!btn || !hint) { return; }
  const body = {
    act: 'probe',
    id: (($('c_id') && $('c_id').value) || '').trim(),
    base_url: (($('c_base') && $('c_base').value) || '').trim(),
    api_key: (($('c_key') && $('c_key').value) || '').trim()
  };
  const keep = hint.textContent;
  btn.disabled = true;
  hint.textContent = '问端点中…';
  try {
    const r = await api('/models', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const d = (r && r.body) || {};
    if (!d.ok) {
      hint.textContent = (d.why || '拉不到') + '（可以直接手填模型名）';
      return;
    }
    $('c_modellist').innerHTML = (d.models || []).map(m =>
      '<option value="' + esc(m) + '"></option>').join('');
    hint.textContent = '拉到 ' + d.count + ' 个 · ' + (d.source || '')
      + '；点输入框挑，也可以手填';
    // 模型名还空着就填第一个：少一次没必要的输入，而且填的一定是真实存在的名字
    if (!$('c_model').value && (d.models || []).length) {
      $('c_model').value = d.models[0];
    }
  } catch (e) {
    hint.textContent = '拉不到：' + e + '（可以直接手填模型名）';
    void keep;
  } finally {
    btn.disabled = false;
  }
});
on('proj', 'onchange', () => {
  curProj = $('proj').value;
  localStorage.setItem('harness.proj', curProj);
  stateNote('新会话会放在 ' + shortPath(curProj), 6000);
});
// ── 左栏宽度可拖 ──
// ★ 用户要"界面可以自己调整容器的大小，就像 workbuddy 一样"。
//   只做左栏一个拖拽点：执行流永远拿剩下的全部 —— 那正是他最想要宽的那一栏。
(function initSplitter() {
  const sp = $('splitter');
  if (!sp) return;
  const KEY = 'harness.leftw';
  const saved = Number(localStorage.getItem(KEY) || 0);
  if (saved >= 180 && saved <= 520) {
    document.documentElement.style.setProperty('--leftw', saved + 'px');
  }
  let dragging = false;
  const clamp = v => Math.max(180, Math.min(520, v));
  const apply = v => {
    const w = clamp(v);
    document.documentElement.style.setProperty('--leftw', w + 'px');
    localStorage.setItem(KEY, String(w));
    // 宽度一变，批注该不该折行也跟着变
    clearTimeout(_redrawT);
    _redrawT = setTimeout(() => {
      document.querySelectorAll('#feed .turn').forEach(drawNoteLines);
    }, 120);
  };
  sp.addEventListener('mousedown', ev => {
    dragging = true;
    sp.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    ev.preventDefault();
  });
  window.addEventListener('mousemove', ev => {
    if (!dragging) return;
    apply(ev.clientX - 12);
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    sp.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });
  sp.addEventListener('dblclick', () => apply(252));
})();

// ── 输入框高度可拖 ──
// ★ 用户：「底部的输入栏最好也是可以手动调节高度的，就跟左栏一样，因为有时候需要输入很多行」。
//   改的是 **min-height**（不是 height）：输入多行时它仍然会自己往下长，拖的只是"最低多高"。
(function initInputResize() {
  const bar = $('cresize');
  if (!bar) return;
  const KEY = 'harness.inputh';
  const saved = Number(localStorage.getItem(KEY) || 0);
  if (saved >= 56 && saved <= 520) {
    document.documentElement.style.setProperty('--inputh', saved + 'px');
  }
  let dragging = false;
  const clamp = v => Math.max(56, Math.min(520, v));
  const apply = v => {
    const h = clamp(v);
    document.documentElement.style.setProperty('--inputh', h + 'px');
    localStorage.setItem(KEY, String(h));
  };
  bar.addEventListener('mousedown', ev => {
    dragging = true;
    bar.classList.add('dragging');
    document.body.style.cursor = 'row-resize';
    document.body.style.userSelect = 'none';
    ev.preventDefault();
  });
  window.addEventListener('mousemove', ev => {
    if (!dragging) return;
    // 鼠标往下走 = 输入框变矮；从窗口底部往上量
    apply(window.innerHeight - ev.clientY - 96);
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });
  bar.addEventListener('dblclick', () => apply(68));
})();

// ── 附件：三条入口（加号 / 粘贴 / 拖入）都汇到一个数组 ──
// ★ 图片只在**发送时**落盘（服务端写 runs/<会话>/img-N.ext），
//   所以在没发送之前它们只是内存里的 data URL —— 不会在会话目录里留垃圾。
//   用户要求界面上必须说明"图会存到会话目录里"，所以附件区下面常驻一行小字。
let _imgs = [];

function _renderChips() {
  const row = $('attachrow'), note = $('attachnote');
  if (!row) return;
  row.hidden = !_imgs.length;
  if (note) note.hidden = !_imgs.length;
  row.innerHTML = _imgs.map((it, i) =>
    `<div class="chip" title="${esc(it.name)} · 发送后存进本会话目录">`
    + `<img src="${it.data}" alt="">`
    + `<span class="cname">${esc(it.name)}</span>`
    + `<button class="cx" data-i="${i}" title="移除">×</button></div>`).join('');
  row.querySelectorAll('.cx').forEach(b => {
    b.onclick = () => { _imgs.splice(Number(b.dataset.i), 1); _renderChips(); };
  });
}

// 拖 / 选 / 粘 都从这里进：按类型分派。
// ★ 原来不是图片就直接 return（静默丢弃）—— 人拖了一个 zip 进去、什么都没发生，
//   只会以为"拖拽坏了"。**看不见的失败等于没做**，所以这里必须有一条明确的出口。
function _addAnyFile(f) {
  if (!f) return;
  if (/^image\//.test(f.type || '')) return _addImageFile(f);
  const nm = String(f.name || '');
  if (/\.(zip|skill)$/i.test(nm)) return _installSkill(f);
  showErr('只接图片和技能压缩包（.zip）。这个文件没加：' + (nm || '(无名)'));
}

function _installSkill(f) {
  if (f.size > 30 * 1024 * 1024) {
    showErr('这个包 ' + (f.size / 1048576).toFixed(1) + ' MB，超过 30 MB 上限');
    return;
  }
  stateNote('正在装技能：' + f.name + '…', 0);   // 0 = 一直留着，等结果覆盖
  const r = new FileReader();
  r.onload = async () => {
    const res = await api('/skills/install', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        // 去掉 `data:application/zip;base64,` 前缀 —— 服务端只认裸 base64
        data: String(r.result || '').replace(/^data:[^,]*,/, ''),
        level: 'user',
      }),
    });
    if (res.status !== 200) {
      const b = res.body || {};
      stateNote('', 1);
      showErr('装技能失败：' + (b.error || ('HTTP ' + res.status))
              + (b.hint ? '（' + b.hint + '）' : ''));
      return;
    }
    const b = res.body || {};
    stateNote('技能「' + b.name + '」装好了 · ' + (b.count || 0) + ' 个文件 · 下一轮生效', 9000);
    clearErr();
    refreshSkillCount();
    if (_skillOn && _spTab === 'skills') loadSkills();
  };
  r.readAsDataURL(f);
}

function _addImageFile(f) {
  if (!f || !/^image\//.test(f.type || '')) return;
  if (f.size > 8 * 1024 * 1024) {
    showErr('这张图 ' + (f.size / 1048576).toFixed(1) + ' MB，超过 8 MB 上限');
    return;
  }
  const r = new FileReader();
  r.onload = () => {
    _imgs.push({ name: f.name || ('粘贴的图-' + (_imgs.length + 1) + '.png'), data: r.result });
    _renderChips();
  };
  r.readAsDataURL(f);
}

(function initAttach() {
  // ① 加号 → 文件选择器
  const pick = $('imgpick');
  const plus = $('plus');
  if (plus && pick) {
    plus.onclick = () => pick.click();
    pick.onchange = () => {
      [...pick.files].forEach(_addAnyFile);
      pick.value = '';                 // 选同一张图两次也要能进来
    };
  }
  // ② 粘贴：从剪贴板里挑图片项
  const ta = $('task');
  if (ta) {
    ta.addEventListener('paste', ev => {
      const items = (ev.clipboardData && ev.clipboardData.items) || [];
      let hit = false;
      for (const it of items) {
        if (it.kind === 'file' && /^image\//.test(it.type)) {
          _addImageFile(it.getAsFile());
          hit = true;
        }
      }
      if (hit) ev.preventDefault();     // 只有真加了图才拦，普通文字粘贴照旧
    });
  }
  // ③ 拖入：整块输入区都接
  const box = document.querySelector('.composer');
  if (box) {
    box.addEventListener('dragover', ev => {
      if (ev.dataTransfer && [...(ev.dataTransfer.types || [])].includes('Files')) {
        ev.preventDefault();
        box.classList.add('dropping');
      }
    });
    box.addEventListener('dragleave', () => box.classList.remove('dropping'));
    box.addEventListener('drop', ev => {
      box.classList.remove('dropping');
      const fs = (ev.dataTransfer && ev.dataTransfer.files) || [];
      if (!fs.length) return;
      ev.preventDefault();
      [...fs].forEach(_addAnyFile);
    });
  }
})();


// ── 状态抽屉：工程状态 ──
// 用户要的"可视化"不是再生成一个文件 —— 文件和人的距离太远。
// 这两样东西应该点开就能看、能点。
function openDrawer(tab) {
  const d = $('drawer');
  if (!d) return;
  d.hidden = false;
  switchTab(tab || 'state');
}
function closeDrawer() {
  const d = $('drawer');
  if (d) d.hidden = true;
}
function switchTab(t) {
  document.querySelectorAll('.dtab').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === t));
  const a = $('panestate'), c2 = $('paneprobe');
  if (a) a.hidden = t !== 'state';
  if (c2) c2.hidden = t !== 'probe';
  if (t === 'probe') paintProbe();
  else paintState();
}

// 把一条命令压成"它在跑什么"（界面只显示这个，原文进 title）。
// 反例（原来就是这样，一列全是内部路径）：
//   C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe bin/accept.py
// 正例：accept.py
function _cmdLabel(s) {
  let parts = String(s || '').trim().split(/\s+/).filter(Boolean);
  const base = p => String(p).replace(/^["']|["']$/g, '').split(/[\\/]/).pop();
  if (!parts.length) return '（空命令）';
  // `cmd /c xxx` 的壳要去掉，否则显示成 "c"
  if (base(parts[0]).toLowerCase() === 'cmd' && /^\/c$/i.test(parts[1] || '')) {
    parts = parts.slice(2);
    if (!parts.length) return 'cmd';
  }
  // 优先取带扩展名的脚本（跳过解释器、跳过 .exe 本身）
  for (const p of parts.slice(1)) {
    const b = base(p);
    if (/\.[a-z0-9]{1,5}$/i.test(b) && !/\.exe$/i.test(b)) return b;
  }
  const b0 = base(parts[0] || '').replace(/\.exe$/i, '');
  if (/^(python|python3|node|bash|sh)$/i.test(b0)) {
    // 只剩解释器时，说明是 `python -c "..."` 这种内联脚本：
    // 显示 python -c，别只显示一个 python（那样看不出它在干嘛）
    const nxt = parts[1] || '';
    return b0 + (/^-{1,2}[a-zA-Z]+$/.test(nxt) ? ' ' + nxt : '');
  }
  // 只在它不是开关（-x / /F）时才拼上去
  const b1 = parts[1] && !/^[-/]/.test(parts[1]) ? base(parts[1]) : '';
  return b0 + (b1 ? ' ' + b1 : '');
}

function _row(cls, text) {
  return `<div class="srow"><span class="dot ${cls}"></span><span>${text}</span></div>`;
}

// 工程状态抽屉（#panestate）：改过哪些文件 / 跑过哪些检查 / 本我 / 服务。
// ★ 只画抽屉，不碰底部那个状态标签 —— 那是 paintPill 的事。
function paintState() {
  const el = $('panestate');
  if (!el) return;
  const out = [];

  if(new URLSearchParams(location.search).get('management')==='1'){
    const facts=window.harnessManagedState;
    const card=(title,body)=>`<div class="scard"><div class="sctitle">${title}</div>${body}</div>`;
    if(!facts||facts.error)out.push(card('当前会话',_row('wait',esc(facts?.error||'正在核对当前会话记录…'))));
    else{
      out.push(card('当前会话',_row('wait',`<code>${esc(facts.conversationId)}</code>`)
        +'<div class="sempty">以下是当前会话最近一页代码记录；完整过程请打开代码审查或工具证据。</div>'
        +(facts.hasMore||facts.incomplete?'<div class="sempty">还有更早记录或证据待核对，当前列表不能代表全部过程。</div>':'')));
      out.push(card('改过什么',facts.edits.length?facts.edits.map(f=>_row(f.state==='completed'?'ok':'wait',`<code>${esc(f.path)}</code> · ${esc(f.state)}`)).join(''):'<div class="sempty">当前页未记录文件写入；不代表工作区没有改动。</div>'));
      out.push(card('跑过的检查',facts.checks.length?facts.checks.map(c=>_row(c.exitCode===0?'ok':c.exitCode===null?'wait':'bad',`<code>${esc(c.id)}</code> · ${esc(c.state)} · 退出码 ${c.exitCode===null?'未知':esc(c.exitCode)}`)).join(''):'<div class="sempty">当前页没有检查记录，检查结果未知。</div>'));
      out.push(card('本轮调用',facts.modelCalls===null?'<div class="sempty">本轮调用记录尚未确认</div>':_row('wait',`记录到 ${esc(facts.modelCalls)} 次模型调用 · ${esc(facts.runId||'轮次未确认')}`)));
    }
    out.push(card('验收','<div class="sempty">检查退出码和阶段结束不代表你已验收。请在交付审查中核对要求与人工确认。</div>'));
  }else{
  // ① 这一轮改了什么
  if (_edits.length) {
    out.push(`<div class="scard"><div class="sctitle">改过 ${_edits.length} 个文件</div>`
      + _edits.slice(-12).map(f =>
          _row('ok', `<code title="${esc(f)}">${esc(String(f).split(/[\\/]/).pop())}</code>`)
        ).join('') + `</div>`);
  } else {
    out.push('<div class="scard"><div class="sctitle">改过什么</div>'
      + '<div class="sempty">这一轮还没动文件</div></div>');
  }

  // ② 跑过什么（倒序，最近的在上面）
  const cs = _checks.slice(-8).reverse();
  out.push('<div class="scard"><div class="sctitle">跑过的检查</div>'
    + (cs.length ? cs.map(c =>
        _row(c.ok ? 'ok' : 'bad',
             `<code title="${esc(c.cmd)}">${esc(_cmdLabel(c.cmd))}</code>`)
      ).join('') : '<div class="sempty">这一轮还没跑过检查</div>')
    + '</div>');

  // ③ 验收：最后一次判据
  out.push('<div class="scard"><div class="sctitle">验收</div>'
    + (_lastVerify
        ? _row(_lastVerify.ok ? 'ok' : 'bad',
               esc(String(_lastVerify.reason || (_lastVerify.ok ? '通过' : '没过')).slice(0, 90)))
        : '<div class="sempty">还没判过</div>')
    + '</div>');

  // ④ 本我：它自己花了多少、核了几次
  const bt = Number(_stat.bTokens || 0);
  const bo = Number(_stat.bCalls || 0);
  const br = Number(_stat.bRechecks || 0);
  out.push('<div class="scard"><div class="sctitle">测试者</div>'
    + (bo || br ? [
        _row('wait', `叫了模型 <b>${bo}</b> 次 · 复跑核实 <b>${br}</b> 次`),
        _row('wait', `花掉 <b>${bt >= 1000 ? (bt / 1000).toFixed(1) + 'K' : bt}</b> token`),
      ].join('') : '<div class="sempty">这一轮还没醒过</div>')
    + '</div>');

  }
  // ⑤ 服务
  out.push('<div class="scard"><div class="sctitle">服务</div>'
    + '<div id="svcrow"><div class="sempty">查一下…</div></div></div>');

  // ⑥ 一次性许可（2026-10-26）：**活着的那几张必须一直看得见**。
  //   原因和"edit 档要有留痕"是同一条 —— 一张没撤掉的许可就是一份看不见的权力；
  //   而它与"从来没有批过"在界面上长得一模一样时，人只会怀疑"是不是没生效"。
  out.push('<div class="scard"><div class="sctitle">一次性许可</div>'
    + '<div id="grow"><div class="sempty">查一下…</div></div></div>');

  el.innerHTML = out.join('');
  _paintGrants();
  api('/health').then(r => {
    const x = $('svcrow');
    if (!x) return;
    x.innerHTML = r.status === 200 && r.body && r.body.ok
      ? _row('ok', '本机服务在跑')
      : _row('bad', '本机服务没响应');
  });
}

// 许可卡的数据（放在 paintState 之外：它要能被"撤销"按钮重新调用）
let _grantsLive = [];

async function _paintGrants() {
  const x = $('grow');
  if (!x) return;
  const r = await api('/grants');
  if (!$('grow')) return;
  const d = r.body || {};
  _grantsLive = d.items || [];
  const rows = _grantsLive.map((it, i) => _row('wait',
    `<span title="${esc(it.sig)}">${esc(it.label || it.sig)}</span>`
    + ` <span class="scdim">${esc(it.mode_cn || '')}`
    + (it.mode === 'ttl' ? ' · 剩 ' + Math.max(1, Math.round((it.left || 0) / 60)) + ' 分钟' : '')
    + `</span> <button class="gbtn" data-grev="${i}">撤销</button>`)).join('');
  // ★ 「以后这类都别问」的例外（2026-10-26）：**必须和一次性许可放在一起看得见**。
  //   两者回答的是同一个问题（现在有哪些不问的地方），分两页的话人只会记得看一页；
  //   而"看不见的权力"和"从来没有过这个权力"在出事那天长得一模一样。
  const _exs = (d.exceptions || {}).items || [];
  const exRows = _exs.map((it, i) => _row('wait',
    `<span title="${esc(it.key || '')}">免问：${esc(it.label || it.key || '')}</span>`
    + ` <span class="scdim">${esc(it.at || '')}`
    + (it.src ? ' · 来自 ' + esc(it.src) : '') + '</span>'
    + ` <button class="gbtn" data-exrev="${i}">撤掉</button>`)).join('');
  // ★ 底线命中（2026-10-26）：拒了就走的话，没人知道它到底在挡什么 ——
  //   "任务一直在做危险的事"和"规则误伤了正常动作"在界面上长得一模一样（都看不见）。
  const _bh = d.baseline_hits || {};
  const bhRow = _bh.error ? _row('bad', '底线计数读不到：' + esc(_bh.error))
    : (!_bh.total
        ? _row('ok', '底线命中：近 7 天 0 次')
        : _row('wait', '底线命中：近 7 天 <b>' + _bh.total + '</b> 次'
            + '（今天 ' + (_bh.today || 0) + ' 次 · '
            + esc(Object.keys(_bh.by_group || {}).map(k => k + ' ' + _bh.by_group[k])
                  .join(' / ') || '—') + '）'
            + (_bh.hint ? '<div class="scdim">⚠ ' + esc(_bh.hint) + '</div>' : '')));
  x.innerHTML = (rows || '<div class="sempty">现在没有活着的'
    + '（被拦下来的那一步，批一次就会出现在这里）</div>')
    + (exRows ? '<div class="sctitle" style="margin-top:8px">免问例外</div>' + exRows
              : '<div class="sempty">没有「以后这类都别问」的例外</div>')
    + '<div class="sctitle" style="margin-top:8px">底线</div>' + bhRow
    // ★ 诊断包（2026-10-26）：出问题时这里能直接打一个**脱敏**的现场包。
    //   以前这一步的答案是"把整个仓库拷走"，而里面有钥匙和全部会话文本 —— 没人敢发。
    + '<div class="sctitle" style="margin-top:8px">出问题了</div>'
    + _row('ok', '<button class="gbtn" data-dg>打一个诊断包</button>'
        + '<span class="scdim" data-dgnote> 脱敏后的现场摘要，能直接发给别人看</span>')
    + (d.rules ? `<div class="sempty">${esc(d.rules)}</div>` : '');
  const _dg = x.querySelector('[data-dg]');
  if (_dg) {
    _dg.onclick = async () => {
      const note = x.querySelector('[data-dgnote]');
      _dg.disabled = true;
      if (note) note.textContent = ' 正在打包…';
      const rr = await api('/diagnose', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ logs: true }),
      });
      const r = rr.body || {};
      _dg.disabled = false;
      if (rr.status !== 200 || !r.ok) {
        if (note) note.textContent = ' 没打成：' + (r.error || '原因不明');
        return;
      }
      // ★ 路径要给全（人下一步就是去发这个文件），并且要**说清里面有什么**。
      //   只回一句"成功了"会让人不知道去哪找、也不知道能不能发。
      if (note) {
        note.innerHTML = ' 已生成 <code>' + esc(r.path || '') + '</code>'
          + '（' + Math.round((r.bytes || 0) / 1024) + ' KB · 脱敏 ' + (r.masked || 0) + ' 处）'
          + ' — 发之前先看里面的 summary.txt';
      }
    };
  }
  x.querySelectorAll('[data-exrev]').forEach(b => {
    b.onclick = async () => {
      const it = _exs[Number(b.dataset.exrev)];
      if (!it) return;
      b.disabled = true;
      const rr = await api('/rules/exception/revoke', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: it.id || '', domain: it.domain || '', key: it.key || '' }),
      });
      if (rr.status !== 200 || !(rr.body || {}).ok) {
        b.disabled = false;
        showErr((rr.body || {}).error || '没撤成');
        return;
      }
      _paintGrants();
    };
  });
  x.querySelectorAll('[data-grev]').forEach(b => {
    b.onclick = async () => {
      const it = _grantsLive[Number(b.dataset.grev)];
      if (!it) return;
      b.disabled = true;
      const rr = await api('/grants', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ act: 'revoke', sig: it.sig, why: '界面里撤的' }),
      });
      if (rr.status !== 200) { b.disabled = false; showErr((rr.body || {}).error || '没撤成'); return; }
      _paintGrants();
    };
  });
}

// ★★ 这里必须用**事件委托**，不能 querySelectorAll 直接绑。
//   原因：抽屉的 HTML 插在 </body> 前，而 <script> 在它**之前** ——
//   脚本跑的时候那些 tab 按钮还没被解析出来，forEach 拿到空集合，
//   tab 点了什么也不发生（而"状态"按钮在顶栏、早于脚本，所以能绑上 ——
//   于是现象是"抽屉能开，但切不到记忆树"，很容易误判成接口问题）。
//   委托挂在 document 上，元素什么时候出现都管用。
document.addEventListener('click', e => {
  const t = e.target && e.target.closest ? e.target.closest('.dtab') : null;
  if (t) { switchTab(t.dataset.tab); return; }
  const c = e.target && e.target.closest ? e.target.closest('#dclose') : null;
  if (c) { closeDrawer(); return; }
  const o = e.target && e.target.closest ? e.target.closest('#openstate') : null;
  if (o) { openDrawer('state'); }
});
window.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeDrawer();
});


// ── 判据体检：造一个"什么都没做"的世界，看这一轮的判据还绿不绿 ──
// 这一页回答的是「你的验收是不是空的」。所以它不自动跑 —— 跑一次要几十秒，
// 而且这个结论只在人想看的时候才有意义。
function paintProbe() {
  const el = $('paneprobe');
  if (!el) return;
  if (el.dataset.done === '1') return;      // 已经跑过就留着结果
  el.innerHTML = `<div class="tsum">判据如果不管世界怎样都绿，那它什么都没核。</div>
    <button id="probeGo" class="tadd" style="margin-left:0">体检这一轮的判据</button>
    <div id="probeOut"></div>`;
  const b = $('probeGo');
  if (b) b.onclick = runProbe;
}

async function runProbe() {
  const el = $('paneprobe');
  const out = $('probeOut');
  const b = $('probeGo');
  if (b) { b.disabled = true; b.textContent = '正在体检…（每条判据跑四遍）'; }
  if (out) out.innerHTML = '';
  const r = await api('/probe' + (cur ? ('?id=' + encodeURIComponent(cur)) : ''));
  const d = r.body || {};
  if (b) { b.disabled = false; b.textContent = '再体检一次'; }
  if (d.error) {
    if (out) out.innerHTML = `<div class="sempty">${esc(d.error)}</div>`;
    return;
  }
  const rows = d.rows || [];
  if (!rows.length) {
    if (out) out.innerHTML = '<div class="sempty">这一轮没有判据</div>';
    return;
  }
  const bad = rows.filter(x => /恒真|自指/.test(x.verdict));
  if (out) out.innerHTML =
      `<div class="tsum" style="margin-top:10px">${rows.length} 条判据，`
    + `<b>${bad.length}</b> 条是空的</div>`
    + rows.map(x => {
        const isBad = /恒真|自指/.test(x.verdict);
        const isSlow = /太慢/.test(x.verdict);
        // ★ 显示"这条核什么"，不显示 r1/r2 —— 编号是锁定时自动编的主键，
        //   对机器有意义，对人没有。编号退到最后一行小字。
        return `<div class="prow">
          <span class="pdot ${isBad ? 'bad' : (isSlow ? 'wait' : 'ok')}"></span>
          <div class="pbody">
            <div class="ptop"><b>${esc(x.label || x.id || '?')}</b>
              <span class="pverdict">${esc(x.verdict_say || x.verdict)}</span></div>
            <div class="pwhy">${esc(x.why || '')}</div>
            ${x.claim ? `<div class="pclaim">原文：${esc(x.claim)}</div>` : ''}
            ${x.id ? `<div class="pclaim">编号 ${esc(x.id)}</div>` : ''}
          </div></div>`;
      }).join('');
  el.dataset.done = '1';
}

on('stop', 'onclick', stop);
on('askgo', 'onclick', sendAskAnswer);
// 「收起」：先去看上下文，回来看见状态是「等你回话」再点开。
// ★ 非模态 dialog 没有 Esc 默认行为（那是模态才有的），所以必须显式给一个收手；
//   同时这里也接住 Esc —— 人在弹窗里按 Esc 的本能不会因为"非模态"就消失。
on('askfold', 'onclick', () => { const d = $('askdlg'); if (d.open) d.close(); });
on('askdlg', 'onkeydown', ev => {
  if (ev.key === 'Escape') { ev.preventDefault(); $('askdlg').close(); }
});
// ★ 弹窗被关掉（ESC / 点遮罩）**不等于问题被回答**：问题还挂着，
//   流里那条"等你回话"的条会留着，随时能再打开。
on('askdlg', 'onclose', () => {
  // 问题还挂着 ⇒ 状态仍要写「等你回话」。只调 paintPill，理由同上：
  // 直接写 #state 会被下一秒的计时器刷新覆盖掉。
  if (_pendingAsk) paintPill();
});
// 输入框改成多行：Enter 发送，Shift+Enter 换行。
// ★ 为什么不用 <input>：一句话说不清的时候人需要换行，而 <input> 换不了行，
//   只能把几段话挤成一行 —— 那也是"输入框太单薄"的一部分。
$('task').addEventListener('keydown', e => {
  // 候选列表开着的时候，方向键和回车属于列表，不属于输入框
  if (_cmdOpen) {
    if (e.key === 'ArrowDown') { e.preventDefault(); cmdMark(_cmdPick + 1); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); cmdMark(_cmdPick - 1); return; }
    if (e.key === 'Tab' || (e.key === 'Enter' && !e.isComposing)) {
      e.preventDefault(); pickCmd(_cmdHit[_cmdPick]); return;
    }
    if (e.key === 'Escape') { e.preventDefault(); cmdClose(); return; }
  }
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); start(); }
});
// 跟着内容长高（最多 180px，超过它自己滚）
$('task').addEventListener('input', () => {
  const t = $('task');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 180) + 'px';
  refreshCmdMenu();
});
// 输入框失焦时收起候选（点了别处就说明不是在挑命令了）。
// ★ 用 setTimeout：mousedown 选候选要先于失焦处理，否则点不中
$('task').addEventListener('blur', () => setTimeout(cmdClose, 120));

if(new URLSearchParams(location.search).get("management")!=="1"){
initShiki();
loadCommands();          // 预取一次，第一次打 / 不用等
loadState();
loadSessions();
// ★ 模型这一行要**在开场就有内容**（2026-10-26）：空下拉看起来像坏了，
//   而"这一句用谁在跑"是打开窗口就该知道的事。
loadModels();
// ★ 上下文那个环开机就画（2026-10-27 自测逮到）：不画的话它是一个**没有任何弧**的圆，
//   看起来像坏了。而且"这一轮用了多少"本来就是打开窗口就该知道的事，
//   原来只在轮询收到 `end` 时才刷 —— 那个时机不由我们决定。
paintRing(0);
refreshCtx();
// ★ 启动时**立刻**拉一次，不等定时器。
//   否则打开窗口先空 1.5 秒，看起来就像"它没去读会话目录，得我点一下才有反应"。
loadState();
loadSessions();

setInterval(pollReceipts, 5000);
pollReceipts();
setInterval(loadState, 3000);
setInterval(loadSessions, 1500);
setInterval(poll, 500);
// ★ 流式单独一个更快的循环：取数要跟得上模型的出字速度。
//   事件轮询仍是 500ms —— 事件是「发生了什么」，不需要那么密。
setInterval(streamTick, 120);
streamTick();


}

// ── 配置有没有写错（2026-10-26）──
// ★ 为什么要在界面上说：只写在日志里，"改错了"和"改对了"在屏幕上长得一样。
async function _checkConfig() {
  const r = await api('/config');
  const w = (r.body && r.body.warns) || [];
  const el = $('cfgw');
  if (!el) return;
  if (!w.length) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = '配置有 ' + w.length + ' 处问题';
  el.title = w.join('\n');
  el.onclick = () => showErr('这几处配置没生效：\n· ' + w.join('\n· '));
}
_checkConfig();

