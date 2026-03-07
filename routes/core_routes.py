from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time

from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context


log = logging.getLogger("rpg")
core_bp = Blueprint("core", __name__)


def _app():
    import app as app_module

    return app_module


def _sse_event(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@core_bp.route("/")
def index():
    app_module = _app()
    try:
        mtimes = ""
        for filename in ("app.js", "style.css"):
            path = os.path.join(app_module.app.static_folder, filename)
            mtimes += str(int(os.path.getmtime(path)))
        cache_v = hashlib.md5(mtimes.encode()).hexdigest()[:8]
    except OSError:
        cache_v = "1"
    return render_template("index.html", v=cache_v)


@core_bp.route("/api/init", methods=["POST"])
def api_init():
    app_module = _app()
    app_module._ensure_data_dir()
    app_module._migrate_to_stories()

    story_id = app_module._active_story_id()

    app_module._migrate_design_files(story_id)

    parsed_path = app_module._story_parsed_path(story_id)
    if not os.path.exists(parsed_path):
        if os.path.exists(app_module.CONVERSATION_PATH):
            app_module.save_parsed()
            if os.path.exists(app_module.LEGACY_PARSED_PATH):
                shutil.copy2(app_module.LEGACY_PARSED_PATH, parsed_path)
        else:
            app_module._save_json(parsed_path, [])
    original = app_module._load_json(parsed_path, [])

    app_module._migrate_to_timeline_tree(story_id)
    app_module._migrate_branch_files(story_id)
    app_module._migrate_schema_abilities(story_id)

    tree = app_module._load_tree(story_id)
    active_branch = tree.get("active_branch_id", "main")
    app_module._load_character_state(story_id, active_branch)

    main_messages_path = app_module._story_messages_path(story_id, "main")
    if not os.path.exists(main_messages_path):
        app_module._save_branch_messages(story_id, "main", [])

    registry = app_module._load_stories_registry()
    story_meta = registry.get("stories", {}).get(story_id, {})
    character_schema = app_module._load_character_schema(story_id)

    return jsonify(
        {
            "ok": True,
            "original_count": len(original),
            "active_branch_id": active_branch,
            "active_story_id": story_id,
            "story_name": story_meta.get("name", story_id),
            "character_schema": character_schema,
        }
    )


@core_bp.route("/api/messages")
def api_messages():
    app_module = _app()
    story_id = app_module._active_story_id()
    branch_id = request.args.get("branch_id", "main")
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 99999, type=int)

    timeline = app_module.get_full_timeline(story_id, branch_id)
    original = app_module._load_json(app_module._story_parsed_path(story_id), [])
    original_count = len(original)

    tree = app_module._load_tree(story_id)
    branch_delta = app_module._load_branch_messages(story_id, branch_id)
    delta_indices = {message.get("index") for message in branch_delta}

    for message in timeline:
        index = message.get("index", 0)
        if index < original_count:
            message["inherited"] = branch_id != "main"
        else:
            message["inherited"] = index not in delta_indices

    total = len(timeline)
    after_index = request.args.get("after_index", None, type=int)
    tail = request.args.get("tail", None, type=int)
    if tail is not None:
        page = timeline[max(0, len(timeline) - tail) :]
    elif after_index is not None:
        page = [message for message in timeline if message.get("index", 0) > after_index]
    else:
        page = timeline[offset : offset + limit]
    fork_points = app_module._get_fork_points(story_id, branch_id)
    sibling_groups = app_module._get_sibling_groups(story_id, branch_id)

    result = {
        "messages": page,
        "total": total,
        "offset": offset,
        "original_count": original_count,
        "fork_points": fork_points,
        "sibling_groups": sibling_groups,
        "branch_id": branch_id,
        "world_day": app_module.get_world_day(story_id, branch_id),
        "dice_modifier": app_module.get_dice_modifier(app_module._story_dir(story_id), branch_id),
    }

    if branch_id.startswith("auto_"):
        state_path = os.path.join(app_module._branch_dir(story_id, branch_id), "auto_play_state.json")
        auto_state = app_module._load_json(state_path, None)
        if not isinstance(auto_state, dict):
            result["live_status"] = "unknown"
        elif (
            auto_state.get("status") == "finished"
            or auto_state.get("death_detected")
            or auto_state.get("consecutive_errors", 0) >= 3
        ):
            result["live_status"] = "finished"
        else:
            result["live_status"] = "running"
        result["auto_play_state"] = auto_state
        result["summary_count"] = len(app_module.get_summaries(story_id, branch_id))

    branch_meta = tree.get("branches", {}).get(branch_id, {})
    has_active_child = any(
        branch.get("parent_branch_id") == branch_id
        and not branch.get("deleted")
        and not branch.get("merged")
        and not branch.get("pruned")
        for branch in tree.get("branches", {}).values()
    )
    if (
        branch_id != "main"
        and not branch_id.startswith("auto_")
        and not branch_meta.get("blank")
        and not branch_meta.get("deleted")
        and not branch_meta.get("merged")
        and not branch_meta.get("pruned")
        and not has_active_child
        and any(message.get("role") == "user" for message in branch_delta)
        and not any(message.get("role") == "gm" for message in branch_delta)
    ):
        parent_id = branch_meta.get("parent_branch_id", "main")
        parent_meta = tree.get("branches", {}).get(parent_id, {})
        result["incomplete"] = {
            "parent_branch_id": parent_id,
            "parent_branch_name": parent_meta.get(
                "name", "主時間線" if parent_id == "main" else parent_id
            ),
        }

    return jsonify(result)


