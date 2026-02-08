"""Bridge to Google Gemini API for GM conversation."""

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request

from gemini_key_manager import get_available_keys, mark_rate_limited

log = logging.getLogger("rpg")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
GEMINI_TIMEOUT = 120  # seconds

# Build SSL context using certifi certificates (fixes macOS SSL issues)
try:
    import certifi
    _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _ssl_ctx = ssl.create_default_context()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_contents(recent_messages: list[dict], user_message: str) -> list[dict]:
    """Convert app messages to Gemini conversation format.

    recent_messages typically ends with the current user message (raw).
    user_message is the augmented version (with lore/events prepended).
    We use recent[:-1] as history and user_message as the new turn.
    """
    contents: list[dict] = []

    # History = everything except the last message (which is the raw user msg)
    history = recent_messages[:-1] if recent_messages else []

    for msg in history:
        role = "model" if msg.get("role") in ("gm", "assistant") else "user"
        text = msg.get("content", "")
        if not text:
            continue
        # Gemini requires strictly alternating roles — merge if needed
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"][0]["text"] += "\n\n" + text
        else:
            contents.append({"role": role, "parts": [{"text": text}]})

    # Append the augmented user message
    if contents and contents[-1]["role"] == "user":
        # Merge with previous user message (shouldn't normally happen)
        contents[-1]["parts"][0]["text"] += "\n\n" + user_message
    else:
        contents.append({"role": "user", "parts": [{"text": user_message}]})

    # Gemini requires the first message to be from user
    if contents and contents[0]["role"] == "model":
        contents.insert(0, {"role": "user", "parts": [{"text": "（故事開始）"}]})

    return contents


def _make_request_body(system_prompt: str, contents: list[dict],
                       temperature: float = 1.0, max_tokens: int = 65536) -> dict:
    body: dict = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }
    if system_prompt:
        body["system_instruction"] = {"parts": [{"text": system_prompt}]}
    return body


def _extract_text(response_data: dict) -> str:
    """Extract text from a Gemini generateContent response."""
    candidates = response_data.get("candidates", [])
    if not candidates:
        block_reason = response_data.get("promptFeedback", {}).get("blockReason", "")
        if block_reason:
            return f"【系統提示】Gemini 安全過濾已啟動（{block_reason}），請調整輸入。"
        return ""

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason", "")
    if finish_reason == "SAFETY":
        return "【系統提示】Gemini 安全過濾已啟動，請調整輸入。"

    parts = candidate.get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


# ---------------------------------------------------------------------------
# Key fallback wrapper (non-streaming)
# ---------------------------------------------------------------------------

def _is_key_error(http_code: int, body_text: str) -> bool:
    """Check if an HTTP error indicates a bad/expired API key (should try next)."""
    if http_code == 429:
        return True
    if http_code == 400 and "api key" in body_text.lower():
        return True
    if http_code in (401, 403):
        return True
    return False


def _with_key_fallback(gemini_cfg: dict, fn):
    """Try fn(api_key) with each available key. On key errors, mark and try next.

    fn should raise urllib.error.HTTPError on failure or return the result.
    """
    keys = get_available_keys(gemini_cfg)
    if not keys:
        return None, "所有 Gemini API key 都在冷卻中，請稍後再試"

    last_err = None
    for key_info in keys:
        api_key = key_info["key"]
        try:
            return fn(api_key), None
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:300]
            if _is_key_error(e.code, body_text):
                mark_rate_limited(api_key)
                last_err = f"API key ...{api_key[-6:]} failed (HTTP {e.code})"
                log.info("    gemini_bridge: HTTP %d on key ...%s, trying next — %s",
                         e.code, api_key[-6:], body_text[:100])
                continue
            # Non-key error — don't retry
            return None, f"Gemini API HTTP {e.code}：{body_text}"
        except Exception as e:
            return None, f"Gemini API 錯誤：{e}"

    return None, f"【系統錯誤】所有 API key 都失敗：{last_err}"


# ---------------------------------------------------------------------------
# GM call — non-streaming
# ---------------------------------------------------------------------------

