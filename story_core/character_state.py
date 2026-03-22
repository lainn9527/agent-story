"""Character state loading, schema helpers, and system prompt construction."""

import copy
import json
import logging
import os
import re

from story_core.dungeon_system import build_dungeon_context
from story_core.gm_cheats import get_fate_mode, get_pistol_mode
from story_core.npc_helpers import (
    _build_npc_summary_text,
    _classify_npc,
    _load_npcs,
    _normalize_npc_tier,
    _rel_to_str,
)
from story_core.prompts import build_system_prompt
from story_core.story_io import (
    _get_image_model,
    _is_image_gen_enabled,
    _load_branch_config,
    _load_tree,
    _load_json,
    _nsfw_preferences_path,
    _save_json,
    _story_character_schema_path,
    _story_character_state_path,
    _story_default_character_state_path,
    _story_dir,
    _story_system_prompt_path,
)
from story_core.world_timer import get_world_day

log = logging.getLogger("rpg")

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
_STATE_CORE_EXTRA_KEYS = ("base_power_level", "health", "spirit_status")
STORY_ANCHOR_LIMIT = 10


def _is_numeric_value(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalize_story_anchor_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^[\-\*\u2022\u2027]+\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text[:160].strip()


def _normalize_story_anchors(raw_anchors: object, limit: int = STORY_ANCHOR_LIMIT) -> list[str]:
    if not isinstance(raw_anchors, list):
        return []
    normalized = []
    seen = set()
    for item in raw_anchors:
        text = _normalize_story_anchor_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
        if len(normalized) >= limit:
            break
    return normalized


def _load_nsfw_preferences(story_id: str) -> dict:
    """Return {"chips": [...], "custom": "..."} or empty dict."""
    path = _nsfw_preferences_path(story_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _format_nsfw_preferences(prefs: dict) -> str:
    """Format chips (by group) + custom text into a structured prompt string."""
    chips = prefs.get("chips", {})
    lines = []
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


def _load_character_schema(story_id: str) -> dict:
    from app import DEFAULT_CHARACTER_SCHEMA

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
    for field in schema.get("lists", []):
        key = field["key"]
        if field.get("type") == "map":
            state[key] = {}
        else:
            state[key] = []
    state["story_anchors"] = []
    return state


def _load_character_state(story_id: str, branch_id: str = "main") -> dict:
    from app import DEFAULT_CHARACTER_STATE, _get_schema_known_keys, _migrate_list_to_map

    path = _story_character_state_path(story_id, branch_id)
    state = _load_json(path, {})
    if not state:
        default_path = _story_default_character_state_path(story_id)
        state = _load_json(default_path, {})
    if not state:
        state = copy.deepcopy(DEFAULT_CHARACTER_STATE)
    if "current_phase" not in state:
        state["current_phase"] = "主神空間"

    dirty = False
    schema = _load_character_schema(story_id)
    known = _get_schema_known_keys(schema)
    buggy_list_keys = set()
    for field in schema.get("lists", []):
        if field.get("state_add_key"):
            buggy_list_keys.add(field["key"])

    for key in list(state.keys()):
        if key.endswith(("_delta", "_add", "_remove")) and key not in known:
            del state[key]
            dirty = True
    for key in buggy_list_keys:
        value = state.get(key)
        if not isinstance(value, list):
            continue
        single_chars = [item for item in value if isinstance(item, str) and len(item) == 1]
        if len(single_chars) >= 3:
            cleaned = [item for item in value if not isinstance(item, str) or len(item) > 1]
            if len(cleaned) != len(value):
                state[key] = cleaned
                dirty = True

    for field in schema.get("lists", []):
        if field.get("type") != "map":
            continue
        key = field["key"]
        value = state.get(key)
        if isinstance(value, list):
            state[key] = _migrate_list_to_map(value)
            dirty = True
            log.info("    auto-migrate: converted %s from list to map in %s/%s", key, story_id, branch_id)

    anchors = _normalize_story_anchors(state.get("story_anchors", []))
    if anchors != state.get("story_anchors"):
        state["story_anchors"] = anchors
        dirty = True
    elif "story_anchors" not in state:
        state["story_anchors"] = []
        dirty = True

    needs_persist = dirty or not os.path.exists(path)
    if dirty:
        log.info("    self-heal: cleaned artifacts from %s/%s", story_id, branch_id)
    if needs_persist:
        _save_json(path, state)
    return state


def _build_critical_facts(story_id: str, branch_id: str, state: dict, npcs: list[dict]) -> str:
    """Build critical facts section for system prompt to prevent inconsistencies."""
    lines = []

    if state.get("current_phase"):
        lines.append(f"- 當前階段：{state['current_phase']}")

    world_day = get_world_day(story_id, branch_id)
    if world_day:
        day = int(world_day) + 1
        hour = int((world_day % 1) * 24)
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

    if state.get("gene_lock"):
        lines.append(f"- 基因鎖：{state['gene_lock']}")
    if state.get("reward_points") is not None:
        try:
            lines.append(f"- 獎勵點餘額：{int(state['reward_points']):,} 點")
        except (ValueError, TypeError):
            lines.append(f"- 獎勵點餘額：{state['reward_points']} 點")

    inventory = state.get("inventory", {})
    if inventory:
        if isinstance(inventory, dict):
            item_names = list(inventory.keys())[:5]
        else:
            item_names = [item.split("—")[0].split(" — ")[0].strip() for item in inventory[:5]]
        lines.append(f"- 關鍵道具：{'、'.join(item_names)}")

    relationships = state.get("relationships", {})
    if npcs:
        groups: dict[str, list[str]] = {}
        for npc in npcs:
            name = npc.get("name", "?")
            category = _classify_npc(npc, relationships)
            relationship = _rel_to_str(relationships.get(name)) or npc.get("relationship_to_player", "")
            tier = _normalize_npc_tier(npc.get("tier"))
            if relationship and tier:
                entry = f"{name}（{relationship}·{tier}級）"
            elif relationship:
                entry = f"{name}（{relationship}）"
            elif tier:
                entry = f"{name}（{tier}級）"
            else:
                entry = name
            groups.setdefault(category, []).append(entry)
        labels = {
            "ally": "隊友",
            "hostile": "敵對",
            "captured": "俘虜",
            "dead": "已故",
            "neutral": "其他NPC",
        }
        for category in ("ally", "hostile", "captured", "dead", "neutral"):
            members = groups.get(category)
            if members:
                lines.append(f"- {labels[category]}：{'、'.join(members)}")
    elif relationships:
        rel_parts = [f"{name}（{_rel_to_str(rel)}）" for name, rel in relationships.items()]
        lines.append(f"- 人際關係：{'、'.join(rel_parts)}")

    anchors = _normalize_story_anchors(state.get("story_anchors", []))
    if anchors:
        lines.append("")
        lines.append("### 長期記憶")
        for anchor in anchors[:STORY_ANCHOR_LIMIT]:
            lines.append(f"- {anchor}")

    if not lines:
        return "（尚無關鍵事實記錄）"
    return "\n".join(lines)


def _format_state_core_value(key: str, val) -> str:
    if key == "reward_points":
        if _is_numeric_value(val):
            return f"{int(val):,} 點"
        return f"{val} 點"
    return str(val)


def _build_core_state_text(story_id: str, state: dict) -> str:
    """Build compact always-injected state text (core fields + systems)."""
    if not isinstance(state, dict):
        return "（尚無角色核心狀態）"

    schema = _load_character_schema(story_id)
    lines = []
    for field in schema.get("fields", []):
        key = field.get("key")
        if not key or key not in state:
            continue
        value = state.get(key)
        if value in (None, ""):
            continue
        label = field.get("label", key)
        lines.append(f"{label}：{_format_state_core_value(key, value)}")

    systems = state.get("systems", {})
    if isinstance(systems, dict) and systems:
        parts = []
        for name, level in systems.items():
            clean_name = str(name).strip()
            if not clean_name:
                continue
            clean_level = "" if level is None else str(level).strip()
            parts.append(f"{clean_name}({clean_level})" if clean_level else clean_name)
        if parts:
            lines.append(f"體系：{'、'.join(parts)}")

    for key in _STATE_CORE_EXTRA_KEYS:
        value = state.get(key)
        if value in (None, ""):
            continue
        if key == "base_power_level":
            label = "基礎戰力"
        elif key == "health":
            label = "生命狀態"
        else:
            label = "精神狀態"
        lines.append(f"{label}：{value}")

    return "\n".join(lines) if lines else "（尚無角色核心狀態）"


def _build_story_system_prompt(
    story_id: str,
    state_text: str,
    branch_id: str = "main",
    narrative_recap: str = "",
    npcs: list[dict] | None = None,
    state_dict: dict | None = None,
) -> str:
    """Read the story's system_prompt.txt and fill in placeholders."""
    from app import _build_lore_text

    tree = _load_tree(story_id)
    branch = tree.get("branches", {}).get(branch_id, {})
    if branch.get("blank"):
        narrative_recap = ""

    if not narrative_recap:
        narrative_recap = "（尚無回顧，完整對話記錄已提供。）"

    prompt_path = _story_system_prompt_path(story_id)
    lore_text = _build_lore_text(story_id, branch_id)
    if npcs is None:
        npcs = _load_npcs(story_id, branch_id)
    npc_text = _build_npc_summary_text(story_id, branch_id, npcs=npcs)
    if state_dict is None:
        try:
            state_dict = json.loads(state_text)
        except (json.JSONDecodeError, TypeError):
            state_dict = {}
    critical_facts = _build_critical_facts(story_id, branch_id, state_dict, npcs)
    branch_config = _load_branch_config(story_id, branch_id)
    team_mode = branch_config.get("team_mode", "free_agent")
    team_rules = _TEAM_RULES.get(team_mode, _TEAM_RULES["free_agent"])
    image_gen_enabled = _is_image_gen_enabled(branch_config)
    image_model = _get_image_model(branch_config)
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
        if critical_facts and "關鍵事實" not in result:
            marker = "## 當前角色狀態"
            idx = result.find(marker)
            facts_section = f"## ⚠️ 關鍵事實（絕對不可搞混）\n{critical_facts}\n\n"
            if idx >= 0:
                result = result[:idx] + facts_section + result[idx:]
            else:
                result = facts_section + result
    else:
        result = build_system_prompt(
            state_text,
            critical_facts=critical_facts,
            dungeon_context=dungeon_context,
        )

    story_dir = _story_dir(story_id)
    if not get_fate_mode(story_dir, branch_id):
        result = re.sub(
            r"## ⚠️ 命運走向系統.*?(?=## |\Z)",
            "",
            result,
            flags=re.DOTALL,
        ).strip() + "\n"

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

    if image_gen_enabled:
        result += (
            "\n\n## 場景插圖設定（系統）\n"
            "- 本分支已啟用場景插圖。每次 GM 回覆都必須輸出且只輸出一個 IMG tag。\n"
            "- 即使是過場、情報或對話回合，也要為當前最具代表性的場景補上一張插圖。\n"
            "- IMG prompt 必須使用英文，聚焦當前回合最終畫面或最具代表性的視覺瞬間。\n"
            f"- 當前圖片模型：`{image_model}`。\n"
        )
    else:
        result += (
            "\n\n## 場景插圖設定（系統）\n"
            "- 本分支已關閉場景插圖。\n"
            "- 禁止輸出任何 IMG tag。\n"
        )
    return result


__all__ = [
    "_load_nsfw_preferences",
    "_format_nsfw_preferences",
    "_load_character_schema",
    "_blank_character_state",
    "_load_character_state",
    "_TEAM_RULES",
    "STORY_ANCHOR_LIMIT",
    "_normalize_story_anchors",
    "_build_critical_facts",
    "_format_state_core_value",
    "_build_core_state_text",
    "_build_story_system_prompt",
]
