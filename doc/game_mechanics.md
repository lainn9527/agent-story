# Game Mechanics（遊戲機制）

## Card Mode 時間與場景同步

敘事第一行的 emoji 標頭與完整日期的 `日期時間 ｜ 地點 ｜ 天氣` 標頭均可同步卡內時鐘、地點與天氣。採用有效標頭後背景記錄員不再次加時；沒有標頭時仍由記錄員處理已發生的時間。時間依活動長度而非回合數推進，敘事提示要求呈現本輪結束時的狀態，短暫對話可以不加時，已完成的長活動與授權快轉應合理跨越分鐘、小時或日期。

## 單卡整理設定的分支範圍

自動／深度回憶可使用同一代理讀取本線整理設定，按場景及上下文決定查什麼。搜尋摘要不作為設定依據，長條目只在完整讀完後交給敘事。回憶關閉時維持本地設定選取，不進行模型查詢。查詢唯讀，不更新 lore、NPC 或遊玩存檔；目前不支援多角色的 lore 查詢。

選用的 `card_lore.json` 為分支獨立資料，fork 複製後依保留原文驗證來源；回退、重置及同回合候選替換不會注入已撤掉來源的條目。當前只支援單卡敘事與人工整理，沒有每輪自動新增設定，也不改 NPC memory、摘要或遊玩正文。

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

檔案：`story_core/dice.py`

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

檔案：`story_core/gm_cheats.py`

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

- 若 `current_dungeon` 是透過敘事/state 路徑被寫入，而不是 `/api/dungeon/enter`，系統會先做 reconciliation，自動補齊 `dungeon_progress.json`
- 進入副本的 reconciliation 發生在 validation 前；離開/切換副本的 archive 發生在 validation 後
- 限制每副本可提升的 rank / gene lock budget
- 超限會被 cap 並回寫

---

## 6. World Time（world_day）

檔案：`story_core/world_timer.py`

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
- query-based 注入只取 active 事件（`planted/triggered`），以 `[相關事件追蹤]` 呈現
- `sticky_priority > 0` 的事件會額外以 `[長期關鍵事件]` 常駐注入，依 `sticky_priority DESC, id DESC` 取最多 4 條
- sticky events 可包含 `resolved` 狀態；用途是保留跨弧線 plot pressure，而不是即時場景相關性
- async extraction 會做標題去重與狀態升級
- async extraction 的 `event_ops` / legacy `events` 可附 `sticky: true`，寫入為 `sticky_priority=1`
- fork 分支時會繼承 parent 在 `branch_point_index` 之前的事件，並保留 `message_index IS NULL` 的 legacy 條目
- fork / merge 會保留 `sticky_priority`
- 刪除分支（hard delete / `was_main` soft-delete）與啟動時 incomplete branch cleanup 會同步清除該分支事件，避免 orphan records

### 7.3 Story anchors（身份層長期記憶）

- 存於各分支 `character_state.json` 的 `story_anchors`
- 上限 10 條，讀取時會 self-heal / normalize / 去重
- 注入位置是 system prompt 的 `critical_facts -> ### 長期記憶`
- 用途是 identity layer 的持久事實：
  - 長期主線
  - 核心隊伍關係
  - 永久代價 / 不可逆變化
  - 已成為角色身份一部分的長期宿敵 / 契約 / 追索壓力
- `story_anchors` 和 sticky events 分工不同：
  - anchors = 身份層事實
  - sticky events = 劇情壓力

### 7.4 NPC tier（戰力細分）

- NPC 可帶 `tier` 欄位，允許 15 個值：`D-/D/D+/C-/C/C+/B-/B/B+/A-/A/A+/S-/S/S+`。
- async extraction prompt 會提取 `tier`；若既有 NPC 本回合無法判定，應省略欄位而非覆蓋成 `null`。
- 存檔前會經過 allowlist 正規化，不合法 tier 會被忽略（不阻塞 NPC 更新）。
- tier 會出現在：
  - `npc_profiles` 標題（`【X 級】`）
  - `critical_facts` 的關係矩陣（`·X級`）
  - `[戰力等級提醒]`（僅已知 tier 且 ally/hostile）

### 7.5 NPC lifecycle（active / archived）

- NPC 新增欄位：
  - `lifecycle_status`：`active` / `archived`（缺省視為 `active`）
  - `archived_reason`：封存來源說明（可為 `null`）
- `archive_kind`：`offstage` / `terminal`
- `origin_dungeon_id` / `origin_run_id`：NPC 首次被記錄時所屬的副本模板與 run 識別（目前 `origin_run_id` 使用當次副本 `entered_at`）
- `_save_npc()` 會用 `current_status` 判定封存/解封存，並區分歸檔種類：
  - terminal archive 關鍵詞：`已損毀`、`威脅解除`、`已失效`、`已封印`、`已消散`
  - offstage archive 關鍵詞：`已退場`、`已離隊`
  - unarchive 關鍵詞：`修復`、`復活`、`再現身`、`重新啟用`、`解除封印`
  - 優先序：顯式 `lifecycle_status` > unarchive > archive > 保持現狀
  - 顯式 `lifecycle_status="archived"` 時，`archive_kind` 若未提供，會沿用既有值；仍無既有值時預設為 `terminal`
- `_extract_tags_async()` 與同步 `NPC` tag 路徑會在寫入時附帶當前 run context；若既有 NPC 是 `archived + archive_kind=offstage`，且後續同一個 `origin_run_id` 又收到新的 NPC update，`_save_npc()` 會自動改回 `active`
- 這個 auto-reactivation 只適用於「同一輪 run 內再次出場」；完整離開副本後重新進入同一模板會產生新的 `origin_run_id`，不會自動復活舊 run 的 archived NPC
- `state_cleanup` 的 `archive_npcs` 建議現在也會帶 `archive_kind`；merge 掉的別名 NPC 一律視為 `terminal`
- `_load_npcs()` 預設只回 active；需要全量時用 `include_archived=True`。
- state.db 對 archived NPC 會打 `NPC|ARCHIVED` tag；`search_state` 預設過濾，只有 forced key（玩家明確提名）才保留。

