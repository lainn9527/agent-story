"""Branch-local reply alternatives. Drafts never enter narration or memory context."""
from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from story_core import story_io as io, compaction
from story_core.card_mode import branches, ensemble as e


def stamp():
    return datetime.now(timezone.utc).isoformat()


def model_metadata(custom=False):
    if custom: return {'generation_provider': 'custom', 'generation_model': None}
    from story_core import llm_bridge
    provider = llm_bridge.get_provider(); config = llm_bridge._get_config()
    section = llm_bridge._get_openrouter_cfg(config) if provider in ('openrouter', 'open_router') else config.get(provider, {})
    defaults = {'gemini':'gemini-2.0-flash', 'vertex':'gemini-2.5-flash',
                'openrouter':'deepseek/deepseek-v4.1-flash', 'open_router':'deepseek/deepseek-v4.1-flash',
                'codex_agent':'gpt-5.4', 'claude_cli':'claude-sonnet-4-5-20250929'}
    return {'generation_provider':provider, 'generation_model':section.get('model', defaults.get(provider))}


def path(slug, bid):
    root = e.path(slug, bid).parent if e.enabled(slug) else Path(io._branch_dir(slug, bid))
    return root / 'card_candidates.json'


def load_index(slug, bid):
    return io._load_json(str(path(slug, bid)), {'version': 2, 'groups': {}})


def load(slug, bid, group=None):
    index = load_index(slug, bid)
    ids = [group] if group is not None else list(index['groups'])
    groups = {}
    for gid in ids:
        if gid in index['groups']:
            groups[gid] = io._load_json(str(path(slug, bid).with_suffix('') / (gid+'.json')), index['groups'][gid])
    return {'version':2, 'groups':groups}


def save(slug, bid, archive):
    index = load_index(slug, bid)
    for gid, data in archive['groups'].items():
        io._save_json(str(path(slug, bid).with_suffix('') / (gid+'.json')), data)
        index['groups'][gid] = {'message_index':data['message_index'],
            'versions':[{**{k:v[k] for k in ('id','favorite')}, **feedback_view(v)} for v in data['versions']],
            'attempts':[{k:a[k] for k in ('id','status')} for a in data['attempts']]}
    index['version'] = 2
    io._save_json(str(path(slug, bid)), index)


def messages(slug, bid):
    return e.load(slug, bid)['messages'] if e.enabled(slug) else io._load_branch_messages(slug, bid)


def identity(rows, pos):
    if pos == 0:
        gm = rows[0]
        group = 'opening_' + hashlib.sha256(gm.get('content', '').encode()).hexdigest()[:32]
        return group, gm.get('reply_version_id') or gm.get('card_turn_id') or gm.get('ensemble_turn_id') or group
    gm, user = rows[pos], rows[pos-1]
    group = gm.get('reply_group_id') or user.get('reply_group_id')
    if not group:
        evidence = [(m.get('role'), m.get('content'), m.get('index')) for m in rows[:pos]]
        group = 'legacy_' + hashlib.sha256(json.dumps(evidence, ensure_ascii=False).encode()).hexdigest()[:32]
    vid = gm.get('reply_version_id') or gm.get('card_turn_id') or gm.get('ensemble_turn_id')
    if not vid:
        vid = 'legacy_' + hashlib.sha256((group + gm.get('content', '')).encode()).hexdigest()[:32]
    return group, vid


def feedback_view(version):
    return {'version_id':version['id'], 'liked':version.get('liked', False),
            'comment':version.get('comment', ''), 'comment_revision':version.get('comment_revision')}


def feedback_views(slug, bid, rows):
    """Lightweight state metadata, never stored on gameplay messages."""
    recorded = {v['id']:feedback_view(v) for data in load_index(slug, bid)['groups'].values()
                for v in data['versions']}
    result = {}
    prefix = hashlib.sha256(b'[')
    for pos, gm in enumerate(rows):
        if gm.get('role') == 'gm':
            vid = gm.get('reply_version_id') or gm.get('card_turn_id') or gm.get('ensemble_turn_id')
            if not vid:
                if not pos: vid = identity(rows, pos)[1]
                else:
                    group = gm.get('reply_group_id') or rows[pos-1].get('reply_group_id')
                    if not group:
                        digest = prefix.copy(); digest.update(b']')
                        group = 'legacy_' + digest.hexdigest()[:32]
                    vid = 'legacy_' + hashlib.sha256((group + gm.get('content', '')).encode()).hexdigest()[:32]
            result[str(gm['index'])] = recorded.get(vid, feedback_view({'id':vid}))
        if pos: prefix.update(b', ')
        prefix.update(json.dumps((gm.get('role'), gm.get('content'), gm.get('index')), ensure_ascii=False).encode())
    return result


