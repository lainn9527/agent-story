"""Opt-in, per-branch Pi maintenance after completed player/GM pairs.

No work on state polling; idle stories cost no API calls. Receipt/revision gates
remain authoritative. Failures wait until another full interval, avoiding loops.
"""
import fcntl
import json
import logging
from pathlib import Path
import threading
from datetime import datetime, timezone
from story_core import story_io as io
from story_core.card_mode import branches, pi_maintenance as tasks

log = logging.getLogger('rpg')
_active = set()
_mutex = threading.Lock()


def config_path(slug):
    return Path(io._story_dir(slug)) / 'pi_schedule.json'


def ledger_path(slug, bid):
    return Path(io._story_messages_path(slug, bid)).parent / 'pi_schedule_status.json'


def _read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def _save(path, doc):
    tasks._write(path, json.dumps(doc, ensure_ascii=False, indent=2).encode())


def _progress(messages):
    pairs = [gm for user, gm in zip(messages, messages[1:])
             if user.get('role') == 'user' and gm.get('role') == 'gm'
             and gm.get('card_record_status', 'done') == 'done']
    ids = [gm.get('card_turn_id') or f"{gm['index']}:{tasks._hash(gm['content'].encode())}" for gm in pairs]
    return len(pairs), ids


def configure(slug, bid, *, cli, every=50, kinds=('memory','lore','summary','inventory')):
    if type(every) is not int or every < 1 or not Path(cli).is_file():
        raise ValueError('需要正整數回合間隔與既有 Pi executable 路徑')
    if not kinds or set(kinds) - {'memory','lore','summary','inventory'}:
        raise ValueError('未知整理種類')
    with branches.lock(slug, bid, blocking=True):
        branches.resolve(slug, bid)
        count, ids = _progress(io._load_branch_messages(slug, bid))
        _save(ledger_path(slug, bid), {'last_attempt_pairs': count, 'anchor': ids[-1] if ids else None,
                                     'status': 'enabled', 'every_pairs': every})
        _save(config_path(slug), {'enabled': True, 'every_pairs': every, 'pi_cli': str(Path(cli).resolve()),
                                 'kinds': list(kinds), 'timeout_seconds': 300})


def maybe_schedule(slug, bid):
    """Return immediately; called after Recorder finishes, never inside its lock."""
    try:
        config = _read(config_path(slug))
        if not config.get('enabled'):
            return
        key = (slug, bid)
        with _mutex:
            if key in _active:
                return
            _active.add(key)
        def worker():
            try:
                run_due(slug, bid)
            except Exception:
                log.exception('Pi scheduled maintenance failed: %s/%s', slug, bid)
            finally:
                with _mutex:
                    _active.discard(key)
        threading.Thread(target=worker, name=f'pi-maintenance-{slug}-{bid}', daemon=True).start()
    except Exception:
        log.exception('Cannot schedule Pi maintenance')


def run_due(slug, bid):
    config = _read(config_path(slug))
    if not config.get('enabled'):
        return {'status':'disabled'}
    path = ledger_path(slug, bid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status':'busy'}
        with branches.lock(slug, bid, blocking=True):
            branches.resolve(slug, bid)
            messages = io._load_branch_messages(slug, bid)
            branches.ensure_settled(messages)
            if not messages or messages[-1].get('role') != 'gm':
                return {'status':'unsettled'}
            count, ids = _progress(messages)
            ledger = _read(path)
            # New branches and rollback/regeneration of the anchor start a fresh interval.
            if not ledger or (ledger.get('anchor') and ledger['anchor'] not in ids) or count < ledger.get('last_attempt_pairs',0):
                ledger = {'last_attempt_pairs': count, 'anchor': ids[-1] if ids else None, 'status':'baseline'}
                _save(path, ledger)
                return ledger
            if count - ledger['last_attempt_pairs'] < config['every_pairs']:
                return {'status':'not_due'}
            ledger.update(last_attempt_pairs=count, anchor=ids[-1] if ids else None,
                          status='running', started_at=datetime.now(timezone.utc).isoformat())
            _save(path, ledger)
        try:
            frozen = tasks.freeze(slug, bid, kinds=config['kinds'], recent=20)
            vertex = io._load_json('llm_config.json', {}).get('vertex', {})
            report = tasks.run(frozen, cli=config['pi_cli'], vertex=vertex, timeout=config.get('timeout_seconds',300))
            ledger.update(run_dir=report['run_dir'], status=report['status'], error=report.get('error'))
            if report['status'] == 'verified' and not _read(config_path(slug)).get('enabled'):
                ledger['status'] = 'disabled_before_apply'
            elif report['status'] == 'verified':
                ledger['apply'] = tasks.apply(report['run_dir'])
                ledger['status'] = ledger['apply']['status']
        except Exception as error:
            ledger.update(status='rejected', error=str(error))
        ledger['finished_at'] = datetime.now(timezone.utc).isoformat()
        with branches.lock(slug, bid, blocking=True):
            current = _read(path)
            if current.get('started_at') == ledger.get('started_at') and current.get('anchor') == ledger.get('anchor'):
                _save(path, ledger)
        return ledger


def reconcile_after_rewind(slug, bid, messages):
    """Called under the branch lock, immediately after retaining a shorter history."""
    path = ledger_path(slug, bid)
    ledger = _read(path)
    if not ledger:
        return
    count, ids = _progress(messages)
    if ((ledger.get('anchor') and ledger['anchor'] not in ids)
            or count < ledger.get('last_attempt_pairs', 0)):
        ledger.update(last_attempt_pairs=count, anchor=ids[-1] if ids else None,
                      status='rewind_baseline')
        _save(path, ledger)
