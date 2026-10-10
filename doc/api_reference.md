# API 參考（app.py）

## 使用慣例

- 除非特別指定，API 會以 `active_story_id`（`data/stories.json`）為目標故事。
- 大部分端點回傳 JSON，常見格式：
  - 成功：`{"ok": true, ...}`
  - 失敗：`{"ok": false, "error": "..."}`
- 分支查詢常用 `branch_id`；未提供時多數預設 `main` 或 active branch。

## SSE 串流格式

以下端點使用 `text/event-stream`：

- `POST /api/send/stream`
- `POST /api/branches/edit/stream`
- `POST /api/branches/regenerate/stream`
- `POST /api/lore/chat/stream`

常見事件 payload：

- `{"type":"text","chunk":"..."}`
- `{"type":"dice","dice":{...}}`
- `{"type":"done", ...}`
- `{"type":"error","message":"..."}`

`/api/send/stream` 的 `done` payload 會包含：
- `user_msg`: 後端實際落盤的玩家訊息（含 authoritative index）
- `gm_msg`: 後端實際落盤的 GM 訊息

---

## 頁面與初始化

| Method | Path | 說明 |
|---|---|---|
| GET | `/` | 主遊戲頁（`templates/index.html`） |
| GET | `/lore` | Lore Console 頁面 |
| POST | `/api/init` | 初始化與遷移（stories/design/tree/schema 等） |

## 對話與狀態

| Method | Path | 說明 | 主要參數 |
|---|---|---|---|
| GET | `/api/messages` | 取得分支完整 timeline（含 fork/sibling/world_day；不含 per-message snapshot 欄位） | `branch_id`, `offset`, `limit`, `after_index`, `tail` |
| POST | `/api/send` | 送出玩家訊息，取得完整 GM 回覆 | body: `message`, `branch_id` |
| POST | `/api/send/stream` | `send` 的 SSE 版本 | body: `message`, `branch_id` |
| POST | `/api/v2/send` | **（實驗）GM v2 單次 grounded 管線**；與 `/api/send` 並存、互不影響 | body: `message`(或 `action`), `branch_id`(可選), `story_id`(可選) |
| GET | `/api/status` | 取得角色狀態（含 world_day、cheat 狀態） | `branch_id` |
| POST | `/api/state/rebuild` | 由 canonical JSON 強制重建分支 `state.db` | body: `branch_id`（可選，預設 active branch） |

`/api/v2/send` 補充（實驗中，見 `doc/architecture.md` §9 / `doc/prompt_design.md` §9）：

- 非串流，回傳 `{"ok": true, "narration", "dice", "next_dice", "flagged", "dropped", "apply", "parse_note", "user_index", "gm_index", "story_id", "branch_id"}`。
- GM 在單次呼叫產出敘事 + `<!--DELTA {json} DELTA-->` 區塊；server 端 parse → deterministic gate → 以 production 持久層落盤（含 state.db sync）→ async critic 審高風險項目。
- 與舊 `/api/send` 的差異：不走 sync tag + 雙層 async extractor + lossy guard；改由 GM 自己宣告 delta（grounded）。
- `dice` 為本回合骰、`next_dice` 為預擲下一回合（pipeline 化）；`flagged` 是交給 async critic 的高風險項，`dropped` 是 gate 擋下的結構性錯誤。

## 分支管理

| Method | Path | 說明 | 主要參數 |
|---|---|---|---|
| GET | `/api/branches` | 列出可見分支（排除 deleted/merged/pruned） | - |
| POST | `/api/branches` | 從指定 index 建立分支 | body: `name`, `parent_branch_id`, `branch_point_index` |
| POST | `/api/branches/blank` | 建立空白分支（`branch_point_index=-1`） | body: `name` |
| POST | `/api/branches/switch` | 切換 active branch | body: `branch_id` |
| PATCH | `/api/branches/<branch_id>` | 分支改名 | body: `name` |
| DELETE | `/api/branches/<branch_id>` | 刪除分支（main 不可刪） | - |
| POST | `/api/branches/<branch_id>/protect` | 切換 protected（防 auto-prune） | - |
| GET | `/api/branches/<branch_id>/config` | 讀分支設定（含 `defaults`） | - |
| POST | `/api/branches/<branch_id>/config` | 更新分支設定（merge，回傳含 `defaults`） | 任意 JSON（常用：`team_mode`, `image_gen_enabled`, `image_model`） |
| POST | `/api/branches/edit` | 編輯歷史玩家訊息並生成新分支 | body: `parent_branch_id`, `branch_point_index`, `edited_message` |
| POST | `/api/branches/edit/stream` | `edit` 的 SSE 版本 | 同上 |
| POST | `/api/branches/regenerate` | 對既有玩家訊息重生成 GM 回覆（新分支；也可補發最後一則尚未有 GM 的玩家訊息） | body: `parent_branch_id`, `branch_point_index` |
| POST | `/api/branches/regenerate/stream` | `regenerate` 的 SSE 版本 | 同上 |
| POST | `/api/branches/promote` | Promote 某分支為主線路徑並剪枝 | body: `branch_id` |
| POST | `/api/branches/merge` | 合併子分支回父分支 | body: `branch_id` |

`/api/branches/<branch_id>/config` 回傳格式補充：
- `config`: 該分支已保存的設定（僅使用者/系統曾寫入的 key）
- `defaults`: 目前後端預設值（目前含 `image_gen_enabled=true`, `image_model`），前端可用來避免硬編碼 default 漂移

