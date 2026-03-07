# Scripts Layout

`scripts/` is grouped by purpose to keep the repo root and script list manageable.

- `scripts/backfill/`: one-off historical data backfills
- `scripts/deploy/`: deployment helpers for Mac production and WSL2
- `scripts/dev/`: local developer utilities such as prod-data sync and E2E server startup
- `scripts/lore/`: lore cleanup and merge tooling
- `scripts/migrations/`: one-time data migrations
- `scripts/state/`: state cleanup and repair helpers

Run scripts from the repo root unless the script says otherwise.
