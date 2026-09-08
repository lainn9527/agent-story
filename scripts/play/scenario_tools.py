#!/usr/bin/env python3
"""Scenario blueprint tools — validate & persist the 劇本架構師 (/scenario) output.

App-free (wraps only `story_io`), gm_tools-style. The /scenario skill produces a
blueprint dict; this validates it against the UNIFIED schema (one schema for both
副本 and 本世界事件 — they differ only in `kind` / `structure` / `context`) and
persists it to `<branch>/scenario.json` for the /play turn-GM to read.

Hard `errors` block a save; `warnings` are advisory (design smells) and still save.

CLI (big CJK blueprints: pipe via stdin / heredoc, like gm_post):
  python3 scenario_tools.py validate < blueprint.json
  python3 scenario_tools.py save --story <s> --branch <b> < blueprint.json
  python3 scenario_tools.py load --story <s> --branch <b>
"""
from __future__ import annotations

import json
import os
import sys

# --- path bootstrap: make `story_core` importable when run as a script ------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from story_core import story_io as sio  # noqa: E402

SCENARIO_FILE = "scenario.json"

KINDS = {"dungeon", "world_event"}
STRUCTURES = {"linear_clear", "bracket", "waves", "investigation", "siege", "open"}
BEAT_TYPES = {"探索", "遭遇", "解謎", "社交", "戰鬥"}
ROLES = {"trash", "elite", "boss", "social"}
TIERS = {"普通", "精良", "命運"}
CONTEXT_KEYS = {
    "dungeon": {"ip", "world_isolation", "entry_prereq", "on_clear", "death"},
    "world_event": {"recurrence", "ties", "persistent_world", "on_resolve"},
}


def _scenario_path(story_id: str, branch_id: str) -> str:
    return os.path.join(sio._branch_dir(story_id, branch_id), SCENARIO_FILE)


