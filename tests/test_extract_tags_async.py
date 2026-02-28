"""Tests for _extract_tags_async parse logic in app.py (Phase 2.6).

Tests the JSON parsing, dedup, and state update logic of the async
tag extraction — NOT the LLM call itself (which is mocked).
"""

import json
import threading
import time
from unittest import mock

import pytest

import app as app_module
import event_db
import lore_db
import state_db
import world_timer

REAL_THREAD = threading.Thread


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
    monkeypatch.setattr(state_db, "STORIES_DIR", str(stories_dir))
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


@pytest.fixture(autouse=True)
def clear_pending_extract():
    with app_module._PENDING_EXTRACT_LOCK:
        app_module._PENDING_EXTRACT.clear()
    yield
    with app_module._PENDING_EXTRACT_LOCK:
        app_module._PENDING_EXTRACT.clear()


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
    def test_state_ops_preferred_over_legacy_state(self, mock_llm, story_id, setup_story):
        llm_response = json.dumps({
            "state_ops": {
                "set": {"current_status": "依 state_ops 套用"},
                "delta": {"reward_points": 120},
            },
            "state": {"current_status": "legacy 不應覆蓋"},
        })
        mock_llm.return_value = llm_response

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=2)

        state_path = setup_story / "branches" / "main" / "character_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["current_status"] == "依 state_ops 套用"
        assert state["reward_points"] == 5120

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

    @mock.patch("llm_bridge.call_oneshot")
    def test_snapshot_synced_after_async_updates(self, mock_llm, story_id, setup_story):
        """Async extraction should refresh the GM message snapshot to canonical state."""
        msg_path = setup_story / "branches" / "main" / "messages.json"
        stale_snapshot = {
            "name": "測試者",
            "current_phase": "主神空間",
            "reward_points": 5000,
            "inventory": [],
            "current_status": "舊狀態",
        }
        msg_path.write_text(json.dumps([{
            "index": 1,
            "role": "gm",
            "content": "舊訊息",
            "state_snapshot": stale_snapshot,
            "npcs_snapshot": [],
            "world_day_snapshot": 0,
            "dungeon_progress_snapshot": {
                "history": [],
                "current_dungeon": None,
                "total_dungeons_completed": 0,
            },
        }], ensure_ascii=False), encoding="utf-8")

        mock_llm.return_value = json.dumps({
            "state": {"current_status": "已同步新狀態"},
        })

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=1)

        state_path = setup_story / "branches" / "main" / "character_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        msgs = json.loads(msg_path.read_text(encoding="utf-8"))
        synced = msgs[0]
        assert synced["state_snapshot"] == state
        assert synced["state_snapshot"]["current_status"] == "已同步新狀態"
        assert "snapshot_async_synced_at" in synced
        assert (story_id, "main", 1) not in app_module._PENDING_EXTRACT

    @mock.patch("llm_bridge.call_oneshot")
    def test_pending_extract_cleared_when_gm_message_missing(self, mock_llm, story_id, setup_story):
        """Pending extraction marker should always clear even if snapshot target is missing."""
        mock_llm.return_value = json.dumps({"state": {"current_status": "略"}})

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=999)

        assert (story_id, "main", 999) not in app_module._PENDING_EXTRACT

    def test_wait_extract_done_waits_prior_indices(self, story_id):
        """Wait should include pending extraction jobs at or before target index."""
        app_module._mark_extract_pending(story_id, "main", 2)
        t0 = time.time()
        app_module._wait_extract_done(story_id, "main", 3, timeout_s=0.06)
        elapsed = time.time() - t0

        app_module._mark_extract_done(story_id, "main", 2)
        assert elapsed >= 0.05

    def test_wait_extract_done_ignores_future_indices(self, story_id):
        """Wait should not block on pending extraction jobs newer than target index."""
        app_module._mark_extract_pending(story_id, "main", 5)
        t0 = time.time()
        app_module._wait_extract_done(story_id, "main", 3, timeout_s=0.2)
        elapsed = time.time() - t0
        app_module._mark_extract_done(story_id, "main", 5)

        assert elapsed < 0.05

    def test_snapshot_sync_uses_branch_messages_lock(self, story_id, setup_story):
        """Snapshot sync should respect per-branch messages lock to avoid RMW races."""
        branch_dir = setup_story / "branches" / "main"
        msg_path = branch_dir / "messages.json"
        msg_path.write_text(json.dumps([
            {
                "index": 1,
                "role": "gm",
                "content": "舊訊息",
                "state_snapshot": {"current_status": "舊狀態"},
                "npcs_snapshot": [],
                "world_day_snapshot": 0,
                "dungeon_progress_snapshot": {
                    "history": [],
                    "current_dungeon": None,
                    "total_dungeons_completed": 0,
                },
            },
            {"index": 2, "role": "user", "content": "後續輸入"},
        ], ensure_ascii=False), encoding="utf-8")

        state_path = branch_dir / "character_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["current_status"] = "新狀態"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        lock = app_module._get_branch_messages_lock(story_id, "main")
        finished = threading.Event()

        def _run_sync():
            app_module._sync_gm_message_snapshot_after_async(story_id, "main", 1)
            finished.set()

        with lock:
            t = REAL_THREAD(target=_run_sync, daemon=True)
            t.start()
            time.sleep(0.05)
            assert not finished.is_set()

        t.join(timeout=1.0)
        assert finished.is_set()

        msgs = json.loads(msg_path.read_text(encoding="utf-8"))
        assert any(m.get("index") == 2 and m.get("role") == "user" for m in msgs)
        gm = next(m for m in msgs if m.get("index") == 1 and m.get("role") == "gm")
        assert gm["state_snapshot"]["current_status"] == "新狀態"


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


