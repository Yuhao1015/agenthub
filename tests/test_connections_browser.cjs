// Static product UI only: every API is intercepted, no serve.py or model calls.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
let server,browser;
(async()=>{
 const kinds=['claude_code','codex','codebuddy'], labels=['Claude Code','Codex','WorkBuddy'];
 let settings={version:1,revision:0,confirmed:false,platform:'win32',agents:Object.fromEntries(kinds.map(k=>[k,{enabled:false,paths:{}}]))};
 let checks=0,scans=0,saves=0,failSave=false,failScan=false,hold=null;
 let api={version:1,revision:0,items:{},migration:{}},apiWrites=[],testCalls=0;
 const unexpected=[],errors=[];
 const rows=()=>kinds.map((kind,i)=>({id:kind,kind,label:labels[i],enabled:settings.agents[kind].enabled,inspection:{kind,revision:settings.revision,fingerprint:'fixture',auth:{status:'unchecked'},catalog:{status:'unsupported',models:[]},configured_models:[],tests:{}},available:scans>0,status:scans?'found':null,detail:'<img src=x onerror=alert(1)>',candidates:[{id:'fixture',label:'本机程序',paths:{executable:'C:\\agents\\'+kind+'.exe'}}]}));
 server=http.createServer((req,res)=>{const p=new URL(req.url,'http://localhost').pathname;if(p==='/harness'){res.setHeader('Content-Type','text/html; charset=utf-8');return res.end('<link rel="stylesheet" href="/assets/connections.css"><button onclick="AgentConnections.open({tab:\'local\'})">接入本机 Agent</button><script src="/assets/async-ui.js"></script><script src="/assets/agent-catalog.js"></script><script src="/assets/connections.js"></script>')}
 const file=path.resolve(root,'.'+p);if(!file.startsWith(root+path.sep)){res.writeHead(403);return res.end()}
 fs.readFile(file,(err,data)=>{if(err){res.writeHead(404);return res.end()}res.setHeader('Content-Type',p.endsWith('.js')?'text/javascript':p.endsWith('.css')?'text/css':'text/html');res.end(data)});});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({headless:true,channel:'chrome'});const page=await browser.newPage({viewport:{width:1080,height:820}});
 page.on('pageerror',e=>errors.push(e.message));
 const json=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
 await page.route('**/*',async route=>{const req=route.request(),u=new URL(req.url());
  if(u.origin!==origin){unexpected.push(req.url());return route.abort()}
  if(u.pathname==='/settings/api-connections'){
   if(req.method()==='POST'){const b=req.postDataJSON();apiWrites.push(b);if(failSave)return json(route,{error:'保存失败'},500);if(hold)await hold;const c=b.connection,id=c.id||'api-'+apiWrites.length;api={...api,revision:api.revision+1,items:{...api.items,[id]:{...c,id,agent_id:id,has_key:b.key_action!=='clear',verified:false}}};}
   return json(route,api);
  }
  if(u.pathname==='/settings/api-connections/test'){testCalls++;return json(route,{ok:true,cost_complete:false,cost_usd:0})}
  if(u.pathname==='/settings/api-connections/delete'){const b=req.postDataJSON();delete api.items[b.id];api.revision++;return json(route,api)}
  if(u.pathname==='/settings/local-agents/check'){checks++;const b=req.postDataJSON();assert.equal(b.kind,'codex');assert.equal(settings.agents[b.kind].enabled,true);return json(route,{inspection:{...rows().find(r=>r.kind===b.kind).inspection,auth:{status:'logged_in'}}})}
  if(u.pathname==='/settings/local-agents/discover'){scans++;if(hold)await hold;if(failScan)return route.abort();return json(route,{settings,agents:rows()})}
  if(u.pathname==='/settings/local-agents'){
   if(req.method()==='POST'){saves++;if(failSave)return json(route,{error:'任务执行中，请稍后重试'},409);const body=req.postDataJSON();assert.equal(body.expected_revision,settings.revision);settings={...body.settings,revision:settings.revision+1};}
   return json(route,{settings,agents:rows()});
  }
  if(u.pathname==='/harness'||/^\/assets\/((connections|async-ui|agent-catalog)\.(js|css))$/.test(u.pathname))return route.continue();
  unexpected.push(req.url());return route.abort();
 });
 await page.goto(origin+'/harness');await page.evaluate(()=>window.addEventListener('agent-connections-changed',()=>window.testChanges=(window.testChanges||0)+1));
 const open=()=>page.getByRole('button',{name:'接入本机 Agent',exact:true}).click();
 await open();const dialog=page.getByRole('dialog');await dialog.waitFor();
 assert.equal(scans,0,'Opening must not discover');
 failScan=true;await dialog.getByRole('button',{name:'发现本机 Agent',exact:true}).click();await dialog.getByRole('status').filter({hasText:'fetch'}).waitFor();failScan=false;
 await dialog.getByRole('button',{name:'发现本机 Agent',exact:true}).click();
 await dialog.getByRole('checkbox',{name:'启用 Codex',exact:true}).check();
 assert.equal(saves,0);assert.equal(await dialog.locator('img').count(),0);
 await page.keyboard.press('Escape');assert.equal(await dialog.isVisible(),false);
 assert.equal(await page.getByRole('button',{name:'接入本机 Agent'}).evaluate(el=>el===document.activeElement),true);
 await open();assert.equal(await dialog.getByRole('checkbox',{name:'启用 Codex',exact:true}).isChecked(),false);
 await dialog.getByRole('checkbox',{name:'启用 Codex',exact:true}).check();
 failSave=true;await dialog.getByRole('button',{name:'接入所选',exact:true}).click();
 await dialog.getByRole('status').filter({hasText:'任务执行中'}).waitFor();assert.equal(settings.confirmed,false);
 failSave=false;await dialog.getByRole('button',{name:'接入所选',exact:true}).click();
 await dialog.getByRole('status').filter({hasText:'已保存'}).waitFor();
 assert.equal(checks,1);assert.equal(testCalls,0);assert.equal(settings.agents.codex.enabled,true);assert.equal(settings.agents.claude_code.enabled,false);
 assert.equal(await page.evaluate(()=>window.testChanges),1);
 await dialog.getByRole('checkbox',{name:'启用 Codex',exact:true}).uncheck();await dialog.getByRole('button',{name:'接入所选',exact:true}).click();
 await page.waitForFunction(()=>window.testChanges===2);assert.equal(settings.agents.codex.enabled,false);
 let release;hold=new Promise(r=>release=r);await dialog.getByRole('button',{name:'发现本机 Agent',exact:true}).click();
 assert.equal(await dialog.locator('.ah-connections-toolbar button').first().getAttribute('aria-busy'),'true');
 assert.equal(await dialog.locator('.ah-connections-toolbar button').first().isDisabled(),true);
 await page.keyboard.press('Escape');await open();release();hold=null;await dialog.getByRole('checkbox',{name:'启用 Codex',exact:true}).waitFor();
 await page.setViewportSize({width:360,height:760});assert.equal(await dialog.evaluate(el=>el.scrollWidth<=el.clientWidth),true);
 await page.screenshot({path:path.join(root,'.superpowers/sdd/2026-09-30-agent-setup-ux/connections-mobile.png')});
 await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});await page.evaluate(()=>AgentConnections.open({tab:'api'}));
 await dialog.getByRole('button',{name:'添加 API 连接',exact:true}).click();
 assert.equal(await dialog.getByLabel('接口格式',{exact:true}).locator('option').count(),3);
 const fill=async name=>{await dialog.getByLabel('连接名称',{exact:true}).fill(name);await dialog.getByLabel('接口格式',{exact:true}).selectOption('anthropic');await dialog.getByLabel('Base URL',{exact:true}).fill('https://example.invalid/v1');await dialog.getByLabel('模型 ID',{exact:true}).fill('test-model');await dialog.getByLabel('API Key',{exact:true}).fill('fake-<&"$-secret')};
 await fill('API <img src=x onerror=alert(1)>');
 await dialog.getByRole('button',{name:'取消编辑',exact:true}).click();assert.equal(apiWrites.length,0);
 await dialog.getByRole('button',{name:'添加 API 连接',exact:true}).click();await fill('第一个');
 failSave=true;await dialog.getByRole('button',{name:'保存连接',exact:true}).click();await dialog.getByRole('status').filter({hasText:'保存失败'}).waitFor();assert.equal(await dialog.getByLabel('API Key',{exact:true}).inputValue(),'fake-<&"$-secret');failSave=false;
 await dialog.getByRole('button',{name:'保存连接',exact:true}).click();await dialog.getByRole('button',{name:'添加 API 连接',exact:true}).waitFor();
 await dialog.getByRole('button',{name:'添加 API 连接',exact:true}).click();await fill('API <img src=x onerror=alert(1)>');await dialog.getByRole('button',{name:'保存连接',exact:true}).click();
 await dialog.getByRole('button',{name:'添加 API 连接',exact:true}).waitFor();assert.equal(Object.keys(api.items).length,2);assert.equal(testCalls,0);assert.equal(await dialog.locator('img').count(),0);
 await dialog.getByRole('button',{name:'编辑',exact:true}).first().click();assert.equal(await dialog.getByLabel('API Key',{exact:true}).inputValue(),'');
 await dialog.getByLabel('密钥操作',{exact:true}).selectOption('keep');await dialog.getByRole('button',{name:'保存连接',exact:true}).click();await dialog.getByRole('button',{name:'添加 API 连接',exact:true}).waitFor();assert.equal(apiWrites.at(-1).key_value,'');
 await dialog.getByRole('button',{name:'编辑',exact:true}).first().click();await dialog.getByLabel('启用此连接',{exact:true}).uncheck();await dialog.getByLabel('密钥操作',{exact:true}).selectOption('clear');await dialog.getByRole('button',{name:'保存连接',exact:true}).click();await dialog.getByRole('button',{name:'添加 API 连接',exact:true}).waitFor();assert.equal(apiWrites.at(-1).key_action,'clear');
 await dialog.getByRole('button',{name:'测试连接',exact:true}).last().click();assert.equal(testCalls,0);await dialog.getByRole('button',{name:'确认测试（可能收费）',exact:true}).click();await dialog.getByRole('status').filter({hasText:'费用未知'}).waitFor();assert.equal(testCalls,1);
 assert.equal(await dialog.evaluate(el=>el.scrollWidth<=el.clientWidth),true);await page.screenshot({path:path.join(root,'.superpowers/sdd/2026-09-30-agent-setup-ux/connections-api-mobile.png')});
 await dialog.getByRole('button',{name:'添加 API 连接',exact:true}).click();
 await dialog.getByLabel('接口格式',{exact:true}).selectOption('openrouter');assert.equal(await dialog.getByLabel('Base URL',{exact:true}).inputValue(),'https://openrouter.ai/api/v1');
 await dialog.getByLabel('Base URL',{exact:true}).fill('https://custom.invalid/v1');await dialog.getByLabel('接口格式',{exact:true}).selectOption('openai_compatible');assert.equal(await dialog.getByLabel('Base URL',{exact:true}).inputValue(),'https://custom.invalid/v1');
 await dialog.getByLabel('连接名称',{exact:true}).fill('OpenAI compatible');await dialog.getByLabel('模型 ID',{exact:true}).fill('model');await dialog.getByLabel('API Key',{exact:true}).fill('fake');
 let unblock;hold=new Promise(r=>unblock=r);const count=apiWrites.length;await dialog.getByRole('button',{name:'保存连接',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.ah-connections-form .ah-connections-primary').disabled);
 await page.keyboard.press('Escape');assert.equal(await dialog.isVisible(),true);assert.equal(apiWrites.length,count+1);unblock();hold=null;await dialog.getByRole('button',{name:'添加 API 连接',exact:true}).waitFor();
 await dialog.getByRole('button',{name:'删除',exact:true}).last().click();await dialog.getByRole('button',{name:'确认删除',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.ah-connections-confirm'));
 assert.doesNotMatch(fs.readFileSync(path.join(root,'settings.html'),'utf8'),/id="(?:ds-key|or-key|save-keys)"/);
 assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);console.log('Connection browser checks passed');
})().catch(e=>{console.error(e);process.exitCode=1}).finally(async()=>{await browser?.close();server?.close()});
