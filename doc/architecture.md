# 系統架構說明

## 1) 總覽

目前系統是「單一 Flask 應用 + 多模組輔助」的架構，主控在 `app.py`。

```text
Browser (static/app.js, templates/index.html)
  -> Flask app (app.py)
      -> LLM bridge (story_core/llm_bridge.py)
          -> story_core/gemini_bridge.py / story_core/claude_bridge.py / story_core/codex_bridge.py
      -> Lore / Event / Usage / State SQLite
      -> JSON runtime files (messages/state/npcs/branch tree/saves...)
      -> Background threads (tag extraction, compaction, NPC evolution, state cleanup, image gen)
```

### 共用聊天 UI

世界模擬器 `app.js` 與角色卡 `card.js` 都使用 `static/chat_ui.js`、`static/style.css` 及 `templates/shared/` 的輸入區／對話框。共用層只處理呈現、互動和 SSE，不持有 story/branch 狀態，也不呼叫初始化 API。兩種模式各自負責資料載入和操作；角色卡繼續走 `/api/card/*` 與 `story_core/card_mode/`。頁面資源版本由 `story_core/web_assets.py` 統一計算。

## 2) 主要模組

- `app.py`
  - 路由層（`/api/*`）
  - 故事/分支管理
  - GM 回覆處理與 tag pipeline
  - branch timeline 合成、fork/merge/promote
- `story_core/llm_bridge.py`
  - 依 `llm_config.json` 分派 provider
  - 統一 non-stream/stream 介面
  - token usage metadata 擷取
- `story_core/llm_trace.py`
  - 結構化落盤 LLM request/response trace
  - 依 story/date/branch/message 分區存檔
  - 內建 retention prune（按日期資料夾清理）
- `story_core/gemini_bridge.py` / `story_core/openrouter_bridge.py` / `story_core/claude_bridge.py`
  - Gemini API、OpenRouter Chat Completions 或 Claude CLI 的實際呼叫
  - OpenRouter 支援串流、一次性呼叫與 Card Recorder tool calling；失敗時不跨 provider fallback
- `story_core/codex_bridge.py`
  - Codex CLI 的 read-only provider
  - 以臨時工作區暴露當前 branch timeline / state / lore / recap / plan
  - 仍走既有 GM parser / snapshot / async extractor pipeline
- `story_core/lore_db.py` / `story_core/event_db.py` / `story_core/usage_db.py`
  - 各自獨立 SQLite
- `story_core/state_db.py`
  - 分支級角色狀態索引（`state.db`）
  - 把 inventory/ability/relationship/mission/system/npc 做可檢索化
  - 支援 lazy rebuild 與分類輸出 `[相關角色狀態]`
- `story_core/dungeon_system.py`
  - 副本模板、進度、成長限制與驗證
- `story_core/compaction.py`
  - 長對話壓縮（recap）
- `story_core/npc_evolution.py`
  - NPC 近期活動演化
- `story_core/state_cleanup.py`
  - LLM 狀態審核：依階段/NPC/事件/道具/關係產出清理建議（歸檔 NPC、合併重複、解決事件、移除道具），背景執行；可由 phase 轉換、每 N 回合、或手動按鈕觸發
- `story_core/world_timer.py`
  - `world_day` 與 `<!--TIME ... TIME-->` 處理
- `story_core/gm_cheats.py` / `story_core/dice.py`
  - 命運骰與金手指狀態
- `story_core/gm_v2/`（平行實驗管線，見 §9）
  - `contract` / `parse` / `gate` / `apply` / `prompt` / `critic` / `pipeline`
  - 單次 grounded GM 呼叫 + delta block，取代 sync/async extractor + guard stack

## 3) 資料分層

### 設計層（可版本控制）

`story_design/<story_id>/`

- `system_prompt.txt`
- `character_schema.json`
- `default_character_state.json`（可選）
- `world_lore.json`
- `parsed_conversation.json`
- `nsfw_preferences.json`

### 執行層（runtime）

`data/stories/<story_id>/`

- `timeline_tree.json`（active branch、分支關係、旗標）
- `branches/<branch_id>/`
  - `messages.json`
  - `character_state.json`
  - `npcs.json`
  - `state.db`
  - `branch_lore.json`
  - `gm_plan.json`
  - `conversation_recap.json`
  - `world_day.json`
  - `dungeon_progress.json`
  - `debug_directive.json`（一次性隱藏 Debug 劇情指令）
  - `pending_debug_overrides.json`（Debug apply 後待下一次 edit/regenerate 消耗的一次性 seed 修正）
  - `gm_cheats.json`
  - `branch_config.json`
