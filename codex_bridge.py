"""Bridge to Codex CLI — non-interactive JSON mode."""

import json
import logging
import os
import subprocess
import threading
import time

log = logging.getLogger("rpg")

CODEX_TIMEOUT = 300  # seconds
CLEAN_CWD = "/tmp"   # avoid loading project instructions into prompt context
CODEX_BIN = os.environ.get("CODEX_BIN", "/usr/local/bin/codex")
DEFAULT_MODEL = "default"

_tls = threading.local()


def get_last_usage() -> dict | None:
    """Return usage from the most recent Codex call on this thread."""
    return getattr(_tls, "last_usage", None)


def _normalize_usage(raw: dict | None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    prompt_tokens = raw.get("input_tokens")
    output_tokens = raw.get("output_tokens")
    total_tokens = None
    if isinstance(prompt_tokens, int) and isinstance(output_tokens, int):
        total_tokens = prompt_tokens + output_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _extract_agent_text(item: dict) -> str:
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    # Fallback for potential future payload variants
    content = item.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("text") or block.get("output_text")
            if isinstance(t, str) and t:
                parts.append(t)
        joined = "".join(parts).strip()
        if joined:
            return joined
    return ""


def _build_chat_prompt(user_message: str, system_prompt: str, recent_messages: list[dict]) -> str:
    context_lines: list[str] = []
    for msg in recent_messages:
        role = msg.get("role", "user")
        prefix = "【玩家】" if role == "user" else "【GM】"
        context_lines.append(f"{prefix}\n{msg.get('content', '')}")

    return (
        "[System Prompt]\n"
        + system_prompt.strip()
        + "\n\n[Conversation Context]\n"
        + ("\n\n".join(context_lines) if context_lines else "(empty)")
        + f"\n\n[玩家的新行動]\n{user_message}\n\n"
        "請以 GM 身份回應，延續故事。"
    )


def _build_oneshot_prompt(prompt: str, system_prompt: str | None = None) -> str:
    if system_prompt:
        return (
            "[System Prompt]\n"
            + system_prompt.strip()
            + "\n\n[User Prompt]\n"
            + prompt
        )
    return prompt


def _codex_cmd(model: str | None) -> list[str]:
    cmd = [
        CODEX_BIN,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--json",
        "-C",
        CLEAN_CWD,
    ]
    selected = (model or DEFAULT_MODEL).strip()
    if selected and selected != "default":
        cmd.extend(["--model", selected])
    cmd.append("-")
    return cmd


def _looks_like_model_error(stderr_text: str) -> bool:
    t = (stderr_text or "").lower()
    return (
        "unknown model" in t
        or "model not found" in t
        or "invalid model" in t
    )


def _parse_codex_json_output(stdout_text: str) -> tuple[str, dict | None]:
    response = ""
    usage = None
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "item.completed":
            item = data.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = _extract_agent_text(item)
                if text:
                    response = text
        elif data.get("type") == "turn.completed":
            usage = _normalize_usage(data.get("usage"))
    return response, usage


def _run_codex_json(prompt: str, model: str) -> tuple[str, dict | None, str | None]:
    t0 = time.time()
    attempts = [model]
    if model and model.strip() and model.strip() != "default":
        # Retry without --model when configured model is invalid on this Codex CLI build.
        attempts.append("default")

    last_err = ""
    last_usage = None

    for idx, attempt_model in enumerate(attempts):
        cmd = _codex_cmd(attempt_model)
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=CODEX_TIMEOUT,
                cwd=CLEAN_CWD,
            )
        except subprocess.TimeoutExpired:
            return "", None, "Codex 回應逾時，請稍後再試。"
        except FileNotFoundError:
            return "", None, "找不到 codex CLI。請確認已安裝 Codex CLI。"
        except Exception as e:
            return "", None, str(e)

        response, usage = _parse_codex_json_output(result.stdout)
        elapsed = time.time() - t0
        last_usage = usage

        if result.returncode == 0 and response:
            log.info("    codex_bridge: OK in %.1fs response_len=%d", elapsed, len(response))
            return response, usage, None

        stderr_text = (result.stderr or "").strip()
        last_err = stderr_text or f"Codex process exited with code {result.returncode}"

        should_retry = (
            idx < len(attempts) - 1
            and _looks_like_model_error(stderr_text)
        )
        if should_retry:
            log.info("    codex_bridge: retrying with default model due to invalid configured model")
            continue

        if result.returncode != 0:
            log.info("    codex_bridge: FAILED in %.1fs rc=%d", elapsed, result.returncode)
        elif not response:
            log.info("    codex_bridge: empty response in %.1fs", elapsed)
        break

    if not last_err:
        last_err = "Codex 回傳空白回應"
    return "", last_usage, last_err


def call_codex_gm(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    session_id: str | None = None,
    model: str = DEFAULT_MODEL,
) -> tuple[str, str | None]:
    """Send a player message to Codex GM (stateless)."""
    del session_id  # kept for API compat
    _tls.last_usage = None
    prompt = _build_chat_prompt(user_message, system_prompt, recent_messages)
    response, usage, err = _run_codex_json(prompt, model=model)
    _tls.last_usage = usage
    if err:
        return f"【系統錯誤】Codex 回應失敗：{err}", None
    return response, None


def call_codex_oneshot(prompt: str, system_prompt: str | None = None, model: str = DEFAULT_MODEL) -> str:
    """One-shot Codex call for background tasks."""
    _tls.last_usage = None
    full_prompt = _build_oneshot_prompt(prompt, system_prompt=system_prompt)
    response, usage, err = _run_codex_json(full_prompt, model=model)
    _tls.last_usage = usage
    if err:
        log.info("codex_bridge: oneshot FAILED: %s", err)
        return ""
    return response


def call_codex_gm_stream(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    session_id: str | None = None,
    model: str = DEFAULT_MODEL,
):
    """Pseudo-stream Codex response as a single chunk.

    Codex `exec --json` currently provides only completed message events
    (no token deltas), so we emit one text chunk followed by done.
    """
    del session_id  # kept for API compat
    _tls.last_usage = None
    prompt = _build_chat_prompt(user_message, system_prompt, recent_messages)
    response, usage, err = _run_codex_json(prompt, model=model)
    _tls.last_usage = usage
    if err:
        yield ("error", err)
        return
    yield ("text", response)
    yield ("done", {"response": response, "session_id": None, "usage": usage})
