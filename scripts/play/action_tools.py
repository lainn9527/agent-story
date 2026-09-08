#!/usr/bin/env python3
"""Action registry — the deterministic 「選單動作」 layer (catalog_tools / time_tools twin).

The traditional-RPG backbone: menu actions (打坐／趕路／宗門任務／採藥／閉關) that
consume resources (time, money) and apply effects **WITHOUT the LLM** — instant,
free, no drift. Driven by `time_economy.json`'s `actions` (same config time_tools
costs from), plus a dynamic 閉關 entry per learnt 功法.

App-free (wraps story_io; imports its siblings time_tools / catalog_tools). The web
UI lists actions and POSTs `do`; the engine executes and op-logs so the narrator
acknowledges it next turn. See doc/architecture_v2.md (確定性路徑).

CLI:
  python3 action_tools.py list --story <s> --branch <b>
  python3 action_tools.py do   --story <s> --branch <b> --action <id>   # id e.g. gather / cultivate:se_yuqi
"""
from __future__ import annotations

import json
import os
import sys

_PLAY_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLAY_DIR not in sys.path:
    sys.path.insert(0, _PLAY_DIR)
_REPO_ROOT = os.path.dirname(os.path.dirname(_PLAY_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from story_core import story_io as sio  # noqa: E402


def _time_tools():
    import time_tools
    return time_tools


def _catalog_tools():
    import catalog_tools
    return catalog_tools


def _state(story_id: str, branch_id: str) -> dict:
    return sio._load_json(sio._story_character_state_path(story_id, branch_id), {})


def _currency(cfg: dict) -> tuple[str, str]:
    return (cfg.get("currency_field") or "fate_points",
            cfg.get("currency_label") or "命運點數")


def _balance(state: dict, field: str) -> float:
    b = state.get(field, 0)
    return b if isinstance(b, (int, float)) and not isinstance(b, bool) else 0


# ---------------------------------------------------------------------------
# 結構動詞 registry — the deterministic, engine-owned actions (副本生命週期等).
# Each entry DECLARES: id/label/icon/category/desc, an `available(state)` gate
# (engine-computed → GM-independent), optional `params` (a UI picker), and the
# `executor` (a tool name routed to gm_v2.toolexec.execute_tool → the SAME
# deterministic path the GM's function-calling uses). This is the single recipe
# for "how to add an action button": append ONE entry here. See
# doc/architecture_v2.md (§ 動作骨幹 / 如何新增一顆動作按鈕).
# ---------------------------------------------------------------------------

def _in_dungeon(st: dict) -> bool:
    return bool(str(st.get("current_dungeon") or "").strip())


def _phase(st: dict) -> str:
    return str(st.get("current_phase") or "主神空間")


def _has_active_quest(st: dict) -> bool:
    qs = st.get("quests")
    return isinstance(qs, list) and any(isinstance(q, dict) and q.get("status") == "active" for q in qs)


def _dungeon_options(story_id: str, branch_id: str) -> list[dict]:
    """Enterable IP worlds = the curated, EXTENSIBLE pool (dungeon_worlds.json) ∪ catalog
    vendors that declare a `world` (the ability-tree worlds — auto-included). The pool is a
    suggestion list, NOT a hard limit: add any IP to the file, or the UI's『其他』lets the
    player free-type a world not listed. This is the reliable source of real-IP副本 options
    (the GM only frames the choice; it must not invent a no-name 秘境)."""
    seen: set[str] = set()
    opts: list[dict] = []
    try:  # 1) curated extensible pool (real IP worlds)
        pool = sio._load_json(os.path.join(sio._story_dir(story_id), "dungeon_worlds.json"), {})
        for w in (pool.get("worlds") or []):
            name = str((w or {}).get("name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                opts.append({"value": name, "label": name, "hint": str((w or {}).get("hint") or "")})
    except Exception:
        pass
    try:  # 2) catalog worlds that already have a 功法樹 (auto-included)
        cat = _catalog_tools().load_catalog(story_id)
        for v in cat.get("vendors", []) or []:
            world = str((v or {}).get("world") or "").strip()
            if world and world not in seen:
                seen.add(world)
                opts.append({"value": world, "label": world, "hint": v.get("name") or ""})
    except Exception:
        pass
    return opts


def _active_quest_options(story_id: str, branch_id: str) -> list[dict]:
    """Open quests to settle — the picker for 完成任務 (engine reads `quests`)."""
    opts: list[dict] = []
    for q in _state(story_id, branch_id).get("quests") or []:
        if isinstance(q, dict) and q.get("status") == "active":
            rf = q.get("reward_fate")
            hint = f"獎勵 {rf}" if rf else ""
            opts.append({"value": q.get("id"), "label": q.get("title") or q.get("id"), "hint": hint})
    return opts


STRUCTURAL_ACTIONS = [
    {
        "id": "enter_dungeon", "label": "進入試煉秘境", "icon": "🌀", "category": "副本",
        "desc": "選一個 IP 世界進入副本（引擎扣時間＋生成劇本）。",
        "available": lambda st: not _in_dungeon(st),  # any hub state (phase 是 GM 自填文字，別硬比對「主神空間」)
        "params": [{"name": "ip", "label": "選擇副本（真實 IP 世界）", "type": "enum",
                    "options_from": "dungeons", "allow_other": True}],
        "executor": "enter_dungeon",
    },
    {
        "id": "exit_dungeon", "label": "離開副本／回歸", "icon": "🚪", "category": "副本",
        "desc": "主動離開當前副本回歸（通關領獎、或中途撤離回歸、或身死）。引擎結算獎勵＋清副本＋收乾淨副本 NPC。",
        "available": lambda st: _in_dungeon(st),
        "params": [{"name": "outcome", "label": "結果", "type": "enum", "default": "cleared",
                    "options": [{"value": "cleared", "label": "通關（領獎）"},
                                {"value": "fled", "label": "撤離／回歸（未通關）"},
                                {"value": "died", "label": "身死"}]}],
        "executor": "exit_dungeon",
    },
    {
        "id": "complete_quest", "label": "完成任務", "icon": "✅", "category": "任務",
        "desc": "結算一個進行中任務，領取宣告好的獎勵（引擎精確發放）。",
        "available": lambda st: _has_active_quest(st),
        "params": [{"name": "quest_id", "label": "選擇任務", "type": "enum", "options_from": "active_quests"},
                   {"name": "outcome", "label": "結果", "type": "enum", "default": "done",
                    "options": [{"value": "done", "label": "完成（領獎）"},
                                {"value": "failed", "label": "放棄／失敗"}]}],
        "executor": "complete_quest",
    },
]
_STRUCTURAL_BY_ID = {a["id"]: a for a in STRUCTURAL_ACTIONS}

_PARAM_OPTION_SOURCES = {"dungeons": _dungeon_options, "active_quests": _active_quest_options}


def _resolve_params(story_id: str, branch_id: str, params: list | None) -> list[dict]:
    """Materialize a spec's params for the UI picker, resolving `options_from`."""
    out: list[dict] = []
    for p in params or []:
        q = dict(p)
        src = q.pop("options_from", None)
        if src and src in _PARAM_OPTION_SOURCES:
            q["options"] = _PARAM_OPTION_SOURCES[src](story_id, branch_id)
        out.append(q)
    return out


def list_actions(story_id: str, branch_id: str) -> dict:
    """Available menu actions (static from config + dynamic 閉關 per 功法) with their
    costs/effects + the current time/deadline status — everything the menu UI needs."""
    tt, ct = _time_tools(), _catalog_tools()
    cfg = tt.config(story_id)
    state = _state(story_id, branch_id)
    cur_field, cur_label = _currency(cfg)
    balance = _balance(state, cur_field)

    actions: list[dict] = []
    # --- static menu actions (config) ---
    for aid, a in (cfg.get("actions") or {}).items():
        if a.get("menu") is False:
            continue
        cost_days, _mult, _ = tt.effective_cost(cfg, state, aid)
        actions.append({
            "id": aid, "label": a.get("label", aid), "icon": a.get("icon", ""),
            "category": a.get("category", "行動"), "desc": a.get("desc", ""),
            "cost_days": cost_days, "gain": a.get("gain") or {},
            "kind": "action", "available": True, "unmet": [],
        })

    # --- dynamic 閉關 per learnt 功法 (→ next breakthrough) ---
    mm = state.get("technique_mastery") if isinstance(state.get("technique_mastery"), dict) else {}
    techdays = state.get("technique_days") if isinstance(state.get("technique_days"), dict) else {}
    levels = ct.MASTERY_LEVELS
    id2name = {it.get("id"): it.get("name") for it in ct.load_catalog(story_id).get("items", []) if isinstance(it, dict)}
    for node, ordv in mm.items():
        if not isinstance(ordv, int) or isinstance(ordv, bool) or ordv >= len(levels) - 1:
            continue
        T = tt.cultivate_threshold(story_id, cfg, state, node)
        acc = techdays.get(node, 0) or 0
        need = max(round(T - acc, 2), 0) or T
        actions.append({
            "id": f"cultivate:{node}", "label": f"閉關：{id2name.get(node, node)}",
            "icon": "🧘‍♂️", "category": "修煉",
            "desc": f"修至下一境（{levels[ordv]} → {levels[min(ordv + 1, len(levels) - 1)]}）",
            "cost_days": need, "gain": {}, "kind": "cultivate", "available": True, "unmet": [],
        })

    # --- structural verbs (registry) — engine-computed availability, GM-independent ---
    for spec in STRUCTURAL_ACTIONS:
        try:
            if not spec["available"](state):
                continue
        except Exception:
            continue
        actions.append({
            "id": spec["id"], "label": spec["label"], "icon": spec.get("icon", ""),
            "category": spec.get("category", "行動"), "desc": spec.get("desc", ""),
            "cost_days": spec.get("cost_days"), "gain": {}, "kind": "structural",
            "available": True, "unmet": [], "params": _resolve_params(story_id, branch_id, spec.get("params")),
        })

    ts = tt.status(story_id, branch_id)
    return {
        "ok": True, "now": ts.get("now", 0), "unit": ts.get("unit", "天"),
        "upcoming": ts.get("upcoming", []),
        "balance": balance, "currency_field": cur_field, "currency_label": cur_label,
        "actions": actions,
    }


def _do_structural(story_id: str, branch_id: str, spec: dict, args: dict) -> dict:
    """Run a structural verb through the engine (gm_v2.toolexec.execute_tool) — the
    SAME deterministic path the GM's function-calling uses. Availability is re-checked
    server-side (never trust the client). Op-logged so the narrator acknowledges it."""
    state = _state(story_id, branch_id)
    try:
        if not spec["available"](state):
            return {"ok": False, "error": "unavailable", "action": spec["id"]}
    except Exception:
        return {"ok": False, "error": "unavailable", "action": spec["id"]}
    try:
        from story_core.gm_v2 import toolexec  # lazy: app-coupled executor
    except Exception as exc:
        return {"ok": False, "error": "engine_unavailable", "detail": str(exc)}
    res = toolexec.execute_tool(story_id, branch_id, spec["executor"], args or {})
    if isinstance(res, dict) and res.get("ok"):
        try:
            _catalog_tools()._append_op(story_id, branch_id, {"op": spec["id"], **(args or {})})
        except Exception:
            pass
    return res


def do_action(story_id: str, branch_id: str, action_id: str, args: dict | None = None) -> dict:
    """Execute a menu action deterministically: spend time (fires deadlines), apply
    gains, op-log for the narrator. `cultivate:<node>` delegates to the time engine;
    structural verbs (副本進/出…) route to the engine via the registry."""
    tt, ct = _time_tools(), _catalog_tools()
    cfg = tt.config(story_id)
    cur_field, cur_label = _currency(cfg)

    spec = _STRUCTURAL_BY_ID.get(action_id)
    if spec:
        return _do_structural(story_id, branch_id, spec, args or {})

    if action_id.startswith("cultivate:"):
        node = action_id.split(":", 1)[1]
        res = tt.cultivate_session(story_id, branch_id, node)  # days=None → to next breakthrough
        if res.get("ok"):
            ct._append_op(story_id, branch_id, {
                "op": "cultivate", "item_id": node,
                "days": res.get("spent"), "mastery": res.get("mastery_label"),
            })
        return res

    act = (cfg.get("actions") or {}).get(action_id)
    if not act:
        return {"ok": False, "error": "unknown_action", "action": action_id}

    adv = tt.advance(story_id, branch_id, action_id)  # spend time + fire deadlines
    if not adv.get("ok"):
        return adv

    gained: dict = {}
    if act.get("gain"):
        state = _state(story_id, branch_id)
        for k, v in act["gain"].items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                state[k] = _balance(state, k) + v
                gained[k] = v
        sio._save_json(sio._story_character_state_path(story_id, branch_id), state)

    ct._append_op(story_id, branch_id, {
        "op": "action", "action": act.get("label", action_id),
        "days": adv.get("spent"), "gain": gained,
    })
    return {
        "ok": True, "action": action_id, "label": act.get("label", action_id),
        "spent_days": adv.get("spent"), "now": adv.get("now"), "fired": adv.get("fired", []),
        "gained": gained, "currency_label": cur_label,
    }


def _main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(description="deterministic menu-action engine")
    p.add_argument("cmd", choices=["list", "do"])
    p.add_argument("--story")
    p.add_argument("--branch")
    p.add_argument("--action")
    p.add_argument("--args", help="JSON args for structural actions, e.g. '{\"ip\":\"鬼滅之刃\"}'")
    a = p.parse_args(argv)

    args = {}
    if a.args:
        try:
            args = json.loads(a.args)
        except Exception:
            args = {}

    if not (a.story and a.branch):
        out = {"ok": False, "error": "need --story --branch"}
    elif a.cmd == "list":
        out = list_actions(a.story, a.branch)
    else:
        out = do_action(a.story, a.branch, a.action, args) if a.action else {"ok": False, "error": "do needs --action"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