- `debug_units/<debug_unit_id>/`
  - `chat.json`（Debug 對話歷史，依 blank root 單位共用）
  - `last_apply_backup.json`（最近一次 Debug apply 前完整快照，供 Undo）
- `lore.db` / `events.db` / `usage.db`
- `saves.json`
- `images/`

`data/llm_traces/`

- `<story_id>/<YYYY-MM-DD>/<branch_id>/<msg_tag>/<HHMMSS.mmm>_<stage>_<id>.json`
- 由 `app.py` 與 `story_core/lore_organizer.py` 在 LLM request/response 前後寫入（best-effort）

## 4) Timeline 與 Branch 模型

- `main` 分支的基底對話來自 `story_design/<story_id>/parsed_conversation.json`。
- 每個分支只存自己的增量訊息（delta）在 `branches/<branch_id>/messages.json`。
- `get_full_timeline()` 會把父鏈訊息與當前分支 delta 合成完整時間線。
- `branch_point_index` 表示從父分支哪個訊息 index 分叉。
- `branch_point_index = -1` 表示空白分支（不繼承任何訊息）。

## 5) `/api/send` 核心管線

1. 寫入玩家訊息到分支 delta。
2. 建構 system prompt（核心角色狀態 + lore + NPC 摘要 + recap + 副本上下文）。
3. 對玩家訊息做 context augmentation（lore/events/GM 隱藏敘事計劃/npc 活動/state RAG/戰力提醒/骰子提示）。
   - 若存在 `debug_directive.json`，會注入 `[Debug 修正指令（僅供 GM 內部參考，勿透露給玩家）]` 區塊（一次性）
   - state RAG 走檢索層限流（預設總筆數 30、NPC 類別上限 10）
   - `must_include_keys` 命中項目保底注入，不受類別上限限制
4. 記錄 request trace，呼叫 LLM（stream 或 non-stream），再記錄 response trace。
5. `_process_gm_response()`：
   - 移除 echo 回來的 context 區塊
   - 同步抽取 `STATE/LORE/NPC/EVENT/IMG/TIME` tag
   - 產生 snapshot（`state_snapshot/npcs_snapshot/world_day_snapshot/dungeon_progress_snapshot`）
6. 落盤 GM 訊息與 snapshot。
7. 背景觸發：
   - `_extract_tags_async()` 二次結構化抽取（LLM）
   - `run_state_cleanup_async()` 狀態清理（phase 轉換 副本→主神空間 或每 N 回合或手動觸發時）
   - `compact_async()` 長對話壓縮
   - `run_npc_evolution_async()` NPC 演化
   - 自動 pruning 無效 sibling（依規則）

## 6) 同步與非同步抽取

### 同步抽取（即時）

- 直接從 GM 回覆內的顯式 tag 解析並套用。
- 優先保證即時可見狀態。
- `_apply_state_update_inner()` 套用 canonical `character_state.json` 後，會同步 `replace_categories_batch(...)` 更新 `state.db`（inventory/ability/relationship/mission/system）。
- wrapper `_apply_state_update()` 會額外處理 `current_dungeon` reconciliation：把 narrative/state 路徑的副本轉場收斂到 `dungeon_progress.json`，並在 growth cap 後重新同步 state.db。
- `_save_npc()` 與 `/api/npcs/<id> DELETE` 會同步 upsert/delete `state.db` 的 npc 類別，確保索引與 `npcs.json` 一致。

### 非同步抽取（背景）

- 由 `_extract_tags_async()` 另外呼叫 `call_oneshot()` 做語意抽取。
- 補齊 lore/events/plan/npc/state/time/branch_title/dungeon progress。
- 支援新契約（優先）：
  - `event_ops`: `update[{id,status}]` + `create[...]`（id-driven）
  - `state_ops`: `set/delta/map_upsert/map_remove/list_add/list_remove`
- 舊契約仍可用（相容）：
  - `events`（title-driven）
  - `state`（legacy update object）
- `pistol_mode` 開啟時會跳過 lore + event + plan 持久化，避免 NSFW 場景污染長期資料。

## 7) 啟動與遷移

`POST /api/init` + `app.py` 啟動流程會處理：

