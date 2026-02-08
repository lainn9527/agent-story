# Story RPG Project

## Overview
Interactive story RPG with branching timelines, multi-story support, and rich narrative systems. Flask backend + vanilla JS frontend, using Claude CLI as the GM (Game Master).

## Architecture
- **Backend**: Flask on port 5051 (`app.py`)
- **AI Bridge**: Multi-provider LLM bridge (`llm_bridge.py`) — dispatches to Gemini API or Claude CLI
- **Frontend**: Vanilla HTML/CSS/JS, light theme, single-page app with slide-out drawer
- **Data**: Per-story isolation in `data/stories/<story_id>/`, SQLite for search indexes

## Key Commands
- Run server: `python app.py` (port 5051)

## Key Files
| File | Purpose |
|------|---------|
| `app.py` | Flask routes, multi-story support, unified tag pipeline |
| `llm_bridge.py` | LLM provider dispatcher, auto-reloads `llm_config.json` |
| `llm_config.json` | Provider config: switch `"provider"` between `"gemini"` / `"claude_cli"` |
| `gemini_bridge.py` | Gemini API bridge (streaming + non-streaming) |
| `claude_bridge.py` | Claude CLI bridge, `--output-format json` for session_id |
| `lore_db.py` | SQLite lore search engine (CJK bigram scoring) |
| `event_db.py` | SQLite event tracing engine (CJK bigram search) |
| `image_gen.py` | Pollinations.ai async image generation |
| `npc_evolution.py` | Background NPC evolution via Claude CLI |
| `auto_play.py` | AI self-play: GM + Player AI loop on dedicated `auto_` branch |
| `static/app.js` | Frontend: drawer UI, NPC/events/images, schema-driven panels |
| `static/style.css` | Light theme CSS |
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
    npc_activities_<branch_id>.json     # Background NPC activity logs
    auto_play_state.json                # Auto-play progress (auto_ branches only)
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

## System Prompt Placeholders
The `system_prompt.txt` template uses these placeholders:
- `{character_state}` — Current character state JSON
- `{story_summary}` — Story summary text
- `{world_lore}` — Lore TOC (compact, not full content)
- `{npc_profiles}` — Formatted NPC profiles

## LLM Provider System
Switch provider by editing `llm_config.json` (auto-reloads on file change, no server restart needed):
```json
{"provider": "gemini"}      // Gemini API (free tier available)
{"provider": "claude_cli"}  // Claude CLI via subprocess (uses Claude subscription)
```

| Provider | Config | Streaming | Session Resume | Cost |
|----------|--------|-----------|---------------|------|
| `gemini` | `gemini.api_key`, `gemini.model` | SSE via `streamGenerateContent` | No (sends history each call) | Free tier / pay-per-token |
| `claude_cli` | `claude_cli.model` | NDJSON via `--output-format stream-json` | Yes (`--resume session_id`) | Included in Claude subscription |

- `llm_bridge.py` exports `call_claude_gm`, `call_claude_gm_stream`, `generate_story_summary`, `call_oneshot`
- `app.py` and `npc_evolution.py` import from `llm_bridge` (never directly from provider bridges)
- Adding a new provider: create `<provider>_bridge.py`, add dispatch branch in `llm_bridge.py`

## Important Patterns
- JSON keys become strings when serialized — use `String(index)` in JS for lookups
- `call_claude_gm()` / `call_claude_gm_stream()` are provider-agnostic (routed via `llm_bridge`)
- All 3 GM routes (send/edit/regen) use `_process_gm_response()` + `_build_augmented_message()`
- CJK search uses bigram (2-char) + trigram (3-char) keyword scoring, not FTS5 tokenizer
- System prompt uses double-braces `{{}}` for literal braces (Python `.format()`)

## Code Style
- Python: standard Flask patterns, all helpers take `story_id`
- JS: vanilla, no framework, no build step
- CSS: light theme, CSS variables for theming
- Prefer editing existing files over creating new ones
