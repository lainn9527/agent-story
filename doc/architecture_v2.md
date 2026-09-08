# v2 架構：確定性引擎 ⊕ 生成式旁白

> 這是 v2 的北極星。新功能一律照這份歸位：先問「它是**底層**還是**模組**？是引擎的職責還是旁白的職責？」
> 對照圖：[architecture_v2.svg](architecture_v2.svg)　·　邊界決策的「為什麼」：[design_decisions_v2.md](design_decisions_v2.md)

## 1. 核心原則

回到專案起點的 root cause——「單靠 1-round LLM 維護 state」會壞（state 漂、lore 錯、退場 NPC 復活、NPC 失憶）。v2 的解法一句話：

> **引擎握真相／資源／規則（確定性、可測、免費）；旁白（LLM）握文筆／判決／內容生成。
> LLM 永不直接改 state——只能發 `delta`，經 `gate → apply` 由引擎落地。**

心智模型：**一個傳統文字 RPG 骨幹 ＋ 一個非常強的旁白 ＋ 受約束的無邊界 RNG。**

## 2. 兩條輸入路徑

| 路徑 | 觸發 | 怎麼跑 | 成本 |
|---|---|---|---|
| **確定性路徑** | 選單動作（練功／購物／接任務／移動／休息） | 直接打引擎端點，**不經 LLM** | 即時、免費、不漂 |
| **生成路徑** | 自由文字（選單外的行動） | LLM turn：`ground → generate → gate → apply → critic` | 一次 LLM 延遲 |

兩條都寫**同一個 state**；引擎是唯一真相來源。大部分回合應該走確定性路徑（這直接解決「一回合等 1 分鐘」）。

## 3. LLM 連接在哪（只在這些縫）

| 玩家做的事 | 引擎（確定性） | 旁白（LLM 生成） |
|---|---|---|
| 購物／兌換 | 扣錢、給物（catalog buy） | —（不需要） |
| 練功（閉關） | 扣天數、推魔潮、算「成」（time_tools cultivate） | 突破體感／異象 |
| 接任務 | 登記、發獎、檢查條件（任務表） | 任務鉤子／對白 |
| 移動／休息 | 扣時間、改位置 | 路途見聞（可選） |
| NPC 互動 | 關係值、記憶、好感門檻 | **對話本身**（grounded 在關係/記憶） |
| 戰鬥 | 級差＋骰子定**趨勢/勝負傾向** | 過程描寫＋創意招式**判決** |
| 世界觀／場景 | 當前 location/state 事實 | **場景描述** |
| 副本／事件 | blueprint 的 beats/gates/獎勵（跑流程） | **設計**（scenario_gen 預生成） |

**LLM 兩種模式**（分清楚）：
1. **渲染**：玩家點了選單動作 → 引擎已結算 → LLM 只把「已發生的轉移」寫成故事，**不決定結果**。
2. **判決／生成**：自由行動、對話、場景、內容生成。

**「無邊界 RNG」的收斂**：傳統 RPG 的 RNG 是骰表（有界），LLM 是無限輸出空間。
**引擎骰子定趨勢（有界），LLM 只填質地（無界）**；自由行動**仍花資源、仍被級/骰/資源約束**，否則玩家打「我一拳打爆魔王」旁白就照演。引擎提供的級差/骰/代價＝綁住旁白的繩子。

## 4. 分層：底層 / 模組 / 內容

**判準：換一個完全不同類型的世界（賽博龐克／克蘇魯／武俠），它還會一模一樣存在嗎？會＝底層；不會＝模組。**

**更精準的切線是「機制 vs 實例」**：kernel 是*機制*（抽象、程式碼、世界無關）；某世界的*實例／詞彙／數值*是 content。
- `命運點數` **不是 kernel**——它是「資源帳本」這個機制的一個**實例**（欄位 `fate_points`）。賽博龐克換 eddies、克蘇魯換理智值，帳本機制不變。
- `魔潮` 是「時間 deadline」機制的一個實例；`修真階梯` 是「級框架」的一個 ladder 實例。

