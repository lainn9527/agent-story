"""Continuity regressions: retained facts, summary lag, and revision-safe rebuilds."""
import pytest
from story_core import compaction
from story_core.card_mode.memory import select_memories
from story_core.card_mode.recap import context_window, COMPACT_PROMPT, META_COMPACT_PROMPT


def test_retrieves_old_fact_outside_recent_window_without_mutating_archive():
    facts = ['雙方約定客房分開居住，並沒有復合。'] + [f'今天買了第{i}本書。' for i in range(160)]
    original = list(facts)
    selected = select_memories(facts, '客房分開居住的約定是什麼？')
    assert facts[0] in selected
    assert selected[-36:] == facts[-36:]
    assert facts == original


def test_three_groups_are_disjoint_ordered_and_redrawn(monkeypatch):
    from story_core.card_mode import memory
    unrelated = [f'archive{n}' for n in range(100)]
    relevant = [f'hiking memory{n}' for n in range(50)]
    recent = [f'recent{n}' for n in range(36)]
    facts = unrelated + relevant + recent
    pools=[]
    def draw(population, count):
        pools.append(list(population))
        return population[:count] if len(pools)==1 else population[-count:]
    monkeypatch.setattr(memory.random,'sample',draw)
    first=select_memories(facts,'hiking')
    second=select_memories(facts,'hiking')
    stable=set(relevant[-36:]+recent)
    assert len(first)==len(second)==108
    assert len(set(first))==108 and stable <= set(first) & set(second)
    assert first!=second and pools[0]==pools[1]
    assert not set(pools[0]) & set(range(114,len(facts)))
    assert first==sorted(first,key=facts.index)
    assert second==sorted(second,key=facts.index)


def test_unrelated_facts_do_not_fill_relevant_quota_and_duplicates_are_removed():
    facts=[f'old{n}' for n in range(100)]+[f'new{n}' for n in range(36)]
    original=list(facts)
    selected=select_memories(facts,'unmatched')
    assert len(selected)==72 and selected[-36:]==facts[-36:]
    assert facts==original
    assert select_memories(['old','new','old'],'')==['new','old']


def test_small_pool_and_disabled_random_do_not_overdraw():
    assert select_memories(['one','two'],'unmatched')==['one','two']
    facts=[f'old{n}' for n in range(40)]
    assert select_memories(facts,'unmatched')==facts
    assert select_memories(facts,'unmatched',random_count=0)==facts[-36:]


def test_old_and_new_agreements_remain_in_historical_order():
    old = '雙方約定每週五在客房讀書。'
    new = '雙方將客房讀書約定改為每週六，週五安排已取消。'
    facts = [old] + [f'購買書籍編號{i}' for i in range(30)] + [new]
    selected = select_memories(facts, '客房讀書約定')
    assert selected.index(old) < selected.index(new)
    # A rewind restores an older snapshot: retrieval cannot consult future memory.
    assert new not in select_memories(facts[:-1], '客房讀書約定')


def test_context_has_no_hole_while_summary_lags():
    timeline = [{'index': i, 'content': str(i)} for i in range(40)]
    window = context_window(timeline, {'recap_text': 'history', 'compacted_through_index': 20})
    assert window == timeline  # Short exchanges expand the raw context.
    assert context_window(timeline, {}) == timeline
    assert len(context_window(timeline, {'recap_text': 'history', 'compacted_through_index': 35})) == 40


def test_adaptive_context_keeps_short_refusal_and_whole_messages():
    timeline = [{'index': i, 'content': '長篇演出' * 200} for i in range(40)]
    timeline[-11]['content'] = '不要'
    for message in timeline[-10:]:
        message['content'] = '嗯'
    window = context_window(timeline, {'recap_text': 'history', 'compacted_through_index': 38})
    assert timeline[-11] in window and len(window) > 10
    assert 8000 <= sum(len(m['content']) for m in window) <= 12000
    assert window == timeline[-len(window):]


def test_context_budget_does_not_skip_large_message_or_drop_summary_lag():
    timeline = [{'index': i, 'content': 'x' * 500} for i in range(60)]
    timeline[-11]['content'] = 'x' * 13000
    assert context_window(timeline, {'recap_text': 'past', 'compacted_through_index': 58}) == timeline[-10:]
    assert context_window(timeline, {'recap_text': 'past', 'compacted_through_index': 4}) == timeline[5:]
    for m in timeline[-10:]:
        m['content'] = 'x' * 2000
    assert context_window(timeline, {'recap_text': 'past', 'compacted_through_index': 58}) == timeline[-10:]


@pytest.mark.parametrize('fail_second', [False, True])
def test_full_message_rebuild_is_batched_and_atomic(tmp_path, monkeypatch, fail_second):
    monkeypatch.setattr(compaction, 'STORIES_DIR', str(tmp_path))
    monkeypatch.setattr(compaction, 'get_character_name', lambda *a: '玩家')
    monkeypatch.setattr('story_core.usage_db.log_from_bridge', lambda *a, **k: None)
    before = {'recap_text': 'legacy', 'compacted_through_index': 1, 'revision': 'a'}
    compaction.save_recap('test', 'main', before)
    tail = '這是長篇結尾才達成的重要約定。'
    history = [{'role': 'gm', 'content': '敘事' * 700 + tail},
               {'role': 'user', 'content': '第二段更改約定'}] + [{'role': 'user', 'content': 'recent'}] * 10
    prompts = []
    def generate(prompt):
        prompts.append(prompt)
        assert compaction.load_recap('test', 'main') == before
        return '' if fail_second and len(prompts) == 2 else f'摘要{len(prompts)}'
    monkeypatch.setattr('story_core.llm_bridge.call_oneshot', generate)
    compaction._run_compaction('test', 'main', history, prompt_template=COMPACT_PROMPT,
                              meta_prompt_template=META_COMPACT_PROMPT,
                              max_message_chars=None, batch_chars=1000, recap_version=2)
    assert tail in prompts[0]  # No 1000-character truncation.
    assert 'legacy' not in prompts[0]  # Migration starts from original messages.
    assert '摘要1' in prompts[1]
    after = compaction.load_recap('test', 'main')
    if fail_second:
        assert after == before
    else:
        assert after['recap_version'] == 2
        assert after['compacted_through_index'] == 1
        assert after['recap_text'] == '摘要2'


