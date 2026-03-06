"""Bridge to local Ollama API for GM conversation.

Uses http://localhost:11434/api/chat (and /api/chat with stream=true).
Supports Qwen 3.5 and other Ollama models; configure via llm_config.json:

  "provider": "ollama",
  "ollama": {
    "base_url": "http://localhost:11434",
    "model": "qwen3.5:latest",
    "think": false
  }
"""

import json
import logging
import threading
import urllib.error
import urllib.request

log = logging.getLogger("rpg")

# Thread-local for last usage (eval_count / prompt_eval_count from Ollama)
_tls = threading.local()

OLLAMA_TIMEOUT = 300  # seconds
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen3.5:latest"


def get_last_usage() -> dict | None:
    """Return usage from last Ollama call: prompt_tokens, output_tokens, total_tokens (best-effort)."""
    return getattr(_tls, "last_usage", None)


def _store_usage(prompt_eval_count: int | None, eval_count: int | None):
    """Store usage in thread-local. Ollama returns prompt_eval_count and eval_count."""
    _tls.last_usage = None
    if prompt_eval_count is not None or eval_count is not None:
        _tls.last_usage = {
            "prompt_tokens": prompt_eval_count,
            "output_tokens": eval_count,
            "total_tokens": (prompt_eval_count or 0) + (eval_count or 0),
        }


def _build_messages(
    system_prompt: str,
    recent_messages: list[dict],
    user_message: str,
) -> list[dict]:
    """Build Ollama messages: system (if any), then user/assistant from recent, then user_message."""
    messages: list[dict] = []
    if system_prompt and system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt.strip()})

    for msg in recent_messages:
        role = "assistant" if msg.get("role") in ("gm", "assistant") else "user"
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n\n" + content
        else:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_message})
    return messages


def _request(
    base_url: str,
    path: str,
    body: dict,
    timeout: int = OLLAMA_TIMEOUT,
) -> tuple[dict | None, dict | None]:
    """POST to Ollama (non-stream). Return (response_dict, error_dict)."""
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return (json.loads(raw) if raw else {}), None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return None, {"code": e.code, "body": body}
    except urllib.error.URLError as e:
        return None, {"reason": str(e.reason) if hasattr(e, "reason") else str(e)}
    except json.JSONDecodeError as e:
        return None, {"parse_error": str(e)}


def _read_stream(resp):
    """Read NDJSON stream from response; yield (chunk_text, done, eval_count, prompt_eval_count)."""
    buffer = ""
    for line in resp:
        buffer += line.decode("utf-8", errors="replace")
        while "\n" in buffer:
            part, buffer = buffer.split("\n", 1)
            part = part.strip()
            if not part:
                continue
            try:
                data = json.loads(part)
            except json.JSONDecodeError:
                continue
            msg = data.get("message") or {}
            content = msg.get("content") or ""
            done = data.get("done", False)
            eval_count = data.get("eval_count")
            prompt_eval_count = data.get("prompt_eval_count")
            yield content, done, eval_count, prompt_eval_count


# ---------------------------------------------------------------------------
# GM call — non-streaming
# ---------------------------------------------------------------------------

def call_ollama_gm(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    session_id: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    think: bool = False,
) -> tuple[str, str | None]:
    """Non-streaming GM call. Returns (response_text, None). session_id ignored."""
    messages = _build_messages(system_prompt, recent_messages, user_message)
    body = {"model": model, "messages": messages, "stream": False, "think": think}

    log.info("    ollama_bridge: calling API model=%s messages=%d", model, len(messages))
    result, err = _request(base_url, "/api/chat", body)
    if err:
        log.info("    ollama_bridge: request failed — %s", err)
        return f"【系統錯誤】Ollama 請求失敗：{err.get('body') or err.get('reason', str(err))}", None

    msg = (result or {}).get("message") or {}
    text = (msg.get("content") or "").strip()
    _store_usage(result.get("prompt_eval_count"), result.get("eval_count"))
    if not text:
        return "【系統錯誤】Ollama 回傳空白回應", None
    return text, None


# ---------------------------------------------------------------------------
# GM call — streaming
# ---------------------------------------------------------------------------

def call_ollama_gm_stream(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    session_id: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    think: bool = False,
):
    """Stream GM response. Yields ("text", delta_str), ("done", {response, session_id, usage}), ("error", msg)."""
    messages = _build_messages(system_prompt, recent_messages, user_message)
    body = {"model": model, "messages": messages, "stream": True, "think": think}
    url = f"{base_url.rstrip('/')}/api/chat"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        log.info("    ollama_bridge_stream: HTTP %d — %s", e.code, body_text[:200])
        yield ("error", f"Ollama 請求失敗：{body_text[:200] or e.code}")
        return
    except urllib.error.URLError as e:
        log.info("    ollama_bridge_stream: URLError — %s", e)
        yield ("error", f"無法連線至 Ollama：{e.reason if hasattr(e, 'reason') else e}")
        return

    accumulated = ""
    last_eval_count = None
    last_prompt_eval_count = None

    try:
        for content, done, eval_count, prompt_eval_count in _read_stream(resp):
            if content:
                accumulated += content
                yield ("text", content)
            if eval_count is not None:
                last_eval_count = eval_count
            if prompt_eval_count is not None:
                last_prompt_eval_count = prompt_eval_count

        _store_usage(last_prompt_eval_count, last_eval_count)
        usage = get_last_usage()
        yield ("done", {"response": accumulated, "session_id": None, "usage": usage})
    except Exception as e:
        log.info("    ollama_bridge_stream: EXCEPTION %s", e)
        if accumulated:
            _store_usage(None, None)
            yield ("done", {"response": accumulated, "session_id": None, "usage": None})
        else:
            yield ("error", str(e))


# ---------------------------------------------------------------------------
# One-shot (tag extraction, NPC evolution, etc.)
# ---------------------------------------------------------------------------

def call_ollama_oneshot(
    prompt: str,
    system_prompt: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    think: bool = False,
) -> str:
    """One-shot completion. Returns response text or empty string."""
    messages = []
    if system_prompt and system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt.strip()})
    messages.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": messages, "stream": False, "think": think}

    result, err = _request(base_url, "/api/chat", body)
    if err:
        log.info("    ollama_bridge oneshot: request failed — %s", err)
        return ""
    msg = (result or {}).get("message") or {}
    _store_usage(result.get("prompt_eval_count"), result.get("eval_count"))
    return (msg.get("content") or "").strip()
