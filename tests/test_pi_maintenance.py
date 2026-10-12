"""Real receipt replay + branch-local commits, without billable model calls."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import pytest
from story_core import story_io as io, compaction
from story_core.card_mode import pi_maintenance as tasks, ensemble, lore


@pytest.fixture
def story(tmp_path, monkeypatch):
    monkeypatch.setattr(io, 'STORIES_DIR', str(tmp_path/'stories'))
    monkeypatch.setattr(io, 'STORY_DESIGN_DIR', str(tmp_path/'design'))
    monkeypatch.setattr(compaction, 'STORIES_DIR', io.STORIES_DIR)
    monkeypatch.setattr(tasks, 'load_card', lambda _: {})
    monkeypatch.setattr(ensemble, 'enabled', lambda _: False)
    npcs = [{'name': '小蘭', 'memory': ['收到草藥。', '收到艾迪送的草藥。', '她拒絕同行。']},
            {'name': '旁人', 'memory': ['收到草藥。']}]
    old = deepcopy(npcs); old[0]['memory'] = ['收到草藥。']
    messages = [{'index': i, 'role': 'gm', 'content': '艾迪送她草藥，她收下了。她拒絕同行。七峰共同管理渡口。',
                 'npcs_snapshot': deepcopy(old), 'pre_npcs_snapshot': deepcopy(old)} for i in range(12)]
    messages[-1]['npcs_snapshot'] = deepcopy(npcs)
    io._save_json(io._story_messages_path('s', 'main'), messages)
    io._save_json(io._story_npcs_path('s', 'main'), npcs)
    io._save_json(str(lore.path('s', 'main')), {'schema_version': 1, 'entries': [{'id':'core','text':'作者設定','always':True}]})
    return tmp_path


MERGE = {'kind':'memory','character':0,'target_ids':['mem:0:0','mem:0:1'],'text':'收到艾迪送的草藥。'}
LORE = {'kind':'lore','target_ids':[],'title':'渡口','keywords':['渡口'],'text':'七峰共同管理渡口。'}
SUMMARY = {'kind':'summary','target_ids':['summary'],'text':'艾迪送她草藥，她收下了。她拒絕同行。'}


def receipt(tmp_path, frozen, operations, audit=True):
    root = tmp_path/'run'; root.mkdir(parents=True, exist_ok=True)
    (root/'snapshot.json').write_text(json.dumps(frozen, ensure_ascii=False))
    (root/'ops.json').write_text(json.dumps(operations, ensure_ascii=False))
    script = """
