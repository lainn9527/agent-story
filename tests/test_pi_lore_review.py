import copy

import pytest

from story_core.card_mode import lore
from story_core.card_mode.pi_lore_review import snapshot, still_current


def fixture():
    text = '算天說魔皇封印可能鬆動。'
    entry = {'id': 'threats', 'text': '封印可能鬆動。\n不指定必然突襲；具體敵人待定。',
             'sources': [{'index': 432, 'hash': lore.content_hash(text)}]}
    return entry, [{'index': 432, 'role': 'gm', 'content': text}]


def test_snapshot_scopes_and_covers_all_claims():
    entry, messages = fixture()
    messages += [{'index': 434, 'role': 'gm', 'content': '未列為來源的最新情節'}]
    frozen = snapshot(entry, messages)
    assert [s['message_index'] for s in frozen['sources']] == [432]
    assert [c['text'] for c in frozen['claims']] == ['封印可能鬆動。', '不指定必然突襲；', '具體敵人待定。']
    frozen['entry']['text'] = 'agent cannot mutate original'
    assert entry['text'].startswith('封印')


@pytest.mark.parametrize('mode', ['removed', 'regenerated', 'user_role'])
def test_snapshot_rejects_invalid_source(mode):
    entry, messages = fixture()
    if mode == 'removed': messages = []
    elif mode == 'regenerated': messages[0]['content'] = '同一編號換成別的原文'
    else: messages[0]['role'] = 'user'
    with pytest.raises(ValueError): snapshot(entry, messages)


def test_unsourced_author_settings_are_not_model_verified():
    with pytest.raises(ValueError): snapshot({'id': 'core', 'text': '作者設定'}, [])


def test_result_stales_if_source_or_lore_changes():
    entry, messages = fixture()
    frozen = snapshot(entry, messages)
    assert still_current(frozen, [entry], messages)
    changed = copy.deepcopy(entry); changed['text'] = '改版 lore'
    assert not still_current(frozen, [changed], messages)
    assert not still_current(frozen, [entry], [])
    assert not still_current(frozen, [], messages)
