import threading
from pathlib import Path
import pytest
from tests.test_card_ensemble import world
from story_core import story_io as io
from story_core.card_mode import generation_control as control, branches
from story_core.card_mode.pipeline import run_card_turn


def test_stalled_generation_is_superseded_without_late_write(world):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors, chunks = [], []
    def slow(*args):
        entered.set()
        release.wait(5)
        finished.set()
        return '舊結果'
    def old():
        try:
            run_card_turn('original', user_message='舊輸入', llm_fn=slow, emit_chunk=chunks.append)
        except control.Cancelled as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=old)
    thread.start()
    assert entered.wait(2)
    try:
        result = run_card_turn('original', user_message='新輸入', llm_fn=lambda *a:'新結果',
                               recorder_fn=lambda *a:None, async_recorder=False)
        thread.join(2)
        assert errors and not thread.is_alive() and result['ok']
        path = Path(io._story_messages_path('original', 'main'))
        before = path.read_bytes()
        release.set()
        assert finished.wait(2)
        assert path.read_bytes() == before
        assert '舊結果' not in path.read_text() and not chunks
        assert '新結果' in path.read_text()
    finally:
        release.set()
        thread.join(2)


def test_timeout_releases_lock_and_preserves_messages(world, monkeypatch):
    monkeypatch.setattr(control, 'TIMEOUT_SECONDS', .05)
    release = threading.Event()
    path = Path(io._story_messages_path('original', 'main'))
    before = path.read_bytes()
    try:
        with pytest.raises(control.Cancelled, match='超過'):
            run_card_turn('original', user_message='逾時輸入', llm_fn=lambda *a: release.wait(2))
        with branches.lock('original', 'main'):
            assert path.read_bytes() == before
    finally:
        release.set()
