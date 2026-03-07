# Agent Onboarding Guide

This is the unified entrypoint for all agents (including Claude/Codex-style agents).

This repository has non-trivial prompt + gameplay logic.
Before making changes, read these files in order:

1. `doc/readme.md`
2. `doc/architecture.md`
3. `doc/prompt_design.md` (if touching LLM/prompt/tag flow)
4. `doc/game_mechanics.md` (if touching gameplay/state/branch/dungeon logic)
5. `doc/api_reference.md` (if touching routes/frontend integration)
6. `doc/development.md` (tests/scripts/runbook)

## Working Rules

- Source of truth is code. If docs and code differ, follow code and update docs in the same change.
- For API changes, update both:
  - `doc/api_reference.md`
  - frontend calls in `static/app.js` (if needed)
- For prompt/tag/state changes, update both:
  - `doc/prompt_design.md`
  - related tests under `tests/`
- For mechanics/rules changes, update both:
  - `doc/game_mechanics.md`
  - related tests under `tests/`

## Minimum Validation

Run the narrowest relevant tests first, then broader ones as needed.
Typical baseline:

```bash
pytest tests/test_api_routes.py
pytest tests/test_state_update.py tests/test_extract_tags_async.py
```

For larger backend refactors or import/layout changes, run:

```bash
pytest
```

## Current Layout

- `app.py`
  - Flask bootstrap / import surface for routes and tests.
  - Keep `python app.py` working.
- `story_core/`
  - Main internal backend package.
  - Most former root-level Python modules now live here (`llm_bridge`, `state_db`, `prompts`, `story_io`, etc.).
- `auto_play.py`
  - Thin CLI compatibility wrapper.
  - Real implementation lives in `story_core/auto_play.py`.
- `routes/`
  - Flask blueprints only; shared logic should usually live in `story_core/`.
- `static/`
  - Frontend JS/CSS.
- `scripts/`
  - Operational utilities grouped by domain:
    - `scripts/backfill/`
    - `scripts/deploy/`
    - `scripts/dev/`
    - `scripts/lore/`
    - `scripts/migrations/`
    - `scripts/state/`

## Navigation Notes

- If you are looking for backend implementation, start in `story_core/` before assuming logic is still in repo root.
- Preserve stable entrypoints and operator workflows:
  - `python app.py`
  - `python auto_play.py`
- New reusable backend modules should generally go under `story_core/`, not the repo root.
- New utility scripts should generally go under the appropriate `scripts/<domain>/` directory.
