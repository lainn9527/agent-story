"""Branch isolation through real file persistence and both card pipelines."""
import copy
import json
import threading
from pathlib import Path
import pytest
from tests.test_card_ensemble import world, make
from story_core import story_io as io, compaction
from story_core.card_mode import branches as b, ensemble as e, ensemble_recorder as er
from story_core.card_mode.pipeline import run_card_turn
from story_core.branch_tree import get_full_timeline


@pytest.fixture(autouse=True)
def no_live_compaction(tmp_path, monkeypatch):
    monkeypatch.setattr(compaction, 'STORIES_DIR', str(tmp_path/'stories'))
    monkeypatch.setattr(compaction, 'compact_async', lambda *a, **k: None)


def single_turn(slug='original', bid='main', text='主線記憶'):
    def recorder(sid, branch, narration, index):
        path = io._story_npcs_path(sid, branch)
        npcs = io._load_json(path, [])
        npcs[0]['memory'].append(text)
        io._save_json(path, npcs)
    return run_card_turn(slug, bid, text, llm_fn=lambda *a, **k: '小蘭：'+text,
                         recorder_fn=recorder, async_recorder=False)


def fork(client, slug='original', bid='main', index=0):
    response = client.post(f'/api/card/{slug}/branches', json={
        'branch_id':bid, 'message_index':index, 'name':'另一個選擇'})
    assert response.status_code == 200, response.json
    return response.json['branch_id']


def test_single_branch_history_memory_recap_reset_and_return(world):
    root, client, _ = world
    single_turn(text='原線已發生')
    main = Path(io._branch_dir('original','main'))
    io._save_json(str(main/'conversation_recap.json'), {'recap_text':'原線未來', 'compacted_through_index':2})
    before = {p.name:p.read_bytes() for p in main.glob('*.json')}
    child = fork(client)
    assert before == {p.name:p.read_bytes() for p in main.glob('*.json')}
    assert not compaction.load_recap('original', child).get('recap_text')
    single_turn(bid=child, text='分支獨有')
    data = client.get('/api/card/original/state').json
    assert data['branch_id'] == child and data['memories'] == ['分支獨有']
    assert '原線已發生' not in json.dumps(data,ensure_ascii=False)
    assert client.post('/api/card/original/branches/switch',json={'branch_id':'main'}).status_code == 200
    assert client.get('/api/card/original/state').json['memories'] == ['原線已發生']
    assert client.post('/api/card/original/reset',json={'branch_id':'main'}).status_code == 200
    assert len(get_full_timeline('original',child)) == 3
    assert client.get('/api/card/original/state?branch_id='+child).json['memories'] == ['分支獨有']
    assert len(client.get('/api/card/original/branches').json['branches']) == 2
    assert client.post('/api/card/original/branches/rename',json={'branch_id':child,'name':'沈晚線'}).status_code == 200
    assert b.tree('original')['branches'][child]['name'] == '沈晚線'


def test_nested_fork_uses_historical_all_npc_snapshot(world):
    _,client,_ = world
    single_turn(text='共同往事')
    child = fork(client,index=2)
    single_turn(bid=child,text='只有A知道')
    grandchild = fork(client,bid=child,index=2)
    assert client.get('/api/card/original/state').json['memories']==['共同往事']
    assert len(get_full_timeline('original',grandchild))==3
    assert client.post('/api/card/original/rewind',json={'branch_id':child,'to_index':0}).status_code==200
    assert len(get_full_timeline('original',grandchild))==3
    assert client.get('/api/card/original/state?branch_id=main').json['memories']==['共同往事']


