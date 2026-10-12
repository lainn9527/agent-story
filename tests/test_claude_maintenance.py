"""Background Claude memory upkeep: cursor, apply through the memory editor, refusals and backoff."""
import json
from pathlib import Path

import pytest
from tests.test_card_branches import world, single_turn, no_live_compaction
from story_core import story_io as io
from story_core.card_mode import claude_maintenance as cm


def enable(every=4):
    Path(cm.config_path('original')).write_text(json.dumps({'enabled': True, 'every_messages': every}))


def memories():
    return io._load_json(io._story_npcs_path('original', 'main'), [])[0]['memory']


def latest():
    return io._load_branch_messages('original', 'main')[-1]['index']


def test_disabled_by_default_and_baselines_once(world):
    assert cm.maybe_run('original', 'main', latest()) is False
    assert not Path(cm.ledger_path('original', 'main')).exists()
    enable()
    assert cm.maybe_run('original', 'main', latest()) is False  # first sight only sets the cursor
    ledger = json.loads(Path(cm.ledger_path('original', 'main')).read_text())
    assert ledger['status'] == 'baseline' and ledger['through_index'] == latest()


def test_pass_merges_new_memories_and_advances_cursor(world):
    enable()
    cm.maybe_run('original', 'main', latest())
    for text in ['小蘭答應週六一起布展。', '小蘭同意週六一起布展。', '小蘭感到極度無奈。']:
        single_turn(text=text)
    seen = {}

    def claude(prompt, cfg):
        seen['prompt'] = prompt
        return json.dumps({'changes': [
            {'index': 0, 'before': '小蘭答應週六一起布展。', 'text': '小蘭答應週六一起布展，地點在學校藝廊。'},
            {'index': 1, 'before': '小蘭同意週六一起布展。', 'text': None},
            {'index': 2, 'before': '小蘭感到極度無奈。', 'text': None}], 'report': '合併布展約定'})

    assert cm.maybe_run('original', 'main', latest(), call=claude, background=False)
    assert '*0\t小蘭答應週六一起布展。' in seen['prompt']  # new memories are marked
    assert memories() == ['小蘭答應週六一起布展，地點在學校藝廊。']
    ledger = json.loads(Path(cm.ledger_path('original', 'main')).read_text())
    assert ledger['status'] == 'done' and ledger['through_index'] == latest() and ledger['memory_count'] == 1
    backups = list(Path(io._story_dir('original'), 'memory_edit_backups', 'main').glob('*/audit.json'))
    assert backups  # undoable through the memory editor's own backup
    # Below the threshold nothing runs.
    assert cm.maybe_run('original', 'main', latest(), call=claude, background=False) is False


def test_oversized_plan_is_refused_cursor_kept_and_backs_off(world):
    enable(every=2)
    cm.maybe_run('original', 'main', latest())
    start = json.loads(Path(cm.ledger_path('original', 'main')).read_text())['through_index']
    single_turn(text='一條新記憶')
    plan = {'changes': [{'index': i, 'before': f'不存在{i}', 'text': None} for i in range(16)]}
    assert cm.maybe_run('original', 'main', latest(), call=lambda p, c: json.dumps(plan), background=False)
    ledger = json.loads(Path(cm.ledger_path('original', 'main')).read_text())
    assert ledger['status'] == 'error' and ledger['through_index'] == start
    assert memories() == ['一條新記憶']
    assert cm.maybe_run('original', 'main', latest(), call=lambda p, c: '{}', background=False) is False  # backoff


def test_rewind_behind_cursor_rebaselines(world):
    enable()
    cm.maybe_run('original', 'main', latest())
    ledger_file = Path(cm.ledger_path('original', 'main'))
    ledger = json.loads(ledger_file.read_text()); ledger['through_index'] = 999
    ledger_file.write_text(json.dumps(ledger))
    assert cm.maybe_run('original', 'main', latest()) is False
    assert json.loads(ledger_file.read_text())['through_index'] == latest()


