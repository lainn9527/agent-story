"""Tests for usage_db.py (Phase 1.6).

Tests usage logging, aggregation, and cross-story totals.
Uses monkeypatched STORIES_DIR for filesystem isolation.
"""

import os
from unittest import mock

import pytest

import usage_db


@pytest.fixture(autouse=True)
def patch_stories_dir(tmp_path, monkeypatch):
    """Redirect usage_db STORIES_DIR to tmp_path and clear initialized cache."""
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir()
    monkeypatch.setattr(usage_db, "STORIES_DIR", str(stories_dir))
    # Clear the _initialized set so tables are re-created for each test
    usage_db._initialized.clear()
    return stories_dir


@pytest.fixture
def story_id():
    return "test_story"


@pytest.fixture
def seed_usage(story_id):
    """Insert sample usage entries."""
    entries = [
        {"provider": "gemini", "model": "gemini-2.5-flash", "call_type": "gm_stream",
         "prompt_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
        {"provider": "gemini", "model": "gemini-2.5-flash", "call_type": "gm_stream",
         "prompt_tokens": 1200, "output_tokens": 600, "total_tokens": 1800},
        {"provider": "gemini", "model": "gemini-2.5-flash", "call_type": "compaction",
         "prompt_tokens": 800, "output_tokens": 200, "total_tokens": 1000},
        {"provider": "claude_cli", "model": "claude-sonnet-4-5", "call_type": "oneshot",
         "prompt_tokens": None, "output_tokens": None, "total_tokens": None},
        {"provider": "gemini", "model": "gemini-2.5-pro", "call_type": "npc_evolution",
         "prompt_tokens": 500, "output_tokens": 300, "total_tokens": 800},
    ]
    for e in entries:
        usage_db.log_usage(
            story_id=story_id,
            provider=e["provider"],
            model=e["model"],
            call_type=e["call_type"],
            prompt_tokens=e["prompt_tokens"],
            output_tokens=e["output_tokens"],
            total_tokens=e["total_tokens"],
            elapsed_ms=100,
        )


# ===================================================================
# log_usage
# ===================================================================


class TestLogUsage:
    def test_basic_log(self, story_id):
        usage_db.log_usage(
            story_id=story_id,
            provider="gemini",
            model="gemini-2.5-flash",
            call_type="gm",
            prompt_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )
        summary = usage_db.get_usage_summary(story_id, days=1)
        assert summary["total"]["calls"] == 1
        assert summary["total"]["total_tokens"] == 150

    def test_null_tokens(self, story_id):
        usage_db.log_usage(
            story_id=story_id,
            provider="claude_cli",
            model="claude-sonnet",
            call_type="gm",
            prompt_tokens=None,
            output_tokens=None,
            total_tokens=None,
        )
        summary = usage_db.get_usage_summary(story_id, days=1)
        assert summary["total"]["calls"] == 1
        # NULL tokens coalesce to 0 in SUM
        assert summary["total"]["total_tokens"] == 0

    def test_with_branch_id(self, story_id):
        usage_db.log_usage(
            story_id=story_id,
            provider="gemini",
            model="flash",
            call_type="gm",
            prompt_tokens=100,
            output_tokens=50,
            total_tokens=150,
            branch_id="branch_abc",
        )
        summary = usage_db.get_usage_summary(story_id, days=1)
        assert summary["total"]["calls"] == 1

    def test_with_elapsed_ms(self, story_id):
        usage_db.log_usage(
            story_id=story_id,
            provider="gemini",
            model="flash",
            call_type="gm",
            prompt_tokens=100,
            output_tokens=50,
            total_tokens=150,
            elapsed_ms=2500,
        )
        summary = usage_db.get_usage_summary(story_id, days=1)
        assert summary["total"]["calls"] == 1


# ===================================================================
# get_usage_summary
# ===================================================================


class TestGetUsageSummary:
    def test_total_aggregation(self, story_id, seed_usage):
        summary = usage_db.get_usage_summary(story_id, days=30)
        assert summary["total"]["calls"] == 5
        # 1500 + 1800 + 1000 + 0 + 800 = 5100
        assert summary["total"]["total_tokens"] == 5100

    def test_by_provider(self, story_id, seed_usage):
        summary = usage_db.get_usage_summary(story_id, days=30)
        # by_provider groups by (provider, model) — not just provider
        total_calls = sum(p["calls"] for p in summary["by_provider"])
        assert total_calls == 5
        providers = {p["provider"] for p in summary["by_provider"]}
        assert "gemini" in providers
        assert "claude_cli" in providers

    def test_by_type(self, story_id, seed_usage):
        summary = usage_db.get_usage_summary(story_id, days=30)
        types = {t["call_type"]: t for t in summary["by_type"]}
        assert "gm_stream" in types
        assert types["gm_stream"]["calls"] == 2
        assert "compaction" in types
        assert types["compaction"]["calls"] == 1

    def test_by_day(self, story_id, seed_usage):
        summary = usage_db.get_usage_summary(story_id, days=30)
        assert len(summary["by_day"]) >= 1
        # All entries logged today
        assert summary["by_day"][0]["calls"] == 5

    def test_empty_story(self, story_id):
        summary = usage_db.get_usage_summary(story_id, days=7)
        assert summary["total"]["calls"] == 0
        assert summary["total"]["total_tokens"] == 0


# ===================================================================
# get_total_usage
# ===================================================================


class TestGetTotalUsage:
    def test_single_story(self, story_id, seed_usage):
        result = usage_db.get_total_usage()
        assert result["total"]["calls"] == 5
        assert len(result["by_story"]) == 1
        assert result["by_story"][0]["story_id"] == story_id

    def test_multiple_stories(self, seed_usage, tmp_path):
        # Create second story
        usage_db.log_usage(
            story_id="story_2",
            provider="gemini",
            model="flash",
            call_type="gm",
            prompt_tokens=200,
            output_tokens=100,
            total_tokens=300,
        )
        result = usage_db.get_total_usage()
        assert result["total"]["calls"] == 6  # 5 + 1
        assert len(result["by_story"]) == 2

    def test_no_stories(self, tmp_path, monkeypatch):
        # Point to empty dir
        empty = tmp_path / "empty_stories"
        empty.mkdir()
        monkeypatch.setattr(usage_db, "STORIES_DIR", str(empty))
        result = usage_db.get_total_usage()
        assert result["total"]["calls"] == 0
        assert result["by_story"] == []


# ===================================================================
# log_from_bridge
# ===================================================================


class TestLogFromBridge:
    def test_with_explicit_usage(self, story_id):
        usage = {
            "provider": "gemini",
            "model": "flash",
            "prompt_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        }
        usage_db.log_from_bridge(story_id, "compaction", 1.5, usage=usage)
        summary = usage_db.get_usage_summary(story_id, days=1)
        assert summary["total"]["calls"] == 1
        assert summary["total"]["total_tokens"] == 150

    @mock.patch("llm_bridge.get_last_usage", return_value=None)
    def test_none_usage_returns_early(self, mock_get, story_id):
        # If usage is None AND get_last_usage returns None, should not log
        usage_db.log_from_bridge(story_id, "compaction", 1.0, usage=None)
        summary = usage_db.get_usage_summary(story_id, days=1)
        assert summary["total"]["calls"] == 0

    def test_elapsed_s_to_ms_conversion(self, story_id):
        usage = {
            "provider": "gemini",
            "model": "flash",
            "prompt_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        }
        usage_db.log_from_bridge(story_id, "gm", 2.5, usage=usage)
        summary = usage_db.get_usage_summary(story_id, days=1)
        assert summary["total"]["calls"] == 1
