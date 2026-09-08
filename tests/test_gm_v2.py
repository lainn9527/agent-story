"""Unit tests for the GM v2 contract: parser + deterministic gate (no LLM/Flask)."""

import json

from story_core.gm_v2.contract import DELTA_CLOSE, DELTA_OPEN
from story_core.gm_v2.ops import gate_delta
from story_core.gm_v2.parse import parse_turn_output

SCHEMA = {
    "fields": [
        {"key": "current_phase", "type": "text"},
        {"key": "current_status", "type": "text"},
        {"key": "reward_points", "type": "number"},
    ],
    "lists": [
        {"key": "inventory", "type": "map"},
        {"key": "systems", "type": "map"},
        {"key": "abilities", "state_add_key": "abilities_add", "state_remove_key": "abilities_remove"},
        {"key": "completed_missions", "state_add_key": "completed_missions_add"},
    ],
    "direct_overwrite_keys": ["base_power_level", "gene_lock", "current_phase", "current_status"],
}


def _block(d: dict) -> str:
    return f"{DELTA_OPEN}\n{json.dumps(d, ensure_ascii=False)}\n{DELTA_CLOSE}"


# --- parse ---------------------------------------------------------------

def test_parse_clean_marker():
    d = {"state_delta": {"reward_points_delta": 300, "inventory": {"刀": "鋒利"}}}
    raw = "你推開大門，踏入主神空間的白色大廳。\n\n<!--ACTIONS-->\n1. 前進\n\n" + _block(d)
    narration, delta, note = parse_turn_output(raw)
    assert note is None
    assert delta == d
    assert "大廳" in narration
    assert DELTA_OPEN not in narration and "reward_points_delta" not in narration


def test_parse_nested_braces_ok():
    d = {"state_delta": {"systems": {"萬象召喚": "A級"}}, "npcs": [{"name": "林牧"}]}
    narration, delta, note = parse_turn_output("敘事\n" + _block(d))
    assert note is None
    assert delta == d  # nested braces survive (close marker delimits)


def test_parse_fenced_fallback():
    raw = '敘事文字。\n```json\n{"state_delta": {"reward_points_delta": 1}}\n```'
    narration, delta, note = parse_turn_output(raw)
    assert delta["state_delta"]["reward_points_delta"] == 1
    assert "```" not in narration


def test_parse_malformed_keeps_narration():
    raw = "重要的敘事內容。\n" + DELTA_OPEN + " {壞掉的 json } " + DELTA_CLOSE
    narration, delta, note = parse_turn_output(raw)
    assert delta == {}
    assert note is not None and "parse failed" in note
    assert "重要的敘事內容" in narration


def test_parse_no_block():
    narration, delta, note = parse_turn_output("純敘事，沒有 delta 區塊")
    assert delta == {}
    assert note == "no delta block found"
    assert narration == "純敘事，沒有 delta 區塊"


# --- gate ----------------------------------------------------------------

def test_gate_keeps_valid():
    out = gate_delta({"state_delta": {"reward_points_delta": 300, "inventory": {"刀": "鋒利"}, "abilities_add": ["御劍"]}}, SCHEMA)
    sd = out["clean"]["state_delta"]
    assert sd["reward_points_delta"] == 300
    assert sd["inventory"] == {"刀": "鋒利"}
    assert sd["abilities_add"] == ["御劍"]
    assert not out["dropped"]
    assert not out["flagged"]


def test_gate_drops_scene_keys():
    out = gate_delta({"state_delta": {"location": "大廳", "threat_level": "高", "combat_status": "戰鬥中"}}, SCHEMA)
    assert "state_delta" not in out["clean"]
    assert {d["reason"] for d in out["dropped"]} == {"scene_key"}


def test_gate_strips_junk_map_keys():
    """LLM junk: a string spread into single-ASCII-char keys ({'g':'山菇'…}) or a method-name
    key ({'append':[…]}) corrupts inventory/techniques. Strip the junk, keep the real items."""
    out = gate_delta({"state_delta": {"inventory": {
        "g": "山菇與野果", "j": "草藥繃帶", "append": ["x"], "青鋼靈劍": "可用",
        "qinggang_spirit_sword": True,      # english-slug 'present flag' dup → junk
        "m4_carbine": "12發",                # legit English-IP item (string status) → KEEP
    }}}, SCHEMA)
    inv = out["clean"]["state_delta"]["inventory"]
    assert inv == {"青鋼靈劍": "可用", "m4_carbine": "12發"}   # real items kept (incl. english-IP item)
    d = next(x for x in out["dropped"] if x["reason"] == "junk_map_keys")
    assert set(d["removed"]) == {"g", "j", "append", "qinggang_spirit_sword"}
    # a map that is ALL junk → the whole field is dropped (nothing legit left)
    out2 = gate_delta({"state_delta": {"inventory": {"g": "x", "append": ["y"]}}}, SCHEMA)
    assert "inventory" not in out2["clean"].get("state_delta", {})