kernel 是「**有**資源帳本／**有**時間 deadline／**有**境界階梯框架」這件事本身。模組＝成組的實例＋少量 schema；content＝純資料實例。

### 底層 Kernel（跨世界不變式，永不 per-world 抽換）
| 機制 | 做什麼 | 檔案 |
|---|---|---|
| 時間經濟 | 世界時鐘、deadline（魔潮）、action 成本、deadline 觸發 | `scripts/play/time_tools.py` |
| 資源帳本（state 數值欄位） | 確定性 spend/grant、餘額檢查（**命運點數＝實例**，非 kernel） | character_state + catalog/time spend |
| 級 / power scale | magnitude 的跨世界共同尺 | `story_design/_shared/power_scale.txt` |
| 骰子 / 命運 | 約束旁白的有界 RNG | `story_core/dice.py` |
| state＋delta/gate/apply | LLM↔引擎契約（提議→處置） | `story_core/gm_v2/{gate,apply,contract}.py` |
| NPC 生命週期＋長期記憶 | active/offstage/terminal、memory/archive、召回 | `story_core/npc_helpers.py`、apply |
| 旁白契約管線 | ground→generate→gate→apply→critic | `story_core/gm_v2/pipeline.py` |

> 時間是底層的範例：所有時間流逝（閉關、趕路、副本、`time` delta）**都路由到同一個時鐘**並觸發同一套 deadline，沒有任何一條 by-pass（否則 `time:{days:90}` 會悄悄跳過魔潮）。

### 模組 Modules（per-world / per-system，可插拔可組合）
| 模組 | 是什麼 | 檔案 |
|---|---|---|
| 功法樹 / 能力體系 | catalog 節點（tier+requires）；青雲/噬魂/鬼滅/火影/生化＝實例 | `scripts/play/catalog_tools.py` + `data/stories/<>/catalog.json` |
| 商城 / vendor | 定價兌換的店家 | 同上 |
| domain 詞庫 | 每世界的特殊軸（剋鬼/血鬼術/查克拉…） | `story_design/<world>/*`、`_shared/xianxia_anchor.txt` |
| 錨點階梯 | 境界→級 對照（修真階梯、鬼階、忍位） | `_shared/xianxia_anchor.txt`、world system_prompt |
| 副本 / 事件 blueprint | 預生成的劇本骨幹 | `story_core/gm_v2/scenario_gen.py`、`scripts/play/scenario_tools.py` |
| 任務系統 | home world 日常任務 | **（待建）** |

模組**插進底層**：功法樹用到 級（等級）＋時間（閉關成本）＋資源（兌換價）＋state（成）；blueprint 用到 級（校準）＋時間＋獎勵。

### 內容 Content（純資料／prose 實例）
`data/stories/<world>/*`、`story_design/<world>/*`、具體功法節點、NPC、副本——authored 或 LLM 生成的資料。

## 5. 一個 kernel／模組會影響什麼？（不只 UI——更是 prompt）

每個 kernel/模組有**三張臉（＋選用的 UI）**。「會不會影響 prompt」答案是：**會，而且 prompt 就是它的其中兩張臉。**

1. **引擎**：確定性 ops＋state（buy/cultivate/advance、gating、欄位）。
2. **grounding（prompt 的*讀*側）**：把現況注入系統提示，讓旁白「看得到」。
   例：`tree_progress`（功法樹）、vendor block（商城）、`time_brief`（時鐘/魔潮）、級/domain 錨點（persona `@include`）、`blueprint_brief`（副本）、NPC 戰力錨＋記憶。
3. **delta 契約（prompt 的*寫*側）**：旁白要能改它，就得有 delta 欄位＋gate。
   例：`cultivate`↔功法樹、`catalog_upsert`↔商城、`npcs`↔NPC kernel、`state_delta`/`time`↔state/時間。
