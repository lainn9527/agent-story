"""Deterministic consistency gate (evaluation pillar B) — no LLM, no latency.

Splits the GM-authored delta into:
  - clean:   ops that pass structural validation (applied directly)
  - dropped: structurally-invalid ops (scene/instruction/unknown keys, bad types)
  - flagged: high-risk-but-kept ops routed to the async critic (pillar C)

Crucially this does NOT re-implement the lossy transient-marker drop
(`_sanitize_async_state_update`), which was the proximate cause of bug #1
(state-update-incomplete). The GM authored the delta grounded; we keep what is
structurally valid and let the async critic judge semantics. The gate only DROPS
things that are unambiguously wrong (scene keys, type errors, off-schema keys).
"""

from story_core.gm_v2 import relationship as _rel
from story_core.gm_v2.contract import VALID_PHASES

# Mirrors story_core.state_updates (inlined to keep gm_v2 app-free / self-contained).
_SCENE_KEYS = {
    "location", "location_update", "location_details",
    "threat_level", "combat_status", "escape_options", "escape_route",
    "noise_level", "facility_status", "npc_status", "weapons_status",
    "tool_status", "available_escape", "available_locations",
    "status_update", "current_predicament",
}
_INSTRUCTION_KEYS = {
    "inventory_use", "inventory_update", "skill_update",
    "status_change", "state_change", "note", "notes",
}

# state_delta keys whose change is permanent power and must be semantically reviewed.
_RISK_DIRECT_KEYS = {"base_power_level", "gene_lock"}
_RISK_MAP_KEYS = {"systems"}

_NPC_LIFECYCLE = {"active", "offstage", "terminal"}

# Engine-owned state — written ONLY by deterministic engine transitions (enter/exit_dungeon,
# cultivate, catalog buy). The GM (delta-block OR the `set_state` tool) is gate-DROPPED from
# these, so `available(state)` and the game rules can TRUST them. Engine paths call
# apply_state_delta directly (bypassing this gate), so locking here blocks only the GM, never
# the engine. This is why current_dungeon can't be half-recorded any more (the recurring miss)
# and why a beat-id can't leak into current_phase. See doc/architecture_v2.md (引擎獨佔欄位).
_ENGINE_OWNED = {
    "current_dungeon",      # 副本生命週期 — only enter/exit_dungeon
    "completed_dungeons",   # 通關紀錄 — only exit_dungeon
    "technique_mastery",    # 「成」— only cultivate / 解鎖 (GM 永不指定等級)
    "technique_days",       # 修煉累積天數 — only the time engine
}
# current_phase values the engine owns (set by enter/exit_dungeon). The GM may still
# narrate-drive 死亡/傳送中, but NOT the dungeon-lifecycle phases (those half-recorded the bug).
_ENGINE_PHASES = {"主神空間", "副本中", "副本結算"}


def _allowed_phases(schema: dict) -> set:
    """Per-story `current_phase` allowlist — `phases` in the story's character_schema.json,
    unioned with the engine's own lifecycle phases. Falls back to the global VALID_PHASES for
    stories that declare none.

    Without this every world whose phase vocabulary isn't the 主神空間/副本 set (urban_daily's
    日常/事件中, demon_tide's 宗門/虛潮) has EVERY current_phase write dropped as invalid_phase,
    so 階段 silently freezes at the default forever.
    """
    ph = (schema or {}).get("phases")
    if isinstance(ph, list):
        vals = {str(p).strip() for p in ph if str(p).strip()}
        if vals:
            return vals | _ENGINE_PHASES
    return VALID_PHASES


def _engine_owned_key(key: str) -> bool:
    base = key
    for sfx in ("_delta", "_add", "_remove"):
        if base.endswith(sfx):
            base = base[: -len(sfx)]
            break
    return base in _ENGINE_OWNED


def _is_numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _schema_keysets(schema: dict) -> dict:
    fields = schema.get("fields", []) or []
    lists = schema.get("lists", []) or []
    field_keys = {f["key"] for f in fields if "key" in f}
    map_keys = {f["key"] for f in fields if f.get("type") == "map"}
    add_keys: dict[str, str] = {}
    remove_keys: dict[str, str] = {}
    list_keys: set[str] = set()
    for ld in lists:
        key = ld.get("key")
        if not key:
            continue
        list_keys.add(key)
        if ld.get("type") == "map":
            map_keys.add(key)
        else:
            add_keys[ld.get("state_add_key") or f"{key}_add"] = key
            remove = ld.get("state_remove_key")
            if remove:
                remove_keys[remove] = key
    direct = set(schema.get("direct_overwrite_keys", []) or []) | field_keys
    # currency / engine-ledger fields: the GM may NOT absolute-overwrite the balance (that's
    # arithmetic on possibly-stale grounding — the double-count class). It MAY still emit a
    # `<field>_delta` (engine computes current+delta atomically) or use the purchase/reward
    # engine tools. Marked per-story in the schema (`"engine_managed": true` on the field).
    currency_keys = {f["key"] for f in fields if f.get("key") and f.get("engine_managed")}
    known = (
        field_keys | list_keys | set(add_keys) | set(remove_keys) | direct
        | {"reward_points", "reward_points_delta"}
    )
    return {
        "map_keys": map_keys, "add_keys": add_keys, "remove_keys": remove_keys,
        "list_keys": list_keys, "direct": direct, "known": known,
        "currency_keys": currency_keys,
    }


