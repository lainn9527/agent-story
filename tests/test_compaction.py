"""Tests for compaction.py (Phase 2.3).

Tests should_compact, _format_messages, get_context_window,
copy_recap_to_branch, load/save_recap, get_recap_text.
Uses monkeypatched STORIES_DIR for filesystem isolation.
"""

import json

import pytest

import compaction


@pytest.fixture(autouse=True)
def patch_stories_dir(tmp_path, monkeypatch):
    """Redirect compaction STORIES_DIR to tmp_path."""
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir()
    monkeypatch.setattr(compaction, "STORIES_DIR", str(stories_dir))
    # Clear locks
    compaction._compact_locks.clear()
    return stories_dir


@pytest.fixture
def story_id():
    return "test_story"


@pytest.fixture
def setup_branch(tmp_path, story_id):
    """Create branch directory for recap storage."""
    def _setup(branch_id="main", recap=None):
        branch_dir = tmp_path / "stories" / story_id / "branches" / branch_id
        branch_dir.mkdir(parents=True, exist_ok=True)
        if recap:
            (branch_dir / "conversation_recap.json").write_text(
                json.dumps(recap, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return branch_dir
    return _setup


def _make_timeline(n, start_index=0):
    """Generate a fake timeline with n messages."""
    msgs = []
    for i in range(n):
        idx = start_index + i
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"index": idx, "role": role, "content": f"Message {idx}"})
    return msgs


# ===================================================================
# should_compact — pure logic
# ===================================================================


class TestShouldCompact:
    def test_no_compaction_needed(self):
        recap = {"compacted_through_index": -1}
        # 40 messages, RECENT_WINDOW=20 → recent_start=20, uncompacted=20-0=20
        # MIN_UNCOMPACTED_FOR_TRIGGER=20 → NOT > 20 → False
        assert compaction.should_compact(recap, 40) is False

    def test_compaction_needed(self):
        recap = {"compacted_through_index": -1}
        # 50 messages → recent_start=30, uncompacted=30-0=30 > 20 → True
        assert compaction.should_compact(recap, 50) is True

    def test_after_previous_compaction(self):
        recap = {"compacted_through_index": 19}
        # 60 messages → recent_start=40, uncompacted=40-20=20 → NOT > 20 → False
        assert compaction.should_compact(recap, 60) is False

    def test_after_previous_compaction_more_messages(self):
        recap = {"compacted_through_index": 19}
        # 61 messages → recent_start=41, uncompacted=41-20=21 > 20 → True
        assert compaction.should_compact(recap, 61) is True

    def test_small_timeline(self):
        recap = {"compacted_through_index": -1}
        assert compaction.should_compact(recap, 10) is False

    def test_empty_recap(self):
        recap = {}
        # Default compacted_through_index = -1
        assert compaction.should_compact(recap, 50) is True

    def test_exact_threshold(self):
        recap = {"compacted_through_index": -1}
        # 41 messages → recent_start=21, uncompacted=21-0=21 > 20 → True
        assert compaction.should_compact(recap, 41) is True


# ===================================================================
# get_context_window — pure logic
# ===================================================================


class TestGetContextWindow:
    def test_returns_last_20(self):
        timeline = _make_timeline(50)
        window = compaction.get_context_window(timeline)
        assert len(window) == 20
        assert window[0]["index"] == 30
        assert window[-1]["index"] == 49

    def test_short_timeline(self):
        timeline = _make_timeline(5)
        window = compaction.get_context_window(timeline)
        assert len(window) == 5

    def test_empty_timeline(self):
        window = compaction.get_context_window([])
        assert window == []

    def test_exactly_20(self):
        timeline = _make_timeline(20)
        window = compaction.get_context_window(timeline)
        assert len(window) == 20


# ===================================================================
# _format_messages
# ===================================================================


