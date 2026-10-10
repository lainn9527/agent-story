/** Recall mode selector; all API calls are intercepted, no player data. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {execFileSync}=require('node:child_process');
const path=require('node:path');
const root=path.resolve(__dirname,'../..');
const html=execFileSync('python3',['-c',"from jinja2 import Environment,FileSystemLoader,select_autoescape; print(Environment(loader=FileSystemLoader('templates'),autoescape=select_autoescape()).get_template('card_chat.html').render(slug='test',v='test',recall_available=True))"],{cwd:root,encoding:'utf8'});
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CHROME_BIN?{executablePath:process.env.CHROME_BIN}:{})});
 try {
  const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[],calls=[];
  page.on('pageerror',e=>errors.push(e.message));
  let messages=[{role:'gm',content:'你好',index:0}];
  await page.route('http://story.test/**',async route=>{
   const req=route.request(),p=new URL(req.url()).pathname;
   if(p.startsWith('/static/'))return route.fulfill({body:readFileSync(path.join(root,p)),contentType:p.endsWith('.css')?'text/css':'text/javascript'});
   if(p==='/card/test')return route.fulfill({body:html,contentType:'text/html'});
   if(p==='/api/config')return route.fulfill({json:{ok:true,provider:'gemini',gemini:{model:'test'}}});
   if(p.endsWith('/state'))return route.fulfill({json:{ok:true,card:{display_name:'小蘭',persona:{},setting:{}},relationship:{tier:'友好'},relationship_tiers:['友好'],memories:[],clock:{},inventory:{},messages}});
   if(p.endsWith('/send/stream')||p.endsWith('/regenerate')){
    const body=req.postDataJSON();calls.push({p,body});
    if(p.endsWith('/regenerate'))messages.pop();else messages.push({role:'user',content:body.message,index:1});
    messages.push({role:'gm',content:'記得那把雨傘。',index:2});
    return route.fulfill({contentType:'text/event-stream',body:'data: {"type":"text","chunk":"記得那把雨傘。"}\n\ndata: {"type":"done","result":{"ok":true,"narration":"記得那把雨傘。"}}\n\n'});
   }
   throw new Error('Unexpected request '+p);
  });
  await page.goto('http://story.test/card/test');await page.waitForSelector('.message.gm');
  assert.equal(await page.locator('#recallModeSelect').inputValue(),'off');
  assert.equal(await page.locator('#narrativeStyleSelect').inputValue(),'original');
  await page.locator('#addon-panel-btn').click();
  await page.locator('#recallModeSelect').selectOption('deep');
  await page.locator('#narrativeStyleSelect').selectOption('natural');
 await page.locator('#plotModeSelect').selectOption('coauthor');
 await page.locator('#sceneModeSelect').selectOption('intimate');
  await page.reload();await page.waitForSelector('.message.gm');
  assert.equal(await page.locator('#recallModeSelect').inputValue(),'deep');
  assert.equal(await page.locator('#narrativeStyleSelect').inputValue(),'natural');
 assert.equal(await page.locator('#plotModeSelect').inputValue(),'coauthor');
 assert.equal(await page.locator('#sceneModeSelect').inputValue(),'intimate');
  await page.locator('#addon-panel-btn').click();
  await page.screenshot({path:'/tmp/card-recall-mobile.png'});
  assert(await page.locator('#recallModeSelect').evaluate(e=>e.getBoundingClientRect().right<=innerWidth));
  await page.locator('#addon-panel-btn').click();
  await page.locator('#user-input').fill('你還記得初次見面嗎？');await page.locator('#send-btn').click();
  await page.waitForFunction(()=>document.querySelectorAll('.message').length===3&&!document.querySelector('#send-btn').disabled);
  assert.equal(calls[0].body.recall_mode,'deep');
  assert.equal(calls[0].body.narrative_style,'natural');
 assert.equal(calls[0].body.plot_mode,'coauthor');
 assert.equal(calls[0].body.scene_mode,'intimate');
  await page.locator('#addon-panel-btn').click();await page.locator('#recallModeSelect').selectOption('auto');await page.locator('#narrativeStyleSelect').selectOption('original');await page.locator('#addon-panel-btn').click();
  await page.locator('[title="重新生成"]').click();await page.locator('#confirm-modal-ok').click();
  await page.waitForFunction(()=>!document.querySelector('#send-btn').disabled);
  assert.equal(calls.at(-1).body.recall_mode,'auto');assert.equal(calls.at(-1).body.stream,true);
  assert.equal(calls.at(-1).body.narrative_style,'original');
  assert.deepEqual(errors,[]);
  console.log('PASS: default off, per-card persistence, send deep, regenerate auto, mobile selector');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
