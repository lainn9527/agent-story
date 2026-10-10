# Prompt Design（LLM 互動設計）

本文件說明目前 codebase 裡，和 LLM 互動的 prompt 組裝方式、輸入輸出契約、以及擴充時的注意事項。

## 1. Prompt 架構分層

目前有 4 個主要 prompt 面向：

1. GM 主 prompt（每回合）
2. 事件後處理抽取 prompt（async extraction）
3. Lore Console 對話 prompt（含提案 tag）
4. Auto-play 專用 prompts（角色生成、玩家行動、摘要）

---

## 2. GM 主 prompt（遊戲主流程）

### 2.1 來源與模板

- 優先讀取：`story_design/<story_id>/system_prompt.txt`
- 若不存在，退回：`story_core/prompts.py::SYSTEM_PROMPT_TEMPLATE`
- 組裝函式：`app.py::_build_story_system_prompt()`

### 2.2 主 prompt 注入資料

`_build_story_system_prompt()` 會將下列資料注入模板：

- `character_state`: 核心角色狀態精簡文本（schema fields + systems + core extras）
- `narrative_recap`: 壓縮摘要（`conversation_recap.json`）
- `world_lore`: 精簡 lore 摘要（非全文）
- `npc_profiles`: 當前分支「active NPC」統計摘要（詳細檔案改由 state RAG 按需注入）
- `team_rules`: branch config 的組隊模式規則（`free_agent` / `fixed_team`）
- `critical_facts`: 關鍵事實區塊（phase、world day、關鍵道具、NPC 關係與 tier，以及 `story_anchors` 的「長期記憶」）
- `dungeon_context`: 副本進度/節點/成長限制上下文

### 2.3 模式切換對 prompt 的影響

- `fate_mode = false`：
  - 會把「命運走向」段落從 system prompt 移除
  - 最近訊息會先做 fate label 清理（避免模型模仿）
- 最近訊息（`recent`）一律會先移除非玩家訊息（`gm`/legacy `assistant`）尾端的選項區塊。
  - 若 GM 依契約在選項區塊前輸出 `<!--ACTIONS-->`，系統會優先從該 marker 起裁掉尾端選項。
  - 編號選項列只有在明確帶 `可選行動` / `你打算` 標頭時才會剝除，避免一般戰利品清單、步驟列舉被誤判。
  - `- **「...」：**` 形式的選項列仍會照舊移除。
  - `當前處境` 這類敘事段落會保留。
- `pistol_mode = true`：
  - 追加「親密場景指示」段落
  - 追加 NSFW 偏好（chips/custom）
- `branch_config.image_gen_enabled = false`：
  - 追加「禁止輸出 IMG tag」系統段落，避免模型繼續發出插圖指令
- `branch_config.image_gen_enabled = true`：
  - 追加「每次 GM 回覆都必須輸出且只輸出一個 IMG tag」系統段落
  - 即使是過場、情報或純對話回合，也要求模型為當前最具代表性的場景出圖
- `branch_config.image_model`：
  - 追加目前圖片模型提示（預設 `gemini-2.5-flash-image`），方便同一故事不同分支使用不同產圖模型

### 2.4 玩家訊息 augmentation（送進 LLM 前）

函式：`app.py::_build_augmented_message()`

注入順序固定為：

1. `[相關世界設定]`（base lore 混合搜尋）
2. `[相關分支設定]`（branch lore bigram 搜尋）
3. `[舊副本關聯角色提醒]`（一次性 dungeon return recall，非 blank branch）
   - 只在 `dungeon_return_memory.json.pending_reentry_dungeon == current_dungeon` 時嘗試注入
   - 只選 `home_scope=dungeon_local`、`home_dungeon=current_dungeon`、`archived+offstage+eligible` 的 NPC
   - 依 relationship / `last_seen_msg_index` / tier 排序，最多 4 名、約 350 chars
   - 第一次嘗試注入後就清掉 pending（有候選或無候選都一樣）
4. `[長期關鍵事件]`（sticky events，非 blank branch）
   - 來自 `events.db.sticky_priority > 0`
   - 依 `sticky_priority DESC, id DESC` 取最多 4 條
   - 可包含 `resolved` 事件；用途是長期 plot pressure，不依賴當前 query 命中
5. `[相關事件追蹤]`（query-matched active events，非 blank branch）
   - 只取 `planted/triggered`
   - 仍維持最多 3 條，避免和 sticky layer 混淆
6. `[GM 敘事計劃（僅供 GM 內部參考，勿透露給玩家）]`（`gm_plan.json`，非 blank branch）
   - 顯示 `arc` + 最多 3 個 `next_beats` + 最多 2 個 `must_payoff`
   - `remaining_turns` 於注入時計算：`ttl_turns - (current_index - payoff.created_at_index)`
   - 只保留 `remaining_turns > 0` 的 payoff
   - 注入段落硬上限 `GM_PLAN_CHAR_LIMIT`（預設 500 chars）
7. `[NPC 近期動態]`
8. `[相關角色狀態]`（state.db 檢索結果）
   - 預設檢索限流：總筆數最多 30、NPC 類別最多 10
   - `must_include_keys` 命中的條目先保留，再填入一般結果
   - archived NPC 預設不注入；玩家明確提名時可透過 `must_include_keys` 召回
9. `[戰力等級提醒]`（僅當存在 tier 已知且分類為 ally/hostile 的 NPC）
10. `[命運走向]`（若 fate mode 開啟且非 `/gm` 指令）
11. `---`
12. 原始玩家輸入

補充：

- lore 檢索 query 不是只用原始玩家輸入。
  - 會先保留原始玩家輸入。
  - 再附加 `current_dungeon`。
  - 再附加「最近幾則 GM 回覆或玩家輸入中明確提到」的 active NPC 名稱。
  - 再附加 recent GM 回覆中的高訊號 CJK terms（優先保留副本名、NPC 名、事件名、術式/道具名）。
- GM 的可選行動區塊契約：整段回覆只允許一個尾端選項區塊。模型必須先輸出 `<!--ACTIONS-->`，再接唯一的一個 `**可選行動：**` 或 `**你打算：**` 標頭與編號選項；正文其他位置不得再出現任何額外的行動選項或同類標頭。`<!--ACTIONS-->` 會保留在 runtime 訊息中供 recent/compaction 清理使用，但前端顯示時會隱藏。
- `[相關世界設定]` 會用這個 expanded query 做 keyword + embedding 混合排序，並套用最低 keyword/RRF 分數門檻，避免弱命中條目塞滿 token budget。
- `[相關分支設定]` 會用同一份 expanded query 做 normalized bigram overlap 搜尋，並額外考慮 query terms 對 `topic/subcategory/content` 的命中；budget 估算以 CJK 約 1 char ≈ 1 token 計，避免低相關的舊分支設定過量注入。

### 2.5 戰力等級一致性（Tier Consistency）

- `system_prompt.txt` 內新增「戰力等級敘事指南（演出落地）」：五大等級 D/C/B/A/S 的具體演出維度與反面檢查。
- `story_core/prompts.py` fallback 模板同步保留精簡版等級框架，避免 fallback 路徑退化。
- NPC tier 採 15 個 sub-tier：`D-/D/D+/C-/C/C+/B-/B/B+/A-/A/A+/S-/S/S+`。
- `_save_npc()` 會做 tier allowlist 正規化；不合法值直接忽略、不覆蓋既有合法值。
- NPC metadata 新增 `lifecycle_status` / `archived_reason` / `archive_kind` / `origin_dungeon_id` / `origin_run_id`；缺省視為 active，archived NPC 預設不進常駐 prompt。
- NPC provenance metadata 另新增 `home_scope` / `home_dungeon` / `return_recall_state` / `last_seen_msg_index`，用於「回舊副本喚回 cast」的提示層，不直接影響 state RAG。
- `_save_npc()` 會依 `current_status` 關鍵詞區分 `archive_kind=terminal|offstage`；若同一個 `origin_run_id` 的 offstage NPC 在後續 GM 回覆又被抽到，會自動轉回 active，並移除 relationship 上的 ` (已歸檔)` 註記。
- `_save_npc()` 在副本內首次記錄 NPC 時，會預設 `home_scope=dungeon_local`、`home_dungeon=current_dungeon`；在主神空間首次記錄則預設 `home_scope=main_god_space`。
- 若既有 NPC 是 `archived + offstage + eligible`，且角色目前重新回到 `home_dungeon`，後續新的 NPC update 會自動 re-activate；舊的 `same origin_run_id` reactivation 仍保留為 fallback。

### 2.6 State RAG（角色狀態按需注入）

- state index 來源：`story_core/state_db.py`（branch 級 `state.db`）。
- 預設 token 預算：`STATE_RAG_TOKEN_BUDGET=2000`（最小 200）。
- 類別覆蓋：inventory / ability / relationship / mission / system / npc。
- `must_include_keys` 會從玩家輸入提取已知實體名做保底納入（忽略長度 < 2 的 key，避免噪音）。
- NPC row 若帶 `NPC|ARCHIVED` tag，預設從檢索結果過濾；forced key 命中時仍保留。

---

## 3. GM 回覆後的 tag 契約

函式：`app.py::_process_gm_response()`

### 3.1 同步 tag（立即處理）