`/api/branches/edit*` / `/api/branches/regenerate*` 補充：
- `edit` 的 `branch_point_index + 1` 必須命中一則既有 `user` 訊息，否則回 `invalid_edit_target`
- `regenerate` 的 `branch_point_index` 必須命中一則既有 `user` 訊息；若下一則存在，必須是 `gm`。特例是它也接受「目前時間線最後一則、尚未有 GM 回覆」的 `user` 訊息作為補發目標，其他情況仍回 `invalid_regenerate_target`

## 故事（Story）管理

| Method | Path | 說明 | 主要參數 |
|---|---|---|---|
| GET | `/api/stories` | 列出故事與 active story（**自動過濾排除 `mode=="card"` 的角色卡**） | - |
| POST | `/api/stories` | 建立新故事（含 prompt/schema/default state） | body: `name`, `description`, `system_prompt`, `character_schema`, `default_character_state` |
| POST | `/api/stories/switch` | 切換 active story | body: `story_id` |
| PATCH | `/api/stories/<story_id>` | 更新故事名稱/描述 | body: `name`, `description` |
| DELETE | `/api/stories/<story_id>` | 刪除故事（不能刪最後一個） | - |
| GET | `/api/stories/<story_id>/schema` | 取得角色 schema | - |

## 角色卡模式（Card Mode）

角色卡模式為獨立的一對一單角色對話管線（詳見 `doc/card_mode_proposal.md`），入口與生成管線獨立，UI 共用 `chat_ui.js` / `style.css`，資料層複用 `story_io`。

| Method | Path | 說明 | 主要參數 |
|---|---|---|---|
| GET | `/card` | 卡片牆頁面（`card_index.html`） | - |
| GET | `/card/<slug>` | 單角色一對一對話頁面（`card_chat.html`） | - |
| GET | `/api/card/list` | 列出所有角色卡（只回傳 registry 中 `mode=="card"` 的項目，含 tagline/pitch） | - |
| GET | `/api/card/<slug>/state` | 取得卡片人設、關係、長期記憶、卡內時鐘、隨身物與訊息列表 | `after`（可選訊息 index 篩選） |
| POST | `/api/card/<slug>/send/stream` | 對話送出（**SSE 串流**：`text` / `done` / `error`） | body: `message` |
| POST | `/api/card/<slug>/regenerate` | 捨棄最後一則 GM 回覆並由 snapshot 還原後重新生成（支援 SSE） | body / header（支援串流） |
| POST | `/api/card/<slug>/rewind` | 回捲至指定 index 並以 snapshot 還原狀態與截斷後續訊息 | body: `to_index` |
| POST | `/api/card/<slug>/reset` | 重置該角色卡至開場白第 0 則初始狀態（清空記憶與還原時鐘） | - |

角色卡前端重生傳入 `{"stream": true}`，與送出共用 SSE 解析：文字事件是 `{"type":"text","chunk":"..."}`；`done.result.ok=false` 仍視為生成失敗。狀態的 `relationship_tiers` 為階梯來源，訊息 `index` 為回捲依據。

單卡既有 `recall_mode: auto/deep` 同時允許查詢本線 `card_lore.json`，無新增請求欄位；查詢結果增加 `lore_sources`，只含實際完整讀取的條目、其來源與讀取範圍。`sources` 仍只代表往事原文；off 不呼叫代理但保留本地 lore 選取。regen 的預先查詢不包含被替換回合，失敗保留原回合。多角色目前維持原本往事查詢。

## Lore

| Method | Path | 說明 | 主要參數 |
|---|---|---|---|
| GET | `/api/lore/search` | lore 搜尋（關鍵字或 tags） | `q`, `tags`, `limit` |
| POST | `/api/lore/rebuild` | 重建 lore 索引 | - |
| GET | `/api/lore/duplicates` | 查近似重複條目 | `story_id`, `threshold` |
| GET | `/api/lore/embedding-stats` | embedding 覆蓋統計 | `story_id` |
| GET | `/api/lore/all` | 取得 base + branch lore（附 layer） | `branch_id` |
| POST | `/api/lore/entry` | 新增 base lore | body: `category`, `subcategory`, `topic`, `content` |
| PUT | `/api/lore/entry` | 更新/rename base lore | body: `topic`, `subcategory`, `new_topic`, `content`, `category` |
| DELETE | `/api/lore/entry` | 刪除 base lore（以 `subcategory+topic`） | body: `topic`, `subcategory` |
| DELETE | `/api/lore/branch/entry` | 刪除 branch lore | body: `branch_id`, `topic`, `subcategory` |
| POST | `/api/lore/promote/review` | LLM 審核 branch lore 升級建議 | body: `branch_id` |
| POST | `/api/lore/promote` | 把 branch lore 提升到 base lore | body: `branch_id`, `topic`, `subcategory`, `content`(可覆寫) |
| POST | `/api/lore/chat/stream` | Lore 對話（SSE，含提案解析） | body: `messages` |
| POST | `/api/lore/apply` | 批次套用 lore 提案 | body: `proposals` |

## Debug Panel

| Method | Path | 說明 | 主要參數 |
|---|---|---|---|
| GET | `/api/debug/session` | 取得 Debug session（依 blank root 單位） | `branch_id` |
| POST | `/api/debug/chat/stream` | Debug 對話串流（SSE，server-owned history） | body: `branch_id`, `user_message` |
| POST | `/api/debug/apply` | 套用勾選修正（逐項執行） | body: `branch_id`, `actions[]`, `directives[]` |
| POST | `/api/debug/undo` | 回滾最近一次 Debug apply | body: `branch_id` |

