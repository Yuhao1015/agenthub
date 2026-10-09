const assert=require('node:assert/strict');
const {dashboard}=require('./dashboard_browser_fixture.cjs');
const id='aaaabbbbcccc';
const timestamp=Math.floor(Date.now()/1000);
const question={id:'q1',question:'需要哪些功能？',options:[{label:'搜索'},{label:'导出'},{label:'其他',other:true}]};
async function capture(page,name){if(!process.env.STUDIO_TEST_OUTPUT)return;const fs=require('node:fs'),path=require('node:path');fs.mkdirSync(process.env.STUDIO_TEST_OUTPUT,{recursive:true});await page.screenshot({path:path.join(process.env.STUDIO_TEST_OUTPUT,name+'.png')});}
async function fixture(history,options={}){
 let posted=[],requests=0;
 const record={id,message:'界面调整',state:'awaiting_input',pending:false,history,created_at:timestamp-6,updated_at:timestamp-6,context:{workdir:'C:/work',prefs:{}},...options};
 const app=await dashboard(async(route,url,req,json)=>{
  if(url.pathname==='/goals'){requests++;await json(route,{revision:requests,items:[record]});return true}
  if(url.pathname==='/goals/'+id+'/message'){
   posted.push(req.postDataJSON());record.history=record.history.map(m=>m.questions?{...m,answered:true,answers:posted.at(-1).choice_answers}:m);
   record.history.push({role:'user',text:posted.at(-1).message,ts:100});record.state='processing';record.pending=true;
   await json(route,{goal:record});return true;
  }
  return false;
 });
 try{await app.page.evaluate(id=>GoalQueue.select(id),id)}catch(error){await app.close();throw error}
 return {...app,record,posted};
}
async function run(name,test){const app=await fixture(test.history||[{role:'user',text:'原始提示词 <img src=x onerror=alert(1)>',ts:1},{role:'manager',text:'',ts:2,questions:[question]}],test.options);try{await test(app);assert.deepEqual(app.errors,[]);assert.deepEqual(app.unexpected,[]);console.log(name+': PASS')}finally{await app.close()}}
const cases={
 scroll:Object.assign(async({page:p,record})=>{
  const top=await p.evaluate(()=>{const sc=document.querySelector('#chat-scroll');sc.scrollTop=180;return sc.scrollTop});
  assert.ok(top>0);await p.evaluate(()=>GoalQueue.refresh());
  assert.ok(Math.abs(await p.locator('#chat-scroll').evaluate(sc=>sc.scrollTop)-top)<2,'Polling must preserve history reading position');
  record.history.push({role:'manager',text:'新增回复',ts:99});await p.evaluate(()=>GoalQueue.refresh());
  assert.ok(Math.abs(await p.locator('#chat-scroll').evaluate(sc=>sc.scrollTop)-top)<2,'A new message must not pull the reader to the bottom');
  const nearBottom=await p.locator('#chat-scroll').evaluate(sc=>{sc.scrollTop=sc.scrollHeight-sc.clientHeight-12;return sc.scrollTop});
  await p.evaluate(()=>GoalQueue.refresh());assert.ok(Math.abs(await p.locator('#chat-scroll').evaluate(sc=>sc.scrollTop)-nearBottom)<2,'Even a small upward scroll should pause following');
  await p.locator('#chat-scroll').evaluate(sc=>sc.scrollTop=sc.scrollHeight);record.history.push({role:'manager',text:'底部跟随',ts:100});
  await p.evaluate(()=>GoalQueue.refresh());assert.ok(await p.locator('#chat-scroll').evaluate(sc=>sc.scrollHeight-sc.clientHeight-sc.scrollTop)<2);
 },{history:Array.from({length:35},(_,i)=>({role:i%2?'manager':'user',text:'聊天记录 '+i+'\n详细信息\n更多信息',ts:i+1}))}),
 focus:async({page:p})=>{
  await p.getByText('其他',{exact:true}).click();const input=p.locator('.other-in');await input.fill('中文补充');
  await input.evaluate(el=>{window.originalChoice=el;el.dispatchEvent(new CompositionEvent('compositionstart',{data:'中'}))});
  await p.evaluate(()=>GoalQueue.refresh());
  assert.equal(await p.evaluate(()=>document.activeElement===window.originalChoice&&window.originalChoice.isConnected),true,'Polling must retain the actual focused input node, including IME composition');
  await p.keyboard.type('继续');assert.equal(await input.inputValue(),'中文补充继续');
  await capture(p,'choices-editor');
 },
 multi:async({page:p,posted,record})=>{
  await p.getByText('搜索',{exact:true}).click();await p.getByText('导出',{exact:true}).click();
  assert.equal(await p.getByRole('checkbox',{name:'搜索',exact:true}).getAttribute('aria-checked'),'true');
  assert.equal(await p.getByRole('checkbox',{name:'导出',exact:true}).getAttribute('aria-checked'),'true');
  await p.getByText('其他',{exact:true}).click();await p.locator('.other-in').fill('本地预览');await p.evaluate(()=>GoalQueue.refresh());
  await capture(p,'multi-choice-desktop');await p.setViewportSize({width:390,height:844});await capture(p,'multi-choice-mobile');
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await p.setViewportSize({width:1536,height:960});
  await p.getByRole('button',{name:'提交回答',exact:true}).click();await p.waitForFunction(()=>GoalQueue.selected().state==='processing');
  assert.equal(posted[0].message,'需要哪些功能？：搜索；导出；本地预览');
  assert.deepEqual(JSON.parse(posted[0].choice_answers.q1),['搜索','导出','其他']);assert.equal(posted[0].choice_answers.__other__q1,'本地预览');
  record.state='awaiting_input';record.pending=false;record.history[1].answers={q1:'导出'};record.history[1].answered=true;await p.evaluate(()=>GoalQueue.refresh());
  assert.equal(await p.getByRole('checkbox',{name:'导出',exact:true}).getAttribute('aria-checked'),'true');
  assert.equal(await p.getByRole('checkbox',{name:'搜索',exact:true}).getAttribute('aria-checked'),'false');
  assert.equal(await p.getByRole('checkbox',{name:'导出',exact:true}).isDisabled(),true);
 },
 actions:async({page:p,posted})=>{
  await p.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>window.copiedMessage=text}}));
  const text='原始提示词 <img src=x onerror=alert(1)>';
  const message=p.locator('.msg.user').first();assert.equal(await message.locator('img').count(),0);
  await message.getByRole('button',{name:'复制消息',exact:true}).click();assert.equal(await p.evaluate(()=>window.copiedMessage),text);
  await message.getByRole('button',{name:'重新发送',exact:true}).click();assert.equal(await p.locator('#prompt').inputValue(),text);assert.equal(posted.length,0);
  await p.locator('#prompt').fill('临时草稿');await message.getByRole('button',{name:'修改提示词',exact:true}).click();assert.equal(await p.locator('#prompt').inputValue(),text);assert.equal(posted.length,0);
  assert.equal(await p.locator('#prompt').evaluate(el=>el===document.activeElement),true);
  await p.evaluate(()=>GoalQueue.refresh());assert.equal(await p.locator('#prompt').inputValue(),text);
  await p.setViewportSize({width:390,height:844});assert.equal(await message.getByRole('button',{name:'复制消息',exact:true}).isVisible(),true);
  await capture(p,'message-actions-mobile');
 },
 manager:Object.assign(async({page:p,record})=>{
  await p.emulateMedia({reducedMotion:'no-preference'});
  await p.getByText('经理思考中',{exact:true}).waitFor();await p.getByText('Read(src/main.py)',{exact:true}).waitFor();
  assert.equal(await p.locator('.work-shimmer').first().evaluate(el=>getComputedStyle(el).animationName),'ah-text-flow');
  await p.locator('.work-shimmer').first().evaluate(el=>window.progressHeader=el);
  record.progress.push({detail:'Grep(入口)',ts:timestamp-2});await p.evaluate(()=>GoalQueue.refresh());await p.getByText('Grep(入口)',{exact:true}).waitFor();
  assert.equal(await p.locator('.work-shimmer').first().evaluate(el=>el===window.progressHeader),true,'Progress refresh should not restart the gradient animation');
  await capture(p,'manager-progress');
  await p.emulateMedia({reducedMotion:'reduce'});assert.equal(await p.locator('.work-shimmer').first().evaluate(el=>getComputedStyle(el).animationName),'none');
  record.pending=false;record.state='awaiting_input';await p.evaluate(()=>GoalQueue.refresh());assert.equal(await p.getByText('经理思考中',{exact:true}).count(),0);
 },{options:{state:'processing',pending:true,progress:[{detail:'Read(src/main.py)',ts:timestamp-5}]}}),
 card_states:async({page:p})=>{
  await p.evaluate(()=>{
   GoalQueue.legacy();S.preChat=[];S.chatSession=null;S.busy=true;S.mode='live';
   S.sessions=[{id:'run',runId:'run',status:'running',chat:[{role:'manager',text:'交付',ts:1,card:{kind:'done',ok:true,runId:'run'}},{role:'manager',text:'失败',ts:2,card:{kind:'fail',agent:'fixture-agent',task:'t1',error:'测试失败',final:true}}],events:[]}];S.activeIdx=0;renderAll();
  });
  await p.getByText('正在统计这次运行改了哪些文件…',{exact:true}).waitFor();assert.equal(await p.getByRole('button',{name:'重跑失败任务',exact:true}).count(),0);
  await p.evaluate(()=>{S.changes.run={changed:1,counts:{modified:1},checkpoints:{on:true}};S.changesVer++;S.busy=false;S.sessions[0].status='failed';renderAll()});
  await p.getByRole('button',{name:'审阅改动',exact:true}).waitFor();await p.getByRole('button',{name:'重跑失败任务',exact:true}).waitFor();
 },
 worker_identity:async({page:p})=>{
  await p.evaluate(()=>{
   GoalQueue.legacy();S.mode='demo';S.preChat=[];S.sessions=[{id:'parallel',status:'running',chat:[],events:[{type:'plan_created',tasks:[{id:'t1',title:'任务一'},{id:'t2',title:'任务二'}]},{type:'task_started',task_id:'t1',agent:'same-agent'},{type:'task_started',task_id:'t2',agent:'same-agent'},{type:'agent_thinking',task_id:'t1',agent:'same-agent',detail:'Read(one.py)'},{type:'agent_thinking',task_id:'t2',agent:'same-agent',detail:'Read(two.py)'},{type:'agent_thinking',agent:'same-agent',detail:'无法确认属于哪个任务'}]}];S.activeIdx=0;renderAll();
  });
  assert.deepEqual(await p.locator('.worker-progress section[data-task-id="t1"] .work-detail').allTextContents(),['Read(one.py)']);
  assert.deepEqual(await p.locator('.worker-progress section[data-task-id="t2"] .work-detail').allTextContents(),['Read(two.py)']);
  // Old demo/events without a task ID remain readable when there is only one candidate.
  await p.evaluate(()=>{S.sessions[0].events.push({type:'task_completed',task_id:'t1',agent:'same-agent'},{type:'agent_thinking',agent:'same-agent',detail:'明确的旧格式事件'});renderAll()});
  await p.locator('.worker-progress section[data-task-id="t2"]').getByText('明确的旧格式事件',{exact:true}).waitFor();
 },
 worker_terminal:async({page:p})=>{
  for(const status of ['cancelled','failed','done']){
   await p.evaluate(status=>{GoalQueue.legacy();S.mode='demo';S.preChat=[];S.sessions=[{id:'terminal',status,chat:[],events:[{type:'plan_created',tasks:[{id:'t1',title:'已终止任务'}]},{type:'task_started',task_id:'t1',agent:'fixture-agent'},{type:status==='cancelled'?'run_cancelled':status==='failed'?'run_crashed':'run_finished'}]}];S.activeIdx=0;renderAll()},status);
   assert.equal(await p.getByText('员工工作中',{exact:true}).count(),0,'Terminal '+status+' must not display active workers');
  }
 },
 employee:async({page:p})=>{
  await p.evaluate(()=>{GoalQueue.legacy();S.mode='demo';S.preChat=[];S.sessions=[{id:'run',runId:'run',status:'running',chat:[],events:[{type:'plan_created',tasks:[{id:'t1',title:'检查入口',type:'coding'}]},{type:'task_started',task_id:'t1',agent:'fixture-agent',ts:1},{type:'agent_thinking',task_id:'t1',agent:'fixture-agent',detail:'Read(src/main.py)',ts:2}]}];S.activeIdx=0;renderAll()});
  await p.getByText('员工工作中',{exact:true}).waitFor();await p.locator('.worker-progress').getByText('Read(src/main.py)',{exact:true}).waitFor();
  await capture(p,'employee-progress');
  await p.evaluate(()=>{S.sessions[0].events.push({type:'task_completed',task_id:'t1',agent:'fixture-agent',ts:3});renderAll()});assert.equal(await p.getByText('员工工作中',{exact:true}).count(),0);
 }
};
(async()=>{const selected=process.argv[2];for(const [name,test] of Object.entries(cases))if(!selected||selected===name)await run(name,test)})().catch(e=>{console.error(e);process.exitCode=1});
