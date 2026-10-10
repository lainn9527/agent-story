"""Both narrator entry points must retain the shared multi-stage contract."""
from datetime import datetime
from copy import deepcopy
import pytest
from tests.test_card_ensemble import world, make
from story_core.card_mode import ensemble as e
from story_core.card_mode.card import load_card
from story_core.card_mode.prompt import build_card_system_prompt
from story_core.card_mode import styles, pipeline
from story_core import story_io as io, compaction


@pytest.mark.parametrize('mode', ['single', 'ensemble'])
def test_narrator_receives_ordered_input_and_player_override_rules(world,mode):
    if mode=='ensemble':
        make(world)
        prompt=e.prompt('cast_test',e.load('cast_test'),'我先遞茶，再展示照片，最後到陽台。','short')
    else:
        prompt=build_card_system_prompt(load_card('original'),{}, {'memory':[]},datetime(2026,9,22),response_length='short')
    assert '接住最新整則輸入' in prompt
    assert prompt.count('## 人物立場與未解決的矛盾') == 1
    assert '人物自辯不等於敘事事實' in prompt
    assert '變化只解決它實際處理的問題' in prompt
    assert '角色台詞不是作者裁決' in prompt
    assert '此授權優先於契約、小說化承接及文風／劇情選項中的不代寫玩家限制' in prompt
    assert '只對本輪授權生效' in prompt
    assert '先前回合已演完的動作不重演' in prompt
    assert '每輪完成一個小互動或提供一點新資訊' not in prompt
    assert prompt.count('## 多段發展的演出節奏')==1
    assert '每個有互動意義的環節' in prompt
    assert '同一則輸入可以包含多次對話交換' in prompt
    assert '不要等玩家整段講完才統一回答' in prompt
    assert '對方尚未聽到後句時，不提前用後句的資訊回答前句' in prompt
    assert '不因每個句號、換行都插入反應' in prompt
    assert '等說完再回應' in prompt
    assert '尚未取得的同意、玩家選擇或未完成的條件' in prompt
    assert '明確要求略過過程、概述、快轉' in prompt


def test_weave_player_input_is_opt_in_per_card():
    card = load_card('hanako')
    plain = build_card_system_prompt(card, {}, {'memory': []}, datetime(2026, 9, 22))
    assert '# 小說化承接' not in plain
    card['output'] = {**card.get('output', {}), 'weave_player_input': True}
    woven = build_card_system_prompt(card, {}, {'memory': []}, datetime(2026, 9, 22))
    assert woven.count('# 小說化承接') == 1
    assert '台詞按原意潤飾' in woven
    assert '給 GM 的指示不入文' in woven
    assert '不能把不同對話時點的話合併成一次講完的長台詞' in woven


def test_lixiaolan_v2_weaves_player_input():
    card = load_card('lixiaolan_v2')
    prompt = build_card_system_prompt(card, {}, {'memory': []}, datetime(2026, 9, 22))
    assert '# 小說化承接' in prompt


@pytest.mark.parametrize('length', [None, 'short', 'long'])
@pytest.mark.parametrize('style', ['original', 'natural'])
@pytest.mark.parametrize('plot', ['follow', 'explore', 'coauthor'])
def test_lixiaolan_prompt_has_no_verbatim_or_dialogue_ending_conflict(length, style, plot):
    card = load_card('lixiaolan_v2')
    original = deepcopy(card)
    prompt = styles.apply_style(build_card_system_prompt(
        card, {}, {'memory': []}, datetime(2026, 9, 22), response_length=length), style, plot)
    assert '潤飾措辭、語氣與節奏' in prompt
    assert '保留否定、條件、不確定性與承諾程度' in prompt
    assert '玩家明確要求逐字保留' in prompt
    assert '沒有固定收尾形式' in prompt
    assert '探索與共同創作可以主動發展，但不必每輪在結尾塞進新鉤子' in prompt
    for conflict in ['保留原本措辭', '只可修正標點與明顯錯字', '原句承接',
                     '角色說完後停下', '遠端訊息以最後一則角色訊息結束']:
        assert conflict not in prompt
    assert card == original


@pytest.mark.parametrize('cast', [False, True])
def test_live_generation_boundary_receives_polish_rules_without_rewriting_input(world, monkeypatch, cast):
    from story_core.card_mode.card import card_path
    _, client, _ = world
    monkeypatch.setattr(compaction, 'compact_async', lambda *a, **k: None)
    card = load_card('original'); card['output']['weave_player_input'] = True
    io._save_json(card_path('original'), card)
    slug = 'original'; captured = []
    # Model output is stubbed: this checks prompt delivery and persistence,
    # not whether an actual model follows the writing instructions.
    output = '「我只是想陪你，你不用現在回答。」她把杯子放回桌上。'
    if cast:
        make(world); slug = 'cast_test'
        monkeypatch.setattr(e, 'generate', lambda user, system, recent: captured.append((user, system)) or output)
        monkeypatch.setattr(e, 'record', lambda *a: [])
    else:
        monkeypatch.setattr(pipeline, '_generate_pass1', lambda **kw:
            (captured.append((kw['user_msg'], kw['system_prompt'])) or output, 'test'))
        run = pipeline.run_card_turn
        def isolated(*a, **kw):
            kw.update(recorder_fn=lambda *a: None, async_recorder=False)
            return run(*a, **kw)
        monkeypatch.setattr('routes.card_routes.run_card_turn', isolated)
    user = '我只是想陪妳 妳不需要現在回荅我 (請潤飾語氣，不要增加承諾)'
    response = client.post(f'/api/card/{slug}/send/stream?sync=1', json={
        'message': user, 'narrative_style': 'natural', 'plot_mode': 'coauthor', 'response_length': 'short'})
    assert b'"type": "done"' in response.data
    assert response.status_code == 200 and captured
    assert user in captured[-1][0]
    assert '台詞按原意潤飾' in captured[-1][1]
    assert '潤飾不改表態' in captured[-1][1]
    assert '沒有固定收尾形式' in captured[-1][1]
    assert '給 GM 的指示不入文' in captured[-1][1]
    assert '同一則輸入可以包含多次對話交換' in captured[-1][1]
    assert '不能把不同對話時點的話合併成一次講完的長台詞' in captured[-1][1]
    assert '不機械地每句插話' in captured[-1][1]
    messages = e.load(slug)['messages'] if cast else io._load_branch_messages(slug, 'main')
    assert messages[-2]['content'] == user
    assert messages[-1]['content'] == output
