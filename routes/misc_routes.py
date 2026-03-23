from __future__ import annotations

from datetime import datetime, timezone
from flask import Blueprint, jsonify, request, send_file
import json
import logging
import os
import uuid


log = logging.getLogger("rpg")
misc_bp = Blueprint("misc", __name__)


def _app():
    import app as app_module

    return app_module


def _active_branch_id(story_id: str) -> str:
    app_module = _app()
    helper = getattr(app_module, "_active_branch_id", None)
    if callable(helper):
        return helper(story_id)
    return app_module._load_tree(story_id).get("active_branch_id", "main")


@misc_bp.route("/api/status")
def api_status():
    app_module = _app()
    story_id = app_module._active_story_id()
    branch_id = request.args.get("branch_id", "main")
    tree = app_module._load_tree(story_id)
    active_branch_id = tree.get("active_branch_id", "main")
    loaded_save = app_module._get_loaded_save_preview(story_id, tree, branch_id)

    if branch_id == active_branch_id and tree.get("loaded_save_id") and not loaded_save:
        if app_module._clear_loaded_save_preview(tree):
            app_module._save_tree(story_id, tree)

    if loaded_save:
        state = dict(
            loaded_save.get("character_snapshot")
            or app_module._load_character_state(story_id, branch_id)
        )
        state["world_day"] = loaded_save.get("world_day", app_module.get_world_day(story_id, branch_id))
        state["loaded_save_id"] = loaded_save.get("id")
    else:
        state = dict(app_module._load_character_state(story_id, branch_id))
        state["world_day"] = app_module.get_world_day(story_id, branch_id)

    state["dice_modifier"] = app_module.get_dice_modifier(app_module._story_dir(story_id), branch_id)
    state["dice_always_success"] = app_module.get_dice_always_success(
        app_module._story_dir(story_id), branch_id
    )
    state["pistol_mode"] = app_module.get_pistol_mode(app_module._story_dir(story_id), branch_id)
    return jsonify(state)


@misc_bp.route("/api/state/rebuild", methods=["POST"])
def api_state_rebuild():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(silent=True) or {}
    branch_id = body.get("branch_id")
    if not branch_id:
        tree = app_module._load_tree(story_id)
        branch_id = tree.get("active_branch_id", "main")
    state = app_module._load_character_state(story_id, branch_id)
    npcs = app_module._load_npcs(story_id, branch_id, include_archived=True)
    count = app_module.rebuild_state_db_from_json(story_id, branch_id, state=state, npcs=npcs)
    summary = app_module.get_state_summary(story_id, branch_id)
    return jsonify({"ok": True, "branch_id": branch_id, "count": count, "summary": summary})


@misc_bp.route("/api/state/cleanup", methods=["POST"])
def api_state_cleanup():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(silent=True) or {}
    branch_id = body.get("branch_id")
    if not branch_id:
        tree = app_module._load_tree(story_id)
        branch_id = tree.get("active_branch_id", "main")
    from story_core.state_cleanup import run_state_cleanup_sync

    try:
        summary = run_state_cleanup_sync(story_id, branch_id)
        return jsonify({"ok": True, "branch_id": branch_id, "summary": summary})
    except Exception as exc:
        log.warning("api_state_cleanup error: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@misc_bp.route("/api/npcs")
def api_npcs():
    app_module = _app()
    story_id = app_module._active_story_id()
    branch_id = request.args.get("branch_id", "main")
    include_archived = request.args.get("include_archived", "").strip().lower() in {"1", "true", "yes"}
    npcs = app_module._load_npcs(story_id, branch_id, include_archived=include_archived)
    return jsonify({"ok": True, "npcs": npcs})


@misc_bp.route("/api/npcs", methods=["POST"])
def api_npcs_create():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "main")
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    app_module._save_npc(story_id, body, branch_id)
    return jsonify(
        {"ok": True, "npcs": app_module._load_npcs(story_id, branch_id, include_archived=True)}
    )


