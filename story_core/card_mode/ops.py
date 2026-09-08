"""Card mode tool definitions, gate validators, and apply executor."""

from __future__ import annotations

import logging
import os

from story_core.card_mode import clock
from story_core.gm_v2 import relationship as _rel
from story_core.gm_v2.gate import _append_recover_name, _is_junk_map_key
from story_core.story_io import (
    _branch_dir,
    _load_json,
    _save_json,
    _story_character_state_path,
    _story_npcs_path,
)

log = logging.getLogger("rpg")

MAX_FACT_LENGTH = 200
MAX_TIME_ADVANCE_MINUTES = 720  # 12 hours


def card_tool_declarations() -> list[dict]:
    """Gemini tool declarations for the 4 card mode tools."""
    return [
        {
            "name": "set_relationship",
            "description": "更新角色對玩家的關係階梯與現況備註。tier 必須在關係階梯中，單回合至多提升 1 階。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tier": {
                        "type": "string",
                        "enum": _rel.TIERS,
                        "description": f"關係階梯之一（{' < '.join(_rel.TIERS)}）",
                    },
                    "note": {
                        "type": "string",
                        "description": "關係變化的具體緣由或現況備註（一句話）",
                    },
                },
                "required": ["tier"],
            },
        },
        {
            "name": "remember",
            "description": "記下角色得知的重要新事實、承諾、約定或長期記憶（非流水帳、非暫態情緒）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "facts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要長期記住的事實字串清單",
                    }
                },
                "required": ["facts"],
            },
        },
        {
            "name": "advance_time",
            "description": "推進劇情時間（以分鐘為單位）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "integer",
                        "description": "流逝的分鐘數（1 至 720 分鐘）",
                    }
                },
                "required": ["minutes"],
            },
        },
        {
            "name": "set_inventory",
            "description": "更新玩家身上的隨身物狀態。鍵為物品名稱，值為狀態說明（值設為空字串 '' 代表移除或耗盡）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "object",
                        "description": "物品名稱映射到狀態；空字串代表移除",
                    }
                },
                "required": ["items"],
            },
        },
    ]


# ---------------------------------------------------------------------------
# Gate Validators
# ---------------------------------------------------------------------------

def gate_relationship(tier: str, note: str, current_tier: str) -> tuple[dict | None, str | None]:
    """Validate relationship change. Enforces tier validity and +1 max positive step."""
    clean_tier = str(tier or "").strip()
    clean_note = str(note or "").strip()

    if not _rel.is_valid_tier(clean_tier):
        return None, f"Invalid tier {clean_tier!r}; valid tiers: {', '.join(_rel.TIERS)}"

    old_idx = _rel.tier_index(current_tier)
    new_idx = _rel.tier_index(clean_tier)

    # If old tier was somehow invalid, treat as DEFAULT_TIER ("陌生")
    if old_idx == -1:
        old_idx = _rel.tier_index(_rel.DEFAULT_TIER)

    # Monotonicity discipline: at most +1 tier step per turn (negative jumps allowed)
    if new_idx > old_idx + 1:
        clamped_tier = _rel.TIERS[old_idx + 1]
        log.warning(
            "gate_relationship: clamped tier jump from %s to %s (requested: %s)",
            current_tier, clamped_tier, clean_tier
        )
        clean_tier = clamped_tier

    return {"tier": clean_tier, "note": clean_note}, None


def gate_remember(raw_facts: object, existing_memories: list[str]) -> tuple[list[str], list[str]]:
    """Validate memory facts. Caps length, drops blanks, and deduplicates."""
    if isinstance(raw_facts, str):
        raw_facts = [raw_facts]
    elif not isinstance(raw_facts, list):
        return [], ["facts must be a list of strings"]

    clean: list[str] = []
    dropped: list[str] = []
    existing_set = {str(m).strip() for m in existing_memories if str(m).strip()}

    for item in raw_facts:
        if not isinstance(item, str):
            dropped.append(f"non-string item: {item!r}")
            continue
        trimmed = item.strip()[:MAX_FACT_LENGTH]
        if not trimmed:
            dropped.append("empty fact string")
            continue
        if trimmed in existing_set or trimmed in clean:
            continue
        clean.append(trimmed)

    return clean, dropped


