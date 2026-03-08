import json

import app as app_module
from story_core import dungeon_system
from story_core import event_db
from story_core import llm_bridge
from story_core import state_cleanup
from story_core import state_db
from story_core import world_timer


STORY_ID = "test_story"
BRANCH_ID = "main"

SCHEMA = {
    "fields": [
        {"key": "current_phase", "type": "text"},
        {"key": "current_status", "type": "text"},
        {"key": "current_dungeon", "type": "text"},
        {"key": "reward_points", "type": "number"},
        {"key": "gene_lock", "type": "text"},
        {"key": "等級", "type": "text"},
    ],
    "lists": [
        {"key": "inventory", "type": "map"},
        {"key": "relationships", "type": "map"},
        {"key": "abilities", "state_add_key": "abilities_add", "state_remove_key": "abilities_remove"},
        {"key": "systems", "type": "map"},
    ],
    "direct_overwrite_keys": [
        "current_phase",
        "current_status",
        "current_dungeon",
        "gene_lock",
        "等級",
    ],
}

TEMPLATES = {
    "dungeons": [
        {
            "id": "alien",
            "name": "異形",
            "difficulty": "B",
            "mainline": {"nodes": [{"id": "node_1", "title": "發現異形"}]},
            "areas": [{"id": "cargo_bay", "name": "貨艙", "initial_status": "discovered"}],
            "progression_rules": {
                "rank_progress": 0.5,
                "gene_lock_gain": 10,
                "gene_lock_stage_cap": "第一階 50%",
                "base_reward": 1000,
                "mainline_multiplier": 1.5,
                "exploration_multiplier": 1.2,
            },
        },
        {
            "id": "naruto",
            "name": "火影忍者",
            "difficulty": "C",
            "mainline": {"nodes": [{"id": "node_1", "title": "進入木葉"}]},
            "areas": [{"id": "konoha_gate", "name": "木葉大門", "initial_status": "discovered"}],
            "progression_rules": {
                "rank_progress": 0.5,
                "gene_lock_gain": 10,
                "gene_lock_stage_cap": "第一階 50%",
                "base_reward": 1000,
                "mainline_multiplier": 1.5,
                "exploration_multiplier": 1.2,
            },
        },
    ]
}


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _branch_dir(tmp_path):
    return tmp_path / "data" / "stories" / STORY_ID / "branches" / BRANCH_ID


def _story_dir(tmp_path):
    return tmp_path / "data" / "stories" / STORY_ID


def _design_dir(tmp_path):
    return tmp_path / "story_design" / STORY_ID


def _load_state(tmp_path):
    path = _branch_dir(tmp_path) / "character_state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _load_progress(tmp_path):
    path = _branch_dir(tmp_path) / "dungeon_progress.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _load_recall_memory(tmp_path):
    path = _branch_dir(tmp_path) / "dungeon_return_memory.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _base_state(**overrides):
    state = {
        "current_phase": "主神空間",
        "current_status": "待命",
        "current_dungeon": "",
        "reward_points": 100,
        "gene_lock": "未開啟",
        "等級": "D",
        "inventory": {},
        "relationships": {},
        "abilities": [],
        "systems": {},
    }
    state.update(overrides)
    return state


def _active_progress(dungeon_id="alien", **overrides):
    progress = {
        "history": [],
        "total_dungeons_completed": 0,
        "current_dungeon": {
            "dungeon_id": dungeon_id,
            "entered_at": "2026-03-07T00:00:00+00:00",
            "entered_at_world_day": 1,
            "current_world_day": 1,
            "mainline_progress": 50,
            "exploration_progress": 30,
            "completed_nodes": [],
            "discovered_areas": ["cargo_bay"],
            "explored_areas": {"cargo_bay": 30},
            "rank_on_enter": "D",
            "gene_lock_on_enter": "未開啟",
            "reward_points_on_enter": 100,
            "growth_budget": {
                "max_rank_progress": 0.5,
                "consumed_rank_progress": 0,
                "max_gene_lock_gain": 10,
                "consumed_gene_lock": 0,
            },
        },
    }
    progress["current_dungeon"].update(overrides)
    return progress


class _InlineThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


def _identity_gate(update, *_args, **_kwargs):
    return update


def _noop(*_args, **_kwargs):
    return None


import pytest


