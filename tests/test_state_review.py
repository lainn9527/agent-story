"""Tests for the deterministic state validation gate.

Tests _validate_state_update() pure function, _run_state_gate() mode behavior,
and integration with _apply_state_update() via STATE_REVIEW_MODE.
"""

import json

import pytest

import app as app_module


# Schema matching the real project
SCHEMA = {
    "fields": [
        {"key": "name", "label": "姓名", "type": "text"},
        {"key": "current_phase", "label": "階段", "type": "text"},
        {"key": "reward_points", "label": "獎勵點", "type": "number"},
        {"key": "current_status", "label": "狀態", "type": "text"},
        {"key": "gene_lock", "label": "基因鎖", "type": "text"},
        {"key": "physique", "label": "體質", "type": "text"},
        {"key": "spirit", "label": "精神力", "type": "text"},
        {"key": "systems", "label": "體系", "type": "map"},
    ],
    "lists": [
        {"key": "inventory", "label": "道具欄", "type": "map"},
        {
            "key": "abilities",
            "label": "能力",
            "state_add_key": "abilities_add",
            "state_remove_key": "abilities_remove",
        },
        {
            "key": "completed_missions",
            "label": "已完成任務",
            "state_add_key": "completed_missions_add",
        },
        {"key": "relationships", "label": "人際關係", "type": "map", "render": "inline"},
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
    "abilities": ["火球術", "空間跳躍"],
    "gene_lock": "未開啟",
    "physique": "普通人",
    "spirit": "普通人",
    "current_status": "等待任務",
    "systems": {"死生之道": "B級"},
}


# ===================================================================
# _validate_state_update — pure function tests
# ===================================================================


class TestValidateCleanUpdate:
    def test_clean_update_no_violations(self):
        update = {
            "current_phase": "副本中",
            "reward_points_delta": -500,
            "inventory": {"新道具": ""},
            "gene_lock": "第一階",
        }
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized == update
        assert violations == []


class TestValidatePhase:
    def test_invalid_phase_dropped(self):
        update = {"current_phase": "戰鬥"}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "current_phase" not in sanitized
        assert len(violations) == 1
        assert violations[0]["rule"] == "invalid_phase"

    def test_valid_phase_kept(self):
        for phase in ["主神空間", "副本中", "副本結算", "傳送中", "死亡"]:
            sanitized, _ = app_module._validate_state_update(
                {"current_phase": phase}, SCHEMA, INITIAL_STATE)
            assert sanitized["current_phase"] == phase


class TestValidateRewardPoints:
    def test_reward_points_delta_non_numeric(self):
        update = {"reward_points_delta": "很多"}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "reward_points_delta" not in sanitized
        assert violations[0]["rule"] == "non_numeric_delta"

    def test_reward_points_non_numeric(self):
        update = {"reward_points": "一千"}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "reward_points" not in sanitized
        assert violations[0]["rule"] == "non_numeric_points"

    def test_reward_points_numeric_kept(self):
        update = {"reward_points": 3000, "reward_points_delta": -100}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized["reward_points"] == 3000
        assert sanitized["reward_points_delta"] == -100
        assert violations == []


class TestValidateMapFields:
    def test_map_field_not_dict(self):
        update = {"inventory": "劍"}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "inventory" not in sanitized
        assert violations[0]["rule"] == "map_not_dict"

    def test_map_field_dict_kept(self):
        update = {"inventory": {"新劍": "強化"}}
        sanitized, _ = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized["inventory"] == {"新劍": "強化"}

    def test_relationships_string_dropped(self):
        update = {"relationships": "小薇"}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "relationships" not in sanitized
        assert violations[0]["rule"] == "map_not_dict"

    def test_systems_string_dropped(self):
        update = {"systems": "A級"}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "systems" not in sanitized
        assert violations[0]["rule"] == "map_not_dict"


class TestValidateAddRemove:
    def test_non_schema_add_dropped(self):
        update = {"foo_add": ["bar"]}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "foo_add" not in sanitized
        assert violations[0]["rule"] == "non_schema_add_remove"

    def test_non_schema_remove_dropped(self):
        update = {"foo_remove": ["bar"]}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "foo_remove" not in sanitized
        assert violations[0]["rule"] == "non_schema_add_remove"

    def test_schema_add_kept(self):
        update = {"abilities_add": ["新技能"]}
        sanitized, _ = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized["abilities_add"] == ["新技能"]

    def test_schema_remove_kept(self):
        update = {"abilities_remove": ["火球術"]}
        sanitized, _ = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized["abilities_remove"] == ["火球術"]

    def test_inventory_add_fallback_kept(self):
        """P1 regression: inventory_add uses fallback key (no explicit state_add_key in schema)."""
        update = {"inventory_add": ["新道具"]}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized["inventory_add"] == ["新道具"]
        assert violations == []

    def test_inventory_remove_fallback_kept(self):
        """P1 regression: inventory_remove uses fallback key."""
        update = {"inventory_remove": ["封印之鏡"]}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized["inventory_remove"] == ["封印之鏡"]
        assert violations == []

    def test_inventory_add_string_wrapped(self):
        """P1 regression: inventory_add string value → wrapped in list."""
        update = {"inventory_add": "單一道具"}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized["inventory_add"] == ["單一道具"]
        assert violations == []

    def test_relationships_add_fallback_kept(self):
        """Relationships map also has fallback _add/_remove keys."""
        update = {"relationships_add": ["新NPC"]}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized["relationships_add"] == ["新NPC"]
        assert violations == []

    def test_add_string_wrapped_to_list(self):
        """Backward compat: string value for _add key → wrapped in list."""
        update = {"abilities_add": "火球術"}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized["abilities_add"] == ["火球術"]
        assert violations == []

    def test_remove_string_wrapped_to_list(self):
        """Backward compat: string value for _remove key → wrapped in list."""
        update = {"abilities_remove": "火球術"}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized["abilities_remove"] == ["火球術"]
        assert violations == []

    def test_add_not_list_or_string_dropped(self):
        update = {"abilities_add": 42}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "abilities_add" not in sanitized
        assert violations[0]["rule"] == "add_remove_not_list"

    def test_completed_missions_add_string_wrapped(self):
        """Legacy backward compat for completed_missions_add string."""
        update = {"completed_missions_add": "生化危機"}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized["completed_missions_add"] == ["生化危機"]
        assert violations == []


class TestValidateDelta:
    def test_delta_non_numeric(self):
        update = {"hp_delta": "增加"}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "hp_delta" not in sanitized
        assert violations[0]["rule"] == "delta_non_numeric"

    def test_delta_numeric_kept(self):
        update = {"hp_delta": 50}
        sanitized, _ = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized["hp_delta"] == 50


class TestValidateSceneKeys:
    def test_scene_keys_dropped(self):
        scene_keys = ["location", "threat_level", "combat_status", "escape_options"]
        update = {k: "value" for k in scene_keys}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        for k in scene_keys:
            assert k not in sanitized
        assert len(violations) == len(scene_keys)
        assert all(v["rule"] == "scene_key" for v in violations)


class TestValidateInstructionKeys:
    def test_instruction_keys_dropped(self):
        instruction_keys = ["inventory_use", "note", "skill_update", "status_change"]
        update = {k: "value" for k in instruction_keys}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        for k in instruction_keys:
            assert k not in sanitized
        assert len(violations) == len(instruction_keys)
        assert all(v["rule"] == "instruction_key" for v in violations)


class TestValidateDirectOverwrite:
    def test_direct_overwrite_list_dropped(self):
        update = {"gene_lock": ["第一階", "第二階"]}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "gene_lock" not in sanitized
        assert violations[0]["rule"] == "overwrite_not_string"

    def test_direct_overwrite_string_kept(self):
        update = {"gene_lock": "第一階"}
        sanitized, _ = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert sanitized["gene_lock"] == "第一階"

    def test_direct_overwrite_number_dropped(self):
        """Text fields (gene_lock, physique, etc.) must be strings — numbers are rejected."""
        update = {"physique": 5}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "physique" not in sanitized
        assert violations[0]["rule"] == "overwrite_not_string"

    def test_direct_overwrite_bool_dropped(self):
        """Booleans are not valid for text overwrite fields."""
        update = {"spirit": True}
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "spirit" not in sanitized
        assert violations[0]["rule"] == "overwrite_not_string"


class TestValidateMultipleViolations:
    def test_mixed_valid_and_invalid(self):
        update = {
            "current_phase": "戰鬥",          # invalid phase → drop
            "reward_points_delta": -500,       # valid
            "location": "深山",                # scene key → drop
            "inventory": {"劍": ""},           # valid
            "foo_add": ["bar"],                # non-schema → drop
            "gene_lock": "第一階",             # valid
        }
        sanitized, violations = app_module._validate_state_update(update, SCHEMA, INITIAL_STATE)
        assert "current_phase" not in sanitized
        assert "location" not in sanitized
        assert "foo_add" not in sanitized
        assert sanitized["reward_points_delta"] == -500
        assert sanitized["inventory"] == {"劍": ""}
        assert sanitized["gene_lock"] == "第一階"
        assert len(violations) == 3


# ===================================================================
# _run_state_gate — mode behavior
# ===================================================================


@pytest.fixture(autouse=True)
def patch_app_paths(tmp_path, monkeypatch):
    """Redirect app.py paths to tmp_path."""
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
    """Create branch dir with character state + schema."""
    def _setup(branch_id="main", state=None, schema=None):
        branch_dir = tmp_path / "data" / "stories" / story_id / "branches" / branch_id
        branch_dir.mkdir(parents=True, exist_ok=True)
        s = state or dict(INITIAL_STATE)
        (branch_dir / "character_state.json").write_text(
            json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        # Write schema so _load_character_schema finds it
        design_dir = tmp_path / "story_design" / story_id
        design_dir.mkdir(parents=True, exist_ok=True)
        sc = schema or SCHEMA
        (design_dir / "character_schema.json").write_text(
            json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8")
        return branch_dir
    return _setup


def _load_state(tmp_path, story_id, branch_id="main"):
    path = tmp_path / "data" / "stories" / story_id / "branches" / branch_id / "character_state.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestRunStateGate:
    def test_off_mode_returns_original(self, monkeypatch):
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "off")
        bad_update = {"current_phase": "戰鬥", "location": "山洞"}
        result = app_module._run_state_gate(bad_update, SCHEMA, INITIAL_STATE)
        assert result == bad_update  # unchanged

    def test_warn_mode_returns_original(self, monkeypatch):
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "warn")
        bad_update = {"current_phase": "戰鬥", "gene_lock": "第一階"}
        result = app_module._run_state_gate(bad_update, SCHEMA, INITIAL_STATE)
        assert result == bad_update  # original, not sanitized

    def test_enforce_mode_returns_sanitized(self, monkeypatch):
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        bad_update = {"current_phase": "戰鬥", "gene_lock": "第一階"}
        result = app_module._run_state_gate(bad_update, SCHEMA, INITIAL_STATE)
        assert "current_phase" not in result
        assert result["gene_lock"] == "第一階"


# ===================================================================
# Integration: _apply_state_update with STATE_REVIEW_MODE
# ===================================================================


class TestIntegrationEnforceMode:
    def test_enforce_drops_bad_keys_applies_good(self, tmp_path, story_id, setup_state, monkeypatch):
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        # Stub validate_dungeon_progression to avoid missing dungeon config
        monkeypatch.setattr(app_module, "validate_dungeon_progression", lambda *a, **kw: None)
        # Stub _normalize_state_async to avoid background threads
        monkeypatch.setattr(app_module, "_normalize_state_async", lambda *a, **kw: None)

        setup_state()
        update = {
            "current_phase": "戰鬥中",   # invalid → dropped
            "reward_points_delta": -500, # valid
            "location": "深山",          # scene key → dropped
            "gene_lock": "第一階",       # valid
        }
        app_module._apply_state_update(story_id, "main", update)
        state = _load_state(tmp_path, story_id)
        assert state["current_phase"] == "主神空間"  # unchanged from original
        assert state["reward_points"] == 4500         # 5000 - 500
        assert "location" not in state
        assert state["gene_lock"] == "第一階"

    def test_warn_mode_applies_original(self, tmp_path, story_id, setup_state, monkeypatch):
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "warn")
        monkeypatch.setattr(app_module, "validate_dungeon_progression", lambda *a, **kw: None)
        monkeypatch.setattr(app_module, "_normalize_state_async", lambda *a, **kw: None)

        setup_state()
        update = {
            "current_phase": "戰鬥中",   # invalid — but warn mode lets it through
            "gene_lock": "第一階",
        }
        app_module._apply_state_update(story_id, "main", update)
        state = _load_state(tmp_path, story_id)
        # In warn mode, the invalid phase gets through to _apply_state_update_inner
        # which applies it via direct_overwrite_keys
        assert state["current_phase"] == "戰鬥中"
        assert state["gene_lock"] == "第一階"

    def test_off_mode_skips_validation(self, tmp_path, story_id, setup_state, monkeypatch):
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "off")
        monkeypatch.setattr(app_module, "validate_dungeon_progression", lambda *a, **kw: None)
        monkeypatch.setattr(app_module, "_normalize_state_async", lambda *a, **kw: None)

        setup_state()
        update = {"current_phase": "戰鬥中", "location": "深山"}
        app_module._apply_state_update(story_id, "main", update)
        state = _load_state(tmp_path, story_id)
        # Everything applied as-is (location blocked by inner, phase applied)
        assert state["current_phase"] == "戰鬥中"
        assert "location" not in state  # inner still blocks scene keys