def test_gate_locks_engine_owned_fields():
    # 引擎獨佔欄位：GM 不准寫（delta 或 set_state 都擋）→ dropped，clean 不含它們
    out = gate_delta({"state_delta": {
        "current_dungeon": "鬼滅之刃",          # 只 enter/exit_dungeon 能寫
        "completed_dungeons_add": ["火影"],      # 只 exit_dungeon
        "technique_mastery": {"kb_base": 3},     # 「成」只 cultivate/解鎖（GM 永不指定）
    }}, SCHEMA)
    assert "state_delta" not in out["clean"]
    assert {d["reason"] for d in out["dropped"]} == {"engine_owned"}


def test_gate_locks_engine_phases_and_corruption():
    # beat-id 等非法 phase → drop（根治 b1:林間迷霧 汙染）
    bad = gate_delta({"state_delta": {"current_phase": "b1:林間迷霧(0%)"}}, SCHEMA)
    assert "state_delta" not in bad["clean"]
    assert any(d["reason"] == "invalid_phase" for d in bad["dropped"])
    # 副本生命週期 phase 由引擎獨佔（enter/exit_dungeon）→ GM 寫被擋
    eng = gate_delta({"state_delta": {"current_phase": "副本中"}}, SCHEMA)
    assert "state_delta" not in eng["clean"]
    assert any(d["reason"] == "engine_owned_phase" for d in eng["dropped"])
    # GM 仍可敘事驅動 死亡/傳送中
    ok = gate_delta({"state_delta": {"current_phase": "死亡"}}, SCHEMA)
    assert ok["clean"]["state_delta"]["current_phase"] == "死亡"


def test_gate_uses_per_story_phase_allowlist():
    """世界自己的階段語彙（urban_daily 的 日常/事件中）由 schema `phases` 宣告；沒宣告的
    story 仍走全域 VALID_PHASES。缺這條時，非「主神空間/副本」系的世界每次 current_phase
    都被當 invalid_phase 丟掉，階段永遠凍在預設值。"""
    urban = dict(SCHEMA, phases=["日常", "事件中", "危機", "靜養", "死亡"])
    ok = gate_delta({"state_delta": {"current_phase": "事件中"}}, urban)
    assert ok["clean"]["state_delta"]["current_phase"] == "事件中"
    # 該世界沒宣告的值仍被擋（beat-id 汙染防線不變）
    bad = gate_delta({"state_delta": {"current_phase": "b1:雨夜的巷口(0%)"}}, urban)
    assert "state_delta" not in bad["clean"]
    assert any(d["reason"] == "invalid_phase" for d in bad["dropped"])
    # 副本生命週期 phase 永遠是引擎的，宣告了也不給 GM 寫
    eng = gate_delta({"state_delta": {"current_phase": "副本中"}}, dict(urban, phases=urban["phases"] + ["副本中"]))
    assert any(d["reason"] == "engine_owned_phase" for d in eng["dropped"])


def test_parse_strips_legacy_tags():
    raw = '敘事開頭。<!--STATE {"x": 1} STATE--> 敘事結尾。\n' + _block({"state_delta": {"reward_points_delta": 1}})
    narration, delta, note = parse_turn_output(raw)
    assert "STATE" not in narration and '"x"' not in narration   # legacy tag stripped
    assert "敘事開頭" in narration and "敘事結尾" in narration
    assert delta["state_delta"]["reward_points_delta"] == 1       # DELTA block still parsed


def test_parse_strips_orphan_state_residue():
    """branch_17adbce0 bug: the GM emitted a MALFORMED legacy tag `STATE STATE-->` (no opening
    `<!--`), which the balanced regex missed → it leaked to the end of the narration. The
    stripper must also kill orphan close fragments — while keeping the <!--ACTIONS--> block."""
    n, _, _ = parse_turn_output("全力迎接戒律堂的審判。\n\nSTATE STATE-->")
    assert n == "全力迎接戒律堂的審判。"
    n2, _, _ = parse_turn_output("結尾。\nNPC-->")
    assert n2 == "結尾。"
    # orphan residue right before an ACTIONS block: residue gone, ACTIONS kept
    n3, _, _ = parse_turn_output("劇情。 STATE STATE-->\n<!--ACTIONS-->\n1. 走")
    assert "STATE" not in n3 and "<!--ACTIONS-->" in n3 and "1. 走" in n3