@pytest.mark.parametrize('raw', ['沒有 JSON', '{"changes": "x"}', '{"changes": [{"before": "a", "text": ""}]}'])
def test_bad_output_is_rejected(raw):
    with pytest.raises(ValueError):
        cm._parse(raw)


def write_lore(entries):
    from story_core.card_mode import lore
    io._save_json(str(lore.path('original', 'main')), {'schema_version': 1, 'entries': entries})


def test_pass_updates_and_adds_lore_with_anchor_and_respects_author_canon(world):
    from story_core.card_mode import lore
    write_lore([{'id': 'core', 'title': '基調', 'text': '作者設定', 'keywords': [], 'always': True},
                {'id': 'xl', 'title': '李小蘭', 'text': '大學生，籌備攝影展。', 'keywords': ['小蘭'], 'category': 'character'}])
    enable()
    cm.maybe_run('original', 'main', latest())
    for text in ['小蘭決定擔任展覽策展人。', '新角色阿凱加入攝影社。', '第三回合']:
        single_turn(text=text)
    plan = {'changes': [], 'report': '更新設定', 'lore': [
        {'id': 'core', 'title': '基調', 'category': 'world', 'keywords': [], 'text': '被改掉了'},
        {'id': 'xl', 'title': '李小蘭', 'category': 'character', 'keywords': ['小蘭', '策展人'],
         'text': '大學生，攝影展策展人。'},
        {'id': None, 'title': '阿凱', 'category': 'character', 'keywords': ['阿凱'], 'text': '新加入攝影社的社員。'}]}
    assert cm.maybe_run('original', 'main', latest(), call=lambda p, c: json.dumps(plan), background=False)
    messages = io._load_branch_messages('original', 'main')
    doc = io._load_json(str(lore.path('original', 'main')), {})
    shown = {e['id']: e for e in lore.available_from(doc, messages)}
    assert shown['core']['text'] == '作者設定'  # locked canon untouched
    assert shown['xl']['text'] == '大學生，攝影展策展人。' and shown['xl']['edited_by'] == 'claude'
    assert shown['xl']['maintenance_anchor']['index'] == latest()
    new = [e for e in shown.values() if e['title'] == '阿凱']
    assert new and new[0]['id'].startswith('cm_')
    assert doc['curated_through_index'] == latest()
    ledger = json.loads(Path(cm.ledger_path('original', 'main')).read_text())
    assert set(ledger['lore']) == {'xl', new[0]['id']}
    assert list(Path(io._story_dir('original'), 'lore_backups', 'main').glob('*/audit.json'))
    # Regenerating the anchor turn retires the pass's lore: the old version comes back, the new entry hides.
    regenerated = [dict(m, content=m['content'] + '（重生）') if m['index'] == latest() else m for m in messages]
    shown = {e['id']: e for e in lore.available_from(doc, regenerated)}
    assert shown['xl']['text'] == '大學生，籌備攝影展。'
    assert not any(e['title'] == '阿凱' for e in shown.values())


def test_many_lore_changes_are_accepted_but_each_is_validated():
    ops = [{'id': None, 'title': f't{i}', 'category': 'other', 'keywords': [], 'text': 'x'} for i in range(20)]
    assert len(cm._parse(json.dumps({'changes': [], 'lore': ops}))['lore']) == 20
    with pytest.raises(ValueError):
        cm._parse(json.dumps({'changes': [], 'lore': [{'id': None, 'title': '', 'text': 'x'}]}))


def test_range_that_keeps_failing_is_skipped_and_recorded(world):
    enable(every=2)
    cm.maybe_run('original', 'main', latest())
    start = json.loads(Path(cm.ledger_path('original', 'main')).read_text())['through_index']
    for n in range(3):
        for _ in range(5):  # get past the threshold and the 10-message backoff
            single_turn(text=f'第{n}輪記憶{_}')
        assert cm.maybe_run('original', 'main', latest(), call=lambda p, c: '壞掉的輸出', background=False)
    ledger = json.loads(Path(cm.ledger_path('original', 'main')).read_text())
    assert ledger['status'] == 'skipped' and ledger['through_index'] == latest()
    assert ledger['skipped'] == [[start + 1, latest()]]
