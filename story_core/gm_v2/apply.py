"""GM v2 applier — persist gate's `clean` delta via PRODUCTION persistence,
bypassing the lossy guards (方案甲).

Design (locked):
- state_delta -> `_apply_state_update_inner` (thin apply + state.db sync) + dungeon
  reconcile, but NOT the `_run_state_gate` / `_sanitize_async_state_update` /
  `_normalize_state_async` stack — those lossy guards are bug #1's proximate cause.
- npcs -> explicit-lifecycle save: the GM's {active|offstage|terminal} is translated
  to the production {lifecycle_status, archive_kind} representation, with NO
  keyword-sniff archive/auto-reactivation (bug #3) and an append-only `memory[]`
  (bug #4). Reuses production identity-resolution / provenance / state.db sync.
- time -> advance_world_day.
- lore -> NOT persisted here; deferred to the async critic (bug #2: lore correctness
  must be reviewed before it enters the KB).

This module is app-coupled at runtime (it writes real app state); only parse.py /
gate.py stay app-free.
"""

import copy
import logging
import re

from story_core.dungeon_return_memory import apply_npc_provenance_defaults
from story_core.npc_helpers import (
    _load_npcs,
    _normalize_npc_tier,
    _resolve_npc_identity,
    _sync_state_db_npc_entry,
)
from story_core.story_io import (
    _load_json,
    _save_json,
    _story_character_state_path,
    _story_npcs_path,
)

log = logging.getLogger("rpg")

MEMORY_HOT_CAP = 30  # injected/hot memory cap; overflow goes to cold memory_archive (never deleted)

# GM vocab {active|offstage|terminal} -> production (lifecycle_status, archive_kind)
_GM_LIFECYCLE_MAP = {
    "active": ("active", None),
    "offstage": ("archived", "offstage"),
    "terminal": ("archived", "terminal"),
}


def _app():
    import app as app_module

    return app_module


def _catalog_tools():
    """The app-free catalog engine (scripts/play/catalog_tools.py), shared with the
    Claude harness — same engine, different brain."""
    import os
    import sys

    play_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "scripts", "play",
    )
    if play_dir not in sys.path:
        sys.path.insert(0, play_dir)
    import catalog_tools

    return catalog_tools


_MILESTONE_FIELDS = (("sect_rank", "晉身"), ("major_realm", "突破至"))


def _add_milestone(state: dict, tag: str) -> bool:
    """Append a progression milestone (short tag) — deduped + capped, so completed_milestones
    stays small (bounded grounding, not an accumulating log). Returns True if it changed."""
    tag = str(tag or "").strip()[:60]
    if not tag:
        return False
    cm = state.get("completed_milestones")
    cm = cm if isinstance(cm, list) else []
    if tag in cm:
        return False
    cm.append(tag)
    state["completed_milestones"] = cm[-15:]
    return True


def _detect_milestones(story_id: str, branch_id: str, old_state: dict, new_state: dict) -> None:
    """Deterministic milestone capture (NO LLM): a sect_rank / major_realm transition is a durable
    progression fact (晉身、突破) the GM must never re-stage — record it so the progress block
    surfaces it. This is the reliable replacement for hoping the GM calls `remember`."""
    tags = []
    for key, label in _MILESTONE_FIELDS:
        ov = str((old_state or {}).get(key) or "").strip()
        nv = str((new_state or {}).get(key) or "").strip()
        if nv and nv != ov:
            tags.append(f"{label}{nv}")
    if not tags:
        return
    path = _story_character_state_path(story_id, branch_id)
    st = _load_json(path, {})
    changed = False
    for t in tags:
        if _add_milestone(st, t):
            changed = True
    if changed:
        _save_json(path, st)