def capture(archive, rows, pos):
    group, vid = identity(rows, pos)
    gm = deepcopy(rows[pos]); gm.update(reply_group_id=group, reply_version_id=vid)
    data = archive['groups'].setdefault(group, {'message_index': gm['index'],
        'user_message': rows[pos-1]['content'] if pos else '', 'versions': [], 'attempts': []})
    found = next((v for v in data['versions'] if v['id'] == vid), None)
    if found is None:
        found = {'id': vid, 'saved_at': stamp(), 'favorite': False}
        data['versions'].append(found)
    found['message'] = gm
    return group, vid


def sync_existing(slug, bid, gm):
    """Called under the gameplay lock when a retained Recorder updates snapshots."""
    group, vid = gm.get('reply_group_id'), gm.get('reply_version_id')
    if not group or not vid or group not in load_index(slug,bid)['groups']: return
    archive = load(slug, bid, group)
    data = archive['groups'].get(group)
    if data:
        version = next((v for v in data['versions'] if v['id'] == vid), None)
        if version is None:
            version = {'id':vid, 'saved_at':stamp(), 'favorite':False}
            data['versions'].append(version)
        version['message'] = deepcopy(gm)
        save(slug, bid, archive)


def begin(slug, bid, rows, body):
    """Archive the old reply before removal and identify the logical player turn."""
    archive = load(slug, bid, identity(rows, len(rows)-1)[0])
    group, vid = capture(archive, rows, len(rows)-1)
    rows[-2]['reply_group_id'] = group
    rows[-1].update(reply_group_id=group, reply_version_id=vid)
    attempt = {'id': uuid.uuid4().hex, 'started_at': stamp(), 'status': 'pending',
               'settings': {k: body.get(k) for k in ('narrative_style', 'plot_mode', 'scene_mode', 'recall_mode', 'response_length')}}
    archive['groups'][group]['attempts'].append(attempt)
    save(slug, bid, archive)
    return {'group': group, 'attempt': attempt['id'], 'previous': vid}


def finish(slug, bid, token, result=None, error=None):
    """An attempt is counted once, including failed/cancelled regenerations."""
    archive = load(slug, bid, token['group']); data = archive['groups'][token['group']]
    attempt = next(a for a in data['attempts'] if a['id'] == token['attempt'])
    rows = messages(slug, bid)
    gm = rows[-1] if rows else {}
    vid = (result or {}).get('reply_version_id')
    if gm.get('role') == 'gm' and gm.get('reply_group_id') == token['group'] and (not vid or gm.get('reply_version_id') == vid):
        _, current_vid = capture(archive, rows, len(rows)-1)
        vid = vid or current_vid
    success = result and result.get('ok') and any(v['id'] == vid for v in data['versions'])
    attempt.update(status='succeeded' if success else 'failed', finished_at=stamp())
    if success:
        attempt['version_id'] = vid
    else:
        attempt['error'] = str(error or (result or {}).get('error') or '生成已取消或未完成')
    save(slug, bid, archive)


def recover_failed(slug, bid, token, generation):
    """Restore the replaced reply only while this failed regen still owns the turn."""
    if branches.generation(slug, bid) != generation: return
    rows = messages(slug, bid)
    if not rows or rows[-1].get('role') != 'user' or rows[-1].get('reply_group_id') != token['group']: return
    data = load(slug, bid, token['group'])['groups'][token['group']]
    gm = deepcopy(next(v['message'] for v in data['versions'] if v['id'] == token['previous']))
    settled = gm.get('card_record_status') not in ('pending', 'error')
    gm['card_turn_id'] = uuid.uuid4().hex
    if not settled:
        gm['card_record_status'] = 'pending'
        gm['card_record_pid'] = os.getpid()
        gm['state_snapshot'] = deepcopy(gm['pre_state_snapshot'])
        gm['npcs_snapshot'] = deepcopy(gm['pre_npcs_snapshot'])
    for field, name in [('state_snapshot','character_state.json'), ('npcs_snapshot','npcs.json'), ('clock_snapshot','card_clock.json')]:
        if gm.get(field) is not None:
            io._save_json(str(Path(io._branch_dir(slug, bid))/name), gm[field])
    io._save_json(io._story_messages_path(slug, bid), rows+[gm])
    sync_existing(slug, bid, gm)
    if not settled:
        from story_core.card_mode.pipeline import record_saved_turn
        record_saved_turn(slug, bid, gm, rows[-1]['content'])


