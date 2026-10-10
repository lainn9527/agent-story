"""Turn edits regenerate from pre-state; draft and instructions are not history."""
import copy
import json
from pathlib import Path
import pytest
from tests.test_card_ensemble import world, make
from tests.test_card_branches import single_turn, no_live_compaction
from story_core import story_io as io
from story_core.card_mode import editing, pipeline, ensemble as e, ensemble_recorder as er, branches as b


def body(messages, index=2, mode='revise', destination='overwrite'):
    return {'message_index':index,'mode':mode,'destination':destination,'message':'新的玩家輸入',
            'instruction':'只改最後一句','expected_reply':next(m['content'] for m in messages if m['index']==index)}


@pytest.mark.parametrize('mode', ['revise','regenerate'])
@pytest.mark.parametrize('destination', ['overwrite','fork'])
def test_single_edit_history_state_and_prompt(world, monkeypatch, mode, destination):
    root, client, _ = world
    single_turn(text='被替換的記憶')
    single_turn(text='未來記憶')
    parent = Path(io._branch_dir('original','main'))
    before = {p.name:p.read_bytes() for p in parent.iterdir() if p.is_file()}
    ms = io._load_branch_messages('original','main')
    captures=[]
    def gm(**kw):
        captures.append(kw)
        assert '未來記憶' not in kw['system_prompt']
        assert all('被替換的記憶' not in m['content'] and '未來記憶' not in m['content'] for m in kw['recent'])
        assert ('【待修改草稿】' in kw['system_prompt']) == (mode=='revise')
        assert kw['user_msg']=='新的玩家輸入'
        return '小蘭答應新的安排。','test'
    monkeypatch.setattr(pipeline,'_generate_pass1',gm)
    def recorder(sid,bid,narration,index):
        npcs=io._load_json(io._story_npcs_path(sid,bid),[])
        assert npcs[0]['memory']==[]
        npcs[0]['memory'].append('新的記憶')
        io._save_json(io._story_npcs_path(sid,bid),npcs)
    original=pipeline.run_card_turn
    monkeypatch.setattr(pipeline,'run_card_turn',lambda *a,**kw: original(*a,**kw,recorder_fn=recorder,async_recorder=False))
    res=client.post('/api/card/original/edit',json=body(ms,mode=mode,destination=destination))
    assert res.status_code==200,res.json
    bid=res.json['branch_id']; new=io._load_branch_messages('original',bid)
    assert len(new)==3 and new[-2]['content']=='新的玩家輸入'
    assert '只改最後一句' not in json.dumps(new,ensure_ascii=False)
    assert new[-1]['pre_npcs_snapshot'][0]['memory']==[]
    assert new[-1]['npcs_snapshot'][0]['memory']==['新的記憶']
    assert io._load_json(io._story_npcs_path('original',bid),[])[0]['memory']==['新的記憶']
    if destination=='fork':
        assert bid!='main'
        assert before=={p.name:p.read_bytes() for p in parent.iterdir() if p.is_file()}
    else: assert bid=='main'


@pytest.mark.parametrize('destination',['overwrite','fork'])
def test_failed_single_edit_preserves_original(world,monkeypatch,destination):
    _,client,_=world; single_turn()
    parent=Path(io._branch_dir('original','main'))
    before={p.name:p.read_bytes() for p in parent.iterdir() if p.is_file()}
    tree=copy.deepcopy(b.tree('original'))
    monkeypatch.setattr(pipeline,'_generate_pass1',lambda **kw:('', 'failed'))
    res=client.post('/api/card/original/edit',json=body(io._load_branch_messages('original','main'),destination=destination))
    assert res.status_code==400
    assert before=={p.name:p.read_bytes() for p in parent.iterdir() if p.is_file()}
    assert b.tree('original')==tree


