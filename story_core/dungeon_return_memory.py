"""Helpers for recalling archived dungeon-local NPCs on dungeon re-entry."""

from __future__ import annotations

import copy
import logging
import os
import re
import unicodedata

from story_core.branch_tree import get_full_timeline
from story_core.story_io import (
    _branch_dir,
    _dungeon_return_memory_path,
    _load_json,
    _save_json,
    _story_character_state_path,
    _story_dir,
    _story_npcs_path,
)

log = logging.getLogger("rpg")

DUNGEON_RETURN_NPC_LIMIT = 4
DUNGEON_RETURN_CHAR_LIMIT = 350

NPC_HOME_SCOPE_DUNGEON_LOCAL = "dungeon_local"
NPC_HOME_SCOPE_MAIN_GOD_SPACE = "main_god_space"
NPC_HOME_SCOPE_CROSS_DUNGEON = "cross_dungeon"
NPC_HOME_SCOPES = {
    NPC_HOME_SCOPE_DUNGEON_LOCAL,
    NPC_HOME_SCOPE_MAIN_GOD_SPACE,
    NPC_HOME_SCOPE_CROSS_DUNGEON,
}

NPC_RETURN_RECALL_ELIGIBLE = "eligible"
NPC_RETURN_RECALL_EXCLUDED_CARRIED_OUT = "excluded_carried_out"
NPC_RETURN_RECALL_EXCLUDED_CONVERTED = "excluded_converted"
NPC_RETURN_RECALL_EXCLUDED_TERMINAL = "excluded_terminal"
NPC_RETURN_RECALL_UNKNOWN = "unknown"
NPC_RETURN_RECALL_STATES = {
    NPC_RETURN_RECALL_ELIGIBLE,
    NPC_RETURN_RECALL_EXCLUDED_CARRIED_OUT,
    NPC_RETURN_RECALL_EXCLUDED_CONVERTED,
    NPC_RETURN_RECALL_EXCLUDED_TERMINAL,
    NPC_RETURN_RECALL_UNKNOWN,
}

NPC_ARCHIVE_KIND_OFFSTAGE = "offstage"
NPC_ARCHIVE_KIND_TERMINAL = "terminal"

_CROSS_DUNGEON_KEYWORDS = (
    "隊友",
    "同伴",
    "戰友",
    "伴侶",
    "隨行",
    "核心隊友",
)
_CONVERTED_KEYWORDS = (
    "召喚錨點",
    "召喚物",
    "素材",
    "封印",
    "咒靈玉",
    "殘響",
    "法相",
    "武裝",
)
_TERMINAL_KEYWORDS = (
    "已損毀",
    "威脅解除",
    "已失效",
    "已封印",
    "已消散",
    "死亡",
    "徹底死亡",
    "徹底擊殺",
)
_DUNGEON_ALIAS_REPLACEMENTS = {
    "迴": "回",
    "臺": "台",
}
_DUNGEON_NAME_PUNCT_RE = re.compile(r"[\s\u3000\-_—–−·•・,，。:：;；'\"“”‘’`()（）\[\]【】{}<>《》〈〉/\\|]+")


def _normalize_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_dungeon_name_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", _normalize_text(value))
    if not text:
        return ""
    for old, new in _DUNGEON_ALIAS_REPLACEMENTS.items():
        text = text.replace(old, new)
    text = _DUNGEON_NAME_PUNCT_RE.sub("", text)
    return text.casefold()


def _load_dungeon_templates(story_id: str) -> list[dict]:
    path = os.path.join(_story_dir(story_id), "dungeons_template.json")
    payload = _load_json(path, {"dungeons": []})
    dungeons = payload.get("dungeons", []) if isinstance(payload, dict) else []
    return [d for d in dungeons if isinstance(d, dict)]


def canonicalize_dungeon_name(story_id: str, dungeon_name: object) -> str:
    """Return the exact template display name when a unique conservative match exists."""
    raw = _normalize_text(dungeon_name)
    if not raw:
        return ""
    exact = {}
    normalized = {}
    for dungeon in _load_dungeon_templates(story_id):
        name = _normalize_text(dungeon.get("name"))
        if not name:
            continue
        exact[name] = name
        normalized.setdefault(_normalize_dungeon_name_key(name), set()).add(name)
    if raw in exact:
        return raw
    key = _normalize_dungeon_name_key(raw)
    matches = sorted(normalized.get(key, set()))
    if len(matches) == 1:
        return matches[0]
    return raw


