"""GM v2 grounded prompt builder (app-free; the caller supplies loaded pieces).

The whole point of grounding: the GM authors its delta while SEEING the current
state + active NPCs (with their durable memory). That is what removes the blind
reverse-engineering of the legacy extractor.
"""

from story_core.gm_v2 import relationship as _relmod
from story_core.gm_v2.contract import OUTPUT_CONTRACT


def _rel_display(relationships, name: str) -> str:
    """The player↔NPC relationship as 'tier（note）' (tolerates str or {tier,note})."""
    if not isinstance(relationships, dict):
        return ""
    return _relmod.display(relationships.get(name))


# presence (在場) 軸 — 獨立於 scope(出身) 與 lifecycle(還在不在故事裡)。grounding 只餵「跟主角同場」
# 的 NPC：母世界的人留在母世界，不會自己串門進副本；主角帶進副本的隊友才在場（GM 把他寫進副本場景
# ＝apply 自動把他 present_world 蓋成當前副本）。修『青雲宗串門到副本』的核心。
_DUNGEON_ANY = "\x00dungeon\x00"  # 標記為副本原生但未記名 → 視為「主角所在的那個副本」


def _npc_present_world(npc: dict) -> str:
    """NPC 此刻所在世界。優先用顯式 present_world(apply 寫入時蓋的)；沒有就從 scope(出身)保守推：
    本世界/母世界→『本世界』；秘境:X/副本:X→該世界名；裸 dungeon_local→哨兵(主角所在副本)；未知→本世界。"""
    pw = str(npc.get("present_world") or "").strip()
    if pw:
        return pw
    scope = str(npc.get("home_scope") or npc.get("scope") or "").strip()
    if not scope or "本世界" in scope or "本界" in scope or "母世界" in scope:
        return "本世界"
    for sep in (":", "："):
        if sep in scope:
            body = scope.split(sep, 1)[1].strip()
            for br in ("（", "("):
                if br in body:
                    body = body.split(br, 1)[0].strip()
            return body or "本世界"
    if "dungeon" in scope.lower() or scope in ("local", "副本", "秘境"):
        return _DUNGEON_ANY
    return scope


def _npc_co_located(npc: dict, current_world: str) -> bool:
    """這個 NPC 此刻是否跟主角同場？current_world＝主角所在世界（副本名 or 『本世界』）。"""
    cw = str(current_world or "").strip()
    pw = _npc_present_world(npc)
    if pw == _DUNGEON_ANY:
        return cw not in ("", "本世界")  # 副本原生：主角在任一副本內即同場
    return pw == cw


