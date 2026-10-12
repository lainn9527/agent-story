import copy
import json

import pytest

from story_core import messages_codec as codec, story_io as io


def _messages(turns=30):
    memory, rows = [], [{'index': 0, 'role': 'gm', 'content': 'opening',
                         'npcs_snapshot': [{'name': '李小蘭', 'memory': []}]}]
    for t in range(1, turns):
        before = [{'name': '李小蘭', 'role': '劍修', 'memory': list(memory)}]
        memory.append(f'第{t}回合的事實')
        if t % 7 == 0:  # maintenance dedup rewrites the middle of the list
            memory = [m for i, m in enumerate(memory) if i % 3] + ['合併後的記憶']
        rows.append({'index': 2 * t - 1, 'role': 'user', 'content': f'u{t}'})
        rows.append({'index': 2 * t, 'role': 'gm', 'content': f'g{t}',
                     'pre_npcs_snapshot': before,
                     'npcs_snapshot': [{'name': '李小蘭', 'role': '劍修', 'memory': list(memory)},
                                       {'name': 'odd', 'memory': [{'not': 'a string'}]}]})
    return rows


def test_round_trip_is_lossless_and_input_untouched():
    rows = _messages()
    original = copy.deepcopy(rows)
    doc = json.loads(json.dumps(codec.encode(rows)))
    assert rows == original
    assert codec.decode(doc) == original


def test_decoded_lists_are_independent():
    doc = json.loads(json.dumps(codec.encode(_messages())))
    rows = codec.decode(doc)
    rows[-1]['npcs_snapshot'][0]['memory'].append('x')
    assert 'x' not in rows[-1]['pre_npcs_snapshot'][0]['memory']


def test_pool_stores_each_string_once_and_runs_stay_short():
    doc = codec.encode(_messages(200))
    assert len(doc['memory_pool']) == len(set(doc['memory_pool']))
    snap = doc['messages'][-1]['pre_npcs_snapshot'][0]['memory']['$pool']
    assert len(snap) < 60  # runs, not one id per memory


def test_plain_lists_and_unknown_versions():
    assert codec.decode([{'role': 'gm'}]) == [{'role': 'gm'}]
    with pytest.raises(ValueError):
        codec.decode({'format': codec.FORMAT, 'version': 99, 'memory_pool': [], 'messages': []})


def test_save_pools_only_card_branch_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(io, 'STORY_DESIGN_DIR', str(tmp_path / 'design'))
    (tmp_path / 'design' / 'card_story').mkdir(parents=True)
    (tmp_path / 'design' / 'card_story' / 'card.json').write_text('{}')
    rows = _messages()
    card = tmp_path / 'stories' / 'card_story' / 'branches' / 'main' / 'messages.json'
    other = tmp_path / 'stories' / 'world_story' / 'branches' / 'main' / 'messages.json'
    for path in (card, other):
        io._save_json(str(path), rows)
        assert io._load_json(str(path), []) == rows
    assert codec.is_encoded(json.loads(card.read_text()))
    assert isinstance(json.loads(other.read_text()), list)
    assert not list(card.parent.glob('*.tmp.*'))
