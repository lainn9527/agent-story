"""Global timer tracking for the shared universe.

Each branch tracks its own `world_day` (float, in days).
TIME tags in GM responses advance the timer.
Fixed costs (dungeon enter/exit, training) also advance it.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

log = logging.getLogger("rpg")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# TIME tag regex — extracted in _process_gm_response()
# ---------------------------------------------------------------------------
TIME_RE = re.compile(r'<!--TIME\s+(.*?)\s*TIME-->', re.DOTALL)

# ---------------------------------------------------------------------------
# Default dungeon time costs (days)
# ---------------------------------------------------------------------------
DUNGEON_TIME_COSTS = {
    "default_enter": 3,
    "default_exit": 1,
    "training": 2,
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _world_day_path(story_id: str, branch_id: str) -> str:
    return os.path.join(
        BASE_DIR, "data", "stories", story_id,
        "branches", branch_id, "world_day.json",
    )


def _load_json(path: str, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _save_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_world_day(story_id: str, branch_id: str) -> float:
    """Get current world_day for a branch. Default 0."""
    data = _load_json(_world_day_path(story_id, branch_id))
    return data.get("world_day", 0) if data else 0


def advance_world_day(story_id: str, branch_id: str, days: float) -> float:
    """Advance world_day by N days. Returns new world_day."""
    if days <= 0:
        return get_world_day(story_id, branch_id)
    path = _world_day_path(story_id, branch_id)
    data = _load_json(path, {"world_day": 0})
    data["world_day"] = data.get("world_day", 0) + days
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    _save_json(path, data)
    log.info("    world_day: branch=%s advanced +%.1f → %.1f",
             branch_id, days, data["world_day"])
    return data["world_day"]


def set_world_day(story_id: str, branch_id: str, day: float):
    """Set world_day to an exact value (used for branch inheritance)."""
    path = _world_day_path(story_id, branch_id)
    data = {
        "world_day": day,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    _save_json(path, data)


def copy_world_day(story_id: str, from_bid: str, to_bid: str):
    """Copy parent's world_day to new branch."""
    day = get_world_day(story_id, from_bid)
    if day > 0:
        set_world_day(story_id, to_bid, day)


# ---------------------------------------------------------------------------
# TIME tag parsing
# ---------------------------------------------------------------------------

def parse_time_tag(time_str: str) -> float:
    """Parse a TIME tag body like 'days:3' or 'hours:8' into days.

    Returns 0 if unparseable.
    """
    time_str = time_str.strip()
    if "days:" in time_str:
        try:
            return float(time_str.split("days:")[1].strip())
        except (ValueError, IndexError):
            return 0
    if "hours:" in time_str:
        try:
            return float(time_str.split("hours:")[1].strip()) / 24
        except (ValueError, IndexError):
            return 0
    return 0


def process_time_tags(gm_text: str, story_id: str, branch_id: str) -> str:
    """Extract <!--TIME ... TIME--> tags from GM text, advance world_day.

    Returns the text with TIME tags removed.
    """
    for m in TIME_RE.finditer(gm_text):
        days = parse_time_tag(m.group(1))
        if days > 0:
            advance_world_day(story_id, branch_id, days)
    return TIME_RE.sub("", gm_text).strip()


# ---------------------------------------------------------------------------
# Phase-based auto-advance
# ---------------------------------------------------------------------------

def advance_dungeon_enter(story_id: str, branch_id: str,
                          dungeon_name: str = "") -> float:
    """Auto-advance world_day when entering a dungeon."""
    cost = DUNGEON_TIME_COSTS["default_enter"]
    return advance_world_day(story_id, branch_id, cost)


def advance_dungeon_exit(story_id: str, branch_id: str) -> float:
    """Auto-advance world_day when exiting a dungeon (recovery day)."""
    cost = DUNGEON_TIME_COSTS["default_exit"]
    return advance_world_day(story_id, branch_id, cost)
