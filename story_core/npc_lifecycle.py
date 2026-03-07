"""Shared NPC lifecycle normalization helpers."""

from __future__ import annotations

NPC_LIFECYCLE_ACTIVE = "active"
NPC_LIFECYCLE_ARCHIVED = "archived"

_ACTIVE_ALIASES = {"active", "啟用", "活动", "活動"}
_ARCHIVED_ALIASES = {"archived", "archive", "封存", "已封存", "归档", "歸檔"}


def parse_npc_lifecycle_status(raw_status: object) -> str | None:
    """Normalize lifecycle status alias to canonical value or None."""
    if not isinstance(raw_status, str):
        return None
    text = raw_status.strip().lower()
    if not text:
        return None
    if text in _ACTIVE_ALIASES:
        return NPC_LIFECYCLE_ACTIVE
    if text in _ARCHIVED_ALIASES:
        return NPC_LIFECYCLE_ARCHIVED
    return None