def apply_state_delta(story_id: str, branch_id: str, state_delta: dict) -> None:
    """Thin apply + state.db sync + dungeon reconcile, WITHOUT the lossy guards."""
    if not state_delta:
        return
    app = _app()
    with app._get_character_state_lock(story_id, branch_id):
        schema = app._load_character_schema(story_id)
        old_state = copy.deepcopy(app._load_character_state(story_id, branch_id))
        # deepcopy: _apply_state_update_inner mutates the update dict in place.
        app._apply_state_update_inner(story_id, branch_id, copy.deepcopy(state_delta), schema)
        new_state = app._load_character_state(story_id, branch_id)
        app.reconcile_dungeon_entry(story_id, branch_id, old_state, new_state)
        app.validate_dungeon_progression(story_id, branch_id, new_state, old_state)
        app.reconcile_dungeon_exit(story_id, branch_id, old_state, new_state)
        _detect_milestones(story_id, branch_id, old_state, app._load_character_state(story_id, branch_id))


def _save_npc_explicit(story_id, branch_id, npc_data, memory_add, msg_index, run_ctx):
    """Save one NPC with EXPLICIT lifecycle only (no keyword sniff / reactivation)."""
    npcs = _load_npcs(story_id, branch_id, include_archived=True)
    name = str(npc_data.get("name", "")).strip()
    if not name:
        return

    matched = _resolve_npc_identity(name, npcs)
    if matched and matched != name:
        name = matched
        npc_data["name"] = matched

    if "tier" in npc_data:
        tier = _normalize_npc_tier(npc_data.get("tier"))
        if tier:
            npc_data["tier"] = tier
        else:
            npc_data.pop("tier", None)

    existing_index, existing = None, None
    for i, entry in enumerate(npcs):
        if entry.get("name") == name:
            existing_index, existing = i, entry
            break

    npc_data = apply_npc_provenance_defaults(story_id, branch_id, npc_data, existing)
    if msg_index is not None:
        try:
            npc_data["last_seen_msg_index"] = int(msg_index)
        except (TypeError, ValueError):
            pass
    if run_ctx:
        if run_ctx.get("dungeon_id") and not (existing or {}).get("origin_dungeon_id"):
            npc_data["origin_dungeon_id"] = run_ctx["dungeon_id"]
        if run_ctx.get("run_id") and not (existing or {}).get("origin_run_id"):
            npc_data["origin_run_id"] = run_ctx["run_id"]

    if "id" not in npc_data:
        npc_data["id"] = (existing or {}).get("id") or "npc_" + re.sub(r"\W+", "", name)[:20]

    # durable memory: hot tier (memory[], capped, injected) + cold archive (never deleted).
    # Overflow oldest-first into memory_archive so consolidation/summary can never lose
    # ground truth — a wrong summary is recoverable by re-reading the archive.
    if memory_add is not None:
        mem = list((existing or {}).get("memory", []))
        archive = list((existing or {}).get("memory_archive", []))
        for raw in (memory_add if isinstance(memory_add, list) else [memory_add]):
            text = str(raw).strip()
            if text and text not in mem and text not in archive:  # dedup across both tiers
                mem.append(text)
        if len(mem) > MEMORY_HOT_CAP:
            overflow = mem[: len(mem) - MEMORY_HOT_CAP]
            mem = mem[len(mem) - MEMORY_HOT_CAP :]
            archive.extend(overflow)
        npc_data["memory"] = mem
        if archive:
            npc_data["memory_archive"] = archive

    if existing_index is not None:
        merged = {**existing, **npc_data}
        npcs[existing_index] = merged
        _save_json(_story_npcs_path(story_id, branch_id), npcs)
        _sync_state_db_npc_entry(story_id, branch_id, merged)
    else:
        npcs.append(npc_data)
        _save_json(_story_npcs_path(story_id, branch_id), npcs)
        _sync_state_db_npc_entry(story_id, branch_id, npc_data)


