"""Card timelines are independent snapshots; parent links are navigation only."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import hashlib
import os
import tempfile
import re
import shutil
import threading
import uuid

from story_core import story_io as io, compaction


class BranchError(ValueError):
    pass


class Busy(BranchError):
    pass


_local = threading.local()


def tree(slug):
    if not io._is_valid_story_id(slug):
        raise BranchError('無效角色卡')
    result = io._load_json(io._story_tree_path(slug), {})
    if not result.get('branches'):
        result = {'active_branch_id': 'main', 'root_branch_id': 'main',
                  'branches': {'main': {'id': 'main', 'name': '主線',
                    'parent_branch_id': None, 'branch_point_index': -1}}}
    return result


def resolve(slug, branch_id=None):
    doc = tree(slug)
    bid = doc.get('active_branch_id', 'main') if branch_id is None else branch_id
    if not isinstance(bid, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', bid):
        raise BranchError('無效分支')
    meta = doc['branches'].get(bid)
    if not meta or any(meta.get(k) for k in ('deleted', 'merged', 'pruned')):
        raise BranchError('分支不存在')
    return bid


@contextmanager
def lock(slug, branch_id='main', blocking=False):
    """Reentrant per-thread, cross-process lock for single-card writes."""
    root = Path(io._story_dir(slug))
    if not root.is_dir():
        raise BranchError('角色卡尚未建立')
    lock_root = Path(tempfile.gettempdir()) / 'story-card-locks'
    lock_root.mkdir(mode=0o700, exist_ok=True)
    key = hashlib.sha256((str(root.resolve()) + ':' + branch_id).encode()).hexdigest()
    path = str(lock_root / key)
    held = getattr(_local, 'held', set())
    if path in held:
        yield
        return
    with open(path, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Busy('此分支正在處理回合，請稍後再試。')
        _local.held = held | {path}
        try:
            yield
        finally:
            _local.held = held
            fcntl.flock(handle, fcntl.LOCK_UN)


def _reviewed_memory_repair(message):
    """Accept audited memory repairs without relabeling all Recorder work as done."""
    repair = message.get('card_memory_backfill')
    return (
        message.get('role') == 'gm'
        and isinstance(repair, dict)
        and repair.get('status') == 'reviewed'
        and isinstance(repair.get('at'), str) and bool(repair['at'].strip())
        and type(repair.get('added')) is int and repair['added'] >= 0
        and isinstance(message.get('state_snapshot'), dict)
        and isinstance(message.get('npcs_snapshot'), list)
        and bool(message['npcs_snapshot'])
        and isinstance(message.get('clock_snapshot'), dict)
        and bool(message['clock_snapshot'])
    )


def recording_blocks_write(message):
    """Dead single-card owners cannot finish; ensemble jobs may resume elsewhere."""
    if message.get('ensemble_record_status') == 'pending':
        return True
    if message.get('card_record_status') != 'pending':
        return False
    pid = message.get('card_record_pid')
    if type(pid) is not int or pid <= 0:
        return True  # No reliable owner information: don't assume cancellation.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # Permission/inspection failures aren't evidence of death.
    return True


def ensure_settled(messages, for_fork=False):
    for message in messages:
        if message.get('ensemble_record_status') in ('pending', 'error'):
            raise Busy('記憶尚未整理完成，請等候或重試整理後再建立分支。')
        status = message.get('card_record_status')
        if recording_blocks_write(message):
            raise Busy('此分支的記憶正在整理，請稍後再試。')
        if for_fork and status in ('pending', 'error') and not (
            status == 'error' and _reviewed_memory_repair(message)
        ):
            raise BranchError('此回合的記憶整理未完成，請重新生成，或從更早的完整回合分支。')


def ensure_snapshot_prefix(messages, checkpoint, *, before_turn=False):
    """A complete single-card checkpoint survives earlier failed recordings.

    Keep failures in the copied history; only the validation view ignores them.
    Confirmed dead single-card owners can cross a later complete checkpoint;
    live/unknown pending work and ensemble failures still block.
    The caller separately checks a retained checkpoint's own recording status.
    """
    prefix = 'pre_' if before_turn else ''
    if not (isinstance(checkpoint.get(prefix + 'state_snapshot'), dict)
            and isinstance(checkpoint.get(prefix + 'npcs_snapshot'), list)
            and bool(checkpoint.get(prefix + 'npcs_snapshot'))
            and isinstance(checkpoint.get(prefix + 'clock_snapshot'), dict)
            and bool(checkpoint.get(prefix + 'clock_snapshot'))):
        raise BranchError('這個回合缺少完整快照，無法安全分支或編輯')
    checked = [{**m, 'card_record_status': None}
               if (m.get('card_record_status') == 'error'
                   or (m.get('card_record_status') == 'pending'
                       and not recording_blocks_write(m))) else m for m in messages]
    ensure_settled(checked, for_fork=True)


def listing(slug):
    doc = tree(slug)
    return {'active_branch_id': doc.get('active_branch_id', 'main'),
            'branches': [v for v in doc['branches'].values()
                         if not any(v.get(k) for k in ('deleted', 'merged', 'pruned'))]}


def change(slug, action, body):
    from story_core.card_mode import ensemble as e
    from story_core.card_mode.card import load_card
    load_card(slug)  # Never create trees for arbitrary or world-mode IDs.
    if action == 'delete':
        bid = resolve(slug, body.get('branch_id'))
        guard = e.lock(slug, branch_id=bid) if e.enabled(slug) else lock(slug, bid)
        with guard, lock(slug, '_tree'):
            bid = resolve(slug, bid)
            doc = tree(slug)
            if bid in ('main', doc.get('root_branch_id')):
                raise BranchError('主線不能刪除')
            parent = resolve(slug, doc['branches'][bid].get('parent_branch_id') or 'main')
            messages = e.load(slug, bid)['messages'] if e.enabled(slug) else io._load_branch_messages(slug, bid)
            if any(m.get('card_record_status') == 'pending' or
                   m.get('ensemble_record_status') == 'pending' for m in messages):
                raise Busy('此分支的記憶正在整理，請稍後再刪除。')
            # Match world-mode reparenting, but full snapshots need no inherited delta.
            for child in doc['branches'].values():
                if child.get('parent_branch_id') == bid:
                    child['parent_branch_id'] = parent
            # Keep files for recovery; resolve() rejects deleted timelines.
            doc['branches'][bid]['deleted'] = True
            doc['branches'][bid]['deleted_at'] = datetime.now(timezone.utc).isoformat()
            if doc.get('active_branch_id') == bid:
                doc['active_branch_id'] = parent
            io._save_json(io._story_tree_path(slug), doc)
        return {'ok': True, 'branch_id': parent, 'switch_to': parent, **listing(slug)}
    if action != 'fork':
        with lock(slug, '_tree'):
            doc = tree(slug)
            bid = resolve(slug, body.get('branch_id'))
            if action == 'switch':
                doc['active_branch_id'] = bid
            elif action == 'rename':
                doc['branches'][bid]['name'] = _name(body.get('name'))
            else:
                raise BranchError('不支援的分支操作')
            io._save_json(io._story_tree_path(slug), doc)
        return {'ok': True, 'branch_id': bid, **listing(slug)}

    source = resolve(slug, body.get('branch_id'))
    is_ensemble = e.enabled(slug)
    guard = e.lock(slug, branch_id=source) if is_ensemble else lock(slug, source)
    with guard:
        if is_ensemble:
            source_doc = e.load(slug, source)
            messages = source_doc['messages']
        else:
            messages = io._load_branch_messages(slug, source)
        index = body.get('message_index', messages[-1]['index'] if messages else None)
        if isinstance(index, bool) or not isinstance(index, int):
            raise BranchError('請選擇已完成的角色回應')
        selected = next((m for m in messages if m.get('index') == index and m.get('role') == 'gm'), None)
        if selected is None:
            raise BranchError('只能從已完成的角色回應建立分支')
        if is_ensemble and (index < source_doc['baseline_index'] or 'ensemble_snapshot' not in selected):
            raise BranchError('只能從多角色副本建立後的回合分支')
        kept = deepcopy([m for m in messages if m['index'] <= index])
        if is_ensemble:
            ensure_settled(kept, for_fork=True)
        else:
            ensure_snapshot_prefix(kept[:-1], selected)
            ensure_settled([selected], for_fork=True)
        latest = index == messages[-1]['index']
        name = _name(body.get('name', f'從 #{index} 分支'))
        bid = 'card_' + uuid.uuid4().hex[:12]
        dest = Path(io.STORIES_DIR) / slug / 'branches' / bid
        with lock(slug, '_tree'):
            doc = tree(slug)
            dest.mkdir(parents=True, exist_ok=False)
            try:
                if is_ensemble:
                    child = deepcopy(source_doc)
                    child['messages'] = kept
                    child['state'] = deepcopy(source_doc['state'] if latest else selected['ensemble_snapshot'])
                    if not latest:
                        child.pop('ensemble_pending_controls', None)
                    e.save(slug, child, bid)
                else:
                    for field, filename in [('state_snapshot', 'character_state.json'),
                                            ('npcs_snapshot', 'npcs.json'), ('clock_snapshot', 'card_clock.json')]:
                        live = Path(io._branch_dir(slug, source)) / filename
                        value = io._load_json(str(live), None) if latest else selected.get(field)
                        if value is None:
                            raise BranchError('這個舊回合缺少完整快照，無法安全分支')
                        io._save_json(str(dest / filename), value)
                    io._save_json(str(dest / 'messages.json'), kept)
                    recap = io._load_json(str(Path(io._branch_dir(slug, source)) / 'conversation_recap.json'), {})
                    recap = compaction.rollback_recap(recap, len(kept))
                    recap['revision'] = uuid.uuid4().hex
                    io._save_json(str(dest / 'conversation_recap.json'), recap)
                from story_core.card_mode import candidates, instructions, lore
                candidates.copy_archive(slug, source, bid, kept)
                instructions.inherit(slug,source,bid)
                lore.inherit(slug, source, bid)
                stamp = datetime.now(timezone.utc).isoformat()
                doc['branches'][bid] = {'id': bid, 'name': name, 'parent_branch_id': source,
                    'branch_point_index': index, 'card_full_snapshot': True,
                    'created_at': stamp, 'updated_at': stamp}
                doc['active_branch_id'] = bid
                io._save_json(io._story_tree_path(slug), doc)
            except Exception:
                shutil.rmtree(dest)
                raise
    return {'ok': True, 'branch_id': bid, **listing(slug)}


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
        raise BranchError('分支名稱須為 1–80 字')
    return value.strip()


def generation(slug, branch_id):
    return io._load_json(str(Path(io._branch_dir(slug, branch_id)) / 'card_generation.json'), {}).get('id')


def invalidate_generation(slug, branch_id):
    token = uuid.uuid4().hex
    io._save_json(str(Path(io._branch_dir(slug, branch_id)) / 'card_generation.json'), {'id':token})
    return token
