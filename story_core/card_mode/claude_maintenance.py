"""Opt-in background memory and lore upkeep by a headless Claude.

Every `every_messages` new messages on a branch, Claude reads the memories added
since its last pass, the current lore and the new messages as evidence, and
returns memory merge/remove edits plus lore updates/additions. Memory edits go
through memory_editor (lock, snapshot rewrite, undoable backup). Lore revisions
carry a maintenance anchor on the pass's last GM message, so a rewind or
regenerate before it falls back to the previous version, as Pi revisions do.
A cursor in the branch makes passes contiguous: a failed pass retries from the
same place instead of skipping ahead.
"""
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from story_core import story_io as io
from story_core.branch_tree import get_full_timeline
from story_core.card_mode import branches, ensemble, lore, lore_editor, memory_editor

log = logging.getLogger('rpg')
DEFAULTS = {'enabled': False, 'every_messages': 50, 'model': 'opus', 'timeout_seconds': 900, 'claude_bin': None}
MAX_MESSAGE_CHARS = 2500
MAX_FAILURES = 3
_running: set[tuple[str, str]] = set()
_guard = threading.Lock()

PROMPT = """你是長篇角色卡故事的資料整理員，負責兩件事：整理長期記憶、維護世界設定。下面依序是：目前全部記憶（每行「索引<TAB>內容」，標 * 的是上次整理之後新增、尚未整理過的）、目前的世界設定、這段期間的原文（證據）。

## 一、記憶
只整理標 * 的記憶，以及與它們重複的舊記憶。處理四類冗餘，一律用合併處理：
1. 換句話說的重複：同一事件、約定或口徑記了多次 → 留一筆，其他刪除。
2. 純情緒反應：只有一時羞憤、無奈、震驚而沒有新事實 → 刪除；持久的態度或關係轉變要保留。
3. 已完成或已取消的短期安排（今晚、明早、一刻鐘後……）→ 原文證明已履行或取消才刪除。
4. 被取代的狀態與進度（修為、進度、處境）→ 有更新的記錄時刪除舊的。
被刪的記憶若有保留記錄沒有的獨有細節，先把細節併入保留那筆（改寫它的內容），再刪除。
不要新增原文沒有的事實，不要刪除仍有長期價值的事實，不確定就保留。

## 二、世界設定
設定每回合會依關鍵字注入給 GM，是跨場景仍有用的穩定背景：人物現況與身分、勢力、地點、功法體系、長期局勢與未完成的重大事件（例如論道、魔道暗線、尚未兌現的重要約定）。
- 依這段原文，更新已過時的條目（修為、處境、持有、立場、局勢進展、已兌現或新增的約定），或為新出現且會反覆登場的人物、勢力、地點、長期事件新增條目。
- 改寫時輸出完整的新內容，保留仍有效的舊資訊；寫成精煉的背景敘述（約 100–400 字），不要寫成事件流水帳，也不要寫一時的行蹤與情緒。
- keywords 要列出 GM 原文會用到的名稱與別名（人名、稱呼、地名、招式、物品），新增條目 3–15 個。
- 標「鎖定」的條目是作者設定，不要修改。人物知情依實際經歷，祕密要註明誰知道。
- 設定都還正確就回傳空清單。

只輸出一個 JSON 物件，不要其他文字：
{"changes": [{"index": 索引, "before": "原內容（逐字）", "text": "改寫後內容；刪除則為 null"}],
 "lore": [{"id": "既有條目 id；新增則為 null", "title": "標題", "category": "world|faction|place|character|power|event|other", "keywords": ["..."], "text": "完整內容"}],
 "report": "一兩句中文摘要"}

# 記憶
{memories}

# 世界設定
{lore}

# 期間原文
{messages}
"""


def config_path(slug):
    return Path(io._story_dir(slug)) / 'claude_maintenance.json'


def ledger_path(slug, bid):
    return Path(io._branch_dir(slug, bid)) / 'claude_maintenance_status.json'


def config(slug):
    return {**DEFAULTS, **io._load_json(str(config_path(slug)), {})}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _save_ledger(slug, bid, **fields):
    io._save_json(str(ledger_path(slug, bid)), fields)


def _memories(slug, bid):
    state = memory_editor.operate(slug, bid, blocking=True)
    character = state['characters'][0]
    return state['revision'], character['id'], [m['text'] for m in character['memories']]


