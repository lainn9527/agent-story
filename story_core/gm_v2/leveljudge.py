"""Tier-3 賦級 judge — assign a combat NPC its 級 / rank_label / domains.

The record pass TRANSCRIBES (creates the NPC from the narration) but doesn't JUDGE
strength — 級 is a *relative-to-protagonist judgment*, not text in the prose, so combat
NPCs land with no level/domains (the 戰力錨 the 級 system needs to adjudicate fights).

This focused async pass fills it: pre-filter to combat NPCs MISSING a level → one cheap
flash-lite call each (grounded on the protagonist's 級 + the world's realm ladder) →
write back. Cheap because RARE (pre-filter + only-missing + skip allies/vendors), like
the identity judge. Idempotent: never re-judges an NPC that already has a level.
"""
from __future__ import annotations

import json
import logging
import re
import threading

log = logging.getLogger("rpg")

JUDGE_MODEL = "gemini-3.1-flash-lite"
# combat-ish signal in name/role/domains (allies/vendors without these are skipped)
_HOSTILE = ("鬼", "弦", "無慘", "敵", "對手", "魔", "妖", "獸", "兵", "刺客", "殺手", "boss",
            "師兄", "師姐", "對戰", "比武", "強者", "守衛", "首領", "BOSS")


def _call(prompt: str, model: str) -> str:
    from story_core.gemini_bridge import call_gemini_oneshot
    from story_core.llm_bridge import _get_config
    return call_gemini_oneshot(prompt, gemini_cfg=_get_config().get("gemini", {}), model=model)


def _realm_ladder(story_id: str) -> list:
    import os
    from story_core.story_io import _load_json, _story_dir
    cat = _load_json(os.path.join(_story_dir(story_id), "catalog.json"), {})
    ladder = cat.get("realm_ladder")
    return ladder if isinstance(ladder, list) else []


def _rank_map(story_id: str) -> dict:
    """世界階序表（職稱→大境界）：宗門位階/職稱 → 該世界戰力錨點的境界（長老→元嬰…）。
    空 = 該世界未定義階序（NPC 仍可由相對 judge 評級）。"""
    import os
    from story_core.story_io import _load_json, _story_dir
    cat = _load_json(os.path.join(_story_dir(story_id), "catalog.json"), {})
    rm = cat.get("rank_map")
    return rm if isinstance(rm, dict) else {}


def _player_level(story_id: str, branch_id: str):
    """主角的細分級 = 大境界base + 層（minor_realm）。練氣一層→1、築基初期→11…。"""
    import os
    from story_core.story_io import _load_json, _story_character_state_path, _story_dir
    st = _load_json(_story_character_state_path(story_id, branch_id), {})
    cat = _load_json(os.path.join(_story_dir(story_id), "catalog.json"), {})
    ladder = cat.get("realm_ladder") or []
    mr = st.get("major_realm")
    base = _realm_block({"rank_label": str(mr or "")}, ladder)
    if base is None:
        return (0 if mr == "凡人" else None), mr
    layer = _parse_layer(str(st.get("minor_realm", "")))
    if layer is None:
        ly = st.get("minor_realm")
        layer = ly if isinstance(ly, int) and not isinstance(ly, bool) else 1
    return base + max(1, min(9, layer)), mr


