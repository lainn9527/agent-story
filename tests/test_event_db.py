"""Tests for event_db.py (Phase 1.4).

Tests event CRUD, CJK bigram search, status filtering, and dedup helpers.
Uses monkeypatched STORIES_DIR for filesystem isolation.
"""

import json
import os

import pytest

import event_db


@pytest.fixture(autouse=True)
def patch_stories_dir(tmp_path, monkeypatch):
    """Redirect event_db STORIES_DIR to tmp_path."""
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir()
    monkeypatch.setattr(event_db, "STORIES_DIR", str(stories_dir))
    return stories_dir


@pytest.fixture
def story_id():
    return "test_story"


@pytest.fixture
def seed_events(story_id):
    """Insert sample events for search tests."""
    events = [
        {
            "event_type": "伏筆",
            "title": "神秘組織的暗示",
            "description": "在副本中發現了一封神秘信件，信中提到某個組織正在暗中操控主神空間的任務分配",
            "status": "planted",
            "tags": "懸念,組織",
            "message_index": 10,
        },
        {
            "event_type": "戰鬥",
            "title": "與伽椰子的決戰",
            "description": "在咒怨副本最終階段，與伽椰子進行了殊死搏鬥",
            "status": "resolved",
            "tags": "戰鬥,咒怨,Boss",
            "message_index": 20,
        },
        {
            "event_type": "發現",
            "title": "基因鎖突破",
            "description": "在極端壓力下成功開啟了基因鎖第一階段，身體素質大幅提升",
            "status": "triggered",
            "tags": "基因鎖,突破",
            "message_index": 25,
        },
        {
            "event_type": "獲得",
            "title": "獲得封印之鏡",
            "description": "從咒怨副本的寶箱中獲得了封印之鏡，可以封印低等級怨靈",
            "status": "resolved",
            "tags": "道具,封印",
            "message_index": 22,
        },
        {
            "event_type": "遭遇",
            "title": "遇見修真前輩",
            "description": "在主神空間遇到了一位修真體系的前輩，他提出可以傳授修真功法",
            "status": "planted",
            "tags": "修真,NPC",
            "message_index": 30,
        },
    ]
    ids = []
    for e in events:
        eid = event_db.insert_event(story_id, e, "main")
        ids.append(eid)
    return ids


# ===================================================================
# insert_event
# ===================================================================


class TestInsertEvent:
    def test_basic_insert(self, story_id):
        event = {
            "event_type": "伏筆",
            "title": "測試事件",
            "description": "描述",
            "status": "planted",
        }
        eid = event_db.insert_event(story_id, event, "main")
        assert eid > 0

    def test_default_status(self, story_id):
        event = {"event_type": "遭遇", "title": "默認狀態", "description": "d"}
        eid = event_db.insert_event(story_id, event, "main")
        fetched = event_db.get_event_by_id(story_id, eid)
        assert fetched["status"] == "planted"

    def test_default_event_type(self, story_id):
        event = {"title": "無類型", "description": "d"}
        eid = event_db.insert_event(story_id, event, "main")
        fetched = event_db.get_event_by_id(story_id, eid)
        assert fetched["event_type"] == "遭遇"

    def test_insert_with_tags(self, story_id):
        event = {
            "event_type": "發現",
            "title": "有標籤",
            "description": "d",
            "tags": "重要,劇情",
        }
        eid = event_db.insert_event(story_id, event, "main")
        fetched = event_db.get_event_by_id(story_id, eid)
        assert fetched["tags"] == "重要,劇情"


# ===================================================================
# get_events
# ===================================================================


class TestGetEvents:
    def test_get_all(self, story_id, seed_events):
        events = event_db.get_events(story_id)
        assert len(events) == 5

    def test_get_by_branch(self, story_id, seed_events):
        events = event_db.get_events(story_id, branch_id="main")
        assert len(events) == 5

    def test_get_empty_branch(self, story_id, seed_events):
        events = event_db.get_events(story_id, branch_id="nonexistent")
        assert len(events) == 0

    def test_get_with_limit(self, story_id, seed_events):
        events = event_db.get_events(story_id, limit=2)
        assert len(events) == 2


