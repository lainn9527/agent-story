# 角色卡模式（Card Mode）實作規格

**狀態**：提案，待實作
**寫給**：實作這個功能的 agent
**Review**：完成後由本規格的作者逐項對照驗收（見 §11）

---

## 0. TL;DR

現有系統是**世界模擬器**：一個 GM 主持整個世界，同一回合要推劇情、演 3–5 個 NPC、給選項、記帳。
本提案新增第二種形態——**角色卡模式**：整個架構只服務**一個角色**，玩家跟她一對一對話。

不是把角色卡塞進世界模擬器，而是**另建一條窄而深的管線**：介面獨立、契約獨立、grounding 只留四樣
（關係／記憶／時間／隨身物），把全部注意力預算給那一個角色。

---

## 1. 為什麼要做（問題陳述）

比對過一份 Gemini 單角色卡對話與本專案 `demon_tide` 的實際產出，差距**不在模型**
（敘事這關走 `llm_config` 的 vertex `gemini-3.8-flash`，同級），在結構：

| | 角色卡對話 | 現行世界模擬器 |
| --- | --- | --- |
| 一回合服務幾個角色 | 1 | 同場 3–5，grounding 最多 12（`prompt.py:55`）| 
| 一回合的其他義務 | 無 | 推劇情＋ACTIONS＋讓記錄員讀得出永久變化 |
| 單一角色分到的字數 | ~100% | <20% |
| 角色素材 | 具體 motif（小虎牙／142cm／繃緊的釦子／只收 Pocky／口癖「……蛤？」） | 抽象形容詞（「惜字如金，冷靜」），`branch_100067d4` 51 隻中 **0 隻有外觀**、17 隻有 memory、1 隻 personality 是 `"未知"` |

**注意力預算是零和的**——再加十條「請把 NPC 演生動」的指令也沒用。所以解法是換一套組裝規則，不是加指令。

---

## 2. 已鎖定的決策（不要自行更改）

| 決策 | 選定 | 理由 |
| --- | --- | --- |
| 掛載層級 | **完全獨立的介面** | 另開 route 與前端頁面，跟現有 RPG UI 分家；兩邊互不看見、互不影響 |
| 保留的機制 | **關係 · NPC 長期記憶 · 時間 · 隨身物** | 其餘（戰力級/domains、商店 vendor、任務、副本、功法樹、貨幣、命運骰）**全部關閉** |
| 建卡方式 | **手寫 `card.json` + 建卡精靈** | schema 可手寫；`scripts/card_create.py` 問幾題後由 LLM 展開成完整卡 |

### 2.1 一個要說清楚的成本

「完全獨立的介面」我在提案時標為最貴的一條——訊息流、串流、fork、存檔、壓縮全都要再接一次。
**降低成本的方式是：介面獨立，資料層複用。**

具體做法：**一張卡 = 一個 story**，資料仍放 `data/stories/<slug>/`，並在 `data/stories.json` 的
registry entry 加 `"mode": "card"`。這樣可以直接白拿：

- `story_io._upsert_branch_message` / `_load_branch_messages`（訊息與 index 管理、thread-safe）
- `npc_helpers._load_npcs` / `_save_npc`（角色記憶、lifecycle）
- `compaction.py`（recap 壓縮，長對話必需）
- 每則訊息的 `state_snapshot` / `npcs_snapshot`（回捲與 regenerate 的地基）
- branch tree / 存檔（v2 再開放，見 §7.3）

代價只有一個：`/api/stories` 與角色卡列表各加一行 mode 過濾（§7.1）。
**不要**另建 `data/cards/` 目錄樹——那會讓上面每一項都要重寫一次。

---

## 3. 架構總覽