def _needs_level(npc: dict, rank_map: dict | None = None, ladder: list | None = None) -> bool:
    if not isinstance(npc, dict) or npc.get("lifecycle_status") == "archived":
        return False
    # 有明確職稱（長老/掌門…）→ 命中世界階序表的 key（longest-match，與 _rank_anchored_level 一致）
    rank_hit = None
    if rank_map:
        rl_blob = " ".join([str(npc.get("rank_label", "")), str(npc.get("role", "")), str(npc.get("name", ""))])
        rank_hit = next((k for k in sorted(rank_map, key=len, reverse=True) if k and k in rl_blob), None)
    if npc.get("level") not in (None, "", 0):
        # 已賦級者預設不重判（idempotent）。例外：有職稱、但 stored 級的【大境界】低於該職稱應有的
        # 大境界（如 長老 卡在 15級/築基，rank_map 長老→元嬰）。職稱＝世界客觀戰力，必須補正，否則
        # rank_map 出現前的相對-judge 舊值會永遠卡著、fork 也一路繼承（玄風長老 15級 bug）。只在「被
        # 低估」時重錨，不動「等於或高於」職稱底線者（不亂降 legit 強者）。重錨後 rank_label 帶上境界
        # → 下輪同大境界不再觸發，無 churn。
        # guard：僅在 rank_label 完全沒提到任何境界詞時才用職稱補正。只要 rank_label 出現境界
        # （明確如「練氣三層」、或估計式如「練氣大圓滿/假築基」），那個敘事給的境界【優先】，不
        # 硬拉成職稱級造成 level↔label 矛盾（錢執事 23/練氣三層、雷刑被拉金丹 bug）。純頭銜
        # （長老/掌門/核心弟子，無境界字）才補。與 _judge_one 先 explicit-realm pin、再 rank 一致。
        _rl = str(npc.get("rank_label", ""))
        _rl_has_realm = any(str(r) in _rl for r in (ladder or [])[1:] if str(r).strip())
        if rank_hit and ladder and not _rl_has_realm:
            rank_base = _realm_block({"rank_label": str(rank_map[rank_hit])}, ladder)
            if rank_base is not None:
                try:
                    stored = int(npc.get("level"))
                except (TypeError, ValueError):
                    stored = None
                if stored is not None and (stored // 10) < (rank_base // 10):
                    return True  # 職稱戰力被低估 → 重新錨定
        return False  # already anchored
    # 尚無 level：
    blob = " ".join([str(npc.get("name", "")), str(npc.get("role", "")),
                     " ".join(str(d) for d in (npc.get("domains") or []))])
    if any(k in blob for k in _HOSTILE):
        return True
    # 有明確職稱的世界人物（長老/掌門…）也要賦級 — 即使不是戰鬥對手（職稱→戰力）
    if rank_hit:
        return True
    return False


# 級 = 大境界×10 + 層（個位）。練氣→0–9、築基→10–19、金丹→20–29…（見 _shared/xianxia_anchor.txt）。
# 大境界 = 級//10；層只是大境界內細分強弱，戰力質變（對人/對十/對百）按大境界算。
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "兩": 2}
# 高境界用「期」字而非「層」數：初/前期→2、中期→5、後期→8、巔峰/圓滿/大圓滿→9（映到個位 0–9）。
_STAGE_OFFSET = (("大圓滿", 9), ("圓滿", 9), ("巔峰", 9), ("顛峰", 9), ("登峰", 9),
                 ("瓶頸", 7), ("後期", 8), ("末期", 8), ("中後", 6), ("中期", 5),
                 ("前中", 3), ("前期", 2), ("初期", 2), ("初成", 1), ("入門", 1))


def _cn_to_int(tok: str) -> int:
    tok = str(tok).strip()
    if tok.isdigit():
        return int(tok)
    if "十" in tok:  # 十=10、十三=13、二十=20、三十二=32
        a, _, b = tok.partition("十")
        tens = _CN_NUM.get(a, 1) if a else 1
        ones = sum(_CN_NUM.get(c, 0) for c in b) if b else 0
        return tens * 10 + ones
    return sum(_CN_NUM.get(c, 0) for c in tok)


def _parse_layer(text) -> int | None:
    """大境界【內】的層 → 個位 0–9。練氣『五層』→5、『三重』→3；築基『中期』→5、『圓滿』→9。
    回 None ＝ 沒有可解析的層次（讓上層用預設/夾鉗或交給 LLM）。十層以上罕見 → 夾到 9。"""
    s = str(text or "")
    m = re.search(r"([一二三四五六七八九十兩百\d]+)\s*[層重階品段]", s)
    if m:
        n = _cn_to_int(m.group(1))
        if n > 0:
            return max(1, min(9, n))
    for kw, off in _STAGE_OFFSET:
        if kw in s:
            return off
    return None


# 戰力「預估／約／≈」標記：括號裡的境界是【戰力等價的估計】，不是該 NPC 真的在修真階梯上。
# 副本原生 NPC（茅山道長／鬼／屍／忍者…）常被記成「茅山道長（築基/金丹預估）」——那是 LLM 給的
# 等價猜測，不該硬釘上修真階梯（九叔 bug）。命中估計標記 → _realm_block 回 None → 交給 LLM 相對主角內插。
_EST_MARK = ("預估", "推估", "估計", "約", "≈", "相當", "等同", "疑似", "左右",
             "上下", "應為", "應有", "存疑", "待估", "?", "？")


def _is_realm_estimate(text, ladder: list) -> bool:
    """rank_label 是『戰力等價估計』而非真境界？命中估計詞，或列了 ≥2 個不同階梯境界（如 築基/金丹 的範圍）。"""
    s = str(text or "")
    if any(e in s for e in _EST_MARK):
        return True
    present = {str(r) for r in (ladder or [])[1:] if str(r).strip() and str(r) in s}  # skip 凡人
    return len(present) >= 2


