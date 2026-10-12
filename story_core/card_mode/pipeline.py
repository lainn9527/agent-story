"""Card Mode turn execution pipeline: PASS 1 (Streaming) + PASS 2 (Recorder)."""

from __future__ import annotations

import copy
import logging
import os
import threading
import time
import uuid
from functools import wraps

from story_core.branch_tree import _next_timeline_index, get_full_timeline
from story_core.card_mode import clock, recall, branches, generation_control, styles, candidates, instructions, lore
from story_core.card_mode.card import load_card
from story_core.card_mode.fields import persona_text
from story_core.card_mode.contract import RECORDER_INSTRUCTION
from story_core.card_mode.ops import card_tool_declarations, execute_card_tool, apply_card_tool
from story_core.card_mode.prompt import build_card_system_prompt, relationship_display
from story_core.card_mode.memory import memory_kwargs, select_memories, memories_before_context
from story_core.card_mode.recap import COMPACT_PROMPT, META_COMPACT_PROMPT, RECAP_VERSION, context_window
from story_core import compaction
from story_core import usage_db
from story_core.gm_v2 import relationship as _rel
from story_core.story_io import (
    _load_branch_messages,
    _load_json,
    _save_json,
    _story_character_state_path,
    _story_npcs_path,
    _upsert_branch_message,
)

log = logging.getLogger("rpg")


def _is_gm_error(raw: object) -> bool:
    if not raw or not str(raw).strip():
        return True
    s = str(raw).strip()
    return s.startswith("【系統錯誤】") or s.startswith("【系統提示】Gemini 安全")


def _generate_pass1(
    user_msg: str,
    system_prompt: str,
    recent: list,
    emit_chunk=None,
    llm_fn=None,
) -> tuple[str | None, str]:
    """Generate PASS 1 narration with streaming and fallback.

    Returns (narration_text, source_str).
    """
    if llm_fn:
        try:
            raw = llm_fn(user_msg, system_prompt, recent)
            if emit_chunk and raw:
                try:
                    emit_chunk(raw)
                except Exception:
                    pass
            return raw, "custom_llm"
        except Exception:
            log.exception("card_pipeline: custom llm_fn failed")
            return None, "failed"

    # Production path
    from story_core.llm_bridge import call_claude_gm, call_claude_gm_stream

    buf: list[str] = []
    if emit_chunk:
        completed = False
        try:
            for ev, payload in call_claude_gm_stream(user_msg, system_prompt, recent):
                if ev == "text" and payload:
                    chunk = str(payload)
                    buf.append(chunk)
                    try:
                        emit_chunk(chunk)
                    except Exception:
                        pass
                elif ev == "error":
                    if not buf:
                        log.warning("card_pipeline: stream failed before first token; falling back to blocking GM")
                        break
                    log.warning("card_pipeline: incomplete stream discarded (%d chars)", sum(len(b) for b in buf))
                    return None, "failed"
                elif ev == "done":
                    if isinstance(payload, dict) and payload.get('incomplete'):
                        return None, "failed"
                    completed = True
        except Exception:
            log.exception("card_pipeline: stream call raised")
            if buf: return None, "failed"

        if buf and completed:
            return "".join(buf), "stream"
        if buf: return None, "failed"

    # Fallback to blocking GM call (retry once)
    log.info("card_pipeline: attempting blocking GM call")
    try:
        raw, _ = call_claude_gm(user_msg, system_prompt, recent)
        if not _is_gm_error(raw):
            if emit_chunk and raw:
                try:
                    emit_chunk(raw)
                except Exception:
                    pass
            return raw, "blocking"
    except Exception:
        log.exception("card_pipeline: blocking GM call failed")

    # OpenRouter already received a streaming attempt plus a blocking retry.
    # Fail closed instead of silently crossing into a separately billed provider.
    from story_core.llm_bridge import get_provider
    if get_provider() in ("openrouter", "open_router"):
        return None, "failed"

    # Legacy retry once with Gemini flash-lite for existing providers.
    try:
        from story_core.gemini_bridge import call_gemini_gm
        from story_core.llm_bridge import _get_config
        g = _get_config().get("gemini", {})
        raw, _ = call_gemini_gm(user_msg, system_prompt, recent, gemini_cfg=g, model="gemini-3.1-flash-lite")
        if not _is_gm_error(raw):
            if emit_chunk and raw:
                try:
                    emit_chunk(raw)
                except Exception:
                    pass
            return raw, "fallback_lite"
    except Exception:
        log.exception("card_pipeline: fallback flash-lite failed")

    return None, "failed"


