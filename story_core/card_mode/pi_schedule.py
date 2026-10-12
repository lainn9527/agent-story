"""Opt-in, per-branch Pi maintenance after completed player/GM pairs.

No work on state polling; idle stories cost no API calls. Receipt/revision gates
remain authoritative. Failures retry on subsequent player turns up to a budget.
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
_active_jobs: dict[tuple[str, str], dict] = {}
_mutex = threading.Lock()
MAX_RETRIES = 3


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


def is_running(slug, bid, kind=None):
    """Check if a background Pi task is currently active for this story branch."""
    key = (slug, bid)
    with _mutex:
        job = _active_jobs.get(key)
        if not job:
            return False
        if kind is not None:
            return kind in job.get('kinds', ())
        return True


def cancel(slug, bid):
    """Cancel an active background Pi run on rewind, regeneration, or branch deletion."""
    key = (slug, bid)
    with _mutex:
        job = _active_jobs.get(key)
        if job:
            job['cancel_event'].set()


def configure(slug, bid, *, cli, every=100, kinds=('memory','lore','summary','inventory')):
    if type(every) is not int or every < 1 or not Path(cli).is_file():
        raise ValueError('需要正整數回合間隔與既有 Pi executable 路徑')
    if not kinds or set(kinds) - {'memory','lore','summary','inventory'}:
        raise ValueError('未知整理種類')
    with branches.lock(slug, bid, blocking=True):
        branches.resolve(slug, bid)
        count, ids = _progress(io._load_branch_messages(slug, bid))
        _save(ledger_path(slug, bid), {'last_attempt_pairs': count, 'last_successful_pairs': count,
                                       'anchor': ids[-1] if ids else None,
                                       'status': 'enabled', 'every_pairs': every, 'retry_count': 0})
        _save(config_path(slug), {'enabled': True, 'every_pairs': every, 'pi_cli': str(Path(cli).resolve()),
                                 'kinds': list(kinds), 'timeout_seconds': 600, 'max_retries': MAX_RETRIES})


def maybe_schedule(slug, bid):
    """Return immediately; called after Recorder finishes, never inside its lock."""
    try:
        config = _read(config_path(slug))
        if not config.get('enabled'):
            return
        key = (slug, bid)
        with _mutex:
            if key in _active_jobs:
                return
        def worker():
            try:
                run_due(slug, bid)
            except Exception:
                log.exception('Pi scheduled maintenance failed: %s/%s', slug, bid)
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
                ledger = {'last_attempt_pairs': count, 'last_successful_pairs': count,
                          'anchor': ids[-1] if ids else None, 'status':'baseline', 'retry_count': 0}
                _save(path, ledger)
                return ledger

            # Check if previous attempt failed and is eligible for retry on new player turn
            last_status = ledger.get('status')
            retry_count = ledger.get('retry_count', 0)
            max_retries = config.get('max_retries', MAX_RETRIES)
            # Only transient failures (timeout, busy branch, rewound history) retry early;
            # a rejected proposal waits for the next interval instead of burning API calls.
            is_failure = last_status == 'rejected' and ledger.get('retryable')
            turns_since_attempt = count - ledger.get('last_attempt_pairs', 0)

            if turns_since_attempt < 1:
                return {'status': 'not_due'}

            if is_failure and retry_count < max_retries:
                # Retry on new player turn
                is_retry = True
            elif count - ledger.get('interval_anchor_pairs', ledger.get('last_attempt_pairs', 0)) >= config['every_pairs']:
                is_retry = False
            else:
                return {'status': 'not_due'}

            ledger.update(last_attempt_pairs=count, anchor=ids[-1] if ids else None,
                          status='running', started_at=datetime.now(timezone.utc).isoformat(),
                          retry_count=(retry_count + 1) if is_retry else 0)
            if not is_retry:
                ledger['interval_anchor_pairs'] = count
            _save(path, ledger)

        key = (slug, bid)
        cancel_event = threading.Event()
        with _mutex:
            _active_jobs[key] = {'cancel_event': cancel_event, 'kinds': config.get('kinds', ())}
        try:
            frozen = tasks.freeze(slug, bid, kinds=config['kinds'], recent=20)
            vertex = io._load_json('llm_config.json', {}).get('vertex', {})
            stale = tasks.staleness_watcher(frozen)
            cancel_cb = lambda: cancel_event.is_set() or stale()
            node_bin = config.get('node_bin') or tasks.resolve_node()
            report = tasks.run(frozen, cli=config['pi_cli'], vertex=vertex,
                               timeout=config.get('timeout_seconds', 600), cancel=cancel_cb,
                               node=node_bin)
            ledger.update(run_dir=report.get('run_dir'), status=report['status'], error=report.get('error'),
                          retryable=bool(report.get('retryable')))
            if report['status'] == 'cancelled':
                ledger['retry_count'] = 0
            elif report['status'] == 'verified' and not _read(config_path(slug)).get('enabled'):
                ledger['status'] = 'disabled_before_apply'
            elif report['status'] == 'verified':
                # Rebase over turns appended during background generation and wait up to 60s for locks
                ledger['apply'] = tasks.apply(report['run_dir'], wait=60, rebase=True, node=node_bin)
                ledger['status'] = ledger['apply']['status']
                if ledger['status'] in ('applied', 'no_changes'):
                    ledger['last_successful_pairs'] = count
                    ledger['retry_count'] = 0
        except tasks.Cancelled as e:
            ledger.update(status='cancelled', error=str(e), retry_count=0)
        except Exception as error:
            ledger.update(status='rejected', error=str(error),
                          retryable=isinstance(error, (branches.Busy, tasks.Stale)))
        finally:
            with _mutex:
                _active_jobs.pop(key, None)
        ledger['finished_at'] = datetime.now(timezone.utc).isoformat()
        with branches.lock(slug, bid, blocking=True):
            current = _read(path)
            if current.get('started_at') == ledger.get('started_at') and current.get('anchor') == ledger.get('anchor'):
                _save(path, ledger)
        return ledger


def reconcile_after_rewind(slug, bid, messages):
    """Called under the branch lock, immediately after retaining a shorter history."""
    cancel(slug, bid)
    path = ledger_path(slug, bid)
    ledger = _read(path)
    if not ledger:
        return
    count, ids = _progress(messages)
    if ((ledger.get('anchor') and ledger['anchor'] not in ids)
            or count < ledger.get('last_attempt_pairs', 0)):
        ledger.update(last_attempt_pairs=count, last_successful_pairs=count,
                      interval_anchor_pairs=count, anchor=ids[-1] if ids else None,
                      status='rewind_baseline', retry_count=0)
        _save(path, ledger)
