"""Tests for _extract_tags_async parse logic in app.py (Phase 2.6).

Tests the JSON parsing, dedup, and state update logic of the async
tag extraction — NOT the LLM call itself (which is mocked).
"""

import json
import threading
from unittest import mock

import pytest

import app as app_module
import event_db
import lore_db
import world_timer


class _SyncThread(threading.Thread):
    """Subclass that runs target synchronously instead of in a new thread."""

    def start(self):
        self.run()


@pytest.fixture(autouse=True)
def patch_all_paths(tmp_path, monkeypatch):
    """Redirect all module paths to tmp_path."""
    stories_dir = tmp_path / "data" / "stories"
    stories_dir.mkdir(parents=True)
    design_dir = tmp_path / "story_design"
    design_dir.mkdir()
    monkeypatch.setattr(app_module, "STORIES_DIR", str(stories_dir))
    monkeypatch.setattr(app_module, "STORY_DESIGN_DIR", str(design_dir))
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(event_db, "STORIES_DIR", str(stories_dir))
    monkeypatch.setattr(lore_db, "STORIES_DIR", str(stories_dir))
    monkeypatch.setattr(lore_db, "STORY_DESIGN_DIR", str(design_dir))
    monkeypatch.setattr(world_timer, "BASE_DIR", str(tmp_path))
    lore_db._embedding_cache.clear()
    return stories_dir


@pytest.fixture(autouse=True)
def run_threads_synchronously(monkeypatch):
    """Make _extract_tags_async run synchronously instead of in a thread."""
    monkeypatch.setattr(app_module.threading, "Thread", _SyncThread)


@pytest.fixture(autouse=True)
def mock_log_usage(monkeypatch):
    """Disable LLM usage logging (no real provider in tests)."""
    monkeypatch.setattr(app_module, "_log_llm_usage", lambda *a, **kw: None)


@pytest.fixture
def story_id():
    return "test_story"


@pytest.fixture
def setup_story(tmp_path, story_id):
    """Set up minimal story for async extraction tests."""
    story_dir = tmp_path / "data" / "stories" / story_id
    branch_dir = story_dir / "branches" / "main"
    branch_dir.mkdir(parents=True, exist_ok=True)

    # Design files directory
    design_dir = tmp_path / "story_design" / story_id
    design_dir.mkdir(parents=True, exist_ok=True)

    # Character state (runtime)
    state = {"name": "測試者", "current_phase": "主神空間", "reward_points": 5000, "inventory": []}
    (branch_dir / "character_state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    # Character schema → story_design/
    schema = {
        "fields": [
            {"key": "name"}, {"key": "current_phase"},
            {"key": "reward_points", "type": "number"}, {"key": "current_status"},
        ],
        "lists": [
            {"key": "inventory", "state_add_key": "inventory_add", "state_remove_key": "inventory_remove"},
            {"key": "relationships", "type": "map"},
        ],
        "direct_overwrite_keys": ["current_phase", "current_status"],
    }
    (design_dir / "character_schema.json").write_text(json.dumps(schema), encoding="utf-8")

    # World lore → story_design/
    (design_dir / "world_lore.json").write_text("[]", encoding="utf-8")

    # Timeline tree (runtime)
    tree = {"active_branch_id": "main", "branches": {
        "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
    }}
    (story_dir / "timeline_tree.json").write_text(json.dumps(tree), encoding="utf-8")

    # NPCs (runtime)
    (branch_dir / "npcs.json").write_text("[]", encoding="utf-8")

    # Messages (runtime)
    (branch_dir / "messages.json").write_text("[]", encoding="utf-8")

    return story_dir


# ===================================================================
# JSON parsing from LLM response
# ===================================================================


class TestAsyncExtractionParsing:
    @mock.patch("llm_bridge.call_oneshot")
    def test_valid_json_parsed(self, mock_llm, story_id, setup_story):
        """Valid JSON response should be parsed and applied."""
        llm_response = json.dumps({
            "lore": [{"category": "體系", "topic": "新發現", "content": "測試內容"}],
            "events": [{"event_type": "發現", "title": "新事件", "description": "描述", "status": "planted", "tags": ""}],
            "npcs": [],
            "state": {"current_status": "探索中"},
            "time": {"hours": 2},
            "branch_title": "測試標題",
        })
        mock_llm.return_value = llm_response

        app_module._extract_tags_async(story_id, "main", "GM回覆文字" * 50, msg_index=1)

        # Verify state was updated
        state_path = setup_story / "branches" / "main" / "character_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["current_status"] == "探索中"

    @mock.patch("llm_bridge.call_oneshot")
    def test_markdown_fenced_json(self, mock_llm, story_id, setup_story):
        """JSON wrapped in markdown code fences should be stripped and parsed."""
        inner = json.dumps({
            "lore": [],
            "events": [],
            "npcs": [],
            "state": {"current_phase": "副本中"},
        })
        mock_llm.return_value = f"```json\n{inner}\n```"

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=1)

        state_path = setup_story / "branches" / "main" / "character_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["current_phase"] == "副本中"

    @mock.patch("llm_bridge.call_oneshot")
    def test_empty_llm_response_no_crash(self, mock_llm, story_id, setup_story):
        """Empty LLM response should not crash."""
        mock_llm.return_value = ""
        # Should not raise
        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=1)

    @mock.patch("llm_bridge.call_oneshot")
    def test_none_llm_response_no_crash(self, mock_llm, story_id, setup_story):
        """None LLM response should not crash."""
        mock_llm.return_value = None
        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=1)

    @mock.patch("llm_bridge.call_oneshot")
    def test_skip_state_flag(self, mock_llm, story_id, setup_story):
        """When skip_state=True, state changes should be ignored."""
        llm_response = json.dumps({
            "state": {"current_status": "不應該被套用"},
        })
        mock_llm.return_value = llm_response

        app_module._extract_tags_async(
            story_id, "main", "GM回覆文字測試" * 50, msg_index=1, skip_state=True
        )

        state_path = setup_story / "branches" / "main" / "character_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state.get("current_status") != "不應該被套用"

    @mock.patch("llm_bridge.call_oneshot")
    def test_short_text_skipped(self, mock_llm, story_id, setup_story):
        """GM text shorter than 200 chars should skip extraction entirely."""
        app_module._extract_tags_async(story_id, "main", "短文字", msg_index=1)
        mock_llm.assert_not_called()

    @mock.patch("llm_bridge.call_oneshot")
    def test_malformed_json_fallback(self, mock_llm, story_id, setup_story):
        """Malformed JSON should try regex fallback to extract first JSON object."""
        inner = json.dumps({"state": {"current_status": "回退解析"}})
        mock_llm.return_value = f"Some preamble text\n{inner}\nSome trailing text"

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=1)

        state_path = setup_story / "branches" / "main" / "character_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["current_status"] == "回退解析"


