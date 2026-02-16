"""Bridge to Claude Code CLI — uses persistent sessions for GM conversation."""

import json
import logging
import os
import subprocess
import time

log = logging.getLogger("rpg")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CLAUDE_TIMEOUT = 300  # seconds (5 min, allows for complex responses)
CLEAN_CWD = "/tmp"    # avoid loading project CLAUDE.md into context
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/Users/eddylai/.local/bin/claude")

# Clean env for subprocess: remove CLAUDECODE to avoid nested-session detection
_CLEAN_ENV = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


def get_last_usage() -> dict | None:
    """Claude CLI does not provide token usage data. Always returns None."""
    return None


# ---------------------------------------------------------------------------
# GM call — persistent session (per-branch)
# ---------------------------------------------------------------------------

def call_claude_gm(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    session_id: str | None = None,
) -> tuple[str, str | None]:
    """Send a player message to Claude GM (stateless).

    Always uses system prompt + recent context (no --resume).
    session_id param kept for API compat but ignored.

    Returns (gm_response_text, None).
    """

    context_lines: list[str] = []
    for msg in recent_messages:
        prefix = "【玩家】" if msg["role"] == "user" else "【GM】"
        context_lines.append(f"{prefix}\n{msg['content']}")

    prompt = (
        "以下是最近的對話紀錄：\n\n"
        + "\n\n".join(context_lines)
        + f"\n\n---\n【玩家的新行動】\n{user_message}\n\n"
        "請以 GM 身份回應，繼續推進故事。"
    )
    cmd = [
        CLAUDE_BIN, "-p",
        "--model", "claude-sonnet-4-5-20250929",
        "--system-prompt", system_prompt,
        "--output-format", "json",
    ]

    log.info("    claude_bridge: calling CLI mode=stateless prompt_len=%d", len(prompt))
    t0 = time.time()

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            cwd=CLEAN_CWD,
            env=_CLEAN_ENV,
        )
        elapsed = time.time() - t0

        if result.returncode != 0:
            error_msg = result.stderr.strip() or "Unknown error"
            log.info("    claude_bridge: FAILED in %.1fs rc=%d err=%s", elapsed, result.returncode, error_msg[:100])
            return f"【系統錯誤】Claude 回應失敗：{error_msg}", None

        # Parse JSON response to extract result text
        try:
            data = json.loads(result.stdout)
            response_text = data.get("result", "").strip()
            log.info("    claude_bridge: OK in %.1fs response_len=%d", elapsed, len(response_text))
            if not response_text:
                return "【系統錯誤】Claude 回傳空白回應", None
            return response_text, None
        except json.JSONDecodeError:
            # Fallback: treat stdout as plain text
            text = result.stdout.strip()
            log.info("    claude_bridge: JSON parse failed, fallback plain text in %.1fs", elapsed)
            if text:
                return text, None
            return "【系統錯誤】無法解析 Claude 回應", None

    except subprocess.TimeoutExpired:
        log.info("    claude_bridge: TIMEOUT after %ds", CLAUDE_TIMEOUT)
        return "【系統錯誤】Claude 回應逾時，請稍後再試。", None
    except FileNotFoundError:
        log.info("    claude_bridge: claude CLI not found")
        return "【系統錯誤】找不到 claude CLI。請確認已安裝 Claude Code。", None
    except Exception as e:
        log.info("    claude_bridge: EXCEPTION %s", e)
        return f"【系統錯誤】{e}", None


# ---------------------------------------------------------------------------
# GM call — streaming (per-branch)
# ---------------------------------------------------------------------------

def call_claude_gm_stream(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    session_id: str | None = None,
):
    """Stream a GM response from Claude CLI (stateless).

    Always uses system prompt + recent context (no --resume).
    session_id param kept for API compat but ignored.

    Yields tuples:
      ("text", delta_str)          — incremental text chunk
      ("done", {response, session_id})  — final result (session_id always None)
      ("error", msg)               — on failure
    """

    context_lines: list[str] = []
    for msg in recent_messages:
        prefix = "【玩家】" if msg["role"] == "user" else "【GM】"
        context_lines.append(f"{prefix}\n{msg['content']}")

    prompt = (
        "以下是最近的對話紀錄：\n\n"
        + "\n\n".join(context_lines)
        + f"\n\n---\n【玩家的新行動】\n{user_message}\n\n"
        "請以 GM 身份回應，繼續推進故事。"
    )
    cmd = [
        CLAUDE_BIN, "-p",
        "--verbose",
        "--model", "claude-sonnet-4-5-20250929",
        "--system-prompt", system_prompt,
        "--output-format", "stream-json",
    ]

    log.info("    claude_bridge_stream: calling CLI mode=stateless prompt_len=%d", len(prompt))
    t0 = time.time()

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered for streaming
            cwd=CLEAN_CWD,
            env=_CLEAN_ENV,
        )
    except FileNotFoundError:
        log.info("    claude_bridge_stream: claude CLI not found")
        yield ("error", "找不到 claude CLI。請確認已安裝 Claude Code。")
        return
    except Exception as e:
        log.info("    claude_bridge_stream: Popen EXCEPTION %s", e)
        yield ("error", str(e))
        return

    # Write prompt to stdin and close it
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except Exception as e:
        log.info("    claude_bridge_stream: stdin write EXCEPTION %s", e)
        proc.kill()
        yield ("error", str(e))
        return

    accumulated = ""

    try:
        while True:
            raw_line = proc.stdout.readline()
            if not raw_line:
                break  # EOF
            line = raw_line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            if msg_type == "assistant":
                # Extract text from message.content[].text
                message = data.get("message", {})
                content_blocks = message.get("content", [])
                content = ""
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        content += block.get("text", "")
                if content and content != accumulated:
                    delta = content[len(accumulated):]
                    if delta:
                        accumulated = content
                        yield ("text", delta)

            elif msg_type == "result":
                # Final result
                result_text = data.get("result", "").strip()
                if result_text:
                    # Yield any remaining delta
                    if result_text != accumulated:
                        delta = result_text[len(accumulated):]
                        if delta:
                            yield ("text", delta)
                    accumulated = result_text

            elif msg_type == "error":
                error_msg = data.get("error", {}).get("message", "Unknown error")
                log.info("    claude_bridge_stream: error event: %s", error_msg)
                yield ("error", error_msg)
                proc.wait()
                return

        proc.wait(timeout=10)
        elapsed = time.time() - t0

        if proc.returncode != 0 and not accumulated:
            stderr_text = proc.stderr.read().strip() if proc.stderr else ""
            error_msg = stderr_text or f"Claude process exited with code {proc.returncode}"
            log.info("    claude_bridge_stream: FAILED in %.1fs rc=%d", elapsed, proc.returncode)
            yield ("error", error_msg)
            return

        log.info("    claude_bridge_stream: OK in %.1fs response_len=%d", elapsed, len(accumulated))

        if not accumulated:
            yield ("error", "Claude 回傳空白回應")
            return

        yield ("done", {"response": accumulated, "session_id": None, "usage": None})

    except Exception as e:
        log.info("    claude_bridge_stream: EXCEPTION %s", e)
        try:
            proc.kill()
        except Exception:
            pass
        if accumulated:
            yield ("done", {"response": accumulated, "session_id": None, "usage": None})
        else:
            yield ("error", str(e))

