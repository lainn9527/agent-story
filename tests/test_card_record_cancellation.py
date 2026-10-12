"""Exercise the production recorder callback path while provider I/O is blocked."""
import copy
import json
import threading

import pytest
from tests.test_card_branches import world, fork, single_turn, no_live_compaction
from story_core import story_io as io, compaction, gemini_bridge, llm_bridge
from story_core.card_mode import branches, clock
from story_core.card_mode.pipeline import run_card_turn


@pytest.fixture
def recording(world, monkeypatch):
    entered=threading.Event();release=threading.Event();late=[];jobs=[]
    monkeypatch.setattr(llm_bridge,'_get_config',lambda:{'provider':'vertex'})
    monkeypatch.setattr(compaction,'compact_async',lambda *a,**k:jobs.append((a,k)))
    def provider(**kwargs):
        tool=kwargs['on_tool']
        if '舊回覆' in kwargs['user_message']:
            assert tool('remember',{'facts':['被撤銷的秘密']})['ok']
            assert tool('set_relationship',{'tier':'信任','note':'被撤銷的關係'})['ok']
            assert tool('add_inventory',{'items':{'舊信物':'被撤銷'}})['ok']
            assert tool('advance_time',{'minutes':60})['ok']
            entered.set();assert release.wait(5)
            late.append(tool('remember',{'facts':['遲到的記憶']}))
        else:
            tool('remember',{'facts':['新版記憶']})
        return '',[], 'ok'
    monkeypatch.setattr(gemini_bridge,'call_gemini_gm_tools',provider)
    import routes.card_routes as routes
    def regen(**kwargs):
        kwargs['async_recorder']=False
        return run_card_turn(**kwargs,llm_fn=lambda *a:'新版回覆')
    monkeypatch.setattr(routes,'run_card_turn',regen)
    yield entered,release,late,jobs
    release.set()
    for thread in threading.enumerate():
        if thread.name.startswith('card-recorder-'):
            thread.join(5);assert not thread.is_alive()


@pytest.mark.parametrize('action',['regenerate','rewind','reset'])
@pytest.mark.parametrize('child',[False,True])
def test_cancel_pending_record_immediately_and_reject_late_tools(world,recording,action,child):
    _,client,_=world;entered,release,late,jobs=recording
    bid=fork(client) if child else 'main'
    parent=copy.deepcopy(io._load_branch_messages('original','main'))
    initial_clock=clock.load_clock('original',bid)
    run_card_turn('original',bid,'原始問題',llm_fn=lambda *a:'舊回覆')
    assert entered.wait(2)
    old=io._load_branch_messages('original',bid)[-1]
    # Tools already ran, but all effects remain private while the API is pending.
    assert not io._load_json(io._story_npcs_path('original',bid),[])[0]['memory']
    assert clock.load_clock('original',bid)==initial_clock
    response=client.post('/api/card/original/'+action,json={'branch_id':bid,'to_index':0})
    assert response.status_code==200,response.json
    assert not release.is_set()
    before={p:io._load_json(p,None) for p in [io._story_messages_path('original',bid),io._story_npcs_path('original',bid),io._story_character_state_path('original',bid),clock._clock_path('original',bid)]}
    if action=='regenerate':
        new=before[io._story_messages_path('original',bid)][-1]
        assert new['index']==old['index'] and new['card_turn_id']!=old['card_turn_id']
        assert before[io._story_npcs_path('original',bid)][0]['memory']==['新版記憶']
    release.set()
    for thread in threading.enumerate():
        if thread.name=='card-recorder-'+old['card_turn_id']:thread.join(3);assert not thread.is_alive()
    assert late and late[0]['ok'] is False
    assert {p:io._load_json(p,None) for p in before}==before
    assert len(branches.tree('original')['branches'])==(2 if child else 1)
    if child:assert io._load_branch_messages('original','main')==parent
    assert not jobs


def test_rewind_to_pending_turn_preserves_that_turn_record(world,recording):
    _,client,_=world;entered,release,late,jobs=recording
    run_card_turn('original','main','原始問題',llm_fn=lambda *a:'舊回覆')
    assert entered.wait(2)
    response=client.post('/api/card/original/rewind',json={'to_index':2})
    assert response.status_code==200
    release.set()
    for thread in threading.enumerate():
        if thread.name.startswith('card-recorder-'):thread.join(3)
    assert io._load_json(io._story_npcs_path('original','main'),[])[0]['memory']==['被撤銷的秘密','遲到的記憶']
    assert io._load_branch_messages('original','main')[-1]['card_record_status']=='done'