def test_ensemble_both_characters_stay_branch_local(world, monkeypatch):
    _,client,sid=make(world)
    monkeypatch.setattr(e,'record',lambda *a:[])
    e.mutate('cast_test','focus',{'character_id':sid,'presence':'present'})
    def gm(*a): return '小蘭：「約好週五。」沈晚：「我帶藍色相簿。」'
    def record(*a): return [
        {'character_id':'lead','memories':[{'fact':'約好週五','evidence':'約好週五'}]},
        {'character_id':sid,'memories':[{'fact':'我帶藍色相簿','evidence':'我帶藍色相簿'}]}]
    e.mutate('cast_test','send',{'message':'一起約時間'},gm,record)
    before=copy.deepcopy(e.load('cast_test'))
    child=fork(client,'cast_test',index=2)
    e.mutate('cast_test','focus',{'character_id':'lead','presence':'remote'},branch_id=child)
    captured=[]
    def gm2(user,system,recent):
        captured.append(system)
        return '小蘭：「改約週六。」沈晚：「我改帶紅色相簿。」'
    def record2(*a):return [
        {'character_id':'lead','memories':[{'fact':'改約週六','evidence':'改約週六'}]},
        {'character_id':sid,'memories':[{'fact':'我改帶紅色相簿','evidence':'我改帶紅色相簿'}]}]
    e.mutate('cast_test','send',{'message':'更改計畫'},gm2,record2,branch_id=child)
    assert '約好週五' in captured[0] and '我帶藍色相簿' in captured[0]
    state=e.load('cast_test',child)['state']
    assert '改約週六' in state['characters']['lead']['memory']
    assert '我改帶紅色相簿' in state['characters'][sid]['memory']
    assert e.load('cast_test')==before
    client.post('/api/card/cast_test/branches/switch',json={'branch_id':'main'})
    assert client.get('/api/card/cast_test/state').json['focus']==sid
    # Explicit target wins over the story's active tab.
    data=client.get('/api/card/cast_test/state?branch_id='+child).json
    assert data['focus']=='lead' and '改約週六' in data['memories']
    e.mutate('cast_test','reset',{},branch_id=child)
    assert e.load('cast_test')==before


def test_async_recorder_keeps_original_branch_when_active_changes(world, monkeypatch):
    _,client,sid=make(world)
    child=fork(client,'cast_test',index=0)
    entered=threading.Event(); release=threading.Event()
    def record(*a):
        entered.set(); assert release.wait(5)
        return [{'character_id':'lead','memories':[{'fact':'只在分支發生','evidence':'只在分支發生'}]}]
    # Use underlying implementation to deliberately hold a real background task.
    e.mutate.func('cast_test','send',{'message':'說吧'},lambda *a:'只在分支發生',record,
                  branch_id=child,async_recorder=True)
    assert entered.wait(5)
    try:
        response=client.post('/api/card/cast_test/branches',json={'branch_id':child,'message_index':2})
        assert response.status_code==409
        assert client.post('/api/card/cast_test/branches/switch',json={'branch_id':'main'}).status_code==200
    finally: release.set()
    worker=er.start('cast_test',record,branch_id=child)
    if worker: worker.join(5)
    assert '只在分支發生' in e.load('cast_test',child)['state']['characters']['lead']['memory']
    assert not e.load('cast_test')['state']['characters']['lead']['memory']


@pytest.mark.parametrize('bid',['../main','missing','',42,False])
def test_invalid_branch_never_falls_back_to_main(world,bid):
    _,client,_=world
    before=Path(io._story_messages_path('original','main')).read_bytes()
    assert client.post('/api/card/original/reset',json={'branch_id':bid}).status_code==400
    assert Path(io._story_messages_path('original','main')).read_bytes()==before


def test_invalid_fork_and_missing_snapshot_are_non_destructive(world):
    _,client,_=world
    single_turn()
    path=io._story_messages_path('original','main')
    msgs=io._load_json(path,[]);del msgs[0]['clock_snapshot'];io._save_json(path,msgs)
    assert client.post('/api/card/original/branches',json={'message_index':0}).status_code==400
    assert client.post('/api/card/original/branches',json={'message_index':1}).status_code==400
    assert len(b.tree('original')['branches'])==1
    assert len(list(Path(io._story_dir('original')).glob('branches/card_*')))==0


