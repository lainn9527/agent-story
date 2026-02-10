# Changelog

All notable changes to the Story RPG project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
