from __future__ import annotations

from datetime import datetime, timezone
from flask import Blueprint, Response, jsonify, request, stream_with_context
import logging
import math
import time


log = logging.getLogger("rpg")
debug_bp = Blueprint("debug", __name__)


def _app():
    import app as app_module

    return app_module


@debug_bp.route("/api/debug/session")
def api_debug_session():
    app_module = _app()
    story_id = app_module._active_story_id()
    branch_id = request.args.get("branch_id", "main")
    tree = app_module._load_tree(story_id)
    if branch_id not in tree.get("branches", {}):
        return jsonify({"ok": False, "error": "branch not found"}), 404

    debug_unit_id = app_module._resolve_debug_unit_id(story_id, branch_id)
    messages = app_module._load_debug_chat(story_id, debug_unit_id)
    return jsonify(
        {
            "ok": True,
            "debug_unit_id": debug_unit_id,
            "target_branch_id": branch_id,
            "messages": messages,
            "state_snapshot": app_module._load_character_state(story_id, branch_id),
            "npcs_snapshot": app_module._load_npcs(story_id, branch_id, include_archived=True),
            "world_day": app_module.get_world_day(story_id, branch_id),
            "dungeon_progress": app_module._load_dungeon_progress(story_id, branch_id) or {},
            "gm_plan": app_module._load_gm_plan(story_id, branch_id),
            "pending_directive": app_module._load_debug_directive(story_id, branch_id),
        }
    )


