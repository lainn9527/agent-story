"""Shared test fixtures for Story RPG tests."""

import json
import os
import shutil

import pytest


# ---------------------------------------------------------------------------
# Sample data constants
# ---------------------------------------------------------------------------

SAMPLE_STORY_ID = "test_story"

SAMPLE_CHARACTER_SCHEMA = {
    "name": {"type": "text", "label": "名稱"},
    "current_phase": {"type": "text", "label": "階段"},
    "reward_points": {"type": "number", "label": "獎勵點"},
    "inventory": {"type": "list", "label": "道具"},
    "relationships": {"type": "map", "label": "關係"},
    "completed_missions": {"type": "list", "label": "已完成副本"},
    "gene_lock": {"type": "text", "label": "基因鎖"},
    "physique": {"type": "text", "label": "體質"},
    "spirit": {"type": "text", "label": "精神力"},
    "current_status": {"type": "text", "label": "當前狀態"},
}

SAMPLE_CHARACTER_STATE = {
    "name": "測試者",
    "current_phase": "主神空間",
    "reward_points": 5000,
    "inventory": ["封印之鏡", "鎮魂符×5"],
    "relationships": {"小薇": "信任"},
    "completed_missions": ["咒怨 — 完美通關"],
    "gene_lock": "未開啟",
    "physique": "普通人",
    "spirit": "普通人",
    "current_status": "等待任務",
}

SAMPLE_LORE_ENTRIES = [
    {
        "category": "主神設定與規則",
        "topic": "空間概述",
        "content": "主神空間是一個超維度存在，收集各個世界的靈魂進行試煉。輪迴者必須在副本世界中完成任務才能獲得獎勵點。[tag: 主神/空間]",
    },
    {
        "category": "主神設定與規則",
        "topic": "任務規則",
        "content": "每次副本任務限時七天（副本內時間），完成度決定獎勵點數量。死亡者可能被復活但需要消耗大量獎勵點。[tag: 任務/規則]",
    },
    {
        "category": "體系",
        "topic": "基因鎖",
        "content": "基因鎖是人類潛能的封印，開啟後可大幅提升身體素質和精神力。共有五個階段，每個階段需要在極端壓力下突破。[tag: 體系/基因鎖]",
    },
    {
        "category": "體系",
        "topic": "修真",
        "content": "修真體系包含煉氣、築基、金丹、元嬰等境界。修真者可操控靈力進行攻擊和防禦，是最全面的戰鬥體系之一。[tag: 體系/修真]",
    },
    {
        "category": "體系",
        "topic": "鬥氣",
        "content": "鬥氣是通過鍛鍊體內生命能量形成的戰鬥力量。分為初級、中級、高級、超級四個等級，擅長近戰和體術。[tag: 體系/鬥氣]",
    },
    {
        "category": "副本世界觀",
        "topic": "咒怨",
        "content": "咒怨副本基於日本恐怖電影世界觀。伽椰子是核心怨靈，進入咒怨之屋的人會被詛咒。副本目標是生存七天或消滅怨靈。[tag: 副本/咒怨]",
    },
    {
        "category": "副本世界觀",
        "topic": "生化危機",
        "content": "生化危機副本基於遊戲世界觀。T病毒爆發，殭屍橫行。副本目標是逃出拉坤市或找到解藥。[tag: 副本/生化危機]",
    },
    {
        "category": "商城",
        "topic": "價目表",
        "content": "主神空間商城提供各類道具和強化服務。基因鎖開啟：5000點，體質強化：2000點起，武器：500-10000點不等。[tag: 商城/價格]",
    },
    {
        "category": "場景",
        "topic": "主神空間設施",
        "content": "主神空間包含休息區、訓練場、商城兌換大廳、任務大廳、醫療區等設施。輪迴者在副本間休息和準備。[tag: 場景/空間]",
    },
    {
        "category": "NPC",
        "topic": "隊友資料",
        "content": "阿豪：體格強壯的格鬥家，性格暴躁但重義氣。小薇：擁有精神力天賦的少女，冷靜理性。[tag: NPC/隊友]",
    },
]

SAMPLE_SYSTEM_PROMPT = """你是一個 RPG 遊戲的 GM（遊戲主持人）。

## 角色狀態
{character_state}

## 敘事回顧
{narrative_recap}

## 世界設定
{world_lore}

## NPC 檔案
{npc_profiles}
"""