def build_npc_block(npcs: list, relationships: dict | None = None, limit: int = 12,
                    current_world: str | None = None) -> str:
    """Active NPCs with their persona (性格/口吻) + combat anchor (級/domain) + durable
    memory, for grounding.

    The persona AND the 級/domains/abilities are re-injected EVERY turn so the GM reads
    back its own prior assignment and stays consistent — keeps the NPC in character and
    on-voice, and stops it re-inventing an enemy's strength mid-fight. The static lexicon
    (xianxia_anchor) shapes the first-contact assignment; THIS is what keeps it stable
    across turns — without it the persona/domains are stored on the NPC but never seen
    again, so the GM drifts off them.

    `current_world` (副本名 or 『本世界』)：若給定，只餵**與主角同場(present)**的 NPC —— 母世界
    NPC 不會在副本裡被注入(不再串門)，帶進副本的隊友才在場。None＝不過濾(向後相容/測試)。
    """
    lines = []
    for npc in npcs or []:
        if not isinstance(npc, dict):
            continue
        if npc.get("lifecycle_status") == "archived":
            continue  # archived NPCs are not in the steady prompt (recall handled elsewhere)
        if current_world and not _npc_co_located(npc, current_world):
            continue  # 不同場(presence 軸) → 不進 grounding；母世界 NPC 不串門進副本
        name = str(npc.get("name", "")).strip()
        if not name:
            continue
        parts = [f"- {name}（{npc.get('role', '')}）"]
        # persona (性格/口吻) — re-injected EVERY turn so the GM keeps the NPC in
        # character and on-voice, instead of drifting (same discipline as 級/domains).
        persona = npc.get("personality")
        if isinstance(persona, dict):  # tolerate legacy Big5 object form
            persona = persona.get("summary")
        persona = str(persona or "").strip()
        if persona:
            parts.append(f"性格：{persona}")
        speech = str(npc.get("speech_style") or "").strip()
        if speech:
            parts.append(f"口吻「{speech}」")
        # player↔NPC relationship — re-injected so it EVOLVES consistently (and 改變 sticks),
        # instead of resetting each turn. 性格 is the stable anchor; 關係 is the dynamic axis.
        rel = _rel_display(relationships, name)
        if rel:
            parts.append(f"與你的關係：{rel}")
        level = npc.get("level", npc.get("tier"))  # `tier` = legacy field name
        rank = str(npc.get("rank_label", "")).strip()
        if level not in (None, ""):
            parts.append(f"[{level}級{('·' + rank) if rank else ''}]")
        elif rank:
            parts.append(f"[{rank}]")
        domains = [str(d) for d in (npc.get("domains") or []) if str(d).strip()]
        if domains:
            parts.append("性質：" + "·".join(domains))
        abilities = npc.get("abilities")
        if isinstance(abilities, dict) and abilities:
            ab = "；".join(
                (f"{k}（{str(v)[:48]}）" if v else str(k))
                for k, v in list(abilities.items())[:4]
            )
            parts.append("招式：" + ab)
        if npc.get("concealment"):
            parts.append(f"收斂：{npc['concealment']}")
        if npc.get("current_status"):
            parts.append(f"狀態：{npc['current_status']}")
        memory = npc.get("memory") or []
        if memory:
            parts.append("記憶：" + "；".join(str(m) for m in memory[-5:]))
        lines.append(" ".join(parts))
        if len(lines) >= limit:
            break
    return "\n".join(lines) if lines else "（無）"


def build_vendor_block(vendors: list) -> str:
    lines = []
    for v in vendors or []:
        if not isinstance(v, dict) or not v.get("id"):
            continue
        cur = v.get("currency_label") or v.get("currency_field") or ""
        lines.append(f"- {v['id']}：{v.get('name', '')}（貨幣：{cur}）")
    return "\n".join(lines)


def build_tree_progress_block(state: dict, catalog_items: list) -> str:
    """Owned 功法樹 nodes with their 成 (mastery) + node id — so the GM can author a
    grounded `cultivate` delta (id-keyed) as cultivation advances in the story.
    Without this the GM can't address the tree, so it would never progress."""
    mm = (state or {}).get("technique_mastery")
    if not isinstance(mm, dict) or not mm:
        return ""
    names = ["未習", "入門", "小成", "大成", "圓滿"]
    id2name = {it.get("id"): it.get("name") for it in (catalog_items or []) if isinstance(it, dict) and it.get("id")}
    lines = []
    for iid, ordv in mm.items():
        if not isinstance(ordv, int) or isinstance(ordv, bool):
            continue
        lines.append(f"- {iid} {id2name.get(iid, iid)}：{names[max(0, min(ordv, len(names) - 1))]}")
    if not lines:
        return ""
    return "## 功法樹修為（GM 內部；cultivate 用下列 id，劇情精進時推進「成」）\n" + "\n".join(lines)