- `STATE`：套用角色狀態更新
- `LORE`：寫入 branch lore
- `NPC`：合併 NPC
- `EVENT`：寫入 events DB
- `IMG`：非同步生成圖片
  - 會先檢查 branch config：
    - `image_gen_enabled = false`：只移除 IMG tag，不會觸發下載
    - `image_gen_enabled = true`：prompt 會要求每回合輸出單一 IMG tag；若模型真的輸出 IMG tag，則觸發下載，且 `image_model` 優先使用 branch config 指定值
- `TIME`：推進 `world_day`

### 3.2 回覆清理

在存回前會先：

- 移除模型回聲的 context 區塊（`[相關世界設定]` 等）
- 移除 fate label（安全網）
- 去重複獎勵提示（保留最後一條）
- `ACTIONS` marker 不會在此步移除；它保留在訊息內供後續 recent/compaction 清理使用，但 `_extract_tags_async()` 會吃去掉尾端選項後的版本。

### 3.3 Snapshot

每則 GM 訊息會附加 snapshot，供 fork/edit/regen 精準回溯：

- `state_snapshot`
- `npcs_snapshot`
- `world_day_snapshot`
- `dungeon_progress_snapshot`

### 3.4 State Index 同步點

- canonical state 寫入最終匯集點是 `_apply_state_update_inner()`；套用後會同步 state.db 非 NPC 類別。
- `_apply_state_update()` 若偵測到 `current_dungeon` 轉場，會額外 reconciliation `dungeon_progress.json`；validation 後的最終 state 也會再同步一次 state.db，避免 growth cap 後索引殘留舊值。
- NPC 寫入透過 `_save_npc()` 同步 state.db（含 `lifecycle_status` / `archived_reason` / `archive_kind` 的 runtime 決策，以及 `NPC|ARCHIVED` tag）；手動刪除 NPC（`DELETE /api/npcs/<id>`）也會同步刪除索引。
- fork/edit/regen/blank/merge 分支操作會用 snapshot 對應的 state/npcs 重建 state.db，避免「分支時間點」與「索引內容」不一致。

---

## 4. Async Extraction Prompt（第二層結構化）

函式：`app.py::_extract_tags_async()`

特點：

- 使用兩個並行的 `call_oneshot()` 解析剛產生的 GM 內容，但套用順序固定為序列化
- 只在 GM 文本長度 >= 200 時啟動

目前拆成兩條 async extractor：

1. `non-state`
   - 輸出：`lore / event_ops / events / plan / npcs / story_anchors / time / branch_title / dungeon`
2. `state-only`
   - 只輸出 `state_ops`（legacy `state` 僅作 defensive fallback）
   - 只允許提取「本回合結束後仍然成立」的穩定角色狀態

兩條 extractor 的推論可並行，但套用順序固定為：

1. 先套 `non-state` 中不碰 `character_state.json` 的部分（lore / events / plan / npcs / time / branch_title / dungeon）
2. 取得 per-branch character-state lock
3. 套 `story_anchors`
4. 套 `state-only`
5. 釋放 lock

### 4.1 Prompt 內建 guardrails

- lore 提取有「通用設定」門檻，避免把一次性劇情寫進知識庫
- event 優先走 `event_ops`（id-driven），避免 title 漂移造成斷鏈
- event create / legacy events 仍保留 title dedup + status 升級規則（planted -> triggered -> resolved/abandoned）
- event 可附 `sticky: true`，只用於 cross-arc plot pressure；實際寫入為 `sticky_priority=1`
- `story_anchors` 是獨立的 identity memory layer，格式為 `{add, update, remove}`；大多數回合應輸出空物件
- `story_anchors.update/remove` 必須 exact-match 既有 anchor，避免 extractor 漂移式改寫長期記憶
- plan 會注入「上一輪 gm_plan 摘要」讓模型可延續或重寫；`must_payoff` 會依 `event_title` relink 到當前分支 active events
- NPC 提取支援 `tier` 欄位；若既有 NPC 本回合無法判定 tier，應省略該欄位（不要用 null 覆蓋）
- `_extract_tags_async()` 會在啟動時先抓當前副本 run context（`dungeon_id` + `entered_at`），並在 `_save_npc()` 時帶入，避免背景執行緒晚跑時吃到錯誤的副本來源
- `state-only` 專門處理 `state_ops`（set/delta/map_upsert/map_remove/list_add/list_remove），fallback 才接受 legacy `state`
- `state-only` prompt 仍保留完整的 state 寫入 contract，而不是只講禁止事項：
  - 明確定義 `state_ops` 的 `set / delta / map_upsert / map_remove / list_add / list_remove` 結構與範例
  - 保留 `current_phase` 合法值、`completed_missions` 嚴格約束、`relationships` 心理狀態追蹤、inventory 清理原則、abilities 維護原則
- `state-only` 明確禁止把一次性爆發、超限、邊緣觸及、位格不穩、外物催化等描述寫進永久 `systems / base_power_level / gene_lock`
- `state-only` 的 canonical update 在套用前會再經過一次保守 guard：
  - 只檢查 candidate value 本身，不掃整段 GM 文本
  - 會丟棄帶有 `暫時 / 邊緣 / 觸及 / 強行 / 超限 / 過載 / 催化 / 不穩 / 波動` 等語義的 `systems.* / base_power_level / gene_lock`
  - `abilities_add` 也會額外過濾 `爆發版 / 感悟版` 等一次性升階描述
- `state-only` v1 不允許 async 更新 `current_dungeon`
  - `current_dungeon` 仍由同步 `STATE` tag、`/api/dungeon/*`、既有 reconciliation 流程負責
  - `dungeon` 抽取仍保留在 `non-state`，用來更新 `dungeon_progress.json`
- time 有上限（單次最多 30 天）
- dungeon 進度提取時會給 node id 對照

### 4.2 特殊模式

- `pistol_mode = true` 時，會跳過 lore + events + plan 持久化（避免污染長期資料）

---

## 5. Lore Console Prompt

路由：`POST /api/lore/chat/stream`

設計：

- 系統 prompt 會塞入全量 lore 分類與條目
- 允許模型輸出提案 tag：
  - `<!--LORE_PROPOSE {...} LORE_PROPOSE-->`
- 後端會解析提案並把 tag 從顯示文本移除
- 若 provider 為 Gemini，會加 `googleSearch` tool 提供 grounding

---

## 6. Auto-play Prompt 設計

檔案：`story_core/auto_play.py`

### 6.1 角色生成 prompt

- `_CHAR_GEN_PROMPT`
- 輸出固定 JSON 結構（personality/opening_message/character_state）

### 6.2 Player AI prompt

- `_PLAYER_SYSTEM_PROMPT`：角色性格 + recap + 角色狀態 + phase hint
- `_PLAYER_TURN_PROMPT`：最近 4 則對話，要求第一人稱 50-150 字行動

### 6.3 Auto summary prompt

- 檔案：`story_core/auto_summary.py`
- 每 5 回合或 phase 轉換時，背景生成 JSON 摘要
- 對話壓縮（`story_core/compaction.py`）在送摘要前也會移除非玩家訊息尾端的選項區塊；優先使用 `<!--ACTIONS-->` marker，否則編號列同樣要求明確 `可選行動` / `你打算` 標頭才會剝除，避免一般清單被固化成錯誤刪除。

---

## 7. Prompt 觀測與追蹤

- `LLM_TRACE_ENABLED`（預設開）
- `LLM_TRACE_RETENTION_DAYS`（預設 14）
- `_trace_llm()` 會記錄 request/response payload（best-effort）
- `_log_llm_usage()` 會寫 token usage 到 `usage.db`

### 7.1 Trace 檔案位置與命名

- 根目錄：`data/llm_traces/<story_id>/`
- 分區：`<YYYY-MM-DD>/<branch_id>/<msg_tag>/`
- 檔名：`<HHMMSS.mmm>_<stage>_<id>.json`
- `msg_tag` 來自 `message_index`（例如 `msg_000407`）；若無 index 則為 `msg_na`

### 7.2 常見 stage

- GM 主流程：`gm_request` / `gm_response_raw`
- 後處理抽取：`extract_tags_request` / `extract_tags_response_raw`
- state key normalize：`state_normalize_request` / `state_normalize_response_raw`
- lore promote review：`lore_promote_review_request` / `lore_promote_review_response_raw`
- lore console：`lore_chat_request` / `lore_chat_response_raw`
- lore organizer：`lore_organizer_request` / `lore_organizer_response_raw`

### 7.3 Debug 建議

- 先用 `story_id + branch_id + message_index` 找對應 `msg_tag` 資料夾。
- 同一個 `msg_tag` 下通常會有同回合 request/response，可直接比對 prompt 與模型輸出。
- trace 可能包含完整 prompt、recent messages、原始回覆，排查時請避免外流敏感資料。

---

## 8. 修改 Prompt 的實務建議

Card Mode 手動「編輯正文」不經敘事 prompt／Recorder／回憶查詢，只將最新正文保存為新的回覆版本，既有狀態與記憶保持原樣。後續敘事／回憶只讀正式時間線採用的正文，未採用舊版留在候選庫；多角色的原知情片段位置隨文字修改搬移，不重新判讀知情或更新記憶。涵蓋替換點的舊摘要失效，沒有額外模型呼叫。詳見 game_mechanics 的回覆候選與 api_reference 的 edit-text。

