// Retain per-conversation composer drafts when visiting compatibility management.
// Model credential fields are deliberately outside this buffer.
// 主要引用或加载入口包括 `desktop/renderer/index.html`。
(() => {
  const entry=new URLSearchParams(location.search);
  if(entry.get('management')==='1'){
    document.body.classList.add('management-only');document.body.dataset.management=entry.get('section')==='skills'?'skills':'state';
    document.documentElement.dataset.theme=entry.get('theme')||'light';
    for(const id of ['cgatesel','mself','mreview','mplan','mswap','mcfg','c_save'])if($(id))$(id).disabled=true;
    if(entry.get('section')==='skills')$('openskills')?.click();
    else if(typeof openDrawer==='function')openDrawer('state');
    return;
  }
  const key = 'harness.compat-drafts.' + servicePort;
  let drafts = {}, broken = false, owner = cur || 'new';
  try {
    drafts = JSON.parse(localStorage.getItem(key) || '{}');
    if (!drafts || typeof drafts !== 'object' || Array.isArray(drafts) || Object.values(drafts).some(v => typeof v !== 'string')) throw Error('invalid drafts');
  } catch { broken = true; }
  const input = $('task');
  function persist() {
    if (broken) { showErr('兼容草稿存储无法读取，原值保留；请复制当前输入再离开。'); return; }
    const next = { ...drafts, [owner]: input.value };
    if (Object.keys(next).length > 100 || JSON.stringify(next).length > 1024 * 1024) { showErr('兼容草稿达到容量，未覆盖旧稿；请复制当前输入。'); return; }
    try { localStorage.setItem(key, JSON.stringify(next)); drafts = next; } catch { showErr('兼容草稿保存失败，请复制当前输入后离开。'); }
  }
  window.harnessCompatContextChange = next => {
    persist();
    owner = next || 'new';
    input.value = drafts[owner] || '';
  };
  window.harnessCompatDraftSent = (target, text) => {
    const id = target || 'new';
    if (broken || drafts[id] !== text) return;
    const next = { ...drafts, [id]: '' };
    try { localStorage.setItem(key, JSON.stringify(next)); drafts = next; } catch { showErr('原请求已收讫，草稿清理未确认；请核对原会话，不要重复发送。'); }
  };
  input.value = drafts[owner] || input.value;
  input.addEventListener('input', persist);
  window.addEventListener('beforeunload', persist);
  const link = document.createElement('a');
  const url = new URL('../renderer-v2/dist/index.html', location.href);
  url.searchParams.set('port', String(servicePort));
  link.href = url.href;
  link.textContent = '返回新版对话与设置';
  link.className = 'pill';
  document.querySelector('.bar').appendChild(link);
  for (const id of ['cgatesel','mself','mreview','mplan','mswap','mcfg','c_save']) {
    const control = $(id);
    if (control) { control.disabled = true; control.title = '此写入口已在新版设置验收，请返回新版设置'; }
  }
  const section = new URLSearchParams(location.search).get('section');
  if (section === 'skills') $('openskills')?.click();
  if (section === 'execution' && typeof openDrawer === 'function') openDrawer('state');
})();
