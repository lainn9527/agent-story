"""LLM Bridge — dispatches GM calls to the configured provider.

Swap provider by editing llm_config.json (auto-reloads on file change).
Supported providers: "gemini", "claude_cli"
"""

import json
import logging
import os
import subprocess
import threading

log = logging.getLogger("rpg")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")

# ---------------------------------------------------------------------------
# Config (auto-reload on file change)
# ---------------------------------------------------------------------------

_config_cache: dict | None = None
_config_mtime: float = 0
_provider_override = threading.local()


def set_provider(provider: str | None):
    """Override the provider for this thread (safe for multi-agent)."""
    _provider_override.value = provider
    log.info("llm_bridge: provider override set to %s (thread %s)",
             provider, threading.current_thread().name)


def _get_config() -> dict:
    global _config_cache, _config_mtime
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
        if _config_cache is None or mtime != _config_mtime:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                _config_cache = json.load(f)
            _config_mtime = mtime
            log.info("llm_bridge: loaded config — provider=%s", _config_cache.get("provider"))
    except Exception:
        if _config_cache is None:
            _config_cache = {"provider": "claude_cli"}
    return _config_cache


def get_provider() -> str:
    override = getattr(_provider_override, 'value', None)
    if override:
        return override
    return _get_config().get("provider", "claude_cli")


# ---------------------------------------------------------------------------
# GM call — non-streaming
# ---------------------------------------------------------------------------

def call_claude_gm(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    session_id: str | None = None,
) -> tuple[str, str | None]:
    """Unified GM call. Returns (response_text, session_id_or_none)."""
    cfg = _get_config()
    provider = get_provider()

    if provider == "gemini":
        from gemini_bridge import call_gemini_gm
        g = cfg.get("gemini", {})
        return call_gemini_gm(
            user_message, system_prompt, recent_messages,
            api_key=g["api_key"], model=g.get("model", "gemini-2.0-flash"),
            session_id=session_id,
        )

    # Default: Claude CLI
    from claude_bridge import call_claude_gm as _claude
    return _claude(user_message, system_prompt, recent_messages, session_id=session_id)


# ---------------------------------------------------------------------------
# GM call — streaming
# ---------------------------------------------------------------------------

def call_claude_gm_stream(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    session_id: str | None = None,
):
    """Unified streaming GM call. Yields ("text"|"done"|"error", payload)."""
    cfg = _get_config()
    provider = get_provider()

    if provider == "gemini":
        from gemini_bridge import call_gemini_gm_stream
        g = cfg.get("gemini", {})
        yield from call_gemini_gm_stream(
            user_message, system_prompt, recent_messages,
            api_key=g["api_key"], model=g.get("model", "gemini-2.0-flash"),
            session_id=session_id,
        )
        return

    # Default: Claude CLI
    from claude_bridge import call_claude_gm_stream as _stream
    yield from _stream(user_message, system_prompt, recent_messages, session_id=session_id)


# ---------------------------------------------------------------------------
# Story summary
# ---------------------------------------------------------------------------

def generate_story_summary(conversation_text: str, summary_path: str | None = None) -> str:
    """Unified story summary generation."""
    cfg = _get_config()
    provider = get_provider()

    if provider == "gemini":
        from gemini_bridge import generate_story_summary_gemini
        g = cfg.get("gemini", {})
        return generate_story_summary_gemini(
            conversation_text, summary_path,
            api_key=g["api_key"], model=g.get("model", "gemini-2.0-flash"),
        )

    from claude_bridge import generate_story_summary as _summary
    return _summary(conversation_text, summary_path)


# ---------------------------------------------------------------------------
# One-shot call (NPC evolution, etc.)
# ---------------------------------------------------------------------------

def call_oneshot(prompt: str, system_prompt: str | None = None) -> str:
    """One-shot LLM call. Returns response text or empty string."""
    cfg = _get_config()
    provider = get_provider()

    if provider == "gemini":
        from gemini_bridge import call_gemini_oneshot
        g = cfg.get("gemini", {})
        return call_gemini_oneshot(
            prompt,
            api_key=g["api_key"], model=g.get("model", "gemini-2.0-flash"),
            system_prompt=system_prompt,
        )

    # Claude CLI one-shot
    from claude_bridge import CLAUDE_BIN
    model = cfg.get("claude_cli", {}).get("model", "claude-sonnet-4-5-20250929")
    cmd = [CLAUDE_BIN, "-p", "--output-format", "json", "--model", model]
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])
    try:
        result = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data.get("result", "").strip()
    except Exception as e:
        log.info("llm_bridge: claude_cli oneshot EXCEPTION %s", e)
    return ""


# ---------------------------------------------------------------------------
# Web-grounded search (always uses Gemini for Google Search grounding)
# ---------------------------------------------------------------------------

def web_search(query: str) -> str:
    """Search the web via Gemini's Google Search grounding.

    Always uses Gemini regardless of the active provider, since only
    Gemini supports Google Search grounding.
    Returns grounded response text or empty string on failure.
    """
    cfg = _get_config()
    g = cfg.get("gemini", {})
    api_key = g.get("api_key")
    if not api_key:
        log.info("llm_bridge: web_search skipped — no Gemini API key")
        return ""
    from gemini_bridge import call_gemini_grounded_search
    return call_gemini_grounded_search(
        query, api_key=api_key, model=g.get("model", "gemini-2.5-flash"),
    )
