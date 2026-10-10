"""Feedback belongs to a reply version, never to the playable timeline."""
from copy import deepcopy
from pathlib import Path

import pytest
from tests.test_card_ensemble import world, make
from tests.test_card_branches import no_live_compaction
from tests.test_card_candidates import initial, regen, versions, select, rows, record
from story_core import story_io as io
from story_core.card_mode import candidates as c, pipeline as p, ensemble as e


def feedback(client, index=2, bid='main', slug='original'):
    state = client.get(f'/api/card/{slug}/state?branch_id={bid}').json
    return next(m['feedback'] for m in state['messages'] if m['index'] == index)


def post(client, view, index=2, bid='main', slug='original', **fields):
    return client.post(f'/api/card/{slug}/feedback', json={
        'branch_id':bid, 'message_index':index, 'version_id':view['version_id'], **fields})


def files(root):
    return {str(f.relative_to(root)):f.read_bytes() for f in root.rglob('*') if f.is_file()}


def test_read_only_state_and_like_comment_preserve_gameplay(world):
    root, client, _ = world; initial()
    store = root/'stories/original'; before = files(store)
    view = feedback(client)
    assert view == {'version_id':rows()[-1]['reply_version_id'], 'liked':False,
                    'comment':'', 'comment_revision':None}
    assert client.get('/api/card/original/feedback').json['items'] == []
    assert before == files(store)
    assert post(client, view, liked=True).status_code == 200
    assert not view['liked'] and feedback(client)['liked']
    view = feedback(client)
    result = post(client, view, comment='沒有迎合，非常喜歡。', expected_comment_revision=None)
    assert result.status_code == 200, result.json
    revised = result.json['feedback']; assert revised['comment_revision']
    assert post(client, revised, liked=False).json['feedback'] == {**revised, 'liked':False}
    item = client.get('/api/card/original/feedback').json['items'][0]
    assert item['user_message'] == '你怎麼想？' and item['content'] == '第一版'
    assert item['context']['before'][-1]['role'] == 'user'
    assert item['context']['source_branch_id'] == 'main'
    assert item['provider'] == 'custom' and item['generated_at']
    after = files(store)
    assert {k:v for k,v in after.items() if not k.startswith('branches/main/card_candidates')} == before
    # Removing a note only affects its own field; no implicit re-like.
    cleared = post(client, revised, comment='', expected_comment_revision=revised['comment_revision'])
    assert cleared.status_code == 200 and not cleared.json['feedback']['liked']
    assert client.get('/api/card/original/feedback').json['items'] == []


def test_conflicts_invalid_input_and_stale_version_do_not_write(world, monkeypatch):
    root, client, _ = world; initial(); view = feedback(client)
    changed = post(client, view, comment='第一筆', expected_comment_revision=None).json['feedback']
    before = files(root/'stories/original')
    assert post(client, view, comment='過期草稿', expected_comment_revision=None).status_code == 409
    for data in [{'liked':'yes'}, {'comment':1}, {'comment':'x'*4001}, {'comment':'缺 revision'},
                 {'liked':True,'message_index':True}, {'liked':True,'version_id':'錯誤'},
                 {'liked':True,'group_id':'../bad'}]:
        assert post(client, changed, **data).status_code == 400
    assert before == files(root/'stories/original')
    assert regen(client, monkeypatch).status_code == 200
    before = files(root/'stories/original')
    assert post(client, changed, liked=True).status_code == 400
    assert before == files(root/'stories/original')


def test_regen_old_preview_and_selection_keep_their_own_feedback(world, monkeypatch):
    _, client, _ = world; initial(); view = feedback(client)
    post(client, view, liked=True)
    first = post(client, view, comment='閃光點', expected_comment_revision=None).json['feedback']
    context = client.get('/api/card/original/feedback').json['items'][0]['context']
    regen(client, monkeypatch)
    assert feedback(client)['version_id'] != first['version_id']
    assert feedback(client)['comment'] == '' and not feedback(client)['liked']
    listing = versions(client)
    assert listing['versions'][0]['feedback'] == first
    assert listing['versions'][1]['feedback'] == feedback(client)
    # An explicit archive group permits rating an unchosen version.
    assert post(client, first, group_id=listing['group_id'], liked=False).status_code == 200
    assert select(client, listing).status_code == 200
    current = feedback(client)
    assert current == {**first, 'liked':False}
    assert client.get('/api/card/original/feedback').json['items'][0]['context'] == context
    captured = []; recorded = []
    p.run_card_turn('original', user_message='接著呢',
        llm_fn=lambda u,s,h:(captured.append((u,s,h)) or '新的回覆'),
        recorder_fn=lambda *args:recorded.append(args), async_recorder=False)
    assert '閃光點' not in str(captured) + str(recorded) + str(rows())
    assert 'feedback_context' not in str(captured) + str(recorded)


