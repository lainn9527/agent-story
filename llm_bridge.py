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

# Thread-local storage for last usage metadata (populated after each call)
_tls = threading.local()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")

# ---------------------------------------------------------------------------
# Config (auto-reload on file change)
# ---------------------------------------------------------------------------

_config_cache: dict | None = None
_config_mtime: float = 0
_provider_override: str | None = None


def set_provider(provider: str | None):
    """Override the provider for this process (useful for auto-play instances)."""
    global _provider_override
    _provider_override = provider
    log.info("llm_bridge: provider override set to %s", provider)


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
    if _provider_override:
        return _provider_override
    return _get_config().get("provider", "claude_cli")


def get_last_usage() -> dict | None:
    """Return usage metadata from the most recent LLM call on this thread.

    Returns dict with keys: provider, model, prompt_tokens, output_tokens, total_tokens.
    Returns None if no usage data is available (e.g. Claude CLI).
    """
    return getattr(_tls, "last_usage", None)


def _capture_usage(provider: str, model: str):
    """Read usage from the bridge module and store in our thread-local."""
    _tls.last_usage = None
    if provider == "gemini":
        from gemini_bridge import get_last_usage as _gemini_usage
        usage = _gemini_usage()
    else:
        from claude_bridge import get_last_usage as _claude_usage
        usage = _claude_usage()
    if usage:
        _tls.last_usage = {**usage, "provider": provider, "model": model}


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
        model = g.get("model", "gemini-2.0-flash")
        result = call_gemini_gm(
            user_message, system_prompt, recent_messages,
            gemini_cfg=g, model=model,
            session_id=session_id,
        )
        _capture_usage(provider, model)
        return result

    # Default: Claude CLI
    model = cfg.get("claude_cli", {}).get("model", "claude-sonnet-4-5-20250929")
    from claude_bridge import call_claude_gm as _claude
    result = _claude(user_message, system_prompt, recent_messages, session_id=session_id)
    _capture_usage(provider, model)
    return result


# ---------------------------------------------------------------------------
# GM call — streaming
# ---------------------------------------------------------------------------

def call_claude_gm_stream(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    session_id: str | None = None,
    tools: list[dict] | None = None,
):
    """Unified streaming GM call. Yields ("text"|"done"|"error", payload).

    The "done" payload includes a "usage" key with provider/model/token info
    (or None if unavailable).
    """
    cfg = _get_config()
    provider = get_provider()

    if provider == "gemini":
        from gemini_bridge import call_gemini_gm_stream
        g = cfg.get("gemini", {})
        model = g.get("model", "gemini-2.0-flash")
        for event_type, payload in call_gemini_gm_stream(
            user_message, system_prompt, recent_messages,
            gemini_cfg=g, model=model,
            session_id=session_id, tools=tools,
        ):
            if event_type == "done" and isinstance(payload, dict):
                usage = payload.get("usage")
                if usage:
                    payload["usage"] = {**usage, "provider": provider, "model": model}
            yield (event_type, payload)
        return

    # Default: Claude CLI (tools not supported)
    if tools:
        log.debug("llm_bridge: tools=%s ignored for provider %s", tools, provider)
    model = cfg.get("claude_cli", {}).get("model", "claude-sonnet-4-5-20250929")
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
        model = g.get("model", "gemini-2.0-flash")
        result = generate_story_summary_gemini(
            conversation_text, summary_path,
            gemini_cfg=g, model=model,
        )
        _capture_usage(provider, model)
        return result

    model = cfg.get("claude_cli", {}).get("model", "claude-sonnet-4-5-20250929")
    from claude_bridge import generate_story_summary as _summary
    result = _summary(conversation_text, summary_path)
    _capture_usage(provider, model)
    return result


# ---------------------------------------------------------------------------
# One-shot call (NPC evolution, etc.)
# ---------------------------------------------------------------------------

def call_oneshot(prompt: str, system_prompt: str | None = None, provider: str | None = None) -> str:
    """One-shot LLM call. Returns response text or empty string.

    Args:
        provider: Override provider for this call only (does not affect global config).
    """
    cfg = _get_config()
    if provider is None:
        provider = get_provider()

    if provider == "gemini":
        from gemini_bridge import call_gemini_oneshot
        g = cfg.get("gemini", {})
        model = g.get("model", "gemini-2.0-flash")
        result = call_gemini_oneshot(
            prompt,
            gemini_cfg=g, model=model,
            system_prompt=system_prompt,
        )
        _capture_usage(provider, model)
        return result

    # Claude CLI one-shot
    _tls.last_usage = None
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
# Embedding (local jina-embeddings-v2-base-zh via fastembed — no API calls)
# ---------------------------------------------------------------------------

_embed_model = None
_embed_lock = __import__("threading").Lock()


def _get_embed_model():
    """Lazy-load the fastembed model (singleton, thread-safe)."""
    global _embed_model
    if _embed_model is None:
        with _embed_lock:
            if _embed_model is None:
                from fastembed import TextEmbedding
                _embed_model = TextEmbedding("jinaai/jina-embeddings-v2-base-zh")
                log.info("llm_bridge: loaded local embedding model jina-embeddings-v2-base-zh")
    return _embed_model


def embed_text(text: str) -> list[float] | None:
    """Embed a single text locally via jina-zh. Returns 768-dim vector or None."""
    try:
        model = _get_embed_model()
        result = list(model.embed([text]))
        return result[0].tolist() if result else None
    except Exception as e:
        log.warning("llm_bridge: embed_text failed — %s", e)
        return None


def embed_texts_batch(texts: list[str]) -> list[list[float]] | None:
    """Batch-embed texts locally via jina-zh. Returns list of 768-dim vectors or None."""
    if not texts:
        return []
    try:
        model = _get_embed_model()
        return [v.tolist() for v in model.embed(texts)]
    except Exception as e:
        log.warning("llm_bridge: embed_texts_batch failed — %s", e)
        return None


# ---------------------------------------------------------------------------
# Gemini config gate (used by web search only)
# ---------------------------------------------------------------------------

def _get_gemini_cfg() -> dict | None:
    """Return Gemini config if Gemini is allowed and keys are configured.

    When provider is overridden (e.g. auto-play sets claude_cli),
    Gemini config is blocked entirely — no API keys, no access.
    """
    if _provider_override:
        return None
    cfg = _get_config()
    g = cfg.get("gemini", {})
    from gemini_key_manager import load_keys
    if not load_keys(g):
        return None
    return g


# ---------------------------------------------------------------------------
# Web-grounded search (always uses Gemini for Google Search grounding)
# ---------------------------------------------------------------------------

def web_search(query: str) -> str:
    """Search the web via Gemini's Google Search grounding.

    Blocked when provider is overridden to non-Gemini (e.g. auto-play).
    Returns grounded response text or empty string on failure.
    """
    g = _get_gemini_cfg()
    if not g:
        return ""
    from gemini_bridge import call_gemini_grounded_search
    model = g.get("model", "gemini-2.5-flash")
    result = call_gemini_grounded_search(query, gemini_cfg=g, model=model)
    _capture_usage("gemini", model)
    return result