def public_version(version, selected=None):
    gm = version['message']
    return {'id': version['id'], 'content': gm['content'], 'favorite': version['favorite'],
            'feedback':feedback_view(version),
            'selected': version['id'] == selected,
            'generated_at': gm.get('generated_at'), 'saved_at': version['saved_at'],
            'provider': gm.get('generation_provider'), 'model': gm.get('generation_model'),
            'text_only_edit': gm.get('reply_text_only_edit', False), 'edited_at': gm.get('reply_edited_at'),
            **{k: gm.get(k) for k in ('narrative_style', 'plot_mode', 'scene_mode', 'recall_mode', 'response_length', 'persistent_instruction')}}


def summaries(slug, bid, rows):
    archive = load_index(slug, bid); result = {}
    indices = {data['message_index'] for data in archive['groups'].values()}
    for pos, gm in enumerate(rows):
        if pos < 1 or gm.get('index') not in indices or gm.get('role') != 'gm' or rows[pos-1].get('role') != 'user': continue
        group, vid = identity(rows, pos); data = archive['groups'].get(group)
        if data:
            attempts = data['attempts']
            result[str(gm['index'])] = {'versions': len(data['versions']), 'regen_count': len(attempts),
                'failed_count': sum(a['status'] == 'failed' for a in attempts),
                'selected_number': next((i+1 for i,v in enumerate(data['versions']) if v['id'] == vid), len(data['versions'])+1),
                'selected_id': vid}
    return result


def listing(slug, bid, index=None, favorites=False):
    rows = messages(slug, bid)
    if favorites:
        favorite_ids = [gid for gid,data in load_index(slug,bid)['groups'].items() if any(v['favorite'] for v in data['versions'])]
        archive = {'version':2, 'groups':{gid:load(slug,bid,gid)['groups'][gid] for gid in favorite_ids}}
        indices = {data['message_index'] for data in archive['groups'].values()}
        # Refresh only already archived, retained variants; don't manufacture history.
        for pos, gm in enumerate(rows):
            if pos > 0 and gm.get('index') in indices and gm.get('role') == 'gm' and rows[pos-1].get('role') == 'user':
                group, _ = identity(rows, pos)
                if group in archive['groups']: capture(archive, rows, pos)
        items = [{**public_version(v), 'group_id': gid, 'message_index': data['message_index'],
                  'user_message': data['user_message']}
                 for gid, data in archive['groups'].items() for v in data['versions'] if v['favorite']]
        return {'ok': True, 'favorites': items}
    pos = next((i for i,m in enumerate(rows) if m.get('role') == 'gm' and m.get('index') == index), -1)
    if pos < 1 or rows[pos-1].get('role') != 'user': raise branches.BranchError('開場沒有候選版本')
    if e.enabled(slug) and index <= e.load(slug, bid)['baseline_index']:
        raise branches.BranchError('副本建立前的回合沒有候選版本')
    archive = load(slug, bid, identity(rows, pos)[0])
    group, selected = capture(archive, rows, pos)
    data = archive['groups'][group]
    return {'ok': True, 'group_id': group, 'message_index': index, 'user_message': data['user_message'],
            'latest': pos == len(rows)-1, 'selected_id': selected,
            'versions': [public_version(v, selected) for v in data['versions']],
            'regen_count': len(data['attempts']),
            'failed_count': sum(a['status'] == 'failed' for a in data['attempts']),
            'attempts': deepcopy(data['attempts'])}


