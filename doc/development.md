# 開發與維運指南

## 1) 開發環境

### 必要條件

- Python 3.11+（WSL2 文件示例使用 3.12）
- `pip`
- 若使用 Claude provider：`@anthropic-ai/claude-code` CLI

### 安裝

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### LLM 設定

建立或修改 `llm_config.json`（含金鑰，不應提交）：

- `provider`: `gemini` 或 `claude_cli`
- `gemini.api_keys`: 可放多把 key（系統會做 fallback/cooldown）
- `gemini.model` / `claude_cli.model`

## 2) 本機啟動流程

```bash
python app.py
```

預設：

- Host: `0.0.0.0`
- Port: `5051`（可用 `PORT` 覆蓋）

首次進站前端會觸發 `POST /api/init`，執行必要遷移與索引初始化。

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
```

### 目前測試範圍（重點）

- Branch tree / fork / sibling 邏輯
- state update 與 map/list schema 行為
- lore/event DB 搜尋與索引
- Flask API 合約（`tests/test_api_routes.py`）
- async tag extraction 關鍵路徑

詳見 `doc/testing_plan.md`。

## 4) 常用腳本

| 檔案 | 用途 |
|---|---|
| `deploy.sh` | Mac 生產機更新與重啟（port 5051） |
| `deploy_wsl2.sh` | WSL2 環境更新與 systemd 重啟 |
| `scripts/backfill_snapshots.py` | 回填歷史 GM 訊息 snapshot |
| `scripts/backfill_branch_titles.py` | 回填分支標題 |
| `scripts/backfill_npc_lifecycle.py` | 回填 NPC lifecycle + R1 去重 + 重建 `state.db` |
| `scripts/migrate_current_dungeon.py` | 回填 `current_dungeon` |
| `scripts/cleanup_character_state.py` | 清理/修復 state 汙染 |
| `scripts/clean_state.py` | 移除 legacy 垃圾欄位 |
| `scripts/lore_cleanup.py` | 一次性 lore 清理 |
| `scripts/lore_merge.py` | lore 語意合併 |

多數腳本支援 `--dry-run`；先 dry-run 再 apply。

## 5) 日常開發建議流程

1. 先啟動 server + 打開 UI 驗證核心 flow（init/messages/send）。
2. 修改後至少跑：
   - 你改到的模組對應測試
   - `tests/test_api_routes.py`（若改 API）
3. 若改了狀態/抽取格式，順手驗證：
   - `tests/test_state_update.py`
   - `tests/test_extract_tags_async.py`
4. 若改 lore/event 搜尋，跑：
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

- Mac production 更新腳本：`./deploy.sh`
- WSL2 更新腳本：`./deploy_wsl2.sh`

`deploy.sh` 會在 `/Users/eddylai/story-prod` 拉最新並重啟 `5051`。  
部署會重啟服務，建議先確認可接受短暫中斷。

## 9) Usage / Trace 觀測

### 9.1 Token usage

- API：`GET /api/usage?story_id=<id>&days=7`
- 跨故事總覽：`GET /api/usage?all=true`
- 存放位置：`data/stories/<story_id>/usage.db`

### 9.2 LLM trace

- 開關：`LLM_TRACE_ENABLED`（預設開）
- 保留天數：`LLM_TRACE_RETENTION_DAYS`（預設 14）
- 由 `app.py::_trace_llm()` 寫入（best-effort，不應阻斷主流程）
- 檔案位置：`data/llm_traces/<story_id>/<YYYY-MM-DD>/<branch_id>/<msg_tag>/`
- 檔名格式：`<HHMMSS.mmm>_<stage>_<id>.json`

常見 `stage`：

- `gm_request` / `gm_response_raw`
- `extract_tags_request` / `extract_tags_response_raw`
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
- 必要時使用 `scripts/backfill_snapshots.py --dry-run`

### 問題：切換機器後資料不一致

- `data/` 與 `story_design/` 需同步（參考 `doc/sync.md`）
- 避免兩台同時寫同一份 SQLite

### 問題：副本 API 行為異常

- 呼叫 dungeon 端點時請明確帶 `branch_id`
- 檢查 `branches/<branch_id>/dungeon_progress.json` 與角色 `current_phase/current_dungeon`

## 11) 版本與變更紀錄

- 版本號來源：`VERSION`
- 變更紀錄：`CHANGELOG.md`
