#!/bin/bash
# Deploy latest main to production (story-prod)
# Usage: ./deploy.sh

set -e

PROD_DIR="/Users/eddylai/story-prod"

echo "==> Fetching latest main..."
git fetch origin main

echo "==> Updating story-prod..."
cd "$PROD_DIR"
git checkout FETCH_HEAD 2>/dev/null || git fetch origin main && git checkout FETCH_HEAD

echo "==> Restarting server on port 5051..."
lsof -ti:5051 | xargs kill 2>/dev/null || true
sleep 1
nohup python3 app.py >> server_stderr.log 2>&1 &
sleep 2

# Verify
if curl -s http://localhost:5051/api/config | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Deploy OK — v{d[\"version\"]} ({d[\"provider\"]})')" 2>/dev/null; then
  echo "==> Done!"
else
  echo "==> ERROR: Server failed to start. Check server.log and server_stderr.log"
  exit 1
fi