def favorite(slug, bid, body):
    if type(body.get('favorite')) is not bool: raise branches.BranchError('請指定收藏狀態')
    group = body.get('group_id'); vid = body.get('version_id')
    if not isinstance(group, str) or not isinstance(vid, str): raise branches.BranchError('無效版本')
    if not group or not all(ch.isalnum() or ch in '_-' for ch in group): raise branches.BranchError('無效版本')
    archive = load(slug, bid, group)
    # A version that has never been regenerated is lazily saved when favorited.
    rows = messages(slug, bid)
    for pos, gm in enumerate(rows):
        if pos > 0 and gm.get('role') == 'gm' and rows[pos-1].get('role') == 'user' and identity(rows, pos) == (group, vid):
            capture(archive, rows, pos)
            break
    version = next((v for v in archive['groups'].get(group, {}).get('versions', []) if v['id'] == vid), None)
    if version is None: raise branches.BranchError('候選版本不存在')
    version['favorite'] = body['favorite']; save(slug, bid, archive)
    return {'ok': True, 'favorite': version['favorite']}


def set_feedback(slug, bid, body):
    """Caller owns the branch lock. Ratings only touch the alternative archive."""
    if not isinstance(body, dict) or type(body.get('message_index')) is not int:
        raise branches.BranchError('請指定回覆')
    if not ('liked' in body or 'comment' in body): raise branches.BranchError('請提供讚或註記')
    if 'liked' in body and type(body['liked']) is not bool: raise branches.BranchError('無效的按讚狀態')
    if 'comment' in body and (not isinstance(body['comment'], str) or len(body['comment']) > 4000):
        raise branches.BranchError('註記最多 4000 字')
    vid = body.get('version_id'); group = body.get('group_id')
    if not isinstance(vid, str) or not vid: raise branches.BranchError('請指定回覆版本')
    if group is not None and (not isinstance(group, str) or not group or
                              not all(ch.isalnum() or ch in '_-' for ch in group)):
        raise branches.BranchError('無效版本')
    rows = messages(slug, bid)
    pos = next((i for i,m in enumerate(rows) if m.get('role') == 'gm' and m.get('index') == body['message_index']), -1)
    current = pos >= 0 and identity(rows, pos)[1] == vid
    if group is None:
        if not current: raise branches.BranchError('回覆已改變，請重新載入後再標記')
        group = identity(rows, pos)[0]
    archive = load(slug, bid, group)
    if current and identity(rows, pos)[0] == group: capture(archive, rows, pos)
    data = archive['groups'].get(group, {})
    version = next((v for v in data.get('versions', []) if v['id'] == vid), None)
    if version is None or data.get('message_index') != body['message_index']:
        raise branches.BranchError('回覆版本不存在')
    if 'comment' in body:
        if 'expected_comment_revision' not in body:
            raise branches.BranchError('請重新開啟註記後再保存')
        if body['expected_comment_revision'] != version.get('comment_revision'):
            raise branches.Busy('註記已更新，草稿仍保留；請重新開啟此版本的註記後再保存。')
        if body['comment'] != version.get('comment', ''):
            version['comment'] = body['comment']; version['comment_revision'] = uuid.uuid4().hex
    if 'liked' in body: version['liked'] = body['liked']
    if 'feedback_context' not in version and (version.get('liked') or version.get('comment')):
        # This is context available when marked, not a reconstructed model request.
        retained = pos >= 0 and identity(rows, pos)[0] == group
        clean = lambda ms:[{k:m[k] for k in ('role','content','index') if k in m} for m in ms]
        version['feedback_context'] = {'captured_at':stamp(), 'source_branch_id':bid,
            'before':clean(rows[max(0,pos-10):pos]) if retained else [],
            'after':clean(rows[pos+1:pos+5]) if retained else [],
            'retained_turn':retained}
    version['feedback_updated_at'] = stamp()
    files = [path(slug,bid), path(slug,bid).with_suffix('')/(group+'.json')]
    backups = {p:p.read_bytes() if p.exists() else None for p in files}
    try: save(slug,bid,archive)
    except Exception:
        for p,raw in backups.items():
            if raw is None: p.unlink(missing_ok=True)
            else:
                tmp = p.with_suffix('.restore'); tmp.write_bytes(raw); tmp.replace(p)
        raise
    return {'ok':True, 'feedback':feedback_view(version)}


