#!/usr/bin/env python3
"""
Clean up corrupted character_state.json files across all branches.

Fixes:
1. Remove single-character entries from list fields (caused by string iteration bug)
2. Remove *_delta keys (should be consumed, not persisted)
3. Remove *_add / *_remove helper keys (should be consumed, not persisted)
4. Remove system keys (world_day, world_time, branch_title)
"""

import json
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
SYSTEM_KEYS = {"world_day", "world_time", "branch_title"}

# List field keys from the schema
LIST_KEYS = {"inventory", "completed_missions", "abilities", "skills"}


def cleanup_state(state):
    """Clean up a character state dict. Returns (cleaned_state, changes)."""
    changes = []

    # 1. Remove single-char entries from list fields
    for key in list(state.keys()):
        val = state[key]
        if isinstance(val, list):
            original_len = len(val)
            # Remove entries that are single characters (corruption from string iteration)
            cleaned = [item for item in val if not isinstance(item, str) or len(item) > 1]
            if len(cleaned) != original_len:
                changes.append(f"  {key}: removed {original_len - len(cleaned)} single-char entries")
                state[key] = cleaned

    # 2. Remove *_delta keys
    for key in list(state.keys()):
        if key.endswith("_delta"):
            changes.append(f"  removed {key}: {state[key]}")
            del state[key]

    # 3. Remove *_add / *_remove helper keys (these shouldn't be persisted)
    for key in list(state.keys()):
        if key.endswith("_add") or key.endswith("_remove"):
            changes.append(f"  removed {key}: {state[key]}")
            del state[key]

    # 4. Remove system keys
    for key in SYSTEM_KEYS:
        if key in state:
            changes.append(f"  removed {key}: {state[key]}")
            del state[key]

    return state, changes


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== DRY RUN (no files will be modified) ===\n")

    total_files = 0
    total_changes = 0

    # Walk all story directories
    stories_dir = os.path.join(DATA_DIR, "stories")
    if not os.path.isdir(stories_dir):
        print(f"No stories directory found at {stories_dir}")
        return

    for story_id in os.listdir(stories_dir):
        story_dir = os.path.join(stories_dir, story_id)
        branches_dir = os.path.join(story_dir, "branches")
        if not os.path.isdir(branches_dir):
            continue

        for branch_id in os.listdir(branches_dir):
            branch_dir = os.path.join(branches_dir, branch_id)
            state_path = os.path.join(branch_dir, "character_state.json")
            if not os.path.isfile(state_path):
                continue

            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)

            state, changes = cleanup_state(state)
            if changes:
                total_files += 1
                total_changes += len(changes)
                print(f"{story_id}/{branch_id}:")
                for c in changes:
                    print(c)

                if not dry_run:
                    with open(state_path, "w", encoding="utf-8") as f:
                        json.dump(state, f, ensure_ascii=False, indent=2)
                    print("  -> SAVED")
                print()

    print(f"\nTotal: {total_files} files, {total_changes} changes"
          + (" (dry run)" if dry_run else ""))


if __name__ == "__main__":
    main()
