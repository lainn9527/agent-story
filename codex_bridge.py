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
DEFAULT_MODEL = "gpt-5.3-codex"

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


def _codex_cmd(model: str) -> list[str]:
    return [
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
        "--model",
        model or DEFAULT_MODEL,
        "-",
    ]


def _run_codex_json(prompt: str, model: str) -> tuple[str, dict | None, str | None]:
    cmd = _codex_cmd(model)
    t0 = time.time()
    response = ""
    usage = None

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

    for raw_line in result.stdout.splitlines():
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

    elapsed = time.time() - t0

    if result.returncode != 0:
        err = result.stderr.strip() or f"Codex process exited with code {result.returncode}"
        log.info("    codex_bridge: FAILED in %.1fs rc=%d", elapsed, result.returncode)
        return "", usage, err

    if not response:
        log.info("    codex_bridge: empty response in %.1fs", elapsed)
        return "", usage, "Codex 回傳空白回應"

    log.info("    codex_bridge: OK in %.1fs response_len=%d", elapsed, len(response))
    return response, usage, None


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
    """Stream a GM response from Codex CLI JSONL events."""
    del session_id  # kept for API compat
    _tls.last_usage = None
    prompt = _build_chat_prompt(user_message, system_prompt, recent_messages)
    cmd = _codex_cmd(model)

    log.info("    codex_bridge_stream: calling CLI prompt_len=%d", len(prompt))
    t0 = time.time()

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=CLEAN_CWD,
        )
    except FileNotFoundError:
        yield ("error", "找不到 codex CLI。請確認已安裝 Codex CLI。")
        return
    except Exception as e:
        yield ("error", str(e))
        return

    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        yield ("error", str(e))
        return

    accumulated = ""
    usage = None

    try:
        while True:
            raw_line = proc.stdout.readline()
            if not raw_line:
                break
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = data.get("type")
            if event_type == "item.completed":
                item = data.get("item", {})
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = _extract_agent_text(item)
                    if text and text != accumulated:
                        delta = text[len(accumulated):] if text.startswith(accumulated) else text
                        accumulated = text
                        if delta:
                            yield ("text", delta)
            elif event_type == "turn.completed":
                usage = _normalize_usage(data.get("usage"))

        proc.wait(timeout=10)
        elapsed = time.time() - t0
        _tls.last_usage = usage

        if proc.returncode != 0 and not accumulated:
            stderr_text = proc.stderr.read().strip() if proc.stderr else ""
            error_msg = stderr_text or f"Codex process exited with code {proc.returncode}"
            log.info("    codex_bridge_stream: FAILED in %.1fs rc=%d", elapsed, proc.returncode)
            yield ("error", error_msg)
            return

        if not accumulated:
            yield ("error", "Codex 回傳空白回應")
            return

        log.info("    codex_bridge_stream: OK in %.1fs response_len=%d", elapsed, len(accumulated))
        yield ("done", {"response": accumulated, "session_id": None, "usage": usage})

    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        if accumulated:
            yield ("done", {"response": accumulated, "session_id": None, "usage": usage})
        else:
            yield ("error", str(e))