def test_parse_strips_bare_trailing_state():
    """branch_a3c6c823 bug: the GM ended on a BARE keyword『STATE』— no opening `<!--` AND no
    closing `-->` (a half-streamed tag), so all the delimiter-anchored regexes skipped it and
    `.strip()` (whitespace-only) left it. Must strip a trailing bare/partial tag WITHOUT eating
    the kept <!--ACTIONS--> block that sits before it."""
    n, _, _ = parse_turn_output("你斬出一劍。\n\n---\n<!--ACTIONS-->\n1. 追擊\n2. 撤退\n\nSTATE")
    assert "STATE" not in n and "<!--ACTIONS-->" in n and "1. 追擊" in n and "2. 撤退" in n
    # bare keyword, no actions
    n2, _, _ = parse_turn_output("劇情結束。\n\nSTATE")
    assert n2 == "劇情結束。"
    # half-streamed partial tag (no close)
    n3, _, _ = parse_turn_output('劇情。\nSTATE {"current_phase": "副本中"')
    assert n3 == "劇情。"


def test_gate_drops_unknown_key():
    out = gate_delta({"state_delta": {"made_up_field": "x"}}, SCHEMA)
    assert any(d["reason"] == "unknown_key" for d in out["dropped"])


def test_gate_flags_power_field_but_keeps_it():
    out = gate_delta({"state_delta": {"base_power_level": "A級（已穩定）"}}, SCHEMA)
    # thin & permissive: kept, NOT dropped (this is the anti-bug-#1 behaviour)
    assert out["clean"]["state_delta"]["base_power_level"] == "A級（已穩定）"
    assert any(f["reason"] == "power_field" for f in out["flagged"])


def test_gate_flags_systems_change():
    out = gate_delta({"state_delta": {"systems": {"萬象召喚": "A級"}}}, SCHEMA)
    assert out["clean"]["state_delta"]["systems"] == {"萬象召喚": "A級"}
    assert any(f["reason"] == "systems_change" for f in out["flagged"])


def test_gate_npc_terminal_with_memory_flagged():
    out = gate_delta({"npcs": [{"name": "伽椰子", "lifecycle_status": "terminal", "memory_add": ["怨念未消"]}]}, SCHEMA)
    assert out["clean"]["npcs"][0]["name"] == "伽椰子"
    assert any(f["reason"] == "terminal_with_memory_add" for f in out["flagged"])


def test_gate_npc_missing_name_dropped():
    out = gate_delta({"npcs": [{"role": "路人"}]}, SCHEMA)
    assert "npcs" not in out["clean"]
    assert any(d["reason"] == "missing_name" for d in out["dropped"])


def test_gate_lore_flagged_for_review():
    out = gate_delta({"lore": [{"topic": "主神空間規則", "content": "..."}]}, SCHEMA)
    assert out["clean"]["lore"]
    assert any(f["reason"] == "lore_needs_review" for f in out["flagged"])


# --- #2 delta-only currency ----------------------------------------------

_CURRENCY_SCHEMA = {
    "fields": [
        {"key": "fate_points", "type": "number", "engine_managed": True},
        {"key": "major_realm", "type": "text"},
    ],
    "lists": [],
    "direct_overwrite_keys": ["major_realm"],
}


def test_gate_currency_absolute_dropped_delta_kept():
    # 引擎帳本：GM 不准絕對覆寫餘額（會拿過期值重算→算錯），但可用 *_delta 增減（引擎算總額）。
    out = gate_delta({"state_delta": {
        "fate_points": 2000,         # 絕對覆寫 → DROP
        "fate_points_delta": 200,    # 增減 → KEEP（NPC 賞賜/變賣/臨時獎勵的合法管道）
        "major_realm": "築基",        # 一般欄位 → KEEP
    }}, _CURRENCY_SCHEMA)
    sd = out["clean"]["state_delta"]
    assert "fate_points" not in sd
    assert sd["fate_points_delta"] == 200
    assert sd["major_realm"] == "築基"
    assert any(d["key"] == "fate_points" and d["reason"] == "currency_absolute" for d in out["dropped"])


def test_gate_currency_unmarked_field_not_locked():
    # 沒標 engine_managed 的數值欄位不受影響（reward_points 仍可絕對寫）
    out = gate_delta({"state_delta": {"reward_points": 500}}, SCHEMA)
    assert out["clean"]["state_delta"]["reward_points"] == 500


# --- #1 story_anchors (長期記憶) write op ----------------------------------

