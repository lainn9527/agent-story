"""Gemini API key pool with rate-limit cooldown tracking."""

import logging
import threading
import time

log = logging.getLogger("rpg")

_lock = threading.Lock()
_cooldowns: dict[str, float] = {}  # api_key -> cooldown_expires_at

COOLDOWN_SECONDS = 60


def load_keys(gemini_cfg: dict) -> list[dict]:
    """Parse both old and new config formats into a list of key dicts.

    New format: {"api_keys": [{"key": "...", "tier": "free"}, ...]}
    Old format: {"api_key": "..."}
    Returns: [{"key": "...", "tier": "free"}, ...]
    """
    if "api_keys" in gemini_cfg:
        return gemini_cfg["api_keys"]
    # Backward compat: single api_key string
    single = gemini_cfg.get("api_key", "")
    if single:
        return [{"key": single, "tier": "free"}]
    return []


def get_available_keys(gemini_cfg: dict) -> list[dict]:
    """Return keys not in cooldown, ordered: free keys first, paid last."""
    keys = load_keys(gemini_cfg)
    now = time.time()
    with _lock:
        available = [k for k in keys if _cooldowns.get(k["key"], 0) <= now]
    # Sort: free before paid
    available.sort(key=lambda k: 0 if k.get("tier") == "free" else 1)
    return available


def mark_rate_limited(api_key: str, cooldown: int = COOLDOWN_SECONDS):
    """Mark a key as rate-limited for `cooldown` seconds."""
    with _lock:
        _cooldowns[api_key] = time.time() + cooldown
    log.info("    gemini_key_mgr: key ...%s rate-limited for %ds", api_key[-6:], cooldown)
