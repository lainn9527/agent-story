"""Shared helper imports and helper functions re-exported by app.py."""

import copy
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timezone

log = logging.getLogger("rpg")

from story_core.llm_bridge import call_claude_gm, call_claude_gm_stream, get_last_usage, get_provider
from story_core import usage_db
from story_core.event_db import (
    insert_event, search_relevant_events, get_events, get_event_by_id,
    update_event_status, search_events as search_events_db,
    get_active_events,
    copy_events_for_fork, merge_events_into, delete_events_for_branch,
)
from story_core.image_gen import generate_image_async, get_image_status, get_image_path
from story_core.lore_db import rebuild_index as rebuild_lore_index, search_relevant_lore, upsert_entry as upsert_lore_entry, get_toc as get_lore_toc, delete_entry as delete_lore_entry, get_entry_count, get_category_summary, get_embedding_stats, find_duplicates
from story_core.state_db import (
    rebuild_from_json as rebuild_state_db_from_json,
    search_state as search_state_entries,
    get_summary as get_state_summary,
    replace_categories_batch as replace_state_categories_batch,
    build_npc_content as build_state_npc_content,
    upsert_entry as upsert_state_entry,
    delete_entry as delete_state_entry,
)
from story_core.npc_evolution import should_run_evolution, run_npc_evolution_async, get_recent_activities, get_all_activities
from story_core.auto_summary import get_summaries
from story_core.dice import roll_fate, format_dice_context
from story_core.parser import parse_conversation, save_parsed
from story_core.prompts import SYSTEM_PROMPT_TEMPLATE, build_system_prompt
from story_core.compaction import (
    load_recap, save_recap, get_recap_text, should_compact, compact_async,
    get_context_window, copy_recap_to_branch, RECENT_WINDOW as RECENT_MESSAGE_COUNT,
)
from story_core.world_timer import process_time_tags, get_world_day, set_world_day, copy_world_day, advance_world_day, TIME_RE
from story_core.lore_organizer import (
    get_lore_lock, try_classify_topic, build_prefix_registry, invalidate_prefix_cache,
    should_organize, organize_lore_async,
)
from story_core.llm_trace import write_trace as write_llm_trace
from story_core.gm_cheats import (
    is_gm_command, apply_dice_command, get_dice_modifier, copy_cheats,
    get_dice_always_success, set_dice_always_success,
    get_fate_mode, set_fate_mode,
    get_pistol_mode, set_pistol_mode,
)
from story_core.dungeon_system import (
    ensure_dungeon_templates, initialize_dungeon_progress, archive_current_dungeon,
    update_dungeon_progress, update_dungeon_area, validate_dungeon_progression,
    build_dungeon_context, copy_dungeon_progress, get_dungeon_progress_snapshot,
    get_current_run_context, reconcile_dungeon_entry, reconcile_dungeon_exit,
    _load_dungeon_templates, _load_dungeon_template, _load_dungeon_progress,
    _parse_rank,
)
from story_core.npc_lifecycle import parse_npc_lifecycle_status
from story_core.character_state import *  # noqa: F401,F403
from story_core.npc_helpers import *  # noqa: F401,F403
from story_core.story_io import *  # noqa: F401,F403
from story_core.tag_extraction import *  # noqa: F401,F403
from story_core.branch_tree import *  # noqa: F401,F403
from story_core.lore_helpers import *  # noqa: F401,F403
from story_core.gm_plan import *  # noqa: F401,F403
from story_core.migrations import *  # noqa: F401,F403
from story_core.state_updates import *  # noqa: F401,F403
from story_core.debug_helpers import *  # noqa: F401,F403
from story_core.gm_pipeline import *  # noqa: F401,F403

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LLM_TRACE_ENABLED = os.environ.get("LLM_TRACE_ENABLED", "1").lower() not in {"0", "false", "off", "no"}
try:
    LLM_TRACE_RETENTION_DAYS = max(1, int(os.environ.get("LLM_TRACE_RETENTION_DAYS", "14")))
except ValueError:
    LLM_TRACE_RETENTION_DAYS = 14


def _log_llm_usage(story_id: str, call_type: str, elapsed_s: float, branch_id: str = "", usage: dict | None = None):
    """Log LLM usage from get_last_usage() or a streaming done payload's usage dict."""
    if usage is None:
        usage = get_last_usage()
    if usage is None:
        return
    try:
        usage_db.log_usage(
            story_id=story_id,
            provider=usage.get("provider", ""),
            model=usage.get("model", ""),
            call_type=call_type,
            prompt_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
            branch_id=branch_id,
            elapsed_ms=int(elapsed_s * 1000) if elapsed_s else None,
        )
    except Exception as e:
        log.debug("usage_db: failed to log — %s", e)