def apply_npcs(story_id, branch_id, npcs, msg_index=None, run_ctx=None) -> list[str]:
    """Apply NPC updates with explicit lifecycle translation. Returns saved names."""
    try:  # realm-anchored 級 enforcement (see below) — load the ladder once
        from story_core.gm_v2.leveljudge import _realm_anchored_level, _realm_ladder
        _ladder = _realm_ladder(story_id)
    except Exception:
        _realm_anchored_level, _ladder = None, []
    try:  # presence (在場) 軸：NPC 此刻所在世界＝主角寫入它時所在的世界（副本名 or 本世界）。
        _cur_world = (_app()._load_character_state(story_id, branch_id).get("current_dungeon") or "").strip() or "本世界"
    except Exception:
        _cur_world = "本世界"
    saved: list[str] = []
    for raw in npcs or []:
        if not isinstance(raw, dict):
            continue
        npc = dict(raw)
        name = str(npc.get("name", "")).strip()
        if not name:
            continue
        # 級 = 大境界×10 + 層（練氣一層→1、築基初期→11…）；大境界 = 級//10。層在大境界【內】細分，
        # 不會把 NPC 推到上一個大境界（_realm_anchored_level 有明確層→pin，無→把現值夾進該大境界 10 格）。
        # Enforce at the write chokepoint: any ladder NPC's 級 is corrected here whatever the source
        # (擋 LLM/recorder 把練氣怪灌成築基級). Non-ladder worlds (鬼滅/火影/殭屍先生) → None → keep given.
        if _realm_anchored_level is not None:
            _anc = _realm_anchored_level(npc, _ladder)
            if _anc is not None and npc.get("level") != _anc:
                npc["level"] = _anc
        # presence：蓋上「此刻所在世界」＝主角當前世界。GM 把母世界隊友寫進副本場景 → 自動標成在副本
        # （hybrid 帶人）；母世界沒被寫到的 NPC 留著舊 present_world → 不在副本場、grounding 不餵（不串門）。
        npc["present_world"] = _cur_world
        gm_lifecycle = npc.pop("lifecycle_status", None)
        memory_add = npc.pop("memory_add", None)
        if gm_lifecycle in _GM_LIFECYCLE_MAP:
            lifecycle_status, archive_kind = _GM_LIFECYCLE_MAP[gm_lifecycle]
            npc["lifecycle_status"] = lifecycle_status
            if lifecycle_status == "archived":
                npc["archive_kind"] = archive_kind
                npc.setdefault("archived_reason", f"gm:{gm_lifecycle}")
            else:
                npc["archive_kind"] = None
                npc["archived_reason"] = None
        _save_npc_explicit(story_id, branch_id, npc, memory_add, msg_index, run_ctx)
        saved.append(name)
    return saved


def advance_time(story_id: str, branch_id: str, time_dict: dict) -> dict:
    """General time passage (劇情時間流逝). Routed through the time economy so it
    advances the SAME clock AND fires deadlines (魔潮) just like cultivation —
    otherwise a `time:{days:90}` jump would silently skip a 魔潮."""
    if not isinstance(time_dict, dict):
        return {}
    # 副本內：凍結主世界時鐘。副本是另一個世界，其內部時間只走敘事（時辰由 grounding 維持）；
    # 進入副本時已收固定的世界時間成本（dungeon_time_default），這裡若再分時推進就會雙扣，
    # 且把主世界時鐘的小數誤當成副本時辰（深夜打殭屍卻顯示 0.46≈中午 的根源）。
    try:
        if (_app()._load_character_state(story_id, branch_id).get("current_dungeon") or "").strip():
            return {"skipped": "in_dungeon"}
    except Exception:
        pass
    try:
        days = float(time_dict.get("days") or 0)
        hours = float(time_dict.get("hours") or 0)
    except (TypeError, ValueError):
        return {}
    total = min(days + hours / 24, 365)  # generous cap; guards GM typos (e.g. days:99999)
    if total <= 0:
        return {}
    try:
        return _time_tools().spend(story_id, branch_id, total)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("gm_v2 advance_time: time engine unavailable (%s); raw advance", exc)
        _app().advance_world_day(story_id, branch_id, total)
        return {"now": _app().get_world_day(story_id, branch_id), "spent": total, "fired": []}


