#!/usr/bin/env python3
"""One-time cleanup script for world_lore.json data quality issues.

Fixes:
  B1. Bracket-wrapped categories: 【主神設定與規則】 → 主神設定與規則
  B2. Bracket-prefixed topics: 【商城】：消耗品 → 消耗品
  B3. Dash separators → colon: 基因鎖 - 第一階 → 基因鎖：第一階
  B4. Orphan grouping via known parent prefixes
  B5. Dedup (exact topic duplicates)
  B6. Category validation
  B7. Rebuild SQLite index

Usage:
  python scripts/cleanup_lore_data.py                    # dry-run
  python scripts/cleanup_lore_data.py --apply            # apply changes
  python scripts/cleanup_lore_data.py --apply --rebuild   # apply + rebuild index
"""

import json
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Use production data dir (worktree doesn't have gitignored data/)
PROD_DIR = os.environ.get("STORY_DIR", os.path.join(os.path.expanduser("~"), "story"))
sys.path.insert(0, PROD_DIR)

STORY_ID = os.environ.get("STORY_ID", "story_original")
LORE_PATH = os.path.join(PROD_DIR, "data", "stories", STORY_ID, "world_lore.json")

VALID_CATEGORIES = {
    "主神設定與規則", "體系", "商城", "副本世界觀",
    "場景", "NPC", "故事追蹤",
}

# Fuzzy remap for LLM-hallucinated categories
CATEGORY_REMAP = {
    "商城道具": "商城",
    "成長系統": "體系",
    "心靈屬性": "體系",
    "戰鬥系統": "體系",
    "戰鬥機制": "體系",
    "技能": "體系",
    "角色": "NPC",
    "人物": "NPC",
    "劇情": "故事追蹤",
    "主線": "故事追蹤",
    "伏筆": "故事追蹤",
    "設定": "主神設定與規則",
    "規則": "主神設定與規則",
    "副本": "副本世界觀",
    "世界觀": "副本世界觀",
}

# Known parent prefixes to group orphans under (category → {prefix: parent_topic})
# These are existing hierarchical parents where orphans can be attached
KNOWN_PARENTS = {
    "場景": {
        "主神空間": "主神空間設施",
        "浣熊市": "副本世界",
    },
}


def fix_category(category: str) -> str:
    """Strip brackets and validate category."""
    cat = category.strip().strip("【】").strip()
    if cat in VALID_CATEGORIES:
        return cat
    remapped = CATEGORY_REMAP.get(cat)
    if remapped:
        return remapped
    # Substring match
    for valid in VALID_CATEGORIES:
        if cat in valid or valid in cat:
            return valid
    return cat  # keep as-is, will be flagged


def fix_topic(topic: str) -> str:
    """Strip bracket prefixes and normalize separators."""
    # B2: Strip 【...】：prefix (e.g. "【商城】：消耗品" → "消耗品")
    topic = re.sub(r'^【[^】]*】[：:\s]*', '', topic).strip()

    # B3: Normalize " - " to "：" (but only for CJK context, not English dashes)
    # Pattern: CJK_text - CJK_text → CJK_text：CJK_text
    topic = re.sub(r'(?<=[\u4e00-\u9fff）\)]) - (?=[\u4e00-\u9fffA-Z])', '：', topic)

    return topic


def run_cleanup(apply: bool = False, rebuild: bool = False):
    with open(LORE_PATH, "r", encoding="utf-8") as f:
        lore = json.load(f)

    print(f"Loaded {len(lore)} entries from world_lore.json\n")

    changes = []
    fixed_entries = []

    for i, entry in enumerate(lore):
        orig_cat = entry.get("category", "")
        orig_topic = entry.get("topic", "")

        new_cat = fix_category(orig_cat)
        new_topic = fix_topic(orig_topic)

        changed = False
        change_desc = []

        if new_cat != orig_cat:
            change_desc.append(f"  cat: '{orig_cat}' → '{new_cat}'")
            changed = True

        if new_topic != orig_topic:
            change_desc.append(f"  topic: '{orig_topic}' → '{new_topic}'")
            changed = True

        if new_cat not in VALID_CATEGORIES:
            change_desc.append(f"  ⚠ INVALID CATEGORY: '{new_cat}'")

        if changed:
            changes.append((i, orig_topic, change_desc))

        fixed_entries.append({
            **entry,
            "category": new_cat,
            "topic": new_topic,
        })

    # Report changes
    print(f"=== CHANGES ({len(changes)}) ===")
    for idx, orig, descs in changes:
        print(f"[{idx}] {orig}")
        for d in descs:
            print(f"    {d}")
    print()

    # Dedup (B5): keep first occurrence of each topic
    seen_topics = {}
    deduped = []
    dup_count = 0
    for entry in fixed_entries:
        topic = entry["topic"]
        if topic in seen_topics:
            dup_count += 1
            print(f"DEDUP: removing duplicate '{topic}' (first at index {seen_topics[topic]})")
            continue
        seen_topics[topic] = len(deduped)
        deduped.append(entry)

    print(f"\nDedup: removed {dup_count} duplicates")
    print(f"Final count: {len(deduped)} entries (was {len(lore)})")

    # Category distribution
    from collections import Counter
    cats = Counter(e["category"] for e in deduped)
    print("\nCategory distribution:")
    for cat, count in cats.most_common():
        marker = "" if cat in VALID_CATEGORIES else " ⚠ INVALID"
        print(f"  {cat}: {count}{marker}")

    # Orphan count
    orphans = [e for e in deduped if "：" not in e["topic"]]
    hierarchical = [e for e in deduped if "：" in e["topic"]]
    print(f"\nHierarchical topics: {len(hierarchical)}")
    print(f"Flat/orphan topics: {len(orphans)}")

    if apply:
        # Backup
        backup_path = LORE_PATH + ".backup"
        if not os.path.exists(backup_path):
            import shutil
            shutil.copy2(LORE_PATH, backup_path)
            print(f"\nBackup saved to {backup_path}")

        with open(LORE_PATH, "w", encoding="utf-8") as f:
            json.dump(deduped, f, ensure_ascii=False, indent=2)
        print(f"✓ Saved {len(deduped)} entries to {LORE_PATH}")

        if rebuild:
            from lore_db import rebuild_index
            rebuild_index(STORY_ID)
            print("✓ Rebuilt SQLite lore index")
    else:
        print("\n(dry run — use --apply to save changes)")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    rebuild = "--rebuild" in sys.argv
    run_cleanup(apply=apply, rebuild=rebuild)
