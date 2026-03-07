"""Tests for Debug Panel API routes."""

import json

import pytest

import app as app_module
import event_db
import lore_db
import state_db


@pytest.fixture(autouse=True)
def patch_app_paths(tmp_path, monkeypatch, patch_paths_all_modules):
    data_dir = tmp_path / "data"
    stories_dir = data_dir / "stories"
    stories_dir.mkdir(parents=True)
    design_dir = tmp_path / "story_design"
    design_dir.mkdir()
    patch_paths_all_modules(monkeypatch, tmp_path, stories_dir, design_dir, app_module=app_module)
    monkeypatch.setattr(app_module, "_log_llm_usage", lambda *a, **kw: None)
    return stories_dir


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def story_id():
    return "test_story"


@pytest.fixture
def setup_story(tmp_path, story_id):
    stories_dir = tmp_path / "data" / "stories"
    story_dir = stories_dir / story_id
    main_branch = story_dir / "branches" / "main"
    blank_root = story_dir / "branches" / "branch_blank_root"
    blank_child = story_dir / "branches" / "branch_blank_child"
    for d in (main_branch, blank_root, blank_child):
        d.mkdir(parents=True, exist_ok=True)

    design_dir = tmp_path / "story_design" / story_id
    design_dir.mkdir(parents=True, exist_ok=True)

    (tmp_path / "data" / "stories.json").write_text(
        json.dumps({
            "active_story_id": story_id,
            "stories": {story_id: {"id": story_id, "name": "測試故事"}},
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    (design_dir / "system_prompt.txt").write_text(
        "你是GM。\n{character_state}\n{narrative_recap}\n{world_lore}\n{npc_profiles}\n{team_rules}\n{other_agents}\n{critical_facts}",
        encoding="utf-8",
    )
    schema = {
        "fields": [
            {"key": "name", "type": "text"},
            {"key": "current_phase", "type": "text"},
            {"key": "reward_points", "type": "number"},
            {"key": "current_status", "type": "text"},
        ],
        "lists": [
            {"key": "inventory", "type": "map"},
            {"key": "relationships", "type": "map"},
        ],
        "direct_overwrite_keys": ["current_phase", "current_status"],
    }
    (design_dir / "character_schema.json").write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    default_state = {
        "name": "測試者",
        "current_phase": "主神空間",
        "reward_points": 1000,
        "inventory": {},
        "relationships": {},
        "current_status": "待命",
    }
    (design_dir / "default_character_state.json").write_text(
        json.dumps(default_state, ensure_ascii=False), encoding="utf-8"
    )
    (design_dir / "world_lore.json").write_text("[]", encoding="utf-8")
    (design_dir / "parsed_conversation.json").write_text("[]", encoding="utf-8")

    tree = {
        "active_branch_id": "main",
        "branches": {
            "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None, "name": "主線"},
            "branch_blank_root": {
                "id": "branch_blank_root",
                "parent_branch_id": "main",
                "branch_point_index": -1,
                "blank": True,
                "name": "空白根",
            },
            "branch_blank_child": {
                "id": "branch_blank_child",
                "parent_branch_id": "branch_blank_root",
                "branch_point_index": 2,
                "name": "空白子分支",
            },
        },
    }
    (story_dir / "timeline_tree.json").write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")

    for bid in ("main", "branch_blank_root", "branch_blank_child"):
        bdir = story_dir / "branches" / bid
        (bdir / "messages.json").write_text("[]", encoding="utf-8")
        (bdir / "character_state.json").write_text(json.dumps(default_state, ensure_ascii=False), encoding="utf-8")
        (bdir / "npcs.json").write_text("[]", encoding="utf-8")
        (bdir / "dungeon_progress.json").write_text(
            json.dumps({"history": [], "current_dungeon": None, "total_dungeons_completed": 0}, ensure_ascii=False),
            encoding="utf-8",
        )

    return story_dir


def _read_sse_events(resp) -> list[dict]:
    body = b"".join(resp.response).decode("utf-8")
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def test_debug_session_uses_blank_root_unit(client, setup_story):
    resp = client.get("/api/debug/session?branch_id=branch_blank_child")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["debug_unit_id"] == "branch_blank_root"
    assert data["target_branch_id"] == "branch_blank_child"


def test_debug_chat_stream_parses_tags_and_persists_history(client, setup_story, monkeypatch):
    def _fake_stream(*_args, **_kwargs):
        yield ("text", "先做檢查。\n")
        yield ("done", {
            "response": (
                "先做檢查。\n"
                "<!--DEBUG_ACTION {\"type\":\"state_patch\",\"update\":{\"reward_points_delta\":50}} DEBUG_ACTION-->\n"
                "<!--DEBUG_DIRECTIVE {\"instruction\":\"下一回合請確認主線任務狀態\"} DEBUG_DIRECTIVE-->"
            ),
            "usage": None,
        })

    monkeypatch.setattr(app_module, "call_claude_gm_stream", _fake_stream)

    resp = client.post("/api/debug/chat/stream", json={
        "branch_id": "branch_blank_child",
        "user_message": "幫我檢查獎勵點和任務狀態",
    })
    assert resp.status_code == 200
    events = _read_sse_events(resp)
    done = next(e for e in events if e.get("type") == "done")
    assert "先做檢查" in done["response"]
    assert done["proposals"][0]["type"] == "state_patch"
    assert done["directives"][0]["instruction"].startswith("下一回合")

    chat_path = setup_story / "debug_units" / "branch_blank_root" / "chat.json"
    chat = json.loads(chat_path.read_text(encoding="utf-8"))
    assert len(chat) == 2
    assert chat[0]["role"] == "user"
    assert chat[1]["role"] == "assistant"


def test_debug_chat_stream_normalizes_action_without_type(client, setup_story, monkeypatch):
    def _fake_stream(*_args, **_kwargs):
        yield ("done", {
            "response": (
                "可先整理欄位。\n"
                "<!--DEBUG_ACTION {\"update\":{\"reward_points_delta\":5}} DEBUG_ACTION-->"
            ),
            "usage": None,
        })

    monkeypatch.setattr(app_module, "call_claude_gm_stream", _fake_stream)

    resp = client.post("/api/debug/chat/stream", json={
        "branch_id": "main",
        "user_message": "整理一下欄位",
    })
    assert resp.status_code == 200
    events = _read_sse_events(resp)
    done = next(e for e in events if e.get("type") == "done")
    assert done["proposals"][0]["type"] == "state_patch"
    assert done["proposals"][0]["update"]["reward_points_delta"] == 5


def test_debug_chat_stream_normalizes_action_with_patch_alias(client, setup_story, monkeypatch):
    def _fake_stream(*_args, **_kwargs):
        yield ("done", {
            "response": (
                "已整理。\n"
                "<!--DEBUG_ACTION {\"action\":\"state_patch\",\"patch\":{\"reward_points_delta\":7}} DEBUG_ACTION-->"
            ),
            "usage": None,
        })

    monkeypatch.setattr(app_module, "call_claude_gm_stream", _fake_stream)

    resp = client.post("/api/debug/chat/stream", json={
        "branch_id": "main",
        "user_message": "整理一下欄位",
    })
    assert resp.status_code == 200
    events = _read_sse_events(resp)
    done = next(e for e in events if e.get("type") == "done")
    assert done["proposals"][0]["type"] == "state_patch"
    assert done["proposals"][0]["update"]["reward_points_delta"] == 7


def test_debug_apply_partial_success_and_audit(client, setup_story):
    resp = client.post("/api/debug/apply", json={
        "branch_id": "main",
        "actions": [
            {"type": "state_patch", "update": {"reward_points_delta": 100}},
            {"type": "dungeon_patch", "progress_delta": 10},
        ],
        "directives": [
            {"instruction": "下一回合請補主線完成提示"},
        ],
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data["results"]) == 2
    assert data["results"][0]["ok"] is True
    assert data["results"][1]["ok"] is False
    assert data["results"][1]["error"] == "no active dungeon"
    assert data["directive_result"]["applied"] == 1
    assert "1/2" in data["audit_summary"]

    directive_path = setup_story / "branches" / "main" / "debug_directive.json"
    assert directive_path.exists()

    messages = json.loads((setup_story / "branches" / "main" / "messages.json").read_text(encoding="utf-8"))
    assert any(m.get("message_type") == "debug_audit" for m in messages)


def test_debug_apply_rejects_too_many_directives(client, setup_story):
    resp = client.post("/api/debug/apply", json={
        "branch_id": "main",
        "actions": [],
        "directives": [
            {"instruction": f"指令 {i}"}
            for i in range(app_module.DEBUG_APPLY_MAX_DIRECTIVES + 1)
        ],
    })
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error"] == f"too many directives (max {app_module.DEBUG_APPLY_MAX_DIRECTIVES})"

    backup_path = setup_story / "debug_units" / "main" / "last_apply_backup.json"
    assert not backup_path.exists()


def test_debug_apply_infers_state_patch_when_type_missing(client, setup_story):
    resp = client.post("/api/debug/apply", json={
        "branch_id": "main",
        "actions": [
            {"update": {"reward_points_delta": 10}},
        ],
        "directives": [],
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["results"][0]["type"] == "state_patch"
    assert data["results"][0]["ok"] is True

    state_path = setup_story / "branches" / "main" / "character_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["reward_points"] == 1010


def test_debug_apply_infers_state_patch_from_action_patch_alias(client, setup_story):
    resp = client.post("/api/debug/apply", json={
        "branch_id": "main",
        "actions": [
            {"action": "state_patch", "patch": {"reward_points_delta": 11}},
        ],
        "directives": [],
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["results"][0]["type"] == "state_patch"
    assert data["results"][0]["ok"] is True

    state_path = setup_story / "branches" / "main" / "character_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["reward_points"] == 1011


def test_debug_undo_restores_snapshot_and_clears_directive(client, setup_story):
    apply_resp = client.post("/api/debug/apply", json={
        "branch_id": "main",
        "actions": [
            {"type": "state_patch", "update": {"reward_points_delta": 200}},
            {"type": "world_day_set", "world_day": 12.5},
        ],
        "directives": [{"instruction": "下一回合請確認任務完成。"}],
    })
    assert apply_resp.status_code == 200

    state_path = setup_story / "branches" / "main" / "character_state.json"
    state_after_apply = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_after_apply["reward_points"] == 1200

    undo_resp = client.post("/api/debug/undo", json={"branch_id": "main"})
    assert undo_resp.status_code == 200
    undo = undo_resp.get_json()
    assert undo["ok"] is True
    assert undo["restored"] is True

    restored_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert restored_state["reward_points"] == 1000

    directive_path = setup_story / "branches" / "main" / "debug_directive.json"
    assert not directive_path.exists()

    messages = json.loads((setup_story / "branches" / "main" / "messages.json").read_text(encoding="utf-8"))
    audits = [m for m in messages if m.get("message_type") == "debug_audit"]
    assert len(audits) == 2
    assert "已回滾 Debug 修正" in audits[-1]["content"]


def test_debug_undo_rejects_invalid_backup_world_day_without_partial_restore(client, setup_story):
    state_path = setup_story / "branches" / "main" / "character_state.json"
    mutated_state = json.loads(state_path.read_text(encoding="utf-8"))
    mutated_state["reward_points"] = 1337
    state_path.write_text(json.dumps(mutated_state, ensure_ascii=False), encoding="utf-8")

    directive_path = setup_story / "branches" / "main" / "debug_directive.json"
    directive_path.write_text(json.dumps({
        "instruction": "保留現有 directive",
    }, ensure_ascii=False), encoding="utf-8")

    backup_path = setup_story / "debug_units" / "main" / "last_apply_backup.json"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(json.dumps({
        "version": 1,
        "created_at": "2026-03-07T00:00:00+00:00",
        "debug_unit_id": "main",
        "target_branch_id": "main",
        "state_snapshot": {
            "name": "測試者",
            "current_phase": "主神空間",
            "reward_points": 1000,
            "inventory": {},
            "relationships": {},
            "current_status": "待命",
        },
        "npcs_snapshot": [],
        "world_day": "oops",
        "dungeon_progress_snapshot": {
            "history": [],
            "current_dungeon": None,
            "total_dungeons_completed": 0,
        },
    }, ensure_ascii=False), encoding="utf-8")

    undo_resp = client.post("/api/debug/undo", json={"branch_id": "main"})
    assert undo_resp.status_code == 400
    undo = undo_resp.get_json()
    assert undo["ok"] is False
    assert undo["error"] == "backup world_day invalid"

    current_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert current_state["reward_points"] == 1337
    assert directive_path.exists()
    assert backup_path.exists()