1. 優先改 `story_design/<story_id>/system_prompt.txt`，不要先硬改 fallback。
2. 新增 tag 前先定義：
   - regex 抽取規則
   - 儲存位置
   - 與 async extraction 的交互（避免雙寫衝突）
3. 若調整 state 規則，務必同步檢查：
   - `_apply_state_update_inner()`
   - `_normalize_state_async()`
   - `tests/test_state_update.py`
4. 若調整抽取 prompt，至少跑：
   - `tests/test_extract_tags_async.py`
   - `tests/test_tag_extraction.py`
5. 若調整 tier 或 relationship 正規化規則，需同步更新：
   - `app.py::_normalize_npc_tier()` / `_rel_to_str()`
   - `story_core/state_db.py` 內對應 helper（目前為避免循環 import 採 duplicated logic）

---

## 9. GM v2 單次 grounded 契約（平行路徑，實驗中）

對應 `POST /api/v2/send` 與 `story_core/gm_v2/`。與第 2~4 節的「主 prompt + sync tag + 雙層 async 抽取」並存，但理念相反：**GM 在單次呼叫裡同時敘事並「自己宣告」結構化狀態變更**，不再由第二個 LLM 逆向 prose。

### 9.1 輸出契約

GM 先寫敘事（同主 prompt 規則：第二人稱、繁中、結尾 `<!--ACTIONS-->`），最後輸出恰好一個 delta 區塊：

```
<!--DELTA
{ "state_delta": {...}, "npcs": [...], "lore": [...], "time": {...}, "recap": "..." }
DELTA-->
```

- marker 用 HTML comment，前端本就隱藏；`parse.py` 容錯切分（缺/壞只記 note，不影響敘事）。
- `state_delta`：同既有 canonical 形狀（直接欄位 / `*_delta` / `*_add`/`*_remove` / map upsert，`""`/`null` 移除）。只放永久變化；一次性爆發/暫時/邊緣只進敘事。
- `npcs`：顯式 `lifecycle_status`（active/offstage/terminal）+ `memory_add`（append-only 長期記憶）。
- `lore`：只放通用設定；不同步入庫，交給 critic 審。

### 9.2 Grounding

`prompt.py` 把當前 state + active NPC（含 `memory[]`）塞進 system prompt，所以 GM 是「看著現況」寫 delta，不是盲寫——這移除了舊雙層抽取的逆向誤差。

### 9.3 兩段式 evaluation（取代 lossy guard）

- **`gate.py`（deterministic，無 LLM）**：丟結構錯（scene/型別/off-schema），flag 高風險（`base_power_level`/`systems`/`gene_lock`、terminal+memory、lore）。**不做 transient-marker 砍值**（舊 `_sanitize` 的誤砍正是 state-update-incomplete 來源）。
- **`critic.py`（async LLM）**：離開關鍵路徑審 flagged，撤銷被誤升的暫時狀態、決定 lore 入不入庫。

### 9.4 與舊抽取的對照

| 舊（§4 雙層抽取） | v2 |
|---|---|
| `_extract_tags_async` 第二讀者 | GM 自己出 delta |
| `_sanitize` / `_run_state_gate` / `_review_state_update_llm` | gate(B) + async critic(C) |
| `_save_npc` 關鍵字 reactivation | 顯式 lifecycle |
| 無 per-NPC 記憶 | append-only `memory[]` |

---

## Debug Panel Prompt（V1）

路由：`POST /api/debug/chat/stream`

- 使用獨立 Debug system prompt（診斷/修正助手），與主 GM prompt 分離。
- 每次呼叫會注入：
  - 主聊天最近 `RECENT_MESSAGE_COUNT` 則（唯讀摘要）
  - `character_state.json`（完整）
  - `npcs.json`（含 archived）
  - `world_day`
  - `dungeon_progress.json`
  - `gm_plan.json`
  - 當前 `debug_directive.json`（若存在）
- Debug 對話歷史由 server 端管理（`debug_units/<unit>/chat.json`），送 LLM 時僅取最近 20 則。
- V1 目前直接注入完整 state / NPC / dungeon JSON，尚未做獨立 token budget；大型故事有 context 爆量風險。

### Debug Tag 契約

- 修正提案：`<!--DEBUG_ACTION {...} DEBUG_ACTION-->`
  - `type` 支援：`state_patch` / `npc_upsert` / `npc_delete` / `world_day_set` / `dungeon_patch`
  - frontend 會沿用後端的 normalization 思路處理 alias payload，歷史訊息重載時也適用
  - 目前接受的 state 類 alias 包含：
    - `{"type":"state_patch","update":{...}}`
    - `{"update":{...}}`
    - `{"action":"state_patch","patch":{...}}`
    - `{"state_patch":{...}}`
- 劇情指令：`<!--DEBUG_DIRECTIVE {"instruction":"..."} DEBUG_DIRECTIVE-->`
  - apply 後寫入 branch `debug_directive.json`
  - 下次主 GM 回合注入 `_build_augmented_message`
  - 該回合完成後由 `_process_gm_response` 自動清除（一次性）

---

## 角色卡模式（Card Mode）Prompt 設計

Card Mode 將注意力預算全部保留給單一角色，摒除世界模擬器的雜項負擔（見 `doc/card_mode_proposal.md`）。

### Prompt 四層結構 (`story_core/card_mode/prompt.py`)

1. **角色扮演契約 (`ROLEPLAY_CONTRACT`)**：
   - 只扮演單一角色與當前場景，無推劇情/維護世界義務。
   - 反應優先於推進：先寫微表情、停頓、身體反應，再寫對白。
   - **具體外觀與肉體細節（Physical & Sensory Grounding）**：每回合自然帶入具體肉體與外觀描寫（身形骨架、膚質觸感、微敞或緊繃的衣著狀態、光影下的線條、體溫或呼吸起伏）；肢體接觸或近身靠近時生動刻劃肌膚觸感與衣料摩擦。
   - **性癖與情慾心理觸發（Fetishes & Psychological Triggers）**：觸及角色性癖與特定偏好時，展現強烈內心動搖與神態回饋（眼神躲閃、耳根泛紅、喉頭吞嚥、反差羞恥或順從沉溺）。
   - **敏感點的即時生理反饋（Sensitive Spots & Weak Points）**：敏感部位被觸碰、吹氣或刺激時，第一時間給予細膩生理反饋（短暫屏息、背脊發麻、指尖抓皺衣料、喉間輕喘、體溫上升），並與心防相互拉扯。
   - 人格為常數：`personality` / `speech_style` / `verbal_tics` 不受存檔影響；變更的是記憶與關係。
   - 慾望驅動與認知邊界保護。
   - 零機器標籤：不輸出任何 ACTIONS / STATE / DELTA 標記，純自然語言對話。

2. **角色卡人設展開**：
   - 角色身份、外觀、身體細節（`body_details`）、性癖偏好（`fetishes`）、生理敏感點（`sensitive_spots`）、個性、口吻、口癖、小動作、慾望、恐懼、Tells、底線、範例台詞。
   - 玩家身份、處境、稱呼，以及場景地點與氛圍池。

3. **Grounding（嚴格僅 4 項）**：
   - **與你的關係**：`gm_v2.relationship.display()`。
   - **她記得的事（長期）**：近期原文開始前的最新 36 條（缺少邊界快照時沿用最新 36 條）、最多 36 條依當前話題檢索的舊記憶、最多 36 條隨機舊記憶，按原順序注入；recap 另保留當前約定。
   - **現在**：年月日、星期、時間 ｜ 地點 ｜ 天氣與氛圍。
   - **你身上有**：玩家隨身裝備與物品狀態。
   - **絕對禁止注入**：domains、戰力級、商店 vendor、任務、副本、功法樹、道德基調等。

4. **輸出格式**：
   - 標頭格式：`📅 YYYY/M/D　⏰ HH:MM　📌 地點　🌤️ 天氣氛圍`（由模型輸出，後端據此同步時鐘）。
   - 建議字數長度提示。
   - **小說化承接（opt-in，`card.json.output.weave_player_input: true`）**：追加 `WEAVE_PLAYER_INPUT_RULES`，要求只串接 GM 正文就能讀成完整小說——玩家本輪台詞（含替其他角色寫的）視為表達素材，像忠於原意的翻譯，以「」台詞保留每句的意思、重要資訊、立場、情緒與順序，允許修正錯字、重組語序並依人物口吻潤飾措辭、語氣與節奏，不要求照抄原句。不得改變否定、條件、不確定性或承諾程度，也不新增表態／決定；有表達作用的口語、猶豫與停頓保留，明示逐字保留的引文或指定用語照原文。動作改寫成敘事、玩家角色的心聲保留為其內心，給 GM 的指示照做但不入文，並與角色反應依時間交錯。此段優先於契約中「不必逐句複誦玩家原文」與日常節奏示例；未開啟的卡不受影響。目前 `lixiaolan_v2` 開啟。保存的玩家輸入仍是原文；潤飾只發生在生成的 GM 正文。

### 蘇曼角色卡外觀一致性

`story_design/suman/card.json` 的外觀定位為成年、豐滿圓潤、白皙細嫩；
`appearance`、`body_details`、相關動作描述與 `opening` 保持一致。
外觀細節隨端杯、撥髮、坐下等動作擇一兩處呈現，避免每回合重複完整清單。
角色卡由 Card Mode 每回合載入；既有對話與摘要不會因此自動改寫，新的開場白只用於新局或重置。

