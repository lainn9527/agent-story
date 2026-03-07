"""Character state update helpers extracted from app_helpers."""

import copy
import logging

from story_core.character_state import _load_character_schema, _load_character_state
from story_core.dungeon_system import reconcile_dungeon_entry, reconcile_dungeon_exit, validate_dungeon_progression
from story_core.event_db import get_active_events, insert_event, update_event_status
from story_core.npc_helpers import _sync_state_db_from_state
from story_core.story_io import _save_json, _story_character_state_path
from story_core.tag_extraction import _NORMALIZE_DASHES_RE, _NORMALIZE_DOTS_RE, _extract_item_base_name

log = logging.getLogger("rpg")


def _is_numeric_value(value: object) -> bool:
    """True for int/float but not bool (bool is a subclass of int in Python)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_SCENE_KEYS = {
    "location", "location_update", "location_details",
    "threat_level", "combat_status", "escape_options", "escape_route",
    "noise_level", "facility_status", "npc_status", "weapons_status",
    "tool_status", "available_escape", "available_locations",
    "status_update", "current_predicament",
}

_INSTRUCTION_KEYS = {
    "inventory_use", "inventory_update", "skill_update",
    "status_change", "state_change", "note", "notes",
}


def _get_schema_known_keys(schema: dict) -> set[str]:
    """Extract all known field keys from character schema."""
    known = set()
    for field in schema.get("fields", []):
        known.add(field["key"])
    for list_def in schema.get("lists", []):
        known.add(list_def["key"])
        if list_def.get("state_add_key"):
            known.add(list_def["state_add_key"])
        if list_def.get("state_remove_key"):
            known.add(list_def["state_remove_key"])
    for key in schema.get("direct_overwrite_keys", []):
        known.add(key)
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
    status = raw_status.strip().lower()
    if not status:
        return None
    status = status.replace("-", "_").replace(" ", "")
    for canonical, aliases in _EVENT_STATUS_ALIASES.items():
        if status == canonical or status in aliases:
            return canonical
    return None


def _build_active_events_hint(story_id: str, branch_id: str, limit: int = 40) -> str:
    rows = get_active_events(story_id, branch_id, limit=limit)
    if not rows:
        return "（無）"
    lines = []
    for row in rows:
        event_id = row.get("id")
        title = str(row.get("title", "")).strip()
        status = _normalize_event_status(row.get("status")) or str(row.get("status", "")).strip()
        if not title or event_id is None:
            continue
        lines.append(f"#{event_id} [{status}] {title}")
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
        event_id = meta.get("id")
        if isinstance(event_id, int):
            id_map[event_id] = {"title": title, "status": meta.get("status", "")}

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
    text = value.strip()
    if not text:
        return
    if text not in target:
        target.append(text)


def _state_ops_to_update(state_ops: dict, schema: dict, current_state: dict | None = None) -> dict:
    """Translate state_ops contract into existing canonical state update shape."""
    if not isinstance(state_ops, dict):
        return {}
    current_state = current_state or {}
    update: dict = {}

    list_defs = {item["key"]: item for item in schema.get("lists", []) if isinstance(item, dict) and item.get("key")}
    map_keys = {key for key, item in list_defs.items() if item.get("type") == "map"}
    map_keys.update({field["key"] for field in schema.get("fields", []) if field.get("type") == "map"})
    list_keys = {key for key, item in list_defs.items() if item.get("type", "list") != "map"}
    direct_overwrite = set(schema.get("direct_overwrite_keys", []))
    known_keys = _get_schema_known_keys(schema)

    set_ops = state_ops.get("set")
    if isinstance(set_ops, dict):
        for key, value in set_ops.items():
            if key in map_keys:
                log.warning("state_ops: reject set.%s (map key); use map_upsert/map_remove", key)
                continue
            if key in list_keys:
                continue
            if value is None:
                continue
            if key == "reward_points":
                log.warning("state_ops: reject set.reward_points; use delta.reward_points")
                continue
            if key in direct_overwrite or key in known_keys:
                update[key] = value

    delta_ops = state_ops.get("delta")
    if isinstance(delta_ops, dict):
        for key, value in delta_ops.items():
            if not _is_numeric_value(value):
                continue
            if key == "reward_points":
                update["reward_points_delta"] = update.get("reward_points_delta", 0) + value
                continue
            if key in known_keys:
                delta_key = f"{key}_delta"
                update[delta_key] = update.get(delta_key, 0) + value

    map_upsert = state_ops.get("map_upsert")
    if isinstance(map_upsert, dict):
        for map_key, kv in map_upsert.items():
            if map_key not in map_keys or not isinstance(kv, dict):
                continue
            bucket = update.setdefault(map_key, {})
            if not isinstance(bucket, dict):
                bucket = {}
                update[map_key] = bucket
            for raw_key, raw_value in kv.items():
                if raw_key is None:
                    continue
                key = str(raw_key).strip()
                if not key:
                    continue
                bucket[key] = raw_value

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
    value = key.replace(" ", "").replace("\u3000", "")
    value = value.replace("（", "(").replace("）", ")")
    result = []
    for ch in value:
        codepoint = ord(ch)
        if 0xFF01 <= codepoint <= 0xFF5E:
            result.append(chr(codepoint - 0xFEE0))
        else:
            result.append(ch)
    value = "".join(result)
    value = _NORMALIZE_DOTS_RE.sub("·", value)
    value = _NORMALIZE_DASHES_RE.sub("—", value)
    return value


def _resolve_map_keys(update_map: dict, existing_map: dict) -> dict:
    """Rewrite update keys to match existing keys via fuzzy normalization."""
    if not existing_map or not update_map:
        return update_map
    norm_to_existing = {_normalize_map_key(existing_key): existing_key for existing_key in existing_map}
    resolved = {}
    for update_key, update_value in update_map.items():
        norm_key = _normalize_map_key(update_key)
        if norm_key in norm_to_existing and update_key != norm_to_existing[norm_key]:
            resolved[norm_to_existing[norm_key]] = update_value
        else:
            resolved[update_key] = update_value
    return resolved


def _dedup_inventory_plain_vs_variant(inv_map: dict) -> dict:
    """Drop plain-name keys when a variant with the same base name exists."""
    if not isinstance(inv_map, dict) or len(inv_map) < 2:
        return inv_map

    groups: dict[str, list[str]] = {}
    for key in inv_map:
        base = _extract_item_base_name(key)
        norm_base = _normalize_map_key(base)
        groups.setdefault(norm_base, []).append(key)

    remove_keys: set[str] = set()
    for keys in groups.values():
        if len(keys) < 2:
            continue
        plain_keys = [key for key in keys if key.strip() == _extract_item_base_name(key)]
        variant_keys = [key for key in keys if key not in plain_keys]
        if plain_keys and variant_keys:
            remove_keys.update(plain_keys)

    if not remove_keys:
        return inv_map
    return {key: value for key, value in inv_map.items() if key not in remove_keys}


def _migrate_list_to_map(items: list) -> dict:
    """Convert a list of item strings to a map (key→value)."""
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
    """Parse a list-format inventory item string into map-format key/value."""
    if " — " in item:
        parts = item.split(" — ", 1)
        return parts[0].strip(), parts[1].strip()

    base = _extract_item_base_name(item)
    remainder = item[len(base):].strip()

    if remainder.startswith("（") and remainder.endswith("）"):
        remainder = remainder[1:-1]
    elif remainder.startswith("(") and remainder.endswith(")"):
        remainder = remainder[1:-1]

    return base, remainder


def _apply_state_update_inner(story_id: str, branch_id: str, update: dict, schema: dict):
    """Core logic: apply a STATE update dict to character state. No normalization."""
    state = _load_character_state(story_id, branch_id)

    for list_def in schema.get("lists", []):
        if list_def.get("type") != "map":
            continue
        list_key = list_def["key"]
        add_key = list_def.get("state_add_key") or f"{list_key}_add"
        remove_key = list_def.get("state_remove_key") or f"{list_key}_remove"
        if add_key in update or remove_key in update:
            inv_map = update.setdefault(list_key, {})
            if not isinstance(inv_map, dict):
                inv_map = {}
                update[list_key] = inv_map
            if remove_key in update:
                remove_value = update.pop(remove_key)
                if isinstance(remove_value, str):
                    remove_value = [remove_value]
                if isinstance(remove_value, list):
                    for item in remove_value:
                        if isinstance(item, str):
                            inv_map[_extract_item_base_name(item)] = None
            if add_key in update:
                add_value = update.pop(add_key)
                if isinstance(add_value, str):
                    add_value = [add_value]
                if isinstance(add_value, list):
                    for item in add_value:
                        if isinstance(item, str):
                            base, status = _parse_item_to_kv(item)
                            inv_map[base] = status
            if inv_map:
                existing = state.get(list_key, {})
                update[list_key] = _resolve_map_keys(inv_map, existing)

    for field_def in schema.get("fields", []):
        if field_def.get("type") != "map":
            continue
        key = field_def["key"]
        if key in update and isinstance(update[key], dict):
            existing = state.get(key, {})
            update[key] = _resolve_map_keys(update[key], existing)
            for item_key, item_value in update[key].items():
                if item_value is None:
                    if item_key in existing:
                        existing.pop(item_key)
                    else:
                        base = _extract_item_base_name(item_key)
                        for existing_key in list(existing):
                            if _extract_item_base_name(existing_key) == base:
                                existing.pop(existing_key)
                                break
                else:
                    existing[item_key] = item_value
            state[key] = existing

    for list_def in schema.get("lists", []):
        key = list_def["key"]
        list_type = list_def.get("type", "list")

        if list_type == "map":
            if key in update and isinstance(update[key], dict):
                existing = state.get(key, {})
                update[key] = _resolve_map_keys(update[key], existing)
                for item_key, item_value in update[key].items():
                    if item_value is None:
                        if item_key in existing:
                            existing.pop(item_key)
                        else:
                            base = _extract_item_base_name(item_key)
                            for existing_key in list(existing):
                                if _extract_item_base_name(existing_key) == base:
                                    existing.pop(existing_key)
                                    break
                    else:
                        existing[item_key] = item_value
                state[key] = existing
        else:
            remove_key = list_def.get("state_remove_key")
            if remove_key and remove_key in update:
                lst = state.get(key, [])
                remove_value = update[remove_key]
                if isinstance(remove_value, str):
                    remove_value = [remove_value]
                elif not isinstance(remove_value, list):
                    remove_value = []
                for remove_item in remove_value:
                    if not isinstance(remove_item, str):
                        continue
                    if remove_item in lst:
                        lst = [item for item in lst if item != remove_item]
                    else:
                        remove_base = _extract_item_base_name(remove_item)
                        lst = [item for item in lst if _extract_item_base_name(item) != remove_base]
                state[key] = lst

            add_key = list_def.get("state_add_key")
            if add_key and add_key in update:
                lst = state.get(key, [])
                add_value = update[add_key]
                if isinstance(add_value, str):
                    add_value = [add_value]
                elif not isinstance(add_value, list):
                    add_value = []
                for item in add_value:
                    if not isinstance(item, str):
                        continue
                    if item in lst:
                        continue
                    add_base = _extract_item_base_name(item)
                    if add_base:
                        lst = [
                            existing_item for existing_item in lst
                            if not (
                                _extract_item_base_name(existing_item) == add_base
                                and existing_item.strip() == add_base
                            )
                        ]
                    lst.append(item)
                state[key] = lst

    for key in list(update.keys()):
        if key.endswith("_delta") and _is_numeric_value(update[key]):
            base_key = key[:-6]
            current = state.get(base_key)
            if _is_numeric_value(current):
                state[base_key] = current + update[key]
            elif base_key == "reward_points":
                state[base_key] = state.get(base_key, 0) + update[key]

    if "reward_points" in update and "reward_points_delta" not in update:
        value = update["reward_points"]
        if _is_numeric_value(value):
            state["reward_points"] = int(value)

    for key in schema.get("direct_overwrite_keys", []):
        if key in update:
            state[key] = update[key]

    if isinstance(state.get("inventory"), dict):
        state["inventory"] = _dedup_inventory_plain_vs_variant(state["inventory"])

    handled_keys = {"reward_points"}
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
    for key in update:
        if key.endswith("_delta"):
            handled_keys.add(key)

    system_keys = {"world_day", "world_time", "branch_title"}
    for key, value in update.items():
        if key in system_keys or key in _SCENE_KEYS or key in _INSTRUCTION_KEYS:
            continue
        if (key.endswith("_add") or key.endswith("_remove")) and key not in handled_keys:
            continue
        if key not in handled_keys and isinstance(value, (str, int, float, bool)):
            state[key] = value

    _save_json(_story_character_state_path(story_id, branch_id), state)
    _sync_state_db_from_state(story_id, branch_id, state)


def _apply_state_update(story_id: str, branch_id: str, update: dict):
    """Apply a STATE update dict to the branch's character state file."""
    schema = _load_character_schema(story_id)
    current_state = _load_character_state(story_id, branch_id)
    old_state = copy.deepcopy(current_state)

    from story_core.gm_pipeline import _normalize_state_async, _run_state_gate

    update = _run_state_gate(
        update, schema, current_state,
        label="state_gate", story_id=story_id, branch_id=branch_id,
    )

    _apply_state_update_inner(story_id, branch_id, update, schema)

    new_state = _load_character_state(story_id, branch_id)
    reconcile_dungeon_entry(story_id, branch_id, old_state, new_state)
    validate_dungeon_progression(story_id, branch_id, new_state, old_state)
    reconcile_dungeon_exit(story_id, branch_id, old_state, new_state)

    _save_json(_story_character_state_path(story_id, branch_id), new_state)
    _sync_state_db_from_state(story_id, branch_id, new_state)

    _normalize_state_async(story_id, branch_id, update, _get_schema_known_keys(schema))


__all__ = [
    "_is_numeric_value",
    "_SCENE_KEYS",
    "_INSTRUCTION_KEYS",
    "_get_schema_known_keys",
    "_EVENT_STATUS_ORDER",
    "_EVENT_STATUS_ALIASES",
    "_normalize_event_status",
    "_build_active_events_hint",
    "_apply_event_ops",
    "_append_unique_str",
    "_state_ops_to_update",
    "_normalize_map_key",
    "_resolve_map_keys",
    "_dedup_inventory_plain_vs_variant",
    "_migrate_list_to_map",
    "_parse_item_to_kv",
    "_apply_state_update_inner",
    "_apply_state_update",
]
