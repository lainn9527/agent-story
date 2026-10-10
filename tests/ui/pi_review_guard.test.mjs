import assert from 'node:assert/strict';
import {ReviewSession, PAGE_CHARS} from '../../scripts/lore/pi_review_guard.mjs';
import {replay} from '../../scripts/lore/pi_review_replay.mjs';

const fixture = () => ({entry: {id: 'threats', text: '封印鬆動；敵人待定。'},
  sources: [{id: 'm:432', message_index: 432, hash: 'snapshot-hash', text: '算天報告：「封印鬆動。」'}],
  claims: [{id: 'c:0', text: '封印鬆動；'}, {id: 'c:1', text: '敵人待定。'}]});
const submission = () => ({checks: [
  {claim_id: 'c:0', verdict: 'supported', reason: '來源保留算天報告歸屬', evidence: [{source_id: 'm:432', quote: '封印鬆動。'}]},
  {claim_id: 'c:1', verdict: 'insufficient', reason: '沒有足以確認敵人的來源', evidence: []},
]});
function ready(snapshot = fixture()) {
  const s = new ReviewSession(snapshot);
  for (const id of s.documents.keys()) {
    let next = 0; while (next !== null) next = s.read({id, offset: next}).next_offset;
  }
  return s;
}

assert.throws(() => new ReviewSession(fixture()).submit(submission()), /Read all pages/);
assert.throws(() => ready().read({id: '../../llm_config.json'}), /Unknown snapshot/);
assert.throws(() => ready().read({id: 'm:432', offset: -1}), /Invalid offset/);
const long = fixture(); long.entry.text = '前段' + '文'.repeat(PAGE_CHARS) + '尾段';
const partial = new ReviewSession(long); partial.read({id: 'l:threats'}); partial.read({id: 'm:432'});
assert.throws(() => partial.submit(submission()), /l:threats/);
partial.read({id: 'l:threats', offset: PAGE_CHARS + 1}); // One-character gap.
assert.throws(() => partial.submit(submission()), /l:threats/);
partial.read({id: 'l:threats', offset: PAGE_CHARS});
assert.equal(partial.submit(submission()).all_supported, false);
const missing = submission(); missing.checks.pop();
assert.throws(() => ready().submit(missing), /Every claim/);
const duplicate = submission(); duplicate.checks[1].claim_id = 'c:0';
assert.throws(() => ready().submit(duplicate), /duplicate claim/);
const fakeQuote = submission(); fakeQuote.checks[0].evidence[0].quote = '敵人就是宗主';
assert.throws(() => ready().submit(fakeQuote), /verbatim/);
const unknownSource = submission(); unknownSource.checks[0].evidence[0].source_id = 'm:999';
assert.throws(() => ready().submit(unknownSource), /Invalid source/);
const empty = submission(); empty.checks[0].evidence = [];
assert.throws(() => ready().submit(empty), /require exact/);
const accepted = ready().submit(submission());
assert.deepEqual(accepted.fully_read_ids, ['l:threats', 'm:432']);
assert.equal(accepted.checks[0].evidence[0].quote, '封印鬆動。');

// Host reconstructs actual successful reads; free-text claims of completion do not count.
const events = [], session = new ReviewSession(fixture());
function call(name, args) {
  const id = String(events.length);
  events.push({type: 'tool_execution_start', toolCallId: id, toolName: name, args});
  const result = name === 'read_snapshot' ? session.read(args) : session.submit(args);
  events.push({type: 'tool_execution_end', toolCallId: id, toolName: name, isError: false,
    result: {content: [{type: 'text', text: JSON.stringify(result)}], details: result}});
}
call('read_snapshot', {id: 'l:threats'}); call('read_snapshot', {id: 'm:432'});
call('submit_review', submission()); events.push({type: 'agent_end'});
assert.deepEqual(replay(fixture(), events), accepted);
assert.throws(() => replay(fixture(), [{type: 'agent_end', messages: ['核對完成']}]), /No completed/);
const tampered = structuredClone(events); tampered[1].result.content[0].text = '{}';
assert.throws(() => replay(fixture(), tampered), /does not match/);
assert.throws(() => replay(fixture(), events.slice(0, -1)), /No completed/);
assert.throws(() => replay(fixture(), [...events, {type: 'message_end', message: {stopReason: 'length'}}]), /Incomplete/);
console.log('PASS: unread/partial/gapped pages, claim coverage, exact quotes, source allowlist, replay tampering, free-text completion, terminal failure');