def call_gemini_gm(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    gemini_cfg: dict,
    model: str = "gemini-2.0-flash",
    session_id: str | None = None,
) -> tuple[str, str | None]:
    """Send a player message to Gemini GM.

    Returns (gm_response_text, None).
    session_id is accepted for interface compatibility but ignored.
    """
    contents = _build_contents(recent_messages, user_message)
    body = _make_request_body(system_prompt, contents)
    payload = json.dumps(body).encode("utf-8")

    log.info("    gemini_bridge: calling API model=%s contents_len=%d", model, len(contents))
    t0 = time.time()

    def _do(api_key):
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
            f":generateContent?key={api_key}"
        )
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT, context=_ssl_ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))

    result, err = _with_key_fallback(gemini_cfg, _do)

    if err:
        return f"【系統錯誤】{err}", None

    elapsed = time.time() - t0
    text = _extract_text(result).strip()
    log.info("    gemini_bridge: OK in %.1fs response_len=%d", elapsed, len(text))

    if not text:
        return "【系統錯誤】Gemini 回傳空白回應", None

    # Check for MAX_TOKENS truncation
    candidates = result.get("candidates", [])
    if candidates and candidates[0].get("finishReason") == "MAX_TOKENS":
        log.warning("    gemini_bridge: response truncated (MAX_TOKENS)")
        text += "\n\n【系統提示】回應因長度限制被截斷，請輸入「繼續」讓 GM 接續。"

    return text, None


# ---------------------------------------------------------------------------
# GM call — streaming (SSE)
# ---------------------------------------------------------------------------

def call_gemini_gm_stream(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    gemini_cfg: dict,
    model: str = "gemini-2.0-flash",
    session_id: str | None = None,
):
    """Stream a GM response from Gemini API.

    Yields tuples:
      ("text", delta_str)                 — incremental text chunk
      ("done", {response, session_id})    — final result
      ("error", msg)                      — on failure
    """
    contents = _build_contents(recent_messages, user_message)
    body = _make_request_body(system_prompt, contents)
    payload = json.dumps(body).encode("utf-8")

    log.info("    gemini_bridge_stream: calling API model=%s contents_len=%d", model, len(contents))
    t0 = time.time()

    # Try each available key until one connects successfully
    keys = get_available_keys(gemini_cfg)
    if not keys:
        yield ("error", "所有 Gemini API key 都在冷卻中，請稍後再試")
        return

    resp = None
    for key_info in keys:
        api_key = key_info["key"]
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
            f":streamGenerateContent?alt=sse&key={api_key}"
        )
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT, context=_ssl_ctx)
            break  # connected OK
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:300]
            if _is_key_error(e.code, body_text):
                mark_rate_limited(api_key)
                log.info("    gemini_bridge_stream: HTTP %d on key ...%s, trying next", e.code, api_key[-6:])
                continue
            log.info("    gemini_bridge_stream: HTTP %d — %s", e.code, body_text)
            yield ("error", f"Gemini API HTTP {e.code}：{body_text}")
            return
        except Exception as e:
            log.info("    gemini_bridge_stream: EXCEPTION on connect — %s", e)
            yield ("error", f"Gemini API 連線失敗：{e}")
            return

    if resp is None:
        yield ("error", "所有 Gemini API key 都失敗")
        return

    accumulated = ""
    truncated = False
    try:
        while True:
            raw_line = resp.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue

            try:
                event_data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            # Check for errors in the event
            if "error" in event_data:
                err_msg = event_data["error"].get("message", "Unknown error")
                log.info("    gemini_bridge_stream: API error — %s", err_msg)
                yield ("error", err_msg)
                resp.close()
                return

            text = _extract_text(event_data)
            if text:
                accumulated += text
                yield ("text", text)

            # Check for MAX_TOKENS truncation
            candidates = event_data.get("candidates", [])
            if candidates:
                finish_reason = candidates[0].get("finishReason", "")
                if finish_reason == "MAX_TOKENS":
                    log.warning("    gemini_bridge_stream: response truncated (MAX_TOKENS)")
                    truncated = True

        resp.close()
        elapsed = time.time() - t0
        log.info("    gemini_bridge_stream: OK in %.1fs response_len=%d", elapsed, len(accumulated))

        if not accumulated:
            yield ("error", "Gemini 回傳空白回應")
            return

        if truncated:
            suffix = "\n\n【系統提示】回應因長度限制被截斷，請輸入「繼續」讓 GM 接續。"
            accumulated += suffix
            yield ("text", suffix)

        yield ("done", {"response": accumulated, "session_id": None})

    except Exception as e:
        log.info("    gemini_bridge_stream: EXCEPTION %s", e)
        try:
            resp.close()
        except Exception:
            pass
        if accumulated:
            yield ("done", {"response": accumulated, "session_id": None})
        else:
            yield ("error", f"Gemini API 串流錯誤：{e}")


