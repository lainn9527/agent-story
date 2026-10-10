import copy
import json
from pathlib import Path
import pytest
from flask import Flask
from story_core.card_mode import ensemble as e
from story_core.card_mode.card import init_card_story


@pytest.fixture
def world(tmp_path, monkeypatch):
    from story_core import story_io as io, usage_db
    monkeypatch.setattr(usage_db, 'STORIES_DIR', str(tmp_path/'stories'))
    monkeypatch.setattr(usage_db, '_initialized', set())
    from functools import partial
    monkeypatch.setattr(e, 'mutate', partial(e.mutate, async_recorder=False))
    def stream(user, system, recent, emit):
        text = e.generate(user, system, recent); emit(text); return text
    monkeypatch.setattr(e, 'generate_stream', stream)
    from story_core.card_mode import card
    from routes.card_routes import card_bp
    monkeypatch.setattr(io,'STORIES_DIR',str(tmp_path/'stories'))
    monkeypatch.setattr(io,'STORIES_REGISTRY_PATH',str(tmp_path/'stories.json'))
    monkeypatch.setattr(card,'STORY_DESIGN_DIR',str(tmp_path/'design'))
    (tmp_path/'stories.json').write_text('{"stories":{}}')
    c={'schema_version':1,'id':'original','display_name':'李小蘭','pitch':'成年大學同學，一起籌備攝影展。',
       'player':{'role':'艾迪，20 歲大學生','address':'你'},
       'setting':{'place':'大學咖啡館','start_datetime':'2026-09-22T10:00:00','weather_pool':['晴']},
       'persona':{'identity':'20 歲大學生','personality':'親切自律，做事可靠，喜歡輕巧的玩笑。',
         'speech_style':'清楚溫和，不替別人決定。','body_details':['黑色長直髮'],'verbal_tics':['我想想。']},
       'opening':'📅 2026/09/22　⏰ 10:00　📌 大學咖啡館　🌤️ 晴\n小蘭正在準備攝影展講義。',
       'start':{'relationship':{'tier':'友好','note':'同學'},'inventory':{'相機':'艾迪保管'}},
       'output':{'length_hint':'200-400'}}
    init_card_story(c)
    app=Flask(__name__,root_path=str(Path.cwd()),template_folder=str(Path.cwd()/'templates'));app.register_blueprint(card_bp)
    app.testing=True
    spec={'name':'沈晚','identity':'20 歲大學生','personality':'直率愛吐槽，熱心攝影，生活有自己的安排。',
          'speech_style':'輕快直白','memories':['沈晚答應週五帶藍色相簿到攝影展。'],
          'tier':'相識','presence':'offstage','status':'在圖書館整理照片'}
    return tmp_path,app.test_client(),spec


def make(world):
    root,client,spec=world
    result=client.post('/api/card/original/ensemble',json={'target_slug':'cast_test','character':spec})
    assert result.status_code==200,result.json
    d=e.load('cast_test');sid=next(k for k in d['state']['characters'] if k!='lead')
    return root,client,sid


def fake_record(system,text):
    return []


def test_copy_isolated_and_legacy_untouched(world,monkeypatch):
    root,client,spec=world
    before={str(p):p.read_bytes() for p in (root/'stories/original').rglob('*') if p.is_file()}
    _,client,sid=make(world)
    assert client.get('/api/card/cast_test/state').json['ensemble']
    assert not client.get('/api/card/original/state').json.get('ensemble')
    assert client.get('/card/cast_test').status_code==200
    assert not (root/'stories/cast_test/branches/main/messages.json').exists()
    monkeypatch.setattr(e,'generate',lambda *a:'李小蘭：「好，攝影展見。」')
    monkeypatch.setattr(e,'record',fake_record)
    res=client.post('/api/card/cast_test/send/stream',json={'message':'好，攝影展見。'})
    assert res.status_code==200 and 'text/event-stream' in res.content_type
    assert '"type": "done"' in res.text
    assert before=={str(p):p.read_bytes() for p in (root/'stories/original').rglob('*') if p.is_file()}
    # Independent cast definitions too.
    e.mutate('cast_test','focus',{'character_id':sid,'presence':'present'})
    assert json.loads((root/'design/original/card.json').read_text())['display_name']=='李小蘭'


