"""Flask backend for 主神空間 RPG Web App — multi-story support."""

import copy
import json
import logging
import logging.handlers
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone

# Version — single source of truth in VERSION file
_version_file = os.path.join(os.path.dirname(__file__), "VERSION")
if os.path.exists(_version_file):
    with open(_version_file) as _f:
        __version__ = _f.read().strip()
else:
    __version__ = "0.0.0"

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

# ---------------------------------------------------------------------------
# Logging — console + rotating file
# ---------------------------------------------------------------------------
_log_fmt = logging.Formatter(
    "[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Console handler (for interactive use / tmux)
_console_h = logging.StreamHandler()
_console_h.setFormatter(_log_fmt)

# Rotating file handler — 5 MB per file, keep 3 backups
_log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log")
_file_h = logging.handlers.RotatingFileHandler(
    _log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_file_h.setFormatter(_log_fmt)

# Root logger — captures Flask/Werkzeug + our "rpg" logger
logging.root.setLevel(logging.INFO)
logging.root.addHandler(_console_h)
logging.root.addHandler(_file_h)

log = logging.getLogger("rpg")

from llm_bridge import call_claude_gm, call_claude_gm_stream, get_last_usage, get_provider
import usage_db
from event_db import insert_event, search_relevant_events, get_events, get_event_by_id, update_event_status, search_events as search_events_db
from image_gen import generate_image_async, get_image_status, get_image_path
from lore_db import rebuild_index as rebuild_lore_index, search_relevant_lore, upsert_entry as upsert_lore_entry, get_toc as get_lore_toc, delete_entry as delete_lore_entry, get_entry_count, get_category_summary, get_embedding_stats, find_duplicates
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
    _load_dungeon_templates, _load_dungeon_template, _load_dungeon_progress,
    _parse_rank,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STORIES_DIR = os.path.join(DATA_DIR, "stories")
STORY_DESIGN_DIR = os.path.join(BASE_DIR, "story_design")
STORIES_REGISTRY_PATH = os.path.join(DATA_DIR, "stories.json")

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
STATE_REVIEW_MODE = os.environ.get("STATE_REVIEW_MODE", "warn")

# LLM reviewer: only active when STATE_REVIEW_MODE=enforce and STATE_REVIEW_LLM=on
STATE_REVIEW_LLM = os.environ.get("STATE_REVIEW_LLM", "off")


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


STATE_REVIEW_LLM_TIMEOUT = _parse_env_float("STATE_REVIEW_LLM_TIMEOUT_MS", 1800.0) / 1000
STATE_REVIEW_LLM_MAX_INFLIGHT = max(1, _parse_env_int("STATE_REVIEW_LLM_MAX_INFLIGHT", 4))
_STATE_REVIEW_LLM_SEM = threading.BoundedSemaphore(STATE_REVIEW_LLM_MAX_INFLIGHT)

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


# ---------------------------------------------------------------------------
# Helpers — generic
# ---------------------------------------------------------------------------

def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_json(path, default=None):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else []


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Helpers — Stories registry
# ---------------------------------------------------------------------------

def _load_stories_registry() -> dict:
    return _load_json(STORIES_REGISTRY_PATH, {})


def _save_stories_registry(registry: dict):
    _save_json(STORIES_REGISTRY_PATH, registry)


def _active_story_id() -> str:
    reg = _load_stories_registry()
    return reg.get("active_story_id", "story_original")


# ---------------------------------------------------------------------------
# Helpers — Story paths
# ---------------------------------------------------------------------------

def _story_dir(story_id: str) -> str:
    return os.path.join(STORIES_DIR, story_id)


def _story_design_dir(story_id: str) -> str:
    return os.path.join(STORY_DESIGN_DIR, story_id)


def _story_tree_path(story_id: str) -> str:
    return os.path.join(_story_dir(story_id), "timeline_tree.json")


def _story_parsed_path(story_id: str) -> str:
    return os.path.join(_story_design_dir(story_id), "parsed_conversation.json")


def _branch_dir(story_id: str, branch_id: str) -> str:
    d = os.path.join(_story_dir(story_id), "branches", branch_id)
    os.makedirs(d, exist_ok=True)
    return d


def _story_messages_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "messages.json")


def _story_character_state_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "character_state.json")


def _nsfw_preferences_path(story_id: str) -> str:
    return os.path.join(_story_design_dir(story_id), "nsfw_preferences.json")


def _load_nsfw_preferences(story_id: str) -> dict:
    """Return {"chips": [...], "custom": "..."} or empty dict."""
    path = _nsfw_preferences_path(story_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


_CHIP_GROUP_LABELS = {
    "style": "風格",
    "positions": "體位",
    "foreplay": "前戲/技巧",
    "climax": "高潮/結束",
    "props": "道具/情境",
    "scene": "場景",
    "focus": "描寫重點",
    "dynamic": "角色動態",
}


def _format_nsfw_preferences(prefs: dict) -> str:
    """Format chips (by group) + custom text into a structured prompt string."""
    chips = prefs.get("chips", {})
    lines = []
    # chips can be dict {group: [values]} or legacy list
    if isinstance(chips, dict):
        for group, values in chips.items():
            if not values:
                continue
            label = _CHIP_GROUP_LABELS.get(group, group)
            lines.append(f"  {label}：{'、'.join(values)}")
    elif isinstance(chips, list) and chips:
        lines.append(f"  {'、'.join(chips)}")
    custom = prefs.get("custom", "").strip()
    if custom:
        lines.append(f"  補充：{custom}")
    return "\n".join(lines) if lines else ""


def _story_system_prompt_path(story_id: str) -> str:
    return os.path.join(_story_design_dir(story_id), "system_prompt.txt")


def _branch_config_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "config.json")


def _load_branch_config(story_id: str, branch_id: str) -> dict:
    return _load_json(_branch_config_path(story_id, branch_id), {})


def _save_branch_config(story_id: str, branch_id: str, config: dict):
    _save_json(_branch_config_path(story_id, branch_id), config)


def _story_character_schema_path(story_id: str) -> str:
    return os.path.join(_story_design_dir(story_id), "character_schema.json")


def _story_default_character_state_path(story_id: str) -> str:
    return os.path.join(_story_design_dir(story_id), "default_character_state.json")


# ---------------------------------------------------------------------------
# Helpers — Story-scoped loaders
# ---------------------------------------------------------------------------

def _load_tree(story_id: str) -> dict:
    return _load_json(_story_tree_path(story_id), {})


def _save_tree(story_id: str, tree: dict):
    _save_json(_story_tree_path(story_id), tree)


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


def _load_character_schema(story_id: str) -> dict:
    path = _story_character_schema_path(story_id)
    return _load_json(path, DEFAULT_CHARACTER_SCHEMA)


def _blank_character_state(story_id: str) -> dict:
    """Generate a blank placeholder character state from schema."""
    schema = _load_character_schema(story_id)
    state = {}
    for field in schema.get("fields", []):
        key = field["key"]
        ftype = field.get("type", "text")
        if ftype == "number":
            state[key] = 0
        else:
            state[key] = "—"
    for lst in schema.get("lists", []):
        key = lst["key"]
        if lst.get("type") == "map":
            state[key] = {}
        else:
            state[key] = []
    return state


def _load_character_state(story_id: str, branch_id: str = "main") -> dict:
    path = _story_character_state_path(story_id, branch_id)
    state = _load_json(path, {})
    if not state:
        # Fallback to story's default character state
        default_path = _story_default_character_state_path(story_id)
        state = _load_json(default_path, {})
    if not state:
        state = copy.deepcopy(DEFAULT_CHARACTER_STATE)
    # Backfill current_phase for legacy branches
    if "current_phase" not in state:
        state["current_phase"] = "主神空間"

    # Self-healing: strip known artifacts from persisted state
    dirty = False
    schema = _load_character_schema(story_id)
    known = _get_schema_known_keys(schema)
    # Which list keys were affected by the string-iteration bug
    buggy_list_keys = set()
    for l in schema.get("lists", []):
        if l.get("state_add_key"):
            buggy_list_keys.add(l["key"])

    for key in list(state.keys()):
        # Remove processing artifacts (_delta, _add, _remove for non-schema keys)
        if key.endswith(("_delta", "_add", "_remove")) and key not in known:
            del state[key]
            dirty = True
    # Remove single-character entries only from lists that have state_add_key
    # (those affected by the string-iteration bug), and only when 3+ single-char
    # entries exist (to avoid false positives on legitimate CJK single-char names)
    for key in buggy_list_keys:
        lst = state.get(key)
        if not isinstance(lst, list):
            continue
        single_chars = [x for x in lst if isinstance(x, str) and len(x) == 1]
        if len(single_chars) >= 3:
            cleaned = [x for x in lst if not isinstance(x, str) or len(x) > 1]
            if len(cleaned) != len(lst):
                state[key] = cleaned
                dirty = True

    # Auto-migrate: convert list-format fields to map when schema says map
    for l in schema.get("lists", []):
        if l.get("type") != "map":
            continue
        lkey = l["key"]
        val = state.get(lkey)
        if isinstance(val, list):
            state[lkey] = _migrate_list_to_map(val)
            dirty = True
            log.info("    auto-migrate: converted %s from list to map in %s/%s", lkey, story_id, branch_id)

    if dirty:
        log.info("    self-heal: cleaned artifacts from %s/%s", story_id, branch_id)
    _save_json(path, state)
    return state


_TEAM_RULES = {
    "free_agent": (
        "4. **組隊系統**：主神以「個人」為單位分配任務，每次從輪迴者中挑選 20-30 人投放進同一副本。"
        "進入副本後自行結盟、組隊、分工，任務結束各自回主神空間，下次重新分配。"
        "每次副本的隊友組合都不同——你可能遇到老戰友、排行榜大佬、甚至死對頭。"
        "信任建立是生存核心：這人可信嗎？合作還是防備？"
    ),
    "fixed_team": (
        "4. **團隊系統**：新人混合隊（20人）存活者可組成固定隊伍（最多8人），"
        "之後每次任務整隊一起進副本。有人死了可招募補位，低於4人主神強制塞人。"
        "固定隊伍間偶爾會被安排進同一副本，形成合作或對抗局面。"
    ),
}


def _rel_to_str(val) -> str:
    """Normalize a relationship value to string (may be str or dict)."""
    if isinstance(val, dict):
        return val.get("summary") or val.get("description") or val.get("type") or ""
    return val or ""


def _classify_npc(npc: dict, rels: dict) -> str:
    """Classify an NPC into a relationship category.

    Uses current_status, character_state relationships, role, and
    relationship_to_player — in that priority order.
    """
    name = npc.get("name", "")
    status = (npc.get("current_status") or "").lower()
    role = (npc.get("role") or "").lower()
    rel_player = (npc.get("relationship_to_player") or "").lower()
    char_rel = _rel_to_str(rels.get(name)).lower()
    combined = f"{status} {role} {rel_player} {char_rel}"

    # Dead NPCs first — GM must not resurrect them
    if any(k in status for k in ("死亡", "已故", "陣亡")):
        return "dead"
    # Hostile
    if any(k in combined for k in ("敵", "對手", "威脅", "仇")):
        return "hostile"
    # Captured / prisoner
    if any(k in combined for k in ("俘", "囚", "關押")):
        return "captured"
    # Ally — broad keyword coverage for production data
    ally_kw = ("隊友", "戰友", "同伴", "盟友", "夥伴", "伴侶", "隨從",
               "忠誠", "信任", "兄弟", "好感", "崇拜", "曖昧", "約定")
    if any(k in combined for k in ally_kw):
        return "ally"
    # NPC appears in character_state relationships → likely ally
    if name in rels:
        return "ally"
    return "neutral"


def _build_critical_facts(story_id: str, branch_id: str, state: dict, npcs: list[dict]) -> str:
    """Build critical facts section for system prompt to prevent factual inconsistencies.

    Accepts pre-loaded state and npcs to avoid redundant file reads.
    """
    lines = []

    # 1. Current phase
    if state.get("current_phase"):
        lines.append(f"- 當前階段：{state['current_phase']}")

    # 2. World time (get_world_day returns float: fractional days)
    wd = get_world_day(story_id, branch_id)
    if wd:
        day = int(wd) + 1  # day 1-based
        hour = int((wd % 1) * 24)
        if hour < 6:
            period = "深夜"
        elif hour < 9:
            period = "清晨"
        elif hour < 12:
            period = "上午"
        elif hour < 18:
            period = "下午"
        else:
            period = "夜晚"
        lines.append(f"- 當前時間：世界第 {day} 天·{period}")

    # 3. Key character stats
    if state.get("gene_lock"):
        lines.append(f"- 基因鎖：{state['gene_lock']}")
    if state.get("reward_points") is not None:
        try:
            lines.append(f"- 獎勵點餘額：{int(state['reward_points']):,} 點")
        except (ValueError, TypeError):
            lines.append(f"- 獎勵點餘額：{state['reward_points']} 點")

    # 4. Key inventory (top 5 items, name only)
    inv = state.get("inventory", {})
    if inv:
        if isinstance(inv, dict):
            item_names = list(inv.keys())[:5]
        else:
            # Legacy list format (pre-migration)
            item_names = [item.split("—")[0].split(" — ")[0].strip() for item in inv[:5]]
        lines.append(f"- 關鍵道具：{'、'.join(item_names)}")

    # 5. NPC relationship matrix
    rels = state.get("relationships", {})
    if npcs:
        groups: dict[str, list[str]] = {}
        for npc in npcs:
            name = npc.get("name", "?")
            cat = _classify_npc(npc, rels)
            rel = _rel_to_str(rels.get(name)) or npc.get("relationship_to_player", "")
            entry = f"{name}（{rel}）" if rel else name
            groups.setdefault(cat, []).append(entry)
        labels = {"ally": "隊友", "hostile": "敵對", "captured": "俘虜",
                  "dead": "已故", "neutral": "其他NPC"}
        for cat in ("ally", "hostile", "captured", "dead", "neutral"):
            members = groups.get(cat)
            if members:
                lines.append(f"- {labels[cat]}：{'、'.join(members)}")
    elif rels:
        rel_parts = [f"{name}（{_rel_to_str(rel)}）" for name, rel in rels.items()]
        lines.append(f"- 人際關係：{'、'.join(rel_parts)}")

    if not lines:
        return "（尚無關鍵事實記錄）"
    return "\n".join(lines)


def _build_story_system_prompt(story_id: str, state_text: str, branch_id: str = "main", narrative_recap: str = "") -> str:
    """Read the story's system_prompt.txt and fill in placeholders."""
    # Blank branches are fresh starts — no narrative context from parent
    tree = _load_tree(story_id)
    branch = tree.get("branches", {}).get(branch_id, {})
    if branch.get("blank"):
        narrative_recap = ""

    if not narrative_recap:
        narrative_recap = "（尚無回顧，完整對話記錄已提供。）"

    prompt_path = _story_system_prompt_path(story_id)
    lore_text = _build_lore_text(story_id, branch_id)
    # Load NPCs once — used by both npc_text and critical_facts
    npcs = _load_npcs(story_id, branch_id)
    npc_text = _build_npc_text(story_id, branch_id)
    # Parse state dict from JSON string to avoid redundant _load_character_state
    try:
        state_dict = json.loads(state_text)
    except (json.JSONDecodeError, TypeError):
        state_dict = {}
    critical_facts = _build_critical_facts(story_id, branch_id, state_dict, npcs)
    branch_config = _load_branch_config(story_id, branch_id)
    team_mode = branch_config.get("team_mode", "free_agent")
    team_rules = _TEAM_RULES.get(team_mode, _TEAM_RULES["free_agent"])
    # Build dungeon context
    dungeon_context = build_dungeon_context(story_id, branch_id)
    if os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            template = f.read()
        result = template.format(
            character_state=state_text,
            story_summary="",
            world_lore=lore_text,
            npc_profiles=npc_text,
            team_rules=team_rules,
            narrative_recap=narrative_recap,
            other_agents="（目前無其他輪迴者資料）",
            critical_facts=critical_facts,
            dungeon_context=dungeon_context,
        )
        # If template lacks {critical_facts} placeholder, inject before character state
        if critical_facts and "關鍵事實" not in result:
            marker = "## 當前角色狀態"
            idx = result.find(marker)
            facts_section = f"## ⚠️ 關鍵事實（絕對不可搞混）\n{critical_facts}\n\n"
            if idx >= 0:
                result = result[:idx] + facts_section + result[idx:]
            else:
                result = facts_section + result
    else:
        # Fallback to prompts.py template
        result = build_system_prompt(state_text, critical_facts=critical_facts, dungeon_context=dungeon_context)

    # Strip fate direction section when fate mode is off
    story_dir = _story_dir(story_id)
    if not get_fate_mode(story_dir, branch_id):
        # Remove the ## ⚠️ 命運走向系統 section (up to next ## heading)
        result = re.sub(
            r"## ⚠️ 命運走向系統.*?(?=## |\Z)",
            "",
            result,
            flags=re.DOTALL,
        ).strip() + "\n"

    # Pistol mode (手槍模式) — inject NSFW scene instructions
    if get_pistol_mode(story_dir, branch_id):
        pistol_block = (
            "\n\n## 親密場景指示（手槍模式已啟用）\n"
            "- 當劇情自然發展到親密、浪漫或情色場景時，請詳細描寫，不要跳過或淡化。\n"
            "- 描寫應包含感官細節（觸覺、視覺、聽覺等），保持文學性與沉浸感。\n"
            "- 【最重要規則】在玩家明確說出「結束」「停止」「離開」等字眼之前，場景永遠不會結束。"
            "高潮不是結束——高潮之後還有餘韻、喘息、擁抱、對話、甚至第二輪。"
            "絕對不要自行宣佈場景結束（如「落下帷幕」「儀式完成」），也不要在場景中插入系統提示或遊戲機制通知。\n"
            "- 角色的情感反應、對話和身體語言都要細膩呈現。\n"
            "- 回覆結尾可以列出 2-4 個選項，但這些選項必須全部是繼續親密互動的方向（例如：換體位、再來一次、事後溫存、轉到另一個角色等），絕對不能出現離開、做其他事、備戰之類的選項。\n"
            "- 每次回覆的親密場景描寫請盡可能寫長，至少 1500 字以上，不設上限。充分展開每個感官細節、情感變化、對話和身體反應，不要草草帶過或省略。\n"
            "- 下方的偏好設定是玩家希望在【整場親密互動過程中】逐漸體驗的元素。"
            "請在心中規劃一個循序漸進的節奏（前戲→升溫→高潮→餘韻），"
            "將這些元素自然分配到不同階段。每次回覆只融入 1-3 個元素，"
            "不要一次全部塞入。根據玩家的回應靈活調整節奏和方向，"
            "不需要死板地按計畫走。\n"
        )
        prefs_text = _format_nsfw_preferences(_load_nsfw_preferences(story_id))
        if prefs_text:
            pistol_block += f"- 玩家偏好設定：\n{prefs_text}\n"
        result += pistol_block

    return result


# ---------------------------------------------------------------------------
# Helpers — STATE tag parsing & character state update
# ---------------------------------------------------------------------------

_TAG_OPEN = r"(?:<!--|\[)"
_TAG_CLOSE = r"(?:-->|\])"
_STATE_RE = re.compile(_TAG_OPEN + r"STATE\s*(.*?)\s*STATE" + _TAG_CLOSE, re.DOTALL)
_LORE_RE = re.compile(_TAG_OPEN + r"LORE\s*(.*?)\s*LORE" + _TAG_CLOSE, re.DOTALL)
_NPC_RE = re.compile(_TAG_OPEN + r"NPC\s*(.*?)\s*NPC" + _TAG_CLOSE, re.DOTALL)
_EVENT_RE = re.compile(_TAG_OPEN + r"EVENT\s*(.*?)\s*EVENT" + _TAG_CLOSE, re.DOTALL)
_IMG_RE = re.compile(_TAG_OPEN + r"IMG\s+prompt:\s*(.*?)\s*IMG" + _TAG_CLOSE, re.DOTALL)


