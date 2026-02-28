# Story RPG 文件總覽

統一 Agent 入口：`/AGENTS.md`

這個專案是一個以 Flask 為核心的文字 RPG 系統，支援：

- 多故事（story）與多分支（branch）時間線
- 角色狀態追蹤（含 inventory/systems/relationships）
- 世界觀知識庫（lore）與事件追蹤（events）
- 命運骰、GM cheats、存檔、副本進度系統
- 多 LLM provider（Gemini / Claude CLI）與串流回應

## 快速開始

1. 建立虛擬環境並安裝依賴

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. 準備 `llm_config.json`（此檔案包含金鑰，已被 `.gitignore`）

```json
{
  "provider": "gemini",
  "gemini": {
    "api_keys": [{"key": "YOUR_KEY", "tier": "free"}],
    "model": "gemini-3-flash-preview"
  },
  "claude_cli": {
    "model": "claude-opus-4-6"
  }
}
```

3. 啟動服務

```bash
python app.py
```

4. 開啟介面

- 主介面: `http://localhost:5051/`
- Lore Console: `http://localhost:5051/lore`

## 開發常用指令

```bash
pytest
pytest -m "not slow"
```

## 建議閱讀順序

1. `doc/architecture.md`: 系統結構與請求管線
2. `doc/prompt_design.md`: LLM prompt 組裝與 tag 契約
3. `doc/game_mechanics.md`: 分支、骰子、副本、存檔等規則
4. `doc/api_reference.md`: API 端點與 SSE 格式
5. `doc/development.md`: 開發、測試、腳本與除錯

## 既有文件

- `doc/testing_plan.md`: 測試策略與覆蓋現況
- `doc/wsl2_setup.md`: WSL2 部署與 systemd 啟動
- `doc/sync.md`: 多機資料同步流程

## 核心資料概念

- `story_design/<story_id>/`: 可版本控制的世界設計資料（prompt/schema/lore）
- `data/stories/<story_id>/`: 遊戲執行期資料（branches、DB、images、saves）
- `timeline_tree.json`: 分支樹與 active branch
- `branches/<branch_id>/messages.json`: 該分支的增量訊息（delta）

## 重要提醒

- `llm_config.json` 請勿提交到版本庫。
- `data/` 為 runtime 資料，切換機器前先做同步/備份。
- 啟動後前端會先呼叫 `POST /api/init`，觸發遷移與初始化流程。
