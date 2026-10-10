/** Browser regression checks. Run with NODE_PATH pointing at an installed playwright package. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const { readFileSync, mkdirSync } = require('node:fs');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const templates = JSON.parse(execFileSync('python3', ['-c', `
import json
from jinja2 import Environment, FileSystemLoader, select_autoescape
env = Environment(loader=FileSystemLoader('templates'), autoescape=select_autoescape())
print(json.dumps({name: env.get_template(name).render(slug='test', v='test') for name in ('index.html', 'card_chat.html', 'card_index.html')}))
`], { cwd: root, encoding: 'utf8' }));
const output = process.env.UI_SCREENSHOTS || '/tmp/story-ui-screenshots';
mkdirSync(output, { recursive: true });

(async () => {
 const browser = await chromium.launch({headless: true, ...(process.env.CHROME_BIN ? {executablePath: process.env.CHROME_BIN} : {})});
 try {
 const page = await browser.newPage({viewport: {width: 1280, height: 900}});
 const errors = []; page.on('pageerror', e => errors.push(e.message));
 let messages = [{role:'gm', content:'📅 2026/05/16 ⏰ 02:08 📌 圖書館 🌤️ 陰\n\n**你好**（抬頭）', index:0}];
 let memory = [], calls = [], failNext = false;
 const cfg = {ok:true, provider:'gemini', gemini:{model:'custom-current-model'}};
 await page.route('http://story.test/**', async route => {
  const req = route.request(), url = new URL(req.url()), pathname = url.pathname;
  if (pathname.startsWith('/static/')) return route.fulfill({body:readFileSync(path.join(root, pathname)), contentType: pathname.endsWith('.css') ? 'text/css' : 'text/javascript'});
  if (pathname === '/card/test' || pathname === '/' || pathname === '/card') return route.fulfill({contentType:'text/html', body:templates[pathname === '/' ? 'index.html' : pathname === '/card' ? 'card_index.html' : 'card_chat.html']});
  let data = {ok:true};
  if (pathname === '/api/config') data = cfg;
  else if (pathname === '/api/card/test/memories') data = {ok:true,revision:'test',characters:[{id:'0',name:'測試角色',memories:memory.map((text,index)=>({text,index}))}]};
  else if (pathname === '/api/card/list') data = {ok:true,cards:[{id:'test',name:'測試角色',pitch:'圖書館的相遇'}]};
  else if (pathname.endsWith('/state')) data = {ok:true,branch_id:'main',branches:[{id:'main',name:'主線'}],card:{display_name:'測試角色',persona:{identity:'圖書館員'},setting:{place:'圖書館'}},relationship:{tier:'信任',note:'記得你的約定'},relationship_tiers:['陌生','信任','親密'],memories:memory,inventory:{書:'拿著'},clock:{time_str:'2026/05/16 02:08',weather:'陰'},messages};
  else if (pathname.endsWith('/send/stream') || pathname.endsWith('/regenerate')) {
   const body = req.postDataJSON(); calls.push({pathname,body});
   if (failNext) {failNext=false; return route.fulfill({contentType:'text/event-stream',body:'data: {"type":"done","result":{"ok":false,"error":"模型暫時無法使用"}}\n\n'});}
   if (pathname.endsWith('/regenerate')) {assert.equal(body.stream,true); messages.pop();}
   else messages.push({role:'user',content:body.message,index:messages.length ? messages.at(-1).index+1 : 1});
   const text = '📅 2026/05/16 ⏰ 02:09 📌 圖書館 🌤️ 陰\n\n**串流回覆**';
   messages.push({role:'gm',content:text,index:messages.at(-1).index+1});
   return route.fulfill({contentType:'text/event-stream',body:`data: ${JSON.stringify({type:'text',chunk:text})}\n\ndata: ${JSON.stringify({type:'done',result:{ok:true,narration:text}})}\n\n`});
  } else if (pathname.endsWith('/rewind')) {calls.push({pathname,body:req.postDataJSON()}); messages=messages.filter(m=>m.index<=req.postDataJSON().to_index);}
  else if (pathname.endsWith('/reset')) {messages=messages.slice(0,1); memory=[];}
  else if (pathname === '/api/init') data = {ok:true,active_branch_id:'main',active_story_id:'world',character_schema:{}};
  else if (pathname === '/api/stories') data = {ok:true,stories:{world:{id:'world',name:'模擬世界'}},active_story_id:'world'};
  else if (pathname === '/api/branches') data = {ok:true,branches:{main:{id:'main',name:'主線'}},active_branch_id:'main'};
  else if (pathname === '/api/messages') data = {ok:true,messages,total:messages.length,sibling_groups:{}};
  else if (pathname === '/api/npcs') data = {ok:true,npcs:[]};
  else if (pathname === '/api/events') data = {ok:true,events:[]};
  else if (pathname === '/api/saves') data = {ok:true,saves:[]};
  else if (pathname === '/api/auto-play/summaries') data = {ok:true,summaries:[]};
  return route.fulfill({json:data});
 });
 await page.goto('http://story.test/card/test');
 await page.waitForSelector('.message.gm');
 assert.equal(await page.locator('.msg-action-btn[title="重新生成"]').count(),0,'opening cannot regenerate');
 assert.equal(await page.locator('#modelSelect').inputValue(),'gemini|custom-current-model');
 await page.locator('#user-input').fill('你好');
 await page.locator('#user-input').press('Enter');
 assert.equal(calls.length,0,'Enter inserts a newline');
 await page.evaluate(() => {
  const originalFetch = window.fetch;
  window.fetch = async (...args) => {
   const response = await originalFetch(...args);
   if (!String(args[0]).endsWith('/send/stream')) return response;
   const text = await response.text(), boundary = text.indexOf('data: {"type":"done"');
   window.fetch = originalFetch;
   return new Response(new ReadableStream({start(controller) {
    controller.enqueue(new TextEncoder().encode(text.slice(0, boundary)));
    window.finishCardStream = () => { controller.enqueue(new TextEncoder().encode(text.slice(boundary))); controller.close(); };
   }}), {headers: {'Content-Type':'text/event-stream'}});
  };
 });
 await page.locator('#user-input').press('Control+Enter');
 await page.waitForFunction(()=>document.querySelector('.message.gm:last-child .content')?.textContent.includes('串流回覆'));
 assert(await page.locator('#send-btn').isDisabled(), 'text is visible before done');
 await page.evaluate(()=>window.finishCardStream());
 await page.waitForFunction(()=>document.querySelectorAll('.message').length===3 && !document.querySelector('#send-btn').disabled);
 assert.equal(calls.length,1);
 assert.match(await page.locator('.message.gm').last().innerText(),/串流回覆/);
 await page.locator('[title="重新生成"]').click();
 await page.locator('#confirm-modal-ok').click();
 await page.waitForFunction(()=>!document.querySelector('#send-btn').disabled);
 assert.equal(calls.at(-1).body.stream,true);
 assert.equal(await page.locator('.message').count(),3);
 await page.locator('#drawer-toggle-btn').click();
 assert.equal(await page.locator('.ladder-step').count(),3,'use server relationship tiers');
 memory=['稍後才完成的長期記憶'];
 await page.locator('#drawer-close-btn').click();
 await page.locator('#cardMemoryOpen').click();
 await page.waitForFunction(()=>document.querySelector('#cardMemoryRows textarea')?.value.includes('稍後才完成'));
 await page.locator('#cardMemoryClose').click();
 await page.locator('#drawer-toggle-btn').click();
 await page.screenshot({path:path.join(output,'card-desktop.png')});
 await page.locator('#drawer-close-btn').click();
 // Non-contiguous authoritative indices must survive round trips.
 messages=[{role:'gm',content:'開場',index:0},{role:'user',content:'問候',index:7},{role:'gm',content:'回答',index:12}];
 await page.waitForSelector('.message[data-index="12"]');
 await page.locator('.message[data-index="12"] [title^="回到"]').click();
 assert.match(await page.locator('#confirm-modal-msg').innerText(),/#12/);
 await page.locator('#confirm-modal-ok').click();
 await page.waitForFunction(()=>!document.querySelector('#send-btn').disabled);
 assert.equal(calls.at(-1).body.to_index,12);
 failNext=true; await page.locator('#user-input').fill('保留失敗的輸入'); await page.locator('#send-btn').click();
 await page.waitForSelector('#alert-modal:not(.hidden)');
 assert.match(await page.locator('#alert-modal-msg').innerText(),/模型暫時無法使用/);
 assert.equal(await page.locator('#user-input').inputValue(),'保留失敗的輸入');
 await page.locator('#alert-modal-ok').click();
 await page.locator('#reset-btn').click(); await page.locator('#confirm-modal-ok').click();
 await page.waitForFunction(()=>document.querySelectorAll('.message').length===1 && !document.querySelector('#send-btn').disabled);
 // A late recorder refresh must not scroll someone reading old messages.
 messages=Array.from({length:50},(_,i)=>({role:i%2?'user':'gm',index:i,content:'一段足夠長的歷史對話。'.repeat(12)}));
 await page.waitForSelector('.message[data-index="49"]');
 await page.locator('#messages').evaluate(el=>el.scrollTop=150);
 memory=['刷新後的新記憶'];
 await page.waitForResponse(async response => new URL(response.url()).pathname.endsWith('/state') &&
  (await response.json()).memories?.includes('刷新後的新記憶'));
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 assert.equal(await page.locator('#messages').evaluate(el=>el.scrollTop),150);
 await page.setViewportSize({width:390,height:844});
 assert.equal(await page.locator('#navCharShort').innerText(),'測試角色');
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile has no horizontal overflow');
 await page.locator('#addon-panel-btn').click();
 assert(await page.locator('#modelSelect').isVisible());
 await page.screenshot({path:path.join(output,'card-mobile.png')});
 await page.goto('http://story.test/card'); await page.waitForSelector('.card-tile');
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'card wall fits mobile');
 await page.screenshot({path:path.join(output,'card-wall-mobile.png')});
 await page.setViewportSize({width:1280,height:900});
 await page.goto('http://story.test/');
 await page.waitForFunction(()=>!document.querySelector('#init-overlay'));
 assert.equal(await page.locator('.message').count(),50,'world adapter still renders shared messages');
 await page.screenshot({path:path.join(output,'world-desktop.png')});
 assert.deepEqual(errors,[]);
 // Exercise real shared transport with fragmented UTF-8 and an unterminated last line.
 const transport = await page.evaluate(async()=>{
  const original=window.fetch;
  const results=[];
  try {
   for (const wire of ['data: {"type":"text","chunk":"你好"}\n\ndata: {"type":"done"}', 'data: {"type":"text","chunk":"部分"}\n\n']) {
    const bytes=new TextEncoder().encode(wire); let i=0;
    window.fetch=async()=>new Response(new ReadableStream({pull(c){if(i<bytes.length)c.enqueue(bytes.slice(i,i+=1));else c.close();}}));
    const events=[]; await streamFromSSE('/test',{},v=>events.push(v),()=>events.push('done'),v=>events.push(v)); results.push(events);
   }
  } finally {window.fetch=original;}
  return results;
 });
 assert.deepEqual(transport,[['你好','done'],['部分','連線中斷']]);
 console.log('PASS: card send/regenerate/rewind/reset/errors, background refresh, mobile layout, world compatibility, fragmented SSE');
 } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
