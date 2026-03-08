"""NPC loading, normalization, text building, and state-db sync helpers."""

import logging
import re
import unicodedata

from story_core.npc_lifecycle import parse_npc_lifecycle_status
from story_core.dungeon_return_memory import (
    NPC_ARCHIVE_KIND_OFFSTAGE,
    NPC_HOME_SCOPE_DUNGEON_LOCAL,
    NPC_RETURN_RECALL_ELIGIBLE,
    NPC_RETURN_RECALL_UNKNOWN,
    apply_npc_provenance_defaults,
    canonicalize_dungeon_name,
    normalize_npc_home_scope,
    normalize_npc_return_recall_state,
)
from story_core.state_db import (
    build_npc_content as build_state_npc_content,
    replace_categories_batch as replace_state_categories_batch,
    upsert_entry as upsert_state_entry,
)
from story_core.story_io import (
    _load_json,
    _save_json,
    _story_character_state_path,
    _story_npcs_path,
)
from story_core.tag_extraction import _extract_item_base_name

log = logging.getLogger("rpg")


def _rel_to_str(val) -> str:
    """Normalize a relationship value to string (may be str or dict)."""
    if isinstance(val, dict):
        return val.get("summary") or val.get("description") or val.get("type") or ""
    return val or ""


_NPC_TIER_ALLOWLIST = {
    "D-", "D", "D+",
    "C-", "C", "C+",
    "B-", "B", "B+",
    "A-", "A", "A+",
    "S-", "S", "S+",
}
_NPC_TIER_TRANSLATION = str.maketrans({
    "－": "-",
    "—": "-",
    "–": "-",
    "−": "-",
    "﹣": "-",
    "ー": "-",
    "＋": "+",
})
_NPC_ARCHIVE_KIND_OFFSTAGE = "offstage"
_NPC_ARCHIVE_KIND_TERMINAL = "terminal"
_NPC_ARCHIVE_KINDS = {
    _NPC_ARCHIVE_KIND_OFFSTAGE,
    _NPC_ARCHIVE_KIND_TERMINAL,
}
_NPC_ARCHIVE_KEYWORDS_TERMINAL = (
    "已損毀",
    "威脅解除",
    "已失效",
    "已封印",
    "已消散",
)
_NPC_ARCHIVE_KEYWORDS_OFFSTAGE = (
    "已退場",
    "已離隊",
)
_NPC_UNARCHIVE_KEYWORDS = (
    "修復",
    "復活",
    "再現身",
    "重新啟用",
    "解除封印",
)
_NPC_NAME_R1_PUNCT_RE = re.compile(
    r"[ \t\r\n\u3000\.\,，。:：;；!！?？'\"“”‘’`~·•・\-—–−_()（）\[\]【】{}<>《》〈〉/\\|+]+"
)


def _normalize_npc_tier(raw_tier: object) -> str | None:
    """Normalize tier text to allowlist value (D-..S+)."""
    if not isinstance(raw_tier, str):
        return None
    tier = raw_tier.strip().upper().translate(_NPC_TIER_TRANSLATION)
    if not tier:
        return None
    tier = tier.replace("級", "").replace("级", "")
    tier = tier.replace(" ", "").replace("\u3000", "")
    return tier if tier in _NPC_TIER_ALLOWLIST else None


def _normalize_npc_name_r1(name: object) -> str:
    """R1 normalization: NFKC + remove spaces/punctuation + casefold."""
    if not isinstance(name, str):
        return ""
    normalized = unicodedata.normalize("NFKC", name).strip()
    if not normalized:
        return ""
    normalized = _NPC_NAME_R1_PUNCT_RE.sub("", normalized)
    return normalized.casefold()


def _resolve_npc_identity(name: str, existing_npcs: list[dict]) -> str | None:
    """Resolve incoming name against existing NPC identity (exact, then R1-equal)."""
    candidate = (name or "").strip()
    if not candidate:
        return None

    for npc in existing_npcs:
        if (npc.get("name") or "").strip() == candidate:
            return npc.get("name")

    normalized_candidate = _normalize_npc_name_r1(candidate)
    if not normalized_candidate:
        return None
    for npc in existing_npcs:
        existing_name = (npc.get("name") or "").strip()
        if not existing_name:
            continue
        if _normalize_npc_name_r1(existing_name) == normalized_candidate:
            return existing_name
    return None


