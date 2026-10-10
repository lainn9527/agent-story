import {readFileSync} from 'node:fs';
import {isDeepStrictEqual} from 'node:util';
import {ReviewSession} from './pi_review_guard.mjs';

export function replay(snapshot, events) {
  const session = new ReviewSession(snapshot), pending = new Map();
  let ended = false;
  for (const event of events) {
    if (event.type === 'tool_execution_start') pending.set(event.toolCallId, event);
    if (event.type === 'tool_execution_end') {
      const start = pending.get(event.toolCallId);
      if (!start) throw Error('Missing tool start');
      pending.delete(event.toolCallId);
      if (event.isError) continue;
      const expected = start.toolName === 'read_snapshot' ? session.read(start.args)
        : start.toolName === 'submit_review' ? session.submit(start.args) : null;
      if (!expected) throw Error('Unexpected tool');
      const observed = JSON.parse(event.result.content.find(item => item.type === 'text').text);
      if (!isDeepStrictEqual(observed, expected)) throw Error('Tool result does not match the host snapshot');
    }
    if (event.type === 'message_end' && ['length', 'error', 'aborted'].includes(event.message?.stopReason)) throw Error('Incomplete model turn');
    if (event.type === 'agent_end') ended = true;
  }
  if (!ended || pending.size || !session.accepted) throw Error('No completed, verified submission');
  return session.accepted;
}

if (process.argv[1]?.endsWith('pi_review_replay.mjs')) {
  try {
    const snapshot = JSON.parse(readFileSync(process.argv[2], 'utf8'));
    const events = readFileSync(process.argv[3], 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line));
    process.stdout.write(JSON.stringify(replay(snapshot, events)));
  } catch (error) {
    process.stderr.write(error.message + '\n'); process.exitCode = 1;
  }
}
