"""Delta-op registry — the single source of truth for the GM's mutation capabilities.

ONE list, two front-ends:
- **delta-block** (current transport): `gate_delta` loops this registry to validate the
  GM's trailing JSON block. Behaviour is byte-identical to the old hand-written gate.
- **function-calling** (Phase 2): each op's `tool`/`summary`/`params` becomes a Gemini
  tool declaration; its `apply` names the executor in apply.py. Adding a capability
  (e.g. a quest op) = appending ONE entry here (+ its apply fn), not editing 3 files.

App-free (imports only the app-free gate helpers + the contract constants). `apply` is a
NAME resolved in apply.py (app-coupled) so this module stays testable without the app.
See doc/architecture_v2.md (§5 四張臉; delta 寫側).
"""

from story_core.gm_v2.gate import (
    gate_catalog_upsert,
    gate_cultivate,
    gate_npcs,
    gate_quests,
    gate_state_delta,
    gate_story_anchors,
)

# --- normalized per-op gates: (value, schema) -> (clean, dropped, flagged) ---


def _g_state(v, schema):
    return gate_state_delta(v or {}, schema)


def _g_npcs(v, schema):
    return gate_npcs(v or [])


def _g_catalog(v, schema):
    clean, dropped = gate_catalog_upsert(v or [])
    return clean, dropped, []


def _g_cultivate(v, schema):
    clean, dropped = gate_cultivate(v or [])
    return clean, dropped, []


def _g_quests(v, schema):
    clean, dropped = gate_quests(v or [])
    return clean, dropped, []


def _g_anchors(v, schema):
    clean, dropped = gate_story_anchors(v)
    return clean, dropped, []


def _g_lore(v, schema):
    if not v:
        return None, [], []
    return v, [], [{"key": "lore", "reason": "lore_needs_review", "count": len(v)}]


def _g_passthrough(v, schema):
    return (v if v else None), [], []


# --- the registry ---------------------------------------------------------
# key      delta field / tool key
# tool     function-calling name (Phase 2)
# summary  one-line capability description (tool description + prompt)
# params   JSON schema for the value (Phase 2 tool parameters)
# gate     app-free validator (value, schema) -> (clean, dropped, flagged)
# apply    name of the executor in apply.py (None = handled elsewhere)