def _normalize_npc_lifecycle_status(raw_status: object) -> str | None:
    return parse_npc_lifecycle_status(raw_status)


def _normalize_npc_archive_kind(raw_kind: object) -> str | None:
    if not isinstance(raw_kind, str):
        return None
    kind = raw_kind.strip().lower()
    return kind if kind in _NPC_ARCHIVE_KINDS else None


def _derive_npc_lifecycle_from_current_status(
    current_status: object,
    existing_status: object,
) -> tuple[str, str | None, str | None]:
    """Derive lifecycle from current_status only; fallback to existing lifecycle."""
    existing = _normalize_npc_lifecycle_status(existing_status) or "active"
    status_text = str(current_status or "").strip()
    if not status_text:
        return existing, None, None
    for kw in _NPC_UNARCHIVE_KEYWORDS:
        if kw in status_text:
            return "active", kw, None
    for kw in _NPC_ARCHIVE_KEYWORDS_TERMINAL:
        if kw in status_text:
            return "archived", kw, _NPC_ARCHIVE_KIND_TERMINAL
    for kw in _NPC_ARCHIVE_KEYWORDS_OFFSTAGE:
        if kw in status_text:
            return "archived", kw, _NPC_ARCHIVE_KIND_OFFSTAGE
    return existing, None, None


def _classify_npc(npc: dict, rels: dict) -> str:
    """Classify an NPC into a relationship category."""
    name = npc.get("name", "")
    status = (npc.get("current_status") or "").lower()
    role = (npc.get("role") or "").lower()
    rel_player = (npc.get("relationship_to_player") or "").lower()
    char_rel = _rel_to_str(rels.get(name)).lower()
    combined = f"{status} {role} {rel_player} {char_rel}"

    if any(k in status for k in ("死亡", "已故", "陣亡")):
        return "dead"
    if any(k in combined for k in ("敵", "對手", "威脅", "仇")):
        return "hostile"
    if any(k in combined for k in ("俘", "囚", "關押")):
        return "captured"
    ally_kw = (
        "隊友", "戰友", "同伴", "盟友", "夥伴", "伴侶", "隨從",
        "忠誠", "信任", "兄弟", "好感", "崇拜", "曖昧", "約定",
    )
    if any(k in combined for k in ally_kw):
        return "ally"
    if name in rels:
        return "ally"
    return "neutral"


def _load_npcs(story_id: str, branch_id: str = "main", include_archived: bool = False) -> list[dict]:
    path = _story_npcs_path(story_id, branch_id)
    npcs = _load_json(path, [])
    if include_archived:
        return npcs
    active_npcs = []
    for npc in npcs:
        if not isinstance(npc, dict):
            continue
        if _normalize_npc_lifecycle_status(npc.get("lifecycle_status")) == "archived":
            continue
        active_npcs.append(npc)
    return active_npcs


def _build_npc_summary_text(story_id: str, branch_id: str = "main", npcs: list[dict] | None = None) -> str:
    """Build compact NPC summary for system prompt (details come from state RAG)."""
    if npcs is None:
        npcs = _load_npcs(story_id, branch_id)
    if not npcs:
        return "（尚無已記錄的 NPC）"
    tier_known = sum(1 for npc in npcs if _normalize_npc_tier(npc.get("tier")))
    if tier_known:
        return (
            f"共 {len(npcs)} 位 NPC（已標註戰力 {tier_known} 位），"
            "系統會根據對話內容自動檢索相關 NPC 檔案。"
        )
    return f"共 {len(npcs)} 位 NPC，系統會根據對話內容自動檢索相關 NPC 檔案。"


def _build_npc_state_entry_content(npc: dict) -> str:
    """Build compact text persisted to state.db npc entries."""
    return build_state_npc_content(npc)


