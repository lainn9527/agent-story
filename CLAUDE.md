# Story RPG Project

## Overview
Interactive story RPG with branching timelines, multi-story support, and rich narrative systems. Flask backend + vanilla JS frontend, using Claude CLI as the GM (Game Master).

## Architecture
- **Backend**: Flask on port 5051 (`app.py`)
- **AI Bridge**: Multi-provider LLM bridge (`llm_bridge.py`) — dispatches to Gemini API or Claude CLI
- **Frontend**: Vanilla HTML/CSS/JS, dark theme, single-page app with slide-out drawer
- **Data**: Per-story isolation in `data/stories/<story_id>/`, SQLite for search indexes

## Key Commands
- Run server: `python app.py` (port 5051)

## Key Files
| File | Purpose |
|------|---------|
| `app.py` | Flask routes, multi-story support, unified tag pipeline |
| `llm_bridge.py` | LLM provider dispatcher, auto-reloads `llm_config.json` |
| `llm_config.json` | Provider config: switch `"provider"` between `"gemini"` / `"claude_cli"` |
| `gemini_bridge.py` | Gemini API bridge (streaming + non-streaming), multi-key fallback |
| `gemini_key_manager.py` | Gemini API key pool with rate-limit/expiry cooldown tracking |
| `compaction.py` | Conversation compaction: rolling narrative recap via background LLM |
| `claude_bridge.py` | Claude CLI bridge, `--output-format json` for session_id |
| `lore_db.py` | SQLite lore search engine (CJK bigram scoring) |
| `usage_db.py` | SQLite token/cost tracking per story, WAL mode |
| `event_db.py` | SQLite event tracing engine (CJK bigram search) |
| `image_gen.py` | Pollinations.ai async image generation |
| `npc_evolution.py` | Background NPC evolution via Claude CLI |
| `world_timer.py` | Per-branch world day/time tracking, TIME tag parsing |
| `auto_play.py` | AI self-play: GM + Player AI loop on dedicated `auto_` branch |
| `static/app.js` | Frontend: drawer UI, NPC/events/images, schema-driven panels |
| `static/style.css` | Dark theme CSS (mobile: novel reader style with serif fonts) |
| `templates/index.html` | Single page HTML with slide-out drawer |

## Data Layout
```
data/
  stories.json                          # Story registry with active_story_id
  stories/<story_id>/
    system_prompt.txt                   # GM system prompt with placeholders
    character_schema.json               # Schema-driven character panel config
    default_character_state.json        # Initial character state
    parsed_conversation.json            # Immutable original messages
    timeline_tree.json                  # Branch tree + session IDs
    messages_<branch_id>.json           # Per-branch delta messages
    character_state_<branch_id>.json    # Per-branch character state
    world_lore.json                     # 41 world lore entries
    lore.db                             # SQLite lore search index
    npcs.json                           # NPC profiles with Big5 personality
    events.db                           # SQLite event tracking
    usage.db                            # SQLite token/cost tracking
    npc_activities_<branch_id>.json     # Background NPC activity logs
    auto_play_state.json                # Auto-play progress (auto_ branches only)
    branches/<branch_id>/
      world_day.json                    # Per-branch world day/time {day, hour}
    images/                             # Generated scene images
  auto_play_characters/                 # Character JSON files for auto-play
```

## Multi-Story System
- Auto-migration from legacy flat layout to `data/stories/<story_id>/`
- Schema-driven character panel; extra fields auto-display without schema changes
- Routes: GET/POST `/api/stories`, POST `/api/stories/switch`

## Branching System
- Timeline tree stored in per-story `timeline_tree.json`
- Each branch has: parent chain, branch_point_index, session_id
- `get_full_timeline(story_id, branch_id)` walks parent chain to build full message history
- Per-branch delta messages and character state
- ChatGPT-style UX: edit (✎) on user msgs, regen (↻) on GM msgs, `< 1/N >` sibling switcher