def maybe_run(slug, bid, latest, *, call=None, background=True):
    """Called after the turn ending at message `latest` is recorded; True when a pass started."""
    cfg = config(slug)
    if not cfg['enabled'] or ensemble.enabled(slug):
        return False
    ledger = io._load_json(str(ledger_path(slug, bid)), {})
    if not isinstance(ledger, dict) or 'through_index' not in ledger or ledger['through_index'] > latest:
        # New branch, or rewound behind the cursor: start counting here, never rescan history.
        _save_ledger(slug, bid, through_index=latest, memory_count=len(_memories(slug, bid)[2]),
                     status='baseline', at=_now())
        return False
    if latest - ledger['through_index'] < cfg['every_messages'] or latest < ledger.get('retry_after', -1):
        return False
    key = (slug, bid)
    with _guard:
        if key in _running:
            return False
        _running.add(key)

    def work():
        try:
            run(slug, bid, cfg, ledger, call=call)
        except Exception as exc:
            log.exception('claude_maintenance: pass failed for %s/%s', slug, bid)
            failures = ledger.get('failures', 0) + 1
            if failures < MAX_FAILURES:
                # Keep the cursor so nothing is skipped, but back off before trying again.
                _save_ledger(slug, bid, **{**ledger, 'status': 'error', 'error': str(exc)[:500],
                                           'failures': failures, 'retry_after': latest + 10, 'at': _now()})
            else:
                # The same range keeps failing (e.g. it needs a bigger plan than allowed): move on
                # instead of retrying forever, and record the range for a manual pass.
                _save_ledger(slug, bid, through_index=latest, memory_count=len(_memories(slug, bid)[2]),
                             status='skipped', error=str(exc)[:500], at=_now(),
                             skipped=ledger.get('skipped', []) + [[ledger['through_index'] + 1, latest]])
        finally:
            with _guard:
                _running.discard(key)

    if background:
        threading.Thread(target=work, daemon=True, name=f'claude-maintenance-{bid}').start()
    else:
        work()
    return True


def run(slug, bid, cfg, ledger, *, call=None):
    _, _, memories = _memories(slug, bid)
    messages = io._load_branch_messages(slug, bid)
    anchor = messages[-1]['index']
    new_from = min(ledger.get('memory_count', len(memories)), len(memories))
    lines = '\n'.join(f"{'*' if i >= new_from else ''}{i}\t{m}" for i, m in enumerate(memories))
    sources = '\n\n'.join(f"#{m['index']} {'玩家' if m['role'] == 'user' else 'GM'}：{m['content'][:MAX_MESSAGE_CHARS]}"
                          for m in messages if m['index'] > ledger['through_index'] and m.get('content'))
    entries = lore.available_from(io._load_json(str(lore.path(slug, bid)), {}), messages)
    lore_text = '\n\n'.join(
        f"[{e['id']}] {e.get('title', e['id'])}（{e.get('category') or 'other'}）{'（鎖定）' if e.get('always') else ''}\n"
        f"keywords: {'、'.join(e.get('keywords', []))}\n{e['text']}" for e in entries) or '（尚無設定）'
    prompt = PROMPT.replace('{memories}', lines).replace('{lore}', lore_text).replace('{messages}', sources)
    plan = _parse((call or _claude)(prompt, cfg))
    new_count = len(memories) - new_from
    # Folding superseded older entries is expected, but refuse a plan that would rewrite wholesale.
    if len(plan['changes']) > max(15, 2 * new_count):
        raise ValueError(f"整理提案過大（{len(plan['changes'])} 筆，新記憶 {new_count} 筆），未套用")
    removed = _apply(slug, bid, plan['changes'])
    lore_ids = apply_lore(slug, bid, plan['lore'], anchor)
    _save_ledger(slug, bid, through_index=anchor, memory_count=len(memories) - removed,
                 status='done', changes=len(plan['changes']), removed=removed, lore=lore_ids,
                 skipped=ledger.get('skipped', []),
                 report=plan.get('report', ''), at=_now())


def _apply(slug, bid, changes, attempts=24):
    """Resolve each change by its exact old text, then commit through the memory editor."""
    for _ in range(attempts):
        revision, cid, current = _memories(slug, bid)
        edits, removed = [], 0
        for change in changes:
            before = change.get('before')
            found = [i for i, m in enumerate(current) if m == before]
            if len(found) != 1:
                continue  # changed or duplicated meanwhile: skip this one only
            text = change.get('text')
            edits.append({'character_id': cid, 'index': found[0], 'text': text})
            removed += text is None
        if not edits:
            return 0
        try:
            memory_editor.operate(slug, bid, {'revision': revision, 'changes': edits}, blocking=True)
            return removed
        except branches.Busy:
            time.sleep(5)  # a turn is being recorded; retry with a fresh revision
    raise branches.Busy('記憶持續在整理中，本次未套用')