def _nonempty_str(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def validate(blueprint: dict) -> dict:
    """Validate a blueprint against the unified schema. Returns {ok, errors, warnings}."""
    errors: list[str] = []
    warnings: list[str] = []
    E = errors.append
    W = warnings.append

    if not isinstance(blueprint, dict):
        return {"ok": False, "errors": ["blueprint must be a JSON object"], "warnings": []}

    kind = blueprint.get("kind")
    if kind not in KINDS:
        E(f"kind: must be one of {sorted(KINDS)} (got {kind!r})")
    if blueprint.get("structure") not in STRUCTURES:
        E(f"structure: must be one of {sorted(STRUCTURES)} (got {blueprint.get('structure')!r})")
    if not _nonempty_str(blueprint.get("title")):
        E("title: required non-empty string")
    if not _nonempty_str(blueprint.get("premise")):
        E("premise: required non-empty string")

    diff = blueprint.get("difficulty")
    if not isinstance(diff, dict) or not _nonempty_str(diff.get("target_level")):
        E("difficulty.target_level: required (e.g. '3級（金丹）')")

    # --- objectives ---
    obj = blueprint.get("objectives")
    main_ids: set = set()
    side_ids: set = set()
    if not isinstance(obj, dict):
        E("objectives: required object with main/side")
        obj = {}
    else:
        main = obj.get("main")
        if not isinstance(main, list) or not main:
            E("objectives.main: required non-empty list")
        else:
            for i, m in enumerate(main):
                if not isinstance(m, dict):
                    E(f"objectives.main[{i}]: must be object")
                    continue
                mid = m.get("id")
                if not mid:
                    E(f"objectives.main[{i}].id: required")
                elif mid in main_ids:
                    E(f"objectives.main: duplicate id {mid!r}")
                else:
                    main_ids.add(mid)
                if not _nonempty_str(m.get("beat")):
                    E(f"objectives.main[{i}].beat: required")
        side = obj.get("side") or []
        if side and not isinstance(side, list):
            E("objectives.side: must be a list")
            side = []
        for i, s in enumerate(side):
            if not isinstance(s, dict):
                E(f"objectives.side[{i}]: must be object")
                continue
            sid = s.get("id")
            if not sid:
                E(f"objectives.side[{i}].id: required")
            elif sid in side_ids or sid in main_ids:
                E(f"objectives.side: duplicate id {sid!r}")
            else:
                side_ids.add(sid)
        if len(side) < 2:
            W(f"objectives.side: only {len(side)} — thin; aim for ≥2 distinct-type side objectives (one tied to a boss-counter/key reward)")

    # --- encounter_pool ---
    enc_ids: set = set()
    has_boss = False
    has_nonbrute = False
    pool = blueprint.get("encounter_pool")
    if not isinstance(pool, list) or not pool:
        E("encounter_pool: required non-empty list")
    else:
        for i, e in enumerate(pool):
            if not isinstance(e, dict):
                E(f"encounter_pool[{i}]: must be object")
                continue
            eid = e.get("id")
            if not eid:
                E(f"encounter_pool[{i}].id: required")
            elif eid in enc_ids:
                E(f"encounter_pool: duplicate id {eid!r}")
            else:
                enc_ids.add(eid)
            if not _nonempty_str(e.get("name")):
                E(f"encounter_pool[{i}].name: required")
            if not _nonempty_str(e.get("level")):
                E(f"encounter_pool[{i}].level: required (級)")
            role = e.get("role")
            if role not in ROLES:
                W(f"encounter_pool[{i}].role: unknown {role!r} (expected {sorted(ROLES)})")
            if role == "boss":
                has_boss = True
            doms = e.get("domains") or []
            if isinstance(doms, list) and any(_nonempty_str(d) and d.strip() != "—" for d in doms):
                has_nonbrute = True
        if not has_boss:
            W("encounter_pool: no encounter with role=boss")
        if not has_nonbrute:
            W("encounter_pool: all pure-戰力 (no domain threat) — add a non-transitive threat (精神/詭異/因果/剋制) for interest")

    # --- beats ---
    beat_ids: set = set()
    beats = blueprint.get("beats")
    if not isinstance(beats, list) or not beats:
        E("beats: required non-empty list")
        beats = []
    else:
        for i, b in enumerate(beats):
            if not isinstance(b, dict):
                E(f"beats[{i}]: must be object")
                continue
            bid = b.get("id")
            if not bid:
                E(f"beats[{i}].id: required")
            elif bid in beat_ids:
                E(f"beats: duplicate id {bid!r}")
            else:
                beat_ids.add(bid)
            if not _nonempty_str(b.get("title")):
                E(f"beats[{i}].title: required")
            if b.get("type") not in BEAT_TYPES:
                W(f"beats[{i}].type: unknown {b.get('type')!r} (expected {sorted(BEAT_TYPES)})")
            for ref in (b.get("encounters") or []):
                if ref not in enc_ids:
                    E(f"beats[{i}].encounters: {ref!r} not in encounter_pool")
            for ref in (b.get("hooks") or []):
                if ref not in side_ids:
                    E(f"beats[{i}].hooks: {ref!r} not in objectives.side")

    # --- rewards ---
    rew_ids: set = set()
    rewards = blueprint.get("rewards")
    if not isinstance(rewards, list) or not rewards:
        W("rewards: empty — nothing for the player to anticipate")
        rewards = []
    else:
        for i, r in enumerate(rewards):
            if not isinstance(r, dict):
                E(f"rewards[{i}]: must be object")
                continue
            rid = r.get("id")
            if not rid:
                E(f"rewards[{i}].id: required")
            elif rid in rew_ids:
                E(f"rewards: duplicate id {rid!r}")
            else:
                rew_ids.add(rid)
            if not _nonempty_str(r.get("item")):
                E(f"rewards[{i}].item: required")
            if r.get("tier") not in TIERS:
                W(f"rewards[{i}].tier: unknown {r.get('tier')!r} (expected {sorted(TIERS)})")
    for s in (obj.get("side") or []):
        rr = s.get("reward_ref") if isinstance(s, dict) else None
        if rr and rr not in rew_ids:
            E(f"objectives.side.reward_ref {rr!r} not in rewards")

    # --- climax ---
    climax = blueprint.get("climax")
    if not isinstance(climax, dict):
        E("climax: required object")
    else:
        if not _nonempty_str(climax.get("trigger_gate")):
            E("climax.trigger_gate: required")
        if not _nonempty_str(climax.get("stakes")):
            W("climax.stakes: empty — define what is won/lost")
        boss_ref = climax.get("boss_ref")
        if not boss_ref:
            W("climax.boss_ref: empty")
        elif boss_ref not in enc_ids:
            E(f"climax.boss_ref {boss_ref!r} not in encounter_pool")

    # --- pacing ---
    pacing = blueprint.get("pacing")
    if not isinstance(pacing, dict):
        E("pacing: required object")
    else:
        mb = pacing.get("min_beats_before_climax")
        if not isinstance(mb, int):
            E("pacing.min_beats_before_climax: required integer")
        elif beats and mb > len(beats):
            E(f"pacing.min_beats_before_climax ({mb}) > number of beats ({len(beats)})")
        elif mb < 1:
            W("pacing.min_beats_before_climax < 1 — climax can trigger immediately (anti-rush off)")
        if not _nonempty_str(pacing.get("anti_rush")):
            W("pacing.anti_rush: empty — describe the gate that prevents rushing to the climax")

    # --- context overlay (type-specific) ---
    ctx = blueprint.get("context")
    if not isinstance(ctx, dict):
        W("context: missing type-specific block")
    elif kind in CONTEXT_KEYS:
        missing = CONTEXT_KEYS[kind] - set(ctx.keys())
        if missing:
            W(f"context ({kind}): missing recommended keys {sorted(missing)}")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _summary(bp: dict) -> dict:
    roles: dict = {}
    for e in (bp.get("encounter_pool") or []):
        roles[e.get("role", "?")] = roles.get(e.get("role", "?"), 0) + 1
    tiers: dict = {}
    for r in (bp.get("rewards") or []):
        tiers[r.get("tier", "?")] = tiers.get(r.get("tier", "?"), 0) + 1
    obj = bp.get("objectives") or {}
    return {
        "kind": bp.get("kind"),
        "structure": bp.get("structure"),
        "title": bp.get("title"),
        "target_level": (bp.get("difficulty") or {}).get("target_level"),
        "beats": len(bp.get("beats") or []),
        "main_objectives": len(obj.get("main") or []),
        "side_objectives": len(obj.get("side") or []),
        "encounters_by_role": roles,
        "rewards_by_tier": tiers,
        "has_climax": isinstance(bp.get("climax"), dict),
    }


def save(story_id: str, branch_id: str, blueprint: dict) -> dict:
    """Validate then persist. Refuses to write if there are hard errors."""
    v = validate(blueprint)
    if not v["ok"]:
        return {"ok": False, "saved": False, "validation": v}
    path = _scenario_path(story_id, branch_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sio._save_json(path, blueprint)
    return {"ok": True, "saved": True, "path": path, "validation": v, "summary": _summary(blueprint)}


def load(story_id: str, branch_id: str) -> dict:
    bp = sio._load_json(_scenario_path(story_id, branch_id), {})
    return {"ok": bool(bp), "blueprint": bp, "summary": _summary(bp) if bp else None}


def _read_payload(arg_json: str | None) -> dict:
    raw = arg_json if arg_json is not None else sys.stdin.read()
    return json.loads(raw or "{}")


def _main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(description="scenario blueprint validate/persist")
    p.add_argument("cmd", choices=["validate", "save", "load"])
    p.add_argument("--story")
    p.add_argument("--branch")
    p.add_argument("--json", default=None, help="blueprint payload (else read stdin)")
    a = p.parse_args(argv)

    if a.cmd == "validate":
        out = validate(_read_payload(a.json))
    elif a.cmd == "save":
        if not a.story or not a.branch:
            out = {"ok": False, "error": "save needs --story and --branch"}
        else:
            out = save(a.story, a.branch, _read_payload(a.json))
    else:  # load
        if not a.story or not a.branch:
            out = {"ok": False, "error": "load needs --story and --branch"}
        else:
            out = load(a.story, a.branch)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