# ===================================================================
# Event dedup
# ===================================================================


class TestAsyncEventDedup:
    @mock.patch("llm_bridge.call_oneshot")
    def test_duplicate_event_not_inserted_twice(self, mock_llm, story_id, setup_story):
        """Events with same title and same status should not create duplicates."""
        # Insert existing event
        event_db.insert_event(story_id, {
            "event_type": "伏筆", "title": "已存在事件", "description": "d", "status": "planted"
        }, "main")

        llm_response = json.dumps({
            "events": [
                {"event_type": "伏筆", "title": "已存在事件", "description": "重複", "status": "planted", "tags": ""},
                {"event_type": "發現", "title": "新事件", "description": "新的", "status": "planted", "tags": ""},
            ],
        })
        mock_llm.return_value = llm_response

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=5)

        titles = event_db.get_event_titles(story_id, "main")
        assert "新事件" in titles
        # "已存在事件" should exist but not duplicated
        events = event_db.get_events(story_id, branch_id="main")
        count = sum(1 for e in events if e["title"] == "已存在事件")
        assert count == 1  # Only the original

    @mock.patch("llm_bridge.call_oneshot")
    def test_event_status_updated_on_advancement(self, mock_llm, story_id, setup_story):
        """Events with same title but advanced status should update, not skip (C1)."""
        # Insert existing event as "planted"
        event_db.insert_event(story_id, {
            "event_type": "伏筆", "title": "基因鎖突破", "description": "伏筆描述", "status": "planted"
        }, "main")

        # LLM says this event is now "triggered"
        llm_response = json.dumps({
            "events": [
                {"event_type": "伏筆", "title": "基因鎖突破", "description": "已觸發", "status": "triggered", "tags": ""},
            ],
        })
        mock_llm.return_value = llm_response

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=5)

        events = event_db.get_events(story_id, branch_id="main")
        matching = [e for e in events if e["title"] == "基因鎖突破"]
        assert len(matching) == 1  # Still only one event
        assert matching[0]["status"] == "triggered"  # Status updated

    @mock.patch("llm_bridge.call_oneshot")
    def test_event_status_no_backward_transition(self, mock_llm, story_id, setup_story):
        """Event status should not go backwards (resolved → planted)."""
        event_db.insert_event(story_id, {
            "event_type": "伏筆", "title": "已解決事件", "description": "d", "status": "resolved"
        }, "main")

        llm_response = json.dumps({
            "events": [
                {"event_type": "伏筆", "title": "已解決事件", "description": "d", "status": "planted", "tags": ""},
            ],
        })
        mock_llm.return_value = llm_response

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=5)

        events = event_db.get_events(story_id, branch_id="main")
        matching = [e for e in events if e["title"] == "已解決事件"]
        assert matching[0]["status"] == "resolved"  # Not reverted to planted


