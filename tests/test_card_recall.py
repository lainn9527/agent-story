"""Recall regression: real source retrieval and strict current-timeline scopes."""
import copy
import json
import sqlite3

import pytest

from story_core.card_mode import recall


def scripted(*actions):
    pending = iter(actions)
    return lambda *_: json.dumps(next(pending), ensure_ascii=False)


def test_off_does_not_call_model_or_change_snapshot():
    history = [{'role':'gm', 'content':'開場', 'index':0}]
    before = copy.deepcopy(history)
    result = recall.collect('test','main',None,'你還記得嗎？',history,
                            ask_fn=lambda *_: pytest.fail('off must not call model'))
    assert recall.grounding(result) == '' and history == before


@pytest.mark.parametrize('bad', ['', 'enabled', [], {}, True, 1])
def test_invalid_mode(bad):
    with pytest.raises(ValueError): recall.validate_mode(bad)


def test_search_old_scene_beyond_recent_context_and_preserve_raw_sources():
    history = [{'role':'gm','index':0,'content':'初次見面，在青鳥書店躲雨。她借你一把黃色雨傘。'}]
    history += [{'role':'gm','index':i,'content':f'第{i}天，討論課業。'} for i in range(1,140)]
    calls=[]
    def agent(system, context):
        data=json.loads(context);calls.append(data)
        if len(calls)==1:return '{"action":"search","query":"雨傘"}'
        if len(calls)==2:
            hit=data['tools'][-1]['result']['hits'][0]
            return json.dumps({'action':'read','id':hit['id'],'offset':hit['offset']})
        return '{"action":"finish","invented_summary":"我們在巴黎接吻"}'
    r=recall.collect('test','main','auto','你還記得我們初次見面嗎？',history,ask_fn=agent)
    assert r['status']=='found' and r['sources'][0]['message_index']==0
    assert '黃色雨傘' in recall.grounding(r) and '巴黎' not in recall.grounding(r)
    assert calls[0]['cue_hint'] is True
    assert '青鳥書店' not in json.dumps(calls[0],ensure_ascii=False)


def test_semantic_auto_can_finish_without_search_despite_keyword():
    seen=[]
    def decide(system,context):
        seen.append(json.loads(context));return '{"action":"finish"}'
    r=recall.collect('t','main','auto','你還記得明天要買牛奶嗎？',
                     [{'role':'gm','content':'明天要買牛奶。'}],ask_fn=decide)
    assert seen[0]['cue_hint'] and len(r['steps'])==1 and not r['sources']


def lore_entries():
    return [{'id': 'shen', 'title': '沈默與御獸峰', 'text': '沈默目前在萬獸血池淬鍊。',
             'keywords': ['沈默', '御獸峰'], 'sources': []}]


def test_auto_lore_reads_without_player_naming_character():
    calls = []
    def agent(system, context):
        data = json.loads(context); calls.append(data)
        assert '玩家不必講出人物或條目名稱' in system
        if len(calls) == 1:
            assert data['current_location'] == '御獸峰'
            assert data['lore_catalog'][0]['title'] == '沈默與御獸峰'
            assert '萬獸血池' not in json.dumps(data, ensure_ascii=False)
            return '{"action":"lore_read","id":"l:shen"}'
        return '{"action":"finish"}'
    r = recall.collect('t', 'main', 'auto', '（請繼續發展）', [], ask_fn=agent,
                       lore_entries=lore_entries(), current_location='御獸峰')
    assert r['status'] == 'found' and not r['sources']
    assert r['lore_sources'][0]['text'] == lore_entries()[0]['text']
    assert '導演背景' in recall.grounding(r)


def test_lore_search_preview_requires_full_read():
    r = recall.collect('t', 'main', 'auto', '去看看他', [], lore_entries=lore_entries(),
        ask_fn=scripted({'action':'lore_search','query':'御獸峰'}, {'action':'finish'},
                        {'action':'lore_read','id':'l:shen'}, {'action':'finish'}))
    assert r['status'] == 'found' and len(r['steps']) == 4
    assert 'error' in r['steps'][1]['result']