### Gemini / Vertex / OpenRouter 歷史訊息邊界

`gemini_bridge._build_contents()` 同時承接兩種呼叫慣例：舊世界流程的 recent
尾端包含本輪原始 `user` 訊息，需以 augmented user input 替換；Card Mode 的
recent 則只包含已完成歷史，尾端為上一則 `gm` / `assistant` 回覆，必須保留。
只在尾端角色為 `user` 時移除待替換訊息，不能無條件切掉最後一則；否則開場與
上一回合結果會從模型輸入消失，造成場景重設、動作重演與重問。
Gemini 的串流、非串流、工具呼叫以及 Vertex 均共用此轉換函式；OpenRouter
bridge 使用相同邊界規則轉成 OpenAI-compatible messages。

Card Mode 的演繹契約同時要求從上一回合結尾的位置、姿勢與物品歸屬接續；
對追問提供新資訊或明確回應，不重演初見反應。小動作、口癖與外觀介紹不是
每回合必填項，簡短追問可低於建議字數，避免用重述前情湊長度。

### 角色卡的日常敘事與事實摘要

Card Mode 的共用契約改為近距離對話：依最新一句接話，動作與反應有輕重，
外觀只選與當下動作相關的细節。環境不替情緒升級，不以宏大比喻或關係宣判收尾，
也不代寫玩家動作或將拒絕解讀成同意。偏好不是強制反應的開關。

`card_mode/recap.py` 提供角色卡專用的初次與再次壓縮模板；pipeline 透過
`compaction.compact_async` 的可選模板參數傳入。世界模式仍用原有模板。
卡片摘要只保留具體事實、明說資訊、承諾完成情況、位置及物品歸屬。
舊摘要注入時明確標示只取事實、不模仿文風，既有對話不改寫。

記憶版本 2：摘要依歷史量以 3000–6000 字為目標（短歷史可更短），分成當前有效約定、重要經過、未完成事項、最後場景。約定需保留當事人、關係含義、接受／拒絕、履行情況；只有明確新事實才能更新當前節，舊版本移至歷史。這是事實記錄，不指定角色未來行為。

角色卡摘要輸入不再截斷每則 1000 字，改為以完整訊息分批（每批約 60000 字元，單則更長仍完整保留）。摘要超過 8000 字元時再次整理為約 4000–6000 字。全批成功才提交；失敗保留原摘要。每批開始及最終提交核對 revision，撤回的工作不能回寫。舊版摘要首次觸發時從現存完整時間線重建，成功後寫入 `recap_version: 2`。

GM 原文窗口採彈性回溯：至少最後 10 則；不足 8000 字元時往前補完整訊息，額外回溯受 12000 字元與 40 則上限限制。短句不跳過、不裁切單則、不跨過過大的前一則去挑更舊訊息。最後 10 則與所有尚未被摘要覆蓋的訊息是保底，可能超過上述上限，避免摘要延遲造成空洞；清空摘要後暫時帶入完整保留歷史。單卡與 ensemble 共用此選取器，世界模擬不變。

`card_mode/memory.py` 使用中文字元 bigram／英文詞的稀有度加權重合，取最近 36 條、檢索最多 36 條有正相關分數的舊記憶，再從其餘舊記憶無放回隨機抽最多 36 條。單角色敘事會將最近 36 條這組改為近期原文開始前快照中仍存在於當前 memory 的最新 36 條；相關及隨機組仍使用原本候選池，不改抽樣方式。邊界採實際 context_window 首則訊息：user 用 npcs_snapshot，gm 用 pre_npcs_snapshot；包含摘要落後而多帶入的原文。缺少有效角色快照時沿用原選法；空記憶快照代表沒有更早記憶，不補近期條目。三組最後去重，可能重疊而不足 108 條，不額外補滿。記錄員和多角色模式維持原選法。完全相同文字保留最後一次記錄；不足不補滿相關名額。每次組裝 prompt（包含 send／regen）重新抽樣，可能偶然抽到相同內容；相同輸入下最近與相關組保持穩定。最後按記錄順序排列，不代表事件發生時間。抽樣僅使用呼叫端提供的目前分支、該角色 memory 列表，不另查其他角色或來源卡，也不保證既有記錄的知情判斷正確。隨機舊事為可選背景，不要求敘事提起。這是字面檢索，不是語意向量搜尋。記憶保持歷史順序、標示舊事未必仍有效；當前有效約定由 recap 保留。記錄員同時讀玩家原文、GM 回覆與相關舊記憶，避免只看旁白漏掉玩家的明確表態。

### Card Mode 可選回覆長度

前端 `response_length` 由送出／重新生成路由傳至 pipeline 與 prompt builder。
短為 200–500、中為 500–1000、長為 3000 字，正文不含標頭。
明確選擇時以該範圍為目標，取代一般的「可低於建議字數」提示。
長篇增加台詞、具體動作與當下細節，不靠重述、代寫玩家或宏大比喻填字；
這是模型生成目標，並非硬截斷或自動補字。未傳選項的舊呼叫維持卡片預設行為。

### 感官細節與自然反應

Card Mode 共用契約以有來源、有後續的觸感、溫度、聲音、距離與表情增加感官密度。
細節與角色台詞交錯，依動作延續而非掃描外觀或堆疊同義形容詞；
角色感受不等於玩家感受，也不自動代表關係升級。保留非露骨與雙方自主性邊界。
短中長的正文目標不變，長篇依同一互動展開，不靠極端反應、重演動作或放大場景填字。

成年角色在換鞋、行走、居家等相關場景可加入非露骨的鞋履、裸足與踩地細節。指甲油顏色存於各卡 `persona.body_details`，區分手指甲和腳趾甲；遵守視線、鞋襪遮擋和顏色連續性，不每輪硬插或為展示安排脫鞋。

### 自由文字玩家檔案

角色卡可在 `player.profile` 保存一段自由文字，不要求姓名、年齡等各自完整填寫。
非空字串會每輪注入玩家身分旁；空值或未提供時維持原有行為。
未寫的資訊保持未知，背景不代替玩家當下的選擇與感受。
`aila` 卡以此提供艾迪的姓名、年齡與工作背景。

### Card Mode 人工整理 lore（單卡試用）

分支的 `card_lore.json` 保存已整理條目，未啟用自動收錄。單卡 send、regenerate 與 edit 共用的敘事管線在角色 prompt 後加入 `card_mode/lore.py` 選出的內容；Recorder 不讀此區塊，不新增模型呼叫。常駐核心之外，本輪輸入關鍵字權重高於當前地點，再以最近四則對話補充搜尋；帶入條目的單層 related 連結，去重後最多六條非核心條目、正文總計最多4500字元，不隨機抽取。

整理內容以延續遊戲氛圍為目的：保留穩定世界背景、宗門生活與資源壓力、人物身分及長期動機、會影響演出的重要能力條件。近期進度、突破層數、百分比、一次性辦案過程、回禮與來訪承諾由記憶和原文承接，不重複收成 lore。功法保留特色、作用與必要代價，不為整理而擴充數值或層級表；避免逐條堆疊防幻覺提醒。精簡不刪除原始對話或已有記憶，也不代表另有程序自動將刪除的 lore 補寫成記憶。

每個來源保存 GM 訊息 index 與正文 SHA-256，只有本分支保留原文仍符合時才取用。分支複製獨立檔案；rewind、reset、regen 移除或替換来源時條目自動失去取用資格，避免同編號候選或未來設定污染歷史。資料是導演背景，不授予所有角色共同知識；傳聞、推測與動態現況依來源及更新原文解讀。沒有取用不等於設定不存在。此版未接多角色副本及 Pi 自動維護。

Pi 核對試驗由 `card_mode/pi_lore_review.py` 及 `scripts/lore/pi_review*.mjs` 執行，每次只給一條有原文來源的 lore 與其本線來源快照。只開放 `read_snapshot`、`submit_review` 兩個工具，不開放一般檔案讀寫、命令或 MCP。完整讀取以 2400 字元分頁及區間覆蓋驗證，每條最多16次讀取；全文讀完才提供逐句 claim 清單。每句需標 supported／conflict／insufficient；前兩者需引用逐字存在的原文，資料不足不強判一致。工具拒絕缺頁、漏句、重複核對及不存在的引用。

主程序另外重播 Pi 的工具事件，與不可變快照比對讀取結果及提交；一般文字宣稱核對完成不當結果。逾時、輸出截斷、工具結果不符、未完成提交均拒收。報告完成只表示讀取與引用通過檢查，不保證模型語義判斷正確；`all_supported` 與完整核對分開。執行後再驗原文／lore 是否更換，過期報告標 stale。此試驗只產出暫存報告，未接自動寫回或增量維護，不改正式遊戲資料。

### Card Mode 場景連續性

時鐘同時辨識 emoji 標頭與 `YYYY/MM/DD（星期）HH:MM ｜ 地點 ｜ 天氣氛圍` 的整行文字標頭（亦接受半形 `|`）。文字標頭須有完整日期與兩個分隔符，不從一般正文中的約定或計畫取時間。啟用標頭時，敘事 prompt 要求提供本輪演出結束時的狀態，依實際活動估算耗時；短暫對話可不加時，用餐、練習、旅程、睡眠及授權快轉反映實際時長，不固定每輪增加一兩分鐘。這是模型演出指引，不是引擎強制加速。