# ===================================================================
# update_event_status
# ===================================================================


class TestUpdateEventStatus:
    def test_planted_to_triggered(self, story_id, seed_events):
        event_db.update_event_status(story_id, seed_events[0], "triggered")
        fetched = event_db.get_event_by_id(story_id, seed_events[0])
        assert fetched["status"] == "triggered"

    def test_triggered_to_resolved(self, story_id, seed_events):
        event_db.update_event_status(story_id, seed_events[2], "resolved")
        fetched = event_db.get_event_by_id(story_id, seed_events[2])
        assert fetched["status"] == "resolved"


# ===================================================================
# search_events — CJK bigram scoring
# ===================================================================


class TestSearchEvents:
    def test_search_by_title_keyword(self, story_id, seed_events):
        results = event_db.search_events(story_id, "神秘組織")
        assert len(results) > 0
        assert results[0]["title"] == "神秘組織的暗示"

    def test_search_by_description(self, story_id, seed_events):
        results = event_db.search_events(story_id, "伽椰子")
        assert len(results) > 0
        titles = [r["title"] for r in results]
        assert "與伽椰子的決戰" in titles

    def test_search_by_tags(self, story_id, seed_events):
        results = event_db.search_events(story_id, "基因鎖")
        assert len(results) > 0
        titles = [r["title"] for r in results]
        assert "基因鎖突破" in titles

    def test_title_match_scores_higher(self, story_id, seed_events):
        # "基因鎖" appears in title of event 3 and in tags — title match (+10) should dominate
        results = event_db.search_events(story_id, "基因鎖")
        assert results[0]["title"] == "基因鎖突破"

    def test_active_only_filter(self, story_id, seed_events):
        results = event_db.search_events(story_id, "基因鎖", active_only=True)
        statuses = {r["status"] for r in results}
        assert "resolved" not in statuses
        assert "abandoned" not in statuses

    def test_active_only_excludes_resolved(self, story_id, seed_events):
        # "伽椰子" event is resolved — should be excluded with active_only
        results = event_db.search_events(story_id, "伽椰子", active_only=True)
        titles = [r["title"] for r in results]
        assert "與伽椰子的決戰" not in titles

    def test_search_no_results(self, story_id, seed_events):
        results = event_db.search_events(story_id, "完全不存在的詞")
        assert results == []

    def test_search_limit(self, story_id, seed_events):
        results = event_db.search_events(story_id, "的", limit=2)
        assert len(results) <= 2

    def test_branch_filter(self, story_id):
        event_db.insert_event(story_id, {"event_type": "發現", "title": "分支事件", "description": "d"}, "branch_a")
        event_db.insert_event(story_id, {"event_type": "發現", "title": "主線事件", "description": "d"}, "main")
        results = event_db.search_events(story_id, "事件", branch_id="branch_a")
        assert all(r["branch_id"] == "branch_a" for r in results)

    def test_english_query_fallback(self, story_id):
        event_db.insert_event(story_id, {"event_type": "發現", "title": "DNA", "description": "gene lock"}, "main")
        results = event_db.search_events(story_id, "DNA")
        assert len(results) > 0


# ===================================================================
# search_relevant_events — formatted injection
# ===================================================================


class TestSearchRelevantEvents:
    def test_formatted_output(self, story_id, seed_events):
        text = event_db.search_relevant_events(story_id, "神秘組織", "main")
        assert "[相關事件追蹤]" in text
        assert "神秘組織的暗示" in text
        assert "已埋" in text  # status=planted

    def test_empty_result(self, story_id):
        text = event_db.search_relevant_events(story_id, "不存在", "main")
        assert text == ""

    def test_description_truncated_to_200(self, story_id):
        long_desc = "A" * 500
        event_db.insert_event(
            story_id,
            {"event_type": "伏筆", "title": "長描述", "description": long_desc},
            "main",
        )
        text = event_db.search_relevant_events(story_id, "長描述", "main")
        # Description should be truncated at 200 chars
        assert len(text) < 500


# ===================================================================
# get_event_titles — dedup helper
# ===================================================================