# In-process recorder threads per branch (thread -> generation token at start), so a
# new turn can wait for them instead of failing.
_recorders: dict[tuple[str, str], dict[threading.Thread, str | None]] = {}
_recorders_guard = threading.Lock()
RECORDER_WAIT_SECONDS = 60


def _wait_for_recorders(story_id, branch_id, timeout=RECORDER_WAIT_SECONDS):
    """Join this branch's running recorders; the branch lock must not be held here.

    Regenerate/rewind/reset/edit invalidate the generation and cancel the pending
    record, so a recorder from an older generation is never waited for.
    """
    with _recorders_guard:
        pending = list(_recorders.get((story_id, branch_id), {}).items())
    deadline = time.monotonic() + timeout
    for thread, started_generation in pending:
        while thread.is_alive() and time.monotonic() < deadline:
            generation_control.check()  # the player may stop while we wait
            if branches.generation(story_id, branch_id) != started_generation:
                break
            thread.join(0.1)


def _locked_turn(fn):
    @wraps(fn)
    def wrapped(story_id, branch_id="main", *args, **kwargs):
        # A reply sent while the previous turn is still recording waits rather than
        # failing with Busy; ensure_settled below still guards other writers.
        with generation_control.turn(story_id, branch_id,
                                     before_lock=lambda: _wait_for_recorders(story_id, branch_id)):
            expected = kwargs.pop("expected_generation", None)
            if expected is not None and expected != branches.generation(story_id, branch_id):
                raise branches.BranchError("此重生已被回退或重置取消，未寫入回覆。")
            branches.ensure_settled(_load_branch_messages(story_id, branch_id))
            return fn(story_id, branch_id, *args, **kwargs)
    return wrapped