@misc_bp.route("/api/npcs/<npc_id>", methods=["DELETE"])
def api_npcs_delete(npc_id: str):
    app_module = _app()
    story_id = app_module._active_story_id()
    branch_id = request.args.get("branch_id", "main")
    include_archived = request.args.get("include_archived", "").strip().lower() in {"1", "true", "yes"}
    npcs = app_module._load_npcs(story_id, branch_id, include_archived=True)
    removed_names = [
        npc.get("name", "").strip()
        for npc in npcs
        if npc.get("id") == npc_id and npc.get("name")
    ]
    npcs = [npc for npc in npcs if npc.get("id") != npc_id]
    app_module._save_json(app_module._story_npcs_path(story_id, branch_id), npcs)
    for name in removed_names:
        app_module.delete_state_entry(story_id, branch_id, category="npc", entry_key=name)
    return jsonify(
        {
            "ok": True,
            "npcs": app_module._load_npcs(
                story_id, branch_id, include_archived=include_archived
            ),
        }
    )


@misc_bp.route("/api/events")
def api_events():
    app_module = _app()
    story_id = app_module._active_story_id()
    branch_id = request.args.get("branch_id")
    limit = int(request.args.get("limit", "50"))
    events = app_module.get_events(story_id, branch_id=branch_id, limit=limit)
    return jsonify({"ok": True, "events": events})


@misc_bp.route("/api/events/search")
def api_events_search():
    app_module = _app()
    story_id = app_module._active_story_id()
    query = request.args.get("q", "").strip()
    branch_id = request.args.get("branch_id")
    limit = int(request.args.get("limit", "10"))
    if not query:
        return jsonify({"ok": True, "events": [], "count": 0})
    results = app_module.search_events_db(story_id, query, branch_id=branch_id, limit=limit)
    return jsonify({"ok": True, "events": results, "count": len(results)})


@misc_bp.route("/api/events/<int:event_id>", methods=["PATCH"])
def api_events_update(event_id: int):
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    new_status = body.get("status", "").strip()
    if new_status not in ("planted", "triggered", "resolved", "abandoned"):
        return jsonify({"ok": False, "error": "invalid status"}), 400
    app_module.update_event_status(story_id, event_id, new_status)
    event = app_module.get_event_by_id(story_id, event_id)
    return jsonify({"ok": True, "event": event})


@misc_bp.route("/api/images/status")
def api_images_status():
    app_module = _app()
    story_id = app_module._active_story_id()
    filename = request.args.get("filename", "")
    if not filename:
        return jsonify({"ok": False, "error": "filename required"}), 400
    status = app_module.get_image_status(story_id, filename)
    if status.get("ready"):
        app_module._sync_message_image_ready(story_id, filename)
    response = jsonify({"ok": True, **status})
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@misc_bp.route("/api/stories/<story_id>/images/<filename>")
def api_images_serve(story_id: str, filename: str):
    app_module = _app()
    if "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"ok": False, "error": "invalid filename"}), 400
    path = app_module.get_image_path(story_id, filename)
    if not path:
        return jsonify({"ok": False, "error": "image not found"}), 404
    return send_file(path, mimetype="image/png")


@misc_bp.route("/api/npc-activities")
def api_npc_activities():
    app_module = _app()
    story_id = app_module._active_story_id()
    branch_id = request.args.get("branch_id", "main")
    activities = app_module.get_all_activities(story_id, branch_id)
    return jsonify({"ok": True, "activities": activities})


@misc_bp.route("/api/saves")
def api_saves_list():
    app_module = _app()
    story_id = app_module._active_story_id()
    saves = app_module._load_json(app_module._story_saves_path(story_id), [])
    slim = []
    for save in saves:
        entry = {
            key: value
            for key, value in save.items()
            if key not in ("character_snapshot", "npc_snapshot", "recap_snapshot")
        }
        slim.append(entry)
    return jsonify({"ok": True, "saves": slim})