def feedback_listing(slug, bid):
    index = load_index(slug,bid); items = []
    for gid,data in index['groups'].items():
        if not any(v.get('liked') or v.get('comment') for v in data['versions']): continue
        group = load(slug,bid,gid)['groups'][gid]
        for version in group['versions']:
            if version.get('liked') or version.get('comment'):
                items.append({**public_version(version), 'group_id':gid, 'message_index':group['message_index'],
                    'user_message':group['user_message'], 'context':deepcopy(version.get('feedback_context', {})),
                    'feedback_updated_at':version.get('feedback_updated_at')})
    return {'ok':True, 'items':items}


def copy_archive(slug, source, target, kept):
    """A fork inherits alternatives only for turns present in its retained prefix."""
    archive = load(slug, source)
    indices = {data['message_index'] for data in archive['groups'].values()}
    ids = {identity(kept, pos)[0] for pos,m in enumerate(kept)
           if m.get('index') in indices and m.get('role') == 'gm' and (pos == 0 or kept[pos-1].get('role') == 'user')}
    archive['groups'] = {k:v for k,v in archive['groups'].items() if k in ids}
    if archive['groups']: save(slug, target, archive)


def _edited_visibility(old, content):
    """Move witnessed text offsets, without interpreting events or updating memory."""
    visibility = deepcopy(old.get('ensemble_visibility'))
    if not isinstance(visibility, dict): return visibility
    original = old['content']
    changes = SequenceMatcher(None, original, content).get_opcodes()
    for ranges in visibility.values():
        if not isinstance(ranges, dict): continue
        spans = ranges.get('gm', [])
        if not isinstance(spans, list): continue
        valid = [s for s in spans if isinstance(s, (list, tuple)) and len(s) == 2
                 and all(type(v) is int for v in s) and 0 <= s[0] < s[1] <= len(original)]
        if valid == [[0, len(original)]]:
            ranges['gm'] = [[0, len(content)]]
            continue
        moved = []
        for a, b in valid:
            for kind, i, end, j, new_end in changes:
                if kind == 'equal':
                    start, stop = max(a, i), min(b, end)
                    if start < stop: moved.append([j+start-i, j+stop-i])
                elif kind == 'replace' and a <= i < end <= b and j < new_end:
                    moved.append([j, new_end])
                elif kind == 'insert' and a < i < b:
                    moved.append([j, new_end])
                # Edits crossing a witnessed boundary remain unshared.
        merged = []
        for a, b in sorted(moved):
            if merged and a <= merged[-1][1]: merged[-1][1] = max(b, merged[-1][1])
            else: merged.append([a, b])
        ranges['gm'] = merged
    return visibility


def edit_latest(slug, bid, body):
    """Caller owns the gameplay lock. Save prose only, preserving all state."""
    index, content = body.get('message_index'), body.get('content')
    if type(index) is not int:
        raise branches.BranchError('請指定要編輯的回覆')
    if not isinstance(content, str) or not content.strip() or len(content) > 200000:
        raise branches.BranchError('回覆不可空白，最多 200000 字')
    rows = messages(slug, bid)
    if (len(rows) < 3 or rows[-1].get('role') != 'gm' or rows[-1].get('index') != index
            or rows[-2].get('role') != 'user'):
        raise branches.BranchError('只能編輯最新的 GM 回覆，請重新載入對話')
    cast = e.enabled(slug)
    doc = e.load(slug, bid) if cast else None
    if cast and index <= doc['baseline_index']:
        raise branches.BranchError('不能編輯副本建立前的回覆')
    group, vid = identity(rows, len(rows)-1)
    if body.get('expected_version_id') != vid or body.get('expected_reply') != rows[-1]['content']:
        raise branches.Busy('回覆已改變，草稿仍保留；請重新載入後再編輯。')
    if any(branches.recording_blocks_write(m) for m in rows):
        raise branches.Busy('記憶正在整理，草稿仍保留，請完成後再保存。')
    if content == rows[-1]['content']:
        return {'ok': True, 'branch_id': bid, 'reply_version_id': vid, 'unchanged': True}
    archive = load(slug, bid, group)
    capture(archive, rows, len(rows)-1)
    updated = deepcopy(rows)
    updated[-2]['reply_group_id'] = group
    updated[-1].update(content=content, reply_group_id=group, reply_version_id=uuid.uuid4().hex,
                       reply_text_only_edit=True, reply_edited_at=stamp(), reply_edit_source_version_id=vid)
    if cast and 'ensemble_visibility' in rows[-1]:
        updated[-1]['ensemble_visibility'] = _edited_visibility(rows[-1], content)
    capture(archive, updated, len(updated)-1)
    message_path = e.path(slug, bid) if cast else Path(io._story_messages_path(slug, bid))
    files = [message_path, path(slug, bid), path(slug, bid).with_suffix('')/(group+'.json')]
    if not cast:
        files.append(Path(io._branch_dir(slug, bid))/'conversation_recap.json')
    backups = {p:p.read_bytes() if p.exists() else None for p in files}
    try:
        save(slug, bid, archive)
        if cast:
            doc['messages'] = updated
            if doc.get('source_recap', {}).get('compacted_through_index', -1) >= len(updated)-1:
                doc['source_recap'] = {}
            e.save(slug, doc, bid)
        else:
            # Reject an in-flight summary of the replaced prose; no model call.
            compaction.invalidate_recap(slug, bid, retained_message_count=len(updated)-1)
            io._save_json(str(message_path), updated)
    except Exception:
        for p, raw in backups.items():
            if raw is None: p.unlink(missing_ok=True)
            else:
                tmp = p.with_suffix('.restore'); tmp.write_bytes(raw); tmp.replace(p)
        raise
    return {'ok': True, 'branch_id': bid, 'reply_version_id': updated[-1]['reply_version_id']}