def build_quests_block(state: dict) -> str:
    """Active quests for grounding — so the GM weaves them into the story AND knows to
    call `complete_quest` (by id) when one is fulfilled. Without this the GM can't see
    open quests, so it would never progress them. Completion/reward stays deterministic;
    this is read-side only."""
    quests = (state or {}).get("quests")
    if not isinstance(quests, list) or not quests:
        return ""
    lines = []
    for q in quests:
        if not isinstance(q, dict) or q.get("status") != "active":
            continue
        bits = [f"- [{q.get('id')}] {q.get('title', '')}"]
        if q.get("desc"):
            bits.append(f"：{q['desc']}")
        rf = q.get("reward_fate")
        if rf:
            bits.append(f"（獎勵 {rf} 命運點數{'＋' + '、'.join(q['reward_items']) if q.get('reward_items') else ''}）")
        lines.append("".join(bits))
    if not lines:
        return ""
    return ("## 進行中任務（GM 用上列 id 操作；獎勵由引擎發，勿自行給）\n"
            "- 劇情推進這些任務；**目標一達成就呼叫 `complete_quest`** 結掉它，別讓已完成的任務孤懸。\n"
            "- **別開模糊的佔位任務**（如「應對未知威脅」「提防某存在」）——資訊不足先不開，等目標具體再開。\n"
            "- 若你新開的任務其實**取代**了上面某個較模糊的舊任務（同一目標、更明確版）→ 一併 `complete_quest` 把舊的結掉。\n"
            + "\n".join(lines))


def build_dungeon_pool_block(worlds: list | None) -> str:
    """Real-IP world options for OPENING a 副本. The GM frames the moment (主神鼎/門戶開啟) and
    presents THESE as the player's choices — it must NOT invent a no-name 秘境 (the tested failure:
    自編荒村義莊). Extensible suggestion pool (dungeon_worlds.json ∪ catalog worlds); the player may
    also free-type another IP via the ⚡ picker. Only shown in the hub (not mid-dungeon)."""
    items = []
    for w in worlds or []:
        if not isinstance(w, dict):
            continue
        n = str(w.get("name") or w.get("value") or "").strip()
        if not n:
            continue
        h = str(w.get("hint") or "").strip()
        items.append(f"- {n}" + (f"（{h}）" if h else ""))
        if len(items) >= 16:
            break
    if not items:
        return ""
    return ("## 開副本＝選一個真實 IP 世界（grounding，務必遵守）\n"
            "劇情要開試煉秘境／副本時，**絕不要自編無名場景**（村莊／義莊／廢礦／凶宅之類）。改為敘事帶出"
            "「主神鼎／諸天門戶開啟、數道世界投影浮現」，把下列【真實 IP 世界】列進結尾的 `<!--ACTIONS-->` 當選項"
            "（並可提示玩家亦能用 ⚡『進入試煉秘境』選單自選其他作品）；玩家選定後由引擎依該 IP 動態生成副本——"
            "**你只負責框架與選項，世界由玩家點**：\n" + "\n".join(items))


def build_progress_block(state: dict) -> str:
    """Prescriptive story-progress anchor — the hard fix for the GM regressing (re-running an
    already-passed 入門考核, re-staging a done beat). BOUNDED BY DESIGN: rendered from compact,
    overwriting engine fields (身份/境界/階段 = single values; completed_dungeons + a capped,
    deduped completed_milestones list; active quests self-prune) — it does NOT grow per turn like
    an append-only log. Tells the GM where the story IS so it advances forward, never re-does a
    completed milestone, and reads current identity as already-held (not yet-to-earn)."""
    if not isinstance(state, dict):
        return ""
    cur = []
    rank = str(state.get("sect_rank") or "").strip()
    if rank:
        cur.append(rank)
    realm = str(state.get("major_realm") or "").strip()
    if realm:
        minor = state.get("minor_realm")
        cur.append(realm + (f"{minor}層" if minor not in (None, "") else ""))
    phase = str(state.get("current_phase") or "").strip()
    lines = ["## 故事進度（grounding，務必遵守）"]
    if cur:
        lines.append("目前身份／境界：" + "｜".join(cur) + (f"（階段：{phase}）" if phase else ""))
    elif phase:
        lines.append(f"目前階段：{phase}")
    done = []
    cd = state.get("completed_dungeons")
    if isinstance(cd, list):
        done += [str(x).strip() for x in cd if str(x).strip()]
    cm = state.get("completed_milestones")
    if isinstance(cm, list):
        done += [str(x).strip() for x in cm if str(x).strip()]
    if done:
        lines.append("已完成（不可重演／重辦）：" + "、".join(done[-12:]))
    quests = [str(q.get("title") or q.get("id")).strip()
              for q in (state.get("quests") or []) if isinstance(q, dict) and q.get("status") == "active"]
    if quests:
        lines.append("進行中任務：" + "、".join(quests[:8]))
    lines.append("→ 承接此進度【往前】推進；上面「已完成」的事件／考核／副本**絕不重辦或重演**，"
                 "也不要把主角已具備的身份（例如已是外門弟子＝入門階段已過）當成尚未取得而重來。")
    return "\n".join(lines)