4. **UI（選用）**：**一種範式**——遊戲內狀態一律 header 按鈕→資料驅動 modal（⚔ 戰力／💰 商城／🌳 功法樹／⚡ 行動／📋 事件任務），核心數值用 menu bar 常駐 chip（角色狀態，嵌在 header；點開看詳情 modal）；drawer 只放 session 管理（故事/分支/存檔/設定）。**內容實例不另開專屬 widget**（魔潮＝deadline 事件，併入 📋，不寫死 header badge）。理由見 [design_decisions_v2.md](design_decisions_v2.md) D7。

**關鍵領悟：prompt 不是獨立的東西——它就是 kernel＋模組「給旁白看的投影」。** 架構圖上那兩條箭頭（grounding 讀、delta 寫）＝prompt 的讀寫兩側。一個模組**若不注入 grounding，旁白就瞎了**（踩過的坑：NPC domain 沒 re-ground → GM 打到一半忘了剋制）。

→ 所以「加一個模組」的完整 checklist **不是「資料＋UI」**，而是：
> **引擎 ops ＋ grounding 注入器 ＋（若旁白要能改）delta 欄位＋gate ＋（選用）UI。**

**例：事件／任務怎麼套這四張臉**（「魔潮」就是這樣）：
- **事件（scheduled）**：`deadline` 是通用「排程事件」機制（kernel）；**魔潮只是 config 實例**（label／週期／on_fire），不是特例。引擎到點自動 fire；`time_brief` 把它注入 grounding（讀側）→ 旁白**讀了就會演**：軟性「距 X N 天」可鋪陳氣氛，硬性「已觸發，本回合必須演出」逼它落地。**不需要 delta 欄位**——事件由引擎 fire，旁白只負責演（headers/badge 一律讀 `label`，不寫死「魔潮」）。
- **任務（quest）**：旁白可在劇情中**即時生出新任務**（開地圖：`quest_add` delta → 引擎持久化）；但**進度/完成/獎勵的算數由引擎**（採幾株、boss 死沒、發多少）——不賭 LLM 數得對。

**通則（兩個軸別混）**：
- **創造**＝生成式：旁白在遊玩中**開地圖**（新任務/NPC/地點/副本即時生成），經 `delta`／`catalog_upsert`／`scenario_gen` **寫進引擎**。
- **創造後的擁有＋記帳**＝確定性：引擎記住它存在、並算進度/完成/級差/餘額。

所以**不是預先備好一切**——引擎是「**會長大的存檔**」，LLM 邊玩邊往裡寫。delta 寫側是**主要的成長管線**（不是邊角）；已落地四條：`npcs`／`lore`／`catalog_upsert`／`scenario_gen`。唯一死守確定性的是「**算數與判定**」，不是「創造」。這正是 root cause 的精準解：問題不是「LLM 不該創造」，而是「LLM 創造了卻沒被確定性記住 → 漂/消失」；修法是**持久化它創造的東西**。

## 6. 加一個新世界的配方

**不要碰底層。** 只產出模組實例（資料）：
1. 錨點階梯（native 境界 → 級）
2. domain 詞庫（該世界特殊軸）
3. 功法樹（catalog 節點＋requires）／或沿用主角母系統
4. 副本 blueprint（scenario_gen）
5. vendor（若該世界有商店）

→ 時間 / 級 / 骰子 / state / NPC / 旁白管線 **全部免費沿用**。

**新模組 checklist（四張臉，照 §5）**：
1. **引擎 ops＋state**（確定性，app-free 的 `*_tools.py`）
2. **grounding 注入器**（往 system prompt 餵現況，否則旁白瞎）
3. **delta 欄位＋gate**（只有「旁白要能改」才需要；玩家 UI 動作不需要）
4. **UI**（選用：面板/選單）

## 7. 動作骨幹：如何新增一顆動作按鈕 / 動詞

