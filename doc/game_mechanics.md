# Game Mechanics（遊戲機制）

本文件整理目前實作中的核心遊戲規則與資料流，對照後端行為（`app.py` + 各模組）。

## 1. Story / Branch / Timeline

### 1.1 故事層

- 系統可同時管理多個 story。
- `data/stories.json` 記錄 `active_story_id` 與 story metadata。

### 1.2 分支層

- 每個 story 有自己的 `timeline_tree.json`。
- `main` 是根分支。
- 一般分支：
  - `parent_branch_id`
  - `branch_point_index`
  - `messages.json`（delta）
- 空白分支：
  - `branch_point_index = -1`
  - 不繼承任何歷史訊息

### 1.3 Timeline 合成規則

函式：`get_full_timeline()`

- 先載入 base conversation（`parsed_conversation.json`）
- 沿著 ancestor chain 逐層套用：
  - 先裁切到 `branch_point_index`
  - 再追加該分支 delta messages

---

## 2. 回合主循環

核心路由：`POST /api/send`（或 stream 版本）

1. 寫入玩家訊息
2. 做 context augmentation（lore/events/NPC 動態/tier 提醒/fate）
3. 呼叫 LLM 取得 GM 回覆
4. 解析 tag + 更新狀態
5. 儲存 GM 訊息 + snapshot
6. 啟動背景機制（async extraction、compaction、NPC evolution）

---

## 3. Fate（命運走向）機制

檔案：`dice.py`

### 3.1 擲骰計算

- 基底：`d100`
- 加成：`physique + spirit + gene_lock + gm cheat + beginner bonus`
- 新手 bonus：前 10 回合線性遞減（+10 -> +1）

### 3.2 結果類型

- `天命 / 順遂 / 平淡 / 波折 / 劫數`
- `always_success` 模式下只會出正向結果（天命/順遂/平淡）

### 3.3 顯示與隱藏

- Fate 注入內容只給 GM（`[命運走向]` context）
- 系統會清理歷史 fate label，避免模型把機制文本說給玩家

---

## 4. Cheats（GM 指令）

檔案：`gm_cheats.py`

每分支獨立存於 `branches/<bid>/gm_cheats.json`。

### 4.1 支援狀態

- `dice_modifier`（`/gm dice +N/-N/reset`）
- `dice_always_success`
- `fate_mode`
- `pistol_mode`

### 4.2 繼承

新分支建立時會 `copy_cheats()`，保持玩法一致。

---

## 5. 狀態同步（STATE Update）機制

核心：`_apply_state_update_inner()`

### 5.1 支援更新型態

- 直接覆蓋欄位（文字/布林/數值）
- `*_delta` 數值增量
- list 欄位的 `*_add` / `*_remove`
- map 欄位（如 `inventory`, `relationships`, `systems`）直接 upsert，`null` 表示刪除

### 5.2 相容與修正

- 舊版 list inventory 會自動轉成 map
- map key 會做 fuzzy normalize（全半形、符號變體）
- 非 schema 的場景暫態欄位會過濾，避免污染角色狀態

### 5.3 副本硬限制校驗

`_apply_state_update()` 內會呼叫 `validate_dungeon_progression()`：

- 限制每副本可提升的 rank / gene lock budget
- 超限會被 cap 並回寫

---

## 6. World Time（world_day）

檔案：`world_timer.py`

- 每分支獨立 `world_day.json`
- GM 回覆中的 `<!--TIME days:N TIME-->` 或 `hours:N` 會推進時間
- 進入副本預設 +3 天，離開副本預設 +1 天

---

## 7. Lore / Event 機制

### 7.1 Lore

- base lore：`story_design/<story_id>/world_lore.json` + `lore.db` 索引
- branch lore：`branches/<bid>/branch_lore.json`（分支專屬）
- 注入搜尋：
  - base lore：hybrid（keyword + embedding RRF）
  - branch lore：CJK bigram 線性搜尋

### 7.2 Event

