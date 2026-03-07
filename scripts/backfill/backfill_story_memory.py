#!/usr/bin/env python3
"""Backfill story anchors and sticky events for an existing branch."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from story_core.character_state import STORY_ANCHOR_LIMIT, _normalize_story_anchors
from story_core.llm_bridge import call_oneshot

logging.basicConfig(
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("story_memory_backfill")

MAX_EVENT_CONTEXT = 200
MAX_LORE_CONTEXT = 80


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


def _extract_json_payload(text: str) -> dict:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("LLM returned empty response")
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(line for line in lines if not line.startswith("```"))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group())
    if not isinstance(data, dict):
        raise ValueError("LLM response is not a JSON object")
    return data


def _story_dir(project_root: str, story_id: str) -> str:
    return os.path.join(project_root, "data", "stories", story_id)


def _branch_dir(project_root: str, story_id: str, branch_id: str) -> str:
    return os.path.join(_story_dir(project_root, story_id), "branches", branch_id)


def _branch_state_path(project_root: str, story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(project_root, story_id, branch_id), "character_state.json")


def _branch_lore_path(project_root: str, story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(project_root, story_id, branch_id), "branch_lore.json")


def _events_db_path(project_root: str, story_id: str) -> str:
    return os.path.join(_story_dir(project_root, story_id), "events.db")


def _get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_sticky_priority_column(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE events ADD COLUMN sticky_priority INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _normalize_sticky_priority(value: object) -> int:
    try:
        priority = int(value or 0)
    except (TypeError, ValueError):
        priority = 0
    return max(0, min(3, priority))


def _load_branch_events(project_root: str, story_id: str, branch_id: str) -> list[dict]:
    db_path = _events_db_path(project_root, story_id)
    if not os.path.exists(db_path):
        return []
    conn = _get_conn(db_path)
    _ensure_sticky_priority_column(conn)
    rows = conn.execute(
        """
        SELECT id, event_type, title, description, status, tags, related_titles, message_index, sticky_priority
        FROM events
        WHERE branch_id = ?
        ORDER BY id
        """,
        (branch_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _format_event_context(events: list[dict]) -> str:
    if not events:
        return "（無事件）"
    lines = []
    for event in events[:MAX_EVENT_CONTEXT]:
        lines.append(
            json.dumps(
                {
                    "id": event.get("id"),
                    "type": event.get("event_type", ""),
                    "title": event.get("title", ""),
                    "status": event.get("status", ""),
                    "sticky_priority": _normalize_sticky_priority(event.get("sticky_priority")),
                    "tags": event.get("tags", ""),
                    "description": str(event.get("description", ""))[:220],
                },
                ensure_ascii=False,
            )
        )
    if len(events) > MAX_EVENT_CONTEXT:
        lines.append(f"（其餘省略 {len(events) - MAX_EVENT_CONTEXT} 筆事件）")
    return "\n".join(lines)


def _format_lore_context(branch_lore: list[dict]) -> str:
    if not branch_lore:
        return "（無分支 lore）"
    lines = []
    for entry in branch_lore[:MAX_LORE_CONTEXT]:
        lines.append(
            json.dumps(
                {
                    "category": entry.get("category", ""),
                    "subcategory": entry.get("subcategory", ""),
                    "topic": entry.get("topic", ""),
                    "content": str(entry.get("content", ""))[:260],
                },
                ensure_ascii=False,
            )
        )
    if len(branch_lore) > MAX_LORE_CONTEXT:
        lines.append(f"（其餘省略 {len(branch_lore) - MAX_LORE_CONTEXT} 筆 lore）")
    return "\n".join(lines)


def _summarize_state(state: dict) -> dict:
    inventory = state.get("inventory", {})
    inventory_sample = inventory
    if isinstance(inventory, dict):
        inventory_sample = dict(list(inventory.items())[:15])
    elif isinstance(inventory, list):
        inventory_sample = inventory[:15]
    return {
        "name": state.get("name"),
        "current_phase": state.get("current_phase"),
        "current_status": state.get("current_status"),
        "gene_lock": state.get("gene_lock"),
        "systems": state.get("systems", {}),
        "relationships": state.get("relationships", {}),
        "completed_missions": state.get("completed_missions", []),
        "inventory_sample": inventory_sample,
        "story_anchors": _normalize_story_anchors(state.get("story_anchors", [])),
    }


def _build_anchor_prompt(story_id: str, branch_id: str, state: dict, branch_lore: list[dict], events: list[dict]) -> str:
    return (
        "你是故事長期記憶整理器。請根據既有 branch 資料，整理應常駐於 system prompt 的 story_anchors。\n\n"
        f"## Story\nstory_id={story_id}\nbranch_id={branch_id}\n\n"
        "## 任務\n"
        "輸出 0-10 條短句 story_anchors。這些錨點是身份層永久事實，只能涵蓋以下 4 類：\n"
        "1. 長期主線\n"
        "2. 核心隊伍關係\n"
        "3. 永久代價 / 不可逆變化\n"
        "4. 長期宿敵 / 契約 / 追索壓力（前提是它已經成為角色身份的一部分）\n\n"
        "不要輸出單純 plot pressure；那種交給 sticky events。\n"
        "不要輸出場景細節、一次性事件、暫時狀態、純 inventory 細節。\n"
        "每條錨點請寫成 8-40 字的簡短 bullet 文本，不要編號。\n\n"
        f"## 角色狀態摘要\n{json.dumps(_summarize_state(state), ensure_ascii=False, indent=2)}\n\n"
        f"## 分支 Lore\n{_format_lore_context(branch_lore)}\n\n"
        f"## 分支事件\n{_format_event_context(events)}\n\n"
        "## 輸出格式\n"
        "只輸出 JSON：\n"
        '{"story_anchors": ["錨點1", "錨點2"]}\n'
    )


def _build_sticky_prompt(story_id: str, branch_id: str, state: dict, current_anchors: list[str], events: list[dict]) -> str:
    return (
        "你是故事事件記憶標註器。請從既有 branch events 中找出應該每回合常駐注入的 sticky events。\n\n"
        f"## Story\nstory_id={story_id}\nbranch_id={branch_id}\n\n"
        "## Sticky 事件定義\n"
        "只標記 cross-arc plot pressure：\n"
        "- 跨弧線的外部威脅 / 追索\n"
        "- 未解的契約 / 承諾\n"
        "- 仍在影響當前劇情的長期伏筆\n\n"
        "不要標記 identity facts；那些已經由 story_anchors 負責。\n"
        "大多數事件都不該是 sticky。最多挑 4 條，並給 priority 1-3。\n"
        "3 = 幾乎每回合都該提醒，2 = 高優先，1 = 一般長期提醒。\n\n"
        f"## 現有 story_anchors\n{json.dumps(current_anchors, ensure_ascii=False, indent=2)}\n\n"
        f"## 角色狀態摘要\n{json.dumps(_summarize_state(state), ensure_ascii=False, indent=2)}\n\n"
        f"## 分支事件\n{_format_event_context(events)}\n\n"
        "## 輸出格式\n"
        "只輸出 JSON：\n"
        '{"sticky_events": [{"id": 123, "sticky_priority": 3, "reason": "仍在跨弧線追索主角"}]}\n'
    )


def _propose_story_anchors(story_id: str, branch_id: str, state: dict, branch_lore: list[dict], events: list[dict]) -> list[str]:
    prompt = _build_anchor_prompt(story_id, branch_id, state, branch_lore, events)
    data = _extract_json_payload(call_oneshot(prompt))
    return _normalize_story_anchors(data.get("story_anchors", []), limit=STORY_ANCHOR_LIMIT)


def _propose_sticky_events(story_id: str, branch_id: str, state: dict, anchors: list[str], events: list[dict]) -> list[dict]:
    prompt = _build_sticky_prompt(story_id, branch_id, state, anchors, events)
    data = _extract_json_payload(call_oneshot(prompt))
    proposals = []
    valid_ids = {event.get("id") for event in events if isinstance(event.get("id"), int)}
    seen_ids = set()
    for item in data.get("sticky_events", []):
        if not isinstance(item, dict):
            continue
        event_id = item.get("id")
        if isinstance(event_id, str) and event_id.isdigit():
            event_id = int(event_id)
        if not isinstance(event_id, int) or event_id not in valid_ids or event_id in seen_ids:
            continue
        seen_ids.add(event_id)
        proposals.append(
            {
                "id": event_id,
                "sticky_priority": _normalize_sticky_priority(item.get("sticky_priority")),
                "reason": str(item.get("reason", "")).strip(),
            }
        )
    proposals = [item for item in proposals if item["sticky_priority"] > 0]
    proposals.sort(key=lambda item: (item["sticky_priority"], item["id"]), reverse=True)
    return proposals[:4]


def _apply_story_anchors(project_root: str, story_id: str, branch_id: str, anchors: list[str]) -> None:
    path = _branch_state_path(project_root, story_id, branch_id)
    state = _load_json(path, {})
    state["story_anchors"] = _normalize_story_anchors(anchors, limit=STORY_ANCHOR_LIMIT)
    _save_json(path, state)


def _apply_sticky_events(project_root: str, story_id: str, branch_id: str, proposals: list[dict]) -> None:
    db_path = _events_db_path(project_root, story_id)
    if not os.path.exists(db_path):
        return
    conn = _get_conn(db_path)
    _ensure_sticky_priority_column(conn)
    conn.execute("UPDATE events SET sticky_priority = 0 WHERE branch_id = ?", (branch_id,))
    for item in proposals:
        conn.execute(
            "UPDATE events SET sticky_priority = ? WHERE branch_id = ? AND id = ?",
            (item["sticky_priority"], branch_id, item["id"]),
        )
    conn.commit()
    conn.close()


def _print_proposal(anchors_before: list[str], anchors_after: list[str], sticky: list[dict], event_title_map: dict[int, str]) -> None:
    print("=== Proposed Story Anchors ===")
    if anchors_after:
        for anchor in anchors_after:
            marker = "=" if anchor in anchors_before else "+"
            print(f"{marker} {anchor}")
    else:
        print("(none)")

    print("\n=== Proposed Sticky Events ===")
    if sticky:
        for item in sticky:
            title = event_title_map.get(item["id"], f"#{item['id']}")
            reason = f" | {item['reason']}" if item.get("reason") else ""
            print(f"P{item['sticky_priority']} #{item['id']} {title}{reason}")
    else:
        print("(none)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill story anchors and sticky events for a branch")
    parser.add_argument("--story-id", required=True, help="Target story_id")
    parser.add_argument("--branch-id", required=True, help="Target branch_id")
    parser.add_argument("--project-root", default=PROJECT_ROOT, help="Project root containing data/stories")
    parser.add_argument("--dry-run", action="store_true", help="Show proposed anchors/sticky events without writing")
    args = parser.parse_args()

    project_root = os.path.abspath(args.project_root)
    branch_dir = _branch_dir(project_root, args.story_id, args.branch_id)
    state_path = _branch_state_path(project_root, args.story_id, args.branch_id)
    if not os.path.isdir(branch_dir):
        log.error("branch not found: %s", branch_dir)
        return 1
    if not os.path.exists(state_path):
        log.error("character_state.json not found: %s", state_path)
        return 1

    state = _load_json(state_path, {})
    branch_lore = _load_json(_branch_lore_path(project_root, args.story_id, args.branch_id), [])
    events = _load_branch_events(project_root, args.story_id, args.branch_id)
    current_anchors = _normalize_story_anchors(state.get("story_anchors", []), limit=STORY_ANCHOR_LIMIT)

    log.info(
        "building story memory proposals for %s/%s (%d events, %d lore entries)",
        args.story_id,
        args.branch_id,
        len(events),
        len(branch_lore),
    )
    proposed_anchors = _propose_story_anchors(args.story_id, args.branch_id, state, branch_lore, events)
    proposed_sticky = _propose_sticky_events(
        args.story_id,
        args.branch_id,
        state,
        proposed_anchors,
        events,
    )

    event_title_map = {event["id"]: event.get("title", "") for event in events if isinstance(event.get("id"), int)}
    _print_proposal(current_anchors, proposed_anchors, proposed_sticky, event_title_map)

    if args.dry_run:
        print("\nDry run only. Re-run without --dry-run to apply.")
        return 0

    _apply_story_anchors(project_root, args.story_id, args.branch_id, proposed_anchors)
    _apply_sticky_events(project_root, args.story_id, args.branch_id, proposed_sticky)
    print("\nApplied story anchors and sticky events.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