def _trace_llm(stage: str, story_id: str, branch_id: str = "",
               message_index: int | None = None, payload: dict | None = None,
               source: str = "", tags: dict | None = None):
    """Best-effort structured LLM trace write. Never raises."""
    if not LLM_TRACE_ENABLED:
        return
    try:
        write_llm_trace(
            data_dir=DATA_DIR,
            story_id=story_id,
            branch_id=branch_id,
            message_index=message_index,
            stage=stage,
            payload=payload or {},
            source=source,
            tags=tags or {},
            retention_days=LLM_TRACE_RETENTION_DAYS,
        )
    except Exception as e:
        log.debug("llm_trace: write failed — %s", e)


DEFAULT_CHARACTER_STATE = {
    "name": "Eddy",
    "current_phase": "主神空間",
    "gene_lock": "未開啟（進度 15%）",
    "physique": "普通人類（稍強）",
    "spirit": "普通人類（偏高）",
    "reward_points": 5000,
    "inventory": {"封印之鏡": "紀念品", "自省之鏡玉佩": "", "鎮魂符": "×3"},
    "completed_missions": ["咒怨 — 完美通關 8/8"],
    "relationships": {
        "小薇": "信任/曖昧",
        "阿豪": "兄弟情",
        "美玲": "好感",
        "Jack": "戰友",
        "小林": "崇拜",
        "佐藤神主": "約定",
    },
    "current_status": "即將返回主神空間，5000點待兌換",
}

DEFAULT_CHARACTER_SCHEMA = {
    "fields": [
        {"key": "name", "label": "姓名", "type": "text"},
        {"key": "current_phase", "label": "階段", "type": "text"},
        {"key": "gene_lock", "label": "基因鎖", "type": "text"},
        {"key": "physique", "label": "體質", "type": "text"},
        {"key": "spirit", "label": "精神力", "type": "text"},
        {"key": "reward_points", "label": "獎勵點", "type": "number", "highlight": True, "suffix": " 點"},
        {"key": "current_status", "label": "狀態", "type": "text"},
    ],
    "lists": [
        {"key": "inventory", "label": "道具欄", "type": "map"},
        {"key": "completed_missions", "label": "已完成任務", "state_add_key": "completed_missions_add"},
        {"key": "relationships", "label": "人際關係", "type": "map", "render": "inline"},
    ],
    "direct_overwrite_keys": ["gene_lock", "physique", "spirit", "current_status", "current_phase"],
}

VALID_PHASES = {"主神空間", "副本中", "副本結算", "傳送中", "死亡"}

# State validation gate mode: "off" | "warn" | "enforce"
# "off" = no validation, "warn" = validate + log but apply original, "enforce" = apply sanitized
STATE_REVIEW_MODE = os.environ.get("STATE_REVIEW_MODE", "enforce")

# LLM reviewer: only active when STATE_REVIEW_MODE=enforce and STATE_REVIEW_LLM=on
STATE_REVIEW_LLM = os.environ.get("STATE_REVIEW_LLM", "on")


def _parse_env_float(name: str, default: float) -> float:
    """Parse float env var with safe fallback."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("invalid %s=%r, using default %s", name, raw, default)
        return default


def _parse_env_int(name: str, default: int) -> int:
    """Parse int env var with safe fallback."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning("invalid %s=%r, using default %s", name, raw, default)
        return default

STATE_REVIEW_LLM_TIMEOUT = _parse_env_float("STATE_REVIEW_LLM_TIMEOUT_MS", 20000.0) / 1000
STATE_REVIEW_LLM_MAX_INFLIGHT = max(1, _parse_env_int("STATE_REVIEW_LLM_MAX_INFLIGHT", 4))
_STATE_REVIEW_LLM_SEM = threading.BoundedSemaphore(STATE_REVIEW_LLM_MAX_INFLIGHT)
STATE_RAG_TOKEN_BUDGET = max(200, _parse_env_int("STATE_RAG_TOKEN_BUDGET", 2000))
STATE_RAG_MAX_ITEMS = max(1, _parse_env_int("STATE_RAG_MAX_ITEMS", 30))
STATE_RAG_NPC_LIMIT = max(1, _parse_env_int("STATE_RAG_NPC_LIMIT", 10))

def _clear_loaded_save_preview(tree: dict) -> bool:
    """Clear loaded-save status preview metadata from timeline tree."""
    changed = False
    if tree.pop("loaded_save_id", None) is not None:
        changed = True
    if tree.pop("loaded_save_branch_id", None) is not None:
        changed = True
    return changed


def _get_loaded_save_preview(story_id: str, tree: dict, branch_id: str) -> dict | None:
    """Return loaded save entry when status should show save snapshot preview."""
    save_id = tree.get("loaded_save_id")
    if not save_id:
        return None

    active_branch_id = tree.get("active_branch_id", "main")
    if branch_id != active_branch_id:
        return None

    save_branch_id = tree.get("loaded_save_branch_id")
    if save_branch_id and save_branch_id != branch_id:
        return None

    saves = _load_json(_story_saves_path(story_id), [])
    save = next((s for s in saves if s.get("id") == save_id), None)
    if not save:
        return None
    if save.get("branch_id") != branch_id:
        return None
    return save