@core_bp.route("/api/send", methods=["POST"])
def api_send():
    app_module = _app()
    t_start = time.time()
    body = request.get_json(force=True)
    user_text = body.get("message", "").strip()
    branch_id = body.get("branch_id", "main")
    if not user_text:
        return jsonify({"ok": False, "error": "empty message"}), 400

    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    branch = branches.get(branch_id)
    if not branch:
        return jsonify({"ok": False, "error": "branch not found"}), 404

    if app_module._clear_loaded_save_preview(tree):
        app_module._save_tree(story_id, tree)

    log.info("/api/send START  msg=%s branch=%s", user_text[:30], branch_id)

    t0 = time.time()
    full_timeline = app_module.get_full_timeline(story_id, branch_id)
    next_msg_index = app_module._next_timeline_index(story_id, branch_id, timeline=full_timeline)

    player_msg = {"role": "user", "content": user_text, "index": next_msg_index}
    app_module._upsert_branch_message(story_id, branch_id, player_msg)
    full_timeline.append(player_msg)
    log.info("  save_user_msg: %.0fms", (time.time() - t0) * 1000)

    story_dir = app_module._story_dir(story_id)
    dice_cmd_result = (
        app_module.apply_dice_command(story_dir, branch_id, user_text)
        if app_module.is_gm_command(user_text)
        else None
    )
    if dice_cmd_result:
        log.info("  /gm dice: %s → %s", dice_cmd_result["old"], dice_cmd_result["new"])

    t0 = time.time()
    state = app_module._load_character_state(story_id, branch_id)
    npcs = app_module._load_npcs(story_id, branch_id)
    state_text = app_module._build_core_state_text(story_id, state)
    recap_text = app_module.get_recap_text(story_id, branch_id)
    system_prompt = app_module._build_story_system_prompt(
        story_id,
        state_text,
        branch_id=branch_id,
        narrative_recap=recap_text,
        npcs=npcs,
        state_dict=state,
    )
    log.info("  build_prompt: %.0fms", (time.time() - t0) * 1000)

    recent = app_module._sanitize_recent_messages(
        full_timeline[-app_module.RECENT_MESSAGE_COUNT :],
        strip_fate=not app_module.get_fate_mode(app_module._story_dir(story_id), branch_id),
    )

    t0 = time.time()
    turn_count = sum(1 for message in full_timeline if message.get("role") == "user")
    augmented_text, dice_result = app_module._build_augmented_message(
        story_id,
        branch_id,
        user_text,
        state,
        npcs=npcs,
        recent_messages=recent,
        turn_count=turn_count,
        current_index=player_msg["index"],
    )
    if dice_result:
        player_msg["dice"] = dice_result
        app_module._upsert_branch_message(story_id, branch_id, player_msg)
    log.info("  context_search: %.0fms", (time.time() - t0) * 1000)

    gm_msg_index = next_msg_index + 1
    app_module._trace_llm(
        stage="gm_request",
        story_id=story_id,
        branch_id=branch_id,
        message_index=gm_msg_index,
        source="/api/send",
        payload={
            "user_text": user_text,
            "augmented_text": augmented_text,
            "system_prompt": system_prompt,
            "recent": recent,
            "dice_result": dice_result,
        },
        tags={"mode": "sync"},
    )
    t0 = time.time()
    gm_response, _ = app_module.call_claude_gm(
        augmented_text, system_prompt, recent, session_id=None
    )
    gm_elapsed = time.time() - t0
    log.info("  claude_call: %.1fs", gm_elapsed)
    app_module._log_llm_usage(story_id, "gm", gm_elapsed, branch_id=branch_id)
    app_module._trace_llm(
        stage="gm_response_raw",
        story_id=story_id,
        branch_id=branch_id,
        message_index=gm_msg_index,
        source="/api/send",
        payload={"response": gm_response, "usage": app_module.get_last_usage()},
        tags={"mode": "sync"},
    )

    t0 = time.time()
    send_turn_count = sum(1 for message in full_timeline if message.get("role") == "user")
    gm_response, image_info, snapshots = app_module._process_gm_response(
        gm_response, story_id, branch_id, gm_msg_index, turn_count=send_turn_count
    )
    log.info("  parse_tags: %.0fms", (time.time() - t0) * 1000)

    t0 = time.time()
    gm_msg = {"role": "gm", "content": gm_response, "index": gm_msg_index}
    if image_info:
        gm_msg["image"] = image_info
    gm_msg.update(snapshots)
    app_module._upsert_branch_message(story_id, branch_id, gm_msg)
    log.info("  save_gm_msg: %.0fms", (time.time() - t0) * 1000)

    turn_count = sum(1 for message in full_timeline if message.get("role") == "user")
    if app_module._load_npcs(story_id, branch_id) and app_module.should_run_evolution(
        story_id, branch_id, turn_count
    ):
        npc_text = app_module._build_npc_text(story_id, branch_id)
        recent_text = "\n".join(message.get("content", "")[:200] for message in full_timeline[-6:])
        app_module.run_npc_evolution_async(story_id, branch_id, turn_count, npc_text, recent_text)

    recap = app_module.load_recap(story_id, branch_id)
    if app_module.should_compact(recap, len(full_timeline) + 1):
        timeline_with_reply = list(full_timeline) + [gm_msg]
        app_module.compact_async(story_id, branch_id, timeline_with_reply)

    pruned = app_module._auto_prune_siblings(story_id, branch_id, gm_msg_index)

    log.info("/api/send DONE   total=%.1fs", time.time() - t_start)
    return jsonify({"ok": True, "player": player_msg, "gm": gm_msg, "pruned_branches": pruned})


