"""GM v2 turn orchestrator: PRE -> GENERATE -> PARSE -> GATE -> APPLY -> CRITIC.

One LLM call per turn. The GM authors narration + a grounded delta block; the
server parses, deterministically gates, applies via production persistence
(方案甲), and fires the async critic for flagged items. The LLM call is injectable
(`llm_fn`) so the whole turn is testable without a provider.
"""

import copy
import logging
import os
import re
import sys
import threading

from story_core import story_io as sio
from story_core.gm_v2 import critic as v2critic
from story_core.gm_v2 import prompt as v2prompt
from story_core.gm_v2.apply import apply_clean_delta
from story_core.gm_v2.ops import gate_delta
from story_core.gm_v2.parse import parse_turn_output

log = logging.getLogger("rpg")

RECENT_LIMIT = 8

# Coarse time-of-day periods (weak→ordered) + their narrative cue words. The 副本/scene 時辰 isn't
# an engine field (world_day's fraction is home-world clock, frozen in a 副本), so we DERIVE the
# current 時辰 from the last narration and ground it forward-only — fixes 晚上→中午 jumps.
_TOD_MAP = [
    ("深夜", ("深夜", "子時", "丑時", "夜半", "三更", "半夜", "午夜")),
    ("凌晨", ("凌晨", "拂曉", "五更", "雞鳴")),
    ("清晨", ("清晨", "早晨", "破曉", "天明", "天剛亮", "晨光", "卯時", "晨曦", "黎明")),
    ("上午", ("上午", "巳時")),
    ("中午", ("中午", "正午", "午時", "晌午", "日正當中")),
    ("下午", ("下午", "未時", "申時")),
    ("傍晚", ("傍晚", "黃昏", "酉時", "日落", "夕陽", "暮色", "薄暮")),
    ("夜晚", ("入夜", "戌時", "亥時", "月色", "月光", "夜色", "夜幕", "今晚", "當夜", "夜晚", "夜")),
]


def _scene_time_of_day(text: str) -> str:
    """The LAST-occurring time cue in the tail of the last narration = the current 時辰."""
    if not text:
        return ""
    tail = text[-400:]
    best, best_pos = "", -1
    for period, kws in _TOD_MAP:
        for kw in kws:
            p = tail.rfind(kw)
            if p > best_pos:
                best_pos, best = p, period
    return best


def _app():
    import app as app_module

    return app_module


def _time_tools():
    """The app-free time-economy engine (scripts/play/time_tools.py)."""
    play_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "scripts", "play",
    )
    if play_dir not in sys.path:
        sys.path.insert(0, play_dir)
    import time_tools

    return time_tools


def _catalog_tools():
    """The app-free catalog/op-log engine (scripts/play/catalog_tools.py)."""
    play_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "scripts", "play",
    )
    if play_dir not in sys.path:
        sys.path.insert(0, play_dir)
    import catalog_tools

    return catalog_tools


def _build_time_brief(story_id: str, branch_id: str) -> str:
    """距 deadline grounding + any fired-deadline forced event (peek, not cleared)."""
    try:
        tt = _time_tools()
        ts = tt.status(story_id, branch_id)
        unit = ts.get("unit", "天")
        ups = ts.get("upcoming") or []
        line = "；".join(
            f"距{u['label']} {u['in_n']} {unit}" + ("⚠️" if u.get("warn") else "") for u in ups
        ) if ups else "（無迫近事件）"
        brief = f"## 世界時間（GM 內部；閉關/趕路/副本消耗時間、逼近 deadline）\n當前：第 {ts.get('now', 0)} {unit}｜{line}"
        events = tt.peek_events(story_id, branch_id)
        if events:
            brief += ("\n**【強制事件：時間流逝中下列 deadline 已觸發，本回合必須演出、不得拖延】** "
                      + "；".join(f"{e.get('label')}（{e.get('on_fire', '')}）" for e in events))
        return brief
    except Exception:
        log.exception("gm_v2: time_brief build failed")
        return ""


def _default_llm(user_msg: str, system_prompt: str, recent: list) -> str:
    """Default brain = the configured GM provider (gemini per llm_config), non-stream."""
    from story_core.llm_bridge import call_claude_gm

    text, _ = call_claude_gm(user_msg, system_prompt, recent)
    return text


GM_FALLBACK_MODEL = "gemini-3.1-flash-lite"  # more available when flash-preview 503s
# function-calling needs a capable model: flash-lite under-calls tools beneath the heavy
# persona (verified — it narrates but skips advance_time/etc). FC default = flash-preview.
GM_TOOLS_MODEL = "gemini-3-flash-preview"
# 2-pass cheap transport: narrate + a FOCUSED record pass. flash-lite calls tools
# reliably on the focused recorder prompt (verified), so both passes use the abundant lite.
GM_RECORDER_MODEL = "gemini-3.1-flash-lite"


def _is_gm_error(raw: object) -> bool:
    """A failed GM response: empty, a provider error, or a safety block with no content."""
    if not raw or not str(raw).strip():
        return True
    s = str(raw).strip()
    return s.startswith("【系統錯誤】") or s.startswith("【系統提示】Gemini 安全")


def _fallback_llm(user_msg: str, system_prompt: str, recent: list) -> str:
    """Fallback GM = flash-lite (plainer prose, but available when flash-preview is overloaded)."""
    from story_core.gemini_bridge import call_gemini_gm
    from story_core.llm_bridge import _get_config

    g = _get_config().get("gemini", {})
    text, _ = call_gemini_gm(user_msg, system_prompt, recent, gemini_cfg=g, model=GM_FALLBACK_MODEL)
    return text