```
                 ┌─────────────────────────── 獨立前端 ───────────────────────────┐
  /card          │ templates/card_index.html  卡片牆（選卡／新建）                │
  /card/<id>     │ templates/card_chat.html   對話頁：訊息流 + 右側角色卡面板     │
                 │ static/card.js  static/card.css                                │
                 └───────────────────────────────┬───────────────────────────────┘
                                                 │ SSE
                 ┌───────────────────────────────▼───────────────────────────────┐
  routes/        │ card_routes.py   /api/card/*   （新 blueprint，不動既有 route）│
                 └───────────────────────────────┬───────────────────────────────┘
                                                 │
                 ┌───────────────────────────────▼───────────────────────────────┐
  story_core/    │ card_mode/                                                     │
                 │   card.py      載卡 / 驗卡 / persona 組裝                       │
                 │   prompt.py    system prompt 組裝（persona + 四樣 grounding）  │
                 │   contract.py  敘事契約 + 記錄員指令（card 版，短）            │
                 │   ops.py       4 個工具的 registry + gate                      │
                 │   clock.py     卡內時鐘（日期/時間/天氣）                       │
                 │   pipeline.py  run_card_turn()：PASS1 串流敘事 / PASS2 背景記錄 │
                 └───────────────────────────────┬───────────────────────────────┘
                                                 │ 複用
                 ┌───────────────────────────────▼───────────────────────────────┐
  既有           │ story_io · npc_helpers · compaction · llm_bridge(串流)          │
                 │ gm_v2.relationship（關係階梯）· gm_v2.gate 的 junk-key 清洗工具 │
                 └────────────────────────────────────────────────────────────────┘
```

**明確不碰**：`story_core/gm_v2/`（世界模擬器管線）、`story_core/gm_pipeline.py`（v1，已凍結）、
`routes/core_routes.py`、`static/app.js`。card mode 只**呼叫**共用的 `story_io` / `npc_helpers` /
`compaction` / `llm_bridge`；若這些檔案需要改，先確認不影響世界模擬器路徑再改，並在 PR 說明。

---

## 4. 資料模型

### 4.1 `story_design/<slug>/card.json`（版控，卡的本體）

```jsonc
{
  "schema_version": 1,
  "id": "hanako",
  "display_name": "花子",
  "tagline": "只要膽子大，女鬼放產假",          // 卡片牆上的一行
  "pitch": "你是舊小學的夜班保全，抓不到偷零食的小偷……",  // 3–6 行情境簡介，顯示在卡片牆與開場前

  "player": {
    "role": "舊小學夜班保全",                    // 玩家扮演誰
    "premise": "再抓不到小偷就要被開除",          // 玩家的處境/動機
    "address": "你"                              // 敘事人稱，固定第二人稱
  },

  "setting": {
    "place": "偏遠郊區的舊小學",
    "era": "現代",
    "start_datetime": "2026-05-16T02:08",        // 卡內時鐘起點（見 clock.py）
    "weather_pool": ["陰", "小雨", "起霧"],       // GM 取材用，不是硬性輪替
    "ambience": ["黑冠麻鷺的呱呱叫", "日光燈管嗡嗡聲", "老水管的咕嚕聲"]
  },

  "persona": {                                   // ★ 這一段是本模式的核心，寫得越具體越好
    "identity": "三十年前死在這所小學的女學生",
    "age_note": "死時 12 歲，思考速度停在那個年紀",
    "appearance": "白襯衫＋紅色吊帶裙，黑色短髮凌亂，身體微透明",
    "body_details": ["身高 142cm", "襯衫第一顆釦子早就崩掉了", "笑起來有顆小虎牙"],
    "personality": "天然呆、好奇心強、對自己是鬼這件事很隨便",
    "speech_style": "短句、語尾拖長、想不通就直接問",
    "verbal_tics": ["......蛤？", "哦——", "嗯...等一下再想"],
    "mannerisms": ["歪頭", "腮幫子鼓起來", "用影子裡的血手接東西"],
    "desires": ["零食（尤其抹茶 Pocky）", "有人陪她講話", "被稱讚"],
    "fears": ["被忘記", "學校被拆掉"],
    "tells": ["緊張時影子會自己動", "說謊時盯著地板"],   // 壓力/情緒的外顯訊號
    "boundaries": ["不會主動殺人", "不離開學校範圍"],     // 角色的行為底線（不是內容分級）
    "sample_lines": [                            // 3–6 句範例台詞，定調用
      "「妖孽...？」",
      "「我可是死了快三十年的大前輩欸。」",
      "「不過...如果給你看了，你會給我零食嗎？」"
    ]
  },

  "opening": "……",                              // 開場敘事全文（第一則 GM 訊息，可含 pitch 之後的第一幕）

  "start": {
    "relationship": { "tier": "陌生", "note": "剛被你抓到偷零食" },
    "inventory": { "警棍": "腰間", "手電筒": "" }   // 玩家的隨身物
  },

  "nsfw": { "enabled": true, "notes": "登場角色皆為成年人設定；..." },

  "output": {
    "header": true,                              // 每回合開頭的 日期/時間/地點/天氣 標頭
    "length_hint": "200-500"                     // 建議字數區間
  }
}
```