注意：

- `apply` 會先寫完整 backup，再逐項執行 action，回傳每項成功/失敗。
- `actions` / `directives` 各自都有上限（預設 20，可由 env 覆蓋）；`directives` 只會採用最後一個非空 instruction。
- `directive` 會獨立寫入 `debug_directive.json`，不受 action 失敗影響。
- `apply` 成功的 action 也會累積寫入 branch 級 `pending_debug_overrides.json`。後續第一次成功的 `edit` / `regenerate` 建新分支時，會先把這些 overrides 套到新分支的 seed state/NPC/world_day/dungeon progress，再繼續生成 GM 回覆。
- 這份 pending overrides 是一次性的：第一次成功的 edit/regenerate child branch 落下首則 GM 回覆後，就會從 source branch 清掉；若之後還要沿用，需重新 `apply`。child branch 不會保留自己的 `pending_debug_overrides.json`。
- `audit_summary` 僅供 Debug Panel UI 顯示，不會另外寫入主聊天訊息。
- `undo` 只支援最近一次 apply（同 debug unit，且 branch 要匹配）；除了還原 state/NPC/world_day/dungeon snapshot，也會把 `pending_debug_overrides.json` 還原到 apply 前的狀態。若 backup 的 `world_day` 損壞，會直接回 `400`，不會 fallback 到目前值。

## NPC / Events / Images

| Method | Path | 說明 | 主要參數 |
|---|---|---|---|
| GET | `/api/npcs` | 列 NPC（預設 active-only） | `branch_id`, `include_archived` (`1/true/yes`) |
| POST | `/api/npcs` | 新增/更新 NPC（回傳 active+archived 全量，便於觀察自動封存） | body: NPC fields + `branch_id` |
| DELETE | `/api/npcs/<npc_id>` | 刪 NPC | `branch_id` |
| GET | `/api/events` | 列事件 | `branch_id`, `limit` |
| GET | `/api/events/search` | 搜事件 | `q`, `branch_id`, `limit` |
| PATCH | `/api/events/<event_id>` | 更新事件狀態 | body: `status` (`planted/triggered/resolved/abandoned`) |
| GET | `/api/images/status` | 查圖片生成狀態（live status，no-store） | `filename` |
| GET | `/api/stories/<story_id>/images/<filename>` | 取圖片檔 | path params |
| GET | `/api/npc-activities` | 取 NPC 活動紀錄 | `branch_id` |

`/api/images/status` 回傳補充：
- `warning`（optional）：圖片生成期間的提示訊息，目前可能出現
  - `{"code":"free_quota_exhausted","message":"Gemini 免費圖片額度已用盡，已自動切換到下一把 API key。"}`。

## 存檔（Saves）

| Method | Path | 說明 | 主要參數 |
|---|---|---|---|
| GET | `/api/saves` | 列出存檔（不含大型 snapshot） | - |
| POST | `/api/saves` | 建立存檔 snapshot | body: `name`(可選) |
| POST | `/api/saves/<save_id>/load` | 載入存檔（切 branch + 啟用 preview） | - |
| PUT | `/api/saves/<save_id>` | 存檔改名 | body: `name` |
| DELETE | `/api/saves/<save_id>` | 刪存檔 | - |

## 設定 / Cheats / 偏好

| Method | Path | 說明 | 主要參數 |
|---|---|---|---|
| GET | `/api/config` | 讀 LLM 設定（已脫敏，不回傳 key） | - |
| POST | `/api/config` | 更新 provider/model | body: `provider`, `gemini.model`, `vertex.model`, `openrouter.model`, `claude_cli.model`, `codex_agent.model` |
| GET | `/api/cheats/dice` | 讀骰子 cheat 狀態 | `branch_id` |
| POST | `/api/cheats/dice` | 設定 always_success | body: `branch_id`, `always_success` |
| GET | `/api/cheats/fate` | 讀 fate mode | `branch_id` |
| POST | `/api/cheats/fate` | 設定 fate mode | body: `branch_id`, `fate_mode` |
| GET | `/api/cheats/pistol` | 讀 pistol mode | `branch_id` |
| POST | `/api/cheats/pistol` | 設定 pistol mode | body: `branch_id`, `pistol_mode` |
| GET | `/api/nsfw-preferences` | 讀 NSFW 偏好 | - |
| POST | `/api/nsfw-preferences` | 寫 NSFW 偏好 | body: `chips`, `custom`, `custom_chips`, `hidden_chips`, `chip_counts` |

## 觀測與其他

| Method | Path | 說明 | 主要參數 |
|---|---|---|---|
| GET | `/api/usage` | token 與美元成本統計 | `story_id`, `days`, `all=true` |
| GET | `/api/auto-play/summaries` | 取 auto-play 摘要 | `branch_id` |
| POST | `/api/bug-report` | 回報特定訊息問題 | body: `branch_id`, `message_index`, `role`, `content_preview`, `description` |

## 副本系統（Dungeon）