- 存於 `events.db`
- 注入只取 active 事件（`planted/triggered`）
- async extraction 會做標題去重與狀態升級
- fork 分支時會繼承 parent 在 `branch_point_index` 之前的事件，並保留 `message_index IS NULL` 的 legacy 條目
- 刪除分支（hard delete / `was_main` soft-delete）與啟動時 incomplete branch cleanup 會同步清除該分支事件，避免 orphan records

### 7.3 NPC tier（戰力細分）

- NPC 可帶 `tier` 欄位，允許 15 個值：`D-/D/D+/C-/C/C+/B-/B/B+/A-/A/A+/S-/S/S+`。
- async extraction prompt 會提取 `tier`；若既有 NPC 本回合無法判定，應省略欄位而非覆蓋成 `null`。
- 存檔前會經過 allowlist 正規化，不合法 tier 會被忽略（不阻塞 NPC 更新）。
- tier 會出現在：
  - `npc_profiles` 標題（`【X 級】`）
  - `critical_facts` 的關係矩陣（`·X級`）
  - `[戰力等級提醒]`（僅已知 tier 且 ally/hostile）

---

## 8. 分支操作機制

### 8.1 Edit / Regenerate

- 都會建立新分支，不覆蓋原分支
- 新分支會繼承：
  - 對應 index 的 state/NPC/world_day snapshot
  - recap、cheat、branch lore、events、dungeon progress

### 8.2 Promote

- 將目標分支路徑視為主線
- 刪除（soft delete）同層非路徑 sibling subtree
- 保留 lineage，並記錄 `promoted_mainline_leaf_id`

### 8.3 Merge

- 子分支訊息覆寫回父分支（從 branch point 之後）
- 複製 state/NPC/recap/world_day/cheats/dungeon progress
- 合併 child events 回 parent：新標題直接新增；同標題時以 child 的 status 覆蓋 parent
- 子分支標記為 `merged`

### 8.4 Auto-prune

條件都成立才會 pruned：

- sibling 且非當前祖先鏈
- 非 main/auto/protected
- 玩家已走過分叉點至少 5 steps
- 該分支 delta 訊息 <= 2
- 無 active children

---

## 9. 存檔機制（Save/Load）

- `POST /api/saves` 會保存 snapshot（state/NPC/recap/world_day）
- `load` 的語意是「切回原分支 + 狀態預覽」：
  - timeline 訊息不回滾
  - `status` API 會先顯示 snapshot（bookmark preview）
- 一旦 send/switch/edit/regenerate 等繼續操作，preview 會清除並回到 live state

---

## 10. 對話壓縮（Compaction）

檔案：`compaction.py`

- 最近 20 則訊息保留原文
- 更早內容壓成 `conversation_recap.json`
- 超過閾值後背景觸發（non-blocking）
- recap 過長時會再做一次 meta-compaction

---

## 11. 副本系統（Dungeon）

檔案：`dungeon_system.py` + `/api/dungeon/*`

### 11.1 進入副本

- 檢查 prerequisite（最低等級等）
- 初始化 `current_dungeon` 進度、節點、地圖探索、成長預算

### 11.2 進度更新

- async extraction 可回寫：
  - `mainline_progress_delta`
  - `completed_nodes`
  - `discovered_areas`
  - `explored_area_updates`

### 11.3 回歸主神空間

- 主線 `< 60%`：不可回歸
- `60% ~ 99%`：可提前回歸，獎勵 50%
- `100%`：正常回歸
- 最終獎勵會套 difficulty scaling

---

## 12. Auto-play 機制

檔案：`auto_play.py`

- 自動玩家會開新 `auto_*` 分支
- 兩個 LLM 角色：
  - GM：走正常主流程
  - Player AI：`call_oneshot()` 產生行動
- 停止條件：
  - 死亡（`current_status == "end"`）
  - 達到 max turns / max dungeons
  - 連續錯誤達上限
  - 出現 `auto_play.stop`