@_locked_turn
def run_card_turn(
    story_id: str,
    branch_id: str = "main",
    user_message: str = "",
    emit_chunk=None,
    llm_fn=None,
    recorder_fn=None,
    skip_user_message: bool = False,
    async_recorder: bool = True,
    response_length: str | None = None,
    recall_mode: str | None = None,
    prepared_recall: dict | None = None,
    revision: str | None = None,
    narrative_style: str | None = None,
    plot_mode: str | None = None,
    scene_mode: str | None = None,
) -> dict:
    """Run a single Card Mode turn.

    PASS 1: Stream narration, commit user and GM messages with snapshots, sync clock from header.
    PASS 2: Run focused recorder pass calling 4 tools, update snapshot on GM message, compact recap.
    """
    narrative_style = styles.validate_style(narrative_style)
    plot_mode = styles.validate_plot_mode(plot_mode)
    scene_mode = styles.validate_scene_mode(scene_mode)
    recall_mode = recall.validate_mode(recall_mode)
    card = load_card(story_id)
    from story_core.card_mode import galgame
    state = _load_json(_story_character_state_path(story_id, branch_id), {"inventory": {}})
    npcs = _load_json(_story_npcs_path(story_id, branch_id), [])
    card_npc = npcs[0] if npcs else {
        "name": card.get("display_name", story_id),
        "role": persona_text(card.get("persona", {}).get("identity", "")),
        "lifecycle_status": "active",
        "memory": [],
        "current_status": "",
        "relationship": card.get("start", {}).get("relationship", {"tier": "陌生", "note": ""}),
    }
    clock_dt = clock.now(story_id, branch_id)
    clock_info = clock.load_clock(story_id, branch_id)
    recap_data = compaction.load_recap(story_id, branch_id)
    recap_text = recap_data.get("recap_text", "")

    full_timeline = get_full_timeline(story_id, branch_id)
    if skip_user_message and full_timeline and full_timeline[-1].get("role") == "user":
        history_for_context = full_timeline[:-1]
    else:
        history_for_context = full_timeline
    # Include every not-yet-summarized message while the background job catches up.
    recent = context_window(history_for_context, recap_data)
    memory_query = user_message + "\n" + "\n".join(
        m.get("content", "") for m in history_for_context[-4:])
    galgame_post = None
    prompt_state = state
    if galgame.enabled(card):
        try:
            assessment = generation_control.external(lambda _: galgame.assess(
                story_id, branch_id, user_message, history_for_context))
            generation_control.check()
            galgame_post = galgame.settle(state.get("galgame"), assessment,
                                          clock_dt.date().isoformat(), clock_info.get("elapsed_minutes", 0))
            prompt_state = {**state, "galgame": galgame_post}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    system_prompt = build_card_system_prompt(
        card=card, state=prompt_state, npc=card_npc, clock_dt=clock_dt,
        last_weather=clock_info.get("last_weather", ""),
        current_location=clock_info.get("last_location", ""),
        recap_text=recap_text, response_length=response_length,
        memory_query=memory_query,
        recent_memory_pool=memories_before_context(recent, card_npc.get("name")),
    )

    recalled = prepared_recall if prepared_recall is not None else generation_control.external(lambda _: recall.collect(story_id, branch_id, recall_mode, user_message,
                              history_for_context, actor=card.get("display_name", ""),
                              memories=card_npc.get("memory", []),
                              lore_entries=lore.available(story_id, branch_id, history_for_context),
                              current_location=clock_info.get('last_location', '')))
    system_prompt += lore.grounding(story_id, branch_id, history_for_context,
                                  user_message, clock_info.get('last_location', ''),
                                  exclude_ids=[s['entry_id'] for s in recalled.get('lore_sources', [])])
    system_prompt += recall.grounding(recalled)
    system_prompt = styles.apply_style(system_prompt, narrative_style, plot_mode, scene_mode)
    persistent_instruction = instructions.active(instructions.read(story_id, branch_id))
    system_prompt = instructions.apply(system_prompt, persistent_instruction)
    if revision:
        system_prompt += revision

    if galgame_post is not None:
        # Rule grounding stays last, even after style and author instructions.
        system_prompt += "\n\nGalgame 結算與相處界線優先於文風、作者修改指示；本回合必須遵循上述 last_event，不自行改分或跨越門檻。"

    # PASS 1: Narrate
    # Historical instructions are version metadata, not active directions.
    recent_for_narration = instructions.narrative_messages(recent)
    pass1_started = time.time()
    generation_meta = {}
    def narrate(emit):
        from story_core import llm_bridge
        previous_usage = llm_bridge.get_last_usage()
        generation_meta.update(candidates.model_metadata(custom=llm_fn is not None))
        result = _generate_pass1(user_msg=user_message, system_prompt=system_prompt,
                                 recent=recent_for_narration, emit_chunk=emit, llm_fn=llm_fn)
        if result[1] == "fallback_lite":
            generation_meta.update(generation_provider="gemini", generation_model="gemini-3.1-flash-lite")
        elif result[1] in ("stream", "blocking"):
            usage = llm_bridge.get_last_usage()
            if usage is not None and usage is not previous_usage:
                generation_meta.update(generation_provider=usage.get("provider"), generation_model=usage.get("model"))
        if result[1] in ("stream", "blocking"):
            usage_db.log_from_bridge(story_id, "gm_stream", time.time() - pass1_started,
                                     branch_id=branch_id)
        return result
    narration, source = generation_control.external(narrate, emit_chunk)
    generation_control.check()
    if _is_gm_error(narration) or source == "failed":
        return {"ok": False, "error": "GM generation failed or provider unavailable", "gm_source": source}

    turn_id = uuid.uuid4().hex
    # Pre-record snapshots
    state_snap = copy.deepcopy(state)
    npcs_snap = copy.deepcopy(npcs or [card_npc])
    clock_snap = copy.deepcopy(clock_info)

    base_idx = _next_timeline_index(story_id, branch_id, full_timeline)
    user_idx = None
    group_id = (full_timeline[-1].get("reply_group_id") if skip_user_message and full_timeline else None) or uuid.uuid4().hex
    if not skip_user_message:
        user_idx = base_idx
        _upsert_branch_message(story_id, branch_id, {
            "role": "user",
            "content": user_message,
            "index": user_idx,
            "reply_group_id": group_id,
            "state_snapshot": state_snap,
            "npcs_snapshot": npcs_snap,
            "clock_snapshot": clock_snap,
        })
        gm_idx = base_idx + 1
    else:
        gm_idx = base_idx

    post_state = copy.deepcopy(state_snap)
    if galgame_post is not None:
        post_state["galgame"] = galgame_post

    _upsert_branch_message(story_id, branch_id, {
        "role": "gm",
        "content": narration,
        "index": gm_idx,
        "card_turn_id": turn_id,
        "reply_group_id": group_id,
        "reply_version_id": turn_id,
        "generated_at": candidates.stamp(),
        **generation_meta,
        "response_length": response_length,
        "recall_mode": recall_mode,
        "card_record_status": "pending",
        "card_record_pid": os.getpid(),
        "narrative_style": narrative_style,
        "plot_mode": plot_mode,
        "scene_mode": scene_mode,
        "persistent_instruction": persistent_instruction,
        "pre_state_snapshot": state_snap,
        "pre_npcs_snapshot": npcs_snap,
        "pre_clock_snapshot": clock_snap,
        "state_snapshot": post_state,
        "npcs_snapshot": npcs_snap,
        "clock_snapshot": clock_snap,
    })

    if galgame_post is not None:
        _save_json(_story_character_state_path(story_id, branch_id), post_state)

    # Sync clock from model output header
    header_owns_time = clock.sync_from_narration(story_id, branch_id, narration)

    # Persist the synchronized clock before the asynchronous recorder starts.
    for message in _load_branch_messages(story_id, branch_id):
        if message.get("index") == gm_idx:
            message["clock_snapshot"] = clock.load_clock(story_id, branch_id)
            message["header_owns_time"] = header_owns_time
            _upsert_branch_message(story_id, branch_id, message)
            candidates.sync_existing(story_id, branch_id, message)
            break

    record_saved_turn(story_id, branch_id, message, user_message, recorder_fn=recorder_fn,
                      llm_fn=llm_fn, async_recorder=async_recorder)

    return {
        "ok": True,
        "narration": narration,
        "gm_source": source,
        "user_index": user_idx,
        "gm_index": gm_idx,
        "reply_version_id": turn_id,
    }


