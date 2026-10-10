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
   let active='main',selected='v2',failSelection=false;
   const versions=[{id:'v1',content:'第一版：她收起相簿，決定自己去展覽。',favorite:false,model:'gemini-test',generated_at:'2026-10-01T04:00:00Z',narrative_style:'natural',plot_mode:'coauthor',scene_mode:'default'},
    {id:'v2',content:'第二版：她提出一起看展覽，再各自選一張照片。',favorite:false,model:'gemini-test',generated_at:'2026-10-01T04:01:00Z',narrative_style:'natural',plot_mode:'coauthor',scene_mode:'default'}];
   const branches=[{id:'main',name:'主線'}];
   const messages={main:[{role:'gm',content:'開場',index:0},{role:'user',content:'你想怎麼安排？',index:1},{role:'gm',content:versions[1].content,index:2}]};
   await page.route('http://story.test/**',async route=>{
    const req=route.request(),url=new URL(req.url()),p=url.pathname;
    if(p.startsWith('/static/'))return route.fulfill({body:readFileSync(path.join(root,p)),contentType:p.endsWith('.css')?'text/css':'text/javascript'});
    if(p==='/card/test')return route.fulfill({body:html,contentType:'text/html'});
    if(p==='/api/config')return route.fulfill({json:{ok:true,provider:'gemini',gemini:{model:'test'}}});
    if(p.endsWith('/state')){
     const bid=url.searchParams.get('branch_id')||active;
     const output=messages[bid].map(m=>({...m,...(m.index===2?{candidate_summary:{versions:2,regen_count:2,failed_count:1,selected_number:selected==='v1'?1:2}}:{})}));
     return route.fulfill({json:{ok:true,branch_id:bid,active_branch_id:active,branches,card:{display_name:'小蘭',persona:{},setting:{}},relationship:{tier:'友好'},relationship_tiers:['友好'],clock:{},inventory:{},memories:[selected==='v1'?'第一版的記憶':'第二版的記憶'],messages:output}});
    }
    if(p.endsWith('/candidates')){
     if(req.method()==='GET'){
      if(url.searchParams.get('favorites')==='1')return route.fulfill({json:{ok:true,favorites:versions.filter(v=>v.favorite).map(v=>({...v,group_id:'g1',message_index:2,user_message:'你想怎麼安排？'}))}});
      return route.fulfill({json:{ok:true,group_id:'g1',message_index:2,user_message:'你想怎麼安排？',selected_id:selected,
       latest:messages[active].at(-1).index===2,regen_count:2,failed_count:1,versions:versions.map(v=>({...v,selected:v.id===selected}))}});
     }
     const body=req.postDataJSON();calls.push({p,body});
     if(body.action==='favorite'){versions.find(v=>v.id===body.version_id).favorite=body.favorite;return route.fulfill({json:{ok:true}});}
     if(failSelection)return route.fulfill({status:400,json:{ok:false,error:'此回合已改變，請重新載入版本'}});
     assert.equal(body.expected_version_id,selected);selected=body.version_id;
     if(messages[body.branch_id].at(-1).index>2){
      active='card_choice';branches.push({id:active,name:'候選 #2',parent_branch_id:body.branch_id});
      messages[active]=messages[body.branch_id].filter(m=>m.index<=2).map(m=>m.index===2?{...m,content:versions.find(v=>v.id===selected).content}:m);
     }else messages[body.branch_id][2].content=versions.find(v=>v.id===selected).content;
     return route.fulfill({json:{ok:true,branch_id:active}});
    }
    if(p.endsWith('/send/stream')){
     const body=req.postDataJSON();calls.push({p,body});messages[body.branch_id].push({role:'user',content:body.message,index:3},{role:'gm',content:'角色自己提出新的想法。',index:4});
     return route.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({type:'done',result:{ok:true,narration:'角色自己提出新的想法。'}})+'\n\n'});
    }
    return route.fulfill({json:{ok:true}});
   });
   await page.goto('http://story.test/card/test');await page.locator('[title="查看回覆版本與收藏"]').waitFor();
   assert(await page.locator('.message.user').first().evaluate(el=>{
    const text=el.querySelector('.content').getBoundingClientRect();
    const edit=el.querySelector('.msg-actions').getBoundingClientRect();
    return edit.left>=text.right && edit.top<text.bottom;
   }),'user edit pencil shares the text row on desktop and mobile');
   await page.locator('[title="查看回覆版本與收藏"]').click();
   await page.getByText('第 2／2 版 · 目前採用',{exact:true}).waitFor();
   assert(await page.locator('#cardCandidateApply').isDisabled());
   assert.match(await page.locator('#cardCandidateStats').textContent(),/重生 2 次，其中 1 次失敗/);
   await page.locator('#cardCandidatePrev').click();
   assert.match(await page.locator('#cardCandidateContent').textContent(),/第一版/);
   assert.match(await page.locator('#message-list').textContent(),/第二版/,'preview cannot change canonical timeline');
   await page.locator('#cardCandidateFavorite').click();await page.getByText('★ 已收藏',{exact:true}).waitFor();
   const box=await page.locator('#cardCandidateDialog').boundingBox();assert(box.x>=0 && box.x+box.width<=viewport.width);
   await page.screenshot({path:`/tmp/story-candidates-${viewport.width}.png`});
   failSelection=true;await page.locator('#cardCandidateApply').click();await page.getByText('此回合已改變，請重新載入版本',{exact:true}).waitFor();
   assert.match(await page.locator('#message-list').textContent(),/第二版/);
   failSelection=false;await page.locator('#cardCandidateApply').click();
   await page.waitForFunction(()=>document.querySelector('#message-list').textContent.includes('第一版'));
   await page.locator('#cardFavoritesOpen').click();await page.getByText('收藏 1／1',{exact:true}).waitFor();
   assert.match(await page.locator('#cardCandidateContent').textContent(),/第一版/);await page.locator('#cardCandidateClose').click();
   await page.locator('#addon-panel-btn').click();await page.locator('#narrativeStyleSelect').selectOption('natural');
   await page.locator('#plotModeSelect').selectOption('coauthor');await page.locator('#sceneModeSelect').selectOption('intimate');await page.locator('#addon-panel-btn').click();
   await page.locator('#user-input').fill('尚未送出的草稿');await page.locator('#cardContinueBtn').click();
   await page.getByText('角色自己提出新的想法。',{exact:true}).waitFor();
   const continuation=calls.find(c=>c.p.endsWith('/send/stream')).body;
   assert.equal(continuation.message,'尚未送出的草稿\n\n（請繼續發展）');assert.equal(continuation.branch_id,'main');
   assert.equal(continuation.narrative_style,'natural');assert.equal(continuation.plot_mode,'coauthor');assert.equal(continuation.scene_mode,'intimate');
   assert.equal(await page.locator('#user-input').inputValue(),'');
   await page.locator('[title="查看回覆版本與收藏"]').first().click();await page.locator('#cardCandidateNext').click();
   await page.getByText('從這版建立分支',{exact:true}).waitFor();await page.locator('#cardCandidateApply').click();await page.waitForURL(/branch_id=card_choice/);
   assert.equal(messages.main.length,5);assert.equal(messages.card_choice.length,3);
   await page.reload();await page.locator('[title="查看回覆版本與收藏"]').click();await page.getByText('第 2／2 版 · 目前採用',{exact:true}).waitFor();
   assert.deepEqual(errors,[]);await page.close();
  }
  console.log('PASS: desktop/mobile candidates, preview, count, favorite, stale guard, selection, historical fork, continuation styles/draft, reload');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