# ===================================================================
# Backward compat: string wrapping in enforce mode
# ===================================================================


class TestEnforceLegacyStringWrapping:
    """Ensure enforce mode preserves backward compat for string _add/_remove values."""

    def test_enforce_wraps_string_add(self, tmp_path, story_id, setup_state, monkeypatch):
        """In enforce mode, string _add value should be wrapped to list, not dropped."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "validate_dungeon_progression", lambda *a, **kw: None)
        monkeypatch.setattr(app_module, "_normalize_state_async", lambda *a, **kw: None)

        setup_state()
        update = {"abilities_add": "新技能"}
        app_module._apply_state_update(story_id, "main", update)
        state = _load_state(tmp_path, story_id)
        assert "新技能" in state["abilities"]

    def test_enforce_wraps_string_remove(self, tmp_path, story_id, setup_state, monkeypatch):
        """In enforce mode, string _remove value should be wrapped to list, not dropped."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "validate_dungeon_progression", lambda *a, **kw: None)
        monkeypatch.setattr(app_module, "_normalize_state_async", lambda *a, **kw: None)

        setup_state()
        update = {"abilities_remove": "火球術"}
        app_module._apply_state_update(story_id, "main", update)
        state = _load_state(tmp_path, story_id)
        assert "火球術" not in state["abilities"]

    def test_enforce_completed_missions_add_string(self, tmp_path, story_id, setup_state, monkeypatch):
        """completed_missions_add with string value → wrapped, not dropped."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "validate_dungeon_progression", lambda *a, **kw: None)
        monkeypatch.setattr(app_module, "_normalize_state_async", lambda *a, **kw: None)

        setup_state()
        update = {"completed_missions_add": "生化危機"}
        app_module._apply_state_update(story_id, "main", update)
        state = _load_state(tmp_path, story_id)
        assert "生化危機" in state["completed_missions"]

    def test_enforce_inventory_add_fallback(self, tmp_path, story_id, setup_state, monkeypatch):
        """P1 regression: inventory_add (fallback key, no explicit state_add_key) must work in enforce mode."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "validate_dungeon_progression", lambda *a, **kw: None)
        monkeypatch.setattr(app_module, "_normalize_state_async", lambda *a, **kw: None)

        setup_state()
        update = {"inventory_add": ["新道具"]}
        app_module._apply_state_update(story_id, "main", update)
        state = _load_state(tmp_path, story_id)
        assert "新道具" in state["inventory"]

    def test_enforce_inventory_remove_fallback(self, tmp_path, story_id, setup_state, monkeypatch):
        """P1 regression: inventory_remove (fallback key) must work in enforce mode."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "validate_dungeon_progression", lambda *a, **kw: None)
        monkeypatch.setattr(app_module, "_normalize_state_async", lambda *a, **kw: None)

        setup_state()
        update = {"inventory_remove": ["封印之鏡"]}
        app_module._apply_state_update(story_id, "main", update)
        state = _load_state(tmp_path, story_id)
        assert "封印之鏡" not in state["inventory"]

    def test_enforce_inventory_add_string_fallback(self, tmp_path, story_id, setup_state, monkeypatch):
        """P1 regression: inventory_add string → wrapped in list, not dropped, in enforce mode."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "validate_dungeon_progression", lambda *a, **kw: None)
        monkeypatch.setattr(app_module, "_normalize_state_async", lambda *a, **kw: None)

        setup_state()
        update = {"inventory_add": "單一道具"}
        app_module._apply_state_update(story_id, "main", update)
        state = _load_state(tmp_path, story_id)
        assert "單一道具" in state["inventory"]