### 7.6 舊副本角色召回（Dungeon Return Recall）

- 每分支新增 `branches/<bid>/dungeon_return_memory.json`
  - `visited_dungeons`: 依首次拜訪順序去重
  - `pending_reentry_dungeon`: 一次性 recall hint
- 權威訊號是 `character_state.current_dungeon` 的轉換；`dungeon_progress` 不是前提。
- 轉場入口分兩類：
  - 顯式 route：`/api/dungeon/enter`、`/api/dungeon/return`
  - 敘事路徑：`_apply_state_update()` / cleanup / normalize 後的 `reconcile_dungeon_entry()`、`reconcile_dungeon_exit()`
- 規則：
  - 首次進入某副本：只把它加入 `visited_dungeons`
  - 再次進入已拜訪副本：設 `pending_reentry_dungeon = current_dungeon`
  - `A -> B` 直接切換時，先處理離開 `A` 的 NPC provenance / recall policy，再處理進入 `B`
- prompt 注入：
  - `_build_augmented_message()` 會在 branch lore 後、sticky events 前插入 `[舊副本關聯角色提醒]`
  - 候選固定為 `home_scope=dungeon_local`、`home_dungeon=current_dungeon`、`archived+offstage+eligible`
  - 最多 4 名，約 350 chars
  - 第一次嘗試注入後就清掉 pending，避免每回合重複提醒
- NPC provenance 欄位：
  - `home_scope`: `dungeon_local | main_god_space | cross_dungeon`
  - `home_dungeon`: 副本顯示名稱
  - `return_recall_state`: `eligible | excluded_carried_out | excluded_converted | excluded_terminal | unknown`
  - `last_seen_msg_index`: 最近一次被 GM 寫到的訊息索引
- 離開副本時：
  - `archived + offstage + dungeon_local` => `eligible`
  - 明確被帶出副本的隊友 => `home_scope=cross_dungeon`、`excluded_carried_out`
  - 召喚物/素材/封印/武裝化 => `excluded_converted`
  - terminal 退場 => `excluded_terminal`
- reactivation：
  - 若既有 NPC 是 `archived + offstage + eligible`
  - 且角色目前回到 `home_dungeon`
  - 且新的 NPC update 沒再要求 archived
  - 則 `_save_npc()` 會自動翻回 active
- branch 行為：
  - fork / edit / regenerate：不能盲 copy recall file，必須依 source timeline 到 branch point 重建
  - blank branch：初始化空 recall file
  - merge：child recall file 直接覆蓋 parent
  - promote / switch：不做額外 copy，沿用原 branch recall file

### 7.7 GM hidden plan（敘事前瞻）

- 每分支可有 `branches/<bid>/gm_plan.json`（玩家不可見）。
- 來源：`_extract_tags_async()` 的 `plan` 擷取結果（LLM 根據 GM 回覆重寫）。
- 注入：`_build_augmented_message()` 會在事件區塊後注入短版 plan block，僅給 GM。
- `must_payoff` 以 `event_title` 為穩定鍵，並在寫入/拷貝時 relink 成目標分支的 `event_id`。
- 倒數規則：`remaining_turns = ttl_turns - (current_index - payoff.created_at_index)`，`remaining <= 0` 的 payoff 不注入。
- `pistol_mode` 時跳過 plan 持久化，避免 NSFW 場景污染敘事規劃。

---

## 8. 分支操作機制

### 8.1 Edit / Regenerate

- 都會建立新分支，不覆蓋原分支
- `regenerate` 除了重生既有 `user -> GM` 配對，也接受「最後一則已落盤但尚未有 GM」的 `user` 回合作為補發目標
- 新分支會繼承：
  - 對應 index 的 state/NPC/world_day snapshot
  - recap、cheat、branch lore、events、dungeon progress、gm plan（若 `plan.updated_at_index <= branch_point_index`）
  - `dungeon_return_memory.json` 會依 source timeline 截到 `branch_point_index` 重建，避免未來拜訪過的副本洩漏到子分支
  - 若 source branch 存在 `pending_debug_overrides.json`，會先把這些修正重播到 child seed；這份 pending 是一次性的，只有第一次成功的 edit/regenerate 會消耗它，之後若還要沿用需重新 apply

### 8.2 Promote

- 將目標分支路徑視為主線
- 刪除（soft delete）同層非路徑 sibling subtree
- 若 promoted 分支有 parent，會把 child 的 `gm_plan.json` 同步到 parent（含 event_id relink）
- 保留 lineage，並記錄 `promoted_mainline_leaf_id`

### 8.3 Merge

- 子分支訊息覆寫回父分支（從 branch point 之後）
- 複製 state/NPC/recap/world_day/cheats/dungeon progress
- 複製 child 的 `dungeon_return_memory.json`
- 合併 child events 回 parent：新標題直接新增；同標題時以 child 的 status 覆蓋 parent
- child 的 `gm_plan.json` 會覆寫 parent，並以 parent 的 active events 重新 relink `must_payoff.event_id`
- 子分支標記為 `merged`

### 8.4 Auto-prune

條件都成立才會 pruned：

- sibling 且非當前祖先鏈
- 非 main/auto/protected
- 玩家已走過分叉點至少 5 steps
- 該分支 delta 訊息 <= 2
- 無 active children

---

## 9. Debug Panel（V1）

### 9.1 作用域與資料

- Debug 對話歷史以 blank root 為單位：`debug_units/<debug_unit_id>/chat.json`
- 實際套用目標永遠是「目前 branch」：state / NPC / world_day / dungeon
- V1 不處理 events，也不直接改 `gm_plan.json`