def test_three_turns_focus_and_knowledge(world):
    _,client,sid=make(world); captured=[]
    def gm(user,system,recent):
        captured.append(system)
        return '李小蘭說：「我明天要面試，先不要告訴沈晚。」' if len(captured)==1 else '沈晚說：「週五我帶藍色相簿。」'
    def recorder(system,text):
        if len(captured)==1:
            return [{'character_id':'lead','memories':[{'fact':'小蘭明天要面試，尚未告知沈晚。','evidence':'我明天要面試'}]},
                    {'character_id':sid,'memories':[{'fact':'沈晚知道面試。','evidence':'我明天要面試'}]}]
        return [{'character_id':sid,'memories':[{'fact':'沈晚週五會帶藍色相簿。','evidence':'週五我帶藍色相簿'}],
                 'relationship':{'tier':'友好','note':'一起準備攝影展','evidence':'週五我帶藍色相簿'}}]
    e.mutate('cast_test','send',{'message':'有什麼近況？'},gm,recorder)
    s=e.load('cast_test')['state'];assert '面試' in s['characters']['lead']['memory'][-1]
    assert not any('面試' in v for v in s['characters'][sid]['memory'])
    e.mutate('cast_test','focus',{'character_id':'lead','presence':'offstage'})
    e.mutate('cast_test','focus',{'character_id':sid,'presence':'present'})
    e.mutate('cast_test','send',{'message':'週五準備如何？'},gm,recorder)
    e.mutate('cast_test','focus',{'character_id':'lead','presence':'present'})
    e.mutate('cast_test','send',{'message':'一起討論展覽。'},gm,recorder)
    for p in captured:
        assert '李小蘭' in p and '沈晚' in p and 'personality' in p and 'presence' in p
    assert '面試' in captured[1] and '不是共同知識' in captured[1]
    s=e.load('cast_test')['state'];assert s['characters'][sid]['relationship']['tier']=='友好'
    assert s['characters']['lead']['relationship']['tier']=='友好'


def test_rollback_regen_reset_and_failure_atomicity(world):
    _,client,sid=make(world)
    gm=lambda *a:'📅 2026/09/22　⏰ 10:05　📌 圖書館　🌤️ 晴\n沈晚：「我帶相簿。」'
    recorder=lambda *a:[{'character_id':sid,'memories':[{'fact':'沈晚帶相簿。','evidence':'我帶相簿'}]}]
    e.mutate('cast_test','focus',{'character_id':sid,'presence':'present'})
    e.mutate('cast_test','send',{'message':'帶什麼？'},gm,recorder)
    first=copy.deepcopy(e.load('cast_test')['state'])
    e.mutate('cast_test','focus',{'character_id':'lead','presence':'remote'})
    e.mutate('cast_test','send',{'message':'你好'},gm,recorder)
    e.mutate('cast_test','rewind',{'to_index':2})
    assert e.load('cast_test')['state']==first
    e.mutate('cast_test','regenerate',{},gm,lambda *a:[])
    state=e.load('cast_test')['state']
    assert state['focus']==sid and '沈晚帶相簿。' not in state['characters'][sid]['memory']
    before=e.path('cast_test').read_bytes()
    def fail(*a):raise ValueError('recorder failed')
    e.mutate('cast_test','send',{'message':'hello'},gm,fail)
    assert e.public_state('cast_test')['recorder']['status']=='error'
    assert e.load('cast_test')['messages'][-1]['content']==gm()
    before=e.path('cast_test').read_bytes()
    with pytest.raises(ValueError):e.mutate('cast_test','regenerate',{},fail,recorder)
    assert before==e.path('cast_test').read_bytes()
    e.mutate('cast_test','reset',{})
    doc=e.load('cast_test');assert doc['state']==doc['initial_state'] and len(doc['messages'])==1