import {readFileSync,writeFileSync} from 'node:fs';
import {MaintenanceSession} from './scripts/lore/pi_maintenance_guard.mjs';
const root=process.argv[1],f=JSON.parse(readFileSync(root+'/snapshot.json','utf8')),s=new MaintenanceSession(f),events=[];
function call(name,args){const id=String(events.length);events.push({type:'tool_execution_start',toolCallId:id,toolName:name,args});const r=s[name](args);events.push({type:'tool_execution_end',toolCallId:id,result:{content:[{type:'text',text:JSON.stringify(r)}]}});return r;}
for(const d of f.records){let offset=0;do{offset=call('read',{id:d.id,offset}).next_offset;}while(offset!==null);}
call('search',{query:'草藥'});
const p=call('propose',{operations:JSON.parse(readFileSync(root+'/ops.json','utf8'))});
call('submit',{checks:p.claims.map(c=>({claim_id:c.id,context_ids:['lore:core'],verdict:'supported',reason:'保留原事實與拒絕；來源證實',evidence:[{source_id:'m:0',quote:'艾迪送她草藥，她收下了。她拒絕同行。七峰共同管理渡口。'}]}))});
events.push({type:'agent_end'});writeFileSync(root+'/events.jsonl',events.map(e=>JSON.stringify(e)).join('\\n'));
"""
    subprocess.run(['node', '--input-type=module', '-e', script, str(root)], check=True, capture_output=True)
    if audit and operations:
        audit_root = receipt(root, {**frozen, 'review_operations':operations}, operations, audit=False)
        audit_root.rename(root/'audit')
    return root


def test_apply_all_kinds_preserves_history_and_undo_restores_bytes(story):
    before = tasks._raw(tasks._paths('s','main'))
    frozen = tasks.freeze('s', kinds=('memory','lore','summary'))
    result = tasks.apply(receipt(story, frozen, [MERGE, LORE, SUMMARY]))
    assert result['status'] == 'applied'
    messages = io._load_branch_messages('s','main')
    assert messages[0]['npcs_snapshot'][0]['memory'] == ['收到草藥。']
    assert messages[-1]['pre_npcs_snapshot'][0]['memory'] == ['收到草藥。']
    assert messages[-1]['npcs_snapshot'][0]['memory'] == ['收到艾迪送的草藥。','她拒絕同行。']
    assert messages[-1]['npcs_snapshot'][1]['memory'] == ['收到草藥。']
    assert [m['content'] for m in messages] == [m['content'] for m in json.loads(before['messages'])]
    entries = lore.available('s','main',messages)
    assert entries[0]['text'] == '作者設定'
    assert entries[1]['sources'] == [{'index':0,'hash':lore.content_hash(messages[0]['content'])}]
    assert compaction.load_recap('s','main')['compacted_through_index'] == 1
    assert tasks.undo(result['backup'])['status'] == 'undone'
    assert tasks._raw(tasks._paths('s','main')) == before


@pytest.mark.parametrize('key', ['messages','npcs','lore','summary','card','tree'])
def test_any_live_revision_change_rejects_without_overwriting(story,key):
    root = receipt(story, tasks.freeze('s'), [MERGE])
    paths = tasks._paths('s','main'); p=paths[key]; p.parent.mkdir(parents=True,exist_ok=True)
    # Whitespace alone is still a conservative version change, including absent files.
    p.write_bytes((p.read_bytes() if p.exists() else b'{}') + b'\n')
    before=tasks._raw(paths)
    with pytest.raises(ValueError,match='已改變'): tasks.apply(root)
    assert tasks._raw(paths)==before


def test_failed_second_write_recovers_every_original(story, monkeypatch):
    frozen = tasks.freeze('s',kinds=('memory','lore')); root=receipt(story,frozen,[MERGE,LORE])
    before=tasks._raw(tasks._paths('s','main')); original=tasks._write; writes=0
    def fail(path,data):
        nonlocal writes
        if path in tasks._paths('s','main').values():
            writes+=1
            if writes==2: raise OSError('disk fault')
        return original(path,data)
    monkeypatch.setattr(tasks,'_write',fail)
    with pytest.raises(OSError,match='disk fault'):tasks.apply(root)
    assert tasks._raw(tasks._paths('s','main'))==before


def test_undo_does_not_clobber_new_turn(story):
    result=tasks.apply(receipt(story,tasks.freeze('s'),[MERGE]))
    p=Path(io._story_messages_path('s','main')); p.write_bytes(p.read_bytes()+b'\n')
    before=tasks._raw(tasks._paths('s','main'))
    with pytest.raises(ValueError,match='玩家進度'):tasks.undo(result['backup'])
    assert tasks._raw(tasks._paths('s','main'))==before


def test_pending_recorder_rejects_freeze(story):
    p=io._story_messages_path('s','main'); messages=io._load_json(p,[])
    messages[-1]['card_record_status']='pending';io._save_json(p,messages)
    with pytest.raises(ValueError,match='記憶正在整理'):tasks.freeze('s')


def test_exact_existing_memory_addition_rejected(story):
    root=receipt(story,tasks.freeze('s'),[{**MERGE,'target_ids':[]}])
    with pytest.raises(ValueError,match='完全重複'):tasks.apply(root)


def test_tampered_receipt_cannot_write(story):
    root=receipt(story,tasks.freeze('s'),[MERGE]);p=root/'events.jsonl'
    text=p.read_text().replace('收到艾迪送的草藥。','她答應同行。',1);p.write_text(text)
    before=tasks._raw(tasks._paths('s','main'))
    with pytest.raises(ValueError):tasks.apply(root)
    assert tasks._raw(tasks._paths('s','main'))==before


def test_interrupted_batch_recovery_uses_before_or_after_hashes(story):
    before=tasks._raw(tasks._paths('s','main'))
    result=tasks.apply(receipt(story,tasks.freeze('s',kinds=('memory','lore')),[MERGE,LORE]))
    backup=Path(result['backup']); audit=json.loads((backup/'audit.json').read_text())
    # Simulate process termination after one write: other files are still original.
    audit['status']='prepared';(backup/'audit.json').write_text(json.dumps(audit))
    paths=tasks._paths('s','main');tasks._restore(paths['messages'],before['messages'])
    assert tasks.recover(backup)['status']=='recovered'
    assert tasks._raw(paths)==before


def test_background_recap_cannot_overwrite_pi_summary(story,monkeypatch):
    from story_core import llm_bridge
    frozen=tasks.freeze('s',kinds=('summary',))
    root=receipt(story,frozen,[SUMMARY])
    timeline=io._load_branch_messages('s','main')
    def model(_prompt):
        tasks.apply(root)
        return '過期的模型摘要'
    monkeypatch.setattr(llm_bridge,'call_oneshot',model)
    from story_core import usage_db
    monkeypatch.setattr(usage_db,'log_from_bridge',lambda *a,**k: None)
    compaction._run_compaction('s','main',timeline,expected_revision=None)
    assert compaction.load_recap('s','main')['recap_text']==SUMMARY['text']


def test_recap_commit_guard_excludes_other_process(story):
    import os
    code='''from story_core import compaction;import sys
compaction.STORIES_DIR=sys.argv[1]
with compaction.recap_commit_guard('s','main'): print('acquired',flush=True)
'''
    with compaction.recap_commit_guard('s','main'):
        p=subprocess.Popen([os.sys.executable,'-c',code,compaction.STORIES_DIR],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            with pytest.raises(subprocess.TimeoutExpired):p.communicate(timeout=.2)
        finally:
            pass
    out,err=p.communicate(timeout=3)
    assert p.returncode==0,err
    assert out.strip()=='acquired'


def test_independent_review_is_mandatory_and_bound_to_same_proposal(story):
    root=receipt(story,tasks.freeze('s'),[MERGE],audit=False)
    with pytest.raises(ValueError,match='獨立查證'):tasks.apply(root)
    frozen=json.loads((root/'snapshot.json').read_text())
    child=receipt(root,{**frozen,'review_operations':[MERGE]},[MERGE],audit=False);child.rename(root/'audit')
    p=root/'audit'/'snapshot.json';data=json.loads(p.read_text());data['review_operations'][0]['text']='改別的內容';p.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='未對應'):tasks.apply(root)


def test_model_timeout_is_fail_closed_and_report_is_bounded(story,monkeypatch):
    def timeout(*args,**kwargs):
        raise subprocess.TimeoutExpired('private huge prompt',1)
    monkeypatch.setattr(tasks,'_run_pi',timeout)
    result=tasks.run(tasks.freeze('s'),cli='/unused/pi',vertex={'project':'test','model':'test'},root=story,timeout=1)
    assert result['status']=='rejected'
    assert '超時' in result['error'] and 'private huge prompt' not in result['error']
    assert not (Path(io._story_dir('s'))/'pi_maintenance_backups').exists()


def test_summary_policy_does_not_force_second_session_but_durable_changes_do(story):
    frozen=tasks.freeze('s',kinds=('summary',))
    root=receipt(story,frozen,[SUMMARY],audit=False)
    assert tasks.verify(root)['all_supported']
    assert not tasks.needs_review(frozen,[SUMMARY])
    assert tasks.needs_review(frozen,[MERGE])
    assert tasks.needs_review(frozen,[LORE])
    assert tasks.needs_review({**frozen,'policy_version':1},[SUMMARY])


def test_usage_ledger_keeps_rejected_apply_and_updates_same_task(story):
    root=receipt(story,tasks.freeze('s'),[MERGE])
    result=tasks.apply(root)
    ledger=Path(io._story_dir('s'))/'pi_maintenance_usage/main'/f'{root.name}.json'
    report=json.loads(ledger.read_text())
    assert report['apply']['status']=='applied'
    with pytest.raises(ValueError):tasks.apply(root)
    report=json.loads(ledger.read_text())
    assert report['apply']['status']=='rejected'
    assert len(list(ledger.parent.glob('*.json')))==1
    assert report['ever_applied'] is True
    assert [a['status'] for a in report['apply_attempts']]==['applied','rejected']


def test_inventory_merge_delete_updates_latest_snapshot_and_undo(story):
    state = {'inventory': {'草藥': '一包', 'herb': '一包', '舊符': '已用完'}, 'other': 7}
    io._save_json(io._story_character_state_path('s','main'), state)
    messages=io._load_branch_messages('s','main')
    for m in messages:
        m['state_snapshot']=deepcopy(state);m['pre_state_snapshot']=deepcopy(state)
    io._save_json(io._story_messages_path('s','main'),messages)
    frozen=tasks.freeze('s',kinds=('inventory',))
    assert {'inv:0','inv:1','inv:2'} <= set(frozen['required_ids'])
    ops=[{'kind':'inventory','target_ids':['inv:0','inv:1'],'item_name':'草藥','text':'一包'},
         {'kind':'inventory','target_ids':['inv:2'],'item_name':'舊符','text':''}]
    before=tasks._raw(tasks._paths('s','main'))
    result=tasks.apply(receipt(story,frozen,ops))
    assert io._load_json(io._story_character_state_path('s','main'),{})['inventory']=={'草藥':'一包'}
    latest=io._load_branch_messages('s','main')[-1]
    assert latest['state_snapshot']['inventory']=={'草藥':'一包'}
    assert latest['pre_state_snapshot']==state
    assert tasks.undo(result['backup'])['status']=='undone'
    assert tasks._raw(tasks._paths('s','main'))==before


def test_inventory_stale_state_rejects(story):
    frozen=tasks.freeze('s')
    root=receipt(story,frozen,[MERGE])
    io._save_json(io._story_character_state_path('s','main'),{'inventory':{'新物':'1'}})
    with pytest.raises(ValueError,match='已改變'):
        tasks.apply(root)


def test_inventory_requires_independent_review(story):
    state={'inventory':{'草藥':'一包'}}
    io._save_json(io._story_character_state_path('s','main'),state)
    frozen=tasks.freeze('s',kinds=('inventory',))
    op={'kind':'inventory','target_ids':['inv:0'],'item_name':'草藥','text':'一包，已收下'}
    root=receipt(story,frozen,[op],audit=False)
    with pytest.raises(ValueError,match='獨立查證'):
        tasks.apply(root)


def test_apply_rebase_preserves_new_appended_turn_and_recorder_memory(story):
    frozen = tasks.freeze('s', kinds=('memory',))
    root = receipt(story, frozen, [MERGE])

    # Player plays another turn while Pi was running, and Recorder appends a new memory
    messages = io._load_branch_messages('s', 'main')
    npcs = io._load_json(io._story_npcs_path('s', 'main'), [])
    npcs[0]['memory'].append('玩家新獲得的記憶。')
    new_gm = deepcopy(messages[-1])
    new_gm.update(index=len(messages), content='玩家繼續遊玩後的回合。',
                  npcs_snapshot=deepcopy(npcs), pre_npcs_snapshot=deepcopy(messages[-1]['npcs_snapshot']))
    messages.append(new_gm)
    io._save_json(io._story_messages_path('s', 'main'), messages)
    io._save_json(io._story_npcs_path('s', 'main'), npcs)

    # Rebase apply succeeds and does not discard the appended memory or turn!
    result = tasks.apply(root, rebase=True)
    assert result['status'] == 'applied'
    after_npcs = io._load_json(io._story_npcs_path('s', 'main'), [])
    assert '玩家新獲得的記憶。' in after_npcs[0]['memory']
    assert '收到艾迪送的草藥。' in after_npcs[0]['memory']
    assert '收到草藥。' not in after_npcs[0]['memory']  # Replaced by MERGE


def test_apply_waits_for_busy_branch_lock(story):
    import threading, time
    from story_core.card_mode import branches
    frozen = tasks.freeze('s', kinds=('memory',))
    root = receipt(story, frozen, [MERGE])

    # Simulate another thread holding the branch lock temporarily
    lock_released = threading.Event()
    def hold_lock():
        with branches.lock('s', 'main'):
            time.sleep(0.15)
        lock_released.set()

    t = threading.Thread(target=hold_lock)
    t.start()
    time.sleep(0.02)

    # apply with wait=1 waits for the lock and succeeds
    result = tasks.apply(root, wait=1, poll=0.02)
    assert result['status'] == 'applied'
    t.join()
    assert lock_released.is_set()