- stories registry migration
- design files migration（舊 `data/stories/<id>/` -> `story_design/<id>/`）
- timeline tree migration
- branch file layout migration
- schema migration（例如 abilities 欄位）
- lore index 初始化
- incomplete branch 清理
- state index lazy rebuild（首次 state search 若無 `state.db` 則自動由 JSON 重建）

此外，branch fork/edit/regen/blank/merge/promote 會以目標分支快照（`state_snapshot` / `npcs_snapshot`）重建 `state.db`，確保索引語義與分支時點一致（不是直接繼承 parent 的最新索引）。`gm_plan.json` 也會在 fork/edit/regen/merge/promote 依分支時點做 copy/relink，避免跨分支沿用錯誤 event id。

## 8) 併發與鎖

- 檔案儲存採「寫入 `.tmp` 後 `os.replace`」避免半寫入。
- `world_timer`、`dungeon_system`、`lore_organizer` 有 branch/story 等級 lock。
- SQLite 使用 WAL（各 DB 模組內設定）。

## 9) GM v2 管線（平行路徑，實驗中）

把 in-session GM 原型搬上 production 的新路徑，與既有 `/api/send` **並存、互不影響**（入口 `POST /api/v2/send`）。核心理念：**一個 applier core，可換的 brain**——GM（Gemini）在單次呼叫裡同時產出敘事與「自己宣告的」結構化狀態變更，取代「第二個 LLM 逆向工程 prose」的 sync/async extractor。

### 動機

舊路徑用無狀態單次抽取維護 durable state，再用 lossy guard 補它的不可靠（`_sanitize_async_state_update` 砍 transient marker、`_save_npc` 關鍵字 archive/reactivation）。這些 guard 本身就是「state 更新不完全」「已退場 NPC 復活」的近因。v2 改成 **grounded + 驗證 + 累積**的多步流程。

### 模組（`story_core/gm_v2/`）

- `contract.py`：輸出契約。GM 在敘事後輸出一個 `<!--DELTA {json} DELTA-->` 區塊（HTML comment marker，前端本就隱藏）。
- `parse.py`：容錯切 `(narration, delta, note)`；缺/壞只記 note、不丟例外。
- `gate.py`：**deterministic gate**（無 LLM）。丟結構性錯誤（scene/instruction/型別/off-schema），flag 高風險欄位（`base_power_level`/`systems`/`gene_lock`、terminal NPC + memory、lore）給 critic。**刻意不重蹈 lossy 砍值**。
- `apply.py`：**applier（方案甲）**。重用 production `_apply_state_update_inner`（thin apply + state.db sync）+ dungeon reconcile，但跳過 `_run_state_gate`/`_sanitize`/`_normalize_state_async`；NPC 用顯式 lifecycle（GM 的 active/offstage/terminal → production `lifecycle_status`/`archive_kind`），無關鍵字 reactivation，並有 append-only `memory[]`。
- `prompt.py`：grounded prompt（system prompt + 當前 state + active NPC 含 memory + 契約）。
- `critic.py`：**async critic**（可注入 LLM）。離開關鍵路徑審 flagged：撤銷被誤升的暫時狀態、決定 lore 入庫。
- `pipeline.py`：`run_turn()` 串接 PRE → 生成 → parse → gate → apply → commit(訊息+snapshot) → 預擲下一骰 → async critic。

### 一回合流程

1. **PRE**（server，無 LLM）：載 state / NPC（含 memory）/ recap、RAG lore、預擲骰 → 組 grounded prompt。
2. **GENERATE**（一次 LLM 呼叫）：敘事 + DELTA block。
3. **PARSE → GATE(B) → APPLY（方案甲落盤）→ commit** 訊息 + snapshot。
4. **async CRITIC(C)**：審 flagged，撤銷/修正、lore 入庫。

### 與舊路徑的取代關係

- 取代 `_extract_tags_async`（GM 自己出 delta，不再二次逆向）。
- 取代 `_sanitize_async_state_update` / `_run_state_gate` / `_review_state_update_llm`（thin apply + critic）。
- 取代 `_save_npc` 關鍵字 reactivation（顯式 lifecycle）。
- 新增 per-NPC `memory[]`。

### 現況

- 已建並以 stub LLM 端到端驗證（parse/gate/apply/commit/critic；三個結構性 bug 在真實落盤路徑上消失；grounding 成立）。
- 尚未串流（route 非串流）、尚未接前端、真 Gemini 品質待 live 驗證。
- 測試：`tests/test_gm_v2.py`（需 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，因 langsmith plugin 破壞整套 pytest）。

