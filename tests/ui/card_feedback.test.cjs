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
  for(const viewport of [{width:1280,height:900},{width:390,height:844}]){
   const page=await browser.newPage({viewport}); const errors=[],posts=[];
   page.on('pageerror',e=>errors.push(e.message));
   let selected='v1',failSave=false,failLike=false,holdLike=null,releaseLike,revision=0;
   const view=id=>({version_id:id,liked:false,comment:'',comment_revision:null});
   const versions=[{id:'v1',content:'小蘭沒有順著他的話，反而提出自己的判斷。',favorite:false,feedback:view('v1')},
    {id:'v2',content:'新版的小蘭自己換了一個話題。',favorite:false,feedback:view('v2')}];
   const opening={role:'gm',content:'開場',index:0,feedback:view('opening')};
   const state=()=>({ok:true,branch_id:'main',active_branch_id:'main',branches:[{id:'main',name:'主線'}],
    card:{display_name:'小蘭',persona:{},setting:{}},relationship:{tier:'友好'},relationship_tiers:['友好'],
    clock:{},inventory:{},memories:['劇情中的原有記憶'],messages:[opening,{role:'user',content:'你怎麼看？',index:1},
     {role:'gm',content:versions.find(v=>v.id===selected).content,index:2,feedback:versions.find(v=>v.id===selected).feedback}]});
   await page.route('http://story.test/**',async route=>{
    const req=route.request(),url=new URL(req.url()),p=url.pathname;
    if(p.startsWith('/static/'))return route.fulfill({body:readFileSync(path.join(root,p)),contentType:p.endsWith('.css')?'text/css':'text/javascript'});
    if(p==='/card/test')return route.fulfill({body:html,contentType:'text/html'});
    if(p==='/api/config')return route.fulfill({json:{ok:true,provider:'gemini',gemini:{model:'test'}}});
    if(p.endsWith('/state'))return route.fulfill({json:state()});
    if(p.endsWith('/feedback')){
     const body=req.postDataJSON();posts.push(body);
     const feedback=body.version_id==='opening'?opening.feedback:versions.find(v=>v.id===body.version_id).feedback;
     if('liked' in body){
      if(holdLike)await holdLike;
      if(failLike)return route.fulfill({status:409,json:{ok:false,error:'回覆已改變'}});
      feedback.liked=body.liked;
     }
     if('comment' in body){
      if(failSave)return route.fulfill({status:409,json:{ok:false,error:'註記已更新，草稿仍保留'}});
      assert.equal(body.expected_comment_revision,feedback.comment_revision);
      feedback.comment=body.comment;feedback.comment_revision='r'+(++revision);
     }
     return route.fulfill({json:{ok:true,feedback}});
    }
    if(p.endsWith('/candidates'))return route.fulfill({json:{ok:true,group_id:'group1',message_index:2,
     user_message:'你怎麼看？',selected_id:selected,latest:true,regen_count:selected==='v1'?0:1,failed_count:0,
     versions:versions.slice(0,selected==='v1'?1:2).map(v=>({...v,selected:v.id===selected}))}});
    if(p.endsWith('/regenerate')){
     selected='v2';
     return route.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({type:'done',result:{ok:true,narration:versions[1].content}})+'\n\n'});
    }
    return route.fulfill({json:{ok:true}});
   });
   await page.goto('http://story.test/card/test');
   const last=page.locator('#message-list .message.gm').last();
   const thumb=()=>last.locator('.card-feedback-like');
   await thumb().waitFor(); assert.equal(await thumb().getAttribute('aria-pressed'),'false');
   assert.equal(await last.locator('.card-feedback-note').count(),0);
   // No dialog on like, duplicate clicks guarded while the request is pending.
   holdLike=new Promise(resolve=>releaseLike=resolve);
   await thumb().click();await page.waitForFunction(()=>document.querySelectorAll('.card-feedback-like')[1].disabled);
   await thumb().evaluate(button=>button.click());
   releaseLike();holdLike=null;
   await last.getByText('加註',{exact:true}).waitFor();
   assert.equal(posts.length,1);assert.equal(await page.locator('#cardFeedbackDialog').evaluate(d=>d.open),false);
   await last.getByText('加註',{exact:true}).click();
   const note='<script>window.bad=true</script>\n她反對艾迪，很有自己的立場。';
   await page.locator('#cardFeedbackComment').fill(note);
   failSave=true;await page.locator('#cardFeedbackSave').click();
   await page.getByText('註記已更新，草稿仍保留',{exact:true}).waitFor();
   assert.equal(await page.locator('#cardFeedbackComment').inputValue(),note);
   assert.equal(await page.locator('#cardFeedbackDialog').evaluate(d=>d.open),true);
   failSave=false;await page.locator('#cardFeedbackSave').click();
   await last.getByText('查看／編輯註記',{exact:true}).waitFor();
   assert.equal(posts.at(-1).version_id,'v1');assert.equal(posts.at(-1).branch_id,'main');
   assert(!('liked' in posts.at(-1)),'saving a comment must not toggle a like');
   assert.deepEqual(state().memories,['劇情中的原有記憶']);
   await thumb().click();await page.waitForFunction(()=>document.querySelectorAll('.card-feedback-like')[1].getAttribute('aria-pressed')==='false');
   await last.getByText('查看／編輯註記',{exact:true}).click();
   assert.equal(await page.locator('#cardFeedbackComment').inputValue(),note);
   assert.equal(await page.evaluate(()=>window.bad),undefined);
   await page.locator('#cardFeedbackComment').fill('未保存的草稿');await page.keyboard.press('Escape');
   await page.locator('#cardFeedbackDiscard[open]').waitFor();
   await page.locator('#cardFeedbackDiscard button[value=cancel]').click();
   assert.equal(await page.locator('#cardFeedbackComment').inputValue(),'未保存的草稿');
   await page.locator('#cardFeedbackCancel').click();await page.locator('#cardFeedbackDiscard button[value=discard]').click();
   await page.reload();await last.getByText('查看／編輯註記',{exact:true}).waitFor();
   assert.equal(await thumb().getAttribute('aria-pressed'),'false');
   // A failed like must preserve the saved note and old visual state.
   failLike=true;await thumb().click();await page.getByText('回覆已改變',{exact:true}).waitFor();
   assert.equal(await thumb().getAttribute('aria-pressed'),'false');failLike=false;
   await page.screenshot({path:`/tmp/story-feedback-${viewport.width}.png`});
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'message actions must fit the viewport');
   // Regen creates a fresh, unrated version; archived version stays editable.
   await last.locator('[title="重新生成"]').click();
   await page.locator('#confirm-modal-ok').click();
   await page.getByText(versions[1].content,{exact:true}).waitFor();
   assert.equal(await thumb().getAttribute('aria-pressed'),'false');
   assert.equal(await last.locator('.card-feedback-note').count(),0);
   await last.locator('[title="查看回覆版本與收藏"]').click();
   await page.getByText('第 2／2 版 · 目前採用',{exact:true}).waitFor();
   await page.locator('#cardCandidatePrev').click();
   await page.locator('#cardCandidateFeedback .card-feedback-note').click();
   assert.equal(await page.locator('#cardFeedbackComment').inputValue(),note);
   await page.locator('#cardFeedbackComment').fill('舊版仍值得保留');await page.locator('#cardFeedbackSave').click();
   await page.waitForFunction(()=>!document.querySelector('#cardFeedbackDialog').open);
   assert.equal(posts.at(-1).version_id,'v1');assert.equal(posts.at(-1).group_id,'group1');
   assert.equal(versions[1].feedback.comment,'');
   await page.locator('#cardCandidateNext').click();
   assert.equal(await page.locator('#cardCandidateFeedback .card-feedback-note').count(),0);
   await page.locator('#cardCandidateClose').click();
   // Clear an unliked note and the optional entry disappears, like remains off.
   selected='v1';await page.reload();await last.locator('.card-feedback-note').click();
   await page.locator('#cardFeedbackComment').fill('');await page.locator('#cardFeedbackSave').click();
   await page.waitForFunction(()=>!document.querySelector('#cardFeedbackDialog').open);
   assert.equal(await last.locator('.card-feedback-note').count(),0);
   assert.equal(await thumb().getAttribute('aria-pressed'),'false');
   // Opening can be liked as well, without a candidate-version button.
   await page.locator('#message-list .message.gm').first().locator('.card-feedback-like').click();
   await page.locator('#message-list .message.gm').first().getByText('加註',{exact:true}).waitFor();
   assert.equal(posts.at(-1).version_id,'opening');assert.equal(posts.at(-1).message_index,0);
   assert.deepEqual(errors,[]);await page.close();
  }
  console.log('PASS: desktop/mobile feedback, no-popup like, duplicate guard, comment failure/draft/discard, reload, unlike retains note, regen isolation, old-version annotation, opening, viewport');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