def gate_advance_time(raw_minutes: object) -> tuple[int | None, str | None]:
    """Validate minute advance."""
    try:
        minutes = int(raw_minutes)  # type: ignore
    except (ValueError, TypeError):
        return None, f"minutes must be an integer, got: {raw_minutes!r}"

    if minutes <= 0:
        return None, "minutes must be positive"
    if minutes > MAX_TIME_ADVANCE_MINUTES:
        log.warning("gate_advance_time: capping %d minutes to %d", minutes, MAX_TIME_ADVANCE_MINUTES)
        minutes = MAX_TIME_ADVANCE_MINUTES

    return minutes, None


def gate_inventory(raw_items: object) -> tuple[dict[str, str], list[str]]:
    """Clean inventory changes using junk map key filters."""
    if not isinstance(raw_items, dict):
        return {}, ["items must be a dict"]

    clean: dict[str, str] = {}
    dropped: list[str] = []

    for k, v in raw_items.items():
        if _is_junk_map_key(k):
            recovered = _append_recover_name(k, v)
            if recovered:
                clean[recovered] = ""
            else:
                dropped.append(f"junk key {k!r}")
            continue

        item_name = str(k).strip().strip("\"'`“”‘’´ ")
        if not item_name:
            dropped.append("empty item name")
            continue

        val_str = str(v).strip() if v is not None else ""
        clean[item_name] = val_str

    return clean, dropped


# ---------------------------------------------------------------------------
# Tool Executor
# ---------------------------------------------------------------------------

def execute_card_tool(story_id: str, branch_id: str, tool_name: str, args: dict) -> dict:
    """Execute a single card tool call deterministically with state persistence."""
    npcs_path = _story_npcs_path(story_id, branch_id)
    npcs = _load_json(npcs_path, [])
    card_npc = npcs[0] if npcs else {"name": story_id, "memory": [], "relationship": {}}

    state_path = _story_character_state_path(story_id, branch_id)
    state = _load_json(state_path, {"inventory": {}})

    if tool_name == "set_relationship":
        cur_tier = card_npc.get("relationship", {}).get("tier", "陌生")
        clean_rel, err = gate_relationship(args.get("tier"), args.get("note", ""), cur_tier)
        if err or not clean_rel:
            return {"ok": False, "error": err or "validation failed"}

        card_npc["relationship"] = clean_rel
        state["relationship_note"] = clean_rel["note"]
        _save_json(npcs_path, npcs)
        _save_json(state_path, state)
        log.info("execute_card_tool: set_relationship for %s to %s", story_id, clean_rel)
        return {"ok": True, "applied": clean_rel}

    elif tool_name == "remember":
        clean_facts, dropped = gate_remember(args.get("facts"), card_npc.get("memory", []))
        if clean_facts:
            card_npc.setdefault("memory", []).extend(clean_facts)
            _save_json(npcs_path, npcs)
            log.info("execute_card_tool: remembered %d facts for %s", len(clean_facts), story_id)
        return {"ok": True, "applied": clean_facts, "dropped": dropped}

    elif tool_name == "advance_time":
        minutes, err = gate_advance_time(args.get("minutes"))
        if err or minutes is None:
            return {"ok": False, "error": err or "validation failed"}

        clock.advance(story_id, branch_id, minutes)
        return {"ok": True, "applied": {"minutes": minutes}}

    elif tool_name == "set_inventory":
        raw_items = args.get("items") if isinstance(args.get("items"), dict) else args
        clean_inv, dropped = gate_inventory(raw_items)
        current_inv = state.setdefault("inventory", {})
        for name, status in clean_inv.items():
            if status == "":
                current_inv.pop(name, None)
            else:
                current_inv[name] = status
        _save_json(state_path, state)
        log.info("execute_card_tool: set_inventory for %s: %s", story_id, clean_inv)
        return {"ok": True, "applied": clean_inv, "dropped": dropped}

    return {"ok": False, "error": f"Unknown tool: {tool_name}"}