確定性輸入路徑的**單一機制**。一顆按鈕 = **動詞（executor，引擎確定性）＋ 名詞（它作用的實例，可 GM 生成）**。分清這兩者，就知道該「提前做」還是「遊戲中長」。

### 三條來源，一張表（`scripts/play/action_tools.py`）
`list_actions(state)` 掃下列來源、用 `available(state)` 過濾，回傳「當下可做」的動作；`do_action(id, args)` 路由執行。
1. **結構動詞 `STRUCTURAL_ACTIONS`（程式宣告）** — enter/exit_dungeon…。每筆宣告 `id/label/icon/category/desc`、`available(state)`（引擎算可達性，**GM-independent**）、選用 `params`（UI picker）、`executor`（→ `gm_v2.toolexec.execute_tool`，**與 GM function-calling 同一條確定性路徑**）。
2. **config 動作（資料：`time_economy.json.actions`）** — 趕路/休整/宗門任務/採藥。耗時＋gain，純資料、零程式。
3. **動態動作（計算）** — 每個「已習得且未圓滿」功法各長一顆 `cultivate:<node>` 閉關。

### 配方 A — 加一顆按鈕（用既有動詞）
- **名詞**（一件商品 / 一個副本 IP / 一個功法節點）：用既有動詞生出來即可，**自動上架成按鈕，不寫任何按鈕碼**。
  例：GM `upsert_catalog_item` 上架 → 💰 立刻可買（走確定性 `buy()`）；catalog 新增帶 `world` 的 vendor → 「進入試煉秘境」picker 自動多一個 IP。
- **新的結構動詞按鈕**：在 `STRUCTURAL_ACTIONS` append 一筆（`id/label/available/params/executor`）。executor 必須是**既有的確定性工具**（toolexec `_FC_ONLY` 或 ops 註冊表）。UI（`static/actions.js`）依 `available` 自動渲染、依 `params` 自動長下拉。

### 配方 B — 加一個新動詞 / module（四張臉，照 §5）
只有**全新確定性機制**（如煉丹：N 藥材→1 丹、引擎算成功率）才需要。**YAGNI：某世界需要才做。**
1. 引擎 op＋state（app-free `*_tools.py`）→ 2. grounding 注入 → 3.（GM 要能觸發才需）delta 欄位＋gate → 4. UI。
然後在 toolexec 註冊 executor、在 `STRUCTURAL_ACTIONS` 加一顆按鈕指向它。

**已建範例＝任務(quest)模組**（就是照配方 B 長出來的，可當樣板）：
- ① 引擎：`quests`（top-level state list）＋ `apply.apply_quest_add`（創造）＋ `apply.complete_quest`（**確定性結算**：標記狀態＋精確發放**開任務時宣告**的獎勵，走 `apply_state_delta`＝引擎算術；冪等不重複發）。
- ② grounding：`prompt.build_quests_block`（注入進行中任務＋id，GM 才看得到、才會推進）。
- ③ delta/工具：`add_quest` op（創造＝生成；delta＋FC 雙前端，gate `gate_quests`）＋ `complete_quest`（FC-only，結算＝確定性）。**「創造」是 GM 的、「結算/算術」是引擎的**——同 exit_dungeon/purchase 紀律。
- ④ UI：📋 事件追蹤面板顯示任務（不碰 v1 events）＋ ⚡「完成任務」結構動作（quest picker，配方 A 一筆 entry）。
- 關鍵：獎勵在 **開任務時就宣告**、完成時引擎照宣告精確發 → LLM 永不做獎勵算術。

