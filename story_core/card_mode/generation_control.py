"""Bound external work without allowing abandoned workers to commit story state."""
from contextlib import contextmanager
from queue import Queue, Empty
import threading
import time

from story_core.card_mode import branches

TIMEOUT_SECONDS = 180
_active = {}
_guard = threading.Lock()
_local = threading.local()


class Cancelled(branches.BranchError):
    pass


def check():
    event = getattr(_local, 'event', None)
    if event is not None and event.is_set():
        raise Cancelled('舊生成已取消，未寫入回覆。')


@contextmanager
def turn(slug, bid):
    key = (slug, bid)
    event = threading.Event()
    with _guard:
        previous = _active.get(key)
        if previous:
            previous.set()
        _active[key] = event
    _local.event = event
    try:
        deadline = time.monotonic() + 3
        while True:
            check()
            manager = branches.lock(slug, bid)
            try:
                manager.__enter__()
                break
            except branches.Busy:
                if not previous or time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
        try:
            check()
            yield
        finally:
            manager.__exit__(None, None, None)
    finally:
        event.set()
        with _guard:
            if _active.get(key) is event:
                del _active[key]
        _local.event = None


def external(fn, emit_chunk=None):
    """fn may call providers, but must not write gameplay state."""
    event = getattr(_local, 'event', None)
    if event is None:
        return fn(emit_chunk)
    output = Queue()
    def emit(text):
        if not event.is_set():
            output.put(('text', text))
    def work():
        try:
            output.put(('result', fn(emit if emit_chunk else None)))
        except BaseException as exc:
            output.put(('error', exc))
    threading.Thread(target=work, daemon=True, name='card-provider').start()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        check()
        if time.monotonic() >= deadline:
            event.set()
            raise Cancelled('生成等待超過 180 秒，已取消；未保存本次生成結果，請重試。')
        try:
            kind, value = output.get(timeout=.1)
        except Empty:
            continue
        check()
        if kind == 'text':
            emit_chunk(value)
        elif kind == 'error':
            raise value
        else:
            return value