def test_partial_lore_never_becomes_full_evidence():
    entries = lore_entries(); entries[0]['text'] = '已確認規則。' * 600
    r = recall.collect('t', 'main', 'auto', '繼續', [], lore_entries=entries,
        ask_fn=scripted({'action':'lore_read','id':'l:shen'},
                        {'action':'finish','reason':'no_relevant_history'}))
    assert not r['lore_sources'] and r['status'] == 'no_evidence'
    assert entries[0]['text'] not in recall.grounding(r)
    r = recall.collect('t', 'main', 'auto', '繼續', [], lore_entries=entries,
        ask_fn=scripted({'action':'lore_read','id':'l:shen'},
                        {'action':'lore_read','id':'l:shen','offset':2400}, {'action':'finish'}))
    assert r['lore_sources'][0]['text'] == entries[0]['text']


def test_unknown_lore_id_cannot_read_other_files():
    r = recall.collect('t', 'main', 'auto', '繼續', [], lore_entries=lore_entries(),
        ask_fn=scripted({'action':'lore_read','id':'../../other/card_lore.json'}, {'action':'finish'}))
    assert not r['lore_sources'] and r['status'] == 'no_evidence'


def test_deep_can_search_without_trigger_and_browse_first_meeting():
    history=[{'role':'gm','index':10,'content':'📌 青鳥書店。她遞給你黃色雨傘。'}]
    r=recall.collect('t','main','deep','其實，我有話想跟你說。',history,
                    ask_fn=scripted({'action':'search','query':'','order':'oldest'},
                                    {'action':'read','id':'m0'}, {'action':'finish'}))
    assert r['sources'][0]['message_index']==10
    assert '黃色雨傘' in recall.grounding(r)


def test_missing_and_hallucinated_source_ids_never_become_evidence():
    r=recall.collect('t','main','deep','巴黎旅行', [{'role':'gm','content':'在書店見面。'}],
                    ask_fn=scripted({'action':'search','query':'巴黎'},
                        {'action':'read','id':'../../other/story'},
                        {'action':'read','id':'m0'}, {'action':'finish'}))
    assert not r['sources'] and r['status']=='no_evidence'
    assert '不可因此編造' in recall.grounding(r)


def test_long_prose_tail_and_pagination_keep_exact_offsets():
    text='普通日常。'*1800+'我們在七星潭看日出，妳把藍色圍巾借給我。'+'後續。'*800
    reader=recall.HistoryReader([{'role':'gm','content':text,'index':20}])
    hit=reader.search('七星潭 圍巾')['hits'][0]
    source=reader.read(hit['id'], hit['offset'])
    assert '七星潭' in source['text']
    assert source['text']==text[source['start']:source['end']]
    assert source['next_offset'] is not None
    assert reader.read('m0',source['next_offset'])['end']>source['end']


def test_neighbors_keep_negation_and_quote_context():
    history=[{'role':'user','index':7,'content':'如果明年去巴黎呢？'},
             {'role':'gm','index':9,'content':'她搖頭：「我們還沒去過巴黎。」'}]
    r=recall.collect('t','main','deep','巴黎',history,ask_fn=scripted(
        {'action':'search','query':'搖頭'}, {'action':'read','id':'m1'},
        {'action':'read','id':'m0'}, {'action':'finish'}))
    assert len(r['sources'])==2
    assert '還沒去過' in recall.grounding(r) and '如果明年' in recall.grounding(r)


def test_budget_stops_repeated_actions():
    count=[]
    def repeat(*_): count.append(1);return '{"action":"search","query":"旅行"}'
    r=recall.collect('t','main','auto','旅行',[{'role':'gm','content':'旅行'}],ask_fn=repeat)
    assert len(count)==4 and not r['sources']


def test_malformed_agent_output_is_not_narration():
    with pytest.raises(ValueError,match='回憶查詢失敗'):
        recall.collect('t','main','deep','回憶',[{'role':'gm','content':'以前'}],
                       ask_fn=lambda *_:'我想起我們去過巴黎')


def history_doc():
    return {'baseline_index':2,'source':'never_read_source_card',
            'state':{'characters':{'lead':{},'shen':{}}},
            'source_recap':{'recap_text':'摘要','compacted_through_index':6},
            'messages':[
                {'role':'gm','index':0,'content':'小蘭初見的黃色雨傘'},
                {'role':'user','index':1,'content':'只跟小蘭說的秘密'},
                {'role':'gm','index':2,'content':'小蘭答應保密'},
                {'role':'user','index':3,'content':'只跟沈晚說的藍色相簿'},
                {'role':'gm','index':4,'content':'沈晚拿出藍色相簿','ensemble_audience':['shen']},
                {'role':'user','index':5,'content':'兩人一起去七星潭'},
                {'role':'gm','index':6,'content':'在七星潭看日出','ensemble_audience':['lead','shen']},
                {'role':'user','index':7,'content':'換場前的私話'},
                {'role':'gm','index':8,'content':'換場後才到達','ensemble_audience':[]},
                {'role':'gm','index':10,'content':'無 audience 的舊紀錄'}]}