class TestAsyncEventOps:
    @mock.patch("llm_bridge.call_oneshot")
    def test_event_ops_update_by_id(self, mock_llm, story_id, setup_story):
        event_id = event_db.insert_event(story_id, {
            "event_type": "伏筆", "title": "舊標題", "description": "d", "status": "planted"
        }, "main")
        mock_llm.return_value = json.dumps({
            "event_ops": {
                "update": [{"id": event_id, "status": "completed"}],
            },
        })

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=8)

        updated = event_db.get_event_by_id(story_id, event_id)
        assert updated["status"] == "resolved"

    @mock.patch("llm_bridge.call_oneshot")
    def test_events_ops_plural_backward_compat(self, mock_llm, story_id, setup_story):
        event_id = event_db.insert_event(story_id, {
            "event_type": "伏筆", "title": "兼容事件", "description": "d", "status": "planted"
        }, "main")
        mock_llm.return_value = json.dumps({
            "events_ops": {
                "update": [{"id": event_id, "status": "ongoing"}],
            },
        })

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=9)

        updated = event_db.get_event_by_id(story_id, event_id)
        assert updated["status"] == "triggered"

    @mock.patch("llm_bridge.call_oneshot")
    def test_event_ops_create_dedup_only_advances_status(self, mock_llm, story_id, setup_story):
        event_db.insert_event(story_id, {
            "event_type": "伏筆", "title": "重複事件", "description": "old", "status": "planted"
        }, "main")
        mock_llm.return_value = json.dumps({
            "event_ops": {
                "create": [
                    {"event_type": "伏筆", "title": "重複事件", "description": "new", "status": "triggered", "tags": ""},
                ],
            },
        })

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=10)

        events = event_db.get_events(story_id, branch_id="main", limit=20)
        matching = [e for e in events if e["title"] == "重複事件"]
        assert len(matching) == 1
        assert matching[0]["status"] == "triggered"


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


# ===================================================================
# GM plan extraction
# ===================================================================


class TestAsyncGmPlanExtraction:
    @mock.patch("llm_bridge.call_oneshot")
    def test_prompt_includes_previous_plan_context(self, mock_llm, story_id, setup_story):
        plan_path = setup_story / "branches" / "main" / "gm_plan.json"
        plan_path.write_text(json.dumps({
            "arc": "舊弧線",
            "next_beats": ["舊節點"],
            "must_payoff": [
                {"event_title": "舊伏筆", "event_id": 1, "ttl_turns": 3, "created_at_index": 5},
            ],
            "updated_at_index": 8,
        }, ensure_ascii=False), encoding="utf-8")

        mock_llm.return_value = json.dumps({"plan": {"arc": "新弧線", "next_beats": [], "must_payoff": []}})

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=10)

        prompt = mock_llm.call_args[0][0]
        assert "上一輪 GM 計劃（供參考，可全部改寫）" in prompt
        assert "舊弧線" in prompt
        assert "舊節點" in prompt

    @mock.patch("llm_bridge.call_oneshot")
    def test_plan_saved_with_event_relink(self, mock_llm, story_id, setup_story):
        event_id = event_db.insert_event(story_id, {
            "event_type": "伏筆", "title": "神秘符文", "description": "d", "status": "planted"
        }, "main")
        mock_llm.return_value = json.dumps({
            "plan": {
                "arc": "符文回收線",
                "next_beats": ["逼近真相"],
                "must_payoff": [
                    {"event_title": "神秘符文", "ttl_turns": 3},
                ],
            },
        })

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=12)

        plan_path = setup_story / "branches" / "main" / "gm_plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert plan["arc"] == "符文回收線"
        assert plan["updated_at_index"] == 12
        assert len(plan["must_payoff"]) == 1
        assert plan["must_payoff"][0]["event_id"] == event_id
        assert plan["must_payoff"][0]["created_at_index"] == 12

    @mock.patch("llm_bridge.call_oneshot")
    def test_same_payoff_keeps_original_created_at_index(self, mock_llm, story_id, setup_story):
        event_id = event_db.insert_event(story_id, {
            "event_type": "伏筆", "title": "神秘符文", "description": "d", "status": "planted"
        }, "main")
        plan_path = setup_story / "branches" / "main" / "gm_plan.json"
        plan_path.write_text(json.dumps({
            "arc": "舊弧線",
            "next_beats": ["舊節點"],
            "must_payoff": [
                {"event_title": "神秘符文", "event_id": event_id, "ttl_turns": 3, "created_at_index": 5},
            ],
            "updated_at_index": 9,
        }, ensure_ascii=False), encoding="utf-8")

        mock_llm.return_value = json.dumps({
            "plan": {
                "arc": "新弧線",
                "next_beats": ["新節點"],
                "must_payoff": [
                    {"event_title": "神秘符文", "event_id": event_id, "ttl_turns": 4, "created_at_index": 999},
                ],
            },
        })

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=13)

        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert plan["updated_at_index"] == 13
        assert plan["must_payoff"][0]["created_at_index"] == 5

    @mock.patch("llm_bridge.call_oneshot")
    def test_pistol_mode_skips_plan_write(self, mock_llm, story_id, setup_story, monkeypatch):
        plan_path = setup_story / "branches" / "main" / "gm_plan.json"
        original = {
            "arc": "保留弧線",
            "next_beats": ["保留節點"],
            "must_payoff": [],
            "updated_at_index": 7,
        }
        plan_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

        monkeypatch.setattr(app_module, "get_pistol_mode", lambda *_a, **_kw: True)
        mock_llm.return_value = json.dumps({
            "plan": {
                "arc": "不應寫入",
                "next_beats": ["不應寫入"],
                "must_payoff": [],
            },
        })

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=15)

        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert plan == original


