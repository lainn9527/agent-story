"""Opt-in real-model recall audit over fixed synthetic history; never player saves."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from story_core.card_mode import recall
from story_core import usage_db, llm_bridge


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run',required=True)
    parser.add_argument('--live',action='store_true',help='Explicitly use the configured paid model')
    args=parser.parse_args()
    if not args.live: parser.error('--live is required')
    if not args.run.replace('-','').isalnum(): parser.error('Invalid run name')
    out=Path('/tmp')/f'card-recall-{args.run}.json'
    dialogue=out.with_suffix('.md')
    if out.exists() or dialogue.exists(): raise ValueError('Do not overwrite audit evidence')
    source=ROOT/'tests/fixtures/card_recall.json'
    case=json.loads(source.read_text())
    # Push actual episodes outside the recent window without changing them.
    history=case['history']+[{'role':'gm','index':i,'content':f'第{i}次課後，小蘭和你簡單談課業。'} for i in range(5,125)]
    usage_db.STORIES_DIR=tempfile.mkdtemp(prefix='card-recall-usage-')
    usage_db._initialized=set()
    report={'case_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'implementation_sha256':hashlib.sha256(Path(recall.__file__).read_bytes()).hexdigest(),
            'provider':llm_bridge.get_provider(),'history':history,'turns':[]}
    def persist():
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        dialogue.write_text('\n\n---\n\n'.join('**玩家**\n\n'+t['input']+'\n\n**小蘭**\n\n'+t.get('response','（這輪未完成）') for t in report['turns']))
    for c in case['cases']:
        entry=dict(c);report['turns'].append(entry);persist()
        actions=[]
        def ask(system,context):
            call={'context':json.loads(context)};actions.append(call)
            entry['agent_calls']=actions;persist()
            raw=recall._ask('synthetic','main',system,context)
            call['response']=raw;persist()
            return raw
        try:
            evidence=recall.collect('synthetic','main',c['mode'],c['input'],history,actor='李小蘭',ask_fn=ask)
            entry['recall']=evidence;persist()
            system=('扮演李小蘭，成年大學生，親切自律、說話清楚溫和。對方是認識一段時間的成年同學艾迪。'
                    '目前在校園咖啡館，關係友好。以自然台詞與少量動作回覆，約150至250字。'
                    '不替玩家決定行動，不補造過去；不知道的事坦白不確定。')+recall.grounding(evidence)
            # The narrator sees only the recent window plus retrieved evidence.
            from story_core.card_mode.ensemble import _metered_call
            response,_=_metered_call('synthetic','recall_audit_gm',llm_bridge.call_claude_gm,
                                     c['input'],system,history[-3:])
            if not response or response.startswith(('【系統錯誤】','【系統提示】')):raise ValueError('Narration failed')
            entry['response']=response
            entry['status']='completed; manual review required'
        except Exception as error:
            entry['status']='failed';entry['error']=f'{type(error).__name__}: {error}'
        persist();print(c['id'],entry['status'],flush=True)
    print(out,flush=True)

if __name__=='__main__':main()
