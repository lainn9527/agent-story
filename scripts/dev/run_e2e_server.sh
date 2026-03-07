#!/usr/bin/env bash
# Start the Flask app on a dedicated port for E2E tests.
# Usage: ./scripts/dev/run_e2e_server.sh
# Override port: E2E_PORT=5053 ./scripts/dev/run_e2e_server.sh
# Base URL is printed so Playwright or other runners can use it.

set -e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

E2E_PORT="${E2E_PORT:-5052}"
export PORT="$E2E_PORT"

echo "E2E server starting at http://localhost:$PORT"
exec python3 app.py
