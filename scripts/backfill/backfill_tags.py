#!/usr/bin/env python3
"""
One-time backfill script for legacy auto-play branches.

1. Delete 5 empty auto branches (dirs + timeline_tree entries)
2. For 2 historical branches (auto_18f8d831, auto_a46a7941):
   a. Batch GM messages (10/batch), call LLM to extract lore/events/npcs
   b. Dedup & save into world_lore.json / events.db / npcs.json
   c. Clean character_state.json: keep only schema-defined fields
   d. Remove session_id from timeline_tree
   e. Set auto_play_state.json status to "finished"
3. Print stats
"""

import json
import logging
import os
import shutil
import sys
import time

# Add project root to path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from llm_bridge import call_oneshot
from event_db import insert_event, get_event_titles
from lore_db import get_toc as get_lore_toc, upsert_entry as upsert_lore_entry

logging.basicConfig(
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("backfill")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STORY_ID = "story_original"
STORIES_DIR = os.path.join(PROJECT_ROOT, "data", "stories")
STORY_DIR = os.path.join(STORIES_DIR, STORY_ID)
BRANCHES_DIR = os.path.join(STORY_DIR, "branches")

EMPTY_BRANCHES = [
    "auto_01ff7774",
    "auto_09890078",
    "auto_2b2652bf",
    "auto_b42727a5",
    "auto_ba6c5521",
]

BACKFILL_BRANCHES = [
    "auto_18f8d831",
    "auto_a46a7941",
]

BATCH_SIZE = 10

# Schema-defined fields to keep in character_state
SCHEMA_FIELDS = {
    "name", "gene_lock", "physique", "spirit", "reward_points",
    "current_status", "inventory", "completed_missions", "relationships",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path, default=None):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else []


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_tree() -> dict:
    return _load_json(os.path.join(STORY_DIR, "timeline_tree.json"), {})


def _save_tree(tree: dict):
    _save_json(os.path.join(STORY_DIR, "timeline_tree.json"), tree)


def _load_lore() -> list[dict]:
    return _load_json(os.path.join(STORY_DIR, "world_lore.json"), [])


def _save_lore_entry(entry: dict):
    """Save a lore entry (dedup by topic). Also updates SQLite index."""
    lore = _load_lore()
    topic = entry.get("topic", "").strip()
    if not topic:
        return False
    for i, existing in enumerate(lore):
        if existing.get("topic") == topic:
            return False  # Already exists, skip
    lore.append(entry)
    _save_json(os.path.join(STORY_DIR, "world_lore.json"), lore)
    upsert_lore_entry(STORY_ID, entry)
    return True


def _load_npcs(branch_id: str) -> list[dict]:
    return _load_json(os.path.join(BRANCHES_DIR, branch_id, "npcs.json"), [])


def _save_npc(npc_data: dict, branch_id: str) -> bool:
    """Save or merge NPC by name. Returns True if new NPC added."""
    import re as _re
    npcs = _load_npcs(branch_id)
    name = npc_data.get("name", "").strip()
    if not name:
        return False
    if "id" not in npc_data:
        npc_data["id"] = "npc_" + _re.sub(r'\W+', '', name)[:20]
    for i, existing in enumerate(npcs):
        if existing.get("name") == name:
            merged = {**existing, **npc_data}
            npcs[i] = merged
            _save_json(os.path.join(BRANCHES_DIR, branch_id, "npcs.json"), npcs)
            return False  # Updated, not new
    npcs.append(npc_data)
    _save_json(os.path.join(BRANCHES_DIR, branch_id, "npcs.json"), npcs)
    return True


# ---------------------------------------------------------------------------
# Step 1: Delete empty branches
# ---------------------------------------------------------------------------

def delete_empty_branches():
    log.info("=== Step 1: Deleting empty auto branches ===")
    tree = _load_tree()
    deleted_count = 0

    for bid in EMPTY_BRANCHES:
        branch_path = os.path.join(BRANCHES_DIR, bid)
        if os.path.exists(branch_path):
            shutil.rmtree(branch_path)
            log.info("  Deleted directory: %s", bid)
            deleted_count += 1
        else:
            log.info("  Directory not found (already gone): %s", bid)

        if bid in tree.get("branches", {}):
            del tree["branches"][bid]
            log.info("  Removed from timeline_tree: %s", bid)

    _save_tree(tree)
    log.info("  Deleted %d empty branches", deleted_count)
    return deleted_count


# ---------------------------------------------------------------------------
# Step 2: Backfill extraction
# ---------------------------------------------------------------------------

def build_extraction_prompt(gm_texts: list[str], toc: str, existing_titles: set[str]) -> str:
    batched = ""
    for i, text in enumerate(gm_texts, 1):
        batched += f"--- GM 回覆 #{i} ---\n{text}\n\n"

    titles_str = ", ".join(sorted(existing_titles)[:50]) if existing_titles else "（無）"

    return (
        "你是一個 RPG 結構化資料擷取工具。分析以下多段 GM 回覆，提取結構化資訊。\n\n"
        f"## GM 回覆\n{batched}\n"
        "## 1. 世界設定（lore）\n"
        "提取新的世界設定：體系規則、副本背景、場景描述等。不要提取劇情動態或角色行動。\n"
        f"已有設定（避免重複）：\n{toc}\n"
        '格式：[{{"category": "分類", "topic": "主題", "content": "完整描述"}}]\n'
        "可用分類：主神設定與規則/體系/商城/副本世界觀/場景/NPC/故事追蹤\n\n"
        "## 2. 事件追蹤（events）\n"
        "提取重要事件：伏筆、轉折、戰鬥、發現等。不要記錄瑣碎事件。\n"
        f"已有事件標題（避免重複）：{titles_str}\n"
        '格式：[{{"event_type": "類型", "title": "標題", "description": "描述", "status": "planted/triggered/resolved", "tags": "關鍵字"}}]\n'
        "可用類型：伏筆/轉折/遭遇/發現/戰鬥/獲得/觸發\n"
        "可用狀態：planted/triggered/resolved\n\n"
        "## 3. NPC 資料（npcs）\n"
        "提取首次登場或有重大變化的 NPC。\n"
        '格式：[{{"name": "名字", "role": "定位", "appearance": "外觀", '
        '"personality": {{"openness": N, "conscientiousness": N, "extraversion": N, '
        '"agreeableness": N, "neuroticism": N, "summary": "一句話"}}, "backstory": "背景"}}]\n\n'
        "## 輸出\n"
        'JSON：{{"lore": [...], "events": [...], "npcs": [...]}}\n'
        "沒有新資訊的類型用空陣列。只輸出 JSON。"
    )


def extract_batch(gm_texts: list[str], toc: str, existing_titles: set[str]) -> dict:
    """Call LLM to extract lore/events/npcs from a batch of GM messages."""
    prompt = build_extraction_prompt(gm_texts, toc, existing_titles)
    response = call_oneshot(prompt)
    if not response:
        log.warning("  Empty LLM response for batch")
        return {"lore": [], "events": [], "npcs": []}

    # Parse JSON from response (handle markdown code blocks)
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (```json and ```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in response
        import re
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                log.warning("  Failed to parse LLM response as JSON")
                return {"lore": [], "events": [], "npcs": []}
        else:
            log.warning("  No JSON found in LLM response")
            return {"lore": [], "events": [], "npcs": []}

    return {
        "lore": data.get("lore", []),
        "events": data.get("events", []),
        "npcs": data.get("npcs", []),
    }


def backfill_branch(branch_id: str, start_batch: int = 1):
    """Backfill lore/events/npcs for a single branch."""
    log.info("=== Backfilling branch: %s (start_batch=%d) ===", branch_id, start_batch)

    # Load messages
    msgs_path = os.path.join(BRANCHES_DIR, branch_id, "messages.json")
    messages = _load_json(msgs_path, [])
    gm_messages = [m for m in messages if m.get("role") == "gm"]
    log.info("  Total messages: %d, GM messages: %d", len(messages), len(gm_messages))

    if not gm_messages:
        log.info("  No GM messages to process")
        return {"lore": 0, "events": 0, "npcs": 0}

    stats = {"lore": 0, "events": 0, "npcs": 0}

    # Process in batches
    total_batches = (len(gm_messages) + BATCH_SIZE - 1) // BATCH_SIZE
    for batch_idx in range(0, len(gm_messages), BATCH_SIZE):
        batch_num = batch_idx // BATCH_SIZE + 1
        if batch_num < start_batch:
            continue
        batch = gm_messages[batch_idx:batch_idx + BATCH_SIZE]
        gm_texts = [m["content"] for m in batch]

        # Get fresh dedup context each batch
        toc = get_lore_toc(STORY_ID)
        existing_titles = get_event_titles(STORY_ID, branch_id)

        log.info("  Batch %d/%d (%d messages)...", batch_num, total_batches, len(batch))

        data = extract_batch(gm_texts, toc, existing_titles)

        # Save lore
        for entry in data.get("lore", []):
            if isinstance(entry, dict) and entry.get("topic"):
                if _save_lore_entry(entry):
                    stats["lore"] += 1
                    log.info("    + Lore: %s", entry["topic"])

        # Save events
        for event in data.get("events", []):
            if isinstance(event, dict) and event.get("title"):
                title = event["title"].strip()
                # Re-check dedup (titles may have been added by earlier batches)
                current_titles = get_event_titles(STORY_ID, branch_id)
                if title not in current_titles:
                    insert_event(STORY_ID, event, branch_id)
                    stats["events"] += 1
                    log.info("    + Event: %s", title)

        # Save NPCs
        for npc in data.get("npcs", []):
            if isinstance(npc, dict) and npc.get("name"):
                if _save_npc(npc, branch_id):
                    stats["npcs"] += 1
                    log.info("    + NPC: %s", npc["name"])

        # Rate limit: small delay between batches
        if batch_num < total_batches:
            time.sleep(2)

    log.info("  Branch %s done: +%d lore, +%d events, +%d npcs",
             branch_id, stats["lore"], stats["events"], stats["npcs"])
    return stats


# ---------------------------------------------------------------------------
# Step 3: Clean character state
# ---------------------------------------------------------------------------

def clean_character_state(branch_id: str):
    """Keep only schema-defined fields, truncate long text values."""
    log.info("=== Cleaning character state: %s ===", branch_id)
    cs_path = os.path.join(BRANCHES_DIR, branch_id, "character_state.json")
    cs = _load_json(cs_path, {})
    if not cs:
        log.info("  No character state found")
        return 0

    original_keys = set(cs.keys())
    removed_keys = original_keys - SCHEMA_FIELDS

    # Build clean state
    clean = {}
    for key in SCHEMA_FIELDS:
        if key in cs:
            clean[key] = cs[key]

    # Truncate physique/spirit if string > 20 chars
    for field in ("physique", "spirit"):
        if field in clean and isinstance(clean[field], str) and len(clean[field]) > 20:
            clean[field] = clean[field][:20]
            log.info("  Truncated %s to 20 chars", field)

    _save_json(cs_path, clean)
    log.info("  Removed %d junk fields: %s", len(removed_keys), ", ".join(sorted(removed_keys)))
    return len(removed_keys)


# ---------------------------------------------------------------------------
# Step 4: Clean timeline_tree (remove session_id, set auto_play_state)
# ---------------------------------------------------------------------------

def clean_branch_metadata(branch_id: str):
    """Remove session_id from timeline_tree, set auto_play_state to finished."""
    log.info("=== Cleaning metadata: %s ===", branch_id)

    # Remove session_id
    tree = _load_tree()
    branch = tree.get("branches", {}).get(branch_id, {})
    if "session_id" in branch:
        del branch["session_id"]
        _save_tree(tree)
        log.info("  Removed session_id")

    # Set auto_play_state to finished
    state_path = os.path.join(BRANCHES_DIR, branch_id, "auto_play_state.json")
    state = _load_json(state_path, {})
    if state:
        state["status"] = "finished"
        _save_json(state_path, state)
        log.info("  Set auto_play_state status to 'finished'")
    else:
        # Create minimal state file
        _save_json(state_path, {"status": "finished"})
        log.info("  Created auto_play_state.json with status='finished'")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Backfill tags for legacy auto-play branches")
    parser.add_argument("--resume", metavar="BRANCH_ID",
                        help="Resume backfill for a specific branch only (skip delete/clean steps)")
    parser.add_argument("--start-batch", type=int, default=1,
                        help="Start from this batch number (1-indexed, use with --resume)")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Legacy Auto-Play Branch Cleanup")
    log.info("Story: %s", STORY_ID)
    log.info("=" * 60)

    if args.resume:
        # Resume mode: only backfill one branch, skip delete/clean
        bid = args.resume
        log.info("RESUME mode: branch=%s, start_batch=%d", bid, args.start_batch)

        lore_before = len(_load_lore())
        events_before = len(get_event_titles(STORY_ID, bid))
        npcs_before = len(_load_npcs(bid))

        stats = backfill_branch(bid, start_batch=args.start_batch)

        lore_after = len(_load_lore())
        events_after = len(get_event_titles(STORY_ID, bid))
        npcs_after = len(_load_npcs(bid))

        log.info("")
        log.info("=" * 60)
        log.info("SUMMARY (resume)")
        log.info("=" * 60)
        log.info("Lore: %d → %d (+%d)", lore_before, lore_after, stats["lore"])
        log.info("Events: %d → %d (+%d)", events_before, events_after, stats["events"])
        log.info("NPCs: %d → %d (+%d)", npcs_before, npcs_after, stats["npcs"])
        log.info("Done!")
        return

    # Full run
    lore_before = len(_load_lore())
    events_before = sum(
        len(get_event_titles(STORY_ID, bid)) for bid in BACKFILL_BRANCHES
    )
    npcs_before = {bid: len(_load_npcs(bid)) for bid in BACKFILL_BRANCHES}

    log.info("Before: %d lore entries, %d events (across backfill branches)",
             lore_before, events_before)
    for bid in BACKFILL_BRANCHES:
        log.info("  %s: %d NPCs", bid, npcs_before[bid])

    # Step 1: Delete empty branches
    deleted = delete_empty_branches()

    # Step 2: Backfill extraction for historical branches
    total_stats = {"lore": 0, "events": 0, "npcs": 0}
    for bid in BACKFILL_BRANCHES:
        stats = backfill_branch(bid)
        for k in total_stats:
            total_stats[k] += stats[k]

    # Step 3: Clean character state
    total_removed = 0
    for bid in BACKFILL_BRANCHES:
        total_removed += clean_character_state(bid)

    # Step 4: Clean metadata
    for bid in BACKFILL_BRANCHES:
        clean_branch_metadata(bid)

    # Final stats
    lore_after = len(_load_lore())
    events_after = sum(
        len(get_event_titles(STORY_ID, bid)) for bid in BACKFILL_BRANCHES
    )
    npcs_after = {bid: len(_load_npcs(bid)) for bid in BACKFILL_BRANCHES}

    log.info("")
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    log.info("Empty branches deleted: %d", deleted)
    log.info("Lore: %d → %d (+%d)", lore_before, lore_after, total_stats["lore"])
    log.info("Events: %d → %d (+%d)", events_before, events_after, total_stats["events"])
    for bid in BACKFILL_BRANCHES:
        log.info("NPCs [%s]: %d → %d", bid, npcs_before[bid], npcs_after[bid])
    log.info("Character state junk fields removed: %d", total_removed)
    log.info("Done!")


if __name__ == "__main__":
    main()
