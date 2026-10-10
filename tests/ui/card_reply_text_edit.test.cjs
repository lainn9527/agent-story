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
   const page=await browser.newPage({viewport});const errors=[],calls=[];page.on('pageerror',e=>errors.push(e.message));
   let failSave=true, selected='v2', latest=4;
   const versions=[{id:'v2',content:'原版正文。\n\n她望向窗外。',favorite:false,model:'test',generated_at:'2026-10-03T01:00:00Z'}];
   const feedback=id=>({version_id:id,liked:false,comment:'',comment_revision:null});
   await page.route('http://story.test/**',async route=>{
    const req=route.request(),url=new URL(req.url()),p=url.pathname;
    if(p.startsWith('/static/'))return route.fulfill({body:readFileSync(path.join(root,p)),contentType:p.endsWith('.css')?'text/css':'text/javascript'});
    if(p==='/card/test')return route.fulfill({body:html,contentType:'text/html'});
    if(p==='/api/config')return route.fulfill({json:{ok:true,provider:'gemini',gemini:{model:'test'}}});
    if(p.endsWith('/state')){
     const messages=[{role:'gm',index:0,content:'開場',feedback:feedback('opening')},
      {role:'user',index:1,content:'前一輪'}, {role:'gm',index:2,content:'歷史正文',feedback:feedback('v1')},
      {role:'user',index:3,content:'你怎麼想？'},
      {role:'gm',index:4,content:versions.find(v=>v.id===selected).content,feedback:feedback(selected),candidate_summary:{versions:versions.length,regen_count:0,failed_count:0,selected_id:selected,selected_number:versions.findIndex(v=>v.id===selected)+1}}];
     if(latest>4)messages.push({role:'user',index:5,content:'下一輪'},{role:'gm',index:6,content:'新的回覆',feedback:feedback('v4')});
     return route.fulfill({json:{ok:true,branch_id:'main',active_branch_id:'main',branches:[{id:'main',name:'主線'}],card:{display_name:'小蘭',persona:{},setting:{}},relationship:{tier:'友好'},relationship_tiers:['友好'],clock:{},inventory:{},memories:['未變動的記憶'],messages}});
    }
    if(p.endsWith('/candidates')){
     if(req.method()==='GET')return route.fulfill({json:{ok:true,group_id:'g1',message_index:4,user_message:'你怎麼想？',latest:latest===4,selected_id:selected,regen_count:0,failed_count:0,versions:versions.map(v=>({...v,selected:v.id===selected,feedback:feedback(v.id)}))}});
     const body=req.postDataJSON();calls.push(body);
     if(body.action==='edit-text'){
      assert.equal(body.branch_id,'main');assert.equal(body.message_index,4);
      assert.equal(body.expected_version_id,selected);assert.equal(body.expected_reply,versions.find(v=>v.id===selected).content);
      if(failSave)return route.fulfill({status:500,json:{ok:false,error:'保存失敗，草稿仍保留，請稍後再試。'}});
      selected='v3';versions.push({id:selected,content:body.content,favorite:false,text_only_edit:true,edited_at:'2026-10-03T01:01:00Z'});
      return route.fulfill({json:{ok:true,branch_id:'main',reply_version_id:selected}});
     }
     if(body.action==='select'){selected=body.version_id;return route.fulfill({json:{ok:true,branch_id:'main'}});}
    }
    return route.fulfill({json:{ok:true}});
   });
   await page.goto('http://story.test/card/test');
   const editButton=page.getByRole('button',{name:'編輯正文',exact:true});await editButton.waitFor();
   assert.equal(await editButton.count(),1,'only latest GM may be manually edited');
   await editButton.click();assert.equal(await page.locator('#cardReplyEditText').inputValue(),versions[0].content);
   assert(await page.locator('#cardReplyEditSave').isDisabled());
   await page.locator('#cardReplyEditText').fill('修改後的正文。\n\n她笑了笑。');
   await page.locator('#cardReplyEditCancel').click();await page.locator('#cardMemoryDiscard').waitFor();
   await page.locator('#cardMemoryDiscard [value="cancel"]').click();
   assert(await page.locator('#cardReplyEditDialog').isVisible());
   await page.locator('#cardReplyEditSave').click();await page.locator('#cardReplyEditError').getByText('保存失敗，草稿仍保留，請稍後再試。',{exact:true}).waitFor();
   assert.equal(await page.locator('#cardReplyEditText').inputValue(),'修改後的正文。\n\n她笑了笑。');
   assert.match(await page.locator('#message-list').textContent(),/原版正文/);
   failSave=false;await page.locator('#cardReplyEditSave').click();
   await page.waitForFunction(()=>!document.querySelector('#cardReplyEditDialog').open && document.querySelector('#message-list').textContent.includes('修改後的正文'));
   assert.equal(calls.filter(c=>c.action==='edit-text').length,2);
   await page.locator('[title="查看回覆版本與收藏"]').last().click();
   await page.getByText('第 2／2 版 · 目前採用',{exact:true}).waitFor();
   assert.match(await page.locator('#cardCandidateMeta').textContent(),/手動編輯/);
   assert.match(await page.locator('#cardCandidateStats').textContent(),/重生 0 次/);
   await page.locator('#cardCandidatePrev').click();assert.match(await page.locator('#cardCandidateContent').textContent(),/原版正文/);
   await page.locator('#cardCandidateApply').click();await page.waitForFunction(()=>document.querySelector('#message-list').textContent.includes('原版正文'));
   await page.locator('[title="查看回覆版本與收藏"]').last().click();await page.locator('#cardCandidateNext').click();await page.locator('#cardCandidateApply').click();
   await page.waitForFunction(()=>document.querySelector('#message-list').textContent.includes('修改後的正文'));
   await page.reload();await editButton.waitFor();await editButton.click();
   assert.equal(await page.locator('#cardReplyEditText').inputValue(),'修改後的正文。\n\n她笑了笑。');
   const box=await page.locator('#cardReplyEditDialog').boundingBox();assert(box.x>=0 && box.x+box.width<=viewport.width);
   await page.screenshot({path:`/tmp/story-reply-text-edit-${viewport.width}.png`});
   await page.locator('#cardReplyEditText').fill(' \n');assert(await page.locator('#cardReplyEditSave').isDisabled());
   await page.locator('#cardReplyEditCancel').click();await page.locator('#cardMemoryDiscard [value="discard"]').click();
   latest=6;await page.reload();await editButton.waitFor();assert.equal(await editButton.count(),1);
   assert.match(await editButton.locator('..').textContent(),/編輯正文/);
   await editButton.click();assert.equal(await page.locator('#cardReplyEditText').inputValue(),'新的回覆');
   assert.deepEqual(errors,[]);await page.close();
  }
  console.log('PASS: latest-only edit, prefill, dirty discard, failure keeps draft, new version, old version selection, no regen count, reload, desktop/mobile');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
