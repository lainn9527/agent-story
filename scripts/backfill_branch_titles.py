#!/usr/bin/env python3
"""
Backfill branch titles for branches that don't have a `title` field.

For each branch without a title, takes the last 1-2 GM messages and calls
call_oneshot() to generate a 4-8 character Chinese action summary.

Usage:
    python scripts/backfill_branch_titles.py [--story STORY_ID] [--dry-run]
"""

import json
import logging
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from llm_bridge import call_oneshot

logging.basicConfig(
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("backfill_titles")

STORIES_DIR = os.path.join(PROJECT_ROOT, "data", "stories")


def _load_json(path, default=None):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else []


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def generate_title(gm_texts: list[str]) -> str:
    """Call LLM to generate a short branch title from GM messages."""
    combined = "\n\n---\n\n".join(gm_texts[-2:])  # last 1-2 messages
    prompt = (
        "用 4-8 個中文字總結以下 GM 回覆中**玩家的核心行動或場景轉折**。\n"
        "例如：「七首殺屍測試」「巷道右側突圍」「自省之眼覺醒」「進入蜀山副本」「商城兌換裝備」\n"
        "要求：動作導向、簡潔、不帶標點符號。只輸出標題文字，不要任何其他內容。\n\n"
        f"## GM 回覆\n{combined}"
    )
    result = call_oneshot(prompt)
    if result:
        title = result.strip().strip("「」\"'")[:20]
        return title
    return ""


def backfill_story(story_id: str, dry_run: bool = False):
    """Backfill titles for all branches in a story that don't have one."""
    story_dir = os.path.join(STORIES_DIR, story_id)
    tree_path = os.path.join(story_dir, "timeline_tree.json")
    tree = _load_json(tree_path, {})
    branches = tree.get("branches", {})

    if not branches:
        log.info("  No branches found for story %s", story_id)
        return 0

    branches_dir = os.path.join(story_dir, "branches")
    count = 0

    for bid, meta in branches.items():
        if meta.get("deleted") or meta.get("merged"):
            continue
        if meta.get("title"):
            continue  # already has a title

        # Load messages for this branch
        msgs_path = os.path.join(branches_dir, bid, "messages.json")
        msgs = _load_json(msgs_path, [])
        gm_msgs = [m.get("content", "") for m in msgs if m.get("role") == "gm" and m.get("content")]

        if not gm_msgs:
            # Try parsed_conversation for main-line branches
            parsed_path = os.path.join(story_dir, "parsed_conversation.json")
            parsed = _load_json(parsed_path, [])
            gm_msgs = [m.get("content", "") for m in parsed if m.get("role") == "gm" and m.get("content")]
            if not gm_msgs:
                log.info("  %s: no GM messages found, skipping", bid)
                continue

        if dry_run:
            log.info("  %s: would generate title from %d GM messages", bid, len(gm_msgs))
            count += 1
            continue

        log.info("  %s: generating title from %d GM messages...", bid, len(gm_msgs))
        title = generate_title(gm_msgs)
        if title:
            meta["title"] = title
            count += 1
            log.info("    → %s", title)
        else:
            log.warning("    → failed to generate title")

        # Rate limit
        time.sleep(1)

    if not dry_run and count > 0:
        _save_json(tree_path, tree)
        log.info("  Saved %d titles to timeline_tree.json", count)

    return count


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Backfill branch titles")
    parser.add_argument("--story", help="Story ID (default: all stories)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Branch Title Backfill")
    log.info("=" * 60)

    if args.story:
        story_ids = [args.story]
    else:
        story_ids = [d for d in os.listdir(STORIES_DIR) if os.path.isdir(os.path.join(STORIES_DIR, d))]

    total = 0
    for sid in story_ids:
        log.info("Story: %s", sid)
        total += backfill_story(sid, dry_run=args.dry_run)

    log.info("")
    log.info("Total titles %s: %d", "to generate" if args.dry_run else "generated", total)


if __name__ == "__main__":
    main()
