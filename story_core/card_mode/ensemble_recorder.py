"""Durable, ordered recording without holding the gameplay lock during model I/O."""
from copy import deepcopy
import fcntl
import json
import logging
import threading

from story_core.card_mode import ensemble as e
from story_core.card_mode.memory import select_memories

log = logging.getLogger('rpg')
_workers = {}
_guard = threading.Lock()


def next_work(doc):
    # Stop behind a failed record: later updates need its corrected starting state.
    for message in doc.get('messages', []):
        status = message.get('ensemble_record_status')
        if status == 'error': return None
        if status == 'pending': return message
    return None


def status(doc):
    states = [m.get('ensemble_record_status') for m in doc.get('messages', [])]
    return {'status': 'error' if 'error' in states else 'pending' if 'pending' in states else 'done',
            'pending': states.count('pending'), 'failed': states.count('error')}


def rebuild(doc, turn_id, updates):
    """Apply a finished record and rebase every later retained snapshot in order."""
    messages = doc['messages']
    start = next(i for i, m in enumerate(messages) if m.get('ensemble_turn_id') == turn_id)
    state = deepcopy(messages[start]['ensemble_pre_snapshot'])
    for i in range(start, len(messages)):
        message = messages[i]
        if message['role'] != 'gm': continue
        if i != start: e.apply_controls(state, message.get('ensemble_controls_before', []))
        before = deepcopy(state)
        message['ensemble_pre_snapshot'] = before
        recorded = updates if i == start else message.get('ensemble_updates', [])
        user = messages[i-1]['content']
        e.apply_updates(state, recorded, '玩家：'+user+'\n敘事：'+message['content'])
        e.advance_clock(state, message['content'])
        message['ensemble_snapshot'] = deepcopy(state)
        if i == start:
            message['ensemble_updates'] = deepcopy(updates)
            message['ensemble_record_status'] = 'done'
            message.pop('ensemble_record_error', None)
        if message.get('ensemble_record_status') == 'done':
            message['ensemble_visibility'] = e._turn_visibility(before, recorded, user, message['content'])
    e.apply_controls(state, doc.get('ensemble_pending_controls', []))
    doc['state'] = state


def drain(slug, record_fn=None, branch_id="main"):
    # Separate cross-process lock serializes recorders, not players. Pending jobs
    # remain in ensemble.json after process death and resume on state read/send.
    with open(e.path(slug, branch_id).parent / '.ensemble-recorder.lock', 'a') as worker_lock:
        fcntl.flock(worker_lock, fcntl.LOCK_EX)
        while True:
            with e.lock(slug, blocking=True, branch_id=branch_id):
                doc = e.load(slug, branch_id)
                message = next_work(doc)
                if message is None: return
                turn_id = message['ensemble_turn_id']
                position = doc['messages'].index(message)
                user = doc['messages'][position-1]['content']
                evidence = '玩家：'+user+'\n敘事：'+message['content']
                roster = deepcopy(message['ensemble_pre_snapshot']['characters'])
                for ch in roster.values(): ch['memory'] = select_memories(ch['memory'], evidence)
            error = None
            try:
                updates = e._metered_call(slug, 'ensemble_recorder', record_fn or e.record,
                    e.RECORD_PROMPT+'\n'+json.dumps(roster, ensure_ascii=False), evidence, branch_id=branch_id)
            except Exception:
                log.exception('ensemble recorder failed for %s turn %s', slug, turn_id)
                error = '記憶整理失敗；對話已保存，可重試整理。'
            with e.lock(slug, blocking=True, branch_id=branch_id):
                current = e.load(slug, branch_id)
                retained = next((m for m in current['messages']
                                 if m.get('ensemble_turn_id') == turn_id), None)
                if retained is None or retained.get('ensemble_record_status') != 'pending':
                    continue  # Removed/replaced turns can never write into the new timeline.
                if error is None:
                    candidate = deepcopy(current)
                    try:
                        rebuild(candidate, turn_id, updates)
                    except Exception:
                        log.exception('ensemble recorder validation failed for %s turn %s', slug, turn_id)
                        error = '記憶整理未通過檢查；對話已保存，可重試整理。'
                    else:
                        e.save(slug, candidate, branch_id)
                        from story_core.card_mode import candidates
                        for saved in candidate['messages']:
                            if saved.get('role') == 'gm' and saved['index'] >= retained['index']:
                                candidates.sync_existing(slug, branch_id, saved)
                if error:
                    retained['ensemble_record_status'] = 'error'
                    retained['ensemble_record_error'] = error
                    e.save(slug, current, branch_id)
                    from story_core.card_mode import candidates
                    candidates.sync_existing(slug, branch_id, retained)
                    return


def _start(slug, record_fn=None, branch_id="main"):
    key = str(e.path(slug, branch_id).resolve())
    with _guard:
        if key in _workers: return _workers[key]
        if not next_work(e.load(slug, branch_id)): return None
        def run():
            failed = False
            try:
                drain(slug, record_fn, branch_id)
            except Exception:
                failed = True
                log.exception('ensemble background worker interrupted for %s', slug)
            finally:
                with _guard: _workers.pop(key, None)
                # Catch an enqueue between drain's final read and worker removal.
                # Infrastructure failure is retried by the next request, not a hot loop.
                if not failed and e.enabled(slug): start(slug, record_fn, branch_id)
        worker = threading.Thread(target=run, name='ensemble-recorder-'+slug, daemon=True)
        _workers[key] = worker
        try:
            worker.start()
        except Exception:
            _workers.pop(key, None)
            raise
        return worker


def start(slug, record_fn=None, branch_id="main"):
    try:
        return _start(slug, record_fn, branch_id)
    except Exception:
        # A saved narration stays successful if scheduling fails. Pending work
        # is durable and the next state poll can retry starting the worker.
        log.exception('could not start ensemble recorder for %s', slug)
        return None