### Card Mode 多角色互動與背景整理

`card_mode/ensemble.py` 使用一次統一敘事與真實 SSE，完整對話先存入副本 `ensemble.json`；`ensemble_recorder.py` 使用另一把跨程序鎖依序處理持久待辦，模型呼叫期間不佔遊戲鎖。完成時驗證 turn ID 並重建後續角色快照及手動控制，防止回退／重生的遲到結果回寫。詳見 prompt_design 的「選用多角色副本」。

### Card Mode 分支

`card_mode/branches.py` 以既有 timeline_tree.json 管理分支名稱、來源點與目前選擇。角色卡子分支保存完整獨立快照（card_full_snapshot），branch_tree.get_full_timeline 對此標記直接讀本線訊息；原世界模式仍沿用父鏈 delta 合成。單卡沿用 branches/<id>，多角色主線保留根 ensemble.json、子線保存 branches/<id>/ensemble.json。前端與所有背景工作捕獲明確 branch_id，切換只改預設選擇，不搬動或合併存檔。

`card_mode/instructions.py` 的本線持續指示獨立保存為 `card_instruction.json`，放在同線的 messages／ensemble 所在目錄。它是作者設定，不併入角色快照；回退／重置保留，一般／編輯／候選新分支繼承建立當下的設定。敘事追加生效文字並在回覆版本記錄 metadata，Recorder／摘要／回憶不注入設定；API 保存使用同線鎖與 revision 防止覆蓋其他視窗的更新。

### Card Mode 回覆候選

`card_mode/candidates.py` 使用 `card_candidates.json` 的輕量索引與 `card_candidates/<reply_group_id>.json` 的逐回合正文／快照，避免每兩秒 state 輪詢讀取全部候選快照。單卡及多角色子線位於分支目錄；多角色 main 位於 story 根目錄。操作沿用各模式的遊戲鎖，收藏與版本不進入模型上下文。新分支只複製保留回合的候選資料；最新回合採用可原線替換，歷史採用建立新線。單卡 Recorder 抽出 `record_saved_turn`，可重新整理已保存正文而不再呼叫敘事模型。


### Pi 資料維護邊界

`card_mode/pi_maintenance.py` 為單卡 memory 增量／合併、lore、summary 的共同任務執行器。Pi 在隔離工作目錄透過受限查讀／提案／核對工具工作；host 重播工具紀錄、檢查 live revision，再持分支短鎖及跨程序 recap commit guard 寫回。正式存檔不直接暴露給模型工具。任務記錄與可撤回備份保留原文證據。可用 CLI 手動啟動，或選用 `pi_schedule` 在 Recorder 完成後每 50 個完整來回背景啟動；下一輪 prompt 預整理仍未串接。


Pi 維護的 `policy_version: 2` 將長期 memory/lore 與滾動摘要區分，前者保留獨立複核，後者保留單 agent 原文與一致性核對。`card_mode/pi_usage.py` 消費原始事件用量，主任務 report/usage 包含子查證；story-local `pi_maintenance_usage` 以每任務一檔保存用量與提交嘗試，不混用 GM usage_log 欄位而造成未知值或子任務重複計費。


Pi 隨身物與背景維護：`inventory` 與 memory/lore/summary 使用同一查讀、提案、逐項引文、獨立複核及版本檢查流程。隨身物 operation 只可替換現有 target_ids，item_name 是統一名稱，text 空字串表示刪除；合併必須包含所有目的條目，不累加同批重複數量。原始敘事與舊回合快照不改，套用更新 character_state 與最新 GM 的 post-state inventory，保存備份及 token usage。不能因久未提及就刪物。

`pi_schedule` 按已完成且 Recorder 成功的 user/GM 配對計數，預設每 50 個來回（約 100 則訊息）啟動一批四類維護；每批最多十二筆操作，不保證掃完全部歷史。只在啟用卡片的 Recorder 結束後啟動背景執行緒；查讀可搜尋完整凍結歷史，預讀近期 20 則，其餘依需要搜尋。模型工作不持遊玩鎖，不阻塞送出。分支獨立計數；新分支或回退失去計數錨點時重設基準。失敗、超時或遊玩／摘要已更新則不套用，隔下一個 50 回合再試，不反覆耗 API。一般即時摘要仍保留，Pi 是補充維護；不啟用時無模型費用。排程狀態保存在分支 pi_schedule_status.json，設定在卡片 pi_schedule.json。
