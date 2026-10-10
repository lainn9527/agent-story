import json
from story_core.card_mode import pi_usage as u


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(x) for x in rows))


def msg(response, usage=None, stop='toolUse'):
    return {'type':'message_end','message':{'role':'assistant','responseId':response,'model':'test',
            'stopReason':stop,'content':[{'type':'toolCall','name':'read'}],'usage':usage or {}}}


def test_account_actual_counters_without_double_counting_reasoning_or_cache(tmp_path):
    p=tmp_path/'events.jsonl'
    a=msg('one',{'input':100,'output':30,'cacheRead':40,'cacheWrite':0,'reasoning':20,'totalTokens':170,'cost':{'total':0}})
    b=msg('two',{'input':50,'output':20,'cacheRead':10,'cacheWrite':0,'reasoning':5,'totalTokens':80})
    write(p,[a,a,b])
    r=u.phase(p,'organize')
    assert r['visible_calls']==2
    assert r['tokens']['total_tokens']==250
    assert r['tokens']['reasoning_tokens']==25
    assert r['tokens']['cache_read_tokens']==50
    assert r['cost_usd'] is None
    assert r['calls'][0]['activity']=='lookup'


def test_missing_usage_partial_tail_and_error_zeros_are_unknown(tmp_path):
    p=tmp_path/'events.jsonl'
    write(p,[msg('one',{'input':8,'output':4,'totalTokens':12}),
             msg(None,{'input':0,'output':0,'totalTokens':0},'error'),
             {'type':'message_start','message':{'role':'assistant'}}])
    with p.open('a') as f:f.write('\n{"type":')
    r=u.phase(p,'organize')
    assert r['visible_calls']==3
    assert r['tokens']['total_tokens'] is None
    assert r['known_tokens']['total_tokens']==12
    assert r['calls_missing_usage']['total_tokens']==2
    assert r['tokens']['reasoning_tokens'] is None


def test_aggregate_both_stages_and_rejected_checks(tmp_path):
    write(tmp_path/'events.jsonl',[msg('one',{'totalTokens':10}),{'type':'tool_execution_end','result':{'content':[{'type':'text','text':json.dumps({'operations':[{'kind':'memory','target_ids':['a','b']}]})}]}}])
    write(tmp_path/'audit'/'events.jsonl',[msg('two',{'totalTokens':20}),{'type':'tool_execution_end','result':{'content':[{'type':'text','text':json.dumps({'checks':[{'verdict':'conflict','reason':'部分不是全部'}]})}]}}])
    frozen={'slug':'s','branch_id':'main','kinds':['memory'],'focus':[],'required_ids':[]}
    r=u.collect(tmp_path,frozen,{'status':'rejected','model':'test'},1234)
    assert r['tokens']['total_tokens']==30 and r['visible_calls']==2
    assert r['proposed']['merge']==1
    assert r['phases'][1]['rejected_checks'][0]['reason']=='部分不是全部'
    assert r['elapsed_ms']==1234


def test_no_trace_is_not_free_and_reported_zero_is_distinct(tmp_path):
    assert u.phase(tmp_path/'missing','organize')['tokens']['total_tokens'] is None
    p=tmp_path/'events.jsonl'
    write(p,[msg('reported',{'input':0,'output':0,'totalTokens':0})])
    assert u.phase(p,'organize')['tokens']['total_tokens']==0