def normalize_npc_home_scope(value: object) -> str | None:
    text = _normalize_text(value)
    return text if text in NPC_HOME_SCOPES else None


def normalize_npc_return_recall_state(value: object) -> str:
    text = _normalize_text(value)
    return text if text in NPC_RETURN_RECALL_STATES else NPC_RETURN_RECALL_UNKNOWN


def normalize_npc_archive_kind(value: object) -> str | None:
    text = _normalize_text(value)
    if text in {NPC_ARCHIVE_KIND_OFFSTAGE, NPC_ARCHIVE_KIND_TERMINAL}:
        return text
    return None


def normalize_npc_lifecycle_status(value: object) -> str:
    text = _normalize_text(value).lower()
    return "archived" if text == "archived" else "active"


def _normalize_memory(data: object) -> dict:
    if not isinstance(data, dict):
        data = {}
    visited = []
    seen = set()
    for raw in data.get("visited_dungeons", []) if isinstance(data.get("visited_dungeons"), list) else []:
        name = _normalize_text(raw)
        if not name or name in seen:
            continue
        visited.append(name)
        seen.add(name)
    pending = _normalize_text(data.get("pending_reentry_dungeon")) or None
    if pending and pending not in seen:
        pending = None
    return {
        "visited_dungeons": visited,
        "pending_reentry_dungeon": pending,
    }


def load_dungeon_return_memory(story_id: str, branch_id: str) -> dict:
    path = _dungeon_return_memory_path(story_id, branch_id)
    normalized = _normalize_memory(_load_json(path, {}))
    if not os.path.exists(path):
        _save_json(path, normalized)
    return normalized


def save_dungeon_return_memory(story_id: str, branch_id: str, memory: dict) -> dict:
    normalized = _normalize_memory(memory)
    _save_json(_dungeon_return_memory_path(story_id, branch_id), normalized)
    return normalized


def init_dungeon_return_memory(story_id: str, branch_id: str) -> dict:
    return save_dungeon_return_memory(
        story_id,
        branch_id,
        {"visited_dungeons": [], "pending_reentry_dungeon": None},
    )


def _state_snapshot_dungeon(snapshot: object, story_id: str) -> str:
    if not isinstance(snapshot, dict):
        return ""
    return canonicalize_dungeon_name(story_id, snapshot.get("current_dungeon"))


def rebuild_dungeon_return_memory_from_timeline(
    story_id: str,
    source_branch_id: str,
    branch_point_index: int,
    *,
    fallback_state: dict | None = None,
) -> dict:
    visited: list[str] = []
    pending: str | None = None
    current_dungeon = ""
    timeline = get_full_timeline(story_id, source_branch_id)
    for message in timeline:
        index = int(message.get("index", -1) or -1)
        if index > branch_point_index:
            break
        if pending and message.get("role") == "user":
            pending = None
        snapshot_dungeon = _state_snapshot_dungeon(message.get("state_snapshot"), story_id)
        if not snapshot_dungeon or snapshot_dungeon == current_dungeon:
            continue
        current_dungeon = snapshot_dungeon
        if snapshot_dungeon in visited:
            pending = snapshot_dungeon
        else:
            visited.append(snapshot_dungeon)
            pending = None

    final_state_dungeon = _state_snapshot_dungeon(fallback_state or {}, story_id)
    if final_state_dungeon and final_state_dungeon != current_dungeon:
        if final_state_dungeon in visited:
            pending = final_state_dungeon
        else:
            visited.append(final_state_dungeon)
            pending = None

    return _normalize_memory(
        {
            "visited_dungeons": visited,
            "pending_reentry_dungeon": pending,
        }
    )


def save_dungeon_return_memory_for_fork(
    story_id: str,
    source_branch_id: str,
    target_branch_id: str,
    branch_point_index: int,
    *,
    fallback_state: dict | None = None,
) -> dict:
    memory = rebuild_dungeon_return_memory_from_timeline(
        story_id,
        source_branch_id,
        branch_point_index,
        fallback_state=fallback_state,
    )
    return save_dungeon_return_memory(story_id, target_branch_id, memory)


