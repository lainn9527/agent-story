#!/bin/bash
# Deploy latest main to WSL2 (in-place)
# Usage: ./deploy_wsl2.sh
#
# Requirements:
#   - Python packages installed: ~/.local/lib/python3.12/site-packages
#   - Claude CLI installed: ~/.npm-global/bin/claude
#   - systemd service: sudo systemctl enable rpg-server (see doc/WSL2_SETUP.md)
#   - Run from the repo root directory

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/usr/bin/python3"
PYTHONPATH_WSL="$HOME/.local/lib/python3.12/site-packages"
PORT="${PORT:-5051}"

echo "==> Pulling latest main..."
cd "$SCRIPT_DIR"
git fetch origin main
git checkout FETCH_HEAD

echo "==> Restarting server via systemd..."
sudo systemctl restart rpg-server

echo "==> Verifying..."
if PYTHONPATH="$PYTHONPATH_WSL" "$PYTHON" -c "
import urllib.request, json, time
time.sleep(2)
d = json.loads(urllib.request.urlopen('http://localhost:$PORT/api/config').read())
print(f'Deploy OK — v{d[\"version\"]} ({d[\"provider\"]})')
" 2>/dev/null; then
  echo "==> Done!"
else
  echo "==> ERROR: Server failed to start. Check: sudo journalctl -u rpg-server -n 30"
  exit 1
fi