@misc_bp.route("/api/saves", methods=["POST"])
def api_saves_create():
    app_module = _app()
    body = request.get_json(force=True)
    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branch_id = tree.get("active_branch_id", "main")

    timeline = app_module.get_full_timeline(story_id, branch_id)
    last_index = timeline[-1].get("index", len(timeline) - 1) if timeline else 0

    character_state = app_module._load_character_state(story_id, branch_id)
    npcs = app_module._load_npcs(story_id, branch_id, include_archived=True)
    world_day = app_module.get_world_day(story_id, branch_id)
    recap = app_module.load_recap(story_id, branch_id)

    last_gm = ""
    for message in reversed(timeline):
        if message.get("role") == "gm":
            last_gm = message.get("content", "")[:100]
            break

    save_id = f"save_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    branch_meta = tree.get("branches", {}).get(branch_id, {})
    branch_name = branch_meta.get("title") or branch_meta.get("name") or branch_id

    save_entry = {
        "id": save_id,
        "name": body.get("name", "").strip() or f"{branch_name} — 第{int(world_day) + 1}天",
        "branch_id": branch_id,
        "message_index": last_index,
        "created_at": now,
        "world_day": world_day,
        "character_snapshot": character_state,
        "npc_snapshot": npcs,
        "recap_snapshot": recap,
        "preview": last_gm,
    }

    saves = app_module._load_json(app_module._story_saves_path(story_id), [])
    saves.insert(0, save_entry)
    app_module._save_json(app_module._story_saves_path(story_id), saves)

    log.info("save created: %s on branch %s at index %d", save_id, branch_id, last_index)
    return jsonify({"ok": True, "save": save_entry})


@misc_bp.route("/api/saves/<save_id>/load", methods=["POST"])
def api_saves_load(save_id: str):
    app_module = _app()
    story_id = app_module._active_story_id()
    saves = app_module._load_json(app_module._story_saves_path(story_id), [])
    save = next((entry for entry in saves if entry["id"] == save_id), None)
    if not save:
        return jsonify({"ok": False, "error": "save not found"}), 404

    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    branch_id = save["branch_id"]

    if branch_id != "main" and branch_id not in branches:
        return jsonify({"ok": False, "error": "original branch no longer exists"}), 404

    branch_meta = branches.get(branch_id)
    if not branch_meta:
        return jsonify({"ok": False, "error": "branch metadata missing"}), 500

    tree["active_branch_id"] = branch_id
    tree["loaded_save_id"] = save_id
    tree["loaded_save_branch_id"] = branch_id
    app_module._save_tree(story_id, tree)

    log.info("save loaded: %s → switched to branch %s (status preview on)", save_id, branch_id)
    return jsonify({"ok": True, "branch_id": branch_id, "branch": branch_meta})


@misc_bp.route("/api/saves/<save_id>", methods=["DELETE"])
def api_saves_delete(save_id: str):
    app_module = _app()
    story_id = app_module._active_story_id()
    saves = app_module._load_json(app_module._story_saves_path(story_id), [])
    new_saves = [save for save in saves if save["id"] != save_id]
    if len(new_saves) == len(saves):
        return jsonify({"ok": False, "error": "save not found"}), 404
    app_module._save_json(app_module._story_saves_path(story_id), new_saves)
    tree = app_module._load_tree(story_id)
    if tree.get("loaded_save_id") == save_id and app_module._clear_loaded_save_preview(tree):
        app_module._save_tree(story_id, tree)
    log.info("save deleted: %s", save_id)
    return jsonify({"ok": True})


@misc_bp.route("/api/saves/<save_id>", methods=["PUT"])
def api_saves_rename(save_id: str):
    app_module = _app()
    body = request.get_json(force=True)
    new_name = body.get("name", "").strip()
    if not new_name:
        return jsonify({"ok": False, "error": "name required"}), 400

    story_id = app_module._active_story_id()
    saves = app_module._load_json(app_module._story_saves_path(story_id), [])
    for save in saves:
        if save["id"] == save_id:
            save["name"] = new_name
            app_module._save_json(app_module._story_saves_path(story_id), saves)
            return jsonify({"ok": True, "save": save})
    return jsonify({"ok": False, "error": "save not found"}), 404


