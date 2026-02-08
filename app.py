"""Flask backend for 主神空間 RPG Web App — multi-story support."""

import copy
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("rpg")

from llm_bridge import call_claude_gm, call_claude_gm_stream, generate_story_summary
from event_db import insert_event, search_relevant_events, get_events, get_event_by_id, update_event_status, search_events as search_events_db
from image_gen import generate_image_async, get_image_status, get_image_path
from lore_db import rebuild_index as rebuild_lore_index, search_relevant_lore, upsert_entry as upsert_lore_entry, get_toc as get_lore_toc, delete_entry as delete_lore_entry
from npc_evolution import should_run_evolution, run_npc_evolution_async, get_recent_activities, get_all_activities
from auto_summary import get_summaries
from dice import roll_fate, format_dice_context
from parser import parse_conversation, save_parsed
from prompts import SYSTEM_PROMPT_TEMPLATE, build_system_prompt
from compaction import (
    load_recap, save_recap, get_recap_text, should_compact, compact_async,
    get_context_window, copy_recap_to_branch, RECENT_WINDOW as RECENT_MESSAGE_COUNT,
)
from world_timer import process_time_tags, get_world_day, copy_world_day

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STORIES_DIR = os.path.join(DATA_DIR, "stories")
STORIES_REGISTRY_PATH = os.path.join(DATA_DIR, "stories.json")

# Legacy paths — used only during migration
CONVERSATION_PATH = os.path.join(BASE_DIR, "Grok_conversation.md")
LEGACY_PARSED_PATH = os.path.join(DATA_DIR, "parsed_conversation.json")
LEGACY_TREE_PATH = os.path.join(DATA_DIR, "timeline_tree.json")
LEGACY_CHARACTER_STATE_PATH = os.path.join(DATA_DIR, "character_state.json")
LEGACY_SUMMARY_PATH = os.path.join(DATA_DIR, "story_summary.txt")
LEGACY_NEW_MESSAGES_PATH = os.path.join(DATA_DIR, "new_messages.json")


DEFAULT_CHARACTER_STATE = {
    "name": "Eddy",
    "gene_lock": "未開啟（進度 15%）",
    "physique": "普通人類（稍強）",
    "spirit": "普通人類（偏高）",
    "reward_points": 5000,
    "inventory": ["封印之鏡（紀念品）", "自省之鏡玉佩", "鎮魂符×3"],
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
        {"key": "gene_lock", "label": "基因鎖", "type": "text"},
        {"key": "physique", "label": "體質", "type": "text"},
        {"key": "spirit", "label": "精神力", "type": "text"},
        {"key": "reward_points", "label": "獎勵點", "type": "number", "highlight": True, "suffix": " 點"},
        {"key": "current_status", "label": "狀態", "type": "text"},
    ],
    "lists": [
        {"key": "inventory", "label": "道具欄", "state_add_key": "inventory_add", "state_remove_key": "inventory_remove"},
        {"key": "completed_missions", "label": "已完成任務", "state_add_key": "completed_missions_add"},
        {"key": "relationships", "label": "人際關係", "type": "map"},
    ],
    "direct_overwrite_keys": ["gene_lock", "physique", "spirit", "current_status"],
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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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


def _story_tree_path(story_id: str) -> str:
    return os.path.join(_story_dir(story_id), "timeline_tree.json")


def _story_parsed_path(story_id: str) -> str:
    return os.path.join(_story_dir(story_id), "parsed_conversation.json")


def _story_summary_path(story_id: str) -> str:
    return os.path.join(_story_dir(story_id), "story_summary.txt")


def _branch_dir(story_id: str, branch_id: str) -> str:
    d = os.path.join(_story_dir(story_id), "branches", branch_id)
    os.makedirs(d, exist_ok=True)
    return d


def _story_messages_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "messages.json")


def _story_character_state_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "character_state.json")


def _story_system_prompt_path(story_id: str) -> str:
    return os.path.join(_story_dir(story_id), "system_prompt.txt")


def _branch_config_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "config.json")


def _load_branch_config(story_id: str, branch_id: str) -> dict:
    return _load_json(_branch_config_path(story_id, branch_id), {})


def _save_branch_config(story_id: str, branch_id: str, config: dict):
    _save_json(_branch_config_path(story_id, branch_id), config)


def _story_character_schema_path(story_id: str) -> str:
    return os.path.join(_story_dir(story_id), "character_schema.json")


def _story_default_character_state_path(story_id: str) -> str:
    return os.path.join(_story_dir(story_id), "default_character_state.json")


# ---------------------------------------------------------------------------
# Helpers — Story-scoped loaders
# ---------------------------------------------------------------------------

def _load_tree(story_id: str) -> dict:
    return _load_json(_story_tree_path(story_id), {})


def _save_tree(story_id: str, tree: dict):
    _save_json(_story_tree_path(story_id), tree)


