"""Tests for _apply_state_update_inner in app.py (Phase 2.2).

Tests inventory add/remove, delta fields, direct overwrite,
schema-driven ops, and edge cases.
Uses monkeypatched paths for filesystem isolation.
"""

import json

import pytest

import app as app_module


# The character schema matching the real project
SCHEMA = {
    "fields": [
        {"key": "name", "label": "姓名", "type": "text"},
        {"key": "current_phase", "label": "階段", "type": "text"},
        {"key": "reward_points", "label": "獎勵點", "type": "number"},
        {"key": "current_status", "label": "狀態", "type": "text"},
        {"key": "gene_lock", "label": "基因鎖", "type": "text"},
        {"key": "physique", "label": "體質", "type": "text"},
        {"key": "spirit", "label": "精神力", "type": "text"},
    ],
    "lists": [
        {
            "key": "inventory",
            "label": "道具欄",
            "state_add_key": "inventory_add",
            "state_remove_key": "inventory_remove",
        },
        {
            "key": "completed_missions",
            "label": "已完成任務",
            "state_add_key": "completed_missions_add",
        },
        {
            "key": "relationships",
            "label": "人際關係",
            "type": "map",
        },
    ],
    "direct_overwrite_keys": [
        "gene_lock", "physique", "spirit", "current_status", "current_phase",
    ],
}

INITIAL_STATE = {
    "name": "測試者",
    "current_phase": "主神空間",
    "reward_points": 5000,
    "inventory": ["封印之鏡", "鎮魂符×5"],
    "relationships": {"小薇": "信任"},
    "completed_missions": ["咒怨 — 完美通關"],
    "gene_lock": "未開啟",
    "physique": "普通人",
    "spirit": "普通人",
    "current_status": "等待任務",
}


@pytest.fixture(autouse=True)
def patch_app_paths(tmp_path, monkeypatch):
    """Redirect app.py's STORIES_DIR to tmp_path."""
    stories_dir = tmp_path / "data" / "stories"
    stories_dir.mkdir(parents=True)
    monkeypatch.setattr(app_module, "STORIES_DIR", str(stories_dir))
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    return stories_dir


@pytest.fixture
def story_id():
    return "test_story"