@pytest.fixture(autouse=True)
def patch_paths(tmp_path, monkeypatch, patch_paths_all_modules):
    stories_dir = tmp_path / "data" / "stories"
    design_dir = tmp_path / "story_design"
    stories_dir.mkdir(parents=True)
    design_dir.mkdir(parents=True)

    patch_paths_all_modules(monkeypatch, tmp_path, stories_dir, design_dir, app_module=app_module)
    monkeypatch.setattr(world_timer, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(
        dungeon_system,
        "_story_dir",
        lambda story_id: str(stories_dir / story_id),
    )
    monkeypatch.setattr(
        dungeon_system,
        "_branch_dir",
        lambda story_id, branch_id: str(stories_dir / story_id / "branches" / branch_id),
    )
    monkeypatch.setattr(state_cleanup, "get_recap_text", lambda *_args, **_kwargs: "")

    _write_json(_design_dir(tmp_path) / "character_schema.json", SCHEMA)
    _write_json(_story_dir(tmp_path) / "dungeons_template.json", TEMPLATES)
    _write_json(_branch_dir(tmp_path) / "npcs.json", [])

    return tmp_path


def test_reconcile_entry_uses_pre_transition_baseline(tmp_path):
    _write_json(_branch_dir(tmp_path) / "character_state.json", _base_state())

    old_state = _base_state(reward_points=100, gene_lock="未開啟", 等級="D")
    new_state = _base_state(
        current_dungeon="火影忍者",
        current_phase="副本中",
        reward_points=240,
        gene_lock="第一階開啟中（進度 18%）",
        等級="C",
    )

    dungeon_system.reconcile_dungeon_entry(STORY_ID, BRANCH_ID, old_state, new_state)

    progress = _load_progress(tmp_path)
    current = progress["current_dungeon"]
    assert current["dungeon_id"] == "naruto"
    assert current["rank_on_enter"] == "D"
    assert current["gene_lock_on_enter"] == "未開啟"
    assert current["reward_points_on_enter"] == 100


def test_get_current_run_context_returns_none_without_active_dungeon(tmp_path):
    _write_json(_branch_dir(tmp_path) / "character_state.json", _base_state())

    assert dungeon_system.get_current_run_context(STORY_ID, BRANCH_ID) is None


def test_get_current_run_context_returns_dungeon_id_and_run_id(tmp_path):
    _write_json(_branch_dir(tmp_path) / "character_state.json", _base_state(current_dungeon="異形"))
    _write_json(_branch_dir(tmp_path) / "dungeon_progress.json", _active_progress())

    run_ctx = dungeon_system.get_current_run_context(STORY_ID, BRANCH_ID)

    assert run_ctx == {
        "dungeon_id": "alien",
        "run_id": "2026-03-07T00:00:00+00:00",
    }


def test_reconcile_exit_archives_with_new_state_and_initializes_switch(tmp_path):
    _write_json(_branch_dir(tmp_path) / "character_state.json", _base_state(current_dungeon="異形"))
    _write_json(_branch_dir(tmp_path) / "dungeon_progress.json", _active_progress())

    old_state = _base_state(current_dungeon="異形", reward_points=100, 等級="D")
    new_state = _base_state(current_dungeon="火影忍者", reward_points=250, 等級="C")

    dungeon_system.reconcile_dungeon_exit(STORY_ID, BRANCH_ID, old_state, new_state)

    progress = _load_progress(tmp_path)
    assert progress["current_dungeon"]["dungeon_id"] == "naruto"
    assert progress["current_dungeon"]["reward_points_on_enter"] == 250
    assert progress["history"][0]["dungeon_id"] == "alien"
    assert progress["history"][0]["rank_after"] == "C"
    assert progress["history"][0]["reward_points_earned"] == 150


def test_reconcile_entry_sets_pending_only_on_reentry(tmp_path):
    _write_json(_branch_dir(tmp_path) / "character_state.json", _base_state())

    dungeon_system.reconcile_dungeon_entry(
        STORY_ID,
        BRANCH_ID,
        _base_state(current_dungeon=""),
        _base_state(current_dungeon="火影忍者", current_phase="副本中"),
    )
    memory = _load_recall_memory(tmp_path)
    assert memory["visited_dungeons"] == ["火影忍者"]
    assert memory["pending_reentry_dungeon"] is None

    dungeon_system.reconcile_dungeon_exit(
        STORY_ID,
        BRANCH_ID,
        _base_state(current_dungeon="火影忍者", current_phase="副本中"),
        _base_state(current_dungeon="", current_phase="主神空間"),
    )
    dungeon_system.reconcile_dungeon_entry(
        STORY_ID,
        BRANCH_ID,
        _base_state(current_dungeon="", current_phase="主神空間"),
        _base_state(current_dungeon="火影忍者", current_phase="副本中"),
    )
    memory = _load_recall_memory(tmp_path)
    assert memory["visited_dungeons"] == ["火影忍者"]
    assert memory["pending_reentry_dungeon"] == "火影忍者"


def test_reconcile_exit_marks_local_archived_npc_eligible_and_tracks_next_dungeon(tmp_path):
    _write_json(_branch_dir(tmp_path) / "character_state.json", _base_state(current_dungeon="異形"))
    _write_json(_branch_dir(tmp_path) / "dungeon_progress.json", _active_progress())
    _write_json(
        _branch_dir(tmp_path) / "npcs.json",
        [
            {
                "name": "老林",
                "role": "在地嚮導",
                "lifecycle_status": "archived",
                "archive_kind": "offstage",
                "archived_reason": "留在原地守望",
                "home_scope": "dungeon_local",
                "home_dungeon": "異形",
            }
        ],
    )

    old_state = _base_state(current_dungeon="異形", current_phase="副本中")
    new_state = _base_state(current_dungeon="火影忍者", current_phase="副本中")
    dungeon_system.reconcile_dungeon_exit(STORY_ID, BRANCH_ID, old_state, new_state)

    npcs = json.loads((_branch_dir(tmp_path) / "npcs.json").read_text(encoding="utf-8"))
    assert npcs[0]["return_recall_state"] == "eligible"

    memory = _load_recall_memory(tmp_path)
    assert memory["visited_dungeons"] == ["火影忍者"]
    assert memory["pending_reentry_dungeon"] is None


def test_apply_state_update_initializes_progress_before_validation(tmp_path, monkeypatch):
    _write_json(_branch_dir(tmp_path) / "character_state.json", _base_state())
    monkeypatch.setattr(app_module, "_run_state_gate", _identity_gate)
    monkeypatch.setattr(app_module, "_normalize_state_async", _noop)

    app_module._apply_state_update(
        STORY_ID,
        BRANCH_ID,
        {
            "current_dungeon": "火影忍者",
            "current_phase": "副本中",
            "gene_lock": "第一階開啟中（進度 18%）",
        },
    )

    state = _load_state(tmp_path)
    progress = _load_progress(tmp_path)
    assert state["gene_lock"] == "第一階開啟中（進度 10%）"
    assert progress["current_dungeon"]["dungeon_id"] == "naruto"
    assert progress["current_dungeon"]["gene_lock_on_enter"] == "未開啟"


def test_apply_state_update_archives_after_validation_with_capped_state(tmp_path, monkeypatch):
    _write_json(
        _branch_dir(tmp_path) / "character_state.json",
        _base_state(current_dungeon="異形", current_phase="副本中"),
    )
    _write_json(_branch_dir(tmp_path) / "dungeon_progress.json", _active_progress())
    monkeypatch.setattr(app_module, "_run_state_gate", _identity_gate)
    monkeypatch.setattr(app_module, "_normalize_state_async", _noop)

    app_module._apply_state_update(
        STORY_ID,
        BRANCH_ID,
        {
            "current_dungeon": "",
            "current_phase": "主神空間",
            "gene_lock": "第一階開啟中（進度 18%）",
        },
    )

    state = _load_state(tmp_path)
    progress = _load_progress(tmp_path)
    assert state["gene_lock"] == "第一階開啟中（進度 10%）"
    assert progress.get("current_dungeon") is None
    assert progress["history"][0]["gene_lock_after"] == "第一階開啟中（進度 10%）"


def test_normalize_reconciles_after_reapply(tmp_path, monkeypatch):
    _write_json(_branch_dir(tmp_path) / "character_state.json", _base_state())
    monkeypatch.setattr(app_module, "_run_state_gate", _identity_gate)
    monkeypatch.setattr(app_module, "_trace_llm", _noop)
    monkeypatch.setattr(app_module, "_log_llm_usage", _noop)
    monkeypatch.setattr(app_module.threading, "Thread", _InlineThread)
    monkeypatch.setattr(
        llm_bridge,
        "call_oneshot",
        lambda _prompt: json.dumps(
            {"current_dungeon": "火影忍者", "current_phase": "副本中"},
            ensure_ascii=False,
        ),
    )

    app_module._normalize_state_async(STORY_ID, BRANCH_ID, {"副本名": "火影忍者"}, {"current_dungeon"})

    progress = _load_progress(tmp_path)
    assert progress["current_dungeon"]["dungeon_id"] == "naruto"


def test_cleanup_reconciles_after_cleanup_batch(tmp_path, monkeypatch):
    _write_json(_branch_dir(tmp_path) / "character_state.json", _base_state())
    monkeypatch.setattr(app_module, "_trace_llm", _noop)
    monkeypatch.setattr(app_module, "_log_llm_usage", _noop)
    monkeypatch.setattr(
        llm_bridge,
        "call_oneshot",
        lambda _prompt: json.dumps(
            {
                "archive_npcs": [],
                "merge_npcs": [],
                "resolve_events": [],
                "remove_inventory": [],
                "remove_abilities": [],
                "add_abilities": [],
                "update_systems": [],
                "clean_relationships": [],
            },
            ensure_ascii=False,
        ),
    )

    def _fake_apply_cleanup(story_id, branch_id, _ops):
        schema = app_module._load_character_schema(story_id)
        app_module._apply_state_update_inner(
            story_id,
            branch_id,
            {"current_dungeon": "火影忍者", "current_phase": "副本中"},
            schema,
        )
        return {
            "archived_npcs": 0,
            "merged_npcs": 0,
            "resolved_events": 0,
            "removed_inventory": 0,
            "removed_abilities": 0,
            "added_abilities": 0,
            "updated_systems": 0,
            "clean_relationships": 0,
        }

    monkeypatch.setattr(state_cleanup, "_apply_cleanup_operations", _fake_apply_cleanup)

    state_cleanup._run_cleanup_core(STORY_ID, BRANCH_ID)

    progress = _load_progress(tmp_path)
    assert progress["current_dungeon"]["dungeon_id"] == "naruto"