「現在」使用保存的 `last_location` 與卡內時間。舊存檔缺少地點時，從最近 GM 標頭讀取，再回退至卡片開場設定。有效時間標頭同步後立即保存訊息時鐘快照；背景記錄員收到同步後時間，其 `advance_time` 呼叫會被忽略，避免同一回合重複加時。完整日期不套用無日期標頭的 12 小時限制，也不因正文提到「明天」而自行跨日。

### Card Mode 回退後的摘要失效處理

角色卡 rewind、regenerate、reset 在更換時間線前清空衍生摘要，保留帶有新 revision 的空摘要。訊息索引可能重用，因此不只比較 compacted_through_index。背景壓縮在排程時捕獲 revision，開始與落盤時再次核對；不符即丟棄，避免舊路線摘要回寫。失效與背景提交使用短鎖，不等待模型生成。下一回合先使用角色資料與最近訊息；後續壓縮依保留的歷史重建，不延續舊摘要。API 回應格式不變。

角色卡回退／重生若只刪除摘要範圍外的近期訊息，保留整份已完成摘要但輪換 revision，阻擋舊背景工作覆寫。只有切入摘要涵蓋範圍或 reset 才清空。

### 選用多角色副本

`story_core/card_mode/ensemble.py` 僅在 `ensemble.json` 存在時啟用。一般回合只有 **一次統一敘事＋一次背景記錄**；不再呼叫 `ensemble_scene`，也不逐人生成。統一敘事者依玩家自然台詞、移動與最近原文決定誰回應，一次完成連貫場景與單一標頭，UI 焦點不自動變更。串流直接轉發 provider 的文字片段，敘事完整成功後保存對話並回 `done`，不等待背景記錄。

統一敘事輸入包含所有角色的完整 persona、個別 relationship／current_status，及各自以最近 36＋相關舊 36＋隨機舊 36 選出的記憶，按角色 ID／姓名分開標示，每份記憶只選取一次、沒有再附全量 archive。這是共享敘事輸入，**不是模型輸入層的私密隔離**。規則要求每人只用自己的記憶、實際參與的對話及明示通訊，不以他人私事冒充自己知道；是否遵守仍依模型理解與人工驗收。

來源摘要固定為副本建立時的版本，作為有歸屬的導演背景。最近原文使用 `context_window`，保留最近窗口及摘要尚未覆蓋的全部對話，包括背景記錄仍 pending／error 的回合。角色資料是最近成功整理的狀態；若與原文衝突，敘事必須承接原文已發生的換場、關係或約定。副本尚未自動壓縮新歷史，長局輸入仍可能增長。

`ensemble_recorder.py` 在背景依回合順序整理所有人的記憶、關係、近況及 scene_events。記錄輸入同樣按角色選取記憶，逐项附本回合逐字 evidence；未知角色、無效出入場引用拒絕整筆記錄，有效但不在該角色參與時段的記憶不寫入。人設及焦點不能由記錄員修改。這是歸屬與引用檢查，不是語意正確性的形式證明。

scene_events 的 evidence 至少四字、在本回合唯一；以引用結尾為進出場邊界。`ensemble_visibility` 保留每人參與的 user/gm 原文字元範圍，供個人回憶查詢；pending 回合暫存空範圍，不預先授權其他人的原文。remote 缺少可驗證片段時保守不提供查詢原文，個人已記錄記憶仍可用。統一敘事本身使用完整導演原文，不能再宣稱和個人回憶共用隔離視野。

每則新 GM 保存唯一 `ensemble_turn_id`、`ensemble_record_status`、前後快照及手動控制紀錄。模型記錄呼叫不持有遊戲檔案鎖；回寫時重新取得鎖、確認同一 turn ID 仍存在。成功後從該回合重建後續快照，重播明示切焦點／presence／新增角色操作，避免漏掉較早才完成的記憶，也不覆蓋較新的手動操作。regen／rewind／reset 移除的回合不接受遲到結果，重用 message index 亦不相容。

記錄失敗保留對話、標記 error，後续記錄等待使用者重試整理；敘事仍可繼續。`retry-recording` 只重新整理已保存原文，不重新生成或重送玩家台詞。待辦存在存檔，服務重啟後讀 state／操作時可恢復 pending 工作；error 不自動無限重試。每張故事有獨立跨程序記錄鎖，避免兩個 worker 同時整理。

敘事與記錄用量分別以 `ensemble_gm`、`ensemble_recorder` 即時入帳，失敗或回退不刪已產生用量；無新 metadata 不重複計費。串流傳輸中斷若 provider 回傳部分文字的 `done(incomplete=true)`，多角色管線仍視為失敗，不提交半段故事；其他既有呼叫者可忽略新增旗標。自動／深度回憶啟用時，按角色授權的額外查詢仍在敘事前進行，不能將此模式宣稱為固定兩次呼叫。

### 選用主動回憶（單卡及多角色）

單卡另支援同一代理查詢人工整理 lore：JSON actions 新增 `lore_search`、`lore_read`，context 提供來源仍有效的條目目錄（不含正文）及當前地點。auto 可依最近對話與玩家行動辨認代詞或「請繼續發展」需要的世界設定，不要求玩家說出條目名稱；deep 同樣可查。歷史原文仍使用原有知情範圍，lore 另標為導演背景，不轉成角色共同知識。目錄或搜尋摘要不當證據；單次讀取最多2400字元，長條目必須讀齊所有區間才注入，完整條目合計最多4500字元。與既有本地 lore 選取去重。off 不呼叫查詢模型，仍保留本地核心／場景設定取用；沒有 lore 的卡片沿用往事功能。

單卡 send、edit 與 regen 使用本線原文快照核對 lore 來源；regen 的預先查詢排除即將被替換的 GM，失敗時保留原回合。本輪結果 `lore_sources` 只保存實際完整讀取內容；未讀完、無效 id、已回退或候選更換的來源不納入。不增加一個獨立 lore 決策模型，但查詢動作會使用既有 auto 四步／deep 八步的模型往返額度。這仍是敘事前查詢，不是敘事生成途中即時工具呼叫；多角色暫不開放 lore 查詢。

`card_mode/recall.py` 在敘事前執行受限 JSON action loop：模型可 `search`、`read`、`finish`，由程式執行唯讀工具，再把結果交回模型決定下一步。所有 provider 使用 `call_oneshot`；Gemini／Vertex 額外傳入 JSON MIME、action schema 與 function calling NONE，避免模型把文字中的 search/read 誤當 native function calling 而回傳 `MALFORMED_FUNCTION_CALL`。其他 provider 仍走文字輸出並由程式驗證。這不是單次問模型自己編一份回憶，也不授予檔案或狀態修改權限。最終只把程式取出的原文切片交給敘事，模型自行寫的摘要不作為引用證據。搜尋有命中但未讀原文時，不能直接 finish；必須先 read，或明示 no_relevant_history 放棄不相關命中，避免看過摘要卻沒把原文交給敘事。

自動回憶將「你還記得／初見／旅行／告白」等詞語作提示，仍由模型判斷語意；深度回憶可在沒有關鍵詞時主動尋找共同經歷。一般模式完全不增加查詢呼叫。auto 最多 4 次、deep 最多 8 次決策，結束或預算耗盡即交回敘事，不無限迴圈。空白／格式錯誤或無效 action 每位角色每回合最多額外重試一次，保留已讀原文與已執行步驟、不重複執行工具；仍失敗則中止，不偷偷改成一般模式。Gemini／Vertex 的失敗結束原因寫入服務日誌，失敗呼叫與重試用量也逐次記錄。Vertex 暫時性 429／5xx／連線逾時另有每位角色每回合共兩次重試，等待 1、2 秒，不消耗格式修正的一次機會；若仍失敗，顯示服務暫時無法使用，不冒充查無資料。

全文採中文 bigram／英文詞的稀有度加權檢索，並可依 oldest/recent 瀏覽；不是向量搜尋，模型可改詞再查。長訊息分塊檢索，讀取保留原文位置及後續 offset，最終來源帶 message index、角色與原文位置。不使用永久索引，所以回捲、重生只會看到目前保留的快照，不會從來源卡讀取副本建立後的其他進度。

檢索來源仍是歷史紀錄，不是對所有往事的真實性背書：原 GM 曾補造的回憶不能因再次搜到就視為獨立佐證。敘事需區分引用、計畫、否定與已發生場景，以後來明確變更為準。找不到的細節保持未知。是否適時查詢、自然使用、遵守語意仍需真模型人工驗收。

多角色的個人回憶使用 `ensemble.authorized_history(include_compacted=True)`，統一敘事另外使用導演可見原文。`ensemble_visibility` 指定每人於 user/gm 原文可讀的字元區間；每段分別檢索、閱讀，出處 offset 保持原訊息座標，不能經前後文展開越過未參與區間。新 metadata 缺少該角色範圍時不退回全文；舊資料才 fallback 整輪 audience，副本起點前僅原主角色可讀。remote／舊跨場回合缺少明確授權片段時保守不提供原文，已記錄的個人記憶仍由原 grounding 帶入。