@misc_bp.route("/api/auto-play/summaries")
def api_auto_play_summaries():
    app_module = _app()
    story_id = app_module._active_story_id()
    branch_id = request.args.get("branch_id", "main")
    return jsonify({"ok": True, "summaries": app_module.get_summaries(story_id, branch_id)})


@misc_bp.route("/api/bug-report", methods=["POST"])
def api_bug_report():
    app_module = _app()
    data = request.get_json(force=True)
    story_id = app_module._active_story_id()
    report = {
        "story_id": story_id,
        "branch_id": data.get("branch_id", ""),
        "message_index": data.get("message_index"),
        "role": data.get("role", ""),
        "content_preview": data.get("content_preview", "")[:500],
        "description": data.get("description", "")[:2000],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    reports_path = os.path.join(app_module._story_dir(story_id), "bug_reports.json")
    reports = app_module._load_json(reports_path, [])
    if len(reports) >= 500:
        reports = reports[-400:]
    reports.append(report)
    app_module._save_json(reports_path, reports)
    log.info("Bug report saved: branch=%s msg=%s", report["branch_id"], report["message_index"])
    return jsonify({"ok": True})


@misc_bp.route("/api/config")
def api_config_get():
    app_module = _app()
    try:
        with open(app_module._LLM_CONFIG_PATH, "r", encoding="utf-8") as handle:
            cfg = json.load(handle)
    except Exception:
        cfg = {"provider": "claude_cli"}

    from story_core.gemini_key_manager import load_keys

    gemini_cfg = cfg.get("gemini", {})
    key_count = len(load_keys(gemini_cfg))

    return jsonify(
        {
            "ok": True,
            "version": app_module.__version__,
            "provider": cfg.get("provider", "claude_cli"),
            "gemini": {
                "model": gemini_cfg.get("model", "gemini-2.0-flash"),
                "key_count": key_count,
            },
            "claude_cli": {
                "model": cfg.get("claude_cli", {}).get("model", "claude-sonnet-4-5-20250929"),
            },
            "codex_agent": {
                "model": cfg.get("codex_agent", {}).get("model", "gpt-5.4"),
            },
        }
    )


@misc_bp.route("/api/config", methods=["POST"])
def api_config_set():
    app_module = _app()
    data = request.get_json(force=True)

    try:
        with open(app_module._LLM_CONFIG_PATH, "r", encoding="utf-8") as handle:
            cfg = json.load(handle)
    except Exception:
        cfg = {"provider": "claude_cli"}

    if "provider" in data:
        cfg["provider"] = data["provider"]

    if "gemini" in data and isinstance(data["gemini"], dict):
        if "gemini" not in cfg:
            cfg["gemini"] = {}
        if "model" in data["gemini"]:
            cfg["gemini"]["model"] = data["gemini"]["model"]

    if "claude_cli" in data and isinstance(data["claude_cli"], dict):
        if "claude_cli" not in cfg:
            cfg["claude_cli"] = {}
        if "model" in data["claude_cli"]:
            cfg["claude_cli"]["model"] = data["claude_cli"]["model"]

    if "codex_agent" in data and isinstance(data["codex_agent"], dict):
        if "codex_agent" not in cfg:
            cfg["codex_agent"] = {}
        if "model" in data["codex_agent"]:
            cfg["codex_agent"]["model"] = data["codex_agent"]["model"]

    with open(app_module._LLM_CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, indent=2, ensure_ascii=False)

    log.info("api_config_set: updated — provider=%s", cfg.get("provider"))
    return jsonify({"ok": True})


@misc_bp.route("/api/cheats/dice", methods=["GET"])
def api_cheats_dice_get():
    app_module = _app()
    story_id = app_module._active_story_id()
    branch_id = request.args.get("branch_id", "main")
    story_dir = app_module._story_dir(story_id)
    return jsonify(
        {
            "always_success": app_module.get_dice_always_success(story_dir, branch_id),
            "dice_modifier": app_module.get_dice_modifier(story_dir, branch_id),
        }
    )


@misc_bp.route("/api/cheats/dice", methods=["POST"])
def api_cheats_dice_set():
    app_module = _app()
    body = request.get_json(force=True)
    story_id = app_module._active_story_id()
    branch_id = body.get("branch_id", "main")
    story_dir = app_module._story_dir(story_id)

    if "always_success" in body:
        enabled = bool(body["always_success"])
        app_module.set_dice_always_success(story_dir, branch_id, enabled)
        log.info("cheats/dice: always_success=%s branch=%s", enabled, branch_id)

    return jsonify(
        {
            "ok": True,
            "always_success": app_module.get_dice_always_success(story_dir, branch_id),
        }
    )


@misc_bp.route("/api/cheats/fate", methods=["GET"])
def api_cheats_fate_get():
    app_module = _app()
    story_id = app_module._active_story_id()
    branch_id = request.args.get("branch_id", "main")
    story_dir = app_module._story_dir(story_id)
    return jsonify({"fate_mode": app_module.get_fate_mode(story_dir, branch_id)})


@misc_bp.route("/api/cheats/fate", methods=["POST"])
def api_cheats_fate_set():
    app_module = _app()
    body = request.get_json(force=True)
    story_id = app_module._active_story_id()
    branch_id = body.get("branch_id", "main")
    story_dir = app_module._story_dir(story_id)

    if "fate_mode" in body:
        enabled = bool(body["fate_mode"])
        app_module.set_fate_mode(story_dir, branch_id, enabled)
        log.info("cheats/fate: fate_mode=%s branch=%s", enabled, branch_id)

    return jsonify({"ok": True, "fate_mode": app_module.get_fate_mode(story_dir, branch_id)})


@misc_bp.route("/api/cheats/pistol", methods=["GET"])
def api_cheats_pistol_get():
    app_module = _app()
    story_id = app_module._active_story_id()
    branch_id = request.args.get("branch_id", "main")
    story_dir = app_module._story_dir(story_id)
    return jsonify({"pistol_mode": app_module.get_pistol_mode(story_dir, branch_id)})


@misc_bp.route("/api/cheats/pistol", methods=["POST"])
def api_cheats_pistol_set():
    app_module = _app()
    body = request.get_json(force=True)
    story_id = app_module._active_story_id()
    branch_id = body.get("branch_id", "main")
    story_dir = app_module._story_dir(story_id)

    if "pistol_mode" in body:
        enabled = bool(body["pistol_mode"])
        app_module.set_pistol_mode(story_dir, branch_id, enabled)
        log.info("cheats/pistol: pistol_mode=%s branch=%s", enabled, branch_id)

    return jsonify({"ok": True, "pistol_mode": app_module.get_pistol_mode(story_dir, branch_id)})


@misc_bp.route("/api/nsfw-preferences", methods=["GET"])
def api_nsfw_preferences_get():
    app_module = _app()
    story_id = app_module._active_story_id()
    return jsonify(app_module._load_nsfw_preferences(story_id))


@misc_bp.route("/api/nsfw-preferences", methods=["POST"])
def api_nsfw_preferences_set():
    app_module = _app()
    body = request.get_json(force=True)
    story_id = app_module._active_story_id()
    prefs = {
        "chips": body.get("chips", []),
        "custom": body.get("custom", "").strip(),
        "custom_chips": body.get("custom_chips", {}),
        "hidden_chips": body.get("hidden_chips", []),
        "chip_counts": body.get("chip_counts", {}),
    }
    path = app_module._nsfw_preferences_path(story_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(prefs, handle, ensure_ascii=False)
    log.info(
        "nsfw-preferences: saved %d chips + %d chars custom for story=%s",
        len(prefs["chips"]),
        len(prefs["custom"]),
        story_id,
    )
    return jsonify({"ok": True})


@misc_bp.route("/api/usage")
def api_usage():
    app_module = _app()
    if request.args.get("all") == "true":
        return jsonify(app_module.usage_db.get_total_usage())

    story_id = request.args.get("story_id") or app_module._active_story_id()
    try:
        days = int(request.args.get("days", 7))
    except (ValueError, TypeError):
        days = 7
    return jsonify(app_module.usage_db.get_usage_summary(story_id, days=days))


@misc_bp.route("/api/dungeon/enter", methods=["POST"])
def api_dungeon_enter():
    app_module = _app()
    story_id = request.json.get("story_id", app_module._active_story_id())
    branch_id = request.json.get("branch_id") or _active_branch_id(story_id)
    dungeon_id = request.json.get("dungeon_id")

    if not dungeon_id:
        return jsonify({"error": "dungeon_id required"}), 400

    template = app_module._load_dungeon_template(story_id, dungeon_id)
    if not template:
        return jsonify({"error": f"Dungeon {dungeon_id} not found"}), 404

    state = app_module._load_character_state(story_id, branch_id)
    player_rank = app_module._parse_rank(state.get("等級", "E"))
    required_rank = app_module._parse_rank(template["prerequisites"].get("min_rank", "E"))
    if player_rank < required_rank:
        return (
            jsonify(
                {
                    "error": "prerequisite_not_met",
                    "message": f"需要 {template['prerequisites']['min_rank']} 級以上才能進入此副本",
                }
            ),
            400,
        )

    progress = app_module._load_dungeon_progress(story_id, branch_id)
    if progress and progress.get("current_dungeon"):
        current_dungeon_id = progress["current_dungeon"]["dungeon_id"]
        current_template = app_module._load_dungeon_template(story_id, current_dungeon_id)
        return (
            jsonify(
                {
                    "error": "already_in_dungeon",
                    "message": f"您已在副本【{current_template.get('name', current_dungeon_id)}】中，請先回歸主神空間",
                }
            ),
            400,
        )

    try:
        app_module.initialize_dungeon_progress(story_id, branch_id, dungeon_id)
    except Exception as exc:
        log.exception("Failed to initialize dungeon progress")
        return jsonify({"error": str(exc)}), 500

    old_state = dict(state)
    state["current_phase"] = "傳送中"
    state["current_status"] = f"準備進入【{template['name']}】副本"
    state["current_dungeon"] = template["name"]
    app_module._save_character_state(story_id, branch_id, state)
    app_module.handle_dungeon_return_transition(
        story_id,
        branch_id,
        old_state,
        state,
        mode="enter",
    )

    try:
        from story_core.world_timer import advance_dungeon_enter

        advance_dungeon_enter(story_id, branch_id, template["name"])
    except ImportError:
        pass

    return jsonify({"success": True, "dungeon": template})


@misc_bp.route("/api/dungeon/progress", methods=["GET"])
def api_dungeon_progress():
    app_module = _app()
    story_id = request.args.get("story_id", app_module._active_story_id())
    branch_id = request.args.get("branch_id") or _active_branch_id(story_id)

    progress = app_module._load_dungeon_progress(story_id, branch_id)
    if not progress or not progress.get("current_dungeon"):
        return jsonify({"in_dungeon": False})

    current = progress["current_dungeon"]
    template = app_module._load_dungeon_template(story_id, current["dungeon_id"])
    if not template:
        return jsonify({"error": "Template not found"}), 500

    completed_nodes = set(current.get("completed_nodes", []))
    nodes_response = []
    next_shown = False
    for node in template["mainline"]["nodes"]:
        if node["id"] in completed_nodes:
            nodes_response.append(
                {"id": node["id"], "title": node["title"], "hint": "已完成", "status": "completed"}
            )
        elif not next_shown:
            is_current = len(nodes_response) == len(completed_nodes)
            nodes_response.append(
                {
                    "id": node["id"],
                    "title": node["title"],
                    "hint": node.get("hint", ""),
                    "status": "active" if is_current else "locked",
                }
            )
            if is_current:
                next_shown = True

    discovered = set(current.get("discovered_areas", []))
    explored = current.get("explored_areas", {})
    areas_response = []
    for area in template.get("areas", []):
        if area["id"] in discovered:
            areas_response.append(
                {
                    "id": area["id"],
                    "name": area["name"],
                    "type": area["type"],
                    "status": "explored" if explored.get(area["id"], 0) > 0 else "discovered",
                    "exploration": explored.get(area["id"], 0),
                }
            )

    return jsonify(
        {
            "in_dungeon": True,
            "dungeon_id": current["dungeon_id"],
            "dungeon_name": template["name"],
            "difficulty": template["difficulty"],
            "mainline_progress": current["mainline_progress"],
            "exploration_progress": current["exploration_progress"],
            "can_exit": current["mainline_progress"] >= 60,
            "mainline_nodes": nodes_response,
            "map_areas": areas_response,
            "metrics": {
                "explored_areas": len(discovered),
                "total_areas": len(template.get("areas", [])),
                "completed_nodes": len(completed_nodes),
                "total_nodes": len(template["mainline"]["nodes"]),
            },
        }
    )


@misc_bp.route("/api/dungeon/return", methods=["POST"])
def api_dungeon_return():
    app_module = _app()
    story_id = request.json.get("story_id", app_module._active_story_id())
    branch_id = request.json.get("branch_id") or _active_branch_id(story_id)

    progress = app_module._load_dungeon_progress(story_id, branch_id)
    if not progress or not progress.get("current_dungeon"):
        return jsonify({"error": "not_in_dungeon", "message": "當前不在副本中"}), 400

    current = progress["current_dungeon"]
    mainline_pct = current["mainline_progress"]

    if mainline_pct < 60:
        return (
            jsonify(
                {
                    "error": "incomplete_mainline",
                    "message": f"主線進度僅 {mainline_pct}%，需達 60% 才能提前回歸（100% 可正常回歸）",
                    "current_progress": mainline_pct,
                }
            ),
            400,
        )

    is_early_exit = mainline_pct < 100

    template = app_module._load_dungeon_template(story_id, current["dungeon_id"])
    if not template:
        return jsonify({"error": "Template not found"}), 500

    rules = template["progression_rules"]
    base = rules["base_reward"]
    mainline_bonus = base * (rules["mainline_multiplier"] - 1) * (mainline_pct / 100)
    exploration_bonus = (
        base * (rules["exploration_multiplier"] - 1) * (current["exploration_progress"] / 100)
    )
    total_reward = int(base + mainline_bonus + exploration_bonus)

    early_penalty = 1.0
    if is_early_exit:
        early_penalty = 0.5
        total_reward = int(total_reward * early_penalty)

    state = app_module._load_character_state(story_id, branch_id)
    old_state = dict(state)
    player_rank = state.get("等級", "E")
    scaling = rules.get("difficulty_scaling", {}).get(player_rank, 1.0)
    total_reward = int(total_reward * scaling)

    exit_reason = "early" if is_early_exit else "normal"
    app_module.archive_current_dungeon(story_id, branch_id, exit_reason=exit_reason)

    state["current_phase"] = "主神空間"
    state["current_status"] = f"副本結束，回歸主神空間。獲得獎勵點數 {total_reward}"
    state["current_dungeon"] = ""
    state["reward_points"] = state.get("reward_points", 0) + total_reward
    app_module._save_character_state(story_id, branch_id, state)
    app_module.handle_dungeon_return_transition(
        story_id,
        branch_id,
        old_state,
        state,
        mode="exit",
    )

    from story_core.state_cleanup import run_state_cleanup_async

    run_state_cleanup_async(story_id, branch_id, force=True)

    try:
        from story_core.world_timer import advance_dungeon_exit

        advance_dungeon_exit(story_id, branch_id)
    except ImportError:
        pass

    return jsonify(
        {
            "success": True,
            "reward_points": total_reward,
            "scaling": scaling,
            "exit_reason": exit_reason,
            "early_penalty": early_penalty,
            "message": (
                f"提前回歸（主線 {mainline_pct}%），獎勵打折 50%。獲得 {total_reward} 點"
                if is_early_exit
                else f"副本完成！獲得獎勵點數 {total_reward}"
            ),
        }
    )
