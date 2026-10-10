import {isDeepStrictEqual} from 'node:util';
// Deterministic gate used both by the agent tools and independent host replay.
export class MaintenanceSession {
  constructor(snapshot) {
    this.snapshot = structuredClone(snapshot);
    this.docs = new Map(snapshot.records.map(r => [r.id, r]));
    this.ranges = new Map(); this.searches = 0; this.calls = 0; this.plan = null; this.accepted = null;
  }
  budget() { if (++this.calls > 240 || this.accepted) throw Error('Task closed or tool budget exhausted'); }
  complete(id) {
    let end = 0;
    for (const [a,b] of [...(this.ranges.get(id) || [])].sort((a,b) => a[0]-b[0])) {
      if (a > end) return false; end = Math.max(end,b);
    }
    return end === this.docs.get(id)?.text.length;
  }
  read({id,offset=0}) {
    this.budget(); const doc=this.docs.get(id);
    if (!doc || !Number.isInteger(offset) || offset < 0 || offset >= doc.text.length) throw Error('Invalid record or offset');
    const end=Math.min(offset+6000,doc.text.length);
    this.ranges.set(id,[...(this.ranges.get(id)||[]),[offset,end]]);
    return {id,kind:doc.kind,role:doc.role??null,text:doc.text.slice(offset,end),next_offset:end<doc.text.length?end:null};
  }
  search({query,offset=0}) {
    this.budget();
    if (typeof query!=='string'||query.length<1||query.length>100||!Number.isInteger(offset)||offset<0) throw Error('Invalid search');
    this.searches++;
    const hits=[...this.docs.values()].filter(d=>d.text.includes(query));
    return {total:hits.length,next_offset:offset+20<hits.length?offset+20:null,
      hits:hits.slice(offset,offset+20).map(d=>{const i=d.text.indexOf(query);return {id:d.id,kind:d.kind,excerpt:d.text.slice(Math.max(0,i-80),i+180)};})};
  }
  propose({operations}) {
    this.budget();
    if (!Array.isArray(operations)||operations.length>12) throw Error('At most twelve bounded changes per task');
    if(this.snapshot.review_operations && !isDeepStrictEqual(operations,this.snapshot.review_operations))throw Error('Independent reviewer cannot change the proposal');
    const used=new Set(); const claims=[]; const opsInventoryNames=new Set();
    const ops=operations.map((op,i)=>{
      if (!this.snapshot.kinds.includes(op.kind)||!['memory','lore','summary','inventory'].includes(op.kind)) throw Error('Kind outside task scope');
      if (typeof op.text!=='string'||(op.kind!=='inventory'&&!op.text.trim())||op.text.length>(op.kind==='summary'?8000:1800)) throw Error('Invalid replacement text');
      if (!Array.isArray(op.target_ids)||op.target_ids.length>12||new Set(op.target_ids).size!==op.target_ids.length) throw Error('Invalid targets');
      if (op.kind==='inventory') {
        if((op.text && !op.text.trim()) || !op.target_ids.length || typeof op.item_name!=='string' || !op.item_name.trim() || op.item_name.length>100)throw Error('Inventory requires existing targets and a bounded item name');
        if(op.text && !/[\u3400-\u9fff]/u.test(op.item_name))throw Error('Inventory display names must use Chinese');
        if(opsInventoryNames.has(op.item_name))throw Error('Duplicate inventory destination');
        opsInventoryNames.add(op.item_name);
        const existing=[...this.docs.values()].find(d=>d.kind==='inventory'&&d.item_name===op.item_name);
        if(op.text && existing && !op.target_ids.includes(existing.id))throw Error('Include existing destination when merging aliases');
      }
      if (op.kind==='summary' && (op.target_ids.length!==1||op.target_ids[0]!=='summary')) throw Error('Summary replaces summary');
      if (op.kind==='lore' && op.target_ids.length>1) throw Error('One lore target per operation');
      if (op.kind==='memory' && (!Number.isInteger(op.character)||!this.snapshot.characters.includes(op.character))) throw Error('Invalid character');
      for (const id of op.target_ids) {
        const d=this.docs.get(id);
        if (!d||d.kind!==op.kind||d.protected||used.has(id)||!this.complete(id)) throw Error('Target unread, protected, duplicate, or wrong kind');
        if(op.kind==='memory'&&d.character!==op.character)throw Error('Do not mix characters');
        used.add(id);
        if (!d.empty) claims.push({id:`keep:${i}:${id}`,operation:i,text:`核對替換是否保留此舊記錄的重要事實、否定、歸屬與時序。若更新狀態，原文是否證明變更？\n${d.text}`});
      }
      if (op.kind==='lore' && (typeof op.title!=='string'||!op.title.trim()||op.title.length>100||!Array.isArray(op.keywords)||op.keywords.length>12||op.keywords.some(k=>typeof k!=='string'||!k.trim()||k.length>50))) throw Error('Lore requires bounded title/keywords');
      if(this.snapshot.policy_version>=2)claims.push({id:`canon:${i}`,operation:i,text:'與作者設定及相關 memory/lore 比對。近期 GM 單次宣稱不能自行變成既有往事或永久設定；說明一致性、明確變更證據或未解矛盾。'});
      const parts=(op.kind==='inventory' && op.text ? op.item_name+'：'+op.text : op.text).match(/[^。；！？\n]+[。；！？]?/g)||[];
      parts.filter(s=>s.trim()).forEach((s,j)=>claims.push({id:`new:${i}:${j}`,operation:i,text:s.trim()}));
      return structuredClone(op);
    });
    // Selected new originals must be read even for a no-op: absence of changes is a decision too.
    const required=[...this.snapshot.required_ids];
    if(ops.some(o=>o.kind==='summary'))required.push(...this.snapshot.summary_required_ids);
    const missing=required.filter(id=>!this.complete(id));
    if(missing.length)throw Error(`Read entire required records: ${missing.join(', ')}`);
    if(claims.length>100)throw Error('Split into smaller tasks (max 100 checks)');
    this.plan={operations:ops,claims};
    return this.plan;
  }
  submit({checks}) {
    this.budget();
    if(!this.plan||!Array.isArray(checks)||checks.length!==this.plan.claims.length)throw Error('Propose first; review every new claim and replaced record');
    const seen=new Set();
    const checked=checks.map(c=>{
      if(!this.plan.claims.some(x=>x.id===c.claim_id)||seen.has(c.claim_id))throw Error('Unknown/duplicate check');
      seen.add(c.claim_id);
      if(!['supported','insufficient','conflict'].includes(c.verdict)||typeof c.reason!=='string'||!c.reason.trim()||c.reason.length>1600)throw Error('Invalid check');
      if(!Array.isArray(c.evidence)||c.evidence.length>8||(c.verdict==='supported'&&!c.evidence.length))throw Error('Evidence required');
      const evidence=c.evidence.map(e=>{
        const d=this.docs.get(e.source_id);
        if(!d||d.kind!=='source'||d.role!=='gm'||!this.complete(d.id)||typeof e.quote!=='string'||!e.quote.trim()||e.quote.length>1500||!d.text.includes(e.quote))throw Error('Evidence must be a verbatim quote from a fully read GM original');
        if(this.plan.operations[this.plan.claims.find(x=>x.id===c.claim_id).operation].kind==='summary'&&!this.snapshot.summary_source_ids.includes(d.id))throw Error('Summary cannot cite beyond its covered prefix');
        return {source_id:d.id,index:d.index,hash:d.hash,quote:e.quote};
      });
      if(c.claim_id.startsWith('canon:') && c.verdict==='supported'){
        if(!this.searches)throw Error('Search related existing facts before accepting durable context');
        if(!Array.isArray(c.context_ids)||!c.context_ids.length||c.context_ids.some(id=>!['canon','memory','lore','summary','inventory'].includes(this.docs.get(id)?.kind)||!this.complete(id)))throw Error('Consistency review needs fully read context records');
      }
      return {...c,evidence};
    });
    this.accepted={operations:this.plan.operations,checks:checked,all_supported:checked.every(c=>c.verdict==='supported'),
      fully_read_ids:[...this.docs.keys()].filter(id=>this.complete(id)),
      limitation:'Exact quotes and read coverage are verified. Semantic preservation is an agent assessment, not a proof.'};
    return this.accepted;
  }
}