def _realm_block(npc: dict, ladder: list) -> int | None:
    """大境界 base（級的十位×10）：練氣→0、築基→10、金丹→20…。凡人/非階梯世界（鬼滅/火影/
    殭屍先生）→ None（沒有母系統境界可錨，交給 LLM 相對主角內插）。
    rank_label 是『戰力等價估計』（築基/金丹預估…）→ 也回 None：那是副本原生 NPC，不硬釘修真階梯。"""
    if not ladder:
        return None
    if _is_realm_estimate(npc.get("rank_label", ""), ladder):
        return None  # 副本原生 NPC 的戰力等價估計 → 不上修真階梯，交給 LLM 相對主角判
    blob = " ".join([str(npc.get("rank_label", "")), str(npc.get("role", "")),
                     str(npc.get("name", ""))])
    best = None  # most-specific (longest) realm name present; ladder is ordered weak→strong
    for idx, realm in enumerate(ladder):
        r = str(realm).strip()
        if r and r in blob and (best is None or len(r) > len(str(ladder[best]))):
            best = idx
    if best in (None, 0):  # index 0 (凡人/最底) 不在戰力級尺上 → 交給 LLM/預設
        return None
    return (best - 1) * 10  # 練氣(idx1)→0、築基(idx2)→10、金丹(idx3)→20…


def _realm_pinned_level(npc: dict, ladder: list) -> int | None:
    """有明確層次（練氣五層／築基中期）→ 大境界base＋層 的【確定值】；無 → None。"""
    base = _realm_block(npc, ladder)
    if base is None:
        return None
    layer = _parse_layer(str(npc.get("rank_label", "")) + " " + str(npc.get("role", "")))
    return base + layer if layer is not None else None


def _clamp_into_block(level, base: int) -> int:
    """把一個級夾進該大境界的 10 格 [base+1, base+9]：已在格內 → 原值（保住層次梯度）；
    跨界（如舊制 2級 其實是築基）→ 取個位搬進本格，擋住跨大境界灌水。無值 → 格底 base+1。"""
    if isinstance(level, (int, float)) and not isinstance(level, bool):
        lv = int(level)
        if base + 1 <= lv <= base + 9:
            return lv
        ones = lv % 10
        return base + (ones if 1 <= ones <= 9 else 1)
    return base + 1


def _realm_anchored_level(npc: dict, ladder: list) -> int | None:
    """寫入鎖點（apply_npcs 用）：這個 NPC 應有的『細分級』＝大境界×10＋層。
    有明確層 → 確定值；無 → 把現有 level 夾進大境界格內（保住層梯度、擋跨界灌水）；
    非階梯世界 → None（不動，交給 LLM）。取代舊『級=境界序號』錨（練氣=1）。"""
    base = _realm_block(npc, ladder)
    if base is None:
        return None
    pinned = _realm_pinned_level(npc, ladder)
    return pinned if pinned is not None else _clamp_into_block(npc.get("level"), base)


def _rank_anchored_level(npc: dict, ladder: list, rank_map: dict | None):
    """職稱→戰力（絕對換算，不靠主角相對推）：rank_label/role/name 命中世界階序表（長老→元嬰…）
    → 該境界 base ＋ 層（rank_label 有層用層，無 → 中期 5）。回 (級, 補上境界的 rank_label)；
    無命中／非階梯境界 → None（交回相對 judge — 用於對手境界不明／剛進新世界）。"""
    if not rank_map:
        return None
    rl = str(npc.get("rank_label", "")).strip()
    blob = " ".join([rl, str(npc.get("role", "")), str(npc.get("name", ""))])
    hit = next((k for k in sorted(rank_map, key=len, reverse=True) if k and k in blob), None)
    if not hit:
        return None
    realm = str(rank_map[hit])
    base = _realm_block({"rank_label": realm}, ladder)
    if base is None:
        return None
    layer = _parse_layer(blob)
    level = base + (layer if layer is not None else 5)  # 無明確層 → 中期
    # 把境界補進 rank_label，使下一輪直接走 explicit-realm pin（不再依賴 rank_map）
    new_rl = rl if _realm_block({"rank_label": rl}, ladder) is not None else (realm + (rl or hit))
    return level, new_rl


