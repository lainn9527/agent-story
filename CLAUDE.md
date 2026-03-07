# Claude Agent Entry

This repository uses a **single onboarding entrypoint** for all agents.

## Start Here

1. Read `AGENTS.md` first.
2. Then follow `doc/readme.md` for full docs navigation.

## Quick Layout

- `app.py`: Flask entrypoint
- `story_core/`: primary internal backend package
- `auto_play.py`: CLI wrapper for `story_core/auto_play.py`
- `routes/`: Flask blueprints
- `static/`: frontend
- `scripts/`: grouped operational utilities

If you are looking for server internals, check `story_core/` first rather than the repo root.

## Canonical Docs

- `doc/architecture.md`
- `doc/prompt_design.md`
- `doc/game_mechanics.md`
- `doc/api_reference.md`
- `doc/development.md`
- `doc/testing_plan.md`
- `doc/sync.md`
- `doc/wsl2_setup.md`

## Note

This file is intentionally minimal to avoid drift.
If any old guidance from previous versions conflicts with code or docs,
consider it deprecated and follow `AGENTS.md` + `doc/*`.