def apply_catalog_upsert(story_id: str, items: list) -> list:
    """Stock new catalog items the GM declared (story-level, persistent). This only
    adds AVAILABILITY — actual buy/sell is player-side UI. Validated in
    catalog_tools.upsert (bad/unknown-vendor items are rejected, not written)."""
    done: list = []
    if not items:
        return done
    try:
        ct = _catalog_tools()
    except Exception as exc:
        log.warning("gm_v2 catalog_upsert: engine unavailable (%s)", exc)
        return done
    for it in items:
        if not isinstance(it, dict):
            continue
        vendor = str(it.get("vendor", "")).strip()
        if not vendor:
            continue
        try:
            res = ct.upsert(story_id, vendor, it)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("gm_v2 catalog_upsert failed for %s: %s", it.get("id"), exc)
            continue
        if res.get("ok"):
            done.append({"id": res.get("id"), "vendor": vendor, "action": res.get("action")})
        else:
            log.warning("gm_v2 catalog_upsert rejected: %s", {k: res.get(k) for k in ("error", "validation")})
    return done


def _time_tools():
    """The app-free time-economy engine (scripts/play/time_tools.py)."""
    import os
    import sys

    play_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "scripts", "play",
    )
    if play_dir not in sys.path:
        sys.path.insert(0, play_dir)
    import time_tools

    return time_tools


def apply_cultivate(story_id: str, branch_id: str, items: list) -> list:
    """閉關 on a 功法 node — spends CALENDAR TIME and lets the ENGINE promote 成 when
    the accumulated days cross the threshold (天賦＝更少天數＝天才). The GM narrates
    the 閉關 (optionally `days`); it does NOT set the 成 level directly — that removes
    the 「GM 心證」 and ties cultivation to the time budget (魔潮 deadline). A long 閉關
    can even cross a deadline mid-seclusion — surfaced via `fired`."""
    done: list = []
    if not items:
        return done
    try:
        tt = _time_tools()
    except Exception as exc:
        log.warning("gm_v2 cultivate: time engine unavailable (%s)", exc)
        return done
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("item_id", "")).strip()
        if not iid:
            continue
        try:
            res = tt.cultivate_session(story_id, branch_id, iid, it.get("days"))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("gm_v2 cultivate failed for %s: %s", iid, exc)
            continue
        if res.get("ok"):
            done.append({"item_id": iid, "mastery": res.get("mastery_label"),
                         "spent_days": res.get("spent"), "promoted": res.get("promoted"),
                         "fired": res.get("fired", [])})
    return done


def _grant_dungeon_rewards(story_id: str, branch_id: str, outcome: str) -> dict:
    """確定性結算：讀 blueprint，把『已達成』的獎勵由引擎精確發放（命運點數定額來自
    blueprint，不是 GM 每回合臨時喊；item/功法 grant 進對應 map）。只有通關才發。"""
    if outcome != "cleared":
        return {}
    try:
        from story_core.gm_v2 import scenario_gen
        bp = scenario_gen.load_blueprint(story_id, branch_id) or {}
    except Exception:
        bp = {}
    granted: dict = {}
    amt = bp.get("clear_reward")
    if isinstance(amt, (int, float)) and not isinstance(amt, bool) and amt:
        apply_state_delta(story_id, branch_id, {"fate_points_delta": amt})  # fixed amount → no double-count
        granted["fate_points"] = amt
    for r in bp.get("rewards", []) or []:
        if not isinstance(r, dict) or "通關" not in str(r.get("gated_by", "")):
            continue
        item = str(r.get("item", "")).strip()
        if not item:
            continue
        target = "techniques" if str(r.get("tier", "")) in ("技能", "功法") else "inventory"
        apply_state_delta(story_id, branch_id, {target: {item: r.get("level_or_effect") or "副本獎勵"}})
        granted.setdefault("items", []).append(item)
    return granted


