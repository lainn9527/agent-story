#!/usr/bin/env bash
# Setup script for Claude Code cloud sessions (claude.ai/code environment "Setup script").
#
# Installs Python deps and, when llm_config.json is absent, generates it from
# environment variables set in the cloud environment settings. Keys never touch git.
#
#   GEMINI_API_KEY          required (comma-separated for several keys)
#   GEMINI_MODEL            optional, default gemini-3.8-flash
#   GEMINI_RECORDER_MODEL   optional Card Recorder override
#   OPENROUTER_API_KEY      optional
#   OPENROUTER_MODEL        optional
#
# Vertex (ADC) is not configured here; cloud sessions use the Gemini API key provider.
set -euo pipefail
cd "$(dirname "$0")/../.."

python3 -m pip install -q -r requirements.txt pytest

if [ -f llm_config.json ]; then
  echo "llm_config.json exists; leaving it unchanged"
  exit 0
fi

if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "GEMINI_API_KEY not set; skipping llm_config.json (LLM calls will fail)" >&2
  exit 0
fi

python3 - <<'EOF'
import json, os

keys = [k.strip() for k in os.environ["GEMINI_API_KEY"].split(",") if k.strip()]
gemini = {"api_keys": [{"key": k, "tier": "paid"} for k in keys],
          "model": os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")}
if os.environ.get("GEMINI_RECORDER_MODEL"):
    gemini["recorder_model"] = os.environ["GEMINI_RECORDER_MODEL"]
cfg = {"provider": "gemini", "gemini": gemini}
if os.environ.get("OPENROUTER_API_KEY"):
    cfg["open_router"] = {"api_key": os.environ["OPENROUTER_API_KEY"],
                          "model": os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash")}
with open("llm_config.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.chmod("llm_config.json", 0o600)
print(f"llm_config.json written (provider=gemini, {len(keys)} key(s))")
EOF
