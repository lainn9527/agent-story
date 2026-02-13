# Changelog

All notable changes to the Story RPG project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.14.4] - 2026-02-12

### Removed
- Claude CLI tool call (`--allowedTools Read,Grep`) — redundant with critical facts injection ([#75])

[#75]: https://github.com/lainn9527/agent-story/pull/75

## [0.14.3] - 2026-02-12

### Fixed
- Fix character-by-character split in list fields when LLM returns string instead of array ([#72])
- Fix missing spacing between label and value in character status panel ([#72])
- Block scene-transient keys (location, threat_level, etc.) and non-schema `_add`/`_remove` keys from persisting in character state ([#72])
- Filter system keys (world_day, world_time, branch_title) from character state ([#72])

### Added
- Collapsible "其他狀態" section for extra fields in character panel, auto-opens when new fields appear ([#72])
- Client-side NPC key filtering (dynamically derived from npcs.json) hides NPC sub-state from player view ([#72])
- Key name humanization: snake_case → Title Case, CJK keys shown as-is ([#72])
- Load-time self-healing: auto-strips `_delta`/`_add`/`_remove` artifacts and single-char list entries ([#72])
- Polling for async tag extraction updates: status panel refreshes within 5-30s after GM response ([#72])

[#72]: https://github.com/lainn9527/agent-story/pull/72

## [0.14.2] - 2026-02-12

### Added
- Addon panel (⚙) next to send button with quick-access model selection, dice cheat toggle, and pistol mode ([#73])
- Pistol mode (手槍模式): per-branch toggle that injects intimate scene instructions into system prompt ([#73])
- Pistol preferences modal with 134 quick-select chips across 8 categories (風格/體位/前戲/高潮/道具/場景/描寫重點/角色動態) ([#73])
- Custom chip support: add/delete user-defined chips per category, persisted in JSON ([#73])
- Frequency-based chip sorting: commonly used chips rise to top after every 3 uses ([#73])
- Structured preference injection: chips formatted by category in system prompt for better LLM comprehension ([#73])
- LLM pacing instructions: mental roadmap (前戲→升溫→高潮→餘韻), 1-3 elements per reply, 1500+ char minimum ([#73])
- Combined provider+model tree-style dropdown with optgroup in addon panel ([#73])
- Pink header badge for pistol mode, green glow on addon button when any addon active ([#73])
- Per-story NSFW preferences stored as `nsfw_preferences.json` ([#73])

[#73]: https://github.com/lainn9527/agent-story/pull/73

## [0.14.1] - 2026-02-12

### Added
- Critical facts injection into GM system prompt: current phase, world time, gene lock, reward points, key inventory, NPC relationship matrix ([#74])
- Claude CLI tool access (`--allowedTools Read,Grep`) for GM fact-checking against game data files ([#74])
- `_rel_to_str()` helper for normalizing dict-type relationship values ([#74])
- `_classify_npc()` with combined signal classification (dead > hostile > captured > ally > neutral) ([#74])

### Fixed
- Dict-type relationship values in `character_state.json` no longer crash NPC classification ([#74])
- Float-type `world_day` values handled correctly in critical facts ([#74])
- Reward points format safely handles non-numeric values ([#74])

[#74]: https://github.com/lainn9527/agent-story/pull/74

## [0.14.0] - 2026-02-12

### Added
- Embedding-based hybrid lore search with RRF (Reciprocal Rank Fusion) ranking ([#61])
- Local embedding model (`jinaai/jina-embeddings-v2-base-zh`) via fastembed — zero API calls, ~11ms per query ([#61])
- Token-budgeted lore injection (~3000 tokens) instead of fixed top-5 ([#61])
- Location pinning: category boosting based on game phase (副本/主神空間/戰鬥) ([#61])
- Duplicate lore detection endpoint `GET /api/lore/duplicates` ([#61])
- Embedding stats endpoint `GET /api/lore/embedding-stats` ([#61])

### Changed
- System prompt lore section: replaced ~6-8K token TOC with compact category summary (~50 tokens) ([#61])
- Auto-play defaults to `claude_cli` provider with zero Gemini usage ([#61])
- Gemini access blocked at `llm_bridge` level when provider is overridden (single gate) ([#61])

### Removed
- Gemini embedding API code from `gemini_bridge.py` (replaced by local model) ([#61])

[#61]: https://github.com/lainn9527/agent-story/pull/61

## [0.13.7] - 2026-02-12

### Fixed
- Cheat settings (金手指) and branch config lost on edit/regen due to `_resolve_sibling_parent` overwriting source branch ([#71])

### Changed
- Mobile touch UX: larger touch targets (44px min), haptic feedback on buttons, active-state visual feedback ([#71])

[#71]: https://github.com/lainn9527/agent-story/pull/71

## [0.13.6] - 2026-02-12

### Added
- Gemini 3 Flash Preview (`gemini-3-flash-preview`) added to model selector ([#70])

[#70]: https://github.com/lainn9527/agent-story/pull/70

## [0.13.5] - 2026-02-12

### Fixed
- Gemini API transient network errors (e.g. "no route to host") now retry with 2s backoff instead of failing immediately ([#69])

[#69]: https://github.com/lainn9527/agent-story/pull/69

## [0.13.4] - 2026-02-11

### Fixed
- User-edited lore entries no longer overwritten by background auto-extraction (`_extract_tags_async`) ([#68])

[#68]: https://github.com/lainn9527/agent-story/pull/68

## [0.13.3] - 2026-02-10

### Added
- Branch tree "▶ 繼續" toolbar button: one-click jump to last-played branch ([#65])
- Branch tree "⇣" per-node button: jump to deepest descendant leaf of any branch ([#65])
- Backend tracks `last_played_branch_id` on send/edit/regen actions ([#65])

[#65]: https://github.com/lainn9527/agent-story/pull/65

## [0.13.2] - 2026-02-10

### Fixed
- Mobile GM messages: regen button and sibling switcher overlapping with text due to missing bottom padding ([#64])
- `UnboundLocalError: 'tree'` crash in `/api/send/stream` when no siblings were pruned ([#64])

[#64]: https://github.com/lainn9527/agent-story/pull/64

## [0.13.1] - 2026-02-10

### Added
- GM cheat mode: `/gm` prefix commands for direct GM communication and rule changes ([#37])
- Dice always-success toggle in drawer settings with 30/50/20 probability split ([#37])
- Header badge `金手指` when always-success mode is active ([#37])
- Per-branch cheat storage in `gm_cheats.json`, inherited on branch creation ([#37])
- Restore drawer branch list with root-only view: main + non-auto blank branches shown first, auto branches collapsed under "Auto-Play (N)" toggle ([#62])
- Branch tree modal is now contextual: shows only the subtree of the currently active root branch ([#62])

### Changed
- Increased mobile content padding for better readability ([#37])

### Fixed
- Story delete button losing hover-reveal styling due to CSS class rename ([#62])
- Escape during branch rename triggering a save instead of cancelling ([#62])
- Branch action buttons invisible on mobile touch devices ([#62])

[#37]: https://github.com/lainn9527/agent-story/pull/37
[#62]: https://github.com/lainn9527/agent-story/pull/62

## [0.13.0] - 2026-02-10

### Added
- Auto-prune abandoned sibling branches: silently marks siblings as pruned when player moves 5+ steps ahead and sibling has ≤2 delta messages ([#54])
- Heart protection toggle (♥) in branch tree modal and sibling switcher to exempt branches from auto-pruning ([#54])
- Promote (⬆) action in branch tree modal ([#54])
- Toast notification for auto-prune and scissors prune failures ([#54])

### Changed
- Drawer branch section stripped to 🌳 (branch tree) and ⊕ (new blank) buttons only — branch list removed in favor of branch tree modal ([#54])
- Branch tree: single-child chains flattened with dashed left border connector ([#54])
- Drawer toggle shortcut changed from Cmd+T to Cmd+Shift+B (avoids Chrome conflict) ([#54])
- Mobile: branch tree action buttons always visible (no hover required), increased touch targets ([#54])

### Removed
- Drawer branch list, promote button, and new-branch button (replaced by branch tree modal) ([#54])
- Delete-previous-version button from messages ([#54])

[#54]: https://github.com/lainn9527/agent-story/pull/54

## [0.12.4] - 2026-02-10

### Changed
- Git Workflow: enforce user-gated e2e testing before PR merge ([#59])
- Git Workflow: add e2e test setup steps (copy data, random port, start server) ([#59])
- Git Workflow: integrate version bump + changelog into merge checklist ([#59])

[#59]: https://github.com/lainn9527/agent-story/pull/59
