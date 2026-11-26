// Run the original skills/state UI in its own restricted view. The chat DOM,
// editors and drafts remain mounted; service credentials never enter its URL.
// 主要引用方包括 `bridge-v2.cjs`。
const {WebContentsView}=require('electron');
const path=require('node:path'),{fileURLToPath}=require('node:url'),crypto=require('node:crypto');
module.exports=function({getWindow,port,readToken,request}){
  const views=new Map();
  const index=path.join(__dirname,'renderer','index.html');
  // 旧管理页的文件请求限于随程序交付的渲染资源和着色脚本。
  function fileAllowed(url){try{const file=path.resolve(fileURLToPath(url));return file.startsWith(path.join(__dirname,'renderer')+path.sep);}catch{return false;}}
  // 只允许旧管理页访问本机服务上对应方法的管理接口，后续宿主据此注入授权。
  function apiAllowed(url,method){try{const u=new URL(url);if(u.protocol!=='http:'||u.hostname!=='127.0.0.1'||u.port!==String(port)||u.username||u.password||decodeURIComponent(u.pathname).includes('..'))return false;
    if(method==='GET'||method==='OPTIONS')return /^\/api\/(state|models|config|health|context|facts|habits|grants|gflow|probe|diagnose|rules|skills|mcp|operations)(\/|$)/.test(u.pathname);
    return method==='POST'&&/^\/api\/(facts|habits|grants|gflow|probe|diagnose|rules|skills|mcp)(\/|$)/.test(u.pathname);
  }catch{return false;}}
  // 解绑主窗口监听和原生视图；传入实例时只关闭该视图。
  function close(id){
    const targets=id?[views.get(id)].filter(Boolean):[...views.values()];
    for(const old of targets){views.delete(old.id);const win=getWindow();
      if(win&&!win.isDestroyed()){win.contentView.removeChildView(old.view);win.webContents.removeListener('did-start-navigation',old.navigate);win.removeListener('closed',old.closed);}
      if(!old.view.webContents.isDestroyed())old.view.webContents.close({waitForBeforeUnload:false});
      old.session.clearStorageData().catch(()=>{});
    }
    return {open:false};
  }
  // 仅执行管理页需要刷新会话事实；请求结束后重新核对原视图仍存活才交回摘要。
  async function refresh(value){
    if(value.refreshing||value.section!=='execution')return;value.refreshing=true;
    let facts={conversationId:value.conversationId,error:'尚未选择会话'};
    try{if(value.conversationId){
      const base='/api/v2/conversations/threads/'+encodeURIComponent(value.conversationId);
      const [code,progress]=await Promise.all([request({path:base+'/code-operations'}),request({path:base+'/progress'})]);
      if(code.status!==200||progress.status!==200)throw Error('会话状态暂不可用，请在工具证据中核对');
      const ops=Array.isArray(code.body.operations)?code.body.operations:[];
      facts={conversationId:value.conversationId,hasMore:!!code.body.hasMore,runId:progress.body.run?.id||null,
        incomplete:!!(progress.body.evidenceIncomplete||progress.body.currentRunPending||progress.body.currentRunUnknown),
        modelCalls:progress.body.run?.modelCalls??null,
        edits:ops.filter(o=>o.operation==='write').slice(-12).map(o=>({path:String(o.result?.path||'路径未确认').slice(0,1024),state:o.state})),
        checks:ops.filter(o=>o.operation==='check').slice(-8).reverse().map(o=>({id:o.id,state:o.result?.process?.state||o.state,exitCode:o.result?.process?.exitCode??null}))};
    }}catch(e){facts={conversationId:value.conversationId,error:e.message};}
    finally{value.refreshing=false;}
    if(views.get(value.id)!==value||value.view.webContents.isDestroyed())return;
    // Only bounded public metadata; no request bodies, source content or service token.
    await value.view.webContents.executeJavaScript('window.harnessManagedState='+JSON.stringify(facts)+';paintState()');
  }
  // `bridge-v2.cjs` 转入旧管理页命令。
  // 按管理区与会话复用实例，其余操作以原视图 ID 定位。
  async function command(body){
    const win=getWindow();if(!win||win.isDestroyed())throw Error('主窗口已关闭');
    if(body?.operation==='close')return close(body.id);
    if(body?.operation==='open'){
      if(!['skills','execution'].includes(body.section))throw Error('未知旧页管理入口');
      const conversationId=body.conversationId||'';if(typeof conversationId!=='string'||conversationId&&!/^[A-Za-z0-9._-]{1,100}$/.test(conversationId))throw Error('会话身份无效');
      const existing=[...views.values()].find(v=>v.section===body.section&&v.conversationId===conversationId);
      if(existing)return {id:existing.id,section:existing.section,open:true};
      for(const old of views.values())if(old.section===body.section)close(old.id);
      const view=new WebContentsView({webPreferences:{partition:'legacy-management-'+crypto.randomUUID(),sandbox:true,contextIsolation:true,nodeIntegration:false,nodeIntegrationInSubFrames:false,nodeIntegrationInWorker:false,webviewTag:false,webSecurity:true,backgroundThrottling:false,safeDialogs:true,zoomFactor:win.webContents.getZoomFactor()}});
      const wc=view.webContents,session=wc.session;const value={id:crypto.randomUUID(),section:body.section,conversationId,view,session};views.set(value.id,value);
      wc.setWebRTCIPHandlingPolicy('disable_non_proxied_udp');
      session.setPermissionRequestHandler((_wc,_permission,callback)=>callback(false));session.setPermissionCheckHandler(()=>false);
      session.on('will-download',event=>event.preventDefault());
      session.webRequest.onBeforeRequest((details,callback)=>callback({cancel:!(details.url.startsWith('file:')?fileAllowed(details.url):apiAllowed(details.url,details.method))}));
      session.webRequest.onBeforeSendHeaders((details,callback)=>{
        const headers={...details.requestHeaders};if(apiAllowed(details.url,details.method))headers.Authorization='Bearer '+readToken();
        callback({requestHeaders:headers});
      });
      wc.setWindowOpenHandler(()=>({action:'deny'}));
      wc.on('will-navigate',(event,url)=>{try{if(path.resolve(fileURLToPath(url))!==index)event.preventDefault();}catch{event.preventDefault();}});
      value.navigate=(_event,_url,_inPlace,main)=>{if(main)close(value.id);};value.closed=()=>close(value.id);
      win.webContents.on('did-start-navigation',value.navigate);win.once('closed',value.closed);
      win.contentView.addChildView(view);view.setVisible(false);
      try{await wc.loadFile(index,{query:{port:String(port),section:body.section,management:'1',theme:['light','dark','contrast'].includes(body.theme)?body.theme:'light'}});}catch(e){close(value.id);throw e;}
      if(views.get(value.id)!==value)throw Error('管理页面已关闭');
      await refresh(value);
      return {id:value.id,section:value.section,open:true};
    }
    const current=views.get(body?.id);
    if(!current)throw Error('旧页管理实例已关闭');
    if(body.operation==='refresh'){await refresh(current);return {id:current?.id,open:!!current};}
    if(body.operation==='theme'){
      if(!['light','dark','contrast'].includes(body.theme))throw Error('未知界面主题');
      await current.view.webContents.executeJavaScript('document.documentElement.dataset.theme='+JSON.stringify(body.theme));return {id:current.id,open:true};
    }
    if(body.operation==='bounds'){
      const values=['x','y','width','height'].map(k=>Number(body[k]));if(values.some(v=>!Number.isFinite(v)||v<0||v>10000))throw Error('管理视图尺寸无效');
      const zoom=win.webContents.getZoomFactor(),bounds=win.getContentBounds();let [x,y,width,height]=values.map(v=>Math.round(v*zoom));
      x=Math.min(x,bounds.width);y=Math.min(y,bounds.height);width=Math.min(width,bounds.width-x);height=Math.min(height,bounds.height-y);
      current.view.setBounds({x,y,width:Math.max(0,width),height:Math.max(0,height)});
      const visible=!!body.visible&&width>0&&height>0;
      if(visible){
        if(current.view.webContents.getZoomFactor()!==zoom)current.view.webContents.setZoomFactor(zoom);
        for(const other of views.values())if(other!==current)other.view.setVisible(false);
      }
      current.view.setVisible(visible);return {id:current.id,open:true};
    }
    throw Error('未知旧页管理操作');
  }
  return {command,close};
};