def enter_dungeon(story_id: str, branch_id: str, ip: str) -> dict:
    """Deterministic 副本-entry: atomically set current_dungeon + phase, fire the scenario
    architect (blueprint) off-path, and charge the world-day cost. One explicit op so the
    lifecycle can't be half-recorded (the recurring current_dungeon miss)."""
    ip = str(ip or "").strip()
    if not ip:
        return {"ok": False, "reason": "missing_ip"}
    app = _app()
    old = app._load_character_state(story_id, branch_id)
    if (old.get("current_dungeon") or "").strip():
        return {"ok": True, "note": "已在副本中", "current_dungeon": old.get("current_dungeon")}
    apply_state_delta(story_id, branch_id, {"current_dungeon": ip, "current_phase": "副本中"})
    new = app._load_character_state(story_id, branch_id)
    try:
        from story_core.gm_v2 import scenario_gen
        scenario_gen.generate_blueprint_async(story_id, branch_id, ip,
                                              state_text=app._build_core_state_text(story_id, new))
    except Exception:
        log.exception("gm_v2 enter_dungeon: scenario_gen dispatch failed")
    try:
        tt = _time_tools()
        cfg = tt.config(story_id)
        if (cfg.get("dungeon_time") or "charged") == "charged":
            tt.advance(story_id, branch_id, days=cfg.get("dungeon_time_default", 7))
    except Exception:
        log.exception("gm_v2 enter_dungeon: time charge failed")
    return {"ok": True, "current_dungeon": ip, "current_phase": "副本中"}


def _archive_dungeon_npcs(story_id: str, branch_id: str, dungeon: str) -> int:
    """離開副本時收乾淨副本 NPC（結構化 exit_dungeon 才會做；走敘事回歸也接到這裡）：
    - 副本【原生】NPC（出身=副本/dungeon_local/秘境，且此刻在這個副本裡）→ offstage（留在副本世界，
      不再 active；以後不會反向串門進母世界，戰力面板也不再掛著）。
    - 母世界【帶進副本的隊友】（出身=本世界/主神空間）→ 跟玩家回家：present_world 設回本世界、保持 active。
    母世界沒進副本的 NPC 完全不動。回傳收掉的數量。"""
    from story_core.gm_v2.prompt import _DUNGEON_ANY, _npc_present_world
    path = _story_npcs_path(story_id, branch_id)
    npcs = _load_json(path, [])
    if not isinstance(npcs, list):
        return 0
    changed = 0
    for n in npcs:
        if not isinstance(n, dict) or n.get("lifecycle_status") == "archived":
            continue
        # 用【出身 scope】判定副本原生，不靠 present_world——present_world 會被寫入時序污染（GM 在
        # current_dungeon 已清空後才 upsert 副本 NPC → 被誤蓋成本世界）。出身才是穩定的歸屬軸。
        scope = str(n.get("home_scope") or n.get("scope") or "")
        is_home = any(k in scope for k in ("本世界", "母世界", "主神", "main_god"))
        is_dungeon_native = (not is_home) and (
            "dungeon" in scope.lower() or "秘境" in scope or "副本" in scope)
        if is_dungeon_native:
            n["lifecycle_status"] = "archived"  # 副本原生 → offstage（留在副本世界）
            n["archive_kind"] = "offstage"
            n.setdefault("archived_reason", f"left_dungeon:{dungeon}")
            changed += 1
        elif is_home and _npc_present_world(n) in (dungeon, _DUNGEON_ANY):
            n["present_world"] = "本世界"  # 帶進副本的母世界隊友 → 跟著回家、保持 active
            changed += 1
        # else: 出身不明（'—'，如本世界長老趙無極）或母世界沒進副本 → 不動（不誤殺）
    if changed:
        _save_json(path, npcs)
    return changed


