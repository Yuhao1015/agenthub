/* Real dashboard behavior with all model-facing endpoints intercepted.
   Run with Playwright available on NODE_PATH: node tests/test_studio_browser.cjs */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const shots = process.env.STUDIO_TEST_OUTPUT || path.join(root, 'docs', 'ui-preview');
const agents = [
 {name:'cc-sonnet-mgr',kind:'claude_code',animal:'corgi',tier:'mid',installed:true,blurb:'与用户澄清目标。'},
 {name:'cc-opus',kind:'claude_code',animal:'owl',tier:'strong',installed:true,blurb:'架构与复杂规划。'},
 {name:'codex-local',kind:'codex',animal:'robot',tier:'mid',installed:true,blurb:'本地代码协作。'},
 {name:'cc-kimi',kind:'codebuddy',animal:'cat',installed:false,blurb:'尚未安装。'},
 {name:'or-test',kind:'openrouter',animal:'whale',installed:true,blurb:'按量 API。'},
 {name:'evil"><img src=x onerror=alert(1)>',kind:'mock',animal:'robot',installed:true,blurb:'<img src=x onerror=alert(1)>'}
];
for(const a of agents){a.group={claude_code:'Claude Code',codex:'Codex',codebuddy:'WorkBuddy',openrouter:'OpenRouter',mock:'本地模拟'}[a.kind];a.model='test-a';a.model_choices=['test-a','test-b','no-effort'];a.allowed_tools=a.name==='cc-sonnet-mgr'?'Read':'Read,Edit';a.manager_rank=a.name==='cc-sonnet-mgr'?0:99;a.route_types=a.name==='cc-sonnet-mgr'?['manager']:['planning','coding'];a.effort_supported=['claude_code','codex'].includes(a.kind);a.suggested_efforts=['low','high'];a.model_capabilities={'test-a':{effort_control:'supported',effort_levels:['low','high'],source:'configuration'},'test-b':{effort_control:'supported',effort_levels:['low','high'],source:'configuration'},'no-effort':{effort_control:'unsupported',effort_levels:[],source:'configuration'}};}
const state={prefs:{manager_agent:'cc-sonnet-mgr',manager_model:'',manager_effort:'',manager_model_mode:'inherit',manager_effort_mode:'inherit',assign_mode:'manual',gate_mode:'ask'},writes:[],failSave:false,holdSave:null,legacyAgents:false};
const changes={checkpoints:{on:true},segments:[{n:1,kind:'run',can_rollback:true,tasks:[],files:[{path:'brief.md',status:'M',added:1,deleted:1}]}],rollbacks:[]};
let undoAttempts=0;
let server,browser;
function json(route,obj,status=200){return route.fulfill({status,contentType:'application/json',body:JSON.stringify(obj)})}
(async()=>{
 server=http.createServer((req,res)=>{
  const name=new URL(req.url,'http://localhost').pathname;
  const target=path.resolve(root,'.'+(name==='/'?'/dashboard.html':name));
  if(!target.startsWith(root+path.sep)){res.writeHead(403);res.end();return;}
  fs.readFile(target,(err,data)=>{if(err){res.writeHead(404);res.end();return;}res.setHeader('Content-Type',target.endsWith('.js')?'text/javascript':target.endsWith('.css')?'text/css':'text/html');res.end(data)});
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const origin='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({headless:true,channel:'chrome'});
 const page=await browser.newPage({viewport:{width:1536,height:960}});
 await page.emulateMedia({reducedMotion:'reduce'});
 const errors=[],unexpected=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());
  if(url.origin!==origin){unexpected.push(req.url());return route.abort()}
  if(url.pathname==='/settings')return json(route,{ui_prefs:state.prefs,workdir:'',budget:{max_usd_per_run:2}});
  if(url.pathname==='/agents')return json(route,{agents:state.legacyAgents?agents.map(({name,kind,animal,installed,blurb})=>({name,kind,animal,installed,blurb})):agents,note:'仅检测 CLI 安装状态，未验证模型或账号可用性'});
  if(url.pathname==='/settings/agent-options'){const body=req.postDataJSON();state.writes.push(body);const agent=agents.find(a=>a.name===body.agent);if(body.options.model!==undefined){agent.model=body.options.model;if(!agent.model_choices.includes(agent.model))agent.model_choices.push(agent.model)}if(body.options.effort!==undefined)agent.effort=body.options.effort;return json(route,{ok:true})}
  if(url.pathname==='/settings/config'){
   const body=req.postDataJSON();state.writes.push(body);
   if(state.holdSave)await state.holdSave;
   if(state.failSave)return json(route,{error:'保存失败示例'},500);
   state.prefs={...body.ui_prefs};return json(route,{ok:true});
  }
  if(url.pathname==='/chat/session/current')return json(route,{active:null,run:null});
  if(url.pathname==='/events.jsonl')return route.fulfill({status:404,body:''});
  if(url.pathname==='/run/changes')return json(route,changes);
  if(url.pathname==='/run/changes/diff')return json(route,{ok:true,diff:'-before\n+after <img src=x onerror=alert(1)>'});
  if(url.pathname==='/run/rollback'){
   state.writes.push(req.postDataJSON());changes.rollbacks=[{pre:'a',post:'b',restored:['brief.md'],undone:false}];
   return json(route,{restored:['brief.md'],skipped:[]});
  }
  if(url.pathname==='/run/rollback/undo'){
   state.writes.push(req.postDataJSON());undoAttempts++;
   if(undoAttempts===1)return json(route,{restored:[],skipped:[{path:'brief.md',reason:'externally changed'}],undone:false,error:'保留进度，可重试'},500);
   changes.rollbacks[0].undone=true;return json(route,{restored:['brief.md'],skipped:[],undone:true});
  }
  if(url.pathname==='/gate'){state.writes.push(req.postDataJSON());return json(route,{ok:true})}
  if(url.pathname==='/task/t1/output')return json(route,{output:'完整示例产出'});
  if(['/chat','/run','/rerun','/cancel'].includes(url.pathname)){unexpected.push('Unexpected execution endpoint '+url.pathname);return json(route,{error:'Test refuses model calls'},409)}
  return route.continue();
 });
 await page.goto(origin);
 await page.waitForFunction(()=>!S.restoringSessions && S.agents.length>0);
 assert.equal(await page.locator('#studio-canvas').count(),1,'Approved Studio layout must be integrated');
 assert.equal(await page.getByRole('button',{name:'接入本机 Agent',exact:true}).count(),1);
 fs.mkdirSync(shots,{recursive:true});
 await page.screenshot({path:path.join(shots,'01-start.png')});
 await page.locator('#studio-manager-trigger').click();
 assert.equal(await page.locator('[data-manager="cc-kimi"]').isDisabled(),true);
 assert.equal(await page.locator('#studio-manager-options img').count(),0,'Agent content stays text');
 await page.screenshot({path:path.join(shots,'02-manager-picker.png')});
 await page.locator('[data-manager="codex-local"]').click();
 const dialog=page.locator('#studio-manager-dialog');
 agents.find(a=>a.name==='codex-local').runtime_model_catalog=[{id:'fixture/m1',name:'Actual directory'}];agents.find(a=>a.name==='codex-local').catalog_status='ready';agents.find(a=>a.name==='codex-local').runtime_model_capabilities={'fixture/m1':{effort_control:'supported',effort_levels:['high'],source:'runtime'}};
 await page.evaluate(()=>refreshAgentMetadata());
 await dialog.getByLabel('模型',{exact:true}).selectOption('catalog:fixture/m1');
 assert.equal(await dialog.getByLabel('模型',{exact:true}).inputValue(),'catalog:fixture/m1');
 assert.equal(await dialog.getByLabel('思考程度',{exact:true}).locator('option[value="e0"]').textContent(),'高 · high');
 const beforeTyping=state.writes.length;
 await dialog.locator('[data-option="model"]').selectOption('custom');
 await dialog.locator('[data-option="model-input"]').fill('custom/model');
 assert.equal(state.writes.length,beforeTyping,'Manager editing stays local');
 assert.match(await dialog.locator('[data-option="capability-note"]').textContent(),/未验证/);
 await dialog.getByRole('button',{name:'保存为此配置默认',exact:true}).click();
 await page.waitForFunction(()=>S.agents.find(a=>a.name==='codex-local').model==='custom/model');
 assert.equal(await page.evaluate(()=>currentPrefs().manager_agent),'cc-sonnet-mgr','Saving profile defaults does not select a new manager');
 await dialog.locator('[data-option="model"]').selectOption('inherit');
 assert.match(await dialog.locator('[data-option="model"] option:checked').textContent(),/custom\/model/,'Open manager draft refreshes saved profile defaults');
 assert.equal(await dialog.locator('[data-option="model-input"]').isVisible(),false,'Inherited default hides the custom editor');
 assert.match(await dialog.locator('[data-option="capability-note"]').textContent(),/未验证/,'Default capability now describes the saved custom model');
 await dialog.locator('[data-option="model"]').selectOption('m2');
 assert.equal(await dialog.locator('[data-option="effort"]').isDisabled(),true);
 await dialog.locator('[data-option="model"]').selectOption('m0');
 await dialog.locator('[data-option="effort"]').selectOption('e1');
 let releaseSave;state.holdSave=new Promise(resolve=>{releaseSave=resolve});
 await page.locator('#studio-manager-apply').click();
 await page.waitForFunction(()=>document.querySelector('#studio-manager-apply').disabled);
 for(const key of ['Tab','Shift+Tab','Tab']){await page.keyboard.press(key);assert.equal(await page.evaluate(()=>document.querySelector('#studio-manager-dialog').contains(document.activeElement)),true,'Saving modal keeps keyboard focus inside')}
 assert.equal(await page.locator('#app').evaluate(el=>el.inert),true);
 releaseSave();state.holdSave=null;
 await page.waitForFunction(()=>document.querySelector('#studio-manager-dialog').hidden);
 assert.equal(state.prefs.manager_agent,'codex-local');
 assert.equal(state.prefs.manager_model,'test-a');assert.equal(state.prefs.manager_effort,'high');
 const savedPrefs=await page.evaluate(()=>currentPrefs());
 assert.equal(state.prefs.assign_mode,'manual');
 assert.equal(state.prefs.gate_mode,'ask');
 assert.match(await page.locator('#studio-manager-name').textContent(),/Codex/);
 state.failSave=true;
 await page.locator('#studio-manager-trigger').click();
 await page.locator('[data-manager="cc-sonnet-mgr"]').click();
 await page.locator('#studio-manager-selection summary').click();
 await page.locator('#studio-manager-profile').selectOption('cc-opus');
 await dialog.locator('[data-option="model"]').selectOption('custom');
 await dialog.locator('[data-option="model-input"]').fill('failure/model');
 await dialog.locator('[data-option="effort"]').selectOption('custom');
 await dialog.locator('[data-option="effort-input"]').fill('custom-high');
 await page.locator('#studio-manager-apply').click();
 await page.waitForFunction(()=>document.querySelector('#studio-manager-error').textContent.length>0);
 assert.equal(await page.locator('#sw-manager').inputValue(),'codex-local','Failed save restores previous selection');
 assert.deepEqual(await page.evaluate(()=>currentPrefs()),savedPrefs,'Failure rolls back every preference');
 await page.keyboard.press('Escape');state.failSave=false;
 const cancelWrites=state.writes.length;
 await page.locator('#studio-manager-trigger').click();
 await dialog.locator('[data-option="model"]').selectOption('custom');
 await dialog.locator('[data-option="model-input"]').fill('cancelled/model');
 await page.keyboard.press('Escape');
 assert.equal(state.writes.length,cancelWrites);
 assert.deepEqual(await page.evaluate(()=>currentPrefs()),savedPrefs);
 await page.locator('#studio-manager-trigger').click();
 await dialog.locator('[data-option="model"]').selectOption('custom');
 await dialog.locator('[data-option="model-input"]').fill('invalid model');
 assert.equal(await page.locator('#studio-manager-apply').isDisabled(),true);
 await page.screenshot({path:path.join(shots,'model-effort-invalid.png')});
 await dialog.locator('[data-option="model-input"]').fill('valid/model');
 await page.keyboard.press('Escape');
 await page.locator('#studio-manager-trigger').click();
 await page.locator('[data-manager=""]').click();
 await page.locator('#studio-manager-apply').click();
 await page.waitForFunction(()=>document.querySelector('#studio-manager-dialog').hidden);
 assert.equal(state.prefs.manager_agent,'');assert.equal(state.prefs.manager_model_mode,'inherit');assert.equal(state.prefs.manager_effort,'');
 await page.locator('#studio-manager-trigger').click();
 await page.locator('[data-manager="codex-local"]').click();
 await dialog.locator('[data-option="model"]').selectOption('m0');
 await dialog.locator('[data-option="effort"]').selectOption('e1');
 await page.locator('#studio-manager-apply').click();
 await page.waitForFunction(()=>document.querySelector('#studio-manager-dialog').hidden);
 await page.locator('#studio-manager-trigger').click();
 await page.locator('[data-manager="or-test"]').click();
 assert.match(await page.locator('#studio-manager-policy').textContent(),/护栏/);
 await page.keyboard.press('Escape');
 await page.evaluate(()=>{S.sendPending=true;renderAll()});
 assert.equal(await page.locator('#studio-manager-trigger').isDisabled(),true);
 assert.equal(await page.locator('#studio-change-manager').isDisabled(),true);
 await page.evaluate(()=>{S.sendPending=false;renderAll();const ts=Date.now()/1000;
  ingest({type:'plan_created',ts,goal:'让想法，有人一起完成。',tasks:[{id:'t1',title:'梳理目标与现有体验',type:'planning',status:'pending',depends_on:[]},{id:'t2',title:'设计经理选择与工作台',type:'coding',status:'pending',depends_on:['t1']}]});
  ingest({type:'gate_opened',kind:'plan',gate_id:'plan',gate_token:'demo-token',available_agents:S.agents,ts});
 });
 assert.equal(await page.locator('#studio-needs-count').textContent(),'1');
 await page.locator('#studio-needs-trigger').click();
 assert.equal(await page.locator('#studio-needs-list button').count(),1);
 await page.locator('#studio-needs-list button').click();
 const planRows=page.locator('#chat .studio-plan-list .node');
 assert.equal(await planRows.count(),2,'Plan confirmation follows the prototype task list');
 const firstRow=await planRows.nth(0).boundingBox(),secondRow=await planRows.nth(1).boundingBox();
 assert(secondRow.y>=firstRow.y+firstRow.height,'Plan tasks are vertically ordered');
 assert.equal(await page.locator('#run-bar > #send').count(),1,'Send action sits beside the composer controls');
 assert.equal(await page.locator('#studio-search').getAttribute('href'),'settings.html#search-input');
 for(const [task,model,effort] of [['t1','m0','e0'],['t2','m1','e1']]){
  await page.locator('#chat .card .node[data-task-id="'+task+'"]').click();
  const picker=page.locator('[data-picker="'+task+'"]');
  await picker.locator('[data-option="agent"]').selectOption('claude_code');
  await picker.locator('[data-option="model"]').selectOption(model);
  await picker.locator('[data-option="effort"]').selectOption(effort);
 }
 await page.locator('#chat .node[data-task-id="t2"]').click();
 await page.setViewportSize({width:1280,height:800});
 await page.evaluate(()=>{document.querySelector('#chat-scroll').scrollTop=0});
 await page.screenshot({path:path.join(shots,'03-plan.png')});
 await page.getByRole('button',{name:'放行开工',exact:true}).click();
 const gateReply=state.writes.find(x=>x.gate_id==='plan'&&x.decision==='approve');
 assert.deepEqual(gateReply.execution_options,{t1:{model:'test-a',effort:'low'},t2:{model:'test-b',effort:'high'}});
 assert.deepEqual(gateReply.overrides,{t1:'cc-opus',t2:'cc-opus'});

 await page.evaluate(()=>{
  const ts=Date.now()/1000;
  ingest({type:'gate_resolved',gate_id:'plan',gate_token:'demo-token',decision:'approve',overrides:{t1:'cc-opus',t2:'cc-opus'},execution_options:{t1:{model:'test-a',effort:'low'},t2:{model:'test-b',effort:'high'}},ts});
  ingest({type:'task_started',task_id:'t1',agent:'cc-opus',animal:'owl',model:'test-a',effort:'low',ts});
  ingest({type:'task_completed',task_id:'t1',agent:'cc-opus',preview:'设计原则已完成',files:[{path:'brief.md',size:230}],cost_usd:0,ts});
  ingest({type:'task_started',task_id:'t2',agent:'cc-opus',animal:'owl',model:'test-b',effort:'high',ts});
 });
 assert.equal(await page.locator('#studio-task-list button').count(),2);
 assert.match(await page.locator('#studio-task-list').textContent(),/test-a.*low/s);
 assert.match(await page.locator('#studio-task-list').textContent(),/test-b.*high/s);
 await page.locator('#studio-task-list button').first().click();
 assert.equal(await page.locator('#drawer-wrap').isVisible(),true);
 await page.locator('#d-close').click();
 await page.locator('#studio-deliverables-tab').click();
 assert.equal(await page.locator('#studio-files .f-row').count(),1);
 await page.screenshot({path:path.join(shots,'04-deliverables.png')});
 await page.locator('#studio-files .f-row').click();
 assert.equal(await page.locator('#pv-wrap').isVisible(),true);
 await page.locator('#pv-close').click();
 assert.equal(await page.locator('#pv-wrap').isVisible(),false,'Closing preview stays closed after the merge');
 await page.locator('#studio-activity-tab').click();
 assert.match(await page.locator('#studio-activity').textContent(),/task_started/);
 await page.evaluate(()=>{S.preChat=[{role:'manager',text:'仍在等待回复',ts:Date.now()/1000}];S.sendPending=true;S.chatSession='draft';S.activeIdx=S.sessions.length;renderAll()});
 assert.equal(await page.locator('#send').isDisabled(),true);
 assert.match(await page.locator('#chat').textContent(),/仍在等待回复/);
 await page.evaluate(()=>{S.preChat=[];S.sendPending=false;S.chatSession=null;S.activeIdx=0;renderAll()});
 for(const viewport of [{width:1536,height:960},{width:1280,height:800},{width:1024,height:768},{width:768,height:1024},{width:390,height:844}]){
  await page.setViewportSize(viewport);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'No overflow at '+viewport.width);
 }
 await page.locator('#studio-canvas-toggle').click();
 await page.locator('#studio-team-tab').click();
 await page.locator('#studio-task-list button').first().click();
 assert.equal(await page.locator('#studio-canvas').isVisible(),false,'Mobile task drawer clears canvas overlay');
 await page.locator('#d-close').click();
 await page.locator('#studio-library').evaluate(el=>el.click());
 await page.locator('#studio-files .f-row').click();
 assert.equal(await page.locator('#studio-canvas').isVisible(),false,'Mobile file preview clears canvas overlay');
 await page.locator('#pv-close').click();
 await page.locator('#studio-canvas-toggle').click();
 await page.locator('#studio-change-manager').click();
 const box=await page.locator('#studio-manager-apply').boundingBox();
 assert(box && box.y+box.height<=844,'Mobile manager action remains visible');
 await page.screenshot({path:path.join(shots,'05-mobile-manager.png')});
 await page.keyboard.press('Escape');
 await page.locator('#studio-canvas-close').click();
 await page.screenshot({path:path.join(shots,'06-mobile.png')});
 await page.setViewportSize({width:1536,height:960});
 await page.locator('#theme-btn').click();
 await page.screenshot({path:path.join(shots,'07-dark.png')});
 // Studio navigation and the safety-net panel share one preview lifecycle.
 await page.evaluate(()=>{S.busy=false;renderAll()});
 await page.setViewportSize({width:390,height:844});
 await page.locator('#studio-canvas-toggle').click();
 await page.evaluate(()=>{location.hash='review=abcd1234'});
 await page.locator('#pv-body .rv-path').waitFor();
 assert.equal(await page.locator('#studio-canvas').isVisible(),false,'Review must clear the mobile canvas overlay');
 assert.equal(await page.locator('#pv-dl').isVisible(),false);
 await page.locator('#pv-body .rv-path').click();
 await page.waitForFunction(()=>document.querySelector('#pv-body pre')?.textContent.includes('+after'));
 assert.equal(await page.locator('#pv-body img').count(),0,'Diff text is never parsed as markup');
 await page.getByRole('button',{name:'返回清单',exact:true}).click();
 page.on('dialog',d=>d.accept());
 await page.getByRole('button',{name:'回滚所选文件',exact:true}).click();
 await page.getByRole('button',{name:'撤销最近一次回滚',exact:true}).waitFor();
 assert.deepEqual(state.writes.at(-1),{run:'abcd1234',paths:['brief.md']});
 assert.equal(await page.getByRole('button',{name:'回滚所选文件',exact:true}).isDisabled(),true);
 await page.getByRole('button',{name:'撤销最近一次回滚',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('#pv-body').textContent.includes('撤销未完成'));
 assert.match(await page.locator('#pv-body').textContent(),/保留进度，可重试/);
 await page.getByRole('button',{name:'撤销最近一次回滚',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('#pv-body').textContent.includes('已撤销回滚'));
 assert.equal(await page.getByRole('button',{name:'回滚所选文件',exact:true}).isEnabled(),true);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 await page.screenshot({path:path.join(shots,'09-integrated-review.png')});
 await page.locator('#pv-close').click();
 await page.locator('#studio-library').evaluate(el=>el.click());
 await page.locator('#studio-files .f-row').click();
 assert.equal(await page.locator('#pv-dl').isVisible(),true,'File download returns after review');
 await page.locator('#pv-close').click();
 await page.evaluate(()=>{openReview('abcd1234');ingest({type:'gate_opened',kind:'task',gate_id:'task:t2',task_id:'t2',gate_token:'new-token',ts:Date.now()/1000});});
 await page.locator('#studio-needs-trigger').evaluate(el=>el.click());
 await page.locator('#studio-needs-list button').first().click();
 assert.equal(await page.locator('#pv-wrap').isVisible(),false,'Needs navigation closes the previous review');
 await page.evaluate(()=>{location.hash=''});
 await page.setViewportSize({width:1536,height:960});
 state.legacyAgents=true;
 await page.reload();await page.waitForFunction(()=>!S.restoringSessions&&S.agents.length>0);
 await page.locator('#studio-manager-trigger').click();
 assert.match(await page.locator('#studio-manager-selection').textContent(),/尚未支持模型设置/);
 await page.locator('[data-manager="cc-sonnet-mgr"]').click();
 await page.locator('#studio-manager-apply').click();
 await page.waitForFunction(()=>document.querySelector('#studio-manager-dialog').hidden);
 assert.deepEqual(Object.keys(state.writes.at(-1).ui_prefs).sort(),['assign_mode','gate_mode','manager_agent']);
 await page.evaluate(()=>{const ts=Date.now()/1000;ingest({type:'plan_created',ts,goal:'legacy',tasks:[{id:'old',title:'Old task',type:'coding',status:'pending',depends_on:[]}]});ingest({type:'gate_opened',kind:'plan',gate_id:'legacy-plan',gate_token:'old-token',available_agents:S.agents,ts})});
 await page.locator('#chat .card .node[data-task-id="old"]').click();
 const oldPicker=page.locator('[data-picker="old"]');
 await oldPicker.locator('[data-option="agent"]').selectOption('claude_code');
 assert.equal(await oldPicker.locator('[data-option="model"]').count(),0,'Legacy task picker must not offer ignored parameters');
 assert.match(await oldPicker.textContent(),/尚未支持模型设置/);
 await page.getByRole('button',{name:'放行开工',exact:true}).click();
 const oldReply=state.writes.at(-1);
 assert.equal(oldReply.gate_id,'legacy-plan');assert.equal(Object.hasOwn(oldReply,'execution_options'),false,'Legacy gates receive only supported fields');
 state.legacyAgents=false;agents.find(a=>a.name==='codex-local').enabled=false;
 await page.evaluate(()=>window.dispatchEvent(new CustomEvent('agent-connections-changed')));
 await page.waitForFunction(()=>S.agents.find(a=>a.name==='codex-local').enabled===false);
 assert.equal(await page.locator('#sw-manager option[value="codex-local"]').isDisabled(),true,'Connection changes refresh execution choices');
 await page.evaluate(()=>{ingest({type:'run_finished',ts:Date.now()/1000,total_cost_usd:0,cost_complete:false,statuses:{old:'done'}});S.activeIdx=S.sessions.length-1;S.tab='cost';renderAll()});
 assert.match(await page.locator('#cost-num').textContent(),/费用未知/,'Unknown API cost is not displayed as free');
 agents.push(...['api-one','api-two'].map((name,i)=>({...agents[0],name,kind:'api',group:'API '+i,enabled:true,effort_supported:false})));
 await page.evaluate(()=>window.dispatchEvent(new CustomEvent('agent-connections-changed')));
 await page.waitForFunction(()=>S.agents.some(a=>a.name==='api-two'));
 await page.locator('#studio-manager-trigger').click();
 assert.equal(await page.locator('[data-manager="api-one"]').count(),1);
 await page.locator('[data-manager="api-two"]').click();
 assert.equal(await page.locator('[data-manager="api-two"]').getAttribute('aria-pressed'),'true');
 assert.equal(await page.locator('[data-manager="api-one"]').getAttribute('aria-pressed'),'false');
 await page.locator('#studio-manager-cancel').click();
 await page.evaluate(()=>{S.sessions=[];S.activeIdx=-1;S.chatSession=null;S.preChat=[];S.runId='failed-plan';S.tab='cost';ingest({type:'api_cost_updated',total_cost_usd:.12,paid_api_cost_usd:.12,cost_complete:false});ingest({type:'run_crashed',error:'fixture',total_cost_usd:.12,cost_complete:false});renderAll()});
 assert.match(await page.locator('#cost-num').textContent(),/费用未知.*0.1200/,'Replayed planning failure keeps its known cost');
 assert.deepEqual(errors,[]);
 assert.deepEqual(unexpected,[]);
 const result={status:'passed',modelCalls:0,browserErrors:errors,screenshots:8};
 fs.writeFileSync(path.join(shots,'verification.json'),JSON.stringify(result,null,2));
 console.log(JSON.stringify(result,null,2));
})().catch(err=>{console.error(err);process.exitCode=1}).finally(async()=>{if(browser)await browser.close();if(server)server.close()});
