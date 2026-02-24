#!/bin/bash
# Deploy latest main to WSL2 (in-place)
# Usage: ./deploy_wsl2.sh
#
# Requirements:
#   - Python packages installed: ~/.local/lib/python3.12/site-packages
#   - Claude CLI installed: ~/.npm-global/bin/claude
#   - Run from the repo root directory

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/usr/bin/python3"
PYTHONPATH_WSL="$HOME/.local/lib/python3.12/site-packages"
CLAUDE_BIN_WSL="$HOME/.npm-global/bin/claude"
PORT="${PORT:-5051}"

echo "==> Pulling latest main..."
cd "$SCRIPT_DIR"
git fetch origin main
git checkout FETCH_HEAD

echo "==> Stopping existing server on port $PORT..."
lsof -ti:$PORT | xargs kill 2>/dev/null || true
sleep 1

echo "==> Starting server..."
PYTHONPATH="$PYTHONPATH_WSL" \
CLAUDE_BIN="$CLAUDE_BIN_WSL" \
nohup "$PYTHON" app.py >> server.log 2>&1 &
sleep 3

echo "==> Verifying..."
if PYTHONPATH="$PYTHONPATH_WSL" "$PYTHON" -c "
import urllib.request, json
d = json.loads(urllib.request.urlopen('http://localhost:$PORT/api/config').read())
print(f'Deploy OK — v{d[\"version\"]} ({d[\"provider\"]})')
" 2>/dev/null; then
  echo "==> Done!"
else
  echo "==> ERROR: Server failed to start. Check server.log"
  exit 1
fi
