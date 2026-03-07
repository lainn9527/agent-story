"""Pure tag extraction and recent-context sanitization helpers."""

import json
import re

_TAG_OPEN = r"(?:<!--|\[)"
_TAG_CLOSE = r"(?:-->|\])"
_STATE_RE = re.compile(_TAG_OPEN + r"STATE\s*(.*?)\s*STATE" + _TAG_CLOSE, re.DOTALL)
_LORE_RE = re.compile(_TAG_OPEN + r"LORE\s*(.*?)\s*LORE" + _TAG_CLOSE, re.DOTALL)
_NPC_RE = re.compile(_TAG_OPEN + r"NPC\s*(.*?)\s*NPC" + _TAG_CLOSE, re.DOTALL)
_EVENT_RE = re.compile(_TAG_OPEN + r"EVENT\s*(.*?)\s*EVENT" + _TAG_CLOSE, re.DOTALL)
_IMG_RE = re.compile(_TAG_OPEN + r"IMG\s+prompt:\s*(.*?)\s*IMG" + _TAG_CLOSE, re.DOTALL)
_DEBUG_ACTION_RE = re.compile(r"<!--DEBUG_ACTION\s*(.*?)\s*DEBUG_ACTION-->", re.DOTALL)
_DEBUG_DIRECTIVE_RE = re.compile(r"<!--DEBUG_DIRECTIVE\s*(.*?)\s*DEBUG_DIRECTIVE-->", re.DOTALL)
_DEBUG_ACTION_TYPES = {"state_patch", "npc_upsert", "npc_delete", "world_day_set", "dungeon_patch"}

_CONTEXT_ECHO_RE = re.compile(
    r"\[(?:長期關鍵事件|命運走向|命運判定|命運骰結果|相關世界設定|相關事件追蹤|NPC 近期動態|GM 敘事計劃（僅供 GM 內部參考，勿透露給玩家）|Debug 修正指令（僅供 GM 內部參考，勿透露給玩家）)\].*?(?=\n---\n|\n\n[^\[\n]|\Z)",
    re.DOTALL,
)

_CHOICE_BLOCK_RE = re.compile(
    r"""
    (?:
        (?:\n|^)
        (?:
            \*{0,2}[^\n]*(?:可選行動|你打算)[^\n]*\*{0,2}\s*\n
        )?
        (?:
            \s*(?:\d+[.)、]|[①②③④⑤⑥⑦⑧⑨⑩])\s*.+(?:\n|$)
        ){2,}
        \s*\Z
    )
    |
    (?:
        (?:\n|^)
        (?:
            \s*-\s*\*{0,2}[「『][^\n」』]{1,80}[」』]\s*[:：]\*{0,2}\s*.+(?:\n|$)
            (?:\s*\n)*
        ){2,}
        \s*\Z
    )
    """,
    re.DOTALL | re.VERBOSE,
)

_FATE_LABEL_RE = re.compile(
    r"#{0,4}\s*\*{0,2}[【\[](?:命運(?:走向|判定)(?:效應|效果|觸發|結果)?|判定(?:結果)?)[:：][^】\]]*[】\]]\*{0,2}\s*"
)
_REWARD_HINT_RE = re.compile(r"【主神提示[:：].*?獎勵點.*?】")

_NORMALIZE_DOTS_RE = re.compile(r"[‧・•]")
_NORMALIZE_DASHES_RE = re.compile(r"[–\-ー]")
_ITEM_BASE_RE = re.compile(r"\s*[（(].*$")
_ITEM_QTY_RE = re.compile(r"\s*[x×]\d+$")


