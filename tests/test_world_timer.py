"""Tests for world_timer.py (Phase 1.2).

Tests time tag parsing, world day advancement, and branch copying.
Uses tmp_path fixtures for filesystem isolation.
"""

import json

import pytest

import world_timer


@pytest.fixture(autouse=True)
def patch_base_dir(tmp_path, monkeypatch):
    """Redirect world_timer's BASE_DIR to tmp_path so all file ops use temp dir."""
    monkeypatch.setattr(world_timer, "BASE_DIR", str(tmp_path))
    # Clear per-branch locks between tests
    world_timer._branch_locks.clear()


@pytest.fixture
def setup_branch(tmp_path):
    """Create the directory structure for a branch."""
    def _setup(story_id="s1", branch_id="main", world_day=0):
        branch_dir = tmp_path / "data" / "stories" / story_id / "branches" / branch_id
        branch_dir.mkdir(parents=True, exist_ok=True)
        if world_day is not None:
            wd_path = branch_dir / "world_day.json"
            wd_path.write_text(
                json.dumps({"world_day": world_day, "last_updated": "2026-01-01T00:00:00+00:00"}),
                encoding="utf-8",
            )
        return branch_dir
    return _setup


# ===================================================================
# parse_time_tag — pure function
# ===================================================================


class TestParseTimeTag:
    def test_days_integer(self):
        assert world_timer.parse_time_tag("days:3") == 3.0

    def test_days_float(self):
        assert world_timer.parse_time_tag("days:1.5") == 1.5

    def test_hours_integer(self):
        assert world_timer.parse_time_tag("hours:12") == pytest.approx(0.5)

    def test_hours_24_equals_one_day(self):
        assert world_timer.parse_time_tag("hours:24") == pytest.approx(1.0)

    def test_hours_8(self):
        assert world_timer.parse_time_tag("hours:8") == pytest.approx(8 / 24)

    def test_invalid_returns_zero(self):
        assert world_timer.parse_time_tag("invalid") == 0

    def test_empty_string(self):
        assert world_timer.parse_time_tag("") == 0

    def test_days_with_trailing_text(self):
        # Regex extracts numeric value, ignores trailing text
        assert world_timer.parse_time_tag("days:3 extra text") == 3.0

    def test_hours_with_trailing_text(self):
        assert world_timer.parse_time_tag("hours:6 blah") == pytest.approx(6 / 24)


# ===================================================================
# get_world_day / set_world_day
# ===================================================================


class TestGetSetWorldDay:
    def test_get_default_when_no_file(self):
        assert world_timer.get_world_day("nonexistent", "main") == 0

    def test_get_existing(self, setup_branch):
        setup_branch("s1", "main", world_day=5.5)
        assert world_timer.get_world_day("s1", "main") == 5.5

    def test_set_and_get(self, setup_branch):
        setup_branch("s1", "main", world_day=0)
        world_timer.set_world_day("s1", "main", 10.0)
        assert world_timer.get_world_day("s1", "main") == 10.0

    def test_set_creates_file(self, tmp_path):
        # Directory doesn't exist yet — set_world_day creates it
        world_timer.set_world_day("new_story", "new_branch", 3.0)
        assert world_timer.get_world_day("new_story", "new_branch") == 3.0


# ===================================================================
# advance_world_day
# ===================================================================


class TestAdvanceWorldDay:
    def test_basic_advance(self, setup_branch):
        setup_branch("s1", "main", world_day=1.0)
        result = world_timer.advance_world_day("s1", "main", 3.0)
        assert result == 4.0
        assert world_timer.get_world_day("s1", "main") == 4.0

    def test_advance_zero_days_no_change(self, setup_branch):
        setup_branch("s1", "main", world_day=5.0)
        result = world_timer.advance_world_day("s1", "main", 0)
        assert result == 5.0

    def test_advance_negative_days_no_change(self, setup_branch):
        setup_branch("s1", "main", world_day=5.0)
        result = world_timer.advance_world_day("s1", "main", -1)
        assert result == 5.0

    def test_advance_fractional(self, setup_branch):
        setup_branch("s1", "main", world_day=0)
        result = world_timer.advance_world_day("s1", "main", 0.5)
        assert result == pytest.approx(0.5)

    def test_advance_cumulative(self, setup_branch):
        setup_branch("s1", "main", world_day=0)
        world_timer.advance_world_day("s1", "main", 1.0)
        world_timer.advance_world_day("s1", "main", 2.0)
        assert world_timer.get_world_day("s1", "main") == 3.0

    def test_advance_creates_file_if_missing(self, tmp_path):
        # No setup_branch — file doesn't exist
        result = world_timer.advance_world_day("s1", "b1", 5.0)
        assert result == 5.0


