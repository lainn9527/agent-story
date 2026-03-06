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
from dungeon_system import reconcile_dungeon_entry, reconcile_dungeon_exit
from event_db import get_active_events, get_event_title_map, update_event_status

log = logging.getLogger("rpg")

# Cooldown: (story_id, branch_id) -> (timestamp, last_turn_index)
_last_cleanup: dict[tuple[str, str], tuple[float, int]] = {}
_cleanup_lock = threading.Lock()

CLEANUP_INTERVAL_TURNS = 15
CLEANUP_MIN_COOLDOWN_SECONDS = 60

_CLEANUP_SYSTEM_PROMPT = """你是主神空間 RPG 的狀態審核員。根據當前遊戲階段、NPC 列表、事件、道具欄、功法與技能、體系、人際關係，判斷哪些資料已過時、重複或放錯位置，應被清理。

輸出 **單一 JSON 物件**，且只輸出 JSON，不要其他文字。格式如下：

```json
{
  "archive_npcs": [{"name": "NPC名", "archive_kind": "offstage|terminal", "reason": "簡短原因"}],
  "merge_npcs": [{"keep": "保留的名稱（全名）", "remove": "要併入的名稱（簡稱或別名）", "reason": "同一角色"}],
  "resolve_events": [{"title": "事件標題", "new_status": "resolved", "reason": "簡短原因"}],
  "remove_inventory": [{"item": "道具名", "reason": "簡短原因"}],
  "remove_abilities": [{"ability": "技能名（完全匹配）", "reason": "簡短原因"}],
  "add_abilities": ["整合後的新技能名稱"],
  "update_systems": [{"key": "體系名", "value": "新描述或 null 表示移除", "reason": "簡短原因"}],
  "clean_relationships": [{"name": "角色名", "action": "archive_note", "reason": "簡短原因"}]
}
```

**全局原則 — 主角專屬（適用於 abilities 和 systems）**：功法與技能（abilities）和體系（systems）只應記錄**主角本人**的能力。描述中明確標註為其他角色承載/學習的條目（如「小琳承載」「小琳學習中」）不屬於主角，應移除。隊友的能力定位已記錄在 NPC 的 role 欄位中。
**例外 — 道具欄（inventory）保留隊友裝備**：由於 NPC 沒有獨立的裝備欄位，inventory 是目前唯一記錄團隊裝備分配的地方。描述為「某某配備」「某某持有」的道具**不要移除**，但值為陣列的裝備清單 key（如 `"主角": [...]`）仍應移除。

規則：
1. **archive_npcs**：將不再活躍的 NPC 標記為歸檔。應歸檔的情況：current_status 描述死亡/已擊殺/已犧牲/已自毀/已退場/已離隊/副本已結束/關係存檔；或明顯屬於「當前副本世界」且當前階段已是主神空間（玩家已離開該副本）。**不要**歸檔玩家持續同伴（relationships 中與玩家有明確羈絆、會跟隨的隊友）。
   - `archive_kind = "offstage"`：暫時離場，未來仍可能再出現（如已退場、已離隊、副本結束但角色未死）。
   - `archive_kind = "terminal"`：永久終局，不應自動回來（如死亡、已損毀、已消散、已合併）。
2. **merge_npcs**：同一角色有多個名稱時（如「卡卡西」與「旗木卡卡西」），保留較完整的名稱（keep），將另一名稱（remove）的資料併入後歸檔 remove。
3. **resolve_events**：已與當前階段無關的舊事件（例如副本專屬事件，且玩家已回主神空間）標為 resolved。new_status 僅用 "resolved" 或 "abandoned"。
4. **remove_inventory**（積極清理）：
   - 已消耗、已無效、或明顯為單次副本用品的道具。
   - **值為陣列的 key**（如 `"主角": [...]`, `"小琳": [...]`）：這是角色裝備清單被錯放為道具欄 key，應全部移除。
   - **重複項**：同一道具以不同 key 出現多次（例如 `"聖光雙槍(簡化版)": "小琳持有"` 與陣列中的同名項），移除冗餘的那個。
   - **非道具**：技能、知識、成就、支線劇情計數、研究參數等不屬於道具欄的資料（如 `"C 級支線劇情": "1"`, `"通靈·靈魂錨定公式": "B級知識"`, `"S 級素材抽獎機會": "1次"`）應移除。
   - **已被系統吸收**：道具已被其他體系/能力整合吸收、不再作為獨立道具存在的條目。注意：「已交給某人」的道具屬於團隊裝備分配，**不要移除**。
5. **remove_abilities**（積極清理，目標是大幅精簡列表）：
   - **重複/同義**：同一技能以不同名稱出現（如「門之鑰 · 術式接管」和「門之鑰：術式接管模式」），保留最完整的，移除其餘。
   - **已被覆蓋**：低階版本已有高階版本時移除低階（如已有「靈魂錨定武裝化 (實踐成果)」則移除「隱藏研究方向：靈魂錨定武裝化」）。
   - **非技能**：稱號、成就、知識圖譜、任務進度等不屬於功法/技能的條目（如「隱藏成就：影之掠奪者」、「稱號：因果玩弄者」、「木葉外圍結界脈絡圖」）應移除。
   - **上級體系已包含**：如果 systems 中已經記錄了某體系（如「萬象召喚 B級」），則其基礎說明型條目（如「萬象召喚（初階）」）可以移除。
   - **碎片化子技能整合**（重要）：同一體系下有大量名稱相似、功能重疊的碎片化條目時，應大量移除並用 `add_abilities` 新增 1-2 個整合條目替代。例如：「空間解析：逆向追蹤」「空間解析：反向重疊模式」「反向傳送導引 (空間特質)」「空間干擾 · 座標鎖死」這四個都屬於門之鑰的空間操控能力，應全部移除，用一個「空間解析系列 (門之鑰衍生)」替代。判斷標準：只有名稱、沒有實際描述內容、且功能高度重疊的條目就應該被整合。
   - ability 字串必須與列表中的完全一致，一字不差。
6. **add_abilities**：配合 remove_abilities 使用。將多個被移除的碎片化子技能整合為一個簡潔的條目。只在整合時使用，不要無故新增技能。
7. **update_systems**：若體系描述過時或需要更新，提供新描述。若要移除體系，value 設為 null。
   - **不屬於主角的體系應移除**（同全局原則）：描述標註為其他角色承載的體系，value 設為 null。
8. **clean_relationships**：可選。對已歸檔 NPC 的關係加註或移除，action 用 "archive_note" 表示僅加註為已歸檔，不強制刪除關係。

若某類沒有要清理的項目，請輸出空陣列 []。不要臆造不存在的名稱。"""