class TestFormatMessages:
    def test_basic_format(self):
        msgs = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "歡迎來到主神空間"},
        ]
        text = compaction._format_messages(msgs)
        assert "【玩家】" in text
        assert "【GM】" in text
        assert "你好" in text
        assert "歡迎來到主神空間" in text

    def test_truncation_at_1000_chars(self):
        msgs = [
            {"role": "user", "content": "A" * 1500},
        ]
        text = compaction._format_messages(msgs)
        assert "…（略）" in text
        # Original 1500 chars should be truncated
        assert "A" * 1001 not in text

    def test_short_message_not_truncated(self):
        msgs = [
            {"role": "user", "content": "短訊息"},
        ]
        text = compaction._format_messages(msgs)
        assert "…（略）" not in text

    def test_empty_content(self):
        msgs = [
            {"role": "user", "content": ""},
        ]
        text = compaction._format_messages(msgs)
        assert "【玩家】" in text


# ===================================================================
# load_recap / save_recap / get_recap_text
# ===================================================================


class TestRecapPersistence:
    def test_load_default_when_no_file(self, story_id, setup_branch):
        setup_branch("main")
        recap = compaction.load_recap(story_id, "main")
        assert recap["compacted_through_index"] == -1
        assert recap["recap_text"] == ""

    def test_save_and_load_roundtrip(self, story_id, setup_branch):
        setup_branch("main")
        data = {
            "compacted_through_index": 25,
            "recap_text": "測試回顧文字",
            "last_compacted_at": "2026-01-01T00:00:00+00:00",
            "total_turns_compacted": 10,
        }
        compaction.save_recap(story_id, "main", data)
        loaded = compaction.load_recap(story_id, "main")
        assert loaded["compacted_through_index"] == 25
        assert loaded["recap_text"] == "測試回顧文字"

    def test_get_recap_text_with_content(self, story_id, setup_branch):
        setup_branch("main", recap={
            "compacted_through_index": 10,
            "recap_text": "這是回顧內容",
        })
        text = compaction.get_recap_text(story_id, "main")
        assert text == "這是回顧內容"

    def test_get_recap_text_fallback(self, story_id, setup_branch):
        setup_branch("main")
        text = compaction.get_recap_text(story_id, "main")
        assert text == compaction._FALLBACK_RECAP


# ===================================================================
# copy_recap_to_branch
# ===================================================================


class TestCopyRecapToBranch:
    def test_copy_basic(self, story_id, setup_branch):
        setup_branch("main", recap={
            "compacted_through_index": 20,
            "recap_text": "主線回顧",
            "total_turns_compacted": 8,
        })
        setup_branch("child")
        compaction.copy_recap_to_branch(story_id, "main", "child", branch_point_index=25)
        child_recap = compaction.load_recap(story_id, "child")
        assert child_recap["recap_text"] == "主線回顧"

    def test_divergence_note_when_branch_within_compacted(self, story_id, setup_branch):
        setup_branch("main", recap={
            "compacted_through_index": 30,
            "recap_text": "主線回顧",
        })
        setup_branch("child")
        # branch_point_index=20 < compacted_through=30 → add note
        compaction.copy_recap_to_branch(story_id, "main", "child", branch_point_index=20)
        child_recap = compaction.load_recap(story_id, "child")
        assert "分支劇情" in child_recap["recap_text"]

    def test_no_note_when_branch_after_compacted(self, story_id, setup_branch):
        setup_branch("main", recap={
            "compacted_through_index": 10,
            "recap_text": "主線回顧",
        })
        setup_branch("child")
        # branch_point_index=20 > compacted_through=10 → no note
        compaction.copy_recap_to_branch(story_id, "main", "child", branch_point_index=20)
        child_recap = compaction.load_recap(story_id, "child")
        assert "分支劇情" not in child_recap["recap_text"]

    def test_no_copy_when_parent_empty(self, story_id, setup_branch):
        setup_branch("main")  # No recap
        setup_branch("child")
        compaction.copy_recap_to_branch(story_id, "main", "child", branch_point_index=5)
        child_recap = compaction.load_recap(story_id, "child")
        assert child_recap["recap_text"] == ""


# ===================================================================
# Constants
# ===================================================================


class TestConstants:
    def test_recent_window(self):
        assert compaction.RECENT_WINDOW == 20

    def test_min_uncompacted_trigger(self):
        assert compaction.MIN_UNCOMPACTED_FOR_TRIGGER == 20

    def test_recap_char_cap(self):
        assert compaction.RECAP_CHAR_CAP == 8000

    def test_meta_compact_target(self):
        assert compaction.RECAP_META_COMPACT_TARGET == 3000
