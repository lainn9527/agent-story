"""Production scheduling/streaming with event gates, never real model calls."""
import copy
from functools import partial
import json
import threading

import pytest
from story_core.card_mode import ensemble as e, ensemble_recorder as recorder
from tests.test_card_ensemble import world, make

REAL_STREAM = e.generate_stream


@pytest.fixture
def async_world(world, monkeypatch):
    monkeypatch.setattr(e, 'mutate', partial(e.mutate, async_recorder=True))
    monkeypatch.setattr(e, 'generate_stream', REAL_STREAM)
    monkeypatch.setattr(e, 'record', lambda *args: [])
    releases = []
    yield world, releases
    for event in releases: event.set()
    # No background thread may escape the temporary filesystem/provider fixture.
    for _ in range(3):
        with recorder._guard: workers = list(recorder._workers.values())
        for worker in workers: worker.join(5)
    with recorder._guard: assert not recorder._workers


def blocker(async_world):
    entered, release = threading.Event(), threading.Event()
    async_world[1].append(release)
    return entered, release


def wait_workers():
    with recorder._guard: workers = list(recorder._workers.values())
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()


def update(fact='週五北門見面', evidence='週五北門見面'):
    return [{'character_id':'lead', 'memories':[{'fact':fact,'evidence':evidence}]}]


def test_http_first_chunk_precedes_generation_finish_and_done_precedes_recording(async_world, monkeypatch):
    from story_core import llm_bridge
    _, client, _ = make(async_world[0])
    generating, finish_gm = blocker(async_world)
    recording, finish_record = blocker(async_world)
    calls = []
    def stream(*args, **kwargs):
        calls.append('gm')
        yield 'text', '小蘭：「'
        generating.set()
        assert finish_gm.wait(5)
        yield 'text', '週五北門見面。」'
        yield 'done', {}
    def record(*args):
        recording.set()
        assert finish_record.wait(5)
        return update()
    monkeypatch.setattr(llm_bridge, 'call_claude_gm_stream', stream)
    monkeypatch.setattr(e, 'record', record)
    response = client.post('/api/card/cast_test/send/stream', json={'message':'週五呢？'}, buffered=False)
    assert response.status_code == 200
    first = next(response.response).decode()
    assert '小蘭' in first and 'done' not in first
    assert generating.wait(1) and not finish_gm.is_set()
    assert len(e.load('cast_test')['messages']) == 1
    finish_gm.set()
    rest = b''.join(response.response).decode()
    assert '"type": "done"' in rest and recording.wait(1)
    assert not finish_record.is_set() and calls == ['gm']
    assert len(e.load('cast_test')['messages']) == 3
    assert client.get('/api/card/cast_test/state').json['recorder']['status'] == 'pending'
    finish_record.set(); wait_workers()
    assert e.public_state('cast_test')['recorder']['status'] == 'done'
    assert '週五北門見面' in e.load('cast_test')['state']['characters']['lead']['memory']


def test_next_turn_sees_pending_prose_and_recording_rebases_snapshots_and_controls(async_world):
    _, _, sid = make(async_world[0])
    entered, release = blocker(async_world)
    def record(system, evidence):
        if '第一回合' in evidence:
            entered.set(); assert release.wait(5)
            return update()
        assert '週五北門見面' in system
        return []
    e.mutate('cast_test','send',{'message':'第一回合'},lambda *a:'週五北門見面',record)
    assert entered.wait(1)
    e.mutate('cast_test','focus',{'character_id':sid,'presence':'present'},record_fn=record)
    def second(user, system, recent):
        assert any('週五北門見面' in m['content'] for m in recent)
        assert '原文' in system
        return '📅 2026/09/22　⏰ 10:30　📌 北門　🌤️ 晴\n沈晚點頭。'
    e.mutate('cast_test','send',{'message':'第二回合'},second,record)
    third = e.mutate('cast_test','add',{'name':'陳安','personality':'沉穩'},record_fn=record)['character_id']
    release.set(); wait_workers()
    doc = e.load('cast_test')
    assert all(m.get('ensemble_record_status')=='done' for m in doc['messages'] if 'ensemble_turn_id' in m)
    assert '週五北門見面' in doc['messages'][-1]['ensemble_pre_snapshot']['characters']['lead']['memory']
    assert doc['state']['focus']==sid and doc['state']['characters'][sid]['presence']=='present'
    assert third in doc['state']['characters'] and doc['state']['location']=='北門'
    e.mutate('cast_test','regenerate',{},lambda *a:'第二版',lambda *a:[])
    wait_workers()
    doc=e.load('cast_test')
    assert '週五北門見面' in doc['state']['characters']['lead']['memory']
    assert third in doc['state']['characters'] and doc['state']['focus']==sid


@pytest.mark.parametrize('action', ['regenerate','rewind','reset'])
def test_late_recording_cannot_resurrect_removed_turn_even_when_index_reused(async_world, action):
    _, _, _ = make(async_world[0])
    entered, release = blocker(async_world)
    def record(system, evidence):
        if '舊秘密' in evidence:
            entered.set(); assert release.wait(5)
            return update('舊秘密','舊秘密')
        return update('新的約定','新的約定')
    e.mutate('cast_test','send',{'message':'你好'},lambda *a:'舊秘密',record)
    assert entered.wait(1)
    old = e.load('cast_test')['messages'][-1]['ensemble_turn_id']
    if action == 'regenerate':
        e.mutate('cast_test', action, {}, lambda *a:'新的約定',record)
    else:
        e.mutate('cast_test', action, {'to_index':0}, record_fn=record)
        e.mutate('cast_test','send',{'message':'再來'},lambda *a:'新的約定',record)
    new = e.load('cast_test')['messages'][-1]
    assert new['index']==2 and new['ensemble_turn_id']!=old
    release.set(); wait_workers()
    doc=e.load('cast_test')
    assert '舊秘密' not in json.dumps(doc,ensure_ascii=False)
    assert '新的約定' in doc['state']['characters']['lead']['memory']