def _sync_state_db_npc_entry(story_id: str, branch_id: str, npc: dict):
    """Sync one NPC row into state.db."""
    name = (npc.get("name") or "").strip()
    if not name:
        return
    tags = "NPC"
    if _normalize_npc_lifecycle_status(npc.get("lifecycle_status")) == "archived":
        tags = "NPC|ARCHIVED"
    upsert_state_entry(
        story_id,
        branch_id,
        category="npc",
        entry_key=name,
        content=_build_npc_state_entry_content(npc),
        tags=tags,
    )


def _sync_state_db_from_state(story_id: str, branch_id: str, state: dict):
    """Sync canonical state JSON into state.db categories (excluding npc)."""
    from app import _parse_item_to_kv

    try:
        inv_rows = []
        inv = state.get("inventory", {})
        if isinstance(inv, dict):
            for key, value in inv.items():
                clean_key = str(key).strip()
                if not clean_key:
                    continue
                inv_rows.append((clean_key, "" if value is None else str(value), "道具"))
        elif isinstance(inv, list):
            for item in inv:
                if not isinstance(item, str) or not item.strip():
                    continue
                key, value = _parse_item_to_kv(item.strip())
                if key:
                    inv_rows.append((key, value, "道具"))

        ability_rows = []
        abilities = state.get("abilities", [])
        if isinstance(abilities, list):
            for item in abilities:
                if isinstance(item, str) and item.strip():
                    ability_rows.append((item.strip(), "", "技能"))

        rel_rows = []
        rels = state.get("relationships", {})
        if isinstance(rels, dict):
            for name, rel in rels.items():
                key = str(name).strip()
                if not key:
                    continue
                rel_rows.append((key, _rel_to_str(rel), "關係"))

        mission_rows = []
        missions = state.get("completed_missions", [])
        if isinstance(missions, list):
            for item in missions:
                if isinstance(item, str) and item.strip():
                    mission_rows.append((item.strip(), "", "任務"))

        system_rows = []
        systems = state.get("systems", {})
        if isinstance(systems, dict):
            for name, level in systems.items():
                key = str(name).strip()
                if not key:
                    continue
                system_rows.append((key, "" if level is None else str(level), "體系"))
        replace_state_categories_batch(
            story_id,
            branch_id,
            {
                "inventory": inv_rows,
                "ability": ability_rows,
                "relationship": rel_rows,
                "mission": mission_rows,
                "system": system_rows,
            },
        )
    except Exception:
        log.warning("state_db sync failed for %s/%s", story_id, branch_id, exc_info=True)


def _clean_relationship_archive_note(story_id: str, branch_id: str, npc_name: str):
    """Remove archive note suffix from an NPC relationship when the NPC returns."""
    from app import _load_character_state

    state = _load_character_state(story_id, branch_id)
    rels = state.get("relationships")
    if not isinstance(rels, dict):
        return
    value = rels.get(npc_name)
    if not isinstance(value, str) or " (已歸檔)" not in value:
        return
    rels[npc_name] = value.replace(" (已歸檔)", "")
    _save_json(_story_character_state_path(story_id, branch_id), state)
    _sync_state_db_from_state(story_id, branch_id, state)


