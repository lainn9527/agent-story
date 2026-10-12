"""Player-facing view and manual corrections of a branch's curated lore."""
import hashlib
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from story_core import story_io as io
from story_core.branch_tree import get_full_timeline
from story_core.card_mode import branches, lore

CATEGORIES = lore.CATEGORIES
CATEGORY_IDS = {key for key, _ in CATEGORIES}
MAX_TITLE = 80
MAX_TEXT = 2000
MAX_KEYWORDS = 30


def _read(slug, bid):
    file = lore.path(slug, bid)
    raw = file.read_bytes() if file.exists() else b''
    doc = json.loads(raw) if raw else {}
    if doc and doc.get('schema_version') != 1:
        raise branches.BranchError('不支援的設定格式')
    return file, raw, hashlib.sha256(raw).hexdigest(), doc


def _origin(entry):
    # Maintenance revisions carry an anchor; manual edits drop it (see operate).
    if entry.get('maintenance_anchor'):
        return 'maintenance'
    return 'manual' if entry.get('edited_by') == 'manual' else 'curated'


def _shown(entry):
    """The editable fields as the panel displays them."""
    return {'title': entry.get('title') or entry['id'], 'text': entry['text'],
            'category': entry.get('category') if entry.get('category') in CATEGORY_IDS else '',
            'keywords': [k for k in entry.get('keywords', []) if isinstance(k, str)],
            'always': bool(entry.get('always'))}


def _public(revision, doc, messages):
    entries = [{'id': e['id'], **_shown(e), 'origin': _origin(e),
                'sources': sorted({s['index'] for s in e.get('sources', []) if isinstance(s.get('index'), int)})}
               for e in lore.available_from(doc, messages)]
    return {'ok': True, 'revision': revision, 'entries': entries,
            'categories': [{'id': k, 'label': v} for k, v in CATEGORIES],
            'curated_through_index': doc.get('curated_through_index')}


def _fields(change):
    title, text = change.get('title'), change.get('text')
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE:
        raise branches.BranchError(f'設定標題不能空白，最多 {MAX_TITLE} 字')
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise branches.BranchError(f'設定內容不能空白，最多 {MAX_TEXT} 字；不要的條目請刪除')
    category = change.get('category', '')
    if category not in CATEGORY_IDS | {''}:
        raise branches.BranchError('未知的設定分類')
    keywords = change.get('keywords', [])
    if (not isinstance(keywords, list) or len(keywords) > MAX_KEYWORDS
            or any(not isinstance(k, str) or len(k) > 40 for k in keywords)):
        raise branches.BranchError(f'關鍵字最多 {MAX_KEYWORDS} 個，每個 40 字內')
    keywords = list(dict.fromkeys(k.strip() for k in keywords if k.strip()))
    if type(change.get('always', False)) is not bool:
        raise branches.BranchError('無效的常駐設定')
    return {'title': title.strip(), 'text': text.strip(), 'category': category,
            'keywords': keywords, 'always': change.get('always', False)}


def operate(slug, bid, body=None):
    """Caller holds the narration lock; stale drafts are rejected by file revision."""
    messages = get_full_timeline(slug, bid)
    file, raw, revision, doc = _read(slug, bid)
    if body is None:
        return _public(revision, doc, messages)
    if not isinstance(body, dict) or not isinstance(body.get('changes'), list):
        raise branches.BranchError('無效的設定修改')
    if body.get('revision') != revision:
        raise branches.Busy('設定已被更新（可能是背景整理）；草稿已保留，請重新載入後再修改。')
    if len(body['changes']) > 500:
        raise branches.BranchError('一次修改過多')
    doc = deepcopy(doc) or {'schema_version': 1, 'entries': []}
    doc.setdefault('entries', [])
    visible = {e['id']: e for e in lore.available_from(doc, messages)}
    raw_by_id = {e.get('id'): e for e in doc['entries']}
    now = datetime.now(timezone.utc).isoformat()
    audit, seen = [], set()
    for change in body['changes']:
        if not isinstance(change, dict):
            raise branches.BranchError('無效的設定修改')
        eid = change.get('id')
        if eid is not None and (eid in seen or eid not in visible):
            raise branches.BranchError('找不到這筆設定，或同一筆重複修改')
        if eid is not None:
            seen.add(eid)
        if change.get('delete'):
            if eid is None:
                raise branches.BranchError('無效的刪除')
            doc['entries'] = [e for e in doc['entries'] if e.get('id') != eid]
            audit.append({'id': eid, 'before': visible[eid], 'after': None})
            continue
        fields = _fields(change)
        if eid is None:
            entry = {'id': 'user_' + uuid.uuid4().hex[:12], **fields, 'related': [], 'sources': [],
                     'edited_by': 'manual', 'edited_at': now}
            doc['entries'].append(entry)
            audit.append({'id': entry['id'], 'before': None, 'after': entry})
            continue
        current = visible[eid]
        if _shown(current) == fields:
            continue
        if {**_shown(current), 'category': fields['category']} == fields:
            # Category is panel metadata, not narrated fact: relabel the whole chain in place.
            version = raw_by_id[eid]
            while isinstance(version, dict):
                version['category'] = fields['category']
                version = version.get('previous_version')
            audit.append({'id': eid, 'category_before': _shown(current)['category'],
                          'category_after': fields['category']})
            continue
        # Edits keep the shown version's sources, so a rewind past those turns still retires them,
        # but not its maintenance anchor: the correction belongs to the player, not that Pi run.
        # The replaced chain stays reachable for candidate switches.
        entry = {**{k: v for k, v in current.items() if k != 'maintenance_anchor'}, **fields,
                 'edited_by': 'manual', 'edited_at': now, 'previous_version': raw_by_id[eid]}
        doc['entries'] = [entry if e.get('id') == eid else e for e in doc['entries']]
        audit.append({'id': eid, 'before': current, 'after': {k: v for k, v in entry.items()
                                                                if k != 'previous_version'}})
    if not audit:
        return _public(revision, doc, messages)
    doc['updated_at'] = now
    backup_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:10]
    backup = Path(io._story_dir(slug)) / 'lore_backups' / bid / backup_id
    backup.mkdir(parents=True, exist_ok=False)
    (backup / 'card_lore_before.json').write_bytes(raw)
    io._save_json(str(backup / 'audit.json'), {'id': backup_id, 'story_id': slug, 'branch_id': bid,
                                               'at': now, 'revision_before': revision, 'changes': audit})
    io._save_json(str(file), doc)
    _, _, revision, doc = _read(slug, bid)
    return {**_public(revision, doc, messages), 'backup_id': backup_id}
