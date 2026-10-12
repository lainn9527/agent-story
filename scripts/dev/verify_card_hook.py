"""Opt-in hook trial using frozen player history in isolated Card Mode saves.

Only the first narration receives a short seed. Subsequent turns use ordinary
input; a second path resumes that first turn to test a player's change of mind.
The real narrator and Recorder run when --live is supplied. No UI integration.
"""
import argparse
from copy import deepcopy
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import random
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from story_core import story_io as io, compaction, llm_bridge, messages_codec, usage_db
from story_core.branch_tree import get_full_timeline
from story_core.card_mode import branches, pipeline
from story_core.card_mode.card import load_card

BANK = ROOT / 'story_design/_shared/xianxia_hooks_v1.json'
INITIAL_INPUT = '（請探索，給我一個可以接著玩的引子。）'
CONTINUE_INPUT = '（請繼續發展。）'
STEER_INPUT = '這件事我先不接，請把東西帶回去。我想先出門逛坊市。'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def hook_prompt(seed):
    return ('\n\n# 本輪探索素材（创作起點，尚非已發生的事）\n'
            + seed + '\n'
            '依眼前局勢改寫成一個具體可回應的入口。未定的細節可由你與玩家的後續互動共同形成，'
            '不預先指定答案或結局。素材不是角色已知的情報或共同往事；以當下新事件展開。'
            '素材涉及的人物、勢力與規則已有設定時承接既定背景；尚未建立時，改成首次接觸、新發現或正在形成的事件，不憑空補寫本線既有的盟約、道統淵源或共同歷史。渡劫、飛升、幽冥等牽動世界規則的題材，先呈現具體現象、一次嘗試或有歸屬的說法，不由旁白直接宣布普世規則。新遭遇可以真實發生，不必一律降成傳聞；與已確立設定衝突時改寫素材。')


def freeze(slug, bid):
    with branches.lock(slug, bid):
        rows = get_full_timeline(slug, bid)
        branches.ensure_settled(rows)
        if not rows or rows[-1].get('role') != 'gm':
            raise ValueError('Wait for a complete GM turn before freezing')
        if rows[-1].get('card_record_status') in ('pending', 'error'):
            raise ValueError('Latest Recorder has not completed')
        folder = Path(io._branch_dir(slug, bid))
        names = ['character_state.json', 'npcs.json', 'card_clock.json',
                 'conversation_recap.json', 'card_lore.json', 'card_instruction.json']
        files = {name: io._load_json(str(folder/name), {}) for name in names if (folder/name).exists()}
        return {'slug': slug, 'source_branch': bid, 'through_index': rows[-1]['index'],
                'card': load_card(slug), 'messages': rows, 'files': files}


def stage(snapshot, root):
    slug = snapshot['slug']
    folder = root/slug/'branches/main'
    write(root/slug/'timeline_tree.json', {'active_branch_id': 'main', 'branches': {
        'main': {'id': 'main', 'card_full_snapshot': True, 'parent_branch_id': None}}})
    write(folder/'messages.json', snapshot['messages'])
    for name, value in snapshot['files'].items():
        write(folder/name, value)
    return folder


def checkpoint(snapshot, folder):
    return {**snapshot, 'messages': messages_codec.decode(json.loads((folder/'messages.json').read_text())),
            'files': {name: json.loads((folder/name).read_text()) for name in snapshot['files']}}