**驗卡規則**（`card.py::validate_card`，缺一律報錯不吞）：
`id` 合法 slug、`display_name` / `pitch` / `opening` 非空、`persona.personality` 與
`persona.speech_style` 非空、`persona.verbal_tics` 與 `persona.body_details` 至少各 1 筆、
`start.relationship.tier` 在關係階梯內（用 `gm_v2.relationship.is_valid_tier`）。

> **為什麼強制 tics/body_details**：這正是現行 NPC 檔缺的東西（0/51 有外觀）。允許空欄位＝允許
> 產生一張跟現在一樣抽象的卡。寧可建卡時擋下來。

### 4.2 Runtime（不版控）

```
data/stories/<slug>/
  timeline_tree.json            # 沿用既有格式（v1 只用 main 一條）
  branches/main/
    messages.json               # 沿用既有訊息格式（role/content/index/*_snapshot）
    npcs.json                   # 只有一筆：卡角色本人（persona 欄位由 card.json 灌入，記憶會長）
    character_state.json        # 只放玩家側：{"inventory": {...}, "relationship_note": ""}
    card_clock.json             # {"start_iso": "...", "elapsed_minutes": 0, "last_weather": "陰"}
    conversation_recap.json     # 沿用 compaction.py
```

Registry `data/stories.json` 的 entry 加 `"mode": "card"`（沒有這個欄位 = 世界模擬器，向後相容）。

### 4.3 卡角色 = 一筆 NPC 記錄

卡片人格（`persona.*`）**每回合從 `card.json` 讀，永遠不寫進 npcs.json**——人格是常數。
`npcs.json` 那一筆只存**會長的東西**：`memory`（append-only）、`current_status`、
`relationship`（tier + note）。這樣改卡（修 tics、補 motif）能立刻對現有存檔生效，
不像現在的 NPC 一旦人設寫爛就被鎖死。

---

## 5. Prompt 組裝（`card_mode/prompt.py`）

一次 turn 的 system prompt =

```
[1] 角色扮演契約（card_mode/contract.py: ROLEPLAY_CONTRACT）
[2] 角色卡本體（由 card.json 展開，見下）
[3] grounding（只有四樣）
[4] 輸出格式（標頭 + 敘事；無 ACTIONS、無 STATE tag、無 DELTA 區塊）
```

### 5.1 角色扮演契約（重點條文）

- 你**只演這一個角色與她所在的場景**；沒有世界要維護、沒有劇情進度要推。
- **反應優先於推進**：玩家說一句話，先寫她的表情、身體、停頓、誤解，再寫她說什麼。
  一整回合停在同一個房間、同一分鐘之內是完全正常的。
- **每回合至少用上一個具體 motif**（`body_details` / `mannerisms` / `verbal_tics` 擇一），
  但不要每次都同一個。
- **人格是常數**：`personality` / `speech_style` / `verbal_tics` 不因劇情改變。會變的是
  記憶、當前狀態、與玩家的關係。
- **慾望驅動**：她有自己想要的東西（`desires`），會為此討價還價、糾纏、鬧脾氣。
- **認知邊界**：她只知道她會知道的事（見 `age_note` / `identity`）。不要讓她突然變得博學。
- 不輸出選項、不輸出狀態表、不輸出任何機器標記。玩家自由輸入。

