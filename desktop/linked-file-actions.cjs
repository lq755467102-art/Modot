// 将会话文件的复制、定位和打开动作交给桌面宿主。
// 主要引用方包括 `bridge-v2.cjs`。
const fs=require('node:fs/promises');
const {COPYFILE_EXCL}=require('node:fs').constants;
const path=require('node:path');
const {randomUUID}=require('node:crypto');

async function fileStamp(file,allowMissing=false) {
  let stat;
  try{stat=await fs.lstat(file,{bigint:true});}
  catch(error){if(allowMissing&&error.code==='ENOENT')return null;throw error;}
  if(!stat.isFile())throw Error('所选位置不是普通文件');
  return {identity:stat.dev+':'+stat.ino,version:[stat.size,stat.mtimeNs,stat.ctimeNs].join(':')};
}

const sameLocation=(a,b)=>path.relative(a,b)==='';
const sameFile=(a,b)=>a?.identity===b?.identity&&a?.version===b?.version;

module.exports=function linkedFileActions({request,getWindow,dialog,shell,externalEditor,dataRoot}){
  async function authorize(selection){
    const {conversationId,resourceId,scopeRevision,path:relativePath,absolutePath}=selection;
    if(typeof conversationId!=='string'||typeof resourceId!=='string'||typeof scopeRevision!=='string'||
       typeof relativePath!=='string'||typeof absolutePath!=='string'||!path.isAbsolute(absolutePath))
      throw Error('文件来源无效');
    const query=new URLSearchParams({conversationId,resourceId,scopeRevision,path:relativePath});
    const result=await request({path:'/api/v2/workroom/linked-file?'+query.toString().replaceAll('..','%2E%2E')});
    if(result.status!==200)throw Error(result.body.error||'文件范围已变化');
    const {absolutePath:resolved,defaultOpen}=result.body;
    if(typeof resolved!=='string'||!path.isAbsolute(resolved)||!sameLocation(absolutePath,resolved)||
       !sameLocation(await fs.realpath(resolved),resolved))throw Error('文件位置已变化，请重新打开链接');
    return {path:resolved,defaultOpen,stamp:await fileStamp(resolved)};
  }

  async function saveAs(source,recheck){
    const result=await dialog.showSaveDialog(getWindow(),{title:'另存文件',defaultPath:path.basename(source.path)});
    if(result.canceled||!result.filePath)return {cancelled:true};
    if(!path.isAbsolute(result.filePath))throw Error('请选择有效的保存位置');
    const parent=await fs.realpath(path.dirname(result.filePath));
    const target=path.join(parent,path.basename(result.filePath));
    if(path.basename(target).includes(':')||sameLocation(source.path,target))throw Error('请选择不同的文件位置');
    const fromData=path.relative(await fs.realpath(dataRoot),target);
    if(fromData===''||(!path.isAbsolute(fromData)&&fromData!=='..'&&!fromData.startsWith('..'+path.sep)))
      throw Error('请选择应用内部数据目录以外的保存位置');
    const before=await fileStamp(target,true);
    if(before?.identity===source.stamp.identity)throw Error('请选择不同的文件位置');
    if(before){
      const answer=await dialog.showMessageBox(getWindow(),{type:'warning',message:'覆盖现有文件？',
        detail:target,buttons:['取消','覆盖'],defaultId:0,cancelId:0});
      if(answer.response!==1)return {cancelled:true};
    }
    const current=await recheck();
    if(!sameFile(current.stamp,source.stamp))throw Error('来源文件已变化，请重新另存');
    const temporary=path.join(parent,'.modot-copy-'+randomUUID());
    try{
      await fs.copyFile(current.path,temporary,COPYFILE_EXCL);
      const checked=await recheck();
      if(!sameFile(checked.stamp,current.stamp))throw Error('来源文件已变化，请重新另存');
      if(!sameLocation(await fs.realpath(path.dirname(result.filePath)),parent)||
         !sameFile(before,await fileStamp(target,true)))throw Error('保存位置已变化，请重新选择');
      if(before)await fs.rename(temporary,target);
      else await fs.copyFile(temporary,target,COPYFILE_EXCL);
      return {path:target};
    }finally{
      await fs.rm(temporary,{force:true});
    }
  }

  return async function act(selection){
    if(!selection||!['open','choose','reveal','save'].includes(selection.action))throw Error('未知文件操作');
    const recheck=()=>authorize(selection);
    const file=await recheck();
    if(selection.action==='reveal'){
      shell.showItemInFolder(file.path);
      return {};
    }
    if(selection.action==='open'){
      if(!file.defaultOpen)throw Error('请为此文件选择打开应用');
      const error=await shell.openPath(file.path);
      if(error)throw Error(error);
      return {};
    }
    if(selection.action==='save')return saveAs(file,recheck);
    const program=await externalEditor.pick('选择用于打开文件的应用','打开文件');
    if(!program)return {cancelled:true};
    const current=await recheck();
    return externalEditor.open(program,current.path,selection.line,path.dirname(current.path));
  };
};