def test_ensemble_actor_privacy_source_baseline_and_covered_history():
    doc=history_doc();before=copy.deepcopy(doc)
    lead=json.dumps(recall.ensemble_history(doc,'lead'),ensure_ascii=False)
    shen=json.dumps(recall.ensemble_history(doc,'shen'),ensure_ascii=False)
    assert '黃色雨傘' in lead and '秘密' in lead and '藍色相簿' not in lead
    assert '黃色雨傘' not in shen and '秘密' not in shen and '藍色相簿' in shen
    assert all('七星潭' in s for s in (lead,shen))
    assert all('私話' not in s and '舊紀錄' not in s for s in (lead,shen))
    assert recall.ensemble_history(doc,'unknown')==[] and doc==before


def test_rewind_and_reused_index_cannot_retrieve_removed_future():
    doc=history_doc()
    doc['messages']=doc['messages'][:3]+[
        {'role':'user','index':3,'content':'改走淡水'},
        {'role':'gm','index':4,'content':'小蘭在淡水','ensemble_audience':['lead']}]
    reader=recall.HistoryReader(recall.ensemble_history(doc,'lead'))
    assert not reader.search('七星潭')['hits']
    assert not reader.search('藍色相簿')['hits']
    assert reader.search('淡水')['hits'][0]['message_index']==3


def test_recall_usage_persists_each_step_even_invalid_json(tmp_path,monkeypatch):
    from story_core import llm_bridge, usage_db
    monkeypatch.setattr(usage_db,'STORIES_DIR',str(tmp_path))
    monkeypatch.setattr(usage_db,'_initialized',set())
    def model(*args,**kwargs):
        llm_bridge._tls.last_usage={'provider':'test','model':'fake','prompt_tokens':13,'output_tokens':2,'total_tokens':15}
        return 'bad json'
    monkeypatch.setattr(llm_bridge,'call_oneshot',model)
    with pytest.raises(ValueError):
        recall.collect('isolate','main','auto','記得嗎',[{'role':'gm','content':'你好'}])
    with sqlite3.connect(tmp_path/'isolate/usage.db') as db:
        assert db.execute('select call_type,total_tokens from usage_log').fetchall()==[('card_recall',15)] * 2
    monkeypatch.setattr(llm_bridge,'call_oneshot',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('transport')))
    with pytest.raises(RuntimeError): recall._ask('isolate','main','system','user')
    with sqlite3.connect(tmp_path/'isolate/usage.db') as db:
        assert db.execute('select count(*) from usage_log').fetchone()[0]==2


@pytest.mark.parametrize('mode', ['auto', 'deep'])
def test_invalid_decision_retries_once_then_reads_actual_source(mode):
    responses=iter(['', '{"action":"search","query":"午餐"}',
                    '{"action":"read","id":"m0"}', '{"action":"finish"}'])
    seen=[]
    def agent(system,context):
        seen.append(json.loads(context))
        return next(responses)
    result=recall.collect('t','main',mode,'那次吃飯',
                          [{'role':'gm','content':'當天中午一起吃午餐。'}],ask_fn=agent)
    assert result['status']=='found' and len(result['retries'])==1
    assert result['sources'][0]['text']=='當天中午一起吃午餐。'
    assert seen[1]['tools']==[] and 'format_reminder' in seen[1]
    assert len(seen)==4


def test_retry_budget_is_shared_across_entire_turn():
    responses=iter(['', '{"action":"search","query":"午餐"}', ''])
    calls=[]
    def agent(*args):
        calls.append(1);return next(responses)
    with pytest.raises(ValueError,match='自動重試仍失敗'):
        recall.collect('t','main','deep','那次吃飯',
                       [{'role':'gm','content':'午餐'}],ask_fn=agent)
    assert len(calls)==3


