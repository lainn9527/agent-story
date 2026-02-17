#!/usr/bin/env python3
"""
Backfill world_day_snapshot and npcs_snapshot into existing GM messages.

After this fix, every GM message gets all 3 snapshots (state_snapshot,
npcs_snapshot, world_day_snapshot) written by _process_gm_response().
This script patches historical messages that predate the fix.

Strategy:
- state_snapshot: Most GM messages already have this. Fill gaps by carrying
  forward the most recent snapshot, or falling back to branch character_state.
- npcs_snapshot: Only existed on messages with NPC tag updates. Fill by
  carrying forward the most recent snapshot, or falling back to branch npcs.json.
- world_day_snapshot: Never existed before. TIME tags have been stripped from
  stored text so exact history can't be reconstructed. Fill all messages with
  the branch's current world_day value (monotonically increasing within a branch,
  so this is a reasonable approximation).

Usage:
    python scripts/backfill_snapshots.py              # apply changes
    python scripts/backfill_snapshots.py --dry-run    # preview only
"""

import json
import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("backfill_snapshots")

STORIES_DIR = os.path.join(PROJECT_ROOT, "data", "stories")
STORY_DESIGN_DIR = os.path.join(PROJECT_ROOT, "story_design")


def _load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _get_world_day(story_id, branch_id):
    """Get current world_day for a branch from its world_day.json."""
    path = os.path.join(STORIES_DIR, story_id, "branches", branch_id, "world_day.json")
    data = _load_json(path)
    return data.get("world_day", 0) if data else 0


def backfill_messages(messages, fallback_state, fallback_npcs, fallback_world_day):
    """Backfill snapshots into a list of messages. Returns (messages, modified_count)."""
    modified = 0

    # Carry-forward state for filling gaps
    last_state = None
    last_npcs = None

    for msg in messages:
        if msg.get("role") != "assistant":
            continue

        changed = False

        # Track carry-forward values from existing snapshots
        if "state_snapshot" in msg:
            last_state = msg["state_snapshot"]
        if "npcs_snapshot" in msg:
            last_npcs = msg["npcs_snapshot"]

        # Fill state_snapshot if missing
        if "state_snapshot" not in msg:
            msg["state_snapshot"] = last_state if last_state is not None else fallback_state
            if msg["state_snapshot"] is not None:
                last_state = msg["state_snapshot"]
            changed = True

        # Fill npcs_snapshot if missing
        if "npcs_snapshot" not in msg:
            msg["npcs_snapshot"] = last_npcs if last_npcs is not None else fallback_npcs
            if msg["npcs_snapshot"] is not None:
                last_npcs = msg["npcs_snapshot"]
            changed = True

        # Fill world_day_snapshot if missing
        if "world_day_snapshot" not in msg:
            msg["world_day_snapshot"] = fallback_world_day
            changed = True

        if changed:
            modified += 1

    return messages, modified


def process_branch(story_id, branch_id, dry_run=False):
    """Backfill snapshots for a single branch's messages.json."""
    msg_path = os.path.join(STORIES_DIR, story_id, "branches", branch_id, "messages.json")
    if not os.path.exists(msg_path):
        return 0

    messages = _load_json(msg_path, [])
    if not messages:
        return 0

    # Fallback values from branch files
    state_path = os.path.join(STORIES_DIR, story_id, "branches", branch_id, "character_state.json")
    npcs_path = os.path.join(STORIES_DIR, story_id, "branches", branch_id, "npcs.json")
    fallback_state = _load_json(state_path, {})
    fallback_npcs = _load_json(npcs_path, [])
    fallback_world_day = _get_world_day(story_id, branch_id)

    messages, modified = backfill_messages(messages, fallback_state, fallback_npcs, fallback_world_day)

    if modified > 0:
        label = "[DRY-RUN] " if dry_run else ""
        log.info("%s%s/%s: patched %d GM messages", label, story_id, branch_id, modified)
        if not dry_run:
            _save_json(msg_path, messages)

    return modified


def process_parsed_conversation(story_id, dry_run=False):
    """Backfill snapshots for a story's parsed_conversation.json (main branch messages)."""
    path = os.path.join(STORY_DESIGN_DIR, story_id, "parsed_conversation.json")
    if not os.path.exists(path):
        return 0

    messages = _load_json(path, [])
    if not messages:
        return 0

    # Fallback: use main branch files
    state_path = os.path.join(STORIES_DIR, story_id, "branches", "main", "character_state.json")
    npcs_path = os.path.join(STORIES_DIR, story_id, "branches", "main", "npcs.json")
    # Also try legacy flat paths
    if not os.path.exists(state_path):
        state_path = os.path.join(STORIES_DIR, story_id, "character_state_main.json")
    if not os.path.exists(npcs_path):
        npcs_path = os.path.join(STORIES_DIR, story_id, "npcs.json")

    fallback_state = _load_json(state_path, {})
    fallback_npcs = _load_json(npcs_path, [])
    fallback_world_day = _get_world_day(story_id, "main")

    messages, modified = backfill_messages(messages, fallback_state, fallback_npcs, fallback_world_day)

    if modified > 0:
        label = "[DRY-RUN] " if dry_run else ""
        log.info("%s%s/parsed_conversation: patched %d GM messages", label, story_id, modified)
        if not dry_run:
            _save_json(path, messages)

    return modified


def main():
    dry_run = "--dry-run" in sys.argv

    if not os.path.isdir(STORIES_DIR):
        log.error("Stories directory not found: %s", STORIES_DIR)
        sys.exit(1)

    total_modified = 0
    total_files = 0

    for story_id in sorted(os.listdir(STORIES_DIR)):
        story_dir = os.path.join(STORIES_DIR, story_id)
        if not os.path.isdir(story_dir):
            continue

        # Process parsed_conversation.json
        m = process_parsed_conversation(story_id, dry_run)
        if m > 0:
            total_modified += m
            total_files += 1

        # Process each branch
        branches_dir = os.path.join(story_dir, "branches")
        if not os.path.isdir(branches_dir):
            continue

        for branch_id in sorted(os.listdir(branches_dir)):
            branch_dir = os.path.join(branches_dir, branch_id)
            if not os.path.isdir(branch_dir):
                continue

            m = process_branch(story_id, branch_id, dry_run)
            if m > 0:
                total_modified += m
                total_files += 1

    mode = "DRY-RUN" if dry_run else "APPLIED"
    log.info("[%s] Done. Patched %d messages across %d files.", mode, total_modified, total_files)


if __name__ == "__main__":
    main()
