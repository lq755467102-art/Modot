// The user selects the program. No PATH search, URL handler or assumed editor.
// 主要引用方包括 `bridge-v2.cjs`。
const fs=require('node:fs'),path=require('node:path'),{spawn}=require('node:child_process');
module.exports=function({app,dialog,getWindow}){
  const preference=()=>path.join(app.getPath('userData'),'external-editor.json');
  let choosing;
  function valid(file){
    return typeof file==='string'&&path.isAbsolute(file)&&fs.existsSync(file)&&fs.statSync(file).isFile()&&
      (process.platform!=='win32'||path.extname(file).toLowerCase()==='.exe');
  }
  async function pick(title,buttonLabel){
    const result=await dialog.showOpenDialog(getWindow(),{title,buttonLabel,
      properties:['openFile'],...(process.platform==='win32'?{filters:[{name:'应用程序',extensions:['exe']}]}:{})});
    if(result.canceled||!result.filePaths[0])return null;
    const executable=fs.realpathSync(result.filePaths[0]);
    if(!valid(executable))throw Error('请选择可用的应用程序');
    return executable;
  }
  async function choose(){
    if(choosing)return choosing;
    choosing=(async()=>{
      const executable=await pick('选择外部编辑器','使用此编辑器');
      if(!executable)return null;
      const file=preference(),temp=file+'.'+process.pid+'.tmp';
      fs.mkdirSync(path.dirname(file),{recursive:true});
      fs.writeFileSync(temp,JSON.stringify({executable}),{encoding:'utf8',mode:0o600});
      fs.renameSync(temp,file);
      return executable;
    })();
    try{return await choosing;}finally{choosing=undefined;}
  }
  async function selected(){
    const file=preference();
    if(fs.existsSync(file)){
      let saved;
      try{saved=JSON.parse(fs.readFileSync(file,'utf8'));}
      catch{throw Error('外部编辑器设置无法读取，请在工作间更多操作中重新选择');}
      if(valid(saved.executable))return saved.executable;
    }
    return choose();
  }
  async function open(executable,target,line,cwd){
    if(!valid(executable))throw Error('编辑器程序已不可用，请重新选择');
    const name=path.basename(executable).toLowerCase();
    const args=name==='devenv.exe'?['/Edit',target]:/^(code|code-insiders|codium)(\.exe)?$/.test(name)
      ?['--goto',target+':'+Math.max(1,Math.floor(Number(line)||1))]:[target];
    await new Promise((resolve,reject)=>{
      const child=spawn(executable,args,{cwd,shell:false,windowsHide:true,detached:true,stdio:'ignore'});
      child.once('error',()=>reject(Error('无法启动所选编辑器，请在工作间更多操作中重新选择')));
      child.once('spawn',()=>{child.unref();resolve();});
    });
    return {opened:true,name:path.basename(executable)};
  }
  return {choose,pick,selected,open};
};
