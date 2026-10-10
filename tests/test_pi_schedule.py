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
    def apply(path):calls.append(path);return {'status':'applied'}
    monkeypatch.setattr(schedule.tasks,'apply',apply)
    assert schedule.run_due('s','main')['status']=='applied'
    assert calls==['test']