| Method | Path | 說明 | 主要參數 |
|---|---|---|---|
| POST | `/api/dungeon/enter` | 進入副本 | body: `story_id`(可選), `branch_id`, `dungeon_id` |
| GET | `/api/dungeon/progress` | 取副本進度 | `story_id`(可選), `branch_id` |
| POST | `/api/dungeon/return` | 回歸主神空間（主線 >= 60%） | body: `story_id`(可選), `branch_id` |

注意：

- 目前建議在 dungeon API 明確傳 `branch_id`，避免走 fallback path。

`/api/config` 回傳補充：
- `provider` 目前可能值：`gemini` / `vertex` / `openrouter` / `claude_cli` / `codex_agent`
- 回傳結構固定包含：
  - `gemini.model`
  - `vertex.model`
  - `openrouter.model` / `openrouter.key_configured`（不回傳 key）
  - `claude_cli.model`
  - `codex_agent.model`
- `codex_agent` 是 read-only provider。前端看起來只是另一個模型供應商選項；後端仍走同一套 `/api/send*`、GM parser、snapshot 與 async extractor 流程。
- `/api/usage` 的各彙總維度包含 `prompt_tokens`、`output_tokens`、`total_tokens`、`cached_tokens`、`reasoning_tokens`、`cost_usd` 與 `calls`。

### Card Mode 回覆長度

`POST /api/card/<slug>/send/stream` 與 `POST /api/card/<slug>/regenerate`
可傳入 `response_length: "short" | "medium" | "long" | "unlimited"`，一般與串流重新生成皆適用。
分別以正文 200–500、500–1000、3000 字為目標（不含標頭）。
省略或 null 時沿用角色卡的 `output.length_hint`；非法值回傳 400，且不改動對話。
前端在輸入區的「繼續發展／持續指示」旁提供短／中／長快捷切換，與附加功能中的下拉選單同步；預設中，並以每張卡為單位記在此瀏覽器，不跨裝置同步。生成期間兩處都停用，下一次送出、繼續發展、重新生成與編輯沿用同一選擇。

角色卡的 `card.player.profile` 可為自由文字玩家檔案；由既有
`GET /api/card/<slug>/state` 的 `card.player` 回傳，前端角色抽屜顯示非空內容。
目前從設計檔編輯，沒有新增獨立的玩家資料編輯 API。

Card Mode 的 `GET /api/card/<slug>/state` 回傳 `clock.location`（目前場景）；前端優先顯示此欄位，卡片 `setting.place` 僅作缺省值。

### Card Mode 回退後的摘要失效處理

角色卡 rewind、regenerate 先計算保留的訊息數量：若摘要覆蓋的整段前綴仍保留（`compacted_through_index < retained_message_count`），保留摘要文字、版本、覆蓋範圍及時間；否則清空摘要，由保留的歷史重建。reset 一律清空。摘要覆蓋索引是時間線陣列位置，不以可能重用的訊息 index 判斷。無論保留或清空，都更換 revision，阻擋撤回前的背景工作提交。背景壓縮在每批開始與落盤時核對 revision；不符即丟棄。失效與背景提交使用短鎖，不等待模型生成。尚未摘要的原文持續注入，API 回應格式不變。

### Card Mode 多角色副本（選用）

`POST /api/card/<slug>/ensemble` 接收 `{target_slug, name?, character}`，以來源当前已提交回合建立獨立副本，回傳 `{ok, slug, url}`。不修改來源卡、對話或記憶；尚未完成的背景記錄不會自動同步到副本。`character` 必填 `name, personality`；選填 `identity, appearance, speech_style, memories`（文字陣列）、`tier, note, status, presence`。presence 為 `present/offstage/remote`，預設離場。副本與設計 ID 必須未存在。

副本另支援 `POST /api/card/<slug>/focus`（`character_id, presence?`）與 `/add`（同 character 格式，最多六人）。切焦點不會自動讓任何人入場／離場。`GET .../state` 多回傳 `ensemble, focus, cast, baseline_index, recorder`，其餘欄位對應焦點角色。

既有 `/send/stream`、`/regenerate`、`/rewind`、`/reset` 路徑在副本上由獨立管線處理。SSE 逐片段回傳 `{type:"text", chunk}`；完整敘事保存後回 `{type:"done", result:{ok,narration,gm_index,gm_source:"ensemble",recorder_status:"pending"}}`，不等待背景記錄。等待 provider 時每 10 秒送 SSE comment keepalive。生成／儲存失敗回 `{type:"error", message}`，部分文字不算已提交，前端重載保存的原回合。非法輸入在開串流前回 400，已忙碌回 409；開流後競爭失敗則 SSE error。生成期間排除其他寫操作，背景記錄不阻擋下一輪。

`GET .../state` 的 `recorder` 為 `{status:"done"|"pending"|"error", pending:<筆數>, failed:<筆數>}`。記錄失敗保留對話並阻止後續記錄越序，仍可繼續敘事。`POST .../retry-recording`（JSON `{}`）把失敗記錄重新排入背景，不更動已保存台詞。state 讀取／操作會喚醒服務重啟後的 pending 工作。

重生成還原該回合前的所有角色，再保留其後明示手動控制；生成失敗保留原回合。rewind 僅接受副本起點起的完整 GM index；reset 回到建立副本時。背景記錄以唯一 turn ID 驗證仍在目前時間線，並更新後續快照；重用 index 不會接收舊結果。原單角色路徑不變。

多角色回合現在會依實際敘事進出場更新 cast.presence；/focus 的 presence 仍可手動校正，僅切 focus 不會造成換場。請求格式不變。