def test_finish_after_search_requires_read_or_explicit_irrelevance():
    history=[{'role':'gm','content':'中午一起吃午餐。'}]
    r=recall.collect('t','main','auto','那次午餐',history,ask_fn=scripted(
        {'action':'search','query':'午餐'}, {'action':'finish'},
        {'action':'read','id':'m0'}, {'action':'finish'}))
    assert r['status']=='found' and r['sources'][0]['text']==history[0]['content']
    r=recall.collect('t','main','auto','不是這次午餐',history,ask_fn=scripted(
        {'action':'search','query':'午餐'}, {'action':'finish','reason':'no_relevant_history'}))
    assert r['status']=='no_evidence' and not r['budget_exhausted']


def test_temporary_errors_back_off_without_repeating_completed_search(monkeypatch):
    from story_core.llm_errors import TransientStructuredError
    waits=[];monkeypatch.setattr(recall.time,'sleep',waits.append)
    calls=[]
    def ask(system,context):
        calls.append(json.loads(context))
        if len(calls)==1: return '{"action":"search","query":"午餐"}'
        if len(calls) in (2,3): raise TransientStructuredError('500')
        if len(calls)==4: return '{"action":"read","id":"m0"}'
        return '{"action":"finish"}'
    result=recall.collect('t','main','auto','那次午餐',
                          [{'role':'gm','content':'中午吃午餐。'}],ask_fn=ask)
    assert waits==[1,2] and result['status']=='found'
    assert len(result['retries'])==2
    assert calls[1]==calls[2]==calls[3]
    assert len([s for s in result['steps'] if isinstance(s['action'],dict) and s['action']['action']=='search'])==1


def test_persistent_service_error_stops_without_narration(monkeypatch):
    from story_core.llm_errors import TransientStructuredError
    waits=[];monkeypatch.setattr(recall.time,'sleep',waits.append)
    def ask(*_): raise TransientStructuredError('500')
    with pytest.raises(ValueError,match='模型服務暫時無法使用'):
        recall.collect('t','main','deep','那次午餐',[{'role':'gm','content':'午餐'}],ask_fn=ask)
    assert waits==[1,2]


def test_recall_requests_provider_json_schema(monkeypatch):
    from story_core import llm_bridge
    captured={}
    def oneshot(prompt,**kwargs):
        captured.update(kwargs);return '{"action":"finish"}'
    monkeypatch.setattr(llm_bridge,'call_oneshot',oneshot)
    assert recall._ask('t','main','system','context')=='{"action":"finish"}'
    assert captured['response_schema']==recall.ACTION_SCHEMA


# Reuse the isolated adult-card fixture; it never reads a player's story.
from tests.test_card_ensemble import world, make


def test_single_pipeline_recalls_summarized_history(world,monkeypatch):
    from story_core.card_mode import pipeline
    from story_core import story_io as io, compaction
    root,_,_=world
    monkeypatch.setattr(compaction,'STORIES_DIR',str(root/'stories'))
    history=[{'role':'gm','index':0,'content':'青鳥書店初見，小蘭借你黃色雨傘。'}]
    history += [{'role':'gm','index':i,'content':f'第{i}天的課業閒聊。'} for i in range(1,80)]
    io._save_json(str(root/'stories/original/branches/main/messages.json'),history)
    monkeypatch.setattr(compaction,'load_recap',lambda *_:{'recap_text':'認識一段時間的朋友。','compacted_through_index':69,'recap_version':2})
    monkeypatch.setattr(compaction,'compact_async',lambda *a,**k:None)
    answers=scripted({'action':'search','query':'雨傘'},{'action':'read','id':'m0'},{'action':'finish'})
    monkeypatch.setattr(recall,'_ask',lambda slug,branch,system,context:answers(system,context))
    def gm(user,system,recent):
        assert '黃色雨傘' in system
        assert '黃色雨傘' not in json.dumps(recent,ensure_ascii=False)
        assert 'message_index' in system
        return '小蘭：「那把黃色雨傘，我還留著。」'
    result=pipeline.run_card_turn('original',user_message='你還記得初次見面嗎？',
        recall_mode='deep',llm_fn=gm,recorder_fn=lambda *a:None,async_recorder=False)
    assert result['ok']


@pytest.mark.parametrize('endpoint', ['send/stream','regenerate'])
@pytest.mark.parametrize('bad', ['wrong', {}, True])
def test_routes_reject_mode_before_any_mutation(world,endpoint,bad):
    root,client,_=world
    before={p:p.read_bytes() for p in (root/'stories').rglob('*') if p.is_file()}
    response=client.post('/api/card/original/'+endpoint,json={'message':'你好','recall_mode':bad})
    assert response.status_code==400
    assert before=={p:p.read_bytes() for p in (root/'stories').rglob('*') if p.is_file()}