@pytest.fixture
def setup_state(tmp_path, story_id):
    """Create branch dir with character state."""
    def _setup(branch_id="main", state=None):
        branch_dir = tmp_path / "data" / "stories" / story_id / "branches" / branch_id
        branch_dir.mkdir(parents=True, exist_ok=True)
        s = state or dict(INITIAL_STATE)
        (branch_dir / "character_state.json").write_text(
            json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return branch_dir
    return _setup


def _load_state(tmp_path, story_id, branch_id="main"):
    """Read character state from disk."""
    path = tmp_path / "data" / "stories" / story_id / "branches" / branch_id / "character_state.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ===================================================================
# Inventory operations
# ===================================================================


class TestInventoryAdd:
    def test_add_single_item(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"inventory_add": ["新道具"]}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "新道具" in state["inventory"]

    def test_add_multiple_items(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"inventory_add": ["劍", "盾"]}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "劍" in state["inventory"]
        assert "盾" in state["inventory"]

    def test_add_string_wrapped_in_list(self, tmp_path, story_id, setup_state):
        """LLM sometimes returns string instead of list."""
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"inventory_add": "單一道具"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "單一道具" in state["inventory"]

    def test_add_duplicate_skipped(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"inventory_add": ["封印之鏡"]}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["inventory"].count("封印之鏡") == 1


class TestExtractItemBaseName:
    """Test _extract_item_base_name helper for flexible name matching."""

    def test_plain_name(self):
        assert app_module._extract_item_base_name("封印之鏡") == "封印之鏡"

    def test_dash_description(self):
        assert app_module._extract_item_base_name("封印之鏡 — 可以封印低等級怨靈") == "封印之鏡"

    def test_halfwidth_paren_status(self):
        assert app_module._extract_item_base_name("大日金烏劍·空燼 (S 級潛力)") == "大日金烏劍·空燼"

    def test_fullwidth_paren_status(self):
        assert app_module._extract_item_base_name("定界珠（生/已綁定）") == "定界珠"

    def test_quantity_suffix(self):
        assert app_module._extract_item_base_name("鎮魂符 x5") == "鎮魂符"

    def test_paren_with_quantity(self):
        assert app_module._extract_item_base_name("特級殘穢·獄門疆碎屑 (剩餘 x2)") == "特級殘穢·獄門疆碎屑"

    def test_quantity_without_paren(self):
        assert app_module._extract_item_base_name("魔虛羅的殘損齒輪 x2") == "魔虛羅的殘損齒輪"

    def test_compound_name_with_dot(self):
        assert app_module._extract_item_base_name("混元·九轉生機膏 x2") == "混元·九轉生機膏"


class TestInventoryRemove:
    def test_remove_item(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"inventory_remove": ["封印之鏡"]}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "封印之鏡" not in state["inventory"]

    def test_remove_string_wrapped(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"inventory_remove": "封印之鏡"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "封印之鏡" not in state["inventory"]

    def test_remove_nonexistent_no_error(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"inventory_remove": ["不存在"]}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert len(state["inventory"]) == 2  # Original items unchanged

    def test_remove_by_name_prefix(self, tmp_path, story_id, setup_state):
        """Items like '封印之鏡 — 描述' should match by name prefix '封印之鏡'."""
        setup_state("main", {**INITIAL_STATE, "inventory": ["封印之鏡 — 可以封印低等級怨靈"]})
        app_module._apply_state_update_inner(story_id, "main", {"inventory_remove": ["封印之鏡"]}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert len(state["inventory"]) == 0

    def test_remove_by_paren_status(self, tmp_path, story_id, setup_state):
        """Items with (status) format should be removable by base name."""
        setup_state("main", {**INITIAL_STATE, "inventory": [
            "大日金烏劍·空燼 (S 級潛力)",
            "大日金烏劍·空燼 (穩定度提升)",
        ]})
        app_module._apply_state_update_inner(
            story_id, "main",
            {"inventory_remove": ["大日金烏劍·空燼"]},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert len([x for x in state["inventory"] if "大日金烏劍" in x]) == 0

    def test_remove_by_fullwidth_paren(self, tmp_path, story_id, setup_state):
        """Items with （全形括號） should also match."""
        setup_state("main", {**INITIAL_STATE, "inventory": ["定界珠（生/已深度綁定）"]})
        app_module._apply_state_update_inner(
            story_id, "main",
            {"inventory_remove": ["定界珠"]},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert len(state["inventory"]) == 0

    def test_remove_with_quantity(self, tmp_path, story_id, setup_state):
        """Items with x2 quantity suffix should match by base name."""
        setup_state("main", {**INITIAL_STATE, "inventory": ["魔虛羅的殘損齒輪 x2"]})
        app_module._apply_state_update_inner(
            story_id, "main",
            {"inventory_remove": ["魔虛羅的殘損齒輪"]},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert len([x for x in state["inventory"] if "魔虛羅" in x]) == 0


# ===================================================================
# Reward points delta
# ===================================================================


class TestRewardPointsDelta:
    def test_positive_delta(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"reward_points_delta": 1000}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["reward_points"] == 6000

    def test_negative_delta(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"reward_points_delta": -2000}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["reward_points"] == 3000

    def test_direct_set_when_no_delta(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"reward_points": 9999}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["reward_points"] == 9999

    def test_delta_takes_precedence(self, tmp_path, story_id, setup_state):
        """When both reward_points and reward_points_delta are present, delta wins."""
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main",
            {"reward_points": 9999, "reward_points_delta": -500},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert state["reward_points"] == 4500  # 5000 - 500


# ===================================================================
# Direct overwrite fields
# ===================================================================


class TestDirectOverwrite:
    def test_overwrite_gene_lock(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"gene_lock": "第一階"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["gene_lock"] == "第一階"

    def test_overwrite_current_phase(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"current_phase": "副本中"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["current_phase"] == "副本中"

    def test_overwrite_current_status(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"current_status": "戰鬥中"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["current_status"] == "戰鬥中"


# ===================================================================
# Relationship map
# ===================================================================


class TestRelationshipMap:
    def test_add_new_relationship(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main",
            {"relationships": {"阿豪": "兄弟情"}},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert state["relationships"]["阿豪"] == "兄弟情"
        assert state["relationships"]["小薇"] == "信任"  # Existing preserved

    def test_update_existing_relationship(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main",
            {"relationships": {"小薇": "深厚信任"}},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert state["relationships"]["小薇"] == "深厚信任"


# ===================================================================
# Scene-transient keys blocked
# ===================================================================


class TestSceneKeysBlocked:
    def test_location_not_saved(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"location": "深山"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "location" not in state

    def test_threat_level_not_saved(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"threat_level": "高"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "threat_level" not in state

    def test_combat_status_not_saved(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"combat_status": "激戰"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "combat_status" not in state


# ===================================================================
# Extra fields (unknown keys)
# ===================================================================


class TestExtraFields:
    def test_unknown_string_field_saved(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"修真境界": "煉氣期"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["修真境界"] == "煉氣期"

    def test_unknown_number_field_saved(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"法力": 100}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["法力"] == 100

    def test_system_keys_blocked(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"world_day": 5, "branch_title": "test"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "world_day" not in state
        assert "branch_title" not in state


# ===================================================================
# Combined updates
# ===================================================================


class TestCombinedUpdates:
    def test_multi_field_update(self, tmp_path, story_id, setup_state):
        setup_state()
        update = {
            "current_phase": "副本中",
            "reward_points_delta": -500,
            "inventory_add": ["急救包"],
            "gene_lock": "第一階",
        }
        app_module._apply_state_update_inner(story_id, "main", update, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["current_phase"] == "副本中"
        assert state["reward_points"] == 4500
        assert "急救包" in state["inventory"]
        assert state["gene_lock"] == "第一階"

    def test_empty_update_no_crash(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["name"] == "測試者"  # Unchanged


# ===================================================================
# Instruction keys blocked
# ===================================================================


class TestInstructionKeysBlocked:
    """LLM intermediate instruction keys should not leak into state."""

    def test_inventory_use_not_saved(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"inventory_use": "藥劑已使用"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "inventory_use" not in state

    def test_skill_update_not_saved(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"skill_update": "因果入侵"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "skill_update" not in state

    def test_inventory_update_not_saved(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"inventory_update": "定界珠已綁定"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "inventory_update" not in state

    def test_legitimate_extra_field_still_saved(self, tmp_path, story_id, setup_state):
        """Ensure real permanent attributes are not blocked."""
        setup_state()
        app_module._apply_state_update_inner(story_id, "main", {"修真境界": "煉氣期"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["修真境界"] == "煉氣期"
