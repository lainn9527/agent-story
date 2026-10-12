"""Routes for Card Mode: card wall, chat interface, and API endpoints."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from functools import wraps

from flask import g, Blueprint, Response, jsonify, redirect, render_template, request, stream_with_context

from story_core.card_mode import clock, ensemble, recall, branches, styles, candidates, instructions, hooks
from story_core.card_mode.card import (
    STORY_DESIGN_DIR,
    card_path,
    init_card_story,
    load_card,
    validate_card,
)
from story_core.card_mode.pipeline import run_card_turn
from story_core.card_mode.prompt import RESPONSE_LENGTHS
from story_core.web_assets import chat_asset_version
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


@card_bp.before_request
def select_card_branch():
    slug = (request.view_args or {}).get("slug")
    if slug and request.path.startswith("/api/card/"):
        data = request.get_json(silent=True)
        requested = data.get("branch_id") if isinstance(data, dict) else request.args.get("branch_id")
        g.card_branch = branches.resolve(slug, requested)


@card_bp.errorhandler(branches.BranchError)
def card_branch_error(exc):
    return jsonify(ok=False, error=str(exc)), 409 if isinstance(exc, branches.Busy) else 400


@card_bp.after_request
def card_branch_metadata(response):
    if request.path.endswith('/state') and response.is_json and response.status_code == 200:
        slug = (request.view_args or {}).get('slug')
        if slug:
            payload = response.get_json()
            payload.update(branches.listing(slug), branch_id=g.card_branch,
                           exploration_materials=hooks.materials(load_card(slug)))
            response.set_data(json.dumps(payload, ensure_ascii=False))
    return response


@card_bp.route('/api/card/<slug>/branches', methods=['GET', 'POST'])
@card_bp.route('/api/card/<slug>/branches/<action>', methods=['POST'])
def api_card_branches(slug, action='fork'):
    if request.method == 'GET':
        return jsonify(ok=True, **branches.listing(slug))
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise branches.BranchError('Expected JSON object')
    try:
        return jsonify(branches.change(slug, action, body))
    except ensemble.Busy as exc:
        return jsonify(ok=False, error=str(exc)), 409


def single_mutation(fn):
    @wraps(fn)
    def wrapped(slug):
        with branches.lock(slug, g.card_branch):
            return fn(slug)
    return wrapped


@card_bp.route("/api/card/<slug>/ensemble", methods=["POST"])
def api_card_ensemble_create(slug):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error="Expected JSON object"), 400
    try:
        with branches.lock(slug, g.card_branch):
            branches.ensure_settled(_load_branch_messages(slug, g.card_branch), for_fork=True)
            target = ensemble.clone(slug, data.get("target_slug", ""), data.get("character"), data.get("name", ""), branch_id=g.card_branch)
        return jsonify(ok=True, slug=target, url="/card/"+target)
    except (ensemble.Busy, branches.Busy) as e:
        return jsonify(ok=False, error=str(e)), 409
    except (ValueError, FileNotFoundError, KeyError) as e:
        return jsonify(ok=False, error=str(e)), 400


@card_bp.route("/api/card/<slug>/focus", methods=["POST"])
@card_bp.route("/api/card/<slug>/add", methods=["POST"])
@card_bp.route("/api/card/<slug>/retry-recording", methods=["POST"])
def api_card_ensemble_only(slug):
    return jsonify(ok=False, error="請先建立多角色副本"), 400


@card_bp.before_request
def dispatch_ensemble():
    # Explicit opt-in through ensemble.json; legacy requests are untouched.
    args = request.view_args or {}
    slug = args.get("slug", "")
    if not ensemble.enabled(slug) or not request.path.startswith("/api/card/"):
        return None
    branch_id = g.card_branch
    action = request.path.rsplit("/", 1)[-1]
    streaming = request.path.endswith("/send/stream")
    if streaming: action = "send"
    try:
        if action == "state" and request.method == "GET":
            from story_core.card_mode import ensemble_recorder
            ensemble_recorder.start(slug, branch_id=branch_id)
            return jsonify(ensemble.public_state(slug, branch_id=branch_id))
        if action not in ("send", "regenerate", "rewind", "reset", "focus", "add", "retry-recording"):
            return None
        if request.method != "POST":
            return None
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("Expected JSON object")
        if action in ("send", "regenerate") and (streaming or body.get("stream") or request.headers.get("Accept")=="text/event-stream"):
            # Reject invalid/busy requests before opening SSE. The worker takes
            # the lock again authoritatively; a racing request gets an SSE error.
            with ensemble.lock(slug, branch_id=branch_id):
                doc = ensemble.load(slug, branch_id)
                if action == "regenerate":
                    last = doc['messages'][-1]
                    if last['index'] <= doc['baseline_index'] or 'ensemble_pre_snapshot' not in last:
                        raise ValueError('不能重新生成副本建立前的回合')
                    user = doc['messages'][-2]['content']
                else: user = body.get('message', '')
                ensemble.validate_turn(user, body.get('response_length'), body.get('recall_mode'), body.get('narrative_style'), body.get('plot_mode'), body.get('scene_mode'))
            events = queue.Queue()
            def work():
                try:
                    result = ensemble.mutate(slug, action, body, branch_id=branch_id,
                        emit_chunk=lambda chunk: events.put({"type":"text", "chunk":chunk}))
                    events.put({"type":"done", "result":result})
                except Exception as exc:
                    log.exception("ensemble stream failed for %s", slug)
                    message = str(exc) if isinstance(exc, (ValueError, ensemble.Busy)) else '生成失敗，回合未寫入；請重試。'
                    events.put({"type":"error", "message":message})
                finally:
                    events.put(None)
            threading.Thread(target=work, daemon=True).start()
            def stream():
                while True:
                    try: event = events.get(timeout=10)
                    except queue.Empty:
                        yield ': keepalive\n\n'
                        continue
                    if event is None: return
                    yield _sse_event(event)
            return Response(stream_with_context(stream()), mimetype="text/event-stream",
                            headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})
        result = ensemble.mutate(slug, action, body, branch_id=branch_id)
        return jsonify(result)
    except ensemble.Busy as e:
        return jsonify(ok=False, error=str(e)), 409
    except (ValueError, KeyError, TypeError) as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception:
        log.exception("ensemble request failed")
        return jsonify(ok=False, error="生成或記錄失敗，回合未寫入；請重試。"), 502


def _sse_event(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@card_bp.route("/card")
def card_index_page():
    """Card wall page."""
    return render_template("card_index.html", v=chat_asset_version())


@card_bp.route("/card/<slug>")
def card_chat_page(slug: str):
    """Card chat conversation page."""
    # Auto-initialize if card.json exists in story_design
    design_card_path = card_path(slug)
    if ensemble.enabled(slug):
        return render_template("card_chat.html", slug=slug, v=chat_asset_version(), recall_available=True)
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

    return render_template("card_chat.html", slug=slug, v=chat_asset_version(), recall_available=True)


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
    from story_core.card_mode import galgame
    branch_id = g.card_branch
    try:
        card = load_card(slug)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 404

    state = _load_json(_story_character_state_path(slug, branch_id), {"inventory": {}})
    npcs = _load_json(_story_npcs_path(slug, branch_id), [])
    card_npc = npcs[0] if npcs else {}
    clock_info = clock.load_clock(slug, branch_id)
    cur_dt = clock.now(slug, branch_id)

    after = request.args.get("after", type=int)
    all_msgs = _load_branch_messages(slug, branch_id)
    if after is not None:
        filtered_msgs = [m for m in all_msgs if m.get("index", 0) > after]
    else:
        filtered_msgs = all_msgs

    clean_msgs = []
    candidate_summaries = candidates.summaries(slug, branch_id, all_msgs)
    feedback_views = candidates.feedback_views(slug, branch_id, all_msgs)
    for m in filtered_msgs:
        clean_msgs.append({
            "role": m.get("role"),
            "content": m.get("content"),
            "index": m.get("index"),
            "candidate_summary": candidate_summaries.get(str(m.get("index"))),
            "feedback":feedback_views.get(str(m.get("index"))),
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
            "relationship_enabled": card.get("relationship_enabled", True),
        },
        "relationship": card_npc.get("relationship", {}),
        "relationship_display": _rel.display(card_npc.get("relationship")),
        "relationship_tiers": _rel.TIERS,
        "galgame": galgame.view(card, state),
        "memories": card_npc.get("memory", []),
        "inventory": state.get("inventory", {}),
        "persistent_instruction":instructions.read(slug,branch_id),
        "clock": {
            "time_str": cur_dt.strftime("%Y/%m/%d %H:%M"),
            "weather": clock_info.get("last_weather", "陰"),
            "location": clock_info.get("last_location", card.get("setting", {}).get("place", "")),
            "elapsed_minutes": clock_info.get("elapsed_minutes", 0),
        },
        "messages": clean_msgs,
    })


@card_bp.route("/api/card/<slug>/send/stream", methods=["POST"])
def api_card_send_stream(slug: str):
    """SSE streaming endpoint for player dialogue."""
    branch_id = g.card_branch
    data = request.get_json(force=True, silent=True) or {}
    try:
        narrative_style = styles.validate_style(data.get("narrative_style"))
        plot_mode = styles.validate_plot_mode(data.get("plot_mode"))
        scene_mode = styles.validate_scene_mode(data.get("scene_mode"))
        recall_mode = recall.validate_mode(data.get("recall_mode"))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    response_length = data.get("response_length")
    if response_length is not None and (not isinstance(response_length, str) or response_length not in RESPONSE_LENGTHS):
        return jsonify({"ok": False, "error": "response_length must be short, medium, long or unlimited"}), 400

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
                    branch_id=branch_id,
                    user_message=user_msg,
                    emit_chunk=emit_chunk,
                    async_recorder=is_async,
                    response_length=response_length,
                    plot_mode=plot_mode, scene_mode=scene_mode,
                    narrative_style=narrative_style,
                    recall_mode=recall_mode,
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
                cur_dt = clock.now(slug, branch_id)
                clock_info = clock.load_clock(slug, branch_id)
                npcs = _load_json(_story_npcs_path(slug, branch_id), [])
                card_npc = npcs[0] if npcs else {}
                state = _load_json(_story_character_state_path(slug, branch_id), {})

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


@card_bp.route("/api/card/<slug>/cancel", methods=["POST"])
def api_card_cancel(slug: str):
    """Stop the running generation; nothing from it is written."""
    from story_core.card_mode import generation_control
    return jsonify(ok=True, cancelled=generation_control.cancel(slug, g.card_branch))


@card_bp.route("/api/card/<slug>/regenerate", methods=["POST"])
@single_mutation
def api_card_regenerate(slug: str):
    """Regenerate the last GM message using snapshot restoration."""
    branch_id = g.card_branch
    data = request.get_json(force=True, silent=True) or {}
    try:
        narrative_style = styles.validate_style(data.get("narrative_style"))
        plot_mode = styles.validate_plot_mode(data.get("plot_mode"))
        scene_mode = styles.validate_scene_mode(data.get("scene_mode"))
        recall_mode = recall.validate_mode(data.get("recall_mode"))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    response_length = data.get("response_length")
    if response_length is not None and (not isinstance(response_length, str) or response_length not in RESPONSE_LENGTHS):
        return jsonify({"ok": False, "error": "response_length must be short, medium, long or unlimited"}), 400

    want_stream = request.headers.get("Accept") == "text/event-stream" or data.get("stream") is True

    msgs = _load_branch_messages(slug, branch_id)
    if not msgs:
        return jsonify({"ok": False, "error": "No messages found"}), 400

    last_msg = msgs[-1]
    if last_msg.get("role") != "gm" or last_msg.get("index", 0) <= 0:
        return jsonify({"ok": False, "error": "No GM reply to regenerate (opening message cannot be regenerated)"}), 400

    attempt = candidates.begin(slug, branch_id, msgs, data)

    # Restore state from snapshot before this GM reply
    pre_state = last_msg.get("pre_state_snapshot") or last_msg.get("state_snapshot")
    pre_npcs = last_msg.get("pre_npcs_snapshot") or last_msg.get("npcs_snapshot")
    pre_clock = last_msg.get("pre_clock_snapshot") or last_msg.get("clock_snapshot")

    # Recall is read-only and can fail independently. Resolve it before removing
    # the old reply so a retrieval/provider error cannot erase that saved turn.
    prepared_recall = None
    if recall_mode != "off":
        retained = msgs[:-1]
        recall_user = retained[-1].get("content", "") if retained and retained[-1].get("role") == "user" else ""
        recall_history = retained[:-1] if retained and retained[-1].get("role") == "user" else retained
        prior_npcs = pre_npcs or _load_json(_story_npcs_path(slug, branch_id), [])
        prior_actor = prior_npcs[0] if prior_npcs else {}
        try:
            from story_core.card_mode import lore
            prepared_recall = recall.collect(slug, branch_id, recall_mode, recall_user, recall_history,
                                             actor=load_card(slug).get("display_name", ""),
                                             memories=prior_actor.get("memory", []),
                                             lore_entries=lore.available(slug, branch_id, recall_history),
                                             current_location=(pre_clock or {}).get('last_location', ''))
        except Exception:
            log.exception("api_card_regenerate: recall failed before rewind")
            candidates.finish(slug, branch_id, attempt, error='回憶查詢失敗')
            return jsonify(ok=False, error="回憶查詢失敗，原回合仍保留，請重試。"), 502

    generation = branches.invalidate_generation(slug, branch_id)
    if pre_state:
        _save_json(_story_character_state_path(slug, branch_id), pre_state)
    if pre_npcs:
        _save_json(_story_npcs_path(slug, branch_id), pre_npcs)
    if pre_clock:
        clock.save_clock(slug, branch_id, pre_clock)

    # Only the last GM is removed; preserve any fully retained summarized prefix.
    compaction.invalidate_recap(slug, branch_id, retained_message_count=len(msgs) - 1)

    # Pop the last GM message
    msgs.pop()
    _save_json(_story_messages_path(slug, branch_id), msgs)
    last_user_msg = msgs[-1].get("content", "") if msgs and msgs[-1].get("role") == "user" else ""

    if want_stream:
        def generate():
            q: queue.Queue = queue.Queue()

            def emit_chunk(chunk: str):
                q.put(("text", chunk))

            def worker():
                try:
                    res = run_card_turn(
                        story_id=slug,
                        branch_id=branch_id,
                        user_message=last_user_msg,
                        skip_user_message=True,
                        expected_generation=generation,
                        response_length=response_length,
                        plot_mode=plot_mode, scene_mode=scene_mode,
                        narrative_style=narrative_style,
                        recall_mode=recall_mode,
                        prepared_recall=prepared_recall,
                        emit_chunk=emit_chunk,
                    )
                    with branches.lock(slug, branch_id, blocking=True):
                        candidates.finish(slug, branch_id, attempt, result=res)
                        if not res.get('ok'): candidates.recover_failed(slug, branch_id, attempt, generation)
                    q.put(("done", res))
                except Exception as e:
                    log.exception("api_card_regenerate: worker error")
                    with branches.lock(slug, branch_id, blocking=True):
                        candidates.finish(slug, branch_id, attempt, error=e)
                        candidates.recover_failed(slug, branch_id, attempt, generation)
                    q.put(("error", str(e)))

            threading.Thread(target=worker, daemon=True).start()

            while True:
                ev, payload = q.get()
                if ev == "text":
                    yield _sse_event({"type": "text", "chunk": payload})
                elif ev == "done":
                    cur_dt = clock.now(slug, branch_id)
                    clock_info = clock.load_clock(slug, branch_id)
                    npcs = _load_json(_story_npcs_path(slug, branch_id), [])
                    card_npc = npcs[0] if npcs else {}
                    state = _load_json(_story_character_state_path(slug, branch_id), {})

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
    try:
        res = run_card_turn(story_id=slug, branch_id=branch_id, user_message=last_user_msg, skip_user_message=True, expected_generation=generation, response_length=response_length, recall_mode=recall_mode, prepared_recall=prepared_recall, narrative_style=narrative_style, plot_mode=plot_mode, scene_mode=scene_mode)
        candidates.finish(slug, branch_id, attempt, result=res)
        if not res.get('ok'): candidates.recover_failed(slug, branch_id, attempt, generation)
    except Exception as exc:
        candidates.finish(slug, branch_id, attempt, error=exc)
        candidates.recover_failed(slug, branch_id, attempt, generation)
        raise
    return jsonify(res)


@card_bp.route("/api/card/<slug>/rewind", methods=["POST"])
@single_mutation
def api_card_rewind(slug: str):
    branch_id = g.card_branch
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

    msgs = _load_branch_messages(slug, branch_id)
    target_msg = None
    for m in msgs:
        if m.get("index") == to_index:
            target_msg = m
            break

    if target_msg is None:
        return jsonify({"ok": False, "error": f"Message with index {to_index} not found"}), 404

    branches.invalidate_generation(slug, branch_id)
    # Restore snapshot
    state_snap = target_msg.get("state_snapshot")
    npcs_snap = target_msg.get("npcs_snapshot")
    clock_snap = target_msg.get("clock_snapshot")

    if state_snap:
        _save_json(_story_character_state_path(slug, branch_id), state_snap)
    if npcs_snap:
        _save_json(_story_npcs_path(slug, branch_id), npcs_snap)
    if clock_snap:
        clock.save_clock(slug, branch_id, clock_snap)

    # Determine the retained prefix before fencing background summary jobs.
    kept = [m for m in msgs if m.get("index", 0) <= to_index]
    compaction.invalidate_recap(slug, branch_id, retained_message_count=len(kept))

    # Truncate messages after to_index
    _save_json(_story_messages_path(slug, branch_id), kept)
    from story_core.card_mode.pi_schedule import reconcile_after_rewind
    reconcile_after_rewind(slug, branch_id, kept)

    log.info("card_routes: rewound %s to index %d (kept %d msgs)", slug, to_index, len(kept))
    return jsonify({"ok": True, "to_index": to_index, "messages_count": len(kept)})


@card_bp.route("/api/card/<slug>/reset", methods=["POST"])
@single_mutation
def api_card_reset(slug: str):
    """Reset story to initial opening state (clears dialogue, memory, restores initial clock & relationship)."""
    branch_id = g.card_branch
    try:
        card = load_card(slug)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 404

    branches.invalidate_generation(slug, branch_id)
    compaction.invalidate_recap(slug, branch_id)
    # Rebuild this branch's opening without reinitializing the story tree.
    initial_rel = card.get('start', {}).get('relationship', {'tier':'陌生', 'note':''})
    state = {'inventory':card.get('start', {}).get('inventory', {}),
             'relationship_note':initial_rel.get('note', '')}
    from story_core.card_mode import galgame
    if galgame.enabled(card):
        state['galgame'] = galgame.initial()
    npcs = [{'name':card['display_name'], 'role':card.get('persona', {}).get('identity', ''),
             'lifecycle_status':'active', 'memory':[], 'current_status':'', 'relationship':initial_rel}]
    setting = card.get('setting', {})
    initial_clock = {'start_iso':setting.get('start_datetime', '2026-05-16T02:08'),
                     'elapsed_minutes':0, 'last_weather':(setting.get('weather_pool') or ['陰'])[0],
                     'last_location':setting.get('place', '')}
    _save_json(_story_character_state_path(slug, branch_id), state)
    _save_json(_story_npcs_path(slug, branch_id), npcs)
    clock.save_clock(slug, branch_id, initial_clock)
    _save_json(_story_messages_path(slug, branch_id), [{'role':'gm', 'content':card['opening'],
        'index':0, 'state_snapshot':state, 'npcs_snapshot':npcs, 'clock_snapshot':initial_clock}])

    from story_core.card_mode.pi_schedule import reconcile_after_rewind
    reconcile_after_rewind(slug, branch_id, _load_branch_messages(slug, branch_id))
    log.info("card_routes: reset %s to opening state", slug)
    return jsonify({"ok": True})


@card_bp.route('/api/card/<slug>/edit', methods=['POST'])
def api_card_edit(slug):
    from story_core.card_mode import editing
    body = request.get_json(silent=True)
    try:
        editing.validate(body)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    bid = g.card_branch
    if not body.get('stream') and request.headers.get('Accept') != 'text/event-stream':
        try:
            return jsonify(editing.edit_turn(slug, bid, body))
        except (branches.Busy, ensemble.Busy) as exc:
            return jsonify(ok=False,error=str(exc)),409
        except ValueError as exc:
            return jsonify(ok=False,error=str(exc)),400
    events = queue.Queue()
    def work():
        try:
            result = editing.edit_turn(slug, bid, body, emit_chunk=lambda chunk: events.put({'type':'text','chunk':chunk}))
            events.put({'type':'done','result':result})
        except Exception as exc:
            log.exception('card edit failed')
            events.put({'type':'error','message':str(exc) if isinstance(exc,(ValueError,ensemble.Busy)) else '生成失敗，原對話已保留，請重試。'})
        finally: events.put(None)
    threading.Thread(target=work,daemon=True).start()
    def stream():
        while True:
            try: event=events.get(timeout=10)
            except queue.Empty:
                yield ': keepalive\n\n'; continue
            if event is None: return
            yield _sse_event(event)
    return Response(stream_with_context(stream()),mimetype='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@card_bp.route('/api/card/<slug>/memories', methods=['GET', 'POST'])
def api_card_memories(slug):
    from story_core.card_mode.memory_editor import operate
    try:
        body = None
        if request.method == 'POST':
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                raise branches.BranchError('Expected JSON object')
        return jsonify(operate(slug, g.card_branch, body))
    except ensemble.Busy as exc:
        return jsonify(ok=False, error=str(exc)), 409


@card_bp.route('/api/card/<slug>/lore', methods=['GET', 'POST'])
def api_card_lore(slug):
    from story_core.card_mode import lore_editor
    load_card(slug)
    if ensemble.enabled(slug):
        raise branches.BranchError('群像模式尚未使用本線設定')
    if request.method == 'GET':
        # Reading needs no lock, so the panel stays usable while a turn is generating.
        return jsonify(lore_editor.operate(slug, g.card_branch))
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise branches.BranchError('Expected JSON object')
    try:
        with branches.lock(slug, g.card_branch):
            return jsonify(lore_editor.operate(slug, g.card_branch, body))
    except OSError:
        log.exception('lore storage failed for %s/%s', slug, g.card_branch)
        return jsonify(ok=False, error='保存失敗，草稿仍保留，請稍後再試。'), 500


@card_bp.route('/api/card/<slug>/candidates', methods=['GET', 'POST'])
def api_card_candidates(slug):
    load_card(slug)
    bid = g.card_branch
    guard = ensemble.lock(slug, branch_id=bid) if ensemble.enabled(slug) else branches.lock(slug, bid)
    try:
        with guard:
            if request.method == 'GET':
                return jsonify(candidates.listing(slug, bid, request.args.get('message_index', type=int),
                                                  request.args.get('favorites') == '1'))
            body = request.get_json(silent=True)
            if not isinstance(body, dict): raise branches.BranchError('請提供版本資料')
            if body.get('action') == 'favorite': return jsonify(candidates.favorite(slug, bid, body))
            if body.get('action') == 'select': return jsonify(candidates.select(slug, bid, body))
            if body.get('action') == 'edit-text':
                try:
                    return jsonify(candidates.edit_latest(slug, bid, body))
                except OSError:
                    log.exception('reply version storage failed for %s/%s', slug, bid)
                    return jsonify(ok=False, error='保存失敗，草稿仍保留，請稍後再試。'), 500
            raise branches.BranchError('無效的版本操作')
    except ensemble.Busy as exc:
        return jsonify(ok=False, error=str(exc)), 409


@card_bp.route('/api/card/<slug>/feedback', methods=['GET', 'POST'])
def api_card_feedback(slug):
    load_card(slug)
    bid = g.card_branch
    guard = ensemble.lock(slug, branch_id=bid) if ensemble.enabled(slug) else branches.lock(slug, bid)
    try:
        with guard:
            if request.method == 'GET': return jsonify(candidates.feedback_listing(slug,bid))
            return jsonify(candidates.set_feedback(slug,bid,request.get_json(silent=True)))
    except ensemble.Busy as exc:
        return jsonify(ok=False,error=str(exc)),409


@card_bp.route('/api/card/<slug>/instruction', methods=['GET', 'POST'])
def api_card_instruction(slug):
    load_card(slug)
    bid = g.card_branch
    guard = ensemble.lock(slug,branch_id=bid) if ensemble.enabled(slug) else branches.lock(slug,bid)
    try:
        with guard:
            config = instructions.read(slug,bid) if request.method == 'GET' else instructions.update(slug,bid,request.get_json(silent=True))
            return jsonify(ok=True,persistent_instruction=config)
    except ensemble.Busy as exc:
        return jsonify(ok=False,error=str(exc)),409
    except OSError:
        log.exception('persistent instruction storage failed for %s/%s',slug,bid)
        return jsonify(ok=False,error='保存失敗，草稿仍保留，請稍後再試。'),500
