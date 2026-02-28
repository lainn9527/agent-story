#!/usr/bin/env python3
"""Backfill NPC lifecycle fields, run R1 dedupe, and rebuild state.db."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import state_db

STORIES_DIR = os.path.join(PROJECT_ROOT, "data", "stories")
ARCHIVE_KEYWORDS = (
    "已損毀",
    "威脅解除",
    "已退場",
    "已失效",
    "已封印",
    "已消散",
    "已離隊",
)
UNARCHIVE_KEYWORDS = (
    "修復",
    "復活",
    "再現身",
    "重新啟用",
    "解除封印",
)
R1_PUNCT_RE = re.compile(r"[ \t\r\n\u3000\.\,，。:：;；!！?？'\"“”‘’`~·•・\-—–−_()（）\[\]【】{}<>《》〈〉/\\|+]+")

logging.basicConfig(
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("npc_lifecycle_backfill")


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _normalize_lifecycle_status(raw_status: object) -> str:
    if not isinstance(raw_status, str):
        return "active"
    text = raw_status.strip().lower()
    if text in {"archived", "archive", "封存", "已封存", "归档", "歸檔"}:
        return "archived"
    return "active"


def _derive_lifecycle(current_status: object, existing_status: object) -> tuple[str, str | None]:
    existing = _normalize_lifecycle_status(existing_status)
    text = str(current_status or "").strip()
    if not text:
        return existing, None
    for kw in UNARCHIVE_KEYWORDS:
        if kw in text:
            return "active", kw
    for kw in ARCHIVE_KEYWORDS:
        if kw in text:
            return "archived", kw
    return existing, None


def _normalize_npc_name_r1(name: object) -> str:
    if not isinstance(name, str):
        return ""
    normalized = unicodedata.normalize("NFKC", name).strip()
    if not normalized:
        return ""
    normalized = R1_PUNCT_RE.sub("", normalized)
    return normalized.casefold()


def _apply_lifecycle(npc: dict) -> tuple[dict, str]:
    updated = dict(npc)
    old = _normalize_lifecycle_status(npc.get("lifecycle_status"))
    new, matched_kw = _derive_lifecycle(npc.get("current_status"), old)
    updated["lifecycle_status"] = new
    if new == "archived":
        if matched_kw:
            updated["archived_reason"] = f"current_status:{matched_kw}"
        else:
            prev_reason = str(npc.get("archived_reason") or "").strip()
            updated["archived_reason"] = prev_reason or "current_status"
    else:
        updated["archived_reason"] = None
    return updated, old


def _dedupe_npcs_r1(npcs: list[dict]) -> tuple[list[dict], int]:
    out: list[dict] = []
    seen: dict[str, int] = {}
    merged_count = 0

    for npc in npcs:
        if not isinstance(npc, dict):
            continue
        name = str(npc.get("name") or "").strip()
        if not name:
            continue
        norm = _normalize_npc_name_r1(name)
        if not norm:
            out.append(dict(npc))
            continue
        if norm not in seen:
            seen[norm] = len(out)
            out.append(dict(npc))
            continue

        idx = seen[norm]
        current = out[idx]
        merged = {**current, **npc}
        merged["name"] = current.get("name", name)
        if current.get("id"):
            merged["id"] = current.get("id")
        out[idx] = merged
        merged_count += 1

    return out, merged_count


def _build_ambiguous_candidates(npcs: list[dict]) -> list[dict]:
    names = []
    for npc in npcs:
        if not isinstance(npc, dict):
            continue
        name = str(npc.get("name") or "").strip()
        if name:
            names.append(name)
    names = sorted(set(names))

    candidates = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a = names[i]
            b = names[j]
            short, long = (a, b) if len(a) <= len(b) else (b, a)
            if len(short) < 2:
                continue
            if short in long and (len(long) - len(short)) <= 6:
                candidates.append(
                    {
                        "name_a": a,
                        "name_b": b,
                        "reason": "substring_name_candidate",
                    }
                )
    return candidates


def _iter_targets(story_filters: list[str], branch_filter: str | None):
    if not os.path.isdir(STORIES_DIR):
        return
    all_story_ids = sorted(
        d for d in os.listdir(STORIES_DIR) if os.path.isdir(os.path.join(STORIES_DIR, d))
    )
    target_story_ids = story_filters if story_filters else all_story_ids
    for story_id in target_story_ids:
        story_dir = os.path.join(STORIES_DIR, story_id)
        branches_dir = os.path.join(story_dir, "branches")
        if not os.path.isdir(branches_dir):
            continue
        if branch_filter:
            branch_ids = [branch_filter]
        else:
            branch_ids = sorted(
                d for d in os.listdir(branches_dir) if os.path.isdir(os.path.join(branches_dir, d))
            )
        for branch_id in branch_ids:
            yield story_id, branch_id


def _process_branch(story_id: str, branch_id: str, apply_changes: bool) -> tuple[dict, list[dict]]:
    branch_dir = os.path.join(STORIES_DIR, story_id, "branches", branch_id)
    npcs_path = os.path.join(branch_dir, "npcs.json")
    state_path = os.path.join(branch_dir, "character_state.json")
    if not os.path.exists(npcs_path):
        return {
            "story_id": story_id,
            "branch_id": branch_id,
            "archived_count": 0,
            "unarchived_count": 0,
            "r1_merged_count": 0,
            "candidate_count": 0,
            "rebuilt_db_count": 0,
        }, []

    raw_npcs = _load_json(npcs_path, [])
    if not isinstance(raw_npcs, list):
        raw_npcs = []

    lifecycle_npcs = []
    archived_count = 0
    unarchived_count = 0
    for npc in raw_npcs:
        if not isinstance(npc, dict):
            continue
        updated, old_status = _apply_lifecycle(npc)
        new_status = updated.get("lifecycle_status", "active")
        if old_status != new_status:
            if new_status == "archived":
                archived_count += 1
            else:
                unarchived_count += 1
        lifecycle_npcs.append(updated)

    deduped_npcs, r1_merged_count = _dedupe_npcs_r1(lifecycle_npcs)
    candidates = _build_ambiguous_candidates(deduped_npcs)
    rebuilt_db_count = 0

    if apply_changes:
        _save_json(npcs_path, deduped_npcs)
        state = _load_json(state_path, {})
        state_db.rebuild_from_json(story_id, branch_id, state=state, npcs=deduped_npcs)
        rebuilt_db_count = 1

    summary = {
        "story_id": story_id,
        "branch_id": branch_id,
        "archived_count": archived_count,
        "unarchived_count": unarchived_count,
        "r1_merged_count": r1_merged_count,
        "candidate_count": len(candidates),
        "rebuilt_db_count": rebuilt_db_count,
    }
    return summary, candidates


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill NPC lifecycle + R1 dedupe, then rebuild state.db"
    )
    parser.add_argument(
        "--story",
        action="append",
        default=[],
        help="Target story_id (repeatable). Default: all stories.",
    )
    parser.add_argument(
        "--branch",
        help="Target branch_id. Default: all branches in target stories.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes and rebuild state.db. Default is dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run (no writes).",
    )
    parser.add_argument(
        "--report-path",
        default=os.path.join(PROJECT_ROOT, "data", "ambiguous_candidates.json"),
        help="Where to write ambiguous candidate report JSON.",
    )
    args = parser.parse_args()

    apply_changes = bool(args.apply and not args.dry_run)
    mode = "apply" if apply_changes else "dry-run"
    log.info("NPC lifecycle backfill mode: %s", mode)

    totals = {
        "archived_count": 0,
        "unarchived_count": 0,
        "r1_merged_count": 0,
        "candidate_count": 0,
        "rebuilt_db_count": 0,
    }
    branch_summaries = []
    report_entries = []

    for story_id, branch_id in _iter_targets(args.story, args.branch):
        summary, candidates = _process_branch(story_id, branch_id, apply_changes=apply_changes)
        branch_summaries.append(summary)
        totals["archived_count"] += summary["archived_count"]
        totals["unarchived_count"] += summary["unarchived_count"]
        totals["r1_merged_count"] += summary["r1_merged_count"]
        totals["candidate_count"] += summary["candidate_count"]
        totals["rebuilt_db_count"] += summary["rebuilt_db_count"]
        if candidates:
            report_entries.append(
                {
                    "story_id": story_id,
                    "branch_id": branch_id,
                    "candidates": candidates,
                }
            )
        log.info(
            "  %s/%s archived=%d unarchived=%d r1_merged=%d candidates=%d rebuilt_db=%d",
            story_id,
            branch_id,
            summary["archived_count"],
            summary["unarchived_count"],
            summary["r1_merged_count"],
            summary["candidate_count"],
            summary["rebuilt_db_count"],
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "branches": report_entries,
        "branch_count": len(branch_summaries),
        "candidate_count": totals["candidate_count"],
    }
    _save_json(args.report_path, report)

    print(json.dumps(
        {
            "mode": mode,
            "archived_count": totals["archived_count"],
            "unarchived_count": totals["unarchived_count"],
            "r1_merged_count": totals["r1_merged_count"],
            "candidate_count": totals["candidate_count"],
            "rebuilt_db_count": totals["rebuilt_db_count"],
            "report_path": args.report_path,
        },
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
