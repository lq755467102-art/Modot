const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {setTimeout: sleep} = require('node:timers/promises');
const read = p => JSON.parse(fs.readFileSync(p,'utf8'));

module.exports = async ({js,request,out,passed,setFixture,restartBackend}) => {
  const dir=path.join(__dirname,'..','examples','worker-scout');
  const sample=read(path.join(dir,'example.json')), definition=read(path.join(dir,'plugin.json')).workers[0];
  const get=async p=>(await js('window.workbench.request({path:'+JSON.stringify(p)+'})')).body;
  async function post(p,b){const r=await request(p,b);assert.equal(r.status,200,JSON.stringify(r));return r.body;}
  const preview=await post('/api/v2/plugins/preview',{path:dir});
  let plugin=await post('/api/v2/plugins/install',{path:dir,fingerprint:preview.fingerprint});
  const enable=async enabled=>{plugin=await post('/api/v2/plugins/'+plugin.id,{expectedRevision:plugin.revision,workersEnabled:enabled,confirm:true});};
  await enable(true);
  const evidence=[], faults=[];
  async function until(f,label){const end=Date.now()+40000;while(Date.now()<end){if(faults.length)throw Error(faults[0]);if(await f())return;await sleep(60);}throw Error(label);}
  try {
    for(const scenario of ['manual','amend','new','wake','stop','revoke','restart','crash','child-first']){
      const parent=(await post('/api/v2/conversations/threads',{title:'任务核心 P3 · '+scenario})).id;
      const route='/api/v2/conversations/threads/'+parent;
      const current=(await get(route+'/execution')).next;
      const mode=['wake','stop','revoke','child-first'].includes(scenario)?'wake':'manual';
      await post(route+'/execution',{expectedRevision:current.revision,workerContinuation:mode,contextLimit:131072,crew:'l0',modelCalls:0,maxTurns:0});
      const caseDir=path.join(out,'data','runs','workbench','subtasks',parent);
      const rows=()=>fs.existsSync(caseDir)?fs.readdirSync(caseDir).filter(f=>f.endsWith('.json')).map(f=>read(path.join(caseDir,f))):[];
      let parentCalls=0,childCalls=0,release=scenario==='child-first',holdWake=false,receipt,secondBody,continued=false,heldChild;
      function end(res,s){res.writeHead(200,{'Content-Type':'text/event-stream'});res.end('data: '+JSON.stringify({choices:[{index:0,delta:{content:s},finish_reason:'stop'}]})+'\n\ndata: [DONE]\n\n');}
      setFixture((body,res,tool)=>{
        const child=body.messages[0].content.includes(definition.method);
        const n=child?++childCalls:++parentCalls;
        const invoke=(name,args={})=>tool(res,name,args,scenario+'-'+(child?'c':'p')+n);
        (async()=>{
          if(child){
            if(n===1){await until(()=>release,'held child not released');if(!res.destroyed)invoke('submit_worker_result',{submission_key:'result',data:sample.output});}
            else if(scenario==='crash')heldChild=res;
            else end(res,'已提交调查。');
          }else if(n===1){invoke('use_tool_group',{group:'plugins'});}
          else if(n===2){invoke('inspect_worker_plugins');}
          else if(n===3){const r=JSON.parse(body.messages.findLast(m=>m.role==='tool').content);invoke('delegate_plugin_worker',{worker:r.workers[0].worker,kind:'report',request_key:'one',resource_ids:[],input:sample.input});}
          else if(n===4){
            receipt=JSON.parse(body.messages.findLast(m=>m.role==='tool').content);assert.equal(receipt.lifetime,mode,JSON.stringify(receipt));
            await until(()=>childCalls>=1,'child did not launch');
            if(scenario==='child-first'){
              await until(()=>rows()[0].state==='completed','child must exit first');invoke('inspect_worker_plugins');
            }else end(res,'本轮独立工作已完成。');
          }
          else {
            secondBody=body;continued=true;
            if(scenario==='wake')await until(()=>holdWake,'wake race was not released');
            end(res,'已审阅当前信息。');
          }
        })().catch(e=>{faults.push(e.stack);if(!res.headersSent&&!res.destroyed)end(res,'fixture failed');});
        return true;
      });
      await post(route+'/send',{text:'整理已委派的调查资料。'});
      const idle=async()=>{const rt=(await get(route+'/progress')).runtime;return !rt.active&&rt.state==='completed';};
      await until(idle,'origin parent did not complete');
      assert.equal(rows()[0].state,scenario==='child-first'?'completed':'running');assert.equal(rows()[0].worker.releasedBy,rows()[0].parentRunId);
      if(scenario==='stop')await post(route+'/stop',{});
      if(scenario==='revoke')await enable(false);
      // A manual continuation can start even before the previous worker exits.
      if(scenario==='new')await post(route+'/send',{text:'新任务：单独记录新的目录说明。'});
      release=true;
      if(scenario==='crash'){
        await until(()=>heldChild&&rows()[0].worker.results.length===1,'durable result before host crash');
        const id=rows()[0].worker.results[0].result_id;
        await restartBackend();heldChild.destroy();
        await until(async()=>!(await get('/api/v2/conversations/threads/'+rows()[0].childId+'/progress')).runtime.active,'crashed child is still alive');
        await get(route+'/subtasks');
        assert.notEqual(rows()[0].state,'completed');
        assert.equal(rows()[0].worker.results[0].result_id,id);
      }else await until(()=>['completed','cancelled'].includes(rows()[0].state),'worker did not settle: '+scenario);
      if(['stop','revoke'].includes(scenario)){
        assert.equal(rows()[0].state,'cancelled');assert.equal(parentCalls,4);
        if(scenario==='revoke')await enable(true);
      } else {
        assert.equal(rows()[0].worker.results.length,1);
        if(scenario==='restart'){await restartBackend();assert.equal(rows()[0].worker.results.length,1);}
        if(scenario==='wake'){
          await until(()=>continued,'completed result did not wake parent');
          const joined=await post(route+'/send',{text:'继续'});assert.ok(joined.alreadyRunning,JSON.stringify(joined));holdWake=true;
        } else if(!['new','child-first'].includes(scenario))await post(route+'/send',{text:scenario==='amend'?'补充：只调查第二套配置。':'继续'});
        await until(()=>continued,'no continuation');await until(idle,'continuation did not finish');
        const note=secondBody.messages.find(m=>m.role==='user'&&m.content?.startsWith?.('[worker-result:'));
        if(scenario==='new'){assert.equal(note,undefined);assert.ok(!rows()[0].worker.results[0].delivery.notifiedIn);}
        else{
          assert.ok(note,scenario);const notice=JSON.parse(note.content.slice(note.content.indexOf('{')));
          assert.equal(notice.result_id,rows()[0].worker.results[0].result_id);
          assert.equal(notice.current_demand_revision,notice.demand_revision+(scenario==='amend'?1:0));
          assert.ok(rows()[0].worker.results[0].delivery.notifiedIn);assert.equal(rows()[0].worker.results[0].disposition,null);
          assert.equal((await get(route+'/subtasks/overview')).tasks[0].resultDelivery,'notified');
        }
        assert.equal(rows().length,1);assert.equal(parentCalls,5);
        if(scenario==='child-first'||scenario==='crash')assert.equal(rows()[0].worker.wakeClaim,undefined);
      }
      evidence.push({scenario,parent,child:rows()[0].childId,source:rows()[0].parentRunId,childState:rows()[0].state,stopCode:rows()[0].stopCode,parentCalls,childCalls,wake:rows()[0].worker.wakeClaim,delivery:rows()[0].worker.results[0]?.delivery});
      fs.writeFileSync(path.join(out,'worker-lifetime.json'),JSON.stringify(evidence,null,2));passed.push('P3 '+scenario);
    }
    assert.deepEqual(faults,[]);
  }finally{setFixture(null);}
};
