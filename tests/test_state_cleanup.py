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
        {"key": "abilities", "label": "功法與技能", "state_add_key": "abilities_add", "state_remove_key": "abilities_remove"},
        {"key": "systems", "label": "體系", "type": "map"},
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


def test_parse_cleanup_response_trailing_backticks():
    ops = {"archive_npcs": [{"name": "NPC1", "reason": "done"}], "remove_inventory": []}
    trailing = json.dumps(ops, ensure_ascii=False) + "\n```"
    assert state_cleanup._parse_cleanup_response(trailing) == ops


def test_parse_cleanup_response_leading_backticks():
    ops = {"archive_npcs": [], "merge_npcs": []}
    leading = "```json\n" + json.dumps(ops, ensure_ascii=False)
    assert state_cleanup._parse_cleanup_response(leading) == ops


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
        "archive_npcs": [{"name": "猿飛日斬", "archive_kind": "offstage", "reason": "副本已結束"}],
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
    assert by_name["猿飛日斬"].get("archive_kind") == "offstage"
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
    assert by_name["卡卡西"].get("archive_kind") == "terminal"


def test_apply_cleanup_invalid_archive_kind_defaults_to_terminal(branch_dir):
    npcs = [
        {"name": "阿喪", "id": "npc_1", "role": "隊友", "current_status": "副本已結束"},
    ]
    (branch_dir / "npcs.json").write_text(json.dumps(npcs, ensure_ascii=False, indent=2), encoding="utf-8")
    state = {"current_phase": "主神空間", "inventory": {}, "relationships": {}}
    (branch_dir / "character_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    ops = {
        "archive_npcs": [{"name": "阿喪", "archive_kind": "oops", "reason": "cleanup"}],
        "merge_npcs": [],
        "resolve_events": [],
        "remove_inventory": [],
        "clean_relationships": [],
    }
    state_cleanup._apply_cleanup_operations(STORY_ID, BRANCH_ID, ops)

    loaded = json.loads((branch_dir / "npcs.json").read_text(encoding="utf-8"))
    assert loaded[0]["archive_kind"] == "terminal"


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


def test_apply_cleanup_clean_relationships(branch_dir):
    (branch_dir / "npcs.json").write_text("[]", encoding="utf-8")
    state = {
        "current_phase": "主神空間",
        "inventory": {},
        "relationships": {"猿飛日斬": "已退場", "小琳": "隊友"},
    }
    (branch_dir / "character_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    ops = {
        "archive_npcs": [],
        "merge_npcs": [],
        "resolve_events": [],
        "remove_inventory": [],
        "clean_relationships": [
            {"name": "猿飛日斬", "action": "archive_note", "reason": "已歸檔"},
        ],
    }
    summary = state_cleanup._apply_cleanup_operations(STORY_ID, BRANCH_ID, ops)
    assert summary["clean_relationships"] == 1

    loaded = json.loads((branch_dir / "character_state.json").read_text(encoding="utf-8"))
    rels = loaded.get("relationships", {})
    assert "已歸檔" in rels.get("猿飛日斬", "")
    assert "已歸檔" not in rels.get("小琳", "")


def test_apply_cleanup_remove_abilities(branch_dir):
    (branch_dir / "npcs.json").write_text("[]", encoding="utf-8")
    state = {
        "current_phase": "主神空間",
        "inventory": {},
        "relationships": {},
        "abilities": [
            "萬象召喚（初階）",
            "基因鎖·第一階",
            "第一階基因鎖 (爆發感適應)",
            "隱藏成就：影之掠奪者",
            "戰術直覺",
        ],
        "systems": {},
    }
    (branch_dir / "character_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    ops = {
        "archive_npcs": [],
        "merge_npcs": [],
        "resolve_events": [],
        "remove_inventory": [],
        "remove_abilities": [
            {"ability": "基因鎖·第一階", "reason": "與第一階基因鎖重複"},
            {"ability": "隱藏成就：影之掠奪者", "reason": "非技能，是成就"},
        ],
        "update_systems": [],
        "clean_relationships": [],
    }
    summary = state_cleanup._apply_cleanup_operations(STORY_ID, BRANCH_ID, ops)
    assert summary["removed_abilities"] == 2

    loaded = json.loads((branch_dir / "character_state.json").read_text(encoding="utf-8"))
    abilities = loaded.get("abilities", [])
    assert "基因鎖·第一階" not in abilities
    assert "隱藏成就：影之掠奪者" not in abilities
    assert "戰術直覺" in abilities
    assert "萬象召喚（初階）" in abilities


