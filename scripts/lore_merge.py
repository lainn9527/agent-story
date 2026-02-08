#!/usr/bin/env python3
"""
Lore semantic merge: group similar entries by topic stem + content similarity,
merge via parallel LLM calls.

Phases:
  0. Backup world_lore.json
  1. Stem grouping — split topic on "：", group by stem within category
  2. Split large groups — sort by topic, chunk into ≤10 entries
  3. Content similarity — bigram overlap to catch singletons with similar content
  4. Parallel LLM merge — ThreadPoolExecutor, each group → 1-3 consolidated entries
  5. Validate + save + rebuild SQLite index

Usage:
  python scripts/lore_merge.py --dry-run          # analyze groups only
  python scripts/lore_merge.py                    # full run
  python scripts/lore_merge.py --workers 6        # more parallelism
"""

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("lore_merge")

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

# Bigram similarity threshold for content-based grouping
SIMILARITY_THRESHOLD = 0.30

# Max entries per LLM call
MAX_GROUP_SIZE = 10

# Max chars for a merged entry (LLM target)
MERGE_TARGET_MAX = 800
MERGE_TARGET_MIN = 200


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


def _cjk_bigrams(text: str) -> set[str]:
    """Extract CJK bigrams from text."""
    cjk_runs = re.findall(r'[\u4e00-\u9fff]+', text)
    bigrams = set()
    for run in cjk_runs:
        for i in range(len(run) - 1):
            bigrams.add(run[i:i + 2])
    return bigrams


def _bigram_similarity(a: str, b: str) -> float:
    """Jaccard similarity of CJK bigrams."""
    ba = _cjk_bigrams(a)
    bb = _cjk_bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def _get_topic_stem(topic: str) -> str:
    """Extract topic stem — the part before first '：' or first 4 CJK chars."""
    if "：" in topic:
        return topic.split("：")[0]
    # For topics without colon, use leading CJK run
    m = re.match(r'[\u4e00-\u9fff]+', topic)
    if m:
        run = m.group()
        # Use up to 4 CJK chars as stem, but at least 3
        return run[:max(4, min(len(run), 6))]
    return topic


def _common_cjk_prefix_len(a: str, b: str) -> int:
    """Length of longest common CJK prefix."""
    ma = re.match(r'[\u4e00-\u9fff]+', a)
    mb = re.match(r'[\u4e00-\u9fff]+', b)
    if not ma or not mb:
        return 0
    ca, cb = ma.group(), mb.group()
    n = 0
    for x, y in zip(ca, cb):
        if x == y:
            n += 1
        else:
            break
    return n


def _total_content_chars(entries: list[dict]) -> int:
    return sum(len(e.get("content", "")) for e in entries)


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
# Phase 1-3: Grouping
# ---------------------------------------------------------------------------

