# 開發與維運指南

## 1) 開發環境

### 必要條件

- Python 3.11+（WSL2 文件示例使用 3.12）
- `pip`
- 若使用 Claude provider：`@anthropic-ai/claude-code` CLI
- 若使用 Codex provider：`codex` CLI

### 安裝

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### LLM 設定

建立或修改 `llm_config.json`（含金鑰，不應提交）：

- `provider`: `gemini`、`vertex`、`openrouter`、`claude_cli` 或 `codex_agent`
- `gemini.api_keys`: 可放多把 key（系統會做 fallback/cooldown）
- `vertex.recorder_model`：Card Mode 單卡／多角色背景記錄模型，例如 `gemini-3.1-flash-lite`；未設定沿用敘事模型，不切換 provider。OpenRouter 同樣支援 `openrouter.recorder_model`。
- `gemini.model` / `vertex.model` / `openrouter.model` / `claude_cli.model` / `codex_agent.model`
- `openrouter.api_key`: OpenRouter key；舊設定鍵 `open_router` 也相容。`reasoning` 預設為 `false`，避免 reasoning-only 消耗。

`codex_agent` 備註：
- 目前是 read-only provider，只會在臨時工作區內讀取 branch/lore/context 檔案。
- 它不會直接寫 runtime JSON；最終仍由既有 Flask route + `gm_pipeline` 落盤。
- 串流模式走 `codex exec --json`；bridge 會在回覆生成期間維持 SSE 活性，並在收到 agent message 時盡快往前端轉發正文。

## 2) 本機啟動流程

```bash
python app.py
```

預設：

- Host: `0.0.0.0`
- Port: `5051`（可用 `PORT` 覆蓋）

首次進站前端會觸發 `POST /api/init`，執行必要遷移與索引初始化。

啟動補建 `dungeons_template.json` 時會跳過角色卡（registry 的 `mode=card`、設計目錄的 `card.json` 或存檔的 `ensemble.json` 任一成立），僅為 RPG 故事建立預設關卡。既有自訂模板不會被啟動流程刪除或覆寫。

## 3) 測試

### 執行

```bash
pytest
```

常用：

```bash
pytest -m "not slow"
pytest tests/test_api_routes.py
pytest tests/test_extract_tags_async.py -q
pytest tests/test_debug_api.py
```

### 目前測試範圍（重點）

- Branch tree / fork / sibling 邏輯
- state update 與 map/list schema 行為
- state cleanup 套用邏輯（`tests/test_state_cleanup.py`）
- lore/event DB 搜尋與索引
- Flask API 合約（`tests/test_api_routes.py`）
- async tag extraction 關鍵路徑

詳見 `doc/testing_plan.md`。

## 4) 常用腳本

| 檔案 | 用途 |
|---|---|
| `scripts/deploy/deploy.sh` | Mac 生產機更新與重啟（port 5051） |
| `scripts/deploy/deploy_wsl2.sh` | WSL2 環境更新與 systemd 重啟 |
| `scripts/backfill/backfill_snapshots.py` | 回填歷史 GM 訊息 snapshot |
| `scripts/backfill/backfill_branch_titles.py` | 回填分支標題 |
| `scripts/backfill/backfill_npc_lifecycle.py` | 回填 NPC lifecycle + R1 去重 + 重建 `state.db` |
| `scripts/backfill/backfill_story_memory.py` | 回填 `story_anchors` 與 sticky events（建議先 `--dry-run`） |
| `scripts/backfill/backfill_dungeon_return_memory.py` | 回填 `dungeon_return_memory.json` 與 NPC provenance（建議先 `--dry-run`） |
| `scripts/migrations/migrate_current_dungeon.py` | 回填 `current_dungeon` |
| `scripts/state/cleanup_character_state.py` | 清理/修復 state 汙染 |
| `scripts/state/clean_state.py` | 移除 legacy 垃圾欄位 |
| `scripts/lore/lore_cleanup.py` | 一次性 lore 清理 |
| `scripts/lore/lore_merge.py` | lore 語意合併 |

多數腳本支援 `--dry-run`；先 dry-run 再 apply。
腳本分類可參考 `scripts/README.md`。

`backfill_story_memory.py` 範例：

```bash
python3 scripts/backfill/backfill_story_memory.py \
  --story-id story_original \
  --branch-id branch_69569153 \
  --dry-run
```

`backfill_dungeon_return_memory.py` 範例：

```bash
python3 scripts/backfill/backfill_dungeon_return_memory.py \
  --story-id story_original \
  --branch-id branch_69569153 \
  --dry-run
```

## 5) 日常開發建議流程

1. 先啟動 server + 打開 UI 驗證核心 flow（init/messages/send）。
2. 修改後至少跑：
   - 你改到的模組對應測試
   - `tests/test_api_routes.py`（若改 API）
3. 若改了狀態/抽取格式，順手驗證：
   - `tests/test_state_update.py`
   - `tests/test_extract_tags_async.py`
4. 若改 Debug Panel（debug chat/apply/undo/directive），至少跑：
   - `tests/test_debug_api.py`
   - `tests/test_context_injection.py`
   - `tests/test_compaction.py`
5. 若改 lore/event 搜尋，跑：
   - `tests/test_lore_db.py`
   - `tests/test_event_db.py`