# ===================================================================
# Lore extraction
# ===================================================================


class TestAsyncLoreExtraction:
    @mock.patch("llm_bridge.call_oneshot")
    def test_new_lore_saved_to_branch(self, mock_llm, story_id, setup_story, tmp_path):
        """New auto-extracted lore should be saved to branch_lore.json (not base)."""
        llm_response = json.dumps({
            "lore": [{"category": "體系", "topic": "新體系", "content": "這是新的體系說明"}],
        })
        mock_llm.return_value = llm_response

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=1)

        # Should be in branch_lore.json, not world_lore.json
        branch_lore_path = setup_story / "branches" / "main" / "branch_lore.json"
        branch_lore = json.loads(branch_lore_path.read_text(encoding="utf-8"))
        topics = [e["topic"] for e in branch_lore]
        assert "新體系" in topics
        saved = next(e for e in branch_lore if e["topic"] == "新體系")
        assert saved.get("source", {}).get("branch_id") == "main"
        assert saved.get("source", {}).get("msg_index") == 1

        # Base lore should remain empty
        design_dir = tmp_path / "story_design" / story_id
        base_lore = json.loads((design_dir / "world_lore.json").read_text(encoding="utf-8"))
        assert len(base_lore) == 0

    @mock.patch("llm_bridge.call_oneshot")
    def test_user_edited_lore_protected(self, mock_llm, story_id, setup_story, tmp_path):
        """Lore entries marked as user-edited should not be overwritten."""
        # Pre-populate with user-edited entry
        design_dir = tmp_path / "story_design" / story_id
        lore = [{"category": "體系", "topic": "保護項", "content": "用戶編輯", "edited_by": "user"}]
        (design_dir / "world_lore.json").write_text(json.dumps(lore, ensure_ascii=False), encoding="utf-8")

        llm_response = json.dumps({
            "lore": [{"category": "體系", "topic": "保護項", "content": "LLM想覆蓋"}],
        })
        mock_llm.return_value = llm_response

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=1)

        lore_path = design_dir / "world_lore.json"
        lore = json.loads(lore_path.read_text(encoding="utf-8"))
        protected = [e for e in lore if e["topic"] == "保護項"]
        assert len(protected) == 1
        assert protected[0]["content"] == "用戶編輯"  # Not overwritten


# ===================================================================
# Time advancement
# ===================================================================


class TestAsyncTimeAdvancement:
    @mock.patch("llm_bridge.call_oneshot")
    def test_time_hours_advanced(self, mock_llm, story_id, setup_story):
        """Time hours should advance world day."""
        # Set up world_day.json (float format: total days)
        branch_dir = setup_story / "branches" / "main"
        (branch_dir / "world_day.json").write_text(
            json.dumps({"world_day": 1.0}), encoding="utf-8"
        )

        llm_response = json.dumps({
            "time": {"hours": 24},
        })
        mock_llm.return_value = llm_response

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=1)

        from world_timer import get_world_day
        wd = get_world_day(story_id, "main")
        # Started at 1.0, advanced by 24h = 1 day → 2.0
        assert wd == 2.0


# ===================================================================
# Branch title
# ===================================================================


class TestAsyncBranchTitle:
    @mock.patch("llm_bridge.call_oneshot")
    def test_branch_title_saved(self, mock_llm, story_id, setup_story):
        """branch_title should be saved to timeline_tree."""
        llm_response = json.dumps({
            "branch_title": "初次探索",
        })
        mock_llm.return_value = llm_response

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=1)

        tree_path = setup_story / "timeline_tree.json"
        tree = json.loads(tree_path.read_text(encoding="utf-8"))
        assert tree["branches"]["main"].get("title") == "初次探索"

    @mock.patch("llm_bridge.call_oneshot")
    def test_branch_title_set_once(self, mock_llm, story_id, setup_story):
        """branch_title should not overwrite existing title."""
        # Pre-set title
        tree_path = setup_story / "timeline_tree.json"
        tree = json.loads(tree_path.read_text(encoding="utf-8"))
        tree["branches"]["main"]["title"] = "原始標題"
        tree_path.write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")

        llm_response = json.dumps({
            "branch_title": "新標題",
        })
        mock_llm.return_value = llm_response

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=1)

        tree = json.loads(tree_path.read_text(encoding="utf-8"))
        assert tree["branches"]["main"]["title"] == "原始標題"  # Not changed
