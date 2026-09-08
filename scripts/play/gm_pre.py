#!/usr/bin/env python3
"""Pre-narration half-turn: roll the fate die + retrieve lore in ONE call.

Usage:
  python scripts/play/gm_pre.py --story story_original --branch <bid> --action "我去搜屍體找工具"

Prints a small JSON {dice, lore} for the GM to write the next beat from.
"""
import argparse
import json

import gm_tools


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--story", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--action", required=True)
    p.add_argument("--lore-limit", type=int, default=3)
    a = p.parse_args()
    out = gm_tools.pre_turn(a.story, a.branch, a.action, lore_limit=a.lore_limit)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
