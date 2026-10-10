"""Manual reply versions change only latest prose, never run model/Recorder work."""
from copy import deepcopy
from pathlib import Path
import threading

import pytest

from tests.test_card_ensemble import world, make
from tests.test_card_branches import no_live_compaction
from tests.test_card_candidates import initial, rows, versions, select, regen
from story_core import story_io as io, compaction, llm_bridge
from story_core.card_mode import candidates as c, branches, ensemble as e, pipeline, ensemble_recorder


def edit(client, data, content='手改正文', slug='original', bid='main', **extra):
    return client.post(f'/api/card/{slug}/candidates', json={
        'action':'edit-text', 'branch_id':bid, 'message_index':data['message_index'],
        'expected_version_id':data['selected_id'],
        'expected_reply':next(v['content'] for v in data['versions'] if v['selected']),
        'content':content, **extra})


def files(root):
    return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def forbid_models(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Manual edit must not call a model or restart recording')
    monkeypatch.setattr(llm_bridge, 'call_oneshot', forbidden)
    monkeypatch.setattr(pipeline, 'record_saved_turn', forbidden)
    monkeypatch.setattr(ensemble_recorder, 'start', forbidden)


def test_edit_archives_old_version_preserves_feedback_state_and_context(world, monkeypatch):
    _, client, _ = world
    initial(); old = deepcopy(rows()[-1]); data = versions(client)
    assert client.post('/api/card/original/feedback', json={'message_index':2,
        'version_id':data['selected_id'], 'liked':True, 'comment':'原版的好處',
        'expected_comment_revision':None}).status_code == 200
    root = Path(io._branch_dir('original', 'main'))
    stable = {name:(root/name).read_bytes() for name in ('npcs.json','character_state.json','card_clock.json')}
    compaction.save_recap('original','main', {'recap_text':'更早的摘要', 'compacted_through_index':0, 'revision':'old'})
    forbid_models(monkeypatch)
    response = edit(client, data, '手改正文\n\n第二段。')
    assert response.status_code == 200, response.json
    assert stable == {name:(root/name).read_bytes() for name in stable}
    gm = rows()[-1]
    assert gm['content'] == '手改正文\n\n第二段。' and gm['reply_version_id'] != data['selected_id']
    for key in old:
        if key not in ('content','reply_version_id'):
            assert gm[key] == old[key], key
    current = versions(client)
    assert [v['content'] for v in current['versions']] == ['第一版','手改正文\n\n第二段。']
    assert current['regen_count'] == current['failed_count'] == 0
    assert current['versions'][0]['feedback']['liked']
    assert current['versions'][0]['feedback']['comment'] == '原版的好處'
    assert not current['versions'][1]['feedback']['liked'] and current['versions'][1]['feedback']['comment'] == ''
    assert current['versions'][1]['text_only_edit'] and current['versions'][1]['edited_at']
    assert current['versions'][1]['generated_at'] == current['versions'][0]['generated_at']
    summary = client.get('/api/card/original/state').json['messages'][-1]['candidate_summary']
    assert summary['versions'] == summary['selected_number'] == 2
    assert compaction.load_recap('original','main')['recap_text'] == '更早的摘要'
    assert compaction.load_recap('original','main')['revision'] != 'old'
    assert len(branches.tree('original')['branches']) == 1
    # Only the accepted text goes into subsequent narration.
    with monkeypatch.context() as restore:
        restore.setattr(pipeline, 'record_saved_turn', lambda *a, **k:None)
        captured=[]
        pipeline.run_card_turn('original', user_message='接下來呢', llm_fn=lambda u,s,h:(captured.append([m['content'] for m in h]) or '新回合'))
        assert '手改正文' in str(captured) and '第一版' not in str(captured)


def test_multiple_edits_stale_guard_noop_and_version_selection(world, monkeypatch):
    _, client, _ = world; initial(); data = versions(client)
    before = files(Path(io._branch_dir('original','main')))
    assert edit(client,data,'第一版').json['unchanged']
    assert files(Path(io._branch_dir('original','main'))) == before
    forbid_models(monkeypatch)
    assert edit(client,data).status_code == 200
    before = files(Path(io._branch_dir('original','main')))
    assert edit(client,data,'過期草稿').status_code == 409
    assert before == files(Path(io._branch_dir('original','main')))
    assert edit(client,versions(client),'再手改').status_code == 200
    data = versions(client)
    assert len(data['versions']) == 3 and data['regen_count'] == 0
    assert select(client,data,0).status_code == 200
    assert rows()[-1]['content'] == '第一版'
    assert select(client,versions(client),2).status_code == 200
    assert rows()[-1]['content'] == '再手改'


@pytest.mark.parametrize('change,status', [
    ({'message_index':0},400), ({'message_index':True},400), ({'message_index':1},400),
    ({'content':''},400), ({'content':' \n'},400), ({'content':None},400),
    ({'content':7},400), ({'content':'x'*200001},400),
    ({'expected_version_id':None},409), ({'expected_reply':'過期正文'},409),
])
def test_invalid_or_opening_edits_never_write(world, change, status):
    _, client, _ = world; initial(); data=versions(client)
    root = Path(io._branch_dir('original','main')); before=files(root)
    assert edit(client,data,**change).status_code == status
    assert files(root) == before


def test_cannot_edit_historical_reply_or_while_latest_is_user(world):
    _, client, _ = world; initial(); old=versions(client)
    pipeline.run_card_turn('original', user_message='下一輪', llm_fn=lambda *a:'第二輪', recorder_fn=lambda *a:None, async_recorder=False)
    root=Path(io._branch_dir('original','main')); before=files(root)
    assert edit(client,old).status_code == 400 and files(root) == before
    data=versions(client,index=4)
    io._save_json(io._story_messages_path('original','main'), rows()+[{'role':'user','content':'生成中','index':5}])
    before=files(root)
    assert edit(client,data).status_code == 400 and files(root) == before


@pytest.mark.parametrize('status', ['pending','error'])
def test_recording_pending_blocks_but_errors_are_preserved(world, monkeypatch, status):
    _, client, _ = world; initial()
    ms=rows(); ms[-1]['card_record_status']=status
    io._save_json(io._story_messages_path('original','main'),ms)
    data=versions(client); root=Path(io._branch_dir('original','main')); before=files(root)
    forbid_models(monkeypatch)
    res=edit(client,data)
    if status == 'pending':
        assert res.status_code == 409 and files(root) == before
    else:
        assert res.status_code == 200
        assert rows()[-1]['card_record_status'] == 'error'
        # A later regen may be recorded normally; selecting the manual version
        # must still preserve its historical error instead of rerunning Recorder.
        with monkeypatch.context() as restore:
            restore.setattr(pipeline,'record_saved_turn',lambda *a, **k:None)
            regen(client,restore,text='後來的候選')
        assert select(client,versions(client),1).status_code == 200
        assert rows()[-1]['content'] == '手改正文' and rows()[-1]['card_record_status'] == 'error'


@pytest.mark.parametrize('failed_file', ['card_candidates.json','messages.json','conversation_recap.json','group'])
def test_storage_failure_rolls_back_both_versions_and_timeline(world, monkeypatch, failed_file):
    _, client, _ = world; initial(); data=versions(client)
    root=Path(io._branch_dir('original','main')); before=files(root)
    save=io._save_json
    def fail(path,data):
        if (failed_file == 'group' and Path(path).parent.name == 'card_candidates') or Path(path).name == failed_file:
            raise OSError('synthetic disk failure')
        return save(path,data)
    monkeypatch.setattr(io,'_save_json',fail)
    if failed_file == 'conversation_recap.json':
        monkeypatch.setattr(compaction,'save_recap',lambda *a, **k:fail(root/failed_file, {}))
    res=edit(client,data)
    assert res.status_code == 500 and files(root) == before


@pytest.mark.parametrize('child', [False,True])
def test_ensemble_edit_keeps_controls_state_and_source_branch(world, monkeypatch, child):
    _,client,sid=make(world)
    e.mutate('cast_test','send',{'message':'展覽安排？'},lambda *a:'原版回覆',lambda *a:[])
    bid='main'
    if child:
        res=client.post('/api/card/cast_test/branches/fork',json={'message_index':2,'name':'測試線'})
        assert res.status_code == 200,res.json
        bid=res.json['branch_id']
    e.mutate('cast_test','focus',{'character_id':sid,'presence':'present'},branch_id=bid)
    old=deepcopy(e.load('cast_test',bid)); source=e.path('cast_test').read_bytes()
    data=client.get(f'/api/card/cast_test/candidates?branch_id={bid}&message_index=2').json
    forbid_models(monkeypatch)
    res=edit(client,data,slug='cast_test',bid=bid)
    assert res.status_code == 200,res.json
    updated=e.load('cast_test',bid)
    assert {k:v for k,v in updated.items() if k != 'messages'} == {k:v for k,v in old.items() if k != 'messages'}
    for field in ['ensemble_snapshot','ensemble_pre_snapshot','ensemble_record_status','ensemble_updates']:
        assert updated['messages'][-1].get(field) == old['messages'][-1].get(field)
    assert updated['messages'][-1]['content'] == '手改正文'
    if child: assert e.path('cast_test').read_bytes() == source
    data=client.get(f'/api/card/cast_test/candidates?branch_id={bid}&message_index=2').json
    assert len(data['versions']) == 2 and data['regen_count'] == 0
    assert select(client,data,0,bid=bid).status_code == 400  # wrong story cannot cross-use IDs
    res=client.post('/api/card/cast_test/candidates',json={'action':'select','branch_id':bid,'message_index':2,
        'group_id':data['group_id'],'expected_version_id':data['selected_id'],'version_id':data['versions'][0]['id']})
    assert res.status_code == 200,res.json
    assert e.load('cast_test',bid)['messages'][-1]['content'] == '原版回覆'
    assert e.load('cast_test',bid)['state'] == old['state']


def test_edit_invalidates_summary_that_contains_replaced_prose(world):
    _,client,_=world; initial(); data=versions(client)
    compaction.save_recap('original','main',{'recap_text':'第一版的舊摘要','compacted_through_index':2})
    assert edit(client,data).status_code == 200
    assert compaction.load_recap('original','main')['recap_text'] == ''


def test_ensemble_visibility_follows_edits_without_sharing_private_text():
    original='小蘭的私密近況。沈晚抵達咖啡館。大家一起討論展覽。'
    public_start=original.index('大家')
    updated='小蘭的私密近況，以及更長的秘密。沈晚抵達咖啡館。大家一起討論展覽。'
    message={'content':original, 'ensemble_visibility':{
        'lead':{'user':[[0,4]], 'gm':[[0,len(original)]]},
        'companion':{'user':[], 'gm':[[public_start,len(original)]]},
        'absent':{'user':[], 'gm':[]}}}
    ranges=c._edited_visibility(message,updated)
    assert ranges['lead']['gm'] == [[0,len(updated)]]
    assert ranges['lead']['user'] == [[0,4]] and ranges['absent']['gm'] == []
    public=''.join(updated[a:b] for a,b in ranges['companion']['gm'])
    assert public == '大家一起討論展覽。' and '秘密' not in public
    # New prose at a private/public boundary does not widen anyone's access.
    updated=original[:public_start]+'未確定誰聽到的插話。'+original[public_start:]
    ranges=c._edited_visibility(message,updated)
    assert ''.join(updated[a:b] for a,b in ranges['companion']['gm']) == '大家一起討論展覽。'


def test_busy_generation_refuses_text_save_without_writing(world):
    _,client,_=world; initial(); data=versions(client)
    entered, release=threading.Event(), threading.Event()
    def hold():
        with branches.lock('original','main'):
            entered.set(); release.wait(3)
    worker=threading.Thread(target=hold); worker.start()
    try:
        assert entered.wait(1)
        root=Path(io._branch_dir('original','main')); before=files(root)
        assert edit(client,data).status_code == 409 and files(root) == before
    finally:
        release.set(); worker.join(1)


def test_legacy_reply_and_earlier_record_error_can_be_edited(world):
    _,client,_=world; initial()
    pipeline.run_card_turn('original', user_message='第二輪', llm_fn=lambda *a:'舊存檔正文',
                           recorder_fn=lambda *a:None, async_recorder=False)
    ms=rows(); ms[2]['card_record_status']='error'
    for m in ms:
        for key in ('reply_group_id','reply_version_id','card_turn_id'):
            m.pop(key,None)
    io._save_json(io._story_messages_path('original','main'),ms)
    data=versions(client,index=4)
    assert edit(client,data).status_code == 200
    assert rows()[2]['card_record_status'] == 'error'
    data=versions(client,index=4)
    assert [v['content'] for v in data['versions']] == ['舊存檔正文','手改正文']


def test_interrupted_historical_recorder_does_not_block_text_edit(world,monkeypatch):
    _,client,_=world;initial()
    pipeline.run_card_turn('original', user_message='第二輪', llm_fn=lambda *a:'最新正文',
                           recorder_fn=lambda *a:None, async_recorder=False)
    ms=rows();ms[2].update(card_record_status='pending',card_record_pid=999999)
    io._save_json(io._story_messages_path('original','main'),ms)
    def missing(*args):raise ProcessLookupError()
    monkeypatch.setattr(branches.os,'kill',missing)
    forbid_models(monkeypatch)
    data=versions(client,index=4)
    assert edit(client,data).status_code==200
    assert rows()[2]['card_record_status']=='pending'
    assert rows()[-1]['content']=='手改正文'
