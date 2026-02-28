# 系統架構說明

## 1) 總覽

目前系統是「單一 Flask 應用 + 多模組輔助」的架構，主控在 `app.py`。

```text
Browser (static/app.js, templates/index.html)
  -> Flask app (app.py)
      -> LLM bridge (llm_bridge.py)
          -> gemini_bridge.py 或 claude_bridge.py
      -> Lore / Event / Usage / State SQLite
      -> JSON runtime files (messages/state/npcs/branch tree/saves...)
      -> Background threads (tag extraction, compaction, NPC evolution, image gen)
```

## 2) 主要模組

- `app.py`
  - 路由層（`/api/*`）
  - 故事/分支管理
  - GM 回覆處理與 tag pipeline
  - branch timeline 合成、fork/merge/promote
- `llm_bridge.py`
  - 依 `llm_config.json` 分派 provider
  - 統一 non-stream/stream 介面
  - token usage metadata 擷取
- `llm_trace.py`
  - 結構化落盤 LLM request/response trace
  - 依 story/date/branch/message 分區存檔
  - 內建 retention prune（按日期資料夾清理）
- `gemini_bridge.py` / `claude_bridge.py`
  - Gemini API 或 Claude CLI 的實際呼叫
- `lore_db.py` / `event_db.py` / `usage_db.py`
  - 各自獨立 SQLite
- `state_db.py`
  - 分支級角色狀態索引（`state.db`）
  - 把 inventory/ability/relationship/mission/system/npc 做可檢索化
  - 支援 lazy rebuild 與分類輸出 `[相關角色狀態]`
- `dungeon_system.py`
  - 副本模板、進度、成長限制與驗證
- `compaction.py`
  - 長對話壓縮（recap）
- `npc_evolution.py`
  - NPC 近期活動演化
- `world_timer.py`
  - `world_day` 與 `<!--TIME ... TIME-->` 處理
- `gm_cheats.py` / `dice.py`
  - 命運骰與金手指狀態

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
  - `gm_cheats.json`
  - `branch_config.json`
- `lore.db` / `events.db` / `usage.db`
- `saves.json`
- `images/`

`data/llm_traces/`

- `<story_id>/<YYYY-MM-DD>/<branch_id>/<msg_tag>/<HHMMSS.mmm>_<stage>_<id>.json`
- 由 `app.py` 與 `lore_organizer.py` 在 LLM request/response 前後寫入（best-effort）

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
   - `compact_async()` 長對話壓縮
   - `run_npc_evolution_async()` NPC 演化
   - 自動 pruning 無效 sibling（依規則）

## 6) 同步與非同步抽取

### 同步抽取（即時）

- 直接從 GM 回覆內的顯式 tag 解析並套用。
- 優先保證即時可見狀態。
- `_apply_state_update_inner()` 套用 canonical `character_state.json` 後，會同步 `replace_categories_batch(...)` 更新 `state.db`（inventory/ability/relationship/mission/system）。
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

此外，branch fork/edit/regen/blank/merge 會以目標分支快照（`state_snapshot` / `npcs_snapshot`）重建 `state.db`，確保索引語義與分支時點一致（不是直接繼承 parent 的最新索引）。`gm_plan.json` 也會在 fork/edit/regen/merge 依分支時點做 copy/relink，避免跨分支沿用錯誤 event id。

## 8) 併發與鎖

- 檔案儲存採「寫入 `.tmp` 後 `os.replace`」避免半寫入。
- `world_timer`、`dungeon_system`、`lore_organizer` 有 branch/story 等級 lock。
- SQLite 使用 WAL（各 DB 模組內設定）。
