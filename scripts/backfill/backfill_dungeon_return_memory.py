#!/usr/bin/env python3
"""Backfill dungeon return memory and NPC recall provenance for an existing branch."""

from __future__ import annotations

import argparse
import json
import sys

from story_core.dungeon_return_memory import (
    backfill_dungeon_return_memory,
    backfill_npc_provenance,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--story-id", required=True, help="Story ID, e.g. story_original")
    parser.add_argument("--branch-id", required=True, help="Branch ID, e.g. branch_1234abcd")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show proposed dungeon return memory / NPC provenance without writing files.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    apply = not args.dry_run
    memory = backfill_dungeon_return_memory(args.story_id, args.branch_id, apply=apply)
    npc_result = backfill_npc_provenance(args.story_id, args.branch_id, apply=apply)

    payload = {
        "story_id": args.story_id,
        "branch_id": args.branch_id,
        "apply": apply,
        "dungeon_return_memory": memory,
        "npc_provenance": {
            "changed_npcs": npc_result["changed_npcs"],
            "filled_fields": npc_result["filled_fields"],
            "sample": npc_result["npcs"][:10],
        },
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