### 9.2 套用與回滾

- `POST /api/debug/apply` 採逐項執行：
  - action 失敗不會中斷其他 action
  - 全部跑完回傳逐項結果
  - `actions` / `directives` payload 都有上限（預設 20）；directive 只保留最後一個有效 instruction
- 套用前會先寫完整備份：`last_apply_backup.json`
  - 含 state / NPC / world_day / dungeon 完整快照
- `POST /api/debug/undo` 僅回滾最近一次 apply（同 debug unit）
  - 若 backup 的 `world_day` 無法解析，undo 直接失敗，不會 fallback 到當前值

### 9.3 劇情修正指令（Directive）

- Debug 指令存於 branch：`debug_directive.json`（單一 active）
- 下次主流程 `_build_augmented_message()` 會注入隱藏段落
- `_process_gm_response()` 完成後自動清除，避免累積污染

### 9.4 Branch 規則

- `edit / regenerate`：複製 directive 到新分支
- `promote`：保留 directive（同步到 parent 路徑）
- 其他分支操作（create/blank/merge/delete/cleanup）：不複製 directive

### 9.5 UI 回饋

- `apply/undo/clear` 的摘要只保留在 Debug Panel API response 與面板內 UI
- 主聊天不再插入 `debug_audit` 訊息，避免污染一般對話流

---

## 10. 存檔機制（Save/Load）

- `POST /api/saves` 會保存 snapshot（state/NPC/recap/world_day）
- `load` 的語意是「切回原分支 + 狀態預覽」：
  - timeline 訊息不回滾
  - `status` API 會先顯示 snapshot（bookmark preview）
- 一旦 send/switch/edit/regenerate 等繼續操作，preview 會清除並回到 live state

---

## 11. 對話壓縮（Compaction）

檔案：`story_core/compaction.py`

- 最近 10 則訊息保留原文
- 更早內容壓成 `conversation_recap.json`
- 超過閾值後背景觸發（non-blocking）
- recap 過長時會再做一次 meta-compaction

---

## 12. 副本系統（Dungeon）

檔案：`story_core/dungeon_system.py` + `/api/dungeon/*`

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

### 11.4 Narrative Path Reconciliation

- 顯式 API 路徑（`/api/dungeon/enter` / `/api/dungeon/return`）仍是主路徑
- safety net 會監看 `character_state.current_dungeon` 的轉場：
  - `"" -> "副本名"`：若 progress 尚未 active，依模板名稱對照自動初始化
  - `"副本名" -> ""`：在 final-turn validation 後自動 archive
  - `"副本A" -> "副本B"`：先 archive 舊副本，再初始化新副本
- 如果副本名稱找不到 template，reconciliation 只記 log，不會憑空建立進度

---

## 12. Auto-play 機制

檔案：`story_core/auto_play.py`

- 自動玩家會開新 `auto_*` 分支
- 兩個 LLM 角色：
  - GM：走正常主流程
  - Player AI：`call_oneshot()` 產生行動
- 停止條件：
  - 死亡（`current_status == "end"`）
  - 達到 max turns / max dungeons
  - 連續錯誤達上限
  - 出現 `auto_play.stop`

---

## 13. GM v2 狀態維護（平行路徑，實驗中）

對應 `POST /api/v2/send`、`story_core/gm_v2/`，詳見 `doc/architecture.md` §9 與 `doc/prompt_design.md` §9。與第 5 節的 STATE Update **並存**。狀態維護的差異重點：

- **GM 自己宣告 delta**（敘事 + `<!--DELTA-->` 區塊），不靠第二個 LLM 逆向 prose。
- **thin apply，無 lossy guard**：重用 `_apply_state_update_inner`（含 state.db sync），但跳過 `_sanitize_async_state_update` 的 transient-marker 砍值——避免合理的永久升級被誤刪。
- **NPC 顯式 lifecycle**：GM 給 active/offstage/terminal，無關鍵字 archive/reactivation；archived NPC 不會因被提到而復活。
- **per-NPC append-only `memory[]`**：長期記憶不在壓縮時遺失。
- **兩段式 evaluation**：deterministic gate（結構，免費）+ async critic（語義，離開關鍵路徑）。

---

## 14. 角色卡模式（Card Mode）機制

對應 `routes/card_routes.py`、`story_core/card_mode/`，詳見 `doc/card_mode_proposal.md`。

### 14.1 核心理念
與世界模擬器不同，角色卡模式不負責推世界進度、不演多個 NPC。全部注意力預算只服務一個角色與玩家進行一對一深度對話。

### 14.2 保留與關閉的機制
- **保留的 4 樣機制**：
  1. **關係階梯（Affinity Ladder）**：使用 `gm_v2.relationship` 具名階梯（敵對 < 戒備 < 陌生 < 相識 < 友好 < 信任 < 摯友），單回合正向至多推進 1 階。
  2. **角色長期記憶（Long-term Memory）**：NPC 記錄保留 append-only `memory[]`；人設常數永遠從 `card.json` 讀取，不鎖死在存檔。
  3. **卡內時鐘（In-card Clock）**：以分鐘為單位精確追蹤，由敘事標頭（`📅⏰📌🌤️`）自動同步：完整日期允許跨日／跨週前進；無日期標頭保留 12 小時上限。有效標頭提供時間時，背景記錄員不得再次加時。地點保存於 `card_clock.json.last_location`，不回退至開場地點。
  4. **隨身物（Inventory）**：追蹤玩家當前持有的物品與備註狀態。
- **關閉的機制**：
  - 戰力等級、domains、商店 vendor、任務系統、副本進度、功法樹、貨幣/命運點數、命運骰。

### 14.3 資料層複用與快照冪等
- 一張卡 = 一個 story（儲存於 `data/stories/<slug>/`，registry 註記 `mode: "card"`）。
- 複用 `story_io`、`npc_helpers`、`compaction`。
- 支援完整的重生（regenerate）與回捲（rewind），利用每則訊息的 snapshot 保證狀態回溯的冪等性。