@debug_bp.route("/api/debug/chat/stream", methods=["POST"])
def api_debug_chat_stream():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "main")
    user_message = str(body.get("user_message", "")).strip()
    if not user_message:
        return Response(
            app_module._sse_event({"type": "error", "message": "user_message required"}),
            mimetype="text/event-stream",
        )
    if len(user_message) > app_module.DEBUG_CHAT_MAX_USER_CHARS:
        return Response(
            app_module._sse_event(
                {
                    "type": "error",
                    "message": f"user_message too long (>{app_module.DEBUG_CHAT_MAX_USER_CHARS})",
                }
            ),
            mimetype="text/event-stream",
        )

    tree = app_module._load_tree(story_id)
    if branch_id not in tree.get("branches", {}):
        return Response(
            app_module._sse_event({"type": "error", "message": "branch not found"}),
            mimetype="text/event-stream",
        )

    debug_unit_id = app_module._resolve_debug_unit_id(story_id, branch_id)
    existing_chat = app_module._load_debug_chat(story_id, debug_unit_id)
    prior = [
        {"role": message["role"], "content": message["content"]}
        for message in existing_chat[-app_module.DEBUG_CHAT_CONTEXT_COUNT :]
    ]
    app_module._append_debug_chat_message(story_id, debug_unit_id, "user", user_message)

    full_timeline = app_module.get_full_timeline(story_id, branch_id)
    recent = app_module._sanitize_recent_messages(
        full_timeline[-app_module.RECENT_MESSAGE_COUNT :],
        strip_fate=not app_module.get_fate_mode(app_module._story_dir(story_id), branch_id),
    )
    debug_system_prompt = app_module._build_debug_system_prompt(story_id, branch_id, recent)

    app_module._trace_llm(
        stage="debug_chat_request",
        story_id=story_id,
        branch_id=branch_id,
        source="/api/debug/chat/stream",
        payload={
            "debug_unit_id": debug_unit_id,
            "user_message": user_message,
            "system_prompt": debug_system_prompt,
            "recent_debug_messages": prior,
        },
        tags={"mode": "stream"},
    )

    def generate():
        t_start = time.time()
        try:
            for event_type, payload in app_module.call_claude_gm_stream(
                user_message, debug_system_prompt, prior, session_id=None
            ):
                if event_type == "text":
                    yield app_module._sse_event({"type": "text", "chunk": payload})
                elif event_type == "error":
                    yield app_module._sse_event({"type": "error", "message": payload})
                    return
                elif event_type == "done":
                    response_raw = payload.get("response", "")
                    app_module._log_llm_usage(
                        story_id,
                        "debug_chat",
                        time.time() - t_start,
                        branch_id=branch_id,
                        usage=payload.get("usage"),
                    )
                    app_module._trace_llm(
                        stage="debug_chat_response_raw",
                        story_id=story_id,
                        branch_id=branch_id,
                        source="/api/debug/chat/stream",
                        payload={"response": response_raw, "usage": payload.get("usage")},
                        tags={"mode": "stream"},
                    )

                    display_text, proposals = app_module._extract_debug_action_tags(response_raw)
                    display_text, directives = app_module._extract_debug_directive_tags(display_text)
                    display_text = display_text.strip()
                    if response_raw:
                        app_module._append_debug_chat_message(
                            story_id, debug_unit_id, "assistant", response_raw
                        )
                    yield app_module._sse_event(
                        {
                            "type": "done",
                            "response": display_text,
                            "proposals": proposals,
                            "directives": directives,
                        }
                    )
        except Exception as exc:
            log.info("/api/debug/chat/stream EXCEPTION %s", exc)
            yield app_module._sse_event({"type": "error", "message": str(exc)})

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@debug_bp.route("/api/debug/apply", methods=["POST"])
def api_debug_apply():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "main")
    actions = body.get("actions", [])
    directives = body.get("directives", [])

    tree = app_module._load_tree(story_id)
    if branch_id not in tree.get("branches", {}):
        return jsonify({"ok": False, "error": "branch not found"}), 404
    if not isinstance(actions, list):
        return jsonify({"ok": False, "error": "actions must be a list"}), 400
    if not isinstance(directives, list):
        return jsonify({"ok": False, "error": "directives must be a list"}), 400
    if len(actions) > app_module.DEBUG_APPLY_MAX_ACTIONS:
        return (
            jsonify({"ok": False, "error": f"too many actions (max {app_module.DEBUG_APPLY_MAX_ACTIONS})"}),
            400,
        )
    if len(directives) > app_module.DEBUG_APPLY_MAX_DIRECTIVES:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"too many directives (max {app_module.DEBUG_APPLY_MAX_DIRECTIVES})",
                }
            ),
            400,
        )

    debug_unit_id = app_module._resolve_debug_unit_id(story_id, branch_id)
    backup = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "debug_unit_id": debug_unit_id,
        "target_branch_id": branch_id,
        "state_snapshot": app_module._load_character_state(story_id, branch_id),
        "npcs_snapshot": app_module._load_npcs(story_id, branch_id, include_archived=True),
        "world_day": app_module.get_world_day(story_id, branch_id),
        "dungeon_progress_snapshot": app_module._load_dungeon_progress(story_id, branch_id)
        or {
            "history": [],
            "current_dungeon": None,
            "total_dungeons_completed": 0,
        },
    }
    app_module._save_last_apply_backup(story_id, debug_unit_id, backup)

    results: list[dict] = []
    for raw_action in actions:
        if not isinstance(raw_action, dict):
            results.append({"type": "unknown", "ok": False, "error": "invalid action payload"})
            continue
        try:
            results.append(app_module._apply_debug_action(story_id, branch_id, raw_action))
        except Exception as exc:
            action_type = str(raw_action.get("type", "unknown"))
            results.append({"type": action_type, "ok": False, "error": str(exc)})

    directive_result = {"ok": True, "applied": 0}
    latest_directive = app_module._pick_latest_debug_directive(directives)
    if latest_directive:
        try:
            app_module._save_debug_directive(
                story_id,
                branch_id,
                {
                    **latest_directive,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "source": "debug_apply",
                    "debug_unit_id": debug_unit_id,
                },
            )
            directive_result["applied"] = 1
        except Exception as exc:
            directive_result = {"ok": False, "applied": 0, "error": str(exc)}

    audit_summary = app_module._build_debug_apply_audit_summary(
        results, directive_result.get("applied", 0)
    )
    try:
        app_module._append_debug_audit_message(story_id, branch_id, audit_summary)
    except Exception as exc:
        log.warning(
            "debug apply audit append failed: story=%s branch=%s error=%s",
            story_id,
            branch_id,
            exc,
        )

    return jsonify(
        {
            "ok": True,
            "results": results,
            "directive_result": directive_result,
            "audit_summary": audit_summary,
        }
    )