@pytest.mark.parametrize('endpoint,stream', [('send/stream',True),('regenerate',True),('regenerate',False)])
def test_routes_forward_recall_choice(world,monkeypatch,endpoint,stream):
    from routes import card_routes
    root,client,_=world
    p=root/'stories/original/branches/main/messages.json'
    p.write_text(json.dumps([{'role':'gm','index':0,'content':'開場'},
                            {'role':'user','index':1,'content':'問候'},
                            {'role':'gm','index':2,'content':'回覆'}]))
    monkeypatch.setattr('story_core.compaction.STORIES_DIR',str(root/'stories'))
    captured=[]
    def pipeline(**kwargs):
        captured.append(kwargs)
        return {'ok':True,'narration':'你好'}
    monkeypatch.setattr(card_routes,'run_card_turn',pipeline)
    monkeypatch.setattr(recall,'collect',lambda *a,**k:{'mode':'deep','status':'no_evidence','sources':[],'steps':[]})
    response=client.post('/api/card/original/'+endpoint,json={'message':'問候','recall_mode':'deep','stream':stream})
    response.get_data()
    assert response.status_code==200 and captured[0]['recall_mode']=='deep'


def test_ensemble_queries_are_per_actor_and_failure_leaves_save_intact(world,monkeypatch):
    from story_core.card_mode import ensemble as e
    _,_,sid=make(world)
    doc=e.load('cast_test')
    doc['state']['characters'][sid]['presence']='present'
    doc['messages'] += [
        {'role':'user','index':1,'content':'只和小蘭說紫色雨傘'},
        {'role':'gm','index':2,'content':'小蘭拿紫色雨傘','ensemble_audience':['lead']},
        {'role':'user','index':3,'content':'只和沈晚說銀色沙漏'},
        {'role':'gm','index':4,'content':'沈晚拿銀色沙漏','ensemble_audience':[sid]}]
    e.save('cast_test',doc)
    def ask(slug,branch,system,context):
        data=json.loads(context)
        if not data['tools']:
            query='紫色雨傘' if data['actor']=='李小蘭' else '銀色沙漏'
            return json.dumps({'action':'search','query':query})
        if len(data['tools'])==1:
            return json.dumps({'action':'read','id':data['tools'][0]['result']['hits'][0]['id']})
        return '{"action":"finish"}'
    monkeypatch.setattr(recall,'_ask',ask)
    captured=[]
    def gm(user,system,recent):
        captured.append(system)
        assert '紫色雨傘' in system and '銀色沙漏' in system
        assert '李小蘭 的個人回憶查詢' in system and '沈晚 的個人回憶查詢' in system
        return '「我記得。」'
    e.mutate('cast_test','send',{'message':'你還記得嗎？','recall_mode':'deep'},gm,lambda *a:[])
    assert len(captured)==1
    before=e.path('cast_test').read_bytes()
    monkeypatch.setattr(recall,'_ask',lambda *a:'bad json')
    with pytest.raises(ValueError):
        e.mutate('cast_test','regenerate',{'recall_mode':'deep'},gm,lambda *a:[])
    assert e.path('cast_test').read_bytes()==before


def test_single_regenerate_recall_failure_preserves_original_reply(world,monkeypatch):
    root,client,_=world
    p=root/'stories/original/branches/main/messages.json'
    p.write_text(json.dumps([{'role':'gm','index':0,'content':'開場'},
                            {'role':'user','index':1,'content':'妳還記得嗎'},
                            {'role':'gm','index':2,'content':'原回覆保留'}]))
    before={p:p.read_bytes() for p in (root/'stories').rglob('*') if p.is_file()}
    monkeypatch.setattr(recall,'_ask',lambda *a:'bad JSON')
    response=client.post('/api/card/original/regenerate',json={'recall_mode':'deep','stream':True})
    assert response.status_code==502
    after={p:p.read_bytes() for p in (root/'stories').rglob('*') if p.is_file()}
    assert all(after[p]==raw for p,raw in before.items())
    # Count this accepted regen failure without changing original gameplay files.
    extra=set(after)-set(before)
    assert all(p.name=='card_candidates.json' or p.parent.name=='card_candidates' for p in extra)
    data=client.get('/api/card/original/candidates?message_index=2').json
    assert data['regen_count']==data['failed_count']==1
    assert data['versions'][0]['content']=='原回覆保留'


