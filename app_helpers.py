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

from llm_bridge import call_claude_gm, call_claude_gm_stream, get_last_usage, get_provider
import usage_db
from event_db import (
    insert_event, search_relevant_events, get_events, get_event_by_id,
    update_event_status, search_events as search_events_db,
    get_active_events,
    copy_events_for_fork, merge_events_into, delete_events_for_branch,
)
from image_gen import generate_image_async, get_image_status, get_image_path
from lore_db import rebuild_index as rebuild_lore_index, search_relevant_lore, upsert_entry as upsert_lore_entry, get_toc as get_lore_toc, delete_entry as delete_lore_entry, get_entry_count, get_category_summary, get_embedding_stats, find_duplicates
from state_db import (
    rebuild_from_json as rebuild_state_db_from_json,
    search_state as search_state_entries,
    get_summary as get_state_summary,
    replace_categories_batch as replace_state_categories_batch,
    build_npc_content as build_state_npc_content,
    upsert_entry as upsert_state_entry,
    delete_entry as delete_state_entry,
)
from npc_evolution import should_run_evolution, run_npc_evolution_async, get_recent_activities, get_all_activities
from auto_summary import get_summaries
from dice import roll_fate, format_dice_context
from parser import parse_conversation, save_parsed
from prompts import SYSTEM_PROMPT_TEMPLATE, build_system_prompt
from compaction import (
    load_recap, save_recap, get_recap_text, should_compact, compact_async,
    get_context_window, copy_recap_to_branch, RECENT_WINDOW as RECENT_MESSAGE_COUNT,
)
from world_timer import process_time_tags, get_world_day, set_world_day, copy_world_day, advance_world_day, TIME_RE
from lore_organizer import (
    get_lore_lock, try_classify_topic, build_prefix_registry, invalidate_prefix_cache,
    should_organize, organize_lore_async,
)
from llm_trace import write_trace as write_llm_trace
from gm_cheats import (
    is_gm_command, apply_dice_command, get_dice_modifier, copy_cheats,
    get_dice_always_success, set_dice_always_success,
    get_fate_mode, set_fate_mode,
    get_pistol_mode, set_pistol_mode,
)
from dungeon_system import (
    ensure_dungeon_templates, initialize_dungeon_progress, archive_current_dungeon,
    update_dungeon_progress, update_dungeon_area, validate_dungeon_progression,
    build_dungeon_context, copy_dungeon_progress, get_dungeon_progress_snapshot,
    get_current_run_context, reconcile_dungeon_entry, reconcile_dungeon_exit,
    _load_dungeon_templates, _load_dungeon_template, _load_dungeon_progress,
    _parse_rank,
)
from npc_lifecycle import parse_npc_lifecycle_status
from character_state import *  # noqa: F401,F403
from npc_helpers import *  # noqa: F401,F403
from story_io import *  # noqa: F401,F403
from tag_extraction import *  # noqa: F401,F403
from branch_tree import *  # noqa: F401,F403
from lore_helpers import *  # noqa: F401,F403
from gm_pipeline import *  # noqa: F401,F403

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LLM_TRACE_ENABLED = os.environ.get("LLM_TRACE_ENABLED", "1").lower() not in {"0", "false", "off", "no"}
try:
    LLM_TRACE_RETENTION_DAYS = max(1, int(os.environ.get("LLM_TRACE_RETENTION_DAYS", "14")))
except ValueError:
    LLM_TRACE_RETENTION_DAYS = 14
# Legacy paths — used only during migration
CONVERSATION_PATH = os.path.join(BASE_DIR, "Grok_conversation.md")
LEGACY_PARSED_PATH = os.path.join(DATA_DIR, "parsed_conversation.json")
LEGACY_TREE_PATH = os.path.join(DATA_DIR, "timeline_tree.json")
LEGACY_CHARACTER_STATE_PATH = os.path.join(DATA_DIR, "character_state.json")
LEGACY_NEW_MESSAGES_PATH = os.path.join(DATA_DIR, "new_messages.json")


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