多人敘事由一次統一生成完成，對外仍是一筆 GM 回覆；SSE text 會分片顯示。

### Card Mode 往事查詢模式

`POST /api/card/<slug>/send/stream`、`POST /api/card/<slug>/regenerate`（一般及 SSE）接受 `recall_mode: "off" | "auto" | "deep"`，單卡與多角色副本皆適用。省略／null 為 off；非法值在生成前回 400，regenerate 的非法值不撤回舊回合。前端附加功能的「回憶模式」依卡片存於此瀏覽器 localStorage，不改角色卡或跨裝置同步。

- off：原流程，不增加回憶模型呼叫。
- auto：模型結合詞語線索與當下語意決定搜尋／閱讀／結束，至多 4 次有效決策；普通對話仍有一次判斷成本。
- deep：允許較深入的主動檢索，至多 8 次有效決策；不是強制每輪回憶。

查詢只讀本輪保留的歷史，不修改記憶或存檔；多角色按角色分別授權，結果附角色標示交给統一敘事。找不到資料就提供「未取回原文」給敘事，不代造往事。空白或格式錯誤每位角色每回合最多額外重試一次；Vertex 暫時性服務／連線錯誤另共可重試兩次（間隔 1、2 秒）；仍失敗會走現有錯誤回應，不默默當成成功或切成 off；已產生的新用量逐次以 `card_recall` 記入故事 usage.db。

單卡重生成先完成唯讀回憶查詢，成功後才執行原有回溯流程；查詢失敗回 502 並保留原回合。這只保護新增的查詢階段，後續敘事／記錄沿用既有單卡流程。多角色敘事成功先原子保存，記錄另於背景提交；回憶／敘事失敗仍保留原回合。

### 角色卡劇情分支

單卡與多角色皆支援：

| Method | Path | 參數與行為 |
|---|---|---|
| GET | `/api/card/<slug>/branches` | 回傳 `branches` 與 `active_branch_id` |
| POST | `/api/card/<slug>/branches` 或 `/branches/fork` | `branch_id?` 來源、`message_index?` 已完成 GM 回應（省略取最後一則）、`name?`；建立並切至獨立分支 |
| POST | `/api/card/<slug>/branches/switch` | `branch_id`；只切換預設路線，不搬移狀態 |
| POST | `/api/card/<slug>/branches/rename` | `branch_id`, `name`（1–80 字） |
| POST | `/api/card/<slug>/branches/delete` | `branch_id`；可刪除非主線；子分支接回被刪分支的父線，完整存檔不變。生成或記憶整理中回 409。軟刪除、保留存檔；成功回 `branch_id` 與 `switch_to` 指向父線供本頁切回，僅目標為 active 時改動 active。 |

`state` 查詢可指定 `?branch_id=`；send/stream、regenerate、rewind、reset、focus、add、retry-recording、ensemble body 可傳 `branch_id`。省略時在請求開始捕獲 active branch（舊存檔為 main），背景工作一直使用捕獲值。state 額外回傳 `branch_id`（本次讀取）、`active_branch_id`（預設選擇）、`branches`。不存在或不合法分支回400，不能退回主線；敘事生成忙碌、送出或建立分支所需記錄尚待整理時回409。新分支需完整快照，多角色不能早於副本建立點。記錄失敗先重試整理（多角色）或重生成（單卡），也可選更早完整回合。

瀏覽器在網址保留 branch_id，每個頁籤固定操作自己正在看的路線；另頁切換不會偷換本頁目標。既有回退／重新生成仍修改目前分支，想保留兩種版本先點回覆旁的 ⑂ 建立分支。reset 僅重置指定分支，保留分支樹及其他路線。角色卡分支不呼叫世界模式的 merge/promote/auto-prune。

單卡 regenerate／rewind／reset 不等待背景記憶整理。被移除回合的記錄結果失效，原本 regen 仍在同一分支替換回覆，不建立分支。串流重生準備後若遭另一請求回退／重置，worker 會檢查 generation token 並回 SSE error，不把舊回覆寫入新時間線。一般 send 及建立分支仍須等待所需記憶整理完成。單卡分支以所選 GM 的完整 state／NPC／clock 快照為檢查點，更早的 recorder error 不阻擋，也不自動補記或清除失敗標記；所選回合本身的 error 仍須經人工審核補記才可分支。更早的單卡 pending 只有在 PID 確認已不存在、且所選檢查點快照完整時可跨過；保留原中斷標記，所選回合本身仍須完成。仍在執行或 PID 無法確認的 pending 繼續拒絕，分叉點之後的工作不影響早期分支。編輯與候選切換同樣以所選版本完整的回合前快照允許更早的 error 或已確認程序不存在的單卡 pending，多角色規則不變。

### 角色卡編輯重演

`POST /api/card/<slug>/edit`，單卡與多角色皆支援：

- `branch_id`：來源分支；`message_index`：要替換的 GM 回合，開場不可編輯。
- `expected_reply`：畫面上原回覆的完整文字；若已改變則拒絕，避免覆蓋別次修改。
- `message`：編輯後的玩家輸入，1–20000 字。
- `mode`：`regenerate` 完全重生，或 `revise` 以原回覆為待修改稿。
- `instruction`：選填修改要求，最多 20000 字；僅供敘事，不寫入角色記憶或玩家對話。
- `destination`：`fork` 新分支保留來源；`overwrite` 替換當輪並刪除其後對話。
- `response_length`、`recall_mode`：沿用 send 規則；`stream:true` 或 SSE Accept 啟用串流。

