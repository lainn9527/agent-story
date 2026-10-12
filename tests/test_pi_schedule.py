import json
from pathlib import Path
from copy import deepcopy
from tests.test_pi_maintenance import story
from story_core import story_io as io
from story_core.card_mode import pi_schedule as schedule
from story_core.card_mode.ops import gate_inventory


def test_quote_pair_deletion_and_partial_status():
    clean,_=gate_inventory({'用完':"''",'交付':'""','藥液':'剩半瓶'})
    assert clean=={'用完':'','交付':'','藥液':'剩半瓶'}


def add_pairs(n):
    m=io._load_branch_messages('s','main')
    template=deepcopy(m[-1])
    for i in range(n):
        m.append({'index':len(m),'role':'user','content':'繼續'})
        gm=deepcopy(template);gm.update(index=len(m),role='gm',content='完成',card_turn_id=f'turn-{len(m)}',card_record_status='done')
        m.append(gm)
    io._save_json(io._story_messages_path('s','main'),m)


def test_fifty_pairs_not_messages_idle_and_retry_budget(story, monkeypatch):
    cli=story/'pi';cli.write_text('test')
    schedule.configure('s','main',cli=str(cli),every=50)
    calls=[]
    def run(frozen,**kw):
        calls.append(frozen)
        return {'status':'rejected','run_dir':'fake','error':'unproven'}
    monkeypatch.setattr(schedule.tasks,'run',run)
    add_pairs(49)
    assert schedule.run_due('s','main')['status']=='not_due'
    assert not calls
    add_pairs(1)
    assert schedule.run_due('s','main')['status']=='rejected'
    assert len(calls)==1 and 'inventory' in calls[0]['kinds']
    assert schedule.run_due('s','main')['status']=='not_due'
    add_pairs(50)
    schedule.run_due('s','main')
    assert len(calls)==2
    messages=io._load_branch_messages('s','main')
    io._save_json(io._story_messages_path('s','main'),messages[:-4])
    assert schedule.run_due('s','main')['status']=='baseline'
    assert len(calls)==2


def test_verified_scheduled_task_applies(story, monkeypatch):
    cli=story/'pi';cli.write_text('test')
    schedule.configure('s','main',cli=str(cli),every=1)
    add_pairs(1)
    monkeypatch.setattr(schedule.tasks,'run',lambda *a,**kw:{'status':'verified','run_dir':'test'})
    calls=[]
    def apply(path,**kw):calls.append(path);return {'status':'applied'}
    monkeypatch.setattr(schedule.tasks,'apply',apply)
    assert schedule.run_due('s','main')['status']=='applied'
    assert calls==['test']


def test_failure_retries_on_subsequent_turns_up_to_budget(story, monkeypatch):
    cli = story / 'pi'; cli.write_text('test')
    schedule.configure('s', 'main', cli=str(cli), every=10)
    calls = []
    def run_fail(frozen, **kw):
        calls.append(frozen)
        return {'status': 'rejected', 'run_dir': 'fake', 'error': 'temporary failure', 'retryable': True}
    monkeypatch.setattr(schedule.tasks, 'run', run_fail)

    add_pairs(10)
    assert schedule.run_due('s', 'main')['status'] == 'rejected'
    assert len(calls) == 1

    # Same turn (0 new pairs) -> not_due
    assert schedule.run_due('s', 'main')['status'] == 'not_due'
    assert len(calls) == 1

    # Retry 1: on next completed turn
    add_pairs(1)
    assert schedule.run_due('s', 'main')['status'] == 'rejected'
    assert len(calls) == 2

    # Retry 2: on next completed turn
    add_pairs(1)
    assert schedule.run_due('s', 'main')['status'] == 'rejected'
    assert len(calls) == 3

    # Retry 3: on next completed turn
    add_pairs(1)
    assert schedule.run_due('s', 'main')['status'] == 'rejected'
    assert len(calls) == 4

    # Retry budget exhausted (3 retries done) -> next turn does not retry immediately
    add_pairs(1)
    assert schedule.run_due('s', 'main')['status'] == 'not_due'
    assert len(calls) == 4


def test_cancel_active_task_on_rewind(story):
    key = ('s', 'main')
    schedule._active_jobs[key] = {'cancel_event': schedule.threading.Event(), 'kinds': ['summary']}
    try:
        assert schedule.is_running('s', 'main', kind='summary')
        messages = io._load_branch_messages('s', 'main')
        schedule.reconcile_after_rewind('s', 'main', messages[:-2])
        assert schedule._active_jobs[key]['cancel_event'].is_set()
    finally:
        schedule._active_jobs.pop(key, None)


def test_compaction_skipped_when_pi_actively_summarizing(story, monkeypatch):
    from story_core import compaction
    compact_runs = []
    monkeypatch.setattr(compaction, '_run_compaction', lambda *a, **k: compact_runs.append(True))
    timeline = io._load_branch_messages('s', 'main')

    # Without Pi running: compact_async executes
    compaction.compact_async('s', 'main', timeline)
    import time; time.sleep(0.1)
    assert len(compact_runs) == 1

    # With Pi running summary: compact_async skips
    key = ('s', 'main')
    schedule._active_jobs[key] = {'cancel_event': schedule.threading.Event(), 'kinds': ['summary']}
    try:
        compaction.compact_async('s', 'main', timeline)
        time.sleep(0.1)
        assert len(compact_runs) == 1  # Still 1, skipped!
    finally:
        schedule._active_jobs.pop(key, None)


def test_rewind_resets_interval_anchor(story, monkeypatch):
    cli=story/'pi';cli.write_text('test')
    schedule.configure('s','main',cli=str(cli),every=10)
    calls=[]
    monkeypatch.setattr(schedule.tasks,'run',lambda frozen,**kw:calls.append(1) or {'status':'cancelled','run_dir':'fake'})
    add_pairs(20)
    schedule.run_due('s','main')
    assert len(calls)==1
    messages=io._load_branch_messages('s','main')
    kept=messages[:-10]
    io._save_json(io._story_messages_path('s','main'),kept)
    schedule.reconcile_after_rewind('s','main',kept)
    add_pairs(10)
    schedule.run_due('s','main')
    assert len(calls)==2  # due one interval after the rewind point, not after the old anchor


def test_rejected_proposal_waits_for_next_interval(story, monkeypatch):
    cli=story/'pi';cli.write_text('test')
    schedule.configure('s','main',cli=str(cli),every=10)
    calls=[]
    monkeypatch.setattr(schedule.tasks,'run',lambda frozen,**kw:calls.append(1) or {'status':'rejected','run_dir':'fake','error':'unproven'})
    add_pairs(10)
    schedule.run_due('s','main')
    add_pairs(1)
    assert schedule.run_due('s','main')['status']=='not_due'
    assert len(calls)==1


def test_busy_apply_is_retried_next_turn(story, monkeypatch):
    cli=story/'pi';cli.write_text('test')
    schedule.configure('s','main',cli=str(cli),every=10)
    monkeypatch.setattr(schedule.tasks,'run',lambda *a,**kw:{'status':'verified','run_dir':'test'})
    calls=[]
    def apply(path,**kw):
        calls.append(path)
        raise schedule.branches.Busy('busy')
    monkeypatch.setattr(schedule.tasks,'apply',apply)
    add_pairs(10)
    assert schedule.run_due('s','main')['retryable']
    add_pairs(1)
    schedule.run_due('s','main')
    assert len(calls)==2
