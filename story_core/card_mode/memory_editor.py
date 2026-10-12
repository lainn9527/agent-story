"""Explicit memory corrections, branch-local snapshots, and recoverable audit backups."""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from copy import deepcopy
from story_core import story_io as io, messages_codec
from story_core.card_mode import branches, ensemble, candidates
from story_core.card_mode.card import load_card


def _read(slug, bid):
    cast = ensemble.enabled(slug)
    paths = [ensemble.path(slug, bid)] if cast else [
        Path(io._story_npcs_path(slug, bid)), Path(io._story_messages_path(slug, bid))]
    # Corrections must survive adopting an archived version of this same branch.
    paths.extend(sorted(candidates.path(slug,bid).with_suffix('').glob('*.json')))
    raw = {str(p): p.read_bytes() for p in paths}
    revision = hashlib.sha256(b''.join(raw.values())).hexdigest()
    docs = [messages_codec.decode(json.loads(raw[str(p)])) for p in paths]
    chars = docs[0]['state']['characters'] if cast else {str(i): ch for i, ch in enumerate(docs[0])}
    return cast, paths, raw, revision, docs, chars


def _public(revision, chars):
    return {'ok': True, 'revision': revision, 'characters': [
        {'id': key, 'name': ch.get('name', key), 'memories': [
            {'index': i, 'text': text} for i, text in enumerate(ch.get('memory', []))]}
        for key, ch in chars.items()]}


def operate(slug, bid, body=None, blocking=False):
    load_card(slug)
    cast = ensemble.enabled(slug)
    guard = ensemble.lock(slug, branch_id=bid) if cast else branches.lock(slug, bid, blocking=blocking)
    with guard:
        branches.resolve(slug, bid)
        cast, paths, raw, revision, docs, chars = _read(slug, bid)
        if body is None:
            return _public(revision, chars)
        if not isinstance(body, dict) or not isinstance(body.get('changes'), list):
            raise branches.BranchError('無效的記憶修改')
        if body.get('revision') != revision:
            raise branches.Busy('記憶或對話已更新；草稿已保留，請重新載入後再修改。')
        messages = docs[0]['messages'] if cast else docs[1]
        if any(branches.recording_blocks_write(m) for m in messages):
            raise branches.Busy('背景記憶正在整理；草稿已保留，請稍後再保存。')
        changes = body['changes']
        if len(changes) > 10000:
            raise branches.BranchError('一次修改過多')
        mappings, seen, audit = {}, set(), []
        for change in changes:
            if not isinstance(change, dict):
                raise branches.BranchError('無效的記憶修改')
            key, idx, value = change.get('character_id'), change.get('index'), change.get('text')
            if not isinstance(key, str) or key not in chars or type(idx) is not int or idx < 0 or idx >= len(chars[key].get('memory', [])):
                raise branches.BranchError('找不到這筆記憶')
            if (key, idx) in seen:
                raise branches.BranchError('同一筆記憶重複修改')
            seen.add((key, idx))
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 5000):
                raise branches.BranchError('記憶不能空白，最多 5000 字；請用 × 刪除')
            old = chars[key]['memory'][idx]
            if not isinstance(old, str):
                raise branches.BranchError('此記憶格式不支援編輯')
            if value == old:
                continue
            mapping = mappings.setdefault(key, {})
            if old in mapping and mapping[old] != value:
                raise branches.BranchError('相同文字的記憶請使用相同修改，或只修改其中一筆')
            mapping[old] = value
            audit.append({'character_id': key, 'index': idx, 'before': old, 'after': value})
        if not audit:
            return _public(revision, chars)

        archived_messages = [v['message'] for doc in docs[1 if cast else 2:]
                             for v in doc.get('versions', [])]

        def replace(ch, mapping):
            if not isinstance(ch, dict) or not isinstance(ch.get('memory'), list):
                return
            ch['memory'] = [mapping.get(v, v) if isinstance(v, str) else v for v in ch['memory']
                            if not isinstance(v, str) or v not in mapping or mapping[v] is not None]

        if cast:
            states = [docs[0].get('state'), docs[0].get('initial_state')]
            for m in messages + archived_messages:
                states.extend([m.get('ensemble_snapshot'), m.get('ensemble_pre_snapshot')])
            for state in states:
                if isinstance(state, dict):
                    for key, mapping in mappings.items():
                        replace(state.get('characters', {}).get(key), mapping)
        else:
            # Identify snapshot characters by unique name, never by drifting list position.
            names = [ch.get('name') for ch in docs[0]]
            for key in mappings:
                if not names[int(key)] or names.count(names[int(key)]) != 1:
                    raise branches.BranchError('角色名稱不唯一，無法安全同步歷史快照')
            snapshots = [docs[0]]
            for m in messages + archived_messages:
                snapshots.extend([m.get('npcs_snapshot'), m.get('pre_npcs_snapshot')])
            for snapshot in snapshots:
                if isinstance(snapshot, list):
                    for key, mapping in mappings.items():
                        for ch in snapshot:
                            if ch.get('name') == names[int(key)]:
                                replace(ch, mapping)

        backup_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:10]
        backup = Path(io._story_dir(slug)) / 'memory_edit_backups' / bid / backup_id
        backup.mkdir(parents=True, exist_ok=False)
        for path in paths:
            (backup / path.name).write_bytes(raw[str(path)])
        log = {'id': backup_id, 'story_id': slug, 'branch_id': bid, 'at': datetime.now(timezone.utc).isoformat(),
               'status': 'prepared', 'revision_before': revision, 'changes': audit,
               'files': [{'path': str(path), 'backup': path.name} for path in paths]}
        io._save_json(str(backup/'audit.json'), log)
        try:
            for path, doc in zip(paths, docs):
                io._save_json(str(path), doc)
            log['status'] = 'applied'
            io._save_json(str(backup/'audit.json'), log)
        except Exception:
            for path in paths:
                path.write_bytes(raw[str(path)])
            log['status'] = 'rolled_back'
            io._save_json(str(backup/'audit.json'), log)
            raise
        _, _, _, revision, _, chars = _read(slug, bid)
        return {**_public(revision, chars), 'backup_id': backup_id}
