import copy
import json
from pathlib import Path
import pytest
from tests.test_card_ensemble import world, make
from story_core import story_io as io
from story_core.card_mode import ensemble as e
from story_core.card_mode import branches


def seed(slug='original'):
    np=io._story_npcs_path(slug,'main');mp=io._story_messages_path(slug,'main')
    chars=io._load_json(np,[]);chars[0]['memory']=['早期記憶','後期記憶']
    chars.append({'name':'別人','memory':['後期記憶']})
    msgs=io._load_json(mp,[])
    early=copy.deepcopy(chars);early[0]['memory']=['早期記憶']
    msgs[0]['npcs_snapshot']=early
    msgs.append({'index':1,'role':'gm','content':'原文後期記憶', 'pre_npcs_snapshot':early,'npcs_snapshot':copy.deepcopy(chars)})
    io._save_json(np,chars);io._save_json(mp,msgs)


def test_edit_delete_snapshots_backup_and_isolation(world):
    root,client,_=world;seed()
    np=Path(io._story_npcs_path('original','main'));mp=Path(io._story_messages_path('original','main'))
    before={p.name:p.read_bytes() for p in [np,mp]}
    data=client.get('/api/card/original/memories').json
    result=client.post('/api/card/original/memories',json={'revision':data['revision'],'changes':[
        {'character_id':'0','index':0,'text':None},{'character_id':'0','index':1,'text':'修正後期'}]})
    assert result.status_code==200,result.json
    msgs=json.loads(mp.read_text())
    assert msgs[0]['npcs_snapshot'][0]['memory']==[]
    assert msgs[1]['pre_npcs_snapshot'][0]['memory']==[]
    assert msgs[1]['npcs_snapshot'][0]['memory']==['修正後期']
    assert msgs[1]['npcs_snapshot'][1]['memory']==['後期記憶']
    assert msgs[1]['content']=='原文後期記憶'
    backup=root/'stories/original/memory_edit_backups/main'/result.json['backup_id']
    for name,raw in before.items():assert (backup/name).read_bytes()==raw
    assert json.loads((backup/'audit.json').read_text())['status']=='applied'
    assert client.post('/api/card/original/rewind',json={'to_index':0}).status_code==200
    assert json.loads(np.read_text())[0]['memory']==[]


def test_conflict_and_pending_do_not_write(world):
    root,client,_=world;seed()
    data=client.get('/api/card/original/memories').json
    p=io._story_messages_path('original','main');msgs=io._load_json(p,[])
    msgs[-1]['card_record_status']='pending';io._save_json(p,msgs)
    body={'revision':data['revision'],'changes':[{'character_id':'0','index':0,'text':None}]}
    assert client.post('/api/card/original/memories',json=body).status_code==409
    body['revision']=client.get('/api/card/original/memories').json['revision']
    assert client.post('/api/card/original/memories',json=body).status_code==409
    assert not (root/'stories/original/memory_edit_backups').exists()


def test_ensemble_target_only_all_snapshot_types(world):
    root,client,sid=make(world)
    d=e.load('cast_test');old=d['state']['characters'][sid]['memory'][0]
    d['messages'].append({'index':2,'role':'gm','content':'unchanged','ensemble_pre_snapshot':copy.deepcopy(d['state']),'ensemble_snapshot':copy.deepcopy(d['state'])})
    e.save('cast_test',d)
    original=Path(io._story_npcs_path('original','main')).read_bytes()
    data=client.get('/api/card/cast_test/memories').json
    result=client.post('/api/card/cast_test/memories',json={'revision':data['revision'],'changes':[{'character_id':sid,'index':0,'text':'週六帶相簿'}]})
    assert result.status_code==200,result.json
    d=e.load('cast_test')
    for s in [d['state'],d['initial_state'],d['messages'][-1]['ensemble_pre_snapshot'],d['messages'][-1]['ensemble_snapshot']]:
        assert s['characters'][sid]['memory']==['週六帶相簿']
        assert s['characters']['lead']['memory']==[]
    assert Path(io._story_npcs_path('original','main')).read_bytes()==original


def test_failed_write_restores_all_files(world,monkeypatch):
    root,client,_=world;seed()
    from story_core.card_mode import memory_editor as editor
    data=client.get('/api/card/original/memories').json
    paths=[Path(io._story_npcs_path('original','main')),Path(io._story_messages_path('original','main'))]
    before=[p.read_bytes() for p in paths]
    original=io._save_json
    def fail(path,value):
        if path==str(paths[1]):raise OSError('disk failure')
        return original(path,value)
    monkeypatch.setattr(io,'_save_json',fail)
    with pytest.raises(OSError):
        editor.operate('original','main',{'revision':data['revision'],'changes':[{'character_id':'0','index':0,'text':None}]})
    assert [p.read_bytes() for p in paths]==before
    audit=list((root/'stories/original/memory_edit_backups/main').glob('*/audit.json'))[0]
    assert json.loads(audit.read_text())['status']=='rolled_back'


