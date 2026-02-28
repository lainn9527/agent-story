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
| GET | `/api/messages` | 取得分支完整 timeline（含 fork/sibling/world_day） | `branch_id`, `offset`, `limit`, `after_index`, `tail` |
| POST | `/api/send` | 送出玩家訊息，取得完整 GM 回覆 | body: `message`, `branch_id` |
| POST | `/api/send/stream` | `send` 的 SSE 版本 | body: `message`, `branch_id` |
| GET | `/api/status` | 取得角色狀態（含 world_day、cheat 狀態） | `branch_id` |
| POST | `/api/state/rebuild` | 由 canonical JSON 強制重建分支 `state.db` | body: `branch_id`（可選，預設 active branch） |

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
| GET | `/api/branches/<branch_id>/config` | 讀分支設定 | - |
| POST | `/api/branches/<branch_id>/config` | 更新分支設定（merge） | 任意 JSON |
| POST | `/api/branches/edit` | 編輯歷史玩家訊息並生成新分支 | body: `parent_branch_id`, `branch_point_index`, `edited_message` |
| POST | `/api/branches/edit/stream` | `edit` 的 SSE 版本 | 同上 |
| POST | `/api/branches/regenerate` | 對既有玩家訊息重生成 GM 回覆（新分支） | body: `parent_branch_id`, `branch_point_index` |
| POST | `/api/branches/regenerate/stream` | `regenerate` 的 SSE 版本 | 同上 |
| POST | `/api/branches/promote` | Promote 某分支為主線路徑並剪枝 | body: `branch_id` |
| POST | `/api/branches/merge` | 合併子分支回父分支 | body: `branch_id` |

## 故事（Story）管理

| Method | Path | 說明 | 主要參數 |
|---|---|---|---|
| GET | `/api/stories` | 列出故事與 active story | - |
| POST | `/api/stories` | 建立新故事（含 prompt/schema/default state） | body: `name`, `description`, `system_prompt`, `character_schema`, `default_character_state` |
| POST | `/api/stories/switch` | 切換 active story | body: `story_id` |
| PATCH | `/api/stories/<story_id>` | 更新故事名稱/描述 | body: `name`, `description` |
| DELETE | `/api/stories/<story_id>` | 刪除故事（不能刪最後一個） | - |
| GET | `/api/stories/<story_id>/schema` | 取得角色 schema | - |

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

## NPC / Events / Images

| Method | Path | 說明 | 主要參數 |
|---|---|---|---|
| GET | `/api/npcs` | 列 NPC（預設 active-only） | `branch_id`, `include_archived` (`1/true/yes`) |
| POST | `/api/npcs` | 新增/更新 NPC（回傳 active+archived 全量，便於觀察自動封存） | body: NPC fields + `branch_id` |
| DELETE | `/api/npcs/<npc_id>` | 刪 NPC | `branch_id` |
| GET | `/api/events` | 列事件 | `branch_id`, `limit` |
| GET | `/api/events/search` | 搜事件 | `q`, `branch_id`, `limit` |
| PATCH | `/api/events/<event_id>` | 更新事件狀態 | body: `status` (`planted/triggered/resolved/abandoned`) |
| GET | `/api/images/status` | 查圖片生成狀態 | `filename` |
| GET | `/api/stories/<story_id>/images/<filename>` | 取圖片檔 | path params |
| GET | `/api/npc-activities` | 取 NPC 活動紀錄 | `branch_id` |

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
| POST | `/api/config` | 更新 provider/model | body: `provider`, `gemini.model`, `claude_cli.model` |
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
| GET | `/api/usage` | token 使用統計 | `story_id`, `days`, `all=true` |
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
