# Changelog

All notable changes to the Story RPG project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.12.2] - 2026-02-10

### Changed
- Lore TOC in system prompt rendered as hierarchical indented tree instead of flat list ([#38])
- Dropped `[tags]` from TOC output — tags are for search scoring, not LLM context ([#38])
- TOC size reduced from ~21K to ~11K chars (~47% token savings) ([#38])

### Fixed
- Handle non-string lore content in `rebuild_index` (dirty data guard) ([#38])

[#38]: https://github.com/lainn9527/agent-story/pull/38

## [0.12.1] - 2026-02-10

### Fixed
- Mobile header: flex-wrap two-row layout, short title "無限輪迴" on mobile, ellipsis truncation ([#53])
- Mobile branch indicator: strip `branch_` prefix for display, widen to 120px, tooltip shows full ID ([#53])
- Mobile GM regen button: 4px left offset to avoid flush-edge placement ([#53])

[#53]: https://github.com/lainn9527/agent-story/pull/53

## [0.12.0] - 2026-02-10

### Added
- LLM branch titles: background `_extract_tags_async()` generates 4-8 char Chinese action summaries for each branch ([#44])
- Game save system: 5 API routes for snapshot/restore game state (character, NPCs, world day, recap) ([#44])
- Dark-themed custom modals: replaced all native `alert()`, `prompt()`, `confirm()` with styled modals ([#44])
- Auto-polling for branch title updates in drawer after send/edit/regen ([#44])
- Backfill script `scripts/backfill_branch_titles.py` for existing branches ([#44])

### Fixed
- Save/load world_day format: use `set_world_day()` instead of raw JSON write to prevent `AttributeError` crash ([#44])
- Frontend save card world_day display to match `updateWorldDayDisplay()` logic ([#44])
- Saves list API strips snapshot data to reduce payload size ([#44])

[#44]: https://github.com/lainn9527/agent-story/pull/44

## [0.11.0] - 2026-02-09

### Added
- Clickable GM options: tap to append action to input, supports multi-select with newline separator ([#47])
- Always-visible message index labels (no hover required) ([#47])
- Bug report button on each message, saves to per-story `bug_reports.json` with auto-cap at 500 ([#47])
- Sibling prune button to batch-delete variant branches ([#47])
- Toast notification system (`showToast()`) for prune and bug report feedback ([#47])
- `scripts/clean_state.py` migration script to remove `*_delta` / `*_add` garbage from character state ([#47])
- Mobile tap-to-reveal edit/regen controls via `touchstart` handler ([#47])

### Changed
- Time estimation prompt enriched with RPG scenario references (combat, exploration, travel durations) ([#47])
- Reward hint dedup: system prompt instruction as root cause fix, regex as safety net ([#47])
- Removed +30min time fallback — time advances only via regex TIME tags or LLM estimation ([#47])

### Fixed
- Orphan branch cleanup: `finally` blocks in edit/regen stream generators detect client disconnect ([#47])
- Filter empty branches in `_get_sibling_groups()` to prevent broken sibling switcher ([#47])
- Character state delta garbage: generic `*_delta` / `*_add` suffix handler prevents accumulation ([#47])
- Mobile CSS: `text-indent: 0` on GM options, touch target spacing for report button ([#47])

## [0.10.1] - 2026-02-09

### Fixed
- Fix branch fork (edit/regen) using parent's current world_day instead of value at branch point ([#49])
- Fix `npcs_snapshot` only saved on NPC-tag messages, causing NPC loss on fork ([#49])

### Added
- `world_day_snapshot` saved on every GM message for accurate fork restoration ([#49])
- `scripts/backfill_snapshots.py` to patch historical messages with missing snapshots ([#49])

## [0.10.0] - 2026-02-09

### Added
- Token/cost tracking via SQLite `usage_db` with per-story isolation ([#46])
- Thread-local usage metadata propagation from Gemini API responses ([#46])
- `GET /api/usage` endpoint with per-story and cross-story aggregation ([#46])
- `usage_db.log_from_bridge()` convenience helper for background callers ([#46])
- Usage tracking for `generate_story_summary` LLM calls ([#46])
- WAL mode for concurrent SQLite reads/writes in usage DB ([#46])

## [0.9.0] - 2026-02-09

### Added
- Lore topic organization system for orphan classification ([#41])
- TIME extraction in `_extract_tags_async` for reliable world timer tracking ([#39])
- Force `claude_cli` for background/script LLM calls to preserve Gemini quota ([#41])

### Fixed
- Fix Gemini SSE stream hanging forever on server-side stall ([#42])
- Fix event injection feedback loop causing repeated GM reward notifications ([#39])
- Fix `float(None)` crash when LLM returns null time values ([#39])
- Cap extracted time advance at 30 days per GM response ([#39])
- Fix orphaned branches from crashed/interrupted edit/regen operations ([#36])
- Reparent children on branch delete instead of cascade deleting ([#29])
- Scope similarity guard to same category to prevent cross-category lore merges ([#34])
- Fix false positive merge: require ≥2 shared bigrams in similarity guard ([#34])

## [0.8.0] - 2026-02-09

### Added
- Lore semantic merge script and similarity guard for insertion ([#34])
- Google Search grounding for lore chat (Gemini provider) ([#35])
- Lore source provenance with deep-link to original message ([#25], [#27])
- Lore cleanup script and fix extraction prompt to prevent duplication ([#28])
- Auto-cleanup failed branches + sibling switcher delete button ([#26])

### Changed
- Improve lore page UX: batch delete, markdown chat, streaming controls ([#30])
- Move checkbox next to edit pencil, dim to match dark background ([#31])

### Fixed
- Abort split on chunk failure to prevent partial data loss ([#28])
- Fix Gemini streaming truncation: raise maxOutputTokens to 65536 and detect MAX_TOKENS ([#28])
- Fix PUT `/api/lore/entry` dropping source provenance on update ([#25])
- Fix lore batch delete: add loading state + stop button aria-label ([#31])

## [0.7.0] - 2026-02-08

### Added
- Lore console page with CRUD and LLM chat ([#22])
- Sub-groups, alpha sort, and toggle-all collapse to lore console ([#22])
- `current_phase` field + sync STATE tag for reliable scene tracking ([#24])

### Fixed
- Fix dead retry button: use event delegation for proposal accept ([#22])
- Fix batch delete: check `res.ok` for server-side errors ([#23])
- UX fixes: loading indicator, scroll position, paragraph spacing ([#25])

## [0.6.0] - 2026-02-08

### Added
- Branch tree modal with merge/batch-delete + UX improvements ([#23])
- Cmd+B hotkey for branch tree modal ([#23])
- Collapse auto-play branches into single group + cache-busting ([#23])

### Fixed
- Fix branch tree depth: sibling detection + linear chain collapse ([#21])
- Fix branch list overflow: max-height 500px was clipping branches ([#23])

## [0.5.0] - 2026-02-08

### Added
- World timer system with day/night display ([#14])
- Branch indicator in header ([#20])

### Fixed
- Fix branch switch flicker and scroll hijack during streaming ([#19])

## [0.4.0] - 2026-02-08

### Added
- Multi-key Gemini fallback and UI provider/model switcher ([#9])
- Mobile novel reader style for better reading experience ([#10])

### Fixed
- Fix Player AI context: add narrative recap, use full recent messages ([#12])
- Fix key fallback error handling and remove dead code in state update ([#13])

## [0.3.0] - 2026-02-08

### Added
- Summary timeline modal ([#5])

### Fixed
- Fix branch merge overwriting parent messages ([#7])
- Fix character name hallucination in summaries ([#8])

## [0.2.0] - 2026-02-08

### Added
- Stateless LLM calls, conversation compaction, and async tag extraction ([#1])

### Fixed
- Add compaction triggers to edit/regen routes, fix logging ([#3])
- Fix timeline ref, state double-apply, JSON fallback ([#3])

## [0.1.0] - 2026-02-08

### Added
- Initial Story RPG project with Flask backend + vanilla JS frontend
- Branching timeline system with edit/regen/sibling switcher
- Auto-play AI self-play mode (GM + Player AI)
- Live view with incremental polling for auto-play branches
- Auto-play summary dashboard with periodic LLM summaries
- NPC system with Big5 personality profiles
- Event tracing system (伏筆/轉折/戰鬥 etc.)
- Image generation via Pollinations.ai
- World lore system with CJK bigram search
- Multi-story support with per-story data isolation
- Hidden tag pipeline (STATE/LORE/NPC/EVENT/IMG)
- Context injection (lore + events + NPC activities)
- Dark theme UI with slide-out drawer

<!-- PR links -->
[#1]: https://github.com/lainn9527/agent-story/pull/1
[#3]: https://github.com/lainn9527/agent-story/pull/3
[#5]: https://github.com/lainn9527/agent-story/pull/5
[#7]: https://github.com/lainn9527/agent-story/pull/7
[#8]: https://github.com/lainn9527/agent-story/pull/8
[#9]: https://github.com/lainn9527/agent-story/pull/9
[#10]: https://github.com/lainn9527/agent-story/pull/10
[#12]: https://github.com/lainn9527/agent-story/pull/12
[#13]: https://github.com/lainn9527/agent-story/pull/13
[#14]: https://github.com/lainn9527/agent-story/pull/14
[#19]: https://github.com/lainn9527/agent-story/pull/19
[#20]: https://github.com/lainn9527/agent-story/pull/20
[#21]: https://github.com/lainn9527/agent-story/pull/21
[#22]: https://github.com/lainn9527/agent-story/pull/22
[#23]: https://github.com/lainn9527/agent-story/pull/23
[#24]: https://github.com/lainn9527/agent-story/pull/24
[#25]: https://github.com/lainn9527/agent-story/pull/25
[#26]: https://github.com/lainn9527/agent-story/pull/26
[#27]: https://github.com/lainn9527/agent-story/pull/27
[#28]: https://github.com/lainn9527/agent-story/pull/28
[#29]: https://github.com/lainn9527/agent-story/pull/29
[#30]: https://github.com/lainn9527/agent-story/pull/30
[#31]: https://github.com/lainn9527/agent-story/pull/31
[#34]: https://github.com/lainn9527/agent-story/pull/34
[#35]: https://github.com/lainn9527/agent-story/pull/35
[#36]: https://github.com/lainn9527/agent-story/pull/36
[#39]: https://github.com/lainn9527/agent-story/pull/39
[#41]: https://github.com/lainn9527/agent-story/pull/41
[#42]: https://github.com/lainn9527/agent-story/pull/42
[#46]: https://github.com/lainn9527/agent-story/pull/46
[#47]: https://github.com/lainn9527/agent-story/pull/47
[#49]: https://github.com/lainn9527/agent-story/pull/49