成功回傳 `ok, narration, gm_index, branch_id`；SSE 用 `text/error/done`，目的分支在 `done.result.branch_id`。前端成功後切換該分支並重讀狀態。失敗保留原對話，失敗新分支會移除；串流草稿可能已顯示，但不作為成功保存。缺少回合前快照、舊回覆不符、未整理的保留前綴會拒絕操作。歷史編輯撤回後續狀態，從原回合 pre snapshot 重建；背景記錄只讀新版玩家輸入和新版正文。

單卡生成的回憶查詢與敘事各有 180 秒等待上限，逾時或同服務同分支新生成到來時取消舊等待並釋放分支鎖。供應商工作可能尚未結束，但遲到文字與結果不再進入對話／記憶；外部工作不直接寫遊戲狀態。正常送出逾時不新增回合，編輯失敗沿用原有還原流程。regen 失敗且仍持有同一回合時還原原版；已被回退／重置／新生成取消時不復活舊回合。跨服務程序仍以檔案鎖拒絕重疊生成，無跨程序即時取消。Vertex SDK 明確設定 90 秒 HTTP timeout、單次嘗試；此設定也涵蓋 Recorder。

### 角色卡記憶編輯

`GET /api/card/<slug>/memories?branch_id=...`：回傳 `revision` 與 `characters[{id,name,memories:[{index,text}]}]`。陣列保持保存順序，UI 倒序呈現。

`POST /api/card/<slug>/memories`：`{branch_id, revision, changes:[{character_id,index,text}]}`；text 為 null 表示刪除，編輯不可空白、最多 5000 字。不接受新增或任意檔案路徑。revision 涵蓋目前記憶及歷史，衝突或背景 pending 回 409 且不寫入。單卡、多角色皆支援。成功回更新後資料與 `backup_id`。

同角色完全相同文字的記憶及快照項目一起修正；不模糊比對，不插入早期不存在的記憶。對話正文、摘要、其他分支不變。備份及操作紀錄位於 `data/stories/<slug>/memory_edit_backups/<branch>/<backup_id>/`，無公開還原 API。

記憶保存與手動正文編輯共用 `branches.recording_blocks_write`：單卡 pending 只有在 `card_record_pid` 確認已不存在時不再阻擋；PID 缺少／不合法、仍存在或無法確認時繼續回409。多角色 pending 是可恢復工作，仍阻擋，不依單卡 PID 判斷。此檢查不修改原 pending 標記、不視為 done、不補記或呼叫模型；revision／分支鎖及保存備份仍適用。歷史分支／重演仍要求所選檢查點完整快照；更早已確認程序不存在的單卡 pending 可跨過，原標記保留。

### Card Mode 回覆候選、收藏與重生次數

單卡與多角色共用 `/api/card/<slug>/candidates`，所有請求可指定 `branch_id`。

| Method | 參數 | 說明 |
|---|---|---|
| GET | `message_index=<GM index>` | 回傳 `group_id, message_index, user_message, latest, selected_id, versions, regen_count, failed_count, attempts`；純翻閱不改進度 |
| GET | `favorites=1` | 回傳本路線所有收藏，包含未採用或已回退的版本 |
| POST | `action:"favorite", group_id, version_id, favorite:bool` | 收藏／取消收藏；從未重生的現存回覆可在此首次存入候選庫 |
| POST | `action:"select", message_index, group_id, version_id, expected_version_id` | 採用候選；最後回合在本線替換，較早回合建立新分支；回傳 `ok, branch_id, recorder_status` |
| POST | `action:"edit-text", message_index, expected_version_id, expected_reply, content` | 手動編輯最新 GM 正文，立即採用為新版本，舊版保留；回傳 `ok, branch_id, reply_version_id` |

`edit-text` 不呼叫模型、不重跑 Recorder，不修改既有記憶、關係、物品、時鐘與快照。僅允許當線最後一則訊息為 GM 且前一則是玩家的回合（開場、副本建立前及歷史回覆拒絕）；原版本 ID 與原正文不符回409，保留草稿。正在生成／任何仍為 pending 的 Recorder 工作回409；歷史 error 不阻擋，也不補記。正文不可空白、最多200000字，未變更回 `unchanged:true` 且不新增版本。磁碟保存失敗回500並還原正文與候選庫。手動編輯不增加 regen attempt／失敗次數，新版不繼承舊版收藏、讚及註記。

手動版本公開 `text_only_edit:true, edited_at`，原模型及 `generated_at` 保留為基底來源；內部記錄來源版本 ID。之後切回手動版沿用其保存的狀態，不重錄記憶，即使基底原有 Recorder error。多角色只搬移原知情片段的文字位置，跨越知情邊界的新文字不擴張知情範圍，不重新判讀事件。單卡以 revision 拒絕替換前的摘要 worker，僅清除已涵蓋被編輯回覆的摘要；多角色同樣移除涵蓋被改回覆的 source recap，沒有摘要模型呼叫。

`versions` 包含 `id, content, selected, favorite, feedback, generated_at, saved_at, provider, model` 與風格／劇情／場景／回憶／長度選項，不公開狀態快照。舊回覆缺少的模型與生成時間回 null，不以保存時間冒充生成時間。`state.messages[].candidate_summary` 提供 `versions, selected_number, selected_id, regen_count, failed_count`，沒有候選庫的回合為 null。

