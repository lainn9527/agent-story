import {readFileSync} from 'node:fs';
import {ReviewSession} from './pi_review_guard.mjs';

export default function(pi) {
  const session = new ReviewSession(JSON.parse(readFileSync(process.env.STORY_PI_SNAPSHOT, 'utf8')));
  const string = {type: 'string'};
  const wrap = value => ({content: [{type: 'text', text: JSON.stringify(value)}], details: value});
  pi.registerTool({
    name: 'read_snapshot', label: 'Read snapshot', executionMode: 'sequential',
    description: 'Read a whitelisted lore/source id, 2400 characters per page. Follow next_offset until null. No file paths.',
    parameters: {type: 'object', properties: {id: string, offset: {type: 'integer', minimum: 0}}, required: ['id'], additionalProperties: false},
    async execute(_id, params) { return wrap(session.read(params)); },
  });
  pi.registerTool({
    name: 'submit_review', label: 'Submit verified review', executionMode: 'sequential',
    description: 'Submit exactly one check for every claim, after reading all lore and source pages. Supported/conflict require verbatim source quotes. Rejected submissions can be corrected.',
    parameters: {type: 'object', properties: {checks: {type: 'array', items: {
      type: 'object', properties: {claim_id: string,
        verdict: {type: 'string', enum: ['supported', 'conflict', 'insufficient']}, reason: string,
        evidence: {type: 'array', items: {type: 'object', properties: {source_id: string, quote: string}, required: ['source_id', 'quote'], additionalProperties: false}}},
      required: ['claim_id', 'verdict', 'reason', 'evidence'], additionalProperties: false,
    }}}, required: ['checks'], additionalProperties: false},
    async execute(_id, params) { return {...wrap(session.submit(params)), terminate: true}; },
  });
  // Even an unexpected tool load cannot expose the host filesystem to the model.
  pi.on('session_start', () => pi.setActiveTools(['read_snapshot', 'submit_review']));
  pi.on('tool_call', event => {
    if (!['read_snapshot', 'submit_review'].includes(event.toolName)) return {block: true, reason: 'Only snapshot review tools are allowed'};
  });
}
