// A launcher may close its console before Electron exits. Stdio is optional;
// an EPIPE while Electron reports another error must not open a second modal.
// 主要引用方包括 `main.js`。
const guarded=new WeakSet();
module.exports=function guardStdio(){
  for(const stream of [process.stdout,process.stderr]){
    if(!stream||guarded.has(stream))continue;
    guarded.add(stream);let broken=false;
    stream.on('error',error=>{
      if(error?.code==='EPIPE'){broken=true;return;}
      if(broken&&error?.code==='ERR_STREAM_DESTROYED')return;
      throw error;
    });
  }
};
