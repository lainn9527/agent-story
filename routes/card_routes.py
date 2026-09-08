"""Routes for Card Mode: card wall, chat interface, and API endpoints."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading

from flask import Blueprint, Response, jsonify, redirect, render_template, request, stream_with_context

from story_core.card_mode import clock
from story_core.card_mode.card import (
    STORY_DESIGN_DIR,
    card_path,
    init_card_story,
    load_card,
    validate_card,
)
from story_core.card_mode.pipeline import run_card_turn
from story_core import compaction
from story_core.gm_v2 import relationship as _rel
from story_core.story_io import (
    _branch_dir,
    _load_branch_messages,
    _load_json,
    _load_stories_registry,
    _save_stories_registry,
    _save_json,
    _story_character_state_path,
    _story_messages_path,
    _story_npcs_path,
)

log = logging.getLogger("rpg")
card_bp = Blueprint("card", __name__)


def _sse_event(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@card_bp.route("/card")
def card_index_page():
    """Card wall page."""
    return render_template("card_index.html")


@card_bp.route("/card/<slug>")
def card_chat_page(slug: str):
    """Card chat conversation page: redirect to unified app with active card story."""
    # Auto-initialize if card.json exists in story_design
    design_card_path = card_path(slug)
    if os.path.exists(design_card_path):
        try:
            b_dir = _branch_dir(slug, "main")
            if not os.path.exists(os.path.join(b_dir, "messages.json")):
                card_data = load_card(slug)
                init_card_story(card_data)
        except Exception:
            log.exception("card_routes: auto-init failed for %s", slug)
    elif slug not in _load_stories_registry().get("stories", {}):
        return f"Card {slug} not found", 404

    try:
        reg = _load_stories_registry()
        reg["active_story_id"] = slug
        _save_stories_registry(reg)
    except Exception:
        pass

    return redirect(f"/?story={slug}")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@card_bp.route("/api/card/list")
def api_card_list():
    """Return all stories registered with mode == 'card'."""
    registry = _load_stories_registry()
    stories = registry.get("stories", {})
    card_list = []

    for sid, meta in stories.items():
        if meta.get("mode") == "card":
            card_list.append(meta)

    # Discover any uninitialized card.json in story_design/
    if os.path.exists(STORY_DESIGN_DIR):
        existing_ids = {c["id"] for c in card_list}
        for entry in os.listdir(STORY_DESIGN_DIR):
            if entry in existing_ids:
                continue
            c_path = os.path.join(STORY_DESIGN_DIR, entry, "card.json")
            if os.path.isfile(c_path):
                try:
                    c_data = _load_json(c_path, {})
                    if not validate_card(c_data):
                        init_card_story(c_data)
                        card_list.append({
                            "id": entry,
                            "name": c_data.get("display_name", entry),
                            "mode": "card",
                            "tagline": c_data.get("tagline", ""),
                            "pitch": c_data.get("pitch", ""),
                        })
                except Exception:
                    pass

    return jsonify({"ok": True, "cards": card_list})


@card_bp.route("/api/card/<slug>/state")
def api_card_state(slug: str):
    """Return card persona, current relationship, memory, clock, inventory, and messages."""
    try:
        card = load_card(slug)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 404

    state = _load_json(_story_character_state_path(slug, "main"), {"inventory": {}})
    npcs = _load_json(_story_npcs_path(slug, "main"), [])
    card_npc = npcs[0] if npcs else {}
    clock_info = clock.load_clock(slug, "main")
    cur_dt = clock.now(slug, "main")

    after = request.args.get("after", type=int)
    all_msgs = _load_branch_messages(slug, "main")
    if after is not None:
        filtered_msgs = [m for m in all_msgs if m.get("index", 0) > after]
    else:
        filtered_msgs = all_msgs

    clean_msgs = []
    for m in filtered_msgs:
        clean_msgs.append({
            "role": m.get("role"),
            "content": m.get("content"),
            "index": m.get("index"),
        })

    return jsonify({
        "ok": True,
        "card": {
            "id": slug,
            "display_name": card.get("display_name", slug),
            "tagline": card.get("tagline", ""),
            "pitch": card.get("pitch", ""),
            "player": card.get("player", {}),
            "setting": card.get("setting", {}),
            "persona": card.get("persona", {}),
        },
        "relationship": card_npc.get("relationship", {}),
        "relationship_display": _rel.display(card_npc.get("relationship")),
        "relationship_tiers": _rel.TIERS,
        "memories": card_npc.get("memory", []),
        "inventory": state.get("inventory", {}),
        "clock": {
            "time_str": cur_dt.strftime("%Y/%m/%d %H:%M"),
            "weather": clock_info.get("last_weather", "陰"),
            "elapsed_minutes": clock_info.get("elapsed_minutes", 0),
        },
        "messages": clean_msgs,
    })


@card_bp.route("/api/card/<slug>/send/stream", methods=["POST"])
def api_card_send_stream(slug: str):
    """SSE streaming endpoint for player dialogue."""
    data = request.get_json(force=True, silent=True) or {}
    user_msg = data.get("message", "").strip()
    if not user_msg:
        return jsonify({"ok": False, "error": "message is required"}), 400

    is_async = request.args.get("sync") != "1"

    def generate():
        q: queue.Queue = queue.Queue()

        def emit_chunk(chunk: str):
            q.put(("text", chunk))

        def worker():
            try:
                res = run_card_turn(
                    story_id=slug,
                    branch_id="main",
                    user_message=user_msg,
                    emit_chunk=emit_chunk,
                    async_recorder=is_async,
                )
                q.put(("done", res))
            except Exception as e:
                log.exception("api_card_send_stream: worker error")
                q.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

        while True:
            ev, payload = q.get()
            if ev == "text":
                yield _sse_event({"type": "text", "chunk": payload})
            elif ev == "done":
                # Include updated state metadata in done event
                cur_dt = clock.now(slug, "main")
                clock_info = clock.load_clock(slug, "main")
                npcs = _load_json(_story_npcs_path(slug, "main"), [])
                card_npc = npcs[0] if npcs else {}
                state = _load_json(_story_character_state_path(slug, "main"), {})

                done_payload = {
                    "type": "done",
                    "result": payload,
                    "clock": {
                        "time_str": cur_dt.strftime("%Y/%m/%d %H:%M"),
                        "weather": clock_info.get("last_weather", "陰"),
                    },
                    "relationship": card_npc.get("relationship", {}),
                    "relationship_display": _rel.display(card_npc.get("relationship")),
                    "memories": card_npc.get("memory", []),
                    "inventory": state.get("inventory", {}),
                }
                yield _sse_event(done_payload)
                break
            elif ev == "error":
                yield _sse_event({"type": "error", "message": payload})
                break

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@card_bp.route("/api/card/<slug>/regenerate", methods=["POST"])
def api_card_regenerate(slug: str):
    """Regenerate the last GM message using snapshot restoration."""
    data = request.get_json(force=True, silent=True) or {}
    want_stream = request.headers.get("Accept") == "text/event-stream" or data.get("stream") is True

    msgs = _load_branch_messages(slug, "main")
    if not msgs:
        return jsonify({"ok": False, "error": "No messages found"}), 400

    last_msg = msgs[-1]
    if last_msg.get("role") != "gm" or last_msg.get("index", 0) <= 0:
        return jsonify({"ok": False, "error": "No GM reply to regenerate (opening message cannot be regenerated)"}), 400

    # Restore state from snapshot before this GM reply
    pre_state = last_msg.get("pre_state_snapshot") or last_msg.get("state_snapshot")
    pre_npcs = last_msg.get("pre_npcs_snapshot") or last_msg.get("npcs_snapshot")
    pre_clock = last_msg.get("pre_clock_snapshot") or last_msg.get("clock_snapshot")

    if pre_state:
        _save_json(_story_character_state_path(slug, "main"), pre_state)
    if pre_npcs:
        _save_json(_story_npcs_path(slug, "main"), pre_npcs)
    if pre_clock:
        clock.save_clock(slug, "main", pre_clock)

    # Pop the last GM message
    msgs.pop()
    _save_json(_story_messages_path(slug, "main"), msgs)

    if want_stream:
        def generate():
            q: queue.Queue = queue.Queue()

            def emit_chunk(chunk: str):
                q.put(("text", chunk))

            def worker():
                try:
                    res = run_card_turn(
                        story_id=slug,
                        branch_id="main",
                        user_message="",
                        skip_user_message=True,
                        emit_chunk=emit_chunk,
                    )
                    q.put(("done", res))
                except Exception as e:
                    log.exception("api_card_regenerate: worker error")
                    q.put(("error", str(e)))

            threading.Thread(target=worker, daemon=True).start()

            while True:
                ev, payload = q.get()
                if ev == "text":
                    yield _sse_event({"type": "text", "chunk": payload})
                elif ev == "done":
                    cur_dt = clock.now(slug, "main")
                    clock_info = clock.load_clock(slug, "main")
                    npcs = _load_json(_story_npcs_path(slug, "main"), [])
                    card_npc = npcs[0] if npcs else {}
                    state = _load_json(_story_character_state_path(slug, "main"), {})

                    yield _sse_event({
                        "type": "done",
                        "result": payload,
                        "clock": {
                            "time_str": cur_dt.strftime("%Y/%m/%d %H:%M"),
                            "weather": clock_info.get("last_weather", "陰"),
                        },
                        "relationship": card_npc.get("relationship", {}),
                        "relationship_display": _rel.display(card_npc.get("relationship")),
                        "memories": card_npc.get("memory", []),
                        "inventory": state.get("inventory", {}),
                    })
                    break
                elif ev == "error":
                    yield _sse_event({"type": "error", "message": payload})
                    break

        return Response(stream_with_context(generate()), mimetype="text/event-stream")

    # Non-stream
    res = run_card_turn(story_id=slug, branch_id="main", user_message="", skip_user_message=True)
    return jsonify(res)


@card_bp.route("/api/card/<slug>/rewind", methods=["POST"])
def api_card_rewind(slug: str):
    data = request.get_json(force=True, silent=True) or {}
    to_index = data.get("to_index")
    if to_index is None:
        to_index = data.get("message_index")
    if to_index is None:
        return jsonify({"ok": False, "error": "to_index is required"}), 400

    try:
        to_index = int(to_index)
    except ValueError:
        return jsonify({"ok": False, "error": "to_index must be an integer"}), 400

    msgs = _load_branch_messages(slug, "main")
    target_msg = None
    for m in msgs:
        if m.get("index") == to_index:
            target_msg = m
            break

    if target_msg is None:
        return jsonify({"ok": False, "error": f"Message with index {to_index} not found"}), 404

    # Restore snapshot
    state_snap = target_msg.get("state_snapshot")
    npcs_snap = target_msg.get("npcs_snapshot")
    clock_snap = target_msg.get("clock_snapshot")

    if state_snap:
        _save_json(_story_character_state_path(slug, "main"), state_snap)
    if npcs_snap:
        _save_json(_story_npcs_path(slug, "main"), npcs_snap)
    if clock_snap:
        clock.save_clock(slug, "main", clock_snap)

    # Truncate messages after to_index
    kept = [m for m in msgs if m.get("index", 0) <= to_index]
    _save_json(_story_messages_path(slug, "main"), kept)

    log.info("card_routes: rewound %s to index %d (kept %d msgs)", slug, to_index, len(kept))
    return jsonify({"ok": True, "to_index": to_index, "messages_count": len(kept)})


@card_bp.route("/api/card/<slug>/reset", methods=["POST"])
def api_card_reset(slug: str):
    """Reset story to initial opening state (clears dialogue, memory, restores initial clock & relationship)."""
    try:
        card = load_card(slug)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 404

    init_card_story(card, overwrite=True)

    # Also reset recap file if present
    recap_path = os.path.join(_branch_dir(slug, "main"), "conversation_recap.json")
    if os.path.exists(recap_path):
        try:
            os.remove(recap_path)
        except Exception:
            pass

    log.info("card_routes: reset %s to opening state", slug)
    return jsonify({"ok": True})
