const assert=require('node:assert/strict');
const {dashboard}=require('./dashboard_browser_fixture.cjs');
(async()=>{
 let picks=0,saves=[],mode='ok',saveFail=false,hold=null,release;
 const candidate='C:/团队/Project A';
 const app=await dashboard(async(route,url,req,json)=>{
  if(url.pathname==='/settings/pick-workdir'){
   picks++;if(hold)await hold;
   if(mode==='cancel')await json(route,{cancelled:true});
   else if(mode==='network')await route.abort();
   else if(mode==='error')await json(route,{error:'选择器超时'},500);
   else if(mode==='bad-json')await route.fulfill({status:200,body:'not json'});
   else await json(route,{path:candidate});return true;
  }
  if(url.pathname==='/settings/workdir'){
   const value=req.postDataJSON().path;saves.push(value);
   if(saveFail)await json(route,{error:'保存失败'},500);else await json(route,{workdir:value});return true;
  }
  return false;
 });
 try{
  const p=app.page;assert.equal(picks,0);
  assert.equal(await p.getByRole('button',{name:'选择工作区',exact:true}).count(),1);
  assert.equal(await p.locator('#wk-chip svg.lucide-plus').count(),1);
  assert.equal(await p.locator('#wk-chip').innerText(),'','Workspace action should show only its icon');
  assert.equal(await p.locator('#send').getAttribute('aria-label'),'发送');
  assert.equal(await p.locator('#send svg.lucide-arrow-up').count(),1);
  await p.locator('#wk-chip').click();await p.waitForFunction(path=>S.workdir===path,candidate);
  assert.deepEqual(saves,[candidate]);assert.equal(await p.locator('#wk-name').textContent(),'Project A');
  await p.waitForFunction(()=>!document.querySelector('#wk-chip').disabled);
  mode='cancel';await p.locator('#wk-chip').click();await p.getByText('已取消选择',{exact:true}).waitFor();assert.equal(saves.length,1);
  mode='error';await p.locator('#wk-chip').click();await p.locator('#wk-path').waitFor({state:'visible'});
  await p.locator('#wk-path').fill('C:/另一个/Manual Space');await p.locator('#wk-save').click();await p.waitForFunction(()=>S.workdir==='C:/另一个/Manual Space');
  assert.equal(await p.locator('#wk-path').inputValue(),'');
  mode='network';await p.locator('#wk-chip').click();await p.locator('#wk-path').waitFor({state:'visible'});
  await p.locator('#wk-path').fill('C:/保留/Typing');mode='bad-json';await p.locator('#wk-chip').click();
  assert.equal(await p.locator('#wk-path').inputValue(),'C:/保留/Typing');
  mode='ok';saveFail=true;await p.locator('#wk-chip').click();await p.getByText('保存失败：保存失败',{exact:true}).waitFor();
  assert.equal(await p.locator('#wk-path').inputValue(),candidate,'Native candidate must survive failed save');
  assert.equal(await p.locator('#wk-path').isVisible(),true);assert.equal(await p.evaluate(()=>S.workdir),'C:/另一个/Manual Space');
  saveFail=false;await p.locator('#wk-save').click();await p.waitForFunction(path=>S.workdir===path,candidate);
  hold=new Promise(r=>release=r);const before=picks;
  await p.evaluate(()=>{renderWorkdirChip();document.querySelector('#wk-chip').click();document.querySelector('#wk-chip').click()});
  await p.waitForFunction(()=>document.querySelector('#wk-chip').getAttribute('aria-busy')==='true');
  await p.waitForTimeout(50);assert.equal(picks,before+1);
  assert.equal(await p.locator('#wk-chip .ah-spinner').isVisible(),true);
  assert.equal(await p.locator('#wk-chip .ah-sr-only').count(),1,'Loading should keep an icon-sized action');
  assert.equal(await p.locator('#wk-name').textContent(),'Project A','Loading must retain workspace context');
  assert.equal((await p.locator('#wk-chip').boundingBox()).width,34);
  release();hold=null;
  await p.waitForFunction(()=>!document.querySelector('#wk-chip').disabled);
  assert.match(await p.locator('#wk-chip').getAttribute('title'),/后续提交/);
  assert.equal(await p.locator('#wk-chip svg.lucide-plus').count(),1,'Restore the action icon after loading');
  if(process.env.STUDIO_TEST_OUTPUT){
   const fs=require('node:fs'),path=require('node:path');fs.mkdirSync(process.env.STUDIO_TEST_OUTPUT,{recursive:true});
   await p.screenshot({path:path.join(process.env.STUDIO_TEST_OUTPUT,'modern-actions-desktop.png')});
   await p.setViewportSize({width:390,height:844});
   await p.screenshot({path:path.join(process.env.STUDIO_TEST_OUTPUT,'modern-actions-mobile.png')});
   assert.equal(await p.locator('#wk-chip').isVisible(),true);assert.equal(await p.locator('#send').isVisible(),true);
   assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'Actions must fit the mobile viewport');
  }
  assert.deepEqual(app.errors,[]);assert.deepEqual(app.unexpected,[]);
  console.log('dashboard workdir browser: PASS');
 }finally{await app.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