def _build_cleanup_prompt(
    current_phase: str,
    current_dungeon: str,
    npcs: list[dict],
    active_events: list[dict],
    inventory: dict,
    relationships: dict,
    abilities: list,
    systems: dict,
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
    abilities_text = json.dumps(abilities, ensure_ascii=False) if abilities else "[]"
    systems_text = json.dumps(systems, ensure_ascii=False) if systems else "（空）"
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
        "## 功法與技能 (abilities)\n"
        + abilities_text + "\n\n"
        "## 體系 (systems)\n"
        + systems_text + "\n\n"
        "## 人際關係 (relationships)\n"
        + rel_text + "\n\n"
        "## 劇情回顧（節錄）\n"
        + recap_preview + "\n\n"
        "請根據上述規則輸出清理建議的 JSON（只輸出 JSON）。"
    )


def _parse_cleanup_response(response_text: str) -> dict:
    text = (response_text or "").strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    else:
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
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
        "removed_abilities": 0,
        "added_abilities": 0,
        "updated_systems": 0,
        "clean_relationships": 0,
    }

    # archive_npcs
    for item in ops.get("archive_npcs") or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        reason = (item.get("reason") or "cleanup").strip()
        archive_kind = item.get("archive_kind", "terminal")
        if archive_kind not in ("offstage", "terminal"):
            archive_kind = "terminal"
        try:
            app_module._save_npc(
                story_id,
                {
                    "name": name,
                    "lifecycle_status": "archived",
                    "archived_reason": reason,
                },
                branch_id,
                archive_kind=archive_kind,
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
                archive_kind="terminal",
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

    # remove_abilities: use abilities_remove via _apply_state_update_inner
    remove_abilities = [
        (x.get("ability") or "").strip()
        for x in (ops.get("remove_abilities") or [])
        if (x.get("ability") or "").strip()
    ]
    if remove_abilities:
        try:
            schema = app_module._load_character_schema(story_id)
            app_module._apply_state_update_inner(
                story_id,
                branch_id,
                {"abilities_remove": remove_abilities},
                schema,
            )
            summary["removed_abilities"] = len(remove_abilities)
        except Exception as e:
            log.warning("state_cleanup: remove_abilities failed: %s", e)

    # add_abilities: use abilities_add via _apply_state_update_inner
    add_abilities = ops.get("add_abilities") or []
    if isinstance(add_abilities, list):
        add_abilities = [a.strip() for a in add_abilities if isinstance(a, str) and a.strip()]
    else:
        add_abilities = []
    if add_abilities:
        try:
            schema = app_module._load_character_schema(story_id)
            app_module._apply_state_update_inner(
                story_id,
                branch_id,
                {"abilities_add": add_abilities},
                schema,
            )
            summary["added_abilities"] = len(add_abilities)
        except Exception as e:
            log.warning("state_cleanup: add_abilities failed: %s", e)

    # update_systems: set or remove system entries via map operations
    system_updates = {}
    for item in ops.get("update_systems") or []:
        key = (item.get("key") or "").strip()
        if not key:
            continue
        value = item.get("value")
        system_updates[key] = value
    if system_updates:
        try:
            schema = app_module._load_character_schema(story_id)
            app_module._apply_state_update_inner(
                story_id,
                branch_id,
                {"systems": system_updates},
                schema,
            )
            summary["updated_systems"] = len(system_updates)
        except Exception as e:
            log.warning("state_cleanup: update_systems failed: %s", e)

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


def _run_cleanup_core(story_id: str, branch_id: str) -> dict:
    """Core cleanup logic. Returns summary dict. Raises on error."""
    import app as app_module
    from llm_bridge import call_oneshot

    state = app_module._load_character_state(story_id, branch_id)
    npcs = app_module._load_npcs(story_id, branch_id, include_archived=False)
    active_events = get_active_events(story_id, branch_id, limit=50)
    inventory = state.get("inventory") or {}
    if not isinstance(inventory, dict):
        inventory = {}
    relationships = state.get("relationships") or {}
    if not isinstance(relationships, dict):
        relationships = {}
    abilities = state.get("abilities") or []
    if not isinstance(abilities, list):
        abilities = []
    systems = state.get("systems") or {}
    if not isinstance(systems, dict):
        systems = {}
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
        abilities=abilities,
        systems=systems,
        recap=recap,
    )
    full_prompt = _CLEANUP_SYSTEM_PROMPT + "\n\n" + prompt

    if hasattr(app_module, "_trace_llm"):
        app_module._trace_llm(
            stage="state_cleanup_request",
            story_id=story_id,
            branch_id=branch_id,
            source="state_cleanup",
            payload={"prompt_preview": full_prompt},
        )

    t0 = time.time()
    response = call_oneshot(full_prompt)

    if hasattr(app_module, "_trace_llm"):
        app_module._trace_llm(
            stage="state_cleanup_response_raw",
            story_id=story_id,
            branch_id=branch_id,
            source="state_cleanup",
            payload={"raw": response, "elapsed": time.time() - t0},
        )

    if hasattr(app_module, "_log_llm_usage"):
        app_module._log_llm_usage(story_id, "oneshot", time.time() - t0, branch_id=branch_id)

    ops = _parse_cleanup_response(response)
    if not ops:
        log.info("state_cleanup: no ops parsed")
        return {"archived_npcs": 0, "merged_npcs": 0, "resolved_events": 0,
                "removed_inventory": 0, "removed_abilities": 0, "added_abilities": 0,
                "updated_systems": 0, "clean_relationships": 0}

    pre_cleanup_state = app_module._load_character_state(story_id, branch_id)
    summary = _apply_cleanup_operations(story_id, branch_id, ops)
    post_cleanup_state = app_module._load_character_state(story_id, branch_id)
    reconcile_dungeon_entry(story_id, branch_id, pre_cleanup_state, post_cleanup_state)
    reconcile_dungeon_exit(story_id, branch_id, pre_cleanup_state, post_cleanup_state)
    log.info(
        "state_cleanup: applied archived=%d merged=%d resolved=%d"
        " removed_inv=%d removed_abilities=%d added_abilities=%d"
        " updated_systems=%d clean_rel=%d",
        summary["archived_npcs"],
        summary["merged_npcs"],
        summary["resolved_events"],
        summary["removed_inventory"],
        summary.get("removed_abilities", 0),
        summary.get("added_abilities", 0),
        summary.get("updated_systems", 0),
        summary.get("clean_relationships", 0),
    )
    return summary


def run_state_cleanup_sync(story_id: str, branch_id: str) -> dict:
    """Run cleanup synchronously, return summary dict."""
    with _cleanup_lock:
        _last_cleanup[(story_id, branch_id)] = (time.time(), -1)
    return _run_cleanup_core(story_id, branch_id)


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
        try:
            _run_cleanup_core(story_id, branch_id)
        except Exception as e:
            log.warning("state_cleanup: error — %s", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