### Blank Branches (Fresh Game Start)
- `POST /api/branches/blank` — creates a branch with `branch_point_index: -1` (inherits zero messages)
- Uses `default_character_state.json` for fresh character state, empty NPCs `{}`
- Branch metadata has `blank: True` flag
- Blank branches are excluded from sibling switcher (`< 1/N >`) via `blank` flag check in `_get_fork_points` / `_get_sibling_groups`
- Drawer UI: `⊕` button next to `+`, blank branches render at depth 0 (same level as main)
- Depth-0 branches use accordion UI — only one expanded at a time, children collapse/expand on click
- After creation, frontend auto-sends: `"開始一個全新的冒險。請引導我創建角色（名稱、性別、背景等），然後開始故事。"`
- Auto-play supports `--blank` flag for the same behavior

## Hidden Tag System (GM → Backend)
GM responses can contain hidden tags that the backend extracts and processes. All tag extraction is unified in `_process_gm_response()`.

| Tag | Regex | Purpose |
|-----|-------|---------|
| `<!--STATE {...} STATE-->` | `_STATE_RE` | Character state updates (inventory, stats, etc.) |
| `<!--LORE {...} LORE-->` | `_LORE_RE` | World lore entries → `world_lore.json` + search index |
| `<!--NPC {...} NPC-->` | `_NPC_RE` | NPC profiles with Big5 personality → `npcs.json` |
| `<!--EVENT {...} EVENT-->` | `_EVENT_RE` | Event tracking (伏筆/轉折/戰鬥/etc.) → `events.db` |
| `<!--IMG prompt: ... IMG-->` | `_IMG_RE` | Scene illustration → async Pollinations.ai download |
| `<!--TIME days:N TIME-->` / `<!--TIME hours:N TIME-->` | `_TIME_RE` | Advance world day/time → `world_day.json` |

## Context Injection (Backend → GM)
Each user message is augmented via `_build_augmented_message()` before sending to Claude:
- `[相關世界設定]` — Top-5 lore entries matching user message (CJK bigram search)
- `[相關事件追蹤]` — Top-3 relevant events from `events.db`
- `[NPC 近期動態]` — Last 2 rounds of background NPC activities

## NPC System
- `npcs.json` stores structured NPC data: name, role, appearance, Big5 personality (1-10), backstory, traits
- NPC profiles injected into system prompt via `{npc_profiles}` placeholder
- Frontend: NPC cards with Big5 personality bars in drawer

## Event Tracing
- `event_db.py` — SQLite with CJK bigram search (same pattern as `lore_db.py`)
- Event types: 伏筆/轉折/遭遇/發現/戰鬥/獲得/觸發
- Status flow: planted → triggered → resolved/abandoned
- Frontend: event items with type badge + status color dot in drawer

## Image Generation
- `image_gen.py` — Pollinations.ai, downloads in daemon thread (non-blocking)
- Images saved to `data/stories/<story_id>/images/`
- Frontend polls `/api/images/status` every 3s until ready (60s timeout)
- Image info stored in message JSON as `"image": {"filename": "...", "ready": bool}`

## Background NPC Evolution
- `npc_evolution.py` — Triggers every 3 player turns (120s cooldown)
- Calls LLM via `llm_bridge.call_oneshot()` in background thread to simulate NPC autonomous activities
- Saves to `npc_activities_<branch_id>.json`
- Activities shown under NPC cards in drawer and injected as context

## Auto-Play (AI Self-Play)
Standalone script where two AI instances play the game autonomously — a GM and a Player AI.

**Run**: `python auto_play.py --character data/auto_play_characters/lin_hao.json --max-turns 50`

| File | Purpose |
|------|---------|
| `auto_play.py` | Orchestrates GM + Player AI loop, writes to dedicated `auto_` branch |
| `data/auto_play_characters/` | Character JSON files for the Player AI |

**Key flags**: `--max-turns`, `--turn-delay`, `--max-dungeons`, `--with-images`, `--no-blank`, `--resume --branch-id <id>`

**Data flow**:
- Default: creates blank branch `auto_<8-hex>` with `branch_point_index: -1` (no inherited messages, fresh character state)
- With `--no-blank`: forks from `--parent-branch` at `--branch-point` (inherits messages)
- Writes `messages.json`, `character_state.json`, `auto_play_state.json` into `branches/auto_<id>/`
- Reuses the same unified tag pipeline (`_process_gm_response`) as normal gameplay
- `auto_play_state.json` tracks: `current_turn`, `current_phase`, `death_detected`, `consecutive_errors`

