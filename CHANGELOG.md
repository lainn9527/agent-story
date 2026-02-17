# Changelog

All notable changes to the Story RPG project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.20.1] - 2026-02-18

### Added
- **Lore helper subcategory field**: 新增/編輯 modal 加入「子分類」欄位，支援 `副本世界觀/副本名/介紹`、`體系/體系名/介紹` 等層級結構 ([#103])
- **Lore chat subcategory support**: AI 提案格式加入 `subcategory` 欄位；系統提示規範 副本世界觀/體系/場景 的命名慣例 ([#103])

### Fixed
- **Topic 唯一性 scoped 化**: 建立/更新 lore 條目時，重複檢查範圍縮小為同一 `(subcategory, topic)` 組合，允許不同副本各有「介紹」條目 ([#103])
- **PUT subcategory 更新**: 編輯時可修改 subcategory；subcategory 變更時同樣觸發衝突檢查 ([#103])
- **Lore apply delete 精確化**: chat 提案 delete 操作改為 (subcategory, topic) 聯合識別，避免跨副本誤刪同名條目 ([#103])

[#103]: https://github.com/lainn9527/agent-story/pull/103

## [0.20.0] - 2026-02-18

### Changed
- **道具欄改為 key-value map**: `inventory` 從 list 改為 map 格式（`{"道具名": "狀態"}`），同名道具自動覆蓋，從根本上解決進化道具重複堆積問題 ([#102])
- **Schema render hint**: 新增 `"render": "inline"` schema 欄位，人際關係保持 `name：value` 單行顯示，道具欄使用 block 雙行佈局 ([#102])

### Added
- **Backward compat shim**: 舊版 `inventory_add`/`inventory_remove` STATE tag 自動轉換為 map delta 格式 ([#102])
- **Auto-migration on load**: 載入分支時自動偵測 list 格式 inventory 並無損轉換為 map（不合併同 base name 的不同道具） ([#102])
- **Map null removal**: `{"inventory": {"道具名": null}}` 可移除道具 ([#102])

[#102]: https://github.com/lainn9527/agent-story/pull/102

## [0.19.4] - 2026-02-18

### Fixed
- **輸入框送出後不縮回**: 送出訊息後 textarea 保持展開狀態不會縮回單行；同時修正 `fillInputWithOption()` 缺少 120px 高度上限 ([#101])

[#101]: https://github.com/lainn9527/agent-story/pull/101

## [0.19.3] - 2026-02-17

### Fixed
- **道具重複堆積**: `inventory_add` 時自動替換同 base name 的裸名舊道具（如 `武器` → `武器（強化版）`），有後綴的變體（如 `定界珠（生）` vs `定界珠（死）`）和消耗品堆疊不受影響 ([#100])
- **人際關係不更新**: 提取 prompt 缺少 map 類型欄位（relationships）的上下文，LLM 看不到現有關係故無法輸出更新；現已包含並加強更新指示 ([#100])

[#100]: https://github.com/lainn9527/agent-story/pull/100

## [0.19.2] - 2026-02-17

### Fixed
- **命運走向關閉後仍出現劫數**: Strip `**【命運走向：XX】**` labels from conversation history before sending to LLM when fate mode is off, so GM has zero exposure to fate patterns ([#99])

[#99]: https://github.com/lainn9527/agent-story/pull/99

## [0.19.1] - 2026-02-17

### Fixed
- **命運走向關閉後仍出現劫數**: GM mimicked fate terms from conversation history even when fate mode was off; added explicit instruction to ignore historical fate references ([#98])

[#98]: https://github.com/lainn9527/agent-story/pull/98

## [0.19.0] - 2026-02-17

### Changed
- **命運走向系統 (Fate Direction System)**: Replaced binary success/failure dice with fate directions (天命/順遂/平淡/波折/劫數) following 塞翁失馬焉知非福 philosophy — good fortune may hide risks, setbacks may bring gains ([#97])
- **行動合理性 (Action Quality)**: Player RP quality now independently affects outcomes — detailed strategies increase success chance regardless of fate direction ([#97])

### Added
- **命運走向開關**: Fate system toggleable on/off per branch, like pistol mode; when off, system prompt fate section is stripped and no dice are rolled ([#97])
- **必勝模式連動**: 必勝模式 toggles are disabled (greyed out) when fate mode is off ([#97])

[#97]: https://github.com/lainn9527/agent-story/pull/97

## [0.18.0] - 2026-02-17

### Added
- **副本系統 (Dungeon System)**: 13 個副本完整定義（D→S 難度），含主線節點、地圖區域、成長規則 ([#94])
- **硬約束成長控制**: `validate_dungeon_progression()` 在代碼層面 cap 每個副本的等級/基因鎖成長，防止 GM 過度慷慨導致角色過快升級 ([#94])
- **副本進度追蹤**: 每個分支獨立的 `dungeon_progress.json`，記錄主線進度、地圖探索度、成長預算消耗 ([#94])
- **Drawer 副本面板**: 副本中顯示進度條、主線節點、可折疊地圖區域；60% 主線完成後可回歸主神空間 ([#94])
- **系統提示副本上下文**: `{dungeon_context}` 佔位符將副本狀態、節點進度、成長限制注入 GM 系統提示 ([#94])
- **異步 LLM 副本提取**: 擴展 `_extract_tags_async()` 自動從 GM 文本提取副本進度更新 ([#94])

[#94]: https://github.com/lainn9527/agent-story/pull/94

## [0.17.1] - 2026-02-17

### Fixed
- **Dice proportional consequences**: GM now scales failure severity proportional to action risk — low-risk actions (casual chat, simple interactions) only result in minor setbacks, not catastrophic relationship-breaking outcomes ([#96])

[#96]: https://github.com/lainn9527/agent-story/pull/96

## [0.17.0] - 2026-02-17

### Changed
- **Design files separated**: Story design files (`system_prompt.txt`, `world_lore.json`, `character_schema.json`, `default_character_state.json`, `parsed_conversation.json`, `nsfw_preferences.json`) now live in `story_design/<story_id>/` instead of `data/stories/<story_id>/`, enabling git tracking of world-building content while runtime data stays gitignored ([#95])
- Auto-migration on startup copies design files from old location to new; old copies become inert ([#95])

[#95]: https://github.com/lainn9527/agent-story/pull/95

## [0.16.5] - 2026-02-17

### Fixed
- **Inventory dedup**: removal now matches by base name (strips parenthetical status, quantity suffixes, dash descriptions) — fixes items like `大日金烏劍·空燼 (穩定度提升)` being unmatchable for removal ([#93])
- **Remove-before-add ordering**: paired `inventory_remove` + `inventory_add` updates now process removal first, preventing the new item from being nuked by base-name matching ([#93])
- **Garbage key filtering**: LLM intermediate instruction keys (`inventory_use`, `skill_update`, etc.) no longer leak into character state as top-level fields ([#93])

### Changed
- **Extraction prompt**: now includes current inventory/abilities list so LLM can properly pair `_remove` + `_add` for item status changes (root cause of duplicate sword entries) ([#93])
- **Lore extraction exclusion**: character-specific abilities/skills no longer extracted as world lore — redirected to character state instead ([#93])

### Added
- **Abilities schema field**: auto-migration adds `abilities` (功法與技能) list to `character_schema.json` and `default_character_state.json` on startup ([#93])

[#93]: https://github.com/lainn9527/agent-story/pull/93

## [0.16.4] - 2026-02-16

### Changed
- **Dice rebalance**: lower outcome thresholds (80/50/30 → 70/40/20) for a more balanced success curve ([#91])
- Expand attribute lookup keywords to match actual GM descriptions — physique (+8 keywords), spirit (+7), gene lock shorthand (`一階`~`四階`) ([#91])

### Added
- **Beginner bonus**: first 10 turns get linearly decaying dice bonus (+10 → +1), easing new players into the game ([#91])

[#91]: https://github.com/lainn9527/agent-story/pull/91

## [0.16.3] - 2026-02-16

### Removed
- **`story_summary` system** — removed entirely to fix cross-branch context leakage. Blank branch children (edit/regen forks) no longer inherit main-story summary. `narrative_recap` (per-branch, rolling) already covers the same ground. ([#92])
- Removed `generate_story_summary()` from all 3 LLM bridges (`llm_bridge.py`, `claude_bridge.py`, `gemini_bridge.py`) ([#92])
- Removed `has_summary` from `/api/init` and `/api/stories/switch` API responses ([#92])

[#92]: https://github.com/lainn9527/agent-story/pull/92

## [0.16.2] - 2026-02-16

### Added
- **Subcategory field** for hierarchical lore organization — entries now support optional `subcategory` for two-level grouping (e.g. `副本世界觀 > 生化危機`, `體系 > 技能`) ([#90])
- Frontend lore console renders category > subcategory tree with collapsible subgroups ([#90])
- `道具` added to allowed lore extraction categories ([#90])

### Changed
- `技能` and `基本屬性` are now subcategories under `體系` instead of top-level categories ([#90])
- Extraction prompt updated with subcategory guidance for `副本世界觀` (mandatory dungeon name) and `體系` (optional skill/attribute classification) ([#90])
- `lore_db.py`: SQLite schema migration adds `subcategory` column, all search/index/TOC functions updated ([#90])

### Fixed
- Subcategory preserved across all save/update/promote/apply code paths (previously silently dropped) ([#90])
- Branch lore search output now includes subcategory in formatted label, matching base lore search ([#90])
- `find_duplicates` and `lore_chat` system prompt now include subcategory in output/grouping ([#90])

[#90]: https://github.com/lainn9527/agent-story/pull/90

## [0.16.1] - 2026-02-16

### Changed
- Strengthen lore extraction prompt to explicitly exclude character-specific content (personal stats, inventory, combat experiences) and require generic language without character names ([#88])

[#88]: https://github.com/lainn9527/agent-story/pull/88

## [0.16.0] - 2026-02-16

### Added
- **Branch lore system**: Auto-extracted lore now saved to per-branch `branch_lore.json` instead of polluting shared `world_lore.json` ([#84])
- Branch lore search with CJK bigram matching, injected as `[相關分支設定]` in context ([#84])
- LLM-powered promotion workflow: review branch lore entries as promote/rewrite/reject, then promote to base lore ([#84])
- Lore console UI: "分支知識" section with teal badges, "審核提升" button for promotion flow ([#84])
- `DELETE /api/lore/branch/entry`, `POST /api/lore/promote`, `POST /api/lore/promote/review` API routes ([#84])
- `GET /api/lore/all` now returns entries with `layer: "base"|"branch"` field ([#84])
- 40 new tests for branch lore helpers, API routes, branch operations, and context injection ([#84])

### Changed
- Inline `<!--LORE-->` tags and `_extract_tags_async()` now write to branch lore instead of base lore ([#84])
- Branch fork/edit/regen/promote/merge operations copy or merge branch lore ([#84])
- Blank branches start with empty branch lore (no inheritance from parent) ([#84])

### Fixed
- Thread safety for concurrent branch lore writes via per-branch locks ([#84])
- Promote/merge uses upsert-merge semantics instead of overwriting target branch lore ([#84])

[#84]: https://github.com/lainn9527/agent-story/pull/84

## [0.15.0] - 2026-02-15

### Fixed
- Branch tree infinite loop on circular parent references in timeline_tree.json ([#82])
- Branch tree KeyError crash when parent branch is hard-deleted ([#82])
- Event dedup blocking status progression — events now update from planted→triggered→resolved instead of being silently skipped ([#82])
- Extraction prompt updated to instruct LLM to re-emit events with changed status ([#82])

### Added
- Comprehensive test suite: 273 tests across 13 files covering tag extraction, world timer, event DB, lore search, compaction, state updates, branch tree, context injection, async extraction, and API routes ([#82])
- `event_db.get_event_title_map()` for status-aware event dedup ([#82])

[#82]: https://github.com/lainn9527/agent-story/pull/82

## [0.14.9] - 2026-02-13

### Fixed
- Clear narrative recap for blank branches — GM no longer references previous storylines in fresh games ([#83])

[#83]: https://github.com/lainn9527/agent-story/pull/83

## [0.14.8] - 2026-02-13

### Fixed
- Fix `deploy.sh` fetching in wrong directory — `FETCH_HEAD` was stale, always one version behind ([#81])

[#81]: https://github.com/lainn9527/agent-story/pull/81

## [0.14.7] - 2026-02-13

### Fixed
- Fix dice system crash when LLM writes non-string character state values (e.g. `"spirit": 80`) ([#80])

[#80]: https://github.com/lainn9527/agent-story/pull/80

## [0.14.6] - 2026-02-13

### Fixed
- Fix Claude CLI nested-session crash when server is started from Claude Code session — strip `CLAUDECODE` env var from all subprocess calls ([#77])

### Added
- Rotating file logger (`server.log`, 5MB x 4 files) alongside console output ([#77])
- Redirect `deploy.sh` stderr to `server_stderr.log` instead of `/dev/null` ([#77])

[#77]: https://github.com/lainn9527/agent-story/pull/77

## [0.14.5] - 2026-02-13

### Added
- Production deploy workflow: `deploy.sh` for one-command deploy after merge ([#76])
- Production isolation: server runs from `story-prod` worktree, decoupled from main repo ([#76])
- Pre-commit hook blocks direct commits to `main` branch ([#76])

### Changed
- CLAUDE.md: enforce worktree-only development, update E2E data paths and merge process ([#76])

[#76]: https://github.com/lainn9527/agent-story/pull/76

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
