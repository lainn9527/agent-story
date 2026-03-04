"""LLM-based state cleanup: review NPCs, events, inventory, relationships and apply cleanup operations.

Triggered automatically on phase transition (副本中 → 主神空間), periodically every N turns,
or manually via POST /api/state/cleanup.
"""

import json
import logging
import re
import threading
import time

from compaction import get_recap_text
from event_db import get_active_events, get_event_title_map, update_event_status

log = logging.getLogger("rpg")

# Cooldown: (story_id, branch_id) -> (timestamp, last_turn_index)
_last_cleanup: dict[tuple[str, str], tuple[float, int]] = {}
_cleanup_lock = threading.Lock()

CLEANUP_INTERVAL_TURNS = 15
CLEANUP_MIN_COOLDOWN_SECONDS = 60

_CLEANUP_SYSTEM_PROMPT = """你是主神空間 RPG 的狀態審核員。根據當前遊戲階段、NPC 列表、事件、道具欄與人際關係，判斷哪些資料已過時或重複，應被清理。

輸出 **單一 JSON 物件**，且只輸出 JSON，不要其他文字。格式如下：

```json
{
  "archive_npcs": [{"name": "NPC名", "reason": "簡短原因"}],
  "merge_npcs": [{"keep": "保留的名稱（全名）", "remove": "要併入的名稱（簡稱或別名）", "reason": "同一角色"}],
  "resolve_events": [{"title": "事件標題", "new_status": "resolved", "reason": "簡短原因"}],
  "remove_inventory": [{"item": "道具名", "reason": "簡短原因"}],
  "clean_relationships": [{"name": "角色名", "action": "archive_note", "reason": "簡短原因"}]
}
```

規則：
1. **archive_npcs**：將不再活躍的 NPC 標記為歸檔。應歸檔的情況：current_status 描述死亡/已擊殺/已犧牲/已自毀/已退場/已離隊/副本已結束/關係存檔；或明顯屬於「當前副本世界」且當前階段已是主神空間（玩家已離開該副本）。**不要**歸檔玩家持續同伴（relationships 中與玩家有明確羈絆、會跟隨的隊友）。
2. **merge_npcs**：同一角色有多個名稱時（如「卡卡西」與「旗木卡卡西」），保留較完整的名稱（keep），將另一名稱（remove）的資料併入後歸檔 remove。
3. **resolve_events**：已與當前階段無關的舊事件（例如副本專屬事件，且玩家已回主神空間）標為 resolved。new_status 僅用 "resolved" 或 "abandoned"。
4. **remove_inventory**：已消耗、已無效、或明顯為單次副本用品的道具。
5. **clean_relationships**：可選。對已歸檔 NPC 的關係加註或移除，action 用 "archive_note" 表示僅加註為已歸檔，不強制刪除關係。

若某類沒有要清理的項目，請輸出空陣列 []。不要臆造不存在的 NPC 名或事件標題。"""


def _build_cleanup_prompt(
    current_phase: str,
    current_dungeon: str,
    npcs: list[dict],
    active_events: list[dict],
    inventory: dict,
    relationships: dict,
    recap: str,
) -> str:
    npc_lines = []
    for n in npcs:
        name = n.get("name", "?")
        role = n.get("role", "")
        status = n.get("current_status", "")
        tier = n.get("tier", "")
        rel = n.get("relationship_to_player", "")
        npc_lines.append(f"- {name} | role={role} | current_status={status} | tier={tier} | relationship_to_player={rel}")
    events_text = "\n".join(f"- [{e.get('status')}] {e.get('title', '')} (id={e.get('id')})" for e in active_events)
    inv_text = json.dumps(inventory, ensure_ascii=False) if inventory else "（空）"
    rel_text = json.dumps(relationships, ensure_ascii=False) if relationships else "（空）"
    recap_preview = (recap or "")[:1000]

    return (
        "## 當前狀態\n"
        f"- current_phase: {current_phase}\n"
        f"- current_dungeon: {current_dungeon}\n\n"
        "## 活躍 NPC 列表（name | role | current_status | tier | relationship_to_player）\n"
        + "\n".join(npc_lines) + "\n\n"
        "## 活躍事件（status, title, id）\n"
        + (events_text or "（無）") + "\n\n"
        "## 道具欄 (inventory)\n"
        + inv_text + "\n\n"
        "## 人際關係 (relationships)\n"
        + rel_text + "\n\n"
        "## 劇情回顧（節錄）\n"
        + recap_preview + "\n\n"
        "請根據上述規則輸出清理建議的 JSON（只輸出 JSON）。"
    )