def _save_npc(
    story_id: str,
    npc_data: dict,
    branch_id: str = "main",
    origin_dungeon_id: str | None = None,
    origin_run_id: str | None = None,
    archive_kind: str | None = None,
    msg_index: int | None = None,
):
    """Save or update an NPC entry. Matches by 'name' field."""
    npcs = _load_npcs(story_id, branch_id, include_archived=True)
    npc_data = dict(npc_data)
    name = npc_data.get("name", "").strip()
    if not name:
        return

    matched_name = _resolve_npc_identity(name, npcs)
    if matched_name and matched_name != name:
        name = matched_name
        npc_data["name"] = matched_name

    if "tier" in npc_data:
        normalized_tier = _normalize_npc_tier(npc_data.get("tier"))
        if normalized_tier:
            npc_data["tier"] = normalized_tier
        else:
            npc_data.pop("tier", None)

    existing_index = None
    existing_npc = None
    for i, existing in enumerate(npcs):
        if existing.get("name") == name:
            existing_index = i
            existing_npc = existing
            break

    npc_data = apply_npc_provenance_defaults(story_id, branch_id, npc_data, existing_npc)
    if msg_index is not None:
        try:
            npc_data["last_seen_msg_index"] = int(msg_index)
        except (TypeError, ValueError):
            pass

    existing_origin_dungeon_id = str((existing_npc or {}).get("origin_dungeon_id") or "").strip()
    existing_origin_run_id = str((existing_npc or {}).get("origin_run_id") or "").strip()
    if origin_dungeon_id and not existing_origin_dungeon_id:
        npc_data["origin_dungeon_id"] = origin_dungeon_id
    if origin_run_id and not existing_origin_run_id:
        npc_data["origin_run_id"] = origin_run_id

    explicit_lifecycle = _normalize_npc_lifecycle_status(npc_data.get("lifecycle_status"))
    existing_lifecycle = _normalize_npc_lifecycle_status(
        existing_npc.get("lifecycle_status") if existing_npc else None
    )
    existing_archive_kind = _normalize_npc_archive_kind(
        existing_npc.get("archive_kind") if existing_npc else None
    )
    normalized_archive_kind = _normalize_npc_archive_kind(archive_kind)
    if normalized_archive_kind is None:
        normalized_archive_kind = _normalize_npc_archive_kind(npc_data.get("archive_kind"))
    incoming_archive_requested = explicit_lifecycle == "archived"
    if explicit_lifecycle:
        npc_data["lifecycle_status"] = explicit_lifecycle
        if explicit_lifecycle == "active":
            npc_data["archived_reason"] = None
            npc_data["archive_kind"] = None
        else:
            npc_data["archive_kind"] = (
                normalized_archive_kind or existing_archive_kind or _NPC_ARCHIVE_KIND_TERMINAL
            )
            explicit_reason = str(npc_data.get("archived_reason") or "").strip()
            if explicit_reason:
                npc_data["archived_reason"] = explicit_reason
            elif existing_npc and existing_npc.get("archived_reason"):
                npc_data["archived_reason"] = existing_npc.get("archived_reason")
            else:
                npc_data["archived_reason"] = "explicit"
    else:
        derived_lifecycle, matched_kw, derived_archive_kind = _derive_npc_lifecycle_from_current_status(
            npc_data.get("current_status"),
            existing_lifecycle,
        )
        if derived_lifecycle == "archived" and matched_kw:
            incoming_archive_requested = True
        npc_data["lifecycle_status"] = derived_lifecycle
        if derived_lifecycle == "archived":
            npc_data["archive_kind"] = (
                derived_archive_kind
                or normalized_archive_kind
                or existing_archive_kind
                or _NPC_ARCHIVE_KIND_TERMINAL
            )
            if matched_kw:
                npc_data["archived_reason"] = f"current_status:{matched_kw}"
            elif existing_npc and existing_npc.get("archived_reason"):
                npc_data["archived_reason"] = existing_npc.get("archived_reason")
            else:
                npc_data["archived_reason"] = "current_status"
        else:
            npc_data["archived_reason"] = None
            npc_data["archive_kind"] = None

    reactivated = False
    state = _load_json(_story_character_state_path(story_id, branch_id), {})
    current_dungeon = canonicalize_dungeon_name(story_id, state.get("current_dungeon"))
    existing_home_scope = normalize_npc_home_scope(
        (existing_npc or {}).get("home_scope") if existing_npc else None
    )
    existing_home_dungeon = canonicalize_dungeon_name(
        story_id,
        (existing_npc or {}).get("home_dungeon") if existing_npc else None,
    )
    existing_recall_state = normalize_npc_return_recall_state(
        (existing_npc or {}).get("return_recall_state") if existing_npc else None
    )
    if (
        existing_npc
        and existing_lifecycle == "archived"
        and existing_archive_kind == _NPC_ARCHIVE_KIND_OFFSTAGE
        and not incoming_archive_requested
        and existing_home_scope == NPC_HOME_SCOPE_DUNGEON_LOCAL
        and existing_home_dungeon
        and existing_home_dungeon == current_dungeon
        and existing_recall_state == NPC_RETURN_RECALL_ELIGIBLE
    ):
        npc_data["lifecycle_status"] = "active"
        npc_data["archive_kind"] = None
        npc_data["archived_reason"] = None
        reactivated = True
    elif (
        existing_npc
        and existing_lifecycle == "archived"
        and existing_archive_kind == _NPC_ARCHIVE_KIND_OFFSTAGE
        and origin_run_id
        and existing_origin_run_id == origin_run_id
        and not incoming_archive_requested
    ):
        npc_data["lifecycle_status"] = "active"
        npc_data["archive_kind"] = None
        npc_data["archived_reason"] = None
        reactivated = True
    elif existing_npc and existing_lifecycle == "archived" and npc_data.get("lifecycle_status") == "active":
        reactivated = True
    if reactivated and "current_status" not in npc_data:
        npc_data["current_status"] = ""

    if reactivated:
        npc_data["return_recall_state"] = NPC_RETURN_RECALL_UNKNOWN

    if "id" not in npc_data:
        if existing_npc and existing_npc.get("id"):
            npc_data["id"] = existing_npc.get("id")
        else:
            npc_data["id"] = "npc_" + re.sub(r"\W+", "", name)[:20]

    if existing_index is not None:
        merged = {**existing_npc, **npc_data}
        npcs[existing_index] = merged
        _save_json(_story_npcs_path(story_id, branch_id), npcs)
        _sync_state_db_npc_entry(story_id, branch_id, merged)
        if reactivated:
            _clean_relationship_archive_note(story_id, branch_id, name)
        return

    npcs.append(npc_data)
    _save_json(_story_npcs_path(story_id, branch_id), npcs)
    _sync_state_db_npc_entry(story_id, branch_id, npc_data)
    if reactivated:
        _clean_relationship_archive_note(story_id, branch_id, name)