@core_bp.route("/api/send/stream", methods=["POST"])
def api_send_stream():
    app_module = _app()
    body = request.get_json(force=True)
    user_text = body.get("message", "").strip()
    branch_id = body.get("branch_id", "main")
    if not user_text:
        return Response(
            _sse_event({"type": "error", "message": "empty message"}),
            mimetype="text/event-stream",
        )

    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    branch = branches.get(branch_id)
    if not branch:
        return Response(
            _sse_event({"type": "error", "message": "branch not found"}),
            mimetype="text/event-stream",
        )

    if app_module._clear_loaded_save_preview(tree):
        app_module._save_tree(story_id, tree)

    log.info("/api/send/stream START  msg=%s branch=%s", user_text[:30], branch_id)

    full_timeline = app_module.get_full_timeline(story_id, branch_id)
    next_msg_index = app_module._next_timeline_index(story_id, branch_id, timeline=full_timeline)

    player_msg = {"role": "user", "content": user_text, "index": next_msg_index}
    app_module._upsert_branch_message(story_id, branch_id, player_msg)
    full_timeline.append(player_msg)

    story_dir = app_module._story_dir(story_id)
    dice_cmd_result = (
        app_module.apply_dice_command(story_dir, branch_id, user_text)
        if app_module.is_gm_command(user_text)
        else None
    )
    if dice_cmd_result:
        log.info("  /gm dice: %s → %s", dice_cmd_result["old"], dice_cmd_result["new"])

    state = app_module._load_character_state(story_id, branch_id)
    npcs = app_module._load_npcs(story_id, branch_id)
    state_text = app_module._build_core_state_text(story_id, state)
    recap_text = app_module.get_recap_text(story_id, branch_id)
    system_prompt = app_module._build_story_system_prompt(
        story_id,
        state_text,
        branch_id=branch_id,
        narrative_recap=recap_text,
        npcs=npcs,
        state_dict=state,
    )

    recent = app_module._sanitize_recent_messages(
        full_timeline[-app_module.RECENT_MESSAGE_COUNT :],
        strip_fate=not app_module.get_fate_mode(app_module._story_dir(story_id), branch_id),
    )
    turn_count = sum(1 for message in full_timeline if message.get("role") == "user")
    augmented_text, dice_result = app_module._build_augmented_message(
        story_id,
        branch_id,
        user_text,
        state,
        npcs=npcs,
        recent_messages=recent,
        turn_count=turn_count,
        current_index=player_msg["index"],
    )
    if dice_result:
        player_msg["dice"] = dice_result
        app_module._upsert_branch_message(story_id, branch_id, player_msg)

    gm_msg_index = next_msg_index + 1
    app_module._trace_llm(
        stage="gm_request",
        story_id=story_id,
        branch_id=branch_id,
        message_index=gm_msg_index,
        source="/api/send/stream",
        payload={
            "user_text": user_text,
            "augmented_text": augmented_text,
            "system_prompt": system_prompt,
            "recent": recent,
            "dice_result": dice_result,
        },
        tags={"mode": "stream"},
    )

    def generate():
        t_start = time.time()
        if dice_result:
            yield _sse_event({"type": "dice", "dice": dice_result})
        try:
            for event_type, payload in app_module.call_claude_gm_stream(
                augmented_text, system_prompt, recent, session_id=None
            ):
                if event_type == "text":
                    yield _sse_event({"type": "text", "chunk": payload})
                elif event_type == "error":
                    yield _sse_event({"type": "error", "message": payload})
                    return
                elif event_type == "done":
                    gm_response = payload["response"]
                    app_module._log_llm_usage(
                        story_id,
                        "gm_stream",
                        time.time() - t_start,
                        branch_id=branch_id,
                        usage=payload.get("usage"),
                    )
                    app_module._trace_llm(
                        stage="gm_response_raw",
                        story_id=story_id,
                        branch_id=branch_id,
                        message_index=gm_msg_index,
                        source="/api/send/stream",
                        payload={"response": gm_response, "usage": payload.get("usage")},
                        tags={"mode": "stream"},
                    )

                    gm_response, image_info, snapshots = app_module._process_gm_response(
                        gm_response, story_id, branch_id, gm_msg_index
                    )

                    gm_msg = {"role": "gm", "content": gm_response, "index": gm_msg_index}
                    if image_info:
                        gm_msg["image"] = image_info
                    gm_msg.update(snapshots)
                    app_module._upsert_branch_message(story_id, branch_id, gm_msg)

                    turn_count = sum(1 for message in full_timeline if message.get("role") == "user")
                    if app_module._load_npcs(story_id, branch_id) and app_module.should_run_evolution(
                        story_id, branch_id, turn_count
                    ):
                        npc_text = app_module._build_npc_text(story_id, branch_id)
                        recent_text = "\n".join(
                            message.get("content", "")[:200] for message in full_timeline[-6:]
                        )
                        app_module.run_npc_evolution_async(
                            story_id, branch_id, turn_count, npc_text, recent_text
                        )

                    recap = app_module.load_recap(story_id, branch_id)
                    if app_module.should_compact(recap, len(full_timeline) + 1):
                        timeline_with_reply = list(full_timeline) + [gm_msg]
                        app_module.compact_async(story_id, branch_id, timeline_with_reply)

                    pruned = app_module._auto_prune_siblings(story_id, branch_id, gm_msg_index)

                    tree_now = app_module._load_tree(story_id)
                    tree_now["last_played_branch_id"] = branch_id
                    app_module._save_tree(story_id, tree_now)
                    log.info("/api/send/stream DONE total=%.1fs", time.time() - t_start)
                    yield _sse_event(
                        {
                            "type": "done",
                            "user_msg": player_msg,
                            "gm_msg": gm_msg,
                            "branch": tree_now["branches"][branch_id],
                            "pruned_branches": pruned,
                        }
                    )
        except Exception as exc:
            import traceback

            log.info("/api/send/stream EXCEPTION %s\n%s", exc, traceback.format_exc())
            yield _sse_event({"type": "error", "message": str(exc)})

    return Response(stream_with_context(generate()), mimetype="text/event-stream")