def _extract_state_tag(text: str) -> tuple[str, list[dict]]:
    """Extract all <!--STATE {...} STATE--> tags from GM response."""
    updates: list[dict] = []
    while True:
        m = _STATE_RE.search(text)
        if not m:
            break
        try:
            updates.append(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            pass
        text = text[: m.start()].rstrip() + text[m.end():]
        text = text.strip()
    return text, updates


def _extract_lore_tag(text: str) -> tuple[str, list[dict]]:
    """Extract all <!--LORE {...} LORE--> tags from GM response."""
    lores: list[dict] = []
    while True:
        m = _LORE_RE.search(text)
        if not m:
            break
        try:
            lores.append(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            pass
        text = text[: m.start()].rstrip() + text[m.end():]
        text = text.strip()
    return text, lores


def _extract_npc_tag(text: str) -> tuple[str, list[dict]]:
    """Extract all <!--NPC {...} NPC--> tags from GM response."""
    npcs: list[dict] = []
    while True:
        m = _NPC_RE.search(text)
        if not m:
            break
        try:
            npcs.append(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            pass
        text = text[: m.start()].rstrip() + text[m.end():]
        text = text.strip()
    return text, npcs


def _extract_event_tag(text: str) -> tuple[str, list[dict]]:
    """Extract all <!--EVENT {...} EVENT--> tags from GM response."""
    events: list[dict] = []
    while True:
        m = _EVENT_RE.search(text)
        if not m:
            break
        try:
            events.append(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            pass
        text = text[: m.start()].rstrip() + text[m.end():]
        text = text.strip()
    return text, events


def _extract_img_tag(text: str) -> tuple[str, str | None]:
    """Extract all <!--IMG prompt: ... IMG--> tags from GM response. Returns (clean_text, first_prompt_or_None)."""
    first_prompt: str | None = None
    while True:
        m = _IMG_RE.search(text)
        if not m:
            break
        p = m.group(1).strip()
        if p and first_prompt is None:
            first_prompt = p
        text = text[: m.start()].rstrip() + text[m.end():]
        text = text.strip()
    return text, first_prompt


# ---------------------------------------------------------------------------
# Helpers — NPC data
# ---------------------------------------------------------------------------

def _story_npcs_path(story_id: str, branch_id: str = "main") -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "npcs.json")


def _load_npcs(story_id: str, branch_id: str = "main") -> list[dict]:
    path = _story_npcs_path(story_id, branch_id)
    return _load_json(path, [])


def _save_npc(story_id: str, npc_data: dict, branch_id: str = "main"):
    """Save or update an NPC entry. Matches by 'name' field."""
    npcs = _load_npcs(story_id, branch_id)
    name = npc_data.get("name", "").strip()
    if not name:
        return

    # Generate id if not present
    if "id" not in npc_data:
        npc_data["id"] = "npc_" + re.sub(r'\W+', '', name)[:20]

    for i, existing in enumerate(npcs):
        if existing.get("name") == name:
            # Merge: preserve fields not in update
            merged = {**existing, **npc_data}
            npcs[i] = merged
            _save_json(_story_npcs_path(story_id, branch_id), npcs)
            return

    npcs.append(npc_data)
    _save_json(_story_npcs_path(story_id, branch_id), npcs)


def _copy_npcs_to_branch(story_id: str, from_branch_id: str, to_branch_id: str):
    """Copy NPC data from parent branch to new branch."""
    npcs = _load_npcs(story_id, from_branch_id)
    _save_json(_story_npcs_path(story_id, to_branch_id), npcs)


def _build_npc_text(story_id: str, branch_id: str = "main") -> str:
    """Build NPC profiles text for system prompt injection."""
    npcs = _load_npcs(story_id, branch_id)
    if not npcs:
        return "（尚無已記錄的 NPC）"

    lines = []
    for npc in npcs:
        lines.append(f"### {npc.get('name', '?')}（{npc.get('role', '?')}）")
        if npc.get("appearance"):
            lines.append(f"- 外觀：{npc['appearance']}")
        p = npc.get("personality", {})
        if isinstance(p, dict) and p.get("summary"):
            lines.append(f"- 性格：{p['summary']}")
        if npc.get("relationship_to_player"):
            lines.append(f"- 與主角關係：{npc['relationship_to_player']}")
        if npc.get("current_status"):
            lines.append(f"- 狀態：{npc['current_status']}")
        if npc.get("notable_traits"):
            lines.append(f"- 特質：{'、'.join(npc['notable_traits'])}")
        lines.append("")

    return "\n".join(lines).strip()


def _story_lore_path(story_id: str) -> str:
    return os.path.join(_story_design_dir(story_id), "world_lore.json")


def _story_saves_path(story_id: str) -> str:
    return os.path.join(_story_dir(story_id), "saves.json")


def _load_lore(story_id: str) -> list[dict]:
    return _load_json(_story_lore_path(story_id), [])


# ---------------------------------------------------------------------------
# Helpers — Branch lore (per-branch auto-extracted lore)
# ---------------------------------------------------------------------------

_branch_lore_locks: dict[str, threading.Lock] = {}
_branch_lore_locks_meta = threading.Lock()


def _get_branch_lore_lock(story_id: str, branch_id: str) -> threading.Lock:
    """Get or create a per-branch lock for branch_lore.json writes."""
    key = f"{story_id}:{branch_id}"
    with _branch_lore_locks_meta:
        if key not in _branch_lore_locks:
            _branch_lore_locks[key] = threading.Lock()
        return _branch_lore_locks[key]


def _branch_lore_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "branch_lore.json")


def _load_branch_lore(story_id: str, branch_id: str) -> list[dict]:
    return _load_json(_branch_lore_path(story_id, branch_id), [])


def _save_branch_lore(story_id: str, branch_id: str, lore: list[dict]):
    _save_json(_branch_lore_path(story_id, branch_id), lore)


def _save_branch_lore_entry(story_id: str, branch_id: str, entry: dict,
                            prefix_registry: dict | None = None):
    """Save a lore entry to branch_lore.json. Upsert by topic.

    Similar to _save_lore_entry but writes to per-branch storage,
    skips lore.db indexing (branch lore uses linear search).
    Thread-safe via per-branch lock.
    """
    topic = entry.get("topic", "").strip()
    if not topic:
        return

    # Auto-classify orphan topic before saving
    category = entry.get("category", "")
    if "：" not in topic and category:
        organized = try_classify_topic(topic, category, story_id, prefix_registry=prefix_registry)
        if organized:
            log.info("    branch_lore auto-classify: '%s' → '%s'", topic, organized)
            topic = organized
            entry["topic"] = topic

    subcategory = entry.get("subcategory", "")
    lock = _get_branch_lore_lock(story_id, branch_id)
    with lock:
        lore = _load_branch_lore(story_id, branch_id)
        for i, existing in enumerate(lore):
            if existing.get("topic") == topic and existing.get("subcategory", "") == subcategory:
                if "category" not in entry and "category" in existing:
                    entry["category"] = existing["category"]
                if "source" not in entry and "source" in existing:
                    entry["source"] = existing["source"]
                if "edited_by" not in entry and "edited_by" in existing:
                    entry["edited_by"] = existing["edited_by"]
                if "subcategory" not in entry and "subcategory" in existing:
                    entry["subcategory"] = existing["subcategory"]
                lore[i] = entry
                _save_branch_lore(story_id, branch_id, lore)
                return
        lore.append(entry)
        _save_branch_lore(story_id, branch_id, lore)


def _merge_branch_lore_into(story_id: str, src_branch_id: str, dst_branch_id: str):
    """Merge source branch_lore into destination (upsert by (subcategory, topic), not overwrite)."""
    src = _load_branch_lore(story_id, src_branch_id)
    if not src:
        return
    dst = _load_branch_lore(story_id, dst_branch_id)
    dst_keys = {(e.get("subcategory", ""), e.get("topic", "")): i for i, e in enumerate(dst)}
    for e in src:
        key = (e.get("subcategory", ""), e.get("topic", ""))
        if key in dst_keys:
            dst[dst_keys[key]] = e
        else:
            dst.append(e)
            dst_keys[key] = len(dst) - 1
    _save_branch_lore(story_id, dst_branch_id, dst)


def _search_branch_lore(story_id: str, branch_id: str, query: str,
                         token_budget: int = 1500,
                         context: dict | None = None) -> str:
    """Search branch_lore.json using CJK bigram scoring. Returns formatted text."""
    lore = _load_branch_lore(story_id, branch_id)
    if not lore:
        return ""

    cjk_re = re.compile(r'[\u4e00-\u9fff]+')

    def _bigrams(text):
        bgs = set()
        for run in cjk_re.findall(text):
            for i in range(len(run) - 1):
                bgs.add(run[i:i + 2])
        return bgs

    query_bgs = _bigrams(query)
    # Also use query words for non-CJK matching
    query_lower = query.lower()

    # Dungeon scoping context
    current_dungeon = ""
    in_dungeon = False
    if context:
        current_dungeon = context.get("dungeon", "")
        phase = context.get("phase", "")
        in_dungeon = bool(current_dungeon and "副本" in phase)

    scored = []
    for e in lore:
        topic = e.get("topic", "")
        content = e.get("content", "")
        category = e.get("category", "")
        text = f"{category} {topic} {content}"

        score = 0.0
        text_bgs = _bigrams(text)
        if query_bgs and text_bgs:
            overlap = query_bgs & text_bgs
            score = len(overlap) / max(len(query_bgs), 1)

        # Boost for substring match in topic
        if query_lower and query_lower in topic.lower():
            score += 2.0

        # Penalize lore from other dungeons
        if in_dungeon and category == "副本世界觀" and e.get("subcategory", "") != current_dungeon:
            score *= 0.1

        if score > 0:
            scored.append((score, e))

    scored.sort(key=lambda x: -x[0])

    # Token-budgeted output
    lines = []
    used_tokens = 0
    for _, e in scored:
        content = e.get("content", "")
        est_tokens = len(content) // 2  # rough CJK estimate
        if used_tokens + est_tokens > token_budget and lines:
            break
        if len(content) > 1200:
            content = content[:1200] + "…（截斷）"
        cat_label = e.get("category", "")
        sub = e.get("subcategory", "")
        if sub:
            cat_label = f"{cat_label}/{sub}"
        lines.append(f"#### {cat_label}：{e.get('topic', '')}")
        lines.append(content)
        lines.append("")
        used_tokens += est_tokens

    if not lines:
        return ""
    return "[相關分支設定]\n" + "\n".join(lines)


def _get_branch_lore_toc(story_id: str, branch_id: str) -> str:
    """Build a simple TOC of branch lore topics for dedup in extraction prompt."""
    lore = _load_branch_lore(story_id, branch_id)
    if not lore:
        return ""
    lines = []
    for e in lore:
        topic = e.get("topic", "")
        cat = e.get("category", "")
        subcat = e.get("subcategory", "")
        if topic:
            prefix = f"{cat}/{subcat}" if subcat else cat
            lines.append(f"- {prefix}：{topic}")
    return "\n".join(lines)


def _find_similar_topic(new_topic: str, new_category: str,
                        topic_categories: dict[str, str], threshold: float = 0.5) -> str | None:
    """Find an existing topic with high CJK bigram overlap, scoped to same category."""
    cjk_re = re.compile(r'[\u4e00-\u9fff]+')

    def _bigrams(text):
        bgs = set()
        for run in cjk_re.findall(text):
            for i in range(len(run) - 1):
                bgs.add(run[i:i + 2])
        return bgs

    new_bgs = _bigrams(new_topic)
    if not new_bgs:
        return None

    best_topic = None
    best_sim = 0.0
    for existing, cat in topic_categories.items():
        if cat != new_category:
            continue  # only compare within same category
        ex_bgs = _bigrams(existing)
        if not ex_bgs:
            continue
        overlap = new_bgs & ex_bgs
        if len(overlap) < 2:
            continue  # require ≥2 shared bigrams to avoid short-topic false positives
        sim = len(overlap) / len(new_bgs | ex_bgs)
        if sim > best_sim:
            best_sim = sim
            best_topic = existing

    return best_topic if best_sim >= threshold else None


def _save_lore_entry(story_id: str, entry: dict, prefix_registry: dict | None = None):
    """Save a lore entry. If same topic exists, update it. Also updates search index.

    Uses lore lock for thread safety + auto-classifies orphan topics.
    """
    topic = entry.get("topic", "").strip()
    if not topic:
        return

    # Auto-classify orphan topic before saving
    category = entry.get("category", "")
    if "：" not in topic and category:
        organized = try_classify_topic(topic, category, story_id, prefix_registry=prefix_registry)
        if organized:
            log.info("    lore auto-classify: '%s' → '%s'", topic, organized)
            topic = organized
            entry["topic"] = topic

    subcategory = entry.get("subcategory", "")
    lock = get_lore_lock(story_id)
    with lock:
        lore = _load_lore(story_id)
        # Update existing (subcategory, topic) or append new
        for i, existing in enumerate(lore):
            if existing.get("topic") == topic and existing.get("subcategory", "") == subcategory:
                # Preserve category if not provided in new entry
                if "category" not in entry and "category" in existing:
                    entry["category"] = existing["category"]
                # Preserve source provenance if not provided in new entry
                if "source" not in entry and "source" in existing:
                    entry["source"] = existing["source"]
                # Preserve edited_by provenance if not provided in new entry
                if "edited_by" not in entry and "edited_by" in existing:
                    entry["edited_by"] = existing["edited_by"]
                if "subcategory" not in entry and "subcategory" in existing:
                    entry["subcategory"] = existing["subcategory"]
                lore[i] = entry
                _save_json(_story_lore_path(story_id), lore)
                upsert_lore_entry(story_id, entry)
                return
        lore.append(entry)
        _save_json(_story_lore_path(story_id), lore)
        upsert_lore_entry(story_id, entry)


def _build_lore_text(story_id: str, branch_id: str = "main") -> str:
    """Build compact lore summary for system prompt.

    Instead of the full TOC (~6-8K tokens), provides a compressed category
    summary (~100 tokens) so the GM retains a mental map of available knowledge.
    Full content is injected per-turn via hybrid search.
    """
    count = get_entry_count(story_id)
    if count == 0:
        # Fallback: check JSON directly (before index is built)
        lore = _load_lore(story_id)
        if not lore:
            return "（尚無已確立的世界設定）"
        count = len(lore)
    cat_summary = get_category_summary(story_id)
    note = f"世界設定共 {count} 條，會根據每回合對話內容自動檢索並注入相關條目。"
    if cat_summary:
        note += f"\n知識分類：{cat_summary}"
    # Note branch-specific lore count if any
    branch_lore = _load_branch_lore(story_id, branch_id)
    if branch_lore:
        note += f"\n另有 {len(branch_lore)} 條分支專屬設定（本次冒險中累積的觀察與發現）。"
    return note


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


def _validate_state_update(update: dict, schema: dict, current_state: dict) -> tuple[dict, list[dict]]:
    """Deterministic validation gate for state updates.

    Returns (sanitized_update, violations) where sanitized_update has invalid
    keys dropped and violations is a list of dicts describing each issue.
    Pure function, no side effects.
    """
    sanitized = {}
    violations = []

    # Build schema-aware lookup sets
    schema_list_keys = set()       # base keys for list-type fields (e.g. "completed_missions")
    schema_add_keys = set()        # e.g. "completed_missions_add", "inventory_add" (fallback)
    schema_remove_keys = set()     # e.g. "completed_missions_remove", "inventory_remove" (fallback)
    map_type_keys = set()          # keys with type=map (lists or fields)
    for l in schema.get("lists", []):
        lkey = l["key"]
        schema_list_keys.add(lkey)
        if l.get("type") == "map":
            map_type_keys.add(lkey)
        # Include both explicit keys AND fallback f"{lkey}_add"/f"{lkey}_remove"
        # because _apply_state_update_inner supports both paths (backward compat
        # for map-type lists that don't declare state_add_key in schema).
        schema_add_keys.add(l.get("state_add_key") or f"{lkey}_add")
        schema_remove_keys.add(l.get("state_remove_key") or f"{lkey}_remove")
    for f in schema.get("fields", []):
        if f.get("type") == "map":
            map_type_keys.add(f["key"])

    direct_overwrite_keys = set(schema.get("direct_overwrite_keys", []))

    for key, val in update.items():
        # Rule 8: Scene/instruction keys — drop early
        if key in _SCENE_KEYS:
            violations.append({"key": key, "rule": "scene_key", "value": val, "action": "drop"})
            continue
        if key in _INSTRUCTION_KEYS:
            violations.append({"key": key, "rule": "instruction_key", "value": val, "action": "drop"})
            continue

        # Rule 1: invalid current_phase
        if key == "current_phase" and val not in VALID_PHASES:
            violations.append({"key": key, "rule": "invalid_phase", "value": val, "action": "drop"})
            continue

        # Rule 2: reward_points_delta must be numeric
        if key == "reward_points_delta" and not isinstance(val, (int, float)):
            violations.append({"key": key, "rule": "non_numeric_delta", "value": val, "action": "drop"})
            continue

        # Rule 3: reward_points must be numeric
        if key == "reward_points" and not isinstance(val, (int, float)):
            violations.append({"key": key, "rule": "non_numeric_points", "value": val, "action": "drop"})
            continue

        # Rule 4: map-type fields must be dict
        if key in map_type_keys and not isinstance(val, dict):
            violations.append({"key": key, "rule": "map_not_dict", "value": type(val).__name__, "action": "drop"})
            continue

        # Rule 5: _add/_remove suffix where base is not a schema list
        if key.endswith("_add") or key.endswith("_remove"):
            if key not in schema_add_keys and key not in schema_remove_keys:
                violations.append({"key": key, "rule": "non_schema_add_remove", "value": val, "action": "drop"})
                continue

        # Rule 6: _add/_remove value — string → wrap [str], non-str/non-list → drop
        if key in schema_add_keys or key in schema_remove_keys:
            if isinstance(val, str):
                val = [val]  # backward compat: wrap string in list
            elif not isinstance(val, list):
                violations.append({"key": key, "rule": "add_remove_not_list", "value": type(val).__name__, "action": "drop"})
                continue

        # Rule 7: *_delta key with non-numeric value
        if key.endswith("_delta") and key != "reward_points_delta":
            if not isinstance(val, (int, float)):
                violations.append({"key": key, "rule": "delta_non_numeric", "value": val, "action": "drop"})
                continue

        # Rule 9: direct overwrite text field must be string
        # (current_phase already handled by rule 1; these are text fields like
        # gene_lock, physique, spirit, current_status — only strings are valid)
        if key in direct_overwrite_keys and key != "current_phase":
            if not isinstance(val, str):
                violations.append({"key": key, "rule": "overwrite_not_string", "value": type(val).__name__, "action": "drop"})
                continue

        sanitized[key] = val

    return sanitized, violations


def _review_state_update_llm(
    current_state: dict,
    schema: dict,
    original_update: dict,
    sanitized_update: dict,
    violations: list[dict],
    story_id: str = "",
    branch_id: str = "",
) -> dict | None:
    """Ask LLM to produce a repair patch for violated state update keys.

    Returns a merged candidate update (sanitized + patch - drop_keys), or None
    on any failure (timeout, parse error, malformed output).
    """
    from llm_bridge import call_oneshot

    schema_summary_lines = []
    for f in schema.get("fields", []):
        schema_summary_lines.append(f"  {f['key']}: {f.get('type', 'text')}")
    for l in schema.get("lists", []):
        ltype = l.get("type", "list")
        schema_summary_lines.append(f"  {l['key']}: {ltype}")
        if l.get("state_add_key"):
            schema_summary_lines.append(f"    (add: {l['state_add_key']})")
    schema_summary = "\n".join(schema_summary_lines)

    violations_text = json.dumps(violations, ensure_ascii=False, indent=2)

    prompt = (
        "你是 RPG 角色狀態更新的審核員。GM 產生了一份狀態更新，但其中部分欄位違反規則被擋下。\n"
        "請根據被擋下的內容，判斷是否能修正後保留，或者應該丟棄。\n\n"
        f"## 角色 Schema\n{schema_summary}\n\n"
        f"## 合法 current_phase 值\n{json.dumps(sorted(VALID_PHASES), ensure_ascii=False)}\n\n"
        f"## 當前角色狀態（節錄）\n{json.dumps({k: current_state[k] for k in list(current_state)[:15]}, ensure_ascii=False, indent=2)}\n\n"
        f"## 原始更新\n{json.dumps(original_update, ensure_ascii=False, indent=2)}\n\n"
        f"## 已通過驗證的部分\n{json.dumps(sanitized_update, ensure_ascii=False, indent=2)}\n\n"
        f"## 被擋下的違規項目\n{violations_text}\n\n"
        "## 輸出格式（嚴格 JSON）\n"
        "```json\n"
        "{\n"
        '  "patch": {},\n'
        '  "drop_keys": [],\n'
        '  "reason": ""\n'
        "}\n"
        "```\n\n"
        "規則：\n"
        "- patch: 修正後可保留的 key-value（必須符合 schema 型別）\n"
        "- drop_keys: 確定要丟棄的 key（從 sanitized 中移除）\n"
        "- reason: 一句話說明判斷理由\n"
        "- 不要憑空新增原始更新中沒有的 key\n"
        "- 不要輸出 location/threat_level 等場景型 key\n"
        "- 只輸出 JSON，不要任何解釋\n"
    )

    try:
        if not _STATE_REVIEW_LLM_SEM.acquire(blocking=False):
            log.warning(
                "state_reviewer: inflight limit reached (%d), fallback",
                STATE_REVIEW_LLM_MAX_INFLIGHT,
            )
            return None

        # Use a daemon thread so timeout fallback returns immediately without
        # waiting for a potentially stuck provider call to finish.
        result_box: dict = {"result": None, "error": None}

        def _call():
            try:
                t0 = time.time()
                result_box["result"] = call_oneshot(prompt)
                if story_id:
                    _log_llm_usage(
                        story_id,
                        "oneshot_state_review",
                        time.time() - t0,
                        branch_id=branch_id,
                    )
            except Exception as e:
                result_box["error"] = e
            finally:
                _STATE_REVIEW_LLM_SEM.release()

        t = threading.Thread(target=_call, daemon=True)
        t.start()
        t.join(STATE_REVIEW_LLM_TIMEOUT)
        if t.is_alive():
            log.warning("state_reviewer: LLM timeout (%.1fs), fallback", STATE_REVIEW_LLM_TIMEOUT)
            return None

        if result_box["error"] is not None:
            raise result_box["error"]

        result = result_box["result"]

        if not result:
            return None
        result = result.strip()
        if result.startswith("```"):
            lines = result.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            result = "\n".join(lines)

        parsed = json.loads(result)
        if not isinstance(parsed, dict):
            return None

        patch = parsed.get("patch", {})
        drop_keys = parsed.get("drop_keys", [])

        if not isinstance(patch, dict):
            log.warning("state_reviewer: patch is not dict, fallback")
            return None
        if not isinstance(drop_keys, list):
            log.warning("state_reviewer: drop_keys is not list, fallback")
            return None

        # Hard guard: reviewer must not inject keys that weren't present in
        # original update/sanitized update. Keep only in-scope keys.
        allowed_patch_keys = set(original_update.keys()) | set(sanitized_update.keys())
        if patch:
            dropped_patch_keys = [k for k in patch if k not in allowed_patch_keys]
            if dropped_patch_keys:
                log.warning(
                    "state_reviewer: dropped %d out-of-scope patch keys: %s",
                    len(dropped_patch_keys),
                    dropped_patch_keys[:5],
                )
                patch = {k: v for k, v in patch.items() if k in allowed_patch_keys}

        # Build candidate: sanitized + patch - drop_keys
        candidate = dict(sanitized_update)
        candidate.update(patch)
        for k in drop_keys:
            if isinstance(k, str):
                candidate.pop(k, None)

        return candidate

    except Exception as e:
        log.warning("state_reviewer: failed (%s), fallback", e)
        return None


def _run_state_gate(update: dict, schema: dict, current_state: dict,
                    label: str = "state_gate", allow_llm: bool = True,
                    story_id: str = "", branch_id: str = "") -> dict:
    """Run state validation gate and return the update to use.

    Respects STATE_REVIEW_MODE:
    - "off": return update unchanged, no validation
    - "warn": validate + log, but return original update
    - "enforce": validate + log, return sanitized update
      - If STATE_REVIEW_LLM=on and allow_llm=True, also asks LLM reviewer
        to repair violations, then re-validates the result
    """
    if STATE_REVIEW_MODE == "off":
        return update

    sanitized, violations = _validate_state_update(update, schema, current_state)
    if violations:
        log.warning("%s: %d violations: %s",
                    label, len(violations),
                    [(v["key"], v["rule"]) for v in violations])

    if STATE_REVIEW_MODE == "enforce":
        # Phase 2: LLM reviewer (only when there are violations to review)
        if (violations and allow_llm
                and STATE_REVIEW_LLM == "on"):
            candidate = _review_state_update_llm(
                current_state, schema, update, sanitized, violations,
                story_id=story_id, branch_id=branch_id)
            if candidate is not None:
                # Second pass: validate reviewer output
                final, v2 = _validate_state_update(candidate, schema, current_state)
                if v2:
                    log.warning("%s: reviewer output had %d violations, using sanitized",
                                label, len(v2))
                    return sanitized
                log.info("%s: reviewer repaired %d keys", label,
                         len(candidate) - len(sanitized))
                return final
        return sanitized
    return update


def _normalize_state_async(story_id: str, branch_id: str, update: dict, known_keys: set[str]):
    """Background: use LLM to remap non-standard STATE fields, then re-apply."""
    import threading

    unknown = [k for k in update if k not in known_keys]
    if not unknown:
        return

    def _do_normalize():
        from llm_bridge import call_oneshot
        prompt = (
            "你是一個 JSON 欄位正規化工具。以下是一個 RPG 角色狀態更新 JSON，"
            "但某些欄位名稱不符合標準。請將它們映射到正確的標準欄位名。\n\n"
            f"標準欄位：{json.dumps(sorted(known_keys), ensure_ascii=False)}\n\n"
            "映射規則：\n"
            "- 任何表示「獲得道具/裝備」的欄位 → 合併至 inventory（map，道具名為 key，狀態為 value）\n"
            "- 任何表示「失去/消耗道具」的欄位 → 合併至 inventory（map，道具名為 key，value 設為 null）\n"
            "- 任何表示「獎勵點變化」的欄位 → reward_points_delta（整數）\n"
            "- 任何表示「完成任務」的欄位 → completed_missions_add（陣列）\n"
            "- 已經是標準欄位名的保持不變\n"
            "- 無法映射的自訂欄位（如 location, threat_level 等描述性狀態）保持原樣\n\n"
            f"原始 JSON：\n{json.dumps(update, ensure_ascii=False, indent=2)}\n\n"
            "請只輸出正規化後的 JSON，不要任何解釋。"
        )

        try:
            t0 = time.time()
            result = call_oneshot(prompt)
            _log_llm_usage(story_id, "oneshot", time.time() - t0, branch_id=branch_id)
            if not result:
                return
            result = result.strip()
            if result.startswith("```"):
                lines = result.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                result = "\n".join(lines)
            normalized = json.loads(result)
            if not isinstance(normalized, dict):
                return
            # Only re-apply if normalization actually changed something
            if normalized == update:
                return
            log.info("    state_normalize: remapped %d unknown keys, re-applying", len(unknown))
            norm_schema = _load_character_schema(story_id)
            normalized = _run_state_gate(
                normalized, norm_schema,
                _load_character_state(story_id, branch_id),
                label="state_gate(normalize)", allow_llm=False,
                story_id=story_id, branch_id=branch_id)
            _apply_state_update_inner(story_id, branch_id, normalized, norm_schema)
        except Exception as e:
            log.info("    state_normalize: failed (%s), skipping", e)

    t = threading.Thread(target=_do_normalize, daemon=True)
    t.start()


def _extract_tags_async(story_id: str, branch_id: str, gm_text: str, msg_index: int, skip_state: bool = False, skip_time: bool = False):
    """Background: use LLM to extract structured tags (lore/event/npc/state/time) from GM response."""
    if len(gm_text) < 200:
        return

    def _do_extract():
        from llm_bridge import call_oneshot
        from event_db import get_event_titles, get_event_title_map, update_event_status

        try:
            # Collect context for dedup — include both base and branch lore
            toc = get_lore_toc(story_id)
            branch_toc = _get_branch_lore_toc(story_id, branch_id)
            if branch_toc:
                toc += "\n（分支設定）\n" + branch_toc
            lore = _load_lore(story_id)
            branch_lore = _load_branch_lore(story_id, branch_id)
            # Merge topics from both layers for similarity matching
            topic_categories = {e.get("topic", ""): e.get("category", "") for e in lore}
            branch_topic_categories = {e.get("topic", ""): e.get("category", "") for e in branch_lore}
            all_topic_categories = {**topic_categories, **branch_topic_categories}
            user_protected = {e.get("topic", "") for e in lore if e.get("edited_by") == "user"}
            existing_titles = get_event_titles(story_id, branch_id)
            existing_title_map = get_event_title_map(story_id, branch_id)

            # Build schema summary for state extraction
            schema = _load_character_schema(story_id)
            schema_lines = []
            for f in schema.get("fields", []):
                if f.get("type") == "map":
                    schema_lines.append(f"- {f['key']}（{f.get('label', '')}）: map，直接輸出 {{\"key\": \"value\"}} 覆蓋，null 表示移除")
                else:
                    schema_lines.append(f"- {f['key']}（{f.get('label', '')}）: {f.get('type', 'text')}")
            for l in schema.get("lists", []):
                ltype = l.get("type", "list")
                if ltype == "map":
                    schema_lines.append(f"- {l['key']}（{l.get('label', '')}）: map，直接輸出 {{\"key\": \"value\"}} 覆蓋，null 表示移除")
                else:
                    add_k = l.get("state_add_key", "")
                    rm_k = l.get("state_remove_key", "")
                    schema_lines.append(f"- {l['key']}（{l.get('label', '')}）: list，新增用 {add_k}，移除用 {rm_k}")
            schema_summary = "\n".join(schema_lines)

            state = _load_character_state(story_id, branch_id)
            existing_state_keys = ", ".join(sorted(state.keys()))

            # Build current list contents for state extraction context
            list_contents_lines = []
            for l in schema.get("lists", []):
                ltype = l.get("type", "list")
                lkey = l["key"]
                if ltype == "map":
                    items = state.get(lkey, {})
                    if items:
                        list_contents_lines.append(f"{l.get('label', lkey)}：{json.dumps(items, ensure_ascii=False)}")
                else:
                    items = state.get(lkey, [])
                    if items:
                        list_contents_lines.append(f"{l.get('label', lkey)}：{json.dumps(items, ensure_ascii=False)}")
            list_contents_str = "\n".join(list_contents_lines) if list_contents_lines else ""

            titles_str = ", ".join(sorted(existing_titles)) if existing_titles else "（無）"

            prompt = (
                "你是一個 RPG 結構化資料擷取工具。分析以下 GM 回覆，提取結構化資訊。\n\n"
                f"## GM 回覆\n{gm_text}\n\n"
                "## 1. 世界設定（lore）\n"
                "提取**通用世界規則與設定**，這些設定要適用於任何角色、任何分支時間線。\n"
                "**核心判斷標準：GM 在未來的其他場景中是否需要參考這條設定？** 只有「是」才提取。\n"
                "✓ 適合提取：體系或副本的核心規則與運作機制、重要且可重複出現的地點（如總部、主要設施）、商城兌換項目\n"
                "✗ 禁止提取：一次性場景細節（具體房間、走廊、臨時戰場的描述）、"
                "劇情事件的具體過程（交給 events 追蹤）、"
                "角色的個人數值或進度（如「基因鎖進度15%」「獎勵點5740」）、"
                "角色獲得/失去的具體道具、角色習得的功法與技能進度、角色的戰鬥過程與經歷、角色之間的互動劇情\n"
                "**撰寫原則：**\n"
                "- 用通用語氣（「輪迴者可以…」「該能力的效果是…」），**不要提及具體角色名**\n"
                "- 如果已有設定中有密切相關的主題，**更新該條目**（使用完全相同的 topic 名稱）\n"
                "- 每個條目只涵蓋一個具體概念，content 控制在 200-800 字\n"
                f"已有設定（優先更新而非新建）：\n{toc}\n"
                '格式：[{{"category": "分類", "subcategory": "子分類(選填)", "topic": "主題", "content": "完整描述"}}]\n'
                "可用分類：主神設定與規則/體系/商城/副本世界觀/道具/場景/NPC/故事追蹤\n"
                "- 體系：必須填 subcategory。框架級概念用 subcategory 為體系名 + topic「介紹」（如 subcategory「基因鎖」topic「介紹」）；單一技能用 subcategory「技能」；基礎數值用 subcategory「基本屬性」\n"
                "- 副本世界觀：必須填 subcategory 為副本名（如「生化危機」「咒怨」）\n"
                "- 道具：角色可使用的物品與裝備\n\n"
                "## 2. 事件追蹤（events）\n"
                "提取重要事件：伏筆、轉折、戰鬥、發現等。不要記錄瑣碎事件。\n"
                f"已有事件標題：{titles_str}\n"
                "- 新事件：正常輸出\n"
                "- 已有事件狀態變化（如伏筆被觸發、事件被解決）：**重新輸出該事件並更新 status**\n"
                '格式：[{{"event_type": "類型", "title": "標題", "description": "描述", "status": "planted", "tags": "關鍵字"}}]\n'
                "可用類型：伏筆/轉折/遭遇/發現/戰鬥/獲得/觸發\n"
                "可用狀態：planted/triggered/resolved\n\n"
                "## 3. NPC 資料（npcs）\n"
                "提取首次登場或有重大變化的 NPC。\n"
                '格式：[{{"name": "名字", "role": "定位", "appearance": "外觀", '
                '"personality": {{"openness": N, "conscientiousness": N, "extraversion": N, '
                '"agreeableness": N, "neuroticism": N, "summary": "一句話"}}, "backstory": "背景"}}]\n\n'
                "## 4. 角色狀態變化（state）\n"
                f"Schema 告訴你角色有哪些欄位：\n{schema_summary}\n"
                f"角色目前有這些欄位：{existing_state_keys}\n"
                + (f"角色目前的列表內容（含人際關係）：\n{list_contents_str}\n" if list_contents_str else "")
                + "\n規則：\n"
                "- **map 型欄位**（道具欄、人際關係等）：直接輸出 map，同名 key 自動覆蓋\n"
                "  - 道具欄：`inventory: {\"道具名\": \"狀態描述\"}`，進化/變化自動覆蓋同名道具\n"
                "  - 移除道具：`inventory: {\"道具名\": null}`\n"
                "  - 無狀態道具：`inventory: {\"道具名\": \"\"}`\n"
                "- 列表型欄位用 `_add` / `_remove` 後綴（如 `abilities_add`, `abilities_remove`）\n"
                "- 數值型欄位用 `_delta` 後綴（如 `reward_points_delta: -500`）\n"
                "- 文字型欄位直接覆蓋（如 `gene_lock: \"第二階\"`），值要簡短（5-20字）\n"
                "- `current_phase` 只能是：主神空間/副本中/副本結算/傳送中/死亡\n"
                "- `current_dungeon`: 當前所在副本名稱（如「咒術迴戰」「民俗台灣」「鬼滅之刃」）。進入副本時設定，回到主神空間時設為空字串 \"\"。必須與世界設定中的副本分類名一致。\n"
                "- **人際關係**：`relationships: {\"NPC名\": \"新關係描述\"}`。**對照上方的目前關係，如果 GM 文本顯示關係有變化（更親密、敵對、信任等），務必輸出更新**\n"
                "- **體系等級**：`systems: {\"體系名\": \"新等級\"}`。當 GM 文本顯示某體系升級（如 B→A、覺醒、突破等），**必須輸出 systems 更新**，格式為等級 + 新特徵（如 `\"死生之道\": \"A級（漩渦瞳·空間感知）\"`）\n"
                "- 可以新增**永久性角色屬性**（如學會新體系時加 `修真境界`, `法力` 等）\n"
                "- **禁止**新增臨時性/場景性欄位（如 location, threat_level, combat_status, escape_options 等一次性描述）\n"
                '- 角色死亡時 `current_phase` 設為 `"死亡"`，`current_status` 設為 `"end"`\n'
                "\n**道具欄清理原則**（每次提取時都必須遵守）：\n"
                "- **禁止寫入場景/戰鬥狀態**：「戰鬥中」「對峙中」「集結中」「盤旋中」「佔領中」「啟動中」「錄製中」「噴湧中」等臨時狀態不是道具，不要寫入 inventory。這些只是當前回合的敘事描述，下一回合就過時了。\n"
                "- **已消耗/已使用的道具**：設為 null 移除（如 `\"榴彈\": null`）\n"
                "- **已融合到角色/裝備的物品**：不再是獨立道具，設為 null 移除\n"
                "- **召喚物/僕從**：只記錄召喚物的存在、等級和數量（如 `\"僕從軍團\": \"A級模板，約30單位\"`），不要為每個單位的部署狀態各開一條\n"
                "- **隊友的基因鎖/能力狀態**：寫入 relationships，不要寫入 inventory\n"
                "- **道具欄應保持精簡**：如果目前已超過 50 項，優先用 null 清理已消耗、已融合、過時的條目\n"
                "\n**技能列表維護原則**：\n"
                "- **技能升級時必須同時移除舊版本**：用 `abilities_remove` 移除被取代的技能，再用 `abilities_add` 加入新版本。例如「咒靈操術 (C級)」升級為「咒靈操術 (A級)」時，要同時 remove C級版本。\n"
                "- **同一技能的不同描述只保留最新**：如「靈視」「靈視 (解析迷霧)」「靈視·微觀解析」只需保留最高階的一個。\n"
                "- **已被體系（systems）涵蓋的技能不需重複列在 abilities**：如 systems 已有「咒靈操術: A級」，abilities 不需要再列「咒靈操術 (A級)」。\n"
                "格式：只填有變化的欄位。\n\n"
                "## 5. 時間流逝（time）\n"
                "估算這段敘事中經過了多少時間。包含明確跳躍和隱含的時間流逝。\n"
                "- 明確跳躍：「三天後」→ days:3、「那天深夜」→ hours:8、「半個月的苦練」→ days:15\n"
                "- 隱含流逝參考：一場小戰鬥 → hours:1、大型戰役/Boss戰 → hours:3、探索建築/區域 → hours:2、長途移動/趕路 → hours:4、休息/過夜 → hours:8、訓練/修煉 → days:1\n"
                "- 純對話/短暫互動/思考/角色創建/主神空間閒聊不需要輸出。只有場景中有實際行動推進才估算。\n"
                '格式：{"days": N} 或 {"hours": N}（只選一種，優先用 days）\n\n'
                "## 6. 分支標題（branch_title）\n"
                "用 4-8 個中文字總結這段 GM 回覆中**玩家的核心行動或場景轉折**。\n"
                "例如：「七首殺屍測試」「巷道右側突圍」「自省之眼覺醒」「進入蜀山副本」「商城兌換裝備」\n"
                "要求：動作導向、簡潔、不帶標點符號。\n"
                '格式：字串\n\n'
            )

            # Add dungeon progress section if currently in dungeon
            dungeon_progress = _load_dungeon_progress(story_id, branch_id)
            if dungeon_progress and dungeon_progress.get("current_dungeon"):
                current = dungeon_progress["current_dungeon"]
                template = _load_dungeon_template(story_id, current["dungeon_id"])
                if template:
                    # GD-M3: include node title mapping so LLM can match story text to node IDs
                    node_list = template["mainline"]["nodes"]
                    nodes_mapping = ", ".join(
                        [f"{n['id']}=「{n['title']}」" for n in node_list]
                    )
                    areas_str = ", ".join([a["id"] for a in template.get("areas", [])])

                    prompt += (
                        f"## 7. 副本進度（dungeon）\n"
                        f"當前在副本【{template['name']}】中。分析 GM 文本中是否存在：\n"
                        f"- 主線劇情節點的完成（如「成功封印伽椰子」）\n"
                        f"- 新區域的發現或探索（如「進入二樓」、「深入地下室」）\n\n"
                        f"節點 ID 對照（依序）：{nodes_mapping}\n"
                        f"參考區域 ID：{areas_str}\n\n"
                        '格式：\n'
                        '{\n'
                        '  "mainline_progress_delta": 20,  // 主線進度增量（0-100）\n'
                        '  "completed_nodes": ["node_2"],  // 新完成的節點 ID\n'
                        '  "discovered_areas": ["umbrella_lab"],  // 新發現的區域 ID\n'
                        '  "explored_area_updates": {\n'
                        '    "umbrella_lab": 30  // 區域探索度增量（0-100）\n'
                        '  }\n'
                        '}\n\n'
                        "**重要**：\n"
                        "- 如果沒有明顯的劇情節點完成，不要輸出 completed_nodes\n"
                        "- 如果沒有新區域發現，不要輸出 discovered_areas\n"
                        "- 保守估計進度，避免過度推進（GM 可能只是鋪墊，尚未真正完成目標）\n\n"
                    )

            prompt += (
                "## 輸出\n"
                "JSON 物件，只包含有內容的類型：\n"
                '{"lore": [...], "events": [...], "npcs": [...], "state": {...}, "time": {"days": N}, "branch_title": "...", "dungeon": {...}}\n'
                "沒有新資訊的類型省略或用空陣列/空物件。只輸出 JSON。"
            )

            t0 = time.time()
            result = call_oneshot(prompt)
            _log_llm_usage(story_id, "oneshot", time.time() - t0, branch_id=branch_id)
            if not result:
                return
            result = result.strip()
            # Strip markdown code fences if present
            if result.startswith("```"):
                lines = result.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                result = "\n".join(lines)

            try:
                data = json.loads(result)
            except json.JSONDecodeError:
                # Fallback: extract first JSON object from response
                m = re.search(r'\{.*\}', result, re.DOTALL)
                if not m:
                    log.info("    extract_tags: no JSON found in response, skipping")
                    return
                data = json.loads(m.group())
            if not isinstance(data, dict):
                return

            saved_counts = {"lore": 0, "events": 0, "npcs": 0, "state": False}

            # Pistol mode: skip lore + event extraction (NSFW scenes shouldn't persist)
            pistol = get_pistol_mode(_story_dir(story_id), branch_id)
            if pistol:
                log.info("    extract_tags: pistol mode ON, skipping lore + events")

            # Build prefix registry once for the batch (avoids per-entry rebuild)
            prefix_reg = build_prefix_registry(story_id)

            # Lore — save to branch_lore (not base), similarity guard to prevent fragmentation
            for entry in ([] if pistol else data.get("lore", [])):
                topic = entry.get("topic", "").strip()
                category = entry.get("category", "").strip()
                if not topic:
                    continue
                # If exact topic exists in branch or base, upsert handles it.
                # If not, check for a similar existing topic (same category) to merge into.
                if topic not in all_topic_categories:
                    similar = _find_similar_topic(topic, category, all_topic_categories)
                    if similar:
                        log.info("    lore merge: '%s' → '%s'", topic, similar)
                        entry["topic"] = similar
                # Skip if resolved topic is user-edited in base (protect manual edits)
                resolved_topic = entry.get("topic", topic)
                if resolved_topic in user_protected:
                    log.info("    lore skip (user-edited): '%s'", resolved_topic)
                    continue
                entry["edited_by"] = "auto"
                _save_branch_lore_entry(story_id, branch_id, entry, prefix_registry=prefix_reg)
                all_topic_categories[entry.get("topic", topic)] = category
                saved_counts["lore"] += 1

            # Invalidate prefix cache if new lore was saved
            if saved_counts["lore"]:
                invalidate_prefix_cache(story_id)

            # Events — dedup by title, update status if changed
            _STATUS_ORDER = {"planted": 0, "triggered": 1, "resolved": 2, "abandoned": 2}
            for event in ([] if pistol else data.get("events", [])):
                title = event.get("title", "").strip()
                if not title:
                    continue
                if title not in existing_titles:
                    event["message_index"] = msg_index
                    new_id = insert_event(story_id, event, branch_id)
                    existing_titles.add(title)
                    existing_title_map[title] = {"id": new_id, "status": event.get("status", "planted")}
                    saved_counts["events"] += 1
                else:
                    # Update status if it advanced (planted→triggered→resolved)
                    new_status = event.get("status", "").strip()
                    existing = existing_title_map.get(title, {})
                    old_status = existing.get("status", "")
                    event_id = existing.get("id")
                    if (event_id and new_status and new_status != old_status
                            and _STATUS_ORDER.get(new_status, -1) > _STATUS_ORDER.get(old_status, -1)):
                        update_event_status(story_id, event_id, new_status)
                        existing_title_map[title]["status"] = new_status
                        saved_counts["events"] += 1

            # NPCs — _save_npc has built-in merge by name
            for npc in data.get("npcs", []):
                if npc.get("name", "").strip():
                    _save_npc(story_id, npc, branch_id)
                    saved_counts["npcs"] += 1

            # State — apply update (skip if regex already handled STATE tag)
            state_update = data.get("state", {})
            if state_update and isinstance(state_update, dict) and not skip_state:
                _apply_state_update(story_id, branch_id, state_update)
                saved_counts["state"] = True

            # Time — advance world_day (skip if regex already found TIME tags)
            time_data = data.get("time", {})
            if time_data and isinstance(time_data, dict) and not skip_time:
                days = time_data.get("days") or 0
                hours = time_data.get("hours") or 0
                total_days = min(float(days) + float(hours) / 24, 30)
                if total_days > 0:
                    advance_world_day(story_id, branch_id, total_days)
                    saved_counts["time"] = total_days

            # Branch title — save to timeline_tree (set-once: only if no title yet)
            branch_title = data.get("branch_title", "")
            if branch_title and isinstance(branch_title, str):
                branch_title = branch_title.strip()[:20]  # cap at 20 chars
                tree = _load_tree(story_id)
                branch_meta = tree.get("branches", {}).get(branch_id)
                if branch_meta and not branch_meta.get("title"):
                    branch_meta["title"] = branch_title
                    _save_tree(story_id, tree)
                    saved_counts["title"] = branch_title

            # Dungeon progress — update mainline/area progress
            dungeon_data = data.get("dungeon", {})
            if dungeon_data and isinstance(dungeon_data, dict):
                if dungeon_data.get("mainline_progress_delta") or dungeon_data.get("completed_nodes"):
                    update_dungeon_progress(story_id, branch_id, {
                        "progress_delta": dungeon_data.get("mainline_progress_delta", 0),
                        "nodes_completed": dungeon_data.get("completed_nodes", [])
                    })
                    saved_counts["dungeon_progress"] = True
                if dungeon_data.get("discovered_areas") or dungeon_data.get("explored_area_updates"):
                    update_dungeon_area(story_id, branch_id, {
                        "discovered_areas": dungeon_data.get("discovered_areas", []),
                        "explored_area_updates": dungeon_data.get("explored_area_updates", {})
                    })
                    saved_counts["dungeon_area"] = True

            log.info(
                "    extract_tags: saved %d lore, %d events, %d npcs, state %s, time %s, title %s, dungeon %s",
                saved_counts["lore"], saved_counts["events"],
                saved_counts["npcs"],
                "updated" if saved_counts["state"] else "no change",
                f"+{saved_counts['time']:.1f}d" if saved_counts.get("time") else "no change",
                repr(saved_counts.get("title", "—")),
                "updated" if saved_counts.get("dungeon_progress") or saved_counts.get("dungeon_area") else "no change",
            )

            # Trigger periodic lore organization if orphans have accumulated
            if should_organize(story_id):
                organize_lore_async(story_id)

        except json.JSONDecodeError as e:
            log.warning("    extract_tags: JSON parse failed (%s), skipping", e)
        except Exception as e:
            log.exception("    extract_tags: failed, skipping")

    t = threading.Thread(target=_do_extract, daemon=True)
    t.start()


_NORMALIZE_DOTS_RE = re.compile(r'[‧・•]')
_NORMALIZE_DASHES_RE = re.compile(r'[–\-ー]')


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


_ITEM_BASE_RE = re.compile(r'\s*[（(].*$')
_ITEM_QTY_RE = re.compile(r'\s*[x×]\d+$')


def _extract_item_base_name(item: str) -> str:
    """Extract base name from an item string, stripping status/description suffixes.

    Handles formats:
      "道具名 — 描述"     → "道具名"
      "道具名 (狀態)"     → "道具名"
      "道具名（狀態）"    → "道具名"
      "道具名 x3"         → "道具名"
      "道具名 (狀態) x2"  → "道具名"
    """
    name = item.split(" — ")[0].strip()
    name = _ITEM_QTY_RE.sub("", name).strip()
    name = _ITEM_BASE_RE.sub("", name).strip()
    return name


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
        if key.endswith("_delta") and isinstance(update[key], (int, float)):
            base_key = key[:-6]  # strip "_delta"
            current = state.get(base_key)
            if isinstance(current, (int, float)):
                state[base_key] = current + update[key]
            elif base_key == "reward_points":
                state[base_key] = state.get(base_key, 0) + update[key]
            # else: base field is not numeric or doesn't exist — skip delta

    # If GM sets reward_points directly (no delta), accept it.
    # Only applies when delta is absent — delta takes precedence when both present.
    if "reward_points" in update and "reward_points_delta" not in update:
        val = update["reward_points"]
        if isinstance(val, (int, float)):
            state["reward_points"] = int(val)

    # Direct overwrite fields from schema
    for key in schema.get("direct_overwrite_keys", []):
        if key in update:
            state[key] = update[key]

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


def _apply_state_update(story_id: str, branch_id: str, update: dict):
    """Apply a STATE update dict to the branch's character state file.

    1. Runs deterministic validation gate (if enabled)
    2. Immediately applies the (possibly sanitized) update
    3. Validates dungeon growth constraints (hard cap)
    4. Kicks off background LLM normalization for non-standard fields
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

    # Hard constraint validation (dungeon system)
    new_state = _load_character_state(story_id, branch_id)
    validate_dungeon_progression(story_id, branch_id, new_state, old_state)
    _save_json(_story_character_state_path(story_id, branch_id), new_state)

    # Background: normalize non-standard fields and re-apply
    _normalize_state_async(story_id, branch_id, update, _get_schema_known_keys(schema))


# ---------------------------------------------------------------------------
# Helpers — Timeline Tree (story-scoped)
# ---------------------------------------------------------------------------

def get_full_timeline(story_id: str, branch_id: str) -> list[dict]:
    """Reconstruct full message timeline for a branch within a story."""
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    parsed_path = _story_parsed_path(story_id)

    if branch_id not in branches:
        base = _load_json(parsed_path, [])
        for m in base:
            m["owner_branch_id"] = "main"
        return base

    # Build ancestor chain from branch_id up to root
    chain = []
    cur = branch_id
    visited = set()
    while cur is not None and cur not in visited:
        branch = branches.get(cur)
        if not branch:
            break
        visited.add(cur)
        chain.append(branch)
        cur = branch.get("parent_branch_id")
    chain.reverse()

    base = _load_json(parsed_path, [])
    for m in base:
        m["owner_branch_id"] = chain[0]["id"]

    timeline = list(base)

    for branch in chain:
        bp_index = branch.get("branch_point_index")
        if bp_index is not None:
            timeline = [m for m in timeline if m.get("index", 0) <= bp_index]

        delta = _load_json(_story_messages_path(story_id, branch["id"]), [])
        for m in delta:
            m["owner_branch_id"] = branch["id"]
        timeline.extend(delta)

    return timeline


def _resolve_sibling_parent(branches: dict, parent_branch_id: str, branch_point_index: int) -> str:
    """Walk up ancestor chain for sibling detection.

    If branch_point_index <= parent's own branch_point_index, the new branch
    should be a sibling (share grandparent), not a child.
    Prevents linear chains from repeated edit/regen at branch origin.
    """
    current = parent_branch_id
    visited = set()
    while current in branches and current != "main" and current not in visited:
        visited.add(current)
        branch = branches[current]
        parent_bp = branch.get("branch_point_index")
        if parent_bp is not None and branch_point_index <= parent_bp:
            current = branch.get("parent_branch_id", "main") or "main"
        else:
            break
    return current


def _get_fork_points(story_id: str, branch_id: str) -> dict:
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    fork_points = {}

    ancestor_ids = set()
    cur = branch_id
    while cur is not None and cur not in ancestor_ids:
        ancestor_ids.add(cur)
        branch = branches.get(cur)
        if not branch:
            break
        cur = branch.get("parent_branch_id")

    for bid, branch in branches.items():
        if bid == branch_id or branch.get("deleted") or branch.get("blank") or branch.get("merged") or branch.get("pruned"):
            continue
        parent = branch.get("parent_branch_id")
        bp_index = branch.get("branch_point_index")
        if parent in ancestor_ids and bp_index is not None:
            if bp_index not in fork_points:
                fork_points[bp_index] = []
            fork_points[bp_index].append({
                "branch_id": bid,
                "branch_name": branch.get("name", bid),
            })

    return fork_points


def _get_sibling_groups(story_id: str, branch_id: str) -> dict:
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})

    if branch_id not in branches:
        return {}

    ancestor_ids = []
    cur = branch_id
    visited = set()
    while cur is not None and cur not in visited:
        visited.add(cur)
        ancestor_ids.append(cur)
        b = branches.get(cur)
        if not b:
            break
        cur = b.get("parent_branch_id")
    ancestor_ids.reverse()
    ancestor_set = set(ancestor_ids)

    sibling_groups = {}

    fork_map = {}
    for bid, b in branches.items():
        if b.get("deleted") or b.get("blank") or b.get("merged") or b.get("pruned"):
            continue
        parent_id = b.get("parent_branch_id")
        bp_index = b.get("branch_point_index")
        if parent_id is not None and bp_index is not None and parent_id in ancestor_set:
            key = (parent_id, bp_index)
            if key not in fork_map:
                fork_map[key] = []
            fork_map[key].append(b)

    parsed_path = _story_parsed_path(story_id)

    for (parent_id, bp_index), children in fork_map.items():
        children.sort(key=lambda b: b.get("created_at", ""))

        parent_delta = _load_json(_story_messages_path(story_id, parent_id), [])
        parent_has_continuation = any(m.get("index", 0) > bp_index for m in parent_delta)
        if parent_id == "main" and not parent_has_continuation:
            parsed = _load_json(parsed_path, [])
            parent_has_continuation = any(m.get("index", 0) > bp_index for m in parsed)

        variants = []

        if parent_has_continuation:
            variants.append({
                "branch_id": parent_id,
                "label": branches[parent_id].get("name", parent_id),
                "is_current": parent_id in ancestor_set and not any(
                    c["id"] in ancestor_set for c in children
                ),
            })

        for child in children:
            # Skip orphan branches (empty messages from interrupted stream)
            child_msgs = _load_json(_story_messages_path(story_id, child["id"]), [])
            if not child_msgs:
                continue
            variants.append({
                "branch_id": child["id"],
                "label": child.get("name", child["id"]),
                "is_current": child["id"] in ancestor_set,
            })

        if len(variants) >= 2:
            current_variant = 0
            for vi, v in enumerate(variants):
                if v["is_current"]:
                    current_variant = vi + 1
                    break

            divergent_index = bp_index + 1
            sibling_groups[str(divergent_index)] = {
                "current_variant": current_variant,
                "total": len(variants),
                "variants": variants,
            }

    return sibling_groups


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
        delta_msgs = _load_json(_story_messages_path(story_id, bid), [])
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
        _save_json(main_msgs_path, [])


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

_CONTEXT_ECHO_RE = re.compile(
    r"\[(?:命運走向|命運判定|命運骰結果|相關世界設定|相關事件追蹤|NPC 近期動態)\].*?(?=\n---\n|\n\n[^\[\n]|\Z)",
    re.DOTALL,
)

# Pattern to strip fate direction labels from GM text in conversation history
# Matches both full-width【】and half-width[] brackets, with optional ### heading
# and ** bold markers.  Examples:
#   **[命運走向：順遂]**   **【命運判定：大成功】**
#   ### **【命運判定:趙姐的話術真實性】**
#   **[命運判定效果：深度寫入]**   **【命運判定觸發:嚴重失敗】**
_FATE_LABEL_RE = re.compile(
    r"#{0,4}\s*\*{0,2}[【\[](?:命運(?:走向|判定)(?:效應|效果|觸發|結果)?|判定(?:結果)?)[:：][^】\]]*[】\]]\*{0,2}\s*"
)


def _strip_fate_from_messages(messages: list[dict]) -> list[dict]:
    """Return a shallow copy of messages with fate direction labels removed from content.

    Used when fate mode is OFF so the GM doesn't see/mimic historical patterns.
    Does NOT modify the original message dicts.
    """
    cleaned = []
    for m in messages:
        content = m.get("content", "")
        if _FATE_LABEL_RE.search(content):
            m = {**m, "content": _FATE_LABEL_RE.sub("", content).strip()}
        cleaned.append(m)
    return cleaned


_REWARD_HINT_RE = re.compile(r"【主神提示[:：].*?獎勵點.*?】")


def _process_gm_response(gm_response: str, story_id: str, branch_id: str, msg_index: int) -> tuple[str, dict | None, dict]:
    """Extract all hidden tags from GM response. Returns (clean_text, image_info, snapshots)."""
    # Strip context injection sections that the GM may have echoed back
    gm_response = _CONTEXT_ECHO_RE.sub("", gm_response).strip()
    gm_response = re.sub(r"^---\s*", "", gm_response).strip()
    gm_response = re.sub(r"\n---\n", "\n", gm_response).strip()

    # Strip fate direction labels the GM may have output (safety net)
    gm_response = _FATE_LABEL_RE.sub("", gm_response).strip()
    gm_response = re.sub(r"\n{3,}", "\n\n", gm_response)

    # Deduplicate reward point hints — keep only the last occurrence
    reward_hints = list(_REWARD_HINT_RE.finditer(gm_response))
    if len(reward_hints) > 1:
        last_hint = reward_hints[-1].group()
        gm_response = _REWARD_HINT_RE.sub("", gm_response) + "\n\n" + last_hint
        gm_response = re.sub(r"\n{3,}", "\n\n", gm_response).strip()

    gm_response, state_updates = _extract_state_tag(gm_response)
    for state_update in state_updates:
        _apply_state_update(story_id, branch_id, state_update)
    if not state_updates:
        log.info("GM response missing STATE tag (msg_index=%d)", msg_index)

    gm_response, lore_entries = _extract_lore_tag(gm_response)
    for lore_entry in lore_entries:
        lore_entry["source"] = {
            "branch_id": branch_id,
            "msg_index": msg_index,
            "excerpt": gm_response[:100],
            "timestamp": datetime.now().isoformat(),
        }
        lore_entry["edited_by"] = "auto"
        _save_branch_lore_entry(story_id, branch_id, lore_entry)

    gm_response, npc_updates = _extract_npc_tag(gm_response)
    for npc_update in npc_updates:
        _save_npc(story_id, npc_update, branch_id)

    gm_response, event_list = _extract_event_tag(gm_response)
    for event_data in event_list:
        event_data["message_index"] = msg_index
        insert_event(story_id, event_data, branch_id)

    gm_response, img_prompt = _extract_img_tag(gm_response)
    image_info = None
    if img_prompt:
        filename = generate_image_async(story_id, img_prompt, msg_index)
        image_info = {"filename": filename, "ready": False}

    # Extract TIME tags and advance world_day
    had_time_tags = bool(TIME_RE.search(gm_response))
    gm_response = process_time_tags(gm_response, story_id, branch_id)

    # Async post-processing: extract structured data via separate LLM call
    _extract_tags_async(story_id, branch_id, gm_response, msg_index,
                        skip_state=False,
                        skip_time=had_time_tags)

    # Build snapshots for branch forking accuracy
    snapshots = {
        "state_snapshot": _load_character_state(story_id, branch_id),
        "npcs_snapshot": _load_npcs(story_id, branch_id),
        "world_day_snapshot": get_world_day(story_id, branch_id),
        "dungeon_progress_snapshot": get_dungeon_progress_snapshot(story_id, branch_id),
    }

    return gm_response, image_info, snapshots


def _find_state_at_index(story_id: str, branch_id: str, target_index: int) -> dict:
    """Walk timeline backwards to find most recent state_snapshot at or before target_index."""
    timeline = get_full_timeline(story_id, branch_id)
    for msg in reversed(timeline):
        if msg.get("index", 0) > target_index:
            continue
        if "state_snapshot" in msg:
            return msg["state_snapshot"]
    # Fallback: story default → global default
    default_path = _story_default_character_state_path(story_id)
    state = _load_json(default_path, {})
    if not state:
        state = copy.deepcopy(DEFAULT_CHARACTER_STATE)
    return state


def _backfill_forked_state(forked_state: dict, story_id: str, source_branch_id: str):
    """Backfill new fields from source branch's current state into forked snapshot.

    Historical state_snapshots may lack fields added after the snapshot was taken
    (e.g. current_dungeon). Inherit from the source branch's live state.
    """
    if "current_dungeon" not in forked_state:
        source_state = _load_character_state(story_id, source_branch_id)
        forked_state["current_dungeon"] = source_state.get("current_dungeon", "")


def _find_npcs_at_index(story_id: str, branch_id: str, target_index: int) -> list[dict]:
    """Walk timeline backwards to find most recent npcs_snapshot at or before target_index."""
    timeline = get_full_timeline(story_id, branch_id)
    for msg in reversed(timeline):
        if msg.get("index", 0) > target_index:
            continue
        if "npcs_snapshot" in msg:
            return msg["npcs_snapshot"]
    return []


def _find_world_day_at_index(story_id: str, branch_id: str, target_index: int) -> float:
    """Walk timeline backwards to find most recent world_day_snapshot at or before target_index."""
    timeline = get_full_timeline(story_id, branch_id)
    for msg in reversed(timeline):
        if msg.get("index", 0) > target_index:
            continue
        if "world_day_snapshot" in msg:
            return msg["world_day_snapshot"]
    return 0  # default: day 0 (same as world_timer default)


def _build_augmented_message(
    story_id: str, branch_id: str, user_text: str,
    character_state: dict | None = None,
    turn_count: int = 0,
) -> tuple[str, dict | None]:
    """Add lore + events + NPC activities + dice context to user message.

    Returns (augmented_text, dice_result_or_None).
    """
    # Check if this is a blank branch (fresh start — skip story-specific events)
    tree = _load_tree(story_id)
    is_blank = tree.get("branches", {}).get(branch_id, {}).get("blank", False)

    # Build location context for category boosting
    lore_context = None
    if character_state:
        lore_context = {
            "phase": character_state.get("current_phase", ""),
            "status": character_state.get("current_status", ""),
            "dungeon": character_state.get("current_dungeon", ""),
        }

    parts = []
    # Search base lore (via lore.db indexed search)
    lore = search_relevant_lore(story_id, user_text, context=lore_context)
    if lore:
        parts.append(lore)
    # Search branch lore (linear CJK bigram search, smaller dataset)
    branch_lore = _search_branch_lore(story_id, branch_id, user_text, context=lore_context)
    if branch_lore:
        parts.append(branch_lore)
    if not is_blank:
        events = search_relevant_events(story_id, user_text, branch_id, limit=3)
        if events:
            parts.append(events)
    activities = get_recent_activities(story_id, branch_id, limit=2)
    if activities:
        parts.append(activities)

    # Fate roll (skip for /gm commands and when fate mode is off)
    dice_result = None
    story_dir = _story_dir(story_id)
    if character_state and not is_gm_command(user_text) and get_fate_mode(story_dir, branch_id):
        cheat_mod = get_dice_modifier(story_dir, branch_id)
        always_win = get_dice_always_success(story_dir, branch_id)
        dice_result = roll_fate(character_state, cheat_modifier=cheat_mod,
                                always_success=always_win,
                                turn_count=turn_count)
        parts.append(format_dice_context(dice_result))

    if parts:
        return "\n".join(parts) + "\n---\n" + user_text, dice_result
    return user_text, dice_result


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def index():
    import hashlib
    # Cache-busting: hash of static file mtimes
    try:
        mtimes = ""
        for fn in ("app.js", "style.css"):
            p = os.path.join(app.static_folder, fn)
            mtimes += str(int(os.path.getmtime(p)))
        cache_v = hashlib.md5(mtimes.encode()).hexdigest()[:8]
    except OSError:
        cache_v = "1"
    return render_template("index.html", v=cache_v)


@app.route("/api/init", methods=["POST"])
def api_init():
    """Initialise: migrate to stories, parse MD, generate summary if needed."""
    _ensure_data_dir()

    # 1. Stories migration
    _migrate_to_stories()

    story_id = _active_story_id()

    # 2. Design files migration (data/stories/ → story_design/)
    #    Must run BEFORE any code that reads from story_design/ paths.
    _migrate_design_files(story_id)

    # 3. Parse conversation (for original story)
    parsed_path = _story_parsed_path(story_id)
    if not os.path.exists(parsed_path):
        if os.path.exists(CONVERSATION_PATH):
            save_parsed()
            if os.path.exists(LEGACY_PARSED_PATH):
                shutil.copy2(LEGACY_PARSED_PATH, parsed_path)
        else:
            _save_json(parsed_path, [])
    original = _load_json(parsed_path, [])

    # 4. Timeline tree migration
    _migrate_to_timeline_tree(story_id)

    # 4b. Branch files migration (flat → branches/ dirs)
    _migrate_branch_files(story_id)

    # 4c. Schema migration: add abilities field
    _migrate_schema_abilities(story_id)

    tree = _load_tree(story_id)

    # 4. Character state
    active_branch = tree.get("active_branch_id", "main")
    _load_character_state(story_id, active_branch)

    # 5. Ensure main messages file exists
    main_msgs_path = _story_messages_path(story_id, "main")
    if not os.path.exists(main_msgs_path):
        _save_json(main_msgs_path, [])

    # 7. Load story metadata
    registry = _load_stories_registry()
    story_meta = registry.get("stories", {}).get(story_id, {})
    character_schema = _load_character_schema(story_id)

    return jsonify({
        "ok": True,
        "original_count": len(original),
        "active_branch_id": active_branch,
        "active_story_id": story_id,
        "story_name": story_meta.get("name", story_id),
        "character_schema": character_schema,
    })


@app.route("/api/messages")
def api_messages():
    """Return full timeline for a branch + fork points."""
    story_id = _active_story_id()
    branch_id = request.args.get("branch_id", "main")
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 99999, type=int)

    timeline = get_full_timeline(story_id, branch_id)
    original = _load_json(_story_parsed_path(story_id), [])
    original_count = len(original)

    tree = _load_tree(story_id)
    branch_delta = _load_json(_story_messages_path(story_id, branch_id), [])
    delta_indices = {m.get("index") for m in branch_delta}

    for msg in timeline:
        idx = msg.get("index", 0)
        if idx < original_count:
            msg["inherited"] = branch_id != "main"
        else:
            msg["inherited"] = idx not in delta_indices

    total = len(timeline)
    after_index = request.args.get("after_index", None, type=int)
    tail = request.args.get("tail", None, type=int)
    if tail is not None:
        page = timeline[max(0, len(timeline) - tail):]
    elif after_index is not None:
        page = [m for m in timeline if m.get("index", 0) > after_index]
    else:
        page = timeline[offset: offset + limit]
    fork_points = _get_fork_points(story_id, branch_id)
    sibling_groups = _get_sibling_groups(story_id, branch_id)

    result = {
        "messages": page,
        "total": total,
        "offset": offset,
        "original_count": original_count,
        "fork_points": fork_points,
        "sibling_groups": sibling_groups,
        "branch_id": branch_id,
        "world_day": get_world_day(story_id, branch_id),
        "dice_modifier": get_dice_modifier(_story_dir(story_id), branch_id),
    }

    if branch_id.startswith("auto_"):
        state_path = os.path.join(_branch_dir(story_id, branch_id), "auto_play_state.json")
        auto_state = _load_json(state_path, None)
        if not isinstance(auto_state, dict):
            result["live_status"] = "unknown"
        elif auto_state.get("status") == "finished" or auto_state.get("death_detected") or auto_state.get("consecutive_errors", 0) >= 3:
            result["live_status"] = "finished"
        else:
            result["live_status"] = "running"
        result["auto_play_state"] = auto_state
        result["summary_count"] = len(get_summaries(story_id, branch_id))

    # Detect incomplete branch (edit interrupted before GM response saved).
    # Only trigger on leaf nodes. Mid-route user-only nodes can be valid when
    # a child branch continues the conversation.
    branch_meta = tree.get("branches", {}).get(branch_id, {})
    has_active_child = any(
        b.get("parent_branch_id") == branch_id
        and not b.get("deleted")
        and not b.get("merged")
        and not b.get("pruned")
        for b in tree.get("branches", {}).values()
    )
    if (branch_id != "main"
            and not branch_id.startswith("auto_")
            and not branch_meta.get("blank")
            and not branch_meta.get("deleted")
            and not branch_meta.get("merged")
            and not branch_meta.get("pruned")
            and not has_active_child
            and any(m.get("role") == "user" for m in branch_delta)
            and not any(m.get("role") == "gm" for m in branch_delta)):
        parent_id = branch_meta.get("parent_branch_id", "main")
        parent_meta = tree.get("branches", {}).get(parent_id, {})
        result["incomplete"] = {
            "parent_branch_id": parent_id,
            "parent_branch_name": parent_meta.get("name", "主時間線" if parent_id == "main" else parent_id),
        }

    return jsonify(result)


@app.route("/api/send", methods=["POST"])
def api_send():
    """Receive player message, call Claude GM, return response."""
    t_start = time.time()
    body = request.get_json(force=True)
    user_text = body.get("message", "").strip()
    branch_id = body.get("branch_id", "main")
    if not user_text:
        return jsonify({"ok": False, "error": "empty message"}), 400

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    branch = branches.get(branch_id)
    if not branch:
        return jsonify({"ok": False, "error": "branch not found"}), 404

    if _clear_loaded_save_preview(tree):
        _save_tree(story_id, tree)

    log.info("/api/send START  msg=%s branch=%s", user_text[:30], branch_id)

    # 1. Save player message
    t0 = time.time()
    delta_path = _story_messages_path(story_id, branch_id)
    delta_msgs = _load_json(delta_path, [])
    full_timeline = get_full_timeline(story_id, branch_id)

    player_msg = {
        "role": "user",
        "content": user_text,
        "index": len(full_timeline),
    }
    delta_msgs.append(player_msg)
    _save_json(delta_path, delta_msgs)
    full_timeline.append(player_msg)
    log.info("  save_user_msg: %.0fms", (time.time() - t0) * 1000)

    # 1b. Process /gm dice command (金手指) — apply before dice roll
    story_dir = _story_dir(story_id)
    dice_cmd_result = apply_dice_command(story_dir, branch_id, user_text) if is_gm_command(user_text) else None
    if dice_cmd_result:
        log.info("  /gm dice: %s → %s", dice_cmd_result["old"], dice_cmd_result["new"])

    # 2. Build system prompt (with narrative recap)
    t0 = time.time()
    state = _load_character_state(story_id, branch_id)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    recap_text = get_recap_text(story_id, branch_id)
    system_prompt = _build_story_system_prompt(story_id, state_text, branch_id=branch_id, narrative_recap=recap_text)
    log.info("  build_prompt: %.0fms", (time.time() - t0) * 1000)

    # 3. Gather recent context
    recent = full_timeline[-RECENT_MESSAGE_COUNT:]
    if not get_fate_mode(_story_dir(story_id), branch_id):
        recent = _strip_fate_from_messages(recent)

    # 3b. Search relevant lore/events/activities and prepend to user message
    t0 = time.time()
    tc = sum(1 for m in full_timeline if m.get("role") == "user")
    augmented_text, dice_result = _build_augmented_message(story_id, branch_id, user_text, state, turn_count=tc)
    if dice_result:
        player_msg["dice"] = dice_result
        _save_json(delta_path, delta_msgs)
    log.info("  context_search: %.0fms", (time.time() - t0) * 1000)

    # 4. Call Claude (stateless)
    t0 = time.time()
    gm_response, _ = call_claude_gm(
        augmented_text, system_prompt, recent, session_id=None
    )
    gm_elapsed = time.time() - t0
    log.info("  claude_call: %.1fs", gm_elapsed)
    _log_llm_usage(story_id, "gm", gm_elapsed, branch_id=branch_id)

    # 5. Extract all hidden tags (STATE, LORE, NPC, EVENT, IMG)
    t0 = time.time()
    gm_msg_index = len(full_timeline)
    gm_response, image_info, snapshots = _process_gm_response(gm_response, story_id, branch_id, gm_msg_index)
    log.info("  parse_tags: %.0fms", (time.time() - t0) * 1000)

    # 6. Save GM response
    t0 = time.time()
    gm_msg = {
        "role": "gm",
        "content": gm_response,
        "index": gm_msg_index,
    }
    if image_info:
        gm_msg["image"] = image_info
    gm_msg.update(snapshots)
    delta_msgs.append(gm_msg)
    _save_json(delta_path, delta_msgs)
    log.info("  save_gm_msg: %.0fms", (time.time() - t0) * 1000)

    # 7. Trigger NPC evolution if due
    turn_count = sum(1 for m in full_timeline if m.get("role") == "user")
    if _load_npcs(story_id, branch_id) and should_run_evolution(story_id, branch_id, turn_count):
        npc_text = _build_npc_text(story_id, branch_id)
        recent_text = "\n".join(m.get("content", "")[:200] for m in full_timeline[-6:])
        run_npc_evolution_async(story_id, branch_id, turn_count, npc_text, recent_text)

    # 8. Trigger compaction if due
    recap = load_recap(story_id, branch_id)
    if should_compact(recap, len(full_timeline) + 1):
        tl = list(full_timeline) + [gm_msg]
        compact_async(story_id, branch_id, tl)

    # 9. Auto-prune abandoned siblings
    pruned = _auto_prune_siblings(story_id, branch_id, gm_msg_index)

    log.info("/api/send DONE   total=%.1fs", time.time() - t_start)
    return jsonify({"ok": True, "player": player_msg, "gm": gm_msg, "pruned_branches": pruned})


def _sse_event(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.route("/api/send/stream", methods=["POST"])
def api_send_stream():
    """Streaming version of /api/send — returns SSE events."""
    body = request.get_json(force=True)
    user_text = body.get("message", "").strip()
    branch_id = body.get("branch_id", "main")
    if not user_text:
        return Response(_sse_event({"type": "error", "message": "empty message"}),
                        mimetype="text/event-stream")

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    branch = branches.get(branch_id)
    if not branch:
        return Response(_sse_event({"type": "error", "message": "branch not found"}),
                        mimetype="text/event-stream")

    if _clear_loaded_save_preview(tree):
        _save_tree(story_id, tree)

    log.info("/api/send/stream START  msg=%s branch=%s", user_text[:30], branch_id)

    # 1. Save player message (before streaming starts)
    delta_path = _story_messages_path(story_id, branch_id)
    delta_msgs = _load_json(delta_path, [])
    full_timeline = get_full_timeline(story_id, branch_id)

    player_msg = {
        "role": "user",
        "content": user_text,
        "index": len(full_timeline),
    }
    delta_msgs.append(player_msg)
    _save_json(delta_path, delta_msgs)
    full_timeline.append(player_msg)

    # 1b. Process /gm dice command (金手指)
    story_dir = _story_dir(story_id)
    dice_cmd_result = apply_dice_command(story_dir, branch_id, user_text) if is_gm_command(user_text) else None
    if dice_cmd_result:
        log.info("  /gm dice: %s → %s", dice_cmd_result["old"], dice_cmd_result["new"])

    # 2. Build system prompt (with narrative recap)
    state = _load_character_state(story_id, branch_id)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    recap_text = get_recap_text(story_id, branch_id)
    system_prompt = _build_story_system_prompt(story_id, state_text, branch_id=branch_id, narrative_recap=recap_text)

    # 3. Gather recent context
    recent = full_timeline[-RECENT_MESSAGE_COUNT:]
    if not get_fate_mode(_story_dir(story_id), branch_id):
        recent = _strip_fate_from_messages(recent)
    tc = sum(1 for m in full_timeline if m.get("role") == "user")
    augmented_text, dice_result = _build_augmented_message(story_id, branch_id, user_text, state, turn_count=tc)
    if dice_result:
        player_msg["dice"] = dice_result
        _save_json(delta_path, delta_msgs)

    gm_msg_index = len(full_timeline)

    def generate():
        t_start = time.time()
        if dice_result:
            yield _sse_event({"type": "dice", "dice": dice_result})
        try:
            for event_type, payload in call_claude_gm_stream(
                augmented_text, system_prompt, recent, session_id=None
            ):
                if event_type == "text":
                    yield _sse_event({"type": "text", "chunk": payload})
                elif event_type == "error":
                    yield _sse_event({"type": "error", "message": payload})
                    return
                elif event_type == "done":
                    gm_response = payload["response"]
                    _log_llm_usage(story_id, "gm_stream", time.time() - t_start,
                                   branch_id=branch_id, usage=payload.get("usage"))

                    # Extract tags
                    gm_response, image_info, snapshots = _process_gm_response(
                        gm_response, story_id, branch_id, gm_msg_index
                    )

                    # Save GM message
                    gm_msg = {
                        "role": "gm",
                        "content": gm_response,
                        "index": gm_msg_index,
                    }
                    if image_info:
                        gm_msg["image"] = image_info
                    gm_msg.update(snapshots)
                    delta_msgs.append(gm_msg)
                    _save_json(delta_path, delta_msgs)

                    # NPC evolution
                    turn_count = sum(1 for m in full_timeline if m.get("role") == "user")
                    if _load_npcs(story_id, branch_id) and should_run_evolution(story_id, branch_id, turn_count):
                        npc_text = _build_npc_text(story_id, branch_id)
                        recent_text = "\n".join(m.get("content", "")[:200] for m in full_timeline[-6:])
                        run_npc_evolution_async(story_id, branch_id, turn_count, npc_text, recent_text)

                    # Trigger compaction if due
                    recap = load_recap(story_id, branch_id)
                    if should_compact(recap, len(full_timeline) + 1):
                        tl = list(full_timeline) + [gm_msg]
                        compact_async(story_id, branch_id, tl)

                    # Auto-prune abandoned siblings
                    pruned = _auto_prune_siblings(story_id, branch_id, gm_msg_index)

                    tree = _load_tree(story_id)
                    tree["last_played_branch_id"] = branch_id
                    _save_tree(story_id, tree)
                    log.info("/api/send/stream DONE total=%.1fs", time.time() - t_start)
                    yield _sse_event({
                        "type": "done",
                        "gm_msg": gm_msg,
                        "branch": tree["branches"][branch_id],
                        "pruned_branches": pruned,
                    })
        except Exception as e:
            import traceback; log.info("/api/send/stream EXCEPTION %s\n%s", e, traceback.format_exc())
            yield _sse_event({"type": "error", "message": str(e)})

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/status")
def api_status():
    """Return character state for a branch."""
    story_id = _active_story_id()
    branch_id = request.args.get("branch_id", "main")
    tree = _load_tree(story_id)
    active_branch_id = tree.get("active_branch_id", "main")
    loaded_save = _get_loaded_save_preview(story_id, tree, branch_id)

    # Self-heal stale preview metadata (e.g. save deleted after load).
    if branch_id == active_branch_id and tree.get("loaded_save_id") and not loaded_save:
        if _clear_loaded_save_preview(tree):
            _save_tree(story_id, tree)

    if loaded_save:
        state = dict(loaded_save.get("character_snapshot") or _load_character_state(story_id, branch_id))
        state["world_day"] = loaded_save.get("world_day", get_world_day(story_id, branch_id))
        state["loaded_save_id"] = loaded_save.get("id")
    else:
        state = dict(_load_character_state(story_id, branch_id))
        state["world_day"] = get_world_day(story_id, branch_id)

    state["dice_modifier"] = get_dice_modifier(_story_dir(story_id), branch_id)
    state["dice_always_success"] = get_dice_always_success(_story_dir(story_id), branch_id)
    state["pistol_mode"] = get_pistol_mode(_story_dir(story_id), branch_id)
    return jsonify(state)


# ---------------------------------------------------------------------------
# Branch API
# ---------------------------------------------------------------------------

@app.route("/api/branches")
def api_branches():
    """Return all branches (excluding soft-deleted ones)."""
    story_id = _active_story_id()
    tree = _load_tree(story_id)
    visible = {bid: b for bid, b in tree.get("branches", {}).items()
               if not b.get("deleted") and not b.get("merged") and not b.get("pruned")}
    return jsonify({
        "active_branch_id": tree.get("active_branch_id", "main"),
        "last_played_branch_id": tree.get("last_played_branch_id"),
        "branches": visible,
    })


@app.route("/api/branches", methods=["POST"])
def api_branches_create():
    """Create a new branch from a specific message index."""
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    parent_branch_id = body.get("parent_branch_id", "main")
    branch_point_index = body.get("branch_point_index")

    if not name:
        return jsonify({"ok": False, "error": "branch name required"}), 400
    if branch_point_index is None:
        return jsonify({"ok": False, "error": "branch_point_index required"}), 400

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    source_branch_id = parent_branch_id  # preserve for branch-level config copy
    parent_branch_id = _resolve_sibling_parent(branches, parent_branch_id, branch_point_index)

    if parent_branch_id not in branches:
        return jsonify({"ok": False, "error": "parent branch not found"}), 404

    branch_id = f"branch_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    forked_state = _find_state_at_index(story_id, parent_branch_id, branch_point_index)
    _backfill_forked_state(forked_state, story_id, source_branch_id)
    _save_json(_story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = _find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    _save_json(_story_npcs_path(story_id, branch_id), forked_npcs)
    _save_branch_config(story_id, branch_id, _load_branch_config(story_id, source_branch_id))
    copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    forked_world_day = _find_world_day_at_index(story_id, parent_branch_id, branch_point_index)
    set_world_day(story_id, branch_id, forked_world_day)
    copy_cheats(_story_dir(story_id), source_branch_id, branch_id)
    # Copy branch lore from source (child inherits parent's branch-specific lore)
    _src_bl = _load_branch_lore(story_id, source_branch_id)
    if _src_bl:
        _save_branch_lore(story_id, branch_id, _src_bl)
    copy_dungeon_progress(story_id, parent_branch_id, branch_id)

    _save_json(_story_messages_path(story_id, branch_id), [])

    branches[branch_id] = {
        "id": branch_id,
        "name": name,
        "parent_branch_id": parent_branch_id,
        "branch_point_index": branch_point_index,
        "created_at": now,
        "session_id": None,
        "character_state_file": f"character_state_{branch_id}.json",
    }
    tree["active_branch_id"] = branch_id
    _clear_loaded_save_preview(tree)
    _save_tree(story_id, tree)

    return jsonify({"ok": True, "branch": branches[branch_id]})


@app.route("/api/branches/blank", methods=["POST"])
def api_branches_blank():
    """Create a blank branch with no inherited messages (fresh game start)."""
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "branch name required"}), 400

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})

    branch_id = f"branch_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    # Use blank character state (placeholder from schema)
    _save_json(_story_character_state_path(story_id, branch_id), _blank_character_state(story_id))

    # Empty NPCs and messages
    _save_json(_story_npcs_path(story_id, branch_id), [])
    _save_json(_story_messages_path(story_id, branch_id), [])

    # Initialize blank dungeon progress (no history)
    from dungeon_system import _save_dungeon_progress
    _save_dungeon_progress(story_id, branch_id, {
        "history": [],
        "current_dungeon": None,
        "total_dungeons_completed": 0
    })

    # Copy branch config from main
    _save_branch_config(story_id, branch_id, _load_branch_config(story_id, "main"))

    branches[branch_id] = {
        "id": branch_id,
        "name": name,
        "parent_branch_id": "main",
        "branch_point_index": -1,  # magic value: inherit nothing
        "created_at": now,
        "session_id": None,
        "blank": True,
    }
    tree["active_branch_id"] = branch_id
    _clear_loaded_save_preview(tree)
    _save_tree(story_id, tree)

    return jsonify({"ok": True, "branch": branches[branch_id]})


@app.route("/api/branches/switch", methods=["POST"])
def api_branches_switch():
    """Switch active branch."""
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "main")

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    if branch_id not in branches:
        return jsonify({"ok": False, "error": "branch not found"}), 404
    branch = branches[branch_id]
    if branch.get("deleted") or branch.get("merged") or branch.get("pruned"):
        return jsonify({"ok": False, "error": "cannot switch to inactive branch"}), 400

    # For promoted mainline, selecting an ancestor node should continue
    # to the deepest active leaf on that same route.
    mainline_leaf = tree.get("promoted_mainline_leaf_id")
    if mainline_leaf in branches:
        chain = []
        cur = mainline_leaf
        seen = set()
        while cur is not None and cur not in seen and cur in branches:
            seen.add(cur)
            chain.append(cur)
            cur = branches[cur].get("parent_branch_id")
        chain_set = set(chain)
        if branch_id in chain_set:
            current = branch_id
            visited = set()
            while current not in visited:
                visited.add(current)
                children = [
                    bid for bid, b in branches.items()
                    if b.get("parent_branch_id") == current
                    and not b.get("deleted")
                    and not b.get("merged")
                    and not b.get("pruned")
                    and bid in chain_set
                ]
                if len(children) != 1:
                    break
                current = children[0]
            branch_id = current

    tree["active_branch_id"] = branch_id
    _clear_loaded_save_preview(tree)
    _save_tree(story_id, tree)

    return jsonify({"ok": True, "active_branch_id": branch_id})


@app.route("/api/branches/<branch_id>", methods=["PATCH"])
def api_branches_rename(branch_id):
    """Rename a branch."""
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    if branch_id not in tree.get("branches", {}):
        return jsonify({"ok": False, "error": "branch not found"}), 404

    tree["branches"][branch_id]["name"] = name
    _save_tree(story_id, tree)

    return jsonify({"ok": True, "branch": tree["branches"][branch_id]})


@app.route("/api/branches/<branch_id>/config", methods=["GET"])
def api_branch_config_get(branch_id):
    """Get branch config."""
    story_id = _active_story_id()
    config = _load_branch_config(story_id, branch_id)
    return jsonify({"ok": True, "config": config})


@app.route("/api/branches/<branch_id>/config", methods=["POST"])
def api_branch_config_set(branch_id):
    """Update branch config (merges with existing)."""
    story_id = _active_story_id()
    config = _load_branch_config(story_id, branch_id)
    body = request.get_json(force=True)
    config.update(body)
    _save_branch_config(story_id, branch_id, config)
    return jsonify({"ok": True, "config": config})


@app.route("/api/branches/edit", methods=["POST"])
def api_branches_edit():
    """Edit a user message: create a branch, save edited message, call Claude."""
    t_start = time.time()
    body = request.get_json(force=True)
    parent_branch_id = body.get("parent_branch_id", "main")
    branch_point_index = body.get("branch_point_index")
    edited_message = body.get("edited_message", "").strip()

    if branch_point_index is None:
        return jsonify({"ok": False, "error": "branch_point_index required"}), 400
    if not edited_message:
        return jsonify({"ok": False, "error": "edited_message required"}), 400

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    source_branch_id = parent_branch_id  # preserve for branch-level config copy

    # No-change guard: reject if edited message is identical to original
    edit_target_index = branch_point_index + 1
    timeline = get_full_timeline(story_id, source_branch_id)
    original_msg = next(
        (m for m in timeline
         if m.get("index") == edit_target_index and m.get("role") == "user"),
        None,
    )
    if original_msg and original_msg.get("content", "").strip() == edited_message:
        return jsonify({"ok": False, "error": "no_change"}), 400

    parent_branch_id = _resolve_sibling_parent(branches, parent_branch_id, branch_point_index)
    if parent_branch_id not in branches:
        return jsonify({"ok": False, "error": "parent branch not found"}), 404

    log.info("/api/branches/edit START  msg=%s", edited_message[:30])

    name = edited_message[:15].strip()
    if len(edited_message) > 15:
        name += "…"

    branch_id = f"branch_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    forked_state = _find_state_at_index(story_id, parent_branch_id, branch_point_index)
    _backfill_forked_state(forked_state, story_id, source_branch_id)
    _save_json(_story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = _find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    _save_json(_story_npcs_path(story_id, branch_id), forked_npcs)
    _save_branch_config(story_id, branch_id, _load_branch_config(story_id, source_branch_id))
    copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    forked_world_day = _find_world_day_at_index(story_id, parent_branch_id, branch_point_index)
    set_world_day(story_id, branch_id, forked_world_day)
    copy_cheats(_story_dir(story_id), source_branch_id, branch_id)
    # Copy branch lore from source (child inherits parent's branch-specific lore)
    _src_bl = _load_branch_lore(story_id, source_branch_id)
    if _src_bl:
        _save_branch_lore(story_id, branch_id, _src_bl)
    copy_dungeon_progress(story_id, parent_branch_id, branch_id)

    user_msg_index = branch_point_index + 1
    gm_msg_index = branch_point_index + 2

    user_msg = {
        "role": "user",
        "content": edited_message,
        "index": user_msg_index,
    }
    delta = [user_msg]
    _save_json(_story_messages_path(story_id, branch_id), delta)

    branches[branch_id] = {
        "id": branch_id,
        "name": name,
        "parent_branch_id": parent_branch_id,
        "branch_point_index": branch_point_index,
        "created_at": now,
        "session_id": None,
        "character_state_file": f"character_state_{branch_id}.json",
    }
    tree["active_branch_id"] = branch_id
    _clear_loaded_save_preview(tree)
    _save_tree(story_id, tree)

    t0 = time.time()
    full_timeline = get_full_timeline(story_id, branch_id)
    state = _load_character_state(story_id, branch_id)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    recap_text = get_recap_text(story_id, branch_id)
    system_prompt = _build_story_system_prompt(story_id, state_text, branch_id=branch_id, narrative_recap=recap_text)
    recent = full_timeline[-RECENT_MESSAGE_COUNT:]
    if not get_fate_mode(_story_dir(story_id), branch_id):
        recent = _strip_fate_from_messages(recent)
    log.info("  build_prompt: %.0fms", (time.time() - t0) * 1000)

    t0 = time.time()
    tc = sum(1 for m in full_timeline if m.get("role") == "user")
    augmented_edit, dice_result = _build_augmented_message(story_id, branch_id, edited_message, state, turn_count=tc)
    if dice_result:
        user_msg["dice"] = dice_result
        _save_json(_story_messages_path(story_id, branch_id), delta)
    log.info("  context_search: %.0fms", (time.time() - t0) * 1000)

    t0 = time.time()
    try:
        gm_response, _ = call_claude_gm(
            augmented_edit, system_prompt, recent, session_id=None
        )
    except Exception as e:
        log.info("/api/branches/edit EXCEPTION %s", e)
        _cleanup_branch(story_id, branch_id)
        return jsonify({"ok": False, "error": str(e)}), 500
    edit_elapsed = time.time() - t0
    log.info("  claude_call: %.1fs", edit_elapsed)
    _log_llm_usage(story_id, "gm", edit_elapsed, branch_id=branch_id)

    gm_response, image_info, snapshots = _process_gm_response(gm_response, story_id, branch_id, gm_msg_index)

    gm_msg = {
        "role": "gm",
        "content": gm_response,
        "index": gm_msg_index,
    }
    if image_info:
        gm_msg["image"] = image_info
    gm_msg.update(snapshots)
    delta.append(gm_msg)
    _save_json(_story_messages_path(story_id, branch_id), delta)

    # Trigger compaction if due
    recap = load_recap(story_id, branch_id)
    if should_compact(recap, len(full_timeline) + 1):
        tl = list(full_timeline) + [gm_msg]
        compact_async(story_id, branch_id, tl)

    log.info("/api/branches/edit DONE   total=%.1fs", time.time() - t_start)
    return jsonify({
        "ok": True,
        "branch": tree["branches"][branch_id],
        "user_msg": user_msg,
        "gm_msg": gm_msg,
    })


@app.route("/api/branches/edit/stream", methods=["POST"])
def api_branches_edit_stream():
    """Streaming version of /api/branches/edit — returns SSE events."""
    body = request.get_json(force=True)
    parent_branch_id = body.get("parent_branch_id", "main")
    branch_point_index = body.get("branch_point_index")
    edited_message = body.get("edited_message", "").strip()

    if branch_point_index is None:
        return Response(_sse_event({"type": "error", "message": "branch_point_index required"}),
                        mimetype="text/event-stream")
    if not edited_message:
        return Response(_sse_event({"type": "error", "message": "edited_message required"}),
                        mimetype="text/event-stream")

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    source_branch_id = parent_branch_id  # preserve for branch-level config copy

    # No-change guard: reject if edited message is identical to original
    edit_target_index = branch_point_index + 1
    timeline = get_full_timeline(story_id, source_branch_id)
    original_msg = next(
        (m for m in timeline
         if m.get("index") == edit_target_index and m.get("role") == "user"),
        None,
    )
    if original_msg and original_msg.get("content", "").strip() == edited_message:
        return Response(_sse_event({"type": "error", "message": "no_change"}),
                        mimetype="text/event-stream")

    parent_branch_id = _resolve_sibling_parent(branches, parent_branch_id, branch_point_index)
    if parent_branch_id not in branches:
        return Response(_sse_event({"type": "error", "message": "parent branch not found"}),
                        mimetype="text/event-stream")

    log.info("/api/branches/edit/stream START  msg=%s", edited_message[:30])

    # Create branch (same as non-streaming)
    name = edited_message[:15].strip()
    if len(edited_message) > 15:
        name += "…"

    branch_id = f"branch_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    forked_state = _find_state_at_index(story_id, parent_branch_id, branch_point_index)
    _backfill_forked_state(forked_state, story_id, source_branch_id)
    _save_json(_story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = _find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    _save_json(_story_npcs_path(story_id, branch_id), forked_npcs)
    _save_branch_config(story_id, branch_id, _load_branch_config(story_id, source_branch_id))
    copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    forked_world_day = _find_world_day_at_index(story_id, parent_branch_id, branch_point_index)
    set_world_day(story_id, branch_id, forked_world_day)
    copy_cheats(_story_dir(story_id), source_branch_id, branch_id)
    # Copy branch lore from source (child inherits parent's branch-specific lore)
    _src_bl = _load_branch_lore(story_id, source_branch_id)
    if _src_bl:
        _save_branch_lore(story_id, branch_id, _src_bl)
    copy_dungeon_progress(story_id, parent_branch_id, branch_id)

    user_msg_index = branch_point_index + 1
    gm_msg_index = branch_point_index + 2

    user_msg = {
        "role": "user",
        "content": edited_message,
        "index": user_msg_index,
    }
    delta = [user_msg]
    _save_json(_story_messages_path(story_id, branch_id), delta)

    branches[branch_id] = {
        "id": branch_id,
        "name": name,
        "parent_branch_id": parent_branch_id,
        "branch_point_index": branch_point_index,
        "created_at": now,
        "session_id": None,
        "character_state_file": f"character_state_{branch_id}.json",
    }
    tree["active_branch_id"] = branch_id
    tree["last_played_branch_id"] = branch_id
    _clear_loaded_save_preview(tree)
    _save_tree(story_id, tree)

    # Build prompt context
    full_timeline = get_full_timeline(story_id, branch_id)
    state = _load_character_state(story_id, branch_id)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    recap_text = get_recap_text(story_id, branch_id)
    system_prompt = _build_story_system_prompt(story_id, state_text, branch_id=branch_id, narrative_recap=recap_text)
    recent = full_timeline[-RECENT_MESSAGE_COUNT:]
    if not get_fate_mode(_story_dir(story_id), branch_id):
        recent = _strip_fate_from_messages(recent)
    tc = sum(1 for m in full_timeline if m.get("role") == "user")
    augmented_edit, dice_result = _build_augmented_message(story_id, branch_id, edited_message, state, turn_count=tc)
    if dice_result:
        user_msg["dice"] = dice_result
        _save_json(_story_messages_path(story_id, branch_id), delta)

    def generate():
        t_start = time.time()
        if dice_result:
            yield _sse_event({"type": "dice", "dice": dice_result})
        try:
            for event_type, payload in call_claude_gm_stream(
                augmented_edit, system_prompt, recent, session_id=None
            ):
                if event_type == "text":
                    yield _sse_event({"type": "text", "chunk": payload})
                elif event_type == "error":
                    _cleanup_branch(story_id, branch_id)
                    yield _sse_event({"type": "error", "message": payload})
                    return
                elif event_type == "done":
                    gm_response = payload["response"]
                    _log_llm_usage(story_id, "gm_stream", time.time() - t_start,
                                   branch_id=branch_id, usage=payload.get("usage"))

                    gm_response, image_info, snapshots = _process_gm_response(
                        gm_response, story_id, branch_id, gm_msg_index
                    )

                    gm_msg = {
                        "role": "gm",
                        "content": gm_response,
                        "index": gm_msg_index,
                    }
                    if image_info:
                        gm_msg["image"] = image_info
                    gm_msg.update(snapshots)
                    delta.append(gm_msg)
                    _save_json(_story_messages_path(story_id, branch_id), delta)

                    # Trigger compaction if due
                    recap = load_recap(story_id, branch_id)
                    if should_compact(recap, len(full_timeline) + 1):
                        tl = list(full_timeline) + [gm_msg]
                        compact_async(story_id, branch_id, tl)

                    log.info("/api/branches/edit/stream DONE total=%.1fs", time.time() - t_start)
                    yield _sse_event({
                        "type": "done",
                        "branch": tree["branches"][branch_id],
                        "user_msg": user_msg,
                        "gm_msg": gm_msg,
                    })
        except Exception as e:
            import traceback; log.info("/api/branches/edit/stream EXCEPTION %s\n%s", e, traceback.format_exc())
            _cleanup_branch(story_id, branch_id)
            yield _sse_event({"type": "error", "message": str(e)})
        finally:
            # If generator is GC'd mid-stream (client disconnect), clean up empty branch
            delta_now = _load_json(_story_messages_path(story_id, branch_id), [])
            has_gm = any(m.get("role") == "gm" for m in delta_now)
            if not has_gm:
                log.info("/api/branches/edit/stream cleanup orphan branch %s", branch_id)
                _cleanup_branch(story_id, branch_id)

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/branches/regenerate", methods=["POST"])
def api_branches_regenerate():
    """Regenerate a GM message: create a branch, call Claude, save new response."""
    t_start = time.time()
    body = request.get_json(force=True)
    parent_branch_id = body.get("parent_branch_id", "main")
    branch_point_index = body.get("branch_point_index")

    if branch_point_index is None:
        return jsonify({"ok": False, "error": "branch_point_index required"}), 400

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    source_branch_id = parent_branch_id  # preserve for branch-level config copy
    parent_branch_id = _resolve_sibling_parent(branches, parent_branch_id, branch_point_index)
    if parent_branch_id not in branches:
        return jsonify({"ok": False, "error": "parent branch not found"}), 404

    parent_timeline = get_full_timeline(story_id, parent_branch_id)
    user_msg_content = ""
    for msg in parent_timeline:
        if msg.get("index") == branch_point_index:
            user_msg_content = msg.get("content", "")
            break

    log.info("/api/branches/regenerate START  idx=%s", branch_point_index)

    name = "Re: " + user_msg_content[:12].strip()
    if len(user_msg_content) > 12:
        name += "…"

    branch_id = f"branch_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    forked_state = _find_state_at_index(story_id, parent_branch_id, branch_point_index)
    _backfill_forked_state(forked_state, story_id, source_branch_id)
    _save_json(_story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = _find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    _save_json(_story_npcs_path(story_id, branch_id), forked_npcs)
    _save_branch_config(story_id, branch_id, _load_branch_config(story_id, source_branch_id))
    copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    forked_world_day = _find_world_day_at_index(story_id, parent_branch_id, branch_point_index)
    set_world_day(story_id, branch_id, forked_world_day)
    copy_cheats(_story_dir(story_id), source_branch_id, branch_id)
    # Copy branch lore from source (child inherits parent's branch-specific lore)
    _src_bl = _load_branch_lore(story_id, source_branch_id)
    if _src_bl:
        _save_branch_lore(story_id, branch_id, _src_bl)
    copy_dungeon_progress(story_id, parent_branch_id, branch_id)

    _save_json(_story_messages_path(story_id, branch_id), [])

    branches[branch_id] = {
        "id": branch_id,
        "name": name,
        "parent_branch_id": parent_branch_id,
        "branch_point_index": branch_point_index,
        "created_at": now,
        "session_id": None,
        "character_state_file": f"character_state_{branch_id}.json",
    }
    tree["active_branch_id"] = branch_id
    _clear_loaded_save_preview(tree)
    _save_tree(story_id, tree)

    t0 = time.time()
    full_timeline = get_full_timeline(story_id, branch_id)
    state = _load_character_state(story_id, branch_id)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    recap_text = get_recap_text(story_id, branch_id)
    system_prompt = _build_story_system_prompt(story_id, state_text, branch_id=branch_id, narrative_recap=recap_text)
    recent = full_timeline[-RECENT_MESSAGE_COUNT:]
    if not get_fate_mode(_story_dir(story_id), branch_id):
        recent = _strip_fate_from_messages(recent)
    log.info("  build_prompt: %.0fms", (time.time() - t0) * 1000)

    t0 = time.time()
    tc = sum(1 for m in full_timeline if m.get("role") == "user")
    augmented_regen, dice_result = _build_augmented_message(story_id, branch_id, user_msg_content, state, turn_count=tc)
    log.info("  context_search: %.0fms", (time.time() - t0) * 1000)

    t0 = time.time()
    try:
        gm_response, _ = call_claude_gm(
            augmented_regen, system_prompt, recent, session_id=None
        )
    except Exception as e:
        log.info("/api/branches/regenerate EXCEPTION %s", e)
        _cleanup_branch(story_id, branch_id)
        return jsonify({"ok": False, "error": str(e)}), 500
    regen_elapsed = time.time() - t0
    log.info("  claude_call: %.1fs", regen_elapsed)
    _log_llm_usage(story_id, "gm", regen_elapsed, branch_id=branch_id)

    gm_msg_index = branch_point_index + 1
    gm_response, image_info, snapshots = _process_gm_response(gm_response, story_id, branch_id, gm_msg_index)

    gm_msg = {
        "role": "gm",
        "content": gm_response,
        "index": gm_msg_index,
    }
    if image_info:
        gm_msg["image"] = image_info
    if dice_result:
        gm_msg["dice"] = dice_result
    gm_msg.update(snapshots)
    _save_json(_story_messages_path(story_id, branch_id), [gm_msg])

    # Trigger compaction if due
    recap = load_recap(story_id, branch_id)
    if should_compact(recap, len(full_timeline) + 1):
        tl = list(full_timeline) + [gm_msg]
        compact_async(story_id, branch_id, tl)

    log.info("/api/branches/regenerate DONE   total=%.1fs", time.time() - t_start)
    return jsonify({
        "ok": True,
        "branch": tree["branches"][branch_id],
        "gm_msg": gm_msg,
    })


@app.route("/api/branches/regenerate/stream", methods=["POST"])
def api_branches_regenerate_stream():
    """Streaming version of /api/branches/regenerate — returns SSE events."""
    body = request.get_json(force=True)
    parent_branch_id = body.get("parent_branch_id", "main")
    branch_point_index = body.get("branch_point_index")

    if branch_point_index is None:
        return Response(_sse_event({"type": "error", "message": "branch_point_index required"}),
                        mimetype="text/event-stream")

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    source_branch_id = parent_branch_id  # preserve for branch-level config copy
    parent_branch_id = _resolve_sibling_parent(branches, parent_branch_id, branch_point_index)
    if parent_branch_id not in branches:
        return Response(_sse_event({"type": "error", "message": "parent branch not found"}),
                        mimetype="text/event-stream")

    parent_timeline = get_full_timeline(story_id, parent_branch_id)
    user_msg_content = ""
    for msg in parent_timeline:
        if msg.get("index") == branch_point_index:
            user_msg_content = msg.get("content", "")
            break

    log.info("/api/branches/regenerate/stream START  idx=%s", branch_point_index)

    # Create branch (same as non-streaming)
    name = "Re: " + user_msg_content[:12].strip()
    if len(user_msg_content) > 12:
        name += "…"

    branch_id = f"branch_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    forked_state = _find_state_at_index(story_id, parent_branch_id, branch_point_index)
    _backfill_forked_state(forked_state, story_id, source_branch_id)
    _save_json(_story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = _find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    _save_json(_story_npcs_path(story_id, branch_id), forked_npcs)
    _save_branch_config(story_id, branch_id, _load_branch_config(story_id, source_branch_id))
    copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    forked_world_day = _find_world_day_at_index(story_id, parent_branch_id, branch_point_index)
    set_world_day(story_id, branch_id, forked_world_day)
    copy_cheats(_story_dir(story_id), source_branch_id, branch_id)
    # Copy branch lore from source (child inherits parent's branch-specific lore)
    _src_bl = _load_branch_lore(story_id, source_branch_id)
    if _src_bl:
        _save_branch_lore(story_id, branch_id, _src_bl)
    copy_dungeon_progress(story_id, parent_branch_id, branch_id)
    _save_json(_story_messages_path(story_id, branch_id), [])

    branches[branch_id] = {
        "id": branch_id,
        "name": name,
        "parent_branch_id": parent_branch_id,
        "branch_point_index": branch_point_index,
        "created_at": now,
        "session_id": None,
        "character_state_file": f"character_state_{branch_id}.json",
    }
    tree["active_branch_id"] = branch_id
    tree["last_played_branch_id"] = branch_id
    _clear_loaded_save_preview(tree)
    _save_tree(story_id, tree)

    # Build prompt context
    full_timeline = get_full_timeline(story_id, branch_id)
    state = _load_character_state(story_id, branch_id)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    recap_text = get_recap_text(story_id, branch_id)
    system_prompt = _build_story_system_prompt(story_id, state_text, branch_id=branch_id, narrative_recap=recap_text)
    recent = full_timeline[-RECENT_MESSAGE_COUNT:]
    if not get_fate_mode(_story_dir(story_id), branch_id):
        recent = _strip_fate_from_messages(recent)
    tc = sum(1 for m in full_timeline if m.get("role") == "user")
    augmented_regen, dice_result = _build_augmented_message(story_id, branch_id, user_msg_content, state, turn_count=tc)

    gm_msg_index = branch_point_index + 1

    def generate():
        t_start = time.time()
        if dice_result:
            yield _sse_event({"type": "dice", "dice": dice_result})
        try:
            for event_type, payload in call_claude_gm_stream(
                augmented_regen, system_prompt, recent, session_id=None
            ):
                if event_type == "text":
                    yield _sse_event({"type": "text", "chunk": payload})
                elif event_type == "error":
                    _cleanup_branch(story_id, branch_id)
                    yield _sse_event({"type": "error", "message": payload})
                    return
                elif event_type == "done":
                    gm_response = payload["response"]
                    _log_llm_usage(story_id, "gm_stream", time.time() - t_start,
                                   branch_id=branch_id, usage=payload.get("usage"))

                    gm_response, image_info, snapshots = _process_gm_response(
                        gm_response, story_id, branch_id, gm_msg_index
                    )

                    gm_msg = {
                        "role": "gm",
                        "content": gm_response,
                        "index": gm_msg_index,
                    }
                    if image_info:
                        gm_msg["image"] = image_info
                    if dice_result:
                        gm_msg["dice"] = dice_result
                    gm_msg.update(snapshots)
                    _save_json(_story_messages_path(story_id, branch_id), [gm_msg])

                    # Trigger compaction if due
                    recap = load_recap(story_id, branch_id)
                    if should_compact(recap, len(full_timeline) + 1):
                        tl = list(full_timeline) + [gm_msg]
                        compact_async(story_id, branch_id, tl)

                    log.info("/api/branches/regenerate/stream DONE total=%.1fs", time.time() - t_start)
                    yield _sse_event({
                        "type": "done",
                        "branch": tree["branches"][branch_id],
                        "gm_msg": gm_msg,
                    })
        except Exception as e:
            import traceback; log.info("/api/branches/regenerate/stream EXCEPTION %s\n%s", e, traceback.format_exc())
            _cleanup_branch(story_id, branch_id)
            yield _sse_event({"type": "error", "message": str(e)})
        finally:
            # If generator is GC'd mid-stream (client disconnect), clean up empty branch
            msgs = _load_json(_story_messages_path(story_id, branch_id), [])
            if not msgs:
                log.info("/api/branches/regenerate/stream cleanup orphan branch %s", branch_id)
                _cleanup_branch(story_id, branch_id)

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/branches/promote", methods=["POST"])
def api_branches_promote():
    """Set branch as main timeline by keeping only root→target lineage."""
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "").strip()

    if not branch_id or branch_id == "main":
        return jsonify({"ok": False, "error": "invalid branch_id"}), 400

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})

    if branch_id not in branches:
        return jsonify({"ok": False, "error": "branch not found"}), 404

    if branches[branch_id].get("deleted"):
        return jsonify({"ok": False, "error": "cannot promote a deleted branch"}), 400
    if branches[branch_id].get("merged"):
        return jsonify({"ok": False, "error": "cannot promote a merged branch"}), 400
    if branches[branch_id].get("pruned"):
        return jsonify({"ok": False, "error": "cannot promote a pruned branch"}), 400

    # Build lineage from target branch upward.
    # If a blank root is encountered, stop there and do not climb to global main.
    ancestor_chain_reverse = []
    cur = branch_id
    visited = set()
    stopped_at_blank_root = False
    while cur is not None and cur not in visited:
        b = branches.get(cur)
        if not b:
            break
        visited.add(cur)
        ancestor_chain_reverse.append(cur)
        if b.get("blank"):
            stopped_at_blank_root = True
            break
        cur = b.get("parent_branch_id")
    ancestor_chain = list(reversed(ancestor_chain_reverse))
    keep_ids = set(ancestor_chain)

    # Remove parent-continuation alternatives along kept lineage so promote
    # results in a single route with no sibling switch variants.
    for i in range(1, len(ancestor_chain)):
        parent_id = ancestor_chain[i - 1]
        child_id = ancestor_chain[i]
        child_bp = branches.get(child_id, {}).get("branch_point_index")
        if child_bp is None:
            continue
        parent_delta = _load_json(_story_messages_path(story_id, parent_id), [])
        trimmed_delta = [m for m in parent_delta if m.get("index", 0) <= child_bp]
        if len(trimmed_delta) != len(parent_delta):
            _save_json(_story_messages_path(story_id, parent_id), trimmed_delta)

    # Build a parent -> children map once, then soft-delete everything under
    # promote root except the kept lineage. This also prunes descendants of
    # target branch when target is not a leaf.
    children_map = {}
    for bid, b in branches.items():
        pid = b.get("parent_branch_id")
        if pid is None:
            continue
        children_map.setdefault(pid, []).append(bid)

    branches_to_remove = set()

    def _collect_subtree(root_id: str):
        stack = [root_id]
        seen = set()
        while stack:
            bid = stack.pop()
            if bid in seen or bid in keep_ids:
                continue
            seen.add(bid)
            branches_to_remove.add(bid)
            stack.extend(children_map.get(bid, []))

    # Prune inside promote root only.
    promote_root_id = ancestor_chain[0] if ancestor_chain else branch_id
    stack = [promote_root_id]
    walked = set()
    while stack:
        current = stack.pop()
        if current in walked:
            continue
        walked.add(current)
        for child_id in children_map.get(current, []):
            if child_id in keep_ids:
                stack.append(child_id)
            else:
                _collect_subtree(child_id)

    # Soft-delete discarded sibling subtrees. Kept lineage is never deleted.
    now = datetime.now(timezone.utc).isoformat()
    for bid in sorted(branches_to_remove):
        branches[bid]["deleted"] = True
        branches[bid]["deleted_at"] = now

    tree["active_branch_id"] = branch_id
    tree["promoted_mainline_leaf_id"] = branch_id
    _save_tree(story_id, tree)

    return jsonify({
        "ok": True,
        "active_branch_id": branch_id,
        "deleted_branch_ids": sorted(branches_to_remove),
        "stopped_at_blank_root": stopped_at_blank_root,
    })


@app.route("/api/branches/merge", methods=["POST"])
def api_branches_merge():
    """Merge a child branch into its parent (overwrite parent from branch point)."""
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "").strip()

    if not branch_id or branch_id == "main":
        return jsonify({"ok": False, "error": "invalid branch_id"}), 400

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})

    if branch_id not in branches:
        return jsonify({"ok": False, "error": "branch not found"}), 404

    child = branches[branch_id]

    if child.get("deleted"):
        return jsonify({"ok": False, "error": "cannot merge a deleted branch"}), 400
    if child.get("merged"):
        return jsonify({"ok": False, "error": "branch already merged"}), 400
    if child.get("pruned"):
        return jsonify({"ok": False, "error": "cannot merge a pruned branch"}), 400

    parent_id = child.get("parent_branch_id")
    if parent_id is None:
        return jsonify({"ok": False, "error": "branch has no parent"}), 400
    if parent_id not in branches:
        return jsonify({"ok": False, "error": "parent branch not found"}), 404

    # 1. Merge child's delta messages into parent
    #    Keep parent messages at or before branch point, then append child messages.
    branch_point = child.get("branch_point_index", -1)
    parent_msgs = _load_json(_story_messages_path(story_id, parent_id), [])
    kept = [m for m in parent_msgs if m.get("index", 0) <= branch_point]

    child_msgs = _load_json(_story_messages_path(story_id, branch_id), [])
    for m in child_msgs:
        m.pop("owner_branch_id", None)
        m.pop("inherited", None)
    kept.extend(child_msgs)
    _save_json(_story_messages_path(story_id, parent_id), kept)

    # 2. Copy character state
    src_char = _story_character_state_path(story_id, branch_id)
    dst_char = _story_character_state_path(story_id, parent_id)
    if os.path.exists(src_char):
        shutil.copy2(src_char, dst_char)

    # 3. Copy NPC data
    src_npcs = _story_npcs_path(story_id, branch_id)
    dst_npcs = _story_npcs_path(story_id, parent_id)
    if os.path.exists(src_npcs):
        shutil.copy2(src_npcs, dst_npcs)

    # 4. Copy recap and world_day from child to parent
    copy_recap_to_branch(story_id, branch_id, parent_id, -1)
    copy_world_day(story_id, branch_id, parent_id)
    copy_cheats(_story_dir(story_id), branch_id, parent_id)
    # Merge branch lore from child into parent (upsert, not overwrite)
    _merge_branch_lore_into(story_id, branch_id, parent_id)
    copy_dungeon_progress(story_id, branch_id, parent_id)

    # 5. Reparent child's children to parent
    for bid, b in branches.items():
        if b.get("parent_branch_id") == branch_id:
            b["parent_branch_id"] = parent_id

    # 6. Mark child as merged
    now = datetime.now(timezone.utc).isoformat()
    child["merged"] = True
    child["merged_at"] = now

    # 7. Switch active branch to parent if currently on the merged child
    if tree.get("active_branch_id") == branch_id:
        tree["active_branch_id"] = parent_id

    _save_tree(story_id, tree)

    return jsonify({"ok": True, "parent_branch_id": parent_id})


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
    bdir = _branch_dir(story_id, branch_id)
    if os.path.isdir(bdir):
        shutil.rmtree(bdir)


@app.route("/api/branches/<branch_id>", methods=["DELETE"])
def api_branches_delete(branch_id):
    """Delete a branch (cannot delete main)."""
    if branch_id == "main":
        return jsonify({"ok": False, "error": "cannot delete main branch"}), 400

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})

    if branch_id not in branches:
        return jsonify({"ok": False, "error": "branch not found"}), 404

    branch = branches[branch_id]
    deleted_parent = branch.get("parent_branch_id", "main") or "main"
    deleted_bp = branch.get("branch_point_index")

    # Reparent ALL children (including deleted/merged) to prevent dangling refs
    all_children = [
        b for b in branches.values()
        if b.get("parent_branch_id") == branch_id
    ]

    # Only materialize messages for active (non-deleted, non-merged, non-pruned) children
    active_children = [
        b for b in all_children
        if not b.get("deleted") and not b.get("merged") and not b.get("pruned")
    ]

    if active_children:
        # Load deleted branch's delta messages before we remove the branch dir
        deleted_delta = _load_json(_story_messages_path(story_id, branch_id), [])

        for child in active_children:
            child_id = child["id"]
            child_bp = child.get("branch_point_index")

            if child_bp is not None and child_bp >= 0 and deleted_bp is not None:
                if child_bp >= deleted_bp:
                    # Case A: child forked within deleted's delta range
                    # Inherit messages from deleted's delta up to child's bp
                    inherited = [m for m in deleted_delta if m.get("index", 0) <= child_bp]
                    if inherited:
                        child_delta = _load_json(_story_messages_path(story_id, child_id), [])
                        _save_json(_story_messages_path(story_id, child_id), inherited + child_delta)
                        child["branch_point_index"] = deleted_bp
                    # else: deleted_delta empty → treat like Case B (keep bp, just reparent)
                else:
                    # Case B: child_bp < deleted_bp — child doesn't inherit anything
                    # bp stays the same (still valid in grandparent's timeline)
                    pass
            # else: blank child (bp=-1) — no messages inherited, bp stays -1

    # Reparent all children (including deleted/merged) to grandparent
    for child in all_children:
        child["parent_branch_id"] = deleted_parent

    # Delete the branch itself (preserve existing was_main soft-delete logic)
    if branch.get("was_main"):
        now = datetime.now(timezone.utc).isoformat()
        branch["deleted"] = True
        branch["deleted_at"] = now
    else:
        bdir = _branch_dir(story_id, branch_id)
        if os.path.isdir(bdir):
            shutil.rmtree(bdir)
        del branches[branch_id]

    if tree.get("active_branch_id") == branch_id:
        tree["active_branch_id"] = deleted_parent

    _save_tree(story_id, tree)

    return jsonify({"ok": True, "switch_to": deleted_parent})


@app.route("/api/branches/<branch_id>/protect", methods=["POST"])
def api_branches_protect(branch_id):
    """Toggle protected flag on a branch (prevents auto-prune)."""
    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})

    if branch_id not in branches:
        return jsonify({"ok": False, "error": "branch not found"}), 404

    branch = branches[branch_id]
    if branch.get("deleted") or branch.get("pruned") or branch.get("merged"):
        return jsonify({"ok": False, "error": "cannot protect inactive branch"}), 400

    if branch.get("protected"):
        branch.pop("protected", None)
        protected = False
    else:
        branch["protected"] = True
        protected = True

    _save_tree(story_id, tree)
    return jsonify({"ok": True, "protected": protected})


# ---------------------------------------------------------------------------
# Story CRUD API
# ---------------------------------------------------------------------------

@app.route("/api/stories")
def api_stories():
    """Return all stories + active_story_id."""
    registry = _load_stories_registry()
    return jsonify({
        "active_story_id": registry.get("active_story_id", "story_original"),
        "stories": registry.get("stories", {}),
    })


@app.route("/api/stories", methods=["POST"])
def api_stories_create():
    """Create a new story."""
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "story name required"}), 400

    description = body.get("description", "").strip()
    system_prompt_text = body.get("system_prompt", "").strip()
    character_schema = body.get("character_schema")
    default_state = body.get("default_character_state")

    story_id = f"story_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    story_dir = _story_dir(story_id)
    os.makedirs(story_dir, exist_ok=True)
    os.makedirs(_story_design_dir(story_id), exist_ok=True)

    # System prompt
    if system_prompt_text:
        with open(_story_system_prompt_path(story_id), "w", encoding="utf-8") as f:
            f.write(system_prompt_text)

    # Character schema
    if character_schema:
        _save_json(_story_character_schema_path(story_id), character_schema)
    else:
        # Minimal default schema
        _save_json(_story_character_schema_path(story_id), {
            "fields": [{"key": "name", "label": "姓名", "type": "text"}],
            "lists": [],
            "direct_overwrite_keys": [],
        })

    # Default character state
    if default_state:
        _save_json(_story_default_character_state_path(story_id), default_state)
    else:
        _save_json(_story_default_character_state_path(story_id), {"name": "—"})

    # Empty parsed conversation
    _save_json(_story_parsed_path(story_id), [])

    # Initialize timeline tree with main branch
    tree = {
        "active_branch_id": "main",
        "branches": {
            "main": {
                "id": "main",
                "name": "主時間線",
                "parent_branch_id": None,
                "branch_point_index": None,
                "created_at": now,
                "session_id": None,
                "character_state_file": "character_state_main.json",
            }
        },
    }
    _save_tree(story_id, tree)

    # Empty main messages
    _save_json(_story_messages_path(story_id, "main"), [])

    # Initial character state for main branch
    ds = default_state if default_state else {"name": "—"}
    _save_json(_story_character_state_path(story_id, "main"), ds)

    # Register in stories.json
    registry = _load_stories_registry()
    registry["stories"][story_id] = {
        "id": story_id,
        "name": name,
        "description": description,
        "created_at": now,
    }
    _save_stories_registry(registry)

    return jsonify({"ok": True, "story": registry["stories"][story_id]})


@app.route("/api/stories/switch", methods=["POST"])
def api_stories_switch():
    """Switch active story — returns init-like data."""
    body = request.get_json(force=True)
    story_id = body.get("story_id", "").strip()
    if not story_id:
        return jsonify({"ok": False, "error": "story_id required"}), 400

    registry = _load_stories_registry()
    if story_id not in registry.get("stories", {}):
        return jsonify({"ok": False, "error": "story not found"}), 404

    registry["active_story_id"] = story_id
    _save_stories_registry(registry)

    # Return init-like data for the switched story
    tree = _load_tree(story_id)
    active_branch = tree.get("active_branch_id", "main")
    original = _load_json(_story_parsed_path(story_id), [])
    story_meta = registry["stories"][story_id]
    character_schema = _load_character_schema(story_id)

    return jsonify({
        "ok": True,
        "active_story_id": story_id,
        "story_name": story_meta.get("name", story_id),
        "active_branch_id": active_branch,
        "original_count": len(original),
        "character_schema": character_schema,
    })


@app.route("/api/stories/<story_id>", methods=["PATCH"])
def api_stories_update(story_id):
    """Update story name/description."""
    body = request.get_json(force=True)
    registry = _load_stories_registry()
    stories = registry.get("stories", {})

    if story_id not in stories:
        return jsonify({"ok": False, "error": "story not found"}), 404

    if "name" in body and body["name"].strip():
        stories[story_id]["name"] = body["name"].strip()
    if "description" in body:
        stories[story_id]["description"] = body["description"].strip()

    _save_stories_registry(registry)
    return jsonify({"ok": True, "story": stories[story_id]})


@app.route("/api/stories/<story_id>", methods=["DELETE"])
def api_stories_delete(story_id):
    """Delete a story (cannot delete the last one)."""
    registry = _load_stories_registry()
    stories = registry.get("stories", {})

    if story_id not in stories:
        return jsonify({"ok": False, "error": "story not found"}), 404
    if len(stories) <= 1:
        return jsonify({"ok": False, "error": "cannot delete the last story"}), 400

    # Remove story directories (runtime data + design files)
    story_dir = _story_dir(story_id)
    if os.path.exists(story_dir):
        shutil.rmtree(story_dir)
    design_dir = _story_design_dir(story_id)
    if os.path.exists(design_dir):
        shutil.rmtree(design_dir)

    del stories[story_id]

    # If active story was deleted, switch to first remaining
    if registry.get("active_story_id") == story_id:
        registry["active_story_id"] = next(iter(stories))

    _save_stories_registry(registry)
    return jsonify({"ok": True, "active_story_id": registry["active_story_id"]})


@app.route("/api/stories/<story_id>/schema")
def api_stories_schema(story_id):
    """Return character schema for a story."""
    registry = _load_stories_registry()
    if story_id not in registry.get("stories", {}):
        return jsonify({"ok": False, "error": "story not found"}), 404
    return jsonify(_load_character_schema(story_id))


# ---------------------------------------------------------------------------
# Lore search API
# ---------------------------------------------------------------------------

@app.route("/api/lore/search")
def api_lore_search():
    """Search world lore. Query params: q (search text), tags (comma-separated), limit."""
    from lore_db import search_lore, search_by_tags, get_all_entries
    story_id = _active_story_id()
    q = request.args.get("q", "").strip()
    tags = request.args.get("tags", "").strip()
    limit = int(request.args.get("limit", "10"))

    if q:
        results = search_lore(story_id, q, limit=limit)
    elif tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        results = search_by_tags(story_id, tag_list, limit=limit)
    else:
        results = get_all_entries(story_id)

    return jsonify({"ok": True, "results": results, "count": len(results)})


@app.route("/api/lore/rebuild", methods=["POST"])
def api_lore_rebuild():
    """Force rebuild the lore search index from world_lore.json."""
    story_id = _active_story_id()
    rebuild_lore_index(story_id)
    return jsonify({"ok": True, "message": "lore index rebuilt"})


@app.route("/api/lore/duplicates")
def api_lore_duplicates():
    """Find near-duplicate lore entries via embedding similarity."""
    story_id = request.args.get("story_id") or _active_story_id()
    try:
        threshold = float(request.args.get("threshold", "0.90"))
    except (ValueError, TypeError):
        threshold = 0.90
    threshold = max(0.5, min(1.0, threshold))  # clamp to [0.5, 1.0]
    pairs = find_duplicates(story_id, threshold=threshold)
    return jsonify({"ok": True, "pairs": pairs, "count": len(pairs), "threshold": threshold})


@app.route("/api/lore/embedding-stats")
def api_lore_embedding_stats():
    """Return embedding coverage stats for the active story."""
    story_id = request.args.get("story_id") or _active_story_id()
    stats = get_embedding_stats(story_id)
    return jsonify({"ok": True, **stats})


# ---------------------------------------------------------------------------
# Lore Console page + CRUD + LLM chat
# ---------------------------------------------------------------------------

@app.route("/lore")
def lore_page():
    """Render the Lore Console page."""
    return render_template("lore.html")


@app.route("/api/lore/all")
def api_lore_all():
    """Return all lore entries grouped by category, with layer info."""
    story_id = _active_story_id()
    branch_id = request.args.get("branch_id")
    if not branch_id:
        tree = _load_tree(story_id)
        branch_id = tree.get("active_branch_id", "main")
    # Base lore (per-story)
    base = _load_lore(story_id)
    for e in base:
        e["layer"] = "base"
    # Branch lore (per-branch)
    branch = _load_branch_lore(story_id, branch_id)
    for e in branch:
        e["layer"] = "branch"
    all_entries = base + branch
    # Collect categories in order of first appearance
    categories = list(dict.fromkeys(e.get("category", "其他") for e in all_entries))
    return jsonify({"ok": True, "entries": all_entries, "categories": categories,
                     "branch_id": branch_id})


@app.route("/api/lore/entry", methods=["POST"])
def api_lore_entry_create():
    """Create a new lore entry."""
    story_id = _active_story_id()
    body = request.get_json(force=True)
    topic = body.get("topic", "").strip()
    category = body.get("category", "其他").strip()
    content = body.get("content", "").strip()
    if not topic:
        return jsonify({"ok": False, "error": "topic required"}), 400
    subcategory = body.get("subcategory", "").strip()
    # Check for duplicate within same (subcategory, topic) scope
    lore = _load_lore(story_id)
    for e in lore:
        if e.get("topic") == topic and e.get("subcategory", "") == subcategory:
            return jsonify({"ok": False, "error": f"topic '{topic}' already exists in this subcategory"}), 409
    entry = {"category": category, "topic": topic, "content": content, "edited_by": "user"}
    if subcategory:
        entry["subcategory"] = subcategory
    _save_lore_entry(story_id, entry)
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/lore/entry", methods=["PUT"])
def api_lore_entry_update():
    """Update an existing lore entry. Supports rename via new_topic."""
    story_id = _active_story_id()
    body = request.get_json(force=True)
    topic = body.get("topic", "").strip()
    if not topic:
        return jsonify({"ok": False, "error": "topic required"}), 400

    req_sub = body.get("subcategory", "").strip()
    lock = get_lore_lock(story_id)
    with lock:
        lore = _load_lore(story_id)
        found = False
        for i, e in enumerate(lore):
            if e.get("topic") == topic and e.get("subcategory", "") == req_sub:
                found = True
                new_topic = body.get("new_topic", topic).strip()
                new_category = body.get("category", e.get("category", "其他")).strip()
                new_content = body.get("content", e.get("content", "")).strip()
                new_sub = body["subcategory"].strip() if "subcategory" in body else e.get("subcategory", "")
                # Check collision when topic or subcategory changes
                if new_topic != topic or new_sub != e.get("subcategory", ""):
                    if any(x is not e and x.get("topic") == new_topic and x.get("subcategory", "") == new_sub for x in lore):
                        return jsonify({"ok": False, "error": f"topic '{new_topic}' already exists in this subcategory"}), 409
                if new_topic != topic or new_sub != req_sub:
                    delete_lore_entry(story_id, topic, req_sub)
                updated = {"category": new_category, "topic": new_topic, "content": new_content, "edited_by": "user"}
                if "subcategory" in body:
                    new_subcategory = body["subcategory"].strip()
                    if new_subcategory:
                        updated["subcategory"] = new_subcategory
                elif e.get("subcategory"):
                    updated["subcategory"] = e["subcategory"]
                if "source" in e:
                    updated["source"] = e["source"]
                lore[i] = updated
                _save_json(_story_lore_path(story_id), lore)
                upsert_lore_entry(story_id, lore[i])
                return jsonify({"ok": True, "entry": lore[i]})
        if not found:
            return jsonify({"ok": False, "error": "entry not found"}), 404


@app.route("/api/lore/entry", methods=["DELETE"])
def api_lore_entry_delete():
    """Delete a lore entry by (subcategory, topic)."""
    story_id = _active_story_id()
    body = request.get_json(force=True)
    topic = body.get("topic", "").strip()
    subcategory = body.get("subcategory", "").strip()
    if not topic:
        return jsonify({"ok": False, "error": "topic required"}), 400

    lock = get_lore_lock(story_id)
    with lock:
        lore = _load_lore(story_id)
        new_lore = [e for e in lore if not (e.get("topic") == topic and e.get("subcategory", "") == subcategory)]
        if len(new_lore) == len(lore):
            return jsonify({"ok": False, "error": "entry not found"}), 404
        _save_json(_story_lore_path(story_id), new_lore)
        delete_lore_entry(story_id, topic, subcategory)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Branch lore CRUD + Promotion
# ---------------------------------------------------------------------------

@app.route("/api/lore/branch/entry", methods=["DELETE"])
def api_branch_lore_entry_delete():
    """Delete a branch lore entry by (subcategory, topic)."""
    story_id = _active_story_id()
    body = request.get_json(force=True)
    topic = body.get("topic", "").strip()
    subcategory = body.get("subcategory", "").strip()
    branch_id = body.get("branch_id", "")
    if not topic or not branch_id:
        return jsonify({"ok": False, "error": "topic and branch_id required"}), 400

    lore = _load_branch_lore(story_id, branch_id)
    new_lore = [e for e in lore if not (e.get("topic") == topic and e.get("subcategory", "") == subcategory)]
    if len(new_lore) == len(lore):
        return jsonify({"ok": False, "error": "entry not found"}), 404
    _save_branch_lore(story_id, branch_id, new_lore)
    return jsonify({"ok": True})


@app.route("/api/lore/promote/review", methods=["POST"])
def api_lore_promote_review():
    """Use LLM to review branch lore entries and propose promotion actions."""
    from llm_bridge import call_oneshot

    story_id = _active_story_id()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "")
    if not branch_id:
        return jsonify({"ok": False, "error": "branch_id required"}), 400

    branch_lore = _load_branch_lore(story_id, branch_id)
    if not branch_lore:
        return jsonify({"ok": True, "proposals": []})

    # Build base lore TOC for context
    base_toc = get_lore_toc(story_id)

    # Format branch entries for review
    entries_text = ""
    for i, e in enumerate(branch_lore):
        entries_text += f"\n### 條目 {i+1}\n"
        entries_text += f"分類: {e.get('category', '')}\n"
        entries_text += f"主題: {e.get('topic', '')}\n"
        entries_text += f"內容: {e.get('content', '')}\n"

    prompt = (
        "你是一個 RPG 世界設定審核員。以下是從某個分支冒險中自動提取的設定條目。\n"
        "請判斷每個條目是否適合提升為永久世界設定（base lore），還是只是特定角色的經驗。\n\n"
        f"## 已有的永久世界設定\n{base_toc}\n\n"
        f"## 待審核的分支設定\n{entries_text}\n\n"
        "## 審核規則\n"
        "對每個條目選擇一個動作：\n"
        "- **promote**: 純粹的世界觀設定（體系規則、副本背景、場景描述、NPC通用資料等），可以直接提升\n"
        "- **rewrite**: 混合內容（包含世界設定但也含有特定角色名稱/經歷），需要改寫後提升。"
        "提供 rewritten_content，移除角色特定內容，只保留通用世界設定\n"
        "- **reject**: 純粹的角色經驗（角色完成了X、角色獲得了Y、角色的狀態等），不適合提升\n\n"
        "## 輸出格式\n"
        "JSON 陣列，每個元素：\n"
        '[{"index": 0, "action": "promote|rewrite|reject", "reason": "簡短理由", '
        '"rewritten_content": "改寫後內容（僅 rewrite 時提供）"}]\n'
        "只輸出 JSON。"
    )

    t0 = time.time()
    result = call_oneshot(prompt)
    _log_llm_usage(story_id, "oneshot", time.time() - t0)

    if not result:
        return jsonify({"ok": False, "error": "LLM call failed"}), 500

    result = result.strip()
    if result.startswith("```"):
        lines = result.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        result = "\n".join(lines)

    try:
        proposals = json.loads(result)
    except json.JSONDecodeError:
        m = re.search(r'\[.*\]', result, re.DOTALL)
        if not m:
            return jsonify({"ok": False, "error": "failed to parse LLM response"}), 500
        try:
            proposals = json.loads(m.group())
        except json.JSONDecodeError:
            return jsonify({"ok": False, "error": "failed to parse LLM response"}), 500

    # Enrich proposals with entry data
    enriched = []
    for p in proposals:
        idx = p.get("index", -1)
        if 0 <= idx < len(branch_lore):
            entry = branch_lore[idx]
            enriched.append({
                "index": idx,
                "action": p.get("action", "reject"),
                "reason": p.get("reason", ""),
                "topic": entry.get("topic", ""),
                "category": entry.get("category", ""),
                "content": entry.get("content", ""),
                "rewritten_content": p.get("rewritten_content", ""),
            })

    return jsonify({"ok": True, "proposals": enriched})


@app.route("/api/lore/promote", methods=["POST"])
def api_lore_promote():
    """Promote a branch lore entry to base lore (world_lore.json)."""
    story_id = _active_story_id()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "")
    topic = body.get("topic", "").strip()
    subcategory = body.get("subcategory", "").strip()
    content_override = body.get("content", "").strip()  # for rewrite action
    if not branch_id or not topic:
        return jsonify({"ok": False, "error": "branch_id and topic required"}), 400

    # Find in branch lore by (subcategory, topic)
    branch_lore = _load_branch_lore(story_id, branch_id)
    entry = None
    for e in branch_lore:
        if e.get("topic") == topic and e.get("subcategory", "") == subcategory:
            entry = e
            break
    if not entry:
        return jsonify({"ok": False, "error": "entry not found in branch lore"}), 404

    # Prepare base entry
    base_entry = {
        "category": entry.get("category", "其他"),
        "topic": topic,
        "content": content_override or entry.get("content", ""),
        "edited_by": "user",  # promoted = user-curated
    }
    if entry.get("subcategory"):
        base_entry["subcategory"] = entry["subcategory"]
    if "source" in entry:
        base_entry["source"] = entry["source"]

    # Save to base lore
    _save_lore_entry(story_id, base_entry)

    # Remove from branch lore by (subcategory, topic)
    new_branch_lore = [e for e in branch_lore if not (e.get("topic") == topic and e.get("subcategory", "") == subcategory)]
    _save_branch_lore(story_id, branch_id, new_branch_lore)

    return jsonify({"ok": True, "entry": base_entry})


_LORE_PROPOSE_RE = re.compile(
    r"<!--LORE_PROPOSE\s*(.*?)\s*LORE_PROPOSE-->", re.DOTALL
)


@app.route("/api/lore/chat/stream", methods=["POST"])
def api_lore_chat_stream():
    """SSE streaming chat for lore discussion. Parses LORE_PROPOSE tags."""
    story_id = _active_story_id()
    body = request.get_json(force=True)
    messages = body.get("messages", [])
    if not messages:
        return Response(_sse_event({"type": "error", "message": "no messages"}),
                        mimetype="text/event-stream")

    # Build system prompt with all lore
    lore = _load_lore(story_id)
    lore_text_parts = []
    from collections import OrderedDict
    groups = OrderedDict()
    for e in lore:
        cat = e.get("category", "其他")
        sub = e.get("subcategory", "")
        key = f"{cat}/{sub}" if sub else cat
        if key not in groups:
            groups[key] = []
        groups[key].append(e)
    for key, entries in groups.items():
        lore_text_parts.append(f"### 【{key}】")
        for e in entries:
            lore_text_parts.append(f"#### {e['topic']}")
            lore_text_parts.append(e.get("content", ""))
            lore_text_parts.append("")

    cat_list = ", ".join(dict.fromkeys(e.get("category", "其他") for e in lore)) if lore else "其他"
    lore_system = f"""你是世界設定管理助手，協助維護 RPG 世界的設定知識庫。

角色：討論/新增/修改/刪除設定，確保一致性，用繁體中文回覆。
重要：變更會即時同步到遊戲中，影響 GM 的下一次回覆。

現有分類：{cat_list}
現有設定（{len(lore)} 條）：
{chr(10).join(lore_text_parts)}

設定格式規範：
- 設定內容可包含 [tag: 標籤1/標籤2] 用於搜尋分類（例：[tag: 體系/戰鬥]）
- 設定內容可包含 [source: 來源] 標記參考資料
- 設定會透過關鍵字搜尋注入 GM 上下文，請使用明確的術語和關鍵字以提升檢索效果
- 新增設定時請使用上方現有分類，避免建立新分類

子分類（subcategory）規範：
- 副本世界觀 的條目：subcategory = 副本名稱（如「海賊王」「生化危機」）。首條總覽條目 topic = 「介紹」；後續條目用具體名稱（如「T病毒」「追蹤者」）
- 體系 的條目：subcategory = 體系名稱（如「霸氣」「基因鎖」）。首條總覽條目 topic = 「介紹」；後續條目用具體名稱
- 場景 的條目：subcategory 建議填對應副本名稱（與副本世界觀對應），topic 為場景名稱
- 其他分類：subcategory 可選，不強制
- 同一 subcategory 下的 topic 必須唯一，但不同 subcategory 間 topic 可重複（例如每個副本都可有「介紹」）
- delete 操作以 (subcategory + topic) 聯合識別，請同時提供 subcategory 以精確刪除

提案格式（當建議變更時使用）：
<!--LORE_PROPOSE {{"action":"add|edit|delete", "category":"...", "subcategory":"...", "topic":"...", "content":"..."}} LORE_PROPOSE-->

規則：
- 先討論再提案，確認用戶意圖後再輸出提案標籤
- content 欄位是完整的設定文字（不是差異）
- delete 操作不需要 content 欄位
- 可在一次回覆中輸出多個提案標籤
- 提案標籤必須放在回覆最末尾"""

    # Conditional Google Search grounding (Gemini only, zero cost when unused)
    from llm_bridge import get_provider
    provider = get_provider()
    tools = None
    if provider == "gemini":
        tools = [{"googleSearch": {}}]
        lore_system += """

網路搜尋：
- 你可以使用 Google Search 搜尋外部資料（動漫/小說/遊戲設定等）
- 當用戶提到外部作品或需要查證資料時，主動搜尋以獲得準確資訊
- 搜尋到的資料可以作為建議設定的依據"""

    # Extract prior messages and last user message (safe access)
    prior = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages[:-1]]
    last_user_msg = messages[-1].get("content", "")

    def generate():
        t_start = time.time()
        try:
            for event_type, payload in call_claude_gm_stream(
                last_user_msg, lore_system, prior, session_id=None,
                tools=tools,
            ):
                if event_type == "text":
                    yield _sse_event({"type": "text", "chunk": payload})
                elif event_type == "error":
                    yield _sse_event({"type": "error", "message": payload})
                    return
                elif event_type == "done":
                    _log_llm_usage(story_id, "lore_chat", time.time() - t_start,
                                   usage=payload.get("usage"))
                    full_response = payload.get("response", "")
                    # Parse LORE_PROPOSE tags
                    proposals = []
                    for m in _LORE_PROPOSE_RE.finditer(full_response):
                        try:
                            p = json.loads(m.group(1))
                            proposals.append(p)
                        except json.JSONDecodeError:
                            pass
                    # Strip tags from display text
                    display_text = _LORE_PROPOSE_RE.sub("", full_response).strip()
                    done_event = {
                        "type": "done",
                        "response": display_text,
                        "proposals": proposals,
                    }
                    if payload.get("grounding"):
                        done_event["grounding"] = payload["grounding"]
                    yield _sse_event(done_event)
        except Exception as e:
            log.info("/api/lore/chat/stream EXCEPTION %s", e)
            yield _sse_event({"type": "error", "message": str(e)})

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/lore/apply", methods=["POST"])
def api_lore_apply():
    """Batch-apply accepted lore proposals."""
    story_id = _active_story_id()
    body = request.get_json(force=True)
    proposals = body.get("proposals", [])
    applied = []
    for p in proposals:
        action = p.get("action", "").lower()
        topic = p.get("topic", "").strip()
        if not topic:
            continue
        if action == "add":
            entry = {
                "category": p.get("category", "其他"),
                "topic": topic,
                "content": p.get("content", ""),
                "edited_by": "user",
            }
            if p.get("subcategory"):
                entry["subcategory"] = p["subcategory"]
            _save_lore_entry(story_id, entry)
            applied.append({"action": "add", "topic": topic})
        elif action == "edit":
            entry = {
                "category": p.get("category", "其他"),
                "topic": topic,
                "content": p.get("content", ""),
                "edited_by": "user",
            }
            if p.get("subcategory"):
                entry["subcategory"] = p["subcategory"]
            _save_lore_entry(story_id, entry)
            applied.append({"action": "edit", "topic": topic})
        elif action == "delete":
            sub = p.get("subcategory", "").strip()
            lore = _load_lore(story_id)
            new_lore = [e for e in lore if not (e.get("topic") == topic and e.get("subcategory", "") == sub)]
            if len(new_lore) < len(lore):
                _save_json(_story_lore_path(story_id), new_lore)
                delete_lore_entry(story_id, topic, sub)
                applied.append({"action": "delete", "topic": topic})
    return jsonify({"ok": True, "applied": applied})


# ---------------------------------------------------------------------------
# NPC API
# ---------------------------------------------------------------------------

@app.route("/api/npcs")
def api_npcs():
    """Return all NPCs for the active story + branch."""
    story_id = _active_story_id()
    branch_id = request.args.get("branch_id", "main")
    npcs = _load_npcs(story_id, branch_id)
    return jsonify({"ok": True, "npcs": npcs})


@app.route("/api/npcs", methods=["POST"])
def api_npcs_create():
    """Create or update an NPC."""
    story_id = _active_story_id()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "main")
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    _save_npc(story_id, body, branch_id)
    return jsonify({"ok": True, "npcs": _load_npcs(story_id, branch_id)})


@app.route("/api/npcs/<npc_id>", methods=["DELETE"])
def api_npcs_delete(npc_id):
    """Delete an NPC by id."""
    story_id = _active_story_id()
    branch_id = request.args.get("branch_id", "main")
    npcs = _load_npcs(story_id, branch_id)
    npcs = [n for n in npcs if n.get("id") != npc_id]
    _save_json(_story_npcs_path(story_id, branch_id), npcs)
    return jsonify({"ok": True, "npcs": npcs})


# ---------------------------------------------------------------------------
# Event API
# ---------------------------------------------------------------------------

@app.route("/api/events")
def api_events():
    """Return events for the active story. Optional query param: branch_id."""
    story_id = _active_story_id()
    branch_id = request.args.get("branch_id")
    limit = int(request.args.get("limit", "50"))
    events = get_events(story_id, branch_id=branch_id, limit=limit)
    return jsonify({"ok": True, "events": events})


@app.route("/api/events/search")
def api_events_search():
    """Search events. Query params: q, branch_id, limit."""
    story_id = _active_story_id()
    q = request.args.get("q", "").strip()
    branch_id = request.args.get("branch_id")
    limit = int(request.args.get("limit", "10"))
    if not q:
        return jsonify({"ok": True, "events": [], "count": 0})
    results = search_events_db(story_id, q, branch_id=branch_id, limit=limit)
    return jsonify({"ok": True, "events": results, "count": len(results)})


@app.route("/api/events/<int:event_id>", methods=["PATCH"])
def api_events_update(event_id):
    """Update event status."""
    story_id = _active_story_id()
    body = request.get_json(force=True)
    new_status = body.get("status", "").strip()
    if new_status not in ("planted", "triggered", "resolved", "abandoned"):
        return jsonify({"ok": False, "error": "invalid status"}), 400
    update_event_status(story_id, event_id, new_status)
    event = get_event_by_id(story_id, event_id)
    return jsonify({"ok": True, "event": event})


# ---------------------------------------------------------------------------
# Image API
# ---------------------------------------------------------------------------

@app.route("/api/images/status")
def api_images_status():
    """Check image generation status."""
    story_id = _active_story_id()
    filename = request.args.get("filename", "")
    if not filename:
        return jsonify({"ok": False, "error": "filename required"}), 400
    status = get_image_status(story_id, filename)
    return jsonify({"ok": True, **status})


@app.route("/api/stories/<story_id>/images/<filename>")
def api_images_serve(story_id, filename):
    """Serve a generated image file."""
    # Sanitize filename to prevent directory traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"ok": False, "error": "invalid filename"}), 400
    path = get_image_path(story_id, filename)
    if not path:
        return jsonify({"ok": False, "error": "image not found"}), 404
    return send_file(path, mimetype="image/png")


# ---------------------------------------------------------------------------
# NPC Activities API
# ---------------------------------------------------------------------------

@app.route("/api/npc-activities")
def api_npc_activities():
    """Return NPC activities for a branch."""
    story_id = _active_story_id()
    branch_id = request.args.get("branch_id", "main")
    activities = get_all_activities(story_id, branch_id)
    return jsonify({"ok": True, "activities": activities})


# ---------------------------------------------------------------------------
# Game Save API (遊戲存檔)
# ---------------------------------------------------------------------------

@app.route("/api/saves")
def api_saves_list():
    """Return all saves for the active story (without bulky snapshots)."""
    story_id = _active_story_id()
    saves = _load_json(_story_saves_path(story_id), [])
    # Strip snapshot data from list response to keep payload small
    slim = []
    for s in saves:
        entry = {k: v for k, v in s.items()
                 if k not in ("character_snapshot", "npc_snapshot", "recap_snapshot")}
        slim.append(entry)
    return jsonify({"ok": True, "saves": slim})


@app.route("/api/saves", methods=["POST"])
def api_saves_create():
    """Create a save (snapshot current state)."""
    body = request.get_json(force=True)
    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branch_id = tree.get("active_branch_id", "main")

    # Get current message count for message_index
    timeline = get_full_timeline(story_id, branch_id)
    last_index = timeline[-1].get("index", len(timeline) - 1) if timeline else 0

    # Snapshot current state
    character_state = _load_character_state(story_id, branch_id)
    npcs = _load_npcs(story_id, branch_id)
    world_day = get_world_day(story_id, branch_id)
    recap = load_recap(story_id, branch_id)

    # Build preview from last GM message
    last_gm = ""
    for m in reversed(timeline):
        if m.get("role") == "gm":
            last_gm = m.get("content", "")[:100]
            break

    save_id = f"save_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    # Branch title/name for display
    branch_meta = tree.get("branches", {}).get(branch_id, {})
    branch_name = branch_meta.get("title") or branch_meta.get("name") or branch_id

    save_entry = {
        "id": save_id,
        "name": body.get("name", "").strip() or f"{branch_name} — 第{int(world_day) + 1}天",
        "branch_id": branch_id,
        "message_index": last_index,
        "created_at": now,
        "world_day": world_day,
        "character_snapshot": character_state,
        "npc_snapshot": npcs,
        "recap_snapshot": recap,
        "preview": last_gm,
    }

    saves = _load_json(_story_saves_path(story_id), [])
    saves.insert(0, save_entry)  # newest first
    _save_json(_story_saves_path(story_id), saves)

    log.info("save created: %s on branch %s at index %d", save_id, branch_id, last_index)
    return jsonify({"ok": True, "save": save_entry})


@app.route("/api/saves/<save_id>/load", methods=["POST"])
def api_saves_load(save_id):
    """Load a save: switch back to the original branch where the save was created."""
    story_id = _active_story_id()
    saves = _load_json(_story_saves_path(story_id), [])
    save = next((s for s in saves if s["id"] == save_id), None)
    if not save:
        return jsonify({"ok": False, "error": "save not found"}), 404

    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    branch_id = save["branch_id"]

    if branch_id != "main" and branch_id not in branches:
        return jsonify({"ok": False, "error": "original branch no longer exists"}), 404

    branch_meta = branches.get(branch_id)
    if not branch_meta:
        return jsonify({"ok": False, "error": "branch metadata missing"}), 500

    tree["active_branch_id"] = branch_id
    tree["loaded_save_id"] = save_id
    tree["loaded_save_branch_id"] = branch_id
    _save_tree(story_id, tree)

    log.info("save loaded: %s → switched to branch %s (status preview on)", save_id, branch_id)
    return jsonify({"ok": True, "branch_id": branch_id, "branch": branch_meta})


@app.route("/api/saves/<save_id>", methods=["DELETE"])
def api_saves_delete(save_id):
    """Delete a save."""
    story_id = _active_story_id()
    saves = _load_json(_story_saves_path(story_id), [])
    new_saves = [s for s in saves if s["id"] != save_id]
    if len(new_saves) == len(saves):
        return jsonify({"ok": False, "error": "save not found"}), 404
    _save_json(_story_saves_path(story_id), new_saves)
    tree = _load_tree(story_id)
    if tree.get("loaded_save_id") == save_id and _clear_loaded_save_preview(tree):
        _save_tree(story_id, tree)
    log.info("save deleted: %s", save_id)
    return jsonify({"ok": True})


@app.route("/api/saves/<save_id>", methods=["PUT"])
def api_saves_rename(save_id):
    """Rename a save."""
    body = request.get_json(force=True)
    new_name = body.get("name", "").strip()
    if not new_name:
        return jsonify({"ok": False, "error": "name required"}), 400

    story_id = _active_story_id()
    saves = _load_json(_story_saves_path(story_id), [])
    for s in saves:
        if s["id"] == save_id:
            s["name"] = new_name
            _save_json(_story_saves_path(story_id), saves)
            return jsonify({"ok": True, "save": s})
    return jsonify({"ok": False, "error": "save not found"}), 404


# ---------------------------------------------------------------------------
# Auto-Play Summaries API
# ---------------------------------------------------------------------------

@app.route("/api/auto-play/summaries")
def api_auto_play_summaries():
    """Return auto-play summaries for a branch."""
    story_id = _active_story_id()
    branch_id = request.args.get("branch_id", "main")
    return jsonify({"ok": True, "summaries": get_summaries(story_id, branch_id)})


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Bug Report API (#6)
# ---------------------------------------------------------------------------

@app.route("/api/bug-report", methods=["POST"])
def api_bug_report():
    """Save a bug report for a specific message."""
    data = request.get_json(force=True)
    story_id = _active_story_id()
    report = {
        "story_id": story_id,
        "branch_id": data.get("branch_id", ""),
        "message_index": data.get("message_index"),
        "role": data.get("role", ""),
        "content_preview": data.get("content_preview", "")[:500],
        "description": data.get("description", "")[:2000],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    reports_path = os.path.join(_story_dir(story_id), "bug_reports.json")
    reports = _load_json(reports_path, [])
    if len(reports) >= 500:
        reports = reports[-400:]  # keep latest 400 when cap hit
    reports.append(report)
    _save_json(reports_path, reports)
    log.info("Bug report saved: branch=%s msg=%s", report["branch_id"], report["message_index"])
    return jsonify({"ok": True})


# LLM Config API (provider / model switcher)
# ---------------------------------------------------------------------------

_LLM_CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")


@app.route("/api/config")
def api_config_get():
    """Return sanitized LLM config (no API keys exposed)."""
    try:
        with open(_LLM_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {"provider": "claude_cli"}

    g = cfg.get("gemini", {})
    # Count keys without exposing them
    from gemini_key_manager import load_keys
    key_count = len(load_keys(g))

    return jsonify({
        "ok": True,
        "version": __version__,
        "provider": cfg.get("provider", "claude_cli"),
        "gemini": {
            "model": g.get("model", "gemini-2.0-flash"),
            "key_count": key_count,
        },
        "claude_cli": {
            "model": cfg.get("claude_cli", {}).get("model", "claude-sonnet-4-5-20250929"),
        },
    })


@app.route("/api/config", methods=["POST"])
def api_config_set():
    """Update provider and/or model. Writes to llm_config.json."""
    data = request.get_json(force=True)

    try:
        with open(_LLM_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {"provider": "claude_cli"}

    if "provider" in data:
        cfg["provider"] = data["provider"]

    if "gemini" in data and isinstance(data["gemini"], dict):
        if "gemini" not in cfg:
            cfg["gemini"] = {}
        if "model" in data["gemini"]:
            cfg["gemini"]["model"] = data["gemini"]["model"]

    if "claude_cli" in data and isinstance(data["claude_cli"], dict):
        if "claude_cli" not in cfg:
            cfg["claude_cli"] = {}
        if "model" in data["claude_cli"]:
            cfg["claude_cli"]["model"] = data["claude_cli"]["model"]

    with open(_LLM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    log.info("api_config_set: updated — provider=%s", cfg.get("provider"))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Cheats API (金手指)
# ---------------------------------------------------------------------------

@app.route("/api/cheats/dice", methods=["GET"])
def api_cheats_dice_get():
    """Get dice cheat status for current branch."""
    story_id = _active_story_id()
    branch_id = request.args.get("branch_id", "main")
    story_dir = _story_dir(story_id)
    return jsonify({
        "always_success": get_dice_always_success(story_dir, branch_id),
        "dice_modifier": get_dice_modifier(story_dir, branch_id),
    })


@app.route("/api/cheats/dice", methods=["POST"])
def api_cheats_dice_set():
    """Toggle dice always-success mode."""
    body = request.get_json(force=True)
    story_id = _active_story_id()
    branch_id = body.get("branch_id", "main")
    story_dir = _story_dir(story_id)

    if "always_success" in body:
        enabled = bool(body["always_success"])
        set_dice_always_success(story_dir, branch_id, enabled)
        log.info("cheats/dice: always_success=%s branch=%s", enabled, branch_id)

    return jsonify({"ok": True, "always_success": get_dice_always_success(story_dir, branch_id)})


@app.route("/api/cheats/fate", methods=["GET"])
def api_cheats_fate_get():
    """Get fate direction mode (命運走向) status for current branch."""
    story_id = _active_story_id()
    branch_id = request.args.get("branch_id", "main")
    story_dir = _story_dir(story_id)
    return jsonify({"fate_mode": get_fate_mode(story_dir, branch_id)})


@app.route("/api/cheats/fate", methods=["POST"])
def api_cheats_fate_set():
    """Toggle fate direction mode (命運走向)."""
    body = request.get_json(force=True)
    story_id = _active_story_id()
    branch_id = body.get("branch_id", "main")
    story_dir = _story_dir(story_id)

    if "fate_mode" in body:
        enabled = bool(body["fate_mode"])
        set_fate_mode(story_dir, branch_id, enabled)
        log.info("cheats/fate: fate_mode=%s branch=%s", enabled, branch_id)

    return jsonify({"ok": True, "fate_mode": get_fate_mode(story_dir, branch_id)})


@app.route("/api/cheats/pistol", methods=["GET"])
def api_cheats_pistol_get():
    """Get pistol mode (手槍模式) status for current branch."""
    story_id = _active_story_id()
    branch_id = request.args.get("branch_id", "main")
    story_dir = _story_dir(story_id)
    return jsonify({"pistol_mode": get_pistol_mode(story_dir, branch_id)})


@app.route("/api/cheats/pistol", methods=["POST"])
def api_cheats_pistol_set():
    """Toggle pistol mode (手槍模式)."""
    body = request.get_json(force=True)
    story_id = _active_story_id()
    branch_id = body.get("branch_id", "main")
    story_dir = _story_dir(story_id)

    if "pistol_mode" in body:
        enabled = bool(body["pistol_mode"])
        set_pistol_mode(story_dir, branch_id, enabled)
        log.info("cheats/pistol: pistol_mode=%s branch=%s", enabled, branch_id)

    return jsonify({"ok": True, "pistol_mode": get_pistol_mode(story_dir, branch_id)})


@app.route("/api/nsfw-preferences", methods=["GET"])
def api_nsfw_preferences_get():
    """Get NSFW preferences (chips + custom) for current story."""
    story_id = _active_story_id()
    return jsonify(_load_nsfw_preferences(story_id))


@app.route("/api/nsfw-preferences", methods=["POST"])
def api_nsfw_preferences_set():
    """Save NSFW preferences (chips + custom) for current story."""
    body = request.get_json(force=True)
    story_id = _active_story_id()
    prefs = {
        "chips": body.get("chips", []),
        "custom": body.get("custom", "").strip(),
        "custom_chips": body.get("custom_chips", {}),
        "hidden_chips": body.get("hidden_chips", []),
        "chip_counts": body.get("chip_counts", {}),
    }
    path = _nsfw_preferences_path(story_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(prefs, f, ensure_ascii=False)
    log.info("nsfw-preferences: saved %d chips + %d chars custom for story=%s",
             len(prefs["chips"]), len(prefs["custom"]), story_id)
    return jsonify({"ok": True})


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

@app.route("/api/usage")
def api_usage():
    """Return token usage summary for a story.

    Query params:
        story_id — defaults to active story
        days     — lookback window (default 7)
        all      — if "true", return cross-story totals instead
    """
    if request.args.get("all") == "true":
        return jsonify(usage_db.get_total_usage())

    story_id = request.args.get("story_id") or _active_story_id()
    try:
        days = int(request.args.get("days", 7))
    except (ValueError, TypeError):
        days = 7
    return jsonify(usage_db.get_usage_summary(story_id, days=days))


# ---------------------------------------------------------------------------
# Dungeon System API
# ---------------------------------------------------------------------------

@app.route("/api/dungeon/enter", methods=["POST"])
def api_dungeon_enter():
    """Enter a new dungeon (initialize dungeon progress)."""
    story_id = request.json.get("story_id", _active_story_id())
    branch_id = request.json.get("branch_id") or _active_branch_id(story_id)
    dungeon_id = request.json.get("dungeon_id")

    if not dungeon_id:
        return jsonify({"error": "dungeon_id required"}), 400

    # Validate dungeon exists
    template = _load_dungeon_template(story_id, dungeon_id)
    if not template:
        return jsonify({"error": f"Dungeon {dungeon_id} not found"}), 404

    # Validate prerequisites
    state = _load_character_state(story_id, branch_id)
    player_rank = _parse_rank(state.get("等級", "E"))
    required_rank = _parse_rank(template["prerequisites"].get("min_rank", "E"))
    if player_rank < required_rank:
        return jsonify({
            "error": "prerequisite_not_met",
            "message": f"需要 {template['prerequisites']['min_rank']} 級以上才能進入此副本"
        }), 400

    # Check if already in a dungeon
    progress = _load_dungeon_progress(story_id, branch_id)
    if progress and progress.get("current_dungeon"):
        current_dungeon_id = progress["current_dungeon"]["dungeon_id"]
        current_template = _load_dungeon_template(story_id, current_dungeon_id)
        return jsonify({
            "error": "already_in_dungeon",
            "message": f"您已在副本【{current_template.get('name', current_dungeon_id)}】中，請先回歸主神空間"
        }), 400

    # Initialize dungeon progress
    try:
        initialize_dungeon_progress(story_id, branch_id, dungeon_id)
    except Exception as e:
        log.exception("Failed to initialize dungeon progress")
        return jsonify({"error": str(e)}), 500

    # Update character state
    state["current_phase"] = "傳送中"
    state["current_status"] = f"準備進入【{template['name']}】副本"
    state["current_dungeon"] = template["name"]
    _save_character_state(story_id, branch_id, state)

    # Advance world time (dungeon enter cost)
    try:
        from world_timer import advance_dungeon_enter
        advance_dungeon_enter(story_id, branch_id, template["name"])
    except ImportError:
        pass  # world_timer module might not exist in older versions

    return jsonify({"success": True, "dungeon": template})


@app.route("/api/dungeon/progress", methods=["GET"])
def api_dungeon_progress():
    """Get current dungeon progress for a branch."""
    story_id = request.args.get("story_id", _active_story_id())
    branch_id = request.args.get("branch_id") or _active_branch_id(story_id)

    progress = _load_dungeon_progress(story_id, branch_id)
    if not progress or not progress.get("current_dungeon"):
        return jsonify({"in_dungeon": False})

    current = progress["current_dungeon"]
    template = _load_dungeon_template(story_id, current["dungeon_id"])
    if not template:
        return jsonify({"error": "Template not found"}), 500

    # Build nodes response (show completed + current + next)
    completed_nodes = set(current.get("completed_nodes", []))
    nodes_response = []
    next_shown = False
    for node in template["mainline"]["nodes"]:
        if node["id"] in completed_nodes:
            nodes_response.append({
                "id": node["id"],
                "title": node["title"],
                "hint": "已完成",
                "status": "completed"
            })
        elif not next_shown:
            is_current = len(nodes_response) == len(completed_nodes)
            nodes_response.append({
                "id": node["id"],
                "title": node["title"],
                "hint": node.get("hint", ""),
                "status": "active" if is_current else "locked"
            })
            if is_current:
                next_shown = True

    # Build areas response (only discovered areas)
    discovered = set(current.get("discovered_areas", []))
    explored = current.get("explored_areas", {})
    areas_response = []
    for area in template.get("areas", []):
        if area["id"] in discovered:
            areas_response.append({
                "id": area["id"],
                "name": area["name"],
                "type": area["type"],
                "status": "explored" if explored.get(area["id"], 0) > 0 else "discovered",
                "exploration": explored.get(area["id"], 0)
            })

    return jsonify({
        "in_dungeon": True,
        "dungeon_id": current["dungeon_id"],
        "dungeon_name": template["name"],
        "difficulty": template["difficulty"],
        "mainline_progress": current["mainline_progress"],
        "exploration_progress": current["exploration_progress"],
        "can_exit": current["mainline_progress"] >= 60,
        "mainline_nodes": nodes_response,
        "map_areas": areas_response,
        "metrics": {
            "explored_areas": len(discovered),
            "total_areas": len(template.get("areas", [])),
            "completed_nodes": len(completed_nodes),
            "total_nodes": len(template["mainline"]["nodes"])
        }
    })


@app.route("/api/dungeon/return", methods=["POST"])
def api_dungeon_return():
    """Return to Main God Space from current dungeon."""
    story_id = request.json.get("story_id", _active_story_id())
    branch_id = request.json.get("branch_id") or _active_branch_id(story_id)

    progress = _load_dungeon_progress(story_id, branch_id)
    if not progress or not progress.get("current_dungeon"):
        return jsonify({
            "error": "not_in_dungeon",
            "message": "當前不在副本中"
        }), 400

    current = progress["current_dungeon"]
    mainline_pct = current["mainline_progress"]

    # GD-C1: allow early exit at >= 60%, but penalise reward; block below 60%
    if mainline_pct < 60:
        return jsonify({
            "error": "incomplete_mainline",
            "message": f"主線進度僅 {mainline_pct}%，需達 60% 才能提前回歸（100% 可正常回歸）",
            "current_progress": mainline_pct
        }), 400

    is_early_exit = mainline_pct < 100

    # Calculate completion reward
    template = _load_dungeon_template(story_id, current["dungeon_id"])
    if not template:
        return jsonify({"error": "Template not found"}), 500

    rules = template["progression_rules"]
    base = rules["base_reward"]
    mainline_bonus = base * (rules["mainline_multiplier"] - 1) * (mainline_pct / 100)
    exploration_bonus = base * (rules["exploration_multiplier"] - 1) * (current["exploration_progress"] / 100)
    total_reward = int(base + mainline_bonus + exploration_bonus)

    # Apply early-exit penalty (60-99%: reward × 0.5)
    early_penalty = 1.0
    if is_early_exit:
        early_penalty = 0.5
        total_reward = int(total_reward * early_penalty)

    # Apply difficulty scaling (if player over-leveled)
    state = _load_character_state(story_id, branch_id)
    player_rank = state.get("等級", "E")
    scaling = rules.get("difficulty_scaling", {}).get(player_rank, 1.0)
    total_reward = int(total_reward * scaling)

    # Archive dungeon
    exit_reason = "early" if is_early_exit else "normal"
    archive_current_dungeon(story_id, branch_id, exit_reason=exit_reason)

    # Update character state
    state["current_phase"] = "主神空間"
    state["current_status"] = f"副本結束，回歸主神空間。獲得獎勵點數 {total_reward}"
    state["current_dungeon"] = ""
    state["reward_points"] = state.get("reward_points", 0) + total_reward
    _save_character_state(story_id, branch_id, state)

    # Advance world time (recovery time)
    try:
        from world_timer import advance_dungeon_exit
        advance_dungeon_exit(story_id, branch_id)
    except ImportError:
        pass

    return jsonify({
        "success": True,
        "reward_points": total_reward,
        "scaling": scaling,
        "exit_reason": exit_reason,
        "early_penalty": early_penalty,
        "message": (
            f"提前回歸（主線 {mainline_pct}%），獎勵打折 50%。獲得 {total_reward} 點"
            if is_early_exit
            else f"副本完成！獲得獎勵點數 {total_reward}"
        )
    })


def _init_dungeon_templates():
    """Ensure dungeon templates exist for all stories on startup."""
    if not os.path.exists(STORIES_DIR):
        return
    for story_dir_name in os.listdir(STORIES_DIR):
        story_path = os.path.join(STORIES_DIR, story_dir_name)
        if os.path.isdir(story_path):
            ensure_dungeon_templates(story_dir_name)


if __name__ == "__main__":
    _ensure_data_dir()
    _cleanup_incomplete_branches()
    _init_lore_indexes()
    _init_dungeon_templates()
    port = int(os.environ.get("PORT", 5051))
    app.run(debug=True, host="0.0.0.0", port=port)