def _split_large_group(entries: list[dict], max_size: int = MAX_GROUP_SIZE) -> list[list[dict]]:
    """Split a large group into sub-groups by finding natural topic boundaries.

    Sort by topic, then try to sub-group by longer shared prefix.
    Falls back to chunking if no natural split point found.
    """
    if len(entries) <= max_size:
        return [entries]

    # Sort by topic for locality
    entries.sort(key=lambda e: e.get("topic", ""))

    # Try to find natural sub-groups: consecutive entries sharing a longer prefix
    # Compute pairwise prefix lengths between consecutive entries
    topics = [e.get("topic", "") for e in entries]
    prefix_lens = []
    for i in range(len(topics) - 1):
        prefix_lens.append(_common_cjk_prefix_len(topics[i], topics[i + 1]))

    if not prefix_lens:
        # Fallback: chunk evenly
        return [entries[i:i + max_size] for i in range(0, len(entries), max_size)]

    # Find split points where prefix length drops (boundary between sub-topics)
    median_prefix = sorted(prefix_lens)[len(prefix_lens) // 2]
    # Split at points where prefix is below median (topic boundary)
    split_threshold = max(median_prefix, 3)

    sub_groups = []
    current = [entries[0]]
    for i in range(len(prefix_lens)):
        if prefix_lens[i] < split_threshold and len(current) >= 2:
            sub_groups.append(current)
            current = [entries[i + 1]]
        else:
            current.append(entries[i + 1])
    if current:
        sub_groups.append(current)

    # If any sub-group is still too large, chunk it
    result = []
    for sg in sub_groups:
        if len(sg) > max_size:
            result.extend([sg[i:i + max_size] for i in range(0, len(sg), max_size)])
        else:
            result.append(sg)

    return result


def group_entries(entries: list[dict]) -> tuple[list[list[dict]], list[dict]]:
    """Group entries by stem + content similarity. Returns (groups, singletons)."""

    # Index by category
    by_category: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_category[e.get("category", "其他")].append(e)

    all_groups = []
    all_singletons = []

    for cat, cat_entries in by_category.items():
        # Phase 1: Stem grouping
        stem_groups: dict[str, list[dict]] = defaultdict(list)
        for e in cat_entries:
            stem = _get_topic_stem(e.get("topic", ""))
            stem_groups[stem].append(e)

        # Phase 2: Split large groups
        groups_in_cat = []
        singletons_in_cat = []

        for stem, group in stem_groups.items():
            if len(group) >= 2:
                sub_groups = _split_large_group(group)
                for sg in sub_groups:
                    if len(sg) >= 2:
                        groups_in_cat.append(sg)
                    else:
                        singletons_in_cat.append(sg[0])
            else:
                singletons_in_cat.append(group[0])

        # Phase 3: Content similarity for singletons
        # Try to pair singletons that have high content overlap
        if len(singletons_in_cat) >= 2:
            n = len(singletons_in_cat)
            parent = list(range(n))

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union(x, y):
                px, py = find(x), find(y)
                if px != py:
                    parent[px] = py

            # Pre-compute text for similarity
            texts = []
            for e in singletons_in_cat:
                texts.append(e.get("topic", "") + " " + e.get("content", "")[:300])

            for i in range(n):
                for j in range(i + 1, n):
                    if find(i) == find(j):
                        continue
                    sim = _bigram_similarity(texts[i], texts[j])
                    if sim >= SIMILARITY_THRESHOLD:
                        union(i, j)

            # Build clusters
            clusters: dict[int, list[int]] = defaultdict(list)
            for i in range(n):
                clusters[find(i)].append(i)

            for root, idxs in clusters.items():
                cluster = [singletons_in_cat[i] for i in idxs]
                if len(cluster) >= 2:
                    # Split if too large
                    for sg in _split_large_group(cluster):
                        if len(sg) >= 2:
                            groups_in_cat.append(sg)
                        else:
                            all_singletons.append(sg[0])
                else:
                    all_singletons.append(cluster[0])
        else:
            all_singletons.extend(singletons_in_cat)

        all_groups.extend(groups_in_cat)

    return all_groups, all_singletons


# ---------------------------------------------------------------------------
# Phase 4: LLM Merge
# ---------------------------------------------------------------------------

def _build_merge_prompt(group: list[dict]) -> str:
    """Build prompt for merging a group of entries."""
    category = group[0].get("category", "其他")
    topics = [e.get("topic", "") for e in group]
    parent_topic = min(topics, key=len)

    entries_text = []
    for e in group:
        entries_text.append(f"### {e.get('topic', '')}\n{e.get('content', '')}")
    all_entries = "\n\n".join(entries_text)

    total_chars = sum(len(e.get("content", "")) for e in group)
    if total_chars <= 1200:
        target_count = "1個"
    elif total_chars <= 3000:
        target_count = "1-2個"
    else:
        target_count = "2-4個"

    return (
        "你是世界設定知識庫的編輯。以下是同一主題的多個重複/碎片條目，請合併為精簡的條目。\n\n"
        "規則：\n"
        f"1. 合併為 {target_count} 條目，每個 content {MERGE_TARGET_MIN}-{MERGE_TARGET_MAX} 字\n"
        f"2. category 統一為「{category}」\n"
        f"3. 主 topic 用「{parent_topic}」，子主題用「{parent_topic}：子標題」\n"
        "4. 去除重複內容，保留所有獨特資訊\n"
        "5. 保持 [tag: ...] [source: ...] 標記\n"
        "6. 不要遺漏任何具體數據（數值、等級、門檻等）\n\n"
        f"共 {len(group)} 個條目（{total_chars} 字）：\n\n"
        f"{all_entries}\n\n"
        "輸出 JSON 陣列：[{\"category\": \"...\", \"topic\": \"...\", \"content\": \"...\"}]\n"
        "只輸出 JSON。"
    )


def _parse_json_array(text: str) -> list[dict] | None:
    """Parse a JSON array from LLM response, handling markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(lines).strip()

    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
    except json.JSONDecodeError:
        pass

    m = re.search(r'\[.*\]', text, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return None


def _merge_one_group(group_idx: int, group: list[dict], delay: float, total: int) -> tuple[int, list[dict] | None, str]:
    """Merge one group via LLM. Returns (group_idx, merged_entries, error_msg)."""
    from llm_bridge import call_oneshot

    if delay > 0:
        time.sleep(delay * (group_idx % 4))  # stagger start times

    prompt = _build_merge_prompt(group)
    topics = [e.get("topic", "") for e in group]
    parent_topic = min(topics, key=len)

    try:
        result = call_oneshot(prompt)
        if not result:
            return (group_idx, None, f"empty LLM response for {parent_topic}")

        parsed = _parse_json_array(result)
        if not parsed:
            return (group_idx, None, f"unparseable response for {parent_topic}")

        valid = []
        category = group[0].get("category", "其他")
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            entry.setdefault("category", category)
            topic = entry.get("topic", "").strip()
            content = entry.get("content", "").strip()
            if topic and content:
                entry["topic"] = topic
                entry["content"] = content
                valid.append(entry)

        if not valid:
            return (group_idx, None, f"no valid entries from merge of {parent_topic}")

        return (group_idx, valid, "")

    except Exception as e:
        return (group_idx, None, f"error merging {parent_topic}: {e}")


def merge_groups_parallel(
    groups: list[list[dict]],
    workers: int = 4,
    delay: float = 1.0,
) -> list[dict]:
    """Merge all groups in parallel. Returns list of merged entries."""
    merged_all = [None] * len(groups)
    failed = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for i, group in enumerate(groups):
            f = executor.submit(_merge_one_group, i, group, delay, len(groups))
            futures[f] = i

        for f in as_completed(futures):
            idx, merged, err = f.result()
            group = groups[idx]
            parent = min((e.get("topic", "") for e in group), key=len)
            if merged:
                log.info("  ✓ [%d/%d] %s: %d entries → %d merged",
                         idx + 1, len(groups), parent, len(group), len(merged))
                merged_all[idx] = merged
            else:
                log.warning("  ✗ [%d/%d] %s: FAILED — %s (keeping originals)",
                            idx + 1, len(groups), parent, err)
                failed.append(idx)
                merged_all[idx] = group  # keep originals on failure

    result = []
    for entries in merged_all:
        if entries:
            result.extend(entries)

    log.info("  Merged %d groups (%d failed, kept originals)", len(groups), len(failed))
    return result


# ---------------------------------------------------------------------------
# Phase 5: Validate + Save
# ---------------------------------------------------------------------------

def validate_and_save(
    story_id: str,
    entries: list[dict],
    dry_run: bool = False,
):
    """Validate entries and save to world_lore.json + rebuild index."""
    # Dedup by topic (keep first)
    seen_topics = set()
    deduped = []
    for e in entries:
        topic = e.get("topic", "").strip()
        if topic and topic not in seen_topics:
            seen_topics.add(topic)
            deduped.append(e)

    # Category cleanup
    for e in deduped:
        cat = e.get("category", "其他")
        if cat in CATEGORY_REMAP:
            e["category"] = CATEGORY_REMAP[cat]
        elif cat not in CANONICAL_CATEGORIES:
            e["category"] = "主神設定與規則"

    # Stats
    cats = defaultdict(int)
    for e in deduped:
        cats[e.get("category", "其他")] += 1
    long_entries = [e for e in deduped if len(e.get("content", "")) > MERGE_TARGET_MAX]

    log.info("Phase 5: Final stats — %d entries", len(deduped))
    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        log.info("  %s: %d", cat, count)
    if long_entries:
        log.info("  Entries >%d chars: %d", MERGE_TARGET_MAX, len(long_entries))
        for e in long_entries[:10]:
            log.info("    [%d chars] %s", len(e.get("content", "")), e.get("topic", ""))

    if dry_run:
        log.info("  [DRY RUN] Would save %d entries", len(deduped))
        return deduped

    # Save
    path = _lore_path(story_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(deduped, f, ensure_ascii=False, indent=2)
    log.info("  Saved world_lore.json (%d entries)", len(deduped))

    # Rebuild index
    from lore_db import rebuild_index
    rebuild_index(story_id)
    log.info("  Rebuilt SQLite FTS index")
    return deduped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Lore semantic merge")
    parser.add_argument("--story-id", default="story_original")
    parser.add_argument("--dry-run", action="store_true", help="Analyze groups only")
    parser.add_argument("--workers", type=int, default=4, help="Parallel LLM workers (default: 4)")
    parser.add_argument("--delay", type=float, default=1.0, help="Stagger delay between workers")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Lore Semantic Merge — story: %s", args.story_id)
    log.info("=" * 60)

    entries = _load_lore(args.story_id)
    log.info("Loaded %d entries (%d total chars)",
             len(entries), sum(len(e.get("content", "")) for e in entries))

    # Phase 0: Backup
    if not args.dry_run:
        backup(args.story_id)

    # Phase 1-3: Group
    log.info("Phase 1-3: Grouping by stem + content similarity...")
    groups, singletons = group_entries(entries)

    total_grouped = sum(len(g) for g in groups)
    log.info("  %d groups (%d entries) + %d singletons = %d total",
             len(groups), total_grouped, len(singletons), total_grouped + len(singletons))

    # Sort for reporting
    groups.sort(key=lambda g: len(g), reverse=True)

    log.info("  Top groups:")
    for i, g in enumerate(groups[:25]):
        topics = [e.get("topic", "") for e in g]
        parent = min(topics, key=len)
        total_c = _total_content_chars(g)
        log.info("    [%d entries, %d chars] %s", len(g), total_c, parent)
        if len(g) <= 6:
            for t in sorted(topics):
                if t != parent:
                    log.info("      - %s", t)

    if args.dry_run:
        log.info("\n[DRY RUN] Would merge %d groups via LLM (%d workers)",
                 len(groups), args.workers)
        est_calls = len(groups)
        est_min = est_calls / args.workers * 0.6
        log.info("Estimated: %d LLM calls, ~%.0f min", est_calls, est_min)
        return

    # Phase 4: LLM Merge
    log.info("Phase 4: Merging %d groups via LLM (%d workers)...", len(groups), args.workers)
    merged_entries = merge_groups_parallel(groups, workers=args.workers, delay=args.delay)

    # Combine
    all_entries = merged_entries + singletons

    # Phase 5: Validate + Save
    validate_and_save(args.story_id, all_entries, dry_run=args.dry_run)

    log.info("=" * 60)
    log.info("Done! %d → %d entries", len(entries), len(all_entries))
    log.info("=" * 60)


if __name__ == "__main__":
    main()
