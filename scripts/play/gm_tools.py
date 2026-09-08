"""GM play tools — the deterministic muscle for in-session GM play.

This is the single source of truth shared by every front-end:
  - Bash CLI    (gm_pre.py / gm_post.py, or `python gm_tools.py <subcmd>`)
  - MCP adapter (mcp_server.py)            — typed tools inside a session
  - API loop    (headless, deferred)       — same dispatch(), different brain

Design rules (see .claude/skills/play/SKILL.md):
  - app-free: never `import app` / bootstrap Flask. Wrap only app-free modules.
  - thin & permissive: apply_state validates schema keys/types but DELIBERATELY
    skips the lossy transient-marker guards (`_sanitize_async_state_update`,
    `_run_state_gate`). Those guards are the proximate cause of the
    "state-update-incomplete" bug; the GM authors clean grounded deltas, so we
    do not re-inherit them here.
  - explicit NPC lifecycle: status changes only when explicitly set — no
    keyword-sniffing auto-reactivation (the "exited-NPC-reappears" bug).
  - durable per-NPC memory: NPCs carry an append-only `memory` list that
    survives compaction (the "npc-long-term-memory" bug).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

# --- path bootstrap: make `story_core` importable when run as a script ------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from story_core import story_io as sio  # noqa: E402

# ---------------------------------------------------------------------------
# Schema / state loading (app-free; we do NOT call character_state loaders
# because those `from app import ...`)
# ---------------------------------------------------------------------------

_FALLBACK_SCHEMA = {
    "fields": [
        {"key": "name", "type": "text"},
        {"key": "current_phase", "type": "text"},
        {"key": "current_status", "type": "text"},
        {"key": "reward_points", "type": "number"},
    ],
    "lists": [
        {"key": "inventory", "type": "map"},
        {"key": "relationships", "type": "map"},
        {"key": "systems", "type": "map"},
        {"key": "abilities", "state_add_key": "abilities_add", "state_remove_key": "abilities_remove"},
        {"key": "completed_missions", "state_add_key": "completed_missions_add"},
    ],
    "direct_overwrite_keys": [
        "gene_lock", "physique", "spirit", "current_status",
        "current_phase", "current_dungeon",
    ],
}


def load_schema(story_id: str) -> dict:
    return sio._load_json(sio._story_character_schema_path(story_id), _FALLBACK_SCHEMA)


def load_state(story_id: str, branch_id: str) -> dict:
    return sio._load_json(sio._story_character_state_path(story_id, branch_id), {})


def save_state(story_id: str, branch_id: str, state: dict):
    sio._save_json(sio._story_character_state_path(story_id, branch_id), state)


def load_npcs(story_id: str, branch_id: str) -> list[dict]:
    data = sio._load_json(sio._story_npcs_path(story_id, branch_id), [])
    return data if isinstance(data, list) else []


def save_npcs(story_id: str, branch_id: str, npcs: list[dict]):
    sio._save_json(sio._story_npcs_path(story_id, branch_id), npcs)


def _ledger_path(story_id: str, branch_id: str) -> str:
    return os.path.join(sio._branch_dir(story_id, branch_id), "tool_gap_log.jsonl")


def _player_turn_count(story_id: str, branch_id: str) -> int:
    msgs = sio._load_branch_messages(story_id, branch_id)
    return sum(1 for m in msgs if m.get("role") == "user")


def _next_index(story_id: str, branch_id: str) -> int:
    msgs = sio._load_branch_messages(story_id, branch_id)
    return (max((m.get("index", -1) for m in msgs), default=-1)) + 1


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

def _schema_index(schema: dict) -> dict:
    field_keys = {f["key"] for f in schema.get("fields", [])}
    numeric_keys = {f["key"] for f in schema.get("fields", []) if f.get("type") == "number"}
    map_keys = {f["key"] for f in schema.get("fields", []) if f.get("type") == "map"}
    add_keys, remove_keys, list_keys = {}, {}, set()
    for ld in schema.get("lists", []):
        k = ld["key"]
        list_keys.add(k)
        if ld.get("type") == "map":
            map_keys.add(k)
        else:
            add_keys[ld.get("state_add_key") or f"{k}_add"] = k
            rk = ld.get("state_remove_key")
            if rk:
                remove_keys[rk] = k
    direct = set(schema.get("direct_overwrite_keys", [])) | field_keys
    return {
        "field_keys": field_keys, "numeric_keys": numeric_keys, "map_keys": map_keys,
        "add_keys": add_keys, "remove_keys": remove_keys,
        "list_keys": list_keys, "direct": direct,
    }


# ===========================================================================
# TOOL: roll_dice
# ===========================================================================

def roll_dice(story_id: str, branch_id: str, action: str = "", cheat_modifier: int = 0) -> dict:
    """Roll a d100 fate die with attribute modifiers (real RNG → GM-fair).

    Returns a compact dict: raw / effective / outcome + a label-free GM hint.
    """
    from story_core.dice import roll_fate, _OUTCOME_GM_HINTS

    state = load_state(story_id, branch_id)
    turn_count = _player_turn_count(story_id, branch_id) + 1
    r = roll_fate(state, cheat_modifier=cheat_modifier, turn_count=turn_count)
    return {
        "raw": r["raw"],
        "effective": r["effective"],
        "attr_bonus": r["attr_bonus"],
        "outcome": r["outcome"],
        "gm_hint": _OUTCOME_GM_HINTS.get(r["outcome"], ""),
        "turn": turn_count,
    }


# ===========================================================================
# TOOL: search_lore
# ===========================================================================

def search_lore(story_id: str, query: str, limit: int = 3, max_chars: int = 400) -> dict:
    """Retrieve the top relevant lore entries (small output, not the 192KB file).

    Falls back gracefully to an empty result when the lore index is not built —
    the GM then relies on reading world_lore.json on demand (escape hatch).
    """
    entries = []
    note = ""
    try:
        from story_core import lore_db
        rows = lore_db.search_lore(story_id, query, limit=limit)
        for r in rows:
            content = (r.get("content") or "").strip()
            if len(content) > max_chars:
                content = content[:max_chars] + "…"
            entries.append({
                "topic": r.get("topic", ""),
                "content": content,
                "score": r.get("score"),
            })
    except Exception as e:  # pragma: no cover - defensive
        note = f"lore search unavailable: {e}"
    if not entries and not note:
        note = "no lore hit (index empty or no match) — fall back to reading world_lore.json on demand"
    return {"query": query, "entries": entries, "note": note}


# ===========================================================================
# TOOL: apply_state  (thin & permissive — NO lossy guard)
# ===========================================================================

# LLM junk-key guard for map fields (inventory/techniques/talents): a real item/功法 name is
# multi-char Chinese, never a lone ASCII char ({"g":"山菇"…}) or a list/dict-method name
# ({"append":[…]}). Mirrors story_core.gm_v2.gate._is_junk_map_key (kept local — app-free module).
_MAP_METHOD_KEYS = frozenset({
    "append", "extend", "insert", "pop", "remove", "update", "keys", "values",
    "items", "get", "setdefault", "clear", "copy", "fromkeys",
    "__proto__", "constructor", "prototype", "function", "functions",
})


def _is_junk_map_key(k) -> bool:
    s = str(k)
    return (len(s) == 1 and s.isascii() and s.isalnum()) or s in _MAP_METHOD_KEYS


def _is_slug_flag(k, v) -> bool:
    """English-slug 'present flag' junk: ASCII-identifier key + bare boolean value
    ({"qinggang_spirit_sword": true}) — a dup of the real Chinese item name. Boolean-gated so
    legit English-IP items (string status) are untouched."""
    s = str(k)
    return (isinstance(v, bool) and s.isascii() and len(s) > 1
            and s[0].isalpha() and s.replace("_", "").isalnum())


def apply_state(story_id: str, branch_id: str, delta: dict) -> dict:
    """Apply a STATE delta to the branch character state.

    Validates against the schema (legal keys / numeric types) but does NOT run
    the transient-marker drop or the LLM state gate. Unknown keys are reported,
    not silently swallowed.
    """
    if not isinstance(delta, dict):
        return {"error": "delta must be an object", "state": load_state(story_id, branch_id)}

    schema = load_schema(story_id)
    ix = _schema_index(schema)
    state = load_state(story_id, branch_id)
    applied, ignored = [], []

    for key, val in delta.items():
        # numeric delta, e.g. reward_points_delta
        if key.endswith("_delta"):
            base = key[: -len("_delta")]
            try:
                cur = state.get(base, 0) or 0
                state[base] = (cur if isinstance(cur, (int, float)) else 0) + val
                applied.append(f"{base} += {val} -> {state[base]}")
            except Exception:
                ignored.append({"key": key, "reason": "bad numeric delta"})
            continue
        # list add
        if key in ix["add_keys"]:
            tgt = ix["add_keys"][key]
            lst = state.setdefault(tgt, []) if isinstance(state.get(tgt, []), list) else []
            state[tgt] = lst
            for item in (val if isinstance(val, list) else [val]):
                if item not in lst:
                    lst.append(item)
                    applied.append(f"{tgt} + {item}")
            continue
        # list remove
        if key in ix["remove_keys"]:
            tgt = ix["remove_keys"][key]
            lst = state.get(tgt, [])
            if isinstance(lst, list):
                for item in (val if isinstance(val, list) else [val]):
                    if item in lst:
                        lst.remove(item)
                        applied.append(f"{tgt} - {item}")
            continue
        # map merge (inventory / relationships / systems); "" or None removes a key
        if key in ix["map_keys"]:
            m = state.get(key)
            if not isinstance(m, dict):
                m = {}
            if isinstance(val, dict):
                for k, v in val.items():
                    if _is_junk_map_key(k) or _is_slug_flag(k, v):  # LLM junk: single char / method / english-slug flag
                        ignored.append({"key": f"{key}[{k}]", "reason": "junk map key"})
                        continue
                    if v in (None, "", "remove"):
                        m.pop(k, None)
                        applied.append(f"{key}[{k}] removed")
                    else:
                        m[k] = v
                        applied.append(f"{key}[{k}] = {v}")
                state[key] = m
            else:
                ignored.append({"key": key, "reason": "map value must be object"})
            continue
        # direct field overwrite (text/number fields)
        if key in ix["direct"]:
            if key in ix["numeric_keys"] and not isinstance(val, (int, float)):
                ignored.append({"key": key, "reason": "numeric field needs number"})
                continue
            state[key] = val
            applied.append(f"{key} = {val}")
            continue
        # bare list key with list value → treat as add (lenient)
        if key in ix["list_keys"] and isinstance(val, list):
            lst = state.setdefault(key, []) if isinstance(state.get(key, []), list) else []
            state[key] = lst
            for item in val:
                if item not in lst:
                    lst.append(item)
                    applied.append(f"{key} + {item}")
            continue
        ignored.append({"key": key, "reason": "unknown key (not in schema)"})

    save_state(story_id, branch_id, state)
    return {"state": state, "applied": applied, "ignored": ignored}


# ===========================================================================
# TOOL: update_npc  (explicit lifecycle + append-only memory)
# ===========================================================================

_LIFECYCLE = {"active", "offstage", "terminal"}


def _npc_id(npc: dict) -> str:
    return npc.get("id") or "npc_" + "".join(
        c for c in str(npc.get("name", "")) if c.isalnum() or "一" <= c <= "鿿"
    )


def update_npc(story_id: str, branch_id: str, npc) -> dict:
    """Upsert one or more NPCs. Lifecycle only changes when explicitly set.

    Supported per-NPC keys: any profile field, plus
      - lifecycle_status: active|offstage|terminal  (explicit, no keyword sniff)
      - memory_add: str | list[str]  → appended to durable `memory` list
    """
    updates = npc if isinstance(npc, list) else [npc]
    npcs = load_npcs(story_id, branch_id)
    by_id = {_npc_id(n): n for n in npcs}
    results = []

    for u in updates:
        if not isinstance(u, dict) or not u.get("name"):
            results.append({"error": "npc needs a name", "got": u})
            continue
        nid = _npc_id(u)
        existing = by_id.get(nid)
        if existing is None:
            existing = {"id": nid}
            npcs.append(existing)
            by_id[nid] = existing
            created = True
        else:
            created = False

        mem_add = u.pop("memory_add", None)
        for k, v in u.items():
            if k == "lifecycle_status":
                if v in _LIFECYCLE:
                    existing[k] = v
                continue
            existing[k] = v
        if mem_add is not None:
            mem = existing.setdefault("memory", [])
            for entry in (mem_add if isinstance(mem_add, list) else [mem_add]):
                entry = str(entry).strip()
                if entry and entry not in mem:
                    mem.append(entry)
        existing.setdefault("lifecycle_status", "active")
        result = {
            "name": existing.get("name"),
            "id": nid,
            "created": created,
            "lifecycle_status": existing.get("lifecycle_status"),
            "memory_len": len(existing.get("memory", [])),
        }
        # Echo v2 power fields so the GM can confirm 級/剋制 after writing.
        for k in ("level", "rank_label", "domains"):
            if existing.get(k) not in (None, "", [], {}):
                result[k] = existing.get(k)
        results.append(result)

    save_npcs(story_id, branch_id, npcs)
    return {"npcs": results, "total": len(npcs)}


# ===========================================================================
# TOOL: commit_turn  (append messages + tool-gap ledger)
# ===========================================================================

def _attach_gm_snapshots(story_id: str, branch_id: str, gm_index: int) -> None:
    """Embed per-message snapshots on the GM reply (production-pipeline parity).

    Fork/regen/edit reconstruct state at a branch point via the per-message
    snapshots (_find_npcs_at_index / _find_state_at_index / _find_world_day_at_index).
    The harness used to write NONE → a fork from a harness branch reset the cast to
    empty and the recorder re-fabricated every reappearing NPC (wrong persona, lost
    memory). Stamp the post-turn snapshot so harness branches fork faithfully too.
    Best-effort: never let a snapshot failure break the turn commit."""
    try:
        msgs = sio._load_branch_messages(story_id, branch_id)
        msg = next((m for m in msgs if m.get("index") == gm_index and m.get("role") == "gm"), None)
        if not msg:
            return
        msg["state_snapshot"] = load_state(story_id, branch_id)
        msg["npcs_snapshot"] = load_npcs(story_id, branch_id)  # full roster incl. archived
        try:
            from story_core.world_timer import get_world_day
            msg["world_day_snapshot"] = get_world_day(story_id, branch_id)
        except Exception:
            pass
        try:
            from story_core.dungeon_system import get_dungeon_progress_snapshot
            msg["dungeon_progress_snapshot"] = get_dungeon_progress_snapshot(story_id, branch_id)
        except Exception:
            pass
        sio._upsert_branch_message(story_id, branch_id, msg)
    except Exception:
        pass


def commit_turn(story_id: str, branch_id: str, user_msg: str, gm_msg: str,
                dice: dict | None = None, freehand: list | None = None) -> dict:
    """Append the player action and GM reply; record off-tool actions."""
    base = _next_index(story_id, branch_id)
    user_entry = {"role": "user", "content": user_msg, "index": base}
    if dice:
        user_entry["dice"] = dice
    gm_entry = {"role": "gm", "content": gm_msg, "index": base + 1}
    sio._upsert_branch_message(story_id, branch_id, user_entry)
    sio._upsert_branch_message(story_id, branch_id, gm_entry)
    # state/npcs are already applied by post_turn before this call → snapshot is
    # post-turn (world_day may still advance after; post_turn re-stamps it then).
    _attach_gm_snapshots(story_id, branch_id, base + 1)
    # Snapshot the UI ops this turn acknowledges → turn_ops[gm_index], so a later
    # regenerate of this turn replays them (parity with gm_v2's _ack_ops; the harness
    # otherwise never persisted them and regen lost the player's 購買/兌換 — same gap
    # class as the per-message state/npcs snapshot). pending_ops still in place here
    # (the GM's separate ops_ack clears it after narrating).
    try:
        import catalog_tools
        _pend = catalog_tools.ops_pending(story_id, branch_id).get("ops", [])
        if _pend:
            catalog_tools.save_turn_ops(story_id, branch_id, base + 1, _pend)
    except Exception:
        pass

    if freehand:
        path = _ledger_path(story_id, branch_id)
        with open(path, "a", encoding="utf-8") as f:
            for item in freehand:
                rec = item if isinstance(item, dict) else {"action": str(item)}
                rec.setdefault("turn", base)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # compaction signal
    from story_core import compaction
    msgs = sio._load_branch_messages(story_id, branch_id)
    recap = compaction.load_recap(story_id, branch_id)
    should = compaction.should_compact(recap, len(msgs))
    return {
        "user_index": base, "gm_index": base + 1, "total": len(msgs),
        "freehand_logged": len(freehand or []),
        "should_compact": should,
    }


# ===========================================================================
# TOOL: recap  (GM authors the summary; no LLM bridge call)
# ===========================================================================

def recap(story_id: str, branch_id: str, recap_text: str | None = None,
          compacted_through_index: int | None = None) -> dict:
    """Read or write the branch recap. The GM authors recap_text directly."""
    from story_core import compaction
    if recap_text is not None:
        data = compaction.load_recap(story_id, branch_id)
        data["recap_text"] = recap_text
        if compacted_through_index is not None:
            data["compacted_through_index"] = compacted_through_index
        compaction.save_recap(story_id, branch_id, data)
    msgs = sio._load_branch_messages(story_id, branch_id)
    data = compaction.load_recap(story_id, branch_id)
    return {
        "recap_text": compaction.get_recap_text(story_id, branch_id),
        "compacted_through_index": data.get("compacted_through_index", -1),
        "total_messages": len(msgs),
        "should_compact": compaction.should_compact(data, len(msgs)),
    }


# ===========================================================================
# TOOL: seed_branch
# ===========================================================================

def register_branch(story_id: str, branch_id: str, name: str | None = None) -> dict:
    """Register a play branch in timeline_tree.json so the existing Flask app
    can LIST and DISPLAY it (read-only viewing in the main UI).

    Registered as a `blank` branch (branch_point_index = -1): no parent
    inheritance (get_full_timeline returns only this branch's own messages), and
    `blank` makes it exempt from the startup incomplete-branch cleanup
    (migrations._cleanup_incomplete_branches). Marked `play_harness: true` so the
    UI can flag it read-only. Idempotent.

    This is Mode-A app-visibility only — it does NOT build state.db or message
    snapshots; just enough for /api/branches + /api/messages + /api/status +
    /api/npcs to render the branch.
    """
    tree = sio._load_tree(story_id)
    if not isinstance(tree, dict) or "main" not in tree.get("branches", {}):
        return {"error": f"story '{story_id}' has no timeline_tree/main; not registering"}
    branches = tree["branches"]
    tree.setdefault("active_branch_id", "main")
    if not name:
        name = (load_state(story_id, branch_id).get("name") or "").strip() or branch_id

    existing = branches.get(branch_id)
    if existing:
        existing.setdefault("blank", True)
        existing["play_harness"] = True
        if not existing.get("name"):
            existing["name"] = name
        sio._save_tree(story_id, tree)
        return {"registered": False, "already": True, "branch_id": branch_id, "name": existing.get("name")}

    branches[branch_id] = {
        "id": branch_id,
        "name": name,
        "parent_branch_id": "main",
        "branch_point_index": -1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_id": None,
        "blank": True,
        "play_harness": True,
    }
    sio._save_tree(story_id, tree)
    return {"registered": True, "branch_id": branch_id, "name": name}


def seed_branch(story_id: str, branch_id: str, mode: str = "A", name: str | None = None) -> dict:
    """Create a fresh branch with seeded state/messages/npcs/recap files."""
    bdir = sio._branch_dir(story_id, branch_id)
    os.makedirs(bdir, exist_ok=True)

    default_state = sio._load_json(sio._story_default_character_state_path(story_id), {})
    state = dict(default_state) if isinstance(default_state, dict) else {}
    if name:
        state["name"] = name
    state_path = sio._story_character_state_path(story_id, branch_id)
    if not os.path.exists(state_path):
        sio._save_json(state_path, state)

    msg_path = sio._story_messages_path(story_id, branch_id)
    if not os.path.exists(msg_path):
        sio._save_json(msg_path, [])
    npc_path = sio._story_npcs_path(story_id, branch_id)
    if not os.path.exists(npc_path):
        sio._save_json(npc_path, [])

    return {
        "story_id": story_id, "branch_id": branch_id, "mode": mode,
        "dir": bdir, "state": state,
        "system_prompt_path": sio._story_system_prompt_path(story_id),
        # Make the branch visible (read-only) in the existing Flask app UI.
        "registered": register_branch(story_id, branch_id, name=name),
        # First turn's die, so the opening flow already holds it (dice pipeline).
        "next_dice": roll_dice(story_id, branch_id),
    }


# ===========================================================================
# Combined half-turn helpers (what gm_pre.py / gm_post.py call) — fewer
# subprocess round-trips = fewer tokens.
# ===========================================================================

def _matches_current_scope(home_scope, state) -> bool:
    """True if an offstage NPC's home_scope matches the current world/秘境 → recall-eligible.

    In a 秘境 (state.current_dungeon set): match NPCs whose home_scope names that 秘境.
    In the home world (no current_dungeon): match `本世界` NPCs.
    """
    hs = str(home_scope or "").strip()
    if not hs:
        return False
    dungeon = str((state or {}).get("current_dungeon") or "").strip()
    if dungeon:
        body = hs
        for sep in (":", "："):
            if sep in body:
                body = body.split(sep, 1)[1]
                break
        for br in ("（", "("):
            if br in body:
                body = body.split(br, 1)[0]
                break
        body = body.strip()
        return bool(body) and (body in dungeon or dungeon in body)
    return "本世界" in hs


def _active_npc_brief(story_id: str, branch_id: str, limit: int = 12) -> list[dict]:
    """PRESENT cast only (lifecycle=active) — full brief for the current scene."""
    rels = (load_state(story_id, branch_id) or {}).get("relationships") or {}
    out = []
    for n in load_npcs(story_id, branch_id):
        if n.get("lifecycle_status", "active") != "active":
            continue
        brief = {
            "name": n.get("name"),
            "role": n.get("role", ""),
            "memory": (n.get("memory") or [])[-3:],
        }
        # Persona (性格/口吻) so the GM keeps the NPC in character + on-voice every turn.
        persona = n.get("personality")
        if isinstance(persona, dict):  # tolerate legacy Big5 object form
            persona = persona.get("summary")
        if persona:
            brief["personality"] = persona
        if n.get("speech_style"):
            brief["speech_style"] = n.get("speech_style")
        # player↔NPC relationship — re-grounded so it EVOLVES consistently (性格固定，關係可變)
        rv = rels.get(n.get("name"))
        if rv:
            if isinstance(rv, dict):
                t, note = rv.get("tier", ""), (rv.get("note") or rv.get("desc") or "")
                brief["relationship_to_player"] = (t + ("（" + note + "）" if note else "")) if t else note
            else:
                brief["relationship_to_player"] = rv
        # Surface v2 power fields so the GM sees 級/剋制 (戰力紀律).
        for k in ("level", "rank_label", "domains", "concealment"):
            if n.get(k) not in (None, "", [], {}):
                brief[k] = n.get(k)
        out.append(brief)
        if len(out) >= limit:
            break
    return out


def _offstage_recall_brief(story_id: str, branch_id: str, state: dict, limit: int = 20) -> list[dict]:
    """OFFSTAGE roster (compact) so the GM stays AWARE of who can return.

    `recall=True` flags NPCs whose `home_scope` matches the current world/秘境 — the
    GM should consider re-introducing them (then set lifecycle=active explicitly).
    terminal NPCs are excluded (gone for good).
    """
    out = []
    for n in load_npcs(story_id, branch_id):
        if n.get("lifecycle_status") != "offstage":
            continue
        out.append({
            "name": n.get("name"),
            "home_scope": n.get("home_scope", ""),
            "level": n.get("level"),
            "rank_label": n.get("rank_label", ""),
            "recall": _matches_current_scope(n.get("home_scope"), state),
            "memory": (n.get("memory") or [])[-1:],
        })
    out.sort(key=lambda r: (not r["recall"],))  # recall-eligible first
    return out[:limit]


def pre_turn(story_id: str, branch_id: str, action: str, lore_limit: int = 3) -> dict:
    """Pre-narration in ONE call: dice + lore + current state + on-stage NPCs.

    Returning state/NPCs here lets the GM narrate AND build the state delta
    without any extra Read round-trips — the main per-turn latency lever.
    """
    st = load_state(story_id, branch_id)
    out = {
        "dice": roll_dice(story_id, branch_id, action=action),
        "lore": search_lore(story_id, action, limit=lore_limit),
        "state": st,
        "active_npcs": _active_npc_brief(story_id, branch_id),        # 在場 (present)
        "offstage": _offstage_recall_brief(story_id, branch_id, st),  # 離場·可召回
    }
    try:  # world clock + 距 deadline，so the GM feels the 魔潮 pressure on resume
        import time_tools
        ts = time_tools.status(story_id, branch_id)
        out["time_status"] = {"now": ts.get("now"), "unit": ts.get("unit"), "upcoming": ts.get("upcoming")}
    except Exception:
        pass
    return out


def post_turn(story_id: str, branch_id: str, payload: dict) -> dict:
    """Post-narration commit: apply state, NPCs, messages, recap-check in one call.

    payload keys: user_msg, gm_msg, dice, state_delta, npcs, freehand, recap_text

    Also PRE-ROLLS the next turn's fate die and returns it as `next_dice`.
    `roll_fate` ignores the action, so a die rolled one turn ahead is identical
    to one rolled at the start of the next turn. Holding it in-context lets the
    GM open the next turn by narrating immediately — no pre-call — collapsing
    steady-state play to ONE round-trip/turn with the story shown first.
    """
    out = {}
    if payload.get("state_delta"):
        r = apply_state(story_id, branch_id, payload["state_delta"])
        out["state"] = r["state"]
        out["state_applied"] = r["applied"]
        out["state_ignored"] = r["ignored"]
    if payload.get("npcs"):
        out["npc"] = update_npc(story_id, branch_id, payload["npcs"])
    if payload.get("recap_text"):
        out["recap"] = recap(story_id, branch_id, recap_text=payload["recap_text"])
    out["commit"] = commit_turn(
        story_id, branch_id,
        payload.get("user_msg", ""), payload.get("gm_msg", ""),
        dice=payload.get("dice"), freehand=payload.get("freehand"),
    )
    # Time economy (Phase 3): 閉關 (cultivate) + general time passage (time) advance
    # the SAME world clock and fire deadlines (魔潮). The engine promotes 成 by
    # accumulated days — the GM does NOT set the level. Status + any fired deadline
    # are surfaced back (like pending_ops) for the next turn.
    try:
        import time_tools
        fired: list = []
        for c in (payload.get("cultivate") or []):
            if isinstance(c, dict) and c.get("item_id"):
                r = time_tools.cultivate_session(story_id, branch_id, str(c["item_id"]), c.get("days"))
                if r.get("ok"):
                    out.setdefault("cultivate", []).append({
                        "item_id": c["item_id"], "mastery": r.get("mastery_label"),
                        "spent": r.get("spent"), "promoted": r.get("promoted"),
                    })
                    fired += r.get("fired") or []
        t = payload.get("time")
        days = 0.0
        if isinstance(t, dict):
            days = float(t.get("days") or 0) + float(t.get("hours") or 0) / 24
        elif isinstance(t, (int, float)) and not isinstance(t, bool):
            days = float(t)
        if days > 0:
            r = time_tools.spend(story_id, branch_id, min(days, 365))
            out["time"] = {"now": r.get("now"), "spent": r.get("spent")}
            fired += r.get("fired") or []
        ts = time_tools.status(story_id, branch_id)
        out["time_status"] = {"now": ts.get("now"), "unit": ts.get("unit"), "upcoming": ts.get("upcoming")}
        if fired:
            out["deadlines_fired"] = fired
    except Exception:
        pass
    # Re-stamp the GM message snapshot AFTER time economy (cultivate/time advanced
    # the world clock post-commit) so world_day_snapshot reflects this turn's spend.
    try:
        _attach_gm_snapshots(story_id, branch_id, out["commit"]["gm_index"])
    except Exception:
        pass
    # Pre-roll next turn's die from the now-updated state. commit_turn already
    # appended this turn's user msg, so turn_count lands on the NEXT turn.
    out["next_dice"] = roll_dice(story_id, branch_id)
    # Surface out-of-band UI ops (catalog buy/refund) so the GM can acknowledge
    # them next turn — folded into the commit response = zero extra round-trip.
    try:
        import catalog_tools
        _pend = catalog_tools.ops_pending(story_id, branch_id).get("ops", [])
        if _pend:
            out["pending_ops"] = _pend
    except Exception:
        pass
    return out


# ===========================================================================
# dispatch + TOOLS schema (shared by MCP adapter and the headless API loop)
# ===========================================================================

DISPATCH = {
    "roll_dice": roll_dice,
    "search_lore": search_lore,
    "apply_state": apply_state,
    "update_npc": update_npc,
    "commit_turn": commit_turn,
    "recap": recap,
    "seed_branch": seed_branch,
    "register_branch": register_branch,
}


def dispatch(name: str, payload: dict):
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    return fn(**payload)


# ---------------------------------------------------------------------------
# Low-level subcommand CLI:  python gm_tools.py <tool> --json '<payload>'
# ---------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description="GM play tools")
    p.add_argument("tool", choices=sorted(DISPATCH.keys()))
    p.add_argument("--json", default="{}", help="JSON payload of kwargs")
    args = p.parse_args(argv)
    payload = json.loads(args.json)
    result = dispatch(args.tool, payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