def _extract_state_must_include_keys(
    user_text: str,
    character_state: dict | None,
    npcs: list[dict] | None,
) -> list[str]:
    """Extract known entities mentioned in user text; force-include in state search."""
    text = user_text or ""
    if not text.strip():
        return []
    text_lower = text.lower()
    must = []
    seen = set()

    def _try_add(name: str):
        n = (name or "").strip()
        if not n or n in seen:
            return
        if len(n) < 2:
            return
        if n in text or n.lower() in text_lower:
            seen.add(n)
            must.append(n)

    if isinstance(character_state, dict):
        inv = character_state.get("inventory", {})
        if isinstance(inv, dict):
            for key in inv:
                _try_add(str(key))
        elif isinstance(inv, list):
            for item in inv:
                if isinstance(item, str):
                    _try_add(_extract_item_base_name(item))

        abilities = character_state.get("abilities", [])
        if isinstance(abilities, list):
            for item in abilities:
                if isinstance(item, str):
                    _try_add(item)

        rels = character_state.get("relationships", {})
        if isinstance(rels, dict):
            for key in rels:
                _try_add(str(key))

    if isinstance(npcs, list):
        for npc in npcs:
            if isinstance(npc, dict):
                _try_add(str(npc.get("name", "")))

    return must


def _story_saves_path(story_id: str) -> str:
    return os.path.join(_story_dir(story_id), "saves.json")


# ---------------------------------------------------------------------------
# Auto-prune abandoned sibling branches
# ---------------------------------------------------------------------------

PRUNE_DEPTH_THRESHOLD = 5
PRUNE_MAX_DELTA_MSGS = 2


def _auto_prune_siblings(story_id: str, branch_id: str, current_msg_index: int) -> list[str]:
    """Auto-prune abandoned sibling branches that the player has moved past.

    A branch is pruned when ALL of these conditions are met:
    - Its parent is in the current branch's ancestor chain (it's a sibling)
    - It is NOT in the ancestor chain itself (not an ancestor)
    - No pruned/deleted/merged/blank/protected flags
    - Not main, not auto_ prefix
    - Player has moved ≥ PRUNE_DEPTH_THRESHOLD steps past the branch point
    - Branch has ≤ PRUNE_MAX_DELTA_MSGS delta messages (abandoned attempt)
    - Branch has no active children (nobody forked from it)

    Returns list of pruned branch IDs.
    """
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})

    # Build ancestor chain for current branch
    ancestor_set = set()
    cur = branch_id
    while cur is not None and cur not in ancestor_set:
        ancestor_set.add(cur)
        b = branches.get(cur)
        if not b:
            break
        cur = b.get("parent_branch_id")

    # Build set of branches that have active children (not pruned/deleted/merged)
    has_active_children = set()
    for b in branches.values():
        pid = b.get("parent_branch_id")
        if pid and not b.get("pruned") and not b.get("deleted") and not b.get("merged") and not b.get("blank"):
            has_active_children.add(pid)

    pruned = []
    for bid, b in branches.items():
        # Skip if already handled or special
        if bid == "main" or bid.startswith("auto_"):
            continue
        if b.get("pruned") or b.get("deleted") or b.get("merged") or b.get("blank") or b.get("protected"):
            continue
        # Must be a sibling (parent in ancestor chain) but not an ancestor itself
        parent_id = b.get("parent_branch_id")
        if parent_id not in ancestor_set or bid in ancestor_set:
            continue
        # Player must have moved past the branch point
        bp_index = b.get("branch_point_index")
        if bp_index is None or (current_msg_index - bp_index) < PRUNE_DEPTH_THRESHOLD:
            continue
        # Must be a short/abandoned branch
        delta_msgs = _load_branch_messages(story_id, bid)
        if len(delta_msgs) > PRUNE_MAX_DELTA_MSGS:
            continue
        # Must not have active children
        if bid in has_active_children:
            continue

        # All conditions met — prune it
        b["pruned"] = True
        b["pruned_at"] = datetime.now(timezone.utc).isoformat()
        pruned.append(bid)

    if pruned:
        _save_tree(story_id, tree)
        log.info("Auto-pruned %d sibling branches: %s", len(pruned), pruned)

    return pruned


# ---------------------------------------------------------------------------
# Helpers — unified tag extraction & context injection
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Branch API
# ---------------------------------------------------------------------------



def _cleanup_branch(story_id, branch_id):
    """Remove a failed branch (no cascade, just this one)."""
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    if branch_id not in branches:
        return
    parent = branches[branch_id].get("parent_branch_id", "main")
    del branches[branch_id]
    if tree.get("active_branch_id") == branch_id:
        tree["active_branch_id"] = parent
    _save_tree(story_id, tree)
    delete_events_for_branch(story_id, branch_id)
    bdir = _branch_dir(story_id, branch_id)
    if os.path.isdir(bdir):
        shutil.rmtree(bdir)


# Story CRUD API
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Debug Panel API
# ---------------------------------------------------------------------------

__all__ = [name for name in globals() if not name.startswith("__")]
