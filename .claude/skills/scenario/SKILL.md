---
name: scenario
description: 劇本架構師 — generate a structured, 級-calibrated, critic-verified blueprint (objectives + encounters + rewards + pacing gates + climax) for a 副本 OR a 本世界事件 BEFORE it is played, so the run has shape, stakes and pacing instead of flat improvisation. Usually spawned by /play as a subagent at 副本-entry / event-trigger; can also be run directly. e.g. "/scenario demon_tide <branch> dungeon 鬼滅" or "/scenario demon_tide <branch> event 宗門比武大會".
---

# /scenario — 劇本架構師（pre-play blueprint generator）

You are the **architect**, not the GM. You **編**（author the structure）; `/play` **演**（narrates turn-by-turn within it）. The whole point: a run improvised step-by-step goes flat and rushes to an anticlimax. You produce a **blueprint** up front — objectives, 級-calibrated encounters, tiered rewards, pacing gates, a climax — so the run has shape and the GM can't accidentally clear it in two turns.

This is v2 ([[v1-v2-split]]); use the 級 power system, never the old D~S.

## Invocation & arguments
`/scenario <story> <branch> <kind> [seed]`
- **kind** — `dungeon`（試煉秘境，某 IP）或 `event`（本世界事件，如 魔潮 / 宗門比武大會 / 宗門政爭）。
- **seed** — the IP name (dungeon) or the event name/hook (event), plus any constraint (target 級, theme).

## One schema, shared core + type overlay (NOT two schemas)
副本與本世界事件**共用核心**；只有 `kind`、`structure`、`context` 不同。

```jsonc
{
  "kind": "dungeon" | "world_event",
  "title": "...",
  "premise": "一句話前提（玩家視角的鉤子）",
  "structure": "linear_clear" | "bracket" | "waves" | "investigation" | "siege" | "open",
  "difficulty": { "target_level": "N級（當地語彙）", "calibrated_to": "主角現級＋隊伍" },

  // ---- SHARED CORE ----
  "objectives": {
    "main": [ {"id":"m1","beat":"...","reveal":"玩家如何得知此目標"} ],
    "side": [ {"id":"s1","beat":"...","optional":true,"reward_ref":"r2","discover":"如何被發現（非自動給）"} ]
  },
  "beats": [   // 脊椎：有序節點，每個帶 pacing gate
    {"id":"b1","title":"...","type":"探索|遭遇|解謎|社交|戰鬥",
     "gate":{"requires":[],"min_main_progress":0},
     "encounters":["e1"], "hooks":["s1"]}
  ],
  "encounter_pool": [
    {"id":"e1","name":"...","level":"N級","domains":["剋制|精神|詭異|因果|規則|—"],
     "concealment":"M級 | none","role":"trash|elite|boss|social",
     "weakness":"剋制/弱點","behavior":"行為傾向"}
  ],
  "climax": {"id":"c1","trigger_gate":"main_progress>=X% 且 <條件>","boss_ref":"e_boss",
             "stakes":"成敗代價","twist":"可選轉折（如偽裝者現形）"},
  "rewards": [
    {"id":"r1","tier":"普通|精良|命運","item":"...","gated_by":"通關|支線s1|隱藏",
     "level_or_effect":"級貢獻或效果","domain":"可選性質"}
  ],
  "pacing": {"min_beats_before_climax": 3, "soft_turns": "預估回合數",
             "anti_rush":"不可跳過的 gate 說明（防一下通關）"},

  // ---- TYPE OVERLAY (context) ----
  "context": {
    // kind=dungeon:    {"ip":"鬼滅","world_isolation":true,"entry_prereq":"級≥?",
    //                   "on_clear":"結算：命運點數＋可能命運道具帶回現實","death":"real"}
    // kind=world_event:{"recurrence":"每年魔潮 / 一次性","ties":["宗門關係","當前排名"],
    //                   "persistent_world":true,"on_resolve":"宗門標準/資源/功法/聲望變動"}
  }
}
```

`structure` 決定 beats 的形狀：`bracket`＝對戰樹（對手級遞升、抽籤 seed）、`waves`＝波次升級＋轉折（魔潮）、`investigation`＝線索樹、`linear_clear`＝主線節點推進（多數副本）、`siege`/`open` 視需要。

## 規模與數量（依副本規模伸縮；下限防單薄、上限防灌水）
先依副本規模/星級定量再填內容：
- **小型**（短、低星）：beats 4–5｜main 1–2｜side 2｜encounter_pool 4–5｜rewards 2
- **標準**：beats 5–7｜main 2–3｜side 2–3｜encounter_pool 5–7｜rewards 3–4
- **大型/招牌**（高星、史詩）：beats 7–10｜main 2–3｜side 3–4｜encounter_pool 8+｜rewards 4+