def exit_dungeon(story_id: str, branch_id: str, outcome: str = "cleared") -> dict:
    """Deterministic 副本-exit/通關: settle blueprint rewards, then atomically clear
    current_dungeon + set phase + append completed_dungeons + 收乾淨副本 NPC. The engine
    owns the whole transition; the GM只需呼叫一次（走敘事回歸也由 record pass 叫到這裡）。"""
    app = _app()
    old = app._load_character_state(story_id, branch_id)
    dungeon = (old.get("current_dungeon") or "").strip()
    if not dungeon:
        return {"ok": True, "note": "本就不在副本中"}
    rewards = _grant_dungeon_rewards(story_id, branch_id, outcome)
    phase = "死亡" if outcome == "died" else "主神空間"
    delta = {"current_dungeon": "", "current_phase": phase}
    if outcome == "cleared":
        delta["completed_dungeons_add"] = [dungeon]
    apply_state_delta(story_id, branch_id, delta)
    archived = _archive_dungeon_npcs(story_id, branch_id, dungeon)  # 副本原生 NPC offstage、隊友帶回家
    return {"ok": True, "left": dungeon, "outcome": outcome, "rewards": rewards, "npcs_archived": archived}


def _load_quests(story_id: str, branch_id: str) -> list:
    state = _load_json(_story_character_state_path(story_id, branch_id), {})
    q = state.get("quests")
    return q if isinstance(q, list) else []


def _save_quests(story_id: str, branch_id: str, quests: list) -> None:
    """Persist the `quests` top-level list (a non-schema key — `_load_character_state`
    preserves plain keys, so it round-trips). RMW the raw json like the other v2 engine
    tools (catalog/time/action) so we don't depend on the schema state machinery."""
    path = _story_character_state_path(story_id, branch_id)
    state = _load_json(path, {})
    state["quests"] = quests
    _save_json(path, state)


def apply_quest_add(story_id: str, branch_id: str, items: list) -> list:
    """Create quests the GM opened (status=active; reward DECLARED now). Dedup by id —
    re-adding an existing id updates its title/desc/reward but NEVER resurrects a
    done/failed quest to active (completion is engine-owned). Returns new ids."""
    if not items:
        return []
    quests = _load_quests(story_id, branch_id)
    by_id = {q.get("id"): q for q in quests if isinstance(q, dict)}
    added: list = []
    for it in items:
        if not isinstance(it, dict):
            continue
        qid = str(it.get("id") or "").strip()
        if not qid:
            continue
        existing = by_id.get(qid)
        if existing:
            for k in ("title", "desc", "reward_fate", "reward_items"):
                if it.get(k) is not None:
                    existing[k] = it[k]
        else:
            q = {"id": qid, "title": it.get("title") or qid,
                 "desc": it.get("desc") or "", "status": "active"}
            if it.get("reward_fate") is not None:
                q["reward_fate"] = it["reward_fate"]
            if it.get("reward_items"):
                q["reward_items"] = it["reward_items"]
            quests.append(q)
            by_id[qid] = q
            added.append(qid)
    _save_quests(story_id, branch_id, quests)
    return added


def complete_quest(story_id: str, branch_id: str, quest_id: str, outcome: str = "done") -> dict:
    """Deterministic 結算：mark a quest done/failed and grant its DECLARED reward exactly
    (fate via fate_points_delta + items into inventory, through apply_state_delta → engine
    math + state.db sync). Idempotent: an already-settled quest is not re-granted. The GM
    never does the arithmetic — same discipline as exit_dungeon / purchase."""
    qid = str(quest_id or "").strip()
    if not qid:
        return {"ok": False, "reason": "missing_quest_id"}
    quests = _load_quests(story_id, branch_id)
    q = next((x for x in quests if isinstance(x, dict) and x.get("id") == qid), None)
    if not q:
        return {"ok": False, "reason": "unknown_quest", "quest_id": qid,
                "hint": "該任務不存在；開任務請用 add_quest"}
    if q.get("status") in ("done", "failed"):
        return {"ok": True, "note": "已結算", "quest_id": qid, "status": q.get("status")}
    granted: dict = {}
    if outcome == "done":
        delta: dict = {}
        rf = q.get("reward_fate")
        if isinstance(rf, (int, float)) and not isinstance(rf, bool) and rf:
            delta["fate_points_delta"] = rf
            granted["fate_points"] = rf
        for item in q.get("reward_items") or []:
            name = str(item).strip()
            if name:
                delta.setdefault("inventory", {})[name] = "任務獎勵"
                granted.setdefault("items", []).append(name)
        if delta:
            apply_state_delta(story_id, branch_id, delta)  # exact engine math + sync
    # update status on the FRESH post-reward state (reward write happened above)
    quests = _load_quests(story_id, branch_id)
    for x in quests:
        if isinstance(x, dict) and x.get("id") == qid:
            x["status"] = "done" if outcome == "done" else "failed"
    _save_quests(story_id, branch_id, quests)
    if outcome == "done":  # record as a deterministic milestone (deduped) so the GM won't re-offer it
        path = _story_character_state_path(story_id, branch_id)
        stt = _load_json(path, {})
        if _add_milestone(stt, f"完成任務：{q.get('title') or qid}"):
            _save_json(path, stt)
    return {"ok": True, "quest_id": qid, "title": q.get("title"),
            "status": "done" if outcome == "done" else "failed", "rewards": granted}


