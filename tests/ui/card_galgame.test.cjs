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
   const page=await browser.newPage({viewport}), errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   let enabled=true;
   await page.route('http://story.test/**',async route=>{
    const p=new URL(route.request().url()).pathname;
    if(p.startsWith('/static/'))return route.fulfill({body:readFileSync(path.join(root,p)),contentType:p.endsWith('.css')?'text/css':'text/javascript'});
    if(p==='/card/test')return route.fulfill({body:html,contentType:'text/html'});
    if(p==='/api/config')return route.fulfill({json:{ok:true,provider:'gemini',gemini:{model:'test'}}});
    if(p.endsWith('/state'))return route.fulfill({json:{ok:true,branch_id:'main',branches:[{id:'main',name:'主線'}],card:{display_name:'林晚 · 雨巷書店',persona:{},setting:{},relationship_enabled:!enabled},relationship:{tier:'陌生'},relationship_tiers:['陌生'],clock:{},inventory:{},memories:[],messages:[{role:'gm',index:0,content:'「避雨可以，濕傘放門口就好。」'}],galgame:enabled?{affinity:12,trust:10,resentment:20,desire:0,stage:'戒備',last_event:{reason:'擅自牽手越過目前界線，她拒絕並拉開距離。<script>throw Error("unsafe")</script>'},boundaries:[{label:'牽手',affinity:45,trust:30,eligible:false}]}:null}});
    return route.fulfill({json:{ok:true}});
   });
   await page.goto('http://story.test/card/test');
   await page.waitForSelector('.message.gm');
   await page.locator('#drawer-toggle-btn').click();
   await page.waitForSelector('#galgamePanel',{state:'visible'});
   assert.equal(await page.locator('#galgamePanel progress').count(),4);
   assert.equal(await page.locator('#galgamePanel progress').first().getAttribute('value'),'12');
   assert.equal(await page.locator('#drawerTierBadge').textContent(),'戒備');
   assert.equal(await page.locator('#galgamePanel script').count(),0);
   await page.locator('#galgamePanel summary').click();
   assert((await page.locator('#galgamePanel').textContent()).includes('好感 45・信任 30'));
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'no page overflow');
   await page.screenshot({path:`/tmp/story-galgame-${viewport.width}.png`});
   enabled=false;
   await page.reload();
   await page.waitForSelector('.message.gm');
   assert(await page.locator('#galgamePanel').isHidden());
   assert.deepEqual(errors,[]);
   await page.close();
  }
 }finally{await browser.close();}
 console.log('Galgame desktop/mobile UI passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
