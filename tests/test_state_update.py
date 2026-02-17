"""Tests for _apply_state_update_inner in app.py.

Tests inventory map operations, delta fields, direct overwrite,
schema-driven ops, backward compatibility, and edge cases.
Uses monkeypatched paths for filesystem isolation.
"""

import json

import pytest

import app as app_module


# The character schema matching the real project (inventory is now map type)
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
            "type": "map",
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
    "inventory": {"封印之鏡": "", "鎮魂符": "×5"},
    "relationships": {"小薇": "信任"},
    "completed_missions": ["咒怨 — 完美通關"],
    "gene_lock": "未開啟",
    "physique": "普通人",
    "spirit": "普通人",
    "current_status": "等待任務",
}


@pytest.fixture(autouse=True)
def patch_app_paths(tmp_path, monkeypatch):
    """Redirect app.py's STORIES_DIR and STORY_DESIGN_DIR to tmp_path."""
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
# Inventory map operations
# ===================================================================


class TestInventoryMap:
    def test_add_new_item(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main", {"inventory": {"新道具": ""}}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "新道具" in state["inventory"]

    def test_add_item_with_status(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main", {"inventory": {"死生之刃": "靈魂加固版"}}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["inventory"]["死生之刃"] == "靈魂加固版"

    def test_evolution_overwrites(self, tmp_path, story_id, setup_state):
        """Adding evolved item auto-overwrites the old status for the same key."""
        setup_state("main", {**INITIAL_STATE, "inventory": {"死生之刃": "初步成型"}})
        app_module._apply_state_update_inner(
            story_id, "main", {"inventory": {"死生之刃": "靈魂加固版"}}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["inventory"]["死生之刃"] == "靈魂加固版"
        assert len([k for k in state["inventory"] if "死生之刃" in k]) == 1

    def test_remove_item_with_null(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main", {"inventory": {"封印之鏡": None}}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "封印之鏡" not in state["inventory"]
        assert "鎮魂符" in state["inventory"]  # Other items preserved

    def test_remove_nonexistent_no_error(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main", {"inventory": {"不存在的道具": None}}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert len(state["inventory"]) == 2  # Original items unchanged

    def test_mixed_add_and_remove(self, tmp_path, story_id, setup_state):
        """Can add and remove items in one update."""
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main",
            {"inventory": {"封印之鏡": None, "新武器": "強化版"}},
            SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "封印之鏡" not in state["inventory"]
        assert state["inventory"]["新武器"] == "強化版"

    def test_preserves_existing_items(self, tmp_path, story_id, setup_state):
        """Map merge preserves items not mentioned in the update."""
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main", {"inventory": {"新道具": ""}}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "封印之鏡" in state["inventory"]
        assert "鎮魂符" in state["inventory"]
        assert "新道具" in state["inventory"]


# ===================================================================
# Backward compatibility: inventory_add / inventory_remove
# ===================================================================


class TestInventoryBackwardCompat:
    """Legacy extraction or STATE tags may still produce inventory_add/inventory_remove."""

    def test_legacy_add_converted_to_map(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main", {"inventory_add": ["新道具"]}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "新道具" in state["inventory"]

    def test_legacy_add_with_status(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main", {"inventory_add": ["死生之刃·日耀輪轉（靈魂加固版）"]}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["inventory"]["死生之刃·日耀輪轉"] == "靈魂加固版"

    def test_legacy_add_string_wrapped(self, tmp_path, story_id, setup_state):
        """LLM sometimes returns string instead of list."""
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main", {"inventory_add": "單一道具"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "單一道具" in state["inventory"]

    def test_legacy_remove_converted_to_null(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main", {"inventory_remove": ["封印之鏡"]}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "封印之鏡" not in state["inventory"]

    def test_legacy_remove_string_wrapped(self, tmp_path, story_id, setup_state):
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main", {"inventory_remove": "封印之鏡"}, SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert "封印之鏡" not in state["inventory"]

    def test_legacy_remove_then_add(self, tmp_path, story_id, setup_state):
        """Paired remove+add should work: remove old, add new."""
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main",
            {
                "inventory_remove": ["封印之鏡"],
                "inventory_add": ["封印之鏡（強化版）"],
            },
            SCHEMA)
        state = _load_state(tmp_path, story_id)
        # Remove sets base name to null, add sets new status — last write wins
        # Since both operate on same base name "封印之鏡", add (non-null) wins
        assert state["inventory"]["封印之鏡"] == "強化版"

    def test_legacy_add_evolution_dedup(self, tmp_path, story_id, setup_state):
        """Legacy add of evolved item should overwrite same base name in map."""
        setup_state("main", {**INITIAL_STATE, "inventory": {"死生之刃": "初步成型"}})
        app_module._apply_state_update_inner(
            story_id, "main",
            {"inventory_add": ["死生之刃（靈魂加固版）"]},
            SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["inventory"]["死生之刃"] == "靈魂加固版"

    def test_legacy_add_dash_format(self, tmp_path, story_id, setup_state):
        """Legacy 'name — description' format should be parsed correctly."""
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main",
            {"inventory_add": ["封印之鏡 — 可以封印低等級怨靈"]},
            SCHEMA)
        state = _load_state(tmp_path, story_id)
        assert state["inventory"]["封印之鏡"] == "可以封印低等級怨靈"


# ===================================================================
# Base name extraction (still used by backward compat and migration)
# ===================================================================


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

    def test_fullwidth_multiply_sign(self):
        assert app_module._extract_item_base_name("鎮魂符×5") == "鎮魂符"


# ===================================================================
# Parse item to key-value
# ===================================================================


class TestParseItemToKv:
    """Test _parse_item_to_kv helper for list→map migration."""

    def test_plain_name(self):
        assert app_module._parse_item_to_kv("蝕魂者之戒") == ("蝕魂者之戒", "")

    def test_dash_format(self):
        assert app_module._parse_item_to_kv("封印之鏡 — 可以封印低等級怨靈") == ("封印之鏡", "可以封印低等級怨靈")

    def test_fullwidth_paren(self):
        assert app_module._parse_item_to_kv("死生之刃·日耀輪轉（靈魂加固版）") == ("死生之刃·日耀輪轉", "靈魂加固版")

    def test_halfwidth_paren(self):
        assert app_module._parse_item_to_kv("大日金烏劍·空燼 (S 級潛力)") == ("大日金烏劍·空燼", "S 級潛力")

    def test_quantity_suffix(self):
        assert app_module._parse_item_to_kv("鎮魂符×5") == ("鎮魂符", "×5")

    def test_quantity_with_space(self):
        assert app_module._parse_item_to_kv("鎮魂符 x3") == ("鎮魂符", "x3")


# ===================================================================
# Auto-migration: list → map
# ===================================================================


class TestMigrateListToMap:
    """Test _migrate_list_to_map for auto-migration on load."""

    def test_basic_migration(self):
        result = app_module._migrate_list_to_map([
            "封印之鏡",
            "鎮魂符×5",
        ])
        assert result == {"封印之鏡": "", "鎮魂符": "×5"}

    def test_dedup_by_base_name(self):
        """Last item with same base name wins — latest evolution kept."""
        result = app_module._migrate_list_to_map([
            "死生之刃·日耀輪轉",
            "死生之刃·日耀輪轉（初步成型）",
            "死生之刃·日耀輪轉（靈魂加固版）",
        ])
        assert result == {"死生之刃·日耀輪轉": "靈魂加固版"}

    def test_different_base_names_preserved(self):
        result = app_module._migrate_list_to_map([
            "封印之鏡",
            "鎮魂符×5",
            "蝕魂者之戒",
        ])
        assert len(result) == 3

    def test_dash_format(self):
        result = app_module._migrate_list_to_map([
            "封印之鏡 — 可以封印低等級怨靈",
        ])
        assert result == {"封印之鏡": "可以封印低等級怨靈"}

    def test_empty_list(self):
        assert app_module._migrate_list_to_map([]) == {}

    def test_auto_migration_on_load(self, tmp_path, story_id, setup_state):
        """Loading a branch with list-format inventory auto-converts to map."""
        legacy_state = {**INITIAL_STATE, "inventory": ["封印之鏡", "鎮魂符×5"]}
        setup_state("main", legacy_state)
        state = app_module._load_character_state(story_id, "main")
        assert isinstance(state["inventory"], dict)
        assert "封印之鏡" in state["inventory"]
        assert state["inventory"]["鎮魂符"] == "×5"


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

    def test_remove_relationship_with_null(self, tmp_path, story_id, setup_state):
        """Null value should remove the relationship entry."""
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main",
            {"relationships": {"小薇": None}},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert "小薇" not in state["relationships"]


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
            "inventory": {"急救包": ""},
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