def select(slug, bid, body):
    """Caller owns the branch lock. Historical selections always create a fork."""
    from story_core.card_mode import pipeline, ensemble_recorder
    rows = messages(slug, bid)
    index = body.get('message_index')
    if type(index) is not int: raise branches.BranchError('請選擇要接續的回合')
    pos = next((i for i,m in enumerate(rows) if m.get('role') == 'gm' and m.get('index') == index), -1)
    if pos < 1 or rows[pos-1].get('role') != 'user': raise branches.BranchError('回合不存在')
    group, active_id = identity(rows, pos)
    if body.get('group_id') != group or body.get('expected_version_id') != active_id:
        raise branches.BranchError('此回合已改變，請重新載入版本')
    archive = load(slug, bid, group); capture(archive, rows, pos)
    entry = next((v for v in archive['groups'][group]['versions'] if v['id'] == body.get('version_id')), None)
    if entry is None: raise branches.BranchError('候選版本不存在')
    cast = e.enabled(slug); original_doc = e.load(slug, bid) if cast else None
    if cast and index <= original_doc['baseline_index']: raise branches.BranchError('不能套用副本建立前的版本')
    if body.get('version_id') == active_id: return {'ok': True, 'branch_id': bid}
    prefix = deepcopy(rows[:pos])
    gm = deepcopy(entry['message'])
    if cast:
        branches.ensure_settled(prefix[:-1], for_fork=True)
        if not isinstance(gm.get('ensemble_pre_snapshot'), dict) or not isinstance(gm.get('ensemble_snapshot'), dict):
            raise branches.BranchError('候選版本缺少完整快照')
        settled = gm.get('ensemble_record_status') == 'done' or gm.get('reply_text_only_edit') is True
        gm['ensemble_turn_id'] = uuid.uuid4().hex
        if not settled:
            gm['ensemble_record_status'] = 'pending'; gm.pop('ensemble_record_error', None)
            gm.pop('ensemble_updates', None); gm['ensemble_visibility'] = {}
            gm['ensemble_snapshot'] = deepcopy(gm['ensemble_pre_snapshot'])
            e.advance_clock(gm['ensemble_snapshot'], gm['content'])
    else:
        if not (isinstance(gm.get('pre_state_snapshot'), dict) and isinstance(gm.get('pre_npcs_snapshot'), list)
                and bool(gm.get('pre_clock_snapshot')) and isinstance(gm.get('clock_snapshot'), dict)):
            raise branches.BranchError('候選版本缺少完整快照')
        # Restore this version's own pre-state, keeping historical failures.
        # The replaced turn is cancellable; retained pending work still blocks.
        branches.ensure_snapshot_prefix(prefix[:-1], gm, before_turn=True)
        settled = gm.get('card_record_status') == 'done' or gm.get('reply_text_only_edit') is True
        if settled and not (isinstance(gm.get('state_snapshot'), dict) and isinstance(gm.get('npcs_snapshot'), list)):
            raise branches.BranchError('候選版本缺少完整快照')
        gm['card_turn_id'] = uuid.uuid4().hex
        if not settled:
            gm['card_record_status'] = 'pending'
            gm['card_record_pid'] = os.getpid()
            gm['state_snapshot'] = deepcopy(gm['pre_state_snapshot'])
            gm['npcs_snapshot'] = deepcopy(gm['pre_npcs_snapshot'])
    prefix[-1]['reply_group_id'] = group
    target = bid if pos == len(rows)-1 else 'card_'+uuid.uuid4().hex[:12]
    forked = target != bid
    dest = Path(io.STORIES_DIR) / slug / 'branches' / target
    if forked: dest.mkdir(parents=True, exist_ok=False)
    guard = (e.lock(slug, branch_id=target) if cast else branches.lock(slug, target)) if forked else None
    names = ['messages.json', 'character_state.json', 'npcs.json', 'card_clock.json',
             'conversation_recap.json', 'card_generation.json', 'card_candidates.json']
    if cast: names = ['ensemble.json', 'card_candidates.json']
    names.append('card_candidates/'+group+'.json')
    store = e.path(slug, bid).parent if cast else dest
    backups = {name:(store/name).read_bytes() if (store/name).exists() else None for name in names} if not forked else {}
    try:
        if guard: guard.__enter__()
        if cast:
            doc = deepcopy(original_doc); doc['messages'] = prefix+[gm]
            doc['state'] = deepcopy(gm['ensemble_snapshot'])
            if forked: doc.pop('ensemble_pending_controls', None)
            else: e.apply_controls(doc['state'], doc.get('ensemble_pending_controls', []))
            if doc.get('source_recap', {}).get('compacted_through_index', -1) >= len(prefix): doc['source_recap'] = {}
            e.save(slug, doc, target)
        else:
            branches.invalidate_generation(slug, target)
            if forked:
                recap = deepcopy(compaction.load_recap(slug, bid))
                io._save_json(str(dest/'conversation_recap.json'), recap)
            compaction.invalidate_recap(slug, target, retained_message_count=len(prefix))
            for field, name in [('state_snapshot', 'character_state.json'), ('npcs_snapshot', 'npcs.json'), ('clock_snapshot', 'card_clock.json')]:
                io._save_json(str(dest/name), gm[field])
            io._save_json(str(dest/'messages.json'), prefix+[gm])
        if forked:
            from story_core.card_mode import instructions
            instructions.inherit(slug,bid,target)
            ids = {identity(prefix+[gm], i)[0] for i,m in enumerate(prefix+[gm])
                   if m.get('role') == 'gm' and (i == 0 or (prefix+[gm])[i-1].get('role') == 'user')}
            child_archive = load(slug, bid)
            child_archive['groups'].update(deepcopy(archive['groups']))
            child_archive['groups'] = {k:v for k,v in child_archive['groups'].items() if k in ids}
            save(slug, target, child_archive)
            with branches.lock(slug, '_tree'):
                tree = branches.tree(slug)
                tree['branches'][target] = {'id':target, 'name':f'候選 #{index}', 'parent_branch_id':bid,
                    'branch_point_index': index, 'card_full_snapshot':True, 'created_at':stamp(), 'updated_at':stamp()}
                tree['active_branch_id'] = target
                io._save_json(io._story_tree_path(slug), tree)
        else: save(slug, bid, archive)
        sync_existing(slug, target, gm)
        if not settled:
            if cast: ensemble_recorder.start(slug, branch_id=target)
            else: pipeline.record_saved_turn(slug, target, gm, prefix[-1]['content'])
    except Exception:
        if forked:
            with branches.lock(slug, '_tree'):
                tree = branches.tree(slug)
                tree['branches'].pop(target, None)
                if tree.get('active_branch_id') == target: tree['active_branch_id'] = bid
                io._save_json(io._story_tree_path(slug), tree)
            shutil.rmtree(dest, ignore_errors=True)
        else:
            for name, raw in backups.items():
                p = store/name
                if raw is None: p.unlink(missing_ok=True)
                else:
                    tmp = p.with_suffix('.restore'); tmp.write_bytes(raw); tmp.replace(p)
        raise
    finally:
        if guard: guard.__exit__(None, None, None)
    status = gm.get('ensemble_record_status' if cast else 'card_record_status')
    return {'ok': True, 'branch_id': target, 'recorder_status': status if settled else 'pending'}
