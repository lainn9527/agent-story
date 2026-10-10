"""Persistent author directions survive reload, while each generation keeps its own copy."""
from pathlib import Path
import json

import pytest
from tests.test_card_ensemble import world, make
from tests.test_card_branches import no_live_compaction
from tests.test_card_editing import body
from tests.test_card_candidates import initial, regen, versions, select, rows, record
from story_core import story_io as io
from story_core.card_mode import instructions as ins, pipeline as p, ensemble as e, branches as b, styles, ensemble_recorder as er

TEXT = '潤飾艾迪的言行時，採用風趣的語氣；沉重時不硬塞笑話。'


def get(client, slug='original', bid='main'):
    response = client.get(f'/api/card/{slug}/instruction?branch_id={bid}')
    assert response.status_code == 200, response.json
    return response.json['persistent_instruction']


def save(client, text=TEXT, enabled=True, slug='original', bid='main', **extra):
    return client.post(f'/api/card/{slug}/instruction', json={'branch_id':bid, 'text':text,
        'enabled':enabled, 'expected_revision':get(client,slug,bid)['revision'], **extra})


def files(root):
    return {str(f.relative_to(root)):f.read_bytes() for f in root.rglob('*') if f.is_file() and f.suffix != '.lock'}


@pytest.mark.parametrize('cast', [False,True])
def test_read_only_reload_toggle_validation_and_conflict(world, cast):
    root, client, _ = world; slug = 'original'
    if cast: make(world); slug='cast_test'
    store=root/'stories'/slug; before=files(store)
    default={'enabled':False,'text':'','revision':None}
    assert get(client,slug) == default
    assert client.get(f'/api/card/{slug}/state').json['persistent_instruction'] == default
    assert files(store) == before
    first=save(client, text='  '+TEXT+'  ',slug=slug).json['persistent_instruction']
    assert first['text'] == TEXT and first['enabled'] and first['revision']
    assert get(client,slug) == first
    assert client.get(f'/api/card/{slug}/state').json['persistent_instruction'] == first
    after=files(store)
    assert {k:v for k,v in after.items() if not k.endswith('card_instruction.json')} == before
    assert save(client,slug=slug).json['persistent_instruction'] == first, 'idempotent save'
    before=files(store)
    for payload in [{'text':None}, {'text':'a'*4001}, {'enabled':'true'}, {'text':'   '}]:
        assert save(client,slug=slug,**payload).status_code == 400
    assert client.post(f'/api/card/{slug}/instruction',json={'branch_id':'main','text':TEXT,'enabled':True}).status_code == 400
    assert save(client,slug=slug,expected_revision=None).status_code == 409
    assert files(store) == before
    disabled=save(client,enabled=False,slug=slug).json['persistent_instruction']
    assert not disabled['enabled'] and disabled['text'] == TEXT
    assert ins.active(get(client,slug)) == ''
    cleared=save(client,text='',enabled=False,slug=slug).json['persistent_instruction']
    assert cleared['text'] == '' and not cleared['enabled']


@pytest.mark.parametrize('cast', [False,True])
@pytest.mark.parametrize('stream', [False,True])
def test_real_send_regen_edit_paths_use_server_settings_and_record_versions(world,monkeypatch,cast,stream):
    root, client, _=world; slug='original'; captures=[]
    if cast:
        make(world); slug='cast_test'
        monkeypatch.setattr(e,'generate',lambda u,s,h:(captures.append((u,s,h)) or '她提出自己的安排。'))
        monkeypatch.setattr(e,'record',lambda *a:[])
        monkeypatch.setattr(er,'start',lambda *args,**kwargs:None)
    else:
        monkeypatch.setattr(p,'_generate_pass1',lambda **kw:(captures.append((kw['user_msg'],kw['system_prompt'],kw['recent'])) or '她提出自己的安排。','test'))
        original=p.run_card_turn
        def isolated(*args,**kwargs):
            kwargs.update(recorder_fn=lambda *a:None,async_recorder=False)
            return original(*args,**kwargs)
        monkeypatch.setattr('routes.card_routes.run_card_turn',isolated)
        monkeypatch.setattr(p,'run_card_turn',isolated)
    prefix=f'/api/card/{slug}'
    save(client,slug=slug)
    response=client.post(prefix+'/send/stream',
        json={'branch_id':'main','message':'這次請認真回答','narrative_style':'natural','plot_mode':'coauthor','scene_mode':'intimate',
              'persistent_instruction':'不要使用這個假的前端覆寫'})
    assert response.status_code == 200
    assert b'"ok": true' in response.data or b'"ok":true' in response.data  # Consume workers before fixtures exit.
    user,system,recent=captures[-1]
    assert user == '這次請認真回答' and system.count(TEXT) == 1
    assert system.index(styles.INTIMATE_SCENE) < system.index(ins.HEADER)
    assert '以本輪為準' in system and '不要使用這個假的前端覆寫' not in system
    messages=e.load(slug)['messages'] if cast else rows()
    assert messages[-1]['persistent_instruction'] == TEXT
    assert all('persistent_instruction' not in m for m in recent)
    # Regen uses the settings saved now, not the replaced version's old instruction.
    second='改用沉穩、精簡的語氣。';save(client,text=second,slug=slug)
    response=client.post(prefix+'/regenerate',json={'branch_id':'main','stream':stream})
    assert response.status_code == 200 and response.data
    assert second in captures[-1][1] and TEXT not in captures[-1][1]
    data=client.get(prefix+'/candidates?branch_id=main&message_index=2').json
    assert [v['persistent_instruction'] for v in data['versions']] == [TEXT,second]
    # Edit forks inherit current settings and one-turn editing directions stay later.
    messages=e.load(slug)['messages'] if cast else rows()
    request=body(messages,destination='fork');request.update(branch_id='main',stream=stream)
    response=client.post(prefix+'/edit',json=request)
    assert response.status_code == 200 and response.data
    child=b.tree(slug)['active_branch_id'];assert child != 'main'
    assert get(client,slug,child) == get(client,slug,'main')
    assert captures[-1][1].index(ins.HEADER) < captures[-1][1].index('【修改指示】')
    assert second in captures[-1][1]
    # Disabling retains editable text but removes the live prompt block, including historical metadata.
    save(client,text=second,enabled=False,slug=slug,bid=child)
    response=client.post(prefix+'/send/stream',json={'branch_id':child,'message':'（請繼續發展）'})
    assert response.status_code == 200 and response.data
    assert ins.HEADER not in captures[-1][1]
    assert second not in str(captures[-1])
    child_msgs=e.load(slug,child)['messages'] if cast else rows(child)
    assert child_msgs[-1]['persistent_instruction'] == ''
    npcs=child_msgs[-1].get('npcs_snapshot',[])
    assert TEXT not in json.dumps(npcs,ensure_ascii=False)
    if cast:
        assert TEXT not in json.dumps(e.load(slug,child)['state'],ensure_ascii=False)


