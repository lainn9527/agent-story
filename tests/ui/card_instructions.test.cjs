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
   const page=await browser.newPage({viewport});const errors=[],posts=[];page.on('pageerror',e=>errors.push(e.message));
   let revision=0,failSave=false,holdSave=null,releaseSave;
   const configs={main:{enabled:false,text:'',revision:null},child:{enabled:false,text:'子線原有設定',revision:'child0'}};
   const messages={main:[{role:'gm',content:'小蘭抬頭看你。',index:0}],child:[{role:'gm',content:'子線開場。',index:0}]};
   const versions={main:[],child:[]};
   function nextConfig(bid,text,enabled){return configs[bid]={text,enabled,revision:'r'+(++revision)};}
   await page.route('http://story.test/**',async route=>{
    const req=route.request(),url=new URL(req.url()),p=url.pathname;
    if(p.startsWith('/static/'))return route.fulfill({body:readFileSync(path.join(root,p)),contentType:p.endsWith('.css')?'text/css':'text/javascript'});
    if(p==='/card/test')return route.fulfill({body:html,contentType:'text/html'});
    if(p==='/api/config')return route.fulfill({json:{ok:true,provider:'gemini',gemini:{model:'test'}}});
    const bid=url.searchParams.get('branch_id')||'main';
    if(p.endsWith('/state'))return route.fulfill({json:{ok:true,branch_id:bid,active_branch_id:bid,
     branches:[{id:'main',name:'主線'},{id:'child',name:'子線',parent_branch_id:'main',branch_point_index:0}],
     card:{display_name:'小蘭',persona:{},setting:{}},relationship:{tier:'友好'},relationship_tiers:['友好'],clock:{},inventory:{},memories:[],
     persistent_instruction:configs[bid],messages:messages[bid]}});
    if(p.endsWith('/instruction')){
     if(req.method()==='GET')return route.fulfill({json:{ok:true,persistent_instruction:configs[bid]}});
     const body=req.postDataJSON();posts.push({p,body});if(holdSave)await holdSave;
     if(failSave)return route.fulfill({status:500,json:{ok:false,error:'保存失敗，草稿仍保留，請稍後再試。'}});
     if(body.expected_revision!==configs[body.branch_id].revision)return route.fulfill({status:409,json:{ok:false,error:'持續指示已更新，草稿仍保留；請重新載入後再保存。'}});
     return route.fulfill({json:{ok:true,persistent_instruction:nextConfig(body.branch_id,body.text.trim(),body.enabled)}});
    }
    if(p.endsWith('/send/stream')||p.endsWith('/regenerate')){
     assert(await page.locator('#cardResponseLength button').evaluateAll(buttons=>buttons.every(b=>b.disabled)), 'length shortcuts disabled during generation');
     const body=req.postDataJSON();posts.push({p,body});const line=messages[body.branch_id],config=configs[body.branch_id];
     const regen=p.endsWith('/regenerate');
     const user=regen?line.at(-2).content:body.message;
     if(regen)line.splice(-2);
     const index=line.length,id='v'+(versions[body.branch_id].length+1);
     const content=`演出 ${id}：小蘭回應了你的話。`;
     line.push({role:'user',content:user,index},{role:'gm',content,index:index+1});
     versions[body.branch_id].push({id,content,persistent_instruction:config.enabled?config.text:'',favorite:false});
     return route.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({type:'done',result:{ok:true,narration:content}})+'\n\n'});
    }
    if(p.endsWith('/candidates'))return route.fulfill({json:{ok:true,group_id:'group',message_index:2,user_message:'先打招呼',latest:true,
     selected_id:versions[bid].at(-1).id,regen_count:versions[bid].length-1,failed_count:0,
     versions:versions[bid].map((v,i)=>({...v,selected:i===versions[bid].length-1}))}});
    return route.fulfill({json:{ok:true}});
   });
   await page.goto('http://story.test/card/test');const open=page.locator('#cardInstructionOpen');
   await open.waitFor();await page.waitForFunction(()=>!document.querySelector('#cardInstructionOpen').disabled);
   assert.equal(await open.textContent(),'指示');
   assert.equal(await page.locator('#cardContinueBtn').textContent(),'繼續 →');
   const length=page.locator('#cardResponseLength');
   assert.equal(await length.locator('[aria-pressed=true]').textContent(),'中');
   await length.getByRole('button',{name:'不限長度',exact:true}).click();
   assert.equal(await page.locator('#responseLengthSelect').inputValue(),'unlimited');
   await page.reload();
   await page.waitForFunction(()=>!document.querySelector('#cardInstructionOpen').disabled);
   assert.equal(await page.locator('#responseLengthSelect').inputValue(),'unlimited');
   assert.equal(await length.locator('[aria-pressed=true]').textContent(),'∞');
   await length.getByRole('button',{name:'長回覆'}).click();
   assert.equal(await page.locator('#responseLengthSelect').inputValue(),'long');
   await page.locator('#addon-panel-btn').click();
   await page.locator('#responseLengthSelect').selectOption('short');
   assert.equal(await length.locator('[aria-pressed=true]').textContent(),'短');
   await page.locator('#addon-panel-btn').click();
   await length.getByRole('button',{name:'長回覆'}).click();
   assert(await length.evaluate(el=>el.getBoundingClientRect().right<=window.innerWidth));
   await page.locator('#input-area').screenshot({path:`/tmp/story-length-shortcuts-${viewport.width}.png`});
   await page.locator('#user-input').fill('尚未送出的草稿');await open.click();
   const text=page.locator('#cardInstructionText'),toggle=page.locator('#cardInstructionEnabled'),save=page.locator('#cardInstructionSave');
   await page.waitForFunction(()=>!document.querySelector('#cardInstructionText').disabled);
   await toggle.check();await save.click();await page.getByText('請填寫持續指示，或關閉此功能',{exact:true}).waitFor();
   const directive='<script>window.bad=true</script>\n艾迪用風趣的方式回覆。';
   await text.fill(directive);failSave=true;await save.click();await page.getByText('保存失敗，草稿仍保留，請稍後再試。',{exact:true}).waitFor();
   assert.equal(await text.inputValue(),directive);failSave=false;
   holdSave=new Promise(resolve=>releaseSave=resolve);await save.click();
   await page.waitForFunction(()=>document.querySelector('#cardInstructionText').disabled);
   await page.locator('#cardInstructionClose').evaluate(button=>button.click());
   assert(await page.locator('#cardInstructionDialog').evaluate(d=>d.open),'saving cannot close the editor');
   releaseSave();holdSave=null;
   await page.locator('#cardInstructionOpen[aria-pressed="true"]').waitFor();
   assert.equal(await page.locator('#user-input').inputValue(),'尚未送出的草稿');
   assert.equal(await page.evaluate(()=>window.bad),undefined);
   await page.reload();await page.locator('#cardInstructionOpen[aria-pressed="true"]').waitFor();
   assert.equal(await length.locator('[aria-pressed=true]').textContent(),'長');
   await open.click();await page.waitForFunction(()=>!document.querySelector('#cardInstructionText').disabled);
   assert.equal(await text.inputValue(),directive);assert(await toggle.isChecked());
   await text.fill('未保存的草稿');await page.keyboard.press('Escape');await page.locator('#cardInstructionDiscard[open]').waitFor();
   await page.locator('#cardInstructionDiscard button[value=cancel]').click();assert.equal(await text.inputValue(),'未保存的草稿');
   // Polling changes the active indicator, never an open editor's draft.
   nextConfig('main','另一個視窗保存的指示',true);
   await save.click();await page.getByText('持續指示已更新，草稿仍保留；請重新載入後再保存。',{exact:true}).waitFor();
   assert.equal(await text.inputValue(),'未保存的草稿');
   await page.locator('#cardInstructionReload').click();await page.locator('#cardInstructionDiscard button[value=discard]').click();
   await page.waitForFunction(()=>document.querySelector('#cardInstructionText').value==='另一個視窗保存的指示');
   await text.fill('艾迪以風趣的語氣承接。');await save.click();await page.waitForFunction(()=>!document.querySelector('#cardInstructionDialog').open);
   await page.locator('#user-input').fill('先打招呼');await page.locator('#send-btn').click();await page.getByText('演出 v1：小蘭回應了你的話。',{exact:true}).waitFor();
   assert.equal(versions.main[0].persistent_instruction,'艾迪以風趣的語氣承接。');
   const send=posts.find(p=>p.p.endsWith('/send/stream')).body;
   assert.equal(send.response_length,'long');
   assert.equal(send.message,'先打招呼');assert(!('persistent_instruction' in send),'server owns saved instructions');
   // Disable without deleting, regenerate with the new saved setting.
   await open.click();await page.waitForFunction(()=>!document.querySelector('#cardInstructionText').disabled);await toggle.uncheck();await save.click();
   await page.locator('#cardInstructionOpen[aria-pressed="false"]').waitFor();assert.equal(configs.main.text,'艾迪以風趣的語氣承接。');
   await page.locator('[title="重新生成"]').click();await page.locator('#confirm-modal-ok').click();
   await page.getByText('演出 v2：小蘭回應了你的話。',{exact:true}).waitFor();assert.equal(versions.main[1].persistent_instruction,'');
   await page.locator('[title="查看回覆版本與收藏"]').click();await page.getByText('第 2／2 版 · 目前採用',{exact:true}).waitFor();
   await page.locator('#cardCandidateInstructionDetails summary').click();assert.match(await page.locator('#cardCandidateInstruction').textContent(),/未啟用/);
   await page.locator('#cardCandidatePrev').click();assert.equal(await page.locator('#cardCandidateInstruction').textContent(),'艾迪以風趣的語氣承接。');
   await page.locator('#cardCandidateClose').click();
   await open.click();await page.waitForFunction(()=>!document.querySelector('#cardInstructionText').disabled);
   await toggle.check();await save.click();await page.locator('#cardInstructionOpen[aria-pressed="true"]').waitFor();
   assert.equal(await open.textContent(),'指示');
   assert(await open.evaluate(e=>e.classList.contains('active')));
   await page.screenshot({path:`/tmp/story-instructions-active-${viewport.width}.png`});
   await open.click();await page.waitForFunction(()=>!document.querySelector('#cardInstructionText').disabled);
   await page.screenshot({path:`/tmp/story-instructions-editor-${viewport.width}.png`});
   const box=await page.locator('#cardInstructionDialog').boundingBox();assert(box.x>=0&&box.x+box.width<=viewport.width);
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   // Clear is reversible until save; then clears and disables together.
   await page.locator('#cardInstructionClear').click();assert.equal(await text.inputValue(),'');assert(!await toggle.isChecked());
   await save.click();await page.waitForFunction(()=>!document.querySelector('#cardInstructionDialog').open);await page.reload();
   await page.getByRole('button',{name:'指示',exact:true}).waitFor();assert.equal(configs.main.text,'');assert.equal(configs.main.enabled,false);
   await page.goto('http://story.test/card/test?branch_id=child');await page.locator('#cardInstructionOpen[aria-pressed="false"]').waitFor();
   await open.click();await page.waitForFunction(()=>!document.querySelector('#cardInstructionText').disabled);
   assert.equal(await text.inputValue(),'子線原有設定');await text.fill('子線用直率語氣');await toggle.check();await save.click();
   await page.locator('#cardInstructionOpen[aria-pressed="true"]').waitFor();assert.equal(posts.at(-1).body.branch_id,'child');assert.equal(configs.main.text,'');
   assert.deepEqual(errors,[]);await page.close();
  }
  console.log('PASS: desktop/mobile instructions, length shortcut/select sync+reload+payload+busy, enable/save/reload, validation, IO/conflict drafts, discard/reload, no HTML execution, generation payload, toggle+regen snapshots, branch pinning, clear, viewport');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