def _parse(raw):
    match = re.search(r'\{.*\}', raw or '', re.S)
    if not match:
        raise ValueError('Claude 沒有回傳 JSON')
    plan = json.loads(match.group(0))
    if not isinstance(plan.get('changes'), list):
        raise ValueError('Claude 回傳格式錯誤')
    for c in plan['changes']:
        if not isinstance(c, dict) or not isinstance(c.get('before'), str) or not (
                c.get('text') is None or (isinstance(c.get('text'), str) and c['text'].strip())):
            raise ValueError('Claude 回傳的修改格式錯誤')
    plan.setdefault('lore', [])
    if not isinstance(plan['lore'], list):
        raise ValueError('Claude 回傳的設定修改格式錯誤')
    for op in plan['lore']:
        if not isinstance(op, dict) or not (op.get('id') is None or isinstance(op.get('id'), str)):
            raise ValueError('Claude 回傳的設定修改格式錯誤')
        lore_editor._fields({**op, 'always': False})  # same limits as the player's lore panel
    return plan


def apply_lore(slug, bid, ops, anchor_index):
    """Update/add lore under the branch lock; never touches author-locked (always) entries or deletes.

    Returns the ids written. Backups use the lore panel's format, so they can be restored the same way.
    """
    if not ops:
        return []
    with branches.lock(slug, bid, blocking=True):
        messages = get_full_timeline(slug, bid)
        anchor_msg = next((m for m in reversed(messages)
                           if m.get('role') == 'gm' and m['index'] <= anchor_index), None)
        if anchor_msg is None:
            return []
        stamp = {'index': anchor_msg['index'], 'hash': lore.content_hash(anchor_msg['content'])}
        file, raw, revision, doc = lore_editor._read(slug, bid)
        doc = json.loads(raw) if raw else {'schema_version': 1, 'entries': []}
        doc.setdefault('entries', [])
        visible = {e['id']: e for e in lore.available_from(doc, messages)}
        raw_by_id = {e.get('id'): e for e in doc['entries']}
        now, audit = _now(), []
        for op in ops:
            eid = op.get('id')
            fields = lore_editor._fields({**op, 'always': False})
            if eid in visible:
                current = visible[eid]
                if current.get('always'):
                    continue  # author canon stays as written
                entry = {**{k: v for k, v in current.items() if k != 'previous_version'}, **fields,
                         'always': False, 'edited_by': 'claude', 'edited_at': now,
                         'maintenance_anchor': stamp, 'previous_version': raw_by_id[eid]}
                doc['entries'] = [entry if e.get('id') == eid else e for e in doc['entries']]
                audit.append({'id': eid, 'before': current, 'after': {k: v for k, v in entry.items()
                                                                        if k != 'previous_version'}})
            elif eid is None:
                entry = {'id': 'cm_' + os.urandom(6).hex(), **fields, 'related': [], 'sources': [],
                         'edited_by': 'claude', 'edited_at': now, 'maintenance_anchor': stamp}
                doc['entries'].append(entry)
                audit.append({'id': entry['id'], 'before': None, 'after': entry})
        if not audit:
            return []
        doc['updated_at'] = now
        doc['curated_through_index'] = max(doc.get('curated_through_index') or -1, anchor_msg['index'])
        backup_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + os.urandom(5).hex()
        backup = Path(io._story_dir(slug)) / 'lore_backups' / bid / backup_id
        backup.mkdir(parents=True, exist_ok=False)
        (backup / 'card_lore_before.json').write_bytes(raw)
        io._save_json(str(backup / 'audit.json'), {'id': backup_id, 'story_id': slug, 'branch_id': bid, 'at': now,
                                                   'source': 'claude_maintenance', 'revision_before': revision,
                                                   'changes': audit})
        io._save_json(str(file), doc)
        return [a['id'] for a in audit]


def _claude_bin(cfg):
    candidates = [cfg.get('claude_bin'), shutil.which('claude'),
                  *sorted(glob.glob(os.path.expanduser('~/.nvm/versions/node/*/bin/claude')), reverse=True),
                  os.path.expanduser('~/.local/bin/claude')]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    raise FileNotFoundError('找不到 claude CLI')


def _claude(prompt, cfg):
    env = {k: v for k, v in os.environ.items() if k not in ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT')}
    with tempfile.TemporaryDirectory() as cwd:  # no tools and an empty cwd: pure text in, JSON out
        result = subprocess.run([_claude_bin(cfg), '-p', '--tools', '', '--strict-mcp-config',
                                 '--output-format', 'json', '--model', cfg['model']],
                                input=prompt, capture_output=True, text=True, cwd=cwd, env=env,
                                timeout=cfg['timeout_seconds'])
    if result.returncode != 0:
        raise RuntimeError(f'claude 結束碼 {result.returncode}：{result.stderr[-300:]}')
    data = json.loads(result.stdout)
    log.info('claude_maintenance: %.0fs, $%s', data.get('duration_ms', 0) / 1000, data.get('total_cost_usd'))
    if data.get('is_error'):
        raise RuntimeError(str(data.get('result'))[:300])
    return data.get('result', '')