### 為什麼按鈕可信：引擎獨佔欄位（kernel 不變式）
`available(state)` 只有在「它讀的 state 不會被 GM 寫歪」時才可信。所以**邊界/資源/進度欄位只准確定性轉換寫、GM 在 gate 被 DROP**（`story_core/gm_v2/gate.py` `_ENGINE_OWNED`）：
- `current_dungeon`、`completed_dungeons`、`technique_mastery`、`technique_days`，以及 `current_phase` 的 `主神空間/副本中/副本結算`。
- 引擎路徑（`apply.enter_dungeon`、結算）**直呼 `apply_state_delta` 繞過 gate**，所以鎖**只擋 GM、不擋引擎**。
- 結果：**「GM 忘了改 `current_dungeon`」結構上不可能**——改它從來不是 GM 的事；欄位翻動 ⟺ 確定性動詞跑過。根治了反覆出現的 `current_dungeon` 漏清 ＋ beat-id 汙染 `current_phase`。
- GM 仍可敘事驅動 `死亡/傳送中`；NPC / lore / 劇情道具等「漏掉可降級」欄位維持 GM 可寫（沈浸感來自敘事，不來自寫 state）。

## 8. 現況

- **底層已就緒**：time_tools、gate/apply/contract、dice、npc 生命週期/記憶、pipeline、power_scale、命運點數、**引擎獨佔欄位鎖（§7）**。
- **模組已就緒**：catalog 功法樹（5 棵）、商城、domain 詞庫、修真錨點、scenario_gen。
- **動作骨幹已就緒**：**動作註冊表 + 選單 UI（§7）** — data-driven `available(state)`、結構動詞（副本進/出、完成任務）＋ config 動作＋動態閉關、picker、點擊直打引擎不經 LLM。
- **任務系統已就緒（輕量）**：quest 模組（§7 配方 B 範例）——`add_quest` 創造、`complete_quest` 確定性結算發獎、grounding 注入、📋 面板＋⚡「完成任務」。進度自動偵測（boss 死沒等）仍未做，現靠玩家/GM 觸發結算。
- **缺口**：
  1. **雙輸入路徑** — 選單動作→引擎；自由文字→LLM turn（現在自由文字全走 LLM；選單已是確定性路徑）。
  2. **任務進度自動偵測** — 目前完成由玩家『完成任務』或 GM `complete_quest` 觸發；條件式自動完成（採滿 N 株等）未做。

## 9. 文字版架構圖

```
                         ┌───────────────────────────┐
                         │        玩家 / UI           │
                         └──────┬──────────────┬──────┘
            選單動作(確定性)     │              │   自由文字(生成)
                                ▼              ▼
                                │      ┌──────────────────────────────┐
                                │      │  旁白 NARRATOR (LLM)           │
                                │      │  ground→generate→gate→apply→critic │
                                │      │  連接點：NPC對話·場景·戰鬥判決·副本生成 │
                                │      └───────┬──────────────▲───────┘
                                │       delta  │   grounding  │ (讀真相)
                                │   (gate→apply)│              │
                                ▼              ▼              │
   ┌────────────────────────────────────────────────────────┴──────┐
   │  引擎 ENGINE（確定性 · 無 LLM · 即時免費 · 唯一真相）            │
   │  ┌── 模組 MODULES（每世界可插拔）──────────────────────────┐  │
   │  │ 功法樹 · 商城 · domain詞庫 · 錨點階梯 · 副本blueprint · 任務 │  │
   │  └──────────────────────────────────────────────────────────┘  │
   │  ┌── 底層 KERNEL（跨世界不變式）────────────────────────────┐  │
   │  │ 時間經濟 · 命運點數 · 級 · 骰子 · state+delta/gate/apply ·   │  │
   │  │ NPC生命週期/記憶 · 旁白契約管線                              │  │
   │  └──────────────────────────────────────────────────────────┘  │
   └────────────────────────────────────────────────────────────────┘
```

## 10. 一句話總結

整個 v2 是把責任**從 LLM 搬進確定性引擎**的過程。底層是搬完、再也不該回到 LLM 的「物理」；模組是插在物理上、每世界可換的「內容系統」；旁白只在縫隙處生成與判決，且永遠經 delta 落地。
