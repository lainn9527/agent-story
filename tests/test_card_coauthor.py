"""Replay isolation and future-context exclusion at the real generation boundary."""
from copy import deepcopy
from tests.test_card_ensemble import world
from scripts.dev.verify_card_coauthor import capture
from story_core import story_io as io


def test_capture_excludes_target_future_and_invalid_recap(world):
    root, _, _ = world
    folder = root/'stories'/'original'/'branches'/'main'
    state = io._load_json(str(folder/'character_state.json'), {})
    npcs = io._load_json(str(folder/'npcs.json'), [])
    clock = io._load_json(str(folder/'card_clock.json'), {})
    messages = [
        {'index':0,'role':'gm','content':'既有場景'},
        {'index':1,'role':'user','content':'先前玩家輸入'},
        {'index':2,'role':'gm','content':'先前角色回應'},
        {'index':3,'role':'user','content':'本輪玩家安排'},
        {'index':4,'role':'gm','content':'TARGET_ONLY_SENTINEL',
         'pre_state_snapshot':state,'pre_npcs_snapshot':npcs,'pre_clock_snapshot':clock},
        {'index':5,'role':'user','content':'FUTURE_ONLY_SENTINEL'},
    ]
    before = {p:p.read_bytes() for p in root.rglob('*.json')}
    original = deepcopy(messages)
    result = capture(messages,4,{'recap_text':'FUTURE_RECAP_SENTINEL','compacted_through_index':4},'original')
    req = result['request']
    assert req['user_msg'] == '本輪玩家安排'
    assert [m['index'] for m in req['recent']] == [0,1,2]
    assert not result['recap_preserved']
    assert all(s not in str(req) for s in ['TARGET_ONLY_SENTINEL','FUTURE_ONLY_SENTINEL','FUTURE_RECAP_SENTINEL'])
    assert messages == original
    assert before == {p:p.read_bytes() for p in root.rglob('*.json')}
    preserved = capture(messages,4,{'recap_text':'SAFE_EARLY_RECAP','compacted_through_index':1},'original')
    assert preserved['recap_preserved']
    assert 'SAFE_EARLY_RECAP' in preserved['request']['system_prompt']
