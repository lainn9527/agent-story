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
