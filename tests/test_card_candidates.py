"""Reply choices preserve state, cancelled recorders and branch provenance."""
from copy import deepcopy
from pathlib import Path
import threading

import pytest
from tests.test_card_ensemble import world, make
from tests.test_card_branches import no_live_compaction
from story_core import story_io as io, compaction, gemini_bridge, llm_bridge
from story_core.card_mode import candidates as c, pipeline as p, branches, ensemble as e, ensemble_recorder as er, clock


def rows(bid='main'):
    return io._load_branch_messages('original', bid)


def record(sid, bid, text, index):
    npcs = io._load_json(io._story_npcs_path(sid, bid), [])
    npcs[0]['memory'].append(text)
    npcs[0]['relationship'] = {'tier':'友好' if '第一' in text else '信任', 'note':text}
    io._save_json(io._story_npcs_path(sid, bid), npcs)
    io._save_json(io._story_character_state_path(sid, bid), {'inventory':{text:'信物'}})


def initial():
    return p.run_card_turn('original', user_message='你怎麼想？', llm_fn=lambda *a:'第一版', recorder_fn=record, async_recorder=False)


def regen(client, monkeypatch, text='第二版', stream=False, bid='main'):
    import routes.card_routes as routes
    original = p.run_card_turn
    monkeypatch.setattr(routes, 'run_card_turn', lambda **kw: original(**kw, llm_fn=lambda *a:text, recorder_fn=record, async_recorder=False))
    return client.post('/api/card/original/regenerate', json={'branch_id':bid, 'stream':stream, 'narrative_style':'natural', 'plot_mode':'coauthor'})


def versions(client, index=2, bid='main'):
    response = client.get(f'/api/card/original/candidates?branch_id={bid}&message_index={index}')
    assert response.status_code == 200, response.json
    return response.json


def select(client, data, number=0, bid='main'):
    return client.post('/api/card/original/candidates', json={'action':'select', 'branch_id':bid,
        'group_id':data['group_id'], 'version_id':data['versions'][number]['id'],
        'expected_version_id':data['selected_id'], 'message_index':data['message_index']})


@pytest.mark.parametrize('status', ['error', 'pending', 'dead_pending'])
def test_historical_choice_uses_checkpoint_after_earlier_failure(world, monkeypatch, status):
    import os
    _, client, _ = world
    initial()
    p.run_card_turn('original', user_message='分叉點', llm_fn=lambda *a:'分叉原版',
                    recorder_fn=record, async_recorder=False)
    checkpoint = deepcopy(rows()[-1])
    regen(client, monkeypatch, text='分叉新版')
    p.run_card_turn('original', user_message='後續', llm_fn=lambda *a:'原線未來',
                    recorder_fn=record, async_recorder=False)
    path = io._story_messages_path('original', 'main')
    messages = rows(); messages[2]['card_record_status'] = 'pending' if status == 'dead_pending' else status
    messages[2]['card_record_pid'] = 987654321 if status == 'dead_pending' else os.getpid()
    io._save_json(path, messages)
    source = Path(io._branch_dir('original', 'main'))
    before = {p.name:p.read_bytes() for p in source.glob('*.json')}
    response = select(client, versions(client, index=4))
    assert before == {p.name:p.read_bytes() for p in source.glob('*.json')}
    if status == 'pending':
        assert response.status_code == 409
        assert len(branches.tree('original')['branches']) == 1
        return
    assert response.status_code == 200, response.json
    child = response.json['branch_id']
    assert child != 'main' and len(rows(child)) == 5
    assert rows(child)[2] == messages[2]
    assert rows(child)[-1]['content'] == checkpoint['content']
    assert io._load_json(io._story_npcs_path('original', child), []) == checkpoint['npcs_snapshot']


