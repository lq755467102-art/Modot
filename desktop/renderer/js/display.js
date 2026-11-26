// 显示偏好：输出长什么样、思考块长什么样。
//
// ★ 为什么单独一个文件：这类偏好是**纯显示**的，不该散在渲染代码里。
//   散在里面的话，"改一下显示方式"就要动渲染逻辑 —— 而渲染逻辑是
//   最容易把行为改坏的地方（前几天连着出了几次"改一处、别处跟着变"）。
//
// ★ 存 localStorage，不进后端：这是"你这台机器上想怎么看"，
//   不是任务的一部分，不该进会话、不该进索引。
// 主要引用或加载入口包括 `desktop/renderer/index.html`。
const DP = (() => {
  const KEY = 'harness.disp';
  const DEF = { output: 'type', think: 'collapse' };
  let v = Object.assign({}, DEF);
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || '{}');
    if (saved && typeof saved === 'object') Object.assign(v, saved);
  } catch (e) { /* 读坏了就用默认，别让显示问题把人挡在界面外 */ }
  return {
    get: (k) => v[k],
    set: (k, x) => { v[k] = x; try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) {} },
    all: () => Object.assign({}, v),
  };
})();

// 面板的开关。按钮在顶栏（`.bar`）上，面板是浮层。
function mountDisp() {
  const btn = $('opendisp'), panel = $('disppanel');
  if (!btn || !panel) return;
  if (panel.dataset.mounted) return;
  panel.dataset.mounted = '1';

  const o = panel.querySelector('#dp-output'), t = panel.querySelector('#dp-think');
  if (o) o.value = DP.get('output');
  if (t) t.value = DP.get('think');

  const close = () => { panel.hidden = true; };
  btn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    panel.hidden = !panel.hidden;
    if (!panel.hidden) {
      // 贴着按钮下方展开，不挡输入框
      const r = btn.getBoundingClientRect();
      panel.style.top = (r.bottom + 6) + 'px';
      panel.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
    }
  });
  document.addEventListener('click', (ev) => {
    if (!panel.hidden && !panel.contains(ev.target) && ev.target !== btn) close();
  });
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') close(); });

  if (o) o.addEventListener('change', () => {
    DP.set('output', o.value);
    const note = panel.querySelector('.dpnote');
    if (note) note.textContent = (o.value === 'type')
      ? '逐字接上：看起来像 WorkBuddy，字一个一个来'
      : '整段出：到多少显示多少（省渲染，长输出不闪）';
  });
  if (t) t.addEventListener('change', () => {
    DP.set('think', t.value);
    // 立即作用在**已经渲染出来**的思考块上：偏好是给人看的，
    // 不能等到下一条才生效（那看起来像"点了没用"）。
    document.querySelectorAll('.reasoning').forEach(sec => {
      if (t.value === 'expand') sec.classList.remove('collapse');
      else if (t.value === 'collapse') sec.classList.add('collapse');
      else sec.style.display = 'none';     // 不显示
      if (t.value !== 'hide') sec.style.display = '';
    });
  });
}

// 直接挂一次（脚本在 body 末尾加载，DOM 已经在了）
if (document.readyState !== 'loading') mountDisp();
else document.addEventListener('DOMContentLoaded', mountDisp);
