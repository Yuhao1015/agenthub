const assert=require('node:assert/strict');
const {dashboard}=require('./dashboard_browser_fixture.cjs');
(async()=>{
 let revision=1,hold=null,release,fail=false,posts=[];
 const records=new Map();
 const a={id:'a'.repeat(12),message:'目标 A',state:'processing',context:{workdir:'C:/原目录',prefs:{}},history:[],pending:true,position:1,run_id:'',response:{},progress:[]};records.set(a.id,a);
 const app=await dashboard(async(route,url,req,json)=>{
  if(url.pathname==='/goals'&&req.method()==='GET'){await json(route,{revision,items:[...records.values()]});return true}
  if(url.pathname==='/goals'&&req.method()==='POST'){
   const b=req.postDataJSON();posts.push(b);if(hold)await hold;
   if(fail){await json(route,{error:'保存失败'},500);return true}
   const r={id:b.request_id,message:b.message,context:{workdir:b.workdir,prefs:b.prefs},state:'waiting',position:records.size+1,pending:false,history:[],response:{},progress:[],run_id:''};records.set(r.id,r);revision++;await json(route,{goal:r},202);return true
  }
  const match=url.pathname.match(/^\/goals\/([a-f0-9]{12})\/(message|cancel|retry)$/);
  if(match){const r=records.get(match[1]);posts.push({action:match[2],id:r.id,...req.postDataJSON()});if(match[2]==='cancel')r.state='cancelled';else if(match[2]==='retry')r.state='waiting';else r.pending=true;revision++;await json(route,{goal:r},202);return true}
  return false;
 });
 try{
  const p=app.page;await p.waitForFunction(()=>window.GoalQueue&&GoalQueue.available);
  await p.evaluate(()=>{S.busy=true;S.sendPending=true;renderAll()});
  assert.equal(await p.locator('#new-btn').isDisabled(),false,'New goal stays available during A');
  await p.locator('#new-btn').click();await p.locator('#prompt').fill('目标 B');
  await p.locator('#send').click();await p.getByText('排队中',{exact:true}).first().waitFor();
  assert.equal(posts.length,1);assert.equal(posts[0].message,'目标 B');
  const b=[...records.values()].find(r=>r.message==='目标 B');
  await p.locator('#new-btn').click();await p.locator('#prompt').fill('目标 C 的草稿');
  a.state='awaiting_input';a.pending=false;a.history=[{role:'user',text:'目标 A',ts:1},{role:'manager',text:'需要哪些内容？',ts:2}];revision++;
  await p.evaluate(()=>GoalQueue.refresh());assert.equal(await p.locator('#prompt').inputValue(),'目标 C 的草稿');
  assert.equal(await p.locator('#chat').getByText('需要哪些内容？').count(),0);
  a.state='running';a.run_id='12345678';revision++;
  await p.evaluate(()=>GoalQueue.refresh());
  await p.evaluate(()=>{S.runId='12345678';ingest({type:'plan_created',ts:Date.now()/1000,goal:'目标 A',tasks:[{id:'t1',title:'A 的任务',status:'pending',depends_on:[]}]})});
  assert.equal(await p.locator('#studio-task-list button').count(),0,'A canvas must not appear in C');
  assert.equal(await p.locator('#prompt').inputValue(),'目标 C 的草稿');
  a.state='awaiting_input';a.run_id='';revision++;await p.evaluate(()=>GoalQueue.refresh());
  await p.locator('[data-goal-id="'+a.id+'"]').click();await p.locator('#chat').getByText('需要哪些内容？').waitFor();
  await p.locator('#prompt').fill('一个页面');await p.locator('#send').click();await p.waitForFunction(()=>GoalQueue.selected().pending);
  await p.locator('[data-goal-id="'+b.id+'"]').click();await p.getByRole('button',{name:'取消等待',exact:true}).click();assert.equal(records.get(b.id).state,'cancelled');
  await p.locator('#new-btn').click();await p.locator('#prompt').fill('@t1 这是新目标');
  hold=new Promise(r=>release=r);await p.locator('#send').click();await p.waitForFunction(()=>GoalQueue.draftPending());
  await p.locator('#new-btn').click();await p.locator('#prompt').fill('迟到时保留这句话');release();hold=null;
  await p.waitForFunction(()=>[...GoalQueue.records.values()].some(r=>r.message==='@t1 这是新目标'));
  assert.equal(await p.locator('#prompt').inputValue(),'迟到时保留这句话');
  fail=true;await p.locator('#send').click();await p.waitForFunction(()=>!GoalQueue.draftPending());assert.equal(await p.locator('#prompt').inputValue(),'迟到时保留这句话');fail=false;
  await p.setViewportSize({width:390,height:844});await p.locator('#prompt').fill('窄屏目标');await p.locator('#prompt').press('Enter');await p.waitForFunction(()=>GoalQueue.selected()?.message==='窄屏目标');
  await p.reload();await p.waitForFunction(()=>window.GoalQueue?.records.size>=4);
  const retryTarget=[...records.values()].find(r=>r.message==='窄屏目标');retryTarget.state='blocked';revision++;
  await p.evaluate(()=>GoalQueue.refresh());await p.evaluate(id=>GoalQueue.select(id),retryTarget.id);
  let warning='';p.once('dialog',async dialog=>{warning=dialog.message();await dialog.dismiss()});
  await p.getByRole('button',{name:'使用当前设置重试',exact:true}).click();
  assert.match(warning,/token|额度/);assert.equal(posts.filter(x=>x.action==='retry').length,0);
  p.once('dialog',dialog=>dialog.accept());await p.getByRole('button',{name:'使用当前设置重试',exact:true}).click();
  await p.waitForFunction(()=>GoalQueue.selected().state==='waiting');assert.equal(posts.filter(x=>x.action==='retry').length,1);
  assert.equal(posts.filter(x=>x.message==='目标 B').length,1);
  assert.deepEqual(app.errors,[]);assert.deepEqual(app.unexpected,[]);
  console.log('queued goals browser: PASS');
 }finally{await app.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
