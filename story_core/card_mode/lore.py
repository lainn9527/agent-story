"""Curated, branch-local lore. Selection is local; nothing is auto-collected."""
from hashlib import sha256
from pathlib import Path

from story_core import story_io as io

MAX_ENTRIES = 6
MAX_CHARS = 4500


def path(slug, bid):
    return Path(io._branch_dir(slug, bid)) / 'card_lore.json'


def content_hash(text):
    return sha256(text.encode('utf-8')).hexdigest()


def inherit(slug, source, target):
    source_path = path(slug, source)
    if source_path.exists():
        io._save_json(str(path(slug, target)), io._load_json(str(source_path), {}))


def available(slug, bid, messages):
    doc = io._load_json(str(path(slug, bid)), {})
    if not doc or doc.get('schema_version') != 1:
        return []
    # A reused index after regen must not resurrect the discarded world's facts.
    sources = {m.get('index'): content_hash(m.get('content', ''))
               for m in messages if m.get('role') == 'gm'}
    result = []
    for entry in doc.get('entries', []):
        # Walk back revisions when their source turn was discarded or regenerated.
        # Histories stay on disk so a candidate switch can recover its own version.
        while isinstance(entry, dict):
            anchor = entry.get('maintenance_anchor')
            valid_anchor = not anchor or sources.get(anchor.get('index')) == anchor.get('hash')
            if (entry.get('id') and entry.get('text') and valid_anchor
                    and all(sources.get(s.get('index')) == s.get('hash')
                            for s in entry.get('sources', []))):
                result.append({k: v for k, v in entry.items() if k != 'previous_version'})
                break
            entry = entry.get('previous_version')
    return result


def grounding(slug, bid, messages, user='', location='', exclude_ids=()):
    entries = [e for e in available(slug, bid, messages) if e['id'] not in exclude_ids]
    by_id = {e['id']: e for e in entries}
    recent = '\n'.join(m.get('content', '') for m in messages[-4:])
    def score(entry):
        return sum(12 * (k in location) + 40 * (k in user) + (k in recent)
                   for k in entry.get('keywords', []) if isinstance(k, str) and k)
    core = [e for e in entries if e.get('always')]
    ranked = sorted((e for e in entries if not e.get('always') and score(e) > 0),
                    key=lambda e: (-score(e), e['id']))
    selected = []; seen = set(); used = 0
    def add(entry):
        nonlocal used
        if entry['id'] in seen:
            return
        block = f"### {entry.get('title', entry['id'])}\n{entry['text']}"
        if used + len(block) > MAX_CHARS:
            return
        selected.append(block); seen.add(entry['id']); used += len(block)
    for entry in core:
        add(entry)
    for entry in ranked:
        if len(seen - {e['id'] for e in core}) >= MAX_ENTRIES:
            break
        add(entry)
        for related in entry.get('related', []):
            if related in by_id and len(seen - {e['id'] for e in core}) < MAX_ENTRIES:
                add(by_id[related])
    if not selected:
        return ''
    return '\n\n# 本線已整理的世界設定\n' + (
        '以下是導演背景，不自動變成每名角色都知道的資訊。人物情報與預測保持其來源，'
        '未完成的任務不當成果，動態現況以較新的正式原文為準。'
        '本輪只取部分相關條目；沒有取到不表示不存在，也不能據此另編相反規則。\n'
    ) + '\n\n'.join(selected)
