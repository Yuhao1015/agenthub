/* Shared connection editor. Discovery is passive; consent is saved separately. */
(()=>{'use strict';
 const make=(tag,cls,text)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=text;return el};
 const button=(text,fn)=>{const b=make('button','',text);b.type='button';b.onclick=()=>{const disabled=b.disabled,result=fn();if(result?.then&&window.AsyncUI){b.disabled=disabled;return AsyncUI.run(b,'处理中…',()=>result)};return result};return b};
 const clone=value=>JSON.parse(JSON.stringify(value));
 const dialog=make('dialog','ah-connections');dialog.setAttribute('aria-labelledby','ah-connections-title');
 const head=make('header'),title=make('h2','','你的模型，你的团队');title.id='ah-connections-title';
 const close=button('关闭',()=>dialog.close());close.setAttribute('aria-label','关闭接入窗口');
 head.append(make('small','','YOUR AGENTS · YOUR CHOICE'),close,title,make('p','','接入本机 Agent 或添加 API 服务，由你决定谁加入团队。'));
 const body=make('div','ah-connections-body'),status=make('p','ah-connections-status');status.setAttribute('role','status');
 const discover=button('发现本机 Agent',scan),save=button('接入所选',persist);save.className='ah-connections-primary';
 const cancel=button('取消',()=>dialog.close()),foot=make('footer');foot.append(status,cancel,save);
 const toolbar=make('div','ah-connections-toolbar');toolbar.append(discover,make('span','','仅检查程序文件；账户与模型未验证。'));
 const cards=make('div','ah-connections-cards'),catalog=make('div');body.append(toolbar,cards,catalog);
 const tabs=make('nav','ah-connections-tabs'),localTab=button('本机 Agent',()=>switchTab('local')),apiTab=button('API 连接',()=>switchTab('api'));
 tabs.setAttribute('aria-label','接入方式');tabs.append(localTab,apiTab);
 const apiBody=make('div','ah-connections-body');apiBody.hidden=true;
 dialog.append(head,tabs,body,apiBody,foot);document.body.append(dialog);
 const formats={openai_compatible:'OpenAI 兼容 · Chat Completions',anthropic:'Anthropic · Messages',openrouter:'OpenRouter'};
 const roles={manager:'经理',planning:'规划',coding:'代码文本',review:'审阅',writing:'写作',analysis:'分析',light:'轻量任务'};
 let tab='local',apiState=null,editing=null,keyAction='replace',keyValue='';
 let draft=null,rows=[],revision=0,busy=false,saving=false,opener=null;
 async function request(url,payload){const response=await fetch(url,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await response.json();if(!response.ok)throw new Error(data.error||'操作失败，请重试');return data}
 function controls(){discover.disabled=busy||saving;save.hidden=tab==='api';save.disabled=!draft||busy||saving;cancel.disabled=saving;close.disabled=saving;localTab.disabled=apiTab.disabled=saving;localTab.setAttribute('aria-current',String(tab==='local'));apiTab.setAttribute('aria-current',String(tab==='api'));for(const input of cards.querySelectorAll('input,select,button'))input.disabled=saving;for(const input of apiBody.querySelectorAll('input,select,button'))input.disabled=busy||saving||input.dataset.unavailable==='true';}
 function render(){cards.replaceChildren();if(!draft)return;
  for(const row of rows){const kind=row.kind,choice=draft.agents[kind];if(!choice)continue;
   const card=make('section','ah-connections-card'),label=make('label','ah-connections-choice'),check=make('input');check.type='checkbox';check.checked=choice.enabled;check.setAttribute('aria-label','启用 '+row.label);check.onchange=()=>choice.enabled=check.checked;
   label.append(check,make('strong','',row.label));card.append(label,make('span','ah-connections-badge',choice.enabled!==!!row.enabled?'待保存选择':row.enabled?({logged_in:'已接入 · 已登录',not_logged_in:'已接入 · 未登录',unsupported:'已接入 · 登录检查暂不支持'}[row.inspection?.auth?.status]||'已接入 · 待检查登录'):row.available?'已发现程序':'尚未找到有效入口'),make('p','',row.detail||'点击发现，或手动填写程序路径。'));
   const details=make('details'),summary=make('summary','','安装位置与手动配置');details.append(summary);
   for(const candidate of row.candidates||[]){const c=make('div','ah-connections-candidate');c.append(make('strong','',candidate.label||'候选安装'),make('code','',Object.values(candidate.paths||{}).join('\n')));const select=button('使用此位置',()=>{choice.paths={...candidate.paths};render()});c.append(select);details.append(c)}
   for(const [key,caption] of kind==='codebuddy'?[['node','Node 程序路径'],['script','CodeBuddy 脚本路径']]:[['executable','CLI 程序路径']]){const field=make('label','ah-connections-field',caption),input=make('input');input.type='text';input.value=choice.paths[key]||'';input.placeholder='留空使用自动发现';input.setAttribute('aria-label',row.label+' '+caption);input.oninput=()=>{choice.paths[key]=input.value};field.append(input);details.append(field)}
   card.append(details,make('small','','启用后使用本机已登录的账号；保存不会发起模型请求。'));cards.append(card);
  }if(window.AgentCatalog)AgentCatalog.mount(catalog,{rows});controls();
 }
 async function open(options={}){if(dialog.open)return;opener=document.activeElement;draft=null;apiState=null;editing=null;keyValue='';rows=[];tab=options.tab==='api'?'api':'local';body.hidden=tab==='api';apiBody.hidden=tab!=='api';busy=true;status.textContent='读取接入设置…';cards.replaceChildren();apiBody.replaceChildren();dialog.showModal();close.focus();controls();if(tab==='api'){await loadApi();return}const token=++revision;
  try{const data=await request('/settings/local-agents');if(token!==revision)return;draft=clone(data.settings);rows=data.agents;status.textContent='选择仅保存在当前项目。';render()}catch(e){if(token===revision)status.textContent=e.message}finally{if(token===revision){busy=false;controls()}}
 }
 async function switchTab(next){if(saving||tab===next)return;++revision;tab=next;busy=false;editing=null;keyValue='';body.hidden=tab==='api';apiBody.hidden=tab!=='api';controls();if(tab==='api')await loadApi();else{busy=true;controls();const token=++revision;try{const data=await request('/settings/local-agents');if(token!==revision)return;draft=clone(data.settings);rows=data.agents;render();status.textContent='选择仅保存在当前项目。'}catch(e){if(token===revision)status.textContent=e.message}finally{if(token===revision){busy=false;controls()}}}}
 async function loadApi(){busy=true;controls();const token=++revision;try{const data=await request('/settings/api-connections');if(token!==revision)return;apiState=data;renderApi();status.textContent='保存不会请求模型；测试连接需另行确认。'}catch(e){if(token===revision){apiBody.replaceChildren(button('重新读取',loadApi));status.textContent=e.message}}finally{if(token===revision){busy=false;controls()}}}
 function field(label,type,value,change){const wrap=make('label','ah-connections-field',label),input=make('input');input.type=type;input.value=value??'';input.setAttribute('aria-label',label);input.oninput=()=>change(input.value);wrap.append(input);return [wrap,input]}
 function select(label,choices,value,change){const wrap=make('label','ah-connections-field',label),input=make('select');input.setAttribute('aria-label',label);for(const [v,t] of Object.entries(choices)){const o=make('option','',t);o.value=v;input.append(o)}input.value=value;input.onchange=()=>change(input.value);wrap.append(input);return [wrap,input]}
 function toggle(label,value,change){const wrap=make('label','ah-connections-choice'),input=make('input');input.type='checkbox';input.checked=!!value;input.setAttribute('aria-label',label);input.onchange=()=>change(input.checked);wrap.append(input,make('span','',label));return wrap}
 function startEdit(item){editing=item?clone(item):{name:'',format:'openai_compatible',base_url:'',model:'',enabled:false,roles:[],tier:null,max_output_tokens:4096,pricing:null};for(const k of ['has_key','verified','test_result','impact'])delete editing[k];keyAction=item?.has_key?'keep':'replace';keyValue='';renderApi();apiBody.querySelector('input')?.focus();status.textContent='填写服务商提供的地址和模型 ID。API 连接仅生成文本，不直接操作文件。'}
 function renderApi(){apiBody.replaceChildren();if(!apiState)return;
  if(editing){renderForm();controls();return}
  const top=make('div','ah-connections-toolbar');top.append(button('添加 API 连接',()=>startEdit()),make('span','',Object.keys(apiState.items).length+' 个连接 · 密钥不回显'));apiBody.append(top);
  if(Object.keys(apiState.migration||{}).length){const migration=make('section','ah-connections-card');migration.append(make('strong','','发现旧版 OpenRouter 配置'),make('p','',Object.values(apiState.migration).map(c=>c.name+' · '+c.model).join('、')),button('确认导入已有连接',()=>mutate('/settings/api-connections/migrate',{revision:apiState.revision,confirm:true})));apiBody.append(migration)}
  if(!Object.keys(apiState.items).length)apiBody.append(make('p','ah-connections-empty','还没有 API 连接。添加你自己的服务地址、模型与密钥。'));
  for(const item of Object.values(apiState.items)){const card=make('section','ah-connections-card');card.append(make('strong','',item.name),make('span','ah-connections-badge',(item.enabled?'已启用':'已停用')+' · '+(item.verified?'本版本测试通过':'未验证')),make('p','',formats[item.format]+' · '+item.model),make('code','',item.base_url),make('small','',item.has_key?'密钥已配置':'未配置密钥'));
   const actions=make('div','ah-connections-actions');actions.append(button('编辑',()=>startEdit(item)),button('测试连接',()=>confirmAction(card,'仅向此地址发送最小测试文本，可能产生费用；不会发送工作区或历史。','确认测试（可能收费）',()=>testConnection(item))),button('删除',()=>{const impact=[...(item.roles||[]).map(r=>roles[r]||r)];if(item.impact?.manager)impact.unshift('当前固定经理');confirmAction(card,'删除后停止接入，历史记录保留。受影响：'+(impact.join('、')||'手动选择')+'。历史凭据副本仍保留在本机。','确认删除',()=>mutate('/settings/api-connections/delete',{id:item.id,revision:apiState.revision}))}));card.append(actions);apiBody.append(card)}controls();
 }
 function confirmAction(card,text,label,action){apiBody.querySelector('.ah-connections-confirm')?.remove();const box=make('div','ah-connections-confirm');box.append(make('p','',text),button(label,action),button('返回',()=>box.remove()));card.append(box);box.querySelector('button').focus()}
 function renderForm(){const form=make('form','ah-connections-form');form.onsubmit=e=>{e.preventDefault();saveApi()};
  form.append(field('连接名称','text',editing.name,v=>editing.name=v)[0]);
  const [urlWrap,urlInput]=field('Base URL','url',editing.base_url,v=>editing.base_url=v);
  form.append(select('接口格式',formats,editing.format,v=>{editing.format=v;if(v==='openrouter'&&!editing.base_url){editing.base_url='https://openrouter.ai/api/v1';urlInput.value=editing.base_url}status.textContent=v==='openrouter'&&editing.base_url!=='https://openrouter.ai/api/v1'?'保留你填写的地址；OpenRouter 标准地址为 https://openrouter.ai/api/v1':'接口格式已选择，保存后生效。'})[0],urlWrap,field('模型 ID','text',editing.model,v=>editing.model=v)[0]);
  const [keyWrap,keyInput]=field('API Key','password','',v=>keyValue=v);keyInput.autocomplete='new-password';keyInput.dataset.unavailable=String(keyAction!=='replace');
  form.append(select('密钥操作',{keep:'保留现有密钥',replace:'填写 / 更换密钥',clear:'清除密钥'},keyAction,v=>{keyAction=v;keyValue='';keyInput.value='';keyInput.dataset.unavailable=String(v!=='replace');controls()})[0],keyWrap,toggle('启用此连接',editing.enabled,v=>editing.enabled=v));
  const advanced=make('details');advanced.append(make('summary','','高级设置 · 职责、档位与费用估算'));
  const responsibilities=make('div','ah-connections-roles');for(const [key,label] of Object.entries(roles))responsibilities.append(toggle(label,editing.roles.includes(key),v=>{editing.roles=v?[...editing.roles,key]:editing.roles.filter(r=>r!==key)}));advanced.append(make('p','','允许自动分配的职责（不选时仅供手动选择）'),responsibilities,select('能力档位',{'':'不指定',economy:'经济',mid:'均衡',strong:'高能力'},editing.tier||'',v=>editing.tier=v||null)[0],field('最大输出 token','number',editing.max_output_tokens,v=>editing.max_output_tokens=Number(v))[0]);
  const price={...(editing.pricing||{})};for(const [key,label] of Object.entries({input:'输入单价',output:'输出单价',cache_read:'缓存读取单价',cache_write:'缓存写入单价'}))advanced.append(field(label+'（美元 / 百万 token）','number',price[key],v=>{if(v==='')delete price[key];else price[key]=Number(v);editing.pricing=Object.keys(price).length?{...price}:null})[0]);
  advanced.append(make('p','','服务商未报告费用时，使用完整单价估算；无法确定费用时会停止同一目标的后续付费调用。单次请求可能超过护栏余额。'));form.append(advanced);
  const actions=make('div','ah-connections-actions'),submit=button('保存连接',saveApi);submit.className='ah-connections-primary';actions.append(button('取消编辑',()=>{editing=null;keyValue='';renderApi()}),submit);form.append(actions);apiBody.append(form);
 }
 async function saveApi(){if(!editing||busy||saving)return;await mutate('/settings/api-connections',{connection:editing,key_action:keyAction,key_value:keyAction==='replace'?keyValue:'',expected_revision:apiState.revision})}
 async function mutate(url,payload){if(busy||saving)return;saving=true;controls();status.textContent='正在保存…';try{apiState=await request(url,payload);editing=null;keyValue='';renderApi();status.textContent='已保存；更改后的连接需要重新测试。';window.dispatchEvent(new CustomEvent('agent-connections-changed'))}catch(e){status.textContent=e.message}finally{saving=false;controls()}}
 async function testConnection(item){if(busy||saving)return;saving=true;controls();status.textContent='正在测试…';try{const result=await request('/settings/api-connections/test',{id:item.id,revision:apiState.revision,confirm_cost:true});item.verified=result.ok===true;renderApi();status.textContent=(result.ok?'测试通过':'测试失败：'+(result.error||'请检查配置'))+' · '+(result.cost_complete===false?'费用未知':'费用 $'+Number(result.cost_usd||0).toFixed(6));}catch(e){status.textContent=e.message}finally{saving=false;controls()}}
 async function scan(){if(busy||saving)return;busy=true;controls();status.textContent='正在查找程序文件…';const token=++revision;
  try{const data=await request('/settings/local-agents/discover',{});if(token!==revision)return;rows=data.agents;render();status.textContent='发现完成，请勾选要接入的 Agent。'}catch(e){if(token===revision)status.textContent=e.message}finally{if(token===revision){busy=false;controls()}}
 }
 async function persist(){if(!draft||busy||saving)return;saving=true;controls();status.textContent='正在保存…';const token=revision;
  try{const data=await request('/settings/local-agents',{settings:{...draft,confirmed:true},expected_revision:draft.revision});if(token!==revision)return;draft=clone(data.settings);rows=data.agents;render();
   let logged=0,unknown=0,failed=0;
   for(const row of rows.filter(r=>r.enabled)){status.textContent='正在检查 '+row.label+' 的安装与登录…';try{const checked=await request('/settings/local-agents/check',{kind:row.kind,revision:draft.revision});if(token!==revision)return;row.inspection=checked.inspection;if(row.inspection.auth?.status==='logged_in')logged++;else if(row.inspection.auth?.status==='not_logged_in'||row.inspection.auth?.status==='error')failed++;else unknown++}catch(e){failed++}}
   render();status.textContent='已保存 · 安装与登录检查完成：'+logged+' 个已登录，'+failed+' 个待处理，'+unknown+' 个暂不支持登录检查。模型需另行选择测试。';window.dispatchEvent(new CustomEvent('agent-connections-changed'));
  }catch(e){if(token===revision)status.textContent=e.message}finally{saving=false;controls()}
 }
 dialog.addEventListener('cancel',e=>{if(saving)e.preventDefault()});
 dialog.addEventListener('close',()=>{if(dialog.open)return;++revision;busy=false;draft=null;editing=null;keyValue='';apiBody.replaceChildren();opener?.focus()});
 window.AgentConnections={open};
})();