def _is_numeric_value(value: object) -> bool:
    """True for int/float but not bool (bool is a subclass of int in Python)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


STATE_REVIEW_LLM_TIMEOUT = _parse_env_float("STATE_REVIEW_LLM_TIMEOUT_MS", 20000.0) / 1000
STATE_REVIEW_LLM_MAX_INFLIGHT = max(1, _parse_env_int("STATE_REVIEW_LLM_MAX_INFLIGHT", 4))
_STATE_REVIEW_LLM_SEM = threading.BoundedSemaphore(STATE_REVIEW_LLM_MAX_INFLIGHT)
STATE_RAG_TOKEN_BUDGET = max(200, _parse_env_int("STATE_RAG_TOKEN_BUDGET", 2000))
STATE_RAG_MAX_ITEMS = max(1, _parse_env_int("STATE_RAG_MAX_ITEMS", 30))
STATE_RAG_NPC_LIMIT = max(1, _parse_env_int("STATE_RAG_NPC_LIMIT", 10))
GM_PLAN_CHAR_LIMIT = max(120, _parse_env_int("GM_PLAN_CHAR_LIMIT", 500))
DEBUG_CHAT_CONTEXT_COUNT = max(1, _parse_env_int("DEBUG_CHAT_CONTEXT_COUNT", 20))
DEBUG_CHAT_MAX_USER_CHARS = max(200, _parse_env_int("DEBUG_CHAT_MAX_USER_CHARS", 4000))
DEBUG_APPLY_MAX_ACTIONS = max(1, _parse_env_int("DEBUG_APPLY_MAX_ACTIONS", 20))
DEBUG_APPLY_MAX_DIRECTIVES = max(1, _parse_env_int("DEBUG_APPLY_MAX_DIRECTIVES", 20))

# Scene-transient keys that should never be persisted (used by validation gate + inner)
_SCENE_KEYS = {
    "location", "location_update", "location_details",
    "threat_level", "combat_status", "escape_options", "escape_route",
    "noise_level", "facility_status", "npc_status", "weapons_status",
    "tool_status", "available_escape", "available_locations",
    "status_update", "current_predicament",
}
# LLM intermediate instruction keys that leak into state
_INSTRUCTION_KEYS = {
    "inventory_use", "inventory_update", "skill_update",
    "status_change", "state_change", "note", "notes",
}

def _resolve_debug_unit_id(story_id: str, branch_id: str) -> str:
    """Resolve debug-unit id from branch ancestry.

    Walk parent_branch_id upward and use the top-most blank ancestor as unit id.
    If no blank ancestor exists, fallback to current branch_id.
    """
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    if branch_id not in branches:
        return branch_id

    cur = branch_id
    visited = set()
    top_blank_id: str | None = None
    while cur is not None and cur not in visited:
        visited.add(cur)
        b = branches.get(cur)
        if not b:
            break
        if b.get("blank"):
            top_blank_id = b.get("id") or cur
        cur = b.get("parent_branch_id")

    return top_blank_id or branch_id


def _load_debug_chat(story_id: str, debug_unit_id: str) -> list[dict]:
    data = _load_json(_debug_chat_path(story_id, debug_unit_id), [])
    if not isinstance(data, list):
        return []
    cleaned: list[dict] = []
    for m in data:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = str(m.get("content", "")).strip()
        if not content:
            continue
        cleaned.append({
            "role": role,
            "content": content,
            "created_at": m.get("created_at"),
        })
    return cleaned


def _save_debug_chat(story_id: str, debug_unit_id: str, messages: list[dict]):
    _save_json(_debug_chat_path(story_id, debug_unit_id), messages)


def _append_debug_chat_message(story_id: str, debug_unit_id: str, role: str, content: str):
    if role not in {"user", "assistant"}:
        return
    text = str(content or "").strip()
    if not text:
        return
    chat = _load_debug_chat(story_id, debug_unit_id)
    chat.append({
        "role": role,
        "content": text,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_debug_chat(story_id, debug_unit_id, chat)


def _load_last_apply_backup(story_id: str, debug_unit_id: str) -> dict:
    data = _load_json(_last_apply_backup_path(story_id, debug_unit_id), {})
    return data if isinstance(data, dict) else {}


def _save_last_apply_backup(story_id: str, debug_unit_id: str, backup: dict):
    _save_json(_last_apply_backup_path(story_id, debug_unit_id), backup)


def _clear_last_apply_backup(story_id: str, debug_unit_id: str):
    path = _last_apply_backup_path(story_id, debug_unit_id)
    if os.path.exists(path):
        os.remove(path)


def _load_debug_directive(story_id: str, branch_id: str) -> dict:
    data = _load_json(_debug_directive_path(story_id, branch_id), {})
    if not isinstance(data, dict):
        return {}
    instruction = str(data.get("instruction", "")).strip()
    if not instruction:
        return {}
    result = dict(data)
    result["instruction"] = instruction
    return result


def _save_debug_directive(story_id: str, branch_id: str, directive: dict):
    if not isinstance(directive, dict):
        return
    instruction = str(directive.get("instruction", "")).strip()
    if not instruction:
        _clear_debug_directive(story_id, branch_id)
        return
    payload = dict(directive)
    payload["instruction"] = instruction
    if not payload.get("created_at"):
        payload["created_at"] = datetime.now(timezone.utc).isoformat()
    _save_json(_debug_directive_path(story_id, branch_id), payload)


def _clear_debug_directive(story_id: str, branch_id: str):
    path = _debug_directive_path(story_id, branch_id)
    if os.path.exists(path):
        os.remove(path)


def _copy_debug_directive(story_id: str, from_bid: str, to_bid: str):
    directive = _load_debug_directive(story_id, from_bid)
    if directive:
        _save_debug_directive(story_id, to_bid, directive)
    else:
        _clear_debug_directive(story_id, to_bid)


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


def _gm_plan_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "gm_plan.json")


def _load_gm_plan(story_id: str, branch_id: str) -> dict:
    data = _load_json(_gm_plan_path(story_id, branch_id), {})
    return data if isinstance(data, dict) else {}


def _save_gm_plan(story_id: str, branch_id: str, plan: dict):
    path = _gm_plan_path(story_id, branch_id)
    if not isinstance(plan, dict) or not plan:
        if os.path.exists(path):
            os.remove(path)
        return
    _save_json(path, plan)


def _safe_int(value: object, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _relink_plan_payoffs_by_title(payoffs: object, active_event_rows: list[dict],
                                  default_created_index: int) -> list[dict]:
    if not isinstance(payoffs, list):
        return []

    id_to_title: dict[int, str] = {}
    title_to_id: dict[str, int] = {}
    for row in active_event_rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "")).strip()
        eid = _safe_int(row.get("id"))
        if not title or eid is None:
            continue
        id_to_title[eid] = title
        if title not in title_to_id:
            title_to_id[title] = eid

    linked: list[dict] = []
    seen_titles: set[str] = set()
    for raw in payoffs:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("event_title", "")).strip()
        if not title or title in seen_titles:
            continue

        raw_event_id = _safe_int(raw.get("event_id"))
        resolved_event_id: int | None = None
        if raw_event_id is not None and id_to_title.get(raw_event_id) == title:
            resolved_event_id = raw_event_id
        else:
            resolved_event_id = title_to_id.get(title)
        if resolved_event_id is None:
            continue

        ttl = _safe_int(raw.get("ttl_turns"), 3) or 3
        ttl = max(1, min(ttl, 6))
        created_at_index = _safe_int(raw.get("created_at_index"), default_created_index)
        if created_at_index is None:
            created_at_index = default_created_index

        linked.append({
            "event_title": title,
            "event_id": resolved_event_id,
            "ttl_turns": ttl,
            "created_at_index": created_at_index,
        })
        seen_titles.add(title)
    return linked


def _normalize_gm_plan_payload(raw_plan: dict, previous_plan: dict, msg_index: int,
                               active_event_rows: list[dict]) -> dict | None:
    """Normalize extracted plan payload.

    Return semantics:
    - dict: valid plan content to persist
    - {}:   valid but empty plan => clear existing gm_plan.json
    - None: invalid payload => ignore, keep existing plan
    """
    if not isinstance(raw_plan, dict):
        return None

    arc = str(raw_plan.get("arc", "")).strip()
    arc = arc[:120]

    next_beats: list[str] = []
    for beat in raw_plan.get("next_beats", []) if isinstance(raw_plan.get("next_beats"), list) else []:
        if not isinstance(beat, str):
            continue
        text = beat.strip()
        if not text or text in next_beats:
            continue
        next_beats.append(text[:80])
        if len(next_beats) >= 3:
            break

    prev_created_at: dict[str, int] = {}
    if isinstance(previous_plan, dict):
        for payoff in previous_plan.get("must_payoff", []) if isinstance(previous_plan.get("must_payoff"), list) else []:
            if not isinstance(payoff, dict):
                continue
            title = str(payoff.get("event_title", "")).strip()
            created_at = _safe_int(payoff.get("created_at_index"))
            if title and created_at is not None:
                prev_created_at[title] = created_at

    raw_payoffs: list[dict] = []
    for payoff in raw_plan.get("must_payoff", []) if isinstance(raw_plan.get("must_payoff"), list) else []:
        if not isinstance(payoff, dict):
            continue
        title = str(payoff.get("event_title", "")).strip()
        if not title:
            continue
        ttl = _safe_int(payoff.get("ttl_turns"), 3) or 3
        ttl = max(1, min(ttl, 6))
        created_at = prev_created_at.get(title)
        if created_at is None:
            created_at = _safe_int(payoff.get("created_at_index"), msg_index)
        if created_at is None:
            created_at = msg_index
        raw_payoffs.append({
            "event_title": title,
            "event_id": payoff.get("event_id"),
            "ttl_turns": ttl,
            "created_at_index": created_at,
        })

    linked_payoffs = _relink_plan_payoffs_by_title(
        raw_payoffs, active_event_rows, default_created_index=msg_index
    )

    if not arc and not next_beats and not linked_payoffs:
        return {}

    return {
        "arc": arc,
        "next_beats": next_beats,
        "must_payoff": linked_payoffs,
        "updated_at_index": msg_index,
    }


def _copy_gm_plan(story_id: str, from_bid: str, to_bid: str, branch_point_index: int | None = None):
    if from_bid == to_bid:
        return

    plan = _load_gm_plan(story_id, from_bid)
    if not plan:
        if branch_point_index is None:
            _save_gm_plan(story_id, to_bid, {})
        return

    if branch_point_index is not None:
        updated_at = _safe_int(plan.get("updated_at_index"))
        if updated_at is None or updated_at > branch_point_index:
            return

    copied = copy.deepcopy(plan)
    copied["arc"] = str(copied.get("arc", "")).strip()[:120]
    copied["next_beats"] = [
        str(b).strip()[:80]
        for b in copied.get("next_beats", [])
        if isinstance(b, str) and str(b).strip()
    ][:3]
    updated_at_index = _safe_int(copied.get("updated_at_index"), 0) or 0
    active_event_rows = get_active_events(story_id, to_bid, limit=80)
    copied["must_payoff"] = _relink_plan_payoffs_by_title(
        copied.get("must_payoff", []),
        active_event_rows,
        default_created_index=updated_at_index,
    )

    if not copied["arc"] and not copied["next_beats"] and not copied["must_payoff"]:
        _save_gm_plan(story_id, to_bid, {})
        return
    _save_gm_plan(story_id, to_bid, copied)


def _compute_payoff_remaining(payoff: dict, current_index: int) -> int:
    ttl = _safe_int(payoff.get("ttl_turns"), 3) or 3
    ttl = max(1, min(ttl, 6))
    created_at = _safe_int(payoff.get("created_at_index"), current_index)
    if created_at is None:
        created_at = current_index
    return ttl - max(0, current_index - created_at)


def _summarize_gm_plan_for_prompt(plan: dict, current_index: int) -> str:
    if not isinstance(plan, dict):
        return "（無）"

    lines: list[str] = []
    arc = str(plan.get("arc", "")).strip()
    if arc:
        lines.append(f"弧線：{arc}")

    beats = [
        str(b).strip()
        for b in plan.get("next_beats", [])
        if isinstance(b, str) and str(b).strip()
    ][:3]
    if beats:
        lines.append("節點：")
        for i, beat in enumerate(beats, 1):
            lines.append(f"{i}. {beat}")

    payoffs: list[str] = []
    for payoff in plan.get("must_payoff", []) if isinstance(plan.get("must_payoff"), list) else []:
        if not isinstance(payoff, dict):
            continue
        title = str(payoff.get("event_title", "")).strip()
        if not title:
            continue
        remaining = _compute_payoff_remaining(payoff, current_index)
        if remaining <= 0:
            continue
        payoffs.append(f"{title}（剩餘 {remaining} 回合）")
    if payoffs:
        lines.append("待回收：")
        lines.extend(f"- {p}" for p in payoffs[:2])

    return "\n".join(lines) if lines else "（無）"


def _build_gm_plan_injection_block(story_id: str, branch_id: str, current_index: int,
                                   char_limit: int = GM_PLAN_CHAR_LIMIT) -> str:
    plan = _load_gm_plan(story_id, branch_id)
    if not isinstance(plan, dict) or not plan:
        return ""

    arc = str(plan.get("arc", "")).strip()
    beats = [
        str(b).strip()
        for b in plan.get("next_beats", [])
        if isinstance(b, str) and str(b).strip()
    ][:3]

    payoffs: list[tuple[str, int]] = []
    for payoff in plan.get("must_payoff", []) if isinstance(plan.get("must_payoff"), list) else []:
        if not isinstance(payoff, dict):
            continue
        title = str(payoff.get("event_title", "")).strip()
        if not title:
            continue
        remaining = _compute_payoff_remaining(payoff, current_index)
        if remaining <= 0:
            continue
        payoffs.append((title, remaining))
    payoffs = payoffs[:2]

    if not arc and not beats and not payoffs:
        return ""

    header = "[GM 敘事計劃（僅供 GM 內部參考，勿透露給玩家）]"

    def _render(arc_text: str, beat_list: list[str], payoff_list: list[tuple[str, int]]) -> str:
        lines = [header]
        if arc_text:
            lines.append(f"- 當前弧線：{arc_text}")
        if beat_list:
            lines.append("- 接下來節點：")
            for i, beat in enumerate(beat_list, 1):
                lines.append(f"  {i}. {beat}")
        if payoff_list:
            lines.append("- 待回收伏筆：")
            for title, remaining in payoff_list:
                lines.append(f"  - {title}（剩餘 {remaining} 回合）")
        return "\n".join(lines)

    while True:
        block = _render(arc, beats, payoffs)
        if len(block) <= char_limit:
            return block
        if payoffs:
            payoffs = payoffs[:-1]
            continue
        if beats:
            beats = beats[:-1]
            continue
        if len(arc) > 24:
            arc = arc[: max(16, len(arc) - 12)].rstrip() + "…"
            continue
        return block[:char_limit].rstrip()


def _build_debug_directive_injection_block(story_id: str, branch_id: str) -> str:
    directive = _load_debug_directive(story_id, branch_id)
    instruction = str(directive.get("instruction", "")).strip()
    if not instruction:
        return ""
    return "\n".join([
        "[Debug 修正指令（僅供 GM 內部參考，勿透露給玩家）]",
        instruction,
    ])


def _format_debug_recent_messages(messages: list[dict]) -> str:
    if not messages:
        return "（無）"
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            label = "玩家"
        elif role in {"gm", "assistant"}:
            label = "GM"
        else:
            label = "系統"
        idx = m.get("index")
        content = str(m.get("content", "")).strip()
        if len(content) > 320:
            content = content[:320].rstrip() + "…"
        lines.append(f"[#{idx}][{label}] {content}")
    return "\n".join(lines)


def _build_debug_system_prompt(story_id: str, branch_id: str, recent_messages: list[dict]) -> str:
    state = _load_character_state(story_id, branch_id)
    npcs = _load_npcs(story_id, branch_id, include_archived=True)
    world_day = get_world_day(story_id, branch_id)
    dungeon_progress = _load_dungeon_progress(story_id, branch_id) or {}
    gm_plan = _load_gm_plan(story_id, branch_id)
    pending_directive = _load_debug_directive(story_id, branch_id)
    recent_text = _format_debug_recent_messages(recent_messages)

    # V1 intentionally injects full JSON snapshots. Large stories can exceed
    # model context here because this prompt path does not enforce a token budget.
    return (
        "你是 RPG Debug 診斷與修正助手。你的任務是：\n"
        "1. 協助檢查狀態/NPC/獎勵點/世界日/副本進度是否一致。\n"
        "2. 可以提供修正提案，但不做劇情演出。\n"
        "3. 修正提案請用標籤輸出：<!--DEBUG_ACTION {json} DEBUG_ACTION-->。\n"
        "4. 劇情修正指令請用標籤輸出：<!--DEBUG_DIRECTIVE {\"instruction\":\"...\"} DEBUG_DIRECTIVE-->。\n"
        "5. 標籤以外的文字會顯示給使用者，請清楚說明判斷依據。\n"
        "6. 若資訊不足，先追問，不要硬猜。\n\n"
        "可用 action type：state_patch / npc_upsert / npc_delete / world_day_set / dungeon_patch。\n"
        "dungeon_patch 可用欄位：progress_delta、completed_nodes、discovered_areas、explored_area_updates。\n\n"
        f"[主聊天最近訊息（唯讀參考）]\n{recent_text}\n\n"
        f"[character_state.json]\n{json.dumps(state, ensure_ascii=False, indent=2)}\n\n"
        f"[npcs.json（含 archived）]\n{json.dumps(npcs, ensure_ascii=False, indent=2)}\n\n"
        f"[world_day]\n{json.dumps(world_day, ensure_ascii=False)}\n\n"
        f"[dungeon_progress.json]\n{json.dumps(dungeon_progress, ensure_ascii=False, indent=2)}\n\n"
        f"[gm_plan.json]\n{json.dumps(gm_plan, ensure_ascii=False, indent=2)}\n\n"
        f"[pending_debug_directive]\n{json.dumps(pending_directive, ensure_ascii=False, indent=2)}\n"
    )


def _get_schema_known_keys(schema: dict) -> set[str]:
    """Extract all known field keys from character schema."""
    known = set()
    for f in schema.get("fields", []):
        known.add(f["key"])
    for l in schema.get("lists", []):
        known.add(l["key"])
        if l.get("state_add_key"):
            known.add(l["state_add_key"])
        if l.get("state_remove_key"):
            known.add(l["state_remove_key"])
    for k in schema.get("direct_overwrite_keys", []):
        known.add(k)
    known.add("reward_points_delta")
    known.add("reward_points")
    return known


_EVENT_STATUS_ORDER = {"planted": 0, "triggered": 1, "resolved": 2, "abandoned": 2}
_EVENT_STATUS_ALIASES = {
    "planted": {
        "planted", "new", "open", "pending", "seeded", "埋下", "伏筆", "鋪墊",
    },
    "triggered": {
        "triggered", "ongoing", "active", "inprogress", "in_progress", "進行中", "觸發", "已觸發",
    },
    "resolved": {
        "resolved", "completed", "complete", "done", "closed", "finish", "finished", "解決", "完成", "已完成",
    },
    "abandoned": {
        "abandoned", "cancelled", "canceled", "dropped", "void", "作廢", "廢棄", "放棄", "取消",
    },
}


def _normalize_event_status(raw_status: object) -> str | None:
    if not isinstance(raw_status, str):
        return None
    s = raw_status.strip().lower()
    if not s:
        return None
    s = s.replace("-", "_").replace(" ", "")
    for canonical, aliases in _EVENT_STATUS_ALIASES.items():
        if s == canonical or s in aliases:
            return canonical
    return None


def _build_active_events_hint(story_id: str, branch_id: str, limit: int = 40) -> str:
    rows = get_active_events(story_id, branch_id, limit=limit)
    if not rows:
        return "（無）"
    lines = []
    for row in rows:
        eid = row.get("id")
        title = str(row.get("title", "")).strip()
        status = _normalize_event_status(row.get("status")) or str(row.get("status", "")).strip()
        if not title or eid is None:
            continue
        lines.append(f"#{eid} [{status}] {title}")
    return "\n".join(lines) if lines else "（無）"


def _apply_event_ops(
    story_id: str,
    branch_id: str,
    event_ops: dict,
    msg_index: int,
    existing_titles: set[str],
    existing_title_map: dict[str, dict],
) -> int:
    """Apply id-driven event ops. Returns number of writes."""
    if not isinstance(event_ops, dict):
        return 0

    writes = 0
    id_map: dict[int, dict] = {}
    for title, meta in existing_title_map.items():
        if not isinstance(meta, dict):
            continue
        eid = meta.get("id")
        if isinstance(eid, int):
            id_map[eid] = {"title": title, "status": meta.get("status", "")}

    for op in event_ops.get("update", []) if isinstance(event_ops.get("update"), list) else []:
        if not isinstance(op, dict):
            continue
        raw_id = op.get("id")
        if isinstance(raw_id, str) and raw_id.isdigit():
            raw_id = int(raw_id)
        if not isinstance(raw_id, int):
            continue
        new_status = _normalize_event_status(op.get("status"))
        if not new_status:
            continue
        current = id_map.get(raw_id)
        if not current:
            continue
        old_status = _normalize_event_status(current.get("status")) or str(current.get("status", "")).strip()
        if _EVENT_STATUS_ORDER.get(new_status, -1) > _EVENT_STATUS_ORDER.get(old_status, -1):
            update_event_status(story_id, raw_id, new_status)
            title = current["title"]
            existing_title_map[title]["status"] = new_status
            id_map[raw_id]["status"] = new_status
            writes += 1

    for op in event_ops.get("create", []) if isinstance(event_ops.get("create"), list) else []:
        if not isinstance(op, dict):
            continue
        title = str(op.get("title", "")).strip()
        if not title:
            continue
        new_status = _normalize_event_status(op.get("status")) or "planted"
        if title in existing_titles:
            existing = existing_title_map.get(title, {})
            event_id = existing.get("id")
            old_status = _normalize_event_status(existing.get("status")) or str(existing.get("status", "")).strip()
            if (
                isinstance(event_id, int)
                and _EVENT_STATUS_ORDER.get(new_status, -1) > _EVENT_STATUS_ORDER.get(old_status, -1)
            ):
                update_event_status(story_id, event_id, new_status)
                existing_title_map[title]["status"] = new_status
                id_map[event_id] = {"title": title, "status": new_status}
                writes += 1
            continue

        payload = {
            "event_type": op.get("event_type", "遭遇"),
            "title": title,
            "description": op.get("description", ""),
            "message_index": msg_index,
            "status": new_status,
            "tags": op.get("tags", ""),
        }
        new_id = insert_event(story_id, payload, branch_id)
        existing_titles.add(title)
        existing_title_map[title] = {"id": new_id, "status": new_status}
        id_map[new_id] = {"title": title, "status": new_status}
        writes += 1

    return writes


def _append_unique_str(target: list[str], value: object):
    if not isinstance(value, str):
        return
    v = value.strip()
    if not v:
        return
    if v not in target:
        target.append(v)


def _state_ops_to_update(state_ops: dict, schema: dict, current_state: dict | None = None) -> dict:
    """Translate state_ops contract into existing canonical state update shape."""
    if not isinstance(state_ops, dict):
        return {}
    current_state = current_state or {}
    update: dict = {}

    list_defs = {l["key"]: l for l in schema.get("lists", []) if isinstance(l, dict) and l.get("key")}
    map_keys = {k for k, l in list_defs.items() if l.get("type") == "map"}
    map_keys.update({f["key"] for f in schema.get("fields", []) if f.get("type") == "map"})
    list_keys = {k for k, l in list_defs.items() if l.get("type", "list") != "map"}
    direct_overwrite = set(schema.get("direct_overwrite_keys", []))
    known_keys = _get_schema_known_keys(schema)

    set_ops = state_ops.get("set")
    if isinstance(set_ops, dict):
        for key, val in set_ops.items():
            if key in map_keys:
                # Full map replacement is not supported by the canonical update
                # pipeline. Callers should use map_upsert/map_remove instead.
                log.warning("state_ops: reject set.%s (map key); use map_upsert/map_remove", key)
                continue
            if key in list_keys:
                # list replacement is unsupported by current canonical pipeline.
                continue
            if val is None:
                # null in set means no-op; deletions should go via map_remove/list_remove.
                continue
            if key == "reward_points":
                # Keep reward_points changes deterministic via delta only.
                log.warning("state_ops: reject set.reward_points; use delta.reward_points")
                continue
            if key in direct_overwrite or key in known_keys:
                update[key] = val

    delta_ops = state_ops.get("delta")
    if isinstance(delta_ops, dict):
        for key, val in delta_ops.items():
            if not _is_numeric_value(val):
                continue
            if key == "reward_points":
                update["reward_points_delta"] = update.get("reward_points_delta", 0) + val
                continue
            if key in known_keys:
                delta_key = f"{key}_delta"
                update[delta_key] = update.get(delta_key, 0) + val

    map_upsert = state_ops.get("map_upsert")
    if isinstance(map_upsert, dict):
        for map_key, kv in map_upsert.items():
            if map_key not in map_keys or not isinstance(kv, dict):
                continue
            bucket = update.setdefault(map_key, {})
            if not isinstance(bucket, dict):
                bucket = {}
                update[map_key] = bucket
            for raw_k, raw_v in kv.items():
                if raw_k is None:
                    continue
                k = str(raw_k).strip()
                if not k:
                    continue
                bucket[k] = raw_v

    map_remove = state_ops.get("map_remove")
    if isinstance(map_remove, dict):
        for map_key, keys in map_remove.items():
            if map_key not in map_keys:
                continue
            if isinstance(keys, str):
                keys = [keys]
            if not isinstance(keys, list):
                continue
            bucket = update.setdefault(map_key, {})
            if not isinstance(bucket, dict):
                bucket = {}
                update[map_key] = bucket
            for raw_key in keys:
                if not isinstance(raw_key, str):
                    continue
                key = raw_key.strip()
                if key:
                    bucket[key] = None

    list_add = state_ops.get("list_add")
    if isinstance(list_add, dict):
        for list_key, items in list_add.items():
            if list_key not in list_keys:
                continue
            if isinstance(items, str):
                items = [items]
            if not isinstance(items, list):
                continue
            list_def = list_defs[list_key]
            add_key = list_def.get("state_add_key") or f"{list_key}_add"
            bucket = update.setdefault(add_key, [])
            if not isinstance(bucket, list):
                bucket = []
                update[add_key] = bucket
            for item in items:
                _append_unique_str(bucket, item)

    list_remove = state_ops.get("list_remove")
    if isinstance(list_remove, dict):
        for list_key, items in list_remove.items():
            if list_key not in list_keys:
                continue
            if isinstance(items, str):
                items = [items]
            if not isinstance(items, list):
                continue
            list_def = list_defs[list_key]
            remove_key = list_def.get("state_remove_key") or f"{list_key}_remove"
            bucket = update.setdefault(remove_key, [])
            if not isinstance(bucket, list):
                bucket = []
                update[remove_key] = bucket
            for item in items:
                _append_unique_str(bucket, item)

    return update


def _normalize_map_key(key: str) -> str:
    """Normalize a map key for fuzzy matching. Preserves semantics, normalizes characters."""
    k = key.replace(' ', '').replace('\u3000', '')
    k = k.replace('（', '(').replace('）', ')')
    # Fullwidth→halfwidth BEFORE dot/dash regex so fullwidth variants are caught
    result = []
    for ch in k:
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:
            result.append(chr(cp - 0xFEE0))
        else:
            result.append(ch)
    k = ''.join(result)
    k = _NORMALIZE_DOTS_RE.sub('·', k)
    k = _NORMALIZE_DASHES_RE.sub('—', k)
    return k


def _resolve_map_keys(update_map: dict, existing_map: dict) -> dict:
    """Rewrite update keys to match existing keys via fuzzy normalization."""
    if not existing_map or not update_map:
        return update_map
    norm_to_existing = {}
    for ek in existing_map:
        norm_to_existing[_normalize_map_key(ek)] = ek
    resolved = {}
    for uk, uv in update_map.items():
        norm_uk = _normalize_map_key(uk)
        if norm_uk in norm_to_existing and uk != norm_to_existing[norm_uk]:
            resolved[norm_to_existing[norm_uk]] = uv
        else:
            resolved[uk] = uv
    return resolved


def _dedup_inventory_plain_vs_variant(inv_map: dict) -> dict:
    """Drop plain-name keys when a variant with the same base name exists.

    Example:
      "縛魂者之脊" + "縛魂者之脊 (C級)" -> keep only the variant key.

    Safety:
      Distinct variant keys (e.g. 定界珠(生) vs 定界珠(死)) are preserved.
    """
    if not isinstance(inv_map, dict) or len(inv_map) < 2:
        return inv_map

    groups: dict[str, list[str]] = {}
    for key in inv_map.keys():
        base = _extract_item_base_name(key)
        norm_base = _normalize_map_key(base)
        groups.setdefault(norm_base, []).append(key)

    remove_keys: set[str] = set()
    for keys in groups.values():
        if len(keys) < 2:
            continue
        plain_keys = [k for k in keys if k.strip() == _extract_item_base_name(k)]
        variant_keys = [k for k in keys if k not in plain_keys]
        if plain_keys and variant_keys:
            remove_keys.update(plain_keys)

    if not remove_keys:
        return inv_map
    return {k: v for k, v in inv_map.items() if k not in remove_keys}


def _migrate_list_to_map(items: list) -> dict:
    """Convert a list of item strings to a map (key→value).

    Lossless migration: only splits on " — " separator.  Items without
    the separator use the full string as key (empty value).  This avoids
    destroying distinct items that share the same base name (e.g.
    定界珠（生） vs 定界珠（死） are different items, not evolutions).
    """
    result = {}
    for item in items:
        if isinstance(item, str):
            item = item.strip()
            if " — " in item:
                parts = item.split(" — ", 1)
                result[parts[0].strip()] = parts[1].strip()
            else:
                result[item] = ""
    return result


def _parse_item_to_kv(item: str) -> tuple[str, str]:
    """Parse a list-format inventory item string into (key, value) for map format.

    Examples:
      "封印之鏡 — 可以封印低等級怨靈"  → ("封印之鏡", "可以封印低等級怨靈")
      "死生之刃·日耀輪轉（靈魂加固版）"  → ("死生之刃·日耀輪轉", "靈魂加固版")
      "鎮魂符×5"                         → ("鎮魂符", "×5")
      "蝕魂者之戒"                       → ("蝕魂者之戒", "")
    """
    # Handle "name — description" format first
    if " — " in item:
        parts = item.split(" — ", 1)
        return parts[0].strip(), parts[1].strip()

    base = _extract_item_base_name(item)
    remainder = item[len(base):].strip()

    # Strip outer parentheses from remainder
    if remainder.startswith("（") and remainder.endswith("）"):
        remainder = remainder[1:-1]
    elif remainder.startswith("(") and remainder.endswith(")"):
        remainder = remainder[1:-1]

    return base, remainder


def _apply_state_update_inner(story_id: str, branch_id: str, update: dict, schema: dict):
    """Core logic: apply a STATE update dict to character state. No normalization."""
    state = _load_character_state(story_id, branch_id)

    # Backward compat: convert legacy inventory_add/inventory_remove to map format.
    # Old extraction or STATE tags may still use list-based add/remove keys for
    # fields that are now map type.  Convert them into the map delta format so
    # the map branch handles them naturally.
    for list_def in schema.get("lists", []):
        if list_def.get("type") != "map":
            continue
        lkey = list_def["key"]
        add_key = list_def.get("state_add_key") or f"{lkey}_add"
        rm_key = list_def.get("state_remove_key") or f"{lkey}_remove"
        if add_key in update or rm_key in update:
            inv_map = update.setdefault(lkey, {})
            if not isinstance(inv_map, dict):
                inv_map = {}
                update[lkey] = inv_map
            # Process removes (set to null)
            if rm_key in update:
                rm_val = update.pop(rm_key)
                if isinstance(rm_val, str):
                    rm_val = [rm_val]
                if isinstance(rm_val, list):
                    for item in rm_val:
                        if isinstance(item, str):
                            inv_map[_extract_item_base_name(item)] = None
            # Process adds (parse item string into key/value)
            if add_key in update:
                add_val = update.pop(add_key)
                if isinstance(add_val, str):
                    add_val = [add_val]
                if isinstance(add_val, list):
                    for item in add_val:
                        if isinstance(item, str):
                            base, status = _parse_item_to_kv(item)
                            inv_map[base] = status
            if inv_map:
                existing = state.get(lkey, {})
                update[lkey] = _resolve_map_keys(inv_map, existing)

    # Process map-type fields defined in schema.fields (e.g. systems)
    for field_def in schema.get("fields", []):
        if field_def.get("type") != "map":
            continue
        key = field_def["key"]
        if key in update and isinstance(update[key], dict):
            existing = state.get(key, {})
            update[key] = _resolve_map_keys(update[key], existing)
            for k, v in update[key].items():
                if v is None:
                    if k in existing:
                        existing.pop(k)
                    else:
                        base = _extract_item_base_name(k)
                        for ek in list(existing):
                            if _extract_item_base_name(ek) == base:
                                existing.pop(ek)
                                break
                else:
                    existing[k] = v
            state[key] = existing

    # Process list fields from schema
    for list_def in schema.get("lists", []):
        key = list_def["key"]
        list_type = list_def.get("type", "list")

        if list_type == "map":
            if key in update and isinstance(update[key], dict):
                existing = state.get(key, {})
                update[key] = _resolve_map_keys(update[key], existing)
                for k, v in update[key].items():
                    if v is None:
                        if k in existing:
                            existing.pop(k)
                        else:
                            base = _extract_item_base_name(k)
                            for ek in list(existing):
                                if _extract_item_base_name(ek) == base:
                                    existing.pop(ek)
                                    break
                    else:
                        existing[k] = v
                state[key] = existing
        else:
            # Process remove BEFORE add — when both are present (e.g. item
            # status change), adding first would cause the base-name removal
            # to nuke the newly added item too.
            remove_key = list_def.get("state_remove_key")
            if remove_key and remove_key in update:
                lst = state.get(key, [])
                rm_val = update[remove_key]
                # Same safety: wrap string in list
                if isinstance(rm_val, str):
                    rm_val = [rm_val]
                elif not isinstance(rm_val, list):
                    rm_val = []
                for rm_item in rm_val:
                    if not isinstance(rm_item, str):
                        continue
                    # Try exact match first; fall back to base-name match.
                    # This prevents over-matching genuinely different items
                    # (e.g. 定界珠（生） vs 定界珠（死）).
                    if rm_item in lst:
                        lst = [x for x in lst if x != rm_item]
                    else:
                        rm_base = _extract_item_base_name(rm_item)
                        lst = [x for x in lst if _extract_item_base_name(x) != rm_base]
                state[key] = lst

            add_key = list_def.get("state_add_key")
            if add_key and add_key in update:
                lst = state.get(key, [])
                add_val = update[add_key]
                # LLM sometimes returns a string instead of list — wrap it
                if isinstance(add_val, str):
                    add_val = [add_val]
                elif not isinstance(add_val, list):
                    add_val = []
                for item in add_val:
                    if not isinstance(item, str):
                        continue
                    if item in lst:
                        continue
                    # Auto-dedup: replace existing items that are "plain" versions
                    # (no parenthetical suffix) of the new item's base name.
                    # Only replaces bare names like 武器 when adding 武器（強化版）.
                    # Items with their own suffix (定界珠（生） vs 定界珠（死）) are
                    # treated as distinct and NOT auto-replaced.
                    add_base = _extract_item_base_name(item)
                    if add_base:
                        lst = [x for x in lst if not (
                            _extract_item_base_name(x) == add_base
                            and x.strip() == add_base  # only replace bare/plain names
                        )]
                    lst.append(item)
                state[key] = lst

    # Generic *_delta handling: apply as addition to base field
    for key in list(update.keys()):
        if key.endswith("_delta") and _is_numeric_value(update[key]):
            base_key = key[:-6]  # strip "_delta"
            current = state.get(base_key)
            if _is_numeric_value(current):
                state[base_key] = current + update[key]
            elif base_key == "reward_points":
                state[base_key] = state.get(base_key, 0) + update[key]
            # else: base field is not numeric or doesn't exist — skip delta

    # If GM sets reward_points directly (no delta), accept it.
    # Only applies when delta is absent — delta takes precedence when both present.
    if "reward_points" in update and "reward_points_delta" not in update:
        val = update["reward_points"]
        if _is_numeric_value(val):
            state["reward_points"] = int(val)

    # Direct overwrite fields from schema
    for key in schema.get("direct_overwrite_keys", []):
        if key in update:
            state[key] = update[key]

    # Inventory hygiene: if both plain and variant keys coexist for same base
    # item, keep the variant key and drop the plain key.
    if isinstance(state.get("inventory"), dict):
        state["inventory"] = _dedup_inventory_plain_vs_variant(state["inventory"])

    # Build handled_keys set
    handled_keys = set()
    handled_keys.add("reward_points")
    for field_def in schema.get("fields", []):
        if field_def.get("type") == "map":
            handled_keys.add(field_def["key"])
    for list_def in schema.get("lists", []):
        handled_keys.add(list_def["key"])
        if list_def.get("state_add_key"):
            handled_keys.add(list_def["state_add_key"])
        if list_def.get("state_remove_key"):
            handled_keys.add(list_def["state_remove_key"])
    for key in schema.get("direct_overwrite_keys", []):
        handled_keys.add(key)
    # Mark all *_delta keys as handled (already processed above)
    for key in update:
        if key.endswith("_delta"):
            handled_keys.add(key)

    # Keys managed by other systems — never save to character state
    _SYSTEM_KEYS = {"world_day", "world_time", "branch_title"}
    # Defense-in-depth: still filter scene/instruction keys here as backup
    # (primary filtering is in _validate_state_update gate)

    # Save extra keys — only non-delta, non-handled, string/number fields
    for key, val in update.items():
        if key in _SYSTEM_KEYS or key in _SCENE_KEYS or key in _INSTRUCTION_KEYS:
            continue
        # Reject keys ending with _add/_remove for non-schema lists
        if (key.endswith("_add") or key.endswith("_remove")) and key not in handled_keys:
            continue
        if key not in handled_keys and isinstance(val, (str, int, float, bool)):
            state[key] = val

    _save_json(_story_character_state_path(story_id, branch_id), state)
    _sync_state_db_from_state(story_id, branch_id, state)


def _apply_state_update(story_id: str, branch_id: str, update: dict):
    """Apply a STATE update dict to the branch's character state file.

    1. Runs deterministic validation gate (if enabled)
    2. Immediately applies the (possibly sanitized) update
    3. Reconciles dungeon entry/exit for narrative-path transitions
    4. Validates dungeon growth constraints (hard cap)
    5. Kicks off background LLM normalization for non-standard fields
    """
    schema = _load_character_schema(story_id)

    # Load once, reuse for gate + dungeon validation
    current_state = _load_character_state(story_id, branch_id)
    old_state = copy.deepcopy(current_state)

    # Deterministic validation gate
    update = _run_state_gate(
        update, schema, current_state,
        label="state_gate", story_id=story_id, branch_id=branch_id,
    )

    # Apply immediately
    _apply_state_update_inner(story_id, branch_id, update, schema)

    new_state = _load_character_state(story_id, branch_id)

    # Initialize dungeon progress before validation so entry turns see the budget.
    reconcile_dungeon_entry(story_id, branch_id, old_state, new_state)

    # Hard constraint validation (dungeon system)
    validate_dungeon_progression(story_id, branch_id, new_state, old_state)

    # Archive old dungeon after validation so exit turns record capped growth.
    reconcile_dungeon_exit(story_id, branch_id, old_state, new_state)

    _save_json(_story_character_state_path(story_id, branch_id), new_state)
    _sync_state_db_from_state(story_id, branch_id, new_state)

    # Background: normalize non-standard fields and re-apply
    _normalize_state_async(story_id, branch_id, update, _get_schema_known_keys(schema))


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
# Migration — legacy data → stories layout
# ---------------------------------------------------------------------------

def _migrate_to_timeline_tree(story_id: str):
    """One-time migration: create timeline_tree.json for a story from existing data."""
    tree_path = _story_tree_path(story_id)
    if os.path.exists(tree_path):
        return

    now = datetime.now(timezone.utc).isoformat()

    session_id_path = os.path.join(DATA_DIR, "session_id.txt")
    session_id = None
    if os.path.exists(session_id_path):
        with open(session_id_path, "r") as f:
            sid = f.read().strip()
            if sid:
                session_id = sid

    tree = {
        "active_branch_id": "main",
        "branches": {
            "main": {
                "id": "main",
                "name": "主時間線",
                "parent_branch_id": None,
                "branch_point_index": None,
                "created_at": now,
                "session_id": session_id,
                "character_state_file": "character_state_main.json",
            }
        },
    }
    _save_tree(story_id, tree)

    # Migrate new_messages.json → messages_main.json
    main_msgs_path = _story_messages_path(story_id, "main")
    legacy_new_msgs = os.path.join(_story_dir(story_id), "new_messages.json")
    if os.path.exists(legacy_new_msgs) and not os.path.exists(main_msgs_path):
        shutil.move(legacy_new_msgs, main_msgs_path)

    # Ensure messages_main.json exists
    if not os.path.exists(main_msgs_path):
        _save_branch_messages(story_id, "main", [])


def _migrate_to_stories():
    """One-time migration: move all legacy flat data/ files into data/stories/story_original/."""
    if os.path.exists(STORIES_REGISTRY_PATH):
        return  # already migrated

    _ensure_data_dir()
    story_id = "story_original"
    story_dir = _story_dir(story_id)
    os.makedirs(story_dir, exist_ok=True)

    # Move files from data/ to data/stories/story_original/
    moves = {
        "timeline_tree.json": "timeline_tree.json",
        "parsed_conversation.json": "parsed_conversation.json",
        "new_messages.json": "new_messages.json",
    }
    for src_name, dst_name in moves.items():
        src = os.path.join(DATA_DIR, src_name)
        dst = os.path.join(story_dir, dst_name)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)

    # Move messages_*.json and character_state_*.json
    for fname in os.listdir(DATA_DIR):
        if fname.startswith("messages_") and fname.endswith(".json"):
            src = os.path.join(DATA_DIR, fname)
            dst = os.path.join(story_dir, fname)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
        elif fname.startswith("character_state_") and fname.endswith(".json"):
            src = os.path.join(DATA_DIR, fname)
            dst = os.path.join(story_dir, fname)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)

    # Also copy the generic character_state.json if it exists (legacy compat)
    legacy_char = os.path.join(DATA_DIR, "character_state.json")
    if os.path.exists(legacy_char):
        dst = os.path.join(story_dir, "character_state.json")
        if not os.path.exists(dst):
            shutil.copy2(legacy_char, dst)

    # Ensure design dir exists for legacy migration
    os.makedirs(_story_design_dir(story_id), exist_ok=True)

    # Generate system_prompt.txt from prompts.py template
    prompt_path = _story_system_prompt_path(story_id)
    if not os.path.exists(prompt_path):
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(SYSTEM_PROMPT_TEMPLATE)

    # Generate character_schema.json
    schema_path = _story_character_schema_path(story_id)
    if not os.path.exists(schema_path):
        _save_json(schema_path, DEFAULT_CHARACTER_SCHEMA)

    # Generate default_character_state.json
    default_state_path = _story_default_character_state_path(story_id)
    if not os.path.exists(default_state_path):
        _save_json(default_state_path, DEFAULT_CHARACTER_STATE)

    # Ensure parsed_conversation.json exists
    parsed_path = _story_parsed_path(story_id)
    if not os.path.exists(parsed_path):
        if os.path.exists(CONVERSATION_PATH):
            save_parsed()
            if os.path.exists(LEGACY_PARSED_PATH):
                shutil.copy2(LEGACY_PARSED_PATH, parsed_path)
        else:
            _save_json(parsed_path, [])

    # Write stories.json registry
    now = datetime.now(timezone.utc).isoformat()
    registry = {
        "active_story_id": story_id,
        "stories": {
            story_id: {
                "id": story_id,
                "name": "主神空間 — 無限輪迴",
                "description": "諸天無限流·主神空間 RPG",
                "created_at": now,
            }
        },
    }
    _save_stories_registry(registry)

    # Run timeline tree migration within the story
    _migrate_to_timeline_tree(story_id)


def _migrate_branch_files(story_id: str):
    """One-time migration: move flat per-branch files into branches/<branch_id>/ subdirs."""
    story_dir = _story_dir(story_id)
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    if not branches:
        return

    migrated = False
    for branch_id in branches:
        branch_dir = os.path.join(story_dir, "branches", branch_id)
        os.makedirs(branch_dir, exist_ok=True)

        # Map: old flat filename → new filename inside branch dir
        moves = [
            (f"messages_{branch_id}.json", "messages.json"),
            (f"character_state_{branch_id}.json", "character_state.json"),
            (f"npcs_{branch_id}.json", "npcs.json"),
            (f"npc_activities_{branch_id}.json", "npc_activities.json"),
        ]
        for old_name, new_name in moves:
            src = os.path.join(story_dir, old_name)
            dst = os.path.join(branch_dir, new_name)
            if os.path.exists(src) and not os.path.exists(dst):
                shutil.move(src, dst)
                migrated = True

    # Also migrate legacy npcs.json → branches/main/npcs.json
    legacy_npcs = os.path.join(story_dir, "npcs.json")
    main_npcs = os.path.join(story_dir, "branches", "main", "npcs.json")
    if os.path.exists(legacy_npcs) and not os.path.exists(main_npcs):
        os.makedirs(os.path.dirname(main_npcs), exist_ok=True)
        shutil.move(legacy_npcs, main_npcs)
        migrated = True

    if migrated:
        log.info("Migrated branch files to branches/ dirs for story %s", story_id)


def _migrate_schema_abilities(story_id: str):
    """One-time migration: add 'abilities' list field to character schema and default state."""
    # --- character_schema.json ---
    schema = _load_character_schema(story_id)
    lists = schema.get("lists", [])
    has_abilities = any(l.get("key") == "abilities" for l in lists)
    if not has_abilities:
        lists.append({
            "key": "abilities",
            "label": "功法與技能",
            "state_add_key": "abilities_add",
            "state_remove_key": "abilities_remove",
        })
        schema["lists"] = lists
        schema_path = _story_character_schema_path(story_id)
        _save_json(schema_path, schema)
        log.info("Migrated character_schema.json: added 'abilities' list for story %s", story_id)

    # --- default_character_state.json ---
    default_path = _story_default_character_state_path(story_id)
    if os.path.exists(default_path):
        default_state = _load_json(default_path, {})
        if "abilities" not in default_state:
            default_state["abilities"] = []
            _save_json(default_path, default_state)
            log.info("Migrated default_character_state.json: added 'abilities' for story %s", story_id)


def _migrate_design_files(story_id: str):
    """One-time migration: copy design files from data/stories/<id>/ to story_design/<id>/.

    Copies files only if they exist in the old location but not in the new one.
    The old copies in data/ become inert (no longer read by code).
    """
    design_dir = _story_design_dir(story_id)
    old_dir = _story_dir(story_id)

    DESIGN_FILES = [
        "system_prompt.txt",
        "character_schema.json",
        "default_character_state.json",
        "world_lore.json",
        "parsed_conversation.json",
        "nsfw_preferences.json",
    ]

    migrated = False
    for fname in DESIGN_FILES:
        old_path = os.path.join(old_dir, fname)
        new_path = os.path.join(design_dir, fname)
        if os.path.exists(old_path) and not os.path.exists(new_path):
            os.makedirs(design_dir, exist_ok=True)
            shutil.copy2(old_path, new_path)
            migrated = True

    if migrated:
        log.info("Migrated design files to story_design/ for story %s", story_id)
        # Rebuild lore index if world_lore.json was migrated
        lore_path = os.path.join(design_dir, "world_lore.json")
        if os.path.exists(lore_path):
            try:
                rebuild_lore_index(story_id)
            except Exception:
                log.warning("Failed to rebuild lore index after migration for %s", story_id, exc_info=True)


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

def _pick_latest_debug_directive(directives: object) -> dict | None:
    if not isinstance(directives, list):
        return None
    for item in reversed(directives):
        if not isinstance(item, dict):
            continue
        instruction = str(item.get("instruction", "")).strip()
        if not instruction:
            continue
        return {"instruction": instruction}
    return None


def _apply_debug_action(story_id: str, branch_id: str, action: dict) -> dict:
    normalized = _normalize_debug_action_payload(action)
    if isinstance(normalized, dict):
        action = normalized
    action_type = str(action.get("type", "")).strip()
    if not action_type:
        return {"type": "unknown", "ok": False, "error": "missing action type"}

    if action_type == "state_patch":
        update = action.get("update")
        if not isinstance(update, dict):
            return {"type": action_type, "ok": False, "error": "invalid state update"}
        _apply_state_update(story_id, branch_id, update)
        return {"type": action_type, "ok": True}

    if action_type == "npc_upsert":
        npc = action.get("npc")
        if not isinstance(npc, dict) or not str(npc.get("name", "")).strip():
            return {"type": action_type, "ok": False, "error": "invalid npc payload"}
        _save_npc(story_id, npc, branch_id)
        return {"type": action_type, "ok": True}

    if action_type == "npc_delete":
        npc_id = str(action.get("npc_id", "")).strip()
        if not npc_id:
            return {"type": action_type, "ok": False, "error": "npc_id required"}
        npcs = _load_npcs(story_id, branch_id, include_archived=True)
        removed_names = [n.get("name", "").strip() for n in npcs if n.get("id") == npc_id and n.get("name")]
        if not removed_names:
            return {"type": action_type, "ok": False, "error": "npc not found"}
        npcs = [n for n in npcs if n.get("id") != npc_id]
        _save_json(_story_npcs_path(story_id, branch_id), npcs)
        for name in removed_names:
            delete_state_entry(story_id, branch_id, category="npc", entry_key=name)
        return {"type": action_type, "ok": True}

    if action_type == "world_day_set":
        world_day = action.get("world_day")
        try:
            day_value = float(world_day)
        except (TypeError, ValueError):
            return {"type": action_type, "ok": False, "error": "invalid world_day"}
        if day_value < 0:
            return {"type": action_type, "ok": False, "error": "world_day must be >= 0"}
        set_world_day(story_id, branch_id, day_value)
        return {"type": action_type, "ok": True}

    if action_type == "dungeon_patch":
        progress = _load_dungeon_progress(story_id, branch_id)
        if not progress or not progress.get("current_dungeon"):
            return {"type": action_type, "ok": False, "error": "no active dungeon"}

        progress_delta_raw = action.get("progress_delta", action.get("mainline_progress_delta"))
        completed_nodes = action.get("completed_nodes", [])
        discovered_areas = action.get("discovered_areas", [])
        explored_updates = action.get("explored_area_updates", {})
        did_update = False

        if progress_delta_raw is not None or completed_nodes:
            if progress_delta_raw is None:
                progress_delta = 0
            else:
                try:
                    progress_delta = int(float(progress_delta_raw))
                except (TypeError, ValueError):
                    return {"type": action_type, "ok": False, "error": "invalid progress_delta"}
            if not isinstance(completed_nodes, list):
                return {"type": action_type, "ok": False, "error": "completed_nodes must be a list"}
            update_dungeon_progress(story_id, branch_id, {
                "progress_delta": progress_delta,
                "nodes_completed": [str(x) for x in completed_nodes if str(x).strip()],
            })
            did_update = True

        if discovered_areas or explored_updates:
            if not isinstance(discovered_areas, list):
                return {"type": action_type, "ok": False, "error": "discovered_areas must be a list"}
            if not isinstance(explored_updates, dict):
                return {"type": action_type, "ok": False, "error": "explored_area_updates must be an object"}
            cleaned_updates = {}
            for area_id, delta in explored_updates.items():
                try:
                    cleaned_updates[str(area_id)] = int(float(delta))
                except (TypeError, ValueError):
                    continue
            update_dungeon_area(story_id, branch_id, {
                "discovered_areas": [str(x) for x in discovered_areas if str(x).strip()],
                "explored_area_updates": cleaned_updates,
            })
            did_update = True

        if not did_update:
            return {"type": action_type, "ok": False, "error": "empty dungeon patch"}
        return {"type": action_type, "ok": True}

    return {"type": action_type, "ok": False, "error": f"unsupported action type: {action_type}"}


def _build_debug_apply_audit_summary(results: list[dict], directive_applied: int) -> str:
    total = len(results)
    success = sum(1 for r in results if r.get("ok"))
    failed_types = [str(r.get("type", "unknown")) for r in results if not r.get("ok")]
    if total == 0:
        summary = "未套用資料修正"
    elif success == 0:
        summary = f"所有修正項目失敗（0/{total}）"
    elif success == total:
        summary = f"已套用 {success}/{total} 項修正"
    else:
        summary = f"已套用 {success}/{total} 項修正（{total - success} 項失敗：{', '.join(failed_types[:5])}）"

    if directive_applied > 0:
        summary += "；已注入劇情指令"
    return summary


def _append_debug_audit_message(story_id: str, branch_id: str, summary: str):
    text = str(summary or "").strip()
    if not text:
        return
    _upsert_branch_message(story_id, branch_id, {
        "role": "system",
        "message_type": "debug_audit",
        "content": text,
        "index": _next_branch_message_index_fast(story_id, branch_id),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# NPC API
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _init_lore_indexes():
    """Rebuild lore search indexes for all stories on startup."""
    if not os.path.exists(STORY_DESIGN_DIR):
        return
    for story_dir_name in os.listdir(STORY_DESIGN_DIR):
        lore_path = os.path.join(STORY_DESIGN_DIR, story_dir_name, "world_lore.json")
        if os.path.exists(lore_path):
            rebuild_lore_index(story_dir_name)


def _cleanup_incomplete_branches():
    """Remove branches orphaned by server crash (no GM response saved)."""
    if not os.path.exists(STORIES_DIR):
        return
    for story_dir_name in os.listdir(STORIES_DIR):
        tree_path = os.path.join(STORIES_DIR, story_dir_name, "timeline_tree.json")
        if not os.path.exists(tree_path):
            continue
        tree = _load_json(tree_path, {})
        branches = tree.get("branches", {})
        modified = False
        to_delete = []

        for bid, branch in branches.items():
            if bid == "main":
                continue
            if branch.get("deleted") or branch.get("blank") or branch.get("merged") or branch.get("pruned"):
                continue
            if bid.startswith("auto_"):
                continue
            # Check messages.json in the branch directory (avoid _branch_dir which calls makedirs)
            msgs_path = os.path.join(STORIES_DIR, story_dir_name, "branches", bid, "messages.json")
            msgs = _load_json(msgs_path, [])
            has_user = any(m.get("role") == "user" for m in msgs)
            has_gm = any(m.get("role") == "gm" for m in msgs)
            # Only delete branches with a user message but no GM response (crashed edit).
            # Empty deltas (crashed regen or manual create) are left alone — can't distinguish.
            if has_user and not has_gm:
                to_delete.append(bid)

        for bid in to_delete:
            parent = branches[bid].get("parent_branch_id", "main")
            # Reparent children to deleted branch's parent
            for other_bid, other_branch in branches.items():
                if other_branch.get("parent_branch_id") == bid:
                    other_branch["parent_branch_id"] = parent
            del branches[bid]
            if tree.get("active_branch_id") == bid:
                tree["active_branch_id"] = parent
            try:
                delete_events_for_branch(story_dir_name, bid)
            except Exception as e:
                log.warning(
                    "Startup cleanup: failed to delete events for branch %s in story %s (%s)",
                    bid, story_dir_name, e,
                )
            bdir = os.path.join(STORIES_DIR, story_dir_name, "branches", bid)
            if os.path.isdir(bdir):
                shutil.rmtree(bdir)
            log.warning("Startup cleanup: removed incomplete branch %s from story %s (no GM response)", bid, story_dir_name)
            modified = True

        if modified:
            _save_json(tree_path, tree)


# ---------------------------------------------------------------------------
# Usage tracking API
# ---------------------------------------------------------------------------



def _init_dungeon_templates():
    """Ensure dungeon templates exist for all stories on startup."""
    if not os.path.exists(STORIES_DIR):
        return
    for story_dir_name in os.listdir(STORIES_DIR):
        story_path = os.path.join(STORIES_DIR, story_dir_name)
        if os.path.isdir(story_path):
            ensure_dungeon_templates(story_dir_name)


__all__ = [name for name in globals() if not name.startswith("__")]