每次通過輸入檢查的 regen 記一次 attempt（含回憶查詢失敗）；保存原版後才撤回。成功正文才增加版本，失敗／取消另計，程序中斷尚未結束者保留 pending attempt。資料從功能啟用後開始累積，無法回補過去已丟棄的重生次數或正文。不完整的單卡／多角色串流不保存成成功候選。

套用時核對目前回合與版本 ID，過期選擇回400；缺完整快照回400，保留前綴尚未整理完成或分支正忙回409。已完成版本直接還原該版的記憶、關係、隨身物與時鐘；未完成／失敗的 Recorder 版本改用新的工作 ID，只重跑背景整理，不重新生成正文。較早回合建立分支並保留原線後續；分支只繼承其保留回合的候選。回退／重置保留本線收藏，已移除回合只可閱讀／收藏，無法直接套用到另一個同編號回合。記憶編輯也同步本線候選庫的相同文字快照，防止切版復活已校正記憶。

「繼續發展」只是送出 `（請繼續發展）` 的快捷鍵，走既有 `send/stream`，沿用目前分支、模型及敘事選項，保留輸入框尚未送出的草稿。

### Card Mode 演出按讚與註記

單卡與多角色共用 `/api/card/<slug>/feedback`，所有請求可指定 `branch_id`。`state.messages[].feedback`（GM 含開場；玩家為 null）及候選 `versions[].feedback` 回傳 `{version_id, liked, comment, comment_revision}`；未標記版本預設 false、空字串、null。讀取 state 不寫入候選庫。

| Method | 參數 | 說明 |
|---|---|---|
| POST | `message_index, version_id, liked:bool` | 標記目前顯示的特定版本；不改註記 |
| POST | `message_index, version_id, comment:string, expected_comment_revision` | 保存／清空註記，最多 4000 字；不改按讚。首次 revision 必須明示 null，衝突回 409 |
| POST | 額外 `group_id` | 指定候選庫版本，可標記未採用／已回退的舊版；省略時必須仍是時間線同一版，否則回 400 |
| GET | 無 | 回傳 `items`：本線仍有讚或註記的版本（含未採用／已回退），各含公開版本欄位、`group_id, message_index, user_message, context, feedback_updated_at` |

資料保存在分支的候選庫，與 `favorite` 獨立。第一次讚或非空註記保存當時可讀的最多前 10 則／後 4 則對話、來源分支和擷取時間；此 context 不是歷史完整 LLM request，舊版未記錄的模型／風格不補猜。保存已有版本正文、原始玩家輸入、選項與快照，不修改正式訊息、記憶、摘要、關係或時鐘，也不發出模型請求。

取消讚保留註記，重生的新版本沒有舊版的標記。一般／編輯／候選分支只繼承保留回合的候選與標記，後續修改彼此獨立。前端單一 👍 按鈕立即切換；有讚或註記才顯示「加註」或「查看／編輯註記」文字入口。保存失敗保留草稿，離開未保存的註記需選擇保留或放棄。

### Card Mode 本線持續指示

單卡與多角色提供 `/api/card/<slug>/instruction`；`branch_id` 的解析和其他 Card API 相同。

- GET 回傳 `{ok:true, persistent_instruction:{enabled:bool, text:string, revision:string|null}}`；缺檔為 false／空字串／null，不建立設定檔。
- POST 接收 `{branch_id, enabled:bool, text:string, expected_revision:string|null}`，text 最多 4000 字，去頭尾空白，啟用時不可空白。缺少欄位／型別錯誤回 400；revision 過期或同線生成正忙回 409；保存失敗回 JSON 500，草稿仍保留。成功回更新後設定，相同內容不輪換 revision。
- `state.persistent_instruction` 同樣回傳上述設定。前端在輸入框上方顯示入口與啟用狀態；修改後按保存才生效，取消不改設定，衝突可保留草稿或確認放棄後重新載入。

存於本線 `card_instruction.json`（單卡在 branches/<id>；多角色主線在故事根目錄，子線在 branches/<id>）。屬寫作設定，rewind／reset 保留；一般分支、編輯新分支及歷史候選新分支繼承建立當下的來源設定，之後互不影響，不按歷史快照回捲。

send、regenerate、edit 從伺服器讀取目前分支設定，不接受生成 body 暗中覆寫。啟用時追加至敘事 system prompt，排在風格選項後、本輪編輯要求前；本輪明確要求優先。GM 的 `persistent_instruction` metadata 保存實際生效文字，關閉為空字串，舊回覆缺欄位為未知；候選 `versions[].persistent_instruction` 回傳該版文字／null，視窗可查看。設定不寫入正文、角色狀態、記憶或摘要，也不追加至 Recorder／回憶查詢，沒有額外模型呼叫。

### Card Mode 探索素材

`GET /api/card/<slug>/state` 在單卡與多角色均回傳 `exploration_materials: [{id, title, seed}]`。卡片 `exploration_material_bank: "xianxia"` 啟用仙俠素材；缺省／null 回傳空陣列，非法 bank 在卡片驗證時拒絕。素材檔為 `story_design/_shared/xianxia_hooks_v1.json`；讀取不寫入 lore、記憶或正式事件。

前端「探索 ✦」組合一條素材與輸入框中可選的作者方向，透過既有 `POST .../send/stream` 的 `message` 送出，其他生成參數維持目前選擇。沒有新增送出端點或隱藏生成參數；重生使用已保存的同份 `message`。普通送出／繼續發展不自動帶素材。世界模式 `static/app.js` 與 `/api/send*` 不使用此欄位。

