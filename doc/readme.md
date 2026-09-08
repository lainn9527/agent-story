# Story RPG 文件總覽

統一 Agent 入口：`/AGENTS.md`

這個專案是一個以 Flask 為核心的文字 RPG 系統，支援：

- 多故事（story）與多分支（branch）時間線
- 角色狀態追蹤（含 inventory/systems/relationships）
- 世界觀知識庫（lore）與事件追蹤（events）
- 命運骰、GM cheats、存檔、副本進度系統
- 多 LLM provider（Gemini / Claude CLI / Codex Agent）與串流回應

目前主要的 Python 內部模組已整理到 `story_core/`；entrypoints 仍維持在 repo root（例如 `app.py`、`auto_play.py`）。

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
  },
  "codex_agent": {
    "model": "gpt-5.4"
  }
}
```

說明：
- `codex_agent` 是 read-only GM provider。後端會為每回合建立臨時工作區，只暴露當前 branch 與 lore 相關檔案給 Codex 讀取，不會讓它直接改 runtime data。
- UI 仍維持同一套 `/api/send*` / parser / state pipeline；Codex 只是新的可選 provider，不會替換既有 Gemini / Claude 路徑。

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

0. `doc/architecture_v2.md`: **v2 北極星** — 確定性引擎 ⊕ 生成式旁白；底層/模組/內容分層（新功能先讀這份歸位）。含 §7「如何新增一顆動作按鈕 / 動詞 / module」配方。
   - `doc/design_decisions_v2.md`: 邊界決策的「為什麼」（沈浸感=敘事、引擎獨佔欄位、GM 提議引擎執行、動詞/名詞）
   - `doc/v2_state_lifecycle.md`: **runtime 狀態與除錯地圖** — branch 檔案地圖、副本生命週期、snapshot/fork 怎麼搬、recap 壓縮，以及「確定性變更在 fork 後被回捲」這類反覆 bug 的修法＋手術式改狀態配方。**debug 狀態/fork/副本前先讀這份**，省得每次重新考古。
1. `doc/architecture.md`: 系統結構與請求管線
2. `doc/prompt_design.md`: LLM prompt 組裝與 tag 契約
3. `doc/game_mechanics.md`: 分支、骰子、副本、存檔等規則
4. `doc/api_reference.md`: API 端點與 SSE 格式
5. `doc/development.md`: 開發、測試、腳本與除錯

## 既有文件

- `doc/card_mode_proposal.md`: **角色卡模式（Card Mode）實作規格** — 提案待實作；單角色一對一形態、獨立介面、只留關係/記憶/時間/隨身物
- `doc/world_authoring.md`: **新增一個世界（story）** — 檔案清單、引擎咬住的欄位（貨幣/身分/階段/`phases`）、catalog、無副本世界的設定；開新世界前先讀這份
- `doc/testing_plan.md`: 測試策略與覆蓋現況
- `doc/wsl2_setup.md`: WSL2 部署與 systemd 啟動
- `doc/sync.md`: 多機資料同步流程

## 核心資料概念

- `story_design/<story_id>/`: 可版本控制的世界設計資料（prompt/schema/lore）
- `data/stories/<story_id>/`: 遊戲執行期資料（branches、DB、images、saves）
- `data/llm_traces/<story_id>/`: LLM request/response trace（依日期與 branch/message 分層）
- `timeline_tree.json`: 分支樹與 active branch
- `branches/<branch_id>/messages.json`: 該分支的增量訊息（delta）

## 重要提醒

- `llm_config.json` 請勿提交到版本庫。
- `data/` 為 runtime 資料，切換機器前先做同步/備份。
- 啟動後前端會先呼叫 `POST /api/init`，觸發遷移與初始化流程。
