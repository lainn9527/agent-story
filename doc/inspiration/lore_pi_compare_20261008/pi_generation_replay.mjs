import {readFileSync} from 'node:fs';
import {isDeepStrictEqual} from 'node:util';
import {DraftSession} from './pi_generation_guard.mjs';
const s=new DraftSession(JSON.parse(readFileSync(process.argv[2],'utf8'))),pending=new Map();let ended=false;
for(const line of readFileSync(process.argv[3],'utf8').split('\n').filter(Boolean)){
 const e=JSON.parse(line);
 if(e.type==='tool_execution_start')pending.set(e.toolCallId,e);
 if(e.type==='tool_execution_end'){
  const start=pending.get(e.toolCallId);if(!start)throw Error('Missing tool start');pending.delete(e.toolCallId);
  if(e.isError)continue;
  const expected=start.toolName==='read_snapshot'?s.read(start.args):start.toolName==='submit_lore'?s.submit(start.args):null;
  if(!expected||!isDeepStrictEqual(expected,JSON.parse(e.result.content.find(c=>c.type==='text').text)))throw Error('Snapshot receipt mismatch');
 }
 if(e.type==='message_end'&&['length','error','aborted'].includes(e.message?.stopReason))throw Error('Incomplete model turn');
 if(e.type==='agent_end')ended=true;
}
if(!ended||pending.size||!s.accepted)throw Error('No verified complete submission');
process.stdout.write(JSON.stringify(s.accepted));
