"""Debug panel helpers extracted from app_helpers."""

import json
import logging
import os
from datetime import datetime, timezone

from story_core.branch_tree import _next_branch_message_index_fast
from story_core.character_state import _load_character_state
from story_core.dungeon_system import _load_dungeon_progress, update_dungeon_area, update_dungeon_progress
from story_core.gm_plan import _load_gm_plan
from story_core.npc_helpers import _load_npcs, _save_npc
from story_core.state_updates import _apply_state_update
from story_core.state_db import delete_entry as delete_state_entry
from story_core.story_io import (
    _debug_chat_path,
    _debug_directive_path,
    _last_apply_backup_path,
    _load_json,
    _load_tree,
    _save_json,
    _story_npcs_path,
    _upsert_branch_message,
)
from story_core.tag_extraction import _normalize_debug_action_payload
from story_core.world_timer import get_world_day, set_world_day

log = logging.getLogger("rpg")


def _parse_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning("invalid %s=%r, using default %s", name, raw, default)
        return default


DEBUG_CHAT_CONTEXT_COUNT = max(1, _parse_env_int("DEBUG_CHAT_CONTEXT_COUNT", 20))
DEBUG_CHAT_MAX_USER_CHARS = max(200, _parse_env_int("DEBUG_CHAT_MAX_USER_CHARS", 4000))
DEBUG_APPLY_MAX_ACTIONS = max(1, _parse_env_int("DEBUG_APPLY_MAX_ACTIONS", 20))
DEBUG_APPLY_MAX_DIRECTIVES = max(1, _parse_env_int("DEBUG_APPLY_MAX_DIRECTIVES", 20))


def _resolve_debug_unit_id(story_id: str, branch_id: str) -> str:
    """Resolve debug-unit id from branch ancestry."""
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    if branch_id not in branches:
        return branch_id

    cur = branch_id
    visited = set()
    top_blank_id: str | None = None
    while cur is not None and cur not in visited:
        visited.add(cur)
        branch = branches.get(cur)
        if not branch:
            break
        if branch.get("blank"):
            top_blank_id = branch.get("id") or cur
        cur = branch.get("parent_branch_id")

    return top_blank_id or branch_id


def _load_debug_chat(story_id: str, debug_unit_id: str) -> list[dict]:
    data = _load_json(_debug_chat_path(story_id, debug_unit_id), [])
    if not isinstance(data, list):
        return []
    cleaned: list[dict] = []
    for message in data:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = str(message.get("content", "")).strip()
        if not content:
            continue
        cleaned.append({
            "role": role,
            "content": content,
            "created_at": message.get("created_at"),
        })
    return cleaned


def _save_debug_chat(story_id: str, debug_unit_id: str, messages: list[dict]):
    _save_json(_debug_chat_path(story_id, debug_unit_id), messages)


