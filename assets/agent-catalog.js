/* Directory entries come exclusively from inspection responses. */
(()=>{'use strict';
 const mounts=new Map(),rows=new Map(),locks=new Set(),manual=new Map();
 const el=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e};
 const btn=(text,fn)=>{const e=el('button',text);e.type='button';e.onclick=fn;return e};
 const date=n=>n?new Date(n*1000).toLocaleString():'尚未检查';
 const authText={logged_in:'已登录',not_logged_in:'未登录',unsupported:'暂不支持自动检查登录',error:'登录检查失败',unchecked:'尚未检查登录'};
 async function request(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok){const error=new Error(d.error||'操作失败，请重试');error.data=d;throw error}return d}
 function update(next){for(const row of next)rows.set(row.kind,row);for(const [box,state] of mounts){if(!box.isConnected){mounts.delete(box);continue}render(box,state)}}
 function publish(row,inspection){if(rows.get(row.kind)?.inspection?.fingerprint!==row.inspection?.fingerprint)return;update([{...row,inspection}]);for(const state of mounts.values())state.onChanged?.(inspection);window.dispatchEvent(new CustomEvent('agent-catalog-changed',{detail:{kind:row.kind}}))}
 async function refresh(kind,revision){const row=rows.get(kind);if(!row?.enabled)throw new Error('请先接入此 Agent');const result=await request('/settings/local-agents/check',{kind,revision});publish(row,result.inspection);return result.inspection}
 function confirm(row,target,opener){
  const inspection=row.inspection||{},dialog=el('dialog',undefined,'ah-model-confirm');dialog.setAttribute('aria-label','确认模型测试');
  dialog.append(el('h3','测试 '+row.label),el('p',target.name||target.id||'执行器默认'),el('p','这次真实调用可能消耗 token、订阅额度或积分。仅测试这个模型；不会发送工作区内容或历史。'));
  const status=el('p');status.setAttribute('role','status');let running=false;
  const close=()=>{dialog.close();dialog.remove();const fallback=[...mounts.keys()].flatMap(box=>[...box.querySelectorAll('.ah-model-row')]).find(line=>line.dataset.modelId===target.id&&line.dataset.modelSource===target.source&&line.closest('[data-agent-kind]')?.dataset.agentKind===row.kind&&line.getClientRects().length)?.querySelector('button');(opener?.isConnected?opener:fallback)?.focus()};
  const cancel=btn('取消测试',close),yes=btn('确认测试',async()=>{
   if(running)return;if(locks.has(row.kind)){status.textContent='此 Agent 正在检查，请稍后测试。';return}running=true;locks.add(row.kind);cancel.disabled=true;
   try{await AsyncUI.run(yes,'正在测试…',async()=>{
    const result=await request('/settings/local-agents/test',{kind:row.kind,model:target.id,source:target.source,revision:inspection.revision,fingerprint:inspection.fingerprint,confirm_cost:true});
    publish(row,result.inspection);
    if(result.ok!==true)throw new Error(result.error||'测试失败，请检查配置');
   });close()}catch(e){if(e.data?.inspection)publish(row,e.data.inspection);status.textContent=e.message;cancel.disabled=false}finally{running=false;locks.delete(row.kind)}
  });
  dialog.append(status,cancel,yes);dialog.addEventListener('cancel',e=>{e.preventDefault();if(!running)close()});document.body.append(dialog);dialog.showModal();cancel.focus();
 }
 function render(box,state){
  const opened=new Set([...box.querySelectorAll('details[data-agent-kind][open]')].map(e=>e.dataset.agentKind));
  const searches=new Map([...box.querySelectorAll('[data-search-kind]')].map(e=>[e.dataset.searchKind,e.value]));
  box.classList.add('ah-agent-catalog');box.replaceChildren();
  for(const kind of state.kinds){const row=rows.get(kind);if(!row)continue;const i=row.inspection||{},group=el('details');group.dataset.agentKind=kind;group.open=opened.has(kind);
   const summary=el('summary'),heading=el('strong',row.label||kind),badge=el('span',row.enabled?(authText[i.auth?.status]||'尚未检查登录'):'未接入','ah-catalog-meta');summary.append(heading,badge);group.append(summary);
   const status=el('p','', 'ah-catalog-meta');status.setAttribute('role','status');
   const refreshButton=btn('检查登录与刷新目录',async()=>{
    if(locks.has(kind))return;locks.add(kind);
    try{await AsyncUI.run(refreshButton,'正在检查登录与目录…',()=>refresh(kind,i.revision));status.textContent='检查完成'}catch(e){status.textContent='检查失败：'+e.message}finally{locks.delete(kind)}
   });refreshButton.disabled=!row.enabled;group.append(refreshButton,status);
   if(!row.enabled){group.append(el('p','请先在接入窗口选择此 Agent。'));box.append(group);continue}
   const catalog=i.catalog||{},meta=el('p',(catalog.stale?'刷新失败，保留上次读取的目录。 ':'')+'目录来源：'+(catalog.source||'尚未读取')+' · '+date(catalog.read_at),'ah-catalog-meta');group.append(meta);
   if(catalog.status!=='ready')group.append(el('p',catalog.status==='unsupported'?'此 Agent 暂不支持自动读取模型列表':'尚无有效目录；请检查登录并刷新目录。'));
   if(catalog.status==='unsupported')group.append(el('p','请打开原 Agent 查看可用模型，再在下面填写模型 ID。'));
   const search=el('input');search.type='search';search.placeholder='搜索已读取的模型';search.setAttribute('aria-label',(row.label||kind)+' 搜索模型');search.dataset.searchKind=kind;search.value=searches.get(kind)||'';group.append(search);
   const lists=el('div');group.append(lists);
   function modelRow(model,source){const target={...model,source},line=el('div',undefined,'ah-model-row');line.dataset.modelId=model.id;line.dataset.modelSource=source;
    line.append(el('strong',model.name||model.id||'执行器默认'),el('code',model.id));
    if(model.is_default||model.hidden)line.append(el('small',[model.is_default?'目录默认':'',model.hidden?'隐藏型号':''].filter(Boolean).join(' · ')));
    const result=i.tests?.[JSON.stringify([source,model.id])],labels={passed:'测试通过',failed:'测试失败',expired:'结果已失效'};
    line.append(el('span',result?(labels[result.status]||'未测试')+' · '+date(result.tested_at):'未测试','ah-catalog-meta'));
    if(result){line.append(el('small',result.cost_status==='unknown'?'工具未报告费用':result.cost_usd!==undefined?'报告费用 $'+result.cost_usd:result.cost_credits!==undefined?'报告积分 '+result.cost_credits:'工具未报告费用'));if(result.usage)line.append(el('small',Object.entries(result.usage).map(([k,v])=>k+': '+v).join(' · ')))}
    if(result?.status==='failed')line.append(el('small',result.error||'调用失败，请检查配置'));
    const test=btn('测试此模型',()=>confirm(row,target,test));line.append(test);return line;
   }
   function draw(){lists.replaceChildren();const term=search.value.toLowerCase();const section=(title,models,source)=>{const filtered=models.filter(m=>(m.id+' '+(m.name||'')).toLowerCase().includes(term));if(!filtered.length)return;lists.append(el('h4',title));for(const m of filtered)lists.append(modelRow(m,source))};
    section('Agent 实际目录',catalog.models||[],'catalog');section('当前配置模型（非目录读取结果）',(i.configured_models||[]).map(m=>typeof m==='string'?{id:m}:({id:m.model||m.id,name:m.label||m.name})),'configured');
    section('你手动添加的模型（未读取）',(manual.get(kind)||[]).map(id=>({id})),'manual');
    if(['codex','claude_code'].includes(kind))section('默认模式（不会验证具体型号）',[{id:'',name:'执行器默认'}],'runtime_default');
   }search.oninput=draw;draw();
   const input=el('input');input.placeholder='从原 Agent 复制模型 ID';input.setAttribute('aria-label',(row.label||kind)+' 手动模型 ID');const add=btn('添加模型 ID',()=>{const value=input.value.trim();if(!value||value.length>200||value.startsWith('-')||/[\s\p{C}]/u.test(value)){status.textContent='请输入有效模型 ID（200 字以内，不含空白或控制字符）。';return}manual.set(kind,[...new Set([...(manual.get(kind)||[]),value])]);input.value='';draw()});group.append(input,add);box.append(group);
  }
 }
 window.AgentCatalog={mount(box,{rows:next,onChanged}={}){for(const r of next||[])rows.set(r.kind,r);mounts.set(box,{kinds:(next||[]).map(r=>r.kind),onChanged});render(box,mounts.get(box))},update,get:kind=>rows.get(kind)?.inspection,refresh};
})();
