"""Exercise style selection at actual generation and HTTP boundaries."""
import json
from pathlib import Path
import pytest
from tests.test_card_ensemble import world, make
from tests.test_card_branches import no_live_compaction
from tests.test_card_editing import body
from story_core.card_mode.legacy_contract import LEGACY_ROLEPLAY_CONTRACT
from story_core import story_io as io
from story_core.card_mode import pipeline, ensemble as e, styles


def test_default_preserves_prompt_exactly():
    original = '角色、記憶與原有寫法\n'
    assert styles.apply_style(original) == original
    assert styles.apply_style(original, 'original') == original
    assert styles.apply_style(original, 'natural') == original + '\n\n' + styles.NATURAL_INTERACTION


@pytest.mark.parametrize('plot', ['explore', 'coauthor', 'legacy_20260908'])
@pytest.mark.parametrize('cast', [False, True])
def test_style_flows_through_send_regenerate_edit_and_switch_back(world, monkeypatch, cast, plot):
    root, client, _ = world
    slug = 'original'
    prompts = []
    if cast:
        make(world); slug = 'cast_test'
        monkeypatch.setattr(e, 'generate', lambda u,s,r: prompts.append(s) or '她點頭：「好。」')
        monkeypatch.setattr(e, 'record', lambda *a: [])
    else:
        monkeypatch.setattr(pipeline, '_generate_pass1', lambda **kw: (prompts.append(kw['system_prompt']) or '她點頭：「好。」', 'test'))
        run = pipeline.run_card_turn
        def isolated(*a, **kw):
            kw.update(recorder_fn=lambda *a: None, async_recorder=False)
            return run(*a, **kw)
        monkeypatch.setattr('routes.card_routes.run_card_turn', isolated)
        monkeypatch.setattr(pipeline, 'run_card_turn', isolated)
    prefix = f'/api/card/{slug}'
    response = client.post(prefix+'/send/stream?sync=1', json={'message':'早安','narrative_style':'natural','plot_mode':plot,'scene_mode':'intimate'})
    assert response.status_code == 200
    assert b'"ok": true' in response.data or b'"ok":true' in response.data
    assert prompts[-1].count(styles.NATURAL_INTERACTION) == 1
    assert prompts[-1].count(styles.EXPLORATION if plot=='explore' else (styles.COAUTHOR_EXPLORATION if plot=='coauthor' else LEGACY_ROLEPLAY_CONTRACT.strip())) == 1
    assert prompts[-1].count(styles.INTIMATE_SCENE) == 1
    assert prompts[-1].count(styles.COAUTHOR_CONTINUITY) == (1 if plot == 'coauthor' else 0)
    response = client.post(prefix+'/regenerate', json={'narrative_style':'original', 'stream':True})
    assert response.status_code == 200
    assert b'"ok": true' in response.data or b'"ok":true' in response.data
    assert styles.NATURAL_INTERACTION not in prompts[-1]
    assert styles.EXPLORATION not in prompts[-1]
    assert styles.INTIMATE_SCENE not in prompts[-1]
    assert styles.COAUTHOR_CONTINUITY not in prompts[-1]
    messages = e.load(slug)['messages'] if cast else io._load_branch_messages(slug,'main')
    request = body(messages, mode='regenerate')
    request.update(narrative_style='natural', plot_mode=plot, scene_mode='intimate')
    response = client.post(prefix+'/edit',json=request)
    assert response.status_code == 200, response.data
    assert prompts[-1].count(styles.NATURAL_INTERACTION) == 1
    assert prompts[-1].count(styles.EXPLORATION if plot=='explore' else (styles.COAUTHOR_EXPLORATION if plot=='coauthor' else LEGACY_ROLEPLAY_CONTRACT.strip())) == 1
    assert prompts[-1].count(styles.INTIMATE_SCENE) == 1
    assert prompts[-1].count(styles.COAUTHOR_CONTINUITY) == (1 if plot == 'coauthor' else 0)
    assert prompts[-1].index(styles.NATURAL_INTERACTION) < prompts[-1].index('【修改指示】')
    messages = e.load(slug)['messages'] if cast else io._load_branch_messages(slug,'main')
    assert messages[-1]['narrative_style']=='natural'
    assert messages[-1]['plot_mode']==plot
    assert messages[-1]['scene_mode']=='intimate'
    assert styles.NATURAL_INTERACTION not in json.dumps(messages, ensure_ascii=False)


@pytest.mark.parametrize('cast', [False, True])
@pytest.mark.parametrize('endpoint', ['send/stream','regenerate','edit'])
@pytest.mark.parametrize('field', ['narrative_style','plot_mode','scene_mode'])
@pytest.mark.parametrize('bad', ['unknown', [], 123])
def test_invalid_style_does_not_mutate_story(world, cast, endpoint, field, bad):
    root, client, _ = world
    slug='original'
    if cast: make(world); slug='cast_test'
    folder=root/'stories'/slug
    before={str(p):p.read_bytes() for p in folder.rglob('*.json')}
    payload={'message':'早安',field:bad,'message_index':2,'mode':'regenerate',
             'destination':'fork','expected_reply':'不存在的回覆'}
    response=client.post(f'/api/card/{slug}/{endpoint}',json=payload)
    assert response.status_code==400
    assert before=={str(p):p.read_bytes() for p in folder.rglob('*.json')}


@pytest.mark.parametrize('style', ['original','natural'])
@pytest.mark.parametrize('plot', ['follow','explore','coauthor'])
@pytest.mark.parametrize('scene', ['default','intimate'])
def test_independent_options_and_order(style, plot, scene):
    expected = 'base'
    for enabled, rules in [(style=='natural', styles.NATURAL_INTERACTION),
                           (plot=='explore', styles.EXPLORATION),
                           (plot=='coauthor', styles.COAUTHOR_EXPLORATION),
                           (scene=='intimate', styles.INTIMATE_SCENE)]:
        if enabled: expected += '\n\n' + rules
    assert styles.apply_style('base', style, plot, scene) == expected


def test_coauthor_preserves_creative_brief_with_continuity_boundary():
    candidate = Path(__file__).resolve().parents[1]/"doc/inspiration/coauthor_exploration_candidate.txt"
    assert styles.COAUTHOR_EXPLORATION == candidate.read_text() + "\n" + styles.COAUTHOR_CONTINUITY


def test_legacy_replaces_actual_contract_and_preserves_grounding():
    from datetime import datetime
    from story_core.card_mode.prompt import build_card_system_prompt
    from story_core.card_mode.contract import ROLEPLAY_CONTRACT
    from story_core.card_mode.legacy_contract import LEGACY_ROLEPLAY_CONTRACT
    base = ROLEPLAY_CONTRACT.strip() + '\n\n角色與記憶資料'
    result = styles.apply_style(base, plot_mode='legacy_20260908')
    assert result == LEGACY_ROLEPLAY_CONTRACT.strip() + '\n\n角色與記憶資料'
    assert ROLEPLAY_CONTRACT.strip() not in result
    assert styles.COAUTHOR_CONTINUITY not in result
    assert styles.validate_plot_mode('legacy_20260908') == 'legacy_20260908'
