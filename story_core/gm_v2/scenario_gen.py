"""GM v2 scenario generation — the /scenario architect, Gemini edition.

On 副本-entry (current_dungeon ""→X) the pipeline fires this OFF the player's
critical path (daemon thread, like the critic). It mirrors the `/scenario` skill:
same unified blueprint schema, same 級 calibration + size floors, validated and
persisted via the SAME app-free `scenario_tools` engine the Claude harness uses.
The blueprint is then loaded back into grounding on subsequent in-dungeon turns
(see render_blueprint_brief + pipeline PRE) so the Gemini GM narrates within it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading

log = logging.getLogger("rpg")


def _scenario_tools():
    play_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "scripts", "play",
    )
    if play_dir not in sys.path:
        sys.path.insert(0, play_dir)
    import scenario_tools

    return scenario_tools


_SCHEMA_SPEC = """{
 "kind":"dungeon","title":"...","premise":"一句玩家視角鉤子","structure":"linear_clear|investigation|siege|waves|bracket|open 擇一",
 "difficulty":{"target_level":"N級","calibrated_to":"..."},
 "objectives":{"main":[{"id":"m1","beat":"...","reveal":"..."}],"side":[{"id":"s1","beat":"...","optional":true,"reward_ref":"r2","discover":"..."}]},
 "beats":[{"id":"b1","title":"...","type":"探索|遭遇|解謎|社交|戰鬥","gate":{"requires":[],"min_main_progress":0},"encounters":["e1"],"hooks":["s1"]}],
 "encounter_pool":[{"id":"e1","name":"...","level":"N級","domains":["..."],"concealment":"none 或描述","role":"trash|elite|boss|social","weakness":"...","behavior":"..."}],
 "climax":{"id":"c1","trigger_gate":"main_progress>=X% 且 ...","boss_ref":"<pool 內 boss id>","stakes":"...","twist":"..."},
 "rewards":[{"id":"r1","tier":"普通|精良|命運","item":"...","gated_by":"通關|支線s1|隱藏","level_or_effect":"...","domain":"選填"}],
 "pacing":{"min_beats_before_climax":3,"soft_turns":"預估","anti_rush":"擋過快的 gate 說明"},
 "context":{"ip":"<IP>","world_isolation":true,"entry_prereq":"...","on_clear":"結算：命運點數＋命運道具帶回","death":"real"}
}"""


def build_scenario_prompt(ip: str, state_text: str) -> str:
    return f"""你是「無限流 / 主神空間」RPG 的劇本架構師。為玩家正要進入的副本生成一份結構化 blueprint（只輸出純 JSON）。

# 副本 IP
{ip}（世界觀隔離：只用該 IP 的元素；主角可帶自身功法／法寶／境界）。

# 主角（校準對象，依此定難度）
{state_text}

# 級系統（兩層）
- 級＝以展現定義：1對人 / 2對十 / 3對百 / 4滅鎮 / 5滅城（向上開放）。
- Δ級律：同級需策略才贏；差1須付代價才求生；差2只能逃；差3即死。
- 第二層 domain（非遞移、可繞過級）：精神／詭異／因果／剋制／感染…。低級但帶 domain 的威脅能危及高級純戰力者。
- 校準：雜魚≤主角、精英±1、boss 高1~2級且**留有代價／策略的可勝路徑**；若主角功法對此 IP 敵人無效，要在 premise 與 boss 機制反映。

# 數量（依規模伸縮，硬下限必守）
硬下限：`side≥2`（型別不重複、**≥1 條綁 boss 剋制或關鍵獎勵**）、`beats≥4`、`encounter_pool≥4`（含 **≥1 非純戰力威脅** + **1 boss**）、`rewards≥2`（**≥1 藏支線/隱藏**）。標準規模：beats 5–7、main 2–3、side 2–3、pool 5–7、rewards 3–4。

# 參照完整性
beats[].encounters ∈ encounter_pool；beats[].hooks ∈ objectives.side；climax.boss_ref ∈ encounter_pool 且 role=boss；side.reward_ref ∈ rewards。

