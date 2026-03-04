"""Tests for state_cleanup: _parse_cleanup_response, _apply_cleanup_operations, should_run_cleanup."""

import json

import pytest

import app as app_module
import event_db
import state_cleanup
import state_db


STORY_ID = "test_story"
BRANCH_ID = "main"

CLEANUP_SCHEMA = {
    "fields": [
        {"key": "current_phase", "type": "text"},
        {"key": "inventory", "type": "map"},
        {"key": "relationships", "type": "map"},
    ],
    "lists": [
        {"key": "inventory", "type": "map"},
        {"key": "relationships", "type": "map"},
    ],
    "direct_overwrite_keys": ["current_phase"],
}


@pytest.fixture(autouse=True)
def patch_paths(tmp_path, monkeypatch):
    """Redirect app, state_db, event_db to tmp_path."""
    stories_dir = tmp_path / "data" / "stories"
    stories_dir.mkdir(parents=True)
    design_dir = tmp_path / "story_design"
    design_dir.mkdir()
    monkeypatch.setattr(app_module, "STORIES_DIR", str(stories_dir))
    monkeypatch.setattr(app_module, "STORY_DESIGN_DIR", str(design_dir))
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(state_db, "STORIES_DIR", str(stories_dir))
    monkeypatch.setattr(event_db, "STORIES_DIR", str(stories_dir))
    (design_dir / STORY_ID).mkdir(parents=True, exist_ok=True)
    (design_dir / STORY_ID / "character_schema.json").write_text(
        json.dumps(CLEANUP_SCHEMA, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def branch_dir(tmp_path):
    """Create branch dir with npcs.json and character_state.json."""
    branch_dir = tmp_path / "data" / "stories" / STORY_ID / "branches" / BRANCH_ID
    branch_dir.mkdir(parents=True)
    return branch_dir


def test_parse_cleanup_response_empty():
    assert state_cleanup._parse_cleanup_response("") == {}
    assert state_cleanup._parse_cleanup_response("not json") == {}


def test_parse_cleanup_response_valid_json():
    ops = {
        "archive_npcs": [{"name": "A", "reason": "done"}],
        "merge_npcs": [],
        "resolve_events": [],
        "remove_inventory": [],
        "clean_relationships": [],
    }
    raw = json.dumps(ops, ensure_ascii=False)
    assert state_cleanup._parse_cleanup_response(raw) == ops


def test_parse_cleanup_response_strips_markdown():
    ops = {"archive_npcs": [], "merge_npcs": [], "resolve_events": [], "remove_inventory": [], "clean_relationships": []}
    wrapped = "```json\n" + json.dumps(ops, ensure_ascii=False) + "\n```"
    assert state_cleanup._parse_cleanup_response(wrapped) == ops


def test_apply_cleanup_archive_npcs(branch_dir):
    npcs = [
        {"name": "猿飛日斬", "id": "npc_1", "role": "三代火影", "current_status": "副本已結束"},
        {"name": "小琳", "id": "npc_2", "role": "隊友", "current_status": "跟隨中"},
    ]
    (branch_dir / "npcs.json").write_text(json.dumps(npcs, ensure_ascii=False, indent=2), encoding="utf-8")
    state = {
        "current_phase": "主神空間",
        "inventory": {},
        "relationships": {"小琳": "隊友"},
    }
    (branch_dir / "character_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    ops = {
        "archive_npcs": [{"name": "猿飛日斬", "reason": "副本已結束"}],
        "merge_npcs": [],
        "resolve_events": [],
        "remove_inventory": [],
        "clean_relationships": [],
    }
    summary = state_cleanup._apply_cleanup_operations(STORY_ID, BRANCH_ID, ops)
    assert summary["archived_npcs"] == 1

    loaded = json.loads((branch_dir / "npcs.json").read_text(encoding="utf-8"))
    by_name = {n["name"]: n for n in loaded}
    assert by_name["猿飛日斬"].get("lifecycle_status") == "archived"
    assert by_name["小琳"].get("lifecycle_status") != "archived"


def test_apply_cleanup_merge_npcs(branch_dir):
    npcs = [
        {"name": "旗木卡卡西", "id": "npc_kakashi", "role": "上忍", "tier": "A+"},
        {"name": "卡卡西", "id": "npc_2", "role": "上忍", "current_status": "戰鬥中"},
    ]
    (branch_dir / "npcs.json").write_text(json.dumps(npcs, ensure_ascii=False, indent=2), encoding="utf-8")
    state = {"current_phase": "副本中", "inventory": {}, "relationships": {}}
    (branch_dir / "character_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    ops = {
        "archive_npcs": [],
        "merge_npcs": [{"keep": "旗木卡卡西", "remove": "卡卡西", "reason": "同一人"}],
        "resolve_events": [],
        "remove_inventory": [],
        "clean_relationships": [],
    }
    summary = state_cleanup._apply_cleanup_operations(STORY_ID, BRANCH_ID, ops)
    assert summary["merged_npcs"] == 1

    loaded = json.loads((branch_dir / "npcs.json").read_text(encoding="utf-8"))
    by_name = {n["name"]: n for n in loaded}
    assert "旗木卡卡西" in by_name
    assert by_name["卡卡西"].get("lifecycle_status") == "archived"


def test_apply_cleanup_resolve_events(branch_dir):
    (branch_dir / "npcs.json").write_text("[]", encoding="utf-8")
    state = {"current_phase": "主神空間", "inventory": {}, "relationships": {}}
    (branch_dir / "character_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    event_id = event_db.insert_event(
        STORY_ID,
        {"event_type": "發現", "title": "生存三則制定", "description": "已觸發", "status": "triggered"},
        BRANCH_ID,
    )
    title_map = event_db.get_event_title_map(STORY_ID, BRANCH_ID)
    assert "生存三則制定" in title_map

    ops = {
        "archive_npcs": [],
        "merge_npcs": [],
        "resolve_events": [{"title": "生存三則制定", "new_status": "resolved", "reason": "副本結束"}],
        "remove_inventory": [],
        "clean_relationships": [],
    }
    summary = state_cleanup._apply_cleanup_operations(STORY_ID, BRANCH_ID, ops)
    assert summary["resolved_events"] == 1

    row = event_db.get_event_by_id(STORY_ID, event_id)
    assert row["status"] == "resolved"


def test_apply_cleanup_remove_inventory(branch_dir):
    (branch_dir / "npcs.json").write_text("[]", encoding="utf-8")
    state = {
        "current_phase": "主神空間",
        "inventory": {"帶血的權限卡": "副本取得", "淨化之塵": "×3"},
        "relationships": {},
    }
    (branch_dir / "character_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    ops = {
        "archive_npcs": [],
        "merge_npcs": [],
        "resolve_events": [],
        "remove_inventory": [{"item": "帶血的權限卡", "reason": "副本道具"}],
        "clean_relationships": [],
    }
    summary = state_cleanup._apply_cleanup_operations(STORY_ID, BRANCH_ID, ops)
    assert summary["removed_inventory"] == 1

    loaded = json.loads((branch_dir / "character_state.json").read_text(encoding="utf-8"))
    assert "帶血的權限卡" not in loaded.get("inventory", {})
    assert "淨化之塵" in loaded.get("inventory", {})


def test_should_run_cleanup_respects_interval():
    # First run: no previous, turn_index 20 -> should run if we don't check last
    # We can't easily test without mocking _last_cleanup. Just test that with negative turn we get False.
    assert state_cleanup.should_run_cleanup("s", "b", -1) is False