# ---------------------------------------------------------------------------
# One-shot call (NPC evolution, summaries, etc.)
# ---------------------------------------------------------------------------

def call_gemini_grounded_search(
    query: str,
    gemini_cfg: dict,
    model: str = "gemini-2.5-flash",
) -> str:
    """Search the web via Gemini's Google Search grounding. Returns grounded response text."""
    search_body = {
        "contents": [{"role": "user", "parts": [{"text": query}]}],
        "tools": [{"googleSearch": {}}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 2048,
        },
    }
    payload = json.dumps(search_body).encode("utf-8")

    def _do(api_key):
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
            f":generateContent?key={api_key}"
        )
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60, context=_ssl_ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))

    result, err = _with_key_fallback(gemini_cfg, _do)
    if err:
        log.info("    gemini_grounded_search: %s", err)
        return ""

    text = _extract_text(result).strip()
    log.info("    gemini_grounded_search: OK query_len=%d response_len=%d", len(query), len(text))
    return text


def call_gemini_oneshot(
    prompt: str,
    gemini_cfg: dict,
    model: str = "gemini-2.0-flash",
    system_prompt: str | None = None,
) -> str:
    """Simple one-shot Gemini call. Returns response text or empty string."""
    contents = [{"role": "user", "parts": [{"text": prompt}]}]
    body = _make_request_body(system_prompt or "", contents, temperature=0.8)
    payload = json.dumps(body).encode("utf-8")

    def _do(api_key):
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
            f":generateContent?key={api_key}"
        )
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT, context=_ssl_ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))

    result, err = _with_key_fallback(gemini_cfg, _do)
    if err:
        log.info("    gemini_oneshot: %s", err)
        return ""

    return _extract_text(result).strip()


# ---------------------------------------------------------------------------
# Story summary
# ---------------------------------------------------------------------------

def generate_story_summary_gemini(
    conversation_text: str,
    summary_path: str | None,
    gemini_cfg: dict,
    model: str = "gemini-2.0-flash",
) -> str:
    """Generate a story summary via Gemini. Caches result to file."""
    if summary_path is None:
        summary_path = os.path.join(DATA_DIR, "story_summary.txt")

    if os.path.exists(summary_path):
        with open(summary_path, "r", encoding="utf-8") as f:
            cached = f.read().strip()
            if cached:
                return cached

    prompt = (
        "以下是一段「諸天無限流·主神空間」文字 RPG 的完整對話紀錄。"
        "請用繁體中文寫一份約 2000 字的故事摘要，包含：\n"
        "1. 主角的角色設定與性格\n"
        "2. 團隊成員介紹與關係\n"
        "3. 任務的完整經過（關鍵事件、轉折、結局）\n"
        "4. 獲得的道具與獎勵\n"
        "5. 重要的伏筆或未解之謎\n"
        "6. 角色目前的狀態與心境\n\n"
        "---\n\n"
        f"{conversation_text}"
    )

    text = call_gemini_oneshot(prompt, gemini_cfg=gemini_cfg, model=model)
    if text:
        os.makedirs(os.path.dirname(summary_path), exist_ok=True)
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(text)
        return text

    # Fallback
    return (
        "【故事摘要】\n"
        "（尚未生成摘要，請稍後再試。）"
    )
