"""Galgame rules and real save lifecycle, with no paid model calls."""
import copy
import json
from pathlib import Path
import pytest
from tests.test_card_ensemble import world
from tests.test_card_branches import no_live_compaction, fork
from story_core import story_io as io
from story_core.card_mode import galgame as g, pipeline
from story_core.card_mode.card import init_card_story, validate_card

RUN_CARD_TURN = pipeline.run_card_turn


def event(action='care', mode='act', evidence='幫忙整理書架', consent_quote=''):
    return dict(action=action, mode=mode, evidence=evidence, consent_quote=consent_quote)


def step(state, action='care', day='2026-10-09', elapsed=0, **kw):
    return g.settle(state, event(action, **kw), day, elapsed)


def test_growth_budget_cooldown_repetition_and_new_day():
    state = g.initial()
    for i in range(100):
        state = step(state, evidence=f'善意{i}')
    assert (state['affinity'], state['trust'], state['daily_gain']) == (3, 3, 3)
    state = step(state, day='2026-10-10', evidence='新的善意')
    assert state['affinity'] == 4
    for i in range(10):
        state = step(state, day='2026-10-10', evidence='新的善意')
    assert state['affinity'] == 4
    # Moving clock backwards does not reopen an earlier daily budget.
    assert step(state, day='2026-10-09', evidence='另一件事')['daily_gain'] == 2


def test_positive_cooldown_is_three_turns():
    state = g.initial()
    values=[]
    for i in range(7):
        state=step(state,evidence=str(i)); values.append(state['affinity'])
    assert values == [1,1,1,2,2,2,3]


@pytest.mark.parametrize('action', list(g.BOUNDARIES))
def test_premature_action_penalty_but_polite_request_declined(action):
    before=g.initial()
    ask=step(before, action, mode='ask')
    act=step(before, action)
    assert ask['resentment'] == 0 and ask['last_event']['outcome']=='decline'
    assert act['resentment'] > 0 and act['last_event']['penalty'] > 0
    assert before==g.initial()


def test_threshold_and_current_consent_are_both_required():
    before={**g.initial(),'affinity':44,'trust':30}
    assert step(before,'hold_hands',consent_quote='可以牽手')['last_event']['outcome']=='boundary_violation'
    before['affinity']=45
    assert step(before,'hold_hands')['last_event']['outcome']=='boundary_violation'
    ok=step(before,'hold_hands',consent_quote='可以牽手')
    assert ok['last_event']['outcome']=='eligible' and ok['affinity']==45
    before['resentment']=15
    assert step(before,'hold_hands',consent_quote='可以牽手')['last_event']['outcome']=='boundary_violation'


def test_dislike_repeat_offence_and_slow_repair():
    state=step(g.initial(),'pressure')
    assert state['resentment']==20 and g.stage(state)=='戒備'
    state=step(state,'pressure')
    assert state['resentment']==45 and g.stage(state)=='厭惡'
    assert step(state,'apology')['resentment']==45
    repair=step(state,'respect')
    assert repair['resentment']==44 and repair['affinity']==0
    assert step(repair,'respect')['resentment']==44


def test_desire_is_independent_and_decay_accumulates_minutes():
    state={**g.initial(),'affinity':50,'trust':50,'desire':10}
    for minute in range(5,66,5):
        state=step(state,'neutral',elapsed=minute)
    assert state['desire']==9 and state['affinity']==50
    assert step(state,'pressure')['desire']==0


def test_evidence_must_be_current_user_and_consent_recent_character():
    with pytest.raises(ValueError): g.validate_assessment(event(evidence='虛構'), '你好', [])
    with pytest.raises(ValueError): g.validate_assessment(event('hug', evidence='抱你',consent_quote='可以'), '抱你', [{'role':'user','content':'可以'}])
    with pytest.raises(ValueError): g.validate_assessment(event('hug', evidence='抱你',consent_quote='可以'), '抱你', [{'role':'gm','content':'可以'},{'role':'gm','content':'不要'}])
    assert g.validate_assessment(event('hug', evidence='抱你',consent_quote='可以'), '抱你', [{'role':'gm','content':'可以'}])['action']=='hug'


