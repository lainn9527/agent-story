"""Bridge to Google Vertex AI (Gemini models) via the google-genai SDK.

Uses `genai.Client(vertexai=True, project=..., location=...)`, which routes
through GCP's native Vertex channel and authenticates with Application
Default Credentials (the local `gcloud auth application-default login`).
No API key — the SDK fetches and refreshes the OAuth token itself.

The message/parse helpers are shared with gemini_bridge where the wire
schema matches; here we just hand `_build_contents`' dict output straight to
the SDK (it accepts the {"role", "parts":[{"text"}]} form).
"""

import logging
import threading
import time

from story_core.gemini_bridge import _build_contents, _format_grounding

log = logging.getLogger("rpg")

_tls = threading.local()

# ---------------------------------------------------------------------------
# Client (cached per (project, region), thread-safe)
# ---------------------------------------------------------------------------

_clients: dict = {}
_clients_lock = threading.Lock()


def _get_client(vertex_cfg: dict):
    project = vertex_cfg.get("project")
    region = vertex_cfg.get("region", "us-central1")
    if not project:
        raise ValueError("vertex config missing 'project'")
    cache_key = (project, region)
    with _clients_lock:
        client = _clients.get(cache_key)
        if client is None:
            from google import genai
            client = genai.Client(vertexai=True, project=project, location=region,
                                  http_options={"timeout": 90000, "retry_options": {"attempts": 1}})
            _clients[cache_key] = client
            log.info("    vertex_bridge: created client project=%s location=%s", project, region)
        return client


def get_last_usage() -> dict | None:
    """Usage metadata from the most recent call on this thread."""
    return getattr(_tls, "last_usage", None)


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------

def _safety_settings():
    from google.genai import types
    cats = [
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    ]
    return [types.SafetySetting(category=c, threshold="BLOCK_NONE") for c in cats]


def _build_config(system_prompt: str | None, temperature: float = 1.0,
                  max_tokens: int = 65536, tools=None):
    from google.genai import types
    kwargs = {
        "temperature": temperature,
        "max_output_tokens": max_tokens,
        "safety_settings": _safety_settings(),
    }
    if system_prompt:
        kwargs["system_instruction"] = system_prompt
    if tools:
        kwargs["tools"] = tools  # raw gemini-style dicts; SDK coerces
    return types.GenerateContentConfig(**kwargs)


def _usage_from(response) -> dict | None:
    meta = getattr(response, "usage_metadata", None)
    if not meta:
        return None
    return {
        "prompt_tokens": getattr(meta, "prompt_token_count", None),
        "output_tokens": getattr(meta, "candidates_token_count", None),
        "total_tokens": getattr(meta, "total_token_count", None),
    }


def _finish_reason(response) -> str:
    cands = getattr(response, "candidates", None) or []
    if not cands:
        return ""
    fr = getattr(cands[0], "finish_reason", "")
    return getattr(fr, "name", str(fr)) if fr else ""


def _grounding_from(response):
    cands = getattr(response, "candidates", None) or []
    if not cands:
        return None
    gm = getattr(cands[0], "grounding_metadata", None)
    if not gm:
        return None
    # SDK object -> dict shape that _format_grounding expects
    try:
        as_dict = gm.model_dump(by_alias=True, exclude_none=True)
    except Exception:
        return None
    return _format_grounding(as_dict)


_TRUNCATE_SUFFIX = "\n\n【系統提示】回應因長度限制被截斷，請輸入「繼續」讓 GM 接續。"


# ---------------------------------------------------------------------------
# GM call — non-streaming
# ---------------------------------------------------------------------------

def call_vertex_gm(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    vertex_cfg: dict,
    model: str = "gemini-2.5-flash",
    session_id: str | None = None,
) -> tuple[str, str | None]:
    """Send a player message to Vertex GM. Returns (gm_response_text, None)."""
    contents = _build_contents(recent_messages, user_message)
    _tls.last_usage = None

    log.info("    vertex_bridge: calling API model=%s contents_len=%d", model, len(contents))
    t0 = time.time()
    try:
        client = _get_client(vertex_cfg)
        resp = client.models.generate_content(
            model=model, contents=contents,
            config=_build_config(system_prompt),
        )
    except Exception as e:
        log.info("    vertex_bridge: EXCEPTION %s", e)
        return f"【系統錯誤】Vertex API 錯誤：{e}", None

    _tls.last_usage = _usage_from(resp)
    text = (resp.text or "").strip()
    log.info("    vertex_bridge: OK in %.1fs response_len=%d", time.time() - t0, len(text))

    if not text:
        if _finish_reason(resp) in ("SAFETY", "PROHIBITED_CONTENT"):
            return "【系統提示】Vertex 安全過濾已啟動，請調整輸入。", None
        return "【系統錯誤】Vertex 回傳空白回應", None

    if _finish_reason(resp) == "MAX_TOKENS":
        log.warning("    vertex_bridge: response truncated (MAX_TOKENS)")
        text += _TRUNCATE_SUFFIX
    return text, None


# ---------------------------------------------------------------------------
# GM call — function-calling loop
# ---------------------------------------------------------------------------