def _load_summary(story_id: str) -> str:
    path = _story_summary_path(story_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
            if text:
                return text
    return ""


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


def _build_story_system_prompt(story_id: str, state_text: str, summary: str, branch_id: str = "main", narrative_recap: str = "") -> str:
    """Read the story's system_prompt.txt and fill in placeholders."""
    # Blank branches are fresh starts — no story summary or NPC context from parent
    tree = _load_tree(story_id)
    branch = tree.get("branches", {}).get(branch_id, {})
    if branch.get("blank"):
        summary = ""

    if not narrative_recap:
        narrative_recap = "（尚無回顧，完整對話記錄已提供。）"

    prompt_path = _story_system_prompt_path(story_id)
    lore_text = _build_lore_text(story_id)
    npc_text = _build_npc_text(story_id, branch_id)
    branch_config = _load_branch_config(story_id, branch_id)
    team_mode = branch_config.get("team_mode", "free_agent")
    team_rules = _TEAM_RULES.get(team_mode, _TEAM_RULES["free_agent"])
    if os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            template = f.read()
        return template.format(
            character_state=state_text,
            story_summary=summary,
            world_lore=lore_text,
            npc_profiles=npc_text,
            team_rules=team_rules,
            narrative_recap=narrative_recap,
            other_agents="（目前無其他輪迴者資料）",
        )
    # Fallback to prompts.py template
    return build_system_prompt(state_text, summary)


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
    return os.path.join(_story_dir(story_id), "world_lore.json")


def _load_lore(story_id: str) -> list[dict]:
    return _load_json(_story_lore_path(story_id), [])


def _save_lore_entry(story_id: str, entry: dict):
    """Save a lore entry. If same topic exists, update it. Also updates search index."""
    lore = _load_lore(story_id)
    topic = entry.get("topic", "").strip()
    if not topic:
        return
    # Update existing topic or append new
    for i, existing in enumerate(lore):
        if existing.get("topic") == topic:
            # Preserve category if not provided in new entry
            if "category" not in entry and "category" in existing:
                entry["category"] = existing["category"]
            lore[i] = entry
            _save_json(_story_lore_path(story_id), lore)
            upsert_lore_entry(story_id, entry)
            return
    lore.append(entry)
    _save_json(_story_lore_path(story_id), lore)
    upsert_lore_entry(story_id, entry)


def _build_lore_text(story_id: str) -> str:
    """Build lore TOC for system prompt. Full content is injected per-turn via search."""
    toc = get_lore_toc(story_id)
    if toc == "（尚無已確立的世界設定）":
        # Fallback: try building from JSON directly (before index is built)
        lore = _load_lore(story_id)
        if not lore:
            return "（尚無已確立的世界設定）"
        from collections import OrderedDict
        groups = OrderedDict()
        for entry in lore:
            cat = entry.get("category", "其他")
            if cat not in groups:
                groups[cat] = []
            groups[cat].append(entry)
        lines = []
        for cat, entries in groups.items():
            lines.append(f"### 【{cat}】")
            for entry in entries:
                content = entry.get("content", "（待建立）")
                if content == "（待建立）":
                    lines.append(f"- {entry['topic']}：（待建立）")
                else:
                    lines.append(f"#### {entry['topic']}\n{content}")
            lines.append("")
        return "\n".join(lines).strip()

    return (
        "以下為世界設定目錄（完整內容會在每次對話中根據相關性自動附加）：\n\n"
        + toc
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
            "- 任何表示「獲得道具/裝備」的欄位 → inventory_add（陣列）\n"
            "- 任何表示「失去/消耗道具」的欄位 → inventory_remove（陣列）\n"
            "- 任何表示「獎勵點變化」的欄位 → reward_points_delta（整數）\n"
            "- 任何表示「完成任務」的欄位 → completed_missions_add（陣列）\n"
            "- 已經是標準欄位名的保持不變\n"
            "- 無法映射的自訂欄位（如 location, threat_level 等描述性狀態）保持原樣\n\n"
            f"原始 JSON：\n{json.dumps(update, ensure_ascii=False, indent=2)}\n\n"
            "請只輸出正規化後的 JSON，不要任何解釋。"
        )

        try:
            result = call_oneshot(prompt)
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
            _apply_state_update_inner(story_id, branch_id, normalized,
                                      _load_character_schema(story_id))
        except Exception as e:
            log.info("    state_normalize: failed (%s), skipping", e)

    t = threading.Thread(target=_do_normalize, daemon=True)
    t.start()


def _extract_tags_async(story_id: str, branch_id: str, gm_text: str, msg_index: int, skip_state: bool = False):
    """Background: use LLM to extract structured tags (lore/event/npc/state) from GM response."""
    if len(gm_text) < 200:
        return

    def _do_extract():
        from llm_bridge import call_oneshot
        from event_db import get_event_titles

        try:
            # Collect context for dedup
            toc = get_lore_toc(story_id)
            lore = _load_lore(story_id)
            existing_topics = {e.get("topic", "") for e in lore}
            existing_titles = get_event_titles(story_id, branch_id)

            # Build schema summary for state extraction
            schema = _load_character_schema(story_id)
            schema_lines = []
            for f in schema.get("fields", []):
                schema_lines.append(f"- {f['key']}（{f.get('label', '')}）: {f.get('type', 'text')}")
            for l in schema.get("lists", []):
                ltype = l.get("type", "list")
                if ltype == "map":
                    schema_lines.append(f"- {l['key']}（{l.get('label', '')}）: map，用直接覆蓋")
                else:
                    add_k = l.get("state_add_key", "")
                    rm_k = l.get("state_remove_key", "")
                    schema_lines.append(f"- {l['key']}（{l.get('label', '')}）: list，新增用 {add_k}，移除用 {rm_k}")
            schema_summary = "\n".join(schema_lines)

            state = _load_character_state(story_id, branch_id)
            existing_state_keys = ", ".join(sorted(state.keys()))

            titles_str = ", ".join(sorted(existing_titles)) if existing_titles else "（無）"

            prompt = (
                "你是一個 RPG 結構化資料擷取工具。分析以下 GM 回覆，提取結構化資訊。\n\n"
                f"## GM 回覆\n{gm_text}\n\n"
                "## 1. 世界設定（lore）\n"
                "提取新的世界設定：體系規則、副本背景、場景描述等。不要提取劇情動態或角色行動。\n"
                f"已有設定（避免重複）：\n{toc}\n"
                '格式：[{{"category": "分類", "topic": "主題", "content": "完整描述"}}]\n'
                "可用分類：主神設定與規則/體系/商城/副本世界觀/場景/NPC/故事追蹤\n\n"
                "## 2. 事件追蹤（events）\n"
                "提取重要事件：伏筆、轉折、戰鬥、發現等。不要記錄瑣碎事件。\n"
                f"已有事件標題（避免重複）：{titles_str}\n"
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
                f"角色目前有這些欄位：{existing_state_keys}\n\n"
                "規則：\n"
                "- 列表型欄位用 `_add` / `_remove` 後綴（如 `inventory_add`, `inventory_remove`）\n"
                "- 數值型欄位用 `_delta` 後綴（如 `reward_points_delta: -500`）\n"
                "- 文字型欄位直接覆蓋（如 `gene_lock: \"第二階\"`），值要簡短（5-20字）\n"
                "- 可以新增**永久性角色屬性**（如學會新體系時加 `修真境界`, `法力` 等）\n"
                "- **禁止**新增臨時性/場景性欄位（如 location, threat_level, combat_status, escape_options 等一次性描述）\n"
                '- 角色死亡時 `current_status` 設為 `"end"`\n'
                "格式：只填有變化的欄位。\n\n"
                "## 輸出\n"
                "JSON 物件，只包含有內容的類型：\n"
                '{"lore": [...], "events": [...], "npcs": [...], "state": {...}}\n'
                "沒有新資訊的類型省略或用空陣列/空物件。只輸出 JSON。"
            )

            result = call_oneshot(prompt)
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

            # Lore — dedup by topic
            for entry in data.get("lore", []):
                topic = entry.get("topic", "").strip()
                if topic and topic not in existing_topics:
                    _save_lore_entry(story_id, entry)
                    existing_topics.add(topic)
                    saved_counts["lore"] += 1

            # Events — dedup by title
            for event in data.get("events", []):
                title = event.get("title", "").strip()
                if title and title not in existing_titles:
                    event["message_index"] = msg_index
                    insert_event(story_id, event, branch_id)
                    existing_titles.add(title)
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

            log.info(
                "    extract_tags: saved %d lore, %d events, %d npcs, state %s",
                saved_counts["lore"], saved_counts["events"],
                saved_counts["npcs"],
                "updated" if saved_counts["state"] else "no change",
            )

        except json.JSONDecodeError as e:
            log.warning("    extract_tags: JSON parse failed (%s), skipping", e)
        except Exception as e:
            log.exception("    extract_tags: failed, skipping")

    t = threading.Thread(target=_do_extract, daemon=True)
    t.start()


def _apply_state_update_inner(story_id: str, branch_id: str, update: dict, schema: dict):
    """Core logic: apply a STATE update dict to character state. No normalization."""
    state = _load_character_state(story_id, branch_id)

    # Process list fields from schema
    for list_def in schema.get("lists", []):
        key = list_def["key"]
        list_type = list_def.get("type", "list")

        if list_type == "map":
            if key in update:
                existing = state.get(key, {})
                existing.update(update[key])
                state[key] = existing
        else:
            add_key = list_def.get("state_add_key")
            if add_key and add_key in update:
                lst = state.get(key, [])
                for item in update[add_key]:
                    if item not in lst:
                        lst.append(item)
                state[key] = lst

            remove_key = list_def.get("state_remove_key")
            if remove_key and remove_key in update:
                lst = state.get(key, [])
                for rm_item in update[remove_key]:
                    rm_name = rm_item.split(" — ")[0].strip()
                    lst = [x for x in lst if x.split(" — ")[0].strip() != rm_name]
                state[key] = lst

    # If GM sets reward_points directly instead of using delta, accept it
    if "reward_points" in update and "reward_points_delta" not in update:
        val = update["reward_points"]
        if isinstance(val, (int, float)):
            state["reward_points"] = int(val)

    # reward_points_delta
    if "reward_points_delta" in update:
        state["reward_points"] = state.get("reward_points", 0) + update["reward_points_delta"]

    # Direct overwrite fields from schema
    for key in schema.get("direct_overwrite_keys", []):
        if key in update:
            state[key] = update[key]

    # Save extra keys not handled above
    handled_keys = set()
    handled_keys.add("reward_points_delta")
    handled_keys.add("reward_points")
    for list_def in schema.get("lists", []):
        handled_keys.add(list_def["key"])
        if list_def.get("state_add_key"):
            handled_keys.add(list_def["state_add_key"])
        if list_def.get("state_remove_key"):
            handled_keys.add(list_def["state_remove_key"])
    for key in schema.get("direct_overwrite_keys", []):
        handled_keys.add(key)
    for key, val in update.items():
        if key not in handled_keys and isinstance(val, (str, int, float, bool)):
            state[key] = val

    _save_json(_story_character_state_path(story_id, branch_id), state)


def _apply_state_update(story_id: str, branch_id: str, update: dict):
    """Apply a STATE update dict to the branch's character state file.

    1. Immediately applies the raw update (never blocks)
    2. Kicks off background LLM normalization for non-standard fields
    """
    schema = _load_character_schema(story_id)

    # Apply immediately with raw update
    _apply_state_update_inner(story_id, branch_id, update, schema)

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
    while cur is not None:
        branch = branches[cur]
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
    while current in branches and current != "main":
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
    while cur is not None:
        ancestor_ids.add(cur)
        branch = branches.get(cur)
        if not branch:
            break
        cur = branch.get("parent_branch_id")

    for bid, branch in branches.items():
        if bid == branch_id or branch.get("deleted") or branch.get("blank") or branch.get("merged"):
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
    while cur is not None:
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
        if b.get("deleted") or b.get("blank") or b.get("merged"):
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
        "story_summary.txt": "story_summary.txt",
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


# ---------------------------------------------------------------------------
# Helpers — unified tag extraction & context injection
# ---------------------------------------------------------------------------

_CONTEXT_ECHO_RE = re.compile(
    r"\[(?:命運判定|命運骰結果|相關世界設定|相關事件追蹤|NPC 近期動態)\].*?(?=\n---\n|\n\n[^\[\n]|\Z)",
    re.DOTALL,
)


def _process_gm_response(gm_response: str, story_id: str, branch_id: str, msg_index: int) -> tuple[str, dict | None, dict]:
    """Extract all hidden tags from GM response. Returns (clean_text, image_info, snapshots)."""
    # Strip context injection sections that the GM may have echoed back
    gm_response = _CONTEXT_ECHO_RE.sub("", gm_response).strip()
    gm_response = re.sub(r"^---\s*", "", gm_response).strip()
    gm_response = re.sub(r"\n---\n", "\n", gm_response).strip()

    gm_response, state_updates = _extract_state_tag(gm_response)
    for state_update in state_updates:
        _apply_state_update(story_id, branch_id, state_update)

    gm_response, lore_entries = _extract_lore_tag(gm_response)
    for lore_entry in lore_entries:
        _save_lore_entry(story_id, lore_entry)

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
    gm_response = process_time_tags(gm_response, story_id, branch_id)

    # Async post-processing: extract structured data via separate LLM call
    # Skip state extraction if regex already found STATE tags (avoid delta double-apply)
    _extract_tags_async(story_id, branch_id, gm_response, msg_index,
                        skip_state=bool(state_updates))

    # Build snapshots for branch forking accuracy
    snapshots = {"state_snapshot": _load_character_state(story_id, branch_id)}
    if npc_updates:
        snapshots["npcs_snapshot"] = _load_npcs(story_id, branch_id)

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


def _find_npcs_at_index(story_id: str, branch_id: str, target_index: int) -> list[dict]:
    """Walk timeline backwards to find most recent npcs_snapshot at or before target_index."""
    timeline = get_full_timeline(story_id, branch_id)
    for msg in reversed(timeline):
        if msg.get("index", 0) > target_index:
            continue
        if "npcs_snapshot" in msg:
            return msg["npcs_snapshot"]
    return []


def _build_augmented_message(
    story_id: str, branch_id: str, user_text: str,
    character_state: dict | None = None,
) -> tuple[str, dict | None]:
    """Add lore + events + NPC activities + dice context to user message.

    Returns (augmented_text, dice_result_or_None).
    """
    # Check if this is a blank branch (fresh start — skip story-specific events)
    tree = _load_tree(story_id)
    is_blank = tree.get("branches", {}).get(branch_id, {}).get("blank", False)

    parts = []
    lore = search_relevant_lore(story_id, user_text, limit=5)
    if lore:
        parts.append(lore)
    if not is_blank:
        events = search_relevant_events(story_id, user_text, branch_id, limit=3)
        if events:
            parts.append(events)
    activities = get_recent_activities(story_id, branch_id, limit=2)
    if activities:
        parts.append(activities)

    # Dice roll
    dice_result = None
    if character_state:
        dice_result = roll_fate(character_state)
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

    # 2. Parse conversation (for original story)
    parsed_path = _story_parsed_path(story_id)
    if not os.path.exists(parsed_path):
        if os.path.exists(CONVERSATION_PATH):
            save_parsed()
            if os.path.exists(LEGACY_PARSED_PATH):
                shutil.copy2(LEGACY_PARSED_PATH, parsed_path)
        else:
            _save_json(parsed_path, [])
    original = _load_json(parsed_path, [])

    # 3. Timeline tree migration
    _migrate_to_timeline_tree(story_id)

    # 3b. Branch files migration (flat → branches/ dirs)
    _migrate_branch_files(story_id)

    tree = _load_tree(story_id)

    # 4. Character state
    active_branch = tree.get("active_branch_id", "main")
    _load_character_state(story_id, active_branch)

    # 5. Story summary
    summary = _load_summary(story_id)
    if not summary and os.path.exists(CONVERSATION_PATH):
        summary_path = _story_summary_path(story_id)
        def _gen_summary():
            with open(CONVERSATION_PATH, "r", encoding="utf-8") as f:
                full_text = f.read()
            generate_story_summary(full_text, summary_path)
        threading.Thread(target=_gen_summary, daemon=True).start()

    # 6. Ensure main messages file exists
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
        "has_summary": bool(summary),
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

    # 2. Build system prompt (with narrative recap)
    t0 = time.time()
    state = _load_character_state(story_id, branch_id)
    summary = _load_summary(story_id)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    recap_text = get_recap_text(story_id, branch_id)
    system_prompt = _build_story_system_prompt(story_id, state_text, summary, branch_id, narrative_recap=recap_text)
    log.info("  build_prompt: %.0fms", (time.time() - t0) * 1000)

    # 3. Gather recent context
    recent = full_timeline[-RECENT_MESSAGE_COUNT:]

    # 3b. Search relevant lore/events/activities and prepend to user message
    t0 = time.time()
    augmented_text, dice_result = _build_augmented_message(story_id, branch_id, user_text, state)
    if dice_result:
        player_msg["dice"] = dice_result
        _save_json(delta_path, delta_msgs)
    log.info("  context_search: %.0fms", (time.time() - t0) * 1000)

    # 4. Call Claude (stateless)
    t0 = time.time()
    gm_response, _ = call_claude_gm(
        augmented_text, system_prompt, recent, session_id=None
    )
    log.info("  claude_call: %.1fs", time.time() - t0)

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

    log.info("/api/send DONE   total=%.1fs", time.time() - t_start)
    return jsonify({"ok": True, "player": player_msg, "gm": gm_msg})


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

    # 2. Build system prompt (with narrative recap)
    state = _load_character_state(story_id, branch_id)
    summary = _load_summary(story_id)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    recap_text = get_recap_text(story_id, branch_id)
    system_prompt = _build_story_system_prompt(story_id, state_text, summary, branch_id, narrative_recap=recap_text)

    # 3. Gather recent context
    recent = full_timeline[-RECENT_MESSAGE_COUNT:]
    augmented_text, dice_result = _build_augmented_message(story_id, branch_id, user_text, state)
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

                    log.info("/api/send/stream DONE total=%.1fs", time.time() - t_start)
                    yield _sse_event({
                        "type": "done",
                        "gm_msg": gm_msg,
                        "branch": tree["branches"][branch_id],
                    })
        except Exception as e:
            log.info("/api/send/stream EXCEPTION %s", e)
            yield _sse_event({"type": "error", "message": str(e)})

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/status")
def api_status():
    """Return character state for a branch."""
    story_id = _active_story_id()
    branch_id = request.args.get("branch_id", "main")
    state = dict(_load_character_state(story_id, branch_id))
    state["world_day"] = get_world_day(story_id, branch_id)
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
               if not b.get("deleted") and not b.get("merged")}
    return jsonify({
        "active_branch_id": tree.get("active_branch_id", "main"),
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
    parent_branch_id = _resolve_sibling_parent(branches, parent_branch_id, branch_point_index)

    if parent_branch_id not in branches:
        return jsonify({"ok": False, "error": "parent branch not found"}), 404

    branch_id = f"branch_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    forked_state = _find_state_at_index(story_id, parent_branch_id, branch_point_index)
    _save_json(_story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = _find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    _save_json(_story_npcs_path(story_id, branch_id), forked_npcs)
    _save_branch_config(story_id, branch_id, _load_branch_config(story_id, parent_branch_id))
    copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    copy_world_day(story_id, parent_branch_id, branch_id)

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
    _save_tree(story_id, tree)

    return jsonify({"ok": True, "branch": branches[branch_id]})


@app.route("/api/branches/switch", methods=["POST"])
def api_branches_switch():
    """Switch active branch."""
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "main")

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    if branch_id not in tree.get("branches", {}):
        return jsonify({"ok": False, "error": "branch not found"}), 404

    tree["active_branch_id"] = branch_id
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
    _save_json(_story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = _find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    _save_json(_story_npcs_path(story_id, branch_id), forked_npcs)
    _save_branch_config(story_id, branch_id, _load_branch_config(story_id, parent_branch_id))
    copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    copy_world_day(story_id, parent_branch_id, branch_id)

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
    _save_tree(story_id, tree)

    t0 = time.time()
    full_timeline = get_full_timeline(story_id, branch_id)
    state = _load_character_state(story_id, branch_id)
    summary = _load_summary(story_id)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    recap_text = get_recap_text(story_id, branch_id)
    system_prompt = _build_story_system_prompt(story_id, state_text, summary, branch_id, narrative_recap=recap_text)
    recent = full_timeline[-RECENT_MESSAGE_COUNT:]
    log.info("  build_prompt: %.0fms", (time.time() - t0) * 1000)

    t0 = time.time()
    augmented_edit, dice_result = _build_augmented_message(story_id, branch_id, edited_message, state)
    if dice_result:
        user_msg["dice"] = dice_result
        _save_json(_story_messages_path(story_id, branch_id), delta)
    log.info("  context_search: %.0fms", (time.time() - t0) * 1000)

    t0 = time.time()
    gm_response, _ = call_claude_gm(
        augmented_edit, system_prompt, recent, session_id=None
    )
    log.info("  claude_call: %.1fs", time.time() - t0)

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
    _save_json(_story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = _find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    _save_json(_story_npcs_path(story_id, branch_id), forked_npcs)
    _save_branch_config(story_id, branch_id, _load_branch_config(story_id, parent_branch_id))
    copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    copy_world_day(story_id, parent_branch_id, branch_id)

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
    _save_tree(story_id, tree)

    # Build prompt context
    full_timeline = get_full_timeline(story_id, branch_id)
    state = _load_character_state(story_id, branch_id)
    summary = _load_summary(story_id)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    recap_text = get_recap_text(story_id, branch_id)
    system_prompt = _build_story_system_prompt(story_id, state_text, summary, branch_id, narrative_recap=recap_text)
    recent = full_timeline[-RECENT_MESSAGE_COUNT:]
    augmented_edit, dice_result = _build_augmented_message(story_id, branch_id, edited_message, state)
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
                    yield _sse_event({"type": "error", "message": payload})
                    return
                elif event_type == "done":
                    gm_response = payload["response"]

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
            log.info("/api/branches/edit/stream EXCEPTION %s", e)
            yield _sse_event({"type": "error", "message": str(e)})

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
    _save_json(_story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = _find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    _save_json(_story_npcs_path(story_id, branch_id), forked_npcs)
    _save_branch_config(story_id, branch_id, _load_branch_config(story_id, parent_branch_id))
    copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    copy_world_day(story_id, parent_branch_id, branch_id)

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
    _save_tree(story_id, tree)

    t0 = time.time()
    full_timeline = get_full_timeline(story_id, branch_id)
    state = _load_character_state(story_id, branch_id)
    summary = _load_summary(story_id)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    recap_text = get_recap_text(story_id, branch_id)
    system_prompt = _build_story_system_prompt(story_id, state_text, summary, branch_id, narrative_recap=recap_text)
    recent = full_timeline[-RECENT_MESSAGE_COUNT:]
    log.info("  build_prompt: %.0fms", (time.time() - t0) * 1000)

    t0 = time.time()
    augmented_regen, dice_result = _build_augmented_message(story_id, branch_id, user_msg_content, state)
    log.info("  context_search: %.0fms", (time.time() - t0) * 1000)

    t0 = time.time()
    gm_response, _ = call_claude_gm(
        augmented_regen, system_prompt, recent, session_id=None
    )
    log.info("  claude_call: %.1fs", time.time() - t0)

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
    _save_json(_story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = _find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    _save_json(_story_npcs_path(story_id, branch_id), forked_npcs)
    _save_branch_config(story_id, branch_id, _load_branch_config(story_id, parent_branch_id))
    copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    copy_world_day(story_id, parent_branch_id, branch_id)
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
    _save_tree(story_id, tree)

    # Build prompt context
    full_timeline = get_full_timeline(story_id, branch_id)
    state = _load_character_state(story_id, branch_id)
    summary = _load_summary(story_id)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    recap_text = get_recap_text(story_id, branch_id)
    system_prompt = _build_story_system_prompt(story_id, state_text, summary, branch_id, narrative_recap=recap_text)
    recent = full_timeline[-RECENT_MESSAGE_COUNT:]
    augmented_regen, dice_result = _build_augmented_message(story_id, branch_id, user_msg_content, state)

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
                    yield _sse_event({"type": "error", "message": payload})
                    return
                elif event_type == "done":
                    gm_response = payload["response"]

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
            log.info("/api/branches/regenerate/stream EXCEPTION %s", e)
            yield _sse_event({"type": "error", "message": str(e)})

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/branches/promote", methods=["POST"])
def api_branches_promote():
    """Promote a branch to become the new main timeline."""
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "").strip()

    if not branch_id or branch_id == "main":
        return jsonify({"ok": False, "error": "invalid branch_id"}), 400

    story_id = _active_story_id()
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})

    if branch_id not in branches:
        return jsonify({"ok": False, "error": "branch not found"}), 404

    original = _load_json(_story_parsed_path(story_id), [])
    original_count = len(original)
    full_timeline = get_full_timeline(story_id, branch_id)
    new_messages = [m for m in full_timeline if m.get("index", 0) >= original_count]
    for m in new_messages:
        m.pop("owner_branch_id", None)
        m.pop("inherited", None)

    ancestor_chain = []
    cur = branch_id
    while cur is not None and cur != "main":
        ancestor_chain.append(cur)
        cur = branches[cur].get("parent_branch_id")

    _save_json(_story_messages_path(story_id, "main"), new_messages)

    # Copy recap and world_day from promoted branch to main
    copy_recap_to_branch(story_id, branch_id, "main", -1)
    copy_world_day(story_id, branch_id, "main")

    src_char = _story_character_state_path(story_id, branch_id)
    dst_char = _story_character_state_path(story_id, "main")
    if os.path.exists(src_char):
        shutil.copy2(src_char, dst_char)

    # Copy NPC data from promoted branch to main
    src_npcs = _story_npcs_path(story_id, branch_id)
    dst_npcs = _story_npcs_path(story_id, "main")
    if os.path.exists(src_npcs):
        shutil.copy2(src_npcs, dst_npcs)

    branches_to_remove = set(ancestor_chain)
    for bid, b in list(branches.items()):
        if bid == "main":
            continue
        if b.get("parent_branch_id") in branches_to_remove:
            if bid in branches_to_remove:
                continue
            b["parent_branch_id"] = "main"

    # Soft-delete ancestor branches (preserve data for timeline reconstruction)
    now = datetime.now(timezone.utc).isoformat()
    for bid in ancestor_chain:
        branches[bid]["deleted"] = True
        branches[bid]["deleted_at"] = now
        branches[bid]["was_main"] = True

    tree["active_branch_id"] = "main"
    _save_tree(story_id, tree)

    return jsonify({"ok": True, "active_branch_id": "main"})


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

    # Collect this branch + all descendants (BFS)
    to_delete = []
    queue = [branch_id]
    while queue:
        bid = queue.pop(0)
        to_delete.append(bid)
        for b in branches.values():
            if b.get("parent_branch_id") == bid and b["id"] not in to_delete and not b.get("deleted"):
                queue.append(b["id"])

    for bid in to_delete:
        b = branches.get(bid)
        if not b:
            continue
        if b.get("was_main"):
            now = datetime.now(timezone.utc).isoformat()
            b["deleted"] = True
            b["deleted_at"] = now
        else:
            bdir = _branch_dir(story_id, bid)
            if os.path.isdir(bdir):
                shutil.rmtree(bdir)
            del branches[bid]

    if tree.get("active_branch_id") in to_delete:
        tree["active_branch_id"] = "main"

    _save_tree(story_id, tree)

    return jsonify({"ok": True})


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
    summary = _load_summary(story_id)
    story_meta = registry["stories"][story_id]
    character_schema = _load_character_schema(story_id)

    return jsonify({
        "ok": True,
        "active_story_id": story_id,
        "story_name": story_meta.get("name", story_id),
        "active_branch_id": active_branch,
        "original_count": len(original),
        "has_summary": bool(summary),
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

    # Remove story directory
    story_dir = _story_dir(story_id)
    if os.path.exists(story_dir):
        shutil.rmtree(story_dir)

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


# ---------------------------------------------------------------------------
# Lore Console page + CRUD + LLM chat
# ---------------------------------------------------------------------------

@app.route("/lore")
def lore_page():
    """Render the Lore Console page."""
    return render_template("lore.html")


@app.route("/api/lore/all")
def api_lore_all():
    """Return all lore entries grouped by category."""
    story_id = _active_story_id()
    lore = _load_lore(story_id)
    # Collect categories in order of first appearance
    categories = list(dict.fromkeys(e.get("category", "其他") for e in lore))
    return jsonify({"ok": True, "entries": lore, "categories": categories})


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
    # Check for duplicate topic
    lore = _load_lore(story_id)
    for e in lore:
        if e.get("topic") == topic:
            return jsonify({"ok": False, "error": f"topic '{topic}' already exists"}), 409
    entry = {"category": category, "topic": topic, "content": content}
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

    lore = _load_lore(story_id)
    found = False
    for i, e in enumerate(lore):
        if e.get("topic") == topic:
            found = True
            new_topic = body.get("new_topic", topic).strip()
            new_category = body.get("category", e.get("category", "其他")).strip()
            new_content = body.get("content", e.get("content", "")).strip()
            # If renaming, delete old index entry
            if new_topic != topic:
                delete_lore_entry(story_id, topic)
            lore[i] = {"category": new_category, "topic": new_topic, "content": new_content}
            _save_json(_story_lore_path(story_id), lore)
            upsert_lore_entry(story_id, lore[i])
            return jsonify({"ok": True, "entry": lore[i]})
    if not found:
        return jsonify({"ok": False, "error": "entry not found"}), 404


@app.route("/api/lore/entry", methods=["DELETE"])
def api_lore_entry_delete():
    """Delete a lore entry by topic."""
    story_id = _active_story_id()
    body = request.get_json(force=True)
    topic = body.get("topic", "").strip()
    if not topic:
        return jsonify({"ok": False, "error": "topic required"}), 400

    lore = _load_lore(story_id)
    new_lore = [e for e in lore if e.get("topic") != topic]
    if len(new_lore) == len(lore):
        return jsonify({"ok": False, "error": "entry not found"}), 404
    _save_json(_story_lore_path(story_id), new_lore)
    delete_lore_entry(story_id, topic)
    return jsonify({"ok": True})


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
        if cat not in groups:
            groups[cat] = []
        groups[cat].append(e)
    for cat, entries in groups.items():
        lore_text_parts.append(f"### 【{cat}】")
        for e in entries:
            lore_text_parts.append(f"#### {e['topic']}")
            lore_text_parts.append(e.get("content", ""))
            lore_text_parts.append("")

    lore_system = f"""你是世界設定管理助手，協助維護 RPG 世界的設定知識庫。

角色：討論/新增/修改/刪除設定，確保一致性，用繁體中文回覆。

現有設定（{len(lore)} 條）：
{chr(10).join(lore_text_parts)}

提案格式（當建議變更時使用）：
<!--LORE_PROPOSE {{"action":"add|edit|delete", "category":"...", "topic":"...", "content":"..."}} LORE_PROPOSE-->

規則：
- 先討論再提案，確認用戶意圖後再輸出提案標籤
- content 欄位是完整的設定文字（不是差異）
- delete 操作不需要 content 欄位
- 可在一次回覆中輸出多個提案標籤
- 提案標籤必須放在回覆最末尾"""

    # Extract prior messages and last user message
    prior = [{"role": m["role"], "content": m["content"]} for m in messages[:-1]]
    last_user_msg = messages[-1].get("content", "")

    def generate():
        try:
            for event_type, payload in call_claude_gm_stream(
                last_user_msg, lore_system, prior, session_id=None
            ):
                if event_type == "text":
                    yield _sse_event({"type": "text", "chunk": payload})
                elif event_type == "error":
                    yield _sse_event({"type": "error", "message": payload})
                    return
                elif event_type == "done":
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
                    yield _sse_event({
                        "type": "done",
                        "response": display_text,
                        "proposals": proposals,
                    })
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
            }
            _save_lore_entry(story_id, entry)
            applied.append({"action": "add", "topic": topic})
        elif action == "edit":
            entry = {
                "category": p.get("category", "其他"),
                "topic": topic,
                "content": p.get("content", ""),
            }
            _save_lore_entry(story_id, entry)
            applied.append({"action": "edit", "topic": topic})
        elif action == "delete":
            lore = _load_lore(story_id)
            new_lore = [e for e in lore if e.get("topic") != topic]
            if len(new_lore) < len(lore):
                _save_json(_story_lore_path(story_id), new_lore)
                delete_lore_entry(story_id, topic)
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
# Auto-Play Summaries API
# ---------------------------------------------------------------------------

@app.route("/api/auto-play/summaries")
def api_auto_play_summaries():
    """Return auto-play summaries for a branch."""
    story_id = _active_story_id()
    branch_id = request.args.get("branch_id", "main")
    return jsonify({"ok": True, "summaries": get_summaries(story_id, branch_id)})


# ---------------------------------------------------------------------------
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
# Entry point
# ---------------------------------------------------------------------------

def _init_lore_indexes():
    """Rebuild lore search indexes for all stories on startup."""
    if not os.path.exists(STORIES_DIR):
        return
    for story_dir_name in os.listdir(STORIES_DIR):
        lore_path = os.path.join(STORIES_DIR, story_dir_name, "world_lore.json")
        if os.path.exists(lore_path):
            rebuild_lore_index(story_dir_name)


if __name__ == "__main__":
    _ensure_data_dir()
    _init_lore_indexes()
    app.run(debug=True, host='0.0.0.0', port=5051)
