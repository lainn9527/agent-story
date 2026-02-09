"""Lore topic organization: rule-based + periodic LLM cleanup for orphan topics.

An 'orphan' topic is one without a `：` separator (no hierarchical prefix).
This module:
  1. Provides per-story write locks for world_lore.json
  2. Builds a prefix registry from existing topics
  3. Rule-based matching: classify orphans by starts-with / exact match
  4. Periodic background LLM cleanup for remaining orphans
  5. rename_lore_topic: atomic rename in JSON + SQLite
"""

import json
import logging
import os
import re
import threading
import time as _time
from datetime import datetime, timezone, timedelta

log = logging.getLogger("rpg")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STORIES_DIR = os.path.join(DATA_DIR, "stories")


def _story_dir(story_id: str) -> str:
    return os.path.join(STORIES_DIR, story_id)


def _lore_path(story_id: str) -> str:
    return os.path.join(_story_dir(story_id), "world_lore.json")


def _state_path(story_id: str) -> str:
    return os.path.join(_story_dir(story_id), "lore_organizer_state.json")


# ---------------------------------------------------------------------------
# Per-story lore write locks
# ---------------------------------------------------------------------------
_lore_write_locks: dict[str, threading.Lock] = {}
_lore_locks_meta = threading.Lock()


def get_lore_lock(story_id: str) -> threading.Lock:
    """Get or create a per-story lock for world_lore.json writes."""
    with _lore_locks_meta:
        if story_id not in _lore_write_locks:
            _lore_write_locks[story_id] = threading.Lock()
        return _lore_write_locks[story_id]


# ---------------------------------------------------------------------------
# Prefix Registry (in-memory cache)
# ---------------------------------------------------------------------------
_prefix_cache: dict[str, dict] = {}
_CACHE_TTL = 60  # seconds


def build_prefix_registry(story_id: str) -> dict:
    """Build prefix registry from world_lore.json. Returns {by_category: {cat: set}, all: set}.

    A prefix is the part before `：` in a topic name. E.g. "基因鎖：概述" → prefix "基因鎖".
    """
    cached = _prefix_cache.get(story_id)
    if cached and (_time.time() - cached.get("ts", 0)) < _CACHE_TTL:
        return cached

    lore_file = _lore_path(story_id)
    if not os.path.exists(lore_file):
        empty = {"by_category": {}, "all": set(), "ts": _time.time()}
        _prefix_cache[story_id] = empty
        return empty

    with open(lore_file, "r", encoding="utf-8") as f:
        lore = json.load(f)

    by_category: dict[str, set] = {}
    all_prefixes: set[str] = set()

    for entry in lore:
        topic = entry.get("topic", "").strip()
        if "：" not in topic:
            continue
        prefix = topic.split("：")[0].strip()
        if not prefix:
            continue
        cat = entry.get("category", "")
        if cat not in by_category:
            by_category[cat] = set()
        by_category[cat].add(prefix)
        all_prefixes.add(prefix)

    result = {"by_category": by_category, "all": all_prefixes, "ts": _time.time()}
    _prefix_cache[story_id] = result
    return result


def invalidate_prefix_cache(story_id: str):
    """Invalidate cached prefix registry for a story."""
    _prefix_cache.pop(story_id, None)


# ---------------------------------------------------------------------------
# Rule-Based Matching (no LLM)
# ---------------------------------------------------------------------------
_SUFFIX_STOPWORDS = {"系統", "機制", "規則", "概述", "體系", "設定", "說明"}


def try_classify_topic(topic: str, category: str, story_id: str,
                       prefix_registry: dict | None = None) -> str | None:
    """Try to classify an orphan topic (no `：`) into a prefixed form.

    Returns new topic string like "基因鎖：概述" or None if no match.
    """
    if "：" in topic:
        return None  # already classified

    if prefix_registry is None:
        prefix_registry = build_prefix_registry(story_id)

    cat_prefixes = prefix_registry.get("by_category", {}).get(category, set())
    if not cat_prefixes:
        return None

    # Rule 1: Starts-with match — orphan starts with a known prefix
    # Pick longest matching prefix to avoid greedy short matches
    best_prefix = None
    best_len = 0
    for prefix in cat_prefixes:
        if topic.startswith(prefix) and len(prefix) > best_len:
            remainder = topic[len(prefix):]
            # Remainder must be non-empty (otherwise it's Rule 2: exact match)
            if remainder:
                # Remainder must be ≥ 2 chars, or be a known stopword
                if len(remainder) >= 2 or remainder in _SUFFIX_STOPWORDS:
                    best_prefix = prefix
                    best_len = len(prefix)

    if best_prefix:
        remainder = topic[best_len:]
        # If remainder is purely a stopword, treat as "概述"-style entry
        if remainder in _SUFFIX_STOPWORDS:
            return f"{best_prefix}：{remainder}"
        return f"{best_prefix}：{remainder}"

    # Rule 2: Exact prefix match — orphan IS a known prefix
    if topic in cat_prefixes:
        return f"{topic}：概述"

    return None


