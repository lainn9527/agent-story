/** Optional ensemble controls; use the same Playwright/Chrome setup as chat_ui.test.cjs. */
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
 const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 let ensemble=false,focus='lead',calls=[],recording='done';
 const cast=[{id:'lead',name:'李小蘭',presence:'present',status:'準備講義',relationship:{tier:'友好',note:'同學'}},{id:'shen',name:'沈晚',presence:'offstage',status:'整理相簿',relationship:{tier:'相識',note:'攝影同好'}}];
 await page.route('http://story.test/**',async route=>{
  const req=route.request(),p=new URL(req.url()).pathname;
  if(p.startsWith('/static/'))return route.fulfill({body:readFileSync(path.join(root,p)),contentType:p.endsWith('.css')?'text/css':'text/javascript'});
  if(p==='/card/test')return route.fulfill({body:html,contentType:'text/html'});
  if(p==='/api/config')return route.fulfill({json:{ok:true,provider:'gemini',gemini:{model:'test'}}});
  if(p.endsWith('/state'))return route.fulfill({json:{ok:true,ensemble,focus,cast,recorder:{status:recording},baseline_index:2,card:{display_name:cast.find(c=>c.id===focus).name,persona:{identity:'成年大學生'},setting:{}},relationship:cast.find(c=>c.id===focus).relationship,relationship_tiers:['相識','友好'],memories:[focus==='lead'?'私下收到好消息':'答應帶藍色相簿'],inventory:{},clock:{},messages:[{role:'gm',content:'開場',index:0},{role:'user',content:'繼續',index:1},{role:'gm',content:'建立副本處',index:2}]}});
  const body=req.postDataJSON();calls.push({p,body});
  if(p.endsWith('/ensemble')){assert.equal(body.character.name,'沈晚');assert.deepEqual(body.character.memories,['答應帶藍色相簿']);ensemble=true;return route.fulfill({json:{ok:true,url:'/card/test'}});}
  if(p.endsWith('/retry-recording'))recording='pending';
  if(p.endsWith('/focus')){focus=body.character_id;cast.find(c=>c.id===focus).presence=body.presence;}
  if(p.endsWith('/add'))cast.push({id:'third',name:body.name,presence:body.presence,status:body.status,relationship:{tier:body.tier,note:body.note}});
  return route.fulfill({json:{ok:true}});
 });
 await page.goto('http://story.test/card/test');await page.waitForSelector('.message.gm');
 await page.locator('#drawer-toggle-btn').click();await page.locator('#ensembleCreateBtn').click();
 await page.locator('#ensembleForm [name=name]').fill('沈晚');await page.locator('#ensembleForm [name=personality]').fill('直率，愛攝影');await page.locator('#ensembleForm [name=memories]').fill('答應帶藍色相簿');
 await page.locator('#ensembleForm [type=submit]').click();
 await page.waitForFunction(()=>document.querySelector('#ensembleControls').hidden===false);
 await page.locator('#drawer-toggle-btn').click();
 assert.equal(await page.locator('[title="重新生成"]').count(),0,'copy baseline cannot regenerate');
 await page.locator('#ensembleFocus').selectOption('shen');
 await page.waitForFunction(()=>document.querySelector('#memoryList').textContent.includes('藍色相簿'));
 assert.equal(await page.locator('#ensemblePresence').inputValue(),'offstage','focus does not teleport actor');
 await page.locator('#ensemblePresence').selectOption('present');
 await page.waitForFunction(()=>document.querySelector('#ensembleCast').textContent.includes('沈晚 · 在場'));
 assert.match(await page.locator('#ensembleCast').innerText(),/李小蘭.*準備講義/);
 await page.locator('#ensembleAddBtn').click();await page.locator('#ensembleForm [name=name]').fill('陳安');await page.locator('#ensembleForm [name=personality]').fill('沉穩，擅長佈展');await page.locator('#ensembleForm [type=submit]').click();
 await page.waitForFunction(()=>document.querySelector('#ensembleFocus').options.length===3);
 await page.locator('#drawer-close-btn').click();
 recording='pending';
 await page.waitForFunction(()=>document.querySelector('#ensembleRecordingText').textContent.includes('可以繼續對話') && !document.querySelector('#ensembleRecording').hidden);
 assert.equal(await page.locator('#send-btn').isDisabled(),false);
 recording='error';
 await page.waitForFunction(()=>!document.querySelector('#ensembleRetryRecording').hidden);
 await page.locator('#ensembleRetryRecording').click();
 await page.waitForFunction(()=>document.querySelector('#ensembleRecordingText').textContent.includes('可以繼續對話'));
 assert(calls.some(c=>c.p.endsWith('/retry-recording')));
 recording='done';
 await page.waitForFunction(()=>document.querySelector('#ensembleRecording').hidden);
 await page.setViewportSize({width:390,height:844});await page.locator('#drawer-toggle-btn').click();await page.locator('#ensembleAddBtn').click();
 assert(await page.locator('#ensembleDialog').isVisible());
 assert(await page.evaluate(()=>document.querySelector('#ensembleDialog').getBoundingClientRect().width<=innerWidth));
 assert.deepEqual(errors,[]);console.log('PASS: isolated-copy form, focus, independent memories, presence, cast, add, baseline guards, mobile dialog');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