# An LLM sometimes emits junk into a map field: a string spread into single-char keys
# ({"g":"山菇與野果","j":"草藥繃帶"…}) or a list/dict-method name as a key ({"append":[…]}).
# These corrupt inventory/techniques/talents. Real item/功法 names are multi-char (Chinese),
# never a lone ASCII char or a method name — so such keys are always junk and get stripped.
_MAP_METHOD_KEYS = frozenset({
    "append", "extend", "insert", "pop", "remove", "update", "keys", "values",
    "items", "get", "setdefault", "clear", "copy", "fromkeys",
    "__proto__", "constructor", "prototype", "function", "functions",
})


# quote/backtick chars the cheap LLM sometimes bakes INTO a map key string ('"$append"').
_KEY_WRAP = "\"'`“”‘’´ "
# append-style operators the LLM invents when it tries to "add X to a map" — it emits
# {"$append": "血煞魔核"} (Mongo-ish) instead of {"血煞魔核": "..."}; the VALUE is the real item.
_APPEND_OPS = frozenset({"append", "push", "add", "addtoset", "extend", "insert",
                         "set", "each", "concat", "$append", "$push", "$add", "$set", "$each"})


def _key_core(k) -> str:
    """The judged core of a map key: strip wrapping quotes/backticks the LLM baked in,
    so '"$append"' is judged as '$append' and '"辟毒法珠"' as '辟毒法珠'."""
    return str(k).strip().strip(_KEY_WRAP).strip()


def _is_junk_map_key(k) -> bool:
    """A map key that is NOT a real item/技能/關係 name. Judged on the quote-stripped core so a
    quoted operator ('"$append"') is caught — but a quoted REAL item ('"辟毒法珠"') is NOT dropped
    here (its quotes are a separate corruption to clean, not a reason to lose the item)."""
    core = _key_core(k)
    if core.startswith("$"):
        return True  # operator-style ($append/$push/$set…), incl. the quoted "$append" form
    if len(core) == 1 and core.isascii() and core.isalnum():
        return True
    if core.lower() in _MAP_METHOD_KEYS or core.lower().lstrip("$") in _MAP_METHOD_KEYS:
        return True
    return False


def _append_recover_name(k, v):
    """If a junk map key is an append-operator carrying a STRING item name as its value
    (the LLM's botched 'add 血煞魔核 to inventory'), return that name so the gate re-keys it
    as a real item instead of losing it. Else None."""
    core = _key_core(k).lower()
    if (core in _APPEND_OPS or core.lstrip("$") in _APPEND_OPS) and isinstance(v, str) and v.strip():
        return v.strip().strip(_KEY_WRAP).strip() or None
    return None


def _is_slug_flag(k, v) -> bool:
    """English-slug 'present flag' junk: an ASCII-identifier key with a bare boolean value
    ({"qinggang_spirit_sword": true}) — a duplicate of the real Chinese item name an LLM
    sometimes emits alongside it. Boolean-gated so a legit English-IP item (which carries a
    STRING status) is never touched; real item/功法 names are Chinese, not ASCII slugs."""
    s = str(k)
    return (isinstance(v, bool) and s.isascii() and len(s) > 1
            and s[0].isalpha() and s.replace("_", "").isalnum())