def copy_dungeon_return_memory(story_id: str, from_branch_id: str, to_branch_id: str) -> dict:
    source = load_dungeon_return_memory(story_id, from_branch_id)
    return save_dungeon_return_memory(story_id, to_branch_id, copy.deepcopy(source))


def _current_state(story_id: str, branch_id: str) -> dict:
    return _load_json(_story_character_state_path(story_id, branch_id), {})


def _current_npcs(story_id: str, branch_id: str) -> list[dict]:
    payload = _load_json(_story_npcs_path(story_id, branch_id), [])
    return payload if isinstance(payload, list) else []


def apply_npc_provenance_defaults(
    story_id: str,
    branch_id: str,
    npc_data: dict,
    existing_npc: dict | None = None,
) -> dict:
    """Fill provenance defaults conservatively for new/updated NPCs."""
    if not isinstance(npc_data, dict):
        return {}
    result = dict(npc_data)
    existing_npc = existing_npc if isinstance(existing_npc, dict) else {}
    state = _current_state(story_id, branch_id)
    current_dungeon = canonicalize_dungeon_name(story_id, state.get("current_dungeon"))
    current_phase = _normalize_text(state.get("current_phase"))

    home_scope = normalize_npc_home_scope(result.get("home_scope"))
    if home_scope is None:
        home_scope = normalize_npc_home_scope(existing_npc.get("home_scope"))
    if home_scope is None:
        if current_dungeon:
            home_scope = NPC_HOME_SCOPE_DUNGEON_LOCAL
        elif "主神空間" in current_phase:
            home_scope = NPC_HOME_SCOPE_MAIN_GOD_SPACE
    if home_scope is not None:
        result["home_scope"] = home_scope

    incoming_home_dungeon = canonicalize_dungeon_name(story_id, result.get("home_dungeon"))
    existing_home_dungeon = canonicalize_dungeon_name(story_id, existing_npc.get("home_dungeon"))
    home_dungeon = incoming_home_dungeon or existing_home_dungeon
    if home_scope == NPC_HOME_SCOPE_DUNGEON_LOCAL:
        if not home_dungeon and current_dungeon:
            home_dungeon = current_dungeon
        if home_dungeon:
            result["home_dungeon"] = home_dungeon
    else:
        result["home_dungeon"] = None

    recall_state = normalize_npc_return_recall_state(result.get("return_recall_state"))
    if recall_state == NPC_RETURN_RECALL_UNKNOWN:
        recall_state = normalize_npc_return_recall_state(existing_npc.get("return_recall_state"))
    if recall_state != NPC_RETURN_RECALL_UNKNOWN:
        result["return_recall_state"] = recall_state
    elif "return_recall_state" in result and recall_state == NPC_RETURN_RECALL_UNKNOWN:
        result["return_recall_state"] = NPC_RETURN_RECALL_UNKNOWN

    return result


def _npc_text_blob(npc: dict, relationship_text: str = "") -> str:
    parts = [
        npc.get("name"),
        npc.get("role"),
        npc.get("relationship_to_player"),
        relationship_text,
        npc.get("current_status"),
        npc.get("archived_reason"),
        npc.get("backstory"),
        " ".join(str(v) for v in npc.get("notable_traits", []) if v),
    ]
    return " ".join(_normalize_text(part) for part in parts if _normalize_text(part))


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _relationship_text(relationships: dict, name: str) -> str:
    if not isinstance(relationships, dict):
        return ""
    value = relationships.get(name)
    return _normalize_text(value)


def _classify_local_npc_for_exit(npc: dict, relationship_text: str) -> tuple[str | None, str]:
    lifecycle = normalize_npc_lifecycle_status(npc.get("lifecycle_status"))
    archive_kind = normalize_npc_archive_kind(npc.get("archive_kind"))
    text_blob = _npc_text_blob(npc, relationship_text)

    if archive_kind == NPC_ARCHIVE_KIND_TERMINAL or _contains_any(text_blob, _TERMINAL_KEYWORDS):
        return None, NPC_RETURN_RECALL_EXCLUDED_TERMINAL
    if _contains_any(text_blob, _CONVERTED_KEYWORDS):
        return None, NPC_RETURN_RECALL_EXCLUDED_CONVERTED
    if _contains_any(text_blob, _CROSS_DUNGEON_KEYWORDS):
        return NPC_HOME_SCOPE_CROSS_DUNGEON, NPC_RETURN_RECALL_EXCLUDED_CARRIED_OUT
    if lifecycle == "archived" and archive_kind == NPC_ARCHIVE_KIND_OFFSTAGE:
        return None, NPC_RETURN_RECALL_ELIGIBLE
    return None, NPC_RETURN_RECALL_UNKNOWN


