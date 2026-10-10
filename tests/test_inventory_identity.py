from copy import deepcopy
from story_core.card_mode.ops import apply_card_tool, card_tool_declarations


def test_existing_names_only_and_atomic_retry():
    state={'inventory':{'定心丹':'十二枚'}};npcs=[{'name':'小蘭'}];before=deepcopy(state)
    result=apply_card_tool(state,npcs,{},'set_inventory',{'items':{'定心丹':'十一枚','calming_pill':'十一枚'}})
    assert not result['ok'] and state==before
    assert apply_card_tool(state,npcs,{},'set_inventory',{'items':{'定心丹':'十一枚'}})['ok']
    assert state['inventory']=={'定心丹':'十一枚'}
    assert apply_card_tool(state,npcs,{},'set_inventory',{'items':{'定心丹':''}})['ok']
    assert state['inventory']=={}


def test_add_requires_chinese_and_new_name():
    state={'inventory':{'鋼劍':'一柄'}};npcs=[{'name':'小蘭'}]
    for name in ['steel_sword','鋼劍']:
        assert not apply_card_tool(state,npcs,{},'add_inventory',{'items':{name:'一柄'}})['ok']
    assert apply_card_tool(state,npcs,{},'add_inventory',{'items':{'任務玉簡':'一枚'}})['ok']
    assert len(state['inventory'])==2
    assert {'add_inventory','set_inventory'} <= {t['name'] for t in card_tool_declarations()}
