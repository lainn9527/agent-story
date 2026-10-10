import {ReviewSession} from '/Users/eddylai/self/story/scripts/lore/pi_review_guard.mjs';
export class DraftSession extends ReviewSession {
  submit({items}) {
    if(this.accepted)throw Error('Already submitted');
    const missing=[...this.documents.keys()].filter(id=>!this.complete(id));
    if(missing.length)throw Error('Read every page first: '+missing.join(','));
    if(!Array.isArray(items)||items.length>(this.snapshot.max_items||12))throw Error('Too many items');
    const seen=new Set();
    const normalized=items.map(item=>{
      if(!/^[a-z][a-z0-9_]{1,48}$/.test(item.id)||seen.has(item.id))throw Error('Invalid or duplicate id');seen.add(item.id);
      if(!['add','update'].includes(item.operation))throw Error('Invalid operation');
      if((item.operation==='update')!==this.snapshot.baseline_ids.includes(item.id))throw Error('Update must use existing id; add must use new id');
      for(const k of ['title','text'])if(typeof item[k]!=='string'||!item[k].trim()||item[k].length>(k==='text'?800:80))throw Error('Invalid '+k);
      if(!Array.isArray(item.keywords)||item.keywords.length>12||item.keywords.some(k=>typeof k!=='string'||!k.trim()||k.length>40))throw Error('Invalid keywords');
      if(!Array.isArray(item.evidence)||!item.evidence.length||item.evidence.length>8)throw Error('Evidence required');
      const evidence=item.evidence.map(ref=>{
        const source=this.snapshot.messages.find(m=>m.role==='gm'&&m.index===ref.message_index);
        if(!source||typeof ref.quote!=='string'||!ref.quote.trim()||ref.quote.length>700||!source.content.includes(ref.quote))throw Error('Invalid citation for '+item.id+' at #'+ref.message_index+'. Copy a short exact substring; do not append a closing quote that is absent in the source.');
        return {message_index:source.index,hash:source.hash,quote:ref.quote};
      });
      return {...item,evidence};
    });
    this.accepted={batch:this.snapshot.batch,items:normalized,fully_read_ids:[...this.documents.keys()],
      limitation:'Read coverage and quotes verified, relevance and semantic entailment require evaluation; proposals only, no story writes.'};
    return this.accepted;
  }
}
