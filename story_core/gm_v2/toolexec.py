"""Tool executor — bridge from ONE function-call to the engine (Phase 2).

`execute_tool(story, branch, tool_name, args)` resolves a tool → registry op →
gate (validate) → apply (persist), coercing the function-calling **singular arg
object** into the shape each op's gate/apply expects (FC args are objects, not
arrays, so a 'multi' op = one item per call → wrapped to a 1-element list).

Used by the Gemini function-calling loop AND testable standalone (no LLM). The
result dict is what gets returned to the model as the functionResponse, so a
rejected/invalid call bounces back and the model can correct (the feedback the
delta-block transport lacked). App-coupled (apply writes real state); the registry
+ gates stay app-free in ops.py. See doc/architecture_v2.md.
"""

from story_core.gm_v2 import apply as _apply
from story_core.gm_v2.ops import DELTA_OPS

_BY_TOOL = {op["tool"]: op for op in DELTA_OPS}


def _coerce(op_key: str, args: dict):
    """FC singular arg-object → the value shape the op's gate/apply expects."""
    args = args or {}
    if op_key in ("npcs", "catalog_upsert", "cultivate", "quests"):
        return [args]                              # one item per call → 1-elem list
    if op_key == "lore":
        return [args.get("text") or args.get("lore") or ""]
    if op_key == "story_anchors":
        return [args.get("text") or args.get("fact") or args.get("anchor") or ""]
    if op_key == "state_delta":
        return args.get("changes") or args         # the map of field changes
    if op_key == "recap":
        return args.get("text") or args.get("recap") or ""
    return args                                    # time: {days, hours}


# apply dispatch — normalized (story, branch, clean, ctx) → result; names match ops.apply
def _ap_state(s, b, v, ctx):
    _apply.apply_state_delta(s, b, v)
    return {"applied": True}


def _ap_npcs(s, b, v, ctx):
    return {"saved": _apply.apply_npcs(s, b, v, ctx.get("msg_index"), ctx.get("run_ctx"))}


def _ap_catalog(s, b, v, ctx):
    res = _apply.apply_catalog_upsert(s, v)        # story-level (no branch)
    if not res:
        return {"ok": False, "reason": "catalog_rejected", "hint": "vendor 不存在或品項無效"}
    return {"ok": True, "added": res}


def _ap_cultivate(s, b, v, ctx):
    res = _apply.apply_cultivate(s, b, v)          # list of promotions that actually applied
    if not res:
        return {"ok": False, "reason": "not_owned",
                "hint": "該功法尚未習得，無法閉關修煉；若只是時間流逝請改用 advance_time"}
    return {"ok": True, "results": res}


def _ap_time(s, b, v, ctx):
    return _apply.advance_time(s, b, v)


def _ap_quest_add(s, b, v, ctx):
    added = _apply.apply_quest_add(s, b, v)
    return {"ok": True, "added": added}


def _ap_anchors(s, b, v, ctx):
    added = _apply.apply_story_anchors_add(s, b, v)
    return {"ok": True, "added": added}


_APPLY = {
    "apply_state_delta": _ap_state,
    "apply_npcs": _ap_npcs,
    "apply_catalog_upsert": _ap_catalog,
    "apply_cultivate": _ap_cultivate,
    "advance_time": _ap_time,
    "apply_quest_add": _ap_quest_add,
    "apply_story_anchors_add": _ap_anchors,
}


def _catalog():
    import os
    import sys
    play_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts", "play")
    if play_dir not in sys.path:
        sys.path.insert(0, play_dir)
    import catalog_tools
    return catalog_tools


def _do_purchase(story_id, branch_id, args, ctx):
    """Deterministic purchase (FC-only): catalog item (vendor+item_id) OR ad-hoc (item+price).
    The ENGINE does exact currency math → the LLM never does arithmetic (no double-count, no
    absolute-overwrite). This is why currency lives here, not in set_state."""
    args = args or {}
    vendor = str(args.get("vendor") or "").strip()
    item_id = str(args.get("item_id") or "").strip()
    ct = _catalog()
    if vendor and item_id:
        return ct.buy(story_id, branch_id, vendor, item_id)
    return ct.spend_and_grant(story_id, branch_id, args.get("price"), args.get("item"),
                              grant_to=str(args.get("grant_to") or "inventory"))


def _do_enter_dungeon(story_id, branch_id, args, ctx):
    return _apply.enter_dungeon(story_id, branch_id, args.get("ip"))


def _do_exit_dungeon(story_id, branch_id, args, ctx):
    return _apply.exit_dungeon(story_id, branch_id, str(args.get("outcome") or "cleared"))


def _do_complete_quest(story_id, branch_id, args, ctx):
    return _apply.complete_quest(story_id, branch_id, str(args.get("quest_id") or args.get("id") or ""),
                                 str(args.get("outcome") or "done"))


