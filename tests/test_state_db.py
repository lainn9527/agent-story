"""Tests for state_db.py."""

import json

import pytest

import state_db


@pytest.fixture(autouse=True)
def patch_paths(tmp_path, monkeypatch):
    stories_dir = tmp_path / "data" / "stories"
    stories_dir.mkdir(parents=True)
    monkeypatch.setattr(state_db, "STORIES_DIR", str(stories_dir))
    return stories_dir


@pytest.fixture
def story_id():
    return "test_story"


@pytest.fixture
def setup_branch(tmp_path, story_id):
    branch_dir = tmp_path / "data" / "stories" / story_id / "branches" / "main"
    branch_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "name": "測試者",
        "inventory": {"封印之鏡": "可封印低等級怨靈", "鎮魂符": "×3"},
        "abilities": ["咒靈操術 (A級)", "靈視"],
        "relationships": {"阿豪": "信任", "審判暴君": "敵對"},
        "completed_missions": ["咒怨 — 完美通關"],
        "systems": {"死生之道": "A級"},
    }
    npcs = [
        {"name": "阿豪", "role": "隊友", "tier": "B+", "current_status": "待命"},
        {"name": "審判暴君", "role": "敵人", "tier": "S-", "current_status": "交戰"},
    ]
    (branch_dir / "character_state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    (branch_dir / "npcs.json").write_text(json.dumps(npcs, ensure_ascii=False), encoding="utf-8")
    return branch_dir


class TestRebuild:
    def test_rebuild_from_json_populates_categories(self, story_id, setup_branch):
        count = state_db.rebuild_from_json(story_id, "main")
        assert count >= 8
        summary = state_db.get_summary(story_id, "main")
        assert "道具" in summary
        assert "技能" in summary
        assert "NPC 檔案" in summary

    def test_search_triggers_lazy_rebuild(self, story_id, setup_branch):
        text = state_db.search_state(story_id, "main", "我要用封印之鏡")
        assert "[相關角色狀態]" in text
        assert "封印之鏡" in text


class TestSearch:
    def test_search_must_include_keeps_forced_key(self, story_id, setup_branch):
        state_db.rebuild_from_json(story_id, "main")
        text = state_db.search_state(
            story_id,
            "main",
            "這句話和阿豪無關",
            token_budget=20,
            must_include_keys=["阿豪"],
        )
        assert "阿豪" in text

    def test_replace_category_clears_old_rows(self, story_id, setup_branch):
        state_db.rebuild_from_json(story_id, "main")
        state_db.replace_category(
            story_id,
            "main",
            "inventory",
            [("新道具", "", "道具")],
        )
        text = state_db.search_state(story_id, "main", "道具")
        assert "新道具" in text
        assert "封印之鏡" not in text

    def test_context_boost_prefers_npc_in_dungeon(self, story_id, setup_branch):
        state_db.rebuild_from_json(story_id, "main")
        text = state_db.search_state(
            story_id,
            "main",
            "暴君",
            context={"phase": "副本中", "status": "戰鬥中", "dungeon": "咒術迴戰"},
        )
        assert "審判暴君" in text
