"""Tests for branch lore feature (PR #84).

Tests the branch_lore helpers, API routes, branch operations,
and context injection changes introduced by the base/branch lore split.
"""

import json
import os
import threading
from unittest import mock

import pytest

import app as app_module
import event_db
import lore_db


# ===================================================================
# Fixtures
# ===================================================================


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
    """Create a minimal story with all required files for branch lore tests."""
    stories_dir = tmp_path / "data" / "stories"
    story_dir = stories_dir / story_id
    branch_dir = story_dir / "branches" / "main"
    branch_dir.mkdir(parents=True, exist_ok=True)

    # Design files directory
    design_dir = tmp_path / "story_design" / story_id
    design_dir.mkdir(parents=True, exist_ok=True)

    # Stories registry
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

    # Base lore (world_lore.json) → story_design/
    base_lore = [
        {"category": "體系", "topic": "基因鎖", "content": "基因鎖是人類潛能的封印", "edited_by": "user"},
        {"category": "副本世界觀", "topic": "咒怨", "content": "咒怨副本基於日本恐怖電影"},
    ]
    (design_dir / "world_lore.json").write_text(
        json.dumps(base_lore, ensure_ascii=False), encoding="utf-8"
    )

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

    # LLM config
    config = {
        "provider": "gemini",
        "gemini": {"api_keys": [{"key": "test_key_123", "tier": "free"}], "model": "gemini-2.5-flash"},
        "claude_cli": {"model": "claude-sonnet-4-5-20250929"},
    }
    (tmp_path / "llm_config.json").write_text(json.dumps(config), encoding="utf-8")

    return story_dir


# ===================================================================
# Unit: Branch Lore Helpers
# ===================================================================


class TestBranchLoreLoadSave:
    def test_load_missing_file_returns_empty(self, story_id, setup_story):
        """Loading branch lore from non-existent file returns empty list."""
        result = app_module._load_branch_lore(story_id, "main")
        assert result == []

    def test_save_and_load_roundtrip(self, story_id, setup_story):
        """Save then load returns same data."""
        entries = [
            {"category": "體系", "topic": "新發現", "content": "內容A"},
            {"category": "副本", "topic": "資料", "content": "內容B"},
        ]
        app_module._save_branch_lore(story_id, "main", entries)
        loaded = app_module._load_branch_lore(story_id, "main")
        assert len(loaded) == 2
        assert loaded[0]["topic"] == "新發現"
        assert loaded[1]["topic"] == "資料"

    def test_branch_lore_path(self, story_id, setup_story):
        """Path should be under branches/<bid>/branch_lore.json."""
        path = app_module._branch_lore_path(story_id, "main")
        assert path.endswith(os.path.join("branches", "main", "branch_lore.json"))


class TestSaveBranchLoreEntry:
    def test_new_entry_appended(self, story_id, setup_story):
        """New entry should be appended."""
        app_module._save_branch_lore_entry(story_id, "main", {
            "category": "體系", "topic": "新體系", "content": "新內容",
        })
        lore = app_module._load_branch_lore(story_id, "main")
        assert len(lore) == 1
        assert lore[0]["topic"] == "新體系"

    def test_existing_topic_updated(self, story_id, setup_story):
        """Existing topic should be updated in place (upsert)."""
        app_module._save_branch_lore_entry(story_id, "main", {
            "category": "體系", "topic": "體系A", "content": "版本1",
        })
        app_module._save_branch_lore_entry(story_id, "main", {
            "category": "體系", "topic": "體系A", "content": "版本2",
        })
        lore = app_module._load_branch_lore(story_id, "main")
        assert len(lore) == 1  # Not duplicated
        assert lore[0]["content"] == "版本2"

    def test_empty_topic_ignored(self, story_id, setup_story):
        """Entry with empty topic should be silently ignored."""
        app_module._save_branch_lore_entry(story_id, "main", {
            "category": "A", "topic": "", "content": "內容",
        })
        lore = app_module._load_branch_lore(story_id, "main")
        assert len(lore) == 0

    def test_preserves_existing_metadata(self, story_id, setup_story):
        """Upsert should preserve existing category/source/edited_by if not in new entry."""
        app_module._save_branch_lore_entry(story_id, "main", {
            "category": "體系", "topic": "保留項", "content": "原始",
            "source": {"branch_id": "main"}, "edited_by": "auto",
        })
        # Update without category/source/edited_by
        app_module._save_branch_lore_entry(story_id, "main", {
            "topic": "保留項", "content": "已更新",
        })
        lore = app_module._load_branch_lore(story_id, "main")
        assert lore[0]["content"] == "已更新"
        assert lore[0]["category"] == "體系"  # Preserved
        assert lore[0]["edited_by"] == "auto"  # Preserved

    def test_thread_safety(self, story_id, setup_story):
        """Concurrent writes should not lose entries."""
        errors = []

        def write_entry(i):
            try:
                app_module._save_branch_lore_entry(story_id, "main", {
                    "category": "test", "topic": f"topic_{i}", "content": f"content_{i}",
                })
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_entry, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        lore = app_module._load_branch_lore(story_id, "main")
        assert len(lore) == 10