class TestGetEventTitles:
    def test_returns_set_of_titles(self, story_id, seed_events):
        titles = event_db.get_event_titles(story_id, "main")
        assert isinstance(titles, set)
        assert "神秘組織的暗示" in titles
        assert "基因鎖突破" in titles
        assert len(titles) == 5

    def test_empty_branch(self, story_id):
        titles = event_db.get_event_titles(story_id, "empty_branch")
        assert titles == set()


# ===================================================================
# get_active_foreshadowing
# ===================================================================


class TestGetActiveForeshadowing:
    def test_returns_only_planted(self, story_id, seed_events):
        events = event_db.get_active_foreshadowing(story_id, "main")
        assert all(e["status"] == "planted" for e in events)

    def test_count(self, story_id, seed_events):
        # 2 planted events: "神秘組織的暗示" and "遇見修真前輩"
        events = event_db.get_active_foreshadowing(story_id, "main")
        assert len(events) == 2


# ===================================================================
# branch helpers
# ===================================================================


class TestCopyEventsForFork:
    def test_filters_by_branch_point_and_keeps_legacy_null_index(self, story_id):
        event_db.insert_event(
            story_id,
            {"event_type": "伏筆", "title": "早期事件", "description": "d", "message_index": 1},
            "main",
        )
        event_db.insert_event(
            story_id,
            {"event_type": "伏筆", "title": "未來事件", "description": "d", "message_index": 5},
            "main",
        )
        event_db.insert_event(
            story_id,
            {"event_type": "伏筆", "title": "舊版事件", "description": "d", "message_index": None},
            "main",
        )
        event_db.copy_events_for_fork(story_id, "main", "branch_a", 2)
        copied = event_db.get_events(story_id, branch_id="branch_a", limit=20)
        titles = {e["title"] for e in copied}
        assert titles == {"早期事件", "舊版事件"}

    def test_branch_point_none_copies_all(self, story_id):
        event_db.insert_event(
            story_id,
            {"event_type": "伏筆", "title": "事件一", "description": "d", "message_index": 1},
            "main",
        )
        event_db.insert_event(
            story_id,
            {"event_type": "遭遇", "title": "事件二", "description": "d", "message_index": 3},
            "main",
        )
        event_db.copy_events_for_fork(story_id, "main", "branch_all", None)
        copied = event_db.get_events(story_id, branch_id="branch_all", limit=20)
        titles = {e["title"] for e in copied}
        assert titles == {"事件一", "事件二"}


class TestMergeEventsInto:
    def test_inserts_new_titles_into_destination(self, story_id):
        event_db.insert_event(
            story_id,
            {"event_type": "發現", "title": "子分支新事件", "description": "d", "status": "triggered"},
            "child",
        )
        event_db.merge_events_into(story_id, "child", "main")
        merged = event_db.get_events(story_id, branch_id="main", limit=20)
        titles = {e["title"] for e in merged}
        assert "子分支新事件" in titles

    def test_overwrites_destination_status_by_title(self, story_id):
        event_db.insert_event(
            story_id,
            {"event_type": "伏筆", "title": "同標題事件", "description": "d", "status": "planted"},
            "main",
        )
        event_db.insert_event(
            story_id,
            {"event_type": "伏筆", "title": "同標題事件", "description": "d", "status": "resolved"},
            "child",
        )
        event_db.merge_events_into(story_id, "child", "main")
        merged = event_db.get_events(story_id, branch_id="main", limit=20)
        match = [e for e in merged if e["title"] == "同標題事件"]
        assert len(match) == 1
        assert match[0]["status"] == "resolved"


class TestDeleteEventsForBranch:
    def test_delete_only_target_branch(self, story_id):
        event_db.insert_event(
            story_id,
            {"event_type": "伏筆", "title": "主線事件", "description": "d"},
            "main",
        )
        event_db.insert_event(
            story_id,
            {"event_type": "伏筆", "title": "分支事件", "description": "d"},
            "branch_x",
        )
        event_db.delete_events_for_branch(story_id, "branch_x")
        assert event_db.get_events(story_id, branch_id="branch_x") == []
        main_events = event_db.get_events(story_id, branch_id="main")
        assert len(main_events) == 1
        assert main_events[0]["title"] == "主線事件"