def build_ops_block(ops: list) -> str:
    """Player's deterministic UI actions since the last GM turn (購買/解鎖/⚡行動/閉關/副本).
    The v2 GM is otherwise BLIND to them → it would re-narrate or invent purchases. Injecting
    these makes the GM ACKNOWLEDGE what already happened and NOT re-do it. (harness already
    surfaces pending_ops; this is the v2 parity.)"""
    if not ops:
        return ""
    lines = []
    for o in ops[-15:]:  # cap — ack-after-turn keeps it small, but guard against a stale pile
        if not isinstance(o, dict):
            continue
        op = o.get("op")
        if op in ("buy", "purchase"):
            amt = o.get("amount")
            lines.append(f"- 已購買「{o.get('item', '')}」" + (f"（-{amt}）" if amt else ""))
        elif op == "refund":
            lines.append(f"- 已退款「{o.get('item', '')}」")
        elif op == "discard":
            lines.append(f"- 已丟棄／移除道具「{o.get('item', '')}」（請當作此物已不在身上，勿再提及）")
        elif op == "cultivate":
            lines.append(f"- 已閉關修煉「{o.get('item_id', o.get('item', ''))}」→ {o.get('mastery', '')}")
        elif op == "action":
            lines.append(f"- 已執行行動：{o.get('action', '')}" + (f"（耗 {o.get('days')} 天）" if o.get('days') else ""))
        elif op in ("enter_dungeon", "exit_dungeon", "complete_quest"):
            extra = "、".join(f"{k}={v}" for k, v in o.items() if k not in ("op", "ts"))
            lines.append(f"- 已執行 {op}（{extra}）")
        else:
            lines.append(f"- 已執行 {op}：{o.get('item') or o.get('action') or ''}")
    if not lines:
        return ""
    return ("## 玩家剛在介面做的操作（已由引擎生效：已扣款/已記錄/已解鎖）——敘事請【承接】這些既成事實，"
            "**切勿重複執行，也不要替玩家發起任何新的購買/解鎖**：\n" + "\n".join(lines))