### 5.2 grounding（**只有這四樣**，其餘一律不注入）

```
## 與你的關係
信任（她開始把你當成「會回來的人」）        ← gm_v2.relationship.display()

## 她記得的事（長期）
- 你是第一個說她腳趾好看的人
- 你欠她 7 包抹茶 Pocky
（最近 8 條；更早的由 recap 承接）

## 現在
2026/5/16（週六）02:32 ｜ 舊小學一樓女廁 ｜ 陰，水管在咕嚕作響

## 你身上有
警棍（腰間）、手電筒、便利商店塑膠袋（空的）
```

**禁止注入**：戰力級/domains、vendor、任務、副本、功法樹、`build_progress_block`、
`build_dungeon_*`、道德基調 block。實作時不要 import `gm_v2.prompt`。

### 5.3 輸出格式

```
📅 2026/5/16　⏰ 02:32　📌 舊小學一樓女廁　🌤️ 陰，水管低沉的咕嚕聲

（敘事正文）
```

標頭由**模型輸出**（不是後端拼），因為天氣/氛圍要跟著劇情走；後端只從輸出中解析出時間
回寫時鐘（§6）。`card.json.output.header = false` 時不要求標頭。

---

## 6. 卡內時鐘（`card_mode/clock.py`）

世界模擬器的 `world_timer` 以「天」為單位，卡模式需要「分鐘」。

```python
def now(story_id, branch_id) -> datetime          # start_iso + elapsed_minutes
def advance(story_id, branch_id, minutes: int) -> datetime
def parse_header_time(text: str) -> datetime | None   # 從模型輸出的標頭解析 ⏰ HH:MM
def sync_from_narration(story_id, branch_id, narration) -> None
    """標頭時間 > 現在 → 對齊（容忍最多 +12h 的跳躍，超過視為模型亂寫，忽略並記 log）。
       標頭時間 < 現在（跨日）→ 只在敘事明示跨日時接受。"""
```

記錄員也可呼叫 `advance_time(minutes)`（§7.2）。兩條路徑都寫同一份 `card_clock.json`，
以「取較大值」收斂，避免雙重計時（世界模擬器踩過這個坑，見 `contract.py` 的 cultivate/time 警語）。

---

## 7. 後端

### 7.1 `routes/card_routes.py`（新 blueprint，`app.py` 註冊）

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/card` | 卡片牆頁面（`card_index.html`） |
| GET | `/card/<slug>` | 對話頁（`card_chat.html`） |
| GET | `/api/card/list` | 只回 registry 中 `mode=="card"` 的項目（含 tagline/pitch） |
| GET | `/api/card/<slug>/state` | 卡片人格摘要 + 關係 + 記憶 + 時鐘 + 隨身物 + 訊息（分頁：`?after=<index>`） |
| POST | `/api/card/<slug>/send/stream` | **SSE**，body `{"message": "..."}`。事件型別沿用既有：`text` / `done` / `error` |
| POST | `/api/card/<slug>/regenerate` | 丟掉最後一則 GM 回覆重生（用該則的 `*_snapshot` 還原狀態後重跑） |
| POST | `/api/card/<slug>/rewind` | body `{"to_index": N}`：截斷訊息並用 snapshot 還原 |
| POST | `/api/card/<slug>/reset` | 回到 `opening`（清訊息、記憶、時鐘、關係） |

同時：`/api/stories`（`routes/story_routes.py:19`）加一行過濾，**不回傳** `mode=="card"` 的 story，
免得角色卡出現在世界模擬器的故事清單裡。

### 7.2 `card_mode/pipeline.py::run_card_turn`

沿用 v2 的兩段式（已驗證可行，見 `gm_v2/pipeline.py::_run_turn_tools`）：

```
PASS 1（同步，串流）
  system = 契約 + 角色卡 + grounding + 輸出格式
  model  = 設定的 provider/model（call_claude_gm_stream，跟 v2 PASS1 同一條）
  → 逐塊 emit 給 SSE → 全文 buffer → commit 成 gm 訊息（含 npcs/clock/inventory snapshot）
  → clock.sync_from_narration()