### Card Mode 敘事風格參數（每輪選項）

`POST /api/card/<slug>/send/stream`、多角色 `/send`、`/regenerate` 與 `/edit` 支援 `narrative_style: "original" | "natural"`。缺少或 null 使用 original；非法字串或型別回傳 400，不進入生成或覆寫。自然互動只增補本輪敘事 prompt；重新生成及編輯使用本次請求的選擇，不自動繼承舊回合。介面在附加功能中提供選單，依卡片保存本機偏好。


### 可組合的劇情與場景選項

Card Mode 單卡與多角色的 send、regenerate、edit 同時支援三個獨立選項：文風 narrative_style（original / natural）、劇情主動性 plot_mode（follow / explore / coauthor）、場景模式 scene_mode（default / intimate）。缺少或 null 採 original、follow、default；非法值在生成與存檔變更前回 400。前端依卡片保存本機偏好，本次選擇套用於下一次生成，GM 訊息記錄三個選項，不回寫舊回合。

探索允許合理的新事件、線索與場景配角，覆蓋原有「不得另設事件」限制，但不替玩家決定、不捏造共同往事、不自動新增固定角色。親密互動聚焦眼前人物、情緒與感官細節；與探索並用時優先發展當前互動、減少外部打斷，尊重離場與快轉。這是獨立的 Card Mode 提示選項，不沿用舊世界模式 pistol 的停用記錄或強制維持場景機制。

提示依序追加自然互動、探索、親密互動，編輯指示仍在其後；預設組合不追加任何提示。Recorder、既有狀態規則與回覆長度保持原有流程，沒有額外模型呼叫。tests/test_card_styles.py 覆蓋十二種組合及單卡／多角色 API 送出、重生、編輯與非法值無寫入；瀏覽器測試涵蓋偏好保存與請求欄位。


「探索・共同創作」使用 `plot_mode: "coauthor"`，單卡與多角色的送出、重生、編輯皆適用，並記錄在 GM metadata。原探索使用 explore，兩者互斥而不疊加；可各自搭配自然互動及親密互動。介面沿用每卡片偏好保存，不改使用者原有選擇、不額外增加模型呼叫。

單角色 `POST /api/card/<slug>/edit`：選定回合有完整且格式正確的回合前快照時，允許其歷史包含 recorder error 或 PID 確認已不存在的單卡 pending，適用 fork / overwrite；保留原失敗標記，不自動補記。仍在執行或 PID 無法確認的 pending、缺少快照與多角色未完成整理仍依原規則拒絕。

### 角色卡文字欄位的陣列格式

`persona.identity`、`persona.appearance`、`persona.personality` 接受字串或字串陣列，舊卡不需遷移。陣列項目須為非空字串，personality 整體不得為空。儲存及 state API 保留原始型態；prompt 逐項列出，NPC role 轉為文字，前端身份／外觀顯示換行文字，避免陣列括號與自動逗號。此變更僅調整編輯格式。

角色卡 state 回應的 `card.relationship_enabled` 明確回傳單卡關係階梯開關（預設 true）；false 時前端隱藏階梯與舊關係說明，後端省略關係 grounding 及更新工具。

舊版 prompt 選項：`plot_mode: legacy_20260908` 替換角色扮演契約為 Git `abd788b` 的 2026-09-08 初版，不疊加探索／共同創作規則。角色卡、記憶、摘要及其他風格選項仍按目前流程，故不等同重現歷史完整 API 請求。搭配 original 文風與 default 場景可避免額外風格增補。預設及既有選項不變。


### 探索素材規模與輸入草稿

Card state 的 `exploration_materials` 每項包含 `id`、`title`、`seed`、`size`（`small` 或 `large`；舊資料省略時預設 small）。單卡與 ensemble 共用，不寫入故事事實。卡片未選素材庫時為空列表。

`card.js` 的探索大小選擇與探索／繼續按鈕均只操作本地輸入草稿，不呼叫 send/stream。只有玩家明確按送出才沿用既有 POST，大小要求已包含在可編輯 message 中，不新增 request 欄位。世界模式 `app.js` 不使用這個 Card endpoint，毋須改動。

Card Mode 回覆長度新增 `unlimited`（不限長度；快捷列顯示 ∞），不加入字數目標，也不回退至角色卡 length_hint。與其他選項相同，保留每卡偏好並適用送出、重生及編輯；模型本身的輸出上限仍有效。

### Galgame 單卡 state

`GET /api/card/<slug>/state` 新增 `galgame`：未啟用時為 null；啟用時包含 affinity、trust、resentment、desire（0–100）、stage、last_event（action/mode/evidence/consent_quote/reason/outcome/delta/penalty/resentment_delta）、回合／日額資訊及 boundaries 陣列（action/label/affinity/trust/eligible）。初始 last_event 只有 reason/delta。讀取不結算、不寫檔。

沿用 send/stream、regenerate、edit、rewind、reset、candidates 與 branches 端點，不接受前端自行指定分數。galgame 的互動分類發生在 narration 前；失敗回 ok=false，不新增當回合分數。前端 `static/card.js` 顯示四項數值、變動原因與門檻；世界模式 app.js 不使用此端點。啟用卡目前建立 ensemble 副本會回驗證錯誤。