@pytest.mark.parametrize('invalid',['stale','opening','missing_snapshot','invalid_mode','empty'])
def test_invalid_edits_do_not_mutate(world,invalid):
    _,client,_=world; single_turn()
    path=io._story_messages_path('original','main');ms=io._load_json(path,[])
    request=body(ms)
    if invalid=='stale':request['expected_reply']='舊版'
    if invalid=='opening':request['message_index']=0
    if invalid=='missing_snapshot':
        ms[-1].pop('pre_npcs_snapshot');io._save_json(path,ms)
    if invalid=='invalid_mode':request['mode']='bad'
    if invalid=='empty':request['message']=' '
    before=Path(path).read_bytes()
    res=client.post('/api/card/original/edit',json=request)
    assert res.status_code==400
    assert before==Path(path).read_bytes()


@pytest.mark.parametrize('destination',['overwrite','fork'])
@pytest.mark.parametrize('mode',['regenerate','revise'])
def test_ensemble_edit_isolated(world,monkeypatch,destination,mode):
    _,client,sid=make(world)
    e.mutate('cast_test','send',{'message':'舊輸入'},lambda *a:'舊回覆',lambda *a:[])
    e.mutate('cast_test','send',{'message':'未來輸入'},lambda *a:'未來回覆',lambda *a:[])
    doc=e.load('cast_test');request=body(doc['messages'],mode=mode,destination=destination)
    def gm(user,system,recent):
        assert user=='新的玩家輸入'
        assert '未來回覆' not in system
        assert ('【待修改草稿】' in system)==(mode=='revise')
        assert not any(m['content'] in ('舊回覆','未來回覆') for m in recent)
        return '新回覆'
    monkeypatch.setattr(e,'generate',gm)
    monkeypatch.setattr(er,'start',lambda *a,**kw:None)
    res=client.post('/api/card/cast_test/edit',json=request)
    assert res.status_code==200,res.json
    bid=res.json['branch_id'];new=e.load('cast_test',bid)
    assert new['messages'][-1]['content']=='新回覆'
    assert new['messages'][-1]['ensemble_turn_id']!=doc['messages'][2]['ensemble_turn_id']
    assert len(new['messages'])==3
    if destination=='fork':assert e.load('cast_test')==doc


def test_edit_stream_returns_destination(world,monkeypatch):
    _,client,_=world;single_turn()
    monkeypatch.setattr(pipeline,'_generate_pass1',lambda **kw: (kw['emit_chunk']('新回覆') or '新回覆','test'))
    original=pipeline.run_card_turn
    monkeypatch.setattr(pipeline,'run_card_turn',lambda *a,**kw:original(*a,**kw,recorder_fn=lambda *a:None,async_recorder=False))
    request=body(io._load_branch_messages('original','main'),destination='fork');request['stream']=True
    res=client.post('/api/card/original/edit',json=request)
    events=[json.loads(line[6:]) for line in res.text.splitlines() if line.startswith('data: ')]
    assert events[0]=={'type':'text','chunk':'新回覆'}
    assert events[-1]['type']=='done' and events[-1]['result']['branch_id']!='main'


def test_edit_cancels_old_recorder_and_records_only_new_prose(world,monkeypatch):
    import threading
    from story_core import gemini_bridge
    _,client,_=world
    entered=threading.Event();release=threading.Event();late=[];recorded=[]
    def provider(**kw):
        recorded.append(kw['user_message'])
        assert '只改最後一句' not in kw['user_message']+kw['system_prompt']
        if '舊回覆' in kw['user_message']:
            kw['on_tool']('remember',{'facts':['舊回覆的記憶']})
            entered.set();assert release.wait(5)
            late.append(kw['on_tool']('remember',{'facts':['遲到記憶']}))
        else:kw['on_tool']('remember',{'facts':['新版記憶']})
        return '',[],'ok'
    monkeypatch.setattr(gemini_bridge,'call_gemini_gm_tools',provider)
    pipeline.run_card_turn('original','main','舊輸入',llm_fn=lambda *a:'舊回覆')
    try:
        assert entered.wait(2)
        original=pipeline.run_card_turn
        monkeypatch.setattr(pipeline,'run_card_turn',lambda *a,**kw:original(*a,**kw,llm_fn=lambda *a:'新版回覆',async_recorder=False))
        request=body(io._load_branch_messages('original','main'))
        res=client.post('/api/card/original/edit',json=request)
        assert res.status_code==200,res.json
        assert io._load_json(io._story_npcs_path('original','main'),[])[0]['memory']==['新版記憶']
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name.startswith('card-recorder-'):thread.join(5)
    assert late==[{'ok':False,'error':'此回合已取消，結果不會寫入。'}]
    assert io._load_json(io._story_npcs_path('original','main'),[])[0]['memory']==['新版記憶']