### 角色卡分支的上下文邊界

單卡敘事、記錄、回憶與摘要全部使用請求捕獲的 branch_id。新分支 card_full_snapshot 直接讀自己的完整歷史，不再讀父線最新內容。分叉只複製截止點以前有效的摘要；不完整的摘要範圍清空，context_window 使用保留原文，避免未來事件進入新線。

多角色敘事、個人回憶的可見區間與背景記錄均讀同一分支 ensemble.json。記錄 worker 的鎖與排程 key 包含分支，結果只回写捕獲路線，不受 active branch 切換影響。分叉不修改 prompt 的角色演繹規則；資料隔離不等於模型永不補造往事。

### 單卡背景記錄取消

每個 GM 保存唯一 card_turn_id。正式 Recorder 的四種工具透過 apply_card_tool 操作私有 state／NPC／clock 候選，不在模型往返期間持有分支鎖，也不逐項修改存檔。完成後取得短鎖，確認同一 turn ID 仍 pending，才寫入狀態、快照並排摘要；失敗或回合已刪除不提交候選。同 index 的 regen 使用另一個 turn ID，舊成功／失敗都不能污染新回合。直接寫檔的自訂 recorder_fn 測試／整合 hook 仍以鎖保護，非正式模型路徑。

### 多段玩家輸入的預設演出

Card Mode 共用 ROLEPLAY_CONTRACT 以「最新整則輸入」為單位承接；同一輸入中的多個動作／話題／場景預設按順序演出角色的具體回應，不只從最後結果繼續。與「先前回合已演完不重演」及「一般一輪一個小互動」明確區分。單卡與統一多角色敘事皆使用此契約。篇幅先精簡修飾而非刪除前段；背景回顧、假設、尚待同意或選擇的後段不強制實現。玩家明示快轉／概述仍優先，不需要另加「請逐一演出」。此為模型行為規則，不是程式強制分段。

同一則輸入可包含多次對話交換，依問句、試探、邀請、停頓、遞出／展示物品等互動辨認回應點，先演對方接住前句，再承接玩家已提供的下一句，不將整則输入合成獨白或等說完才一次回覆；角色還未聽見後句時，不提前使用後句資訊。小說化潤飾保留不同對話時點，不因可改語序而併掉回應空間。連續說明、完整告白及明示不中斷／等說完再回應則保留連貫，不把每個句號或換行都當插話要求。原有尚待同意／選擇／條件時停止及不代寫玩家規則仍適用。

### 角色卡自然收尾

共用契約的「自然停筆」以本輪內容演完為停止點；依既有敘事視角，最後一段可為台詞、動作、內心、場景或沉默，沒有固定收尾形式。不為製造下一輪而追加問句、邀約、俏皮話、提醒或事件；角色沒有新內容要說時，不為收尾再補一句。探索／共同創作仍可主動發展，但不要求每輪結尾提供新鉤子。有當下具體理由的台詞仍保留，不改成每輪固定沉默／動作結尾，也不省略玩家本輪安排的多段互動。單卡與多角色共用此規則；小蘭 v2 的 boundaries 與預設 length_hint 移除「角色說完後停下／以最後一則角色訊息結束」要求，避免覆蓋共用收尾規則。不修改角色人格、記憶或已保存對話。

角色卡單人記錄員在 provider=vertex 時，透過 call_gm_tools 執行 remember 等工具，不再轉到 Gemini API。單卡及多角色記錄員可用 provider 設定區的 recorder_model 獨立指定模型；未設定或空白時沿用敘事模型。Vertex 可設定 recorder_model=gemini-3.1-flash-lite，敘事仍使用 vertex.model。原文、快照與分支歸屬規則不變；此切換不會自動補跑先前失敗回合。

### 編輯回合的敘事指示

Card Mode `/edit` 的 `revision` 只加入本輪敘事 system prompt。完全重生不提供原回覆；局部修改把原回覆明確標為「待修改草稿，非已發生歷史」，要求保留相容的未指定段落，只修改要求及必要連帶矛盾，輸出完整正文。歷史、回憶和狀態均以回合前為界。修改要求與草稿不進入玩家訊息、Recorder 或記憶；只有新版正式正文和玩家輸入會被記錄。模型可能調整超出預期的文字，不是程式逐字 patch。

角色卡敘事不再注入口癖 verbal_tics（含多角色 roster），亦移除口癖使用指示。原卡片欄位保留作資料相容，不修改歷史對話或記憶；一般說話風格仍保留。

角色卡保留 desires 與心理描寫；最近幾輪已表達的心理動機，沒有新觸發、矛盾或理解時不重述意義，有新進展仍可充分描寫。本次依使用者要求不進行試演或測試驗證。

角色卡可選 `setting.initial_background` 放初始家庭與人物關係，不建立正式配角。敘事 prompt 明確標示其為故事起點，後續明確變化以當前劇情與記憶為準，且不代表玩家角色知悉私事。未設定的舊卡輸出不變；這是提示層優先順序，不是額外的關係狀態機。

单卡可设 `relationship_note_enabled: false`：敘事與 Recorder 的當前關係只送 tier，Recorder 更新關係時僅提交 tier，原 NPC note 與玩家 relationship_note 保留但不更新。不清理歷史文字、記憶或 UI 舊值。預設開啟；目前僅李小蘭 v2 停用，多角色模式尚不適用此開關。

李小蘭 v2 另設 `relationship_enabled: false`，單卡完全省略關係 grounding 與 Recorder 關係指示、工具，並拒絕意外的 set_relationship 呼叫；階級及 note 都不再參與敘事或更新。歷史欄位保留，UI 不再顯示舊階級與描述，關係發展以對話與長期記憶呈現。其他卡預設行為不變。

### 可切換敘事風格

Card Mode 的 `narrative_style` 可為 `original`（預設、不追加規則）或 `natural`（自然互動，試用）。`card_mode/styles.py` 保存小蘭 v2 試演的六條場景因果與對話表現指引；單卡在回憶 grounding 後、編輯指示前注入，多角色在完整場景 prompt 後注入。角色卡、記憶、摘要、字數與小說化承接開關維持各自邏輯。選項只作用於敘事，不交給 Recorder。新 GM 訊息保存 `narrative_style` 作來源標記；不補改舊訊息。風格不是確定性品質保證。


### 可組合的劇情與場景選項

Card Mode 單卡與多角色的 send、regenerate、edit 同時支援三個獨立選項：文風 narrative_style（original / natural）、劇情主動性 plot_mode（follow / explore / coauthor）、場景模式 scene_mode（default / intimate）。缺少或 null 採 original、follow、default；非法值在生成與存檔變更前回 400。前端依卡片保存本機偏好，本次選擇套用於下一次生成，GM 訊息記錄三個選項，不回寫舊回合。

探索允許合理的新事件、線索與場景配角，覆蓋原有「不得另設事件」限制，但不替玩家決定、不捏造共同往事、不自動新增固定角色。親密互動聚焦眼前人物、情緒與感官細節；與探索並用時優先發展當前互動、減少外部打斷，尊重離場與快轉。這是獨立的 Card Mode 提示選項，不沿用舊世界模式 pistol 的停用記錄或強制維持場景機制。

提示依序追加自然互動、探索、親密互動，編輯指示仍在其後；預設組合不追加任何提示。Recorder、既有狀態規則與回覆長度保持原有流程，沒有額外模型呼叫。tests/test_card_styles.py 覆蓋十二種組合及單卡／多角色 API 送出、重生、編輯與非法值無寫入；瀏覽器測試涵蓋偏好保存與請求欄位。


### 探索：舞台服務角色與共同寫作

候選提示 `inspiration/coauthor_exploration_candidate.txt` 嘗試人物驅動的創作接力：先演好作者已安排的內容，再提供具體反應、願望或場景機會；已明示授權的結果要演完，未指定的玩家選擇仍留白。作者改向時放下未發生原案，不以增加事件、配角或固定戀愛反應為目標。自然停筆仍有效，不必每輪遞新鉤子。參見 [真實舞會基準](inspiration/coauthor_benchmark.md)。使用者審閱後決定兩版並列：`explore` 維持原探索，`coauthor` 使用 `styles.COAUTHOR_EXPLORATION`，保留已試跑候選作創作主體，另追加 `COAUTHOR_CONTINUITY` 事實與知情邊界；預設 follow 不變。不新增角色狀態或素材自動注入。


「探索・共同創作」使用 `plot_mode: "coauthor"`，單卡與多角色的送出、重生、編輯皆適用，並記錄在 GM metadata。原探索使用 explore，兩者互斥而不疊加；可各自搭配自然互動及親密互動。介面沿用每卡片偏好保存，不改使用者原有選擇、不額外增加模型呼叫。


共同創作的事實邊界：新事件與當下反應仍可主動創作；具體往事、約定、場外行動與角色知情須有本輪資料依據。摘要缺漏不自行補細節，未採用提案／草稿及其他分支不當歷史。缺依據時以眼前觀察或新提議續戲；玩家明確補設可承接。允許對既有經歷產生新感受，但不自動把一人的自白變成雙方共識，亦不預設道歉後一定釋懷或一定留有疙瘩。歷史候選檔保留作先前試跑基準。僅改共同創作的敘事提示，不新增模型呼叫；離線測試驗證單卡／多角色送出、重生、編輯的注入與切回移除，不代表已驗證模型幻覺率改善。

