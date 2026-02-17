"""Tests for context injection structure (Phase 2.4).

Tests _build_augmented_message output format and _build_story_system_prompt
placeholder filling. Uses mocked search/dice/activity functions.
"""

import json
import os
from unittest import mock

import pytest

import app as app_module


@pytest.fixture(autouse=True)
def patch_app_paths(tmp_path, monkeypatch):
    """Redirect app paths to tmp_path."""
    stories_dir = tmp_path / "data" / "stories"
    stories_dir.mkdir(parents=True)
    design_dir = tmp_path / "story_design"
    design_dir.mkdir()
    monkeypatch.setattr(app_module, "STORIES_DIR", str(stories_dir))
    monkeypatch.setattr(app_module, "STORY_DESIGN_DIR", str(design_dir))
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    return stories_dir


@pytest.fixture
def story_id():
    return "test_story"


@pytest.fixture
def setup_story(tmp_path, story_id):
    """Set up a minimal story for context injection tests."""
    story_dir = tmp_path / "data" / "stories" / story_id
    story_dir.mkdir(parents=True, exist_ok=True)
    branch_dir = story_dir / "branches" / "main"
    branch_dir.mkdir(parents=True, exist_ok=True)

    # Design files directory
    design_dir = tmp_path / "story_design" / story_id
    design_dir.mkdir(parents=True, exist_ok=True)

    # Design files → story_design/
    prompt = (
        "你是GM。\n"
        "## 角色狀態\n{character_state}\n"
        "## 敘事回顧\n{narrative_recap}\n"
        "## 世界設定\n{world_lore}\n"
        "## NPC\n{npc_profiles}\n"
        "## 團隊規則\n{team_rules}\n"
        "## 其他\n{other_agents}\n"
        "## 關鍵事實\n{critical_facts}\n"
    )
    (design_dir / "system_prompt.txt").write_text(prompt, encoding="utf-8")

    # Timeline tree (runtime)
    (story_dir / "timeline_tree.json").write_text(json.dumps({
        "active_branch_id": "main",
        "branches": {
            "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
        },
    }), encoding="utf-8")

    # Character state (runtime)
    state = {"name": "測試者", "current_phase": "主神空間", "reward_points": 5000}
    (branch_dir / "character_state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )

    # Character schema → story_design/
    (design_dir / "character_schema.json").write_text(json.dumps({
        "fields": [{"key": "name"}, {"key": "current_phase"}, {"key": "reward_points"}],
        "lists": [],
        "direct_overwrite_keys": [],
    }), encoding="utf-8")

    # NPCs (runtime)
    (branch_dir / "npcs.json").write_text("[]", encoding="utf-8")

    # Branch config (runtime)
    (branch_dir / "branch_config.json").write_text(json.dumps({}), encoding="utf-8")

    # World lore → story_design/
    (design_dir / "world_lore.json").write_text("[]", encoding="utf-8")

    return story_dir


# ===================================================================
# _build_augmented_message
# ===================================================================


class TestBuildAugmentedMessage:
    @mock.patch("app.search_relevant_lore", return_value="[相關世界設定]\n#### 體系：基因鎖\n基因鎖是...")
    @mock.patch("app.search_relevant_events", return_value="[相關事件追蹤]\n- [伏筆] 神秘組織（已埋）")
    @mock.patch("app.get_recent_activities", return_value="[NPC 近期動態]\n- 阿豪：訓練中")
    @mock.patch("app.roll_fate", return_value={"outcome": "順遂", "roll": 15})
    @mock.patch("app.format_dice_context", return_value="[命運走向] 順遂")
    @mock.patch("app.is_gm_command", return_value=False)
    def test_all_sections_present(self, mock_gm, mock_fmt, mock_roll, mock_act, mock_evt, mock_lore, story_id, setup_story):
        state = {"current_phase": "主神空間"}
        text, dice = app_module._build_augmented_message(story_id, "main", "我要修煉", state)

        assert "[相關世界設定]" in text
        assert "[相關事件追蹤]" in text
        assert "[NPC 近期動態]" in text
        assert "[命運走向]" in text
        assert "我要修煉" in text
        assert dice is not None

    @mock.patch("app.search_relevant_lore", return_value="[相關世界設定]\n內容")
    @mock.patch("app.search_relevant_events", return_value="")
    @mock.patch("app.get_recent_activities", return_value="")
    @mock.patch("app.is_gm_command", return_value=False)
    @mock.patch("app.roll_fate", return_value={"outcome": "順遂"})
    @mock.patch("app.format_dice_context", return_value="[命運走向] 順遂")
    def test_empty_sections_omitted(self, mock_fmt, mock_roll, mock_gm, mock_act, mock_evt, mock_lore, story_id, setup_story):
        text, _ = app_module._build_augmented_message(story_id, "main", "你好", {"current_phase": "主神空間"})
        assert "[相關世界設定]" in text
        # Empty events/activities should not have headers
        assert "[相關事件追蹤]" not in text

    @mock.patch("app.search_relevant_lore", return_value="[相關世界設定]\n內容")
    @mock.patch("app.search_relevant_events", return_value="")
    @mock.patch("app.get_recent_activities", return_value="")
    @mock.patch("app.is_gm_command", return_value=False)
    @mock.patch("app.roll_fate", return_value={"outcome": "順遂"})
    @mock.patch("app.format_dice_context", return_value="[命運走向] 順遂")
    def test_user_text_at_end(self, mock_fmt, mock_roll, mock_gm, mock_act, mock_evt, mock_lore, story_id, setup_story):
        text, _ = app_module._build_augmented_message(story_id, "main", "原始訊息", {"current_phase": "主神空間"})
        # User text should be after the separator
        assert text.endswith("原始訊息")
        assert "---\n原始訊息" in text

    @mock.patch("app.search_relevant_lore", return_value="")
    @mock.patch("app.search_relevant_events", return_value="")
    @mock.patch("app.get_recent_activities", return_value="")
    @mock.patch("app.is_gm_command", return_value=True)
    def test_gm_command_no_dice(self, mock_gm, mock_act, mock_evt, mock_lore, story_id, setup_story):
        text, dice = app_module._build_augmented_message(story_id, "main", "/gm 修改", {"current_phase": "主神空間"})
        assert dice is None


# ===================================================================
# _build_story_system_prompt
# ===================================================================


class TestBuildStorySystemPrompt:
    def test_no_residual_placeholders(self, story_id, setup_story):
        state_text = json.dumps({"name": "測試者"}, ensure_ascii=False)
        prompt = app_module._build_story_system_prompt(
            story_id, state_text, branch_id="main", narrative_recap="回顧"
        )
        # No unfilled placeholders
        for placeholder in ["{character_state}", "{narrative_recap}",
                            "{world_lore}", "{npc_profiles}"]:
            assert placeholder not in prompt

    def test_character_state_injected(self, story_id, setup_story):
        state_text = json.dumps({"name": "英雄", "reward_points": 9999}, ensure_ascii=False)
        prompt = app_module._build_story_system_prompt(
            story_id, state_text, branch_id="main"
        )
        assert "英雄" in prompt
        assert "9999" in prompt

    def test_narrative_recap_default(self, story_id, setup_story):
        prompt = app_module._build_story_system_prompt(
            story_id, "{}", branch_id="main"
        )
        assert "尚無回顧" in prompt