### 14.4 人設規格：肉體外觀、性癖與敏感點
- **外觀與身體細節（`appearance` / `body_details`）**：必須包含具體肉體特徵、衣著微敞或崩落細節、膚觸體溫，在肢體接觸或近身對話時提供生動的感官描寫。
- **性癖與情慾張力（`fetishes`）**：角色專屬的心理渴望與偏好情境（如順從支配、撫摸後頸、被當作女人對待的反差等），觸及時產生強烈內心動搖與羞恥沉溺。
- **生理敏感點（`sensitive_spots`）**：生理弱點部位（後頸、腰際、耳垂、掌心等）及被刺激時的第一即時生理反饋（屏息、輕喘、發顫、體溫飆升），隨關係階梯的心防產生拉扯。

### Card Mode 回退後的摘要失效處理

角色卡 rewind、regenerate 先計算保留的訊息數量：若摘要覆蓋的整段前綴仍保留（`compacted_through_index < retained_message_count`），保留摘要文字、版本、覆蓋範圍及時間；否則清空摘要，由保留的歷史重建。reset 一律清空。摘要覆蓋索引是時間線陣列位置，不以可能重用的訊息 index 判斷。無論保留或清空，都更換 revision，阻擋撤回前的背景工作提交。背景壓縮在每批開始與落盤時核對 revision；不符即丟棄。失效與背景提交使用短鎖，不等待模型生成。尚未摘要的原文持續注入，API 回應格式不變。

### Card Mode 記憶版本 2

- 短期：最近 10 則原文，背景摘要尚未涵蓋的較早訊息也保留，沒有中間空洞。
- 歷史摘要：3000–6000 字目標，另列仍有效的約定與關係事實；新約定更新當前節，舊約定留在歷史。不得把角色說法當成已完成事件或指定未來結局。
- NPC 記憶：沿用 append-only 與既有 snapshot 回捲，單角色敘事讀取近期原文開始前的最新 36 條（使用原文首則的回合前快照，缺快照時退回原本最新 36 條）、按話題找出的最多 36 條舊記憶、其餘舊記憶隨機最多 36 條。相關／隨機候選池不變，僅移動最新組的範圍；記錄員與多角色模式不變。三組去重後按記錄順序排列，最多 108 條；不足不硬補。每次送出／重生成重新抽樣。僅取目前分支該角色的既有列表，不保證原記錄知情判斷正確；不修改歷史。
- 舊版摘要自動從現存完整歷史重建。每批約 60000 字元且不截斷單則訊息，完整成功且 revision 一致才提交；失敗時保留原摘要供重試。

### 多角色共用時間線（Card Mode 選用副本）

角色抽屜的「建立多角色副本」會保留當前故事進度，另建隔離存檔，再加入固定角色；原卡繼續原本玩法。副本內所有人共用一條時間線，每人有獨立人設、記憶、關係與近況。切換焦點可以從小蘭轉向沈晚再轉回，之前發生的事件仍然存在；這不是兩份平行世界自動合併。

抽屜可新增角色、切互動焦點、設定在場／離場／通訊中，並同時查看全員近況。新增角色只繼承手動填入的已知經歷，不會自動知道其他人的私事。切焦點不等於換場。玩家自然走到別處、角色實際抵達或離開時，記錄員依本回合原文同步出場狀態；邀約／計畫不視為完成移動。抽屜仍可手動校正，但正常換場不需每輪操作。副本第一版沿用初始共享物品資料，尚無物品增減記錄；原單人卡物品機制不受影響。

副本起點之前的歷史保留閱讀，不能從副本改寫。回溯、重新生成會連同所有角色快照一起恢復；重置回到副本建立時。新模式生成中拒絕切換、重置或其他新回合，避免舊請求覆蓋新狀態。回覆邊生成邊顯示，完整保存後即可繼續聊天；各人的記憶、關係及近況在背景依序整理。整理期間的下一輪會讀到剛剛保存的原文，不必等記憶更新。失敗時顯示「記憶整理失敗，對話已保存」，可用「重試整理」重讀原文。遲到的背景結果不會復活已回退或重生撤銷的回合。

保存位置為副本自己的 `data/stories/<slug>/ensemble.json`，不使用原單人 `branches/main` 的背景寫入流程；卡檔同樣複製到獨立 `story_design/<slug>/card.json`。不會回寫或合併到來源卡。

多角色副本建立時，若來源時鐘未保存地點，從同一份已擷取對話快照的最近有效 GM 標頭恢復場景；只有沒有可恢復場景時才使用開場地點，不回寫來源。

自然換場由統一敘事依玩家原文直接承接，之後背景記錄再更新出場狀態。所有角色資料按歸屬提供給同一敘事者，私密知情依提示規則維持；不是分開模型輸入的硬隔離。一般每輪共兩次呼叫，只有一次敘事位於互動等待路徑。

### 回憶模式（Card Mode）

附加功能可選「一般／自動回憶／深度回憶」，預設一般，單卡與多角色皆可用。自動回憶按話題判斷是否翻閱歷史；深度回憶允許多次搜尋與讀上下文，適合需要重訪舊事的互動。選擇會隨送出及重生成傳入，每張卡在目前瀏覽器各自記住。

查詢不寫入遊戲狀態、不改摘要或角色記憶；只以保留歷史的原文補充這輪敘事，回捲後不可檢索已刪除的未来。多角色各自查自己的可讀範圍，取回結果附角色歸屬後交給統一敘事者；共享輸入中的知情邊界依敘事規則維持。不保證一定找到資料或一定產生感人的回應；未查到時不應補造。自動與深度會增加模型用量與等待時間，深度有較多查詢步驟。

### 角色卡分支與切回

