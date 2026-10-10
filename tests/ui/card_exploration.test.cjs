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
 try{
  for(const viewport of [{width:1280,height:900},{width:390,height:844}]){
   const page=await browser.newPage({viewport}),posts=[],errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   const materials=[{id:'old',size:'small',title:'舊物',seed:'一件委託裡混入用途不明的舊物。'},
                    {id:'visit',size:'large',title:'魔潮',seed:'魔潮侵境，宗門徵調弟子。'}];
   let enabled=false,fail=false,release;
   const messages=[{role:'gm',index:0,content:'雨夜的客棧。'}];
   await page.route('http://story.test/**',async route=>{
    const req=route.request(),p=new URL(req.url()).pathname;
    if(p.startsWith('/static/'))return route.fulfill({body:readFileSync(path.join(root,p)),contentType:p.endsWith('.css')?'text/css':'text/javascript'});
    if(p==='/card/test')return route.fulfill({body:html,contentType:'text/html'});
    if(p==='/api/config')return route.fulfill({json:{ok:true,provider:'gemini',gemini:{model:'test'}}});
    if(p.endsWith('/state'))return route.fulfill({json:{ok:true,branch_id:'main',branches:[{id:'main',name:'主線'}],card:{display_name:'小蘭',persona:{},setting:{}},relationship:{tier:'相識'},relationship_tiers:['相識'],clock:{},inventory:{},memories:[],messages,exploration_materials:enabled?materials:[]}});
    if(p.endsWith('/send/stream')){
     const body=req.postDataJSON();posts.push(body);
     if(fail){fail=false;return route.fulfill({contentType:'text/event-stream',body:'data: {"type":"done","result":{"ok":false,"error":"試驗失敗"}}\n\n'});}
     await new Promise(resolve=>{release=resolve;});
     messages.push({role:'user',index:messages.length,content:body.message},{role:'gm',index:messages.length+1,content:'有人敲門。'});
     return route.fulfill({contentType:'text/event-stream',body:'data: {"type":"text","chunk":"有人敲門。"}\n\ndata: {"type":"done","result":{"ok":true}}\n\n'});
    }
    return route.fulfill({json:{ok:true}});
   });
   await page.goto('http://story.test/card/test');
   await page.waitForSelector('.message.gm');
   assert(await page.locator('#cardExploreBtn').isHidden(),'no bank, no button');
   enabled=true;
   await page.reload();
   await page.waitForSelector('#cardExploreBtn:enabled');
   const controls=await page.locator('.card-compose-shortcuts > *:not([hidden])').evaluateAll(es=>es.map(e=>e.getBoundingClientRect().top));
   assert(controls.every(y=>Math.abs(y-controls[0])<2),'all shortcuts stay on one line');
   const layout=await page.evaluate(()=>{
    const box=id=>{const r=document.getElementById(id).getBoundingClientRect();return {x:r.x,width:r.width,top:r.top,bottom:r.bottom};};
    const row=document.querySelector('.card-compose-shortcuts');
    return {clear:box('cardClearDraft'),gear:box('addon-panel-btn'),clearInRow:row.contains(document.getElementById('cardClearDraft')),gearInRow:row.contains(document.getElementById('addon-panel-btn')),fits:row.scrollWidth<=row.clientWidth};
   });
   assert(layout.gearInRow && layout.fits,'settings share the shortcut row without horizontal scrolling');
   assert(layout.clearInRow && layout.clear.x>=layout.gear.x+layout.gear.width,'clear sits immediately right of settings');
   assert(Math.abs(layout.clear.top-layout.gear.top)<2,'clear and settings share one row');
   await page.locator('#user-input').fill('艾迪想先了解坊市，不想接宗門任務。');
   await page.locator('#cardResponseLength [data-length="short"]').click();
   await page.locator('#cardExploreSize').selectOption('large');
   assert.equal(await page.locator('#cardExploreBtn').textContent(),'探索·大');
   assert.equal(posts.length,0,'choosing scale never sends');
   await page.locator('#cardExploreBtn').click();
   assert.equal(posts.length,0,'explore only prepares a draft');
   const draft=await page.locator('#user-input').inputValue();
   assert.equal(draft,materials[1].title+'：'+materials[1].seed+'\n\n艾迪想先了解坊市，不想接宗門任務。');
   const preview=await page.locator('#user-input').evaluate(e=>({scroll:e.scrollTop,cursor:e.selectionStart,focused:document.activeElement===e}));
   assert.equal(preview.scroll,0,'preview starts at the event, not the prompt tail');
   assert.equal(preview.cursor,0);
   assert.equal(preview.focused,false,'preview does not summon the mobile keyboard');
   assert(draft.includes(materials[1].seed));assert(!draft.includes(materials[0].seed));
   assert(draft.includes('艾迪想先了解坊市'));
   await page.locator('#cardExploreBtn').click();
   assert.equal(await page.locator('#user-input').inputValue(),draft,'reroll preserves original direction without nesting');

   await page.locator('#cardExploreBtn').click();
   await page.locator('#send-btn').click();
   await page.waitForFunction(()=>document.querySelector('#cardExploreBtn').disabled);
   assert.equal(posts.length,1);
   assert.equal(posts[0].branch_id,'main');
   assert.equal(posts[0].response_length,'short');
   assert.equal(posts[0].message,draft);
   assert.equal(materials.filter(m=>posts[0].message.includes(m.seed)).length,1);
   assert.equal(await page.locator('#user-input').inputValue(),'');
   assert(await page.locator('#cardContinueBtn').isDisabled());
   release();
   await page.waitForFunction(()=>!document.querySelector('#cardExploreBtn').disabled);
   await page.locator('#cardExploreSize').selectOption('small');
   await page.locator('#cardExploreBtn').click();
   const smallDraft=await page.locator('#user-input').inputValue();
   assert(materials.some(m=>m.size==='small' && smallDraft===m.title+'：'+m.seed),'small drafts also contain only the event');
   await page.locator('#send-btn').click();
   await page.waitForFunction(()=>document.querySelector('#cardExploreBtn').disabled);
   const firstSeed=materials.find(m=>posts[0].message.includes(m.seed));
   assert(!posts[1].message.includes(firstSeed.seed),'successive explore avoids the same seed');
   release();
   await page.waitForFunction(()=>!document.querySelector('#cardExploreBtn').disabled);
   await page.locator('#cardContinueBtn').click();
   await page.waitForFunction(()=>document.querySelector('#cardContinueBtn').disabled);
   await page.locator('#cardContinueBtn').dispatchEvent('click');
   assert.equal(posts.length,3,'continue sends immediately, busy click cannot send twice');
   assert.equal(await page.locator('#user-input').inputValue(),'');
   assert.equal(posts[2].message,'（請繼續發展）','continue does not inject another seed');
   release();
   await page.waitForFunction(()=>!document.querySelector('#cardExploreBtn').disabled);
   fail=true;
   await page.locator('#user-input').fill('保留這個方向');
   await page.locator('#cardExploreBtn').click();
   await page.locator('#send-btn').click();
   await page.waitForSelector('#alert-modal:not(.hidden)');
   assert.equal(await page.locator('#user-input').inputValue(),posts[3].message,'failed seed and direction retained for retry');
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'no horizontal overflow');
   await page.reload();
   await page.waitForSelector('#cardExploreBtn:enabled');
   assert.equal(await page.locator('#cardExploreSize').inputValue(),'small','scale persists across reloads');
   await page.locator('#cardExploreBtn').click();
   const countBeforeClear=posts.length;
   await page.locator('#cardClearDraft').click();
   assert.equal(await page.locator('#user-input').inputValue(),'');
   assert(await page.locator('#cardClearDraft').isDisabled());
   assert.equal(posts.length,countBeforeClear,'clearing a draft never sends');
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'clear button fits mobile');
   assert.deepEqual(errors,[]);
   await page.close();
  }
  console.log('PASS: exploration opt-in, one seed, author direction, busy state, failure recovery, continue drafts and scale persistence, desktop/mobile');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