@pytest.mark.parametrize('stream', [False, True])
def test_regen_history_selection_and_next_context(world, monkeypatch, stream):
    _, client, _ = world
    initial(); old = deepcopy(rows()[-1])
    response = regen(client, monkeypatch, stream=stream)
    assert response.status_code == 200
    if stream: assert '"type": "done"' in response.text
    data = versions(client)
    assert [v['content'] for v in data['versions']] == ['第一版','第二版']
    assert data['regen_count'] == 1 and data['failed_count'] == 0
    assert data['versions'][1]['narrative_style'] == 'natural'
    assert data['versions'][1]['plot_mode'] == 'coauthor'
    assert data['versions'][1]['generated_at'] and data['versions'][1]['provider'] == 'custom'
    state = client.get('/api/card/original/state').json
    assert state['messages'][-1]['candidate_summary']['selected_number'] == 2
    assert select(client, data).status_code == 200
    assert rows()[-1]['content'] == '第一版' and rows()[-1]['card_turn_id'] != old['card_turn_id']
    assert io._load_json(io._story_npcs_path('original','main'),[])[0]['memory'] == ['第一版']
    assert io._load_json(io._story_character_state_path('original','main'),{}) == old['state_snapshot']
    assert clock.load_clock('original','main') == old['clock_snapshot']
    assert len(branches.tree('original')['branches']) == 1
    captured=[]
    p.run_card_turn('original', user_message='然後呢？', llm_fn=lambda u,s,h:(captured.append((s,h)) or '後續'), recorder_fn=lambda *a:None, async_recorder=False)
    assert '第二版' not in str(captured)
    assert '第一版' in str(captured)


@pytest.mark.parametrize('stream', [False, True])
def test_failed_regen_counted_and_original_restored(world, monkeypatch, stream):
    _, client, _ = world; initial()
    old = deepcopy(rows()[-1])
    response = regen(client, monkeypatch, text='', stream=stream)
    assert response.status_code == 200
    assert rows()[-1]['content'] == '第一版'
    assert rows()[-1]['npcs_snapshot'] == old['npcs_snapshot']
    data = versions(client)
    assert len(data['versions']) == 1 and data['regen_count'] == data['failed_count'] == 1
    assert data['attempts'][0]['status'] == 'failed'
    assert regen(client, monkeypatch).status_code == 200
    data = versions(client)
    assert data['regen_count'] == 2 and data['failed_count'] == 1 and len(data['versions']) == 2


def test_historical_choice_forks_and_inherits_only_retained_groups(world, monkeypatch):
    _, client, _ = world; initial(); regen(client, monkeypatch)
    p.run_card_turn('original', user_message='未來問題', llm_fn=lambda *a:'未來版', recorder_fn=record, async_recorder=False)
    regen(client, monkeypatch, text='未來候選')
    main = Path(io._branch_dir('original','main'))
    before = {f.name:f.read_bytes() for f in main.glob('*.json')}
    data = versions(client)
    response = select(client, data)
    assert response.status_code == 200, response.json
    bid = response.json['branch_id']; assert bid != 'main'
    assert before == {f.name:f.read_bytes() for f in main.glob('*.json')}
    assert len(rows(bid)) == 3 and rows(bid)[-1]['content'] == '第一版'
    assert io._load_json(io._story_npcs_path('original',bid),[])[0]['memory'] == ['第一版']
    assert len(c.load('original',bid)['groups']) == 1
    assert len(versions(client,bid=bid)['versions']) == 2
    assert branches.tree('original')['active_branch_id'] == bid


def test_favorite_survives_reset_without_attaching_to_reused_index(world, monkeypatch):
    _, client, _ = world; initial(); regen(client, monkeypatch)
    data = versions(client)
    response = client.post('/api/card/original/candidates', json={'action':'favorite', 'group_id':data['group_id'],
        'version_id':data['versions'][0]['id'], 'favorite':True})
    assert response.status_code == 200
    client.post('/api/card/original/reset', json={})
    initial()
    later = versions(client)
    assert later['group_id'] != data['group_id'] and later['regen_count'] == 0 and len(later['versions']) == 1
    saved = client.get('/api/card/original/candidates?favorites=1').json['favorites']
    assert len(saved) == 1 and saved[0]['content'] == '第一版'
    assert select(client,data).status_code == 400


def test_stale_selection_and_missing_snapshot_are_rejected(world, monkeypatch):
    _, client, _ = world; initial(); regen(client, monkeypatch)
    data = versions(client); regen(client, monkeypatch, text='第三版')
    assert select(client,data).status_code == 400
    data = versions(client); archive = c.load('original','main')
    archive['groups'][data['group_id']]['versions'][0]['message'].pop('pre_clock_snapshot')
    c.save('original','main',archive)
    before = deepcopy(rows())
    assert select(client,data).status_code == 400
    assert rows() == before


