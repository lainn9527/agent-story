# Changelog

All notable changes to the Story RPG project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **戰力等級演出指南（全等級）**: `story_design/story_original/system_prompt.txt` 新增 D/C/B/A/S 五級敘事落地表（攻擊描寫、環境影響、受傷描寫、旁觀者反應）與反面檢查，補齊「等級定義」到「實際演出」的落差。 ([#134])
- **NPC tier 結構化資料流**: async extraction prompt 新增 `tier` 欄位，支援 15 個 sub-tier（`D-/D/D+/.../S+`）；`_save_npc()` 新增 allowlist 正規化，確保 NPC 強度標記可穩定持久化。 ([#134])
- **State RAG 索引（`state.db`）**: 新增 `state_db.py`，把 inventory/ability/relationship/mission/system/npc 建成可檢索索引；支援 lazy rebuild、must-include entity 保底、CJK bigram 搜尋與分類輸出 `[相關角色狀態]`。 ([#136])
- **State 索引維護 API**: 新增 `POST /api/state/rebuild`，可針對指定分支從 canonical `character_state.json` + `npcs.json` 強制重建 `state.db` 並回傳 summary。 ([#136])
- **Extraction ops 契約（event/state）**: `_extract_tags_async()` 新增 `event_ops`（id-driven update/create）與 `state_ops`（set/delta/map_upsert/map_remove/list_add/list_remove）優先路徑，並保留 legacy `events` / `state` fallback 相容。

### Changed
- **GM 上下文注入 tier 證據**: `npc_profiles` 會顯示 `【X 級】`，`critical_facts` 會顯示 `·X級`；有已知 `tier` 且分類為 ally/hostile 的 NPC 時，`_build_augmented_message()` 會注入 `[戰力等級提醒]`。 ([#134])
- **同請求 NPC 讀取優化**: `/api/send`、`/api/send/stream`、`/api/branches/edit*`、`/api/branches/regenerate*` 路徑改為單次載入 `npcs`，並傳入 `_build_story_system_prompt()` / `_build_augmented_message()`，避免同回合重複讀檔。 ([#134])
- **Events 分支一致性**: fork（create/edit/regenerate 與 stream 版本）會複製 parent 在 `branch_point_index` 之前的事件（含 `message_index IS NULL` legacy 條目）；merge 時 child 事件會 upsert 回 parent，同標題以 child `status` 覆蓋。 ([#135])
- **System prompt 改為 Core State + On-demand State RAG**: `{character_state}` 改為核心欄位精簡文本（含 systems），`{npc_profiles}` 改為統計摘要；詳細道具/技能/NPC 檔案改由 `_build_augmented_message()` 按需注入。 ([#136])
- **分支語義對齊（snapshot rebuild）**: fork/edit/regen/blank/merge 等分支操作不再複製 parent DB，而是用該分支時點的 `state_snapshot`/`npcs_snapshot` 重建 `state.db`，避免索引與分支時態漂移。 ([#136])
- **State RAG 檢索限流**: `search_state()` 新增 `category_limits`/`max_items` 後處理限流；預設注入改為「最多 30 條、NPC 類最多 10 條」，但 `must_include_keys` 保底條目不受類別上限限制。
- **Prompt 去偏置**: `system_prompt.txt` 與 `prompts.py` 的固定人名示例改為中性示例，降低空白分支固定生成同名 NPC 的偏置風險。

### Fixed
- **tier 覆蓋穩定性**: extraction prompt 補充規則「既有 NPC 若本回合無法判定 tier，省略欄位不要輸出 null」，搭配 `_save_npc()` 的 invalid-tier 忽略邏輯，避免合法 tier 被不確定輸出污染。 ([#134])
- **Events orphan 清理**: 分支清理（failed branch cleanup、hard delete、`was_main` soft-delete、startup incomplete cleanup）會同步刪除 `events.db` 對應 `branch_id`，避免 dead data 殘留。 ([#135])
- **State RAG 檢索噪音控制**: must-include entity 抽取忽略單字元 key，降低短詞誤命中造成的無關注入。 ([#136])
- **未選選項回灌污染**: `recent` 在送入 LLM 前會移除所有非 `user` 訊息（含 legacy `assistant`）尾端的「可選行動」區塊；compaction 摘要前也會做同樣清洗，避免提案選項被當成既成事實反覆回灌。 ([#139])
- **事件標題漂移斷鏈**: 透過 `event_ops.update(id,status)` 避免 LLM 輕微改寫 title 就變成新事件，提升 active event close/推進穩定性。

## [0.20.16] - 2026-02-28

### Added
- **角色狀態 deterministic validation gate（Phase 1）**: 寫入前新增 `_validate_state_update` 規則閘門，統一檢查並清理非法 `current_phase`、非數值點數欄位、錯誤型別 map/list 更新、非 schema `_add/_remove`、scene/instruction 汙染鍵等；`_apply_state_update` 與 `_normalize_state_async` 共同走 `_run_state_gate`，在 `enforce` 模式下只套用清洗後更新。新增完整測試覆蓋 `tests/test_state_review.py`。 ([#128])
- **LLM reviewer repair path（Phase 2）**: `enforce + STATE_REVIEW_LLM=on` 時，對 violations 啟用 `_review_state_update_llm` 產生 `patch/drop_keys` 修補建議，並二次套用 deterministic gate 驗證後才可寫入。新增 `tests/test_state_reviewer.py` 覆蓋 reviewer 成功/失敗/格式錯誤/二次驗證路徑。 ([#128])
- **Structured LLM trace logging**: 新增 `llm_trace.py`，在 `gm/oneshot/lore chat/lore organizer` 等路徑記錄 request/response 原始 payload，檔案落在 `data/llm_traces/<story_id>/<YYYY-MM-DD>/<branch_id>/msg_<index>/...json`，支援 `LLM_TRACE_ENABLED` 與 `LLM_TRACE_RETENTION_DAYS`。 ([#132])

### Changed
- **State review 預設升級為強制審核**: 預設 `STATE_REVIEW_MODE=enforce`、`STATE_REVIEW_LLM=on`，直接以 gate + reviewer 作為 production 預設行為。 ([#128])
- **Reviewer timeout 預設調整為 20 秒**: `STATE_REVIEW_LLM_TIMEOUT_MS` 預設從 1800ms 提升至 20000ms，提高 reviewer 命中率。 ([#128])

### Fixed
- **Reviewer 穩定性與資源保護**: 新增安全 env parse、inflight semaphore 上限、usage logging（`oneshot_state_review`）、out-of-scope patch key 過濾，避免 reviewer 注入未授權鍵與不受控併發。 ([#128])
- **數值型欄位 bool 漏洞**: 明確排除 `bool` 被視為 `int` 的情況，修復 `reward_points`/`*_delta` 在 gate 與 apply 路徑可能被 `True/False` 誤用而污染狀態。 ([#128])

[#128]: https://github.com/lainn9527/agent-story/pull/128
[#132]: https://github.com/lainn9527/agent-story/pull/132
[#134]: https://github.com/lainn9527/agent-story/pull/134
[#135]: https://github.com/lainn9527/agent-story/pull/135
[#136]: https://github.com/lainn9527/agent-story/pull/136
[#139]: https://github.com/lainn9527/agent-story/pull/139

## [0.20.15] - 2026-02-27

### Changed
- **Extraction prompt 防膨脹改進**: 新增「道具欄清理原則」（禁止場景狀態寫入 inventory、已消耗/已融合道具自動移除、召喚物簡化追蹤、隊友狀態歸入 relationships、50 項 soft cap）和「技能列表維護原則」（升級時移除舊版本、同系列只保留最高階、systems 已涵蓋的不重複列出），解決 character_state 正向回饋膨脹問題 ([#125])

[#125]: https://github.com/lainn9527/agent-story/pull/125

## [0.20.14] - 2026-02-27

### Added
- **戰力等級框架**: System prompt 新增 D/C/B/A/S 五級戰力定義、級間差距規則（同級/+1/+2/+3）、GM 約束（敵人強度一致、成長有跡可循、副本難度匹配、禁止套路化以弱勝強），解決 GM 無視等級邊界的問題 ([#124])

[#124]: https://github.com/lainn9527/agent-story/pull/124

## [0.20.13] - 2026-02-26

### Fixed
- **命運骰結果洩漏至敘事**: GM 直接輸出「命運走向：順遂」「天命的機緣降臨」等命運骰結果，破壞沉浸感。根因：context injection 包含結果標籤名稱（天命/順遂/平淡/波折/劫數），system prompt 也列出完整定義。修正：(1) 新增 `_OUTCOME_GM_HINTS` 用方向性描述（極度有利/偏向有利/中性/偏向不利/極度不利）取代標籤名稱 (2) system prompt 移除結果列表 (3) `_process_gm_response()` 加入 `_FATE_LABEL_RE` 安全網清理殘留標籤 ([#123])
- **存檔讀取語意穩定化**: `Load Save` 保持 bookmark 模式（顯示存檔快照狀態 preview，但不回滾分支訊息），並補齊 preview 生命周期清理（send/send_stream/switch/edit/regen/create）與 stale metadata 自癒；同時新增 save/load API 測試覆蓋 (`api_send_stream`、`api_branches_switch`、`api_branches_edit`、缺失 save/metadata 邊界案例) ([#120])

[#120]: https://github.com/lainn9527/agent-story/pull/120
[#123]: https://github.com/lainn9527/agent-story/pull/123

## [0.20.12] - 2026-02-24

### Fixed
- **角色狀態重複項目**: LLM 跨回合常用微妙不同的名稱指同一物品（如 "G病毒原始株" vs "G 病毒·原始株"、"C級支線劇情" vs "C 級支線劇情"），導致道具欄/人際關係/體系累積重複。新增 fuzzy key matching 層，標準化空白、中間點、破折號、括號、全形英數字後比對，更新時自動對應到既有 key，並支援 base-name fallback 移除 ([#114])

[#114]: https://github.com/lainn9527/agent-story/pull/114

## [0.20.11] - 2026-02-19

### Fixed
- **關閉骰子仍顯示命運走向**: `_FATE_LABEL_RE` 只匹配全形括號 `【】`，漏掉 GM 常用的半形 `[]`（如 `**[命運走向：順遂]**`）。fate mode 關閉時歷史訊息未被 strip，GM 從 context 模仿繼續輸出。修正 regex 支援兩種括號及 `效果/觸發/結果` 後綴 ([#113])

[#113]: https://github.com/lainn9527/agent-story/pull/113

## [0.20.10] - 2026-02-18

### Fixed
- **手槍模式 lore/event 洩漏**: 手槍模式開啟時，`_extract_tags_async()` 仍會提取 lore 和 event 存入分支資料，fork 時會帶到新分支。現在手槍模式下跳過 lore + event 提取，NPC/state/time 等照常運作 ([#111])

[#111]: https://github.com/lainn9527/agent-story/pull/111

## [0.20.9] - 2026-02-18

### Fixed
- **體系升級未同步**: Async extraction LLM 對體系（systems）等級升級輸出 `state no change`，因 prompt 缺乏明確指引。新增規則：GM 文本顯示體系等級變化時，必須輸出 `systems` map 更新。同時補強 `_apply_state_update_inner` 支援 `schema.fields` 中 `type: map` 的欄位（defensive fix）並新增 5 個測試 ([#112])

[#112]: https://github.com/lainn9527/agent-story/pull/112

## [0.20.8] - 2026-02-18

### Fixed
- **Fate strip 自我強化**: GM 在 fate mode OFF 時自行生成 `【判定：大成功】` 等格式，留在 recent window 造成後續回合持續模仿。擴大 `_FATE_LABEL_RE` regex 範圍，也清除 `【判定：】`、`【判定結果：】` 變體 ([#110])

[#110]: https://github.com/lainn9527/agent-story/pull/110

## [0.20.7] - 2026-02-18

### Fixed
- **副本世界觀隔離指示**: System prompt 新增「副本世界觀隔離」規則，禁止 GM 在副本中引入其他副本的角色、怪物、科技或設定（如民俗恐怖副本出現電磁脈衝武器） ([#109])

[#109]: https://github.com/lainn9527/agent-story/pull/109

## [0.20.6] - 2026-02-18

### Fixed
- **分支 fork 遺失 current_dungeon**: 編輯/重生成建立新分支時，歷史 state snapshot 缺少 `current_dungeon` 欄位，導致新分支失去副本上下文。新增 `_backfill_forked_state()` 從 source branch 繼承 ([#108])

[#108]: https://github.com/lainn9527/agent-story/pull/108

## [0.20.5] - 2026-02-18

### Fixed
- **副本 Lore 跨副本污染**: 玩家在副本中時，其他副本的世界設定不再被注入 GM 上下文。`search_hybrid()` 和 `_search_branch_lore()` 對非當前副本的 `副本世界觀` 條目施加 0.1x 分數懲罰 ([#107])
- **編輯無變更後端守衛**: 編輯訊息但內容未變更時，後端直接返回 400 `no_change`，不再建立新分支或呼叫 LLM。前端同步修正 DOM 還原和 toast 提示 ([#107])

### Added
- `current_dungeon` 角色狀態欄位：追蹤玩家當前所在副本名稱，由 LLM 抽取自動維護。進入/離開副本 API 同步設定 ([#107])
- `scripts/migrate_current_dungeon.py` 資料遷移腳本：為既有分支回填 `current_dungeon`，支援 `--dry-run` ([#107])

[#107]: https://github.com/lainn9527/agent-story/pull/107

## [0.20.4] - 2026-02-18

### Fixed
- **Lore 全面 (subcategory, topic) 聯合比對**: 修復所有 lore CRUD 操作只用 topic 比對的系統性問題，改為 (subcategory, topic) 聯合識別 ([#105])
  - `DELETE /api/lore/entry` — 刪除「進擊的巨人/介紹」不再連帶刪除 32 條同名條目
  - `DELETE /api/lore/branch/entry` — 同上，分支 lore 刪除
  - `PUT /api/lore/entry` — 編輯時正確定位同 subcategory 的條目
  - `_save_lore_entry()` — 連續採用兩個副本推薦不再互相覆蓋
  - `_save_branch_lore_entry()` — 分支 lore 自動擷取不再覆蓋同 topic 條目
  - `_merge_branch_lore_into()` — 分支合併時保留不同 subcategory 的同名條目
  - `POST /api/lore/promote` — 提升分支知識到 base 時精確匹配
  - `POST /api/lore/apply` delete action — chat 提案刪除精確匹配
  - 前端 `updateEntry()`、`saveModal()`、promote 按鈕 — 傳送 subcategory
- **lore.db 搜尋索引 schema 升級**: `topic UNIQUE` → `UNIQUE(subcategory, topic)` 複合唯一鍵，自動遷移舊 DB，保留 embeddings ([#105])
  - `rebuild_index()`、`upsert_entry()`、`delete_entry()` 全面改用 (subcategory, topic) 查詢

[#105]: https://github.com/lainn9527/agent-story/pull/105

## [0.20.3] - 2026-02-18

### Fixed
- **Lore 抽取過於積極**: 收緊 branch lore 提取標準，加入「GM 在未來其他場景是否需要此設定？」判斷門檻；明確禁止一次性場景細節（具體房間、走廊、臨時戰場），將劇情事件導向 events 追蹤 ([#106])

[#106]: https://github.com/lainn9527/agent-story/pull/106

## [0.20.2] - 2026-02-18

### Changed
- **體系改為 key-value map**: `systems` 從 list 改為 map 格式（`{"死生之道": "B級"}`），與 inventory/relationships 一致，更新時直接覆蓋 ([#104])

### Fixed
- **Async state extraction 被跳過**: GM 輸出 `<!--STATE-->` tag 時，async extraction 的 state 提取被 `skip_state=True` 完全跳過，導致 STATE tag 不完整時 systems/abilities 等欄位漏更新；現在 async extraction 永遠執行 state 提取 ([#104])

[#104]: https://github.com/lainn9527/agent-story/pull/104

## [0.20.1] - 2026-02-18

### Added
- **Lore helper subcategory field**: 新增/編輯 modal 加入「子分類」欄位，支援 `副本世界觀/副本名/介紹`、`體系/體系名/介紹` 等層級結構 ([#103])
- **Lore chat subcategory support**: AI 提案格式加入 `subcategory` 欄位；系統提示規範 副本世界觀/體系/場景 的命名慣例 ([#103])

### Fixed
- **Topic 唯一性 scoped 化**: 建立/更新 lore 條目時，重複檢查範圍縮小為同一 `(subcategory, topic)` 組合，允許不同副本各有「介紹」條目 ([#103])
- **PUT subcategory 更新**: 編輯時可修改 subcategory；subcategory 變更時同樣觸發衝突檢查 ([#103])
- **Lore apply delete 精確化**: chat 提案 delete 操作改為 (subcategory, topic) 聯合識別，避免跨副本誤刪同名條目 ([#103])

[#103]: https://github.com/lainn9527/agent-story/pull/103

## [0.20.0] - 2026-02-18

### Changed
- **道具欄改為 key-value map**: `inventory` 從 list 改為 map 格式（`{"道具名": "狀態"}`），同名道具自動覆蓋，從根本上解決進化道具重複堆積問題 ([#102])
- **Schema render hint**: 新增 `"render": "inline"` schema 欄位，人際關係保持 `name：value` 單行顯示，道具欄使用 block 雙行佈局 ([#102])

### Added
- **Backward compat shim**: 舊版 `inventory_add`/`inventory_remove` STATE tag 自動轉換為 map delta 格式 ([#102])
- **Auto-migration on load**: 載入分支時自動偵測 list 格式 inventory 並無損轉換為 map（不合併同 base name 的不同道具） ([#102])
- **Map null removal**: `{"inventory": {"道具名": null}}` 可移除道具 ([#102])

[#102]: https://github.com/lainn9527/agent-story/pull/102

## [0.19.4] - 2026-02-18

### Fixed
- **輸入框送出後不縮回**: 送出訊息後 textarea 保持展開狀態不會縮回單行；同時修正 `fillInputWithOption()` 缺少 120px 高度上限 ([#101])

[#101]: https://github.com/lainn9527/agent-story/pull/101

## [0.19.3] - 2026-02-17

### Fixed
- **道具重複堆積**: `inventory_add` 時自動替換同 base name 的裸名舊道具（如 `武器` → `武器（強化版）`），有後綴的變體（如 `定界珠（生）` vs `定界珠（死）`）和消耗品堆疊不受影響 ([#100])
- **人際關係不更新**: 提取 prompt 缺少 map 類型欄位（relationships）的上下文，LLM 看不到現有關係故無法輸出更新；現已包含並加強更新指示 ([#100])

[#100]: https://github.com/lainn9527/agent-story/pull/100

## [0.19.2] - 2026-02-17

### Fixed
- **命運走向關閉後仍出現劫數**: Strip `**【命運走向：XX】**` labels from conversation history before sending to LLM when fate mode is off, so GM has zero exposure to fate patterns ([#99])

[#99]: https://github.com/lainn9527/agent-story/pull/99

## [0.19.1] - 2026-02-17

### Fixed
- **命運走向關閉後仍出現劫數**: GM mimicked fate terms from conversation history even when fate mode was off; added explicit instruction to ignore historical fate references ([#98])

[#98]: https://github.com/lainn9527/agent-story/pull/98

## [0.19.0] - 2026-02-17

### Changed
- **命運走向系統 (Fate Direction System)**: Replaced binary success/failure dice with fate directions (天命/順遂/平淡/波折/劫數) following 塞翁失馬焉知非福 philosophy — good fortune may hide risks, setbacks may bring gains ([#97])
- **行動合理性 (Action Quality)**: Player RP quality now independently affects outcomes — detailed strategies increase success chance regardless of fate direction ([#97])

### Added
- **命運走向開關**: Fate system toggleable on/off per branch, like pistol mode; when off, system prompt fate section is stripped and no dice are rolled ([#97])
- **必勝模式連動**: 必勝模式 toggles are disabled (greyed out) when fate mode is off ([#97])

[#97]: https://github.com/lainn9527/agent-story/pull/97

## [0.18.0] - 2026-02-17

### Added
- **副本系統 (Dungeon System)**: 13 個副本完整定義（D→S 難度），含主線節點、地圖區域、成長規則 ([#94])
- **硬約束成長控制**: `validate_dungeon_progression()` 在代碼層面 cap 每個副本的等級/基因鎖成長，防止 GM 過度慷慨導致角色過快升級 ([#94])
- **副本進度追蹤**: 每個分支獨立的 `dungeon_progress.json`，記錄主線進度、地圖探索度、成長預算消耗 ([#94])
- **Drawer 副本面板**: 副本中顯示進度條、主線節點、可折疊地圖區域；60% 主線完成後可回歸主神空間 ([#94])
- **系統提示副本上下文**: `{dungeon_context}` 佔位符將副本狀態、節點進度、成長限制注入 GM 系統提示 ([#94])
- **異步 LLM 副本提取**: 擴展 `_extract_tags_async()` 自動從 GM 文本提取副本進度更新 ([#94])

[#94]: https://github.com/lainn9527/agent-story/pull/94

## [0.17.1] - 2026-02-17

### Fixed
- **Dice proportional consequences**: GM now scales failure severity proportional to action risk — low-risk actions (casual chat, simple interactions) only result in minor setbacks, not catastrophic relationship-breaking outcomes ([#96])

[#96]: https://github.com/lainn9527/agent-story/pull/96

## [0.17.0] - 2026-02-17

### Changed
- **Design files separated**: Story design files (`system_prompt.txt`, `world_lore.json`, `character_schema.json`, `default_character_state.json`, `parsed_conversation.json`, `nsfw_preferences.json`) now live in `story_design/<story_id>/` instead of `data/stories/<story_id>/`, enabling git tracking of world-building content while runtime data stays gitignored ([#95])
- Auto-migration on startup copies design files from old location to new; old copies become inert ([#95])

[#95]: https://github.com/lainn9527/agent-story/pull/95

## [0.16.5] - 2026-02-17

### Fixed
- **Inventory dedup**: removal now matches by base name (strips parenthetical status, quantity suffixes, dash descriptions) — fixes items like `大日金烏劍·空燼 (穩定度提升)` being unmatchable for removal ([#93])
- **Remove-before-add ordering**: paired `inventory_remove` + `inventory_add` updates now process removal first, preventing the new item from being nuked by base-name matching ([#93])
- **Garbage key filtering**: LLM intermediate instruction keys (`inventory_use`, `skill_update`, etc.) no longer leak into character state as top-level fields ([#93])

### Changed
- **Extraction prompt**: now includes current inventory/abilities list so LLM can properly pair `_remove` + `_add` for item status changes (root cause of duplicate sword entries) ([#93])
- **Lore extraction exclusion**: character-specific abilities/skills no longer extracted as world lore — redirected to character state instead ([#93])

### Added
- **Abilities schema field**: auto-migration adds `abilities` (功法與技能) list to `character_schema.json` and `default_character_state.json` on startup ([#93])

[#93]: https://github.com/lainn9527/agent-story/pull/93

## [0.16.4] - 2026-02-16

### Changed
- **Dice rebalance**: lower outcome thresholds (80/50/30 → 70/40/20) for a more balanced success curve ([#91])
- Expand attribute lookup keywords to match actual GM descriptions — physique (+8 keywords), spirit (+7), gene lock shorthand (`一階`~`四階`) ([#91])

### Added
- **Beginner bonus**: first 10 turns get linearly decaying dice bonus (+10 → +1), easing new players into the game ([#91])

[#91]: https://github.com/lainn9527/agent-story/pull/91

## [0.16.3] - 2026-02-16

### Removed
- **`story_summary` system** — removed entirely to fix cross-branch context leakage. Blank branch children (edit/regen forks) no longer inherit main-story summary. `narrative_recap` (per-branch, rolling) already covers the same ground. ([#92])
- Removed `generate_story_summary()` from all 3 LLM bridges (`llm_bridge.py`, `claude_bridge.py`, `gemini_bridge.py`) ([#92])
- Removed `has_summary` from `/api/init` and `/api/stories/switch` API responses ([#92])

[#92]: https://github.com/lainn9527/agent-story/pull/92

## [0.16.2] - 2026-02-16

### Added
- **Subcategory field** for hierarchical lore organization — entries now support optional `subcategory` for two-level grouping (e.g. `副本世界觀 > 生化危機`, `體系 > 技能`) ([#90])
- Frontend lore console renders category > subcategory tree with collapsible subgroups ([#90])
- `道具` added to allowed lore extraction categories ([#90])

### Changed
- `技能` and `基本屬性` are now subcategories under `體系` instead of top-level categories ([#90])
- Extraction prompt updated with subcategory guidance for `副本世界觀` (mandatory dungeon name) and `體系` (optional skill/attribute classification) ([#90])
- `lore_db.py`: SQLite schema migration adds `subcategory` column, all search/index/TOC functions updated ([#90])

### Fixed
- Subcategory preserved across all save/update/promote/apply code paths (previously silently dropped) ([#90])
- Branch lore search output now includes subcategory in formatted label, matching base lore search ([#90])
- `find_duplicates` and `lore_chat` system prompt now include subcategory in output/grouping ([#90])

[#90]: https://github.com/lainn9527/agent-story/pull/90

## [0.16.1] - 2026-02-16

### Changed
- Strengthen lore extraction prompt to explicitly exclude character-specific content (personal stats, inventory, combat experiences) and require generic language without character names ([#88])

[#88]: https://github.com/lainn9527/agent-story/pull/88

## [0.16.0] - 2026-02-16

### Added
- **Branch lore system**: Auto-extracted lore now saved to per-branch `branch_lore.json` instead of polluting shared `world_lore.json` ([#84])
- Branch lore search with CJK bigram matching, injected as `[相關分支設定]` in context ([#84])
- LLM-powered promotion workflow: review branch lore entries as promote/rewrite/reject, then promote to base lore ([#84])
- Lore console UI: "分支知識" section with teal badges, "審核提升" button for promotion flow ([#84])
- `DELETE /api/lore/branch/entry`, `POST /api/lore/promote`, `POST /api/lore/promote/review` API routes ([#84])
- `GET /api/lore/all` now returns entries with `layer: "base"|"branch"` field ([#84])
- 40 new tests for branch lore helpers, API routes, branch operations, and context injection ([#84])

### Changed
- Inline `<!--LORE-->` tags and `_extract_tags_async()` now write to branch lore instead of base lore ([#84])
- Branch fork/edit/regen/promote/merge operations copy or merge branch lore ([#84])
- Blank branches start with empty branch lore (no inheritance from parent) ([#84])

### Fixed
- Thread safety for concurrent branch lore writes via per-branch locks ([#84])
- Promote/merge uses upsert-merge semantics instead of overwriting target branch lore ([#84])

[#84]: https://github.com/lainn9527/agent-story/pull/84

## [0.15.0] - 2026-02-15

### Fixed
- Branch tree infinite loop on circular parent references in timeline_tree.json ([#82])
- Branch tree KeyError crash when parent branch is hard-deleted ([#82])
- Event dedup blocking status progression — events now update from planted→triggered→resolved instead of being silently skipped ([#82])
- Extraction prompt updated to instruct LLM to re-emit events with changed status ([#82])

### Added
- Comprehensive test suite: 273 tests across 13 files covering tag extraction, world timer, event DB, lore search, compaction, state updates, branch tree, context injection, async extraction, and API routes ([#82])
- `event_db.get_event_title_map()` for status-aware event dedup ([#82])

[#82]: https://github.com/lainn9527/agent-story/pull/82

## [0.14.9] - 2026-02-13

### Fixed
- Clear narrative recap for blank branches — GM no longer references previous storylines in fresh games ([#83])

[#83]: https://github.com/lainn9527/agent-story/pull/83

## [0.14.8] - 2026-02-13

### Fixed
- Fix `deploy.sh` fetching in wrong directory — `FETCH_HEAD` was stale, always one version behind ([#81])

[#81]: https://github.com/lainn9527/agent-story/pull/81

## [0.14.7] - 2026-02-13

### Fixed
- Fix dice system crash when LLM writes non-string character state values (e.g. `"spirit": 80`) ([#80])

[#80]: https://github.com/lainn9527/agent-story/pull/80

## [0.14.6] - 2026-02-13

### Fixed
- Fix Claude CLI nested-session crash when server is started from Claude Code session — strip `CLAUDECODE` env var from all subprocess calls ([#77])

### Added
- Rotating file logger (`server.log`, 5MB x 4 files) alongside console output ([#77])
- Redirect `deploy.sh` stderr to `server_stderr.log` instead of `/dev/null` ([#77])

[#77]: https://github.com/lainn9527/agent-story/pull/77

## [0.14.5] - 2026-02-13

### Added
- Production deploy workflow: `deploy.sh` for one-command deploy after merge ([#76])
- Production isolation: server runs from `story-prod` worktree, decoupled from main repo ([#76])
- Pre-commit hook blocks direct commits to `main` branch ([#76])

### Changed
- CLAUDE.md: enforce worktree-only development, update E2E data paths and merge process ([#76])

[#76]: https://github.com/lainn9527/agent-story/pull/76

## [0.14.4] - 2026-02-12

### Removed
- Claude CLI tool call (`--allowedTools Read,Grep`) — redundant with critical facts injection ([#75])

[#75]: https://github.com/lainn9527/agent-story/pull/75

## [0.14.3] - 2026-02-12

### Fixed
- Fix character-by-character split in list fields when LLM returns string instead of array ([#72])
- Fix missing spacing between label and value in character status panel ([#72])
- Block scene-transient keys (location, threat_level, etc.) and non-schema `_add`/`_remove` keys from persisting in character state ([#72])
- Filter system keys (world_day, world_time, branch_title) from character state ([#72])

### Added
- Collapsible "其他狀態" section for extra fields in character panel, auto-opens when new fields appear ([#72])
- Client-side NPC key filtering (dynamically derived from npcs.json) hides NPC sub-state from player view ([#72])
- Key name humanization: snake_case → Title Case, CJK keys shown as-is ([#72])
- Load-time self-healing: auto-strips `_delta`/`_add`/`_remove` artifacts and single-char list entries ([#72])
- Polling for async tag extraction updates: status panel refreshes within 5-30s after GM response ([#72])

[#72]: https://github.com/lainn9527/agent-story/pull/72

## [0.14.2] - 2026-02-12

### Added
- Addon panel (⚙) next to send button with quick-access model selection, dice cheat toggle, and pistol mode ([#73])
- Pistol mode (手槍模式): per-branch toggle that injects intimate scene instructions into system prompt ([#73])
- Pistol preferences modal with 134 quick-select chips across 8 categories (風格/體位/前戲/高潮/道具/場景/描寫重點/角色動態) ([#73])
- Custom chip support: add/delete user-defined chips per category, persisted in JSON ([#73])
- Frequency-based chip sorting: commonly used chips rise to top after every 3 uses ([#73])
- Structured preference injection: chips formatted by category in system prompt for better LLM comprehension ([#73])
- LLM pacing instructions: mental roadmap (前戲→升溫→高潮→餘韻), 1-3 elements per reply, 1500+ char minimum ([#73])
- Combined provider+model tree-style dropdown with optgroup in addon panel ([#73])
- Pink header badge for pistol mode, green glow on addon button when any addon active ([#73])
- Per-story NSFW preferences stored as `nsfw_preferences.json` ([#73])

[#73]: https://github.com/lainn9527/agent-story/pull/73

## [0.14.1] - 2026-02-12

### Added
- Critical facts injection into GM system prompt: current phase, world time, gene lock, reward points, key inventory, NPC relationship matrix ([#74])
- Claude CLI tool access (`--allowedTools Read,Grep`) for GM fact-checking against game data files ([#74])
- `_rel_to_str()` helper for normalizing dict-type relationship values ([#74])
- `_classify_npc()` with combined signal classification (dead > hostile > captured > ally > neutral) ([#74])

### Fixed
- Dict-type relationship values in `character_state.json` no longer crash NPC classification ([#74])
- Float-type `world_day` values handled correctly in critical facts ([#74])
- Reward points format safely handles non-numeric values ([#74])

[#74]: https://github.com/lainn9527/agent-story/pull/74

## [0.14.0] - 2026-02-12

### Added
- Embedding-based hybrid lore search with RRF (Reciprocal Rank Fusion) ranking ([#61])
- Local embedding model (`jinaai/jina-embeddings-v2-base-zh`) via fastembed — zero API calls, ~11ms per query ([#61])
- Token-budgeted lore injection (~3000 tokens) instead of fixed top-5 ([#61])
- Location pinning: category boosting based on game phase (副本/主神空間/戰鬥) ([#61])
- Duplicate lore detection endpoint `GET /api/lore/duplicates` ([#61])
- Embedding stats endpoint `GET /api/lore/embedding-stats` ([#61])

### Changed
- System prompt lore section: replaced ~6-8K token TOC with compact category summary (~50 tokens) ([#61])
- Auto-play defaults to `claude_cli` provider with zero Gemini usage ([#61])
- Gemini access blocked at `llm_bridge` level when provider is overridden (single gate) ([#61])

### Removed
- Gemini embedding API code from `gemini_bridge.py` (replaced by local model) ([#61])

[#61]: https://github.com/lainn9527/agent-story/pull/61

## [0.13.7] - 2026-02-12

### Fixed
- Cheat settings (金手指) and branch config lost on edit/regen due to `_resolve_sibling_parent` overwriting source branch ([#71])

### Changed
- Mobile touch UX: larger touch targets (44px min), haptic feedback on buttons, active-state visual feedback ([#71])

[#71]: https://github.com/lainn9527/agent-story/pull/71

## [0.13.6] - 2026-02-12

### Added
- Gemini 3 Flash Preview (`gemini-3-flash-preview`) added to model selector ([#70])

[#70]: https://github.com/lainn9527/agent-story/pull/70

## [0.13.5] - 2026-02-12

### Fixed
- Gemini API transient network errors (e.g. "no route to host") now retry with 2s backoff instead of failing immediately ([#69])

[#69]: https://github.com/lainn9527/agent-story/pull/69

## [0.13.4] - 2026-02-11

### Fixed
- User-edited lore entries no longer overwritten by background auto-extraction (`_extract_tags_async`) ([#68])

[#68]: https://github.com/lainn9527/agent-story/pull/68

## [0.13.3] - 2026-02-10

### Added
- Branch tree "▶ 繼續" toolbar button: one-click jump to last-played branch ([#65])
- Branch tree "⇣" per-node button: jump to deepest descendant leaf of any branch ([#65])
- Backend tracks `last_played_branch_id` on send/edit/regen actions ([#65])

[#65]: https://github.com/lainn9527/agent-story/pull/65

## [0.13.2] - 2026-02-10

### Fixed
- Mobile GM messages: regen button and sibling switcher overlapping with text due to missing bottom padding ([#64])
- `UnboundLocalError: 'tree'` crash in `/api/send/stream` when no siblings were pruned ([#64])

[#64]: https://github.com/lainn9527/agent-story/pull/64

## [0.13.1] - 2026-02-10

### Added
- GM cheat mode: `/gm` prefix commands for direct GM communication and rule changes ([#37])
- Dice always-success toggle in drawer settings with 30/50/20 probability split ([#37])
- Header badge `金手指` when always-success mode is active ([#37])
- Per-branch cheat storage in `gm_cheats.json`, inherited on branch creation ([#37])
- Restore drawer branch list with root-only view: main + non-auto blank branches shown first, auto branches collapsed under "Auto-Play (N)" toggle ([#62])
- Branch tree modal is now contextual: shows only the subtree of the currently active root branch ([#62])

### Changed
- Increased mobile content padding for better readability ([#37])

### Fixed
- Story delete button losing hover-reveal styling due to CSS class rename ([#62])
- Escape during branch rename triggering a save instead of cancelling ([#62])
- Branch action buttons invisible on mobile touch devices ([#62])

[#37]: https://github.com/lainn9527/agent-story/pull/37
[#62]: https://github.com/lainn9527/agent-story/pull/62

## [0.13.0] - 2026-02-10

### Added
- Auto-prune abandoned sibling branches: silently marks siblings as pruned when player moves 5+ steps ahead and sibling has ≤2 delta messages ([#54])
- Heart protection toggle (♥) in branch tree modal and sibling switcher to exempt branches from auto-pruning ([#54])
- Promote (⬆) action in branch tree modal ([#54])
- Toast notification for auto-prune and scissors prune failures ([#54])

### Changed
- Drawer branch section stripped to 🌳 (branch tree) and ⊕ (new blank) buttons only — branch list removed in favor of branch tree modal ([#54])
- Branch tree: single-child chains flattened with dashed left border connector ([#54])
- Drawer toggle shortcut changed from Cmd+T to Cmd+Shift+B (avoids Chrome conflict) ([#54])
- Mobile: branch tree action buttons always visible (no hover required), increased touch targets ([#54])

### Removed
- Drawer branch list, promote button, and new-branch button (replaced by branch tree modal) ([#54])
- Delete-previous-version button from messages ([#54])

[#54]: https://github.com/lainn9527/agent-story/pull/54

## [0.12.4] - 2026-02-10

### Changed
- Git Workflow: enforce user-gated e2e testing before PR merge ([#59])
- Git Workflow: add e2e test setup steps (copy data, random port, start server) ([#59])
- Git Workflow: integrate version bump + changelog into merge checklist ([#59])

[#59]: https://github.com/lainn9527/agent-story/pull/59