def _generate_gm(llm_fn, user_msg, system_prompt, recent) -> tuple[str | None, str]:
    """Primary GM → retry once → fallback flash-lite. Returns (raw, source) or (None, 'failed')."""
    raw = llm_fn(user_msg, system_prompt, recent)
    if not _is_gm_error(raw):
        return raw, "primary"
    log.warning("gm_v2: GM error (primary), retrying once")
    raw = llm_fn(user_msg, system_prompt, recent)
    if not _is_gm_error(raw):
        return raw, "primary_retry"
    log.warning("gm_v2: GM still erroring, falling back to %s", GM_FALLBACK_MODEL)
    raw = _fallback_llm(user_msg, system_prompt, recent)
    if not _is_gm_error(raw):
        return raw, "fallback"
    log.error("gm_v2: GM unavailable after retry + fallback")
    return None, "failed"


def _stream_gm(user_msg, system_prompt, recent, emit_chunk) -> tuple[str | None, str]:
    """串流式 PASS1 narrate（邊串流邊 buffer）：透過 provider-dispatched 串流 bridge 逐塊吐字，
    每塊呼叫 emit_chunk(delta) 給前端，同時累積完整文字。回 (raw, 'stream')。
    若【第一塊之前】就失敗（無任何 text）→ 回 (None, 'failed')，讓呼叫端退回阻塞式 _generate_gm
    （retry + flash-lite fallback 的可靠性安全網）。一旦開始順利吐字，途中錯誤就收下已串流的部分
    （503 在串流開始後極罕見；這是 user 選定的取捨）。"""
    from story_core.llm_bridge import call_claude_gm_stream
    buf: list[str] = []
    try:
        for ev, payload in call_claude_gm_stream(user_msg, system_prompt, recent):
            if ev == "text" and payload:
                buf.append(str(payload))
                try:
                    emit_chunk(str(payload))
                except Exception:
                    pass  # 前端斷線等：繼續 buffer，狀態仍會 commit
            elif ev == "error":
                if not buf:
                    return None, "failed"  # 開串流前就失敗 → 退回阻塞重試/fallback
                log.warning("gm_v2: stream errored mid-narration, keeping partial (%d chars)", sum(len(b) for b in buf))
                break  # 已串流部分 → 收下
    except Exception:
        log.exception("gm_v2: stream PASS1 raised")
        if not buf:
            return None, "failed"
    raw = "".join(buf)
    return (raw, "stream") if raw.strip() else (None, "failed")