抽屜可確認刪除目前分支並切回父線。主線不可刪除，生成或記憶整理中需稍後再試。採軟刪除：從選單隱藏、拒絕後續 API 存取，保留原檔供人工救回；直屬子分支接回上一層，更深層關係不變。角色卡為完整快照，只改父子連結，不使用世界模式的 delta 補接；子分支的分叉回合、記憶與歷史不變。

角色回應旁的 ⑂ 可從該回合結束建立新路線，抽屜「劇情分支」可切回與改名。單卡／多角色都沿用 timeline_tree.json 的 parent_branch_id、branch_point_index 與 active_branch_id。角色卡子分支標記 card_full_snapshot，存完整已保留歷史，不在讀取時重新拼接父線；父線之後回退、重生、重置均不改變子線。

單卡在 branches/<id>/ 保存完整 messages、全員 npcs、玩家 inventory、card_clock、有效前綴 recap（新 revision）。歷史分叉使用該回合快照；從最新回合分叉保存當前狀態。越過分叉點的摘要不複製。固定 card.json 仍由同張卡共用，設計人設的手動修改仍適用各線，動態記憶／關係／近況則各線獨立。

多角色舊主線仍使用根目錄 ensemble.json，不遷移；子分支使用 branches/<id>/ensemble.json，保留全員人設、記憶、關係、焦點、在場狀態、原文可見範圍及來源摘要。記錄 worker 與用量帶 branch_id；切換不會把未完成舊工作改投新線。分支建立只接受已完整整理的保留前綴，避免拿未完成記憶當完整狀態。每個分支各自鎖定寫入，樹的修改另有鎖；單卡正在生成或整理時同線破壞性操作會回忙碌，仍可切去別線。

新增功能不批次轉換舊資料、不自動刪除分支、不自動合併互斥劇情。原回退及重生成仍是當線操作，先分支即可保留替代版本。重置單卡只清當線至卡片開場，多角色只清當線至副本建立時。

單卡在回覆顯示後可以立即 regen／回退／重置，即使背景記憶還沒整理完。記錄包含記憶、關係、物品、時間，先存在私有候選狀態，完成時確認原回合仍存在才寫回。被撤銷回合的遲到結果全部丟棄；回退目標回合若仍保留，該回合自己的整理可以完成。regen 不新增分支。已送出的供應商請求不保證能中止，取消指的是不再接受它對存檔的影響。

單卡從已完成且有完整 state／NPC／clock 快照的回合分支時，更早的 Recorder error 不再阻擋，原失敗標記與漏記狀況保留，不自動補記。所選回合本身若為 error，仍須已有人工審核的 card_memory_backfill（reviewed、時間、非負新增筆數）及完整快照才可分支；這不表示關係、物品、時間工具已重新執行。更早的單卡 pending 在 PID 確認已不存在時可跨過完整檢查點，保留原中斷標記；所選回合本身仍須完成。仍在執行或 PID 無法確認的 pending、不完整快照及多角色整理錯誤仍阻擋。分叉點之後的工作不影響從早期完整回合分支。

### 角色卡編輯與局部重演

玩家輸入及角色回覆旁的編輯按鈕，帶入該輪玩家輸入並展示原回覆。可選完全重生／依原回覆修改，以及開新分支／覆蓋當線。覆蓋歷史回合會移除該輪以後的對話與狀態；新分支保留原路線。兩者從原 GM 的 pre snapshot 重演，清除跨越編輯點的摘要，已取消版本的 Recorder 依 turn ID 不再提交。已有資料缺少 pre snapshot 時不猜測還原點。生成失敗還原原單卡資料；多角色使用記憶中的候選文件，成功才保存。處理中同線寫入受鎖保護，背景整理維持原機制。

單卡生成的回憶查詢與敘事各有 180 秒等待上限，逾時或同服務同分支新生成到來時取消舊等待並釋放分支鎖。供應商工作可能尚未結束，但遲到文字與結果不再進入對話／記憶；外部工作不直接寫遊戲狀態。正常送出逾時不新增回合，編輯失敗沿用原有還原流程，regen 沿用先撤銷上一輪的語意。跨服務程序仍以檔案鎖拒絕重疊生成，無跨程序即時取消。Vertex SDK 明確設定 90 秒 HTTP timeout、單次嘗試；此設定也涵蓋 Recorder。

### 記憶編輯視窗

右上角「記憶」開大型視窗，移除側欄長列表。依角色選擇，以保存順序由新到舊列出記憶；編輯及 × 刪除只改草稿，按「保存變更」才寫回。關閉／重新載入前確認放棄草稿；409 或寫入失敗留住草稿。相同文字視為同一項，同角色所有完全相同項目同步處理。

保存鎖定當前分支並檢查版本和 pending Recorder。單卡修改 npcs.json 與 messages.json 的 npcs_snapshot／pre_npcs_snapshot；多角色修改當線 ensemble.json 的 state、initial_state、ensemble_snapshot、ensemble_pre_snapshot，按角色 ID 隔離。僅替換／刪除已存在項目，不把晚期知識插入早期。保留角色關係、正文及其他欄位；不修改摘要或回憶檢索原文，因此不等於撤銷劇情事實，原文仍可能使該事被重新記錄。

單卡若舊 pending 紀錄的執行 PID 已確定不存在，記憶保存與最新正文編輯不再等待這項已中斷工作。保留原紀錄，不冒充整理成功或自動補記；仍存在、無 PID／不合法 PID 或無法確認的工作繼續阻擋。多角色工作可能在重啟後恢復，pending 保護維持。此調整不改歷史分支／重演要求完整快照的規則。

每次保存前建立原檔備份與 audit.json，記錄時間、分支、前後文字及 prepared/applied/rolled_back 狀態。一般寫入例外還原原檔；多檔更新若程序中斷，依 prepared 日誌與備份人工恢復，不宣稱跨檔交易。

### 角色卡敘事風格