def test_gate_story_anchors_clean_dedup_drop_blanks():
    out = gate_delta({"story_anchors": ["主角是穿越者", "主角是穿越者", "  ", "與仇人立死約"]}, SCHEMA)
    assert out["clean"]["story_anchors"] == ["主角是穿越者", "與仇人立死約"]  # dedup + 去空白
    assert any(d["reason"] == "empty" for d in out["dropped"])


def test_story_anchors_op_registered():
    from story_core.gm_v2.ops import DELTA_OPS
    op = next((o for o in DELTA_OPS if o["key"] == "story_anchors"), None)
    assert op is not None and op["tool"] == "remember" and op["apply"] == "apply_story_anchors_add"


# --- 級 vs 層: realm-anchored level (the 練氣五層→5級 fix) ---------------------

def test_leveljudge_anchor_is_fine_grained_realm_x10_plus_layer():
    from story_core.gm_v2.leveljudge import _realm_anchored_level
    ladder = ["凡人", "練氣", "築基", "金丹", "元嬰"]
    # 級 = 大境界×10 + 層（個位）：練氣(base0)、築基(base10)、金丹(base20)…
    assert _realm_anchored_level({"rank_label": "練氣五層"}, ladder) == 5    # 0 + 5層
    assert _realm_anchored_level({"rank_label": "練氣三層"}, ladder) == 3    # 0 + 3層
    assert _realm_anchored_level({"rank_label": "築基初期"}, ladder) == 12   # 10 + 初期(2)
    assert _realm_anchored_level({"rank_label": "金丹中期", "role": "長老"}, ladder) == 25  # 20 + 中期(5)
    # 大境界相同 → 級//10 一致（戰力質變按大境界）
    assert _realm_anchored_level({"rank_label": "練氣一層"}, ladder) // 10 == \
        _realm_anchored_level({"rank_label": "練氣九層"}, ladder) // 10 == 0
    # 裸境界（無層）→ 夾到該大境界格底；舊制 level 跨界 → 搬進本格（防灌水）
    assert _realm_anchored_level({"rank_label": "築基", "level": None}, ladder) == 11  # 10 + 格底1
    assert _realm_anchored_level({"rank_label": "築基期", "level": 2}, ladder) == 12   # 舊2 → 築基格內
    # 凡人(最底, index 0) → None（非戰 0級不被強制升為 1）
    assert _realm_anchored_level({"rank_label": "凡人·山野通"}, ladder) is None
    # 非修真體系（鬼滅/火影/殭屍先生）無階梯境界 → None（交給 LLM 判定）
    assert _realm_anchored_level({"rank_label": "下弦之伍"}, ladder) is None
    assert _realm_anchored_level({"rank_label": "鬼殺候補·日輪刀"}, ladder) is None
    assert _realm_anchored_level({"rank_label": "練氣五層"}, []) is None  # 無階梯 → None


# --- prompt builder (app-free) -------------------------------------------

def test_system_prompt_grounds_and_contracts():
    from story_core.gm_v2.contract import DELTA_OPEN
    from story_core.gm_v2.prompt import build_system_prompt
    npcs = [
        {"name": "林牧", "role": "輪迴者", "lifecycle_status": "active", "memory": ["欠你一個人情"]},
        {"name": "鬼影", "lifecycle_status": "archived", "memory": ["已被封印"]},
    ]
    sp = build_system_prompt("你是 GM。", "reward_points: 100", npcs, inventory={"日輪刀": "鋒利", "回血丹": "3顆"})
    assert "你是 GM。" in sp
    assert "reward_points: 100" in sp
    assert "林牧" in sp and "欠你一個人情" in sp   # active NPC + durable memory grounded
    assert "鬼影" not in sp                        # archived NPC excluded from steady prompt
    assert "日輪刀" in sp and "回血丹" in sp        # inventory canonical names grounded (Tier-1)
    assert "命名紀律" in sp                         # naming-discipline reminder present
    assert DELTA_OPEN in sp                         # output contract embedded


def test_user_message_order_and_dice():
    from story_core.gm_v2.prompt import build_user_message
    msg = build_user_message("我攻擊", lore="[相關設定]\nX", recap="前情", dice_hint="趨勢有利")
    assert msg.rstrip().endswith("我攻擊")         # player action last
    assert "前情" in msg and "X" in msg and "趨勢有利" in msg
    assert msg.index("前情") < msg.index("我攻擊")


def test_user_message_bare():
    from story_core.gm_v2.prompt import build_user_message
    assert build_user_message("只有行動") == "只有行動"


# --- critic (pure parts) -------------------------------------------------