## 6) Git 與分支流程（建議）

### 6.1 建議用 worktree 開功能分支

```bash
git worktree add ../story-<branch-name> -b <branch-name> main
```

優點：

- 與 `main` 工作目錄隔離
- 可同時開多個任務分支
- 減少誤改 production/runtime 檔案風險

### 6.2 Provider 建議

如需大量本機測試，建議用 `claude_cli` 減少消耗共享 Gemini quota。

### 6.3 最小 PR 自檢

至少附上：

- 你改動的對應測試結果
- 若有 API 變更，`tests/test_api_routes.py`
- 若有 prompt/state/tag 變更，`tests/test_extract_tags_async.py` + `tests/test_state_update.py`

## 7) E2E 測試（建議）

可用隨機 port 開測試服，避免撞到既有服務：

```bash
PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
echo "http://localhost:$PORT"
PORT=$PORT python app.py
```

## 8) Release / Deploy 基線流程

### 8.1 發版前

1. 更新 `VERSION`
2. 更新 `CHANGELOG.md`（新增版本段落與重點）
3. 跑完整或足夠覆蓋的測試

### 8.2 合併後（可選）

```bash
git tag vX.Y.Z
```

### 8.3 部署

- Mac production 更新腳本：`./scripts/deploy/deploy.sh`
- WSL2 更新腳本：`./scripts/deploy/deploy_wsl2.sh`

`scripts/deploy/deploy.sh` 會在 `/Users/eddylai/story-prod` 拉最新並重啟 `5051`。
部署會重啟服務，建議先確認可接受短暫中斷。

## 9) Usage / Trace 觀測

### 9.1 Token usage

- API：`GET /api/usage?story_id=<id>&days=7`
- 跨故事總覽：`GET /api/usage?all=true`
- 存放位置：`data/stories/<story_id>/usage.db`
- OpenRouter 額外記錄 API 回傳的 `cached_tokens`、`reasoning_tokens` 與 `cost_usd`。

### 9.2 LLM trace

- 開關：`LLM_TRACE_ENABLED`（預設開）
- 保留天數：`LLM_TRACE_RETENTION_DAYS`（預設 14）
- 由 `app.py::_trace_llm()` 寫入（best-effort，不應阻斷主流程）
- 檔案位置：`data/llm_traces/<story_id>/<YYYY-MM-DD>/<branch_id>/<msg_tag>/`
- 檔名格式：`<HHMMSS.mmm>_<stage>_<id>.json`

常見 `stage`：

- `gm_request` / `gm_response_raw`
- `extract_tags_request` / `extract_tags_response_raw`
- `state_cleanup_request` / `state_cleanup_response_raw`
- `state_normalize_request` / `state_normalize_response_raw`
- `lore_promote_review_request` / `lore_promote_review_response_raw`
- `lore_chat_request` / `lore_chat_response_raw`
- `lore_organizer_request` / `lore_organizer_response_raw`

快速排查指令（以 `story_original` 為例）：

```bash
# 看最近 20 個 trace 檔案
find data/llm_traces/story_original -type f | sort | tail -n 20

# 找特定 stage
rg -n '"stage": "gm_request"' data/llm_traces/story_original

# 看某個 msg_tag 下的 request/response
ls -lah data/llm_traces/story_original/<YYYY-MM-DD>/<branch_id>/msg_000407/
```

注意：

- trace 可能含完整 prompt、recent messages、原始模型輸出，分享前請先去識別化。
- retention 清理是「寫入時觸發、按日期資料夾刪除」；調整天數後需有新 trace 寫入才會生效。

## 10) 常見問題排查

### 問題：`/api/send` 或串流無回應

- 檢查 `server.log`
- 檢查 `llm_config.json` provider/model 是否可用
- Claude 模式確認 `CLAUDE_BIN` 路徑

### 問題：Lore 搜尋結果怪或很少

- 呼叫 `POST /api/lore/rebuild`
- 檢查 `story_design/<story_id>/world_lore.json` 是否有內容

### 問題：分支狀態看起來錯位

- 確認目標分支 `messages.json` 內是否有對應 snapshot
- 必要時使用 `scripts/backfill/backfill_snapshots.py --dry-run`

### 問題：NPC/事件/道具欄髒亂（過多已結束副本角色、重複名稱、過時事件）

- 設定面板「狀態清理」按鈕可手動觸發 LLM 審核與清理（歸檔 NPC、合併重複、解決事件、移除道具）
- 或呼叫 `POST /api/state/cleanup`（body 可帶 `branch_id`）；清理在背景執行，完成後會寫入 npcs/state/events

### 問題：切換機器後資料不一致

- `data/` 與 `story_design/` 需同步（參考 `doc/sync.md`）
- 避免兩台同時寫同一份 SQLite

### 問題：副本 API 行為異常

- 呼叫 dungeon 端點時請明確帶 `branch_id`
- 檢查 `branches/<branch_id>/dungeon_progress.json` 與角色 `current_phase/current_dungeon`

## 11) 版本與變更紀錄

- 版本號來源：`VERSION`
- 變更紀錄：`CHANGELOG.md`

## 12) 共用聊天 UI 驗證