def test_future_recap_is_not_given_to_revised_turn(world,monkeypatch):
    from story_core import compaction
    _,client,_=world;single_turn();single_turn()
    path=Path(io._branch_dir('original','main'))/'conversation_recap.json'
    io._save_json(str(path),{'recap_text':'未來秘密','compacted_through_index':4,'revision':'old'})
    def generate(**kw):
        assert '未來秘密' not in kw['system_prompt']
        return '新回覆','test'
    monkeypatch.setattr(pipeline,'_generate_pass1',generate)
    original=pipeline.run_card_turn
    monkeypatch.setattr(pipeline,'run_card_turn',lambda *a,**kw:original(*a,**kw,recorder_fn=lambda *a:None,async_recorder=False))
    res=client.post('/api/card/original/edit',json=body(io._load_branch_messages('original','main'),destination='fork'))
    assert res.status_code==200,res.json
    assert compaction.load_recap('original','main')['recap_text']=='未來秘密'
    assert not compaction.load_recap('original',res.json['branch_id'])['recap_text']


def test_ensemble_generation_failure_keeps_original(world,monkeypatch):
    _,client,_=make(world)
    e.mutate('cast_test','send',{'message':'原輸入'},lambda *a:'原回覆',lambda *a:[])
    before=e.load('cast_test')
    def fail(*a):raise ValueError('測試模型失敗')
    monkeypatch.setattr(e,'generate',fail)
    for destination in ('overwrite','fork'):
        res=client.post('/api/card/cast_test/edit',json=body(before['messages'],destination=destination))
        assert res.status_code==400
        assert e.load('cast_test')==before
        assert len(b.tree('cast_test')['branches'])==1


@pytest.mark.parametrize('destination', ['overwrite', 'fork'])
@pytest.mark.parametrize('status', ['error', 'pending', 'dead_pending'])
def test_historical_recorder_failure_does_not_block_snapshot_edit(world, monkeypatch, destination, status):
    import os
    single_turn(text='早期回合')
    single_turn(text='要編輯的回合')
    path = io._story_messages_path('original', 'main')
    messages = io._load_branch_messages('original', 'main')
    messages[2]['card_record_status'] = 'pending' if status == 'dead_pending' else status
    messages[2]['card_record_pid'] = 987654321 if status == 'dead_pending' else os.getpid()
    io._save_json(path, messages)
    before = Path(path).read_bytes()
    expected = copy.deepcopy(messages[-1]['pre_npcs_snapshot'])
    def gm(**kw):
        return '修改後的演出', 'test'
    monkeypatch.setattr(pipeline, '_generate_pass1', gm)
    original = pipeline.run_card_turn
    monkeypatch.setattr(pipeline, 'run_card_turn', lambda *a, **kw: original(
        *a, **kw, recorder_fn=lambda *args: None, async_recorder=False))
    if status == 'pending':
        with pytest.raises(b.Busy):
            editing.edit_turn('original', 'main', body(messages, index=4, destination=destination))
        assert Path(path).read_bytes() == before
        return
    result = editing.edit_turn('original', 'main', body(messages, index=4, destination=destination))
    updated = io._load_branch_messages('original', result['branch_id'])
    assert updated[2] == messages[2]
    assert updated[-1]['pre_npcs_snapshot'] == expected
    assert updated[-1]['card_record_status'] == 'done'
    if destination == 'fork':
        assert Path(path).read_bytes() == before