def test_critic_prompt_includes_flags():
    from story_core.gm_v2.critic import build_critic_prompt
    flagged = [{"key": "systems", "reason": "systems_change"}]
    delta = {"state_delta": {"systems": {"萬象召喚": "A級（暫時穩定）"}}, "lore": [{"topic": "X"}]}
    p = build_critic_prompt("敘事內容", flagged, delta)
    assert "萬象召喚" in p and "敘事內容" in p


def test_critic_parse_json_fenced_and_raw():
    from story_core.gm_v2.critic import _parse_json
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('前綴 {"b": 2} 後綴')["b"] == 2
    assert _parse_json("not json") == {}


# --- identity pre-filter (deterministic recall, NOT a precision decision) ---

def test_identity_candidate_prefilter():
    from story_core.gm_v2.identity import find_candidate_pairs
    npcs = [
        {"name": "基斯"}, {"name": "基斯 (Chris Redfield)"},
        {"name": "李青"}, {"name": "李青山"}, {"name": "吉爾"},
    ]
    pairs = {frozenset(p) for p in find_candidate_pairs(npcs)}
    # surface-overlap pairs ARE candidates — the LLM judge decides same/different
    assert frozenset({"基斯", "基斯 (Chris Redfield)"}) in pairs   # variant (judge -> same)
    assert frozenset({"李青", "李青山"}) in pairs                   # substring trap (judge -> different)
    # no overlap -> never even asked (no judge call)
    assert frozenset({"基斯", "吉爾"}) not in pairs
    assert frozenset({"李青", "吉爾"}) not in pairs


def test_identity_inventory_candidate_prefilter():
    from story_core.gm_v2.identity import find_candidate_item_pairs
    inv = {"傳送令": "1張", "萬寶宗傳送令": "可回宗門", "回血丹": "3顆", "聚靈丹": "1瓶"}
    pairs = {frozenset(p) for p in find_candidate_item_pairs(inv)}
    assert frozenset({"傳送令", "萬寶宗傳送令"}) in pairs   # variant -> candidate (judge decides)
    assert frozenset({"回血丹", "聚靈丹"}) not in pairs     # unrelated -> no judge call


# --- regenerate ops replay (op-log snapshot survives ack/fork) ------------

def _catalog_tools_mod():
    import os, sys
    play_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "play")
    if play_dir not in sys.path:
        sys.path.insert(0, play_dir)
    import catalog_tools
    return catalog_tools


def test_turn_ops_snapshot_survives_ack(tmp_path, monkeypatch):
    """A GM reply's ops snapshot must outlive ops_ack (which clears pending_ops), so a
    later regenerate of that reply can replay them. This is the core of the regen fix."""
    ct = _catalog_tools_mod()
    monkeypatch.setattr(ct.sio, "_branch_dir", lambda s, b: str(tmp_path))

    # player buys a 功法 via the deterministic UI -> appended to pending_ops
    ct._append_op("s", "main", {"op": "buy", "item": "九陽神功", "amount": 50})
    assert len(ct.ops_pending("s", "main")["ops"]) == 1

    # GM turn at index 7 acknowledges it: snapshot first, then clear pending
    pending = ct.ops_pending("s", "main")["ops"]
    ct.save_turn_ops("s", "main", 7, pending)
    ct.ops_ack("s", "main")

    # pending is now empty (won't re-surface next turn / isn't copied on fork)...
    assert ct.ops_pending("s", "main")["ops"] == []
    # ...but the per-turn snapshot persists, keyed by the GM reply index
    replay = ct.turn_ops("s", "main", 7)
    assert len(replay) == 1 and replay[0]["item"] == "九陽神功"
    # other indices are unaffected; build_ops_block renders the replayed op
    assert ct.turn_ops("s", "main", 8) == []
    from story_core.gm_v2 import prompt as v2prompt
    assert "九陽神功" in v2prompt.build_ops_block(replay)


def test_save_turn_ops_noop_on_empty(tmp_path, monkeypatch):
    ct = _catalog_tools_mod()
    monkeypatch.setattr(ct.sio, "_branch_dir", lambda s, b: str(tmp_path))
    ct.save_turn_ops("s", "main", 3, [])     # nothing acknowledged -> no snapshot written
    ct.save_turn_ops("s", "main", None, [{"op": "buy"}])  # no index -> no-op
    assert ct.turn_ops("s", "main", 3) == []


# --- manual inventory discard (clean up items the GM forgot to remove) ----

