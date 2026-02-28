# Multi-Machine Sync Guide

## Setup
- **Mac (dev)**: `/Users/eddylai/story-prod/` — production server (port 5051)
- **Windows (dev)**: `~/project/agent-story/` — via Tailscale (`100.81.16.83`, SSH port 2222)

## What's in git vs what needs manual sync

| 路徑 | Git tracked? | 說明 |
|------|-------------|------|
| `story_design/<story_id>/world_lore.json` | ✅ whitelisted | Base lore — **需要手動 sync**（server 會修改但不自動 commit） |
| `story_design/<story_id>/system_prompt.txt` 等 | ✅ tracked | 正常 git push/pull |
| `data/` | ❌ gitignored | 存檔、SQLite DB、images — 需要手動 sync |
| `llm_config.json` | ❌ gitignored | API keys — 需要手動 sync |

## Mac → Windows sync 指令

```bash
# 1. Runtime data (存檔、DB、images)
rsync -avz -e "ssh -p 2222" /Users/eddylai/story-prod/data/ eddylai@100.81.16.83:~/project/agent-story/data/

# 2. Base lore (story_design — server 會即時修改，不走 git)
rsync -avz -e "ssh -p 2222" /Users/eddylai/story-prod/story_design/ eddylai@100.81.16.83:~/project/agent-story/story_design/

# 3. API keys / provider config
rsync -avz -e "ssh -p 2222" /Users/eddylai/story-prod/llm_config.json eddylai@100.81.16.83:~/project/agent-story/llm_config.json
```

## Sync 後在 Windows 重建 lore index

```bash
curl -X POST http://localhost:5051/api/lore/rebuild
```

## 注意事項

- **不要兩台同時跑 server**：SQLite 不支援多機同時寫入，`lore.db` / `events.db` 可能損壞
- **切換前確認 Syncthing / rsync 完成**：避免存檔遺失
- **只 sync 單一檔案**（例如只更新 world_lore.json）時，先確認對方沒有新增的 entries 要保留
