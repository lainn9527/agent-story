#!/usr/bin/env python3
"""Time economy — the genre-agnostic action/time-budget engine (catalog_tools twin).

Time is the **second resource ledger**: unlike 命運點數 (earnable), it is
non-earnable and monotonic — every montage-scale action spends it, and a deadline
fires when the clock reaches it. That deadline-with-teeth is what turns "spending
time" into real opportunity cost (代價感).

App-free (wraps only `story_io`). Reads a per-world config and writes the SAME
per-branch `world_day.json` the legacy `world_timer` uses, so the clock is unified
(this engine becomes the single high-level entry; raw days still land in one file).

Storage:
  data/stories/<story>/time_economy.json          — per-world config (actions/deadlines/modifiers)
  data/stories/<story>/branches/<b>/world_day.json — per-branch clock + per-branch deadline progress

CLI:
  python3 time_tools.py status   --story <s> --branch <b>
  python3 time_tools.py advance  --story <s> --branch <b> --action cultivate
  python3 time_tools.py advance  --story <s> --branch <b> --days 3
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

CONFIG_FILE = "time_economy.json"
CLOCK_FILE = "world_day.json"  # shared with story_core.world_timer (one clock, no fragmentation)


def _config(story_id: str) -> dict:
    cfg = sio._load_json(os.path.join(sio._story_dir(story_id), CONFIG_FILE), {})
    return cfg if isinstance(cfg, dict) else {}


def _clock_path(story_id: str, branch_id: str) -> str:
    return os.path.join(sio._branch_dir(story_id, branch_id), CLOCK_FILE)


def _load_clock(story_id: str, branch_id: str) -> dict:
    data = sio._load_json(_clock_path(story_id, branch_id), {})
    return data if isinstance(data, dict) else {}


def _save_clock(story_id: str, branch_id: str, data: dict):
    data["last_updated"] = _now_iso()
    sio._save_json(_clock_path(story_id, branch_id), data)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state(story_id: str, branch_id: str) -> dict:
    return sio._load_json(sio._story_character_state_path(story_id, branch_id), {})


def get_now(story_id: str, branch_id: str) -> float:
    d = _load_clock(story_id, branch_id).get("world_day", 0)
    return d if isinstance(d, (int, float)) and not isinstance(d, bool) else 0


def _seed_deadlines(cfg: dict, clock: dict) -> dict:
    """Per-branch deadline progress (next_at advances as recurring deadlines fire).
    Seeded from config on first touch."""
    prog = clock.get("deadlines")
    if not isinstance(prog, dict):
        prog = {}
    for dl in cfg.get("deadlines", []) or []:
        did = dl.get("id")
        if did and did not in prog:
            prog[did] = {"next_at": dl.get("next_at", 0)}
    clock["deadlines"] = prog
    return prog


def _effective_cost(cfg: dict, state: dict, action_id: str) -> tuple[float, float, dict]:
    """(cost, applied_mult, action_def). Modifiers match a token in a state map/list
    (e.g. a 日息 talent) and multiply the base cost (天賦＝修煉快十倍）."""
    act = (cfg.get("actions") or {}).get(action_id) or {}
    base = act.get("base_cost", 0)
    if not isinstance(base, (int, float)) or isinstance(base, bool):
        base = 0
    mult = 1.0
    for mod in cfg.get("modifiers", []) or []:
        if action_id not in (mod.get("applies_to") or []):
            continue
        src = (state or {}).get(mod.get("source"), {})
        match = str(mod.get("match", ""))
        keys = src.keys() if isinstance(src, dict) else (src if isinstance(src, list) else [])
        if match and any(match in str(k) for k in keys):
            m = mod.get("mult", 1.0)
            if isinstance(m, (int, float)) and not isinstance(m, bool):
                mult *= m
    return round(base * mult, 2), mult, act


def status(story_id: str, branch_id: str) -> dict:
    cfg = _config(story_id)
    clock = _load_clock(story_id, branch_id)
    now = clock.get("world_day", 0) or 0
    prog = _seed_deadlines(cfg, clock)
    label = {dl.get("id"): dl for dl in cfg.get("deadlines", []) or []}
    upcoming = []
    for did, p in prog.items():
        dl = label.get(did, {})
        nxt = p.get("next_at")
        if nxt is None:
            continue
        remaining = round(nxt - now, 2)
        warn_at = dl.get("warn_at") or []
        upcoming.append({
            "id": did, "label": dl.get("label", did), "at": nxt,
            "in_n": remaining,
            "warn": bool(warn_at) and remaining > 0 and remaining <= max(warn_at),
        })
    upcoming.sort(key=lambda u: u["in_n"])
    return {"ok": True, "now": now, "unit": cfg.get("unit", "天"), "upcoming": upcoming}


def advance(story_id: str, branch_id: str, action_id: str | None = None,
            days: float | None = None, ctx: dict | None = None) -> dict:
    """Spend time. Either an `action_id` (cost from config × modifiers) or raw `days`.
    Advances the clock, fires any crossed deadlines, returns warnings."""
    cfg = _config(story_id)
    state = ctx if isinstance(ctx, dict) else _load_state(story_id, branch_id)
    action_label = None
    mult = 1.0
    if days is None:
        if not action_id or action_id not in (cfg.get("actions") or {}):
            return {"ok": False, "error": "unknown_action", "action": action_id,
                    "known": sorted((cfg.get("actions") or {}).keys())}
        cost, mult, act = _effective_cost(cfg, state, action_id)
        action_label = act.get("label", action_id)
    else:
        try:
            cost = round(float(days), 2)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_days", "days": days}
    if cost < 0:
        return {"ok": False, "error": "negative_cost", "cost": cost}

    clock = _load_clock(story_id, branch_id)
    prog = _seed_deadlines(cfg, clock)
    before = clock.get("world_day", 0) or 0
    now = round(before + cost, 2)
    clock["world_day"] = now

    label = {dl.get("id"): dl for dl in cfg.get("deadlines", []) or []}
    fired = []
    for did, p in prog.items():
        dl = label.get(did, {})
        nxt = p.get("next_at")
        recurring = dl.get("recurring")
        while nxt is not None and now >= nxt:
            fired.append({"id": did, "label": dl.get("label", did), "at": nxt,
                          "on_fire": dl.get("on_fire", "")})
            if isinstance(recurring, (int, float)) and not isinstance(recurring, bool) and recurring > 0:
                nxt = round(nxt + recurring, 2)
            else:
                nxt = None
        p["next_at"] = nxt
    if fired:  # persist for one-shot surfacing next turn (deadline teeth)
        pend = clock.get("pending_events")
        clock["pending_events"] = (pend if isinstance(pend, list) else []) + fired
    _save_clock(story_id, branch_id, clock)

    warnings = []
    for did, p in prog.items():
        dl = label.get(did, {})
        nxt = p.get("next_at")
        if nxt is None:
            continue
        remaining = round(nxt - now, 2)
        if (dl.get("warn_at") or []) and 0 < remaining <= max(dl["warn_at"]):
            warnings.append(f"距{dl.get('label', did)}剩 {remaining} {cfg.get('unit', '天')}")

    return {"ok": True, "now": now, "spent": cost, "from": before,
            "action": action_id, "action_label": action_label, "mult": mult,
            "fired": fired, "warnings": warnings}


def spend(story_id: str, branch_id: str, days: float) -> dict:
    """Raw time advance (escape hatch — prefer advance(action_id) so cost is data)."""
    return advance(story_id, branch_id, days=days)


def peek_events(story_id: str, branch_id: str) -> list:
    """Read pending fired-deadline events WITHOUT clearing (for building the prompt)."""
    evs = _load_clock(story_id, branch_id).get("pending_events") or []
    return evs if isinstance(evs, list) else []


def clear_events(story_id: str, branch_id: str, count: int | None = None) -> None:
    """Clear pending events AFTER the turn commits. `count` clears only the first N
    (the ones surfaced this turn) so events that FIRED during this turn's apply
    (a 閉關 that crossed a deadline) survive to be surfaced next turn."""
    clock = _load_clock(story_id, branch_id)
    evs = clock.get("pending_events")
    if not isinstance(evs, list) or not evs:
        return
    clock["pending_events"] = [] if count is None else evs[count:]
    _save_clock(story_id, branch_id, clock)


def pull_events(story_id: str, branch_id: str) -> list:
    """Read + CLEAR pending fired-deadline events (one-shot; for CLI/harness use)."""
    evs = peek_events(story_id, branch_id)
    if evs:
        clear_events(story_id, branch_id)
    return evs


# ---------------------------------------------------------------------------
# cultivation ⇄ time bridge (Phase 2): 閉關 spends calendar days; the ENGINE
# promotes 成 when accumulated days cross the threshold — the level is computed,
# NOT set by the GM (removes the 「GM 心證」 the time economy exists to kill).
# ---------------------------------------------------------------------------

def _catalog_tools():
    play_dir = os.path.dirname(os.path.abspath(__file__))
    if play_dir not in sys.path:
        sys.path.insert(0, play_dir)
    import catalog_tools

    return catalog_tools


def _save_state(story_id: str, branch_id: str, state: dict):
    sio._save_json(sio._story_character_state_path(story_id, branch_id), state)


def _cultivate_threshold(story_id: str, cfg: dict, state: dict, node_id: str) -> float:
    """Calendar-days per 成-step for THIS character: node `cultivate_cost` (or config
    `actions.cultivate.base_cost`) × the talent multiplier. Talent ⇒ fewer days ⇒ 天才."""
    base = (cfg.get("actions", {}).get("cultivate", {}) or {}).get("base_cost", 30)
    try:
        cat = _catalog_tools().load_catalog(story_id)
        node = next((it for it in cat.get("items", []) if isinstance(it, dict) and it.get("id") == node_id), None)
        if node and isinstance(node.get("cultivate_cost"), (int, float)) and not isinstance(node.get("cultivate_cost"), bool):
            base = node["cultivate_cost"]
    except Exception:
        pass
    _, mult, _ = _effective_cost(cfg, state, "cultivate")
    return max(round(base * mult, 2), 0.01)


# --- public API for sibling modules (don't reach into the _underscore internals) ---
def config(story_id: str) -> dict:
    return _config(story_id)


def effective_cost(cfg: dict, state: dict, action_id: str) -> tuple[float, float, dict]:
    return _effective_cost(cfg, state, action_id)


def cultivate_threshold(story_id: str, cfg: dict, state: dict, node_id: str) -> float:
    return _cultivate_threshold(story_id, cfg, state, node_id)


def cultivate_session(story_id: str, branch_id: str, node_id: str,
                      days: float | None = None, ctx: dict | None = None) -> dict:
    """閉關 on an OWNED 功法 node. Spends calendar days (advancing the clock + firing
    deadlines), accumulates toward the per-step threshold, and auto-promotes 成.

    days=None ⇒ 閉關直到下一次突破 (spend exactly what the next step needs).
    days=N    ⇒ 閉關 N 天 (partial progress is saved; may or may not break through).
    """
    cfg = _config(story_id)
    ct = _catalog_tools()
    state = _load_state(story_id, branch_id)  # fresh; single save at the end
    mm = state.get("technique_mastery") if isinstance(state.get("technique_mastery"), dict) else {}
    mastery = mm.get(node_id, 0) if isinstance(mm.get(node_id), int) else 0
    if mastery < 1:
        return {"ok": False, "error": "not_owned", "node": node_id,
                "note": "先解鎖（buy/unlock）才能修煉"}
    label_for = lambda o: ct.MASTERY_LEVELS[max(0, min(int(o), len(ct.MASTERY_LEVELS) - 1))]
    if mastery >= len(ct.MASTERY_LEVELS) - 1:
        return {"ok": True, "node": node_id, "mastery": mastery, "mastery_label": label_for(mastery),
                "spent": 0, "promoted": 0, "already_max": True, "now": get_now(story_id, branch_id)}

    T = _cultivate_threshold(story_id, cfg, state, node_id)
    techdays = state.get("technique_days") if isinstance(state.get("technique_days"), dict) else {}
    acc = techdays.get(node_id, 0) or 0

    if days is None:
        spend_days = max(round(T - acc, 2), 0.0) or T  # until next breakthrough
    else:
        try:
            spend_days = round(float(days), 2)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_days", "days": days}
    if spend_days < 0:
        return {"ok": False, "error": "negative_days"}

    adv = advance(story_id, branch_id, days=spend_days, ctx=state)  # clock only; fires deadlines

    acc += spend_days
    promoted = 0
    while acc >= T and mastery < len(ct.MASTERY_LEVELS) - 1:
        mastery += 1
        acc = round(acc - T, 2)
        promoted += 1
    techdays[node_id] = round(acc, 2)
    mm[node_id] = mastery
    state["technique_days"] = techdays
    state["technique_mastery"] = mm
    _save_state(story_id, branch_id, state)

    return {
        "ok": True, "node": node_id, "spent": spend_days, "now": adv.get("now"),
        "mastery": mastery, "mastery_label": label_for(mastery), "promoted": promoted,
        "threshold": T, "accumulated": techdays[node_id],
        "fired": adv.get("fired", []), "warnings": adv.get("warnings", []),
    }


def _main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(description="time economy (clock/action/deadline) tools")
    p.add_argument("cmd", choices=["status", "advance", "spend", "cultivate"])
    p.add_argument("--story")
    p.add_argument("--branch")
    p.add_argument("--action")
    p.add_argument("--node", help="cultivate: 功法樹節點 id")
    p.add_argument("--days", type=float, default=None)
    a = p.parse_args(argv)

    if not (a.story and a.branch):
        out = {"ok": False, "error": f"{a.cmd} needs --story --branch"}
    elif a.cmd == "status":
        out = status(a.story, a.branch)
    elif a.cmd == "advance":
        out = advance(a.story, a.branch, a.action, a.days)
    elif a.cmd == "cultivate":
        out = cultivate_session(a.story, a.branch, a.node, a.days) if a.node else {"ok": False, "error": "cultivate needs --node [--days]"}
    else:  # spend
        out = spend(a.story, a.branch, a.days) if a.days is not None else {"ok": False, "error": "spend needs --days"}

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
