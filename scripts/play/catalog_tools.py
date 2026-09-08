#!/usr/bin/env python3
"""Catalog tools — the deterministic vendor/catalog engine for v2.

A persistent, **genre-agnostic** shop catalog: a vendor has a `currency_field`
(a key in character_state, e.g. fate_points / reward_points / money) and a list
of priced items. 修仙商城 / 宗門兌換 / 都市掙錢 are all the same engine, different
config. Buying is a DETERMINISTIC transaction (check balance → deduct currency →
grant item) — it runs WITHOUT the LLM, called by the UI via /api/v2/catalog/*.

App-free (wraps only `story_io`), gm_tools-style. Writes the SAME
`character_state.json` the GM reads, so a UI purchase is visible to the GM next
turn (the GM caches state in-context for latency, so it should re-read after the
player shops).

Storage:
  data/stories/<story>/catalog.json   — story-level definition (shared across branches)
Currency + owned items live in the per-branch character_state (no extra overlay;
no unique/restock tracking in v1 — every item is repurchasable).

CLI:
  python3 catalog_tools.py view  --story <s> --branch <b> [--vendor <v>]
  python3 catalog_tools.py buy   --story <s> --branch <b> --vendor <v> --id <item_id>
  python3 catalog_tools.py validate < catalog.json
  python3 catalog_tools.py save     --story <s> < catalog.json
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from story_core import story_io as sio  # noqa: E402

CATALOG_FILE = "catalog.json"
# map-type state fields an item may be granted into (genre-agnostic defaults)
GRANT_MAP_FIELDS = {"inventory", "techniques", "talents", "systems"}

# Skill-tree mastery ladder (「成」). Ordinal index; gating compares >=. The GM
# advances mastery narratively (set_mastery); the tree gate enforces it deterministically.
MASTERY_LEVELS = ["未習", "入門", "小成", "大成", "圓滿"]
MASTERY_FIELD = "technique_mastery"  # character_state map: {item_id: ordinal 0..4}


def _mastery(state: dict, item_id: str) -> int:
    m = state.get(MASTERY_FIELD)
    v = m.get(item_id, 0) if isinstance(m, dict) else 0
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _mastery_label(ordinal: int) -> str:
    i = max(0, min(int(ordinal), len(MASTERY_LEVELS) - 1))
    return MASTERY_LEVELS[i]


# --- 功法樹 single source of truth ------------------------------------------
# A 功法樹 node = a catalog item carrying `tier`. Its ONLY ownership/level store is
# `technique_mastery` (id-keyed ordinal). The name-keyed `techniques` map is for
# NON-tree freeform skills only. Keeping a tree 功法 in BOTH stores is the drift
# (a name in techniques with no mastery id → the tree doesn't recognise it; a
# free-text purchase that lands in techniques bypasses the tree). Every acquisition
# path funnels tree nodes into mastery and keeps them OUT of techniques.

def _is_tree_node(item: dict) -> bool:
    return isinstance(item, dict) and "tier" in item


def _tree_node_by_id(catalog: dict, item_id: str) -> dict | None:
    iid = str(item_id or "").strip()
    if not iid:
        return None
    for it in catalog.get("items", []):
        if _is_tree_node(it) and it.get("id") == iid:
            return it
    return None


def _tree_node_by_name(catalog: dict, name: str) -> dict | None:
    n = str(name or "").strip()
    if not n:
        return None
    for it in catalog.get("items", []):
        if _is_tree_node(it) and str(it.get("name", "")).strip() == n:
            return it
    return None


def _unlock_mastery(state: dict, item_id: str, min_ordinal: int = 1) -> None:
    """Mark a tree node owned at >= min_ordinal (idempotent; never demotes)."""
    mm = state.get(MASTERY_FIELD)
    if not isinstance(mm, dict):
        mm = {}
    mm[item_id] = max(_mastery(state, item_id), int(min_ordinal))
    state[MASTERY_FIELD] = mm


def reconcile_techniques(state: dict, catalog: dict) -> bool:
    """Collapse the dual store to one truth: fold any `techniques` entry whose name
    matches a 功法樹 node into `technique_mastery` (id-keyed, >=入門) and drop it from
    `techniques`. Idempotent — a no-op once clean. Returns True if it changed state.
    Used at acquire-time (self-heal) and as a one-shot migration over existing branches."""
    tech = state.get("techniques")
    if not isinstance(tech, dict) or not tech:
        return False
    changed = False
    for name in list(tech.keys()):
        node = _tree_node_by_name(catalog, name)
        if node:
            _unlock_mastery(state, node.get("id"), 1)
            tech.pop(name, None)
            changed = True
    if changed:
        state["techniques"] = tech
    return changed


def _check_requires(catalog: dict, state: dict, item: dict) -> tuple[bool, list[str]]:
    """Deterministic prerequisite gate for a tree node. Returns (ok, unmet_reasons).

    Gates on prior-技能 mastery (`prereq: {item_id: min_ordinal}`) and 境界
    (`min_realm`, compared against the story's realm_ladder). Money is checked
    separately by buy(); this is the *tree* lock, not the price lock.
    """
    req = item.get("requires") or {}
    if not isinstance(req, dict):
        return True, []
    reasons: list[str] = []
    id2name = {
        it.get("id"): it.get("name", it.get("id"))
        for it in catalog.get("items", []) if isinstance(it, dict) and it.get("id")
    }
    for pid, need in (req.get("prereq") or {}).items():
        have = _mastery(state, pid)
        if have < int(need):
            reasons.append(f"需「{id2name.get(pid, pid)}」{_mastery_label(need)}")
    min_realm = req.get("min_realm")
    if min_realm:
        ladder = catalog.get("realm_ladder") or []
        cur = state.get("major_realm")
        ci = ladder.index(cur) if cur in ladder else -1
        ri = ladder.index(min_realm) if min_realm in ladder else 0
        if ci < ri:
            reasons.append(f"需 {min_realm} 境（你目前 {cur or '凡人'}）")
    # optional relationship gate: requires.min_relationship = {"npc": 名, "tier": 階梯}
    mr = req.get("min_relationship")
    if isinstance(mr, dict) and mr.get("npc") and mr.get("tier"):
        try:
            from story_core.gm_v2 import relationship as _rel
        except Exception:
            _rel = None
        npc, tier = mr["npc"], mr["tier"]
        rv = (state.get("relationships") or {}).get(npc)
        if _rel is not None and not _rel.meets(rv, tier):
            reasons.append(f"需與「{npc}」關係達 {tier}")
    return (not reasons, reasons)


def _catalog_path(story_id: str) -> str:
    return os.path.join(sio._story_dir(story_id), CATALOG_FILE)


def load_catalog(story_id: str) -> dict:
    cat = sio._load_json(_catalog_path(story_id), {})
    return cat if isinstance(cat, dict) else {}


def _load_state(story_id: str, branch_id: str) -> dict:
    return sio._load_json(sio._story_character_state_path(story_id, branch_id), {})


def _save_state(story_id: str, branch_id: str, state: dict):
    sio._save_json(sio._story_character_state_path(story_id, branch_id), state)


def _ledger_path(story_id: str, branch_id: str) -> str:
    return os.path.join(sio._branch_dir(story_id, branch_id), "catalog_ledger.json")


def _ops_path(story_id: str, branch_id: str) -> str:
    return os.path.join(sio._branch_dir(story_id, branch_id), "pending_ops.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_list(path: str) -> list:
    data = sio._load_json(path, [])
    return data if isinstance(data, list) else []


def _append_op(story_id: str, branch_id: str, op: dict):
    """Append a UI operation to the per-branch op-log so the GM can learn of it
    next turn (the bridge between out-of-band UI mutations and the LLM)."""
    ops = _load_list(_ops_path(story_id, branch_id))
    op = dict(op)
    op.setdefault("ts", _now())
    ops.append(op)
    sio._save_json(_ops_path(story_id, branch_id), ops)


def ops_pending(story_id: str, branch_id: str) -> dict:
    """Read the unacknowledged UI ops (GM-side; consumed by /play)."""
    return {"ok": True, "ops": _load_list(_ops_path(story_id, branch_id))}


def ops_ack(story_id: str, branch_id: str) -> dict:
    """Clear the op-log after the GM has acknowledged the ops in narration."""
    cleared = _load_list(_ops_path(story_id, branch_id))
    sio._save_json(_ops_path(story_id, branch_id), [])
    return {"ok": True, "cleared": len(cleared), "ops": cleared}


def _turn_ops_path(story_id: str, branch_id: str) -> str:
    return os.path.join(sio._branch_dir(story_id, branch_id), "turn_ops.json")


def save_turn_ops(story_id: str, branch_id: str, gm_index: int, ops: list) -> None:
    """Snapshot the UI ops a GM reply acknowledged, keyed by that reply's message index.
    ops_pending is cleared each turn AND not copied on fork, so a later regenerate of this
    GM reply would otherwise be blind to the player's purchases/actions. This durable,
    per-(branch, index) snapshot lets regenerate replay the exact ops for that turn."""
    if gm_index is None or not ops:
        return
    snap = sio._load_json(_turn_ops_path(story_id, branch_id), {})
    if not isinstance(snap, dict):
        snap = {}
    snap[str(gm_index)] = ops
    sio._save_json(_turn_ops_path(story_id, branch_id), snap)


def turn_ops(story_id: str, branch_id: str, gm_index: int) -> list:
    """Read back the ops a GM reply acknowledged (for regenerate replay). Empty if none."""
    snap = sio._load_json(_turn_ops_path(story_id, branch_id), {})
    if isinstance(snap, dict):
        v = snap.get(str(gm_index))
        if isinstance(v, list):
            return v
    return []


# ---------------------------------------------------------------------------
# Per-op UNDO (player UI ops between GM turns) — snapshot-based LIFO.
#
# Every deterministic player op (買/退/解鎖/⚡行動/閉關/副本) spends real resources
# (currency, world-time, deadlines, dungeon lifecycle). Some can't be cleanly reversed
# in-place (a fired 魔潮 deadline, a generated 副本 blueprint), so undo = RESTORE a full
# pre-op snapshot rather than per-op reverse logic. Snapshots are pushed BEFORE each op
# (by the player-UI routes) and the whole stack is cleared when a GM turn commits (the
# ops become permanent narrative — past ops use the timeline rewind/fork instead).
# LIFO: undoing op i restores the state just before it, which also rolls back ops i+1…n.
# ---------------------------------------------------------------------------

def _undo_path(story_id: str, branch_id: str) -> str:
    return os.path.join(sio._branch_dir(story_id, branch_id), "op_undo.json")


# files an op may mutate; snapshot/restore as a unit. None = file absent (restore deletes it).
def _snapshot_targets(story_id: str, branch_id: str) -> dict:
    bdir = sio._branch_dir(story_id, branch_id)
    return {
        "character_state": sio._story_character_state_path(story_id, branch_id),
        "npcs": sio._story_npcs_path(story_id, branch_id),
        "world_day": os.path.join(bdir, "world_day.json"),
        "dungeon_progress": os.path.join(bdir, "dungeon_progress.json"),
        "catalog_ledger": _ledger_path(story_id, branch_id),
        "pending_ops": _ops_path(story_id, branch_id),
    }


def snapshot_branch(story_id: str, branch_id: str) -> dict:
    """Capture the mutable per-branch files as a dict {key: parsed-json | None}."""
    snap: dict = {}
    for key, path in _snapshot_targets(story_id, branch_id).items():
        snap[key] = sio._load_json(path, None) if os.path.exists(path) else None
    return snap


def _restore_branch(story_id: str, branch_id: str, snap: dict) -> None:
    targets = _snapshot_targets(story_id, branch_id)
    for key, path in targets.items():
        val = (snap or {}).get(key, "__missing__")
        if val == "__missing__":
            continue  # key not in snapshot → leave file untouched
        if val is None:
            if os.path.exists(path):
                try:
                    os.remove(path)  # file didn't exist pre-op → undo removes it
                except OSError:
                    pass
        else:
            sio._save_json(path, val)


def push_undo(story_id: str, branch_id: str, pre_snapshot: dict, label: str = "") -> None:
    """Record one undo entry: the PRE-op snapshot + the op record (the last pending_ops
    entry this op just appended) for display. Call AFTER a successful player op."""
    stack = _load_list(_undo_path(story_id, branch_id))
    pending = _load_list(_ops_path(story_id, branch_id))
    op = pending[-1] if pending else None
    stack.append({"op": op, "label": label, "snapshot": pre_snapshot, "ts": _now()})
    sio._save_json(_undo_path(story_id, branch_id), stack)


def list_undo(story_id: str, branch_id: str) -> dict:
    """The cancellable player ops since the last GM turn (op records for display, oldest→newest)."""
    stack = _load_list(_undo_path(story_id, branch_id))
    ops = [{"index": i, "op": e.get("op"), "label": e.get("label", "")}
           for i, e in enumerate(stack) if isinstance(e, dict)]
    return {"ok": True, "ops": ops, "count": len(ops)}


def undo_to(story_id: str, branch_id: str, index: int) -> dict:
    """Undo op `index` (and every op after it): restore that op's pre-op snapshot, then
    truncate the stack to [0, index). Returns how many ops were rolled back."""
    stack = _load_list(_undo_path(story_id, branch_id))
    if not isinstance(index, int) or index < 0 or index >= len(stack):
        return {"ok": False, "reason": "bad_index", "count": len(stack)}
    entry = stack[index]
    if not isinstance(entry, dict) or "snapshot" not in entry:
        return {"ok": False, "reason": "no_snapshot"}
    _restore_branch(story_id, branch_id, entry["snapshot"])
    # the snapshot already restored pending_ops to its pre-op state; truncate the undo stack too
    sio._save_json(_undo_path(story_id, branch_id), stack[:index])
    return {"ok": True, "undone": len(stack) - index, "remaining": index}


def clear_undo(story_id: str, branch_id: str) -> None:
    """Drop the undo stack — called when a GM turn commits (ops become permanent)."""
    sio._save_json(_undo_path(story_id, branch_id), [])


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def validate(catalog: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(catalog, dict):
        return {"ok": False, "errors": ["catalog must be a JSON object"], "warnings": []}

    vendors = catalog.get("vendors")
    vendor_ids: set = set()
    if not isinstance(vendors, list) or not vendors:
        errors.append("vendors: required non-empty list")
    else:
        for i, v in enumerate(vendors):
            if not isinstance(v, dict):
                errors.append(f"vendors[{i}]: must be object")
                continue
            vid = v.get("id")
            if not vid:
                errors.append(f"vendors[{i}].id: required")
            elif vid in vendor_ids:
                errors.append(f"vendors: duplicate id {vid!r}")
            else:
                vendor_ids.add(vid)
            if not (isinstance(v.get("name"), str) and v["name"].strip()):
                errors.append(f"vendors[{i}].name: required")
            if not (isinstance(v.get("currency_field"), str) and v["currency_field"].strip()):
                errors.append(f"vendors[{i}].currency_field: required (a character_state key)")

    items = catalog.get("items")
    item_ids: set = set()
    if not isinstance(items, list):
        errors.append("items: required list")
    else:
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                errors.append(f"items[{i}]: must be object")
                continue
            iid = it.get("id")
            if not iid:
                errors.append(f"items[{i}].id: required")
            elif iid in item_ids:
                errors.append(f"items: duplicate id {iid!r}")
            else:
                item_ids.add(iid)
            if not (isinstance(it.get("name"), str) and it["name"].strip()):
                errors.append(f"items[{i}].name: required")
            if it.get("vendor") not in vendor_ids:
                errors.append(f"items[{i}].vendor {it.get('vendor')!r} not in vendors")
            price = it.get("price")
            if not isinstance(price, (int, float)) or isinstance(price, bool) or price < 0:
                errors.append(f"items[{i}].price: required non-negative number")
            gt = it.get("grant_to", "inventory")
            if gt not in GRANT_MAP_FIELDS:
                warnings.append(f"items[{i}].grant_to {gt!r} not a known map field {sorted(GRANT_MAP_FIELDS)}")
            if not (isinstance(it.get("desc"), str) and it["desc"].strip()):
                warnings.append(f"items[{i}].desc: empty")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def save_catalog(story_id: str, catalog: dict) -> dict:
    v = validate(catalog)
    if not v["ok"]:
        return {"ok": False, "saved": False, "validation": v}
    sio._save_json(_catalog_path(story_id), catalog)
    return {"ok": True, "saved": True, "path": _catalog_path(story_id), "validation": v}


def upsert(story_id: str, vendor: str, item: dict) -> dict:
    """GM-side: add or update ONE item in the catalog (lazy-grow during play).

    Use when the story makes a new thing purchasable (玩到一半出新貨 / 副本結算解鎖)
    or the player seeks an item the GM decides to stock. Persists to catalog.json,
    so it stays available next visit; the shop UI shows it on its next poll.
    Validated before write — a bad item is rejected, not saved.
    """
    if not isinstance(item, dict):
        return {"ok": False, "error": "item must be an object"}
    catalog = load_catalog(story_id)
    catalog.setdefault("vendors", [])
    catalog.setdefault("items", [])
    vendor_ids = {v.get("id") for v in catalog["vendors"] if isinstance(v, dict)}
    if vendor not in vendor_ids:
        return {"ok": False, "error": "vendor_not_found", "vendor": vendor, "known_vendors": sorted(vendor_ids)}

    item = dict(item)
    item["vendor"] = vendor
    if not (isinstance(item.get("id"), str) and item["id"].strip()):
        return {"ok": False, "error": "item.id required"}

    items = catalog["items"]
    replaced = False
    for i, ex in enumerate(items):
        if isinstance(ex, dict) and ex.get("id") == item["id"]:
            items[i] = item
            replaced = True
            break
    if not replaced:
        # dedup by (vendor, name): the GM often mints a NEW id for an item that already
        # exists under its canonical id (e.g. 御氣引息功) → without this it piles up
        # duplicates. Same vendor + same name = already stocked → skip, point at existing.
        name = str(item.get("name", "")).strip()
        if name:
            for ex in items:
                if (isinstance(ex, dict) and ex.get("vendor") == vendor
                        and str(ex.get("name", "")).strip() == name):
                    return {"ok": True, "action": "exists", "id": ex.get("id"),
                            "note": f"同名品項已上架（{ex.get('id')}），略過避免重複"}
        items.append(item)

    v = validate(catalog)
    if not v["ok"]:
        return {"ok": False, "saved": False, "validation": v}
    sio._save_json(_catalog_path(story_id), catalog)
    return {"ok": True, "saved": True, "action": "updated" if replaced else "added",
            "id": item["id"], "vendor": vendor, "validation": v}


# ---------------------------------------------------------------------------
# view / buy (deterministic; called by UI, never the GM)
# ---------------------------------------------------------------------------

def _owned(state: dict, item: dict) -> bool:
    if _is_tree_node(item):  # tree nodes: ownership lives in technique_mastery, not a bucket
        return _mastery(state, item.get("id")) >= 1
    bucket = state.get(item.get("grant_to", "inventory"))
    return isinstance(bucket, dict) and item.get("name") in bucket


def view(story_id: str, branch_id: str, vendor: str | None = None) -> dict:
    catalog = load_catalog(story_id)
    state = _load_state(story_id, branch_id)
    refundable_ids = {(e.get("vendor"), e.get("item_id")) for e in _load_list(_ledger_path(story_id, branch_id))}
    vendors = {v["id"]: v for v in catalog.get("vendors", []) if isinstance(v, dict) and v.get("id")}
    out_vendors = []
    for vid, v in vendors.items():
        if vendor and vid != vendor:
            continue
        cur_field = v.get("currency_field", "")
        balance = state.get(cur_field, 0)
        if not isinstance(balance, (int, float)) or isinstance(balance, bool):
            balance = 0
        items = []
        for it in catalog.get("items", []):
            if it.get("vendor") != vid:
                continue
            price = it.get("price", 0)
            items.append({
                "id": it.get("id"),
                "name": it.get("name"),
                "category": it.get("category", ""),
                "price": price,
                "level": it.get("level", ""),
                "domains": it.get("domains", []),
                "requirement": it.get("requirement", ""),
                "desc": it.get("desc", ""),
                "affordable": isinstance(price, (int, float)) and balance >= price,
                "shortfall": max(0, price - balance) if isinstance(price, (int, float)) else None,
                "owned": _owned(state, it),
                "refundable": (vid, it.get("id")) in refundable_ids,
            })
        out_vendors.append({
            "id": vid, "name": v.get("name", vid),
            "currency_field": cur_field,
            "currency_label": v.get("currency_label", cur_field),
            "balance": balance,
            "items": items,
        })
    return {"ok": True, "vendors": out_vendors}


def buy(story_id: str, branch_id: str, vendor: str, item_id: str) -> dict:
    catalog = load_catalog(story_id)
    v = next((x for x in catalog.get("vendors", []) if x.get("id") == vendor), None)
    if not v:
        return {"ok": False, "reason": "vendor_not_found", "vendor": vendor}
    item = next((x for x in catalog.get("items", []) if x.get("id") == item_id and x.get("vendor") == vendor), None)
    if not item:
        return {"ok": False, "reason": "item_not_found", "id": item_id}

    cur_field = v.get("currency_field", "")
    price = item.get("price", 0)
    if not isinstance(price, (int, float)) or isinstance(price, bool):
        return {"ok": False, "reason": "bad_price", "id": item_id}

    state = _load_state(story_id, branch_id)
    ok_req, unmet = _check_requires(catalog, state, item)
    if not ok_req:
        return {"ok": False, "reason": "locked", "id": item_id, "unmet": unmet}
    balance = state.get(cur_field, 0)
    if not isinstance(balance, (int, float)) or isinstance(balance, bool):
        balance = 0
    if balance < price:
        return {"ok": False, "reason": "insufficient", "balance": balance,
                "price": price, "shortfall": price - balance, "currency_label": v.get("currency_label", cur_field)}

    # deduct currency + grant item (same character_state the GM reads)
    state[cur_field] = balance - price
    target = item.get("grant_to", "inventory")
    if _is_tree_node(item):
        # 功法樹節點：唯一真理＝technique_mastery（id-keyed），入門(1) 起算。**不**再雙寫進
        # techniques（那是漂移來源）；UI/grounding 從 mastery+catalog 取名。
        _unlock_mastery(state, item_id, 1)
    else:
        bucket = state.get(target)
        if not isinstance(bucket, dict):
            bucket = {}
        bucket[item.get("name")] = item.get("desc") or "已購入"
        state[target] = bucket
    _save_state(story_id, branch_id, state)

    # record purchase (enables refund) + op-log (so the GM learns of it next turn)
    ledger = _load_list(_ledger_path(story_id, branch_id))
    ledger.append({
        "vendor": vendor, "item_id": item_id, "name": item.get("name"),
        "price": price, "currency_field": cur_field,
        "currency_label": v.get("currency_label", cur_field),
        "grant_to": target, "ts": _now(),
    })
    sio._save_json(_ledger_path(story_id, branch_id), ledger)
    _append_op(story_id, branch_id, {
        "op": "buy", "item": item.get("name"), "amount": price,
        "currency": v.get("currency_label", cur_field), "granted_into": target,
    })

    return {
        "ok": True,
        "bought": item.get("name"),
        "granted_into": target,
        "spent": price,
        "currency_label": v.get("currency_label", cur_field),
        "new_balance": state[cur_field],
    }


def refund(story_id: str, branch_id: str, vendor: str, item_id: str) -> dict:
    """Reverse a purchase (反悔): full price back + remove the item. Only items in
    the purchase ledger are refundable (so looted/quest items can't be exploited).
    Refunds the most recent matching purchase."""
    ledger = _load_list(_ledger_path(story_id, branch_id))
    idx = None
    for i in range(len(ledger) - 1, -1, -1):
        e = ledger[i]
        if e.get("vendor") == vendor and e.get("item_id") == item_id:
            idx = i
            break
    if idx is None:
        return {"ok": False, "reason": "no_purchase", "id": item_id}

    entry = ledger[idx]
    cur_field = entry.get("currency_field", "")
    price = entry.get("price", 0)
    target = entry.get("grant_to", "inventory")
    name = entry.get("name")

    state = _load_state(story_id, branch_id)
    bal = state.get(cur_field, 0)
    if not isinstance(bal, (int, float)) or isinstance(bal, bool):
        bal = 0
    state[cur_field] = bal + price
    # symmetric with buy(): a 功法樹 node's ownership lives in technique_mastery (id-keyed),
    # not a name bucket — so reverse the unlock there, not in `target`.
    if _tree_node_by_id(load_catalog(story_id), item_id):
        mm = state.get(MASTERY_FIELD)
        if isinstance(mm, dict):
            mm.pop(item_id, None)
            state[MASTERY_FIELD] = mm
    else:
        bucket = state.get(target)
        if isinstance(bucket, dict):
            bucket.pop(name, None)
            state[target] = bucket
    _save_state(story_id, branch_id, state)

    ledger.pop(idx)
    sio._save_json(_ledger_path(story_id, branch_id), ledger)
    _append_op(story_id, branch_id, {
        "op": "refund", "item": name, "amount": price,
        "currency": entry.get("currency_label", cur_field),
    })

    return {
        "ok": True, "refunded": name, "amount": price,
        "currency_label": entry.get("currency_label", cur_field),
        "new_balance": state[cur_field],
    }


def discard_item(story_id: str, branch_id: str, name: str, field: str = "inventory") -> dict:
    """Manually remove ONE item from a map-field bucket (inventory/techniques/talents/systems).

    Unlike refund() this is NOT ledger-gated — it clears ANY item, including quest / loot
    items the GM forgot to remove (the「越積越多」cleanup the player asked for). It is a
    丟棄, not a 退款: no currency is credited. To stop a buy→discard→refund double-dip, any
    matching purchase-ledger entry (same name+field) is dropped too, so a stale 退貨 button
    can't re-credit a now-gone item. Op-logged (op=discard) so the GM acknowledges the
    removal next turn and stops referencing the item. Tree 功法 (technique_mastery) is NOT
    a name bucket — this won't touch it; pass field accordingly for freeform techniques."""
    name = str(name or "").strip()
    if not name:
        return {"ok": False, "reason": "missing_item"}
    if field not in GRANT_MAP_FIELDS:
        field = "inventory"
    state = _load_state(story_id, branch_id)
    bucket = state.get(field)
    if not isinstance(bucket, dict) or name not in bucket:
        return {"ok": False, "reason": "not_found", "id": name, "field": field}
    bucket.pop(name, None)
    state[field] = bucket
    _save_state(story_id, branch_id, state)

    # drop any refundable ledger entry for this item so it can't later be 退貨-exploited
    ledger = _load_list(_ledger_path(story_id, branch_id))
    kept = [e for e in ledger
            if not (e.get("name") == name and e.get("grant_to", "inventory") == field)]
    if len(kept) != len(ledger):
        sio._save_json(_ledger_path(story_id, branch_id), kept)

    _append_op(story_id, branch_id, {"op": "discard", "item": name, "field": field})
    return {"ok": True, "discarded": name, "field": field}


def spend_and_grant(story_id: str, branch_id: str, amount, item_name: str,
                    grant_to: str = "inventory", item_desc: str = "",
                    currency_field: str = "fate_points") -> dict:
    """Ad-hoc deterministic purchase: deduct EXACTLY `amount` from the currency and grant
    `item_name`. For free-text purchases NOT in the catalog — the ENGINE does the math so
    the LLM never has to (no double-count, no absolute-overwrite). Insufficient → refused."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad_amount", "amount": amount}
    if amount < 0:
        return {"ok": False, "reason": "negative_amount"}
    name = str(item_name or "").strip()
    if not name:
        return {"ok": False, "reason": "missing_item"}
    state = _load_state(story_id, branch_id)
    bal = state.get(currency_field, 0)
    if not isinstance(bal, (int, float)) or isinstance(bal, bool):
        bal = 0
    if bal < amount:
        return {"ok": False, "reason": "insufficient", "balance": bal, "shortfall": amount - bal}
    state[currency_field] = bal - amount
    # if the free-text item is actually a 功法樹 node (GM narrating a 功法 purchase), funnel it
    # into technique_mastery (the tree's single truth) instead of dumping a phantom name into
    # techniques — otherwise the tree never recognises it (the free-text-bypass drift).
    node = _tree_node_by_name(load_catalog(story_id), name)
    if node:
        _unlock_mastery(state, node.get("id"), 1)
        _save_state(story_id, branch_id, state)
        _append_op(story_id, branch_id, {"op": "purchase", "item": name, "amount": amount,
                                         "granted_into": MASTERY_FIELD})
        return {"ok": True, "item": name, "spent": amount, "new_balance": state[currency_field],
                "granted_into": MASTERY_FIELD, "node_id": node.get("id")}
    if grant_to not in GRANT_MAP_FIELDS:
        grant_to = "inventory"
    bucket = state.get(grant_to)
    if not isinstance(bucket, dict):
        bucket = {}
    bucket[name] = item_desc or "已購入"
    state[grant_to] = bucket
    _save_state(story_id, branch_id, state)
    _append_op(story_id, branch_id, {"op": "purchase", "item": name, "amount": amount, "granted_into": grant_to})
    return {"ok": True, "item": name, "spent": amount, "new_balance": state[currency_field], "granted_into": grant_to}


# ---------------------------------------------------------------------------
# skill tree (功法樹) — read-only view; unlock goes through buy() (requires-gated)
# ---------------------------------------------------------------------------

def tree_view(story_id: str, branch_id: str, vendor: str | None = None) -> dict:
    """Skill-tree view for a 宗門 vendor: nodes (catalog items with `tier`) +
    their lock state computed deterministically from mastery + 境界 + balance.
    Pure read; the UI renders the graph and calls buy() to unlock."""
    catalog = load_catalog(story_id)
    state = _load_state(story_id, branch_id)
    items = [it for it in catalog.get("items", []) if isinstance(it, dict) and "tier" in it]
    # every 宗門 that has a tree (preserve catalog vendor order) → UI switcher
    tree_vendor_ids: list[str] = []
    for it in items:
        vid = it.get("vendor")
        if vid and vid not in tree_vendor_ids:
            tree_vendor_ids.append(vid)
    vdefs = {v.get("id"): v for v in catalog.get("vendors", []) if isinstance(v, dict) and v.get("id")}
    # 無限流: NO acquisition gate — 主神空間/兌換 lets you learn ANY world's arts with
    # 命運點數, visited or not (and once learnt they're retained across all worlds).
    # `world` is kept only to default the open panel to your current 副本's tree.
    tree_vendors = [{"id": vid, "name": (vdefs.get(vid) or {}).get("name", vid)} for vid in tree_vendor_ids]
    if not vendor or vendor not in tree_vendor_ids:
        cur_dungeon = (state.get("current_dungeon") or "").strip()

        def _in_world(vid: str) -> bool:
            vd = vdefs.get(vid) or {}
            w = str(vd.get("world", "")).strip()
            if not w or not cur_dungeon:
                return False
            cands = [w] + [str(a) for a in (vd.get("aliases") or [])]
            return any(c and (c in cur_dungeon or cur_dungeon in c) for c in cands)

        in_world = [vid for vid in tree_vendor_ids if _in_world(vid)]
        vendor = in_world[0] if in_world else (tree_vendor_ids[0] if tree_vendor_ids else None)
    vdef = vdefs.get(vendor, {})
    cur_field = vdef.get("currency_field", "")
    balance = state.get(cur_field, 0)
    if not isinstance(balance, (int, float)) or isinstance(balance, bool):
        balance = 0

    nodes = []
    for it in items:
        if it.get("vendor") != vendor:
            continue
        iid = it.get("id")
        mastery = _mastery(state, iid)
        owned = mastery >= 1
        ok_req, unmet = _check_requires(catalog, state, it)
        price = it.get("price", 0)
        affordable = isinstance(price, (int, float)) and balance >= price
        if owned:
            status = "unlocked"
        elif not ok_req:
            status = "locked"
        elif not affordable:
            status = "broke"
        else:
            status = "available"
        nodes.append({
            "id": iid, "name": it.get("name"), "tier": it.get("tier", 0),
            "level": it.get("level", ""), "domains": it.get("domains", []),
            "category": it.get("category", ""), "price": price, "desc": it.get("desc", ""),
            "mastery": mastery, "mastery_label": _mastery_label(mastery),
            "owned": owned, "status": status, "unmet": unmet,
            "affordable": affordable,
            "shortfall": max(0, price - balance) if isinstance(price, (int, float)) else None,
            "prereq": list((it.get("requires") or {}).get("prereq", {}).keys()),
            "min_realm": (it.get("requires") or {}).get("min_realm", ""),
        })
    nodes.sort(key=lambda n: (n["tier"], n["id"]))
    return {
        "ok": True, "vendor": vendor, "vendor_name": vdef.get("name", vendor),
        "vendors": tree_vendors,
        "currency_field": cur_field, "currency_label": vdef.get("currency_label", cur_field),
        "balance": balance, "mastery_levels": MASTERY_LEVELS, "nodes": nodes,
    }


def set_mastery(story_id: str, branch_id: str, item_id: str, mastery) -> dict:
    """Advance (or set) a 功法's 成 — the GM calls this as cultivation progresses
    in the story; the tree gate then lets downstream nodes unlock. Accepts an
    ordinal int or a 成-name (入門/小成/大成/圓滿). Moves the dial on an OWNED 功法;
    granting the 功法 itself is buy/unlock."""
    if isinstance(mastery, str) and not mastery.lstrip("-").isdigit():
        mastery = MASTERY_LEVELS.index(mastery) if mastery in MASTERY_LEVELS else 0
    try:
        ordv = max(0, min(int(mastery), len(MASTERY_LEVELS) - 1))
    except (TypeError, ValueError):
        return {"ok": False, "error": "mastery must be int 0..4 or a 成-name"}
    state = _load_state(story_id, branch_id)
    mm = state.get(MASTERY_FIELD)
    if not isinstance(mm, dict):
        mm = {}
    mm[item_id] = ordv
    state[MASTERY_FIELD] = mm
    _save_state(story_id, branch_id, state)
    _append_op(story_id, branch_id, {"op": "cultivate", "item_id": item_id, "mastery": _mastery_label(ordv)})
    return {"ok": True, "item_id": item_id, "mastery": ordv, "mastery_label": _mastery_label(ordv)}


def _read_stdin_json(arg_json: str | None) -> dict:
    raw = arg_json if arg_json is not None else sys.stdin.read()
    return json.loads(raw or "{}")


def _main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(description="catalog (vendor/shop) tools")
    p.add_argument("cmd", choices=["view", "buy", "refund", "upsert", "validate", "save",
                                   "ops_pending", "ops_ack", "tree", "unlock", "cultivate"])
    p.add_argument("--story")
    p.add_argument("--branch")
    p.add_argument("--vendor")
    p.add_argument("--id")
    p.add_argument("--mastery", default=None, help="cultivate: ordinal 0..4 or 成-name (入門/小成/大成/圓滿)")
    p.add_argument("--json", default=None)
    a = p.parse_args(argv)

    def _need(*names):
        return all(getattr(a, n) for n in names)

    if a.cmd == "validate":
        out = validate(_read_stdin_json(a.json))
    elif a.cmd == "save":
        out = save_catalog(a.story, _read_stdin_json(a.json)) if a.story else {"ok": False, "error": "save needs --story"}
    elif a.cmd == "view":
        out = view(a.story, a.branch, a.vendor) if _need("story", "branch") else {"ok": False, "error": "view needs --story --branch"}
    elif a.cmd == "buy":
        out = buy(a.story, a.branch, a.vendor, a.id) if _need("story", "branch", "vendor", "id") else {"ok": False, "error": "buy needs --story --branch --vendor --id"}
    elif a.cmd == "refund":
        out = refund(a.story, a.branch, a.vendor, a.id) if _need("story", "branch", "vendor", "id") else {"ok": False, "error": "refund needs --story --branch --vendor --id"}
    elif a.cmd == "upsert":
        out = upsert(a.story, a.vendor, _read_stdin_json(a.json)) if _need("story", "vendor") else {"ok": False, "error": "upsert needs --story --vendor + item JSON (stdin/--json)"}
    elif a.cmd == "ops_pending":
        out = ops_pending(a.story, a.branch) if _need("story", "branch") else {"ok": False, "error": "ops_pending needs --story --branch"}
    elif a.cmd == "tree":
        out = tree_view(a.story, a.branch, a.vendor) if _need("story", "branch") else {"ok": False, "error": "tree needs --story --branch [--vendor]"}
    elif a.cmd == "unlock":
        out = buy(a.story, a.branch, a.vendor, a.id) if _need("story", "branch", "vendor", "id") else {"ok": False, "error": "unlock needs --story --branch --vendor --id"}
    elif a.cmd == "cultivate":
        out = set_mastery(a.story, a.branch, a.id, a.mastery) if (_need("story", "branch", "id") and a.mastery is not None) else {"ok": False, "error": "cultivate needs --story --branch --id --mastery"}
    else:  # ops_ack
        out = ops_ack(a.story, a.branch) if _need("story", "branch") else {"ok": False, "error": "ops_ack needs --story --branch"}

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