def build_grounding_block(state_text: str, npcs: list, inventory: dict | None = None, critical_facts: str = "", vendors: list | None = None, blueprint_brief: str = "", tree_progress: str = "", time_brief: str = "", quests_brief: str = "", relationships: dict | None = None, ops_brief: str = "", current_world: str | None = None) -> str:
    block = [
        "## 當前角色狀態（grounding，請以此為準）",
        state_text or "（無）",
    ]
    if time_brief:
        block += ["", time_brief]
    if ops_brief:
        block += ["", ops_brief]
    if isinstance(inventory, dict) and inventory:
        block += [
            "",
            "## 當前道具（canonical 名稱，後續沿用完全相同字串）",
            "、".join(str(k) for k in inventory.keys()),
        ]
    block += [
        "",
        "## 當前在場 NPC（**只列與主角同場者**；canonical 名稱，含性格/口吻·與你的關係·戰力錨〔級·性質·招式〕與長期記憶——性格固定別改，關係可隨劇情演進，戰鬥照錨判定）",
        build_npc_block(npcs, relationships, current_world=current_world),
        "（關係階梯（弱→強）：" + _relmod.ladder_str()
        + "。關係隨劇情演進時，在 state_delta 設 relationships[NPC名]="
        + '{"tier":"上列之一","note":"原因/現況"}——是「設定當前等級」非加減；性格/口吻不要改。）',
    ]
    vendor_block = build_vendor_block(vendors)
    if vendor_block:
        block += [
            "",
            "## 可上架商店 vendor（catalog_upsert 的 vendor 用這些 id）",
            vendor_block,
        ]
    if tree_progress:
        block += ["", tree_progress]
    if quests_brief:
        block += ["", quests_brief]
    if critical_facts:
        block += ["", "## 關鍵事實", critical_facts]
    if blueprint_brief:
        block += ["", blueprint_brief]
    block += [
        "",
        "（命名紀律：上面的 NPC / 道具名稱是 canonical——後續一律沿用完全相同字串，不要改寫或換寫法；氛圍/環境不要建成 NPC。）",
    ]
    return "\n".join(block)


def build_moral_tone_block(in_dungeon: bool = False) -> str:
    """道德基調（反臉譜化）——逐回合重申，對抗模型『仙俠＝魔教反派/正道主角』的類型先驗
    （一句靜態設定壓不過先驗，要常常講）。本世界給完整灰度；副本內只給 IP-中立的反臉譜提醒，
    不把修真正邪塞進 IP 世界（尊重世界觀隔離）。修『正邪對立太重』。"""
    if in_dungeon:
        return ("## 道德基調（副本內·反臉譜化）\n"
                "沿用當前 IP 的善惡設定（**勿套修真正邪**）。但**有名有姓的反派別寫成純臉譜的惡**——"
                "給他動機、立場與人性層次；無名雜兵／異種（殭屍／普通鬼等）維持其設定即可，不必強加灰度。")
    return ("## 道德基調（反臉譜化，務必遵守）\n"
            "本作**不是非黑即白**：**真正的敵人是虛潮（扭曲之物），不是魔道**。\n"
            "- 魔道五宗＝抗虛潮的人族【盟友】，**不是反派**；正魔之爭是資源／話語權／秘境名額的暗流，**不是你死我活的剿滅**。\n"
            "- **別給正道自動正義光環、別把魔道一律寫純惡**：正道有腐敗、傾軋、偽善；魔道有義氣、苦衷、底線。善惡在**立場與處境**，不在「正／魔」標籤。\n"
            "- **有名有姓的 NPC 都要有自己的利益、恐懼與道理**，別寫成「為惡而惡」或「純善無瑕」。")


def build_system_prompt(persona: str, state_text: str, npcs: list, inventory: dict | None = None, critical_facts: str = "", vendors: list | None = None, blueprint_brief: str = "", tree_progress: str = "", time_brief: str = "", quests_brief: str = "", relationships: dict | None = None, ops_brief: str = "", current_world: str | None = None) -> str:
    """persona (story system_prompt) + grounding + the v2 output contract.

    `current_world` (副本名 or 『本世界』) gates NPC grounding to同場 NPCs only (母世界 NPC 不串門進副本)."""
    return "\n\n".join([
        (persona or "").strip(),
        build_grounding_block(state_text, npcs, inventory, critical_facts, vendors, blueprint_brief, tree_progress, time_brief, quests_brief, relationships, ops_brief, current_world),
        OUTPUT_CONTRACT,
    ])