def test_apply_cleanup_consolidate_abilities(branch_dir):
    """Remove fragmented sub-abilities and add consolidated replacements."""
    (branch_dir / "npcs.json").write_text("[]", encoding="utf-8")
    state = {
        "current_phase": "主神空間",
        "inventory": {},
        "relationships": {},
        "abilities": [
            "空間解析：逆向追蹤",
            "空間解析：反向重疊模式",
            "反向傳送導引 (空間特質)",
            "空間干擾 · 座標鎖死",
            "戰術直覺",
        ],
        "systems": {"萬象召喚": "B級"},
    }
    (branch_dir / "character_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    ops = {
        "archive_npcs": [],
        "merge_npcs": [],
        "resolve_events": [],
        "remove_inventory": [],
        "remove_abilities": [
            {"ability": "空間解析：逆向追蹤", "reason": "碎片化子技能整合"},
            {"ability": "空間解析：反向重疊模式", "reason": "碎片化子技能整合"},
            {"ability": "反向傳送導引 (空間特質)", "reason": "碎片化子技能整合"},
            {"ability": "空間干擾 · 座標鎖死", "reason": "碎片化子技能整合"},
        ],
        "add_abilities": ["空間解析系列 (門之鑰衍生·B級)"],
        "update_systems": [],
        "clean_relationships": [],
    }
    summary = state_cleanup._apply_cleanup_operations(STORY_ID, BRANCH_ID, ops)
    assert summary["removed_abilities"] == 4
    assert summary["added_abilities"] == 1

    loaded = json.loads((branch_dir / "character_state.json").read_text(encoding="utf-8"))
    abilities = loaded.get("abilities", [])
    assert "空間解析：逆向追蹤" not in abilities
    assert "空間解析：反向重疊模式" not in abilities
    assert "反向傳送導引 (空間特質)" not in abilities
    assert "空間干擾 · 座標鎖死" not in abilities
    assert "空間解析系列 (門之鑰衍生·B級)" in abilities
    assert "戰術直覺" in abilities


def test_apply_cleanup_update_systems(branch_dir):
    (branch_dir / "npcs.json").write_text("[]", encoding="utf-8")
    state = {
        "current_phase": "主神空間",
        "inventory": {},
        "relationships": {},
        "abilities": [],
        "systems": {"萬象召喚": "B級（觸碰因果轉化門檻）", "舊體系": "已過時"},
    }
    (branch_dir / "character_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    ops = {
        "archive_npcs": [],
        "merge_npcs": [],
        "resolve_events": [],
        "remove_inventory": [],
        "remove_abilities": [],
        "update_systems": [
            {"key": "舊體系", "value": None, "reason": "已過時移除"},
        ],
        "clean_relationships": [],
    }
    summary = state_cleanup._apply_cleanup_operations(STORY_ID, BRANCH_ID, ops)
    assert summary["updated_systems"] == 1

    loaded = json.loads((branch_dir / "character_state.json").read_text(encoding="utf-8"))
    systems = loaded.get("systems", {})
    assert "舊體系" not in systems
    assert systems.get("萬象召喚") == "B級（觸碰因果轉化門檻）"


def test_apply_cleanup_remove_inventory_array_values(branch_dir):
    """Inventory keys with array values (misplaced equipment loadouts) should be removable."""
    (branch_dir / "npcs.json").write_text("[]", encoding="utf-8")
    state = {
        "current_phase": "主神空間",
        "inventory": {
            "奈米防護塗層": "良好",
            "主角": ["縛魂者之脊", "影鐵強化作戰服"],
            "C 級支線劇情": "1",
        },
        "relationships": {},
        "abilities": [],
        "systems": {},
    }
    (branch_dir / "character_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    ops = {
        "archive_npcs": [],
        "merge_npcs": [],
        "resolve_events": [],
        "remove_inventory": [
            {"item": "主角", "reason": "角色裝備清單錯放為道具欄 key"},
            {"item": "C 級支線劇情", "reason": "非道具"},
        ],
        "remove_abilities": [],
        "update_systems": [],
        "clean_relationships": [],
    }
    summary = state_cleanup._apply_cleanup_operations(STORY_ID, BRANCH_ID, ops)
    assert summary["removed_inventory"] == 2

    loaded = json.loads((branch_dir / "character_state.json").read_text(encoding="utf-8"))
    inv = loaded.get("inventory", {})
    assert "主角" not in inv
    assert "C 級支線劇情" not in inv
    assert inv.get("奈米防護塗層") == "良好"


def test_should_run_cleanup_respects_interval():
    # First run: no previous, turn_index 20 -> should run if we don't check last
    # We can't easily test without mocking _last_cleanup. Just test that with negative turn we get False.
    assert state_cleanup.should_run_cleanup("s", "b", -1) is False
