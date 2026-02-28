"""Tests for llm_trace structured event logging."""

import json
from datetime import datetime, timezone
from pathlib import Path

import llm_trace


def test_write_trace_creates_partitioned_file(tmp_path):
    data_dir = tmp_path / "data"
    now = datetime(2026, 2, 28, 12, 34, 56, tzinfo=timezone.utc)

    out = llm_trace.write_trace(
        data_dir=str(data_dir),
        story_id="story_original",
        branch_id="branch_abc123",
        message_index=407,
        stage="gm_request",
        payload={"k": "v"},
        source="/api/send",
        tags={"mode": "sync"},
        now_utc=now,
    )

    assert out is not None
    out_path = Path(out)
    assert out_path.exists()
    assert "llm_traces/story_original/2026-02-28/branch_abc123/msg_000407/" in out

    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["story_id"] == "story_original"
    assert data["branch_id"] == "branch_abc123"
    assert data["message_index"] == 407
    assert data["stage"] == "gm_request"
    assert data["source"] == "/api/send"
    assert data["payload"] == {"k": "v"}
    assert data["tags"] == {"mode": "sync"}


def test_write_trace_prunes_old_date_directories(tmp_path):
    data_dir = tmp_path / "data"
    root = data_dir / "llm_traces" / "story_original"
    old_day = root / "2026-01-01"
    old_day.mkdir(parents=True, exist_ok=True)
    (old_day / "stale.json").write_text("{}", encoding="utf-8")

    now = datetime(2026, 2, 28, 12, 0, 0, tzinfo=timezone.utc)
    out = llm_trace.write_trace(
        data_dir=str(data_dir),
        story_id="story_original",
        branch_id="main",
        message_index=1,
        stage="extract_tags_request",
        payload={"prompt": "x"},
        retention_days=14,
        now_utc=now,
    )

    assert out is not None
    assert not old_day.exists()

    expected_day = root / "2026-02-28"
    assert expected_day.exists()