@pytest.mark.parametrize('fork_kind', ['ordinary', 'candidate'])
def test_fork_inherits_only_retained_feedback_and_edits_are_independent(world, monkeypatch, fork_kind):
    _, client, _ = world; initial()
    opening = feedback(client, 0); post(client, opening, index=0, liked=True)
    first = feedback(client); post(client, first, liked=True)
    regen(client, monkeypatch); listing = versions(client)
    p.run_card_turn('original', user_message='未來', llm_fn=lambda *a:'未來回覆',
                    recorder_fn=record, async_recorder=False)
    post(client, feedback(client, 4), index=4, liked=True)
    if fork_kind == 'ordinary':
        response = client.post('/api/card/original/branches', json={'branch_id':'main','message_index':2})
    else: response = select(client, listing)
    assert response.status_code == 200, response.json
    child = response.json['branch_id']
    parent = files(Path(io._branch_dir('original', 'main')))
    items = client.get(f'/api/card/original/feedback?branch_id={child}').json['items']
    assert {item['message_index'] for item in items} == {0,2}
    original = next(item for item in items if item['message_index'] == 2)
    result = post(client, original['feedback'], bid=child, group_id=original['group_id'],
                  comment='子線評語', expected_comment_revision=None)
    assert result.status_code == 200
    assert files(Path(io._branch_dir('original', 'main'))) == parent
    assert all(item['feedback']['comment'] != '子線評語'
               for item in client.get('/api/card/original/feedback?branch_id=main').json['items'])


@pytest.mark.parametrize('action,body', [('rewind', {'to_index':0}), ('reset', {})])
def test_removed_version_keeps_feedback_without_attaching_to_reused_index(world, action, body):
    _, client, _ = world; initial(); old = feedback(client); post(client, old, liked=True)
    assert client.post(f'/api/card/original/{action}',json=body).status_code == 200
    initial(); fresh = feedback(client)
    assert fresh['version_id'] != old['version_id'] and not fresh['liked']
    items = client.get('/api/card/original/feedback').json['items']
    assert len(items) == 1 and items[0]['feedback']['version_id'] == old['version_id']
    assert post(client, old, liked=False).status_code == 400


def test_pending_recorder_and_sync_do_not_overwrite_feedback(world):
    _, client, _ = world; initial()
    saved = rows(); saved[-1]['card_record_status'] = 'pending'
    io._save_json(io._story_messages_path('original','main'),saved)
    view = feedback(client); assert post(client, view, liked=True).status_code == 200
    saved[-1]['card_record_status'] = 'done'
    saved[-1]['npcs_snapshot'][0]['memory'].append('後來整理完成')
    c.sync_existing('original','main', saved[-1])
    assert feedback(client)['liked']
    version = next(iter(c.load('original','main')['groups'].values()))['versions'][0]
    assert version['message']['card_record_status'] == 'done'
    assert version['liked'] and 'feedback_context' in version


def test_feedback_write_failure_rolls_back_both_archive_files(world, monkeypatch):
    root, client, _ = world; initial(); view = feedback(client)
    post(client, view, liked=True); before = files(root/'stories/original')
    original = io._save_json
    def fail(name, data):
        if Path(name).name == 'card_candidates.json': raise OSError('disk failure')
        return original(name,data)
    monkeypatch.setattr(io,'_save_json',fail)
    with pytest.raises(OSError): post(client, view, comment='不能保存',expected_comment_revision=None)
    assert before == files(root/'stories/original')


def test_legacy_feedback_ids_match_without_mutating_messages(world):
    _, client, _ = world
    legacy = [{'role':'gm','content':'開場','index':0}]
    for i in range(1,301,2):
        legacy.extend([{'role':'user','content':f'問題{i} " 換行\n', 'index':i},
                       {'role':'gm','content':f'回覆{i}', 'index':i+1}])
    before = deepcopy(legacy)
    views = c.feedback_views('original','main',legacy)
    for pos,m in enumerate(legacy):
        if m['role'] == 'gm': assert views[str(m['index'])]['version_id'] == c.identity(legacy,pos)[1]
    assert legacy == before


def test_ensemble_feedback_is_branch_local_and_does_not_change_cast(world):
    _, client, _ = make(world)
    e.mutate('cast_test','send', {'message':'今天呢'}, lambda *a:'共同演出', lambda *a:[])
    before = e.path('cast_test').read_bytes(); view = feedback(client, slug='cast_test')
    assert post(client, view, slug='cast_test', liked=True).status_code == 200
    assert before == e.path('cast_test').read_bytes()
    e.mutate('cast_test','regenerate', {}, lambda *a:'新版演出', lambda *a:[])
    assert not feedback(client,slug='cast_test')['liked']
    item = client.get('/api/card/cast_test/feedback').json['items'][0]
    assert item['content'] == '共同演出' and item['feedback']['version_id'] == view['version_id']
    branch = client.post('/api/card/cast_test/branches', json={'branch_id':'main','message_index':2})
    assert branch.status_code == 200, branch.json
    child = branch.json['branch_id']
    before = e.path('cast_test','main').read_bytes()
    assert post(client, view, bid=child, slug='cast_test', group_id=item['group_id'],
                comment='多角色評語',expected_comment_revision=None).status_code == 200
    assert before == e.path('cast_test','main').read_bytes()
    assert client.get('/api/card/cast_test/feedback?branch_id=main').json['items'][0]['feedback']['comment'] == ''
