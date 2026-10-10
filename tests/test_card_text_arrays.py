from copy import deepcopy
from datetime import datetime
import pytest
from story_core.card_mode.card import load_card, validate_card, init_card_story
from story_core.card_mode.prompt import build_card_system_prompt


@pytest.mark.parametrize('field', ['identity', 'appearance', 'personality'])
def test_string_and_array_roundtrip(field, tmp_path):
    card = deepcopy(load_card('hanako'))
    card['persona'][field] = ['第一項設定', '第二項設定']
    assert validate_card(card) == []
    init_card_story(card, stories_dir=str(tmp_path/'stories'), design_dir=str(tmp_path/'design'))
    loaded = load_card('hanako', design_dir=str(tmp_path/'design'))
    assert loaded['persona'][field] == card['persona'][field]
    prompt = build_card_system_prompt(loaded, {}, {}, datetime(2026, 5, 16))
    assert '- 第一項設定\n- 第二項設定' in prompt
    assert "['第一項設定'" not in prompt
    card['persona'][field] = '原有字串設定'
    assert validate_card(card) == []
    assert '原有字串設定' in build_card_system_prompt(card, {}, {}, datetime(2026, 5, 16))


@pytest.mark.parametrize('field', ['identity', 'appearance', 'personality'])
@pytest.mark.parametrize('value', [42, {}, ['有效項', 42], [''], [None]])
def test_invalid_items_are_rejected(field, value):
    card = deepcopy(load_card('hanako'))
    card['persona'][field] = value
    assert any(field in error for error in validate_card(card))


def test_personality_still_required():
    card = deepcopy(load_card('hanako'))
    card['persona']['personality'] = []
    assert any('personality' in error for error in validate_card(card))