def _extract_state_tag(text: str) -> tuple[str, list[dict]]:
    """Extract all <!--STATE {...} STATE--> tags from GM response."""
    updates: list[dict] = []
    while True:
        m = _STATE_RE.search(text)
        if not m:
            break
        try:
            updates.append(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            pass
        text = text[: m.start()].rstrip() + text[m.end():]
        text = text.strip()
    return text, updates


def _extract_lore_tag(text: str) -> tuple[str, list[dict]]:
    """Extract all <!--LORE {...} LORE--> tags from GM response."""
    lores: list[dict] = []
    while True:
        m = _LORE_RE.search(text)
        if not m:
            break
        try:
            lores.append(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            pass
        text = text[: m.start()].rstrip() + text[m.end():]
        text = text.strip()
    return text, lores


def _extract_npc_tag(text: str) -> tuple[str, list[dict]]:
    """Extract all <!--NPC {...} NPC--> tags from GM response."""
    npcs: list[dict] = []
    while True:
        m = _NPC_RE.search(text)
        if not m:
            break
        try:
            npcs.append(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            pass
        text = text[: m.start()].rstrip() + text[m.end():]
        text = text.strip()
    return text, npcs


def _extract_event_tag(text: str) -> tuple[str, list[dict]]:
    """Extract all <!--EVENT {...} EVENT--> tags from GM response."""
    events: list[dict] = []
    while True:
        m = _EVENT_RE.search(text)
        if not m:
            break
        try:
            events.append(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            pass
        text = text[: m.start()].rstrip() + text[m.end():]
        text = text.strip()
    return text, events


def _extract_img_tag(text: str) -> tuple[str, str | None]:
    """Extract all <!--IMG prompt: ... IMG--> tags from GM response."""
    first_prompt: str | None = None
    while True:
        m = _IMG_RE.search(text)
        if not m:
            break
        prompt = m.group(1).strip()
        if prompt and first_prompt is None:
            first_prompt = prompt
        text = text[: m.start()].rstrip() + text[m.end():]
        text = text.strip()
    return text, first_prompt


def _extract_debug_action_tags(text: str) -> tuple[str, list[dict]]:
    """Extract all <!--DEBUG_ACTION {...} DEBUG_ACTION--> tags from debug response."""
    actions: list[dict] = []
    while True:
        m = _DEBUG_ACTION_RE.search(text)
        if not m:
            break
        try:
            payload = json.loads(m.group(1))
            normalized = _normalize_debug_action_payload(payload)
            if isinstance(normalized, dict):
                actions.append(normalized)
        except (json.JSONDecodeError, ValueError):
            pass
        text = text[: m.start()].rstrip() + text[m.end():]
        text = text.strip()
    return text, actions


def _extract_debug_directive_tags(text: str) -> tuple[str, list[dict]]:
    """Extract all <!--DEBUG_DIRECTIVE {...} DEBUG_DIRECTIVE--> tags from debug response."""
    directives: list[dict] = []
    while True:
        m = _DEBUG_DIRECTIVE_RE.search(text)
        if not m:
            break
        try:
            payload = json.loads(m.group(1))
            if isinstance(payload, dict):
                instruction = str(payload.get("instruction", "")).strip()
                if instruction:
                    directives.append({"instruction": instruction})
        except (json.JSONDecodeError, ValueError):
            pass
        text = text[: m.start()].rstrip() + text[m.end():]
        text = text.strip()
    return text, directives


def _normalize_debug_action_payload(payload: object) -> dict | None:
    """Normalize model-emitted debug action payloads into canonical action objects."""
    if not isinstance(payload, dict):
        return None

    data = dict(payload)
    nested_action = data.get("action")
    if isinstance(nested_action, dict):
        nested = _normalize_debug_action_payload(nested_action)
        if nested:
            return nested

    action_type = str(
        data.get("type") or data.get("action_type") or data.get("kind") or data.get("action") or ""
    ).strip()
    if action_type in _DEBUG_ACTION_TYPES:
        normalized = dict(data)
        normalized["type"] = action_type
        payload_obj = normalized.get("payload")
        if isinstance(payload_obj, dict):
            for k, v in payload_obj.items():
                normalized.setdefault(k, v)

        if action_type == "state_patch":
            state_obj = normalized.get("state_patch")
            patch_obj = normalized.get("patch")
            patch_data_obj = normalized.get("patch_data")
            if not isinstance(normalized.get("update"), dict):
                if isinstance(state_obj, dict):
                    normalized["update"] = state_obj
                elif isinstance(patch_obj, dict):
                    normalized["update"] = patch_obj
                elif isinstance(patch_data_obj, dict):
                    normalized["update"] = patch_data_obj
                else:
                    extras = {
                        k: v
                        for k, v in normalized.items()
                        if k
                        not in {
                            "type",
                            "action_type",
                            "kind",
                            "action",
                            "payload",
                            "state_patch",
                            "patch",
                            "patch_data",
                        }
                    }
                    if extras:
                        normalized["update"] = extras
        elif action_type == "npc_upsert":
            npc_obj = normalized.get("npc_upsert")
            if not isinstance(normalized.get("npc"), dict) and isinstance(npc_obj, dict):
                normalized["npc"] = npc_obj
        elif action_type == "npc_delete":
            if not str(normalized.get("npc_id", "")).strip():
                npc_delete_val = normalized.get("npc_delete")
                if isinstance(npc_delete_val, str) and npc_delete_val.strip():
                    normalized["npc_id"] = npc_delete_val.strip()
        elif action_type == "world_day_set":
            if "world_day" not in normalized and "value" in normalized:
                normalized["world_day"] = normalized.get("value")
        elif action_type == "dungeon_patch":
            dungeon_obj = normalized.get("dungeon_patch")
            if isinstance(dungeon_obj, dict):
                for k, v in dungeon_obj.items():
                    normalized.setdefault(k, v)
        return normalized

    if isinstance(data.get("update"), dict):
        return {"type": "state_patch", "update": data["update"]}
    if isinstance(data.get("patch"), dict):
        return {"type": "state_patch", "update": data["patch"]}
    if isinstance(data.get("patch_data"), dict):
        return {"type": "state_patch", "update": data["patch_data"]}

    if "world_day" in data:
        return {"type": "world_day_set", "world_day": data.get("world_day")}

    if isinstance(data.get("npc"), dict):
        return {"type": "npc_upsert", "npc": data["npc"]}

    npc_id = str(data.get("npc_id", "")).strip()
    if npc_id:
        return {"type": "npc_delete", "npc_id": npc_id}

    dungeon_keys = {
        "progress_delta",
        "mainline_progress_delta",
        "completed_nodes",
        "discovered_areas",
        "explored_area_updates",
    }
    if any(k in data for k in dungeon_keys):
        out = {"type": "dungeon_patch"}
        for k in dungeon_keys:
            if k in data:
                out[k] = data[k]
        return out

    for key in _DEBUG_ACTION_TYPES:
        if key not in data:
            continue
        value = data.get(key)
        if key == "state_patch":
            if isinstance(value, dict):
                if isinstance(value.get("update"), dict):
                    return {"type": "state_patch", "update": value["update"]}
                return {"type": "state_patch", "update": value}
            return None
        if key == "npc_upsert":
            if isinstance(value, dict):
                if isinstance(value.get("npc"), dict):
                    return {"type": "npc_upsert", "npc": value["npc"]}
                return {"type": "npc_upsert", "npc": value}
            return None
        if key == "npc_delete":
            if isinstance(value, dict):
                nested_id = str(value.get("npc_id", "")).strip()
                if nested_id:
                    return {"type": "npc_delete", "npc_id": nested_id}
                return None
            if isinstance(value, str) and value.strip():
                return {"type": "npc_delete", "npc_id": value.strip()}
            return None
        if key == "world_day_set":
            if isinstance(value, dict) and "world_day" in value:
                return {"type": "world_day_set", "world_day": value.get("world_day")}
            return {"type": "world_day_set", "world_day": value}
        if key == "dungeon_patch":
            if isinstance(value, dict):
                out = {"type": "dungeon_patch"}
                for dungeon_key in dungeon_keys:
                    if dungeon_key in value:
                        out[dungeon_key] = value[dungeon_key]
                return out
            return None

    return None


def _strip_fate_from_messages(messages: list[dict]) -> list[dict]:
    """Return a shallow copy of messages with fate direction labels removed."""
    cleaned = []
    for message in messages:
        content = message.get("content", "")
        if _FATE_LABEL_RE.search(content):
            message = {**message, "content": _FATE_LABEL_RE.sub("", content).strip()}
        cleaned.append(message)
    return cleaned


def _strip_choice_block(text: str) -> str:
    """Remove trailing '可選行動' section from a GM message."""
    if not text:
        return text
    return _CHOICE_BLOCK_RE.sub("", text).rstrip()


def _strip_choices_from_messages(messages: list[dict]) -> list[dict]:
    """Return a shallow copy of messages with GM choice blocks removed."""
    cleaned = []
    for message in messages:
        if message.get("role") == "user":
            cleaned.append(message)
            continue
        content = message.get("content", "")
        stripped = _strip_choice_block(content)
        if stripped != content:
            message = {**message, "content": stripped}
        cleaned.append(message)
    return cleaned


def _sanitize_recent_messages(messages: list[dict], *, strip_fate: bool) -> list[dict]:
    """Strip non-narrative scaffolding from recent messages before model input."""
    cleaned = [message for message in messages if message.get("message_type") != "debug_audit"]
    cleaned = _strip_choices_from_messages(cleaned)
    if strip_fate:
        cleaned = _strip_fate_from_messages(cleaned)
    return cleaned


def _extract_item_base_name(item: str) -> str:
    """Extract base name from an item string, stripping status/quantity suffixes."""
    name = item.split(" — ")[0].strip()
    name = _ITEM_QTY_RE.sub("", name).strip()
    name = _ITEM_BASE_RE.sub("", name).strip()
    return name


__all__ = [
    "_TAG_OPEN",
    "_TAG_CLOSE",
    "_STATE_RE",
    "_LORE_RE",
    "_NPC_RE",
    "_EVENT_RE",
    "_IMG_RE",
    "_DEBUG_ACTION_RE",
    "_DEBUG_DIRECTIVE_RE",
    "_DEBUG_ACTION_TYPES",
    "_CONTEXT_ECHO_RE",
    "_CHOICE_BLOCK_RE",
    "_FATE_LABEL_RE",
    "_REWARD_HINT_RE",
    "_NORMALIZE_DOTS_RE",
    "_NORMALIZE_DASHES_RE",
    "_ITEM_BASE_RE",
    "_ITEM_QTY_RE",
    "_extract_state_tag",
    "_extract_lore_tag",
    "_extract_npc_tag",
    "_extract_event_tag",
    "_extract_img_tag",
    "_extract_debug_action_tags",
    "_extract_debug_directive_tags",
    "_normalize_debug_action_payload",
    "_strip_fate_from_messages",
    "_strip_choice_block",
    "_strip_choices_from_messages",
    "_sanitize_recent_messages",
    "_extract_item_base_name",
]