class TestMergeBranchLore:
    def test_empty_src_noop(self, story_id, setup_story):
        """Merge from empty source should not change destination."""
        # Create branch_b with no lore
        branch_b = setup_story / "branches" / "branch_b"
        branch_b.mkdir(parents=True, exist_ok=True)

        app_module._save_branch_lore(story_id, "main", [
            {"category": "A", "topic": "existing", "content": "data"},
        ])
        app_module._merge_branch_lore_into(story_id, "branch_b", "main")
        result = app_module._load_branch_lore(story_id, "main")
        assert len(result) == 1
        assert result[0]["topic"] == "existing"

    def test_non_overlapping_entries_appended(self, story_id, setup_story):
        """Non-overlapping entries should be appended."""
        branch_b = setup_story / "branches" / "branch_b"
        branch_b.mkdir(parents=True, exist_ok=True)

        app_module._save_branch_lore(story_id, "main", [
            {"category": "A", "topic": "main_topic", "content": "main"},
        ])
        app_module._save_branch_lore(story_id, "branch_b", [
            {"category": "B", "topic": "branch_topic", "content": "branch"},
        ])
        app_module._merge_branch_lore_into(story_id, "branch_b", "main")
        result = app_module._load_branch_lore(story_id, "main")
        assert len(result) == 2
        topics = {e["topic"] for e in result}
        assert topics == {"main_topic", "branch_topic"}

    def test_overlapping_topics_upserted(self, story_id, setup_story):
        """Overlapping topics should be overwritten by source."""
        branch_b = setup_story / "branches" / "branch_b"
        branch_b.mkdir(parents=True, exist_ok=True)

        app_module._save_branch_lore(story_id, "main", [
            {"category": "A", "topic": "shared", "content": "old"},
            {"category": "A", "topic": "main_only", "content": "keep"},
        ])
        app_module._save_branch_lore(story_id, "branch_b", [
            {"category": "A", "topic": "shared", "content": "new"},
            {"category": "B", "topic": "branch_only", "content": "add"},
        ])
        app_module._merge_branch_lore_into(story_id, "branch_b", "main")
        result = app_module._load_branch_lore(story_id, "main")
        assert len(result) == 3
        shared = next(e for e in result if e["topic"] == "shared")
        assert shared["content"] == "new"  # Updated from source


