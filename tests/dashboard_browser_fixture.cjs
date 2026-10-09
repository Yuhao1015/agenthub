const fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
async function dashboard(routeHandler){
 const server=http.createServer((req,res)=>{
  const name=new URL(req.url,'http://localhost').pathname;
  const target=path.resolve(root,'.'+(name==='/'?'/dashboard.html':name));
  if(!target.startsWith(root+path.sep)){res.writeHead(403).end();return}
  fs.readFile(target,(err,data)=>{if(err){res.writeHead(404).end();return}res.setHeader('Content-Type',target.endsWith('.js')?'text/javascript':target.endsWith('.css')?'text/css':'text/html');res.end(data)});
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const origin='http://127.0.0.1:'+server.address().port;
 const browser=await chromium.launch({headless:true,channel:'chrome'}),page=await browser.newPage({viewport:{width:1536,height:960}});
 page.setDefaultTimeout(6000);await page.emulateMedia({reducedMotion:'reduce'});
 const errors=[],unexpected=[];page.on('pageerror',e=>errors.push(e.message));
 const json=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
 await page.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());
  if(url.origin!==origin){unexpected.push(req.url());return route.abort()}
  if(await routeHandler(route,url,req,json))return;
  if(url.pathname==='/settings')return json(route,{ui_prefs:{manager_agent:'',assign_mode:'auto',gate_mode:'auto'},workdir:'C:/初始/Workspace',budget:{max_usd_per_run:2}});
  if(url.pathname==='/agents')return json(route,{agents:[]});
  if(url.pathname==='/chat/session/current')return json(route,{active:null,run:null});
  if(url.pathname==='/events.jsonl')return json(route,{},404);
  if(url.pathname==='/goals')return json(route,{revision:0,items:[]});
  if(url.pathname==='/settings/config')return json(route,{ok:true});
  if(url.pathname.startsWith('/assets/')||url.pathname==='/'||url.pathname==='/dashboard.html')return route.continue();
  unexpected.push(req.method()+' '+url.pathname);return route.abort();
 });
 await page.goto(origin);await page.waitForFunction(()=>!S.restoringSessions&&S.workdir);
 return {page,errors,unexpected,json,close:async()=>{await browser.close();await new Promise(r=>server.close(r))}};
}
module.exports={dashboard};
