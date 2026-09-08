"""Card schema validation, loader, and initialization."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re

from story_core.gm_v2 import relationship as _rel
from story_core.story_io import (
    BASE_DIR,
    DATA_DIR,
    _load_json,
    _load_stories_registry,
    _save_json,
    _save_stories_registry,
    _story_dir,
    _branch_dir,
    _story_tree_path,
    _story_messages_path,
    _story_npcs_path,
    _story_character_state_path,
)

log = logging.getLogger("rpg")

STORY_DESIGN_DIR = os.path.join(BASE_DIR, "story_design")
_SLUG_RE = re.compile(r"^[a-z0-9_]+$")


def validate_card(card: dict) -> list[str]:
    """Validate a card.json structure according to Card Mode specification.

    Returns a list of error strings. Empty list means the card is valid.
    """
    errors: list[str] = []
    if not isinstance(card, dict):
        return ["Card data must be a JSON object."]

    if card.get("schema_version") != 1:
        errors.append("`schema_version` must be 1.")

    card_id = str(card.get("id") or "").strip()
    if not card_id:
        errors.append("`id` is required.")
    elif not _SLUG_RE.match(card_id):
        errors.append("`id` must contain only lowercase alphanumeric characters and underscores.")

    if not str(card.get("display_name") or "").strip():
        errors.append("`display_name` must be a non-empty string.")

    if not str(card.get("pitch") or "").strip():
        errors.append("`pitch` must be a non-empty string.")

    if not str(card.get("opening") or "").strip():
        errors.append("`opening` must be a non-empty string.")

    persona = card.get("persona")
    if not isinstance(persona, dict):
        errors.append("`persona` must be an object.")
    else:
        if not str(persona.get("personality") or "").strip():
            errors.append("`persona.personality` must be a non-empty string.")
        if not str(persona.get("speech_style") or "").strip():
            errors.append("`persona.speech_style` must be a non-empty string.")

        verbal_tics = persona.get("verbal_tics")
        if not isinstance(verbal_tics, list) or not [t for t in verbal_tics if str(t).strip()]:
            errors.append("`persona.verbal_tics` must contain at least 1 non-empty string.")

        body_details = persona.get("body_details")
        if not isinstance(body_details, list) or not [b for b in body_details if str(b).strip()]:
            errors.append("`persona.body_details` must contain at least 1 non-empty string.")

        fetishes = persona.get("fetishes")
        if fetishes is not None and (not isinstance(fetishes, list) or not all(isinstance(f, str) for f in fetishes)):
            errors.append("`persona.fetishes` must be a list of strings if provided.")

        sensitive_spots = persona.get("sensitive_spots")
        if sensitive_spots is not None and (not isinstance(sensitive_spots, list) or not all(isinstance(s, str) for s in sensitive_spots)):
            errors.append("`persona.sensitive_spots` must be a list of strings if provided.")

    start = card.get("start")
    if not isinstance(start, dict):
        errors.append("`start` must be an object.")
    else:
        rel = start.get("relationship")
        if not isinstance(rel, dict):
            errors.append("`start.relationship` must be an object.")
        else:
            tier = str(rel.get("tier") or "").strip()
            if not _rel.is_valid_tier(tier):
                errors.append(f"`start.relationship.tier` must be one of: {', '.join(_rel.TIERS)} (got: {tier!r}).")

    return errors


def card_path(story_id: str, design_dir: str | None = None) -> str:
    """Return the absolute path to a story's card.json."""
    root = design_dir or STORY_DESIGN_DIR
    return os.path.join(root, story_id, "card.json")


def load_card(story_id: str, design_dir: str | None = None) -> dict:
    """Load and validate card.json for story_id. Raises ValueError on failure."""
    path = card_path(story_id, design_dir=design_dir)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Card not found at: {path}")

    card = _load_json(path, {})
    errors = validate_card(card)
    if errors:
        raise ValueError(f"Card validation failed for {story_id}: {'; '.join(errors)}")

    return card