class TestSearchBranchLore:
    def test_empty_returns_empty(self, story_id, setup_story):
        """Empty branch lore returns empty string."""
        result = app_module._search_branch_lore(story_id, "main", "基因鎖")
        assert result == ""

    def test_cjk_bigram_matching(self, story_id, setup_story):
        """CJK bigram matching should find relevant entries."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "體系", "topic": "基因鎖突破", "content": "第一階段突破記錄"},
            {"category": "副本", "topic": "咒怨經歷", "content": "副本任務完成"},
        ])
        result = app_module._search_branch_lore(story_id, "main", "基因鎖")
        assert "[相關分支設定]" in result
        assert "基因鎖突破" in result

    def test_topic_substring_boost(self, story_id, setup_story):
        """Topic substring match should rank higher."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "other", "topic": "其他事項", "content": "包含修真的描述"},
            {"category": "體系", "topic": "修真進度", "content": "簡短內容"},
        ])
        result = app_module._search_branch_lore(story_id, "main", "修真")
        assert "修真進度" in result

    def test_token_budget_limits_output(self, story_id, setup_story):
        """Token budget should limit the number of returned entries."""
        # Create many entries with long content
        entries = []
        for i in range(20):
            entries.append({
                "category": "test",
                "topic": f"長篇設定{i}",
                "content": f"這是一段很長的設定內容，用來測試token預算限制{'很長的內容' * 50}",
            })
        app_module._save_branch_lore(story_id, "main", entries)
        # With a small budget, not all entries should be returned
        result = app_module._search_branch_lore(story_id, "main", "設定", token_budget=200)
        assert "[相關分支設定]" in result
        # Count returned sections (each entry has a #### header)
        headers = [line for line in result.split("\n") if line.startswith("####")]
        assert len(headers) < 20  # Budget should limit output

    def test_no_match_returns_empty(self, story_id, setup_story):
        """Query with no match returns empty string."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "A", "topic": "中文主題", "content": "中文內容"},
        ])
        result = app_module._search_branch_lore(story_id, "main", "xyz")
        assert result == ""

    def test_dungeon_scoping_penalizes_other_dungeons(self, story_id, setup_story):
        """Cross-dungeon entries should be penalized when in a specific dungeon."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "副本世界觀", "subcategory": "咒術迴戰", "topic": "咒術迴戰機制", "content": "呪力是咒術師的核心戰鬥力來源"},
            {"category": "副本世界觀", "subcategory": "民俗台灣", "topic": "民俗台灣習俗", "content": "台灣民俗副本涉及各種禁忌和習俗"},
        ])
        context = {"phase": "副本中", "status": "", "dungeon": "民俗台灣"}
        result = app_module._search_branch_lore(story_id, "main", "副本", context=context)
        assert "民俗台灣習俗" in result
        # 咒術迴戰 should be penalized — if it appears at all, it should be after 民俗台灣
        lines = result.split("\n")
        headers = [l for l in lines if l.startswith("####")]
        if len(headers) >= 2:
            assert headers[0].find("民俗台灣") >= 0

    def test_dungeon_scoping_no_penalty_without_context(self, story_id, setup_story):
        """Without context, no dungeon penalty applied."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "副本世界觀", "subcategory": "咒術迴戰", "topic": "呪力系統", "content": "呪力是咒術師的核心戰鬥力來源"},
            {"category": "副本世界觀", "subcategory": "民俗台灣", "topic": "民俗禁忌", "content": "台灣民俗副本涉及各種禁忌和習俗"},
        ])
        result = app_module._search_branch_lore(story_id, "main", "副本")
        # Both should appear without penalty
        assert "[相關分支設定]" in result


class TestGetBranchLoreTOC:
    def test_empty_returns_empty(self, story_id, setup_story):
        """No branch lore returns empty string."""
        result = app_module._get_branch_lore_toc(story_id, "main")
        assert result == ""

    def test_formats_entries(self, story_id, setup_story):
        """TOC should format as '- category：topic' per entry."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "體系", "topic": "基因鎖突破"},
            {"category": "副本", "topic": "咒怨經歷"},
        ])
        result = app_module._get_branch_lore_toc(story_id, "main")
        assert "- 體系：基因鎖突破" in result
        assert "- 副本：咒怨經歷" in result


# ===================================================================
# Integration: Context Injection
# ===================================================================


class TestBuildLoreTextWithBranch:
    def test_no_branch_lore_no_note(self, story_id, setup_story):
        """Without branch lore, no branch note in output."""
        lore_db.rebuild_index(story_id)
        text = app_module._build_lore_text(story_id, branch_id="main")
        assert "分支專屬設定" not in text

    def test_with_branch_lore_shows_count(self, story_id, setup_story):
        """With branch lore, shows count note."""
        lore_db.rebuild_index(story_id)
        app_module._save_branch_lore(story_id, "main", [
            {"category": "A", "topic": "topic1", "content": "c1"},
            {"category": "B", "topic": "topic2", "content": "c2"},
        ])
        text = app_module._build_lore_text(story_id, branch_id="main")
        assert "2 條分支專屬設定" in text