def update_npc_recall_policy_for_exit(story_id: str, branch_id: str, old_dungeon: object) -> int:
    old_dungeon_name = canonicalize_dungeon_name(story_id, old_dungeon)
    if not old_dungeon_name:
        return 0
    state = _current_state(story_id, branch_id)
    relationships = state.get("relationships") if isinstance(state, dict) else {}
    npcs = _current_npcs(story_id, branch_id)
    changed = 0
    new_npcs = []
    for npc in npcs:
        if not isinstance(npc, dict):
            new_npcs.append(npc)
            continue
        home_scope = normalize_npc_home_scope(npc.get("home_scope"))
        home_dungeon = canonicalize_dungeon_name(story_id, npc.get("home_dungeon"))
        if home_scope != NPC_HOME_SCOPE_DUNGEON_LOCAL or home_dungeon != old_dungeon_name:
            new_npcs.append(npc)
            continue
        relationship_text = _relationship_text(relationships, _normalize_text(npc.get("name")))
        new_scope, recall_state = _classify_local_npc_for_exit(npc, relationship_text)
        updated = dict(npc)
        if new_scope:
            updated["home_scope"] = new_scope
            updated["home_dungeon"] = None
        if normalize_npc_return_recall_state(updated.get("return_recall_state")) != recall_state:
            updated["return_recall_state"] = recall_state
        if updated != npc:
            changed += 1
        new_npcs.append(updated)
    if changed:
        _save_json(_story_npcs_path(story_id, branch_id), new_npcs)
    return changed


def handle_dungeon_return_transition(
    story_id: str,
    branch_id: str,
    old_state: dict | None,
    new_state: dict | None,
    *,
    mode: str = "full",
) -> dict:
    """Update dungeon return memory using current_dungeon transition."""
    old_dungeon = canonicalize_dungeon_name(story_id, (old_state or {}).get("current_dungeon"))
    new_dungeon = canonicalize_dungeon_name(story_id, (new_state or {}).get("current_dungeon"))
    memory = load_dungeon_return_memory(story_id, branch_id)
    visited = list(memory.get("visited_dungeons", []))
    pending = memory.get("pending_reentry_dungeon")
    changed = False

    do_exit = mode in {"full", "exit"} and old_dungeon and old_dungeon != new_dungeon
    do_enter = (
        new_dungeon
        and new_dungeon != old_dungeon
        and (mode in {"full", "enter"} or (mode == "exit" and bool(old_dungeon)))
    )

    if do_exit:
        update_npc_recall_policy_for_exit(story_id, branch_id, old_dungeon)
    if do_enter:
        if new_dungeon in visited:
            if pending != new_dungeon:
                pending = new_dungeon
                changed = True
        else:
            visited.append(new_dungeon)
            pending = None
            changed = True

    normalized = _normalize_memory(
        {
            "visited_dungeons": visited,
            "pending_reentry_dungeon": pending,
        }
    )
    if normalized != memory or changed:
        save_dungeon_return_memory(story_id, branch_id, normalized)
    return normalized


def _candidate_relationship_summary(npc: dict, relationship_text: str) -> str:
    for value in (relationship_text, npc.get("relationship_to_player"), npc.get("backstory")):
        text = _normalize_text(value)
        if text:
            return text[:80]
    return "曾在此副本與你有所交集"


def _candidate_exit_status(npc: dict) -> str:
    for value in (npc.get("archived_reason"), npc.get("current_status")):
        text = _normalize_text(value)
        if text:
            return text.replace("current_status:", "")[:60]
    return "已離場"