`tests/ui/chat_ui.test.cjs` 使用 Playwright 與替身 API 驗證角色卡送出／重生／回捲／重置、失敗提示、背景狀態刷新、手機版及世界模擬器初始化。所有請求攔截在測試瀏覽器，不寫 runtime 或呼叫付費模型。

```bash
# 使用已安裝的 playwright；必要時 NODE_PATH 指向它所在的 node_modules
CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" node tests/ui/chat_ui.test.cjs
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_mode.py tests/test_api_routes.py -q
```

Linux／CI 可省略 `CHROME_BIN`，使用 Playwright 已安裝的 Chromium。截圖預設存 `/tmp/story-ui-screenshots`，可用 `UI_SCREENSHOTS` 改位置。瀏覽器替身測試不代表真實模型品質、30 回合壓縮或後端併發驗證。

### Card Mode 記憶回歸與摘要候選重建

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_memory.py tests/test_card_mode.py tests/test_compaction.py -q`
涵蓋舊記憶檢索、約定先後順序、回捲快照、摘要延遲窗口、長篇末尾保留、版本遷移與批次失敗／revision 原子提交。

`python3 scripts/backfill/rebuild_card_recap.py naomi --output /tmp/naomi-recap.json`
會呼叫設定的模型，由完整現存時間線產生候選，不改遊戲。套用前審核摘要、比對 `source_digest` 與原摘要是否仍一致，先備份再在停止服務期間替換 recap。不要覆寫訊息或 NPC 快照。實際模型摘要仍需人工核對關鍵約定，單元測試不保證生成品質。

### 多角色副本驗證

離線回歸：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_ensemble.py tests/test_card_ensemble_async.py tests/test_card_mode.py tests/test_card_memory.py tests/test_compaction.py tests/test_state_update.py tests/test_extract_tags_async.py -q`。涵蓋原卡不變、個人記錄歸屬、統一敘事一次呼叫、焦點／記憶／關係／時鐘回溯，以及記錄失敗保留對話。`test_card_ensemble_async.py` 以事件閘門驗證首片段早於敘事完成、done 早於背景整理、pending 原文續聊、後續快照重建、手動控制保留、regen／rewind／reset 的遲到結果丟棄、重啟恢復、按序重試與串流中斷原子性。

明確 opt-in 的付費模型測試：`CARD_ENSEMBLE_LIVE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_ensemble.py::test_live_focus_handoff -q -s`。全程為 temporary directory 的成年校園攝影展資料；四輪測試小蘭私訊、沈晚接續、雙方共同得知消息、回到小蘭。每輪輸出 `/tmp/card-ensemble-live-report.json` 供人工檢查，不載入使用者劇情。

瀏覽器測試：設定 `NODE_PATH` 指向已安裝 Playwright 的 node_modules、`CHROME_BIN` 指向 Chrome，執行 `node tests/ui/card_ensemble.test.cjs`，以及 `node tests/ui/chat_ui.test.cjs`。新功能只透過「建立多角色副本」選用，沒有批次遷移舊資料。

多角色窄範圍回歸另包含舊時鐘缺少／空白場景、已有場景優先、來源不變，以及實際 SQLite 用量兩筆分開記錄、無效 JSON、傳輸失敗、無效敘事與舊 metadata 不重複計費；測試完全使用暫存存檔及模擬 bridge，不呼叫付費模型。

### 多角色自然玩法驗證（取代強提示的品質結論）

舊 test_live_focus_handoff 含「誰不知情／不得發言」等強提示，只能作明示舞台指令測試，不能用其通過宣稱自然敘事通過。原四輪輸出保留。

`python3 scripts/dev/verify_card_ensemble_natural.py --run <新的名稱>` 為付費 opt-in 驗證，使用固定 `tests/fixtures/ensemble_natural.json` 六輪自然台詞／動作與預先列明判定，全程不切焦點、不修改 presence/memory。只使用獨立 temporary directory 合成成年角色存檔。runner 明確等待背景整理以便檢視狀態（正式互動不等待）；記錄失敗列入該已保存回合的 recorder 狀態，不重送同一玩家輸入。每輪狀態、原始記錄更新與模型失敗均另存 `/tmp/card-ensemble-natural-<名稱>.json`；同名拒絕覆寫；同名 `.md` 僅含玩家台詞／回覆。原報告不覆寫。預設輪間等待 30 秒，失敗同一輸入最多三次嘗試，每次保留錯誤並列出重試操作。

機制檢查與自然語氣／知情合理性分開：人工逐輪核對位置、誰能聽見、公開前後記憶、非焦點人物自主性、是否捏造場外歷史。不得只用關鍵字出現宣告角色不跑調。離線測試包含同輪入場前／離場後的知情範圍、離場角色近況隔離、無效移動證據原子失敗，以及換場後回溯／重生。

自然驗證 runner 另保存每次場景判讀、實際敘事 prompt/history、未提交草稿與記錄員輸出。相關離線測試驗證私聊原文和缺席者記憶確實不在另一人輸入中、場景判讀不能改寫事實、三段用量分別記錄、失敗草稿與重試保留，以及同名報告禁止覆寫。

### 回憶模式驗證