附加功能的「敘事風格」可選原有或自然互動（試用），預設原有，依卡片保存在此瀏覽器。選擇套用到下一次送出、重新生成或編輯；單卡、多角色皆支援。切換本身不寫入對話、記憶或關係；後續生成仍沿用既有保存流程。切回原有只停止注入新增指引，不回寫已接受的自然互動正文。


### 可組合的劇情與場景選項

Card Mode 單卡與多角色的 send、regenerate、edit 同時支援三個獨立選項：文風 narrative_style（original / natural）、劇情主動性 plot_mode（follow / explore / coauthor）、場景模式 scene_mode（default / intimate）。缺少或 null 採 original、follow、default；非法值在生成與存檔變更前回 400。前端依卡片保存本機偏好，本次選擇套用於下一次生成，GM 訊息記錄三個選項，不回寫舊回合。

探索允許合理的新事件、線索與場景配角，覆蓋原有「不得另設事件」限制，但不替玩家決定、不捏造共同往事、不自動新增固定角色。親密互動聚焦眼前人物、情緒與感官細節；與探索並用時優先發展當前互動、減少外部打斷，尊重離場與快轉。這是獨立的 Card Mode 提示選項，不沿用舊世界模式 pistol 的停用記錄或強制維持場景機制。

提示依序追加自然互動、探索、親密互動，編輯指示仍在其後；預設組合不追加任何提示。Recorder、既有狀態規則與回覆長度保持原有流程，沒有額外模型呼叫。tests/test_card_styles.py 覆蓋十二種組合及單卡／多角色 API 送出、重生、編輯與非法值無寫入；瀏覽器測試涵蓋偏好保存與請求欄位。


### 探索：舞台服務角色與共同寫作

候選提示 `inspiration/coauthor_exploration_candidate.txt` 嘗試人物驅動的創作接力：先演好作者已安排的內容，再提供具體反應、願望或場景機會；已明示授權的結果要演完，未指定的玩家選擇仍留白。作者改向時放下未發生原案，不以增加事件、配角或固定戀愛反應為目標。自然停筆仍有效，不必每輪遞新鉤子。參見 [真實舞會基準](inspiration/coauthor_benchmark.md)。使用者審閱後決定兩版並列：`explore` 維持原探索，`coauthor` 使用 `styles.COAUTHOR_EXPLORATION`，內容與已試跑候選逐字一致；預設 follow 不變。不新增角色狀態或素材自動注入。


「探索・共同創作」使用 `plot_mode: "coauthor"`，單卡與多角色的送出、重生、編輯皆適用，並記錄在 GM metadata。原探索使用 explore，兩者互斥而不疊加；可各自搭配自然互動及親密互動。介面沿用每卡片偏好保存，不改使用者原有選擇、不額外增加模型呼叫。

### 單角色編輯與歷史整理失敗

編輯回合及採用候選版本使用所選 GM 回合的完整 pre_state_snapshot、pre_npcs_snapshot、pre_clock_snapshot 還原，因此更早回合的 card_record_status=error 或 PID 確認已不存在的單卡 pending 不再阻擋覆蓋或建立新分支。失敗標記仍原樣保留，這不表示漏記已補齊。缺少或格式錯誤的回合前快照仍拒絕；歷史 pending 若仍在執行或 PID 無法確認則繼續阻擋；已確認程序不存在者只在完整檢查點之前可跨過，原標記不變。多角色檢查不變；一般分支使用所選回合完成後的完整快照。

### 小說化承接與自然收尾

啟用 output.weave_player_input 的卡片會將玩家本輪故事內容編入 GM 正文。玩家台詞按原意演出，允許修正錯字、語序並依人物口吻潤飾措辭、語氣與節奏；保留意思、立場、情緒及重要資訊，不增加表態、承諾或決定，明示逐字保留的用語照原文。玩家輸入保存原文，既有對話不回寫。演完本輪內容即可停筆，最後一段可依敘事視角為台詞、動作、內心、場景或沉默；探索／共同創作也不要求每輪結尾塞鉤子。這些是敘事提示，非文字後處理或模型品質保證，不改 Recorder 規則或增加模型呼叫。

單卡與多角色皆依同一輸入內的互動需要交錯演出：玩家前句／動作 → 對方回應 → 玩家已提供的後句，不一律將整則輸入視為一次說完。潤飾不能併掉不同對話時點；明示不中斷或等說完再回答時保留完整說話段落。句號與換行本身不強制插話，不新增玩家未提供的後續台詞或決定，尚待同意或條件未成立時仍停下。

### 回覆候選與創作接續

最新生成的 GM 回覆旁有「編輯正文」，可直接修改全文並保存新版本。只有最後一輪可操作（開場除外）；舊正文保留於同輪版本列表，新版立即採用，不增加重生次數、不建立分支。保留原記憶、關係、物品、時鐘及快照，不呼叫模型或重新整理記憶；與原有「編輯回合」重演功能分開。保存前核對原版本／正文，過期或保存失敗保留草稿；正在生成或 Recorder pending 時等待，歷史失敗不自動補記。新版的收藏、讚與註記預設空白，原標記仍屬舊版。保存未改動內容不新增版本。

手動版可沿用版本列表翻閱／採用／收藏與歷史分支機制，切回該版不重新整理記憶。改動日期顯示為手動編輯，基底模型／生成時間仍保留。多角色沿原知情範圍搬移文字位置，跨界的新片段維持未分享，不推斷新增事件。僅移除涵蓋被替換正文的摘要，舊背景摘要不能覆蓋新正文；早期有效摘要保留。

Card Mode 的 regen 在目前分支保存被替換回覆與快照，以玩家回合 `reply_group_id` 和候選 `reply_version_id` 區分。索引在回退／重置／編輯後可以重用，但新回合不繼承舊候選與計數。保存的版本可翻閱、收藏；只有明確採用才改動遊戲。最新回合切版不建立分支；較早回合切版建立獨立完整快照分支，原線後續保留。