def run_path(snapshot, output, inputs, seed=None, *, generate=None, recorder=None):
    """Use the real prompt/clock/state/recorder pipeline, with optional test doubles."""
    root = output/'sandbox'
    live_root = Path(io.STORIES_DIR).resolve()
    if root.resolve().is_relative_to(live_root):
        raise ValueError('Sandbox cannot be inside live story data')
    if root.exists():
        raise ValueError('Use a fresh path; prior trial evidence cannot be overwritten')
    folder = stage(snapshot, root)
    config = deepcopy(llm_bridge._get_config())
    cfg = config.get(config.get('provider'), {})
    report = {'source_through_index': snapshot['through_index'], 'seed': seed,
              'provider': config.get('provider'), 'model': cfg.get('model'),
              'recorder_model': cfg.get('recorder_model'), 'turns': []}
    after_first = None
    def persist():
        write(output/'report.json', report)
        (output/'dialogue.txt').write_text('\n\n' + ('\n\n' + '='*32 + '\n\n').join(
            f"第 {t['round']} 輪\n玩家：{t['input']}\n\nGM：\n{t.get('text', '（未完成）')}"
            for t in report['turns']))
    with ExitStack() as stack:
        stack.enter_context(patch.object(io, 'STORIES_DIR', str(root)))
        stack.enter_context(patch.object(compaction, 'STORIES_DIR', str(root)))
        stack.enter_context(patch.object(usage_db, 'STORIES_DIR', str(root)))
        stack.enter_context(patch.object(usage_db, '_initialized', set()))
        stack.enter_context(patch.object(pipeline, 'load_card', lambda _: deepcopy(snapshot['card'])))
        stack.enter_context(patch.object(llm_bridge, '_get_config', lambda: deepcopy(config)))
        # These short trials retain all unsummarized new turns. No asynchronous
        # work may outlive this isolated process scope or escape its data root.
        stack.enter_context(patch.object(compaction, 'compact_async', lambda *a, **kw: None))
        for number, user in enumerate(inputs, 1):
            row = {'round': number, 'input': user, 'status': 'started'}
            report['turns'].append(row)
            persist()
            def narrate(user_msg, system_prompt, recent, **unused):
                if number == 1 and seed:
                    system_prompt += hook_prompt(seed)
                request = {'user_msg': user_msg, 'system_prompt': system_prompt,
                           'recent': [{k:m[k] for k in ('role','content','index') if k in m} for m in recent]}
                write(output/f'round-{number}-request.json', request)
                row['request_sha256'] = digest(request)
                call = generate or llm_bridge.call_claude_gm
                started = time.monotonic()
                text, _ = call(user_msg, system_prompt, request['recent'])
                row.update(elapsed_seconds=round(time.monotonic()-started, 2),
                           usage=deepcopy(llm_bridge.get_last_usage()))
                if not text or text.startswith(('【系統錯誤】','【系統提示】')):
                    return None, 'failed'
                return text, 'blocking'
            try:
                # Reset only the runner's memory sampling for reproducible input
                # construction. This does not seed the remote model's output.
                random.seed(61008+number)
                with patch.object(pipeline, '_generate_pass1', narrate):
                    result = pipeline.run_card_turn(snapshot['slug'], 'main', user,
                        async_recorder=False, recorder_fn=recorder,
                        response_length='medium', recall_mode='off',
                        narrative_style='natural', plot_mode='coauthor', scene_mode='default')
                if not result.get('ok'):
                    raise ValueError('Narration failed; no accepted output for this turn')
                rows = messages_codec.decode(json.loads((folder/'messages.json').read_text()))
                gm = rows[-1]
                row.update(text=gm['content'], gm_index=gm['index'],
                           record_status=gm.get('card_record_status'), status='completed')
                if gm.get('card_record_status') != 'done':
                    row['status'] = 'recording_failed'
                    persist()
                    break
                if number == 1:
                    after_first = checkpoint(snapshot, folder)
            except Exception as error:
                row.update(status='failed', error=f'{type(error).__name__}: {error}')
                persist()
                break
            persist()
            print(output.name, number, row['status'], len(row.get('text','')), flush=True)
    report['complete'] = len(report['turns']) == len(inputs) and all(t['status']=='completed' for t in report['turns'])
    persist()
    return report, after_first


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--slug', default='xianmen_last_night')
    parser.add_argument('--branch', default='main')
    parser.add_argument('--seed', default='ordinary_object')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output must be a new directory')
    if args.output.resolve().is_relative_to(Path(io.STORIES_DIR).resolve()):
        parser.error('Output cannot be inside live story data')
    bank = json.loads(BANK.read_text())
    material = next(e for e in bank['entries'] if e['id']==args.seed)
    snapshot = freeze(args.slug, args.branch)
    args.output.mkdir(parents=True)
    write(args.output/'snapshot.json', snapshot)
    write(args.output/'manifest.json', {
        'kind':'current_save_isolated_trial_not_historical_replay',
        'source_through_index':snapshot['through_index'], 'snapshot_sha256':digest(snapshot),
        'material':material, 'seed_injected_once':True, 'live':args.live,
        'controls':{'narrative_style':'natural','plot_mode':'coauthor','recall_mode':'off',
                    'response_length':'medium','recorder':'normal synchronous in sandbox',
                    'compaction':'disabled for bounded trial'},
        'criteria':['入口可回應且未預定結局','後續有人物行動或局勢改變',
                    '重要資訊與前輪連續','玩家改向後不強拉回原案',
                    '未將素材冒充既有往事'],
        'implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    print('Frozen through',snapshot['through_index'],'material',material['id'],flush=True)
    if not args.live:
        print('Snapshot only; --live required for model calls',flush=True)
        return
    report, initial = run_path(snapshot, args.output/'continue', [INITIAL_INPUT]+[CONTINUE_INPUT]*4, material['seed'])
    if initial:
        run_path(initial, args.output/'steer', [STEER_INPUT,CONTINUE_INPUT])
    # A player may keep playing during this test. Compare only the frozen prefix,
    # rather than incorrectly treating additional live turns as experiment writes.
    current = get_full_timeline(args.slug,args.branch)
    write(args.output/'source_check.json', {
        'isolated_data_root':True, 'source_through_index':snapshot['through_index'],
        'frozen_prefix_still_matches':digest(current[:len(snapshot['messages'])])==digest(snapshot['messages']),
        'current_message_count':len(current), 'frozen_message_count':len(snapshot['messages']),
        'no_formal_branch_created':True})
    print('Trial saved:',args.output, 'continue complete:',report['complete'],flush=True)


if __name__=='__main__':
    main()
