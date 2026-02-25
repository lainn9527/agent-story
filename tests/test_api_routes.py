"""Tests for Flask API routes in app.py (Phase 2.5).

Integration tests using Flask test_client. Tests CRUD routes for
stories, branches, events, lore, NPCs, config, and cheats.
LLM calls are mocked; tests focus on request/response contracts.
"""

import json
import os

import pytest

import app as app_module
import event_db
import lore_db


@pytest.fixture(autouse=True)
def patch_app_paths(tmp_path, monkeypatch):
    """Redirect all app paths to tmp_path."""
    data_dir = tmp_path / "data"
    stories_dir = data_dir / "stories"
    stories_dir.mkdir(parents=True)
    design_dir = tmp_path / "story_design"
    design_dir.mkdir()
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(app_module, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(app_module, "STORIES_DIR", str(stories_dir))
    monkeypatch.setattr(app_module, "STORY_DESIGN_DIR", str(design_dir))
    monkeypatch.setattr(app_module, "STORIES_REGISTRY_PATH", str(data_dir / "stories.json"))
    monkeypatch.setattr(app_module, "_LLM_CONFIG_PATH", str(tmp_path / "llm_config.json"))
    monkeypatch.setattr(event_db, "STORIES_DIR", str(stories_dir))
    monkeypatch.setattr(lore_db, "STORIES_DIR", str(stories_dir))
    monkeypatch.setattr(lore_db, "STORY_DESIGN_DIR", str(design_dir))
    lore_db._embedding_cache.clear()
    return stories_dir


@pytest.fixture
def client():
    """Flask test client."""
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def story_id():
    return "test_story"


@pytest.fixture
def setup_story(tmp_path, story_id):
    """Create a minimal story with all required files."""
    stories_dir = tmp_path / "data" / "stories"
    story_dir = stories_dir / story_id
    branch_dir = story_dir / "branches" / "main"
    branch_dir.mkdir(parents=True, exist_ok=True)

    # Design files directory
    design_dir = tmp_path / "story_design" / story_id
    design_dir.mkdir(parents=True, exist_ok=True)

    # Stories registry (dict keyed by story_id)
    registry = {
        "active_story_id": story_id,
        "stories": {
            story_id: {
                "id": story_id,
                "name": "測試故事",
                "description": "測試用",
                "created_at": "2026-01-01T00:00:00",
            },
        },
    }
    (tmp_path / "data" / "stories.json").write_text(
        json.dumps(registry, ensure_ascii=False), encoding="utf-8"
    )

    # Design files → story_design/
    (design_dir / "system_prompt.txt").write_text(
        "你是GM。\n{character_state}\n{narrative_recap}\n"
        "{world_lore}\n{npc_profiles}\n{team_rules}\n{other_agents}\n{critical_facts}",
        encoding="utf-8",
    )

    # Character state (runtime)
    state = {"name": "測試者", "current_phase": "主神空間", "reward_points": 5000, "inventory": []}
    (branch_dir / "character_state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )

    # Character schema → story_design/
    schema = {
        "fields": [
            {"key": "name", "label": "姓名"},
            {"key": "current_phase", "label": "階段"},
            {"key": "reward_points", "label": "獎勵點", "type": "number"},
        ],
        "lists": [
            {"key": "inventory", "label": "道具欄",
             "state_add_key": "inventory_add", "state_remove_key": "inventory_remove"},
        ],
        "direct_overwrite_keys": ["current_phase"],
    }
    (design_dir / "character_schema.json").write_text(json.dumps(schema), encoding="utf-8")

    # Default character state → story_design/
    (design_dir / "default_character_state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )

    # Timeline tree (runtime)
    tree = {
        "active_branch_id": "main",
        "branches": {
            "main": {
                "id": "main",
                "parent_branch_id": None,
                "branch_point_index": None,
                "name": "主線",
            },
        },
    }
    (story_dir / "timeline_tree.json").write_text(json.dumps(tree), encoding="utf-8")

    # World lore → story_design/
    (design_dir / "world_lore.json").write_text("[]", encoding="utf-8")

    # Parsed conversation → story_design/
    parsed = [
        {"index": 0, "role": "user", "content": "你好"},
        {"index": 1, "role": "assistant", "content": "歡迎來到主神空間"},
        {"index": 2, "role": "user", "content": "開始任務"},
        {"index": 3, "role": "assistant", "content": "任務開始了"},
    ]
    (design_dir / "parsed_conversation.json").write_text(
        json.dumps(parsed, ensure_ascii=False), encoding="utf-8"
    )

    # Branch messages (runtime)
    (branch_dir / "messages.json").write_text("[]", encoding="utf-8")

    # NPCs (runtime)
    (branch_dir / "npcs.json").write_text("[]", encoding="utf-8")

    # Branch config (runtime)
    (branch_dir / "branch_config.json").write_text("{}", encoding="utf-8")

    # LLM config (for /api/config)
    config = {
        "provider": "gemini",
        "gemini": {"api_keys": [{"key": "test_key_123", "tier": "free"}], "model": "gemini-2.5-flash"},
        "claude_cli": {"model": "claude-sonnet-4-5-20250929"},
    }
    (tmp_path / "llm_config.json").write_text(json.dumps(config), encoding="utf-8")

    return story_dir


# ===================================================================
# Stories CRUD
# ===================================================================


class TestStoriesAPI:
    def test_get_stories(self, client, setup_story, story_id):
        resp = client.get("/api/stories")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "stories" in data
        # stories is a dict keyed by story_id
        assert story_id in data["stories"]
        assert data["stories"][story_id]["name"] == "測試故事"

    def test_create_story(self, client, setup_story, tmp_path):
        resp = client.post("/api/stories", json={"name": "新故事", "description": "新的"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["story"]["name"] == "新故事"
        # Verify it appears in stories dict
        resp2 = client.get("/api/stories")
        assert len(resp2.get_json()["stories"]) == 2

    def test_switch_story(self, client, setup_story, story_id):
        resp = client.post("/api/stories/switch", json={"story_id": story_id})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["active_story_id"] == story_id

    def test_update_story(self, client, setup_story, story_id):
        resp = client.patch(f"/api/stories/{story_id}", json={"name": "改名故事"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        # Verify name changed
        resp2 = client.get("/api/stories")
        stories = resp2.get_json()["stories"]
        assert stories[story_id]["name"] == "改名故事"

    def test_get_schema(self, client, setup_story, story_id):
        resp = client.get(f"/api/stories/{story_id}/schema")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "fields" in data
        keys = [f["key"] for f in data["fields"]]
        assert "name" in keys


# ===================================================================
# Branches
# ===================================================================


class TestBranchesAPI:
    def test_get_branches(self, client, setup_story):
        resp = client.get("/api/branches")
        assert resp.status_code == 200
        data = resp.get_json()
        # branches is a dict keyed by branch_id
        assert "main" in data["branches"]

    def test_create_branch(self, client, setup_story, story_id):
        resp = client.post("/api/branches", json={
            "name": "測試分支",
            "branch_point_index": 1,
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "branch" in data

    def test_create_blank_branch(self, client, setup_story, story_id):
        resp = client.post("/api/branches/blank", json={"name": "空白分支"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        branch = data["branch"]
        assert branch.get("blank") is True
        assert branch["branch_point_index"] == -1

    def test_switch_branch(self, client, setup_story):
        # Create a branch first
        resp = client.post("/api/branches", json={"name": "切換用", "branch_point_index": 1})
        branch = resp.get_json()["branch"]
        bid = branch["id"]
        # Switch to it
        resp2 = client.post("/api/branches/switch", json={"branch_id": bid})
        assert resp2.status_code == 200
        assert resp2.get_json()["ok"] is True

    def test_rename_branch(self, client, setup_story):
        resp = client.patch("/api/branches/main", json={"name": "新主線名"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_delete_main_blocked(self, client, setup_story):
        resp = client.delete("/api/branches/main")
        # Deleting main should fail
        assert resp.status_code == 400 or resp.get_json().get("error")

    def test_delete_non_main(self, client, setup_story):
        # Create and then delete
        resp = client.post("/api/branches", json={"name": "刪除用", "branch_point_index": 1})
        bid = resp.get_json()["branch"]["id"]
        # Switch back to main first
        client.post("/api/branches/switch", json={"branch_id": "main"})
        resp2 = client.delete(f"/api/branches/{bid}")
        assert resp2.status_code == 200
        assert resp2.get_json()["ok"] is True

    def test_edit_no_change_rejected(self, client, setup_story):
        """Editing a message with identical content should return 400."""
        # The parsed conversation has: index 0 = user "你好", index 2 = user "開始任務"
        # Edit at branch_point_index=1 means the user msg at index 2 is being edited
        resp = client.post("/api/branches/edit", json={
            "parent_branch_id": "main",
            "branch_point_index": 1,
            "edited_message": "開始任務",  # same as original
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["error"] == "no_change"

    def test_edit_changed_message_allowed(self, client, setup_story, monkeypatch):
        """Editing a message with different content should proceed (mock LLM)."""
        import app as app_module
        # Mock the LLM call to avoid actual API calls
        monkeypatch.setattr(app_module, "call_claude_gm", lambda *a, **kw: ("GM回覆", None))
        monkeypatch.setattr(app_module, "_extract_tags_async", lambda *a, **kw: None)
        resp = client.post("/api/branches/edit", json={
            "parent_branch_id": "main",
            "branch_point_index": 1,
            "edited_message": "改變了的訊息",  # different from original
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_edit_stream_no_change_rejected(self, client, setup_story):
        """Streaming edit with identical content should return error SSE event."""
        resp = client.post("/api/branches/edit/stream", json={
            "parent_branch_id": "main",
            "branch_point_index": 1,
            "edited_message": "開始任務",  # same as original
        })
        # The response is SSE; check that it contains the error
        data = resp.get_data(as_text=True)
        assert "no_change" in data

    def test_branch_config_roundtrip(self, client, setup_story):
        # GET default config
        resp = client.get("/api/branches/main/config")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        # POST config
        resp2 = client.post("/api/branches/main/config", json={"cheat_dice": True})
        assert resp2.status_code == 200
        config = resp2.get_json()["config"]
        assert config["cheat_dice"] is True

        # GET again to verify
        resp3 = client.get("/api/branches/main/config")
        assert resp3.get_json()["config"]["cheat_dice"] is True


# ===================================================================
# Messages & Status
# ===================================================================


class TestMessagesAPI:
    def test_get_messages(self, client, setup_story):
        resp = client.get("/api/messages")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "messages" in data
        assert len(data["messages"]) == 4

    def test_get_messages_with_after_index(self, client, setup_story):
        resp = client.get("/api/messages?after_index=2")
        assert resp.status_code == 200
        data = resp.get_json()
        messages = data["messages"]
        assert all(m["index"] > 2 for m in messages)

    def test_get_status(self, client, setup_story):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "測試者"
        assert data["current_phase"] == "主神空間"
        assert data["reward_points"] == 5000


# ===================================================================
# Game Saves
# ===================================================================


class TestSavesAPI:
    def test_load_save_status_preview_keeps_timeline(self, client, setup_story, story_id):
        # Create save at current branch head (snapshot reward_points=5000)
        save_resp = client.post("/api/saves", json={"name": "B存檔"})
        assert save_resp.status_code == 200
        save = save_resp.get_json()["save"]

        # Simulate later progress state on same branch (reward_points=123456)
        state_path = app_module._story_character_state_path(story_id, "main")
        with open(state_path, "r", encoding="utf-8") as f:
            live_state = json.load(f)
        live_state["reward_points"] = 123456
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(live_state, f, ensure_ascii=False)

        # Load save should switch branch but keep full timeline visible
        load_resp = client.post(f"/api/saves/{save['id']}/load")
        assert load_resp.status_code == 200
        assert load_resp.get_json()["ok"] is True

        messages_resp = client.get("/api/messages?branch_id=main")
        assert messages_resp.status_code == 200
        assert len(messages_resp.get_json()["messages"]) == 4

        # Status should show saved snapshot (5000), not current live state (123456)
        status_resp = client.get("/api/status?branch_id=main")
        assert status_resp.status_code == 200
        status = status_resp.get_json()
        assert status["reward_points"] == 5000
        assert status["loaded_save_id"] == save["id"]

    def test_send_after_load_save_uses_live_state(self, client, setup_story, story_id, monkeypatch):
        # Save snapshot first (reward_points=5000)
        save_resp = client.post("/api/saves", json={"name": "B存檔"})
        save = save_resp.get_json()["save"]

        # Move branch live state forward (reward_points=98765)
        state_path = app_module._story_character_state_path(story_id, "main")
        with open(state_path, "r", encoding="utf-8") as f:
            live_state = json.load(f)
        live_state["reward_points"] = 98765
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(live_state, f, ensure_ascii=False)

        # Load save -> status preview uses snapshot
        client.post(f"/api/saves/{save['id']}/load")
        preview_status = client.get("/api/status?branch_id=main").get_json()
        assert preview_status["reward_points"] == 5000

        captured = {}

        def fake_call_claude_gm(_user_text, system_prompt, _recent, session_id=None):
            captured["system_prompt"] = system_prompt
            assert session_id is None
            return ("GM回覆", None)

        monkeypatch.setattr(app_module, "call_claude_gm", fake_call_claude_gm)
        monkeypatch.setattr(
            app_module,
            "_process_gm_response",
            lambda gm_response, _story_id, _branch_id, _idx: (gm_response, None, {}),
        )

        send_resp = client.post("/api/send", json={"message": "繼續前進", "branch_id": "main"})
        assert send_resp.status_code == 200
        assert send_resp.get_json()["ok"] is True

        # Send should still use branch live state (98765), not save snapshot (5000)
        assert "98765" in captured["system_prompt"]

        # Preview should be cleared after sending; status returns live state again
        status_after_send = client.get("/api/status?branch_id=main").get_json()
        assert status_after_send["reward_points"] == 98765
        assert "loaded_save_id" not in status_after_send


# ===================================================================
# Events
# ===================================================================


class TestEventsAPI:
    def test_get_events_empty(self, client, setup_story, story_id):
        resp = client.get("/api/events")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert isinstance(data["events"], list)

    def test_get_events_after_insert(self, client, setup_story, story_id):
        event_db.insert_event(story_id, {
            "event_type": "伏筆", "title": "測試事件", "description": "描述"
        }, "main")
        resp = client.get("/api/events")
        assert resp.status_code == 200
        events = resp.get_json()["events"]
        assert len(events) >= 1
        titles = [e["title"] for e in events]
        assert "測試事件" in titles

    def test_update_event_status(self, client, setup_story, story_id):
        event_db.insert_event(story_id, {
            "event_type": "伏筆", "title": "狀態測試", "description": "d"
        }, "main")
        events = event_db.get_events(story_id, branch_id="main")
        eid = events[0]["id"]
        resp = client.patch(f"/api/events/{eid}", json={"status": "triggered"})
        assert resp.status_code == 200

    def test_search_events(self, client, setup_story, story_id):
        event_db.insert_event(story_id, {
            "event_type": "發現", "title": "搜尋目標", "description": "重要事件"
        }, "main")
        resp = client.get("/api/events/search?q=搜尋目標")
        assert resp.status_code == 200


# ===================================================================
# Lore
# ===================================================================


class TestLoreAPI:
    def test_get_lore_all_empty(self, client, setup_story):
        resp = client.get("/api/lore/all")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_create_lore_entry(self, client, setup_story):
        resp = client.post("/api/lore/entry", json={
            "category": "體系", "topic": "新體系", "content": "測試內容"
        })
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_update_lore_entry(self, client, setup_story):
        client.post("/api/lore/entry", json={
            "category": "體系", "topic": "更新測試", "content": "原始"
        })
        resp = client.put("/api/lore/entry", json={
            "topic": "更新測試", "content": "已更新"
        })
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_delete_lore_entry(self, client, setup_story):
        client.post("/api/lore/entry", json={
            "category": "體系", "topic": "刪除測試", "content": "將被刪除"
        })
        resp = client.delete("/api/lore/entry", json={"topic": "刪除測試"})
        assert resp.status_code == 200

    def test_search_lore(self, client, setup_story):
        client.post("/api/lore/entry", json={
            "category": "體系", "topic": "基因鎖", "content": "基因鎖是一種限制"
        })
        resp = client.get("/api/lore/search?q=基因鎖")
        assert resp.status_code == 200


# ===================================================================
# NPCs
# ===================================================================


class TestNPCsAPI:
    def test_get_npcs_empty(self, client, setup_story):
        resp = client.get("/api/npcs")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert isinstance(data["npcs"], list)

    def test_create_npc(self, client, setup_story):
        resp = client.post("/api/npcs", json={
            "name": "阿豪",
            "role": "隊友",
            "appearance": "高大壯碩",
        })
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        # Verify
        resp2 = client.get("/api/npcs")
        names = [n["name"] for n in resp2.get_json()["npcs"]]
        assert "阿豪" in names

    def test_delete_npc(self, client, setup_story):
        client.post("/api/npcs", json={"name": "臨時NPC", "role": "路人"})
        npcs = client.get("/api/npcs").get_json()["npcs"]
        assert len(npcs) == 1
        npc_id = npcs[0]["id"]
        resp = client.delete(f"/api/npcs/{npc_id}")
        assert resp.status_code == 200
        # Verify deleted
        npcs2 = client.get("/api/npcs").get_json()["npcs"]
        assert len(npcs2) == 0


# ===================================================================
# Config
# ===================================================================


class TestConfigAPI:
    def test_get_config(self, client, setup_story):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["provider"] == "gemini"
        assert "version" in data
        # API keys should NOT be exposed
        raw = json.dumps(data)
        assert "test_key_123" not in raw


# ===================================================================
# Cheats
# ===================================================================


class TestCheatsAPI:
    def test_dice_cheat_get(self, client, setup_story):
        resp = client.get("/api/cheats/dice")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "always_success" in data

    def test_dice_cheat_set(self, client, setup_story):
        resp = client.post("/api/cheats/dice", json={"always_success": True})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        # Verify
        resp2 = client.get("/api/cheats/dice")
        assert resp2.get_json()["always_success"] is True

    def test_pistol_cheat_get(self, client, setup_story):
        resp = client.get("/api/cheats/pistol")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "pistol_mode" in data

    def test_pistol_cheat_set(self, client, setup_story):
        resp = client.post("/api/cheats/pistol", json={"pistol_mode": True})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        resp2 = client.get("/api/cheats/pistol")
        assert resp2.get_json()["pistol_mode"] is True