def test_regenerate_lore_preflight_excludes_reply_being_replaced(world, monkeypatch):
    from routes import card_routes
    from story_core.card_mode import lore
    from story_core import story_io as io
    root, client, _ = world
    messages = [{'role': 'gm', 'index': 0, 'content': '開場設定'},
                {'role': 'user', 'index': 1, 'content': '請繼續發展'},
                {'role': 'gm', 'index': 2, 'content': '待替換設定',
                 'pre_clock_snapshot': {'last_location': '御獸峰'}}]
    io._save_json(str(root / 'stories/original/branches/main/messages.json'), messages)
    io._save_json(str(lore.path('original', 'main')), {'schema_version': 1, 'entries': [
        {'id': str(m['index']), 'text': m['content'], 'sources': [
            {'index': m['index'], 'hash': lore.content_hash(m['content'])}]}
        for m in messages if m['role'] == 'gm']})
    calls = []
    def collect(*args, **kwargs):
        calls.append(kwargs)
        return {'mode': 'auto', 'status': 'no_evidence', 'sources': [], 'lore_sources': []}
    monkeypatch.setattr(recall, 'collect', collect)
    monkeypatch.setattr(card_routes, 'run_card_turn', lambda **kwargs: {'ok': True, 'narration': '新回覆'})
    monkeypatch.setattr('story_core.compaction.STORIES_DIR', str(root / 'stories'))
    response = client.post('/api/card/original/regenerate',
                           json={'recall_mode': 'auto', 'stream': False})
    assert response.status_code == 200
    assert [e['id'] for e in calls[0]['lore_entries']] == ['0']
    assert calls[0]['current_location'] == '御獸峰'


def test_midturn_spans_recall_public_fact_without_reading_private_prefix():
    doc=history_doc()
    user='只告訴小蘭的暗號是紫蝶。沈晚走進來後，我說攝影展入選了。'
    gm='小蘭藏好暗號紙條。沈晚說恭喜，小蘭也一起笑了。'
    user_start=user.index('我說')
    gm_start=gm.index('沈晚說')
    doc['messages'] += [
        {'role':'user','index':11,'content':user},
        {'role':'gm','index':12,'content':gm,'ensemble_audience':['lead','shen'],
         'ensemble_visibility':{'shen':{'user':[[user_start,len(user)]], 'gm':[[gm_start,len(gm)]]}}}]
    rows=recall.ensemble_history(doc,'shen')
    reader=recall.HistoryReader(rows)
    assert not reader.search('紫蝶')['hits'] and not reader.search('暗號')['hits']
    hit=reader.search('入選')['hits'][0]
    result=reader.read(hit['id'],hit['offset'])
    assert result['start']==user_start
    assert result['text']==user[result['start']:result['end']]
    assert reader.read(hit['id'],0).get('error')
    assert reader.read(hit['id'],len(user)).get('error')
    assert not any(r['index']==12 for r in recall.ensemble_history(doc,'lead'))


def test_disjoint_spans_not_joined_across_hidden_scene():
    doc=history_doc();text='公開前段。私人暗號紫蝶。公開後段。'
    doc['messages'].append({'role':'gm','index':12,'content':text,
        'ensemble_visibility':{'shen':{'gm':[[0,5],[12,len(text)]]}}})
    rows=[r for r in recall.ensemble_history(doc,'shen') if r['index']==12]
    assert len(rows)==2
    assert rows[0]['source_start']==0 and rows[1]['source_start']==12
    assert all('紫蝶' not in r['content'] for r in rows)


def test_empty_or_malformed_visibility_never_falls_back_to_full_audience():
    doc=history_doc()
    doc['messages'].append({'role':'gm','index':12,'content':'私話紫蝶',
        'ensemble_audience':['shen'],'ensemble_visibility':{'shen':{'gm':[['0',100],[-1,5],[0,999]]}}})
    assert '紫蝶' not in json.dumps(recall.ensemble_history(doc,'shen'),ensure_ascii=False)


def test_selector_requires_backend_capability(world):
    from flask import render_template
    _,client,_=world
    assert b'id="recallModeSelect"' in client.get('/card/original').data
    with client.application.app_context():
        old_backend_page=render_template('card_chat.html',slug='original',v='test')
    assert 'id="recallModeSelect"' not in old_backend_page