# 輸出格式（嚴格 JSON，只輸出 JSON、無任何說明文字）
{_SCHEMA_SPEC}"""


def _parse_json(raw: object) -> dict | None:
    txt = str(raw or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(json)?", "", txt).strip()
        txt = re.sub(r"```$", "", txt).strip()
    match = re.search(r"\{.*\}", txt, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _default_llm(prompt: str) -> str:
    from story_core.gemini_bridge import call_gemini_oneshot
    from story_core.llm_bridge import _get_config

    g = _get_config().get("gemini", {})
    return call_gemini_oneshot(prompt, gemini_cfg=g, model=g.get("model", "gemini-2.5-flash"))


def generate_blueprint(story_id: str, branch_id: str, ip: str, *, state_text: str = "", llm_fn=None) -> dict:
    """Generate → validate → persist a blueprint for `ip`. Retries once on parse/validate failure."""
    llm_fn = llm_fn or _default_llm
    st = _scenario_tools()
    prompt = build_scenario_prompt(ip, state_text)
    for attempt in range(2):
        bp = _parse_json(llm_fn(prompt))
        if not bp:
            log.warning("gm_v2 scenario_gen: no JSON (attempt %d) ip=%s", attempt + 1, ip)
            continue
        bp.setdefault("context", {}).setdefault("ip", ip)
        # deterministic 通關命運點數（依 target_level 定額；exit_dungeon 結算照發，不靠 LLM 喊數字）
        _m = re.search(r"(\d+)", str((bp.get("difficulty", {}) or {}).get("target_level", "")))
        bp.setdefault("clear_reward", (int(_m.group(1)) if _m else 1) * 400)
        v = st.validate(bp)
        if v["ok"]:
            res = st.save(story_id, branch_id, bp)
            log.info("gm_v2 scenario_gen: saved blueprint for %s (warnings=%d)", ip, len(v["warnings"]))
            return {"ok": True, "saved": res.get("saved"), "summary": st._summary(bp), "warnings": v["warnings"]}
        log.warning("gm_v2 scenario_gen: validation errors (attempt %d): %s", attempt + 1, v["errors"][:5])
    return {"ok": False, "error": "scenario generation failed (parse/validate after retry)"}


def generate_blueprint_async(story_id: str, branch_id: str, ip: str, *, state_text: str = ""):
    """Fire-and-forget on a daemon thread (off the player's critical path)."""
    threading.Thread(
        target=generate_blueprint, args=(story_id, branch_id, ip),
        kwargs={"state_text": state_text}, daemon=True,
    ).start()


def load_blueprint(story_id: str, branch_id: str) -> dict:
    try:
        return _scenario_tools().load(story_id, branch_id).get("blueprint") or {}
    except Exception:
        return {}


def render_blueprint_brief(bp: dict) -> str:
    """Compact GM-internal rendering for grounding (NOT shown to the player)."""
    if not bp:
        return ""
    diff = bp.get("difficulty", {}) or {}
    lines = [
        f"【當前副本劇本（GM 內部參考，勿洩漏給玩家）】{bp.get('title','')}｜{bp.get('structure','')}｜難度 {diff.get('target_level','')}",
        "前提：" + str(bp.get("premise", "")),
    ]
    obj = bp.get("objectives", {}) or {}
    if obj.get("main"):
        lines.append("主線：" + "；".join(str(m.get("beat", "")) for m in obj["main"]))
    if obj.get("side"):
        lines.append("支線：" + "；".join(f"{s.get('id')}={s.get('beat','')}（發現:{s.get('discover','')}）" for s in obj["side"]))
    beats = bp.get("beats", []) or []
    if beats:
        lines.append("節點：" + " → ".join(
            f"{b.get('id')}:{b.get('title','')}({(b.get('gate',{}) or {}).get('min_main_progress',0)}%)" for b in beats
        ))
    pool = bp.get("encounter_pool", []) or []
    if pool:
        def _e(e):
            dom = ("·".join(e.get("domains", [])) ) if e.get("domains") else ""
            return f"{e.get('name')}[{e.get('level')}{('/'+dom) if dom else ''}{(','+e.get('role')) if e.get('role') else ''}]"
        lines.append("敵人池：" + "；".join(_e(e) for e in pool))
    cx = bp.get("climax", {}) or {}
    if cx:
        lines.append(f"高潮：{cx.get('trigger_gate','')}｜boss={cx.get('boss_ref','')}｜代價:{cx.get('stakes','')}｜轉折:{cx.get('twist','')}")
    rw = bp.get("rewards", []) or []
    if rw:
        lines.append("獎勵：" + "；".join(f"{r.get('item')}({r.get('tier')}/{r.get('gated_by')})" for r in rw))
    pc = bp.get("pacing", {}) or {}
    if pc:
        lines.append(f"節奏：min_beats_before_climax={pc.get('min_beats_before_climax')}｜防衝刺:{pc.get('anti_rush','')}")
    lines.append("（依此推進：beats 順序 + pacing/anti_rush 擋過快、climax 的 trigger_gate 未達不可觸發；按 reveal/discover 漸進揭露，絕不把劇本／級表倒給玩家。）")
    return "\n".join(lines)


__all__ = [
    "build_scenario_prompt", "generate_blueprint", "generate_blueprint_async",
    "load_blueprint", "render_blueprint_brief",
]