已完成候選還原其當時狀態。記錄未完成的候選從其回合前快照與已保存正文重新整理記憶，使用新的 Recorder 工作 ID；舊 worker 遲到時不能寫回。多角色同時還原全員狀態，最新回合保留已明示的焦點／出場控制。摘要保留範圍在替換點之前，舊摘要 worker 的 revision 失效。未採用候選不會被長期記憶、回憶搜尋或摘要當成已發生的事。

重生次數只表示已記錄的候選探索，不能作為不滿意或角色品質的分數；失敗另計，舊次數不回補。收藏保留本線已回退版本的正文；已不在時間線的回合不直接套用。「繼續發展」沿用玩家既有的括號指示，透過同一回合生成流程讓 GM 接續。

GM 回覆（含開場）可按 👍，有讚後出現可選註記入口；取消讚不刪除註記，無讚但有註記仍可編輯。讚與註記屬於具體回覆版本，不跟著 index 跑到 regen 或重置後的新回覆。版本視窗也能標記未採用版本。一般／編輯／候選分支繼承保留回合的候選與評語，分支間後續編輯互不影響。與既有收藏分開保存，不解鎖內容、不改關係，也不代表任何劇情選擇。

### 本線持續指示

輸入框上方可保存自由文字演出要求並啟用／關閉，例如艾迪言行的潤飾語氣。保存後套用後續送出、繼續發展、重生與編輯重演，本輪明確要求優先。關閉保留可編輯文字，清空會一併關閉；修改草稿要按保存才生效。

這是作者的分支設定，不是角色狀態或劇情記憶，回退／重置／切回舊候選都保留目前設定。一般／編輯／歷史候選新分支繼承建立時的來源設定，後續各自修改；舊回覆 metadata 仍保留當時實際指示。無指示的舊存檔預設關閉，不回補既有對話。

### 授權自行續寫的玩家控制

Card Mode 本輪要求「請繼續發展」或明確授權代演時，GM 可以代寫玩家角色的台詞、內心及重要決定，優先於提示中的一般不代寫限制。授權不自動延續到下一輪一般輸入，不更換劇情模式、角色設定或記錄流程；人物界線與既有事實仍有效，不保證特定結局。

舊版 prompt 選項：`plot_mode: legacy_20260908` 替換角色扮演契約為 Git `abd788b` 的 2026-09-08 初版，不疊加探索／共同創作規則。角色卡、記憶、摘要及其他風格選項仍按目前流程，故不等同重現歷史完整 API 請求。搭配 original 文風與 default 場景可避免額外風格增補。預設及既有選項不變。

### Card Mode 按需探索

角色卡選用 `exploration_material_bank: "xianxia"` 時，輸入區顯示「探索 ✦」。點擊一次帶一個短引子及輸入框中的可選方向，沿用本線正常演出、記錄與回合快照；素材本身不成為已發生事實。角色、物件來歷、是否接下及後續仍由 GM 與玩家互動形成，不生成預定任務流程。之後「繼續發展」不抽取新素材；重生沿用同份玩家訊息，因此不暗中換事件。未設定素材庫的卡片不受影響。


### 可查證的背景資料維護

單卡的 Pi 維護任務可以整理 memory 增量／合併、lore 與摘要；目前以 CLI 啟動，尚未改成每回合自動觸發。經原文引文核對與版本檢查後可直接套用，不需逐條審核；不確定則整批保留原資料。

合併記憶只作用於當前角色資料及最新回合 post snapshot，早期回合與 pre snapshot 留原樣。回溯或採用歷史候選仍恢復該時間點資料，不能將後來的資訊塞入過去。正式寫回保留備份與查證紀錄；有新進度時禁止直接 undo 覆蓋。模型查證失誤仍可能發生，引用存在不等於語義必然正確。ensemble 暫不使用此整理器。


Pi 維護 policy 2：GM 回覆不增加阻塞式 review。長期 memory／lore 維持原文＋既有設定一致性核對及獨立複核；近期演出一句新聲稱不能直接升格成長期事實。摘要允许省略非關鍵细節，但重要約定、否定與因果須保留；摘要專用任務不固定做第二個模型複核。用量連同任務結果持久保存，失敗亦計入調整基準。自動接遊玩仍屬後續工作。


### 探索與繼續先填草稿

探索支援不限／小事件／大事件，大小代表遭遇或篇章舞台而非結果重要性。已有大事件時優先展開其中的遭遇，不強制每次另開新篇章。大小選擇依卡片記在本機；選擇不生成，按探索只將引子與原有方向填入文字框。繼續則在原有草稿後追加繼續指示；玩家可修改或清空，親自送出後才影響遊戲。


探索素材的世界觀前提：全部 48 條仍可依大小抽取，不新增按鈕或啟用開關。有既定人物、勢力、規則就承接；未建立時改寫成首次接觸、新發現或正在形成的事件，不替本線補寫舊盟約、祖師淵源或共同歷史。涉及渡劫、飛升、幽冥等普世規則時，先呈現局部現象、具體嘗試或明確歸屬的說法，依後續證據展開；允許當下真實的新遭遇，並非所有素材都只能當傳聞。已確立設定與素材衝突時，以既定設定為準改寫素材。人妖會盟、祖庭、渡劫等七條素材另提供有／無背景的切入方式，ID 保持不變。探索依舊只填草稿，由玩家手動送出。

探索素材的小／大按入口形態區分：小事件是靈泉改道、雷劫將至等具體仙俠遭遇點；大事件是論道大會、魔潮等可容納多條發展的舞台。不以場面強弱分類，不強制小事件銜接大事件。原有 24 條通用小事件已移除，替換為 24 條仙俠引子並使用新 ID；大事件素材保持原樣。