def test_concurrent_switch_rejected_and_input_validation(world):
    _,client,sid=make(world)
    with e.lock('cast_test'):
        res=client.post('/api/card/cast_test/focus',json={'character_id':sid})
        assert res.status_code==409
        assert client.post('/api/card/cast_test/send/stream',json={'message':'hi'}).status_code==409
    assert client.post('/api/card/cast_test/focus',json={'character_id':'missing'}).status_code==400
    assert client.post('/api/card/cast_test/add',json={'name':'李小蘭','personality':'x'}).status_code==400
    assert client.post('/api/card/cast_test/send/stream',json={'message':'hi','response_length':'bad'}).status_code==400
    assert client.post('/api/card/original/ensemble',json={'target_slug':'../escape','character':world[2]}).status_code==400
    assert client.post('/api/card/cast_test/rewind',json={'to_index':-1}).status_code==400


def test_evidence_owner_and_presence_gates(world):
    _,_,sid=make(world);state=e.load('cast_test')['state']
    e.apply_updates(state,[{'character_id':'lead','memories':[{'fact':'fake','evidence':'not present'}]}],'heard')
    assert 'fake' not in state['characters']['lead']['memory']
    with pytest.raises(ValueError):e.apply_updates(state,[{'character_id':'unknown'}],'heard')


@pytest.mark.skipif(__import__('os').environ.get('CARD_ENSEMBLE_LIVE')!='1', reason='Opt-in paid model test')
def test_live_focus_handoff(world):
    """Legacy explicit stage directions, NOT natural-play quality validation."""
    root,client,sid=make(world)
    report=[]
    out=Path('/tmp/card-ensemble-live-report.json')
    def play(text):
        result=e.mutate('cast_test','send',{'message':text,'response_length':'short'})
        doc=e.load('cast_test')
        report.append({'input':text,'response':result['narration'],'state':copy.deepcopy(doc['state'])})
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        return result['narration']
    play('我對小蘭說：「我的攝影展入選了，但先別告訴沈晚，我想自己跟她說。」小蘭聽清楚並答應保密。沈晚不在場。')
    doc=e.load('cast_test'); assert any('入選' in x for x in doc['state']['characters']['lead']['memory'])
    assert not any('入選' in x for x in doc['state']['characters'][sid]['memory'])
    e.mutate('cast_test','focus',{'character_id':'lead','presence':'offstage'})
    e.mutate('cast_test','focus',{'character_id':sid,'presence':'present'})
    second=play('我遇見沈晚：「你週五說要帶的東西準備好了嗎？」小蘭已離場；沈晚還不知道攝影展的結果，不自行透露。')
    assert '相簿' in second
    e.mutate('cast_test','focus',{'character_id':'lead','presence':'present'})
    third=play('我們三人一起坐下。我當著兩人說：「攝影展入選了！週五麻煩沈晚帶藍色相簿，小蘭幫我核對展覽講義。」請讓兩人各自回應，各自確認自己的分工。')
    assert '小蘭' in third and '沈晚' in third
    s=e.load('cast_test')['state']; assert any('入選' in x for x in s['characters'][sid]['memory'])
    e.mutate('cast_test','focus',{'character_id':sid,'presence':'offstage'})
    e.mutate('cast_test','focus',{'character_id':'lead','presence':'present'})
    fourth=play('沈晚先走了。我問小蘭：「我們週五各自要準備什麼？你還記得我最早私下跟你說的事嗎？」只讓在場的小蘭回應，不替沈晚發言。')
    assert '相簿' in fourth and '講義' in fourth
    out=Path('/tmp/card-ensemble-live-report.json');out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('Live report:',out)


def test_absent_actor_survives_many_turns_and_return(world):
    _,_,sid=make(world)
    initial=copy.deepcopy(e.load('cast_test')['state']['characters'][sid])
    seen=[]
    def gm(user,system,recent):
        seen.append(system)
        return '李小蘭：「我會帶講義。」'
    for n in range(8):
        e.mutate('cast_test','send',{'message':f'討論第 {n+1} 張照片'},gm,lambda *a:[])
    assert e.load('cast_test')['state']['characters'][sid]==initial
    e.mutate('cast_test','focus',{'character_id':sid,'presence':'present'})
    e.mutate('cast_test','send',{'message':'回來討論週五安排'},gm,lambda *a:[])
    assert all('李小蘭' in p and '沈晚' in p for p in seen)
    assert all('藍色相簿' in p for p in seen[:8])
    assert '藍色相簿' in seen[8]
    assert len(seen)==9
    assert e.load('cast_test')['state']['characters'][sid]['persona']==initial['persona']
    e.mutate('cast_test','rewind',{'to_index':16})
    assert e.load('cast_test')['state']['characters'][sid]==initial