# ===================================================================
# NPC tier extraction/normalization
# ===================================================================


class TestNpcTier:
    @mock.patch("llm_bridge.call_oneshot")
    def test_extract_prompt_includes_tier_schema(self, mock_llm, story_id, setup_story):
        """Extraction prompt should ask for tier field with allowlist format."""
        mock_llm.return_value = json.dumps({"npcs": []})

        app_module._extract_tags_async(story_id, "main", "GM回覆文字測試" * 50, msg_index=1)

        prompt = mock_llm.call_args[0][0]
        assert '"tier": "D-~S+ 或 null"' in prompt
        assert "D-/D/D+/C-/C/C+/B-/B/B+/A-/A/A+/S-/S/S+" in prompt
        assert "請省略 tier 欄位（不要輸出 null 覆蓋）" in prompt

    def test_save_npc_normalizes_tier_value(self, story_id, setup_story):
        """_save_npc should normalize variants like 's-級' to allowlist value."""
        app_module._save_npc(story_id, {"name": "審判暴君", "role": "敵人", "tier": " s-級 "}, "main")

        npcs_path = setup_story / "branches" / "main" / "npcs.json"
        npcs = json.loads(npcs_path.read_text(encoding="utf-8"))
        assert npcs[0]["tier"] == "S-"

    def test_invalid_tier_does_not_overwrite_existing(self, story_id, setup_story):
        """Invalid tier should be ignored and preserve previous valid tier."""
        app_module._save_npc(story_id, {"name": "阿豪", "role": "隊友", "tier": "B+"}, "main")
        app_module._save_npc(story_id, {"name": "阿豪", "role": "隊友", "tier": "SSS"}, "main")

        npcs_path = setup_story / "branches" / "main" / "npcs.json"
        npcs = json.loads(npcs_path.read_text(encoding="utf-8"))
        assert npcs[0]["tier"] == "B+"

    def test_save_npc_archives_on_terminal_current_status(self, story_id, setup_story):
        app_module._save_npc(
            story_id,
            {"name": "安德斯", "role": "敵人", "current_status": "已損毀，威脅解除"},
            "main",
        )

        npcs_path = setup_story / "branches" / "main" / "npcs.json"
        npcs = json.loads(npcs_path.read_text(encoding="utf-8"))
        assert npcs[0]["lifecycle_status"] == "archived"
        assert str(npcs[0].get("archived_reason", "")).startswith("current_status:")

    def test_save_npc_unarchives_on_repair_keyword(self, story_id, setup_story):
        app_module._save_npc(
            story_id,
            {"name": "安德斯", "role": "敵人", "current_status": "已損毀，威脅解除"},
            "main",
        )
        app_module._save_npc(
            story_id,
            {"name": "安德斯", "current_status": "修復完成"},
            "main",
        )

        npcs_path = setup_story / "branches" / "main" / "npcs.json"
        npcs = json.loads(npcs_path.read_text(encoding="utf-8"))
        assert len(npcs) == 1
        assert npcs[0]["lifecycle_status"] == "active"
        assert npcs[0]["archived_reason"] is None

    def test_save_npc_r1_name_merge(self, story_id, setup_story):
        app_module._save_npc(story_id, {"name": "小琳", "role": "高中少女"}, "main")
        app_module._save_npc(story_id, {"name": "小 琳", "relationship_to_player": "信任"}, "main")

        npcs_path = setup_story / "branches" / "main" / "npcs.json"
        npcs = json.loads(npcs_path.read_text(encoding="utf-8"))
        assert len(npcs) == 1
        assert npcs[0]["name"] == "小琳"
        assert npcs[0]["relationship_to_player"] == "信任"
