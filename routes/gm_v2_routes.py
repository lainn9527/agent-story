"""GM v2 route — parallel to the legacy /api/send path (decision 2).

Single grounded Gemini call per turn: PRE -> generate -> parse -> gate -> apply
(via production persistence, 方案甲) -> async critic. The legacy /api/send and its
extractor/guard stack are untouched; this is opt-in via /api/v2/send.

V1 is non-streaming (returns the full narration). Streaming (narration first,
delta block buffered server-side) is a follow-up that reuses the same pipeline.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

log = logging.getLogger("rpg")
gm_v2_bp = Blueprint("gm_v2", __name__)


def _app():
    import app as app_module

    return app_module


@gm_v2_bp.route("/api/v2/send", methods=["POST"])
def api_v2_send():
    app_module = _app()
    data = request.get_json(silent=True) or {}

    story_id = data.get("story_id") or app_module._active_story_id()
    branch_id = data.get("branch_id")
    if not branch_id:
        tree = app_module._load_tree(story_id)
        branch_id = tree.get("active_branch_id", "main")

    action = (data.get("message") or data.get("action") or "").strip()
    if not action:
        return jsonify({"ok": False, "error": "empty message"}), 400

    try:
        from story_core.gm_v2.pipeline import run_turn

        result = run_turn(story_id, branch_id, action)
        if result.get("error"):
            return jsonify({"ok": False, "error": result["error"]}), 503
        return jsonify({"ok": True, "story_id": story_id, "branch_id": branch_id, **result})
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/send failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Catalog (vendor/shop) — deterministic, UI-driven, NO LLM. Backed by the
# app-free engine scripts/play/catalog_tools.py (v2). view is read-only; buy is
# an additive v2 write (deduct currency + grant item) that does NOT touch the v1
# pipeline. story-level catalog.json + per-branch character_state.
# ---------------------------------------------------------------------------

def _catalog_tools():
    import os
    import sys

    play_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "play"
    )
    if play_dir not in sys.path:
        sys.path.insert(0, play_dir)
    import catalog_tools

    return catalog_tools


def _time_tools():
    import os
    import sys

    play_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "play"
    )
    if play_dir not in sys.path:
        sys.path.insert(0, play_dir)
    import time_tools

    return time_tools


def _action_tools():
    import os
    import sys

    play_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "play"
    )
    if play_dir not in sys.path:
        sys.path.insert(0, play_dir)
    import action_tools

    return action_tools


def _resolve_story_branch(app_module, data_or_args, is_get: bool):
    story_id = data_or_args.get("story_id") or app_module._active_story_id()
    branch_id = data_or_args.get("branch_id")
    if not branch_id:
        tree = app_module._load_tree(story_id)
        branch_id = tree.get("active_branch_id", "main")
    return story_id, branch_id


@gm_v2_bp.route("/api/v2/catalog", methods=["GET"])
def api_v2_catalog():
    app_module = _app()
    story_id, branch_id = _resolve_story_branch(app_module, request.args, True)
    vendor = request.args.get("vendor")
    try:
        return jsonify(_catalog_tools().view(story_id, branch_id, vendor))
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/catalog failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@gm_v2_bp.route("/api/v2/catalog/buy", methods=["POST"])
def api_v2_catalog_buy():
    app_module = _app()
    data = request.get_json(silent=True) or {}
    story_id, branch_id = _resolve_story_branch(app_module, data, False)
    vendor = (data.get("vendor") or "").strip()
    item_id = (data.get("item_id") or data.get("id") or "").strip()
    if not vendor or not item_id:
        return jsonify({"ok": False, "error": "vendor and item_id required"}), 400
    try:
        ct = _catalog_tools()
        pre = ct.snapshot_branch(story_id, branch_id)  # capture BEFORE the op (undo support)
        res = ct.buy(story_id, branch_id, vendor, item_id)
        if isinstance(res, dict) and res.get("ok"):
            ct.push_undo(story_id, branch_id, pre)
            app_module._sync_latest_gm_snapshot(story_id, branch_id)  # carry buy across fork/regen
        return jsonify(res)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/catalog/buy failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@gm_v2_bp.route("/api/v2/catalog/refund", methods=["POST"])
def api_v2_catalog_refund():
    app_module = _app()
    data = request.get_json(silent=True) or {}
    story_id, branch_id = _resolve_story_branch(app_module, data, False)
    vendor = (data.get("vendor") or "").strip()
    item_id = (data.get("item_id") or data.get("id") or "").strip()
    if not vendor or not item_id:
        return jsonify({"ok": False, "error": "vendor and item_id required"}), 400
    try:
        ct = _catalog_tools()
        pre = ct.snapshot_branch(story_id, branch_id)
        res = ct.refund(story_id, branch_id, vendor, item_id)
        if isinstance(res, dict) and res.get("ok"):
            ct.push_undo(story_id, branch_id, pre)
            app_module._sync_latest_gm_snapshot(story_id, branch_id)  # carry refund across fork/regen
        return jsonify(res)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/catalog/refund failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@gm_v2_bp.route("/api/v2/inventory/remove", methods=["POST"])
def api_v2_inventory_remove():
    """丟棄物品欄道具 — deterministic, NOT ledger-gated (clears quest/loot items the GM
    forgot to remove). No currency refund. Same undo + snapshot-sync as buy/refund so it
    survives fork/regen instead of being rewound back into the inventory."""
    app_module = _app()
    data = request.get_json(silent=True) or {}
    story_id, branch_id = _resolve_story_branch(app_module, data, False)
    name = (data.get("name") or data.get("item") or "").strip()
    field = (data.get("field") or "inventory").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    try:
        ct = _catalog_tools()
        pre = ct.snapshot_branch(story_id, branch_id)  # capture BEFORE the op (undo support)
        res = ct.discard_item(story_id, branch_id, name, field)
        if isinstance(res, dict) and res.get("ok"):
            ct.push_undo(story_id, branch_id, pre)
            app_module._sync_latest_gm_snapshot(story_id, branch_id)  # carry discard across fork/regen
        return jsonify(res)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/inventory/remove failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Skill tree (功法樹) — read-only graph view; unlock reuses the requires-gated
# buy(); cultivate advances 成 (GM/narrative-driven). Same app-free engine.
# ---------------------------------------------------------------------------

@gm_v2_bp.route("/api/v2/time", methods=["GET"])
def api_v2_time():
    app_module = _app()
    story_id, branch_id = _resolve_story_branch(app_module, request.args, True)
    try:
        return jsonify(_time_tools().status(story_id, branch_id))
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/time failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@gm_v2_bp.route("/api/v2/quests", methods=["GET"])
def api_v2_quests():
    app_module = _app()
    story_id, branch_id = _resolve_story_branch(app_module, request.args, True)
    try:
        from story_core.gm_v2.apply import _load_quests
        return jsonify({"ok": True, "quests": _load_quests(story_id, branch_id)})
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/quests failed")
        return jsonify({"ok": False, "error": str(exc), "quests": []}), 500


@gm_v2_bp.route("/api/v2/actions", methods=["GET"])
def api_v2_actions():
    app_module = _app()
    story_id, branch_id = _resolve_story_branch(app_module, request.args, True)
    try:
        return jsonify(_action_tools().list_actions(story_id, branch_id))
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/actions failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@gm_v2_bp.route("/api/v2/actions/do", methods=["POST"])
def api_v2_actions_do():
    app_module = _app()
    data = request.get_json(silent=True) or {}
    story_id, branch_id = _resolve_story_branch(app_module, data, False)
    action_id = (data.get("action") or data.get("action_id") or "").strip()
    if not action_id:
        return jsonify({"ok": False, "error": "action required"}), 400
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    try:
        ct = _catalog_tools()
        pre = ct.snapshot_branch(story_id, branch_id)  # capture BEFORE the op (undo support)
        res = _action_tools().do_action(story_id, branch_id, action_id, args)
        if isinstance(res, dict) and res.get("ok"):
            ct.push_undo(story_id, branch_id, pre)
            app_module._sync_latest_gm_snapshot(story_id, branch_id)  # carry action across fork/regen
        return jsonify(res)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/actions/do failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Pending player ops (this turn, not yet sent to GM) + LIFO undo.
# ---------------------------------------------------------------------------

@gm_v2_bp.route("/api/v2/ops/pending", methods=["GET"])
def api_v2_ops_pending():
    app_module = _app()
    story_id, branch_id = _resolve_story_branch(app_module, request.args, True)
    try:
        return jsonify(_catalog_tools().list_undo(story_id, branch_id))
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/ops/pending failed")
        return jsonify({"ok": False, "error": str(exc), "ops": [], "count": 0}), 500


@gm_v2_bp.route("/api/v2/ops/undo", methods=["POST"])
def api_v2_ops_undo():
    app_module = _app()
    data = request.get_json(silent=True) or {}
    story_id, branch_id = _resolve_story_branch(app_module, data, False)
    index = data.get("index")
    try:
        return jsonify(_catalog_tools().undo_to(story_id, branch_id, index))
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/ops/undo failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@gm_v2_bp.route("/api/v2/tree", methods=["GET"])
def api_v2_tree():
    app_module = _app()
    story_id, branch_id = _resolve_story_branch(app_module, request.args, True)
    vendor = request.args.get("vendor")
    try:
        return jsonify(_catalog_tools().tree_view(story_id, branch_id, vendor))
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/tree failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@gm_v2_bp.route("/api/v2/tree/unlock", methods=["POST"])
def api_v2_tree_unlock():
    app_module = _app()
    data = request.get_json(silent=True) or {}
    story_id, branch_id = _resolve_story_branch(app_module, data, False)
    vendor = (data.get("vendor") or "").strip()
    item_id = (data.get("item_id") or data.get("id") or "").strip()
    if not vendor or not item_id:
        return jsonify({"ok": False, "error": "vendor and item_id required"}), 400
    try:
        ct = _catalog_tools()
        pre = ct.snapshot_branch(story_id, branch_id)
        res = ct.buy(story_id, branch_id, vendor, item_id)
        if isinstance(res, dict) and res.get("ok"):
            ct.push_undo(story_id, branch_id, pre)
            app_module._sync_latest_gm_snapshot(story_id, branch_id)  # carry unlock across fork/regen
        return jsonify(res)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/tree/unlock failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@gm_v2_bp.route("/api/v2/tree/cultivate", methods=["POST"])
def api_v2_tree_cultivate():
    """閉關修煉：spend calendar days; the time engine auto-promotes 成 (level computed,
    not set). `days` optional — omit to 修到下一次突破."""
    app_module = _app()
    data = request.get_json(silent=True) or {}
    story_id, branch_id = _resolve_story_branch(app_module, data, False)
    item_id = (data.get("item_id") or data.get("id") or data.get("node") or "").strip()
    if not item_id:
        return jsonify({"ok": False, "error": "item_id required"}), 400
    days = data.get("days")
    days = days if isinstance(days, (int, float)) and not isinstance(days, bool) else None
    try:
        ct = _catalog_tools()
        pre = ct.snapshot_branch(story_id, branch_id)
        res = _time_tools().cultivate_session(story_id, branch_id, item_id, days)
        if isinstance(res, dict) and res.get("ok"):
            try:  # op-log so the GM acknowledges the 閉關 next turn (parity with the ⚡ menu path)
                ct._append_op(story_id, branch_id, {"op": "cultivate", "item_id": item_id,
                                                    "days": res.get("spent"), "mastery": res.get("mastery_label")})
            except Exception:
                pass
            ct.push_undo(story_id, branch_id, pre)
            app_module._sync_latest_gm_snapshot(story_id, branch_id)  # carry cultivate across fork/regen
        return jsonify(res)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("gm_v2 /api/v2/tree/cultivate failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