# ---------------------------------------------------------------------------
# Orphan detection helpers
# ---------------------------------------------------------------------------

def find_orphans(story_id: str) -> list[dict]:
    """Find all orphan topics (no `：`) in world_lore.json."""
    lore_file = _lore_path(story_id)
    if not os.path.exists(lore_file):
        return []
    with open(lore_file, "r", encoding="utf-8") as f:
        lore = json.load(f)
    return [e for e in lore if "：" not in e.get("topic", "")]


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

def _load_state(story_id: str) -> dict:
    path = _state_path(story_id)
    if not os.path.exists(path):
        return {"last_organized_at": None, "skip_topics": {}, "total_organized": 0}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(story_id: str, state: dict):
    path = _state_path(story_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# rename_lore_topic
# ---------------------------------------------------------------------------

def rename_lore_topic(story_id: str, old_topic: str, new_topic: str):
    """Rename a lore topic in world_lore.json + SQLite index. Must be called within lore lock."""
    from lore_db import delete_entry as delete_lore_entry, upsert_entry as upsert_lore_entry

    lore_file = _lore_path(story_id)
    if not os.path.exists(lore_file):
        return

    with open(lore_file, "r", encoding="utf-8") as f:
        lore = json.load(f)

    updated = False
    updated_entry = None
    for entry in lore:
        if entry.get("topic") == old_topic:
            entry["topic"] = new_topic
            updated_entry = entry
            updated = True
            break

    if not updated:
        return

    # Atomic write
    tmp = lore_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lore, f, ensure_ascii=False, indent=2)
    os.replace(tmp, lore_file)

    # Update SQLite index
    delete_lore_entry(story_id, old_topic)
    upsert_lore_entry(story_id, updated_entry)

    log.info("    lore_organizer: renamed '%s' → '%s'", old_topic, new_topic)


# ---------------------------------------------------------------------------
# Periodic Background LLM Cleanup
# ---------------------------------------------------------------------------
MIN_ORPHANS_FOR_TRIGGER = 5
MIN_COOLDOWN_SECONDS = 300  # 5 minutes
MAX_ORPHANS_PER_LLM_CALL = 20
SKIP_TTL_DAYS = 7


