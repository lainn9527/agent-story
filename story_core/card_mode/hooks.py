"""Opt-in short creative seeds; reading these never changes story canon."""
import json
import logging
from pathlib import Path

BANKS = {'xianxia': Path(__file__).resolve().parents[2] / 'story_design/_shared/xianxia_hooks_v1.json'}
log = logging.getLogger('rpg')


def materials(card):
    bank = card.get('exploration_material_bank')
    if not isinstance(bank, str) or bank not in BANKS:
        return []
    try:
        entries = json.loads(BANKS[bank].read_text())['entries']
        return [{'id': e['id'], 'title': e['title'], 'seed': e['seed'], 'size': e.get('size', 'small')}
                for e in entries]
    except (OSError, ValueError, KeyError, TypeError):
        log.exception('Cannot read Card Mode exploration bank %s', bank)
        return []