@pytest.fixture
def game(world, monkeypatch):
    root, client, _ = world
    card=json.loads(Path('story_design/linwan_galgame/card.json').read_text())
    assert validate_card(card)==[]
    init_card_story(card)
    monkeypatch.setattr(g,'assess',lambda sid,bid,user,recent: event('pressure' if user=='糾纏' else 'care', evidence=user))
    monkeypatch.setattr(pipeline,'_generate_pass1',lambda **kw: ('林晚：「先慢慢認識吧。」','test'))
    run=pipeline.run_card_turn
    def isolated(*a,**kw):
        kw.update(recorder_fn=lambda *a:None,async_recorder=False)
        return run(*a,**kw)
    monkeypatch.setattr('routes.card_routes.run_card_turn',isolated)
    monkeypatch.setattr(pipeline,'run_card_turn',isolated)
    return root,client,card


def send(client,text='幫忙整理書架',branch='main'):
    result=client.post('/api/card/linwan_galgame/send/stream?sync=1',json={'message':text,'branch_id':branch})
    assert result.status_code==200,result.text
    assert '"ok": true' in result.text or '"ok":true' in result.text,result.text


def state(client,branch='main'):
    return client.get('/api/card/linwan_galgame/state?branch_id='+branch).json['galgame']


def test_api_prompt_snapshot_fork_rewind_reset(game,monkeypatch):
    root,client,card=game
    prompts=[]
    monkeypatch.setattr(pipeline,'_generate_pass1',lambda **kw: (prompts.append(kw['system_prompt']) or '林晚：「請不要糾纏。」','test'))
    send(client,'糾纏')
    assert state(client)['resentment']==20
    assert 'boundary_violation' in prompts[-1] and '無限制角色扮演' not in prompts[-1]
    rows=io._load_branch_messages(card['id'],'main')
    assert rows[-1]['pre_state_snapshot']['galgame']['resentment']==0
    assert rows[-1]['state_snapshot']['galgame']['resentment']==20
    child=fork(client,slug=card['id'],index=2)
    send(client,'糾纏',branch=child)
    assert state(client,child)['resentment']==45
    assert state(client)['resentment']==20
    result=client.post('/api/card/linwan_galgame/rewind',json={'branch_id':child,'message_index':2})
    assert result.status_code==200,result.json
    assert state(client,child)['resentment']==20
    assert client.post('/api/card/linwan_galgame/reset',json={'branch_id':child}).status_code==200
    assert state(client,child)['resentment']==0
    assert state(client)['resentment']==20


def test_regenerate_does_not_double_award(game):
    _,client,card=game
    send(client)
    assert state(client)['affinity']==1
    result=client.post('/api/card/linwan_galgame/regenerate',json={'branch_id':'main','stream':True})
    assert result.status_code==200,result.text
    assert state(client)['affinity']==1 and state(client)['turns']==1


def test_failed_generation_or_assessment_never_settles(game,monkeypatch):
    _,client,card=game
    before=state(client)
    monkeypatch.setattr(pipeline,'_generate_pass1',lambda **kw: ('','failed'))
    pipeline.run_card_turn(card['id'],user_message='糾纏')
    assert state(client)==before
    def fail(*a): raise ValueError('互動判定未完成')
    monkeypatch.setattr(g,'assess',fail)
    result=pipeline.run_card_turn(card['id'],user_message='糾纏')
    assert not result['ok'] and state(client)==before
    assert len(io._load_branch_messages(card['id'],'main'))==1


def test_recorder_cannot_override_or_reaward(game,monkeypatch):
    _,client,card=game
    # Exercise production recorder callback, not the no-op fixture.
    run=RUN_CARD_TURN
    def tools(**kw):
        assert not any(t['name']=='set_relationship' for t in kw['tool_decls'])
        assert not kw['on_tool']('set_relationship',{'tier':'摯友'})['ok']
        kw['on_tool']('remember',{'facts':['玩家幫忙整理書架。']})
        return '',[],'ok'
    monkeypatch.setattr('story_core.llm_bridge.get_provider',lambda:'vertex')
    monkeypatch.setattr('story_core.llm_bridge.call_gm_tools',tools)
    monkeypatch.setattr('story_core.llm_bridge._get_config',lambda:{})
    monkeypatch.setattr('story_core.llm_bridge.get_recorder_model',lambda:None)
    result=run(card['id'],user_message='幫忙整理書架',async_recorder=False)
    assert result['ok']
    assert state(client)['affinity']==1
    msg=io._load_branch_messages(card['id'],'main')[-1]
    assert msg['card_record_status']=='done'
    pipeline.record_saved_turn(card['id'],'main',msg,'幫忙整理書架',async_recorder=False)
    assert state(client)['affinity']==1


