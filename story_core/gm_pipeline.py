from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
import threading
import time

from story_core.state_updates import (
    _EVENT_STATUS_ORDER,
    _INSTRUCTION_KEYS,
    _SCENE_KEYS,
    _is_numeric_value,
    _normalize_event_sticky_priority,
)

log = logging.getLogger("rpg")


def _app():
    import app as app_module

    return app_module


def _normalize_story_anchor_value(app_module, value: object) -> str:
    normalized = app_module._normalize_story_anchors([value], limit=1)
    return normalized[0] if normalized else ""


def _apply_story_anchor_ops(story_id: str, branch_id: str, raw_ops: object) -> tuple[list[str], list[str]] | None:
    app_module = _app()

    if not isinstance(raw_ops, dict):
        return None

    raw_add = raw_ops.get("add")
    raw_update = raw_ops.get("update")
    raw_remove = raw_ops.get("remove")
    if not isinstance(raw_add, list) and not isinstance(raw_update, list) and not isinstance(raw_remove, list):
        return None

    state = app_module._load_character_state(story_id, branch_id)
    before = app_module._normalize_story_anchors(state.get("story_anchors", []))
    anchors = list(before)

    remove_set = set()
    if isinstance(raw_remove, list):
        for item in raw_remove:
            text = _normalize_story_anchor_value(app_module, item)
            if text:
                remove_set.add(text)
    if remove_set:
        anchors = [anchor for anchor in anchors if anchor not in remove_set]

    replacements: dict[str, str] = {}
    if isinstance(raw_update, list):
        existing_anchor_set = set(anchors)
        for item in raw_update:
            if not isinstance(item, dict):
                continue
            old_text = _normalize_story_anchor_value(app_module, item.get("old"))
            new_text = _normalize_story_anchor_value(app_module, item.get("new"))
            if not old_text or not new_text or old_text == new_text:
                continue
            if old_text in existing_anchor_set:
                replacements[old_text] = new_text
    if replacements:
        anchors = [replacements.get(anchor, anchor) for anchor in anchors]

    additions: list[str] = []
    if isinstance(raw_add, list):
        for item in raw_add:
            text = _normalize_story_anchor_value(app_module, item)
            if text:
                additions.append(text)

    after = app_module._normalize_story_anchors([*anchors, *additions])
    if after == before:
        return None

    state["story_anchors"] = after
    app_module._save_json(app_module._story_character_state_path(story_id, branch_id), state)
    log.info(
        "    story_anchors: %s",
        json.dumps({"before": before, "after": after}, ensure_ascii=False),
    )
    return before, after


def _validate_state_update(update: dict, schema: dict, current_state: dict) -> tuple[dict, list[dict]]:
    """Deterministic validation gate for state updates."""
    app_module = _app()

    sanitized = {}
    violations = []

    schema_add_keys = set()
    schema_remove_keys = set()
    map_type_keys = set()
    for list_def in schema.get("lists", []):
        list_key = list_def["key"]
        if list_def.get("type") == "map":
            map_type_keys.add(list_key)
        schema_add_keys.add(list_def.get("state_add_key") or f"{list_key}_add")
        schema_remove_keys.add(list_def.get("state_remove_key") or f"{list_key}_remove")
    for field in schema.get("fields", []):
        if field.get("type") == "map":
            map_type_keys.add(field["key"])

    direct_overwrite_keys = set(schema.get("direct_overwrite_keys", []))

    for key, value in update.items():
        if key in _SCENE_KEYS:
            violations.append({"key": key, "rule": "scene_key", "value": value, "action": "drop"})
            continue
        if key in _INSTRUCTION_KEYS:
            violations.append({"key": key, "rule": "instruction_key", "value": value, "action": "drop"})
            continue

        if key == "current_phase" and value not in app_module.VALID_PHASES:
            violations.append({"key": key, "rule": "invalid_phase", "value": value, "action": "drop"})
            continue

        if key == "reward_points_delta" and not _is_numeric_value(value):
            violations.append({"key": key, "rule": "non_numeric_delta", "value": value, "action": "drop"})
            continue

        if key == "reward_points" and not _is_numeric_value(value):
            violations.append({"key": key, "rule": "non_numeric_points", "value": value, "action": "drop"})
            continue

        if key in map_type_keys and not isinstance(value, dict):
            violations.append({"key": key, "rule": "map_not_dict", "value": type(value).__name__, "action": "drop"})
            continue

        if key.endswith("_add") or key.endswith("_remove"):
            if key not in schema_add_keys and key not in schema_remove_keys:
                violations.append({"key": key, "rule": "non_schema_add_remove", "value": value, "action": "drop"})
                continue

        if key in schema_add_keys or key in schema_remove_keys:
            if isinstance(value, str):
                value = [value]
            elif not isinstance(value, list):
                violations.append({"key": key, "rule": "add_remove_not_list", "value": type(value).__name__, "action": "drop"})
                continue

        if key.endswith("_delta") and key != "reward_points_delta":
            if not _is_numeric_value(value):
                violations.append({"key": key, "rule": "delta_non_numeric", "value": value, "action": "drop"})
                continue

        if key in direct_overwrite_keys and key != "current_phase":
            if not isinstance(value, str):
                violations.append({"key": key, "rule": "overwrite_not_string", "value": type(value).__name__, "action": "drop"})
                continue

        sanitized[key] = value

    return sanitized, violations


