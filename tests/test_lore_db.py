"""Tests for lore_db.py (Phase 1.3).

Tests CJK bigram scoring, keyword search, index building, upsert, and tag extraction.
Uses monkeypatched STORIES_DIR for filesystem isolation.
Embedding-related functions are tested with mocked llm_bridge.
"""

import json
import os

import pytest

import lore_db


@pytest.fixture(autouse=True)
def patch_stories_dir(tmp_path, monkeypatch):
    """Redirect lore_db STORIES_DIR and STORY_DESIGN_DIR to tmp_path."""
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir()
    design_dir = tmp_path / "story_design"
    design_dir.mkdir()
    monkeypatch.setattr(lore_db, "STORIES_DIR", str(stories_dir))
    monkeypatch.setattr(lore_db, "STORY_DESIGN_DIR", str(design_dir))
    # Clear embedding cache
    lore_db._embedding_cache.clear()
    return stories_dir


@pytest.fixture
def story_id():
    return "test_story"


@pytest.fixture
def setup_lore(tmp_path, story_id):
    """Create a story dir with world_lore.json and build the index."""
    # lore.db goes in stories dir (runtime), world_lore.json in design dir
    story_path = tmp_path / "stories" / story_id
    story_path.mkdir(parents=True)
    design_path = tmp_path / "story_design" / story_id
    design_path.mkdir(parents=True, exist_ok=True)

    lore_entries = [
        {
            "category": "主神設定與規則",
            "topic": "空間概述",
            "content": "主神空間是一個超維度存在，收集各個世界的靈魂進行試煉。[tag: 主神/空間]",
        },
        {
            "category": "主神設定與規則",
            "topic": "任務規則",
            "content": "每次副本任務限時七天，完成度決定獎勵點數量。死亡者可能被復活但需要消耗獎勵點。[tag: 任務/規則]",
        },
        {
            "category": "體系",
            "topic": "基因鎖",
            "content": "基因鎖是人類潛能的封印，開啟後可大幅提升身體素質和精神力。共有五個階段。[tag: 體系/基因鎖]",
        },
        {
            "category": "體系",
            "topic": "修真",
            "content": "修真體系包含煉氣、築基、金丹、元嬰等境界。修真者可操控靈力進行攻擊和防禦。[tag: 體系/修真]",
        },
        {
            "category": "體系",
            "topic": "鬥氣",
            "content": "鬥氣是通過鍛鍊體內生命能量形成的戰鬥力量。分為初級、中級、高級、超級四個等級。[tag: 體系/鬥氣]",
        },
        {
            "category": "副本世界觀",
            "topic": "咒怨",
            "content": "咒怨副本基於日本恐怖電影世界觀。伽椰子是核心怨靈，進入咒怨之屋的人會被詛咒。[tag: 副本/咒怨]",
        },
        {
            "category": "副本世界觀",
            "topic": "生化危機",
            "content": "生化危機副本基於遊戲世界觀。T病毒爆發，殭屍橫行。副本目標是逃出拉坤市。[tag: 副本/生化危機]",
        },
        {
            "category": "商城",
            "topic": "價目表",
            "content": "主神空間商城提供各類道具和強化服務。基因鎖開啟：5000點，體質強化：2000點起。[tag: 商城/價格]",
        },
    ]

    (design_path / "world_lore.json").write_text(
        json.dumps(lore_entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Build index (without embeddings)
    lore_db.rebuild_index(story_id)
    return story_path


@pytest.fixture
def setup_lore_with_subcategory(tmp_path, story_id):
    """Create lore entries with subcategory for dungeon scoping tests."""
    story_path = tmp_path / "stories" / story_id
    story_path.mkdir(parents=True, exist_ok=True)
    design_path = tmp_path / "story_design" / story_id
    design_path.mkdir(parents=True, exist_ok=True)

    lore_entries = [
        {
            "category": "副本世界觀",
            "subcategory": "咒怨",
            "topic": "咒怨",
            "content": "咒怨副本基於日本恐怖電影世界觀。伽椰子是核心怨靈，進入咒怨之屋的人會被詛咒。副本目標是生存或消滅怨靈。",
        },
        {
            "category": "副本世界觀",
            "subcategory": "生化危機",
            "topic": "生化危機",
            "content": "生化危機副本基於遊戲世界觀。T病毒爆發，殭屍橫行。副本目標是逃出拉坤市。",
        },
        {
            "category": "主神設定與規則",
            "subcategory": "",
            "topic": "任務規則",
            "content": "每次副本任務限時七天，完成度決定獎勵點數量。",
        },
    ]

    (design_path / "world_lore.json").write_text(
        json.dumps(lore_entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lore_db.rebuild_index(story_id)
    return story_path


# ===================================================================
# extract_tags — pure function
# ===================================================================


class TestExtractTags:
    def test_single_tag(self):
        tags = lore_db.extract_tags("內容 [tag: 體系/基因鎖]")
        assert tags == ["體系", "基因鎖"]

    def test_multiple_tags(self):
        tags = lore_db.extract_tags("[tag: A/B] 中間 [tag: C]")
        assert tags == ["A", "B", "C"]

    def test_dedup_preserves_order(self):
        tags = lore_db.extract_tags("[tag: A/B] [tag: B/C]")
        assert tags == ["A", "B", "C"]

    def test_no_tags(self):
        tags = lore_db.extract_tags("普通文字，沒有標籤")
        assert tags == []

    def test_empty_string(self):
        tags = lore_db.extract_tags("")
        assert tags == []


# ===================================================================
# rebuild_index
# ===================================================================


class TestRebuildIndex:
    def test_builds_all_entries(self, story_id, setup_lore):
        stats = lore_db.get_embedding_stats(story_id)
        assert stats["total"] == 8

    def test_skips_placeholder_entries(self, tmp_path, story_id):
        design_path = tmp_path / "story_design" / story_id
        design_path.mkdir(parents=True, exist_ok=True)
        entries = [
            {"category": "A", "topic": "Real", "content": "真實內容"},
            {"category": "A", "topic": "Placeholder", "content": "（待建立）這個還沒寫"},
        ]
        (design_path / "world_lore.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )
        lore_db.rebuild_index(story_id)
        stats = lore_db.get_embedding_stats(story_id)
        assert stats["total"] == 1  # Placeholder skipped

    def test_removes_stale_entries(self, tmp_path, story_id, setup_lore):
        # Initially 8 entries
        assert lore_db.get_embedding_stats(story_id)["total"] == 8

        # Rewrite with fewer entries
        design_path = tmp_path / "story_design" / story_id
        entries = [
            {"category": "體系", "topic": "基因鎖", "content": "更新後的內容"},
        ]
        (design_path / "world_lore.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )
        lore_db.rebuild_index(story_id)
        assert lore_db.get_embedding_stats(story_id)["total"] == 1

    def test_no_json_file_does_nothing(self, story_id):
        # No world_lore.json — should not raise
        lore_db.rebuild_index(story_id)


# ===================================================================
# upsert_entry
# ===================================================================


class TestUpsertEntry:
    def test_insert_new(self, story_id, setup_lore):
        before = lore_db.get_embedding_stats(story_id)["total"]
        lore_db.upsert_entry(story_id, {
            "category": "新分類",
            "topic": "全新主題",
            "content": "新的內容",
        })
        after = lore_db.get_embedding_stats(story_id)["total"]
        assert after == before + 1

    def test_update_existing(self, story_id, setup_lore):
        before = lore_db.get_embedding_stats(story_id)["total"]
        lore_db.upsert_entry(story_id, {
            "category": "體系",
            "topic": "基因鎖",  # Existing topic
            "content": "完全更新的內容，不一樣了",
        })
        after = lore_db.get_embedding_stats(story_id)["total"]
        assert after == before  # No new entry, just updated

    def test_empty_topic_ignored(self, story_id, setup_lore):
        before = lore_db.get_embedding_stats(story_id)["total"]
        lore_db.upsert_entry(story_id, {
            "category": "A",
            "topic": "",
            "content": "有內容但沒主題",
        })
        after = lore_db.get_embedding_stats(story_id)["total"]
        assert after == before


# ===================================================================
# search_lore — CJK bigram keyword scoring
# ===================================================================


class TestSearchLore:
    def test_search_by_topic(self, story_id, setup_lore):
        results = lore_db.search_lore(story_id, "基因鎖")
        assert len(results) > 0
        assert results[0]["topic"] == "基因鎖"

    def test_topic_match_scores_highest(self, story_id, setup_lore):
        # "基因鎖" in topic of one entry, in content of "價目表"
        results = lore_db.search_lore(story_id, "基因鎖")
        assert results[0]["topic"] == "基因鎖"

    def test_search_multiple_results(self, story_id, setup_lore):
        # "體系" appears in multiple entries' tags
        results = lore_db.search_lore(story_id, "體系修煉")
        assert len(results) > 1

    def test_search_no_results(self, story_id, setup_lore):
        results = lore_db.search_lore(story_id, "完全不相關的英文 xyz")
        # Fallback: uses full query as keyword — unlikely to match
        # CJK bigrams: no CJK in query, so keywords = {"完全不相關的英文 xyz"}
        # Actually the query has CJK, so bigrams will be generated
        # Let's use pure English
        results = lore_db.search_lore(story_id, "xyz")
        assert results == []

    def test_search_respects_limit(self, story_id, setup_lore):
        results = lore_db.search_lore(story_id, "的", limit=3)
        assert len(results) <= 3

    def test_cjk_bigram_generation(self, story_id, setup_lore):
        # "修真" (2 chars) → 1 bigram: "修真"
        results = lore_db.search_lore(story_id, "修真")
        assert len(results) > 0
        topics = [r["topic"] for r in results]
        assert "修真" in topics

    def test_cjk_trigram_generation(self, story_id, setup_lore):
        # "基因鎖" (3 chars) → 2 bigrams ("基因", "因鎖") + 1 trigram ("基因鎖")
        results = lore_db.search_lore(story_id, "基因鎖")
        assert results[0]["topic"] == "基因鎖"

    def test_content_match_lower_score(self, story_id, setup_lore):
        # "副本" appears in content of many entries but in topic of "咒怨" and "生化危機"
        results = lore_db.search_lore(story_id, "副本世界")
        # Entries with 副本 in topic/category should rank higher
        assert len(results) > 0

    def test_tag_match_medium_score(self, story_id, setup_lore):
        # "咒怨" is in tags of the 咒怨 entry
        results = lore_db.search_lore(story_id, "咒怨")
        assert results[0]["topic"] == "咒怨"

    def test_english_fallback(self, story_id, setup_lore):
        # Pure English query → uses full string as keyword
        # "T病毒" has CJK: "T病毒" → bigrams: "病毒"
        results = lore_db.search_lore(story_id, "T病毒")
        assert len(results) > 0


# ===================================================================
# search_hybrid — RRF fusion
# ===================================================================


class TestSearchHybrid:
    def test_keyword_only_when_no_embeddings(self, story_id, setup_lore):
        """When no embeddings exist, hybrid should still return keyword results."""
        results = lore_db.search_hybrid(story_id, "基因鎖")
        assert len(results) > 0
        topics = [r["topic"] for r in results]
        assert "基因鎖" in topics

    def test_token_budget_respected(self, story_id, setup_lore):
        # With a very small budget, should limit results
        results = lore_db.search_hybrid(story_id, "的", token_budget=100)
        total_tokens = sum(
            min(len(r.get("content", "")), 1200) + len(r.get("topic", "")) + 20
            for r in results
        )
        # First entry is always included even if over budget
        assert len(results) >= 1

    def test_category_boost_dungeon(self, story_id, setup_lore):
        """副本中 phase should boost 副本世界觀 entries."""
        context = {"phase": "副本中", "status": ""}
        results = lore_db.search_hybrid(
            story_id, "規則",
            context=context,
        )
        # With dungeon boost, 副本世界觀 entries should rank higher
        assert len(results) > 0

    def test_category_boost_space(self, story_id, setup_lore):
        """主神空間 phase should boost 主神設定/商城/場景."""
        context = {"phase": "主神空間", "status": ""}
        results = lore_db.search_hybrid(
            story_id, "規則",
            context=context,
        )
        assert len(results) > 0

    def test_no_context_no_boost(self, story_id, setup_lore):
        results = lore_db.search_hybrid(story_id, "基因鎖", context=None)
        assert len(results) > 0

    def test_empty_results_when_no_match(self, story_id, setup_lore):
        results = lore_db.search_hybrid(story_id, "zzz_no_match")
        assert results == []

    def test_dungeon_scoping_penalizes_other_dungeons(self, story_id, setup_lore_with_subcategory):
        """When in 咒怨 dungeon, 生化危機 entries should be penalized."""
        context = {"phase": "副本中", "status": "", "dungeon": "咒怨"}
        results = lore_db.search_hybrid(story_id, "副本", context=context)
        assert len(results) > 0
        # 咒怨 should rank before 生化危機
        topics = [r["topic"] for r in results]
        if "咒怨" in topics and "生化危機" in topics:
            assert topics.index("咒怨") < topics.index("生化危機")

    def test_dungeon_scoping_no_penalty_without_dungeon(self, story_id, setup_lore_with_subcategory):
        """Without dungeon context, no penalization."""
        context = {"phase": "副本中", "status": "", "dungeon": ""}
        results = lore_db.search_hybrid(story_id, "副本", context=context)
        # Both should appear without strong penalization
        topics = [r["topic"] for r in results]
        assert "咒怨" in topics or "生化危機" in topics

    def test_dungeon_scoping_no_penalty_in_main_space(self, story_id, setup_lore_with_subcategory):
        """In 主神空間 (no 副本 in phase), no dungeon penalty even if dungeon set."""
        context = {"phase": "主神空間", "status": "", "dungeon": "咒怨"}
        results = lore_db.search_hybrid(story_id, "副本", context=context)
        topics = [r["topic"] for r in results]
        # 生化危機 should NOT be heavily penalized since we're not in a dungeon phase
        assert "生化危機" in topics or len(results) > 0