def test_discard_item_removes_and_op_logs(tmp_path, monkeypatch):
    """丟棄 a non-ledger inventory item (the GM-forgot-to-clean case): item gone, OTHER
    items untouched, NO currency credited (it's 丟棄 not 退款), op-logged so the GM is told."""
    ct = _catalog_tools_mod()
    monkeypatch.setattr(ct.sio, "_branch_dir", lambda s, b: str(tmp_path))
    state_path = str(tmp_path / "character_state.json")
    monkeypatch.setattr(ct.sio, "_story_character_state_path", lambda s, b: state_path)
    ct.sio._save_json(state_path, {"fate_points": 100,
                                   "inventory": {"殭屍封印符": "舊任務遺留", "靈石": "貨幣"}})

    res = ct.discard_item("s", "main", "殭屍封印符")
    assert res["ok"] and res["discarded"] == "殭屍封印符"
    st = ct.sio._load_json(state_path, {})
    assert "殭屍封印符" not in st["inventory"]
    assert "靈石" in st["inventory"]      # other items untouched
    assert st["fate_points"] == 100       # no currency credited
    ops = ct.ops_pending("s", "main")["ops"]
    assert ops and ops[-1]["op"] == "discard" and ops[-1]["item"] == "殭屍封印符"
    from story_core.gm_v2 import prompt as v2prompt
    assert "殭屍封印符" in v2prompt.build_ops_block(ops)  # GM told it's gone

    # missing item -> refused, no removal
    res2 = ct.discard_item("s", "main", "不存在的東西")
    assert not res2["ok"] and res2["reason"] == "not_found"


def test_discard_item_drops_ledger_to_block_refund_exploit(tmp_path, monkeypatch):
    """Discarding a bought item drops its ledger entry so buy→discard→refund can't double-dip."""
    ct = _catalog_tools_mod()
    monkeypatch.setattr(ct.sio, "_branch_dir", lambda s, b: str(tmp_path))
    state_path = str(tmp_path / "character_state.json")
    monkeypatch.setattr(ct.sio, "_story_character_state_path", lambda s, b: state_path)
    ct.sio._save_json(state_path, {"fate_points": 0, "inventory": {"桃木劍": "已購入"}})
    ct.sio._save_json(ct._ledger_path("s", "main"),
                      [{"vendor": "wanbao", "item_id": "mg_wpn_peach", "name": "桃木劍",
                        "price": 100, "currency_field": "fate_points", "grant_to": "inventory"}])

    ct.discard_item("s", "main", "桃木劍")
    assert ct._load_list(ct._ledger_path("s", "main")) == []   # ledger entry dropped
    r = ct.refund("s", "main", "wanbao", "mg_wpn_peach")        # can't re-credit a discarded item
    assert not r["ok"] and r["reason"] == "no_purchase"


# --- harness per-message snapshot parity (fork/regen NPC reconstruction) --

def _gm_tools_mod():
    import os, sys
    play_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "play")
    if play_dir not in sys.path:
        sys.path.insert(0, play_dir)
    import gm_tools
    return gm_tools


def test_harness_commit_embeds_snapshots(tmp_path, monkeypatch):
    """gm_tools.commit_turn must embed npcs/state snapshots on the GM message. Without
    them, _find_npcs_at_index returns [] at a fork point and regen re-fabricates the
    cast (the play_eeaa57 → branch_872d63b2 bug)."""
    gm = _gm_tools_mod()
    monkeypatch.setattr(gm.sio, "_branch_dir", lambda s, b: str(tmp_path))
    # canonical roster + state on the branch (what /play wrote directly)
    gm.save_npcs("s", "main", [{"name": "柳素素", "personality": "話多，神經大條", "lifecycle_status": "archived"}])
    gm.save_state("s", "main", {"name": "陸凡", "major_realm": "練氣"})
    gm.sio._save_json(gm.sio._story_messages_path("s", "main"), [])

    res = gm.commit_turn("s", "main", "我繼續修煉", "你盤膝坐在硬木榻上。")
    msgs = gm.sio._load_branch_messages("s", "main")
    gm_msg = next(m for m in msgs if m["index"] == res["gm_index"])

    # the GM reply now carries the canonical snapshot a fork would reconstruct from
    assert "npcs_snapshot" in gm_msg
    assert gm_msg["npcs_snapshot"][0]["name"] == "柳素素"
    assert gm_msg["npcs_snapshot"][0]["personality"] == "話多，神經大條"  # canon, not re-fabricated
    assert gm_msg["state_snapshot"]["major_realm"] == "練氣"