SAMPLE_NPCS = [
    {
        "id": "npc_阿豪",
        "name": "阿豪",
        "role": "隊友",
        "appearance": "光頭壯漢，有龍形刺青",
        "personality": {
            "openness": 5,
            "conscientiousness": 4,
            "extraversion": 8,
            "agreeableness": 4,
            "neuroticism": 7,
            "summary": "粗獷直爽的硬漢",
        },
        "relationship_to_player": "兄弟",
        "current_status": "休息中",
        "notable_traits": ["體格強壯"],
        "backstory": "街頭長大的格鬥家",
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def story_dir(tmp_path):
    """Create a minimal story directory with all required files.

    Returns (stories_dir, story_id) — stories_dir is the parent that modules
    can use as STORIES_DIR.

    Design files (system_prompt, schema, lore, etc.) are written to
    story_design/<story_id>/ matching the production layout.
    """
    stories_dir = tmp_path / "data" / "stories"
    story_path = stories_dir / SAMPLE_STORY_ID
    story_path.mkdir(parents=True)

    # Design files directory
    design_path = tmp_path / "story_design" / SAMPLE_STORY_ID
    design_path.mkdir(parents=True)

    # branches/main
    main_branch = story_path / "branches" / "main"
    main_branch.mkdir(parents=True)

    # Write design files to story_design/
    (design_path / "system_prompt.txt").write_text(SAMPLE_SYSTEM_PROMPT, encoding="utf-8")
    (design_path / "character_schema.json").write_text(
        json.dumps(SAMPLE_CHARACTER_SCHEMA, ensure_ascii=False), encoding="utf-8"
    )
    (design_path / "default_character_state.json").write_text(
        json.dumps(SAMPLE_CHARACTER_STATE, ensure_ascii=False), encoding="utf-8"
    )
    (design_path / "world_lore.json").write_text(
        json.dumps(SAMPLE_LORE_ENTRIES, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Per-branch files (runtime data stays in data/stories/)
    (main_branch / "messages.json").write_text("[]", encoding="utf-8")
    (main_branch / "character_state.json").write_text(
        json.dumps(SAMPLE_CHARACTER_STATE, ensure_ascii=False), encoding="utf-8"
    )
    (main_branch / "npcs.json").write_text(
        json.dumps(SAMPLE_NPCS, ensure_ascii=False), encoding="utf-8"
    )
    (main_branch / "world_day.json").write_text(
        json.dumps({"world_day": 0, "last_updated": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )

    # stories.json
    stories_json = tmp_path / "data" / "stories.json"
    stories_json.write_text(
        json.dumps({"active_story_id": SAMPLE_STORY_ID, "stories": {SAMPLE_STORY_ID: {"id": SAMPLE_STORY_ID, "name": "測試故事"}}}),
        encoding="utf-8",
    )

    return stories_dir, SAMPLE_STORY_ID


@pytest.fixture
def story_id():
    """Just the test story ID constant."""
    return SAMPLE_STORY_ID


@pytest.fixture
def sample_timeline_tree():
    """A multi-branch timeline tree for branch logic tests.

    Structure:
      main (root)
        ├── branch_a (fork at index 5)
        │   └── branch_c (fork at index 8)
        ├── branch_b (fork at index 5, sibling of branch_a)
        └── branch_blank (blank, branch_point_index=-1)
    """
    return {
        "active_branch_id": "main",
        "branches": {
            "main": {
                "id": "main",
                "name": "主時間線",
                "parent_branch_id": None,
                "branch_point_index": None,
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            "branch_a": {
                "id": "branch_a",
                "name": "分支A",
                "parent_branch_id": "main",
                "branch_point_index": 5,
                "created_at": "2026-01-02T00:00:00+00:00",
            },
            "branch_b": {
                "id": "branch_b",
                "name": "分支B",
                "parent_branch_id": "main",
                "branch_point_index": 5,
                "created_at": "2026-01-02T01:00:00+00:00",
            },
            "branch_c": {
                "id": "branch_c",
                "name": "分支C",
                "parent_branch_id": "branch_a",
                "branch_point_index": 8,
                "created_at": "2026-01-03T00:00:00+00:00",
            },
            "branch_blank": {
                "id": "branch_blank",
                "name": "空白分支",
                "parent_branch_id": "main",
                "branch_point_index": -1,
                "created_at": "2026-01-04T00:00:00+00:00",
                "blank": True,
            },
        },
    }