def _recent_gm_narration(story_id: str, branch_id: str, n: int = 3) -> str:
    """The last n GM narrations on this branch (own delta) — recent context for the reconciler."""
    try:
        from story_core import story_io as sio
        msgs = sio._load_json(sio._story_messages_path(story_id, branch_id), [])
        gms = [str(m.get("content") or "") for m in msgs
               if isinstance(m, dict) and m.get("role") == "gm"]
        return "\n\n".join(g[:600] for g in gms[-n:] if g)
    except Exception:
        return ""


def reconcile_quests(story_id: str, branch_id: str, model: str | None = None) -> list:
    """A2 任務對帳：用 LLM 判斷哪些【進行中】任務的目標已在劇情中【明確達成】，逐一 complete_quest。
    保守——尚在進行／尚未發生／不確定一律保留。**不碰任務『創建』**（所以絕無『新任務開不出來』的 FP）；
    誤結是軟性、可逆（任務仍在、玩家可改回 active）。Returns the quest ids it closed."""
    import json as _json
    quests = _load_quests(story_id, branch_id)
    active = [q for q in quests if isinstance(q, dict) and q.get("status") == "active" and q.get("id")]
    if len(active) < 2:
        return []
    try:
        from story_core import compaction
        recap = compaction.get_recap_text(story_id, branch_id) or ""
    except Exception:
        recap = ""
    recent = _recent_gm_narration(story_id, branch_id, 3)
    qlist = "\n".join(f"- [{q['id']}] {q.get('title','')}：{q.get('desc','')}" for q in active)
    prompt = (
        "你是任務對帳員。判斷下列【進行中】任務裡，哪些的目標已在劇情中【明確達成】。\n"
        "【保守原則】只列出目標確實已完成的；尚在進行、尚未發生、或不確定的，一律【不要】列入。\n\n"
        f"［劇情摘要］\n{recap}\n\n［最近劇情］\n{recent}\n\n［進行中任務］\n{qlist}\n\n"
        '只輸出 JSON（不要說明）：{"done": ["已達成任務的 id", ...]}（無則 {"done": []}）'
    )
    try:
        from story_core.llm_bridge import call_oneshot
        raw = call_oneshot(prompt)
    except Exception:
        return []
    m = re.search(r"\{.*\}", str(raw or ""), re.DOTALL)
    if not m:
        return []
    try:
        data = _json.loads(m.group(0))
    except Exception:
        return []
    ids = data.get("done") if isinstance(data, dict) else None
    if not isinstance(ids, list):
        return []
    active_ids = {q["id"] for q in active}
    closed = []
    for qid in ids:
        qid = str(qid).strip()
        if qid in active_ids:
            r = complete_quest(story_id, branch_id, qid)
            if r.get("ok") and r.get("status") == "done":
                closed.append(qid)
                log.info("reconcile_quests: auto-closed %s (%s/%s)", qid, story_id, branch_id)
    return closed


_ANCHOR_PUNCT_RE = re.compile(r"[\s，。、；：「」『』（）()…·,.!?！？]+")


def _anchor_norm(s: str) -> str:
    return _ANCHOR_PUNCT_RE.sub("", str(s or ""))


