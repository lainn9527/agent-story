"""Hook experiments keep live saves isolated and do not repeat the seed."""
from copy import deepcopy
import json

from tests.test_card_ensemble import world
from scripts.dev.verify_card_hook import freeze, run_path, INITIAL_INPUT, CONTINUE_INPUT, STEER_INPUT
from story_core import story_io as io


def test_seed_once_real_pipeline_and_player_steering_isolated(world, tmp_path):
    root, _, _ = world
    source = root/'stories/original'
    before = {str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()}
    snapshot = freeze('original','main')
    original = deepcopy(snapshot)
    calls = []
    def generate(user, system, recent):
        if not calls:
            assert '首次接觸、新發現或正在形成的事件' in system
            assert '不由旁白直接宣布普世規則' in system
        calls.append((user,system,recent))
        text = 'INTRO_FACT' if len(calls)==1 else 'CONTINUATION_ONLY_FACT'
        return '📅 2026/09/22　⏰ 10:05　📌 大學咖啡館　🌤️ 晴\n'+text, []
    def recorder(*args):
        pass
    report, first = run_path(snapshot,tmp_path/'main-trial',
                            [INITIAL_INPUT,CONTINUE_INPUT,CONTINUE_INPUT],
                            'UNSEEN_HOOK_SEED', generate=generate,recorder=recorder)
    assert report['complete'] and len(calls)==3
    assert 'UNSEEN_HOOK_SEED' in calls[0][1]
    assert all('UNSEEN_HOOK_SEED' not in c[1] for c in calls[1:])
    assert first['messages'][-1]['content'].endswith('INTRO_FACT')
    calls.clear()
    def steer(user,system,recent):
        calls.append((user,system,recent))
        return '📅 2026/09/22　⏰ 10:10　📌 坊市　🌤️ 晴\n依玩家改向。',[]
    steered, _ = run_path(first,tmp_path/'steer-trial',[STEER_INPUT,CONTINUE_INPUT],
                         generate=steer,recorder=recorder)
    assert steered['complete']
    assert calls[0][0]==STEER_INPUT
    assert 'CONTINUATION_ONLY_FACT' not in str(calls[0])
    assert 'INTRO_FACT' in str(calls[0][2])
    assert snapshot==original
    assert before=={str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()}
    assert json.loads((tmp_path/'main-trial/report.json').read_text())['complete']


def test_recording_failure_stops_trial(world,tmp_path):
    snapshot=freeze('original','main')
    calls=[]
    def generate(*args):
        calls.append(args)
        return '📅 2026/09/22　⏰ 10:05　📌 大學咖啡館　🌤️ 晴\n新的訊息。',[]
    def fail_record(*args):
        raise RuntimeError('Recorder failed')
    report,first=run_path(snapshot,tmp_path/'failed-trial',
                          [INITIAL_INPUT,CONTINUE_INPUT], '短引子',
                          generate=generate,recorder=fail_record)
    assert len(calls)==1 and not report['complete'] and first is None
    assert report['turns'][0]['status']=='recording_failed'
    assert report['turns'][0]['record_status']=='error'