@debug_bp.route("/api/debug/undo", methods=["POST"])
def api_debug_undo():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "main")

    tree = app_module._load_tree(story_id)
    if branch_id not in tree.get("branches", {}):
        return jsonify({"ok": False, "error": "branch not found"}), 404

    debug_unit_id = app_module._resolve_debug_unit_id(story_id, branch_id)
    backup = app_module._load_last_apply_backup(story_id, debug_unit_id)
    if not backup:
        return jsonify({"ok": False, "error": "no backup available"}), 400
    if backup.get("target_branch_id") != branch_id:
        return jsonify({"ok": False, "error": "backup target mismatch"}), 400

    state_snapshot = backup.get("state_snapshot")
    npcs_snapshot = backup.get("npcs_snapshot")
    world_day = backup.get("world_day")
    dungeon_snapshot = backup.get("dungeon_progress_snapshot")

    if not isinstance(state_snapshot, dict):
        return jsonify({"ok": False, "error": "backup state invalid"}), 400
    if not isinstance(npcs_snapshot, list):
        return jsonify({"ok": False, "error": "backup npc snapshot invalid"}), 400
    if not isinstance(dungeon_snapshot, dict):
        return jsonify({"ok": False, "error": "backup dungeon snapshot invalid"}), 400
    try:
        restored_world_day = float(world_day)
    except (TypeError, ValueError):
        log.warning(
            "debug undo rejected: invalid backup world_day story=%s branch=%s debug_unit=%s value=%r",
            story_id,
            branch_id,
            debug_unit_id,
            world_day,
        )
        return jsonify({"ok": False, "error": "backup world_day invalid"}), 400
    if not math.isfinite(restored_world_day) or restored_world_day < 0:
        log.warning(
            "debug undo rejected: non-finite backup world_day story=%s branch=%s debug_unit=%s value=%r",
            story_id,
            branch_id,
            debug_unit_id,
            world_day,
        )
        return jsonify({"ok": False, "error": "backup world_day invalid"}), 400

    app_module._save_json(app_module._story_character_state_path(story_id, branch_id), state_snapshot)
    app_module._save_json(app_module._story_npcs_path(story_id, branch_id), npcs_snapshot)
    app_module.rebuild_state_db_from_json(story_id, branch_id, state=state_snapshot, npcs=npcs_snapshot)
    app_module.set_world_day(story_id, branch_id, restored_world_day)

    app_module._save_json(app_module._dungeon_progress_path(story_id, branch_id), dungeon_snapshot)
    app_module._clear_debug_directive(story_id, branch_id)
    app_module._clear_last_apply_backup(story_id, debug_unit_id)

    audit_summary = "已回滾 Debug 修正（還原至套用前）"
    try:
        app_module._append_debug_audit_message(story_id, branch_id, audit_summary)
    except Exception as exc:
        log.warning(
            "debug undo audit append failed: story=%s branch=%s error=%s",
            story_id,
            branch_id,
            exc,
        )
    return jsonify({"ok": True, "restored": True, "audit_summary": audit_summary})


@debug_bp.route("/api/debug/directive/clear", methods=["POST"])
def api_debug_directive_clear():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "main")

    app_module._clear_debug_directive(story_id, branch_id)
    return jsonify({"ok": True})


@debug_bp.route("/api/debug/clear", methods=["POST"])
def api_debug_clear():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "main")

    tree = app_module._load_tree(story_id)
    if branch_id not in tree.get("branches", {}):
        return jsonify({"ok": False, "error": "branch not found"}), 404

    debug_unit_id = app_module._resolve_debug_unit_id(story_id, branch_id)

    app_module._save_json(app_module._debug_chat_path(story_id, debug_unit_id), [])
    app_module._clear_debug_directive(story_id, branch_id)
    app_module._clear_last_apply_backup(story_id, debug_unit_id)

    return jsonify({"ok": True, "audit_summary": "已清除 Debug 對話與提案紀錄"})