def record_saved_turn(story_id, branch_id, message, user_message, *, recorder_fn=None, llm_fn=None, async_recorder=True):
    """Record a saved narration without asking the narrator to write it again."""
    card = load_card(story_id)
    narration, gm_idx, turn_id = message['content'], message['index'], message['card_turn_id']
    state_snap = copy.deepcopy(message['pre_state_snapshot'])
    npcs_snap = copy.deepcopy(message['pre_npcs_snapshot'])
    state, card_npc = state_snap, npcs_snap[0]
    header_owns_time = message.get('header_owns_time', bool(clock.parse_header_time(narration)))
    # Private candidate: model callbacks never modify live files.
    staged_state = copy.deepcopy(state_snap)
    from story_core.card_mode import galgame
    if galgame.enabled(card):
        # Settlement belongs to the saved reply, not Recorder. Re-recording is idempotent.
        staged_state["galgame"] = copy.deepcopy(message["state_snapshot"].get("galgame", galgame.initial()))
    staged_npcs = copy.deepcopy(npcs_snap)
    staged_clock = copy.deepcopy(message["clock_snapshot"])

    def retained_message():
        return next((m for m in _load_branch_messages(story_id, branch_id)
                     if m.get('card_turn_id') == turn_id and m.get('card_record_status') == 'pending'), None)

    # PASS 2: Recorder pass
    def _execute_pass2_locked():
        try:
            if retained_message() is None:
                return
            if recorder_fn:
                recorder_fn(story_id, branch_id, narration, gm_idx)
            else:
                from story_core.gemini_bridge import call_gemini_gm_tools
                from story_core.llm_bridge import _get_config, call_gm_tools, get_provider, get_recorder_model

                relationship_enabled = card.get("relationship_enabled", True) and not galgame.enabled(card)
                recorder_instruction = RECORDER_INSTRUCTION
                if not relationship_enabled:
                    start = recorder_instruction.index("1. `set_relationship")
                    end = recorder_instruction.index("2. `remember", start)
                    recorder_instruction = recorder_instruction[:start] + recorder_instruction[end:]
                    recorder_instruction = recorder_instruction.replace("以下 5 個工具", "以下工具")
                recorder_prompt = (
                    f"{recorder_instruction}\n\n"
                    f"【當前卡片角色】{card.get('display_name', '')}\n" +
                    (f"【當前關係】{relationship_display(card, card_npc.get('relationship'))}\n" if relationship_enabled else "") +
                    f"【已知長期記憶】{select_memories(card_npc.get('memory', []), user_message + narration, **memory_kwargs(card))}\n"
                    f"【當前隨身物】{state.get('inventory', {})}\n"
                    f"【當前時間】{clock.now(story_id, branch_id).strftime('%Y/%m/%d %H:%M')}\n"
                    f"【時間來源】{'標頭已提供，本輪不得再呼叫 advance_time' if header_owns_time else '未提供標頭，只記明確經過的時間'}\n"
                )

                g = _get_config().get("gemini", {})
                model = get_recorder_model() or g.get("model", "gemini-3.7-flash")

                def on_tool(name, args):
                    if name == "set_relationship" and not relationship_enabled:
                        return {"ok": False, "error": "此卡已停用關係標籤更新"}
                    if name == "advance_time" and header_owns_time:
                        return {"ok": True, "applied": {}, "note": "Time already supplied by narration header"}
                    if retained_message() is None:
                        return {"ok": False, "error": "此回合已取消，結果不會寫入。"}
                    if name == "set_relationship" and card.get("relationship_note_enabled", True) is False:
                        old_relation = copy.deepcopy(staged_npcs[0].get("relationship", {}))
                        old_note = staged_state.get("relationship_note")
                        had_note = "relationship_note" in staged_state
                        result = apply_card_tool(staged_state, staged_npcs, staged_clock, name, args)
                        if result.get("ok"):
                            if "note" in old_relation:
                                staged_npcs[0]["relationship"]["note"] = old_relation["note"]
                            else:
                                staged_npcs[0]["relationship"].pop("note", None)
                            if had_note:
                                staged_state["relationship_note"] = old_note
                            else:
                                staged_state.pop("relationship_note", None)
                            result["applied"] = {"tier": staged_npcs[0]["relationship"]["tier"]}
                        return result
                    return apply_card_tool(staged_state, staged_npcs, staged_clock, name, args)

                recorder_started = time.time()
                recorder_args = {
                    "user_message": "【玩家本回合原文】\n" + user_message + "\n【本回合敘事】\n" + narration,
                    "system_prompt": recorder_prompt,
                    "recent_messages": [],
                    "on_tool": on_tool,
                    "tool_decls": [tool for tool in card_tool_declarations()
                                   if relationship_enabled or tool["name"] != "set_relationship"],
                    "max_rounds": 4,
                }
                if llm_fn is None and get_provider() in ("vertex", "openrouter", "open_router"):
                    record_result = call_gm_tools(**recorder_args, **({"model_override": get_recorder_model()} if get_recorder_model() else {}))
                    usage_db.log_from_bridge(
                        story_id, "card_recorder", time.time() - recorder_started,
                        branch_id=branch_id,
                    )
                else:
                    record_result = call_gemini_gm_tools(
                        **recorder_args,
                        gemini_cfg=g,
                        model=model,
                    )

                if isinstance(record_result, tuple) and len(record_result) >= 3 and record_result[2] != "ok":
                    raise ValueError("記憶整理未完成")

            with branches.lock(story_id, branch_id, blocking=True):
                if retained_message() is None:
                    return
                if not recorder_fn:
                    _save_json(_story_character_state_path(story_id, branch_id), staged_state)
                    _save_json(_story_npcs_path(story_id, branch_id), staged_npcs)
                    clock.save_clock(story_id, branch_id, staged_clock)
                # Re-snapshot on committed GM message with post-record data
                new_state = _load_json(_story_character_state_path(story_id, branch_id), {})
                new_npcs = _load_json(_story_npcs_path(story_id, branch_id), [])
                new_clock = clock.load_clock(story_id, branch_id)

                cur_msgs = _load_branch_messages(story_id, branch_id)
                for m in cur_msgs:
                    if m.get("card_turn_id") == turn_id:
                        m["card_record_status"] = "done"
                        m["state_snapshot"] = new_state
                        m["npcs_snapshot"] = new_npcs
                        m["clock_snapshot"] = new_clock
                        _upsert_branch_message(story_id, branch_id, m)
                        candidates.sync_existing(story_id, branch_id, m)
                        break

                # Trigger compaction if threshold met
                updated_timeline = get_full_timeline(story_id, branch_id)
                cur_recap = compaction.load_recap(story_id, branch_id)
                if (compaction.should_compact(cur_recap, len(updated_timeline))
                        or (cur_recap.get("recap_version") != RECAP_VERSION
                            and len(updated_timeline) > compaction.RECENT_WINDOW)):
                    compaction.compact_async(
                        story_id, branch_id, updated_timeline,
                        prompt_template=COMPACT_PROMPT,
                        meta_prompt_template=META_COMPACT_PROMPT,
                        max_message_chars=None, batch_chars=60000, recap_version=RECAP_VERSION,
                    )
        except Exception:
            log.exception("card_pipeline: PASS 2 recorder execution raised")

    def _execute_pass2():
        if recorder_fn:
            # Trusted synchronous integration hooks may write directly. Production
            # model recording always uses the staged, cancellable path above.
            with branches.lock(story_id, branch_id, blocking=True):
                if retained_message() is not None: _execute_pass2_locked()
        else:
            _execute_pass2_locked()
        with branches.lock(story_id, branch_id, blocking=True):
            message = retained_message()
            if message is not None:
                message['card_record_status'] = 'error'
                _upsert_branch_message(story_id, branch_id, message)
                candidates.sync_existing(story_id, branch_id, message)

        if not recorder_fn:
            from story_core.card_mode.pi_schedule import maybe_schedule
            maybe_schedule(story_id, branch_id)
            try:
                from story_core.card_mode import claude_maintenance
                claude_maintenance.maybe_run(story_id, branch_id, gm_idx)
            except Exception:
                log.exception("card_pipeline: claude maintenance trigger failed")

    if async_recorder:
        key = (story_id, branch_id)

        def _tracked():
            try:
                _execute_pass2()
            finally:
                with _recorders_guard:
                    _recorders.get(key, {}).pop(thread, None)

        thread = threading.Thread(target=_tracked, name="card-recorder-"+turn_id, daemon=True)
        with _recorders_guard:
            _recorders.setdefault(key, {})[thread] = branches.generation(story_id, branch_id)
        thread.start()
    else:
        _execute_pass2()
