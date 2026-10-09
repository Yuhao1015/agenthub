/* Presentation adapter over the existing dashboard state. No new API contracts. */
(() => {
 'use strict';
 const create=(tag,cls,text)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=text;return el};
 const icon=name=>{const el=create('i');el.dataset.lucide=name;el.setAttribute('aria-hidden','true');return el};
 const button=(id,text,iconName)=>{const el=create('button','',text);el.id=id;el.type='button';if(iconName)el.prepend(icon(iconName));return el};
 const statusText={pending:'待依赖',ready:'待开始',running:'进行中',retrying:'重试中',awaiting_input:'需要回复',done:'已完成',failed:'失败',skipped:'已跳过'};
 const sourceText={claude_code:'Claude Code · 订阅',codex:'Codex CLI · 登录账号',codebuddy:'WorkBuddy / CodeBuddy · 积分',dsh:'DeepSeek Harness · 按量，未计量',openrouter:'OpenRouter · 按量',mock:'本地模拟'};
 const aliases={'cc-sonnet-mgr':'Claude Code · Sonnet','cc-sonnet':'Claude Code · Sonnet','cc-opus':'Claude Code · Opus','cc-haiku':'Claude Code · Haiku','codex-local':'Codex'};
 const agentByName=name=>S.agents.find(a=>a.name===name);
 const agentLabel=name=>{const a=agentByName(name);return a?.display_name||a?.label||aliases[name]||name||'自动选择'};
 const main=$('#main'),layout=create('div');layout.id='studio-layout';
 const conversation=create('section');conversation.id='studio-conversation';conversation.setAttribute('aria-label','与经理的对话');
 const canvas=create('aside');canvas.id='studio-canvas';canvas.setAttribute('aria-label','团队画布');
 const intro=create('div','studio-intro');
 intro.innerHTML='<div class="studio-eyebrow">An idea, a team, a little momentum.</div><h1>让想法，有人一起完成。</h1><p>与你的经理讨论目标，让团队的计划、进度与交付在同一个工作台展开。</p>';
 const prompts=create('div','studio-prompts');
 for(const [label,text] of [['设计一个产品首页','帮我设计一个清晰、克制的产品首页。'],['一起审阅项目','帮我审阅这个项目，列出最值得优先修复的问题。'],['研究一个新想法','研究多 agent 的协作模式，整理成一份可讨论的方案。']]){
  const b=create('button','',label);b.onclick=()=>setPromptText(text);prompts.append(b);
 }
 intro.append(prompts);$('#chat-scroll').prepend(intro);
 conversation.append($('#chat-scroll'),$('#input-bar'));
 canvas.innerHTML='<div class="studio-canvas-heading"><div><div class="studio-eyebrow">Shared canvas</div><h2>思路展开，团队就位。</h2></div><button id="studio-canvas-close" aria-label="关闭团队画布"><i data-lucide="x"></i></button></div><div class="studio-tabs" role="tablist" aria-label="团队画布内容"><button id="studio-team-tab" class="on" role="tab" aria-selected="true">团队</button><button id="studio-deliverables-tab" role="tab" aria-selected="false">交付物</button><button id="studio-activity-tab" role="tab" aria-selected="false">活动</button></div><div class="studio-manager-card"><div class="studio-manager-mark"><i data-lucide="sparkles"></i></div><div class="studio-manager-copy"><small id="studio-manager-kind">由你指定的经理</small><strong id="studio-manager-name">自动选择</strong><small id="studio-manager-subline">按当前经理路由选择</small></div><button id="studio-change-manager">换经理</button></div>';
 canvas.append($('#strip'));
 const taskSection=create('section');taskSection.id='studio-tasks';taskSection.innerHTML='<div class="studio-section-title"><strong>团队计划</strong><span id="studio-task-progress">还没有任务</span></div><div id="studio-task-list"></div>';
 const filesSection=create('section');filesSection.id='studio-deliverables';filesSection.hidden=true;filesSection.innerHTML='<div class="studio-section-title"><strong>交付文件</strong><span id="studio-file-count"></span></div><div id="studio-files"></div>';
 const activitySection=create('section');activitySection.id='studio-activity-section';activitySection.hidden=true;activitySection.innerHTML='<div class="studio-section-title"><strong>协作轨迹</strong><span>真实事件</span></div><div id="studio-activity"></div>';
 const attention=button('studio-attention','有事项需要你处理','bell');attention.className='studio-attention';attention.hidden=true;
 const foot=create('div');foot.id='studio-canvas-foot';foot.innerHTML='<span>AgentHub Studio</span><span id="studio-data-source">LIVE · 当前运行</span>';
 canvas.append(taskSection,filesSection,activitySection,attention,foot);
 layout.append(conversation,canvas);main.insertBefore(layout,$('#drawer-wrap'));
 $('.brand span').textContent='agenthub.';
 const nav=create('nav','studio-nav');nav.setAttribute('aria-label','工作台导航');
 const home=button('studio-home','工作台','panels-top-left');home.className='selected';
 const needs=button('studio-needs-trigger','需要你','bell');const needsCount=create('span','studio-count','0');needsCount.id='studio-needs-count';needs.append(needsCount);
 const library=button('studio-library','交付物','files');
 const search=create('a','','搜索对话');search.id='studio-search';search.href='settings.html#search-input';search.prepend(icon('search'));nav.append(search,home,needs,library);
 const project=create('div','studio-project');project.innerHTML='<small>工作空间</small><a href="settings.html"><b>A</b>当前项目与配置<i data-lucide="chevron-right"></i></a>';
 $('#side-head').after(nav,project);$('#side-label').textContent='最近的目标';
 $('#topbar .title').innerHTML='<span>我的工作空间 /</span>AgentHub';
 const canvasToggle=button('studio-canvas-toggle','','panel-right-open');canvasToggle.setAttribute('aria-label','展开或收起团队画布');$('#topbar').append(canvasToggle);
 const menu=button('studio-menu','','menu');menu.setAttribute('aria-label','展开导航');$('#topbar').prepend(menu);
 const managerTrigger=button('studio-manager-trigger','','sparkles');const managerTriggerLabel=create('span');managerTrigger.append(managerTriggerLabel,icon('chevron-down'));$('#run-bar').prepend(managerTrigger);
 $('#sw-manager').closest('details').hidden=true;
 $('#run-bar').append($('#send'));$('#input-bar').append($('#input-foot'));
 const localCard=create('div','studio-local-card');localCard.append(create('strong','','你的模型，你的团队'),create('p','','从已配置的 Agent 中选择负责人。'));$('#side-foot').before(localCard);
 const connect=button('studio-connect','接入本机 Agent');connect.onclick=()=>window.AgentConnections.open({tab:'local'});localCard.append(connect);window.addEventListener('agent-connections-changed',()=>loadAgents());
 let tab='team',draftManager='',draftOptions={},draftError='',savingManager=false,focusBefore=null;
 function overlay(id,title){const el=create('div','studio-overlay');el.id=id;el.hidden=true;el.innerHTML='<section class="studio-dialog" role="dialog" aria-modal="true" aria-labelledby="'+id+'-title"><div class="studio-dialog-head"><div><div class="studio-eyebrow">A little clarity, a better collaboration.</div><h2 id="'+id+'-title"></h2><p></p></div><button class="studio-close" aria-label="关闭面板"><i data-lucide="x"></i></button></div><div class="studio-dialog-body"></div><div class="studio-dialog-foot"></div></section>';el.querySelector('h2').textContent=title;el.querySelector('.studio-close').onclick=()=>closeOverlay(el);el.addEventListener('click',e=>{if(e.target===el)closeOverlay(el)});document.body.append(el);return el}
 const managerDialog=overlay('studio-manager-dialog','谁来带领这次协作？');
 managerDialog.querySelector('.studio-dialog-head p').textContent='经理和你讨论目标、梳理计划。执行任务的人，可以另选。';
 managerDialog.querySelector('.studio-dialog-body').innerHTML='<div id="studio-manager-options" role="group" aria-label="选择经理"></div><div id="studio-manager-selection"></div><div id="studio-manager-policy"></div><div id="studio-manager-error" role="status"></div>';
 const managerFoot=managerDialog.querySelector('.studio-dialog-foot');const precision=create('small');precision.id='studio-manager-precision';
 const managerCancel=button('studio-manager-cancel','取消');managerCancel.className='btn';managerCancel.onclick=()=>closeOverlay(managerDialog);
 const managerApply=button('studio-manager-apply','使用这位经理','arrow-right');managerApply.className='btn primary';managerFoot.append(precision,managerCancel,managerApply);
 const needsDialog=overlay('studio-needs-dialog','有件事，需要你。');needsDialog.querySelector('.studio-dialog-head p').textContent='当前运行的确认、提问和异常，汇聚在这里。';needsDialog.querySelector('.studio-dialog-body').innerHTML='<div id="studio-needs-list"></div>';needsDialog.querySelector('.studio-dialog-foot').remove();
 function openOverlay(el){focusBefore=document.activeElement;$('#app').inert=true;el.hidden=false;el.querySelector('.studio-close').focus()}
 function closeOverlay(el){if(el===managerDialog&&savingManager)return;el.hidden=true;$('#app').inert=[managerDialog,needsDialog].some(x=>!x.hidden);focusBefore?.focus()}
 function familyLabel(a){return a?.group||{claude_code:'Claude Code',codex:'Codex',codebuddy:'WorkBuddy',dsh:'DeepSeek Harness',openrouter:'OpenRouter',mock:'本地模拟'}[a?.kind]||a?.kind||'自动选择'}
 function familyKey(a){return a?.kind==='api'?a.name:a?.kind}
 function selectionSummary(name,options){
  const a=agentByName(name);if(!a)return name||'自动选择';
  const model=Object.hasOwn(options,'model')?(options.model||'执行器默认'):(a.model||'配置默认');
  const effort=Object.hasOwn(options,'effort')?(options.effort||'默认'):(a.effort||'默认');
  return familyLabel(a)+' · '+model+' · '+effort;
 }
 function openManager(){if(draftLocked())return;draftManager=$('#sw-manager').value;draftOptions={...managerExecutionOptions};draftError='';$('#studio-manager-error').textContent='';renderManagerOptions();openOverlay(managerDialog)}
 managerTrigger.onclick=openManager;$('#studio-change-manager').onclick=openManager;
 // Action icons retain names for assistive technology and hover hints.
 for(const [id,label,glyph] of [['studio-change-manager','换经理','repeat-2'],['studio-connect','接入本机 Agent','plug'],['studio-manager-cancel','取消','x'],['studio-manager-apply','使用这位经理','arrow-right']]){
  const el=document.getElementById(id);el.classList.add('icon-only');el.title=label;el.setAttribute('aria-label',label);el.replaceChildren(icon(glyph));
 }
 function updateManagerPolicy(){
  const a=agentByName(draftManager);
  $('#studio-manager-policy').textContent=(draftManager?'固定经理，不自动换人。':'使用当前配置的经理路由和降级链。')+'\n保存在当前项目，从下一轮经理对话生效。已经开始的任务不重新分配。'+(a?.kind==='openrouter'?'\n这位经理按量计费，经理调用不受运行费用护栏保护。':'');
  precision.textContent=S.agentPrecision||'仅展示已配置的 agent，未验证模型或账号可用性。';
  managerApply.disabled=savingManager||draftLocked()||!!draftError||!!(draftManager&&(!a||!a.installed||a.enabled===false));managerCancel.disabled=savingManager;
 }
 function renderManagerOptions(){
  const options=$('#studio-manager-options');options.replaceChildren();
  const families=new Map();for(const a of S.agents){if(a.enabled===false)continue;const key=familyKey(a);if(!families.has(key))families.set(key,[]);families.get(key).push(a)}
  for(const [kind,profiles] of families){
   const available=profiles.filter(a=>a.installed),ranked=(available.length?available:profiles).slice().sort((a,b)=>(a.manager_rank??999)-(b.manager_rank??999));
   const representative=ranked[0],selected=familyKey(agentByName(draftManager))===kind;
   const b=create('button','studio-manager-option'+(selected?' selected':''));b.dataset.manager=representative.name;b.type='button';b.disabled=savingManager||!available.length;b.setAttribute('aria-pressed',String(selected));
   const heading=create('div','studio-option-heading'),sprite=create('canvas');paintSprite(sprite,representative.animal,2);sprite.setAttribute('aria-hidden','true');
   heading.append(sprite,create('strong','',familyLabel(representative)),create('span','studio-radio'));
   b.append(heading,create('small','',representative.blurb||'选好 Agent，再设置模型与思考程度。'),create('span','studio-option-source',(representative.kind==='api'?'按量 API · 文本生成':sourceText[kind]||kind)+(available.length?'':' · 本机未安装')));
   b.onclick=()=>{if(!selected){draftManager=representative.name;draftOptions={};draftError='';renderManagerOptions();options.querySelector('[data-manager="'+CSS.escape(representative.name)+'"]')?.focus()}};options.append(b);
  }
  const auto=create('button','studio-manager-option automatic'+(!draftManager?' selected':''));auto.type='button';auto.dataset.manager='';auto.disabled=savingManager;auto.setAttribute('aria-pressed',String(!draftManager));
  auto.append(create('strong','','自动选择'),create('small','','由当前经理路由选人，不可用时按配置降级。'));auto.onclick=()=>{draftManager='';draftOptions={};draftError='';renderManagerOptions();options.querySelector('[data-manager=""]')?.focus()};options.append(auto);
  if(!S.agents.length)options.prepend(create('p','studio-note','尚未取得 agent 清单。请启动本地服务，并配置模型资源。'));
  const selection=$('#studio-manager-selection');selection.replaceChildren();const a=agentByName(draftManager);
  if(a){
   selection.append(create('div','studio-section-title','模型与思考程度'));
   const advanced=create('details','studio-profile'),summary=create('summary','', '执行配置 · '+a.name),profile=create('select');profile.id='studio-manager-profile';profile.setAttribute('aria-label','经理执行配置');
   for(const item of families.get(familyKey(a))||[]){const opt=optionEl(item.name,item.name+' · 工具 '+(item.allowed_tools||'配置默认'));opt.disabled=!item.installed;profile.append(opt)}profile.value=a.name;profile.disabled=savingManager;
   profile.onchange=()=>{draftManager=profile.value;draftOptions={};draftError='';renderManagerOptions()};advanced.append(summary,profile);selection.append(advanced);
   if(Array.isArray(a.model_choices)){
    const controls=createExecutionControls(a,draftOptions,(next,_render,_save,error)=>{draftOptions=next;draftError=error;updateManagerPolicy()});
    draftError=controls.dataset.error||'';selection.append(controls);
    for(const el of controls.querySelectorAll('input,select,button'))if(savingManager)el.disabled=true;
   }else{draftOptions={};draftError='';selection.append(create('p','studio-note','当前服务尚未支持模型设置；仍可选择 Agent。'))}
  }else draftError='';
  updateManagerPolicy();icons();
 }
 managerApply.onclick=async()=>{
  if(savingManager||draftLocked()||draftError)return;
  const previous=$('#sw-manager').value,previousOptions={...managerExecutionOptions},agent=agentByName(draftManager);
  if(draftManager&&(!agent||!agent.installed||agent.enabled===false))return;
  savingManager=true;renderManagerOptions();managerDialog.querySelector('.studio-close').focus();$('#studio-manager-error').textContent='';
  $('#sw-manager').value=draftManager;managerExecutionOptions={...draftOptions};renderManagerWarning();
  const ok=await savePrefs();
  if(!ok){$('#sw-manager').value=previous;managerExecutionOptions=previousOptions;renderManagerWarning();$('#studio-manager-error').textContent='经理设置未保存，请重试。原来的 Agent、模型和思考选择已保留。'}
  savingManager=false;renderManagerOptions();renderSummary();
  if(ok){window.renderManagerOptions();closeOverlay(managerDialog);hint('经理配置已保存 · 下一轮对话生效；已开始的任务不变')}
 };
 const applyManager=managerApply.onclick;managerApply.onclick=()=>AsyncUI.run(managerApply,'保存经理设置…',applyManager);
 function renderSummary(){
  const goal=S.mode==='live'?window.GoalQueue?.selected():null,prefs=goal&&!goal.draft?goal.context.prefs:null;
  const name=prefs?prefs.manager_agent||'':$('#sw-manager').value;
  const captured={};
  if(prefs){
   if(prefs.manager_model_mode==='runtime')captured.model='';else if(prefs.manager_model_mode==='explicit')captured.model=prefs.manager_model;
   if(prefs.manager_effort_mode==='default')captured.effort='';else if(prefs.manager_effort_mode==='explicit')captured.effort=prefs.manager_effort;
  }
  const summary=selectionSummary(name,prefs?captured:managerExecutionOptions);
  managerTriggerLabel.textContent=summary;$('#studio-manager-name').textContent=name?familyLabel(agentByName(name)):'自动选择';
  managerTrigger.title=(name?'固定经理 · 不自动换人':'由当前经理路由自动选择')+(S.agentPrecision?' · '+S.agentPrecision:'');
  $('#studio-manager-kind').textContent=name?'由你指定的经理':'自动选择经理';$('#studio-manager-subline').textContent=name?summary.split(' · ').slice(1).join(' · ')+' · 下一轮生效':'按配置路由，保留降级链';
  managerTrigger.disabled=draftLocked();$('#studio-change-manager').disabled=draftLocked();
 }
 function emptyText(container,title,text){container.replaceChildren();const el=create('div','studio-empty');el.append(create('strong','',title),create('p','',text));container.append(el)}
 function pendingItems(){
  const goal=S.mode==='live'?window.GoalQueue?.selected():null;
  const items=[],current=goal?selectedSession():currentRunSession();
  if(current){
   if(current.status==='running')for(const m of current.chat){const c=m.card;if(c&&!c.frozen&&(c.kind==='gate-plan'||c.kind==='gate-task'))items.push({title:c.kind==='gate-plan'?'确认团队计划':'确认写盘任务',note:c.title||'你确认后，团队再继续。',card:c,session:current})}
   for(const t of computeView(current).tasks.values())if(t.status==='awaiting_input'||t.status==='failed'||t.stale)items.push({title:(t.stale?'产出需要复核：':t.status==='failed'?'任务失败：':'任务等你回复：')+(t.title||t.id),note:agentLabel(t.agent||t.intended),task:t.id,session:current});
  }
  if(goal||S.chatSession)for(const m of goal?GoalQueue.chatMessages(goal):S.preChat){if(m.card?.kind==='choices'&&!m.card.frozen)items.push({title:'回答经理的澄清问题',note:'补充一点信息，让目标更清晰。',draft:true,card:m.card})}
  return items;
 }
 function goToItem(item){
  closeOverlay(needsDialog);canvas.classList.remove('mobile-open');S.drawer=null;S.preview=null;S.pvDiff=false;pvShown=null;closeReview();
  S.activeIdx=item.draft?S.sessions.length:S.sessions.indexOf(item.session);S.followLatest=true;renderAll();
  if(item.task)openDrawer(item.task);
  else{const messages=item.draft?S.preChat:item.session.chat;const index=messages.filter(m=>m.card).findIndex(m=>m.card===item.card);$('#chat').querySelectorAll('.card')[index]?.scrollIntoView({behavior:'smooth',block:'center'})}
  $('#side').classList.remove('studio-side-open');
 }
 function renderNeeds(){const items=pendingItems();needsCount.textContent=String(items.length);const list=$('#studio-needs-list');list.replaceChildren();for(const item of items){const b=create('button','studio-needs-item');b.append(create('strong','',item.title),create('small','',item.note));b.onclick=()=>goToItem(item);list.append(b)}if(!items.length)emptyText(list,'现在，可以专心做事。','当前没有等待你处理的确认或任务。');attention.hidden=!items.length;attention.replaceChildren(icon('bell'),create('span','',items.length+' 件事需要你 · 点击查看'))}
 function showNeeds(){renderNeeds();openOverlay(needsDialog)}needs.onclick=showNeeds;attention.onclick=showNeeds;
 function renderTasks(){
  const tasks=[...view.tasks.values()],box=$('#studio-task-list');box.replaceChildren();
  const done=tasks.filter(t=>t.status==='done').length;$('#studio-task-progress').textContent=tasks.length?done+' / '+tasks.length+' 已完成':'等待一个新目标';
  tasks.forEach((t,index)=>{const b=create('button','studio-task');b.type='button';b.dataset.task=t.id;
   const number=create('span','studio-task-number',t.status==='done'?'✓':t.status==='running'?'↗':String(index+1));
   const copy=create('span','studio-task-copy');const name=t.agent||t.pinned_agent||t.intended;const chosen={...(t.execution_options||{})};if(Object.hasOwn(t,'model'))chosen.model=t.model;if(Object.hasOwn(t,'effort'))chosen.effort=t.effort;copy.append(create('strong','',t.title||t.id),create('small','',name?(Object.keys(chosen).length?selectionSummary(name,chosen):familyLabel(agentByName(name))+' · '+(['pending','ready'].includes(t.status)?'按执行配置':'参数未记录')):'按配置路由'));
   const state=t.stale?'stale':t.status;const tag=create('span','studio-state '+state,t.stale?'需复核':statusText[t.status]||t.status);
   b.append(number,copy,tag);b.onclick=()=>openDrawer(t.id);box.append(b);
  });
  if(!tasks.length)emptyText(box,'团队会在这里展开工作。','告诉经理你的目标，一起确认计划，再开始协作。');
 }
 function renderFiles(){
  const files=$('#studio-files');files.replaceChildren();const seen=new Set();
  for(const task of view.tasks.values())for(const file of task.files||[]){if(seen.has(file.path))continue;seen.add(file.path);files.append(fileRow(task.id,file))}
  $('#studio-file-count').textContent=seen.size?seen.size+' 个文件':'暂无交付';
  if(!seen.size)emptyText(files,'把想法变成看得见的结果。','任务产生的交付文件会收纳到这里。先预览，再决定下一步。');
 }
 function renderActivity(){
  const events=(selectedSession()?.events||[]).slice(-40).reverse(),box=$('#studio-activity');box.replaceChildren();
  const names={plan_created:'计划已创建',task_started:'任务开始',task_completed:'任务完成',task_failed:'任务失败',gate_opened:'等待用户确认',gate_resolved:'确认已处理',run_finished:'本次运行结束',run_crashed:'引擎异常退出',run_cancelled:'本次运行取消',agent_thinking:'执行器活动',agent_failed:'执行器失败',task_awaiting_input:'等待用户回复'};
  for(const e of events){const row=create('div','studio-activity-row');row.append(create('strong','',names[e.type]||e.type),create('p','',[e.task_id,e.agent?agentLabel(e.agent):'',e.detail||e.error||e.title||''].filter(Boolean).join(' · ')),create('small','',e.type+' · '+fmtTs(e.ts||nowTs())));box.append(row)}
  if(!events.length)emptyText(box,'协作的轨迹会留在这里。','此处展示已有事件记录，不生成模拟进度旁白。');
 }
 function showTab(next){
  tab=next;$('#strip').hidden=next!=='team';taskSection.hidden=next!=='team';filesSection.hidden=next!=='files';activitySection.hidden=next!=='activity';
  for(const [id,value] of [['studio-team-tab','team'],['studio-deliverables-tab','files'],['studio-activity-tab','activity']]){const b=$('#'+id);b.classList.toggle('on',next===value);b.setAttribute('aria-selected',String(next===value))}
  if(next==='team')requestAnimationFrame(sizeCanvas);
 }
 $('#studio-team-tab').onclick=()=>showTab('team');$('#studio-deliverables-tab').onclick=()=>showTab('files');$('#studio-activity-tab').onclick=()=>showTab('activity');
 function openCanvas(next){showTab(next);if(innerWidth<=950)canvas.classList.add('mobile-open');$('#side').classList.remove('studio-side-open')}
 library.onclick=()=>openCanvas('files');home.onclick=()=>{openCanvas('team');$('#side').classList.remove('studio-side-open')};canvasToggle.onclick=()=>canvas.classList.toggle('mobile-open');$('#studio-canvas-close').onclick=()=>canvas.classList.remove('mobile-open');menu.onclick=()=>$('#side').classList.toggle('studio-side-open');
 document.querySelectorAll('.seg').forEach(b=>b.addEventListener('click',()=>showTab('team')));
 function renderStudio(){
  renderSummary();renderTasks();renderFiles();renderActivity();renderNeeds();showTab(tab);
  const sess=selectedSession(),goal=S.mode==='live'?window.GoalQueue?.selected():null;intro.classList.toggle('compact',goal?!goal.draft:!!(sess||S.chatSession||S.preChat.length||S.pendingGoal));
  $('#studio-data-source').textContent=S.mode==='demo'?'DEMO · 模拟事件':S.liveDown?'已断开 · 保留最后状态':'LIVE · 当前查看的运行';
 }
 const nativeDrawer=openDrawer;openDrawer=function(id){canvas.classList.remove('mobile-open');nativeDrawer(id)};
 const nativePreview=openPreview;openPreview=function(id,file){canvas.classList.remove('mobile-open');nativePreview(id,file)};
 const nativeReview=openReview;openReview=function(id){canvas.classList.remove('mobile-open');nativeReview(id)};
 // Reuse the existing gate controls and handlers in the prototype's task list.
 const nativeCard=cardEl;cardEl=function(card,ts){
  const el=nativeCard(card,ts);if(card.kind!=='gate-plan')return el;
  el.classList.add('studio-plan-card');const list=el.querySelector('.dag-grid');
  const nodes=[...list.querySelectorAll('.node')],rows=[];
  const tasks=[...view.tasks.values()];
  tasks.forEach((task,index)=>{
   const node=nodes.find(n=>n.dataset.taskId===task.id);if(!node)return;
   const picker=node.nextElementSibling?.matches('.agent-picker')?node.nextElementSibling:null;
   const row=create('div','studio-plan-row'),copy=create('span','studio-plan-copy');
   copy.append(node.querySelector('.t'),node.querySelector('.m'));
   const agent=card.agents.find(a=>a.name===card.overrides?.[task.id]);
   const choice=create('span','studio-plan-choice',agent?familyLabel(agent):'自动分配');
   choice.append(icon(card.frozen?'check':'chevron-down'));
   node.replaceChildren(create('span','studio-step',String(index+1)),copy,choice);
   node.disabled=!!card.frozen||!!card.posting;
   node.setAttribute('aria-expanded',String(card.openPicker===task.id));
   row.append(node);if(picker)row.append(picker);rows.push(row);
  });
  list.replaceChildren(...rows);list.className='studio-plan-list';list.removeAttribute('style');
  const head=el.querySelector('.c-head');head.replaceChildren(icon('clipboard-list'),create('strong','',card.frozen?'团队计划':'这次，我们这样推进'));
  head.append(create('span','studio-plan-status'+(card.frozen?' resolved':''),card.frozen?(card.decision==='approve'?'已确认':'已取消'):'等待确认'));
  const caption=create('p','studio-plan-caption',tasks.length+' 个任务 · '+(card.frozen?'本次计划的执行配置已确认':'点击任务选择 Agent、模型与思考程度'));head.after(caption);
  const note=el.querySelector(':scope > .gate-note');if(note)note.textContent=card.frozen?'':'确认后，团队按计划开始。任务依赖可在右侧依赖图查看。';
  return el;
 };
 const nativePrefs=currentPrefs;currentPrefs=function(){const prefs=nativePrefs();return S.agents.some(a=>Array.isArray(a.model_choices))?prefs:{manager_agent:prefs.manager_agent,assign_mode:prefs.assign_mode,gate_mode:prefs.gate_mode}};
 const nativeRender=renderAll;renderAll=function(){nativeRender();renderStudio()};
 const nativeAgents=loadAgents;loadAgents=async function(...args){await nativeAgents(...args);renderStudio();if(!managerDialog.hidden)renderManagerOptions()};
 const nativeMetadata=refreshAgentMetadata;refreshAgentMetadata=async function(...args){await nativeMetadata(...args);renderStudio();if(!managerDialog.hidden)renderManagerOptions()};
 const nativeManagerWarning=renderManagerWarning;renderManagerWarning=function(){nativeManagerWarning();renderSummary()};
 const nativeLiveState=renderLiveState;renderLiveState=function(){nativeLiveState();$('#studio-data-source').textContent=S.mode==='demo'?'DEMO · 模拟事件':S.liveDown?'已断开 · 保留最后状态':'LIVE · 当前查看的运行'};
 const nativeMode=setMode;setMode=function(mode){nativeMode(mode);renderStudio()};
 document.addEventListener('keydown',e=>{
  const el=[managerDialog,needsDialog].find(x=>!x.hidden);
  if(e.key==='Escape'){if(el){e.preventDefault();e.stopImmediatePropagation();closeOverlay(el)}else{canvas.classList.remove('mobile-open');$('#side').classList.remove('studio-side-open')}}
  if(el&&e.key==='Tab'){const focusable=[...el.querySelectorAll('button:not(:disabled),a[href],input,select,textarea')].filter(x=>x.getClientRects().length);const first=focusable[0],last=focusable.at(-1);if(!focusable.includes(document.activeElement)){e.preventDefault();(e.shiftKey?last:first)?.focus()}else if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus()}}
 },true);
 renderStudio();icons();requestAnimationFrame(sizeCanvas);
})();
