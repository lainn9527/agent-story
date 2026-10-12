"""Freeze real Card Mode replay inputs, then optionally compare prompt variants.

No live story writes, recorder, synthesized recap, or target-reply injection.
Historical request traces are unavailable: these are current-pipeline replays.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import runpy
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from story_core import story_io as io, compaction, llm_bridge, messages_codec
from story_core.card_mode import pipeline


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def capture(messages, target, recap, slug):
    pos = next(i for i, m in enumerate(messages) if m['index'] == target)
    old = messages[pos]
    assert old['role'] == 'gm' and messages[pos-1]['role'] == 'user'
    prefix = deepcopy(messages[:pos-1])
    captured = {}
    def intercept(**kw):
        captured.update({k: deepcopy(kw[k]) for k in ('user_msg', 'system_prompt', 'recent')})
        return None, 'failed'  # Stop before commit or recorder.
    with tempfile.TemporaryDirectory(prefix='card-coauthor-') as tmp:
        root = Path(tmp)
        branch = root / slug / 'branches/main'
        write(root / slug / 'timeline_tree.json', {'active_branch_id':'main', 'branches':{
            'main': {'id':'main', 'card_full_snapshot':True, 'parent_branch_id':None}}})
        write(branch / 'messages.json', prefix)
        for field, name in [('pre_state_snapshot','character_state.json'),
                            ('pre_npcs_snapshot','npcs.json'), ('pre_clock_snapshot','card_clock.json')]:
            write(branch / name, old[field])
        write(branch / 'conversation_recap.json', recap)
        with patch.object(io, 'STORIES_DIR', str(root)), patch.object(compaction, 'STORIES_DIR', str(root)):
            compaction.invalidate_recap(slug, 'main', retained_message_count=len(prefix))
            used_recap = compaction.load_recap(slug, 'main')
            with patch.object(pipeline, '_generate_pass1', intercept):
                result = pipeline.run_card_turn(slug, 'main', messages[pos-1]['content'],
                    narrative_style='natural', plot_mode='follow', scene_mode='default',
                    recall_mode='off', response_length='medium')
            assert not result['ok'] and captured
            assert messages_codec.decode(json.loads((branch/'messages.json').read_text())) == prefix
    # Never send state metadata from history to a provider or expose future snapshots.
    captured['recent'] = [{k:m[k] for k in ('role','content','index') if k in m} for m in captured['recent']]
    assert all(m['index'] < target-1 for m in captured['recent'])
    return {'target':target, 'request':captured, 'accepted_output':old['content'],
            'prefix_count':len(prefix), 'recap_preserved':bool(used_recap.get('recap_text')),
            'source_snapshot_hash':digest({k:old[k] for k in old if k.startswith('pre_')})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--old-styles', type=Path, required=True)
    parser.add_argument('--new-rules', type=Path, default=ROOT/'doc/inspiration/coauthor_exploration_candidate.txt')
    parser.add_argument('--branch', default='card_a2203bb7d67a')
    parser.add_argument('--targets', type=int, nargs='+', default=[424,426,428,438])
    parser.add_argument('--samples', type=int, choices=range(1,6), default=1)
    parser.add_argument('--live', action='store_true', help='Call provider for every target, variant and sample')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    assert not (args.output/'manifest.json').exists(), 'Use a fresh output directory'
    slug, branch = 'lixiaolan_v2', args.branch
    assert io._is_valid_story_id(branch), 'Invalid branch'
    source = Path(io.STORIES_DIR)/slug/'branches'/branch
    frozen = {p:p.read_bytes() for p in [source/'messages.json', source/'conversation_recap.json',
                                       ROOT/'story_design'/slug/'card.json']}
    messages = messages_codec.decode(json.loads(frozen[source/'messages.json']))
    recap = json.loads(frozen[source/'conversation_recap.json'])
    old_rules = runpy.run_path(str(args.old_styles))['EXPLORATION']
    new_rules = args.new_rules.read_text()
    cfg = deepcopy(llm_bridge._get_config())
    provider = cfg.get('provider')
    assert provider in ('vertex','gemini','openrouter'), 'Use a supported direct API provider'
    cases = [capture(messages, idx, recap, slug) for idx in args.targets]
    manifest = {'kind':'current_pipeline_reconstruction_not_historical_request',
                'source_branch':branch, 'samples_per_variant':args.samples, 'provider':provider, 'model':cfg.get(provider,{}).get('model'),
                'controls':{'narrative_style':'natural','response_length':'medium','recall_mode':'off','scene_mode':'default'},
                'old_exploration':old_rules, 'new_exploration':new_rules,
                'source_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(b).hexdigest() for p,b in frozen.items()},
                'cases':[{k:v for k,v in c.items() if k not in ('request','accepted_output')} for c in cases]}
    write(args.output/'manifest.json', manifest)
    tasks=[]
    for case in cases:
        write(args.output/f"{case['target']}-input.json", case)
        for variant,rules in [('old',old_rules),('new',new_rules)]:
            req=deepcopy(case['request']); req['system_prompt']+='\n\n'+rules
            write(args.output/f"{case['target']}-{variant}-request.json",req)
            for sample in range(1,args.samples+1):
                tasks.append((case['target'],variant,req,sample))
    print('Frozen cases; recent-message counts:',[len(c['request']['recent']) for c in cases],flush=True)
    if args.live:
        def run(task):
            idx,variant,req,sample=task; started=time.time()
            suffix = f"-sample{sample}" if args.samples>1 else ""
            text,_=llm_bridge.call_claude_gm(req['user_msg'],req['system_prompt'],req['recent'])
            ok=bool(text) and not text.startswith(('【系統錯誤】','【系統提示】'))
            write(args.output/f'{idx}-{variant}{suffix}-result.json',{'ok':ok,'text':text,
                'request_sha256':digest(req),'elapsed_seconds':time.time()-started,'usage':llm_bridge.get_last_usage()})
            print(idx,variant,sample,'OK' if ok else 'FAILED',len(text or ''),flush=True)
        with patch.object(llm_bridge,'_get_config',lambda:deepcopy(cfg)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(run,tasks))
    unchanged=all(p.read_bytes()==b for p,b in frozen.items())
    write(args.output/'source_check.json',{'source_files_unchanged':unchanged})
    print('Source files unchanged:',unchanged,flush=True)


if __name__=='__main__':
    main()
