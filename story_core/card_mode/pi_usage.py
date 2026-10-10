"""Pi event accounting. Preserve unknowns; never infer a bill from zero SDK prices."""
import json
from pathlib import Path

FIELDS = {'input_tokens': 'input', 'output_tokens': 'output', 'cache_read_tokens': 'cacheRead',
          'cache_write_tokens': 'cacheWrite', 'reasoning_tokens': 'reasoning', 'total_tokens': 'totalTokens'}


def events(path):
    if not Path(path).exists():
        return []
    rows = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        try:
            rows.append(json.loads(line))
        except (ValueError, TypeError):
            # Killed processes may leave a partial final line. Account known calls anyway.
            continue
    return rows


def phase(path, name):
    calls = []; pending = None; seen = set(); followups = 0; failures = 0
    operations = []; rejected = []
    for event in events(path):
        msg = event.get('message') or {}
        if event.get('type') == 'message_start' and msg.get('role') == 'assistant':
            if pending is not None:
                calls.append(pending)
            pending = {'stage': name, 'status': 'unfinished', 'usage': {k: None for k in FIELDS},
                       'cost_usd': None, 'activity': 'unknown', 'followup': max(0, followups-1)}
        if event.get('type') == 'message_end' and msg.get('role') == 'user':
            followups += 1
        if event.get('type') == 'message_end' and msg.get('role') == 'assistant':
            identity = msg.get('responseId') or msg.get('timestamp')
            if identity is not None and identity in seen:
                pending = None
                continue
            if identity is not None:
                seen.add(identity)
            raw = msg.get('usage') or {}
            # SDK error placeholders often contain zeroes, without a provider response.
            unknown = msg.get('stopReason') in ('error', 'aborted') and not msg.get('responseId')
            usage = {k: raw.get(v) if not unknown and type(raw.get(v)) in (int, float) and raw[v] >= 0 else None
                     for k, v in FIELDS.items()}
            tools = [x.get('name') for x in msg.get('content', []) if isinstance(x, dict) and x.get('type') == 'toolCall']
            activity = 'verification' if 'submit' in tools else 'proposal' if 'propose' in tools else 'lookup' if any(t in tools for t in ('read','search')) else 'other'
            calls.append({'stage': name, 'provider': msg.get('provider'), 'model': msg.get('model'),
                          'response_id': msg.get('responseId'), 'status': msg.get('stopReason'),
                          'activity': activity, 'requested_tools': tools, 'followup': max(0, followups-1),
                          'usage': usage, 'reported_usage': raw, 'cost_usd': None})
            pending = None
        if event.get('type') == 'tool_execution_end' and event.get('isError'):
            failures += 1
        if event.get('type') == 'tool_execution_end' and not event.get('isError'):
            try:
                value = json.loads(next(c['text'] for c in event['result']['content'] if c['type']=='text'))
                if 'operations' in value:
                    operations = value['operations']
                if 'checks' in value:
                    rejected = [c for c in value['checks'] if c.get('verdict') != 'supported']
            except (KeyError, StopIteration, TypeError, ValueError):
                pass
    if pending is not None:
        calls.append(pending)
    return {'name': name, 'calls': calls, 'visible_calls': len(calls), 'tool_failures': failures,
            'followups': max(0, followups-1), 'operations': operations, 'rejected_checks': rejected,
            **totals(calls)}


def totals(calls):
    known = {k: sum(c['usage'][k] for c in calls if c['usage'][k] is not None) for k in FIELDS}
    missing = {k: sum(c['usage'][k] is None for c in calls) for k in FIELDS}
    return {'tokens': {k: None if not calls or missing[k] else known[k] for k in FIELDS},
            'known_tokens': known, 'calls_missing_usage': missing, 'cost_usd': None}


def collect(work, frozen, report, elapsed_ms=None):
    work = Path(work)
    phases = [phase(work/'events.jsonl', 'independent_review' if frozen.get('review_operations') is not None else 'organize')]
    if (work/'audit'/'events.jsonl').exists():
        phases.append(phase(work/'audit'/'events.jsonl', 'independent_review'))
    for item in phases:
        rp = work/'audit'/'report.json' if item['name']=='independent_review' else work/'report.json'
        old = json.loads(rp.read_text()) if rp.exists() else {}
        item['elapsed_ms'] = old.get('phase_elapsed_ms')
    phases[0]['elapsed_ms'] = report.get('phase_elapsed_ms', phases[0]['elapsed_ms'])
    calls = [c for p in phases for c in p['calls']]
    ops = phases[0]['operations']
    return {'schema_version': 1, 'task_id': work.name, 'story_id': frozen['slug'], 'branch_id': frozen['branch_id'],
            'kinds': frozen['kinds'], 'focus': frozen['focus'], 'required_ids': frozen['required_ids'],
            'policy_version': frozen.get('policy_version', 1), 'model': report.get('model'),
            'status': report['status'], 'error': report.get('error'), 'apply': report.get('apply'),
            'apply_attempts': report.get('apply_attempts', []),
            'ever_applied': any(a.get('status')=='applied' for a in report.get('apply_attempts', [])),
            'elapsed_ms': elapsed_ms, 'visible_calls': len(calls), 'phases': phases,
            'usage_reported_calls': sum(bool(c.get('reported_usage')) for c in calls),
            'proposed': {'total': len(ops), 'add': sum(not o.get('target_ids') for o in ops),
                         'merge': sum(o['kind']=='memory' and len(o.get('target_ids',[]))>1 for o in ops),
                         'replace': sum(bool(o.get('target_ids')) and not(o['kind']=='memory' and len(o['target_ids'])>1) for o in ops)},
            **totals(calls),
            'accounting_note': 'Pi-reported counters; do not add reasoning/cache to totalTokens. Activities label calls, not separable costs. Provider-internal retries may be invisible. Missing usage is unknown; known_tokens is a lower bound. SDK cost is unpriced and excluded.'}