def test_harness_commit_writes_turn_ops(tmp_path, monkeypatch):
    """commit_turn must snapshot pending UI ops → turn_ops[gm_index] so a regen of a
    harness turn replays the player's 購買/兌換 (the play_eeaa57 #86 御氣引息功 case —
    harness never persisted ops, so regen's replay_ops came up empty)."""
    gm = _gm_tools_mod()
    import catalog_tools
    monkeypatch.setattr(gm.sio, "_branch_dir", lambda s, b: str(tmp_path))
    gm.sio._save_json(gm.sio._story_messages_path("s", "main"), [])
    # player bought a 功法 via the web UI → appended to pending_ops out-of-band
    catalog_tools._append_op("s", "main", {"op": "purchase", "item": "御氣引息功", "amount": 200})

    res = gm.commit_turn("s", "main", "兌換功法開始修煉", "你走到兌換處，換了門基礎功法。")

    # the turn's ops are snapshotted under the GM reply index → regen reads them back
    replayed = catalog_tools.turn_ops("s", "main", res["gm_index"])
    assert replayed and replayed[0]["item"] == "御氣引息功"
    # and a different index is unaffected (per-turn keyed map, not a single slot)
    assert catalog_tools.turn_ops("s", "main", res["gm_index"] + 99) == []


# --- leveljudge 賦級: 級 == realm position, even when the LLM contradicts it ---

def test_realm_anchored_level_is_realm_block_plus_layer():
    from story_core.gm_v2 import leveljudge as lj
    ladder = ["凡人", "練氣", "築基", "金丹"]
    assert lj._realm_anchored_level({"rank_label": "練氣三層"}, ladder) == 3    # base0 + 層3
    assert lj._realm_anchored_level({"rank_label": "築基中期"}, ladder) == 15   # base10 + 中期(5)
    assert lj._realm_anchored_level({"rank_label": "凡人"}, ladder) is None     # idx0 below combat scale
    assert lj._realm_anchored_level({"rank_label": "外門老弟子"}, ladder) is None  # no realm → LLM


def test_npc_grounding_is_co_located_only_no_homeworld_bleed():
    """青雲宗串門 bug: in a 副本, grounding must feed ONLY NPCs co-located with the player —
    home-world (本世界) NPCs left behind must NOT bleed into the 副本 prompt, but a teammate
    BROUGHT into the 副本 (present_world stamped to the 副本) MUST be grounded (origin-agnostic)."""
    from story_core.gm_v2.prompt import build_npc_block, build_dungeon_seal_block, _npc_present_world
    npcs = [
        {"name": "葉明", "role": "同門", "home_scope": "本世界（修真）", "lifecycle_status": "active"},
        {"name": "九叔", "role": "茅山道長", "home_scope": "dungeon_local", "lifecycle_status": "active"},
        {"name": "炭治郎", "role": "鬼殺", "home_scope": "秘境:鬼滅", "lifecycle_status": "active"},
        # a 本世界 teammate the player BROUGHT into the 副本 → present_world stamped to the 副本
        {"name": "柳素素", "role": "同門", "home_scope": "本世界（修真）",
         "present_world": "殭屍先生", "lifecycle_status": "active"},
    ]
    blk = build_npc_block(npcs, current_world="殭屍先生")
    assert "九叔" in blk            # 副本-native → grounded
    assert "柳素素" in blk           # brought teammate (present in 副本) → grounded (origin-agnostic)
    assert "葉明" not in blk         # home NPC left behind → NOT bled into the 副本
    assert "炭治郎" not in blk       # a DIFFERENT 副本's NPC → not co-located
    # at home, the home NPCs are grounded and the 副本 natives are not
    home = build_npc_block(npcs, current_world="本世界")
    assert "葉明" in home and "九叔" not in home
    # present-world derivation: home_scope → present world; explicit present_world wins
    assert _npc_present_world({"home_scope": "本世界（修真）"}) == "本世界"
    assert _npc_present_world({"home_scope": "秘境:鬼滅"}) == "鬼滅"
    assert _npc_present_world({"home_scope": "本世界", "present_world": "殭屍先生"}) == "殭屍先生"
    # the 副本-seal policy block names the dungeon and bans home-world 串門
    seal = build_dungeon_seal_block("殭屍先生")
    assert "殭屍先生" in seal and "串門" in seal and "青雲宗" in seal


def test_moral_tone_block_anti_flatten_context_aware():
    """逐回合道德基調 block: home world reasserts the grey-morality worldview (虛潮=真敵,
    魔道五宗=盟友, 正道也腐敗) to fight the 仙俠 genre prior; inside a 副本 it stays IP-neutral
    (反臉譜 only) so it doesn't import 修真 正邪 and break 世界觀隔離."""
    from story_core.gm_v2.prompt import build_moral_tone_block
    home = build_moral_tone_block(in_dungeon=False)
    assert "虛潮" in home and "盟友" in home and "正道" in home  # full grey-morality
    assert "魔道五宗" in home
    dungeon = build_moral_tone_block(in_dungeon=True)
    assert "IP" in dungeon and "臉譜" in dungeon            # anti-flatten kept
    assert "虛潮" not in dungeon and "魔道五宗" not in dungeon  # but NO 修真 morality imported into the IP


