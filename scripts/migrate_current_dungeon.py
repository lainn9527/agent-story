#!/usr/bin/env python3
"""One-time data migration: backfill current_dungeon for branches in a dungeon.

For branches where current_phase contains '副本' but current_dungeon is empty,
infers the dungeon name from the last 5 副本世界觀 entries in branch_lore.json
(majority vote of subcategory).

Usage:
    python scripts/migrate_current_dungeon.py [--dry-run]
"""

import json
import os
import sys
from collections import Counter
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STORIES_DIR = os.path.join(DATA_DIR, "stories")


def migrate(dry_run: bool = False):
    stories_json = os.path.join(DATA_DIR, "stories.json")
    if not os.path.exists(stories_json):
        print("No stories.json found, nothing to migrate.")
        return

    registry = json.loads(Path(stories_json).read_text(encoding="utf-8"))
    story_ids = list(registry.get("stories", {}).keys())

    total_migrated = 0

    for story_id in story_ids:
        tree_path = os.path.join(STORIES_DIR, story_id, "timeline_tree.json")
        if not os.path.exists(tree_path):
            continue

        tree = json.loads(Path(tree_path).read_text(encoding="utf-8"))
        branches = tree.get("branches", {})

        for bid, branch in branches.items():
            if branch.get("deleted"):
                continue

            branch_dir = os.path.join(STORIES_DIR, story_id, "branches", bid)
            state_path = os.path.join(branch_dir, "character_state.json")
            if not os.path.exists(state_path):
                continue

            state = json.loads(Path(state_path).read_text(encoding="utf-8"))
            phase = state.get("current_phase", "")

            if "副本" not in phase:
                continue
            if state.get("current_dungeon"):
                continue  # already set

            # Infer from branch_lore last entries
            bl_path = os.path.join(branch_dir, "branch_lore.json")
            if not os.path.exists(bl_path):
                continue

            bl = json.loads(Path(bl_path).read_text(encoding="utf-8"))
            dungeon_entries = [
                e.get("subcategory", "")
                for e in bl
                if e.get("category") == "副本世界觀" and e.get("subcategory")
            ]
            if not dungeon_entries:
                continue

            # Take last 5 entries, majority vote
            last_n = dungeon_entries[-5:]
            most_common = Counter(last_n).most_common(1)[0][0]

            if dry_run:
                print(f"[DRY-RUN] {story_id}/{bid}: would set current_dungeon={most_common}")
            else:
                state["current_dungeon"] = most_common
                Path(state_path).write_text(
                    json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"[MIGRATED] {story_id}/{bid}: current_dungeon={most_common}")

            total_migrated += 1

    print(f"\nTotal: {total_migrated} branches {'would be' if dry_run else ''} migrated.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== DRY RUN MODE ===\n")
    migrate(dry_run=dry_run)