class TestBuildAugmentedMessageWithBranchLore:
    @mock.patch("app.search_relevant_lore", return_value="[相關世界設定]\n基因鎖")
    @mock.patch("app.search_relevant_events", return_value="")
    @mock.patch("app.get_recent_activities", return_value="")
    @mock.patch("app.is_gm_command", return_value=False)
    @mock.patch("app.roll_fate", return_value={"outcome": "順遂"})
    @mock.patch("app.format_dice_context", return_value="[命運走向] 順遂")
    def test_branch_lore_injected(self, mock_fmt, mock_roll, mock_gm,
                                  mock_act, mock_evt, mock_lore,
                                  story_id, setup_story):
        """Branch lore search results should appear in augmented message."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "體系", "topic": "基因鎖突破", "content": "第一階段突破記錄"},
        ])
        text, _ = app_module._build_augmented_message(
            story_id, "main", "基因鎖怎麼突破",
            {"current_phase": "主神空間"},
        )
        assert "[相關世界設定]" in text
        assert "[相關分支設定]" in text
        assert "基因鎖突破" in text

    @mock.patch("app.search_relevant_lore", return_value="[相關世界設定]\n基因鎖")
    @mock.patch("app.search_relevant_events", return_value="")
    @mock.patch("app.get_recent_activities", return_value="")
    @mock.patch("app.is_gm_command", return_value=False)
    @mock.patch("app.roll_fate", return_value={"outcome": "順遂"})
    @mock.patch("app.format_dice_context", return_value="[命運走向] 順遂")
    def test_no_branch_lore_no_section(self, mock_fmt, mock_roll, mock_gm,
                                       mock_act, mock_evt, mock_lore,
                                       story_id, setup_story):
        """Without branch lore entries, no branch section in augmented message."""
        text, _ = app_module._build_augmented_message(
            story_id, "main", "你好",
            {"current_phase": "主神空間"},
        )
        assert "[相關分支設定]" not in text


# ===================================================================
# Integration: API Routes
# ===================================================================


class TestLoreAllAPIWithLayers:
    def test_base_entries_have_layer(self, client, setup_story):
        """GET /api/lore/all should return base entries with layer='base'."""
        resp = client.get("/api/lore/all")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        base_entries = [e for e in data["entries"] if e.get("layer") == "base"]
        assert len(base_entries) == 2  # 基因鎖 + 咒怨

    def test_branch_entries_have_layer(self, client, setup_story, story_id):
        """Branch lore entries should have layer='branch'."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "體系", "topic": "分支知識", "content": "分支內容"},
        ])
        resp = client.get("/api/lore/all")
        data = resp.get_json()
        branch_entries = [e for e in data["entries"] if e.get("layer") == "branch"]
        assert len(branch_entries) == 1
        assert branch_entries[0]["topic"] == "分支知識"

    def test_returns_branch_id(self, client, setup_story):
        """Response should include branch_id."""
        resp = client.get("/api/lore/all")
        data = resp.get_json()
        assert "branch_id" in data