def _parse_cleanup_response(response_text: str) -> dict:
    text = (response_text or "").strip()
    # Strip optional markdown code block
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("state_cleanup: JSON parse failed, raw: %s", text[:500])
        return {}


def _apply_cleanup_operations(
    story_id: str,
    branch_id: str,
    ops: dict,
) -> dict:
    """Apply cleanup operations. Uses late import of app to avoid circular import."""
    import app as app_module

    summary = {
        "archived_npcs": 0,
        "merged_npcs": 0,
        "resolved_events": 0,
        "removed_inventory": 0,
        "clean_relationships": 0,
    }

    # archive_npcs
    for item in ops.get("archive_npcs") or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        reason = (item.get("reason") or "cleanup").strip()
        try:
            app_module._save_npc(
                story_id,
                {
                    "name": name,
                    "lifecycle_status": "archived",
                    "archived_reason": reason,
                },
                branch_id,
            )
            summary["archived_npcs"] += 1
        except Exception as e:
            log.warning("state_cleanup: archive_npc %s failed: %s", name, e)

    # merge_npcs: merge remove into keep, then archive remove
    for item in ops.get("merge_npcs") or []:
        keep = (item.get("keep") or "").strip()
        remove = (item.get("remove") or "").strip()
        if not keep or not remove or keep == remove:
            continue
        try:
            npcs = app_module._load_npcs(story_id, branch_id, include_archived=True)
            keep_npc = next((n for n in npcs if (n.get("name") or "").strip() == keep), None)
            remove_npc = next((n for n in npcs if (n.get("name") or "").strip() == remove), None)
            if not remove_npc:
                continue
            # Keep canonical entry (keep_npc) as base; fill missing/empty fields from remove_npc
            merged = dict(keep_npc) if keep_npc else dict(remove_npc)
            for k, v in (remove_npc or {}).items():
                if k in ("name", "id"):
                    continue
                if v and (k not in merged or merged[k] in (None, "")):
                    merged[k] = v
            merged["name"] = keep
            merged["lifecycle_status"] = "active"
            merged["archived_reason"] = None
            app_module._save_npc(story_id, merged, branch_id)
            app_module._save_npc(
                story_id,
                {"name": remove, "lifecycle_status": "archived", "archived_reason": "merged_into_" + keep},
                branch_id,
            )
            summary["merged_npcs"] += 1
        except Exception as e:
            log.warning("state_cleanup: merge_npcs %s -> %s failed: %s", remove, keep, e)

    # resolve_events: by title, need event_id from branch
    title_map = get_event_title_map(story_id, branch_id)
    for item in ops.get("resolve_events") or []:
        title = (item.get("title") or "").strip()
        new_status = (item.get("new_status") or "resolved").strip()
        if new_status not in ("resolved", "abandoned"):
            new_status = "resolved"
        if not title or title not in title_map:
            continue
        try:
            event_id = title_map[title]["id"]
            update_event_status(story_id, event_id, new_status)
            summary["resolved_events"] += 1
        except Exception as e:
            log.warning("state_cleanup: resolve_event %s failed: %s", title, e)

    # remove_inventory: build inventory update with nulls
    remove_items = [x.get("item") for x in (ops.get("remove_inventory") or []) if (x.get("item") or "").strip()]
    if remove_items:
        try:
            schema = app_module._load_character_schema(story_id)
            inv_update = {item: None for item in remove_items}
            app_module._apply_state_update_inner(
                story_id,
                branch_id,
                {"inventory": inv_update},
                schema,
            )
            summary["removed_inventory"] = len(remove_items)
        except Exception as e:
            log.warning("state_cleanup: remove_inventory failed: %s", e)

    # clean_relationships: annotate relationship value with " (已歸檔)" for archived NPCs
    clean_rel_names = [
        (x.get("name") or "").strip()
        for x in (ops.get("clean_relationships") or [])
        if (x.get("name") or "").strip()
    ]
    if clean_rel_names:
        try:
            state = app_module._load_character_state(story_id, branch_id)
            rels = dict(state.get("relationships") or {})
            updated = 0
            for name in clean_rel_names:
                if name not in rels or not rels[name]:
                    continue
                if "已歸檔" in (rels[name] or ""):
                    continue
                rels[name] = (rels[name] or "").strip() + " (已歸檔)"
                updated += 1
            if updated:
                schema = app_module._load_character_schema(story_id)
                app_module._apply_state_update_inner(
                    story_id, branch_id, {"relationships": rels}, schema
                )
                summary["clean_relationships"] = updated
        except Exception as e:
            log.warning("state_cleanup: clean_relationships failed: %s", e)

    return summary