def test_source_branch_used_for_ensemble_clone(world):
    _,client,spec=world
    single_turn(text='主線事件')
    child=fork(client,index=0);single_turn(bid=child,text='另一條線的事')
    response=client.post('/api/card/original/ensemble',json={'target_slug':'new_cast','branch_id':child,'character':spec})
    assert response.status_code==200,response.json
    assert e.load('new_cast')['state']['characters']['lead']['memory']==['另一條線的事']


def test_single_stream_captures_branch_before_switch(world, monkeypatch):
    _,client,_=world
    child=fork(client)
    captured=[]
    def run(**kwargs):
        captured.append(kwargs['branch_id'])
        b.change('original','switch',{'branch_id':'main'})
        return single_turn(bid=kwargs['branch_id'],text='串流分支記憶')
    monkeypatch.setattr('routes.card_routes.run_card_turn',run)
    response=client.post('/api/card/original/send/stream',json={'branch_id':child,'message':'繼續'})
    assert '"type": "done"' in response.text
    assert captured==[child]
    assert client.get('/api/card/original/state').json['memories']==[]
    assert client.get('/api/card/original/state?branch_id='+child).json['memories']==['串流分支記憶']


def test_single_async_record_fork_is_busy_switch_is_safe(world):
    _,client,_=world
    child=fork(client)
    entered=threading.Event();release=threading.Event();finished=threading.Event()
    def recorder(slug,bid,*args):
        entered.set();assert release.wait(5)
        path=io._story_npcs_path(slug,bid);npcs=io._load_json(path,[])
        npcs[0]['memory'].append('分支專屬背景記錄');io._save_json(path,npcs)
        finished.set()
    run_card_turn('original',child,'新訊息',llm_fn=lambda *a,**k:'回應',recorder_fn=recorder)
    assert entered.wait(5)
    try:
        assert client.post('/api/card/original/branches',json={'branch_id':child,'message_index':2}).status_code==409
        assert client.post('/api/card/original/branches/switch',json={'branch_id':'main'}).status_code==200
    finally:release.set()
    assert finished.wait(5)
    with b.lock('original',child,blocking=True):
        assert client.get('/api/card/original/state?branch_id='+child).json['memories']==['分支專屬背景記錄']
    assert client.get('/api/card/original/state').json['memories']==[]


def test_failed_or_interrupted_record_can_regen_but_not_fork(world,monkeypatch):
    _,client,_=world
    single_turn()
    path=io._story_messages_path('original','main');msgs=io._load_json(path,[])
    msgs[-1]['card_record_status']='pending';msgs[-1]['card_record_pid']=987654321
    io._save_json(path,msgs)
    assert client.post('/api/card/original/branches',json={'message_index':2}).status_code==400
    monkeypatch.setattr('routes.card_routes.run_card_turn',lambda **kw:{'ok':True})
    assert client.post('/api/card/original/regenerate',json={}).status_code==200


def test_recall_and_generation_use_branch_only(world,monkeypatch):
    from story_core.card_mode import recall
    _,client,_=world
    single_turn(text='主線獨有秘密')
    child=fork(client,index=0)
    captures=[]
    def collect(slug,bid,mode,user,history,**kwargs):
        captures.append((bid,copy.deepcopy(history),kwargs['memories']))
        return {"mode":"off"}
    monkeypatch.setattr(recall,'collect',collect)
    single_turn(bid=child,text='分支新事')
    assert captures[0][0]==child
    assert '主線獨有秘密' not in json.dumps(captures[0],ensure_ascii=False)


def test_ensemble_branch_regen_and_rewind_leave_parent(world):
    _,client,_=make(world)
    e.mutate('cast_test','send',{'message':'共同'},lambda *a:'共同往事',lambda *a:[])
    original=copy.deepcopy(e.load('cast_test'))
    child=fork(client,'cast_test',index=2)
    e.mutate('cast_test','send',{'message':'分支'},lambda *a:'原回應',lambda *a:[],branch_id=child)
    e.mutate('cast_test','regenerate',{},lambda *a:'新的回應',lambda *a:[],branch_id=child)
    assert e.load('cast_test',child)['messages'][-1]['content']=='新的回應'
    e.mutate('cast_test','rewind',{'to_index':2},branch_id=child)
    assert len(e.load('cast_test',child)['messages'])==3
    assert e.load('cast_test')==original