def _anchor_similar(a: str, b: str) -> bool:
    """True if two anchors are near-duplicate paraphrases (so the list stops piling up the same
    fact, e.g. the 9 「御氣引息功濾網」 variants). Containment OR high char-4gram overlap."""
    na, nb = _anchor_norm(a), _anchor_norm(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    ga = {na[i:i + 4] for i in range(max(0, len(na) - 3))}
    gb = {nb[i:i + 4] for i in range(max(0, len(nb) - 3))}
    if not ga or not gb:
        return False
    inter = len(ga & gb)
    return inter / len(ga | gb) >= 0.6


def apply_story_anchors_add(story_id: str, branch_id: str, items: list) -> list:
    """Append GM long-term-memory facts to `story_anchors`, dropping near-duplicate paraphrases
    (containment / 4-gram overlap) so the list stays small, then normalize to the 12-item core
    limit. story_anchors is a cross-story CORE field (not in any per-story schema), so — like
    quests — RMW the raw json under the state lock. Returns the facts actually added."""
    facts = [s for s in (items or []) if isinstance(s, str) and s.strip()]
    if not facts:
        return []
    app = _app()
    path = _story_character_state_path(story_id, branch_id)
    with app._get_character_state_lock(story_id, branch_id):
        state = _load_json(path, {})
        existing = state.get("story_anchors")
        existing = existing if isinstance(existing, list) else []
        before = list(existing)
        kept, pool = [], list(existing)
        for f in facts:  # drop facts that paraphrase something already remembered
            if any(_anchor_similar(f, e) for e in pool):
                continue
            kept.append(f)
            pool.append(f)
        merged = app._normalize_story_anchors([*existing, *kept])
        state["story_anchors"] = merged
        _save_json(path, state)
    return [a for a in merged if a not in before]


def apply_clean_delta(story_id, branch_id, clean, msg_index=None, run_ctx=None) -> dict:
    """Persist gate's `clean` delta. Returns a summary; lore is deferred to the critic."""
    summary: dict = {}
    if clean.get("state_delta"):
        apply_state_delta(story_id, branch_id, clean["state_delta"])
        summary["state"] = True
    if clean.get("npcs"):
        summary["npcs"] = apply_npcs(story_id, branch_id, clean["npcs"], msg_index, run_ctx)
    if clean.get("catalog_upsert"):
        summary["catalog_upsert"] = apply_catalog_upsert(story_id, clean["catalog_upsert"])
    if clean.get("quests"):
        summary["quests_added"] = apply_quest_add(story_id, branch_id, clean["quests"])
    if clean.get("story_anchors"):
        summary["story_anchors_added"] = apply_story_anchors_add(story_id, branch_id, clean["story_anchors"])
    fired: list = []
    cultivated_days = 0.0
    if clean.get("cultivate"):
        cult = apply_cultivate(story_id, branch_id, clean["cultivate"])
        summary["cultivate"] = cult
        for c in cult:
            cultivated_days += float(c.get("spent_days") or c.get("spent") or 0)
            fired.extend(c.get("fired") or [])
    # 閉關的 days 已由 cultivate 計入世界時鐘；若同回合 GM 又開 time，跳過以免時間被扣兩次
    if clean.get("time") and cultivated_days <= 0:
        t = advance_time(story_id, branch_id, clean["time"])
        if t:
            summary["time"] = t
            fired.extend(t.get("fired") or [])
    elif clean.get("time"):
        summary["time_skipped"] = {"reason": "閉關已計時", "ignored": clean["time"]}
    if fired:
        summary["deadlines_fired"] = fired  # pipeline surfaces these as a forced event next turn
    if clean.get("lore"):
        summary["lore_deferred"] = len(clean["lore"])  # handed to async critic, not written here
    return summary


__all__ = ["apply_state_delta", "apply_npcs", "advance_time", "apply_catalog_upsert",
           "apply_cultivate", "enter_dungeon", "exit_dungeon", "apply_quest_add",
           "complete_quest", "_load_quests", "apply_story_anchors_add", "apply_clean_delta"]
