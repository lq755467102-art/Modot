// Development preview: use the real app/bridge with its own persistent profile.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {spawn}=require('node:child_process'),{pathToFileURL}=require('node:url');
const root=path.resolve(__dirname,'..'),renderer=path.join(__dirname,'renderer-v2');
const smoke=process.argv.slice(2).includes('--smoke');
const nativeWindow=process.type==='browser';
async function main(){
 if(process.argv.slice(2).some(arg=>arg!=='--smoke'))throw Error('Supported option: --smoke');
 const local=process.env.LOCALAPPDATA||path.join(os.homedir(),'AppData','Local');
 const home=process.env.MODOT_PREVIEW_HOME?path.resolve(process.env.MODOT_PREVIEW_HOME):smoke
  ?fs.mkdtempSync(path.join(os.tmpdir(),'Modot-preview-smoke-')):path.join(local,'Modot','frontend-preview');
 fs.mkdirSync(home,{recursive:true});
 const env={...process.env};
 const runtimeNames=['HARNESS_PYTHON','HARNESS_SANDBOX_PYTHON_ROOT','HARNESS_GIT'];
 for(const name of Object.keys(env))if(name.startsWith('HARNESS_')&&!runtimeNames.includes(name))delete env[name];
 const localPython=path.join(root,'.venv','Scripts','python.exe');
 if(!env.HARNESS_PYTHON && !fs.existsSync(path.join(__dirname,'python-path.txt')) && fs.existsSync(localPython))env.HARNESS_PYTHON=localPython;
 if(env.HARNESS_PYTHON){process.env.HARNESS_PYTHON=env.HARNESS_PYTHON;}
 const python=require('./find-python.cjs').findPython();
 if(!python)throw Error('Python 3.10+ is required. Set desktop/python-path.txt to a real interpreter.');
 env.HARNESS_PYTHON=python.exe;
 if(!env.HARNESS_SANDBOX_PYTHON_ROOT&&fs.existsSync(path.join(path.dirname(python.exe),'Lib/os.py')))env.HARNESS_SANDBOX_PYTHON_ROOT=path.dirname(python.exe);
 env.PYTHONUTF8='1';env.PYTHONIOENCODING='utf-8';
 env.HARNESS_DATA=path.join(home,'data');env.HARNESS_USER_DATA=path.join(home,'profile');
 Object.assign(env,{
  HARNESS_MODELS:path.join(home,'models.json'),HARNESS_CREDS:path.join(home,'credentials.env'),
  HARNESS_HABITS:path.join(home,'HABITS.md'),HARNESS_HABITS_INDEX:path.join(home,'habits.json'),
  HARNESS_FACTS:path.join(home,'facts'),HARNESS_SKILLS:path.join(home,'skills'),
  HARNESS_HOOKS:path.join(home,'hooks.json'),HARNESS_GRANTS:path.join(home,'grants.jsonl'),
  HARNESS_BASELINE_HITS:path.join(home,'baseline-hits.jsonl'),
 });
 env.HARNESS_PORT=process.env.MODOT_PREVIEW_PORT||'18798';
 if(!/^\d+$/.test(env.HARNESS_PORT)||+env.HARNESS_PORT<1024||+env.HARNESS_PORT>65535)throw Error('Invalid MODOT_PREVIEW_PORT.');
 if(smoke){env.MODOT_PREVIEW_SMOKE='1';env.HARNESS_START_HIDDEN='1';}
 else delete env.MODOT_PREVIEW_SMOKE;
 env.MODOT_PREVIEW_HOME=home;
 for(const dir of [env.HARNESS_DATA,env.HARNESS_USER_DATA])fs.mkdirSync(dir,{recursive:true});
 // A frozen acceptance preview can start this entry directly with Electron.
 // Keep the same isolated settings as the build launcher, including removed overrides.
 if(nativeWindow&&!smoke){
  if(!fs.existsSync(path.join(renderer,'dist','index.html')))throw Error('预览界面尚未构建。请运行源码目录里的 Modot-Preview.bat。');
  for(const name of Object.keys(process.env))if(!Object.hasOwn(env,name))delete process.env[name];
  for(const [name,value] of Object.entries(env))if(process.env[name]!==value)process.env[name]=value;
  require('./preview-app.cjs');
  return;
 }
 if(!fs.existsSync(path.join(renderer,'node_modules/vite/package.json')))throw Error('Frontend dependencies are missing. Run npm ci in desktop/renderer-v2.');
 console.log('Building the current Modot frontend...');
 const {build}=await import(pathToFileURL(require.resolve('./renderer-v2/node_modules/vite')).href);
 // Retain old hashed assets: an already open development window may still use them.
 await build({root:renderer,configFile:path.join(renderer,'vite.config.ts'),build:{outDir:path.join(renderer,'dist'),emptyOutDir:false},logLevel:'error'});
 delete env.ELECTRON_RUN_AS_NODE;
 const exe=path.join(__dirname,'node_modules/electron/dist/electron.exe');
 const log=path.join(home,'preview.log'),fd=fs.openSync(log,'a');
 console.log('Preview data: '+home);console.log('Startup log: '+log);
 const child=spawn(exe,[path.join(__dirname,'preview-app.cjs')],{cwd:root,env,detached:!smoke,windowsHide:true,stdio:['ignore',fd,fd]});
 fs.closeSync(fd);
 child.on('error',error=>{console.error(error.message);process.exitCode=1;});
 if(smoke){
  await new Promise((resolve,reject)=>{child.once('error',reject);child.once('exit',resolve);});
  const resultPath=path.join(home,'smoke-result.json');
  const result=JSON.parse(fs.readFileSync(resultPath,'utf8'));
  if(!result.passed)throw Error(result.error||'Preview smoke failed; inspect '+log);
  console.log('Preview smoke passed: '+resultPath);
 }else{await new Promise((resolve,reject)=>{child.once('spawn',resolve);child.once('error',reject);});child.unref();console.log('Modot preview started.');}
}
main().catch(error=>{
 if(nativeWindow){
  const {app,dialog}=require('electron');
  dialog.showErrorBox('Modot 预览无法打开',error.message);
  app.exit(1);
 }else{console.error('Preview failed: '+error.message);process.exitCode=1;}
});
