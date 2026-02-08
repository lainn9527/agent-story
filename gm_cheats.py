"""GM 金手指系統 — /gm command parsing and per-branch cheat storage."""

import json
import os
import re
import shutil
from typing import Optional

# ── /gm dice command pattern ─────────────────────────────────────
# Matches: /gm dice +30, /gm dice -10, /gm 骰子 +20, /gm dice reset
_DICE_CMD_RE = re.compile(
    r"^/gm\s+(?:dice|骰子)\s*([+-]\d+|reset|重置)",
    re.IGNORECASE,
)


def _cheats_path(story_dir: str, branch_id: str) -> str:
    return os.path.join(story_dir, "branches", branch_id, "gm_cheats.json")


def load_cheats(story_dir: str, branch_id: str) -> dict:
    path = _cheats_path(story_dir, branch_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cheats(story_dir: str, branch_id: str, cheats: dict) -> None:
    path = _cheats_path(story_dir, branch_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cheats, f, ensure_ascii=False, indent=2)


def copy_cheats(story_dir: str, src_branch: str, dst_branch: str) -> None:
    """Copy cheats from source branch to destination (on branch creation)."""
    src = _cheats_path(story_dir, src_branch)
    if os.path.exists(src):
        dst = _cheats_path(story_dir, dst_branch)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)


def get_dice_modifier(story_dir: str, branch_id: str) -> int:
    """Get the current dice modifier for a branch."""
    cheats = load_cheats(story_dir, branch_id)
    return cheats.get("dice_modifier", 0)


def is_gm_command(text: str) -> bool:
    """Check if a message is a /gm command."""
    return text.strip().startswith("/gm")


def parse_dice_command(text: str) -> Optional[int]:
    """Parse /gm dice command and return the new modifier value.

    Returns:
        int: The new modifier value (0 for reset), or None if not a dice command.
    """
    m = _DICE_CMD_RE.match(text.strip())
    if not m:
        return None
    val = m.group(1)
    if val in ("reset", "重置"):
        return 0
    return int(val)


def apply_dice_command(story_dir: str, branch_id: str, text: str) -> Optional[dict]:
    """Parse and apply a /gm dice command if present.

    Returns:
        dict with {old, new, action} if a dice command was applied, else None.
    """
    new_mod = parse_dice_command(text)
    if new_mod is None:
        return None

    cheats = load_cheats(story_dir, branch_id)
    old_mod = cheats.get("dice_modifier", 0)
    cheats["dice_modifier"] = new_mod
    save_cheats(story_dir, branch_id, cheats)

    action = "reset" if new_mod == 0 else ("add" if new_mod > 0 else "subtract")
    return {"old": old_mod, "new": new_mod, "action": action}