def should_run_cleanup(
    story_id: str,
    branch_id: str,
    turn_index: int,
) -> bool:
    """True if cleanup should run: interval since last cleanup >= CLEANUP_INTERVAL_TURNS and cooldown elapsed."""
    with _cleanup_lock:
        key = (story_id, branch_id)
        last = _last_cleanup.get(key, (0, -CLEANUP_INTERVAL_TURNS))
        last_ts, last_turn = last
    if turn_index < 0:
        return False
    if turn_index - last_turn < CLEANUP_INTERVAL_TURNS:
        return False
    if time.time() - last_ts < CLEANUP_MIN_COOLDOWN_SECONDS:
        return False
    return True


def run_state_cleanup_async(
    story_id: str,
    branch_id: str,
    force: bool = False,
    turn_index: int | None = None,
) -> None:
    """Run LLM-based state cleanup in a background thread. When force=True, run regardless of cooldown."""
    if turn_index is None:
        turn_index = -1

    with _cleanup_lock:
        _last_cleanup[(story_id, branch_id)] = (time.time(), turn_index)

    def _run():
        import app as app_module
        from llm_bridge import call_oneshot

        try:
            state = app_module._load_character_state(story_id, branch_id)
            npcs = app_module._load_npcs(story_id, branch_id, include_archived=False)
            active_events = get_active_events(story_id, branch_id, limit=50)
            inventory = state.get("inventory") or {}
            if not isinstance(inventory, dict):
                inventory = {}
            relationships = state.get("relationships") or {}
            if not isinstance(relationships, dict):
                relationships = {}
            recap = get_recap_text(story_id, branch_id)

            current_phase = (state.get("current_phase") or "主神空間").strip()
            current_dungeon = (state.get("current_dungeon") or "").strip()

            prompt = _build_cleanup_prompt(
                current_phase=current_phase,
                current_dungeon=current_dungeon,
                npcs=npcs,
                active_events=active_events,
                inventory=inventory,
                relationships=relationships,
                recap=recap,
            )
            full_prompt = _CLEANUP_SYSTEM_PROMPT + "\n\n" + prompt

            if hasattr(app_module, "_trace_llm"):
                app_module._trace_llm(
                    story_id,
                    branch_id,
                    "state_cleanup_request",
                    {"prompt_preview": full_prompt[:1500]},
                )

            t0 = time.time()
            response = call_oneshot(full_prompt)

            if hasattr(app_module, "_trace_llm"):
                app_module._trace_llm(
                    story_id,
                    branch_id,
                    "state_cleanup_response_raw",
                    {"raw": response[:2000], "elapsed": time.time() - t0},
                )

            if hasattr(app_module, "_log_llm_usage"):
                app_module._log_llm_usage(story_id, "oneshot", time.time() - t0, branch_id=branch_id)

            ops = _parse_cleanup_response(response)
            if not ops:
                log.info("state_cleanup: no ops parsed")
                return

            summary = _apply_cleanup_operations(story_id, branch_id, ops)
            log.info(
                "state_cleanup: applied archived=%d merged=%d resolved=%d removed_inv=%d clean_rel=%d",
                summary["archived_npcs"],
                summary["merged_npcs"],
                summary["resolved_events"],
                summary["removed_inventory"],
                summary.get("clean_relationships", 0),
            )
        except Exception as e:
            log.warning("state_cleanup: error — %s", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