DELTA_OPS = [
    {
        "key": "state_delta", "tool": "set_state",
        "summary": "覆蓋/增減角色狀態欄位（境界、命運點數、inventory/relationships 等 map、*_delta 數值增減）。",
        "params": {"type": "object", "description": "character_state 欄位變更；map 欄位直接 upsert，value 設 '' 移除"},
        "gate": _g_state, "apply": "apply_state_delta",
    },
    {
        "key": "npcs", "tool": "upsert_npc",
        "summary": "新增/更新有出場或變化的 NPC：首次登場給人設（性格/口吻/背景）；顯式 lifecycle、append-only memory、戰力錨 級/domain/招式。性格/口吻是角色本質，定了之後別改（要變的是記憶/狀態/關係）。",
        "params": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "role": {"type": "string"},
            "lifecycle_status": {"type": "string", "enum": ["active", "offstage", "terminal"]},
            "personality": {"type": "string", "description": "性格本質（首次登場填，之後別改）"},
            "speech_style": {"type": "string", "description": "說話口吻/語氣（首次登場填，之後別改）"},
            "background": {"type": "string", "description": "出身/背景一句話"},
            "level": {"type": "number"}, "rank_label": {"type": "string"},
            "domains": {"type": "array", "items": {"type": "string"}},
            "abilities": {"type": "object"}, "current_status": {"type": "string"},
            "memory_add": {"type": "array", "items": {"type": "string"}},
        }, "required": ["name", "lifecycle_status"]}},
        "gate": _g_npcs, "apply": "apply_npcs",
    },
    {
        "key": "catalog_upsert", "tool": "upsert_catalog_item",
        "summary": "讓某商品正式上架可購（商城補貨／宗門開放兌換／解鎖新功法販售）。只上架，不直接給玩家。",
        "params": {"type": "array", "items": {"type": "object", "properties": {
            "vendor": {"type": "string"}, "id": {"type": "string"}, "name": {"type": "string"},
            "price": {"type": "number"}, "desc": {"type": "string"}, "category": {"type": "string"},
            "grant_to": {"type": "string"}, "level": {"type": "string"},
            "domains": {"type": "array", "items": {"type": "string"}},
        }, "required": ["vendor", "id", "name", "price"]}},
        "gate": _g_catalog, "apply": "apply_catalog_upsert",
    },
    {
        "key": "cultivate", "tool": "cultivate",
        "summary": "閉關修煉某已習得功法：投入天數→引擎依天賦自動推進「成」、消耗世界時間、可能觸發 deadline。",
        "params": {"type": "array", "items": {"type": "object", "properties": {
            "item_id": {"type": "string", "description": "功法樹節點 id"},
            "days": {"type": "number", "description": "投入天數；省略＝閉關到下一次突破"},
        }, "required": ["item_id"]}},
        "gate": _g_cultivate, "apply": "apply_cultivate",
    },
    {
        "key": "quests", "tool": "add_quest",
        "summary": "開新任務（劇情即時生出任務／接到委託時用）。只負責『創造』——完成與獎勵由引擎確定性結算，這裡不發獎。reward 現在宣告，完成時引擎精確發放。**別開模糊佔位任務（如『應對未知威脅』）：目標具體才開；若新任務取代了舊的模糊任務，記得 complete_quest 結掉舊的。**",
        "params": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string", "description": "穩定、不重複的任務 id"},
            "title": {"type": "string"}, "desc": {"type": "string"},
            "reward_fate": {"type": "number", "description": "完成時發放的命運點數（現在宣告）"},
            "reward_items": {"type": "array", "items": {"type": "string"}, "description": "完成時給的道具名"},
        }, "required": ["id", "title"]}},
        "gate": _g_quests, "apply": "apply_quest_add",
    },
    {
        "key": "story_anchors", "tool": "remember",
        "summary": "記下會【跨場景長期影響】的關鍵事實：身世真相、重大恩怨/承諾、世界級變動、主角的長期目標。只放之後仍會被參照、壓縮後也不能忘的；一次性劇情/暫態別放。**進度/身份等級/副本通關/任務完成由系統自動記錄，別用這個（會重複）**。append-only（只增不改）。",
        "params": {"type": "array", "items": {"type": "string"}},
        "gate": _g_anchors, "apply": "apply_story_anchors_add",
    },
    {
        "key": "lore", "tool": "add_lore",
        "summary": "確立未來其他場景會參照的通用設定（不放一次性劇情）。經 async critic 審核後才入庫。",
        "params": {"type": "array", "items": {"type": "string"}},
        "gate": _g_lore, "apply": None,  # deferred to async critic, not applied here
    },
    {
        "key": "time", "tool": "advance_time",
        "summary": "劇情時間流逝（非閉關）。路由到時間經濟同一時鐘並觸發 deadline。閉關的時間請用 cultivate，勿重複。",
        "params": {"type": "object", "properties": {"days": {"type": "number"}, "hours": {"type": "number"}}},
        "gate": _g_passthrough, "apply": "advance_time",
    },
    {
        "key": "recap", "tool": "set_recap",
        "summary": "需要壓縮歷史時填寫的劇情摘要。",
        "params": {"type": "string"},
        "gate": _g_passthrough, "apply": None,  # consumed by the commit/compaction path
    },
]

_BY_KEY = {op["key"]: op for op in DELTA_OPS}


def gate_delta(delta: dict, schema: dict) -> dict:
    """Gate a full GM-authored delta by looping the registry. Returns {clean, dropped,
    flagged}. Byte-identical to the legacy hand-written gate_delta (registry order =
    the old call order)."""
    delta = delta or {}
    clean: dict = {}
    dropped: list = []
    flagged: list = []
    for op in DELTA_OPS:
        c, d, f = op["gate"](delta.get(op["key"]), schema)
        if c:
            clean[op["key"]] = c
        dropped += d
        flagged += f
    return {"clean": clean, "dropped": dropped, "flagged": flagged}


def tool_specs() -> list:
    """Function-calling declarations (Phase 2): name/description/parameters per op."""
    return [
        {"name": op["tool"], "key": op["key"], "description": op["summary"], "parameters": op["params"]}
        for op in DELTA_OPS
    ]


__all__ = ["DELTA_OPS", "gate_delta", "tool_specs"]