離線：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_recall.py tests/test_card_mode.py tests/test_card_ensemble.py -q`。涵蓋長期原文找回、無線索的主動查詢、找不到／假造來源、長訊息分頁、每人可見範圍、回捲重用 index、非法模式先拒絕、送出／重生傳遞與每步用量失敗留存。

介面：依上述 Playwright 環境執行 `node tests/ui/card_recall.test.cjs`，確認預設一般、瀏覽器持久化、送出／重生選項與手機選單；API 為替身。

明確真模型驗證：`python3 scripts/dev/verify_card_recall.py --run <unique-name> --live`。只使用 `tests/fixtures/card_recall.json` 的固定成年合成故事與暫存 usage DB；不讀玩家存檔。保留每次 agent 查詢、讀取來源及敘事，寫 `/tmp/card-recall-<unique-name>.json` 與只有對話的 `.md`，拒絕覆蓋既有結果。模型／服務失敗照實保留，不能把工具機制通過當成敘事品質通過。

自然玩法的定點複驗可使用 `python3 scripts/dev/verify_card_ensemble_natural.py --run <新名稱> --resume-report /tmp/card-ensemble-natural-<原名稱>.json --after-round 4`。runner 複製原隔離存檔，再走正常 rewind 操作，只重試原固定輸入的後兩輪；來源報告及存檔不變。報告列出複製、回溯與失敗重試。前四輪加上重試後兩輪的閱讀版必須註明是接續複驗，不能稱為最終程式一次完整六輪通過。

### 角色卡分支驗證

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_branches.py tests/test_card_mode.py tests/test_card_ensemble.py tests/test_card_ensemble_async.py tests/test_card_recall.py -q`。

分支測試使用暫存存檔與替身模型：單卡／雙角色多輪切換、歷史分叉與再分叉、全員記憶傳入敘事、父線回退不改子線、當線重置與重生、摘要未來資訊排除、回憶來源、串流捕獲 branch_id、背景記錄期間切線、無效 ID／缺快照拒絕。`tests/ui/card_branches.test.cjs` 使用既有 Playwright/CHROME_BIN 環境，驗證分叉、切回、命名、網址重載與多頁籤分支固定、手機對話框。不呼叫付費模型、不變更玩家存檔。

取消回歸：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_record_cancellation.py tests/test_card_branches.py tests/test_card_ensemble_async.py -q`。以事件閘門暫停正式 Recorder provider 的替身，先執行四種工具，再測主線／子線 regen、回退、重置立即成功、遲到工具及快照／摘要不寫回、同 index 不同 turn ID、保留目標回合仍可完成整理、串流 worker 啟動前被重置，以及不產生新分支。

### 角色卡編輯重演驗證

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_editing.py tests/test_card_branches.py tests/test_card_record_cancellation.py tests/test_card_ensemble_async.py -q`。
`node tests/ui/card_editing.test.cjs`（Playwright／CHROME_BIN 環境同上）。
使用暫存故事驗證兩種生成 × 兩種保存、舊記憶與未來原文排除、來源分支不變、失敗還原、缺快照／過期編輯拒絕、多角色與串流目的分支。UI 使用替身模型，涵蓋選項傳送、預填、手機視窗和失敗保留輸入。

記憶編輯驗證：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_memory_editor.py -q`；UI：`node tests/ui/card_memories.test.cjs`（環境需 Playwright，CHROME_BIN 可指定 Chrome）。

中斷 Recorder 保存回歸：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_memory_editor.py tests/test_card_reply_text_edit.py tests/test_card_editing.py tests/test_card_candidates.py tests/test_card_branches.py tests/test_card_record_cancellation.py -q`。使用暫存存檔及 PID 檢查替身，驗證已不存在的單卡 PID 不再阻擋保存、原 pending 標記／正文保留、不補記、快照同步及備份；另驗證完整的後續檢查點可供分支、編輯重演及歷史候選切換，缺少快照及分叉點本身未完成仍拒絕。仍存在／未知／無權限的 PID 與多角色 pending 繼續保護；不呼叫付費模型。

人工恢復記憶修改：查 `data/stories/<slug>/memory_edit_backups/<branch>/<id>/audit.json`，先備份最新分支並確認無生成或 Recorder 工作。若修改後尚未繼續遊玩，可按 files 對照還原原檔；若已有新回合，不能整份覆蓋，需依 changes 做選擇性反向修改，避免刪去後續進度。prepared 代表中途可能未完成，應逐檔比對備份；applied 為完成，rolled_back 為已還原。

