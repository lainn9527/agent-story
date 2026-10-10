const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {execFileSync}=require('node:child_process');
const path=require('node:path');
const root=path.resolve(__dirname,'../..');
const html=execFileSync('python3',['-c',`from jinja2 import Environment,FileSystemLoader
print(Environment(loader=FileSystemLoader('templates')).get_template('card_chat.html').render(slug='test',v='test'))`],{cwd:root,encoding:'utf8'});
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_BIN});
 try {
 for(const viewport of [{width:1280,height:900},{width:390,height:844}]) {
 const page=await browser.newPage({viewport});const errors=[],calls=[];page.on('pageerror',e=>errors.push(e.message));
 let fail=false, memories=['最早記憶','中間記憶','最新記憶'];
 const payload=()=>({ok:true,revision:'r1',characters:[{id:'0',name:'小蘭',memories:memories.map((text,index)=>({text,index}))}]});
 await page.route('http://story.test/**',async route=>{
  const req=route.request(),p=new URL(req.url()).pathname;
  if(p.startsWith('/static/'))return route.fulfill({body:readFileSync(path.join(root,p)),contentType:p.endsWith('.css')?'text/css':'text/javascript'});
  if(p==='/card/test')return route.fulfill({body:html,contentType:'text/html'});
  if(p.endsWith('/memories')){
   if(req.method()==='POST'){
    calls.push(req.postDataJSON());
    if(fail)return route.fulfill({status:409,json:{ok:false,error:'記憶已更新'}});
    const edits=new Map(calls.at(-1).changes.map(c=>[c.index,c.text]));
    memories=memories.map((m,i)=>edits.has(i)?edits.get(i):m).filter(m=>m!==null);
   }
   return route.fulfill({json:payload()});
  }
  if(p.endsWith('/state'))return route.fulfill({json:{ok:true,branch_id:'main',branches:[{id:'main',name:'主線'}],card:{display_name:'小蘭',persona:{},setting:{}},relationship:{tier:'相識'},relationship_tiers:['相識'],clock:{},inventory:{},memories,messages:[{index:0,role:'gm',content:'開場'}]}});
  return route.fulfill({json:{ok:true}});
 });
 await page.goto('http://story.test/card/test');await page.waitForSelector('.message.gm');
 await page.click('#cardMemoryOpen');await page.waitForSelector('.memory-editor-row textarea');
 assert.deepEqual(await page.locator('.memory-editor-row textarea').evaluateAll(nodes=>nodes.map(n=>n.value)),['最新記憶','中間記憶','最早記憶']);
 await page.locator('.memory-editor-row textarea').first().fill('修改最新');
 await page.getByRole('button',{name:'刪除記憶 2',exact:true}).click();
 assert.equal(calls.length,0);
 await page.click('#cardMemoryClose');await page.click('#cardMemoryDiscard [value=cancel]');
 assert.equal(await page.locator('.memory-editor-row textarea').first().inputValue(),'修改最新');
 fail=true;await page.click('#cardMemorySave');await page.waitForFunction(()=>document.querySelector('#cardMemoryError').textContent.includes('記憶已更新'));
 assert.equal(await page.locator('.memory-editor-row textarea').first().inputValue(),'修改最新');
 fail=false;await page.click('#cardMemorySave');await page.waitForFunction(()=>document.querySelector('#cardMemoryError').textContent.includes('已保存'));
 assert.deepEqual(memories,['最早記憶','修改最新']);assert.equal(calls[1].branch_id,'main');
 const box=await page.locator('#cardMemoryDialog').boundingBox();assert(box.x>=0 && box.x+box.width<=viewport.width);
 await page.screenshot({path:`/tmp/story-memory-${viewport.width}.png`});
 await page.click('#cardMemoryClose');await page.click('#cardMemoryOpen');await page.waitForSelector('.memory-editor-row textarea');
 assert.equal(await page.locator('.memory-editor-row textarea').first().inputValue(),'修改最新');
 assert.deepEqual(errors,[]);await page.close();
 }
 } finally {await browser.close();}
 console.log('memory editor desktop/mobile passed');
})().catch(e=>{console.error(e);process.exit(1)});
