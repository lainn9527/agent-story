from copy import deepcopy

from story_core import story_io as io
from story_core.card_mode import lore


def setup_lore(tmp_path, monkeypatch):
    monkeypatch.setattr(io, 'STORIES_DIR', str(tmp_path))
    rows = [{'index': 0, 'role': 'gm', 'content': '開場'},
            {'index': 2, 'role': 'gm', 'content': '已建立的世界'}]
    doc = {'schema_version': 1, 'entries': [
        {'id': 'core', 'title': '核心', 'text': '固定規則', 'always': True},
        {'id': 'local', 'title': '迎星峰', 'text': '當地設施',
         'keywords': ['迎星峰'], 'related': ['device']},
        {'id': 'device', 'title': '算天', 'text': '推演器靈', 'keywords': ['算天']},
        {'id': 'remote', 'title': '九幽魔域', 'text': '遠方情報',
         'keywords': ['九幽'], 'sources': [{'index': 2, 'hash': lore.content_hash(rows[1]['content'])}]},
    ]}
    io._save_json(str(lore.path('s', 'main')), doc)
    return rows, doc


def test_location_selects_related_lore_without_remote_facts(tmp_path, monkeypatch):
    rows, _ = setup_lore(tmp_path, monkeypatch)
    result = lore.grounding('s', 'main', rows, '繼續', '迎星峰')
    assert '固定規則' in result and '當地設施' in result and '推演器靈' in result
    assert '遠方情報' not in result
    assert result == lore.grounding('s', 'main', rows, '繼續', '迎星峰')


def test_user_can_retrieve_offscene_lore(tmp_path, monkeypatch):
    rows, _ = setup_lore(tmp_path, monkeypatch)
    assert '遠方情報' in lore.grounding('s', 'main', rows, '九幽是什麼', '迎星峰')


def test_rewind_and_regen_cannot_reuse_discarded_source(tmp_path, monkeypatch):
    rows, _ = setup_lore(tmp_path, monkeypatch)
    assert '遠方情報' not in lore.grounding('s', 'main', rows[:1], '九幽', '')
    changed = deepcopy(rows); changed[1]['content'] = '同編號另一候選'
    assert '遠方情報' not in lore.grounding('s', 'main', changed, '九幽', '')


def test_fork_inherits_separate_file_and_filters_future(tmp_path, monkeypatch):
    rows, _ = setup_lore(tmp_path, monkeypatch)
    lore.inherit('s', 'main', 'child')
    assert lore.path('s', 'child').exists()
    assert '遠方情報' not in lore.grounding('s', 'child', rows[:1], '九幽', '')
    child = io._load_json(str(lore.path('s', 'child')), {})
    child['entries'][0]['text'] = '分支獨有規則'
    io._save_json(str(lore.path('s', 'child')), child)
    assert '分支獨有規則' not in lore.grounding('s', 'main', rows, '', '')


def test_char_budget_and_no_random_fallback(tmp_path, monkeypatch):
    rows, _ = setup_lore(tmp_path, monkeypatch)
    result = lore.grounding('s', 'main', rows, '', '')
    assert '固定規則' in result and '當地設施' not in result
    monkeypatch.setattr(lore, 'MAX_CHARS', 10)
    result = lore.grounding('s', 'main', rows, '九幽', '迎星峰')
    assert '當地設施' not in result and '遠方情報' not in result


def test_cards_without_curated_lore_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(io, 'STORIES_DIR', str(tmp_path))
    assert lore.grounding('unrelated', 'main', [], '', '') == ''
