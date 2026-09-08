#!/usr/bin/env bash
# Detached dev server on :5056 — survives sleep / terminal-disconnect (unlike the preview-managed
# server). NOT a boot service: after a reboot, run this again. Crash ≠ auto-restart.
# Usage:  bash scripts/dev/run_dev_server.sh          (start, or report already-running)
#         bash scripts/dev/run_dev_server.sh restart  (kill + start fresh — use after code edits)
set -euo pipefail
cd "$(dirname "$0")/../.."

PORT=5056
PY=/usr/local/bin/python3            # the interpreter that has flask (see .claude/launch.json)
LOG=/tmp/story_dev_${PORT}.log

if [ "${1:-}" = "restart" ]; then
  lsof -ti:"$PORT" 2>/dev/null | xargs kill 2>/dev/null || true
  sleep 1
fi

if lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "already running on :$PORT (use 'restart' to reload code). log: $LOG"
  exit 0
fi

PORT="$PORT" NO_RELOAD=1 nohup "$PY" app.py > "$LOG" 2>&1 &
disown
sleep 2
if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/" | grep -q 200; then
  echo "started on http://localhost:$PORT  (log: $LOG)"
else
  echo "started but not responding yet — check $LOG"
fi