@pytest.mark.parametrize("recorder_model", [None, "gemini-3.1-flash-lite"])
def test_vertex_recorder_uses_selected_model_and_commits_memory(world, monkeypatch, recorder_model):
    from story_core.card_mode import pipeline
    from story_core import llm_bridge
    _,client,_ = world
    child = fork(client)
    monkeypatch.setattr(pipeline, '_generate_pass1', lambda **kw: ('小蘭答應星期五帶講義。','test'))
    monkeypatch.setattr(llm_bridge, '_get_config', lambda: {'provider':'vertex','vertex':{'model':'selected-vertex-model', 'recorder_model':recorder_model}})
    monkeypatch.setattr(llm_bridge, 'get_provider', lambda: 'vertex')
    monkeypatch.setattr(llm_bridge, '_capture_usage', lambda *a: None)
    monkeypatch.setattr('story_core.usage_db.log_from_bridge', lambda *a,**kw: None)
    def wrong_backend(**kwargs):
        pytest.fail('Vertex recorder must not call Gemini API')
    monkeypatch.setattr('story_core.gemini_bridge.call_gemini_gm_tools',wrong_backend)
    called=[]
    def vertex(*args, **kwargs):
        called.append(kwargs['model'])
        result=kwargs['on_tool']('remember',{'facts':['小蘭答應星期五帶講義。']})
        assert result['ok']
        return '',[], 'ok'
    monkeypatch.setattr('story_core.vertex_bridge.call_vertex_gm_tools',vertex)
    result=pipeline.run_card_turn('original',child,'星期五帶講義好嗎',async_recorder=False)
    assert result['ok'] and called==[recorder_model or 'selected-vertex-model']
    assert client.get('/api/card/original/state?branch_id='+child).json['memories']==['小蘭答應星期五帶講義。']
    assert client.get('/api/card/original/state?branch_id=main').json['memories']==[]
    assert io._load_branch_messages('original',child)[-1]['card_record_status']=='done'


def test_reviewed_memory_repair_allows_fork_without_clearing_error(world):
    _, client, _ = world
    single_turn(text='已補回的記憶')
    path = io._story_messages_path('original', 'main')
    messages = io._load_json(path, [])
    messages[-1]['card_record_status'] = 'error'
    messages[-1]['card_memory_backfill'] = {'status':'reviewed','at':'2026-09-24T00:00:00Z','added':1}
    io._save_json(path, messages)
    single_turn(text='後續記憶')
    original = io._load_json(path, [])
    for index, memories in [(2, ['已補回的記憶']), (4, ['已補回的記憶','後續記憶'])]:
        child = fork(client, index=index)
        assert client.get('/api/card/original/state?branch_id='+child).json['memories'] == memories
        assert io._load_branch_messages('original', child)[2]['card_record_status'] == 'error'
    assert io._load_json(path, []) == original


@pytest.mark.parametrize('historical', [False, True])
@pytest.mark.parametrize('status', ['error', 'dead_pending'])
def test_completed_checkpoint_can_fork_after_historical_recorder_failure(world, historical, status):
    _, client, _ = world
    single_turn(text='早期回合')
    path = io._story_messages_path('original', 'main')
    messages = io._load_json(path, [])
    messages[2]['card_record_status'] = 'pending' if status == 'dead_pending' else status
    messages[2]['card_record_pid'] = 987654321
    interrupted = copy.deepcopy(messages[2])
    io._save_json(path, messages)
    single_turn(text='已完成的分叉點')
    checkpoint = copy.deepcopy(io._load_json(path, [])[-1])
    if historical:
        single_turn(text='原線的未來')
        messages = io._load_json(path, [])
        messages[-1]['card_record_status'] = 'pending'
        io._save_json(path, messages)
    source = Path(io._branch_dir('original', 'main'))
    before = {p.name: p.read_bytes() for p in source.glob('*.json')}
    child = fork(client, index=4)
    assert before == {p.name: p.read_bytes() for p in source.glob('*.json')}
    kept = io._load_branch_messages('original', child)
    assert len(kept) == 5 and kept[2] == interrupted
    assert kept[-1] == checkpoint
    for field, name in [('state_snapshot', 'character_state.json'),
                        ('npcs_snapshot', 'npcs.json'), ('clock_snapshot', 'card_clock.json')]:
        assert io._load_json(str(Path(io._branch_dir('original', child)) / name), None) == checkpoint[field]