def gate_state_delta(state_delta: dict, schema: dict) -> tuple[dict, list[dict], list[dict]]:
    """Return (clean, dropped, flagged) for a state_delta object."""
    ks = _schema_keysets(schema)
    clean: dict = {}
    dropped: list[dict] = []
    flagged: list[dict] = []

    for key, value in (state_delta or {}).items():
        if _engine_owned_key(key):
            # engine-owned: only deterministic transitions may write it (not the GM)
            dropped.append({"key": key, "reason": "engine_owned"})
            continue
        if key in ks["currency_keys"]:
            # delta-only currency: drop the absolute overwrite, keep `<key>_delta` (a different
            # key string, so it never matches here). GM gives the amount; engine owns the ledger.
            dropped.append({"key": key, "reason": "currency_absolute", "value": value,
                            "hint": f"用 {key}_delta 增減或 purchase 工具；GM 不直接覆寫餘額"})
            continue
        if key in _SCENE_KEYS:
            dropped.append({"key": key, "reason": "scene_key"})
            continue
        if key in _INSTRUCTION_KEYS:
            dropped.append({"key": key, "reason": "instruction_key"})
            continue
        if key == "current_phase":
            # corruption guard (beat-ids etc.) + lifecycle phases are engine-owned
            if value not in _allowed_phases(schema):
                dropped.append({"key": key, "reason": "invalid_phase", "value": value})
                continue
            if value in _ENGINE_PHASES:
                dropped.append({"key": key, "reason": "engine_owned_phase", "value": value})
                continue
        if key in ks["map_keys"]:
            if not isinstance(value, dict):
                dropped.append({"key": key, "reason": "map_not_dict"})
                continue
            junk = [(k, v) for k, v in value.items() if _is_junk_map_key(k) or _is_slug_flag(k, v)]
            if junk:
                value = {k: v for k, v in value.items()
                         if not (_is_junk_map_key(k) or _is_slug_flag(k, v))}
                # recover append-operator junk ({"$append":"血煞魔核"}) → re-key the value as a
                # real item, so the GM's botched 'add to map' doesn't silently lose the item.
                recovered = []
                for jk, jv in junk:
                    name = _append_recover_name(jk, jv)
                    if name and name not in value:
                        value[name] = ""
                        recovered.append(name)
                rec = {"key": key, "reason": "junk_map_keys", "removed": [k for k, _ in junk]}
                if recovered:
                    rec["recovered"] = recovered
                dropped.append(rec)
                if not value:
                    continue
        if key.endswith("_add") or key.endswith("_remove"):
            if key not in ks["add_keys"] and key not in ks["remove_keys"]:
                dropped.append({"key": key, "reason": "non_schema_add_remove"})
                continue
            if isinstance(value, str):
                value = [value]
            elif not isinstance(value, list):
                dropped.append({"key": key, "reason": "add_remove_not_list"})
                continue
        if key.endswith("_delta") and key != "reward_points_delta" and not _is_numeric(value):
            dropped.append({"key": key, "reason": "delta_non_numeric", "value": value})
            continue
        if key in ("reward_points", "reward_points_delta") and not _is_numeric(value):
            dropped.append({"key": key, "reason": "non_numeric", "value": value})
            continue
        if (
            key in ks["direct"] and key != "current_phase"
            and not isinstance(value, (str, int, float, bool))
        ):
            dropped.append({"key": key, "reason": "overwrite_bad_type"})
            continue
        if (
            key not in ks["known"]
            and not (key.endswith("_delta") or key.endswith("_add") or key.endswith("_remove"))
        ):
            dropped.append({"key": key, "reason": "unknown_key"})
            continue

        if key == "relationships" and isinstance(value, dict):
            # 關係 value 可為純字串(舊)或 {tier,note}(新)；tier 不在階梯就 flag（不丟，寬鬆）
            for nm, rv in value.items():
                if isinstance(rv, dict) and rv.get("tier") and not _rel.is_valid_tier(rv.get("tier")):
                    flagged.append({"key": "relationships", "npc": nm,
                                    "reason": "bad_relationship_tier", "value": rv.get("tier")})

        if key in _RISK_DIRECT_KEYS:
            flagged.append({"key": key, "reason": "power_field", "value": value})
        elif key in _RISK_MAP_KEYS and key in ks["map_keys"]:
            flagged.append({"key": key, "reason": "systems_change"})

        clean[key] = value

    return clean, dropped, flagged


def gate_npcs(npcs: list) -> tuple[list, list[dict], list[dict]]:
    """Return (clean, dropped, flagged) for the npcs list."""
    clean: list = []
    dropped: list[dict] = []
    flagged: list[dict] = []
    for npc in npcs or []:
        if not isinstance(npc, dict) or not str(npc.get("name", "")).strip():
            dropped.append({"npc": npc, "reason": "missing_name"})
            continue
        lifecycle = npc.get("lifecycle_status")
        if lifecycle is not None and lifecycle not in _NPC_LIFECYCLE:
            flagged.append({"name": npc.get("name"), "reason": "bad_lifecycle", "value": lifecycle})
        if lifecycle == "terminal" and npc.get("memory_add"):
            flagged.append({"name": npc.get("name"), "reason": "terminal_with_memory_add"})
        clean.append(npc)
    return clean, dropped, flagged


