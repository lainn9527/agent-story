import {readFileSync} from 'node:fs';
import {DraftSession} from './pi_generation_guard.mjs';
export default function(pi){
 const session=new DraftSession(JSON.parse(readFileSync(process.env.STORY_PI_SNAPSHOT,'utf8')));
 const str={type:'string'};
 const wrap=value=>({content:[{type:'text',text:JSON.stringify(value)}],details:value});
 pi.registerTool({name:'read_snapshot',label:'Read source',executionMode:'sequential',description:'Read 2400-character pages from l:baseline or dialogue. Follow next_offset until null.',
  parameters:{type:'object',properties:{id:str,offset:{type:'integer',minimum:0}},required:['id'],additionalProperties:false},
  async execute(_id,p){return wrap(session.read(p));}});
 pi.registerTool({name:'submit_lore',label:'Submit lore proposals',executionMode:'sequential',description:'Submit new facts only. All pages must be read; every item requires exact quotes from GM messages in this batch.',
  parameters:{type:'object',properties:{items:{type:'array',items:{type:'object',properties:{id:str,operation:{type:'string',enum:['add','update']},title:str,text:str,keywords:{type:'array',items:str},evidence:{type:'array',items:{type:'object',properties:{message_index:{type:'integer'},quote:str},required:['message_index','quote'],additionalProperties:false}}},required:['id','operation','title','text','keywords','evidence'],additionalProperties:false}}},required:['items'],additionalProperties:false},
  async execute(_id,p){return {...wrap(session.submit(p)),terminate:true};}});
 pi.on('session_start',()=>pi.setActiveTools(['read_snapshot','submit_lore']));
 pi.on('tool_call',e=>{if(!['read_snapshot','submit_lore'].includes(e.toolName))return {block:true,reason:'Snapshot tools only'};});
}