**硬下限（任何規模都要滿足，不足就重生）**：`side ≥ 2`、`beats ≥ 4`、`encounter_pool ≥ 4`（含 **≥1 非純戰力威脅** + **1 boss**）、`rewards ≥ 2`（**≥1 藏在支線/隱藏**）。支線要**型別不重複**（戰鬥/解謎/社交/探索別都一樣），且**至少一條與 boss 剋制或關鍵獎勵掛鉤**（如生化危機那份：支線拿到 boss 弱點/EMP）。

## Steps
1. **Ground（先讀，別憑空）**：主角現在的級/境界/功法/domain（`character_state.json`）；級錨點（`_shared/power_scale.txt` + 該世界 system_prompt 的錨點表）；該 IP / 事件的 lore（`search_lore`，必要時讀 `world_lore.json`）。沒有錨點就先補錨點（一次定死）。
2. **Design**：照上面 schema 產出 blueprint。校準規則見下。
3. **Critic（必跑）**：對照下方 checklist 自我審查；要徹底就 **spawn 一個 critic subagent** 專門挑平衡/級一致/節奏/獎勵問題，依回饋修一輪。
4. **Persist（用驗證器，別用裸 Write）**：把 blueprint 經 heredoc 餵給 `scenario_tools.py save`——它會**先做 schema 驗證**，有 hard error 就**拒寫**並回報，warning 則照寫：
   ```bash
   python3 scripts/play/scenario_tools.py save --story <story> --branch <branch> <<'SCN'
   { ...blueprint json... }
   SCN
   ```
   有 error → 照回報修正後重存；只想檢查不寫用 `validate`（讀 stdin）。turn-GM 之後用 `scenario_tools.py load` 讀回。存到 `<branch>/scenario.json`（單一 active blueprint）。
5. **Hand back**：回給 `/play` 一段**摘要**（前提、結構、主線節點數、climax 條件、關鍵威脅、可期待的獎勵）——不是整份 JSON。

## 級 校準規則
- 敵人池圍繞「**難度 target × 主角現級**」鋪：雜魚 ≤ 主角、精英 ±1 級、boss 高 1~2 級（且留**有代價/剋制的可勝路徑**，照 power_scale 的 Δ級律）。
- 至少植入**一個非戰力威脅**（精神/詭異/因果/規則/剋制 puzzle）——否則退化成比數字。
- 部分敵人帶 `concealment`（看不穿真級），製造伏擊與遐想；遵守揭露模型（相對讀數＋鑑定vs收斂）。
- boss 不只是更大的數字：給它一個 domain 或機制，逼玩家用策略/剋制。

## Critic checklist
- **級一致**：每個 encounter 的級都對得上錨點；雜魚/精英/boss 落差合理。
- **可勝且不無聊**：主角（現級＋kit＋domain）有可行解（可能需剋制/策略/代價）；沒有「無解的牆又沒逃生口」，也沒有「無腦輾」。
- **節奏**：climax 前有足夠 beats（`anti_rush` gate 存在）；不灌水、不虎頭蛇尾。
- **獎勵**：分級、有 gate；招牌獎勵藏在支線/隱藏，不是通關白送。
- **結構完整／不單薄**：主線 起→中→climax；**`side ≥ 2` 且型別不重複、至少一條與 boss 剋制或關鍵獎勵掛鉤**；`beats ≥ 4`；hooks 有接上。（不足下限 → 重生，別硬交。）
- **變化度**：不是清一色純戰力；有非遞移威脅、有 concealment 驚喜。

## Integration with /play
- `/play` 偵測到 `current_dungeon` 改變 / 事件觸發時，**spawn 本 skill 當 subagent** 生成 blueprint（重活丟 subagent，play 主 context 保持乾淨）。
- turn-GM 之後讀 `scenario.json`，**在 blueprint 內即興**：依 beats 推進、用 pacing gate 擋過快、按 reveal 規則漸進揭露——**絕不把 blueprint／級表 直接倒給玩家**。
- 進度推進、支線達成、獎勵發放都回寫 state（沿用 gm_post）。

## Anti-patterns
- ❌ 把整份 blueprint 或級表輸出給玩家（它是 GM 內部）。
- ❌ 一條線到底、沒有支線/可選內容。
- ❌ boss＝同類雜魚只是數字更大（無 domain/機制）。
- ❌ 沒有 pacing gate → 玩家三回合直奔 boss。
- ❌ 用舊 D~S；一律用級（[[power-scale-design]]）。
