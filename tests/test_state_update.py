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
        assert result == {"封印之鏡": "", "鎮魂符×5": ""}

    def test_lossless_distinct_items(self):
        """Distinct items with same base name are ALL preserved (lossless)."""
        result = app_module._migrate_list_to_map([
            "定界珠（生）",
            "定界珠（死）",
            "定界珠（因果）",
        ])
        assert result == {
            "定界珠（生）": "",
            "定界珠（死）": "",
            "定界珠（因果）": "",
        }

    def test_evolution_stages_preserved(self):
        """Evolution stages kept as separate keys during migration (lossless)."""
        result = app_module._migrate_list_to_map([
            "死生之刃·日耀輪轉",
            "死生之刃·日耀輪轉（初步成型）",
            "死生之刃·日耀輪轉（靈魂加固版）",
        ])
        assert len(result) == 3
        assert "死生之刃·日耀輪轉" in result
        assert "死生之刃·日耀輪轉（初步成型）" in result
        assert "死生之刃·日耀輪轉（靈魂加固版）" in result

    def test_different_items_preserved(self):
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
        assert "鎮魂符×5" in state["inventory"]


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


# ===================================================================
# schema.fields map-type entries (e.g. systems/體系)
# ===================================================================

SCHEMA_WITH_FIELDS_MAP = {
    "fields": [
        {"key": "name", "label": "姓名", "type": "text"},
        {"key": "reward_points", "label": "獎勵點", "type": "number"},
        {"key": "systems", "label": "體系", "type": "map"},
    ],
    "lists": [
        {"key": "inventory", "label": "道具欄", "type": "map"},
    ],
    "direct_overwrite_keys": [],
}

INITIAL_STATE_WITH_SYSTEMS = {
    "name": "測試者",
    "reward_points": 5000,
    "inventory": {},
    "systems": {"死生之道": "B級（具備空間掌控力）"},
}


class TestFieldsMapType:
    """schema.fields entries with type=map should be deep-merged like schema.lists map entries."""

    def test_system_grade_upgrade_upserts_key(self, tmp_path, story_id, setup_state):
        """Upgrading a system grade (e.g. B→A) updates the existing key."""
        setup_state(state=dict(INITIAL_STATE_WITH_SYSTEMS))
        app_module._apply_state_update_inner(
            story_id, "main",
            {"systems": {"死生之道": "A級（漩渦瞳·空間感知）"}},
            SCHEMA_WITH_FIELDS_MAP,
        )
        state = _load_state(tmp_path, story_id)
        assert state["systems"]["死生之道"] == "A級（漩渦瞳·空間感知）"

    def test_system_add_new_key(self, tmp_path, story_id, setup_state):
        """Adding a new body cultivation system appends without removing others."""
        setup_state(state=dict(INITIAL_STATE_WITH_SYSTEMS))
        app_module._apply_state_update_inner(
            story_id, "main",
            {"systems": {"修真之道": "C級（入門）"}},
            SCHEMA_WITH_FIELDS_MAP,
        )
        state = _load_state(tmp_path, story_id)
        assert state["systems"]["死生之道"] == "B級（具備空間掌控力）"
        assert state["systems"]["修真之道"] == "C級（入門）"

    def test_system_remove_with_null(self, tmp_path, story_id, setup_state):
        """Setting a key to null removes it from the map."""
        setup_state(state=dict(INITIAL_STATE_WITH_SYSTEMS))
        app_module._apply_state_update_inner(
            story_id, "main",
            {"systems": {"死生之道": None}},
            SCHEMA_WITH_FIELDS_MAP,
        )
        state = _load_state(tmp_path, story_id)
        assert "死生之道" not in state["systems"]

    def test_system_non_dict_value_ignored(self, tmp_path, story_id, setup_state):
        """If LLM outputs a string instead of a dict, the field is not overwritten."""
        setup_state(state=dict(INITIAL_STATE_WITH_SYSTEMS))
        app_module._apply_state_update_inner(
            story_id, "main",
            {"systems": "A級"},  # malformed LLM output
            SCHEMA_WITH_FIELDS_MAP,
        )
        state = _load_state(tmp_path, story_id)
        # Original value preserved; string value was not applied
        assert state["systems"] == {"死生之道": "B級（具備空間掌控力）"}

    def test_fields_map_key_in_handled_keys_no_extra_save(self, tmp_path, story_id, setup_state):
        """fields-map keys are in handled_keys, so they are not double-saved as scalar extra fields."""
        setup_state(state=dict(INITIAL_STATE_WITH_SYSTEMS))
        # If 'systems' were NOT in handled_keys, a dict value would be ignored by
        # the extra-keys scalar guard, which is fine — but it should be processed
        # by the fields-map loop, not fall through.
        app_module._apply_state_update_inner(
            story_id, "main",
            {"systems": {"死生之道": "A級（漩渦瞳）"}},
            SCHEMA_WITH_FIELDS_MAP,
        )
        state = _load_state(tmp_path, story_id)
        assert isinstance(state["systems"], dict)
        assert state["systems"]["死生之道"] == "A級（漩渦瞳）"