def _prune_skip_topics(skip_topics: dict) -> dict:
    """Remove skip_topics entries older than SKIP_TTL_DAYS."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SKIP_TTL_DAYS)).isoformat()
    return {topic: ts for topic, ts in skip_topics.items() if ts > cutoff}


def should_organize(story_id: str) -> bool:
    """Check if periodic LLM organization should run."""
    state = _load_state(story_id)

    # Cooldown check
    last = state.get("last_organized_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if elapsed < MIN_COOLDOWN_SECONDS:
                return False
        except (ValueError, TypeError):
            pass

    # Count actionable orphans (exclude skip_topics)
    orphans = find_orphans(story_id)
    skip = _prune_skip_topics(state.get("skip_topics", {}))
    actionable = [o for o in orphans if o.get("topic", "") not in skip]

    return len(actionable) >= MIN_ORPHANS_FOR_TRIGGER


def organize_lore_async(story_id: str):
    """Run LLM-based orphan classification in a background thread."""
    def _do_organize():
        try:
            _organize_orphans_llm(story_id)
        except Exception:
            log.exception("lore_organizer: background organize failed")

    t = threading.Thread(target=_do_organize, daemon=True)
    t.start()


def _organize_orphans_llm(story_id: str):
    """LLM-based orphan classification (closed-option, no generation)."""
    from llm_bridge import call_oneshot

    lock = get_lore_lock(story_id)
    state = _load_state(story_id)
    skip = _prune_skip_topics(state.get("skip_topics", {}))

    orphans = find_orphans(story_id)
    actionable = [o for o in orphans if o.get("topic", "") not in skip]

    if not actionable:
        state["last_organized_at"] = datetime.now(timezone.utc).isoformat()
        state["skip_topics"] = skip
        _save_state(story_id, state)
        return

    # Build prefix registry once
    registry = build_prefix_registry(story_id)

    # Phase 1: Rule-based pass first
    rule_classified = 0
    remaining = []
    for orphan in actionable:
        topic = orphan.get("topic", "")
        category = orphan.get("category", "")
        new_topic = try_classify_topic(topic, category, story_id, prefix_registry=registry)
        if new_topic:
            with lock:
                rename_lore_topic(story_id, topic, new_topic)
            rule_classified += 1
        else:
            remaining.append(orphan)

    if rule_classified:
        log.info("lore_organizer: rule-based classified %d orphans", rule_classified)
        invalidate_prefix_cache(story_id)
        registry = build_prefix_registry(story_id)

    if not remaining:
        state["last_organized_at"] = datetime.now(timezone.utc).isoformat()
        state["skip_topics"] = skip
        state["total_organized"] = state.get("total_organized", 0) + rule_classified
        _save_state(story_id, state)
        return

    # Phase 2: LLM classification (batch up to MAX_ORPHANS_PER_LLM_CALL)
    batch = remaining[:MAX_ORPHANS_PER_LLM_CALL]

    # Build prefix list by category
    prefix_lines = []
    for cat, prefixes in sorted(registry.get("by_category", {}).items()):
        if prefixes:
            prefix_lines.append(f"[{cat}] {', '.join(sorted(prefixes))}")

    if not prefix_lines:
        # No prefixes to classify into — skip all
        now = datetime.now(timezone.utc).isoformat()
        for o in batch:
            skip[o.get("topic", "")] = now
        state["last_organized_at"] = now
        state["skip_topics"] = skip
        state["total_organized"] = state.get("total_organized", 0) + rule_classified
        _save_state(story_id, state)
        return

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

    result = call_oneshot(prompt)
    if not result:
        log.warning("lore_organizer: LLM returned empty response")
        state["last_organized_at"] = datetime.now(timezone.utc).isoformat()
        state["skip_topics"] = skip
        _save_state(story_id, state)
        return

    # Parse LLM response
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
            log.warning("lore_organizer: LLM response not parseable")
            state["last_organized_at"] = datetime.now(timezone.utc).isoformat()
            _save_state(story_id, state)
            return
        try:
            classifications = json.loads(m.group())
        except json.JSONDecodeError:
            log.warning("lore_organizer: LLM JSON parse failed")
            state["last_organized_at"] = datetime.now(timezone.utc).isoformat()
            _save_state(story_id, state)
            return

    if not isinstance(classifications, list):
        log.warning("lore_organizer: LLM returned non-list")
        state["last_organized_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(story_id, state)
        return

    # Build lookup: topic → orphan category
    orphan_cats = {o.get("topic", ""): o.get("category", "") for o in batch}
    all_prefixes = registry.get("all", set())
    llm_classified = 0
    now = datetime.now(timezone.utc).isoformat()

    for item in classifications:
        if not isinstance(item, dict):
            continue
        topic = item.get("topic", "").strip()
        prefix = item.get("prefix", "").strip()

        if not topic or topic not in orphan_cats:
            continue

        if prefix == "none" or not prefix:
            skip[topic] = now
            continue

        # Validation gate: prefix must exist in same category's prefix set
        orphan_cat = orphan_cats[topic]
        cat_prefixes = registry.get("by_category", {}).get(orphan_cat, set())
        if prefix not in cat_prefixes:
            # Check all categories as fallback (LLM might suggest cross-category)
            if prefix not in all_prefixes:
                log.info("    lore_organizer: LLM suggested invalid prefix '%s' for '%s', skipping", prefix, topic)
                skip[topic] = now
                continue

        new_topic = f"{prefix}：{topic}"
        with lock:
            rename_lore_topic(story_id, topic, new_topic)
        llm_classified += 1

    if llm_classified:
        log.info("lore_organizer: LLM classified %d orphans", llm_classified)
        invalidate_prefix_cache(story_id)

    # Update state
    state["last_organized_at"] = now
    state["skip_topics"] = skip
    state["total_organized"] = state.get("total_organized", 0) + rule_classified + llm_classified
    _save_state(story_id, state)