### 敘事風格驗證

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_styles.py tests/test_card_mode.py tests/test_card_editing.py tests/test_card_ensemble.py -q`。使用隔離資料和替身模型檢查預設不變、自然風格只進敘事、API 傳遞與無效值拒絕。`node tests/ui/card_recall.test.cjs`、`node tests/ui/card_editing.test.cjs` 同時檢查風格選單偏好、手機顯示及送出／重生／編輯參數。測試不呼叫付費模型。


### 可組合的劇情與場景選項

Card Mode 單卡與多角色的 send、regenerate、edit 同時支援三個獨立選項：文風 narrative_style（original / natural）、劇情主動性 plot_mode（follow / explore / coauthor）、場景模式 scene_mode（default / intimate）。缺少或 null 採 original、follow、default；非法值在生成與存檔變更前回 400。前端依卡片保存本機偏好，本次選擇套用於下一次生成，GM 訊息記錄三個選項，不回寫舊回合。

探索允許合理的新事件、線索與場景配角，覆蓋原有「不得另設事件」限制，但不替玩家決定、不捏造共同往事、不自動新增固定角色。親密互動聚焦眼前人物、情緒與感官細節；與探索並用時優先發展當前互動、減少外部打斷，尊重離場與快轉。這是獨立的 Card Mode 提示選項，不沿用舊世界模式 pistol 的停用記錄或強制維持場景機制。

提示依序追加自然互動、探索、親密互動，編輯指示仍在其後；預設組合不追加任何提示。Recorder、既有狀態規則與回覆長度保持原有流程，沒有額外模型呼叫。tests/test_card_styles.py 覆蓋十二種組合及單卡／多角色 API 送出、重生、編輯與非法值無寫入；瀏覽器測試涵蓋偏好保存與請求欄位。


### 共同創作的隔離試跑

`scripts/dev/verify_card_coauthor.py --output <新目錄> --old-styles <舊 styles.py 快照>` 透過正式 Card Mode 管線在暫存存檔重建 #424、426、428、438 的模型輸入，預設不呼叫模型；加 `--live` 才呼叫設定 provider 共八次（新舊探索各四次）。保存完整請求、原回覆、結果、使用量和來源雜湊，不執行 Recorder。這是目前管線重建，不是歷史原始 request 重播；詳見 inspiration/coauthor_benchmark.md。舊 styles 檔需為可信任本地 Python 檔。測試使用 tests/test_card_coauthor.py 與 tests/test_card_styles.py。

探索多次抽樣：同一腳本可用 `--branch <分支> --targets <GM index...> --samples 3`，每個回合的兩種提示各生成三次；完整保留樣本，不挑選最佳輸出。原始輸入仍取前一則 user，不能把具體指示改寫成「自由探索」。


「探索・共同創作」使用 `plot_mode: "coauthor"`，單卡與多角色的送出、重生、編輯皆適用，並記錄在 GM metadata。原探索使用 explore，兩者互斥而不疊加；可各自搭配自然互動及親密互動。介面沿用每卡片偏好保存，不改使用者原有選擇、不額外增加模型呼叫。

### 回覆候選驗證

手動編輯最新 GM 正文：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_reply_text_edit.py tests/test_card_candidates.py tests/test_card_feedback.py -q`；`node tests/ui/card_reply_text_edit.test.cjs`（Playwright／CHROME_BIN 環境同上）。暫存存檔及替身 API 驗證最新限定、過期版本拒絕、舊版保留與採用、標記隔離、無模型／Recorder 呼叫、狀態／快照保留、pending／error、磁碟失敗還原、多角色主線／子線與知情片段位移、摘要失效，以及桌面／手機草稿／放棄／重載。截圖 `/tmp/story-reply-text-edit-1280.png`、`/tmp/story-reply-text-edit-390.png`，不修改玩家存檔。

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_candidates.py tests/test_card_recall.py tests/test_card_record_cancellation.py tests/test_card_branches.py tests/test_card_editing.py tests/test_card_memory_editor.py tests/test_card_ensemble_async.py -q`

使用暫存存檔及替身模型驗證成功／失敗重生計數、候選選項、正文／記憶／關係／隨身物／時鐘切回、後續上下文排除未採用版本、舊 worker 取消、未完成候選只重錄記憶、歷史採用新分支、一般／編輯分支候選繼承、收藏跨回退保留、重用 index 隔離、過期／缺快照拒絕、寫入失敗還原，以及記憶校正不因切版失效。

`node tests/ui/card_candidates.test.cjs`（沿用 Playwright／CHROME_BIN 環境）檢查桌面／手機版本翻閱、只預覽不改進度、收藏、過期提示、採用、歷史分支、重載及續寫快捷鍵的風格傳遞與草稿保留。截圖為 `/tmp/story-candidates-1280.png`、`/tmp/story-candidates-390.png`。所有 UI API 為替身，無法代表真模型敘事品質。

演出評語：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_feedback.py tests/test_card_candidates.py -q` 驗證讀取無寫入、讚／註記欄位獨立、comment revision 衝突、regen／切版／回退／重置隔離、一般／候選分支繼承、上下文不洩入敘事或 Recorder、保存失敗還原、背景 Recorder 同步保留評語、舊存檔識別與多角色分支隔離。

`node tests/ui/card_feedback.test.cjs`（同 Playwright 環境）驗證桌面／手機直接按讚不彈窗、重複請求阻擋、保存失敗保留草稿、離開確認、重新載入、取消讚保留註記、舊版評語、新版本空白、清空註記及開場標記。截圖 `/tmp/story-feedback-1280.png`、`/tmp/story-feedback-390.png`，不對真實遊戲存檔按讚。