# ===================================================================
# Fuzzy key normalization
# ===================================================================


class TestNormalizeMapKey:
    """Test _normalize_map_key helper for character-variant normalization."""

    def test_strip_spaces(self):
        assert app_module._normalize_map_key("C 級支線劇情") == "C級支線劇情"

    def test_normalize_middle_dots(self):
        for dot in ['‧', '・', '•']:
            assert app_module._normalize_map_key(f"G病毒{dot}原始株") == "G病毒·原始株"

    def test_normalize_dashes(self):
        for dash in ['–', '-', 'ー']:
            assert app_module._normalize_map_key(f"死生之刃{dash}覺醒") == "死生之刃—覺醒"

    def test_normalize_fullwidth_alpha(self):
        assert app_module._normalize_map_key("Ｇ病毒") == "G病毒"

    def test_normalize_fullwidth_digits(self):
        assert app_module._normalize_map_key("等級５") == "等級5"

    def test_normalize_brackets(self):
        assert app_module._normalize_map_key("空間戒指（5立方公尺）") == "空間戒指(5立方公尺)"

    def test_strip_fullwidth_space(self):
        assert app_module._normalize_map_key("死生\u3000之刃") == "死生之刃"

    def test_fullwidth_hyphen(self):
        assert app_module._normalize_map_key("死生之刃－覺醒") == "死生之刃—覺醒"

    def test_combined_normalization(self):
        assert app_module._normalize_map_key("Ｇ 病毒・原始株（Ａ級）") == "G病毒·原始株(A級)"


# ===================================================================
# Fuzzy dedup in inventory (map type via schema.lists)
# ===================================================================