def call_vertex_gm_tools(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    vertex_cfg: dict,
    model: str = "gemini-2.5-flash",
    on_tool=None,
    tool_decls: list | None = None,
    max_rounds: int = 6,
) -> tuple[str, list, str]:
    """Function-calling GM turn (see gemini_bridge.call_gemini_gm_tools)."""
    from google.genai import types

    contents = _build_contents(recent_messages, user_message)
    tools = [{"functionDeclarations": tool_decls}] if tool_decls else None
    config = _build_config(system_prompt, tools=tools)
    executed: list = []
    narration_parts: list = []
    _tls.last_usage = None

    try:
        client = _get_client(vertex_cfg)
        for _round in range(max_rounds):
            resp = client.models.generate_content(model=model, contents=contents, config=config)
            if resp.text:
                narration_parts.append(resp.text)
            fcalls = list(getattr(resp, "function_calls", None) or [])
            if not fcalls:
                break
            contents.append(resp.candidates[0].content)
            resp_parts = []
            for fc in fcalls:
                name = fc.name
                args = dict(fc.args or {})
                res = on_tool(name, args) if on_tool else {"ok": False, "error": "no_executor"}
                executed.append({"name": name, "args": args, "result": res})
                resp_parts.append(types.Part.from_function_response(name=name, response=res))
            contents.append(types.Content(role="user", parts=resp_parts))
    except Exception as e:
        log.info("    vertex_bridge_tools: EXCEPTION %s", e)
        return f"【系統錯誤】Vertex API 錯誤：{e}", executed, "error"

    return "\n".join(p for p in narration_parts if p).strip(), executed, "ok"


# ---------------------------------------------------------------------------
# GM call — streaming
# ---------------------------------------------------------------------------

def call_vertex_gm_stream(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    vertex_cfg: dict,
    model: str = "gemini-2.5-flash",
    session_id: str | None = None,
    tools: list[dict] | None = None,
):
    """Stream a GM response from Vertex. Yields ("text"|"done"|"error", payload)."""
    contents = _build_contents(recent_messages, user_message)

    log.info("    vertex_bridge_stream: calling API model=%s contents_len=%d", model, len(contents))
    t0 = time.time()

    accumulated = ""
    truncated = False
    usage_metadata = None
    grounding = None
    try:
        client = _get_client(vertex_cfg)
        stream = client.models.generate_content_stream(
            model=model, contents=contents,
            config=_build_config(system_prompt, tools=tools),
        )
        for chunk in stream:
            text = getattr(chunk, "text", None) or ""
            if text:
                accumulated += text
                yield ("text", text)
            u = _usage_from(chunk)
            if u:
                usage_metadata = u
            if _finish_reason(chunk) == "MAX_TOKENS":
                truncated = True
            g = _grounding_from(chunk)
            if g:
                grounding = g
    except Exception as e:
        log.info("    vertex_bridge_stream: EXCEPTION %s", e)
        if accumulated:
            yield ("done", {"response": accumulated, "session_id": None, "incomplete": True})
        else:
            yield ("error", f"Vertex API 串流錯誤：{e}")
        return

    log.info("    vertex_bridge_stream: OK in %.1fs response_len=%d", time.time() - t0, len(accumulated))

    if not accumulated:
        yield ("error", "Vertex 回傳空白回應")
        return

    if truncated:
        accumulated += _TRUNCATE_SUFFIX
        yield ("text", _TRUNCATE_SUFFIX)

    done_payload = {"response": accumulated, "session_id": None, "usage": usage_metadata}
    if grounding:
        done_payload["grounding"] = grounding
    yield ("done", done_payload)


# ---------------------------------------------------------------------------
# One-shot call (NPC evolution, summaries, etc.)
# ---------------------------------------------------------------------------

def call_vertex_oneshot(
    prompt: str,
    vertex_cfg: dict,
    model: str = "gemini-2.5-flash",
    system_prompt: str | None = None,
    *, response_schema: dict | None = None,
) -> str:
    """Simple one-shot Vertex call. Returns response text or empty string."""
    _tls.last_usage = None
    try:
        client = _get_client(vertex_cfg)
        config = _build_config(system_prompt, temperature=0.8)
        if response_schema is not None:
            from google.genai import types
            config.response_mime_type = "application/json"
            config.response_json_schema = response_schema
            config.tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="NONE"))
        resp = client.models.generate_content(
            model=model, contents=prompt,
            config=config,
        )
    except Exception as e:
        log.info("    vertex_oneshot: %s", e)
        if response_schema is not None:
            log.warning("vertex_oneshot structured request failed: %s", type(e).__name__)
            if getattr(e, "code", None) in (429, 500, 502, 503, 504) or isinstance(e, (TimeoutError, ConnectionError)):
                from story_core.llm_errors import TransientStructuredError
                raise TransientStructuredError(f"Vertex structured request temporarily unavailable: {getattr(e, 'code', type(e).__name__)}") from e
            raise ValueError(f"Vertex structured request failed: {type(e).__name__}") from e
        return ""
    _tls.last_usage = _usage_from(resp)
    text = (resp.text or "").strip()
    if response_schema is not None and (not text or _finish_reason(resp) != "STOP"):
        reason = _finish_reason(resp) or "EMPTY_RESPONSE"
        log.warning("vertex_oneshot structured response failed: finish_reason=%s", reason)
        raise ValueError(f"Vertex structured response failed: {reason}")
    return text