def test_exit_dungeon_archives_dungeon_natives_brings_teammates_home(monkeypatch, tmp_path):
    """exit_dungeon cleanup (走敘事回歸也接到這裡): 副本-native NPCs (dungeon_local/秘境 origin) →
    archived(offstage), keyed on ORIGIN not present_world (which write-ordering can mis-stamp);
    a 母世界 teammate brought in (本世界 origin, present in 副本) → comes home (active);
    home NPCs not in the 副本 + unknown-origin elders ('—') → untouched (no mis-archive)."""
    from story_core import story_io as sio
    from story_core.gm_v2 import apply as A
    monkeypatch.setattr(sio, "_branch_dir", lambda s, b: str(tmp_path))
    npcs = [
        # mis-stamped present_world=本世界, but ORIGIN=dungeon → still archived (the 灰骨真人 straggler)
        {"name": "灰骨真人", "home_scope": "dungeon_local", "lifecycle_status": "active", "present_world": "本世界"},
        {"name": "炭治郎", "home_scope": "秘境:鬼滅", "lifecycle_status": "active"},
        # 本世界 teammate brought INTO the 副本 (present there) → comes home, stays active
        {"name": "葉明", "home_scope": "本世界（修真）", "present_world": "終焉之山", "lifecycle_status": "active"},
        {"name": "謝林", "home_scope": "本世界（修真）", "lifecycle_status": "active"},  # at home → untouched
        {"name": "趙無極", "home_scope": "—", "lifecycle_status": "active"},            # unknown origin → untouched
    ]
    sio._save_json(sio._story_npcs_path("s", "main"), npcs)
    A._archive_dungeon_npcs("s", "main", "終焉之山")
    out = {n["name"]: n for n in sio._load_json(sio._story_npcs_path("s", "main"), [])}
    assert out["灰骨真人"]["lifecycle_status"] == "archived"   # dungeon-native by origin → offstage
    assert out["炭治郎"]["lifecycle_status"] == "archived"
    assert out["葉明"]["lifecycle_status"] == "active" and out["葉明"]["present_world"] == "本世界"  # teammate home
    assert out["謝林"]["lifecycle_status"] == "active"        # home NPC untouched
    assert out["趙無極"]["lifecycle_status"] == "active"      # unknown-origin elder NOT mis-archived


def test_realm_estimate_not_pinned_to_xianxia_ladder():
    """九叔 bug: a 副本-native NPC recorded with a power-EQUIVALENCE estimate in parens
    (茅山道長（築基/金丹預估）) must NOT be hard-pinned to the 修真 ladder — that realm name is a
    guess, not a 修真 境界. Estimate (預估/約/≈) or a multi-realm range → None (LLM judges)."""
    from story_core.gm_v2 import leveljudge as lj
    ladder = ["凡人", "練氣", "築基", "金丹", "元嬰"]
    assert lj._realm_anchored_level({"rank_label": "茅山道長（築基/金丹預估）"}, ladder) is None
    assert lj._realm_anchored_level({"rank_label": "強鬼（約築基）"}, ladder) is None
    assert lj._realm_anchored_level({"rank_label": "上忍（築基級推估）"}, ladder) is None
    # but a clean 修真 NPC with the realm in parens is NOT an estimate → still pinned
    assert lj._realm_anchored_level({"rank_label": "外門弟子（練氣三層）"}, ladder) == 3


def test_leveljudge_reanchors_inflated_llm_output_into_realm_block(monkeypatch):
    """anti-inflation: input has no realm → LLM judges, but the LLM OUTPUTS 練氣三層 with an
    inflated level 25 (= 金丹-tier). The 級 must be re-anchored into the OUTPUT realm's block —
    練氣三層 → 3, never the contradicting 25."""
    from story_core.gm_v2 import leveljudge as lj
    ladder = ["凡人", "練氣", "築基", "金丹"]
    monkeypatch.setattr(lj, "_call",
                        lambda prompt, model: '{"level": 25, "rank_label": "練氣三層", "domains": ["真元修煉"]}')
    npc = {"name": "趙猛", "role": "外門老弟子", "domains": []}  # no realm in input → LLM path
    res = lj._judge_one(npc, p_level=1, p_realm="練氣", model="x", ladder=ladder)
    assert res["level"] == 3   # re-anchored to 練氣三層 (大境界 0), not the LLM's inflated 25