def _copy_npcs_to_branch(story_id: str, from_branch_id: str, to_branch_id: str):
    """Copy NPC data from parent branch to new branch."""
    npcs = _load_npcs(story_id, from_branch_id, include_archived=True)
    _save_json(_story_npcs_path(story_id, to_branch_id), npcs)


def _build_npc_text(story_id: str, branch_id: str = "main", npcs: list[dict] | None = None) -> str:
    """Build NPC profiles text for system prompt injection."""
    if npcs is None:
        npcs = _load_npcs(story_id, branch_id)
    if not npcs:
        return "（尚無已記錄的 NPC）"

    lines = []
    for npc in npcs:
        tier = _normalize_npc_tier(npc.get("tier"))
        tier_label = f"【{tier} 級】" if tier else ""
        lines.append(f"### {npc.get('name', '?')}（{npc.get('role', '?')}）{tier_label}")
        if npc.get("appearance"):
            lines.append(f"- 外觀：{npc['appearance']}")
        personality = npc.get("personality", {})
        if isinstance(personality, dict) and personality.get("summary"):
            lines.append(f"- 性格：{personality['summary']}")
        if npc.get("relationship_to_player"):
            lines.append(f"- 與主角關係：{npc['relationship_to_player']}")
        if npc.get("current_status"):
            lines.append(f"- 狀態：{npc['current_status']}")
        if npc.get("notable_traits"):
            lines.append(f"- 特質：{'、'.join(npc['notable_traits'])}")
        lines.append("")

    return "\n".join(lines).strip()


__all__ = [
    "_rel_to_str",
    "_normalize_npc_tier",
    "_normalize_npc_name_r1",
    "_resolve_npc_identity",
    "_normalize_npc_lifecycle_status",
    "_derive_npc_lifecycle_from_current_status",
    "_classify_npc",
    "_load_npcs",
    "_save_npc",
    "_copy_npcs_to_branch",
    "_build_npc_text",
    "_build_npc_summary_text",
    "_build_npc_state_entry_content",
    "_sync_state_db_npc_entry",
    "_sync_state_db_from_state",
]
