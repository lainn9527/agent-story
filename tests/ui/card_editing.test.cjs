const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {execFileSync}=require('node:child_process');
const path=require('node:path');
const root=path.resolve(__dirname,'../..');
const html=execFileSync('python3',['-c',`from jinja2 import Environment,FileSystemLoader,select_autoescape
print(Environment(loader=FileSystemLoader('templates'),autoescape=select_autoescape()).get_template('card_chat.html').render(slug='test',v='test'))`],{cwd:root,encoding:'utf8'});
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CHROME_BIN?{executablePath:process.env.CHROME_BIN}:{})});
 try {
 for(const viewport of [{width:1280,height:900},{width:390,height:844}]) {
 const page=await browser.newPage({viewport});const errors=[],calls=[];page.on('pageerror',e=>errors.push(e.message));
 let active='main', branches=[{id:'main',name:'主線'}],fail=false;
 const messages={main:[{role:'gm',content:'開場',index:0},{role:'user',content:'原玩家輸入',index:1},{role:'gm',content:'原回覆：前半很好，最後一句不對。',index:2},{role:'user',content:'後續',index:3},{role:'gm',content:'後續回覆',index:4}]};
 await page.route('http://story.test/**',async route=>{
  const req=route.request(),url=new URL(req.url()),p=url.pathname;
  if(p.startsWith('/static/'))return route.fulfill({body:readFileSync(path.join(root,p)),contentType:p.endsWith('.css')?'text/css':'text/javascript'});
  if(p==='/card/test')return route.fulfill({body:html,contentType:'text/html'});
  if(p==='/api/config')return route.fulfill({json:{ok:true,provider:'gemini',gemini:{model:'test'}}});
  if(p.endsWith('/state')){
   const bid=url.searchParams.get('branch_id')||active;
   return route.fulfill({json:{ok:true,branch_id:bid,active_branch_id:active,branches,card:{display_name:'小蘭',persona:{},setting:{}},relationship:{tier:'友好'},relationship_tiers:['友好'],clock:{},inventory:{},memories:[],messages:messages[bid]}});
  }
  if(p.endsWith('/edit')){
   const body=req.postDataJSON();calls.push(body);
   if(fail)return route.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({type:'error',message:'模擬失敗'})+'\n\n'});
   const target=body.destination==='fork'?'card_'+calls.length:body.branch_id;
   if(target!==body.branch_id)branches.push({id:target,name:'編輯分支',parent_branch_id:body.branch_id});
   messages[target]=messages[body.branch_id].filter(m=>m.index<body.message_index-1).concat([{role:'user',index:body.message_index-1,content:body.message},{role:'gm',index:body.message_index,content:'修改後回覆'}]);active=target;
   return route.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({type:'text',chunk:'修改後回覆'})+'\n\ndata: '+JSON.stringify({type:'done',result:{ok:true,branch_id:target,narration:'修改後回覆'}})+'\n\n'});
  }
  return route.fulfill({json:{ok:true}});
 });
 await page.goto('http://story.test/card/test');await page.waitForSelector('.message.gm');
 await page.locator('#addon-panel-btn').click();
 await page.locator('#narrativeStyleSelect').selectOption('natural');
 await page.locator('#plotModeSelect').selectOption('coauthor');
 await page.locator('#sceneModeSelect').selectOption('intimate');
 await page.locator('#addon-panel-btn').click();
 await page.locator('[title="編輯此回合"]').first().click();
 assert.equal(await page.locator('#cardEditMessage').inputValue(),'原玩家輸入');
 let box=await page.locator('#cardEditDialog').boundingBox();assert(box.x>=0 && box.x+box.width<=viewport.width);
 await page.screenshot({path:`/tmp/story-edit-${viewport.width}.png`});
 await page.locator('#cardEditInstruction').fill('保留前半，最後改成微酸。');
 await page.locator('#cardEditForm [type=submit]').click();
 await page.waitForURL(/branch_id=card_1/);
 await page.waitForFunction(()=>document.querySelector('#message-list').textContent.includes('修改後回覆'));
 assert.equal(calls[0].mode,'revise');assert.equal(calls[0].destination,'fork');assert.equal(calls[0].branch_id,'main');assert.equal(calls[0].message_index,2);
 assert.equal(messages.main.length,5);
 assert.equal(calls[0].narrative_style,'natural');
 assert.equal(calls[0].plot_mode,'coauthor');
 assert.equal(calls[0].scene_mode,'intimate');
 assert.equal(calls[0].expected_reply,'原回覆：前半很好，最後一句不對。');
 await page.locator('[title="編輯此回合"]').first().click();
 await page.locator('[name=editMode][value=regenerate]').check();await page.locator('[name=editDestination][value=overwrite]').check();
 assert.match(await page.locator('#cardEditNotice').textContent(),/移除其後所有對話/);
 await page.locator('#cardEditMessage').fill('重寫的玩家輸入');await page.locator('#cardEditForm [type=submit]').click();
 await page.waitForFunction(()=>document.querySelector('#message-list').textContent.includes('重寫的玩家輸入'));
 await page.waitForFunction(()=>document.querySelector('[title="編輯此回合"]') && !document.querySelector('[title="編輯此回合"]').disabled);
 assert.equal(calls[1].mode,'regenerate');assert.equal(calls[1].destination,'overwrite');assert.equal(calls[1].branch_id,'card_1');
 fail=true;
 await page.locator('[title="編輯此回合"]').first().click();await page.locator('#cardEditInstruction').fill('失敗後仍保留的要求');
 await page.locator('#cardEditForm [type=submit]').click();
 await page.getByText('生成失敗：模擬失敗',{exact:true}).waitFor();
 // Shared alert dialog is confirmed with its existing OK button.
 await page.locator('#alert-modal-ok').click();
 await page.locator('#cardEditDialog[open]').waitFor();
 assert.equal(await page.locator('#cardEditInstruction').inputValue(),'失敗後仍保留的要求');
 assert.equal(errors.length,0,errors.join('\n'));
 await page.close();
 }
 console.log('card editing desktop/mobile passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
