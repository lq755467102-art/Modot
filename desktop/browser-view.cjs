// Each tab owns an unprivileged view. GitHub sign-in is shared only within
// this Electron profile; other browser tabs and previews have isolated storage.
// 主要引用方包括 `bridge-v2.cjs`。
const {WebContentsView,session:electronSession}=require('electron');
const crypto=require('node:crypto');
const githubPartition='persist:modot-github-web';
function githubUrl(value,resource=false) {
  try {
    const url=new URL(value);
    if(url.protocol!=='https:'||url.port||url.username||url.password)return null;
    if(url.hostname==='github.com'||resource&&(
      url.hostname==='github.githubassets.com'||url.hostname.endsWith('.githubusercontent.com')||
      url.hostname==='githubusercontent.com'||url.hostname==='api.github.com'))return url;
  } catch {}
  return null;
}
function githubPage(value) {
  const url=githubUrl(value);
  const match=url?.pathname.match(/^\/([A-Za-z0-9-]{1,39}\/[A-Za-z0-9_.-]{1,100})\/pull\/([1-9][0-9]{0,11})(?:\/(files|checks|commits))?\/?$/);
  if(!match)return null;
  const [,repository,number,section]=match;
  const check=url.searchParams.get('check_run_id');
  const checkId=section==='checks'&&/^[1-9][0-9]{0,17}$/.test(check||'')?check:null;
  const safeUrl='https://github.com/'+repository+'/pull/'+number+(section?'/'+section:'')+(checkId?'?check_run_id='+checkId:'');
  return {repository,number:Number(number),section:section||'conversation',checkId,url:safeUrl};
}
const signInPage=value=>/^\/(login|logout|session|sessions|settings|account|accounts|signup|two-factor|orgs\/[^/]+\/sso)(?:\/|$)/i.test(githubUrl(value)?.pathname||'');
module.exports=function browserController({getWindow,record,publishGitHub=()=>{}}){
  const tabs=new Map();
  let clearingGitHub=false;
  let host=null,visible=null;
  const log=(tab,value)=>{
    if(tab.github)value={operation:value.operation,id:tab.id,url:githubPage(value.url)?.url||'https://github.com/'};
    try{record(value);}catch{tab.notice='浏览操作记录未保存，请核对磁盘空间；未报告引用已留痕';}
  };
  function publish(tab) {
    if(!tab.github)return null;
    const wc=tab.view.webContents;
    const data={...tab.github,id:tab.id,version:tab.version,active:tab.active&&!tab.blocked,
      page:tab.navigating||tab.needsUser||tab.failedUrl||tab.httpStatus>=400?null:githubPage(wc.getURL()),
      status:tab.needsUser||signInPage(wc.getURL())?'needs-user':tab.navigating?'loading':tab.failedUrl||tab.httpStatus>=400?'unavailable':'ready',
      ...(tab.receipt?{receipt:tab.receipt}:{})};
    const fingerprint=JSON.stringify(data);
    if(fingerprint!==tab.published||Date.now()-tab.publishTime>=2000){
      if(publishGitHub(tab.id,{...data,updatedAt:Date.now()})===false)return null;
      tab.published=fingerprint;tab.publishTime=Date.now();
    }
    return data;
  }
  const snapshot=tab=>{
    const {view,id,notice,preview}=tab,wc=view.webContents;
    return {open:true,id,url:tab.failedUrl||wc.getURL(),title:wc.getTitle(),loading:wc.isLoading(),
      canBack:wc.navigationHistory.canGoBack(),canForward:wc.navigationHistory.canGoForward(),
      loadFailed:!!tab.failedUrl,notice,previewId:preview?.id};
  };
  function detach() {
    if(host&&!host.isDestroyed()){
      host.webContents.removeListener('did-start-navigation',closeAll);
      host.webContents.removeListener('render-process-gone',closeAll);
      host.removeListener('closed',closeAll);
    }
    host=null;
  }
  function closeAll() {
    for(const id of tabs.keys())close(id);
    detach();
  }
  function close(id){
    const old=tabs.get(id);
    if(!old)return {open:false};
    tabs.delete(id);
    if(visible===old)visible=null;
    clearInterval(old.timer);
    if(old.github)publishGitHub(id,null);
    if(host&&!host.isDestroyed())host.contentView.removeChildView(old.view);
    if(!old.view.webContents.isDestroyed())old.view.webContents.close({waitForBeforeUnload:false});
    if(!old.github)old.session.clearStorageData().catch(()=>{});
    if(!tabs.size)detach();
    return {open:false};
  }
  function allowed(value,preview,resource=false,github=false){
    if(github)return !!githubUrl(value,resource);
    try{const u=new URL(value);if(u.username||u.password)return false;
      if(preview){const source=new URL(preview.url);return u.hostname===source.hostname&&u.port===source.port&&(u.protocol===source.protocol||resource&&u.protocol==='ws:'&&source.protocol==='http:');}
      return ['http:','https:'].includes(u.protocol)||resource&&['ws:','wss:'].includes(u.protocol);
    }catch{return false;}
  }
  function load(tab,url){
    const wc=tab.view.webContents;
    tab.failedUrl=undefined;
    tab.notice='';
    return wc.loadURL(url).catch(error=>{
      if(wc.isDestroyed()||error.code==='ERR_ABORTED'||error.errno===-3)return;
      tab.failedUrl=url;
      tab.notice=tab.github?'GitHub 页面未能加载，请重试。':'页面加载失败：'+error.message;
      tab.navigating=false;
      publish(tab);
    });
  }
  async function command(body,{lookupPreview}={}){
    if(!body||typeof body.operation!=='string')throw Error('浏览器操作无效');
    const win=getWindow();if(!win||win.isDestroyed())throw Error('主窗口已关闭');
    if(body.operation==='github-clear'){
      if(clearingGitHub)throw Error('正在清除 GitHub 网页登录。');
      clearingGitHub=true;
      try {
        for(const tab of tabs.values())if(tab.github)close(tab.id);
        const profile=electronSession.fromPartition(githubPartition);
        await profile.clearStorageData();
        await profile.clearCache();
        return {open:false};
      } finally {clearingGitHub=false;}
    }
    if(body.operation==='open'){
      let preview=null;
      if(lookupPreview){
        const owner=win.webContents;
        let navigated=false;
        const invalidate=()=>{navigated=true;};
        owner.once('did-start-navigation',invalidate);
        owner.once('render-process-gone',invalidate);
        try{preview=await lookupPreview();}
        finally{
          if(!owner.isDestroyed()){
            owner.removeListener('did-start-navigation',invalidate);
            owner.removeListener('render-process-gone',invalidate);
          }
        }
        if(navigated||win.isDestroyed()||getWindow()!==win)throw Error('主窗口已变化，请重新打开预览');
        if(preview.state!=='ready')throw Error('所选预览未就绪');
      }
      let github=null;
      if(body.github){
        const {owner,pluginId,pluginRevision}=body.github;
        if(clearingGitHub||preview||typeof owner!=='string'||!/^[A-Za-z0-9_-]{0,100}$/.test(owner)||
          typeof pluginId!=='string'||!/^[A-Za-z0-9._-]{1,160}$/.test(pluginId)||!Number.isSafeInteger(pluginRevision)||pluginRevision<1)
          throw Error('GitHub 工作间身份已变化，请重新打开。');
        github={owner,pluginId,pluginRevision};
      }
      const target=preview?.url||body.url;
      const permits=(url,resource=false)=>allowed(url,preview,resource,github);
      if(!permits(target))throw Error(github?'GitHub 工作间支持 github.com 的 HTTPS 地址。':'请输入有效的 HTTP 或 HTTPS 网页地址');
      if(host&&host!==win)closeAll();
      if(body.id)close(body.id);
      if(!host){
        host=win;
        win.once('closed',closeAll);
        win.webContents.once('did-start-navigation',closeAll);
        win.webContents.once('render-process-gone',closeAll);
      }
      const view=new WebContentsView({webPreferences:{partition:github?githubPartition:'browser-'+crypto.randomUUID(),sandbox:true,
        contextIsolation:true,nodeIntegration:false,nodeIntegrationInSubFrames:false,nodeIntegrationInWorker:false,
        webSecurity:true,webviewTag:false,allowRunningInsecureContent:false,safeDialogs:true,
        safeDialogsMessage:'阻止此页面继续弹出确认框',disableDialogs:false,backgroundThrottling:false}});
      const wc=view.webContents,session=wc.session;
      const value={id:crypto.randomUUID(),view,session,preview,lookupPreview,github,notice:'',timer:null,blocked:false,
        active:false,version:0,navigating:!!github,httpStatus:0};
      tabs.set(value.id,value);
      view.setVisible(false);win.contentView.addChildView(view);
      wc.setWebRTCIPHandlingPolicy('disable_non_proxied_udp');
      session.setPermissionRequestHandler((_wc,_permission,callback)=>callback(false));
      session.setPermissionCheckHandler(()=>false);
      const blockDownload=(event,_item,source)=>{
        if(source!==wc)return;
        event.preventDefault();value.notice='此网页下载未保存。已核验本地应用请使用“打开隔离预览”另存下载。';
        log(value,{operation:'download-blocked',id:value.id,url:wc.getURL()});
      };
      session.on('will-download',blockDownload);
      wc.once('destroyed',()=>session.removeListener('will-download',blockDownload));
      session.webRequest.onBeforeRequest((details,callback)=>{
        const resource=details.resourceType!=='mainFrame';
        if(resource&&(details.url.startsWith('data:')||details.url.startsWith('blob:')))return callback({cancel:false});
        if(!permits(details.url,resource))return callback({cancel:true});
        if(!lookupPreview)return callback({cancel:false});
        lookupPreview(true).then(app=>callback({cancel:app.state!=='ready'||new URL(app.url).origin!==new URL(preview.url).origin}),()=>callback({cancel:true}));
      });
      wc.setWindowOpenHandler(()=>{value.notice='页面请求打开新窗口，已保留在当前浏览器中；可手动输入目标地址。';return {action:'deny'};});
      for(const event of ['will-navigate','will-redirect'])wc.on(event,(event,url)=>{
        if(!permits(url)){
          event.preventDefault();value.notice=github?'此跳转需要在系统浏览器中完成。':'目标地址超出当前浏览范围，未导航';
          if(github){value.needsUser=true;publish(value);}
        }
      });
      wc.on('will-attach-webview',event=>event.preventDefault());
      wc.on('did-start-navigation',(_event,_url,inPage,main)=>{
        if(github&&main&&!inPage){
          value.version++;value.navigating=true;value.receipt=null;value.needsUser=false;publish(value);
        }
      });
      wc.on('did-navigate',(_event,url,status)=>{
        if(value.failedUrl)value.notice='';
        value.failedUrl=undefined;
        value.httpStatus=status;value.navigating=false;publish(value);
        value.location=github?JSON.stringify([githubPage(url),signInPage(url)]):undefined;
        log(value,{operation:'navigate',id:value.id,url});
      });
      wc.on('did-navigate-in-page',(_event,url,main)=>{
        if(!github||!main)return;
        const location=JSON.stringify([githubPage(url),signInPage(url)]);
        if(location!==value.location){
          value.location=location;value.version++;value.receipt=null;publish(value);
        }
      });
      wc.on('did-fail-load',(_event,code,description,url,isMain)=>{
        if(isMain&&code!==-3&&permits(url)){
          value.failedUrl=url;
          value.notice=github?'GitHub 页面未能加载，请重试。':'页面加载失败：'+description;
          value.navigating=false;publish(value);
        }
      });
      wc.on('render-process-gone',()=>{value.notice='网页进程已退出；可显式重新打开';value.blocked=true;view.setVisible(false);publish(value);});
      if(lookupPreview){value.timer=setInterval(async()=>{try{const app=await lookupPreview(true);if(tabs.has(value.id)&&app.state!=='ready'){value.blocked=true;value.notice='本地预览已失效，页面已停止';view.setVisible(false);wc.stop();}}catch{if(tabs.has(value.id)){value.blocked=true;value.notice='预览授权已变化，页面已停止';view.setVisible(false);wc.stop();}}},2000);value.timer.unref();}
      // Navigation errors are visible state, not an exception which discards
      // the page's address and user's draft.
      void load(value,target);
      log(value,{operation:'open',id:value.id,url:target,previewId:preview?.id});
      return snapshot(value);
    }
    if(body.operation==='close')return close(body.id);
    const value=tabs.get(body.id);
    if(!value||host!==win)throw Error('浏览器实例已变化，请重新打开');
    const wc=value.view.webContents;
    if(body.operation==='github-capture'){
      const state=publish(value);
      return state?.active&&state.owner===body.owner?{id:value.id,version:state.version}:null;
    }
    if(body.operation==='github-authority'){
      const {owner,pluginId,pluginRevision}=body;
      if(!value.github||value.github.owner!==owner||value.github.pluginId!==pluginId||
        !Number.isSafeInteger(pluginRevision)||pluginRevision<value.github.pluginRevision)
        throw Error('GitHub 连接身份已变化。');
      value.github.pluginRevision=pluginRevision;
      publish(value);
      return snapshot(value);
    }
    if(body.operation==='github-navigate'){
      const current=publish(value),target=githubPage(body.url);
      if(!current?.active||current.owner!==body.owner||current.version!==body.version||!current.page||
        !target||target.url!==body.url||!['files','checks'].includes(target.section)||
        target.repository.toLowerCase()!==current.page.repository.toLowerCase()||target.number!==current.page.number)
        return {outcome:'stale'};
      await load(value,target.url);
      if(!tabs.has(value.id)||!value.active)return {outcome:'stale'};
      if(value.needsUser||signInPage(wc.getURL()))return {outcome:'needs-user'};
      if(value.version!==body.version+1)return {outcome:'stale'};
      if(value.failedUrl||value.httpStatus!==200||githubPage(wc.getURL())?.url!==target.url)return {outcome:'missing'};
      // Verify the site's active section or selected check, returning no DOM text.
      const selected=await wc.executeJavaScript(`((target)=>{
        const pathMatches=link=>link?.pathname===new URL(target.url).pathname;
        if(target.checkId)return [...document.querySelectorAll('#js-selected-check-run a')].some(link=>
          link.pathname.endsWith('/job/'+target.checkId)||(pathMatches(link)&&new URL(link.href).searchParams.get('check_run_id')===target.checkId));
        return [...document.querySelectorAll('a[aria-current="page"]')].some(pathMatches);
      })(${JSON.stringify(target)})`).catch(()=>false);
      if(!tabs.has(value.id)||!value.active||value.version!==body.version+1||githubPage(wc.getURL())?.url!==target.url)
        return {outcome:'stale'};
      if(selected!==true)return {outcome:'missing'};
      value.receipt={id:body.requestId,url:target.url,version:value.version};
      if(!publish(value))return {outcome:'missing'};
      return {outcome:'opened'};
    }
    if(body.operation==='bounds'){
      const {x,y,width,height}=body;
      if(![x,y,width,height].every(n=>Number.isFinite(n)&&n>=0&&n<=20000))throw Error('浏览器区域无效');
      // BrowserPane reports CSS pixels. Electron child views are positioned in window DIPs.
      const zoom=win.webContents.getZoomFactor(),size=win.getContentBounds();
      const left=Math.min(size.width,Math.round(x*zoom)),top=Math.min(size.height,Math.round(y*zoom));
      const right=Math.min(size.width,Math.round((x+width)*zoom)),bottom=Math.min(size.height,Math.round((y+height)*zoom));
      value.view.setBounds({x:left,y:top,width:right-left,height:bottom-top});
      const show=!!body.visible&&right>left&&bottom>top&&!value.blocked;
      if(show){
        if(visible&&visible!==value)visible.view.setVisible(false);
        visible=value;
      }else if(visible===value)visible=null;
      value.view.setVisible(show);
      value.active=body.active===true;
      publish(value);
      return snapshot(value);
    }
    if(body.operation==='navigate'){
      if(!allowed(body.url,value.preview,false,value.github))throw Error(value.github?'请在系统浏览器中打开站外地址。':'地址超出当前浏览范围');
      await load(value,body.url);
    }else if(body.operation==='back'){if(wc.navigationHistory.canGoBack())wc.navigationHistory.goBack();}
    else if(body.operation==='forward'){if(wc.navigationHistory.canGoForward())wc.navigationHistory.goForward();}
    else if(body.operation==='refresh'){
      // Failed navigation can leave Chromium on the previously committed URL.
      if(value.failedUrl)await load(value,value.failedUrl);
      else{value.notice='';wc.reload();}
    }
    else if(body.operation==='quote'){
      if(value.failedUrl)throw Error('当前页面未加载，请重试后再引用');
      if(value.preview&&value.lookupPreview){const app=await value.lookupPreview(true);if(app.state!=='ready')throw Error('页面来源已失效，未生成引用');}
      if(value.github&&signInPage(wc.getURL()))throw Error('登录和账号管理页面不能引用。');
      const version=value.version;
      const quote=await wc.executeJavaScript(`(()=>{const selected=String(window.getSelection()||'');const text=selected||(${value.github?'""':"document.body?.innerText||''"});return {url:location.href,title:document.title,text:text.slice(0,12000),selection:!!selected,complete:text.length<=12000};})()`);
      if(!tabs.has(value.id)||version!==value.version||!allowed(quote.url,value.preview,false,value.github))throw Error('页面已经变化，未生成引用');
      if(value.github){
        if(!quote.selection)throw Error('请先在网页中选中要引用的文字。');
        const url=githubUrl(quote.url);
        if(signInPage(quote.url))throw Error('登录和账号管理页面不能引用。');
        quote.url=githubPage(quote.url)?.url||url.origin+url.pathname;
      }
      const evidence={...quote,at:new Date().toISOString(),browserId:value.id,previewId:value.preview?.id,previewRevision:value.preview?.revision};
      if(value.github)log(value,{operation:'quote',url:evidence.url});
      else record({operation:'quote',...evidence});
      return evidence;
    }else if(body.operation!=='state')throw Error('没有此浏览器操作');
    return snapshot(value);
  }
  return {command,close};
};