def test_switching_pending_candidate_replays_only_recorder_and_rejects_old_worker(world, monkeypatch):
    _, client, _ = world
    entered, release = threading.Event(), threading.Event(); calls=[]; late=[]
    monkeypatch.setattr(llm_bridge,'_get_config', lambda:{})
    def provider(**kw):
        calls.append(kw['user_message']); tool=kw['on_tool']
        text = '第一版' if '第一版' in kw['user_message'] else '第二版'
        tool('remember',{'facts':[text]})
        if len(calls) == 1:
            entered.set(); assert release.wait(5)
            late.append(tool('remember',{'facts':['過期記憶']}))
        return '', [], 'ok'
    monkeypatch.setattr(gemini_bridge,'call_gemini_gm_tools', provider)
    original = p.run_card_turn
    import routes.card_routes as routes
    monkeypatch.setattr(routes,'run_card_turn',lambda **kw: original(**kw,llm_fn=lambda *a:'第二版',async_recorder=False))
    try:
        original('original',user_message='你怎麼想？',llm_fn=lambda *a:'第一版')
        assert entered.wait(2)
        assert client.post('/api/card/original/regenerate',json={}).status_code == 200
        data = versions(client)
        assert select(client,data).status_code == 200
        for thread in threading.enumerate():
            if thread.name.startswith('card-recorder-') and thread.name != 'card-recorder-'+data['versions'][0]['id']:
                thread.join(2)
        assert io._load_json(io._story_npcs_path('original','main'),[])[0]['memory'] == ['第一版']
        release.set()
        for thread in threading.enumerate():
            if thread.name.startswith('card-recorder-'): thread.join(2)
        assert late and late[0]['ok'] is False and len(calls) == 3
        assert rows()[-1]['card_record_status'] == 'done'
        assert io._load_json(io._story_npcs_path('original','main'),[])[0]['memory'] == ['第一版']
    finally: release.set()


def test_ensemble_choice_restores_every_actor_and_controls(world, monkeypatch):
    _, client, sid = make(world)
    e.mutate('cast_test','focus',{'character_id':sid,'presence':'present'})
    e.mutate('cast_test','send',{'message':'你帶什麼？'},lambda *a:'沈晚：「我帶藍色相簿。」',
        lambda *a:[{'character_id':sid,'memories':[{'fact':'我帶藍色相簿','evidence':'我帶藍色相簿'}]}])
    old = deepcopy(e.load('cast_test')['state'])
    e.mutate('cast_test','regenerate',{},lambda *a:'沈晚：「我帶紅色相簿。」',
        lambda *a:[{'character_id':sid,'memories':[{'fact':'我帶紅色相簿','evidence':'我帶紅色相簿'}]}])
    e.mutate('cast_test','focus',{'character_id':'lead','presence':'remote'})
    data = client.get('/api/card/cast_test/candidates?message_index=2').json
    response = client.post('/api/card/cast_test/candidates',json={'action':'select','message_index':2,
        'group_id':data['group_id'],'version_id':data['versions'][0]['id'],'expected_version_id':data['selected_id']})
    assert response.status_code == 200,response.json
    doc = e.load('cast_test')
    assert doc['state']['characters'][sid]['memory'] == old['characters'][sid]['memory']
    assert doc['state']['focus'] == 'lead' and doc['state']['characters']['lead']['presence'] == 'remote'
    assert '紅色' not in str(doc['state'])
    public = client.get('/api/card/cast_test/state').json
    assert public['messages'][-1]['candidate_summary']['versions'] == 2


def test_memory_corrections_survive_archived_version_selection(world, monkeypatch):
    _,client,_=world; initial()
    p.run_card_turn('original', user_message='第二個問題', llm_fn=lambda *a:'第二輪原版', recorder_fn=record, async_recorder=False)
    regen(client,monkeypatch,text='第二輪候選')
    memory=client.get('/api/card/original/memories').json
    response=client.post('/api/card/original/memories',json={'revision':memory['revision'],
        'changes':[{'character_id':'0','index':0,'text':'已校正的往事'}]})
    assert response.status_code==200,response.json
    data=versions(client,index=4)
    assert select(client,data).status_code==200
    assert io._load_json(io._story_npcs_path('original','main'),[])[0]['memory']==['已校正的往事','第二輪原版']


