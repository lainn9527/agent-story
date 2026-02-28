# 系統架構說明

## 1) 總覽

目前系統是「單一 Flask 應用 + 多模組輔助」的架構，主控在 `app.py`。

```text
Browser (static/app.js, templates/index.html)
  -> Flask app (app.py)
      -> LLM bridge (llm_bridge.py)
          -> gemini_bridge.py 或 claude_bridge.py
      -> Lore / Event / Usage SQLite
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
- `gemini_bridge.py` / `claude_bridge.py`
  - Gemini API 或 Claude CLI 的實際呼叫
- `lore_db.py` / `event_db.py` / `usage_db.py`
  - 各自獨立 SQLite
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
  - `branch_lore.json`
  - `conversation_recap.json`
  - `world_day.json`
  - `dungeon_progress.json`
  - `gm_cheats.json`
  - `branch_config.json`
- `lore.db` / `events.db` / `usage.db`
- `saves.json`
- `images/`

## 4) Timeline 與 Branch 模型

- `main` 分支的基底對話來自 `story_design/<story_id>/parsed_conversation.json`。
- 每個分支只存自己的增量訊息（delta）在 `branches/<branch_id>/messages.json`。
- `get_full_timeline()` 會把父鏈訊息與當前分支 delta 合成完整時間線。
- `branch_point_index` 表示從父分支哪個訊息 index 分叉。
- `branch_point_index = -1` 表示空白分支（不繼承任何訊息）。

## 5) `/api/send` 核心管線

1. 寫入玩家訊息到分支 delta。
2. 建構 system prompt（角色狀態 + lore + NPC + recap + 副本上下文）。
3. 對玩家訊息做 context augmentation（lore/events/npc 活動/骰子提示）。
4. 呼叫 LLM（stream 或 non-stream）。
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

### 非同步抽取（背景）

- 由 `_extract_tags_async()` 另外呼叫 `call_oneshot()` 做語意抽取。
- 補齊 lore/events/npc/state/time/branch_title/dungeon progress。
- `pistol_mode` 開啟時會跳過 lore + event 持久化，避免 NSFW 場景污染長期資料。

## 7) 啟動與遷移

`POST /api/init` + `app.py` 啟動流程會處理：

- stories registry migration
- design files migration（舊 `data/stories/<id>/` -> `story_design/<id>/`）
- timeline tree migration
- branch file layout migration
- schema migration（例如 abilities 欄位）
- lore index 初始化
- incomplete branch 清理

## 8) 併發與鎖

- 檔案儲存採「寫入 `.tmp` 後 `os.replace`」避免半寫入。
- `world_timer`、`dungeon_system`、`lore_organizer` 有 branch/story 等級 lock。
- SQLite 使用 WAL（各 DB 模組內設定）。

