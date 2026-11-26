// Draft public contract over real package installation, hidden Electron and L0.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
module.exports=async({win,js,wait,request,out,passed,setFixture,restartBackend})=>{
 const get=async p=>(await js(`window.workbench.request({path:${JSON.stringify(p)}})`)).body;
 const post=async(p,b)=>{const r=await request(p,b);assert.equal(r.status,200,JSON.stringify(r.body));return r.body;};
 const sleep=ms=>new Promise(r=>setTimeout(r,ms));
 const until=async fn=>{const end=Date.now()+30000;while(Date.now()<end){if(fn())return;await sleep(50);}throw Error('fixture state timeout');};
 const data=path.join(out,'data'),runs=path.join(data,'runs');
 const read=p=>JSON.parse(fs.readFileSync(p,'utf8'));
 const rows=()=>{const dir=path.join(runs,'workbench','subtasks',thread.id);return fs.existsSync(dir)?fs.readdirSync(dir).filter(f=>f.endsWith('.json')).map(f=>read(path.join(dir,f))):[];};
 const row=id=>rows().find(r=>r.id===id);
 const finish=async route=>wait(`window.workbench.request({path:${JSON.stringify(route+'/progress')}}).then(r=>!r.body.runtime.active&&!['starting','running'].includes(r.body.runtime.state))`,180000);
 const say=(res,text)=>{if(res.destroyed)return;res.writeHead(200,{'Content-Type':'text/event-stream'});res.end('data: '+JSON.stringify({choices:[{index:0,delta:{content:text},finish_reason:'stop'}]})+'\n\ndata: [DONE]\n\n');};
 const shape={type:'object',properties:{note:{type:'string',maxLength:1000}},required:['note'],additionalProperties:false};
 const source=path.join(out,'worker-contract-package');fs.mkdirSync(source);
 fs.writeFileSync(path.join(source,'plugin.json'),JSON.stringify({name:'p1-conformance-only',version:'0.1.0',workers:[{name:'fixture',protocol:'modot.worker/1-draft',description:'Contract fixture, not official assistant',method:'METHOD_PRIVATE: investigate only the explicit delegation. Publish facts.',inputSchema:shape,outputSchema:shape,requires:['native-l0','messages','resume','structured-result']}]}));
 const preview=await post('/api/v2/plugins/preview',{path:source});const pkg=await post('/api/v2/plugins/install',{path:source,fingerprint:preview.fingerprint});
 assert.equal(pkg.workersEnabled,false);
 await js("document.querySelector('.surface-navigation [aria-label=插件管理]').click()");
 const rowButton=`button[data-catalog-key="${pkg.id}"]`;
 await wait(`!!document.querySelector(${JSON.stringify(rowButton)})`);
 assert.match(await js(`document.querySelector(${JSON.stringify(rowButton)}).textContent`),/任务核心.*已停用/);
 await js(`document.querySelector(${JSON.stringify(rowButton)}).click()`);
 await wait("document.querySelector('.catalog-detail')?.textContent.includes('Contract fixture')");
 await js("[...document.querySelectorAll('.catalog-segments button')].find(b=>b.textContent==='管理').click()");
 const toggle='input[aria-label="启用子 Agent 贡献 p1-conformance-only"]';
 await wait(`!!document.querySelector(${JSON.stringify(toggle)})`);
 assert.equal(await js(`document.querySelector(${JSON.stringify(toggle)}).checked`),false);
 await js(`document.querySelector(${JSON.stringify(toggle)}).click()`);
 await wait(`window.workbench.request({path:'/api/v2/plugins'}).then(r=>r.body.plugins.some(p=>p.id==='${pkg.id}'&&p.workersEnabled))`);
 let installed=(await get('/api/v2/plugins')).plugins.find(p=>p.id===pkg.id);
 await wait("document.querySelector('.catalog-identity')?.textContent.includes('已启用')");
 await js("document.querySelector('.surface-navigation [aria-label=对话]').click()");
 passed.push('existing plugin UI exposes a separate worker toggle, disabled after install and explicitly enabled for the next run');
 const thread=await post('/api/v2/conversations/threads',{title:'P1 · 任务核心公开契约／续接、取消、强退与成果回收'});
 const route='/api/v2/conversations/threads/'+thread.id;
 const cfg=await get(route+'/execution');await post(route+'/execution',{expectedRevision:cfg.next.revision,crew:'l0',charter:'off',maxTurns:0,modelCalls:0,contextLimit:131072});
 let parentStep=0,childSteps={},first,second,crashed,revoked,resultId,firstArgs,heldCrash,heldRevoke;
 const wire=[],faults=[];
 const worker=pkg.id+'/fixture';
 const last=body=>JSON.parse([...body.messages].reverse().find(m=>m.role==='tool')?.content||'{}');
 const toolCall=(res,tool,name,args)=>tool(res,name,args,'contract-'+Math.random().toString(16).slice(2));
 setFixture((body,res,tool)=>{(async()=>{try{
  const system=body.messages[0].content,child=system.includes('METHOD_PRIVATE');
  const text=JSON.stringify(body.messages),value=last(body);
  wire.push({child,parentStep,body:structuredClone(body)});
  if(child){
   const name=text.includes('CRASH_INPUT')?'crash':text.includes('REVOKE_INPUT')?'revoke':text.includes('SECOND_INPUT')?'second':'first';
   const step=childSteps[name]||0;childSteps[name]=step+1;
   assert.ok(body.tools.some(t=>t.function.name==='submit_worker_result'));
   assert.ok(!text.includes('PARENT_PRIVATE'));
   if(name==='first'){
    if(step===0)toolCall(res,tool,'ask_human',{question:'FIRST_QUESTION: please supply the allowed detail.'});
    else if(step===1){assert.ok(text.includes('ANSWER_ONE'));assert.ok(text.includes('FIRST_QUESTION'));toolCall(res,tool,'submit_worker_result',{submission_key:'first-result',data:{note:'RESULT_ONE'}});}
    else if(step===2){resultId=value.result_id;toolCall(res,tool,'submit_worker_result',{submission_key:'first-result',data:{note:'RESULT_ONE'}});}
    else {assert.equal(value.duplicate,true);say(res,'FIRST_FINAL: result saved without acceptance.');}
   }else if(name==='second'){
    if(step===0)toolCall(res,tool,'read_task_requirements',{});
    else {heldRevoke=res; /* Parent cancels this active call. */}
   }else if(name==='crash'){
    if(step===0)toolCall(res,tool,'submit_worker_result',{submission_key:'before-crash',data:{note:'CRASH_SAVED'}});
    else if(step===1){heldCrash=res;}
    else if(step===2){assert.ok(text.includes('CRASH_SAVED'));assert.ok(text.includes('RECOVER_WITH_HISTORY'));toolCall(res,tool,'submit_worker_result',{submission_key:'after-crash',data:{note:'CRASH_RECOVERED'}});}
    else say(res,'CRASH_FINAL: recovered own history.');
   }else{
    if(step===0){heldRevoke=res;await until(()=>installed.workersEnabled===false);toolCall(res,tool,'submit_worker_result',{submission_key:'forbidden',data:{note:'MUST_NOT_SAVE'}});}
    else say(res,'REVOKED_END: stopped.');
   }
  }else{
   assert.ok(!system.includes('METHOD_PRIVATE'));
   const step=parentStep++;
   const manage=(id,action,extra={})=>toolCall(res,tool,'manage_plugin_worker',{delegation_id:id,action,...extra});
   switch(step){
    case 0:toolCall(res,tool,'use_tool_group',{group:'plugins'});break;
    case 1:toolCall(res,tool,'inspect_worker_plugins',{});break;
    case 2:assert.equal(value.workers[0].worker,worker);firstArgs={worker,request_key:'first',input:{note:'FIRST_INPUT'},resource_ids:[],kind:'report'};toolCall(res,tool,'delegate_plugin_worker',firstArgs);break;
    case 3:assert.equal(value.lifetime,'parent-run');first=value.delegation_id;toolCall(res,tool,'delegate_plugin_worker',firstArgs);break;
    case 4:assert.equal(value.delegation_id,first);await until(()=>row(first)?.state==='blocked');manage(first,'query');break;
    case 5:assert.equal(value.state,'blocked');manage(first,'resume',{request_key:'resume-first',text:'ANSWER_ONE: keep original scope, return the recorded facts.'});break;
    case 6:assert.equal(value.child_session_id,row(first).childId);manage(first,'resume',{request_key:'resume-first',text:'ANSWER_ONE: keep original scope, return the recorded facts.'});break;
    case 7:assert.equal(value.duplicate,true);await until(()=>row(first)?.state==='completed');manage(first,'result',{result_id:resultId});break;
    case 8:assert.equal(value.data.note,'RESULT_ONE');assert.ok(row(first).worker.results[0].delivery.includedIn);manage(first,'receipt',{request_key:'receipt-first',result_id:resultId,disposition:'partial'});break;
    case 9:toolCall(res,tool,'delegate_plugin_worker',{worker,request_key:'second',input:{note:'SECOND_INPUT'},resource_ids:[],kind:'report'});break;
    case 10:second=value.delegation_id;await until(()=>childSteps.second>=2);manage(second,'cancel');break;
    case 11:assert.equal(value.stop_requested,true);await until(()=>row(second)?.state==='cancelled');manage(second,'cancel');break;
    case 12:assert.equal(value.state,'cancelled');toolCall(res,tool,'delegate_plugin_worker',{worker,request_key:'crash',input:{note:'CRASH_INPUT'},resource_ids:[],kind:'report'});break;
    case 13:crashed=value.delegation_id;await until(()=>!!heldCrash);process.kill(read(path.join(runs,row(crashed).childId,'runtime.json')).pid,'SIGKILL');await until(()=>row(crashed)?.state==='unknown');manage(crashed,'query');break;
    case 14:assert.equal(value.state,'unknown');assert.equal(value.results.length,1);manage(crashed,'resume',{request_key:'recover',text:'RECOVER_WITH_HISTORY: inspect existing evidence, continue without repeating effects.'});break;
    case 15:await until(()=>row(crashed)?.state==='completed');manage(crashed,'query');break;
    case 16:assert.equal(value.results.length,2);toolCall(res,tool,'delegate_plugin_worker',{worker,request_key:'revoke',input:{note:'REVOKE_INPUT'},resource_ids:[],kind:'report'});break;
    case 17:revoked=value.delegation_id;await until(()=>childSteps.revoke>=1);installed=await post('/api/v2/plugins/'+pkg.id,{expectedRevision:installed.revision,workersEnabled:false});await until(()=>['cancelled','blocked','failed'].includes(row(revoked)?.state));manage(first,'result',{result_id:resultId});break;
    default:assert.equal(value.data.note,'RESULT_ONE');say(res,'PARENT_FINAL: explicit receipt; unfinished or revoked work is not acceptance.');
   }
  }
 }catch(e){faults.push(e.stack);say(res,'FIXTURE_ERROR: '+e.message);}})();return true;});
 try{
  await post(route+'/send',{text:'PARENT_PRIVATE: verify the public worker contract. Use only explicit delegation inputs. No official assistants.'});
  await finish(route);assert.deepEqual(faults,[]);assert.equal((await get(route+'/progress')).runtime.state,'completed');
  assert.equal(rows().length,4);assert.equal(row(first).worker.results.length,1);assert.equal(row(first).worker.results[0].disposition.value,'partial');
  assert.equal(row(revoked).worker.results.length,0);assert.equal(row(crashed).worker.attempt,2);
  const child=read(path.join(runs,row(first).childId,'session.json'));assert.equal(child.status,'success');assert.ok(JSON.stringify(child.msgs).includes('FIRST_QUESTION'));
  assert.ok(JSON.stringify(child.msgs).includes('ANSWER_ONE'));
  for(const r of rows()){const files=fs.readdirSync(path.join(runs,r.childId,'execution-runs'));for(const f of files.filter(f=>f.endsWith('.json'))){assert.equal(read(path.join(runs,r.childId,'execution-runs',f)).config.contextLimit,131072);}}
  const original=JSON.stringify(rows());await restartBackend();assert.equal(JSON.stringify(rows()),original);
  const archive=await get(route+'/subtasks/overview');assert.equal(archive.tasks.length,4);
  // Kill the host while both a parent request and a child request are in flight.
  // The default parent-run mode keeps submitted evidence after a host crash.
  installed=await post('/api/v2/plugins/'+pkg.id,{expectedRevision:installed.revision,workersEnabled:true,confirm:true});
  const recovery=await post('/api/v2/conversations/threads',{title:'P1 · 运行中强退宿主／保留成果、显式收回'});
  const recoveryRoute='/api/v2/conversations/threads/'+recovery.id;
  const rcfg=await get(recoveryRoute+'/execution');await post(recoveryRoute+'/execution',{expectedRevision:rcfg.next.revision,crew:'l0',charter:'off',maxTurns:0,modelCalls:0,contextLimit:131072});
  const recoveryRows=()=>{const d=path.join(runs,'workbench','subtasks',recovery.id);return fs.existsSync(d)?fs.readdirSync(d).filter(f=>f.endsWith('.json')).map(f=>read(path.join(d,f))):[];};
  let rp=0,rc=0,holdingParent,holdingChild;
  setFixture((body,res,tool)=>{wire.push({mode:'host-crash',body:structuredClone(body)});
   if(body.messages[0].content.includes('METHOD_PRIVATE')){
    if(rc++===0)toolCall(res,tool,'submit_worker_result',{submission_key:'host-crash',data:{note:'HOST_CRASH_SAVED'}});else holdingChild=res;
   }else switch(rp++){
    case 0:toolCall(res,tool,'use_tool_group',{group:'plugins'});break;
    case 1:toolCall(res,tool,'delegate_plugin_worker',{worker,request_key:'host-crash',input:{note:'HOST_CRASH_INPUT'},resource_ids:[],kind:'report'});break;
    default:holdingParent=res;
   }return true;});
  await post(recoveryRoute+'/send',{text:'HOST_CRASH_PARENT: delegate one report and keep evidence.'});
  await until(()=>holdingParent&&holdingChild&&recoveryRows()[0]?.worker.results.length===1);
  const retained=recoveryRows()[0],savedId=retained.worker.results[0].result_id;
  await restartBackend();holdingParent.destroy();holdingChild.destroy();
  await finish(recoveryRoute);await finish('/api/v2/conversations/threads/'+retained.childId);
  assert.equal(recoveryRows().length,1);assert.equal(recoveryRows()[0].worker.results[0].result_id,savedId);
  await post(recoveryRoute+'/subtasks/'+retained.id+'/cancel',{});
  assert.equal(recoveryRows()[0].state,'cancelled');
  let recoverStep=0;
  setFixture((body,res,tool)=>{wire.push({mode:'host-recovery-read',body:structuredClone(body)});const v=last(body);
   switch(recoverStep++){
    case 0:toolCall(res,tool,'use_tool_group',{group:'plugins'});break;
    case 1:toolCall(res,tool,'manage_plugin_worker',{delegation_id:retained.id,action:'result',result_id:savedId});break;
    case 2:assert.equal(v.data.note,'HOST_CRASH_SAVED');toolCall(res,tool,'manage_plugin_worker',{delegation_id:retained.id,action:'resume',request_key:'cross-run-not-supported',text:'continue'});break;
    default:assert.equal(v.code,'worker_lifetime');say(res,'RECOVERY_FINAL: evidence recovered, no automatic replay or cross-run execution.');
   }return true;});
  await post(recoveryRoute+'/send',{text:'继续'});await finish(recoveryRoute);
  assert.equal((await get(recoveryRoute+'/progress')).runtime.state,'completed');assert.equal(recoveryRows().length,1);assert.equal(rc,2);
  passed.push('active host force-kill with parent-run lifetime: saved result survives, explicit cancellation releases ownership, same-task evidence is readable and the running delegation cannot resume in a later parent run');
  assert.equal(win.isVisible(),false);
  passed.push('worker public contract: package activation, lazy discovery, duplicate delegation, same-history resume, immutable result submission and separate adoption receipt');
  passed.push('worker public contract: cancel, killed child recovery without replay, revocation rejects old result handle, persisted results survive backend force restart');
  passed.push('worker actual context: parent excludes private method; child excludes parent private task; 131072 and native L0 preserved; hidden Electron throughout');
  fs.writeFileSync(path.join(out,'worker-contract-cases.json'),JSON.stringify({parent:thread.id,recoveryParent:recovery.id,plugin:pkg.id,delegations:[...rows(),...recoveryRows()].map(r=>({id:r.id,child:r.childId,state:r.state}))},null,2));
 }finally{setFixture(null);for(const res of [heldCrash,heldRevoke])if(res&&!res.destroyed)res.destroy();fs.writeFileSync(path.join(out,'worker-contract-wire.json'),JSON.stringify({wire,faults},null,2));}
};