PASS 2（背景 thread，flash-lite，focused）
  只有 4 個工具：
    set_relationship(tier, note)      # tier 必須在階梯內；一回合最多升一階（沿用 v2 的循階漸進紀律）
    remember(facts: [str])            # append 到卡角色的 memory
    advance_time(minutes: int)
    set_inventory({name: status})     # 玩家隨身物；value "" = 移除
  gate：沿用 gm_v2.gate 的 junk-map-key 清洗（`_is_junk_map_key` / `_append_recover_name`），
        其餘一律 drop 並記 log。**不要**引入 set_state / purchase / quest / dungeon。
  結束後：若 compaction.should_compact() → 觸發 recap 壓縮（沿用既有）
```

失敗策略沿用 v2：串流第一塊之前失敗 → 退回阻塞式重試一次 → 再失敗回 `error` 事件。

### 7.3 v1 範圍外（明確不做）

分支樹 / fork / 存檔 / 插圖 / 命運骰 / 多人卡（一次兩個角色）/ 匯入既有 NPC 成卡。
`timeline_tree.json` 保持單一 `main` 分支即可（結構留著，之後要接 fork 不用搬家）。

---

## 8. 前端（獨立頁面）

`templates/card_index.html` — 卡片牆：每張卡一個 card（頭像佔位／`display_name`／`tagline`／
最後對話時間），點進去 `/card/<slug>`；右上角「＋ 新建角色卡」導向精靈說明（v1 可先只給 CLI 指令）。

`templates/card_chat.html` — 兩欄：

- **左（主）**：訊息流。GM 訊息渲染標頭列（📅⏰📌🌤️ 做成一行小字 chip）＋敘事正文；
  玩家訊息靠右。底部輸入框（Enter 送出、Shift+Enter 換行）、串流中顯示游標、
  最後一則 GM 訊息旁有「🔄 重生」。
- **右（側欄，可收合）**：角色卡面板——立繪佔位、`display_name`、關係階梯（含當前 tier 高亮）、
  「她記得的事」最近 5 條、卡內時間、你的隨身物。

`static/card.js`：只做這頁的事，**不要 import/依賴 `app.js`**（那支 5000 行是世界模擬器的）。
SSE 解析沿用 `app.js` 既有的 `type: text/done/error` 格式即可（照抄那段小邏輯比共用好，兩邊會分歧）。

`static/card.css`：獨立樣式；深色底、比 RPG 頁更安靜（單角色對話要的是專注，不是儀表板）。
沿用 `style.css` 的 CSS 變數色票以維持一致。

---

## 9. 建卡精靈 `scripts/card_create.py`

仿 `scripts/world_create.py` 的形狀（互動問答 → LLM 展開 → 寫檔），問這幾題：

1. 角色名 + 一句話定位（例：三十年前死在小學的女鬼，只想吃零食）
2. 玩家扮演誰 / 為什麼會遇到她
3. 場景（地點、時代、開場時間）
4. 三個具體特徵（外觀／口癖／小動作，各給例子引導）
5. 她想要什麼 / 她怕什麼
6. 分級（一般 / 成人）

→ 一次 LLM 呼叫展開成完整 `card.json`（含 `opening` 開場敘事、`sample_lines`）
→ `validate_card()` 通過才寫檔
→ 寫 `story_design/<slug>/card.json`、scaffold `data/stories/<slug>/`、註冊 registry（`mode:"card"`）

`--dry-run` 印出不寫檔。**沒有 LLM 也要能用**：`--blank` 產生填空模板供手寫。

---

## 10. 建議的 PR 切法

| PR | 內容 | 可獨立驗證 |
| --- | --- | --- |
| 1 | `card.json` schema + `card_mode/card.py`（載/驗）+ `clock.py` + 單元測試 + 一張手寫範例卡 `story_design/hanako/card.json` | pytest |
| 2 | `card_mode/{contract,prompt,ops,pipeline}.py` + 離線測試（注入假 `llm_fn`，驗證 prompt 組裝與四工具 gate） | pytest |
| 3 | `routes/card_routes.py` + SSE，用 curl 打通一輪真實對話 | curl / 手測 |
| 4 | 前端兩頁 + card.js/card.css | 瀏覽器 |
| 5 | `scripts/card_create.py` | CLI |

每個 PR 都要能單獨跑過 `python3 -m pytest -q tests/test_card_mode.py`。

---

## 11. 驗收條件（Review 會逐項對照）

**功能**

1. `/card` 只列出 `mode=="card"` 的卡；`/api/stories` **不**回傳它們。
2. 開新卡 → 第一則訊息是 `card.json.opening`，時鐘 = `start_datetime`，關係 = `start.relationship`。
3. 送出一句話 → SSE 逐字串流 → 結束後訊息已落 `messages.json`，且該則帶 `npcs_snapshot` / clock snapshot。
4. 背景記錄跑完後：關係變化落在 npcs.json 的該筆記錄、記憶 append、時鐘前進、隨身物更新。
5. 「重生」用 snapshot 還原後重跑，不會把上一輪的記憶記兩次（冪等）。
6. 連續 30 回合後 recap 有壓縮，且壓縮後角色仍記得 `memory` 裡的關鍵事實。

**品質（人工讀，這是這個功能存在的理由）**

7. 隨機抽 5 則 GM 回覆：**每則至少出現一個** `body_details` / `mannerisms` / `verbal_tics` 的具體 motif。
8. 回覆中不出現：選項區塊、狀態表、級數、任何 `<!--...-->` 標記。
9. 角色不會因為玩家一句話就跳關係階梯（一回合最多一階）。
10. 人格穩定：修改 `card.json` 的 tics 後重開對話，新語氣立刻生效（證明人格沒被寫進 npcs.json）。

**隔離**

11. `git diff` 不得包含 `story_core/gm_v2/`、`story_core/gm_pipeline.py`、`routes/core_routes.py`、
    `static/app.js` 的行為變更（純新增檔案除外）。
12. `python3 -m pytest -q` 相對本規格實作前沒有新增失敗
    （已知既有失敗：`tests/test_state_update.py::TestMigrateListToMap::test_auto_migration_on_load`）。
13. 開一局 `demon_tide` 與 `urban_daily` 各跑一回合，行為與現在一致。

---

## 12. 明確不要做的事

- 不要為了「省事」把 card mode 接進 `gm_v2.pipeline.run_turn`——那條路上的每個 grounding block
  都是世界模擬器的義務，接進來就會把注意力預算又吃回去，這個功能就白做了。
- 不要把戰力級 / domains / 任務 / 副本 / 商店「先留著以後可能會用」。要留的話留在 card.json 的
  註解裡，不要進 prompt。
- 不要新增 `data/cards/` 目錄樹（理由見 §2.1）。
- 不要動 `story_design/_shared/*.txt`——那些是世界模擬器的共用區塊，card mode 有自己的契約。
- 不要在 `card.json` 允許空的 `verbal_tics` / `body_details`（§4.1 的驗卡規則）。

---

## 13. 附錄：現行 NPC 素材密度實測（動機佐證）

`data/stories/demon_tide/branches/branch_100067d4/npcs.json`，51 筆：

```
personality  49/51   但內容是「惜字如金，冷靜」這類抽象形容詞，1 筆為 "未知"
speech_style 49/51   「短句，直接，沒有廢話」
background   44/51
appearance    0/51   ← 沒有這個欄位
memory       17/51   內容是劇情流水帳（「第四日率先穩住水之呼吸節律兩刻鐘」），不是慾望/傷口
```

`story_design/_shared/npc_acting.txt` 要求 GM 參考「口頭禪」「行為傾向」「壓力反應」「近期經歷」
「可聊話題」——**其中四項在 v2 的 NPC schema 根本不存在**（`gm_v2/ops.py:80`），指令空轉。

> 這個問題在世界模擬器那邊也該修（補人設欄位 + 背景補完判官），但**不屬於本規格範圍**。
> card mode 的 `persona` 結構可以當作那次修法的欄位設計原型。