def test_revision_change_between_batches_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr(compaction, 'STORIES_DIR', str(tmp_path))
    monkeypatch.setattr(compaction, 'get_character_name', lambda *a: '玩家')
    monkeypatch.setattr('story_core.usage_db.log_from_bridge', lambda *a, **k: None)
    calls = []
    def generate(prompt):
        calls.append(prompt)
        compaction.invalidate_recap('test', 'main')
        return '已撤回的情節'
    monkeypatch.setattr('story_core.llm_bridge.call_oneshot', generate)
    history = [{'role': 'user', 'content': '123456'}] * 12
    compaction._run_compaction('test', 'main', history, max_message_chars=None,
                              batch_chars=6, recap_version=2)
    assert len(calls) == 1
    assert compaction.load_recap('test', 'main')['recap_text'] == ''


# Exercise the real route/prompt path without making paid narrative calls.
from tests.test_card_ensemble import world


def test_send_then_regenerate_redraws_memory_without_changing_archive(world, monkeypatch):
    import json
    from story_core.card_mode import memory, pipeline
    from routes import card_routes
    root, client, _ = world
    monkeypatch.setattr(compaction, 'STORIES_DIR', str(root/'stories'))
    p=root/'stories/original/branches/main/npcs.json'
    npcs=json.loads(p.read_text())
    facts=[f'archive{i}' for i in range(100)]+[f'hiking memory{i}' for i in range(50)]+[f'recent{i}' for i in range(36)]
    npcs[0]['memory']=facts
    p.write_text(json.dumps(npcs))
    draws=[];prompts=[]
    def sample(pool,count):
        draws.append(list(pool))
        return pool[:count] if len(draws)==1 else pool[-count:]
    monkeypatch.setattr(memory.random,'sample',sample)
    def generate(**kwargs):
        prompts.append(kwargs['system_prompt'])
        return '她點頭：「你好。」','custom_llm'
    monkeypatch.setattr(pipeline,'_generate_pass1',generate)
    def run(**kwargs):
        kwargs.update(async_recorder=False,recorder_fn=lambda *a:None)
        return pipeline.run_card_turn(**kwargs)
    monkeypatch.setattr(card_routes,'run_card_turn',run)
    response=client.post('/api/card/original/send/stream',json={'message':'hiking'})
    assert '"type": "done"' in response.get_data(as_text=True)
    response=client.post('/api/card/original/regenerate',json={})
    assert response.status_code==200 and response.json['ok']
    selections=[{line[2:] for line in text.splitlines() if line.startswith('- ') and line[2:] in facts} for text in prompts]
    assert len(selections)==2 and len(selections[0])==len(selections[1])==108
    assert selections[0]!=selections[1]
    assert set(facts[114:]) <= selections[0] & selections[1]
    assert json.loads(p.read_text())[0]['memory']==facts


def test_recent_quota_moves_before_context_without_reading_future():
    from story_core.card_mode.memory import memories_before_context
    facts = [f'fact{i}' for i in range(160)]
    recent = [{'role': 'user', 'npcs_snapshot': [{'name': '小蘭', 'memory': facts[:100] + ['future']}]}]
    pool = memories_before_context(recent, '小蘭')
    assert select_memories(facts, relevant_count=0, random_count=0,
                           recent_memory_pool=pool) == facts[64:100]
    assert select_memories(facts, relevant_count=0, random_count=0,
                           recent_memory_pool=[]) == []
    assert select_memories(facts[:5], relevant_count=0, random_count=0,
                           recent_memory_pool=facts[:2]) == facts[:2]


def test_context_boundary_requires_pre_snapshot_for_gm_and_legacy_falls_back():
    from story_core.card_mode.memory import memories_before_context
    npc = lambda facts: [{'name': '小蘭', 'memory': facts}]
    assert memories_before_context([{'role': 'gm', 'pre_npcs_snapshot': npc(['old']),
                                     'npcs_snapshot': npc(['old', 'new'])}], '小蘭') == ['old']
    assert memories_before_context([{'role': 'gm', 'npcs_snapshot': npc(['new'])}], '小蘭') is None
    assert memories_before_context([], '小蘭') is None
    assert memories_before_context([{'role': 'user', 'npcs_snapshot': npc(['old'])}], '其他人') is None


def test_shift_preserves_relevance_and_random_pools(monkeypatch):
    from story_core.card_mode import memory
    facts = [f'hiking fact{i}' for i in range(160)]
    pools = []
    def sample(pool, count):
        pools.append(list(pool))
        return pool[:count]
    monkeypatch.setattr(memory.random, 'sample', sample)
    original = select_memories(facts, 'hiking')
    shifted = select_memories(facts, 'hiking', recent_memory_pool=facts[:80])
    assert pools[0] == pools[1]
    assert set(original) - set(facts[-36:]) <= set(shifted)
    assert set(facts[44:80]) <= set(shifted)
    assert len(shifted) <= 108
