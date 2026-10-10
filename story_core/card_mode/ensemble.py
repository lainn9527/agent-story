"""Opt-in ensemble copies. One atomic document, explicit per-character knowledge.

Legacy cards never enter this code path. Narration commits immediately; ordered
background recording rebuilds retained snapshots under the same document lock.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import json
import re
from pathlib import Path
import shutil
import time
import uuid

from story_core import story_io as io
from story_core.card_mode.card import load_card, validate_card, card_path
from story_core.card_mode.clock import parse_header_time
from story_core.card_mode.prompt import build_card_system_prompt, RESPONSE_LENGTHS
from story_core.card_mode import styles, candidates, instructions
from story_core.card_mode.ops import gate_relationship, gate_remember
from story_core.card_mode import recall
from story_core.gm_v2 import relationship as rel


class Busy(Exception):
    pass


def path(slug, branch_id="main"):
    if not io._is_valid_story_id(slug):
        raise ValueError('Invalid card ID')
    if not isinstance(branch_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', branch_id):
        raise ValueError('Invalid branch ID')
    root = Path(io.STORIES_DIR) / slug
    return root / 'ensemble.json' if branch_id == 'main' else root / 'branches' / branch_id / 'ensemble.json' 


def enabled(slug):
    return io._is_valid_story_id(slug) and path(slug).is_file()


@contextmanager
def lock(slug, blocking=False, branch_id="main"):
    p = path(slug, branch_id).parent
    if not p.is_dir():
        raise ValueError('Card not found')
    with open(p / '.ensemble.lock', 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Busy('角色回合處理中，請等完成後再切換或操作。')
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load(slug, branch_id="main"):
    return io._load_json(str(path(slug, branch_id)), {})


def save(slug, doc, branch_id="main"):
    io._save_json(str(path(slug, branch_id)), doc)


def character(spec):
    if not isinstance(spec, dict):
        raise ValueError('角色資料必須是物件')
    def text(key, required=False):
        val = spec.get(key, '')
        if not isinstance(val, str) or len(val) > 8000 or (required and not val.strip()):
            raise ValueError('請填寫有效的 ' + key)
        return val.strip()
    name, personality = text('name', True), text('personality', True)
    memories = spec.get('memories', [])
    if not isinstance(memories, list) or len(memories) > 100 or any(not isinstance(x, str) or len(x) > 500 for x in memories):
        raise ValueError('已知經歷需為文字清單，每條最多 500 字')
    presence = spec.get('presence', 'offstage')
    if presence not in ('present', 'offstage', 'remote'):
        raise ValueError('Invalid presence')
    tier = spec.get('tier', '相識')
    if not rel.is_valid_tier(tier):
        raise ValueError('Invalid relationship tier')
    cid = 'c_' + uuid.uuid4().hex[:10]
    return cid, {
        'name': name, 'persona': {'identity': text('identity') or name,
        'personality': personality, 'speech_style': text('speech_style') or '自然說話，保持自身個性',
        'appearance': text('appearance'), 'body_details': [], 'verbal_tics': []},
        'memory': [s.strip() for s in memories if s.strip()],
        'relationship': {'tier': tier, 'note': text('note')},
        'presence': presence, 'current_status': text('status'),
    }


def clone(source, target, spec, label='', branch_id='main'):
    """Publish an independent copy; never initialize or mutate the source."""
    path(source, branch_id); path(target)
    if not isinstance(label, str) or len(label) > 100:
        raise ValueError('副本名稱最多 100 字')
    if enabled(source):
        raise ValueError('已是多角色副本；可直接新增角色')
    card = load_card(source)
    from story_core.card_mode import galgame
    if galgame.enabled(card):
        raise ValueError("Galgame 第一版只支援單女角，暫不能建立多角色副本。")
    cid, companion = character(spec)
    src = Path(io.STORIES_DIR) / source / 'branches' / branch_id
    names = ['messages.json', 'npcs.json', 'character_state.json', 'card_clock.json', 'conversation_recap.json']
    blobs = {n: (src/n).read_bytes() if (src/n).exists() else b'{}' for n in names}
    vals = {n: json.loads(v) for n,v in blobs.items()}
    messages = vals['messages.json']; npcs = vals['npcs.json']
    if not isinstance(messages, list) or not messages or messages[-1].get('role') != 'gm':
        raise ValueError('請等來源回合完成後再建立副本')
    original = npcs[0] if npcs else {}
    if companion['name'] == card['display_name']:
        raise ValueError('角色名稱不可重複')
    clock = vals['card_clock.json']
    # Recover from the already captured history, never a second live-file read.
    if not clock.get('last_location'):
        for message in reversed(messages):
            if message.get('role') == 'gm':
                header = parse_header_time(message.get('content', ''))
                if header and header.get('location'):
                    clock['last_location'] = header['location']
                    break
    dt = datetime.fromisoformat(clock.get('start_iso', card['setting']['start_datetime'])) + timedelta(minutes=clock.get('elapsed_minutes', 0))
    state = {'focus': 'lead', 'characters': {'lead': {
        'name': card['display_name'], 'persona': deepcopy(card['persona']),
        'memory': deepcopy(original.get('memory', [])),
        'relationship': deepcopy(original.get('relationship', card['start']['relationship'])),
        'presence': 'present', 'current_status': original.get('current_status', '')}, cid: companion},
        'inventory': vals['character_state.json'].get('inventory', {}),
        'datetime': dt.isoformat(), 'location': clock.get('last_location') or card['setting']['place'],
        'weather': clock.get('last_weather', '')}
    history = [{k: x[k] for k in ('role','content','index') if k in x} for x in messages]
    baseline = history[-1]['index']; history[-1]['ensemble_snapshot'] = deepcopy(state)
    doc = {'version': 1, 'baseline_index': baseline, 'state': state,
           'initial_state': deepcopy(state), 'messages': history,
           'source': source, 'source_branch_id': branch_id, 'source_recap': vals['conversation_recap.json']}
    target_dir = path(target).parent; design_dir = Path(card_path(target)).parent
    if target_dir.exists() or design_dir.exists():
        raise ValueError('副本 ID 已存在')
    registry_path = Path(io.STORIES_REGISTRY_PATH)
    # Serialize registry edits made by this feature across workers.
    with open(registry_path.with_suffix('.ensemble.lock'), 'a') as reglock:
        fcntl.flock(reglock, fcntl.LOCK_EX)
        registry = io._load_stories_registry()
        if target in registry.get('stories', {}):
            raise ValueError('副本 ID 已存在')
        if any((src/n).exists() and (src/n).read_bytes() != v for n,v in blobs.items()):
            raise Busy('來源資料剛更新，請重試')
        target_dir.mkdir(parents=True, exist_ok=False)
        try:
            design_dir.mkdir(parents=True, exist_ok=False)
            copied = deepcopy(card); copied['id'] = target
            copied['tagline'] = '多角色共用時間線'
            assert not validate_card(copied)
            io._save_json(str(design_dir/'card.json'), copied)
            save(target, doc)
            now = datetime.now(timezone.utc).isoformat()
            registry.setdefault('stories', {})[target] = {
                'id': target, 'mode': 'card', 'name': label or card['display_name']+'・多角色',
                'tagline': copied['tagline'], 'pitch': card['pitch'], 'created_at': now, 'updated_at': now}
            io._save_stories_registry(registry)
        except Exception:
            shutil.rmtree(target_dir)
            if design_dir.exists(): shutil.rmtree(design_dir)
            raise
    return target


def public_state(slug, doc=None, branch_id="main"):
    doc = doc or load(slug, branch_id); s = doc['state']; char = s['characters'][s['focus']]
    card = deepcopy(load_card(slug)); card['display_name'] = char['name']; card['persona'] = char['persona']
    from story_core.card_mode.ensemble_recorder import status
    candidate_summaries = candidates.summaries(slug, branch_id, doc['messages'])
    feedback_views = candidates.feedback_views(slug, branch_id, doc['messages'])
    return {'ok': True, 'card': card, 'ensemble': True, 'focus': s['focus'],
        'recorder': status(doc),
        'baseline_index': doc['baseline_index'],
        'cast': [{'id': k, 'name': v['name'], 'presence': v['presence'],
                  'status': v['current_status'], 'relationship': v['relationship'],
                  'memory_count': len(v['memory'])} for k,v in s['characters'].items()],
        'relationship': char['relationship'], 'relationship_display': rel.display(char['relationship']),
        'relationship_tiers': rel.TIERS, 'memories': char['memory'], 'inventory': s['inventory'],
        'persistent_instruction':instructions.read(slug,branch_id),
        'clock': {'time_str': datetime.fromisoformat(s['datetime']).strftime('%Y/%m/%d %H:%M'),
                  'location': s['location'], 'weather': s['weather']},
        'messages': [{**{k: m[k] for k in ('role','content','index')},
                      'feedback':feedback_views.get(str(m['index'])),
                      'candidate_summary':candidate_summaries.get(str(m['index']))} for m in doc['messages']]}


def _active_characters(state):
    return {cid for cid, ch in state['characters'].items() if ch['presence'] in ('present', 'remote')}


def _turn_visibility(before, updates, user, narration):
    """Original-text offsets witnessed by each actor, including mid-turn arrivals."""
    text = '玩家：'+user+'\n敘事：'+narration
    sections = {'user':(3,3+len(user)), 'gm':(3+len(user)+4,len(text))}
    events = {u['character_id']:u.get('scene_events',[]) for u in updates}
    result = {}
    for cid, ch in before['characters'].items():
        transitions = sorted((text.index(ev['evidence'])+len(ev['evidence']),ev['presence'])
                             for ev in events.get(cid,[]))
        cursor = 0; present = ch['presence']=='present'; spans = []
        for boundary, presence in transitions:
            if present and boundary > cursor: spans.append((cursor,boundary))
            cursor = boundary; present = presence=='present'
        if present and cursor<len(text):spans.append((cursor,len(text)))
        result[cid] = {}
        for role,(start,end) in sections.items():
            result[cid][role] = [[max(a,start)-start,min(b,end)-start] for a,b in spans
                                if max(a,start)<min(b,end)]
    return result


def authorized_history(doc, actor, include_compacted=True):
    """Authorized original fragments, with Python-character offsets (end exclusive).

    Never returns hidden text. Each fragment retains the original message index;
    callers must not expand its start/end boundaries when retrieving context.
    """
    if actor not in doc['state']['characters']:return []
    recap = doc.get('source_recap', {})
    covered = (recap.get('compacted_through_index', -1)
               if not include_compacted and recap.get('recap_text') else -1)
    history = []
    for i, message in enumerate(doc['messages']):
        if message['role'] != 'gm':continue
        previous = doc['messages'][i-1] if i and doc['messages'][i-1]['role']=='user' else None
        if 'ensemble_visibility' in message:
            visibility = message['ensemble_visibility']
            ranges = visibility.get(actor,{}) if isinstance(visibility,dict) else {}
            if not isinstance(ranges,dict):ranges = {}
        else:
            ranges = None
        if ranges is None:
            witnessed = set(message.get('ensemble_audience', []))
            if message['index'] <= doc['baseline_index']:witnessed = {'lead'}
            if actor not in witnessed:continue
        for position, item in ((i-1,previous),(i,message)):
            if item is None or position<=covered:continue
            content = item['content']
            spans = ranges.get(item['role'],[]) if ranges is not None else [[0,len(content)]]
            if not isinstance(spans,list):continue
            for span in spans:
                if (not isinstance(span,(list,tuple)) or len(span)!=2
                        or any(type(value) is not int for value in span)):continue
                start,end = span
                if not (0<=start<end<=len(content)):continue
                fragment = content[start:end]
                if fragment.strip():
                    history.append({'index':item['index'],'role':item['role'],'content':fragment,
                                    'start':start,'end':end,'source_length':len(content)})
    return history


def visible_history(doc):
    """Normal narration uses the same authorization as deep recall, without compacted prose."""
    audience = {doc['narration_actor']} if doc.get('narration_actor') else _active_characters(doc['state'])
    if len(audience)!=1:return []
    fragments = authorized_history(doc,next(iter(audience)),include_compacted=False)
    history = []; last = None
    for fragment in fragments:
        key = (fragment['index'],fragment['role'])
        if key==last:
            history[-1]['content'] += '\n（未參與片段已省略）\n'+fragment['content']
        else:
            history.append({'role':fragment['role'],'content':fragment['content']})
        last = key
    return history


def prompt(slug, doc, user, length=None):
    from story_core.card_mode.memory import select_memories
    s = doc['state']
    card = deepcopy(load_card(slug))
    card['display_name'] = '多角色共同場景'
    card['persona'] = {'identity': '統一敘事者，依各自人設演出當前場景中的角色',
                       'personality': '每個角色獨立，詳見下方角色資料',
                       'speech_style': '依各角色風格，一次完成自然連貫的互動'}
    base = build_card_system_prompt(card, s, {'memory': [], 'relationship': {}},
        datetime.fromisoformat(s['datetime']), last_weather=s['weather'],
        current_location=s['location'], response_length=length)
    recap = doc.get('source_recap', {}).get('recap_text', '')
    roster = deepcopy(s['characters'])
    for ch in roster.values():
        ch.get('persona', {}).pop('verbal_tics', None)
        ch['memory'] = select_memories(ch['memory'], user)
    base += ('\n一次寫完整場景，讓實際被接觸的人自然回應；不必輪流點名每個角色。'
             '角色資料是最近完成記錄時的狀態；若與最近原文衝突，以原文已發生的事件為準。'
             '背景記錄尚未完成不代表剛剛的對話、移動或約定沒有發生。')
    base += '\n# 副本建立前的歷史摘要（導演背景，不是角色共同知識）\n' + recap
    return base + '\n\n# 多角色同一時間線（優先適用）\n' + (
        '焦點只決定主要互動對象，不改變其他人的性格、關係或所在位置。'
        '以下資料是導演可見，不代表每個角色都知道。每人只能使用自己的 memory 與當場聽見的內容；'
        '出場狀態表示回合開始時的場景，需承接玩家本回合實際移動與敘事中已發生的進出場。'
        '玩家離開時，未明示同行的人留在原地；走到另一人面前可開始當面互動。'
        '邀約或計畫不等於已經移動，電話邀請不等於已抵達。'
        'offstage 不能隔空插話或得知別處私下對話；remote 只經由明示通訊得知內容。'
        '焦點只是互動偏好；玩家實際接觸或詢問另一人時讓對方回應，不強迫原焦點跟隨或發言。'
        '過往對話、摘要與其他人的記憶不是共同知識。疑問與推測不是已知事實。'
        '歷史事實必須有對話或該角色記憶依據：不得補造既往行動、私訊、群組對話、承諾、共同往事或玩家過失。'
        '不能為了熟悉感、打趣或串場，聲稱某人先前做了資料未記載的事；未知就保持未知。'
        '可以演出眼前的新動作、提議與感受，不可倒填場外已發生的事情。'
        '若本回合換場，時間地點標頭使用回合結束時玩家所在的場景。'
        '維持每個人的穩定人設，不因成為配角就簡化成阻礙或工具人。'
        '此回合可由在場配角自然回應，明確區分說話者，不替玩家決定行動。\n'
        '焦點 ID: '+s['focus']+'\n角色資料:\n'+json.dumps(roster, ensure_ascii=False))


RECORD_PROMPT = '''你是多角色事實記錄員。只回傳 JSON {"updates": [...]}。
每筆更新 {"character_id": "...", "memories": [{"fact":"...", "evidence":"本回合原文的逐字片段"}],
"relationship": {"tier":"相識", "note":"具體關係", "evidence":"原文片段"},
"status": {"value":"目前狀態", "evidence":"原文片段"},
"scene_events": [{"presence":"present/offstage/remote", "evidence":"實際進出場的原文片段"}]}。
沒有變化就省略欄位；每位角色最多一筆 updates，場景事件放在該筆 scene_events 陣列。
先依時間順序判讀每人進出場，再記錄她真正能聽見的消息。presence 是相對玩家當下場景，非絕對存在。
玩家走到別處時，未同行的人變 offstage；玩家實際抵達某人面前，對方變 present。
角色離開、掛電話亦更新；不要把邀請、計畫、說要離開等尚未發生的事當成已移動。
每個 scene_event 引用至少四字、在本回合僅出現一次的逐字 evidence，引用完整移動動作，不能只引名字。
場景事件須覆蓋所有受影響者，包含非焦點角色。不得把切焦點當成出入場。
玩家原文已完成的移動以玩家原文為切換依據，不改用敘事較晚的重述，否則會漏記移動之後玩家說的消息。
場景切換前後的知情範圍分開：剛入場的人不能補聽之前私話；已離場的人仍保留離場前公開得知的事。
只記角色當時 present 或明示 remote 接收到的內容；offstage 期間的消息不得更新。記錄該角色實際得知的事，
引用 evidence 必須能支持該角色在場聽見、被告知或經歷的事，不把其他人的私事轉給她。
逐一檢查每位在場角色：本回合首次得知的重要消息、承諾和分工，都要寫入該人的 memories。
公開的重要消息應分別記給每位實際聽見的角色，不可只記焦點角色。
status 是之後會被覆蓋的暫時近況，不能代替長期 memories；同一事實必要時兩者皆記。
evidence 從本回合玩家或敘事逐字複製，不能改字、補姓名或用省略號，fact 則用完整第三人稱。
不要記旁白的心理定論、隱藏動機、暫態情緒。結束約定不等於變陌生；保持既有認識與共同經歷。
關係階梯：敵對、戒備、陌生、相識、友好、信任、摯友。不得修改人設或切換焦點。
'''


def apply_updates(state, updates, evidence_text):
    if not isinstance(updates, list) or len(updates) > 24:
        raise ValueError('Invalid recorder updates')
    def supported(item):
        return isinstance(item, dict) and isinstance(item.get('evidence'), str) and len(item['evidence'].strip()) >= 2 and item['evidence'] in evidence_text
    seen = set()
    for u in updates:
        if not isinstance(u, dict): raise ValueError('Invalid character update')
        cid = u.get('character_id')
        ch = state['characters'].get(cid)
        if ch is None: raise ValueError('Unknown character ID')
        if cid in seen: raise ValueError('Duplicate character update')
        seen.add(cid)
        events = u.get('scene_events', [])
        if not isinstance(events, list) or len(events) > 12:
            raise ValueError('Invalid scene events')
        transitions = []
        for event in events:
            if (not supported(event) or len(event['evidence'].strip()) < 4
                    or evidence_text.count(event['evidence']) != 1
                    or event.get('presence') not in ('present', 'offstage', 'remote')):
                raise ValueError('出入場記錄缺少明確依據，這回合未寫入，請重試。')
            # Transition takes effect after the quoted action is completed.
            boundary = evidence_text.index(event['evidence']) + len(event['evidence'])
            transitions.append((boundary, event['presence']))
        unique = {}
        for boundary, value in transitions:
            if boundary in unique and unique[boundary] != value:
                raise ValueError('出入場記錄互相矛盾，這回合未寫入，請重試。')
            unique[boundary] = value
        transitions = sorted(unique.items())
        initial_presence = ch['presence']

        def heard(item):
            if not supported(item): return False
            start = evidence_text.index(item['evidence'])
            end = start + len(item['evidence'])
            presence = initial_presence
            for boundary, value in transitions:
                if boundary <= start:
                    presence = value
                elif boundary < end and value == 'offstage':
                    return False
            return presence in ('present', 'remote')

        for fact in u.get('memories', []):
            if heard(fact) and isinstance(fact.get('fact'), str):
                clean,_ = gate_remember([fact['fact']], ch['memory']); ch['memory'].extend(clean)
        relation = u.get('relationship')
        if heard(relation):
            clean,err = gate_relationship(relation.get('tier'), relation.get('note'), ch['relationship']['tier'])
            if err: raise ValueError(err)
            ch['relationship'] = clean
        status = u.get('status')
        if (heard(status) or (supported(status) and any(
                status['evidence'] == event['evidence'] for event in events))) and isinstance(status.get('value'), str):
            ch['current_status'] = status['value'][:1000]
        if transitions:
            ch['presence'] = transitions[-1][1]


def generate(user, system, recent):
    from story_core.llm_bridge import call_claude_gm
    text,_ = call_claude_gm(user, system, recent)
    if not text or text.startswith(('【系統錯誤】', '【系統提示】')):
        raise ValueError('模型生成失敗，這回合未寫入，請重試')
    return text


def record(system, user):
    from story_core.llm_bridge import call_oneshot, get_recorder_model
    model = get_recorder_model()
    result = call_oneshot(user, system_prompt=system, **({"model_override": model} if model else {})).strip()
    if result.startswith('```'): result = result.split('\n',1)[1].rsplit('```',1)[0]
    return json.loads(result)['updates']


def _metered_call(slug, call_type, fn, *args, branch_id="main"):
    """Usage survives failed turns; persist each call before metadata is replaced."""
    from story_core import llm_bridge, usage_db
    previous = llm_bridge.get_last_usage()
    started = time.monotonic()
    try:
        return fn(*args)
    finally:
        usage = llm_bridge.get_last_usage()
        # Bridge publishes a new normalized dict for a completed model call.
        # An exception before publication must not re-charge the previous call.
        if usage is not None and usage is not previous:
            usage_db.log_from_bridge(slug, call_type, time.monotonic() - started,
                                     branch_id=branch_id, usage=deepcopy(usage))


def generate_stream(user, system, recent, emit_chunk):
    from story_core.llm_bridge import call_claude_gm_stream
    chunks = []; completed = False
    for kind, payload in call_claude_gm_stream(user, system, recent):
        if kind == 'text':
            if not isinstance(payload, str): raise ValueError('Invalid narration chunk')
            chunks.append(payload)
            emit_chunk(payload)
        elif kind == 'error':
            raise ValueError('模型生成失敗，這回合未寫入，請重試')
        elif kind == 'done':
            if isinstance(payload, dict) and payload.get('incomplete'):
                raise ValueError('模型連線中斷，這回合未寫入，請重試')
            completed = True
    text = ''.join(chunks)
    if not completed or not text.strip() or text.startswith(('【系統錯誤】', '【系統提示】')):
        raise ValueError('模型生成未完成，這回合未寫入，請重試')
    return text


def _narrate(slug, view, user, length, generate_fn, recall_mode="off", emit_chunk=None, branch_id="main", revision=None, narrative_style=None, plot_mode=None, scene_mode=None, persistent_instruction=''):
    """One narrator owns the scene; individual memory ownership remains labelled."""
    from story_core.card_mode.recap import context_window
    system = styles.apply_style(prompt(slug, view, user, length), narrative_style, plot_mode, scene_mode)
    system = instructions.apply(system,persistent_instruction)
    if revision: system += revision
    # Retrieval still respects each actor's original-text authorization.
    for cid, actor in view['state']['characters'].items():
        recalled = recall.collect(slug, branch_id, recall_mode, user,
                                  recall.ensemble_history(view, cid),
                                  actor=actor['name'], memories=actor['memory'])
        grounding = recall.grounding(recalled)
        if grounding:
            system += '\n# '+actor['name']+' 的個人回憶查詢\n'+grounding
    recent = context_window(view['messages'], view.get('source_recap', {}))
    recent = [{'role':m['role'], 'content':m['content']} for m in recent]
    if emit_chunk is not None and generate_fn is None:
        return _metered_call(slug, 'ensemble_gm', generate_stream, user, system, recent, emit_chunk, branch_id=branch_id)
    text = _metered_call(slug, 'ensemble_gm', generate_fn or generate, user, system, recent, branch_id=branch_id)
    if not isinstance(text, str) or not text.strip() or text.startswith(('【系統錯誤】', '【系統提示】')):
        raise ValueError('Empty or invalid narration')
    if emit_chunk is not None: emit_chunk(text)
    return text


def advance_clock(state, narration):
    header = parse_header_time(narration)
    if header:
        try:
            dt = datetime.fromisoformat(state['datetime'])
            new = dt.replace(year=header['year'] or dt.year, month=header['month'] or dt.month,
                day=header['day'] or dt.day, hour=header['hour'], minute=header['minute'])
            if new >= dt: state['datetime'] = new.isoformat()
            if header['location']: state['location'] = header['location']
            if header['weather']: state['weather'] = header['weather']
        except ValueError: pass


def validate_turn(user, length, recall_mode, narrative_style=None, plot_mode=None, scene_mode=None):
    styles.validate_style(narrative_style)
    styles.validate_plot_mode(plot_mode)
    styles.validate_scene_mode(scene_mode)
    recall.validate_mode(recall_mode)
    if not isinstance(user, str) or not user.strip() or len(user) > 20000:
        raise ValueError('請輸入有效訊息（最多 20000 字）')
    if length is not None and length not in RESPONSE_LENGTHS: raise ValueError('Invalid response_length')


def turn(slug, doc, user, length=None, generate_fn=None, recall_mode="off", emit_chunk=None, branch_id="main", revision=None, narrative_style=None, plot_mode=None, scene_mode=None, reply_group_id=None):
    """Only narration is on the interactive path. Pending work is durable."""
    validate_turn(user, length, recall_mode, narrative_style, plot_mode, scene_mode)
    candidate = deepcopy(doc); before = deepcopy(candidate['state'])
    from story_core import llm_bridge
    metadata = candidates.model_metadata(custom=generate_fn is not None)
    previous_usage = llm_bridge.get_last_usage()
    persistent_instruction = instructions.active(instructions.read(slug,branch_id))
    narration = _narrate(slug, doc, user, length, generate_fn, recall.validate_mode(recall_mode), emit_chunk, branch_id, revision, narrative_style, plot_mode, scene_mode, persistent_instruction)
    usage = llm_bridge.get_last_usage()
    if generate_fn is None and usage is not None and usage is not previous_usage:
        metadata.update(generation_provider=usage.get('provider'), generation_model=usage.get('model'))
    advance_clock(candidate['state'], narration)
    idx = doc['messages'][-1]['index']+1
    group, vid = reply_group_id or uuid.uuid4().hex, uuid.uuid4().hex
    candidate['messages'].extend([
        {'role':'user','content':user,'index':idx,'reply_group_id':group},
        {'role':'gm','content':narration,'index':idx+1,'ensemble_pre_snapshot':before,
         'reply_group_id':group,'reply_version_id':vid,'generated_at':candidates.stamp(),
         **metadata, 'response_length':length,'recall_mode':recall.validate_mode(recall_mode),
         'ensemble_snapshot':deepcopy(candidate['state']),
         'ensemble_turn_id':uuid.uuid4().hex, 'ensemble_record_status':'pending',
         'narrative_style':styles.validate_style(narrative_style),
         'plot_mode':styles.validate_plot_mode(plot_mode),
         'scene_mode':styles.validate_scene_mode(scene_mode),
         'persistent_instruction':persistent_instruction,
         'ensemble_controls_before':candidate.pop('ensemble_pending_controls', []),
         # Do not invent per-actor authorization before the recorder has evidence.
         'ensemble_visibility':{}}])
    return candidate, {'ok':True,'narration':narration,'gm_index':idx+1,'gm_source':'ensemble','reply_version_id':vid,
                       'recorder_status':'pending'}


def apply_controls(state, controls):
    for control in controls:
        cid = control['character_id']
        if control['action'] == 'add':
            state['characters'][cid] = deepcopy(control['character'])
        else:
            state['focus'] = cid
            if 'presence' in control: state['characters'][cid]['presence'] = control['presence']


def mutate(slug, action, body, generate_fn=None, record_fn=None, *, emit_chunk=None, async_recorder=True, branch_id="main"):
    from story_core.card_mode import ensemble_recorder
    with lock(slug, branch_id=branch_id):
        doc = load(slug, branch_id); s = doc['state']
        attempt = None
        if action in ('send','regenerate'):
            if action == 'regenerate':
                last = doc['messages'][-1]
                if last['index'] <= doc['baseline_index'] or 'ensemble_pre_snapshot' not in last:
                    raise ValueError('不能重新生成副本建立前的回合')
                user = doc['messages'][-2]['content']
                validate_turn(user, body.get('response_length'), body.get('recall_mode'), body.get('narrative_style'), body.get('plot_mode'), body.get('scene_mode'))
                attempt = candidates.begin(slug, branch_id, doc['messages'], body)
                doc['state'] = deepcopy(last['ensemble_pre_snapshot']); doc['messages'] = doc['messages'][:-2]
                # Preserve explicit controls made before and after the replaced turn.
                controls = doc.get('ensemble_pending_controls', [])
                apply_controls(doc['state'], controls)
                doc['ensemble_pending_controls'] = deepcopy(last.get('ensemble_controls_before', []))+controls
            else: user = body.get('message', '')
            try:
                doc, result = turn(slug, doc, user, body.get('response_length'), generate_fn,
                                   recall_mode=body.get('recall_mode'), emit_chunk=emit_chunk, branch_id=branch_id,
                                   narrative_style=body.get('narrative_style'), plot_mode=body.get('plot_mode'), scene_mode=body.get('scene_mode'),
                                   reply_group_id=attempt['group'] if attempt else None)
            except Exception as exc:
                if attempt: candidates.finish(slug, branch_id, attempt, error=exc)
                raise
        elif action == 'focus':
            cid = body.get('character_id')
            if cid not in s['characters']: raise ValueError('Unknown character ID')
            control = {'action':'focus', 'character_id':cid}
            if 'presence' in body:
                if body['presence'] not in ('present','remote','offstage'): raise ValueError('Invalid presence')
                control['presence'] = body['presence']
            apply_controls(s, [control]); doc.setdefault('ensemble_pending_controls', []).append(control)
            result = {'ok':True}
        elif action == 'add':
            if len(s['characters']) >= 6: raise ValueError('目前最多六位固定角色')
            cid,ch = character(body)
            if any(x['name']==ch['name'] for x in s['characters'].values()): raise ValueError('角色名稱不可重複')
            control = {'action':'add', 'character_id':cid, 'character':ch}
            apply_controls(s, [control]); doc.setdefault('ensemble_pending_controls', []).append(control)
            result = {'ok':True,'character_id':cid}
        elif action == 'rewind':
            index = body.get('to_index', body.get('message_index'))
            selected = next((m for m in doc['messages'] if m['index']==index and 'ensemble_snapshot' in m), None)
            if selected is None or index < doc['baseline_index']: raise ValueError('只能回到副本建立後的已完成回合')
            doc['state'] = deepcopy(selected['ensemble_snapshot'])
            doc['messages'] = [m for m in doc['messages'] if m['index']<=index]
            doc.pop('ensemble_pending_controls', None)
            result = {'ok':True}
        elif action == 'reset':
            doc['state'] = deepcopy(doc['initial_state'])
            doc['messages'] = [m for m in doc['messages'] if m['index']<=doc['baseline_index']]
            doc.pop('ensemble_pending_controls', None)
            result = {'ok':True}
        elif action == 'retry-recording':
            for message in doc['messages']:
                if message.get('ensemble_record_status') == 'error':
                    message['ensemble_record_status'] = 'pending'
                    message.pop('ensemble_record_error', None)
            result = {'ok':True}
        else: raise ValueError('Unknown action')
        save(slug, doc, branch_id)
        if attempt: candidates.finish(slug, branch_id, attempt, result=result)
    if async_recorder:
        ensemble_recorder.start(slug, record_fn, branch_id=branch_id)
    else:
        ensemble_recorder.drain(slug, record_fn, branch_id=branch_id)
    return result
