import {readFileSync} from 'node:fs';
import {MaintenanceSession} from './pi_maintenance_guard.mjs';
export default function(pi) {
  const s=new MaintenanceSession(JSON.parse(readFileSync(process.env.STORY_PI_SNAPSHOT,'utf8')));
  const str={type:'string'}, arr=items=>({type:'array',items});
  const obj=(properties,required=Object.keys(properties))=>({type:'object',properties,required,additionalProperties:false});
  const schemas={
    read:obj({id:str,offset:{type:'integer'}},['id']),
    search:obj({query:str,offset:{type:'integer'}},['query']),
    propose:obj({operations:arr(obj({kind:{type:'string',enum:['memory','lore','summary','inventory']},target_ids:arr(str),text:str,item_name:str,character:{type:'integer'},title:str,keywords:arr(str)},['kind','target_ids','text']))}),
    submit:obj({checks:arr(obj({claim_id:str,verdict:{type:'string',enum:['supported','conflict','insufficient']},reason:str,context_ids:arr(str),evidence:arr(obj({source_id:str,quote:str}))},['claim_id','verdict','reason','evidence']))}),
  };
  const descriptions={read:'Read a frozen record completely, following next_offset. No filesystem access.',search:'Search ALL original records and existing memory/lore by exact substring. Paginate; snippets do not count as reading.',propose:'After reading required records, propose bounded replacements/additions (empty list is valid). Returns a checklist for every new claim and replaced record.',submit:'Check every checklist item against fully read GM originals, with exact quotations; any uncertainty prevents all writes.'};
  for(const name of Object.keys(schemas))pi.registerTool({name,label:name,executionMode:'sequential',description:descriptions[name],parameters:schemas[name],
    async execute(_id,args){const value=s[name](args);return {content:[{type:'text',text:JSON.stringify(value)}],details:value,...(name==='submit'?{terminate:true}:{})};}});
  let reminders=0;
  pi.on('agent_end',()=>{if(!s.accepted && reminders++<2)pi.sendUserMessage('整理尚未完成。請接續已讀記錄，完成搜尋、propose 與逐項 submit；若無可確認的修改，提交空 operations 與空 checks。不要只停止或口頭回覆。',{deliverAs:'followUp'});});
  pi.on('session_start',()=>pi.setActiveTools(Object.keys(schemas)));
  pi.on('tool_call',e=>{if(!Object.hasOwn(schemas,e.toolName))return {block:true,reason:'Only maintenance snapshot tools are allowed'};});
}