def test_regen_stream_cancelled_before_worker_cannot_repopulate_reset_branch(world,recording):
    _,client,_=world;single_turn();bid=fork(client,index=2)
    app=client.application
    with app.test_request_context('/api/card/original/regenerate',method='POST',json={'branch_id':bid,'stream':True}):
        response=app.full_dispatch_request()
        statuses=[]
        def reset():
            with app.test_client() as other:statuses.append(other.post('/api/card/original/reset',json={'branch_id':bid}).status_code)
        thread=threading.Thread(target=reset);thread.start();thread.join(2)
        assert statuses==[200]
        text=''.join(x.decode() if isinstance(x,bytes) else x for x in response.response)
        assert '"type": "error"' in text and '取消' in text
        assert len(io._load_branch_messages('original',bid))==1
        assert len(branches.tree('original')['branches'])==2


def test_record_provider_failure_does_not_commit_partial_tool_results(world,monkeypatch):
    def fail(**kwargs):
        kwargs['on_tool']('remember',{'facts':['不能留下的半成品']})
        return '',[], 'error'
    monkeypatch.setattr(gemini_bridge,'call_gemini_gm_tools',fail)
    monkeypatch.setattr(llm_bridge,'_get_config',lambda:{})
    run_card_turn('original','main','你好',llm_fn=lambda *a:'回覆',async_recorder=False)
    assert io._load_branch_messages('original','main')[-1]['card_record_status']=='error'
    assert io._load_json(io._story_npcs_path('original','main'),[])[0]['memory']==[]


def test_next_turn_waits_for_running_recorder_instead_of_busy(world, recording):
    """A reply sent while the previous turn is still recording waits for it, then runs."""
    entered, release, late, jobs = recording
    run_card_turn('original', 'main', '原始問題', llm_fn=lambda *a: '舊回覆')
    assert entered.wait(2)
    result = {}
    worker = threading.Thread(target=lambda: result.update(run_card_turn(
        'original', 'main', '下一句', llm_fn=lambda *a: '下一回覆', async_recorder=False)))
    worker.start()
    worker.join(0.3)
    assert worker.is_alive()  # waiting on the first recorder, not failing with Busy
    release.set()
    worker.join(5)
    assert result.get('ok'), result
    memory = io._load_json(io._story_npcs_path('original', 'main'), [])[0]['memory']
    assert '被撤銷的秘密' in memory and '新版記憶' in memory


def test_cancelled_generation_recorder_is_not_waited_for(monkeypatch):
    from story_core.card_mode import pipeline
    token = {'id': 'g1'}
    monkeypatch.setattr(pipeline.branches, 'generation', lambda slug, bid: token['id'])
    stop = threading.Event()
    thread = threading.Thread(target=lambda: stop.wait(5))
    monkeypatch.setitem(pipeline._recorders, ('s', 'b'), {thread: 'g1'})
    thread.start()
    token['id'] = 'g2'  # regenerate/rewind/reset invalidated the generation
    pipeline._wait_for_recorders('s', 'b', timeout=5)
    assert thread.is_alive()
    stop.set(); thread.join(1)


def test_player_stop_aborts_turn_without_writing(world):
    from story_core.card_mode import generation_control
    _, client, _ = world
    before = copy.deepcopy(io._load_branch_messages('original', 'main'))
    started, out = threading.Event(), {}
    def slow_provider(*a):
        started.set(); threading.Event().wait(2); return '不該寫入'
    def turn():
        try: run_card_turn('original', 'main', '想取消的句子', llm_fn=slow_provider, async_recorder=False)
        except Exception as exc: out['error'] = exc
    worker = threading.Thread(target=turn); worker.start()
    assert started.wait(2)
    response = client.post('/api/card/original/cancel', json={'branch_id': 'main'})
    assert response.json == {'ok': True, 'cancelled': True}
    worker.join(2)
    assert not worker.is_alive()
    assert isinstance(out.get('error'), generation_control.Cancelled)
    assert '停止' in str(out['error'])
    assert io._load_branch_messages('original', 'main') == before
    assert client.post('/api/card/original/cancel', json={'branch_id': 'main'}).json['cancelled'] is False