class TestBranchLoreDeleteAPI:
    def test_delete_branch_entry(self, client, setup_story, story_id):
        """DELETE /api/lore/branch/entry should remove entry from branch lore."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "A", "topic": "刪除目標", "content": "content"},
            {"category": "B", "topic": "保留項", "content": "keep"},
        ])
        resp = client.delete("/api/lore/branch/entry", json={
            "topic": "刪除目標", "branch_id": "main",
        })
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        lore = app_module._load_branch_lore(story_id, "main")
        assert len(lore) == 1
        assert lore[0]["topic"] == "保留項"

    def test_delete_nonexistent_entry_404(self, client, setup_story, story_id):
        """Deleting a non-existent entry returns 404."""
        resp = client.delete("/api/lore/branch/entry", json={
            "topic": "不存在", "branch_id": "main",
        })
        assert resp.status_code == 404

    def test_delete_missing_params_400(self, client, setup_story):
        """Missing topic or branch_id returns 400."""
        resp = client.delete("/api/lore/branch/entry", json={"topic": "x"})
        assert resp.status_code == 400


class TestLorePromoteAPI:
    def test_promote_moves_to_base(self, client, setup_story, story_id):
        """POST /api/lore/promote should move entry from branch to base."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "體系", "topic": "可提升項", "content": "世界設定"},
        ])
        resp = client.post("/api/lore/promote", json={
            "branch_id": "main", "topic": "可提升項",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["entry"]["topic"] == "可提升項"
        assert data["entry"]["edited_by"] == "user"  # Promoted = user-curated

        # Verify moved: not in branch, is in base
        branch_lore = app_module._load_branch_lore(story_id, "main")
        assert all(e["topic"] != "可提升項" for e in branch_lore)
        base_lore = app_module._load_lore(story_id)
        assert any(e["topic"] == "可提升項" for e in base_lore)

    def test_promote_with_content_override(self, client, setup_story, story_id):
        """Promote with content override (rewrite action)."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "體系", "topic": "改寫項", "content": "含角色名的內容"},
        ])
        resp = client.post("/api/lore/promote", json={
            "branch_id": "main", "topic": "改寫項",
            "content": "通用世界設定（已改寫）",
        })
        assert resp.status_code == 200
        entry = resp.get_json()["entry"]
        assert entry["content"] == "通用世界設定（已改寫）"

    def test_promote_nonexistent_entry_404(self, client, setup_story):
        """Promoting non-existent entry returns 404."""
        resp = client.post("/api/lore/promote", json={
            "branch_id": "main", "topic": "不存在",
        })
        assert resp.status_code == 404


class TestLorePromoteReviewAPI:
    @mock.patch("llm_bridge.call_oneshot")
    def test_review_returns_proposals(self, mock_llm, client, setup_story, story_id):
        """POST /api/lore/promote/review should return LLM proposals."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "體系", "topic": "世界規則", "content": "純世界設定"},
            {"category": "故事追蹤", "topic": "角色經歷", "content": "角色完成了副本"},
        ])
        mock_llm.return_value = json.dumps([
            {"index": 0, "action": "promote", "reason": "純世界設定"},
            {"index": 1, "action": "reject", "reason": "角色經驗"},
        ])
        resp = client.post("/api/lore/promote/review", json={"branch_id": "main"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["proposals"]) == 2
        assert data["proposals"][0]["action"] == "promote"
        assert data["proposals"][0]["topic"] == "世界規則"
        assert data["proposals"][1]["action"] == "reject"

    @mock.patch("llm_bridge.call_oneshot")
    def test_review_empty_branch_lore(self, mock_llm, client, setup_story):
        """Review with empty branch lore returns empty proposals."""
        resp = client.post("/api/lore/promote/review", json={"branch_id": "main"})
        assert resp.status_code == 200
        assert resp.get_json()["proposals"] == []
        mock_llm.assert_not_called()

    @mock.patch("llm_bridge.call_oneshot")
    def test_review_malformed_llm_response(self, mock_llm, client, setup_story, story_id):
        """Malformed LLM response should be handled gracefully."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "A", "topic": "test", "content": "c"},
        ])
        mock_llm.return_value = "not json at all"
        resp = client.post("/api/lore/promote/review", json={"branch_id": "main"})
        assert resp.status_code == 500

    @mock.patch("llm_bridge.call_oneshot")
    def test_review_markdown_fenced_json(self, mock_llm, client, setup_story, story_id):
        """LLM response wrapped in markdown fences should be parsed."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "A", "topic": "test", "content": "c"},
        ])
        inner = json.dumps([{"index": 0, "action": "promote", "reason": "ok"}])
        mock_llm.return_value = f"```json\n{inner}\n```"
        resp = client.post("/api/lore/promote/review", json={"branch_id": "main"})
        assert resp.status_code == 200
        assert len(resp.get_json()["proposals"]) == 1

    def test_review_missing_branch_id_400(self, client, setup_story):
        """Missing branch_id returns 400."""
        resp = client.post("/api/lore/promote/review", json={})
        assert resp.status_code == 400


# ===================================================================
# Integration: Branch Operations
# ===================================================================


