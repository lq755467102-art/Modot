// Fixed Provider responses through the real, hidden Electron managed-run path.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
module.exports=async({win,js,wait,request,out,passed,setFixture,restartBackend})=>{
 const get=async p=>(await js(`window.workbench.request({path:${JSON.stringify(p)}})`)).body;
 const post=async(p,b)=>{const r=await request(p,b);assert.equal(r.status,200,JSON.stringify(r.body));return r.body;};
 const finish=async route=>wait(`window.workbench.request({path:${JSON.stringify(route+'/progress')}}).then(r=>!r.body.runtime.active&&!['starting','running'].includes(r.body.runtime.state))`,120000);
 const data=path.join(out,'data'),registry=()=>JSON.parse(fs.readFileSync(path.join(data,'runs/workbench/registry.json'),'utf8'));
 const events=id=>fs.readFileSync(path.join(data,'runs',id,'events.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
 const root=path.join(out,'native-child-project');fs.mkdirSync(root);
 const source='# KEEP_HUMAN_NOTE\nVALUE = 4\n';fs.writeFileSync(path.join(root,'sample.py'),source);
 for(const args of [['init','-b','agent/native-children'],['add','.'],['-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','baseline']])execFileSync(process.env.HARNESS_GIT,args,{cwd:root,windowsHide:true});
 const project=await post('/api/v2/projects',{name:'原生 L0 任务核心 P0 · 隔离与独立历史'});
 const resource=await post(`/api/v2/projects/${project.id}/resources`,{kind:'code',path:root});
 await post(`/api/v2/projects/${project.id}/resources/${resource.id}/permission`,{permission:'read',write:true,execute:true,confirm:true});
 const thread=await post('/api/v2/conversations/threads',{projectId:project.id,title:'01 · 主子原生 L0、独立历史与隔离检查'});
 const route='/api/v2/conversations/threads/'+thread.id;
 await post(route+'/scope',{projectId:project.id,resourceIds:[resource.id]});
 const cfg=await get(route+'/execution');await post(route+'/execution',{expectedRevision:cfg.next.revision,crew:'l0',charter:'off',maxTurns:0,modelCalls:0,contextLimit:131072});
 let parentStep=0,childStep=0,childId,revision,check;
 let releaseChild,markChildReady;
 const parentHasWorked=new Promise(resolve=>{releaseChild=resolve;});
 const childIsReading=new Promise(resolve=>{markChildReady=resolve;});
 const wire=[],faults=[];
 const say=(res,text)=>{res.writeHead(200,{'Content-Type':'text/event-stream'});res.end('data: '+JSON.stringify({choices:[{index:0,delta:{content:text},finish_reason:'stop'}]})+'\n\ndata: [DONE]\n\n');};
 const last=body=>JSON.parse([...body.messages].reverse().find(m=>m.role==='tool')?.content||'{}');
 setFixture((body,res,tool)=>{try{
  const first=body.messages.find(m=>m.role==='user')?.content||'';
  const child=first.includes('NATIVE_CHILD_GOAL:');
  wire.push({child,step:child?childStep:parentStep,body:structuredClone(body)});
  const value=last(body),names=(body.tools||[]).map(t=>t.function.name);
  if(child){
   const r=registry();const childThread=Object.values(r.threads).find(t=>t.subtaskOf?.parentId===thread.id);childId=childThread.id;
   const cr=Object.values(r.projects[childThread.projectId].resources)[0];
   assert.ok(names.includes('use_tool_group'));assert.ok(names.includes('search_project_code'));
   assert.ok(!names.includes('create_subtask'));assert.ok(!names.includes('call_plugin_tool'));
   switch(childStep++){
    case 0: assert.ok(names.includes('load_skill'));tool(res,'load_skill',{name:'native-child-fixture'},'child-skill');break;
    case 1: assert.ok(JSON.stringify(value).includes('NATIVE_SKILL_BODY'));markChildReady();
     parentHasWorked.then(()=>tool(res,'search_project_code',{resource_id:cr.id,query:'VALUE'},'child-search'));break;
    case 2: tool(res,'inspect_code',{resource_id:cr.id,relative_path:'sample.py'},'child-read');break;
    case 3: revision=value.revision;tool(res,'edit_project_file',{resource_id:cr.id,relative_path:'sample.py',old_text:'VALUE = 4',new_text:'VALUE = 5',expected_revision:revision},'child-edit');break;
    case 4: assert.equal(value.ok,true,JSON.stringify(value));tool(res,'run_project_check',{resource_id:cr.id,script:'from pathlib import Path\nassert "VALUE = 5" in Path("sample.py").read_text()\nassert "KEEP_HUMAN_NOTE" in Path("sample.py").read_text()\nprint("CHILD_CHECK_CURRENT_VERSION")',purpose:'exploration',timeout:30},'child-check');break;
    case 5: assert.equal(value.process?.exitCode,0,JSON.stringify(value));check=value;tool(res,'use_tool_group',{group:'verification'},'child-group');break;
    case 6: assert.ok(names.includes('inspect_delivery'));tool(res,'create_subtask',{request_key:'forbidden',goal:'must reject',resource_ids:[],kind:'report'},'child-recursion');break;
    case 7: assert.equal(value.code,'child_capability_denied');tool(res,'inspect_code',{resource_id:resource.id,relative_path:'sample.py'},'child-other-resource');break;
    case 8: assert.equal(value.code,'resource_denied');tool(res,'record_working_summary',{summary:'CHILD_PRIVATE_HISTORY: changed the isolated file; test receipt is evidence for that version only.',sources:['sample.py:2','child-check: actual exit 0']},'child-summary');break;
    default:assert.equal(value.ok,true);say(res,'CHILD_FINISHED: 独立历史已保存，隔离文件已修改并实际测试；没有整合父工程。');
   }
  }else{
   switch(parentStep++){
    case 0: tool(res,'inspect_code',{resource_id:resource.id},'parent-read');break;
    case 1: revision=value.projectRevision;tool(res,'use_tool_group',{group:'subtasks'},'parent-group');break;
    case 2: tool(res,'create_subtask',{request_key:'native-code',goal:'NATIVE_CHILD_GOAL: edit only the isolated sample.py and test it. Preserve KEEP_HUMAN_NOTE.',resource_ids:[resource.id],kind:'code',expected_revision:revision},'parent-dispatch');break;
    case 3: assert.equal(value.ok,true);assert.ok(!JSON.stringify(body.messages).includes('CHILD_FINISHED'));
     tool(res,'inspect_code',{resource_id:resource.id,relative_path:'sample.py'},'parent-independent-read');break;
    case 4: assert.ok(value.text.includes('KEEP_HUMAN_NOTE'));
     childIsReading.then(()=>{say(res,'PARENT_DRAFT: 父目录已核对，等待隔离副本结果。');releaseChild();});break;
    default:assert.equal(body.messages.filter(m=>typeof m.content==='string'&&m.content.startsWith('系统实际子任务汇总')&&m.content.includes('CHILD_FINISHED')).length,1);say(res,'PARENT_FINISHED: 收到子任务成果，保留父工程原文件，没有自动合并。');
   }
  }
 }catch(error){faults.push(error.stack);say(res,'FIXTURE_ERROR: '+error.message);}return true;});
 try{
  await post(route+'/send',{text:'NATIVE_PARENT_GOAL: delegate one isolated code task; preserve KEEP_HUMAN_NOTE and report facts without merging.'});
  await finish(route);assert.deepEqual(faults,[]);
  assert.equal((await get(route+'/progress')).runtime.state,'completed');assert.ok(childId);
  const childDir=path.join(data,'runs',childId),state=JSON.parse(fs.readFileSync(path.join(childDir,'session.json'),'utf8'));
  assert.ok(state.executorWork);assert.equal(state.status,'success');assert.ok(JSON.stringify(state.msgs).includes('CHILD_PRIVATE_HISTORY'));
  assert.ok(!JSON.stringify(state.msgs).includes('PARENT_FINISHED'));assert.equal(fs.readFileSync(path.join(root,'sample.py'),'utf8'),source);
  const configs=events(childId).filter(e=>e.act==='execution_config');assert.equal(configs.length,1);
  assert.equal(configs[0].config.contextLimit,131072);assert.equal(configs[0].config.maxTurns,0);assert.equal(configs[0].config.modelCalls,0);
  const parentRequest=wire.find(w=>!w.child).body,childRequest=wire.find(w=>w.child).body;
  assert.deepEqual(childRequest.tools,parentRequest.tools,'same initial native tool schemas');
  const childSystem=childRequest.messages[0].content;
  const boundary=childSystem.split('\n\n').find(s=>s.startsWith('你在独立隔离子任务中工作，'));
  assert.ok(boundary);
  assert.equal(childSystem.replace('\n\n'+boundary,''),parentRequest.messages[0].content,'only the explicit delegated authority differs from common L0 system content');
  assert.ok(events(childId).filter(e=>e.act==='context_request').length>8,'child must pass the old 8-turn limit');
  assert.ok(check.sourceRevision||check.target||check.process);
  const tasks=await get(route+'/subtasks/overview');assert.equal(tasks.tasks[0].state,'completed');
  const pe=events(thread.id);
  assert.equal(pe.filter(e=>e.act==='subtasks_joined').length,1);
  assert.ok(!pe.some(e=>e.act==='say'&&e.roleSummary&&e.text.includes('PARENT_DRAFT')));
  assert.equal(pe.filter(e=>e.act==='say'&&e.roleSummary&&e.text.includes('PARENT_FINISHED')).length,1);
  passed.push('ordinary delegation: parent reads its own project before the child finishes; one joined result reaches the model before one final reply');
  passed.push('native child: same ExecutorCore, Skill/search/tool groups, >8 turns, 131072 and unlimited configured calls; independent history and actual isolated check');
  passed.push('native child: recursive dispatch and parent resource rejected; user source unchanged; parent collected result and ended normally');
  // A normal parent continuation must retain its own history without rerunning the child.
  const beforeCount=wire.filter(w=>w.child).length;parentStep=50;
  setFixture((body,res)=>{wire.push({child:false,mode:'parent-resume',body:structuredClone(body)});assert.ok(JSON.stringify(body.messages).includes('PARENT_FINISHED'));say(res,'PARENT_RESUMED: 原成果仍可读，本次没有重派子任务。');return true;});
  await post(route+'/send',{text:'继续原任务，只回报已保留的成果，不重派或合并。'});await finish(route);
  assert.equal(wire.filter(w=>w.child).length,beforeCount);assert.equal((await get(route+'/progress')).runtime.state,'completed');
  assert.equal(fs.readFileSync(path.join(root,'sample.py'),'utf8'),source);assert.equal(win.isVisible(),false);
  passed.push('native child: default parent continuation retains history and does not redispatch completed work; hidden Electron throughout');
  const recovery=await post('/api/v2/conversations/threads',{title:'02 · 普通子任务强退恢复与取消'});
  const recoveryRoute='/api/v2/conversations/threads/'+recovery.id;
  const recoveryConfig=await get(recoveryRoute+'/execution');
  await post(recoveryRoute+'/execution',{expectedRevision:recoveryConfig.next.revision,crew:'l0',charter:'off',maxTurns:0,modelCalls:12,contextLimit:131072});
  const dir=path.join(data,'runs','workbench','subtasks',recovery.id);
  const read=p=>JSON.parse(fs.readFileSync(p,'utf8'));
  const task=()=>{
   const files=fs.readdirSync(dir).filter(f=>f.endsWith('.json'));
   assert.equal(files.length,1);return read(path.join(dir,files[0]));
  };
  let step=0,childRequests=0,holdingParent,holdingChild,timer;
  let bothInFlight;
  const inFlight=new Promise(resolve=>{bothInFlight=resolve;});
  setFixture((body,res,tool)=>{
   wire.push({mode:'native-host-crash',body:structuredClone(body)});
   if(body.messages.find(m=>m.role==='user')?.content.includes('CHILD_RECOVERY_GOAL')){
    childRequests++;holdingChild=res;
   }else if(step++===0)tool(res,'use_tool_group',{group:'subtasks'},'recovery-group');
   else if(step===2)tool(res,'create_subtask',{request_key:'lost-native',goal:'CHILD_RECOVERY_GOAL inspect the delegated request only',resource_ids:[],kind:'report',model_calls:2},'recovery-dispatch');
   else holdingParent=res;
   if(holdingParent&&holdingChild)bothInFlight();return true;
  });
  try{
   await post(recoveryRoute+'/send',{text:'Run one independent report and continue the parent task.'});
   await Promise.race([inFlight,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('parent and child did not overlap before restart')),60000);})]);
   const before=task();assert.ok(before.childRunId);assert.equal(before.state,'running');
   const allocation=()=>read(before.rootBudget).allocations[before.id];
   assert.equal(allocation().terminal,false);
   await restartBackend();
   holdingParent.destroy();holdingChild.destroy();
   // Inspect the persisted row before any subtask overview or cancellation request.
   const deadline=Date.now()+10000;
   while(task().state==='running'&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,50));
   assert.equal(task().state,'unknown');assert.equal(task().childRunId,before.childRunId);
   assert.equal(allocation().terminal,false);
   await finish(recoveryRoute);await finish('/api/v2/conversations/threads/'+before.childId);
   const cancelRoute=recoveryRoute+'/subtasks/'+before.id+'/cancel';
   await post(cancelRoute,{});assert.equal(task().state,'cancelled');assert.equal(allocation().terminal,true);
   const released=allocation();await post(cancelRoute,{});assert.deepEqual(allocation(),released);
   assert.equal(childRequests,1);assert.equal(step,3);assert.equal(win.isVisible(),false);
   passed.push('native host force-kill: startup reconciles without opening an overview, preserves an uncertain reservation, and explicit cancellation after verified exit releases it once without replay');
   fs.writeFileSync(path.join(out,'native-child-cases.json'),JSON.stringify({parent:thread.id,child:childId,project:project.id,root,recoveryParent:recovery.id,recoveryChild:before.childId},null,2));
  }finally{clearTimeout(timer);holdingParent?.destroy();holdingChild?.destroy();}
 }finally{setFixture(null);fs.writeFileSync(path.join(out,'native-child-wire.json'),JSON.stringify({wire,faults},null,2));}
};