class TestFuzzyInventoryDedup:
    """Fuzzy key matching prevents duplicate inventory entries from LLM character variations."""

    def test_spaces_dedup(self, tmp_path, story_id, setup_state):
        """'C級支線劇情' update matches existing 'C 級支線劇情'."""
        setup_state("main", {**INITIAL_STATE, "inventory": {"C 級支線劇情": "進行中"}})
        app_module._apply_state_update_inner(
            story_id, "main",
            {"inventory": {"C級支線劇情": "已完成"}},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert "C 級支線劇情" in state["inventory"]
        assert state["inventory"]["C 級支線劇情"] == "已完成"
        assert "C級支線劇情" not in state["inventory"]

    def test_dots_dedup(self, tmp_path, story_id, setup_state):
        """'G病毒·原始株' matches 'G病毒‧原始株'."""
        setup_state("main", {**INITIAL_STATE, "inventory": {"G病毒‧原始株": ""}})
        app_module._apply_state_update_inner(
            story_id, "main",
            {"inventory": {"G病毒·原始株": "已使用"}},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert "G病毒‧原始株" in state["inventory"]
        assert state["inventory"]["G病毒‧原始株"] == "已使用"
        assert "G病毒·原始株" not in state["inventory"]

    def test_brackets_dedup(self, tmp_path, story_id, setup_state):
        """'空間戒指（5立方公尺）' matches '空間戒指(5立方公尺)'."""
        setup_state("main", {**INITIAL_STATE, "inventory": {"空間戒指(5立方公尺)": ""}})
        app_module._apply_state_update_inner(
            story_id, "main",
            {"inventory": {"空間戒指（5立方公尺）": "已裝備"}},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert "空間戒指(5立方公尺)" in state["inventory"]
        assert state["inventory"]["空間戒指(5立方公尺)"] == "已裝備"

    def test_no_false_positive(self, tmp_path, story_id, setup_state):
        """Genuinely different items remain separate."""
        setup_state("main", {**INITIAL_STATE, "inventory": {"冰劍": ""}})
        app_module._apply_state_update_inner(
            story_id, "main",
            {"inventory": {"火劍": ""}},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert "冰劍" in state["inventory"]
        assert "火劍" in state["inventory"]
        assert len([k for k in state["inventory"] if k in ("冰劍", "火劍")]) == 2

    def test_fuzzy_removal_base_name(self, tmp_path, story_id, setup_state):
        """Removing with base-name fallback: {'道具名（舊狀態）': null} removes '道具名（新狀態）'."""
        setup_state("main", {**INITIAL_STATE, "inventory": {"道具名（新狀態）": "強化"}})
        app_module._apply_state_update_inner(
            story_id, "main",
            {"inventory": {"道具名（舊狀態）": None}},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert "道具名（新狀態）" not in state["inventory"]
        assert "道具名（舊狀態）" not in state["inventory"]

    def test_fullwidth_alpha_dedup(self, tmp_path, story_id, setup_state):
        """Full-width 'Ｓ級武器' matches half-width 'S級武器'."""
        setup_state("main", {**INITIAL_STATE, "inventory": {"S級武器": ""}})
        app_module._apply_state_update_inner(
            story_id, "main",
            {"inventory": {"Ｓ級武器": "強化版"}},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert "S級武器" in state["inventory"]
        assert state["inventory"]["S級武器"] == "強化版"
        assert "Ｓ級武器" not in state["inventory"]


# ===================================================================
# Fuzzy dedup in relationships (map type via schema.lists)
# ===================================================================


class TestFuzzyRelationshipsDedup:
    def test_relationships_space_dedup(self, tmp_path, story_id, setup_state):
        """'小 薇' update matches existing '小薇'."""
        setup_state()
        app_module._apply_state_update_inner(
            story_id, "main",
            {"relationships": {"小 薇": "深厚信任"}},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert "小薇" in state["relationships"]
        assert state["relationships"]["小薇"] == "深厚信任"
        assert "小 薇" not in state["relationships"]


# ===================================================================
# Fuzzy dedup in systems (map type via schema.fields)
# ===================================================================


class TestFuzzySystemsDedup:
    def test_systems_dot_dedup(self, tmp_path, story_id, setup_state):
        """'死生・之道' matches existing '死生·之道'."""
        setup_state(state={**INITIAL_STATE_WITH_SYSTEMS,
                           "systems": {"死生·之道": "B級"}})
        app_module._apply_state_update_inner(
            story_id, "main",
            {"systems": {"死生・之道": "A級"}},
            SCHEMA_WITH_FIELDS_MAP,
        )
        state = _load_state(tmp_path, story_id)
        assert "死生·之道" in state["systems"]
        assert state["systems"]["死生·之道"] == "A級"
        assert "死生・之道" not in state["systems"]

    def test_systems_removal_base_name_fallback(self, tmp_path, story_id, setup_state):
        """Removing system by base-name fallback when key has paren suffix."""
        setup_state(state={**INITIAL_STATE_WITH_SYSTEMS,
                           "systems": {"修真之道（入門）": "C級"}})
        app_module._apply_state_update_inner(
            story_id, "main",
            {"systems": {"修真之道（進階）": None}},
            SCHEMA_WITH_FIELDS_MAP,
        )
        state = _load_state(tmp_path, story_id)
        assert "修真之道（入門）" not in state["systems"]
        assert "修真之道（進階）" not in state["systems"]


# ===================================================================
# Fuzzy dedup via legacy inventory_add path
# ===================================================================


class TestFuzzyLegacyAdd:
    def test_legacy_add_fuzzy_matches_existing(self, tmp_path, story_id, setup_state):
        """Legacy inventory_add with space variation should match existing key."""
        setup_state("main", {**INITIAL_STATE, "inventory": {"G 病毒·原始株": ""}})
        app_module._apply_state_update_inner(
            story_id, "main",
            {"inventory_add": ["G病毒·原始株（已使用）"]},
            SCHEMA,
        )
        state = _load_state(tmp_path, story_id)
        assert "G 病毒·原始株" in state["inventory"]
        assert "G病毒·原始株" not in state["inventory"]