def gate_catalog_upsert(items: list) -> tuple[list, list[dict]]:
    """Return (clean, dropped) for catalog_upsert items. Structural checks only;
    vendor-existence + full schema validation happen in catalog_tools.upsert at apply."""
    clean: list = []
    dropped: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            dropped.append({"item": it, "reason": "not_object"})
            continue
        if not str(it.get("id", "")).strip():
            dropped.append({"item": it, "reason": "missing_id"})
            continue
        if not str(it.get("name", "")).strip():
            dropped.append({"item": it, "reason": "missing_name"})
            continue
        if not str(it.get("vendor", "")).strip():
            dropped.append({"item": it, "reason": "missing_vendor"})
            continue
        price = it.get("price")
        if not _is_numeric(price) or price < 0:
            dropped.append({"item": it, "reason": "bad_price"})
            continue
        clean.append(it)
    return clean, dropped


def gate_cultivate(items: list) -> tuple[list, list[dict]]:
    """Return (clean, dropped) for 閉關 cultivate items: a node id + optional `days`
    invested (omitted ⇒ 閉關到下一次突破). The 成 promotion + clock advance + talent
    discount are computed by the time engine (time_tools.cultivate_session) at apply
    — the GM never sets the level (that's the 「GM 心證」 the time economy removes)."""
    clean: list = []
    dropped: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            dropped.append({"item": it, "reason": "not_object"})
            continue
        iid = str(it.get("item_id") or it.get("node") or "").strip()
        if not iid:
            dropped.append({"item": it, "reason": "missing_item_id"})
            continue
        entry = {"item_id": iid}
        d = it.get("days")
        if d is not None:
            ok = (isinstance(d, (int, float)) and not isinstance(d, bool)) or (
                isinstance(d, str) and d.strip().lstrip("-").replace(".", "", 1).isdigit()
            )
            if not ok:
                dropped.append({"item": it, "reason": "bad_days", "value": d})
                continue
            if float(d) < 0:
                dropped.append({"item": it, "reason": "negative_days", "value": d})
                continue
            entry["days"] = float(d)
        clean.append(entry)
    return clean, dropped


def gate_quests(items: list) -> tuple[list, list[dict]]:
    """Validate GM quest-CREATION items: `id` + `title` required; declared reward numeric.
    Only gates 創造（開地圖）—— completion + reward SETTLEMENT is deterministic (engine,
    apply.complete_quest), never inferred here. Reward is DECLARED at creation so the engine
    grants an exact amount later (no LLM arithmetic). See doc/architecture_v2.md §6/§7."""
    clean: list = []
    dropped: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            dropped.append({"item": it, "reason": "not_object"})
            continue
        qid = str(it.get("id") or "").strip()
        title = str(it.get("title") or it.get("name") or "").strip()
        if not qid:
            dropped.append({"item": it, "reason": "missing_id"})
            continue
        if not title:
            dropped.append({"item": it, "reason": "missing_title"})
            continue
        entry = {"id": qid, "title": title,
                 "desc": str(it.get("desc") or it.get("description") or "").strip()}
        rf = it.get("reward_fate")
        if rf is not None:
            if not (isinstance(rf, (int, float)) and not isinstance(rf, bool)) or rf < 0:
                dropped.append({"item": it, "reason": "bad_reward_fate", "value": rf})
                continue
            entry["reward_fate"] = rf
        ri = it.get("reward_items")
        if isinstance(ri, list):
            cleaned = [str(x).strip() for x in ri if str(x).strip()]
            if cleaned:
                entry["reward_items"] = cleaned
        clean.append(entry)
    return clean, dropped


_STORY_ANCHOR_TEXT_CAP = 200


def gate_story_anchors(items) -> tuple[list, list[dict]]:
    """Validate GM long-term-memory additions: a list of non-empty fact strings (a bare
    string or a {"add":[...]} object are tolerated). Each is trimmed + length-capped; blanks
    and intra-batch dupes are dropped. APPEND-ONLY — the GM can record durable facts but not
    edit/remove them (compaction + the critic curate; durable memory only grows). The 12-item
    core limit is enforced at apply (story_anchors is a cross-story core field, not per-schema).
    See doc/architecture_v2.md (長期記憶)."""
    if isinstance(items, dict):
        items = items.get("add") or items.get("anchors") or []
    if isinstance(items, str):
        items = [items]
    clean: list = []
    dropped: list[dict] = []
    seen: set = set()
    for raw in items or []:
        if not isinstance(raw, str):
            dropped.append({"item": raw, "reason": "not_string"})
            continue
        text = raw.strip()[:_STORY_ANCHOR_TEXT_CAP]
        if not text:
            dropped.append({"item": raw, "reason": "empty"})
            continue
        if text in seen:
            continue
        seen.add(text)
        clean.append(text)
    return clean, dropped


# gate_delta moved to ops.py (registry-driven) — it loops DELTA_OPS calling the per-op
# helpers above. Import it from story_core.gm_v2.ops (re-exported via __init__).

__all__ = ["gate_state_delta", "gate_npcs", "gate_catalog_upsert", "gate_cultivate",
           "gate_quests", "gate_story_anchors"]