def _load_persona(story_id: str) -> str:
    """Read the story system prompt, expanding `@include _shared/<file>` directives."""
    path = sio._story_system_prompt_path(story_id)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return ""
    design_dir = os.path.dirname(os.path.dirname(path))  # story_design/

    def _expand(match: re.Match) -> str:
        rel = match.group(1).strip()
        try:
            with open(os.path.join(design_dir, rel), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    return re.sub(r"@include\s+(\S+)", _expand, text)


def _roll(story_id: str, branch_id: str, state: dict, turn_count: int) -> dict:
    from story_core.dice import _OUTCOME_GM_HINTS, roll_fate

    r = roll_fate(state, turn_count=turn_count)
    return {
        "raw": r["raw"], "effective": r["effective"], "attr_bonus": r["attr_bonus"],
        "outcome": r["outcome"], "gm_hint": _OUTCOME_GM_HINTS.get(r["outcome"], ""), "turn": turn_count,
    }


def _dungeon_entry_hook(app, story_id, branch_id, old_state, new_state) -> None:
    """On 副本-entry (current_dungeon ''→X): fire the scenario architect off the critical
    path + charge the world-day cost upfront (副本＝主世界時間消費). Shared by both transports."""
    if not ((new_state.get("current_dungeon") or "").strip() and not (old_state.get("current_dungeon") or "").strip()):
        return
    try:
        from story_core.gm_v2 import scenario_gen
        scenario_gen.generate_blueprint_async(
            story_id, branch_id, (new_state.get("current_dungeon") or "").strip(),
            state_text=app._build_core_state_text(story_id, new_state),
        )
    except Exception:
        log.exception("gm_v2: scenario_gen dispatch failed")
    try:
        tt = _time_tools()
        cfg = tt.config(story_id)
        if (cfg.get("dungeon_time") or "charged") == "charged":
            cost = cfg.get("dungeon_time_default", 7)
            try:
                from story_core.gm_v2 import scenario_gen as _sg
                bp = _sg.load_blueprint(story_id, branch_id) or {}
            except Exception:
                bp = {}
            wd = bp.get("world_day_cost")
            tt.advance(story_id, branch_id, days=(wd if isinstance(wd, (int, float)) and not isinstance(wd, bool) else cost))
    except Exception:
        log.exception("gm_v2: dungeon time charge failed")


def _maybe_compact_recap(app, story_id, branch_id):
    """v2 parity with v1's post-turn recap compaction (routes.core_routes). Without this the
    v2 pipeline NEVER compacts → the rolling recap (the GM's long-term memory) freezes at the
    branch's seed recap; an old quest/thread eventually falls out of BOTH the recent-msg window
    AND the recap, so the GM can no longer narrate it（『玄風長老交辦的任務 GM 演不出來』bug）.
    Best-effort + async — a failure here must never break the already-committed turn."""
    try:
        from story_core import compaction
        tl = app.get_full_timeline(story_id, branch_id)
        recap = compaction.load_recap(story_id, branch_id)
        if compaction.should_compact(recap, len(tl)):
            compaction.compact_async(story_id, branch_id, tl)
    except Exception:
        pass


def _maybe_reconcile_quests(app, story_id, branch_id):
    """A2 背景任務對帳：每回合後，用 LLM 結掉劇情已明確達成的 active 任務（便宜 GM 常漏完成、或模糊
    佔位任務被取代卻孤懸）。Gated ≥2 active；async + best-effort；**絕不碰任務創建**（故無『新任務開
    不出來』FP）。對帳後同步快照，使自動結案扛得過本回合的 regen。"""
    try:
        from story_core.gm_v2 import apply as _apply
        quests = _apply._load_quests(story_id, branch_id)
        if sum(1 for q in quests if isinstance(q, dict) and q.get("status") == "active") < 2:
            return
        import threading

        def _run():
            try:
                if _apply.reconcile_quests(story_id, branch_id):
                    app._sync_latest_gm_snapshot(story_id, branch_id)
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass


# 偵測敘事跨進副本邊界用的標記（GM 被要求在開副本時印【試煉秘境開啟：X】，見 system_prompt）。
# 需有「：/:」分隔出 IP 名，避免誤吃【秘境結算】這種無冒號的系統訊息。
_DUNGEON_ENTRY_MARKER_RE = re.compile(r"【[^】]*?(?:試煉秘境|秘境|副本)[^】]*?[：:]\s*([^】]+?)\s*】")
_ENTRY_IP_REJECT = ("結算", "獎勵", "獲得", "點數", "完成", "通報", "預警", "結束")


def _extract_dungeon_entry_ip(narration: str):
    """從敘事裡的開副本標記（如【試煉秘境開啟：鬼滅之刃】）抽出 IP 名；沒有就回 None。"""
    if not narration:
        return None
    m = _DUNGEON_ENTRY_MARKER_RE.search(narration)
    if not m:
        return None
    ip = m.group(1).strip(" 　「」『』：:【】·・")
    if not (1 <= len(ip) <= 40):
        return None
    if any(bad in ip for bad in _ENTRY_IP_REJECT):
        return None
    return ip


def _maybe_suggest_dungeon_entry(app, story_id, branch_id, result):
    """B3 安全網：敘事跨進了副本（出現開副本標記），但記錄員沒觸發 `enter_dungeon`
    （current_dungeon 仍空）→ 在回應夾帶 `dungeon_entry_suggestion`，前端跳一顆「確認進入」
    讓玩家一鍵補登記，免得整個副本在帳外跑（沒劇本、沒獎勵、fork 後消失）。
    見 doc/v2_state_lifecycle.md §8（fork/漏記 bug 家族）。"""
    try:
        if not isinstance(result, dict) or result.get("error"):
            return
        state = app._load_character_state(story_id, branch_id)
        if (state.get("current_dungeon") or "").strip():
            return  # 引擎已登記進入，無需提示
        ip = _extract_dungeon_entry_ip(result.get("narration") or "")
        if ip:
            result["dungeon_entry_suggestion"] = {"ip": ip}
            log.info("gm_v2: 副本進入未登記，提示玩家確認 enter_dungeon(%r)", ip)
    except Exception:
        log.exception("gm_v2: dungeon-entry suggestion failed")


def run_turn(story_id, branch_id, player_action, *, llm_fn=None, dice=None, async_critic=True, async_identity=True, transport="delta", model=None, skip_user_message=False, replay_ops=None, emit_chunk=None, length=None) -> dict:
    """Run one v2 GM turn. Returns narration + turn metadata (incl. next_dice).

    transport="delta" (default): GM emits narration + a trailing DELTA block (parse→gate→apply).
    transport="tools": GM narrates + calls function-tools that validate+apply live (Phase 2).
    skip_user_message=True: the player message ALREADY exists in the timeline (regenerate —
    re-generate the GM reply only). Used by regenerate/edit v2 routing on a fresh fork.
    replay_ops: regenerate path — the UI ops the ORIGINAL GM reply acknowledged (pending_ops
    was already cleared by that turn and is not copied on fork, so the fresh fork is otherwise
    blind to them). When given, these are surfaced instead of reading the (empty) op-log."""
    app = _app()
    llm_fn = llm_fn or _default_llm

    # ---- PRE (server, no LLM): grounding + pre-rolled dice ----
    state = app._load_character_state(story_id, branch_id)
    npcs = app._load_npcs(story_id, branch_id, include_archived=True)
    schema = app._load_character_schema(story_id)
    timeline = app.get_full_timeline(story_id, branch_id)
    user_turns = sum(1 for m in timeline if m.get("role") == "user")
    if dice is None:
        dice = _roll(story_id, branch_id, state, user_turns + 1)

    persona = _load_persona(story_id)
    state_text = app._build_core_state_text(story_id, state)
    inventory = state.get("inventory") if isinstance(state.get("inventory"), dict) else None
    _catalog = sio._load_json(os.path.join(sio._story_dir(story_id), "catalog.json"), {})
    vendors = _catalog.get("vendors") if isinstance(_catalog, dict) else None
    catalog_items = _catalog.get("items") if isinstance(_catalog, dict) else None
    tree_progress = v2prompt.build_tree_progress_block(state, catalog_items or [])
    # _build_core_state_text only emits schema FIELDS + systems → it omits map/list fields the
    # GM still needs to see (天賦/已通關副本/長期記憶/非樹技能). Augment here (single point → flows to
    # every transport). Same grounding-blindness class as ops/persona. 功法樹節點已由 tree_progress
    # (id-keyed mastery) 注入，故 techniques 這裡只列「非樹」自由技能，避免重複。
    _tree_names = {str(it.get("name", "")).strip()
                   for it in (catalog_items or []) if isinstance(it, dict) and "tier" in it}
    _extras = []
    # 故事進度錨（有界，從精簡欄位渲染）：擋 GM 倒退重辦已完成的事（已是外門弟子卻又開入門考核）。
    _prog = v2prompt.build_progress_block(state)
    if _prog:
        _extras.append(_prog)
    # 時辰連續性：從上一則敘事推得目前時辰，要求只能往前——擋「晚上出門→中午回來」那種倒退/亂跳。
    _lastgm = next((m for m in reversed(timeline)
                    if m.get("role") == "gm" and (m.get("content") or "").strip()), None)
    _tod = _scene_time_of_day(_lastgm.get("content") if _lastgm else "")
    if _tod:
        _extras.append(
            "## 時辰連續性（務必遵守）\n目前時辰：" + _tod
            + "。時間【只能往前】推進（深夜→凌晨→清晨→上午→中午→下午→傍晚→夜），"
            "**絕不可倒退或亂跳時辰**——已是深夜／夜晚就不要突然變成中午／下午；"
            "除非玩家動作或劇情明確交代『過了很久／到了隔天』，否則沿用目前時辰，且同一段連續場景內時辰一致。")
    # 開副本＝從真實 IP 世界池選（GM 框架、玩家點）；只在主神空間/hub 顯示（副本中不需要）。
    _cur_dungeon = (state.get("current_dungeon") or "").strip()
    if not _cur_dungeon:
        try:
            _pool = sio._load_json(os.path.join(sio._story_dir(story_id), "dungeon_worlds.json"), {})
            _dpb = v2prompt.build_dungeon_pool_block(_pool.get("worlds") if isinstance(_pool, dict) else None)
            if _dpb:
                _extras.append(_dpb)
        except Exception:
            pass
    else:
        # 副本封閉原則：母世界（青雲宗等）不串門進副本，外來者一律副本當地（修『青雲宗串門』政策面）。
        _seal = v2prompt.build_dungeon_seal_block(_cur_dungeon)
        if _seal:
            _extras.append(_seal)
    # 道德基調（反臉譜化）逐回合重申，對抗『仙俠＝魔教反派』類型先驗（修『正邪對立太重』）。
    # 本世界給完整灰度；副本內只給 IP-中立提醒，不破壞世界觀隔離。
    _extras.append(v2prompt.build_moral_tone_block(bool(_cur_dungeon)))
    _tals = state.get("talents")
    if isinstance(_tals, dict) and _tals:
        _extras.append("天賦：" + "；".join(
            (f"{k}（{str(v)[:80]}）" if v else str(k)) for k, v in list(_tals.items())[:8]))
    _tech = state.get("techniques")
    if isinstance(_tech, dict) and _tech:
        _free_tech = [k for k in _tech.keys() if str(k).strip() not in _tree_names]
        if _free_tech:
            _extras.append("已習技能（非功法樹）：" + "、".join(str(k) for k in _free_tech[:12]))
    _ab = state.get("abilities")
    if isinstance(_ab, list) and _ab:
        _extras.append("功法與技能：" + "、".join(str(x) for x in _ab[:12]))
    _cd = state.get("completed_dungeons")
    if isinstance(_cd, list) and _cd:
        _extras.append("已通關副本：" + "、".join(str(x) for x in _cd[:12]))
    _anchors = state.get("story_anchors")
    if isinstance(_anchors, list) and _anchors:
        _extras.append("長期記憶：" + "；".join(str(x) for x in _anchors[:12]))
    if _extras:
        state_text = (state_text or "") + "\n" + "\n".join(_extras)
    quests_brief = v2prompt.build_quests_block(state)
    try:
        if replay_ops is not None:
            pending_ops = list(replay_ops)  # regenerate: replay the original turn's ops (fork has none)
        else:
            pending_ops = _catalog_tools().ops_pending(story_id, branch_id).get("ops", [])
        ops_brief = v2prompt.build_ops_block(pending_ops)
    except Exception:
        pending_ops, ops_brief = [], ""
    blueprint_brief = ""
    if (state.get("current_dungeon") or "").strip():
        from story_core.gm_v2 import scenario_gen
        blueprint_brief = scenario_gen.render_blueprint_brief(scenario_gen.load_blueprint(story_id, branch_id))
    time_brief = _build_time_brief(story_id, branch_id)
    try:
        _pre_event_n = len(_time_tools().peek_events(story_id, branch_id))  # cleared post-commit
    except Exception:
        _pre_event_n = 0
    system_prompt = v2prompt.build_system_prompt(
        persona, state_text, npcs, inventory=inventory, vendors=vendors,
        blueprint_brief=blueprint_brief, tree_progress=tree_progress, time_brief=time_brief,
        quests_brief=quests_brief, relationships=state.get("relationships"), ops_brief=ops_brief,
        current_world=((state.get("current_dungeon") or "").strip() or "本世界"),  # 同場過濾：副本內不餵母世界 NPC
    )

    try:
        recap = app.get_recap_text(story_id, branch_id) or ""
    except Exception:
        from story_core import compaction
        recap = compaction.get_recap_text(story_id, branch_id) or ""
    try:
        lore = app.search_relevant_lore(story_id, player_action) or ""
    except Exception:
        lore = ""
    # mark PRIOR player actions as already-executed history. The cheap narrate model treats a
    # past user instruction in `recent` as still-pending and RE-PERFORMS it (re-enacting 震碎鎖鏈
    # /「說自己學東西快」at the top of the new turn). Tagging them「已執行」separates done-history
    # from the one new action (which is in user_msg). All user msgs in `recent` are prior (the
    # current action isn't committed yet at this point).
    recent = []
    for m in timeline[-RECENT_LIMIT:]:
        c = m.get("content")
        if not c:
            continue
        role = m.get("role")
        if role == "user":
            c = "（玩家先前動作，已執行完畢、結果已寫進劇情，請勿再演一次）：" + c
        recent.append({"role": role, "content": c})
    # continuity anchor: the END of the last GM narration (markers stripped), placed right next to
    # the player action so the model continues FROM there instead of re-staging the just-played
    # beat. reversed→first gm: for a normal send that's the previous reply; for regen the trailing
    # (inherited) user msg is skipped → the gm before it, i.e. the scene the player acted from.
    _last_gm = next((m for m in reversed(timeline)
                     if m.get("role") == "gm" and (m.get("content") or "").strip()), None)
    _scene_tail = ""
    if _last_gm:
        _t = _last_gm["content"]
        for _mk in ("<!--ACTIONS-->", "<!--DELTA", "<!--STATE", "<!--NPC", "<!--TIME", "<!--LORE"):
            _i = _t.find(_mk)
            if _i >= 0:
                _t = _t[:_i]
        _t = _t.strip()
        _scene_tail = _t[-450:] if len(_t) > 450 else _t
    user_msg = v2prompt.build_user_message(
        player_action, lore=lore, recap=recap, dice_hint=dice.get("gm_hint", ""),
        scene_tail=_scene_tail, length=length,
    )

    def _ack_ops(gm_index=None):
        # clear the surfaced UI ops only AFTER a successful turn (a failed/503 turn keeps
        # them so the retry's GM still sees the player's purchases). Snapshot them onto the
        # committed GM reply first, so a later regenerate of this reply can replay them
        # (pending_ops is cleared here and not copied on fork → regen would be blind).
        if ops_brief:
            try:
                _catalog_tools().save_turn_ops(story_id, branch_id, gm_index, pending_ops)
                _catalog_tools().ops_ack(story_id, branch_id)
            except Exception:
                pass
        # the player ops are now baked into this committed turn → drop the per-op undo stack
        # (past ops are rewound via the timeline fork, not this between-turns panel)
        try:
            _catalog_tools().clear_undo(story_id, branch_id)
        except Exception:
            pass

    # ---- transport=tools: 2-pass cheap (narrate + background record) ----
    if transport == "tools":
        result = _run_turn_tools(
            app, story_id, branch_id, player_action, state, npcs, schema, dice,
            persona, state_text, inventory, vendors, tree_progress, blueprint_brief,
            time_brief, recent, user_msg, user_turns, _pre_event_n, async_identity, model, llm_fn,
            quests_brief=quests_brief, skip_user_message=skip_user_message, ops_brief=ops_brief,
            emit_chunk=emit_chunk,
        )
        if isinstance(result, dict) and not result.get("error"):
            _ack_ops(result.get("gm_index"))
            _maybe_compact_recap(app, story_id, branch_id)
            _maybe_reconcile_quests(app, story_id, branch_id)
            _maybe_suggest_dungeon_entry(app, story_id, branch_id, result)
        return result
    # ---- transport=tools-inline: single capable-model FC (narrate+tools in one pass) ----
    if transport == "tools-inline":
        result = _run_turn_tools_inline(
            app, story_id, branch_id, player_action, state, npcs, schema, dice,
            persona, state_text, inventory, vendors, tree_progress, blueprint_brief,
            time_brief, recent, user_msg, user_turns, _pre_event_n, async_identity, model,
            quests_brief=quests_brief, skip_user_message=skip_user_message, ops_brief=ops_brief,
        )
        if isinstance(result, dict) and not result.get("error"):
            _ack_ops(result.get("gm_index"))
            _maybe_compact_recap(app, story_id, branch_id)
            _maybe_reconcile_quests(app, story_id, branch_id)
            _maybe_suggest_dungeon_entry(app, story_id, branch_id, result)
        return result

    # ---- GENERATE (one LLM call, with retry + flash-lite fallback) ----
    raw, gm_source = _generate_gm(llm_fn, user_msg, system_prompt, recent)
    if raw is None:
        # Provider unavailable after retry + fallback — commit NOTHING, surface an error.
        return {
            "error": "GM provider unavailable (retry + fallback both failed)",
            "narration": None, "gm_source": "failed", "dice": dice, "next_dice": None,
            "apply": {}, "identity": None, "dropped": [], "flagged": [],
            "parse_note": None, "user_index": None, "gm_index": None,
        }

    # ---- PARSE -> GATE ----
    narration, delta, note = parse_turn_output(raw)
    gated = gate_delta(delta, schema)

    # ---- APPLY + commit (player msg, then delta, then gm msg w/ post-apply snapshots) ----
    base = app._next_timeline_index(story_id, branch_id)  # full timeline → correct on fresh forks (regen/edit)
    gm_idx = base if skip_user_message else base + 1       # regen: user already in timeline → GM-only
    if not skip_user_message:
        sio._upsert_branch_message(story_id, branch_id, {
            "role": "user", "content": player_action, "index": base, "dice": dice,
        })
    apply_summary = apply_clean_delta(story_id, branch_id, gated["clean"], msg_index=gm_idx)

    new_state = app._load_character_state(story_id, branch_id)
    _dungeon_entry_hook(app, story_id, branch_id, state, new_state)
    snapshots = {
        "state_snapshot": new_state,
        "npcs_snapshot": app._load_npcs(story_id, branch_id, include_archived=True),
        "world_day_snapshot": app.get_world_day(story_id, branch_id),
        "dungeon_progress_snapshot": app.get_dungeon_progress_snapshot(story_id, branch_id),
    }
    sio._upsert_branch_message(story_id, branch_id, {
        "role": "gm", "content": narration, "index": gm_idx, **snapshots,
    })
    if _pre_event_n:  # clear only the deadline events surfaced THIS turn (apply may have fired new ones)
        try:
            _time_tools().clear_events(story_id, branch_id, count=_pre_event_n)
        except Exception:
            log.exception("gm_v2: clear_events failed")

    # ---- pre-roll next turn's die (pipeline) ----
    next_dice = _roll(story_id, branch_id, new_state, user_turns + 2)

    # ---- CRITIC (off the critical path) ----
    if gated["flagged"]:
        if async_critic:
            v2critic.review_flagged_async(story_id, branch_id, narration, gated["flagged"], delta)
        else:
            v2critic.review_flagged(story_id, branch_id, narration, gated["flagged"], delta)

    # ---- IDENTITY resolution (Tier-3, off the critical path) ----
    # NPC + inventory variants; pre-filter + cache keep judge calls rare.
    identity_result = None
    sd = gated["clean"].get("state_delta") or {}
    npc_changed = bool(apply_summary.get("npcs"))
    inv_changed = "inventory" in sd or "inventory_add" in sd
    if npc_changed or inv_changed:
        from story_core.gm_v2 import identity as v2id
        if async_identity:
            if npc_changed:
                v2id.resolve_identities_async(story_id, branch_id)
            if inv_changed:
                v2id.resolve_inventory_identities_async(story_id, branch_id)
        else:
            identity_result = {
                "npc": v2id.resolve_identities(story_id, branch_id) if npc_changed else None,
                "inventory": v2id.resolve_inventory_identities(story_id, branch_id) if inv_changed else None,
            }

    _ack_ops(gm_idx)  # delta turn committed → snapshot ops onto the reply, then clear
    _maybe_compact_recap(app, story_id, branch_id)
    _maybe_reconcile_quests(app, story_id, branch_id)
    result = {
        "narration": narration,
        "parse_note": note,
        "gm_source": gm_source,
        "dropped": gated["dropped"],
        "flagged": gated["flagged"],
        "apply": apply_summary,
        "identity": identity_result,
        "dice": dice,
        "next_dice": next_dice,
        "user_index": None if skip_user_message else base,
        "gm_index": gm_idx,
    }
    _maybe_suggest_dungeon_entry(app, story_id, branch_id, result)
    return result


def _build_recorder_prompt(schema: dict, grounding: str) -> str:
    """Focused recorder system prompt: the instruction + the schema's real field names
    (so the model writes `fate_points`, not the 中文 label) + the current grounding."""
    from story_core.gm_v2.contract import RECORDER_INSTRUCTION
    keys = [f.get("key") for f in (schema.get("fields") or []) if f.get("key")]
    keys += [ld.get("key") for ld in (schema.get("lists") or []) if ld.get("key")]
    field_hint = "、".join(sorted({k for k in keys if k}))
    return RECORDER_INSTRUCTION + "\n\n可用 state 欄位名：" + field_hint + "\n\n" + grounding


def record_turn(app, story_id, branch_id, narration, schema, recorder_sys, msg_index,
                old_state, pre_event_n, model=None) -> list:
    """PASS 2 (background): focused recorder reads the committed narration and calls tools
    to persist durable changes (multi-round FC loop, gate-guarded). Then runs the shared
    post-turn hooks (dungeon entry, re-snapshot the gm msg, clear events, identity). Cheap
    flash-lite calls tools reliably here because the task is FOCUSED. Idempotent (dedup +
    time double-spend guard) + a reconciliation round to catch misses. Background = cheap,
    so we DON'T pre-filter (skipping risks a silent miss; an empty record call is harmless)."""
    if not (narration or "").strip():
        return []
    try:
        import json as _json
        from story_core.gemini_bridge import call_gemini_gm_tools
        from story_core.gm_v2.toolexec import execute_tool, tool_declarations
        from story_core.llm_bridge import _get_config
        g = _get_config().get("gemini", {})
        rmodel = model or GM_RECORDER_MODEL
        ctx = {"msg_index": msg_index}
        _seen: set = set()       # idempotency: never double-apply the same (tool, args)
        _time_done = {"v": False}  # B: time advances once/turn (cultivate OR advance_time)

        def on_tool(n, a):
            key = (n, _json.dumps(a, sort_keys=True, ensure_ascii=False, default=str))
            if key in _seen:
                return {"ok": True, "tool": n, "note": "本回合已記錄，略過重複"}
            if n == "advance_time" and _time_done["v"]:
                return {"ok": True, "tool": n, "note": "本回合時間已計（閉關/前一動作），略過避免雙扣"}
            _seen.add(key)
            res = execute_tool(story_id, branch_id, n, a, schema=schema, ctx=ctx)
            if isinstance(res, dict) and res.get("ok") and n in ("advance_time", "cultivate"):
                _time_done["v"] = True
            return res

        _, executed, _ = call_gemini_gm_tools(
            "【本回合敘事】\n" + narration, recorder_sys, [], g,
            model=rmodel, on_tool=on_tool, tool_decls=tool_declarations(), max_rounds=4,
        )
        # ② reconciliation: one focused verify round (dedup+guard ⇒ no re-apply). Beyond "did I
        # miss a change THIS turn", it also does a POSSESSION-CONSISTENCY check: a 功法/道具 the
        # protagonist USES in the narration but that isn't in the ledger means it was learned
        # earlier and never recorded (the 太上導引調息功/五雷鎮屍符 miss class — gradual learning has
        # no clean acquire-beat, so it only ever surfaces as "used"). Conservative + gated + idempotent.
        done = "、".join(sorted({e["name"] for e in executed
                                 if isinstance(e.get("result"), dict) and e["result"].get("ok")})) or "（無）"
        verify_sys = (recorder_sys + "\n\n【補漏與一致性檢查】本回合已記錄的工具：" + done +
                      "。請重讀敘事，做兩件事：\n"
                      "1. 補上本回合『仍未記錄』的永久變化（呼叫工具）。\n"
                      "2. 【功法／道具一致性】敘事中主角**施展、運轉或明確持有**的功法／法寶／符籙，"
                      "是否都已在上方 grounding 的功法／道具清單裡？若有『用到卻不在清單上』的"
                      "（例：施展某功法、動用某法寶，清單卻沒有）——代表它先前漏記了，**用 set_state "
                      "補進 `techniques`／`inventory`**（成／狀態照敘事描述，拿不準給保守值如「入門」）。"
                      "這專治『早就學會、近期只是使用、卻從沒入帳』的漏記。\n"
                      "鐵則：只補**敘事白紙黑字、且確實屬於主角**的（NPC／對手的功法法寶不可記到主角頭上）；"
                      "拿不準就不要動（漏記可日後再補，亂記會污染狀態）。已記齊就完全不要呼叫任何工具。")
        _, executed2, _ = call_gemini_gm_tools(
            "【本回合敘事】\n" + narration, verify_sys, [], g,
            model=rmodel, on_tool=on_tool, tool_decls=tool_declarations(), max_rounds=3,
        )
        executed += executed2
    except Exception:
        log.exception("gm_v2 record_turn: record pass failed")
        return []

    new_state = app._load_character_state(story_id, branch_id)
    _dungeon_entry_hook(app, story_id, branch_id, old_state, new_state)
    new_state = app._load_character_state(story_id, branch_id)  # hook may advance time
    try:  # re-snapshot the gm msg so the power panel reflects post-record state
        sio._upsert_branch_message(story_id, branch_id, {
            "role": "gm", "index": msg_index, "content": narration,
            "state_snapshot": new_state,
            "npcs_snapshot": app._load_npcs(story_id, branch_id, include_archived=True),
            "world_day_snapshot": app.get_world_day(story_id, branch_id),
            "dungeon_progress_snapshot": app.get_dungeon_progress_snapshot(story_id, branch_id),
        })
    except Exception:
        log.exception("gm_v2 record_turn: re-snapshot failed")
    if pre_event_n:
        try:
            _time_tools().clear_events(story_id, branch_id, count=pre_event_n)
        except Exception:
            log.exception("gm_v2 record_turn: clear_events failed")

    ok = [e for e in executed if isinstance(e.get("result"), dict) and e["result"].get("ok")]
    if ok:
        try:
            from story_core.gm_v2 import identity as v2id
            if any(e["name"] == "upsert_npc" for e in ok):
                v2id.resolve_identities_async(story_id, branch_id)
                from story_core.gm_v2 import leveljudge as v2lj  # 賦級：補戰鬥 NPC 的 級/domain
                v2lj.resolve_levels_async(story_id, branch_id)
            if any(e["name"] == "set_state" and "inventory" in str(e.get("args", "")) for e in ok):
                v2id.resolve_inventory_identities_async(story_id, branch_id)
        except Exception:
            log.exception("gm_v2 record_turn: identity/level dispatch failed")
    return executed


def _run_turn_tools(app, story_id, branch_id, player_action, state, npcs, schema, dice,
                    persona, state_text, inventory, vendors, tree_progress, blueprint_brief,
                    time_brief, recent, user_msg, user_turns, pre_event_n, async_identity, model=None, llm_fn=None,
                    quests_brief="", skip_user_message=False, ops_brief="", emit_chunk=None) -> dict:
    """2-pass cheap transport. PASS 1: narrate only (sync, flash-lite, no tools) → commit +
    return FAST. PASS 2: record (BACKGROUND, flash-lite, focused recorder + tools + grounding)
    persists durable changes — multi-round, gate-guarded, pre-filtered. Player never waits on
    PASS 2; state is eventually-consistent (narration is the ground truth it re-derives from)."""
    from story_core.gm_v2.contract import NARRATE_CONTRACT

    grounding = v2prompt.build_grounding_block(
        state_text, npcs, inventory=inventory, vendors=vendors,
        blueprint_brief=blueprint_brief, tree_progress=tree_progress, time_brief=time_brief,
        quests_brief=quests_brief, relationships=state.get("relationships"), ops_brief=ops_brief,
    )
    narrate_sys = "\n\n".join([(persona or "").strip(), grounding, NARRATE_CONTRACT])

    # ---- PASS 1: narrate ----
    # 邊串流邊 buffer：emit_chunk 給定且走預設 provider → 串流逐字吐給前端，同時累積完整文字。
    # 只有「第一塊之前」就失敗才退回阻塞式 _generate_gm（retry + flash-lite fallback 安全網）。
    raw, gm_source = None, "failed"
    # stream only on the default provider path (run_turn normalises llm_fn → _default_llm; a custom
    # llm_fn means a test/caller override → use it blocking, don't stream the wrong brain).
    if emit_chunk is not None and llm_fn in (None, _default_llm):
        raw, gm_source = _stream_gm(user_msg, narrate_sys, recent, emit_chunk)
    if raw is None:
        raw, gm_source = _generate_gm(llm_fn or _default_llm, user_msg, narrate_sys, recent)
    if raw is None:
        return {"error": "GM provider unavailable (retry + fallback both failed)",
                "narration": None, "gm_source": "failed", "dice": dice, "next_dice": None,
                "user_index": None, "gm_index": None}
    narration, _delta, _note = parse_turn_output(raw)
    narration = (narration or raw or "").strip()

    base = app._next_timeline_index(story_id, branch_id)  # full timeline → correct on fresh forks (regen/edit)
    gm_idx = base if skip_user_message else base + 1       # regen: user already in timeline → GM-only
    if not skip_user_message:
        sio._upsert_branch_message(story_id, branch_id, {"role": "user", "content": player_action, "index": base, "dice": dice})
    snapshots = {
        "state_snapshot": app._load_character_state(story_id, branch_id),
        "npcs_snapshot": app._load_npcs(story_id, branch_id, include_archived=True),
        "world_day_snapshot": app.get_world_day(story_id, branch_id),
        "dungeon_progress_snapshot": app.get_dungeon_progress_snapshot(story_id, branch_id),
    }
    sio._upsert_branch_message(story_id, branch_id, {"role": "gm", "content": narration, "index": gm_idx, **snapshots})
    next_dice = _roll(story_id, branch_id, state, user_turns + 2)

    # ---- PASS 2: record (background — player already has the story) ----
    recorder_sys = _build_recorder_prompt(schema, grounding)
    threading.Thread(
        target=record_turn,
        args=(app, story_id, branch_id, narration, schema, recorder_sys, gm_idx, state, pre_event_n, model),
        daemon=True,
    ).start()

    return {"narration": narration, "gm_source": gm_source or "tools-2pass", "dice": dice,
            "next_dice": next_dice, "user_index": None if skip_user_message else base,
            "gm_index": gm_idx, "recording": "background"}


def _run_turn_tools_inline(app, story_id, branch_id, player_action, state, npcs, schema, dice,
                           persona, state_text, inventory, vendors, tree_progress, blueprint_brief,
                           time_brief, recent, user_msg, user_turns, pre_event_n, async_identity, model=None,
                           quests_brief="", skip_user_message=False, ops_brief="") -> dict:
    """transport=tools-inline: ONE capable-model (flash-preview) call narrates AND calls tools
    live (the GM decides changes as it acts). Higher quality + simpler than 2-pass, but spends
    the scarce preview quota. Same idempotency (dedup) + time double-spend guard as the recorder."""
    import json as _json
    from story_core.gemini_bridge import call_gemini_gm_tools
    from story_core.gm_v2.contract import TOOLS_CONTRACT
    from story_core.gm_v2.toolexec import execute_tool, tool_declarations
    from story_core.llm_bridge import _get_config

    grounding = v2prompt.build_grounding_block(
        state_text, npcs, inventory=inventory, vendors=vendors,
        blueprint_brief=blueprint_brief, tree_progress=tree_progress, time_brief=time_brief,
        quests_brief=quests_brief, relationships=state.get("relationships"), ops_brief=ops_brief,
    )
    system_prompt = "\n\n".join([(persona or "").strip(), grounding, TOOLS_CONTRACT])
    g = _get_config().get("gemini", {})

    base = app._next_timeline_index(story_id, branch_id)  # full timeline → correct on fresh forks (regen/edit)
    gm_idx = base if skip_user_message else base + 1       # regen: user already in timeline → GM-only
    ctx = {"msg_index": gm_idx}
    _seen: set = set()
    _time_done = {"v": False}

    def on_tool(n, a):
        key = (n, _json.dumps(a, sort_keys=True, ensure_ascii=False, default=str))
        if key in _seen:
            return {"ok": True, "tool": n, "note": "已記錄，略過重複"}
        if n == "advance_time" and _time_done["v"]:
            return {"ok": True, "tool": n, "note": "本回合時間已計，略過避免雙扣"}
        _seen.add(key)
        res = execute_tool(story_id, branch_id, n, a, schema=schema, ctx=ctx)
        if isinstance(res, dict) and res.get("ok") and n in ("advance_time", "cultivate"):
            _time_done["v"] = True
        return res

    narration, executed, src = call_gemini_gm_tools(
        user_msg, system_prompt, recent, g, model=model or GM_TOOLS_MODEL,
        on_tool=on_tool, tool_decls=tool_declarations(),
    )
    if src == "error" or not (narration or "").strip():
        return {"error": narration or "GM tools-inline turn failed", "narration": None,
                "gm_source": "tools_inline_failed", "dice": dice, "next_dice": None,
                "user_index": None, "gm_index": None}

    if not skip_user_message:
        sio._upsert_branch_message(story_id, branch_id, {"role": "user", "content": player_action, "index": base, "dice": dice})
    new_state = app._load_character_state(story_id, branch_id)
    _dungeon_entry_hook(app, story_id, branch_id, state, new_state)
    new_state = app._load_character_state(story_id, branch_id)
    sio._upsert_branch_message(story_id, branch_id, {
        "role": "gm", "content": narration, "index": gm_idx,
        "state_snapshot": new_state,
        "npcs_snapshot": app._load_npcs(story_id, branch_id, include_archived=True),
        "world_day_snapshot": app.get_world_day(story_id, branch_id),
        "dungeon_progress_snapshot": app.get_dungeon_progress_snapshot(story_id, branch_id),
    })
    if pre_event_n:
        try:
            _time_tools().clear_events(story_id, branch_id, count=pre_event_n)
        except Exception:
            log.exception("gm_v2: clear_events failed")
    next_dice = _roll(story_id, branch_id, new_state, user_turns + 2)

    ok = [e for e in executed if isinstance(e.get("result"), dict) and e["result"].get("ok")]
    if async_identity and ok:
        try:
            from story_core.gm_v2 import identity as v2id
            if any(e["name"] == "upsert_npc" for e in ok):
                v2id.resolve_identities_async(story_id, branch_id)
                from story_core.gm_v2 import leveljudge as v2lj
                v2lj.resolve_levels_async(story_id, branch_id)
            if any(e["name"] == "set_state" and "inventory" in str(e.get("args", "")) for e in ok):
                v2id.resolve_inventory_identities_async(story_id, branch_id)
        except Exception:
            log.exception("gm_v2: identity/level dispatch failed")
    return {"narration": narration, "gm_source": "tools-inline",
            "applied": [{"tool": e["name"], "ok": isinstance(e.get("result"), dict) and e["result"].get("ok")} for e in executed],
            "dice": dice, "next_dice": next_dice,
            "user_index": None if skip_user_message else base, "gm_index": gm_idx}


__all__ = ["run_turn"]
