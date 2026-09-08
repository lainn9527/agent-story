"""Card Mode turn execution pipeline: PASS 1 (Streaming) + PASS 2 (Recorder)."""

from __future__ import annotations

import copy
import logging
import threading

from story_core.branch_tree import _next_timeline_index, get_full_timeline
from story_core.card_mode import clock
from story_core.card_mode.card import load_card
from story_core.card_mode.contract import RECORDER_INSTRUCTION
from story_core.card_mode.ops import card_tool_declarations, execute_card_tool
from story_core.card_mode.prompt import build_card_system_prompt
from story_core import compaction
from story_core.gm_v2 import relationship as _rel
from story_core.story_io import (
    _load_branch_messages,
    _load_json,
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
                    log.warning("card_pipeline: stream errored mid-narration, keeping %d chars", sum(len(b) for b in buf))
                    break
        except Exception:
            log.exception("card_pipeline: stream call raised")

        if buf:
            return "".join(buf), "stream"

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

    # Retry once with flash-lite
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


def run_card_turn(
    story_id: str,
    branch_id: str = "main",
    user_message: str = "",
    emit_chunk=None,
    llm_fn=None,
    recorder_fn=None,
    skip_user_message: bool = False,
    async_recorder: bool = True,
) -> dict:
    """Run a single Card Mode turn.

    PASS 1: Stream narration, commit user and GM messages with snapshots, sync clock from header.
    PASS 2: Run focused recorder pass calling 4 tools, update snapshot on GM message, compact recap.
    """
    card = load_card(story_id)
    state = _load_json(_story_character_state_path(story_id, branch_id), {"inventory": {}})
    npcs = _load_json(_story_npcs_path(story_id, branch_id), [])
    card_npc = npcs[0] if npcs else {
        "name": card.get("display_name", story_id),
        "role": card.get("persona", {}).get("identity", ""),
        "lifecycle_status": "active",
        "memory": [],
        "current_status": "",
        "relationship": card.get("start", {}).get("relationship", {"tier": "陌生", "note": ""}),
    }
    clock_dt = clock.now(story_id, branch_id)
    clock_info = clock.load_clock(story_id, branch_id)
    recap_data = compaction.load_recap(story_id, branch_id)
    recap_text = recap_data.get("recap_text", "")

    # Build Prompt
    system_prompt = build_card_system_prompt(
        card=card,
        state=state,
        npc=card_npc,
        clock_dt=clock_dt,
        last_weather=clock_info.get("last_weather", ""),
        recap_text=recap_text,
    )

    full_timeline = get_full_timeline(story_id, branch_id)
    if skip_user_message and full_timeline and full_timeline[-1].get("role") == "user":
        history_for_context = full_timeline[:-1]
    else:
        history_for_context = full_timeline
    recent = compaction.get_context_window(history_for_context)

    # PASS 1: Narrate
    narration, source = _generate_pass1(
        user_msg=user_message,
        system_prompt=system_prompt,
        recent=recent,
        emit_chunk=emit_chunk,
        llm_fn=llm_fn,
    )

    if not narration or source == "failed":
        return {
            "ok": False,
            "error": "GM generation failed or provider unavailable",
            "gm_source": source,
        }

    # Pre-record snapshots
    state_snap = copy.deepcopy(state)
    npcs_snap = copy.deepcopy(npcs or [card_npc])
    clock_snap = copy.deepcopy(clock_info)

    base_idx = _next_timeline_index(story_id, branch_id, full_timeline)
    user_idx = None
    if not skip_user_message:
        user_idx = base_idx
        _upsert_branch_message(story_id, branch_id, {
            "role": "user",
            "content": user_message,
            "index": user_idx,
            "state_snapshot": state_snap,
            "npcs_snapshot": npcs_snap,
            "clock_snapshot": clock_snap,
        })
        gm_idx = base_idx + 1
    else:
        gm_idx = base_idx

    _upsert_branch_message(story_id, branch_id, {
        "role": "gm",
        "content": narration,
        "index": gm_idx,
        "pre_state_snapshot": state_snap,
        "pre_npcs_snapshot": npcs_snap,
        "pre_clock_snapshot": clock_snap,
        "state_snapshot": state_snap,
        "npcs_snapshot": npcs_snap,
        "clock_snapshot": clock_snap,
    })

    # Sync clock from model output header
    clock.sync_from_narration(story_id, branch_id, narration)

    # PASS 2: Recorder pass
    def _execute_pass2():
        try:
            if recorder_fn:
                recorder_fn(story_id, branch_id, narration, gm_idx)
            else:
                from story_core.gemini_bridge import call_gemini_gm_tools
                from story_core.llm_bridge import _get_config

                recorder_prompt = (
                    f"{RECORDER_INSTRUCTION}\n\n"
                    f"【當前卡片角色】{card.get('display_name', '')}\n"
                    f"【當前關係】{_rel.display(card_npc.get('relationship'))}\n"
                    f"【已知長期記憶】{card_npc.get('memory', [])[-8:]}\n"
                    f"【當前隨身物】{state.get('inventory', {})}\n"
                    f"【當前時間】{clock_dt.strftime('%Y/%m/%d %H:%M')}\n"
                )

                g = _get_config().get("gemini", {})
                model = "gemini-3.1-flash-lite"

                def on_tool(name, args):
                    return execute_card_tool(story_id, branch_id, name, args)

                call_gemini_gm_tools(
                    user_message="【本回合敘事】\n" + narration,
                    system_prompt=recorder_prompt,
                    recent_messages=[],
                    gemini_cfg=g,
                    model=model,
                    on_tool=on_tool,
                    tool_decls=card_tool_declarations(),
                    max_rounds=4,
                )

            # Re-snapshot on committed GM message with post-record data
            new_state = _load_json(_story_character_state_path(story_id, branch_id), {})
            new_npcs = _load_json(_story_npcs_path(story_id, branch_id), [])
            new_clock = clock.load_clock(story_id, branch_id)

            cur_msgs = _load_branch_messages(story_id, branch_id)
            for m in cur_msgs:
                if m.get("index") == gm_idx:
                    m["state_snapshot"] = new_state
                    m["npcs_snapshot"] = new_npcs
                    m["clock_snapshot"] = new_clock
                    _upsert_branch_message(story_id, branch_id, m)
                    break

            # Trigger compaction if threshold met
            updated_timeline = get_full_timeline(story_id, branch_id)
            cur_recap = compaction.load_recap(story_id, branch_id)
            if compaction.should_compact(cur_recap, len(updated_timeline)):
                compaction.compact_async(story_id, branch_id, updated_timeline)

        except Exception:
            log.exception("card_pipeline: PASS 2 recorder execution raised")

    if async_recorder:
        threading.Thread(target=_execute_pass2, daemon=True).start()
    else:
        _execute_pass2()

    return {
        "ok": True,
        "narration": narration,
        "gm_source": source,
        "user_index": user_idx,
        "gm_index": gm_idx,
    }