@pytest.mark.parametrize('cast', [False,True])
def test_ordinary_forks_preserve_current_settings_and_parent_is_independent(world,cast):
    root, client, _=world; slug='original'
    if cast:
        make(world);slug='cast_test'
        e.mutate(slug,'send',{'message':'第一輪'},lambda *a:'第一版',lambda *a:[])
    else: initial()
    config=save(client,slug=slug).json['persistent_instruction']
    response=client.post(f'/api/card/{slug}/branches',json={'branch_id':'main','message_index':2})
    assert response.status_code == 200, response.json
    child=response.json['branch_id'];assert get(client,slug,child) == config
    parent=files(ins.path(slug,'main').parent)
    save(client,text='子線另用俏皮語氣',slug=slug,bid=child)
    # Parent may contain the newly created child; compare only its existing files.
    assert all((ins.path(slug,'main').parent/k).read_bytes() == v for k,v in parent.items() if not k.startswith('branches/'))
    assert get(client,slug,'main') == config
    assert get(client,slug,child)['text'] != config['text']


def test_candidate_historical_fork_inherits_current_config_not_old_version(world,monkeypatch):
    _,client,_=world
    save(client);initial();regen(client,monkeypatch)
    listing=versions(client)
    p.run_card_turn('original',user_message='後來',llm_fn=lambda *a:'後來回覆',recorder_fn=record,async_recorder=False)
    current=save(client,text='現在想使用的新語氣').json['persistent_instruction']
    result=select(client,listing)
    assert result.status_code == 200, result.json
    child=result.json['branch_id']
    assert get(client,bid=child) == current
    assert rows(child)[-1]['persistent_instruction'] == TEXT


@pytest.mark.parametrize('cast', [False,True])
def test_rewind_reset_keep_author_preferences(world,cast):
    _,client,_=world;slug='original'
    if cast:
        make(world);slug='cast_test';e.mutate(slug,'send',{'message':'早安'},lambda *a:'早安',lambda *a:[])
    else:initial()
    config=save(client,slug=slug).json['persistent_instruction']
    for action,payload in [('rewind',{'to_index':0}),('reset',{})]:
        response=client.post(f'/api/card/{slug}/{action}',json={'branch_id':'main',**payload})
        assert response.status_code == 200, response.json
        assert get(client,slug) == config


@pytest.mark.parametrize('cast', [False,True])
def test_busy_configuration_returns_409_and_write_failure_preserves_data(world,monkeypatch,cast):
    root,client,_=world;slug='original'
    if cast:make(world);slug='cast_test'
    before=files(root/'stories'/slug)
    # A separate lock holder is needed: the single-card lock is reentrant per thread.
    import threading
    acquired=threading.Event();release=threading.Event()
    def holder():
        manager=e.lock(slug) if cast else b.lock(slug)
        with manager:acquired.set();release.wait(5)
    thread=threading.Thread(target=holder);thread.start();assert acquired.wait(5)
    try:
        response=client.post(f'/api/card/{slug}/instruction',json={'enabled':True,'text':TEXT,'expected_revision':None})
        assert response.status_code == 409
    finally:release.set();thread.join(5)
    assert files(root/'stories'/slug) == before
    save(client,slug=slug);before=files(root/'stories'/slug)
    original=io._save_json
    def fail(name,data):
        if Path(name).name == 'card_instruction.json':raise OSError('disk failure')
        return original(name,data)
    monkeypatch.setattr(io,'_save_json',fail)
    result=save(client,text='新的草稿',slug=slug)
    assert result.status_code == 500 and '草稿仍保留' in result.json['error']
    assert files(root/'stories'/slug) == before


def test_disabled_has_no_prompt_change():
    assert ins.apply('原有 prompt','') == '原有 prompt'
