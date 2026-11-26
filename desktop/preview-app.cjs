// 承载独立数据目录下的开发预览和启动冒烟检查，最后由正式 Electron 主入口创建窗口。
// 主要引用或加载入口包括 `preview.cjs`。
const fs=require('node:fs'),path=require('node:path');
const {app}=require('electron');
const home=process.env.MODOT_PREVIEW_HOME;
if(!home||!process.env.HARNESS_USER_DATA||!process.env.HARNESS_DATA)throw Error('Use Modot-Preview.bat to launch the preview.');
app.setPath('userData',process.env.HARNESS_USER_DATA);
if(!app.requestSingleInstanceLock())app.quit();
else{
 let mainWindow, painted=false, openRequested=false;
 const reveal=()=>{
  if(!openRequested||!painted||!mainWindow||mainWindow.isDestroyed())return;
  openRequested=false;
  if(mainWindow.isMinimized())mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
 };
 app.on('browser-window-created',(_event,win)=>{
  if(mainWindow&&!mainWindow.isDestroyed())return;
  mainWindow=win;painted=false;
  win.once('ready-to-show',()=>{painted=true;reveal();});
 });
 app.on('second-instance',()=>{openRequested=true;reveal();});
 if(process.env.MODOT_PREVIEW_SMOKE==='1'){
  const resultPath=path.join(home,'smoke-result.json');let finished=false;
  const finish=(result)=>{if(finished)return;finished=true;fs.writeFileSync(resultPath,JSON.stringify(result,null,2));app.quit();};
  const timer=setTimeout(()=>finish({passed:false,error:'Preview startup timed out'}),60000);timer.unref();
  app.on('browser-window-created',(_event,win)=>win.webContents.once('did-finish-load',async()=>{
   try{
    for(let i=0;i<200;i++){if(await win.webContents.executeJavaScript(`!!document.querySelector('#agent-composer')`))break;await new Promise(r=>setTimeout(r,50));}
    const state=await win.webContents.executeJavaScript(`(async()=>({navigation:Array.from(document.querySelectorAll('.builtin-application-navigation button')).map(b=>b.getAttribute('aria-label')),health:await window.workbench.request({path:'/api/health?full=1'}),bootstrap:await window.workbench.request({path:'/api/v2/bootstrap'})}))()`);
    if(!['对话','工作间'].every(name=>state.navigation.includes(name)) && !['Chat','Workroom'].every(name=>state.navigation.includes(name)))throw Error('Current frontend navigation did not load');
    if(state.health.status!==200||path.resolve(state.health.body.paths.data)!==path.resolve(process.env.HARNESS_DATA))throw Error('Preview backend or data isolation failed');
    if(state.bootstrap.status!==200)throw Error('Native bridge request failed');
    const models=await win.webContents.executeJavaScript(`window.workbench.request({path:'/api/models'})`);
    if(models.status!==200||models.body.profiles.length!==0)throw Error('Fresh smoke profile must not inherit private models');
    await win.webContents.executeJavaScript(`document.querySelector('.surface-navigation [aria-label="记忆"]').click()`);
    for(let i=0;i<200;i++){if(await win.webContents.executeJavaScript(`!!document.querySelector('.memory-provider-summary button')`))break;await new Promise(r=>setTimeout(r,50));}
    await win.webContents.executeJavaScript(`document.querySelector('.memory-provider-summary button').click()`);
    for(let i=0;i<200;i++){if(await win.webContents.executeJavaScript(`(()=>{const e=document.querySelector('[data-memory-extensions] select');return e&&!e.disabled&&!e.closest('[hidden],[inert]')})()`))break;await new Promise(r=>setTimeout(r,50));}
    const controls=await win.webContents.executeJavaScript(`(()=>{const e=document.querySelector('[data-memory-extensions] select');return {visible:!!e&&!e.closest('[hidden],[inert]'),error:document.querySelector('[data-memory-extensions] [role="alert"]')?.textContent||''}})()`);
    if(!controls.visible||controls.error)throw Error('Memory management preview failed: '+controls.error);
    await win.webContents.executeJavaScript(`new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))`);
    fs.writeFileSync(path.join(home,'preview.png'),(await win.webContents.capturePage()).toPNG());
    clearTimeout(timer);finish({passed:true,navigation:state.navigation,data:state.health.body.paths.data,profile:app.getPath('userData'),models:process.env.HARNESS_MODELS,modelProfiles:models.body.profiles.length,habits:process.env.HARNESS_HABITS,port:process.env.HARNESS_PORT,hidden:!win.isVisible(),memoryManagement:controls,screenshot:path.join(home,'preview.png')});
   }catch(error){clearTimeout(timer);finish({passed:false,error:error.stack});}
  }));
 }
 require('./main.js');
}