### 回覆版本的上下文隔離

`card_mode/candidates.py` 在分支的候選資料檔保存 regen 正文、選項、生成來源和狀態快照。敘事、Recorder、摘要與回憶仍只讀本線正式訊息，候選庫不注入模型。翻閱與收藏完全本地；採用已完成版本使用快照，未完成版只重跑 `pipeline.record_saved_turn` 或多角色 Recorder，正文保持該候選原文。新 Recorder 使用新工作 ID，防止舊工作回寫。`reply_group_id` 是玩家回合識別，`reply_version_id` 是候選識別，兩者都與可重用的訊息 index 分開。

單卡從完整快照分支或採用候選時，更早的 Recorder error 保留在歷史而不阻擋檢查點；不自動補記，不把失敗改標 done，下一輪仍只使用新分支保存的正文與記憶。更早單卡 pending 的 PID 確認已不存在時，可跨過所選完整檢查點而保留原標記；仍在執行或 PID 無法確認的 pending、所選回合本身未完成及多角色整理錯誤仍阻擋。

「繼續」只將 `（請繼續發展）` 填入文字框，保留已有草稿，連點不重複追加相同尾句；玩家手動送出後才依目前生成選項演出，按鈕本身不發請求。

演出 👍／註記只存為候選版本的獨立欄位，不寫入正式 message 或其角色快照。首次正向標記保存當時可讀的前後正文及來源分支，並沿用該版已有生成選項／來源；這是供作者回顧或未來評估的資料，不是歷史完整模型請求。敘事、Recorder、摘要、回憶查詢不讀評語、不因按讚自動修改 prompt 或人物設定，沒有額外 API 模型往返。

### 探索按鈕、仙俠引子素材與隔離試玩

`story_design/_shared/xianxia_hooks_v1.json` 保存 24 個小事件引子與 24 個大事件舞台，以 size: small／large 分類，保留標題、起點與場合標籤。大事件涵蓋論道、丹道、劍道、渡劫、飛升、遠航、人妖盟約、宗門建設與災變，兼有合作、悟道及衝突，不全以陰謀或戰爭收束。素材尚非世界設定或角色記憶，不預定人物、能力數值、答案及結局；具體內容由當前背景、GM 與玩家輸入共同形成。角色卡可設定 `exploration_material_bank: "xianxia"` 選用這批；未設定或 null 不顯示探索入口。單卡／多角色的 state 只回傳素材，不將其自動當成 lore、記憶或事件。

輸入區「探索 ✦」旁的下拉箭頭可選不限／小事件／大事件，預設不限；偏好依卡片保存在此瀏覽器 localStorage，切換本身不生成、不修改草稿。按鈕顯示探索、探索·小或探索·大，點擊只填入可編輯文字框，不呼叫 API。按所選大小抽一條素材，避開本頁上次抽到的同一條；缺少該類素材時保留原草稿，不偷換成其他類。原有輸入保留，未編輯的探索草稿重抽時替換引子而不巢狀重複包裝。

草稿只包含「事件名稱：事件描述」，後接玩家原有輸入。大小僅用於篩選素材，不再附加探索、規模、世界觀補設、節奏、選擇權或結局指令；素材 seed 本身也移除演出規則。玩家編輯後手動送出，才走原有 send/stream 與目前生成選項。重生沿用正式保存的輸入，不重抽素材；不修改每日 prompt。

`scripts/dev/verify_card_hook.py` 凍結一個已完成回合的存檔，在獨立資料根目錄走正式 Card Mode 敘事、時鐘與同步 Recorder 流程。僅第一輪在敘事 prompt 追加一個短引子與入口要求，後四輪只使用既有「繼續發展」；第二條試玩路徑從同一首輪快照改向，再接一輪續寫，排除第一條路徑的後續內容。沿用 natural／coauthor／default，回憶關閉，不變動正式 prompt；保存每輪實際請求、正文、敘事模型用量及 Recorder 記錄狀態；Recorder 用量不保證由橋接層回傳。

短試玩停用背景摘要，既有 `context_window` 保留未摘要的新回合；這不是長期記憶壓力測試。沒有 `--live` 只保存快照；真模型失敗或 Recorder 失敗會保留紀錄並停止該路徑，不重送、不挑選最佳版本。機制測試不代表模型能可靠編排情節，實際輸出需人工檢視人物行動、因果、局勢變化及玩家改向是否有效。

### 本線持續指示

`card_mode/instructions.py` 從分支設定讀取作者自由文字，只在敘事 prompt 增加一個「本線持續指示：寫作設定」區塊，順序為角色／事實背景、風格選項、持續指示、本輪編輯要求。區塊說明提示不是台詞、已發生事件或記憶；與本輪安排或編輯要求衝突時，以本輪為準，仍保留原意、立場、既有事實及玩家自主。

單卡與多角色的送出、regen、編輯重演使用同一份分支設定。GM metadata 保存該次實際使用文字，後來修改設定不改舊版本；關閉時保存空字串，舊存檔未記錄時不補猜。單卡 recent 去除持續指示 metadata，多角色 recent 維持只取 role/content；Codex 敘事臨時工作區的 RECENT_MESSAGES／FULL_TIMELINE 也移除該欄位，避免歷史版本指示變成目前的有效要求。Recorder、回憶查詢及摘要不注入此設定，正式玩家輸入保持原文；不新增模型呼叫，啟用只增加該段輸入字數。

### 角色卡文字欄位的陣列格式

`persona.identity`、`persona.appearance`、`persona.personality` 接受字串或字串陣列，舊卡不需遷移。陣列項目須為非空字串，personality 整體不得為空。儲存及 state API 保留原始型態；prompt 逐項列出，NPC role 轉為文字，前端身份／外觀顯示換行文字，避免陣列括號與自動逗號。此變更僅調整編輯格式。

### 人物立場、未解矛盾與授權續寫

Card Mode 共用 ROLEPLAY_CONTRACT 區分人物自我辯解與旁白事實；分手、道歉等變化只解決實際處理的問題，不自動抹去隱瞞與信任問題，也不強制自責、懲罰或持續衝突。玩家角色的質問不是作者裁決，對方可以反駁、逃避或部分承認；改變須有新資訊與反應過程，玩家角色也不自動成為清醒裁判。單卡、多角色敘事共用，不進入 Recorder，不增加模型呼叫。

本輪明確要求「請繼續發展」或代演時，可在授權範圍內代寫玩家角色的重要決定與台詞，此條優先於小說化承接、文風與劇情選項的不代寫限制；一般输入仍由玩家決定下一步。不是結果保證，也不固定角色結局。

舊版 prompt 選項：`plot_mode: legacy_20260908` 替換角色扮演契約為 Git `abd788b` 的 2026-09-08 初版，不疊加探索／共同創作規則。角色卡、記憶、摘要及其他風格選項仍按目前流程，故不等同重現歷史完整 API 請求。搭配 original 文風與 default 場景可避免額外風格增補。預設及既有選項不變。


### Pi 共用資料維護任務（2026-10-08）

`card_mode/pi_maintenance.py` 與 `scripts/lore/pi_maintenance*` 將記憶增量、記憶語義合併、精簡 lore、事實摘要放在同一套 task 協定。這版是手動啟動的小批任務；尚未取代遊玩中的 Recorder 或自動摘要，也沒有預製下一輪 prompt。自動排程與即時銜接另做。