# ===================================================================
# copy_world_day
# ===================================================================


class TestCopyWorldDay:
    def test_copy_to_new_branch(self, setup_branch):
        setup_branch("s1", "main", world_day=7.5)
        setup_branch("s1", "child", world_day=None)
        world_timer.copy_world_day("s1", "main", "child")
        assert world_timer.get_world_day("s1", "child") == 7.5

    def test_copy_zero_does_not_write(self, setup_branch):
        setup_branch("s1", "main", world_day=0)
        setup_branch("s1", "child", world_day=None)
        world_timer.copy_world_day("s1", "main", "child")
        # world_day=0 → copy_world_day skips (day > 0 check)
        assert world_timer.get_world_day("s1", "child") == 0


# ===================================================================
# process_time_tags — integration with advance_world_day
# ===================================================================


class TestProcessTimeTags:
    def test_single_days_tag(self, setup_branch):
        setup_branch("s1", "main", world_day=1.0)
        text = "三天後，他們抵達了目的地。<!--TIME days:3 TIME-->新的冒險開始了。"
        clean = world_timer.process_time_tags(text, "s1", "main")
        assert "<!--TIME" not in clean
        assert "TIME-->" not in clean
        assert "三天後" in clean
        assert "新的冒險開始了" in clean
        assert world_timer.get_world_day("s1", "main") == 4.0

    def test_single_hours_tag(self, setup_branch):
        setup_branch("s1", "main", world_day=0)
        text = "戰鬥持續了很久。<!--TIME hours:8 TIME-->"
        clean = world_timer.process_time_tags(text, "s1", "main")
        assert world_timer.get_world_day("s1", "main") == pytest.approx(8 / 24)

    def test_multiple_time_tags(self, setup_branch):
        setup_branch("s1", "main", world_day=0)
        text = "<!--TIME days:1 TIME-->休息了一天。<!--TIME hours:12 TIME-->又過了半天。"
        clean = world_timer.process_time_tags(text, "s1", "main")
        expected = 1.0 + 12 / 24
        assert world_timer.get_world_day("s1", "main") == pytest.approx(expected)
        assert "<!--TIME" not in clean

    def test_no_time_tags(self, setup_branch):
        setup_branch("s1", "main", world_day=5.0)
        text = "普通對話，沒有時間推進。"
        clean = world_timer.process_time_tags(text, "s1", "main")
        assert clean == text
        assert world_timer.get_world_day("s1", "main") == 5.0

    def test_invalid_time_tag_no_advance(self, setup_branch):
        setup_branch("s1", "main", world_day=1.0)
        text = "<!--TIME invalid TIME-->"
        world_timer.process_time_tags(text, "s1", "main")
        assert world_timer.get_world_day("s1", "main") == 1.0


# ===================================================================
# Dungeon helpers
# ===================================================================


class TestDungeonHelpers:
    def test_dungeon_enter_adds_3_days(self, setup_branch):
        setup_branch("s1", "main", world_day=1.0)
        result = world_timer.advance_dungeon_enter("s1", "main")
        assert result == 4.0

    def test_dungeon_exit_adds_1_day(self, setup_branch):
        setup_branch("s1", "main", world_day=10.0)
        result = world_timer.advance_dungeon_exit("s1", "main")
        assert result == 11.0

    def test_dungeon_costs_constants(self):
        assert world_timer.DUNGEON_TIME_COSTS["default_enter"] == 3
        assert world_timer.DUNGEON_TIME_COSTS["default_exit"] == 1
        assert world_timer.DUNGEON_TIME_COSTS["training"] == 2
