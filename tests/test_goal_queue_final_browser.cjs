const assert=require('node:assert/strict');
const {dashboard}=require('./dashboard_browser_fixture.cjs');
const goal=(id,state,run_id='')=>({id,message:'目标 '+id[0],state,run_id,pending:false,history:[{role:'user',text:'原目标',ts:1}],context:{workdir:'C:/A',prefs:{}},response:{},position:1});
async function contextBeforeAwait(){
 const posts=[];const app=await dashboard(async(route,url,req,json)=>{
  if(url.pathname==='/goals'&&req.method()==='POST'){const b=req.postDataJSON();posts.push(b);await json(route,{goal:{...goal(b.request_id,'waiting'),message:b.message}},202);return true}return false;
 });
 try{
  const p=app.page;await p.waitForFunction(()=>GoalQueue.available);
  await p.evaluate(()=>{
   window.waitForPrefs=()=>new Promise(r=>window.releasePrefs=r);
   window.currentPrefs=()=>({...window.testPrefs});
   window.testPrefs={manager_agent:'manager-A',manager_model:'model-A',manager_effort:'high',manager_model_mode:'explicit',manager_effort_mode:'explicit',gate_mode:'ask',assign_mode:'manual'};
   S.workdir='C:/A';GoalQueue.newDraft();GoalQueue.submit('A');
  });
  await p.waitForFunction(()=>!!window.releasePrefs);
  await p.evaluate(()=>{GoalQueue.newDraft();$('#prompt').value='B text';S.workdir='C:/B';window.testPrefs={manager_agent:'manager-B',manager_model:'model-B',manager_effort:'low',gate_mode:'auto',assign_mode:'auto'};releasePrefs(true)});
  await p.waitForFunction(()=>[...GoalQueue.records.values()].some(r=>r.message==='A'));
  assert.equal(posts[0].workdir,'C:/A');assert.equal(posts[0].prefs.manager_agent,'manager-A');assert.equal(posts[0].prefs.manager_model,'model-A');assert.equal(posts[0].prefs.manager_effort,'high');assert.equal(posts[0].prefs.gate_mode,'ask');assert.equal(posts[0].prefs.assign_mode,'manual');
  assert.equal(await p.locator('#prompt').inputValue(),'B text');
 }finally{await app.close()}
}
async function restoredQueuedSession(){
 for(const status of ['asking','confirm']){
  const a=goal('a'.repeat(12),'awaiting_input');a.response={status,question:'继续确认',goal:'A',constraints:''};
  const posts=[];const app=await dashboard(async(route,url,req,json)=>{
   if(url.pathname==='/goals'&&req.method()==='GET'){await json(route,{revision:1,items:[a]});return true}
   if(url.pathname==='/chat/session/current'){await json(route,{active:{session_id:a.id,history:a.history,pending:false,awaiting_confirmation:status==='confirm',proposal:status==='confirm'?{goal:'A',constraints:''}:null,cost_usd:0},run:null});return true}
   if(url.pathname==='/goals/'+a.id+'/message'){posts.push(req.postDataJSON());await json(route,{goal:{...a,pending:true}},202);return true}
   return false;
  });
  try{
   const p=app.page;await p.waitForFunction(()=>GoalQueue.available);
   assert.equal(await p.evaluate(()=>GoalQueue.selected()?.id),a.id,'restored queued session must be selected immediately');
   await p.locator('#prompt').fill('继续');await p.locator('#send').click();await p.waitForFunction(()=>GoalQueue.selected().pending);
   assert.equal(posts.length,1);assert.deepEqual(app.unexpected,[]);
  }finally{await app.close()}
 }
}
async function historicalRunAndReply(){
 const a=goal('a'.repeat(12),'done','12345678'),b=goal('b'.repeat(12),'done','87654321');let denyHistory=false;
 const events=[{type:'plan_created',ts:2,goal:'旧目标',tasks:[{id:'t1',title:'旧目标的任务',status:'pending',depends_on:[]}]},{type:'task_completed',ts:3,task_id:'t1',agent:'fake',preview:'old output',cost_usd:0},{type:'run_finished',ts:4,statuses:{t1:'done'},total_cost_usd:0}];
 const app=await dashboard(async(route,url,req,json)=>{
  if(url.pathname==='/goals'&&req.method()==='GET'){await json(route,{revision:1,items:[a,b]});return true}
  if(url.pathname==='/goals/'+a.id+'/run'){await json(route,denyHistory?{error:'no archive'}:{run_id:a.run_id,events,status:'done'},denyHistory?404:200);return true}
  if(url.pathname==='/task/t1/message'){assert.equal(req.postDataJSON().run_id,a.run_id);await json(route,{reply:'paid answer',cost_usd:.01});return true}
  if(url.pathname==='/run/changes'){await json(route,{checkpoints:{on:false},rollbacks:[]});return true}
  return false;
 });
 try{
  const p=app.page;await p.waitForFunction(()=>GoalQueue.available);await p.evaluate(()=>{S.runId='87654321';S.busy=true});await p.locator('[data-goal-id="'+a.id+'"]').click();
  await p.waitForFunction(()=>!!GoalQueue.session());assert.equal(await p.locator('#studio-task-list').getByText('旧目标的任务',{exact:true}).count(),1,'historical task must hydrate');
  assert.equal(await p.evaluate(()=>S.runId),'87654321');assert.equal(await p.evaluate(()=>S.busy),true,'historical hydration must not change live owner');
  await p.locator('#prompt').fill('@t1 question');await p.locator('#send').click();await p.locator('#chat').getByText('paid answer',{exact:true}).waitFor();
  assert.equal(await p.locator('#prompt').inputValue(),'');await p.evaluate(()=>GoalQueue.refresh());await p.locator('#chat').getByText('paid answer',{exact:true}).waitFor();
  denyHistory=true;await p.evaluate(()=>{S.sessions=[];GoalQueue.select('a'.repeat(12))});
  await p.locator('#prompt').fill('@t1 second');await p.locator('#send').click();await p.waitForFunction(()=>!GoalQueue.requestPending());
  assert.equal(await p.locator('#chat').getByText('paid answer',{exact:true}).count(),2,'answer must survive a missing run cache');
  assert.deepEqual(app.errors,[]);assert.deepEqual(app.unexpected,[]);
 }finally{await app.close()}
}
async function replyWithoutCache(){
 const a=goal('a'.repeat(12),'done','12345678');
 const app=await dashboard(async(route,url,req,json)=>{
  if(url.pathname==='/goals'){await json(route,{revision:1,items:[a]});return true}
  if(url.pathname.endsWith('/run')){await json(route,{error:'archive missing'},404);return true}
  if(url.pathname==='/task/t1/message'){await json(route,{reply:'answer without cache'});return true}
  return false;
 });
 try{const p=app.page;await p.waitForFunction(()=>GoalQueue.available);await p.evaluate(()=>GoalQueue.select('a'.repeat(12)));await p.locator('#prompt').fill('@t1 question');await p.locator('#send').click();await p.waitForFunction(()=>!GoalQueue.requestPending());assert.equal(await p.locator('#chat').getByText('answer without cache',{exact:true}).count(),1)}finally{await app.close()}
}
async function interruptedResolutionRequiresConfirmation(){
 const a=goal('a'.repeat(12),'interrupted','12345678');const posts=[];
 const app=await dashboard(async(route,url,req,json)=>{
  if(url.pathname==='/goals'){await json(route,{revision:1,items:[a]});return true}
  if(url.pathname.endsWith('/run')){await json(route,{error:'no run directory'},404);return true}
  if(url.pathname.endsWith('/resolve')){posts.push(req.postDataJSON());await json(route,{goal:{...a,state:'cancelled'}},202);return true}return false;
 });
 try{
  const p=app.page;await p.waitForFunction(()=>GoalQueue.available);await p.evaluate(()=>GoalQueue.select('a'.repeat(12)));
  p.once('dialog',async d=>{assert.match(d.message(),/停止/);await d.dismiss()});
  await p.getByRole('button',{name:'确认运行已停止并结束目标'}).click();assert.equal(posts.length,0);
  p.once('dialog',d=>d.accept());await p.getByRole('button',{name:'确认运行已停止并结束目标'}).click();
  await p.waitForFunction(()=>GoalQueue.selected().state==='cancelled');assert.deepEqual(posts,[{confirm_stopped:true}]);
 }finally{await app.close()}
}
(async()=>{const failures=[];for(const fn of [contextBeforeAwait,restoredQueuedSession,historicalRunAndReply,replyWithoutCache,interruptedResolutionRequiresConfirmation]){try{await fn();console.log(fn.name+': PASS')}catch(e){failures.push(fn.name);console.error(fn.name,e)}}assert.deepEqual(failures,[])})().catch(e=>{console.error(e);process.exitCode=1});