**Live View in Web UI**:
- `/api/messages?after_index=N` — incremental polling, returns only messages with `index > N`
- For `auto_` branches, response includes `live_status` (`"running"` / `"finished"` / `"unknown"`) + `auto_play_state`
- Frontend polls every 3s, appends new messages via `appendMessage()`, auto-scrolls
- `AUTO` badge on auto branches in drawer; `● LIVE` pulsing indicator in header during playback
- Input disabled while viewing live auto-play; re-enabled when `live_status === "finished"`

## World Timer
- `world_timer.py` — per-branch world day/time tracking stored in `branches/<bid>/world_day.json`
- Format: `{"day": 1, "hour": 0}` — day (integer), hour (0-23)
- GM outputs `<!--TIME days:N TIME-->` or `<!--TIME hours:N TIME-->` to advance time
- `process_time_tags()` called in `_process_gm_response()` — parses tags, advances time, strips tags from output
- `copy_world_day()` called on all branch creation routes (inherits parent's world day)
- Thread-safe: per-branch `threading.Lock` for `advance_world_day()`
- Dungeon helpers: `advance_dungeon_enter()` (+3 days), `advance_dungeon_exit()` (+1 day)
- Frontend: `updateWorldDayDisplay()` shows `✦ 世界第 N 天·時段` in header
- 5 time periods: 深夜(0-6h), 清晨(6-9h), 上午(9-12h), 下午(12-18h), 夜晚(18-24h)
- API: `world_day` field returned in `/api/messages` and `/api/status` responses

## System Prompt Placeholders
The `system_prompt.txt` template uses these placeholders:
- `{character_state}` — Current character state JSON
- `{story_summary}` — Story summary text
- `{narrative_recap}` — Rolling narrative recap from conversation compaction
- `{world_lore}` — Lore TOC (compact, not full content)
- `{npc_profiles}` — Formatted NPC profiles

## LLM Provider System
Switch provider via drawer UI (⚙️ 設定) or by editing `llm_config.json` (auto-reloads on file change, no server restart needed).

**API endpoints**: `GET /api/config` (sanitized, no keys), `POST /api/config` (update provider/model)

**Config format** (`llm_config.json`):
```json
{
  "provider": "gemini",
  "gemini": {
    "api_keys": [
      {"key": "AIza...", "tier": "free"},
      {"key": "AIza...", "tier": "paid"}
    ],
    "model": "gemini-2.5-flash"
  },
  "claude_cli": { "model": "claude-sonnet-4-5-20250929" }
}
```
Backward-compatible: old `"api_key": "string"` format auto-converts to single-element list.

**Multi-key Gemini fallback** (`gemini_key_manager.py`):
- On HTTP 429 (rate limit), 400 (expired key), 401, 403 → mark key with 60s cooldown, try next
- Key priority: free keys first, paid keys last
- All `gemini_bridge.py` functions accept `gemini_cfg` dict (not raw `api_key`)

| Provider | Config | Streaming | Cost |
|----------|--------|-----------|------|
| `gemini` | `gemini.api_keys[]`, `gemini.model` | SSE via `streamGenerateContent` | Free tier / pay-per-token |
| `claude_cli` | `claude_cli.model` | NDJSON via `--output-format stream-json` | Included in Claude subscription |

- `llm_bridge.py` exports `call_claude_gm`, `call_claude_gm_stream`, `generate_story_summary`, `call_oneshot`
- `app.py` and `npc_evolution.py` import from `llm_bridge` (never directly from provider bridges)
- Adding a new provider: create `<provider>_bridge.py`, add dispatch branch in `llm_bridge.py`

## Usage Tracking
- `usage_db.py` — Per-story SQLite (`data/stories/<story_id>/usage.db`), WAL mode
- Thread-local usage propagation: `gemini_bridge._tls` → `llm_bridge._tls` (enriched with provider/model)
- `llm_bridge.get_last_usage()` — read after any non-streaming LLM call
- Streaming: usage passed via `"usage"` key in `"done"` payload dict
- `app.py._log_llm_usage()` — logs usage in Flask routes (send/edit/regen/lore_chat/summary)
- `usage_db.log_from_bridge()` — convenience one-liner for background callers (compaction, NPC evolution, auto-play, auto-summary, lore organizer)
- Call types: `gm`, `gm_stream`, `oneshot`, `compaction`, `npc_evolution`, `auto_play_player`, `auto_play_chargen`, `auto_summary`, `lore_organize`, `lore_chat`, `summary`
- API: `GET /api/usage?story_id=...&days=7` (per-story), `GET /api/usage?all=true` (cross-story)
- Claude CLI calls log with `null` tokens (no usage data available)

## Important Patterns
- JSON keys become strings when serialized — use `String(index)` in JS for lookups
- `call_claude_gm()` / `call_claude_gm_stream()` are provider-agnostic (routed via `llm_bridge`)
- All 3 GM routes (send/edit/regen) use `_process_gm_response()` + `_build_augmented_message()`
- CJK search uses bigram (2-char) + trigram (3-char) keyword scoring, not FTS5 tokenizer
- System prompt uses double-braces `{{}}` for literal braces (Python `.format()`)

## Testing
- **Run**: `python3 -m pytest tests/ -q`
- **Framework**: pytest, `tests/` directory, shared fixtures in `conftest.py`
- **Isolation**: All tests use `tmp_path` — no production data touched. LLM calls mocked via `unittest.mock.patch`.
- **Full plan**: See `doc/TESTING_PLAN.md` for detailed coverage map and TBD items.

### What to test for new features
- **New tag type or regex**: Add cases in `test_tag_extraction.py` — valid extraction, malformed input, tag stripping from clean text
- **New DB module (SQLite)**: CRUD operations, search with CJK bigrams, empty/edge inputs, branch filtering
- **State update logic**: Normal case, LLM quirks (string-instead-of-list, wrong types), delta with negative values, field that doesn't exist
- **Branch tree changes**: Linear chain, forked chain, blank branches, **circular parent references** (must not hang), **missing parent** (must not crash)
- **New API route**: Response shape matches what frontend expects, error cases (missing params, 404), auth/validation
- **Async extraction changes**: Mock `call_oneshot`, verify dedup logic, test with ≥200 char CJK text (shorter text is skipped)
- **Context injection changes**: Verify section presence and format in augmented message

### Testing patterns
- Monkeypatch module-level constants: `STORIES_DIR`, `BASE_DIR`, `DATA_DIR`, `_LLM_CONFIG_PATH`
- `_SyncThread` subclass to run background threads synchronously (see `test_extract_tags_async.py`)
- Use `conftest.py:story_dir` fixture for per-story directory setup with all required files

## Development Guidelines
- **Production runs from `/Users/eddylai/story-prod`** (a git worktree on port 5051). NEVER edit files there directly.
- **NEVER edit source files in `/Users/eddylai/story` (main repo) directly.** This repo is for PR management and creating worktrees only. All code changes MUST happen in a feature worktree. There is a pre-commit hook on `main` that blocks direct commits as a safety net.
- **Use `claude_cli` provider for testing.** Gemini free-tier keys are shared with production and rate-limited. Set `"provider": "claude_cli"` in your local `llm_config.json` to avoid burning Gemini quota.

### Git Workflow
- **Default branch**: `main`
- **Always use git worktree.** Create a new branch based on `main` in a worktree for every task. Never work directly on `main`. This rule has been violated 5+ times — zero tolerance.
  ```bash
  git worktree add ../story-<branch-name> -b <branch-name> main
  ```
- **Test before merging.** Run `python3 -m pytest tests/ -q` and include results in the PR. For e2e testing, use auto-play (`python auto_play.py`) or create a test branch in the web UI.
- **Open a PR** when the work is ready for review.
- **PR Review process**: Spawn 4 subagents (BE, FE, UI/UX, Game Director) to review the PR and leave comments via `gh pr review` / `gh pr comment`.
  - If any reviewer leaves actionable comments, address them and reply on the same comment thread.
  - Repeat until all reviewers have no remaining issues.
- **E2E testing (user-gated).** After all reviews pass, set up e2e testing for the user:
  1. Copy production data and config into the worktree (never symlink — avoids data pollution):
     ```bash
     cp -r /Users/eddylai/story-prod/data ../story-<branch-name>/data
     cp /Users/eddylai/story-prod/llm_config.json ../story-<branch-name>/llm_config.json
     ```
  2. Find a random available port and start the server:
     ```bash
     PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()") && echo "Test server on port $PORT" && cd ../story-<branch-name> && PORT=$PORT python app.py
     ```
  3. Tell the user the URL (`http://localhost:<port>`) and ask them to test.
  **Do NOT merge until the user explicitly confirms the PR is ready to merge.**
- **Merge process**: Once the user confirms e2e testing passed:
  1. Bump `VERSION` and add a new section to `CHANGELOG.md` (see Versioning & Changelog below).
  2. Commit the version bump, then rebase onto `main` and merge:
  ```bash
  git rebase main
  gh pr merge <pr-number> --rebase --delete-branch
  git worktree remove ../story-<branch-name>
  ```
  3. **Ask the user before deploying.** Deploy restarts the production server (brief downtime). Only run after explicit user confirmation:
  ```bash
  /Users/eddylai/story/deploy.sh
  ```
  **Never merge without user confirmation and version bump. Never deploy without user confirmation.**

## Versioning & Changelog
- **Version file**: `VERSION` (single source of truth, read by `app.py` as `__version__`)
- **Changelog**: `CHANGELOG.md` — [Keep a Changelog](https://keepachangelog.com/) format, [Semantic Versioning](https://semver.org/)
- **API**: `GET /api/config` returns `version` field
- **Bump workflow**: When releasing a new version:
  1. Update `VERSION` with the new version number
  2. Add a new `## [x.y.z] - YYYY-MM-DD` section to `CHANGELOG.md`
  3. Each entry should link to its PR: `([#N])` with reference-style links at the bottom
  4. Categories: `Added`, `Changed`, `Fixed`, `Removed`
  5. Tag the commit: `git tag vX.Y.Z`

## Code Style
- Python: standard Flask patterns, all helpers take `story_id`
- JS: vanilla, no framework, no build step
- CSS: dark theme, CSS variables for theming
- Prefer editing existing files over creating new ones

## Pending Feature PRs (Multi-Agent Shared Universe)
The multi-agent system is being implemented across 4 PRs. PR #14 (World Timer) is merged. The remaining 3 PRs are open and reviewed:

| PR | Branch | Status | Depends On |
|----|--------|--------|------------|
| #14 | ~~feature/world-timer~~ | **Merged** | — |
| #15 | `feature/multi-agent-backend` | Open (reviewed) | PR #14 |
| #16 | `feature/auto-play-agent` | Open (reviewed) | PR #15 |
| #17 | `feature/agent-ui` | Open (reviewed) | PR #15 |

**PR #15: Agent Manager + Shared World Backend** (~700 lines)
- New files: `agent_manager.py`, `shared_world.py`, `prompts.py`
- Agent lifecycle (create/start/pause/stop/delete), per-story `agents.json`
- Snapshot-based cross-agent awareness (`agent_snapshots.json`)
- LLM character generation: `POST /api/agents/generate-character`
- API routes: `/api/agents`, `/api/agents/<id>/<action>`, `/api/leaderboard`

**PR #16: Auto-Play Agent Integration** (~200 lines)
- `auto_play.py` modifications for agent-managed runs
- Cooperative stop via `agents.json` status check
- World timer integration (dungeon enter/exit advances day)
- Snapshot saving on phase changes and every 20 turns

**PR #17: Agent Frontend UI + LLM Character Builder** (~500 lines)
- Agent panel in drawer with status badges and controls
- Agent creation modal with LLM character generation flow
- Leaderboard panel, auto-polling when agents running
- XSS-safe rendering with `escapeHtml()`

PR #16 and #17 are independent of each other (both depend on #15 only). Merge order: #15 → (#16, #17 in any order).
