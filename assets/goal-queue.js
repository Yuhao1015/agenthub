/* Goal identities own their draft and asynchronous results. Queries never run a model. */
(() => {
 'use strict';
 const records=new Map(),drafts=new Map(),inputs=new Map(),pending=new Set(),chats=new Map(),replies=new Map(),followups=new Map(),runLoads=new Map();
 let selectedId=null,revision=-1,mutation=0,available=false,loading=null;
 const labels={waiting:'排队中',processing:'经理处理中',starting:'正在启动',awaiting_input:'等待你回复',running:'运行中',blocked:'配置需要更新',interrupted:'已中断',done:'完成',failed:'失败',cancelled:'已取消'};
 const selected=()=>records.get(selectedId)||drafts.get(selectedId)||null;
 const saveInput=()=>{if(selectedId)inputs.set(selectedId,$('#prompt').value)};
 const keepRecord=r=>{
  const old=records.get(r.id);records.set(r.id,r);
  if(!old||JSON.stringify(old.history)!==JSON.stringify(r.history))chats.delete(r.id);
 };
 function select(id){
  saveInput();selectedId=id;S.followLatest=false;
  const record=records.get(id),index=record?.run_id?S.sessions.findIndex(s=>s.runId===record.run_id):-1;
  S.activeIdx=index>=0?index:S.sessions.length;
  S.drawer=null;S.preview=null;S.pvDiff=false;pvShown=null;closeReview();
  S.pendingChoiceAnswers=null;
  $('#prompt').value=inputs.get(id)||'';autoGrow();renderAll();
  if(record?.run_id)loadRun(record);
 }
 async function loadRun(record){
  const runId=record.run_id;
  if(S.sessions.some(s=>s.runId===runId)||runLoads.has(runId))return;
  const promise=(async()=>{
   try{
    const response=await fetch('/goals/'+record.id+'/run',{cache:'no-store'});
    const data=await response.json();
    if(!response.ok||data.run_id!==runId||!Array.isArray(data.events))throw new Error('历史任务暂时无法读取，追问回复仍会显示');
    if(!S.sessions.some(s=>s.runId===runId)){
     const restored=hydrateRunSession(runId,data.events);
     if(restored){restored.id='history-'+runId;S.sessions.unshift(restored);if(S.activeIdx>=0)S.activeIdx++}
    }
   }catch(e){hint(e.message||'历史任务读取失败，请重新选择重试')}
   finally{runLoads.delete(runId);renderAll()}
  })();
  runLoads.set(runId,promise);
  renderAll();
 }
 function reconcileRestored(){
  const id=S.chatSession;
  if(id&&records.has(id)){
   stopRestoredManagerPolling();stopManagerProgress();clearRestoredDraftState();
   if(!selectedId)select(id);
  }
 }
 function newDraft(){
  const id=newSessionId();drafts.set(id,{id,draft:true,state:'draft',message:'',request:null});
  select(id);$('#prompt').focus();return id;
 }
 function legacy(){saveInput();selectedId=null;S.pendingChoiceAnswers=null}
 function draftPending(){
  const r=selected();
  return !!r&&(pending.has(r.id)||r.pending||['waiting','processing','starting'].includes(r.state));
 }
 async function refresh(){
  const generation=mutation;
  const response=await fetch('/goals',{cache:'no-store'});
  if(!response.ok){if(available)hint('目标列表读取失败，请重试');return false}
  const data=await response.json();
  if(!Array.isArray(data.items)||!Number.isInteger(data.revision))return false;
  available=true;
  if(generation!==mutation||data.revision<revision)return true;
  revision=data.revision;
  for(const r of data.items)keepRecord(r);
  renderAll();return true;
 }
 async function init(){
  if(loading)return loading;
  loading=(async()=>{
   try{await refresh();reconcileRestored();if(available&&S.mode==='live'&&!S.chatSession&&!selectedId)newDraft()}
   catch(_){hint('目标列表暂时无法读取，请刷新重试')}
   if(available)Timers.start('goals',()=>refresh().catch(()=>hint('目标列表连接已断开')),1000);
  })();
  try{await loading}finally{loading=null}
 }
 async function post(path,body){
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok)throw new Error(data.error||'目标操作失败');
  if(data.goal){mutation++;keepRecord(data.goal)}
  return data;
 }
 async function send(text,answers){
  const target=selected();if(!target||draftPending())return;
  const id=target.id;
  const context=target.draft?{workdir:S.workdir,prefs:{...currentPrefs()}}:null;
  // Reserve this identity before awaiting settings, but leave every other draft usable.
  inputs.set(id,text);pending.add(id);renderAll();
  try{
   if(!await waitForPrefs())throw new Error('设置尚未保存，请重试');
   let body,path;
   if(target.draft){
    if(target.request&&target.request.message!==text)throw new Error('请先重试原内容确认上次提交，或新建目标');
    body=target.request||(target.request={request_id:id,message:text,...context});
    path='/goals';
   }else{
    if(target.state!=='awaiting_input')throw new Error('此目标当前不等待经理回复');
    if(!replies.has(id))replies.set(id,{request_id:newSessionId(),message:text,choice_answers:answers||{}});
    body=replies.get(id);
    if(body.message!==text)throw new Error('请先确认上一条回复的提交状态');
    path='/goals/'+id+'/message';
   }
   await post(path,body);inputs.set(id,'');
   if(target.draft)drafts.delete(id);else replies.delete(id);
   if(selectedId===id){$('#prompt').value='';autoGrow();S.pendingChoiceAnswers=null}
  }catch(e){
   hint(e.message||'目标提交失败，输入已保留');
   // A read can establish acceptance even when the POST response was lost.
   try{
    const known=await refresh();
    if(target.draft&&known){
     if(records.has(id)){drafts.delete(id);inputs.set(id,'');if(selectedId===id)$('#prompt').value=''}
     else target.request=null;
    }
   }catch(_){}
  }finally{pending.delete(id);renderAll()}
 }
 async function followup(text){
  const target=selected(),match=text.match(FOLLOW_UP_RE);if(!target?.run_id||!match)return;
  const id=target.id;if(pending.has(id))return;
  pending.add(id);inputs.set(id,text);renderAll();
  try{
   const response=await fetch('/task/'+encodeURIComponent(match[1])+'/message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:match[2],run_id:target.run_id})});
   const data=await response.json();if(!response.ok)throw new Error(data.error||'追问失败');
   if(!followups.has(id))followups.set(id,[]);
   followups.get(id).push({role:'user',text,ts:nowTs()},{role:'manager',text:data.queued?'消息已排队，执行器会在此目标中回复。':data.reply||'（无回复）',ts:nowTs()});
   inputs.set(id,'');if(selectedId===id){$('#prompt').value='';autoGrow()}
  }catch(e){hint(e.message)}finally{pending.delete(id);renderAll()}
 }
 async function action(id,name){
  if(pending.has(id))return;
  if(name==='retry'&&!window.confirm('重试将使用当前工作区和设置，可能继续消耗 token 或额度。是否重试此目标？'))return;
  if(name==='resolve'&&!window.confirm('请先检查此目标的已有运行，并停止仍在运行的 Agent。确认运行已停止后，结束此目标并继续队列？已有运行不会重新执行。'))return;
  const body=name==='retry'?{request_id:newSessionId(),use_current_settings:true,workdir:S.workdir,prefs:currentPrefs()}:name==='resolve'?{confirm_stopped:true}:{};
  pending.add(id);renderAll();
  try{await post('/goals/'+id+'/'+name,body)}catch(e){hint(e.message)}finally{pending.delete(id);renderAll()}
 }
 function chatMessages(r){
  if(r.draft)return [];
  if(!chats.has(r.id))chats.set(r.id,historyToChat(r.history||[]));
  const messages=chats.get(r.id).slice();
  const sess=S.sessions.find(s=>s.runId===r.run_id);
  if(sess)messages.push(...sess.chat.filter(m=>!m.restoredManager));
  messages.push(...(followups.get(r.id)||[]));
  if(!messages.length)messages.push({role:'user',text:r.message,ts:r.created_at||nowTs()});
  if(r.pending)messages.push({role:'manager',text:'__typing__',ts:r.updated_at||nowTs(),goalProgress:r.progress});
  return messages;
 }
 function session(){const r=selected();return r?.run_id?S.sessions.find(s=>s.runId===r.run_id)||null:null}
 function renderSide(nav){
  for(const r of [...drafts.values(),...records.values()].reverse()){
   const button=document.createElement('button');button.className='sess'+(selectedId===r.id?' active':'');button.dataset.goalId=r.id;
   const title=document.createElement('span');title.className='s-title';title.textContent=r.message||inputs.get(r.id)||'新目标草稿';
   const meta=document.createElement('span');meta.className='s-meta';meta.textContent=r.draft?'未提交':labels[r.state]||r.state;
   if(r.state==='waiting')meta.textContent+=' · 第 '+r.position+' 位';
   button.append(title,meta);button.onclick=()=>select(r.id);nav.appendChild(button);
  }
 }
 function renderStatus(chat,r){
  if(r.draft)return;
  const box=document.createElement('div');box.className='goal-status';box.setAttribute('role','status');
  const label=document.createElement('strong');label.textContent=labels[r.state]||r.state;box.append(label);
  const text=document.createElement('p');text.textContent=r.error||(r.state==='waiting'?'第 '+r.position+' 位；前面的目标完成后，经理会先与你澄清。':'');box.append(text);
  if(runLoads.has(r.run_id)){const loading=document.createElement('p');loading.textContent='正在读取此目标的任务…';const spinner=document.createElement('span');spinner.className='ah-spinner';spinner.setAttribute('aria-hidden','true');loading.prepend(spinner);box.append(loading)}
  if(['waiting','awaiting_input','running'].includes(r.state)&&!r.pending){
   const button=document.createElement('button');button.className='btn small';button.textContent=r.state==='running'?'停止此目标':'取消等待';button.disabled=pending.has(r.id);button.onclick=()=>AsyncUI.run(button,'正在取消…',()=>action(r.id,'cancel'));box.append(button);
  }
  if(['blocked','interrupted'].includes(r.state)&&!r.run_id){
   const button=document.createElement('button');button.className='btn small';button.textContent='使用当前设置重试';button.disabled=pending.has(r.id);button.onclick=()=>AsyncUI.run(button,'正在提交…',()=>action(r.id,'retry'));box.append(button);
  }
  if(r.state==='interrupted'&&r.run_id){
   const button=document.createElement('button');button.className='btn small';button.textContent='确认运行已停止并结束目标';button.disabled=pending.has(r.id);button.onclick=()=>AsyncUI.run(button,'正在结束…',()=>action(r.id,'resolve'));box.append(button);
  }
  chat.append(box);
 }
 window.GoalQueue={records,drafts,labels,get available(){return available},init,newDraft,select,legacy,submit:send,reply:send,
  cancel:id=>action(id,'cancel'),retry:id=>action(id,'retry'),resolve:id=>action(id,'resolve'),reconcileRestored,refresh,selected,selectedRunId:()=>selected()?.run_id||'',
  draftPending,requestPending:()=>pending.has(selectedId),chatMessages,session,renderSide,renderStatus,followup,ownsRun:id=>[...records.values()].some(r=>r.run_id===id),
  inputChanged:()=>{if(selectedId)inputs.set(selectedId,$('#prompt').value)}};
 window.addEventListener('pageshow',e=>{if(e.persisted)init()});
})();