def _recall_candidates(story_id: str, branch_id: str, current_dungeon: str) -> list[dict]:
    state = _current_state(story_id, branch_id)
    relationships = state.get("relationships") if isinstance(state, dict) else {}
    candidates = []
    for npc in _current_npcs(story_id, branch_id):
        if not isinstance(npc, dict):
            continue
        if normalize_npc_home_scope(npc.get("home_scope")) != NPC_HOME_SCOPE_DUNGEON_LOCAL:
            continue
        if canonicalize_dungeon_name(story_id, npc.get("home_dungeon")) != current_dungeon:
            continue
        if normalize_npc_lifecycle_status(npc.get("lifecycle_status")) != "archived":
            continue
        if normalize_npc_archive_kind(npc.get("archive_kind")) != NPC_ARCHIVE_KIND_OFFSTAGE:
            continue
        if normalize_npc_return_recall_state(npc.get("return_recall_state")) != NPC_RETURN_RECALL_ELIGIBLE:
            continue
        name = _normalize_text(npc.get("name"))
        if not name:
            continue
        relationship_text = _relationship_text(relationships, name)
        candidates.append(
            {
                "name": name,
                "role": _normalize_text(npc.get("role")) or "角色",
                "summary": _candidate_relationship_summary(npc, relationship_text),
                "exit_status": _candidate_exit_status(npc),
                "has_relationship": bool(relationship_text),
                "last_seen_msg_index": int(npc.get("last_seen_msg_index") or 0),
                "has_tier": bool(_normalize_text(npc.get("tier"))),
            }
        )
    candidates.sort(
        key=lambda item: (
            0 if item["has_relationship"] else 1,
            -item["last_seen_msg_index"],
            0 if item["has_tier"] else 1,
            item["name"],
        )
    )
    return candidates


def consume_dungeon_return_recall_block(
    story_id: str,
    branch_id: str,
    *,
    limit: int = DUNGEON_RETURN_NPC_LIMIT,
    char_limit: int = DUNGEON_RETURN_CHAR_LIMIT,
) -> str:
    memory = load_dungeon_return_memory(story_id, branch_id)
    pending = canonicalize_dungeon_name(story_id, memory.get("pending_reentry_dungeon"))
    if not pending:
        return ""
    state = _current_state(story_id, branch_id)
    current_dungeon = canonicalize_dungeon_name(story_id, state.get("current_dungeon"))
    if not current_dungeon or current_dungeon != pending:
        return ""

    lines = ["[舊副本關聯角色提醒]"]
    used = len(lines[0])
    for candidate in _recall_candidates(story_id, branch_id, current_dungeon):
        if len(lines) - 1 >= max(1, limit):
            break
        line = (
            f"- {candidate['name']}（{candidate['role']}）："
            f"{candidate['summary']}。離開時狀態：{candidate['exit_status']}"
        )
        if len(lines) > 1 and used + 1 + len(line) > max(80, char_limit):
            break
        lines.append(line)
        used += 1 + len(line)

    updated = dict(memory)
    updated["pending_reentry_dungeon"] = None
    save_dungeon_return_memory(story_id, branch_id, updated)

    return "\n".join(lines) if len(lines) > 1 else ""


def backfill_dungeon_return_memory(story_id: str, branch_id: str, *, apply: bool = True) -> dict:
    timeline = get_full_timeline(story_id, branch_id)
    branch_point = max((int(msg.get("index", -1) or -1) for msg in timeline), default=-1)
    state = _current_state(story_id, branch_id)
    memory = rebuild_dungeon_return_memory_from_timeline(
        story_id,
        branch_id,
        branch_point,
        fallback_state=state,
    )
    if apply:
        save_dungeon_return_memory(story_id, branch_id, memory)
    return memory


def _resolve_template_name_from_origin(story_id: str, origin_dungeon_id: object) -> str:
    target = _normalize_text(origin_dungeon_id)
    if not target:
        return ""
    for dungeon in _load_dungeon_templates(story_id):
        if _normalize_text(dungeon.get("id")) == target:
            return _normalize_text(dungeon.get("name"))
    return ""


def _match_unique_known_dungeon(story_id: str, text: object) -> str:
    raw = _normalize_text(text)
    if not raw:
        return ""
    raw_key = _normalize_dungeon_name_key(raw)
    matches = []
    for dungeon in _load_dungeon_templates(story_id):
        name = _normalize_text(dungeon.get("name"))
        if name and _normalize_dungeon_name_key(name) and _normalize_dungeon_name_key(name) in raw_key:
            matches.append(name)
    return matches[0] if len(set(matches)) == 1 else ""