持續指示：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_instructions.py tests/test_card_styles.py tests/test_card_branches.py tests/test_card_editing.py tests/test_card_candidates.py -q`，以暫存存檔與替身模型驗證單卡／多角色的開關、重載、空白／型別／長度檢查、revision 衝突、忙碌及保存失敗、真實送出／regen／編輯入口的 prompt／版本記錄、本輪優先順序、關閉後移除歷史指示、分支繼承與獨立修改、rewind／reset 保留。

`node tests/ui/card_instructions.test.cjs`（同 Playwright 環境）驗證桌面／手機入口、啟用狀態、草稿／衝突／重載／放棄、開關保留文字、清空、分支目標、原始玩家輸入及各版指示查看。截圖 `/tmp/story-instructions-active-1280.png`、`/tmp/story-instructions-active-390.png`、`/tmp/story-instructions-editor-1280.png`、`/tmp/story-instructions-editor-390.png`。未驗證真模型必然遵守語氣，未變動玩家存檔設定。

輸入區快捷列維持單行：續寫按鈕顯示「繼續 →」，持續指示以顏色與 aria-pressed 表示開關，文字固定「持續指示」。窄於控制列的視窗可橫向捲動。

同一 UI 測試亦檢查輸入區短／中／長快捷切換與齒輪選單雙向同步、重載後保留選擇、生成期間停用，以及實際送出的 `response_length`；桌面／手機輸入區截圖為 `/tmp/story-length-shortcuts-1280.png`、`/tmp/story-length-shortcuts-390.png`。

### 探索按鈕與仙俠引子隔離試玩

探索素材的正式資料位於 `story_design/_shared/xianxia_hooks_v1.json`，卡片用 `exploration_material_bank: "xianxia"` 選用。`tests/test_card_exploration.py` 驗證單卡／多角色 state 的選用與讀取無寫入；`tests/ui/card_exploration.test.cjs` 用替身 API 驗證桌面／手機探索、輸入方向、忙碌狀態、失敗保留與繼續發展不追加素材。執行方式與其他 Playwright 測試相同，不呼叫付費模型、不寫正式遊玩進度。

`python3 scripts/dev/verify_card_hook.py --output /tmp/<新的試玩名稱>` 凍結 `xianmen_last_night/main` 最新完整回合，不呼叫模型；加 `--live` 才使用設定的敘事模型與 Recorder。可指定 `--slug`、`--branch` 及 `--seed`（素材 ID）。預設一條五輪續寫，加一條從相同首輪改向的兩輪路徑；素材只在首輪注入，後續不補劇情方向。輸出必須是新目錄且不在正式遊戲資料內。

每輪 request／report／dialogue.txt 與試玩存檔都保存在指定目錄，敘事用量寫入試玩 usage DB，Recorder 用量可能缺漏；不建立正式分支、不修改原角色卡或正式記憶。腳本短試玩停用摘要背景工作，保留所有未摘要回合。`source_check.json` 只比對凍結歷史前綴，允許使用者同時新增正式回合；如前綴被回捲或重生，應註明試玩仍以舊快照為基準。

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_hook_trial.py -q` 使用替身模型驗證正式資料不變、引子只注入首輪、改向路徑不讀另一條後續及 Recorder 失敗停試。真人／真模型品質另讀輸出判斷，不以機制測試當品質結論。

### Pi lore 核對試驗（唯讀）

`python3 scripts/lore/pi_review.py <slug> --branch main --entry <id> --pi-cli <已安裝的 pi 執行檔或 CLI.js>` 使用現有 Vertex project／region／model 及 ADC，一條一個全新隔離 session，每批最多四條。此命令會呼叫模型；沒有自動安裝 Pi、沒有啟動背景更新，也沒有正式 lore 寫回。每條輸出獨立暫存目錄中的 snapshot.json、events.jsonl、stderr.log、report.json。`reviewed` 表示程式已核驗完整工具流程；`review.all_supported` 另表達模型是否認為每句都有支持，仍需注意語義判斷限制。rejected／stale 不沿用。

離線驗證：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_pi_lore_review.py tests/test_card_lore.py -q`；`node tests/ui/pi_review_guard.test.mjs`。涵蓋原文來源雜湊、撤回／同編號重生、作者設定排除、快照獨立、缺頁／區間缺口、漏句／重複、虛構引用、其他來源拒讀、工具紀錄竄改、只用文字稱完成及模型截斷。這些檢查不能證明模型的語義判斷一定正確。


### Pi 共用維護任務

此流程不用逐條人工 approve；`run --apply` 在程式查證及版本檢查通過後直接提交。未加 `--apply` 只產生試跑結果。遊玩後自動觸發／下一輪資料組裝尚未接入，現有 Recorder／摘要照常。

```bash
# 使用已安裝 Pi executable，或其 dist/bundle/cli.js；不使用 npx 自動安裝。
python3 scripts/lore/pi_maintenance.py run xianmen_last_night --kind memory --kind lore --recent 20 --pi-cli /absolute/path/to/pi --apply
# 一次針對少量明確記憶；ID 可在 snapshot.json 查到。
python3 scripts/lore/pi_maintenance.py run xianmen_last_night --kind memory --focus mem:0:485 --recent 2 --pi-cli /absolute/path/to/pi
# 摘要沿用同一協定，單獨小批執行。
python3 scripts/lore/pi_maintenance.py run xianmen_last_night --kind summary --pi-cli /absolute/path/to/pi --apply
python3 scripts/lore/pi_maintenance.py apply /absolute/path/to/pi_maintenance_run
python3 scripts/lore/pi_maintenance.py undo /absolute/path/to/backup
python3 scripts/lore/pi_maintenance.py recover /absolute/path/to/prepared_backup
```

`run` 會呼叫目前 `llm_config.json` 的 Vertex 模型（使用 ADC），可用 `--timeout 300` 設上限，最大 600 秒。memory／lore 非空提案會再開新 Pi 上下文獨立查證，兩階段共用 timeout；摘要專用任務完成自核與一致性核對後不固定再開第二階段；驗證失敗整批不寫回。每批原文 `--recent` 為 1–60 則；完整歷史仍能按需搜尋。回傳 run_dir 內有 snapshot/events/report；正式寫回備份在 `data/stories/<slug>/pi_maintenance_backups/<branch>/<id>/`。舊 `pi_review.py` 保留為單條 lore 唯讀核對，不會自動寫回。

提交前會再次重播 events 並查版本，請不要手改工具紀錄。原文、NPC、lore、摘要、卡片或分支樹任一變更均須重新跑任務。摘要工作提交的短鎖跨程序共用，模型來回期間不持鎖。

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_memory.py tests/test_compaction.py tests/test_pi_maintenance.py tests/test_pi_lore_review.py tests/test_card_lore.py -q
node --test tests/ui/pi_maintenance_guard.test.mjs tests/ui/pi_review_guard.test.mjs
```