def test_classifier_structured_output_retry_and_server_evidence(world,monkeypatch):
    from story_core import llm_bridge
    monkeypatch.setattr(llm_bridge,'get_recorder_model',lambda:None)
    calls=[]
    responses=iter(['普通聊天',json.dumps({'action':'neutral','mode':'ask','consent_quote':''})])
    def once(prompt,**kw):
        calls.append(kw)
        assert kw['response_schema']==g.ASSESSMENT_SCHEMA
        return next(responses)
    monkeypatch.setattr(llm_bridge,'call_oneshot',once)
    result=g.assess('original','main','你好',[])
    assert result==event('neutral','ask','你好') and len(calls)==2
    assert '上一份結果未通過檢查' in calls[1]['system_prompt']


@pytest.mark.parametrize('raw', ['{}','null','[]','',
    '{"action":"arbitrary","mode":"act","consent_quote":""}',
    '{"action":"hug","mode":"act","consent_quote":"捏造同意"}'])
def test_classifier_invalid_structured_output_never_bypasses_validation(world,monkeypatch,raw):
    from story_core import llm_bridge
    calls=[]
    monkeypatch.setattr(llm_bridge,'get_recorder_model',lambda:None)
    monkeypatch.setattr(llm_bridge,'call_oneshot',lambda *a,**kw: calls.append(1) or raw)
    with pytest.raises(ValueError,match='未結算'): g.assess('original','main','抱你',[])
    assert len(calls)==2


def test_schema_and_existing_card_opt_out(game):
    _,client,card=game
    assert client.get('/api/card/original/state').json['galgame'] is None
    bad=copy.deepcopy(card);bad['persona']['age']=17
    assert validate_card(bad)
    bad=copy.deepcopy(card);bad['galgame']='yes'
    assert validate_card(bad)


def test_edit_replaces_assessment_and_candidates_restore_it(game):
    from tests.test_card_editing import body
    _,client,card=game
    send(client,'糾纏')
    messages=io._load_branch_messages(card['id'],'main')
    response=client.post('/api/card/linwan_galgame/edit',json=body(messages))
    assert response.status_code==200,response.json
    assert state(client)['resentment']==0 and state(client)['affinity']==1
    assert client.post('/api/card/linwan_galgame/regenerate',json={'branch_id':'main','stream':True}).status_code==200
    data=client.get('/api/card/linwan_galgame/candidates?branch_id=main&message_index=2').json
    response=client.post('/api/card/linwan_galgame/candidates',json={
        'action':'select','branch_id':'main','group_id':data['group_id'],
        'version_id':data['versions'][0]['id'],'expected_version_id':data['selected_id'],'message_index':2})
    assert response.status_code==200,response.json
    assert state(client)['affinity']==1 and state(client)['turns']==1


def test_multi_character_conversion_is_rejected_without_copy(game):
    root,client,card=game
    result=client.post('/api/card/linwan_galgame/ensemble',json={
        'target_slug':'forbidden_copy','character':{'name':'另一人'}})
    assert result.status_code==400 and '單女角' in result.json['error']
    assert not (root/'stories/forbidden_copy').exists()


def test_failed_recorder_preserves_committed_score_and_retry_uses_same_settlement(game,monkeypatch):
    _,client,card=game
    monkeypatch.setattr('story_core.llm_bridge.get_provider',lambda:'vertex')
    monkeypatch.setattr('story_core.llm_bridge._get_config',lambda:{})
    monkeypatch.setattr('story_core.llm_bridge.get_recorder_model',lambda:None)
    monkeypatch.setattr('story_core.llm_bridge.call_gm_tools',lambda **kw: ('',[],'error'))
    result=RUN_CARD_TURN(card['id'],user_message='幫忙整理書架',async_recorder=False)
    assert result['ok'] and state(client)['affinity']==1
    msg=io._load_branch_messages(card['id'],'main')[-1]
    assert msg['card_record_status']=='error'
    msg['card_record_status']='pending'
    io._upsert_branch_message(card['id'],'main',msg)
    monkeypatch.setattr('story_core.llm_bridge.call_gm_tools',lambda **kw: ('',[],'ok'))
    pipeline.record_saved_turn(card['id'],'main',msg,'幫忙整理書架',async_recorder=False)
    assert state(client)['affinity']==1 and state(client)['turns']==1
    assert io._load_branch_messages(card['id'],'main')[-1]['card_record_status']=='done'


def test_galgame_verbal_tics_are_optional():
    card=json.loads(Path('story_design/linwan_galgame/card.json').read_text())
    assert 'verbal_tics' not in card['persona']
    assert validate_card(card)==[]
    card['persona']['verbal_tics']=[]
    assert validate_card(card)==[]
    card['persona']['verbal_tics']='固定句'
    assert validate_card(card)
