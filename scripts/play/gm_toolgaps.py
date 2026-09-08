#!/usr/bin/env python3
"""Tool-gap review: aggregate off-tool ("freehand") actions logged during play.

Reads data/stories/<story>/branches/<branch>/tool_gap_log.jsonl and prints a
ranked backlog of recurring actions that were done WITHOUT a gm_* tool — the
candidates to promote into gm_tools.py (which makes them available in both the
Bash and MCP front-ends at once).

High-frequency recurring/mechanical actions → toolify.
One-off / creative actions → leave as freehand (frequency is the filter).

Usage:
  python scripts/play/gm_toolgaps.py --story story_original --branch <bid>
"""
import argparse
import collections
import json
import os

import gm_tools


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--story", required=True)
    p.add_argument("--branch", required=True)
    a = p.parse_args()

    path = gm_tools._ledger_path(a.story, a.branch)
    if not os.path.exists(path):
        print("(no tool-gap log yet — nothing logged off-tool)")
        return 0

    counter = collections.Counter()
    examples: dict[str, dict] = {}
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            key = (rec.get("action") or "?").strip()
            counter[key] += 1
            examples.setdefault(key, rec)

    print(f"# Tool-gap backlog — {n} freehand actions logged\n")
    for action, count in counter.most_common():
        ex = examples[action]
        flag = "  ← toolify" if count >= 2 else ""
        print(f"{count:>3}×  {action}{flag}")
        if ex.get("why_no_tool"):
            print(f"      why: {ex['why_no_tool']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