@pytest.mark.parametrize('status', ['pending', 'unknown_pending', 'target_error', 'ensemble_error'])
def test_checkpoint_never_bypasses_unfinished_retained_work(world, status):
    import os
    _, client, _ = world
    single_turn(text='早期回合')
    single_turn(text='分叉點')
    path = io._story_messages_path('original', 'main')
    messages = io._load_json(path, [])
    if status == 'target_error':
        messages[-1]['card_record_status'] = 'error'
    elif status == 'ensemble_error':
        messages[2]['ensemble_record_status'] = 'error'
    else:
        messages[2]['card_record_status'] = 'pending'
        if status == 'pending':
            messages[2]['card_record_pid'] = os.getpid()
        else:
            messages[2].pop('card_record_pid', None)
    io._save_json(path, messages)
    before = Path(path).read_bytes()
    response = client.post('/api/card/original/branches', json={'message_index': 4})
    assert response.status_code in (400, 409)
    assert len(b.tree('original')['branches']) == 1
    assert Path(path).read_bytes() == before


@pytest.mark.parametrize('field,value', [
    ('state_snapshot', None), ('state_snapshot', []),
    ('npcs_snapshot', None), ('npcs_snapshot', {}), ('npcs_snapshot', []),
    ('clock_snapshot', None), ('clock_snapshot', []), ('clock_snapshot', {})])
@pytest.mark.parametrize('status', ['error', 'dead_pending'])
def test_historical_failure_requires_complete_selected_checkpoint(world, field, value, status):
    _, client, _ = world
    single_turn(text='早期回合')
    single_turn(text='分叉點')
    path = io._story_messages_path('original', 'main')
    messages = io._load_json(path, [])
    messages[2]['card_record_status'] = 'pending' if status == 'dead_pending' else status
    messages[2]['card_record_pid'] = 987654321
    messages[-1][field] = value
    io._save_json(path, messages)
    before = Path(path).read_bytes()
    response = client.post('/api/card/original/branches', json={'message_index': 4})
    assert response.status_code == 400
    assert len(b.tree('original')['branches']) == 1
    assert Path(path).read_bytes() == before


@pytest.mark.parametrize('invalid', ['unreviewed', 'missing_snapshot', 'pending', 'ensemble', 'malformed'])
def test_memory_repair_does_not_bypass_unsettled_or_incomplete_turns(world, invalid):
    _, client, _ = world
    single_turn()
    path = io._story_messages_path('original', 'main')
    messages = io._load_json(path, [])
    msg = messages[-1]
    msg['card_record_status'] = 'error'
    msg['card_memory_backfill'] = {'status':'reviewed','at':'2026-09-24T00:00:00Z','added':0}
    if invalid == 'unreviewed': msg['card_memory_backfill']['status'] = 'proposed'
    if invalid == 'missing_snapshot': msg.pop('npcs_snapshot')
    if invalid == 'pending': msg['card_record_status'] = 'pending'
    if invalid == 'ensemble': msg['ensemble_record_status'] = 'error'
    if invalid == 'malformed': msg['card_memory_backfill'] = 'reviewed'
    io._save_json(path, messages)
    response = client.post('/api/card/original/branches', json={'branch_id':'main','message_index':2})
    assert response.status_code in (400, 409)
    assert len(b.tree('original')['branches']) == 1


