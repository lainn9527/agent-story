#!/usr/bin/env bash
# Clone game data from story-prod into current repo for local testing.
# Usage: ./scripts/dev/clone_prod_data.sh
# Override source: PROD_DIR=/path/to/story-prod ./scripts/dev/clone_prod_data.sh

set -e
PROD_DIR="${PROD_DIR:-/Users/eddylai/story-prod}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [[ ! -d "$PROD_DIR" ]]; then
  echo "ERROR: PROD_DIR not found: $PROD_DIR"
  exit 1
fi

echo "==> Cloning from $PROD_DIR to $ROOT"

echo "  - data/"
rsync -a --delete "$PROD_DIR/data/" "$ROOT/data/"

echo "  - story_design/"
rsync -a "$PROD_DIR/story_design/" "$ROOT/story_design/"

echo "==> Done. Optional: copy llm_config.json manually if you need prod API keys:"
echo "    cp $PROD_DIR/llm_config.json $ROOT/llm_config.json"