def _append_debug_chat_message(story_id: str, debug_unit_id: str, role: str, content: str):
    if role not in {"user", "assistant"}:
        return
    text = str(content or "").strip()
    if not text:
        return
    chat = _load_debug_chat(story_id, debug_unit_id)
    chat.append({
        "role": role,
        "content": text,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_debug_chat(story_id, debug_unit_id, chat)


def _load_last_apply_backup(story_id: str, debug_unit_id: str) -> dict:
    data = _load_json(_last_apply_backup_path(story_id, debug_unit_id), {})
    return data if isinstance(data, dict) else {}


def _save_last_apply_backup(story_id: str, debug_unit_id: str, backup: dict):
    _save_json(_last_apply_backup_path(story_id, debug_unit_id), backup)


def _clear_last_apply_backup(story_id: str, debug_unit_id: str):
    path = _last_apply_backup_path(story_id, debug_unit_id)
    if os.path.exists(path):
        os.remove(path)


def _load_debug_directive(story_id: str, branch_id: str) -> dict:
    data = _load_json(_debug_directive_path(story_id, branch_id), {})
    if not isinstance(data, dict):
        return {}
    instruction = str(data.get("instruction", "")).strip()
    if not instruction:
        return {}
    result = dict(data)
    result["instruction"] = instruction
    return result


def _save_debug_directive(story_id: str, branch_id: str, directive: dict):
    if not isinstance(directive, dict):
        return
    instruction = str(directive.get("instruction", "")).strip()
    if not instruction:
        _clear_debug_directive(story_id, branch_id)
        return
    payload = dict(directive)
    payload["instruction"] = instruction
    if not payload.get("created_at"):
        payload["created_at"] = datetime.now(timezone.utc).isoformat()
    _save_json(_debug_directive_path(story_id, branch_id), payload)


def _clear_debug_directive(story_id: str, branch_id: str):
    path = _debug_directive_path(story_id, branch_id)
    if os.path.exists(path):
        os.remove(path)


def _copy_debug_directive(story_id: str, from_bid: str, to_bid: str):
    directive = _load_debug_directive(story_id, from_bid)
    if directive:
        _save_debug_directive(story_id, to_bid, directive)
    else:
        _clear_debug_directive(story_id, to_bid)


def _build_debug_directive_injection_block(story_id: str, branch_id: str) -> str:
    directive = _load_debug_directive(story_id, branch_id)
    instruction = str(directive.get("instruction", "")).strip()
    if not instruction:
        return ""
    return "\n".join([
        "[Debug 修正指令（僅供 GM 內部參考，勿透露給玩家）]",
        instruction,
    ])


def _format_debug_recent_messages(messages: list[dict]) -> str:
    if not messages:
        return "（無）"
    lines: list[str] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            label = "玩家"
        elif role in {"gm", "assistant"}:
            label = "GM"
        else:
            label = "系統"
        idx = message.get("index")
        content = str(message.get("content", "")).strip()
        if len(content) > 320:
            content = content[:320].rstrip() + "…"
        lines.append(f"[#{idx}][{label}] {content}")
    return "\n".join(lines)


def _build_debug_system_prompt(story_id: str, branch_id: str, recent_messages: list[dict]) -> str:
    state = _load_character_state(story_id, branch_id)
    npcs = _load_npcs(story_id, branch_id, include_archived=True)
    world_day = get_world_day(story_id, branch_id)
    dungeon_progress = _load_dungeon_progress(story_id, branch_id) or {}
    gm_plan = _load_gm_plan(story_id, branch_id)
    pending_directive = _load_debug_directive(story_id, branch_id)
    recent_text = _format_debug_recent_messages(recent_messages)

    return (
        "你是 RPG Debug 診斷與修正助手。你的任務是：\n"
        "1. 協助檢查狀態/NPC/獎勵點/世界日/副本進度是否一致。\n"
        "2. 可以提供修正提案，但不做劇情演出。\n"
        "3. 修正提案請用標籤輸出：<!--DEBUG_ACTION {json} DEBUG_ACTION-->。\n"
        "4. 劇情修正指令請用標籤輸出：<!--DEBUG_DIRECTIVE {\"instruction\":\"...\"} DEBUG_DIRECTIVE-->。\n"
        "5. 標籤以外的文字會顯示給使用者，請清楚說明判斷依據。\n"
        "6. 若資訊不足，先追問，不要硬猜。\n\n"
        "可用 action type：state_patch / npc_upsert / npc_delete / world_day_set / dungeon_patch。\n"
        "dungeon_patch 可用欄位：progress_delta、completed_nodes、discovered_areas、explored_area_updates。\n\n"
        f"[主聊天最近訊息（唯讀參考）]\n{recent_text}\n\n"
        f"[character_state.json]\n{json.dumps(state, ensure_ascii=False, indent=2)}\n\n"
        f"[npcs.json（含 archived）]\n{json.dumps(npcs, ensure_ascii=False, indent=2)}\n\n"
        f"[world_day]\n{json.dumps(world_day, ensure_ascii=False)}\n\n"
        f"[dungeon_progress.json]\n{json.dumps(dungeon_progress, ensure_ascii=False, indent=2)}\n\n"
        f"[gm_plan.json]\n{json.dumps(gm_plan, ensure_ascii=False, indent=2)}\n\n"
        f"[pending_debug_directive]\n{json.dumps(pending_directive, ensure_ascii=False, indent=2)}\n"
    )


def _pick_latest_debug_directive(directives: object) -> dict | None:
    if not isinstance(directives, list):
        return None
    for item in reversed(directives):
        if not isinstance(item, dict):
            continue
        instruction = str(item.get("instruction", "")).strip()
        if not instruction:
            continue
        return {"instruction": instruction}
    return None


def _apply_debug_action(story_id: str, branch_id: str, action: dict) -> dict:
    normalized = _normalize_debug_action_payload(action)
    if isinstance(normalized, dict):
        action = normalized
    action_type = str(action.get("type", "")).strip()
    if not action_type:
        return {"type": "unknown", "ok": False, "error": "missing action type"}

    if action_type == "state_patch":
        update = action.get("update")
        if not isinstance(update, dict):
            return {"type": action_type, "ok": False, "error": "invalid state update"}
        _apply_state_update(story_id, branch_id, update)
        return {"type": action_type, "ok": True}

    if action_type == "npc_upsert":
        npc = action.get("npc")
        if not isinstance(npc, dict) or not str(npc.get("name", "")).strip():
            return {"type": action_type, "ok": False, "error": "invalid npc payload"}
        _save_npc(story_id, npc, branch_id)
        return {"type": action_type, "ok": True}

    if action_type == "npc_delete":
        npc_id = str(action.get("npc_id", "")).strip()
        if not npc_id:
            return {"type": action_type, "ok": False, "error": "npc_id required"}
        npcs = _load_npcs(story_id, branch_id, include_archived=True)
        removed_names = [npc.get("name", "").strip() for npc in npcs if npc.get("id") == npc_id and npc.get("name")]
        if not removed_names:
            return {"type": action_type, "ok": False, "error": "npc not found"}
        npcs = [npc for npc in npcs if npc.get("id") != npc_id]
        _save_json(_story_npcs_path(story_id, branch_id), npcs)
        for name in removed_names:
            delete_state_entry(story_id, branch_id, category="npc", entry_key=name)
        return {"type": action_type, "ok": True}

    if action_type == "world_day_set":
        world_day = action.get("world_day")
        try:
            day_value = float(world_day)
        except (TypeError, ValueError):
            return {"type": action_type, "ok": False, "error": "invalid world_day"}
        if day_value < 0:
            return {"type": action_type, "ok": False, "error": "world_day must be >= 0"}
        set_world_day(story_id, branch_id, day_value)
        return {"type": action_type, "ok": True}

    if action_type == "dungeon_patch":
        progress = _load_dungeon_progress(story_id, branch_id)
        if not progress or not progress.get("current_dungeon"):
            return {"type": action_type, "ok": False, "error": "no active dungeon"}

        progress_delta_raw = action.get("progress_delta", action.get("mainline_progress_delta"))
        completed_nodes = action.get("completed_nodes", [])
        discovered_areas = action.get("discovered_areas", [])
        explored_updates = action.get("explored_area_updates", {})
        did_update = False

        if progress_delta_raw is not None or completed_nodes:
            if progress_delta_raw is None:
                progress_delta = 0
            else:
                try:
                    progress_delta = int(float(progress_delta_raw))
                except (TypeError, ValueError):
                    return {"type": action_type, "ok": False, "error": "invalid progress_delta"}
            if not isinstance(completed_nodes, list):
                return {"type": action_type, "ok": False, "error": "completed_nodes must be a list"}
            update_dungeon_progress(story_id, branch_id, {
                "progress_delta": progress_delta,
                "nodes_completed": [str(item) for item in completed_nodes if str(item).strip()],
            })
            did_update = True

        if discovered_areas or explored_updates:
            if not isinstance(discovered_areas, list):
                return {"type": action_type, "ok": False, "error": "discovered_areas must be a list"}
            if not isinstance(explored_updates, dict):
                return {"type": action_type, "ok": False, "error": "explored_area_updates must be an object"}
            cleaned_updates = {}
            for area_id, delta in explored_updates.items():
                try:
                    cleaned_updates[str(area_id)] = int(float(delta))
                except (TypeError, ValueError):
                    continue
            update_dungeon_area(story_id, branch_id, {
                "discovered_areas": [str(item) for item in discovered_areas if str(item).strip()],
                "explored_area_updates": cleaned_updates,
            })
            did_update = True

        if not did_update:
            return {"type": action_type, "ok": False, "error": "empty dungeon patch"}
        return {"type": action_type, "ok": True}

    return {"type": action_type, "ok": False, "error": f"unsupported action type: {action_type}"}


def _build_debug_apply_audit_summary(results: list[dict], directive_applied: int) -> str:
    total = len(results)
    success = sum(1 for result in results if result.get("ok"))
    failed_types = [str(result.get("type", "unknown")) for result in results if not result.get("ok")]
    if total == 0:
        summary = "未套用資料修正"
    elif success == 0:
        summary = f"所有修正項目失敗（0/{total}）"
    elif success == total:
        summary = f"已套用 {success}/{total} 項修正"
    else:
        summary = f"已套用 {success}/{total} 項修正（{total - success} 項失敗：{', '.join(failed_types[:5])}）"

    if directive_applied > 0:
        summary += "；已注入劇情指令"
    return summary


def _append_debug_audit_message(story_id: str, branch_id: str, summary: str):
    text = str(summary or "").strip()
    if not text:
        return
    _upsert_branch_message(story_id, branch_id, {
        "role": "system",
        "message_type": "debug_audit",
        "content": text,
        "index": _next_branch_message_index_fast(story_id, branch_id),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


__all__ = [
    "DEBUG_CHAT_CONTEXT_COUNT",
    "DEBUG_CHAT_MAX_USER_CHARS",
    "DEBUG_APPLY_MAX_ACTIONS",
    "DEBUG_APPLY_MAX_DIRECTIVES",
    "_resolve_debug_unit_id",
    "_load_debug_chat",
    "_save_debug_chat",
    "_append_debug_chat_message",
    "_load_last_apply_backup",
    "_save_last_apply_backup",
    "_clear_last_apply_backup",
    "_load_debug_directive",
    "_save_debug_directive",
    "_clear_debug_directive",
    "_copy_debug_directive",
    "_build_debug_directive_injection_block",
    "_format_debug_recent_messages",
    "_build_debug_system_prompt",
    "_pick_latest_debug_directive",
    "_apply_debug_action",
    "_build_debug_apply_audit_summary",
    "_append_debug_audit_message",
]
