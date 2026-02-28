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
- 若不存在，退回：`prompts.py::SYSTEM_PROMPT_TEMPLATE`
- 組裝函式：`app.py::_build_story_system_prompt()`

### 2.2 主 prompt 注入資料

`_build_story_system_prompt()` 會將下列資料注入模板：

- `character_state`: 核心角色狀態精簡文本（schema fields + systems + core extras）
- `narrative_recap`: 壓縮摘要（`conversation_recap.json`）
- `world_lore`: 精簡 lore 摘要（非全文）
- `npc_profiles`: 當前分支「active NPC」統計摘要（詳細檔案改由 state RAG 按需注入）
- `team_rules`: branch config 的組隊模式規則（`free_agent` / `fixed_team`）
- `critical_facts`: 關鍵事實區塊（phase、world day、關鍵道具、NPC 關係與 tier）
- `dungeon_context`: 副本進度/節點/成長限制上下文

### 2.3 模式切換對 prompt 的影響

- `fate_mode = false`：
  - 會把「命運走向」段落從 system prompt 移除
  - 最近訊息會先做 fate label 清理（避免模型模仿）
- 最近訊息（`recent`）一律會先移除非玩家訊息（`gm`/legacy `assistant`）尾端的「可選行動」區塊，避免選項文字反覆回灌造成上下文污染。
- `pistol_mode = true`：
  - 追加「親密場景指示」段落
  - 追加 NSFW 偏好（chips/custom）

### 2.4 玩家訊息 augmentation（送進 LLM 前）

函式：`app.py::_build_augmented_message()`

注入順序固定為：

1. `[相關世界設定]`（base lore 混合搜尋）
2. `[相關分支設定]`（branch lore bigram 搜尋）
3. `[相關事件追蹤]`（非 blank branch）
4. `[NPC 近期動態]`
5. `[相關角色狀態]`（state.db 檢索結果）
   - 預設檢索限流：總筆數最多 30、NPC 類別最多 10
   - `must_include_keys` 命中的條目先保留，再填入一般結果
   - archived NPC 預設不注入；玩家明確提名時可透過 `must_include_keys` 召回
6. `[戰力等級提醒]`（僅當存在 tier 已知且分類為 ally/hostile 的 NPC）
7. `[命運走向]`（若 fate mode 開啟且非 `/gm` 指令）
8. `---`
9. 原始玩家輸入

### 2.5 戰力等級一致性（Tier Consistency）

- `system_prompt.txt` 內新增「戰力等級敘事指南（演出落地）」：五大等級 D/C/B/A/S 的具體演出維度與反面檢查。
- `prompts.py` fallback 模板同步保留精簡版等級框架，避免 fallback 路徑退化。
- NPC tier 採 15 個 sub-tier：`D-/D/D+/C-/C/C+/B-/B/B+/A-/A/A+/S-/S/S+`。
- `_save_npc()` 會做 tier allowlist 正規化；不合法值直接忽略、不覆蓋既有合法值。
- NPC metadata 新增 `lifecycle_status` / `archived_reason`；缺省視為 active，archived NPC 預設不進常駐 prompt。

### 2.6 State RAG（角色狀態按需注入）

- state index 來源：`state_db.py`（branch 級 `state.db`）。
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
- `TIME`：推進 `world_day`

### 3.2 回覆清理

在存回前會先：

- 移除模型回聲的 context 區塊（`[相關世界設定]` 等）
- 移除 fate label（安全網）
- 去重複獎勵提示（保留最後一條）

### 3.3 Snapshot

每則 GM 訊息會附加 snapshot，供 fork/edit/regen 精準回溯：

- `state_snapshot`
- `npcs_snapshot`
- `world_day_snapshot`
- `dungeon_progress_snapshot`

### 3.4 State Index 同步點

- canonical state 寫入最終匯集點是 `_apply_state_update_inner()`；套用後會同步 state.db 非 NPC 類別。
- NPC 寫入透過 `_save_npc()` 同步 state.db（含 `lifecycle_status` / `archived_reason` 與 `NPC|ARCHIVED` tag）；手動刪除 NPC（`DELETE /api/npcs/<id>`）也會同步刪除索引。
- fork/edit/regen/blank/merge 分支操作會用 snapshot 對應的 state/npcs 重建 state.db，避免「分支時間點」與「索引內容」不一致。

---

## 4. Async Extraction Prompt（第二層結構化）

函式：`app.py::_extract_tags_async()`

特點：

- 使用 `call_oneshot()` 解析剛產生的 GM 內容
- 輸出 JSON（lore/events/npcs/state/time/branch_title/dungeon）
- 只在 GM 文本長度 >= 200 時啟動

### 4.1 Prompt 內建 guardrails

- lore 提取有「通用設定」門檻，避免把一次性劇情寫進知識庫
- event 優先走 `event_ops`（id-driven），避免 title 漂移造成斷鏈
- event create 仍保留 title dedup + status 升級規則（planted -> triggered -> resolved/abandoned）
- NPC 提取支援 `tier` 欄位；若既有 NPC 本回合無法判定 tier，應省略該欄位（不要用 null 覆蓋）
- state 優先走 `state_ops`（set/delta/map_upsert/map_remove/list_add/list_remove），fallback 才用 legacy `state`
- time 有上限（單次最多 30 天）
- dungeon 進度提取時會給 node id 對照

### 4.2 特殊模式

- `pistol_mode = true` 時，會跳過 lore + events 持久化（避免污染長期資料）

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

檔案：`auto_play.py`

### 6.1 角色生成 prompt

- `_CHAR_GEN_PROMPT`
- 輸出固定 JSON 結構（personality/opening_message/character_state）

### 6.2 Player AI prompt

- `_PLAYER_SYSTEM_PROMPT`：角色性格 + recap + 角色狀態 + phase hint
- `_PLAYER_TURN_PROMPT`：最近 4 則對話，要求第一人稱 50-150 字行動

### 6.3 Auto summary prompt

- 檔案：`auto_summary.py`
- 每 5 回合或 phase 轉換時，背景生成 JSON 摘要
- 對話壓縮（`compaction.py`）在送摘要前也會移除非玩家訊息的「可選行動」區塊，避免選項被固化進長期 recap。

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
   - `state_db.py` 內對應 helper（目前為避免循環 import 採 duplicated logic）
