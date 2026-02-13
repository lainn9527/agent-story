"""Tests for gemini_key_manager.py (Phase 1.5).

Tests key loading, tier ordering, cooldown, and edge cases.
Pure in-memory logic — no filesystem, no network.
"""

import time

import pytest

import gemini_key_manager


@pytest.fixture(autouse=True)
def reset_cooldowns():
    """Clear cooldown state between tests."""
    gemini_key_manager._cooldowns.clear()


# ===================================================================
# load_keys
# ===================================================================


class TestLoadKeys:
    def test_new_format(self):
        cfg = {
            "api_keys": [
                {"key": "key_free_1", "tier": "free"},
                {"key": "key_paid_1", "tier": "paid"},
            ]
        }
        keys = gemini_key_manager.load_keys(cfg)
        assert len(keys) == 2
        assert keys[0]["key"] == "key_free_1"
        assert keys[1]["tier"] == "paid"

    def test_old_format_single_key(self):
        cfg = {"api_key": "legacy_key_123"}
        keys = gemini_key_manager.load_keys(cfg)
        assert len(keys) == 1
        assert keys[0]["key"] == "legacy_key_123"
        assert keys[0]["tier"] == "free"

    def test_empty_config(self):
        cfg = {}
        keys = gemini_key_manager.load_keys(cfg)
        assert keys == []

    def test_empty_api_key_string(self):
        cfg = {"api_key": ""}
        keys = gemini_key_manager.load_keys(cfg)
        assert keys == []

    def test_new_format_takes_priority(self):
        cfg = {
            "api_keys": [{"key": "new_key", "tier": "free"}],
            "api_key": "old_key",
        }
        keys = gemini_key_manager.load_keys(cfg)
        assert len(keys) == 1
        assert keys[0]["key"] == "new_key"


# ===================================================================
# get_available_keys
# ===================================================================


class TestGetAvailableKeys:
    def test_all_available(self):
        cfg = {
            "api_keys": [
                {"key": "k1", "tier": "paid"},
                {"key": "k2", "tier": "free"},
            ]
        }
        available = gemini_key_manager.get_available_keys(cfg)
        assert len(available) == 2
        # Free keys should come first
        assert available[0]["tier"] == "free"
        assert available[1]["tier"] == "paid"

    def test_free_first_ordering(self):
        cfg = {
            "api_keys": [
                {"key": "k1", "tier": "paid"},
                {"key": "k2", "tier": "free"},
                {"key": "k3", "tier": "free"},
                {"key": "k4", "tier": "paid"},
            ]
        }
        available = gemini_key_manager.get_available_keys(cfg)
        tiers = [k["tier"] for k in available]
        # All free before all paid
        assert tiers == ["free", "free", "paid", "paid"]

    def test_cooled_down_key_excluded(self):
        cfg = {"api_keys": [{"key": "k1", "tier": "free"}, {"key": "k2", "tier": "free"}]}
        gemini_key_manager.mark_rate_limited("k1")
        available = gemini_key_manager.get_available_keys(cfg)
        assert len(available) == 1
        assert available[0]["key"] == "k2"

    def test_all_keys_cooled_down(self):
        cfg = {"api_keys": [{"key": "k1", "tier": "free"}]}
        gemini_key_manager.mark_rate_limited("k1")
        available = gemini_key_manager.get_available_keys(cfg)
        assert available == []


# ===================================================================
# mark_rate_limited
# ===================================================================


class TestMarkRateLimited:
    def test_key_becomes_unavailable(self):
        cfg = {"api_keys": [{"key": "k1", "tier": "free"}]}
        assert len(gemini_key_manager.get_available_keys(cfg)) == 1
        gemini_key_manager.mark_rate_limited("k1")
        assert len(gemini_key_manager.get_available_keys(cfg)) == 0

    def test_key_recovers_after_cooldown(self, monkeypatch):
        cfg = {"api_keys": [{"key": "k1", "tier": "free"}]}
        now = time.time()

        # Mark as limited
        monkeypatch.setattr(time, "time", lambda: now)
        gemini_key_manager.mark_rate_limited("k1", cooldown=10)

        # Still limited after 5s
        monkeypatch.setattr(time, "time", lambda: now + 5)
        assert len(gemini_key_manager.get_available_keys(cfg)) == 0

        # Available after 11s
        monkeypatch.setattr(time, "time", lambda: now + 11)
        assert len(gemini_key_manager.get_available_keys(cfg)) == 1

    def test_custom_cooldown(self, monkeypatch):
        cfg = {"api_keys": [{"key": "k1", "tier": "free"}]}
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now)
        gemini_key_manager.mark_rate_limited("k1", cooldown=120)

        # Still limited at 60s
        monkeypatch.setattr(time, "time", lambda: now + 60)
        assert len(gemini_key_manager.get_available_keys(cfg)) == 0

        # Available at 121s
        monkeypatch.setattr(time, "time", lambda: now + 121)
        assert len(gemini_key_manager.get_available_keys(cfg)) == 1

    def test_default_cooldown_is_60s(self):
        assert gemini_key_manager.COOLDOWN_SECONDS == 60

    def test_rate_limit_unknown_key_no_error(self):
        # Should not raise even if key isn't in any config
        gemini_key_manager.mark_rate_limited("unknown_key")
