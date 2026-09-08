#!/usr/bin/env python3
"""Post-narration commit half-turn: apply state + NPCs + messages + recap-check.

Reads ONE JSON payload from stdin (preferred) or --json:
  {
    "user_msg": "...", "gm_msg": "...",
    "dice": {...},                      # the dice block from gm_pre (attaches to player msg)
    "state_delta": {"reward_points_delta": 300, "current_status": "..."},
    "npcs": [{"name": "...", "lifecycle_status": "active", "memory_add": "..."}],
    "freehand": [{"action": "...", "why_no_tool": "...", "how_done": "..."}],
    "recap_text": "..."                 # optional, only when GM compacts this turn
  }

Usage:
  echo '<json>' | python scripts/play/gm_post.py --story story_original --branch <bid>
"""
import argparse
import json
import sys

import gm_tools


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--story", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--json", default=None, help="payload (else read stdin)")
    a = p.parse_args()
    raw = a.json if a.json is not None else sys.stdin.read()
    payload = json.loads(raw or "{}")
    out = gm_tools.post_turn(a.story, a.branch, payload)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
