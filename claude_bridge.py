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


# ---------------------------------------------------------------------------
# Story summary (one-shot, separate from GM session)
# ---------------------------------------------------------------------------

def generate_story_summary(conversation_text: str, summary_path: str | None = None) -> str:
    """Ask Claude to produce a ~2000-char summary of the full conversation.

    Args:
        conversation_text: The full conversation text to summarize.
        summary_path: Path to save the summary. If None, uses legacy default.
    """

    if summary_path is None:
        summary_path = os.path.join(DATA_DIR, "story_summary.txt")

    # Return cached version if it exists
    if os.path.exists(summary_path):
        with open(summary_path, "r", encoding="utf-8") as f:
            cached = f.read().strip()
            if cached:
                return cached

    prompt = (
        "以下是一段「諸天無限流·主神空間」文字 RPG 的完整對話紀錄。"
        "請用繁體中文寫一份約 2000 字的故事摘要，包含：\n"
        "1. 主角 Eddy 的角色設定與性格\n"
        "2. 團隊成員介紹與關係\n"
        "3. 《咒怨》任務的完整經過（關鍵事件、轉折、結局）\n"
        "4. 獲得的道具與獎勵\n"
        "5. 重要的伏筆或未解之謎\n"
        "6. 角色目前的狀態與心境\n\n"
        "---\n\n"
        f"{conversation_text}"
    )

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", "--output-format", "text"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=180,
            env=_CLEAN_ENV,
        )

        if result.returncode == 0 and result.stdout.strip():
            summary = result.stdout.strip()
            os.makedirs(os.path.dirname(summary_path), exist_ok=True)
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(summary)
            return summary

    except Exception:
        pass

    return _fallback_summary()


def _fallback_summary() -> str:
    return (
        "【故事摘要】\n"
        "主角 Eddy，30歲男性，被拉入主神空間成為輪迴者。\n"
        "首次任務：進入《咒怨》世界，與7名新人隊友（小薇、阿豪、美玲、Jack、小林等）"
        "在伽椰子的詛咒中求生。\n"
        "Eddy 以冷靜果決的領導力帶領全隊存活，最終以「被看見」的方式讓伽椰子選擇離去，"
        "達成完美通關（8/8存活）。\n"
        "獲得 5000 獎勵點、封印之鏡（紀念品）、自省之鏡玉佩、鎮魂符×3。\n"
        "與佐藤神主建立了深厚的信任關係，獲得臨別贈禮。\n"
        "目前處於 24 小時休整期尾聲，即將返回主神空間進行獎勵兌換。"
    )