@pytest.mark.parametrize('ensemble_mode', [False, True])
def test_delete_leaf_preserves_other_saves_and_blocks_access(world, ensemble_mode):
    _, client, _ = world
    slug = 'original'
    if ensemble_mode:
        make(world)
        slug = 'cast_test'
    child = fork(client, slug)
    main = Path(io._story_dir(slug))
    before = {str(p): p.read_bytes() for p in main.rglob('*.json')
              if p.name != 'timeline_tree.json'}
    response = client.post(f'/api/card/{slug}/branches/delete', json={'branch_id':child})
    assert response.status_code == 200, response.json
    assert response.json['branch_id'] == response.json['active_branch_id'] == 'main'
    assert child not in [x['id'] for x in response.json['branches']]
    assert before == {str(p): p.read_bytes() for p in main.rglob('*.json')
                      if p.name != 'timeline_tree.json'}
    for action in ('switch', 'rename', 'delete'):
        assert client.post(f'/api/card/{slug}/branches/{action}',
                           json={'branch_id':child, 'name':'gone'}).status_code == 400
    assert client.get(f'/api/card/{slug}/state?branch_id={child}').status_code == 400
    assert client.get(f'/api/card/{slug}/state').status_code == 200


def test_delete_main_and_pending_are_rejected(world):
    _, client, _ = world
    assert client.post('/api/card/original/branches/delete', json={'branch_id':'main'}).status_code == 400
    child = fork(client)
    grandchild = fork(client, bid=child)
    path = io._story_messages_path('original', grandchild)
    messages = io._load_json(path, [])
    messages[-1]['card_record_status'] = 'pending'
    io._save_json(path, messages)
    assert client.post('/api/card/original/branches/delete', json={'branch_id':grandchild}).status_code == 409
    messages[-1]['card_record_status'] = 'error'
    io._save_json(path, messages)
    assert client.post('/api/card/original/branches/delete', json={'branch_id':grandchild}).status_code == 200
    assert client.post('/api/card/original/branches/delete', json={'branch_id':child}).status_code == 200


@pytest.mark.parametrize('ensemble_mode', [False, True])
def test_delete_parent_reparents_children_without_changing_snapshots(world, ensemble_mode):
    _, client, _ = world
    slug = 'original'
    if ensemble_mode:
        make(world)
        slug = 'cast_test'
    ancestor = fork(client, slug)
    parent = fork(client, slug, bid=ancestor)
    child = fork(client, slug, bid=parent)
    sibling = fork(client, slug, bid=parent)
    descendant = fork(client, slug, bid=child)
    root = Path(io._story_dir(slug))
    before = {str(p): p.read_bytes() for p in root.rglob('*.json')
              if p.name != 'timeline_tree.json'}
    original_point = b.tree(slug)['branches'][child]['branch_point_index']
    result = client.post(f'/api/card/{slug}/branches/delete', json={'branch_id':parent})
    assert result.status_code == 200, result.json
    assert result.json['branch_id'] == result.json['switch_to'] == ancestor
    assert result.json['active_branch_id'] == descendant
    tree = b.tree(slug)
    assert tree['branches'][child]['parent_branch_id'] == ancestor
    assert tree['branches'][sibling]['parent_branch_id'] == ancestor
    assert tree['branches'][descendant]['parent_branch_id'] == child
    assert tree['branches'][child]['branch_point_index'] == original_point
    assert before == {str(p): p.read_bytes() for p in root.rglob('*.json')
                      if p.name != 'timeline_tree.json'}
    for bid in (child, sibling, descendant):
        assert client.post(f'/api/card/{slug}/branches/switch', json={'branch_id':bid}).status_code == 200
        assert client.get(f'/api/card/{slug}/state?branch_id={bid}').status_code == 200
    # Active parent deletion switches to its own parent; surviving children still fork.
    client.post(f'/api/card/{slug}/branches/switch', json={'branch_id':ancestor})
    result = client.post(f'/api/card/{slug}/branches/delete', json={'branch_id':ancestor})
    assert result.json['active_branch_id'] == 'main'
    assert b.tree(slug)['branches'][child]['parent_branch_id'] == 'main'
    assert fork(client, slug, bid=child)