def _review_state_update_llm(
    current_state: dict,
    schema: dict,
    original_update: dict,
    sanitized_update: dict,
    violations: list[dict],
    story_id: str = "",
    branch_id: str = "",
) -> dict | None:
    """Ask the LLM reviewer to repair invalid state keys."""
    app_module = _app()
    from story_core.llm_bridge import call_oneshot

    schema_summary_lines = []
    for field in schema.get("fields", []):
        schema_summary_lines.append(f"  {field['key']}: {field.get('type', 'text')}")
    for list_def in schema.get("lists", []):
        list_type = list_def.get("type", "list")
        schema_summary_lines.append(f"  {list_def['key']}: {list_type}")
        if list_def.get("state_add_key"):
            schema_summary_lines.append(f"    (add: {list_def['state_add_key']})")
    schema_summary = "\n".join(schema_summary_lines)

    prompt = (
        "你是 RPG 角色狀態更新的審核員。GM 產生了一份狀態更新，但其中部分欄位違反規則被擋下。\n"
        "請根據被擋下的內容，判斷是否能修正後保留，或者應該丟棄。\n\n"
        f"## 角色 Schema\n{schema_summary}\n\n"
        f"## 合法 current_phase 值\n{json.dumps(sorted(app_module.VALID_PHASES), ensure_ascii=False)}\n\n"
        f"## 當前角色狀態（節錄）\n{json.dumps({k: current_state[k] for k in list(current_state)[:15]}, ensure_ascii=False, indent=2)}\n\n"
        f"## 原始更新\n{json.dumps(original_update, ensure_ascii=False, indent=2)}\n\n"
        f"## 已通過驗證的部分\n{json.dumps(sanitized_update, ensure_ascii=False, indent=2)}\n\n"
        f"## 被擋下的違規項目\n{json.dumps(violations, ensure_ascii=False, indent=2)}\n\n"
        "## 輸出格式（嚴格 JSON）\n"
        "```json\n"
        "{\n"
        '  "patch": {},\n'
        '  "drop_keys": [],\n'
        '  "reason": ""\n'
        "}\n"
        "```\n\n"
        "規則：\n"
        "- patch: 修正後可保留的 key-value（必須符合 schema 型別）\n"
        "- drop_keys: 確定要丟棄的 key（從 sanitized 中移除）\n"
        "- reason: 一句話說明判斷理由\n"
        "- 不要憑空新增原始更新中沒有的 key\n"
        "- 不要輸出 location/threat_level 等場景型 key\n"
        "- 只輸出 JSON，不要任何解釋\n"
    )

    permit_lock = threading.Lock()
    permit_released = False

    def _release_permit_once():
        nonlocal permit_released
        with permit_lock:
            if permit_released:
                return
            permit_released = True
        app_module._STATE_REVIEW_LLM_SEM.release()

    try:
        if not app_module._STATE_REVIEW_LLM_SEM.acquire(blocking=False):
            log.warning(
                "state_reviewer: inflight limit reached (%d), fallback",
                app_module.STATE_REVIEW_LLM_MAX_INFLIGHT,
            )
            return None

        result_box: dict[str, object] = {"result": None, "error": None}

        def _call():
            try:
                started = time.time()
                result_box["result"] = call_oneshot(prompt)
                if story_id:
                    app_module._log_llm_usage(
                        story_id,
                        "oneshot_state_review",
                        time.time() - started,
                        branch_id=branch_id,
                    )
            except Exception as exc:
                result_box["error"] = exc
            finally:
                _release_permit_once()

        worker = threading.Thread(target=_call, daemon=True)
        worker.start()
        worker.join(app_module.STATE_REVIEW_LLM_TIMEOUT)
        if worker.is_alive():
            log.warning("state_reviewer: LLM timeout (%.1fs), fallback", app_module.STATE_REVIEW_LLM_TIMEOUT)
            _release_permit_once()
            return None

        if result_box["error"] is not None:
            raise result_box["error"]

        result = result_box["result"]
        if not result:
            return None

        result = str(result).strip()
        if result.startswith("```"):
            lines = result.split("\n")
            result = "\n".join(line for line in lines if not line.startswith("```"))

        parsed = json.loads(result)
        if not isinstance(parsed, dict):
            return None

        patch = parsed.get("patch", {})
        drop_keys = parsed.get("drop_keys", [])
        if not isinstance(patch, dict):
            log.warning("state_reviewer: patch is not dict, fallback")
            return None
        if not isinstance(drop_keys, list):
            log.warning("state_reviewer: drop_keys is not list, fallback")
            return None

        allowed_patch_keys = set(original_update.keys()) | set(sanitized_update.keys())
        if patch:
            dropped_patch_keys = [key for key in patch if key not in allowed_patch_keys]
            if dropped_patch_keys:
                log.warning(
                    "state_reviewer: dropped %d out-of-scope patch keys: %s",
                    len(dropped_patch_keys),
                    dropped_patch_keys[:5],
                )
                patch = {key: value for key, value in patch.items() if key in allowed_patch_keys}

        candidate = dict(sanitized_update)
        candidate.update(patch)
        for key in drop_keys:
            if isinstance(key, str):
                candidate.pop(key, None)
        return candidate

    except Exception as exc:
        _release_permit_once()
        log.warning("state_reviewer: failed (%s), fallback", exc)
        return None


def _run_state_gate(
    update: dict,
    schema: dict,
    current_state: dict,
    label: str = "state_gate",
    allow_llm: bool = True,
    story_id: str = "",
    branch_id: str = "",
) -> dict:
    """Run state validation and optional LLM repair."""
    app_module = _app()

    if app_module.STATE_REVIEW_MODE == "off":
        return update

    sanitized, violations = _validate_state_update(update, schema, current_state)
    if violations:
        log.warning(
            "%s: %d violations: %s",
            label,
            len(violations),
            [(violation["key"], violation["rule"]) for violation in violations],
        )

    if app_module.STATE_REVIEW_MODE == "enforce":
        if violations and allow_llm and app_module.STATE_REVIEW_LLM == "on":
            candidate = app_module._review_state_update_llm(
                current_state,
                schema,
                update,
                sanitized,
                violations,
                story_id=story_id,
                branch_id=branch_id,
            )
            if candidate is not None:
                final, second_pass_violations = _validate_state_update(candidate, schema, current_state)
                if second_pass_violations:
                    log.warning(
                        "%s: reviewer output had %d violations, using sanitized",
                        label,
                        len(second_pass_violations),
                    )
                    return sanitized
                log.info("%s: reviewer repaired %d keys", label, len(candidate) - len(sanitized))
                return final
        return sanitized

    return update


def _normalize_state_async(story_id: str, branch_id: str, update: dict, known_keys: set[str]):
    """Background: use LLM to remap non-standard STATE fields, then re-apply."""
    app_module = _app()

    unknown = [key for key in update if key not in known_keys]
    if not unknown:
        return

    def _do_normalize():
        from story_core.llm_bridge import call_oneshot

        prompt = (
            "你是一個 JSON 欄位正規化工具。以下是一個 RPG 角色狀態更新 JSON，"
            "但某些欄位名稱不符合標準。請將它們映射到正確的標準欄位名。\n\n"
            f"標準欄位：{json.dumps(sorted(known_keys), ensure_ascii=False)}\n\n"
            "映射規則：\n"
            "- 任何表示「獲得道具/裝備」的欄位 → 合併至 inventory（map，道具名為 key，狀態為 value）\n"
            "- 任何表示「失去/消耗道具」的欄位 → 合併至 inventory（map，道具名為 key，value 設為 null）\n"
            "- 任何表示「獎勵點變化」的欄位 → reward_points_delta（整數）\n"
            "- 任何表示「完成任務」的欄位 → completed_missions_add（陣列）\n"
            "- 已經是標準欄位名的保持不變\n"
            "- 無法映射的自訂欄位（如 location, threat_level 等描述性狀態）保持原樣\n\n"
            f"原始 JSON：\n{json.dumps(update, ensure_ascii=False, indent=2)}\n\n"
            "請只輸出正規化後的 JSON，不要任何解釋。"
        )

        try:
            app_module._trace_llm(
                stage="state_normalize_request",
                story_id=story_id,
                branch_id=branch_id,
                source="_normalize_unknown_state_keys",
                payload={"prompt": prompt, "original_update": update},
                tags={"mode": "oneshot"},
            )
            started = time.time()
            result = call_oneshot(prompt)
            app_module._log_llm_usage(story_id, "oneshot", time.time() - started, branch_id=branch_id)
            app_module._trace_llm(
                stage="state_normalize_response_raw",
                story_id=story_id,
                branch_id=branch_id,
                source="_normalize_unknown_state_keys",
                payload={"response": result, "usage": app_module.get_last_usage()},
                tags={"mode": "oneshot"},
            )
            if not result:
                return
            result = result.strip()
            if result.startswith("```"):
                lines = result.split("\n")
                result = "\n".join(line for line in lines if not line.startswith("```"))
            normalized = json.loads(result)
            if not isinstance(normalized, dict) or normalized == update:
                return

            log.info("    state_normalize: remapped %d unknown keys, re-applying", len(unknown))
            normalized_schema = app_module._load_character_schema(story_id)
            pre_state = app_module._load_character_state(story_id, branch_id)
            normalized = app_module._run_state_gate(
                normalized,
                normalized_schema,
                pre_state,
                label="state_gate(normalize)",
                allow_llm=False,
                story_id=story_id,
                branch_id=branch_id,
            )
            app_module._apply_state_update_inner(story_id, branch_id, normalized, normalized_schema)
            post_state = app_module._load_character_state(story_id, branch_id)
            app_module.reconcile_dungeon_entry(story_id, branch_id, pre_state, post_state)
            app_module.reconcile_dungeon_exit(story_id, branch_id, pre_state, post_state)
        except Exception as exc:
            log.info("    state_normalize: failed (%s), skipping", exc)

    worker = threading.Thread(target=_do_normalize, daemon=True)
    worker.start()


