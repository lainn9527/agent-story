"""OpenRouter bridge using its OpenAI-compatible Chat Completions API."""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("rpg")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_TIMEOUT = 120
_tls = threading.local()
try:
    import certifi
    _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _ssl_ctx = ssl.create_default_context()


def get_last_usage() -> dict | None:
    return getattr(_tls, "last_usage", None)


def _build_messages(recent_messages: list[dict], user_message: str,
                    system_prompt: str | None = None) -> list[dict]:
    """Convert app history to OpenAI chat messages without dropping Card history."""
    history = recent_messages
    if history and history[-1].get("role") == "user":
        history = history[:-1]

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for msg in history:
        role = "assistant" if msg.get("role") in ("gm", "assistant") else "user"
        messages.append({"role": role, "content": str(msg.get("content", ""))})
    messages.append({"role": "user", "content": user_message})
    return messages


def _normalize_usage(data: dict | None, *, provider: str | None = None) -> dict | None:
    if not data:
        return None
    prompt_details = data.get("prompt_tokens_details") or {}
    completion_details = data.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": data.get("prompt_tokens"),
        "output_tokens": data.get("completion_tokens"),
        "total_tokens": data.get("total_tokens"),
        "cached_tokens": prompt_details.get("cached_tokens"),
        "reasoning_tokens": completion_details.get("reasoning_tokens"),
        "cost_usd": data.get("cost"),
        "upstream_provider": provider,
    }


def _merge_usage(total: dict | None, current: dict | None) -> dict | None:
    if not current:
        return total
    if total is None:
        return dict(current)
    for key in ("prompt_tokens", "output_tokens", "total_tokens",
                "cached_tokens", "reasoning_tokens"):
        if current.get(key) is not None:
            total[key] = (total.get(key) or 0) + current[key]
    if current.get("cost_usd") is not None:
        total["cost_usd"] = (total.get("cost_usd") or 0.0) + current["cost_usd"]
    if current.get("upstream_provider"):
        total["upstream_provider"] = current["upstream_provider"]
    return total


def _reasoning_config(openrouter_cfg: dict) -> dict:
    value = openrouter_cfg.get("reasoning", False)
    if isinstance(value, dict):
        return value
    return {"enabled": bool(value)}


def _request_body(openrouter_cfg: dict, model: str, messages: list[dict], *,
                  temperature: float = 1.0, tools: list[dict] | None = None,
                  stream: bool = False) -> dict:
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": int(openrouter_cfg.get("max_output_tokens", 65536)),
        "reasoning": _reasoning_config(openrouter_cfg),
        "usage": {"include": True},
    }
    if tools:
        body["tools"] = tools
        body["parallel_tool_calls"] = False
    provider_sort = openrouter_cfg.get("provider_sort")
    if provider_sort:
        body["provider"] = {"sort": provider_sort}
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    return body


def _api_key(openrouter_cfg: dict) -> str:
    key = str(openrouter_cfg.get("api_key", "")).strip()
    if not key:
        raise ValueError("OpenRouter config missing 'api_key'")
    return key


def _open_request(openrouter_cfg: dict, body: dict):
    url = str(openrouter_cfg.get("base_url", OPENROUTER_URL)).strip() or OPENROUTER_URL
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_api_key(openrouter_cfg)}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost/story",
            "X-OpenRouter-Title": "Story RPG",
        },
    )
    return urllib.request.urlopen(
        req,
        timeout=int(openrouter_cfg.get("timeout", OPENROUTER_TIMEOUT)),
        context=_ssl_ctx,
    )