def test_failed_recorder_retains_dialogue_and_retries_in_order_without_regeneration(async_world):
    _,client,_=make(async_world[0]); calls=[]
    def fail(*args): raise ValueError('bad json')
    e.mutate('cast_test','send',{'message':'第一回合'},lambda *a:'週五北門見面',fail)
    wait_workers()
    first=copy.deepcopy(e.load('cast_test')['messages'][-1])
    def record(system,evidence):
        calls.append(evidence)
        if '第一回合' in evidence: return update()
        assert '週五北門見面' in system
        return []
    e.mutate('cast_test','send',{'message':'第二回合'},lambda *a:'好啊',record)
    wait_workers()
    assert not calls
    state=client.get('/api/card/cast_test/state').json
    assert state['recorder']=={'status':'error','failed':1,'pending':1}
    e.mutate('cast_test','retry-recording',{},record_fn=record)
    wait_workers()
    doc=e.load('cast_test')
    assert len(calls)==2 and doc['messages'][2]['content']==first['content']
    assert doc['messages'][2]['ensemble_turn_id']==first['ensemble_turn_id']
    assert doc['messages'][4]['ensemble_pre_snapshot']['characters']['lead']['memory'][-1]=='週五北門見面'
    assert e.public_state('cast_test')['recorder']['status']=='done'


def test_read_state_resumes_durable_pending_job_after_restart(async_world, monkeypatch):
    _,client,_=make(async_world[0]);doc=e.load('cast_test')
    candidate,_=e.turn('cast_test',doc,'記一下',generate_fn=lambda *a:'週五北門見面')
    e.save('cast_test',candidate)  # Simulated process exit after saving, before spawning.
    monkeypatch.setattr(e,'record',lambda *a:update())
    assert client.get('/api/card/cast_test/state').status_code==200
    wait_workers()
    assert e.public_state('cast_test')['recorder']['status']=='done'


def test_partial_stream_failure_preserves_old_regen_turn(async_world, monkeypatch):
    from story_core import llm_bridge
    _,client,_=make(async_world[0])
    e.mutate('cast_test','send',{'message':'原始輸入'},lambda *a:'原始完整回應',lambda *a:[])
    wait_workers(); before=e.path('cast_test').read_bytes()
    def broken(*args,**kwargs):
        yield 'text','只有半句'
        yield 'error','斷線'
    monkeypatch.setattr(llm_bridge,'call_claude_gm_stream',broken)
    response=client.post('/api/card/cast_test/regenerate',json={'stream':True})
    assert '只有半句' in response.text and '"type": "error"' in response.text
    assert '"type": "done"' not in response.text
    assert e.path('cast_test').read_bytes()==before


def test_completed_record_is_retained_when_rewinding_to_its_pending_snapshot(async_world):
    _,_,_=make(async_world[0]);entered,release=blocker(async_world)
    def record(*args):
        entered.set(); assert release.wait(5); return update()
    e.mutate('cast_test','send',{'message':'好'},lambda *a:'週五北門見面',record)
    assert entered.wait(1)
    e.mutate('cast_test','send',{'message':'繼續'},lambda *a:'第二回合',record)
    e.mutate('cast_test','rewind',{'to_index':2},record_fn=record)
    release.set(); wait_workers()
    doc=e.load('cast_test')
    assert len(doc['messages'])==3 and '週五北門見面' in doc['state']['characters']['lead']['memory']


def test_vertex_partial_transport_failure_is_not_a_completed_turn(monkeypatch):
    from types import SimpleNamespace
    from story_core import vertex_bridge, llm_bridge
    def broken(**kwargs):
        yield SimpleNamespace(text='只收到半句', usage_metadata=None, candidates=[])
        raise ConnectionError('disconnected')
    client=SimpleNamespace(models=SimpleNamespace(generate_content_stream=broken))
    monkeypatch.setattr(vertex_bridge,'_get_client',lambda *a:client)
    monkeypatch.setattr(vertex_bridge,'_build_config',lambda *a,**k:None)
    def stream(*args,**kwargs):
        yield from vertex_bridge.call_vertex_gm_stream(*args,vertex_cfg={})
    monkeypatch.setattr(llm_bridge,'call_claude_gm_stream',stream)
    chunks=[]
    with pytest.raises(ValueError,match='中斷'):
        e.generate_stream('你好','prompt',[],chunks.append)
    assert chunks==['只收到半句']


def test_worker_start_failure_keeps_successful_narration_and_can_resume(async_world, monkeypatch):
    _,_,_=make(async_world[0])
    with monkeypatch.context() as patch:
        def fail_start(self): raise RuntimeError('no thread available')
        patch.setattr(threading.Thread,'start',fail_start)
        result=e.mutate('cast_test','send',{'message':'好'},lambda *a:'週五北門見面',lambda *a:update())
        assert result['ok'] and e.load('cast_test')['messages'][-1]['content']=='週五北門見面'
        assert not recorder._workers
    recorder.start('cast_test',lambda *a:update());wait_workers()
    assert e.public_state('cast_test')['recorder']['status']=='done'
