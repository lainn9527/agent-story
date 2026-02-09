#!/usr/bin/env python3
"""
One-time lore cleanup script: dedup fragmented entries, remap categories, split long entries.

Phases:
  0. Backup world_lore.json
  1. Exact dedup (keep first occurrence of each topic)
  2. Absorb micro-fragments into parent entries (content-similarity based)
  3. Category remap to canonical 7
  4. Split long entries (>2000 chars) via LLM
  5. Validate + save + rebuild SQLite index

Usage:
  python scripts/lore_cleanup.py --dry-run          # analyze only
  python scripts/lore_cleanup.py                    # full run
  python scripts/lore_cleanup.py --skip-split       # dedup + remap only, no LLM
"""

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("lore_cleanup")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CANONICAL_CATEGORIES = {
    "主神設定與規則", "體系", "商城", "副本世界觀", "場景", "NPC", "故事追蹤",
}

CATEGORY_REMAP = {
    "道具": "商城",
    "基本屬性": "體系",
    "副本背景": "副本世界觀",
    "世界設定": "主神設定與規則",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lore_path(story_id: str) -> str:
    return os.path.join(PROJECT_ROOT, "data", "stories", story_id, "world_lore.json")


def _load_lore(story_id: str) -> list[dict]:
    path = _lore_path(story_id)
    if not os.path.exists(path):
        log.error("world_lore.json not found at %s", path)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_lore(story_id: str, lore: list[dict]):
    path = _lore_path(story_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(lore, f, ensure_ascii=False, indent=2)


def _strip_tags(content: str) -> str:
    """Strip [tag: ...] and [source: ...] markers for comparison."""
    return re.sub(r"\s*\[(?:tag|source):\s*[^\]]*\]", "", content).strip()


# ---------------------------------------------------------------------------
# Phase 0: Backup
# ---------------------------------------------------------------------------

def backup(story_id: str) -> str:
    src = _lore_path(story_id)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = src.replace(".json", f".backup.{ts}.json")
    shutil.copy2(src, dst)
    log.info("Phase 0: Backup → %s", os.path.basename(dst))
    return dst


# ---------------------------------------------------------------------------
# Phase 1: Exact dedup by topic
# ---------------------------------------------------------------------------

def dedup_exact(lore: list[dict]) -> list[dict]:
    """Remove entries with identical topic, keeping first occurrence."""
    seen = set()
    result = []
    dupes = 0
    for entry in lore:
        topic = entry.get("topic", "").strip()
        if topic in seen:
            dupes += 1
            continue
        seen.add(topic)
        result.append(entry)
    log.info("Phase 1: Exact dedup — removed %d duplicates (%d → %d)", dupes, len(lore), len(result))
    return result


# ---------------------------------------------------------------------------
# Phase 2: Absorb micro-fragments into parent entries
# ---------------------------------------------------------------------------

def _find_parent_topic(topic: str, topic_set: set[str]) -> str | None:
    """Find a parent topic that this fragment belongs to.

    A fragment like '基因鎖開啟失敗與求生本能爆發' belongs to parent '基因鎖'.
    Match by: fragment topic starts with parent topic, parent topic is shorter.
    """
    # Also check colon-based parent: "基因鎖：xxx" → parent "基因鎖"
    if "：" in topic:
        parent_candidate = topic.split("：")[0].strip()
        if parent_candidate in topic_set and parent_candidate != topic:
            return parent_candidate

    best = None
    best_len = 0
    for candidate in topic_set:
        if candidate == topic:
            continue
        # Fragment topic starts with parent topic
        if topic.startswith(candidate) and len(candidate) > best_len:
            best = candidate
            best_len = len(candidate)
    return best


def absorb_fragments(lore: list[dict]) -> list[dict]:
    """Absorb short fragment entries into their parent topic entries.

    Fragment = entry whose topic starts with a parent topic AND content < 300 chars.
    The fragment's content is appended to the parent entry.
    """
    # Build topic → entry index
    topic_to_idx = {}
    for i, entry in enumerate(lore):
        topic = entry.get("topic", "").strip()
        topic_to_idx[topic] = i

    topic_set = set(topic_to_idx.keys())
    absorbed = set()  # indices to remove

    for i, entry in enumerate(lore):
        topic = entry.get("topic", "").strip()
        content = entry.get("content", "")
        content_len = len(_strip_tags(content))

        # Only absorb short fragments (< 300 chars content)
        if content_len >= 300:
            continue

        parent_topic = _find_parent_topic(topic, topic_set)
        if parent_topic is None:
            continue

        parent_idx = topic_to_idx[parent_topic]
        if parent_idx == i:
            continue

        # Don't absorb into an already-absorbed entry
        if parent_idx in absorbed:
            continue

        # Append fragment content to parent
        parent_entry = lore[parent_idx]
        fragment_label = topic.replace(parent_topic, "").lstrip("：:／/— ")
        if fragment_label:
            addition = f"\n\n【{fragment_label}】\n{_strip_tags(content)}"
        else:
            addition = f"\n\n{_strip_tags(content)}"

        parent_entry["content"] = parent_entry.get("content", "") + addition
        absorbed.add(i)

    result = [e for i, e in enumerate(lore) if i not in absorbed]
    log.info("Phase 2: Absorbed %d micro-fragments into parent entries (%d → %d)",
             len(absorbed), len(lore), len(result))
    return result


# ---------------------------------------------------------------------------
# Phase 3: Category remap
# ---------------------------------------------------------------------------

def remap_categories(lore: list[dict]) -> list[dict]:
    remapped = 0
    for entry in lore:
        cat = entry.get("category", "")
        if cat in CATEGORY_REMAP:
            entry["category"] = CATEGORY_REMAP[cat]
            remapped += 1
    log.info("Phase 3: Remapped %d entries to canonical categories", remapped)
    return lore


# ---------------------------------------------------------------------------
# Phase 4: Split long entries via LLM
# ---------------------------------------------------------------------------

def _build_split_prompt(entry: dict) -> str:
    return (
        "你是世界設定知識庫的編輯。以下條目過長，請拆分為多個子條目。\n\n"
        "規則：\n"
        "1. 每個子條目 content 200-800 字\n"
        "2. topic 格式：「{parent}：{sub}」（parent 是原始 topic）\n"
        "3. 第一個子條目是概述/總覽\n"
        "4. category 不變，保持 [tag:] [source:] 標記\n"
        "5. 不要遺漏任何內容\n\n"
        f"原始條目：\n"
        f"category: {entry['category']}\n"
        f"topic: {entry['topic']}\n"
        f"content:\n{entry['content']}\n\n"
        "輸出 JSON 陣列：[{\"category\": \"...\", \"topic\": \"...\", \"content\": \"...\"}]\n"
        "只輸出 JSON。"
    )


def _build_chunk_split_prompt(entry: dict, chunk: str, chunk_idx: int, total_chunks: int) -> str:
    return (
        "你是世界設定知識庫的編輯。以下是一個過長條目的其中一段，請拆分為子條目。\n\n"
        "規則：\n"
        "1. 每個子條目 content 200-800 字\n"
        f"2. topic 格式：「{entry['topic']}：{{sub_topic}}」\n"
        "3. category 不變，保持 [tag:] [source:] 標記\n"
        "4. 不要遺漏任何內容\n\n"
        f"原始條目 topic: {entry['topic']}\n"
        f"原始條目 category: {entry['category']}\n"
        f"（第 {chunk_idx}/{total_chunks} 段）\n\n"
        f"content:\n{chunk}\n\n"
        "輸出 JSON 陣列：[{\"category\": \"...\", \"topic\": \"...\", \"content\": \"...\"}]\n"
        "只輸出 JSON。"
    )


def _chunk_by_sections(content: str, max_chunk: int = 3000) -> list[str]:
    """Split content at 【...】 boundaries into chunks of roughly max_chunk chars."""
    sections = re.split(r"(?=【)", content)
    chunks = []
    current = ""
    for section in sections:
        if len(current) + len(section) > max_chunk and current:
            chunks.append(current.strip())
            current = section
        else:
            current += section
    if current.strip():
        chunks.append(current.strip())
    return chunks if chunks else [content]


def _parse_json_array(text: str) -> list[dict]:
    """Parse a JSON array from LLM response, handling markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # Fallback: find JSON array (greedy — json.loads validates correctness)
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            data = json.loads(m.group())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    return []


def split_long_entries(lore: list[dict], delay: float = 3.0, dry_run: bool = False) -> list[dict]:
    """Split entries >2000 chars via LLM calls."""
    from llm_bridge import call_oneshot

    long_entries = [(i, e) for i, e in enumerate(lore) if len(e.get("content", "")) > 2000]
    if not long_entries:
        log.info("Phase 4: No entries >2000 chars to split")
        return lore

    log.info("Phase 4: %d entries >2000 chars to split%s",
             len(long_entries), " (DRY RUN)" if dry_run else "")

    if dry_run:
        for i, entry in long_entries:
            log.info("  Would split: [%d chars] %s", len(entry["content"]), entry["topic"])
        return lore

    # Build replacement map: original index → list of replacement entries
    replacements: dict[int, list[dict]] = {}

    for idx, (orig_idx, entry) in enumerate(long_entries):
        topic = entry.get("topic", "")
        content = entry.get("content", "")
        content_len = len(content)

        log.info("  Splitting [%d/%d]: [%d chars] %s",
                 idx + 1, len(long_entries), content_len, topic)

        sub_entries = []

        # Always pre-chunk to avoid LLM timeout on long prompts
        chunks = _chunk_by_sections(content, max_chunk=2000)
        if len(chunks) > 1:
            log.info("    Pre-chunked into %d segments", len(chunks))

        for ci, chunk in enumerate(chunks, 1):
            if len(chunks) == 1:
                prompt = _build_split_prompt(entry)
            else:
                prompt = _build_chunk_split_prompt(entry, chunk, ci, len(chunks))
            result = call_oneshot(prompt)
            if result:
                parsed = _parse_json_array(result)
                if parsed:
                    sub_entries.extend(parsed)
                    log.info("    Chunk %d → %d sub-entries", ci, len(parsed))
                else:
                    log.warning("    Chunk %d: LLM returned non-parseable response, aborting split", ci)
                    sub_entries = []  # discard partial results to avoid data loss
                    break
            else:
                log.warning("    Chunk %d: empty LLM response, aborting split", ci)
                sub_entries = []  # discard partial results to avoid data loss
                break
            if ci < len(chunks):
                time.sleep(delay)

        if sub_entries:
            # Validate sub-entries have required fields
            valid = []
            for se in sub_entries:
                if isinstance(se, dict) and se.get("topic") and se.get("content"):
                    if not se.get("category"):
                        se["category"] = entry.get("category", "")
                    valid.append(se)
            if valid:
                replacements[orig_idx] = valid
                log.info("    Split into %d valid sub-entries", len(valid))
            else:
                log.warning("    No valid sub-entries, keeping original")
        else:
            log.warning("    No sub-entries produced, keeping original")

        if idx < len(long_entries) - 1:
            time.sleep(delay)

    # Rebuild lore list with replacements
    result = []
    for i, entry in enumerate(lore):
        if i in replacements:
            result.extend(replacements[i])
        else:
            result.append(entry)

    total_new = sum(len(v) for v in replacements.values())
    total_replaced = len(replacements)
    log.info("  Split complete: %d entries replaced → %d sub-entries", total_replaced, total_new)
    return result


# ---------------------------------------------------------------------------
# Phase 5: Validate + Save + Rebuild
# ---------------------------------------------------------------------------

def validate_and_save(story_id: str, lore: list[dict], dry_run: bool = False):
    """Validate, save, and rebuild index."""
    # Dedup one more time (split may have introduced topic collisions)
    seen_topics = set()
    deduped = []
    for entry in lore:
        topic = entry.get("topic", "").strip()
        if not topic:
            log.warning("  Skipping entry with empty topic")
            continue
        if topic in seen_topics:
            log.info("  Removing post-split duplicate: %s", topic)
            continue
        seen_topics.add(topic)
        deduped.append(entry)

    # Validate categories
    non_canonical = 0
    for entry in deduped:
        cat = entry.get("category", "")
        if cat not in CANONICAL_CATEGORIES:
            log.warning("  Non-canonical category: '%s' on topic '%s'", cat, entry.get("topic"))
            non_canonical += 1

    # Warn about still-long entries
    still_long = [(e["topic"], len(e["content"])) for e in deduped if len(e.get("content", "")) > 1500]
    if still_long:
        log.info("  Entries still >1500 chars after cleanup:")
        for topic, clen in still_long:
            log.info("    [%d chars] %s", clen, topic)

    # Stats
    from collections import Counter
    cats = Counter(e.get("category", "") for e in deduped)
    log.info("Phase 5: Final stats — %d entries", len(deduped))
    for cat, count in cats.most_common():
        log.info("  %s: %d", cat, count)

    if dry_run:
        log.info("  DRY RUN — no files modified")
        return

    _save_lore(story_id, deduped)
    log.info("  Saved world_lore.json (%d entries)", len(deduped))

    from lore_db import rebuild_index
    rebuild_index(story_id)
    log.info("  Rebuilt SQLite FTS index")


# ---------------------------------------------------------------------------
# Phase 6: Organize orphan topics (rule-based + LLM)
# ---------------------------------------------------------------------------

def organize_orphans(story_id: str, delay: float = 3.0, dry_run: bool = False):
    """Organize orphan topics (no `：`) into hierarchical prefixed form.

    Phase 1: Rule-based matching (starts-with / exact prefix match)
    Phase 2: LLM classification (closed-option, batched)
    """
    from lore_organizer import (
        build_prefix_registry, try_classify_topic, find_orphans,
        get_lore_lock, rename_lore_topic, invalidate_prefix_cache,
    )
    from llm_bridge import call_oneshot

    orphans = find_orphans(story_id)
    log.info("Phase 6: Organize orphans — %d orphan topics found", len(orphans))
    if not orphans:
        return

    registry = build_prefix_registry(story_id)

    # Phase 6a: Rule-based
    rule_classified = []
    remaining = []
    for orphan in orphans:
        topic = orphan.get("topic", "")
        category = orphan.get("category", "")
        new_topic = try_classify_topic(topic, category, story_id, prefix_registry=registry)
        if new_topic:
            rule_classified.append((topic, new_topic))
        else:
            remaining.append(orphan)

    log.info("  Phase 6a (rules): %d classified, %d remaining", len(rule_classified), len(remaining))
    for old, new in rule_classified:
        log.info("    %s → %s", old, new)

    if not dry_run and rule_classified:
        lock = get_lore_lock(story_id)
        for old, new in rule_classified:
            with lock:
                rename_lore_topic(story_id, old, new)
        invalidate_prefix_cache(story_id)
        registry = build_prefix_registry(story_id)

    # Phase 6b: LLM classification
    if not remaining:
        log.info("  Phase 6b (LLM): no remaining orphans")
        return

    # Build prefix list by category for LLM prompt
    prefix_lines = []
    for cat, prefixes in sorted(registry.get("by_category", {}).items()):
        if prefixes:
            prefix_lines.append(f"[{cat}] {', '.join(sorted(prefixes))}")

    if not prefix_lines:
        log.info("  Phase 6b (LLM): no prefixes available, skipping")
        return

    all_prefixes = registry.get("all", set())
    llm_classified = []
    skipped = []

    # Process in batches of 20
    for batch_start in range(0, len(remaining), 20):
        batch = remaining[batch_start:batch_start + 20]
        batch_num = batch_start // 20 + 1
        total_batches = (len(remaining) + 19) // 20

        orphan_lines = []
        for i, o in enumerate(batch, 1):
            orphan_lines.append(f"{i}. [{o.get('category', '')}] {o.get('topic', '')}")

        prompt = (
            "你是分類工具。將以下 orphan 主題歸入已知前綴，或回答 \"none\"。\n"
            "注意：即使主題名稱不以前綴開頭，只要語意上屬於該前綴的範疇就應歸入。\n\n"
            "可用前綴（按分類）：\n"
            f"{chr(10).join(prefix_lines)}\n\n"
            "待分類：\n"
            f"{chr(10).join(orphan_lines)}\n\n"
            '只輸出 JSON 陣列：[{"topic": "原始主題", "prefix": "匹配的前綴" 或 "none"}]\n'
            "只輸出 JSON。"
        )

        log.info("  Phase 6b (LLM): batch %d/%d (%d orphans)", batch_num, total_batches, len(batch))

        if dry_run:
            for o in batch:
                log.info("    Would classify via LLM: [%s] %s", o.get("category", ""), o.get("topic", ""))
            continue

        result = call_oneshot(prompt)
        if not result:
            log.warning("    LLM returned empty response, skipping batch")
            skipped.extend(o.get("topic", "") for o in batch)
            continue

        result = result.strip()
        if result.startswith("```"):
            lines = result.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            result = "\n".join(lines)

        try:
            classifications = json.loads(result)
        except json.JSONDecodeError:
            m = re.search(r'\[[\s\S]*\]', result)
            if not m:
                log.warning("    LLM response not parseable, skipping batch")
                skipped.extend(o.get("topic", "") for o in batch)
                continue
            try:
                classifications = json.loads(m.group())
            except json.JSONDecodeError:
                log.warning("    LLM JSON parse failed, skipping batch")
                skipped.extend(o.get("topic", "") for o in batch)
                continue

        if not isinstance(classifications, list):
            log.warning("    LLM returned non-list, skipping batch")
            skipped.extend(o.get("topic", "") for o in batch)
            continue

        orphan_cats = {o.get("topic", ""): o.get("category", "") for o in batch}
        lock = get_lore_lock(story_id)

        for item in classifications:
            if not isinstance(item, dict):
                continue
            topic = item.get("topic", "").strip()
            prefix = item.get("prefix", "").strip()

            if not topic or topic not in orphan_cats:
                continue

            if prefix == "none" or not prefix:
                skipped.append(topic)
                log.info("    LLM: '%s' → none (skipped)", topic)
                continue

            # Validation: prefix must exist in known prefixes
            orphan_cat = orphan_cats[topic]
            cat_prefixes = registry.get("by_category", {}).get(orphan_cat, set())
            if prefix not in cat_prefixes and prefix not in all_prefixes:
                log.info("    LLM: invalid prefix '%s' for '%s', skipping", prefix, topic)
                skipped.append(topic)
                continue

            new_topic = f"{prefix}：{topic}"
            with lock:
                rename_lore_topic(story_id, topic, new_topic)
            llm_classified.append((topic, new_topic))
            log.info("    LLM: %s → %s", topic, new_topic)

        if batch_start + 20 < len(remaining):
            time.sleep(delay)

    log.info("  Phase 6b (LLM): %d classified, %d skipped", len(llm_classified), len(skipped))

    if not dry_run and llm_classified:
        invalidate_prefix_cache(story_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Lore cleanup: dedup + split long entries + organize orphans")
    parser.add_argument("--story-id", default="story_original", help="Story ID (default: story_original)")
    parser.add_argument("--dry-run", action="store_true", help="Analyze only, no mutations")
    parser.add_argument("--skip-split", action="store_true", help="Dedup + remap only, no LLM calls")
    parser.add_argument("--organize-orphans", action="store_true", help="Organize orphan topics (rule-based + LLM)")
    parser.add_argument("--apply", action="store_true", help="Apply changes (with --organize-orphans, default is dry-run)")
    parser.add_argument("--delay", type=float, default=3.0, help="Delay between LLM calls (default: 3s)")
    args = parser.parse_args()

    story_id = args.story_id

    if args.organize_orphans:
        # Organize-orphans mode: focus on orphan topic classification
        effective_dry_run = not args.apply
        log.info("=" * 60)
        log.info("Lore Cleanup: Organize Orphan Topics")
        log.info("Story: %s  |  dry_run=%s", story_id, effective_dry_run)
        log.info("=" * 60)

        lore = _load_lore(story_id)
        orphan_count = sum(1 for e in lore if "：" not in e.get("topic", ""))
        log.info("Loaded %d entries (%d orphans)", len(lore), orphan_count)

        if not effective_dry_run:
            backup(story_id)

        organize_orphans(story_id, delay=args.delay, dry_run=effective_dry_run)

        if not effective_dry_run:
            # Rebuild SQLite index after renames
            from lore_db import rebuild_index
            rebuild_index(story_id)
            log.info("Rebuilt SQLite FTS index")

        # Final stats
        lore = _load_lore(story_id)
        final_orphans = sum(1 for e in lore if "：" not in e.get("topic", ""))
        log.info("Final: %d entries, %d orphans (was %d)", len(lore), final_orphans, orphan_count)

        log.info("=" * 60)
        log.info("Done!")
        log.info("=" * 60)
        return

    log.info("=" * 60)
    log.info("Lore Cleanup: Dedup + Split Long Entries")
    log.info("Story: %s  |  dry_run=%s  |  skip_split=%s", story_id, args.dry_run, args.skip_split)
    log.info("=" * 60)

    lore = _load_lore(story_id)
    log.info("Loaded %d entries", len(lore))

    # Phase 0: Backup
    if not args.dry_run:
        backup(story_id)

    # Phase 1: Exact dedup
    lore = dedup_exact(lore)

    # Phase 2: Absorb micro-fragments
    lore = absorb_fragments(lore)

    # Phase 3: Category remap
    lore = remap_categories(lore)

    # Phase 4: Split long entries
    if not args.skip_split:
        lore = split_long_entries(lore, delay=args.delay, dry_run=args.dry_run)
    else:
        long_count = sum(1 for e in lore if len(e.get("content", "")) > 2000)
        log.info("Phase 4: Skipped (--skip-split). %d entries still >2000 chars.", long_count)

    # Phase 5: Validate + save + rebuild
    validate_and_save(story_id, lore, dry_run=args.dry_run)

    log.info("=" * 60)
    log.info("Done!")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