探索大小只影響抽取池，草稿僅放事件名稱、描述與玩家原有輸入；不另加派遣、選項、節奏或世界觀建立指令。素材描述亦不內嵌這類指令。


Pi 隨身物與背景維護：`inventory` 與 memory/lore/summary 使用同一查讀、提案、逐項引文、獨立複核及版本檢查流程。隨身物 operation 只可替換現有 target_ids，item_name 是統一名稱，text 空字串表示刪除；合併必須包含所有目的條目，不累加同批重複數量。原始敘事與舊回合快照不改，套用更新 character_state 與最新 GM 的 post-state inventory，保存備份及 token usage。不能因久未提及就刪物。

`pi_schedule` 按已完成且 Recorder 成功的 user/GM 配對計數，預設每 50 個來回（約 100 則訊息）啟動一批四類維護；每批最多十二筆操作，不保證掃完全部歷史。只在啟用卡片的 Recorder 結束後啟動背景執行緒；查讀可搜尋完整凍結歷史，預讀近期 20 則，其餘依需要搜尋。模型工作不持遊玩鎖，不阻塞送出。分支獨立計數；新分支或回退失去計數錨點時重設基準。失敗、超時或遊玩／摘要已更新則不套用，隔下一個 50 回合再試，不反覆耗 API。一般即時摘要仍保留，Pi 是補充維護；不啟用時無模型費用。排程狀態保存在分支 pi_schedule_status.json，設定在卡片 pi_schedule.json。


Pi 整理後回退：memory/inventory 沿 GM post-state 快照恢復，回到整理落點保留、回到更早點恢復舊值（舊重複資料可能重現）。Pi lore 保存 previous_version 與 maintenance_anchor，讀取時依保留 GM 的 index/hash 選出有效版本；新條目在整理落點被移除後不可用，替換條目則回復舊版本。分支繼承同一版本鏈但獨立存檔，同編號不同正文不復活未來 lore；維護再抽取時僅讀有效版本。摘要原有 covered-prefix 檢查保留仍完全有效的歷史摘要，回退進其覆蓋範圍則清除並更新 revision，阻擋舊背景提交。回退／重置當下校正 Pi 回合計數，舊任務不得覆蓋重設後的計數帳本。
驗證：`tests/test_pi_rewind.py` 透過真實 rewind/fork API 驗證四類資料，無正式故事修改或模型呼叫。

隨身物寫入拆分：`set_inventory` 僅更新／刪除逐字匹配的現有名稱；`add_inventory` 只新增含中文且尚不存在的名稱。任一非法名稱使整批拒絕並回傳現有名稱讓 Recorder 修正，不再把改名當新增。Pi 替換目的名稱亦須含中文（刪除舊英文項目仍允許）。數量變更須有原文明確依據，不因閉關或快轉自動推算消耗。

### 單女角 Galgame（選用）

`card.json.galgame.enabled=true` 啟用 `story_core/card_mode/galgame.py`；persona.age 與 player.age 均須為 >=18 的整數。範例 `linwan_galgame`（林晚・雨巷書店），可從 `/card` 或 `/card/linwan_galgame` 進入，首次開啟沿用既有初始化。第一版不支援轉成多角色副本。其他卡預設不啟用。

分支 character_state.galgame 保存好感、信任、反感、性慾（0–100）、加分日額／回合冷卻／近期獎勵指紋與本回合判定。初始均為 0；好感階段 0 陌生、15 相識、40 熟悉、65 曖昧、80 深厚好感，並不自動宣告交往。反感 >=15 覆蓋為戒備，>=40 為厭惡。

- 具體體貼或尊重：每次好感與信任最多各 +1，兩次獎勵間隔至少 3 回合，每個劇情日最多 3 次；最近 30 個已獎勵的相同原文證據不重複獎勵。普通聊天、承諾、稱讚不自動加分。不同措辭是否屬重複行為仍由分類模型判斷。
- 有反感時，符合獎勵條件的互動只減反感 1，不增加好感或信任。道歉不立即恢復數值；時間本身不消除反感。
- 追問私事需好感20／信任15（扣4）；約會30／20（扣6）；牽手45／30（扣10）；擁抱55／40（扣12）；親吻70／55（扣18）；告白65／50（扣8）。這是嘗試直接實行的門檻；未達標時禮貌詢問會婉拒、不扣分。被拒後糾纏算施壓。
- 通過門檻且反感 <15 只代表可考慮；肢體接觸還需最近角色回覆中對這次行動的明確同意。否則按越界扣好感與信任、等量增加反感。侮辱扣12、施壓扣20；既有反感 >=15 時懲罰再加5；好感最低0也會繼續累積反感。
- 性慾與好感／同意分離；熟悉後調情 +1，符合關係條件且有同意的肢體接觸 +2，越界歸0。每經過60劇情分鐘減1，保留不足一小時的餘額，於下一回合結算；不因數值而自動同意或觸發親密劇情。

生成之前先分類與純函式結算，將本輪拒絕／戒備與確切結果交給旁白；生成成功才保存。GM pre-state 保留結算前、post-state 保留結算後，沿用回退、重生、候選、編輯及分支快照。Recorder 不得修改 galgame 或關係階梯，重錄不重複加分。分類或生成失敗不結算。日額使用劇情日期，快轉多日也只結算當前一次互動。

Galgame 卡的 persona.verbal_tics 可省略或留空，不要求固定口癖；林晚卡不設定口癖，側欄不顯示此項。

互動分類使用結構化 JSON（不是模型自行選擇是否呼叫工具）；格式或同意引文不合格最多自動修正一次，失敗不結算、不用普通互動掩蓋錯誤。玩家證據由程式直接取原文。

曦和紀事卡：男性出生機率約1/500，法定成年20歲，艾迪成年日開場。生活津貼、孕期補助及男嬰獎勵皆為虛構敘事設定，尚無銀行／懷孕／補助結算引擎，不自動寫分數或代替玩家做選擇。