@pytest.mark.parametrize('stored,expected', [(None, '圖書館'), ('', '圖書館'), ('攝影教室', '攝影教室')])
def test_clone_recovers_scene_from_captured_history(world, stored, expected):
    root,client,_=world
    branch=root/'stories/original/branches/main'
    messages=json.loads((branch/'messages.json').read_text())
    messages.extend([{'role':'gm','index':1,'content':'📅 2026/09/22　⏰ 10:10　📌 圖書館　🌤️ 晴\n整理照片。'},
                     {'role':'gm','index':2,'content':'沒有新場景標頭。'}])
    (branch/'messages.json').write_text(json.dumps(messages,ensure_ascii=False))
    clock=json.loads((branch/'card_clock.json').read_text())
    clock.pop('last_location',None)
    if stored is not None:clock['last_location']=stored
    (branch/'card_clock.json').write_text(json.dumps(clock))
    before={p:p.read_bytes() for p in branch.iterdir() if p.is_file()}
    _,client,_=make(world)
    assert client.get('/api/card/cast_test/state').json['clock']['location']==expected
    assert before=={p:p.read_bytes() for p in before}


@pytest.mark.parametrize('failure', [None, 'invalid_json', 'transport', 'gm_invalid'])
def test_usage_each_call_and_failed_turns(world, monkeypatch, failure):
    import sqlite3
    from story_core import llm_bridge
    root,_,_=make(world)
    before=e.path('cast_test').read_bytes()
    metadata={'current':{'provider':'stale','total_tokens':999}}
    monkeypatch.setattr(llm_bridge,'get_last_usage',lambda:metadata['current'])
    def publish(tokens,cost):
        metadata['current']={'provider':'vertex','model':'test-model','prompt_tokens':tokens,
                             'output_tokens':10,'total_tokens':tokens+10,'cost_usd':cost}
    def gm(*args):
        publish(100,.01)
        return ('【系統錯誤】失敗' if failure=='gm_invalid' else '李小蘭：「好。」'),None
    def recorder(*args,**kwargs):
        if failure=='transport':raise RuntimeError('transport failed before usage published')
        publish(200,.02)
        return 'invalid json' if failure=='invalid_json' else '{"updates":[]}'
    monkeypatch.setattr(llm_bridge,'call_claude_gm',gm)
    monkeypatch.setattr(llm_bridge,'call_oneshot',recorder)
    if failure=='gm_invalid':
        with pytest.raises((ValueError,RuntimeError)):
            e.mutate('cast_test','send',{'message':'你好'})
        assert e.path('cast_test').read_bytes()==before
    else:
        e.mutate('cast_test','send',{'message':'你好'})
        assert e.public_state('cast_test')['recorder']['status']==('error' if failure else 'done')
    with sqlite3.connect(root/'stories/cast_test/usage.db') as db:
        rows=db.execute('SELECT call_type,total_tokens,cost_usd,story_id,branch_id FROM usage_log ORDER BY id').fetchall()
    expected=[('ensemble_gm',110,.01,'cast_test','main')]
    if failure not in ('transport','gm_invalid'):
        expected.append(('ensemble_recorder',210,.02,'cast_test','main'))
    assert rows==expected
    assert not (root/'stories/original/usage.db').exists()


