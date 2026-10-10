"""Paid opt-in natural-play audit. Synthetic saves only; immutable per-run reports."""
import argparse
import copy
import hashlib
import json
import logging
import sys
import shutil
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from story_core import story_io as io, usage_db
from story_core.card_mode import card, ensemble as e


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', required=True)
    parser.add_argument('--output-dir', default='/tmp')
    parser.add_argument('--pace', type=float, default=30)
    parser.add_argument('--attempts', type=int, default=3)
    parser.add_argument('--resume-report')
    parser.add_argument('--after-round', type=int, default=0)
    args = parser.parse_args()
    if not args.run.replace('-', '').isalnum(): raise ValueError('Invalid run name')
    report_path = Path(args.output_dir) / f'card-ensemble-natural-{args.run}.json'
    dialogue_path = report_path.with_suffix('.md')
    if report_path.exists() or dialogue_path.exists(): raise ValueError('Never overwrite an audit run')
    case_path = ROOT / 'tests/fixtures/ensemble_natural.json'
    case = json.loads(case_path.read_text())
    if args.after_round and not args.resume_report:raise ValueError('Rewind requires a saved report')
    if not 0<=args.after_round<len(case['turns']):raise ValueError('Invalid rewind round')
    sandbox = Path(tempfile.mkdtemp(prefix='ensemble-natural-'))
    operations = []
    if args.resume_report:
        parent = json.loads(Path(args.resume_report).read_text())
        if parent['case_sha256'] != hashlib.sha256(case_path.read_bytes()).hexdigest():
            raise ValueError('Cannot change the frozen test case')
        origin = Path(parent['sandbox']).resolve()
        if not origin.name.startswith('ensemble-natural-') or not origin.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise ValueError('Only isolated audit saves may be copied')
        shutil.copytree(origin,sandbox,dirs_exist_ok=True)
    io.STORIES_DIR = usage_db.STORIES_DIR = str(sandbox / 'stories')
    io.STORIES_REGISTRY_PATH = str(sandbox / 'stories.json')
    card.STORY_DESIGN_DIR = str(sandbox / 'design')
    usage_db._initialized = set()
    c = {'schema_version':1,'id':'synthetic','display_name':'李小蘭','pitch':'成年大學同學籌備攝影展。',
         'player':{'role':'艾迪，20 歲大學生','address':'你'},
         'setting':{'place':'大學咖啡館','start_datetime':'2026-09-22T13:30:00','weather_pool':['晴']},
         'persona':{'identity':'20 歲大學生','personality':'親切自律，做事可靠，有自己的課業安排，喜歡輕巧的玩笑。',
                    'speech_style':'清楚溫和，不替別人决定。','body_details':['黑色長直髮'],'verbal_tics':['我想想。']},
         'opening':'📅 2026/09/22　⏰ 13:30　📌 大學咖啡館　🌤️ 晴\n小蘭剛答應週五替你校對攝影展講義，正在咖啡館桌邊整理初稿。',
         'start':{'relationship':{'tier':'友好','note':'認識兩年的同學，常一起討論課業'},'inventory':{'相機':'艾迪保管'}},
         'output':{'length_hint':'200-400'}}
    if args.resume_report:
        target_index = e.load('natural')['baseline_index']+2*args.after_round
        e.mutate('natural','rewind',{'to_index':target_index})
        operations.append({'operation':'copy isolated save, then normal rewind API',
                           'source_report':args.resume_report,'to_index':target_index})
    else:
        card.init_card_story(c)
        e.clone('synthetic','natural',{'name':'沈晚','identity':'20 歲大學生','personality':'直率愛吐槽，熱心攝影，有自己的社團安排。',
            'speech_style':'輕快直白，熟人間愛開玩笑。','memories':['沈晚答應週五帶藍色相簿到攝影展。','沈晚今天下午要出席社團會議。'],
            'tier':'相識','note':'攝影社認識的朋友','presence':'offstage','status':'在圖書館的照片整理桌整理照片'})
    from story_core.llm_bridge import get_provider, _get_config
    provider = get_provider()
    report = {'provider':provider,'model':_get_config().get(provider,{}).get('model'),'case':case,'case_sha256':hashlib.sha256(case_path.read_bytes()).hexdigest(),
              'implementation_sha256':hashlib.sha256(Path(e.__file__).read_bytes()).hexdigest(),
              'sandbox':str(sandbox),'initial':e.load('natural'),'turns':[],'ui_operations':operations}
    def persist():
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        dialogue_path.write_text('\n\n'.join('**玩家**\n\n'+r['input']+'\n\n**角色回覆**\n\n'+r.get('response','（此回合未完成）') for r in report['turns']))
    persist()
    for n,user in enumerate(case['turns'],1):
        if n<=args.after_round:continue
        entry={'round':n,'input':user,'before':copy.deepcopy(e.load('natural')['state'])}
        report['turns'].append(entry);persist()
        def recorder(system, text):
            updates = e.record(system, text)
            attempt_record['recorder_updates'] = updates
            persist()
            return updates
        def gm(user, system, history):
            call = {'system_prompt':system,'history':history,'user_message':user}
            attempt_record.setdefault('narrator_calls',[]).append(call)
            persist()
            response = e.generate(user, system, history)
            call['narration'] = response
            attempt_record['narration'] = response
            persist()
            return response
        entry['attempts'] = []
        completed = False
        for attempt in range(1, args.attempts + 1):
            attempt_record = {'attempt':attempt}
            entry['attempts'].append(attempt_record)
            persist()
            try:
                result=e.mutate('natural','send',{'message':user,'response_length':'short','recall_mode':'off'},generate_fn=gm,record_fn=recorder,async_recorder=False)
                entry.update(response=result['narration'],after=copy.deepcopy(e.load('natural')['state']))
                attempt_record['outcome'] = 'committed'
                attempt_record['recorder'] = e.public_state('natural')['recorder']
                completed = True
                persist();print(f'Round {n}: saved',flush=True)
                break
            except Exception as err:
                attempt_record.update(error=f'{type(err).__name__}: {err}',after=copy.deepcopy(e.load('natural')['state']))
                persist(); print(f'Round {n} attempt {attempt}: ERROR {err}',flush=True)
                if attempt < args.attempts:
                    report['ui_operations'].append({'round':n,'operation':'retry same input after failed uncommitted turn','attempt':attempt+1})
                    time.sleep(max(30, args.pace))
        if not completed:break
        if n < len(case['turns']):time.sleep(args.pace)
    print(report_path,flush=True)

if __name__=='__main__':main()