def _judge_one(npc: dict, p_level, p_realm: str, model: str, ladder: list | None = None, rank_map: dict | None = None):
    ladder = ladder or []
    base = _realm_block(npc, ladder)
    pinned = _realm_pinned_level(npc, ladder)
    # fully deterministic: explicit 層 (pinned) + already carries domains → no LLM at all
    # (realm+層 gives the fine 級; avoids the 層→大境界 misfire AND a call).
    if pinned is not None and npc.get("domains"):
        return {"level": pinned, "rank_label": str(npc.get("rank_label", "")),
                "domains": [str(x) for x in (npc.get("domains") or []) if str(x).strip()]}

    # 職稱→戰力（絕對，優先於相對 judge）：有明確職稱就按世界階序換算，不靠主角相對推。
    # 相對 judge 只在「對手境界不明／剛進新世界」(無職稱可換) 時才接手（見下方 prompt）。
    ranked = _rank_anchored_level(npc, ladder, rank_map)
    if ranked is not None:
        lvl, rl = ranked
        return {"level": lvl, "rank_label": rl,
                "domains": [str(x) for x in (npc.get("domains") or []) if str(x).strip()]}

    last = (npc.get("memory") or ["—"])[-1]
    ctx = f"名:{npc.get('name')}｜身份:{npc.get('role', '')}｜既有性質:{npc.get('domains') or []}｜近況:{last}"
    prompt = f"""你是無限流 RPG 的戰力評級員。級制：**級 = 大境界×10 + 層**（練氣 0–9＝對人 / 築基 10–19＝對十 / 金丹 20–29＝對百 / 元嬰 30–39＝滅鎮 / 化神 40＋＝滅城）；大境界 = 級÷10。
主角當前 {p_level if p_level is not None else '?'}級（{p_realm or '?'}）。
為下面這個「敵對／對戰」NPC 賦戰力錨（以【大境界】為主、層為個位細分，相對主角內插）：
{ctx}
校準：
- 副本雜兵／普通屍／普通鬼 → 練氣級 **1~5級**（≤ 主角，**別灌水**）。
- 勢均對手 → 與主角同大境界、±幾層。
- 強敵 → 高主角約 1 個大境界（≈ +10）。
- 碾壓級 boss → 高 2 個大境界以上（≈ +20↑）。
**注意：「五層／三重／中期」是大境界【內】的層（練氣五層=5級），不是把級拉高 5 個大境界**——別把層數誤當大境界。
domains＝非數值性質標籤（劍修/體修/身法/精神/詭異/因果/剋制/再生…），無則給 []。
只輸出 JSON（不要任何說明）：{{"level": <整數，大境界×10+層>, "rank_label": "<當地語彙，如 練氣三層/下弦之伍>", "domains": ["..."]}}"""
    raw = _call(prompt, model)
    m = re.search(r"\{.*\}", str(raw or ""), re.DOTALL)
    d = {}
    if m:
        try:
            d = json.loads(m.group())
        except json.JSONDecodeError:
            d = {}
    lvl = d.get("level")
    lvl_int = int(lvl) if isinstance(lvl, (int, float)) and not isinstance(lvl, bool) else None
    if base is not None:
        # ladder NPC: pinned 層 wins; else clamp the LLM's number into this 大境界's 10-block
        # (can't let the LLM push 練氣 → 築基-tier — the anti-inflation guard).
        final_level = pinned if pinned is not None else _clamp_into_block(
            lvl_int if lvl_int is not None else npc.get("level"), base)
    elif lvl_int is not None:
        final_level = lvl_int
        # non-ladder input, but the LLM may have OUTPUT a母系統 realm (rank_label 練氣三層) whose
        # 級 contradicts its number → re-anchor on the OUTPUT realm (same 層≠大境界 guard).
        out_base = _realm_block({"rank_label": d.get("rank_label", "")}, ladder)
        if out_base is not None:
            out_pin = _realm_pinned_level({"rank_label": d.get("rank_label", "")}, ladder)
            final_level = out_pin if out_pin is not None else _clamp_into_block(final_level, out_base)
    else:
        return None
    return {"level": final_level,
            "rank_label": str(d.get("rank_label", "")) or str(npc.get("rank_label", "")),
            "domains": [str(x) for x in (d.get("domains") or []) if str(x).strip()]}


def resolve_levels(story_id: str, branch_id: str, model: str = JUDGE_MODEL) -> list:
    """Judge + write back 級/domain for every combat NPC missing a level. Returns the
    list of assignments made (empty if nothing needed judging)."""
    from story_core.story_io import _load_json, _save_json, _story_npcs_path
    path = _story_npcs_path(story_id, branch_id)
    npcs = _load_json(path, [])
    if not isinstance(npcs, list):
        return []
    rank_map = _rank_map(story_id)
    ladder = _realm_ladder(story_id)
    targets = [n for n in npcs if _needs_level(n, rank_map, ladder)]
    if not targets:
        return []
    p_level, p_realm = _player_level(story_id, branch_id)
    done, changed = [], False
    for npc in targets:
        try:
            v = _judge_one(npc, p_level, p_realm, model, ladder, rank_map)
        except Exception:
            log.exception("leveljudge: judge failed for %s", npc.get("name"))
            continue
        if not v:
            continue
        npc["level"] = v["level"]
        if v["rank_label"]:
            npc["rank_label"] = v["rank_label"]
        if v["domains"]:
            npc["domains"] = v["domains"]
        changed = True
        done.append({"name": npc.get("name"), **v})
    if changed:
        _save_json(path, npcs)
    return done


def resolve_levels_async(story_id: str, branch_id: str, model: str = JUDGE_MODEL):
    threading.Thread(target=resolve_levels, args=(story_id, branch_id, model), daemon=True).start()


__all__ = ["resolve_levels", "resolve_levels_async"]