def init_card_story(
    card: dict,
    stories_dir: str | None = None,
    design_dir: str | None = None,
    overwrite: bool = False,
) -> None:
    """Scaffold runtime data for a card mode story and register it in stories.json.

    Creates:
    - story_design/<id>/card.json (if not already present or if saving)
    - data/stories/<id>/timeline_tree.json (single 'main' branch)
    - data/stories/<id>/branches/main/messages.json (message index 0: opening with snapshots)
    - data/stories/<id>/branches/main/npcs.json (single card character record)
    - data/stories/<id>/branches/main/character_state.json (inventory + relationship_note)
    - data/stories/<id>/branches/main/card_clock.json (start_iso + elapsed_minutes + weather)
    - data/stories.json registry entry with mode="card"
    """
    errors = validate_card(card)
    if errors:
        raise ValueError(f"Cannot initialize invalid card: {'; '.join(errors)}")

    story_id = card["id"]
    display_name = card["display_name"]

    # 1. Ensure card.json in design dir
    design_path = card_path(story_id, design_dir=design_dir)
    os.makedirs(os.path.dirname(design_path), exist_ok=True)
    if not os.path.exists(design_path) or overwrite:
        _save_json(design_path, card)

    # 2. Runtime directories
    s_dir = _story_dir(story_id) if not stories_dir else os.path.join(stories_dir, story_id)
    b_dir = _branch_dir(story_id, "main") if not stories_dir else os.path.join(s_dir, "branches", "main")
    os.makedirs(b_dir, exist_ok=True)

    now_iso = datetime.now(timezone.utc).isoformat()
    start_dt_str = card.get("setting", {}).get("start_datetime", "2026-05-16T02:08")
    weather_pool = card.get("setting", {}).get("weather_pool") or ["陰"]
    initial_weather = weather_pool[0] if weather_pool else "陰"
    start_rel = card.get("start", {}).get("relationship", {"tier": "陌生", "note": ""})
    start_inv = card.get("start", {}).get("inventory", {})

    # 3. timeline_tree.json
    tree_path = _story_tree_path(story_id) if not stories_dir else os.path.join(s_dir, "timeline_tree.json")
    tree = {
        "active_branch_id": "main",
        "root_branch_id": "main",
        "branches": {
            "main": {
                "id": "main",
                "name": "主線",
                "parent_branch_id": None,
                "branch_point_index": -1,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        },
    }
    _save_json(tree_path, tree)

    # 4. card_clock.json
    clock_path = os.path.join(b_dir, "card_clock.json")
    clock_data = {
        "start_iso": start_dt_str,
        "elapsed_minutes": 0,
        "last_weather": initial_weather,
    }
    _save_json(clock_path, clock_data)

    # 5. character_state.json (player side)
    char_state_path = _story_character_state_path(story_id, "main") if not stories_dir else os.path.join(b_dir, "character_state.json")
    char_state = {
        "inventory": dict(start_inv),
        "relationship_note": str(start_rel.get("note", "")),
    }
    _save_json(char_state_path, char_state)

    # 6. npcs.json (only the dynamic fields: memory, status, relationship)
    npcs_path = _story_npcs_path(story_id, "main") if not stories_dir else os.path.join(b_dir, "npcs.json")
    card_npc = {
        "name": display_name,
        "role": card.get("persona", {}).get("identity", ""),
        "lifecycle_status": "active",
        "memory": [],
        "current_status": "",
        "relationship": {
            "tier": start_rel.get("tier", "陌生"),
            "note": start_rel.get("note", ""),
        },
    }
    _save_json(npcs_path, [card_npc])

    # 7. messages.json (initial opening GM message)
    msg_path = _story_messages_path(story_id, "main") if not stories_dir else os.path.join(b_dir, "messages.json")
    opening_msg = {
        "role": "gm",
        "content": card["opening"],
        "index": 0,
        "state_snapshot": char_state,
        "npcs_snapshot": [card_npc],
        "clock_snapshot": clock_data,
    }
    _save_json(msg_path, [opening_msg])

    # 8. Register in data/stories.json
    registry = _load_stories_registry()
    stories = registry.get("stories", {})
    stories[story_id] = {
        "id": story_id,
        "name": display_name,
        "description": card.get("pitch", ""),
        "mode": "card",
        "tagline": card.get("tagline", ""),
        "pitch": card.get("pitch", ""),
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    registry["stories"] = stories
    _save_stories_registry(registry)
    log.info("card_mode: initialized story %s (card: %s)", story_id, display_name)
