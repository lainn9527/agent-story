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
 const page=await browser.newPage();const errors=[],calls=[];page.on('pageerror',e=>errors.push(e.message));
 let active='main',branches=[{id:'main',name:'主線'}];
 const messages={main:[{role:'gm',content:'共同開場',index:0},{role:'user',content:'原路線',index:1},{role:'gm',content:'原路線結局',index:2}]};
 await page.route('http://story.test/**',async route=>{
  const req=route.request(),url=new URL(req.url()),p=url.pathname;
  if(p.startsWith('/static/'))return route.fulfill({body:readFileSync(path.join(root,p)),contentType:p.endsWith('.css')?'text/css':'text/javascript'});
  if(p==='/card/test')return route.fulfill({body:html,contentType:'text/html'});
  if(p==='/api/config')return route.fulfill({json:{ok:true,provider:'gemini',gemini:{model:'test'}}});
  if(p.endsWith('/state')){
   const bid=url.searchParams.get('branch_id')||active;
   return route.fulfill({json:{ok:true,branch_id:bid,active_branch_id:active,branches,card:{display_name:'小蘭',persona:{},setting:{}},relationship:{tier:'友好'},relationship_tiers:['友好'],clock:{},inventory:{},memories:[bid==='main'?'原路線记憶':'分支记憶'],messages:messages[bid]}});
  }
  const body=req.postDataJSON();calls.push({p,body});
  if(p.endsWith('/branches/fork')){assert.equal(body.branch_id,'main');assert.equal(body.message_index,0);active='card_b';branches.push({id:active,name:body.name,parent_branch_id:'main',branch_point_index:0});messages.card_b=[messages.main[0]];}
  if(p.endsWith('/branches/switch'))active=body.branch_id;
  if(p.endsWith('/branches/rename'))branches.find(b=>b.id===body.branch_id).name=body.name;
  if(p.endsWith('/send/stream')){
   assert.equal(body.branch_id,'card_b');messages.card_b.push({role:'user',index:1,content:body.message},{role:'gm',index:2,content:'分支的回覆'});
   return route.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({type:'text',chunk:'分支的回覆'})+'\n\ndata: '+JSON.stringify({type:'done',result:{ok:true,narration:'分支的回覆'}})+'\n\n'});
  }
  return route.fulfill({json:{ok:true,branch_id:active,branches}});
 });
 await page.goto('http://story.test/card/test');await page.waitForSelector('.message.gm');
 await page.locator('[title="從此處建立分支，保留原路線"]').first().click();
 await page.locator('#cardBranchInput').fill('試另一個選擇');await page.locator('#cardBranchForm [type=submit]').click();
 await page.waitForFunction(()=>document.querySelectorAll('.message').length===1 && document.querySelector('#cardBranchName').textContent==='試另一個選擇');
 assert.match(page.url(),/branch_id=card_b/);
 await page.locator('#user-input').fill('新的行動');await page.locator('#send-btn').click();
 await page.waitForFunction(()=>document.querySelector('#message-list').textContent.includes('分支的回覆'));
 await page.locator('#drawer-toggle-btn').click();await page.locator('#cardBranchSelect').selectOption('main');
 await page.waitForFunction(()=>document.querySelector('#message-list').textContent.includes('原路線結局'));
 assert(!(await page.locator('#message-list').textContent()).includes('分支的回覆'));
 await page.locator('#cardBranchSelect').selectOption('card_b');
 await page.waitForFunction(()=>document.querySelector('#message-list').textContent.includes('分支的回覆'));
 await page.locator('#cardBranchRename').click();await page.locator('#cardBranchInput').fill('沈晚線');await page.locator('#cardBranchForm [type=submit]').click();
 await page.waitForFunction(()=>document.querySelector('#cardBranchName').textContent==='沈晚線');
 // Another tab's active pointer does not silently move this pinned tab.
 active='main';await page.waitForTimeout(2200);
 assert.equal(await page.locator('#cardBranchSelect').inputValue(),'card_b');
 await page.reload();await page.waitForFunction(()=>document.querySelector('#message-list').textContent.includes('分支的回覆'));
 await page.setViewportSize({width:390,height:844});await page.locator('#drawer-toggle-btn').click();await page.locator('#cardBranchRename').click();
 assert(await page.evaluate(()=>document.querySelector('#cardBranchDialog').getBoundingClientRect().width<=innerWidth));
 assert(calls.some(c=>c.p.endsWith('/send/stream')&&c.body.branch_id==='card_b'));
 assert.deepEqual(errors,[]);console.log('PASS: fork at historical message, independent switching, branch-pinned send/poll/reload, rename, mobile');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