def backfill_npc_provenance(story_id: str, branch_id: str, *, apply: bool = True) -> dict:
    state = _current_state(story_id, branch_id)
    relationships = state.get("relationships") if isinstance(state, dict) else {}
    current_dungeon = canonicalize_dungeon_name(story_id, state.get("current_dungeon"))
    npcs = _current_npcs(story_id, branch_id)
    changed = 0
    filled = 0
    updated_npcs = []
    for npc in npcs:
        if not isinstance(npc, dict):
            updated_npcs.append(npc)
            continue
        updated = dict(npc)
        before = dict(updated)

        home_scope = normalize_npc_home_scope(updated.get("home_scope"))
        home_dungeon = canonicalize_dungeon_name(story_id, updated.get("home_dungeon"))
        if not home_dungeon:
            home_dungeon = _resolve_template_name_from_origin(story_id, updated.get("origin_dungeon_id"))
        if not home_dungeon:
            for field in (
                updated.get("backstory"),
                updated.get("archived_reason"),
                updated.get("relationship_to_player"),
                _relationship_text(relationships, _normalize_text(updated.get("name"))),
            ):
                home_dungeon = _match_unique_known_dungeon(story_id, field)
                if home_dungeon:
                    break

        if home_scope is None:
            if home_dungeon:
                home_scope = NPC_HOME_SCOPE_DUNGEON_LOCAL
            elif "主神空間" in _normalize_text(updated.get("current_status")):
                home_scope = NPC_HOME_SCOPE_MAIN_GOD_SPACE
            elif current_dungeon and normalize_npc_lifecycle_status(updated.get("lifecycle_status")) == "active":
                home_scope = NPC_HOME_SCOPE_CROSS_DUNGEON
        if home_scope is not None:
            updated["home_scope"] = home_scope
        if home_scope == NPC_HOME_SCOPE_DUNGEON_LOCAL and home_dungeon:
            updated["home_dungeon"] = home_dungeon
        elif home_scope in {NPC_HOME_SCOPE_MAIN_GOD_SPACE, NPC_HOME_SCOPE_CROSS_DUNGEON}:
            updated["home_dungeon"] = None

        if "last_seen_msg_index" not in updated or updated.get("last_seen_msg_index") is None:
            updated["last_seen_msg_index"] = None
        if normalize_npc_return_recall_state(updated.get("return_recall_state")) == NPC_RETURN_RECALL_UNKNOWN:
            relationship_text = _relationship_text(relationships, _normalize_text(updated.get("name")))
            if home_scope == NPC_HOME_SCOPE_DUNGEON_LOCAL and home_dungeon:
                _, recall_state = _classify_local_npc_for_exit(updated, relationship_text)
                updated["return_recall_state"] = recall_state
            else:
                updated["return_recall_state"] = NPC_RETURN_RECALL_UNKNOWN

        if updated != before:
            changed += 1
        if any(updated.get(key) != before.get(key) for key in ("home_scope", "home_dungeon", "return_recall_state", "last_seen_msg_index")):
            filled += 1
        updated_npcs.append(updated)

    if changed and apply:
        _save_json(_story_npcs_path(story_id, branch_id), updated_npcs)
    return {
        "changed_npcs": changed,
        "filled_fields": filled,
        "npcs": updated_npcs,
    }


__all__ = [
    "DUNGEON_RETURN_NPC_LIMIT",
    "DUNGEON_RETURN_CHAR_LIMIT",
    "NPC_HOME_SCOPE_DUNGEON_LOCAL",
    "NPC_HOME_SCOPE_MAIN_GOD_SPACE",
    "NPC_HOME_SCOPE_CROSS_DUNGEON",
    "NPC_RETURN_RECALL_ELIGIBLE",
    "NPC_RETURN_RECALL_EXCLUDED_CARRIED_OUT",
    "NPC_RETURN_RECALL_EXCLUDED_CONVERTED",
    "NPC_RETURN_RECALL_EXCLUDED_TERMINAL",
    "NPC_RETURN_RECALL_UNKNOWN",
    "canonicalize_dungeon_name",
    "normalize_npc_home_scope",
    "normalize_npc_return_recall_state",
    "normalize_npc_archive_kind",
    "normalize_npc_lifecycle_status",
    "load_dungeon_return_memory",
    "save_dungeon_return_memory",
    "init_dungeon_return_memory",
    "save_dungeon_return_memory_for_fork",
    "copy_dungeon_return_memory",
    "apply_npc_provenance_defaults",
    "handle_dungeon_return_transition",
    "consume_dungeon_return_recall_block",
    "backfill_dungeon_return_memory",
    "backfill_npc_provenance",
]