- 凍結當前單卡分支原文、各角色 memory、lore、摘要與檔案版本。模型只有 `search`、`read`、`propose`、`submit`；不能讀任意檔案、執行指令或直接寫回。全部文字視為資料。既有安裝的 Pi 走 Vertex 設定及 ADC，不自動安裝或跨 provider fallback。
- `search` 搜尋全快照，分頁傳回摘要；只有 `read` 完整讀取才算查證。提案前必讀指定近期原文；合併時必讀全部被取代記錄。新記憶先搜尋現有條目，只記新資訊或明確變化，不能靠相似度刪掉一字之差的記憶。
- 每一新增陳述、每一被替換記錄都有獨立核對項。policy_version 2 另外對每個操作產生 canon 一致性核對項：先搜尋相關舊資料，再讀取 context_ids 指定的設定／memory／lore（摘要亦可對照舊摘要）；沒有比對依據不得自評 supported。作者卡與 always 且無來源的核心設定納入必讀。必須引用讀完的正式 GM 原文，逐字引文及來源 index/hash 由程式驗證。user 意向、其他分支、未採用候選、舊記憶本身不能充當原文證據。任何 `conflict`／`insufficient` 整批不寫入；沒必要修改可提交空操作。
- 合併需保留當事人、否定、條件、時間與情報歸屬，尤其部分／全部、暫時／永久、提議／答應不可升格。工具可驗證引用與讀取，**語義是否充分支持仍是模型判斷**，不宣稱沒有幻覺。
- lore 只收跨場景有用的穩定少量設定，不把事件流水帳、行蹤、關係進度重抄進去。沒有原文來源的作者設定以及來源失效條目不能自動改寫；新條目沒有 `always`，由既有相關性選取與回憶工具使用。
- summary 同樣先查證再寫回；每批最多向前整理 60 則，保留最近 10 則為原文，新增摘要內容不可引用覆蓋位置之後的資料。需讀完本批未摘要原文和舊摘要；舊事不確定時回查歷史。摘要最多 8000 字元。
- 每批最多 12 項改動、100 項核對、每階段 240 次工具呼叫及整體 timeout。模型提前停筆且未提交時最多補兩次續做提醒；仍未完成則失敗，不以口頭輸出代替完成。
- policy_version 2 按資料壽命分配查證：memory／lore 的非空提案維持全新 Pi 上下文獨立查證；摘要專用批次完成原文核對與一致性檢查即可，不固定重跑另一個模型。摘要可省略非關鍵日常細節與修辭，但不得丟失重要因果、有效約定、否定、未完成事項及限定。混合批次只要包含 memory／lore，整批仍複核；建議不同種類分批。舊快照維持原本嚴格雙階段規則，不能藉新版程式跳過舊任務查證。
- 複核者取得原提案與已定位的來源／上下文 ID，自己讀取正式原文；不提供整理者自評作答案。它可以继续搜尋，不必從零定位。兩階段共用 timeout；任一不通過則不提交。空操作不必第二次查證。
- host 獨立重播兩份工具紀錄後，取得分支鎖及摘要跨程序鎖，再比對原文／NPC／lore／摘要／卡片／分支樹的檔案版本。任何變更拒絕過期結果。通過後備份原始 bytes、原文快照、引文及工具紀錄，再整批寫入；一般寫檔例外復原整批。完全重複的新增記憶也會拒絕。
- 記憶只更新當前 NPC 與最新 GM 的 post snapshot；不修改更早快照、pre snapshot 或歷史候選，不把後來的知識洩漏到早期。採用舊候選／rollback 會回到該版本原始記憶，需要時再整理。
- `undo` 僅在套用後資料未再變動時可撤回。程序被終止留下 `prepared` 批次可用 `recover` 核對前後 hash 後復原；發現新遊玩資料則拒絕覆蓋。原文從不改寫。

目前支援單卡引擎（包含故事中有許多配角的單卡），ensemble 另有角色知識隔離，這版明確拒絕該模式。紀錄／摘要／lore 共用任務執行器，不表示本輪已自動整理全歷史。


### Pi 任務用量（2026-10-08 修正）

`card_mode/pi_usage.py` 從 Pi 的最終 assistant message usage 計算每次可見模型呼叫，不重複計算串流 delta／同 response ID。保留模型、provider、各次 input/output/cacheRead/cacheWrite/reasoning/totalTokens、原始 usage、停止狀態、續做次數、工具失敗及核對拒絕原因。organize 與 independent_review 分開統計，再加總成任務；lookup/proposal/verification 只是該呼叫所要求工具的分類，不把同一次模型費用虛拆成多份。

token 欄位維持 Pi 回報定義；reasoning 與 cache 不再加到 totalTokens，避免重算。缺欄位或超時中的未完成呼叫標 null，另留 known_tokens 已知小計；沒有事件也不是已知零用量。SDK 內部未暴露的 API 重試無法準確計數。自訂模型未設定價格時 SDK cost=0 不能當免費，費用存 null，原回報保留作參考。

每個主任務在 run_dir 保存 report.json 與 usage.json，並將完整用量存入 `data/stories/<slug>/pi_maintenance_usage/<branch>/<task_id>.json`。成功、拒絕、超時、空提案都記錄；獨立複核合併進父任務，不另列一份帳而雙重加總。寫回成功或失敗更新同一任務，保留 apply_attempts 與 ever_applied。原文索引、任務類型、focus、policy version、改動提案數與耗時一併保存，供後續品質／用量比較。尚未設任意 token 硬上限，也不宣稱本次已量得節省比例。


探索草稿先呈現事件名稱與描述，再保留作者原有輸入；填入時停在開頭，不自動聚焦。

探索素材的小／大按入口形態區分：小事件是靈泉改道、雷劫將至等具體仙俠遭遇點；大事件是論道大會、魔潮等可容納多條發展的舞台。不以場面強弱分類，不強制小事件銜接大事件。原有 24 條通用小事件已移除，替換為 24 條仙俠引子並使用新 ID；大事件素材保持原樣。

Card Mode 回覆長度新增 `unlimited`（不限長度；快捷列顯示 ∞），不加入字數目標，也不回退至角色卡 length_hint。與其他選項相同，保留每卡偏好並適用送出、重生及編輯；模型本身的輸出上限仍有效。


Pi 隨身物與背景維護：`inventory` 與 memory/lore/summary 使用同一查讀、提案、逐項引文、獨立複核及版本檢查流程。隨身物 operation 只可替換現有 target_ids，item_name 是統一名稱，text 空字串表示刪除；合併必須包含所有目的條目，不累加同批重複數量。原始敘事與舊回合快照不改，套用更新 character_state 與最新 GM 的 post-state inventory，保存備份及 token usage。不能因久未提及就刪物。

`pi_schedule` 按已完成且 Recorder 成功的 user/GM 配對計數，預設每 50 個來回（約 100 則訊息）啟動一批四類維護；每批最多十二筆操作，不保證掃完全部歷史。只在啟用卡片的 Recorder 結束後啟動背景執行緒；查讀可搜尋完整凍結歷史，預讀近期 20 則，其餘依需要搜尋。模型工作不持遊玩鎖，不阻塞送出。分支獨立計數；新分支或回退失去計數錨點時重設基準。失敗、超時或遊玩／摘要已更新則不套用，隔下一個 50 回合再試，不反覆耗 API。一般即時摘要仍保留，Pi 是補充維護；不啟用時無模型費用。排程狀態保存在分支 pi_schedule_status.json，設定在卡片 pi_schedule.json。


Pi 整理後回退：memory/inventory 沿 GM post-state 快照恢復，回到整理落點保留、回到更早點恢復舊值（舊重複資料可能重現）。Pi lore 保存 previous_version 與 maintenance_anchor，讀取時依保留 GM 的 index/hash 選出有效版本；新條目在整理落點被移除後不可用，替換條目則回復舊版本。分支繼承同一版本鏈但獨立存檔，同編號不同正文不復活未來 lore；維護再抽取時僅讀有效版本。摘要原有 covered-prefix 檢查保留仍完全有效的歷史摘要，回退進其覆蓋範圍則清除並更新 revision，阻擋舊背景提交。回退／重置當下校正 Pi 回合計數，舊任務不得覆蓋重設後的計數帳本。
驗證：`tests/test_pi_rewind.py` 透過真實 rewind/fork API 驗證四類資料，無正式故事修改或模型呼叫。

隨身物寫入拆分：`set_inventory` 僅更新／刪除逐字匹配的現有名稱；`add_inventory` 只新增含中文且尚不存在的名稱。任一非法名稱使整批拒絕並回傳現有名稱讓 Recorder 修正，不再把改名當新增。Pi 替換目的名稱亦須含中文（刪除舊英文項目仍允許）。數量變更須有原文明確依據，不因閉關或快轉自動推算消耗。

### Galgame 先判定、後敘事

選用 `galgame.enabled=true` 的單卡在敘事前使用目前 provider 的 `call_oneshot(response_schema=ASSESSMENT_SCHEMA)`（沿用 recorder_model）。Gemini／Vertex 使用原生 JSON schema；其餘 provider 的文字輸出也必須通過相同 JSON 與欄位驗證。普通卡不增加此呼叫。只回 action、ask/act、最近角色回覆的同意引文，不允許提交分數。玩家原文證據由 host 直接保存，不要求模型重抄。另記 `galgame_assessment` 用量；實際延遲與模型費用增加。

分類器閱讀最近6則歷史與完整本輪玩家輸入，辨識否定、計畫、禮貌詢問、實際行動、重複糾纏；多行為取最嚴重越界，不能用同回合好話抵銷。host 驗證 JSON、完整欄位、enum 與同意引文來源，再由純函式執行固定上限和扣分。格式／引文錯誤最多自動修正一次，日誌記錄失敗類型，不記玩家原文；仍失敗則不生成、不結算，回報具體原因。沒有把無效回覆默認成普通互動。引文檢查不是語義正確性的證明，分類是否合理仍需試玩。

舊版使用 AUTO 工具選擇，API 成功也不代表模型有提交可接受的工具判定，且工具執行後還多一次無用途的回覆。2026-10-09 改為單次結構化分類，移除這個不必要的工具迴圈；歷史失敗未保存工具回傳，無法追認每次是缺呼叫或引文不合格。

Galgame 使用獨立的一般戀愛敘事契約，不附加普通卡的破壁提示。狀態 grounding 指定當回合結果；末尾優先規則要求風格、修改與持續指示不能改分或繞門檻。拒絕不得演成欲拒還迎，反感影響角色立場，親密情節含蓄或淡出。正文後有三個可選行動，仍可自由輸入。這是敘事約束，不宣稱模型必然完全遵循。

Recorder 讀寫原有記憶、隨身物與時鐘，不暴露 set_relationship，也不重新分類。已保存回覆的 post-state.galgame 是重錄時的來源，避免重複加分。證據與結果保存在 last_event，方便追查錯誤分類。

世界觀卡 `xihe_adulthood` 以 setting.initial_background 保存人口、成年、生育福利與多人物敘事設定；relationship_enabled=false，不套用單女角 galgame 數值。角色無固定口癖。