# function-calling-ONLY tools (deliberately NOT in the delta block: currency arithmetic +
# critical lifecycle must be deterministic, never inferred from the GM's free-text delta).
_FC_ONLY = {
    "purchase": {
        "description": "確定性購買：花命運點數取得一件東西（引擎精確扣款；劇情中自由購買用這個，**絕不要**用 set_state 改錢）。買 catalog 既有品項給 vendor+item_id；自由購買給 item+price（＋選填 grant_to）。",
        "parameters": {"type": "object", "properties": {
            "item": {"type": "string", "description": "物品名（自由購買）"},
            "price": {"type": "number", "description": "花費的命運點數（自由購買）"},
            "grant_to": {"type": "string", "description": "inventory/techniques/talents；預設 inventory"},
            "vendor": {"type": "string", "description": "catalog vendor id（買既有品項時）"},
            "item_id": {"type": "string", "description": "catalog item id（買既有品項時）"},
        }},
        "handler": _do_purchase,
    },
    "enter_dungeon": {
        "description": "進入試煉秘境/副本（**進副本就呼叫這個，不要用 set_state 改 current_dungeon**）。引擎原子設好 current_dungeon＋phase＋生成劇本＋扣副本時間。",
        "parameters": {"type": "object", "properties": {
            "ip": {"type": "string", "description": "副本 IP 名（如 鬼滅之刃 / 火影忍者）"},
        }, "required": ["ip"]},
        "handler": _do_enter_dungeon,
    },
    "exit_dungeon": {
        "description": "離開/通關副本（**結束副本就呼叫這個，不要用 set_state**）。引擎原子結算 blueprint 獎勵＋清 current_dungeon＋設 phase＋記 completed。",
        "parameters": {"type": "object", "properties": {
            "outcome": {"type": "string", "description": "cleared(通關，預設) / fled(逃離) / died(身死)"},
        }},
        "handler": _do_exit_dungeon,
    },
    "complete_quest": {
        "description": "結算一個任務（完成/失敗就呼叫這個，**不要用 set_state 改 quests 或發獎**）。引擎標記狀態＋精確發放開任務時宣告的獎勵。開新任務請用 add_quest。",
        "parameters": {"type": "object", "properties": {
            "quest_id": {"type": "string", "description": "要結算的任務 id"},
            "outcome": {"type": "string", "description": "done(完成，預設·發獎) / failed(失敗·不發獎)"},
        }, "required": ["quest_id"]},
        "handler": _do_complete_quest,
    },
}


def execute_tool(story_id: str, branch_id: str, tool_name: str, args: dict,
                 schema: dict | None = None, ctx: dict | None = None) -> dict:
    """Validate + apply one tool-call. Returns the functionResponse payload (ok/
    rejected/error) — a rejected call informs the model so it can fix and retry."""
    op = _BY_TOOL.get(tool_name)
    if not op:
        fc = _FC_ONLY.get(tool_name)
        if fc:
            try:
                res = fc["handler"](story_id, branch_id, args or {}, ctx or {})
            except Exception as exc:
                return {"ok": False, "tool": tool_name, "reason": "apply_error", "error": str(exc)}
            base = res if isinstance(res, dict) else {"result": res}
            ok = not (isinstance(res, dict) and res.get("ok") is False)
            return {"ok": ok, "tool": tool_name, **{k: v for k, v in base.items() if k != "ok"}}
        return {"ok": False, "error": "unknown_tool", "tool": tool_name,
                "known": sorted(_BY_TOOL) + sorted(_FC_ONLY)}
    value = _coerce(op["key"], args)
    clean, dropped, flagged = op["gate"](value, schema or {})
    if not clean:
        return {"ok": False, "tool": tool_name, "reason": "rejected",
                "dropped": dropped, "hint": "args failed validation — fix and retry"}
    apply_name = op.get("apply")
    applied = None
    if apply_name and apply_name in _APPLY:
        try:
            applied = _APPLY[apply_name](story_id, branch_id, clean, ctx or {})
        except Exception as exc:  # surface to the model, don't crash the turn
            return {"ok": False, "tool": tool_name, "reason": "apply_error", "error": str(exc)}
        # surface apply-LEVEL failure (cultivate not_owned, catalog rejected) so the model
        # gets feedback and self-corrects (e.g. retries with advance_time) — the boundary
        # feedback the delta transport lacked.
        if isinstance(applied, dict) and applied.get("ok") is False:
            return {"ok": False, "tool": tool_name, **{k: v for k, v in applied.items() if k != "ok"}}
    return {"ok": True, "tool": tool_name, "applied": applied,
            "dropped": dropped, "flagged": flagged,
            "deferred": apply_name is None}  # lore/recap: gated+kept, persisted elsewhere


def tool_declarations() -> list:
    """Gemini functionDeclarations (singular per-item params from the registry)."""
    decls = []
    for op in DELTA_OPS:
        p = op["params"]
        # multi ops: the FC tool takes ONE item = the array's item schema
        params = p.get("items") if isinstance(p, dict) and p.get("type") == "array" else p
        if op["key"] == "lore":
            params = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
        elif op["key"] == "story_anchors":
            params = {"type": "object", "properties": {"text": {"type": "string",
                      "description": "要長期記住的一條關鍵事實"}}, "required": ["text"]}
        elif op["key"] == "recap":
            params = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
        elif op["key"] == "state_delta":
            params = {"type": "object", "properties": {"changes": op["params"]}, "required": ["changes"]}
        decls.append({"name": op["tool"], "description": op["summary"], "parameters": params})
    for name, spec in _FC_ONLY.items():
        decls.append({"name": name, "description": spec["description"], "parameters": spec["parameters"]})
    return decls


__all__ = ["execute_tool", "tool_declarations"]