def test_branch_isolation_duplicate_matching_and_stale_revision(world):
    root,client,_=world;seed()
    result=client.post('/api/card/original/branches',json={'message_index':0,'name':'另一條線'})
    assert result.status_code==200,result.json
    bid=result.json['branch_id'];folder=Path(io._branch_dir('original',bid))
    untouched={str(p):p.read_bytes() for p in folder.glob('*.json')}
    data=client.get('/api/card/original/memories?branch_id=main').json
    body={'branch_id':'main','revision':data['revision'],'changes':[{'character_id':'0','index':1,'text':None}]}
    assert client.post('/api/card/original/memories',json=body).status_code==200
    assert untouched=={str(p):p.read_bytes() for p in folder.glob('*.json')}
    assert client.post('/api/card/original/memories',json=body).status_code==409


def test_busy_lock_rejects_memory_save(world):
    from story_core.card_mode import branches
    import threading
    _,client,_=world;seed()
    data=client.get('/api/card/original/memories').json
    entered,release=threading.Event(),threading.Event()
    def hold():
        with branches.lock('original','main'):
            entered.set();release.wait(3)
    t=threading.Thread(target=hold);t.start();assert entered.wait(2)
    try:
        assert client.post('/api/card/original/memories',json={'revision':data['revision'],'changes':[]}).status_code==409
    finally:
        release.set();t.join(2)


def test_dead_single_recorder_does_not_block_memory_save_or_claim_completion(world, monkeypatch):
    root,client,_=world;seed()
    path=io._story_messages_path('original','main');messages=io._load_json(path,[])
    # Reproduce an old interrupted turn followed by a completed turn.
    messages[0].update(card_record_status='pending',card_record_pid=999999,card_turn_id='interrupted')
    messages[-1]['card_record_status']='done';io._save_json(path,messages)
    def missing(pid,signal):
        assert pid==999999 and signal==0
        raise ProcessLookupError()
    monkeypatch.setattr(branches.os,'kill',missing)
    from story_core import llm_bridge
    def no_model(*args,**kwargs):pytest.fail('Must not retry or backfill interrupted recording')
    monkeypatch.setattr(llm_bridge,'call_oneshot',no_model)
    before=Path(path).read_bytes()
    data=client.get('/api/card/original/memories').json
    assert Path(path).read_bytes()==before  # Loading does not invalidate the draft revision.
    response=client.post('/api/card/original/memories',json={'revision':data['revision'],'changes':[
        {'character_id':'0','index':1,'text':'修正後期'}]})
    assert response.status_code==200,response.json
    assert io._load_json(io._story_npcs_path('original','main'),[])[0]['memory']==['早期記憶','修正後期']
    updated=io._load_json(path,[])
    assert updated[0]['card_record_status']=='pending' and updated[0]['card_turn_id']=='interrupted'
    assert updated[-1]['npcs_snapshot'][0]['memory']==['早期記憶','修正後期']
    assert [m['content'] for m in updated]==[m['content'] for m in messages]
    backup=root/'stories/original/memory_edit_backups/main'/response.json['backup_id']
    assert (backup/'messages.json').read_bytes()==before


@pytest.mark.parametrize('pid', [None, True, '999999', 0, -1])
def test_unknown_recorder_owner_still_blocks_memory_save(world,pid):
    root,client,_=world;seed()
    path=io._story_messages_path('original','main');messages=io._load_json(path,[])
    messages[0].update(card_record_status='pending',card_record_pid=pid);io._save_json(path,messages)
    before=Path(path).read_bytes();data=client.get('/api/card/original/memories').json
    res=client.post('/api/card/original/memories',json={'revision':data['revision'],'changes':[]})
    assert res.status_code==409 and Path(path).read_bytes()==before
    assert not (root/'stories/original/memory_edit_backups').exists()


@pytest.mark.parametrize('inspection', ['alive','permission_denied'])
def test_live_or_uninspectable_owner_still_blocks_save(world,monkeypatch,inspection):
    root,client,_=world;seed()
    path=io._story_messages_path('original','main');messages=io._load_json(path,[])
    messages[0].update(card_record_status='pending',card_record_pid=999999);io._save_json(path,messages)
    def inspect(pid,signal):
        if inspection=='permission_denied':raise PermissionError()
    monkeypatch.setattr(branches.os,'kill',inspect)
    data=client.get('/api/card/original/memories').json
    assert client.post('/api/card/original/memories',json={'revision':data['revision'],'changes':[]}).status_code==409
    assert not (root/'stories/original/memory_edit_backups').exists()


def test_ensemble_pending_is_not_treated_as_dead_single_recorder(world,monkeypatch):
    root,client,_=make(world)
    doc=e.load('cast_test');doc['messages'][-1].update(ensemble_record_status='pending',card_record_pid=999999)
    e.save('cast_test',doc)
    from story_core.card_mode import ensemble_recorder
    monkeypatch.setattr(ensemble_recorder,'start',lambda *a,**k:None)
    def missing(*args):raise ProcessLookupError()
    monkeypatch.setattr(branches.os,'kill',missing)
    before=e.path('cast_test').read_bytes();data=client.get('/api/card/cast_test/memories').json
    res=client.post('/api/card/cast_test/memories',json={'revision':data['revision'],'changes':[]})
    assert res.status_code==409 and e.path('cast_test').read_bytes()==before
    assert not (root/'stories/cast_test/memory_edit_backups').exists()