def test_natural_arrival_and_departure_knowledge_windows(world):
    _,_,sid=make(world)
    state=e.load('cast_test')['state']
    evidence='玩家：小蘭，作品入選了。我離開咖啡館，走到沈晚桌邊。週五改在北門見。\n敘事：沈晚答應在北門見。'
    updates=[
        {'character_id':'lead','scene_events':[{'presence':'offstage','evidence':'我離開咖啡館'}],
         'memories':[{'fact':'作品入選了','evidence':'作品入選了'}, {'fact':'北門約定','evidence':'週五改在北門見'}]},
        {'character_id':sid,'scene_events':[{'presence':'present','evidence':'走到沈晚桌邊'}],
         'memories':[{'fact':'作品入選了','evidence':'作品入選了'}, {'fact':'北門約定','evidence':'週五改在北門見'}]}]
    e.apply_updates(state,updates,evidence)
    assert state['characters']['lead']['presence']=='offstage'
    assert state['characters'][sid]['presence']=='present'
    assert '作品入選了' in state['characters']['lead']['memory']
    assert '北門約定' not in state['characters']['lead']['memory']
    assert '作品入選了' not in state['characters'][sid]['memory']
    assert '北門約定' in state['characters'][sid]['memory']


def test_departed_actor_keeps_only_earlier_public_information(world):
    _,_,sid=make(world)
    state=e.load('cast_test')['state'];state['characters'][sid]['presence']='present'
    evidence='玩家：作品入選了。\n敘事：沈晚起身離開。\n玩家：我對小蘭說私下的事。'
    e.apply_updates(state,[{'character_id':sid,
        'scene_events':[{'presence':'offstage','evidence':'沈晚起身離開'}],
        'memories':[{'fact':'知道入選','evidence':'作品入選了'},
                    {'fact':'知道私事','evidence':'私下的事'}]}],evidence)
    assert state['characters'][sid]['presence']=='offstage'
    assert '知道入選' in state['characters'][sid]['memory']
    assert '知道私事' not in state['characters'][sid]['memory']


def test_unfounded_scene_event_does_not_commit_state(world):
    _,_,sid=make(world);before=copy.deepcopy(e.load('cast_test')['state'])
    e.mutate('cast_test','send',{'message':'你好'},lambda *a:'小蘭抬頭。',
             lambda *a:[{'character_id':sid,'scene_events':[{'presence':'present','evidence':'沈晚走到桌邊'}]}])
    assert e.load('cast_test')['state']==before
    assert e.public_state('cast_test')['recorder']['status']=='error'
    assert e.load('cast_test')['messages'][-1]['content']=='小蘭抬頭。'


def test_departure_does_not_leak_later_private_status(world):
    _,_,sid=make(world);state=e.load('cast_test')['state']
    state['characters'][sid]['presence']='present';before=state['characters'][sid]['current_status']
    e.apply_updates(state,[{'character_id':sid,
        'scene_events':[{'presence':'offstage','evidence':'沈晚起身離開'}],
        'status':{'value':'知道秘密','evidence':'小蘭說出秘密'}}],
        '敘事：沈晚起身離開。小蘭說出秘密。')
    assert state['characters'][sid]['current_status']==before
    assert state['characters'][sid]['presence']=='offstage'


def test_natural_scene_changes_rewind_and_regenerate_atomically(world):
    _,_,sid=make(world)
    before=copy.deepcopy(e.load('cast_test')['state'])
    user='我離開咖啡館，走到沈晚桌邊。'
    gm=lambda *a:'📅 2026/09/22　⏰ 10:10　📌 圖書館　🌤️ 晴\n沈晚抬頭。'
    record=lambda *a:[{'character_id':'lead','scene_events':[{'presence':'offstage','evidence':'我離開咖啡館'}]},
                      {'character_id':sid,'scene_events':[{'presence':'present','evidence':'走到沈晚桌邊'}]}]
    e.mutate('cast_test','send',{'message':user},gm,record)
    after=copy.deepcopy(e.load('cast_test')['state'])
    assert after['characters']['lead']['presence']=='offstage'
    assert after['characters'][sid]['presence']=='present'
    assert after['focus']==before['focus'] and after['location']=='圖書館'
    e.mutate('cast_test','regenerate',{},gm,record)
    assert e.load('cast_test')['state']==after
    e.mutate('cast_test','rewind',{'to_index':0})
    assert e.load('cast_test')['state']==before