class TestBranchOperationsWithLore:
    def test_blank_branch_no_branch_lore(self, client, setup_story, story_id):
        """Blank branch should not inherit any branch lore."""
        # Add branch lore to main
        app_module._save_branch_lore(story_id, "main", [
            {"category": "A", "topic": "main_lore", "content": "should not inherit"},
        ])
        resp = client.post("/api/branches/blank", json={"name": "空白分支"})
        assert resp.status_code == 200
        branch = resp.get_json()["branch"]
        bid = branch["id"]
        # Blank branch should have empty branch lore
        lore = app_module._load_branch_lore(story_id, bid)
        assert lore == []

    def test_fork_copies_branch_lore(self, client, setup_story, story_id):
        """Forking a branch should copy parent's branch_lore.json."""
        # Add branch lore to main
        app_module._save_branch_lore(story_id, "main", [
            {"category": "體系", "topic": "繼承項", "content": "should be copied"},
        ])
        resp = client.post("/api/branches", json={
            "name": "分支",
            "branch_point_index": 1,
        })
        assert resp.status_code == 200
        bid = resp.get_json()["branch"]["id"]
        lore = app_module._load_branch_lore(story_id, bid)
        assert len(lore) == 1
        assert lore[0]["topic"] == "繼承項"

    def test_fork_lore_is_independent_copy(self, client, setup_story, story_id):
        """Forked branch lore should be independent (not shared reference)."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "A", "topic": "shared", "content": "original"},
        ])
        resp = client.post("/api/branches", json={
            "name": "分支",
            "branch_point_index": 1,
        })
        bid = resp.get_json()["branch"]["id"]

        # Modify child's branch lore
        app_module._save_branch_lore_entry(story_id, bid, {
            "category": "B", "topic": "child_only", "content": "new",
        })

        # Parent should not be affected
        parent_lore = app_module._load_branch_lore(story_id, "main")
        assert len(parent_lore) == 1
        assert parent_lore[0]["topic"] == "shared"

    def test_fork_excludes_future_branch_lore_by_source_msg_index(self, client, setup_story, story_id):
        """Fork should not inherit branch lore extracted after branch_point_index."""
        app_module._save_branch_lore(story_id, "main", [
            {
                "category": "體系",
                "topic": "過去設定",
                "content": "可繼承",
                "source": {"branch_id": "main", "msg_index": 1},
            },
            {
                "category": "體系",
                "topic": "未來設定",
                "content": "不應繼承",
                "source": {"branch_id": "main", "msg_index": 3},
            },
        ])

        resp = client.post("/api/branches", json={
            "name": "分支",
            "branch_point_index": 1,
        })
        assert resp.status_code == 200
        bid = resp.get_json()["branch"]["id"]

        lore = app_module._load_branch_lore(story_id, bid)
        topics = {e["topic"] for e in lore}
        assert "過去設定" in topics
        assert "未來設定" not in topics

    def test_fork_keeps_legacy_branch_lore_without_source(self, client, setup_story, story_id):
        """Legacy entries without source metadata should still be inherited."""
        app_module._save_branch_lore(story_id, "main", [
            {"category": "體系", "topic": "舊格式設定", "content": "legacy"},
        ])
        resp = client.post("/api/branches", json={
            "name": "分支",
            "branch_point_index": 1,
        })
        assert resp.status_code == 200
        bid = resp.get_json()["branch"]["id"]

        lore = app_module._load_branch_lore(story_id, bid)
        topics = {e["topic"] for e in lore}
        assert "舊格式設定" in topics


# ===================================================================
# Integration: Inline LORE Tag → Branch Lore
# ===================================================================


class TestInlineLoreTagGoesToBranch:
    def test_inline_lore_tag_saves_to_branch(self, story_id, setup_story):
        """Inline <!--LORE {...} LORE--> tag should save to branch_lore.json."""
        gm_text = (
            "故事內容。\n"
            '<!--LORE {"category": "體系", "topic": "內聯設定", "content": "測試內容"} LORE-->\n'
            "更多故事。"
        )
        app_module._process_gm_response(gm_text, story_id, "main", msg_index=5)

        branch_lore = app_module._load_branch_lore(story_id, "main")
        topics = [e["topic"] for e in branch_lore]
        assert "內聯設定" in topics

        # Should NOT be in base lore
        base_lore = app_module._load_lore(story_id)
        base_topics = [e["topic"] for e in base_lore]
        assert "內聯設定" not in base_topics
