import {readFileSync} from 'node:fs';
import {isDeepStrictEqual} from 'node:util';
import {MaintenanceSession} from './pi_maintenance_guard.mjs';
export function replay(snapshot,events){
  const s=new MaintenanceSession(snapshot),pending=new Map();let ended=false;
  for(const e of events){
    if(e.type==='tool_execution_start'){
      ended=false;
      if(pending.has(e.toolCallId)||!['read','search','propose','submit'].includes(e.toolName))throw Error('Invalid tool start');
      pending.set(e.toolCallId,e);
    }
    if(e.type==='tool_execution_end'){
      const start=pending.get(e.toolCallId);if(!start)throw Error('Missing tool start');pending.delete(e.toolCallId);
      // Execute failures too: read/search budgets must match the live session.
      let expected,err;try{expected=s[start.toolName](start.args);}catch(error){err=error;}
      if(e.isError){if(!err)throw Error('Unexpected failed receipt');continue;}
      if(err)throw err;
      const observed=JSON.parse(e.result.content.find(c=>c.type==='text').text);
      if(!isDeepStrictEqual(expected,observed))throw Error('Receipt differs from frozen source');
    }
    if(e.type==='message_end'&&['length','error','aborted'].includes(e.message?.stopReason))throw Error('Incomplete model turn');
    if(e.type==='agent_end')ended=true;
  }
  if(!ended||pending.size||!s.accepted)throw Error('Task did not finish verification');
  return s.accepted;
}
if(process.argv[1]?.endsWith('pi_maintenance_replay.mjs')){
  try{process.stdout.write(JSON.stringify(replay(JSON.parse(readFileSync(process.argv[2],'utf8')),readFileSync(process.argv[3],'utf8').split('\n').filter(Boolean).map(JSON.parse))));}
  catch(e){process.stderr.write(e.message+'\n');process.exitCode=1;}
}