def _extract_tags_async(
    story_id: str,
    branch_id: str,
    gm_text: str,
    msg_index: int,
    skip_state: bool = False,
    skip_time: bool = False,
):
    """Background: use LLM to extract structured tags from a GM response."""
    app_module = _app()

    if len(gm_text) < 200:
        return

    app_module._mark_extract_pending(story_id, branch_id, msg_index)
    run_ctx = app_module.get_current_run_context(story_id, branch_id)

    def _do_extract():
        from story_core.event_db import get_event_title_map, get_event_titles
        from story_core.llm_bridge import call_oneshot

        try:
            toc = app_module.get_lore_toc(story_id)
            branch_toc = app_module._get_branch_lore_toc(story_id, branch_id)
            if branch_toc:
                toc += "\n（分支設定）\n" + branch_toc
            lore = app_module._load_lore(story_id)
            branch_lore = app_module._load_branch_lore(story_id, branch_id)
            topic_categories = {entry.get("topic", ""): entry.get("category", "") for entry in lore}
            branch_topic_categories = {entry.get("topic", ""): entry.get("category", "") for entry in branch_lore}
            all_topic_categories = {**topic_categories, **branch_topic_categories}
            user_protected = {entry.get("topic", "") for entry in lore if entry.get("edited_by") == "user"}
            existing_titles = get_event_titles(story_id, branch_id)
            existing_title_map = get_event_title_map(story_id, branch_id)
            active_events_text = app_module._build_active_events_hint(story_id, branch_id, limit=40)
            previous_plan = app_module._load_gm_plan(story_id, branch_id)
            previous_plan_text = app_module._summarize_gm_plan_for_prompt(previous_plan, current_index=msg_index)

            schema = app_module._load_character_schema(story_id)
            schema_lines = []
            for field in schema.get("fields", []):
                if field.get("type") == "map":
                    schema_lines.append(
                        f"- {field['key']}（{field.get('label', '')}）: map，直接輸出 {{\"key\": \"value\"}} 覆蓋，null 表示移除"
                    )
                else:
                    schema_lines.append(f"- {field['key']}（{field.get('label', '')}）: {field.get('type', 'text')}")
            for list_def in schema.get("lists", []):
                list_type = list_def.get("type", "list")
                if list_type == "map":
                    schema_lines.append(
                        f"- {list_def['key']}（{list_def.get('label', '')}）: map，直接輸出 {{\"key\": \"value\"}} 覆蓋，null 表示移除"
                    )
                else:
                    add_key = list_def.get("state_add_key", "")
                    remove_key = list_def.get("state_remove_key", "")
                    schema_lines.append(
                        f"- {list_def['key']}（{list_def.get('label', '')}）: list，新增用 {add_key}，移除用 {remove_key}"
                    )
            schema_summary = "\n".join(schema_lines)

            state = app_module._load_character_state(story_id, branch_id)
            existing_state_keys = ", ".join(sorted(state.keys()))
            current_story_anchors = app_module._normalize_story_anchors(state.get("story_anchors", []))
            story_anchors_text = "\n".join(f"- {anchor}" for anchor in current_story_anchors) if current_story_anchors else "（無）"

            list_contents_lines = []
            for list_def in schema.get("lists", []):
                list_type = list_def.get("type", "list")
                list_key = list_def["key"]
                if list_type == "map":
                    items = state.get(list_key, {})
                    if items:
                        list_contents_lines.append(
                            f"{list_def.get('label', list_key)}：{json.dumps(items, ensure_ascii=False)}"
                        )
                else:
                    items = state.get(list_key, [])
                    if items:
                        list_contents_lines.append(
                            f"{list_def.get('label', list_key)}：{json.dumps(items, ensure_ascii=False)}"
                        )
            list_contents_str = "\n".join(list_contents_lines) if list_contents_lines else ""

            prompt = (
                "你是一個 RPG 結構化資料擷取工具。分析以下 GM 回覆，提取結構化資訊。\n\n"
                f"## GM 回覆\n{gm_text}\n\n"
                "## 1. 世界設定（lore）\n"
                "提取**通用世界規則與設定**，這些設定要適用於任何角色、任何分支時間線。\n"
                "**核心判斷標準：GM 在未來的其他場景中是否需要參考這條設定？** 只有「是」才提取。\n"
                "✓ 適合提取：體系或副本的核心規則與運作機制、重要且可重複出現的地點（如總部、主要設施）、商城兌換項目\n"
                "✗ 禁止提取：玩家或特定 NPC 專屬的獨有道具、一次性消耗品、個人技能與強化素材（如『空白的因果律之格』、『紅衣核心』等）。**這些屬於 `inventory` 或 `abilities` 的管轄範圍。Lore 僅保留所有輪迴者皆適用的「客觀世界規律、體系通用設定與常規商城販售物」。**"
                "劇情事件的具體過程（交給 events 追蹤）、"
                "角色的個人數值或進度（如「基因鎖進度15%」「獎勵點5740」）、"
                "角色獲得/失去的具體道具、角色習得的功法與技能進度、角色的戰鬥過程與經歷、角色之間的互動劇情\n"
                "**撰寫原則：**\n"
                "- 用通用語氣（「輪迴者可以…」「該能力的效果是…」），**不要提及具體角色名**\n"
                "- 如果已有設定中有密切相關的主題，**更新該條目**（使用完全相同的 topic 名稱）\n"
                "- 每個條目只涵蓋一個具體概念，content 控制在 200-800 字\n"
                f"已有設定（優先更新而非新建）：\n{toc}\n"
                '格式：[{{"category": "分類", "subcategory": "子分類(選填)", "topic": "主題", "content": "完整描述"}}]\n'
                "可用分類：主神設定與規則/體系/商城/副本世界觀/道具/場景/NPC/故事追蹤\n"
                "- 體系：必須填 subcategory。框架級概念用 subcategory 為體系名 + topic「介紹」（如 subcategory「基因鎖」topic「介紹」）；單一技能用 subcategory「技能」；基礎數值用 subcategory「基本屬性」\n"
                "- 副本世界觀：必須填 subcategory 為副本名（如「生化危機」「咒怨」）\n"
                "- 道具：角色可使用的物品與裝備\n\n"
                "## 2. 事件追蹤（events）\n"
                "提取重要事件：伏筆、轉折、戰鬥、發現等。不要記錄瑣碎事件。\n"
                "**【防幻覺絕對規則】：**\n"
                "* **嚴禁僅因對話中「提及」、「討論」或「回憶」某事件就更改其狀態（例如提及「回歸現實」不代表該事件被 triggered）。**\n"
                "* `triggered`（觸發）：必須是該事件在物理層面、劇情層面產生了**實質性的初步進展或變故**。\n"
                "* `resolved`（解決）：必須是該事件的目標已徹底完成或因故徹底終結。\n"
                "優先輸出 `event_ops`（用 id 更新，避免 title 漂移）：\n"
                f"{active_events_text}\n"
                "- `sticky` 只用於跨弧線 plot pressure：外部威脅/追索、未解契約/承諾、仍在影響劇情的長期伏筆。**身份事實不要放在 events，改放 story_anchors。**\n"
                "- 大多數事件都不是 sticky；只有真正需要長期常駐提醒的事件才標 `sticky: true`\n"
                "- update：已有事件狀態變化（如伏筆被觸發、事件被解決）時，輸出 id + status；若這個事件已成為長期劇情壓力，也可加 `sticky`\n"
                "- create：只有真的新事件才建立（title 不要跟現有事件只差幾個字）；sticky 事件仍然必須是 plot pressure，不可和 story_anchors 重複\n"
                'event_ops 格式：{"update": [{"id": 123, "status": "triggered", "sticky": true}], "create": [{"event_type": "類型", "title": "標題", "description": "描述", "status": "planted", "tags": "關鍵字", "sticky": true}]}\n'
                "- 相容：若你無法使用 event id，才改用 legacy `events` 陣列格式。\n"
                'legacy events 格式：[{"event_type": "類型", "title": "標題", "description": "描述", "status": "planted", "tags": "關鍵字", "sticky": true}]\n'
                "可用類型：伏筆/轉折/遭遇/發現/戰鬥/獲得/觸發\n"
                "可用狀態：planted/triggered/resolved/abandoned（可用同義詞如 ongoing/completed，系統會正規化）\n\n"
                "## 3. GM 敘事計劃（plan）\n"
                "提取 GM 回覆裡隱含的敘事走向，僅供後續 GM 生成時參考，不可透露給玩家。\n"
                "上一輪 GM 計劃（供參考，可全部改寫）：\n"
                f"{previous_plan_text}\n"
                "輸出規則：\n"
                "- arc：當前弧線，1 句話\n"
                "- next_beats：接下來 1-3 個敘事節點（短句）\n"
                "- must_payoff：0-2 個近期必須回收的伏筆\n"
                "- must_payoff.event_title 必須對應目前 active 事件標題；event_id 能確認時才填\n"
                "- ttl_turns 僅可為 1-6（預設 3）\n"
                '格式：{"arc": "弧線", "next_beats": ["節點1", "節點2"], "must_payoff": [{"event_title": "神秘符文", "event_id": 42, "ttl_turns": 3}]}\n\n'
                "## 4. NPC 資料（npcs）\n"
                "提取首次登場或有重大變化的 NPC。\n"
                '- tier：戰力等級（D-/D/D+/C-/C/C+/B-/B/B+/A-/A/A+/S-/S/S+）。'
                "只有在文本明確提及或可直接判定時才填，否則用 null，不要猜測。\n"
                "- 若是已存在的 NPC 且本回合無法判定 tier，請省略 tier 欄位（不要輸出 null 覆蓋）。\n"
                '格式：[{{"name": "名字", "role": "定位", "tier": "D-~S+ 或 null", "appearance": "外觀", '
                '"personality": {{"openness": N, "conscientiousness": N, "extraversion": N, '
                '"agreeableness": N, "neuroticism": N, "summary": "一句話"}}, "backstory": "背景"}}]\n\n'
                "## 5. 角色狀態變化（state）\n"
                f"Schema 告訴你角色有哪些欄位：\n{schema_summary}\n"
                f"角色目前有這些欄位：{existing_state_keys}\n"
                + (f"角色目前的列表內容（含人際關係）：\n{list_contents_str}\n" if list_contents_str else "")
                + "\n規則：\n"
                "- **map 型欄位**（道具欄、人際關係等）：直接輸出 map，同名 key 自動覆蓋\n"
                "  - 道具欄：`inventory: {\"道具名\": \"狀態描述\"}`，進化/變化自動覆蓋同名道具\n"
                "  - 移除道具：`inventory: {\"道具名\": null}`\n"
                "  - 無狀態道具：`inventory: {\"道具名\": \"\"}`\n"
                "- 列表型欄位用 `_add` / `_remove` 後綴（如 `abilities_add`, `abilities_remove`）\n"
                "- 數值型欄位用 `_delta` 後綴（如 `reward_points_delta: -500`）\n"
                "- 文字型欄位直接覆蓋（如 `gene_lock: \"第二階\"`），值要簡短（5-20字）\n"
                "- `current_phase` 只能是：主神空間/副本中/副本結算/傳送中/死亡\n"
                "- `current_dungeon`: 當前所在副本名稱（如「咒術迴戰」「民俗台灣」「鬼滅之刃」）。進入副本時設定，回到主神空間時設為空字串 \"\"。必須與世界設定中的副本分類名一致。\n"
                "- **人際關係**：`relationships: {\"NPC名\": \"新關係描述\"}`。**對照上方的目前關係，如果 GM 文本顯示關係有變化（更親密、敵對、信任等），務必輸出更新**\n"
                "- **體系等級**：`systems: {\"體系名\": \"新等級\"}`。當 GM 文本顯示某體系升級（如 B→A、覺醒、突破等），**必須輸出 systems 更新**，格式為等級 + 新特徵（如 `\"死生之道\": \"A級（漩渦瞳·空間感知）\"`）\n"
                "- 可以新增**永久性角色屬性**（如學會新體系時加 `修真境界`, `法力` 等）\n"
                '- 角色死亡時 `current_phase` 設為 `"死亡"`，`current_status` 設為 `"end"`\n'
                "**【欄位嚴格寫入規則】：**\n"
                "- **`completed_missions`（已完成任務）**：**絕對約束！** 僅限於主神明確發布並結算的「主線/支線任務」與「隱藏成就」。**嚴禁**將「獲得裝備」、「得知情報」、「抵達某地」或「日常行為」視為任務寫入此陣列。\n"
                "- **`relationships`（人際關係與 NPC 狀態）**：不僅記錄好感度與敵友關係。當 GM 文本明確描寫 NPC 的**心理狀態或情緒發生重大轉折**（例如：對未來副本感到恐懼、對玩家產生依賴/警惕），**必須更新該 NPC 的關係描述**，以確保 NPC 具備記憶與情緒連貫性。\n"
                "- **`systems`（體系等級）**：`systems: {\"體系名\": \"新等級\"}`。當 GM 文本顯示某體系升級（如 B→A、覺醒、突破等），必須輸出 systems 更新，格式為等級 + 新特徵（如 `\"死生之道\": \"A級（漩渦瞳·空間感知）\"`）。\n"
                "- **禁止**新增臨時性/場景性欄位（如 location, threat_level, combat_status, escape_options 等一次性描述）\n"
                "\n**道具欄清理原則**（每次提取時都必須遵守）：\n"
                "- **禁止寫入場景/戰鬥狀態**：「戰鬥中」「對峙中」「集結中」「盤旋中」「佔領中」「啟動中」「錄製中」「噴湧中」等臨時狀態不是道具，不要寫入 inventory。這些只是當前回合的敘事描述，下一回合就過時了。\n"
                "- **已消耗/已使用的道具**：設為 null 移除（如 `\"榴彈\": null`）\n"
                "- **已融合到角色/裝備的物品**：不再是獨立道具，設為 null 移除\n"
                "- **召喚物/僕從**：只記錄召喚物的存在、等級和數量（如 `\"僕從軍團\": \"A級模板，約30單位\"`），不要為每個單位的部署狀態各開一條\n"
                "- **隊友的基因鎖/能力狀態**：寫入 relationships，不要寫入 inventory\n"
                "- **道具欄應保持精簡**：如果目前已超過 50 項，優先用 null 清理已消耗、已融合、過時的條目\n"
                "\n**技能列表維護原則**：\n"
                "- **技能升級時必須同時移除舊版本**：用 `abilities_remove` 移除被取代的技能，再用 `abilities_add` 加入新版本。例如「咒靈操術 (C級)」升級為「咒靈操術 (A級)」時，要同時 remove C級版本。\n"
                "- **同一技能的不同描述只保留最新**：如「靈視」「靈視 (解析迷霧)」「靈視·微觀解析」只需保留最高階的一個。\n"
                "- **已被體系（systems）涵蓋的技能不需重複列在 abilities**：如 systems 已有「咒靈操術: A級」，abilities 不需要再列「咒靈操術 (A級)」。\n"
                "優先輸出 `state_ops`（結構化操作，避免覆蓋錯誤）：\n"
                '{"set": {"current_phase": "副本中"}, "delta": {"reward_points": -500}, "map_upsert": {"inventory": {"封印之鏡": "裂痕"}}, "map_remove": {"inventory": ["一次性符"]}, "list_add": {"abilities": ["靈視·微觀解析"]}, "list_remove": {"abilities": ["靈視"]}}\n'
                "- set：直接覆蓋欄位（null 表示不變，不是刪除）\n"
                "- delta：數值增減（reward_points 建議用這個）\n"
                "- map_upsert/map_remove：map 型欄位增修與刪除\n"
                "- list_add/list_remove：list 型欄位增減\n"
                "- 相容：若你無法輸出 state_ops，才輸出 legacy `state` 物件。\n"
                "格式：state/state_ops 只填有變化的欄位。\n\n"
                "## 6. 長期記憶（story_anchors）\n"
                "提取角色/隊伍/故事的**身份層永久事實**。這些內容會常駐進 system prompt，必須非常保守。\n"
                "只允許 4 類：長期主線、核心隊伍關係、永久代價/不可逆變化、長期宿敵/契約/追索壓力。\n"
                "不要把單純 plot pressure 放進 story_anchors；那種放到 sticky events。\n"
                f"目前 story_anchors：\n{story_anchors_text}\n"
                "規則：\n"
                "- `add`：只加入 genuinely new 的跨弧線永久事實\n"
                "- `update`：只有既有 anchor 被故事**明確推翻或明確升級**時才用，而且 old 必須和現有 anchor 完全一致\n"
                "- `remove`：只有該事實被故事**明確否定**時才用，而且文字必須和現有 anchor 完全一致\n"
                "- 大多數回合應該輸出空的 `story_anchors: {}`\n"
                '格式：{"add": ["新 anchor"], "update": [{"old": "舊 anchor", "new": "新 anchor"}], "remove": ["舊 anchor"]}\n\n'
                "## 7. 時間流逝（time）\n"
                "估算這段敘事中經過了多少時間。包含明確跳躍和隱含的時間流逝。\n"
                "- 明確跳躍：「三天後」→ days:3、「那天深夜」→ hours:8、「半個月的苦練」→ days:15\n"
                "- 隱含流逝參考：一場小戰鬥 → hours:1、大型戰役/Boss戰 → hours:3、探索建築/區域 → hours:2、長途移動/趕路 → hours:4、休息/過夜 → hours:8、訓練/修煉 → days:1\n"
                "- 純對話/短暫互動/思考/角色創建/主神空間閒聊不需要輸出。只有場景中有實際行動推進才估算。\n"
                '格式：{"days": N} 或 {"hours": N}（只選一種，優先用 days）\n\n'
                "## 8. 分支標題（branch_title）\n"
                "用 4-8 個中文字總結這段 GM 回覆中**玩家的核心行動或場景轉折**。\n"
                "例如：「七首殺屍測試」「巷道右側突圍」「自省之眼覺醒」「進入蜀山副本」「商城兌換裝備」\n"
                "要求：動作導向、簡潔、不帶標點符號。\n"
                '格式：字串\n\n'
            )

            dungeon_progress = app_module._load_dungeon_progress(story_id, branch_id)
            if dungeon_progress and dungeon_progress.get("current_dungeon"):
                current = dungeon_progress["current_dungeon"]
                template = app_module._load_dungeon_template(story_id, current["dungeon_id"])
                if template:
                    node_list = template["mainline"]["nodes"]
                    nodes_mapping = ", ".join([f"{node['id']}=「{node['title']}」" for node in node_list])
                    areas_str = ", ".join([area["id"] for area in template.get("areas", [])])
                    prompt += (
                        f"## 9. 副本進度（dungeon）\n"
                        f"當前在副本【{template['name']}】中。分析 GM 文本中是否存在：\n"
                        f"- 主線劇情節點的完成（如「成功封印伽椰子」）\n"
                        f"- 新區域的發現或探索（如「進入二樓」、「深入地下室」）\n\n"
                        f"節點 ID 對照（依序）：{nodes_mapping}\n"
                        f"參考區域 ID：{areas_str}\n\n"
                        '格式：\n'
                        '{\n'
                        '  "mainline_progress_delta": 20,\n'
                        '  "completed_nodes": ["node_2"],\n'
                        '  "discovered_areas": ["umbrella_lab"],\n'
                        '  "explored_area_updates": {\n'
                        '    "umbrella_lab": 30\n'
                        '  }\n'
                        '}\n\n'
                        "**重要**：\n"
                        "- 如果沒有明顯的劇情節點完成，不要輸出 completed_nodes\n"
                        "- 如果沒有新區域發現，不要輸出 discovered_areas\n"
                        "- 保守估計進度，避免過度推進（GM 可能只是鋪墊，尚未真正完成目標）\n\n"
                    )

            prompt += (
                "## 輸出\n"
                "JSON 物件，只包含有內容的類型：\n"
                '{"lore": [...], "event_ops": {"update": [...], "create": [...]}, "events": [...], "plan": {...}, "npcs": [...], "state_ops": {...}, "state": {...}, "story_anchors": {"add": [...], "update": [...], "remove": [...]}, "time": {"days": N}, "branch_title": "...", "dungeon": {...}}\n'
                "沒有新資訊的類型省略或用空陣列/空物件。只輸出 JSON。"
            )

            app_module._trace_llm(
                stage="extract_tags_request",
                story_id=story_id,
                branch_id=branch_id,
                message_index=msg_index,
                source="_extract_tags_async",
                payload={"gm_text": gm_text, "prompt": prompt, "skip_state": skip_state, "skip_time": skip_time},
                tags={"mode": "oneshot"},
            )
            started = time.time()
            result = call_oneshot(prompt)
            app_module._log_llm_usage(story_id, "oneshot", time.time() - started, branch_id=branch_id)
            app_module._trace_llm(
                stage="extract_tags_response_raw",
                story_id=story_id,
                branch_id=branch_id,
                message_index=msg_index,
                source="_extract_tags_async",
                payload={"response": result, "usage": app_module.get_last_usage()},
                tags={"mode": "oneshot"},
            )
            if not result:
                return

            result = result.strip()
            if result.startswith("```"):
                lines = result.split("\n")
                result = "\n".join(line for line in lines if not line.startswith("```"))

            try:
                data = json.loads(result)
            except json.JSONDecodeError:
                match = re.search(r"\{.*\}", result, re.DOTALL)
                if not match:
                    log.info("    extract_tags: no JSON found in response, skipping")
                    return
                data = json.loads(match.group())
            if not isinstance(data, dict):
                return

            saved_counts = {
                "lore": 0,
                "events": 0,
                "npcs": 0,
                "state": False,
                "anchors": "no change",
                "plan": "no change",
            }

            pistol = app_module.get_pistol_mode(app_module._story_dir(story_id), branch_id)
            if pistol:
                log.info("    extract_tags: pistol mode ON, skipping lore + events + plan")

            prefix_registry = app_module.build_prefix_registry(story_id)

            for entry in ([] if pistol else data.get("lore", [])):
                topic = entry.get("topic", "").strip()
                category = entry.get("category", "").strip()
                if not topic:
                    continue
                if topic not in all_topic_categories:
                    similar = app_module._find_similar_topic(topic, category, all_topic_categories)
                    if similar:
                        log.info("    lore merge: '%s' → '%s'", topic, similar)
                        entry["topic"] = similar
                resolved_topic = entry.get("topic", topic)
                if resolved_topic in user_protected:
                    log.info("    lore skip (user-edited): '%s'", resolved_topic)
                    continue
                entry["edited_by"] = "auto"
                entry["source"] = {
                    "branch_id": branch_id,
                    "msg_index": msg_index,
                    "excerpt": gm_text[:100],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                app_module._save_branch_lore_entry(story_id, branch_id, entry, prefix_registry=prefix_registry)
                all_topic_categories[entry.get("topic", topic)] = category
                saved_counts["lore"] += 1

            if saved_counts["lore"]:
                app_module.invalidate_prefix_cache(story_id)

            if not pistol:
                event_ops = data.get("event_ops")
                if event_ops is None:
                    event_ops = data.get("events_ops")
                if isinstance(event_ops, dict):
                    saved_counts["events"] += app_module._apply_event_ops(
                        story_id,
                        branch_id,
                        event_ops,
                        msg_index,
                        existing_titles,
                        existing_title_map,
                    )
                else:
                    for event in data.get("events", []):
                        title = event.get("title", "").strip()
                        if not title:
                            continue
                        new_status = app_module._normalize_event_status(event.get("status")) or "planted"
                        sticky_priority = _normalize_event_sticky_priority(
                            event.get("sticky_priority"),
                            event.get("sticky"),
                            default=0,
                        ) or 0
                        if title not in existing_titles:
                            event["message_index"] = msg_index
                            event["status"] = new_status
                            event["sticky_priority"] = sticky_priority
                            new_id = app_module.insert_event(story_id, event, branch_id)
                            existing_titles.add(title)
                            existing_title_map[title] = {
                                "id": new_id,
                                "status": new_status,
                                "sticky_priority": sticky_priority,
                            }
                            saved_counts["events"] += 1
                        else:
                            existing = existing_title_map.get(title, {})
                            old_status = app_module._normalize_event_status(existing.get("status")) or str(existing.get("status", "")).strip()
                            event_id = existing.get("id")
                            old_sticky_priority = int(existing.get("sticky_priority") or 0)
                            if (
                                isinstance(event_id, int)
                                and _EVENT_STATUS_ORDER.get(new_status, -1)
                                > _EVENT_STATUS_ORDER.get(old_status, -1)
                            ):
                                app_module.update_event_status(story_id, event_id, new_status)
                                existing_title_map[title]["status"] = new_status
                                saved_counts["events"] += 1
                            if isinstance(event_id, int) and sticky_priority > old_sticky_priority:
                                app_module.update_event_sticky_priority(story_id, event_id, sticky_priority)
                                existing_title_map[title]["sticky_priority"] = sticky_priority
                                saved_counts["events"] += 1

            if pistol:
                saved_counts["plan"] = "skipped"
            elif "plan" in data:
                plan_data = data.get("plan")
                if isinstance(plan_data, dict):
                    active_event_rows = app_module.get_active_events(story_id, branch_id, limit=80)
                    normalized_plan = app_module._normalize_gm_plan_payload(
                        plan_data,
                        previous_plan=previous_plan,
                        msg_index=msg_index,
                        active_event_rows=active_event_rows,
                    )
                    if normalized_plan is not None:
                        app_module._save_gm_plan(story_id, branch_id, normalized_plan)
                        saved_counts["plan"] = "updated" if normalized_plan else "cleared"
                else:
                    saved_counts["plan"] = "ignored"

            for npc in data.get("npcs", []):
                if npc.get("name", "").strip():
                    app_module._save_npc(
                        story_id,
                        npc,
                        branch_id,
                        origin_dungeon_id=run_ctx["dungeon_id"] if run_ctx else None,
                        origin_run_id=run_ctx["run_id"] if run_ctx else None,
                    )
                    saved_counts["npcs"] += 1

            if not skip_state:
                state_applied = False
                state_ops = data.get("state_ops")
                if isinstance(state_ops, dict):
                    current_state = app_module._load_character_state(story_id, branch_id)
                    ops_update = app_module._state_ops_to_update(state_ops, schema, current_state)
                    if ops_update:
                        app_module._apply_state_update(story_id, branch_id, ops_update)
                        state_applied = True
                if not state_applied:
                    state_update = data.get("state", {})
                    if state_update and isinstance(state_update, dict):
                        app_module._apply_state_update(story_id, branch_id, state_update)
                        state_applied = True
                saved_counts["state"] = state_applied

            anchor_change = _apply_story_anchor_ops(story_id, branch_id, data.get("story_anchors"))
            if anchor_change is not None:
                _before, after = anchor_change
                saved_counts["anchors"] = f"updated ({len(after)})"

            time_data = data.get("time", {})
            if time_data and isinstance(time_data, dict) and not skip_time:
                days = time_data.get("days") or 0
                hours = time_data.get("hours") or 0
                total_days = min(float(days) + float(hours) / 24, 30)
                if total_days > 0:
                    app_module.advance_world_day(story_id, branch_id, total_days)
                    saved_counts["time"] = total_days

            branch_title = data.get("branch_title", "")
            if branch_title and isinstance(branch_title, str):
                branch_title = branch_title.strip()[:20]
                tree = app_module._load_tree(story_id)
                branch_meta = tree.get("branches", {}).get(branch_id)
                if branch_meta and not branch_meta.get("title"):
                    branch_meta["title"] = branch_title
                    app_module._save_tree(story_id, tree)
                    saved_counts["title"] = branch_title

            dungeon_data = data.get("dungeon", {})
            if dungeon_data and isinstance(dungeon_data, dict):
                if dungeon_data.get("mainline_progress_delta") or dungeon_data.get("completed_nodes"):
                    app_module.update_dungeon_progress(
                        story_id,
                        branch_id,
                        {
                            "progress_delta": dungeon_data.get("mainline_progress_delta", 0),
                            "nodes_completed": dungeon_data.get("completed_nodes", []),
                        },
                    )
                    saved_counts["dungeon_progress"] = True
                if dungeon_data.get("discovered_areas") or dungeon_data.get("explored_area_updates"):
                    app_module.update_dungeon_area(
                        story_id,
                        branch_id,
                        {
                            "discovered_areas": dungeon_data.get("discovered_areas", []),
                            "explored_area_updates": dungeon_data.get("explored_area_updates", {}),
                        },
                    )
                    saved_counts["dungeon_area"] = True

            log.info(
                "    extract_tags: saved %d lore, %d events, %d npcs, state %s, anchors %s, time %s, title %s, plan %s, dungeon %s",
                saved_counts["lore"],
                saved_counts["events"],
                saved_counts["npcs"],
                "updated" if saved_counts["state"] else "no change",
                saved_counts.get("anchors", "no change"),
                f"+{saved_counts['time']:.1f}d" if saved_counts.get("time") else "no change",
                repr(saved_counts.get("title", "—")),
                saved_counts.get("plan", "no change"),
                "updated" if saved_counts.get("dungeon_progress") or saved_counts.get("dungeon_area") else "no change",
            )

            if app_module.should_organize(story_id):
                app_module.organize_lore_async(story_id)

        except json.JSONDecodeError as exc:
            log.warning("    extract_tags: JSON parse failed (%s), skipping", exc)
        except Exception:
            log.exception("    extract_tags: failed, skipping")
        finally:
            try:
                app_module._sync_gm_message_snapshot_after_async(story_id, branch_id, msg_index)
            finally:
                app_module._mark_extract_done(story_id, branch_id, msg_index)

    worker = threading.Thread(target=_do_extract, daemon=True)
    worker.start()


def _process_gm_response(
    gm_response: str,
    story_id: str,
    branch_id: str,
    msg_index: int,
    turn_count: int | None = None,
) -> tuple[str, dict | None, dict]:
    """Extract hidden tags from GM response and return cleaned text plus snapshots."""
    app_module = _app()

    gm_response = app_module._CONTEXT_ECHO_RE.sub("", gm_response).strip()
    gm_response = re.sub(r"^---\s*", "", gm_response).strip()
    gm_response = re.sub(r"\n---\n", "\n", gm_response).strip()
    gm_response = app_module._FATE_LABEL_RE.sub("", gm_response).strip()
    gm_response = re.sub(r"\n{3,}", "\n\n", gm_response)

    reward_hints = list(app_module._REWARD_HINT_RE.finditer(gm_response))
    if len(reward_hints) > 1:
        last_hint = reward_hints[-1].group()
        gm_response = app_module._REWARD_HINT_RE.sub("", gm_response) + "\n\n" + last_hint
        gm_response = re.sub(r"\n{3,}", "\n\n", gm_response).strip()

    gm_response, state_updates = app_module._extract_state_tag(gm_response)
    if state_updates:
        old_phase_before_state = app_module._load_character_state(story_id, branch_id).get("current_phase")
    for state_update in state_updates:
        app_module._apply_state_update(story_id, branch_id, state_update)
    if not state_updates:
        log.info("GM response missing STATE tag (msg_index=%d)", msg_index)
    if state_updates:
        new_state = app_module._load_character_state(story_id, branch_id)
        if old_phase_before_state in ("副本中", "副本結算") and new_state.get("current_phase") == "主神空間":
            from story_core.state_cleanup import run_state_cleanup_async

            run_state_cleanup_async(story_id, branch_id, force=True)

    gm_response, lore_entries = app_module._extract_lore_tag(gm_response)
    for lore_entry in lore_entries:
        lore_entry["source"] = {
            "branch_id": branch_id,
            "msg_index": msg_index,
            "excerpt": gm_response[:100],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        lore_entry["edited_by"] = "auto"
        app_module._save_branch_lore_entry(story_id, branch_id, lore_entry)

    gm_response, npc_updates = app_module._extract_npc_tag(gm_response)
    run_ctx = app_module.get_current_run_context(story_id, branch_id)
    for npc_update in npc_updates:
        app_module._save_npc(
            story_id,
            npc_update,
            branch_id,
            origin_dungeon_id=run_ctx["dungeon_id"] if run_ctx else None,
            origin_run_id=run_ctx["run_id"] if run_ctx else None,
        )

    gm_response, event_list = app_module._extract_event_tag(gm_response)
    for event_data in event_list:
        event_data["message_index"] = msg_index
        app_module.insert_event(story_id, event_data, branch_id)

    gm_response, image_prompt = app_module._extract_img_tag(gm_response)
    image_info = None
    if image_prompt:
        branch_config = app_module._load_branch_config(story_id, branch_id)
        if app_module._is_image_gen_enabled(branch_config):
            filename = app_module.generate_image_async(
                story_id,
                image_prompt,
                msg_index,
                model=app_module._get_image_model(branch_config),
            )
            image_info = {"filename": filename, "ready": False}
        else:
            log.info("image_gen disabled by branch config: branch=%s msg_index=%s", branch_id, msg_index)

    had_time_tags = bool(app_module.TIME_RE.search(gm_response))
    gm_response = app_module.process_time_tags(gm_response, story_id, branch_id)
    gm_response_for_async = app_module._strip_choice_block(gm_response)

    app_module._extract_tags_async(
        story_id,
        branch_id,
        gm_response_for_async,
        msg_index,
        skip_state=False,
        skip_time=had_time_tags,
    )

    from story_core.state_cleanup import run_state_cleanup_async, should_run_cleanup

    cleanup_turn = turn_count if turn_count is not None else msg_index
    if should_run_cleanup(story_id, branch_id, cleanup_turn):
        run_state_cleanup_async(story_id, branch_id, force=False, turn_index=cleanup_turn)

    snapshots = {
        "state_snapshot": app_module._load_character_state(story_id, branch_id),
        "npcs_snapshot": app_module._load_npcs(story_id, branch_id, include_archived=True),
        "world_day_snapshot": app_module.get_world_day(story_id, branch_id),
        "dungeon_progress_snapshot": app_module.get_dungeon_progress_snapshot(story_id, branch_id),
    }

    app_module._clear_debug_directive(story_id, branch_id)
    return gm_response, image_info, snapshots


def _find_state_at_index(story_id: str, branch_id: str, target_index: int) -> dict:
    """Walk timeline backwards to find the most recent state snapshot."""
    app_module = _app()

    timeline = app_module.get_full_timeline(story_id, branch_id)
    for message in reversed(timeline):
        if message.get("index", 0) > target_index:
            continue
        if "state_snapshot" in message:
            return message["state_snapshot"]

    default_path = app_module._story_default_character_state_path(story_id)
    state = app_module._load_json(default_path, {})
    if not state:
        state = app_module.copy.deepcopy(app_module.DEFAULT_CHARACTER_STATE)
    return state


def _backfill_forked_state(forked_state: dict, story_id: str, source_branch_id: str):
    """Backfill fields missing from an old state snapshot."""
    app_module = _app()

    if "current_dungeon" not in forked_state:
        source_state = app_module._load_character_state(story_id, source_branch_id)
        forked_state["current_dungeon"] = source_state.get("current_dungeon", "")


def _find_npcs_at_index(story_id: str, branch_id: str, target_index: int) -> list[dict]:
    """Walk timeline backwards to find the most recent NPC snapshot."""
    app_module = _app()

    timeline = app_module.get_full_timeline(story_id, branch_id)
    for message in reversed(timeline):
        if message.get("index", 0) > target_index:
            continue
        if "npcs_snapshot" in message:
            return message["npcs_snapshot"]
    return []


def _find_world_day_at_index(story_id: str, branch_id: str, target_index: int) -> float:
    """Walk timeline backwards to find the most recent world_day snapshot."""
    app_module = _app()

    timeline = app_module.get_full_timeline(story_id, branch_id)
    for message in reversed(timeline):
        if message.get("index", 0) > target_index:
            continue
        if "world_day_snapshot" in message:
            return message["world_day_snapshot"]
    return 0


def _sync_gm_message_snapshot_after_async(story_id: str, branch_id: str, msg_index: int):
    """Refresh a GM message snapshot after async extraction finishes."""
    app_module = _app()

    path = app_module._story_messages_path(story_id, branch_id)
    lock = app_module._get_branch_messages_lock(story_id, branch_id)
    for _attempt in range(5):
        with lock:
            delta = app_module._load_json(path, [])
            updated = False
            for message in delta:
                if message.get("index") != msg_index or message.get("role") != "gm":
                    continue
                message["state_snapshot"] = app_module._load_character_state(story_id, branch_id)
                message["npcs_snapshot"] = app_module._load_npcs(story_id, branch_id, include_archived=True)
                message["world_day_snapshot"] = app_module.get_world_day(story_id, branch_id)
                message["dungeon_progress_snapshot"] = app_module.get_dungeon_progress_snapshot(story_id, branch_id)
                message["snapshot_async_synced_at"] = datetime.now(timezone.utc).isoformat()
                updated = True
                break
            if updated:
                app_module._save_json(path, delta)
                return
        time.sleep(0.1)
    log.info(
        "snapshot_sync: gm message not found (story=%s branch=%s msg=%s)",
        story_id,
        branch_id,
        msg_index,
    )


def _build_augmented_message(
    story_id: str,
    branch_id: str,
    user_text: str,
    character_state: dict | None = None,
    npcs: list[dict] | None = None,
    recent_messages: list[dict] | None = None,
    turn_count: int = 0,
    current_index: int | None = None,
) -> tuple[str, dict | None]:
    """Add lore, events, activities, and dice context to a user message."""
    app_module = _app()

    tree = app_module._load_tree(story_id)
    is_blank = tree.get("branches", {}).get(branch_id, {}).get("blank", False)

    lore_context = None
    if character_state:
        lore_context = {
            "phase": character_state.get("current_phase", ""),
            "status": character_state.get("current_status", ""),
            "dungeon": character_state.get("current_dungeon", ""),
        }
    if npcs is None:
        npcs = app_module._load_npcs(story_id, branch_id)

    lore_query = app_module._build_lore_search_query(
        user_text,
        recent_messages=recent_messages,
        npcs=npcs,
        current_dungeon=(character_state or {}).get("current_dungeon", ""),
    )

    parts = []
    lore = app_module.search_relevant_lore(story_id, lore_query, context=lore_context)
    if lore:
        parts.append(lore)

    branch_lore = app_module._search_branch_lore(story_id, branch_id, lore_query, context=lore_context)
    if branch_lore:
        parts.append(branch_lore)

    if not is_blank:
        sticky_events = app_module.format_sticky_events(story_id, branch_id, limit=4)
        if sticky_events:
            parts.append(sticky_events)
        events = app_module.search_relevant_events(story_id, user_text, branch_id, limit=3)
        if events:
            parts.append(events)
        if current_index is not None:
            plan_block = app_module._build_gm_plan_injection_block(story_id, branch_id, current_index)
            if plan_block:
                parts.append(plan_block)

    directive_block = app_module._build_debug_directive_injection_block(story_id, branch_id)
    if directive_block:
        parts.append(directive_block)

    activities = app_module.get_recent_activities(story_id, branch_id, limit=2)
    if activities:
        parts.append(activities)

    if character_state:
        all_npcs = app_module._load_npcs(story_id, branch_id, include_archived=True)
        must_include = app_module._extract_state_must_include_keys(user_text, character_state, all_npcs)
        state_block = app_module.search_state_entries(
            story_id,
            branch_id,
            user_text,
            token_budget=app_module.STATE_RAG_TOKEN_BUDGET,
            must_include_keys=must_include,
            context=lore_context,
            category_limits={"npc": app_module.STATE_RAG_NPC_LIMIT},
            max_items=app_module.STATE_RAG_MAX_ITEMS,
        )
        if state_block:
            parts.append(state_block)

    if character_state:
        relationships = character_state.get("relationships", {})
        if not isinstance(relationships, dict):
            relationships = {}
        if npcs is None:
            npcs = app_module._load_npcs(story_id, branch_id)
        tier_entries = []
        for npc in npcs:
            tier = app_module._normalize_npc_tier(npc.get("tier"))
            if not tier:
                continue
            category = app_module._classify_npc(npc, relationships)
            if category not in ("hostile", "ally"):
                continue
            category_label = "敵對" if category == "hostile" else "隊友"
            tier_entries.append(f"{npc.get('name', '?')}（{category_label}·{tier}級）")
        if tier_entries:
            parts.append(
                "\n".join(
                    [
                        "[戰力等級提醒]",
                        f"- 已知戰力單位：{'、'.join(tier_entries)}",
                        "- 同級可拉鋸，跨一級低級方明顯劣勢，跨兩級接近碾壓。",
                        "- +/- 只影響同級內強弱；高階能力需呈現代價或限制。",
                    ]
                )
            )

    dice_result = None
    story_dir = app_module._story_dir(story_id)
    if character_state and not app_module.is_gm_command(user_text) and app_module.get_fate_mode(story_dir, branch_id):
        cheat_modifier = app_module.get_dice_modifier(story_dir, branch_id)
        always_win = app_module.get_dice_always_success(story_dir, branch_id)
        dice_result = app_module.roll_fate(
            character_state,
            cheat_modifier=cheat_modifier,
            always_success=always_win,
            turn_count=turn_count,
        )
        parts.append(app_module.format_dice_context(dice_result))

    if parts:
        return "\n".join(parts) + "\n---\n" + user_text, dice_result
    return user_text, dice_result


__all__ = [
    "_validate_state_update",
    "_review_state_update_llm",
    "_run_state_gate",
    "_normalize_state_async",
    "_extract_tags_async",
    "_process_gm_response",
    "_find_state_at_index",
    "_backfill_forked_state",
    "_find_npcs_at_index",
    "_find_world_day_at_index",
    "_sync_gm_message_snapshot_after_async",
    "_build_augmented_message",
]