def test_natural_audit_preserves_failed_draft_and_retry(tmp_path,monkeypatch):
    from scripts.dev import verify_card_ensemble_natural as audit
    from story_core import story_io as io, usage_db
    from story_core.card_mode import card
    import sys
    for module,key in [(io,'STORIES_DIR'),(io,'STORIES_REGISTRY_PATH'),(usage_db,'STORIES_DIR'),
                       (usage_db,'_initialized'),(card,'STORY_DESIGN_DIR')]:
        monkeypatch.setattr(module,key,getattr(module,key))  # restore runner's temporary configuration
    monkeypatch.setattr(sys,'argv',['audit','--run','offline-audit','--output-dir',str(tmp_path),'--pace','0'])
    monkeypatch.setattr(audit.time,'sleep',lambda *a:None)
    count={'gm':0,'record':0}
    def gm(*a):
        count['gm']+=1
        return f'小蘭：「第 {count["gm"]} 份草稿。」'
    def record(*a):
        count['record']+=1
        if count['record']==1:raise ValueError('synthetic recorder failure')
        return []
    monkeypatch.setattr(e,'generate',gm);monkeypatch.setattr(e,'record',record)
    audit.main()
    report=json.loads((tmp_path/'card-ensemble-natural-offline-audit.json').read_text())
    first=report['turns'][0]
    assert first['attempts'][0]['narration']=='小蘭：「第 1 份草稿。」'
    assert first['attempts'][0]['outcome']=='committed'
    assert first['attempts'][0]['recorder']['status']=='error'
    assert len(first['attempts'])==1
    assert len(report['turns'])==6 and len(report['ui_operations'])==0
    with pytest.raises(ValueError,match='Never overwrite'):
        audit.main()
    original_report=tmp_path/'card-ensemble-natural-offline-audit.json'
    report_bytes=original_report.read_bytes()
    source_save=Path(report['sandbox'])/'stories/natural/ensemble.json'
    source_bytes=source_save.read_bytes()
    monkeypatch.setattr(sys,'argv',['audit','--run','offline-rewind','--output-dir',str(tmp_path),
        '--resume-report',str(original_report),'--after-round','4','--pace','0'])
    audit.main()
    resumed=json.loads((tmp_path/'card-ensemble-natural-offline-rewind.json').read_text())
    assert [t['round'] for t in resumed['turns']]==[5,6]
    assert resumed['ui_operations'][0]['to_index']==8
    assert original_report.read_bytes()==report_bytes and source_save.read_bytes()==source_bytes


def test_unified_narrator_sees_labelled_memories_once(world):
    _,_,sid=make(world)
    doc=e.load('cast_test');doc['state']['characters'][sid]['presence']='present'
    doc['state']['characters'][sid]['memory'].append('沈晚的相簿暗記是紫色月亮')
    doc['state']['characters']['lead']['memory'].append('小蘭的私人信封是橘色星星')
    calls=[]
    def gm(user,system,recent):
        calls.append(system)
        assert system.count('沈晚的相簿暗記是紫色月亮')==1
        assert system.count('小蘭的私人信封是橘色星星')==1
        assert '不是共同知識' in system
        return '小蘭：「我帶了講義。」沈晚：「那我看第一頁。」'
    candidate,result=e.turn('cast_test',doc,'我們看看講義吧。',generate_fn=gm)
    assert len(calls)==1 and '小蘭' in result['narration'] and '沈晚' in result['narration']
    assert candidate['state']['characters']==doc['state']['characters']


def test_shared_scene_calls_one_narrator_and_one_recorder(world,monkeypatch):
    import sqlite3
    from story_core import llm_bridge
    root,_,sid=make(world);meta={'v':None};calls=[]
    monkeypatch.setattr(llm_bridge,'get_last_usage',lambda:meta['v'])
    def gm(*a):
        calls.append(1);meta['v']={'provider':'vertex','model':'test','total_tokens':101}
        return '📅 2026/09/22　⏰ 10:10　📌 咖啡館　🌤️ 晴\n小蘭點頭。沈晚：「好。」'
    def record(*a):
        meta['v']={'provider':'vertex','model':'test','total_tokens':200}
        return [{'character_id':sid,'scene_events':[{'presence':'present','evidence':'沈晚走到桌邊'}]}]
    result=e.mutate('cast_test','send',{'message':'沈晚走到桌邊。'},gm,record)
    assert len(calls)==1 and result['narration'].count('📅')==1
    assert e.load('cast_test')['state']['characters'][sid]['presence']=='present'
    with sqlite3.connect(root/'stories/cast_test/usage.db') as db:
        rows=db.execute('SELECT call_type,total_tokens FROM usage_log ORDER BY id').fetchall()
    assert rows==[('ensemble_gm',101),('ensemble_recorder',200)]


