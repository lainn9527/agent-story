"""Exploration materials are opt-in authoring aids, not stored story facts."""
import json

import pytest

from tests.test_card_ensemble import world, make
from story_core.card_mode.card import card_path, validate_card


@pytest.mark.parametrize('cast', [False, True])
def test_materials_opt_in_state_and_no_story_writes(world, cast):
    root, client, _ = world
    slug = 'original'
    assert client.get('/api/card/original/state').json['exploration_materials'] == []
    path = card_path(slug)
    card = json.loads(open(path).read())
    card['exploration_material_bank'] = 'xianxia'
    with open(path, 'w') as f:
        json.dump(card, f, ensure_ascii=False)
    assert not validate_card(card)
    if cast:
        make(world)
        slug = 'cast_test'
    folder = root / 'stories' / slug
    before = {str(p): p.read_bytes() for p in folder.rglob('*.json')}
    data = client.get(f'/api/card/{slug}/state').json
    materials = data['exploration_materials']
    assert len(materials) == 48
    assert len({m['id'] for m in materials}) == 48
    assert all(m['seed'] and set(m) == {'id', 'title', 'seed', 'size'} for m in materials)
    assert sum(m['size'] == 'small' for m in materials) == 24
    assert sum(m['size'] == 'large' for m in materials) == 24
    assert not any(m['seed'] in str(data['messages']) for m in materials)
    assert before == {str(p): p.read_bytes() for p in folder.rglob('*.json')}


@pytest.mark.parametrize('bank', ['../../secret', [], 1])
def test_reject_unknown_material_bank(world, bank):
    card = json.loads(open(card_path('original')).read())
    card['exploration_material_bank'] = bank
    assert any('exploration_material_bank' in e for e in validate_card(card))