def _post_json(openrouter_cfg: dict, body: dict) -> dict:
    with _open_request(openrouter_cfg, body) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _error_text(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            message = payload.get("error", {}).get("message")
            if message:
                return f"HTTP {exc.code}: {message}"
        except Exception:
            pass
        return f"HTTP {exc.code}"
    return str(exc)


def _message_text(message: dict | None) -> str:
    if not message:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def call_openrouter_gm(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    openrouter_cfg: dict,
    model: str,
    session_id: str | None = None,
) -> tuple[str, None]:
    del session_id
    _tls.last_usage = None
    messages = _build_messages(recent_messages, user_message, system_prompt)
    body = _request_body(openrouter_cfg, model, messages)
    started = time.time()
    log.info("    openrouter_bridge: calling API model=%s messages=%d", model, len(messages))
    try:
        result = _post_json(openrouter_cfg, body)
    except Exception as exc:
        error = _error_text(exc)
        log.info("    openrouter_bridge: EXCEPTION %s", error)
        return f"【系統錯誤】OpenRouter API 錯誤：{error}", None

    _tls.last_usage = _normalize_usage(result.get("usage"), provider=result.get("provider"))
    choice = (result.get("choices") or [{}])[0]
    text = _message_text(choice.get("message")).strip()
    log.info("    openrouter_bridge: OK in %.1fs response_len=%d", time.time() - started, len(text))
    if not text:
        return "【系統錯誤】OpenRouter 回傳空白回應", None
    if choice.get("finish_reason") == "length":
        text += "\n\n【系統提示】回應因長度限制被截斷，請輸入「繼續」讓 GM 接續。"
    return text, None


def call_openrouter_gm_stream(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    openrouter_cfg: dict,
    model: str,
    session_id: str | None = None,
    tools: list[dict] | None = None,
):
    del session_id
    _tls.last_usage = None
    messages = _build_messages(recent_messages, user_message, system_prompt)
    body = _request_body(openrouter_cfg, model, messages, tools=tools, stream=True)
    started = time.time()
    accumulated = ""
    finish_reason = ""
    usage = None
    log.info("    openrouter_bridge_stream: calling API model=%s messages=%d", model, len(messages))
    try:
        with _open_request(openrouter_cfg, body) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if event.get("usage"):
                    usage = _normalize_usage(event["usage"], provider=event.get("provider"))
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                chunk = _message_text(choice.get("delta"))
                if chunk:
                    accumulated += chunk
                    yield ("text", chunk)
    except Exception as exc:
        error = _error_text(exc)
        log.info("    openrouter_bridge_stream: EXCEPTION %s", error)
        yield ("error", f"OpenRouter API 串流錯誤：{error}")
        return

    _tls.last_usage = usage
    log.info("    openrouter_bridge_stream: OK in %.1fs response_len=%d", time.time() - started, len(accumulated))
    if not accumulated:
        yield ("error", "OpenRouter 回傳空白回應")
        return
    if finish_reason == "length":
        suffix = "\n\n【系統提示】回應因長度限制被截斷，請輸入「繼續」讓 GM 接續。"
        accumulated += suffix
        yield ("text", suffix)
    yield ("done", {"response": accumulated, "session_id": None, "usage": usage})


def call_openrouter_gm_tools(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    openrouter_cfg: dict,
    model: str,
    on_tool=None,
    tool_decls: list | None = None,
    max_rounds: int = 6,
) -> tuple[str, list, str]:
    """Run an OpenAI-compatible function-calling loop for Card Recorder."""
    _tls.last_usage = None
    messages = _build_messages(recent_messages, user_message, system_prompt)
    tools = [
        {"type": "function", "function": declaration}
        for declaration in (tool_decls or [])
    ]
    executed: list = []
    narration_parts: list[str] = []
    total_usage = None

    for _round in range(max_rounds):
        body = _request_body(openrouter_cfg, model, messages, tools=tools)
        try:
            result = _post_json(openrouter_cfg, body)
        except Exception as exc:
            _tls.last_usage = total_usage
            return f"【系統錯誤】OpenRouter API 錯誤：{_error_text(exc)}", executed, "error"

        total_usage = _merge_usage(
            total_usage,
            _normalize_usage(result.get("usage"), provider=result.get("provider")),
        )
        choices = result.get("choices") or []
        if not choices:
            _tls.last_usage = total_usage
            return "【系統錯誤】OpenRouter 回傳空白回應", executed, "error"
        message = choices[0].get("message") or {}
        text = _message_text(message).strip()
        if text:
            narration_parts.append(text)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            break

        messages.append({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": tool_calls,
        })
        for call in tool_calls:
            function = call.get("function") or {}
            name = function.get("name", "")
            raw_args = function.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            result_payload = on_tool(name, args) if on_tool else {"ok": False, "error": "no_executor"}
            executed.append({"name": name, "args": args, "result": result_payload})
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": json.dumps(result_payload, ensure_ascii=False),
            })

    _tls.last_usage = total_usage
    return "\n".join(narration_parts).strip(), executed, "ok"


def call_openrouter_oneshot(
    prompt: str,
    openrouter_cfg: dict,
    model: str,
    system_prompt: str | None = None,
) -> str:
    text, _ = call_openrouter_gm(
        prompt,
        system_prompt or "",
        [],
        openrouter_cfg=openrouter_cfg,
        model=model,
    )
    if text.startswith("【系統錯誤】"):
        return ""
    return text.strip()
