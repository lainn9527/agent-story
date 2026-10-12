import json
from pathlib import Path

from tests.test_card_ensemble import world
from story_core import story_io as io
from story_core.card_mode import lore


def seed():
    rows = io._load_json(io._story_messages_path('original', 'main'), [])
    rows.append({'index': 1, 'role': 'user', 'content': '去迎星峰'})
    rows.append({'index': 2, 'role': 'gm', 'content': '迎星峰的超算陣啟動了'})
    io._save_json(io._story_messages_path('original', 'main'), rows)
    io._save_json(str(lore.path('original', 'main')), {'schema_version': 1, 'entries': [
        {'id': 'core', 'title': '核心', 'text': '固定規則', 'always': True},
        {'id': 'peak', 'title': '迎星峰', 'text': '艾迪居所', 'keywords': ['迎星峰'],
         'sources': [{'index': 2, 'hash': lore.content_hash(rows[2]['content'])}],
         'maintenance_anchor': {'index': 2, 'hash': lore.content_hash(rows[2]['content'])}},
        {'id': 'gone', 'title': '已捨棄', 'text': '被重生掉的世界',
         'sources': [{'index': 2, 'hash': lore.content_hash('另一個候選')}]},
    ]})


def test_lists_visible_entries_with_categories(world):
    _, client, _ = world; seed()
    data = client.get('/api/card/original/lore').json
    assert [e['id'] for e in data['entries']] == ['core', 'peak']
    peak = data['entries'][1]
    assert peak['category'] == '' and peak['origin'] == 'maintenance' and peak['sources'] == [2]
    assert {'id': 'character', 'label': '人物'} in data['categories']


def test_edit_add_delete_backup_and_grounding(world):
    root, client, _ = world; seed()
    file = Path(lore.path('original', 'main')); before = file.read_bytes()
    data = client.get('/api/card/original/lore').json
    result = client.post('/api/card/original/lore', json={'revision': data['revision'], 'changes': [
        {'id': 'peak', 'title': '迎星峰別苑', 'text': '艾迪居所與超算中心', 'category': 'place',
         'keywords': ['迎星峰', ' 超算 ', '迎星峰'], 'always': False},
        {'id': 'core', 'delete': True},
        {'id': None, 'title': '七峰論道', 'text': '2029/11 舉行', 'category': 'event',
         'keywords': ['論道'], 'always': True}]})
    assert result.status_code == 200, result.json
    entries = {e['title']: e for e in result.json['entries']}
    assert set(entries) == {'迎星峰別苑', '七峰論道'}
    assert entries['迎星峰別苑']['keywords'] == ['迎星峰', '超算']
    assert entries['迎星峰別苑']['origin'] == 'manual' and entries['迎星峰別苑']['sources'] == [2]
    assert entries['七峰論道']['origin'] == 'manual' and entries['七峰論道']['category'] == 'event'
    stored = json.loads(file.read_text())
    peak = next(e for e in stored['entries'] if e['id'] == 'peak')
    assert peak['previous_version']['text'] == '艾迪居所' and 'maintenance_anchor' not in peak
    backup = root / 'stories/original/lore_backups/main' / result.json['backup_id']
    assert (backup / 'card_lore_before.json').read_bytes() == before
    rows = io._load_json(io._story_messages_path('original', 'main'), [])
    prompt = lore.grounding('original', 'main', rows, '', '迎星峰')
    assert '2029/11 舉行' in prompt and '艾迪居所與超算中心' in prompt and '固定規則' not in prompt


def test_rewind_still_retires_edited_entry(world):
    _, client, _ = world; seed()
    data = client.get('/api/card/original/lore').json
    client.post('/api/card/original/lore', json={'revision': data['revision'], 'changes': [
        {'id': 'peak', 'title': '迎星峰', 'text': '改寫', 'category': 'place', 'keywords': [], 'always': False}]})
    rows = io._load_json(io._story_messages_path('original', 'main'), [])
    assert [e['id'] for e in lore.available('original', 'main', rows[:1])] == ['core']


def test_stale_revision_and_invalid_input_do_not_write(world):
    root, client, _ = world; seed()
    file = Path(lore.path('original', 'main')); before = file.read_bytes()
    stale = client.post('/api/card/original/lore', json={'revision': 'old', 'changes': [{'id': 'core', 'delete': True}]})
    assert stale.status_code == 409
    revision = client.get('/api/card/original/lore').json['revision']
    for change in [{'id': 'gone', 'delete': True},
                   {'id': 'core', 'title': '核心', 'text': '  ', 'category': 'world', 'keywords': []},
                   {'id': 'core', 'title': '核心', 'text': '規則', 'category': 'nope', 'keywords': []}]:
        assert client.post('/api/card/original/lore', json={'revision': revision, 'changes': [change]}).status_code == 400
    assert file.read_bytes() == before
    assert not (root / 'stories/original/lore_backups').exists()


def test_unchanged_save_is_a_noop(world):
    root, client, _ = world; seed()
    data = client.get('/api/card/original/lore').json
    core = data['entries'][0]
    result = client.post('/api/card/original/lore', json={'revision': data['revision'], 'changes': [
        {k: core[k] for k in ('id', 'title', 'text', 'category', 'keywords', 'always')}]})
    assert result.status_code == 200 and 'backup_id' not in result.json
    assert result.json['revision'] == data['revision']


def test_category_only_change_keeps_origin_and_anchor(world):
    _, client, _ = world; seed()
    data = client.get('/api/card/original/lore').json
    peak = data['entries'][1]
    result = client.post('/api/card/original/lore', json={'revision': data['revision'], 'changes': [
        {**{k: peak[k] for k in ('id', 'title', 'text', 'keywords', 'always')}, 'category': 'place'}]})
    assert result.status_code == 200 and 'backup_id' in result.json
    shown = next(e for e in result.json['entries'] if e['id'] == 'peak')
    assert shown['category'] == 'place' and shown['origin'] == 'maintenance'
    stored = next(e for e in json.loads(Path(lore.path('original', 'main')).read_text())['entries'] if e['id'] == 'peak')
    assert 'previous_version' not in stored and stored['maintenance_anchor']['index'] == 2