def build_dungeon_seal_block(dungeon: str) -> str:
    """副本封閉原則：副本是獨立世界，母世界（青雲宗等）不會自己串門進來。外來者一律副本當地。
    只在副本內注入（修『青雲宗串門到副本』的政策面那一刀）。"""
    d = str(dungeon or "").strip()
    if not d:
        return ""
    return (f"## 副本封閉原則（務必遵守）\n"
            f"你在【副本：{d}】內。副本是**獨立封閉的世界**，與主角的母世界（修真界／青雲宗等宗門）完全隔開：\n"
            f"- 母世界的人、宗門、勢力**不會自己跑進副本**。外來者、增援、追兵、敵人一律是 **{d} 當地**的存在"
            f"（當地同道／勢力／怪物／官府／亂黨…），用當地語彙與設定，**別把青雲宗／修真宗門搬進來**。\n"
            f"- **絕不要讓母世界 NPC 主動『串門』進副本**。除非主角【明確主動】用主神空間能力召喚、或帶人同行；"
            f"若主角帶了母世界隊友，只有那位隊友在場，其餘母世界角色都留在母世界。")


# Per-turn output-length override (UI selector). "normal"/None → no directive (the
# persona's adaptive default governs); only short/long inject an explicit instruction.
# Placed at the very END of the user message (highest salience for the narrate model).
_LENGTH_DIRECTIVES = {
    "short": "[本回合輸出長度：精簡] 只演繹這個動作的直接結果，約 80–180 字；節奏明快，不額外鋪陳環境／心理／旁支（除非劇情必要）。結尾仍給 2-3 個可選行動。",
    "long": "[本回合輸出長度：精彩] 這是值得著墨的一刻，放開篇幅（約 600–1200 字）：充分鋪陳感官、情緒、張力與節奏起伏，但不灌水、不重複已演過的內容。",
}


def build_user_message(
    player_action: str,
    lore: str = "",
    recap: str = "",
    dice_hint: str = "",
    extra_blocks: list | None = None,
    scene_tail: str = "",
    length: str | None = None,
) -> str:
    """Augmented player turn: context blocks + the raw player action last.

    `scene_tail` = the END of the previous GM narration, placed immediately before the player
    action and framed as "continue from HERE, do NOT re-write/re-stage what's above". This is the
    HARD anchor against the GM re-narrating the just-played beat (a buried system-prompt rule
    wasn't enough for the cheaper narrate model) — the model sees exactly where the scene is and
    what the player does next."""
    parts: list[str] = []
    if recap:
        parts.append("[劇情摘要]\n" + recap)
    if lore:
        parts.append(lore)
    for block in extra_blocks or []:
        if block:
            parts.append(block)
    if dice_hint:
        parts.append("[命運走向（僅供 GM 參考，勿透露給玩家）]\n" + dice_hint)
    if scene_tail:
        parts.append("[劇情現況——上一幕已演到這裡（從此處【往下接續】，下面是已發生的內容，"
                     "**絕對不可重寫、重述或重演**；玩家已讀過）]\n…" + scene_tail)
    _ld = _LENGTH_DIRECTIVES.get((length or "").strip().lower(), "")
    _len_tail = ("\n\n" + _ld) if _ld else ""
    if parts:
        return ("\n\n".join(parts)
                + "\n\n---\n[本回合【只】演繹下面這一個玩家『最新』動作：把它接在上面劇情之後往下寫。"
                "上面的一切（包含玩家『先前』所有動作與對話）都【已經發生、執行完畢】——"
                "**絕對不要重演、重述、或再做一次任何先前的動作／對話／橋段**（例如先前已說過的話、已出過的招、已破過的鎖）。"
                "直接從這個新動作開始往下：]\n" + player_action + _len_tail)
    return player_action + _len_tail


__all__ = ["build_npc_block", "build_vendor_block", "build_tree_progress_block", "build_quests_block", "build_dungeon_pool_block", "build_progress_block", "build_ops_block", "build_grounding_block", "build_dungeon_seal_block", "build_moral_tone_block", "build_system_prompt", "build_user_message"]