def test_midturn_arrival_keeps_public_announcement_but_not_earlier_private_words(world,monkeypatch):
    _,_,sid=make(world)
    doc=e.load('cast_test');doc['state']['characters']['lead']['presence']='offstage'
    doc['state']['characters'][sid]['presence']='present'
    user='我跟沈晚說相簿暗記是紫色月亮。我們回到小蘭桌邊坐下，我把入選通知放在兩人面前。'
    events=[{'character_id':'lead','scene_events':[{'presence':'present','evidence':'我們回到小蘭桌邊坐下'}]}]
    e.save('cast_test',doc)
    e.mutate('cast_test','send',{'message':user},lambda *a:'小蘭：「終於可以公開恭喜你了。」',lambda *a:events)
    candidate=e.load('cast_test')
    candidate['narration_actor']='lead'
    history=e.visible_history(candidate)
    prose='\n'.join(m['content'] for m in history)
    assert '紫色月亮' not in prose
    assert '入選通知放在兩人面前' in prose and '終於可以公開恭喜' in prose
    # Persist exact offsets for recall/source readers, not an invented paraphrase.
    spans=candidate['messages'][-1]['ensemble_visibility']['lead']['user']
    assert ''.join(user[a:b] for a,b in spans)=='，我把入選通知放在兩人面前。'


def test_visibility_omits_private_words_after_departure(world):
    _,_,sid=make(world);before=e.load('cast_test')['state']
    before['characters'][sid]['presence']='present'
    user='我們今天一起看展。';narration='沈晚推門離開店裡。小蘭說暗號是橘色星星。'
    updates=[{'character_id':sid,'scene_events':[{'presence':'offstage','evidence':'沈晚推門離開店裡'}]}]
    spans=e._turn_visibility(before,updates,user,narration)
    heard=''.join(narration[a:b] for a,b in spans[sid]['gm'])
    assert '橘色星星' not in heard and '推門離開' in heard


def test_authorized_history_offsets_and_compacted_access_share_one_policy(world):
    _,_,sid=make(world);doc=e.load('cast_test')
    user='不可聽見。公開通知。';gm='大家一起討論。'
    doc['messages'] += [{'role':'user','index':1,'content':user},
                        {'role':'gm','index':2,'content':gm,
                         'ensemble_visibility':{sid:{'user':[[6,len(user)]],'gm':[[0,len(gm)]]},
                                                'lead':{'user':[[0,len(user)]],'gm':[[0,len(gm)]]}}}]
    doc['source_recap']={'recap_text':'摘要','compacted_through_index':2}
    doc['narration_actor']=sid
    assert e.visible_history(doc)==[]
    allowed=e.authorized_history(doc,sid)
    assert [r['index'] for r in allowed]==[1,2]
    assert allowed[0]['content']==user[6:] and allowed[0]['start']==6
    assert allowed[0]['end']==len(user) and allowed[0]['source_length']==len(user)
    assert all('不可聽見' not in r['content'] for r in allowed)
    assert e.authorized_history(doc,'unknown')==[]


def test_addressed_actor_can_depart_without_changing_focus(world):
    _,_,sid=make(world);doc=e.load('cast_test');doc['state']['characters'][sid]['presence']='present'
    e.save('cast_test',doc)
    calls=[]
    def gm(user,system,recent):
        calls.append(system)
        return '沈晚：「我去開會了。」沈晚推門走出咖啡館。小蘭：「我們繼續看講義。」'
    result=e.mutate('cast_test','send',{'message':'沈晚，妳先去開會吧。'},generate_fn=gm,
        record_fn=lambda *a:[{'character_id':sid,'scene_events':[{'presence':'offstage','evidence':'沈晚推門走出咖啡館'}]}])
    state=e.load('cast_test')['state']
    assert len(calls)==1 and state['focus']=='lead' and state['characters'][sid]['presence']=='offstage'
