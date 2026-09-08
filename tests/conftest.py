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

SAMPLE_CHARACTER_STATE = {}

SAMPLE_LORE_ENTRIES = []

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


def _patch_paths_all_modules(monkeypatch, tmp_path, stories_dir, design_dir, app_module=None):
    """Patch shared path constants across app and extracted helper modules."""
    import app as imported_app_module
    from story_core import app_helpers
    from story_core import event_db
    from story_core import lore_db
    from story_core import migrations
    from story_core import state_db
    from story_core import story_io

    app_target = app_module or imported_app_module
    data_dir = tmp_path / "data"
    patches = (
        ("BASE_DIR", str(tmp_path)),
        ("DATA_DIR", str(data_dir)),
        ("STORIES_DIR", str(stories_dir)),
        ("STORY_DESIGN_DIR", str(design_dir)),
        ("STORIES_REGISTRY_PATH", str(data_dir / "stories.json")),
        ("_LLM_CONFIG_PATH", str(tmp_path / "llm_config.json")),
    )
    for mod in (app_target, app_helpers, story_io, migrations, event_db, lore_db, state_db):
        for attr, value in patches:
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, value)
    lore_db._embedding_cache.clear()


@pytest.fixture
def patch_paths_all_modules():
    return _patch_paths_all_modules


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