Pi 用量與調整基準：新任務不論成功／失敗／超時都保存 `usage.json`，並持久保存於 story 的 `pi_maintenance_usage/<branch>/<task_id>.json`。同任務 apply 只更新這一份帳，保留各次寫回嘗試。不要將子查證帳再加一次。費用尚無可信價格時是 null，未知 tokens 也不當 0。

舊任務可直接回算，不重新呼叫模型或修改遊玩資料：

```bash
python3 scripts/lore/pi_maintenance.py usage /absolute/path/to/pi_maintenance_run
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_pi_maintenance.py tests/test_pi_usage.py -q
```

回算只有原始事件確實留下的用量；沒有事件的舊呼叫不能補猜。對照品質時同時看任務是否寫回、拒絕原因、已知用量與缺報欄位，而非只比較成功批次。


探索草稿與規模驗證：`tests/test_card_exploration.py` 驗證 48 條素材（24 小、24 大）的 state 回傳與無存檔修改；`tests/ui/card_exploration.test.cjs` 驗證桌面／390px 手機大小選擇、持久偏好、按鈕不送出、原草稿保留、重抽不巢狀包裝、手動送出與失敗復原。`tests/ui/card_candidates.test.cjs` 的繼續流程也改成先驗證草稿，再手動送出。測試用替身 API，不生成正式故事。

探索草稿閱讀位置：事件標題與引子置於文字框最前，填入後回到開頭，不自動聚焦或喚起手機鍵盤；點文字框仍可編輯。繼續仍在尾端追加，但帶入後同樣停在開頭、不自動聚焦或喚起鍵盤。`card_exploration.test.cjs` 驗證桌面／手機文字順序、scrollTop、游標位置及不自動聚焦。

Card Mode 輸入框提供小型「清空」按鈕，只清除本地草稿並重設輸入高度／探索草稿追蹤，不送出、不改對話；空白或生成中停用。按鈕置於角色卡快捷列，世界模式不受影響。探索 UI 替身測試覆蓋桌面與手機清空無 POST、空白停用及無水平溢出。

Card Mode 手機輸入列：「清空」置於快捷列齒輪右側；「持續指示」按鈕縮為「指示」（對話框名稱不變）。探索 UI 測試驗證清空位於齒輪右側與手機快捷列不需橫向捲動。


Pi 隨身物與背景維護：`inventory` 與 memory/lore/summary 使用同一查讀、提案、逐項引文、獨立複核及版本檢查流程。隨身物 operation 只可替換現有 target_ids，item_name 是統一名稱，text 空字串表示刪除；合併必須包含所有目的條目，不累加同批重複數量。原始敘事與舊回合快照不改，套用更新 character_state 與最新 GM 的 post-state inventory，保存備份及 token usage。不能因久未提及就刪物。

`pi_schedule` 按已完成且 Recorder 成功的 user/GM 配對計數，預設每 50 個來回（約 100 則訊息）啟動一批四類維護；每批最多十二筆操作，不保證掃完全部歷史。只在啟用卡片的 Recorder 結束後啟動背景執行緒；查讀可搜尋完整凍結歷史，預讀近期 20 則，其餘依需要搜尋。模型工作不持遊玩鎖，不阻塞送出。分支獨立計數；新分支或回退失去計數錨點時重設基準。失敗、超時或遊玩／摘要已更新則不套用，隔下一個 50 回合再試，不反覆耗 API。一般即時摘要仍保留，Pi 是補充維護；不啟用時無模型費用。排程狀態保存在分支 pi_schedule_status.json，設定在卡片 pi_schedule.json。

```bash
python3 scripts/lore/pi_maintenance.py schedule xianmen_last_night --every 50 --pi-cli /absolute/path/to/pi
python3 scripts/lore/pi_maintenance.py schedule xianmen_last_night --disable
python3 scripts/lore/pi_maintenance.py run xianmen_last_night --kind inventory --recent 20 --pi-cli /absolute/path/to/pi --apply
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_pi_schedule.py tests/test_pi_maintenance.py tests/test_pi_usage.py -q
node --test tests/ui/pi_maintenance_guard.test.mjs
```
Recorder 使用現有名稱與剩餘量，literal `''` / `""` 會正規化為刪除標記；狀態敘述仍由 Pi 查原文複核，不以「已使用」關鍵字直接刪除半瓶物品。


