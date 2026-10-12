"""Exercise real rewind API after a verified Pi commit; no model calls."""
from copy import deepcopy
from tests.test_card_ensemble import world
from tests.test_pi_maintenance import receipt, MERGE, SUMMARY
from story_core import story_io as io, compaction
from story_core.card_mode import pi_maintenance as tasks, lore, pi_schedule
import pytest


@pytest.mark.parametrize('target', [0, 2, 12])
def test_four_kinds_after_real_rewind(world, monkeypatch, target):
    root, client, _ = world
    monkeypatch.setattr(compaction,'STORIES_DIR',io.STORIES_DIR)
    monkeypatch.setattr(io,'STORY_DESIGN_DIR',str(root/'design'))
    slug='original'; bid='main'
    initial=io._load_branch_messages(slug,bid)[0]
    npc=deepcopy(initial['npcs_snapshot'])
    npc[0]['memory']=['收到草藥。','收到艾迪送的草藥。']
    state={'inventory':{'草藥':'一包','herb':'一包'}}
    rows=[]
    source='艾迪送她草藥，她收下了。她拒絕同行。七峰共同管理渡口。'
    for i in range(13):
        m=deepcopy(initial)
        m.update(index=i,role='gm' if i%2==0 else 'user',content=source if i%2==0 else '繼續',
                 state_snapshot=deepcopy(state),npcs_snapshot=deepcopy(npc),card_record_status='done',card_turn_id=f't{i}')
        rows.append(m)
    io._save_json(io._story_messages_path(slug,bid),rows)
    io._save_json(io._story_npcs_path(slug,bid),npc)
    io._save_json(io._story_character_state_path(slug,bid),state)
    io._save_json(str(lore.path(slug,bid)),{'schema_version':1,'entries':[
        {'id':'core','text':'作者設定','always':True},
        {'id':'port','text':'舊渡口','sources':[{'index':0,'hash':lore.content_hash(source)}]}]})
    cli=root/'pi';cli.write_text('test')
    pi_schedule.configure(slug,bid,cli=str(cli))
    frozen=tasks.freeze(slug,kinds=('memory','inventory','lore','summary'))
    ops=[MERGE, SUMMARY,
         {'kind':'inventory','target_ids':['inv:0','inv:1'],'item_name':'草藥','text':'一包'},
         {'kind':'lore','target_ids':['lore:port'],'title':'渡口','keywords':['渡口'],'category':'place','text':'七峰共同管理渡口。'}]
    tasks.apply(receipt(root,frozen,ops))
    response=client.post('/api/card/original/rewind',json={'to_index':target,'branch_id':'main'})
    assert response.status_code==200,response.json
    kept=io._load_branch_messages(slug,bid)
    expected_clean=target==12
    assert io._load_json(io._story_character_state_path(slug,bid),{})['inventory']==({'草藥':'一包'} if expected_clean else state['inventory'])
    assert io._load_json(io._story_npcs_path(slug,bid),[])[0]['memory']==([MERGE['text']] if expected_clean else npc[0]['memory'])
    available=lore.available(slug,bid,kept)
    assert next(e for e in available if e['id']=='port')['text']==('七峰共同管理渡口。' if expected_clean else '舊渡口')
    recap=compaction.load_recap(slug,bid)
    # Summary through #2 is still valid at #2; earlier rollback must clear it.
    assert bool(recap.get('recap_text')) == (target>=2)
    ledger=pi_schedule._read(pi_schedule.ledger_path(slug,bid))
    assert ledger['last_attempt_pairs']==target//2
    # Same index with different narration must not revive a discarded lore revision.
    if target==12:
        kept[-1]['content']='另一候選'
        assert next(e for e in lore.available(slug,bid,kept) if e['id']=='port')['text']=='舊渡口'
    else:
        # Real fork inherits only lore that is valid in the retained timeline.
        child=client.post('/api/card/original/branches',json={'action':'fork','branch_id':'main','from_index':target,'name':'回退後'})
        assert child.status_code==200,child.json
        child_id=child.json['branch_id']
        assert next(e for e in lore.available(slug,child_id,kept) if e['id']=='port')['text']=='舊渡口'
