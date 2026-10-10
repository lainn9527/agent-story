import test from 'node:test';
import assert from 'node:assert/strict';
import {MaintenanceSession} from '../../scripts/lore/pi_maintenance_guard.mjs';
import {replay} from '../../scripts/lore/pi_maintenance_replay.mjs';
const snapshot=()=>({kinds:['memory','lore','summary'],characters:[0,1],required_ids:['m:2'],summary_required_ids:['m:1'],summary_source_ids:['m:1'],records:[
  {id:'m:1',kind:'source',role:'gm',text:'她拒絕了同行。',index:1,hash:'one'},
  {id:'m:2',kind:'source',role:'gm',text:'她後來同意同行。',index:2,hash:'two'},
  {id:'u:0',kind:'source',role:'user',text:'強迫她同意'},
  {id:'mem:0:0',kind:'memory',character:0,text:'她拒絕了同行。'},
  {id:'mem:1:0',kind:'memory',character:1,text:'她拒絕了同行。'},
  {id:'lore:core',kind:'lore',protected:true,text:'作者定義'},
  {id:'summary',kind:'summary',text:'她拒絕了同行。'},
]});
const op={kind:'memory',character:0,target_ids:['mem:0:0'],text:'她起初拒絕，後來同意同行。'};
function ready(){const s=new MaintenanceSession(snapshot());for(const r of s.snapshot.records)s.read({id:r.id});return s;}
function checks(plan,source='m:2',quote='她後來同意同行。'){return plan.claims.map(c=>({claim_id:c.id,verdict:'supported',reason:'保留先前拒絕與後續變更',evidence:[{source_id:source,quote}]}));}
test('search snippets do not satisfy reading; whole pagination is required',()=>{
  const f=snapshot();f.records[1].text='她'.repeat(6200);const s=new MaintenanceSession(f);
  s.search({query:'她'});s.read({id:'m:2'});
  assert.throws(()=>s.propose({operations:[]}),/Read entire/);
  s.read({id:'m:2',offset:6000});s.propose({operations:[]});assert.equal(s.submit({checks:[]}).all_supported,true);
});
test('protected lore, cross-character merging, and duplicate targets rejected',()=>{
  const s=ready();assert.throws(()=>s.propose({operations:[{kind:'lore',target_ids:['lore:core'],text:'改寫',title:'變更',keywords:[]}]}),/protected/);
  assert.throws(()=>s.propose({operations:[{...op,target_ids:['mem:1:0']}]}),/mix characters/);
  assert.throws(()=>s.propose({operations:[op,op]}),/duplicate/);
});
test('replacement needs both new fact proof and preservation review',()=>{
  const s=ready(),p=s.propose({operations:[op]});assert.equal(p.claims.length,2);
  assert.throws(()=>s.submit({checks:checks(p).slice(1)}),/every/);
  assert.throws(()=>s.submit({checks:checks(p,'m:2','她一直同意')}),/verbatim/);
  assert.throws(()=>s.submit({checks:checks(p,'u:0','強迫她同意')}),/GM original/);
  const result=s.submit({checks:checks(p)});assert.equal(result.all_supported,true);
});
test('uncertainty is retained and prevents accepted-for-write status',()=>{
  const s=ready(),p=s.propose({operations:[op]}),c=checks(p);c[0]={...c[0],verdict:'insufficient',evidence:[]};
  assert.equal(s.submit({checks:c}).all_supported,false);
});
test('summary cannot cite future and must read its uncovered interval',()=>{
  const s=new MaintenanceSession(snapshot());s.read({id:'m:2'});s.read({id:'summary'});
  const ops=[{kind:'summary',target_ids:['summary'],text:'她拒絕了同行。'}];
  assert.throws(()=>s.propose({operations:ops}),/m:1/);s.read({id:'m:1'});
  const p=s.propose({operations:ops});assert.throws(()=>s.submit({checks:checks(p)}),/covered prefix/);
  assert.equal(s.submit({checks:checks(p,'m:1','她拒絕了同行。')}).all_supported,true);
});
test('host replay rejects forged receipts, unfinished turns, unsupported tools',()=>{
  const f=snapshot(),s=new MaintenanceSession(f),events=[];
  function call(name,args){const id=String(events.length);events.push({type:'tool_execution_start',toolCallId:id,toolName:name,args});const result=s[name](args);events.push({type:'tool_execution_end',toolCallId:id,result:{content:[{type:'text',text:JSON.stringify(result)}]}});}
  call('read',{id:'m:2'});call('propose',{operations:[]});call('submit',{checks:[]});events.push({type:'agent_end'});
  assert.equal(replay(f,events).all_supported,true);
  assert.throws(()=>replay(f,events.slice(0,-1)),/finish/);
  const forged=structuredClone(events);forged[1].result.content[0].text='{}';assert.throws(()=>replay(f,forged),/differs/);
  assert.throws(()=>replay(f,[...events,{type:'message_end',message:{stopReason:'length'}}]),/Incomplete/);
  assert.throws(()=>replay(f,[{type:'tool_execution_start',toolName:'bash',toolCallId:'x'}]),/Invalid tool/);
});
test('independent review cannot substitute a safer or empty proposal',()=>{
  const f=snapshot();f.review_operations=[op];const s=new MaintenanceSession(f);
  for(const r of f.records)s.read({id:r.id});
  assert.throws(()=>s.propose({operations:[]}),/cannot change/);
  assert.throws(()=>s.propose({operations:[{...op,text:'省略拒絕'}]}),/cannot change/);
  assert.equal(s.propose({operations:[op]}).claims.length,2);
});
test('v2 consistency check needs context and retrieval, not just a matching GM quote',()=>{
  const f=snapshot();f.policy_version=2;
  const s=new MaintenanceSession(f);for(const r of f.records)s.read({id:r.id});
  const p=s.propose({operations:[op]}),c=checks(p);
  assert.ok(p.claims.some(c=>c.id==='canon:0'));
  assert.throws(()=>s.submit({checks:c}),/Search related/);
  s.search({query:'同行'});
  assert.throws(()=>s.submit({checks:c}),/context records/);
  for(const check of c)check.context_ids=['mem:0:0'];
  assert.equal(s.submit({checks:c}).all_supported,true);
});