def test_selection_failure_rolls_back_files_and_fork_registration(world, monkeypatch):
    _,client,_=world; initial();regen(client,monkeypatch)
    data=versions(client); original_save=c.save
    main=Path(io._branch_dir('original','main'))
    before={str(f.relative_to(main)):f.read_bytes() for f in main.rglob('*.json')}
    def fail(*args): raise OSError('模擬磁碟失敗')
    monkeypatch.setattr(c,'save',fail)
    with pytest.raises(OSError): select(client,data)
    assert before=={str(f.relative_to(main)):f.read_bytes() for f in main.rglob('*.json')}
    monkeypatch.setattr(c,'save',original_save)
    p.run_card_turn('original',user_message='後續',llm_fn=lambda *a:'後續版',recorder_fn=record,async_recorder=False)
    monkeypatch.setattr(c,'save',fail)
    with pytest.raises(OSError): select(client,data)
    assert len(branches.tree('original')['branches'])==1
    assert [d.name for d in main.parent.iterdir() if d.is_dir()]==['main']


def test_regular_fork_and_edit_fork_preserve_prior_candidates(world, monkeypatch):
    _,client,_=world; initial();regen(client,monkeypatch)
    response=client.post('/api/card/original/branches',json={'message_index':2,'name':'保存候選'})
    bid=response.json['branch_id']
    assert len(versions(client,bid=bid)['versions'])==2
    p.run_card_turn('original',bid,user_message='新問題',llm_fn=lambda *a:'新回覆',recorder_fn=record,async_recorder=False)
    original=p.run_card_turn
    monkeypatch.setattr(p,'run_card_turn',lambda *a,**kw:original(*a,**kw,llm_fn=lambda *a:'修改回覆',recorder_fn=record,async_recorder=False))
    response=client.post('/api/card/original/edit',json={'branch_id':bid,'message_index':4,'expected_reply':'新回覆',
        'message':'改過的問題','mode':'regenerate','destination':'fork'})
    assert response.status_code==200,response.json
    edited=response.json['branch_id']
    assert len(versions(client,bid=edited)['versions'])==2
    assert versions(client,index=4,bid=edited)['regen_count']==0


@pytest.mark.parametrize('end',['error','incomplete','missing_done'])
def test_partial_regen_is_failure_and_restores_original(world,monkeypatch,end):
    _,client,_=world;initial()
    def stream(*a,**kw):
        yield 'text','只有半段的故事'
        if end=='error':yield 'error','連線中斷'
        elif end=='incomplete':yield 'done',{'incomplete':True}
    monkeypatch.setattr(llm_bridge,'call_claude_gm_stream',stream)
    # No fallback may replace an already shown partial stream with a second story.
    monkeypatch.setattr(llm_bridge,'call_claude_gm',lambda *a,**kw:pytest.fail('unexpected second narration'))
    response=client.post('/api/card/original/regenerate',json={'stream':True})
    assert response.status_code==200
    assert '"ok": false' in response.get_data(as_text=True)
    assert rows()[-1]['content']=='第一版'
    data=versions(client)
    assert data['failed_count']==1 and len(data['versions'])==1


def test_ensemble_pending_choice_is_re_recorded_and_historical_fork_isolated(world,monkeypatch):
    _,client,sid=make(world)
    e.mutate('cast_test','send',{'message':'你好'},lambda *a:'小蘭：「藍色相簿。」',lambda *a:[])
    original=deepcopy(e.load('cast_test'))
    original['messages'][-1]['ensemble_record_status']='pending'
    e.save('cast_test',original)
    monkeypatch.setattr(er,'start',lambda *a,**kw:None)
    e.mutate('cast_test','regenerate',{},lambda *a:'小蘭：「紅色相簿。」',lambda *a:[])
    e.mutate('cast_test','send',{'message':'後來？'},lambda *a:'後來的回覆',lambda *a:[])
    parent=deepcopy(e.load('cast_test'))
    data=client.get('/api/card/cast_test/candidates?message_index=2').json
    response=client.post('/api/card/cast_test/candidates',json={'action':'select','message_index':2,
        'group_id':data['group_id'],'version_id':data['versions'][0]['id'],'expected_version_id':data['selected_id']})
    assert response.status_code==200,response.json
    bid=response.json['branch_id'];assert bid!='main'
    assert e.load('cast_test')==parent
    assert e.load('cast_test',bid)['messages'][-1]['ensemble_record_status']=='pending'
    er.drain('cast_test',lambda *a:[{'character_id':'lead','memories':[{'fact':'藍色相簿','evidence':'藍色相簿'}]}],branch_id=bid)
    doc=e.load('cast_test',bid)
    assert doc['messages'][-1]['ensemble_record_status']=='done'
    assert '藍色相簿' in doc['state']['characters']['lead']['memory']
    assert '紅色相簿' not in str(doc['state'])
