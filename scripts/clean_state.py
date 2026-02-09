#!/usr/bin/env python3
"""Clean legacy garbage fields from character_state.json files.

Removes:
- *_delta fields (should have been applied and discarded)
- *_add fields (temporary LLM artifacts)
- world_day (belongs in world_day.json, not character state)
- Known garbage keys that are not in the character schema

Usage:
    python scripts/clean_state.py                    # dry-run (default)
    python scripts/clean_state.py --apply            # actually write changes
    python scripts/clean_state.py --story story_id   # specific story only
"""
import json
import os
import sys
import glob

BASE = os.path.join(os.path.dirname(__file__), "..", "data", "stories")

# Keys that belong in character_state (from schema + known valid extras)
VALID_KEYS = {
    "name", "current_phase", "current_status",
    "gene_lock", "physique", "spirit", "reward_points",
    "inventory", "completed_missions", "relationships",
    "skills", "abilities", "traits",
}


def is_garbage(key, value):
    """Return True if this key should be removed."""
    # Delta fields — should have been applied
    if key.endswith("_delta"):
        return True
    # Add fields — temporary LLM artifacts
    if key.endswith("_add"):
        return True
    # world_day doesn't belong in character state
    if key == "world_day":
        return True
    return False


def clean_state(state):
    """Return (cleaned_state, removed_keys)."""
    removed = {}
    cleaned = {}
    for k, v in state.items():
        if is_garbage(k, v):
            removed[k] = v
        else:
            cleaned[k] = v
    return cleaned, removed


def main():
    apply = "--apply" in sys.argv
    story_filter = None
    if "--story" in sys.argv:
        idx = sys.argv.index("--story")
        story_filter = sys.argv[idx + 1]

    pattern = os.path.join(BASE, "*", "branches", "*", "character_state.json")
    files = glob.glob(pattern)

    # Also check legacy flat files
    legacy_pattern = os.path.join(BASE, "*", "character_state_*.json")
    files += glob.glob(legacy_pattern)

    total_removed = 0
    for path in sorted(files):
        if story_filter and story_filter not in path:
            continue

        with open(path) as f:
            state = json.load(f)

        cleaned, removed = clean_state(state)
        if not removed:
            continue

        total_removed += len(removed)
        print(f"\n{path}")
        for k, v in removed.items():
            val_str = str(v)[:60]
            print(f"  - {k}: {val_str}")

        if apply:
            with open(path, "w") as f:
                json.dump(cleaned, f, ensure_ascii=False, indent=2)
            print(f"  => CLEANED ({len(removed)} keys removed)")

    print(f"\n{'=' * 40}")
    print(f"Total garbage keys found: {total_removed}")
    if not apply:
        print("Dry run — use --apply to write changes")
    else:
        print("Changes applied!")


if __name__ == "__main__":
    main()