Pi 整理後回退：memory/inventory 沿 GM post-state 快照恢復，回到整理落點保留、回到更早點恢復舊值（舊重複資料可能重現）。Pi lore 保存 previous_version 與 maintenance_anchor，讀取時依保留 GM 的 index/hash 選出有效版本；新條目在整理落點被移除後不可用，替換條目則回復舊版本。分支繼承同一版本鏈但獨立存檔，同編號不同正文不復活未來 lore；維護再抽取時僅讀有效版本。摘要原有 covered-prefix 檢查保留仍完全有效的歷史摘要，回退進其覆蓋範圍則清除並更新 revision，阻擋舊背景提交。回退／重置當下校正 Pi 回合計數，舊任務不得覆蓋重設後的計數帳本。
驗證：`tests/test_pi_rewind.py` 透過真實 rewind/fork API 驗證四類資料，無正式故事修改或模型呼叫。

隨身物寫入拆分：`set_inventory` 僅更新／刪除逐字匹配的現有名稱；`add_inventory` 只新增含中文且尚不存在的名稱。任一非法名稱使整批拒絕並回傳現有名稱讓 Recorder 修正，不再把改名當新增。Pi 替換目的名稱亦須含中文（刪除舊英文項目仍允許）。數量變更須有原文明確依據，不因閉關或快轉自動推算消耗。

### Galgame 慢熱互動驗證

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_card_galgame.py tests/test_card_branches.py tests/test_card_candidates.py tests/test_card_editing.py tests/test_card_record_cancellation.py -q`；`node tests/ui/card_galgame.test.cjs`（Playwright，CHROME_BIN 可指定 Chrome）。測試用隔離存檔與替身模型，驗證日額／冷卻／重複證據、各門檻、同意引文、厭惡與慢速修復、性慾衰減、生成失敗、Recorder 不改分與不重結算、回退／重生／分支，以及桌面和390px手機數值面板與輸出轉義。不呼叫付費模型，不修改現有玩家存檔。

新卡 `story_design/linwan_galgame/card.json` 已選用；重載服務程式後，從 `/card/linwan_galgame` 開啟即可沿既有流程初始化。真模型對自由文字的分類及敘事一致性需另外試玩，不用替身測試宣稱已驗證。

2026-10-09 本次驗證：新 Galgame 21 項全過；Card Mode 相關組合共 262 passed、1 skipped，排除工作區已刪除 `story_design/suman/card.json` 所致的 `test_suman_appearance_reaches_roleplay_prompt`（未排除時此項失敗）。桌面1280px與手機390px Playwright 通過。

AGENTS baseline 三檔另有238 passed、3 failed：`TestDungeonAPI.test_edit_replays_pending_debug_overrides_and_consumes_source_pending`（5080/5040）、`TestDungeonAPI.test_regenerate_stream_replays_pending_debug_overrides_and_consumes_source_pending`（5030/5015）、`TestMigrateListToMap.test_auto_migration_on_load`（inventory 仍為 list）。這三條走世界模式的既有 debug override／state migration 路徑，此次未修改其實作；不以本次通過的 Card Mode 測試宣稱全倉庫通過。

本次另以新版程序在 localhost:5056 提供試玩（僅本機，非開機服務；日誌 `/tmp/story_galgame_5056.log`），共用此工作區存檔，5051 原程序未重啟。新卡頁面200且 state 回讀四值0、陌生、僅1則開場，沒有呼叫付費模型。一般後續啟動仍使用專案既有方式；不要用仍未重載新程式的舊5051程序遊玩此新卡。

Galgame 判定格式修正（2026-10-09）：改用原生結構化 JSON，保留 enum／同意引文檢查，格式失敗最多自動修正一次。`tests/test_card_galgame.py tests/test_card_record_cancellation.py tests/test_card_generation_timeout.py tests/test_recall_structured_output.py` 共48 passed。使用當前 Vertex/Gemini 在隔離目錄 `/var/folders/cv/nr37t6051dz68d5m8yqk2hlr0000gn/T/galgame-structured-check-g41nrvyc` 跑一次完整生成與 Recorder，結果 ok=true、record_status=done、turns=1；再驗證禮貌詢問牽手 decline／反感0，以及無視拒絕糾纏 boundary_violation／反感20。沒有寫入正式對話。5051與5056均重載並state回讀成功。歷史失敗只留下API成功日誌，無法逐次分辨缺工具提交或引文驗證失敗；本次修正消除可省略工具提交的設計，並新增明確診斷。

2026-10-09：「繼續」恢復直接送出，會將既有草稿與「（請繼續發展）」一起送出；不重複追加同一尾端指示，沿用目前分支、字數、文風等生成設定。生成中停用並阻擋重複點擊，失敗沿原送出流程恢復草稿。「探索」仍只帶入草稿。card_candidates 與 card_exploration 的桌面／手機測試驗證直接送出。
